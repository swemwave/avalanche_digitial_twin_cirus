"""Read-only inventory and immutable preservation of an active baked surface."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.bake_identity import sha256_file, sha256_json


PRESERVATION_SCHEMA = "baked-surface-preservation-v1"
PRESERVATION_STORAGE_SCHEMA = "baked-surface-preservation-storage-v1"
INVENTORY_FILENAME = "inventory.json"
CHECKSUMS_FILENAME = "checksums.json"


class BakePreservationError(RuntimeError):
    """Raised when a bake cannot be inventoried or preserved exactly."""


def inventory_directory(root: str | Path) -> dict[str, Any]:
    source = Path(root).resolve()
    if source.is_symlink() or not source.is_dir():
        raise BakePreservationError(f"Bake inventory root is not a regular directory: {source}")
    files: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise BakePreservationError(f"Bake inventory contains a symbolic link: {path}")
        if path.is_file():
            files.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    meta_path = source / "meta.json"
    bake_sha256 = None
    if meta_path.is_file():
        try:
            bake_sha256 = json.loads(meta_path.read_text(encoding="utf-8")).get(
                "identity", {}
            ).get("bake_sha256")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BakePreservationError("Active bake meta.json is unreadable.") from exc
    inventory_sha256 = sha256_json(files)
    return {
        "schema_version": PRESERVATION_SCHEMA,
        "source_role": "complete_runtime_baked_surface_before_controlled_rebuild",
        "source_bake_sha256": bake_sha256,
        "file_count": len(files),
        "total_bytes": sum(record["bytes"] for record in files),
        "inventory_sha256": inventory_sha256,
        "files": files,
        "preservation_id": f"preserved-bake-{inventory_sha256}",
    }


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def validate_preservation(path: str | Path) -> dict[str, Any]:
    root = Path(path).resolve()
    inventory_path = root / INVENTORY_FILENAME
    checksums_path = root / CHECKSUMS_FILENAME
    copied = root / "baked"
    if not inventory_path.is_file() or not checksums_path.is_file() or not copied.is_dir():
        raise BakePreservationError("Preserved bake is incomplete.")
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BakePreservationError("Preserved bake metadata is unreadable.") from exc
    expected_inventory_keys = {
        "schema_version",
        "source_role",
        "source_bake_sha256",
        "file_count",
        "total_bytes",
        "inventory_sha256",
        "files",
        "preservation_id",
    }
    if set(inventory) != expected_inventory_keys or inventory.get("schema_version") != PRESERVATION_SCHEMA:
        raise BakePreservationError("Preserved bake inventory is not strict.")
    if set(checksums) != {"schema_version", "files"} or checksums.get("schema_version") != PRESERVATION_STORAGE_SCHEMA:
        raise BakePreservationError("Preserved bake checksum manifest is not strict.")
    if set(checksums.get("files", {})) != {INVENTORY_FILENAME}:
        raise BakePreservationError("Preserved bake checksum file set is not exact.")
    if checksums["files"][INVENTORY_FILENAME] != sha256_file(inventory_path):
        raise BakePreservationError("Preserved bake inventory failed its SHA-256 check.")
    actual = inventory_directory(copied)
    if actual != inventory:
        raise BakePreservationError("Preserved baked-surface bytes differ from the inventory.")
    if root.name != inventory["preservation_id"]:
        raise BakePreservationError("Preserved bake directory conflicts with its identity.")
    return inventory


def preserve_bake(
    source_bake: str | Path,
    runtime_root: str | Path,
) -> Path:
    source = Path(source_bake).resolve()
    runtime = Path(runtime_root).resolve()
    initial = inventory_directory(source)
    required = initial["total_bytes"] * 2 + 64 * 1024 * 1024
    free = shutil.disk_usage(runtime).free
    if free < required:
        raise BakePreservationError(
            f"Insufficient free space to preserve and verify the bake: {free} < {required}."
        )
    parent = runtime / "verification" / "bake-preservation"
    target = parent / initial["preservation_id"]
    if target.exists():
        validate_preservation(target)
        return target
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{initial['preservation_id']}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        shutil.copytree(source, staging / "baked", copy_function=shutil.copy2)
        copied_inventory = inventory_directory(staging / "baked")
        if copied_inventory != initial:
            raise BakePreservationError(
                "Active bake changed or a copied byte differed during preservation."
            )
        inventory_bytes = _canonical_bytes(initial)
        _write_fsynced(staging / INVENTORY_FILENAME, inventory_bytes)
        _write_fsynced(
            staging / CHECKSUMS_FILENAME,
            _canonical_bytes(
                {
                    "schema_version": PRESERVATION_STORAGE_SCHEMA,
                    "files": {INVENTORY_FILENAME: sha256_file(staging / INVENTORY_FILENAME)},
                }
            ),
        )
        os.replace(staging, target)
    except Exception:
        if staging.exists() and staging.parent == parent:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    validate_preservation(target)
    return target
