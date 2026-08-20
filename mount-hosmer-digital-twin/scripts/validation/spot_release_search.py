"""Score a release configuration on a cached SPOT block under hourly forcing.

This is the SPOT counterpart of :mod:`release_search`, which scores the CERRA
``regime-hindcast-v1`` development blocks. It exists separately rather than as a
flag on that module because three things genuinely differ and pretending they
do not would corrupt a comparison:

1. **The capture rule.** ``spot-blind-swiss-v1`` declares a 10% minimum overlap;
   ``regime-hindcast-v1`` declares 5%. Both are computed here, always, and both
   are reported. The 10% rule is the one the frozen SPOT result used and is the
   primary number; the 5% rule is reported beside it so the configuration
   search's margins remain comparable. Neither is chosen after seeing a score.
2. **The forcing.** ERA5 at 0.25 degrees, five variables. No ``snow_depth`` and
   no ``shortwave_radiation``, so ``snow_depth_m`` and ``insolation`` are passed
   as ``None`` -- absent, not zero.
3. **The window.** One storm window per block, not a list of storm cycles, and
   the payload carries only seven antecedent hours.

Everything else is deliberately identical to :mod:`release_search`: the same
pinned slope response, the same same-area budget rule, the same sparse event
membership, and the same refusal to let a candidate move its own baseline.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from scipy import sparse

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for _source in (
    REPOSITORY_ROOT / "packages" / "avycore" / "src",
    Path(__file__).resolve().parent,
):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from release_search import (  # noqa: E402
    BASELINE_SLOPE_BREAKPOINTS_DEG,
    BASELINE_SLOPE_SCORES,
)
from avycore.snowpack.regimes import DRY_SLAB  # noqa: E402
from avycore.snowpack.release_v2 import (  # noqa: E402
    ReleaseConfigV2,
    integrate_state,
    regime_scores,
    release_mask,
    required_capability,
)

#: Both rules are computed for every configuration and both are reported.
#: ``spot`` is what ``spot-blind-swiss-v1`` froze; ``regime`` is what the
#: release configuration search used. Declaring both up front is what stops a
#: later choice between them from being a choice of the more flattering number.
CAPTURE_RULES = {"spot_10_percent": 0.10, "regime_5_percent": 0.05}

#: The primary rule for a SPOT block is the rule SPOT froze.
PRIMARY_CAPTURE_RULE = "spot_10_percent"

TERRAIN_LAYERS = (
    "elevation",
    "slope",
    "aspect",
    "general_curvature",
    "plan_curvature",
    "forest_mask",
)


@dataclass
class SpotBlock:
    """One cached SPOT block: terrain, hourly ERA5 forcing, targets, eligibility."""

    block_id: str
    metadata: dict[str, Any]
    slope: np.ndarray
    aspect: np.ndarray
    general_curvature: np.ndarray
    plan_curvature: np.ndarray
    forest: np.ndarray
    elevation: np.ndarray
    terrain_mask: np.ndarray
    eligible: np.ndarray
    sample_index: np.ndarray
    forcing: dict[str, np.ndarray]
    times_utc: list[str]
    resolution_m: float
    membership: sparse.csr_matrix
    event_cell_counts: np.ndarray
    event_required: dict[str, np.ndarray]
    event_scorable: np.ndarray
    eligible_flat: np.ndarray
    event_count: int

    @classmethod
    def load(cls, path: Path) -> "SpotBlock":
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata_json"].item()))
            get = lambda name: np.asarray(archive[name])  # noqa: E731
            terrain_mask = np.logical_or.reduce(
                [get(f"mask_{name}") for name in TERRAIN_LAYERS]
            )
            eligible = get("eligible")
            flat_indices = get("event_flat_indices")
            offsets = get("event_offsets")
            geometry_complete = get("event_geometry_complete")
            block = cls(
                block_id=metadata["block_id"],
                metadata=metadata,
                slope=get("layer_slope").astype("float64"),
                aspect=get("layer_aspect").astype("float64"),
                general_curvature=get("layer_general_curvature").astype("float64"),
                plan_curvature=get("layer_plan_curvature").astype("float64"),
                forest=get("layer_forest_mask").astype("float64"),
                elevation=get("layer_elevation").astype("float64"),
                terrain_mask=terrain_mask,
                eligible=eligible,
                sample_index=get("sample_index").astype(np.intp),
                forcing={
                    "air_temperature_c": get("forcing_air_temperature_c"),
                    "precipitation_mm": get("forcing_precipitation_mm"),
                    "wind_speed_10m_kmh": get("forcing_wind_speed_10m_kmh"),
                    "wind_from_direction_deg": get("forcing_wind_from_direction_deg"),
                    "sample_elevation_m": get("forcing_sample_elevation_m"),
                    "diagnostic_snowfall_cm": get("diagnostic_snowfall_cm"),
                },
                times_utc=list(metadata["forcing_times_utc"]),
                resolution_m=float(metadata["resolution_m"]),
                membership=sparse.csr_matrix((0, 0)),
                event_cell_counts=np.zeros(0, dtype="int64"),
                event_required={},
                event_scorable=np.zeros(0, dtype=bool),
                eligible_flat=np.flatnonzero(eligible.reshape(-1)),
                event_count=int(offsets.size - 1),
            )
        block._build_targets(flat_indices, offsets, geometry_complete)
        return block

    def _build_targets(
        self,
        flat_indices: np.ndarray,
        offsets: np.ndarray,
        geometry_complete: np.ndarray,
    ) -> None:
        """Compress the event rasters into one sparse (events x eligible) matrix.

        An event cell outside the eligible mask means the event overlaps
        incomplete model inputs. The frozen scorer forces such an event
        uncaptured rather than dropping it, so it stays in the denominator with
        ``scorable`` false.
        """
        position = np.full(self.eligible.size, -1, dtype="int64")
        position[self.eligible_flat] = np.arange(self.eligible_flat.size, dtype="int64")
        rows: list[int] = []
        columns: list[int] = []
        cell_counts: list[int] = []
        scorable: list[bool] = []
        for event in range(int(offsets.size - 1)):
            indices = flat_indices[offsets[event] : offsets[event + 1]]
            mapped = position[indices]
            known = mapped >= 0
            rows.extend([event] * int(np.count_nonzero(known)))
            columns.extend(int(value) for value in mapped[known])
            cell_counts.append(int(indices.size))
            scorable.append(bool(geometry_complete[event]) and bool(known.all()))
        self.membership = sparse.csr_matrix(
            (np.ones(len(rows), dtype="int32"), (rows, columns)),
            shape=(int(offsets.size - 1), self.eligible_flat.size),
        )
        self.event_cell_counts = np.asarray(cell_counts, dtype="int64")
        self.event_required = {
            name: np.ceil(fraction * self.event_cell_counts).astype("int64")
            for name, fraction in CAPTURE_RULES.items()
        }
        self.event_scorable = np.asarray(scorable, dtype=bool)

    def captured(self, selection: np.ndarray, rule: str) -> int:
        overlaps = self.membership @ selection.astype("int32")
        return int(
            np.count_nonzero((overlaps >= self.event_required[rule]) & self.event_scorable)
        )

    def slope_baseline_selection(self, cell_budget: int) -> np.ndarray:
        """The highest-slope-score eligible cells at exactly this area budget."""
        scores = np.interp(
            self.slope.reshape(-1)[self.eligible_flat],
            BASELINE_SLOPE_BREAKPOINTS_DEG,
            BASELINE_SLOPE_SCORES,
        )
        order = np.argsort(-scores, kind="stable")
        selection = np.zeros(self.eligible_flat.size, dtype=bool)
        selection[order[:cell_budget]] = True
        return selection


def predict(
    block: SpotBlock, config: ReleaseConfigV2
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, Any]]:
    """Release footprint for the block's single frozen storm window.

    Returns the union, the per-regime footprints, and diagnostics. The
    per-regime split is not decoration: ``spot-blind-swiss-v1`` ran
    ``avycore.hazard.risk``, which has **one** release score, while this engine
    has four regimes. Comparing a four-regime union against a one-regime frozen
    mask would credit the repair with zones that come from a pathway the frozen
    engine never had. The dry-slab footprint is the like-for-like one, and it is
    also the only pathway any of the three documented defects touches.

    ``snow_depth_m`` and ``insolation`` are ``None`` because the frozen ERA5
    request does not contain them. Substituting zero would tell the wet-snow
    pathway that the ground is bare and tell the melt term that the sun never
    shone, both of which are claims the data does not make.
    """
    window = block.metadata["storm_window"]
    state = integrate_state(
        times_utc=block.times_utc,
        storm_start_exclusive_utc=str(window["start_utc"]),
        air_temperature_c=block.forcing["air_temperature_c"],
        precipitation_mm=block.forcing["precipitation_mm"],
        wind_speed_10m_kmh=block.forcing["wind_speed_10m_kmh"],
        wind_from_direction_deg=block.forcing["wind_from_direction_deg"],
        snow_depth_m=None,
        sample_elevation_m=block.forcing["sample_elevation_m"],
        sample_index=block.sample_index,
        elevation_m=block.elevation,
        supported=~block.terrain_mask,
        config=config,
    )
    scores, active, missing = regime_scores(
        slope=block.slope,
        aspect=block.aspect,
        general_curvature=block.general_curvature,
        plan_curvature=block.plan_curvature,
        forest=block.forest,
        terrain_mask=block.terrain_mask,
        state=state,
        insolation=None,
        config=config,
    )
    mask, counts = release_mask(
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
    # One regime at a time, through the identical extraction. Their union is
    # not the combined mask in general -- the per-regime zone cap and the
    # minimum-area filter both act on whatever reaches them -- so each is
    # computed rather than inferred.
    per_regime: dict[str, np.ndarray] = {}
    for regime in scores:
        single_active = {name: np.zeros_like(value) for name, value in active.items()}
        single_active[regime] = active[regime]
        regime_mask, _ = release_mask(
            scores=scores,
            active=single_active,
            missing=missing,
            slope=block.slope,
            aspect=block.aspect,
            elevation=block.elevation,
            forest=block.forest,
            resolution_m=block.resolution_m,
            config=config,
        )
        per_regime[regime] = regime_mask
    valid = ~block.terrain_mask
    # Defect 2, evaluated against this configuration and this block's own
    # hourly state rather than against the frozen scalar. With transport zero
    # the dry-slab loading term is fixed by new snow alone, so the terrain
    # capability a cell must reach is a pure function of the storm -- and
    # capability is a product of factors each bounded by 1, so a requirement
    # above 1.0 is unreachable by any terrain whatsoever.
    peak_new_snow = float(state.new_snow_index_cm[valid].max())
    needed = required_capability(peak_new_snow, transport=0.0, config=config)
    return mask, per_regime, {
        "dry_slab_saturation": {
            "peak_new_snow_index_cm_best_cell": peak_new_snow,
            "observed_transport_term": float(state.drift_index_normalized[valid].max()),
            "required_terrain_capability_at_best_cell": needed,
            "capability_is_reachable": bool(needed <= 1.0),
        },
        "zone_count_by_regime": counts,
        "zone_count_total": int(sum(counts.values())),
        "state_diagnostics": {
            "peak_new_snow_index_cm_maximum": float(state.new_snow_index_cm[valid].max()),
            "peak_new_snow_index_cm_mean": float(state.new_snow_index_cm[valid].mean()),
            "drift_index_normalized_maximum": float(
                state.drift_index_normalized[valid].max()
            ),
            "drift_index_normalized_mean": float(
                state.drift_index_normalized[valid].mean()
            ),
            "buried_weak_interface_proxy_maximum": float(
                state.buried_weak_interface_proxy[valid].max()
            ),
            "storm_hour_count": int(state.metadata["hour_count_storm"]),
            "antecedent_hour_count": int(
                state.metadata["hour_count_total"] - state.metadata["hour_count_storm"]
            ),
        },
    }


def metrics_for_mask(block: SpotBlock, predicted: np.ndarray) -> dict[str, Any]:
    """Capture, coverage and budget for a footprint, beside the slope baseline.

    Structurally the same function ``freeze_release_config_search._metrics``
    applies to the CERRA blocks, extended to report every declared capture rule
    rather than one.
    """
    flat = block.eligible_flat
    selection = predicted.reshape(-1)[flat].astype(bool)
    flagged = int(selection.sum())
    mapped = np.asarray(block.membership.sum(axis=0)).ravel() > 0
    mapped_count = int(mapped.sum())
    baseline = block.slope_baseline_selection(flagged)
    slope_values = block.slope.reshape(-1)[flat]

    capture: dict[str, Any] = {}
    for rule in CAPTURE_RULES:
        captured = block.captured(selection, rule)
        baseline_captured = block.captured(baseline, rule)
        capture[rule] = {
            "minimum_overlap_fraction": CAPTURE_RULES[rule],
            "captured_event_count": captured,
            "event_capture_fraction": captured / block.event_count,
            "slope_baseline_captured_event_count": baseline_captured,
            "slope_baseline_event_capture_fraction": baseline_captured
            / block.event_count,
            "capture_margin_percentage_points": 100.0
            * (captured - baseline_captured)
            / block.event_count,
        }

    return {
        "block_id": block.block_id,
        "event_count": block.event_count,
        "scorable_event_count": int(block.event_scorable.sum()),
        "capture_by_rule": capture,
        "primary_capture_rule": PRIMARY_CAPTURE_RULE,
        "capture_margin_percentage_points": capture[PRIMARY_CAPTURE_RULE][
            "capture_margin_percentage_points"
        ],
        "mapped_positive_footprint_coverage_fraction": (
            float((selection & mapped).sum() / mapped_count) if mapped_count else None
        ),
        "flagged_eligible_cell_count": flagged,
        "flagged_eligible_terrain_fraction": flagged / flat.size,
        "mean_slope_of_flagged_terrain_deg": (
            float(slope_values[selection].mean()) if flagged else None
        ),
        "slope_only_same_area_budget": {
            "mapped_positive_footprint_coverage_fraction": (
                float((baseline & mapped).sum() / mapped_count) if mapped_count else None
            ),
            "mean_slope_of_flagged_terrain_deg": (
                float(slope_values[baseline].mean()) if flagged else None
            ),
        },
        "spatial_agreement_with_slope_baseline_fraction": (
            float((selection & baseline).sum() / flagged) if flagged else None
        ),
        "flagged_outside_eligible_cell_count": int(
            np.count_nonzero(predicted & ~block.eligible)
        ),
        "flagged_on_missing_input_cell_count": int(
            np.count_nonzero(predicted & block.terrain_mask)
        ),
    }


def evaluate(block: SpotBlock, config: ReleaseConfigV2) -> dict[str, Any]:
    """Score one configuration on one block against the pinned slope baseline."""
    predicted, per_regime, diagnostics = predict(block, config)
    return {
        **metrics_for_mask(block, predicted),
        **diagnostics,
        "regime_footprints": {
            regime: {
                "flagged_eligible_cell_count": int(
                    mask.reshape(-1)[block.eligible_flat].sum()
                ),
                **(
                    {"metrics": metrics_for_mask(block, mask)}
                    if regime == DRY_SLAB
                    else {}
                ),
            }
            for regime, mask in per_regime.items()
        },
        "dry_slab_is_the_like_for_like_pathway": (
            "spot-blind-swiss-v1 ran a single-regime release score. Only the "
            "dry-slab footprint is comparable to its frozen mask, and only the "
            "dry-slab pathway is touched by the three documented defects."
        ),
    }


def load_blocks(cache_dir: Path, block_ids: Sequence[str] | None = None) -> list[SpotBlock]:
    """Load cached SPOT blocks.

    There is no development/reserved guard here of the kind
    :func:`release_search.load_blocks` enforces, because no SPOT block is
    reserved: all five were scored and viewed in the frozen experiment. The
    reserved 1999 lattice blocks belong to ``regime-hindcast-v1`` and are not
    reachable from this cache at all.
    """
    paths = sorted(cache_dir.glob("*.npz"))
    blocks = [SpotBlock.load(path) for path in paths]
    if block_ids is not None:
        wanted = set(block_ids)
        blocks = [block for block in blocks if block.block_id in wanted]
    return blocks
