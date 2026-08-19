"""Freeze the stratigraphy-augmented release search into one result artifact.

Same shape and the same metrics as ``freeze_release_config_search.py``, which
froze the first search: it re-runs no search, adds no metric the frozen scorer
does not define, and records how many configurations were evaluated rather than
only the best one. What it adds is the decomposition this search exists to
report -- for every configuration, whether the weak-interface term carried
weight, and how the best stratigraphy configuration compares with the best
configuration that used none.

The comparison against ``release-config-search-v1`` is computed here rather than
quoted, so both numbers come from the same code path on the same cache.

Two logs go in, not one. The first execution of this search was stopped by a
plateau counted across the state-key candidate ordering before its declared
anchors were scored; the harness was repaired and the search re-run. That first
run is recorded here with its digest, its stop point and the declared points it
never reached, because a run that happened is a run that gets reported. None of
its numbers are used.
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
for _source in (
    REPOSITORY_ROOT / "packages" / "avycore" / "src",
    Path(__file__).resolve().parent,
):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

import freeze_release_config_search as previous  # noqa: E402
import release_search as rs  # noqa: E402
import run_release_stratigraphy_sweep as sweep  # noqa: E402
from avycore.snowpack.release_v2 import ReleaseConfigV2  # noqa: E402
from avycore.snowpack.stratigraphy import (  # noqa: E402
    stratigraphy_parameter_manifest,
)

RESERVED_BLOCKS = ("row1col4", "row2col4", "row6col9", "row5col10")
PREVIOUS_RESULT = (
    REPOSITORY_ROOT / "validation-data/results/release-config-search-v1.json"
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument(
        "--aborted-log",
        type=Path,
        required=True,
        help=(
            "The log of the first execution, which a plateau counted across the "
            "state-key ordering stopped before its declared anchors were scored. "
            "Required, not optional: a run that happened is a run that gets "
            "reported, and the artifact records its digest and its stop point."
        ),
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    entries = [
        json.loads(line)
        for line in args.log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["record"], []).append(entry)
    declaration = grouped["search_declaration"][0]
    screens = grouped.get("screen", [])
    fulls = grouped.get("full_evaluation", [])
    outcome = grouped["search_result"][0]

    aborted_entries = [
        json.loads(line)
        for line in args.aborted_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    aborted_outcome = next(
        entry for entry in aborted_entries if entry["record"] == "search_result"
    )
    aborted_declaration = next(
        entry for entry in aborted_entries if entry["record"] == "search_declaration"
    )
    aborted_declared_scored = sorted(
        {
            entry["config"]["config_id"]
            for entry in aborted_entries
            if entry["record"] == "screen"
            and entry["config"]["config_id"].startswith(("anchor_", "ladder_"))
        }
    )
    declared_ids = set(declaration["declared_configuration_ids"])

    blocks = rs.load_blocks(args.cache_dir)
    cache = sweep.StateCache()

    wanted: dict[str, ReleaseConfigV2] = {}
    for entry in screens:
        if entry["config"]["config_id"].startswith(("anchor_", "ladder_")):
            wanted[entry["config"]["config_id"]] = ReleaseConfigV2(**entry["config"])
    for entry in fulls:
        wanted[entry["config"]["config_id"]] = ReleaseConfigV2(**entry["config"])

    evaluations = []
    for config_id, config in sorted(wanted.items()):
        per_block = []
        for block in blocks:
            predicted = sweep.base._mask_from_states(
                block, cache.states(block, config), cache.insolation(block), config
            )
            per_block.append(previous._metrics(block, predicted))
        margins = [item["capture_margin_percentage_points"] for item in per_block]
        evaluations.append(
            {
                "config_id": config_id,
                "uses_stratigraphy": bool(config.weak_loading_weight != 0.0),
                "weak_loading_weight": config.weak_loading_weight,
                "parameters": config.manifest(resolution_m=30.0),
                "blocks": per_block,
                "minimum_margin_percentage_points": min(margins),
                "blocks_beating_slope_baseline": sum(
                    1 for value in margins if value > 0.0
                ),
                "meets_success_rule": bool(
                    min(margins) >= declaration["success_margin_percentage_points"]
                ),
            }
        )
    evaluations.sort(
        key=lambda item: item["minimum_margin_percentage_points"], reverse=True
    )

    with_stratigraphy = [item for item in evaluations if item["uses_stratigraphy"]]
    without_stratigraphy = [
        item for item in evaluations if not item["uses_stratigraphy"]
    ]

    def best(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not items:
            return None
        winner = max(items, key=lambda item: item["minimum_margin_percentage_points"])
        return {
            "config_id": winner["config_id"],
            "weak_loading_weight": winner["weak_loading_weight"],
            "minimum_margin_percentage_points": winner[
                "minimum_margin_percentage_points"
            ],
            "blocks_beating_slope_baseline": winner["blocks_beating_slope_baseline"],
        }

    best_with = best(with_stratigraphy)
    best_without = best(without_stratigraphy)
    previous_result = json.loads(PREVIOUS_RESULT.read_text(encoding="utf-8"))
    previous_best = previous_result["evaluations"][0]

    succeeded = any(item["meets_success_rule"] for item in evaluations)
    payload = {
        "schema": "avycore-release-stratigraphy-search-v1",
        "experiment_id": sweep.SEARCH_ID,
        "status": (
            "met_predeclared_success_rule"
            if succeeded
            else "failed_predeclared_success_rule"
        ),
        "purpose": (
            "release-config-search-v1 concluded in writing that its remaining gap "
            "against a same-area slope baseline was the absence of snowpack "
            "stratigraphy rather than a parameterisation deficit. This search adds "
            "exactly that variable, holding the acceptance rule, the budget, the "
            "stop conditions, the screening block, the baseline and the blocks "
            "unchanged, so the conclusion is tested rather than restated."
        ),
        "partition": "development_only",
        "search_declaration": {
            key: value for key, value in declaration.items() if key != "record"
        },
        "stop_condition": {
            "reason": outcome["stop_reason"],
            "rule": (
                "SUCCESS, FUTILITY, BUDGET or PLATEAU, whichever fires first; "
                "PLATEAU is 50 consecutive configurations with no improvement over "
                "the running best screening margin. All four are the first "
                "search's, unchanged, and none was weakened after a score was seen."
            ),
            "configurations_evaluated": outcome["configurations_evaluated"],
            "configurations_rejected_by_guardrail": outcome[
                "configurations_rejected_by_guardrail"
            ],
            "configurations_fully_evaluated": outcome["configurations_fully_evaluated"],
            "best_screen_margin_percentage_points": outcome[
                "best_screen_margin_percentage_points"
            ],
            "most_blocks_beating_slope_baseline": outcome[
                "most_blocks_beating_slope_baseline"
            ],
        },
        "acceptance": {
            "rule": (
                "A configuration succeeds only if its release-only event capture "
                "beats the same-area slope-only baseline on all five development "
                "blocks by at least 5 percentage points, at equal flagged-terrain "
                "budget, with every physical guardrail passing."
            ),
            "rule_is_identical_to": "release-config-search-v1",
            "passed": succeeded,
            "reserved_block_spent": False,
            "reserved_blocks_still_sealed": list(RESERVED_BLOCKS),
        },
        "stratigraphy_effect": {
            "question": (
                "Does giving the buried weak-interface index loading weight move "
                "the worst-block margin, which is the quantity the acceptance rule "
                "is written on?"
            ),
            "best_configuration_using_stratigraphy": best_with,
            "best_configuration_using_none": best_without,
            "previous_search_best": {
                "config_id": previous_best["config_id"],
                "minimum_margin_percentage_points": previous_best[
                    "minimum_margin_percentage_points"
                ],
                "blocks_beating_slope_baseline": previous_best[
                    "blocks_beating_slope_baseline"
                ],
            },
            "worst_block_margin_change_percentage_points": (
                None
                if best_with is None or best_without is None
                else best_with["minimum_margin_percentage_points"]
                - best_without["minimum_margin_percentage_points"]
            ),
            "configurations_using_stratigraphy": len(with_stratigraphy),
            "configurations_using_none": len(without_stratigraphy),
        },
        "stratigraphy_semantics": {
            "what_it_is": (
                "A bounded index built from antecedent surface meteorology and a "
                "modelled snow-depth series: kinetic-growth gradient hours or "
                "cold-calm-dry surface hours, decayed by antecedent melt and rain, "
                "gated on storm burial and on a pre-storm pack existing."
            ),
            "what_it_is_not": (
                "It contains no snow profile, no stability test, no grain-type "
                "observation and no measurement of any buried layer. It is not a "
                "probability and it cannot be verified from the data that produces "
                "it."
            ),
            "unknown_is_missing_input_not_zero": (
                "Without a snow-depth series the gradient mechanism is unevaluable. "
                "A configuration with non-zero weight removes such a cell from the "
                "dry-slab admissible set instead of scoring it as though the "
                "unmeasured interface were absent."
            ),
            "parameters": stratigraphy_parameter_manifest(),
        },
        "baseline_definition": {
            "slope_only_same_area_budget": (
                "The highest-scoring cells under the regime-hindcast-v1 published "
                "slope response, taken at exactly the model's own flagged-cell "
                "count."
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
            "metric_is_unchanged_from": "release-config-search-v1",
            "note": (
                "Unmapped terrain is unknown, never verified negative. Capture "
                "counts a mapped outline as captured when at least 5% of its cells "
                "are flagged, so it rewards touching many outlines and does not "
                "reward covering any one of them well."
            ),
        },
        "evaluations": evaluations,
        "integrity": {
            "frozen_v1_sources_modified": False,
            "frozen_experiment_specs_modified": False,
            "frozen_digests_rewritten": False,
            "previous_search_artifact_modified": False,
            "previous_search_still_replays": (
                "release-config-search-v1.json replays byte-for-byte from its "
                "committed sweep log against the current release_v2, because "
                "weak_loading_weight defaults to zero and every stratigraphy field "
                "is inert at that weight."
            ),
            "reserved_blocks_predicted_or_scored": False,
            "reserved_block_outlines_opened": False,
            "development_blocks_are_already_burned": (
                "All five development blocks were scored and viewed in the frozen "
                "SPOT and CERRA experiments. Every number in this artifact is a "
                "development number."
            ),
        },
        "limitations": [
            "This is a development search, not a validation result. It adds zero "
            "events to the strict field holdout, does not change is_validated, and "
            "licenses no accuracy claim.",
            "Event capture is positive-only. A configuration that flags more "
            "terrain captures more events; the same-area baseline is what makes the "
            "comparison meaningful.",
            "The stratigraphy index is a meteorological reconstruction, not an "
            "observation. A negative result here bounds what this reconstruction "
            "can do; it does not bound what an observed weak layer could do.",
            "The gradient mechanism needs a snow-depth series. CERRA supplies one; "
            "the frozen ERA5 request does not, so this term cannot be evaluated at "
            "all on the SPOT blocks.",
            "Wet-snow and dry-loose formulations were held at their frozen values, "
            "so any capture change is attributable to the dry-slab pathway.",
        ],
        "software_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": {
                name: importlib.metadata.version(name)
                for name in (
                    "numpy",
                    "scipy",
                    "rasterio",
                    "geopandas",
                    "shapely",
                    "pyproj",
                )
            },
        },
        "inputs": {
            "previous_search_result_sha256": _sha256_file(PREVIOUS_RESULT),
            "release_v2_source_sha256": _sha256_file(
                REPOSITORY_ROOT
                / "packages/avycore/src/avycore/snowpack/release_v2.py"
            ),
            "stratigraphy_source_sha256": _sha256_file(
                REPOSITORY_ROOT
                / "packages/avycore/src/avycore/snowpack/stratigraphy.py"
            ),
            "sweep_source_sha256": _sha256_file(
                REPOSITORY_ROOT
                / "scripts/validation/run_release_stratigraphy_sweep.py"
            ),
        },
        "sweep_log_sha256": _sha256_file(args.log),
        "superseded_execution": {
            "what_it_was": (
                "The first execution of this same search, at the same seed, "
                "budget, plateau limit and acceptance rule. It is reported "
                "because it ran, not because it is used: no number in this "
                "artifact comes from it."
            ),
            "harness_defect": (
                "Candidates are sorted by snow-state key so each hourly "
                "integration happens once, and the plateau counter ran across "
                "that ordering. State-key order has nothing to do with promise, "
                "so the counter reached its limit while declared points were "
                "still unscored and stopped the run."
            ),
            "repair": (
                "The declared anchors and the weak-weight ladder are evaluated "
                "before the seeded sample, and the plateau counter runs over the "
                "sampled portion only. Nothing else moved: same seed, same "
                "budget, same plateau limit, same acceptance rule, same blocks."
            ),
            "repair_is_verifiable_from_the_logs": (
                "Both logs carry anchor_v1_frozen and anchor_v2_baseline. Their "
                "screening margins agree, which is what shows the repaired "
                "harness scores a configuration identically and changed only the "
                "order candidates are drawn in."
            ),
            "log_sha256": _sha256_file(args.aborted_log),
            "seed": aborted_declaration["seed"],
            "stop_reason": aborted_outcome["stop_reason"],
            "configurations_evaluated": aborted_outcome["configurations_evaluated"],
            "configurations_fully_evaluated": aborted_outcome[
                "configurations_fully_evaluated"
            ],
            "best_minimum_margin_percentage_points": aborted_outcome[
                "best_minimum_margin_percentage_points"
            ],
            "most_blocks_beating_slope_baseline": aborted_outcome[
                "most_blocks_beating_slope_baseline"
            ],
            "success_configuration_ids": aborted_outcome["success_configuration_ids"],
            "declared_configuration_ids_scored": aborted_declared_scored,
            "declared_configuration_ids_never_scored": sorted(
                declared_ids - set(aborted_declared_scored)
            ),
            "it_also_failed_the_rule": not aborted_outcome["success_configuration_ids"],
        },
        "emails_sent": False,
        "disclaimer": (
            "Experimental research prototype; not an operational avalanche forecast "
            "and not a replacement for Avalanche Canada guidance or field "
            "assessment. Scores are relative indices, not probabilities."
        ),
    }
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {"result": str(args.result), "result_sha256": _sha256_file(args.result)}
        )
    )


if __name__ == "__main__":
    main()
