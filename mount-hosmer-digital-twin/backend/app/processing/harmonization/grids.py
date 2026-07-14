"""Common analysis grids for the Mount Hosmer AOI.

Every raster the application produces is written on one of these grids, in the
same projected CRS, with the same origin and pixel alignment. That is what makes
layers stackable: a pixel at (row, col) on the terrain grid refers to exactly the
same ground as the pixel at (row, col) on any other terrain-grid layer.

Three grids exist, and the reason there are three is the reason the whole
harmonization layer exists:

``terrain`` (5 m default)
    Built from the 1 m BC LiDAR. Slope, curvature, gullies and ridges are the
    inputs that actually decide where an avalanche starts, and they are exactly
    the quantities destroyed by coarse pixels. This grid must never be derived
    from the 30 m DEM where LiDAR exists.

``environmental`` (10 m)
    The native resolution of Sentinel-2 and ESA WorldCover. Snow cover, land
    cover, and vegetation live here. Resampling these to 5 m would invent detail
    that the sensor never measured.

``fallback`` (30 m)
    The native resolution of Copernicus GLO-30 and Landsat. Used only where no
    better source exists, and always recorded as such in the provenance raster.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.settings import Settings

try:  # pragma: no cover - exercised implicitly wherever rasterio is installed
    import rasterio
    from rasterio.coords import BoundingBox
    from rasterio.crs import CRS
    from rasterio.transform import Affine, from_origin
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]

#: Used only when metadata/grid_and_aoi.json is unavailable. These are the
#: published AOI bounds for Mount Hosmer in EPSG:26911.
DEFAULT_EXTENT = (637650.0, 5491570.0, 649650.0, 5503570.0)

NODATA = -9999.0


@dataclass(frozen=True)
class AnalysisGrid:
    """An immutable raster grid definition."""

    name: str
    resolution_m: float
    west: float
    south: float
    east: float
    north: float
    crs_string: str

    @property
    def width(self) -> int:
        return int(round((self.east - self.west) / self.resolution_m))

    @property
    def height(self) -> int:
        return int(round((self.north - self.south) / self.resolution_m))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def pixel_count(self) -> int:
        return self.height * self.width

    @property
    def transform(self) -> "Affine":
        return from_origin(self.west, self.north, self.resolution_m, self.resolution_m)

    @property
    def crs(self) -> "CRS":
        return CRS.from_string(self.crs_string)

    @property
    def bounds(self) -> "BoundingBox":
        return BoundingBox(self.west, self.south, self.east, self.north)

    def profile(self, *, dtype: str = "float32", count: int = 1, nodata: float | None = NODATA) -> dict[str, Any]:
        """A rasterio creation profile for this grid.

        Cloud Optimized GeoTIFF layout: internally tiled with 512 px blocks and
        DEFLATE-compressed, so the API can read windows without pulling whole
        rasters into memory.
        """
        return {
            "driver": "GTiff",
            "height": self.height,
            "width": self.width,
            "count": count,
            "dtype": dtype,
            "crs": self.crs,
            "transform": self.transform,
            "nodata": nodata,
            "compress": "deflate",
            "predictor": 2 if dtype.startswith("float") else 1,
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "BIGTIFF": "IF_SAFER",
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "resolution_m": self.resolution_m,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "crs": self.crs_string,
            "bounds": [self.west, self.south, self.east, self.north],
        }

    def matches(self, other: "AnalysisGrid") -> bool:
        return (
            self.crs_string == other.crs_string
            and self.resolution_m == other.resolution_m
            and self.shape == other.shape
            and (self.west, self.north) == (other.west, other.north)
        )


@dataclass(frozen=True)
class GridSet:
    terrain: AnalysisGrid
    environmental: AnalysisGrid
    fallback: AnalysisGrid

    def by_name(self, name: str) -> AnalysisGrid:
        try:
            return {
                "terrain": self.terrain,
                "environmental": self.environmental,
                "fallback": self.fallback,
            }[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown analysis grid {name!r}; expected terrain, environmental, or fallback."
            ) from exc

    def describe(self) -> dict[str, Any]:
        return {
            "analysis_crs": self.terrain.crs_string,
            "grids": [
                self.terrain.describe(),
                self.environmental.describe(),
                self.fallback.describe(),
            ],
        }


def aoi_extent(settings: Settings) -> tuple[float, float, float, float]:
    """Read the fixed AOI extent from source metadata, falling back to constants."""
    path = settings.data_root / "metadata" / "grid_and_aoi.json"
    if not path.exists():
        return DEFAULT_EXTENT
    try:
        grid = json.loads(path.read_text(encoding="utf-8"))
        west, south, east, north = grid["fixed_extent_analysis_crs"]
        return float(west), float(south), float(east), float(north)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return DEFAULT_EXTENT


def _grid(settings: Settings, name: str, resolution: float) -> AnalysisGrid:
    west, south, east, north = aoi_extent(settings)
    # Snap the extent outward to a whole number of pixels so every grid shares
    # the same origin and no grid is a fractional pixel wider than another.
    east = west + round((east - west) / resolution) * resolution
    north = south + round((north - south) / resolution) * resolution
    return AnalysisGrid(
        name=name,
        resolution_m=resolution,
        west=west,
        south=south,
        east=east,
        north=north,
        crs_string=settings.analysis_crs,
    )


def terrain_grid(settings: Settings) -> AnalysisGrid:
    return _grid(settings, "terrain", settings.terrain_resolution_m)


def environmental_grid(settings: Settings) -> AnalysisGrid:
    return _grid(settings, "environmental", settings.environmental_resolution_m)


def fallback_grid(settings: Settings) -> AnalysisGrid:
    return _grid(settings, "fallback", settings.fallback_resolution_m)


def grid_set(settings: Settings) -> GridSet:
    return GridSet(
        terrain=terrain_grid(settings),
        environmental=environmental_grid(settings),
        fallback=fallback_grid(settings),
    )


def grid_from_path(path: Path, name: str = "source") -> AnalysisGrid:
    """Describe an existing raster as an AnalysisGrid, for alignment checks."""
    if rasterio is None:  # pragma: no cover
        raise RuntimeError("rasterio is required to inspect raster grids")
    with rasterio.open(path) as src:
        bounds = src.bounds
        return AnalysisGrid(
            name=name,
            resolution_m=float(src.res[0]),
            west=float(bounds.left),
            south=float(bounds.bottom),
            east=float(bounds.right),
            north=float(bounds.top),
            crs_string=str(src.crs),
        )
