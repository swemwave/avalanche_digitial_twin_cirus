"""Strict ConditionPack to SNOWPACK/MeteoIO SMET forcing adapter."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from avycore.conditions import ConditionPack


SMET_ADAPTER_VERSION = "condition-pack-to-smet-v1"
SMET_FIELDS = ("timestamp", "TA", "RH", "VW", "DW", "ISWR", "ILWR", "PSUM", "PSUM_PH", "P")
VARIABLES = {
    "TA": "air_temperature",
    "RH": "relative_humidity",
    "VW": "wind_speed",
    "DW": "wind_direction",
    "ISWR": "shortwave_radiation",
    "ILWR": "longwave_radiation",
    "PSUM": "precipitation_amount",
    "P": "surface_pressure",
}


class SmetAdapterError(ValueError):
    """Raised rather than filling, interpolating, or guessing required forcing."""


@dataclass(frozen=True)
class SmetTerrain:
    longitude_deg: float
    latitude_deg: float
    elevation_m: float
    slope_angle_deg: float
    slope_aspect_deg_true: float

    def __post_init__(self) -> None:
        values = (
            self.longitude_deg, self.latitude_deg, self.elevation_m,
            self.slope_angle_deg, self.slope_aspect_deg_true,
        )
        if not all(math.isfinite(value) for value in values):
            raise SmetAdapterError("SMET terrain values must be finite.")
        if not -180 <= self.longitude_deg <= 180 or not -90 <= self.latitude_deg <= 90:
            raise SmetAdapterError("SMET geographic coordinates are invalid or swapped.")
        if not 0 <= self.slope_angle_deg <= 90:
            raise SmetAdapterError("SMET slope angle must be in [0,90] degrees.")
        if not 0 <= self.slope_aspect_deg_true < 360:
            raise SmetAdapterError("SMET slope aspect must be [0,360) degrees true.")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise SmetAdapterError("SMET cannot contain non-finite values.")
    return format(value, ".17g")


def _phase_fraction(phase: str, precipitation: float) -> float:
    if phase == "snow":
        return 0.0
    if phase in {"rain", "freezing_rain"}:
        return 1.0
    if phase == "mixed":
        return 0.5
    if phase == "none" and precipitation == 0.0:
        return 0.0
    raise SmetAdapterError(
        "Precipitation phase is unknown/inconsistent; no phase threshold or guess is allowed."
    )


def condition_pack_to_smet(
    pack: ConditionPack,
    terrain: SmetTerrain,
    *,
    station_id: str = "mount-hosmer-offline",
) -> bytes:
    """Render complete hourly forcing or reject it without gap filling.

    RH is converted from percent to fraction.  Hourly kg m-2 h-1 is written as
    the numerically equivalent millimetres accumulated over the preceding
    one-hour timestep.  PSUM_PH is liquid fraction, not a categorical code.
    """

    if len(pack.stations) != 1:
        raise SmetAdapterError("The v1 adapter requires exactly one forcing station series.")
    if not station_id or any(char.isspace() for char in station_id):
        raise SmetAdapterError("SMET station_id must be non-empty and contain no whitespace.")
    timeline = pack.times.hourly_timestamps()
    series_maps = {
        name: {item.time_utc: item for item in pack.variables[name].values}
        for name in set(VARIABLES.values()) | {"precipitation_phase"}
    }
    lines = [
        "SMET 1.1 ASCII",
        "[HEADER]",
        f"station_id = {station_id}",
        "station_name = Mount Hosmer isolated offline snow-model input",
        f"latitude = {_number(terrain.latitude_deg)}",
        f"longitude = {_number(terrain.longitude_deg)}",
        f"altitude = {_number(terrain.elevation_m)}",
        f"slope_angle = {_number(terrain.slope_angle_deg)}",
        f"slope_azi = {_number(terrain.slope_aspect_deg_true)}",
        "nodata = -999",
        "tz = 0",
        f"source = ConditionPack {pack.condition_id}; {SMET_ADAPTER_VERSION}",
        f"fields = {' '.join(SMET_FIELDS)}",
        "[DATA]",
    ]
    for timestamp in timeline:
        row: dict[str, float] = {}
        for smet_name, variable in VARIABLES.items():
            item = series_maps[variable].get(timestamp)
            if item is None or item.masked or item.value is None or item.status in {"missing", "gap_filled"}:
                raise SmetAdapterError(
                    f"Required {variable} forcing is missing or gap-filled at {timestamp.isoformat()}."
                )
            row[smet_name] = float(item.value)
        phase_item = series_maps["precipitation_phase"].get(timestamp)
        if phase_item is None or phase_item.masked or phase_item.value is None:
            raise SmetAdapterError(f"Required precipitation phase is missing at {timestamp.isoformat()}.")
        row["RH"] /= 100.0
        if not 0 <= row["RH"] <= 1:
            raise SmetAdapterError("Relative humidity conversion produced a value outside [0,1].")
        row["PSUM_PH"] = _phase_fraction(str(phase_item.value), row["PSUM"])
        rendered = [timestamp.strftime("%Y-%m-%dT%H:%M:%S")]
        rendered.extend(_number(row[name]) for name in SMET_FIELDS[1:])
        lines.append(" ".join(rendered))
    return ("\n".join(lines) + "\n").encode("ascii")


def smet_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
