from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "acquire_regobs_blinded_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("acquire_regobs_blinded_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_source_geometry_preserves_provider_coordinates_and_claim_boundary() -> None:
    record = {
        "AvalancheObs": {
            "StartLong": 7.0,
            "StartLat": 61.0,
            "StopLong": 7.1,
            "StopLat": 60.9,
            "StartExtent": [[7.0, 61.0], [7.01, 61.0]],
            "StopExtent": [[7.1, 60.9], [7.11, 60.9]],
        }
    }
    geometry = MODULE.source_geometry(record)
    assert geometry["provider_start_point"] == [7.0, 61.0]
    assert geometry["provider_stop_point"] == [7.1, 60.9]
    assert geometry["provider_start_extent_ring"] is record["AvalancheObs"]["StartExtent"]
    assert "not yet a trusted release polygon" in geometry["provider_semantics"]["start_extent"]
    assert "not automatically a dense-flow" in geometry["provider_semantics"]["stop_extent"]
    assert geometry["geometry_modified_to_fit_any_evaluated_result"] is False


def test_attachment_urls_are_restricted_to_public_regobs_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unexpected RegObs attachment URL"):
        MODULE._download("https://example.com/image.jpg", tmp_path / "x.jpg", offline=True)


def test_immutable_evidence_never_overwrites_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    MODULE._write_immutable(path, b"one")
    MODULE._write_immutable(path, b"one")
    with pytest.raises(ValueError, match="Immutable RegObs evidence conflict"):
        MODULE._write_immutable(path, b"two")
