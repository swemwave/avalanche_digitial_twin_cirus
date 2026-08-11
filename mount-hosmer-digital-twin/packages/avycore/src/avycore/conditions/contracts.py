"""Strict, versioned and replayable hourly meteorological forcing contract."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .units import CANONICAL_UNITS


CONDITION_PACK_SCHEMA_VERSION = "mount-hosmer-condition-pack-v1"
REQUIRED_VARIABLES = tuple(CANONICAL_UNITS)
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
PRECIPITATION_PHASES = {"rain", "snow", "mixed", "freezing_rain", "none", "unknown"}

VariableName = Literal[
    "air_temperature",
    "relative_humidity",
    "wind_speed",
    "wind_direction",
    "precipitation_phase",
    "precipitation_amount",
    "surface_pressure",
    "shortwave_radiation",
    "longwave_radiation",
]
ValueStatus = Literal["observed", "analyzed", "forecast", "gap_filled", "missing"]


class ConditionPackError(ValueError):
    """Raised when normalized conditions are ambiguous, incomplete, or modified."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC.")
    return value


class MountainGridIdentity(StrictModel):
    mountain_pack_id: str = Field(pattern=ID_PATTERN)
    mountain_pack_sha256: str = Field(pattern=SHA256_PATTERN)
    grid_sha256: str = Field(pattern=SHA256_PATTERN)
    crs: str = Field(min_length=1)
    axis_order: Literal["easting_northing"]
    horizontal_units: Literal["metre"]
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)
    resolution_m: float = Field(gt=0)

    @field_validator("resolution_m")
    @classmethod
    def finite_resolution(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Grid resolution must be finite.")
        return value


class SourceStatement(StrictModel):
    provider_id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    source_uri: str | None = None
    licence: str = Field(min_length=1)
    licence_uri: str | None = None
    permitted_use: str = Field(min_length=1)


class ConditionTimes(StrictModel):
    acquisition_start_utc: datetime
    acquisition_end_utc: datetime
    publication_time_utc: datetime
    valid_start_utc: datetime
    valid_end_utc: datetime
    staleness_reference_time_utc: datetime
    cadence_seconds: Literal[3600]

    @field_validator(
        "acquisition_start_utc",
        "acquisition_end_utc",
        "publication_time_utc",
        "valid_start_utc",
        "valid_end_utc",
        "staleness_reference_time_utc",
    )
    @classmethod
    def utc_only(cls, value: datetime, info: Any) -> datetime:
        return _require_utc(value, info.field_name)

    @model_validator(mode="after")
    def ordered_hourly_bounds(self) -> "ConditionTimes":
        if self.acquisition_end_utc < self.acquisition_start_utc:
            raise ValueError("Acquisition end must not precede acquisition start.")
        if self.valid_end_utc < self.valid_start_utc:
            raise ValueError("Valid end must not precede valid start.")
        for name in ("valid_start_utc", "valid_end_utc"):
            value = getattr(self, name)
            if value.minute or value.second or value.microsecond:
                raise ValueError(f"{name} must lie exactly on a UTC hour.")
        return self

    def hourly_timestamps(self) -> tuple[datetime, ...]:
        count = int((self.valid_end_utc - self.valid_start_utc).total_seconds() // 3600) + 1
        return tuple(self.valid_start_utc + timedelta(hours=index) for index in range(count))


class Station(StrictModel):
    station_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    longitude_deg: float = Field(ge=-180, le=180)
    latitude_deg: float = Field(ge=-90, le=90)
    elevation_m: float
    coordinate_source: str = Field(min_length=1)
    horizontal_uncertainty_m: float | None = Field(default=None, ge=0)
    elevation_uncertainty_m: float | None = Field(default=None, ge=0)

    @field_validator(
        "longitude_deg",
        "latitude_deg",
        "elevation_m",
        "horizontal_uncertainty_m",
        "elevation_uncertainty_m",
    )
    @classmethod
    def finite_coordinates(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Station coordinates, elevation, and uncertainty must be finite.")
        return value


class SourceFile(StrictModel):
    source_file_id: str = Field(pattern=ID_PATTERN)
    locator: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)


class Transformation(StrictModel):
    transformation_id: str = Field(pattern=ID_PATTERN)
    method: str = Field(min_length=1)
    version: str = Field(min_length=1)
    code_sha256: str = Field(pattern=SHA256_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)


class VariableProvenance(StrictModel):
    kind: Literal["direct", "derived"]
    method: str = Field(min_length=1)
    version: str = Field(min_length=1)
    source_variables: tuple[str, ...] = ()
    citation: str = Field(min_length=1)
    assumptions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def document_derivation(self) -> "VariableProvenance":
        if self.kind == "derived" and not self.source_variables:
            raise ValueError("A derived variable must name its source variables.")
        return self


class QCFlag(StrictModel):
    code: str = Field(min_length=1)
    severity: Literal["information", "accepted", "suspect", "rejected"]
    source: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class ValueUncertainty(StrictModel):
    status: Literal["quantified", "unknown", "not_provided"]
    standard_uncertainty: float | None = Field(default=None, ge=0)
    unit: str | None = None
    basis: str = Field(min_length=1)

    @field_validator("standard_uncertainty")
    @classmethod
    def finite_uncertainty(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Quantified uncertainty must be finite.")
        return value

    @model_validator(mode="after")
    def consistent_status(self) -> "ValueUncertainty":
        if self.status == "quantified" and (
            self.standard_uncertainty is None or not self.unit
        ):
            raise ValueError("Quantified uncertainty requires a value and explicit unit.")
        if self.status != "quantified" and (
            self.standard_uncertainty is not None or self.unit is not None
        ):
            raise ValueError("Unknown/not-provided uncertainty cannot carry a numeric value or unit.")
        return self


class ValueStaleness(StrictModel):
    status: Literal["fresh", "stale", "unknown", "not_applicable"]
    age_seconds: int | None = Field(default=None, ge=0)
    threshold_seconds: int | None = Field(default=None, gt=0)
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_status(self) -> "ValueStaleness":
        if self.status in {"fresh", "stale"}:
            if self.age_seconds is None or self.threshold_seconds is None:
                raise ValueError("Fresh/stale status requires age_seconds and threshold_seconds.")
            is_stale = self.age_seconds > self.threshold_seconds
            if is_stale != (self.status == "stale"):
                raise ValueError("Staleness status conflicts with its age and threshold.")
        elif self.age_seconds is not None or self.threshold_seconds is not None:
            raise ValueError("Unknown/not-applicable staleness cannot carry age or threshold.")
        return self


class ValueLineage(StrictModel):
    source_file_sha256: str = Field(pattern=SHA256_PATTERN)
    source_record_id: str = Field(min_length=1)
    transformation_ids: tuple[str, ...] = ()


class ConditionValue(StrictModel):
    station_id: str = Field(pattern=ID_PATTERN)
    time_utc: datetime
    value: float | str | None
    masked: bool
    status: ValueStatus
    qc_flags: tuple[QCFlag, ...] = ()
    uncertainty: ValueUncertainty
    staleness: ValueStaleness
    lineage: tuple[ValueLineage, ...] = Field(min_length=1)

    @field_validator("time_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        value = _require_utc(value, "time_utc")
        if value.minute or value.second or value.microsecond:
            raise ValueError("Condition values must lie exactly on a UTC hour.")
        return value

    @model_validator(mode="after")
    def preserve_missingness(self) -> "ConditionValue":
        if self.status == "missing":
            if self.value is not None or not self.masked:
                raise ValueError("Missing values must be null and explicitly masked.")
            if self.uncertainty.status == "quantified":
                raise ValueError("Missing values cannot carry quantified value uncertainty.")
            if self.staleness.status not in {"unknown", "not_applicable"}:
                raise ValueError("Missing values require unknown or not-applicable staleness.")
        elif self.value is None or self.masked:
            raise ValueError("Present values require a non-null value and masked=false.")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Present numeric values must be finite.")
        if self.status == "gap_filled" and not any(
            item.transformation_ids for item in self.lineage
        ):
            raise ValueError("Gap-filled values require transformation lineage.")
        return self


class VariableSeries(StrictModel):
    variable: VariableName
    unit: str = Field(min_length=1)
    provenance: VariableProvenance
    values: tuple[ConditionValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_values(self) -> "VariableSeries":
        expected_unit = CANONICAL_UNITS[self.variable]
        if self.unit != expected_unit:
            raise ValueError(
                f"{self.variable} must use canonical unit {expected_unit!r}, got {self.unit!r}."
            )
        for item in self.values:
            if item.value is None:
                continue
            if self.variable == "precipitation_phase":
                if not isinstance(item.value, str) or item.value not in PRECIPITATION_PHASES:
                    raise ValueError(
                        f"Precipitation phase must be one of {sorted(PRECIPITATION_PHASES)}."
                    )
                continue
            if isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
                raise ValueError(f"{self.variable} requires numeric values.")
            value = float(item.value)
            if self.variable == "air_temperature" and value <= 0:
                raise ValueError("Air temperature in kelvin must be greater than zero.")
            if self.variable == "relative_humidity" and not 0 <= value <= 100:
                raise ValueError("Relative humidity must be in [0, 100] percent.")
            if self.variable == "wind_direction" and not 0 <= value < 360:
                raise ValueError("Wind direction must be in [0, 360) meteorological degrees true.")
            if self.variable in {
                "wind_speed",
                "precipitation_amount",
                "shortwave_radiation",
                "longwave_radiation",
            } and value < 0:
                raise ValueError(f"{self.variable} cannot be negative.")
            if self.variable == "surface_pressure" and value <= 0:
                raise ValueError("Surface pressure must be greater than zero pascals.")
        return self


class NormalizationMetadata(StrictModel):
    software: str = Field(min_length=1)
    software_version: str = Field(min_length=1)
    method: str = Field(min_length=1)
    code_sha256: str = Field(pattern=SHA256_PATTERN)


class ConditionPackDraft(StrictModel):
    schema_version: Literal[CONDITION_PACK_SCHEMA_VERSION]
    mountain_grid: MountainGridIdentity
    source: SourceStatement
    times: ConditionTimes
    stations: tuple[Station, ...] = Field(min_length=1)
    source_files: tuple[SourceFile, ...] = Field(min_length=1)
    transformations: tuple[Transformation, ...] = ()
    normalization: NormalizationMetadata
    variables: dict[VariableName, VariableSeries]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_complete_pack(self) -> "ConditionPackDraft":
        if tuple(sorted(station.station_id for station in self.stations)) != tuple(
            station.station_id for station in self.stations
        ):
            raise ValueError("Stations must be ordered by station_id for deterministic replay.")
        station_ids = [station.station_id for station in self.stations]
        if len(station_ids) != len(set(station_ids)):
            raise ValueError("station_id values must be unique.")
        source_ids = [item.source_file_id for item in self.source_files]
        source_hashes = [item.sha256 for item in self.source_files]
        if tuple(sorted(source_ids)) != tuple(source_ids):
            raise ValueError("Source files must be ordered by source_file_id for deterministic replay.")
        if len(source_ids) != len(set(source_ids)) or len(source_hashes) != len(set(source_hashes)):
            raise ValueError("Source file IDs and SHA-256 values must be unique.")
        transformation_ids = [item.transformation_id for item in self.transformations]
        if tuple(sorted(transformation_ids)) != tuple(transformation_ids):
            raise ValueError(
                "Transformations must be ordered by transformation_id for deterministic replay."
            )
        if len(transformation_ids) != len(set(transformation_ids)):
            raise ValueError("transformation_id values must be unique.")

        missing_variables = sorted(set(REQUIRED_VARIABLES) - set(self.variables))
        extra_variables = sorted(set(self.variables) - set(REQUIRED_VARIABLES))
        if missing_variables or extra_variables:
            raise ValueError(
                f"Condition Pack variable set is incomplete; missing={missing_variables}, "
                f"unexpected={extra_variables}."
            )

        expected_pairs = tuple(
            (station_id, timestamp)
            for station_id in station_ids
            for timestamp in self.times.hourly_timestamps()
        )
        known_hashes = set(source_hashes)
        known_transformations = set(transformation_ids)
        for name in REQUIRED_VARIABLES:
            series = self.variables[name]  # type: ignore[index]
            if series.variable != name:
                raise ValueError(f"Variable key {name!r} conflicts with series name.")
            actual_pairs = tuple((item.station_id, item.time_utc) for item in series.values)
            if actual_pairs != expected_pairs:
                raise ValueError(
                    f"{name} must contain exactly one value per station and UTC hour in "
                    "station_id/time order."
                )
            for item in series.values:
                if item.uncertainty.status == "quantified" and (
                    item.uncertainty.unit != series.unit
                ):
                    raise ValueError(
                        f"{name} uncertainty must use the normalized series unit {series.unit!r}."
                    )
                for lineage in item.lineage:
                    if lineage.source_file_sha256 not in known_hashes:
                        raise ValueError(f"{name} references an undeclared source-file hash.")
                    unknown = set(lineage.transformation_ids) - known_transformations
                    if unknown:
                        raise ValueError(
                            f"{name} references undeclared transformations: {sorted(unknown)}."
                        )
        return self


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def normalized_output_sha256(draft: ConditionPackDraft) -> str:
    payload = draft.model_dump(mode="json")
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


class ConditionPack(ConditionPackDraft):
    condition_id: str = Field(pattern=r"^condition-[0-9a-f]{64}$")
    normalized_output_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def verify_identity(self) -> "ConditionPack":
        draft = ConditionPackDraft.model_validate(
            self.model_dump(
                mode="json", exclude={"condition_id", "normalized_output_sha256"}
            )
        )
        expected_hash = normalized_output_sha256(draft)
        if self.normalized_output_sha256 != expected_hash:
            raise ValueError("normalized_output_sha256 does not match normalized content.")
        if self.condition_id != f"condition-{expected_hash}":
            raise ValueError("condition_id does not match normalized content.")
        return self


def build_condition_pack(draft: ConditionPackDraft | dict[str, Any]) -> ConditionPack:
    """Validate normalized content and add its immutable, content-derived identity."""

    validated = (
        draft if isinstance(draft, ConditionPackDraft) else ConditionPackDraft.model_validate(draft)
    )
    output_hash = normalized_output_sha256(validated)
    return ConditionPack.model_validate(
        {
            **validated.model_dump(mode="json"),
            "condition_id": f"condition-{output_hash}",
            "normalized_output_sha256": output_hash,
        }
    )


def canonical_condition_pack_bytes(pack: ConditionPack) -> bytes:
    """Return stable UTF-8 bytes for storage and byte-for-byte replay checks."""

    validated = ConditionPack.model_validate(pack.model_dump(mode="json"))
    return _canonical_json_bytes(validated.model_dump(mode="json")) + b"\n"
