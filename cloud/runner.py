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
        for attempt in range(3):
            body = json.dumps(envelope(self.secret, payload)).encode()
            try:
                with urlopen(Request(self.url, data=body, headers={"Content-Type": "application/json"}), timeout=100) as response:
                    value = json.load(response)
                if not value.get("ok"):
                    raise RuntimeError(value.get("error", "Bridge request rejected"))
                return value["value"]
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
                if attempt == 2:
                    raise RuntimeError("Drive bridge unavailable; check the job status before requesting another run") from None
                time.sleep(2 ** attempt)


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
