"""Versioned, bounded snapshots. Never extract arbitrary ZIP paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

MAX_ZIP = 8_000_000
MAX_EXPANDED = 24_000_000
FILES = {
    "db.xlsx": "객실_DB.xlsx",
    "result.xlsx": "예약현황_Result.xlsx",
    "catalog.json": "백업/통합관리/catalog.json",
    "workspace.json": "백업/통합관리/workspace.json",
    "state.json": "state.json",
    "history.json": "history.json",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unpack(data, folder, expected_hash=None):
    if len(data) > MAX_ZIP or (expected_hash and digest(data) != expected_hash):
        raise ValueError("Snapshot size or checksum mismatch")
    with ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(FILES):
            raise ValueError("Unexpected snapshot file list")
        if sum(f.file_size for f in archive.infolist()) > MAX_EXPANDED:
            raise ValueError("Expanded snapshot too large")
        contents = {name: archive.read(name) for name in names}
    state = json.loads(contents["state.json"])
    if digest(contents["db.xlsx"]) != state.get("revision"):
        raise ValueError("DB revision mismatch")
    for name, path in FILES.items():
        target = Path(folder) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[name])
    return state


def pack(manager):
    manager.persist()
    manager.store.save_meta()
    state = manager.state()
    state.update(cloud=True, folder="Google Drive", busy=False)
    # Absolute PC paths are not useful in a cloud snapshot.
    state.pop("downloads", None)
    state["job"] = dict(state["job"])
    state["job"].pop("csv", None)
    history = {entry["key"]: manager.store.history(entry["key"]) for entry in state["entries"]}
    values = {
        "db.xlsx": manager.store.db.read_bytes(),
        "result.xlsx": manager.store.result.read_bytes(),
        "catalog.json": manager.store.meta_path.read_bytes(),
        "workspace.json": manager.state_path.read_bytes(),
        "state.json": json.dumps(state, ensure_ascii=False).encode("utf-8"),
        "history.json": json.dumps(history, ensure_ascii=False).encode("utf-8"),
    }
    if sum(map(len, values.values())) > MAX_EXPANDED:
        raise ValueError("Snapshot exceeded Apps Script storage transfer limit")
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in values.items():
            archive.writestr(name, data)
    data = output.getvalue()
    if len(data) > MAX_ZIP:
        raise ValueError("Snapshot exceeds transfer limit; previous Drive data preserved")
    return data
