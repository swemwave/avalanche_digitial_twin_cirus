from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "audit_public_validation_sources.py"
)
SPEC = importlib.util.spec_from_file_location("audit_public_validation_sources", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_source_audit_is_restricted_to_reviewed_primary_api() -> None:
    MODULE._validate_source_url("https://zenodo.org/api/records/15863589")
    with pytest.raises(ValueError, match="non-reviewed source URL"):
        MODULE._validate_source_url("https://example.com/api/records/15863589")
    with pytest.raises(ValueError, match="non-record Zenodo API path"):
        MODULE._validate_source_url("https://zenodo.org/search?q=avalanche")


def test_every_source_retains_explicit_contract_blockers() -> None:
    assert len(MODULE.SOURCES) >= 7
    for source in MODULE.SOURCES:
        assert source["blocking_contract_fields"]
        assert source["assessment"]
        assert source["landing_page"].startswith("https://")


def test_immutable_source_cache_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "response.json"
    MODULE._write_immutable(path, b"first")
    MODULE._write_immutable(path, b"first")
    with pytest.raises(ValueError, match="Immutable public-source cache conflict"):
        MODULE._write_immutable(path, b"second")
