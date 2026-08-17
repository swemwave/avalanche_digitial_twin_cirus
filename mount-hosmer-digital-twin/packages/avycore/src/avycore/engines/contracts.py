"""Portable contracts for deterministic avalanche-model engine plugins.

The contracts deliberately contain no geospatial or external-model imports.  A
serving process may inspect a baked result or an engine catalogue without
importing GDAL, rasterio, AvaFrame, SNOWPACK, or r.avaflow.  External adapters
live in ``app.processing`` and communicate with their models through files and
isolated subprocesses.

"Universal" in this module means that a site can supply a compatible declared
input pack.  It does not mean that an engine is valid in every avalanche regime
or location.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python

from avycore.hazard.constants import DISCLAIMER


ENGINE_CONTRACT_SCHEMA_VERSION = "avycore-engine-contract-v1"
NORMALIZED_RESULT_SCHEMA_VERSION = "avycore-normalized-result-v1"
NORMALIZED_COMPARISON_SCHEMA_VERSION = "avycore-normalized-comparison-v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"


def validate_research_disclaimer(value: str) -> str:
    normalized = value.lower()
    required = (
        "not an operational avalanche forecast",
        "not a calibrated avalanche probability",
        "field assessment",
    )
    if any(phrase not in normalized for phrase in required):
        raise ValueError("Engine results require the research/forecast/field disclaimer.")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineStage(StrEnum):
    SNOW_STATE = "snow_state"
    RELEASE = "release"
    RUNOUT = "runout"


class AvalancheRegime(StrEnum):
    DRY_SLAB = "dry_slab"
    DRY_LOOSE = "dry_loose"
    WET_SNOW = "wet_snow"
    DENSE_DRY = "dense_dry"
    POWDER_CLOUD = "powder_cloud"
    MIXED = "mixed"
    GLIDE = "glide"
    DEBRIS_FLOW = "debris_flow"


class ExecutionBoundary(StrEnum):
    IN_PROCESS_BASELINE = "in_process_baseline"
    OFFLINE_SUBPROCESS = "offline_subprocess"
    OFFLINE_CONTAINER = "offline_container"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"


class ValidationLevel(StrEnum):
    UNVERIFIED = "unverified"
    SOFTWARE_VERIFICATION_ONLY = "software_verification_only"
    CHARACTERIZED_NUMERICALLY = "characterized_numerically"
    PHYSICALLY_CALIBRATED = "physically_calibrated"
    INDEPENDENTLY_FIELD_VALIDATED = "independently_field_validated"


class InputKind(StrEnum):
    SCALAR = "scalar"
    FLAG = "flag"
    TEXT = "text"
    RASTER = "raster"
    VECTOR = "vector"
    PACK = "pack"


class OutputQuantity(StrEnum):
    SNOW_STATE = "snow_state"
    RELEASE_INDEX = "release_index"
    RELEASE_EXTENT = "release_extent"
    RELEASE_THICKNESS = "release_thickness"
    RELEASE_DENSITY = "release_density"
    RUNOUT_EXTENT = "runout_extent"
    FLOW_DEPTH = "flow_depth"
    FLOW_VELOCITY = "flow_velocity"
    FLOW_PRESSURE = "flow_pressure"


CANONICAL_OUTPUT_UNITS: dict[OutputQuantity, str] = {
    OutputQuantity.SNOW_STATE: "1",
    OutputQuantity.RELEASE_INDEX: "1",
    OutputQuantity.RELEASE_EXTENT: "1",
    OutputQuantity.RELEASE_THICKNESS: "m",
    OutputQuantity.RELEASE_DENSITY: "kg m-3",
    OutputQuantity.RUNOUT_EXTENT: "1",
    OutputQuantity.FLOW_DEPTH: "m",
    OutputQuantity.FLOW_VELOCITY: "m s-1",
    OutputQuantity.FLOW_PRESSURE: "kPa",
}


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON encoding used by replay identities."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


class ArtifactRef(StrictModel):
    """Immutable file reference.

    ``uri`` may be absolute for an input supplied to an offline runner and
    relative for a portable output bundle.  Adapters must resolve it against an
    explicitly supplied root and reject path escapes before opening the file.
    """

    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    byte_size: int = Field(gt=0)
    media_type: str = Field(min_length=1)


class CRSContract(StrictModel):
    definition: str = Field(min_length=1)
    projected: bool
    horizontal_unit: Literal["m", "degree"]
    coordinate_order: Literal["x,y", "longitude,latitude"]
    vertical_datum: str | None = None
    vertical_datum_status: Literal["known", "unknown", "mixed"]

    @model_validator(mode="after")
    def coordinate_semantics(self) -> "CRSContract":
        if self.projected:
            if self.horizontal_unit != "m" or self.coordinate_order != "x,y":
                raise ValueError("Projected model grids require metre units and x,y order.")
        elif self.horizontal_unit != "degree" or self.coordinate_order != "longitude,latitude":
            raise ValueError(
                "Geographic coordinates require degree units and longitude,latitude order."
            )
        if self.vertical_datum_status == "known" and not self.vertical_datum:
            raise ValueError("A known vertical datum must be named.")
        if self.vertical_datum_status != "known" and self.vertical_datum is not None:
            raise ValueError("Unknown or mixed vertical datum status cannot name one datum.")
        return self


class GridContract(StrictModel):
    crs: CRSContract
    shape: tuple[int, int]
    affine_transform: tuple[float, float, float, float, float, float]
    cell_size_x_m: float = Field(gt=0)
    cell_size_y_m: float = Field(gt=0)
    origin_semantics: Literal["upper_left_outer_corner", "lower_left_cell_center"]

    @field_validator("shape")
    @classmethod
    def nonempty_shape(cls, value: tuple[int, int]) -> tuple[int, int]:
        if len(value) != 2 or value[0] <= 0 or value[1] <= 0:
            raise ValueError("Grid shape must contain two positive dimensions.")
        return value

    @field_validator("affine_transform")
    @classmethod
    def finite_transform(
        cls, value: tuple[float, float, float, float, float, float]
    ) -> tuple[float, float, float, float, float, float]:
        if len(value) != 6 or not all(math.isfinite(item) for item in value):
            raise ValueError("Affine transform must contain six finite coefficients.")
        return value


class MaskContract(StrictModel):
    artifact: ArtifactRef
    dtype: Literal["bool"] = "bool"
    true_means: Literal["invalid_or_unknown"] = "invalid_or_unknown"
    valid_cells: int = Field(ge=0)
    masked_cells: int = Field(ge=0)
    combined_from: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def nonempty_population(self) -> "MaskContract":
        if self.valid_cells + self.masked_cells <= 0:
            raise ValueError("A mask must describe at least one grid cell.")
        return self


class RasterField(StrictModel):
    quantity: OutputQuantity
    unit: str
    artifact: ArtifactRef
    mask: MaskContract
    grid: GridContract
    dtype: str = Field(min_length=1)
    valid_min: float | None = None
    valid_max: float | None = None
    semantics: str = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_unit_and_grid_population(self) -> "RasterField":
        if self.unit != CANONICAL_OUTPUT_UNITS[self.quantity]:
            raise ValueError(
                f"{self.quantity.value} requires unit {CANONICAL_OUTPUT_UNITS[self.quantity]!r}."
            )
        cells = self.grid.shape[0] * self.grid.shape[1]
        if self.mask.valid_cells + self.mask.masked_cells != cells:
            raise ValueError("Mask cell counts do not match the raster grid.")
        if self.valid_min is not None and not math.isfinite(self.valid_min):
            raise ValueError("Raster valid_min must be finite when supplied.")
        if self.valid_max is not None and not math.isfinite(self.valid_max):
            raise ValueError("Raster valid_max must be finite when supplied.")
        if (
            self.valid_min is not None
            and self.valid_max is not None
            and self.valid_min > self.valid_max
        ):
            raise ValueError("Raster valid_min exceeds valid_max.")
        return self


class VectorField(StrictModel):
    quantity: OutputQuantity
    unit: str
    artifact: ArtifactRef
    crs: CRSContract
    geometry_types: tuple[Literal["Polygon", "MultiPolygon"], ...] = Field(min_length=1)
    feature_count: int = Field(ge=0)
    semantics: str = Field(min_length=1)

    @model_validator(mode="after")
    def canonical_unit(self) -> "VectorField":
        if self.unit != CANONICAL_OUTPUT_UNITS[self.quantity]:
            raise ValueError(
                f"{self.quantity.value} requires unit {CANONICAL_OUTPUT_UNITS[self.quantity]!r}."
            )
        return self


class InputRequirement(StrictModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: InputKind
    unit: str | None
    required: bool = True
    missing_policy: Literal["fail", "not_applicable"] = "fail"
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def unit_for_kind(self) -> "InputRequirement":
        if self.kind in {InputKind.SCALAR, InputKind.RASTER} and self.unit is None:
            raise ValueError("Numeric scalar and raster inputs require an explicit unit.")
        if self.kind in {InputKind.FLAG, InputKind.TEXT, InputKind.VECTOR, InputKind.PACK}:
            if self.unit is not None:
                raise ValueError(f"{self.kind.value} input declarations do not carry a scalar unit.")
        return self


class ParameterValidityRange(StrictModel):
    """Machine-checkable execution range, kept distinct from accuracy evidence."""

    input_name: str = Field(pattern=IDENTIFIER_PATTERN)
    unit: str
    lower: float | None = None
    upper: float | None = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True
    basis: Literal["adapter_execution_domain", "upstream_documentation", "calibration"]
    interpretation: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "ParameterValidityRange":
        if self.lower is None and self.upper is None:
            raise ValueError("A parameter validity range requires at least one finite bound.")
        values = tuple(value for value in (self.lower, self.upper) if value is not None)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Parameter validity bounds must be finite.")
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError("Parameter lower bound exceeds upper bound.")
        return self


class SpatialApplicability(StrictModel):
    input_names: tuple[str, ...] = Field(min_length=1)
    require_projected_metre_crs: bool
    require_same_crs: bool
    coordinate_order: Literal["x,y", "longitude,latitude"]
    interpretation: str = Field(min_length=1)

    @field_validator("input_names")
    @classmethod
    def unique_input_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Spatial applicability input names must be unique.")
        return value


class DeclaredInput(StrictModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: InputKind
    unit: str | None = None
    value: bool | int | float | str | None = None
    artifact: ArtifactRef | None = None
    grid: GridContract | None = None
    crs: CRSContract | None = None
    mask: MaskContract | None = None
    status: Literal["provided", "missing"]
    source_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def value_shape(self) -> "DeclaredInput":
        if self.status == "missing":
            if any(item is not None for item in (self.value, self.artifact, self.grid, self.crs, self.mask)):
                raise ValueError("A missing input cannot carry a value, artifact, grid, CRS, or mask.")
            return self
        if self.kind in {InputKind.RASTER, InputKind.VECTOR, InputKind.PACK}:
            if self.artifact is None or self.value is not None:
                raise ValueError("Spatial and pack inputs require exactly one artifact.")
        elif self.value is None or self.artifact is not None:
            raise ValueError("Scalar, flag, and text inputs require exactly one inline value.")
        if self.kind == InputKind.RASTER:
            if self.grid is None or self.mask is None or self.crs is not None:
                raise ValueError("Raster inputs require an explicit grid and mask, and no separate CRS.")
        elif self.kind == InputKind.VECTOR:
            if self.crs is None or self.grid is not None or self.mask is not None:
                raise ValueError("Vector inputs require an explicit CRS, and no grid or raster mask.")
        elif any(item is not None for item in (self.grid, self.crs, self.mask)):
            raise ValueError("Only spatial inputs may carry grid, CRS, or mask metadata.")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Input values must be finite.")
        return self


class ValidationStatus(StrictModel):
    level: ValidationLevel
    evidence: tuple[str, ...] = Field(min_length=1)
    eligible_field_events: int = Field(ge=0)
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def field_claim_gate(self) -> "ValidationStatus":
        if self.level == ValidationLevel.INDEPENDENTLY_FIELD_VALIDATED:
            if self.eligible_field_events <= 0:
                raise ValueError("Field-validation status requires eligible field events.")
        elif self.eligible_field_events != 0:
            raise ValueError(
                "Non-field-validated engine status cannot publish eligible field events."
            )
        return self


class EngineDescriptor(StrictModel):
    schema_version: Literal["avycore-engine-contract-v1"]
    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    display_name: str = Field(min_length=1)
    stage: EngineStage
    implementation_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    execution_boundary: ExecutionBoundary
    source_url: str = Field(min_length=1)
    license_spdx: str = Field(min_length=1)
    supported_regimes: tuple[AvalancheRegime, ...] = Field(min_length=1)
    required_inputs: tuple[InputRequirement, ...] = Field(min_length=1)
    parameter_validity: tuple[ParameterValidityRange, ...] = ()
    spatial_applicability: tuple[SpatialApplicability, ...] = ()
    output_capabilities: tuple[OutputQuantity, ...] = Field(min_length=1)
    deterministic: bool
    selection_priority: int = Field(ge=0)
    applicability: tuple[str, ...] = Field(min_length=1)
    validation: ValidationStatus
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_declarations(self) -> "EngineDescriptor":
        input_names = [item.name for item in self.required_inputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("Engine input requirement names must be unique.")
        if len(self.supported_regimes) != len(set(self.supported_regimes)):
            raise ValueError("Engine supported regimes must be unique.")
        if len(self.output_capabilities) != len(set(self.output_capabilities)):
            raise ValueError("Engine output capabilities must be unique.")
        declared = set(input_names)
        range_names = [item.input_name for item in self.parameter_validity]
        if len(range_names) != len(set(range_names)):
            raise ValueError("Engine parameter validity declarations must be unique.")
        referenced = set(range_names)
        for spatial in self.spatial_applicability:
            referenced.update(spatial.input_names)
        if not referenced <= declared:
            raise ValueError("Applicability constraints reference undeclared engine inputs.")
        return self


class EngineAvailability(StrictModel):
    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    status: AvailabilityStatus
    reason: str = Field(min_length=1)
    detected_version: str | None = None
    executable_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def available_has_version(self) -> "EngineAvailability":
        if self.status == AvailabilityStatus.AVAILABLE and not self.detected_version:
            raise ValueError("An available external engine must report its detected version.")
        return self


class EngineRunRequest(StrictModel):
    schema_version: Literal["avycore-engine-contract-v1"]
    site_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_disclaimer: str = Field(min_length=80)
    stage: EngineStage
    regime: AvalancheRegime
    inputs: tuple[DeclaredInput, ...]
    requested_outputs: tuple[OutputQuantity, ...] = Field(min_length=1)
    requested_engine_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    scenario_sha256: str = Field(pattern=SHA256_PATTERN)
    seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def unique_request_fields(self) -> "EngineRunRequest":
        names = [item.name for item in self.inputs]
        if len(names) != len(set(names)):
            raise ValueError("Engine request input names must be unique.")
        if len(self.requested_outputs) != len(set(self.requested_outputs)):
            raise ValueError("Requested outputs must be unique.")
        return self

    @field_validator("research_disclaimer")
    @classmethod
    def safe_disclaimer(cls, value: str) -> str:
        return validate_research_disclaimer(value)

    def input_map(self) -> dict[str, DeclaredInput]:
        return {item.name: item for item in self.inputs}


class UncertaintyBound(StrictModel):
    parameter: str = Field(pattern=IDENTIFIER_PATTERN)
    unit: str = Field(min_length=1)
    lower: float
    central: float
    upper: float
    basis: Literal["source", "literature", "expert", "numerical"]
    source: str = Field(min_length=1)
    interpretation: Literal["bounded_sensitivity_not_probability"] = (
        "bounded_sensitivity_not_probability"
    )

    @model_validator(mode="after")
    def ordered_finite_bounds(self) -> "UncertaintyBound":
        values = (self.lower, self.central, self.upper)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Uncertainty bounds must be finite.")
        if not self.lower <= self.central <= self.upper:
            raise ValueError("Uncertainty bounds must satisfy lower <= central <= upper.")
        return self


class RunProvenance(StrictModel):
    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    engine_version: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    license_spdx: str = Field(min_length=1)
    execution_boundary: ExecutionBoundary
    executable_sha256: str = Field(pattern=SHA256_PATTERN)
    environment_sha256: str = Field(pattern=SHA256_PATTERN)
    adapter_sha256: str = Field(pattern=SHA256_PATTERN)
    selection_sha256: str = Field(pattern=SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=SHA256_PATTERN)
    input_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    output_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    scenario_sha256: str = Field(pattern=SHA256_PATTERN)
    seed: int | None = Field(default=None, ge=0)
    source_urls: tuple[str, ...] = Field(min_length=1)


class _NormalizedResultBase(StrictModel):
    schema_version: Literal["avycore-normalized-result-v1"]
    result_id: str
    site_id: str = Field(pattern=IDENTIFIER_PATTERN)
    disclaimer: str = Field(min_length=80)
    regime: AvalancheRegime
    provenance: RunProvenance
    validation: ValidationStatus
    uncertainty: tuple[UncertaintyBound, ...]
    warnings: tuple[str, ...]
    limitations: tuple[str, ...] = Field(min_length=1)

    @field_validator("disclaimer")
    @classmethod
    def research_disclaimer(cls, value: str) -> str:
        return validate_research_disclaimer(value)

    def _identity_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"result_id"})


class NormalizedSnowStateResult(_NormalizedResultBase):
    result_id: str = Field(pattern=r"^snow-state-result-[0-9a-f]{64}$")
    stage: Literal[EngineStage.SNOW_STATE]
    snow_state_pack: ArtifactRef
    variables: tuple[RasterField, ...]

    @model_validator(mode="after")
    def identity(self) -> "NormalizedSnowStateResult":
        _check_result_identity(self, "snow-state-result")
        return self


class NormalizedReleaseResult(_NormalizedResultBase):
    result_id: str = Field(pattern=r"^release-result-[0-9a-f]{64}$")
    stage: Literal[EngineStage.RELEASE]
    release_extent: RasterField
    release_polygons: VectorField | None
    release_index: RasterField | None
    release_thickness: RasterField | None
    release_density: RasterField | None
    release_area_m2: float = Field(ge=0)
    release_volume_m3: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def quantities_and_identity(self) -> "NormalizedReleaseResult":
        if self.release_extent.quantity != OutputQuantity.RELEASE_EXTENT:
            raise ValueError("release_extent has the wrong normalized quantity.")
        if self.release_index and self.release_index.quantity != OutputQuantity.RELEASE_INDEX:
            raise ValueError("release_index has the wrong normalized quantity.")
        if self.release_thickness and self.release_thickness.quantity != OutputQuantity.RELEASE_THICKNESS:
            raise ValueError("release_thickness has the wrong normalized quantity.")
        if self.release_density and self.release_density.quantity != OutputQuantity.RELEASE_DENSITY:
            raise ValueError("release_density has the wrong normalized quantity.")
        if self.release_volume_m3 is not None and self.release_thickness is None:
            raise ValueError("Release volume requires an explicit release-thickness output.")
        _check_result_identity(self, "release-result")
        return self


class NormalizedRunoutResult(_NormalizedResultBase):
    result_id: str = Field(pattern=r"^runout-result-[0-9a-f]{64}$")
    stage: Literal[EngineStage.RUNOUT]
    runout_extent: RasterField
    runout_polygons: VectorField | None
    flow_depth: RasterField | None
    flow_velocity: RasterField | None
    flow_pressure: RasterField | None
    runout_area_m2: float = Field(ge=0)
    aoi_status: Literal["complete_within_domain", "truncated_at_domain", "unknown"]

    @model_validator(mode="after")
    def quantities_and_identity(self) -> "NormalizedRunoutResult":
        expected = (
            (self.runout_extent, OutputQuantity.RUNOUT_EXTENT),
            (self.flow_depth, OutputQuantity.FLOW_DEPTH),
            (self.flow_velocity, OutputQuantity.FLOW_VELOCITY),
            (self.flow_pressure, OutputQuantity.FLOW_PRESSURE),
        )
        for field, quantity in expected:
            if field is not None and field.quantity != quantity:
                raise ValueError(f"{quantity.value} output has the wrong normalized quantity.")
        if self.aoi_status == "truncated_at_domain" and not self.warnings:
            raise ValueError("A domain-truncated run must carry a visible warning.")
        _check_result_identity(self, "runout-result")
        return self


NormalizedResult = NormalizedSnowStateResult | NormalizedReleaseResult | NormalizedRunoutResult


class ComparisonMetric(StrictModel):
    name: str = Field(pattern=IDENTIFIER_PATTERN)
    quantity: OutputQuantity
    unit: str = Field(min_length=1)
    status: Literal["available", "not_applicable", "unsupported"]
    value: float | None
    valid_cells: int = Field(ge=0)
    semantics: str = Field(min_length=1)

    @model_validator(mode="after")
    def available_value(self) -> "ComparisonMetric":
        if self.status == "available":
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("An available comparison metric requires a finite value.")
            if self.valid_cells <= 0:
                raise ValueError("An available comparison metric requires common valid cells.")
        elif self.value is not None:
            raise ValueError("A non-available comparison metric cannot publish a value.")
        return self


class NormalizedComparisonResult(StrictModel):
    schema_version: Literal["avycore-normalized-comparison-v1"]
    comparison_id: str = Field(pattern=r"^comparison-result-[0-9a-f]{64}$")
    site_id: str = Field(pattern=IDENTIFIER_PATTERN)
    disclaimer: str = Field(min_length=80)
    left_result_id: str = Field(pattern=r"^runout-result-[0-9a-f]{64}$")
    right_result_id: str = Field(pattern=r"^runout-result-[0-9a-f]{64}$")
    left_engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    right_engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    comparator_version: str = Field(min_length=1)
    grid: GridContract
    common_mask: MaskContract
    metrics: tuple[ComparisonMetric, ...] = Field(min_length=1)
    warnings: tuple[str, ...]
    limitations: tuple[str, ...] = Field(min_length=1)

    @field_validator("disclaimer")
    @classmethod
    def research_disclaimer(cls, value: str) -> str:
        return validate_research_disclaimer(value)

    @model_validator(mode="after")
    def identity(self) -> "NormalizedComparisonResult":
        cells = self.grid.shape[0] * self.grid.shape[1]
        if self.common_mask.valid_cells + self.common_mask.masked_cells != cells:
            raise ValueError("Comparison mask cell counts do not match the comparison grid.")
        payload = self.model_dump(mode="json", exclude={"comparison_id"})
        expected = f"comparison-result-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"
        if self.comparison_id != expected:
            raise ValueError("Normalized comparison identity does not match its content.")
        return self


def _check_result_identity(result: _NormalizedResultBase, prefix: str) -> None:
    expected = f"{prefix}-{hashlib.sha256(canonical_json_bytes(result._identity_payload())).hexdigest()}"
    if result.result_id != expected:
        raise ValueError("Normalized result identity does not match its content.")


def build_result(model: type[NormalizedResult], content: dict[str, Any]) -> NormalizedResult:
    """Build a normalized result and derive its immutable identity.

    ``model`` is explicit so callers cannot let untrusted payload content choose a
    Pydantic class.  The stage/result prefix pairing remains deterministic.
    """

    prefixes: dict[type[Any], str] = {
        NormalizedSnowStateResult: "snow-state-result",
        NormalizedReleaseResult: "release-result",
        NormalizedRunoutResult: "runout-result",
    }
    if model not in prefixes:
        raise TypeError("Unsupported normalized result model.")
    without_identity = dict(content)
    without_identity.pop("result_id", None)
    normalized = to_jsonable_python(without_identity)
    identity = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
    return model.model_validate(
        {**without_identity, "result_id": f"{prefixes[model]}-{identity}"}
    )


def build_comparison(content: dict[str, Any]) -> NormalizedComparisonResult:
    without_identity = dict(content)
    without_identity.pop("comparison_id", None)
    normalized = to_jsonable_python(without_identity)
    identity = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
    return NormalizedComparisonResult.model_validate(
        {**without_identity, "comparison_id": f"comparison-result-{identity}"}
    )


def sha256_of_manifest(items: dict[str, Any]) -> str:
    """Hash a canonical manifest while rejecting NaN and unstable ordering."""

    return hashlib.sha256(canonical_json_bytes(items)).hexdigest()


def assert_identifier(value: str) -> str:
    """Small public helper for adapters before a value reaches a file path."""

    if re.fullmatch(IDENTIFIER_PATTERN, value) is None:
        raise ValueError(f"Invalid engine identifier: {value!r}")
    return value


__all__ = [
    "ArtifactRef",
    "AvailabilityStatus",
    "AvalancheRegime",
    "CANONICAL_OUTPUT_UNITS",
    "CRSContract",
    "DeclaredInput",
    "DISCLAIMER",
    "ENGINE_CONTRACT_SCHEMA_VERSION",
    "EngineAvailability",
    "EngineDescriptor",
    "EngineRunRequest",
    "EngineStage",
    "ExecutionBoundary",
    "GridContract",
    "InputKind",
    "InputRequirement",
    "MaskContract",
    "ComparisonMetric",
    "NormalizedComparisonResult",
    "NORMALIZED_COMPARISON_SCHEMA_VERSION",
    "NORMALIZED_RESULT_SCHEMA_VERSION",
    "NormalizedReleaseResult",
    "NormalizedResult",
    "NormalizedRunoutResult",
    "NormalizedSnowStateResult",
    "OutputQuantity",
    "ParameterValidityRange",
    "RasterField",
    "RunProvenance",
    "SpatialApplicability",
    "UncertaintyBound",
    "ValidationLevel",
    "ValidationStatus",
    "VectorField",
    "build_result",
    "build_comparison",
    "canonical_json_bytes",
    "sha256_of_manifest",
    "validate_research_disclaimer",
]
