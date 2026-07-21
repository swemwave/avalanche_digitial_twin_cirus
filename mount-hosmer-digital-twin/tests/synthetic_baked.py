"""A tiny synthetic baked terrain, so the Stage 3 tests never need the real bake.

It writes exactly what ``app.baked.load_baked`` reads -- ``meta.json`` + ``layers/*.npy``
-- for a small cone-shaped hill, with a linear grid->WGS84 lattice. No rasterio,
no pyproj, no source data: everything here is plain numpy, matching how the runtime
loads the real bake.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# A small AOI-like WGS84 box the linear lattice maps the grid onto.
LON0, LON1 = -115.06, -114.96
LAT_BOTTOM, LAT_TOP = 49.58, 49.64


def write_synthetic_baked(runtime_root: Path, *, n: int = 120, res: float = 5.0) -> Path:
    """Write a synthetic ``baked/`` under ``runtime_root``. Returns the baked dir.

    The hill is a cone: uniform 35 deg flanks (squarely in the slab-release band),
    with aspect pointing radially outward, so a directional wind loads one flank.
    """
    baked = runtime_root / "baked"
    (baked / "layers").mkdir(parents=True, exist_ok=True)

    yy, xx = np.mgrid[0:n, 0:n].astype("float64")
    cy = cx = (n - 1) / 2.0
    drow = yy - cy
    dcol = xx - cx
    dist_m = np.hypot(drow, dcol) * res

    # Cone: slope tan = 0.70 -> ~35 deg everywhere but the very apex.
    elevation = (2500.0 - 0.70 * dist_m).astype("float32")
    slope = np.full((n, n), 35.0, dtype="float32")
    apex = dist_m < res  # the flat cap
    slope[apex] = 0.0

    # Aspect: compass degrees clockwise from north, pointing downslope (outward).
    aspect = (np.degrees(np.arctan2(dcol, -drow)) % 360.0).astype("float32")
    aspect[apex] = -1.0

    zeros = np.zeros((n, n), dtype="float32")
    layers = {
        "elevation": elevation,
        "slope": slope,
        "aspect": aspect,
        "plan_curvature": zeros,
        "general_curvature": zeros,
        "forest_mask": zeros,           # open terrain everywhere
        "distance_to_ridge": (dist_m).astype("float32"),
    }
    for name, array in layers.items():
        np.save(baked / "layers" / f"{name}.npy", array)

    # A linear grid->WGS84 control lattice (5x5). Row 0 is north (LAT_TOP).
    nodes = 5
    cols = np.linspace(0.0, float(n), nodes)
    rows = np.linspace(0.0, float(n), nodes)
    lon = np.zeros((nodes, nodes))
    lat = np.zeros((nodes, nodes))
    for i, r in enumerate(rows):
        for j, c in enumerate(cols):
            lon[i, j] = LON0 + (LON1 - LON0) * (c / n)
            lat[i, j] = LAT_TOP + (LAT_BOTTOM - LAT_TOP) * (r / n)

    meta = {
        "schema": "stage3-baked-v1",
        "disclaimer": "Experimental, non-operational research tool. NOT an avalanche forecast.",
        "grid": {
            "crs": "EPSG:26911",
            "resolution_m": res,
            "west": 640000.0,
            "south": 5490000.0,
            "east": 640000.0 + n * res,
            "north": 5490000.0 + n * res,
            "width": n,
            "height": n,
        },
        "aoi_bbox_wgs84": [LON0, LAT_BOTTOM, LON1, LAT_TOP],
        "aoi_corners_wgs84": [[LON0, LAT_TOP], [LON1, LAT_TOP], [LON1, LAT_BOTTOM], [LON0, LAT_BOTTOM]],
        "center_wgs84": [(LON0 + LON1) / 2, (LAT_BOTTOM + LAT_TOP) / 2],
        "tiles": {"path": "tiles/{z}/{x}/{y}.png", "tile_size": 256, "min_zoom": 8, "max_zoom": 15, "encoding": "mapbox", "count": 0},
        "imagery": {
            "path": "imagery/{z}/{x}/{y}.png",
            "tile_size": 256,
            "min_zoom": 8,
            "max_zoom": 15,
            "count": 0,
            "kind": "Sentinel-2 L2A natural colour",
            "captured_at_utc": "2026-01-15T18:46:29+00:00",
            "cloud_percent": 6.5,
            "source_resolution_m": 10,
            "visual_context_only": True,
        },
        "layers": [{"name": name, "file": f"layers/{name}.npy"} for name in layers],
        "terrain": {"lidar_fraction": 1.0, "valid_fraction": 1.0, "effective_source_resolution_m": 1.0},
        "reproject": {
            "cols": [round(float(c), 3) for c in cols],
            "rows": [round(float(r), 3) for r in rows],
            "lon": [[round(float(v), 8) for v in line] for line in lon],
            "lat": [[round(float(v), 8) for v in line] for line in lat],
        },
        "warnings": [],
    }
    (baked / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return baked
