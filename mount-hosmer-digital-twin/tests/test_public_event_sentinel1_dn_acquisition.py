from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "acquire_public_event_sentinel1_dn.py"
)
SPEC = importlib.util.spec_from_file_location(
    "acquire_public_event_sentinel1_dn", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_polarizations_exclude_non_measurement_assets() -> None:
    scene = {
        "assets": [
            {"asset_name": "schema-noise-vv"},
            {"asset_name": "vv"},
            {"asset_name": "vh"},
            {"asset_name": "safe-manifest"},
        ]
    }
    assert [item["asset_name"] for item in MODULE._polarizations(scene)] == [
        "vh",
        "vv",
    ]


def test_build_nearest_dn_preserves_order_and_counts_empty_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisition = {
        "schema": "avycore-public-event-imagery-acquisition-v2",
        "predictions_generated": False,
        "candidates": [{"candidate_id": "b"}, {"candidate_id": "a"}],
    }
    acquisition_path = tmp_path / "acquisition.json"
    acquisition_path.write_text(json.dumps(acquisition), encoding="utf-8")

    def fake_worker(payload: tuple[dict, str, bool]) -> dict:
        candidate = payload[0]
        valid = 0 if candidate["candidate_id"] == "b" else 4
        return {
            "candidate_id": candidate["candidate_id"],
            "scenes": [
                {
                    "assets": [
                        {
                            "pre_calibration_resampling": "nearest",
                            "raster": {"valid_pixel_count_all_bands": valid},
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(MODULE, "_candidate_worker", fake_worker)
    artifact = MODULE.build_nearest_dn(
        acquisition_path, tmp_path / "cache", offline=True, workers=1
    )
    assert [candidate["candidate_id"] for candidate in artifact["candidates"]] == [
        "b",
        "a",
    ]
    assert artifact["resampling"] == "nearest"
    assert artifact["counts"] == {
        "candidates": 2,
        "polarization_assets": 2,
        "empty_assets": 1,
        "valid_pixels": 4,
    }
