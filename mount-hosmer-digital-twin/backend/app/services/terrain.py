from __future__ import annotations

import colorsys
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image
from pyproj import Transformer
from scipy import ndimage

from app.core.paths import ensure_runtime_dirs, relative_source_path
from app.core.settings import Settings
from app.services.cache import cache_matches, source_fingerprint, write_cache_log

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import reproject, transform_bounds
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]


DISCLAIMER = (
    "Experimental research prototype. This output has not been validated for "
    "operational avalanche forecasting and must not be used as a replacement "
    "for professional avalanche forecasts or field assessment."
)

LAYER_IDS = {
    "elevation",
    "hillshade",
    "slope",
    "aspect",
    "landcover",
    "profile_curvature",
    "plan_curvature",
    "tri",
    "tpi",
    "flow_direction",
    "flow_accumulation",
    "ridges",
    "gullies",
    "drainage",
    "surface_height",
    "open_terrain_mask",
    "forested_terrain_mask",
    "bare_rock_mask",
    "snow_ice_mask",
    "terrain_susceptibility",
}

ESA_WORLDCOVER = {
    10: {"label": "Tree cover / forest", "color": "#0b6b3a", "susceptibility": 20},
    20: {"label": "Shrubland", "color": "#b7a642", "susceptibility": 65},
    30: {"label": "Grassland", "color": "#d8d85d", "susceptibility": 75},
    40: {"label": "Cropland", "color": "#c48a4a", "susceptibility": 55},
    50: {"label": "Built-up", "color": "#c65f5f", "susceptibility": 10},
    60: {"label": "Bare or sparse vegetation", "color": "#d4c0a1", "susceptibility": 85},
    70: {"label": "Snow and ice", "color": "#e9f4ff", "susceptibility": 80},
    80: {"label": "Water", "color": "#3f82c4", "susceptibility": 0},
    90: {"label": "Herbaceous wetland", "color": "#64a889", "susceptibility": 20},
    95: {"label": "Mangroves", "color": "#1d5d44", "susceptibility": 20},
    100: {"label": "Moss and lichen", "color": "#c5d7a0", "susceptibility": 65},
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def terrain_paths(settings: Settings) -> dict[str, Path]:
    root = settings.data_root
    return {
        "dem": root / "static" / "terrain_fallback" / "Copernicus_DEM_GLO30_EPSG26911_30m.tif",
        "slope": root / "static" / "terrain_fallback" / "Copernicus_DEM_Slope_Degrees_EPSG26911_30m.tif",
        "aspect": root / "static" / "terrain_fallback" / "Copernicus_DEM_Aspect_Degrees_EPSG26911_30m.tif",
        "landcover": root / "static" / "landcover" / "ESA_WorldCover_2021_EPSG26911_10m.tif",
        "osm": root / "static" / "openstreetmap" / "mount_hosmer_osm_features.geojson",
        "aoi": root / "metadata" / "mount_hosmer_aoi.geojson",
        "grid": root / "metadata" / "grid_and_aoi.json",
        "lidar_dem_dir": root / "static" / "lidar_bc" / "downloads" / "LiDAR_DEM_Index_1_20_000",
        "lidar_dsm_dir": root / "static" / "lidar_bc" / "downloads" / "LiDAR_DSM_Index_1_20_000",
    }


def layer_index_path(settings: Settings) -> Path:
    return settings.runtime_root / "processed" / "static" / "terrain_layers.json"


def layer_metadata_path(settings: Settings, layer_id: str) -> Path:
    return settings.runtime_root / "processed" / "static" / f"{layer_id}.metadata.json"


def layer_preview_path(settings: Settings, layer_id: str) -> Path:
    return settings.runtime_root / "previews" / "layers" / f"{layer_id}.png"


def processed_raster_path(settings: Settings, layer_id: str) -> Path:
    return settings.runtime_root / "processed" / "static" / f"{layer_id}.tif"


def processed_vector_path(settings: Settings, asset_id: str) -> Path:
    return settings.runtime_root / "processed" / "static" / f"{asset_id}.geojson"


def validate_layer_id(layer_id: str) -> str:
    if layer_id not in LAYER_IDS:
        raise KeyError(f"Unknown layer id: {layer_id}")
    return layer_id


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_weights(settings: Settings) -> dict[str, Any]:
    path = settings.backend_root / "config" / "susceptibility_weights.yaml"
    if not path.exists():
        return {
            "terrain": {
                "slope_weight": 0.45,
                "curvature_weight": 0.10,
                "ruggedness_weight": 0.10,
                "landcover_weight": 0.10,
                "elevation_weight": 0.10,
                "aspect_weight": 0.10,
                "ridge_gully_weight": 0.05,
            },
            "combined": {"terrain_weight": 0.65, "dynamic_weight": 0.35},
        }
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_raster(path: Path) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    if rasterio is None:
        raise RuntimeError("rasterio is required for terrain processing")
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True).astype("float32")
        profile = src.profile.copy()
        profile["bounds"] = src.bounds
        profile["transform"] = src.transform
        profile["crs"] = src.crs
        profile["resolution"] = src.res
        return arr, profile


def normalize(values: np.ma.MaskedArray | np.ndarray, low: float | None = None, high: float | None = None) -> np.ndarray:
    arr = np.ma.asarray(values).astype("float32")
    compressed = arr.compressed() if np.ma.is_masked(arr) else np.asarray(arr).ravel()
    compressed = compressed[np.isfinite(compressed)]
    if compressed.size == 0:
        return np.zeros(arr.shape, dtype="float32")
    lo = float(np.percentile(compressed, 2)) if low is None else low
    hi = float(np.percentile(compressed, 98)) if high is None else high
    if hi <= lo:
        hi = lo + 1.0
    out = (np.asarray(arr.filled(lo), dtype="float32") - lo) / (hi - lo)
    return np.clip(out, 0, 1)


def rgba_from_hex(color: str, alpha: int = 220) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def ramp(values: np.ndarray, colors: list[str], alpha: np.ndarray | None = None) -> np.ndarray:
    values = np.clip(values, 0, 1)
    stops = np.linspace(0, 1, len(colors))
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    channels = np.array([rgba_from_hex(color, 255) for color in colors], dtype=np.float32)
    for channel in range(3):
        rgba[..., channel] = np.interp(values, stops, channels[:, channel]).astype(np.uint8)
    rgba[..., 3] = 225 if alpha is None else alpha.astype(np.uint8)
    return rgba


def save_png(path: Path, rgba: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path)


def raster_coordinates(profile: dict[str, Any]) -> tuple[list[list[float]], list[float]]:
    bounds = profile["bounds"]
    crs = profile["crs"]
    west, south, east, north = transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21)
    return [[west, north], [east, north], [east, south], [west, south]], [west, south, east, north]


def layer_base(
    settings: Settings,
    layer_id: str,
    title: str,
    group: str,
    source_files: list[Path],
    profile: dict[str, Any],
    legend: list[dict[str, str]],
    opacity: float,
    stats: dict[str, Any],
    algorithm: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    coordinates, bounds_wgs84 = raster_coordinates(profile)
    preview_path = layer_preview_path(settings, layer_id)
    metadata = {
        "id": layer_id,
        "title": title,
        "group": group,
        "kind": "raster_overlay",
        "preview_url": f"/api/layers/{layer_id}/preview",
        "metadata_url": f"/api/layers/{layer_id}/metadata",
        "download_url": f"/api/download/{layer_id}" if processed_raster_path(settings, layer_id).exists() else None,
        "coordinates": coordinates,
        "bounds_wgs84": bounds_wgs84,
        "opacity": opacity,
        "legend": legend,
        "stats": stats,
        "source_files": [relative_source_path(settings.data_root, path) for path in source_files if path.exists()],
        "generated_preview": str(preview_path.relative_to(settings.project_root).as_posix()),
        "processing": {
            "generated_at_utc": utc_now_iso(),
            "algorithm": algorithm,
            "crs": str(profile["crs"]),
            "resolution": list(profile["resolution"]),
            "bounds": list(profile["bounds"]),
        },
        "warnings": warnings or [],
    }
    layer_metadata_path(settings, layer_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def masked_stats(arr: np.ma.MaskedArray | np.ndarray) -> dict[str, float | int | None]:
    masked = np.ma.asarray(arr)
    values = masked.compressed() if np.ma.is_masked(masked) else np.asarray(masked).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"min": None, "max": None, "mean": None, "valid_pixels": 0}
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "valid_pixels": int(values.size),
    }


def hillshade(dem: np.ma.MaskedArray, xres: float, yres: float) -> np.ma.MaskedArray:
    filled = dem.filled(float(dem.mean()))
    dy, dx = np.gradient(filled, yres, xres)
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    azimuth = np.deg2rad(315.0)
    altitude = np.deg2rad(45.0)
    shaded = 255.0 * (
        np.sin(altitude) * np.sin(slope)
        + np.cos(altitude) * np.cos(slope) * np.cos(azimuth - aspect)
    )
    shaded = np.clip(shaded, 0, 255).astype("float32")
    return np.ma.array(shaded, mask=np.ma.getmaskarray(dem))


def landcover_to_rgba(landcover: np.ma.MaskedArray) -> np.ndarray:
    rgba = np.zeros((*landcover.shape, 4), dtype=np.uint8)
    data = np.asarray(landcover.filled(-9999)).astype(int)
    for value, details in ESA_WORLDCOVER.items():
        rgba[data == value] = rgba_from_hex(details["color"], 215)
    rgba[np.ma.getmaskarray(landcover)] = (0, 0, 0, 0)
    return rgba


def aspect_to_rgba(aspect: np.ma.MaskedArray) -> np.ndarray:
    data = np.asarray(aspect.filled(0), dtype="float32")
    hsv = (data % 360.0) / 360.0
    rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
    flat = hsv.ravel()
    colors = np.array([colorsys.hsv_to_rgb(float(value), 0.66, 0.95) for value in flat], dtype=np.float32)
    rgba[..., :3] = (colors.reshape((*data.shape, 3)) * 255).astype(np.uint8)
    rgba[..., 3] = 210
    rgba[np.ma.getmaskarray(aspect)] = (0, 0, 0, 0)
    return rgba


def resample_landcover_to_dem(landcover_path: Path, dem_profile: dict[str, Any]) -> np.ndarray:
    with rasterio.open(landcover_path) as src:
        destination = np.full((dem_profile["height"], dem_profile["width"]), -9999, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=destination,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dem_profile["transform"],
            dst_crs=dem_profile["crs"],
            dst_nodata=-9999,
            resampling=Resampling.nearest,
        )
    return destination


def score_aspect(aspect: np.ma.MaskedArray) -> np.ndarray:
    data = np.asarray(aspect.filled(180), dtype="float32") % 360.0
    northness = (np.cos(np.deg2rad(data)) + 1.0) / 2.0
    eastness = (np.sin(np.deg2rad(data)) + 1.0) / 2.0
    return np.clip((0.65 * northness + 0.35 * eastness) * 100.0, 0, 100)


def terrain_susceptibility(
    dem: np.ma.MaskedArray,
    slope: np.ma.MaskedArray,
    aspect: np.ma.MaskedArray,
    landcover_resampled: np.ndarray,
    weights: dict[str, Any],
) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    mask = np.ma.getmaskarray(dem) | np.ma.getmaskarray(slope) | np.ma.getmaskarray(aspect)
    terrain_weights = weights.get("terrain", {})
    weight_values = {
        "slope": float(terrain_weights.get("slope_weight", 0.45)),
        "curvature": float(terrain_weights.get("curvature_weight", 0.10)),
        "ruggedness": float(terrain_weights.get("ruggedness_weight", 0.10)),
        "landcover": float(terrain_weights.get("landcover_weight", 0.10)),
        "elevation": float(terrain_weights.get("elevation_weight", 0.10)),
        "aspect": float(terrain_weights.get("aspect_weight", 0.10)),
        "ridge_gully": float(terrain_weights.get("ridge_gully_weight", 0.05)),
    }
    total = sum(weight_values.values()) or 1.0
    weight_values = {key: value / total for key, value in weight_values.items()}

    slope_data = np.asarray(slope.filled(0), dtype="float32")
    slope_score = np.interp(
        slope_data,
        [0, 20, 25, 30, 38, 45, 55, 70],
        [0, 5, 30, 75, 100, 95, 55, 25],
    )

    filled_dem = np.asarray(dem.filled(float(dem.mean())), dtype="float32")
    smoothed = ndimage.uniform_filter(filled_dem, size=7, mode="nearest")
    tpi = filled_dem - smoothed
    rugged = ndimage.generic_filter(filled_dem, np.std, size=3, mode="nearest")
    laplace = np.abs(ndimage.laplace(filled_dem))

    elevation_score = normalize(dem) * 100.0
    rugged_score = normalize(rugged) * 100.0
    curvature_score = normalize(laplace) * 100.0
    ridge_gully_score = normalize(np.abs(tpi)) * 100.0
    aspect_score = score_aspect(aspect)

    landcover_score = np.zeros_like(slope_data, dtype="float32") + 50
    lc_int = landcover_resampled.astype(int)
    for value, details in ESA_WORLDCOVER.items():
        landcover_score[lc_int == value] = float(details["susceptibility"])
    landcover_score[lc_int == -9999] = 50

    combined = (
        slope_score * weight_values["slope"]
        + curvature_score * weight_values["curvature"]
        + rugged_score * weight_values["ruggedness"]
        + landcover_score * weight_values["landcover"]
        + elevation_score * weight_values["elevation"]
        + aspect_score * weight_values["aspect"]
        + ridge_gully_score * weight_values["ridge_gully"]
    )
    combined = np.clip(combined, 0, 100).astype("float32")
    combined[mask] = -9999
    score = np.ma.masked_equal(combined, -9999)

    explanation = {
        "disclaimer": DISCLAIMER,
        "model_type": "Configurable rules-based static terrain susceptibility prototype",
        "not_used": "No supervised classifier was trained because historical avalanche occurrence labels are incomplete.",
        "weights_normalized": weight_values,
        "factors": [
            {"factor": "Slope", "source": "Copernicus DEM slope", "reason": "Dominant terrain control; 30-45 degrees is weighted highest."},
            {"factor": "Elevation", "source": "Copernicus DEM", "reason": "Higher terrain receives a larger heuristic score."},
            {"factor": "Aspect", "source": "Copernicus DEM aspect", "reason": "North and east aspects receive a larger static score in this prototype."},
            {"factor": "Land cover", "source": "ESA WorldCover 2021", "reason": "Open and bare terrain scores higher than forested or water classes."},
            {"factor": "Ruggedness", "source": "DEM neighborhood standard deviation", "reason": "Rougher terrain receives higher context score."},
            {"factor": "Curvature", "source": "DEM Laplacian approximation", "reason": "Local convex/concave terrain contributes to release/terrain-trap context."},
            {"factor": "Ridge/gully proxy", "source": "Topographic position index", "reason": "Large positive or negative local position scores higher."},
        ],
        "classes": [
            {"min": 0, "max": 24, "label": "Low terrain susceptibility", "color": "#2f8a4c"},
            {"min": 25, "max": 49, "label": "Moderate", "color": "#d6c64b"},
            {"min": 50, "max": 74, "label": "Considerable", "color": "#dd8f35"},
            {"min": 75, "max": 100, "label": "High", "color": "#c23b35"},
        ],
    }
    return score, explanation


def susceptibility_to_rgba(score: np.ma.MaskedArray) -> np.ndarray:
    data = np.asarray(score.filled(-9999), dtype="float32")
    colors = ["#2f8a4c", "#d6c64b", "#dd8f35", "#c23b35"]
    rgba = ramp(np.clip(data / 100.0, 0, 1), colors)
    rgba[..., 3] = 185
    rgba[np.ma.getmaskarray(score)] = (0, 0, 0, 0)
    return rgba


def write_susceptibility_tif(settings: Settings, score: np.ma.MaskedArray, dem_profile: dict[str, Any]) -> Path:
    path = processed_raster_path(settings, "terrain_susceptibility")
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dem_profile.copy()
    profile.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate")
    profile.pop("bounds", None)
    profile.pop("resolution", None)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(score.filled(-9999).astype("float32"), 1)
    return path


def grid_profile(settings: Settings, resolution: float = 30.0) -> dict[str, Any]:
    grid_path = terrain_paths(settings)["grid"]
    if grid_path.exists():
        grid = json.loads(grid_path.read_text(encoding="utf-8"))
        west, south, east, north = grid["fixed_extent_analysis_crs"]
    else:
        west, south, east, north = [637650.0, 5491570.0, 649650.0, 5503570.0]
    width = int(round((east - west) / resolution))
    height = int(round((north - south) / resolution))
    return {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": rasterio.crs.CRS.from_string("EPSG:26911"),
        "transform": from_origin(west, north, resolution, resolution),
        "nodata": -9999.0,
        "bounds": rasterio.coords.BoundingBox(west, south, east, north),
        "resolution": (resolution, resolution),
    }


def select_latest_lidar_tiles(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    by_tile: dict[str, Path] = {}
    for path in sorted(folder.glob("*.tif")):
        stem = path.stem.lower().replace("_dsm", "")
        parts = stem.split("_")
        tile_key = "_".join(parts[:2]) if len(parts) >= 2 else stem
        current = by_tile.get(tile_key)
        if current is None or ("2022" in path.name and "2022" not in current.name):
            by_tile[tile_key] = path
    return sorted(by_tile.values())


def mosaic_lidar_to_grid(tile_paths: list[Path], target_profile: dict[str, Any]) -> tuple[np.ma.MaskedArray, list[str]]:
    data = np.full((target_profile["height"], target_profile["width"]), -9999.0, dtype="float32")
    warnings: list[str] = []
    for tile_path in tile_paths:
        try:
            with rasterio.open(tile_path) as src:
                temp = np.full_like(data, -9999.0)
                reproject(
                    source=rasterio.band(src, 1),
                    destination=temp,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=target_profile["transform"],
                    dst_crs=target_profile["crs"],
                    dst_nodata=-9999.0,
                    resampling=Resampling.bilinear,
                )
                valid = np.isfinite(temp) & (temp != -9999.0)
                data[valid] = temp[valid]
        except Exception as exc:
            warnings.append(f"LiDAR tile could not be mosaicked: {tile_path.name}: {exc}")
    return np.ma.masked_equal(data, -9999.0), warnings


def write_raster(settings: Settings, layer_id: str, arr: np.ma.MaskedArray | np.ndarray, profile: dict[str, Any]) -> Path:
    path = processed_raster_path(settings, layer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    output_profile = profile.copy()
    output_profile.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate")
    output_profile.pop("bounds", None)
    output_profile.pop("resolution", None)
    with rasterio.open(path, "w", **output_profile) as dst:
        dst.write(np.ma.asarray(arr).filled(-9999.0).astype("float32"), 1)
    return path


def write_mask_raster(settings: Settings, layer_id: str, mask: np.ndarray, profile: dict[str, Any]) -> Path:
    return write_raster(settings, layer_id, np.ma.array(mask.astype("float32"), mask=False), profile)


def slope_aspect_from_dem(dem: np.ma.MaskedArray, xres: float, yres: float) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    filled = dem.filled(float(dem.mean()))
    dy, dx = np.gradient(filled, yres, xres)
    slope = np.rad2deg(np.arctan(np.hypot(dx, dy))).astype("float32")
    aspect = (np.rad2deg(np.arctan2(-dx, dy)) + 360.0) % 360.0
    mask = np.ma.getmaskarray(dem)
    return np.ma.array(slope, mask=mask), np.ma.array(aspect.astype("float32"), mask=mask)


def terrain_derivatives(dem: np.ma.MaskedArray, xres: float, yres: float) -> dict[str, np.ma.MaskedArray]:
    filled = dem.filled(float(dem.mean()))
    mask = np.ma.getmaskarray(dem)
    dy, dx = np.gradient(filled, yres, xres)
    dyy, dyx = np.gradient(dy, yres, xres)
    dxy, dxx = np.gradient(dx, yres, xres)
    gradient_sq = dx * dx + dy * dy
    gradient = np.sqrt(gradient_sq)
    eps = 1e-6
    profile_curvature = -((dx * dx * dxx + 2 * dx * dy * dxy + dy * dy * dyy) / ((gradient_sq + eps) * np.sqrt(gradient_sq + 1 + eps)))
    plan_curvature = ((dy * dy * dxx - 2 * dx * dy * dxy + dx * dx * dyy) / ((gradient_sq + eps) ** 1.5))
    rugged = ndimage.generic_filter(filled, np.std, size=3, mode="nearest")
    tpi = filled - ndimage.uniform_filter(filled, size=9, mode="nearest")
    tri = np.sqrt(ndimage.generic_filter((filled - ndimage.uniform_filter(filled, size=3, mode="nearest")) ** 2, np.mean, size=3, mode="nearest"))
    flow_direction, flow_accumulation = flow_products(filled, mask, xres, yres)
    ridge_threshold = np.percentile(tpi[~mask], 88) if np.any(~mask) else 0
    gully_threshold = np.percentile(tpi[~mask], 12) if np.any(~mask) else 0
    accum_threshold = np.percentile(flow_accumulation[~mask], 92) if np.any(~mask) else 0
    ridges = ((tpi >= ridge_threshold) & (gradient > np.percentile(gradient[~mask], 50))).astype("float32")
    gullies = ((tpi <= gully_threshold) & (flow_accumulation >= np.percentile(flow_accumulation[~mask], 65))).astype("float32")
    drainage = (flow_accumulation >= accum_threshold).astype("float32")
    return {
        "profile_curvature": np.ma.array(profile_curvature.astype("float32"), mask=mask),
        "plan_curvature": np.ma.array(plan_curvature.astype("float32"), mask=mask),
        "tri": np.ma.array(tri.astype("float32"), mask=mask),
        "tpi": np.ma.array(tpi.astype("float32"), mask=mask),
        "flow_direction": np.ma.array(flow_direction.astype("float32"), mask=mask),
        "flow_accumulation": np.ma.array(flow_accumulation.astype("float32"), mask=mask),
        "ridges": np.ma.array(ridges, mask=mask),
        "gullies": np.ma.array(gullies, mask=mask),
        "drainage": np.ma.array(drainage, mask=mask),
    }


def flow_products(elevation: np.ndarray, mask: np.ndarray, xres: float, yres: float) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = elevation.shape
    directions = np.zeros((rows, cols), dtype="float32")
    receivers = np.full((rows, cols, 2), -1, dtype=np.int32)
    offsets = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]
    distances = np.array([yres, np.hypot(xres, yres), xres, np.hypot(xres, yres), yres, np.hypot(xres, yres), xres, np.hypot(xres, yres)], dtype="float32")
    for code, (dr, dc) in enumerate(offsets, start=1):
        source = elevation[max(0, -dr): rows - max(0, dr), max(0, -dc): cols - max(0, dc)]
        target = elevation[max(0, dr): rows - max(0, -dr), max(0, dc): cols - max(0, -dc)]
        drop = (source - target) / distances[code - 1]
        view = directions[max(0, -dr): rows - max(0, dr), max(0, -dc): cols - max(0, dc)]
        rec_view = receivers[max(0, -dr): rows - max(0, dr), max(0, -dc): cols - max(0, dc)]
        update = drop > view
        view[update] = drop[update]
        rr, cc = np.indices(source.shape)
        rec_view[..., 0][update] = rr[update] + max(0, -dr) + dr
        rec_view[..., 1][update] = cc[update] + max(0, -dc) + dc
    direction_codes = np.zeros((rows, cols), dtype="float32")
    for code, (dr, dc) in enumerate(offsets, start=1):
        src_rows, src_cols = np.indices((rows, cols))
        direction_codes[(receivers[..., 0] == src_rows + dr) & (receivers[..., 1] == src_cols + dc)] = code
    direction_codes[mask] = 0
    accumulation = np.ones((rows, cols), dtype="float32")
    accumulation[mask] = 0
    flat_order = np.argsort(elevation.ravel())[::-1]
    for flat_index in flat_order:
        r, c = divmod(int(flat_index), cols)
        if mask[r, c]:
            continue
        rr, cc = receivers[r, c]
        if rr >= 0 and cc >= 0 and not mask[rr, cc]:
            accumulation[rr, cc] += accumulation[r, c]
    return direction_codes, accumulation


def binary_rgba(mask: np.ma.MaskedArray, color: str, alpha: int = 220) -> np.ndarray:
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    active = np.asarray(mask.filled(0)) > 0
    rgba[active] = rgba_from_hex(color, alpha)
    return rgba


def write_contours(settings: Settings, dem: np.ma.MaskedArray, profile: dict[str, Any], interval: int = 100) -> dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output = processed_vector_path(settings, "contours")
    output.parent.mkdir(parents=True, exist_ok=True)
    values = dem.filled(np.nan)
    valid = dem.compressed()
    if valid.size == 0:
        geojson = {"type": "FeatureCollection", "features": []}
        output.write_text(json.dumps(geojson), encoding="utf-8")
        return geojson
    levels = np.arange(np.floor(valid.min() / interval) * interval, np.ceil(valid.max() / interval) * interval + interval, interval)
    transform = profile["transform"]
    cols = np.arange(profile["width"])
    rows = np.arange(profile["height"])
    xs = transform.c + (cols + 0.5) * transform.a
    ys = transform.f + (rows + 0.5) * transform.e
    contour_set = plt.contour(xs, ys, values, levels=levels)
    transformer = Transformer.from_crs(profile["crs"], "EPSG:4326", always_xy=True)
    features = []
    for level, segments in zip(contour_set.levels, contour_set.allsegs):
        for vertices in segments:
            if len(vertices) < 2:
                continue
            lon, lat = transformer.transform(vertices[:, 0], vertices[:, 1])
            coords = [[float(x), float(y)] for x, y in zip(lon, lat)]
            features.append(
                {
                    "type": "Feature",
                    "properties": {"elevation_m": float(level)},
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
            )
    plt.close()
    geojson = {"type": "FeatureCollection", "features": features}
    output.write_text(json.dumps(geojson), encoding="utf-8")
    return geojson


def landcover_masks(landcover_resampled: np.ndarray) -> dict[str, np.ndarray]:
    lc = landcover_resampled.astype(int)
    return {
        "open_terrain_mask": np.isin(lc, [20, 30, 40, 60, 70, 100]).astype("float32"),
        "forested_terrain_mask": (lc == 10).astype("float32"),
        "bare_rock_mask": (lc == 60).astype("float32"),
        "snow_ice_mask": (lc == 70).astype("float32"),
    }


def choose_dem(settings: Settings, target_profile: dict[str, Any]) -> tuple[np.ma.MaskedArray, dict[str, Any], list[Path], list[str], str]:
    paths = terrain_paths(settings)
    warnings: list[str] = []
    lidar_tiles = select_latest_lidar_tiles(paths["lidar_dem_dir"])
    if lidar_tiles:
        mosaic, mosaic_warnings = mosaic_lidar_to_grid(lidar_tiles, target_profile)
        warnings.extend(mosaic_warnings)
        coverage = float(mosaic.count()) / float(mosaic.size)
        if coverage > 0.95:
            write_raster(settings, "elevation", mosaic, target_profile)
            return mosaic, target_profile, lidar_tiles, warnings, f"BC LiDAR DEM mosaic resampled to 30 m ({coverage:.1%} grid coverage)"
        warnings.append(f"LiDAR DEM mosaic coverage was only {coverage:.1%}; falling back to Copernicus DEM.")
    dem, dem_profile = read_raster(paths["dem"])
    return dem, dem_profile, [paths["dem"]], warnings, "Copernicus fallback DEM GLO-30, 30 m"


def build_surface_height(settings: Settings, dem: np.ma.MaskedArray, target_profile: dict[str, Any]) -> tuple[np.ma.MaskedArray | None, list[str], list[Path]]:
    paths = terrain_paths(settings)
    dsm_tiles = select_latest_lidar_tiles(paths["lidar_dsm_dir"])
    warnings: list[str] = []
    if not dsm_tiles:
        return None, ["No LiDAR DSM tiles found; surface height skipped."], []
    dsm, dsm_warnings = mosaic_lidar_to_grid(dsm_tiles, target_profile)
    warnings.extend(dsm_warnings)
    coverage = float(dsm.count()) / float(dsm.size)
    if coverage <= 0.95:
        warnings.append(f"LiDAR DSM mosaic coverage was only {coverage:.1%}; surface height skipped.")
        return None, warnings, dsm_tiles
    write_raster(settings, "selected_dsm", dsm, target_profile)
    surface = np.ma.array(dsm - dem, mask=np.ma.getmaskarray(dsm) | np.ma.getmaskarray(dem))
    write_raster(settings, "surface_height", surface, target_profile)
    return surface, warnings, dsm_tiles


def terrain_metadata(settings: Settings) -> dict[str, Any]:
    paths = terrain_paths(settings)
    index_path = layer_index_path(settings)
    selected = "BC LiDAR DEM mosaic resampled to 30 m" if processed_raster_path(settings, "elevation").exists() else "Copernicus fallback DEM GLO-30, 30 m"
    reason = "BC LiDAR DEM/DSM tiles are used when mosaicking covers the AOI; Copernicus is retained as fallback."
    if index_path.exists():
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            selected = index.get("terrain_metadata", {}).get("terrain_source_selected", selected)
            reason = index.get("terrain_metadata", {}).get("terrain_source_reason", reason)
        except Exception:
            pass
    return {
        "analysis_crs": "EPSG:26911",
        "terrain_source_selected": selected,
        "terrain_source_reason": reason,
        "available_sources": {
            "copernicus_dem": paths["dem"].exists(),
            "copernicus_slope": paths["slope"].exists(),
            "copernicus_aspect": paths["aspect"].exists(),
            "worldcover": paths["landcover"].exists(),
            "osm": paths["osm"].exists(),
            "bc_lidar_dem_tiles_present": (settings.data_root / "static" / "lidar_bc" / "downloads" / "LiDAR_DEM_Index_1_20_000").exists(),
            "bc_lidar_dsm_tiles_present": (settings.data_root / "static" / "lidar_bc" / "downloads" / "LiDAR_DSM_Index_1_20_000").exists(),
        },
        "lidar_dem_tiles": [path.name for path in select_latest_lidar_tiles(paths["lidar_dem_dir"])],
        "lidar_dsm_tiles": [path.name for path in select_latest_lidar_tiles(paths["lidar_dsm_dir"])],
        "disclaimer": DISCLAIMER,
    }


def generate_terrain_layers(settings: Settings, force: bool = False) -> dict[str, Any]:
    if rasterio is None:
        raise RuntimeError("rasterio is required for terrain processing")
    ensure_runtime_dirs(settings.runtime_root)
    (settings.runtime_root / "previews" / "layers").mkdir(parents=True, exist_ok=True)
    (settings.runtime_root / "processed" / "static").mkdir(parents=True, exist_ok=True)
    index_path = layer_index_path(settings)
    paths = terrain_paths(settings)
    source_files = [
        paths["dem"],
        paths["slope"],
        paths["aspect"],
        paths["landcover"],
        paths["osm"],
        paths["aoi"],
        paths["grid"],
        *select_latest_lidar_tiles(paths["lidar_dem_dir"]),
        *select_latest_lidar_tiles(paths["lidar_dsm_dir"]),
    ]
    cache_metadata = source_fingerprint(
        settings,
        source_files,
        config_files=[settings.backend_root / "config" / "susceptibility_weights.yaml"],
        parameters={
            "processor": "terrain",
            "analysis_crs": "EPSG:26911",
            "terrain_resolution_m": 30,
            "landcover_resolution_m": 10,
        },
    )
    if not force and index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        if (
            cache_matches(data.get("cache", {}), cache_metadata)
            and all(layer_preview_path(settings, layer["id"]).exists() for layer in data.get("layers", []))
        ):
            return data

    for key in ["dem", "landcover"]:
        if not paths[key].exists():
            raise FileNotFoundError(f"Required terrain source missing: {paths[key]}")

    target_profile = grid_profile(settings, resolution=30.0)
    dem, dem_profile, dem_sources, dem_warnings, terrain_source = choose_dem(settings, target_profile)
    dem_profile["height"] = dem.shape[0]
    dem_profile["width"] = dem.shape[1]
    slope, aspect = slope_aspect_from_dem(dem, float(dem_profile["resolution"][0]), float(dem_profile["resolution"][1]))
    write_raster(settings, "elevation", dem, dem_profile)
    write_raster(settings, "slope", slope, dem_profile)
    write_raster(settings, "aspect", aspect, dem_profile)

    landcover, landcover_profile = read_raster(paths["landcover"])
    write_raster(settings, "landcover", landcover, landcover_profile)
    landcover_resampled = resample_landcover_to_dem(paths["landcover"], dem_profile)

    surface_height, surface_warnings, dsm_sources = build_surface_height(settings, dem, dem_profile)

    valid_alpha = (~np.ma.getmaskarray(dem) * 225).astype(np.uint8)
    save_png(
        layer_preview_path(settings, "elevation"),
        ramp(normalize(dem), ["#355c45", "#8e935a", "#caa36b", "#e5dfcf"], alpha=valid_alpha),
    )

    shade = hillshade(dem, float(dem_profile["resolution"][0]), float(dem_profile["resolution"][1]))
    write_raster(settings, "hillshade", shade, dem_profile)
    save_png(layer_preview_path(settings, "hillshade"), ramp(normalize(shade, 0, 255), ["#111111", "#ffffff"], alpha=valid_alpha))

    save_png(
        layer_preview_path(settings, "slope"),
        ramp(normalize(slope, 0, 65), ["#2f8a4c", "#d6c64b", "#dd8f35", "#c23b35"], alpha=(~np.ma.getmaskarray(slope) * 215).astype(np.uint8)),
    )
    save_png(layer_preview_path(settings, "aspect"), aspect_to_rgba(aspect))
    save_png(layer_preview_path(settings, "landcover"), landcover_to_rgba(landcover))

    derivatives = terrain_derivatives(dem, float(dem_profile["resolution"][0]), float(dem_profile["resolution"][1]))
    derivative_specs = {
        "profile_curvature": ("Profile curvature", "Terrain derivative", ["#3f4c6b", "#eeeeee", "#8b3f2f"], "DEM second derivative along slope direction"),
        "plan_curvature": ("Plan curvature", "Terrain derivative", ["#3f4c6b", "#eeeeee", "#8b3f2f"], "DEM second derivative across contour direction"),
        "tri": ("Terrain Ruggedness Index", "Terrain derivative", ["#243b53", "#b8c06a", "#d16d3d"], "3x3 terrain ruggedness approximation"),
        "tpi": ("Topographic Position Index", "Terrain derivative", ["#3f6f8f", "#eeeeee", "#9a5d2f"], "Elevation minus 9x9 neighborhood mean"),
        "flow_direction": ("Flow direction", "Hydrology", ["#293241", "#98c1d9", "#ee6c4d"], "D8 steepest-descent flow direction code"),
        "flow_accumulation": ("Flow accumulation", "Hydrology", ["#14213d", "#219ebc", "#fca311"], "Approximate D8 contributing-cell accumulation"),
    }
    for layer_id, (title, group, colors, algorithm) in derivative_specs.items():
        arr = derivatives[layer_id]
        write_raster(settings, layer_id, arr, dem_profile)
        values = np.log1p(arr) if layer_id == "flow_accumulation" else arr
        save_png(layer_preview_path(settings, layer_id), ramp(normalize(values), colors, alpha=(~np.ma.getmaskarray(arr) * 190).astype(np.uint8)))

    for layer_id, color in [("ridges", "#f6f1d1"), ("gullies", "#42c2ff"), ("drainage", "#168aad")]:
        write_raster(settings, layer_id, derivatives[layer_id], dem_profile)
        save_png(layer_preview_path(settings, layer_id), binary_rgba(derivatives[layer_id], color, 230))

    if surface_height is not None:
        save_png(layer_preview_path(settings, "surface_height"), ramp(normalize(surface_height, 0, 45), ["#1b4965", "#bee9e8", "#ffb703"], alpha=(~np.ma.getmaskarray(surface_height) * 200).astype(np.uint8)))

    masks = landcover_masks(landcover_resampled)
    for layer_id, color in [
        ("open_terrain_mask", "#d8d85d"),
        ("forested_terrain_mask", "#0b6b3a"),
        ("bare_rock_mask", "#d4c0a1"),
        ("snow_ice_mask", "#e9f4ff"),
    ]:
        write_mask_raster(settings, layer_id, masks[layer_id], dem_profile)
        save_png(layer_preview_path(settings, layer_id), binary_rgba(np.ma.array(masks[layer_id], mask=False), color, 210))

    contours_geojson = write_contours(settings, dem, dem_profile)

    weights = load_weights(settings)
    susceptibility, explanation = terrain_susceptibility(dem, slope, aspect, landcover_resampled, weights)
    susceptibility_tif = write_susceptibility_tif(settings, susceptibility, dem_profile)
    save_png(layer_preview_path(settings, "terrain_susceptibility"), susceptibility_to_rgba(susceptibility))

    layers = [
        layer_base(
            settings,
            "elevation",
            "Elevation",
            "Terrain",
            dem_sources,
            dem_profile,
            [
                {"label": "Low", "color": "#355c45"},
                {"label": "Mid", "color": "#caa36b"},
                {"label": "High", "color": "#e5dfcf"},
            ],
            0.82,
            masked_stats(dem),
            "Color-ramped source DEM preview",
        ),
        layer_base(
            settings,
            "hillshade",
            "Hillshade",
            "Terrain",
            dem_sources,
            dem_profile,
            [{"label": "Shadow", "color": "#111111"}, {"label": "Illuminated", "color": "#ffffff"}],
            0.62,
            masked_stats(shade),
            "315 degree azimuth, 45 degree altitude analytical hillshade",
        ),
        layer_base(
            settings,
            "slope",
            "Slope degrees",
            "Terrain",
            dem_sources,
            dem_profile,
            [
                {"label": "0-25 deg", "color": "#2f8a4c"},
                {"label": "25-35 deg", "color": "#d6c64b"},
                {"label": "35-45 deg", "color": "#dd8f35"},
                {"label": "45+ deg", "color": "#c23b35"},
            ],
            0.72,
            masked_stats(slope),
            "Slope regenerated from selected terrain source",
        ),
        layer_base(
            settings,
            "aspect",
            "Aspect",
            "Terrain",
            dem_sources,
            dem_profile,
            [
                {"label": "0/360 N", "color": "#f25f5c"},
                {"label": "90 E", "color": "#ffe066"},
                {"label": "180 S", "color": "#70c1b3"},
                {"label": "270 W", "color": "#50514f"},
            ],
            0.55,
            masked_stats(aspect),
            "Aspect regenerated from selected terrain source, hue wheel display",
        ),
        layer_base(
            settings,
            "landcover",
            "ESA WorldCover",
            "Static context",
            [paths["landcover"]],
            landcover_profile,
            [
                {"label": details["label"], "color": details["color"]}
                for value, details in ESA_WORLDCOVER.items()
                if value in set(np.asarray(landcover.compressed()).astype(int).tolist())
            ],
            0.68,
            masked_stats(landcover),
            "ESA WorldCover 2021 class-color display using official class codes",
        ),
    ]

    for layer_id, (title, group, _, algorithm) in derivative_specs.items():
        layers.append(
            layer_base(
                settings,
                layer_id,
                title,
                group,
                dem_sources,
                dem_profile,
                [{"label": "Low/negative", "color": "#3f6f8f"}, {"label": "Mid", "color": "#eeeeee"}, {"label": "High/positive", "color": "#d16d3d"}],
                0.55,
                masked_stats(derivatives[layer_id]),
                algorithm,
            )
        )

    for layer_id, title, color, algorithm in [
        ("ridges", "Approximate ridges", "#f6f1d1", "High positive TPI plus moderate/steep terrain"),
        ("gullies", "Approximate gullies", "#42c2ff", "Low negative TPI plus convergent flow context"),
        ("drainage", "Drainage lines", "#168aad", "High D8 flow accumulation cells"),
        ("open_terrain_mask", "Open-terrain mask", "#d8d85d", "ESA WorldCover open/sparse/snow/grass/shrub classes"),
        ("forested_terrain_mask", "Forested-terrain mask", "#0b6b3a", "ESA WorldCover tree-cover class"),
        ("bare_rock_mask", "Bare-rock mask", "#d4c0a1", "ESA WorldCover bare/sparse vegetation class"),
        ("snow_ice_mask", "Snow-and-ice mask", "#e9f4ff", "ESA WorldCover snow and ice class"),
    ]:
        layers.append(
            layer_base(
                settings,
                layer_id,
                title,
                "Derived mask",
                [paths["landcover"]] if "mask" in layer_id else dem_sources,
                dem_profile,
                [{"label": title, "color": color}],
                0.45,
                masked_stats(derivatives[layer_id] if layer_id in derivatives else masks[layer_id]),
                algorithm,
            )
        )

    if surface_height is not None:
        layers.append(
            layer_base(
                settings,
                "surface_height",
                "Surface height",
                "LiDAR DSM minus DEM",
                [*dem_sources, *dsm_sources],
                dem_profile,
                [{"label": "Low", "color": "#1b4965"}, {"label": "Mid", "color": "#bee9e8"}, {"label": "High", "color": "#ffb703"}],
                0.45,
                masked_stats(surface_height),
                "AOI-aligned LiDAR DSM minus selected DEM; represents surface height, not snow depth.",
                surface_warnings,
            )
        )

    layers.append(
        layer_base(
            settings,
            "terrain_susceptibility",
            "Experimental terrain susceptibility",
            "Prototype susceptibility",
            [*dem_sources, paths["landcover"]],
            dem_profile,
            [{"label": item["label"], "color": item["color"]} for item in explanation["classes"]],
            0.78,
            masked_stats(susceptibility),
            "Rules-based weighted static terrain susceptibility, 0-100",
            [DISCLAIMER],
        )
    )
    for layer in layers:
        if layer["id"] == "terrain_susceptibility":
            layer["download_url"] = "/api/download/terrain_susceptibility"
            layer["disclaimer"] = DISCLAIMER

    metadata = {
        "analysis_crs": "EPSG:26911",
        "terrain_source_selected": terrain_source,
        "terrain_source_reason": "BC LiDAR DEM tiles are mosaicked to the 30 m AOI grid when coverage is sufficient; Copernicus is used only as fallback.",
        "available_sources": terrain_metadata(settings)["available_sources"],
        "lidar_dem_tiles": [path.name for path in select_latest_lidar_tiles(paths["lidar_dem_dir"])],
        "lidar_dsm_tiles": [path.name for path in select_latest_lidar_tiles(paths["lidar_dsm_dir"])],
        "contour_feature_count": len(contours_geojson.get("features", [])),
        "warnings": dem_warnings + surface_warnings,
        "disclaimer": DISCLAIMER,
    }
    index = {
        "generated_at_utc": utc_now_iso(),
        "terrain_metadata": metadata,
        "layers": layers,
        "vector_assets": {
            "contours": {
                "url": "/api/terrain/contours",
                "download_url": "/api/download/contours",
                "feature_count": len(contours_geojson.get("features", [])),
                "interval_m": 100,
            }
        },
        "susceptibility": {
            **explanation,
            "output_raster": str(susceptibility_tif.relative_to(settings.project_root).as_posix()),
            "output_sha256": file_sha256(susceptibility_tif),
            "stats": masked_stats(susceptibility),
        },
        "cache": cache_metadata,
    }
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    write_cache_log(settings, "terrain", cache_metadata)
    return index


def get_terrain_layers(settings: Settings) -> dict[str, Any]:
    return generate_terrain_layers(settings, force=False)


def get_layer_metadata(settings: Settings, layer_id: str) -> dict[str, Any]:
    validate_layer_id(layer_id)
    generate_terrain_layers(settings, force=False)
    path = layer_metadata_path(settings, layer_id)
    if not path.exists():
        raise FileNotFoundError(f"Layer metadata missing: {layer_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_layer_preview(settings: Settings, layer_id: str) -> Path:
    validate_layer_id(layer_id)
    generate_terrain_layers(settings, force=False)
    path = layer_preview_path(settings, layer_id)
    if not path.exists():
        raise FileNotFoundError(f"Layer preview missing: {layer_id}")
    return path


def get_download_asset(settings: Settings, asset_id: str) -> Path:
    if asset_id == "contours":
        generate_terrain_layers(settings, force=False)
        path = processed_vector_path(settings, "contours")
        if not path.exists():
            raise FileNotFoundError("Contours GeoJSON is missing")
        return path
    if asset_id not in LAYER_IDS:
        raise KeyError(f"Unknown downloadable asset: {asset_id}")
    generate_terrain_layers(settings, force=False)
    path = processed_raster_path(settings, asset_id)
    if not path.exists():
        raise FileNotFoundError(f"Download asset missing: {asset_id}")
    return path


def get_susceptibility(settings: Settings) -> dict[str, Any]:
    return get_terrain_layers(settings)["susceptibility"]


def get_contours(settings: Settings) -> dict[str, Any]:
    generate_terrain_layers(settings, force=False)
    path = processed_vector_path(settings, "contours")
    if not path.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text(encoding="utf-8"))


def categorize_osm_feature(properties: dict[str, Any], geometry_type: str | None) -> str:
    if properties.get("building"):
        return "buildings"
    if properties.get("railway"):
        return "railways"
    if properties.get("aerialway"):
        return "aerial_lifts"
    if properties.get("power") in {"line", "minor_line", "tower", "pole"}:
        return "power"
    if properties.get("waterway") or properties.get("natural") == "water":
        return "waterways"
    highway = properties.get("highway")
    if highway in {"path", "footway", "track", "cycleway", "bridleway"}:
        return "trails"
    if highway:
        return "roads"
    if geometry_type == "Point":
        return "other_points"
    return "other"


def get_osm_infrastructure(settings: Settings) -> dict[str, Any]:
    path = terrain_paths(settings)["osm"]
    if not path.exists():
        return {"type": "FeatureCollection", "features": [], "categories": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    categories: dict[str, int] = {}
    features = []
    for feature in data.get("features", []):
        props = dict(feature.get("properties") or {})
        geometry_type = (feature.get("geometry") or {}).get("type")
        category = categorize_osm_feature(props, geometry_type)
        categories[category] = categories.get(category, 0) + 1
        props["dt_category"] = category
        feature["properties"] = props
        features.append(feature)
    return {
        "type": "FeatureCollection",
        "features": features,
        "categories": categories,
        "source": relative_source_path(settings.data_root, path),
    }
