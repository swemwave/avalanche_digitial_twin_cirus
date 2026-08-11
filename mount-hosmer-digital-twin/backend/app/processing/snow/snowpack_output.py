"""Strict parser for real SNOWPACK 3.7.0 SMET time-series output."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


PARSER_VERSION = "snowpack-3.7.0-smet-output-v1"
VOLATILE_HEADER_KEYS = frozenset({"creator_name", "creator_type", "date_created", "history"})
SNOW_STATE_FIELDS = {
    "snow_height": ("HS_mod", "m", "modeled vertical snow height"),
    "snow_water_equivalent": ("SWE", "kg m-2", "modeled snowpack mass per horizontal area"),
    "surface_temperature": ("TSS_mod", "K", "modeled snow surface temperature"),
}


class SnowpackOutputError(ValueError):
    """Raised when official output is ambiguous, incomplete, or nonphysical."""


@dataclass(frozen=True)
class ParsedSnowpackSmet:
    header: dict[str, str]
    fields: tuple[str, ...]
    timestamps_utc: tuple[datetime, ...]
    values: dict[str, tuple[float | None, ...]]
    raw_sha256: str
    normalized_sha256: str


def _parse_vector(header: dict[str, str], key: str, count: int) -> tuple[float, ...]:
    try:
        values = tuple(float(item) for item in header[key].split())
    except (KeyError, ValueError) as exc:
        raise SnowpackOutputError(f"Missing or invalid {key!r} vector.") from exc
    if len(values) != count or not all(math.isfinite(item) for item in values):
        raise SnowpackOutputError(f"{key!r} must contain one finite value per field.")
    return values


def _canonical_normalized_bytes(
    header: dict[str, str],
    fields: tuple[str, ...],
    timestamps: tuple[datetime, ...],
    values: dict[str, tuple[float | None, ...]],
) -> bytes:
    stable_header = {
        key: value for key, value in header.items() if key not in VOLATILE_HEADER_KEYS
    }
    payload = {
        "parser_version": PARSER_VERSION,
        "header": stable_header,
        "fields": fields,
        "timestamps_utc": [item.isoformat().replace("+00:00", "Z") for item in timestamps],
        "physical_values": values,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def parse_snowpack_smet(content: bytes) -> ParsedSnowpackSmet:
    """Parse physical values while preserving declared units, time, and nodata."""

    raw_sha256 = hashlib.sha256(content).hexdigest()
    try:
        lines = content.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise SnowpackOutputError("SNOWPACK SMET output is not valid UTF-8.") from exc
    if not lines or lines[0].strip() not in {"SMET 1.1 ASCII", "SMET 1.0 ASCII"}:
        raise SnowpackOutputError("Unsupported or missing ASCII SMET signature.")
    try:
        header_start = next(i for i, line in enumerate(lines) if line.strip() == "[HEADER]")
        data_start = next(i for i, line in enumerate(lines) if line.strip() == "[DATA]")
    except StopIteration as exc:
        raise SnowpackOutputError("SMET output must contain HEADER and DATA sections.") from exc
    if data_start <= header_start:
        raise SnowpackOutputError("SMET sections are out of order.")

    header: dict[str, str] = {}
    for raw in lines[header_start + 1 : data_start]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise SnowpackOutputError(f"Malformed SMET header line: {raw!r}.")
        key, value = (part.strip() for part in stripped.split("=", 1))
        if not key or not value or key in header:
            raise SnowpackOutputError(f"Duplicate or empty SMET header key {key!r}.")
        header[key] = value
    try:
        fields = tuple(header["fields"].split())
        nodata = float(header["nodata"])
        timezone_hours = float(header["tz"])
    except (KeyError, ValueError) as exc:
        raise SnowpackOutputError("SMET fields, nodata, and tz declarations are required.") from exc
    if not fields or fields[0] != "timestamp" or len(set(fields)) != len(fields):
        raise SnowpackOutputError("SMET fields must be unique and begin with timestamp.")
    if not all(math.isfinite(item) for item in (nodata, timezone_hours)):
        raise SnowpackOutputError("SMET nodata and time-zone offset must be finite.")
    multipliers = _parse_vector(header, "units_multiplier", len(fields))
    offsets = _parse_vector(header, "units_offset", len(fields))

    timestamps: list[datetime] = []
    columns: dict[str, list[float | None]] = {field: [] for field in fields[1:]}
    for line_number, raw in enumerate(lines[data_start + 1 :], start=data_start + 2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != len(fields):
            raise SnowpackOutputError(
                f"SMET row {line_number} has {len(parts)} values; expected {len(fields)}."
            )
        try:
            local_time = datetime.fromisoformat(parts[0])
        except ValueError as exc:
            raise SnowpackOutputError(f"Invalid SMET timestamp on row {line_number}.") from exc
        if local_time.tzinfo is not None:
            raise SnowpackOutputError("SMET timestamp must be naive when the tz header is present.")
        utc_time = (local_time - timedelta(hours=timezone_hours)).replace(tzinfo=timezone.utc)
        if timestamps and utc_time <= timestamps[-1]:
            raise SnowpackOutputError("SMET timestamps must be strictly increasing without duplicates.")
        timestamps.append(utc_time)
        for index, field in enumerate(fields[1:], start=1):
            try:
                raw_value = float(parts[index])
            except ValueError as exc:
                raise SnowpackOutputError(
                    f"Non-numeric SMET value for {field!r} on row {line_number}."
                ) from exc
            if not math.isfinite(raw_value):
                raise SnowpackOutputError("SMET data cannot contain non-finite numeric tokens.")
            if raw_value == nodata:
                columns[field].append(None)
            else:
                physical = raw_value * multipliers[index] + offsets[index]
                if not math.isfinite(physical):
                    raise SnowpackOutputError("SMET unit conversion produced a non-finite value.")
                columns[field].append(physical)
    if not timestamps:
        raise SnowpackOutputError("SMET output contains no data records.")

    frozen_values = {field: tuple(values) for field, values in columns.items()}
    frozen_times = tuple(timestamps)
    normalized = _canonical_normalized_bytes(header, fields, frozen_times, frozen_values)
    return ParsedSnowpackSmet(
        header=header,
        fields=fields,
        timestamps_utc=frozen_times,
        values=frozen_values,
        raw_sha256=raw_sha256,
        normalized_sha256=hashlib.sha256(normalized).hexdigest(),
    )


def snow_state_variables(parsed: ParsedSnowpackSmet) -> dict[str, dict[str, object]]:
    """Extract the limited SnowStatePack v2 fields from declared physical output."""

    if any(
        item.minute or item.second or item.microsecond for item in parsed.timestamps_utc
    ):
        raise SnowpackOutputError(
            "SnowStatePack output timestamps must resolve to exact UTC hours."
        )
    result: dict[str, dict[str, object]] = {}
    for variable, (field, unit, semantics) in SNOW_STATE_FIELDS.items():
        if field not in parsed.values:
            raise SnowpackOutputError(f"Required real SNOWPACK output field {field!r} is absent.")
        values = parsed.values[field]
        rows: list[dict[str, object]] = []
        for timestamp, value in zip(parsed.timestamps_utc, values, strict=True):
            if value is None:
                rows.append(
                    {
                        "time_utc": timestamp,
                        "value": None,
                        "masked": True,
                        "status": "missing",
                        "output_field": field,
                    }
                )
                continue
            if variable in {"snow_height", "snow_water_equivalent"} and value < 0:
                raise SnowpackOutputError(f"{field} contains a negative physical value.")
            if variable == "surface_temperature" and value <= 0:
                raise SnowpackOutputError("TSS_mod is not a positive kelvin temperature.")
            rows.append(
                {
                    "time_utc": timestamp,
                    "value": value,
                    "masked": False,
                    "status": "modeled",
                    "output_field": field,
                }
            )
        result[variable] = {
            "variable": variable,
            "unit": unit,
            "semantics": semantics,
            "values": rows,
        }
    return result


def require_exact_cadence(
    timestamps: Iterable[datetime], *, cadence_seconds: int = 3600
) -> None:
    ordered = tuple(timestamps)
    if cadence_seconds <= 0 or not ordered:
        raise SnowpackOutputError("Cadence and timestamp series must be non-empty and positive.")
    expected = timedelta(seconds=cadence_seconds)
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous != expected:
            raise SnowpackOutputError(
                f"SNOWPACK output cadence is not exactly {cadence_seconds} seconds."
            )
