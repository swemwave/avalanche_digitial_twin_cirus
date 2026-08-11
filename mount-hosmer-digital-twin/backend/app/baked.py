"""Runtime access to the Stage 3 baked terrain -- with NO geospatial dependency.

``bake.py`` did all the rasterio/pyproj work once, offline. This module is what
the running service uses instead: it loads the ``.npy`` terrain layers with plain
numpy, reads ``meta.json``, and turns the baked control lattice into a callable
that maps grid pixels ``(col, row)`` to WGS84 ``(lon, lat)`` -- the one thing the
runtime would otherwise need pyproj for.

Nothing here imports rasterio or pyproj. It imports scipy only for the bilinear
interpolation of the reprojection lattice, and scipy is a runtime dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from app.bake_identity import BakeCompatibilityError, processing_manifest, validate_bake
from app.core.settings import Settings


class BakeNotFoundError(RuntimeError):
    """Raised when the baked terrain is missing. Run ``python -m app.bake``."""


class BakeIncompatibleError(RuntimeError):
    """Raised when baked artifacts do not match the current scientific contract."""


@dataclass(frozen=True)
class BakedGrid:
    """The analysis grid, reconstructed from meta.json. Pure numeric; no CRS libs.

    Deliberately duck-type-compatible with ``AnalysisGrid`` for the two members the
    runout engine reads: :pyattr:`shape` and :pyattr:`resolution_m`.
    """

    resolution_m: float
    west: float
    south: float
    east: float
    north: float
    crs_string: str
    width: int
    height: int
    name: str = "terrain"

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)


class Reprojector:
    """Maps grid pixel ``(col, row)`` -> WGS84 ``(lon, lat)`` via the baked lattice.

    The lattice is a coarse (21x21) grid of control points computed with pyproj at
    bake time. UTM 11N -> WGS84 is smooth, so bilinear interpolation between the
    control points reproduces it to well under a centimetre over this 12 km AOI --
    exact for any purpose this app has, and with zero runtime dependency on pyproj.
    """

    def __init__(self, cols: np.ndarray, rows: np.ndarray, lon: np.ndarray, lat: np.ndarray) -> None:
        from scipy.interpolate import RegularGridInterpolator

        # RegularGridInterpolator wants strictly ascending axes: (row, col).
        self._lon = RegularGridInterpolator(
            (rows, cols), lon, bounds_error=False, fill_value=None
        )
        self._lat = RegularGridInterpolator(
            (rows, cols), lat, bounds_error=False, fill_value=None
        )

    def __call__(self, col: Any, row: Any) -> tuple[Any, Any]:
        """Vectorized. ``col``/``row`` may be scalars or arrays (shapely passes arrays)."""
        col_arr = np.asarray(col, dtype="float64")
        row_arr = np.asarray(row, dtype="float64")
        points = np.column_stack([row_arr.ravel(), col_arr.ravel()])
        lon = self._lon(points).reshape(col_arr.shape)
        lat = self._lat(points).reshape(col_arr.shape)
        if np.ndim(col) == 0:
            return float(lon), float(lat)
        return lon, lat


@dataclass
class BakedTerrain:
    """Everything the runtime needs from the bake, loaded and ready."""

    grid: BakedGrid
    reproject: Reprojector
    meta: dict[str, Any]
    root: Path

    _cache: dict[str, np.ma.MaskedArray]

    def layer(self, name: str) -> np.ma.MaskedArray:
        """A baked terrain layer as a masked array (NaN -> masked; invariant I3)."""
        if name not in self._cache:
            path = self.root / "layers" / f"{name}.npy"
            if not path.exists():
                available = [record["name"] for record in self.meta.get("layers", [])]
                raise KeyError(f"Baked layer {name!r} not found. Available: {available}")
            values = np.load(path, mmap_mode="r")
            record = next(
                (item for item in self.meta.get("layers", []) if item.get("name") == name), {}
            )
            nodata = record.get("nodata", "NaN")
            if nodata == "NaN":
                layer = np.ma.masked_invalid(values, copy=False)
            else:
                layer = np.ma.masked_equal(values, nodata, copy=False)
            self._cache[name] = layer
        return self._cache[name]

    @property
    def disclaimer(self) -> str:
        return str(self.meta.get("disclaimer", ""))

    @property
    def bake_sha256(self) -> str:
        return str(self.meta.get("identity", {}).get("bake_sha256", ""))


def baked_root(settings: Settings) -> Path:
    return settings.runtime_root / "baked"


@lru_cache(maxsize=2)
def _load(root_str: str, mtime: float, expected_processing_sha256: str | None) -> BakedTerrain:
    root = Path(root_str)
    try:
        meta = validate_bake(
            root, expected_processing_sha256=expected_processing_sha256
        )
    except BakeCompatibilityError as exc:
        raise BakeIncompatibleError(
            f"The terrain bake is stale, incomplete, or corrupted: {exc} "
            "Rebuild it with: python -m app.bake --force"
        ) from exc
    g = meta["grid"]
    grid = BakedGrid(
        resolution_m=float(g["resolution_m"]),
        west=float(g["west"]),
        south=float(g["south"]),
        east=float(g["east"]),
        north=float(g["north"]),
        crs_string=str(g["crs"]),
        width=int(g["width"]),
        height=int(g["height"]),
    )
    lattice = meta["reproject"]
    reproject = Reprojector(
        cols=np.asarray(lattice["cols"], dtype="float64"),
        rows=np.asarray(lattice["rows"], dtype="float64"),
        lon=np.asarray(lattice["lon"], dtype="float64"),
        lat=np.asarray(lattice["lat"], dtype="float64"),
    )
    return BakedTerrain(grid=grid, reproject=reproject, meta=meta, root=root, _cache={})


def load_baked(settings: Settings) -> BakedTerrain:
    """Load the baked terrain, cached against meta.json's mtime."""
    root = baked_root(settings)
    meta_path = root / "meta.json"
    if not meta_path.exists():
        raise BakeNotFoundError(
            f"No baked terrain at {root}. Build it once with: python -m app.bake"
        )
    try:
        expected_processing = processing_manifest(settings.project_root)["sha256"]
    except FileNotFoundError:
        # Hermetic callers may supply a synthetic runtime without a source checkout.
        # The bake's own schema, identity, and layer checksums are still validated.
        expected_processing = None
    return _load(str(root), meta_path.stat().st_mtime, expected_processing)


def tile_path(settings: Settings, z: int, x: int, y: int) -> Path:
    """Filesystem path of a baked terrain-RGB tile. Static PNG; no rasterio to serve."""
    return baked_root(settings) / "tiles" / str(z) / str(x) / f"{y}.png"


def imagery_tile_path(settings: Settings, z: int, x: int, y: int) -> Path:
    """Filesystem path of a baked natural-colour tile. Static PNG; no source-data read."""
    return baked_root(settings) / "imagery" / str(z) / str(x) / f"{y}.png"


def exposure_features_path(settings: Settings) -> Path:
    """Filesystem path of the baked exposure vectors. Static GeoJSON; nothing computed."""
    return baked_root(settings) / "exposure" / "features.geojson"
