"""Common analysis grids for the Mount Hosmer AOI.

Every raster the application produces is written on one of these grids, in the
same projected CRS, with the same origin and pixel alignment. That is what makes
layers stackable: a pixel at (row, col) on the terrain grid refers to exactly the
same ground as the pixel at (row, col) on any other terrain-grid layer.

The runtime bake uses one terrain grid (5 m default).

``terrain``
    Built from the 1 m BC LiDAR. Slope, curvature, gullies and ridges are the
    inputs that actually decide where an avalanche starts, and they are exactly
    the quantities destroyed by coarse pixels. This grid must never be derived
    from the 30 m DEM where LiDAR exists.

"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.settings import Settings
from app.processing.mountain_pack import load_mountain_pack

try:  # pragma: no cover - exercised implicitly wherever rasterio is installed
    from rasterio.coords import BoundingBox
    from rasterio.crs import CRS
    from rasterio.transform import Affine, from_origin
except Exception:  # pragma: no cover
    pass

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


def aoi_extent(settings: Settings) -> tuple[float, float, float, float]:
    """Read the declared extent from the validated mountain pack.

    There is intentionally no Mount Hosmer fallback. A missing or malformed
    pack must stop the bake rather than quietly select a different mountain.
    """
    pack, _ = load_mountain_pack(settings)
    return pack.grid.bounds


def _grid(settings: Settings, name: str, resolution: float) -> AnalysisGrid:
    west, south, east, north = aoi_extent(settings)
    pack, _ = load_mountain_pack(settings)
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
        crs_string=pack.grid.analysis_crs,
    )


def terrain_grid(settings: Settings) -> AnalysisGrid:
    pack, _ = load_mountain_pack(settings)
    return _grid(settings, "terrain", pack.grid.resolution_m)
