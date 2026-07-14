"""Terrain derivatives, checked against surfaces whose answer is known analytically.

These are the tests that catch a sign flip. A sign flip in plan curvature is not
a cosmetic bug: it would make the model call every gully a spur, and gullies are
where avalanches become deep and deadly.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.processing.harmonization.grids import AnalysisGrid
from app.processing.terrain import derivatives as terrain


@pytest.fixture
def grid() -> AnalysisGrid:
    return AnalysisGrid("test", 1.0, 0.0, 0.0, 100.0, 100.0, "EPSG:26911")


def surface(values: np.ndarray) -> np.ma.MaskedArray:
    return np.ma.array(values.astype("float32"), mask=np.zeros(values.shape, dtype=bool))


@pytest.fixture
def coords() -> tuple[np.ndarray, np.ndarray]:
    rows, cols = np.mgrid[0:100, 0:100].astype("float64")
    # Row index increases southward, matching raster convention.
    return cols - 50.0, rows - 50.0


def test_planar_slope_is_exact(grid: AnalysisGrid, coords) -> None:
    x, y = coords
    for expected_deg in (15.0, 30.0, 45.0):
        dem = surface(-y * np.tan(np.deg2rad(expected_deg)))
        slope, _ = terrain.slope_aspect(dem, grid)
        interior = slope[10:90, 10:90]
        assert np.allclose(interior, expected_deg, atol=0.01)


def test_aspect_points_downhill_clockwise_from_north(grid: AnalysisGrid, coords) -> None:
    x, y = coords
    # A cone: every flank faces directly away from the apex at (50, 50).
    dem = surface(1000.0 - np.hypot(x, y))
    _, aspect = terrain.slope_aspect(dem, grid)

    assert aspect[30, 50] == pytest.approx(0.0, abs=1.0)    # north of apex faces north
    assert aspect[50, 70] == pytest.approx(90.0, abs=1.0)   # east of apex faces east
    assert aspect[70, 50] == pytest.approx(180.0, abs=1.0)  # south of apex faces south
    assert aspect[50, 30] == pytest.approx(270.0, abs=1.0)  # west of apex faces west


def test_flat_ground_has_no_aspect(grid: AnalysisGrid) -> None:
    dem = surface(np.full((100, 100), 1500.0))
    slope, aspect = terrain.slope_aspect(dem, grid)
    assert np.allclose(slope, 0.0)
    # -1 is the flat sentinel. Flat must stay distinguishable from "faces north".
    assert np.all(aspect == -1.0)


def test_general_curvature_is_positive_on_a_dome(grid: AnalysisGrid, coords) -> None:
    x, y = coords
    dome = terrain.curvatures(surface(-0.01 * (x**2 + y**2)), grid)["general_curvature"]
    bowl = terrain.curvatures(surface(+0.01 * (x**2 + y**2)), grid)["general_curvature"]
    assert dome[50, 55] > 0
    assert bowl[50, 55] < 0


def test_profile_curvature_is_positive_where_a_slope_steepens_downhill(grid, coords) -> None:
    x, y = coords
    convex_roll = terrain.curvatures(surface(-0.005 * y**2), grid)["profile_curvature"]
    concave_bench = terrain.curvatures(surface(0.005 * y**2 - y), grid)["profile_curvature"]
    assert convex_roll[60, 50] > 0
    assert concave_bench[60, 50] < 0


def test_plan_curvature_is_negative_in_a_convergent_gully(grid, coords) -> None:
    """The sign that matters most. Convergent must be negative, divergent positive."""
    x, y = coords
    gully = terrain.curvatures(surface(-0.5 * y + 0.01 * x**2), grid)["plan_curvature"]
    spur = terrain.curvatures(surface(-0.5 * y - 0.01 * x**2), grid)["plan_curvature"]
    assert gully[50, 50] < 0, "a convergent gully must have negative plan curvature"
    assert spur[50, 50] > 0, "a divergent spur must have positive plan curvature"


def test_flow_accumulation_conserves_cells(grid: AnalysisGrid, coords) -> None:
    """Every cell drains, and the outlet collects everything upstream of it."""
    x, y = coords
    # A single tilted plane: all flow leaves through the southern edge.
    network = terrain.flow_network(surface(-y * 2.0), grid)
    assert network.accumulation.min() >= 1.0
    # The bottom row must have collected the whole column above it.
    assert network.accumulation[-1, 50] == pytest.approx(100.0)


def test_flow_accumulation_matches_a_known_convergent_basin(grid, coords) -> None:
    x, y = coords
    network = terrain.flow_network(surface(-0.5 * y + 0.01 * x**2), grid)
    # Flow converges onto the x == 0 axis, so low on the basin the axis must
    # carry several times what the flank does. D8 quantizes flow to eight
    # directions, so convergence is real but not perfect -- 3x is the signal.
    assert network.accumulation[90, 50] > 3 * network.accumulation[90, 20]

    # And a plane with no convergence must not show the same signature.
    plane = terrain.flow_network(surface(-0.5 * y), grid)
    assert plane.accumulation[90, 50] == pytest.approx(plane.accumulation[90, 20])


def test_flow_accumulation_is_not_quadratic_in_cell_count(grid) -> None:
    """Guards the vectorized topological peel against a regression to a per-cell loop."""
    import time

    rows, cols = np.mgrid[0:600, 0:600].astype("float64")
    large = AnalysisGrid("large", 1.0, 0.0, 0.0, 600.0, 600.0, "EPSG:26911")
    dem = surface(-rows * 2.0 + 0.001 * (cols - 300.0) ** 2)

    started = time.perf_counter()
    network = terrain.flow_network(dem, large)
    elapsed = time.perf_counter() - started

    assert network.accumulation.max() > 0
    # 360k cells. The old per-cell Python loop took minutes at this size.
    assert elapsed < 15.0, f"flow routing took {elapsed:.1f}s; it should be near-linear"


def test_nodata_never_becomes_zero_elevation(grid: AnalysisGrid, coords) -> None:
    x, y = coords
    values = (1500.0 - y * 2.0).astype("float32")
    mask = np.zeros(values.shape, dtype=bool)
    mask[40:60, 40:60] = True
    dem = np.ma.array(values, mask=mask)

    slope, aspect = terrain.slope_aspect(dem, grid)
    curvature = terrain.curvatures(dem, grid)
    network = terrain.flow_network(dem, grid)

    # The hole stays a hole in every derivative -- it does not become a crater.
    assert np.all(np.ma.getmaskarray(slope)[40:60, 40:60])
    assert np.all(np.ma.getmaskarray(aspect)[40:60, 40:60])
    assert np.all(np.ma.getmaskarray(curvature["general_curvature"])[40:60, 40:60])
    assert np.all(network.accumulation[40:60, 40:60] == 0)
    assert np.all(network.direction[40:60, 40:60] == 0)
    # And the valid terrain around it is still sane.
    assert slope[10, 10] == pytest.approx(np.rad2deg(np.arctan(2.0)), abs=0.01)


def test_canopy_height_rejects_impossible_values(grid: AnalysisGrid) -> None:
    dem = surface(np.full((10, 10), 1000.0))
    dsm_values = np.full((10, 10), 1020.0)
    dsm_values[0, 0] = 995.0    # surface below ground: co-registration error
    dsm_values[0, 1] = 1300.0   # 300 m "tree": a bad pixel
    chm = terrain.canopy_height(surface(dsm_values), dem, max_height_m=60.0)

    assert chm[0, 0] == 0.0, "negative canopy height must clamp to zero, not stay negative"
    assert np.ma.getmaskarray(chm)[0, 1], "an impossible canopy height must be masked, not clamped"
    assert chm[5, 5] == pytest.approx(20.0)


def test_elevation_bands(grid: AnalysisGrid) -> None:
    dem = surface(np.array([[1500.0, 1800.0], [2100.0, 2400.0]]))
    bands = terrain.elevation_bands(dem, below_treeline_max_m=1700.0, treeline_max_m=2000.0)
    assert bands[0, 0] == 1.0  # below treeline
    assert bands[0, 1] == 2.0  # treeline
    assert bands[1, 0] == 3.0  # alpine
    assert bands[1, 1] == 3.0


def test_slope_band_continuity_separates_isolated_from_connected_steep_terrain(grid) -> None:
    values = np.full((100, 100), 5.0)
    values[20:80, 20:80] = 38.0  # a large connected steep face
    values[5, 5] = 38.0          # one isolated steep pixel
    slope = surface(values)

    continuity = terrain.slope_band_continuity(slope, grid, low_deg=30.0, high_deg=45.0, radius_m=10.0)

    assert continuity[50, 50] == pytest.approx(1.0, abs=0.01), "middle of the face is fully connected"
    assert continuity[5, 5] < 0.2, "an isolated steep pixel cannot produce a slab"
