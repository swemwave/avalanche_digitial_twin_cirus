"""Software verification for evidence-owned validation rasterization.

All geometries in this module are synthetic analytical fixtures.  They verify
grid orientation, cell-centre rasterization, provenance binding, cohort controls,
and metric arithmetic; they are not field observations or physical validation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from avycore.validation import (
    VALIDATION_CONTRACT_VERSION,
    EvaluationGrid,
    PredictionContext,
    PredictionScenario,
    binary_mask_metrics,
    load_validation_dataset,
    paired_endpoint_metrics,
)
from avycore.validation.trust import TRUSTED_DATASET_IDENTITIES_SHA256


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _polygon(
    observation_id: str,
    event_id: str,
    observation_type: str,
    bounds: tuple[float, float, float, float],
    *,
    partition: str = "verification",
    field_evidence: bool = False,
    uncertainty_m: float = 0.0,
    target_observation_types: tuple[str, ...] = (
        "release_polygon",
        "deposit_polygon",
        "runout_endpoint",
    ),
    scenario: dict | None = None,
) -> dict:
    west, south, east, north = bounds
    properties = {
        "observation_id": observation_id,
        "event_id": event_id,
        "source_feature_id": f"SOURCE-{observation_id}",
        "observation_type": observation_type,
        "partition": partition,
        "observation_method": (
            "Synthetic analytical polygon" if not field_evidence else "Test-labelled survey polygon"
        ),
        "observation_method_class": (
            "ground_survey" if field_evidence else "synthetic_fixture"
        ),
        "verification_status": "field_verified" if field_evidence else "synthetic",
        "event_date_status": "known",
        "event_start_date": "2025-02-01",
        "event_end_date": "2025-02-01",
        "scenario_status": "documented" if scenario is not None else "unknown",
        "horizontal_uncertainty_m": uncertainty_m,
    }
    if scenario is not None:
        properties["scenario_inputs"] = scenario
    if observation_type == "survey_coverage_polygon":
        properties["target_observation_types"] = list(target_observation_types)
    return {
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
        "properties": properties,
    }


def _endpoint(
    observation_id: str,
    event_id: str,
    coordinates: tuple[float, float],
    *,
    partition: str = "verification",
    field_evidence: bool = False,
    uncertainty_m: float = 5.0,
    scenario: dict | None = None,
) -> dict:
    properties = {
        "observation_id": observation_id,
        "event_id": event_id,
        "source_feature_id": f"SOURCE-{observation_id}",
        "observation_type": "runout_endpoint",
        "partition": partition,
        "observation_method": (
            "Synthetic analytical endpoint" if not field_evidence else "Test-labelled survey point"
        ),
        "observation_method_class": (
            "ground_survey" if field_evidence else "synthetic_fixture"
        ),
        "verification_status": "field_verified" if field_evidence else "synthetic",
        "event_date_status": "known",
        "event_start_date": "2025-02-01",
        "event_end_date": "2025-02-01",
        "scenario_status": "documented" if scenario is not None else "unknown",
        "horizontal_uncertainty_m": uncertainty_m,
    }
    if scenario is not None:
        properties["scenario_inputs"] = scenario
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": list(coordinates)},
        "properties": properties,
    }


def _write_dataset(
    root: Path,
    features: list[dict],
    *,
    field_validation: bool = False,
    surveyed_domain: bool = True,
    bounds: tuple[float, float, float, float] = (0.0, 0.0, 60.0, 60.0),
    positional_uncertainty_m: float = 0.0,
):
    root.mkdir(parents=True, exist_ok=True)
    collection = {"type": "FeatureCollection", "features": features}
    observation_payload = json.dumps(collection, separators=(",", ":"))
    observation_path = root / "observations.geojson"
    observation_path.write_text(observation_payload, encoding="utf-8")
    observation_hash = hashlib.sha256(observation_payload.encode("utf-8")).hexdigest()
    observation_types = sorted(
        {feature["properties"]["observation_type"] for feature in features}
    )
    west, south, east, north = bounds
    manifest = {
        "schema_version": VALIDATION_CONTRACT_VERSION,
        "dataset_id": "field-contract-fixture" if field_validation else "software-grid-fixture",
        "title": "Contract and numerical verification fixture",
        "source": {
            "provider": "Synthetic pytest fixture",
            "citation": "Generated analytical geometry",
            "source_uri": "https://example.invalid/not-field-evidence",
            "licence": "Test-only",
            "permitted_use": "Software and contract verification only",
        },
        "acquisition": {
            "status": "bounded",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "temporal_precision": "day",
            "basis": "Synthetic fixture bounds",
        },
        "evidence_type": "field_observation" if field_validation else "synthetic",
        "scientific_use": "field_validation" if field_validation else "software_verification",
        "independent_of_model": True,
        "observation_types": observation_types,
        "original_crs": "EPSG:26911",
        "crs": "EPSG:26911",
        "horizontal_units": "metre",
        "axis_order": "easting_northing",
        "coordinate_dimensions": 2,
        "normalization_type": "identity",
        "normalization_method": "Identity transform in synthetic projected coordinates",
        "normalization_software": "none",
        "original_source_sha256": _sha256(f"original-source-{root.name}"),
        "spatial_coverage": {
            "west": west,
            "south": south,
            "east": east,
            "north": north,
            "description": "Exact analytical fixture extent",
        },
        "coverage_semantics": (
            "surveyed_domain" if surveyed_domain else "positive_observations_only"
        ),
        "survey_completeness": (
            "complete_for_declared_target" if surveyed_domain else "incomplete"
        ),
        "detection_limitations": "Analytical cell-centre fixture only",
        "absence_semantics": (
            "surveyed_domain_supports_known_absence"
            if surveyed_domain
            else "unknown_unless_explicitly_observed"
        ),
        "positional_uncertainty": {
            "status": "quantified",
            "horizontal_m": positional_uncertainty_m,
            "confidence_level": 0.95,
            "method": "Exact synthetic test parameter",
        },
        "observations_file": observation_path.name,
        "observations_sha256": observation_hash,
        "limitations": [
            "Coordinates and field labels are test fixtures, not evidence of avalanche accuracy."
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return load_validation_dataset(manifest_path)


def _scenario_dict() -> dict:
    return {
        "new_snow_cm": 30.0,
        "wind_speed_kmh": 40.0,
        "wind_direction_deg": 225.0,
        "release_size": "medium",
        "source": "Synthetic test scenario",
        "uncertainty_statement": "Exact only for software verification.",
    }


def _grid(*, shape: tuple[int, int] = (6, 6), north: float = 60.0) -> EvaluationGrid:
    return EvaluationGrid(
        crs="EPSG:26911",
        west=0.0,
        north=north,
        resolution_m=10.0,
        shape=shape,
        source_artifact_sha256=_sha256("synthetic-bake-artifact"),
    )


def _context(
    grid: EvaluationGrid,
    event_id: str,
    *,
    scenario: dict | None = None,
) -> PredictionContext:
    values = scenario or _scenario_dict()
    return PredictionContext(
        event_id=event_id,
        model_version="software-verification-model-v1",
        config_sha256=_sha256("synthetic-model-config"),
        bake_sha256=grid.source_artifact_sha256,
        engine="fast_routing_alpha",
        random_seed=None,
        scenario=PredictionScenario(
            new_snow_cm=values["new_snow_cm"],
            wind_speed_kmh=values["wind_speed_kmh"],
            wind_direction_deg=values["wind_direction_deg"],
            release_size=values["release_size"],
        ),
    )


def test_evaluation_grid_identity_is_internal_and_binds_the_source_artifact() -> None:
    first = _grid()
    replay = _grid()
    moved = EvaluationGrid(
        crs=first.crs,
        west=first.west,
        north=first.north + 10.0,
        resolution_m=first.resolution_m,
        shape=first.shape,
        source_artifact_sha256=first.source_artifact_sha256,
    )

    assert first.grid_identity_sha256 == replay.grid_identity_sha256
    assert first.grid_identity_sha256 != moved.grid_identity_sha256
    assert first.south == 0.0
    assert first.east == 60.0
    assert first.cell_area_m2 == 100.0
    with pytest.raises(ValueError, match="real artifact"):
        EvaluationGrid("EPSG:26911", 0.0, 60.0, 10.0, (6, 6), "0" * 64)


def test_prediction_context_preserves_engine_seed_semantics() -> None:
    grid = _grid()
    scenario = PredictionScenario(30.0, 40.0, 225.0, "medium")
    common = {
        "event_id": "EVENT-1",
        "model_version": "software-verification-model-v1",
        "config_sha256": _sha256("synthetic-model-config"),
        "bake_sha256": grid.source_artifact_sha256,
        "scenario": scenario,
    }

    with pytest.raises(ValueError, match="fast routing requires random_seed=None"):
        PredictionContext(
            **common,
            engine="fast_routing_alpha",
            random_seed=1,
        )
    with pytest.raises(ValueError, match="require a random_seed"):
        PredictionContext(
            **common,
            engine="particle_ensemble_voellmy",
            random_seed=None,
        )

    first = PredictionContext(
        **common,
        engine="particle_ensemble_voellmy",
        random_seed=1,
    )
    second = PredictionContext(
        **common,
        engine="particle_ensemble_voellmy",
        random_seed=2,
    )
    assert first.context_identity_sha256 != second.context_identity_sha256


def test_polygon_evidence_is_rasterized_at_north_up_cell_centres(tmp_path: Path) -> None:
    dataset = _write_dataset(
        tmp_path,
        [
            _polygon("TARGET", "EVENT-1", "release_polygon", (0.0, 30.0, 10.0, 40.0)),
            _polygon("COVERAGE", "EVENT-1", "survey_coverage_polygon", (0.0, 0.0, 40.0, 40.0)),
        ],
        bounds=(0.0, 0.0, 40.0, 40.0),
    )
    grid = _grid(shape=(4, 4), north=40.0)
    predicted = np.zeros(grid.shape, dtype=bool)
    predicted[0, 0] = True

    metrics = binary_mask_metrics(
        predicted,
        valid_mask=np.ones(grid.shape, dtype=bool),
        evaluation_grid=grid,
        prediction_context=_context(grid, "EVENT-1"),
        dataset=dataset,
        partition="verification",
        observation_type="release_polygon",
        observation_ids=["TARGET"],
        coverage_observation_ids=["COVERAGE"],
    )

    assert metrics.observed_positive_cell_count == 1
    assert metrics.true_positive_cell_count == 1
    assert metrics.false_positive_cell_count == 0
    assert metrics.false_negative_cell_count == 0
    assert metrics.intersection_over_union == 1.0


def test_polygon_uncertainty_and_missing_inputs_are_derived_not_caller_supplied(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path,
        [
            _polygon(
                "TARGET",
                "EVENT-1",
                "release_polygon",
                (5.0, 5.0, 35.0, 35.0),
                uncertainty_m=6.0,
            ),
            _polygon("COVERAGE", "EVENT-1", "survey_coverage_polygon", (0.0, 0.0, 40.0, 40.0)),
        ],
        bounds=(0.0, 0.0, 40.0, 40.0),
        positional_uncertainty_m=6.0,
    )
    grid = _grid(shape=(4, 4), north=40.0)
    predicted = np.zeros(grid.shape, dtype=bool)
    predicted[1:3, 1:3] = True
    valid = np.ones(grid.shape, dtype=bool)
    valid[1, 1] = False

    metrics = binary_mask_metrics(
        predicted,
        valid_mask=valid,
        evaluation_grid=grid,
        prediction_context=_context(grid, "EVENT-1"),
        dataset=dataset,
        partition="verification",
        observation_type="release_polygon",
        observation_ids=["TARGET"],
        coverage_observation_ids=["COVERAGE"],
    )

    assert metrics.surveyed_cell_count == 16
    assert metrics.excluded_uncertain_boundary_cell_count == 12
    assert metrics.excluded_missing_cell_count == 1
    assert metrics.comparable_cell_count == 3
    assert metrics.observed_positive_cell_count == 16
    assert metrics.observed_comparable_cell_count == 3
    assert metrics.true_positive_cell_count == 3
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.observed_area_m2 == 300.0
    assert metrics.excluded_observed_area_m2 == 1300.0
    assert metrics.boundary_uncertainty_m_by_observation == (("TARGET", 6.0),)
    assert metrics.grid_identity_sha256 == grid.grid_identity_sha256
    assert metrics.prediction_context_sha256 == _context(
        grid, "EVENT-1"
    ).context_identity_sha256
    assert len(metrics.predicted_mask_sha256) == 64
    assert len(metrics.valid_mask_sha256) == 64
    assert len(metrics.prediction_artifact_sha256) == 64
    assert metrics.uses_field_evidence is False
    assert metrics.contract_eligible_for_independent_holdout_validation is False
    assert metrics.dataset_trust_status == "not_applicable"
    assert metrics.is_independent_holdout_validation is False


@pytest.mark.parametrize(
    ("coverage_event", "coverage_targets", "coverage_bounds", "message"),
    [
        ("OTHER-EVENT", ("release_polygon",), (0.0, 0.0, 40.0, 40.0), "same event"),
        ("EVENT-1", ("deposit_polygon",), (0.0, 0.0, 40.0, 40.0), "not registered"),
        ("EVENT-1", ("release_polygon",), (0.0, 0.0, 15.0, 15.0), "does not fully cover"),
    ],
)
def test_polygon_evaluator_rejects_unlinked_or_incomplete_coverage(
    tmp_path: Path,
    coverage_event: str,
    coverage_targets: tuple[str, ...],
    coverage_bounds: tuple[float, float, float, float],
    message: str,
) -> None:
    dataset = _write_dataset(
        tmp_path,
        [
            _polygon("TARGET", "EVENT-1", "release_polygon", (10.0, 10.0, 20.0, 20.0)),
            _polygon(
                "COVERAGE",
                coverage_event,
                "survey_coverage_polygon",
                coverage_bounds,
                target_observation_types=coverage_targets,
            ),
        ],
        bounds=(0.0, 0.0, 40.0, 40.0),
    )
    grid = _grid(shape=(4, 4), north=40.0)

    with pytest.raises(ValueError, match=message):
        binary_mask_metrics(
            np.zeros(grid.shape, dtype=bool),
            valid_mask=np.ones(grid.shape, dtype=bool),
            evaluation_grid=grid,
            prediction_context=_context(grid, "EVENT-1"),
            dataset=dataset,
            partition="verification",
            observation_type="release_polygon",
            observation_ids=["TARGET"],
            coverage_observation_ids=["COVERAGE"],
        )


def test_field_holdout_requires_complete_cohort_and_stays_untrusted_by_default(
    tmp_path: Path,
) -> None:
    scenario = _scenario_dict()
    features = [
        _polygon(
            "TARGET-A",
            "EVENT-1",
            "release_polygon",
            (10.0, 10.0, 20.0, 20.0),
            partition="holdout",
            field_evidence=True,
            uncertainty_m=2.0,
            scenario=scenario,
        ),
        _polygon(
            "TARGET-B",
            "EVENT-1",
            "release_polygon",
            (30.0, 30.0, 40.0, 40.0),
            partition="holdout",
            field_evidence=True,
            uncertainty_m=2.0,
            scenario=scenario,
        ),
        _polygon(
            "COVERAGE",
            "EVENT-1",
            "survey_coverage_polygon",
            (0.0, 0.0, 60.0, 60.0),
            partition="holdout",
            field_evidence=True,
            uncertainty_m=2.0,
            target_observation_types=("release_polygon",),
            scenario=scenario,
        ),
    ]
    dataset = _write_dataset(
        tmp_path,
        features,
        field_validation=True,
        positional_uncertainty_m=2.0,
    )
    grid = _grid()
    context = _context(grid, "EVENT-1", scenario=scenario)
    predicted = np.zeros(grid.shape, dtype=bool)
    predicted[4, 1] = True
    predicted[2, 3] = True

    with pytest.raises(ValueError, match="complete registered target cohort"):
        binary_mask_metrics(
            predicted,
            valid_mask=np.ones(grid.shape, dtype=bool),
            evaluation_grid=grid,
            prediction_context=context,
            dataset=dataset,
            partition="holdout",
            observation_type="release_polygon",
            observation_ids=["TARGET-A"],
            coverage_observation_ids=["COVERAGE"],
        )

    metrics = binary_mask_metrics(
        predicted,
        valid_mask=np.ones(grid.shape, dtype=bool),
        evaluation_grid=grid,
        prediction_context=context,
        dataset=dataset,
        partition="holdout",
        observation_type="release_polygon",
        observation_ids=["TARGET-A", "TARGET-B"],
        coverage_observation_ids=["COVERAGE"],
    )

    assert TRUSTED_DATASET_IDENTITIES_SHA256 == frozenset()
    assert metrics.uses_field_evidence is True
    assert metrics.contract_eligible_for_independent_holdout_validation is True
    assert metrics.dataset_trust_registered is False
    assert metrics.dataset_trust_status == "unregistered"
    assert metrics.is_independent_holdout_validation is False


def test_prediction_context_must_match_the_grid_bake_and_registered_scenario(
    tmp_path: Path,
) -> None:
    scenario = _scenario_dict()
    dataset = _write_dataset(
        tmp_path,
        [
            _polygon(
                "TARGET",
                "EVENT-1",
                "release_polygon",
                (10.0, 10.0, 20.0, 20.0),
                uncertainty_m=0.0,
                scenario=scenario,
            ),
            _polygon(
                "COVERAGE",
                "EVENT-1",
                "survey_coverage_polygon",
                (0.0, 0.0, 60.0, 60.0),
                scenario=scenario,
            ),
        ],
        positional_uncertainty_m=0.0,
    )
    grid = _grid()
    wrong_bake = PredictionContext(
        event_id="EVENT-1",
        model_version="software-verification-model-v1",
        config_sha256=_sha256("synthetic-model-config"),
        bake_sha256=_sha256("different-bake"),
        engine="fast_routing_alpha",
        random_seed=None,
        scenario=_context(grid, "EVENT-1").scenario,
    )
    with pytest.raises(ValueError, match="does not match.*source artifact"):
        binary_mask_metrics(
            np.zeros(grid.shape, dtype=bool),
            valid_mask=np.ones(grid.shape, dtype=bool),
            evaluation_grid=grid,
            prediction_context=wrong_bake,
            dataset=dataset,
            partition="verification",
            observation_type="release_polygon",
            observation_ids=["TARGET"],
            coverage_observation_ids=["COVERAGE"],
        )

    wrong_scenario = PredictionContext(
        event_id="EVENT-1",
        model_version="software-verification-model-v1",
        config_sha256=_sha256("synthetic-model-config"),
        bake_sha256=grid.source_artifact_sha256,
        engine="fast_routing_alpha",
        random_seed=None,
        scenario=PredictionScenario(31.0, 40.0, 225.0, "medium"),
    )
    with pytest.raises(ValueError, match="does not match registered scenario"):
        binary_mask_metrics(
            np.zeros(grid.shape, dtype=bool),
            valid_mask=np.ones(grid.shape, dtype=bool),
            evaluation_grid=grid,
            prediction_context=wrong_scenario,
            dataset=dataset,
            partition="verification",
            observation_type="release_polygon",
            observation_ids=["TARGET"],
            coverage_observation_ids=["COVERAGE"],
        )


def test_endpoint_predictions_are_context_bound_hashed_and_report_missing(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset(
        tmp_path,
        [
            _endpoint("END-A", "EVENT-A", (15.0, 15.0)),
            _endpoint("END-B", "EVENT-B", (35.0, 35.0)),
        ],
        surveyed_domain=False,
        positional_uncertainty_m=5.0,
    )
    grid = _grid()
    contexts = [_context(grid, "EVENT-A"), _context(grid, "EVENT-B")]
    predictions = np.asarray([[18.0, 19.0], [np.nan, np.nan]])

    metrics = paired_endpoint_metrics(
        predictions,
        predicted_valid=np.asarray([True, False]),
        prediction_contexts=contexts,
        evaluation_grid=grid,
        observation_ids=["END-A", "END-B"],
        dataset=dataset,
        partition="verification",
    )

    assert metrics.errors_m == pytest.approx((5.0,))
    assert metrics.evaluated_observation_ids == ("END-A",)
    assert metrics.prediction_coverage_fraction == 0.5
    assert metrics.quantified_uncertainty_count == 1
    assert metrics.within_uncertainty_fraction == 1.0
    assert len(metrics.prediction_artifact_sha256s) == 2
    assert len(metrics.prediction_set_sha256) == 64
    assert metrics.dataset_trust_status == "not_applicable"
    assert metrics.is_independent_holdout_validation is False

    with pytest.raises(ValueError, match="does not match observation event"):
        paired_endpoint_metrics(
            predictions,
            predicted_valid=np.asarray([True, False]),
            prediction_contexts=[contexts[1], contexts[0]],
            evaluation_grid=grid,
            observation_ids=["END-A", "END-B"],
            dataset=dataset,
            partition="verification",
        )

    with pytest.raises(ValueError, match=r"must use \(NaN, NaN\)"):
        paired_endpoint_metrics(
            [[18.0, 19.0], [0.0, 0.0]],
            predicted_valid=np.asarray([True, False]),
            prediction_contexts=contexts,
            evaluation_grid=grid,
            observation_ids=["END-A", "END-B"],
            dataset=dataset,
            partition="verification",
        )


def test_field_endpoint_holdout_requires_every_registered_target(tmp_path: Path) -> None:
    scenario = _scenario_dict()
    dataset = _write_dataset(
        tmp_path,
        [
            _endpoint(
                "END-A",
                "EVENT-A",
                (15.0, 15.0),
                partition="holdout",
                field_evidence=True,
                scenario=scenario,
            ),
            _endpoint(
                "END-B",
                "EVENT-B",
                (35.0, 35.0),
                partition="holdout",
                field_evidence=True,
                scenario=scenario,
            ),
        ],
        field_validation=True,
        surveyed_domain=False,
        positional_uncertainty_m=5.0,
    )
    grid = _grid()

    with pytest.raises(ValueError, match="complete registered target cohort"):
        paired_endpoint_metrics(
            [[15.0, 15.0]],
            predicted_valid=np.asarray([True]),
            prediction_contexts=[_context(grid, "EVENT-A", scenario=scenario)],
            evaluation_grid=grid,
            observation_ids=["END-A"],
            dataset=dataset,
            partition="holdout",
        )

    metrics = paired_endpoint_metrics(
        [[15.0, 15.0], [35.0, 35.0]],
        predicted_valid=np.asarray([True, True]),
        prediction_contexts=[
            _context(grid, "EVENT-A", scenario=scenario),
            _context(grid, "EVENT-B", scenario=scenario),
        ],
        evaluation_grid=grid,
        observation_ids=["END-A", "END-B"],
        dataset=dataset,
        partition="holdout",
    )

    assert metrics.errors_m == (0.0, 0.0)
    assert metrics.contract_eligible_for_independent_holdout_validation is True
    assert metrics.dataset_trust_status == "unregistered"
    assert metrics.is_independent_holdout_validation is False
