"""Predict and score the frozen multi-regime Swiss avalanche hindcast.

``predict`` verifies only terrain, land cover, CERRA, image coverage and cloud
inputs. It cannot resolve or open an avalanche-outline source. ``score`` first
verifies a hashed prediction bound to the exact frozen specification and only
then resolves the target vectors. This one-way split is the experiment's main
target-leakage control.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from shapely.geometry import mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SOURCE = REPOSITORY_ROOT / "packages" / "avycore" / "src"
BACKEND_SOURCE = REPOSITORY_ROOT / "backend"
SCRIPT_SOURCE = Path(__file__).resolve().parent
for source in (AVYCORE_SOURCE, BACKEND_SOURCE, SCRIPT_SOURCE):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

import run_spot_blind_hindcast as legacy  # noqa: E402
from app.assess import MODEL_VERSION, _config, assessment_model_identity  # noqa: E402
from avycore.hazard import runout  # noqa: E402
from avycore.hazard.conditions import Conditions  # noqa: E402
from avycore.snowpack import (  # noqa: E402
    DRY_LOOSE,
    DRY_SLAB,
    FULL_DEPTH_GLIDE,
    REGIMES,
    WET_SNOW,
    ForcingSampleGrid,
    HourlyForcing,
    compute_regime_release,
    extract_regime_release_zones,
    insolation_index,
    integrate_snow_state,
    parameter_manifest,
    sample_lattice,
)

SPEC_RELATIVE_PATH = Path("validation-data/experiments/regime-hindcast-v1.json")
PREDICTION_RELATIVE_DIR = Path("validation-data/predictions/regime-hindcast-v1")
RESULT_RELATIVE_DIR = Path("validation-data/results")
RUNNER_RELATIVE_PATH = Path("scripts/validation/run_regime_hindcast.py")
MODEL_SOURCE_PATHS = (
    Path("packages/avycore/src/avycore/snowpack/forcing.py"),
    Path("packages/avycore/src/avycore/snowpack/solar.py"),
    Path("packages/avycore/src/avycore/snowpack/state.py"),
    Path("packages/avycore/src/avycore/snowpack/regimes.py"),
    Path("packages/avycore/src/avycore/snowpack/zones.py"),
    Path("packages/avycore/src/avycore/hazard/runout.py"),
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return value


def _source_path(item: dict[str, Any], source_root: Path, target_root: Path) -> Path:
    root = {
        "source_root": source_root,
        "target_root": target_root,
        "repository_root": REPOSITORY_ROOT,
    }.get(item["root"])
    if root is None:
        raise ValueError(f"Unknown source root {item['root']!r}.")
    return root / Path(item["path"])


def _verify_sources(
    spec: dict[str, Any],
    *,
    source_root: Path,
    target_root: Path,
    include_targets: bool,
) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for item in spec["source_inputs"]:
        if item["role"].startswith("evaluation_target") and not include_targets:
            continue
        path = _source_path(item, source_root, target_root)
        if not path.is_file():
            raise FileNotFoundError(f"Frozen source is missing: {path}")
        if path.stat().st_size != int(item["bytes"]):
            raise ValueError(f"Frozen byte size changed for {item['id']!r}.")
        actual = _sha256_file(path)
        if actual != item["sha256"]:
            raise ValueError(f"Frozen SHA-256 changed for {item['id']!r}: {actual}")
        resolved[item["id"]] = path
    return resolved


def _verify_model_identity(spec: dict[str, Any]) -> None:
    identity = spec["model_identity"]
    if _canonical_sha256(parameter_manifest()) != identity["snowpack_parameter_sha256"]:
        raise ValueError("The frozen snow/regime parameter manifest changed.")
    if assessment_model_identity()["sha256"] != identity["runout_parameter_sha256"]:
        raise ValueError("The frozen production runout parameter manifest changed.")
    paths = (*MODEL_SOURCE_PATHS, RUNNER_RELATIVE_PATH)
    for relative in paths:
        actual = _sha256_file(REPOSITORY_ROOT / relative)
        if actual != identity["source_sha256"][relative.as_posix()]:
            raise ValueError(f"Frozen model identity mismatch for {relative.as_posix()}.")


def _partition_blocks(spec: dict[str, Any], partition: str) -> list[dict[str, Any]]:
    if partition not in ("development", "holdout"):
        raise ValueError("partition must be development or holdout.")
    return list(spec["partitions"][partition]["blocks"])


def _forcing_grid(block: dict[str, Any], path: Path) -> ForcingSampleGrid:
    payload = json.loads(path.read_bytes())
    points = payload if isinstance(payload, list) else [payload]
    east, north = sample_lattice(
        west_m=float(block["simulation_grid"]["bounds"][0]),
        south_m=float(block["simulation_grid"]["bounds"][1]),
        east_m=float(block["simulation_grid"]["bounds"][2]),
        north_m=float(block["simulation_grid"]["bounds"][3]),
        count_per_axis=3,
    )
    if len(points) != len(east):
        raise ValueError(f"{block['block_id']} forcing point count changed.")
    forcings = []
    for point in points:
        hourly = point["hourly"]
        forcings.append(
            HourlyForcing(
                times_utc=tuple(hourly["time"]),
                latitude_deg=float(point["latitude"]),
                longitude_deg=float(point["longitude"]),
                sample_elevation_m=float(point["elevation"]),
                air_temperature_c=np.asarray(hourly["temperature_2m"]),
                precipitation_mm=np.asarray(hourly["precipitation"]),
                provider_snowfall_cm=np.asarray(hourly["snowfall"]),
                snow_depth_m=np.asarray(hourly["snow_depth"]),
                wind_speed_10m_kmh=np.asarray(hourly["wind_speed_10m"]),
                wind_from_direction_deg=np.asarray(hourly["wind_direction_10m"]),
                shortwave_radiation_w_m2=np.asarray(hourly["shortwave_radiation"]),
            )
        )
    return ForcingSampleGrid(
        sample_easting_m=east,
        sample_northing_m=north,
        forcings=tuple(forcings),
        crs="EPSG:2056",
    )


def _cell_centres(grid: legacy.ExperimentGrid) -> tuple[np.ndarray, np.ndarray]:
    east = grid.west + (np.arange(grid.width, dtype="float64") + 0.5) * grid.resolution_m
    north = grid.north - (np.arange(grid.height, dtype="float64") + 0.5) * grid.resolution_m
    return np.meshgrid(east, north)


def _coverage_mask(
    block: dict[str, Any], sources: dict[str, Path], grid: legacy.ExperimentGrid
) -> np.ndarray:
    if "coverage_source_id" not in block:
        return np.ones(grid.shape, dtype=bool)
    # These are image-acquisition metadata, not avalanche targets.
    import geopandas as gpd
    from shapely import union_all

    coverage = gpd.read_file(sources[block["coverage_source_id"]])
    cloud = gpd.read_file(sources[block["cloud_source_id"]])
    if str(coverage.crs).upper() != "EPSG:2056" or str(cloud.crs).upper() != "EPSG:2056":
        raise ValueError("Frozen coverage and cloud layers must be EPSG:2056.")
    geometry = union_all(coverage.geometry.values).difference(union_all(cloud.geometry.values))
    return rasterize(
        [(mapping(geometry), 1)],
        out_shape=grid.shape,
        transform=grid.transform,
        fill=0,
        all_touched=False,
        dtype="uint8",
    ).astype(bool)


def _simulate_engine(
    engine: Any,
    *,
    zones: Sequence[Any],
    cap: int,
    terrain: legacy.ExperimentTerrain,
    seed: int | None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    selected = sorted(
        zones, key=lambda zone: zone.properties["estimated_release_score"], reverse=True
    )[:cap]
    reached = np.zeros(terrain.grid.shape, dtype=bool)
    uncertainty = np.zeros(terrain.grid.shape, dtype=bool)
    particles_left = 0
    summaries = []
    flow_map = {DRY_SLAB: "dry_slab", DRY_LOOSE: "dry_slab", WET_SNOW: "wet_snow"}
    for position, zone in enumerate(selected):
        regime = zone.properties["release_regime"]
        if regime == FULL_DEPTH_GLIDE:
            raise AssertionError("Unsupported glide release produced a runout zone.")
        result = engine.simulate(
            zone=zone,
            grid=terrain.grid,
            elevation=terrain.layer("elevation"),
            slope=terrain.layer("slope"),
            forest_mask=terrain.layer("forest_mask"),
            plan_curvature=terrain.layer("plan_curvature"),
            config=_config(),
            release_size="medium",
            seed=None if seed is None else seed + position,
            alpha_override_deg=None,
            flow_regime=flow_map[regime],
        )
        reached |= result.reached
        uncertainty |= result.uncertainty
        particles_left += int(result.metadata.get("particles_left_the_aoi", 0))
        summaries.append(
            {
                "zone_id": result.zone_id,
                "release_regime": regime,
                "runout_flow_regime": flow_map[regime],
                "reached_cells": int(np.count_nonzero(result.reached)),
                "particles_left_the_aoi": int(result.metadata.get("particles_left_the_aoi", 0)),
            }
        )
    return reached, uncertainty, {
        "engine": engine.name,
        "engine_mode": engine.engine_mode,
        "zone_cap": cap,
        "available_release_zone_count": len(zones),
        "simulated_release_zone_count": len(selected),
        "particles_left_the_aoi": particles_left,
        "reached_simulation_boundary": legacy._touches_boundary(reached),
        "uncertainty_reached_simulation_boundary": legacy._touches_boundary(uncertainty),
        "zones": summaries,
    }


def _predict_cycle(
    *,
    cycle: dict[str, Any],
    samples: ForcingSampleGrid,
    terrain: legacy.ExperimentTerrain,
    release_support: np.ndarray,
    sample_index: np.ndarray,
    seed: int,
    spec: dict[str, Any],
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    window = samples.window(
        cycle["antecedent_start_exclusive_utc"], cycle["end_utc"]
    )
    state = integrate_snow_state(
        window,
        elevation_m=terrain.layer("elevation"),
        sample_index=sample_index,
        storm_start_exclusive_utc=cycle["start_utc"],
        supported=release_support,
    )
    shortwave = np.mean(window.stack("shortwave_radiation_w_m2"), axis=0)
    latitude = float(np.mean([item.latitude_deg for item in window.forcings]))
    longitude = float(np.mean([item.longitude_deg for item in window.forcings]))
    solar = insolation_index(
        slope_deg=np.asarray(terrain.layer("slope").filled(0.0)),
        aspect_deg=np.asarray(terrain.layer("aspect").filled(-1.0)),
        timestamps_utc=window.times_utc,
        shortwave_w_m2=shortwave,
        latitude_deg=latitude,
        longitude_deg=longitude,
    )
    field = compute_regime_release(
        terrain, state, insolation_index=solar, supported=release_support
    )
    zone_set = extract_regime_release_zones(
        terrain,
        field,
        state_layers={
            "new_snow_index_cm": state.new_snow_index_cm,
            "drift_index_normalized": state.drift_index_normalized,
            "rain_on_snow_mm": state.rain_on_snow_mm,
            "positive_degree_hours": state.positive_degree_hours,
            "weak_interface_proxy_diagnostic": state.buried_weak_interface_proxy,
        },
        maximum_zones_per_regime=int(spec["fixed_model"]["maximum_zones_per_regime"]),
    )
    release_by_regime = {regime: np.zeros(terrain.grid.shape, dtype=bool) for regime in REGIMES}
    release = np.zeros(terrain.grid.shape, dtype=bool)
    for zone in zone_set.zones:
        release |= zone.pixels
        release_by_regime[zone.properties["release_regime"]] |= zone.pixels
    hybrid, hybrid_uncertainty, hybrid_meta = _simulate_engine(
        runout.ParticleRunoutEngine(runout.HYBRID),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["hybrid_zone_cap"]),
        terrain=terrain,
        seed=seed,
    )
    alpha, alpha_uncertainty, alpha_meta = _simulate_engine(
        runout.FastRunoutEngine(),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["alpha_only_zone_cap"]),
        terrain=terrain,
        seed=None,
    )
    dynamics, dynamics_uncertainty, dynamics_meta = _simulate_engine(
        runout.ParticleRunoutEngine(runout.DYNAMICS_ONLY),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["dynamics_only_zone_cap"]),
        terrain=terrain,
        seed=seed,
    )
    arrays = {
        "release": release,
        "hybrid_runout": hybrid,
        "hybrid_uncertainty": hybrid_uncertainty,
        "alpha_only_runout": alpha,
        "alpha_only_uncertainty": alpha_uncertainty,
        "dynamics_only_runout": dynamics,
        "dynamics_only_uncertainty": dynamics_uncertainty,
        **{f"release_{name}": value for name, value in release_by_regime.items()},
    }
    metadata = {
        "cycle_id": cycle["cycle_id"],
        "window": cycle,
        "snow_state": state.summary(),
        "release": {
            "zone_count": len(zone_set.zones),
            "zone_count_by_regime": {
                regime: sum(
                    zone.properties["release_regime"] == regime for zone in zone_set.zones
                )
                for regime in REGIMES
            },
            "explanation": zone_set.explanation,
            "field_explanation": field.explanation,
        },
        "engines": {
            "hybrid": hybrid_meta,
            "alpha_only": alpha_meta,
            "dynamics_only": dynamics_meta,
        },
        "insolation": {
            "reference_latitude_deg": latitude,
            "reference_longitude_deg": longitude,
            "shortwave_spatial_aggregation": "arithmetic mean of nine native-cell series",
            "minimum": float(np.min(solar)),
            "maximum": float(np.max(solar)),
        },
    }
    return arrays, metadata


def _predict_block(
    block: dict[str, Any], spec: dict[str, Any], sources: dict[str, Path]
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    terrain, terrain_meta, core_window = legacy._terrain(block, spec, sources)
    samples = _forcing_grid(block, sources[block["meteorology_source_id"]])
    east, north = _cell_centres(terrain.grid)
    sample_index = samples.nearest_sample_index(east, north)
    release_support = np.zeros(terrain.grid.shape, dtype=bool)
    release_support[core_window] = True
    coverage = _coverage_mask(block, sources, terrain.grid)
    aggregate = {
        name: np.zeros(terrain.grid.shape, dtype=bool)
        for name in (
            "release",
            "hybrid_runout",
            "hybrid_uncertainty",
            "alpha_only_runout",
            "alpha_only_uncertainty",
            "dynamics_only_runout",
            "dynamics_only_uncertainty",
            *(f"release_{regime}" for regime in REGIMES),
        )
    }
    cycles = []
    for position, cycle in enumerate(block["storm_cycles"]):
        cycle_arrays, cycle_meta = _predict_cycle(
            cycle=cycle,
            samples=samples,
            terrain=terrain,
            release_support=release_support,
            sample_index=sample_index,
            seed=int(spec["fixed_model"]["random_seed"]) + position * 1000,
            spec=spec,
        )
        for name, value in cycle_arrays.items():
            aggregate[name] |= value
        cycles.append(cycle_meta)
    complete = np.logical_and.reduce(
        [
            ~np.ma.getmaskarray(terrain.layer(name))
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
    eligible_simulation = complete & coverage
    arrays = {
        "eligible": np.asarray(eligible_simulation[core_window], dtype=bool),
        "slope_deg": np.asarray(terrain.layer("slope").filled(np.nan)[core_window], dtype="float32"),
        **{name: np.asarray(value[core_window], dtype=bool) for name, value in aggregate.items()},
    }
    engine_names = ("hybrid", "alpha_only", "dynamics_only")
    engines = {}
    for name in engine_names:
        entries = [cycle["engines"][name] for cycle in cycles]
        engines[name] = {
            "engine": entries[0]["engine"],
            "engine_mode": entries[0]["engine_mode"],
            "particles_left_the_aoi": sum(item["particles_left_the_aoi"] for item in entries),
            "reached_simulation_boundary": any(item["reached_simulation_boundary"] for item in entries),
            "uncertainty_reached_simulation_boundary": any(
                item["uncertainty_reached_simulation_boundary"] for item in entries
            ),
            "cycles": entries,
        }
    metadata = {
        "schema": "avycore-regime-hindcast-prediction-v1",
        "block_id": block["block_id"],
        "model_version": MODEL_VERSION,
        "terrain": terrain_meta,
        "forcing": {
            "source_sha256": _sha256_file(sources[block["meteorology_source_id"]]),
            "source_product": "CERRA 5.5 km hourly via Open-Meteo",
            "spatial_assignment": samples.summary(),
        },
        "cycles": cycles,
        "release": {
            "zone_count": sum(cycle["release"]["zone_count"] for cycle in cycles),
            "release_cell_count_core": int(np.count_nonzero(arrays["release"])),
            "release_cell_count_by_regime_core": {
                regime: int(np.count_nonzero(arrays[f"release_{regime}"])) for regime in REGIMES
            },
        },
        "engines": engines,
        "evaluation_coverage": {
            "role": "aerial acquisition footprint minus clouds" if "coverage_source_id" in block else "complete terrain inputs",
            "core_fraction": float(coverage[core_window].mean()),
        },
        "mask_sha256": {
            name: legacy._bool_mask_sha256(value)
            for name, value in arrays.items()
            if value.dtype == np.bool_
        },
        "held_out_outlines_opened": False,
        "held_out_outlines_used_as_model_inputs": False,
        "prediction_generation_order": "terrain_and_CERRA_to_snow_state_to_regime_release_to_runout",
    }
    metadata["prediction_identity_sha256"] = _canonical_sha256(metadata)
    return arrays, metadata


def _write_prediction(
    output: Path, arrays: dict[str, np.ndarray], metadata: dict[str, Any], spec_sha256: str
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {**metadata, "experiment_spec_sha256": spec_sha256}
    np.savez_compressed(
        output,
        **arrays,
        metadata_json=np.asarray(json.dumps(metadata, sort_keys=True, separators=(",", ":"))),
    )


def _load_prediction(path: Path, spec_sha256: str) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    with np.load(path, allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"].item()))
        arrays = {name: np.asarray(archive[name]) for name in archive.files if name != "metadata_json"}
    if metadata["experiment_spec_sha256"] != spec_sha256:
        raise ValueError(f"Prediction {path.name} is not bound to the frozen spec.")
    for name, expected in metadata["mask_sha256"].items():
        if legacy._bool_mask_sha256(arrays[name]) != expected:
            raise ValueError(f"Prediction mask {name!r} failed integrity verification.")
    return arrays, metadata


def predict_partition(
    *, spec_path: Path, partition: str, source_root: Path, target_root: Path, prediction_dir: Path
) -> None:
    spec = _load_json(spec_path)
    _verify_model_identity(spec)
    sources = _verify_sources(
        spec, source_root=source_root, target_root=target_root, include_targets=False
    )
    spec_sha256 = _sha256_file(spec_path)
    for block in _partition_blocks(spec, partition):
        arrays, metadata = _predict_block(block, spec, sources)
        output = prediction_dir / f"{block['block_id']}.npz"
        _write_prediction(output, arrays, metadata, spec_sha256)
        print(
            json.dumps(
                {
                    "block_id": block["block_id"],
                    "prediction_artifact": str(output),
                    "prediction_artifact_sha256": _sha256_file(output),
                    "release_zone_count": metadata["release"]["zone_count"],
                    "held_out_outlines_opened": False,
                },
                sort_keys=True,
            )
        )


def score_partition(
    *,
    spec_path: Path,
    partition: str,
    source_root: Path,
    target_root: Path,
    prediction_dir: Path,
    result_path: Path,
) -> None:
    spec = _load_json(spec_path)
    _verify_model_identity(spec)
    sources = _verify_sources(
        spec, source_root=source_root, target_root=target_root, include_targets=True
    )
    spec_sha256 = _sha256_file(spec_path)
    block_results = []
    for block in _partition_blocks(spec, partition):
        target = sources[block["outline_shapefile_source_id"]]
        prediction_path = prediction_dir / f"{block['block_id']}.npz"
        arrays, metadata = _load_prediction(prediction_path, spec_sha256)
        if metadata["held_out_outlines_opened"] is not False:
            raise ValueError("Prediction does not prove held-out target isolation.")
        result = legacy._score_block(
            block=block,
            arrays=arrays,
            prediction_metadata=metadata,
            prediction_path=prediction_path,
            shapefile=target,
            spec=spec,
        )
        # Add mechanism-specific release-only metrics without using target type
        # to select a mechanism during prediction.
        event_masks, event_ids, complete, attributes, _ = legacy._target_events(target, block)
        for regime in REGIMES:
            result["metrics"][f"release_{regime}"] = legacy.storm_window_positive_metrics(
                arrays[f"release_{regime}"],
                eligible=arrays["eligible"],
                event_masks=event_masks,
                event_ids=event_ids,
                geometry_complete=complete,
                capture_minimum_overlap_fraction=float(spec["metrics"]["event_capture_minimum_overlap_fraction"]),
                cell_area_m2=float(block["core_grid"]["resolution_m"]) ** 2,
            ).to_dict()
        block_results.append(result)
        primary = result["metrics"]["hybrid_end_to_end"]
        print(json.dumps({"block_id": block["block_id"], "event_count": primary["event_count"], "captured_event_count": primary["captured_event_count"], "flagged_eligible_terrain_fraction": primary["flagged_eligible_terrain_fraction"], "acceptance_passed": result["acceptance_passed"]}, sort_keys=True))
    aggregate = legacy._aggregate_holdout(block_results, spec)
    output = {
        "schema": "avycore-regime-hindcast-results-v1",
        "experiment_id": spec["experiment_id"],
        "experiment_spec_path": spec_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "experiment_spec_sha256": spec_sha256,
        "partition": partition,
        "split": spec["partitions"][partition]["split_description"],
        "prediction_generated_before_target_scoring": True,
        "held_out_outlines_used_as_model_inputs": False,
        "negative_evidence_used": False,
        "parameters_changed_after_viewing_holdout_results": False,
        "block_results": block_results,
        "aggregate": aggregate,
        "acceptance_rule": spec["acceptance_rule"],
        "acceptance_passed": aggregate["acceptance_passed"],
        "strict_field_validation": {
            "field_validation_holdout_n": 0,
            "is_validated": False,
            "trusted_dataset_identity_registry_modified": False,
            "reason": "Positive-only remotely mapped outlines are not verified binary field-validation data.",
        },
        "source_inputs": spec["source_inputs"],
        "model_identity": spec["model_identity"],
        "software_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "rasterio", "geopandas", "shapely", "pyproj")},
        },
        "emails_sent": False,
        "disclaimer": "Experimental research prototype; not an operational avalanche forecast and not a replacement for Avalanche Canada guidance or field assessment. Scores are relative indices, not probabilities.",
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"result": str(result_path), "result_sha256": _sha256_file(result_path), "acceptance_passed": output["acceptance_passed"]}, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=REPOSITORY_ROOT / SPEC_RELATIVE_PATH)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--prediction-dir", type=Path, default=REPOSITORY_ROOT / PREDICTION_RELATIVE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    predict = commands.add_parser("predict")
    predict.add_argument("--partition", choices=("development", "holdout"), required=True)
    score = commands.add_parser("score")
    score.add_argument("--partition", choices=("development", "holdout"), required=True)
    score.add_argument("--result", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "predict":
        predict_partition(spec_path=args.spec.resolve(), partition=args.partition, source_root=args.source_root.resolve(), target_root=args.target_root.resolve(), prediction_dir=args.prediction_dir.resolve())
        return
    result = args.result or (REPOSITORY_ROOT / RESULT_RELATIVE_DIR / f"regime-hindcast-v1-{args.partition}.json")
    score_partition(spec_path=args.spec.resolve(), partition=args.partition, source_root=args.source_root.resolve(), target_root=args.target_root.resolve(), prediction_dir=args.prediction_dir.resolve(), result_path=result.resolve())


if __name__ == "__main__":
    main()
