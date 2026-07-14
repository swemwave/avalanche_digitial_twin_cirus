from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.core.paths import ensure_runtime_dirs
from app.core.settings import Settings
from app.services.cache import cache_matches, source_fingerprint, write_cache_log
from app.services.conditions import (
    forecast_summary_path,
    get_avalanche_forecast,
    get_snow_summary,
    get_weather_summary,
    snow_summary_path,
    weather_summary_path,
)
from app.services.events import (
    discover_event_dirs,
    event_output_dir,
    event_preview_dir,
    event_preview_path,
    event_summary_path,
    get_event,
    validate_event_id,
)
from app.services.terrain import (
    DISCLAIMER,
    file_sha256,
    generate_terrain_layers,
    load_weights,
    masked_stats,
    processed_raster_path,
    raster_coordinates,
    read_raster,
    susceptibility_to_rgba,
)

try:
    import rasterio
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def susceptibility_summary_path(settings: Settings, event_id: str) -> Path:
    return event_output_dir(settings, event_id) / "susceptibility_summary.json"


def combined_raster_path(settings: Settings, event_id: str) -> Path:
    return event_output_dir(settings, event_id) / "combined_susceptibility.tif"


def combined_metadata_path(settings: Settings, event_id: str) -> Path:
    return event_output_dir(settings, event_id) / "combined_susceptibility.metadata.json"


def combined_preview_path(settings: Settings, event_id: str) -> Path:
    return event_preview_dir(settings, event_id) / "combined_susceptibility.png"


def config_hash(settings: Settings) -> str | None:
    path = settings.backend_root / "config" / "susceptibility_weights.yaml"
    return file_sha256(path) if path.exists() else None


def first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and np.isfinite(value):
            return float(value)
    return None


def interp_score(value: float | None, xs: list[float], ys: list[float], clip_low: bool = True) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    score = float(np.interp(value, xs, ys))
    if clip_low:
        return float(np.clip(score, 0, 100))
    return score


def positive_value(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, float(value))


def best_window(windows: dict[str, Any], preferred: str = "previous_72_hours") -> dict[str, Any]:
    if preferred in windows:
        return windows[preferred] or {}
    for key in ["previous_48_hours", "previous_24_hours", "previous_168_hours"]:
        if key in windows:
            return windows[key] or {}
    return {}


def max_station_window_value(snow_event: dict[str, Any], key: str, window_name: str = "previous_72_hours") -> float | None:
    values: list[float] = []
    for station in (snow_event.get("stations") or {}).values():
        window = station.get(window_name) or best_window(station)
        value = first_number((window or {}).get(key))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def average_available(values: list[float | None]) -> float | None:
    available = [float(value) for value in values if value is not None and np.isfinite(value)]
    return float(np.mean(available)) if available else None


def make_component(
    *,
    component: str,
    source: str,
    timestamp: str | None,
    units: str,
    original_value: float | str | None,
    normalized_value: float | None,
    weight: float,
    reason: str,
    status: str | None = None,
) -> dict[str, Any]:
    missing = normalized_value is None
    return {
        "component": component,
        "source": source,
        "timestamp": timestamp,
        "units": units,
        "original_value": original_value,
        "normalized_value": round(float(normalized_value), 3) if normalized_value is not None else None,
        "weight": weight,
        "weighted_value": round(float(normalized_value) * weight, 3) if normalized_value is not None else None,
        "missing_data": missing,
        "status": status or ("missing" if missing else "available"),
        "reason": reason,
    }


def dynamic_weights(weights: dict[str, Any]) -> dict[str, float]:
    config = weights.get("dynamic", {})
    return {
        "recent_snowfall": float(config.get("recent_snowfall_weight", 0.18)),
        "recent_precipitation": float(config.get("recent_precipitation_weight", 0.12)),
        "swe_change": float(config.get("swe_change_weight", 0.14)),
        "snow_depth_change": float(config.get("snow_depth_change_weight", 0.10)),
        "rapid_warming": float(config.get("rapid_warming_weight", 0.14)),
        "strong_wind": float(config.get("strong_wind_weight", 0.14)),
        "satellite_snow_cover": float(config.get("satellite_snow_cover_weight", 0.12)),
        "surface_temperature": float(config.get("surface_temperature_weight", 0.06)),
    }


def minimum_available_weight(weights: dict[str, Any]) -> float:
    return float(weights.get("dynamic", {}).get("minimum_available_weight", 0.25))


def avalanche_forecast_component(forecast: dict[str, Any], event_time: str | None) -> dict[str, Any]:
    display = ((forecast.get("highest_danger") or {}).get("display") or (forecast.get("highest_danger") or {}).get("value"))
    valid_until = forecast.get("valid_until_utc")
    status = "not_applicable_to_event"
    reason = (
        "Avalanche Canada data is current regional forecast context. It is not used as a historical label for this event date."
    )
    if not display:
        status = "missing"
        reason = "No Avalanche Canada danger rating was available."
    return make_component(
        component="avalanche_canada_current_forecast_context",
        source="Avalanche Canada current forecast",
        timestamp=valid_until or event_time,
        units="danger rating",
        original_value=str(display) if display else None,
        normalized_value=None,
        weight=0.0,
        status=status,
        reason=reason,
    )


def score_dynamic_conditions(
    *,
    event: dict[str, Any],
    weather: dict[str, Any],
    snow: dict[str, Any],
    forecast: dict[str, Any],
    weights: dict[str, Any],
) -> dict[str, Any]:
    event_id = str(event["event_id"])
    event_time = event.get("summary", {}).get("landsat_datetime_utc") or event.get("summary", {}).get("sentinel_datetime_utc")
    weather_event = (weather.get("event_windows") or {}).get(event_id) or {}
    snow_event = (snow.get("event_windows") or {}).get(event_id) or {}
    weather_72 = best_window(weather_event.get("windows") or {}, "previous_72_hours")
    weather_24 = best_window(weather_event.get("windows") or {}, "previous_24_hours")
    weights_by_component = dynamic_weights(weights)

    snowfall = first_number(weather_72.get("total_snowfall_cm"))
    precipitation = first_number(weather_72.get("total_precipitation_mm"))
    warming = positive_value(first_number(weather_24.get("temperature_change_c")))
    wind = max(
        value
        for value in [
            first_number(weather_72.get("maximum_wind_speed_kmh")),
            first_number(weather_72.get("maximum_gust_kmh")),
        ]
        if value is not None
    ) if any(first_number(weather_72.get(key)) is not None for key in ["maximum_wind_speed_kmh", "maximum_gust_kmh"]) else None
    swe_change = positive_value(max_station_window_value(snow_event, "swe_change_mm"))
    snow_depth_change = positive_value(max_station_window_value(snow_event, "snow_depth_change_cm"))

    sensors = event.get("summary", {}).get("sensors", {})
    sentinel = sensors.get("sentinel2") or {}
    landsat = sensors.get("landsat") or {}
    satellite_snow = average_available(
        [
            first_number(sentinel.get("snow_cover_percent")),
            first_number(landsat.get("snow_cover_percent")),
        ]
    )
    surface_temp = first_number(((landsat.get("surface_temperature_c") or {}).get("mean")))

    components = [
        make_component(
            component="recent_snowfall",
            source="ECCC weather previous 72 hours",
            timestamp=event_time,
            units="cm",
            original_value=snowfall,
            normalized_value=interp_score(snowfall, [0, 5, 15, 30, 50], [0, 25, 60, 85, 100]),
            weight=weights_by_component["recent_snowfall"],
            reason="More recent snowfall increases the prototype dynamic condition score.",
        ),
        make_component(
            component="recent_precipitation",
            source="ECCC weather previous 72 hours",
            timestamp=event_time,
            units="mm",
            original_value=precipitation,
            normalized_value=interp_score(precipitation, [0, 5, 20, 50, 100], [0, 20, 55, 85, 100]),
            weight=weights_by_component["recent_precipitation"],
            reason="Recent precipitation can add load or liquid water depending on temperature.",
        ),
        make_component(
            component="swe_change",
            source="BC snow stations previous 72 hours",
            timestamp=event_time,
            units="mm",
            original_value=swe_change,
            normalized_value=interp_score(swe_change, [0, 10, 30, 60], [0, 30, 70, 100]),
            weight=weights_by_component["swe_change"],
            reason="Positive SWE change is treated as added snowpack load.",
        ),
        make_component(
            component="snow_depth_change",
            source="BC snow stations previous 72 hours",
            timestamp=event_time,
            units="cm",
            original_value=snow_depth_change,
            normalized_value=interp_score(snow_depth_change, [0, 5, 20, 40], [0, 25, 70, 100]),
            weight=weights_by_component["snow_depth_change"],
            reason="Positive snow-depth change is treated as added snowpack load.",
        ),
        make_component(
            component="rapid_warming",
            source="ECCC weather previous 24 hours",
            timestamp=event_time,
            units="deg C",
            original_value=warming,
            normalized_value=interp_score(warming, [0, 2, 5, 10], [0, 35, 75, 100]),
            weight=weights_by_component["rapid_warming"],
            reason="Rapid warming can weaken snowpack structure; cooling is scored low only when measured.",
        ),
        make_component(
            component="strong_wind",
            source="ECCC weather previous 72 hours",
            timestamp=event_time,
            units="km/h",
            original_value=wind,
            normalized_value=interp_score(wind, [0, 20, 40, 70, 100], [0, 20, 55, 85, 100]),
            weight=weights_by_component["strong_wind"],
            reason="Strong wind can transport snow and form wind slabs where snow is available.",
        ),
        make_component(
            component="satellite_snow_cover",
            source="Sentinel-2/Landsat event summary",
            timestamp=event_time,
            units="percent",
            original_value=satellite_snow,
            normalized_value=interp_score(satellite_snow, [0, 10, 30, 70, 100], [0, 20, 55, 90, 100]),
            weight=weights_by_component["satellite_snow_cover"],
            reason="Satellite snow cover confirms whether avalanche terrain is snow covered in the event scene.",
        ),
        make_component(
            component="surface_temperature",
            source="Landsat thermal event summary",
            timestamp=event_time,
            units="deg C",
            original_value=surface_temp,
            normalized_value=interp_score(surface_temp, [-20, -10, -3, 0, 5, 10], [5, 15, 45, 65, 90, 100]),
            weight=weights_by_component["surface_temperature"],
            reason="Warmer surface-temperature signal increases the prototype wet-snow condition score.",
        ),
        avalanche_forecast_component(forecast, event_time),
    ]

    weighted_components = [component for component in components if component["weight"] > 0]
    total_weight = sum(float(component["weight"]) for component in weighted_components)
    available_components = [component for component in weighted_components if component["normalized_value"] is not None]
    available_weight = sum(float(component["weight"]) for component in available_components)
    minimum_weight = minimum_available_weight(weights)
    warnings: list[str] = []
    if total_weight <= 0 or available_weight <= 0:
        score = None
        warnings.append("No dynamic condition inputs were available; dynamic score was not calculated.")
    else:
        score = sum(float(component["normalized_value"]) * float(component["weight"]) for component in available_components) / available_weight
        if available_weight / total_weight < minimum_weight:
            warnings.append(
                f"Dynamic score has low input coverage ({available_weight / total_weight:.1%}); combined index is withheld."
            )

    for warning in weather.get("warnings", []):
        if "No ECCC" in warning:
            warnings.append(warning)
    for warning in snow.get("warnings", []):
        if "2C21P:archive" in warning:
            warnings.append(warning)
    for warning in forecast.get("warnings", []):
        warnings.append(warning)

    return {
        "score": round(float(score), 3) if score is not None else None,
        "available_weight": round(available_weight, 4),
        "total_configured_weight": round(total_weight, 4),
        "available_weight_fraction": round(available_weight / total_weight, 4) if total_weight else 0,
        "minimum_available_weight_fraction": minimum_weight,
        "components": components,
        "warnings": sorted(set(warnings)),
    }


def write_combined_raster(
    settings: Settings,
    event_id: str,
    terrain_score: np.ma.MaskedArray,
    terrain_profile: dict[str, Any],
    dynamic_score: float,
    weights: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if rasterio is None:
        raise RuntimeError("rasterio is required for combined susceptibility processing")
    combined_config = weights.get("combined", {})
    terrain_weight = float(combined_config.get("terrain_weight", 0.65))
    dynamic_weight = float(combined_config.get("dynamic_weight", 0.35))
    weight_total = terrain_weight + dynamic_weight or 1.0
    terrain_weight = terrain_weight / weight_total
    dynamic_weight = dynamic_weight / weight_total

    combined_values = np.clip(np.asarray(terrain_score.filled(-9999), dtype="float32") * terrain_weight + dynamic_score * dynamic_weight, 0, 100)
    combined = np.ma.array(combined_values.astype("float32"), mask=np.ma.getmaskarray(terrain_score))
    path = combined_raster_path(settings, event_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = terrain_profile.copy()
    profile.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate")
    profile.pop("bounds", None)
    profile.pop("resolution", None)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(combined.filled(-9999).astype("float32"), 1)
    preview = combined_preview_path(settings, event_id)
    preview.parent.mkdir(parents=True, exist_ok=True)
    from app.services.terrain import save_png  # local import keeps terrain display helpers centralized

    save_png(preview, susceptibility_to_rgba(combined))
    coordinates, bounds_wgs84 = raster_coordinates(terrain_profile)
    metadata = {
        "id": "combined_susceptibility",
        "title": "Combined prototype susceptibility",
        "kind": "raster_overlay",
        "preview_url": f"/api/susceptibility/events/{event_id}/preview",
        "metadata_url": f"/api/susceptibility/events/{event_id}/metadata",
        "download_url": f"/api/susceptibility/events/{event_id}/download",
        "coordinates": coordinates,
        "bounds_wgs84": bounds_wgs84,
        "opacity": 0.78,
        "legend": [
            {"label": "0-24 Low prototype index", "color": "#2f8a4c"},
            {"label": "25-49 Moderate", "color": "#d6c64b"},
            {"label": "50-74 Considerable", "color": "#dd8f35"},
            {"label": "75-100 High", "color": "#c23b35"},
        ],
        "stats": masked_stats(combined),
        "processing": {
            "generated_at_utc": utc_now_iso(),
            "algorithm": "terrain_susceptibility * terrain_weight + dynamic_condition_index * dynamic_weight",
            "terrain_weight": terrain_weight,
            "dynamic_weight": dynamic_weight,
            "dynamic_condition_index": dynamic_score,
            "crs": str(terrain_profile["crs"]),
            "resolution": list(terrain_profile["resolution"]),
            "bounds": list(terrain_profile["bounds"]),
            "output_sha256": file_sha256(path),
        },
        "warnings": [DISCLAIMER],
    }
    combined_metadata_path(settings, event_id).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return path, metadata


def process_event_susceptibility(settings: Settings, event_id: str, force: bool = False) -> dict[str, Any]:
    event_id = validate_event_id(settings, event_id)
    ensure_runtime_dirs(settings.runtime_root)
    output = susceptibility_summary_path(settings, event_id)
    terrain_index = generate_terrain_layers(settings, force=False)
    event = get_event(settings, event_id)
    weather = get_weather_summary(settings)
    snow = get_snow_summary(settings)
    forecast = get_avalanche_forecast(settings)
    cache_metadata = source_fingerprint(
        settings,
        [
            processed_raster_path(settings, "terrain_susceptibility"),
            event_summary_path(settings, event_id),
            weather_summary_path(settings),
            snow_summary_path(settings),
            forecast_summary_path(settings),
        ],
        config_files=[settings.backend_root / "config" / "susceptibility_weights.yaml"],
        parameters={
            "processor": "susceptibility",
            "event_id": event_id,
            "model_type": "rules_based_dynamic_combined",
        },
    )
    if output.exists() and not force:
        cached = json.loads(output.read_text(encoding="utf-8"))
        combined_available = bool(cached.get("combined_index", {}).get("available"))
        combined_outputs_ok = not combined_available or (
            combined_raster_path(settings, event_id).exists()
            and combined_metadata_path(settings, event_id).exists()
            and combined_preview_path(settings, event_id).exists()
        )
        if cache_matches(cached.get("cache", {}), cache_metadata) and combined_outputs_ok:
            return cached

    weights = load_weights(settings)
    dynamic = score_dynamic_conditions(event=event, weather=weather, snow=snow, forecast=forecast, weights=weights)

    terrain_path = processed_raster_path(settings, "terrain_susceptibility")
    terrain_score, terrain_profile = read_raster(terrain_path)
    combined_layer = None
    combined = None
    warnings = list(dynamic["warnings"])
    if dynamic["score"] is not None and dynamic["available_weight_fraction"] >= dynamic["minimum_available_weight_fraction"]:
        _, combined_layer = write_combined_raster(settings, event_id, terrain_score, terrain_profile, float(dynamic["score"]), weights)
        combined = {
            "available": True,
            "stats": combined_layer["stats"],
            "terrain_weight": combined_layer["processing"]["terrain_weight"],
            "dynamic_weight": combined_layer["processing"]["dynamic_weight"],
            "output_raster": str(combined_raster_path(settings, event_id).relative_to(settings.project_root).as_posix()),
            "output_sha256": combined_layer["processing"]["output_sha256"],
        }
    else:
        warnings.append("Combined index was not calculated because dynamic input coverage was insufficient.")
        combined = {"available": False, "stats": None}

    payload = {
        "schema_version": 1,
        "event_id": event_id,
        "date_label": event.get("date_label"),
        "generated_at_utc": utc_now_iso(),
        "model_type": "Configurable rules-based dynamic and combined avalanche susceptibility prototype",
        "terrain_susceptibility": {
            "stats": terrain_index.get("susceptibility", {}).get("stats"),
            "model_type": terrain_index.get("susceptibility", {}).get("model_type"),
            "factors": terrain_index.get("susceptibility", {}).get("factors", []),
            "weight_in_combined_index": float((weights.get("combined") or {}).get("terrain_weight", 0.65)),
        },
        "dynamic_condition_index": dynamic,
        "combined_index": combined,
        "combined_layer": combined_layer,
        "configuration": {
            "version": settings.app_version,
            "weights_file": "backend/config/susceptibility_weights.yaml",
            "weights_sha256": config_hash(settings),
            "weights": weights,
        },
        "warnings": sorted(set(warnings)),
        "disclaimer": DISCLAIMER,
        "cache": cache_metadata,
    }
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_cache_log(settings, f"susceptibility_{event_id}", cache_metadata)
    return payload


def process_all_event_susceptibility(settings: Settings, force: bool = False) -> dict[str, Any]:
    processed = []
    errors = []
    for event_dir in discover_event_dirs(settings):
        try:
            payload = process_event_susceptibility(settings, event_dir.name, force=force)
            processed.append(
                {
                    "event_id": event_dir.name,
                    "dynamic_score": payload.get("dynamic_condition_index", {}).get("score"),
                    "combined_available": payload.get("combined_index", {}).get("available"),
                    "warning_count": len(payload.get("warnings", [])),
                }
            )
        except Exception as exc:
            errors.append({"event_id": event_dir.name, "error": str(exc)})
    return {"processed": processed, "errors": errors, "count": len(processed)}


def get_event_susceptibility(settings: Settings, event_id: str) -> dict[str, Any]:
    return process_event_susceptibility(settings, event_id, force=False)


def get_event_susceptibility_metadata(settings: Settings, event_id: str) -> dict[str, Any]:
    validate_event_id(settings, event_id)
    process_event_susceptibility(settings, event_id, force=False)
    path = combined_metadata_path(settings, event_id)
    if not path.exists():
        raise FileNotFoundError(f"Combined susceptibility metadata missing: {event_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def get_event_susceptibility_preview(settings: Settings, event_id: str) -> Path:
    validate_event_id(settings, event_id)
    process_event_susceptibility(settings, event_id, force=False)
    path = combined_preview_path(settings, event_id)
    if not path.exists():
        raise FileNotFoundError(f"Combined susceptibility preview missing: {event_id}")
    return path


def get_event_susceptibility_download(settings: Settings, event_id: str) -> Path:
    validate_event_id(settings, event_id)
    process_event_susceptibility(settings, event_id, force=False)
    path = combined_raster_path(settings, event_id)
    if not path.exists():
        raise FileNotFoundError(f"Combined susceptibility raster missing: {event_id}")
    return path
