"""M2 ECCC historical-forcing adapter acceptance and failure tests."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.cli import _reject_condition_path_in_data, replay_eccc_conditions_command
from app.processing.conditions.eccc import (
    ECCCHistoricalProvider,
    ECCCProviderError,
    audit_eccc_snapshot,
    import_eccc_snapshot,
    load_eccc_snapshot,
    mountain_grid_from_pack,
)
from app.processing.conditions.protocol import ConditionRequest, replay_provider
from app.processing.conditions.storage import load_condition_pack
from avycore.conditions import canonical_condition_pack_bytes


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "conditions" / "eccc"
MOUNTAIN_PACK = Path(__file__).parents[1] / "backend" / "config" / "mount_hosmer.pack.json"
START = datetime(2025, 11, 1, 0, tzinfo=UTC)
END = datetime(2025, 11, 1, 2, tzinfo=UTC)


def _snapshot(tmp_path: Path, *, stations: Path | None = None, hourly: Path | None = None):
    root = import_eccc_snapshot(
        stations or FIXTURE / "climate-stations.csv",
        hourly or FIXTURE / "climate-hourly.csv",
        tmp_path,
    )
    return load_eccc_snapshot(root)


def _request() -> ConditionRequest:
    return ConditionRequest(mountain_grid_from_pack(MOUNTAIN_PACK), START, END)


def _provider(tmp_path: Path, **kwargs) -> ECCCHistoricalProvider:
    return ECCCHistoricalProvider(
        _snapshot(tmp_path),
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=1136.7,
        **kwargs,
    )


def _copy_csv_with_rows(source: Path, target: Path, mutate) -> Path:
    with source.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fields = list(rows[0])
    mutate(rows)
    with target.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return target


def test_fixture_has_recorded_licence_sizes_and_sha256() -> None:
    manifest = json.loads((FIXTURE / "fixture-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["licence"].startswith("ECCC Data Servers End-use Licence")
    assert manifest["original_source_sha256"] == {
        "climate-stations_2025-11-01_2026-05-31.csv": (
            "96c270e6eea1eb06702f0df0341fbf33086aa642866ce830d11f21da7561b2aa"
        ),
        "climate-hourly_2025-11-01_2026-05-31.csv": (
            "0e336e718a7b8a268efdc9b876a0cef5b4b16edf17db8e85ef41c519f09942fe"
        ),
    }
    for name, expected in manifest["files"].items():
        content = (FIXTURE / name).read_bytes()
        assert len(content) == expected["bytes"]
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]


def test_station_selection_uses_all_declared_criteria_and_reports_withheld_error(
    tmp_path: Path,
) -> None:
    report = audit_eccc_snapshot(
        _snapshot(tmp_path),
        _request(),
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2496.78,
    )
    assert report["selected_station_id"] == "1157631"
    assert report["recommended_station_id"] == "1157631"
    assert report["station_override_applied"] is False
    assert report["withheld_station_id"] == "1157635"
    assert report["gap_fill_fraction"] == 0.0
    selected = report["candidates"][0]
    assert selected["operator"].startswith("Environment and Climate Change Canada")
    assert selected["horizontal_distance_km"] > 0
    assert selected["elevation_difference_m"] < -1000
    assert selected["variable_missing_fraction"]["air_temperature"] == pytest.approx(1 / 3)
    comparison = report["withheld_station_comparison"]["air_temperature"]
    assert comparison["overlap_hours"] == 2
    assert comparison["comparison_unit"] == "degC"
    assert comparison["selected_minus_withheld_bias_source_units"] == pytest.approx(-1.4)
    assert "not Mount Hosmer truth" in comparison["comparison_role"]


def test_station_override_binds_quality_coverage_and_comparison_to_actual_station(
    tmp_path: Path,
) -> None:
    report = audit_eccc_snapshot(
        _snapshot(tmp_path),
        _request(),
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2496.78,
        selected_station_id="1157635",
    )
    assert report["recommended_station_id"] == "1157631"
    assert report["selected_station_id"] == "1157635"
    assert report["station_override_applied"] is True
    assert report["withheld_station_id"] == "1157631"
    assert report["forcing_coverage"]["air_temperature"]["available_hours"] == 3
    comparison = report["withheld_station_comparison"]["air_temperature"]
    assert comparison["overlap_hours"] == 2
    assert comparison["selected_minus_withheld_bias_source_units"] == pytest.approx(1.4)


def test_normalization_is_utc_unit_controlled_mask_preserving_and_deterministic(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    first = replay_provider(provider, _request())
    second = replay_provider(provider, _request())
    assert first.condition_id == second.condition_id
    assert canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(second)
    assert first.stations[0].station_id == "eccc-1157631"
    assert all(item.time_utc.utcoffset().total_seconds() == 0 for item in first.variables["air_temperature"].values)

    temperature = first.variables["air_temperature"].values
    assert temperature[0].value == pytest.approx(277.95)
    assert temperature[2].value is None
    assert temperature[2].masked is True
    assert temperature[2].status == "missing"
    assert temperature[2].qc_flags[0].code == "M"

    assert first.variables["wind_speed"].values[0].value == pytest.approx(14 / 3.6)
    assert first.variables["wind_direction"].values[0].value == 180.0
    calm = first.variables["wind_direction"].values[1]
    assert calm.value is None and calm.masked
    assert calm.qc_flags[0].code == "CALM"
    assert first.variables["surface_pressure"].values[0].value == 88710.0
    assert first.variables["precipitation_amount"].values[1].value == 1.0
    assert [item.value for item in first.variables["precipitation_phase"].values] == [
        "none",
        "mixed",
        "none",
    ]
    assert all(item.value is None and item.masked for item in first.variables["shortwave_radiation"].values)
    assert all(item.value is None and item.masked for item in first.variables["longwave_radiation"].values)


def test_temperature_elevation_transfer_is_explicit_and_characterized(tmp_path: Path) -> None:
    provider = ECCCHistoricalProvider(
        _snapshot(tmp_path),
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=1236.7,
        selected_station_id="1157631",
    )
    pack = replay_provider(provider, _request())
    assert pack.variables["air_temperature"].values[0].value == pytest.approx(277.30)
    transformation = next(
        item for item in pack.transformations if item.transformation_id == "temperature-elevation-v1"
    )
    assert transformation.parameters["lapse_rate_k_per_m"] == 0.0065
    assert transformation.parameters["target_elevation_m"] == 1236.7
    assert pack.variables["air_temperature"].values[0].uncertainty.status == "unknown"


def test_exact_duplicate_is_counted_but_conflicting_revision_is_rejected(tmp_path: Path) -> None:
    duplicate = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv",
        tmp_path / "exact.csv",
        lambda rows: rows.append(dict(rows[0])),
    )
    exact_snapshot = _snapshot(tmp_path / "exact-runtime", hourly=duplicate)
    report = audit_eccc_snapshot(
        exact_snapshot,
        _request(),
        target_longitude_deg=-115.0,
        target_latitude_deg=49.6,
        target_elevation_m=2000.0,
    )
    assert report["duplicate_records"]["exact_duplicates"] == 1

    def add_revision(rows):
        revision = dict(rows[0])
        revision["TEMP"] = "99.0"
        rows.append(revision)

    conflict = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv", tmp_path / "conflict.csv", add_revision
    )
    conflict_snapshot = _snapshot(tmp_path / "conflict-runtime", hourly=conflict)
    with pytest.raises(ECCCProviderError, match="Conflicting duplicate/revised"):
        audit_eccc_snapshot(
            conflict_snapshot,
            _request(),
            target_longitude_deg=-115.0,
            target_latitude_deg=49.6,
            target_elevation_m=2000.0,
        )


def test_missing_hour_and_long_gap_remain_missing_without_interpolation(tmp_path: Path) -> None:
    def remove_primary_middle(rows):
        rows[:] = [
            row
            for row in rows
            if not (row["CLIMATE_IDENTIFIER"] == "1157631" and row["UTC_DATE"].endswith("01:00:00"))
        ]

    gapped = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv", tmp_path / "gapped.csv", remove_primary_middle
    )
    snapshot = _snapshot(tmp_path / "runtime", hourly=gapped)
    provider = ECCCHistoricalProvider(
        snapshot,
        target_longitude_deg=-115.0,
        target_latitude_deg=49.6,
        target_elevation_m=1136.7,
        selected_station_id="1157631",
    )
    long_request = ConditionRequest(
        mountain_grid_from_pack(MOUNTAIN_PACK),
        START,
        datetime(2025, 11, 1, 10, tzinfo=UTC),
    )
    pack = replay_provider(provider, long_request)
    middle = pack.variables["air_temperature"].values[1]
    assert middle.value is None and middle.masked and middle.status == "missing"
    assert pack.variables["precipitation_amount"].values[1].value is None
    assert all(
        item.value is None and item.masked
        for item in pack.variables["air_temperature"].values[3:]
    )
    assert not any(item.status == "gap_filled" for series in pack.variables.values() for item in series.values)


def test_malformed_columns_timezone_numbers_and_snapshot_tampering_are_rejected(
    tmp_path: Path,
) -> None:
    malformed = tmp_path / "malformed.csv"
    malformed.write_text("CLIMATE_IDENTIFIER,UTC_DATE\n1157631,2025-11-01T00:00:00\n", encoding="utf-8")
    malformed_snapshot = _snapshot(tmp_path / "malformed-runtime", hourly=malformed)
    with pytest.raises(ECCCProviderError, match="missing required columns"):
        audit_eccc_snapshot(
            malformed_snapshot,
            _request(),
            target_longitude_deg=-115.0,
            target_latitude_deg=49.6,
            target_elevation_m=2000.0,
        )

    def bad_timezone(rows):
        rows[0]["UTC_DATE"] = "2025-11-01T00:00:00-07:00"

    non_utc = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv", tmp_path / "non-utc.csv", bad_timezone
    )
    non_utc_snapshot = _snapshot(tmp_path / "non-utc-runtime", hourly=non_utc)
    with pytest.raises(ECCCProviderError, match="must be UTC"):
        audit_eccc_snapshot(
            non_utc_snapshot,
            _request(),
            target_longitude_deg=-115.0,
            target_latitude_deg=49.6,
            target_elevation_m=2000.0,
        )

    snapshot = _snapshot(tmp_path / "tamper-runtime")
    snapshot.hourly_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ECCCProviderError, match="checksum mismatch"):
        load_eccc_snapshot(snapshot.root)


def test_snapshot_manifest_lineage_and_shape_are_integrity_checked(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path / "licence-runtime")
    manifest_path = snapshot.root / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["licence"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ECCCProviderError, match="invalid licence lineage"):
        load_eccc_snapshot(snapshot.root)

    snapshot = _snapshot(tmp_path / "extra-runtime")
    manifest_path = snapshot.root / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["untracked_note"] = "not identity-bound"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ECCCProviderError, match="missing or unexpected fields"):
        load_eccc_snapshot(snapshot.root)


def test_cli_replays_existing_cache_identity_without_reimport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _snapshot(tmp_path)
    provider = ECCCHistoricalProvider(
        snapshot,
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=1136.7,
        selected_station_id="1157631",
    )
    expected = replay_provider(provider, _request())
    args = type(
        "Args",
        (),
        {
            "snapshot": str(snapshot.root),
            "stations": None,
            "hourly": None,
            "runtime_root": str(tmp_path),
            "mountain_pack": str(MOUNTAIN_PACK),
            "start": "2025-11-01T00:00:00Z",
            "end": "2025-11-01T02:00:00Z",
            "target_elevation_m": 1136.7,
            "station_id": "1157631",
        },
    )()

    assert replay_eccc_conditions_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["source_snapshot_id"] == snapshot.snapshot_id
    assert output["condition_id"] == expected.condition_id
    assert output["deterministic_replay_identical"] is True
    assert load_condition_pack(output["condition_pack_path"]) == expected
    assert len(list((tmp_path / "sources" / "conditions" / "eccc").iterdir())) == 1


def test_cli_condition_paths_cannot_enter_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "DATA" / "mount_hosmer_data"
    data_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="read-only DATA root"):
        _reject_condition_path_in_data(data_root / "runtime", data_root, "output")
    with pytest.raises(ValueError, match="read-only DATA root"):
        _reject_condition_path_in_data(data_root / "snapshot", data_root, "source")


def test_unknown_qc_flag_is_retained_as_suspect_not_silently_discarded(tmp_path: Path) -> None:
    def add_flag(rows):
        rows[0]["TEMP_FLAG"] = "E"

    flagged = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv", tmp_path / "flagged.csv", add_flag
    )
    snapshot = _snapshot(tmp_path / "runtime", hourly=flagged)
    provider = ECCCHistoricalProvider(
        snapshot,
        target_longitude_deg=-115.0,
        target_latitude_deg=49.6,
        target_elevation_m=1136.7,
        selected_station_id="1157631",
    )
    value = replay_provider(provider, _request()).variables["air_temperature"].values[0]
    assert value.value is not None
    assert value.qc_flags[0].code == "E"
    assert value.qc_flags[0].severity == "suspect"


def test_source_hash_changes_snapshot_and_condition_identity(tmp_path: Path) -> None:
    first = _provider(tmp_path / "first")
    first_pack = replay_provider(first, _request())

    def change_measurement(rows):
        rows[0]["TEMP"] = "4.9"

    changed = _copy_csv_with_rows(
        FIXTURE / "climate-hourly.csv", tmp_path / "changed.csv", change_measurement
    )
    second_snapshot = _snapshot(tmp_path / "second", hourly=changed)
    second = ECCCHistoricalProvider(
        second_snapshot,
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=1136.7,
        selected_station_id="1157631",
    )
    second_pack = replay_provider(second, _request())
    assert first.snapshot.snapshot_id != second.snapshot.snapshot_id
    assert first_pack.condition_id != second_pack.condition_id
