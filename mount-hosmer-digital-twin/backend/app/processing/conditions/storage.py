"""Atomic, content-addressed storage for validated Condition Packs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from avycore.conditions import ConditionPack, canonical_condition_pack_bytes


CONDITION_PACK_FILENAME = "condition-pack.json"
CHECKSUMS_FILENAME = "checksums.json"


class ConditionPackStorageError(RuntimeError):
    """Raised when a stored pack is incomplete, corrupt, or unsafe to replace."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_pack_file(path: Path) -> tuple[ConditionPack, bytes]:
    try:
        content = path.read_bytes()
        raw = json.loads(content.decode("utf-8"))
        return ConditionPack.model_validate(raw), content
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ConditionPackStorageError(f"Invalid Condition Pack {path}: {exc}") from exc


def load_condition_pack(path: str | Path) -> ConditionPack:
    """Load a pack file or verify a complete atomically-written pack directory."""

    requested = Path(path).resolve()
    if requested.is_file():
        return _read_pack_file(requested)[0]
    if not requested.is_dir():
        raise ConditionPackStorageError(f"Condition Pack path does not exist: {requested}")

    pack_path = requested / CONDITION_PACK_FILENAME
    checksums_path = requested / CHECKSUMS_FILENAME
    if not pack_path.is_file() or not checksums_path.is_file():
        raise ConditionPackStorageError(
            f"Condition Pack directory is incomplete: {requested}"
        )
    pack, pack_bytes = _read_pack_file(pack_path)
    try:
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConditionPackStorageError(f"Invalid checksum manifest: {exc}") from exc
    expected = {
        "schema": "condition-pack-storage-v1",
        "condition_id": pack.condition_id,
        "normalized_output_sha256": pack.normalized_output_sha256,
        "files": {
            CONDITION_PACK_FILENAME: {
                "bytes": len(pack_bytes),
                "sha256": _sha256_bytes(pack_bytes),
            }
        },
    }
    if checksums != expected:
        raise ConditionPackStorageError(
            "Condition Pack checksum manifest does not match its immutable content."
        )
    if requested.name != pack.condition_id:
        raise ConditionPackStorageError(
            "Condition Pack directory name does not match condition_id."
        )
    return pack


def _promote_condition_directory(staging: Path, target: Path) -> None:
    """Expose a complete new identity with one same-volume atomic rename."""

    staging.rename(target)


def write_condition_pack(pack: ConditionPack, runtime_root: str | Path) -> Path:
    """Atomically write one immutable pack below runtime/baked/conditions/.

    Existing valid content with the same identity is returned unchanged. Existing
    corrupt or conflicting content is never overwritten.
    """

    validated = ConditionPack.model_validate(pack.model_dump(mode="json"))
    runtime = Path(runtime_root).resolve()
    conditions_root = runtime / "baked" / "conditions"
    conditions_root.mkdir(parents=True, exist_ok=True)
    target = conditions_root / validated.condition_id
    if target.exists():
        existing = load_condition_pack(target)
        if existing != validated:
            raise ConditionPackStorageError(
                f"Condition identity collision at {target}; existing content differs."
            )
        return target

    staging = conditions_root / f".condition-build-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        pack_bytes = canonical_condition_pack_bytes(validated)
        _write_fsynced(staging / CONDITION_PACK_FILENAME, pack_bytes)
        checksums = {
            "schema": "condition-pack-storage-v1",
            "condition_id": validated.condition_id,
            "normalized_output_sha256": validated.normalized_output_sha256,
            "files": {
                CONDITION_PACK_FILENAME: {
                    "bytes": len(pack_bytes),
                    "sha256": _sha256_bytes(pack_bytes),
                }
            },
        }
        _write_fsynced(staging / CHECKSUMS_FILENAME, _canonical_json_bytes(checksums))
        # Validate the complete staging payload before it can become visible.
        staged_pack, staged_bytes = _read_pack_file(staging / CONDITION_PACK_FILENAME)
        if staged_pack != validated or _sha256_bytes(staged_bytes) != checksums["files"][
            CONDITION_PACK_FILENAME
        ]["sha256"]:
            raise ConditionPackStorageError("Staged Condition Pack failed verification.")
        if target.exists():
            # A concurrent identical writer won the race. Verify it and discard
            # this unpromoted staging directory.
            existing = load_condition_pack(target)
            if existing != validated:
                raise ConditionPackStorageError(
                    f"Condition identity collision at {target}; existing content differs."
                )
            return target
        try:
            _promote_condition_directory(staging, target)
        except OSError:
            if not target.exists():
                raise
            # Close the remaining race between the existence check and rename.
            # A concurrently published pack is acceptable only when fully valid
            # and content-identical.
            existing = load_condition_pack(target)
            if existing != validated:
                raise ConditionPackStorageError(
                    f"Condition identity collision at {target}; existing content differs."
                )
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
