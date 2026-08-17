"""Strict pre-ingestion contract for field-validation data-owner deliveries.

This contract is intentionally earlier than :mod:`avycore.validation.contracts`.
It checks that an owner supplied the source material needed for independent
scientific review; it does not normalize geometries, assign calibration or
holdout membership, register trust, run a model, or make evidence eligible by
itself.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OWNER_DELIVERY_SCHEMA_VERSION = "avycore-field-validation-owner-delivery-v1"

_UNACCEPTABLE_REQUIRED_TEXT = {
    "assumed",
    "inferred",
    "missing",
    "model-derived",
    "model derived",
    "n/a",
    "na",
    "none",
    "not available",
    "substituted",
    "tbd",
    "unknown",
}


def _require_observed_text(value: str, field_name: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    if normalized in _UNACCEPTABLE_REQUIRED_TEXT:
        raise ValueError(
            f"{field_name} must be an observed, documented value; missing, inferred, "
            "substituted, assumed, or model-derived placeholders are rejected."
        )
    return value


def _require_direct_method_text(value: str, field_name: str) -> str:
    value = _require_observed_text(value, field_name)
    if re.search(
        r"\b(?:assumed|inferred|substitut(?:e|ed|ion)|model[ -]derived|"
        r"back[ -]calculated)\b",
        value,
        flags=re.IGNORECASE,
    ):
        raise ValueError(
            f"{field_name} must document a direct observation or measurement method; "
            "inferred, substituted, assumed, back-calculated, or model-derived methods "
            "are rejected."
        )
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImmutableAsset(StrictModel):
    """One original owner-delivered file with explicit reuse terms."""

    relative_path: str = Field(min_length=1)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    copyright_holder: str = Field(min_length=1)
    licence: str = Field(min_length=1)
    licence_uri: str = Field(min_length=1)
    permitted_use: str = Field(min_length=1)
    redistribution_permitted: bool
    licence_record_relative_path: str = Field(min_length=1)
    licence_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_owner_delivered_bytes: Literal[True]

    @field_validator("relative_path", "licence_record_relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str, info: Any) -> str:
        path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if (
            path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or "\\" in value
            or ".." in path.parts
            or value.endswith("/")
        ):
            raise ValueError(
                f"Asset {info.field_name} must name a file below the delivery root."
            )
        return value

    @field_validator(
        "source_uri",
        "copyright_holder",
        "licence",
        "licence_uri",
        "permitted_use",
    )
    @classmethod
    def reject_missing_licence_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)


class RequiredObservationProvenance(StrictModel):
    """Explicitly rules out filling a required observation from a model or prior."""

    evidence_origin: Literal["direct_owner_observation"]
    original_measurement_available: Literal[True]
    missing_value_supplied: Literal[False]
    inferred_value: Literal[False]
    substituted_value: Literal[False]
    model_derived_value: Literal[False]
    independent_of_prediction: Literal[True]
    measurement_preserving_processing: str = Field(min_length=1)

    @field_validator("measurement_preserving_processing")
    @classmethod
    def reject_missing_processing_lineage(cls, value: str) -> str:
        return _require_observed_text(value, "measurement_preserving_processing")


class CrsDatumLineage(StrictModel):
    lineage_record: ImmutableAsset
    original_crs: str = Field(min_length=1)
    horizontal_datum: str = Field(min_length=1)
    horizontal_datum_realization: str = Field(min_length=1)
    vertical_datum: str = Field(min_length=1)
    vertical_datum_realization: str = Field(min_length=1)
    vertical_coordinate_type: Literal["orthometric_height", "ellipsoidal_height"]
    horizontal_units: Literal["metre"]
    vertical_units: Literal["metre"]
    axis_order: Literal["easting_northing"]
    coordinate_epoch: str = Field(min_length=1)
    transformation_to_delivery_crs: str = Field(min_length=1)
    delivery_crs: str = Field(min_length=1)

    @field_validator(
        "original_crs",
        "horizontal_datum",
        "horizontal_datum_realization",
        "vertical_datum",
        "vertical_datum_realization",
        "coordinate_epoch",
        "transformation_to_delivery_crs",
        "delivery_crs",
    )
    @classmethod
    def reject_missing_crs_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)


class QuantifiedUncertainty(StrictModel):
    magnitude: float = Field(ge=0)
    units: str = Field(min_length=1)
    confidence_level: float = Field(gt=0, le=1)
    method: str = Field(min_length=1)

    @field_validator("magnitude", "confidence_level")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Uncertainty values must be finite.")
        return value

    @field_validator("units", "method")
    @classmethod
    def reject_missing_uncertainty_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)


class BoundedMeasurement(StrictModel):
    estimate: float = Field(gt=0)
    lower: float = Field(gt=0)
    upper: float = Field(gt=0)
    confidence_level: float = Field(gt=0, le=1)
    method: str = Field(min_length=1)

    @field_validator("estimate", "lower", "upper", "confidence_level")
    @classmethod
    def require_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Measurement values must be finite.")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> "BoundedMeasurement":
        if not self.lower <= self.estimate <= self.upper:
            raise ValueError("Measurement estimate must lie inside its uncertainty bounds.")
        if self.upper <= self.lower:
            raise ValueError("Measurement uncertainty upper bound must exceed its lower bound.")
        return self

    @field_validator("method")
    @classmethod
    def reject_missing_measurement_method(cls, value: str) -> str:
        return _require_observed_text(value, "method")


class ObservedReleaseGeometry(StrictModel):
    geometry: ImmutableAsset
    crs_lineage: CrsDatumLineage
    provenance: RequiredObservationProvenance
    observation_time_utc: datetime
    observation_method: str = Field(min_length=1)
    positional_uncertainty: QuantifiedUncertainty
    independently_observed: Literal[True]
    independent_of_model_output: Literal[True]

    @field_validator("observation_time_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "observation_time_utc")

    @field_validator("observation_method")
    @classmethod
    def require_direct_observation_method(cls, value: str) -> str:
        return _require_direct_method_text(value, "observation_method")

    @model_validator(mode="after")
    def require_metre_uncertainty(self) -> "ObservedReleaseGeometry":
        if self.positional_uncertainty.units != "metre":
            raise ValueError("Release-geometry positional uncertainty must use metre.")
        return self


class ReleaseThicknessEvidence(StrictModel):
    measurement_m: BoundedMeasurement
    source: ImmutableAsset
    provenance: RequiredObservationProvenance
    measurement_time_utc: datetime
    normal_to_slope: Literal[True]
    event_specific: Literal[True]

    @field_validator("measurement_time_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "measurement_time_utc")


class ReleaseDensityEvidence(StrictModel):
    measurement_kg_m3: BoundedMeasurement
    source: ImmutableAsset
    provenance: RequiredObservationProvenance
    measurement_time_utc: datetime
    event_specific: Literal[True]

    @field_validator("measurement_time_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "measurement_time_utc")


class EventSurfaceDem(StrictModel):
    dem: ImmutableAsset
    crs_lineage: CrsDatumLineage
    provenance: RequiredObservationProvenance
    acquisition_start_utc: datetime
    acquisition_end_utc: datetime
    surface_type: Literal["pre_event_snow_surface"]
    vertical_uncertainty: QuantifiedUncertainty
    horizontal_uncertainty: QuantifiedUncertainty
    valid_at_event: Literal[True]
    validity_method: str = Field(min_length=1)

    @field_validator("acquisition_start_utc", "acquisition_end_utc")
    @classmethod
    def require_utc(cls, value: datetime, info: Any) -> datetime:
        return _require_utc(value, info.field_name)

    @field_validator("validity_method")
    @classmethod
    def require_direct_validity_method(cls, value: str) -> str:
        return _require_direct_method_text(value, "validity_method")

    @model_validator(mode="after")
    def validate_interval(self) -> "EventSurfaceDem":
        if self.acquisition_end_utc < self.acquisition_start_utc:
            raise ValueError("DEM acquisition_end_utc precedes acquisition_start_utc.")
        if self.vertical_uncertainty.units != "metre":
            raise ValueError("DEM vertical uncertainty must use metre.")
        if self.horizontal_uncertainty.units != "metre":
            raise ValueError("DEM horizontal uncertainty must use metre.")
        return self


class TerminalDenseFlowObservation(StrictModel):
    observation_type: Literal["terminal_deposit_polygon", "terminal_endpoint"]
    geometry: ImmutableAsset
    crs_lineage: CrsDatumLineage
    provenance: RequiredObservationProvenance
    observation_time_utc: datetime
    observation_method: str = Field(min_length=1)
    component_attribution_method: str = Field(min_length=1)
    positional_uncertainty: QuantifiedUncertainty
    component: Literal["dense_flow"]
    terminal_feature_verified: Literal[True]
    independent_of_model_output: Literal[True]

    @field_validator("observation_time_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "observation_time_utc")

    @field_validator("observation_method", "component_attribution_method")
    @classmethod
    def require_direct_terminal_method(cls, value: str, info: Any) -> str:
        return _require_direct_method_text(value, info.field_name)

    @model_validator(mode="after")
    def require_metre_uncertainty(self) -> "TerminalDenseFlowObservation":
        if self.positional_uncertainty.units != "metre":
            raise ValueError("Terminal-observation positional uncertainty must use metre.")
        return self


class SurveyCoverageDetection(StrictModel):
    survey_coverage_geometry: ImmutableAsset
    detection_mask: ImmutableAsset
    crs_lineage: CrsDatumLineage
    provenance: RequiredObservationProvenance
    mapping_time_utc: datetime
    coverage_positional_uncertainty: QuantifiedUncertainty
    declared_target: Literal["terminal_dense_flow_deposit_or_endpoint"]
    complete_search_inside_coverage: Literal[True]
    non_detection_inside_coverage: Literal["observed_negative"]
    outside_coverage: Literal["unknown"]
    masked_or_occluded_cells: Literal["unknown"]
    detection_limit: str = Field(min_length=1)
    detection_limit_units: str = Field(min_length=1)
    detection_confidence_level: float = Field(gt=0, le=1)
    completeness_method: str = Field(min_length=1)

    @field_validator("mapping_time_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "mapping_time_utc")

    @field_validator("detection_limit", "detection_limit_units", "completeness_method")
    @classmethod
    def reject_missing_detection_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)

    @field_validator("detection_confidence_level")
    @classmethod
    def require_finite_confidence(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Detection confidence must be finite.")
        return value

    @model_validator(mode="after")
    def require_metre_uncertainty(self) -> "SurveyCoverageDetection":
        if self.coverage_positional_uncertainty.units != "metre":
            raise ValueError("Survey-coverage positional uncertainty must use metre.")
        return self


class FieldValidationOwnerEvent(StrictModel):
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    mountain_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    path_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    storm_cycle_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    grouping_evidence: ImmutableAsset
    grouping_method: str = Field(min_length=1)
    avalanche_regime: Literal["dry_dense_slab"]
    regime_evidence: ImmutableAsset
    regime_provenance: RequiredObservationProvenance
    regime_classification_method: str = Field(min_length=1)
    event_start_utc: datetime
    event_end_utc: datetime
    event_time_confidence: str = Field(min_length=1)
    event_time_evidence: ImmutableAsset
    event_time_provenance: RequiredObservationProvenance
    release_geometry: ObservedReleaseGeometry
    release_thickness: ReleaseThicknessEvidence
    release_density: ReleaseDensityEvidence
    event_surface_dem: EventSurfaceDem
    terminal_observation: TerminalDenseFlowObservation
    survey_coverage: SurveyCoverageDetection

    @field_validator("event_start_utc", "event_end_utc")
    @classmethod
    def require_utc(cls, value: datetime, info: Any) -> datetime:
        return _require_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_times(self) -> "FieldValidationOwnerEvent":
        if self.event_end_utc < self.event_start_utc:
            raise ValueError("event_end_utc precedes event_start_utc.")
        if self.event_surface_dem.acquisition_end_utc > self.event_start_utc:
            raise ValueError("The event-surface DEM must be acquired before the event starts.")
        if self.release_geometry.observation_time_utc < self.event_start_utc:
            raise ValueError("Release geometry cannot be observed before the event starts.")
        if self.terminal_observation.observation_time_utc < self.event_start_utc:
            raise ValueError("Terminal observation cannot be observed before the event starts.")
        if self.survey_coverage.mapping_time_utc < self.event_start_utc:
            raise ValueError("Survey coverage cannot be mapped before the event starts.")
        return self

    @field_validator(
        "grouping_method",
        "regime_classification_method",
        "event_time_confidence",
    )
    @classmethod
    def reject_missing_event_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)


class FieldValidationOwnerDelivery(StrictModel):
    schema_version: Literal[OWNER_DELIVERY_SCHEMA_VERSION]
    delivery_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    provider: str = Field(min_length=1)
    provider_contact: str = Field(min_length=1)
    delivery_version: str = Field(min_length=1)
    delivery_licence: str = Field(min_length=1)
    permitted_use: str = Field(min_length=1)
    permission_or_licence_record: ImmutableAsset
    grouping_status: Literal["provider_proposed_requires_independent_review"]
    partition_status: Literal["unassigned_until_complete_cohort_review"]
    predictions_generated: Literal[False]
    holdout_targets_accessed: Literal[False]
    events: tuple[FieldValidationOwnerEvent, ...] = Field(min_length=1)

    @field_validator(
        "provider",
        "provider_contact",
        "delivery_version",
        "delivery_licence",
        "permitted_use",
    )
    @classmethod
    def reject_missing_delivery_lineage(cls, value: str, info: Any) -> str:
        return _require_observed_text(value, info.field_name)

    @model_validator(mode="after")
    def validate_events(self) -> "FieldValidationOwnerDelivery":
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Owner delivery event_id values must be unique.")
        licence_record = self.permission_or_licence_record
        if (
            licence_record.licence_record_relative_path != licence_record.relative_path
            or licence_record.licence_record_sha256 != licence_record.sha256
        ):
            raise ValueError(
                "The permission_or_licence_record must identify itself as the immutable "
                "licence record."
            )
        for asset in _iter_assets(self):
            if (
                asset.licence_record_relative_path != licence_record.relative_path
                or asset.licence_record_sha256 != licence_record.sha256
            ):
                raise ValueError(
                    f"{asset.relative_path}: asset licence identity does not match the "
                    "delivery permission_or_licence_record."
                )
        return self


def owner_delivery_cohort_gate(
    deliveries: tuple[FieldValidationOwnerDelivery, ...],
) -> dict[str, Any]:
    """Report whether structurally complete deliveries reach the cohort minimum.

    Passing this gate permits independent evidence review only. It never assigns
    partitions, registers trust, or authorizes a model prediction.
    """

    events = [event for delivery in deliveries for event in delivery.events]
    event_ids = [event.event_id for event in events]
    duplicate_event_ids = sorted(
        event_id for event_id in set(event_ids) if event_ids.count(event_id) > 1
    )
    observed = {
        "events": len(events),
        "independent_paths": len({event.path_id for event in events}),
        "mountains": len({event.mountain_id for event in events}),
        "storm_cycles": len({event.storm_cycle_id for event in events}),
    }
    required = {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 3,
    }
    checks = {
        key: observed[key] >= minimum for key, minimum in required.items()
    }
    checks["unique_event_ids_across_deliveries"] = not duplicate_event_ids
    return {
        "passed_for_independent_scientific_review": all(checks.values()),
        "checks": checks,
        "required": required,
        "observed": observed,
        "duplicate_event_ids": duplicate_event_ids,
        "partition_assigned": False,
        "predictions_authorized": False,
        "claim_boundary": (
            "A structurally complete delivery still requires provenance, licence, "
            "independence, CRS/datum, uncertainty, component-attribution, grouping, "
            "and trust review before any split or prediction."
        ),
    }


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must carry an explicit UTC offset.")
    return value


def _iter_assets(value: Any) -> Iterator[ImmutableAsset]:
    if isinstance(value, ImmutableAsset):
        yield value
    elif isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield from _iter_assets(getattr(value, field_name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_assets(item)


__all__ = [
    "OWNER_DELIVERY_SCHEMA_VERSION",
    "BoundedMeasurement",
    "CrsDatumLineage",
    "EventSurfaceDem",
    "FieldValidationOwnerDelivery",
    "FieldValidationOwnerEvent",
    "ImmutableAsset",
    "ObservedReleaseGeometry",
    "QuantifiedUncertainty",
    "RequiredObservationProvenance",
    "ReleaseDensityEvidence",
    "ReleaseThicknessEvidence",
    "SurveyCoverageDetection",
    "TerminalDenseFlowObservation",
    "owner_delivery_cohort_gate",
]
