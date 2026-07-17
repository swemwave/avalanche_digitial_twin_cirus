"""Terrain derivatives for avalanche analysis.

Sign conventions are fixed here and used everywhere downstream. They are chosen
to match how avalanche terrain is actually reasoned about:

``general_curvature``
    Positive = convex (ridge, dome, roll). Negative = concave (bowl, gully).
``profile_curvature``
    Curvature *along* the fall line. Positive = convex, the slope steepens
    downhill and flow accelerates. Convex rolls are where slabs go into tension.
``plan_curvature``
    Curvature *across* the fall line. Positive = divergent (spur, spreading
    flow). Negative = convergent (gully, channelling flow). Convergent terrain is
    what turns a moving avalanche into a deep, destructive one.

Depressions are deliberately **not** filled before flow routing. In hydrology a
sink is an artefact to be removed; in avalanche modelling a sink is a real place
where moving snow decelerates and stops. Filling them would erase the terrain
traps we are trying to find.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import ndimage

from app.processing.harmonization.grids import AnalysisGrid

# 3x3 neighbour offsets, clockwise from north, with their D8 codes.
D8_OFFSETS: list[tuple[int, int]] = [
    (-1, 0),   # 1  N
    (-1, 1),   # 2  NE
    (0, 1),    # 3  E
    (1, 1),    # 4  SE
    (1, 0),    # 5  S
    (1, -1),   # 6  SW
    (0, -1),   # 7  W
    (-1, -1),  # 8  NW
]


@dataclass
class FlowNetwork:
    direction: np.ndarray       # D8 code 1-8, 0 where there is no downhill neighbour
    accumulation: np.ndarray    # contributing cell count, including the cell itself
    receiver: np.ndarray        # flat index of the downstream cell, -1 for sinks
    sinks: np.ndarray           # boolean, cells with no lower neighbour


def _shift_replicate(array: np.ndarray, drow: int, dcol: int) -> np.ndarray:
    """Neighbour value at (drow, dcol), edge-replicated.

    Edge replication rather than wrap-around: a cell on the AOI boundary sees
    itself as its own neighbour, giving zero gradient there instead of a
    fictitious cliff from the opposite side of the mountain.
    """
    rows, cols = array.shape
    row_index = np.clip(np.arange(rows) + drow, 0, rows - 1)
    col_index = np.clip(np.arange(cols) + dcol, 0, cols - 1)
    return array[np.ix_(row_index, col_index)]


def _fill_for_analysis(dem: np.ma.MaskedArray) -> tuple[np.ndarray, np.ndarray]:
    """Return a gap-free working array plus the original validity mask.

    Holes are filled by nearest-valid-neighbour so that convolution kernels do
    not smear NoData across the raster. Every derivative is re-masked with the
    original mask before it is returned, so filled pixels never leak into an
    output.
    """
    mask = np.ma.getmaskarray(dem)
    filled = np.asarray(dem.filled(np.nan), dtype="float64")
    if mask.any() and not mask.all():
        indices = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
        filled = filled[tuple(indices)]
    elif mask.all():
        filled = np.zeros_like(filled)
    return np.nan_to_num(filled, nan=0.0), mask


def slope_aspect(dem: np.ma.MaskedArray, grid: AnalysisGrid) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    """Slope in degrees and aspect in degrees clockwise from north (Horn 1981).

    Horn's 3x3 kernel is used rather than a simple central difference because it
    weights the eight neighbours, which is markedly less noisy on real LiDAR --
    and slope noise translates directly into false release zones.
    """
    z, mask = _fill_for_analysis(dem)
    cell = grid.resolution_m

    z1 = _shift_replicate(z, -1, -1)
    z2 = _shift_replicate(z, -1, 0)
    z3 = _shift_replicate(z, -1, 1)
    z4 = _shift_replicate(z, 0, -1)
    z6 = _shift_replicate(z, 0, 1)
    z7 = _shift_replicate(z, 1, -1)
    z8 = _shift_replicate(z, 1, 0)
    z9 = _shift_replicate(z, 1, 1)

    dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8.0 * cell)
    dzdy = ((z7 + 2 * z8 + z9) - (z1 + 2 * z2 + z3)) / (8.0 * cell)

    slope = np.rad2deg(np.arctan(np.hypot(dzdx, dzdy)))
    aspect = np.rad2deg(np.arctan2(dzdy, -dzdx))
    aspect = np.where(aspect < 0, 90.0 - aspect, np.where(aspect > 90.0, 450.0 - aspect, 90.0 - aspect))
    # Flat ground has no aspect. -1 is the standard sentinel; masking it would
    # lose the distinction between "flat" and "no data".
    aspect = np.where(np.hypot(dzdx, dzdy) < 1e-8, -1.0, aspect % 360.0)

    return (
        np.ma.array(slope.astype("float32"), mask=mask),
        np.ma.array(aspect.astype("float32"), mask=mask),
    )


def curvatures(dem: np.ma.MaskedArray, grid: AnalysisGrid) -> dict[str, np.ma.MaskedArray]:
    """General, profile and plan curvature (Zevenbergen & Thorne 1987).

    Units are 1/100 m, the conventional reporting unit for terrain curvature.
    """
    z, mask = _fill_for_analysis(dem)
    cell = float(grid.resolution_m)

    z1 = _shift_replicate(z, -1, -1)
    z2 = _shift_replicate(z, -1, 0)
    z3 = _shift_replicate(z, -1, 1)
    z4 = _shift_replicate(z, 0, -1)
    z5 = z
    z6 = _shift_replicate(z, 0, 1)
    z7 = _shift_replicate(z, 1, -1)
    z8 = _shift_replicate(z, 1, 0)
    z9 = _shift_replicate(z, 1, 1)

    d = ((z4 + z6) / 2.0 - z5) / cell**2
    e = ((z2 + z8) / 2.0 - z5) / cell**2
    f = (-z1 + z3 + z7 - z9) / (4.0 * cell**2)
    g = (-z4 + z6) / (2.0 * cell)
    h = (z2 - z8) / (2.0 * cell)

    gh = g * g + h * h
    safe = np.where(gh < 1e-12, 1e-12, gh)

    general = -2.0 * (d + e) * 100.0
    profile = -2.0 * (d * g * g + e * h * h + f * g * h) / safe * 100.0
    # Negated relative to the ArcGIS "plan curvature" sign so that the result
    # reads the way avalanche terrain is described: convergent gully negative,
    # divergent spur positive. Verified against synthetic surfaces in
    # tests/test_terrain_derivatives.py.
    plan = -2.0 * (d * h * h + e * g * g - f * g * h) / safe * 100.0

    # Where the surface is flat there is no fall line, so directional curvature
    # is undefined rather than zero. Report it as zero but keep general curvature,
    # which remains well defined.
    flat = gh < 1e-12
    profile = np.where(flat, 0.0, profile)
    plan = np.where(flat, 0.0, plan)

    return {
        "general_curvature": np.ma.array(general.astype("float32"), mask=mask),
        "profile_curvature": np.ma.array(profile.astype("float32"), mask=mask),
        "plan_curvature": np.ma.array(plan.astype("float32"), mask=mask),
    }


def hillshade(
    dem: np.ma.MaskedArray,
    grid: AnalysisGrid,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
    z_factor: float = 1.0,
) -> np.ma.MaskedArray:
    slope, aspect = slope_aspect(dem, grid)
    slope_rad = np.deg2rad(np.asarray(slope.filled(0)) * z_factor)
    aspect_rad = np.deg2rad(np.where(np.asarray(aspect.filled(-1)) < 0, 0.0, np.asarray(aspect.filled(0))))
    zenith = np.deg2rad(90.0 - altitude_deg)
    azimuth = np.deg2rad(360.0 - azimuth_deg + 90.0)
    shaded = 255.0 * (
        np.cos(zenith) * np.cos(slope_rad)
        + np.sin(zenith) * np.sin(slope_rad) * np.cos(azimuth - aspect_rad)
    )
    return np.ma.array(
        np.clip(shaded, 0, 255).astype("float32"), mask=np.ma.getmaskarray(dem)
    )


def ruggedness(dem: np.ma.MaskedArray, grid: AnalysisGrid) -> dict[str, np.ma.MaskedArray]:
    """Terrain Ruggedness Index (Riley 1999) and roughness (relief in a 3x3)."""
    z, mask = _fill_for_analysis(dem)
    squares = np.zeros_like(z)
    for drow, dcol in D8_OFFSETS:
        squares += (_shift_replicate(z, drow, dcol) - z) ** 2
    tri = np.sqrt(squares)

    maximum = ndimage.maximum_filter(z, size=3, mode="nearest")
    minimum = ndimage.minimum_filter(z, size=3, mode="nearest")
    roughness = maximum - minimum

    return {
        "tri": np.ma.array(tri.astype("float32"), mask=mask),
        "roughness": np.ma.array(roughness.astype("float32"), mask=mask),
    }


def topographic_position(
    dem: np.ma.MaskedArray,
    grid: AnalysisGrid,
    radii_m: tuple[float, ...] = (25.0, 100.0, 500.0),
) -> dict[str, np.ma.MaskedArray]:
    """Topographic Position Index at several neighbourhood sizes.

    Scale is the whole point. At 25 m, TPI finds micro-rolls and small gully
    banks. At 500 m it finds the main ridge lines and the valley floor. A cell
    can be a local high point on a slope that is, at mountain scale, a bowl --
    and only a multi-scale reading tells you that.
    """
    z, mask = _fill_for_analysis(dem)
    result: dict[str, np.ma.MaskedArray] = {}
    for radius in radii_m:
        cells = max(1, int(round(radius / grid.resolution_m)))
        size = 2 * cells + 1
        mean = ndimage.uniform_filter(z, size=size, mode="nearest")
        tpi = z - mean
        result[f"tpi_{int(radius)}m"] = np.ma.array(tpi.astype("float32"), mask=mask)
    return result


def flow_network(dem: np.ma.MaskedArray, grid: AnalysisGrid) -> FlowNetwork:
    """D8 steepest-descent routing and contributing-area accumulation.

    Accumulation is computed by peeling the flow tree in topological waves
    (Kahn's algorithm, vectorized). Each cell is visited exactly once, so this is
    O(n) and runs in well under a second on the 2400x2400 terrain grid -- where
    the previous per-cell Python loop would have taken minutes.
    """
    z, mask = _fill_for_analysis(dem)
    valid = ~mask
    rows, cols = z.shape
    cell = grid.resolution_m

    best_drop = np.zeros((rows, cols), dtype="float64")
    direction = np.zeros((rows, cols), dtype="uint8")
    receiver = np.full((rows, cols), -1, dtype="int64")

    flat_index = np.arange(rows * cols, dtype="int64").reshape(rows, cols)

    for code, (drow, dcol) in enumerate(D8_OFFSETS, start=1):
        distance = np.hypot(drow * cell, dcol * cell)
        neighbour = _shift_replicate(z, drow, dcol)
        neighbour_index = _shift_replicate(flat_index, drow, dcol)
        neighbour_valid = _shift_replicate(valid, drow, dcol)
        # A cell on the boundary replicates itself; exclude that self-reference,
        # and never route into a NoData neighbour -- a hole in the DEM is not a
        # place for snow to flow to.
        is_self = neighbour_index == flat_index
        drop = (z - neighbour) / distance
        better = (drop > best_drop) & valid & neighbour_valid & ~is_self
        best_drop = np.where(better, drop, best_drop)
        direction = np.where(better, code, direction)
        receiver = np.where(better, neighbour_index, receiver)

    direction[~valid] = 0
    receiver[~valid] = -1
    sinks = valid & (receiver < 0)

    accumulation = _accumulate(receiver.ravel(), valid.ravel(), rows * cols).reshape(rows, cols)
    accumulation[~valid] = 0.0

    return FlowNetwork(
        direction=direction,
        accumulation=accumulation,
        receiver=receiver,
        sinks=sinks,
    )


def _accumulate(receiver: np.ndarray, valid: np.ndarray, size: int) -> np.ndarray:
    """Sum contributing cells down the D8 tree, one topological wave at a time."""
    accumulation = valid.astype("float64")
    has_receiver = receiver >= 0

    indegree = np.zeros(size, dtype="int64")
    np.add.at(indegree, receiver[has_receiver], 1)

    frontier = np.flatnonzero(valid & (indegree == 0))
    # Strict descent guarantees an acyclic tree, so this terminates with every
    # valid cell drained exactly once.
    while frontier.size:
        targets = receiver[frontier]
        routed = targets >= 0
        source = frontier[routed]
        target = targets[routed]
        if source.size == 0:
            break
        np.add.at(accumulation, target, accumulation[source])
        np.add.at(indegree, target, -1)
        frontier = np.unique(target[indegree[target] == 0])

    return accumulation


def distance_to(mask: np.ndarray, grid: AnalysisGrid) -> np.ndarray:
    """Euclidean distance in metres to the nearest True cell."""
    if not mask.any():
        return np.full(mask.shape, np.inf, dtype="float32")
    return (ndimage.distance_transform_edt(~mask) * grid.resolution_m).astype("float32")


def elevation_bands(
    dem: np.ma.MaskedArray,
    below_treeline_max_m: float,
    treeline_max_m: float,
) -> np.ma.MaskedArray:
    """Avalanche Canada elevation bands: 1 below treeline, 2 treeline, 3 alpine.

    The thresholds are configuration, not physics -- treeline varies by aspect and
    by range -- so they live in avalanche_model.yaml (terrain.below_treeline_max_m,
    terrain.treeline_max_m) and are passed in.
    """
    elevation = np.asarray(dem.filled(0.0))
    band = np.ones(elevation.shape, dtype="float32")
    band[elevation > below_treeline_max_m] = 2.0
    band[elevation > treeline_max_m] = 3.0
    return np.ma.array(band, mask=np.ma.getmaskarray(dem))


def canopy_height(
    dsm: np.ma.MaskedArray,
    dem: np.ma.MaskedArray,
    max_height_m: float = 60.0,
) -> np.ma.MaskedArray:
    """Canopy height model = DSM - DEM, valid only where both surfaces exist.

    Negative values are physically impossible (the surface cannot be below the
    ground) and indicate co-registration error between the two acquisitions, so
    they are clamped to zero. Values above ``max_height_m`` are treated as noise
    and masked rather than clamped, because a 200 m "tree" is not a short tree,
    it is a bad pixel.
    """
    both = ~np.ma.getmaskarray(dsm) & ~np.ma.getmaskarray(dem)
    height = np.asarray(dsm.filled(0.0)) - np.asarray(dem.filled(0.0))
    height = np.where(height < 0, 0.0, height)
    invalid = ~both | (height > max_height_m)
    return np.ma.array(height.astype("float32"), mask=invalid)


def slope_band_continuity(
    slope: np.ma.MaskedArray,
    grid: AnalysisGrid,
    low_deg: float,
    high_deg: float,
    radius_m: float = 50.0,
) -> np.ma.MaskedArray:
    """Fraction of the surrounding terrain that is also in the avalanche slope band.

    A 40-degree pixel surrounded by cliffs and benches cannot produce a slab; a
    40-degree pixel in the middle of an unbroken 40-degree face can. Slope alone
    cannot tell those apart. This can.
    """
    in_band = (
        (np.asarray(slope.filled(0.0)) >= low_deg) & (np.asarray(slope.filled(0.0)) <= high_deg)
    ).astype("float64")
    cells = max(1, int(round(radius_m / grid.resolution_m)))
    fraction = ndimage.uniform_filter(in_band, size=2 * cells + 1, mode="nearest")
    return np.ma.array(fraction.astype("float32"), mask=np.ma.getmaskarray(slope))


def summarize_flow(network: FlowNetwork) -> dict[str, Any]:
    return {
        "sink_count": int(network.sinks.sum()),
        "max_accumulation_cells": float(network.accumulation.max()),
        "routed_cells": int((network.receiver >= 0).sum()),
        "note": (
            "Depressions are not filled. In avalanche modelling a closed depression is a real "
            "deceleration and deposition site, not a DEM artefact to be removed."
        ),
    }
