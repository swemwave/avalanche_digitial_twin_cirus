"""Evidence-bound spatial validation metrics.

The evaluator owns observation rasterization.  Callers provide only a model
prediction and its valid-data mask; observed positives, surveyed known-absence
coverage, and positional-uncertainty exclusions are derived from immutable
features registered by :mod:`avycore.validation.contracts`.

Results from synthetic datasets are software verification.  A field holdout can
be labelled independent only when its immutable dataset identity is explicitly
listed in the code-reviewed trust registry.  The registry is intentionally empty
until real Mount Hosmer evidence has been reviewed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Sequence

import numpy as np
import shapely
from shapely.geometry import box, shape

from . import trust
from .contracts import ValidationDataset

PolygonObservationType = Literal["release_polygon", "deposit_polygon"]
EvaluationPartition = Literal["calibration", "holdout", "qualitative", "verification"]
RunoutEngineName = Literal["fast_routing_alpha", "particle_ensemble_voellmy"]
ReleaseSize = Literal["small", "medium", "large", "very_large"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RASTER_CHUNK_CELLS = 1_000_000
_PREDICTION_CONTEXT_VERSION = "avycore-prediction-context-v1"
_EVALUATION_GRID_VERSION = "avycore-evaluation-grid-v1"


def _ratio(numerator: int, denominator: int) -> float | None:
    return float(numerator / denominator) if denominator else None


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_sha256(value: Any, name: str, *, reject_placeholder: bool = True) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest.")
    if reject_placeholder and len(set(value)) == 1:
        raise ValueError(f"{name} must identify a real artifact, not a placeholder digest.")
    return value


def _require_nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty normalized string.")
    return value


def _require_finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number.")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite.")
    return converted


def _require_bool_array(value: Any, name: str, *, ndim: int = 2) -> np.ndarray:
    if np.ma.isMaskedArray(value):
        raise TypeError(
            f"{name} must not be a masked array; missing model inputs belong in valid_mask."
        )
    array = np.asarray(value)
    if array.dtype != np.bool_:
        raise TypeError(f"{name} must be a boolean array.")
    if array.ndim != ndim:
        raise ValueError(f"{name} must have {ndim} dimensions.")
    return array


def _bool_mask_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array, dtype=np.bool_)
    header = json.dumps(
        {"format": "packed-bool-little-v1", "shape": list(contiguous.shape)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    packed = np.packbits(contiguous.reshape(-1), bitorder="little").tobytes()
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    digest.update(packed)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvaluationGrid:
    """Exact north-up, square-cell grid used for one spatial evaluation.

    ``source_artifact_sha256`` is the immutable bake that supplied the grid and
    model inputs.  The grid identity is computed internally; a caller cannot pass
    an arbitrary identity string detached from the actual grid definition.
    """

    crs: str
    west: float
    north: float
    resolution_m: float
    shape: tuple[int, int]
    source_artifact_sha256: str
    grid_identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        crs = _require_nonempty_text(self.crs, "crs")
        if not re.fullmatch(r"EPSG:[1-9][0-9]*", crs):
            raise ValueError("crs must be a normalized EPSG authority string.")
        west = _require_finite_number(self.west, "west")
        north = _require_finite_number(self.north, "north")
        resolution = _require_finite_number(self.resolution_m, "resolution_m")
        if resolution <= 0:
            raise ValueError("resolution_m must be positive.")
        if not isinstance(self.shape, tuple) or len(self.shape) != 2:
            raise TypeError("shape must be an exact (rows, columns) tuple.")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.shape
        ):
            raise ValueError("shape rows and columns must be positive integers.")
        artifact = _require_sha256(
            self.source_artifact_sha256,
            "source_artifact_sha256",
        )
        south = north - self.shape[0] * resolution
        east = west + self.shape[1] * resolution
        if not math.isfinite(south) or not math.isfinite(east):
            raise ValueError("Evaluation grid bounds must be finite.")
        identity = _canonical_sha256(
            {
                "schema": _EVALUATION_GRID_VERSION,
                "crs": crs,
                "west_hex": west.hex(),
                "north_hex": north.hex(),
                "resolution_m_hex": resolution.hex(),
                "shape": list(self.shape),
                "source_artifact_sha256": artifact,
                "row_order": "north_to_south",
                "cell_sampling": "cell_center",
            }
        )
        object.__setattr__(self, "grid_identity_sha256", identity)

    @property
    def cell_area_m2(self) -> float:
        return float(self.resolution_m**2)

    @property
    def east(self) -> float:
        return float(self.west + self.shape[1] * self.resolution_m)

    @property
    def south(self) -> float:
        return float(self.north - self.shape[0] * self.resolution_m)


@dataclass(frozen=True)
class PredictionScenario:
    """Normalized deterministic scenario inputs, with no implicit clamping."""

    new_snow_cm: float
    wind_speed_kmh: float
    wind_direction_deg: float
    release_size: ReleaseSize

    def __post_init__(self) -> None:
        snow = _require_finite_number(self.new_snow_cm, "new_snow_cm")
        wind = _require_finite_number(self.wind_speed_kmh, "wind_speed_kmh")
        direction = _require_finite_number(self.wind_direction_deg, "wind_direction_deg")
        if not 0 <= snow <= 300:
            raise ValueError("new_snow_cm must already be normalized to [0, 300].")
        if not 0 <= wind <= 200:
            raise ValueError("wind_speed_kmh must already be normalized to [0, 200].")
        if not 0 <= direction < 360:
            raise ValueError(
                "wind_direction_deg must be in [0, 360) using the meteorological wind-from "
                "convention."
            )
        if self.release_size not in {"small", "medium", "large", "very_large"}:
            raise ValueError("release_size is not recognized.")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "new_snow_cm": float(self.new_snow_cm),
            "wind_speed_kmh": float(self.wind_speed_kmh),
            "wind_direction_deg": float(self.wind_direction_deg),
            "release_size": self.release_size,
        }


@dataclass(frozen=True)
class PredictionContext:
    """Identity of the deterministic run that produced one prediction."""

    event_id: str
    model_version: str
    config_sha256: str
    bake_sha256: str
    engine: RunoutEngineName
    random_seed: int | None
    scenario: PredictionScenario
    context_identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        event_id = _require_nonempty_text(self.event_id, "event_id")
        model_version = _require_nonempty_text(self.model_version, "model_version")
        config = _require_sha256(self.config_sha256, "config_sha256")
        bake = _require_sha256(self.bake_sha256, "bake_sha256")
        if self.engine not in {"fast_routing_alpha", "particle_ensemble_voellmy"}:
            raise ValueError("engine must name a canonical deterministic runout engine.")
        if self.engine == "fast_routing_alpha":
            if self.random_seed is not None:
                raise ValueError("Deterministic fast routing requires random_seed=None.")
        elif (
            isinstance(self.random_seed, bool)
            or not isinstance(self.random_seed, int)
            or not 0 <= self.random_seed <= 2**63 - 1
        ):
            raise ValueError(
                "Particle-ensemble predictions require a random_seed in [0, 2**63 - 1]."
            )
        if not isinstance(self.scenario, PredictionScenario):
            raise TypeError("scenario must be a PredictionScenario.")
        identity = _canonical_sha256(
            {
                "schema": _PREDICTION_CONTEXT_VERSION,
                "event_id": event_id,
                "model_version": model_version,
                "config_sha256": config,
                "bake_sha256": bake,
                "engine": self.engine,
                "random_seed": self.random_seed,
                "scenario": self.scenario.canonical_dict(),
            }
        )
        object.__setattr__(self, "context_identity_sha256", identity)


def _feature_index(dataset: ValidationDataset) -> dict[str, Any]:
    return {item.observation_id: item for item in dataset.observations}


def _require_partition(dataset: ValidationDataset, partition: str) -> None:
    if partition not in dataset.partition_counts:
        raise ValueError(
            f"Partition {partition!r} is not present in validation dataset "
            f"{dataset.manifest.dataset_id!r}."
        )


def _require_registered_features(
    dataset: ValidationDataset,
    observation_ids: Sequence[str],
    *,
    partition: str,
    observation_type: str,
) -> tuple[Any, ...]:
    if not observation_ids or len(set(observation_ids)) != len(observation_ids):
        raise ValueError("Observation IDs must be a non-empty unique sequence.")
    index = _feature_index(dataset)
    selected = []
    for observation_id in observation_ids:
        item = index.get(observation_id)
        if item is None:
            raise ValueError(f"Observation {observation_id!r} is not registered in the dataset.")
        if item.partition != partition or item.observation_type != observation_type:
            raise ValueError(
                f"Observation {observation_id!r} is {item.observation_type!r}/{item.partition!r}, "
                f"not {observation_type!r}/{partition!r}."
            )
        selected.append(item)
    return tuple(selected)


def _require_quantitative_dataset(dataset: ValidationDataset) -> None:
    if dataset.manifest.scientific_use not in {
        "field_validation",
        "calibration_only",
        "software_verification",
    }:
        raise ValueError("This dataset's scientific use does not permit quantitative metrics.")


def _require_complete_field_holdout(
    dataset: ValidationDataset,
    *,
    partition: str,
    observation_type: str,
    observation_ids: Sequence[str],
) -> None:
    if dataset.manifest.scientific_use != "field_validation" or partition != "holdout":
        return
    expected = {
        item.observation_id
        for item in dataset.observations
        if item.partition == "holdout" and item.observation_type == observation_type
    }
    supplied = set(observation_ids)
    if supplied != expected:
        omitted = sorted(expected - supplied)
        unexpected = sorted(supplied - expected)
        raise ValueError(
            "Independent field holdout metrics require the complete registered target cohort; "
            f"omitted={omitted}, unexpected={unexpected}."
        )


def _require_grid_binding(grid: EvaluationGrid, dataset: ValidationDataset) -> None:
    if grid.crs != dataset.manifest.crs:
        raise ValueError(
            f"Evaluation grid CRS {grid.crs!r} does not match dataset CRS "
            f"{dataset.manifest.crs!r}."
        )


def _require_context_grid_binding(
    context: PredictionContext,
    grid: EvaluationGrid,
) -> None:
    if context.bake_sha256 != grid.source_artifact_sha256:
        raise ValueError(
            "Prediction bake_sha256 does not match the EvaluationGrid source artifact."
        )


def _registered_scenario(item: Any) -> dict[str, Any] | None:
    if item.properties.get("scenario_status") != "documented":
        return None
    scenario = item.properties.get("scenario_inputs")
    if scenario is None:
        return None
    return {
        "new_snow_cm": float(scenario["new_snow_cm"]),
        "wind_speed_kmh": float(scenario["wind_speed_kmh"]),
        "wind_direction_deg": float(scenario["wind_direction_deg"]),
        "release_size": scenario["release_size"],
    }


def _require_context_for_observation(
    context: PredictionContext,
    item: Any,
    *,
    require_registered_scenario: bool,
) -> None:
    if context.event_id != item.event_id:
        raise ValueError(
            f"Prediction context event {context.event_id!r} does not match observation "
            f"event {item.event_id!r}."
        )
    registered = _registered_scenario(item)
    if registered is None and require_registered_scenario:
        raise ValueError(
            f"Observation {item.observation_id!r} has no documented historical scenario; "
            "quantitative field evaluation is not permitted."
        )
    if registered is not None and context.scenario.canonical_dict() != registered:
        raise ValueError(
            f"Prediction scenario does not match registered scenario for {item.observation_id!r}."
        )


def _require_field_uncertainty(dataset: ValidationDataset) -> None:
    if dataset.manifest.scientific_use not in {"field_validation", "calibration_only"}:
        return
    uncertainty = dataset.manifest.positional_uncertainty
    if (
        uncertainty.status != "quantified"
        or uncertainty.horizontal_m is None
        or uncertainty.confidence_level is None
        or not uncertainty.method.strip()
    ):
        raise ValueError(
            "Quantitative field metrics require manifest-level positional uncertainty with a "
            "distance, confidence level, and method."
        )


def _feature_uncertainty_m(dataset: ValidationDataset, item: Any) -> float:
    value = item.properties.get("horizontal_uncertainty_m")
    if value is None and dataset.manifest.positional_uncertainty.status == "quantified":
        value = dataset.manifest.positional_uncertainty.horizontal_m
    if value is None:
        raise ValueError(
            f"Observation {item.observation_id!r} has no quantified positional uncertainty."
        )
    result = _require_finite_number(value, "horizontal_uncertainty_m")
    if result < 0:
        raise ValueError("horizontal_uncertainty_m cannot be negative.")
    return result


def _trust_flags(
    dataset: ValidationDataset,
    partition: str,
) -> tuple[bool, bool, bool, str, bool]:
    uses_field_evidence = dataset.manifest.evidence_type in {
        "field_observation",
        "authoritative_inventory",
    }
    contract_eligible = (
        uses_field_evidence
        and dataset.manifest.scientific_use == "field_validation"
        and dataset.manifest.independent_of_model
        and partition == "holdout"
    )
    registered = dataset.dataset_identity_sha256 in trust.TRUSTED_DATASET_IDENTITIES_SHA256
    status = (
        "not_applicable"
        if not uses_field_evidence
        else "code_reviewed_trusted"
        if registered
        else "unregistered"
    )
    independent = contract_eligible and registered
    return uses_field_evidence, contract_eligible, registered, status, independent


def _union_geometry(features: Sequence[Any]):
    geometries = [shape(item.geometry) for item in features]
    merged = shapely.union_all(geometries)
    if merged.is_empty:
        raise ValueError("Registered geometries unexpectedly produced an empty union.")
    return merged


def _rasterize_cell_centers(
    geometry: Any,
    grid: EvaluationGrid,
    *,
    max_chunk_cells: int = _RASTER_CHUNK_CELLS,
) -> np.ndarray:
    """Rasterize by testing north-up cell centres in bounded Shapely 2 chunks."""

    rows, cols = grid.shape
    if max_chunk_cells <= 0:
        raise ValueError("max_chunk_cells must be positive.")
    rows_per_chunk = max(1, max_chunk_cells // cols)
    x = grid.west + (np.arange(cols, dtype="float64") + 0.5) * grid.resolution_m
    result = np.zeros(grid.shape, dtype=bool)
    shapely.prepare(geometry)
    for row_start in range(0, rows, rows_per_chunk):
        row_stop = min(rows, row_start + rows_per_chunk)
        row_numbers = np.arange(row_start, row_stop, dtype="float64")
        y = grid.north - (row_numbers + 0.5) * grid.resolution_m
        xx = np.broadcast_to(x, (row_stop - row_start, cols))
        yy = np.broadcast_to(y[:, None], xx.shape)
        result[row_start:row_stop] = shapely.intersects_xy(geometry, xx, yy)
    return result


def _polygon_evidence(
    *,
    dataset: ValidationDataset,
    grid: EvaluationGrid,
    partition: str,
    observation_type: PolygonObservationType,
    observation_ids: Sequence[str],
    coverage_observation_ids: Sequence[str],
    prediction_context: PredictionContext,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[Any, ...], tuple[tuple[str, float], ...]]:
    targets = _require_registered_features(
        dataset,
        observation_ids,
        partition=partition,
        observation_type=observation_type,
    )
    _require_complete_field_holdout(
        dataset,
        partition=partition,
        observation_type=observation_type,
        observation_ids=observation_ids,
    )
    target_events = {item.event_id for item in targets}
    if target_events != {prediction_context.event_id}:
        raise ValueError(
            "A polygon prediction must evaluate one registered event matching its "
            "PredictionContext."
        )
    for item in targets:
        _require_context_for_observation(
            prediction_context,
            item,
            require_registered_scenario=dataset.manifest.scientific_use
            in {"field_validation", "calibration_only"},
        )

    coverage = _require_registered_features(
        dataset,
        coverage_observation_ids,
        partition=partition,
        observation_type="survey_coverage_polygon",
    )
    if {item.event_id for item in coverage} != target_events:
        raise ValueError("Target and survey coverage geometries must belong to the same event.")
    incompatible = [
        item.observation_id
        for item in coverage
        if observation_type not in item.properties.get("target_observation_types", ())
    ]
    if incompatible:
        raise ValueError(
            f"Survey coverage {incompatible} is not registered for {observation_type!r}."
        )
    eligible_coverage = {
        item.observation_id
        for item in dataset.observations
        if item.partition == partition
        and item.event_id == prediction_context.event_id
        and item.observation_type == "survey_coverage_polygon"
        and observation_type in item.properties.get("target_observation_types", ())
    }
    if set(coverage_observation_ids) != eligible_coverage:
        omitted = sorted(eligible_coverage - set(coverage_observation_ids))
        unexpected = sorted(set(coverage_observation_ids) - eligible_coverage)
        raise ValueError(
            "Evaluation requires the complete compatible survey-coverage cohort; "
            f"omitted={omitted}, unexpected={unexpected}."
        )

    target_geometry = _union_geometry(targets)
    coverage_geometry = _union_geometry(coverage)
    if not coverage_geometry.covers(target_geometry):
        raise ValueError("Survey coverage geometry does not fully cover the target geometry.")
    grid_geometry = box(grid.west, grid.south, grid.east, grid.north)
    if not grid_geometry.covers(coverage_geometry):
        raise ValueError(
            "EvaluationGrid does not fully cover the registered survey domain; cropping known "
            "absence is not permitted."
        )

    uncertainties = tuple(
        (item.observation_id, _feature_uncertainty_m(dataset, item)) for item in targets
    )
    boundary_bands = [
        shape(item.geometry).boundary.buffer(distance)
        for item, (_, distance) in zip(targets, uncertainties)
        if distance > 0
    ]
    uncertain_geometry = shapely.union_all(boundary_bands) if boundary_bands else None
    observed = _rasterize_cell_centers(target_geometry, grid)
    domain = _rasterize_cell_centers(coverage_geometry, grid)
    uncertain = (
        np.zeros(grid.shape, dtype=bool)
        if uncertain_geometry is None
        else _rasterize_cell_centers(uncertain_geometry, grid)
    )
    if not domain.any():
        raise ValueError("Registered survey coverage contains no EvaluationGrid cell centres.")
    if not observed.any():
        raise ValueError(
            "Registered target geometries contain no EvaluationGrid cell centres; use a finer grid."
        )
    if np.any(observed & ~domain):
        raise ValueError(
            "Observed positives outside the surveyed domain indicate a rasterization error."
        )
    return observed, domain, uncertain, targets, uncertainties


@dataclass(frozen=True)
class BinaryMaskMetrics:
    dataset_id: str
    dataset_identity_sha256: str
    evidence_use: str
    partition: str
    uses_field_evidence: bool
    contract_eligible_for_independent_holdout_validation: bool
    dataset_trust_registered: bool
    dataset_trust_status: str
    is_independent_holdout_validation: bool
    observation_type: str
    observation_ids: tuple[str, ...]
    coverage_observation_ids: tuple[str, ...]
    grid_crs: str
    grid_identity_sha256: str
    grid_source_artifact_sha256: str
    prediction_context_sha256: str
    predicted_mask_sha256: str
    valid_mask_sha256: str
    prediction_artifact_sha256: str
    cell_area_m2: float
    boundary_uncertainty_m_by_observation: tuple[tuple[str, float], ...]
    positional_uncertainty_confidence_level: float | None
    positional_uncertainty_method: str
    rasterization_method: str
    uncertain_boundary_method: str
    surveyed_cell_count: int
    comparable_cell_count: int
    excluded_missing_cell_count: int
    excluded_uncertain_boundary_cell_count: int
    model_coverage_fraction: float
    observed_positive_cell_count: int
    observed_comparable_cell_count: int
    observed_model_coverage_fraction: float | None
    predicted_positive_cell_count: int
    predicted_positive_outside_survey_cell_count: int
    true_positive_cell_count: int
    false_positive_cell_count: int
    false_negative_cell_count: int
    precision: float | None
    recall: float | None
    f1: float | None
    intersection_over_union: float | None
    observed_area_m2: float
    predicted_area_m2: float
    intersection_area_m2: float
    union_area_m2: float
    excluded_observed_area_m2: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def binary_mask_metrics(
    predicted: Any,
    *,
    valid_mask: Any,
    evaluation_grid: EvaluationGrid,
    prediction_context: PredictionContext,
    dataset: ValidationDataset,
    partition: EvaluationPartition,
    observation_type: PolygonObservationType,
    observation_ids: Sequence[str],
    coverage_observation_ids: Sequence[str],
) -> BinaryMaskMetrics:
    """Evaluate a polygon prediction against internally rasterized evidence."""

    _require_partition(dataset, partition)
    _require_quantitative_dataset(dataset)
    if (
        dataset.manifest.coverage_semantics != "surveyed_domain"
        or dataset.manifest.absence_semantics != "surveyed_domain_supports_known_absence"
        or dataset.manifest.survey_completeness != "complete_for_declared_target"
    ):
        raise ValueError(
            "Precision/IoU requires an independently surveyed domain with known-absence semantics."
        )
    if not isinstance(evaluation_grid, EvaluationGrid):
        raise TypeError("evaluation_grid must be an EvaluationGrid.")
    if not isinstance(prediction_context, PredictionContext):
        raise TypeError("prediction_context must be a PredictionContext.")
    _require_grid_binding(evaluation_grid, dataset)
    _require_context_grid_binding(prediction_context, evaluation_grid)
    _require_field_uncertainty(dataset)

    predicted_array = _require_bool_array(predicted, "predicted")
    valid_array = _require_bool_array(valid_mask, "valid_mask")
    if predicted_array.shape != evaluation_grid.shape or valid_array.shape != evaluation_grid.shape:
        raise ValueError("Prediction and valid-data masks must match EvaluationGrid.shape exactly.")
    observed_array, domain, uncertain, _targets, uncertainties = _polygon_evidence(
        dataset=dataset,
        grid=evaluation_grid,
        partition=partition,
        observation_type=observation_type,
        observation_ids=observation_ids,
        coverage_observation_ids=coverage_observation_ids,
        prediction_context=prediction_context,
    )

    certain_domain = domain & ~uncertain
    certain_count = int(np.count_nonzero(certain_domain))
    if certain_count == 0:
        raise ValueError("Positional uncertainty excludes every surveyed cell centre.")
    comparable = certain_domain & valid_array
    surveyed_cells = int(np.count_nonzero(domain))
    comparable_cells = int(np.count_nonzero(comparable))
    if comparable_cells == 0:
        raise ValueError("No cells have both surveyed coverage and complete model inputs.")

    predicted_valid = predicted_array & comparable
    observed_valid = observed_array & comparable
    true_positive = int(np.count_nonzero(predicted_valid & observed_valid))
    false_positive = int(np.count_nonzero(predicted_valid & ~observed_array))
    false_negative = int(np.count_nonzero(~predicted_array & observed_valid))
    predicted_positive = int(np.count_nonzero(predicted_valid))
    observed_positive = int(np.count_nonzero(observed_array & domain))
    observed_comparable = int(np.count_nonzero(observed_valid))
    if observed_comparable == 0:
        raise ValueError(
            "No observed-positive cell centre remains after uncertainty and model masks."
        )
    union = true_positive + false_positive + false_negative
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )
    excluded_observed = observed_positive - observed_comparable
    uses_field, eligible, registered, trust_status, independent = _trust_flags(
        dataset, partition
    )

    predicted_hash = _bool_mask_sha256(predicted_array)
    valid_hash = _bool_mask_sha256(valid_array)
    prediction_artifact_hash = _canonical_sha256(
        {
            "schema": "avycore-polygon-prediction-artifact-v1",
            "grid_identity_sha256": evaluation_grid.grid_identity_sha256,
            "prediction_context_sha256": prediction_context.context_identity_sha256,
            "predicted_mask_sha256": predicted_hash,
            "valid_mask_sha256": valid_hash,
        }
    )
    cell_area_m2 = evaluation_grid.cell_area_m2
    manifest_uncertainty = dataset.manifest.positional_uncertainty
    return BinaryMaskMetrics(
        dataset_id=dataset.manifest.dataset_id,
        dataset_identity_sha256=dataset.dataset_identity_sha256,
        evidence_use=dataset.manifest.scientific_use,
        partition=partition,
        uses_field_evidence=uses_field,
        contract_eligible_for_independent_holdout_validation=eligible,
        dataset_trust_registered=registered,
        dataset_trust_status=trust_status,
        is_independent_holdout_validation=independent,
        observation_type=observation_type,
        observation_ids=tuple(observation_ids),
        coverage_observation_ids=tuple(coverage_observation_ids),
        grid_crs=evaluation_grid.crs,
        grid_identity_sha256=evaluation_grid.grid_identity_sha256,
        grid_source_artifact_sha256=evaluation_grid.source_artifact_sha256,
        prediction_context_sha256=prediction_context.context_identity_sha256,
        predicted_mask_sha256=predicted_hash,
        valid_mask_sha256=valid_hash,
        prediction_artifact_sha256=prediction_artifact_hash,
        cell_area_m2=cell_area_m2,
        boundary_uncertainty_m_by_observation=uncertainties,
        positional_uncertainty_confidence_level=manifest_uncertainty.confidence_level,
        positional_uncertainty_method=manifest_uncertainty.method,
        rasterization_method=(
            "Shapely 2 vectorized intersects_xy at north-up square-grid cell centres; "
            "polygon boundaries are included."
        ),
        uncertain_boundary_method=(
            "Cell centres intersecting each registered target boundary buffered by that "
            "observation's horizontal positional uncertainty are excluded."
        ),
        surveyed_cell_count=surveyed_cells,
        comparable_cell_count=comparable_cells,
        excluded_missing_cell_count=int(np.count_nonzero(certain_domain & ~valid_array)),
        excluded_uncertain_boundary_cell_count=int(np.count_nonzero(domain & uncertain)),
        model_coverage_fraction=float(comparable_cells / certain_count),
        observed_positive_cell_count=observed_positive,
        observed_comparable_cell_count=observed_comparable,
        observed_model_coverage_fraction=_ratio(observed_comparable, observed_positive),
        predicted_positive_cell_count=predicted_positive,
        predicted_positive_outside_survey_cell_count=int(
            np.count_nonzero(predicted_array & ~domain)
        ),
        true_positive_cell_count=true_positive,
        false_positive_cell_count=false_positive,
        false_negative_cell_count=false_negative,
        precision=precision,
        recall=recall,
        f1=f1,
        intersection_over_union=_ratio(true_positive, union),
        observed_area_m2=observed_comparable * cell_area_m2,
        predicted_area_m2=predicted_positive * cell_area_m2,
        intersection_area_m2=true_positive * cell_area_m2,
        union_area_m2=union * cell_area_m2,
        excluded_observed_area_m2=excluded_observed * cell_area_m2,
    )


@dataclass(frozen=True)
class EndpointMetrics:
    dataset_id: str
    dataset_identity_sha256: str
    evidence_use: str
    partition: str
    uses_field_evidence: bool
    contract_eligible_for_independent_holdout_validation: bool
    dataset_trust_registered: bool
    dataset_trust_status: str
    is_independent_holdout_validation: bool
    prediction_crs: str
    grid_identity_sha256: str
    grid_source_artifact_sha256: str
    prediction_context_sha256s: tuple[str, ...]
    prediction_artifact_sha256s: tuple[str, ...]
    prediction_set_sha256: str
    requested_pair_count: int
    evaluated_pair_count: int
    missing_prediction_count: int
    prediction_coverage_fraction: float
    observation_ids: tuple[str, ...]
    evaluated_observation_ids: tuple[str, ...]
    errors_m: tuple[float, ...]
    mean_error_m: float
    median_error_m: float
    root_mean_square_error_m: float
    maximum_error_m: float
    quantified_uncertainty_count: int
    positional_uncertainty_confidence_level: float | None
    positional_uncertainty_method: str
    within_uncertainty_fraction: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _endpoint_artifact_sha256(
    *,
    observation_id: str,
    xy: np.ndarray,
    valid: bool,
    context: PredictionContext,
    grid: EvaluationGrid,
) -> str:
    coordinates = [float(xy[0]).hex(), float(xy[1]).hex()] if valid else None
    return _canonical_sha256(
        {
            "schema": "avycore-endpoint-prediction-artifact-v1",
            "observation_id": observation_id,
            "valid": valid,
            "coordinates_xy_hex": coordinates,
            "grid_identity_sha256": grid.grid_identity_sha256,
            "prediction_context_sha256": context.context_identity_sha256,
        }
    )


def paired_endpoint_metrics(
    predicted_xy_m: Any,
    *,
    predicted_valid: Any,
    prediction_contexts: Sequence[PredictionContext],
    evaluation_grid: EvaluationGrid,
    observation_ids: Sequence[str],
    dataset: ValidationDataset,
    partition: EvaluationPartition,
) -> EndpointMetrics:
    """Evaluate a complete, ID-bound endpoint cohort with per-run provenance."""

    _require_partition(dataset, partition)
    _require_quantitative_dataset(dataset)
    if not isinstance(evaluation_grid, EvaluationGrid):
        raise TypeError("evaluation_grid must be an EvaluationGrid.")
    _require_grid_binding(evaluation_grid, dataset)
    _require_field_uncertainty(dataset)
    observations = _require_registered_features(
        dataset,
        observation_ids,
        partition=partition,
        observation_type="runout_endpoint",
    )
    _require_complete_field_holdout(
        dataset,
        partition=partition,
        observation_type="runout_endpoint",
        observation_ids=observation_ids,
    )
    if len(prediction_contexts) != len(observations) or not all(
        isinstance(context, PredictionContext) for context in prediction_contexts
    ):
        raise ValueError("prediction_contexts must contain one PredictionContext per observation.")
    for context, observation in zip(prediction_contexts, observations):
        _require_context_grid_binding(context, evaluation_grid)
        _require_context_for_observation(
            context,
            observation,
            require_registered_scenario=dataset.manifest.scientific_use
            in {"field_validation", "calibration_only"},
        )
    comparable_identity = {
        (context.model_version, context.config_sha256, context.bake_sha256, context.engine)
        for context in prediction_contexts
    }
    if len(comparable_identity) != 1:
        raise ValueError(
            "Every endpoint in a cohort must use the same model, config, bake, and engine."
        )

    predicted = np.asarray(predicted_xy_m, dtype="float64")
    valid = _require_bool_array(predicted_valid, "predicted_valid", ndim=1)
    if predicted.ndim != 2 or predicted.shape != (len(observations), 2):
        raise ValueError(
            "predicted_xy_m must contain one (easting, northing) pair per observation ID."
        )
    if valid.shape != (len(observations),):
        raise ValueError("predicted_valid must contain one value per requested observation ID.")
    if np.isinf(predicted).any() or not np.isfinite(predicted[valid]).all():
        raise ValueError(
            "Evaluated prediction coordinates must be finite; infinity is never missing data."
        )
    if (~valid).any() and not np.isnan(predicted[~valid]).all():
        raise ValueError(
            "Missing endpoint predictions must use (NaN, NaN), not arbitrary coordinates."
        )
    evaluated_count = int(np.count_nonzero(valid))
    if evaluated_count == 0:
        raise ValueError("No endpoint prediction is valid; endpoint error is not evaluable.")
    x = predicted[valid, 0]
    y = predicted[valid, 1]
    if np.any(
        (x < evaluation_grid.west)
        | (x > evaluation_grid.east)
        | (y < evaluation_grid.south)
        | (y > evaluation_grid.north)
    ):
        raise ValueError(
            "A valid endpoint prediction lies outside its bake-bound EvaluationGrid; mark AOI "
            "escapes as missing predictions."
        )

    observed = np.asarray(
        [item.geometry["coordinates"] for item in observations], dtype="float64"
    )
    if observed.shape != predicted.shape or not np.isfinite(observed).all():
        raise ValueError("Registered endpoint coordinates are not finite normalized 2D points.")
    errors = np.linalg.norm(predicted[valid] - observed[valid], axis=1)

    uncertainty = np.asarray(
        [_feature_uncertainty_m(dataset, item) for item in observations], dtype="float64"
    )
    known = valid & np.isfinite(uncertainty)
    quantified = int(np.count_nonzero(known))
    within_fraction = (
        float(
            np.mean(
                np.linalg.norm(predicted[known] - observed[known], axis=1)
                <= uncertainty[known]
            )
        )
        if quantified
        else None
    )
    uses_field, eligible, registered, trust_status, independent = _trust_flags(
        dataset, partition
    )
    evaluated_ids = tuple(
        observation_id for observation_id, include in zip(observation_ids, valid) if include
    )
    artifact_hashes = tuple(
        _endpoint_artifact_sha256(
            observation_id=observation_id,
            xy=xy,
            valid=bool(include),
            context=context,
            grid=evaluation_grid,
        )
        for observation_id, xy, include, context in zip(
            observation_ids, predicted, valid, prediction_contexts
        )
    )
    set_hash = _canonical_sha256(
        {
            "schema": "avycore-endpoint-prediction-set-v1",
            "dataset_identity_sha256": dataset.dataset_identity_sha256,
            "partition": partition,
            "artifacts": list(artifact_hashes),
        }
    )
    manifest_uncertainty = dataset.manifest.positional_uncertainty
    return EndpointMetrics(
        dataset_id=dataset.manifest.dataset_id,
        dataset_identity_sha256=dataset.dataset_identity_sha256,
        evidence_use=dataset.manifest.scientific_use,
        partition=partition,
        uses_field_evidence=uses_field,
        contract_eligible_for_independent_holdout_validation=eligible,
        dataset_trust_registered=registered,
        dataset_trust_status=trust_status,
        is_independent_holdout_validation=independent,
        prediction_crs=evaluation_grid.crs,
        grid_identity_sha256=evaluation_grid.grid_identity_sha256,
        grid_source_artifact_sha256=evaluation_grid.source_artifact_sha256,
        prediction_context_sha256s=tuple(
            context.context_identity_sha256 for context in prediction_contexts
        ),
        prediction_artifact_sha256s=artifact_hashes,
        prediction_set_sha256=set_hash,
        requested_pair_count=len(observations),
        evaluated_pair_count=evaluated_count,
        missing_prediction_count=len(observations) - evaluated_count,
        prediction_coverage_fraction=float(evaluated_count / len(observations)),
        observation_ids=tuple(observation_ids),
        evaluated_observation_ids=evaluated_ids,
        errors_m=tuple(float(value) for value in errors),
        mean_error_m=float(np.mean(errors)),
        median_error_m=float(np.median(errors)),
        root_mean_square_error_m=float(np.sqrt(np.mean(errors**2))),
        maximum_error_m=float(np.max(errors)),
        quantified_uncertainty_count=quantified,
        positional_uncertainty_confidence_level=manifest_uncertainty.confidence_level,
        positional_uncertainty_method=manifest_uncertainty.method,
        within_uncertainty_fraction=within_fraction,
    )
