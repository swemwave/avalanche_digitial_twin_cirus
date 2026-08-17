"""Standalone AvaFrame 2.1 worker used only by the offline adapter.

This file intentionally imports neither ``app`` nor ``avycore``.  It is launched
with ``python -I`` so the selected AvaFrame environment, rather than the serving
environment, supplies the numerical and geospatial dependency closure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
import shapefile
from avaframe.com1DFA import com1DFA
from avaframe.in3Utils import cfgUtils
from rasterio.crs import CRS


SCHEMA_VERSION = "avycore-avaframe-worker-v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json(value) + b"\n")


def _read_request(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported or missing worker request schema_version.")
    return value


def _validate_grid(dataset: rasterio.io.DatasetReader, declared: dict[str, object]) -> None:
    if dataset.crs is None:
        raise ValueError("The DEM has no CRS.")
    declared_crs = CRS.from_user_input(declared["crs"]["definition"])
    if dataset.crs != declared_crs:
        raise ValueError(
            f"DEM CRS {dataset.crs!s} does not match declared CRS {declared_crs!s}."
        )
    if not bool(declared["crs"]["projected"]):
        raise ValueError("AvaFrame adapter requires a projected metre-based DEM.")
    if [dataset.height, dataset.width] != list(declared["shape"]):
        raise ValueError("DEM dimensions do not match the declared grid.")
    expected_transform = tuple(float(item) for item in declared["affine_transform"])
    actual_transform = tuple(dataset.transform)[:6]
    if not np.allclose(actual_transform, expected_transform, rtol=0.0, atol=1.0e-9):
        raise ValueError("DEM affine transform does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.a), float(declared["cell_size_x_m"]), abs_tol=1e-9):
        raise ValueError("DEM horizontal cell size does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.e), float(declared["cell_size_y_m"]), abs_tol=1e-9):
        raise ValueError("DEM vertical cell size does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.a), abs(dataset.transform.e), abs_tol=1e-9):
        raise ValueError("This AvaFrame slice requires square DEM cells.")


def _prepare_dem(request: dict[str, object], avalanche_dir: Path) -> tuple[dict[str, object], np.ndarray]:
    source = Path(str(request["terrain_dem_path"]))
    source_mask = np.load(Path(str(request["terrain_mask_path"])), allow_pickle=False)
    if source_mask.dtype != np.dtype("bool"):
        raise ValueError("The declared terrain mask must be a bool NPY array.")
    with rasterio.open(source) as dataset:
        declared_grid = request["terrain_grid"]
        if not isinstance(declared_grid, dict):
            raise ValueError("terrain_grid must be an object.")
        _validate_grid(dataset, declared_grid)
        dem = dataset.read(1, masked=True)
        if source_mask.shape != dem.shape:
            raise ValueError("The explicit terrain mask shape does not match the DEM.")
        source_invalid = np.ma.getmaskarray(dem) | ~np.isfinite(dem.filled(np.nan))
        if not np.array_equal(source_invalid, source_mask):
            raise ValueError("The explicit terrain mask does not match DEM nodata/non-finite cells.")
        profile = dataset.profile.copy()
        crs = dataset.crs
        transform = dataset.transform

    inputs = avalanche_dir / "Inputs"
    inputs.mkdir(parents=True, exist_ok=False)
    target = inputs / "dem.tif"
    profile.update(driver="GTiff", dtype="float32", count=1, nodata=-9999.0, compress="lzw")
    values = np.asarray(dem.filled(-9999.0), dtype=np.float32)
    with rasterio.open(target, "w", **profile) as output:
        output.write(values, 1)

    grid = {
        "crs": {
            "definition": crs.to_string(),
            "projected": bool(crs.is_projected),
            "horizontal_unit": "m",
            "coordinate_order": "x,y",
            "vertical_datum": declared_grid["crs"].get("vertical_datum"),
            "vertical_datum_status": declared_grid["crs"]["vertical_datum_status"],
        },
        "shape": [int(values.shape[0]), int(values.shape[1])],
        "affine_transform": [float(item) for item in tuple(transform)[:6]],
        "cell_size_x_m": float(abs(transform.a)),
        "cell_size_y_m": float(abs(transform.e)),
        "origin_semantics": "upper_left_outer_corner",
    }
    return grid, source_mask


def _polygon_parts(geometry: dict[str, object]) -> list[list[list[list[float]]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return [coordinates]
    if geometry_type == "MultiPolygon":
        return coordinates
    raise ValueError(f"Release geometry must be Polygon or MultiPolygon, got {geometry_type!r}.")


def _prepare_release(request: dict[str, object], avalanche_dir: Path, dem_crs: str) -> int:
    declared_crs = request["release_crs"]
    if not isinstance(declared_crs, dict):
        raise ValueError("release_crs must be an object.")
    if declared_crs.get("coordinate_order") != "x,y" or not declared_crs.get("projected"):
        raise ValueError("Release vectors require projected x,y coordinate order.")
    if CRS.from_user_input(str(declared_crs["definition"])) != CRS.from_user_input(dem_crs):
        raise ValueError("Release-vector CRS does not match the DEM CRS.")

    collection = json.loads(Path(str(request["release_geojson_path"])).read_text(encoding="utf-8"))
    if collection.get("type") != "FeatureCollection" or not collection.get("features"):
        raise ValueError("Release input must be a non-empty GeoJSON FeatureCollection.")
    release_dir = avalanche_dir / "Inputs" / "REL"
    release_dir.mkdir(parents=True, exist_ok=False)
    target = release_dir / "release.shp"
    thickness = float(request["parameters"]["release_thickness"])
    feature_number = 0
    with shapefile.Writer(str(target), shapeType=shapefile.POLYGON) as writer:
        writer.field("Name", "C", size=64)
        writer.field("thickness", "F", size=18, decimal=8)
        writer.field("ci95", "F", size=18, decimal=8)
        for feature in collection["features"]:
            geometry = feature.get("geometry")
            if not isinstance(geometry, dict):
                raise ValueError("Release feature is missing a geometry object.")
            for polygon in _polygon_parts(geometry):
                if not polygon or len(polygon[0]) < 4:
                    raise ValueError("Release polygons must contain a closed exterior ring.")
                parts = [
                    [[float(coordinate[0]), float(coordinate[1])] for coordinate in ring]
                    for ring in polygon
                ]
                writer.poly(parts)
                writer.record(f"release_{feature_number}", thickness, 0.0)
                feature_number += 1
    target.with_suffix(".prj").write_text(CRS.from_user_input(dem_crs).to_wkt(), encoding="utf-8")
    if feature_number == 0:
        raise ValueError("Release input contains no polygon parts.")
    return feature_number


def _configuration(avalanche_dir: Path, parameters: dict[str, object]) -> tuple[object, object, dict[str, object]]:
    cfg_main = cfgUtils.getGeneralConfig()
    cfg_main["MAIN"]["avalancheDir"] = str(avalanche_dir)
    cfg_main["MAIN"]["nCPU"] = "1"
    cfg_main["MAIN"]["CPUPercent"] = "100"
    for key in tuple(cfg_main["FLAGS"]):
        if key.lower() in {
            "showplot",
            "saveplot",
            "createreport",
            "showonlinebackground",
            "reportdir",
            "reportonefile",
            "debugplot",
        }:
            cfg_main["FLAGS"][key] = "False"

    cfg = cfgUtils.getModuleConfig(com1DFA, avalanche_dir, toPrint=False)
    general = cfg["GENERAL"]
    general["simTypeList"] = "null"
    general["resType"] = "ppr|pft|pfv"
    general["initPartDistType"] = "uniform"
    general["seed"] = str(int(parameters["seed"]))
    general["rho"] = repr(float(parameters["release_density"]))
    general["relThFromFile"] = "True"
    general["secRelArea"] = "False"
    general["timeDependentRelease"] = "False"
    general["dt"] = repr(float(parameters["time_step"]))
    general["tEnd"] = repr(float(parameters["simulation_time"]))
    general["sphKernelRadiusTimeStepping"] = "False"
    general["meshCellSize"] = repr(float(parameters["mesh_cell_size"]))
    general["cleanRemeshedRasters"] = "True"
    general["frictModel"] = "Voellmy"
    general["muvoellmy"] = repr(float(parameters["voellmy_mu"]))
    general["xsivoellmy"] = repr(float(parameters["voellmy_xi"]))
    cfg["EXPORTS"]["exportData"] = "True"

    main_manifest = {key: cfg_main["MAIN"][key] for key in sorted(cfg_main["MAIN"])}
    # The random staging path has no numerical meaning.  Recording it would make
    # byte-identical solver outputs appear non-replayable across output roots.
    main_manifest["avalancheDir"] = "{isolated_avalanche_dir}"
    manifest = {
        "general": {key: general[key] for key in sorted(general)},
        "exports": {key: cfg["EXPORTS"][key] for key in sorted(cfg["EXPORTS"])},
        "main": main_manifest,
        "flags": {key: cfg_main["FLAGS"][key] for key in sorted(cfg_main["FLAGS"])},
    }
    return cfg_main, cfg, manifest


def _environment_manifest() -> dict[str, object]:
    packages = sorted(
        (
            {
                "name": str(distribution.metadata.get("Name", "")).lower(),
                "version": str(distribution.version),
            }
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        ),
        key=lambda item: (item["name"], item["version"]),
    )
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "packages": packages,
    }


def _one_peak_file(peak_dir: Path, quantity: str) -> Path:
    candidates = sorted(peak_dir.glob(f"*_{quantity}.asc")) + sorted(peak_dir.glob(f"*_{quantity}.tif"))
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected exactly one {quantity!r} peak raster, found {len(candidates)}."
        )
    return candidates[0]


def _read_peak(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, object], object, object]:
    with rasterio.open(path) as source:
        field = source.read(1, masked=True)
        values = np.asarray(field.filled(0.0), dtype=np.float32)
        invalid = np.ma.getmaskarray(field) | ~np.isfinite(values)
        values[invalid] = 0.0
        profile = {
            "shape": [int(source.height), int(source.width)],
            "transform": [float(item) for item in tuple(source.transform)[:6]],
            "crs": source.crs.to_string() if source.crs else None,
            "cell_size_x_m": float(abs(source.transform.a)),
            "cell_size_y_m": float(abs(source.transform.e)),
        }
        return values, invalid, profile, source.transform, source.crs


def _normalize(
    avalanche_dir: Path,
    normalized_dir: Path,
    input_mask: np.ndarray,
    config: dict[str, object],
    environment: dict[str, object],
    feature_count: int,
) -> dict[str, object]:
    peak_dir = avalanche_dir / "Outputs" / "com1DFA" / "peakFiles"
    raw = {quantity: _read_peak(_one_peak_file(peak_dir, quantity)) for quantity in ("pft", "pfv", "ppr")}
    profiles = [item[2] for item in raw.values()]
    if any(profile != profiles[0] for profile in profiles[1:]):
        raise RuntimeError("AvaFrame peak rasters do not share one grid and CRS.")
    if tuple(profiles[0]["shape"]) != input_mask.shape:
        raise RuntimeError("AvaFrame output grid differs from the declared input-mask grid.")

    combined_mask = input_mask.copy()
    for item in raw.values():
        combined_mask |= item[1]
    depth, velocity, pressure = (raw[key][0] for key in ("pft", "pfv", "ppr"))
    for field in (depth, velocity, pressure):
        field[combined_mask] = 0.0
    runout = (depth > 0.0) & ~combined_mask

    normalized_dir.mkdir(parents=True, exist_ok=False)
    np.save(normalized_dir / "mask.npy", combined_mask, allow_pickle=False)
    np.save(normalized_dir / "runout.npy", runout, allow_pickle=False)
    np.save(normalized_dir / "depth.npy", depth, allow_pickle=False)
    np.save(normalized_dir / "velocity.npy", velocity, allow_pickle=False)
    np.save(normalized_dir / "pressure.npy", pressure, allow_pickle=False)

    shapes = []
    transform = raw["pft"][3]
    if np.any(runout):
        shapes = [
            {"type": "Feature", "properties": {}, "geometry": geometry}
            for geometry, value in rasterio.features.shapes(
                runout.astype(np.uint8), mask=runout, transform=transform
            )
            if int(value) == 1
        ]
    polygons = {"type": "FeatureCollection", "features": shapes}
    _write_json(normalized_dir / "runout.geojson", polygons)
    _write_json(normalized_dir / "configuration.json", config)
    _write_json(normalized_dir / "environment.json", environment)

    valid = ~combined_mask
    boundary_touched = bool(
        np.any(runout[0, :])
        or np.any(runout[-1, :])
        or np.any(runout[:, 0])
        or np.any(runout[:, -1])
    )
    cell_area = float(profiles[0]["cell_size_x_m"] * profiles[0]["cell_size_y_m"])
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": importlib.metadata.version("avaframe"),
        "configuration_sha256": _sha256(config),
        "environment_sha256": _sha256(environment),
        "grid": profiles[0],
        "valid_cells": int(np.count_nonzero(valid)),
        "masked_cells": int(np.count_nonzero(combined_mask)),
        "release_feature_count": feature_count,
        "runout_feature_count": len(shapes),
        "geometry_types": sorted({feature["geometry"]["type"] for feature in shapes}),
        "runout_area_m2": float(np.count_nonzero(runout) * cell_area),
        "boundary_touched": boundary_touched,
        "ranges": {
            "runout": [0.0, 1.0],
            "depth": [float(np.min(depth[valid])), float(np.max(depth[valid]))],
            "velocity": [float(np.min(velocity[valid])), float(np.max(velocity[valid]))],
            "pressure": [float(np.min(pressure[valid])), float(np.max(pressure[valid]))],
        },
    }
    _write_json(normalized_dir / "worker-metadata.json", metadata)
    return metadata


def run(request_path: Path, normalized_dir: Path) -> None:
    request = _read_request(request_path)
    version = importlib.metadata.version("avaframe")
    if version != request["expected_engine_version"]:
        raise RuntimeError(
            f"AvaFrame version mismatch: expected {request['expected_engine_version']}, got {version}."
        )
    parameters = request["parameters"]
    if parameters["entrainment_enabled"] is not False:
        raise ValueError("This adapter slice does not support entrainment.")

    avalanche_dir = request_path.parent / "avalanche-project"
    if avalanche_dir.exists():
        raise FileExistsError(f"Isolated avalanche project already exists: {avalanche_dir}")
    avalanche_dir.mkdir()
    grid, input_mask = _prepare_dem(request, avalanche_dir)
    feature_count = _prepare_release(request, avalanche_dir, str(grid["crs"]["definition"]))
    cfg_main, cfg, manifest = _configuration(avalanche_dir, parameters)
    com1DFA.com1DFAMain(cfg_main, cfgInfo=cfg)
    _normalize(
        avalanche_dir,
        normalized_dir,
        input_mask,
        manifest,
        _environment_manifest(),
        feature_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    run(arguments.request.resolve(), arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
