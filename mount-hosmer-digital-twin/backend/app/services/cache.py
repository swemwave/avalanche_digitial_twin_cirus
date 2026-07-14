from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.settings import Settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def stable_json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path, root: Path | None = None) -> str:
    resolved = path.resolve()
    if root is not None:
        try:
            return resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            pass
    return str(resolved)


def file_record(path: Path, root: Path | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    record: dict[str, Any] = {
        "path": display_path(resolved, root),
        "exists": resolved.exists(),
    }
    if not resolved.exists():
        record["checksum_status"] = "missing"
        return record
    stat = resolved.stat()
    record.update(
        {
            "size_bytes": int(stat.st_size),
            "modified_time_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }
    )
    record["sha256"] = file_sha256(resolved)
    record["checksum_status"] = "sha256"
    return record


def source_fingerprint(
    settings: Settings,
    source_files: list[Path],
    *,
    config_files: list[Path] | None = None,
    parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sources = [
        file_record(path, root=settings.data_root)
        for path in sorted({path.resolve() for path in source_files}, key=lambda item: str(item).lower())
    ]
    configs = [
        file_record(path, root=settings.project_root)
        for path in sorted({path.resolve() for path in config_files or []}, key=lambda item: str(item).lower())
    ]
    payload = {
        "application_version": settings.app_version,
        "sources": sources,
        "configuration_files": configs,
        "parameters": parameters or {},
    }
    return {
        "generated_at_utc": utc_now_iso(),
        **payload,
        "input_signature_sha256": stable_json_hash(payload),
    }


def cache_matches(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
    return bool(
        existing.get("input_signature_sha256")
        and existing.get("input_signature_sha256") == expected.get("input_signature_sha256")
    )


def write_cache_log(settings: Settings, name: str, payload: dict[str, Any]) -> Path:
    cache_dir = settings.runtime_root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
