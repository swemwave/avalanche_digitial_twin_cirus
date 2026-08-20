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
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from shapely.geometry import shape

from .status import VALIDATION_CONTRACT_VERSION

ValidationComponent = Literal["release", "conditional_runout", "end_to_end"]
EvidenceProfile = Literal["R", "C", "E"]
LabelState = Literal["positive_unlabelled", "surveyed_positive_and_known_absence"]
AvalancheRegime = Literal["dry_dense_slab"]
ObservationType = Literal[
    "release_polygon",
    "deposit_polygon",
    "avalanche_footprint",
    "runout_endpoint",
    "survey_coverage_polygon",
    "invalid_observation_mask",
]
EvidenceType = Literal[
    "field_observation",
    "authoritative_inventory",
    "reviewed_remote_sensing",
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
    "reviewed_remote_sensing",
    "remote_sensing_interpretation",
    "synthetic_fixture",
    "model_output",
]

# This is deliberately an allowlist, not an EPSG-code parser. Membership says
# only that a maintainer reviewed the CRS definition as projected with horizontal
# metre units for this validation contract. It does not establish that a
# particular dataset used the correct datum, epoch, axis order, or transform.
REVIEWED_PROJECTED_METRE_CRS = frozenset(
    {
        "EPSG:2056",  # CH1903+ / LV95
        "EPSG:26911",  # NAD83 / UTM zone 11N
        "EPSG:32613",  # WGS 84 / UTM zone 13N
    }
)


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


class PhysicalQuantityEvidence(StrictModel):
    """Observed, bounded, or predeclared-distribution release-state evidence."""

    quantity: Literal["release_thickness", "release_density"]
    representation: Literal["measured_value", "bounded_interval", "distribution"]
    units: Literal["metre", "kilogram_per_cubic_metre"]
    value: float | None = None
    lower: float | None = None
    upper: float | None = None
    distribution_name: str | None = None
    distribution_parameters: Mapping[str, float] | None = None
    source_uri: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1)
    uncertainty_statement: str = Field(min_length=1)
    frozen_without_runout_target: bool

    @model_validator(mode="after")
    def validate_quantity(self) -> "PhysicalQuantityEvidence":
        expected_units = {
            "release_thickness": "metre",
            "release_density": "kilogram_per_cubic_metre",
        }[self.quantity]
        if self.units != expected_units:
            raise ValueError(
                f"{self.quantity} requires units={expected_units!r}; unit conversion must be "
                "completed and recorded before ingestion."
            )
        numeric = (self.value, self.lower, self.upper)
        if any(value is not None and not math.isfinite(value) for value in numeric):
            raise ValueError("Release-state values and bounds must be finite.")
        if any(value is not None and value <= 0 for value in numeric):
            raise ValueError("Release thickness and density evidence must be positive.")
        if self.representation == "measured_value":
            if self.value is None or any(value is not None for value in (self.lower, self.upper)):
                raise ValueError("measured_value requires only value.")
            if self.distribution_name is not None or self.distribution_parameters is not None:
                raise ValueError("measured_value must not carry distribution fields.")
        elif self.representation == "bounded_interval":
            if self.value is not None or self.lower is None or self.upper is None:
                raise ValueError("bounded_interval requires lower and upper, and no value.")
            if self.upper <= self.lower:
                raise ValueError("Release-state upper bound must exceed the lower bound.")
            if self.distribution_name is not None or self.distribution_parameters is not None:
                raise ValueError("bounded_interval must not carry distribution fields.")
        else:
            if self.value is not None or self.lower is not None or self.upper is not None:
                raise ValueError("distribution uses distribution fields, not value/bounds.")
            if not isinstance(self.distribution_name, str) or not self.distribution_name.strip():
                raise ValueError("distribution requires a named distribution.")
            if not self.distribution_parameters:
                raise ValueError("distribution requires finite numeric parameters.")
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in self.distribution_parameters.values()
            ):
                raise ValueError("Distribution parameters must be finite numbers.")
        return self


class TerrainSurfaceMetadata(StrictModel):
    source_uri: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquisition_start_date: date
    acquisition_end_date: date
    acquisition_epoch_statement: str = Field(min_length=1)
    crs: str = Field(pattern=r"^EPSG:[1-9][0-9]*$")
    horizontal_units: Literal["metre"]
    vertical_units: Literal["metre"]
    vertical_datum: str = Field(min_length=1)
    surface_type: Literal["bare_earth", "snow_surface", "digital_surface_model"]
    event_surface_mismatch_statement: str = Field(min_length=1)
    transformation_lineage: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_surface(self) -> "TerrainSurfaceMetadata":
        if self.acquisition_end_date < self.acquisition_start_date:
            raise ValueError("DEM acquisition_end_date precedes acquisition_start_date.")
        if self.crs not in REVIEWED_PROJECTED_METRE_CRS:
            raise ValueError("DEM CRS must be a code-reviewed projected metre CRS.")
        return self


class EventInputEvidence(StrictModel):
    input_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    category: Literal["event_forcing", "snow_state"]
    parameter: str = Field(min_length=1)
    units: str = Field(min_length=1)
    valid_start_utc: datetime
    valid_end_utc: datetime
    source_uri: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: str = Field(min_length=1)
    uncertainty_statement: str = Field(min_length=1)
    spatial_representativeness: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_interval(self) -> "EventInputEvidence":
        for name, value in (
            ("valid_start_utc", self.valid_start_utc),
            ("valid_end_utc", self.valid_end_utc),
        ):
            if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError(f"{name} must carry an explicit UTC offset.")
        if self.valid_end_utc < self.valid_start_utc:
            raise ValueError("Event input valid_end_utc precedes valid_start_utc.")
        return self


class ValidationEventMetadata(StrictModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    mountain_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    path_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    storm_cycle_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    avalanche_regime: AvalancheRegime
    event_start_utc: datetime
    event_end_utc: datetime
    event_time_confidence: str = Field(min_length=1)
    terrain_surface: TerrainSurfaceMetadata
    release_thickness: PhysicalQuantityEvidence | None = None
    release_density: PhysicalQuantityEvidence | None = None
    model_inputs: tuple[EventInputEvidence, ...] = ()
    release_to_runout_rule_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_event(self) -> "ValidationEventMetadata":
        for name, value in (
            ("event_start_utc", self.event_start_utc),
            ("event_end_utc", self.event_end_utc),
        ):
            if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
                raise ValueError(f"{name} must carry an explicit UTC offset.")
        if self.event_end_utc < self.event_start_utc:
            raise ValueError("event_end_utc precedes event_start_utc.")
        if self.release_thickness is not None and (
            self.release_thickness.quantity != "release_thickness"
        ):
            raise ValueError("release_thickness carries the wrong physical quantity.")
        if self.release_density is not None and self.release_density.quantity != "release_density":
            raise ValueError("release_density carries the wrong physical quantity.")
        input_ids = [item.input_id for item in self.model_inputs]
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("Event model-input IDs must be unique.")
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
    schema_version: Literal[
        "avycore-validation-dataset-v2",
        "avycore-validation-dataset-v3",
    ]
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    title: str = Field(min_length=1)
    source: SourceMetadata
    acquisition: AcquisitionMetadata
    evidence_type: EvidenceType
    scientific_use: ScientificUse
    independent_of_model: bool
    component_tested: ValidationComponent | None = None
    evidence_profile: EvidenceProfile | None = None
    label_state: LabelState | None = None
    events: tuple[ValidationEventMetadata, ...] | None = None
    observation_types: tuple[ObservationType, ...] = Field(min_length=1)
    original_crs: str = Field(
        min_length=1,
        description="CRS of the immutable source evidence before normalization.",
    )
    crs: str = Field(
        pattern=r"^EPSG:[1-9][0-9]*$",
        description=(
            "Normalized projected EPSG CRS. Coordinates must use easting/northing axis order "
            "and metre units; the loader deliberately performs no CRS inference or transform."
        ),
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

    @field_validator("crs")
    @classmethod
    def require_reviewed_projected_metre_crs(cls, value: str) -> str:
        if value not in REVIEWED_PROJECTED_METRE_CRS:
            supported = ", ".join(sorted(REVIEWED_PROJECTED_METRE_CRS))
            raise ValueError(
                f"crs must be a code-reviewed projected metre CRS; reviewed values are "
                f"{supported}. Before adding another EPSG code, review its projection, "
                "horizontal units, axis order, and datum/epoch compatibility."
            )
        return value

    @model_validator(mode="after")
    def validate_scientific_use(self) -> "ValidationDatasetManifest":
        is_v3 = self.schema_version == "avycore-validation-dataset-v3"
        trusted = {"field_observation", "authoritative_inventory"}
        if is_v3:
            trusted.add("reviewed_remote_sensing")
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
        if self.evidence_type == "reviewed_remote_sensing" and not is_v3:
            raise ValueError("Reviewed remote-sensing evidence requires validation contract v3.")
        if self.evidence_type == "reviewed_remote_sensing" and self.scientific_use not in {
            "field_validation",
            "calibration_only",
            "excluded",
        }:
            raise ValueError(
                "Reviewed remote sensing is a quantitative v3 evidence class; unreviewed or "
                "qualitative interpretation must use remote_sensing_interpretation."
            )
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
        if is_v3:
            if self.component_tested is None or self.evidence_profile is None:
                raise ValueError("Validation contract v3 requires component_tested and evidence_profile.")
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
            if self.label_state is None:
                raise ValueError("Validation contract v3 requires an explicit label_state.")
            if not self.events:
                raise ValueError("Validation contract v3 requires non-empty event metadata.")
            event_ids = [event.event_id for event in self.events]
            if len(set(event_ids)) != len(event_ids):
                raise ValueError("Validation contract v3 event_id values must be unique.")
            if any(event.terrain_surface.crs != self.crs for event in self.events):
                raise ValueError("Every v3 DEM CRS must match the normalized dataset CRS.")
            required_observation_types = {
                "R": {"release_polygon"},
                "C": {"release_polygon"},
                "E": {"release_polygon"},
            }[self.evidence_profile]
            if not required_observation_types.issubset(self.observation_types):
                raise ValueError(
                    f"Evidence profile {self.evidence_profile} requires observation types "
                    f"{sorted(required_observation_types)}."
                )
            if self.evidence_profile in {"C", "E"} and not {
                "deposit_polygon",
                "runout_endpoint",
            }.intersection(self.observation_types):
                raise ValueError(
                    f"Evidence profile {self.evidence_profile} requires a dense-flow deposit "
                    "polygon and/or terminal endpoint."
                )
            if self.evidence_profile in {"C", "E"}:
                incomplete_release_state = [
                    event.event_id
                    for event in self.events
                    if event.release_thickness is None
                    or event.release_density is None
                    or not event.release_thickness.frozen_without_runout_target
                    or not event.release_density.frozen_without_runout_target
                ]
                if incomplete_release_state:
                    raise ValueError(
                        "Profiles C/E require thickness and density evidence frozen without "
                        f"the observed runout target; incomplete events={incomplete_release_state}."
                    )
            if self.evidence_profile == "E":
                missing_inputs = [event.event_id for event in self.events if not event.model_inputs]
                missing_rules = [
                    event.event_id
                    for event in self.events
                    if event.release_to_runout_rule_sha256 is None
                ]
                if missing_inputs or missing_rules:
                    raise ValueError(
                        "Profile E requires provenance-bearing release-model inputs and a frozen "
                        "release-to-runout rule for every event; "
                        f"missing_inputs={missing_inputs}, missing_rules={missing_rules}."
                    )
            elif any(event.release_to_runout_rule_sha256 is not None for event in self.events):
                raise ValueError("release_to_runout_rule_sha256 is valid only for Profile E.")
            surveyed = self.label_state == "surveyed_positive_and_known_absence"
            if surveyed != (self.coverage_semantics == "surveyed_domain"):
                raise ValueError(
                    "label_state and coverage_semantics disagree; positive/unlabelled evidence "
                    "cannot claim a surveyed negative domain."
                )
            if surveyed and (
                self.absence_semantics != "surveyed_domain_supports_known_absence"
                or self.survey_completeness != "complete_for_declared_target"
            ):
                raise ValueError(
                    "surveyed_positive_and_known_absence requires complete-search known-absence "
                    "semantics."
                )
            if not surveyed and (
                self.absence_semantics != "unknown_unless_explicitly_observed"
                or self.survey_completeness == "complete_for_declared_target"
            ):
                raise ValueError(
                    "positive_unlabelled evidence must preserve unknown absence explicitly."
                )
        elif any(
            value is not None
            for value in (
                self.component_tested,
                self.evidence_profile,
                self.label_state,
                self.events,
            )
        ):
            raise ValueError("Component profiles and grouped event metadata require contract v3.")
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
        "reviewed_remote_sensing",
        "unverified",
        "synthetic",
    }
    if verification_status not in allowed_verification:
        raise ValidationContractError(
            f"Feature {index} has unknown verification_status {verification_status!r}."
        )
    if manifest.scientific_use in {"field_validation", "calibration_only"} and (
        verification_status
        not in {"field_verified", "professionally_verified", "reviewed_remote_sensing"}
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
        "reviewed_remote_sensing",
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
        "reviewed_remote_sensing",
    }
    if manifest.scientific_use in {"field_validation", "calibration_only"} and (
        method_class not in quantitative_methods
    ):
        raise ValidationContractError(
            f"Feature {index} method class {method_class!r} cannot support quantitative field "
            "calibration or validation."
        )
    evidence_method = {
        "reviewed_remote_sensing": "reviewed_remote_sensing",
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
    if scenario_status not in {"documented", "partially_documented", "unknown"}:
        raise ValidationContractError(
            f"Feature {index} scenario_status must be 'documented', "
            "'partially_documented', or 'unknown'."
        )
    scenario = properties.get("scenario_inputs")
    if scenario_status in {"documented", "partially_documented"}:
        if not isinstance(scenario, dict):
            raise ValidationContractError(
                f"Feature {index} documented scenario requires scenario_inputs."
            )
        numeric_fields = ("new_snow_cm", "wind_speed_kmh", "wind_direction_deg")
        for field_name in numeric_fields:
            if scenario_status == "partially_documented" and field_name not in scenario:
                continue
            value = scenario.get(field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValidationContractError(
                    f"Feature {index} scenario_inputs.{field_name} must be finite."
                )
        if (
            ("new_snow_cm" in scenario and scenario["new_snow_cm"] < 0)
            or ("wind_speed_kmh" in scenario and scenario["wind_speed_kmh"] < 0)
        ):
            raise ValidationContractError(
                f"Feature {index} scenario snow and wind speed cannot be negative."
            )
        if (
            "wind_direction_deg" in scenario
            and not 0 <= scenario["wind_direction_deg"] < 360
        ):
            raise ValidationContractError(
                f"Feature {index} scenario wind direction must be in [0, 360) degrees using "
                "the meteorological wind-from convention."
            )
        if (
            (scenario_status == "documented" or "release_size" in scenario)
            and scenario.get("release_size")
            not in {"small", "medium", "large", "very_large"}
        ):
            raise ValidationContractError(
                f"Feature {index} scenario_inputs.release_size is invalid."
            )
        if scenario_status == "partially_documented" and not any(
            field_name in scenario for field_name in (*numeric_fields, "release_size")
        ):
            raise ValidationContractError(
                f"Feature {index} partially documented scenario requires at least one observed "
                "scenario input."
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

    is_v3 = manifest.schema_version == "avycore-validation-dataset-v3"
    if is_v3:
        v3_required = (
            "mountain_id",
            "path_id",
            "storm_cycle_id",
            "observation_confidence",
            "confidence_basis",
            "survey_date",
            "source_resolution_m",
            "detection_limitations",
            "horizontal_uncertainty_confidence_level",
            "horizontal_uncertainty_method",
            "annotation_blind_to_model_output",
        )
        missing_v3 = [name for name in v3_required if properties.get(name) is None]
        if missing_v3:
            raise ValidationContractError(
                f"Feature {index} is missing validation-contract-v3 properties: {missing_v3}."
            )
        event_index = {event.event_id: event for event in manifest.events or ()}
        event = event_index.get(properties["event_id"])
        if event is None:
            raise ValidationContractError(
                f"Feature {index} event_id is not registered in manifest.events."
            )
        for name in ("mountain_id", "path_id", "storm_cycle_id"):
            if properties[name] != getattr(event, name):
                raise ValidationContractError(
                    f"Feature {index} {name} does not match manifest event metadata."
                )
        if properties["observation_confidence"] not in {"high", "medium", "low"}:
            raise ValidationContractError(
                f"Feature {index} observation_confidence must be high, medium, or low."
            )
        for name in (
            "confidence_basis",
            "detection_limitations",
            "horizontal_uncertainty_method",
        ):
            if not isinstance(properties[name], str) or not properties[name].strip():
                raise ValidationContractError(f"Feature {index} {name} must be non-empty.")
        try:
            date.fromisoformat(properties["survey_date"])
        except (TypeError, ValueError) as exc:
            raise ValidationContractError(
                f"Feature {index} survey_date must be an ISO 8601 calendar date."
            ) from exc
        for name, allow_zero in (
            ("source_resolution_m", False),
            ("horizontal_uncertainty_m", True),
            ("horizontal_uncertainty_confidence_level", False),
        ):
            value = properties.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or (float(value) < 0 if allow_zero else float(value) <= 0)
            ):
                raise ValidationContractError(
                    f"Feature {index} {name} must be a finite "
                    f"{'non-negative' if allow_zero else 'positive'} number."
                )
        if not 0 < float(properties["horizontal_uncertainty_confidence_level"]) <= 1:
            raise ValidationContractError(
                f"Feature {index} horizontal uncertainty confidence must be in (0, 1]."
            )
        blind = properties["annotation_blind_to_model_output"]
        if not isinstance(blind, bool):
            raise ValidationContractError(
                f"Feature {index} annotation_blind_to_model_output must be boolean."
            )
        protocol_sha = properties.get("annotation_protocol_sha256")
        if method_class == "reviewed_remote_sensing":
            if verification_status != "reviewed_remote_sensing":
                raise ValidationContractError(
                    f"Feature {index} reviewed remote sensing requires matching verification status."
                )
            if not blind:
                raise ValidationContractError(
                    f"Feature {index} remote-sensing annotation was not blind to model output."
                )
            if not isinstance(protocol_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", protocol_sha):
                raise ValidationContractError(
                    f"Feature {index} reviewed remote sensing requires annotation_protocol_sha256."
                )
        elif protocol_sha is not None and (
            not isinstance(protocol_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", protocol_sha)
        ):
            raise ValidationContractError(
                f"Feature {index} annotation_protocol_sha256 must be a lowercase SHA-256."
            )
        if observation_type == "release_polygon" and manifest.evidence_profile in {"C", "E"}:
            if properties.get("release_geometry_independent") is not True:
                raise ValidationContractError(
                    f"Feature {index} Profiles C/E require an independent release polygon."
                )
        if observation_type == "deposit_polygon" and manifest.evidence_profile in {"C", "E"}:
            if properties.get("flow_observation_scope") != "dense_flow_deposit":
                raise ValidationContractError(
                    f"Feature {index} Profiles C/E require a dense-flow deposit target."
                )
        if observation_type == "runout_endpoint" and manifest.evidence_profile in {"C", "E"}:
            if properties.get("terminal_dense_flow_toe") is not True:
                raise ValidationContractError(
                    f"Feature {index} Profiles C/E require a terminal dense-flow toe."
                )

    try:
        parsed = shape(geometry)
    except Exception as exc:  # shapely provides several parse exception types
        raise ValidationContractError(f"Feature {index} has invalid GeoJSON geometry: {exc}") from exc
    expected_geometry = {
        "release_polygon": {"Polygon", "MultiPolygon"},
        "deposit_polygon": {"Polygon", "MultiPolygon"},
        "avalanche_footprint": {"Polygon", "MultiPolygon"},
        "runout_endpoint": {"Point"},
        "survey_coverage_polygon": {"Polygon", "MultiPolygon"},
        "invalid_observation_mask": {"Polygon", "MultiPolygon"},
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
        allowed_targets = {
            "release_polygon",
            "deposit_polygon",
            "avalanche_footprint",
            "runout_endpoint",
        }
        if (
            not isinstance(coverage_targets, list)
            or not coverage_targets
            or len(set(coverage_targets)) != len(coverage_targets)
            or not set(coverage_targets).issubset(allowed_targets)
        ):
            raise ValidationContractError(
                f"Feature {index} survey coverage requires unique target_observation_types "
                "chosen from release_polygon, deposit_polygon, avalanche_footprint, and "
                "runout_endpoint."
            )
        if is_v3:
            if not isinstance(properties.get("detection_mask_observation_ids"), list):
                raise ValidationContractError(
                    f"Feature {index} v3 survey coverage requires detection_mask_observation_ids."
                )
            complete_search = properties.get("complete_search_semantics")
            expected_complete_search = (
                manifest.label_state == "surveyed_positive_and_known_absence"
            )
            if complete_search is not expected_complete_search:
                raise ValidationContractError(
                    f"Feature {index} complete_search_semantics conflicts with label_state."
                )
    elif coverage_targets is not None:
        raise ValidationContractError(
            f"Feature {index} target_observation_types is valid only on survey coverage polygons."
        )
    if observation_type != "survey_coverage_polygon" and (
        properties.get("detection_mask_observation_ids") is not None
        or properties.get("complete_search_semantics") is not None
    ):
        raise ValidationContractError(
            f"Feature {index} detection-mask links and complete-search semantics belong only "
            "on survey coverage polygons."
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
    path_partitions: dict[str, str] = {}
    storm_partitions: dict[str, str] = {}
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
        if manifest.schema_version == "avycore-validation-dataset-v3":
            for group_name, partitions in (
                ("path_id", path_partitions),
                ("storm_cycle_id", storm_partitions),
            ):
                group_id = item.properties[group_name]
                previous_partition = partitions.setdefault(group_id, item.partition)
                if previous_partition != item.partition:
                    raise ValidationContractError(
                        f"{group_name} {group_id!r} appears in both {previous_partition!r} and "
                        f"{item.partition!r}; grouped holdout leakage is not permitted."
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
        item
        for item in observations
        if item.observation_type
        not in {"survey_coverage_polygon", "invalid_observation_mask"}
    ]
    if not target_observations:
        raise ValidationContractError(
            "A validation dataset requires at least one release, deposit, whole-avalanche "
            "footprint, or endpoint observation."
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
    if manifest.schema_version == "avycore-validation-dataset-v3":
        event_records = {event.event_id: event for event in manifest.events or ()}
        observed_event_ids = {item.event_id for item in observations}
        if observed_event_ids != set(event_records):
            raise ValidationContractError(
                "Manifest event metadata and observation event membership must match exactly; "
                f"missing_observations={sorted(set(event_records) - observed_event_ids)}, "
                f"unregistered_events={sorted(observed_event_ids - set(event_records))}."
            )
        by_event: dict[str, list[NormalizedObservation]] = {}
        for item in observations:
            by_event.setdefault(item.event_id, []).append(item)
        for event_id, event_features in by_event.items():
            event_types = {item.observation_type for item in event_features}
            if "release_polygon" not in event_types:
                raise ValidationContractError(
                    f"Profile {manifest.evidence_profile} event {event_id!r} lacks a release polygon."
                )
            if manifest.evidence_profile in {"C", "E"} and not {
                "deposit_polygon",
                "runout_endpoint",
            }.intersection(event_types):
                raise ValidationContractError(
                    f"Profile {manifest.evidence_profile} event {event_id!r} lacks a dense-flow "
                    "deposit polygon or terminal endpoint."
                )
            if manifest.label_state == "surveyed_positive_and_known_absence":
                component_targets = {
                    "release": {"release_polygon"},
                    "conditional_runout": {"deposit_polygon", "runout_endpoint"},
                    "end_to_end": {
                        "release_polygon",
                        "deposit_polygon",
                        "runout_endpoint",
                    },
                }[manifest.component_tested]
                target_types = {
                    item.observation_type
                    for item in event_features
                    if item.observation_type
                    not in {"survey_coverage_polygon", "invalid_observation_mask"}
                    and item.observation_type in component_targets
                }
                covered_types: set[str] = set()
                for coverage in (
                    item
                    for item in event_features
                    if item.observation_type == "survey_coverage_polygon"
                ):
                    covered_types.update(coverage.properties["target_observation_types"])
                missing_target_coverage = sorted(target_types - covered_types)
                if missing_target_coverage:
                    raise ValidationContractError(
                        f"Event {event_id!r} lacks complete-search coverage for targets "
                        f"{missing_target_coverage}."
                    )
        feature_index = {item.observation_id: item for item in observations}
        for coverage in (
            item for item in observations if item.observation_type == "survey_coverage_polygon"
        ):
            mask_ids = coverage.properties.get("detection_mask_observation_ids", ())
            if len(set(mask_ids)) != len(mask_ids):
                raise ValidationContractError(
                    f"Coverage {coverage.observation_id!r} repeats detection-mask IDs."
                )
            for mask_id in mask_ids:
                mask = feature_index.get(mask_id)
                if mask is None or mask.observation_type != "invalid_observation_mask":
                    raise ValidationContractError(
                        f"Coverage {coverage.observation_id!r} references missing/non-mask "
                        f"observation {mask_id!r}."
                    )
                if mask.event_id != coverage.event_id or mask.partition != coverage.partition:
                    raise ValidationContractError(
                        f"Coverage {coverage.observation_id!r} detection masks must share its "
                        "event and partition."
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
            manifest.schema_version,
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
