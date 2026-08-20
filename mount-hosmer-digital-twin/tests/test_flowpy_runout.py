"""Flow-Py engine identity, normalization rules, and the analytical energy-line case."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from avycore.engines import (
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
    CANONICAL_OUTPUT_UNITS,
    UPSTREAM_FLOWPY,
    OutputQuantity,
    canonical_engine_registry,
    descriptor_by_id,
)
from app.processing.runout.flowpy import (
    UNSUPPORTED_FLOWPY_OUTPUTS,
    UPSTREAM_FLOWPY_REVIEWED_COMMITS,
    AvaFrameCom4FlowPyAdapter,
    UpstreamFlowPyAdapter,
    flowpy_energy_line_reference,
    normalized_text_sha256,
)
from app.processing.runout.flowpy_benchmark import (
    BENCHMARK_ID,
    EnergyLineCase,
    verify_stored_benchmark,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENERGY_LINE_DIR = PROJECT_ROOT / "validation-data" / "benchmarks" / "flowpy-energy-line"
ACCEPTANCE = ENERGY_LINE_DIR / "acceptance.json"


def test_flowpy_and_com4flowpy_are_separate_engine_identities():
    assert AVAFRAME_FLOWPY.engine_id != UPSTREAM_FLOWPY.engine_id
    assert AVAFRAME_FLOWPY.license_spdx == "EUPL-1.2"
    assert UPSTREAM_FLOWPY.license_spdx == "GPL-3.0-or-later"
    assert UPSTREAM_FLOWPY.source_url == "https://github.com/avaframe/FlowPy"
    catalogue = {descriptor.engine_id for descriptor, _ in canonical_engine_registry().inventory()}
    assert {AVAFRAME_FLOWPY.engine_id, UPSTREAM_FLOWPY.engine_id} <= catalogue
    assert descriptor_by_id(UPSTREAM_FLOWPY.engine_id) is UPSTREAM_FLOWPY


def test_flowpy_declares_routing_outputs_and_no_dense_flow_quantities():
    outputs = set(AVAFRAME_FLOWPY.output_capabilities)
    assert outputs == {
        OutputQuantity.RUNOUT_EXTENT,
        OutputQuantity.ENERGY_LINE_HEIGHT,
        OutputQuantity.TRAVEL_ANGLE,
    }
    assert not outputs & {
        OutputQuantity.FLOW_DEPTH,
        OutputQuantity.FLOW_VELOCITY,
        OutputQuantity.FLOW_PRESSURE,
        OutputQuantity.ARRIVAL_TIME,
    }
    unsupported = {item.quantity for item in UNSUPPORTED_FLOWPY_OUTPUTS}
    assert unsupported == {
        OutputQuantity.FLOW_DEPTH,
        OutputQuantity.FLOW_VELOCITY,
        OutputQuantity.FLOW_PRESSURE,
        OutputQuantity.ARRIVAL_TIME,
    }
    assert all(len(item.reason) > 40 for item in UNSUPPORTED_FLOWPY_OUTPUTS)
    # The two engines must not be describable by the same capability set, or a
    # caller could treat one as a drop-in substitute for the other.
    assert set(AVAFRAME_COM1DFA.output_capabilities) != outputs


def test_new_normalized_quantities_carry_explicit_canonical_units():
    assert CANONICAL_OUTPUT_UNITS[OutputQuantity.ENERGY_LINE_HEIGHT] == "m"
    assert CANONICAL_OUTPUT_UNITS[OutputQuantity.TRAVEL_ANGLE] == "degree"
    assert CANONICAL_OUTPUT_UNITS[OutputQuantity.ARRIVAL_TIME] == "s"


@pytest.mark.parametrize(
    "parameters",
    [
        {"alpha_angle": 0.0},
        {"alpha_angle": 90.0},
        {"flowpy_exponent": 0.5},
        {"flowpy_exponent": 8.5},
        {"flux_threshold": 0.0},
        {"flux_threshold": 1.0},
        {"max_energy_line_height": 0.0},
        {"alpha_angle": True},
    ],
)
def test_flowpy_rejects_out_of_domain_routing_parameters(parameters):
    complete = {
        "alpha_angle": 25.0,
        "flowpy_exponent": 8.0,
        "flux_threshold": 0.0003,
        "max_energy_line_height": 270.0,
        **parameters,
    }
    with pytest.raises(ValueError):
        AvaFrameCom4FlowPyAdapter._validate_parameters(complete)


def test_energy_line_reference_matches_the_published_routing_rule():
    """Step the upstream rule cell by cell and compare with the closed form."""

    case = EnergyLineCase()
    elevation = case.elevation()
    column = case.release_column
    alpha = 25.0
    reference = flowpy_energy_line_reference(
        elevation=elevation,
        release_row=case.release_row,
        release_column=column,
        cell_size_m=case.cell_size_m,
        alpha_degrees=alpha,
        max_energy_line_height_m=1.0e6,
    )

    stepped = np.zeros(case.rows, dtype=np.float64)
    drop_per_cell = np.diff(elevation[:, column].astype(np.float64))
    step_cost = case.cell_size_m * math.tan(math.radians(alpha))
    for row in range(case.release_row, case.rows - 1):
        stepped[row + 1] = stepped[row] + (-drop_per_cell[row]) - step_cost
    compare = reference > 0.0
    assert compare.any()
    assert np.allclose(stepped[compare], reference[compare], atol=1.0e-9)


def test_energy_line_reference_is_clipped_and_starts_at_the_release_cell():
    case = EnergyLineCase()
    reference = flowpy_energy_line_reference(
        elevation=case.elevation(),
        release_row=case.release_row,
        release_column=case.release_column,
        cell_size_m=case.cell_size_m,
        alpha_degrees=25.0,
        max_energy_line_height_m=10.0,
    )
    assert reference.max() <= 10.0
    assert reference.min() >= 0.0
    assert np.all(reference[: case.release_row] == 0.0)


def test_frozen_energy_line_benchmark_passed_and_stays_bound_to_its_thresholds():
    runs = sorted((ENERGY_LINE_DIR / "runs").iterdir())
    assert runs, "The frozen Flow-Py analytical benchmark record is missing."
    for run in runs:
        report = verify_stored_benchmark(run, ACCEPTANCE)
        assert report["benchmark_id"] == BENCHMARK_ID
        assert report["passed"] is True
        assert report["engine"]["engine_id"] == AVAFRAME_FLOWPY.engine_id
        assert report["engine"]["license_spdx"] == "EUPL-1.2"
        # The stopping cell is the sharpest statement the case makes: the model
        # must halt exactly where the angle-of-reach line meets the terrain.
        assert report["metrics"]["stopping_row_difference_cells"] == 0.0
        assert report["analytic_last_reached_row"] == report["modelled_last_reached_row"]
        for name, limit in report["acceptance_limits"].items():
            assert abs(report["metrics"][name]) <= limit
        assert all(report["metric_passed"].values())
        assert report["invariants"]["velocity_depth_pressure_arrival_unsupported"] is True
        assert report["invariants"]["unreached_cells_are_zero_not_masked"] is True


def test_energy_line_acceptance_document_is_self_identifying():
    payload = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    assert payload["benchmark_id"] == BENCHMARK_ID
    assert "doi.org/10.5194/gmd-15-2423-2022" in payload["upstream_reference"]["doi"]
    assert payload["upstream_reference"]["executed_implementation"].startswith("AvaFrame com4FlowPy")
    with pytest.raises(Exception):
        edited = dict(payload)
        edited["acceptance_metrics"] = {**payload["acceptance_metrics"], "stopping_row_difference_cells": 99.0}
        tampered = ENERGY_LINE_DIR / "tampered-acceptance.json"
        try:
            tampered.write_text(json.dumps(edited), encoding="utf-8")
            verify_stored_benchmark(sorted((ENERGY_LINE_DIR / "runs").iterdir())[0], tampered)
        finally:
            tampered.unlink(missing_ok=True)


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_flowpy_energy_line_benchmark_passes_and_replays(tmp_path: Path):
    from app.processing.runout.flowpy_benchmark import run_energy_line_benchmark

    python = Path(os.environ["AVAFRAME_TEST_PYTHON"])
    first = run_energy_line_benchmark(
        avaframe_python=python, acceptance_path=ACCEPTANCE, output_root=tmp_path / "runs"
    )
    second = run_energy_line_benchmark(
        avaframe_python=python, acceptance_path=ACCEPTANCE, output_root=tmp_path / "runs"
    )
    assert first.report["passed"] is True
    assert first.result_id == second.result_id
    assert first.runout.result_id == second.runout.result_id
    assert first.runout.flow_depth is None
    assert first.runout.flow_velocity is None
    assert first.runout.flow_pressure is None
    assert first.runout.energy_line_height is not None
    assert first.runout.energy_line_height.unit == "m"
    assert first.runout.travel_angle is not None
    assert first.runout.travel_angle.unit == "degree"

    upstream = json.loads((first.bundle_path / "upstream-implementation.json").read_text())
    assert upstream["provider"] == "avaframe.com4FlowPy"
    assert upstream["upstream_family"] == "flow-py"
    assert upstream["avaframe_version"] == "2.1"
    assert all(len(item["sha256"]) == 64 for item in upstream["files"])


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_two_engine_comparison_runs_from_one_release_and_reports_disagreement(tmp_path: Path):
    from app.processing.runout.synthetic import run_synthetic_engine_comparison

    run = run_synthetic_engine_comparison(
        avaframe_python=Path(os.environ["AVAFRAME_TEST_PYTHON"]),
        output_root=tmp_path / "comparison",
        simulation_time_s=25.0,
    )
    assert run.com1dfa.provenance.engine_id == "runout.avaframe_com1dfa"
    assert run.flowpy.provenance.engine_id == "runout.avaframe_flowpy"

    # Both engines must be bound to the same release identity: a comparison of
    # two differently prepared inputs measures the preparation, not the models.
    release_hash = run.release.result_id.rsplit("-", 1)[1]
    for result in (run.com1dfa, run.flowpy):
        assert result.site_id == run.release.site_id
        assert result.disclaimer == run.release.disclaimer
    for bundle in (run.com1dfa_bundle, run.flowpy_bundle):
        inputs = json.loads((bundle / "result.json").read_text())
        assert inputs["provenance"]["input_manifest_sha256"]
    assert release_hash

    metrics = {metric.name: metric for metric in run.comparison.metrics}
    assert metrics["extent_intersection_over_union"].status == "available"
    assert 0.0 <= metrics["extent_intersection_over_union"].value <= 1.0
    assert metrics["maximum_reach_difference"].status == "available"
    assert metrics["common_valid_coverage_fraction"].value == pytest.approx(1.0)
    for name in ("depth", "velocity", "pressure", "arrival_time"):
        metric = metrics[f"{name}_mean_absolute_difference"]
        assert metric.status == "unsupported"
        assert metric.value is None
        assert "Flow-Py" in metric.semantics
    assert any("disagreement" in item for item in run.comparison.limitations)


def test_upstream_identity_survives_a_windows_crlf_checkout(tmp_path: Path):
    """The reviewed digest is a property of the commit, not of the checkout.

    Git rewrites line endings on checkout, so a byte-for-byte hash would reject a
    perfectly valid Flow-Py checkout on Windows and accept it on Linux.
    """

    body = "import sys\nprint(sys.argv)\n"
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    lf.write_bytes(body.encode())
    crlf.write_bytes(body.replace("\n", "\r\n").encode())
    assert lf.read_bytes() != crlf.read_bytes()
    assert normalized_text_sha256(lf) == normalized_text_sha256(crlf)


def test_a_released_v1_0_3_checkout_is_recognised_and_still_refused(tmp_path: Path):
    released = UPSTREAM_FLOWPY_REVIEWED_COMMITS["7b061599355cef584491d69eae2686307d286901"]
    checkout = tmp_path / "FlowPy"
    checkout.mkdir()
    # Stand in for the real file: the adapter matches on the recorded digest, so
    # the test pins the digest rather than shipping a copy of GPL-3.0 upstream code.
    (checkout / "main.py").write_text("placeholder\n", encoding="utf-8")
    adapter = UpstreamFlowPyAdapter(checkout)
    assert adapter.availability().status == "unavailable"

    UPSTREAM_FLOWPY_REVIEWED_COMMITS["7b061599355cef584491d69eae2686307d286901"] = {
        **released,
        "main_py_sha256": normalized_text_sha256(checkout / "main.py"),
    }
    try:
        availability = adapter.availability()
        assert availability.status == "misconfigured"
        assert "v1.0.3" in availability.reason
        assert "ignores adapter arguments" in availability.reason
        assert availability.detected_version == "1.0.3"
    finally:
        UPSTREAM_FLOWPY_REVIEWED_COMMITS["7b061599355cef584491d69eae2686307d286901"] = released
