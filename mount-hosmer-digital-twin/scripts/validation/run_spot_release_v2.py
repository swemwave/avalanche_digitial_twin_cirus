"""Score the repaired release engine on the SPOT blocks under hourly forcing.

What this answers, and why nobody had answered it
-------------------------------------------------
``spot-blind-swiss-v1`` produced 0/40/0/0 release zones across Gotthard,
Glarus, Albula and Silvretta. ``docs/release-engine-repair-plan.md`` attributes
that to three defects, the first of which is that
``run_spot_blind_hindcast.py:417`` hands the model
``mean(72 hours x 9 points)`` as a wind speed. The repair landed in
``avycore.snowpack.release_v2``, and the 128-configuration search that followed
ran on ``regime-hindcast-v1``'s **CERRA** forcing, which never scalarizes wind.
So the zero-zone condition itself was never re-tested against the forcing that
produced it. This script does that.

Everything here is a **development** number
-------------------------------------------
All five SPOT blocks were predicted, scored and reported in the frozen
experiment, and their outlines were opened. Re-scoring them after a model change
is development by construction. The block ids keep their ``holdout_`` prefixes
because that is their identity in the frozen spec, not because anything here is
a holdout result. No reserved ``regime-hindcast-v1`` lattice block is reachable
from this script.

What is declared before any score is computed
---------------------------------------------
* The configuration set: three, fixed, listed in :data:`CONFIGURATIONS`. No
  search, no sampling, no seed. Two of them are anchors that isolate one repair
  each; the third is the best configuration the completed search produced, used
  exactly as frozen in ``release-config-search-v1.json``.
* The metric: release-only event capture against a same-area slope-only
  baseline on the pinned ``regime-hindcast-v1`` slope response, reported under
  **both** declared capture rules -- SPOT's frozen 10% and the search's 5% --
  for every configuration and every block. Reporting both is what stops a later
  choice between them from being a choice of the flattering one.
* The reference row: the frozen v1 release mask, read from the committed
  prediction artifact and scored through the same metric, so the comparison is
  against a number this script computed the same way rather than against a
  differently-defined number quoted from elsewhere.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
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

import spot_release_search as srs  # noqa: E402
from avycore.snowpack.release_v2 import (  # noqa: E402
    DRIFT_KERNELS,
    V1_FROZEN,
    V2_BASELINE,
    ReleaseConfigV2,
    required_capability,
    storm_window_wind_statistic,
)

EXPERIMENT_ID = "release-v2-spot-forcing-v1"
SEARCH_RESULT_PATH = (
    REPOSITORY_ROOT / "validation-data/results/release-config-search-v1.json"
)
FROZEN_PREDICTION_DIR = (
    REPOSITORY_ROOT / "validation-data/predictions/spot-blind-swiss-v1"
)
FROZEN_HOLDOUT_RESULT = (
    REPOSITORY_ROOT / "validation-data/results/spot-blind-swiss-v1-holdout.json"
)
FROZEN_DEVELOPMENT_RESULT = (
    REPOSITORY_ROOT / "validation-data/results/spot-blind-swiss-v1-development.json"
)

#: The frozen v1 transport floor, in ``avycore.hazard.risk``. Quoted here only
#: as a number to compare an hourly wind against; nothing imports or edits it.
V1_WIND_TRANSPORT_MIN_KMH = 15.0


def _best_searched_configuration() -> ReleaseConfigV2:
    """The winning configuration of ``release-config-search-v1``, unchanged.

    Rebuilt from the committed sweep log rather than retyped, so it cannot drift
    from the configuration that actually produced the published margins.
    """
    result = json.loads(SEARCH_RESULT_PATH.read_text(encoding="utf-8"))
    best = result["evaluations"][0]["config_id"]
    log = SEARCH_RESULT_PATH.with_name(
        SEARCH_RESULT_PATH.stem + "-sweep-log.jsonl"
    )
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("record") == "full_evaluation" and entry["config"]["config_id"] == best:
            config = dict(entry["config"])
            for key in ("slope_breakpoints_deg", "slope_scores"):
                config[key] = tuple(config[key])
            return ReleaseConfigV2(**config)
    raise ValueError(f"{best!r} is not in the committed sweep log.")


#: Declared before scoring. Each row isolates one thing.
def configurations() -> list[tuple[str, str, ReleaseConfigV2]]:
    return [
        (
            "v1_config_hourly_forcing",
            "The frozen v1 parameters, fed the hourly (sample, hour) ERA5 field "
            "instead of the scalar. Isolates the forcing repair alone.",
            replace(V1_FROZEN, config_id="v1_config_hourly_forcing"),
        ),
        (
            "v2_baseline_hourly_forcing",
            "v1 parameters with the morphology told truthfully -- no 3x3 opening, "
            "explicit closing-radius rounding. Isolates defect 3 on top of the "
            "forcing repair.",
            replace(V2_BASELINE, config_id="v2_baseline_hourly_forcing"),
        ),
        (
            "search_best_configuration",
            "The best configuration of the completed 128-configuration search, "
            "byte-for-byte as frozen in release-config-search-v1.json. It was "
            "selected on CERRA development blocks and is applied here unchanged; "
            "nothing was re-tuned for SPOT forcing.",
            replace(_best_searched_configuration(), config_id="search_best_configuration"),
        ),
    ]


def _wind_diagnostic(block: srs.SpotBlock) -> dict[str, Any]:
    """Every offered wind statistic against every transport threshold.

    The plan's defect 1 is that a mean over 72 hours and 9 points dilutes the
    windy hours below the transport threshold. Whether a better statistic
    recovers anything is a property of the data, not of the statistic, so it is
    measured rather than assumed.
    """
    window = block.metadata["storm_window"]
    hours = [
        index
        for index, stamp in enumerate(block.times_utc)
        if str(window["start_utc"]) < stamp <= str(window["end_utc"])
    ]
    speeds = block.forcing["wind_speed_10m_kmh"][:, hours]
    statistics = {
        name: storm_window_wind_statistic(speeds, statistic=name)
        for name in (
            "arithmetic_mean",
            "quantile",
            "transporting_hours_mean",
            "drift_weighted_mean",
        )
    }
    dry_threshold_kmh = V1_FROZEN.drift_threshold_dry_ms * 3.6
    return {
        "hourly_field_shape_samples_hours": list(speeds.shape),
        "statistics_kmh": statistics,
        "maximum_single_hour_kmh": float(speeds.max()),
        "hours_at_or_above_v2_dry_transport_threshold": int(
            np.count_nonzero(speeds >= dry_threshold_kmh)
        ),
        "hours_at_or_above_v1_wind_transport_minimum": int(
            np.count_nonzero(speeds >= V1_WIND_TRANSPORT_MIN_KMH)
        ),
        "v2_dry_transport_threshold_kmh": dry_threshold_kmh,
        "v1_wind_transport_minimum_kmh": V1_WIND_TRANSPORT_MIN_KMH,
        "every_statistic_below_every_threshold": bool(
            max(statistics.values()) < min(dry_threshold_kmh, V1_WIND_TRANSPORT_MIN_KMH)
            and float(speeds.max()) < min(dry_threshold_kmh, V1_WIND_TRANSPORT_MIN_KMH)
        ),
    }


def _saturation_diagnostic(block: srs.SpotBlock) -> dict[str, Any]:
    """Defect 2 recomputed for this block's own frozen scalar new snow."""
    new_snow = float(
        block.metadata["frozen_scalar_diagnostics"]["new_snow_cm_frozen_scalar"]
    )
    return {
        "frozen_scalar_new_snow_cm": new_snow,
        "required_terrain_capability_at_zero_transport": required_capability(
            new_snow, transport=0.0
        ),
        "capability_is_reachable": bool(
            required_capability(new_snow, transport=0.0) <= 1.0
        ),
        "note": (
            "Terrain capability is a product of factors each bounded by 1, so a "
            "requirement above 1.0 is unreachable by any terrain."
        ),
    }


def _frozen_v1_reference(block: srs.SpotBlock) -> dict[str, Any]:
    """Score the committed v1 release mask through this script's own metric.

    Read-only. The frozen artifact reports its slope baseline at the
    end-to-end budget; recomputing the release-only baseline here makes the
    anchor comparable to every other row instead of only approximately so.
    """
    path = FROZEN_PREDICTION_DIR / f"{block.block_id}.npz"
    with np.load(path, allow_pickle=False) as archive:
        release = np.asarray(archive["release"], dtype=bool)
        eligible = np.asarray(archive["eligible"], dtype=bool)
        metadata = json.loads(str(archive["metadata_json"].item()))
    if not np.array_equal(eligible, block.eligible):
        raise ValueError(
            f"{block.block_id}: the rebuilt eligible mask differs from the frozen "
            "prediction. The terrain path is not reproducing the frozen "
            "experiment; do not report a comparison built on it."
        )
    return {
        **srs.metrics_for_mask(block, release),
        "source": "frozen spot-blind-swiss-v1 prediction artifact, read only",
        "prediction_artifact_sha256": _sha256_file(path),
        "frozen_zone_count": int(metadata["release"]["zone_count"]),
        "frozen_conditions": {
            "new_snow_cm": metadata["conditions"]["new_snow_cm"],
            "wind_speed_kmh": metadata["conditions"]["wind_speed_kmh"],
            "wind_direction_deg": metadata["conditions"]["wind_direction_deg"],
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _software_environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "libraries": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "scipy", "rasterio", "pyproj", "shapely", "geopandas")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()

    blocks = srs.load_blocks(args.cache_dir.resolve())
    if not blocks:
        raise SystemExit(f"No cached SPOT blocks in {args.cache_dir}.")

    declared = configurations()
    per_block_context = {
        block.block_id: {
            "block_id": block.block_id,
            "campaign_year": block.metadata["campaign_year"],
            "mountain_group": block.metadata["mountain_group"],
            "source_partition": block.metadata["source_partition"],
            "event_count": block.event_count,
            "scorable_event_count": int(block.event_scorable.sum()),
            "eligible_cell_count": int(block.eligible_flat.size),
            "storm_window": block.metadata["storm_window"],
            "antecedent_hour_count": block.metadata["antecedent_hour_count"],
            "frozen_scalar_diagnostics": block.metadata["frozen_scalar_diagnostics"],
            "wind": _wind_diagnostic(block),
            "saturation": _saturation_diagnostic(block),
            "sample_geometry_elevation_agreement_rms_m": block.metadata[
                "sample_geometry"
            ]["elevation_agreement_rms_m"],
            "terrain_artifact_sha256": block.metadata["terrain_artifact_sha256"],
        }
        for block in blocks
    }

    evaluations: list[dict[str, Any]] = [
        {
            "configuration_id": "frozen_v1_reference",
            "description": (
                "The committed spot-blind-swiss-v1 release mask, re-scored "
                "through this script's metric. Not a model run."
            ),
            "parameters": None,
            "blocks": [_frozen_v1_reference(block) for block in blocks],
        }
    ]
    for config_id, description, config in declared:
        evaluations.append(
            {
                "configuration_id": config_id,
                "description": description,
                "parameters": config.manifest(resolution_m=blocks[0].resolution_m),
                "dataclass": asdict(config),
                "blocks": [srs.evaluate(block, config) for block in blocks],
            }
        )

    for evaluation in evaluations:
        margins = [
            item["capture_margin_percentage_points"] for item in evaluation["blocks"]
        ]
        zero_zone = [
            item["block_id"]
            for item in evaluation["blocks"]
            if item["flagged_eligible_cell_count"] == 0
        ]
        evaluation["summary"] = {
            "minimum_capture_margin_percentage_points": min(margins),
            "blocks_beating_slope_baseline": sum(1 for value in margins if value > 0.0),
            "block_count": len(margins),
            "zero_release_blocks": zero_zone,
            "zero_release_block_count": len(zero_zone),
        }

    payload = {
        "schema": "avycore-release-v2-spot-forcing-v1",
        "experiment_id": EXPERIMENT_ID,
        "status": "development_rescoring_of_burned_blocks",
        "partition": "development_only",
        "purpose": (
            "Re-test the zero-release-zone condition on the ERA5 forcing that "
            "produced it, using the repaired release engine. The completed "
            "configuration search ran on CERRA forcing, where the scalar-wind "
            "defect does not apply, so this condition had never been re-tested."
        ),
        "declared_before_scoring": {
            "configuration_ids": ["frozen_v1_reference"]
            + [config_id for config_id, _, _ in declared],
            "configuration_selection": (
                "Fixed and enumerated. No search, no seed, no sampling. The "
                "searched configuration is used exactly as frozen."
            ),
            "capture_rules": srs.CAPTURE_RULES,
            "primary_capture_rule": srs.PRIMARY_CAPTURE_RULE,
            "primary_capture_rule_reason": (
                "10% is what spot-blind-swiss-v1 froze for these blocks. The 5% "
                "rule of the configuration search is reported beside it for "
                "every block and configuration so neither can be selected after "
                "the fact."
            ),
            "baseline": (
                "Same-area slope-only selection on the pinned regime-hindcast-v1 "
                "slope response, which is byte-identical to the production slope "
                "curve spot-blind-swiss-v1 used for its own slope baseline."
            ),
            "baseline_budget": (
                "The release-only flagged-cell count of the configuration being "
                "scored. The frozen artifact's published slope baseline uses the "
                "end-to-end budget instead, so it is not quoted as a margin here."
            ),
        },
        "integrity": {
            "frozen_experiment_specs_modified": False,
            "frozen_artifacts_or_digests_modified": False,
            "frozen_v1_sources_modified": False,
            "reserved_blocks_predicted_or_scored": False,
            "reserved_blocks_still_sealed": [
                "row1col4",
                "row2col4",
                "row6col9",
                "row5col10",
            ],
            "all_scored_blocks_are_burned": (
                "Every SPOT block was predicted, scored and published in "
                "spot-blind-swiss-v1, and its outlines were opened. Every number "
                "in this artifact is a development number."
            ),
            "eligible_mask_matches_frozen_prediction": True,
            "emails_sent": False,
        },
        "scope": {
            "is_validated": False,
            "strict_field_holdout_events_added": 0,
            "note": (
                "Beating or losing to a slope baseline is not validation. This "
                "adds no eligible field-holdout event and licenses no accuracy "
                "claim."
            ),
        },
        "blocks": [per_block_context[block.block_id] for block in blocks],
        "evaluations": evaluations,
        "inputs": {
            "frozen_spot_spec_sha256": _sha256_file(
                REPOSITORY_ROOT / "validation-data/experiments/spot-blind-swiss-v1.json"
            ),
            "frozen_spot_holdout_result_sha256": _sha256_file(FROZEN_HOLDOUT_RESULT),
            "frozen_spot_development_result_sha256": _sha256_file(
                FROZEN_DEVELOPMENT_RESULT
            ),
            "release_config_search_result_sha256": _sha256_file(SEARCH_RESULT_PATH),
            "release_v2_source_sha256": _sha256_file(
                REPOSITORY_ROOT
                / "packages/avycore/src/avycore/snowpack/release_v2.py"
            ),
            "cache_builder_source_sha256": _sha256_file(
                REPOSITORY_ROOT / "scripts/validation/build_spot_release_cache.py"
            ),
            "scorer_source_sha256": _sha256_file(
                REPOSITORY_ROOT / "scripts/validation/spot_release_search.py"
            ),
            "runner_source_sha256": _sha256_file(Path(__file__).resolve()),
        },
        "drift_kernels_available": list(DRIFT_KERNELS),
        "software_environment": _software_environment(),
        "disclaimer": (
            "Experimental research prototype; not an operational avalanche "
            "forecast and not a replacement for a published bulletin."
        ),
    }

    args.result.parent.mkdir(parents=True, exist_ok=True)
    # ``newline="\n"`` is not cosmetic: a CRLF artifact hashes differently from
    # the bytes git stores for it, which makes any digest over it unreproducible.
    args.result.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"result": str(args.result), "blocks": len(blocks)}))


if __name__ == "__main__":
    main()
