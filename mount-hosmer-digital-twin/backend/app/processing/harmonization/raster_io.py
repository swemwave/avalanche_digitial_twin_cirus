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

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from app.core.settings import Settings
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_raster(path: Path, band: int = 1) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    """Read a band as a masked float32 array, with its profile."""
    _require_rasterio()
    with rasterio.open(path) as src:
        arr = src.read(band, masked=True).astype("float32")
        profile = dict(src.profile)
        profile.update(
            bounds=src.bounds,
            transform=src.transform,
            crs=src.crs,
            resolution=src.res,
        )
        return arr, profile


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


def write_metadata(
    settings: Settings,
    path: Path,
    *,
    layer_id: str,
    title: str,
    grid: AnalysisGrid,
    source_datasets: list[str],
    algorithm: str,
    parameters: dict[str, Any],
    units: str,
    provenance: str,
    raster_path: Path | None = None,
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the sidecar that lets someone reproduce or challenge a raster.

    Records what it was made from, how, with which parameters, on which grid,
    when, by which software version, and the checksum of the result.
    """
    metadata: dict[str, Any] = {
        "id": layer_id,
        "title": title,
        "units": units,
        "provenance": provenance,
        "source_datasets": source_datasets,
        "algorithm": algorithm,
        "parameters": parameters,
        "grid": grid.describe(),
        "generated_at_utc": utc_now_iso(),
        "software_version": settings.app_version,
        "model_version": settings.model_version,
        "warnings": warnings or [],
    }
    if raster_path is not None and raster_path.exists():
        metadata["raster"] = {
            "filename": raster_path.name,
            "size_bytes": raster_path.stat().st_size,
            "sha256": file_sha256(raster_path),
        }
    if extra:
        metadata.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def masked_stats(array: np.ma.MaskedArray | np.ndarray) -> dict[str, float | int | None]:
    """Summary statistics over valid pixels only.

    Returns ``None`` rather than 0 when there are no valid pixels, so an empty
    layer is never mistaken for a layer full of zeros.
    """
    masked = np.ma.asarray(array)
    values = masked.compressed() if np.ma.is_masked(masked) else np.asarray(masked).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "std": None,
            "valid_pixels": 0,
            "valid_fraction": 0.0,
        }
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "std": float(values.std()),
        "valid_pixels": int(values.size),
        "valid_fraction": round(float(values.size) / float(masked.size), 4),
    }
