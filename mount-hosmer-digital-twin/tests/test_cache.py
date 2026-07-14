from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from app.core.settings import Settings
from app.services.cache import cache_matches, source_fingerprint, write_cache_log


def test_source_fingerprint_is_stable_and_detects_input_change(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    data_root.mkdir()
    project_root.mkdir()
    source = data_root / "source.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    settings = Settings(
        project_root=project_root,
        backend_root=project_root / "backend",
        runtime_root=runtime_root,
        data_root=data_root,
    )

    first = source_fingerprint(settings, [source], parameters={"processor": "test"})
    second = source_fingerprint(settings, [source], parameters={"processor": "test"})

    assert first["input_signature_sha256"] == second["input_signature_sha256"]
    assert cache_matches(first, second)

    source.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    third = source_fingerprint(settings, [source], parameters={"processor": "test"})

    assert third["input_signature_sha256"] != first["input_signature_sha256"]
    assert not cache_matches(first, third)


def test_source_fingerprint_uses_sha256_not_size_or_mtime_only(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    data_root.mkdir()
    project_root.mkdir()
    source = data_root / "same-size.bin"
    source.write_bytes(b"abcdef")
    stat = source.stat()
    settings = Settings(
        project_root=project_root,
        backend_root=project_root / "backend",
        runtime_root=runtime_root,
        data_root=data_root,
    )

    first = source_fingerprint(settings, [source], parameters={"processor": "test"})
    source.write_bytes(b"abcdeg")
    os.utime(source, (stat.st_atime, stat.st_mtime))
    second = source_fingerprint(settings, [source], parameters={"processor": "test"})

    assert first["sources"][0]["size_bytes"] == second["sources"][0]["size_bytes"]
    assert first["sources"][0]["modified_time_utc"] == second["sources"][0]["modified_time_utc"]
    assert first["sources"][0]["sha256"] != second["sources"][0]["sha256"]
    assert first["input_signature_sha256"] != second["input_signature_sha256"]


def test_cache_log_records_reproducible_metadata(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    data_root.mkdir()
    project_root.mkdir()
    source = data_root / "largeish.bin"
    source.write_bytes(b"abcdef")
    settings = Settings(
        project_root=project_root,
        backend_root=project_root / "backend",
        runtime_root=runtime_root,
        data_root=data_root,
    )

    payload = source_fingerprint(settings, [source], parameters={"processor": "test"})
    path = write_cache_log(settings, "test_cache", payload)
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["sources"][0]["path"] == "largeish.bin"
    assert saved["sources"][0]["checksum_status"] == "sha256"
    assert saved["sources"][0]["sha256"] == hashlib.sha256(b"abcdef").hexdigest()
    assert saved["input_signature_sha256"] == payload["input_signature_sha256"]
