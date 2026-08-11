"""Strict, content-addressed output contract for isolated snow models."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SNOW_STATE_PACK_SCHEMA_VERSION = "mount-hosmer-snow-state-pack-v2"
SCIENTIFIC_REPLAY_SCHEMA_VERSION = "mount-hosmer-snow-scientific-replay-v1"
DISCLAIMER = (
    "Experimental research prototype only; not an operational avalanche forecast, not a "
    "probability, and never a replacement for Avalanche Canada guidance or field assessment."
)
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SnowVariable = Literal["snow_height", "snow_water_equivalent", "surface_temperature"]
UNITS = {
    "snow_height": "m",
    "snow_water_equivalent": "kg m-2",
    "surface_temperature": "K",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC.")
    if value.minute or value.second or value.microsecond:
        raise ValueError(f"{name} must lie exactly on a UTC hour.")
    return value


class SnowModelIdentity(StrictModel):
    engine: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=SHA256_PATTERN)
    binary_inventory_sha256: str = Field(pattern=SHA256_PATTERN)
    executable_version_output: str = Field(min_length=1)
    configuration_sha256: str = Field(pattern=SHA256_PATTERN)
    input_inventory_sha256: str = Field(pattern=SHA256_PATTERN)
    adapter_code_sha256: str = Field(pattern=SHA256_PATTERN)
    output_parser_version: str = Field(min_length=1)
    command_argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class SnowInputLineage(StrictModel):
    condition_id: str = Field(pattern=r"^condition-[0-9a-f]{64}$")
    condition_normalized_output_sha256: str = Field(pattern=SHA256_PATTERN)
    condition_pack_file_sha256: str = Field(pattern=SHA256_PATTERN)
    bake_sha256: str = Field(pattern=SHA256_PATTERN)
    reference_elevation_id: str = Field(pattern=r"^reference-elevation-[0-9a-f]{64}$")
    reference_elevation_file_sha256: str = Field(pattern=SHA256_PATTERN)
    forcing_adapter_version: Literal["condition-pack-to-smet-v1"]
    forcing_file_sha256: str = Field(pattern=SHA256_PATTERN)
    forcing_file_bytes: int = Field(gt=0)


class SnowTerrainContract(StrictModel):
    coordinate_order: Literal["longitude,latitude"]
    longitude_deg: float = Field(ge=-180, le=180)
    latitude_deg: float = Field(ge=-90, le=90)
    reference_elevation_m: float
    slope_angle_deg: float = Field(ge=0, le=90)
    slope_aspect_deg_true: float = Field(ge=0, lt=360)
    roughness_length_m: float | None = Field(default=None, gt=0)
    canopy_height_m: float | None = Field(default=None, ge=0)
    vertical_datum_status: Literal["known", "unknown"]
    geometry_use: Literal["isolated_offline_model_input_not_activated"]

    @field_validator("reference_elevation_m")
    @classmethod
    def finite_elevation(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Reference elevation must be finite.")
        return value


class SnowStateTimes(StrictModel):
    start_utc: datetime
    end_utc: datetime
    cadence_seconds: Literal[3600]

    @field_validator("start_utc", "end_utc")
    @classmethod
    def utc_hours(cls, value: datetime, info: Any) -> datetime:
        return _utc(value, info.field_name)

    @model_validator(mode="after")
    def ordered(self) -> "SnowStateTimes":
        if self.end_utc < self.start_utc:
            raise ValueError("Snow-state end precedes start.")
        return self

    def timestamps(self) -> tuple[datetime, ...]:
        count = int((self.end_utc - self.start_utc).total_seconds() // 3600) + 1
        return tuple(self.start_utc + timedelta(hours=index) for index in range(count))


class SnowStateValue(StrictModel):
    time_utc: datetime
    value: float | None
    masked: bool
    status: Literal["modeled", "missing"]
    output_field: str = Field(min_length=1)

    @field_validator("time_utc")
    @classmethod
    def exact_utc_hour(cls, value: datetime) -> datetime:
        return _utc(value, "time_utc")

    @model_validator(mode="after")
    def missingness(self) -> "SnowStateValue":
        if self.status == "missing":
            if self.value is not None or not self.masked:
                raise ValueError("Missing snow state must remain null and masked.")
        elif self.value is None or self.masked or not math.isfinite(self.value):
            raise ValueError("Modeled snow state must be finite and unmasked.")
        return self


class SnowStateSeries(StrictModel):
    variable: SnowVariable
    unit: str
    semantics: str = Field(min_length=1)
    values: tuple[SnowStateValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_unit_and_bounds(self) -> "SnowStateSeries":
        if self.unit != UNITS[self.variable]:
            raise ValueError("Snow-state variable uses a non-canonical unit.")
        for item in self.values:
            if item.value is None:
                continue
            if self.variable in {"snow_height", "snow_water_equivalent"} and item.value < 0:
                raise ValueError("Snow height and SWE cannot be negative.")
            if self.variable == "surface_temperature" and item.value <= 0:
                raise ValueError("Surface temperature must be greater than zero kelvin.")
        return self


class ProcessEvidence(StrictModel):
    exit_code: Literal[0]
    stdout_sha256: str = Field(pattern=SHA256_PATTERN)
    stderr_sha256: str = Field(pattern=SHA256_PATTERN)
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    output_file_sha256: str = Field(pattern=SHA256_PATTERN)
    normalized_output_sha256: str = Field(pattern=SHA256_PATTERN)
    output_file_bytes: int = Field(gt=0)


class SnowStatePackDraft(StrictModel):
    schema_version: Literal["mount-hosmer-snow-state-pack-v2"]
    disclaimer: Literal[
        "Experimental research prototype only; not an operational avalanche forecast, not a probability, and never a replacement for Avalanche Canada guidance or field assessment."
    ]
    activation_status: Literal["isolated_offline_not_served_not_assessed"]
    model: SnowModelIdentity
    input_lineage: SnowInputLineage
    terrain: SnowTerrainContract
    times: SnowStateTimes
    variables: dict[SnowVariable, SnowStateSeries]
    process: ProcessEvidence
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_exact_timeline(self) -> "SnowStatePackDraft":
        required = set(UNITS)
        if set(self.variables) != required:
            raise ValueError("SnowStatePack variable set is incomplete.")
        timestamps = self.times.timestamps()
        for name in sorted(required):
            series = self.variables[name]  # type: ignore[index]
            if series.variable != name:
                raise ValueError("Snow-state series key conflicts with variable name.")
            if tuple(item.time_utc for item in series.values) != timestamps:
                raise ValueError("Snow-state series must exactly cover the ordered UTC timeline.")
        return self


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def scientific_replay_sha256(draft: SnowStatePackDraft | dict[str, Any]) -> str:
    """Identity of scientific inputs and normalized values, excluding run noise.

    The full SnowStatePack remains an immutable run artifact: absolute executable
    location, argv, timeout, raw output, stdout, and stderr all affect
    ``snow_state_id``.  They intentionally do not affect this replay identity.
    """

    validated = (
        draft if isinstance(draft, SnowStatePackDraft) else SnowStatePackDraft.model_validate(draft)
    )
    model = validated.model
    payload = {
        "schema_version": SCIENTIFIC_REPLAY_SCHEMA_VERSION,
        "model": {
            "engine": model.engine,
            "executable_sha256": model.executable_sha256,
            "binary_inventory_sha256": model.binary_inventory_sha256,
            "configuration_sha256": model.configuration_sha256,
            "input_inventory_sha256": model.input_inventory_sha256,
            "adapter_code_sha256": model.adapter_code_sha256,
            "output_parser_version": model.output_parser_version,
        },
        "input_lineage": validated.input_lineage.model_dump(mode="json"),
        "terrain": validated.terrain.model_dump(mode="json"),
        "times": validated.times.model_dump(mode="json"),
        "variables": {
            name: validated.variables[name].model_dump(mode="json")
            for name in sorted(validated.variables)
        },
        "normalized_output_sha256": validated.process.normalized_output_sha256,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


class SnowStatePack(SnowStatePackDraft):
    scientific_replay_sha256: str = Field(pattern=SHA256_PATTERN)
    snow_state_id: str = Field(pattern=r"^snow-state-[0-9a-f]{64}$")

    @model_validator(mode="after")
    def content_identity(self) -> "SnowStatePack":
        draft_content = self.model_dump(
            mode="json", exclude={"snow_state_id", "scientific_replay_sha256"}
        )
        draft = SnowStatePackDraft.model_validate(draft_content)
        expected_replay = scientific_replay_sha256(draft)
        if self.scientific_replay_sha256 != expected_replay:
            raise ValueError("SnowStatePack scientific replay identity does not match content.")
        artifact = {**draft_content, "scientific_replay_sha256": expected_replay}
        expected = f"snow-state-{hashlib.sha256(_canonical(artifact)).hexdigest()}"
        if self.snow_state_id != expected:
            raise ValueError("SnowStatePack identity does not match content.")
        return self


def build_snow_state_pack(draft: SnowStatePackDraft | dict[str, Any]) -> SnowStatePack:
    validated = draft if isinstance(draft, SnowStatePackDraft) else SnowStatePackDraft.model_validate(draft)
    content = validated.model_dump(mode="json")
    replay_identity = scientific_replay_sha256(validated)
    artifact = {**content, "scientific_replay_sha256": replay_identity}
    identity = hashlib.sha256(_canonical(artifact)).hexdigest()
    return SnowStatePack.model_validate({**artifact, "snow_state_id": f"snow-state-{identity}"})


def canonical_snow_state_pack_bytes(pack: SnowStatePack) -> bytes:
    validated = SnowStatePack.model_validate(pack.model_dump(mode="json"))
    return _canonical(validated.model_dump(mode="json")) + b"\n"
