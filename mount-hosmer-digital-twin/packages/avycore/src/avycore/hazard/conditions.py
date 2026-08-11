"""Condition inputs shared by AvyCore's deterministic and assistant APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

RELEASE_SIZES = ("small", "medium", "large", "very_large")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

#: Flow regimes the runout engines can be pointed at. ``dry_slab`` is the historical
#: default and the only one the previous configuration described.
FLOW_REGIMES = ("dry_slab", "wet_snow", "powder", "mixed")

#: Rain/snow classification band. This is the SAME 0-2 degC air-temperature band the
#: offline M2 forcing work already uses, reused here rather than invented: at or
#: below 0 degC all new precipitation counts as snow, at or above 2 degC all of it
#: counts as rain, and it interpolates between. It is a temperature classification,
#: NOT a phase observation.
RAIN_SNOW_LOWER_C = 0.0
RAIN_SNOW_UPPER_C = 2.0


def snow_fraction(air_temperature_c: float | None) -> float:
    """Fraction of new precipitation classified as snow, from air temperature.

    Returns 1.0 when temperature is unknown, so an unspecified temperature
    reproduces the historical behaviour exactly rather than assuming a warm day.
    """
    if air_temperature_c is None:
        return 1.0
    value = float(air_temperature_c)
    if value <= RAIN_SNOW_LOWER_C:
        return 1.0
    if value >= RAIN_SNOW_UPPER_C:
        return 0.0
    return float(
        (RAIN_SNOW_UPPER_C - value) / (RAIN_SNOW_UPPER_C - RAIN_SNOW_LOWER_C)
    )


def compass_name(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return COMPASS[int(((degrees % 360.0) + 11.25) // 22.5) % 16]


@dataclass
class Conditions:
    """User-supplied model conditions; wind direction uses the FROM convention."""

    new_snow_cm: float = 0.0
    wind_speed_kmh: float = 0.0
    wind_direction_deg: float = 225.0
    release_size: str = "medium"
    #: Optional. Every one of these defaults to None, and None reproduces the
    #: historical numbers exactly -- an unspecified value is never a default guess.
    air_temperature_c: float | None = None
    flow_regime: str | None = None
    alpha_angle_override_deg: float | None = None

    @property
    def snow_fraction(self) -> float:
        """Share of ``new_snow_cm`` classified as snow rather than rain."""
        return snow_fraction(self.air_temperature_c)

    @property
    def effective_new_snow_cm(self) -> float:
        """The dry-slab loading depth after the rain/snow classification."""
        return self.new_snow_cm * self.snow_fraction

    def clamped(self) -> "Conditions":
        numeric = (self.new_snow_cm, self.wind_speed_kmh, self.wind_direction_deg)
        if not all(np.isfinite(value) for value in numeric):
            raise ValueError("Condition values must be finite numbers.")
        if self.release_size not in RELEASE_SIZES:
            raise ValueError(
                f"Unknown release size {self.release_size!r}; expected one of {RELEASE_SIZES}."
            )
        if self.air_temperature_c is not None and not np.isfinite(self.air_temperature_c):
            raise ValueError("Air temperature must be a finite number when supplied.")
        if self.flow_regime is not None and self.flow_regime not in FLOW_REGIMES:
            raise ValueError(
                f"Unknown flow regime {self.flow_regime!r}; expected one of {FLOW_REGIMES}."
            )
        if self.alpha_angle_override_deg is not None and not np.isfinite(
            self.alpha_angle_override_deg
        ):
            raise ValueError("Alpha-angle override must be a finite number when supplied.")
        return Conditions(
            new_snow_cm=float(np.clip(self.new_snow_cm, 0.0, 300.0)),
            wind_speed_kmh=float(np.clip(self.wind_speed_kmh, 0.0, 200.0)),
            wind_direction_deg=float(self.wind_direction_deg % 360.0),
            release_size=self.release_size,
            air_temperature_c=(
                None if self.air_temperature_c is None
                else float(np.clip(self.air_temperature_c, -60.0, 40.0))
            ),
            flow_regime=self.flow_regime,
            alpha_angle_override_deg=(
                None if self.alpha_angle_override_deg is None
                else float(self.alpha_angle_override_deg)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "new_snow_cm": round(self.new_snow_cm, 1),
            "wind_speed_kmh": round(self.wind_speed_kmh, 1),
            "wind_direction_deg": round(self.wind_direction_deg, 1),
            "wind_direction_compass": compass_name(self.wind_direction_deg),
            "release_size": self.release_size,
            "provenance": "user_supplied",
            "air_temperature_c": (
                None if self.air_temperature_c is None else round(self.air_temperature_c, 1)
            ),
            "flow_regime": self.flow_regime,
            "alpha_angle_override_deg": (
                None if self.alpha_angle_override_deg is None
                else round(self.alpha_angle_override_deg, 1)
            ),
        }
        if self.air_temperature_c is not None:
            payload["precipitation_snow_fraction"] = round(self.snow_fraction, 3)
            payload["effective_new_snow_cm"] = round(self.effective_new_snow_cm, 1)
        return payload
