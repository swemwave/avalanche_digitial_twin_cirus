"""Raster I/O with resampling chosen by what the data *means*.

The choice of resampling kernel is a scientific decision, not a performance one.
Bilinearly interpolating an ESA WorldCover class raster produces land-cover class
7.4, which does not exist. Nearest-neighbour downsampling a 1 m DEM to 5 m throws
away 24 of every 25 measurements and keeps whichever one happened to land under
the pixel centre, which adds aliasing noise to slope exactly where slope matters.

So callers declare the *semantics* of the band and this module picks the kernel:

===============  ==================  ==================
Semantics        Downsample          Upsample
===============  ==================  ==================
CONTINUOUS       average             bilinear
INDEX            average             bilinear
CATEGORICAL      mode                nearest
BINARY           mode                nearest
===============  ==================  ==================

NoData is preserved end to end. Nothing here ever substitutes zero for missing.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import numpy as np

from app.processing.harmonization.grids import NODATA, AnalysisGrid

try:  # pragma: no cover
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]
    Resampling = None  # type: ignore[assignment]


class Semantics(str, Enum):
    """What a band's numbers actually represent."""

    CONTINUOUS = "continuous"
    """Physical quantity with a meaningful mean: elevation, temperature, reflectance."""

    INDEX = "index"
    """Normalized ratio with a meaningful mean: NDSI, NDVI, NDMI."""

    CATEGORICAL = "categorical"
    """Class codes with no meaningful mean: land cover, SCL, QA bitmask."""

    BINARY = "binary"
    """A 0/1 mask."""


def _require_rasterio() -> None:
    if rasterio is None:  # pragma: no cover
        raise RuntimeError("rasterio is required for raster processing")


def resampling_for(semantics: Semantics, *, downsampling: bool) -> "Resampling":
    """Pick the kernel that preserves the meaning of the band."""
    if semantics in (Semantics.CATEGORICAL, Semantics.BINARY):
        return Resampling.mode if downsampling else Resampling.nearest
    return Resampling.average if downsampling else Resampling.bilinear


def read_aligned(
    path: Path,
    grid: AnalysisGrid,
    semantics: Semantics = Semantics.CONTINUOUS,
    band: int = 1,
) -> np.ma.MaskedArray:
    """Read a source raster onto ``grid``, reprojecting and resampling as needed.

    Pixels the source does not cover come back masked, never zero-filled.
    """
    _require_rasterio()
    with rasterio.open(path) as src:
        source_res = float(src.res[0])
        downsampling = grid.resolution_m > source_res
        destination = np.full(grid.shape, NODATA, dtype="float32")
        reproject(
            source=rasterio.band(src, band),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=NODATA,
            resampling=resampling_for(semantics, downsampling=downsampling),
        )
    invalid = ~np.isfinite(destination) | (destination == NODATA)
    return np.ma.array(destination, mask=invalid)


def write_raster(
    path: Path,
    array: np.ma.MaskedArray | np.ndarray,
    grid: AnalysisGrid,
    *,
    dtype: str = "float32",
    build_overviews: bool = True,
) -> Path:
    """Write a single-band raster on ``grid`` as a tiled, compressed GeoTIFF.

    Overviews are built so the API and the browser can request a cheap
    low-resolution preview instead of pulling a 2400x2400 float raster.
    """
    _require_rasterio()
    if array.shape != grid.shape:
        raise ValueError(
            f"Array shape {array.shape} does not match the {grid.name} grid {grid.shape}. "
            f"Align it with read_aligned() before writing."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    filled = np.ma.asarray(array).filled(NODATA).astype(dtype)
    with rasterio.open(path, "w", **grid.profile(dtype=dtype)) as dst:
        dst.write(filled, 1)
        if build_overviews:
            dst.build_overviews([2, 4, 8, 16], Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")
    return path
