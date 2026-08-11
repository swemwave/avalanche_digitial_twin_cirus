"""Small compatibility layer over xDEM's tested terrain-attribute algorithms.

The padding and scale adapters preserve the Stage 3 bake's established Horn and
Zevenbergen-Thorne outputs, including its avalanche-facing curvature signs.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage
from xdem import terrain

from app.processing.harmonization.grids import AnalysisGrid


def _fill(dem: np.ma.MaskedArray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-fill gaps for convolution, then let callers restore the mask."""
    mask = np.ma.getmaskarray(dem)
    values = np.asarray(dem.filled(np.nan), dtype="float64")
    if mask.any() and not mask.all():
        nearest = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
        values = values[tuple(nearest)]
    elif mask.all():
        values = np.zeros_like(values)
    return np.nan_to_num(values, nan=0.0), mask


def _crop(values: np.ndarray, cells: int) -> np.ndarray:
    return values[cells:-cells, cells:-cells]


def slope_aspect(
    dem: np.ma.MaskedArray, grid: AnalysisGrid
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    """Horn slope and compass aspect, with ``-1`` retained for flat ground."""
    values, mask = _fill(dem)
    padded = np.pad(values, 1, mode="edge")
    slope = _crop(
        terrain.slope(
            padded, surface_fit="Horn", resolution=grid.resolution_m, engine="scipy"
        ),
        1,
    )
    aspect = _crop(terrain.aspect(padded, surface_fit="Horn", engine="scipy"), 1)
    aspect = np.where(slope < 1e-8, -1.0, aspect)
    return (
        np.ma.array(slope.astype("float32"), mask=mask),
        np.ma.array(aspect.astype("float32"), mask=mask),
    )


def curvatures(dem: np.ma.MaskedArray, grid: AnalysisGrid) -> dict[str, np.ma.MaskedArray]:
    """The two runtime curvature fields backed by xDEM's Z-T implementation.

    The historical ``plan_curvature`` contract is the directional tangential
    curvature (positive divergent, negative convergent), so that exact xDEM
    attribute is used. xDEM's general curvature is divided by two to retain the
    project's established 1/100 m scale.
    """
    values, mask = _fill(dem)
    padded = np.pad(values, 1, mode="edge")
    options = {
        "resolution": grid.resolution_m,
        "surface_fit": "ZevenbergThorne",
        "engine": "scipy",
    }
    general = _crop(terrain.curvature(padded, **options), 1) / 2.0
    plan = _crop(
        terrain.tangential_curvature(padded, curv_method="directional", **options), 1
    )
    return {
        "general_curvature": np.ma.array(general.astype("float32"), mask=mask),
        "plan_curvature": np.ma.array(plan.astype("float32"), mask=mask),
    }


def canopy_height(
    dsm: np.ma.MaskedArray, dem: np.ma.MaskedArray, max_height_m: float = 60.0
) -> np.ma.MaskedArray:
    both = ~np.ma.getmaskarray(dsm) & ~np.ma.getmaskarray(dem)
    height = np.maximum(np.asarray(dsm.filled(0.0)) - np.asarray(dem.filled(0.0)), 0.0)
    return np.ma.array(height.astype("float32"), mask=~both | (height > max_height_m))
