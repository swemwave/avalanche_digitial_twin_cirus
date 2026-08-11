"""Atomic storage and strict replay validation for SnowStatePacks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from avycore.snow import SnowStatePack, canonical_snow_state_pack_bytes


PACK_FILENAME = "snow-state-pack.json"
CHECKSUMS_FILENAME = "checksums.json"
STORAGE_SCHEMA = "snow-state-pack-storage-v2"


class SnowStateStorageError(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_snow_state_pack(path: str | Path) -> SnowStatePack:
    requested = Path(path).resolve()
    if requested.is_file():
        try:
            return SnowStatePack.model_validate_json(requested.read_bytes())
        except Exception as exc:
            raise SnowStateStorageError(f"Invalid SnowStatePack: {exc}") from exc
    pack_path, checksums_path = requested / PACK_FILENAME, requested / CHECKSUMS_FILENAME
    try:
        pack_bytes = pack_path.read_bytes()
        pack = SnowStatePack.model_validate_json(pack_bytes)
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnowStateStorageError(f"Invalid SnowStatePack storage: {exc}") from exc
    expected = {
        "schema": STORAGE_SCHEMA,
        "snow_state_id": pack.snow_state_id,
        "files": {PACK_FILENAME: {"bytes": len(pack_bytes), "sha256": _sha(pack_bytes)}},
    }
    if checksums != expected or requested.name != pack.snow_state_id:
        raise SnowStateStorageError("SnowStatePack checksum or directory identity conflicts.")
    return pack


def write_snow_state_pack(pack: SnowStatePack, runtime_root: str | Path) -> Path:
    validated = SnowStatePack.model_validate(pack.model_dump(mode="json"))
    root = Path(runtime_root).resolve() / "snow-state-packs"
    root.mkdir(parents=True, exist_ok=True)
    target = root / validated.snow_state_id
    if target.exists():
        if load_snow_state_pack(target) != validated:
            raise SnowStateStorageError("SnowStatePack identity collision.")
        return target
    staging = root / f".snow-state-build-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        pack_bytes = canonical_snow_state_pack_bytes(validated)
        pack_path = staging / PACK_FILENAME
        with pack_path.open("xb") as stream:
            stream.write(pack_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        checksums = {
            "schema": STORAGE_SCHEMA,
            "snow_state_id": validated.snow_state_id,
            "files": {PACK_FILENAME: {"bytes": len(pack_bytes), "sha256": _sha(pack_bytes)}},
        }
        (staging / CHECKSUMS_FILENAME).write_bytes(_canonical(checksums))
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target
