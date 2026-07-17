"""app.geo: rasterio-free mask->GeoJSON and path->LineString."""

from __future__ import annotations

import numpy as np

from app import geo


def linear_reproject(col, row):
    """A trivial (col,row)->(lon,lat) map, vectorized like the real Reprojector."""
    col = np.asarray(col, dtype="float64")
    row = np.asarray(row, dtype="float64")
    lon = -115.0 + col * 0.001
    lat = 49.6 - row * 0.001
    if np.ndim(col) == 0:
        return float(lon), float(lat)
    return lon, lat


def _all_points(geometry):
    out = []

    def rec(node):
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            out.append(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                rec(item)

    rec(geometry["coordinates"])
    return out


def test_mask_to_geojson_solid_block():
    mask = np.zeros((40, 40), dtype=bool)
    mask[10:30, 12:24] = True
    gj = geo.mask_to_geojson(mask, linear_reproject, simplify_px=0)
    assert gj is not None
    assert gj["type"] in ("Polygon", "MultiPolygon")
    points = _all_points(gj)
    lons = [p[0] for p in points]
    # Block spans cols 12..24 -> lon -115 + [0.012 .. 0.024]
    assert min(lons) >= -115.0 and max(lons) <= -114.97


def test_mask_to_geojson_preserves_hole():
    mask = np.zeros((40, 40), dtype=bool)
    mask[8:32, 8:32] = True
    mask[14:26, 14:26] = False  # a hole
    gj = geo.mask_to_geojson(mask, linear_reproject, simplify_px=0)
    assert gj is not None
    rings = gj["coordinates"] if gj["type"] == "Polygon" else gj["coordinates"][0]
    assert len(rings) >= 2, "the interior hole should survive as a second ring"


def test_mask_empty_is_none():
    assert geo.mask_to_geojson(np.zeros((5, 5), dtype=bool), linear_reproject) is None


def test_min_pixels_filter():
    mask = np.zeros((20, 20), dtype=bool)
    mask[0:2, 0:2] = True  # 4 pixels
    assert geo.mask_to_geojson(mask, linear_reproject, min_pixels=10) is None


def test_path_to_geojson():
    path = [(0, 0), (1, 2), (3, 4), (6, 6)]
    gj = geo.path_to_geojson(path, linear_reproject)
    assert gj is not None and gj["type"] == "LineString"
    assert len(gj["coordinates"]) >= 2


def test_path_too_short_is_none():
    assert geo.path_to_geojson([(0, 0)], linear_reproject) is None
