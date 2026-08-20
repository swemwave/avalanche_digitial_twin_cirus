"""Acquire nearest-sample Sentinel-1 DN chips for radiometric calibration.

The general imagery acquisition artifact retains the originally requested visual
resampling lineage.  This separate immutable artifact reuses its frozen scene
selection and public source URLs but GCP-geocodes raw GRD samples with nearest
resampling so amplitude is never bilinearly mixed before power calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-sentinel1-dn-nearest-v1"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
ACQUISITION_SCRIPT = Path(__file__).with_name("acquire_public_event_imagery.py")


def _acquisition_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "public_event_imagery_acquisition_helpers", ACQUISITION_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load imagery acquisition helpers.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _polarizations(scene: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            asset
            for asset in scene["assets"]
            if asset["asset_name"] in {"hh", "hv", "vh", "vv"}
        ],
        key=lambda asset: asset["asset_name"],
    )


def _candidate_worker(
    payload: tuple[dict[str, Any], str, bool]
) -> dict[str, Any]:
    candidate, cache_root_text, offline = payload
    helpers = _acquisition_module()
    cache_root = Path(cache_root_text)
    grid = candidate["chip_grid"]
    target_epsg = int(str(grid["crs"]).split(":", 1)[1])
    scenes = []
    for scene in candidate["sentinel_1_grd"]["scenes"]:
        assets = []
        for source_asset in _polarizations(scene):
            name = source_asset["asset_name"]
            result = helpers._acquire_raster_chip(
                source_asset["source_href"],
                cache_root
                / candidate["candidate_id"]
                / scene["position"]
                / f"{name}.tif",
                target_epsg=target_epsg,
                center_x=float(grid["center"][0]),
                center_y=float(grid["center"][1]),
                asset_name=name,
                offline=offline,
                force_resampling="nearest",
            )
            result["source_acquisition_asset_sha256"] = source_asset["sha256"]
            result["pre_calibration_resampling"] = "nearest"
            assets.append(result)
        scenes.append(
            {
                "position": scene["position"],
                "earth_search_item_id": scene["earth_search_item_id"],
                "acquisition_time_utc": scene["acquisition_time_utc"],
                "assets": assets,
            }
        )
    result: dict[str, Any] = {
        "candidate_id": candidate["candidate_id"],
        "status": "acquired_nearest_sample_dn_for_calibration",
        "chip_grid": grid,
        "scenes": scenes,
    }
    result["normalized_candidate_sha256"] = _sha256_bytes(_canonical_json(result))
    return result


def build_nearest_dn(
    acquisition_path: Path,
    cache_root: Path,
    *,
    offline: bool,
    workers: int,
) -> dict[str, Any]:
    payload = acquisition_path.read_bytes()
    acquisition = json.loads(payload)
    if acquisition.get("schema") != "avycore-public-event-imagery-acquisition-v2":
        raise ValueError("Nearest-DN acquisition requires imagery acquisition v2.")
    if acquisition.get("predictions_generated") is not False:
        raise ValueError("Refusing imagery selected after prediction access.")
    if workers < 1:
        raise ValueError("Workers must be at least one.")
    inputs = [
        (candidate, str(cache_root), offline)
        for candidate in acquisition["candidates"]
    ]
    if workers == 1:
        candidates = [_candidate_worker(item) for item in inputs]
    else:
        ordered: list[dict[str, Any] | None] = [None] * len(inputs)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_candidate_worker, item): index
                for index, item in enumerate(inputs)
            }
            for future in as_completed(futures):
                index = futures[future]
                ordered[index] = future.result()
                print(
                    f"Completed nearest DN for {inputs[index][0]['candidate_id']}.",
                    flush=True,
                )
        if any(candidate is None for candidate in ordered):
            raise RuntimeError("A nearest-DN candidate result is missing.")
        candidates = [candidate for candidate in ordered if candidate is not None]
    assets = [
        asset
        for candidate in candidates
        for scene in candidate["scenes"]
        for asset in scene["assets"]
    ]
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_imagery_acquisition_sha256": _sha256_bytes(payload),
        "selection_reused_without_change": True,
        "predictions_generated": False,
        "model_results_opened": False,
        "holdout_targets_accessed": False,
        "resampling": "nearest",
        "radiometric_reason": (
            "Preserve one source GRD amplitude sample per target pixel; do not bilinearly "
            "mix amplitude before DN^2 noise subtraction and calibration."
        ),
        "claim_boundary": (
            "Nearest GCP geocoding preserves samples for the separate radiometric stage. "
            "It is not radiometric calibration or terrain flattening by itself."
        ),
        "counts": {
            "candidates": len(candidates),
            "polarization_assets": len(assets),
            "empty_assets": sum(
                asset["raster"]["valid_pixel_count_all_bands"] == 0
                for asset in assets
            ),
            "valid_pixels": sum(
                asset["raster"]["valid_pixel_count_all_bands"] for asset in assets
            ),
        },
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--acquisition",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-imagery-acquisition-v2.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache/public-event-sentinel1-dn-nearest-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-sentinel1-dn-nearest-v1.json",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_nearest_dn(
        args.acquisition.resolve(),
        args.cache_root.resolve(),
        offline=args.offline,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Nearest S1 DN assets={artifact['counts']['polarization_assets']}; "
        f"empty={artifact['counts']['empty_assets']}."
    )


if __name__ == "__main__":
    main()
