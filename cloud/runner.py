"""One authenticated job per ephemeral GitHub Actions runner."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.snapshot import digest, pack, unpack

ID_RE = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
RETRYABLE_CODES = {"BUSY", "SERVICE_TEMPORARY"}
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


class BridgeRequestError(RuntimeError):
    pass


def envelope(secret, payload, timestamp=None, nonce=None):
    stamp = int(time.time()) if timestamp is None else timestamp
    nonce = nonce or str(uuid.uuid4())
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    signature = hmac.new(secret.encode(), f"{stamp}\n{nonce}\n{encoded}".encode(), hashlib.sha256).hexdigest()
    return {"timestamp": stamp, "nonce": nonce, "payload": encoded, "signature": signature}


class Bridge:
    def __init__(self, url, secret, job_id, execution):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "script.google.com" or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parsed.path) or parsed.query:
            raise ValueError("BRIDGE_URL must be an Apps Script /exec URL")
        if len(secret) < 32 or not ID_RE.fullmatch(job_id):
            raise ValueError("Invalid bridge secret or job ID")
        self.url, self.secret, self.job_id, self.execution = url, secret, job_id, execution
        self.lease = None

    def call(self, action, **kwargs):
        payload = {"action": action, "job_id": self.job_id, "execution": self.execution, "lease": self.lease, **kwargs}
        # Stay below the five-minute heartbeat expiry; retry this request, not the collection.
        deadline = time.monotonic() + 210
        last_error = "클라우드 연결 응답이 없습니다."
        for attempt in range(5):
            body = json.dumps(envelope(self.secret, payload)).encode()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = min(100 if action == "commit" else 40, remaining)
                with urlopen(Request(self.url, data=body, headers={"Content-Type": "application/json"}), timeout=timeout) as response:
                    value = json.load(response)
                if not isinstance(value, dict):
                    raise BridgeRequestError("클라우드 응답 형식이 올바르지 않습니다. [INVALID_RESPONSE]")
                if not value.get("ok"):
                    code = str(value.get("code", "REQUEST_REJECTED"))
                    if not re.fullmatch(r"[A-Z_]{1,40}", code):
                        code = "REQUEST_REJECTED"
                    stage = value.get("stage", action)
                    if not isinstance(stage, str) or not re.fullmatch(r"[a-z_]{1,40}", stage):
                        stage = action
                    ref = value.get("reference", "")
                    ref = ref if isinstance(ref, str) and ID_RE.fullmatch(ref) else ""
                    last_error = f"{str(value.get('error', '클라우드 요청이 거절됐습니다.'))[:250]} [{code}; {stage}]"
                    if ref:
                        last_error += f" 참조: {ref}"
                    if code not in RETRYABLE_CODES or value.get("retryable") is not True:
                        raise BridgeRequestError(last_error)
                else:
                    return value["value"]
            except HTTPError as exc:
                last_error = f"Google 연결 HTTP 오류 [{exc.code}; {action}]"
                if exc.code not in RETRYABLE_HTTP:
                    raise BridgeRequestError(last_error) from None
            except URLError as exc:
                if isinstance(exc.reason, ssl.SSLError):
                    raise BridgeRequestError("보안 연결 인증서 확인에 실패했습니다. [TLS_ERROR]") from None
                last_error = f"네트워크 연결이 일시적으로 끊겼습니다. [{action}]"
            except (TimeoutError, json.JSONDecodeError, ConnectionError):
                last_error = f"Google 응답이 지연되거나 불완전합니다. [{action}]"
            if attempt < 4:
                delay = 2 ** (attempt + 1)
                if deadline - time.monotonic() <= delay:
                    break
                print(f"Cloud connection retry {attempt + 1}/4 ({action}).", flush=True)
                time.sleep(delay)
        raise BridgeRequestError("자동 재연결 횟수/시간을 초과했습니다. " + last_error)


def execute(bridge, manager, job):
    kind, payload = job["kind"], job["payload"]
    if kind in {"apply", "archive", "room-rules"}:
        if bridge.call("progress", progress={"message": "DB 수정 준비 중"})["stopRequested"]:
            raise RuntimeError("Job stopped before DB update")
        method = {"apply": manager.apply, "archive": manager.archive, "room-rules": manager.set_rules}[kind]
        response = method(payload)
        if "backup" in response:
            response["backup"] = "Google Drive / 이전 스냅샷"
        return response, {"state": "completed", "message": "DB 수정 및 저장 완료", "completed": 1, "total": 1}
    if kind not in {"collect", "scan", "discover", "verify"}:
        raise ValueError("Unsupported job kind")
    manager.start(kind, payload)
    try:
        while manager.busy():
            with manager.lock:
                progress = copy.deepcopy(manager.job)
            feedback = bridge.call("progress", progress=progress)
            if feedback["stopRequested"]:
                manager.stop.set()
            manager.worker.join(timeout=10)
    except Exception:
        manager.stop.set()
        manager.worker.join(timeout=90)
        raise
    if manager.job["state"] == "failed":
        raise RuntimeError(manager.job["message"])
    return {}, copy.deepcopy(manager.job)


def run(bridge):
    from booking_manager import Manager
    claim = bridge.call("claim")
    if claim.get("terminal"):
        print("Job already finished; no collection repeated.")
        return
    bridge.lease = claim["lease"]
    try:
        with tempfile.TemporaryDirectory(prefix="camping-job-") as temp:
            unpack(base64.b64decode(claim["bundle"], validate=True), temp, claim["sha256"])
            manager = Manager(temp)
            response, progress = execute(bridge, manager, claim["job"])
            bridge.call("progress", progress={**progress, "message": "Google Drive에 결과 저장 중"})
            bundle = pack(manager)
            result = bridge.call("commit", bundle=base64.b64encode(bundle).decode(), sha256=digest(bundle), response=response, progress=progress)
            if not result.get("saved"):
                raise RuntimeError("Drive did not confirm the saved snapshot")
            print(f"Drive snapshot saved. Outcome: {progress['state']}; processed {progress.get('completed', 0)}/{progress.get('total', 0)}.")
    except Exception as exc:
        try:
            bridge.call("fail", message=str(exc)[:600])
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        run(Bridge(os.environ["BRIDGE_URL"], os.environ["BRIDGE_SECRET"], os.environ["JOB_ID"],
            os.environ.get("GITHUB_RUN_ID", "local") + ":" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")))
    except Exception:
        # Avoid dumping URLs, request envelopes or local workbook content into Actions logs.
        print("Cloud job failed. Open the private web app job log for details.", file=sys.stderr)
        sys.exit(1)
