"""Validation-data contracts and metrics; software verification, not field validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from avycore.validation import (
    LEGACY_VALIDATION_CONTRACT_VERSIONS,
    VALIDATION_CONTRACT_VERSION,
    EvaluationGrid,
    PredictionContext,
    PredictionScenario,
    ValidationContractError,
    ValidationDatasetManifest,
    binary_mask_metrics,
    load_validation_dataset,
    paired_endpoint_metrics,
)


def _feature(
    observation_id: str,
    event_id: str,
    partition: str,
    coordinates: list[float],
    *,
    verification_status: str = "field_verified",
    scenario_status: str = "documented",
    horizontal_uncertainty_m: float = 15.0,
    observation_method_class: str | None = None,
) -> dict:
    if observation_method_class is None:
        observation_method_class = (
            "synthetic_fixture" if verification_status == "synthetic" else "ground_survey"
        )
    properties = {
        "observation_id": observation_id,
        "event_id": event_id,
        "source_feature_id": f"SOURCE-{observation_id}",
        "observation_type": "runout_endpoint",
        "partition": partition,
        "observation_method": "Surveyed endpoint",
        "observation_method_class": observation_method_class,
        "verification_status": verification_status,
        "event_date_status": "known",
        "event_start_date": "2025-02-01",
        "event_end_date": "2025-02-01",
        "scenario_status": scenario_status,
        "horizontal_uncertainty_m": horizontal_uncertainty_m,
    }
    if scenario_status == "documented":
        properties["scenario_inputs"] = {
            "new_snow_cm": 30.0,
            "wind_speed_kmh": 40.0,
            "wind_direction_deg": 225.0,
            "release_size": "medium",
            "source": "Independent event record",
            "uncertainty_statement": "Conditions are reconstructed with stated source uncertainty.",
        }
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinates},
        "properties": properties,
    }
    return feature


def _polygon_feature(
    observation_id: str,
    event_id: str,
    partition: str,
    observation_type: str,
    bounds: tuple[float, float, float, float],
) -> dict:
    west, south, east, north = bounds
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        },
        "properties": {
            "observation_id": observation_id,
            "event_id": event_id,
            "source_feature_id": f"SOURCE-{observation_id}",
            "observation_type": observation_type,
            "partition": partition,
            "observation_method": "Characterized synthetic raster fixture",
            "observation_method_class": "synthetic_fixture",
            "verification_status": "synthetic",
            "event_date_status": "known",
            "event_start_date": "2025-02-01",
            "event_end_date": "2025-02-01",
            "scenario_status": "unknown",
            "horizontal_uncertainty_m": 0.0,
        },
    }
    if observation_type == "survey_coverage_polygon":
        feature["properties"]["target_observation_types"] = [
            "release_polygon",
            "deposit_polygon",
            "runout_endpoint",
        ]
    return feature


def _manifest(observations_sha256: str, **updates) -> dict:
    manifest = {
        "schema_version": LEGACY_VALIDATION_CONTRACT_VERSIONS[-1],
        "dataset_id": "verified-events-v1",
        "title": "Independent surveyed avalanche endpoints",
        "source": {
            "provider": "Example professional survey provider",
            "citation": "Example event survey, version 1",
            "source_uri": "https://example.invalid/survey",
            "licence": "Research permission recorded by the provider",
            "permitted_use": "Model calibration and independent holdout evaluation",
        },
        "acquisition": {
            "status": "bounded",
            "start_date": "2025-01-01",
            "end_date": "2026-04-30",
            "temporal_precision": "day",
            "basis": "Event records and survey dates supplied by the provider",
        },
        "evidence_type": "field_observation",
        "scientific_use": "field_validation",
        "independent_of_model": True,
        "observation_types": ["runout_endpoint"],
        "original_crs": "EPSG:26911",
        "crs": "EPSG:26911",
        "horizontal_units": "metre",
        "axis_order": "easting_northing",
        "coordinate_dimensions": 2,
        "normalization_type": "identity",
        "normalization_method": "Identity transform; source survey already used EPSG:26911",
        "normalization_software": "none",
        "original_source_sha256": "0" * 64,
        "spatial_coverage": {
            "west": 637650.0,
            "south": 5491570.0,
            "east": 649650.0,
            "north": 5503570.0,
            "description": "Declared survey holding extent; not an absence-observation mask",
        },
        "coverage_semantics": "positive_observations_only",
        "survey_completeness": "incomplete",
        "detection_limitations": "Only mapped positive observations are represented.",
        "absence_semantics": "unknown_unless_explicitly_observed",
        "positional_uncertainty": {
            "status": "quantified",
            "horizontal_m": 15.0,
            "confidence_level": 0.95,
            "method": "Provider survey specification",
        },
        "observations_file": "observations.geojson",
        "observations_sha256": observations_sha256,
        "limitations": [
            "This synthetic contract fixture verifies ingestion only; it is not real field evidence."
        ],
    }
    manifest.update(updates)
    return manifest


def _write_dataset(tmp_path: Path, features: list[dict], **manifest_updates) -> Path:
    observations = {"type": "FeatureCollection", "features": features}
    observations_path = tmp_path / "observations.geojson"
    payload = json.dumps(observations, separators=(",", ":"))
    observations_path.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(digest, **manifest_updates)), encoding="utf-8"
    )
    return manifest_path


def _evaluation_grid(
    *,
    west: float = 639900.0,
    north: float = 5_495_200.0,
    resolution_m: float = 100.0,
    shape: tuple[int, int] = (3, 3),
) -> EvaluationGrid:
    return EvaluationGrid(
        crs="EPSG:26911",
        west=west,
        north=north,
        resolution_m=resolution_m,
        shape=shape,
        source_artifact_sha256=hashlib.sha256(b"validation-fixture-bake").hexdigest(),
    )


def _prediction_context(grid: EvaluationGrid, event_id: str) -> PredictionContext:
    return PredictionContext(
        event_id=event_id,
        model_version="software-verification-model-v1",
        config_sha256=hashlib.sha256(b"validation-fixture-config").hexdigest(),
        bake_sha256=grid.source_artifact_sha256,
        engine="fast_routing_alpha",
        engine_mode="alpha_only",
        random_seed=None,
        particles_left_the_aoi=0,
        aoi_boundary_contact=False,
        scenario=PredictionScenario(30.0, 40.0, 225.0, "medium"),
    )


def test_contract_loads_lineage_and_event_level_calibration_holdout(tmp_path: Path) -> None:
    manifest_path = _write_dataset(
        tmp_path,
        [
            _feature("OBS-1", "EVENT-1", "calibration", [640000.0, 5_495_000.0]),
            _feature("OBS-2", "EVENT-2", "holdout", [641000.0, 5_496_000.0]),
        ],
    )

    dataset = load_validation_dataset(manifest_path)

    assert dataset.manifest.schema_version == LEGACY_VALIDATION_CONTRACT_VERSIONS[-1]
    assert VALIDATION_CONTRACT_VERSION == "avycore-validation-dataset-v3"
    assert dataset.manifest.crs == "EPSG:26911"
    assert dataset.partition_counts == {"calibration": 1, "holdout": 1}
    assert [item.observation_id for item in dataset.observations] == ["OBS-1", "OBS-2"]


def test_contract_accepts_declared_non_bc_projected_crs() -> None:
    payload = _manifest(
        "1" * 64,
        original_crs="EPSG:2056",
        crs="EPSG:2056",
        normalization_method="Identity transform; source used CH1903+ / LV95",
    )

    manifest = ValidationDatasetManifest.model_validate(payload)

    assert manifest.crs == "EPSG:2056"
    colorado = ValidationDatasetManifest.model_validate(
        {
            **payload,
            "original_crs": "EPSG:32613",
            "crs": "EPSG:32613",
            "normalization_method": "Identity transform; source used WGS 84 / UTM zone 13N",
        }
    )
    assert colorado.crs == "EPSG:32613"
    with pytest.raises(ValueError, match="crs"):
        ValidationDatasetManifest.model_validate({**payload, "crs": "epsg:2056"})
    with pytest.raises(ValueError, match="code-reviewed projected metre CRS"):
        ValidationDatasetManifest.model_validate({**payload, "crs": "EPSG:4326"})


def test_contract_preserves_qualitative_whole_footprint_and_partial_scenario(
    tmp_path: Path,
) -> None:
    feature = _polygon_feature(
        "FOOTPRINT-1",
        "EVENT-1",
        "qualitative",
        "avalanche_footprint",
        (2_784_000.0, 1_184_000.0, 2_784_100.0, 1_184_100.0),
    )
    feature["properties"].update(
        observation_method="Manual interpretation of a post-event orthophoto",
        observation_method_class="remote_sensing_interpretation",
        verification_status="unverified",
        scenario_status="partially_documented",
        scenario_inputs={
            "new_snow_cm": 60.0,
            "source": "Campaign report",
            "uncertainty_statement": "Wind speed and direction were not quantified.",
        },
    )
    dataset = load_validation_dataset(
        _write_dataset(
            tmp_path,
            [feature],
            evidence_type="remote_sensing_interpretation",
            scientific_use="qualitative_comparison",
            observation_types=["avalanche_footprint"],
            original_crs="EPSG:2056",
            crs="EPSG:2056",
            normalization_method="Identity transform; source used CH1903+ / LV95",
            spatial_coverage={
                "west": 2_783_000.0,
                "south": 1_183_000.0,
                "east": 2_786_000.0,
                "north": 1_186_000.0,
                "description": "Positive-observation holding extent",
            },
        )
    )

    observation = dataset.observations[0]
    assert observation.observation_type == "avalanche_footprint"
    assert observation.properties["scenario_status"] == "partially_documented"
    assert observation.properties["scenario_inputs"]["new_snow_cm"] == 60.0


def test_contract_rejects_calibration_holdout_event_leakage(tmp_path: Path) -> None:
    manifest_path = _write_dataset(
        tmp_path,
        [
            _feature("OBS-1", "SAME-EVENT", "calibration", [640000.0, 5_495_000.0]),
            _feature("OBS-2", "SAME-EVENT", "holdout", [641000.0, 5_496_000.0]),
        ],
    )

    with pytest.raises(ValidationContractError, match="leakage"):
        load_validation_dataset(manifest_path)


@pytest.mark.parametrize("evidence_type", ["remote_sensing_interpretation", "synthetic", "model_output"])
def test_contract_never_promotes_ineligible_evidence_to_ground_truth(evidence_type: str) -> None:
    payload = _manifest("1" * 64, evidence_type=evidence_type)

    with pytest.raises(ValueError):
        ValidationDatasetManifest.model_validate(payload)


def test_contract_rejects_modified_observation_bytes(tmp_path: Path) -> None:
    manifest_path = _write_dataset(
        tmp_path,
        [_feature("OBS-1", "EVENT-1", "holdout", [640000.0, 5_495_000.0])],
    )
    observations_path = tmp_path / "observations.geojson"
    observations_path.write_text(
        observations_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )

    with pytest.raises(ValidationContractError, match="SHA-256"):
        load_validation_dataset(manifest_path)


def test_contract_rejects_nonfinite_uncertainty_and_fake_identity_transform() -> None:
    infinite = _manifest("1" * 64)
    infinite["positional_uncertainty"] = {
        "status": "quantified",
        "horizontal_m": float("inf"),
        "method": "Invalid test value",
    }
    with pytest.raises(ValueError, match="finite"):
        ValidationDatasetManifest.model_validate(infinite)

    contradictory = _manifest(
        "1" * 64,
        original_crs="EPSG:4326",
        normalization_type="identity",
    )
    with pytest.raises(ValueError, match="coordinate operation"):
        ValidationDatasetManifest.model_validate(contradictory)

    unknown_acquisition = _manifest("1" * 64)
    unknown_acquisition["acquisition"] = {
        "status": "unknown",
        "temporal_precision": "unknown",
        "basis": "No event acquisition dates are available",
    }
    with pytest.raises(ValueError, match="known or bounded acquisition"):
        ValidationDatasetManifest.model_validate(unknown_acquisition)


def test_loaded_contract_is_deeply_immutable(tmp_path: Path) -> None:
    dataset = load_validation_dataset(
        _write_dataset(
            tmp_path,
            [_feature("OBS-1", "EVENT-1", "holdout", [640000.0, 5_495_000.0])],
        )
    )

    with pytest.raises(ValidationError):
        dataset.manifest.scientific_use = "excluded"
    with pytest.raises(TypeError):
        dataset.observations[0].properties["partition"] = "calibration"
    with pytest.raises(TypeError):
        dataset.partition_counts["holdout"] = 99


def test_contract_represents_unknown_event_dates_without_fabricating_bounds(
    tmp_path: Path,
) -> None:
    feature = _feature(
        "OBS-1",
        "EVENT-UNKNOWN-DATE",
        "verification",
        [640000.0, 5_495_000.0],
        verification_status="synthetic",
        scenario_status="unknown",
    )
    feature["properties"]["event_date_status"] = "unknown"
    feature["properties"].pop("event_start_date")
    feature["properties"].pop("event_end_date")

    dataset = load_validation_dataset(
        _write_dataset(
            tmp_path,
            [feature],
            evidence_type="synthetic",
            scientific_use="software_verification",
        )
    )

    assert dataset.observations[0].properties["event_date_status"] == "unknown"
    assert "event_start_date" not in dataset.observations[0].properties
    assert len(dataset.manifest_sha256) == 64
    assert len(dataset.dataset_identity_sha256) == 64

    feature["properties"]["event_start_date"] = "2025-02-01"
    with pytest.raises(ValidationContractError, match="invented date bounds"):
        load_validation_dataset(
            _write_dataset(
                tmp_path,
                [feature],
                evidence_type="synthetic",
                scientific_use="software_verification",
            )
        )


def test_contract_enforces_normalized_meteorological_wind_direction(tmp_path: Path) -> None:
    feature = _feature(
        "OBS-1", "EVENT-1", "holdout", [640000.0, 5_495_000.0]
    )
    feature["properties"]["scenario_inputs"]["wind_direction_deg"] = 360.0

    with pytest.raises(ValidationContractError, match="wind direction"):
        load_validation_dataset(_write_dataset(tmp_path, [feature]))


def test_contract_rejects_imagery_method_relabelled_as_authoritative(
    tmp_path: Path,
) -> None:
    feature = _feature(
        "OBS-1",
        "EVENT-1",
        "holdout",
        [640000.0, 5_495_000.0],
        observation_method_class="remote_sensing_interpretation",
    )
    feature["properties"]["observation_method"] = (
        "Unverified Sentinel-2 visual interpretation"
    )

    with pytest.raises(ValidationContractError, match="cannot support quantitative field"):
        load_validation_dataset(
            _write_dataset(tmp_path, [feature], evidence_type="authoritative_inventory")
        )


def test_contract_requires_consistent_event_dates_and_scenarios(tmp_path: Path) -> None:
    target = _polygon_feature(
        "RELEASE-1",
        "EVENT-1",
        "verification",
        "release_polygon",
        (640000.0, 5_495_000.0, 640100.0, 5_495_100.0),
    )
    coverage = _polygon_feature(
        "COVERAGE-1",
        "EVENT-1",
        "verification",
        "survey_coverage_polygon",
        (639900.0, 5_494_900.0, 640200.0, 5_495_200.0),
    )
    coverage["properties"]["event_start_date"] = "2025-02-02"
    coverage["properties"]["event_end_date"] = "2025-02-02"

    with pytest.raises(ValidationContractError, match="inconsistent dates or scenario"):
        load_validation_dataset(
            _write_dataset(
                tmp_path,
                [target, coverage],
                evidence_type="synthetic",
                scientific_use="software_verification",
                observation_types=["release_polygon", "survey_coverage_polygon"],
                coverage_semantics="surveyed_domain",
                survey_completeness="complete_for_declared_target",
                detection_limitations="The synthetic domain is fully characterized.",
                absence_semantics="surveyed_domain_supports_known_absence",
            )
        )


def _software_polygon_dataset(tmp_path: Path):
    return load_validation_dataset(
        _write_dataset(
            tmp_path,
            [
                _polygon_feature(
                    "RELEASE-1",
                    "EVENT-1",
                    "verification",
                    "release_polygon",
                    (640000.0, 5_495_000.0, 640100.0, 5_495_100.0),
                ),
                _polygon_feature(
                    "COVERAGE-1",
                    "EVENT-1",
                    "verification",
                    "survey_coverage_polygon",
                    (639900.0, 5_494_900.0, 640200.0, 5_495_200.0),
                ),
            ],
            evidence_type="synthetic",
            scientific_use="software_verification",
            observation_types=["release_polygon", "survey_coverage_polygon"],
            coverage_semantics="surveyed_domain",
            survey_completeness="complete_for_declared_target",
            detection_limitations="The complete domain is a characterized synthetic grid.",
            absence_semantics="surveyed_domain_supports_known_absence",
        )
    )


def _software_endpoint_dataset(tmp_path: Path):
    return load_validation_dataset(
        _write_dataset(
            tmp_path,
            [
                _feature(
                    "END-1",
                    "EVENT-1",
                    "verification",
                    [640000.0, 5_495_000.0],
                    verification_status="synthetic",
                    scenario_status="unknown",
                    horizontal_uncertainty_m=5.0,
                ),
                _feature(
                    "END-2",
                    "EVENT-2",
                    "verification",
                    [641000.0, 5_496_000.0],
                    verification_status="synthetic",
                    scenario_status="unknown",
                ),
            ],
            evidence_type="synthetic",
            scientific_use="software_verification",
            observation_types=["runout_endpoint"],
        )
    )


def test_binary_metrics_derive_evidence_geometry_and_report_missing_model_coverage(
    tmp_path: Path,
) -> None:
    dataset = _software_polygon_dataset(tmp_path)
    grid = _evaluation_grid()
    predicted = np.zeros(grid.shape, dtype=bool)
    valid = np.ones(grid.shape, dtype=bool)
    predicted[0, 0] = True  # excluded because the model has no valid input there
    valid[0, 0] = False
    predicted[1, 1] = True
    predicted[2, 2] = True

    metrics = binary_mask_metrics(
        predicted,
        valid_mask=valid,
        evaluation_grid=grid,
        prediction_context=_prediction_context(grid, "EVENT-1"),
        dataset=dataset,
        partition="verification",
        observation_type="release_polygon",
        observation_ids=["RELEASE-1"],
        coverage_observation_ids=["COVERAGE-1"],
    )

    assert metrics.uses_field_evidence is False
    assert metrics.is_independent_holdout_validation is False
    assert metrics.surveyed_cell_count == 9
    assert metrics.comparable_cell_count == 8
    assert metrics.excluded_missing_cell_count == 1
    assert metrics.excluded_uncertain_boundary_cell_count == 0
    assert metrics.model_coverage_fraction == pytest.approx(8 / 9)
    assert metrics.observed_positive_cell_count == 1
    assert metrics.observed_comparable_cell_count == 1
    assert metrics.observed_model_coverage_fraction == 1.0
    assert metrics.predicted_positive_cell_count == 2
    assert metrics.predicted_positive_outside_survey_cell_count == 0
    assert metrics.true_positive_cell_count == 1
    assert metrics.false_positive_cell_count == 1
    assert metrics.false_negative_cell_count == 0
    assert metrics.false_positive_area_m2 == 10_000.0
    assert metrics.false_negative_area_m2 == 0.0
    assert metrics.precision == pytest.approx(0.5)
    assert metrics.recall == 1.0
    assert metrics.f1 == pytest.approx(2 / 3)
    assert metrics.intersection_over_union == pytest.approx(0.5)
    assert metrics.observed_area_m2 == 10_000.0
    assert metrics.predicted_area_m2 == 20_000.0
    assert metrics.intersection_area_m2 == 10_000.0
    assert metrics.union_area_m2 == 20_000.0
    assert metrics.excluded_observed_area_m2 == 0.0
    assert metrics.dataset_trust_status == "not_applicable"
    assert metrics.engine_mode == "alpha_only"
    assert metrics.component_tested == "empirical_alpha_angle_plus_routing"


def test_endpoint_metrics_bind_ids_and_report_missing_predictions(tmp_path: Path) -> None:
    dataset = _software_endpoint_dataset(tmp_path)
    grid = _evaluation_grid(
        west=639000.0,
        north=5_497_000.0,
        resolution_m=100.0,
        shape=(30, 30),
    )
    metrics = paired_endpoint_metrics(
        [[640003.0, 5_495_004.0], [np.nan, np.nan]],
        predicted_valid=[True, False],
        prediction_contexts=[
            _prediction_context(grid, "EVENT-1"),
            _prediction_context(grid, "EVENT-2"),
        ],
        evaluation_grid=grid,
        observation_ids=["END-1", "END-2"],
        dataset=dataset,
        partition="verification",
    )

    assert metrics.requested_pair_count == 2
    assert metrics.evaluated_pair_count == 1
    assert metrics.missing_prediction_count == 1
    assert metrics.prediction_coverage_fraction == 0.5
    assert metrics.evaluated_observation_ids == ("END-1",)
    assert metrics.errors_m == pytest.approx((5.0,))
    assert metrics.mean_error_m == pytest.approx(5.0)
    assert metrics.median_error_m == pytest.approx(5.0)
    assert metrics.root_mean_square_error_m == pytest.approx(5.0)
    assert metrics.maximum_error_m == pytest.approx(5.0)
    assert metrics.quantified_uncertainty_count == 1
    assert metrics.within_uncertainty_fraction == 1.0
    assert metrics.uses_field_evidence is False
    assert metrics.dataset_trust_status == "not_applicable"
    assert metrics.engine_mode == "alpha_only"
    assert metrics.component_tested == "empirical_alpha_angle_plus_routing"
    assert metrics.aoi_coverage_status == "complete"


def test_endpoint_metrics_reject_qualitative_evidence(tmp_path: Path) -> None:
    dataset = load_validation_dataset(
        _write_dataset(
            tmp_path,
            [
                _feature(
                    "END-1",
                    "EVENT-1",
                    "qualitative",
                    [640000.0, 5_495_000.0],
                    verification_status="unverified",
                    scenario_status="unknown",
                    observation_method_class="remote_sensing_interpretation",
                )
            ],
            evidence_type="remote_sensing_interpretation",
            scientific_use="qualitative_comparison",
            observation_types=["runout_endpoint"],
        )
    )
    grid = _evaluation_grid(
        west=639000.0,
        north=5_497_000.0,
        resolution_m=100.0,
        shape=(30, 30),
    )

    with pytest.raises(ValueError, match="does not permit quantitative"):
        paired_endpoint_metrics(
            [[640000.0, 5_495_000.0]],
            predicted_valid=[True],
            prediction_contexts=[_prediction_context(grid, "EVENT-1")],
            evaluation_grid=grid,
            observation_ids=["END-1"],
            dataset=dataset,
            partition="qualitative",
        )


def test_metrics_reject_unregistered_partition_and_masked_arrays(tmp_path: Path) -> None:
    dataset = _software_polygon_dataset(tmp_path)
    evaluation_grid = _evaluation_grid()
    grid = np.zeros(evaluation_grid.shape, dtype=bool)
    context = _prediction_context(evaluation_grid, "EVENT-1")

    with pytest.raises(ValueError, match="not present"):
        binary_mask_metrics(
            grid,
            valid_mask=np.ones_like(grid),
            evaluation_grid=evaluation_grid,
            prediction_context=context,
            dataset=dataset,
            partition="holdout",
            observation_type="release_polygon",
            observation_ids=["RELEASE-1"],
            coverage_observation_ids=["COVERAGE-1"],
        )

    with pytest.raises(TypeError, match="masked array"):
        binary_mask_metrics(
            np.ma.array(grid, mask=np.zeros_like(grid)),
            valid_mask=np.ones_like(grid),
            evaluation_grid=evaluation_grid,
            prediction_context=context,
            dataset=dataset,
            partition="verification",
            observation_type="release_polygon",
            observation_ids=["RELEASE-1"],
            coverage_observation_ids=["COVERAGE-1"],
        )
