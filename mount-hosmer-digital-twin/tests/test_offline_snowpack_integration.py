"""Inactive offline M3 schema, adapter, process, and storage tests."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from avycore.conditions import ConditionPackDraft, build_condition_pack
from avycore.snow import (
    DISCLAIMER,
    SnowStatePack,
    build_snow_state_pack,
    canonical_snow_state_pack_bytes,
)
from app.processing.conditions.eccc import (
    ECCCHistoricalProvider,
    import_eccc_snapshot,
    load_eccc_snapshot,
    mountain_grid_from_pack,
)
from app.processing.conditions.protocol import ConditionRequest, replay_provider
from app.processing.snow.execution import SnowProcessError, execute_snow_process
from app.processing.snow.smet import SmetAdapterError, SmetTerrain, condition_pack_to_smet
from app.processing.snow.storage import (
    SnowStateStorageError,
    load_snow_state_pack,
    write_snow_state_pack,
)


UTC = timezone.utc
ECCC_FIXTURE = Path(__file__).parent / "fixtures" / "conditions" / "eccc"
MOUNTAIN_PACK = Path(__file__).parents[1] / "backend" / "config" / "mount_hosmer.pack.json"
FORCING_START = datetime(2025, 11, 1, 0, tzinfo=UTC)
FORCING_END = datetime(2025, 11, 1, 1, tzinfo=UTC)
ZERO_HASH = "0" * 64
ONE_HASH = "1" * 64


def _eccc_fixture_condition_pack(tmp_path: Path):
    snapshot_root = import_eccc_snapshot(
        ECCC_FIXTURE / "climate-stations.csv",
        ECCC_FIXTURE / "climate-hourly.csv",
        tmp_path,
    )
    provider = ECCCHistoricalProvider(
        load_eccc_snapshot(snapshot_root),
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2499.3645924,
        selected_station_id="1157631",
    )
    request = ConditionRequest(
        mountain_grid_from_pack(MOUNTAIN_PACK),
        FORCING_START,
        FORCING_END,
    )
    return replay_provider(provider, request)


def _complete_condition_pack(tmp_path: Path):
    original = _eccc_fixture_condition_pack(tmp_path)
    draft = original.model_dump(
        mode="json", exclude={"condition_id", "normalized_output_sha256"}
    )
    timestamps = original.times.hourly_timestamps()
    present = {
        "air_temperature": 270.0,
        "relative_humidity": 80.0,
        "wind_speed": 3.0,
        "wind_direction": 90.0,
        "precipitation_phase": "none",
        "precipitation_amount": 0.0,
        "surface_pressure": 85000.0,
        "shortwave_radiation": 100.0,
        "longwave_radiation": 250.0,
    }
    for name, series in draft["variables"].items():
        values = series["values"][:2]
        for index, item in enumerate(values):
            item.update(
                {
                    "time_utc": timestamps[index],
                    "value": present[name],
                    "masked": False,
                    "status": "observed",
                }
            )
        series["values"] = values
    return build_condition_pack(ConditionPackDraft.model_validate(draft))


def _terrain() -> SmetTerrain:
    return SmetTerrain(-115.01138889, 49.61361111, 2499.3645924, 43.7, 135.0)


def _snow_draft() -> dict:
    times = ["2025-11-01T00:00:00Z", "2025-11-01T01:00:00Z"]
    variables = {}
    specifications = {
        "snow_height": ("m", [0.0, 0.01], "modeled vertical snow height", "HS_model"),
        "snow_water_equivalent": (
            "kg m-2", [0.0, 1.0], "modeled snowpack mass per horizontal area", "SWE"
        ),
        "surface_temperature": ("K", [268.0, 267.5], "modeled surface temperature", "TSS_model"),
    }
    for name, (unit, values, semantics, field) in specifications.items():
        variables[name] = {
            "variable": name,
            "unit": unit,
            "semantics": semantics,
            "values": [
                {"time_utc": time, "value": value, "masked": False,
                 "status": "modeled", "output_field": field}
                for time, value in zip(times, values, strict=True)
            ],
        }
    return {
        "schema_version": "mount-hosmer-snow-state-pack-v2",
        "disclaimer": DISCLAIMER,
        "activation_status": "isolated_offline_not_served_not_assessed",
        "model": {
            "engine": "synthetic-process-fixture",
            "executable_sha256": ZERO_HASH,
            "binary_inventory_sha256": ZERO_HASH,
            "executable_version_output": "synthetic 1",
            "configuration_sha256": ONE_HASH,
            "input_inventory_sha256": ONE_HASH,
            "adapter_code_sha256": ZERO_HASH,
            "output_parser_version": "synthetic-output-parser-v1",
            "command_argv": ["synthetic", "--offline"],
            "timeout_seconds": 10.0,
        },
        "input_lineage": {
            "condition_id": f"condition-{ZERO_HASH}",
            "condition_normalized_output_sha256": ZERO_HASH,
            "condition_pack_file_sha256": ONE_HASH,
            "bake_sha256": ZERO_HASH,
            "reference_elevation_id": f"reference-elevation-{ONE_HASH}",
            "reference_elevation_file_sha256": ONE_HASH,
            "forcing_adapter_version": "condition-pack-to-smet-v1",
            "forcing_file_sha256": ZERO_HASH,
            "forcing_file_bytes": 100,
        },
        "terrain": {
            "coordinate_order": "longitude,latitude",
            "longitude_deg": -115.01138889,
            "latitude_deg": 49.61361111,
            "reference_elevation_m": 2499.3645924,
            "slope_angle_deg": 43.7,
            "slope_aspect_deg_true": 135.0,
            "roughness_length_m": None,
            "canopy_height_m": None,
            "vertical_datum_status": "unknown",
            "geometry_use": "isolated_offline_model_input_not_activated",
        },
        "times": {"start_utc": times[0], "end_utc": times[1], "cadence_seconds": 3600},
        "variables": variables,
        "process": {
            "exit_code": 0,
            "stdout_sha256": ZERO_HASH,
            "stderr_sha256": ZERO_HASH,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "output_file_sha256": ONE_HASH,
            "normalized_output_sha256": ONE_HASH,
            "output_file_bytes": 100,
        },
        "limitations": [
            "Synthetic redistributable fixture; not a SNOWPACK result and not field validation."
        ],
    }


def test_condition_pack_to_smet_is_exact_utc_unit_explicit_and_deterministic(
    tmp_path: Path,
) -> None:
    pack = _complete_condition_pack(tmp_path)
    first = condition_pack_to_smet(pack, _terrain())
    assert first == condition_pack_to_smet(pack, _terrain())
    text = first.decode("ascii")
    assert "fields = timestamp TA RH VW DW ISWR ILWR PSUM PSUM_PH P" in text
    assert "tz = 0" in text
    assert "2025-11-01T00:00:00 270 0.80000000000000004 3 90 100 250 0 0 85000" in text
    assert b"-999" in first  # declaration only; no missing value was filled into data


def test_smet_rejects_missing_radiation_gap_fill_unknown_phase_and_coordinate_swap(
    tmp_path: Path,
) -> None:
    unmodified = _eccc_fixture_condition_pack(tmp_path / "masked")
    assert all(
        item.value is None and item.masked
        for item in unmodified.variables["shortwave_radiation"].values
    )
    with pytest.raises(SmetAdapterError, match="shortwave_radiation"):
        condition_pack_to_smet(unmodified, _terrain())

    pack = _complete_condition_pack(tmp_path / "complete")
    raw = pack.model_dump(mode="json")
    raw["variables"]["precipitation_phase"]["values"][0].update(
        {"value": "unknown", "masked": False, "status": "observed"}
    )
    raw.pop("condition_id")
    raw.pop("normalized_output_sha256")
    unknown = build_condition_pack(raw)
    with pytest.raises(SmetAdapterError, match="unknown/inconsistent"):
        condition_pack_to_smet(unknown, _terrain())
    with pytest.raises(SmetAdapterError, match="coordinates"):
        SmetTerrain(49.6, -115.0, 2500.0, 30.0, 0.0)


def test_snow_state_contract_identity_timeline_missingness_and_storage(tmp_path: Path) -> None:
    pack = build_snow_state_pack(_snow_draft())
    replay = build_snow_state_pack(_snow_draft())
    assert pack == replay
    assert canonical_snow_state_pack_bytes(pack) == canonical_snow_state_pack_bytes(replay)
    target = write_snow_state_pack(pack, tmp_path)
    assert write_snow_state_pack(replay, tmp_path) == target
    assert load_snow_state_pack(target) == pack

    malformed = _snow_draft()
    malformed["variables"]["snow_height"]["values"][0].update(
        {"value": 0.0, "masked": True, "status": "missing"}
    )
    with pytest.raises(ValidationError, match="Missing snow state"):
        build_snow_state_pack(malformed)

    checksums = target / "checksums.json"
    content = json.loads(checksums.read_text(encoding="utf-8"))
    content["files"]["snow-state-pack.json"]["sha256"] = ZERO_HASH
    checksums.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(SnowStateStorageError, match="checksum"):
        load_snow_state_pack(target)


def test_snow_state_scientific_replay_ignores_run_noise_but_not_physics() -> None:
    baseline = build_snow_state_pack(_snow_draft())

    volatile = _snow_draft()
    volatile["model"].update(
        {
            "executable_version_output": "synthetic 1 from another host",
            "command_argv": ["D:/another-host/synthetic.exe", "--offline"],
            "timeout_seconds": 20.0,
        }
    )
    volatile["process"].update(
        {
            "stdout_sha256": "2" * 64,
            "stderr_sha256": "3" * 64,
            "stdout_bytes": 17,
            "stderr_bytes": 9,
            "output_file_sha256": "4" * 64,
            "output_file_bytes": 101,
        }
    )
    volatile["limitations"] = [
        "Same scientific replay with deliberately different run-environment evidence."
    ]
    noisy_run = build_snow_state_pack(volatile)

    assert noisy_run.snow_state_id != baseline.snow_state_id
    assert noisy_run.scientific_replay_sha256 == baseline.scientific_replay_sha256

    scientific_changes = (
        ("binary_inventory_sha256", "5" * 64),
        ("configuration_sha256", "6" * 64),
        ("input_inventory_sha256", "7" * 64),
    )
    for field, value in scientific_changes:
        changed = _snow_draft()
        changed["model"][field] = value
        candidate = build_snow_state_pack(changed)
        assert candidate.scientific_replay_sha256 != baseline.scientific_replay_sha256

    changed_output = _snow_draft()
    changed_output["process"]["normalized_output_sha256"] = "8" * 64
    assert (
        build_snow_state_pack(changed_output).scientific_replay_sha256
        != baseline.scientific_replay_sha256
    )

    changed_physics = _snow_draft()
    changed_physics["variables"]["snow_height"]["values"][1]["value"] = 0.02
    assert (
        build_snow_state_pack(changed_physics).scientific_replay_sha256
        != baseline.scientific_replay_sha256
    )


FAKE_PROCESS = b"""\
import pathlib, sys, time
mode = sys.argv[1]
if mode == 'fail':
    print('synthetic failure', file=sys.stderr)
    raise SystemExit(7)
if mode == 'timeout':
    time.sleep(5)
if mode == 'empty':
    pathlib.Path('result.bin').write_bytes(b'')
elif mode != 'missing':
    pathlib.Path('result.bin').write_bytes(pathlib.Path('forcing.smet').read_bytes())
"""


def test_external_process_hashes_versions_bounds_failures_and_replays() -> None:
    kwargs = {
        "input_files": {"fake.py": FAKE_PROCESS, "forcing.smet": b"fixture forcing\n"},
        "output_relative_path": "result.bin",
        "timeout_seconds": 2.0,
    }
    first = execute_snow_process(sys.executable, ("fake.py", "ok"), **kwargs)
    replay = execute_snow_process(sys.executable, ("fake.py", "ok"), **kwargs)
    assert first.output == replay.output == b"fixture forcing\n"
    assert first.executable_sha256 == replay.executable_sha256
    assert first.version_output == replay.version_output
    with pytest.raises(SnowProcessError, match="exit 7"):
        execute_snow_process(sys.executable, ("fake.py", "fail"), **kwargs)
    with pytest.raises(SnowProcessError, match="exceeded"):
        execute_snow_process(
            sys.executable, ("fake.py", "timeout"), **{**kwargs, "timeout_seconds": 0.1}
        )
    with pytest.raises(SnowProcessError, match="did not create"):
        execute_snow_process(sys.executable, ("fake.py", "missing"), **kwargs)
    with pytest.raises(SnowProcessError, match="empty"):
        execute_snow_process(sys.executable, ("fake.py", "empty"), **kwargs)


def test_serving_imports_do_not_reference_offline_snow_dependencies() -> None:
    backend = Path(__file__).parents[1] / "backend" / "app"
    serving = [backend / "assess.py", backend / "baked.py", backend / "main.py",
               *sorted((backend / "api").glob("*.py"))]
    for path in serving:
        source = path.read_text(encoding="utf-8")
        assert "processing.snow" not in source
        assert "avycore.snow" not in source
