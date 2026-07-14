"""Wind loading and release-zone segmentation, on surfaces with known answers.

These are the tests that catch the two failures that do not announce themselves.

A sign error in the wind vector math does not crash and does not look wrong: it
simply puts every modelled lee slope on the far side of the mountain from the real
one, and the output stays entirely plausible. So the wind tests assert the *lee is
opposite the wind* at several bearings, including across the 0/360 seam.

Labelling release zones without regard to aspect does not crash either. The steep
ground encircling a peak is topologically connected the whole way round it, so a
single connected component floods across the north, east and south faces alike and
returns one enormous "zone" that faces every direction at once. It has no fall
line, so it cannot be simulated, and its mean aspect is an average over the whole
compass. On the real Mount Hosmer data this produced a single 1,614-hectare zone
spanning 1,100 m of vertical, which held 89% of all release area.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.models import release_zones, wind
from app.models.release_zones import aspect_sector, segment_release_zones
from app.processing.harmonization.grids import AnalysisGrid
from app.processing.terrain import derivatives as terrain

GRID = AnalysisGrid("test", 10.0, 0.0, 0.0, 1000.0, 1000.0, "EPSG:26911")

OCTANTS = {
    "N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0,
    "S": 180.0, "SW": 225.0, "W": 270.0, "NW": 315.0,
}


def cone() -> tuple[np.ma.MaskedArray, np.ma.MaskedArray, np.ma.MaskedArray]:
    """A cone. Every flank faces directly away from the apex, so all aspects exist."""
    rows, cols = np.mgrid[0:100, 0:100].astype("float64")
    x, y = cols - 50.0, rows - 50.0
    dem = np.ma.array(
        (2000.0 - 0.6 * np.hypot(x, y) * GRID.resolution_m).astype("float32"),
        mask=np.zeros((100, 100), dtype=bool),
    )
    slope, aspect = terrain.slope_aspect(dem, GRID)
    return dem, slope, aspect


def zeros_like(array: np.ma.MaskedArray) -> np.ma.MaskedArray:
    return np.ma.array(np.zeros(array.shape, dtype="float32"), mask=np.ma.getmaskarray(array))


def octant_of(degrees: np.ndarray) -> np.ndarray:
    return (((degrees + 22.5) % 360.0) // 45.0).astype(int)


# --- Wind vector math ---------------------------------------------------------


def test_angular_difference_is_correct_across_the_zero_seam() -> None:
    # The whole reason direction is handled as a vector: 350 and 10 degrees are 20
    # degrees apart, not 340.
    assert wind.angular_difference(np.array([10.0]), 350.0)[0] == pytest.approx(20.0)
    assert wind.angular_difference(np.array([350.0]), 10.0)[0] == pytest.approx(20.0)
    assert wind.angular_difference(np.array([0.0]), 180.0)[0] == pytest.approx(180.0)
    assert wind.angular_difference(np.array([90.0]), 90.0)[0] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("wind_from", "expected_lee", "expected_windward"),
    [
        (225.0, "NE", "SW"),   # the wind_loading preset: a southwesterly
        (45.0, "SW", "NE"),    # reversed, to catch a symmetric sign error
        (0.0, "S", "N"),       # a northerly, exercising the 0/360 seam
        (270.0, "E", "W"),     # a westerly
    ],
)
def test_lee_deposition_lands_on_the_slope_facing_away_from_the_wind(
    wind_from: float, expected_lee: str, expected_windward: str
) -> None:
    """Snow is stripped from the slope facing the wind and dropped on the far side.

    If this inverts, every modelled wind slab ends up on the wrong side of the
    mountain and nothing about the output looks amiss.
    """
    dem, slope, aspect = cone()
    config = _config()

    fields = wind.compute(
        config=config,
        grid=GRID,
        elevation=dem,
        slope=slope,
        aspect=aspect,
        plan_curvature=zeros_like(dem),
        distance_to_ridge=np.ma.array(np.full(dem.shape, 500.0, "float32")),
        forest_mask=zeros_like(dem),
        snow_availability=np.ma.array(np.ones(dem.shape, "float32")),
        wind_speed_kmh=55.0,
        wind_direction_deg=wind_from,
        wind_consistency=0.9,
    )
    assert fields.available

    aspect_values = np.asarray(aspect.filled(-1.0))
    on_slope = (aspect_values >= 0) & (np.asarray(slope.filled(0.0)) > 20.0)
    octants = octant_of(aspect_values)

    def mean_by_octant(field: np.ma.MaskedArray) -> dict[str, float]:
        values = np.asarray(field.filled(0.0))
        means = {}
        for name, degrees in OCTANTS.items():
            selection = on_slope & (octants == octant_of(np.array([degrees]))[0])
            means[name] = float(values[selection].mean()) if selection.any() else 0.0
        return means

    lee = mean_by_octant(fields.lee_deposition)
    erosion = mean_by_octant(fields.windward_erosion)

    assert max(lee, key=lee.get) == expected_lee, (
        f"Wind from {wind_from:.0f} deg should deposit snow on the {expected_lee} lee slope, "
        f"but deposition peaked on {max(lee, key=lee.get)}. The wind vector math is inverted."
    )
    assert max(erosion, key=erosion.get) == expected_windward, (
        f"Wind from {wind_from:.0f} deg should scour the {expected_windward} windward slope, "
        f"but erosion peaked on {max(erosion, key=erosion.get)}."
    )
    # The two must be opposites, and a slope cannot be scoured and loaded at once.
    assert lee[expected_windward] == pytest.approx(0.0, abs=1e-6)
    assert erosion[expected_lee] == pytest.approx(0.0, abs=1e-6)


def test_wind_cannot_load_a_slope_that_has_no_snow() -> None:
    """A gale over bare rock builds no slab."""
    dem, slope, aspect = cone()
    fields = wind.compute(
        config=_config(),
        grid=GRID,
        elevation=dem,
        slope=slope,
        aspect=aspect,
        plan_curvature=zeros_like(dem),
        distance_to_ridge=np.ma.array(np.full(dem.shape, 500.0, "float32")),
        forest_mask=zeros_like(dem),
        snow_availability=zeros_like(dem),  # no snow anywhere
        wind_speed_kmh=90.0,
        wind_direction_deg=225.0,
        wind_consistency=1.0,
    )
    assert float(fields.lee_deposition.max()) == pytest.approx(0.0, abs=1e-6)
    assert float(fields.wind_loading.max()) == pytest.approx(0.0, abs=1e-6)


def test_missing_wind_is_excluded_not_scored_as_zero() -> None:
    """Invariant I3: unknown wind is unknown, not calm."""
    dem, slope, aspect = cone()
    fields = wind.compute(
        config=_config(),
        grid=GRID,
        elevation=dem,
        slope=slope,
        aspect=aspect,
        plan_curvature=zeros_like(dem),
        distance_to_ridge=np.ma.array(np.full(dem.shape, 500.0, "float32")),
        forest_mask=zeros_like(dem),
        snow_availability=np.ma.array(np.ones(dem.shape, "float32")),
        wind_speed_kmh=None,
        wind_direction_deg=None,
        wind_consistency=None,
    )
    assert fields.available is False
    assert any("EXCLUDED" in warning for warning in fields.warnings)


# --- Release-zone segmentation ------------------------------------------------


def test_aspect_sector_puts_north_in_one_bin_across_the_seam() -> None:
    # 350 and 10 degrees are both north-facing and must land in the same sector,
    # or every north slope in the AOI is split down the middle.
    sectors = aspect_sector(np.array([350.0, 0.0, 10.0, 180.0, -1.0]), 45.0)
    assert sectors[0] == sectors[1] == sectors[2]
    assert sectors[3] != sectors[0]
    assert sectors[4] == -1  # flat ground has no aspect


def test_flat_ground_cannot_bridge_two_opposing_slopes() -> None:
    """A flat col must not fuse the slopes on either side of it into one zone."""
    candidate = np.ones((3, 9), dtype=bool)
    aspect_values = np.array([[0.0] * 4 + [-1.0] + [180.0] * 4] * 3)  # N | flat | S
    labels, count = segment_release_zones(candidate, aspect_values, 45.0)

    north = set(np.unique(labels[:, :4]))
    south = set(np.unique(labels[:, 5:]))
    assert count >= 2
    assert not (north & south), "A flat col bridged a north slope to a south slope."


def test_a_cone_does_not_become_one_zone_facing_every_direction() -> None:
    """The bug, reproduced on a surface where the right answer is not arguable.

    Every flank of a cone is steep and they are all connected around the summit.
    Plain connected-component labelling therefore returns ONE zone, whose 'dominant
    aspect' is a vector mean over the entire compass and is meaningless. On the real
    mountain this fused the whole massif into a single 1,614-hectare zone.
    """
    _, slope, aspect = cone()
    aspect_values = np.asarray(aspect.filled(-1.0))
    candidate = np.asarray(slope.filled(0.0)) > 25.0
    assert candidate.sum() > 1000  # the whole cone flank qualifies

    from scipy import ndimage

    naive, naive_count = ndimage.label(candidate, structure=np.ones((3, 3), dtype=int))
    assert naive_count == 1, "Precondition: the cone flank is one connected component."

    # And that single naive component genuinely spans the whole compass.
    naive_aspects = aspect_values[naive == 1]
    _, naive_consistency = release_zones.vector_mean_aspect(naive_aspects)
    assert naive_consistency is not None and naive_consistency < 0.2, (
        "Precondition: the naive zone faces every direction at once."
    )

    labels, count = segment_release_zones(candidate, aspect_values, 45.0)
    assert count >= 8, f"A cone must split into at least one zone per aspect sector, got {count}."

    # Every resulting zone must face a coherent direction.
    for label in range(1, count + 1):
        pixels = labels == label
        if pixels.sum() < 20:
            continue
        _, consistency = release_zones.vector_mean_aspect(aspect_values[pixels])
        assert consistency is not None and consistency > 0.75, (
            f"Zone {label} spans too many aspects (consistency {consistency}); "
            f"components merged across aspect sectors."
        )


def test_elevation_bands_bound_how_tall_a_zone_can_be() -> None:
    """Aspect alone still lets a zone run the whole height of a face.

    On the real mountain, aspect-sector segmentation left a zone that held a
    coherent aspect while stretching 3.2 km along the massif and spanning 707 m of
    vertical. A slab is a few hundred metres of vertical at most, and a zone that
    tall has no meaningful crown to simulate a release from.
    """
    _, slope, aspect = cone()
    aspect_values = np.asarray(aspect.filled(-1.0))
    candidate = np.asarray(slope.filled(0.0)) > 25.0

    # The cone falls 0.6 m per metre, so its flank spans hundreds of metres of relief.
    rows, cols = np.mgrid[0:100, 0:100].astype("float64")
    elevation = 2000.0 - 0.6 * np.hypot(cols - 50.0, rows - 50.0) * GRID.resolution_m

    band = 100.0
    labels, count = segment_release_zones(candidate, aspect_values, 45.0, elevation, band)

    for label in range(1, count + 1):
        pixels = labels == label
        if pixels.sum() < 10:
            continue
        relief = float(elevation[pixels].max() - elevation[pixels].min())
        assert relief <= band + 1e-6, (
            f"Zone {label} spans {relief:.0f} m of vertical, more than the {band:.0f} m band."
        )

    # And banding must not undo the aspect guarantee.
    sectors = aspect_sector(aspect_values, 45.0)
    for label in range(1, count + 1):
        pixels = labels == label
        assert len(np.unique(sectors[pixels])) == 1


def test_segmentation_never_merges_across_aspect_sectors() -> None:
    """The invariant itself: one zone, one sector. No exceptions."""
    _, slope, aspect = cone()
    aspect_values = np.asarray(aspect.filled(-1.0))
    candidate = np.asarray(slope.filled(0.0)) > 25.0

    labels, count = segment_release_zones(candidate, aspect_values, 45.0)
    sectors = aspect_sector(aspect_values, 45.0)

    for label in range(1, count + 1):
        pixels = labels == label
        assert len(np.unique(sectors[pixels])) == 1, (
            f"Zone {label} contains more than one aspect sector."
        )


def _config():
    """The real model configuration, so the tests exercise shipped parameters."""
    from app.core.model_config import load_model_config
    from app.core.settings import get_settings

    return load_model_config(get_settings())
