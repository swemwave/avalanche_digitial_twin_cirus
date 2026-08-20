"""Offline contract checks for the committed real-event qualitative experiment.

These tests deliberately do not call the network or reinterpret the result as
field validation.  They verify that the frozen experiment, registered dataset
identities, public lower-rigor metric records, engine mode, split, and artifact
identity remain reproducible from the committed JSON files. They do not rerun
the engine because the reviewed DEM and raw Shapefile witnesses are external
artifacts; the runner fail-closes on those source identities during regeneration.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from avycore.validation import load_validation_dataset


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "validation-data/experiments/alpha-only-real-events-v1.json"
RESULT_PATH = ROOT / "validation-data/results/alpha-only-real-events-v1.json"
RELEASE_SIZES = ["small", "medium", "large", "very_large"]
BRAMA_MANIFEST = ROOT / "validation-data/braemabuehl-2019-qualitative/manifest.json"
SPOT_MANIFEST = ROOT / "validation-data/davos-spot-2019-qualitative/manifest.json"


def _load(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    return json.loads(raw), raw


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _all_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [
            nested for item in value.values() for nested in _all_keys(item)
        ]
    if isinstance(value, list):
        return [nested for item in value for nested in _all_keys(item)]
    return []


def test_qualitative_alpha_artifact_is_bound_to_frozen_spec_and_sources() -> None:
    spec, spec_bytes = _load(SPEC_PATH)
    result, result_bytes = _load(RESULT_PATH)

    assert spec["frozen_before_results"] is True
    assert spec["fixed_model"]["parameter_tuning_permitted"] is False
    assert result["experiment_spec_sha256"] == hashlib.sha256(spec_bytes).hexdigest()
    amendment = spec["post_freeze_metadata_amendments"][0]
    assert amendment == result["post_freeze_metadata_amendments"][0]
    assert amendment["original_frozen_spec_sha256"] == (
        "ec7f104cb68b9bdba064ba2e5806fe126cb342da1a0a5ee0ecac434eefff7f85"
    )
    assert amendment["result_informed"] is False
    assert result["original_pre_amendment_frozen_spec_sha256"] == amendment[
        "original_frozen_spec_sha256"
    ]
    assert result["parameters_tuned"] is False
    assert result["split_frozen_before_results"] is True
    assert result["release_depth_sensitivity"]["status"] == "unsupported"
    method = result["metric_method"]
    assert method["implementation"] == (
        "avycore.validation.metrics.positive_only_polygon_metrics"
    )
    assert method["prediction_context"] == "QualitativePredictionContext"
    assert method["public_metric_artifact_schema"] == (
        "avycore-positive-only-polygon-prediction-artifact-v2"
    )
    assert "Separately derived" in method["predicted_to_mapped_area_ratio_scope"]
    assert "Separately derived" in method["release_overlap_diagnostic_scope"]
    assert "No complete historical scenario is invented" in method[
        "historical_scenario_handling"
    ]
    assert method["negative_evidence_used"] is False
    engine_source = ROOT / result["model_identity"]["engine_source_path"]
    assert result["model_identity"] == {
        "parameter_manifest_sha256": (
            "aaafd6f9fc6e8d598cb56c479bcc072374ee94ba17613c0ac2df0594f8059628"
        ),
        "engine_source_path": "packages/avycore/src/avycore/hazard/runout.py",
        "engine_source_sha256": hashlib.sha256(engine_source.read_bytes()).hexdigest(),
    }
    regeneration = result["regeneration_environment"]
    runner_source = ROOT / regeneration["runner_source_path"]
    assert regeneration["scope"] == (
        "Reproduction provenance only; library and runner versions are not "
        "scientific evidence or model-validation identity."
    )
    assert regeneration["runner_source_path"] == (
        "scripts/validation/run_qualitative_alpha_experiment.py"
    )
    assert regeneration["runner_source_sha256"] == hashlib.sha256(
        runner_source.read_bytes()
    ).hexdigest()
    assert set(regeneration["library_versions"]) == {
        "numpy",
        "rasterio",
        "geopandas",
        "shapely",
    }
    assert all(regeneration["library_versions"].values())
    assert b"\r" not in result_bytes

    datasets = {
        "braemabuehl": load_validation_dataset(BRAMA_MANIFEST),
        "davos_spot": load_validation_dataset(SPOT_MANIFEST),
    }
    for key, dataset in datasets.items():
        recorded = result["validation_datasets"][key]
        assert recorded["dataset_id"] == dataset.manifest.dataset_id
        assert recorded["dataset_identity_sha256"] == (
            dataset.dataset_identity_sha256
        )
        assert recorded["manifest_sha256"] == dataset.manifest_sha256
        assert recorded["observations_sha256"] == (
            dataset.manifest.observations_sha256
        )
        assert recorded["scientific_use"] == "qualitative_comparison"
        assert recorded["absence_semantics"] == (
            "unknown_unless_explicitly_observed"
        )
    assert result["terrain_error_characterization"]["brama"][
        "alpha_horizontal_scale_m_from_documented_new_snow"
    ] == {
        "small": 0.96,
        "medium": 1.18,
        "large": 1.41,
        "very_large": 1.74,
    }
    assert result["terrain_error_characterization"]["spot_copernicus_glo30"][
        "alpha_horizontal_scale_m_from_4m_relative_vertical_specification"
    ] == {
        "small": 6.4,
        "medium": 7.85,
        "large": 9.42,
        "very_large": 11.62,
    }

    assert result["sources"]["brama"]["dem_md5"] == (
        "680930cdd4af3410551909810a66ca54"
    )
    assert result["sources"]["spot"]["source_archive_sha256"] == (
        "5529482a35823f4b3f2d870df7da52e2c73af151f6eb9c12d3869353379368bb"
    )
    assert result["sources"]["spot_dem"]["sha256"] == (
        "6a7eccb6d198f01a1fdfcca0e1cef837ef294456fb7243ec4d0966e089b1e7fc"
    )


def test_qualitative_alpha_artifact_never_claims_field_validation_or_negatives() -> None:
    result, _ = _load(RESULT_PATH)

    assert result["schema"] == "avycore-qualitative-alpha-results-v2"
    assert result["scientific_use"] == "qualitative_comparison"
    assert result["component_tested"] == "empirical_alpha_angle_plus_routing"
    assert result["engine"] == "fast_routing_alpha"
    assert result["engine_mode"] == "alpha_only"
    assert result["flow_regime_assumption"] == "dry_slab_unverified"
    assert "Model assumption only" in result["flow_regime_assumption_scope"]
    assert result["is_field_validation"] is False
    assert result["is_validated"] is False
    assert result["counts"]["field_validation_holdout_n"] == 0
    assert not any("iou" in key.lower() for key in _all_keys(result))
    assert not any("mountain_area" == key for key in _all_keys(result))
    spec, _ = _load(SPEC_PATH)
    assert not any("mountain_area" == key for key in _all_keys(spec))
    assert "direct_boolean_mask_arithmetic" not in json.dumps(result)
    assert "not evidence of independent mountains" in result["analysis_group_scope"]

    for event in result["events"]:
        for run in event["runs"]:
            assert run["component_tested"] == "empirical_alpha_angle_plus_routing"
            assert run["engine_mode"] == "alpha_only"
            assert run["random_seed"] is None
            assert run["flow_regime_assumption"] == "dry_slab_unverified"
            assert run["metric_scope"] == "mapped_positive_coverage_only"
            assert run["supports_independent_validation_claim"] is False
            assert run["negative_evidence_used"] is False
            assert run["unmapped_cells_treated_as_negative"] is False


def test_qualitative_alpha_artifact_split_order_and_public_metrics_are_stable() -> None:
    spec, _ = _load(SPEC_PATH)
    result, _ = _load(RESULT_PATH)

    assert [event["event_id"] for event in result["events"]] == [
        event["event_id"] for event in spec["events"]
    ]
    assert result["partitions"] == spec["partitions"]
    assert result["counts"] == {
        "registered_event_count": 8,
        "analysis_group_count": 4,
        "planned_run_count": 32,
        "executed_run_count": 32,
        "scoreable_qualitative_run_count": 32,
        "unscoreable_run_count": 0,
        "field_validation_holdout_n": 0,
    }

    dataset_by_id = {
        item["dataset_id"]: item
        for item in result["validation_datasets"].values()
    }

    for event in result["events"]:
        assert event["analysis_group"]
        assert event["dataset_identity_sha256"] == dataset_by_id[
            event["dataset_id"]
        ]["dataset_identity_sha256"]
        assert len(event["evaluation_source_artifact_sha256"]) == 64
        assert set(event["input_mask_sha256"]) == {
            "valid",
            "release",
            "mapped_positive",
        }
        assert all(
            len(value) == 64 for value in event["input_mask_sha256"].values()
        )
        for key, value in event["input_lineage"].items():
            if not key.endswith("_geometry_files_sha256"):
                continue
            names = list(value)
            assert len(names) == len({name.casefold() for name in names})
            assert len(names) == 5
        assert event["input_lineage"][
            "geometry_used_by_model"
        ] == "committed_normalized_validation_observation"
        raw_match_fields = [
            value
            for key, value in event["input_lineage"].items()
            if key.startswith("raw_geometry_exact_match_to_committed_observation")
        ]
        assert raw_match_fields == [True]
        assert [run["release_size"] for run in event["runs"]] == RELEASE_SIZES
        for run in event["runs"]:
            assert run["status"] == "scoreable_qualitative"
            assert run["particles_left_the_aoi"] == 0
            assert run["aoi_boundary_contact"] is False
            assert run["analysis_group"] == event["analysis_group"]
            assert run["validation_partition"] == "qualitative"
            assert run["dataset_id"] == event["dataset_id"]
            assert run["dataset_identity_sha256"] == event[
                "dataset_identity_sha256"
            ]
            assert run["mapped_positive_observation_id"] == event[
                "mapped_positive_observation_id"
            ]
            assert run["mapped_positive_geometry_source"] == (
                "committed_normalized_validation_observation"
            )
            assert len(run["predicted_mask_sha256"]) == 64
            assert len(run["run_configuration_sha256"]) == 64
            assert run["mapped_positive_coverage_includes_release_cells"] is (
                run["intersecting_mapped_positive_release_cell_count"] > 0
            )
            assert run["intersecting_mapped_positive_cell_count"] == (
                run["intersecting_mapped_positive_release_cell_count"]
                + run["intersecting_mapped_positive_nonrelease_cell_count"]
            )
            metric = run["positive_only_metric"]
            assert metric is not None
            assert metric["prediction_context_kind"] == (
                "qualitative_missingness_aware"
            )
            assert metric["evidence_use"] == "qualitative_comparison"
            assert metric["historical_scenario_complete"] is False
            assert metric["scenario_documentation_by_observation"]
            assert metric["aoi_coverage_status"] == "complete"
            assert metric["aoi_boundary_contact"] is False
            assert metric["particles_left_the_aoi"] == 0
            assert metric["component_tested"] == (
                "empirical_alpha_angle_plus_routing"
            )
            assert metric["engine"] == "fast_routing_alpha"
            assert metric["engine_mode"] == "alpha_only"
            assert metric["partition"] == "qualitative"
            assert metric["metric_scope"] == "mapped_positive_coverage_only"
            assert metric["supports_independent_validation_claim"] is False
            assert metric["negative_evidence_used"] is False
            assert metric["unmapped_cells_treated_as_negative"] is False
            assert metric["dataset_id"] == run["dataset_id"]
            assert metric["dataset_identity_sha256"] == run[
                "dataset_identity_sha256"
            ]
            assert metric["observation_ids"] == [
                run["mapped_positive_observation_id"]
            ]
            assert metric["observation_type"] == run[
                "mapped_positive_observation_type"
            ]
            assert metric["run_configuration_sha256"] == run[
                "run_configuration_sha256"
            ]
            assert metric["grid_identity_sha256"] == run[
                "evaluation_grid_identity_sha256"
            ]
            assert metric["grid_source_artifact_sha256"] == run[
                "evaluation_source_artifact_sha256"
            ]
            assert metric["bake_sha256"] == run[
                "evaluation_source_artifact_sha256"
            ]
            assert metric["predicted_mask_sha256"] == run[
                "predicted_mask_sha256"
            ]
            assert metric["valid_mask_sha256"] == event["input_mask_sha256"][
                "valid"
            ]
            assert len(metric["prediction_context_sha256"]) == 64
            assert len(metric["prediction_artifact_sha256"]) == 64
            mapped = run["mapped_positive_comparable_cell_count"]
            assert metric["mapped_positive_cell_count"] == run[
                "mapped_positive_cell_count"
            ]
            assert metric["mapped_positive_comparable_cell_count"] == mapped
            assert metric["predicted_positive_valid_cell_count"] == run[
                "predicted_positive_valid_cell_count"
            ]
            assert metric["intersecting_mapped_positive_cell_count"] == run[
                "intersecting_mapped_positive_cell_count"
            ]
            assert run["mapped_positive_coverage"] == pytest.approx(
                metric["mapped_positive_coverage_fraction"],
                abs=5.1e-7,
            )
            assert run["predicted_to_mapped_area_ratio"] == pytest.approx(
                run["predicted_positive_valid_cell_count"] / mapped,
                abs=5.1e-7,
            )

    identity = result.pop("artifact_identity_sha256")
    assert result["artifact_identity_scope"] == (
        "canonical SHA-256 of the complete result object excluding only "
        "artifact_identity_sha256"
    )
    assert identity == _canonical_sha256(result)
