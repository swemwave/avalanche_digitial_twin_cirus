from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from avycore.validation import TRUSTED_DATASET_IDENTITIES_SHA256


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "validation-data/experiments/regime-hindcast-v1.json"
SELECTION = ROOT / "validation-data/experiments/regime-hindcast-v1-holdout-blocks.json"
DEVELOPMENT = ROOT / "validation-data/results/regime-hindcast-v1-development.json"
HOLDOUT = ROOT / "validation-data/results/regime-hindcast-v1-holdout.json"
SUMMARY = ROOT / "validation-data/results/regime-hindcast-v1-summary.json"
PREDICTIONS = ROOT / "validation-data/predictions/regime-hindcast-v1"


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


def test_holdout_was_selected_and_predicted_without_avalanche_outlines() -> None:
    spec = _load(SPEC)
    selection = _load(SELECTION)
    result = _load(HOLDOUT)

    assert spec["status"] == "frozen_before_holdout_prediction"
    assert selection["selection_used_avalanche_outlines"] is False
    assert spec["leakage_controls"]["holdout_block_selection_used_avalanche_outlines"] is False
    assert spec["leakage_controls"]["prediction_command_resolves_evaluation_targets"] is False
    assert result["prediction_generated_before_target_scoring"] is True
    assert result["held_out_outlines_used_as_model_inputs"] is False
    assert result["parameters_changed_after_viewing_holdout_results"] is False

    spec_hash = _sha256(SPEC)
    result_blocks = {item["block_id"]: item for item in result["block_results"]}
    assert set(result_blocks) == {
        "holdout_1999_c04r04",
        "holdout_1999_c09r05",
        "holdout_1999_c05r04",
        "holdout_1999_c03r01",
        "holdout_1999_c04r03",
    }
    for block_id, block in result_blocks.items():
        path = PREDICTIONS / f"{block_id}.npz"
        assert block["prediction_artifact_sha256"] == _sha256(path)
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata_json"].item()))
            assert metadata["experiment_spec_sha256"] == spec_hash
            assert metadata["held_out_outlines_opened"] is False
            assert metadata["held_out_outlines_used_as_model_inputs"] is False


def test_frozen_model_sources_and_every_reported_source_are_identified() -> None:
    spec = _load(SPEC)
    summary = _load(SUMMARY)
    identity = spec["model_identity"]
    for relative, expected in identity["source_sha256"].items():
        assert _sha256(ROOT / relative) == expected

    required = {
        "id",
        "role",
        "path",
        "bytes",
        "sha256",
        "licence",
        "version",
        "crs",
        "unit",
        "transformation",
        "missing_value_rule",
    }
    lineage = summary["source_lineage"]
    assert len(lineage) == 65
    assert len({item["id"] for item in lineage}) == len(lineage)
    for item in lineage:
        assert required <= set(item)
        assert item["bytes"] > 0
        assert len(item["sha256"]) == 64
        assert all(item[key] not in (None, "") for key in required)


def test_regime_hindcast_preserves_the_failed_holdout_result() -> None:
    result = _load(HOLDOUT)
    summary = _load(SUMMARY)
    aggregate = result["aggregate"]

    assert result["acceptance_passed"] is False
    assert aggregate["acceptance_passed"] is False
    assert aggregate["all_predeclared_groups_qualified"] is True
    assert aggregate["qualifying_group_count"] == 5
    assert aggregate["mapped_event_count"] == 1798
    assert aggregate["captured_event_count"] == 446
    assert aggregate["event_capture_fraction"] == pytest.approx(446 / 1798)
    assert aggregate["mapped_positive_footprint_coverage_fraction"] == pytest.approx(
        23548 / 267981
    )
    assert aggregate["flagged_eligible_terrain_fraction"] == pytest.approx(
        97657 / 1906165
    )
    assert summary["holdout_event_support"]["incomplete_event_count"] == 35
    assert summary["domain_escape"] == {
        "block_count": 0,
        "particles_left_the_aoi": 0,
    }

    expected = {
        "release_only": 440,
        "routed_nonrelease_only": 37,
        "hybrid_end_to_end": 446,
        "alpha_only_end_to_end": 501,
        "dynamics_only_end_to_end": 446,
        "release_dry_slab": 192,
        "release_wet_snow": 140,
        "release_dry_loose": 240,
        "release_full_depth_glide": 0,
    }
    for name, captured in expected.items():
        item = summary["holdout_aggregate_by_ablation"][name]
        assert item["event_count"] == 1798
        assert item["captured_event_count"] == captured

    for block in result["block_results"]:
        assert block["acceptance_passed"] is False
        assert block["domain_escape"] is False
        assert block["prediction_summary"]["terrain"]["core_complete_input_fraction"] == 1.0
        assert block["acceptance_checks"]["event_capture"] is False
        assert block["acceptance_checks"]["exceeds_slope_only"] is False
        assert block["acceptance_checks"]["exceeds_random_97_5_percentile"] is False


def test_mapped_regime_strata_and_status_are_not_overclaimed() -> None:
    result = _load(HOLDOUT)
    summary = _load(SUMMARY)
    strata = summary["holdout_mapped_regime_stratification"]
    assert strata == {
        "FULL_DEPTH": {
            "captured_event_count": 133,
            "event_capture_fraction": pytest.approx(133 / 660),
            "event_count": 660,
        },
        "LOOSE_SNOW": {
            "captured_event_count": 16,
            "event_capture_fraction": pytest.approx(16 / 74),
            "event_count": 74,
        },
        "SLAB": {
            "captured_event_count": 225,
            "event_capture_fraction": pytest.approx(225 / 787),
            "event_count": 787,
        },
        "UNKNOWN": {
            "captured_event_count": 72,
            "event_capture_fraction": pytest.approx(72 / 277),
            "event_count": 277,
        },
    }
    assert result["strict_field_validation"]["field_validation_holdout_n"] == 0
    assert result["strict_field_validation"]["is_validated"] is False
    assert result["strict_field_validation"]["trusted_dataset_identity_registry_modified"] is False
    assert summary["emails_sent"] is False
    assert summary["parameters_changed_after_holdout_view"] is False
    assert TRUSTED_DATASET_IDENTITIES_SHA256 == frozenset()

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


def test_development_split_is_distinct_and_also_failed() -> None:
    development = _load(DEVELOPMENT)
    spec = _load(SPEC)
    assert development["partition"] == "development"
    assert development["acceptance_passed"] is False
    assert {block["campaign_year"] for block in development["block_results"]} == {
        2018,
        2019,
    }
    development_bounds = {
        tuple(block["core_grid"]["bounds"])
        for block in spec["partitions"]["development"]["blocks"]
    }
    holdout_bounds = {
        tuple(block["core_grid"]["bounds"])
        for block in spec["partitions"]["holdout"]["blocks"]
    }
    assert development_bounds.isdisjoint(holdout_bounds)
