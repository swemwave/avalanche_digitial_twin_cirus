"""Summarise a release-configuration sweep log into the numbers a report needs.

Reads the JSONL the sweep writes and prints: how many configurations were
evaluated in total (not just the winners), how many the guardrails rejected,
the anchors' own scores, the best candidate on each axis, and whether any
predeclared stop condition was met. Nothing here re-runs the model, so it
cannot change a score.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        grouped.setdefault(entry["record"], []).append(entry)
    return grouped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--top", type=int, default=12)
    args = parser.parse_args()

    grouped = load(args.log)
    declaration = grouped.get("search_declaration", [{}])[0]
    screens = grouped.get("screen", [])
    fulls = grouped.get("full_evaluation", [])
    result = grouped.get("search_result", [{}])[0]

    print("=== declaration (before any score was seen) ===")
    for key in (
        "screen_block_id",
        "promotion_fraction",
        "success_margin_percentage_points",
        "maximum_flagged_eligible_terrain_fraction",
        "configuration_budget",
        "plateau_limit",
        "seed",
    ):
        print(f"  {key}: {declaration.get(key)}")

    print("\n=== configurations evaluated ===")
    outcomes = Counter(entry["outcome"] for entry in screens)
    print(f"  total screened: {len(screens)}")
    for name, count in sorted(outcomes.items()):
        print(f"    {name}: {count}")
    failed = Counter()
    for entry in screens:
        for check, ok in entry["guardrails"]["checks"].items():
            if not ok:
                failed[check] += 1
    for check, count in sorted(failed.items()):
        print(f"    guardrail failed - {check}: {count}")
    print(f"  promoted to five-block evaluation: {len(fulls)}")

    print("\n=== anchors, screened on the declared screening block ===")
    for entry in screens:
        if entry["config"]["config_id"].startswith(("anchor_", "ladder_")):
            screen = entry["screen"]
            print(
                f"  {entry['config']['config_id']:26s} capture={screen['event_capture_fraction']:.4f} "
                f"slope={screen['slope_baseline_event_capture_fraction']:.4f} "
                f"margin={screen['capture_margin_percentage_points']:+7.2f}pp "
                f"flagged={screen['flagged_eligible_terrain_fraction']:.4f} "
                f"[{entry['outcome']}]"
            )

    print(f"\n=== best {args.top} by screening margin ===")
    ranked = sorted(
        (e for e in screens if e["outcome"] == "screened"),
        key=lambda e: e["screen"]["capture_margin_percentage_points"],
        reverse=True,
    )
    for entry in ranked[: args.top]:
        screen = entry["screen"]
        config = entry["config"]
        print(
            f"  {config['config_id']:16s} margin={screen['capture_margin_percentage_points']:+7.2f}pp "
            f"capture={screen['event_capture_fraction']:.4f} flagged={screen['flagged_eligible_terrain_fraction']:.4f} "
            f"thr={config['release_threshold']:g} base={config['loading_base']:g} "
            f"snow_w={config['snow_loading_weight']:g} wind_w={config['wind_loading_weight']:g} "
            f"kernel={config['drift_kernel']} open={config['opening_structure']}"
        )

    print("\n=== full five-block evaluations ===")
    for entry in sorted(fulls, key=lambda e: e["minimum_margin_percentage_points"], reverse=True):
        margins = ", ".join(f"{value:+.2f}" for value in entry["margins_percentage_points"])
        print(
            f"  {entry['config']['config_id']:16s} min={entry['minimum_margin_percentage_points']:+7.2f}pp "
            f"beats={entry['blocks_beating_slope_baseline']}/5 success={entry['meets_success_rule']} "
            f"margins=[{margins}]"
        )

    print("\n=== outcome ===")
    for key, value in sorted(result.items()):
        if key != "record":
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
