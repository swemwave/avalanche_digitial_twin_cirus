"""Derive a compact, machine-readable report from frozen hindcast results.

This script performs reporting arithmetic only. It cannot alter predictions,
model parameters, the frozen specification, or source identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _aggregate_metric(blocks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    metrics = [block["metrics"][name] for block in blocks]
    events = sum(item["event_count"] for item in metrics)
    captured = sum(item["captured_event_count"] for item in metrics)
    mapped = sum(item["mapped_positive_union_cell_count"] for item in metrics)
    intersecting = sum(item["intersecting_mapped_positive_cell_count"] for item in metrics)
    eligible = sum(item["eligible_terrain_cell_count"] for item in metrics)
    predicted = sum(item["predicted_eligible_cell_count"] for item in metrics)
    return {
        "event_count": events,
        "captured_event_count": captured,
        "event_capture_fraction": captured / events,
        "mapped_positive_union_cell_count": mapped,
        "intersecting_mapped_positive_cell_count": intersecting,
        "mapped_positive_footprint_coverage_fraction": intersecting / mapped,
        "eligible_terrain_cell_count": eligible,
        "predicted_eligible_cell_count": predicted,
        "flagged_eligible_terrain_fraction": predicted / eligible,
        "incomplete_input_event_count": sum(
            item["incomplete_input_event_count"] for item in metrics
        ),
    }


def _source_lineage(spec: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in spec["source_inputs"]}
    defaults = {
        "spot_2018": {
            "version": "EnviDat archive retrieved 2026-08-13",
            "crs": "EPSG:2056 for extracted vectors",
            "unit": "projected coordinates in metres; polygon attributes categorical",
            "transformation": "safe ZIP extraction only before scoring",
        },
        "spot_2019": {
            "version": "EnviDat archive retrieved 2026-08-13",
            "crs": "EPSG:2056 for extracted vectors",
            "unit": "projected coordinates in metres; polygon attributes categorical",
            "transformation": "safe ZIP extraction only before scoring",
        },
        "aerial_1999": {
            "version": "EnviDat v1.0; archive last modified 2025-08-22; retrieved 2026-08-13",
            "crs": "EPSG:2056 for extracted vectors",
            "unit": "projected coordinates in metres; polygon attributes categorical",
            "transformation": "safe outer and nested ZIP extraction only before selection/scoring",
        },
        "globcover_2009": {
            "version": "GlobCover 2009 v2.3",
            "crs": "EPSG:4326 native raster",
            "unit": "categorical land-cover class",
            "transformation": "nearest-neighbour reprojection to EPSG:2056 at 30 m; declared forest-class mapping",
        },
    }
    result = []
    for item in spec["source_inputs"]:
        parent_id = item.get("extracted_from")
        parent = by_id.get(parent_id, {})
        family = defaults.get(parent_id or item["id"], {})
        source_id = item["id"]
        if source_id.startswith("Copernicus_DSM"):
            version = item.get("product_edition", "2021")
            crs = item.get("native_crs", "EPSG:4326")
            unit = item.get("unit", "metre elevation")
            transformation = "bilinear reprojection to EPSG:2056 at 30 m; masks preserved"
        elif source_id.startswith("cerra_"):
            version = item.get("product_version")
            crs = item.get("native_crs")
            unit = item.get("units")
            transformation = item.get("transformations") + "; nearest-sample assignment to terrain cells and documented elevation lapse transfer"
        else:
            version = family.get("version", item.get("product_version", item.get("product_edition")))
            crs = item.get("crs", item.get("native_crs", family.get("crs", "archive container; not applicable")))
            unit = item.get("unit", item.get("units", family.get("unit", "archive bytes; not applicable")))
            transformation = item.get("transformations", family.get("transformation", "none; byte-for-byte download"))
        result.append(
            {
                "id": source_id,
                "role": item["role"],
                "root": item["root"],
                "path": item["path"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "url": item.get("url", parent.get("url")),
                "doi": item.get("doi", parent.get("doi")),
                "licence": item.get("licence", parent.get("licence")),
                "version": version,
                "crs": crs,
                "unit": unit,
                "transformation": transformation,
                "missing_value_rule": item.get(
                    "missing_rule",
                    "Vector/raster source is required byte-for-byte; no missing value is converted to zero.",
                ),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_bytes())
    development = json.loads(args.development.read_bytes())
    holdout = json.loads(args.holdout.read_bytes())
    metric_names = (
        "release_only",
        "routed_nonrelease_only",
        "hybrid_end_to_end",
        "alpha_only_end_to_end",
        "dynamics_only_end_to_end",
        "release_dry_slab",
        "release_wet_snow",
        "release_dry_loose",
        "release_full_depth_glide",
    )
    mapped_regimes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for block in holdout["block_results"]:
        for regime, values in block["stratified_primary_capture"]["type"].items():
            mapped_regimes[regime][0] += values["captured_event_count"]
            mapped_regimes[regime][1] += values["event_count"]
    report = {
        "schema": "avycore-regime-hindcast-derived-report-v1",
        "experiment_id": spec["experiment_id"],
        "experiment_spec_sha256": _sha256(args.spec),
        "development_result_sha256": _sha256(args.development),
        "holdout_result_sha256": _sha256(args.holdout),
        "development_split": spec["partitions"]["development"]["split_description"],
        "holdout_split": spec["partitions"]["holdout"]["split_description"],
        "holdout_aggregate_by_ablation": {
            name: _aggregate_metric(holdout["block_results"], name)
            for name in metric_names
        },
        "holdout_mapped_regime_stratification": {
            regime: {
                "captured_event_count": values[0],
                "event_count": values[1],
                "event_capture_fraction": values[0] / values[1],
            }
            for regime, values in sorted(mapped_regimes.items())
        },
        "holdout_same_budget_baselines": {
            block["block_id"]: block["baselines"] for block in holdout["block_results"]
        },
        "terrain_block_bootstrap_intervals": holdout["aggregate"][
            "mountain_block_bootstrap_intervals"
        ],
        "holdout_event_support": {
            "incomplete_event_count": sum(
                block["metrics"]["hybrid_end_to_end"]["incomplete_input_event_count"]
                for block in holdout["block_results"]
            ),
            "event_count": holdout["aggregate"]["mapped_event_count"],
            "terrain_and_CERRA_complete_in_all_blocks": all(
                block["prediction_summary"]["terrain"]["core_complete_input_fraction"] == 1.0
                for block in holdout["block_results"]
            ),
            "cause": "Mapped geometries intersected the edge of the aerial-acquisition-minus-cloud eligible mask; these events were retained and forced uncaptured.",
        },
        "domain_escape": {
            "block_count": sum(block["domain_escape"] for block in holdout["block_results"]),
            "particles_left_the_aoi": sum(
                block["prediction_summary"]["engines"]["hybrid"]["particles_left_the_aoi"]
                for block in holdout["block_results"]
            ),
        },
        "acceptance_passed": holdout["acceptance_passed"],
        "strongest_permitted_claim": (
            "On this positive-only 1999 five-block comparison, the frozen multi-regime "
            "model captured more mapped events than the earlier scalar-forcing experiment, "
            "but failed the predeclared capture, completeness, slope-baseline and random-"
            "baseline criteria. It is not field validated."
        ),
        "strict_holdout_n": 0,
        "is_validated": False,
        "trusted_registry_modified": False,
        "emails_sent": False,
        "parameters_changed_after_holdout_view": False,
        "source_lineage": _source_lineage(spec),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"output": str(args.output), "sha256": _sha256(args.output)}))


if __name__ == "__main__":
    main()
