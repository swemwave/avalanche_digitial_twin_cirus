"""Real-format SNOWPACK 3.7.0 output parser characterization."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.processing.snow.snowpack_output import (
    SnowpackOutputError,
    parse_snowpack_smet,
    require_exact_cadence,
    snow_state_variables,
)
from app.processing.snow.official_example import load_official_example_verification


UTC = timezone.utc


FIXTURE = b"""SMET 1.1 ASCII
[HEADER]
station_id = synthetic
nodata = -999
tz = 0
units_offset = 0 0 0 273.15 0 0
units_multiplier = 1 0.01 1 1 1 1
plot_unit = - m kg/m2 K kg/m2 W/m2
fields = timestamp HS_mod SWE TSS_mod MS_Water Qs
creator_name = volatile
date_created = 2026-08-09
history = volatile host and timestamp
[DATA]
2025-11-01T00:00:00 100 50 -5 51 -10
2025-11-01T01:00:00 101 51 -4 -999 12
"""


def test_parser_applies_declared_units_utc_nodata_and_normalized_identity() -> None:
    parsed = parse_snowpack_smet(FIXTURE)
    assert parsed.timestamps_utc == (
        datetime(2025, 11, 1, 0, tzinfo=UTC),
        datetime(2025, 11, 1, 1, tzinfo=UTC),
    )
    assert parsed.values["HS_mod"] == pytest.approx((1.0, 1.01))
    assert parsed.values["SWE"] == pytest.approx((50.0, 51.0))
    assert parsed.values["TSS_mod"] == pytest.approx((268.15, 269.15))
    assert parsed.values["MS_Water"] == (51.0, None)
    changed_volatile = FIXTURE.replace(b"volatile host and timestamp", b"different run stamp")
    replay = parse_snowpack_smet(changed_volatile)
    assert replay.raw_sha256 != parsed.raw_sha256
    assert replay.normalized_sha256 == parsed.normalized_sha256

    variables = snow_state_variables(parsed)
    assert variables["snow_height"]["unit"] == "m"
    assert variables["surface_temperature"]["values"][0]["value"] == pytest.approx(268.15)
    require_exact_cadence(parsed.timestamps_utc)


def test_parser_rejects_ambiguous_layout_cadence_and_nonphysical_state() -> None:
    with pytest.raises(SnowpackOutputError, match="one finite value per field"):
        parse_snowpack_smet(FIXTURE.replace(b"units_multiplier = 1 0.01 1 1 1 1", b"units_multiplier = 1"))
    parsed = parse_snowpack_smet(
        FIXTURE.replace(b"2025-11-01T01:00:00", b"2025-11-01T02:00:00")
    )
    with pytest.raises(SnowpackOutputError, match="cadence"):
        require_exact_cadence(parsed.timestamps_utc)
    negative = parse_snowpack_smet(FIXTURE.replace(b"100 50 -5", b"-1 50 -5"))
    with pytest.raises(SnowpackOutputError, match="negative"):
        snow_state_variables(negative)


def test_parser_reads_preserved_official_370_example_when_available() -> None:
    path = (
        Path(__file__).parents[1]
        / "runtime"
        / "verification"
        / "snowpack-example-probe-d72bb0ab53434169a3123e5f9a25295f"
        / "output"
        / "MST96_res.smet"
    )
    if not path.is_file():
        pytest.skip("Preserved official SNOWPACK 3.7.0 example output is unavailable.")
    parsed = parse_snowpack_smet(path.read_bytes())
    assert parsed.header["product_version"] == "1.0"
    assert parsed.header["tz"] == "1"
    assert {"HS_mod", "SWE", "TSS_mod", "Qs", "MS_Water"} <= set(parsed.fields)
    assert parsed.values["HS_mod"]
    assert parsed.values["SWE"]
    assert parsed.timestamps_utc[0].utcoffset().total_seconds() == 0


def test_preserved_official_verifications_pass_strict_storage_validation() -> None:
    root = Path(__file__).parents[1] / "runtime" / "reports" / "snowpack"
    verifications = sorted(root.glob("verification-*")) if root.is_dir() else []
    if not verifications:
        pytest.skip("Preserved official SNOWPACK verification is unavailable.")
    reports = [load_official_example_verification(path) for path in verifications]
    assert all(item["result"] == "PASS_OFFICIAL_SMOKE_ONLY" for item in reports)
    assert {
        item["parsed_smet"]["normalized_sha256"] for item in reports
    } == {"ef6c1ce7d0f260db67d495350a830eb994e7507b5e0823b71c21322305b1a549"}
