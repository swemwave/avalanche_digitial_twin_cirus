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

from app.assess import assessment_model_identity
from app.bake_identity import BAKE_SCHEMA, bake_sha256, processing_manifest, sha256_file

# A small AOI-like WGS84 box the linear lattice maps the grid onto.
LON0, LON1 = -115.06, -114.96
LAT_BOTTOM, LAT_TOP = 49.58, 49.64


def write_synthetic_baked(
    runtime_root: Path,
    *,
    n: int = 120,
    res: float = 5.0,
    exposure: bool = True,
) -> Path:
    """Write a synthetic ``baked/`` under ``runtime_root``. Returns the baked dir.

    The hill is a cone: uniform 35 deg flanks (squarely in the slab-release band),
    with aspect pointing radially outward, so a directional wind loads one flank.

    With ``exposure`` (the default) the bake also carries the two optional exposure
    layers: a synthetic trunk-road corridor across the southern flank, and unknown
    coverage along the northern edge. Pass ``exposure=False`` to reproduce a bake
    built before exposure existed -- the assessment must still succeed there and
    report the exposure term as unavailable rather than as zero.
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
        "terrain_source": np.ones((n, n), dtype="uint8"),
        "forest_source": np.ones((n, n), dtype="uint8"),
    }

    exposure_meta = None
    if exposure:
        # A trunk-road corridor across the southern flank, and an unknown strip along
        # the northern edge. Zero inside the known area is a MEASUREMENT ("the extract
        # covers this and maps nothing"); the northern strip is genuinely unknown.
        corridor = slice(int(n * 0.84), int(n * 0.87))
        unknown = slice(0, 3)
        weight = np.zeros((n, n), dtype="float32")
        codes = np.zeros((n, n), dtype="uint8")
        weight[corridor, :] = 0.9
        codes[corridor, :] = 2
        weight[unknown, :] = np.nan
        codes[unknown, :] = 255
        layers["exposure_weight"] = weight
        layers["exposure_class"] = codes
        exposure_meta = _synthetic_exposure_meta(baked, n=n, res=res)

    nodata_by_layer = {"terrain_source": 0, "forest_source": 0, "exposure_class": 255}
    layer_records = []
    for name, array in layers.items():
        path = baked / "layers" / f"{name}.npy"
        np.save(path, array)
        nodata = nodata_by_layer.get(name, "NaN")
        valid = (
            int(np.count_nonzero(np.isfinite(array)))
            if nodata == "NaN"
            else int(np.count_nonzero(array != nodata))
        )
        layer_records.append(
            {
                "name": name,
                "file": f"layers/{name}.npy",
                "dtype": str(array.dtype),
                "shape": list(array.shape),
                "units": "categorical code" if nodata != "NaN" else "synthetic test units",
                "nodata": nodata,
                "valid_pixels": valid,
                "valid_fraction": round(valid / array.size, 4),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

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
        "schema": BAKE_SCHEMA,
        "disclaimer": "Experimental, non-operational research tool. NOT an avalanche forecast.",
        "grid": {
            "crs": "EPSG:26911",
            "coordinate_order": "easting,northing",
            "resolution_m": res,
            "west": 640000.0,
            "south": 5490000.0,
            "east": 640000.0 + n * res,
            "north": 5490000.0 + n * res,
            "width": n,
            "height": n,
            "transform": [res, 0.0, 640000.0, 0.0, -res, 5490000.0 + n * res],
            "transform_semantics": (
                "Affine (a,b,c,d,e,f) maps pixel-edge (col,row) to projected "
                "(easting,northing); array values represent cell centres."
            ),
            "vertical_datum": {"status": "unknown", "name": None},
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
        "layers": layer_records,
        "exposure": exposure_meta,
        "sources": {"download_manifest_sha256": None, "files": [], "sha256": "synthetic"},
        "processing": processing_manifest(Path(__file__).parents[1]),
        "model": assessment_model_identity(),
        "terrain": {
            "lidar_fraction": 1.0,
            "valid_fraction": 1.0,
            "effective_source_resolution_m": 1.0,
            "coverage_by_source_label": {"Synthetic LiDAR": 1.0},
            "source_codes": {"1": "Synthetic LiDAR"},
        },
        "forest": {
            "coverage_by_source_label": {"Synthetic forest source": 1.0},
            "source_codes": {"1": "Synthetic forest source"},
        },
        "reproject": {
            "cols": [round(float(c), 3) for c in cols],
            "rows": [round(float(r), 3) for r in rows],
            "lon": [[round(float(v), 8) for v in line] for line in lon],
            "lat": [[round(float(v), 8) for v in line] for line in lat],
        },
        "warnings": [],
    }
    meta["identity"] = {"bake_sha256": bake_sha256(meta)}
    (baked / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return baked


def _synthetic_exposure_meta(baked: Path, *, n: int, res: float) -> dict:
    """Write the display vector and return the ``meta.json`` exposure block.

    Mirrors the shape ``app.processing.exposure`` produces, so runtime code that
    reads class codes, labels, weights, licence and attribution is exercised here
    exactly as it will be against the real bake.
    """
    features = {
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap contributors",
        "licence": "Open Database License (ODbL) 1.0",
        "derived_classes": ["inferred_settlement"],
        "limitation": "Synthetic exposure fixture; not a survey and not real data.",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[LON0, LAT_BOTTOM + 0.008], [LON1, LAT_BOTTOM + 0.008]],
                },
                "properties": {
                    "name": "Synthetic trunk corridor",
                    "exposure_class": "highway_major",
                    "exposure_label": "Trunk highway",
                    "exposure_weight": 0.9,
                    "derived": False,
                },
            }
        ],
    }
    path = baked / "exposure" / "features.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(features, separators=(",", ":")), encoding="utf-8")
    return {
        "source": {
            "provider": "Synthetic test fixture",
            "citation": "Synthetic exposure corridor",
            "licence": "Open Database License (ODbL) 1.0",
        },
        "licence": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors",
        "analysis_crs": "EPSG:26911",
        "classes": [
            {
                "name": "highway_major",
                "label": "Trunk highway",
                "code": 2,
                "weight": 0.9,
                "buffer_m": 15.0,
                "tags": "highway=trunk, highway=trunk_link",
                "derived": False,
                "feature_count": 1,
                "grid_cell_count": int(n * (int(n * 0.87) - int(n * 0.84))),
            }
        ],
        "class_weight_by_name": {"highway_major": 0.9},
        "class_label_by_code": {"2": "Trunk highway"},
        "settlement_derivation": {
            "rule": "No settlement clusters in the synthetic fixture.",
            "accepted_cluster_count": 0,
            "is_survey_of_structures": False,
        },
        "features_path": "exposure/features.geojson",
        "features_sha256": sha256_file(path),
        "feature_count": 1,
        "layers": ["exposure_weight", "exposure_class"],
        "used_in_release_model": False,
        "used_in_runout_model": False,
        "role": "consequence_term_of_composite_hazard_index_only",
        "is_calibrated": False,
        "limitation": "Synthetic exposure fixture; not a survey and not real data.",
        "cell_resolution_m": res,
    }
