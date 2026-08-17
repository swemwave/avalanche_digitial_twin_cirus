"""Freeze the regime-hindcast specification from an acquisition manifest.

Run this only while development remains open. Once a holdout prediction has
been generated, changing this specification, its model sources, or its fixed
parameters constitutes a new experiment and the existing holdout must remain
preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SOURCE = REPOSITORY_ROOT / "packages" / "avycore" / "src"
BACKEND_SOURCE = REPOSITORY_ROOT / "backend"
for source in (AVYCORE_SOURCE, BACKEND_SOURCE):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from app.assess import assessment_model_identity  # noqa: E402
from avycore.snowpack import parameter_manifest  # noqa: E402

MODEL_SOURCES = (
    Path("packages/avycore/src/avycore/snowpack/forcing.py"),
    Path("packages/avycore/src/avycore/snowpack/solar.py"),
    Path("packages/avycore/src/avycore/snowpack/state.py"),
    Path("packages/avycore/src/avycore/snowpack/regimes.py"),
    Path("packages/avycore/src/avycore/snowpack/zones.py"),
    Path("packages/avycore/src/avycore/hazard/runout.py"),
    Path("scripts/validation/run_regime_hindcast.py"),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _relative_source(record: dict[str, Any], source_root: Path, target_root: Path) -> dict[str, Any]:
    absolute = Path(record["path"]).resolve()
    try:
        relative = absolute.relative_to(source_root)
        root = "source_root"
    except ValueError:
        relative = absolute.relative_to(target_root)
        root = "target_root"
    source_id = record["source_id"]
    role = record["role"]
    if role in {"evaluation", "evaluation_target_vector_component"}:
        role = "evaluation_target_archive" if absolute.suffix == ".zip" else "evaluation_target"
    return {
        "id": source_id,
        "role": role,
        "root": root,
        "path": relative.as_posix(),
        "bytes": record["bytes"],
        "sha256": record["sha256"],
        **{
            key: record[key]
            for key in (
                "url",
                "doi",
                "licence",
                "product",
                "product_version",
                "product_edition",
                "native_crs",
                "native_resolution",
                "native_horizontal_resolution",
                "temporal_resolution",
                "unit",
                "units",
                "transformations",
                "missing_rule",
                "extracted_from",
                "crs",
            )
            if key in record and record[key] is not None
        },
    }


def _dem_id(old_id: str) -> str:
    parts = old_id.split("_")
    latitude = parts[-2].upper()
    longitude = parts[-1].upper()
    return f"Copernicus_DSM_COG_10_{latitude}_00_{longitude}_00_DEM"


def _prepare_block(block: dict[str, Any], *, partition: str) -> dict[str, Any]:
    result = dict(block)
    result["partition"] = partition
    result["meteorology_source_id"] = f"cerra_{block['block_id']}"
    result["release_size"] = "medium"
    result["flow_regime"] = "multi_regime_target_independent"
    result["dem_source_ids"] = [
        item if item.startswith("Copernicus_DSM") else _dem_id(item)
        for item in block["dem_source_ids"]
    ]
    if partition == "development":
        result["outline_shapefile_source_id"] = (
            "spot_2018_shp" if int(block["campaign_year"]) == 2018 else "spot_2019_shp"
        )
    else:
        result["outline_shapefile_source_id"] = "spot_1999_shp"
        result["coverage_source_id"] = "coverage_1999_shp"
        result["cloud_source_id"] = "cloud_1999_shp"
    result.pop("storm_window", None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquisition", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    acquisition = json.loads(args.acquisition.read_bytes())
    source_root = Path(acquisition["source_root"]).resolve()
    target_root = Path(acquisition["target_root"]).resolve()
    source_inputs = [
        _relative_source(record, source_root, target_root)
        for record in acquisition["source_files"]
    ]
    ids = [item["id"] for item in source_inputs]
    if len(ids) != len(set(ids)):
        raise ValueError("Acquisition source IDs are not unique.")
    development = [
        _prepare_block(block, partition="development")
        for block in acquisition["development_blocks"]
    ]
    holdout = [
        _prepare_block(block, partition="holdout")
        for block in acquisition["holdout_blocks"]
    ]
    all_blocks = development + holdout
    spec = {
        "schema": "avycore-regime-hindcast-experiment-v1",
        "experiment_id": "regime-hindcast-v1",
        "status": "frozen_before_holdout_prediction",
        "frozen_at_utc": "2026-08-13T23:00:00Z",
        "purpose": (
            "A positive-only release-plus-runout hindcast testing CERRA 5.5 km hourly "
            "forcing, an evolving snow-state representation, and target-independent "
            "dry-slab, wet-snow and dry-loose release mechanisms."
        ),
        "safety": {
            "is_operational_forecast": False,
            "is_probability": False,
            "disclaimer": (
                "Experimental research prototype; not an operational avalanche forecast "
                "and not a replacement for Avalanche Canada guidance or field assessment. "
                "Scores are relative indices, not probabilities."
            ),
        },
        "leakage_controls": {
            "development_targets": "2018 and 2019 SPOT campaigns; all prior outcomes already viewed",
            "holdout_target": "1999 panchromatic aerial mapping campaign; different campaign, years and fixed terrain cores",
            "holdout_block_selection_used_avalanche_outlines": False,
            "prediction_command_resolves_evaluation_targets": False,
            "score_requires_prediction_bound_to_exact_spec_sha256": True,
            "mapped_type_size_geometry_used_by_prediction": False,
            "negative_evidence_used": False,
            "post_holdout_parameter_changes_permitted": False,
        },
        "partitions": {
            "development": {
                "split_description": "Previously viewed 24 Jan 2018 and 16 Jan 2019 SPOT campaigns; five fixed blocks; development only.",
                "blocks": development,
            },
            "holdout": {
                "split_description": (
                    "Previously unscored 25 Feb-1 Mar 1999 aerial campaign; five fixed "
                    "20.1 km lattice blocks selected without avalanche outlines; predictions "
                    "union three predeclared storm cycles (26-29 Jan, 5-10 Feb, 17-24 Feb)."
                ),
                "block_selection_artifact": "validation-data/experiments/regime-hindcast-v1-holdout-blocks.json",
                "blocks": holdout,
            },
        },
        "landcover": {
            "source_id": "globcover_2009_tif",
            "forest_classes": [40, 50, 60, 70, 90, 100, 160, 170],
            "mosaic_class_fraction": {"110": 0.6, "120": 0.3},
        },
        "fixed_model": {
            "random_seed": 20260813,
            "release_size": "medium",
            "hybrid_zone_cap": 6,
            "alpha_only_zone_cap": 12,
            "dynamics_only_zone_cap": 6,
            "maximum_zones_per_regime": 40,
            "unsupported_regimes": ["full_depth_glide"],
            "dry_loose_runout_mapping": "dry_slab flow factors; no calibrated loose-snow runout factors exist",
        },
        "metrics": {
            "positive_only": True,
            "event_capture_minimum_overlap_fraction": 0.05,
            "mountain_block_bootstrap": {
                "confidence_level": 0.95,
                "replicate_count": 10_000,
                "random_seed": 20260814,
            },
        },
        "baselines": {
            "same_area_budget": True,
            "random_replicate_count": 1000,
            "random_seed_by_block": {
                block["block_id"]: 310_000 + index for index, block in enumerate(all_blocks)
            },
        },
        "acceptance_rule": {
            "minimum_qualifying_group_count": 2,
            "minimum_mapped_events_per_group": 5,
            "minimum_event_capture_fraction": 0.70,
            "maximum_flagged_eligible_terrain_fraction": 0.20,
            "must_exceed_same_area_slope_only_in_every_qualifying_group": True,
            "must_exceed_same_area_random_97_5_percentile_in_every_qualifying_group": True,
            "require_complete_inputs": True,
            "require_no_domain_escape": True,
            "all_predeclared_groups_must_qualify": True,
        },
        "source_inputs": source_inputs,
        "model_identity": {
            "snowpack_parameter_manifest": parameter_manifest(),
            "snowpack_parameter_sha256": _canonical_sha256(parameter_manifest()),
            "runout_parameter_sha256": assessment_model_identity()["sha256"],
            "source_sha256": {
                path.as_posix(): _sha256_file(REPOSITORY_ROOT / path) for path in MODEL_SOURCES
            },
        },
        "limitations": [
            "CERRA is reanalysis at 5.5 km, not an observation or a 30 m meteorological field.",
            "CERRA snow depth is a modelled grid-cell mean; no observed snow profile, weak layer, stability test or basal liquid-water state is available.",
            "The buried-interface proxy is diagnostic only and cannot change release scores.",
            "Wet-snow scoring is a surface-wetting susceptibility proxy and does not distinguish wet slab from wet loose.",
            "Full-depth/glide release is explicitly unsupported.",
            "Positive-only mapped outlines provide no verified negatives and cannot validate specificity or false-alarm rate.",
            "The 1999 mapping is cumulative across three avalanche cycles and does not provide an exact release timestamp for every outline.",
            "Fixed terrain blocks are bootstrap units, not statistically independent mountain ranges.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"output": str(args.output), "sha256": _sha256_file(args.output)}))


if __name__ == "__main__":
    main()
