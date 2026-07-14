from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.paths import ensure_runtime_dirs, relative_source_path
from app.core.settings import Settings
from app.services.cache import cache_matches, source_fingerprint, write_cache_log
from app.services.events import discover_event_dirs, read_json
from app.services.terrain import DISCLAIMER, file_sha256


WINDOW_HOURS = [24, 48, 72, 168]

SNOW_STATIONS = {
    "2C09Q": {
        "name": "Morrissey Ridge",
        "latitude": 49.447222,
        "longitude": -114.975,
        "elevation_m": 1860.0,
    },
    "2C21P": {
        "name": "Fernie",
        "latitude": 49.48825,
        "longitude": -115.0726111,
        "elevation_m": 988.0,
    },
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dynamic_dir(settings: Settings) -> Path:
    return settings.runtime_root / "processed" / "dynamic"


def weather_parquet_path(settings: Settings) -> Path:
    return dynamic_dir(settings) / "weather_normalized.parquet"


def weather_summary_path(settings: Settings) -> Path:
    return dynamic_dir(settings) / "weather_summary.json"


def snow_parquet_path(settings: Settings) -> Path:
    return dynamic_dir(settings) / "snow_stations_normalized.parquet"


def snow_summary_path(settings: Settings) -> Path:
    return dynamic_dir(settings) / "snow_summary.json"


def forecast_summary_path(settings: Settings) -> Path:
    return dynamic_dir(settings) / "avalanche_forecast.json"


def numeric(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="float64")
    return pd.to_numeric(series, errors="coerce")


def optional_col(df: pd.DataFrame, name: str) -> pd.Series | None:
    return df[name] if name in df.columns else None


def first_existing(df: pd.DataFrame, names: list[str]) -> pd.Series | None:
    for name in names:
        if name in df.columns:
            return df[name]
    return None


def to_utc(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="datetime64[ns, UTC]")
    return pd.to_datetime(series, errors="coerce", utc=True)


def records_for_json(df: pd.DataFrame, columns: list[str] | None = None) -> list[dict[str, Any]]:
    if columns is not None:
        existing = [column for column in columns if column in df.columns]
        frame = df[existing].copy()
    else:
        frame = df.copy()
    for column in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            frame[column] = frame[column].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    frame = frame.astype(object).where(pd.notna(frame), None)
    return frame.to_dict("records")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")


def write_parquet(df: pd.DataFrame, path: Path, warnings: list[str]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
        return {"path": str(path), "format": "parquet", "sha256": file_sha256(path)}
    except Exception as exc:
        fallback = path.with_suffix(".csv")
        df.to_csv(fallback, index=False)
        warnings.append(f"Parquet write failed ({exc}); wrote CSV fallback {fallback.name}.")
        return {"path": str(fallback), "format": "csv", "sha256": file_sha256(fallback)}


def source_files(root: Path, pattern: str) -> list[Path]:
    return sorted(path for path in root.glob(pattern) if path.is_file())


def normalized_output_exists(path: Path) -> bool:
    return path.exists() or path.with_suffix(".csv").exists()


def discover_weather_sources(settings: Settings) -> list[Path]:
    weather_root = settings.data_root / "dynamic" / "weather_eccc"
    return [
        *source_files(weather_root, "climate-hourly_*.csv"),
        *source_files(weather_root, "climate-daily_*.csv"),
        *source_files(weather_root, "climate-stations_*.csv"),
    ]


def discover_snow_sources(settings: Settings) -> list[Path]:
    snow_root = settings.data_root / "dynamic" / "snow_bc"
    return sorted(path for path in snow_root.glob("*/*.csv") if path.is_file())


def discover_forecast_sources(settings: Settings) -> list[Path]:
    root = settings.data_root / "dynamic" / "avalanche_canada"
    return sorted(path for path in root.glob("*.json") if path.is_file())


def aoi_centroid(settings: Settings) -> tuple[float, float]:
    path = settings.data_root / "metadata" / "mount_hosmer_aoi.geojson"
    if not path.exists():
        return -115.011, 49.614
    data = json.loads(path.read_text(encoding="utf-8"))
    points: list[tuple[float, float]] = []

    def collect(coords: Any) -> None:
        if isinstance(coords, list) and len(coords) >= 2 and all(isinstance(v, (int, float)) for v in coords[:2]):
            points.append((float(coords[0]), float(coords[1])))
            return
        if isinstance(coords, list):
            for item in coords:
                collect(item)

    for feature in data.get("features", []):
        geometry = feature.get("geometry") or {}
        collect(geometry.get("coordinates"))
    if not points:
        return -115.011, 49.614
    return sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points)


def haversine_km(lon1: float | None, lat1: float | None, lon2: float, lat2: float) -> float | None:
    if lon1 is None or lat1 is None or not np.isfinite(lon1) or not np.isfinite(lat1):
        return None
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def wind_degrees(series: pd.Series | None) -> pd.Series:
    values = numeric(series)
    if values.empty:
        return values
    valid = values.dropna()
    if not valid.empty and valid.max() <= 36:
        values = values * 10.0
    return values.where((values >= 0) & (values <= 360))


def normalize_weather_frame(df: pd.DataFrame, cadence: str, source_file: str) -> pd.DataFrame:
    station_key = first_existing(df, ["CLIMATE_IDENTIFIER", "STN_ID", "STATION_NAME"])
    station_id = first_existing(df, ["STN_ID", "CLIMATE_IDENTIFIER"])
    timestamp = to_utc(first_existing(df, ["UTC_DATE", "LOCAL_DATE"]))
    local_timestamp = to_utc(optional_col(df, "LOCAL_DATE"))
    out = pd.DataFrame(
        {
            "source_file": source_file,
            "cadence": cadence,
            "station_key": station_key.astype(str) if station_key is not None else "",
            "station_id": station_id.astype(str) if station_id is not None else "",
            "station_name": optional_col(df, "STATION_NAME").astype(str) if "STATION_NAME" in df.columns else "",
            "timestamp_utc": timestamp,
            "local_timestamp": local_timestamp,
            "latitude": numeric(first_existing(df, ["latitude", "LATITUDE_DECIMAL_DEGREES", "LATITUDE"])),
            "longitude": numeric(first_existing(df, ["longitude", "LONGITUDE_DECIMAL_DEGREES", "LONGITUDE"])),
            "air_temperature_c": numeric(first_existing(df, ["TEMP", "MEAN_TEMPERATURE"])),
            "temperature_min_c": numeric(optional_col(df, "MIN_TEMPERATURE")),
            "temperature_max_c": numeric(optional_col(df, "MAX_TEMPERATURE")),
            "precipitation_mm": numeric(first_existing(df, ["PRECIP_AMOUNT", "TOTAL_PRECIPITATION"])),
            "rainfall_mm": numeric(optional_col(df, "TOTAL_RAIN")),
            "snowfall_cm": numeric(optional_col(df, "TOTAL_SNOW")),
            "snow_on_ground_cm": numeric(optional_col(df, "SNOW_ON_GROUND")),
            "wind_speed_kmh": numeric(optional_col(df, "WIND_SPEED")),
            "wind_direction_degrees": wind_degrees(first_existing(df, ["WIND_DIRECTION", "DIRECTION_MAX_GUST"])),
            "wind_gust_kmh": numeric(optional_col(df, "SPEED_MAX_GUST")),
            "relative_humidity_percent": numeric(optional_col(df, "RELATIVE_HUMIDITY")),
            "relative_humidity_min_percent": numeric(optional_col(df, "MIN_REL_HUMIDITY")),
            "relative_humidity_max_percent": numeric(optional_col(df, "MAX_REL_HUMIDITY")),
            "station_pressure_kpa": numeric(optional_col(df, "STATION_PRESSURE")),
        }
    )
    return out[out["timestamp_utc"].notna()].copy()


def load_weather_frames(settings: Settings) -> tuple[pd.DataFrame, list[Path], list[str]]:
    weather_root = settings.data_root / "dynamic" / "weather_eccc"
    warnings: list[str] = []
    sources: list[Path] = []
    frames: list[pd.DataFrame] = []
    for cadence, pattern in [("hourly", "climate-hourly_*.csv"), ("daily", "climate-daily_*.csv")]:
        files = source_files(weather_root, pattern)
        if not files:
            warnings.append(f"No ECCC {cadence} weather CSV files found.")
        for path in files:
            try:
                df = pd.read_csv(path, low_memory=False)
                frames.append(normalize_weather_frame(df, cadence, relative_source_path(settings.data_root, path)))
                sources.append(path)
            except Exception as exc:
                warnings.append(f"Unable to normalize weather file {path.name}: {exc}")
    if not frames:
        return pd.DataFrame(), sources, warnings
    weather = pd.concat(frames, ignore_index=True)
    stations = source_files(weather_root, "climate-stations_*.csv")
    if stations:
        sources.extend(stations)
        try:
            station_df = pd.read_csv(stations[0], low_memory=False)
            station_df["station_key"] = station_df["CLIMATE_IDENTIFIER"].fillna(station_df["STN_ID"]).astype(str)
            lookup = station_df.set_index("station_key")[["ELEVATION", "STATION_TYPE"]].to_dict("index")
            weather["station_elevation_m"] = weather["station_key"].map(lambda key: lookup.get(str(key), {}).get("ELEVATION"))
            weather["station_type"] = weather["station_key"].map(lambda key: lookup.get(str(key), {}).get("STATION_TYPE"))
        except Exception as exc:
            warnings.append(f"Unable to join station metadata: {exc}")
            weather["station_elevation_m"] = np.nan
            weather["station_type"] = None
    else:
        weather["station_elevation_m"] = np.nan
        weather["station_type"] = None
        warnings.append("No ECCC station metadata CSV found.")
    return weather, sources, warnings


def dominant_wind_direction(values: pd.Series) -> float | None:
    valid = values.dropna()
    if valid.empty:
        return None
    radians = np.deg2rad(valid.to_numpy(dtype="float64"))
    sin_mean = np.sin(radians).mean()
    cos_mean = np.cos(radians).mean()
    degrees = (np.rad2deg(np.arctan2(sin_mean, cos_mean)) + 360.0) % 360.0
    return round(float(degrees), 1)


def value_sum(series: pd.Series) -> float | None:
    valid = series.dropna()
    return None if valid.empty else round(float(valid.sum()), 3)


def value_min(series: pd.Series) -> float | None:
    valid = series.dropna()
    return None if valid.empty else round(float(valid.min()), 3)


def value_max(series: pd.Series) -> float | None:
    valid = series.dropna()
    return None if valid.empty else round(float(valid.max()), 3)


def value_mean(series: pd.Series) -> float | None:
    valid = series.dropna()
    return None if valid.empty else round(float(valid.mean()), 3)


def value_change(series: pd.Series) -> float | None:
    valid = series.dropna()
    if len(valid) < 2:
        return None
    return round(float(valid.iloc[-1] - valid.iloc[0]), 3)


def event_datetimes(settings: Settings) -> dict[str, datetime]:
    events: dict[str, datetime] = {}
    for event_dir in discover_event_dirs(settings):
        metadata, _ = read_json(event_dir / "event_metadata.json")
        raw = metadata.get("landsat_datetime_utc") or metadata.get("sentinel_datetime_utc")
        if raw:
            parsed = pd.to_datetime(raw, errors="coerce", utc=True)
            if pd.notna(parsed):
                events[event_dir.name] = parsed.to_pydatetime()
                continue
        try:
            events[event_dir.name] = datetime.strptime(event_dir.name.removeprefix("MH_"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return events


def weather_event_windows(weather: pd.DataFrame, settings: Settings) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    if weather.empty:
        return windows
    hourly = weather[weather["cadence"] == "hourly"].copy()
    daily = weather[weather["cadence"] == "daily"].copy()
    for event_id, event_time in event_datetimes(settings).items():
        event_payload: dict[str, Any] = {"event_time_utc": event_time.isoformat(), "windows": {}}
        for hours in WINDOW_HOURS:
            start = event_time - timedelta(hours=hours)
            hourly_window = hourly[(hourly["timestamp_utc"] >= start) & (hourly["timestamp_utc"] <= event_time)]
            daily_window = daily[(daily["timestamp_utc"] >= start) & (daily["timestamp_utc"] <= event_time)]
            temp = hourly_window["air_temperature_c"] if not hourly_window.empty else daily_window["air_temperature_c"]
            event_payload["windows"][f"previous_{hours}_hours"] = {
                "record_count": int(len(hourly_window) + len(daily_window)),
                "total_precipitation_mm": value_sum(hourly_window["precipitation_mm"]) if not hourly_window.empty else value_sum(daily_window["precipitation_mm"]),
                "total_snowfall_cm": value_sum(daily_window["snowfall_cm"]),
                "temperature_min_c": value_min(temp),
                "temperature_max_c": value_max(temp),
                "temperature_mean_c": value_mean(temp),
                "temperature_change_c": value_change(temp),
                "maximum_wind_speed_kmh": value_max(hourly_window["wind_speed_kmh"]),
                "dominant_wind_direction_degrees": dominant_wind_direction(hourly_window["wind_direction_degrees"]),
                "maximum_gust_kmh": value_max(daily_window["wind_gust_kmh"]),
                "snow_on_ground_change_cm": value_change(daily_window["snow_on_ground_cm"]),
            }
        windows[event_id] = event_payload
    return windows


def weather_station_summaries(weather: pd.DataFrame, settings: Settings) -> list[dict[str, Any]]:
    if weather.empty:
        return []
    lon, lat = aoi_centroid(settings)
    summaries = []
    grouped = weather.groupby("station_key", dropna=False)
    for station_key, group in grouped:
        daily = group[group["cadence"] == "daily"]
        hourly = group[group["cadence"] == "hourly"]
        latitude = group["latitude"].dropna().iloc[0] if group["latitude"].notna().any() else None
        longitude = group["longitude"].dropna().iloc[0] if group["longitude"].notna().any() else None
        elevation = group["station_elevation_m"].dropna().iloc[0] if "station_elevation_m" in group and group["station_elevation_m"].notna().any() else None
        summaries.append(
            {
                "station_key": str(station_key),
                "station_name": str(group["station_name"].dropna().iloc[0]) if group["station_name"].notna().any() else str(station_key),
                "latitude": float(latitude) if latitude is not None else None,
                "longitude": float(longitude) if longitude is not None else None,
                "elevation_m": float(elevation) if elevation is not None else None,
                "distance_to_aoi_km": haversine_km(float(longitude) if longitude is not None else None, float(latitude) if latitude is not None else None, lon, lat),
                "daily_records": int(len(daily)),
                "hourly_records": int(len(hourly)),
                "latest_timestamp_utc": group["timestamp_utc"].max().isoformat(),
                "variables_available": sorted(
                    column
                    for column in [
                        "air_temperature_c",
                        "temperature_min_c",
                        "temperature_max_c",
                        "precipitation_mm",
                        "rainfall_mm",
                        "snowfall_cm",
                        "snow_on_ground_cm",
                        "wind_speed_kmh",
                        "wind_direction_degrees",
                        "wind_gust_kmh",
                        "relative_humidity_percent",
                        "station_pressure_kpa",
                    ]
                    if column in group and group[column].notna().any()
                ),
            }
        )
    return sorted(summaries, key=lambda item: (item["distance_to_aoi_km"] is None, item["distance_to_aoi_km"] or 99999, item["station_name"]))


def process_weather(settings: Settings, force: bool = False) -> dict[str, Any]:
    ensure_runtime_dirs(settings.runtime_root)
    dynamic_dir(settings).mkdir(parents=True, exist_ok=True)
    cache_metadata = source_fingerprint(
        settings,
        discover_weather_sources(settings),
        parameters={"processor": "weather", "windows_hours": WINDOW_HOURS},
    )
    if weather_summary_path(settings).exists() and normalized_output_exists(weather_parquet_path(settings)) and not force:
        cached = json.loads(weather_summary_path(settings).read_text(encoding="utf-8"))
        if cache_matches(cached.get("cache", {}), cache_metadata):
            return cached
    weather, sources, warnings = load_weather_frames(settings)
    if weather.empty:
        payload = {
            "generated_at_utc": utc_now_iso(),
            "records": 0,
            "stations": [],
            "warnings": warnings,
            "series": [],
            "cache": cache_metadata,
        }
        write_json(weather_summary_path(settings), payload)
        write_cache_log(settings, "weather", cache_metadata)
        return payload
    weather = weather.sort_values(["timestamp_utc", "station_key", "cadence"]).reset_index(drop=True)
    output = write_parquet(weather, weather_parquet_path(settings), warnings)
    stations = weather_station_summaries(weather, settings)
    daily = weather[weather["cadence"] == "daily"].copy()
    hourly = weather[weather["cadence"] == "hourly"].copy()
    latest_hourly = hourly["timestamp_utc"].max() if not hourly.empty else pd.NaT
    hourly_recent = hourly[hourly["timestamp_utc"] >= latest_hourly - pd.Timedelta(days=10)] if pd.notna(latest_hourly) else hourly.head(0)
    default_station = next(
        (
            station["station_key"]
            for station in stations
            if station["daily_records"] > 0
            and station["hourly_records"] > 0
            and "air_temperature_c" in station["variables_available"]
            and "wind_speed_kmh" in station["variables_available"]
        ),
        next((station["station_key"] for station in stations if station["daily_records"] > 0 and "air_temperature_c" in station["variables_available"]), stations[0]["station_key"] if stations else None),
    )
    payload = {
        "schema_version": 1,
        "generated_at_utc": utc_now_iso(),
        "output": output,
        "source_files": [relative_source_path(settings.data_root, path) for path in sources],
        "record_count": int(len(weather)),
        "date_range_utc": {
            "start": weather["timestamp_utc"].min().isoformat(),
            "end": weather["timestamp_utc"].max().isoformat(),
        },
        "latest_weather_date": weather["timestamp_utc"].max().isoformat(),
        "station_count": len(stations),
        "default_station_key": default_station,
        "stations": stations,
        "daily_series": records_for_json(
            daily,
            [
                "timestamp_utc",
                "station_key",
                "station_name",
                "air_temperature_c",
                "temperature_min_c",
                "temperature_max_c",
                "precipitation_mm",
                "rainfall_mm",
                "snowfall_cm",
                "snow_on_ground_cm",
                "wind_gust_kmh",
            ],
        ),
        "hourly_recent_series": records_for_json(
            hourly_recent,
            [
                "timestamp_utc",
                "station_key",
                "station_name",
                "air_temperature_c",
                "precipitation_mm",
                "wind_speed_kmh",
                "wind_direction_degrees",
                "relative_humidity_percent",
                "station_pressure_kpa",
            ],
        ),
        "event_windows": weather_event_windows(weather, settings),
        "warnings": sorted(set(warnings)),
        "cache": cache_metadata,
    }
    write_json(weather_summary_path(settings), payload)
    write_cache_log(settings, "weather", cache_metadata)
    return payload


def normalize_snow_current_frame(df: pd.DataFrame, station_id: str, source_file: str) -> pd.DataFrame:
    station = SNOW_STATIONS.get(station_id, {})
    timestamp = to_utc(optional_col(df, "DateTime"))
    snow_depth = numeric(optional_col(df, "SD")) if "SD" in df.columns else pd.Series(np.nan, index=df.index)
    swe = numeric(optional_col(df, "SW")) if "SW" in df.columns else pd.Series(np.nan, index=df.index)
    return pd.DataFrame(
        {
            "source_file": source_file,
            "source_type": "current",
            "station_id": optional_col(df, "Location ID").fillna(station_id).astype(str) if "Location ID" in df.columns else station_id,
            "station_name": optional_col(df, "Location Name").fillna(station.get("name", station_id)).astype(str) if "Location Name" in df.columns else station.get("name", station_id),
            "timestamp_utc": timestamp,
            "latitude": numeric(optional_col(df, "Latitude")).fillna(station.get("latitude")),
            "longitude": numeric(optional_col(df, "Longitude")).fillna(station.get("longitude")),
            "elevation_m": numeric(optional_col(df, "Elevation")).fillna(station.get("elevation_m")),
            "snow_depth_cm": snow_depth,
            "swe_mm": swe,
            "snow_density_kg_m3": np.nan,
            "air_temperature_c": numeric(optional_col(df, "TA")),
            "temperature_min_c": np.nan,
            "temperature_max_c": np.nan,
            "precipitation_mm": numeric(optional_col(df, "PC")),
            "accumulated_precipitation_mm": np.nan,
        }
    ).loc[lambda frame: frame["timestamp_utc"].notna()]


def normalize_snow_archive_frame(df: pd.DataFrame, source_file: str) -> pd.DataFrame:
    station_id = "2C09Q"
    station = SNOW_STATIONS[station_id]
    timestamp = pd.to_datetime(optional_col(df, "Date"), errors="coerce", utc=True)
    return pd.DataFrame(
        {
            "source_file": source_file,
            "source_type": "archive",
            "station_id": optional_col(df, "Pillow I.D.").fillna(station_id).astype(str) if "Pillow I.D." in df.columns else station_id,
            "station_name": station["name"],
            "timestamp_utc": timestamp,
            "latitude": station["latitude"],
            "longitude": station["longitude"],
            "elevation_m": station["elevation_m"],
            "snow_depth_cm": np.nan,
            "swe_mm": numeric(optional_col(df, "Snow Water Equivalent")),
            "snow_density_kg_m3": np.nan,
            "air_temperature_c": np.nan,
            "temperature_min_c": numeric(optional_col(df, "Temp. Min.")),
            "temperature_max_c": numeric(optional_col(df, "Temp. Max.")),
            "precipitation_mm": numeric(optional_col(df, "Precipitation")),
            "accumulated_precipitation_mm": numeric(optional_col(df, "Accumulated Precip.")),
        }
    ).loc[lambda frame: frame["timestamp_utc"].notna()]


def load_snow_frames(settings: Settings) -> tuple[pd.DataFrame, list[Path], list[str]]:
    snow_root = settings.data_root / "dynamic" / "snow_bc"
    frames: list[pd.DataFrame] = []
    sources: list[Path] = []
    warnings = [
        "bc_snow:2C21P:archive returned HTTP 404; current Fernie data may exist while historical archive coverage is unavailable."
    ]
    for station_id in SNOW_STATIONS:
        station_dir = snow_root / station_id
        for path in sorted(station_dir.glob("*current_raw.csv")):
            sources.append(path)
            try:
                df = pd.read_csv(path, low_memory=False)
                frames.append(normalize_snow_current_frame(df, station_id, relative_source_path(settings.data_root, path)))
            except Exception as exc:
                warnings.append(f"Unable to normalize snow current file {path.name}: {exc}")
        if station_id == "2C09Q":
            archive = station_dir / "2C09Q_archive_raw.csv"
            if archive.exists():
                sources.append(archive)
                try:
                    df = pd.read_csv(archive, skiprows=8, encoding="cp1252", low_memory=False)
                    frames.append(normalize_snow_archive_frame(df, relative_source_path(settings.data_root, archive)))
                except Exception as exc:
                    warnings.append(f"Unable to normalize Morrissey Ridge archive {archive.name}: {exc}")
    if not frames:
        return pd.DataFrame(), sources, warnings
    return pd.concat(frames, ignore_index=True), sources, warnings


def snow_event_windows(snow: pd.DataFrame, settings: Settings) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    if snow.empty:
        return windows
    for event_id, event_time in event_datetimes(settings).items():
        event_payload: dict[str, Any] = {"event_time_utc": event_time.isoformat(), "stations": {}}
        for station_id, group in snow.groupby("station_id"):
            station_payload: dict[str, Any] = {}
            for hours in WINDOW_HOURS:
                start = event_time - timedelta(hours=hours)
                window = group[(group["timestamp_utc"] >= start) & (group["timestamp_utc"] <= event_time)].sort_values("timestamp_utc")
                station_payload[f"previous_{hours}_hours"] = {
                    "record_count": int(len(window)),
                    "swe_change_mm": value_change(window["swe_mm"]),
                    "snow_depth_change_cm": value_change(window["snow_depth_cm"]),
                    "total_precipitation_mm": value_sum(window["precipitation_mm"]),
                    "temperature_min_c": value_min(pd.concat([window["temperature_min_c"], window["air_temperature_c"]], ignore_index=True)),
                    "temperature_max_c": value_max(pd.concat([window["temperature_max_c"], window["air_temperature_c"]], ignore_index=True)),
                }
            event_payload["stations"][str(station_id)] = station_payload
        windows[event_id] = event_payload
    return windows


def snow_station_summaries(snow: pd.DataFrame) -> list[dict[str, Any]]:
    summaries = []
    for station_id, station in SNOW_STATIONS.items():
        group = snow[snow["station_id"] == station_id] if not snow.empty and "station_id" in snow else pd.DataFrame()
        summaries.append(
            {
                "station_id": station_id,
                "station_name": station["name"],
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "elevation_m": station["elevation_m"],
                "record_count": int(len(group)),
                "current_record_count": int((group["source_type"] == "current").sum()) if not group.empty else 0,
                "archive_record_count": int((group["source_type"] == "archive").sum()) if not group.empty else 0,
                "date_range_utc": {
                    "start": group["timestamp_utc"].min().isoformat() if not group.empty else None,
                    "end": group["timestamp_utc"].max().isoformat() if not group.empty else None,
                },
                "latest_snow_depth_cm": value_max(group.tail(1)["snow_depth_cm"]) if not group.empty else None,
                "latest_swe_mm": value_max(group.tail(1)["swe_mm"]) if not group.empty else None,
                "variables_available": sorted(
                    column
                    for column in ["snow_depth_cm", "swe_mm", "air_temperature_c", "precipitation_mm", "temperature_min_c", "temperature_max_c"]
                    if not group.empty and column in group and group[column].notna().any()
                ),
            }
        )
    return summaries


def process_snow(settings: Settings, force: bool = False) -> dict[str, Any]:
    ensure_runtime_dirs(settings.runtime_root)
    dynamic_dir(settings).mkdir(parents=True, exist_ok=True)
    cache_metadata = source_fingerprint(
        settings,
        discover_snow_sources(settings),
        parameters={"processor": "snow", "stations": sorted(SNOW_STATIONS)},
    )
    if snow_summary_path(settings).exists() and normalized_output_exists(snow_parquet_path(settings)) and not force:
        cached = json.loads(snow_summary_path(settings).read_text(encoding="utf-8"))
        if cache_matches(cached.get("cache", {}), cache_metadata):
            return cached
    snow, sources, warnings = load_snow_frames(settings)
    if not snow.empty:
        snow = snow.sort_values(["timestamp_utc", "station_id", "source_type"]).reset_index(drop=True)
    output = write_parquet(snow, snow_parquet_path(settings), warnings)
    chart_start = pd.Timestamp("2025-10-01T00:00:00Z")
    chart_series = snow[snow["timestamp_utc"] >= chart_start] if not snow.empty else snow
    payload = {
        "schema_version": 1,
        "generated_at_utc": utc_now_iso(),
        "output": output,
        "source_files": [relative_source_path(settings.data_root, path) for path in sources],
        "record_count": int(len(snow)),
        "stations": snow_station_summaries(snow),
        "series": records_for_json(
            chart_series,
            [
                "timestamp_utc",
                "station_id",
                "station_name",
                "source_type",
                "snow_depth_cm",
                "swe_mm",
                "snow_density_kg_m3",
                "air_temperature_c",
                "temperature_min_c",
                "temperature_max_c",
                "precipitation_mm",
                "accumulated_precipitation_mm",
            ],
        ),
        "event_windows": snow_event_windows(snow, settings),
        "warnings": sorted(set(warnings)),
        "cache": cache_metadata,
    }
    write_json(snow_summary_path(settings), payload)
    write_cache_log(settings, "snow", cache_metadata)
    return payload


def strip_html(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_danger_ratings(ratings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = []
    for item in ratings:
        bands = {}
        for key, label in [("alp", "alpine"), ("tln", "treeline"), ("btl", "below_treeline")]:
            band = (item.get("ratings") or {}).get(key) or {}
            rating = band.get("rating") or {}
            bands[label] = {
                "display": rating.get("display"),
                "value": rating.get("value"),
                "colour": rating.get("colour"),
            }
        parsed.append(
            {
                "date": (item.get("date") or {}).get("value"),
                "date_display": (item.get("date") or {}).get("display"),
                "ratings": bands,
            }
        )
    return parsed


def parse_avalanche_problems(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed = []
    for problem in problems:
        parsed.append(
            {
                "type": (problem.get("type") or {}).get("display") or (problem.get("type") or {}).get("value"),
                "likelihood": (problem.get("likelihood") or {}).get("display"),
                "size": (problem.get("expectedSize") or {}).get("display"),
                "description": strip_html(problem.get("comment") or problem.get("description")),
                "elevations": problem.get("elevations"),
                "aspects": problem.get("aspects"),
            }
        )
    return parsed


def process_avalanche_forecast(settings: Settings, force: bool = False) -> dict[str, Any]:
    ensure_runtime_dirs(settings.runtime_root)
    dynamic_dir(settings).mkdir(parents=True, exist_ok=True)
    cache_metadata = source_fingerprint(
        settings,
        discover_forecast_sources(settings),
        parameters={"processor": "avalanche_forecast"},
    )
    if forecast_summary_path(settings).exists() and not force:
        cached = json.loads(forecast_summary_path(settings).read_text(encoding="utf-8"))
        if cache_matches(cached.get("cache", {}), cache_metadata):
            return cached
    root = settings.data_root / "dynamic" / "avalanche_canada"
    point_path = root / "current_point.json"
    products_path = root / "current_products.json"
    metadata_path = root / "current_metadata.json"
    warnings: list[str] = []
    point, point_warnings = read_json(point_path)
    warnings.extend(point_warnings)
    products: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for path, label in [(products_path, "current_products.json"), (metadata_path, "current_metadata.json")]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if label == "current_products.json" and isinstance(data, list):
                    products = data
                if label == "current_metadata.json" and isinstance(data, list):
                    metadata = data
            except Exception as exc:
                warnings.append(f"Unable to read Avalanche Canada {label}: {exc}")
        else:
            warnings.append(f"Missing Avalanche Canada {label}.")
    report = point.get("report") or {}
    owner = point.get("owner") or {}
    issued = pd.to_datetime(report.get("dateIssued"), errors="coerce", utc=True)
    valid_until = pd.to_datetime(report.get("validUntil"), errors="coerce", utc=True)
    now = pd.Timestamp.now(tz="UTC")
    valid = bool(pd.notna(valid_until) and valid_until >= now)
    highest = None
    point_area_id = (point.get("area") or {}).get("id")
    for item in metadata:
        area = item.get("area") or {}
        if area.get("id") == point_area_id or area.get("name") == point_area_id:
            highest = item.get("highestDanger")
            break
    if highest is None and metadata:
        highest = metadata[0].get("highestDanger")
    highlights = strip_html(report.get("highlights"))
    if highlights and "Regular avalanche forecasts have ended" in highlights:
        warnings.append("Avalanche Canada reports regular forecasts have ended for the season; danger may still exist in high elevation terrain.")
    if not valid:
        warnings.append("Avalanche Canada forecast validity period appears stale or unavailable.")
    payload = {
        "schema_version": 1,
        "generated_at_utc": utc_now_iso(),
        "applicable_region": report.get("title") or (point.get("area") or {}).get("name"),
        "area": point.get("area"),
        "forecast_url": point.get("url"),
        "source": {
            "owner": owner.get("display") or owner.get("value") or "Avalanche Canada",
            "url": owner.get("url") or "https://avalanche.ca",
            "source_files": [
                relative_source_path(settings.data_root, path)
                for path in [point_path, products_path, metadata_path, root / "current_areas.json"]
                if path.exists()
            ],
        },
        "publication_time_utc": issued.isoformat() if pd.notna(issued) else None,
        "valid_until_utc": valid_until.isoformat() if pd.notna(valid_until) else None,
        "timezone": report.get("timezone"),
        "freshness": {
            "valid_now": valid,
            "status": "valid" if valid else "stale_or_unknown",
            "as_of_utc": now.isoformat(),
            "age_hours": round(float((now - issued).total_seconds() / 3600), 2) if pd.notna(issued) else None,
        },
        "highest_danger": highest,
        "confidence": (report.get("confidence") or {}).get("rating"),
        "highlights": highlights,
        "summaries": [
            {
                "type": ((summary.get("type") or {}).get("display") or (summary.get("type") or {}).get("value")),
                "content": strip_html(summary.get("content")),
            }
            for summary in report.get("summaries", [])
        ],
        "danger_ratings": parse_danger_ratings(report.get("dangerRatings") or []),
        "avalanche_problems": parse_avalanche_problems(report.get("problems") or []),
        "terrain_and_travel_advice": [strip_html(item) for item in report.get("terrainAndTravelAdvice", [])],
        "product_count": len(products),
        "metadata_count": len(metadata),
        "warnings": sorted(set(warnings)),
        "disclaimer": "This is current regional professional forecast context from Avalanche Canada, not a historical avalanche observation label.",
        "cache": cache_metadata,
    }
    write_json(forecast_summary_path(settings), payload)
    write_cache_log(settings, "avalanche_forecast", cache_metadata)
    return payload


def process_dynamic(settings: Settings, force: bool = False) -> dict[str, Any]:
    weather = process_weather(settings, force=force)
    snow = process_snow(settings, force=force)
    forecast = process_avalanche_forecast(settings, force=force)
    return {
        "generated_at_utc": utc_now_iso(),
        "weather": {
            "record_count": weather.get("record_count", 0),
            "station_count": weather.get("station_count", 0),
            "warnings": weather.get("warnings", []),
        },
        "snow": {
            "record_count": snow.get("record_count", 0),
            "station_count": len(snow.get("stations", [])),
            "warnings": snow.get("warnings", []),
        },
        "forecast": {
            "applicable_region": forecast.get("applicable_region"),
            "valid_until_utc": forecast.get("valid_until_utc"),
            "warnings": forecast.get("warnings", []),
        },
    }


def get_weather(settings: Settings) -> dict[str, Any]:
    return process_weather(settings, force=False)


def get_weather_summary(settings: Settings) -> dict[str, Any]:
    payload = process_weather(settings, force=False)
    return {key: value for key, value in payload.items() if key not in {"daily_series", "hourly_recent_series"}}


def get_snow(settings: Settings) -> dict[str, Any]:
    return process_snow(settings, force=False)


def get_snow_summary(settings: Settings) -> dict[str, Any]:
    payload = process_snow(settings, force=False)
    return {key: value for key, value in payload.items() if key != "series"}


def get_avalanche_forecast(settings: Settings) -> dict[str, Any]:
    return process_avalanche_forecast(settings, force=False)
