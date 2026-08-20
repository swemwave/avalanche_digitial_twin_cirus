"""Run the frozen SPOT 2018/2019 blind storm-window hindcast.

Prediction and scoring are deliberately separate commands. ``predict`` never
opens an avalanche outline. It derives terrain, loads the frozen historical
forcing, lets AvyCore select release zones, and routes only those predicted
zones. ``score`` can run only after a prediction artifact with the exact frozen
experiment hash exists; only then does it open the SPOT outlines as targets.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from scipy import sparse
from shapely import make_valid
from shapely.geometry import box, mapping

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SOURCE = REPOSITORY_ROOT / "packages" / "avycore" / "src"
BACKEND_SOURCE = REPOSITORY_ROOT / "backend"
for source in (AVYCORE_SOURCE, BACKEND_SOURCE):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from app.assess import (  # noqa: E402
    MAX_ADVANCED_ZONES,
    MAX_SIM_ZONES,
    MODEL_VERSION,
    _config,
    assessment_model_identity,
)
from app.processing.terrain import derivatives as terrain_derivatives  # noqa: E402
from avycore.hazard import risk, runout  # noqa: E402
from avycore.hazard.conditions import Conditions  # noqa: E402
from avycore.validation import (  # noqa: E402
    mountain_block_bootstrap_interval,
    storm_window_positive_metrics,
)

SPEC_RELATIVE_PATH = Path("validation-data/experiments/spot-blind-swiss-v1.json")
PREDICTION_RELATIVE_DIR = Path("validation-data/predictions/spot-blind-swiss-v1")
RESULT_RELATIVE_DIR = Path("validation-data/results")
RUNNER_RELATIVE_PATH = Path("scripts/validation/run_spot_blind_hindcast.py")
RISK_SOURCE_RELATIVE_PATH = Path("packages/avycore/src/avycore/hazard/risk.py")
RUNOUT_SOURCE_RELATIVE_PATH = Path("packages/avycore/src/avycore/hazard/runout.py")


@dataclass(frozen=True)
class ExperimentGrid:
    west: float
    south: float
    east: float
    north: float
    resolution_m: float
    crs_string: str = "EPSG:2056"

    @property
    def width(self) -> int:
        return int(round((self.east - self.west) / self.resolution_m))

    @property
    def height(self) -> int:
        return int(round((self.north - self.south) / self.resolution_m))

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def transform(self) -> rasterio.Affine:
        return from_origin(
            self.west, self.north, self.resolution_m, self.resolution_m
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExperimentGrid":
        west, south, east, north = (float(item) for item in value["bounds"])
        grid = cls(
            west=west,
            south=south,
            east=east,
            north=north,
            resolution_m=float(value["resolution_m"]),
            crs_string=str(value["crs"]),
        )
        if list(grid.shape) != list(value["shape"]):
            raise ValueError("Frozen grid bounds, resolution, and shape disagree.")
        return grid


class ExperimentTerrain:
    def __init__(
        self,
        grid: ExperimentGrid,
        layers: dict[str, np.ma.MaskedArray],
    ) -> None:
        self.grid = grid
        self._layers = layers
        transformer = Transformer.from_crs(grid.crs_string, "EPSG:4326", always_xy=True)

        def project(cols: Any, rows: Any) -> tuple[Any, Any]:
            x = grid.west + np.asarray(cols) * grid.resolution_m
            y = grid.north - np.asarray(rows) * grid.resolution_m
            return transformer.transform(x, y)

        self.reproject = project

    def layer(self, name: str) -> np.ma.MaskedArray:
        return self._layers[name]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _bool_mask_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array, dtype=np.bool_)
    header = json.dumps(
        {"format": "packed-bool-little-v1", "shape": list(contiguous.shape)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    digest.update(np.packbits(contiguous.reshape(-1), bitorder="little").tobytes())
    return digest.hexdigest()


def _masked_float32_sha256(array: np.ma.MaskedArray) -> str:
    masked = np.ma.asarray(array)
    values = np.ascontiguousarray(masked.filled(0.0), dtype="<f4")
    mask = np.ascontiguousarray(np.ma.getmaskarray(masked), dtype=np.bool_)
    header = json.dumps(
        {"format": "masked-float32-little-v1", "shape": list(values.shape)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    digest.update(values.tobytes(order="C"))
    digest.update(np.packbits(mask.reshape(-1), bitorder="little").tobytes())
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return value


def _source_path(
    item: dict[str, Any],
    *,
    source_root: Path,
    spot_root: Path,
) -> Path:
    root = {
        "source_root": source_root,
        "spot_root": spot_root,
        "repository_root": REPOSITORY_ROOT,
    }.get(item["root"])
    if root is None:
        raise ValueError(f"Unknown source root {item['root']!r}.")
    return root / Path(item["path"])


def _verify_sources(
    spec: dict[str, Any],
    *,
    source_root: Path,
    spot_root: Path,
    include_evaluation_targets: bool,
) -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for item in spec["source_inputs"]:
        if item["role"] == "evaluation_target" and not include_evaluation_targets:
            continue
        path = _source_path(item, source_root=source_root, spot_root=spot_root)
        if not path.is_file():
            raise FileNotFoundError(f"Frozen source is missing: {path}")
        if path.stat().st_size != int(item["bytes"]):
            raise ValueError(f"Frozen byte size changed for {item['id']!r}.")
        actual = _sha256_file(path)
        if actual != item["sha256"]:
            raise ValueError(
                f"Frozen SHA-256 changed for {item['id']!r}: {actual}"
            )
        resolved[item["id"]] = path
    return resolved


def _verify_model_identity(spec: dict[str, Any]) -> None:
    identity = spec["model_identity"]
    runtime = assessment_model_identity()
    if runtime["sha256"] != identity["parameter_manifest_sha256"]:
        raise ValueError("The production parameter manifest changed after experiment freeze.")
    for key, relative in (
        ("risk_source_sha256", RISK_SOURCE_RELATIVE_PATH),
        ("runout_source_sha256", RUNOUT_SOURCE_RELATIVE_PATH),
        ("runner_source_sha256", RUNNER_RELATIVE_PATH),
    ):
        actual = _sha256_file(REPOSITORY_ROOT / relative)
        if actual != identity[key]:
            raise ValueError(f"Frozen model identity mismatch for {relative.as_posix()}.")


def _block_by_id(spec: dict[str, Any], block_id: str) -> dict[str, Any]:
    blocks = [
        block
        for partition in spec["partitions"].values()
        for block in partition["blocks"]
        if block["block_id"] == block_id
    ]
    if len(blocks) != 1:
        raise ValueError(f"Expected exactly one frozen block named {block_id!r}.")
    return blocks[0]


def _partition_blocks(spec: dict[str, Any], partition: str) -> list[dict[str, Any]]:
    if partition not in {"development", "holdout"}:
        raise ValueError("partition must be 'development' or 'holdout'.")
    return list(spec["partitions"][partition]["blocks"])


def _read_mosaic(paths: Sequence[Path], grid: ExperimentGrid) -> np.ma.MaskedArray:
    destination = np.full(grid.shape, np.nan, dtype="float32")
    for path in paths:
        temporary = np.full(grid.shape, np.nan, dtype="float32")
        with rasterio.open(path) as source:
            reproject(
                source=rasterio.band(source, 1),
                destination=temporary,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source.nodata,
                dst_transform=grid.transform,
                dst_crs=grid.crs_string,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
                num_threads=1,
            )
        fill = ~np.isfinite(destination) & np.isfinite(temporary)
        destination[fill] = temporary[fill]
    return np.ma.array(destination, mask=~np.isfinite(destination), copy=False)


def _read_forest(path: Path, grid: ExperimentGrid) -> tuple[np.ma.MaskedArray, np.ndarray]:
    classes = np.zeros(grid.shape, dtype="uint8")
    with rasterio.open(path) as source:
        reproject(
            source=rasterio.band(source, 1),
            destination=classes,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs_string,
            dst_nodata=0,
            resampling=Resampling.nearest,
            num_threads=1,
        )
    valid = classes != 0
    forest = np.zeros(grid.shape, dtype="float32")
    forest[np.isin(classes, (40, 50, 60, 70, 90, 100, 160, 170))] = 1.0
    forest[classes == 110] = 0.6
    forest[classes == 120] = 0.3
    return np.ma.array(forest, mask=~valid), classes


def _terrain(
    block: dict[str, Any],
    spec: dict[str, Any],
    sources: dict[str, Path],
) -> tuple[ExperimentTerrain, dict[str, Any], tuple[slice, slice]]:
    grid = ExperimentGrid.from_dict(block["simulation_grid"])
    dem_paths = [sources[item] for item in block["dem_source_ids"]]
    elevation = _read_mosaic(dem_paths, grid)
    slope, aspect = terrain_derivatives.slope_aspect(elevation, grid)
    curvatures = terrain_derivatives.curvatures(elevation, grid)
    forest, landcover_classes = _read_forest(
        sources[spec["landcover"]["source_id"]], grid
    )
    layers = {
        "elevation": elevation,
        "slope": slope,
        "aspect": aspect,
        "general_curvature": curvatures["general_curvature"],
        "plan_curvature": curvatures["plan_curvature"],
        "forest_mask": forest,
    }
    terrain = ExperimentTerrain(grid, layers)
    combined_valid = np.logical_and.reduce(
        [~np.ma.getmaskarray(layer) for layer in layers.values()]
    )
    row_start, row_stop, col_start, col_stop = (
        int(value) for value in block["core_window_rowcol"]
    )
    core_window = (slice(row_start, row_stop), slice(col_start, col_stop))
    core_valid = combined_valid[core_window]
    metadata = {
        "grid": block["simulation_grid"],
        "core_grid": block["core_grid"],
        "layer_sha256": {
            name: _masked_float32_sha256(layer) for name, layer in layers.items()
        },
        "combined_valid_mask_sha256": _bool_mask_sha256(combined_valid),
        "simulation_complete_input_fraction": float(combined_valid.mean()),
        "core_complete_input_fraction": float(core_valid.mean()),
        "landcover_class_counts": {
            str(int(value)): int(count)
            for value, count in zip(*np.unique(landcover_classes, return_counts=True))
        },
    }
    metadata["terrain_artifact_sha256"] = _canonical_sha256(metadata)
    return terrain, metadata, core_window


def _aggregate_weather(
    block: dict[str, Any],
    path: Path,
) -> tuple[Conditions, dict[str, Any]]:
    payload = json.loads(path.read_bytes())
    points = payload if isinstance(payload, list) else [payload]
    start = str(block["storm_window"]["start_utc"])
    end = str(block["storm_window"]["end_utc"])
    snowfall_by_point: list[float] = []
    precipitation_by_point: list[float] = []
    temperatures: list[float] = []
    eastward: list[float] = []
    northward: list[float] = []
    scalar_speeds: list[float] = []
    returned_points: list[dict[str, float]] = []
    for point in points:
        hourly = point["hourly"]
        selected = [
            index
            for index, timestamp in enumerate(hourly["time"])
            if start < timestamp <= end
        ]
        if len(selected) != int(block["storm_window"]["hour_count"]):
            raise ValueError(
                f"{block['block_id']} has {len(selected)} forcing hours, not the frozen count."
            )
        required = (
            "temperature_2m",
            "precipitation",
            "snowfall",
            "wind_speed_10m",
            "wind_direction_10m",
        )
        if any(hourly[name][index] is None for name in required for index in selected):
            raise ValueError(
                f"{block['block_id']} contains null required meteorological cells."
            )
        snowfall_by_point.append(
            float(sum(float(hourly["snowfall"][index]) for index in selected))
        )
        precipitation_by_point.append(
            float(sum(float(hourly["precipitation"][index]) for index in selected))
        )
        for index in selected:
            temperatures.append(float(hourly["temperature_2m"][index]))
            speed = float(hourly["wind_speed_10m"][index])
            direction = math.radians(float(hourly["wind_direction_10m"][index]))
            scalar_speeds.append(speed)
            eastward.append(-speed * math.sin(direction))
            northward.append(-speed * math.cos(direction))
        returned_points.append(
            {
                "latitude": float(point["latitude"]),
                "longitude": float(point["longitude"]),
                "elevation_m": float(point["elevation"]),
            }
        )

    mean_eastward = float(np.mean(eastward))
    mean_northward = float(np.mean(northward))
    wind_from = (
        math.degrees(math.atan2(-mean_eastward, -mean_northward)) + 360.0
    ) % 360.0
    conditions = Conditions(
        new_snow_cm=float(np.mean(snowfall_by_point)),
        wind_speed_kmh=float(np.mean(scalar_speeds)),
        wind_direction_deg=wind_from,
        release_size=str(block["release_size"]),
        air_temperature_c=None,
        flow_regime=str(block["flow_regime"]),
    ).clamped()
    metadata = {
        "source_file_sha256": _sha256_file(path),
        "selection_interval": "start_exclusive_end_inclusive",
        "start_utc": start,
        "end_utc": end,
        "hour_count_per_point": int(block["storm_window"]["hour_count"]),
        "point_count": len(points),
        "returned_grid_points": returned_points,
        "snowfall_cm_by_point": snowfall_by_point,
        "new_snow_cm_spatial_mean": conditions.new_snow_cm,
        "precipitation_mm_spatial_mean": float(np.mean(precipitation_by_point)),
        "temperature_c_mean": float(np.mean(temperatures)),
        "temperature_c_minimum": float(np.min(temperatures)),
        "temperature_c_maximum": float(np.max(temperatures)),
        "wind_speed_kmh_scalar_mean": conditions.wind_speed_kmh,
        "wind_vector_mean_speed_kmh": float(math.hypot(mean_eastward, mean_northward)),
        "wind_from_direction_deg_from_vector_mean": conditions.wind_direction_deg,
        "model_temperature_input": None,
        "model_temperature_input_reason": (
            "Open-Meteo snowfall is already phase-classified. Passing mean temperature to "
            "Conditions would apply the model's rain/snow classifier a second time."
        ),
        "meteorological_wind_convention": "from_direction_degrees_clockwise_from_north",
    }
    return conditions, metadata


def _touches_boundary(mask: np.ndarray) -> bool:
    return bool(
        mask.any()
        and (
            np.any(mask[0])
            or np.any(mask[-1])
            or np.any(mask[:, 0])
            or np.any(mask[:, -1])
        )
    )


def _simulate_engine(
    engine: Any,
    *,
    zones: Sequence[Any],
    cap: int,
    terrain: ExperimentTerrain,
    conditions: Conditions,
    seed: int | None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    selected = sorted(
        zones,
        key=lambda zone: zone.properties["estimated_release_score"],
        reverse=True,
    )[:cap]
    reached = np.zeros(terrain.grid.shape, dtype=bool)
    uncertainty = np.zeros(terrain.grid.shape, dtype=bool)
    particles_left = 0
    zone_summaries: list[dict[str, Any]] = []
    for zone in selected:
        result = engine.simulate(
            zone=zone,
            grid=terrain.grid,
            elevation=terrain.layer("elevation"),
            slope=terrain.layer("slope"),
            forest_mask=terrain.layer("forest_mask"),
            plan_curvature=terrain.layer("plan_curvature"),
            config=_config(),
            release_size=conditions.release_size,
            seed=seed,
            alpha_override_deg=conditions.alpha_angle_override_deg,
            flow_regime=conditions.flow_regime,
        )
        reached |= result.reached
        uncertainty |= result.uncertainty
        particles_left += int(result.metadata.get("particles_left_the_aoi", 0))
        zone_summaries.append(
            {
                "zone_id": result.zone_id,
                "engine_mode": result.metadata.get("engine_mode"),
                "reached_cells": int(np.count_nonzero(result.reached)),
                "uncertainty_cells": int(np.count_nonzero(result.uncertainty)),
                "particles_left_the_aoi": int(
                    result.metadata.get("particles_left_the_aoi", 0)
                ),
                "runout_length_m": result.metadata.get("runout_length_m"),
                "horizontal_reach_m": result.metadata.get("horizontal_reach_m"),
                "alpha_angle_expected_deg": result.metadata.get(
                    "alpha_angle_expected_deg",
                    result.metadata.get("alpha_angle_deg"),
                ),
                "alpha_envelope_deg": result.metadata.get("alpha_envelope_deg"),
            }
        )
    return reached, uncertainty, {
        "engine": engine.name,
        "engine_mode": engine.engine_mode,
        "zone_cap": cap,
        "available_release_zone_count": len(zones),
        "simulated_release_zone_count": len(selected),
        "particles_left_the_aoi": particles_left,
        "reached_simulation_boundary": _touches_boundary(reached),
        "uncertainty_reached_simulation_boundary": _touches_boundary(uncertainty),
        "zones": zone_summaries,
    }


def _predict_block(
    block: dict[str, Any],
    spec: dict[str, Any],
    sources: dict[str, Path],
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    terrain, terrain_meta, core_window = _terrain(block, spec, sources)
    conditions, weather_meta = _aggregate_weather(
        block, sources[block["meteorology_source_id"]]
    )
    # The fixed core is the prediction AOI; the surrounding terrain exists only
    # to let core-originating runout continue without an artificial edge. If
    # release extraction were allowed throughout the halo, production's zone cap
    # could be consumed by starts outside the evaluated domain and particles could
    # launch directly beside the outer boundary. Masking release support to the
    # target-independent core reproduces an AOI assessment while retaining the
    # larger elevation surface solely for routing.
    release_support = np.zeros(terrain.grid.shape, dtype=bool)
    release_support[core_window] = True
    risk_field = risk.compute_release(
        terrain, conditions, condition_coverage=release_support
    )
    zone_set = risk.extract_release_zones(terrain, risk_field, conditions)
    release = np.zeros(terrain.grid.shape, dtype=bool)
    for zone in zone_set.zones:
        release |= zone.pixels

    seed = int(spec["fixed_model"]["random_seed"])
    hybrid, hybrid_uncertainty, hybrid_meta = _simulate_engine(
        runout.ParticleRunoutEngine(runout.HYBRID),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["hybrid_zone_cap"]),
        terrain=terrain,
        conditions=conditions,
        seed=seed,
    )
    alpha, alpha_uncertainty, alpha_meta = _simulate_engine(
        runout.FastRunoutEngine(),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["alpha_only_zone_cap"]),
        terrain=terrain,
        conditions=conditions,
        seed=None,
    )
    dynamics, dynamics_uncertainty, dynamics_meta = _simulate_engine(
        runout.ParticleRunoutEngine(runout.DYNAMICS_ONLY),
        zones=zone_set.zones,
        cap=int(spec["fixed_model"]["dynamics_only_zone_cap"]),
        terrain=terrain,
        conditions=conditions,
        seed=seed,
    )
    eligible = np.logical_and.reduce(
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
    slope_values = np.asarray(terrain.layer("slope").filled(np.nan), dtype="float32")
    arrays = {
        "eligible": np.asarray(eligible[core_window], dtype=bool),
        "slope_deg": np.asarray(slope_values[core_window], dtype="float32"),
        "release": np.asarray(release[core_window], dtype=bool),
        "hybrid_runout": np.asarray(hybrid[core_window], dtype=bool),
        "hybrid_uncertainty": np.asarray(hybrid_uncertainty[core_window], dtype=bool),
        "alpha_only_runout": np.asarray(alpha[core_window], dtype=bool),
        "alpha_only_uncertainty": np.asarray(
            alpha_uncertainty[core_window], dtype=bool
        ),
        "dynamics_only_runout": np.asarray(dynamics[core_window], dtype=bool),
        "dynamics_only_uncertainty": np.asarray(
            dynamics_uncertainty[core_window], dtype=bool
        ),
    }
    metadata = {
        "schema": "avycore-spot-blind-prediction-metadata-v1",
        "block_id": block["block_id"],
        "model_version": MODEL_VERSION,
        "parameter_manifest_sha256": assessment_model_identity()["sha256"],
        "conditions": conditions.to_dict(),
        "weather": weather_meta,
        "terrain": terrain_meta,
        "release": {
            "zone_count": len(zone_set.zones),
            "release_cell_count_simulation_grid": int(np.count_nonzero(release)),
            "release_cell_count_core": int(np.count_nonzero(arrays["release"])),
            "warnings": list(zone_set.warnings),
            "explanation": zone_set.explanation,
        },
        "engines": {
            "hybrid": hybrid_meta,
            "alpha_only": alpha_meta,
            "dynamics_only": dynamics_meta,
        },
        "mask_sha256": {
            name: _bool_mask_sha256(value)
            for name, value in arrays.items()
            if value.dtype == np.bool_
        },
        "held_out_outlines_opened": False,
        "held_out_outlines_used_as_model_inputs": False,
        "prediction_generation_order": (
            "terrain_and_forcing_to_release_scoring_to_predicted_release_zones_to_runout"
        ),
    }
    metadata["prediction_identity_sha256"] = _canonical_sha256(metadata)
    return arrays, metadata


def _write_prediction(
    output: Path,
    arrays: dict[str, np.ndarray],
    metadata: dict[str, Any],
    spec_sha256: str,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {**metadata, "experiment_spec_sha256": spec_sha256}
    np.savez_compressed(
        output,
        **arrays,
        metadata_json=np.asarray(
            json.dumps(metadata, sort_keys=True, separators=(",", ":"))
        ),
    )


def predict_partition(
    *,
    spec_path: Path,
    partition: str,
    source_root: Path,
    spot_root: Path,
    prediction_dir: Path,
) -> None:
    spec = _load_json(spec_path)
    _verify_model_identity(spec)
    sources = _verify_sources(
        spec,
        source_root=source_root,
        spot_root=spot_root,
        include_evaluation_targets=False,
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
                    "conditions": metadata["conditions"],
                    "held_out_outlines_opened": False,
                },
                sort_keys=True,
            )
        )


def _load_prediction(
    path: Path, *, spec_sha256: str
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    with np.load(path, allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"].item()))
        arrays = {
            name: np.asarray(archive[name])
            for name in archive.files
            if name != "metadata_json"
        }
    if metadata["experiment_spec_sha256"] != spec_sha256:
        raise ValueError(f"Prediction {path.name} was not generated from the frozen spec.")
    for name, expected in metadata["mask_sha256"].items():
        if _bool_mask_sha256(arrays[name]) != expected:
            raise ValueError(f"Prediction mask {name!r} failed integrity verification.")
    return arrays, metadata


def _polygon_part(geometry: Any) -> Any:
    repaired = make_valid(geometry)
    if repaired.geom_type in {"Polygon", "MultiPolygon"}:
        return repaired
    polygons = [
        item
        for item in getattr(repaired, "geoms", ())
        if item.geom_type in {"Polygon", "MultiPolygon"}
    ]
    if not polygons:
        return None
    from shapely import union_all

    return union_all(polygons)


def _target_events(
    shapefile: Path,
    block: dict[str, Any],
) -> tuple[list[np.ndarray], list[str], list[bool], list[dict[str, Any]], dict[str, Any]]:
    # Lazy import keeps the prediction command free of any outline reader.
    import geopandas as gpd

    frame = gpd.read_file(shapefile)
    if str(frame.crs).upper() != "EPSG:2056":
        frame = frame.to_crs("EPSG:2056")
    grid = ExperimentGrid.from_dict(block["core_grid"])
    core = box(grid.west, grid.south, grid.east, grid.north)
    candidates = frame.iloc[list(frame.sindex.query(core, predicate="intersects"))]
    masks: list[np.ndarray] = []
    ids: list[str] = []
    complete: list[bool] = []
    attributes: list[dict[str, Any]] = []
    repaired_count = 0
    skipped_nonpolygon_count = 0
    for index, row in candidates.sort_index().iterrows():
        original = row.geometry
        geometry = _polygon_part(original)
        if geometry is None or geometry.is_empty:
            skipped_nonpolygon_count += 1
            continue
        if not original.is_valid:
            repaired_count += 1
        if not core.covers(geometry.centroid):
            continue
        event_id = f"{block['campaign_year']}-{int(index):05d}"
        mask = rasterize(
            [(mapping(geometry), 1)],
            out_shape=grid.shape,
            transform=grid.transform,
            fill=0,
            all_touched=True,
            dtype="uint8",
        ).astype(bool)
        if not mask.any():
            raise ValueError(f"Mapped event {event_id} produced no all-touched grid cells.")
        ids.append(event_id)
        masks.append(mask)
        complete.append(bool(core.covers(geometry)))
        attributes.append(
            {
                "event_id": event_id,
                "source_row_index": int(index),
                "type": str(row.get("typ", "UNKNOWN")),
                "trigger": str(row.get("trg_typ", "UNKNOWN")),
                "size": None if row.get("sze") is None else str(row.get("sze")),
                "mapping_shape_quality": (
                    None
                    if row.get("aval_shape") is None
                    else str(row.get("aval_shape"))
                ),
                "geometry_complete_within_core": complete[-1],
            }
        )
    return masks, ids, complete, attributes, {
        "source_feature_count": int(len(frame)),
        "candidate_intersection_count": int(len(candidates)),
        "centroid_assigned_event_count": len(ids),
        "invalid_geometry_repaired_count": repaired_count,
        "nonpolygon_geometry_skipped_count": skipped_nonpolygon_count,
        "assignment_rule": "geometry_centroid_covered_by_fixed_core",
        "boundary_rule": (
            "Centroid-assigned outlines crossing the fixed core remain in the event "
            "denominator and are forced uncaptured."
        ),
        "rasterization": "rasterio.features.rasterize all_touched=True",
    }


def _production_slope_score(slope_deg: np.ndarray) -> np.ndarray:
    manifest = assessment_model_identity()["parameter_manifest"]["release"]
    return np.interp(
        slope_deg,
        np.asarray(manifest["slope_breakpoints_deg"], dtype="float64"),
        np.asarray(manifest["slope_scores"], dtype="float64"),
    )


def _slope_baseline(
    slope_deg: np.ndarray,
    eligible: np.ndarray,
    predicted_cell_count: int,
) -> np.ndarray:
    eligible_indices = np.flatnonzero(eligible.reshape(-1))
    scores = _production_slope_score(slope_deg.reshape(-1)[eligible_indices])
    order = np.argsort(-scores, kind="stable")
    selected = eligible_indices[order[:predicted_cell_count]]
    result = np.zeros(eligible.size, dtype=bool)
    result[selected] = True
    return result.reshape(eligible.shape)


def _random_baseline(
    *,
    event_masks: Sequence[np.ndarray],
    geometry_complete: Sequence[bool],
    eligible: np.ndarray,
    predicted_cell_count: int,
    capture_threshold: float,
    replicate_count: int,
    seed: int,
) -> dict[str, Any]:
    eligible_flat = np.flatnonzero(eligible.reshape(-1))
    eligible_count = len(eligible_flat)
    position = np.full(eligible.size, -1, dtype="int32")
    position[eligible_flat] = np.arange(eligible_count, dtype="int32")
    event_rows: list[int] = []
    event_cols: list[int] = []
    event_counts: list[int] = []
    complete_inputs: list[bool] = []
    for row_index, event in enumerate(event_masks):
        flat = np.flatnonzero(event.reshape(-1))
        positions = position[flat]
        known = positions >= 0
        event_rows.extend([row_index] * int(np.count_nonzero(known)))
        event_cols.extend(int(value) for value in positions[known])
        event_counts.append(len(flat))
        complete_inputs.append(bool(np.all(known)))
    membership = sparse.csr_matrix(
        (
            np.ones(len(event_rows), dtype="uint8"),
            (event_rows, event_cols),
        ),
        shape=(len(event_masks), eligible_count),
    )
    mapped_union = np.logical_or.reduce(event_masks)
    mapped_count = int(np.count_nonzero(mapped_union))
    union_positions = position[np.flatnonzero(mapped_union.reshape(-1))]
    union_positions = union_positions[union_positions >= 0]
    required = np.ceil(capture_threshold * np.asarray(event_counts)).astype(int)
    event_scorable = np.asarray(geometry_complete, dtype=bool) & np.asarray(
        complete_inputs, dtype=bool
    )
    rng = np.random.default_rng(seed)
    capture_rates: list[float] = []
    coverage_rates: list[float] = []
    batch_size = 32
    for start in range(0, replicate_count, batch_size):
        batch = min(batch_size, replicate_count - start)
        selections = np.zeros((eligible_count, batch), dtype="uint8")
        for column in range(batch):
            chosen = rng.choice(
                eligible_count, size=predicted_cell_count, replace=False
            )
            selections[chosen, column] = 1
        overlaps = np.asarray(membership @ selections)
        captures = (overlaps >= required[:, None]) & event_scorable[:, None]
        capture_rates.extend(np.mean(captures, axis=0).astype(float))
        coverage_rates.extend(
            (
                np.sum(selections[union_positions, :], axis=0, dtype="int64")
                / mapped_count
            ).astype(float)
        )
    capture_array = np.asarray(capture_rates)
    coverage_array = np.asarray(coverage_rates)
    return {
        "method": "seeded_uniform_without_replacement_at_model_terrain_area_budget",
        "random_seed": seed,
        "replicate_count": replicate_count,
        "predicted_cell_count_per_replicate": predicted_cell_count,
        "event_capture_fraction_mean": float(capture_array.mean()),
        "event_capture_fraction_2_5_percentile": float(
            np.quantile(capture_array, 0.025)
        ),
        "event_capture_fraction_97_5_percentile": float(
            np.quantile(capture_array, 0.975)
        ),
        "mapped_positive_footprint_coverage_fraction_mean": float(
            coverage_array.mean()
        ),
        "mapped_positive_footprint_coverage_fraction_97_5_percentile": float(
            np.quantile(coverage_array, 0.975)
        ),
    }


def _stratified_capture(
    event_scores: Sequence[dict[str, Any]],
    attributes: Sequence[dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    score_by_id = {item["event_id"]: item for item in event_scores}
    result: dict[str, Any] = {}
    for attribute in attributes:
        label = str(attribute.get(key, "UNKNOWN"))
        score = score_by_id[attribute["event_id"]]
        group = result.setdefault(label, {"event_count": 0, "captured_event_count": 0})
        group["event_count"] += 1
        group["captured_event_count"] += int(score["captured"])
    for group in result.values():
        group["event_capture_fraction"] = (
            group["captured_event_count"] / group["event_count"]
        )
    return result


def _score_block(
    *,
    block: dict[str, Any],
    arrays: dict[str, np.ndarray],
    prediction_metadata: dict[str, Any],
    prediction_path: Path,
    shapefile: Path,
    spec: dict[str, Any],
) -> dict[str, Any]:
    event_masks, event_ids, geometry_complete, attributes, target_meta = _target_events(
        shapefile, block
    )
    if not event_masks:
        raise ValueError(f"{block['block_id']} contains no centroid-assigned mapped events.")
    eligible = np.asarray(arrays["eligible"], dtype=bool)
    release = np.asarray(arrays["release"], dtype=bool)
    hybrid_runout = np.asarray(arrays["hybrid_runout"], dtype=bool)
    capture_threshold = float(spec["metrics"]["event_capture_minimum_overlap_fraction"])
    cell_area = float(block["core_grid"]["resolution_m"]) ** 2

    masks = {
        "release_only": release,
        "routed_nonrelease_only": hybrid_runout & ~release,
        "hybrid_end_to_end": release | hybrid_runout,
        "alpha_only_end_to_end": release
        | np.asarray(arrays["alpha_only_runout"], dtype=bool),
        "dynamics_only_end_to_end": release
        | np.asarray(arrays["dynamics_only_runout"], dtype=bool),
    }
    metrics = {
        name: storm_window_positive_metrics(
            mask,
            eligible=eligible,
            event_masks=event_masks,
            event_ids=event_ids,
            geometry_complete=geometry_complete,
            capture_minimum_overlap_fraction=capture_threshold,
            cell_area_m2=cell_area,
        ).to_dict()
        for name, mask in masks.items()
    }
    primary = metrics["hybrid_end_to_end"]
    predicted_count = int(primary["predicted_eligible_cell_count"])
    slope_mask = _slope_baseline(
        np.asarray(arrays["slope_deg"], dtype="float32"),
        eligible,
        predicted_count,
    )
    slope_metrics = storm_window_positive_metrics(
        slope_mask,
        eligible=eligible,
        event_masks=event_masks,
        event_ids=event_ids,
        geometry_complete=geometry_complete,
        capture_minimum_overlap_fraction=capture_threshold,
        cell_area_m2=cell_area,
    ).to_dict()
    random_seed = int(spec["baselines"]["random_seed_by_block"][block["block_id"]])
    random_metrics = _random_baseline(
        event_masks=event_masks,
        geometry_complete=geometry_complete,
        eligible=eligible,
        predicted_cell_count=predicted_count,
        capture_threshold=capture_threshold,
        replicate_count=int(spec["baselines"]["random_replicate_count"]),
        seed=random_seed,
    )
    engine = prediction_metadata["engines"]["hybrid"]
    incomplete = (
        primary["incomplete_input_event_count"] > 0
        or prediction_metadata["terrain"]["core_complete_input_fraction"] < 1.0
    )
    domain_escape = bool(
        engine["particles_left_the_aoi"]
        or engine["reached_simulation_boundary"]
        or engine["uncertainty_reached_simulation_boundary"]
    )
    acceptance = spec["acceptance_rule"]
    checks = {
        "minimum_events": primary["event_count"]
        >= int(acceptance["minimum_mapped_events_per_group"]),
        "event_capture": primary["event_capture_fraction"]
        >= float(acceptance["minimum_event_capture_fraction"]),
        "terrain_budget": primary["flagged_eligible_terrain_fraction"]
        <= float(acceptance["maximum_flagged_eligible_terrain_fraction"]),
        "exceeds_slope_only": primary["event_capture_fraction"]
        > slope_metrics["event_capture_fraction"],
        "exceeds_random_97_5_percentile": primary["event_capture_fraction"]
        > random_metrics["event_capture_fraction_97_5_percentile"],
        "complete_inputs": not incomplete,
        "no_domain_escape": not domain_escape,
    }
    primary_scores = primary["event_scores"]
    return {
        "block_id": block["block_id"],
        "mountain_group": block["mountain_group"],
        "campaign_year": block["campaign_year"],
        "partition": block["partition"],
        "prediction_artifact_path": prediction_path.relative_to(
            REPOSITORY_ROOT
        ).as_posix(),
        "prediction_artifact_sha256": _sha256_file(prediction_path),
        "prediction_identity_sha256": prediction_metadata[
            "prediction_identity_sha256"
        ],
        "target_selection": target_meta,
        "metrics": metrics,
        "baselines": {
            "slope_only_same_area_budget": slope_metrics,
            "seeded_random_same_area_budget": random_metrics,
        },
        "stratified_primary_capture": {
            "type": _stratified_capture(primary_scores, attributes, "type"),
            "mapping_shape_quality": _stratified_capture(
                primary_scores, attributes, "mapping_shape_quality"
            ),
        },
        "domain_escape": domain_escape,
        "incomplete_inputs": incomplete,
        "acceptance_checks": checks,
        "acceptance_passed": all(checks.values()),
        "prediction_summary": prediction_metadata,
    }


def _aggregate_holdout(
    blocks: Sequence[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    primary = [block["metrics"]["hybrid_end_to_end"] for block in blocks]
    bootstrap = spec["metrics"]["mountain_block_bootstrap"]
    kwargs = {
        "confidence_level": float(bootstrap["confidence_level"]),
        "replicate_count": int(bootstrap["replicate_count"]),
        "random_seed": int(bootstrap["random_seed"]),
    }
    capture = mountain_block_bootstrap_interval(
        [item["captured_event_count"] for item in primary],
        [item["event_count"] for item in primary],
        **kwargs,
    ).to_dict()
    footprint = mountain_block_bootstrap_interval(
        [item["intersecting_mapped_positive_cell_count"] for item in primary],
        [item["mapped_positive_union_cell_count"] for item in primary],
        **kwargs,
    ).to_dict()
    budget = mountain_block_bootstrap_interval(
        [item["predicted_eligible_cell_count"] for item in primary],
        [item["eligible_terrain_cell_count"] for item in primary],
        **kwargs,
    ).to_dict()
    qualifying = [
        block
        for block in blocks
        if block["metrics"]["hybrid_end_to_end"]["event_count"]
        >= int(spec["acceptance_rule"]["minimum_mapped_events_per_group"])
    ]
    all_predeclared_qualify = len(qualifying) == len(blocks)
    required_groups = int(spec["acceptance_rule"]["minimum_qualifying_group_count"])
    passed = bool(
        all_predeclared_qualify
        and len(qualifying) >= required_groups
        and all(block["acceptance_passed"] for block in qualifying)
    )
    return {
        "predeclared_group_count": len(blocks),
        "qualifying_group_count": len(qualifying),
        "all_predeclared_groups_qualified": all_predeclared_qualify,
        "mapped_event_count": sum(item["event_count"] for item in primary),
        "captured_event_count": sum(item["captured_event_count"] for item in primary),
        "event_capture_fraction": capture["estimate"],
        "mapped_positive_footprint_coverage_fraction": footprint["estimate"],
        "flagged_eligible_terrain_fraction": budget["estimate"],
        "mountain_block_bootstrap_intervals": {
            "event_capture_fraction": capture,
            "mapped_positive_footprint_coverage_fraction": footprint,
            "flagged_eligible_terrain_fraction": budget,
        },
        "acceptance_passed": passed,
    }


def score_partition(
    *,
    spec_path: Path,
    partition: str,
    source_root: Path,
    spot_root: Path,
    prediction_dir: Path,
    result_path: Path,
) -> None:
    spec = _load_json(spec_path)
    _verify_model_identity(spec)
    sources = _verify_sources(
        spec,
        source_root=source_root,
        spot_root=spot_root,
        include_evaluation_targets=True,
    )
    spec_sha256 = _sha256_file(spec_path)
    campaign = spec["partitions"][partition]
    shapefile = sources[campaign["outline_shapefile_source_id"]]
    block_results: list[dict[str, Any]] = []
    for block in _partition_blocks(spec, partition):
        prediction_path = prediction_dir / f"{block['block_id']}.npz"
        arrays, metadata = _load_prediction(
            prediction_path, spec_sha256=spec_sha256
        )
        if metadata["held_out_outlines_opened"] is not False:
            raise ValueError("Prediction metadata does not prove outline isolation.")
        block_result = _score_block(
            block=block,
            arrays=arrays,
            prediction_metadata=metadata,
            prediction_path=prediction_path,
            shapefile=shapefile,
            spec=spec,
        )
        block_results.append(block_result)
        print(
            json.dumps(
                {
                    "block_id": block["block_id"],
                    "event_count": block_result["metrics"]["hybrid_end_to_end"][
                        "event_count"
                    ],
                    "event_capture_fraction": block_result["metrics"][
                        "hybrid_end_to_end"
                    ]["event_capture_fraction"],
                    "flagged_eligible_terrain_fraction": block_result["metrics"][
                        "hybrid_end_to_end"
                    ]["flagged_eligible_terrain_fraction"],
                    "acceptance_passed": block_result["acceptance_passed"],
                },
                sort_keys=True,
            )
        )

    aggregate = (
        _aggregate_holdout(block_results, spec)
        if partition == "holdout"
        else None
    )
    result = {
        "schema": "avycore-spot-blind-swiss-hindcast-results-v1",
        "experiment_id": spec["experiment_id"],
        "experiment_spec_path": spec_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "experiment_spec_sha256": spec_sha256,
        "partition": partition,
        "prediction_generated_before_target_scoring": True,
        "held_out_outlines_used_as_model_inputs": False,
        "held_out_outlines_used_as_release_seeds": False,
        "negative_evidence_used": False,
        "parameters_changed_after_viewing_holdout_results": False,
        "model_parameters_tuned_in_this_experiment": False,
        "block_results": block_results,
        "aggregate": aggregate,
        "acceptance_rule": spec["acceptance_rule"],
        "acceptance_passed": (
            aggregate["acceptance_passed"]
            if aggregate is not None
            else all(block["acceptance_passed"] for block in block_results)
        ),
        "strict_field_validation": {
            "field_validation_holdout_n": 0,
            "is_validated": False,
            "trusted_dataset_identity_registry_modified": False,
            "reason": (
                "Positive-only satellite mapping lacks verified negatives and an exact "
                "machine-readable acquisition-footprint polygon; it is not eligible for the "
                "project's independently reviewed strict binary field-validation contract."
            ),
        },
        "software_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": {
                name: importlib.metadata.version(name)
                for name in (
                    "numpy",
                    "scipy",
                    "rasterio",
                    "geopandas",
                    "shapely",
                    "pyproj",
                    "xdem",
                )
            },
        },
        "source_inputs": spec["source_inputs"],
        "model_identity": spec["model_identity"],
        "emails_sent": False,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "result": str(result_path),
                "result_sha256": _sha256_file(result_path),
                "acceptance_passed": result["acceptance_passed"],
            },
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        default=REPOSITORY_ROOT / SPEC_RELATIVE_PATH,
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--spot-root", type=Path, required=True)
    parser.add_argument(
        "--prediction-dir",
        type=Path,
        default=REPOSITORY_ROOT / PREDICTION_RELATIVE_DIR,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    predict = subparsers.add_parser("predict")
    predict.add_argument("--partition", choices=("development", "holdout"), required=True)
    score = subparsers.add_parser("score")
    score.add_argument("--partition", choices=("development", "holdout"), required=True)
    score.add_argument("--result", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "predict":
        predict_partition(
            spec_path=args.spec.resolve(),
            partition=args.partition,
            source_root=args.source_root.resolve(),
            spot_root=args.spot_root.resolve(),
            prediction_dir=args.prediction_dir.resolve(),
        )
        return
    result = args.result
    if result is None:
        result = REPOSITORY_ROOT / RESULT_RELATIVE_DIR / (
            f"spot-blind-swiss-v1-{args.partition}.json"
        )
    score_partition(
        spec_path=args.spec.resolve(),
        partition=args.partition,
        source_root=args.source_root.resolve(),
        spot_root=args.spot_root.resolve(),
        prediction_dir=args.prediction_dir.resolve(),
        result_path=result.resolve(),
    )


if __name__ == "__main__":
    main()
