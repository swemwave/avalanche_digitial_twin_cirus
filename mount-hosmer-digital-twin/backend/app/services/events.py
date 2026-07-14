from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.core.paths import ensure_runtime_dirs, relative_source_path
from app.core.settings import Settings
from app.services.cache import cache_matches, source_fingerprint, write_cache_log
from app.services.terrain import DISCLAIMER, file_sha256

try:
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_bounds, transform_geom
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]


EVENT_ID_RE = re.compile(r"^MH_\d{8}T\d{6}Z$")
LAYER_ID_RE = re.compile(r"^[a-z0-9_]+$")

SENTINEL_KEYS = {
    "B02": "Blue",
    "B03": "Green",
    "B04": "Red",
    "B08": "Near infrared",
    "B11": "Shortwave infrared 1",
    "B12": "Shortwave infrared 2",
    "SCL": "Scene classification",
    "NDVI": "NDVI",
    "NDSI": "NDSI",
    "NDMI": "NDMI",
}

LANDSAT_KEYS = {
    "blue": "Blue",
    "green": "Green",
    "red": "Red",
    "nir08": "Near infrared",
    "swir16": "Shortwave infrared 1",
    "swir22": "Shortwave infrared 2",
    "lwir11": "Thermal infrared",
    "qa_pixel": "Pixel quality",
    "NDVI": "NDVI",
    "NDSI": "NDSI",
    "NDMI": "NDMI",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_root(settings: Settings) -> Path:
    return settings.data_root / "events"


def event_output_dir(settings: Settings, event_id: str) -> Path:
    return settings.runtime_root / "processed" / "events" / event_id


def event_preview_dir(settings: Settings, event_id: str) -> Path:
    return settings.runtime_root / "previews" / "events" / event_id


def event_summary_path(settings: Settings, event_id: str) -> Path:
    return event_output_dir(settings, event_id) / "event_summary.json"


def event_layer_metadata_path(settings: Settings, event_id: str, layer_id: str) -> Path:
    return event_output_dir(settings, event_id) / f"{layer_id}.metadata.json"


def event_preview_path(settings: Settings, event_id: str, layer_id: str) -> Path:
    return event_preview_dir(settings, event_id) / f"{layer_id}.png"


def save_png(path: Path, rgba: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path)


def event_date_label(event_id: str) -> str:
    try:
        return datetime.strptime(event_id.removeprefix("MH_"), "%Y%m%dT%H%M%SZ").date().isoformat()
    except ValueError:
        return event_id.removeprefix("MH_")


def discover_event_dirs(settings: Settings) -> list[Path]:
    root = event_root(settings)
    if not root.exists():
        return []
    return sorted(path for path in root.glob("MH_*") if path.is_dir() and EVENT_ID_RE.match(path.name))


def validate_event_id(settings: Settings, event_id: str) -> str:
    if not EVENT_ID_RE.match(event_id):
        raise KeyError(f"Unknown event id: {event_id}")
    allowed = {path.name for path in discover_event_dirs(settings)}
    if event_id not in allowed:
        raise KeyError(f"Unknown event id: {event_id}")
    return event_id


def validate_layer_id(layer_id: str) -> str:
    if not LAYER_ID_RE.match(layer_id):
        raise KeyError(f"Unknown event layer id: {layer_id}")
    return layer_id


def read_json(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        return {}, [f"Missing event metadata file: {path.name}"]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as exc:
        return {}, [f"Unreadable JSON metadata: {path.name}: {exc}"]


def discover_event_files(event_dir: Path) -> dict[str, dict[str, Path]]:
    files: dict[str, dict[str, Path]] = {"sentinel2": {}, "landsat": {}}
    sentinel_dir = event_dir / "sentinel2"
    if sentinel_dir.exists():
        for path in sorted(sentinel_dir.glob("*.tif")):
            name = path.name.upper()
            for key in SENTINEL_KEYS:
                if f"_{key}_" in name:
                    files["sentinel2"][key] = path
                    break
    landsat_dir = event_dir / "landsat"
    if landsat_dir.exists():
        for path in sorted(landsat_dir.glob("*.tif")):
            lower = path.name.lower()
            for key in LANDSAT_KEYS:
                if f"_{key.lower()}_" in lower:
                    files["landsat"][key] = path
                    break
    return files


def raster_coordinates(profile: dict[str, Any]) -> tuple[list[list[float]], list[float]]:
    bounds = profile["bounds"]
    crs = profile["crs"]
    west, south, east, north = transform_bounds(crs, "EPSG:4326", *bounds, densify_pts=21)
    return [[west, north], [east, north], [east, south], [west, south]], [west, south, east, north]


def read_raster(path: Path) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    if rasterio is None:
        raise RuntimeError("rasterio is required for event processing")
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True).astype("float32")
        profile = src.profile.copy()
        profile["bounds"] = src.bounds
        profile["transform"] = src.transform
        profile["crs"] = src.crs
        profile["resolution"] = src.res
        profile["width"] = src.width
        profile["height"] = src.height
        profile["nodata"] = src.nodata
        return arr, profile


def aoi_inside_mask(settings: Settings, profile: dict[str, Any]) -> tuple[np.ndarray, list[str]]:
    if rasterio is None:
        return np.ones((profile["height"], profile["width"]), dtype=bool), ["rasterio unavailable; AOI mask skipped"]
    aoi_path = settings.data_root / "metadata" / "mount_hosmer_aoi.geojson"
    if not aoi_path.exists():
        return np.ones((profile["height"], profile["width"]), dtype=bool), ["AOI GeoJSON missing; using full raster extent"]
    try:
        data = json.loads(aoi_path.read_text(encoding="utf-8"))
        geometries = [feature.get("geometry") for feature in data.get("features", []) if feature.get("geometry")]
        if not geometries:
            return np.ones((profile["height"], profile["width"]), dtype=bool), ["AOI GeoJSON has no geometries; using full raster extent"]
        target_crs = str(profile["crs"])
        transformed = [transform_geom("EPSG:4326", target_crs, geometry) for geometry in geometries]
        mask = geometry_mask(
            transformed,
            out_shape=(profile["height"], profile["width"]),
            transform=profile["transform"],
            invert=True,
        )
        return mask.astype(bool), []
    except Exception as exc:
        return np.ones((profile["height"], profile["width"]), dtype=bool), [f"AOI masking failed; using full raster extent: {exc}"]


def valid_pixels(arr: np.ma.MaskedArray, inside: np.ndarray | None = None) -> np.ndarray:
    data = np.asarray(np.ma.getdata(arr), dtype="float32")
    mask = ~np.ma.getmaskarray(arr) & np.isfinite(data)
    if inside is not None:
        mask &= inside
    return mask


def masked_from_valid(values: np.ndarray, valid: np.ndarray) -> np.ma.MaskedArray:
    return np.ma.array(values.astype("float32"), mask=~valid)


def summary_stats(arr: np.ma.MaskedArray | np.ndarray, inside: np.ndarray | None = None) -> dict[str, float | int | None]:
    masked = np.ma.asarray(arr)
    if inside is not None:
        masked = np.ma.array(masked, mask=np.ma.getmaskarray(masked) | ~inside)
    values = masked.compressed() if np.ma.is_masked(masked) else np.asarray(masked).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return {"min": None, "max": None, "mean": None, "median": None, "valid_pixels": 0}
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(np.median(values)),
        "valid_pixels": int(values.size),
    }


def percent(numerator: int | float, denominator: int | float) -> float | None:
    if denominator <= 0:
        return None
    return round(float(numerator) / float(denominator) * 100.0, 3)


def normalize_channel(arr: np.ma.MaskedArray, valid: np.ndarray) -> np.ndarray:
    values = np.asarray(arr.filled(np.nan), dtype="float32")
    sample = values[valid & np.isfinite(values)]
    if sample.size == 0:
        return np.zeros(values.shape, dtype="float32")
    low = float(np.percentile(sample, 2))
    high = float(np.percentile(sample, 98))
    if high <= low:
        high = low + 1.0
    return np.nan_to_num(np.clip((values - low) / (high - low), 0, 1), nan=0.0, posinf=1.0, neginf=0.0)


def composite_rgba(
    red: np.ma.MaskedArray,
    green: np.ma.MaskedArray,
    blue: np.ma.MaskedArray,
    inside: np.ndarray,
    alpha_value: int = 235,
) -> tuple[np.ndarray, np.ndarray]:
    valid = valid_pixels(red, inside) & valid_pixels(green, inside) & valid_pixels(blue, inside)
    rgba = np.zeros((*red.shape, 4), dtype=np.uint8)
    rgba[..., 0] = (normalize_channel(red, valid) * 255).astype(np.uint8)
    rgba[..., 1] = (normalize_channel(green, valid) * 255).astype(np.uint8)
    rgba[..., 2] = (normalize_channel(blue, valid) * 255).astype(np.uint8)
    rgba[..., 3] = (valid.astype(np.uint8) * alpha_value)
    return rgba, valid


def ramp(values: np.ndarray, colors: list[str], valid: np.ndarray, alpha_value: int = 220) -> np.ndarray:
    values = np.nan_to_num(np.clip(values, 0, 1), nan=0.0, posinf=1.0, neginf=0.0)
    stops = np.linspace(0, 1, len(colors))
    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    channels = np.array(
        [[int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)] for color in colors],
        dtype="float32",
    )
    for channel in range(3):
        rgba[..., channel] = np.interp(values, stops, channels[:, channel]).astype(np.uint8)
    rgba[..., 3] = valid.astype(np.uint8) * alpha_value
    return rgba


def index_rgba(index: np.ma.MaskedArray, inside: np.ndarray, colors: list[str]) -> tuple[np.ndarray, np.ndarray]:
    valid = valid_pixels(index, inside)
    values = np.asarray(index.filled(np.nan), dtype="float32")
    display = (np.clip(values, -1, 1) + 1.0) / 2.0
    return ramp(display, colors, valid, 225), valid


def thermal_rgba(kelvin: np.ma.MaskedArray, inside: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ma.MaskedArray]:
    valid = valid_pixels(kelvin, inside)
    celsius_values = np.asarray(kelvin.filled(np.nan), dtype="float32") - 273.15
    sample = celsius_values[valid & np.isfinite(celsius_values)]
    if sample.size == 0:
        norm = np.zeros(celsius_values.shape, dtype="float32")
    else:
        low = float(np.percentile(sample, 2))
        high = float(np.percentile(sample, 98))
        if high <= low:
            high = low + 1.0
        norm = np.clip((celsius_values - low) / (high - low), 0, 1)
    return ramp(norm, ["#2f5f9e", "#e8e6c9", "#cf533f"], valid, 225), valid, masked_from_valid(celsius_values, valid)


def mask_rgba(mask: np.ndarray, color: str, alpha_value: int = 215) -> np.ndarray:
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    rgba[..., 0] = int(color[1:3], 16)
    rgba[..., 1] = int(color[3:5], 16)
    rgba[..., 2] = int(color[5:7], 16)
    rgba[..., 3] = mask.astype(np.uint8) * alpha_value
    return rgba


def normalized_difference(
    first: np.ma.MaskedArray,
    second: np.ma.MaskedArray,
    inside: np.ndarray,
    denominator_floor: float = 0.01,
) -> tuple[np.ma.MaskedArray, list[str]]:
    a = np.asarray(first.filled(np.nan), dtype="float32")
    b = np.asarray(second.filled(np.nan), dtype="float32")
    denominator = a + b
    valid = valid_pixels(first, inside) & valid_pixels(second, inside) & (np.abs(denominator) > denominator_floor)
    values = np.divide(a - b, denominator, out=np.full(a.shape, np.nan, dtype="float32"), where=valid)
    clipped = np.clip(values, -1, 1)
    warnings: list[str] = []
    dropped = int((valid_pixels(first, inside) & valid_pixels(second, inside) & ~valid).sum())
    if dropped:
        warnings.append(f"{dropped} pixels had near-zero denominator and were masked before index calculation.")
    return masked_from_valid(clipped, valid), warnings


def sentinel_scl_masks(scl: np.ma.MaskedArray, inside: np.ndarray) -> dict[str, np.ndarray | float | None]:
    values = np.asarray(scl.filled(-9999), dtype="int16")
    available = valid_pixels(scl, inside)
    cloud = available & np.isin(values, [8, 9, 10])
    shadow = available & (values == 3)
    snow = available & (values == 11)
    valid = available & ~np.isin(values, [0, 1, 3, 8, 9, 10])
    available_count = int(available.sum())
    return {
        "cloud": cloud,
        "shadow": shadow,
        "snow": snow,
        "valid": valid,
        "available_count": available_count,
        "cloud_percent": percent(int(cloud.sum()), available_count),
        "valid_percent": percent(int(valid.sum()), available_count),
        "snow_percent": percent(int(snow.sum()), int(valid.sum())),
    }


def landsat_qa_masks(qa: np.ma.MaskedArray, inside: np.ndarray) -> dict[str, np.ndarray | float | None]:
    values = np.asarray(qa.filled(0), dtype="uint32")
    available = valid_pixels(qa, inside)
    fill = available & ((values & (1 << 0)) > 0)
    dilated = available & ((values & (1 << 1)) > 0)
    cirrus = available & ((values & (1 << 2)) > 0)
    cloud = available & ((values & (1 << 3)) > 0)
    shadow = available & ((values & (1 << 4)) > 0)
    snow = available & ((values & (1 << 5)) > 0)
    cloud_any = dilated | cirrus | cloud
    valid = available & ~fill & ~cloud_any & ~shadow
    available_count = int(available.sum())
    return {
        "cloud": cloud_any,
        "shadow": shadow,
        "snow": snow,
        "valid": valid,
        "available_count": available_count,
        "cloud_percent": percent(int(cloud_any.sum()), available_count),
        "valid_percent": percent(int(valid.sum()), available_count),
        "snow_percent": percent(int(snow.sum()), int(valid.sum())),
    }


def layer_record(
    settings: Settings,
    event_id: str,
    layer_id: str,
    title: str,
    sensor: str,
    profile: dict[str, Any],
    source_files: list[Path],
    legend: list[dict[str, str]],
    opacity: float,
    stats: dict[str, Any],
    algorithm: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    coordinates, bounds_wgs84 = raster_coordinates(profile)
    record = {
        "id": layer_id,
        "title": title,
        "sensor": sensor,
        "kind": "raster_overlay",
        "preview_url": f"/api/events/{event_id}/layers/{layer_id}/preview",
        "metadata_url": f"/api/events/{event_id}/layers/{layer_id}/metadata",
        "coordinates": coordinates,
        "bounds_wgs84": bounds_wgs84,
        "opacity": opacity,
        "legend": legend,
        "stats": stats,
        "source_files": [relative_source_path(settings.data_root, path) for path in source_files if path.exists()],
        "processing": {
            "generated_at_utc": utc_now_iso(),
            "algorithm": algorithm,
            "crs": str(profile["crs"]),
            "resolution": list(profile["resolution"]),
            "bounds": list(profile["bounds"]),
        },
        "warnings": warnings or [],
    }
    event_layer_metadata_path(settings, event_id, layer_id).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def process_event(settings: Settings, event_id: str, force: bool = False) -> dict[str, Any]:
    event_id = validate_event_id(settings, event_id)
    ensure_runtime_dirs(settings.runtime_root)
    event_output_dir(settings, event_id).mkdir(parents=True, exist_ok=True)
    event_preview_dir(settings, event_id).mkdir(parents=True, exist_ok=True)
    summary_path = event_summary_path(settings, event_id)
    event_dir = event_root(settings) / event_id
    files = discover_event_files(event_dir)
    source_files = [
        event_dir / "event_metadata.json",
        *[path for sensor_files in files.values() for path in sensor_files.values()],
    ]
    cache_metadata = source_fingerprint(
        settings,
        source_files,
        parameters={
            "processor": "event",
            "event_id": event_id,
            "analysis_crs": "EPSG:26911",
        },
    )
    if summary_path.exists() and not force:
        cached = json.loads(summary_path.read_text(encoding="utf-8"))
        if cache_matches(cached.get("cache", {}), cache_metadata) and all(
            event_preview_path(settings, event_id, layer["id"]).exists()
            for layer in cached.get("layers", [])
        ):
            return cached

    metadata, warnings = read_json(event_dir / "event_metadata.json")
    cache: dict[Path, tuple[np.ma.MaskedArray, dict[str, Any], np.ndarray]] = {}
    layers: list[dict[str, Any]] = []
    sensor_summary: dict[str, Any] = {
        "sentinel2": {
            "available": bool(files["sentinel2"]),
            "available_layers": sorted(files["sentinel2"]),
            "metadata_cloud_percent": metadata.get("sentinel_cloud_percent"),
        },
        "landsat": {
            "available": bool(files["landsat"]),
            "available_layers": sorted(files["landsat"]),
            "metadata_cloud_percent": metadata.get("landsat_cloud_percent"),
        },
    }

    def load(path: Path) -> tuple[np.ma.MaskedArray, dict[str, Any], np.ndarray]:
        if path not in cache:
            arr, profile = read_raster(path)
            inside, mask_warnings = aoi_inside_mask(settings, profile)
            warnings.extend(mask_warnings)
            cache[path] = (arr, profile, inside)
        return cache[path]

    def add_composite(
        sensor: str,
        layer_id: str,
        title: str,
        band_keys: tuple[str, str, str],
        legend: list[dict[str, str]],
        algorithm: str,
        opacity: float = 0.78,
    ) -> None:
        source = files[sensor]
        missing = [key for key in band_keys if key not in source]
        if missing:
            warnings.append(f"{event_id}: {title} skipped; missing {', '.join(missing)}.")
            return
        red, profile, inside = load(source[band_keys[0]])
        green, _, _ = load(source[band_keys[1]])
        blue, _, _ = load(source[band_keys[2]])
        rgba, valid = composite_rgba(red, green, blue, inside)
        save_png(event_preview_path(settings, event_id, layer_id), rgba)
        layers.append(
            layer_record(
                settings,
                event_id,
                layer_id,
                title,
                "Sentinel-2" if sensor == "sentinel2" else "Landsat",
                profile,
                [source[band_keys[0]], source[band_keys[1]], source[band_keys[2]]],
                legend,
                opacity,
                {"valid_pixels": int(valid.sum()), "min": None, "max": None, "mean": None, "median": None},
                algorithm,
            )
        )

    add_composite(
        "sentinel2",
        "s2_true_color",
        "Sentinel-2 true colour",
        ("B04", "B03", "B02"),
        [{"label": "Natural colour composite", "color": "#d8e1d0"}],
        "Percentile-stretched B04/B03/B02 RGB composite",
    )
    add_composite(
        "sentinel2",
        "s2_false_vegetation",
        "Sentinel-2 false-colour vegetation",
        ("B08", "B04", "B03"),
        [{"label": "Vegetation signal", "color": "#d35f5f"}],
        "Percentile-stretched B08/B04/B03 RGB composite",
        0.7,
    )
    add_composite(
        "sentinel2",
        "s2_snow_moisture",
        "Sentinel-2 snow/moisture composite",
        ("B12", "B11", "B08"),
        [{"label": "SWIR/NIR snow and moisture contrast", "color": "#7ab7d8"}],
        "Percentile-stretched B12/B11/B08 RGB composite",
        0.72,
    )
    add_composite(
        "landsat",
        "landsat_true_color",
        "Landsat true colour",
        ("red", "green", "blue"),
        [{"label": "Natural colour composite", "color": "#d8e1d0"}],
        "Percentile-stretched red/green/blue RGB composite",
    )
    add_composite(
        "landsat",
        "landsat_false_color",
        "Landsat false colour",
        ("nir08", "red", "green"),
        [{"label": "Vegetation/snow contrast", "color": "#d35f5f"}],
        "Percentile-stretched NIR/red/green RGB composite",
        0.7,
    )

    def add_index_file(
        sensor: str,
        key: str,
        layer_id: str,
        title: str,
        colors: list[str],
        source_label: str,
    ) -> None:
        source = files[sensor]
        if key not in source:
            warnings.append(f"{event_id}: {title} skipped; missing {key}.")
            return
        index, profile, inside = load(source[key])
        rgba, _ = index_rgba(index, inside, colors)
        save_png(event_preview_path(settings, event_id, layer_id), rgba)
        stats = summary_stats(index, inside)
        if stats["min"] is not None and (float(stats["min"]) < -1.5 or float(stats["max"]) > 1.5):
            warnings.append(f"{event_id}: {title} has values outside the normal -1 to 1 index range; display is clipped.")
        layers.append(
            layer_record(
                settings,
                event_id,
                layer_id,
                title,
                "Sentinel-2" if sensor == "sentinel2" else "Landsat",
                profile,
                [source[key]],
                [{"label": "-1 low", "color": colors[0]}, {"label": "0 neutral", "color": colors[1]}, {"label": "+1 high", "color": colors[-1]}],
                0.72,
                stats,
                source_label,
            )
        )
        sensor_summary[sensor].setdefault("indices", {})[key.lower()] = stats

    for key, title, colors in [
        ("NDVI", "Sentinel-2 NDVI", ["#7b362c", "#d7d4a2", "#287947"]),
        ("NDSI", "Sentinel-2 NDSI", ["#51406f", "#b7c7c9", "#f4fbff"]),
        ("NDMI", "Sentinel-2 NDMI", ["#7a4b2d", "#d8cf9d", "#2d7191"]),
    ]:
        add_index_file("sentinel2", key, f"s2_{key.lower()}", title, colors, f"Source {key} raster, clipped to -1 to 1 for display")

    def add_landsat_index(
        layer_id: str,
        title: str,
        first_key: str,
        second_key: str,
        colors: list[str],
        index_key: str,
    ) -> None:
        source = files["landsat"]
        missing = [key for key in [first_key, second_key] if key not in source]
        if missing:
            warnings.append(f"{event_id}: {title} skipped; missing {', '.join(missing)}.")
            return
        first, profile, inside = load(source[first_key])
        second, _, _ = load(source[second_key])
        index, index_warnings = normalized_difference(first, second, inside)
        warnings.extend([f"{event_id}: {title}: {warning}" for warning in index_warnings])
        if index_key in source:
            raw_index, _, _ = load(source[index_key])
            raw_stats = summary_stats(raw_index, inside)
            if raw_stats["min"] is not None and (float(raw_stats["min"]) < -1.5 or float(raw_stats["max"]) > 1.5):
                warnings.append(
                    f"{event_id}: source Landsat {index_key} raster contains out-of-range values; "
                    "preview and summary were recomputed from scaled reflectance bands."
                )
        rgba, _ = index_rgba(index, inside, colors)
        save_png(event_preview_path(settings, event_id, layer_id), rgba)
        stats = summary_stats(index)
        source_files = [source[first_key], source[second_key]]
        if index_key in source:
            source_files.append(source[index_key])
        layers.append(
            layer_record(
                settings,
                event_id,
                layer_id,
                title,
                "Landsat",
                profile,
                source_files,
                [{"label": "-1 low", "color": colors[0]}, {"label": "0 neutral", "color": colors[1]}, {"label": "+1 high", "color": colors[-1]}],
                0.72,
                stats,
                f"Computed ({first_key} - {second_key}) / ({first_key} + {second_key}) from scaled reflectance bands",
            )
        )
        sensor_summary["landsat"].setdefault("indices", {})[index_key.lower()] = stats

    add_landsat_index("landsat_ndvi", "Landsat NDVI", "nir08", "red", ["#7b362c", "#d7d4a2", "#287947"], "NDVI")
    add_landsat_index("landsat_ndsi", "Landsat NDSI", "green", "swir16", ["#51406f", "#b7c7c9", "#f4fbff"], "NDSI")
    add_landsat_index("landsat_ndmi", "Landsat NDMI", "nir08", "swir16", ["#7a4b2d", "#d8cf9d", "#2d7191"], "NDMI")

    if "lwir11" in files["landsat"]:
        thermal, profile, inside = load(files["landsat"]["lwir11"])
        rgba, valid, celsius = thermal_rgba(thermal, inside)
        save_png(event_preview_path(settings, event_id, "landsat_surface_temperature"), rgba)
        stats = summary_stats(celsius)
        layers.append(
            layer_record(
                settings,
                event_id,
                "landsat_surface_temperature",
                "Landsat surface temperature",
                "Landsat",
                profile,
                [files["landsat"]["lwir11"]],
                [{"label": "Cold", "color": "#2f5f9e"}, {"label": "Warm", "color": "#cf533f"}],
                0.7,
                stats,
                "Thermal raster displayed as degrees Celsius using percentile stretch",
            )
        )
        sensor_summary["landsat"]["surface_temperature_c"] = stats
    else:
        warnings.append(f"{event_id}: Landsat surface-temperature display skipped; missing lwir11.")

    if "SCL" in files["sentinel2"]:
        scl, profile, inside = load(files["sentinel2"]["SCL"])
        masks = sentinel_scl_masks(scl, inside)
        sensor_summary["sentinel2"].update(
            {
                "cloud_percent_calculated": masks["cloud_percent"],
                "valid_pixel_percent": masks["valid_percent"],
                "snow_cover_percent": masks["snow_percent"],
            }
        )
        for layer_id, title, key, color in [
            ("s2_cloud_mask", "Sentinel-2 cloud mask", "cloud", "#f0f4ff"),
            ("s2_cloud_shadow_mask", "Sentinel-2 cloud-shadow mask", "shadow", "#363f55"),
            ("s2_valid_data_mask", "Sentinel-2 valid-data mask", "valid", "#7cc78b"),
            ("s2_snow_class_mask", "Sentinel-2 snow-class mask", "snow", "#e8f7ff"),
        ]:
            mask = masks[key]
            if isinstance(mask, np.ndarray):
                save_png(event_preview_path(settings, event_id, layer_id), mask_rgba(mask, color))
                layers.append(
                    layer_record(
                        settings,
                        event_id,
                        layer_id,
                        title,
                        "Sentinel-2",
                        profile,
                        [files["sentinel2"]["SCL"]],
                        [{"label": title, "color": color}],
                        0.65,
                        {"min": 0, "max": 1, "mean": float(mask.mean()), "median": None, "valid_pixels": int(mask.sum())},
                        "Derived from Sentinel-2 Scene Classification Layer classes",
                    )
                )
    else:
        warnings.append(f"{event_id}: Sentinel-2 cloud/valid/snow masks skipped; missing SCL.")

    if "qa_pixel" in files["landsat"]:
        qa, profile, inside = load(files["landsat"]["qa_pixel"])
        masks = landsat_qa_masks(qa, inside)
        sensor_summary["landsat"].update(
            {
                "cloud_percent_calculated": masks["cloud_percent"],
                "valid_pixel_percent": masks["valid_percent"],
                "snow_cover_percent": masks["snow_percent"],
            }
        )
        for layer_id, title, key, color in [
            ("landsat_cloud_mask", "Landsat cloud mask", "cloud", "#f0f4ff"),
            ("landsat_valid_data_mask", "Landsat valid-data mask", "valid", "#7cc78b"),
            ("landsat_snow_mask", "Landsat snow-quality mask", "snow", "#e8f7ff"),
        ]:
            mask = masks[key]
            if isinstance(mask, np.ndarray):
                save_png(event_preview_path(settings, event_id, layer_id), mask_rgba(mask, color))
                layers.append(
                    layer_record(
                        settings,
                        event_id,
                        layer_id,
                        title,
                        "Landsat",
                        profile,
                        [files["landsat"]["qa_pixel"]],
                        [{"label": title, "color": color}],
                        0.65,
                        {"min": 0, "max": 1, "mean": float(mask.mean()), "median": None, "valid_pixels": int(mask.sum())},
                        "Derived from Landsat QA_PIXEL bit flags",
                    )
                )
    else:
        warnings.append(f"{event_id}: Landsat cloud/valid/snow masks skipped; missing qa_pixel.")

    payload = {
        "schema_version": 1,
        "event_id": event_id,
        "date_label": event_date_label(event_id),
        "generated_at_utc": utc_now_iso(),
        "metadata": metadata,
        "source_files": {
            sensor: {key: relative_source_path(settings.data_root, path) for key, path in sensor_files.items()}
            for sensor, sensor_files in files.items()
        },
        "summary": {
            "landsat_datetime_utc": metadata.get("landsat_datetime_utc"),
            "sentinel_datetime_utc": metadata.get("sentinel_datetime_utc"),
            "time_difference_hours": metadata.get("time_difference_hours"),
            "sensors": sensor_summary,
        },
        "layers": layers,
        "warnings": sorted(set(warnings)),
        "disclaimer": DISCLAIMER,
        "processing": {
            "source_event_folder": relative_source_path(settings.data_root, event_dir),
            "application_version": settings.app_version,
            "event_summary_sha256": None,
        },
        "cache": cache_metadata,
    }
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["processing"]["event_summary_sha256"] = file_sha256(summary_path)
    summary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_cache_log(settings, f"event_{event_id}", cache_metadata)
    return payload


def process_all_events(settings: Settings, force: bool = False) -> dict[str, Any]:
    results = []
    errors = []
    for path in discover_event_dirs(settings):
        try:
            result = process_event(settings, path.name, force=force)
            results.append({"event_id": path.name, "layer_count": len(result.get("layers", [])), "warning_count": len(result.get("warnings", []))})
        except Exception as exc:
            errors.append({"event_id": path.name, "error": str(exc)})
    return {"processed": results, "errors": errors, "count": len(results)}


def list_events(settings: Settings) -> dict[str, Any]:
    events = []
    for path in discover_event_dirs(settings):
        metadata, warnings = read_json(path / "event_metadata.json")
        files = discover_event_files(path)
        summary = event_summary_path(settings, path.name)
        events.append(
            {
                "event_id": path.name,
                "date_label": event_date_label(path.name),
                "metadata": metadata,
                "available_sensors": [sensor for sensor, sensor_files in files.items() if sensor_files],
                "available_layers": {sensor: sorted(sensor_files) for sensor, sensor_files in files.items()},
                "processed": summary.exists(),
                "summary_url": f"/api/events/{path.name}",
                "warnings": warnings,
            }
        )
    return {"events": events, "count": len(events)}


def get_event(settings: Settings, event_id: str) -> dict[str, Any]:
    return process_event(settings, event_id, force=False)


def get_event_layer_metadata(settings: Settings, event_id: str, layer_id: str) -> dict[str, Any]:
    event_id = validate_event_id(settings, event_id)
    layer_id = validate_layer_id(layer_id)
    event = process_event(settings, event_id, force=False)
    allowed = {layer["id"] for layer in event.get("layers", [])}
    if layer_id not in allowed:
        raise KeyError(f"Unknown event layer id: {layer_id}")
    path = event_layer_metadata_path(settings, event_id, layer_id)
    if not path.exists():
        raise FileNotFoundError(f"Event layer metadata missing: {event_id}/{layer_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_event_layer_preview(settings: Settings, event_id: str, layer_id: str) -> Path:
    event_id = validate_event_id(settings, event_id)
    layer_id = validate_layer_id(layer_id)
    event = process_event(settings, event_id, force=False)
    allowed = {layer["id"] for layer in event.get("layers", [])}
    if layer_id not in allowed:
        raise KeyError(f"Unknown event layer id: {layer_id}")
    path = event_preview_path(settings, event_id, layer_id)
    if not path.exists():
        raise FileNotFoundError(f"Event layer preview missing: {event_id}/{layer_id}")
    return path


def get_event_susceptibility(settings: Settings, event_id: str) -> dict[str, Any]:
    event = process_event(settings, event_id, force=False)
    sensors = event.get("summary", {}).get("sensors", {})
    components = []
    for sensor_name, sensor in sensors.items():
        for key in ["cloud_percent_calculated", "valid_pixel_percent", "snow_cover_percent"]:
            value = sensor.get(key)
            components.append(
                {
                    "source": sensor_name,
                    "timestamp": event.get("metadata", {}).get(f"{'sentinel' if sensor_name == 'sentinel2' else 'landsat'}_datetime_utc"),
                    "component": key,
                    "units": "percent",
                    "original_value": value,
                    "normalized_value": None,
                    "weight": None,
                    "missing_data": value is None,
                }
            )
    return {
        "event_id": event["event_id"],
        "model_type": "dynamic condition index placeholder",
        "dynamic_condition_index": None,
        "combined_index": None,
        "components": components,
        "warnings": [
            "Dynamic and combined susceptibility scoring are scheduled for Milestone 5; missing values are not interpreted as safe conditions."
        ],
        "disclaimer": DISCLAIMER,
    }
