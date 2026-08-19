"""Evaluate a release configuration against a cached development block.

This is the scoring core the sweep harness drives. It reproduces exactly what
the frozen scorer computes for the ``release_only`` ablation -- same event
rasterization, same 5% overlap capture rule, same eligible mask, same
same-area slope baseline -- and nothing else, because the search only moves
release localization.

The slope-only baseline is **pinned** to the slope curve published in
``regime-hindcast-v1``. It is deliberately not read from the candidate
configuration: a search that is allowed to move its own baseline is not being
measured against anything.
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
for _source in (REPOSITORY_ROOT / "packages" / "avycore" / "src",):
    if str(_source) not in sys.path:
        sys.path.insert(0, str(_source))

from avycore.snowpack import insolation_index  # noqa: E402
from avycore.snowpack.release_v2 import (  # noqa: E402
    ReleaseConfigV2,
    integrate_state,
    regime_scores,
    release_mask,
)

#: The v1 published slope response, frozen here as the baseline's curve so a
#: configuration cannot move the bar it is being judged against.
BASELINE_SLOPE_BREAKPOINTS_DEG = (0, 20, 25, 30, 34, 40, 45, 50, 55, 65, 90)
BASELINE_SLOPE_SCORES = (0, 0, 15, 55, 85, 100, 95, 75, 45, 15, 0)

CAPTURE_MINIMUM_OVERLAP_FRACTION = 0.05


@dataclass
class Block:
    """One cached development block: terrain, forcing, targets, eligibility."""

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
    event_required: np.ndarray
    event_scorable: np.ndarray
    eligible_flat: np.ndarray
    event_count: int

    @classmethod
    def load(cls, path: Path) -> "Block":
        with np.load(path, allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata_json"].item()))
            get = lambda name: np.asarray(archive[name])  # noqa: E731
            terrain_mask = np.logical_or.reduce(
                [
                    get(f"mask_{name}")
                    for name in (
                        "elevation",
                        "slope",
                        "aspect",
                        "general_curvature",
                        "plan_curvature",
                        "forest_mask",
                    )
                ]
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
                    "snow_depth_m": get("forcing_snow_depth_m"),
                    "shortwave_radiation_w_m2": get("forcing_shortwave_radiation_w_m2"),
                    "sample_elevation_m": get("forcing_sample_elevation_m"),
                },
                times_utc=list(metadata["forcing_times_utc"]),
                resolution_m=float(metadata["resolution_m"]),
                membership=sparse.csr_matrix((0, 0)),
                event_required=np.zeros(0, dtype="int64"),
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
        cols: list[int] = []
        required: list[int] = []
        scorable: list[bool] = []
        for event in range(int(offsets.size - 1)):
            indices = flat_indices[offsets[event] : offsets[event + 1]]
            mapped = position[indices]
            known = mapped >= 0
            rows.extend([event] * int(np.count_nonzero(known)))
            cols.extend(int(value) for value in mapped[known])
            required.append(int(np.ceil(CAPTURE_MINIMUM_OVERLAP_FRACTION * indices.size)))
            scorable.append(bool(geometry_complete[event]) and bool(known.all()))
        self.membership = sparse.csr_matrix(
            (np.ones(len(rows), dtype="int32"), (rows, cols)),
            shape=(int(offsets.size - 1), self.eligible_flat.size),
        )
        self.event_required = np.asarray(required, dtype="int64")
        self.event_scorable = np.asarray(scorable, dtype=bool)

    def capture(self, predicted: np.ndarray) -> tuple[int, int]:
        """Captured events and flagged eligible cells for a predicted footprint."""
        selection = predicted.reshape(-1)[self.eligible_flat].astype("int32")
        overlaps = self.membership @ selection
        captured = int(
            np.count_nonzero((overlaps >= self.event_required) & self.event_scorable)
        )
        return captured, int(selection.sum())

    def slope_baseline_capture(self, predicted_cell_count: int) -> int:
        """Capture of the highest-slope-score cells at the same area budget."""
        scores = np.interp(
            self.slope.reshape(-1)[self.eligible_flat],
            BASELINE_SLOPE_BREAKPOINTS_DEG,
            BASELINE_SLOPE_SCORES,
        )
        order = np.argsort(-scores, kind="stable")
        selection = np.zeros(self.eligible_flat.size, dtype="int32")
        selection[order[:predicted_cell_count]] = 1
        overlaps = self.membership @ selection
        return int(
            np.count_nonzero((overlaps >= self.event_required) & self.event_scorable)
        )


def _insolation(block: Block, cycle: dict[str, Any], config: ReleaseConfigV2) -> np.ndarray:
    start, end = cycle["antecedent_start_exclusive_utc"], cycle["end_utc"]
    selected = [
        index
        for index, stamp in enumerate(block.times_utc)
        if start < stamp <= end
    ]
    shortwave = block.forcing["shortwave_radiation_w_m2"][:, selected].mean(axis=0)
    return insolation_index(
        slope_deg=block.slope,
        aspect_deg=block.aspect,
        timestamps_utc=[block.times_utc[index] for index in selected],
        shortwave_w_m2=shortwave,
        latitude_deg=float(block.metadata["latitude_deg"]),
        longitude_deg=float(block.metadata["longitude_deg"]),
    )


def predict(block: Block, config: ReleaseConfigV2) -> tuple[np.ndarray, dict[str, Any]]:
    """Union the release footprint over every predeclared storm cycle."""
    supported = ~block.terrain_mask
    union = np.zeros(block.slope.shape, dtype=bool)
    zone_counts: dict[str, int] = {}
    for cycle in block.metadata["storm_cycles"]:
        start, end = cycle["antecedent_start_exclusive_utc"], cycle["end_utc"]
        selected = [
            index for index, stamp in enumerate(block.times_utc) if start < stamp <= end
        ]
        if not selected:
            raise ValueError(f"{block.block_id} cycle {cycle['cycle_id']} has no hours.")
        state = integrate_state(
            times_utc=[block.times_utc[index] for index in selected],
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
        scores, active, missing = regime_scores(
            slope=block.slope,
            aspect=block.aspect,
            general_curvature=block.general_curvature,
            plan_curvature=block.plan_curvature,
            forest=block.forest,
            terrain_mask=block.terrain_mask,
            state=state,
            insolation=_insolation(block, cycle, config),
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
        union |= mask
        for regime, value in counts.items():
            zone_counts[regime] = zone_counts.get(regime, 0) + value
    return union, {"zone_count_by_regime": zone_counts}


def evaluate(block: Block, config: ReleaseConfigV2) -> dict[str, Any]:
    """Score one configuration on one block against the pinned slope baseline."""
    predicted, meta = predict(block, config)
    captured, flagged = block.capture(predicted)
    baseline = block.slope_baseline_capture(flagged) if flagged else 0
    eligible_count = int(block.eligible_flat.size)
    return {
        "block_id": block.block_id,
        "event_count": block.event_count,
        "captured_event_count": captured,
        "event_capture_fraction": captured / block.event_count,
        "flagged_eligible_cell_count": flagged,
        "flagged_eligible_terrain_fraction": flagged / eligible_count,
        "slope_baseline_captured_event_count": baseline,
        "slope_baseline_event_capture_fraction": baseline / block.event_count,
        "capture_margin_percentage_points": 100.0
        * (captured - baseline)
        / block.event_count,
        "flagged_outside_eligible_cell_count": int(
            np.count_nonzero(predicted & ~block.eligible)
        ),
        "flagged_on_missing_input_cell_count": int(
            np.count_nonzero(predicted & block.terrain_mask)
        ),
        **meta,
    }


def load_blocks(cache_dir: Path, block_ids: Sequence[str] | None = None) -> list[Block]:
    paths = sorted(cache_dir.glob("*.npz"))
    blocks = [Block.load(path) for path in paths]
    if block_ids is not None:
        wanted = set(block_ids)
        blocks = [block for block in blocks if block.block_id in wanted]
    for block in blocks:
        if not block.block_id.startswith("development_"):
            raise ValueError(
                f"{block.block_id} is not a development block. The search never "
                "touches a reserved block."
            )
    return blocks
