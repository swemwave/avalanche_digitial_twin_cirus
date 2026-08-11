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
    static/openstreetmap/mount_hosmer_osm_features.geojson
                                            roads, rail and derived built-up outlines

Every ``.laz`` point cloud, every dynamic file, and every event except the one
fixed Sentinel-2 scene named below is off-limits. The optical scene is used only
to bake a visual true-colour surface; no satellite value enters the risk model.
The OpenStreetMap extract is exposure: it never enters the release model and acts
only as the consequence term of the composite hazard index.

Outputs -- all under ``runtime/baked/`` (invariant I1: never ``DATA\``):

    runtime/baked/tiles/{z}/{x}/{y}.png   terrain-RGB tiles for the 3D MapLibre mesh
    runtime/baked/imagery/{z}/{x}/{y}.png true-colour Sentinel-2 surface tiles
    runtime/baked/layers/*.npy            six model layers + two provenance layers
                                            (+ two optional exposure layers)
    runtime/baked/exposure/features.geojson  classified WGS84 exposure vectors, for display
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
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from app.core.model_config import DISCLAIMER, load_model_config
from app.core.settings import Settings, get_settings
from app.assess import assessment_model_identity
from app.bake_identity import (
    BAKE_SCHEMA,
    BakeCompatibilityError,
    bake_sha256,
    preserve_additive_baked_children,
    promote_bake,
    processing_manifest,
    remove_generated_directory,
    sha256_file,
    source_lineage,
    validate_bake,
)
from app.processing.terrain import engine as terrain_engine
from app.processing.harmonization.grids import AnalysisGrid
from app.processing.mountain_pack import load_mountain_pack, validate_declared_sources

#: Terrain layers written to ``baked/layers/*.npy``. The first six drive the
#: model; the final two preserve the per-pixel source lineage used to build them.
#:
#:   elevation           runout physics; release-zone elevation banding
#:   slope               risk slope-band term; starting-zone terrain
#:   aspect              risk aspect-vs-wind loading term
#:   plan_curvature      runout gully friction; convergence
#:   general_curvature   risk convex-curvature term (slab tension)
#:   forest_mask         risk forest damping; runout forest friction
#:   terrain_source      DEM source code (2022 LiDAR / 2016 LiDAR / Copernicus)
#:   forest_source       canopy classification source (LiDAR / WorldCover)
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
    "terrain_source",
    "forest_source",
)

LAYER_SPECS: dict[str, dict[str, Any]] = {
    "elevation": {"dtype": "float32", "units": "m above sea level", "nodata": "NaN"},
    "slope": {"dtype": "float32", "units": "degrees", "nodata": "NaN"},
    "aspect": {
        "dtype": "float32",
        "units": "degrees clockwise from analysis-grid north; -1 denotes flat terrain",
        "nodata": "NaN",
    },
    "plan_curvature": {"dtype": "float32", "units": "1/100 m", "nodata": "NaN"},
    "general_curvature": {"dtype": "float32", "units": "1/100 m", "nodata": "NaN"},
    "forest_mask": {"dtype": "float32", "units": "binary fraction (0 or 1)", "nodata": "NaN"},
    "terrain_source": {"dtype": "uint8", "units": "categorical code", "nodata": 0},
    "forest_source": {"dtype": "uint8", "units": "categorical code", "nodata": 0},
    "exposure_weight": {
        "dtype": "float32",
        "units": "relative exposure weight 0-1; 0 means the extract maps nothing here",
        "nodata": "NaN",
    },
    "exposure_class": {
        "dtype": "uint8",
        "units": "exposure class code; 0 means the extract maps nothing here",
        "nodata": 255,
    },
}

#: Optional layers written only when the pack declares the source they need. They
#: are never required: a pack without exposure data still bakes, and the assessment
#: reports the exposure term as unavailable rather than as zero.
OPTIONAL_LAYERS = ("exposure_weight", "exposure_class")

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

def baked_root(settings: Settings) -> Path:
    return settings.runtime_root / "baked"


# --- Layers -------------------------------------------------------------------


def _layer_record(name: str, array: np.ma.MaskedArray, out_dir: Path) -> dict[str, Any]:
    """Write one ``.npy`` layer and describe it for ``meta.json``."""
    spec = LAYER_SPECS[name]
    dtype = str(spec["dtype"])
    nodata = spec["nodata"]
    fill_value = np.nan if nodata == "NaN" else nodata
    filled = array.astype(dtype).filled(fill_value)
    path = out_dir / f"{name}.npy"
    np.save(path, filled)
    valid = int((~np.ma.getmaskarray(array)).sum())
    return {
        "name": name,
        "file": f"layers/{name}.npy",
        "dtype": dtype,
        "shape": list(filled.shape),
        "units": spec["units"],
        "nodata": nodata,
        "valid_pixels": valid,
        "valid_fraction": round(valid / filled.size, 4),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


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
        records.append(_layer_record(name, np.ma.asarray(products[name]), out_dir))
    return records


# --- Exposure -----------------------------------------------------------------


def _bake_exposure(
    settings: Settings, mountain_pack, grid: AnalysisGrid, out: Path
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, Path | None]:
    """Bake the declared exposure extract into a raster, a class code and a vector.

    Returns the extra layer records, the ``meta.json`` exposure block, and the
    source path so it joins the bake's source lineage. A pack that declares no
    exposure asset -- or declares one that is missing -- bakes without exposure and
    the assessment reports the term as unavailable, never as zero.
    """
    from app.processing.exposure import build_exposure

    role = "exposure_features"
    if role not in mountain_pack.assets:
        return [], None, None
    source_path = mountain_pack.asset_path(settings.data_root, role)
    if not source_path.is_file():
        return [], None, None

    asset = mountain_pack.assets[role]
    products = build_exposure(
        source_path=source_path,
        aoi_path=mountain_pack.asset_path(settings.data_root, "aoi"),
        grid=grid,
        source_statement=asset.source.model_dump(mode="json"),
    )

    records = [
        _layer_record("exposure_weight", products.weight, out / "layers"),
        _layer_record("exposure_class", products.class_code, out / "layers"),
    ]
    features_path = out / "exposure" / "features.geojson"
    features_path.parent.mkdir(parents=True, exist_ok=True)
    features_path.write_text(
        json.dumps(products.features, separators=(",", ":")), encoding="utf-8"
    )
    exposure_meta = {
        **products.meta,
        "features_path": "exposure/features.geojson",
        "features_sha256": sha256_file(features_path),
        "features_bytes": features_path.stat().st_size,
        "feature_count": len(products.features["features"]),
        "layers": [record["name"] for record in records],
    }
    return records, exposure_meta, source_path


# --- Tiles --------------------------------------------------------------------


def _tile_cover(grid: AnalysisGrid):
    """Standards-based WebMercatorQuad tiles covering the AOI."""
    import morecantile

    return morecantile.tms.get("WebMercatorQuad").tiles(
        *_bounds_wgs84(grid), zooms=range(MIN_ZOOM, MAX_ZOOM + 1), truncate=True
    )


def _terrain_rgb(elevation: np.ma.MaskedArray, floor_m: float) -> bytes:
    """Encode Mapbox Terrain-RGB; gaps become a visible valley-floor plinth."""
    from rio_tiler.models import ImageData

    values = np.where(np.isfinite(elevation.filled(np.nan)), elevation.filled(np.nan), floor_m)
    packed = np.clip((values + 10000.0) / 0.1, 0, 256**3 - 1).astype("uint32")
    rgb = np.stack(((packed >> 16) & 255, (packed >> 8) & 255, packed & 255)).astype("uint8")
    return ImageData(rgb).render(img_format="PNG", add_mask=False)


def _bake_tiles(elevation_cog: Path, out_dir: Path, grid: AnalysisGrid) -> int:
    """Pre-render every terrain-RGB tile the mesh needs into ``out_dir``.

    rio-tiler owns the COG windowing, reprojection and tile bounds. The runtime
    still serves only the resulting static PNGs.
    """
    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader

    written = 0
    with Reader(str(elevation_cog)) as source:
        sample = source.preview(max_size=512).array.compressed()
        if not sample.size:
            raise ValueError(f"Elevation raster has no valid pixels: {elevation_cog}")
        floor = float(sample.min())
        for tile in _tile_cover(grid):
            try:
                image = source.tile(
                    tile.x,
                    tile.y,
                    tile.z,
                    tilesize=256,
                    resampling_method="bilinear",
                    reproject_method="bilinear",
                )
            except TileOutsideBounds:
                continue
            elevation = np.ma.asarray(image.array)[0]
            if not elevation.count():
                continue
            path = out_dir / str(tile.z) / str(tile.x) / f"{tile.y}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_terrain_rgb(elevation, floor))
            written += 1
    return written


def _sentinel_rgb_sources(settings: Settings) -> tuple[Path, Path, Path] | None:
    """Return pack-declared red, green and blue display rasters, if present."""
    pack, _ = load_mountain_pack(settings)
    roles = ("imagery_red", "imagery_green", "imagery_blue")
    if not any(role in pack.assets for role in roles):
        return None
    return tuple(pack.asset_path(settings.data_root, role) for role in roles)  # type: ignore[return-value]


def _imagery_stretch(sources: tuple[Path, Path, Path]) -> list[tuple[float, float]]:
    """Find robust per-band display limits without letting a few bright pixels dominate."""
    from rio_tiler.io import Reader

    limits: list[tuple[float, float]] = []
    for source in sources:
        with Reader(str(source)) as dataset:
            values = dataset.preview(max_size=1024).array.compressed()
        if not values.size:
            raise ValueError(f"Sentinel-2 band has no valid pixels: {source}")
        low, high = np.percentile(values, (2.0, 98.0))
        limits.append((float(low), float(high)))
    return limits


def _bake_imagery_tiles(
    sources: tuple[Path, Path, Path], out_dir: Path, grid: AnalysisGrid
) -> tuple[int, list[tuple[float, float]]]:
    """Bake a natural-colour RGBA tile pyramid for draping over the 3D mesh."""
    from contextlib import ExitStack

    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader
    from rio_tiler.models import ImageData

    stretch = _imagery_stretch(sources)
    written = 0

    with ExitStack() as stack:
        datasets = [stack.enter_context(Reader(str(source))) for source in sources]
        for tile in _tile_cover(grid):
            try:
                warped = np.ma.concatenate(
                    [
                        dataset.tile(
                            tile.x,
                            tile.y,
                            tile.z,
                            tilesize=256,
                            resampling_method="bilinear",
                            reproject_method="bilinear",
                        ).array
                        for dataset in datasets
                    ]
                )
            except TileOutsideBounds:
                continue
            valid = ~np.any(np.ma.getmaskarray(warped), axis=0)
            if not valid.any():
                continue
            rgb = np.zeros(warped.shape, dtype="uint8")
            for index, (low, high) in enumerate(stretch):
                normalized = np.clip((warped[index].filled(np.nan) - low) / (high - low), 0, 1)
                rgb[index] = np.nan_to_num(normalized ** (1 / 1.15) * 255).astype("uint8")
            image = ImageData(np.ma.array(rgb, mask=np.broadcast_to(~valid, rgb.shape)))
            path = out_dir / str(tile.z) / str(tile.x) / f"{tile.y}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image.render(img_format="PNG"))
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
    mountain_pack, mountain_pack_identity = load_mountain_pack(settings)
    pack_source_warnings = validate_declared_sources(mountain_pack, settings.data_root)
    started = time.perf_counter()
    target = baked_root(settings)
    processing = processing_manifest(settings.project_root)
    meta_path = target / "meta.json"

    if meta_path.exists() and not force:
        try:
            existing = validate_bake(
                target,
                expected_processing_sha256=processing["sha256"],
                expected_mountain_pack_sha256=mountain_pack_identity["sha256"],
            )
        except BakeCompatibilityError as exc:
            raise BakeCompatibilityError(
                f"Existing bake at {target} is stale or incompatible: {exc} "
                "Rebuild it explicitly with: python -m app.bake --force"
            ) from exc
        print(f"[bake] compatible bake already exists at {target} ({existing['identity']['bake_sha256'][:12]}).")
        return existing

    settings.runtime_root.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=".baked-build-", dir=settings.runtime_root))
    print(f"[bake] staging output -> {out}")

    try:
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
        print(f"[bake] rendering optional natural-colour tiles z{MIN_ZOOM}-z{MAX_ZOOM}...")
        imagery_sources = _sentinel_rgb_sources(settings)
        imagery_count = 0
        imagery_stretch: list[tuple[float, float]] = []
        if imagery_sources is not None:
            imagery_count, imagery_stretch = _bake_imagery_tiles(
                imagery_sources, out / "imagery", grid
            )
        print(f"[bake] wrote {imagery_count} imagery tiles")

        # 4. The .npy terrain layers the runtime loads with plain numpy.
        print(f"[bake] saving {len(BAKED_LAYERS)} layers to layers/*.npy...")
        layer_records = _save_layers(products, out / "layers")
        for record in layer_records:
            if record["name"] == "terrain_source":
                record["source_codes"] = terrain_meta["terrain_source_codes"]
            elif record["name"] == "forest_source":
                record["source_codes"] = terrain_meta["forest_model"]["source_codes"]

        # 4b. Optional exposure: what is in the valley, from the declared OSM
        #     extract. Consequence only -- it never enters the release model.
        print("[bake] building optional exposure layer from the declared extract...")
        exposure_records, exposure_meta, exposure_source = _bake_exposure(
            settings, mountain_pack, grid, out
        )
        layer_records.extend(exposure_records)
        if exposure_meta is None:
            print("[bake] no exposure asset declared or present; exposure will be unavailable")
        else:
            print(
                f"[bake] exposure: {exposure_meta['feature_count']} display features, "
                f"{exposure_meta['exposed_cell_count']} exposed cells "
                f"({exposure_meta['exposed_fraction_of_aoi']:.4%} of the AOI)"
            )

        input_paths = [
            *terrain_meta["source_files"],
            *(imagery_sources or ()),
            *((exposure_source,) if exposure_source is not None else ()),
            mountain_pack.asset_path(settings.data_root, "aoi"),
        ]
        sources = source_lineage(settings.data_root, input_paths)

        # 5. Metadata: everything the runtime and the map need, plus the reprojection
        #    lattice that replaces pyproj at runtime.
        print("[bake] building grid->WGS84 lattice and writing meta.json...")
        bbox = _bounds_wgs84(grid)
        terrain_model = terrain_meta.get("terrain_model", {})
        meta = {
        "schema": BAKE_SCHEMA,
        "mountain": {
            "id": mountain_pack.id,
            "name": mountain_pack.name,
            "model_profile": mountain_pack.model_profile,
            "model_calibrated_locally": mountain_pack.model_calibrated_locally,
        },
        "mountain_pack": mountain_pack_identity,
        "generated_at_utc": _utc_now_iso(),
        "duration_seconds": round(time.perf_counter() - started, 1),
        "disclaimer": DISCLAIMER,
        "grid": {
            "crs": grid.crs_string,
            "coordinate_order": mountain_pack.grid.coordinate_order,
            "resolution_m": grid.resolution_m,
            "west": grid.west,
            "south": grid.south,
            "east": grid.east,
            "north": grid.north,
            "width": grid.width,
            "height": grid.height,
            "transform": list(grid.transform)[:6],
            "transform_semantics": (
                "Affine (a,b,c,d,e,f) maps pixel-edge (col,row) to projected "
                "(easting,northing); array values represent cell centres."
            ),
            "vertical_datum": mountain_pack.grid.vertical_datum.model_dump(mode="json"),
        },
        "aoi_bbox_wgs84": bbox,
        "aoi_corners_wgs84": [
            [bbox[0], bbox[3]],
            [bbox[2], bbox[3]],
            [bbox[2], bbox[1]],
            [bbox[0], bbox[1]],
        ],
        "center_wgs84": list(mountain_pack.center_wgs84),
        "tiles": {
            "path": "tiles/{z}/{x}/{y}.png",
            "tile_size": 256,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "encoding": "mapbox",
            "count": tile_count,
        },
        "imagery": ({
            "path": "imagery/{z}/{x}/{y}.png",
            "tile_size": 256,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "count": imagery_count,
            "kind": "Natural-colour visual context",
            "captured_at_utc": mountain_pack.assets["imagery_red"].captured_at_utc,
            "cloud_percent": mountain_pack.assets["imagery_red"].cloud_percent,
            "source_resolution_m": mountain_pack.assets["imagery_red"].native_resolution_m,
            "display_stretch_2_98": [
                {"band": band, "low": round(low, 3), "high": round(high, 3)}
                for band, (low, high) in zip(("red", "green", "blue"), imagery_stretch)
            ],
            "visual_context_only": True,
        } if imagery_sources is not None else None),
        "layers": layer_records,
        "exposure": exposure_meta,
        "sources": sources,
        "processing": processing,
        "model": assessment_model_identity(),
        "terrain": {
            "lidar_fraction": terrain_model.get("lidar_fraction"),
            "valid_fraction": terrain_model.get("valid_fraction"),
            "effective_source_resolution_m": terrain_model.get("effective_source_resolution_m"),
            "coverage_by_source_label": terrain_model.get("coverage_by_source_label"),
            "source_codes": terrain_meta["terrain_source_codes"],
        },
        "forest": terrain_meta["forest_model"],
        "reproject": _reproject_lattice(grid),
        "warnings": sorted(
            set([*products.warnings, *mountain_pack.warnings(), *pack_source_warnings])
        ),
        "is_operational_forecast": False,
        }
        meta["identity"] = {"bake_sha256": bake_sha256(meta)}
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        validate_bake(
            out,
            expected_processing_sha256=processing["sha256"],
            expected_mountain_pack_sha256=mountain_pack_identity["sha256"],
        )
        preserved = preserve_additive_baked_children(target, out)
        for record in preserved:
            print(
                f"[bake] preserved additive {record['name']}: "
                f"{record['file_count']} files, {record['bytes']} bytes, "
                f"inventory={record['inventory_sha256'][:12]}"
            )
        promote_bake(out, target, settings.runtime_root)

        print(
            f"[bake] done in {meta['duration_seconds']}s: "
            f"{tile_count} tiles, {len(layer_records)} layers, "
            f"lidar_fraction={meta['terrain']['lidar_fraction']}, "
            f"bake_sha256={meta['identity']['bake_sha256'][:12]}"
        )
        if meta["warnings"]:
            print(f"[bake] {len(meta['warnings'])} warning(s):")
            for warning in meta["warnings"]:
                print(f"       - {warning}")
        return meta
    finally:
        if out.exists():
            remove_generated_directory(out, settings.runtime_root)


def check_bake(settings: Settings | None = None) -> dict[str, Any]:
    """Validate the existing bake without reading any source data."""
    settings = settings or get_settings()
    target = baked_root(settings)
    processing = processing_manifest(settings.project_root)
    _, mountain_pack_identity = load_mountain_pack(settings)
    try:
        meta = validate_bake(
            target,
            expected_processing_sha256=processing["sha256"],
            expected_mountain_pack_sha256=mountain_pack_identity["sha256"],
        )
    except BakeCompatibilityError as exc:
        raise BakeCompatibilityError(
            f"Bake at {target} is stale or incompatible: {exc} "
            "Rebuild it explicitly with: python -m app.bake --force"
        ) from exc
    print(f"[bake] valid {meta['schema']} at {target}: {meta['identity']['bake_sha256']}")
    return meta


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bake Stage 3 terrain artifacts (offline, one-time).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--force", action="store_true", help="Rebuild even if a bake already exists.")
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate the existing bake without reading the source-data tree.",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    settings.validate(require_data_root=not args.check)
    if args.check:
        check_bake(settings)
    else:
        bake(settings, force=args.force)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
