"""The three newly-activated inputs, and the advisories that replace the rest.

Two properties matter most here:

* an omitted optional input reproduces the historical result EXACTLY, so nothing
  was quietly given a default;
* no advisory ever changes a computed number -- the whole point of the advisory
  mechanism is that evidence the model cannot use is reported rather than
  converted into an invented coefficient.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from avycore.advisories import build_advisories
from avycore.hazard.conditions import (
    RAIN_SNOW_LOWER_C,
    RAIN_SNOW_UPPER_C,
    Conditions,
    snow_fraction,
)
from avycore.scenario import PARAMETER_CATALOG

from app import assess as assess_mod
from app import baked as baked_mod
from app.core.settings import Settings
from synthetic_baked import write_synthetic_baked

LOADED = dict(new_snow_cm=50.0, wind_speed_kmh=60.0, wind_direction_deg=225.0)


def _terrain(tmp_path: Path):
    write_synthetic_baked(tmp_path)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path,
        data_root=tmp_path,
    )
    baked_mod._load.cache_clear()
    return baked_mod.load_baked(settings)


def _record(parameter: str, value, status: str = "measured") -> dict:
    return {"status": status, "value": value, "label": PARAMETER_CATALOG[parameter].label}


# --- The rain/snow phase gate -------------------------------------------------


@pytest.mark.parametrize(
    "temperature,expected",
    [(None, 1.0), (-20.0, 1.0), (RAIN_SNOW_LOWER_C, 1.0), (1.0, 0.5), (RAIN_SNOW_UPPER_C, 0.0), (9.0, 0.0)],
)
def test_snow_fraction_follows_the_documented_band(temperature, expected) -> None:
    assert snow_fraction(temperature) == pytest.approx(expected)


def test_unknown_temperature_reproduces_the_historical_result(tmp_path: Path) -> None:
    """An omitted optional input must never become a default assumption."""
    bt = _terrain(tmp_path)
    without = assess_mod.assess(bt, Conditions(**LOADED))
    with_cold = assess_mod.assess(bt, Conditions(**LOADED, air_temperature_c=-8.0))

    assert without["release_potential_index"] == with_cold["release_potential_index"]
    assert without["area_hazard_index"] == with_cold["area_hazard_index"]


def test_rain_reduces_dry_slab_loading_and_says_it_is_not_safer(tmp_path: Path) -> None:
    bt = _terrain(tmp_path)
    dry = assess_mod.assess(bt, Conditions(**LOADED, air_temperature_c=-5.0))
    wet = assess_mod.assess(bt, Conditions(**LOADED, air_temperature_c=6.0))

    assert wet["release_potential_index"] < dry["release_potential_index"]
    advisory = next(
        item for item in wet["scenario"]["advisories"] if item["advisory_id"] == "rain_on_snow"
    )
    assert advisory["severity"] == "critical"
    assert advisory["overrides_model"] is True
    assert "not about hazard" in advisory["detail"].lower() or "NOT about hazard" in advisory["detail"]
    assert "wet-slab" in advisory["detail"]


def test_conditions_publish_what_the_loading_term_actually_used(tmp_path: Path) -> None:
    bt = _terrain(tmp_path)
    result = assess_mod.assess(bt, Conditions(**LOADED, air_temperature_c=1.0))
    conditions = result["conditions"]

    assert conditions["air_temperature_c"] == 1.0
    assert conditions["precipitation_snow_fraction"] == pytest.approx(0.5)
    assert conditions["effective_new_snow_cm"] == pytest.approx(25.0)


# --- Flow regime and the alpha override ---------------------------------------


def test_flow_regime_changes_the_runout_and_is_reported(tmp_path: Path) -> None:
    bt = _terrain(tmp_path)
    dry = assess_mod.assess(bt, Conditions(**LOADED))
    wet = assess_mod.assess(bt, Conditions(**LOADED, flow_regime="wet_snow"))

    # Wet flow carries more Coulomb friction and a steeper alpha, so it stops sooner.
    assert wet["runout"]["core_area_m2"] < dry["runout"]["core_area_m2"]
    assert wet["runout"]["per_zone"][0]["flow_regime"] == "wet_snow"
    assert wet["runout"]["per_zone"][0]["alpha_angle_deg"] > dry["runout"]["per_zone"][0]["alpha_angle_deg"]
    assert dry["runout"]["per_zone"][0]["flow_regime"] == "dry_slab"


def test_an_unknown_flow_regime_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValueError, match="Unknown flow regime"):
        Conditions(**LOADED, flow_regime="slush").clamped()


def test_alpha_override_replaces_the_regional_angle(tmp_path: Path) -> None:
    bt = _terrain(tmp_path)
    default = assess_mod.assess(bt, Conditions(**LOADED))
    steep = assess_mod.assess(bt, Conditions(**LOADED, alpha_angle_override_deg=35.0))

    assert default["runout"]["per_zone"][0]["alpha_angle_deg"] == 27.0
    assert steep["runout"]["per_zone"][0]["alpha_angle_deg"] == 35.0
    assert steep["runout"]["per_zone"][0]["alpha_source"] == "user_override"
    assert default["runout"]["per_zone"][0]["alpha_source"] == "configured_regional_default"
    # A steeper angle of reach cannot run further than a shallower one.
    assert steep["runout"]["core_area_m2"] <= default["runout"]["core_area_m2"]


@pytest.mark.parametrize("angle", [1.0, 88.0])
def test_the_scenario_contract_rejects_an_out_of_envelope_alpha(angle: float) -> None:
    """The contract refuses the value outright rather than silently clamping it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="runout_alpha_angle must be"):
        assess_mod.assess  # noqa: B018 - keep the import graph honest
        from avycore.scenario import scenario_from_simple_values

        scenario_from_simple_values(
            new_snow_cm=50.0,
            wind_speed_kmh=60.0,
            wind_direction_deg=225.0,
            release_size="medium",
            alpha_angle_override_deg=angle,
        )


def test_the_engine_still_clamps_as_a_backstop() -> None:
    """Defence in depth: a caller bypassing the contract cannot leave the envelope."""
    from avycore.hazard.runout import _alpha_for

    config = assess_mod._config()
    bounds = assess_mod.RUNOUT_PARAMS["alpha_override_bounds_deg"]

    assert _alpha_for(config, "medium", override_deg=1.0) == bounds["minimum"]
    assert _alpha_for(config, "medium", override_deg=88.0) == bounds["maximum"]
    assert _alpha_for(config, "medium", override_deg=30.0) == 30.0


# --- Advisories ---------------------------------------------------------------


def test_direct_instability_signs_produce_one_overriding_advisory() -> None:
    advisories = build_advisories(
        {
            "whumpfing": _record("whumpfing", True),
            "shooting_cracks": _record("shooting_cracks", True),
            "recent_avalanche_activity": _record("recent_avalanche_activity", True),
        }
    )
    signs = next(item for item in advisories if item.advisory_id == "direct_instability_signs")

    assert signs.severity == "critical"
    assert signs.overrides_model is True
    assert "3 direct instability signs" in signs.title
    assert signs.to_dict()["changed_the_number"] is False


def test_a_false_observation_is_not_an_instability_sign() -> None:
    advisories = build_advisories({"whumpfing": _record("whumpfing", False)})
    assert [item.advisory_id for item in advisories] == []


def test_unknown_records_never_raise_an_advisory() -> None:
    advisories = build_advisories(
        {"whumpfing": _record("whumpfing", None, status="unknown")}
    )
    assert advisories == []


@pytest.mark.parametrize("layer", ["surface_hoar", "facets", "depth_hoar"])
def test_persistent_weak_layers_are_critical(layer: str) -> None:
    advisories = build_advisories({"weak_layer_type": _record("weak_layer_type", layer)})
    weak = next(item for item in advisories if item.advisory_id == "weak_layer_recorded")
    assert weak.severity == "critical"
    assert weak.overrides_model is True
    assert "did not change the index" in weak.detail


def test_a_non_persistent_weak_layer_is_a_warning_not_a_critical() -> None:
    advisories = build_advisories(
        {"weak_layer_type": _record("weak_layer_type", "crust_interface")}
    )
    weak = next(item for item in advisories if item.advisory_id == "weak_layer_recorded")
    assert weak.severity == "warning"
    assert weak.overrides_model is False


@pytest.mark.parametrize("result,severity", [("ECTP", "critical"), ("ECTN", "warning")])
def test_propagating_stability_tests_rank_above_non_propagating(result, severity) -> None:
    advisories = build_advisories(
        {"stability_test_result": _record("stability_test_result", result)}
    )
    test = next(item for item in advisories if item.advisory_id == "stability_test_recorded")
    assert test.severity == severity
    assert "not numerically ingested" in test.detail


def test_advisories_are_ordered_by_severity_and_are_deterministic() -> None:
    records = {
        "whumpfing": _record("whumpfing", True),
        "snow_depth": _record("snow_depth", 180.0),
        "stability_test_result": _record("stability_test_result", "ECTN"),
    }
    first = build_advisories(records)
    second = build_advisories(records)

    assert [item.advisory_id for item in first] == [item.advisory_id for item in second]
    severities = [item.severity for item in first]
    assert severities == sorted(severities, key=lambda value: {"critical": 0, "warning": 1, "note": 2}[value])


def test_no_advisory_ever_changes_the_number(tmp_path: Path) -> None:
    """The whole contract in one assertion."""
    from avycore.scenario import (
        InputSource,
        InputUncertainty,
        ScenarioInput,
        SpatialScope,
        scenario_from_simple_values,
    )

    bt = _terrain(tmp_path)
    plain = scenario_from_simple_values(
        new_snow_cm=50.0, wind_speed_kmh=60.0, wind_direction_deg=225.0, release_size="medium"
    )
    source = InputSource(name="Field notebook", kind="measurement")
    uncertainty = InputUncertainty(kind="not_provided", basis="none")
    loud = plain.model_copy(
        update={
            "inputs": (
                *plain.inputs,
                ScenarioInput(
                    input_id="whumpf",
                    category="field_observations",
                    parameter="whumpfing",
                    value=True,
                    unit="boolean",
                    status="measured",
                    observed_at_utc="2026-02-01T18:00:00Z",
                    source=source,
                    uncertainty=uncertainty,
                    spatial_scope=SpatialScope(kind="whole_area"),
                ),
                ScenarioInput(
                    input_id="weak",
                    category="snowpack_weak_layers",
                    parameter="weak_layer_type",
                    value="surface_hoar",
                    unit="category",
                    status="measured",
                    observed_at_utc="2026-02-01T18:00:00Z",
                    source=source,
                    uncertainty=uncertainty,
                    spatial_scope=SpatialScope(kind="whole_area"),
                ),
            )
        }
    )

    quiet_result = assess_mod.assess(bt, scenario=plain)
    loud_result = assess_mod.assess(bt, scenario=loud)

    assert loud_result["release_potential_index"] == quiet_result["release_potential_index"]
    assert loud_result["area_hazard_index"] == quiet_result["area_hazard_index"]
    assert [zone["hazard_index"] for zone in loud_result["zones"]] == [
        zone["hazard_index"] for zone in quiet_result["zones"]
    ]
    # ...but the reader is told, loudly.
    advisories = loud_result["scenario"]["advisories"]
    assert {item["advisory_id"] for item in advisories} >= {
        "direct_instability_signs",
        "weak_layer_recorded",
    }
    assert loud_result["scenario"]["advisory_summary"]["field_evidence_overrides_model"] is True
    assert all(item["changed_the_number"] is False for item in advisories)
    assert quiet_result["scenario"]["advisories"] == []


def test_every_catalog_parameter_is_active_or_advisory() -> None:
    """No parameter is left as dead weight the user cannot act on."""
    roles = {name: definition.model_role for name, definition in PARAMETER_CATALOG.items()}
    assert set(roles.values()) == {"active", "advisory"}
    assert roles["air_temperature"] == "active"
    assert roles["flow_regime"] == "active"
    assert roles["runout_alpha_angle"] == "active"
    assert roles["whumpfing"] == "advisory"
