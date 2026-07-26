r"""The one-time, offline terrain bake for Stage 3 ("Ultra").

Everything Stage 3 needs from the 46 GB source tree is time-invariant terrain, so
it is computed **once**, here, and never again at runtime. After this script runs,
a running Stage-3 service needs nothing from ``DATA\`` and no geospatial library:
it loads the ``.npy`` layers with plain numpy and serves the static PNG tiles.
That is the whole point of the bake -- it takes runtime's ``DATA\`` dependency,
and its dependency on rasterio/pyproj/GDAL, to zero.

Inputs -- the allow-list from ``docs/data-footprint.md`` and *nothing else*:

    metadata/grid_and_aoi.json            analysis grid + CRS + fixed extent
    metadata/mount_hosmer_aoi.geojson     AOI polygon
    static/lidar_bc/.../*.tif             1 m BC LiDAR DEM + DSM tiles
    static/terrain_fallback/*.tif         Copernicus GLO-30 gap-fill
    static/landcover/*.tif                ESA WorldCover forest mask
    events/MH_20260116T183016Z/sentinel2/*_{B02,B03,B04}_*.tif
                                            fixed winter true-colour context

Every ``.laz`` point cloud, every dynamic file, and every event except the one
fixed Sentinel-2 scene named below is off-limits. The optical scene is used only
to bake a visual true-colour surface; no satellite value enters the risk model.

Outputs -- all under ``runtime/baked/`` (invariant I1: never ``DATA\``):

    runtime/baked/tiles/{z}/{x}/{y}.png   terrain-RGB tiles for the 3D MapLibre mesh
    runtime/baked/imagery/{z}/{x}/{y}.png true-colour Sentinel-2 surface tiles
    runtime/baked/layers/*.npy            slope, aspect, curvature, elevation, forest_mask, ...
    runtime/baked/meta.json               grid/AOI/tile metadata + a grid->WGS84 lattice
    runtime/baked/elevation.tif           bake-only COG the tiles are rendered from

Run it with::

    python -m app.bake            # from the app root (or backend/)
    python -m app.bake --force    # ignore an existing bake and rebuild

This module imports rasterio, pyproj and PIL. That is deliberate and permitted:
``bake.py`` is a **bake-time** tool. The runtime service must never import it.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.core.model_config import DISCLAIMER, load_model_config
from app.core.settings import Settings, get_settings
from app.processing.terrain import engine as terrain_engine
from app.processing.harmonization.grids import AnalysisGrid

#: Terrain layers written to ``baked/layers/*.npy``. This is a deliberate superset
#: of what the current risk + runout code strictly needs, so tuning the risk model
#: does not force a re-bake:
#:
#:   elevation           runout physics; release-zone elevation banding
#:   slope               risk slope-band term; starting-zone terrain
#:   aspect              risk aspect-vs-wind loading term
#:   plan_curvature      runout gully friction; convergence
#:   general_curvature   risk convex-curvature term (slab tension)
#:   forest_mask         risk forest damping; runout forest friction
#:   distance_to_ridge   lee-loading proxy (wind loads slopes below ridges)
#:
#: Each is stored as float32 with NaN in masked cells; the runtime rebuilds the
#: masked array with ``np.ma.masked_invalid``. Missing stays missing (invariant I3).
BAKED_LAYERS = (
    "elevation",
    "slope",
    "aspect",
    "plan_curvature",
    "general_curvature",
    "forest_mask",
    "distance_to_ridge",
)

#: Zoom range baked for the 3D mesh. The 5 m terrain grid does not hold detail
#: finer than ~z15 (a z15 tile is ~3 m/px here), so baking z16 would only inflate
#: the tile count to serve interpolated pixels. MapLibre over-zooms the z15 tiles
#: above this, which is exactly the right behaviour for a 5 m source.
MIN_ZOOM = 8
MAX_ZOOM = 15

#: Size of the grid->WGS84 control lattice stored in meta.json. The runtime maps
#: pixel (col, row) -> (lon, lat) by bilinear interpolation over this lattice,
#: which replaces pyproj at runtime. UTM 11N -> WGS84 is smooth and conformal, so
#: over a 12 km AOI a 21x21 lattice (600 m node spacing) is accurate to well under
#: a centimetre -- far below anything that matters for a research visualisation.
LATTICE_NODES = 21

# A fixed winter capture gives the natural surface a useful snow/forest view while
# keeping the runtime deterministic and completely offline. These are Sentinel-2
# L2A surface-reflectance bands at 10 m: blue, green, red become RGB in that order.
SATELLITE_EVENT_ID = "MH_20260116T183016Z"
SATELLITE_CAPTURED_AT = "2026-01-15T18:46:29.024000+00:00"
SATELLITE_CLOUD_PERCENT = 6.523087


def baked_root(settings: Settings) -> Path:
    return settings.runtime_root / "baked"


# --- Layers -------------------------------------------------------------------


def _save_layers(products: terrain_engine.TerrainProducts, out_dir: Path) -> list[dict[str, Any]]:
    """Dump the baked terrain layers to ``.npy`` and return their descriptors."""
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for name in BAKED_LAYERS:
        if name not in products:
            raise KeyError(
                f"The terrain engine did not produce layer {name!r}; the bake cannot continue. "
                f"Available: {sorted(products.layers)}"
            )
        array = products[name]
        # NaN carries "no data" through a plain numpy load. Never zero (invariant I3).
        filled = np.ma.asarray(array).astype("float32").filled(np.nan)
        np.save(out_dir / f"{name}.npy", filled)
        valid = int(np.isfinite(filled).sum())
        records.append(
            {
                "name": name,
                "file": f"layers/{name}.npy",
                "dtype": "float32",
                "shape": list(filled.shape),
                "valid_pixels": valid,
                "valid_fraction": round(valid / filled.size, 4),
            }
        )
    return records


# --- Tiles --------------------------------------------------------------------


def _mercator_bounds(grid: AnalysisGrid) -> tuple[float, float, float, float]:
    """The AOI grid bounds, reprojected to Web Mercator (EPSG:3857)."""
    from rasterio.warp import transform_bounds

    return transform_bounds(
        grid.crs, "EPSG:3857", grid.west, grid.south, grid.east, grid.north, densify_pts=21
    )


def _tile_range(z: int, merc: tuple[float, float, float, float]) -> tuple[range, range]:
    """XYZ tile indices covering ``merc`` (west, south, east, north) at zoom ``z``."""
    from app.services.tiles import ORIGIN

    span = (2.0 * ORIGIN) / (2**z)
    west, south, east, north = merc
    x_min = int((west + ORIGIN) // span)
    x_max = int((east + ORIGIN) // span)
    # Web Mercator Y counts down from the north, so north maps to the smaller index.
    y_min = int((ORIGIN - north) // span)
    y_max = int((ORIGIN - south) // span)
    limit = 2**z
    xs = range(max(0, x_min), min(limit - 1, x_max) + 1)
    ys = range(max(0, y_min), min(limit - 1, y_max) + 1)
    return xs, ys


def _bake_tiles(elevation_cog: Path, out_dir: Path, grid: AnalysisGrid) -> int:
    """Pre-render every terrain-RGB tile the mesh needs into ``out_dir``.

    Reuses the runtime tile renderer's rasterio-backed internals -- this is the
    bake, where rasterio is allowed. The output is plain PNGs the runtime serves
    statically, so the runtime tile route needs no rasterio at all.
    """
    from app.services import tiles as tile_mod

    merc = _mercator_bounds(grid)
    floor = tile_mod._floor_elevation_cached(str(elevation_cog), elevation_cog.stat().st_mtime)

    written = 0
    for z in range(MIN_ZOOM, MAX_ZOOM + 1):
        xs, ys = _tile_range(z, merc)
        for x in xs:
            for y in ys:
                data = tile_mod._read_tile(elevation_cog, z, x, y)
                if data is None:
                    # This tile's frame does not touch the AOI's data. The mesh
                    # never asks for it (the source is clipped to the AOI bounds),
                    # so there is nothing to render.
                    continue
                png = tile_mod._encode_terrain_rgb(data, floor)
                path = out_dir / str(z) / str(x) / f"{y}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(png)
                written += 1
    return written


def _sentinel_rgb_sources(settings: Settings) -> tuple[Path, Path, Path]:
    """Return red, green and blue rasters for the fixed baked winter scene."""
    scene = settings.data_root / "events" / SATELLITE_EVENT_ID / "sentinel2"
    paths: list[Path] = []
    for band in ("B04", "B03", "B02"):
        matches = sorted(scene.glob(f"*_{band}_EPSG26911_10m.tif"))
        if len(matches) != 1:
            raise FileNotFoundError(
                f"Expected one Sentinel-2 {band} raster in {scene}, found {len(matches)}."
            )
        paths.append(matches[0])
    return paths[0], paths[1], paths[2]


def _imagery_stretch(sources: tuple[Path, Path, Path]) -> list[tuple[float, float]]:
    """Find robust per-band display limits without letting a few bright pixels dominate."""
    import rasterio

    limits: list[tuple[float, float]] = []
    for source in sources:
        with rasterio.open(source) as dataset:
            sample = dataset.read(
                1,
                out_shape=(max(1, dataset.height // 8), max(1, dataset.width // 8)),
                masked=True,
            )
        values = sample.compressed()
        if not values.size:
            raise ValueError(f"Sentinel-2 band has no valid pixels: {source}")
        low, high = np.percentile(values, (2.0, 98.0))
        limits.append((float(low), float(high)))
    return limits


def _bake_imagery_tiles(
    sources: tuple[Path, Path, Path], out_dir: Path, grid: AnalysisGrid
) -> tuple[int, list[tuple[float, float]]]:
    """Bake a natural-colour RGBA tile pyramid for draping over the 3D mesh."""
    import io

    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import reproject

    from app.services.tiles import TILE_SIZE, WEB_MERCATOR, tile_bounds

    stretch = _imagery_stretch(sources)
    merc = _mercator_bounds(grid)
    written = 0

    with rasterio.open(sources[0]) as red, rasterio.open(sources[1]) as green, rasterio.open(
        sources[2]
    ) as blue:
        datasets = (red, green, blue)
        for z in range(MIN_ZOOM, MAX_ZOOM + 1):
            xs, ys = _tile_range(z, merc)
            for x in xs:
                for y in ys:
                    left, bottom, right, top = tile_bounds(z, x, y)
                    warped = np.full((3, TILE_SIZE, TILE_SIZE), np.nan, dtype="float32")
                    for index, dataset in enumerate(datasets):
                        reproject(
                            source=rasterio.band(dataset, 1),
                            destination=warped[index],
                            src_transform=dataset.transform,
                            src_crs=dataset.crs,
                            src_nodata=dataset.nodata,
                            dst_transform=transform_from_bounds(
                                left, bottom, right, top, TILE_SIZE, TILE_SIZE
                            ),
                            dst_crs=WEB_MERCATOR,
                            dst_nodata=np.nan,
                            resampling=Resampling.bilinear,
                        )

                    valid = np.all(np.isfinite(warped), axis=0)
                    if not np.any(valid):
                        continue
                    rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype="uint8")
                    for index, (low, high) in enumerate(stretch):
                        normalized = np.clip((warped[index] - low) / (high - low), 0.0, 1.0)
                        # A light display gamma opens dark forest detail while snow stays white.
                        rgba[..., index] = np.nan_to_num(normalized ** (1.0 / 1.15) * 255).astype(
                            "uint8"
                        )
                    rgba[..., 3] = valid.astype("uint8") * 255

                    buffer = io.BytesIO()
                    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
                    path = out_dir / str(z) / str(x) / f"{y}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(buffer.getvalue())
                    written += 1
    return written, stretch


# --- Grid -> WGS84 reprojection lattice ---------------------------------------


def _reproject_lattice(grid: AnalysisGrid) -> dict[str, Any]:
    """A control lattice mapping pixel (col, row) -> (lon, lat), for the runtime.

    The runtime has no pyproj, so it cannot reproject the analysis grid to the
    WGS84 the map draws in. We precompute the transform here as a lattice of
    control points and let the runtime bilinear-interpolate it. See LATTICE_NODES
    for why that is accurate enough to be exact for our purposes.
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs(grid.crs_string, "EPSG:4326", always_xy=True)
    transform = grid.transform  # (col, row) -> (easting, northing)

    height, width = grid.shape
    cols = np.linspace(0.0, float(width), LATTICE_NODES)
    rows = np.linspace(0.0, float(height), LATTICE_NODES)

    lon = np.zeros((LATTICE_NODES, LATTICE_NODES), dtype="float64")
    lat = np.zeros((LATTICE_NODES, LATTICE_NODES), dtype="float64")
    for i, row in enumerate(rows):
        for j, col in enumerate(cols):
            easting, northing = transform * (col, row)
            longitude, latitude = transformer.transform(easting, northing)
            lon[i, j] = longitude
            lat[i, j] = latitude

    return {
        "note": (
            "Bilinear control lattice for pixel (col, row) -> (lon, lat). cols/rows are "
            "pixel positions along each axis; lon[i][j]/lat[i][j] correspond to (rows[i], cols[j]). "
            "Replaces pyproj at runtime."
        ),
        "cols": [round(float(c), 3) for c in cols],
        "rows": [round(float(r), 3) for r in rows],
        "lon": [[round(float(v), 8) for v in line] for line in lon],
        "lat": [[round(float(v), 8) for v in line] for line in lat],
    }


def _bounds_wgs84(grid: AnalysisGrid) -> list[float]:
    from rasterio.warp import transform_bounds

    west, south, east, north = transform_bounds(
        grid.crs, "EPSG:4326", grid.west, grid.south, grid.east, grid.north, densify_pts=21
    )
    return [west, south, east, north]


# --- Orchestration ------------------------------------------------------------


def bake(settings: Settings | None = None, *, force: bool = False) -> dict[str, Any]:
    """Run the full bake. Returns the metadata written to ``baked/meta.json``."""
    from app.processing.harmonization.raster_io import write_raster

    settings = settings or get_settings()
    started = time.perf_counter()
    out = baked_root(settings)
    meta_path = out / "meta.json"

    if meta_path.exists() and not force:
        existing = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"[bake] already baked at {out} (schema {existing.get('schema')}). Use --force to rebuild.")
        return existing

    out.mkdir(parents=True, exist_ok=True)
    print(f"[bake] output -> {out}")

    config = load_model_config(settings)
    print("[bake] computing terrain products from the LiDAR mosaic (this is the slow part)...")
    products, terrain_meta = terrain_engine.compute(settings, config)
    grid = products.grid
    assert grid is not None, "terrain engine did not set a grid"
    print(f"[bake] grid {grid.name}: {grid.shape} @ {grid.resolution_m:g} m, CRS {grid.crs_string}")

    # 1. Elevation COG -- the source the tiles are rendered from. Bake-only.
    elevation_cog = out / "elevation.tif"
    print("[bake] writing elevation COG for tile rendering...")
    write_raster(elevation_cog, products["elevation"], grid, build_overviews=True)

    # 2. Terrain-RGB tiles for the 3D mesh.
    print(f"[bake] rendering terrain-RGB tiles z{MIN_ZOOM}-z{MAX_ZOOM}...")
    tiles_dir = out / "tiles"
    tile_count = _bake_tiles(elevation_cog, tiles_dir, grid)
    print(f"[bake] wrote {tile_count} tiles")

    # 3. Fixed true-colour surface imagery. It is visual context only and never
    #    enters the risk model.
    print(f"[bake] rendering winter Sentinel-2 RGB tiles z{MIN_ZOOM}-z{MAX_ZOOM}...")
    imagery_sources = _sentinel_rgb_sources(settings)
    imagery_count, imagery_stretch = _bake_imagery_tiles(
        imagery_sources, out / "imagery", grid
    )
    print(f"[bake] wrote {imagery_count} imagery tiles")

    # 4. The .npy terrain layers the runtime loads with plain numpy.
    print(f"[bake] saving {len(BAKED_LAYERS)} layers to layers/*.npy...")
    layer_records = _save_layers(products, out / "layers")

    # 5. Metadata: everything the runtime and the map need, plus the reprojection
    #    lattice that replaces pyproj at runtime.
    print("[bake] building grid->WGS84 lattice and writing meta.json...")
    bbox = _bounds_wgs84(grid)
    terrain_model = terrain_meta.get("terrain_model", {})
    meta = {
        "schema": "stage3-baked-v1",
        "generated_at_utc": _utc_now_iso(),
        "duration_seconds": round(time.perf_counter() - started, 1),
        "disclaimer": DISCLAIMER,
        "grid": {
            "crs": grid.crs_string,
            "resolution_m": grid.resolution_m,
            "west": grid.west,
            "south": grid.south,
            "east": grid.east,
            "north": grid.north,
            "width": grid.width,
            "height": grid.height,
            "transform": list(grid.transform)[:6],
        },
        "aoi_bbox_wgs84": bbox,
        "aoi_corners_wgs84": [
            [bbox[0], bbox[3]],
            [bbox[2], bbox[3]],
            [bbox[2], bbox[1]],
            [bbox[0], bbox[1]],
        ],
        "center_wgs84": [round((bbox[0] + bbox[2]) / 2, 6), round((bbox[1] + bbox[3]) / 2, 6)],
        "tiles": {
            "path": "tiles/{z}/{x}/{y}.png",
            "tile_size": 256,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "encoding": "mapbox",
            "count": tile_count,
        },
        "imagery": {
            "path": "imagery/{z}/{x}/{y}.png",
            "tile_size": 256,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "count": imagery_count,
            "kind": "Sentinel-2 L2A natural colour",
            "event_id": SATELLITE_EVENT_ID,
            "captured_at_utc": SATELLITE_CAPTURED_AT,
            "cloud_percent": SATELLITE_CLOUD_PERCENT,
            "source_resolution_m": 10,
            "display_stretch_2_98": [
                {"band": band, "low": round(low, 3), "high": round(high, 3)}
                for band, (low, high) in zip(("red", "green", "blue"), imagery_stretch)
            ],
            "visual_context_only": True,
        },
        "layers": layer_records,
        "terrain": {
            "lidar_fraction": terrain_model.get("lidar_fraction"),
            "valid_fraction": terrain_model.get("valid_fraction"),
            "effective_source_resolution_m": terrain_model.get("effective_source_resolution_m"),
            "coverage_by_source_label": terrain_model.get("coverage_by_source_label"),
        },
        "reproject": _reproject_lattice(grid),
        "warnings": sorted(set(products.warnings)),
        "is_operational_forecast": False,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(
        f"[bake] done in {meta['duration_seconds']}s: "
        f"{tile_count} tiles, {len(layer_records)} layers, "
        f"lidar_fraction={meta['terrain']['lidar_fraction']}"
    )
    if meta["warnings"]:
        print(f"[bake] {len(meta['warnings'])} warning(s):")
        for warning in meta["warnings"]:
            print(f"       - {warning}")
    return meta


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bake Stage 3 terrain artifacts (offline, one-time).")
    parser.add_argument("--force", action="store_true", help="Rebuild even if a bake already exists.")
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.validate(require_data_root=True)
    bake(settings, force=args.force)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
