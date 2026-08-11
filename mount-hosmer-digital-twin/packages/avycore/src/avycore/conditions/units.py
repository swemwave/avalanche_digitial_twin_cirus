"""Controlled unit conversion for normalized Condition Pack variables.

Conversions are deliberately explicit and small. Provider adapters must name a
supported source unit; no unit is guessed from a value or provider name.
"""

from __future__ import annotations

import math
from typing import Final


CANONICAL_UNITS: Final[dict[str, str]] = {
    "air_temperature": "K",
    "relative_humidity": "%",
    "wind_speed": "m s-1",
    "wind_direction": "degree_true",
    "precipitation_phase": "category",
    "precipitation_amount": "kg m-2 h-1",
    "surface_pressure": "Pa",
    "shortwave_radiation": "W m-2",
    "longwave_radiation": "W m-2",
}


class UnitConversionError(ValueError):
    """Raised when a source unit cannot be converted without guessing."""


def _finite(value: float) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise UnitConversionError("Condition values must be finite when present.")
    return converted


def convert_value(variable: str, value: float | str | None, source_unit: str) -> float | str | None:
    """Convert one provider value to the contract's canonical unit.

    ``None`` is returned unchanged so a provider gap cannot become a numerical
    zero during normalization.
    """

    if variable not in CANONICAL_UNITS:
        raise UnitConversionError(f"Unknown Condition Pack variable {variable!r}.")
    if value is None:
        return None
    unit = source_unit.strip()

    if variable == "precipitation_phase":
        if unit != "category" or not isinstance(value, str):
            raise UnitConversionError("Precipitation phase requires a categorical string value.")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UnitConversionError(f"{variable} requires a numeric value.")

    numeric = _finite(value)
    canonical = CANONICAL_UNITS[variable]
    if variable == "wind_direction" and unit == "degree_true":
        converted = numeric % 360.0
    elif unit == canonical:
        converted = numeric
    elif variable == "air_temperature" and unit == "degC":
        converted = numeric + 273.15
    elif variable == "air_temperature" and unit == "degF":
        converted = (numeric - 32.0) * (5.0 / 9.0) + 273.15
    elif variable == "relative_humidity" and unit == "fraction":
        converted = numeric * 100.0
    elif variable == "wind_speed" and unit == "km h-1":
        converted = numeric / 3.6
    elif variable == "precipitation_amount" and unit == "mm h-1":
        # One millimetre of liquid-water equivalent over one square metre has
        # a mass of one kilogram. This does not infer phase or undercatch.
        converted = numeric
    elif variable == "surface_pressure" and unit == "hPa":
        converted = numeric * 100.0
    elif variable == "surface_pressure" and unit == "kPa":
        converted = numeric * 1000.0
    elif variable in {"shortwave_radiation", "longwave_radiation"} and unit == "kW m-2":
        converted = numeric * 1000.0
    else:
        raise UnitConversionError(
            f"Unsupported unit conversion for {variable}: {unit!r} -> {canonical!r}."
        )
    return _finite(converted)
