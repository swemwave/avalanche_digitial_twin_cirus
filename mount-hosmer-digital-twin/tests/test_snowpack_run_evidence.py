"""Hermetic SNOWPACK binary, input, and replay-evidence contract tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.processing.snow.execution import ExternalProcessResult
from app.processing.snow.run_evidence import (
    SnowpackRunEvidenceError,
    build_snowpack_run_evidence,
    derive_binary_inventory,
)
from app.processing.snow.snowpack_output import parse_snowpack_smet


FORCING = b"""SMET 1.1 ASCII
[HEADER]
station_id = representative-slope
nodata = -999
tz = 0
fields = timestamp TA RH VW DW ISWR ILWR PSUM PSUM_PH P
[DATA]
2025-11-01T00:00:00 270 0.8 3 90 100 250 0 0 85000
2025-11-01T01:00:00 271 0.81 4 100 110 251 1 0 85010
"""

OUTPUT = b"""SMET 1.1 ASCII
[HEADER]
station_id = representative-slope
nodata = -999
tz = 0
units_offset = 0 0 0 273.15
units_multiplier = 1 0.01 1 1
fields = timestamp HS_mod SWE TSS_mod
creator_name = volatile-host-a
history = volatile-run-a
[DATA]
2025-11-01T00:00:00 100 50 -5
2025-11-01T01:00:00 101 51 -4
"""

INPUTS = {
    "config/snowpack.ini": b"[SNOWPACK]\nCALCULATION_STEP_LENGTH = 60\n",
    "forcing/representative.smet": FORCING,
    "initial/representative.sno": b"SMET 1.1 ASCII\n[HEADER]\nstation_id = initial\n[DATA]\n",
    "site/representative.ini": b"slope_angle = 43.7\nslope_azi = 135\n",
}
ROLES = {
    "configuration": ("config/snowpack.ini",),
    "forcing": ("forcing/representative.smet",),
    "initial_state": ("initial/representative.sno",),
    "site_parameters": ("site/representative.ini",),
}


def _binary_closure(tmp_path: Path) -> Path:
    binary = tmp_path / "snowpack.exe"
    binary.write_bytes(b"synthetic SNOWPACK executable")
    (tmp_path / "libmeteoio.dll").write_bytes(b"synthetic MeteoIO runtime")
    (tmp_path / "libsnowpack.DLL").write_bytes(b"synthetic SNOWPACK runtime")
    (tmp_path / "ignored.dll.a").write_bytes(b"not a runtime DLL")
    return binary


def _result(
    executable: Path,
    output: bytes = OUTPUT,
    *,
    version: str = "SNOWPACK 3.7.0",
    stdout: bytes = b"run-a stdout",
    stderr: bytes = b"",
) -> ExternalProcessResult:
    return ExternalProcessResult(
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        version_output=version,
        command_argv=(
            str(executable.resolve()),
            "-c",
            "config/snowpack.ini",
            "-i",
            "forcing/representative.smet",
        ),
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        output=output,
    )


def _evidence(
    executable: Path,
    *,
    inputs: dict[str, bytes] | None = None,
    roles: dict[str, tuple[str, ...]] | None = None,
    output: bytes = OUTPUT,
    version: str = "SNOWPACK 3.7.0",
    stdout: bytes = b"run-a stdout",
    stderr: bytes = b"",
):
    return build_snowpack_run_evidence(
        _result(
            executable,
            output,
            version=version,
            stdout=stdout,
            stderr=stderr,
        ),
        executable,
        input_files=INPUTS if inputs is None else inputs,
        input_roles=ROLES if roles is None else roles,
        parsed_output=parse_snowpack_smet(output),
        timeout_seconds=60.0,
    )


def test_binary_inventory_covers_executable_and_every_adjacent_runtime_dll(
    tmp_path: Path,
) -> None:
    executable = _binary_closure(tmp_path)
    records, identity = derive_binary_inventory(executable)

    assert tuple(item.relative_path for item in records) == (
        "snowpack.exe",
        "libmeteoio.dll",
        "libsnowpack.DLL",
    )
    assert all(item.bytes > 0 and len(item.sha256) == 64 for item in records)
    assert len(identity) == 64

    lone_executable = tmp_path / "incomplete" / "snowpack.exe"
    lone_executable.parent.mkdir()
    lone_executable.write_bytes(b"executable without its runtime closure")
    with pytest.raises(SnowpackRunEvidenceError, match="no adjacent runtime DLLs"):
        derive_binary_inventory(lone_executable)


def test_scientific_replay_is_stable_across_volatile_run_output(tmp_path: Path) -> None:
    executable = _binary_closure(tmp_path)
    baseline = _evidence(executable)
    volatile_output = OUTPUT.replace(b"volatile-host-a", b"volatile-host-b").replace(
        b"volatile-run-a", b"volatile-run-b"
    )
    noisy_run = _evidence(
        executable,
        output=volatile_output,
        version="SNOWPACK 3.7.0 built on another host",
        stdout=b"different stdout and timing",
        stderr=b"different non-fatal diagnostics",
    )

    assert noisy_run.run_evidence_id != baseline.run_evidence_id
    assert noisy_run.raw_output_sha256 != baseline.raw_output_sha256
    assert noisy_run.normalized_output_sha256 == baseline.normalized_output_sha256
    assert noisy_run.model_input_replay_sha256 == baseline.model_input_replay_sha256
    assert baseline.executable_sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert baseline.relative_command_argv[0] == "snowpack.exe"
    assert baseline.input_roles == ROLES
    assert set(baseline.input_role_inventory_sha256) == set(ROLES)
    assert all(len(value) == 64 for value in baseline.input_role_inventory_sha256.values())
    assert baseline.stdout_bytes == len(b"run-a stdout")
    assert baseline.stderr_bytes == 0
    assert noisy_run.stdout_bytes == len(b"different stdout and timing")
    assert noisy_run.stderr_bytes == len(b"different non-fatal diagnostics")
    assert noisy_run.raw_output_bytes == len(volatile_output)
    with pytest.raises(TypeError):
        baseline.input_roles["forcing"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        baseline.input_role_inventory_sha256["forcing"] = "0" * 64  # type: ignore[index]


def test_binary_input_configuration_and_physical_changes_change_replay(
    tmp_path: Path,
) -> None:
    executable = _binary_closure(tmp_path)
    baseline = _evidence(executable)

    (tmp_path / "libsnowpack.DLL").write_bytes(b"different SNOWPACK runtime")
    changed_binary = _evidence(executable)
    assert changed_binary.binary_inventory_sha256 != baseline.binary_inventory_sha256
    assert changed_binary.model_input_replay_sha256 != baseline.model_input_replay_sha256

    changed_inputs = dict(INPUTS)
    changed_inputs["site/representative.ini"] = (
        b"slope_angle = 44.0\nslope_azi = 135\n"
    )
    changed_site = _evidence(executable, inputs=changed_inputs)
    assert changed_site.input_inventory_sha256 != changed_binary.input_inventory_sha256
    assert (
        changed_site.input_role_inventory_sha256["site_parameters"]
        != changed_binary.input_role_inventory_sha256["site_parameters"]
    )
    assert changed_site.input_role_inventory_sha256["forcing"] == (
        changed_binary.input_role_inventory_sha256["forcing"]
    )
    assert changed_site.model_input_replay_sha256 != changed_binary.model_input_replay_sha256

    changed_configuration = dict(INPUTS)
    changed_configuration["config/snowpack.ini"] = (
        b"[SNOWPACK]\nCALCULATION_STEP_LENGTH = 30\n"
    )
    changed_config = _evidence(executable, inputs=changed_configuration)
    assert changed_config.input_role_inventory_sha256["configuration"] != (
        changed_binary.input_role_inventory_sha256["configuration"]
    )
    assert changed_config.input_role_inventory_sha256["site_parameters"] == (
        changed_binary.input_role_inventory_sha256["site_parameters"]
    )
    assert changed_config.model_input_replay_sha256 != changed_binary.model_input_replay_sha256

    physical_output = OUTPUT.replace(b"101 51 -4", b"102 51 -4")
    changed_physics = _evidence(executable, output=physical_output)
    assert changed_physics.normalized_output_sha256 != changed_binary.normalized_output_sha256
    assert changed_physics.model_input_replay_sha256 != changed_binary.model_input_replay_sha256


def test_input_inventory_requires_complete_exclusive_role_assignment(tmp_path: Path) -> None:
    executable = _binary_closure(tmp_path)

    missing_role = dict(ROLES)
    del missing_role["initial_state"]
    with pytest.raises(SnowpackRunEvidenceError, match="input roles are incomplete"):
        _evidence(executable, roles=missing_role)

    unassigned = {**INPUTS, "unassigned/extra.ini": b"must be inventoried explicitly\n"}
    with pytest.raises(SnowpackRunEvidenceError, match="assigned to exactly one"):
        _evidence(executable, inputs=unassigned)

    duplicate_assignment = dict(ROLES)
    duplicate_assignment["site_parameters"] = (
        "site/representative.ini",
        "initial/representative.sno",
    )
    with pytest.raises(SnowpackRunEvidenceError, match="assigned to exactly one"):
        _evidence(executable, roles=duplicate_assignment)

    unknown = dict(ROLES)
    unknown["initial_state"] = ("initial/not-in-inventory.sno",)
    with pytest.raises(SnowpackRunEvidenceError, match="references unknown files"):
        _evidence(executable, roles=unknown)

    noncanonical = dict(INPUTS)
    noncanonical["config\\snowpack.ini"] = noncanonical.pop("config/snowpack.ini")
    with pytest.raises(SnowpackRunEvidenceError, match="canonical forward-slash"):
        _evidence(executable, inputs=noncanonical)


def test_run_evidence_refuses_unbounded_or_unidentified_execution(tmp_path: Path) -> None:
    executable = _binary_closure(tmp_path)
    parsed = parse_snowpack_smet(OUTPUT)
    with pytest.raises(SnowpackRunEvidenceError, match="successful bounded runs"):
        build_snowpack_run_evidence(
            _result(executable, version=""),
            executable,
            input_files=INPUTS,
            input_roles=ROLES,
            parsed_output=parsed,
            timeout_seconds=60.0,
        )
    with pytest.raises(SnowpackRunEvidenceError, match="successful bounded runs"):
        build_snowpack_run_evidence(
            _result(executable),
            executable,
            input_files=INPUTS,
            input_roles=ROLES,
            parsed_output=parsed,
            timeout_seconds=float("nan"),
        )


@pytest.mark.parametrize(
    ("forcing", "message"),
    [
        (FORCING.replace(b"tz = 0", b"tz = -7"), "UTC declaration"),
        (
            FORCING.replace(
                b"2025-11-01T01:00:00", b"2025-11-01T01:00:00-07:00"
            ),
            "naive exact hours",
        ),
        (
            FORCING.replace(b"2025-11-01T01:00:00", b"2025-11-01T02:00:00"),
            "cadence is not exactly hourly",
        ),
        (FORCING.replace(b"271 0.81", b"-999 0.81"), "missing required values"),
    ],
)
def test_forcing_refuses_non_utc_non_hourly_and_missing_values(
    tmp_path: Path, forcing: bytes, message: str
) -> None:
    executable = _binary_closure(tmp_path)
    inputs = dict(INPUTS)
    inputs["forcing/representative.smet"] = forcing

    with pytest.raises(SnowpackRunEvidenceError, match=message):
        _evidence(executable, inputs=inputs)
