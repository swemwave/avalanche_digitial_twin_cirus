from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from avycore.validation import TRUSTED_DATASET_IDENTITIES_SHA256


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "validation-data/experiments/spot-blind-swiss-v1.json"
RESULT_PATH = ROOT / "validation-data/results/spot-blind-swiss-v1-holdout.json"
DEVELOPMENT_RESULT_PATH = (
    ROOT / "validation-data/results/spot-blind-swiss-v1-development.json"
)
PREDICTION_DIR = ROOT / "validation-data/predictions/spot-blind-swiss-v1"


def _load(path: Path) -> dict:
    return json.loads(path.read_bytes())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [
            nested for item in value.values() for nested in _keys(item)
        ]
    if isinstance(value, list):
        return [nested for item in value for nested in _keys(item)]
    return []


def test_spot_hindcast_is_bound_to_frozen_sources_model_and_predictions() -> None:
    spec = _load(SPEC_PATH)
    result = _load(RESULT_PATH)

    assert spec["frozen_before_holdout_predictions"] is True
    assert spec["leakage_controls"]["parameter_tuning_permitted"] is False
    assert result["experiment_spec_sha256"] == _sha256(SPEC_PATH)
    assert result["model_parameters_tuned_in_this_experiment"] is False
    assert result["parameters_changed_after_viewing_holdout_results"] is False
    assert result["held_out_outlines_used_as_model_inputs"] is False
    assert result["held_out_outlines_used_as_release_seeds"] is False

    identity = spec["model_identity"]
    for source_key, path_key in (
        ("risk_source_sha256", "risk_source_path"),
        ("runout_source_sha256", "runout_source_path"),
        ("runner_source_sha256", "runner_source_path"),
    ):
        assert identity[source_key] == _sha256(ROOT / identity[path_key])

    inputs = spec["source_inputs"]
    assert len({item["id"] for item in inputs}) == len(inputs)
    assert all(len(item["sha256"]) == 64 for item in inputs)
    assert all(item["sha256"] == item["sha256"].lower() for item in inputs)

    spec_sha256 = _sha256(SPEC_PATH)
    result_blocks = {item["block_id"]: item for item in result["block_results"]}
    assert set(result_blocks) == {
        "holdout_gotthard",
        "holdout_glarus",
        "holdout_albula",
        "holdout_silvretta",
    }
    for block_id, block in result_blocks.items():
        prediction = PREDICTION_DIR / f"{block_id}.npz"
        assert block["prediction_artifact_sha256"] == _sha256(prediction)
        with np.load(prediction, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata_json"].item()))
            assert metadata["experiment_spec_sha256"] == spec_sha256
            assert metadata["held_out_outlines_opened"] is False
            assert metadata["held_out_outlines_used_as_model_inputs"] is False
            assert metadata["prediction_identity_sha256"] == block[
                "prediction_identity_sha256"
            ]


def test_spot_hindcast_preserves_the_failed_acceptance_result() -> None:
    result = _load(RESULT_PATH)
    aggregate = result["aggregate"]

    assert result["acceptance_passed"] is False
    assert aggregate["acceptance_passed"] is False
    assert aggregate["all_predeclared_groups_qualified"] is True
    assert aggregate["qualifying_group_count"] == 4
    assert aggregate["mapped_event_count"] == 1025
    assert aggregate["captured_event_count"] == 22
    assert aggregate["event_capture_fraction"] == pytest.approx(22 / 1025)
    assert aggregate["mapped_positive_footprint_coverage_fraction"] == pytest.approx(
        591 / 92402
    )
    assert aggregate["flagged_eligible_terrain_fraction"] == pytest.approx(
        5463 / 1795600
    )

    expected = {
        "holdout_gotthard": (241, 0, 0),
        "holdout_glarus": (433, 22, 40),
        "holdout_albula": (182, 0, 0),
        "holdout_silvretta": (169, 0, 0),
    }
    for block in result["block_results"]:
        event_count, captured_count, release_zones = expected[block["block_id"]]
        primary = block["metrics"]["hybrid_end_to_end"]
        assert primary["event_count"] == event_count
        assert primary["captured_event_count"] == captured_count
        assert block["prediction_summary"]["release"]["zone_count"] == release_zones
        assert block["acceptance_passed"] is False
        assert block["domain_escape"] is False
        assert block["incomplete_inputs"] is False

    assert result["strict_field_validation"] == {
        "field_validation_holdout_n": 0,
        "is_validated": False,
        "reason": (
            "Positive-only satellite mapping lacks verified negatives and an exact "
            "machine-readable acquisition-footprint polygon; it is not eligible for the "
            "project's independently reviewed strict binary field-validation contract."
        ),
        "trusted_dataset_identity_registry_modified": False,
    }
    assert TRUSTED_DATASET_IDENTITIES_SHA256 == frozenset()


def test_spot_hindcast_never_emits_negative_domain_metrics() -> None:
    spec = _load(SPEC_PATH)
    result = _load(RESULT_PATH)
    prohibited = {
        "precision",
        "specificity",
        "false_positive_rate",
        "f1",
        "intersection_over_union",
        "probability_calibration",
        "accuracy",
    }

    assert not (prohibited & {key.lower() for key in _keys(result)})
    assert result["negative_evidence_used"] is False
    for block in result["block_results"]:
        for metric in block["metrics"].values():
            assert metric["negative_evidence_used"] is False
            assert metric["unmapped_cells_treated_as_negative"] is False
    assert set(spec["metrics"]["forbidden_metrics"]) == {
        "precision",
        "specificity",
        "false-positive rate",
        "F1",
        "IoU",
        "probability calibration",
        "generic accuracy",
    }


def test_development_result_precedes_and_also_fails() -> None:
    result = _load(DEVELOPMENT_RESULT_PATH)
    block = result["block_results"][0]
    primary = block["metrics"]["hybrid_end_to_end"]

    assert result["partition"] == "development"
    assert result["acceptance_passed"] is False
    assert primary["event_count"] == 282
    assert primary["captured_event_count"] == 15
    assert block["domain_escape"] is False
    assert block["incomplete_inputs"] is False
