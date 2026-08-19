"""Search release configurations on development blocks, inside one process.

Structure, and why it is this shape:

* **One process, one log.** Every configuration evaluated is appended to a JSONL
  file, including the ones that lose and the ones the guardrails reject. The
  total count is what makes the best score interpretable.
* **Screen on one block, then promote.** Every candidate is evaluated on a
  single predeclared development block. Only the top decile by capture margin
  is promoted to all five. That is roughly a five-fold saving and it is
  declared before the search rather than after.
* **Group by snow state.** The hourly integration dominates the cost and only
  depends on the wind/settlement parameters, so configurations are sorted by
  their state key and each state is integrated once per block.
* **Guardrails are vetoes, not tie-breakers.** A configuration that lights up a
  benign day, flags a missing-input cell, or exceeds the declared terrain
  budget is rejected regardless of what it captured.

Stop conditions are checked after every configuration and the first to fire
wins: SUCCESS, FUTILITY, BUDGET, PLATEAU. They are the ones predeclared in
``docs/release-engine-repair-plan.md`` and the session prompt; none of them is
weakened after a score is seen.

The reserved 1999 lattice blocks are unreachable from this script. It loads
development caches only, and ``release_search.load_blocks`` refuses anything
whose block id is not a development block.
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
for _source in (REPOSITORY_ROOT / "packages" / "avycore" / "src", Path(__file__).resolve().parent):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

import release_search as rs  # noqa: E402
from avycore.snowpack.release_v2 import (  # noqa: E402
    V1_FROZEN,
    V2_BASELINE,
    ReleaseConfigV2,
    SnowStateV2,
    guardrail_report,
    regime_scores,
    release_mask,
)

#: The block every candidate is screened on. Declared before the search.
SCREEN_BLOCK_ID = "development_western_bernese"

#: Fraction of screened candidates promoted to the full five-block evaluation.
PROMOTION_FRACTION = 0.10

#: Predeclared acceptance bar. A configuration must beat the same-area
#: slope-only baseline by at least this margin on every development block.
SUCCESS_MARGIN_PERCENTAGE_POINTS = 5.0

#: The v1 acceptance rule's terrain budget, reused unchanged.
MAXIMUM_FLAGGED_ELIGIBLE_TERRAIN_FRACTION = 0.20

CONFIGURATION_BUDGET = 200
COMPUTE_BUDGET_SECONDS = 4 * 3600
PLATEAU_LIMIT = 50

#: Saturation scale that makes each kernel comparable. A storm holding 10 m/s
#: for 24 hours over available snow gives (10 - 7.7)^3 * 24 = 292 cubic units,
#: (10 - 7.7) * 24 = 55 linear units, or 24 hours; v1's cubic reference of 2000
#: sits an order above one such burst.
KERNEL_REFERENCE = {"cubic_excess": 2000.0, "linear_excess": 100.0, "hours_above": 24.0}

STATE_KEY_FIELDS = (
    "drift_kernel",
    "drift_threshold_dry_ms",
    "drift_threshold_wet_ms",
    "drift_index_full",
    "drift_settlement_time_h",
    "new_snow_settlement_time_h",
    "drift_available_min_new_snow_cm",
)

#: Alternative slope responses. ``v1`` is the published curve. ``broad`` keeps
#: score on the 28-52 degree ground the published curve discounts steeply;
#: ``narrow`` concentrates on the 34-45 degree slab band.
SLOPE_CURVES = {
    "v1": (
        (0, 20, 25, 30, 34, 40, 45, 50, 55, 65, 90),
        (0, 0, 15, 55, 85, 100, 95, 75, 45, 15, 0),
    ),
    "broad": (
        (0, 22, 26, 30, 34, 40, 46, 52, 58, 70, 90),
        (0, 0, 40, 75, 95, 100, 100, 90, 65, 20, 0),
    ),
    "narrow": (
        (0, 26, 30, 34, 38, 42, 45, 50, 55, 65, 90),
        (0, 0, 10, 60, 100, 100, 85, 45, 20, 5, 0),
    ),
}


def _state_key(config: ReleaseConfigV2) -> tuple:
    return tuple(getattr(config, name) for name in STATE_KEY_FIELDS)


def candidate_configurations(seed: int, count: int) -> list[ReleaseConfigV2]:
    """Anchors first, then a seeded quasi-random sample of the declared space.

    The anchors make the log self-explaining: the frozen engine and the
    morphology-only repair are evaluated under exactly the same metric as every
    tuned candidate, so the reader can see what the tuning bought.
    """
    configs: list[ReleaseConfigV2] = [
        replace(V1_FROZEN, config_id="anchor_v1_frozen"),
        replace(V2_BASELINE, config_id="anchor_v2_baseline"),
    ]
    # A pure threshold ladder on the repaired morphology: the single most
    # informative one-dimensional slice, and the plan asks for the threshold to
    # be re-derived rather than left at 55.
    for threshold in (25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 60.0):
        configs.append(
            replace(
                V2_BASELINE,
                config_id=f"ladder_threshold_{threshold:g}",
                release_threshold=threshold,
                threshold_derivation="declared_ladder_point",
            )
        )

    rng = np.random.default_rng(seed)
    kernels = list(KERNEL_REFERENCE)
    while len(configs) < count:
        kernel = kernels[int(rng.integers(len(kernels)))]
        scale = float(rng.choice([0.1, 0.25, 0.5, 1.0, 4.0]))
        curve_name = str(rng.choice(list(SLOPE_CURVES)))
        breakpoints, scores = SLOPE_CURVES[curve_name]
        index = len(configs)
        configs.append(
            ReleaseConfigV2(
                config_id=f"sample_{index:04d}",
                drift_kernel=kernel,
                drift_index_full=KERNEL_REFERENCE[kernel] * scale,
                drift_threshold_dry_ms=float(rng.choice([6.5, 7.7, 9.0])),
                drift_threshold_wet_ms=float(rng.choice([9.0, 9.9, 11.0])),
                drift_settlement_time_h=float(rng.choice([24.0, 48.0, 96.0])),
                new_snow_settlement_time_h=float(rng.choice([24.0, 48.0, 96.0])),
                release_threshold=float(rng.choice([25.0, 30.0, 35.0, 40.0, 45.0, 50.0, 55.0])),
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
                threshold_derivation=f"seeded_sample:{curve_name}",
            )
        )
    return configs[:count]


class StateCache:
    """Snow states for one state key, across the blocks that need them."""

    def __init__(self) -> None:
        self._key: tuple | None = None
        self._states: dict[str, list[SnowStateV2]] = {}
        self._insolation: dict[str, list[np.ndarray]] = {}

    def insolation(self, block: rs.Block) -> list[np.ndarray]:
        if block.block_id not in self._insolation:
            self._insolation[block.block_id] = [
                rs._insolation(block, cycle, V1_FROZEN)
                for cycle in block.metadata["storm_cycles"]
            ]
        return self._insolation[block.block_id]

    def states(self, block: rs.Block, config: ReleaseConfigV2) -> list[SnowStateV2]:
        key = _state_key(config)
        if key != self._key:
            self._key = key
            self._states = {}
        if block.block_id not in self._states:
            self._states[block.block_id] = _integrate_cycles(block, config)
        return self._states[block.block_id]


def _integrate_cycles(block: rs.Block, config: ReleaseConfigV2) -> list[SnowStateV2]:
    from avycore.snowpack.release_v2 import integrate_state

    supported = ~block.terrain_mask
    states = []
    for cycle in block.metadata["storm_cycles"]:
        start, end = cycle["antecedent_start_exclusive_utc"], cycle["end_utc"]
        selected = [i for i, t in enumerate(block.times_utc) if start < t <= end]
        states.append(
            integrate_state(
                times_utc=[block.times_utc[i] for i in selected],
                storm_start_exclusive_utc=cycle["start_utc"],
                air_temperature_c=block.forcing["air_temperature_c"][:, selected],
                precipitation_mm=block.forcing["precipitation_mm"][:, selected],
                wind_speed_10m_kmh=block.forcing["wind_speed_10m_kmh"][:, selected],
                wind_from_direction_deg=block.forcing["wind_from_direction_deg"][:, selected],
                snow_depth_m=block.forcing["snow_depth_m"][:, selected],
                sample_elevation_m=block.forcing["sample_elevation_m"],
                sample_index=block.sample_index,
                elevation_m=block.elevation,
                supported=supported,
                config=config,
            )
        )
    return states


def _mask_from_states(
    block: rs.Block,
    states: list[SnowStateV2],
    insolations: list[np.ndarray],
    config: ReleaseConfigV2,
) -> np.ndarray:
    union = np.zeros(block.slope.shape, dtype=bool)
    for state, insolation in zip(states, insolations):
        scores, active, missing = regime_scores(
            slope=block.slope,
            aspect=block.aspect,
            general_curvature=block.general_curvature,
            plan_curvature=block.plan_curvature,
            forest=block.forest,
            terrain_mask=block.terrain_mask,
            state=state,
            insolation=insolation,
            config=config,
        )
        mask, _ = release_mask(
            scores=scores,
            active=active,
            missing=missing,
            slope=block.slope,
            aspect=block.aspect,
            elevation=block.elevation,
            forest=block.forest,
            resolution_m=block.resolution_m,
            config=config,
        )
        union |= mask
    return union


def _benign_state(block: rs.Block) -> SnowStateV2:
    """No new snow, no drift, no rain, no melt, cold -- on this block's terrain."""
    shape = block.slope.shape
    zero = np.zeros(shape, dtype="float32")
    return SnowStateV2(
        new_snow_index_cm=zero.copy(),
        drift_index_normalized=zero.copy(),
        drift_from_direction_deg=np.full(shape, -1.0, dtype="float32"),
        rain_on_snow_mm=zero.copy(),
        positive_degree_hours=zero.copy(),
        antecedent_snow_depth_m=zero.copy(),
        peak_temperature_c=np.full(shape, -8.0, dtype="float32"),
        mean_storm_temperature_c=np.full(shape, -8.0, dtype="float32"),
        buried_weak_interface_proxy=zero.copy(),
        mask=block.terrain_mask.copy(),
    )


def _score_block(
    block: rs.Block, config: ReleaseConfigV2, cache: StateCache
) -> dict[str, Any]:
    predicted = _mask_from_states(
        block, cache.states(block, config), cache.insolation(block), config
    )
    captured, flagged = block.capture(predicted)
    baseline = block.slope_baseline_capture(flagged) if flagged else 0
    return {
        "block_id": block.block_id,
        "event_count": block.event_count,
        "captured_event_count": captured,
        "event_capture_fraction": captured / block.event_count,
        "slope_baseline_captured_event_count": baseline,
        "slope_baseline_event_capture_fraction": baseline / block.event_count,
        "capture_margin_percentage_points": 100.0 * (captured - baseline) / block.event_count,
        "flagged_eligible_cell_count": flagged,
        "flagged_eligible_terrain_fraction": flagged / block.eligible_flat.size,
        "flagged_outside_eligible_cell_count": int(
            np.count_nonzero(predicted & ~block.eligible)
        ),
        "flagged_on_missing_input_cell_count": int(
            np.count_nonzero(predicted & block.terrain_mask)
        ),
    }


def _guardrails(
    block: rs.Block, config: ReleaseConfigV2, screen: dict[str, Any], cache: StateCache
) -> dict[str, Any]:
    benign = _mask_from_states(
        block, [_benign_state(block)], [cache.insolation(block)[0]], config
    )
    report = guardrail_report(
        benign_release_cell_count=int(np.count_nonzero(benign)),
        benign_eligible_cell_count=int(block.eligible_flat.size),
        flagged_outside_eligible_cell_count=screen["flagged_outside_eligible_cell_count"],
        flagged_on_missing_input_cell_count=screen["flagged_on_missing_input_cell_count"],
    )
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
    parser.add_argument("--seed", type=int, default=20260818)
    parser.add_argument("--budget", type=int, default=CONFIGURATION_BUDGET)
    args = parser.parse_args()

    blocks = {block.block_id: block for block in rs.load_blocks(args.cache_dir)}
    if SCREEN_BLOCK_ID not in blocks:
        raise SystemExit(f"Screening block {SCREEN_BLOCK_ID} is not cached.")
    screen_block = blocks[SCREEN_BLOCK_ID]
    other_blocks = [b for name, b in sorted(blocks.items()) if name != SCREEN_BLOCK_ID]

    configs = candidate_configurations(args.seed, args.budget)
    # Group by state key so each hourly integration happens once.
    configs.sort(key=lambda item: (_state_key(item), item.config_id))

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
                "screen_block_id": SCREEN_BLOCK_ID,
                "promotion_fraction": PROMOTION_FRACTION,
                "success_margin_percentage_points": SUCCESS_MARGIN_PERCENTAGE_POINTS,
                "maximum_flagged_eligible_terrain_fraction": MAXIMUM_FLAGGED_ELIGIBLE_TERRAIN_FRACTION,
                "configuration_budget": args.budget,
                "compute_budget_seconds": COMPUTE_BUDGET_SECONDS,
                "plateau_limit": PLATEAU_LIMIT,
                "seed": args.seed,
                "baseline_slope_curve": "regime-hindcast-v1 published slope response, pinned",
                "blocks": sorted(blocks),
            }
        )

        for config in configs:
            if time.time() - started > COMPUTE_BUDGET_SECONDS:
                stop_reason = "BUDGET_COMPUTE_SECONDS"
                break
            evaluated += 1
            screen = _score_block(screen_block, config, cache)
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
            record(entry)
            screened.append((margin, config, screen))
            if margin > best_margin:
                best_margin, plateau = margin, 0
            else:
                plateau += 1
                if plateau >= PLATEAU_LIMIT:
                    stop_reason = "PLATEAU"
                    break

        if stop_reason is None and evaluated >= args.budget:
            stop_reason = "BUDGET_CONFIGURATIONS"

        # Promote the top decile of guardrail-passing candidates.
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
                result = _score_block(block, config, cache)
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
        best_beats = max((item["blocks_beating_slope_baseline"] for item in full_results), default=0)
        if success:
            stop_reason = "SUCCESS"
        elif evaluated >= args.budget and best_beats < 3:
            stop_reason = "FUTILITY"
        record(
            {
                "record": "search_result",
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
    print(json.dumps({"stop_reason": stop_reason, "evaluated": evaluated, "log": str(args.log)}))


if __name__ == "__main__":
    main()
