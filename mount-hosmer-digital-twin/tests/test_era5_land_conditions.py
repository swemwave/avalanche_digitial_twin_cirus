"""Characterized pure contracts for the blocked ERA5-Land acquisition path."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from app.processing.conditions.era5_land import (
    AccumulatedValue,
    Era5LandError,
    audit_cds_access,
    build_monthly_request_manifest,
    canonical_request_manifest_bytes,
    deaccumulate_full_product,
    precipitation_metres_to_mm,
    radiation_energy_to_mean_flux,
    reconstruct_step_24,
    relative_humidity_fraction,
    saturation_vapour_pressure_over_water_pa,
    wind_speed_and_from_direction,
    write_request_manifest,
)


UTC = timezone.utc


def _present(time_utc: datetime, value: float) -> AccumulatedValue:
    return AccumulatedValue(time_utc=time_utc, value=value, masked=False)


def test_request_manifest_is_exact_monthly_credential_free_and_deterministic() -> None:
    start = datetime(2025, 7, 1, tzinfo=UTC)
    end = datetime(2026, 5, 31, 23, tzinfo=UTC)
    manifest = build_monthly_request_manifest(
        start, end, area=(49.70, -115.20, 49.30, -114.80)
    )
    first = canonical_request_manifest_bytes(manifest)
    assert first == canonical_request_manifest_bytes(manifest)
    decoded = json.loads(first)
    assert decoded["dataset"] == "reanalysis-era5-land"
    assert decoded["expected_consolidated_expver"] == "0001"
    assert len(decoded["requests"]) == 11
    assert decoded["requests"][0]["target_filename"] == "era5_land_202507.grib"
    assert decoded["requests"][-1]["request"]["day"][-1] == "31"
    assert decoded["requests"][0]["request"]["area"] == [49.7, -115.2, 49.3, -114.8]
    assert b"CDSAPI_KEY" not in first and b"credential" not in first.lower().replace(
        b"no credential", b""
    )


def test_cds_access_audit_never_returns_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CDSAPI_URL", "present-but-not-returned")
    monkeypatch.setenv("CDSAPI_KEY", "secret-must-not-be-returned")
    audit = audit_cds_access(home=tmp_path)
    assert audit.url_environment_present is True
    assert audit.key_environment_present is True
    assert audit.config_file_present is False
    assert "secret" not in repr(audit)


def test_request_manifest_storage_is_content_addressed_idempotent_and_strict(tmp_path) -> None:
    manifest = build_monthly_request_manifest(
        datetime(2025, 7, 1, tzinfo=UTC),
        datetime(2026, 5, 31, 23, tzinfo=UTC),
        area=(49.70, -115.20, 49.30, -114.80),
    )
    target = write_request_manifest(manifest, tmp_path)
    assert target.name.startswith("request-")
    assert write_request_manifest(manifest, tmp_path) == target
    (target / "checksums.json").write_bytes(b"corrupt")
    with pytest.raises(Era5LandError, match="incomplete or conflicting"):
        write_request_manifest(manifest, tmp_path)


def test_full_product_deaccumulation_reconstructs_step_24_and_utc_boundary() -> None:
    first = datetime(2025, 7, 1, 0, tzinfo=UTC)
    samples = [_present(first, 9.9)]
    samples.extend(
        _present(first + timedelta(hours=hour), hour * 0.1)
        for hour in range(1, 24)
    )
    samples.append(_present(datetime(2025, 7, 2, 0, tzinfo=UTC), 2.4))
    samples.append(_present(datetime(2025, 7, 2, 1, tzinfo=UTC), 0.2))

    intervals = deaccumulate_full_product(samples)
    by_time = {item.time_utc: item for item in intervals}
    assert by_time[first].masked is True  # preceding 23 UTC record was not supplied
    assert by_time[datetime(2025, 7, 1, 1, tzinfo=UTC)].value == pytest.approx(0.1)
    assert by_time[datetime(2025, 7, 2, 0, tzinfo=UTC)].value == pytest.approx(0.1)
    assert by_time[datetime(2025, 7, 2, 1, tzinfo=UTC)].value == pytest.approx(0.2)
    reconstructed = reconstruct_step_24(intervals)
    assert len(reconstructed) == 1
    assert reconstructed[0].forecast_start_utc == first
    assert reconstructed[0].interval_sum == pytest.approx(2.4)
    assert reconstructed[0].step_24_accumulation == pytest.approx(2.4)


def test_deaccumulation_preserves_missingness_rejects_duplicates_and_negative_values() -> None:
    first = datetime(2025, 7, 1, 0, tzinfo=UTC)
    missing = AccumulatedValue(first, None, True)
    samples = [
        missing,
        _present(first + timedelta(hours=1), 0.5),
        AccumulatedValue(first + timedelta(hours=2), None, True),
        _present(first + timedelta(hours=3), 0.6),
    ]
    intervals = deaccumulate_full_product(samples)
    assert [item.masked for item in intervals] == [True, False, True, True]
    assert intervals[0].value is None and intervals[2].value is None
    with pytest.raises(Era5LandError, match="duplicate"):
        deaccumulate_full_product([_present(first, 1.0), _present(first, 1.0)])
    with pytest.raises(Era5LandError, match="Negative interval"):
        deaccumulate_full_product(
            [_present(first + timedelta(hours=1), 1.0), _present(first + timedelta(hours=2), 0.9)]
        )


def test_deaccumulation_marks_only_explicit_tiny_negative_roundoff() -> None:
    first = datetime(2025, 7, 1, 1, tzinfo=UTC)
    intervals = deaccumulate_full_product(
        [_present(first, 1.0), _present(first + timedelta(hours=1), 1.0 - 5e-13)],
        negative_noise_tolerance=1e-12,
    )
    assert intervals[1].value == 0.0
    assert intervals[1].status == "negative_roundoff_adjusted_to_zero"


def test_units_rh_and_wind_conventions_are_characterized_without_clipping() -> None:
    assert precipitation_metres_to_mm(0.001) == pytest.approx(1.0)
    assert radiation_energy_to_mean_flux(3_600_000.0) == pytest.approx(1000.0)
    assert saturation_vapour_pressure_over_water_pa(273.16) == pytest.approx(611.21)
    rh = relative_humidity_fraction(293.15, 283.15)
    assert rh == pytest.approx(0.5251, abs=5e-4)
    with pytest.raises(Era5LandError, match=r"outside \[0,1\]"):
        relative_humidity_fraction(273.15, 274.15)

    speed, direction = wind_speed_and_from_direction(0.0, -10.0)
    assert speed == pytest.approx(10.0)
    assert direction == pytest.approx(0.0)  # northerly: wind is from true north
    speed, direction = wind_speed_and_from_direction(-10.0, 0.0)
    assert direction == pytest.approx(90.0)  # easterly
    assert wind_speed_and_from_direction(0.0, 0.0) == (0.0, None)

    with pytest.raises(Era5LandError):
        radiation_energy_to_mean_flux(-1.0)
    with pytest.raises(Era5LandError):
        precipitation_metres_to_mm(math.nan)
