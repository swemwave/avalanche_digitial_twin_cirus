"""Rasterio- and pyproj-free geometry, for the Stage 3 runtime.

The legacy pipeline turned boolean masks into WGS84 GeoJSON with
``rasterio.features.shapes`` + ``rasterio.warp.transform_geom``. Stage 3 drops
rasterio and pyproj from the runtime, so this module does the same job with only
numpy, shapely and the baked reprojection lattice (see :mod:`app.baked`).

Two conversions the map needs:

* :func:`mask_to_geojson` -- a boolean footprint (release zone, runout, envelope)
  to a WGS84 GeoJSON Polygon/MultiPolygon.
* :func:`path_to_geojson` -- a ``(row, col)`` centre-line to a WGS84 LineString.

**Why run-length boxes rather than a marching-squares tracer.** A footprint is
built as the union of unit pixel squares. Encoding each row's True pixels as a few
contiguous spans gives a few hundred axis-aligned boxes instead of thousands of
single-pixel polygons, and shapely's ``unary_union`` merges them -- holes and all --
correctly and fast. The union is done in grid ``(col, row)`` space, then every
vertex is reprojected to ``(lon, lat)``; because the grid->lon/lat map is smooth
and monotonic, that preserves the topology of the polygon exactly.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import shapely
from shapely.geometry import LineString, mapping
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

#: A callable ``(col, row) -> (lon, lat)``; :class:`app.baked.Reprojector` satisfies it.
Reproject = Callable[[Any, Any], tuple[Any, Any]]


def _mask_geometry(mask: np.ndarray):
    """Union of pixel squares for ``mask``, in grid ``(x=col, y=row)`` space.

    Each True pixel is a unit square; encoding each row's True pixels as a few
    contiguous column spans gives a few hundred axis-aligned boxes instead of
    thousands of single-pixel polygons, and ``unary_union`` merges them (holes and
    all) correctly. The spans are found in one vectorized pass over the whole mask,
    and the boxes are built with shapely's vectorized ``box`` -- both far cheaper
    than the old per-row Python loop that allocated one geometry object per span.
    """
    # Crop to the mask's bounding box first. A runout footprint or release zone is
    # a small patch of a 2400x2400 grid, so scanning the whole grid for every mask
    # is wasteful -- the span search runs on the cropped sub-array instead.
    row_any = mask.any(axis=1)
    if not row_any.any():
        return None
    col_any = mask.any(axis=0)
    rr = np.flatnonzero(row_any)
    cc = np.flatnonzero(col_any)
    r0, c0 = int(rr[0]), int(cc[0])
    sub = mask[r0 : int(rr[-1]) + 1, c0 : int(cc[-1]) + 1]

    # Pad a False column on each side so a run touching an edge still shows both a
    # rising (+1) and falling (-1) transition. np.diff along the columns marks span
    # boundaries; np.nonzero returns them row-major, so boxes come out row-ascending
    # then column-ascending -- the same order as the old nested loop, which keeps
    # the union input (and therefore its output) identical.
    padded = np.zeros((sub.shape[0], sub.shape[1] + 2), dtype=np.int8)
    padded[:, 1:-1] = sub
    diff = np.diff(padded, axis=1)
    rows, start_cols = np.nonzero(diff == 1)   # box minx = start_col, miny = row
    _, stop_cols = np.nonzero(diff == -1)      # one stop per start, same rows
    if rows.size == 0:
        return None
    # box(minx, miny, maxx, maxy), offset back to full-grid coords; y=row increases
    # downward, which is fine -- the reprojection lattice uses the same convention.
    boxes = shapely.box(start_cols + c0, rows + r0, stop_cols + c0, rows + r0 + 1)
    return unary_union(boxes)


def mask_to_geojson(
    mask: np.ndarray,
    reproject: Reproject,
    *,
    simplify_px: float = 1.0,
    min_pixels: int = 1,
) -> dict[str, Any] | None:
    """A boolean footprint -> WGS84 GeoJSON Polygon/MultiPolygon, or ``None`` if empty.

    ``simplify_px`` is a Douglas-Peucker tolerance in *pixels*, applied in grid space
    before reprojection, so the served geometry is not a staircase of every pixel edge.
    """
    mask = np.asarray(mask, dtype=bool)
    if int(mask.sum()) < max(1, min_pixels):
        return None

    geometry = _mask_geometry(mask)
    if geometry is None or geometry.is_empty:
        return None

    if simplify_px > 0:
        geometry = geometry.simplify(simplify_px, preserve_topology=True)
        if geometry.is_empty:
            return None

    # Reproject every vertex (col, row) -> (lon, lat). shapely hands the transform
    # coordinate arrays, which the reprojector vectorizes.
    def _to_wgs84(x, y, z=None):  # noqa: ANN001 - shapely's transform signature
        lon, lat = reproject(x, y)
        return lon, lat

    geometry = shapely_transform(_to_wgs84, geometry)
    return mapping(geometry)


def path_to_geojson(
    path_rowcol: list[tuple[int, int]],
    reproject: Reproject,
) -> dict[str, Any] | None:
    """A ``(row, col)`` centre-line -> WGS84 GeoJSON LineString, or ``None`` if too short."""
    if not path_rowcol or len(path_rowcol) < 2:
        return None
    # Sample the pixel centres: (col + 0.5, row + 0.5).
    cols = np.array([c + 0.5 for _, c in path_rowcol], dtype="float64")
    rows = np.array([r + 0.5 for r, _ in path_rowcol], dtype="float64")
    lon, lat = reproject(cols, rows)
    line = LineString(np.column_stack([np.asarray(lon), np.asarray(lat)]))
    line = line.simplify(1e-5, preserve_topology=False)
    if line.is_empty or len(line.coords) < 2:
        return None
    return mapping(line)
