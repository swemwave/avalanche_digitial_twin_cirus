"""The composite area hazard index: its components, and what it refuses to do.

The safety-critical properties under test are the ones that keep a missing
measurement from reading as a low number:

* exposure can only ever RAISE a zone's index, so an unmapped valley is never
  reported as safer than the terrain alone says;
* a zone whose runout was never simulated is labelled release-only, not scored
  with reach and exposure silently set to zero;
* a bake with no exposure layer still assesses, with the term marked unavailable.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from avycore.hazard import composite

from app import assess as assess_mod
from app import baked as baked_mod
from app.core.settings import Settings
from avycore.hazard.risk import Conditions
from synthetic_baked import write_synthetic_baked

LOADED = dict(new_snow_cm=50.0, wind_speed_kmh=60.0, wind_direction_deg=225.0)


def _assess(tmp_path: Path, *, exposure: bool = True, **conditions) -> dict:
    write_synthetic_baked(tmp_path, exposure=exposure)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path,
        data_root=tmp_path,
    )
    baked_mod._load.cache_clear()
    bt = baked_mod.load_baked(settings)
    return assess_mod.assess(bt, Conditions(**(conditions or LOADED)))


# --- The per-zone combination ------------------------------------------------


def test_zone_index_is_release_scaled_by_reach_then_raised_by_exposure() -> None:
    reach = composite.reach_component(
        horizontal_reach_m=composite.REACH_FULL_DISTANCE_M,
        vertical_drop_m=composite.REACH_FULL_DROP_M,
        max_velocity_ms=composite.REACH_FULL_VELOCITY_MS,
    )
    assert reach.available and reach.value == pytest.approx(1.0)

    exposure = composite.exposure_component(
        peak_core=0.0, peak_envelope=0.0, covered_area_m2=0.0
    )
    zone = composite.zone_hazard_index(
        zone_id="RZ001", area_m2=10_000.0, release_score=80.0, reach=reach, exposure=exposure
    )

    # Full reach means the reach factor is exactly 1, so the index is the release
    # score itself; nothing mapped in the path leaves it untouched.
    assert zone.terrain_and_reach_index == pytest.approx(80.0)
    assert zone.index == pytest.approx(80.0)
    assert zone.basis == "release_reach_exposure"


def test_reach_floor_bounds_a_zone_that_runs_nowhere() -> None:
    reach = composite.reach_component(
        horizontal_reach_m=0.0, vertical_drop_m=0.0, max_velocity_ms=0.0
    )
    zone = composite.zone_hazard_index(
        zone_id="RZ001",
        area_m2=1.0,
        release_score=100.0,
        reach=reach,
        exposure=composite.exposure_unavailable("no layer"),
    )
    assert reach.value == pytest.approx(0.0)
    assert zone.index == pytest.approx(100.0 * composite.REACH_BASE)


def test_missing_velocity_renormalises_instead_of_scoring_zero() -> None:
    """Fast routing reports no velocity. That must not cap its reach term."""
    both = composite.reach_component(
        horizontal_reach_m=composite.REACH_FULL_DISTANCE_M,
        vertical_drop_m=composite.REACH_FULL_DROP_M,
    )
    assert both.available
    assert both.value == pytest.approx(1.0)
    assert both.parts["velocity"] is None
    assert "renormalised" in (both.unavailable_reason or "")


def test_reach_with_no_measurement_at_all_is_unavailable_not_zero() -> None:
    reach = composite.reach_component()
    assert reach.available is False
    assert reach.value is None


@pytest.mark.parametrize("exposure_value", [0.0, 0.25, 0.5, 0.9, 1.0])
def test_exposure_never_pushes_a_zone_below_its_terrain_and_reach_index(
    exposure_value: float,
) -> None:
    reach = composite.reach_component(horizontal_reach_m=900.0, vertical_drop_m=400.0)
    exposure = composite.exposure_component(
        peak_core=exposure_value,
        peak_envelope=exposure_value,
        covered_area_m2=exposure_value * composite.EXPOSURE_FULL_COVERAGE_M2,
    )
    zone = composite.zone_hazard_index(
        zone_id="RZ001", area_m2=1.0, release_score=70.0, reach=reach, exposure=exposure
    )
    assert zone.index >= zone.terrain_and_reach_index
    assert zone.index <= 100.0


def test_exposure_uplift_is_bounded_by_the_published_maximum() -> None:
    reach = composite.reach_component(horizontal_reach_m=100.0, vertical_drop_m=50.0)
    saturated = composite.exposure_component(
        peak_core=1.0, peak_envelope=1.0, covered_area_m2=1e9
    )
    zone = composite.zone_hazard_index(
        zone_id="RZ001", area_m2=1.0, release_score=40.0, reach=reach, exposure=saturated
    )
    assert saturated.value == pytest.approx(1.0)
    assert zone.index == pytest.approx(
        zone.terrain_and_reach_index * (1.0 + composite.EXPOSURE_MAX_UPLIFT)
    )


def test_an_unsimulated_zone_is_release_only_not_zeroed() -> None:
    zone = composite.zone_hazard_index(
        zone_id="RZ040",
        area_m2=1.0,
        release_score=62.0,
        reach=composite.reach_unavailable("not simulated"),
        exposure=composite.exposure_unavailable("no footprint"),
    )
    assert zone.basis == "release_only"
    assert zone.components_available == {"release": True, "reach": False, "exposure": False}
    assert zone.index == pytest.approx(62.0)
    payload = zone.to_dict()
    assert payload["reach"]["value"] is None
    assert payload["exposure"]["value"] is None
    assert "release only" in payload["basis_label"].lower()


# --- The aggregate -----------------------------------------------------------


def _zone(zone_id: str, area: float, release: float, reach_m: float) -> composite.ZoneHazard:
    return composite.zone_hazard_index(
        zone_id=zone_id,
        area_m2=area,
        release_score=release,
        reach=composite.reach_component(horizontal_reach_m=reach_m, vertical_drop_m=reach_m / 2),
        exposure=composite.exposure_unavailable("no layer"),
    )


def test_area_index_is_the_area_weighted_mean_of_the_zone_indices() -> None:
    zones = [
        _zone("RZ001", 100_000.0, 60.0, 300.0),
        _zone("RZ002", 5_000.0, 95.0, 1800.0),
        _zone("RZ003", 20_000.0, 72.0, 900.0),
    ]
    area = composite.aggregate_area_hazard(zones)

    total = sum(zone.area_m2 for zone in zones)
    expected = sum(zone.area_m2 * zone.index for zone in zones) / total
    assert area.index == pytest.approx(expected)


def test_the_peak_zone_is_published_so_dilution_is_visible() -> None:
    big_and_mild = _zone("RZ001", 500_000.0, 58.0, 200.0)
    small_and_severe = _zone("RZ002", 4_000.0, 98.0, 2500.0)
    area = composite.aggregate_area_hazard([big_and_mild, small_and_severe])

    assert area.peak_zone_id == "RZ002"
    assert area.peak_zone_index == pytest.approx(round(small_and_severe.index, 1))
    assert area.peak_zone_basis == "release_reach_exposure" or area.peak_zone_basis
    assert area.index < area.peak_zone_index  # the mean really did hide it


def test_the_aggregate_reports_how_many_zones_carried_each_component() -> None:
    simulated = _zone("RZ001", 10_000.0, 70.0, 800.0)
    unsimulated = composite.zone_hazard_index(
        zone_id="RZ002",
        area_m2=10_000.0,
        release_score=70.0,
        reach=composite.reach_unavailable("not simulated"),
        exposure=composite.exposure_unavailable("no footprint"),
    )
    area = composite.aggregate_area_hazard([simulated, unsimulated])

    assert area.components["contributing_zone_count"] == {
        "release": 2,
        "reach": 1,
        "exposure": 0,
    }
    assert area.components["zone_count_by_basis"] == {
        "release_and_reach": 1,
        "release_only": 1,
    }
    assert "not directly comparable" in area.components["comparability"]


def test_no_zones_yields_no_index_rather_than_zero() -> None:
    area = composite.aggregate_area_hazard([])
    assert area.index is None
    assert area.peak_zone_index is None
    assert "not a hazard of zero" in area.components["method"]


# --- End to end, against the synthetic bake ----------------------------------


def test_assessment_publishes_the_composite_beside_the_release_index(tmp_path: Path) -> None:
    result = _assess(tmp_path)

    assert result["release_potential_index"] is not None
    assert result["area_hazard_index"] is not None
    assert result["area_hazard_band"] in {label for _, label, _ in assess_mod.RISK_CLASSES}
    assert result["area_hazard_color"] in {color for _, _, color in assess_mod.RISK_CLASSES}
    # The legacy fields keep their old meaning and are not the composite.
    assert result["hazard_score"] == result["release_potential_index"]
    assert result["risk_level"] == result["release_potential_band"]
    assert result["no_zone_release_percentile_index"] is None


def test_end_to_end_area_index_equals_the_area_weighted_mean_of_published_zones(
    tmp_path: Path,
) -> None:
    result = _assess(tmp_path)
    zones = result["zones"]
    assert zones

    total_area = sum(zone["area_m2"] for zone in zones)
    expected = sum(zone["area_m2"] * zone["hazard_index"] for zone in zones) / total_area
    assert result["area_hazard_index"] == pytest.approx(round(expected, 1), abs=0.05)

    peak = max(zones, key=lambda zone: zone["hazard_index"])
    assert result["peak_zone_id"] == peak["zone_id"]
    assert result["peak_zone_index"] == pytest.approx(peak["hazard_index"])


def test_every_zone_carries_a_band_and_colour_so_the_client_never_rederives_them(
    tmp_path: Path,
) -> None:
    result = _assess(tmp_path)
    bands = {label: color for _, label, color in assess_mod.RISK_CLASSES}
    for zone in result["zones"]:
        assert zone["hazard_band"] in bands
        assert zone["hazard_color"] == bands[zone["hazard_band"]]
    published = result["hazard_components"]["band_thresholds"]
    assert [item["upper"] for item in published] == [20, 40, 60, 80, 100]


def test_unsimulated_zones_are_flagged_rather_than_zeroed(tmp_path: Path) -> None:
    result = _assess(tmp_path)
    zones = result["zones"]
    simulated = {entry["zone_id"] for entry in result["runout"]["per_zone"]}
    unsimulated = [zone for zone in zones if zone["zone_id"] not in simulated]
    assert unsimulated, "the synthetic cone should produce more zones than the simulation cap"

    for zone in unsimulated:
        components = zone["hazard_components"]
        assert components["basis"] == "release_only"
        assert components["components_available"]["reach"] is False
        assert components["reach"]["value"] is None
        # The index is the release estimate, not a low number standing in for
        # "we did not look".
        assert zone["hazard_index"] == pytest.approx(zone["estimated_release_score"], abs=0.05)
        assert zone["hazard_index"] > 0

    assert result["hazard_detail"]["component_contributing_zone_count"]["reach"] == len(simulated)
    assert any("release-only" in warning for warning in result["warnings"])


def test_exposure_raises_the_zones_whose_runout_crosses_the_corridor(tmp_path: Path) -> None:
    result = _assess(tmp_path)
    exposed = [
        zone
        for zone in result["zones"]
        if (zone["hazard_components"]["exposure"]["value"] or 0.0) > 0.0
    ]
    assert exposed, "the synthetic corridor should be crossed by at least one runout"

    for zone in exposed:
        components = zone["hazard_components"]
        assert components["exposure"]["classes"] == ["Trunk highway"]
        assert zone["hazard_index"] > components["terrain_and_reach_index"]
        assert components["exposure_uplift_points"] > 0

    untouched = [
        zone
        for zone in result["zones"]
        if zone["hazard_components"]["exposure"]["available"]
        and (zone["hazard_components"]["exposure"]["value"] or 0.0) == 0.0
    ]
    for zone in untouched:
        components = zone["hazard_components"]
        assert zone["hazard_index"] == pytest.approx(components["terrain_and_reach_index"])


def test_assessment_succeeds_against_a_bake_with_no_exposure_layer(tmp_path: Path) -> None:
    result = _assess(tmp_path, exposure=False)

    assert result["area_hazard_index"] is not None
    assert result["hazard_components"]["contributing_zone_count"]["exposure"] == 0
    assert result["hazard_components"]["exposure_layer"]["available"] is False
    assert result["provenance"]["exposure"]["available"] is False
    for zone in result["zones"]:
        components = zone["hazard_components"]
        assert components["exposure"]["available"] is False
        assert components["exposure"]["value"] is None
        # No exposure evidence leaves the terrain-and-reach index exactly as it was.
        assert zone["hazard_index"] == pytest.approx(components["terrain_and_reach_index"])
    assert any("no exposure layer" in warning.lower() for warning in result["warnings"])


def test_a_missing_exposure_layer_does_not_lower_any_zone(tmp_path: Path) -> None:
    """The whole point of a one-directional uplift: absence costs nothing."""
    with_layer = _assess(tmp_path / "with", exposure=True)
    without_layer = _assess(tmp_path / "without", exposure=False)

    by_id = {zone["zone_id"]: zone for zone in without_layer["zones"]}
    for zone in with_layer["zones"]:
        bare = by_id[zone["zone_id"]]
        assert zone["hazard_index"] >= bare["hazard_index"] - 1e-9
        assert (
            zone["hazard_components"]["terrain_and_reach_index"]
            == pytest.approx(bare["hazard_components"]["terrain_and_reach_index"])
        )


def test_the_no_zone_fallback_keeps_its_own_field_and_method(tmp_path: Path) -> None:
    """A benign day: no zones, so no composite index -- and a different number."""
    result = _assess(
        tmp_path, new_snow_cm=0.0, wind_speed_kmh=0.0, wind_direction_deg=0.0
    )

    assert result["release_zones"]["zone_count"] == 0
    assert result["area_hazard_index"] is None
    assert result["area_hazard_band"] is None
    assert result["peak_zone_index"] is None
    # Invariant I3: the quiet day is still a number with a reason, not zero.
    assert result["release_potential_index"] > 0
    assert result["no_zone_release_percentile_index"] == result["release_potential_index"]
    fallback = result["hazard_components"]["no_zone_fallback"]
    assert fallback["field"] == "no_zone_release_percentile_index"
    assert "must never be read as one" in fallback["method"]


# --- The parameter manifest --------------------------------------------------


def test_the_composite_constants_are_in_the_hashed_parameter_manifest() -> None:
    manifest = assess_mod.assessment_parameter_manifest()
    assert manifest["composite_hazard_index"]["reach_base"] == composite.REACH_BASE
    assert (
        manifest["composite_hazard_index"]["exposure_max_uplift"]
        == composite.EXPOSURE_MAX_UPLIFT
    )


def test_changing_a_composite_constant_changes_config_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = assess_mod.assessment_model_identity()["sha256"]
    monkeypatch.setattr(composite, "EXPOSURE_MAX_UPLIFT", composite.EXPOSURE_MAX_UPLIFT + 0.01)
    after = assess_mod.assessment_model_identity()["sha256"]
    assert before != after


# --- The guardrail, enforced behaviourally -----------------------------------


def test_the_release_model_never_sees_exposure(tmp_path: Path) -> None:
    """avycore.hazard.risk must not import, receive, or see exposure input."""
    import inspect

    from avycore.hazard import risk

    source = inspect.getsource(risk)
    assert "exposure" not in source.lower()
    assert "exposure" not in inspect.signature(risk.compute_release).parameters
    assert "exposure" not in inspect.signature(risk.extract_release_zones).parameters

    # And behaviourally: the release raster is identical either way.
    with_layer = _assess(tmp_path / "with", exposure=True)
    without_layer = _assess(tmp_path / "without", exposure=False)
    assert with_layer["release_potential_index"] == without_layer["release_potential_index"]
    assert [zone["estimated_release_score"] for zone in with_layer["zones"]] == [
        zone["estimated_release_score"] for zone in without_layer["zones"]
    ]


def test_the_exposure_layer_is_never_a_runout_or_release_input(tmp_path: Path) -> None:
    result = _assess(tmp_path)
    coverage = result["coverage"]
    assert "exposure_weight" not in coverage["release_model"]["required_layers"]
    assert "exposure_weight" not in coverage["runout_model"]["required_layers"]
    assert result["provenance"]["exposure"]["used_in_release_model"] is False
    assert result["provenance"]["exposure"]["used_in_runout_model"] is False


# --- Determinism -------------------------------------------------------------


def test_the_composite_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    first = _assess(tmp_path / "a")
    second = _assess(tmp_path / "b")
    assert first["area_hazard_index"] == second["area_hazard_index"]
    assert [zone["hazard_index"] for zone in first["zones"]] == [
        zone["hazard_index"] for zone in second["zones"]
    ]
    assert all(
        math.isfinite(zone["hazard_index"]) for zone in first["zones"]
    ), "an index must always be a real number"
    assert np.all(
        np.array([zone["hazard_index"] for zone in first["zones"]]) <= 100.0
    )
