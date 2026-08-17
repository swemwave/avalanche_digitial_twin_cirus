"""Hermetic subprocess boundaries plus optional real AvaFrame integration."""

from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.processing.runout.avaframe import AvaFrameCom1DFAAdapter
from app.processing.runout.availability import (
    AvaFrameFlowPyAvailabilityAdapter,
    RAvaFlowAvailabilityAdapter,
)
from app.processing.runout.process import ExternalModelProcessError, run_isolated_worker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMILARITY_ACCEPTANCE = (
    PROJECT_ROOT
    / "validation-data"
    / "benchmarks"
    / "avaframe-2.1-avaSimilaritySol"
    / "acceptance.json"
)


def test_missing_avaframe_environment_is_explicitly_unavailable(tmp_path: Path):
    availability = AvaFrameCom1DFAAdapter(tmp_path / "missing-python").availability()
    assert availability.status == "unavailable"
    assert "does not exist" in availability.reason


def test_unimplemented_external_adapters_fail_closed(tmp_path: Path):
    flowpy = AvaFrameFlowPyAvailabilityAdapter(tmp_path / "missing-python").availability()
    r_avaflow = RAvaFlowAvailabilityAdapter().availability()
    assert flowpy.status == "unavailable"
    assert "does not exist" in flowpy.reason
    assert r_avaflow.status == "unavailable"
    assert "No version-bound" in r_avaflow.reason


def test_subprocess_nonzero_exit_is_visible(tmp_path: Path):
    worker = tmp_path / "worker.py"
    worker.write_text("raise SystemExit(7)\n", encoding="utf-8")
    with pytest.raises(ExternalModelProcessError) as caught:
        run_isolated_worker(
            sys.executable,
            worker,
            (),
            cwd=tmp_path,
            timeout_seconds=5.0,
        )
    assert caught.value.code == "nonzero_exit"
    assert "exit 7" in str(caught.value)


def test_subprocess_timeout_is_visible(tmp_path: Path):
    worker = tmp_path / "worker.py"
    worker.write_text("import time\ntime.sleep(2)\n", encoding="utf-8")
    with pytest.raises(ExternalModelProcessError) as caught:
        run_isolated_worker(
            sys.executable,
            worker,
            (),
            cwd=tmp_path,
            timeout_seconds=0.05,
        )
    assert caught.value.code == "timeout"


def test_subprocess_capture_bound_is_visible(tmp_path: Path):
    worker = tmp_path / "worker.py"
    worker.write_text("print('x' * 2048)\n", encoding="utf-8")
    with pytest.raises(ExternalModelProcessError) as caught:
        run_isolated_worker(
            sys.executable,
            worker,
            (),
            cwd=tmp_path,
            timeout_seconds=5.0,
            maximum_capture_bytes=100,
        )
    assert caught.value.code == "capture_too_large"


@pytest.mark.parametrize("entrypoint", ["app.main", "app.main_assess", "app.main_assistant"])
def test_serving_entrypoints_do_not_import_external_engine_adapters(entrypoint: str):
    banned = (
        "avaframe",
        "rasterio",
        "pyproj",
        "app.processing.runout",
        "app.processing.runout.avaframe",
    )
    probe = (
        "import importlib,sys;"
        f"importlib.import_module({entrypoint!r});"
        f"print(','.join(name for name in {banned!r} if name in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == ""


def test_similarity_acceptance_is_locked_and_contains_no_pending_identity():
    acceptance = json.loads(SIMILARITY_ACCEPTANCE.read_text(encoding="utf-8"))
    assert acceptance["schema"] == "avycore-avaframe-analytical-acceptance-v1"
    assert acceptance["locked_before_first_run"] is True
    assert acceptance["engine"]["version"] == "2.1"
    assert acceptance["case_configuration"]["seed"] == 12345
    assert "pending" not in SIMILARITY_ACCEPTANCE.read_text(encoding="utf-8").lower()
    for identity in acceptance["upstream_inputs"]["files"].values():
        assert identity["byte_size"] > 0
        assert len(identity["sha256"]) == 64
    controlling = {
        name: metric["maximum"]
        for name, metric in acceptance["acceptance_metrics"].items()
        if isinstance(metric, dict) and "maximum" in metric
    }
    assert controlling == {
        "front_downstream_absolute_error_m": 6.0,
        "flow_thickness_relative_l2": 0.5,
        "flow_thickness_relative_linf": 0.75,
        "momentum_relative_l2": 0.5,
        "momentum_relative_linf": 0.75,
        "flow_velocity_relative_l2": None,
        "solver_mass_balance_relative_error": 1e-12,
        "initial_volume_relative_error": 0.05,
    }


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_avaframe_similarity_solution_matches_preregistered_gate(tmp_path: Path):
    source_inputs = Path(
        os.environ.get(
            "AVAFRAME_TEST_SOURCE_INPUTS",
            str(
                PROJECT_ROOT
                / ".test-tmp"
                / "avaframe-source-2.1"
                / "avaframe"
                / "data"
                / "avaSimilaritySol"
                / "Inputs"
            ),
        )
    )
    if not source_inputs.is_dir():
        pytest.skip(
            "Set AVAFRAME_TEST_SOURCE_INPUTS to the exact OpenNHM/AvaFrame 2.1 "
            "avaframe/data/avaSimilaritySol/Inputs directory."
        )
    adapter = AvaFrameCom1DFAAdapter(
        Path(os.environ["AVAFRAME_TEST_PYTHON"]),
        timeout_seconds=900.0,
    )
    run = adapter.run_similarity_benchmark(
        source_inputs=source_inputs,
        acceptance_path=SIMILARITY_ACCEPTANCE,
        output_root=tmp_path / "first",
    )
    assert run.report["overall_passed"] is True
    assert run.report["engine"]["version"] == "2.1"
    assert run.report["seed"] == 12345
    assert run.report["scientific_status"] == "software_verification_only"
    assert run.report["grid"]["cell_size_m"] == 3.0
    assert run.report["grid"]["crs"] is None
    assert run.report["grid"]["crs_status"] == "undefined_local_cartesian"
    assert run.report["mask"]["invalid_cell_count"] == 0
    assert run.report["pressure"]["status"] == "not_applicable"
    assert all(item["passed"] for item in run.report["invariants"].values())
    assert all(item["passed"] for item in run.report["metrics"].values())

    replay = adapter.run_similarity_benchmark(
        source_inputs=source_inputs,
        acceptance_path=SIMILARITY_ACCEPTANCE,
        output_root=tmp_path / "second",
    )
    assert replay.result_id == run.result_id
    assert replay.report["artifacts"] == run.report["artifacts"]
    first_scientific_report = {
        key: value
        for key, value in run.report.items()
        if key != "upstream_execution_label"
    }
    replay_scientific_report = {
        key: value
        for key, value in replay.report.items()
        if key != "upstream_execution_label"
    }
    assert replay_scientific_report == first_scientific_report
    assert "excluded_from_result_id" in run.report["upstream_execution_label"][
        "identity_role"
    ]


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_avaframe_synthetic_example_is_normalized_and_replayable(tmp_path: Path):
    from app.processing.runout.synthetic import run_synthetic_example

    python = Path(os.environ["AVAFRAME_TEST_PYTHON"])
    first = run_synthetic_example(
        avaframe_python=python,
        output_root=tmp_path / "first",
        simulation_time_s=25.0,
    )
    second = run_synthetic_example(
        avaframe_python=python,
        output_root=tmp_path / "second",
        simulation_time_s=25.0,
    )
    assert first.release.result_id == second.release.result_id
    assert first.runout.result_id == second.runout.result_id
    assert first.release.release_area_m2 > 0
    assert first.runout.runout_area_m2 > 0
    assert first.runout.aoi_status == "complete_within_domain"
    assert first.runout.flow_depth is not None
    assert first.runout.flow_velocity is not None
    assert first.runout.flow_pressure is not None
    assert first.runout.flow_depth.unit == "m"
    assert first.runout.flow_velocity.unit == "m s-1"
    assert first.runout.flow_pressure.unit == "kPa"
    assert first.runout.provenance.engine_version == "2.1"
    assert first.runout.validation.eligible_field_events == 0
    assert first.runout.uncertainty == ()
    assert any("no propagated uncertainty" in item for item in first.runout.limitations)
