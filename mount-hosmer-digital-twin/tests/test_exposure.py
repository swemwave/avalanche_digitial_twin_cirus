"""What the simulated avalanche hits -- and what it must refuse to claim it missed.

Two failure modes are under test here, and neither one crashes.

The first is geometric. A road is a LineString with two endpoints a kilometre apart.
If the intersection is tested only at the supplied vertices, a runout crossing the
middle of that road touches no vertex, and the road is reported as untouched. The
answer is not "no roads are exposed" -- it is "we sampled the road in two places and
neither was in the path". ``_densify`` exists to close that gap, and
``test_a_road_is_detected_even_when_no_vertex_is_in_the_runout`` is what proves it
still does.

The second is epistemic, and it is the one this codebase cares about most. The OSM
extract for this AOI contains exactly ONE building. That is a statement about
OpenStreetMap's coverage of rural British Columbia, not about the mountain. So a
result of "0 buildings exposed" is not reassurance, and every result says so.
"""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest

from app.core.model_config import load_model_config
from app.core.settings import get_settings
from app.processing.harmonization.grids import AnalysisGrid
from app.simulation import exposure
from tests.synthetic import EXTENT, osm_features

WEST, SOUTH, EAST, NORTH = EXTENT
RESOLUTION = 10.0

GRID = AnalysisGrid("terrain", RESOLUTION, WEST, SOUTH, EAST, NORTH, "EPSG:26911")

# The synthetic road runs east-west 500 m south of the peak, from col 5 to col 115.
# Its only two vertices are those endpoints.
ROAD_ROW = 110
PEAK_ROW, PEAK_COL = 60, 60


@pytest.fixture
def settings(tmp_path):
    """Settings whose data root holds nothing but the OSM extract."""
    osm_dir = tmp_path / "static" / "openstreetmap"
    osm_dir.mkdir(parents=True)
    (osm_dir / "mount_hosmer_osm_features.geojson").write_text(
        json.dumps(osm_features()), encoding="utf-8"
    )
    return dataclasses.replace(get_settings(), data_root=tmp_path)


def fields(reached: np.ndarray, *, intensity: float = 0.8, velocity: float = 20.0):
    """Flow fields that are non-zero only where the avalanche actually reached."""
    return (
        np.where(reached, intensity, 0.0).astype("float32"),
        np.where(reached, velocity, 0.0).astype("float32"),
    )


def release_at_peak() -> np.ndarray:
    pixels = np.zeros(GRID.shape, dtype=bool)
    pixels[PEAK_ROW - 2 : PEAK_ROW + 2, PEAK_COL - 2 : PEAK_COL + 2] = True
    return pixels


def narrow_swath() -> np.ndarray:
    """A path down the fall line that crosses the road but misses both its endpoints."""
    reached = np.zeros(GRID.shape, dtype=bool)
    reached[PEAK_ROW:ROAD_ROW + 3, 20:31] = True
    return reached


def analyze(settings, reached: np.ndarray) -> exposure.ExposureResult:
    intensity, velocity = fields(reached)
    return exposure.analyze(
        settings=settings,
        config=load_model_config(settings),
        grid=GRID,
        reached=reached,
        intensity=intensity,
        velocity=velocity,
        release_pixels=release_at_peak(),
    )


# --- Intersection -------------------------------------------------------------


def test_a_road_is_detected_even_when_no_vertex_is_in_the_runout(settings) -> None:
    """The road's only two vertices are outside the path. It is still hit.

    This is the whole reason `_densify` exists. The runout crosses the road at
    columns 20-30; the road's vertices are at columns 5 and 115. Sampling the
    geometry only where OSM happened to put a vertex would report this road as
    untouched -- a false negative that reads exactly like a real "nothing exposed".
    """
    reached = narrow_swath()
    assert not reached[ROAD_ROW, 5] and not reached[ROAD_ROW, 115], (
        "Fixture is wrong: at least one road endpoint is inside the runout, so this "
        "test would pass without any densification at all."
    )
    assert reached[ROAD_ROW, 25], "Fixture is wrong: the runout does not cross the road."

    result = analyze(settings, reached)

    roads = [asset for asset in result.assets if asset.category == "roads"]
    assert roads, "The runout crosses the road, but no road was reported as exposed."
    assert roads[0].name == "Synthetic Valley Road"
    assert roads[0].intersects

    # The swath is 11 cells wide at 10 m, so roughly 100 m of road is inside it.
    assert 50.0 < (roads[0].length_in_runout_m or 0.0) < 200.0, (
        f"Reported {roads[0].length_in_runout_m} m of road in a runout ~110 m wide."
    )


def test_flow_intensity_and_velocity_are_taken_from_the_cells_actually_hit(settings) -> None:
    reached = narrow_swath()
    result = analyze(settings, reached)

    road = next(asset for asset in result.assets if asset.category == "roads")
    assert road.max_intensity == pytest.approx(0.8, abs=1e-3)
    assert road.max_velocity_ms == pytest.approx(20.0, abs=1e-3)
    assert road.distance_from_release_m is not None and road.distance_from_release_m > 0


def test_an_asset_outside_the_runout_is_not_reported(settings) -> None:
    """The building sits at column ~63; the swath is at columns 20-30."""
    result = analyze(settings, narrow_swath())

    assert not [asset for asset in result.assets if asset.category == "buildings"]
    assert result.summary["buildings_in_runout"] == 0


def test_a_building_inside_the_runout_is_reported(settings) -> None:
    reached = np.zeros(GRID.shape, dtype=bool)
    reached[PEAK_ROW:ROAD_ROW + 3, 55:70] = True  # a swath that does cover the building

    result = analyze(settings, reached)

    buildings = [asset for asset in result.assets if asset.category == "buildings"]
    assert buildings, "The building is inside the runout but was not reported."
    assert buildings[0].name == "Synthetic Cabin"
    assert result.summary["buildings_in_runout"] == 1
    # A building is a point, not a line; a length would be meaningless.
    assert buildings[0].length_in_runout_m is None


def test_nothing_is_exposed_when_the_avalanche_reaches_nothing(settings) -> None:
    result = analyze(settings, np.zeros(GRID.shape, dtype=bool))
    assert result.assets == []
    assert result.consequence_score == 0.0


# --- Honesty ------------------------------------------------------------------


def test_zero_exposed_buildings_never_reads_as_zero_risk(settings) -> None:
    """A missing building is not evidence that no building exists.

    The consequence engine is fed an OSM extract with one building in it. If it ever
    reported "0 buildings affected" as a clean result, it would be manufacturing
    reassurance out of a data gap -- the same class of error as letting a missing
    snowfall reading become a snowfall of zero.
    """
    result = analyze(settings, narrow_swath())

    assert result.summary["buildings_in_runout"] == 0

    blob = " ".join(result.warnings).lower()
    assert "not evidence" in blob or "incomplete" in blob, (
        "A result with zero exposed buildings carried no completeness warning. It reads "
        "as 'nothing is at risk', which the building data cannot support."
    )
    assert result.completeness["building_data_is_complete"] is False


def test_an_empty_runout_still_refuses_to_say_nothing_is_at_risk(settings) -> None:
    result = analyze(settings, np.zeros(GRID.shape, dtype=bool))

    blob = " ".join(result.warnings).lower()
    assert "not be read as" in blob or "not evidence" in blob, (
        "A runout that hit no known asset reported a bare consequence of 0 with no "
        "caveat. Given the building data, that is a claim the model cannot make."
    )


def test_missing_infrastructure_data_is_reported_not_silently_scored_zero(tmp_path) -> None:
    """No OSM file at all must not look like 'a clean mountain with nothing on it'."""
    settings = dataclasses.replace(get_settings(), data_root=tmp_path)  # no static/ at all
    reached = narrow_swath()
    intensity, velocity = fields(reached)

    result = exposure.analyze(
        settings=settings,
        config=load_model_config(settings),
        grid=GRID,
        reached=reached,
        intensity=intensity,
        velocity=velocity,
        release_pixels=release_at_peak(),
    )

    assert result.assets == []
    blob = " ".join(result.warnings).lower()
    assert "missing" in blob, "A missing OSM file produced no warning about missing data."
    assert "meaningless" in blob, (
        "With no infrastructure data loaded, the consequence score is meaningless and must "
        "say so rather than presenting a confident 0."
    )
