"""Validated ingestion contract for independent avalanche observations.

The loader intentionally performs no CRS transformation and no interpretation of
imagery. A provider must normalize geometries into one declared projected CRS
with metre units before ingestion, record lineage and uncertainty, and label the
permissible scientific use. This keeps silent coordinate/unit mistakes and
calibration/holdout leakage out of later metrics.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import shape

from .status import VALIDATION_CONTRACT_VERSION

ObservationType = Literal[
    "release_polygon",
    "deposit_polygon",
    "runout_endpoint",
    "survey_coverage_polygon",
]
EvidenceType = Literal[
    "field_observation",
    "authoritative_inventory",
    "remote_sensing_interpretation",
    "synthetic",
    "model_output",
]
ScientificUse = Literal[
    "field_validation",
    "calibration_only",
    "qualitative_comparison",
    "software_verification",
    "excluded",
]
Partition = Literal["calibration", "holdout", "qualitative", "verification", "excluded"]
ObservationMethodClass = Literal[
    "ground_survey",
    "professional_field_mapping",
    "authoritative_occurrence_record",
    "remote_sensing_interpretation",
    "synthetic_fixture",
    "model_output",
]


class ValidationContractError(ValueError):
    """Raised when validation evidence is incomplete, ambiguous, or inconsistent."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceMetadata(StrictModel):
    provider: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    source_uri: str | None = None
    licence: str = Field(min_length=1)
    permitted_use: str = Field(
        min_length=1,
        description="Human-readable licence/permission statement for scientific use.",
    )


class AcquisitionMetadata(StrictModel):
    status: Literal["known", "bounded", "unknown"]
    start_date: date | None = None
    end_date: date | None = None
    temporal_precision: Literal["instant", "day", "month", "season", "year", "unknown"]
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> "AcquisitionMetadata":
        if self.status == "unknown":
            if self.start_date is not None or self.end_date is not None:
                raise ValueError("Unknown acquisition dates must not carry invented date bounds.")
            if self.temporal_precision != "unknown":
                raise ValueError("Unknown acquisition dates require temporal_precision='unknown'.")
            return self
        if self.start_date is None:
            raise ValueError("Known or bounded acquisition dates require start_date.")
        if self.temporal_precision == "unknown":
            raise ValueError("Known or bounded acquisition dates require a temporal precision.")
        end = self.end_date or self.start_date
        if end < self.start_date:
            raise ValueError("Acquisition end_date must be on or after start_date.")
        if self.status == "known" and end != self.start_date:
            raise ValueError("A date range must use acquisition status='bounded'.")
        return self


class PositionalUncertainty(StrictModel):
    status: Literal["quantified", "unknown"]
    horizontal_m: float | None = Field(default=None, ge=0)
    confidence_level: float | None = Field(default=None, gt=0, le=1)
    method: str = Field(min_length=1)

    @field_validator("horizontal_m", "confidence_level")
    @classmethod
    def require_finite_values(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Uncertainty values must be finite when quantified.")
        return value

    @model_validator(mode="after")
    def validate_quantification(self) -> "PositionalUncertainty":
        if self.status == "quantified" and self.horizontal_m is None:
            raise ValueError("Quantified positional uncertainty requires horizontal_m.")
        if self.status == "unknown" and (
            self.horizontal_m is not None or self.confidence_level is not None
        ):
            raise ValueError("Unknown positional uncertainty must not carry numeric bounds.")
        return self


class SpatialCoverage(StrictModel):
    west: float
    south: float
    east: float
    north: float
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "SpatialCoverage":
        values = (self.west, self.south, self.east, self.north)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Spatial coverage bounds must be finite.")
        if self.east <= self.west or self.north <= self.south:
            raise ValueError("Spatial coverage bounds must have positive width and height.")
        return self


class ValidationDatasetManifest(StrictModel):
    schema_version: Literal[VALIDATION_CONTRACT_VERSION]
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    title: str = Field(min_length=1)
    source: SourceMetadata
    acquisition: AcquisitionMetadata
    evidence_type: EvidenceType
    scientific_use: ScientificUse
    independent_of_model: bool
    observation_types: tuple[ObservationType, ...] = Field(min_length=1)
    original_crs: str = Field(
        min_length=1,
        description="CRS of the immutable source evidence before normalization.",
    )
    crs: Literal["EPSG:26911"] = Field(
        description="Normalized Mount Hosmer analysis CRS (NAD83 / UTM zone 11N)."
    )
    horizontal_units: Literal["metre"]
    axis_order: Literal["easting_northing"]
    coordinate_dimensions: Literal[2]
    normalization_type: Literal["identity", "coordinate_operation"]
    normalization_method: str = Field(min_length=1)
    normalization_software: str = Field(min_length=1)
    original_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spatial_coverage: SpatialCoverage
    coverage_semantics: Literal["surveyed_domain", "positive_observations_only", "unknown"]
    survey_completeness: Literal["complete_for_declared_target", "incomplete", "unknown"]
    detection_limitations: str = Field(min_length=1)
    absence_semantics: Literal[
        "surveyed_domain_supports_known_absence",
        "unknown_unless_explicitly_observed",
    ]
    positional_uncertainty: PositionalUncertainty
    observations_file: str = Field(min_length=1)
    observations_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_scientific_use(self) -> "ValidationDatasetManifest":
        trusted = {"field_observation", "authoritative_inventory"}
        if self.scientific_use in {"field_validation", "calibration_only"}:
            if self.evidence_type not in trusted:
                raise ValueError(
                    "Field validation/calibration requires field observations or an authoritative "
                    "inventory; imagery interpretation, synthetic data, and model output are not "
                    "ground truth."
                )
            if not self.independent_of_model:
                raise ValueError("Validation/calibration evidence must be independent of the model.")
        if self.evidence_type == "synthetic" and self.scientific_use != "software_verification":
            raise ValueError("Synthetic evidence is permissible only for software verification.")
        if self.evidence_type == "model_output" and self.scientific_use != "excluded":
            raise ValueError("Model output cannot be used as independent validation evidence.")
        if (
            self.evidence_type == "remote_sensing_interpretation"
            and self.scientific_use not in {"qualitative_comparison", "excluded"}
        ):
            raise ValueError("Imagery interpretation may be qualitative context, not ground truth.")
        if self.scientific_use in {"field_validation", "calibration_only"} and (
            self.acquisition.status == "unknown"
        ):
            raise ValueError(
                "Field calibration/validation requires a known or bounded acquisition period."
            )
        if self.coverage_semantics == "surveyed_domain":
            if "survey_coverage_polygon" not in self.observation_types:
                raise ValueError(
                    "surveyed_domain coverage requires survey_coverage_polygon observations."
                )
            if self.absence_semantics != "surveyed_domain_supports_known_absence":
                raise ValueError(
                    "surveyed_domain coverage must explicitly state that the surveyed domain "
                    "supports known absence."
                )
            if self.survey_completeness != "complete_for_declared_target":
                raise ValueError(
                    "Known-absence metrics require a survey complete for the declared target."
                )
        elif self.absence_semantics != "unknown_unless_explicitly_observed":
            raise ValueError(
                "Positive-only or unknown coverage cannot claim known avalanche absence."
            )
        elif self.survey_completeness == "complete_for_declared_target":
            raise ValueError(
                "Positive-only or unknown coverage cannot claim a complete survey domain."
            )
        same_crs = self.original_crs.strip().upper() == self.crs
        if same_crs and self.normalization_type != "identity":
            raise ValueError("Matching original/normalized CRS requires identity normalization.")
        if not same_crs and self.normalization_type != "coordinate_operation":
            raise ValueError("Different original/normalized CRS requires a coordinate operation.")
        if not same_crs and self.normalization_software.strip().lower() in {"none", "n/a"}:
            raise ValueError("A coordinate operation must identify the normalization software.")
        return self


@dataclass(frozen=True)
class NormalizedObservation:
    observation_id: str
    event_id: str
    observation_type: str
    partition: str
    geometry: Mapping[str, Any]
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class ValidationDataset:
    manifest: ValidationDatasetManifest
    observations: tuple[NormalizedObservation, ...]
    manifest_path: Path
    observations_path: Path
    partition_counts: Mapping[str, int]
    manifest_sha256: str
    dataset_identity_sha256: str


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_json_value(value: Any) -> Any:
    """Return immutable contract values in a stable JSON-compatible form."""

    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_observations_path(manifest_path: Path, relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute():
        raise ValidationContractError("observations_file must be relative to the manifest.")
    root = manifest_path.parent.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValidationContractError("observations_file escapes the dataset directory.") from exc
    return candidate


def _expected_partition(scientific_use: str) -> set[str]:
    return {
        "field_validation": {"calibration", "holdout"},
        "calibration_only": {"calibration"},
        "qualitative_comparison": {"qualitative"},
        "software_verification": {"verification"},
        "excluded": {"excluded"},
    }[scientific_use]


def _validate_feature(
    raw: Any,
    *,
    index: int,
    manifest: ValidationDatasetManifest,
) -> NormalizedObservation:
    if not isinstance(raw, dict) or raw.get("type") != "Feature":
        raise ValidationContractError(f"Feature {index} is not a GeoJSON Feature.")
    geometry = raw.get("geometry")
    properties = raw.get("properties")
    if not isinstance(geometry, dict) or not isinstance(properties, dict):
        raise ValidationContractError(f"Feature {index} requires geometry and object properties.")

    required = (
        "observation_id",
        "event_id",
        "source_feature_id",
        "observation_type",
        "partition",
        "observation_method",
        "observation_method_class",
        "verification_status",
        "event_date_status",
        "scenario_status",
    )
    missing = [name for name in required if not isinstance(properties.get(name), str) or not properties[name]]
    if missing:
        raise ValidationContractError(f"Feature {index} has missing/invalid properties: {missing}.")
    observation_type = properties["observation_type"]
    partition = properties["partition"]
    if observation_type not in manifest.observation_types:
        raise ValidationContractError(
            f"Feature {index} observation_type {observation_type!r} is not declared by the manifest."
        )
    allowed_partitions = _expected_partition(manifest.scientific_use)
    if partition not in allowed_partitions:
        raise ValidationContractError(
            f"Feature {index} partition {partition!r} is inconsistent with scientific_use "
            f"{manifest.scientific_use!r}; expected one of {sorted(allowed_partitions)}."
        )
    verification_status = properties["verification_status"]
    allowed_verification = {
        "field_verified",
        "professionally_verified",
        "unverified",
        "synthetic",
    }
    if verification_status not in allowed_verification:
        raise ValidationContractError(
            f"Feature {index} has unknown verification_status {verification_status!r}."
        )
    if manifest.scientific_use in {"field_validation", "calibration_only"} and (
        verification_status not in {"field_verified", "professionally_verified"}
    ):
        raise ValidationContractError(
            f"Feature {index} is not field/professionally verified and cannot support "
            f"{manifest.scientific_use}."
        )
    if manifest.evidence_type == "synthetic" and verification_status != "synthetic":
        raise ValidationContractError(
            f"Feature {index} from a synthetic dataset must be labelled synthetic."
        )
    method_class = properties["observation_method_class"]
    allowed_method_classes = {
        "ground_survey",
        "professional_field_mapping",
        "authoritative_occurrence_record",
        "remote_sensing_interpretation",
        "synthetic_fixture",
        "model_output",
    }
    if method_class not in allowed_method_classes:
        raise ValidationContractError(
            f"Feature {index} has unknown observation_method_class {method_class!r}."
        )
    quantitative_methods = {
        "ground_survey",
        "professional_field_mapping",
        "authoritative_occurrence_record",
    }
    if manifest.scientific_use in {"field_validation", "calibration_only"} and (
        method_class not in quantitative_methods
    ):
        raise ValidationContractError(
            f"Feature {index} method class {method_class!r} cannot support quantitative field "
            "calibration or validation."
        )
    evidence_method = {
        "remote_sensing_interpretation": "remote_sensing_interpretation",
        "synthetic": "synthetic_fixture",
        "model_output": "model_output",
    }.get(manifest.evidence_type)
    if evidence_method is not None and method_class != evidence_method:
        raise ValidationContractError(
            f"Feature {index} method class {method_class!r} conflicts with manifest evidence "
            f"type {manifest.evidence_type!r}."
        )
    event_date_status = properties["event_date_status"]
    if event_date_status not in {"known", "bounded", "unknown"}:
        raise ValidationContractError(
            f"Feature {index} event_date_status must be 'known', 'bounded', or 'unknown'."
        )
    raw_event_start = properties.get("event_start_date")
    raw_event_end = properties.get("event_end_date")
    if event_date_status == "unknown":
        if raw_event_start is not None or raw_event_end is not None:
            raise ValidationContractError(
                f"Feature {index} with an unknown event date must not carry invented date bounds."
            )
    else:
        if not isinstance(raw_event_start, str) or not isinstance(raw_event_end, str):
            raise ValidationContractError(
                f"Feature {index} known/bounded event dates require ISO 8601 start and end dates."
            )
        try:
            event_start = date.fromisoformat(raw_event_start)
            event_end = date.fromisoformat(raw_event_end)
        except ValueError as exc:
            raise ValidationContractError(
                f"Feature {index} event dates must be ISO 8601 calendar dates."
            ) from exc
        if event_end < event_start:
            raise ValidationContractError(
                f"Feature {index} event_end_date precedes event_start_date."
            )
        if event_date_status == "known" and event_end != event_start:
            raise ValidationContractError(
                f"Feature {index} date ranges require event_date_status='bounded'."
            )
        if event_date_status == "bounded" and event_end == event_start:
            raise ValidationContractError(
                f"Feature {index} single-day events require event_date_status='known'."
            )
    scenario_status = properties["scenario_status"]
    if scenario_status not in {"documented", "unknown"}:
        raise ValidationContractError(
            f"Feature {index} scenario_status must be 'documented' or 'unknown'."
        )
    scenario = properties.get("scenario_inputs")
    if scenario_status == "documented":
        if not isinstance(scenario, dict):
            raise ValidationContractError(
                f"Feature {index} documented scenario requires scenario_inputs."
            )
        numeric_fields = ("new_snow_cm", "wind_speed_kmh", "wind_direction_deg")
        for field_name in numeric_fields:
            value = scenario.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationContractError(
                    f"Feature {index} scenario_inputs.{field_name} must be finite."
                )
        if scenario["new_snow_cm"] < 0 or scenario["wind_speed_kmh"] < 0:
            raise ValidationContractError(
                f"Feature {index} scenario snow and wind speed cannot be negative."
            )
        if not 0 <= scenario["wind_direction_deg"] < 360:
            raise ValidationContractError(
                f"Feature {index} scenario wind direction must be in [0, 360) degrees using "
                "the meteorological wind-from convention."
            )
        if scenario.get("release_size") not in {"small", "medium", "large", "very_large"}:
            raise ValidationContractError(
                f"Feature {index} scenario_inputs.release_size is invalid."
            )
        if not isinstance(scenario.get("source"), str) or not scenario["source"].strip():
            raise ValidationContractError(
                f"Feature {index} documented scenario requires a source statement."
            )
        if (
            not isinstance(scenario.get("uncertainty_statement"), str)
            or not scenario["uncertainty_statement"].strip()
        ):
            raise ValidationContractError(
                f"Feature {index} documented scenario requires an uncertainty statement."
            )
    elif scenario is not None:
        raise ValidationContractError(
            f"Feature {index} with unknown scenario status must not carry scenario_inputs."
        )

    try:
        parsed = shape(geometry)
    except Exception as exc:  # shapely provides several parse exception types
        raise ValidationContractError(f"Feature {index} has invalid GeoJSON geometry: {exc}") from exc
    expected_geometry = {
        "release_polygon": {"Polygon", "MultiPolygon"},
        "deposit_polygon": {"Polygon", "MultiPolygon"},
        "runout_endpoint": {"Point"},
        "survey_coverage_polygon": {"Polygon", "MultiPolygon"},
    }[observation_type]
    if parsed.geom_type not in expected_geometry:
        raise ValidationContractError(
            f"Feature {index} {observation_type!r} requires {sorted(expected_geometry)}, not "
            f"{parsed.geom_type}."
        )
    if parsed.is_empty:
        raise ValidationContractError(f"Feature {index} geometry is empty.")
    if not parsed.is_valid:
        raise ValidationContractError(f"Feature {index} geometry is topologically invalid.")
    if parsed.has_z:
        raise ValidationContractError(f"Feature {index} must contain normalized 2D coordinates only.")
    bounds = parsed.bounds
    if len(bounds) != 4 or not all(math.isfinite(value) for value in bounds):
        raise ValidationContractError(f"Feature {index} contains non-finite coordinates.")
    coverage = manifest.spatial_coverage
    if (
        bounds[0] < coverage.west
        or bounds[1] < coverage.south
        or bounds[2] > coverage.east
        or bounds[3] > coverage.north
    ):
        raise ValidationContractError(
            f"Feature {index} lies outside the manifest's declared spatial coverage."
        )

    uncertainty = properties.get("horizontal_uncertainty_m")
    if uncertainty is not None and (
        isinstance(uncertainty, bool)
        or not isinstance(uncertainty, (int, float))
        or not math.isfinite(float(uncertainty))
        or float(uncertainty) < 0
    ):
        raise ValidationContractError(
            f"Feature {index} horizontal_uncertainty_m must be a finite non-negative number."
        )
    coverage_targets = properties.get("target_observation_types")
    if observation_type == "survey_coverage_polygon":
        allowed_targets = {"release_polygon", "deposit_polygon", "runout_endpoint"}
        if (
            not isinstance(coverage_targets, list)
            or not coverage_targets
            or len(set(coverage_targets)) != len(coverage_targets)
            or not set(coverage_targets).issubset(allowed_targets)
        ):
            raise ValidationContractError(
                f"Feature {index} survey coverage requires unique target_observation_types "
                "chosen from release_polygon, deposit_polygon, and runout_endpoint."
            )
    elif coverage_targets is not None:
        raise ValidationContractError(
            f"Feature {index} target_observation_types is valid only on survey coverage polygons."
        )
    return NormalizedObservation(
        observation_id=properties["observation_id"],
        event_id=properties["event_id"],
        observation_type=observation_type,
        partition=partition,
        geometry=_deep_freeze(geometry),
        properties=_deep_freeze(dict(properties)),
    )


def load_validation_dataset(manifest_path: str | Path) -> ValidationDataset:
    """Load and verify a normalized validation dataset and its GeoJSON lineage."""

    manifest_path = Path(manifest_path).resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        raw_manifest = json.loads(manifest_bytes.decode("utf-8"))
        manifest = ValidationDatasetManifest.model_validate(raw_manifest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationContractError(f"Invalid validation manifest {manifest_path}: {exc}") from exc

    observations_path = _resolve_observations_path(manifest_path, manifest.observations_file)
    if not observations_path.is_file():
        raise ValidationContractError(f"Observation file does not exist: {observations_path}")
    try:
        observation_bytes = observations_path.read_bytes()
    except OSError as exc:
        raise ValidationContractError(f"Cannot read observation GeoJSON: {exc}") from exc
    actual_sha = hashlib.sha256(observation_bytes).hexdigest()
    if actual_sha != manifest.observations_sha256:
        raise ValidationContractError(
            "Observation SHA-256 does not match the manifest; evidence may be stale or modified."
        )
    try:
        collection = json.loads(observation_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationContractError(f"Invalid observation GeoJSON: {exc}") from exc
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValidationContractError("Observation file must be a GeoJSON FeatureCollection.")
    if "crs" in collection:
        raise ValidationContractError(
            "GeoJSON 'crs' members are not accepted; the manifest CRS is the single authority."
        )
    raw_features = collection.get("features")
    if not isinstance(raw_features, list) or not raw_features:
        raise ValidationContractError("Observation FeatureCollection must contain at least one feature.")

    observations = tuple(
        _validate_feature(item, index=index, manifest=manifest)
        for index, item in enumerate(raw_features)
    )
    observation_ids = [item.observation_id for item in observations]
    if len(set(observation_ids)) != len(observation_ids):
        raise ValidationContractError("observation_id values must be unique within a dataset.")

    event_partitions: dict[str, str] = {}
    event_metadata: dict[str, tuple[Any, ...]] = {}
    counts: dict[str, int] = {}
    for item in observations:
        previous = event_partitions.setdefault(item.event_id, item.partition)
        if previous != item.partition:
            raise ValidationContractError(
                f"Event {item.event_id!r} appears in both {previous!r} and {item.partition!r}; "
                "calibration/holdout leakage is not permitted."
            )
        event_signature = (
            item.properties["event_date_status"],
            item.properties.get("event_start_date"),
            item.properties.get("event_end_date"),
            item.properties["scenario_status"],
            json.dumps(
                _canonical_json_value(item.properties.get("scenario_inputs")),
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        previous_metadata = event_metadata.setdefault(item.event_id, event_signature)
        if previous_metadata != event_signature:
            raise ValidationContractError(
                f"Event {item.event_id!r} has inconsistent dates or scenario inputs across its "
                "registered observations."
            )
        if manifest.scientific_use in {"field_validation", "calibration_only"} and (
            item.properties["event_date_status"] == "unknown"
        ):
            raise ValidationContractError(
                f"Event {item.event_id!r} has an unknown date and cannot support quantitative "
                "field calibration or validation."
            )
        if item.properties["event_date_status"] != "unknown" and (
            manifest.acquisition.start_date is not None
        ):
            event_start = date.fromisoformat(item.properties["event_start_date"])
            event_end = date.fromisoformat(item.properties["event_end_date"])
            acquisition_end = manifest.acquisition.end_date or manifest.acquisition.start_date
            if event_start < manifest.acquisition.start_date or event_end > acquisition_end:
                raise ValidationContractError(
                    f"Event {item.event_id!r} date bounds fall outside the manifest acquisition "
                    "period."
                )
        counts[item.partition] = counts.get(item.partition, 0) + 1
    target_observations = [
        item for item in observations if item.observation_type != "survey_coverage_polygon"
    ]
    if not target_observations:
        raise ValidationContractError(
            "A validation dataset requires at least one release, deposit, or endpoint observation."
        )
    if manifest.coverage_semantics == "surveyed_domain":
        feature_partitions = {item.partition for item in observations}
        coverage_partitions = {
            item.partition
            for item in observations
            if item.observation_type == "survey_coverage_polygon"
        }
        missing_coverage = sorted(feature_partitions - coverage_partitions)
        if missing_coverage:
            raise ValidationContractError(
                "Every partition in a surveyed-domain dataset requires a survey coverage polygon; "
                f"missing for {missing_coverage}."
            )
    if manifest.scientific_use == "field_validation" and not any(
        item.partition == "holdout" for item in target_observations
    ):
        raise ValidationContractError(
            "A field_validation dataset must contain an explicit holdout target observation. Use "
            "scientific_use='calibration_only' when no independent holdout exists."
        )

    identity_payload = "\0".join(
        (
            VALIDATION_CONTRACT_VERSION,
            manifest_sha256,
            actual_sha,
            manifest.original_source_sha256,
        )
    ).encode("utf-8")
    return ValidationDataset(
        manifest=manifest,
        observations=observations,
        manifest_path=manifest_path,
        observations_path=observations_path,
        partition_counts=MappingProxyType(dict(counts)),
        manifest_sha256=manifest_sha256,
        dataset_identity_sha256=hashlib.sha256(identity_payload).hexdigest(),
    )
