"""Strict, deterministic user-entered avalanche research scenarios.

This contract is intentionally separate from :mod:`avycore.conditions`, whose
``ConditionPack`` represents offline hourly meteorological forcing.  A scenario
is an explicit user-entered research hypothesis.  Only the four inputs already
used by the characterized release/runout model are numerically active; all other
records are retained as context and reported as unsupported by the current model.

Spatial applicability is evaluated at baked grid-cell centres.  Missing coverage
stays missing: it is returned as a condition-support mask which the release model
must intersect with its terrain-input mask.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .advisories import advisory_summary, build_advisories
from .hazard.conditions import Conditions, RELEASE_SIZES, snow_fraction
from .hazard.protocols import Terrain


SCENARIO_SCHEMA_VERSION = "mount-hosmer-observation-scenario-v1"
SCENARIO_CANONICALIZATION_VERSION = "canonical-json-v1"
SCOPE_RASTERIZATION_VERSION = "baked-cell-centres-wgs84-v1"

InputCategory = Literal[
    "weather_loading",
    "snowpack_weak_layers",
    "field_observations",
    "release_assumptions",
    "flow_runout",
]
InputStatus = Literal["measured", "estimated", "assumed", "unknown"]
ScenarioMode = Literal["simple", "advanced"]
ScenarioClassification = Literal[
    "terrain_only",
    "hypothetical",
    "partially_observation_constrained",
    "fully_specified_research_scenario",
]
ParameterName = Literal[
    "new_snow_depth",
    "wind_speed",
    "wind_direction",
    "air_temperature",
    "snow_depth",
    "snow_water_equivalent",
    "slab_depth",
    "weak_layer_depth",
    "weak_layer_type",
    "stability_test_result",
    "recent_avalanche_activity",
    "shooting_cracks",
    "whumpfing",
    "release_size",
    "trigger_type",
    "release_depth",
    "flow_regime",
    "avalanche_density",
    "runout_alpha_angle",
]


class ScenarioContractError(ValueError):
    """Raised when a scenario is ambiguous or cannot be evaluated safely."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParameterDefinition(StrictModel):
    category: InputCategory
    unit: str
    label: str
    model_role: Literal["active", "advisory", "unsupported_context"]
    required: bool = False
    value_type: Literal["number", "category", "boolean"]
    minimum: float | None = None
    maximum: float | None = None
    maximum_exclusive: bool = False
    allowed_values: tuple[str, ...] = ()
    description: str


PARAMETER_CATALOG: dict[str, ParameterDefinition] = {
    "new_snow_depth": ParameterDefinition(
        category="weather_loading",
        unit="cm",
        label="New storm-snow depth",
        model_role="active",
        required=True,
        value_type="number",
        minimum=0.0,
        maximum=300.0,
        description=(
            "User-entered storm-snow depth. The current uncalibrated loading term saturates at "
            "50 cm; the 300 cm limit is an input-contract bound, not a physical maximum."
        ),
    ),
    "wind_speed": ParameterDefinition(
        category="weather_loading",
        unit="km/h",
        label="Representative transport wind speed",
        model_role="active",
        required=True,
        value_type="number",
        minimum=0.0,
        maximum=200.0,
        description=(
            "Representative wind speed used by the dry-snow transport rule; transport is zero "
            "through 15 km/h and reaches its configured maximum at 40 km/h."
        ),
    ),
    "wind_direction": ParameterDefinition(
        category="weather_loading",
        unit="degree_true",
        label="Wind direction (FROM)",
        model_role="active",
        required=True,
        value_type="number",
        minimum=0.0,
        maximum=360.0,
        maximum_exclusive=True,
        description="Meteorological direction from which the wind blows, clockwise from true north.",
    ),
    "release_size": ParameterDefinition(
        category="release_assumptions",
        unit="category",
        label="Release-size class",
        model_role="active",
        required=True,
        value_type="category",
        allowed_values=tuple(RELEASE_SIZES),
        description=(
            "Explicit runout sensitivity assumption selecting the configured regional alpha angle; "
            "it is not an observed release probability."
        ),
    ),
    "air_temperature": ParameterDefinition(
        category="weather_loading", unit="degC", label="Air temperature",
        model_role="active", value_type="number", minimum=-60.0, maximum=40.0,
        description=(
            "Classifies how much of the new precipitation is snow rather than rain, using the "
            "same 0-2 degC band as the offline forcing work. Rain does not build a dry storm "
            "slab, so the loading term is reduced -- which is NOT a finding that a warm day is "
            "safer. Leave it blank to reproduce the temperature-free result exactly."
        ),
    ),
    "snow_depth": ParameterDefinition(
        category="snowpack_weak_layers", unit="cm", label="Total snow depth",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; not mapped into the current release equation.",
    ),
    "snow_water_equivalent": ParameterDefinition(
        category="snowpack_weak_layers", unit="mm", label="Snow water equivalent",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; not mapped into the current release equation.",
    ),
    "slab_depth": ParameterDefinition(
        category="snowpack_weak_layers", unit="cm", label="Slab depth",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; the current model has no slab-layer mechanics.",
    ),
    "weak_layer_depth": ParameterDefinition(
        category="snowpack_weak_layers", unit="cm", label="Weak-layer depth",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; the current model cannot resolve weak-layer failure.",
    ),
    "weak_layer_type": ParameterDefinition(
        category="snowpack_weak_layers", unit="category", label="Weak-layer type",
        model_role="advisory", value_type="category",
        description="Recorded as context; no weak-layer type changes the current score.",
    ),
    "stability_test_result": ParameterDefinition(
        category="snowpack_weak_layers", unit="category", label="Stability-test result",
        model_role="advisory", value_type="category",
        description="Recorded as context; ECT/PST/compression results are not numerically ingested.",
    ),
    "recent_avalanche_activity": ParameterDefinition(
        category="field_observations", unit="boolean", label="Recent avalanche activity",
        model_role="advisory", value_type="boolean",
        description="Important field context that the current model cannot incorporate.",
    ),
    "shooting_cracks": ParameterDefinition(
        category="field_observations", unit="boolean", label="Shooting cracks",
        model_role="advisory", value_type="boolean",
        description="Important field context that the current model cannot incorporate.",
    ),
    "whumpfing": ParameterDefinition(
        category="field_observations", unit="boolean", label="Whumpfing",
        model_role="advisory", value_type="boolean",
        description="Important field context that the current model cannot incorporate.",
    ),
    "trigger_type": ParameterDefinition(
        category="release_assumptions", unit="category", label="Trigger type",
        model_role="advisory", value_type="category",
        description="Recorded as context; trigger type does not alter the current release index.",
    ),
    "release_depth": ParameterDefinition(
        category="release_assumptions", unit="cm", label="Release depth",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; not a calibrated input to release or runout.",
    ),
    "flow_regime": ParameterDefinition(
        category="flow_runout", unit="category", label="Flow regime",
        model_role="active", value_type="category",
        allowed_values=("dry_slab", "wet_snow", "powder", "mixed"),
        description=(
            "Selects the published friction set and alpha shift the runout engines use. These "
            "remain dry-snow engines pointed at different UNCALIBRATED constants -- selecting "
            "'powder' does not add powder-cloud physics. Blank keeps the dry-slab defaults."
        ),
    ),
    "avalanche_density": ParameterDefinition(
        category="flow_runout", unit="kg/m3", label="Avalanche density",
        model_role="advisory", value_type="number", minimum=0.0,
        description="Recorded as context; density is not used by the current runout engines.",
    ),
    "runout_alpha_angle": ParameterDefinition(
        category="flow_runout", unit="degree", label="User alpha angle",
        model_role="active", value_type="number", minimum=15.0, maximum=40.0,
        description=(
            "Replaces the configured regional angle of reach for a sensitivity run, clamped to a "
            "reviewed 15-40 degree envelope. A smaller angle runs further. Still UNCALIBRATED, "
            "exactly like the default it replaces; blank keeps the regional value."
        ),
    ),
}

ACTIVE_PARAMETERS = ("new_snow_depth", "wind_speed", "wind_direction", "release_size")


class InputSource(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "measurement",
        "derived_from_measurement",
        "model_or_analysis",
        "expert_judgment",
        "user_assumption",
        "other",
    ]
    reference: str | None = Field(default=None, max_length=500)
    method: str | None = Field(default=None, max_length=1000)


class InputUncertainty(StrictModel):
    kind: Literal["quantified", "range", "qualitative", "unknown", "not_provided"]
    value: float | None = Field(default=None, ge=0)
    lower: float | None = None
    upper: float | None = None
    unit: str | None = None
    basis: str = Field(min_length=1, max_length=1000)

    @field_validator("value", "lower", "upper")
    @classmethod
    def finite_values(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Uncertainty values must be finite.")
        return value

    @model_validator(mode="after")
    def consistent_kind(self) -> "InputUncertainty":
        if self.kind == "quantified":
            if self.value is None or not self.unit or self.lower is not None or self.upper is not None:
                raise ValueError("Quantified uncertainty requires value and unit only.")
        elif self.kind == "range":
            if self.lower is None or self.upper is None or not self.unit or self.lower > self.upper:
                raise ValueError("Range uncertainty requires ordered lower/upper values and a unit.")
            if self.value is not None:
                raise ValueError("Range uncertainty cannot also carry a standard value.")
        elif any(item is not None for item in (self.value, self.lower, self.upper, self.unit)):
            raise ValueError(
                "Qualitative/unknown/not-provided uncertainty cannot carry numeric bounds or a unit."
            )
        return self


class SpatialScope(StrictModel):
    kind: Literal[
        "whole_area",
        "elevation_band",
        "aspect_band",
        "elevation_aspect_band",
        "drawn_area",
    ] = "whole_area"
    elevation_min_m: float | None = None
    elevation_max_m: float | None = None
    aspect_min_deg: float | None = None
    aspect_max_deg: float | None = None
    geometry: dict[str, Any] | None = None

    @field_validator("elevation_min_m", "elevation_max_m", "aspect_min_deg", "aspect_max_deg")
    @classmethod
    def finite_scope_values(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("Spatial-scope bounds must be finite.")
        return value

    @model_validator(mode="after")
    def consistent_scope(self) -> "SpatialScope":
        needs_elevation = self.kind in {"elevation_band", "elevation_aspect_band"}
        needs_aspect = self.kind in {"aspect_band", "elevation_aspect_band"}
        if needs_elevation:
            if self.elevation_min_m is None or self.elevation_max_m is None:
                raise ValueError("Elevation scopes require minimum and maximum elevations in metres.")
            if self.elevation_min_m > self.elevation_max_m:
                raise ValueError("Elevation scope minimum cannot exceed its maximum.")
        elif self.elevation_min_m is not None or self.elevation_max_m is not None:
            raise ValueError("Elevation bounds are only valid for elevation scopes.")
        if needs_aspect:
            if self.aspect_min_deg is None or self.aspect_max_deg is None:
                raise ValueError("Aspect scopes require minimum and maximum degrees true.")
            if not (0 <= self.aspect_min_deg < 360 and 0 <= self.aspect_max_deg < 360):
                raise ValueError("Aspect bounds must be in [0, 360) degrees true.")
        elif self.aspect_min_deg is not None or self.aspect_max_deg is not None:
            raise ValueError("Aspect bounds are only valid for aspect scopes.")
        if self.kind == "drawn_area":
            _validate_wgs84_geometry(self.geometry)
        elif self.geometry is not None:
            raise ValueError("Geometry is only valid for a drawn-area scope.")
        return self


class ScenarioInput(StrictModel):
    input_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    category: InputCategory
    parameter: ParameterName
    value: float | str | bool | None = None
    unit: str = Field(min_length=1)
    status: InputStatus
    observed_at_utc: datetime | None = None
    source: InputSource | None = None
    uncertainty: InputUncertainty
    spatial_scope: SpatialScope = Field(default_factory=SpatialScope)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("observed_at_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("observed_at_utc must be timezone-aware UTC.")
        return value

    @model_validator(mode="after")
    def validate_against_catalog(self) -> "ScenarioInput":
        definition = PARAMETER_CATALOG[self.parameter]
        if self.category != definition.category:
            raise ValueError(
                f"{self.parameter} belongs to {definition.category}, not {self.category}."
            )
        if self.unit != definition.unit:
            raise ValueError(
                f"{self.parameter} requires canonical unit {definition.unit!r}, got {self.unit!r}."
            )
        if self.status == "unknown":
            if self.value is not None:
                raise ValueError("Unknown inputs must carry value=null; zero is a measured/assumed value.")
            if self.uncertainty.kind in {"quantified", "range"}:
                raise ValueError("Unknown inputs cannot carry quantified uncertainty.")
            return self
        if self.value is None:
            raise ValueError("Measured, estimated, and assumed inputs require a value.")
        if self.status in {"measured", "estimated"}:
            if self.observed_at_utc is None or self.source is None:
                raise ValueError("Measured/estimated inputs require a UTC timestamp and source.")
            if self.status == "measured" and self.source.kind != "measurement":
                raise ValueError("Measured inputs require source.kind='measurement'.")
        _validate_parameter_value(self.parameter, self.value, definition)
        if self.uncertainty.kind in {"quantified", "range"} and (
            self.uncertainty.unit != definition.unit
        ):
            raise ValueError("Quantified input uncertainty must use the input's canonical unit.")
        return self


class Scenario(StrictModel):
    schema_version: Literal[SCENARIO_SCHEMA_VERSION] = SCENARIO_SCHEMA_VERSION
    mode: ScenarioMode = "advanced"
    name: str | None = Field(default=None, max_length=200)
    valid_at_utc: datetime | None = None
    inputs: tuple[ScenarioInput, ...]

    @field_validator("valid_at_utc")
    @classmethod
    def valid_time_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("valid_at_utc must be timezone-aware UTC.")
        return value

    @model_validator(mode="after")
    def unambiguous_inputs(self) -> "Scenario":
        ids = [item.input_id for item in self.inputs]
        if len(ids) != len(set(ids)):
            raise ValueError("Scenario input_id values must be unique.")
        active = [item.parameter for item in self.inputs if item.parameter in ACTIVE_PARAMETERS]
        if len(active) != len(set(active)):
            raise ValueError(
                "The current model supports one record per active parameter; spatial overrides are "
                "not yet supported."
            )
        return self


@dataclass(frozen=True)
class ResolvedScenario:
    scenario: Scenario
    conditions: Conditions | None
    condition_coverage: np.ndarray | None
    can_compute: bool
    classification: ScenarioClassification
    report: dict[str, Any]
    scenario_sha256: str


def parameter_catalog_manifest() -> dict[str, Any]:
    """Return the versioned public input catalog for model identity/reporting."""

    return {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "scope_rasterization_version": SCOPE_RASTERIZATION_VERSION,
        "parameters": {
            name: definition.model_dump(mode="json")
            for name, definition in sorted(PARAMETER_CATALOG.items())
        },
    }


def scenario_sha256(scenario: Scenario) -> str:
    """Content identity independent of input-list order and JSON key order."""

    payload = scenario.model_dump(mode="json")
    payload["inputs"] = sorted(payload["inputs"], key=lambda item: item["input_id"])
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scenario_from_legacy_conditions(conditions: Conditions) -> Scenario:
    """Make an explicit hypothetical scenario without changing legacy numerics."""

    normalized = conditions.clamped()
    return scenario_from_simple_values(
        new_snow_cm=normalized.new_snow_cm,
        wind_speed_kmh=normalized.wind_speed_kmh,
        wind_direction_deg=normalized.wind_direction_deg,
        release_size=normalized.release_size,
        air_temperature_c=normalized.air_temperature_c,
        flow_regime=normalized.flow_regime,
        alpha_angle_override_deg=normalized.alpha_angle_override_deg,
    )


def scenario_from_simple_values(
    *,
    new_snow_cm: float | None,
    wind_speed_kmh: float | None,
    wind_direction_deg: float | None,
    release_size: str | None,
    air_temperature_c: float | None = None,
    flow_regime: str | None = None,
    alpha_angle_override_deg: float | None = None,
) -> Scenario:
    """Build the simple UI/legacy adapter while preserving omitted values as unknown."""

    source = InputSource(name="User-entered simple controls", kind="user_assumption")
    provided_uncertainty = InputUncertainty(
        kind="not_provided", basis="No uncertainty was provided."
    )
    unknown_uncertainty = InputUncertainty(
        kind="unknown", basis="The input value is unknown or was omitted."
    )
    scope = SpatialScope(kind="whole_area")

    def numeric(
        input_id: str,
        parameter: Literal["new_snow_depth", "wind_speed", "wind_direction"],
        value: float | None,
    ) -> ScenarioInput:
        definition = PARAMETER_CATALOG[parameter]
        return ScenarioInput(
            input_id=input_id,
            category="weather_loading",
            parameter=parameter,
            value=value,
            unit=definition.unit,
            status="assumed" if value is not None else "unknown",
            source=source if value is not None else None,
            uncertainty=provided_uncertainty if value is not None else unknown_uncertainty,
            spatial_scope=scope,
            notes="Compatibility adapter for the simple scenario controls.",
        )

    def optional(input_id: str, parameter: str, value, category: str) -> ScenarioInput | None:
        """Only emit an optional active input when the caller actually supplied it."""
        if value is None:
            return None
        definition = PARAMETER_CATALOG[parameter]
        return ScenarioInput(
            input_id=input_id,
            category=category,
            parameter=parameter,
            value=value,
            unit=definition.unit,
            status="assumed",
            source=source,
            uncertainty=provided_uncertainty,
            spatial_scope=scope,
            notes="Compatibility adapter for the simple scenario controls.",
        )

    release_value = release_size if release_size is not None else "medium"
    optional_inputs = [
        item
        for item in (
            optional("simple-air-temperature", "air_temperature", air_temperature_c, "weather_loading"),
            optional("simple-flow-regime", "flow_regime", flow_regime, "flow_runout"),
            optional("simple-alpha-angle", "runout_alpha_angle", alpha_angle_override_deg, "flow_runout"),
        )
        if item is not None
    ]
    return Scenario(
        mode="simple",
        inputs=(
            numeric("simple-new-snow", "new_snow_depth", new_snow_cm),
            numeric("simple-wind-speed", "wind_speed", wind_speed_kmh),
            numeric("simple-wind-direction", "wind_direction", wind_direction_deg),
            ScenarioInput(
                input_id="simple-release-size", category="release_assumptions",
                parameter="release_size", value=release_value, unit="category",
                status="assumed", source=source, uncertainty=provided_uncertainty,
                spatial_scope=scope,
            ),
            *optional_inputs,
        ),
    )


def resolve_scenario(terrain: Terrain, scenario: Scenario) -> ResolvedScenario:
    """Resolve active values and their joint spatial support against a baked terrain."""

    by_parameter = {item.parameter: item for item in scenario.inputs}
    wind_speed = _known_number(by_parameter.get("wind_speed"))
    direction_required = wind_speed is None or wind_speed > 15.0
    required = ["new_snow_depth", "wind_speed", "release_size"]
    if direction_required:
        required.insert(2, "wind_direction")

    missing: list[str] = []
    support_masks: dict[str, np.ndarray | None] = {}
    scope_cache: dict[str, np.ndarray | None] = {}
    per_input_coverage: dict[str, dict[str, Any]] = {}
    for parameter in required:
        record = by_parameter.get(parameter)
        if record is None or record.status == "unknown":
            missing.append(parameter)
            continue
        scope_key = json.dumps(
            record.spatial_scope.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if scope_key not in scope_cache:
            scope_cache[scope_key] = spatial_scope_mask(terrain, record.spatial_scope)
        support = scope_cache[scope_key]
        support_masks[parameter] = support
        cells = terrain.grid.shape[0] * terrain.grid.shape[1]
        valid_cells = cells if support is None else int(np.count_nonzero(support))
        per_input_coverage[parameter] = {
            "grid_cell_count": cells,
            "supported_cell_count": valid_cells,
            "unsupported_cell_count": cells - valid_cells,
            "supported_fraction": round(valid_cells / cells, 6) if cells else 0.0,
            "scope": record.spatial_scope.model_dump(mode="json"),
        }

    can_compute = not missing
    joint: np.ndarray | None = None
    if can_compute:
        for support in support_masks.values():
            if support is None:
                continue
            joint = support.copy() if joint is None else joint & support
        if joint is not None and not np.any(joint):
            can_compute = False
            missing.append("joint_spatial_coverage")

    if can_compute:
        new_snow = _known_number(by_parameter.get("new_snow_depth"))
        speed = _known_number(by_parameter.get("wind_speed"))
        release_size = _known_category(by_parameter.get("release_size"))
        direction = _known_number(by_parameter.get("wind_direction"))
        # Direction is mathematically irrelevant while the configured transport
        # factor is zero.  Use a deterministic inert placeholder without claiming
        # it is an observation; the report keeps it unknown/not-required.
        if direction is None and speed is not None and speed <= 15.0:
            direction = 0.0
        assert new_snow is not None and speed is not None and direction is not None
        assert release_size is not None
        # Optional active inputs. Each stays None when the user did not supply it,
        # and None reproduces the historical numbers exactly -- an omitted value is
        # never turned into a default assumption.
        conditions = Conditions(
            new_snow,
            speed,
            direction,
            release_size,
            air_temperature_c=_known_number(by_parameter.get("air_temperature")),
            flow_regime=_known_category(by_parameter.get("flow_regime")),
            alpha_angle_override_deg=_known_number(by_parameter.get("runout_alpha_angle")),
        ).clamped()
    else:
        conditions = None

    classification, basis = _classify_scenario(
        by_parameter, can_compute=can_compute, condition_coverage=joint,
        direction_required=direction_required,
    )
    # Advisory records never enter an equation, so they are reported separately --
    # and, unlike before, they now produce deterministic advisories rather than
    # being listed and forgotten.
    advisory_records = {
        item.parameter: item
        for item in scenario.inputs
        if PARAMETER_CATALOG[item.parameter].model_role in {"advisory", "active"}
        and item.status != "unknown"
    }
    advisories = build_advisories(
        advisory_records,
        new_snow_cm=_known_number(by_parameter.get("new_snow_depth")),
        snow_fraction=snow_fraction(_known_number(by_parameter.get("air_temperature"))),
    )
    unsupported = [
        _input_report(item, used=False)
        for item in scenario.inputs
        if PARAMETER_CATALOG[item.parameter].model_role in {"advisory", "unsupported_context"}
        and item.status != "unknown"
    ]
    inputs_report = []
    for item in sorted(scenario.inputs, key=lambda value: value.input_id):
        used = can_compute and item.parameter in required and item.status != "unknown"
        inputs_report.append(_input_report(item, used=used))

    cells = terrain.grid.shape[0] * terrain.grid.shape[1]
    joint_cells = 0 if not can_compute else cells if joint is None else int(np.count_nonzero(joint))
    known_required = len(required) - len([name for name in missing if name in required])
    assumptions = [
        f"{PARAMETER_CATALOG[item.parameter].label}: {item.value} {item.unit}"
        for item in scenario.inputs
        if item.status == "assumed" and item.value is not None
    ]
    warnings: list[str] = []
    if missing:
        warnings.append(
            "Condition-dependent release and runout are unavailable because required inputs or "
            f"joint spatial support are missing: {', '.join(missing)}. Missing values were not "
            "converted to zero."
        )
    if unsupported:
        warnings.append(
            f"{len(unsupported)} supplied snowpack, field, release, or flow record(s) are retained "
            "as context but do not change this model's numerical output."
        )
    red_flags = [
        item.parameter for item in scenario.inputs
        if item.parameter in {"recent_avalanche_activity", "shooting_cracks", "whumpfing"}
        and item.status != "unknown" and item.value is True
    ]
    if red_flags:
        warnings.append(
            "Field red flags were supplied but are not incorporated into the index. They remain "
            "essential field-assessment information and must not be discounted by this result."
        )
    report = {
        "schema_version": SCENARIO_SCHEMA_VERSION,
        "mode": scenario.mode,
        "classification": classification,
        "classification_basis": basis,
        "conditions_used": can_compute,
        "result_scope": (
            "supported_area_only" if can_compute and joint is not None else
            "whole_area" if can_compute else "unavailable_missing_inputs"
        ),
        "completeness": {
            "required_parameters": required,
            "required_input_count": len(required),
            "known_required_input_count": known_required,
            "unknown_required_parameters": [name for name in missing if name in required],
            "provenance_complete_count": sum(
                1 for name in required
                if (record := by_parameter.get(name)) is not None
                and record.status != "unknown"
                and (record.status == "assumed" or (
                    record.source is not None and record.observed_at_utc is not None
                ))
            ),
            "unsupported_supplied_input_count": len(unsupported),
        },
        "condition_coverage": {
            "denominator": "All cells in the rectangular baked analysis grid.",
            "grid_cell_count": cells,
            "joint_supported_cell_count": joint_cells,
            "joint_unsupported_cell_count": cells - joint_cells,
            "joint_supported_fraction": round(joint_cells / cells, 6) if cells else 0.0,
            "per_input": per_input_coverage,
            "scope_rasterization_version": SCOPE_RASTERIZATION_VERSION,
            "cell_inclusion_rule": "Baked grid-cell centre lies within the supplied scope.",
        },
        "inputs": inputs_report,
        "unsupported_inputs": unsupported,
        "advisories": [item.to_dict() for item in advisories],
        "advisory_summary": advisory_summary(advisories),
        "assumptions": assumptions,
        "uncertainty_propagated": False,
        "uncertainty_statement": (
            "Supplied input uncertainty is preserved as provenance but is not statistically "
            "propagated by the current deterministic model."
        ),
        "warnings": warnings,
        "limitations": [
            "Fully specified means complete for the implemented simplified model, not a complete "
            "snowpack description, physical validation, probability, or operational forecast.",
            "Snowpack, weak-layer, stability-test, red-flag, and extra flow-property records are "
            "not numerically supported by the current model.",
            "Elevation scopes inherit the bake's documented unknown/mixed vertical-datum limitation.",
        ],
    }
    identity = scenario_sha256(scenario)
    return ResolvedScenario(
        scenario=scenario,
        conditions=conditions,
        condition_coverage=joint,
        can_compute=can_compute,
        classification=classification,
        report=report,
        scenario_sha256=identity,
    )


def spatial_scope_mask(terrain: Terrain, scope: SpatialScope) -> np.ndarray | None:
    """Return a boolean support mask, or ``None`` for whole-area support."""

    if scope.kind == "whole_area":
        return None
    mask = np.ones(terrain.grid.shape, dtype=bool)
    if scope.kind in {"elevation_band", "elevation_aspect_band"}:
        elevation = terrain.layer("elevation")
        values = np.asarray(elevation.filled(np.nan), dtype="float64")
        mask &= ~np.ma.getmaskarray(elevation) & np.isfinite(values)
        mask &= values >= float(scope.elevation_min_m)
        mask &= values <= float(scope.elevation_max_m)
    if scope.kind in {"aspect_band", "elevation_aspect_band"}:
        aspect = terrain.layer("aspect")
        values = np.asarray(aspect.filled(np.nan), dtype="float64")
        mask &= ~np.ma.getmaskarray(aspect) & np.isfinite(values)
        lower = float(scope.aspect_min_deg)
        upper = float(scope.aspect_max_deg)
        if lower <= upper:
            mask &= (values >= lower) & (values <= upper)
        else:
            mask &= (values >= lower) | (values <= upper)
    if scope.kind == "drawn_area":
        return _drawn_area_mask(terrain, scope.geometry)
    return mask


def _drawn_area_mask(terrain: Terrain, geometry_value: dict[str, Any] | None) -> np.ndarray:
    import shapely
    from shapely.geometry import shape

    geometry = shape(geometry_value)
    if geometry.is_empty or not geometry.is_valid:
        raise ScenarioContractError("Drawn-area geometry must be non-empty and valid.")
    rows, cols = terrain.grid.shape
    output = np.zeros((rows, cols), dtype=bool)
    col_centres = np.arange(cols, dtype="float64") + 0.5
    # Chunk the real 5.8 M-cell grid so WGS84 longitude/latitude arrays do not
    # materially increase the already characterized assessment memory peak.
    for row_start in range(0, rows, 128):
        row_stop = min(rows, row_start + 128)
        rr, cc = np.meshgrid(
            np.arange(row_start, row_stop, dtype="float64") + 0.5,
            col_centres,
            indexing="ij",
        )
        lon, lat = terrain.reproject(cc, rr)
        output[row_start:row_stop] = shapely.intersects_xy(geometry, lon, lat)
    return output


def _classify_scenario(
    by_parameter: dict[str, ScenarioInput],
    *,
    can_compute: bool,
    condition_coverage: np.ndarray | None,
    direction_required: bool,
) -> tuple[ScenarioClassification, list[str]]:
    loading = [by_parameter.get(name) for name in ("new_snow_depth", "wind_speed")]
    known_loading = [item for item in loading if item is not None and item.status != "unknown"]
    if not known_loading:
        return "terrain_only", [
            "No evaluable current loading was supplied; condition-dependent release/runout is unavailable."
        ]
    weather_names = ["new_snow_depth", "wind_speed"] + (
        ["wind_direction"] if direction_required else []
    )
    weather = [by_parameter.get(name) for name in weather_names]
    observation_constrained = [item for item in weather if _is_observation_constrained(item)]
    all_observation_constrained = all(_is_observation_constrained(item) for item in weather)
    whole_area = condition_coverage is None
    if can_compute and all_observation_constrained and whole_area:
        return "fully_specified_research_scenario", [
            "Every weather/loading input used by the current model is measured or derived from a "
            "measurement with complete whole-area applicability.",
            "Release size remains an explicit runout sensitivity assumption.",
        ]
    if observation_constrained:
        return "partially_observation_constrained", [
            "At least one active weather/loading input is measurement-traceable, but another active "
            "input is assumed/unknown or spatial applicability is incomplete."
        ]
    return "hypothetical", [
        "No active weather/loading input is measurement-traceable; values are assumptions or "
        "non-observational estimates."
    ]


def _is_observation_constrained(item: ScenarioInput | None) -> bool:
    if item is None or item.status == "unknown" or item.source is None:
        return False
    return item.status == "measured" or (
        item.status == "estimated" and item.source.kind == "derived_from_measurement"
    )


def _known_number(item: ScenarioInput | None) -> float | None:
    if item is None or item.status == "unknown" or item.value is None:
        return None
    if isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
        raise ScenarioContractError(f"{item.parameter} must be numeric.")
    return float(item.value)


def _known_category(item: ScenarioInput | None) -> str | None:
    if item is None or item.status == "unknown" or item.value is None:
        return None
    if not isinstance(item.value, str):
        raise ScenarioContractError(f"{item.parameter} must be categorical.")
    return item.value


def _input_report(item: ScenarioInput, *, used: bool) -> dict[str, Any]:
    definition = PARAMETER_CATALOG[item.parameter]
    return {
        **item.model_dump(mode="json"),
        "label": definition.label,
        "model_role": definition.model_role,
        "used_in_computation": used,
        "support_statement": definition.description,
    }


def _validate_parameter_value(
    parameter: str, value: float | str | bool, definition: ParameterDefinition
) -> None:
    if definition.value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{parameter} requires a numeric value.")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError(f"{parameter} must be finite.")
        if definition.minimum is not None and numeric < definition.minimum:
            raise ValueError(f"{parameter} must be at least {definition.minimum} {definition.unit}.")
        if definition.maximum is not None:
            invalid = numeric >= definition.maximum if definition.maximum_exclusive else numeric > definition.maximum
            if invalid:
                comparator = "less than" if definition.maximum_exclusive else "at most"
                raise ValueError(
                    f"{parameter} must be {comparator} {definition.maximum} {definition.unit}."
                )
    elif definition.value_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{parameter} requires a boolean value.")
    else:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{parameter} requires a non-empty categorical value.")
        if definition.allowed_values and value not in definition.allowed_values:
            raise ValueError(
                f"{parameter} must be one of {list(definition.allowed_values)}, got {value!r}."
            )


def _validate_wgs84_geometry(value: dict[str, Any] | None) -> None:
    if not isinstance(value, dict) or value.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Drawn-area geometry must be a WGS84 Polygon or MultiPolygon.")
    coordinates = value.get("coordinates")
    points: list[tuple[float, float]] = []

    def visit(node: Any) -> None:
        if (
            isinstance(node, (list, tuple)) and len(node) >= 2
            and isinstance(node[0], (int, float)) and not isinstance(node[0], bool)
            and isinstance(node[1], (int, float)) and not isinstance(node[1], bool)
        ):
            lon, lat = float(node[0]), float(node[1])
            if not math.isfinite(lon) or not math.isfinite(lat):
                raise ValueError("Drawn-area coordinates must be finite.")
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Drawn-area coordinates must use WGS84 [longitude, latitude].")
            points.append((lon, lat))
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item)

    visit(coordinates)
    if len(points) < 4:
        raise ValueError("Drawn-area geometry must contain a closed polygon ring.")
    if len(points) > 2000:
        raise ValueError("Drawn-area geometry is limited to 2000 vertices per scenario input.")
    from shapely.geometry import shape

    geometry = shape(value)
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("Drawn-area geometry must be non-empty, closed, and topologically valid.")


__all__ = [
    "ACTIVE_PARAMETERS",
    "PARAMETER_CATALOG",
    "SCENARIO_CANONICALIZATION_VERSION",
    "SCENARIO_SCHEMA_VERSION",
    "SCOPE_RASTERIZATION_VERSION",
    "InputSource",
    "InputUncertainty",
    "ParameterDefinition",
    "ResolvedScenario",
    "Scenario",
    "ScenarioClassification",
    "ScenarioContractError",
    "ScenarioInput",
    "SpatialScope",
    "parameter_catalog_manifest",
    "resolve_scenario",
    "scenario_from_legacy_conditions",
    "scenario_from_simple_values",
    "scenario_sha256",
    "spatial_scope_mask",
]
