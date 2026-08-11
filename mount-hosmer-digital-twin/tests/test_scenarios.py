"""Observation-scenario contracts, spatial support, and numerical invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from avycore.hazard import risk
from avycore.scenario import (
    InputSource,
    InputUncertainty,
    Scenario,
    ScenarioInput,
    SpatialScope,
    resolve_scenario,
    scenario_from_simple_values,
    scenario_sha256,
)
from app import assess as assess_mod
from app import baked as baked_mod
from app.core.settings import Settings
from synthetic_baked import LAT_BOTTOM, LAT_TOP, LON0, LON1, write_synthetic_baked


def _load(tmp_path: Path) -> baked_mod.BakedTerrain:
    write_synthetic_baked(tmp_path)
    return baked_mod.load_baked(
        Settings(
            project_root=tmp_path,
            backend_root=tmp_path / "backend",
            runtime_root=tmp_path,
            data_root=tmp_path,
        )
    )


def _uncertainty(kind: str = "not_provided") -> InputUncertainty:
    return InputUncertainty(kind=kind, basis="Synthetic scenario test.")


def _input(
    parameter: str,
    value,
    *,
    status: str = "assumed",
    scope: SpatialScope | None = None,
) -> ScenarioInput:
    definitions = {
        "new_snow_depth": ("weather_loading", "cm"),
        "wind_speed": ("weather_loading", "km/h"),
        "wind_direction": ("weather_loading", "degree_true"),
        "release_size": ("release_assumptions", "category"),
        "weak_layer_type": ("snowpack_weak_layers", "category"),
        "shooting_cracks": ("field_observations", "boolean"),
    }
    category, unit = definitions[parameter]
    source = None
    observed = None
    if status == "assumed":
        source = InputSource(name="Synthetic assumption", kind="user_assumption")
    elif status == "measured":
        source = InputSource(name="Synthetic field observation", kind="measurement")
        observed = datetime(2026, 2, 1, 18, tzinfo=UTC)
    return ScenarioInput.model_validate(
        {
            "input_id": f"{parameter}-test",
            "category": category,
            "parameter": parameter,
            "value": value,
            "unit": unit,
            "status": status,
            "observed_at_utc": observed,
            "source": source,
            "uncertainty": _uncertainty("unknown" if status == "unknown" else "not_provided"),
            "spatial_scope": scope or SpatialScope(),
        }
    )


def _complete_scenario(
    *,
    weather_status: str = "assumed",
    scope: SpatialScope | None = None,
    extras: tuple[ScenarioInput, ...] = (),
) -> Scenario:
    return Scenario(
        mode="advanced",
        inputs=(
            _input("new_snow_depth", 50.0, status=weather_status, scope=scope),
            _input("wind_speed", 60.0, status=weather_status, scope=scope),
            _input("wind_direction", 225.0, status=weather_status, scope=scope),
            _input("release_size", "medium", status="assumed"),
            *extras,
        ),
    )


def test_unknown_value_cannot_carry_zero() -> None:
    with pytest.raises(ValidationError, match="value=null"):
        _input("new_snow_depth", 0.0, status="unknown")


def test_measured_input_requires_traceable_source_and_utc_time() -> None:
    with pytest.raises(ValidationError, match="timestamp and source"):
        ScenarioInput.model_validate(
            {
                "input_id": "snow-missing-lineage",
                "category": "weather_loading",
                "parameter": "new_snow_depth",
                "value": 12,
                "unit": "cm",
                "status": "measured",
                "uncertainty": _uncertainty(),
            }
        )


def test_scenario_identity_is_independent_of_input_order() -> None:
    scenario = _complete_scenario()
    reordered = Scenario.model_validate(
        {**scenario.model_dump(mode="json"), "inputs": list(reversed(scenario.inputs))}
    )
    assert scenario_sha256(scenario) == scenario_sha256(reordered)


def test_uniform_structured_scenario_is_exactly_legacy_scalar(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    scenario = _complete_scenario()
    resolved = resolve_scenario(bt, scenario)
    assert resolved.conditions is not None
    assert resolved.condition_coverage is None

    structured = risk.compute_release(
        bt, resolved.conditions, condition_coverage=resolved.condition_coverage
    )
    legacy = risk.compute_release(bt, risk.Conditions(50, 60, 225, "medium"))

    np.testing.assert_array_equal(structured.release.data, legacy.release.data)
    np.testing.assert_array_equal(
        np.ma.getmaskarray(structured.release), np.ma.getmaskarray(legacy.release)
    )


def test_spatial_scope_excludes_unknown_coverage_instead_of_lowering_it(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    scope = SpatialScope(kind="aspect_band", aspect_min_deg=315, aspect_max_deg=45)
    resolved = resolve_scenario(bt, _complete_scenario(scope=scope))
    assert resolved.can_compute is True
    assert resolved.condition_coverage is not None
    assert resolved.report["result_scope"] == "supported_area_only"
    assert 0 < resolved.report["condition_coverage"]["joint_supported_fraction"] < 1

    field = risk.compute_release(
        bt, resolved.conditions, condition_coverage=resolved.condition_coverage
    )
    assert np.all(np.ma.getmaskarray(field.release)[~resolved.condition_coverage])


def test_drawn_wgs84_scope_is_rasterized_at_grid_cell_centres(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    middle = (LON0 + LON1) / 2
    scope = SpatialScope(
        kind="drawn_area",
        geometry={
            "type": "Polygon",
            "coordinates": [[
                [LON0, LAT_BOTTOM],
                [middle, LAT_BOTTOM],
                [middle, LAT_TOP],
                [LON0, LAT_TOP],
                [LON0, LAT_BOTTOM],
            ]],
        },
    )
    resolved = resolve_scenario(bt, _complete_scenario(scope=scope))

    assert resolved.condition_coverage is not None
    fraction = resolved.report["condition_coverage"]["joint_supported_fraction"]
    assert 0.45 <= fraction <= 0.55


def test_observations_classify_full_only_for_whole_area_measurements(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    full = resolve_scenario(bt, _complete_scenario(weather_status="measured"))
    assert full.classification == "fully_specified_research_scenario"

    scoped = resolve_scenario(
        bt,
        _complete_scenario(
            weather_status="measured",
            scope=SpatialScope(kind="elevation_band", elevation_min_m=2100, elevation_max_m=2500),
        ),
    )
    assert scoped.classification == "partially_observation_constrained"


def test_unsupported_context_changes_reporting_not_physics(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    base = assess_mod.assess(bt, scenario=_complete_scenario(), simulation_mode="fast")
    contextual = assess_mod.assess(
        bt,
        scenario=_complete_scenario(
            extras=(
                _input("weak_layer_type", "surface_hoar"),
                _input("shooting_cracks", True, status="measured"),
            )
        ),
        simulation_mode="fast",
    )

    assert contextual["release_potential_index"] == base["release_potential_index"]
    assert contextual["zones"] == base["zones"]
    assert contextual["runout"]["core_area_m2"] == base["runout"]["core_area_m2"]
    assert contextual["scenario"]["reproducibility"]["scenario_sha256"] != base["scenario"]["reproducibility"]["scenario_sha256"]
    assert len(contextual["scenario"]["unsupported_inputs"]) == 2
    assert any("red flags" in warning.lower() for warning in contextual["warnings"])


def test_all_unknown_loading_returns_terrain_only_without_safe_zero(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    scenario = scenario_from_simple_values(
        new_snow_cm=None,
        wind_speed_kmh=None,
        wind_direction_deg=None,
        release_size="medium",
    )
    result = assess_mod.assess(bt, scenario=scenario, simulation_mode="fast")

    assert result["scenario"]["classification"] == "terrain_only"
    assert result["release_potential_index"] is None
    assert result["hazard_score"] is None
    assert result["runout"]["status"] == "unavailable_missing_inputs"
    assert result["runout"]["core_area_m2"] is None
    assert "not converted to zero" in " ".join(result["warnings"]).lower()


def test_measured_zero_is_distinct_from_unknown(tmp_path: Path) -> None:
    bt = _load(tmp_path)
    measured_zero = Scenario(
        inputs=(
            _input("new_snow_depth", 0.0, status="measured"),
            _input("wind_speed", 0.0, status="measured"),
            _input("wind_direction", None, status="unknown"),
            _input("release_size", "medium"),
        )
    )
    resolved = resolve_scenario(bt, measured_zero)
    assert resolved.can_compute is True
    assert resolved.conditions is not None
    assert resolved.conditions.new_snow_cm == 0.0
    assert resolved.conditions.wind_speed_kmh == 0.0
