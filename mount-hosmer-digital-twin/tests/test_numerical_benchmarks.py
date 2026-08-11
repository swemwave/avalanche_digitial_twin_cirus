"""Characterized synthetic benchmarks for AvyCore's numerical kernels.

These tests are software verification, not field validation.  Their planes,
blocks, and masks have exact analytical properties, but they are not avalanche
observations and must never be presented as evidence of Mount Hosmer accuracy.
The assertions favor physical/numerical invariants over large snapshots so an
implementation can change without silently changing units, masks, conventions,
or deterministic behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
from shapely.geometry import shape as shapely_shape

from avycore.hazard import geometry
from avycore.hazard import risk
from avycore.hazard import runout
from avycore.hazard.conditions import Conditions
from avycore.hazard.zone import ReleaseZone


@dataclass(frozen=True)
class _Grid:
    shape: tuple[int, int]
    resolution_m: float


class _Terrain:
    """Minimal in-memory implementation of the runtime Terrain protocol."""

    def __init__(self, layers: dict[str, np.ndarray], *, resolution_m: float = 5.0) -> None:
        first = next(iter(layers.values()))
        self.grid = _Grid(tuple(first.shape), resolution_m)
        self._layers = {
            name: np.ma.array(values, copy=True) for name, values in layers.items()
        }

    def layer(self, name: str) -> np.ma.MaskedArray:
        return self._layers[name]

    @staticmethod
    def reproject(col: Any, row: Any) -> tuple[Any, Any]:
        """Simple grid (col, row) -> WGS84 (lon, lat) map for geometry tests."""
        col_array = np.asarray(col, dtype="float64")
        row_array = np.asarray(row, dtype="float64")
        lon = -115.0 + col_array * 0.001
        lat = 50.0 - row_array * 0.001
        if np.ndim(col_array) == 0:
            return float(lon), float(lat)
        return lon, lat


class _Config:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def require(self, dotted: str) -> Any:
        node: Any = self._values
        for part in dotted.split("."):
            node = node[part]
        return node


def _terrain_with(
    *,
    slope: np.ndarray,
    aspect: np.ndarray | None = None,
    elevation: np.ndarray | None = None,
    general_curvature: np.ndarray | None = None,
    plan_curvature: np.ndarray | None = None,
    forest_mask: np.ndarray | None = None,
    resolution_m: float = 5.0,
) -> _Terrain:
    shape = slope.shape

    def zeros() -> np.ndarray:
        return np.zeros(shape, dtype="float32")

    return _Terrain(
        {
            "elevation": (
                np.full(shape, 2000.0, dtype="float32")
                if elevation is None
                else elevation
            ),
            "slope": slope,
            "aspect": np.zeros(shape, dtype="float32") if aspect is None else aspect,
            "general_curvature": zeros() if general_curvature is None else general_curvature,
            "plan_curvature": zeros() if plan_curvature is None else plan_curvature,
            "forest_mask": zeros() if forest_mask is None else forest_mask,
        },
        resolution_m=resolution_m,
    )


def _risk_field(values: np.ndarray, *, mask: np.ndarray | bool = False) -> risk.RiskField:
    diagnostics = np.zeros(values.shape, dtype="float32")
    return risk.RiskField(
        release=np.ma.array(values, mask=mask, dtype="float32"),
        slope_term=diagnostics,
        wind_load_term=diagnostics.copy(),
        loading=diagnostics.copy(),
        explanation={},
    )


def _runout_config(
    *,
    particles: int = 32,
    max_steps: int = 800,
    jitter: float = 0.25,
    seed: int = 20260713,
    max_path_length_m: float = 400.0,
    spreading: float = 0.35,
) -> _Config:
    return _Config(
        {
            "runout": {
                "alpha_angle_deg": {
                    "small": 32.0,
                    "medium": 27.0,
                    "large": 23.0,
                    "very_large": 19.0,
                },
                "alpha_uncertainty_deg": 4.0,
                "friction": {
                    "open_snow": 0.20,
                    "forest": 0.35,
                    "gully": 0.16,
                    "xi_open": 1200.0,
                    "xi_forest": 500.0,
                },
                "fast_mode": {
                    "spreading": spreading,
                    "max_path_length_m": max_path_length_m,
                    "minimum_flux": 0.001,
                },
                "advanced_mode": {
                    "particles_per_zone": particles,
                    "max_steps": max_steps,
                    "time_step_s": 0.2,
                    "lateral_jitter": jitter,
                    "stopping_velocity_ms": 1.0,
                    "random_seed": seed,
                    "velocity_classes_ms": [5.0, 15.0, 25.0, 40.0],
                },
            }
        }
    )


def _planar_runout_case(
    *,
    shape: tuple[int, int] = (48, 31),
    grade_deg: float = 35.0,
    resolution_m: float = 5.0,
    masked_rows: slice | None = None,
    one_cell_zone: bool = False,
) -> tuple[_Terrain, ReleaseZone]:
    rows, cols = shape
    row_index = np.arange(rows, dtype="float64")[:, None]
    elevation = np.broadcast_to(
        3000.0 - np.tan(np.deg2rad(grade_deg)) * resolution_m * row_index,
        shape,
    ).astype("float32", copy=True)
    elevation_mask = np.zeros(shape, dtype=bool)
    if masked_rows is not None:
        elevation_mask[masked_rows, :] = True
    elevation_layer = np.ma.array(elevation, mask=elevation_mask)

    terrain = _terrain_with(
        slope=np.full(shape, grade_deg, dtype="float32"),
        aspect=np.full(shape, 180.0, dtype="float32"),
        elevation=elevation_layer,
        resolution_m=resolution_m,
    )
    pixels = np.zeros(shape, dtype=bool)
    if one_cell_zone:
        pixels[2, cols // 2] = True
    else:
        pixels[2:4, cols // 2 - 1 : cols // 2 + 2] = True
    return terrain, ReleaseZone("RZ001", pixels, geometry=None)


def _simulate(
    mode: str,
    terrain: _Terrain,
    zone: ReleaseZone,
    config: _Config,
    *,
    release_size: str = "medium",
    seed: int | None = None,
) -> runout.RunoutResult:
    return runout.get_engine(mode).simulate(
        zone=zone,
        grid=terrain.grid,
        elevation=terrain.layer("elevation"),
        slope=terrain.layer("slope"),
        forest_mask=terrain.layer("forest_mask"),
        plan_curvature=terrain.layer("plan_curvature"),
        config=config,
        release_size=release_size,
        seed=seed,
    )


def test_release_breakpoints_preserve_degree_and_centimetre_units() -> None:
    """The documented slope curve and 50 cm snow saturation are characterized."""
    slopes = np.asarray([risk.SLOPE_BREAKPOINTS_DEG], dtype="float32")
    terrain = _terrain_with(slope=slopes)

    field = risk.compute_release(terrain, Conditions(new_snow_cm=50.0))

    # Zero curvature contributes the documented neutral capability factor 0.85.
    # Fifty centimetres is a full snow term, weighted 0.60.  The loading multiplier
    # is therefore 0.20 + 0.80 * 0.60 = 0.68.  Slopes outside 25--60 degrees retain
    # one tenth of the piecewise score.
    expected = np.asarray(risk.SLOPE_SCORES, dtype="float64") * 0.85 * 0.68
    outside = (slopes[0] < risk.SLOPE_MIN_DEG) | (slopes[0] > risk.SLOPE_MAX_DEG)
    expected[outside] *= 0.1
    np.testing.assert_allclose(field.release.compressed(), expected, rtol=0.0, atol=1e-5)
    assert field.release.dtype == np.dtype("float32")
    np.testing.assert_allclose(field.loading, 0.60, rtol=0.0, atol=1e-7)

    saturated = risk.compute_release(terrain, Conditions(new_snow_cm=300.0))
    np.testing.assert_array_equal(field.release, saturated.release)


def test_release_unions_every_required_mask_and_remains_a_bounded_index() -> None:
    """No required terrain gap may become a neutral, safe-looking numeric value."""
    required = [
        "slope",
        "aspect",
        "general_curvature",
        "plan_curvature",
        "forest_mask",
    ]
    shape = (2, len(required))
    layers: dict[str, np.ndarray] = {
        "elevation": np.full(shape, 2000.0, dtype="float32"),
        "slope": np.full(shape, 40.0, dtype="float32"),
        "aspect": np.full(shape, 45.0, dtype="float32"),
        "general_curvature": np.zeros(shape, dtype="float32"),
        "plan_curvature": np.zeros(shape, dtype="float32"),
        "forest_mask": np.zeros(shape, dtype="float32"),
    }
    expected_mask = np.zeros(shape, dtype=bool)
    for col, name in enumerate(required):
        layer_mask = np.zeros(shape, dtype=bool)
        layer_mask[0, col] = True
        layers[name] = np.ma.array(layers[name], mask=layer_mask)
        expected_mask |= layer_mask

    field = risk.compute_release(_Terrain(layers), Conditions(300.0, 200.0, 225.0))

    np.testing.assert_array_equal(np.ma.getmaskarray(field.release), expected_mask)
    valid = field.release.compressed()
    assert np.isfinite(valid).all()
    assert np.all((0.0 <= valid) & (valid <= 100.0))


def test_meteorological_wind_from_convention_loads_the_opposite_aspect() -> None:
    """A 225 degree (SW-from) wind loads 45 degree NE-facing terrain."""
    aspects = np.asarray([[45.0, 135.0, 225.0, 315.0]], dtype="float32")
    terrain = _terrain_with(
        slope=np.full(aspects.shape, 40.0, dtype="float32"),
        aspect=aspects,
    )

    loaded = risk.compute_release(terrain, Conditions(0.0, 40.0, 225.0))
    wrapped = risk.compute_release(terrain, Conditions(0.0, 40.0, 585.0))
    below_transport = risk.compute_release(terrain, Conditions(0.0, 15.0, 225.0))

    # With neutral plan curvature, the wind term is 0.7 times the cosine lee
    # factor: full on NE, half on cross-slopes, and zero on windward SW.
    np.testing.assert_allclose(
        loaded.wind_load_term,
        [[0.70, 0.35, 0.0, 0.35]],
        rtol=0.0,
        atol=1e-7,
    )
    np.testing.assert_array_equal(loaded.release, wrapped.release)
    np.testing.assert_array_equal(below_transport.wind_load_term, np.zeros(aspects.shape))
    assert loaded.release[0, 0] > loaded.release[0, 1] > loaded.release[0, 2]
    assert loaded.release[0, 3] == pytest.approx(loaded.release[0, 1], abs=1e-6)
    assert "from SW loads the lee (NE-facing)" in loaded.explanation["terms"]["wind_loading"]


def test_vector_mean_aspect_wraps_north_and_rejects_an_ambiguous_pair() -> None:
    direction, consistency = risk.vector_mean_aspect(
        np.asarray([350.0, 10.0, -1.0, np.nan], dtype="float64")
    )
    assert direction == pytest.approx(0.0, abs=1e-12)
    assert consistency == pytest.approx(np.cos(np.deg2rad(10.0)), abs=1e-4)
    assert risk.vector_mean_aspect(np.asarray([0.0, 180.0])) == (None, 0.0)


def test_zone_extraction_respects_aspect_elevation_threshold_and_area_units() -> None:
    """Adjacent terrain is split into coherent slabs, with area derived from metres."""
    shape = (60, 60)
    score = np.full(shape, risk.RELEASE_THRESHOLD - 0.01, dtype="float32")
    score[10:50, 10:50] = risk.RELEASE_THRESHOLD  # threshold is inclusive
    aspect = np.zeros(shape, dtype="float32")
    aspect[10:50, 30:50] = 90.0
    elevation = np.full(shape, 1200.0, dtype="float32")
    elevation[30:50, 10:50] = 1600.0
    terrain = _terrain_with(
        slope=np.full(shape, 35.0, dtype="float32"),
        aspect=aspect,
        elevation=elevation,
        resolution_m=5.0,
    )

    result = risk.extract_release_zones(terrain, _risk_field(score), Conditions())

    assert len(result.zones) == 4
    assert result.explanation["zone_count"] == 4
    assert result.explanation["total_release_area_km2"] == pytest.approx(0.04)
    assert result.explanation["is_probability"] is False
    assert result.explanation["disclaimer"]

    combined = np.zeros(shape, dtype=bool)
    for zone in result.zones:
        assert not np.any(combined & zone.pixels)
        combined |= zone.pixels
        assert int(zone.pixels.sum()) == 400
        assert zone.properties["area_m2"] == 10_000.0
        assert zone.properties["area_hectares"] == 1.0
        assert zone.properties["estimated_release_score"] == risk.RELEASE_THRESHOLD
        assert zone.properties["dominant_aspect_deg"] in {0.0, 90.0}
        assert zone.properties["elevation_mean_m"] in {1200.0, 1600.0}
        assert zone.properties["is_probability"] is False
        assert zone.geometry is not None
    expected = np.zeros(shape, dtype=bool)
    expected[10:50, 10:50] = True
    np.testing.assert_array_equal(combined, expected)


def test_zone_extraction_below_threshold_is_explicitly_not_a_safe_result() -> None:
    shape = (30, 30)
    terrain = _terrain_with(slope=np.full(shape, 40.0, dtype="float32"))
    field = _risk_field(np.full(shape, risk.RELEASE_THRESHOLD - 0.01, dtype="float32"))

    result = risk.extract_release_zones(terrain, field, Conditions())

    assert result.zones == []
    assert result.explanation == {"zone_count": 0}
    assert len(result.warnings) == 1
    assert "NOT a statement that the mountain is safe" in result.warnings[0]


def test_geometry_preserves_pixel_area_and_row_col_coordinate_order() -> None:
    """An anisotropic transform makes a row/column swap immediately observable."""

    def affine_reproject(col: Any, row: Any) -> tuple[Any, Any]:
        return np.asarray(col) * 10.0, np.asarray(row) * -20.0

    mask = np.zeros((5, 6), dtype=bool)
    mask[1, 2:4] = True
    mask[3, 4] = True
    polygon = geometry.mask_to_geojson(mask, affine_reproject, simplify_px=0.0)

    assert polygon is not None
    polygon_shape = shapely_shape(polygon)
    assert polygon_shape.area == pytest.approx(3 * 10.0 * 20.0, abs=1e-9)
    assert polygon_shape.bounds == pytest.approx((20.0, -80.0, 50.0, -20.0), abs=1e-9)

    path = geometry.path_to_geojson([(1, 2), (3, 4)], affine_reproject)
    assert path is not None
    assert path["type"] == "LineString"
    np.testing.assert_allclose(
        np.asarray(path["coordinates"]),
        np.asarray([[25.0, -30.0], [45.0, -70.0]]),
        rtol=0.0,
        atol=1e-12,
    )


@pytest.mark.parametrize("mode", sorted(runout.ENGINES))
def test_runout_engines_honor_elevation_nodata_grid_bounds_and_square_metre_units(
    mode: str,
) -> None:
    """A full-width NoData barrier is impermeable in both numerical engines."""
    terrain, zone = _planar_runout_case(masked_rows=slice(20, 24))
    result = _simulate(mode, terrain, zone, _runout_config(), seed=314159)
    nodata = np.ma.getmaskarray(terrain.layer("elevation"))

    assert result.reached.shape == terrain.grid.shape
    assert result.uncertainty.shape == terrain.grid.shape
    assert result.reached.dtype == np.dtype("bool")
    assert result.uncertainty.dtype == np.dtype("bool")
    assert not np.any(result.reached & nodata)
    assert not np.any(result.uncertainty & nodata)
    assert not result.reached[24:, :].any()
    assert not result.uncertainty[24:, :].any()
    assert np.all(~result.reached | result.uncertainty)

    cell_area_m2 = terrain.grid.resolution_m**2
    assert result.metadata["runout_area_m2"] == pytest.approx(
        float(result.reached.sum()) * cell_area_m2
    )
    assert result.metadata["uncertainty_area_m2"] == pytest.approx(
        float(result.uncertainty.sum()) * cell_area_m2
    )
    assert result.metadata["start_elevation_m"] == pytest.approx(
        float(terrain.layer("elevation")[2, terrain.grid.shape[1] // 2]),
        abs=0.1,
    )
    assert result.metadata["is_validated"] is False
    assert "NOT CALIBRATED" in result.metadata["calibration"]
    assert result.metadata["disclaimer"]

    assert result.intensity.dtype == np.dtype("float32")
    assert result.velocity.dtype == np.dtype("float32")
    assert np.isfinite(result.intensity).all()
    assert np.isfinite(result.velocity).all()
    assert np.all((0.0 <= result.intensity) & (result.intensity <= 1.0))
    assert np.all(result.velocity >= 0.0)


@pytest.mark.parametrize("layer_name", ["forest_mask", "plan_curvature"])
def test_particle_runout_treats_unknown_friction_inputs_as_barriers(layer_name: str) -> None:
    """Missing friction data must not silently become neutral/open runout terrain."""
    terrain, zone = _planar_runout_case(shape=(48, 31), grade_deg=35.0)
    values = terrain.layer(layer_name).copy()
    mask = np.zeros(terrain.grid.shape, dtype=bool)
    mask[20:24, :] = True
    values.mask = mask
    terrain._layers[layer_name] = values

    result = _simulate(
        "advanced",
        terrain,
        zone,
        _runout_config(particles=48, max_steps=1200),
        release_size="very_large",
        seed=314159,
    )

    assert not np.any(result.reached & mask)
    assert not np.any(result.uncertainty & mask)
    assert not result.reached[24:, :].any()
    assert not result.uncertainty[24:, :].any()
    assert result.metadata["required_terrain_layers"] == [
        "elevation",
        "forest_mask",
        "plan_curvature",
    ]
    assert result.metadata["valid_grid_fraction"] == pytest.approx(44 / 48, abs=1e-6)


def test_fast_runout_obeys_alpha_envelopes_length_cap_and_is_seed_independent() -> None:
    terrain, zone = _planar_runout_case(
        shape=(80, 21), grade_deg=30.0, one_cell_zone=True
    )
    config = _runout_config(max_path_length_m=100.0, spreading=0.0)
    small = _simulate("fast", terrain, zone, config, release_size="small", seed=1)
    small_other_seed = _simulate("fast", terrain, zone, config, release_size="small", seed=999)
    medium = _simulate("fast", terrain, zone, config, release_size="medium", seed=1)
    very_large = _simulate("fast", terrain, zone, config, release_size="very_large", seed=1)

    np.testing.assert_array_equal(small.reached, small_other_seed.reached)
    np.testing.assert_array_equal(small.intensity, small_other_seed.intensity)
    assert small.reached.sum() < medium.reached.sum() <= very_large.reached.sum()

    start_row, start_col, start_z = runout._start_point(zone, terrain.layer("elevation"))
    z = np.asarray(terrain.layer("elevation"))
    cell = terrain.grid.resolution_m
    for result in (small, medium, very_large):
        assert result.metadata["horizontal_reach_m"] <= 100.0
        for footprint, minimum_angle in (
            (result.reached, result.metadata["alpha_angle_deg"]),
            (result.uncertainty, result.metadata["alpha_envelope_deg"]),
        ):
            for row, col in np.argwhere(footprint):
                distance = np.hypot((row - start_row) * cell, (col - start_col) * cell)
                if distance == 0.0:
                    continue
                angle = np.degrees(np.arctan2(start_z - z[row, col], distance))
                assert angle + 1e-5 >= minimum_angle


def test_particle_runout_replays_exactly_with_a_seed_and_reports_aoi_escape() -> None:
    terrain, zone = _planar_runout_case(shape=(42, 31), grade_deg=35.0)
    config = _runout_config(particles=24, max_steps=1200, jitter=0.35, seed=777)

    first = _simulate("advanced", terrain, zone, config, release_size="very_large", seed=4242)
    replay = _simulate("advanced", terrain, zone, config, release_size="very_large", seed=4242)
    different = _simulate("advanced", terrain, zone, config, release_size="very_large", seed=4243)

    np.testing.assert_array_equal(first.reached, replay.reached)
    np.testing.assert_array_equal(first.uncertainty, replay.uncertainty)
    np.testing.assert_array_equal(first.intensity, replay.intensity)
    np.testing.assert_array_equal(first.velocity, replay.velocity)
    assert first.stopping_points == replay.stopping_points
    assert first.metadata == replay.metadata
    assert first.warnings == replay.warnings
    assert first.metadata["random_seed"] == 4242

    assert different.metadata["random_seed"] == 4243
    assert not np.array_equal(first.intensity, different.intensity)
    assert first.metadata["particles_left_the_aoi"] > 0
    assert any("ran off the edge of the study area" in warning for warning in first.warnings)


def test_particle_density_accumulates_every_duplicate_particle_visit() -> None:
    """Ensemble density counts particles, not merely unique occupied cells."""
    terrain, zone = _planar_runout_case(
        shape=(20, 11), grade_deg=35.0, one_cell_zone=True
    )
    particle_count = 17

    result = _simulate(
        "advanced",
        terrain,
        zone,
        _runout_config(
            particles=particle_count,
            max_steps=1,
            jitter=0.0,
        ),
        seed=1234,
    )

    assert result.metadata["particle_cell_visits"] == particle_count
    assert result.metadata["maximum_particle_visits_per_cell"] == particle_count


@pytest.mark.parametrize("mode", sorted(runout.ENGINES))
def test_runout_engines_return_an_explicit_empty_zone_result(mode: str) -> None:
    terrain, _ = _planar_runout_case(shape=(20, 20))
    empty = ReleaseZone("RZ-empty", np.zeros(terrain.grid.shape, dtype=bool), geometry=None)

    result = _simulate(mode, terrain, empty, _runout_config())

    assert not result.reached.any()
    assert not result.uncertainty.any()
    assert np.count_nonzero(result.intensity) == 0
    assert np.count_nonzero(result.velocity) == 0
    assert result.warnings == ["The release zone contained no cells."]


@pytest.mark.parametrize("mode", sorted(runout.ENGINES))
def test_runout_engines_fail_closed_when_every_release_cell_lacks_elevation(mode: str) -> None:
    terrain, zone = _planar_runout_case(shape=(20, 20))
    elevation = terrain.layer("elevation").copy()
    elevation.mask = np.ma.getmaskarray(elevation).copy()
    elevation.mask[zone.pixels] = True
    terrain._layers["elevation"] = elevation

    result = _simulate(mode, terrain, zone, _runout_config())

    assert not result.reached.any()
    assert not result.uncertainty.any()
    assert any("no cells with" in warning for warning in result.warnings)


def test_unknown_runout_engine_and_release_size_fail_closed() -> None:
    with pytest.raises(KeyError, match="Unknown simulation mode"):
        runout.get_engine("not-an-engine")

    terrain, zone = _planar_runout_case(shape=(20, 20), one_cell_zone=True)
    with pytest.raises(KeyError, match="Unknown release size"):
        _simulate("fast", terrain, zone, _runout_config(), release_size="enormous")
