"""M0 baseline acceptance tests; synthetic software verification only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import cli
from app.baseline import (
    BASELINE_SCHEMA,
    CASE_NAMES,
    compare_engines,
    frozen_results,
    load_expectations,
    run_case,
)


BASELINE_PATH = Path(__file__).parents[1] / "backend" / "config" / "m0-baseline.json"


def test_checked_in_m0_manifest_is_exactly_reproducible() -> None:
    expected = load_expectations(BASELINE_PATH)

    assert expected == frozen_results(seed=expected["seed"])
    assert expected["schema"] == BASELINE_SCHEMA
    assert expected["purpose"] == "synthetic software verification; not field validation"


def test_comparison_runs_both_engines_on_identical_inputs_and_records_required_evidence() -> None:
    report = compare_engines(
        "fast",
        "advanced",
        expectations_path=BASELINE_PATH,
    )

    assert report["all_checked_results_match"] is True
    assert report["frozen_baseline_mismatches"] == []
    assert tuple(case["case"] for case in report["cases"]) == CASE_NAMES
    for comparison in report["cases"]:
        baseline = comparison["baseline"]
        candidate = comparison["candidate"]
        assert baseline["input_sha256"] == comparison["identical_input_sha256"]
        assert candidate["input_sha256"] == comparison["identical_input_sha256"]
        assert baseline["input_coverage"] == candidate["input_coverage"]
        assert baseline["matches_frozen_baseline"] is True
        assert candidate["matches_frozen_baseline"] is True
        for measurement in (baseline, candidate):
            assert len(measurement["output_sha256"]) == 64
            assert measurement["performance"]["runtime_seconds"] >= 0.0
            assert measurement["performance"]["peak_rss_bytes"] > 0
            assert measurement["performance"]["peak_rss_delta_bytes"] >= 0
            assert measurement["performance"]["performance_is_acceptance_threshold"] is False

    by_case = {item["case"]: item for item in report["cases"]}
    assert by_case["benign"]["baseline"]["summary"]["reached_cells"] == 0
    assert by_case["loaded"]["baseline"]["summary"]["reached_cells"] > 0
    missing = by_case["missing_data"]["baseline"]["input_coverage"]
    assert missing["release_required_valid_fraction"] < 1.0
    assert missing["runout_required_valid_fraction"] < 1.0
    assert by_case["aoi_boundary"]["candidate"]["summary"]["particles_left_the_aoi"] > 0


def test_seeded_candidate_replay_keeps_input_and_output_hashes_exact() -> None:
    first = run_case("advanced", "loaded", seed=424242)
    replay = run_case("advanced", "loaded", seed=424242)

    assert first["input_sha256"] == replay["input_sha256"]
    assert first["input_coverage"] == replay["input_coverage"]
    assert first["output_sha256"] == replay["output_sha256"]
    assert first["summary"] == replay["summary"]


def test_compare_baseline_cli_writes_a_machine_readable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "comparison.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "app.cli",
            "compare-baseline",
            "--expectations",
            str(BASELINE_PATH),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["all_checked_results_match"] is True
    assert report["baseline_engine"] == "fast"
    assert report["candidate_engine"] == "advanced"
