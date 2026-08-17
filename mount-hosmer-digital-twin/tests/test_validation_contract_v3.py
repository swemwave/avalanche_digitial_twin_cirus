"""Validation-contract v3 evidence gates; synthetic contract fixtures only."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from avycore.validation import (
    VALIDATION_CONTRACT_VERSION,
    ComponentPredictionContext,
    EvaluationGrid,
    PositiveOnlyPolygonEvaluationCase,
    ValidationContractError,
    binary_mask_metrics,
    load_validation_dataset,
    model_validation_status,
    paired_endpoint_metrics,
    positive_only_polygon_cohort_metrics,
    positive_only_polygon_metrics,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _quantity(quantity: str) -> dict:
    return {
        "quantity": quantity,
        "representation": "bounded_interval",
        "units": (
            "metre" if quantity == "release_thickness" else "kilogram_per_cubic_metre"
        ),
        "lower": 0.5 if quantity == "release_thickness" else 180.0,
        "upper": 1.5 if quantity == "release_thickness" else 260.0,
        "source_uri": "https://example.invalid/release-state",
        "source_sha256": _sha(f"{quantity}-source"),
        "provenance": "Independent synthetic contract evidence; not field validation.",
        "uncertainty_statement": "Bounds are fixed synthetic test values.",
        "frozen_without_runout_target": True,
    }


def _event(event_id: str, *, component: str, suffix: str = "a") -> dict:
    event = {
        "event_id": event_id,
        "mountain_id": f"mountain-{suffix}",
        "path_id": f"path-{suffix}",
        "storm_cycle_id": f"storm-{suffix}",
        "avalanche_regime": "dry_dense_slab",
        "event_start_utc": "2025-02-01T00:00:00Z",
        "event_end_utc": "2025-02-01T06:00:00Z",
        "event_time_confidence": "Bounded to a six-hour interval by an independent record.",
        "terrain_surface": {
            "source_uri": "https://example.invalid/dem",
            "source_sha256": _sha(f"dem-{suffix}"),
            "acquisition_start_date": "2024-08-01",
            "acquisition_end_date": "2024-08-01",
            "acquisition_epoch_statement": "Synthetic pre-event bare-earth surface.",
            "crs": "EPSG:26911",
            "horizontal_units": "metre",
            "vertical_units": "metre",
            "vertical_datum": "CGVD2013 synthetic fixture declaration",
            "surface_type": "bare_earth",
            "event_surface_mismatch_statement": (
                "Snow-surface mismatch is explicit and not converted to zero error."
            ),
            "transformation_lineage": ("Identity projected-metre test grid",),
        },
        "model_inputs": [],
    }
    if component in {"conditional_runout", "end_to_end"}:
        event["release_thickness"] = _quantity("release_thickness")
        event["release_density"] = _quantity("release_density")
    if component == "end_to_end":
        event["model_inputs"] = [
            {
                "input_id": f"forcing-{suffix}",
                "category": "event_forcing",
                "parameter": "new_snow_depth",
                "units": "metre",
                "valid_start_utc": "2025-01-31T00:00:00Z",
                "valid_end_utc": "2025-02-01T06:00:00Z",
                "source_uri": "https://example.invalid/forcing",
                "source_sha256": _sha(f"forcing-{suffix}"),
                "provenance": "Independent synthetic forcing fixture.",
                "uncertainty_statement": "Uncertainty is explicit in the fixture.",
                "spatial_representativeness": "Applies only to the declared synthetic path.",
            }
        ]
        event["release_to_runout_rule_sha256"] = _sha(f"release-rule-{suffix}")
    return event


def _polygon(bounds: tuple[float, float, float, float]) -> dict:
    west, south, east, north = bounds
    return {
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
    }


def _feature(
    observation_id: str,
    event: dict,
    observation_type: str,
    *,
    partition: str = "holdout",
    bounds: tuple[float, float, float, float] = (20.0, 20.0, 40.0, 40.0),
    reviewed_remote_sensing: bool = False,
    detection_mask_ids: list[str] | None = None,
    target_types: list[str] | None = None,
) -> dict:
    method_class = (
        "reviewed_remote_sensing" if reviewed_remote_sensing else "ground_survey"
    )
    properties = {
        "observation_id": observation_id,
        "event_id": event["event_id"],
        "mountain_id": event["mountain_id"],
        "path_id": event["path_id"],
        "storm_cycle_id": event["storm_cycle_id"],
        "source_feature_id": f"SOURCE-{observation_id}",
        "observation_type": observation_type,
        "partition": partition,
        "observation_method": "Blind reviewed mapping on an independent synthetic source",
        "observation_method_class": method_class,
        "verification_status": (
            "reviewed_remote_sensing" if reviewed_remote_sensing else "field_verified"
        ),
        "event_date_status": "known",
        "event_start_date": "2025-02-01",
        "event_end_date": "2025-02-01",
        "scenario_status": "unknown",
        "observation_confidence": "high",
        "confidence_basis": "Independent synthetic contract fixture.",
        "survey_date": "2025-02-02",
        "source_resolution_m": 2.0,
        "detection_limitations": "No limitation is hidden; this is synthetic evidence.",
        "horizontal_uncertainty_m": 2.0,
        "horizontal_uncertainty_confidence_level": 0.95,
        "horizontal_uncertainty_method": "Synthetic fixed-radius uncertainty.",
        "annotation_blind_to_model_output": True,
    }
    if reviewed_remote_sensing:
        properties["annotation_protocol_sha256"] = _sha("blind-annotation-protocol")
    if observation_type == "release_polygon":
        properties["release_geometry_independent"] = True
    if observation_type == "deposit_polygon":
        properties["flow_observation_scope"] = "dense_flow_deposit"
    if observation_type == "runout_endpoint":
        properties["terminal_dense_flow_toe"] = True
    if observation_type == "survey_coverage_polygon":
        properties["target_observation_types"] = target_types or ["release_polygon"]
        properties["detection_mask_observation_ids"] = detection_mask_ids or []
        properties["complete_search_semantics"] = True
    geometry = (
        {"type": "Point", "coordinates": [bounds[0], bounds[1]]}
        if observation_type == "runout_endpoint"
        else _polygon(bounds)
    )
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _manifest(
    observations_sha256: str,
    events: list[dict],
    observation_types: list[str],
    *,
    component: str,
    positive_unlabelled: bool,
    reviewed_remote_sensing: bool = False,
) -> dict:
    profile = {"release": "R", "conditional_runout": "C", "end_to_end": "E"}[
        component
    ]
    return {
        "schema_version": VALIDATION_CONTRACT_VERSION,
        "dataset_id": f"v3-{component.replace('_', '-')}-fixture",
        "title": "Synthetic validation-contract v3 fixture",
        "source": {
            "provider": "Synthetic pytest fixture",
            "citation": "Contract verification only",
            "source_uri": "https://example.invalid/source",
            "licence": "Test-only",
            "permitted_use": "Contract and software verification only",
        },
        "acquisition": {
            "status": "bounded",
            "start_date": "2025-02-01",
            "end_date": "2025-02-02",
            "temporal_precision": "day",
            "basis": "Synthetic event and survey timestamps",
        },
        "evidence_type": (
            "reviewed_remote_sensing" if reviewed_remote_sensing else "field_observation"
        ),
        "scientific_use": "field_validation",
        "independent_of_model": True,
        "component_tested": component,
        "evidence_profile": profile,
        "label_state": (
            "positive_unlabelled"
            if positive_unlabelled
            else "surveyed_positive_and_known_absence"
        ),
        "events": events,
        "observation_types": observation_types,
        "original_crs": "EPSG:26911",
        "crs": "EPSG:26911",
        "horizontal_units": "metre",
        "axis_order": "easting_northing",
        "coordinate_dimensions": 2,
        "normalization_type": "identity",
        "normalization_method": "Identity operation on the synthetic test grid",
        "normalization_software": "none",
        "original_source_sha256": _sha("v3-original-source"),
        "spatial_coverage": {
            "west": 0.0,
            "south": 0.0,
            "east": 100.0,
            "north": 100.0,
            "description": "Synthetic contract-test extent",
        },
        "coverage_semantics": (
            "positive_observations_only" if positive_unlabelled else "surveyed_domain"
        ),
        "survey_completeness": (
            "incomplete" if positive_unlabelled else "complete_for_declared_target"
        ),
        "detection_limitations": "Synthetic limitations are explicitly declared.",
        "absence_semantics": (
            "unknown_unless_explicitly_observed"
            if positive_unlabelled
            else "surveyed_domain_supports_known_absence"
        ),
        "positional_uncertainty": {
            "status": "quantified",
            "horizontal_m": 2.0,
            "confidence_level": 0.95,
            "method": "Synthetic fixture uncertainty",
        },
        "observations_file": "observations.geojson",
        "observations_sha256": observations_sha256,
        "limitations": (
            "Synthetic field labels exercise the contract and are not accuracy evidence.",
        ),
    }


def _write(
    root: Path,
    features: list[dict],
    events: list[dict],
    *,
    component: str,
    positive_unlabelled: bool = True,
    reviewed_remote_sensing: bool = False,
    manifest_update: dict | None = None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"type": "FeatureCollection", "features": features},
        separators=(",", ":"),
    )
    (root / "observations.geojson").write_text(payload, encoding="utf-8")
    observation_types = sorted(
        {feature["properties"]["observation_type"] for feature in features}
    )
    manifest = _manifest(
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        events,
        observation_types,
        component=component,
        positive_unlabelled=positive_unlabelled,
        reviewed_remote_sensing=reviewed_remote_sensing,
    )
    if manifest_update:
        manifest.update(manifest_update)
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _grid() -> EvaluationGrid:
    return EvaluationGrid(
        crs="EPSG:26911",
        west=0.0,
        north=100.0,
        resolution_m=10.0,
        shape=(10, 10),
        source_artifact_sha256=_sha("v3-bake"),
    )


def _context(event_id: str, component: str) -> ComponentPredictionContext:
    return ComponentPredictionContext(
        event_id=event_id,
        component_tested=component,
        evidence_profile={
            "release": "R",
            "conditional_runout": "C",
            "end_to_end": "E",
        }[component],
        model_role="baseline",
        model_version="frozen-transparent-baseline-v1",
        config_sha256=_sha("v3-config"),
        bake_sha256=_grid().source_artifact_sha256,
        prediction_inputs_sha256=_sha(f"inputs-{event_id}"),
        engine=("current_release_model" if component == "release" else "fast_routing_alpha"),
        engine_mode=("relative_index_threshold" if component == "release" else "alpha_only"),
        random_seed=None,
        particles_left_the_aoi=0,
        aoi_boundary_contact=False,
        observed_release_geometry_supplied=component == "conditional_runout",
        release_initial_conditions_sha256=(
            None if component == "release" else _sha(f"release-state-{event_id}")
        ),
        holdout_targets_accessed=False,
        scenario=None,
    )


def test_v3_status_is_component_scoped_without_broadening_global_claim() -> None:
    status = model_validation_status()

    assert status["field_validation"]["status"] == "unavailable"
    assert status["field_validation"]["eligible_observation_count"] == 0
    assert status["validation_data_contract"]["schema_version"] == (
        "avycore-validation-dataset-v3"
    )
    assert status["validation_data_contract"]["trusted_dataset_count_by_component"] == {
        "release": 0,
        "conditional_runout": 0,
        "end_to_end": 0,
    }
    components = status["component_field_validation"]
    assert components["release"]["evidence_profile"] == "R"
    assert components["conditional_runout"]["evidence_profile"] == "C"
    assert components["end_to_end"]["evidence_profile"] == "E"
    assert all(item["status"] == "unavailable" for item in components.values())


def test_profile_c_endpoint_accepts_no_invented_weather_scenario(tmp_path: Path) -> None:
    event = _event("EVENT-C", component="conditional_runout")
    dataset = load_validation_dataset(
        _write(
            tmp_path,
            [
                _feature("RELEASE-C", event, "release_polygon"),
                _feature(
                    "TOE-C",
                    event,
                    "runout_endpoint",
                    bounds=(50.0, 20.0, 50.0, 20.0),
                ),
            ],
            [event],
            component="conditional_runout",
        )
    )

    result = paired_endpoint_metrics(
        [[53.0, 24.0]],
        predicted_valid=[True],
        prediction_contexts=[_context("EVENT-C", "conditional_runout")],
        evaluation_grid=_grid(),
        observation_ids=["TOE-C"],
        dataset=dataset,
        partition="holdout",
    )

    assert result.component_tested == "conditional_runout"
    assert result.evidence_profile == "C"
    assert result.errors_m == pytest.approx((5.0,))
    assert result.contract_eligible_for_independent_holdout_validation is True
    assert result.is_independent_holdout_validation is False
    assert dataset.observations[1].properties["scenario_status"] == "unknown"


def test_reviewed_remote_sensing_can_be_quantitative_only_when_blind_and_quantified(
    tmp_path: Path,
) -> None:
    event = _event("EVENT-R", component="release")
    feature = _feature(
        "RELEASE-R",
        event,
        "release_polygon",
        reviewed_remote_sensing=True,
    )
    dataset = load_validation_dataset(
        _write(
            tmp_path / "valid",
            [feature],
            [event],
            component="release",
            reviewed_remote_sensing=True,
        )
    )
    assert dataset.manifest.evidence_type == "reviewed_remote_sensing"

    not_blind = deepcopy(feature)
    not_blind["properties"]["annotation_blind_to_model_output"] = False
    with pytest.raises(ValidationContractError, match="not blind"):
        load_validation_dataset(
            _write(
                tmp_path / "not-blind",
                [not_blind],
                [event],
                component="release",
                reviewed_remote_sensing=True,
            )
        )

    unquantified = deepcopy(feature)
    unquantified["properties"].pop("horizontal_uncertainty_m")
    with pytest.raises(ValidationContractError, match="horizontal_uncertainty_m"):
        load_validation_dataset(
            _write(
                tmp_path / "unquantified",
                [unquantified],
                [event],
                component="release",
                reviewed_remote_sensing=True,
            )
        )


def test_positive_unlabelled_release_metric_has_no_negative_fields(tmp_path: Path) -> None:
    event = _event("EVENT-R", component="release")
    dataset = load_validation_dataset(
        _write(
            tmp_path,
            [_feature("RELEASE-R", event, "release_polygon")],
            [event],
            component="release",
        )
    )
    predicted = np.zeros(_grid().shape, dtype=bool)
    predicted[6:8, 2:4] = True
    result = positive_only_polygon_metrics(
        predicted,
        valid_mask=np.ones(_grid().shape, dtype=bool),
        evaluation_grid=_grid(),
        prediction_context=_context("EVENT-R", "release"),
        dataset=dataset,
        partition="holdout",
        observation_type="release_polygon",
        observation_ids=["RELEASE-R"],
    )

    payload = result.to_dict()
    assert result.component_tested == "release"
    assert result.evidence_profile == "R"
    assert result.negative_evidence_used is False
    assert result.unmapped_cells_treated_as_negative is False
    assert "precision" not in payload
    assert "false_positive_cell_count" not in payload
    assert "intersection_over_union" not in payload

    with pytest.raises(ValueError, match="Precision/IoU"):
        binary_mask_metrics(
            predicted,
            valid_mask=np.ones(_grid().shape, dtype=bool),
            evaluation_grid=_grid(),
            prediction_context=_context("EVENT-R", "release"),
            dataset=dataset,
            partition="holdout",
            observation_type="release_polygon",
            observation_ids=["RELEASE-R"],
            coverage_observation_ids=[],
        )


def test_positive_unlabelled_holdout_rejects_partial_event_cohort(tmp_path: Path) -> None:
    first = _event("EVENT-A", component="release", suffix="a")
    second = _event("EVENT-B", component="release", suffix="b")
    dataset = load_validation_dataset(
        _write(
            tmp_path,
            [
                _feature("RELEASE-A", first, "release_polygon"),
                _feature("RELEASE-B", second, "release_polygon", bounds=(50, 50, 70, 70)),
            ],
            [first, second],
            component="release",
        )
    )
    case = PositiveOnlyPolygonEvaluationCase(
        predicted=np.zeros(_grid().shape, dtype=bool),
        valid_mask=np.ones(_grid().shape, dtype=bool),
        evaluation_grid=_grid(),
        prediction_context=_context("EVENT-A", "release"),
        observation_ids=("RELEASE-A",),
    )
    with pytest.raises(ValueError, match="every registered holdout event"):
        positive_only_polygon_cohort_metrics(
            [case],
            dataset=dataset,
            partition="holdout",
            observation_type="release_polygon",
        )


def test_profile_c_binary_deposit_uses_complete_search_and_detection_masks(
    tmp_path: Path,
) -> None:
    event = _event("EVENT-C", component="conditional_runout")
    features = [
        _feature("RELEASE-C", event, "release_polygon"),
        _feature("DEPOSIT-C", event, "deposit_polygon", bounds=(20, 20, 40, 40)),
        _feature("MASK-C", event, "invalid_observation_mask", bounds=(0, 0, 10, 10)),
        _feature(
            "COVERAGE-C",
            event,
            "survey_coverage_polygon",
            bounds=(0, 0, 100, 100),
            detection_mask_ids=["MASK-C"],
            target_types=["deposit_polygon"],
        ),
    ]
    dataset = load_validation_dataset(
        _write(
            tmp_path,
            features,
            [event],
            component="conditional_runout",
            positive_unlabelled=False,
        )
    )
    predicted = np.zeros(_grid().shape, dtype=bool)
    predicted[6:8, 2:4] = True
    result = binary_mask_metrics(
        predicted,
        valid_mask=np.ones(_grid().shape, dtype=bool),
        evaluation_grid=_grid(),
        prediction_context=_context("EVENT-C", "conditional_runout"),
        dataset=dataset,
        partition="holdout",
        observation_type="deposit_polygon",
        observation_ids=["DEPOSIT-C"],
        coverage_observation_ids=["COVERAGE-C"],
    )
    assert result.component_tested == "conditional_runout"
    assert result.surveyed_cell_count == 99
    assert result.intersection_over_union == 1.0

    with pytest.raises(ValueError, match="does not score component_tested"):
        binary_mask_metrics(
            predicted,
            valid_mask=np.ones(_grid().shape, dtype=bool),
            evaluation_grid=_grid(),
            prediction_context=_context("EVENT-C", "conditional_runout"),
            dataset=dataset,
            partition="holdout",
            observation_type="release_polygon",
            observation_ids=["RELEASE-C"],
            coverage_observation_ids=["COVERAGE-C"],
        )


def test_v3_rejects_group_leakage_units_and_end_to_end_target_access(tmp_path: Path) -> None:
    calibration = _event("EVENT-DEV", component="release", suffix="shared")
    holdout = _event("EVENT-HOLDOUT", component="release", suffix="shared")
    features = [
        _feature("RELEASE-DEV", calibration, "release_polygon", partition="calibration"),
        _feature("RELEASE-HOLDOUT", holdout, "release_polygon", partition="holdout"),
    ]
    with pytest.raises(ValidationContractError, match="grouped holdout leakage"):
        load_validation_dataset(
            _write(
                tmp_path / "leakage",
                features,
                [calibration, holdout],
                component="release",
            )
        )

    conditional = _event("EVENT-C", component="conditional_runout")
    conditional["release_density"]["units"] = "kilogram_per_square_metre"
    with pytest.raises(ValidationContractError, match="kilogram_per_cubic_metre"):
        load_validation_dataset(
            _write(
                tmp_path / "units",
                [
                    _feature("RELEASE-C", conditional, "release_polygon"),
                    _feature("TOE-C", conditional, "runout_endpoint", bounds=(50, 20, 50, 20)),
                ],
                [conditional],
                component="conditional_runout",
            )
        )

    with pytest.raises(ValueError, match="holdout target access"):
        replace(
            _context("EVENT-E", "end_to_end"),
            holdout_targets_accessed=True,
        )


def test_profile_e_requires_inputs_rule_and_never_observed_release_on_prediction_path(
    tmp_path: Path,
) -> None:
    event = _event("EVENT-E", component="end_to_end")
    missing_inputs = deepcopy(event)
    missing_inputs["model_inputs"] = []
    with pytest.raises(ValidationContractError, match="missing_inputs"):
        load_validation_dataset(
            _write(
                tmp_path / "missing-inputs",
                [
                    _feature("RELEASE-E", missing_inputs, "release_polygon"),
                    _feature(
                        "TOE-E",
                        missing_inputs,
                        "runout_endpoint",
                        bounds=(50, 20, 50, 20),
                    ),
                ],
                [missing_inputs],
                component="end_to_end",
            )
        )

    with pytest.raises(ValueError, match="must not receive observed release geometry"):
        replace(
            _context("EVENT-E", "end_to_end"),
            observed_release_geometry_supplied=True,
        )

    wrong_dem = deepcopy(event)
    wrong_dem["terrain_surface"]["crs"] = "EPSG:2056"
    with pytest.raises(ValidationContractError, match="DEM CRS must match"):
        load_validation_dataset(
            _write(
                tmp_path / "wrong-dem",
                [
                    _feature("RELEASE-E", wrong_dem, "release_polygon"),
                    _feature("TOE-E", wrong_dem, "runout_endpoint", bounds=(50, 20, 50, 20)),
                ],
                [wrong_dem],
                component="end_to_end",
            )
        )
