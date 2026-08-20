"""Search release configurations again, with snowpack stratigraphy in the space.

Why a second search, and what is deliberately identical
-------------------------------------------------------
``release-config-search-v1`` evaluated 128 configurations over drift kernels,
release thresholds, loading bases, snow and wind weights, slope responses and
morphologies. It failed its predeclared +5 pp rule and stopped on PLATEAU. Its
written conclusion was specific and testable: the remaining gap is **not**
parameterisation, it is that the model carries no snowpack-stratigraphy
information. This search tests that conclusion by adding exactly that variable
and changing nothing else that could explain a difference.

Held identical to the first search, on purpose:

* the acceptance rule -- at least +5 percentage points of release-only event
  capture over the same-area slope-only baseline on **every** development block;
* the screening block, the promotion fraction, and the pinned slope baseline;
* the configuration budget, the compute budget and the plateau limit;
* the terrain-budget veto and the benign-day veto;
* the five development blocks, all of them already burned.

Changed, and only this:

* the space gains :mod:`avycore.snowpack.stratigraphy`'s weak-interface
  parameters, with ``weak_loading_weight`` as the dimension that matters;
* a second physical veto is added rather than substituted -- a full weak
  interface with **no** load must still produce no release terrain. A weak
  layer is not a hazard by itself, and a configuration that treats it as one
  has stopped being a loading model in the other direction;
* the seed is new, because the sampler draws more values per configuration and
  the old stream cannot be continued;
* the declared anchors and the weak-weight ladder are evaluated **before** the
  seeded sample, and the plateau counter runs over the sampled portion only.

That last point is a defect repair, not a relaxation. Both searches sort
candidates by snow-state key so each hourly integration happens once, and that
order has nothing to do with promise -- so a plateau measured across it can stop
the run before its own declared anchors have been scored. The first execution of
this search did exactly that: it stopped at 66 configurations having evaluated
two of the eight declared points, and none of the weak-weight ladder. Its log is
kept and its digest is recorded in the result artifact, because a run that
happened is a run that gets reported. The seed, the budget, the plateau limit and
the acceptance rule are unchanged; only the order in which candidates are drawn
from the same declared set moved.

The critical temperature gradient (10 K/m) and the minimum depth used in the
gradient estimate are **not** searched. They are literature regime boundaries,
and fitting a physical threshold to a capture score is how a search launders a
tuned constant into a citation.

The reserved 1999 lattice blocks are unreachable from this script, exactly as
before: it loads development caches only and ``release_search.load_blocks``
refuses anything else.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
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

import release_search as rs  # noqa: E402
import run_release_config_sweep as base  # noqa: E402
from avycore.snowpack.release_v2 import (  # noqa: E402
    V1_FROZEN,
    V2_BASELINE,
    ReleaseConfigV2,
    SnowStateV2,
    guardrail_report,
)

SEARCH_ID = "release-stratigraphy-search-v1"

#: Every one of these is the first search's value, restated rather than
#: imported so that a later edit to one search cannot silently move the other.
SCREEN_BLOCK_ID = "development_western_bernese"
PROMOTION_FRACTION = 0.10
SUCCESS_MARGIN_PERCENTAGE_POINTS = 5.0
MAXIMUM_FLAGGED_ELIGIBLE_TERRAIN_FRACTION = 0.20
CONFIGURATION_BUDGET = 200
COMPUTE_BUDGET_SECONDS = 4 * 3600
PLATEAU_LIMIT = 50

#: New, because the sampler's draw order changed. Declared before the run.
SEED = 20260819

#: Fields that change the hourly integration and therefore the cached state.
#: The two gradient fields are here defensively: nothing below varies them, but
#: a cache keyed without them would be silently wrong if anything ever did.
STATE_KEY_FIELDS = base.STATE_KEY_FIELDS + (
    "weak_critical_gradient_k_per_m",
    "weak_gradient_minimum_snow_depth_m",
)

#: The searched weak-interface weights. Zero is included so the space contains
#: the first search's answer and a stratigraphy-free configuration can win.
WEAK_LOADING_WEIGHTS = (0.0, 0.2, 0.4, 0.7, 1.0, 1.5)


def _state_key(config: ReleaseConfigV2) -> tuple:
    return tuple(getattr(config, name) for name in STATE_KEY_FIELDS)


class StateCache(base.StateCache):
    """``base.StateCache`` keyed on the stratigraphy-aware state key."""

    def states(self, block: rs.Block, config: ReleaseConfigV2) -> list[SnowStateV2]:
        key = _state_key(config)
        if key != self._key:
            self._key = key
            self._states = {}
        if block.block_id not in self._states:
            self._states[block.block_id] = base._integrate_cycles(block, config)
        return self._states[block.block_id]


def _best_previous_configuration() -> ReleaseConfigV2:
    """The winning configuration of the first search, rebuilt from its log."""
    result = json.loads(
        (
            REPOSITORY_ROOT / "validation-data/results/release-config-search-v1.json"
        ).read_text(encoding="utf-8")
    )
    best = result["evaluations"][0]["config_id"]
    log = (
        REPOSITORY_ROOT
        / "validation-data/results/release-config-search-v1-sweep-log.jsonl"
    )
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if (
            entry.get("record") == "full_evaluation"
            and entry["config"]["config_id"] == best
        ):
            config = dict(entry["config"])
            for key in ("slope_breakpoints_deg", "slope_scores"):
                config[key] = tuple(config[key])
            return ReleaseConfigV2(**config)
    raise ValueError(f"{best!r} is not in the committed sweep log.")


def candidate_configurations(seed: int, count: int) -> list[ReleaseConfigV2]:
    """Anchors first, then a seeded quasi-random sample of the declared space.

    The anchors are what make the log self-explaining. Three of them reproduce
    points the first search already published, so the reader can see that this
    harness scores them the same way; the ladder is the single most informative
    one-dimensional slice, holding the first search's winner fixed and moving
    only the weak-interface weight.
    """
    previous_best = _best_previous_configuration()
    configs: list[ReleaseConfigV2] = [
        replace(V1_FROZEN, config_id="anchor_v1_frozen"),
        replace(V2_BASELINE, config_id="anchor_v2_baseline"),
        replace(previous_best, config_id="anchor_search_v1_best"),
    ]
    for weight in WEAK_LOADING_WEIGHTS:
        if weight == 0.0:
            continue
        configs.append(
            replace(
                previous_best,
                config_id=f"ladder_weak_weight_{weight:g}",
                weak_loading_weight=weight,
            )
        )

    rng = np.random.default_rng(seed)
    kernels = list(base.KERNEL_REFERENCE)
    while len(configs) < count:
        kernel = kernels[int(rng.integers(len(kernels)))]
        scale = float(rng.choice([0.1, 0.25, 0.5, 1.0, 4.0]))
        curve_name = str(rng.choice(list(base.SLOPE_CURVES)))
        breakpoints, scores = base.SLOPE_CURVES[curve_name]
        weak_weight = float(rng.choice(WEAK_LOADING_WEIGHTS))
        burial_minimum = float(rng.choice([5.0, 10.0, 20.0]))
        index = len(configs)
        configs.append(
            ReleaseConfigV2(
                config_id=f"sample_{index:04d}",
                drift_kernel=kernel,
                drift_index_full=base.KERNEL_REFERENCE[kernel] * scale,
                drift_threshold_dry_ms=float(rng.choice([6.5, 7.7, 9.0])),
                drift_threshold_wet_ms=float(rng.choice([9.0, 9.9, 11.0])),
                drift_settlement_time_h=float(rng.choice([24.0, 48.0, 96.0])),
                new_snow_settlement_time_h=float(rng.choice([24.0, 48.0, 96.0])),
                release_threshold=float(
                    rng.choice([25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 55.0])
                ),
                loading_base=float(rng.choice([0.10, 0.20, 0.35, 0.50])),
                snow_loading_weight=float(rng.choice([0.4, 0.6, 0.9, 1.2])),
                wind_loading_weight=float(rng.choice([0.4, 0.75, 1.2, 1.6])),
                new_snow_full_cm=float(rng.choice([25.0, 50.0, 80.0])),
                slope_breakpoints_deg=breakpoints,
                slope_scores=scores,
                forest_damping_max=float(rng.choice([0.5, 0.7, 0.9])),
                convexity_weight=float(rng.choice([0.0, 0.15, 0.35])),
                minimum_zone_area_m2=float(rng.choice([900.0, 2500.0, 10000.0])),
                opening_structure=str(rng.choice(["none", "cross", "square3"])),
                radius_rounding="ceil",
                weak_loading_weight=weak_weight,
                weak_faceting_full_hours=float(rng.choice([48.0, 72.0, 120.0])),
                weak_melt_destruction_positive_degree_hours=float(
                    rng.choice([12.0, 24.0, 48.0])
                ),
                weak_rain_destruction_mm=float(rng.choice([2.0, 5.0, 10.0])),
                weak_burial_minimum_new_snow_cm=burial_minimum,
                weak_burial_full_new_snow_cm=burial_minimum
                + float(rng.choice([15.0, 30.0, 60.0])),
                weak_slab_minimum_antecedent_depth_m=float(
                    rng.choice([0.1, 0.2, 0.4])
                ),
                threshold_derivation=f"seeded_sample:{curve_name}",
            )
        )
    return configs[:count]


def _benign_state(block: rs.Block, *, weak_interface: bool) -> SnowStateV2:
    """A constructed benign day, optionally carrying a full weak interface.

    ``weak_interface=False`` is the first search's check, unchanged: no new
    snow, no drift, no rain, no melt, cold. ``weak_interface=True`` adds the
    veto this search needs: a fully weakened, deeply buried-capable interface
    under a pack that received **no** storm snow. A weak layer with nothing on
    top of it is not a release, and a configuration that flags terrain for it
    has learned to score the antecedent period instead of the storm.

    Nothing here is missing data. Every value is a stated scenario, which is
    why zeros are legitimate: the guardrail asserts what the model must do on a
    day it is told everything about.
    """
    shape = block.slope.shape
    zero = np.zeros(shape, dtype="float32")
    ones = np.ones(shape, dtype="float32")
    return SnowStateV2(
        new_snow_index_cm=zero.copy(),
        drift_index_normalized=zero.copy(),
        drift_from_direction_deg=np.full(shape, -1.0, dtype="float32"),
        rain_on_snow_mm=zero.copy(),
        positive_degree_hours=zero.copy(),
        antecedent_snow_depth_m=np.full(shape, 1.0, dtype="float32"),
        peak_temperature_c=np.full(shape, -8.0, dtype="float32"),
        mean_storm_temperature_c=np.full(shape, -8.0, dtype="float32"),
        buried_weak_interface_proxy=zero.copy(),
        mask=block.terrain_mask.copy(),
        surface_weakening_fraction=(ones.copy() if weak_interface else zero.copy()),
        faceting_hours=(
            np.full(shape, 1000.0, dtype="float32") if weak_interface else zero.copy()
        ),
        antecedent_positive_degree_hours=zero.copy(),
        antecedent_rain_mm=zero.copy(),
        snow_depth_series_available=True,
    )


def _guardrails(
    block: rs.Block, config: ReleaseConfigV2, screen: dict[str, Any], cache: StateCache
) -> dict[str, Any]:
    insolation = cache.insolation(block)[0]
    benign = base._mask_from_states(
        block, [_benign_state(block, weak_interface=False)], [insolation], config
    )
    report = guardrail_report(
        benign_release_cell_count=int(np.count_nonzero(benign)),
        benign_eligible_cell_count=int(block.eligible_flat.size),
        flagged_outside_eligible_cell_count=screen["flagged_outside_eligible_cell_count"],
        flagged_on_missing_input_cell_count=screen["flagged_on_missing_input_cell_count"],
    )
    weak_only = base._mask_from_states(
        block, [_benign_state(block, weak_interface=True)], [insolation], config
    )
    weak_only_count = int(np.count_nonzero(weak_only))
    report["weak_interface_without_load_release_cell_count"] = weak_only_count
    report["checks"]["weak_interface_without_load_is_quiet"] = (
        weak_only_count / block.eligible_flat.size
    ) <= report["benign_maximum_fraction"]
    report["checks"]["within_terrain_budget"] = (
        screen["flagged_eligible_terrain_fraction"]
        <= MAXIMUM_FLAGGED_ELIGIBLE_TERRAIN_FRACTION
    )
    report["passed"] = all(report["checks"].values())
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--budget", type=int, default=CONFIGURATION_BUDGET)
    args = parser.parse_args()

    blocks = {block.block_id: block for block in rs.load_blocks(args.cache_dir)}
    if SCREEN_BLOCK_ID not in blocks:
        raise SystemExit(f"Screening block {SCREEN_BLOCK_ID} is not cached.")
    screen_block = blocks[SCREEN_BLOCK_ID]
    other_blocks = [b for name, b in sorted(blocks.items()) if name != SCREEN_BLOCK_ID]

    # Declared points first, seeded sample second. Within each group, sort by
    # state key so each hourly integration still happens once. The plateau
    # counter below runs over the sampled group only: an anchor is evaluated
    # because it was declared, not because it is winning, so letting it either
    # reset or advance a plateau would make the stop rule depend on an
    # arbitrary ordering rather than on the search running out of ideas.
    declared, sampled = [], []
    for config in candidate_configurations(args.seed, args.budget):
        target = (
            declared
            if config.config_id.startswith(("anchor_", "ladder_"))
            else sampled
        )
        target.append(config)
    declared.sort(key=lambda item: (_state_key(item), item.config_id))
    sampled.sort(key=lambda item: (_state_key(item), item.config_id))
    configs = declared + sampled
    declared_ids = {config.config_id for config in declared}

    args.log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cache = StateCache()
    screened: list[tuple[float, ReleaseConfigV2, dict[str, Any]]] = []
    evaluated = 0
    rejected = 0
    best_margin = -np.inf
    plateau = 0
    stop_reason = None

    with args.log.open("w", encoding="utf-8", newline="\n") as log:

        def record(payload: dict[str, Any]) -> None:
            log.write(json.dumps(payload, sort_keys=True) + "\n")
            log.flush()

        record(
            {
                "record": "search_declaration",
                "search_id": SEARCH_ID,
                "screen_block_id": SCREEN_BLOCK_ID,
                "promotion_fraction": PROMOTION_FRACTION,
                "success_margin_percentage_points": SUCCESS_MARGIN_PERCENTAGE_POINTS,
                "maximum_flagged_eligible_terrain_fraction": (
                    MAXIMUM_FLAGGED_ELIGIBLE_TERRAIN_FRACTION
                ),
                "configuration_budget": args.budget,
                "compute_budget_seconds": COMPUTE_BUDGET_SECONDS,
                "plateau_limit": PLATEAU_LIMIT,
                "seed": args.seed,
                "baseline_slope_curve": (
                    "regime-hindcast-v1 published slope response, pinned"
                ),
                "blocks": sorted(blocks),
                "new_dimension": "avycore.snowpack.stratigraphy weak-interface index",
                "weak_loading_weights": list(WEAK_LOADING_WEIGHTS),
                "not_searched": [
                    "weak_critical_gradient_k_per_m",
                    "weak_gradient_minimum_snow_depth_m",
                ],
                "not_searched_reason": (
                    "Literature regime boundaries. Fitting a physical threshold "
                    "to a capture score would launder a tuned constant into a "
                    "citation."
                ),
                "guardrails": [
                    "benign_day_quiet",
                    "weak_interface_without_load_is_quiet",
                    "no_flag_outside_eligible",
                    "no_flag_on_missing_input",
                    "within_terrain_budget",
                ],
                "rule_unchanged_from": "release-config-search-v1",
                "declared_configuration_ids": sorted(declared_ids),
                "evaluation_order": (
                    "declared anchors and ladder first, then the seeded sample; "
                    "each group sorted by snow-state key so every hourly "
                    "integration happens once"
                ),
                "plateau_counter_scope": "seeded sample only",
                "plateau_counter_scope_reason": (
                    "A plateau measured across a state-key ordering can stop a "
                    "run before its declared anchors are scored, which is what "
                    "ended this search's first execution at 66 configurations."
                ),
            }
        )

        for config in configs:
            if time.time() - started > COMPUTE_BUDGET_SECONDS:
                stop_reason = "BUDGET_COMPUTE_SECONDS"
                break
            evaluated += 1
            screen = base._score_block(screen_block, config, cache)
            guard = _guardrails(screen_block, config, screen, cache)
            margin = screen["capture_margin_percentage_points"]
            entry = {
                "record": "screen",
                "index": evaluated,
                "config": asdict(config),
                "screen": screen,
                "guardrails": guard,
                "elapsed_seconds": round(time.time() - started, 2),
            }
            if not guard["passed"]:
                rejected += 1
                entry["outcome"] = "rejected_by_guardrail"
                record(entry)
                continue
            entry["outcome"] = "screened"
            entry["is_declared_point"] = config.config_id in declared_ids
            record(entry)
            screened.append((margin, config, screen))
            if config.config_id in declared_ids:
                # A declared point still sets the bar a sampled candidate has to
                # clear -- it just cannot advance the plateau counter.
                best_margin = max(best_margin, margin)
                continue
            if margin > best_margin:
                best_margin, plateau = margin, 0
            else:
                plateau += 1
                if plateau >= PLATEAU_LIMIT:
                    stop_reason = "PLATEAU"
                    break

        if stop_reason is None and evaluated >= args.budget:
            stop_reason = "BUDGET_CONFIGURATIONS"

        screened.sort(key=lambda item: item[0], reverse=True)
        promote_count = max(1, int(round(PROMOTION_FRACTION * len(screened))))
        promoted = screened[:promote_count]
        record(
            {
                "record": "promotion",
                "screened_count": len(screened),
                "rejected_count": rejected,
                "evaluated_count": evaluated,
                "promoted_count": len(promoted),
                "promoted_config_ids": [config.config_id for _, config, _ in promoted],
            }
        )

        full_results = []
        for margin, config, screen in promoted:
            blocks_result = [screen]
            passed_guardrails = True
            for block in other_blocks:
                result = base._score_block(block, config, cache)
                guard = _guardrails(block, config, result, cache)
                result["guardrails"] = guard
                passed_guardrails &= bool(guard["passed"])
                blocks_result.append(result)
            margins = [item["capture_margin_percentage_points"] for item in blocks_result]
            beats = sum(1 for value in margins if value > 0.0)
            payload = {
                "record": "full_evaluation",
                "config": asdict(config),
                "blocks": blocks_result,
                "margins_percentage_points": margins,
                "minimum_margin_percentage_points": min(margins),
                "blocks_beating_slope_baseline": beats,
                "guardrails_passed_everywhere": passed_guardrails,
                "meets_success_rule": bool(
                    passed_guardrails
                    and min(margins) >= SUCCESS_MARGIN_PERCENTAGE_POINTS
                ),
                "elapsed_seconds": round(time.time() - started, 2),
            }
            full_results.append(payload)
            record(payload)

        success = [item for item in full_results if item["meets_success_rule"]]
        best_beats = max(
            (item["blocks_beating_slope_baseline"] for item in full_results), default=0
        )
        if success:
            stop_reason = "SUCCESS"
        elif evaluated >= args.budget and best_beats < 3:
            stop_reason = "FUTILITY"
        record(
            {
                "record": "search_result",
                "search_id": SEARCH_ID,
                "stop_reason": stop_reason,
                "configurations_evaluated": evaluated,
                "configurations_rejected_by_guardrail": rejected,
                "configurations_fully_evaluated": len(full_results),
                "best_screen_margin_percentage_points": (
                    None if best_margin == -np.inf else best_margin
                ),
                "best_minimum_margin_percentage_points": min(
                    (item["minimum_margin_percentage_points"] for item in full_results),
                    default=None,
                ),
                "most_blocks_beating_slope_baseline": best_beats,
                "success_configuration_ids": [
                    item["config"]["config_id"] for item in success
                ],
                "total_seconds": round(time.time() - started, 2),
            }
        )
    print(
        json.dumps(
            {"stop_reason": stop_reason, "evaluated": evaluated, "log": str(args.log)}
        )
    )


if __name__ == "__main__":
    main()
