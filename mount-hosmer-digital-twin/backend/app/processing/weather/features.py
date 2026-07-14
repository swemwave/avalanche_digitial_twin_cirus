"""Weather features, and the ConditionSet that every downstream model consumes.

There are three ways to ask this system "what were the conditions?":

* **historical replay** -- pick a datetime, get what the instruments recorded
* **current** -- get the most recent valid observations, and be told how old they are
* **scenario** -- state a hypothetical, and have every value marked `user_supplied`

All three produce the same object, a :class:`ConditionSet`. The snow, wind and
instability models take a ConditionSet and cannot tell which mode produced it --
except by reading the provenance on each value, which is exactly the point. A
scenario cannot masquerade as an observation.

Wind direction is handled as a **vector** throughout. Averaging 350 deg and 10 deg
as scalars gives 180 deg -- a south wind, the exact opposite of the north wind
actually blowing. Every directional statistic here goes through the unit circle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core import provenance as prov
from app.core.model_config import ModelConfig
from app.core.paths import relative_source_path
from app.core.settings import Settings

WINDOWS_HOURS = (6, 12, 24, 48, 72)

#: Mount Hosmer AOI centroid, WGS84. Used for station-distance weighting.
AOI_LON, AOI_LAT = -115.011, 49.614

#: Representative elevation of the avalanche terrain we care about. Station
#: readings are lapse-rate adjusted to this height. It is a single number
#: standing in for a mountain that spans 1000-2600 m, which is a real limitation.
REFERENCE_ELEVATION_M = 2000.0


class Mode(str, Enum):
    HISTORICAL = "historical"
    CURRENT = "current"
    SCENARIO = "scenario"


@dataclass
class WeatherSource:
    """Normalized ECCC observations, hourly and daily, for the whole record."""

    hourly: pd.DataFrame
    daily: pd.DataFrame
    stations: list[dict[str, Any]]
    source_files: list[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.hourly.empty and self.daily.empty

    @property
    def latest_observation(self) -> datetime | None:
        times = [
            frame["timestamp_utc"].max()
            for frame in (self.hourly, self.daily)
            if not frame.empty
        ]
        times = [time for time in times if pd.notna(time)]
        return max(times).to_pydatetime() if times else None

    def primary_station(self) -> dict[str, Any] | None:
        return self.stations[0] if self.stations else None


@dataclass
class ScenarioInput:
    """A user-defined hypothetical. Every field is optional; supplied fields win."""

    snowfall_24h_cm: float | None = None
    snowfall_48h_cm: float | None = None
    snowfall_72h_cm: float | None = None
    rain_24h_mm: float | None = None
    temperature_c: float | None = None
    temperature_change_24h_c: float | None = None
    wind_speed_kmh: float | None = None
    wind_direction_deg: float | None = None
    wind_gust_kmh: float | None = None
    snow_depth_index: float | None = None
    swe_index: float | None = None
    freeze_thaw: bool | None = None
    release_size: str = "medium"
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if value is not None}


@dataclass
class ConditionSet:
    """The complete, provenance-tagged weather state used by every model."""

    mode: Mode
    valid_time_utc: datetime
    values: dict[str, prov.Value]
    station: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)
    scenario: ScenarioInput | None = None
    release_size: str = "medium"

    def get(self, name: str) -> prov.Value | None:
        return self.values.get(name)

    def number(self, name: str, default: float | None = None) -> float | None:
        value = self.values.get(name)
        if value is None or not value.is_available:
            return default
        return value.numeric

    def require_number(self, name: str) -> float | None:
        """Numeric value, or None when unavailable. Never a fabricated default."""
        return self.number(name)

    @property
    def observation_age_hours(self) -> float | None:
        if self.mode is Mode.SCENARIO:
            return None
        timestamps = [
            value.timestamp_utc for value in self.values.values() if value.timestamp_utc
        ]
        if not timestamps:
            return None
        newest = max(pd.to_datetime(stamp, utc=True) for stamp in timestamps)
        now = pd.Timestamp.now(tz="UTC")
        return round(float((now - newest).total_seconds() / 3600.0), 2)

    @property
    def station_distance_km(self) -> float | None:
        return (self.station or {}).get("distance_to_aoi_km")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "valid_time_utc": self.valid_time_utc.isoformat(),
            "release_size": self.release_size,
            "station": self.station,
            "observation_age_hours": self.observation_age_hours,
            "station_distance_km": self.station_distance_km,
            "scenario": self.scenario.to_dict() if self.scenario else None,
            "values": {name: value.to_dict() for name, value in self.values.items()},
            "provenance_summary": prov.summarize(list(self.values.values())),
            "warnings": sorted(set(self.warnings)),
        }


# --- Loading -----------------------------------------------------------------


def _numeric(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype="float64")
    return pd.to_numeric(frame[name], errors="coerce")


def _first(frame: pd.DataFrame, names: list[str]) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    return pd.Series(np.nan, index=frame.index, dtype="float64")


def _wind_degrees(series: pd.Series) -> pd.Series:
    """ECCC reports wind direction in tens of degrees. Convert, and reject junk."""
    values = pd.to_numeric(series, errors="coerce")
    valid = values.dropna()
    if not valid.empty and valid.max() <= 36:
        values = values * 10.0
    return values.where((values >= 0) & (values <= 360))


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return round(radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def _normalize(frame: pd.DataFrame, cadence: str, source: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "source_file": source,
            "cadence": cadence,
            "station_key": _first(frame, ["CLIMATE_IDENTIFIER", "STN_ID", "STATION_NAME"]).astype(str),
            "station_name": (
                frame["STATION_NAME"].astype(str) if "STATION_NAME" in frame.columns else ""
            ),
            "timestamp_utc": pd.to_datetime(
                _first(frame, ["UTC_DATE", "LOCAL_DATE"]), errors="coerce", utc=True
            ),
            "latitude": _numeric(frame, "LATITUDE_DECIMAL_DEGREES").fillna(_numeric(frame, "y")),
            "longitude": _numeric(frame, "LONGITUDE_DECIMAL_DEGREES").fillna(_numeric(frame, "x")),
            "temperature_c": pd.to_numeric(
                _first(frame, ["TEMP", "MEAN_TEMPERATURE"]), errors="coerce"
            ),
            "temperature_min_c": _numeric(frame, "MIN_TEMPERATURE"),
            "temperature_max_c": _numeric(frame, "MAX_TEMPERATURE"),
            "precipitation_mm": pd.to_numeric(
                _first(frame, ["PRECIP_AMOUNT", "TOTAL_PRECIPITATION"]), errors="coerce"
            ),
            "rainfall_mm": _numeric(frame, "TOTAL_RAIN"),
            "snowfall_cm": _numeric(frame, "TOTAL_SNOW"),
            "snow_on_ground_cm": _numeric(frame, "SNOW_ON_GROUND"),
            "wind_speed_kmh": _numeric(frame, "WIND_SPEED"),
            "wind_direction_deg": _wind_degrees(
                _first(frame, ["WIND_DIRECTION", "DIRECTION_MAX_GUST"])
            ),
            "wind_gust_kmh": _numeric(frame, "SPEED_MAX_GUST"),
            "relative_humidity_percent": _numeric(frame, "RELATIVE_HUMIDITY"),
        }
    )
    return out[out["timestamp_utc"].notna()].copy()


def load_weather(settings: Settings) -> WeatherSource:
    root = settings.data_root / "dynamic" / "weather_eccc"
    warnings: list[str] = []
    frames: dict[str, list[pd.DataFrame]] = {"hourly": [], "daily": []}
    sources: list[str] = []

    for cadence, pattern in (("hourly", "climate-hourly_*.csv"), ("daily", "climate-daily_*.csv")):
        files = sorted(root.glob(pattern)) if root.exists() else []
        if not files:
            warnings.append(f"No ECCC {cadence} weather files found in {root}.")
        for path in files:
            if path.stat().st_size == 0:
                warnings.append(f"ECCC {cadence} file {path.name} is empty and was skipped.")
                continue
            try:
                raw = pd.read_csv(path, low_memory=False)
            except Exception as exc:
                warnings.append(f"Could not read {path.name}: {exc}")
                continue
            if raw.empty:
                warnings.append(f"ECCC {cadence} file {path.name} has a header but no rows.")
                continue
            frames[cadence].append(_normalize(raw, cadence, relative_source_path(settings.data_root, path)))
            sources.append(relative_source_path(settings.data_root, path))

    hourly = (
        pd.concat(frames["hourly"], ignore_index=True).sort_values("timestamp_utc")
        if frames["hourly"]
        else pd.DataFrame()
    )
    daily = (
        pd.concat(frames["daily"], ignore_index=True).sort_values("timestamp_utc")
        if frames["daily"]
        else pd.DataFrame()
    )

    stations = _station_summaries(settings, hourly, daily, root, warnings)
    return WeatherSource(hourly=hourly, daily=daily, stations=stations, source_files=sources, warnings=warnings)


def _station_summaries(
    settings: Settings,
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    root: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    combined = pd.concat([frame for frame in (hourly, daily) if not frame.empty], ignore_index=True)
    if combined.empty:
        return []

    elevation: dict[str, float] = {}
    station_files = sorted(root.glob("climate-stations_*.csv")) if root.exists() else []
    if station_files:
        try:
            meta = pd.read_csv(station_files[0], low_memory=False)
            meta["key"] = meta["CLIMATE_IDENTIFIER"].astype(str)
            elevation = pd.to_numeric(meta["ELEVATION"], errors="coerce").groupby(meta["key"]).first().to_dict()
        except Exception as exc:
            warnings.append(f"Could not read ECCC station metadata: {exc}")

    summaries: list[dict[str, Any]] = []
    for key, group in combined.groupby("station_key"):
        lat = group["latitude"].dropna()
        lon = group["longitude"].dropna()
        latitude = float(lat.iloc[0]) if not lat.empty else None
        longitude = float(lon.iloc[0]) if not lon.empty else None
        distance = (
            haversine_km(longitude, latitude, AOI_LON, AOI_LAT)
            if latitude is not None and longitude is not None
            else None
        )
        names = group["station_name"].dropna()
        summaries.append(
            {
                "station_key": str(key),
                "station_name": str(names.iloc[0]) if not names.empty else str(key),
                "latitude": latitude,
                "longitude": longitude,
                "elevation_m": float(elevation.get(str(key))) if elevation.get(str(key)) is not None else None,
                "distance_to_aoi_km": distance,
                "hourly_records": int((group["cadence"] == "hourly").sum()),
                "daily_records": int((group["cadence"] == "daily").sum()),
                "first_observation_utc": group["timestamp_utc"].min().isoformat(),
                "latest_observation_utc": group["timestamp_utc"].max().isoformat(),
            }
        )

    # Nearest station first. Everything downstream uses [0] as the primary.
    return sorted(
        summaries,
        key=lambda item: (item["distance_to_aoi_km"] is None, item["distance_to_aoi_km"] or 1e9),
    )


# --- Directional statistics ---------------------------------------------------


def vector_mean_direction(
    directions_deg: np.ndarray, weights: np.ndarray | None = None
) -> tuple[float | None, float | None]:
    """Resultant direction and consistency of a set of wind bearings.

    Returns ``(direction_deg, consistency)`` where consistency is 0-1: 1 means
    every reading pointed the same way, 0 means they cancelled out entirely.

    This is why scalar averaging is forbidden. Winds at 350 and 10 degrees average
    to 0 degrees (north) here, and to 180 degrees (south) if you take the
    arithmetic mean -- an answer that is not merely wrong but exactly inverted,
    which would put the modelled lee slope on the wrong side of the mountain.
    """
    values = np.asarray(directions_deg, dtype="float64")
    finite = np.isfinite(values)
    if not finite.any():
        return None, None
    values = values[finite]
    magnitude = (
        np.asarray(weights, dtype="float64")[finite] if weights is not None else np.ones(values.size)
    )
    magnitude = np.where(np.isfinite(magnitude), magnitude, 0.0)
    if magnitude.sum() <= 0:
        magnitude = np.ones(values.size)

    radians = np.deg2rad(values)
    east = float((magnitude * np.sin(radians)).sum())
    north = float((magnitude * np.cos(radians)).sum())
    if abs(east) < 1e-12 and abs(north) < 1e-12:
        return None, 0.0

    direction = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    consistency = math.hypot(east, north) / float(magnitude.sum())
    return round(direction, 1), round(min(consistency, 1.0), 4)


def _window(frame: pd.DataFrame, end: datetime, hours: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    start = pd.Timestamp(end) - pd.Timedelta(hours=hours)
    return frame[(frame["timestamp_utc"] > start) & (frame["timestamp_utc"] <= pd.Timestamp(end))]


def _sum(series: pd.Series) -> float | None:
    valid = series.dropna()
    return round(float(valid.sum()), 3) if not valid.empty else None


def _max(series: pd.Series) -> float | None:
    valid = series.dropna()
    return round(float(valid.max()), 3) if not valid.empty else None


def _min(series: pd.Series) -> float | None:
    valid = series.dropna()
    return round(float(valid.min()), 3) if not valid.empty else None


def _mean(series: pd.Series) -> float | None:
    valid = series.dropna()
    return round(float(valid.mean()), 3) if not valid.empty else None


def _change(series: pd.Series) -> float | None:
    valid = series.dropna()
    if len(valid) < 2:
        return None
    return round(float(valid.iloc[-1] - valid.iloc[0]), 3)


def freeze_thaw_cycles(temperatures: pd.Series) -> int | None:
    """Count zero crossings. Each one weakens the bond between snow layers."""
    valid = temperatures.dropna().to_numpy(dtype="float64")
    if valid.size < 2:
        return None
    above = valid > 0.0
    return int(np.count_nonzero(above[1:] != above[:-1]))


def hours_since(frame: pd.DataFrame, column: str, end: datetime, threshold: float = 0.2) -> float | None:
    """Hours since the last time ``column`` exceeded ``threshold``."""
    if frame.empty or column not in frame.columns:
        return None
    past = frame[frame["timestamp_utc"] <= pd.Timestamp(end)]
    events = past[pd.to_numeric(past[column], errors="coerce") > threshold]
    if events.empty:
        return None
    last = events["timestamp_utc"].max()
    return round(float((pd.Timestamp(end) - last).total_seconds() / 3600.0), 2)


# --- Feature construction -----------------------------------------------------


def _value(
    name: str,
    number: float | None,
    units: str,
    source: str,
    timestamp: datetime | None,
    provenance: str,
    reason: str,
    missing_reason: str,
) -> prov.Value:
    if number is None or not np.isfinite(number):
        return prov.unavailable(name, units, source, missing_reason)
    return prov.Value(
        name=name,
        value=round(float(number), 4),
        units=units,
        provenance=provenance,  # type: ignore[arg-type]
        source=source,
        timestamp_utc=timestamp.isoformat() if timestamp else None,
        detail=reason,
    )


def build_conditions(
    settings: Settings,
    config: ModelConfig,
    source: WeatherSource,
    valid_time: datetime,
    mode: Mode,
    scenario: ScenarioInput | None = None,
) -> ConditionSet:
    """Assemble every weather feature at ``valid_time``, tagged with provenance."""
    warnings = list(source.warnings)
    station = source.primary_station()
    values: dict[str, prov.Value] = {}

    hourly = source.hourly
    daily = source.daily
    label = (
        f"ECCC {station['station_name']}" if station else "ECCC weather"
    )
    missing = "No ECCC observation covered this time window."

    if source.is_empty:
        warnings.append(
            "No ECCC weather observations are available at all. Every weather-derived value is "
            "unavailable and the dynamic instability score will be withheld."
        )

    if station and station.get("distance_to_aoi_km") is not None:
        distance = station["distance_to_aoi_km"]
        elevation = station.get("elevation_m")
        warnings.append(
            f"Nearest weather station ({station['station_name']}) is {distance} km from the AOI"
            + (f" at {elevation:.0f} m elevation" if elevation else "")
            + ". All weather values are interpolated to the mountain, not measured on it."
        )

    # Station observations are away from the AOI, so anything derived from them
    # is `interpolated`, never `observed`. This distinction propagates into the
    # confidence score.
    station_provenance = "interpolated"

    for hours in WINDOWS_HOURS:
        hour_window = _window(hourly, valid_time, hours)
        day_window = _window(daily, valid_time, hours)
        temps = hour_window["temperature_c"] if not hour_window.empty else (
            day_window["temperature_c"] if not day_window.empty else pd.Series(dtype="float64")
        )

        values[f"temperature_mean_{hours}h_c"] = _value(
            f"temperature_mean_{hours}h_c", _mean(temps), "deg C", label, valid_time,
            station_provenance, f"Mean air temperature over the previous {hours} h.", missing,
        )
        values[f"temperature_min_{hours}h_c"] = _value(
            f"temperature_min_{hours}h_c", _min(temps), "deg C", label, valid_time,
            station_provenance, f"Minimum air temperature over the previous {hours} h.", missing,
        )
        values[f"temperature_max_{hours}h_c"] = _value(
            f"temperature_max_{hours}h_c", _max(temps), "deg C", label, valid_time,
            station_provenance, f"Maximum air temperature over the previous {hours} h.", missing,
        )
        values[f"temperature_change_{hours}h_c"] = _value(
            f"temperature_change_{hours}h_c", _change(temps), "deg C", label, valid_time,
            station_provenance,
            f"Change in air temperature across the previous {hours} h. Positive is warming.",
            missing,
        )
        values[f"precipitation_{hours}h_mm"] = _value(
            f"precipitation_{hours}h_mm",
            _sum(hour_window["precipitation_mm"]) if not hour_window.empty else _sum(day_window["precipitation_mm"]) if not day_window.empty else None,
            "mm", label, valid_time, station_provenance,
            f"Total precipitation over the previous {hours} h.", missing,
        )
        values[f"snowfall_{hours}h_cm"] = _value(
            f"snowfall_{hours}h_cm",
            _sum(day_window["snowfall_cm"]) if not day_window.empty else None,
            "cm", label, valid_time, station_provenance,
            f"Reported snowfall over the previous {hours} h. Only the DAILY ECCC feed reports "
            f"snowfall, so short windows may be unavailable even when precipitation is not.",
            "ECCC reports snowfall only in the daily feed; no daily record covers this window.",
        )
        values[f"rainfall_{hours}h_mm"] = _value(
            f"rainfall_{hours}h_mm",
            _sum(day_window["rainfall_mm"]) if not day_window.empty else None,
            "mm", label, valid_time, station_provenance,
            f"Reported rainfall over the previous {hours} h.",
            "ECCC reports rainfall only in the daily feed; no daily record covers this window.",
        )
        values[f"wind_speed_max_{hours}h_kmh"] = _value(
            f"wind_speed_max_{hours}h_kmh",
            _max(hour_window["wind_speed_kmh"]) if not hour_window.empty else None,
            "km/h", label, valid_time, station_provenance,
            f"Maximum sustained wind speed over the previous {hours} h.", missing,
        )
        values[f"wind_gust_max_{hours}h_kmh"] = _value(
            f"wind_gust_max_{hours}h_kmh",
            _max(day_window["wind_gust_kmh"]) if not day_window.empty else None,
            "km/h", label, valid_time, station_provenance,
            f"Maximum wind gust over the previous {hours} h.", missing,
        )

        if not hour_window.empty:
            direction, consistency = vector_mean_direction(
                hour_window["wind_direction_deg"].to_numpy(dtype="float64"),
                hour_window["wind_speed_kmh"].to_numpy(dtype="float64"),
            )
        else:
            direction, consistency = None, None

        values[f"wind_direction_{hours}h_deg"] = _value(
            f"wind_direction_{hours}h_deg", direction, "degrees", label, valid_time,
            station_provenance,
            f"Speed-weighted vector-mean wind direction over the previous {hours} h "
            f"(the direction the wind is blowing FROM).",
            missing,
        )
        values[f"wind_consistency_{hours}h"] = _value(
            f"wind_consistency_{hours}h", consistency, "0-1", label, valid_time, "derived",
            "How consistently the wind held one direction. Low values mean the wind swung around "
            "and no single lee slope was loaded.",
            missing,
        )

        if not temps.empty and temps.notna().any():
            above = int((temps.dropna() > 0.0).sum())
            values[f"hours_above_freezing_{hours}h"] = _value(
                f"hours_above_freezing_{hours}h", float(above), "hours", label, valid_time,
                "derived", f"Hours with air temperature above 0 C in the previous {hours} h.", missing,
            )
            cycles = freeze_thaw_cycles(temps)
            values[f"freeze_thaw_cycles_{hours}h"] = _value(
                f"freeze_thaw_cycles_{hours}h",
                float(cycles) if cycles is not None else None,
                "count", label, valid_time, "derived",
                "Zero-degree crossings. Each cycle can form a melt-freeze crust or weaken bonds.",
                missing,
            )
        else:
            values[f"hours_above_freezing_{hours}h"] = prov.unavailable(
                f"hours_above_freezing_{hours}h", "hours", label, missing
            )
            values[f"freeze_thaw_cycles_{hours}h"] = prov.unavailable(
                f"freeze_thaw_cycles_{hours}h", "count", label, missing
            )

    # --- Composite indices ----------------------------------------------------
    _add_composites(values, hourly, daily, valid_time, label, config, warnings)

    # --- Freshness ------------------------------------------------------------
    latest = source.latest_observation
    if latest is not None:
        age = (valid_time - latest).total_seconds() / 3600.0
        if age > 0:
            values["weather_data_age_hours"] = _value(
                "weather_data_age_hours", age, "hours", label, valid_time, "derived",
                "How stale the newest observation is relative to the requested time.",
                missing,
            )
            if age > 72:
                warnings.append(
                    f"The newest ECCC observation is {age:.0f} h before the requested time. "
                    f"These conditions are stale."
                )
        else:
            values["weather_data_age_hours"] = _value(
                "weather_data_age_hours", 0.0, "hours", label, valid_time, "derived",
                "Observations cover the requested time.", missing,
            )

    if station and station.get("distance_to_aoi_km") is not None:
        values["station_distance_km"] = _value(
            "station_distance_km", station["distance_to_aoi_km"], "km", label, valid_time,
            "derived", "Distance from the nearest weather station to the AOI centroid.", missing,
        )

    conditions = ConditionSet(
        mode=mode,
        valid_time_utc=valid_time,
        values=values,
        station=station,
        warnings=warnings,
        scenario=scenario,
        release_size=scenario.release_size if scenario else "medium",
    )

    if scenario is not None:
        _apply_scenario(conditions, scenario)
    return conditions


def _add_composites(
    values: dict[str, prov.Value],
    hourly: pd.DataFrame,
    daily: pd.DataFrame,
    valid_time: datetime,
    label: str,
    config: ModelConfig,
    warnings: list[str],
) -> None:
    snow_threshold = float(config.get("snow.snow_rain_threshold_c", 1.0))
    rain_threshold = float(config.get("snow.all_rain_threshold_c", 3.0))

    window_72 = _window(hourly, valid_time, 72)
    window_24 = _window(hourly, valid_time, 24)

    # Phase-partition hourly precipitation by the temperature at which it fell.
    # ECCC's hourly feed does not say whether precipitation was snow or rain, and
    # for avalanche purposes that is the single most important thing about it.
    if not window_72.empty and window_72["precipitation_mm"].notna().any():
        temps = window_72["temperature_c"].to_numpy(dtype="float64")
        precip = pd.to_numeric(window_72["precipitation_mm"], errors="coerce").to_numpy(dtype="float64")
        precip = np.where(np.isfinite(precip), precip, 0.0)

        snow_fraction = np.clip(
            (rain_threshold - temps) / max(rain_threshold - snow_threshold, 1e-6), 0.0, 1.0
        )
        snow_fraction = np.where(np.isfinite(temps), snow_fraction, np.nan)

        known = np.isfinite(snow_fraction)
        snow_water = float((precip[known] * snow_fraction[known]).sum()) if known.any() else None
        rain_water = float((precip[known] * (1.0 - snow_fraction[known])).sum()) if known.any() else None

        values["snow_water_equivalent_72h_mm"] = _value(
            "snow_water_equivalent_72h_mm", snow_water, "mm", label, valid_time, "modelled",
            "Precipitation that fell while the air was cold enough to snow, phase-partitioned by "
            "temperature. This is a MODEL, not an observation of snowfall.",
            "No hourly precipitation and temperature pair covered this window.",
        )
        values["rain_72h_mm"] = _value(
            "rain_72h_mm", rain_water, "mm", label, valid_time, "modelled",
            "Precipitation that fell while the air was warm enough to rain. Rain on an existing "
            "snowpack is one of the strongest instability signals we can actually observe.",
            "No hourly precipitation and temperature pair covered this window.",
        )
    else:
        values["snow_water_equivalent_72h_mm"] = prov.unavailable(
            "snow_water_equivalent_72h_mm", "mm", label, "No hourly precipitation in this window."
        )
        values["rain_72h_mm"] = prov.unavailable(
            "rain_72h_mm", "mm", label, "No hourly precipitation in this window."
        )

    # Storm loading: precipitation delivered while it was cold and windy is what
    # builds a wind slab. Rain delivered in a calm thaw is a different problem.
    precip_72 = values.get("precipitation_72h_mm")
    wind_72 = values.get("wind_speed_max_72h_kmh")
    if precip_72 and precip_72.is_available and wind_72 and wind_72.is_available:
        transport = float(config.get("wind.transport_threshold_kmh", 15.0))
        strong = float(config.get("wind.strong_transport_kmh", 40.0))
        wind_factor = np.clip(
            ((wind_72.numeric or 0.0) - transport) / max(strong - transport, 1e-6), 0.0, 1.0
        )
        loading = float(precip_72.numeric or 0.0) * (0.5 + 0.5 * float(wind_factor))
        values["storm_loading_index_mm"] = _value(
            "storm_loading_index_mm", loading, "mm water equivalent", label, valid_time, "modelled",
            "Precipitation over 72 h, amplified by how much wind was available to redistribute it "
            "into lee slopes.",
            "Requires both precipitation and wind.",
        )
    else:
        values["storm_loading_index_mm"] = prov.unavailable(
            "storm_loading_index_mm", "mm water equivalent", label,
            "Requires both 72 h precipitation and 72 h wind speed; at least one is unavailable.",
        )

    # Rapid warming: the 24 h rise, but only the positive part. Cooling is not
    # "negative warming" for this purpose -- it is a different, stabilizing process.
    change_24 = values.get("temperature_change_24h_c")
    if change_24 and change_24.is_available:
        rise = max(0.0, float(change_24.numeric or 0.0))
        values["rapid_warming_24h_c"] = _value(
            "rapid_warming_24h_c", rise, "deg C", label, valid_time, "derived",
            "The warming part of the 24 h temperature change. Rapid warming, especially through "
            "0 C, weakens bonds and can trigger wet-snow release.",
            "No 24 h temperature change available.",
        )
    else:
        values["rapid_warming_24h_c"] = prov.unavailable(
            "rapid_warming_24h_c", "deg C", label, "No 24 h temperature change available."
        )

    # Rain on snow.
    rain = values.get("rain_72h_mm")
    max_temp = values.get("temperature_max_24h_c")
    if rain and rain.is_available and max_temp and max_temp.is_available:
        on_snow = float(rain.numeric or 0.0)
        values["rain_on_snow_mm"] = _value(
            "rain_on_snow_mm", on_snow, "mm", label, valid_time, "modelled",
            "Rain falling on an existing snowpack. It adds load, lubricates weak layers, and "
            "destroys the snowpack's strength faster than any other observable process.",
            "Requires modelled rain and a temperature record.",
        )
    else:
        values["rain_on_snow_mm"] = prov.unavailable(
            "rain_on_snow_mm", "mm", label, "Requires modelled rain and a temperature record."
        )

    since_precip = hours_since(hourly, "precipitation_mm", valid_time, threshold=0.2)
    values["hours_since_precipitation"] = _value(
        "hours_since_precipitation", since_precip, "hours", label, valid_time, "derived",
        "Time since the last measurable precipitation. A long dry spell lets a storm slab settle "
        "and bond.",
        "No precipitation event found in the record before this time.",
    )
    since_snow = hours_since(daily, "snowfall_cm", valid_time, threshold=0.2)
    values["hours_since_snowfall"] = _value(
        "hours_since_snowfall", since_snow, "hours", label, valid_time, "derived",
        "Time since the last reported snowfall.",
        "No reported snowfall found in the record before this time.",
    )

    if not window_24.empty:
        completeness = float(window_24["temperature_c"].notna().mean())
        values["observation_completeness_24h"] = _value(
            "observation_completeness_24h", completeness, "0-1", label, valid_time, "derived",
            "Fraction of the previous 24 hourly slots that actually carried a temperature reading.",
            "No hourly records in the previous 24 h.",
        )
        if completeness < 0.5:
            warnings.append(
                f"Only {completeness:.0%} of the previous 24 hourly weather slots contain data. "
                f"Window statistics are computed from a sparse record."
            )
    else:
        values["observation_completeness_24h"] = prov.unavailable(
            "observation_completeness_24h", "0-1", label, "No hourly records in the previous 24 h."
        )


def _apply_scenario(conditions: ConditionSet, scenario: ScenarioInput) -> None:
    """Overwrite observed values with the user's hypothesis, marking each as such.

    The overwritten values keep their names, so the models behave identically --
    but every one of them now reports `user_supplied`, and the UI renders them
    with a distinct marker. A scenario result can never be mistaken for an
    observation-driven one.
    """
    note = "Entered by the user as a hypothetical scenario. This is not a measurement."

    mapping: list[tuple[str | None, float | None, str]] = [
        ("snowfall_24h_cm", scenario.snowfall_24h_cm, "cm"),
        ("snowfall_48h_cm", scenario.snowfall_48h_cm, "cm"),
        ("snowfall_72h_cm", scenario.snowfall_72h_cm, "cm"),
        ("rainfall_24h_mm", scenario.rain_24h_mm, "mm"),
        ("temperature_mean_24h_c", scenario.temperature_c, "deg C"),
        ("temperature_change_24h_c", scenario.temperature_change_24h_c, "deg C"),
        ("wind_speed_max_72h_kmh", scenario.wind_speed_kmh, "km/h"),
        ("wind_speed_max_24h_kmh", scenario.wind_speed_kmh, "km/h"),
        ("wind_direction_72h_deg", scenario.wind_direction_deg, "degrees"),
        ("wind_direction_24h_deg", scenario.wind_direction_deg, "degrees"),
        ("wind_gust_max_72h_kmh", scenario.wind_gust_kmh, "km/h"),
        ("snow_depth_index", scenario.snow_depth_index, "0-1"),
        ("swe_index", scenario.swe_index, "0-1"),
    ]

    for name, value, units in mapping:
        if name is None or value is None:
            continue
        conditions.values[name] = prov.user_supplied(name, float(value), units, note)

    if scenario.wind_direction_deg is not None:
        # A user who states a wind direction is stating a steady wind.
        for hours in WINDOWS_HOURS:
            conditions.values[f"wind_consistency_{hours}h"] = prov.user_supplied(
                f"wind_consistency_{hours}h", 1.0, "0-1",
                "Implied by a user-specified steady wind direction.",
            )

    if scenario.temperature_change_24h_c is not None:
        conditions.values["rapid_warming_24h_c"] = prov.user_supplied(
            "rapid_warming_24h_c", max(0.0, float(scenario.temperature_change_24h_c)), "deg C", note
        )

    if scenario.rain_24h_mm is not None:
        conditions.values["rain_on_snow_mm"] = prov.user_supplied(
            "rain_on_snow_mm", float(scenario.rain_24h_mm), "mm", note
        )
        conditions.values["rain_72h_mm"] = prov.user_supplied(
            "rain_72h_mm", float(scenario.rain_24h_mm), "mm", note
        )

    if scenario.freeze_thaw:
        conditions.values["freeze_thaw_cycles_24h"] = prov.user_supplied(
            "freeze_thaw_cycles_24h", 2.0, "count", note
        )

    conditions.warnings.append(
        "SCENARIO MODE. The values below marked 'user supplied' are hypothetical inputs, not "
        "observations. This result describes what the model would say IF those conditions held."
    )


# --- The three entry points ---------------------------------------------------


def replay_conditions(
    settings: Settings, config: ModelConfig, at: datetime, source: WeatherSource | None = None
) -> ConditionSet:
    """Historical replay: conditions as the instruments recorded them at ``at``."""
    source = source or load_weather(settings)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    conditions = build_conditions(settings, config, source, at, Mode.HISTORICAL)

    latest = source.latest_observation
    earliest = min(
        (frame["timestamp_utc"].min() for frame in (source.hourly, source.daily) if not frame.empty),
        default=None,
    )
    if earliest is not None and at < earliest.to_pydatetime():
        conditions.warnings.append(
            f"The requested time {at.isoformat()} is BEFORE the start of the weather record "
            f"({earliest.isoformat()}). No observations cover it."
        )
    if latest is not None and at > latest + timedelta(hours=1):
        conditions.warnings.append(
            f"The requested time {at.isoformat()} is AFTER the end of the weather record "
            f"({latest.isoformat()}). No observations cover it."
        )
    return conditions


def current_conditions(
    settings: Settings, config: ModelConfig, source: WeatherSource | None = None
) -> ConditionSet:
    """Most recent valid observations, with their age stated plainly."""
    source = source or load_weather(settings)
    latest = source.latest_observation
    if latest is None:
        now = datetime.now(timezone.utc)
        conditions = build_conditions(settings, config, source, now, Mode.CURRENT)
        conditions.warnings.append(
            "No weather observations exist at all, so 'current conditions' cannot be assembled."
        )
        return conditions

    conditions = build_conditions(settings, config, source, latest, Mode.CURRENT)
    age = (datetime.now(timezone.utc) - latest).total_seconds() / 3600.0
    conditions.warnings.append(
        f"'Current' conditions are the most recent VALID observations, from {latest.isoformat()} "
        f"-- {age:.0f} hours ago. This dataset is a fixed historical download covering "
        f"2025-11-01 to 2026-05-31; it is not a live feed."
    )
    return conditions


def scenario_conditions(
    settings: Settings,
    config: ModelConfig,
    scenario: ScenarioInput,
    base_time: datetime | None = None,
    source: WeatherSource | None = None,
) -> ConditionSet:
    """A user-defined hypothetical, layered over a real baseline where one exists."""
    source = source or load_weather(settings)
    anchor = base_time or source.latest_observation or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return build_conditions(settings, config, source, anchor, Mode.SCENARIO, scenario=scenario)
