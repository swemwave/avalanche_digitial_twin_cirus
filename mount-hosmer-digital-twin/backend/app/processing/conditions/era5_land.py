"""Strict offline ERA5-Land request and interval-transformation primitives.

This module deliberately does not import ``cdsapi`` or GRIB tooling. Acquisition
and decoding are offline concerns, and callers must first prove CDS access and
the exact response contract. The pure functions here characterize the full
hourly GRIB product described by the official ERA5-Land documentation; they are
not valid for the already-deaccumulated ARCO subset.
"""

from __future__ import annotations

import calendar
import hashlib
import importlib.util
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


UTC = timezone.utc
ERA5_LAND_DATASET = "reanalysis-era5-land"
ERA5_LAND_CONTRACT_VERSION = "era5-land-full-grib-hourly-v1"
ERA5_LAND_DOI = "10.24381/cds.e2161bac"
ERA5_LAND_LICENCE = "CC-BY-4.0"
CONSOLIDATED_EXPVER = "0001"
ERA5_LAND_VARIABLES = (
    "2m_temperature",
    "2m_dewpoint_temperature",
    "surface_pressure",
    "total_precipitation",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_solar_radiation_downwards",
    "surface_thermal_radiation_downwards",
    "soil_temperature_level_1",
)

# ECMWF IFS saturation-vapour-pressure constants for liquid water. ERA5's
# near-surface RH guidance uses saturation over water for this derivation.
IFS_ES_A1_PA = 611.21
IFS_ES_A3_WATER = 17.502
IFS_ES_A4_WATER_K = 32.19
IFS_TRIPLE_POINT_K = 273.16
RH_METHOD_VERSION = "ecmwf-ifs-cy48r1-water-v1"


class Era5LandError(ValueError):
    """Raised when request, time, missingness, or physical contracts fail."""


@dataclass(frozen=True)
class CdsAccessAudit:
    """Credential-safe access facts; credential values are never read or returned."""

    config_file_present: bool
    url_environment_present: bool
    key_environment_present: bool
    cdsapi_installed: bool

    @property
    def locally_configured(self) -> bool:
        return self.config_file_present or (
            self.url_environment_present and self.key_environment_present
        )


@dataclass(frozen=True)
class AccumulatedValue:
    """One exact ERA5 validity-hour accumulation, preserving missingness."""

    time_utc: datetime
    value: float | None
    masked: bool

    def __post_init__(self) -> None:
        _require_exact_utc_hour(self.time_utc, "time_utc")
        if self.value is None:
            if not self.masked:
                raise Era5LandError("Missing accumulated values must be explicitly masked.")
        elif self.masked or not math.isfinite(self.value):
            raise Era5LandError("Present accumulated values must be finite and unmasked.")


@dataclass(frozen=True)
class IntervalValue:
    """One deaccumulated preceding-hour interval plus explicit QC status."""

    time_utc: datetime
    value: float | None
    masked: bool
    status: str
    accumulated_value: float | None
    previous_accumulated_value: float | None


@dataclass(frozen=True)
class DailyReconstruction:
    """Reconstruction of one 00-UTC forecast's documented step-24 total."""

    forecast_start_utc: datetime
    interval_sum: float
    step_24_accumulation: float
    absolute_difference: float


def _require_exact_utc_hour(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise Era5LandError(f"{name} must be timezone-aware UTC.")
    if value.minute or value.second or value.microsecond:
        raise Era5LandError(f"{name} must lie exactly on a UTC hour.")


def audit_cds_access(*, home: str | Path | None = None) -> CdsAccessAudit:
    """Audit only the presence of CDS configuration without reading secrets."""

    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    return CdsAccessAudit(
        config_file_present=(home_path / ".cdsapirc").is_file(),
        url_environment_present=bool(os.environ.get("CDSAPI_URL")),
        key_environment_present=bool(os.environ.get("CDSAPI_KEY")),
        cdsapi_installed=importlib.util.find_spec("cdsapi") is not None,
    )


def build_monthly_request_manifest(
    start_utc: datetime,
    end_utc: datetime,
    *,
    area: tuple[float, float, float, float],
) -> dict[str, Any]:
    """Build deterministic credential-free monthly full-product requests.

    ``area`` is ordered north, west, south, east as required by CDS. Requests
    cover whole calendar months; normalization must mask hours outside the
    requested validity window and must use the preceding accumulation where a
    00 UTC interval lies on the output boundary.
    """

    _require_exact_utc_hour(start_utc, "start_utc")
    _require_exact_utc_hour(end_utc, "end_utc")
    if end_utc < start_utc:
        raise Era5LandError("end_utc must not precede start_utc.")
    north, west, south, east = area
    if not all(math.isfinite(item) for item in area):
        raise Era5LandError("CDS area coordinates must be finite.")
    if not (-90 <= south <= north <= 90 and -180 <= west <= east <= 180):
        raise Era5LandError("CDS area must be ordered north, west, south, east.")

    requests: list[dict[str, Any]] = []
    year, month = start_utc.year, start_utc.month
    while (year, month) <= (end_utc.year, end_utc.month):
        days = calendar.monthrange(year, month)[1]
        requests.append(
            {
                "target_filename": f"era5_land_{year}{month:02d}.grib",
                "request": {
                    "variable": list(ERA5_LAND_VARIABLES),
                    "year": str(year),
                    "month": f"{month:02d}",
                    "day": [f"{day:02d}" for day in range(1, days + 1)],
                    "time": [f"{hour:02d}:00" for hour in range(24)],
                    "area": [north, west, south, east],
                    "data_format": "grib",
                    "download_format": "unarchived",
                },
            }
        )
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return {
        "schema": "era5-land-request-manifest-v1",
        "transformation_contract": ERA5_LAND_CONTRACT_VERSION,
        "dataset": ERA5_LAND_DATASET,
        "doi": ERA5_LAND_DOI,
        "licence": ERA5_LAND_LICENCE,
        "expected_consolidated_expver": CONSOLIDATED_EXPVER,
        "valid_start_utc": start_utc.isoformat().replace("+00:00", "Z"),
        "valid_end_utc": end_utc.isoformat().replace("+00:00", "Z"),
        "requests": requests,
        "invariant_geopotential_included": False,
        "notes": [
            "No credential is present in this manifest.",
            "Geopotential is an invariant auxiliary field and requires a separately verified request.",
            "Returned grid coordinates, grid elevation, and expver must be read from the response.",
        ],
    }


def canonical_request_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialize an acquisition manifest reproducibly without provider secrets."""

    return (
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def write_request_manifest(manifest: dict[str, Any], runtime_root: str | Path) -> Path:
    """Atomically store one credential-free, content-addressed CDS request."""

    content = canonical_request_manifest_bytes(manifest)
    digest = hashlib.sha256(content).hexdigest()
    request_id = f"request-{digest}"
    root = Path(runtime_root).resolve() / "sources" / "conditions" / "era5-land"
    root.mkdir(parents=True, exist_ok=True)
    target = root / request_id
    expected_checksums = canonical_request_manifest_bytes(
        {
            "schema": "era5-land-request-storage-v1",
            "request_id": request_id,
            "files": {
                "request-manifest.json": {"bytes": len(content), "sha256": digest}
            },
        }
    )
    if target.exists():
        if not target.is_dir():
            raise Era5LandError(f"ERA5 request target is not a directory: {target}")
        manifest_path = target / "request-manifest.json"
        checksums_path = target / "checksums.json"
        if (
            not manifest_path.is_file()
            or not checksums_path.is_file()
            or manifest_path.read_bytes() != content
            or checksums_path.read_bytes() != expected_checksums
        ):
            raise Era5LandError("Existing ERA5 request artifact is incomplete or conflicting.")
        return target

    staging = root / f".request-build-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for filename, payload in (
            ("request-manifest.json", content),
            ("checksums.json", expected_checksums),
        ):
            with (staging / filename).open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        staging.rename(target)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def deaccumulate_full_product(
    accumulated: Iterable[AccumulatedValue],
    *,
    negative_noise_tolerance: float = 1e-12,
) -> tuple[IntervalValue, ...]:
    """Convert full-product validity-time accumulations to hourly intervals.

    At 01 UTC the current value is forecast step 1 and is already the hourly
    amount. At all other validity hours, including 00 UTC (the preceding day's
    step 24), the interval is current minus the exact previous validity hour.
    Missing input remains missing. Small negative floating-point noise is set to
    zero with an explicit status; larger negative increments are rejected.
    """

    if not math.isfinite(negative_noise_tolerance) or negative_noise_tolerance < 0:
        raise Era5LandError("negative_noise_tolerance must be finite and non-negative.")
    ordered = sorted(accumulated, key=lambda item: item.time_utc)
    if len({item.time_utc for item in ordered}) != len(ordered):
        raise Era5LandError("ERA5 accumulated series contains duplicate UTC timestamps.")
    by_time = {item.time_utc: item for item in ordered}
    result: list[IntervalValue] = []
    for current in ordered:
        previous: AccumulatedValue | None = None
        if current.time_utc.hour != 1:
            previous = by_time.get(current.time_utc - timedelta(hours=1))
        if current.value is None or (
            current.time_utc.hour != 1
            and (previous is None or previous.value is None)
        ):
            result.append(
                IntervalValue(
                    time_utc=current.time_utc,
                    value=None,
                    masked=True,
                    status="missing_source_interval",
                    accumulated_value=current.value,
                    previous_accumulated_value=None if previous is None else previous.value,
                )
            )
            continue
        value = current.value
        previous_value: float | None = None
        if current.time_utc.hour != 1:
            assert previous is not None and previous.value is not None
            previous_value = previous.value
            value -= previous.value
        status = "deaccumulated"
        if value < -negative_noise_tolerance:
            raise Era5LandError(
                f"Negative interval {value!r} at {current.time_utc.isoformat()} exceeds "
                f"the explicit tolerance {negative_noise_tolerance!r}."
            )
        if value < 0:
            value = 0.0
            status = "negative_roundoff_adjusted_to_zero"
        result.append(
            IntervalValue(
                time_utc=current.time_utc,
                value=value,
                masked=False,
                status=status,
                accumulated_value=current.value,
                previous_accumulated_value=previous_value,
            )
        )
    return tuple(result)


def reconstruct_step_24(
    intervals: Iterable[IntervalValue],
    *,
    tolerance: float = 1e-9,
) -> tuple[DailyReconstruction, ...]:
    """Prove each complete 24-interval day reconstructs its step-24 value."""

    if not math.isfinite(tolerance) or tolerance < 0:
        raise Era5LandError("tolerance must be finite and non-negative.")
    materialized = tuple(intervals)
    by_time = {item.time_utc: item for item in materialized}
    if len(by_time) != len(materialized):
        raise Era5LandError("Interval series contains duplicate UTC timestamps.")
    starts = sorted(
        timestamp for timestamp in by_time if timestamp.hour == 1
    )
    reconstructions: list[DailyReconstruction] = []
    for first_validity in starts:
        forecast_start = first_validity - timedelta(hours=1)
        timestamps = tuple(first_validity + timedelta(hours=index) for index in range(24))
        values = [by_time.get(timestamp) for timestamp in timestamps]
        if any(item is None or item.masked or item.value is None for item in values):
            continue
        step_24 = by_time[timestamps[-1]].accumulated_value
        if step_24 is None:
            continue
        interval_sum = math.fsum(item.value for item in values if item is not None and item.value is not None)
        difference = abs(interval_sum - step_24)
        if difference > tolerance:
            raise Era5LandError(
                f"Hourly intervals for {forecast_start.date()} do not reconstruct step 24: "
                f"difference={difference!r}."
            )
        reconstructions.append(
            DailyReconstruction(forecast_start, interval_sum, step_24, difference)
        )
    return tuple(reconstructions)


def precipitation_metres_to_mm(interval_metres: float) -> float:
    if not math.isfinite(interval_metres) or interval_metres < 0:
        raise Era5LandError("Precipitation interval must be finite and non-negative.")
    return interval_metres * 1000.0


def radiation_energy_to_mean_flux(interval_j_m2: float, *, seconds: int = 3600) -> float:
    if not math.isfinite(interval_j_m2) or interval_j_m2 < 0 or seconds <= 0:
        raise Era5LandError("Radiation energy and interval duration must be non-negative.")
    return interval_j_m2 / seconds


def saturation_vapour_pressure_over_water_pa(temperature_k: float) -> float:
    """ECMWF IFS saturation vapour pressure over liquid water."""

    if not math.isfinite(temperature_k) or temperature_k <= IFS_ES_A4_WATER_K:
        raise Era5LandError("Temperature is outside the vapour-pressure method domain.")
    return IFS_ES_A1_PA * math.exp(
        IFS_ES_A3_WATER
        * (temperature_k - IFS_TRIPLE_POINT_K)
        / (temperature_k - IFS_ES_A4_WATER_K)
    )


def relative_humidity_fraction(temperature_k: float, dewpoint_k: float) -> float:
    """Derive RH without clipping; inconsistent T/Td inputs are rejected."""

    pressure = saturation_vapour_pressure_over_water_pa(dewpoint_k)
    saturation = saturation_vapour_pressure_over_water_pa(temperature_k)
    rh = pressure / saturation
    if not math.isfinite(rh) or not 0 <= rh <= 1:
        raise Era5LandError("Derived relative humidity lies outside [0,1].")
    return rh


def wind_speed_and_from_direction(
    u10_m_s: float,
    v10_m_s: float,
    *,
    calm_tolerance_m_s: float = 1e-12,
) -> tuple[float, float | None]:
    """Return scalar speed and meteorological direction-from degrees true."""

    if not all(math.isfinite(value) for value in (u10_m_s, v10_m_s, calm_tolerance_m_s)):
        raise Era5LandError("Wind components and tolerance must be finite.")
    if calm_tolerance_m_s < 0:
        raise Era5LandError("Calm-wind tolerance must be non-negative.")
    speed = math.hypot(u10_m_s, v10_m_s)
    if speed <= calm_tolerance_m_s:
        return speed, None
    direction = (270.0 - math.degrees(math.atan2(v10_m_s, u10_m_s))) % 360.0
    return speed, direction
