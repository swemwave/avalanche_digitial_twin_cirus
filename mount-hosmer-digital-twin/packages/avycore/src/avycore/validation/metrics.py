"""Evidence-bound spatial validation metrics.

The evaluator owns observation rasterization.  Callers provide only a model
prediction and its valid-data mask; observed positives, surveyed known-absence
coverage, and positional-uncertainty exclusions are derived from immutable
features registered by :mod:`avycore.validation.contracts`.

Results from synthetic datasets are software verification.  A field holdout can
be labelled independent only when its immutable dataset identity is explicitly
listed in the code-reviewed trust registry.  The registry is intentionally empty
until an eligible independent field-observation cohort has been reviewed.

The strict binary evaluator is intentionally separate from the lower-rigor
positive-only evaluator. The latter never interprets unmapped space as absence
and therefore cannot emit IoU, precision, or an independent-validation claim.
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
from .contracts import (
    REVIEWED_PROJECTED_METRE_CRS,
    ValidationComponent,
    ValidationDataset,
)

PolygonObservationType = Literal["release_polygon", "deposit_polygon"]
PositiveOnlyPolygonObservationType = Literal[
    "release_polygon",
    "deposit_polygon",
    "avalanche_footprint",
]
EvaluationPartition = Literal["calibration", "holdout", "qualitative", "verification"]
RunoutEngineName = Literal["fast_routing_alpha", "particle_ensemble_voellmy"]
RunoutEngineMode = Literal["alpha_only", "dynamics_only", "hybrid"]
ExtentComponent = Literal[
    "empirical_alpha_angle_plus_routing",
    "particle_dynamics_path_extent",
    "empirical_alpha_energy_line_bounded_particle_path_extent",
]
ReleaseSize = Literal["small", "medium", "large", "very_large"]
AOICoverageStatus = Literal[
    "complete",
    "aoi_boundary_contact",
    "particles_left_the_aoi",
    "aoi_boundary_contact_and_particles_left_the_aoi",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RASTER_CHUNK_CELLS = 1_000_000
_PREDICTION_CONTEXT_VERSION = "avycore-prediction-context-v3"
_QUALITATIVE_PREDICTION_CONTEXT_VERSION = "avycore-qualitative-prediction-context-v1"
_COMPONENT_PREDICTION_CONTEXT_VERSION = "avycore-component-prediction-context-v1"
_EVALUATION_GRID_VERSION = "avycore-evaluation-grid-v1"
_HISTORICAL_SCENARIO_FIELDS = (
    "new_snow_cm",
    "wind_speed_kmh",
    "wind_direction_deg",
    "release_size",
)


def _ratio(numerator: int, denominator: int) -> float | None:
    return float(numerator / denominator) if denominator else None


def _extent_component_for_mode(engine_mode: RunoutEngineMode) -> ExtentComponent:
    """Name the active component an extent/endpoint metric can speak to."""

    return {
        "alpha_only": "empirical_alpha_angle_plus_routing",
        "dynamics_only": "particle_dynamics_path_extent",
        "hybrid": "empirical_alpha_energy_line_bounded_particle_path_extent",
    }[engine_mode]


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
        if crs not in REVIEWED_PROJECTED_METRE_CRS:
            supported = ", ".join(sorted(REVIEWED_PROJECTED_METRE_CRS))
            raise ValueError(
                f"crs must be a code-reviewed projected metre CRS; reviewed values are "
                f"{supported}. Before adding another EPSG code, review its projection, "
                "horizontal units, axis order, and datum/epoch compatibility."
            )
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
    engine_mode: RunoutEngineMode
    random_seed: int | None
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
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
            if self.engine_mode != "alpha_only":
                raise ValueError("fast_routing_alpha requires engine_mode='alpha_only'.")
            if self.random_seed is not None:
                raise ValueError("Deterministic fast routing requires random_seed=None.")
        else:
            if self.engine_mode not in {"dynamics_only", "hybrid"}:
                raise ValueError(
                    "particle_ensemble_voellmy requires engine_mode='dynamics_only' or 'hybrid'."
                )
            if (
                isinstance(self.random_seed, bool)
                or not isinstance(self.random_seed, int)
                or not 0 <= self.random_seed <= 2**63 - 1
            ):
                raise ValueError(
                    "Particle-ensemble predictions require a random_seed in [0, 2**63 - 1]."
                )
        if (
            isinstance(self.particles_left_the_aoi, bool)
            or not isinstance(self.particles_left_the_aoi, int)
            or self.particles_left_the_aoi < 0
        ):
            raise ValueError("particles_left_the_aoi must be a non-negative integer.")
        if self.engine_mode == "alpha_only" and self.particles_left_the_aoi != 0:
            raise ValueError("alpha_only routing cannot report particle AOI escapes.")
        if not isinstance(self.aoi_boundary_contact, bool):
            raise TypeError("aoi_boundary_contact must be a boolean.")
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
                "engine_mode": self.engine_mode,
                "random_seed": self.random_seed,
                "particles_left_the_aoi": self.particles_left_the_aoi,
                "aoi_boundary_contact": self.aoi_boundary_contact,
                "scenario": self.scenario.canonical_dict(),
            }
        )
        object.__setattr__(self, "context_identity_sha256", identity)

    @property
    def aoi_coverage_status(self) -> AOICoverageStatus:
        if self.aoi_boundary_contact and self.particles_left_the_aoi:
            return "aoi_boundary_contact_and_particles_left_the_aoi"
        if self.aoi_boundary_contact:
            return "aoi_boundary_contact"
        if self.particles_left_the_aoi:
            return "particles_left_the_aoi"
        return "complete"


@dataclass(frozen=True)
class ComponentPredictionContext:
    """Contract-v3 prediction identity scoped to one validation component.

    Profile C deliberately permits ``scenario=None`` because event new-snow and
    wind are not inputs to a conditional runout initialized from an independently
    observed release. ``prediction_inputs_sha256`` binds the actual immutable
    input inventory. Holdout target access is an explicit fail-closed field.
    """

    event_id: str
    component_tested: ValidationComponent
    evidence_profile: Literal["R", "C", "E"]
    model_role: Literal["baseline", "candidate"]
    model_version: str
    config_sha256: str
    bake_sha256: str
    prediction_inputs_sha256: str
    engine: str
    engine_mode: str
    random_seed: int | None
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
    observed_release_geometry_supplied: bool
    release_initial_conditions_sha256: str | None
    holdout_targets_accessed: bool
    scenario: PredictionScenario | None = None
    context_identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        event_id = _require_nonempty_text(self.event_id, "event_id")
        model_version = _require_nonempty_text(self.model_version, "model_version")
        engine = _require_nonempty_text(self.engine, "engine")
        engine_mode = _require_nonempty_text(self.engine_mode, "engine_mode")
        config = _require_sha256(self.config_sha256, "config_sha256")
        bake = _require_sha256(self.bake_sha256, "bake_sha256")
        inputs = _require_sha256(
            self.prediction_inputs_sha256,
            "prediction_inputs_sha256",
        )
        expected_profile = {
            "release": "R",
            "conditional_runout": "C",
            "end_to_end": "E",
        }[self.component_tested]
        if self.evidence_profile != expected_profile:
            raise ValueError(
                f"component_tested={self.component_tested!r} requires evidence_profile="
                f"{expected_profile!r}."
            )
        if (
            isinstance(self.random_seed, bool)
            or self.random_seed is not None
            and (
                not isinstance(self.random_seed, int)
                or not 0 <= self.random_seed <= 2**63 - 1
            )
        ):
            raise ValueError("random_seed must be null or an integer in [0, 2**63 - 1].")
        if (
            isinstance(self.particles_left_the_aoi, bool)
            or not isinstance(self.particles_left_the_aoi, int)
            or self.particles_left_the_aoi < 0
        ):
            raise ValueError("particles_left_the_aoi must be a non-negative integer.")
        if not isinstance(self.aoi_boundary_contact, bool):
            raise TypeError("aoi_boundary_contact must be a boolean.")
        if not isinstance(self.observed_release_geometry_supplied, bool):
            raise TypeError("observed_release_geometry_supplied must be a boolean.")
        if not isinstance(self.holdout_targets_accessed, bool):
            raise TypeError("holdout_targets_accessed must be a boolean.")
        if self.holdout_targets_accessed:
            raise ValueError(
                "Prediction context reports holdout target access; leaked predictions are "
                "unscoreable."
            )
        if self.component_tested == "release":
            if self.observed_release_geometry_supplied:
                raise ValueError("Release detection cannot consume observed release geometry.")
            if self.release_initial_conditions_sha256 is not None:
                raise ValueError("Profile R has no runout release-initial-condition artifact.")
        else:
            if self.release_initial_conditions_sha256 is None:
                raise ValueError("Profiles C/E require release_initial_conditions_sha256.")
            _require_sha256(
                self.release_initial_conditions_sha256,
                "release_initial_conditions_sha256",
            )
            if self.component_tested == "conditional_runout" and not (
                self.observed_release_geometry_supplied
            ):
                raise ValueError(
                    "Profile C must be initialized from independently observed release geometry."
                )
            if self.component_tested == "end_to_end" and (
                self.observed_release_geometry_supplied
            ):
                raise ValueError(
                    "Profile E must not receive observed release geometry on the prediction path."
                )
        if self.scenario is not None and not isinstance(self.scenario, PredictionScenario):
            raise TypeError("scenario must be a PredictionScenario or None.")
        identity = _canonical_sha256(
            {
                "schema": _COMPONENT_PREDICTION_CONTEXT_VERSION,
                "event_id": event_id,
                "component_tested": self.component_tested,
                "evidence_profile": self.evidence_profile,
                "model_role": self.model_role,
                "model_version": model_version,
                "config_sha256": config,
                "bake_sha256": bake,
                "prediction_inputs_sha256": inputs,
                "engine": engine,
                "engine_mode": engine_mode,
                "random_seed": self.random_seed,
                "particles_left_the_aoi": self.particles_left_the_aoi,
                "aoi_boundary_contact": self.aoi_boundary_contact,
                "observed_release_geometry_supplied": (
                    self.observed_release_geometry_supplied
                ),
                "release_initial_conditions_sha256": (
                    self.release_initial_conditions_sha256
                ),
                "holdout_targets_accessed": self.holdout_targets_accessed,
                "scenario": self.scenario.canonical_dict() if self.scenario else None,
            }
        )
        object.__setattr__(self, "context_identity_sha256", identity)

    @property
    def aoi_coverage_status(self) -> AOICoverageStatus:
        if self.aoi_boundary_contact and self.particles_left_the_aoi:
            return "aoi_boundary_contact_and_particles_left_the_aoi"
        if self.aoi_boundary_contact:
            return "aoi_boundary_contact"
        if self.particles_left_the_aoi:
            return "particles_left_the_aoi"
        return "complete"


@dataclass(frozen=True)
class QualitativePredictionContext:
    """Identity for a lower-rigor run with an incomplete historical scenario.

    Unlike :class:`PredictionContext`, this context deliberately has no complete
    ``PredictionScenario``. ``run_configuration_sha256`` binds the exact
    sensitivity/assumption artifact used to create the prediction without
    misrepresenting those assumptions as observed event-day snow or wind. The
    registered evidence remains the source of historical-scenario missingness.
    This context is accepted only by the qualitative positive-only evaluator.
    """

    event_id: str
    model_version: str
    config_sha256: str
    bake_sha256: str
    engine: RunoutEngineName
    engine_mode: RunoutEngineMode
    random_seed: int | None
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
    run_configuration_sha256: str
    context_identity_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        event_id = _require_nonempty_text(self.event_id, "event_id")
        model_version = _require_nonempty_text(self.model_version, "model_version")
        config = _require_sha256(self.config_sha256, "config_sha256")
        bake = _require_sha256(self.bake_sha256, "bake_sha256")
        run_configuration = _require_sha256(
            self.run_configuration_sha256,
            "run_configuration_sha256",
        )
        if self.engine not in {"fast_routing_alpha", "particle_ensemble_voellmy"}:
            raise ValueError("engine must name a canonical deterministic runout engine.")
        if self.engine == "fast_routing_alpha":
            if self.engine_mode != "alpha_only":
                raise ValueError("fast_routing_alpha requires engine_mode='alpha_only'.")
            if self.random_seed is not None:
                raise ValueError("Deterministic fast routing requires random_seed=None.")
        else:
            if self.engine_mode not in {"dynamics_only", "hybrid"}:
                raise ValueError(
                    "particle_ensemble_voellmy requires engine_mode='dynamics_only' or 'hybrid'."
                )
            if (
                isinstance(self.random_seed, bool)
                or not isinstance(self.random_seed, int)
                or not 0 <= self.random_seed <= 2**63 - 1
            ):
                raise ValueError(
                    "Particle-ensemble predictions require a random_seed in [0, 2**63 - 1]."
                )
        if (
            isinstance(self.particles_left_the_aoi, bool)
            or not isinstance(self.particles_left_the_aoi, int)
            or self.particles_left_the_aoi < 0
        ):
            raise ValueError("particles_left_the_aoi must be a non-negative integer.")
        if self.engine_mode == "alpha_only" and self.particles_left_the_aoi != 0:
            raise ValueError("alpha_only routing cannot report particle AOI escapes.")
        if not isinstance(self.aoi_boundary_contact, bool):
            raise TypeError("aoi_boundary_contact must be a boolean.")
        identity = _canonical_sha256(
            {
                "schema": _QUALITATIVE_PREDICTION_CONTEXT_VERSION,
                "event_id": event_id,
                "model_version": model_version,
                "config_sha256": config,
                "bake_sha256": bake,
                "engine": self.engine,
                "engine_mode": self.engine_mode,
                "random_seed": self.random_seed,
                "particles_left_the_aoi": self.particles_left_the_aoi,
                "aoi_boundary_contact": self.aoi_boundary_contact,
                "run_configuration_sha256": run_configuration,
            }
        )
        object.__setattr__(self, "context_identity_sha256", identity)

    @property
    def aoi_coverage_status(self) -> AOICoverageStatus:
        if self.aoi_boundary_contact and self.particles_left_the_aoi:
            return "aoi_boundary_contact_and_particles_left_the_aoi"
        if self.aoi_boundary_contact:
            return "aoi_boundary_contact"
        if self.particles_left_the_aoi:
            return "particles_left_the_aoi"
        return "complete"


@dataclass(frozen=True)
class QualitativeScenarioDocumentation:
    """Observed historical-scenario completeness for one mapped positive."""

    observation_id: str
    status: Literal["documented", "partially_documented", "unknown"]
    documented_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


PredictionEvidenceContext = PredictionContext | ComponentPredictionContext


def _is_v3(dataset: ValidationDataset) -> bool:
    return dataset.manifest.schema_version == "avycore-validation-dataset-v3"


def _require_component_context(
    dataset: ValidationDataset,
    context: PredictionEvidenceContext,
) -> None:
    if _is_v3(dataset):
        if not isinstance(context, ComponentPredictionContext):
            raise TypeError(
                "Validation contract v3 metrics require a ComponentPredictionContext."
            )
        if (
            context.component_tested != dataset.manifest.component_tested
            or context.evidence_profile != dataset.manifest.evidence_profile
        ):
            raise ValueError(
                "Prediction component/profile does not match the validation dataset contract."
            )
    elif not isinstance(context, PredictionContext):
        raise TypeError("Legacy validation datasets require a PredictionContext.")


def _require_metric_profile(dataset: ValidationDataset, observation_type: str) -> None:
    """Reject a target that does not test the dataset's declared component."""

    if not _is_v3(dataset):
        return
    component = dataset.manifest.component_tested
    permitted = {
        "release": {"release_polygon"},
        "conditional_runout": {"deposit_polygon", "runout_endpoint"},
        "end_to_end": {"release_polygon", "deposit_polygon", "runout_endpoint"},
    }[component]
    if observation_type not in permitted:
        raise ValueError(
            f"Observation type {observation_type!r} does not score component_tested="
            f"{component!r}; permitted targets are {sorted(permitted)}."
        )


def _metric_component(dataset: ValidationDataset, context: PredictionEvidenceContext) -> str:
    if _is_v3(dataset):
        assert isinstance(context, ComponentPredictionContext)
        return context.component_tested
    return _extent_component_for_mode(context.engine_mode)  # type: ignore[arg-type]


def _physical_component(context: PredictionEvidenceContext) -> str:
    if context.engine_mode in {"alpha_only", "dynamics_only", "hybrid"}:
        return _extent_component_for_mode(context.engine_mode)  # type: ignore[arg-type]
    return "release_detection_geometry"


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
    context: PredictionContext | ComponentPredictionContext | QualitativePredictionContext,
    grid: EvaluationGrid,
) -> None:
    if context.bake_sha256 != grid.source_artifact_sha256:
        raise ValueError(
            "Prediction bake_sha256 does not match the EvaluationGrid source artifact."
        )


def _require_scorable_prediction(
    context: PredictionContext | ComponentPredictionContext | QualitativePredictionContext,
) -> None:
    if context.particles_left_the_aoi or context.aoi_boundary_contact:
        failures = []
        if context.aoi_boundary_contact:
            failures.append("aoi_boundary_contact=True")
        if context.particles_left_the_aoi:
            failures.append(
                f"particles_left_the_aoi={context.particles_left_the_aoi}"
            )
        raise ValueError(
            f"Prediction is unscoreable because {', '.join(failures)}; enlarge the AOI and "
            "rerun rather than treating a boundary-contacting or escaped footprint as a "
            "complete runout extent."
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


def _qualitative_scenario_documentation(item: Any) -> QualitativeScenarioDocumentation:
    """Expose registered event-data gaps without manufacturing model inputs."""

    status = item.properties.get("scenario_status")
    if status not in {"documented", "partially_documented", "unknown"}:
        raise ValueError(
            f"Observation {item.observation_id!r} has an invalid scenario_status."
        )
    scenario = item.properties.get("scenario_inputs") or {}
    documented = tuple(field for field in _HISTORICAL_SCENARIO_FIELDS if field in scenario)
    missing = tuple(field for field in _HISTORICAL_SCENARIO_FIELDS if field not in scenario)
    return QualitativeScenarioDocumentation(
        observation_id=item.observation_id,
        status=status,
        documented_fields=documented,
        missing_fields=missing,
    )


def _require_qualitative_context_for_observation(
    context: QualitativePredictionContext,
    item: Any,
) -> None:
    if context.event_id != item.event_id:
        raise ValueError(
            f"Prediction context event {context.event_id!r} does not match observation "
            f"event {item.event_id!r}."
        )


def _require_context_for_observation(
    context: PredictionEvidenceContext,
    item: Any,
    *,
    require_registered_scenario: bool,
) -> None:
    if context.event_id != item.event_id:
        raise ValueError(
            f"Prediction context event {context.event_id!r} does not match observation "
            f"event {item.event_id!r}."
        )
    if isinstance(context, ComponentPredictionContext):
        return
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
        "reviewed_remote_sensing",
    }
    contract_eligible = (
        uses_field_evidence
        and dataset.manifest.scientific_use == "field_validation"
        and dataset.manifest.independent_of_model
        and partition == "holdout"
    )
    if _is_v3(dataset):
        component = dataset.manifest.component_tested
        assert component is not None
        registered = (
            dataset.dataset_identity_sha256
            in trust.TRUSTED_DATASET_IDENTITIES_BY_COMPONENT[component]
        )
    else:
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
    prediction_context: PredictionEvidenceContext,
    complete_holdout_cohort_verified: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[Any, ...], tuple[tuple[str, float], ...]]:
    targets = _require_registered_features(
        dataset,
        observation_ids,
        partition=partition,
        observation_type=observation_type,
    )
    if not complete_holdout_cohort_verified:
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
    if _is_v3(dataset):
        detection_mask_ids = tuple(
            sorted(
                {
                    mask_id
                    for item in coverage
                    for mask_id in item.properties.get(
                        "detection_mask_observation_ids",
                        (),
                    )
                }
            )
        )
        if detection_mask_ids:
            detection_masks = _require_registered_features(
                dataset,
                detection_mask_ids,
                partition=partition,
                observation_type="invalid_observation_mask",
            )
            invalid_geometry = _union_geometry(detection_masks)
            coverage_geometry = coverage_geometry.difference(invalid_geometry)
            if coverage_geometry.is_empty:
                raise ValueError("Detection masks exclude the complete survey domain.")
            if not coverage_geometry.covers(target_geometry):
                raise ValueError(
                    "A target intersects an invalid-observation mask and is not scoreable."
                )
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
    model_version: str
    model_role: str
    engine: str
    engine_mode: str
    component_tested: str
    evidence_profile: str | None
    physical_component_tested: str
    avalanche_regime: str
    mountain_id: str | None
    storm_cycle_id: str | None
    path_id: str | None
    event_id: str
    aoi_coverage_status: str
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
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
    false_positive_area_m2: float
    false_negative_area_m2: float
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


def _binary_mask_metrics_impl(
    predicted: Any,
    *,
    valid_mask: Any,
    evaluation_grid: EvaluationGrid,
    prediction_context: PredictionEvidenceContext,
    dataset: ValidationDataset,
    partition: EvaluationPartition,
    observation_type: PolygonObservationType,
    observation_ids: Sequence[str],
    coverage_observation_ids: Sequence[str],
    complete_holdout_cohort_verified: bool,
) -> BinaryMaskMetrics:
    """Evaluate a polygon prediction against internally rasterized evidence."""

    _require_partition(dataset, partition)
    _require_quantitative_dataset(dataset)
    _require_metric_profile(dataset, observation_type)
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
    _require_component_context(dataset, prediction_context)
    _require_grid_binding(evaluation_grid, dataset)
    _require_context_grid_binding(prediction_context, evaluation_grid)
    _require_scorable_prediction(prediction_context)
    _require_field_uncertainty(dataset)

    predicted_array = _require_bool_array(predicted, "predicted")
    valid_array = _require_bool_array(valid_mask, "valid_mask")
    if predicted_array.shape != evaluation_grid.shape or valid_array.shape != evaluation_grid.shape:
        raise ValueError("Prediction and valid-data masks must match EvaluationGrid.shape exactly.")
    observed_array, domain, uncertain, targets, uncertainties = _polygon_evidence(
        dataset=dataset,
        grid=evaluation_grid,
        partition=partition,
        observation_type=observation_type,
        observation_ids=observation_ids,
        coverage_observation_ids=coverage_observation_ids,
        prediction_context=prediction_context,
        complete_holdout_cohort_verified=complete_holdout_cohort_verified,
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
    target = targets[0]
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
        model_version=prediction_context.model_version,
        model_role=(
            prediction_context.model_role
            if isinstance(prediction_context, ComponentPredictionContext)
            else "legacy_unspecified"
        ),
        engine=prediction_context.engine,
        engine_mode=prediction_context.engine_mode,
        component_tested=_metric_component(dataset, prediction_context),
        evidence_profile=dataset.manifest.evidence_profile,
        physical_component_tested=_physical_component(prediction_context),
        avalanche_regime=(
            "dry_dense_slab" if _is_v3(dataset) else "legacy_unspecified"
        ),
        mountain_id=target.properties.get("mountain_id"),
        storm_cycle_id=target.properties.get("storm_cycle_id"),
        path_id=target.properties.get("path_id"),
        event_id=prediction_context.event_id,
        aoi_coverage_status=prediction_context.aoi_coverage_status,
        particles_left_the_aoi=prediction_context.particles_left_the_aoi,
        aoi_boundary_contact=prediction_context.aoi_boundary_contact,
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
        false_positive_area_m2=false_positive * cell_area_m2,
        false_negative_area_m2=false_negative * cell_area_m2,
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


def binary_mask_metrics(
    predicted: Any,
    *,
    valid_mask: Any,
    evaluation_grid: EvaluationGrid,
    prediction_context: PredictionEvidenceContext,
    dataset: ValidationDataset,
    partition: EvaluationPartition,
    observation_type: PolygonObservationType,
    observation_ids: Sequence[str],
    coverage_observation_ids: Sequence[str],
) -> BinaryMaskMetrics:
    """Evaluate one polygon prediction using the original strict contract."""

    return _binary_mask_metrics_impl(
        predicted,
        valid_mask=valid_mask,
        evaluation_grid=evaluation_grid,
        prediction_context=prediction_context,
        dataset=dataset,
        partition=partition,
        observation_type=observation_type,
        observation_ids=observation_ids,
        coverage_observation_ids=coverage_observation_ids,
        complete_holdout_cohort_verified=False,
    )


@dataclass(frozen=True)
class BinaryMaskEvaluationCase:
    """One event-bound prediction supplied to the strict holdout cohort API."""

    predicted: Any
    valid_mask: Any
    evaluation_grid: EvaluationGrid
    prediction_context: PredictionEvidenceContext
    observation_ids: tuple[str, ...]
    coverage_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.observation_ids, tuple) or not self.observation_ids:
            raise TypeError("observation_ids must be a non-empty tuple.")
        if (
            not isinstance(self.coverage_observation_ids, tuple)
            or not self.coverage_observation_ids
        ):
            raise TypeError("coverage_observation_ids must be a non-empty tuple.")


@dataclass(frozen=True)
class BinaryMaskCohortMetrics:
    """Complete field-holdout polygon cohort, retaining per-event strict scores.

    IoU and related counts are intentionally not pooled across events. Different
    events may use different grids or resolutions, so each ``event_metrics``
    member remains the authoritative strict score for that event.
    """

    dataset_id: str
    dataset_identity_sha256: str
    evidence_use: str
    partition: str
    observation_type: str
    complete_registered_target_cohort: bool
    complete_cohort_event_count: int
    independent_holdout_event_count: int
    observation_count: int
    event_ids: tuple[str, ...]
    model_version: str
    model_role: str
    engine: str
    engine_mode: str
    component_tested: str
    evidence_profile: str | None
    physical_component_tested: str
    avalanche_regime: str
    aoi_coverage_status: str
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
    uses_field_evidence: bool
    contract_eligible_for_independent_holdout_validation: bool
    dataset_trust_registered: bool
    dataset_trust_status: str
    is_independent_holdout_validation: bool
    metric_aggregation: str
    prediction_set_sha256: str
    event_metrics: tuple[BinaryMaskMetrics, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def binary_mask_cohort_metrics(
    cases: Sequence[BinaryMaskEvaluationCase],
    *,
    dataset: ValidationDataset,
    partition: Literal["holdout"],
    observation_type: PolygonObservationType,
) -> BinaryMaskCohortMetrics:
    """Evaluate every event in one strict registered field-holdout cohort.

    This is an additive cohort path. ``binary_mask_metrics`` retains its original
    fail-closed complete-target behavior for one-event datasets and continues to
    reject a partial multi-event holdout.
    """

    _require_partition(dataset, partition)
    if dataset.manifest.scientific_use != "field_validation" or partition != "holdout":
        raise ValueError(
            "Strict polygon cohort metrics require scientific_use='field_validation' and "
            "partition='holdout'."
        )
    if not cases or not all(isinstance(case, BinaryMaskEvaluationCase) for case in cases):
        raise TypeError("cases must contain at least one BinaryMaskEvaluationCase.")

    expected_by_event: dict[str, set[str]] = {}
    for item in dataset.observations:
        if item.partition == partition and item.observation_type == observation_type:
            expected_by_event.setdefault(item.event_id, set()).add(item.observation_id)
    if not expected_by_event:
        raise ValueError("The requested holdout polygon target type is not registered.")

    cases_by_event: dict[str, BinaryMaskEvaluationCase] = {}
    for case in cases:
        event_id = case.prediction_context.event_id
        if event_id in cases_by_event:
            raise ValueError(
                f"Strict cohort requires exactly one prediction case for {event_id!r}."
            )
        cases_by_event[event_id] = case
    if set(cases_by_event) != set(expected_by_event):
        missing = sorted(set(expected_by_event) - set(cases_by_event))
        unexpected = sorted(set(cases_by_event) - set(expected_by_event))
        raise ValueError(
            "Strict cohort requires exactly one prediction for every registered holdout event; "
            f"missing={missing}, unexpected={unexpected}."
        )

    comparable_identity = {
        (
            case.prediction_context.model_version,
            case.prediction_context.config_sha256,
            case.prediction_context.engine,
            case.prediction_context.engine_mode,
        )
        for case in cases
    }


    if len(comparable_identity) != 1:
        raise ValueError(
            "Every strict polygon cohort event must use the same model, config, engine, and "
            "engine mode; bake identities may differ by evaluation grid."
        )

    event_metrics: list[BinaryMaskMetrics] = []
    for event_id in sorted(expected_by_event):
        case = cases_by_event[event_id]
        supplied = set(case.observation_ids)
        expected = expected_by_event[event_id]
        if len(supplied) != len(case.observation_ids) or supplied != expected:
            omitted = sorted(expected - supplied)
            unexpected = sorted(supplied - expected)
            raise ValueError(
                f"Strict cohort event {event_id!r} requires every registered target; "
                f"omitted={omitted}, unexpected={unexpected}."
            )
        event_metrics.append(
            _binary_mask_metrics_impl(
                case.predicted,
                valid_mask=case.valid_mask,
                evaluation_grid=case.evaluation_grid,
                prediction_context=case.prediction_context,
                dataset=dataset,
                partition=partition,
                observation_type=observation_type,
                observation_ids=tuple(sorted(case.observation_ids)),
                coverage_observation_ids=tuple(sorted(case.coverage_observation_ids)),
                complete_holdout_cohort_verified=True,
            )
        )

    first = event_metrics[0]
    uses_field, eligible, registered, trust_status, independent = _trust_flags(
        dataset, partition
    )
    prediction_set_sha256 = _canonical_sha256(
        {
            "schema": "avycore-binary-mask-holdout-cohort-v1",
            "dataset_identity_sha256": dataset.dataset_identity_sha256,
            "partition": partition,
            "observation_type": observation_type,
            "event_predictions": [
                {
                    "event_id": event_id,
                    "prediction_artifact_sha256": metric.prediction_artifact_sha256,
                }
                for event_id, metric in zip(sorted(expected_by_event), event_metrics)
            ],
        }
    )
    return BinaryMaskCohortMetrics(
        dataset_id=dataset.manifest.dataset_id,
        dataset_identity_sha256=dataset.dataset_identity_sha256,
        evidence_use=dataset.manifest.scientific_use,
        partition=partition,
        observation_type=observation_type,
        complete_registered_target_cohort=True,
        complete_cohort_event_count=len(event_metrics),
        independent_holdout_event_count=len(event_metrics) if independent else 0,
        observation_count=sum(len(items) for items in expected_by_event.values()),
        event_ids=tuple(sorted(expected_by_event)),
        model_version=first.model_version,
        model_role=first.model_role,
        engine=first.engine,
        engine_mode=first.engine_mode,
        component_tested=first.component_tested,
        evidence_profile=first.evidence_profile,
        physical_component_tested=first.physical_component_tested,
        avalanche_regime=first.avalanche_regime,
        aoi_coverage_status="complete",
        particles_left_the_aoi=0,
        aoi_boundary_contact=False,
        uses_field_evidence=uses_field,
        contract_eligible_for_independent_holdout_validation=eligible,
        dataset_trust_registered=registered,
        dataset_trust_status=trust_status,
        is_independent_holdout_validation=independent,
        metric_aggregation="per_event_only_no_pooled_iou",
        prediction_set_sha256=prediction_set_sha256,
        event_metrics=tuple(event_metrics),
    )


def _optional_feature_uncertainty_m(
    dataset: ValidationDataset,
    item: Any,
) -> float | None:
    value = item.properties.get("horizontal_uncertainty_m")
    if value is None and dataset.manifest.positional_uncertainty.status == "quantified":
        value = dataset.manifest.positional_uncertainty.horizontal_m
    if value is None:
        return None
    result = _require_finite_number(value, "horizontal_uncertainty_m")
    if result < 0:
        raise ValueError("horizontal_uncertainty_m cannot be negative.")
    return result


@dataclass(frozen=True)
class PositiveOnlyPolygonMetrics:
    """Overlap with mapped positives when the source supplies no known absences.

    This deliberately has no precision, false-positive, F1, or IoU fields. A
    prediction outside a mapped positive is *unmapped*, not evidence of error.
    """

    dataset_id: str
    dataset_identity_sha256: str
    evidence_use: str
    partition: str
    uses_field_evidence: bool
    contract_eligible_for_independent_holdout_validation: bool
    dataset_trust_registered: bool
    dataset_trust_status: str
    is_independent_holdout_validation: bool
    metric_scope: str
    supports_independent_validation_claim: bool
    observation_type: str
    observation_ids: tuple[str, ...]
    grid_crs: str
    grid_identity_sha256: str
    grid_source_artifact_sha256: str
    prediction_context_sha256: str
    prediction_context_kind: str
    model_version: str
    model_role: str
    config_sha256: str
    bake_sha256: str
    run_configuration_sha256: str | None
    engine: str
    engine_mode: str
    component_tested: str
    evidence_profile: str | None
    physical_component_tested: str
    avalanche_regime: str
    mountain_id: str | None
    storm_cycle_id: str | None
    path_id: str | None
    event_id: str
    aoi_coverage_status: str
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
    predicted_mask_sha256: str
    valid_mask_sha256: str
    prediction_artifact_sha256: str
    scenario_documentation_by_observation: tuple[QualitativeScenarioDocumentation, ...]
    historical_scenario_complete: bool
    cell_area_m2: float
    boundary_uncertainty_m_by_observation: tuple[tuple[str, float | None], ...]
    positional_uncertainty_status: str
    positional_uncertainty_confidence_level: float | None
    positional_uncertainty_method: str
    rasterization_method: str
    negative_evidence_used: bool
    unmapped_cells_treated_as_negative: bool
    unmapped_prediction_semantics: str
    mapped_positive_cell_count: int
    mapped_positive_comparable_cell_count: int
    excluded_missing_mapped_positive_cell_count: int
    excluded_uncertain_boundary_cell_count: int
    mapped_positive_model_coverage_fraction: float
    predicted_positive_valid_cell_count: int
    intersecting_mapped_positive_cell_count: int
    predicted_positive_unmapped_cell_count: int
    mapped_positive_coverage_fraction: float
    mapped_positive_area_m2: float
    mapped_positive_comparable_area_m2: float
    intersection_area_m2: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _positive_only_polygon_metrics_impl(
    predicted: Any,
    *,
    valid_mask: Any,
    evaluation_grid: EvaluationGrid,
    prediction_context: (
        PredictionContext | ComponentPredictionContext | QualitativePredictionContext
    ),
    dataset: ValidationDataset,
    partition: Literal["calibration", "holdout", "qualitative"],
    observation_type: PositiveOnlyPolygonObservationType,
    observation_ids: Sequence[str],
    complete_holdout_cohort_verified: bool,
) -> PositiveOnlyPolygonMetrics:
    """Compare a prediction with mapped positives without inventing negatives.

    Use this lower-rigor path only for calibration-only or qualitative evidence
    whose coverage explicitly says that absence is unknown. Calibration still
    requires a complete, registered :class:`PredictionScenario`. Qualitative
    comparison instead requires :class:`QualitativePredictionContext`, which
    binds a run-configuration artifact without fabricating missing event-day
    snow or wind. The metric reports those registered evidence gaps. It cannot
    produce IoU or support an independent field-validation claim.
    """

    _require_partition(dataset, partition)
    v3_field_evidence = (
        _is_v3(dataset)
        and dataset.manifest.scientific_use == "field_validation"
        and partition == "holdout"
    )
    if not v3_field_evidence and dataset.manifest.scientific_use not in {
        "calibration_only",
        "qualitative_comparison",
    }:
        raise ValueError(
            "Positive-only polygon metrics are limited to calibration_only or "
            "qualitative_comparison evidence."
        )
    if (
        dataset.manifest.coverage_semantics == "surveyed_domain"
        or dataset.manifest.absence_semantics != "unknown_unless_explicitly_observed"
    ):
        raise ValueError(
            "Positive-only metrics require evidence that explicitly treats unmapped cells as "
            "unknown, not known absence."
        )
    if _is_v3(dataset) and dataset.manifest.label_state != "positive_unlabelled":
        raise ValueError(
            "This path is for explicit positive/unlabelled evidence; surveyed negatives must "
            "use the strict binary evaluator."
        )
    _require_metric_profile(dataset, observation_type)
    if not isinstance(evaluation_grid, EvaluationGrid):
        raise TypeError("evaluation_grid must be an EvaluationGrid.")
    qualitative_evidence = dataset.manifest.scientific_use == "qualitative_comparison"
    if _is_v3(dataset):
        _require_component_context(dataset, prediction_context)  # type: ignore[arg-type]
    elif qualitative_evidence:
        if not isinstance(prediction_context, QualitativePredictionContext):
            raise TypeError(
                "qualitative_comparison requires a QualitativePredictionContext so missing "
                "historical scenario inputs are not fabricated."
            )
    elif not isinstance(prediction_context, PredictionContext):
        raise TypeError(
            "calibration_only requires a PredictionContext with a complete documented scenario."
        )
    _require_grid_binding(evaluation_grid, dataset)
    _require_context_grid_binding(prediction_context, evaluation_grid)
    _require_scorable_prediction(prediction_context)

    targets = _require_registered_features(
        dataset,
        observation_ids,
        partition=partition,
        observation_type=observation_type,
    )
    if v3_field_evidence and not complete_holdout_cohort_verified:
        _require_complete_field_holdout(
            dataset,
            partition=partition,
            observation_type=observation_type,
            observation_ids=observation_ids,
        )
    if {item.event_id for item in targets} != {prediction_context.event_id}:
        raise ValueError(
            "A positive-only polygon prediction must evaluate one registered event matching "
            "its PredictionContext."
        )
    for item in targets:
        if qualitative_evidence:
            _require_qualitative_context_for_observation(prediction_context, item)
        else:
            _require_context_for_observation(
                prediction_context,
                item,
                require_registered_scenario=not _is_v3(dataset),
            )
    scenario_documentation = tuple(
        _qualitative_scenario_documentation(item) for item in targets
    )

    predicted_array = _require_bool_array(predicted, "predicted")
    valid_array = _require_bool_array(valid_mask, "valid_mask")
    if predicted_array.shape != evaluation_grid.shape or valid_array.shape != evaluation_grid.shape:
        raise ValueError("Prediction and valid-data masks must match EvaluationGrid.shape exactly.")

    target_geometry = _union_geometry(targets)
    grid_geometry = box(
        evaluation_grid.west,
        evaluation_grid.south,
        evaluation_grid.east,
        evaluation_grid.north,
    )
    if not grid_geometry.covers(target_geometry):
        raise ValueError(
            "EvaluationGrid does not fully cover the registered mapped positives; cropping "
            "positive evidence is not permitted."
        )
    observed = _rasterize_cell_centers(target_geometry, evaluation_grid)
    if not observed.any():
        raise ValueError(
            "Registered target geometries contain no EvaluationGrid cell centres; use a finer grid."
        )

    uncertainties = tuple(
        (item.observation_id, _optional_feature_uncertainty_m(dataset, item))
        for item in targets
    )
    boundary_bands = [
        shape(item.geometry).boundary.buffer(distance)
        for item, (_, distance) in zip(targets, uncertainties)
        if distance is not None and distance > 0
    ]
    uncertain = (
        np.zeros(evaluation_grid.shape, dtype=bool)
        if not boundary_bands
        else _rasterize_cell_centers(shapely.union_all(boundary_bands), evaluation_grid)
    )
    certain_positive = observed & ~uncertain
    comparable_positive = certain_positive & valid_array
    certain_count = int(np.count_nonzero(certain_positive))
    comparable_count = int(np.count_nonzero(comparable_positive))
    if certain_count == 0:
        raise ValueError("Positional uncertainty excludes every mapped-positive cell centre.")
    if comparable_count == 0:
        raise ValueError("No mapped-positive cell has complete model inputs.")

    predicted_valid = predicted_array & valid_array
    intersection = predicted_array & comparable_positive
    mapped_count = int(np.count_nonzero(observed))
    intersection_count = int(np.count_nonzero(intersection))
    predicted_valid_count = int(np.count_nonzero(predicted_valid))
    predicted_unmapped_count = int(np.count_nonzero(predicted_valid & ~observed))
    predicted_hash = _bool_mask_sha256(predicted_array)
    valid_hash = _bool_mask_sha256(valid_array)
    prediction_artifact_hash = _canonical_sha256(
        {
            "schema": "avycore-positive-only-polygon-prediction-artifact-v2",
            "dataset_identity_sha256": dataset.dataset_identity_sha256,
            "partition": partition,
            "observation_ids": list(observation_ids),
            "grid_identity_sha256": evaluation_grid.grid_identity_sha256,
            "prediction_context_sha256": prediction_context.context_identity_sha256,
            "predicted_mask_sha256": predicted_hash,
            "valid_mask_sha256": valid_hash,
        }
    )
    manifest_uncertainty = dataset.manifest.positional_uncertainty
    uses_field, eligible, registered, trust_status, independent = _trust_flags(
        dataset,
        partition,
    )
    uses_field = dataset.manifest.evidence_type in {
        "field_observation",
        "authoritative_inventory",
        "reviewed_remote_sensing",
    }
    cell_area_m2 = evaluation_grid.cell_area_m2
    target = targets[0]
    return PositiveOnlyPolygonMetrics(
        dataset_id=dataset.manifest.dataset_id,
        dataset_identity_sha256=dataset.dataset_identity_sha256,
        evidence_use=dataset.manifest.scientific_use,
        partition=partition,
        uses_field_evidence=uses_field,
        contract_eligible_for_independent_holdout_validation=eligible,
        dataset_trust_registered=registered,
        dataset_trust_status=trust_status,
        is_independent_holdout_validation=independent,
        metric_scope=(
            "positive_unlabelled_mapped_positive_coverage"
            if _is_v3(dataset)
            else "mapped_positive_coverage_only"
        ),
        supports_independent_validation_claim=independent,
        observation_type=observation_type,
        observation_ids=tuple(observation_ids),
        grid_crs=evaluation_grid.crs,
        grid_identity_sha256=evaluation_grid.grid_identity_sha256,
        grid_source_artifact_sha256=evaluation_grid.source_artifact_sha256,
        prediction_context_sha256=prediction_context.context_identity_sha256,
        prediction_context_kind=(
            "component_scoped_v3"
            if isinstance(prediction_context, ComponentPredictionContext)
            else
            "qualitative_missingness_aware"
            if qualitative_evidence
            else "complete_documented_scenario"
        ),
        model_version=prediction_context.model_version,
        model_role=(
            prediction_context.model_role
            if isinstance(prediction_context, ComponentPredictionContext)
            else "legacy_unspecified"
        ),
        config_sha256=prediction_context.config_sha256,
        bake_sha256=prediction_context.bake_sha256,
        run_configuration_sha256=(
            prediction_context.run_configuration_sha256
            if isinstance(prediction_context, QualitativePredictionContext)
            else None
        ),
        engine=prediction_context.engine,
        engine_mode=prediction_context.engine_mode,
        component_tested=_metric_component(dataset, prediction_context),  # type: ignore[arg-type]
        evidence_profile=dataset.manifest.evidence_profile,
        physical_component_tested=_physical_component(prediction_context),  # type: ignore[arg-type]
        avalanche_regime=(
            "dry_dense_slab" if _is_v3(dataset) else "legacy_unspecified"
        ),
        mountain_id=target.properties.get("mountain_id"),
        storm_cycle_id=target.properties.get("storm_cycle_id"),
        path_id=target.properties.get("path_id"),
        event_id=prediction_context.event_id,
        aoi_coverage_status=prediction_context.aoi_coverage_status,
        particles_left_the_aoi=prediction_context.particles_left_the_aoi,
        aoi_boundary_contact=prediction_context.aoi_boundary_contact,
        predicted_mask_sha256=predicted_hash,
        valid_mask_sha256=valid_hash,
        prediction_artifact_sha256=prediction_artifact_hash,
        scenario_documentation_by_observation=scenario_documentation,
        historical_scenario_complete=all(
            not item.missing_fields and item.status == "documented"
            for item in scenario_documentation
        ),
        cell_area_m2=cell_area_m2,
        boundary_uncertainty_m_by_observation=uncertainties,
        positional_uncertainty_status=manifest_uncertainty.status,
        positional_uncertainty_confidence_level=manifest_uncertainty.confidence_level,
        positional_uncertainty_method=manifest_uncertainty.method,
        rasterization_method=(
            "Shapely 2 vectorized intersects_xy at north-up square-grid cell centres; "
            "polygon boundaries are included."
        ),
        negative_evidence_used=False,
        unmapped_cells_treated_as_negative=False,
        unmapped_prediction_semantics=(
            "Predicted cells outside mapped positives are unscored because the source supplies "
            "no surveyed known-absence domain."
        ),
        mapped_positive_cell_count=mapped_count,
        mapped_positive_comparable_cell_count=comparable_count,
        excluded_missing_mapped_positive_cell_count=int(
            np.count_nonzero(certain_positive & ~valid_array)
        ),
        excluded_uncertain_boundary_cell_count=int(np.count_nonzero(observed & uncertain)),
        mapped_positive_model_coverage_fraction=float(comparable_count / certain_count),
        predicted_positive_valid_cell_count=predicted_valid_count,
        intersecting_mapped_positive_cell_count=intersection_count,
        predicted_positive_unmapped_cell_count=predicted_unmapped_count,
        mapped_positive_coverage_fraction=float(intersection_count / comparable_count),
        mapped_positive_area_m2=mapped_count * cell_area_m2,
        mapped_positive_comparable_area_m2=comparable_count * cell_area_m2,
        intersection_area_m2=intersection_count * cell_area_m2,
    )


def positive_only_polygon_metrics(
    predicted: Any,
    *,
    valid_mask: Any,
    evaluation_grid: EvaluationGrid,
    prediction_context: (
        PredictionContext | ComponentPredictionContext | QualitativePredictionContext
    ),
    dataset: ValidationDataset,
    partition: Literal["calibration", "holdout", "qualitative"],
    observation_type: PositiveOnlyPolygonObservationType,
    observation_ids: Sequence[str],
) -> PositiveOnlyPolygonMetrics:
    """Evaluate one explicit positive/unlabelled target without inventing negatives."""

    return _positive_only_polygon_metrics_impl(
        predicted,
        valid_mask=valid_mask,
        evaluation_grid=evaluation_grid,
        prediction_context=prediction_context,
        dataset=dataset,
        partition=partition,
        observation_type=observation_type,
        observation_ids=observation_ids,
        complete_holdout_cohort_verified=False,
    )


@dataclass(frozen=True)
class PositiveOnlyPolygonEvaluationCase:
    predicted: Any
    valid_mask: Any
    evaluation_grid: EvaluationGrid
    prediction_context: ComponentPredictionContext
    observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.observation_ids, tuple) or not self.observation_ids:
            raise TypeError("observation_ids must be a non-empty tuple.")


@dataclass(frozen=True)
class PositiveOnlyPolygonCohortMetrics:
    dataset_id: str
    dataset_identity_sha256: str
    partition: str
    observation_type: str
    component_tested: str
    evidence_profile: str
    complete_registered_target_cohort: bool
    complete_cohort_event_count: int
    independent_holdout_event_count: int
    event_ids: tuple[str, ...]
    model_version: str
    model_role: str
    engine: str
    engine_mode: str
    physical_component_tested: str
    dataset_trust_status: str
    is_independent_holdout_validation: bool
    negative_evidence_used: Literal[False]
    metric_aggregation: str
    prediction_set_sha256: str
    event_metrics: tuple[PositiveOnlyPolygonMetrics, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def positive_only_polygon_cohort_metrics(
    cases: Sequence[PositiveOnlyPolygonEvaluationCase],
    *,
    dataset: ValidationDataset,
    partition: Literal["holdout"],
    observation_type: PositiveOnlyPolygonObservationType,
) -> PositiveOnlyPolygonCohortMetrics:
    """Score every event in a v3 positive/unlabelled holdout, per event only."""

    _require_partition(dataset, partition)
    if (
        not _is_v3(dataset)
        or dataset.manifest.scientific_use != "field_validation"
        or dataset.manifest.label_state != "positive_unlabelled"
    ):
        raise ValueError(
            "Positive-only holdout cohorts require v3 field_validation evidence with "
            "label_state='positive_unlabelled'."
        )
    _require_metric_profile(dataset, observation_type)
    if not cases or not all(
        isinstance(case, PositiveOnlyPolygonEvaluationCase) for case in cases
    ):
        raise TypeError(
            "cases must contain at least one PositiveOnlyPolygonEvaluationCase."
        )
    expected_by_event: dict[str, set[str]] = {}
    for item in dataset.observations:
        if item.partition == partition and item.observation_type == observation_type:
            expected_by_event.setdefault(item.event_id, set()).add(item.observation_id)
    if not expected_by_event:
        raise ValueError("The requested positive/unlabelled holdout target is not registered.")
    cases_by_event: dict[str, PositiveOnlyPolygonEvaluationCase] = {}
    for case in cases:
        event_id = case.prediction_context.event_id
        if event_id in cases_by_event:
            raise ValueError(f"Cohort requires exactly one prediction for {event_id!r}.")
        cases_by_event[event_id] = case
    if set(cases_by_event) != set(expected_by_event):
        missing = sorted(set(expected_by_event) - set(cases_by_event))
        unexpected = sorted(set(cases_by_event) - set(expected_by_event))
        raise ValueError(
            "Positive/unlabelled holdout metrics require every registered holdout event; "
            f"missing={missing}, unexpected={unexpected}."
        )
    comparable_identity = {
        (
            case.prediction_context.model_version,
            case.prediction_context.config_sha256,
            case.prediction_context.model_role,
            case.prediction_context.engine,
            case.prediction_context.engine_mode,
            case.prediction_context.component_tested,
        )
        for case in cases
    }
    if len(comparable_identity) != 1:
        raise ValueError(
            "Every cohort event must use the same model, role, config, engine, mode, and "
            "validation component."
        )
    event_metrics: list[PositiveOnlyPolygonMetrics] = []
    for event_id in sorted(expected_by_event):
        case = cases_by_event[event_id]
        supplied = set(case.observation_ids)
        expected = expected_by_event[event_id]
        if len(supplied) != len(case.observation_ids) or supplied != expected:
            raise ValueError(
                f"Event {event_id!r} requires every registered target; "
                f"omitted={sorted(expected - supplied)}, "
                f"unexpected={sorted(supplied - expected)}."
            )
        event_metrics.append(
            _positive_only_polygon_metrics_impl(
                case.predicted,
                valid_mask=case.valid_mask,
                evaluation_grid=case.evaluation_grid,
                prediction_context=case.prediction_context,
                dataset=dataset,
                partition=partition,
                observation_type=observation_type,
                observation_ids=tuple(sorted(case.observation_ids)),
                complete_holdout_cohort_verified=True,
            )
        )
    first = event_metrics[0]
    _uses_field, _eligible, _registered, trust_status, independent = _trust_flags(
        dataset,
        partition,
    )
    prediction_set_sha256 = _canonical_sha256(
        {
            "schema": "avycore-positive-unlabelled-holdout-cohort-v1",
            "dataset_identity_sha256": dataset.dataset_identity_sha256,
            "partition": partition,
            "observation_type": observation_type,
            "event_predictions": [
                {
                    "event_id": event_id,
                    "prediction_artifact_sha256": metric.prediction_artifact_sha256,
                }
                for event_id, metric in zip(sorted(expected_by_event), event_metrics)
            ],
        }
    )
    return PositiveOnlyPolygonCohortMetrics(
        dataset_id=dataset.manifest.dataset_id,
        dataset_identity_sha256=dataset.dataset_identity_sha256,
        partition=partition,
        observation_type=observation_type,
        component_tested=first.component_tested,
        evidence_profile=first.evidence_profile or "",
        complete_registered_target_cohort=True,
        complete_cohort_event_count=len(event_metrics),
        independent_holdout_event_count=len(event_metrics) if independent else 0,
        event_ids=tuple(sorted(expected_by_event)),
        model_version=first.model_version,
        model_role=first.model_role,
        engine=first.engine,
        engine_mode=first.engine_mode,
        physical_component_tested=first.physical_component_tested,
        dataset_trust_status=trust_status,
        is_independent_holdout_validation=independent,
        negative_evidence_used=False,
        metric_aggregation="per_event_only_no_pooled_overlap",
        prediction_set_sha256=prediction_set_sha256,
        event_metrics=tuple(event_metrics),
    )


@dataclass(frozen=True)
class MappedPositiveEventScore:
    """One mapped avalanche scored without treating surrounding terrain as absence."""

    event_id: str
    mapped_positive_cell_count: int
    intersecting_predicted_cell_count: int
    overlap_fraction: float
    geometry_complete_within_evaluation_domain: bool
    complete_model_inputs: bool
    captured: bool


@dataclass(frozen=True)
class StormWindowPositiveMetrics:
    """Positive-only event capture and terrain-budget diagnostics.

    ``eligible`` is the complete, predeclared terrain domain. Predictions outside
    it are reported but never converted to a safe-looking zero or silently added
    to the denominator. Event masks are mapped positives only; this result
    intentionally contains no precision, specificity, false-positive, F1, IoU,
    or probability-calibration fields.
    """

    metric_schema: str
    capture_minimum_overlap_fraction: float
    cell_area_m2: float
    negative_evidence_used: bool
    unmapped_cells_treated_as_negative: bool
    unmapped_prediction_semantics: str
    eligible_terrain_cell_count: int
    predicted_eligible_cell_count: int
    predicted_outside_eligible_cell_count: int
    mapped_positive_union_cell_count: int
    mapped_positive_missing_input_cell_count: int
    intersecting_mapped_positive_cell_count: int
    event_count: int
    captured_event_count: int
    incomplete_geometry_event_count: int
    incomplete_input_event_count: int
    event_capture_fraction: float
    mapped_positive_footprint_coverage_fraction: float
    flagged_eligible_terrain_fraction: float
    predicted_to_mapped_area_ratio: float
    eligible_terrain_area_m2: float
    predicted_area_m2: float
    mapped_positive_area_m2: float
    intersection_area_m2: float
    predicted_mask_sha256: str
    eligible_mask_sha256: str
    event_scores: tuple[MappedPositiveEventScore, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def storm_window_positive_metrics(
    predicted: Any,
    *,
    eligible: Any,
    event_masks: Sequence[Any],
    event_ids: Sequence[str],
    geometry_complete: Sequence[bool],
    capture_minimum_overlap_fraction: float,
    cell_area_m2: float,
) -> StormWindowPositiveMetrics:
    """Score a frozen storm-window footprint against mapped-positive outlines.

    Every supplied event remains in the denominator. An outline that crosses the
    fixed evaluation boundary, or intersects an incomplete-input cell, is an
    explicit uncaptured event rather than an exclusion. ``event_masks`` must
    already be rasterized on the prediction grid by a caller that preserves the
    source CRS and cell-centre convention.
    """

    predicted_array = _require_bool_array(predicted, "predicted")
    eligible_array = _require_bool_array(eligible, "eligible")
    if predicted_array.shape != eligible_array.shape:
        raise ValueError("predicted and eligible must have identical shapes.")
    if not event_masks:
        raise ValueError("event_masks must contain at least one mapped positive.")
    if not (
        len(event_masks) == len(event_ids) == len(geometry_complete)
    ):
        raise ValueError(
            "event_masks, event_ids, and geometry_complete must have equal lengths."
        )
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("event_ids must be unique.")
    threshold = _require_finite_number(
        capture_minimum_overlap_fraction,
        "capture_minimum_overlap_fraction",
    )
    if not 0.0 < threshold <= 1.0:
        raise ValueError("capture_minimum_overlap_fraction must be in (0, 1].")
    area = _require_finite_number(cell_area_m2, "cell_area_m2")
    if area <= 0.0:
        raise ValueError("cell_area_m2 must be positive.")
    if not all(isinstance(value, bool) for value in geometry_complete):
        raise TypeError("geometry_complete must contain booleans.")

    event_arrays: list[np.ndarray] = []
    for index, value in enumerate(event_masks):
        event = _require_bool_array(value, f"event_masks[{index}]")
        if event.shape != predicted_array.shape:
            raise ValueError("Every event mask must match the prediction shape.")
        if not event.any():
            raise ValueError(f"Event {event_ids[index]!r} contains no grid-cell centres.")
        event_arrays.append(event)

    predicted_eligible = predicted_array & eligible_array
    mapped_union = np.logical_or.reduce(event_arrays)
    intersection = predicted_eligible & mapped_union
    scores: list[MappedPositiveEventScore] = []
    for event_id, event, geometry_is_complete in zip(
        event_ids, event_arrays, geometry_complete
    ):
        mapped_count = int(np.count_nonzero(event))
        intersecting = int(np.count_nonzero(event & predicted_eligible))
        overlap = float(intersecting / mapped_count)
        input_complete = not bool(np.any(event & ~eligible_array))
        captured = bool(
            geometry_is_complete and input_complete and overlap >= threshold
        )
        scores.append(
            MappedPositiveEventScore(
                event_id=_require_nonempty_text(event_id, "event_id"),
                mapped_positive_cell_count=mapped_count,
                intersecting_predicted_cell_count=intersecting,
                overlap_fraction=overlap,
                geometry_complete_within_evaluation_domain=geometry_is_complete,
                complete_model_inputs=input_complete,
                captured=captured,
            )
        )

    eligible_count = int(np.count_nonzero(eligible_array))
    if eligible_count == 0:
        raise ValueError("eligible contains no complete terrain cells.")
    mapped_count = int(np.count_nonzero(mapped_union))
    if mapped_count == 0:  # defensive; individual masks are already non-empty
        raise ValueError("Mapped-positive union contains no grid-cell centres.")
    predicted_count = int(np.count_nonzero(predicted_eligible))
    intersection_count = int(np.count_nonzero(intersection))
    captured_count = sum(score.captured for score in scores)
    return StormWindowPositiveMetrics(
        metric_schema="avycore-storm-window-positive-metrics-v1",
        capture_minimum_overlap_fraction=threshold,
        cell_area_m2=area,
        negative_evidence_used=False,
        unmapped_cells_treated_as_negative=False,
        unmapped_prediction_semantics=(
            "Predicted cells outside mapped outlines are used only for the predeclared "
            "eligible-terrain area budget; they are not labelled false positives."
        ),
        eligible_terrain_cell_count=eligible_count,
        predicted_eligible_cell_count=predicted_count,
        predicted_outside_eligible_cell_count=int(
            np.count_nonzero(predicted_array & ~eligible_array)
        ),
        mapped_positive_union_cell_count=mapped_count,
        mapped_positive_missing_input_cell_count=int(
            np.count_nonzero(mapped_union & ~eligible_array)
        ),
        intersecting_mapped_positive_cell_count=intersection_count,
        event_count=len(scores),
        captured_event_count=captured_count,
        incomplete_geometry_event_count=sum(
            not score.geometry_complete_within_evaluation_domain for score in scores
        ),
        incomplete_input_event_count=sum(
            not score.complete_model_inputs for score in scores
        ),
        event_capture_fraction=float(captured_count / len(scores)),
        mapped_positive_footprint_coverage_fraction=float(
            intersection_count / mapped_count
        ),
        flagged_eligible_terrain_fraction=float(predicted_count / eligible_count),
        predicted_to_mapped_area_ratio=float(predicted_count / mapped_count),
        eligible_terrain_area_m2=eligible_count * area,
        predicted_area_m2=predicted_count * area,
        mapped_positive_area_m2=mapped_count * area,
        intersection_area_m2=intersection_count * area,
        predicted_mask_sha256=_bool_mask_sha256(predicted_array),
        eligible_mask_sha256=_bool_mask_sha256(eligible_array),
        event_scores=tuple(scores),
    )


@dataclass(frozen=True)
class MountainBlockBootstrapInterval:
    """A deterministic percentile interval from whole-mountain resampling."""

    estimate: float
    lower: float
    upper: float
    confidence_level: float
    replicate_count: int
    random_seed: int
    resampling_unit: str = "mountain_block"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def mountain_block_bootstrap_interval(
    numerators: Sequence[int | float],
    denominators: Sequence[int | float],
    *,
    confidence_level: float = 0.95,
    replicate_count: int = 10_000,
    random_seed: int = 20260713,
) -> MountainBlockBootstrapInterval:
    """Bootstrap a pooled ratio while resampling complete mountain blocks."""

    numerator = np.asarray(numerators, dtype="float64")
    denominator = np.asarray(denominators, dtype="float64")
    if numerator.ndim != 1 or denominator.ndim != 1 or numerator.shape != denominator.shape:
        raise ValueError("numerators and denominators must be aligned one-dimensional sequences.")
    if numerator.size == 0:
        raise ValueError("At least one mountain block is required.")
    if not np.isfinite(numerator).all() or not np.isfinite(denominator).all():
        raise ValueError("Bootstrap inputs must be finite.")
    if np.any(numerator < 0.0) or np.any(denominator <= 0.0):
        raise ValueError("Numerators must be non-negative and denominators positive.")
    if np.any(numerator > denominator):
        raise ValueError("A block numerator cannot exceed its denominator.")
    confidence = _require_finite_number(confidence_level, "confidence_level")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence_level must be in (0, 1).")
    if isinstance(replicate_count, bool) or not isinstance(replicate_count, int):
        raise TypeError("replicate_count must be an integer.")
    if replicate_count < 100:
        raise ValueError("replicate_count must be at least 100.")
    if isinstance(random_seed, bool) or not isinstance(random_seed, int):
        raise TypeError("random_seed must be an integer.")

    rng = np.random.default_rng(random_seed)
    indices = rng.integers(0, numerator.size, size=(replicate_count, numerator.size))
    sampled_numerator = numerator[indices].sum(axis=1)
    sampled_denominator = denominator[indices].sum(axis=1)
    ratios = sampled_numerator / sampled_denominator
    tail = (1.0 - confidence) / 2.0
    return MountainBlockBootstrapInterval(
        estimate=float(numerator.sum() / denominator.sum()),
        lower=float(np.quantile(ratios, tail)),
        upper=float(np.quantile(ratios, 1.0 - tail)),
        confidence_level=confidence,
        replicate_count=replicate_count,
        random_seed=random_seed,
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
    model_version: str
    model_role: str
    engine: str
    engine_mode: str
    component_tested: str
    evidence_profile: str | None
    physical_component_tested: str
    avalanche_regime: str
    event_ids: tuple[str, ...]
    mountain_ids: tuple[str, ...]
    storm_cycle_ids: tuple[str, ...]
    path_ids: tuple[str, ...]
    aoi_coverage_status: str
    particles_left_the_aoi: int
    aoi_boundary_contact: bool
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
    context: PredictionEvidenceContext,
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
    prediction_contexts: Sequence[PredictionEvidenceContext],
    evaluation_grid: EvaluationGrid,
    observation_ids: Sequence[str],
    dataset: ValidationDataset,
    partition: EvaluationPartition,
) -> EndpointMetrics:
    """Evaluate a complete, ID-bound endpoint cohort with per-run provenance."""

    _require_partition(dataset, partition)
    _require_quantitative_dataset(dataset)
    _require_metric_profile(dataset, "runout_endpoint")
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
    if len(prediction_contexts) != len(observations):
        raise ValueError("prediction_contexts must contain one context per observation.")
    for context, observation in zip(prediction_contexts, observations):
        _require_component_context(dataset, context)
        _require_context_grid_binding(context, evaluation_grid)
        _require_scorable_prediction(context)
        _require_context_for_observation(
            context,
            observation,
            require_registered_scenario=dataset.manifest.scientific_use
            in {"field_validation", "calibration_only"}
            and not _is_v3(dataset),
        )
    comparable_identity = {
        (
            context.model_version,
            context.config_sha256,
            context.bake_sha256,
            context.engine,
            context.engine_mode,
            (
                context.model_role
                if isinstance(context, ComponentPredictionContext)
                else "legacy_unspecified"
            ),
        )
        for context in prediction_contexts
    }
    if len(comparable_identity) != 1:
        raise ValueError(
            "Every endpoint in a cohort must use the same model, config, bake, engine, and "
            "engine mode."
        )

    predicted = np.asarray(predicted_xy_m, dtype="float64")
    valid = _require_bool_array(predicted_valid, "predicted_valid", ndim=1)
    if predicted.ndim != 2 or predicted.shape != (len(observations), 2):
        raise ValueError(
            "predicted_xy_m must contain one (easting, northing) pair per observation ID."
        )
    if valid.shape != (len(observations),):
        raise ValueError("predicted_valid must contain one value per requested observation ID.")
    if (
        dataset.manifest.scientific_use == "field_validation"
        and partition == "holdout"
        and not bool(np.all(valid))
    ):
        missing_ids = [
            observation_id
            for observation_id, include in zip(observation_ids, valid)
            if not include
        ]
        raise ValueError(
            "Independent field holdout endpoint metrics require a valid prediction for every "
            f"registered endpoint; missing predictions={missing_ids}. A failed or truncated "
            "run must be reported as a failure, not dropped from the holdout score."
        )
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
        model_version=prediction_contexts[0].model_version,
        model_role=(
            prediction_contexts[0].model_role
            if isinstance(prediction_contexts[0], ComponentPredictionContext)
            else "legacy_unspecified"
        ),
        engine=prediction_contexts[0].engine,
        engine_mode=prediction_contexts[0].engine_mode,
        component_tested=_metric_component(dataset, prediction_contexts[0]),
        evidence_profile=dataset.manifest.evidence_profile,
        physical_component_tested=_physical_component(prediction_contexts[0]),
        avalanche_regime=(
            "dry_dense_slab" if _is_v3(dataset) else "legacy_unspecified"
        ),
        event_ids=tuple(item.event_id for item in observations),
        mountain_ids=tuple(
            sorted(
                {
                    item.properties["mountain_id"]
                    for item in observations
                    if "mountain_id" in item.properties
                }
            )
        ),
        storm_cycle_ids=tuple(
            sorted(
                {
                    item.properties["storm_cycle_id"]
                    for item in observations
                    if "storm_cycle_id" in item.properties
                }
            )
        ),
        path_ids=tuple(
            sorted(
                {
                    item.properties["path_id"]
                    for item in observations
                    if "path_id" in item.properties
                }
            )
        ),
        aoi_coverage_status="complete",
        particles_left_the_aoi=0,
        aoi_boundary_contact=False,
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
