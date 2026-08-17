"""Characterization tests for the separate deterministic release regimes.

Software verification on controlled terrain and controlled snow states. These
tests fix the regime activation logic, the terrain responses, the mask
behaviour, and the leakage controls that keep held-out target attributes out of
prediction. They say nothing about field accuracy.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pytest

from avycore.hazard import risk
from avycore.hazard.conditions import Conditions
from avycore.snowpack import regimes as regime_module
from avycore.snowpack.regimes import (
    DRY_LOOSE,
    DRY_SLAB,
    FULL_DEPTH_GLIDE,
    REGIMES,
    WET_SNOW,
    compute_regime_release,
)
from avycore.snowpack.state import SnowState
from avycore.snowpack.zones import (
    REGIME_EXTRACTION_RULES,
    extract_regime_release_zones,
    segment_within_aspect_and_elevation,
)

SCIENTIFIC_USE = "software_verification"


@dataclass(frozen=True)
class _Grid:
    shape: tuple[int, int]
    resolution_m: float


class _Terrain:
    """Minimal in-memory implementation of the runtime Terrain protocol."""

    def __init__(self, layers: dict[str, np.ndarray], *, resolution_m: float = 30.0) -> None:
        first = next(iter(layers.values()))
        self.grid = _Grid(tuple(first.shape), resolution_m)
        self._layers = {
            name: np.ma.array(values, copy=True) for name, values in layers.items()
        }

    def layer(self, name: str) -> np.ma.MaskedArray:
        return self._layers[name]

    @staticmethod
    def reproject(col: Any, row: Any) -> tuple[Any, Any]:
        col_array = np.asarray(col, dtype="float64")
        row_array = np.asarray(row, dtype="float64")
        lon = 9.8 + col_array * 0.0004
        lat = 46.8 - row_array * 0.0003
        if np.ndim(col_array) == 0:
            return float(lon), float(lat)
        return lon, lat


def _terrain(
    *,
    slope: np.ndarray,
    aspect: np.ndarray | None = None,
    elevation: np.ndarray | None = None,
    general_curvature: np.ndarray | None = None,
    plan_curvature: np.ndarray | None = None,
    forest_mask: np.ndarray | None = None,
    mask: np.ndarray | None = None,
) -> _Terrain:
    shape = slope.shape

    def layer(values: np.ndarray | None, default: float) -> np.ndarray:
        base = np.full(shape, default, dtype="float32") if values is None else values
        return np.ma.array(base, mask=False if mask is None else mask)

    return _Terrain(
        {
            "elevation": layer(elevation, 2200.0),
            "slope": layer(slope, 0.0),
            "aspect": layer(aspect, 180.0),
            "general_curvature": layer(general_curvature, 0.0),
            "plan_curvature": layer(plan_curvature, 0.0),
            "forest_mask": layer(forest_mask, 0.0),
        }
    )


def _state(shape: tuple[int, int], **overrides: Any) -> SnowState:
    """A uniform snow state with every field explicitly set."""

    def field(value: float, dtype: str = "float32") -> np.ndarray:
        return np.full(shape, value, dtype=dtype)

    defaults: dict[str, Any] = {
        "new_snow_index_cm": field(0.0),
        "drift_index": field(0.0),
        "drift_index_normalized": field(0.0),
        "drift_from_direction_deg": field(-1.0),
        "peak_hour_index": field(0.0, "int32"),
        "storm_new_snow_cm": field(0.0),
        "storm_rain_mm": field(0.0),
        "rain_on_snow_mm": field(0.0),
        "positive_degree_hours": field(0.0),
        "antecedent_snow_depth_m": field(0.0),
        "peak_snow_depth_m": field(0.0),
        "peak_temperature_c": field(-8.0),
        "mean_storm_temperature_c": field(-8.0),
        "minimum_storm_temperature_c": field(-12.0),
        "buried_weak_interface_proxy": field(0.0),
        "independent_peak_new_snow_index_cm": field(0.0),
        "independent_peak_drift_index": field(0.0),
        "mask": np.zeros(shape, dtype=bool),
        "metadata": {"antecedent_snow_depth_available": True},
    }
    for name, value in overrides.items():
        if name not in defaults:
            raise KeyError(name)
        defaults[name] = (
            value
            if isinstance(value, (np.ndarray, dict))
            else np.full(shape, value, dtype=defaults[name].dtype)
        )
    return SnowState(**defaults)


# ---------------------------------------------------------------------------
# Regime activation is decided by the model from pre-event forcing alone
# ---------------------------------------------------------------------------


def test_a_dry_cold_storm_activates_only_the_dry_regimes():
    shape = (4, 4)
    terrain = _terrain(slope=np.full(shape, 38.0, dtype="float32"))
    state = _state(shape, new_snow_index_cm=40.0, independent_peak_new_snow_index_cm=40.0)
    field = compute_regime_release(terrain, state)
    supported = field.explanation["regime_supported"]
    assert supported[DRY_SLAB] is True
    assert supported[DRY_LOOSE] is True
    assert supported[WET_SNOW] is False
    assert supported[FULL_DEPTH_GLIDE] is False
    assert "liquid water" in field.explanation["regime_unsupported_reason"][WET_SNOW]


def test_a_rain_on_snow_event_activates_the_wet_regime_and_not_dry_loose():
    shape = (4, 4)
    terrain = _terrain(slope=np.full(shape, 36.0, dtype="float32"))
    state = _state(
        shape,
        rain_on_snow_mm=30.0,
        positive_degree_hours=60.0,
        antecedent_snow_depth_m=1.5,
        peak_temperature_c=3.0,
        mean_storm_temperature_c=3.0,
    )
    field = compute_regime_release(terrain, state)
    supported = field.explanation["regime_supported"]
    assert supported[WET_SNOW] is True
    assert supported[DRY_LOOSE] is False
    assert supported[FULL_DEPTH_GLIDE] is False
    assert field.explanation["dominant_regime_cell_counts"][WET_SNOW] > 0


def test_full_depth_glide_is_explicitly_unsupported_with_surface_only_inputs():
    shape = (1, 3)
    terrain = _terrain(
        slope=np.full(shape, 38.0, dtype="float32"),
        forest_mask=np.array([[0.0, 0.9, 0.0]], dtype="float32"),
    )
    state = _state(
        shape,
        antecedent_snow_depth_m=np.array([[1.5, 1.5, 0.2]], dtype="float32"),
        positive_degree_hours=40.0,
        peak_temperature_c=2.0,
        mean_storm_temperature_c=2.0,
    )
    field = compute_regime_release(terrain, state)
    regime = field.regime_scores[FULL_DEPTH_GLIDE]
    assert regime.supported is False
    assert not regime.active.any()
    assert not regime.score.any()
    assert "no basal liquid-water observation" in regime.unsupported_reason


def test_unavailable_antecedent_depth_is_reported_as_unknown_not_shallow():
    shape = (2, 2)
    terrain = _terrain(slope=np.full(shape, 38.0, dtype="float32"))
    state = _state(
        shape,
        positive_degree_hours=40.0,
        metadata={"antecedent_snow_depth_available": False},
    )
    field = compute_regime_release(terrain, state)
    reason = field.explanation["regime_unsupported_reason"][FULL_DEPTH_GLIDE]
    assert "not silently substituted" in reason


def test_drift_suppresses_dry_loose_but_feeds_the_dry_slab():
    shape = (1, 2)
    terrain = _terrain(
        slope=np.full(shape, 45.0, dtype="float32"),
        aspect=np.full(shape, 90.0, dtype="float32"),
    )
    undrifted = _state(shape, new_snow_index_cm=25.0, independent_peak_new_snow_index_cm=25.0)
    drifted = _state(
        shape,
        new_snow_index_cm=25.0,
        independent_peak_new_snow_index_cm=25.0,
        drift_index_normalized=1.0,
        drift_from_direction_deg=270.0,
    )
    quiet_field = compute_regime_release(terrain, undrifted)
    windy_field = compute_regime_release(terrain, drifted)
    loose_quiet = float(quiet_field.regime_scores[DRY_LOOSE].score[0, 0])
    loose_windy = float(windy_field.regime_scores[DRY_LOOSE].score[0, 0])
    slab_quiet = float(quiet_field.regime_scores[DRY_SLAB].score[0, 0])
    slab_windy = float(windy_field.regime_scores[DRY_SLAB].score[0, 0])
    assert loose_windy < loose_quiet
    assert slab_windy > slab_quiet


# ---------------------------------------------------------------------------
# Wind-from convention and terrain response
# ---------------------------------------------------------------------------


def test_drift_from_the_west_loads_east_facing_lee_slopes():
    aspect = np.array([[0.0, 90.0, 180.0, 270.0]], dtype="float32")
    terrain = _terrain(slope=np.full((1, 4), 38.0, dtype="float32"), aspect=aspect)
    state = _state(
        (1, 4),
        new_snow_index_cm=20.0,
        independent_peak_new_snow_index_cm=20.0,
        drift_index_normalized=1.0,
        drift_from_direction_deg=270.0,
    )
    score = compute_regime_release(terrain, state).regime_scores[DRY_SLAB].score[0]
    # Wind FROM 270 (west) loads the lee, which faces 90 (east).
    assert int(np.argmax(score)) == 1
    assert float(score[3]) < float(score[1])


def test_a_missing_drift_direction_contributes_no_lee_loading():
    terrain = _terrain(slope=np.full((1, 4), 38.0, dtype="float32"))
    state = _state(
        (1, 4),
        new_snow_index_cm=20.0,
        independent_peak_new_snow_index_cm=20.0,
        drift_index_normalized=1.0,
        drift_from_direction_deg=-1.0,
    )
    windy = compute_regime_release(terrain, state).regime_scores[DRY_SLAB].score
    calm = compute_regime_release(
        terrain, _state((1, 4), new_snow_index_cm=20.0, independent_peak_new_snow_index_cm=20.0)
    ).regime_scores[DRY_SLAB].score
    np.testing.assert_allclose(windy, calm)


def test_regime_slope_optima_are_ordered_loose_steeper_than_slab_steeper_than_wet():
    slopes = np.arange(15.0, 76.0, 1.0, dtype="float32").reshape(1, -1)
    shape = slopes.shape
    terrain = _terrain(slope=slopes)
    state = _state(
        shape,
        new_snow_index_cm=40.0,
        independent_peak_new_snow_index_cm=40.0,
        rain_on_snow_mm=30.0,
        positive_degree_hours=60.0,
        antecedent_snow_depth_m=1.5,
        peak_temperature_c=-4.0,
        mean_storm_temperature_c=-1.0,
    )
    field = compute_regime_release(terrain, state)
    best = {
        regime: float(slopes[0, int(np.argmax(field.regime_scores[regime].score[0]))])
        for regime in REGIMES
    }
    assert best[WET_SNOW] < best[DRY_SLAB] < best[DRY_LOOSE]
    assert best[FULL_DEPTH_GLIDE] < best[DRY_LOOSE]


# ---------------------------------------------------------------------------
# Equivalence with the untouched production dry-slab model
# ---------------------------------------------------------------------------


def test_dry_slab_reduces_exactly_to_the_production_release_model():
    """With no drift and no weak-interface proxy the regime score is production's.

    The production model is deliberately left unmodified so previously frozen
    experiments replay byte-for-byte. This test pins the claim that the new
    dry-slab regime changes only the *source* of the loading, not the equations.
    """

    rng = np.random.default_rng(20260814)
    shape = (24, 24)
    slope = rng.uniform(0.0, 70.0, shape).astype("float32")
    aspect = rng.uniform(0.0, 360.0, shape).astype("float32")
    general = rng.normal(0.0, 0.01, shape).astype("float32")
    plan = rng.normal(0.0, 0.01, shape).astype("float32")
    forest = rng.uniform(0.0, 1.0, shape).astype("float32")
    terrain = _terrain(
        slope=slope,
        aspect=aspect,
        general_curvature=general,
        plan_curvature=plan,
        forest_mask=forest,
    )
    new_snow_cm = 37.5
    conditions = Conditions(
        new_snow_cm=new_snow_cm,
        # Below the production 15 km/h transport threshold, so its wind term is
        # exactly zero and the two loadings are comparable.
        wind_speed_kmh=5.0,
        wind_direction_deg=225.0,
        release_size="medium",
    )
    production = risk.compute_release(terrain, conditions)
    state = _state(
        shape,
        new_snow_index_cm=new_snow_cm,
        independent_peak_new_snow_index_cm=new_snow_cm,
    )
    regime = compute_regime_release(terrain, state).regime_scores[DRY_SLAB].score
    np.testing.assert_allclose(
        np.asarray(production.release.filled(0.0), dtype="float64"),
        np.asarray(regime, dtype="float64"),
        rtol=1e-6,
        atol=1e-5,
    )


def test_weak_interface_proxy_is_diagnostic_and_cannot_change_dry_slab_score():
    shape = (8, 8)
    rng = np.random.default_rng(7)
    terrain = _terrain(slope=rng.uniform(20.0, 55.0, shape).astype("float32"))
    without = compute_regime_release(
        terrain, _state(shape, new_snow_index_cm=20.0, independent_peak_new_snow_index_cm=20.0)
    ).regime_scores[DRY_SLAB].score
    with_proxy = compute_regime_release(
        terrain,
        _state(
            shape,
            new_snow_index_cm=20.0,
            independent_peak_new_snow_index_cm=20.0,
            buried_weak_interface_proxy=1.0,
        ),
    ).regime_scores[DRY_SLAB].score
    np.testing.assert_array_equal(with_proxy, without)
    explanation = compute_regime_release(
        terrain,
        _state(
            shape,
            new_snow_index_cm=20.0,
            independent_peak_new_snow_index_cm=20.0,
            buried_weak_interface_proxy=1.0,
        ),
    ).regime_scores[DRY_SLAB].explanation
    assert "Diagnostic only" in explanation["weak_interface_role"]


# ---------------------------------------------------------------------------
# Masks, bounds, determinism
# ---------------------------------------------------------------------------


def test_missing_terrain_stays_masked_and_never_scores_zero_silently():
    shape = (1, 3)
    mask = np.array([[False, True, False]])
    terrain = _terrain(slope=np.full(shape, 38.0, dtype="float32"), mask=mask)
    state = _state(shape, new_snow_index_cm=40.0, independent_peak_new_snow_index_cm=40.0)
    field = compute_regime_release(terrain, state)
    assert np.ma.getmaskarray(field.release).tolist() == [[False, True, False]]
    assert field.dominant_regime.tolist() == [[1, 0, 1]]


def test_a_complete_cell_with_no_active_regime_scores_a_computed_zero():
    shape = (2, 2)
    terrain = _terrain(slope=np.full(shape, 38.0, dtype="float32"))
    field = compute_regime_release(terrain, _state(shape))
    assert not np.ma.getmaskarray(field.release).any()
    assert float(np.asarray(field.release).max()) == 0.0
    assert "not a statement that the cell is safe" in field.explanation["zero_semantics"]


def test_every_regime_score_stays_inside_the_relative_index_range():
    shape = (16, 16)
    rng = np.random.default_rng(11)
    terrain = _terrain(
        slope=rng.uniform(0.0, 90.0, shape).astype("float32"),
        aspect=rng.uniform(0.0, 360.0, shape).astype("float32"),
        general_curvature=rng.normal(0.0, 0.05, shape).astype("float32"),
        plan_curvature=rng.normal(0.0, 0.05, shape).astype("float32"),
        forest_mask=rng.uniform(0.0, 1.0, shape).astype("float32"),
    )
    state = _state(
        shape,
        new_snow_index_cm=200.0,
        independent_peak_new_snow_index_cm=200.0,
        drift_index_normalized=1.0,
        drift_from_direction_deg=315.0,
        rain_on_snow_mm=200.0,
        positive_degree_hours=500.0,
        antecedent_snow_depth_m=6.0,
        buried_weak_interface_proxy=1.0,
        peak_temperature_c=-6.0,
        mean_storm_temperature_c=-3.0,
    )
    field = compute_regime_release(terrain, state)
    for regime in REGIMES:
        score = field.regime_scores[regime].score
        assert float(score.min()) >= 0.0
        assert float(score.max()) <= 100.0
    assert float(np.asarray(field.release).max()) <= 100.0


def test_regime_release_is_deterministic():
    shape = (12, 12)
    rng = np.random.default_rng(3)
    terrain = _terrain(slope=rng.uniform(10.0, 60.0, shape).astype("float32"))
    state = _state(shape, new_snow_index_cm=30.0, independent_peak_new_snow_index_cm=30.0)
    first = compute_regime_release(terrain, state)
    second = compute_regime_release(terrain, state)
    np.testing.assert_array_equal(
        np.asarray(first.release), np.asarray(second.release)
    )
    np.testing.assert_array_equal(first.dominant_regime, second.dominant_regime)


# ---------------------------------------------------------------------------
# Leakage controls
# ---------------------------------------------------------------------------


def test_no_regime_entry_point_accepts_a_mapped_target_attribute():
    """Prediction must be unable to see a held-out outline's type, size or shape."""

    forbidden = {
        "avalanche_type",
        "mapped_type",
        "observed_type",
        "target",
        "target_type",
        "outline",
        "outlines",
        "event_type",
        "mapped_size",
        "observed_size",
    }
    for function in (
        compute_regime_release,
        extract_regime_release_zones,
    ):
        parameters = set(inspect.signature(function).parameters)
        assert not (parameters & forbidden), function.__name__


def test_regime_modules_do_not_import_bake_only_or_vector_dependencies():
    import avycore.snowpack as package

    for module in (
        package,
        package.forcing,
        package.state,
        package.regimes,
        package.zones,
        package.solar,
    ):
        source = inspect.getsource(module)
        for banned in (
            "import rasterio",
            "import geopandas",
            "import fiona",
            "import pyproj",
            "import pandas",
            "import xdem",
            "from osgeo",
        ):
            assert banned not in source, f"{module.__name__} imports {banned}"


# ---------------------------------------------------------------------------
# Zone extraction
# ---------------------------------------------------------------------------


def test_segmentation_matches_the_production_implementation_exactly():
    rng = np.random.default_rng(20260814)
    shape = (40, 40)
    candidate = rng.random(shape) > 0.4
    aspect = rng.uniform(-1.0, 360.0, shape)
    elevation = rng.uniform(1200.0, 3200.0, shape)
    mine, my_count = segment_within_aspect_and_elevation(candidate, aspect, elevation)
    theirs, their_count = risk._segment(candidate, aspect, elevation)
    assert my_count == their_count
    np.testing.assert_array_equal(mine, theirs)


def test_new_snow_alone_barely_reaches_the_production_release_threshold():
    """Characterizes the knife edge the storm-window hindcast ran into.

    With the drift term at zero, the dry-slab score is capped at
    ``100 * capability * (0.20 + 0.80 * 0.60) = 68 * capability``. Perfect
    terrain -- the 40 deg optimum, fully convex, unforested -- therefore reaches
    68, but an ordinary planar 38 deg open slope reaches only 54.9 and falls
    *below* the configured threshold of 55 no matter how much snow falls. This
    is why wind loading, not snowfall, decides whether the model produces any
    release zone at all, and why a forcing product whose window-mean wind sits
    under the transport threshold produces none.
    """

    shape = (1, 2)
    terrain = _terrain(
        slope=np.array([[38.0, 40.0]], dtype="float32"),
        general_curvature=np.array([[0.0, 1.0]], dtype="float32"),
    )
    saturated = _state(
        shape, new_snow_index_cm=1000.0, independent_peak_new_snow_index_cm=1000.0
    )
    score = compute_regime_release(terrain, saturated).regime_scores[DRY_SLAB].score[0]
    assert float(score[0]) == pytest.approx(54.91, abs=0.01)
    assert float(score[0]) < risk.RELEASE_THRESHOLD
    assert float(score[1]) == pytest.approx(68.0, abs=0.01)


def test_zones_are_tagged_by_regime_and_never_merged_across_regimes():
    shape = (60, 60)
    terrain = _terrain(
        slope=np.full(shape, 38.0, dtype="float32"),
        aspect=np.full(shape, 90.0, dtype="float32"),
        # A convex roll lifts the planar-slope score over the configured
        # threshold; see the knife-edge characterization above.
        general_curvature=np.full(shape, 1.0, dtype="float32"),
    )
    state = _state(
        shape,
        new_snow_index_cm=60.0,
        independent_peak_new_snow_index_cm=60.0,
        rain_on_snow_mm=40.0,
        positive_degree_hours=60.0,
        antecedent_snow_depth_m=2.0,
        peak_temperature_c=1.0,
        mean_storm_temperature_c=0.5,
    )
    field = compute_regime_release(terrain, state)
    zone_set = extract_regime_release_zones(terrain, field)
    found = {zone.properties["release_regime"] for zone in zone_set.zones}
    assert {DRY_SLAB, WET_SNOW} <= found
    # The same ground carries two mechanisms and therefore two separate zones.
    slab = [z for z in zone_set.zones if z.properties["release_regime"] == DRY_SLAB]
    wet = [z for z in zone_set.zones if z.properties["release_regime"] == WET_SNOW]
    assert slab and wet
    assert bool(np.any(slab[0].pixels & wet[0].pixels))


def test_dry_loose_zones_can_use_terrain_the_slab_window_discards():
    rule = REGIME_EXTRACTION_RULES[DRY_LOOSE]
    slab_rule = REGIME_EXTRACTION_RULES[DRY_SLAB]
    assert rule.slope_max_deg > slab_rule.slope_max_deg
    shape = (40, 40)
    terrain = _terrain(slope=np.full(shape, 65.0, dtype="float32"))
    state = _state(shape, new_snow_index_cm=40.0, independent_peak_new_snow_index_cm=40.0)
    field = compute_regime_release(terrain, state)
    zone_set = extract_regime_release_zones(terrain, field)
    regimes_found = {zone.properties["release_regime"] for zone in zone_set.zones}
    assert regimes_found == {DRY_LOOSE}


def test_no_zone_anywhere_reports_a_reason_and_never_claims_safety():
    shape = (30, 30)
    terrain = _terrain(slope=np.full(shape, 5.0, dtype="float32"))
    field = compute_regime_release(terrain, _state(shape))
    zone_set = extract_regime_release_zones(terrain, field)
    assert zone_set.zones == []
    assert any("NOT a statement that the mountain is safe" in w for w in zone_set.warnings)
    for regime in REGIMES:
        assert zone_set.explanation["per_regime"][regime]["zone_count"] == 0
