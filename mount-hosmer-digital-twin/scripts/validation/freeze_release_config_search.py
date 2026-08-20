"""Freeze the release-configuration search into one committed result artifact.

Reads the sweep log and recomputes, for the anchors and every fully-evaluated
configuration, the metrics the frozen experiments report: event capture,
mapped-positive footprint coverage, and flagged eligible terrain, each beside
the same-area slope-only baseline. It adds no metric the frozen scorer does not
already define and it re-runs no search, so it cannot improve a score.

The artifact records the total number of configurations evaluated, not only the
best one. Without that count the best margin is uninterpretable.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for _source in (REPOSITORY_ROOT / "packages" / "avycore" / "src", Path(__file__).resolve().parent):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

import release_search as rs  # noqa: E402
import run_release_config_sweep as sweep  # noqa: E402
from avycore.snowpack.release_v2 import ReleaseConfigV2  # noqa: E402

RESERVED_BLOCKS = ("row1col4", "row2col4", "row6col9", "row5col10")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(block: rs.Block, predicted: np.ndarray) -> dict[str, Any]:
    """Capture, coverage and budget for a footprint, plus the slope baseline."""
    flat = block.eligible_flat
    selection = predicted.reshape(-1)[flat].astype(bool)
    flagged = int(selection.sum())
    mapped = np.asarray(block.membership.sum(axis=0)).ravel() > 0
    mapped_count = int(mapped.sum())

    def capture(chosen: np.ndarray) -> int:
        overlaps = block.membership @ chosen.astype("int32")
        return int(np.count_nonzero((overlaps >= block.event_required) & block.event_scorable))

    scores = np.interp(
        block.slope.reshape(-1)[flat],
        rs.BASELINE_SLOPE_BREAKPOINTS_DEG,
        rs.BASELINE_SLOPE_SCORES,
    )
    order = np.argsort(-scores, kind="stable")
    baseline = np.zeros(flat.size, dtype=bool)
    baseline[order[:flagged]] = True

    captured, baseline_captured = capture(selection), capture(baseline)
    return {
        "block_id": block.block_id,
        "event_count": block.event_count,
        "captured_event_count": captured,
        "event_capture_fraction": captured / block.event_count,
        "mapped_positive_footprint_coverage_fraction": float(
            (selection & mapped).sum() / mapped_count
        ),
        "flagged_eligible_cell_count": flagged,
        "flagged_eligible_terrain_fraction": flagged / flat.size,
        "mean_slope_of_flagged_terrain_deg": float(
            block.slope.reshape(-1)[flat][selection].mean()
        )
        if flagged
        else None,
        "slope_only_same_area_budget": {
            "captured_event_count": baseline_captured,
            "event_capture_fraction": baseline_captured / block.event_count,
            "mapped_positive_footprint_coverage_fraction": float(
                (baseline & mapped).sum() / mapped_count
            ),
            "mean_slope_of_flagged_terrain_deg": float(
                block.slope.reshape(-1)[flat][baseline].mean()
            )
            if flagged
            else None,
        },
        "spatial_agreement_with_slope_baseline_fraction": float(
            (selection & baseline).sum() / flagged
        )
        if flagged
        else None,
        "capture_margin_percentage_points": 100.0
        * (captured - baseline_captured)
        / block.event_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    entries = [json.loads(line) for line in args.log.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["record"], []).append(entry)
    declaration = grouped["search_declaration"][0]
    screens = grouped.get("screen", [])
    fulls = grouped.get("full_evaluation", [])
    outcome = grouped["search_result"][0]

    blocks = rs.load_blocks(args.cache_dir)
    cache = sweep.StateCache()

    wanted: dict[str, ReleaseConfigV2] = {}
    for entry in screens:
        if entry["config"]["config_id"].startswith("anchor_"):
            wanted[entry["config"]["config_id"]] = ReleaseConfigV2(**entry["config"])
    for entry in fulls:
        wanted[entry["config"]["config_id"]] = ReleaseConfigV2(**entry["config"])

    evaluations = []
    for config_id, config in sorted(wanted.items()):
        per_block = []
        for block in blocks:
            predicted = sweep._mask_from_states(
                block, cache.states(block, config), cache.insolation(block), config
            )
            per_block.append(_metrics(block, predicted))
        margins = [item["capture_margin_percentage_points"] for item in per_block]
        evaluations.append(
            {
                "config_id": config_id,
                "parameters": config.manifest(resolution_m=30.0),
                "blocks": per_block,
                "minimum_margin_percentage_points": min(margins),
                "blocks_beating_slope_baseline": sum(1 for value in margins if value > 0.0),
                "meets_success_rule": bool(
                    min(margins) >= declaration["success_margin_percentage_points"]
                ),
            }
        )
    evaluations.sort(key=lambda item: item["minimum_margin_percentage_points"], reverse=True)

    payload = {
        "schema": "avycore-release-config-search-v1",
        "experiment_id": "release-config-search-v1",
        "status": "failed_predeclared_success_rule",
        "purpose": (
            "Repair the three documented release-engine defects, then search release "
            "configurations on development blocks only, to decide whether one reserved "
            "1999 lattice block should be spent on a confirmatory test."
        ),
        "partition": "development_only",
        "search_declaration": {
            key: value for key, value in declaration.items() if key != "record"
        },
        "stop_condition": {
            "reason": outcome["stop_reason"],
            "rule": (
                "PLATEAU: 50 consecutive configurations with no improvement over the "
                "running best screening margin. Predeclared; not weakened after any "
                "score was seen."
            ),
            "configurations_evaluated": outcome["configurations_evaluated"],
            "configurations_rejected_by_guardrail": outcome[
                "configurations_rejected_by_guardrail"
            ],
            "configurations_fully_evaluated": outcome["configurations_fully_evaluated"],
            "best_screen_margin_percentage_points": outcome[
                "best_screen_margin_percentage_points"
            ],
            "most_blocks_beating_slope_baseline": outcome["most_blocks_beating_slope_baseline"],
        },
        "acceptance": {
            "rule": (
                "A configuration succeeds only if its release-only event capture beats "
                "the same-area slope-only baseline on all five development blocks by at "
                "least 5 percentage points, at equal flagged-terrain budget, with every "
                "physical guardrail passing."
            ),
            "passed": False,
            "reserved_block_spent": False,
            "reserved_blocks_still_sealed": list(RESERVED_BLOCKS),
        },
        "baseline_definition": {
            "slope_only_same_area_budget": (
                "The highest-scoring cells under the regime-hindcast-v1 published slope "
                "response, taken at exactly the model's own flagged-cell count."
            ),
            "slope_curve_is_pinned": True,
            "why_pinned": (
                "Candidate configurations may move their own slope response. If the "
                "baseline moved with them the comparison would measure nothing."
            ),
        },
        "metric_semantics": {
            "positive_only": True,
            "capture_minimum_overlap_fraction": rs.CAPTURE_MINIMUM_OVERLAP_FRACTION,
            "unmapped_cells_treated_as_negative": False,
            "note": (
                "Unmapped terrain is unknown, never verified negative. Capture counts a "
                "mapped outline as captured when at least 5% of its cells are flagged, "
                "so it rewards touching many outlines and does not reward covering any "
                "one of them well."
            ),
        },
        "evaluations": evaluations,
        "integrity": {
            "frozen_v1_sources_modified": False,
            "frozen_experiment_specs_modified": False,
            "frozen_digests_rewritten": False,
            "repaired_engine_reproduces_v1": (
                "avycore.snowpack.release_v2 at its V1_FROZEN configuration reproduces "
                "all five committed regime-hindcast-v1 development release masks "
                "cell-for-cell, so every difference reported here is attributable to a "
                "configuration change and not to a reimplementation."
            ),
            "reserved_blocks_predicted_or_scored": False,
            "reserved_block_outlines_opened": False,
            "development_blocks_are_already_burned": (
                "All five development blocks were scored and viewed in the frozen SPOT "
                "and CERRA experiments. Every number in this artifact is a development "
                "number."
            ),
        },
        "limitations": [
            "This is a development search, not a validation result. It adds zero events "
            "to the strict field holdout, does not change is_validated, and licenses no "
            "accuracy claim.",
            "Event capture is positive-only. A configuration that flags more terrain "
            "captures more events; the same-area baseline is what makes the comparison "
            "meaningful, and the model loses it.",
            "The search moved release localization only. No runout engine was simulated, "
            "because release localization dominated both frozen failures.",
            "Wet-snow and dry-loose formulations were held at their frozen values so any "
            "capture change is attributable to the dry-slab pathway and its extraction.",
        ],
        "software_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": {
                name: importlib.metadata.version(name)
                for name in ("numpy", "scipy", "rasterio", "geopandas", "shapely", "pyproj")
            },
        },
        "sweep_log_sha256": _sha256_file(args.log),
        "emails_sent": False,
        "disclaimer": (
            "Experimental research prototype; not an operational avalanche forecast and "
            "not a replacement for Avalanche Canada guidance or field assessment. Scores "
            "are relative indices, not probabilities."
        ),
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"result": str(args.result), "result_sha256": _sha256_file(args.result)}))


if __name__ == "__main__":
    main()
