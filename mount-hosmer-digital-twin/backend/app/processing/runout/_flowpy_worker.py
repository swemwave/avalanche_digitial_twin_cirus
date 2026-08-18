"""Standalone AvaFrame com4FlowPy worker used only by the offline Flow-Py adapter.

This file intentionally imports neither ``app`` nor ``avycore``.  It is launched
with ``python -I`` so the selected AvaFrame environment, rather than the serving
environment, supplies the numerical and geospatial dependency closure.

com4FlowPy routes every release cell in its own ``multiprocessing`` pool.  On
Windows that pool spawns fresh interpreters which re-import this module, so the
``__main__`` guard at the bottom is a correctness requirement, not a style
choice: without it the worker forks endlessly instead of running.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.metadata
import json
import math
import platform
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
from avaframe.com4FlowPy import com4FlowPy
from rasterio.crs import CRS


SCHEMA_VERSION = "avycore-flowpy-worker-v1"

# com4FlowPy writes this value into cells the process never reached.  That is a
# model statement ("no flux routed here"), not missing input data, so it must
# never be folded into the unknown-data mask.
UNAFFECTED_VALUE = -9999.0

# Layers this slice requests from com4FlowPy, mapped to the file-name stem the
# upstream writer actually produces (``zDelta`` is written lowercase).
REQUESTED_LAYERS = {
    "zDelta": "zdelta",
    "cellCounts": "cellCounts",
    "travelLengthMax": "travelLengthMax",
    "fpTravelAngleMax": "fpTravelAngleMax",
    "slTravelAngle": "slTravelAngle",
}

# Upstream defaults that this slice deliberately holds fixed.  Every one of them
# is a module we do not supply an input layer for; leaving them enabled would let
# com4FlowPy read an absent raster or silently change the routing physics.
FIXED_GENERAL_OPTIONS = {
    "infra": "False",
    "forest": "False",
    "forestInteraction": "False",
    "previewMode": "False",
    "variableUmaxLim": "False",
    "variableAlpha": "False",
    "variableExponent": "False",
    "fluxDistOldVersion": "False",
}


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        raise ValueError(f"DEM CRS {dataset.crs!s} does not match declared CRS {declared_crs!s}.")
    if not bool(declared["crs"]["projected"]):
        raise ValueError("com4FlowPy requires a projected metre-based DEM.")
    if [dataset.height, dataset.width] != list(declared["shape"]):
        raise ValueError("DEM dimensions do not match the declared grid.")
    expected_transform = tuple(float(item) for item in declared["affine_transform"])
    if not np.allclose(tuple(dataset.transform)[:6], expected_transform, rtol=0.0, atol=1.0e-9):
        raise ValueError("DEM affine transform does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.a), float(declared["cell_size_x_m"]), abs_tol=1e-9):
        raise ValueError("DEM horizontal cell size does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.e), float(declared["cell_size_y_m"]), abs_tol=1e-9):
        raise ValueError("DEM vertical cell size does not match the declared grid.")
    if not math.isclose(abs(dataset.transform.a), abs(dataset.transform.e), abs_tol=1e-9):
        raise ValueError("com4FlowPy requires square DEM cells.")


def _prepare_inputs(request: dict[str, object], work: Path) -> tuple[dict[str, object], np.ndarray, int]:
    """Copy DEM and release raster onto one verified grid inside the work directory."""

    declared_grid = request["terrain_grid"]
    if not isinstance(declared_grid, dict):
        raise ValueError("terrain_grid must be an object.")
    terrain_mask = np.load(Path(str(request["terrain_mask_path"])), allow_pickle=False)
    if terrain_mask.dtype != np.dtype("bool"):
        raise ValueError("The declared terrain mask must be a bool NPY array.")

    with rasterio.open(Path(str(request["terrain_dem_path"]))) as dataset:
        _validate_grid(dataset, declared_grid)
        dem = dataset.read(1, masked=True)
        if terrain_mask.shape != dem.shape:
            raise ValueError("The explicit terrain mask shape does not match the DEM.")
        source_invalid = np.ma.getmaskarray(dem) | ~np.isfinite(dem.filled(np.nan))
        if not np.array_equal(source_invalid, terrain_mask):
            raise ValueError("The explicit terrain mask does not match DEM nodata/non-finite cells.")
        profile = dataset.profile.copy()
        transform = dataset.transform
        crs = dataset.crs

    release_mask = np.load(Path(str(request["release_mask_path"])), allow_pickle=False)
    release = np.load(Path(str(request["release_path"])), allow_pickle=False)
    if release.shape != dem.shape or release_mask.shape != dem.shape:
        raise ValueError("Release arrays do not share the DEM grid.")
    if release_mask.dtype != np.dtype("bool"):
        raise ValueError("The declared release mask must be a bool NPY array.")
    release_values = np.asarray(release, dtype=np.float32)
    if np.any(~np.isfinite(release_values[~release_mask])):
        raise ValueError("Release raster contains non-finite values inside its valid domain.")
    if np.any(release_values[~release_mask] < 0.0):
        raise ValueError("Release raster contains negative values inside its valid domain.")

    # A release cell on unknown terrain has no defensible starting altitude, so
    # this is refused rather than quietly dropped.
    if np.any((release_values > 0.0) & ~release_mask & terrain_mask):
        raise ValueError("Release cells overlap unknown DEM cells; the run is refused.")
    release_cells = int(np.count_nonzero((release_values > 0.0) & ~release_mask))
    if release_cells == 0:
        raise ValueError("Release raster contains no positive release cell.")

    profile.update(driver="GTiff", dtype="float32", count=1, nodata=UNAFFECTED_VALUE, compress="lzw")
    dem_path = work / "dem.tif"
    with rasterio.open(dem_path, "w", **profile) as output:
        output.write(np.asarray(dem.filled(UNAFFECTED_VALUE), dtype=np.float32), 1)

    # Unknown or masked release cells become a negative nodata value, which
    # upstream reads as "not a release cell" rather than as a zero measurement.
    release_out = np.where(release_mask, UNAFFECTED_VALUE, release_values).astype(np.float32)
    release_path = work / "release.tif"
    with rasterio.open(release_path, "w", **profile) as output:
        output.write(release_out, 1)

    grid = {
        "crs": {
            "definition": crs.to_string(),
            "projected": bool(crs.is_projected),
            "horizontal_unit": "m",
            "coordinate_order": "x,y",
            "vertical_datum": declared_grid["crs"].get("vertical_datum"),
            "vertical_datum_status": declared_grid["crs"]["vertical_datum_status"],
        },
        "shape": [int(dem.shape[0]), int(dem.shape[1])],
        "affine_transform": [float(item) for item in tuple(transform)[:6]],
        "cell_size_x_m": float(abs(transform.a)),
        "cell_size_y_m": float(abs(transform.e)),
        "origin_semantics": "upper_left_outer_corner",
    }
    return grid, terrain_mask, release_cells


def _module_inventory() -> dict[str, object]:
    """Hash the com4FlowPy sources that actually executed.

    Flow-Py exists both as an archived standalone distribution and as this
    AvaFrame port.  Recording the executed module bytes is what makes the claim
    "this normalized result came from com4FlowPy" checkable after the fact.
    """

    package = Path(com4FlowPy.__file__).resolve().parent
    files = sorted(item for item in package.iterdir() if item.suffix in {".py", ".ini"})
    return {
        "provider": "avaframe.com4FlowPy",
        "upstream_family": "flow-py",
        "avaframe_version": importlib.metadata.version("avaframe"),
        "files": [
            {"name": item.name, "byte_size": item.stat().st_size, "sha256": _file_sha256(item)}
            for item in files
        ],
    }


def _configuration(request: dict[str, object]) -> tuple[configparser.SectionProxy, dict[str, object], str]:
    parameters = request["parameters"]
    package = Path(com4FlowPy.__file__).resolve().parent
    default_ini = package / "com4FlowPyCfg.ini"
    default_sha256 = _file_sha256(default_ini)

    parser = configparser.ConfigParser()
    parser.optionxform = str
    read = parser.read(default_ini, encoding="utf-8")
    if not read or "GENERAL" not in parser:
        raise RuntimeError("The pinned com4FlowPy configuration file could not be read.")
    general = parser["GENERAL"]

    general["alpha"] = repr(float(parameters["alpha_angle"]))
    general["exp"] = repr(float(parameters["flowpy_exponent"]))
    general["flux_threshold"] = repr(float(parameters["flux_threshold"]))
    general["max_z"] = repr(float(parameters["max_energy_line_height"]))
    for key, value in FIXED_GENERAL_OPTIONS.items():
        general[key] = value

    # One tile and one process: com4FlowPy merges overlapping tiles with max/sum
    # reductions and distributes release cells over a pool, so both settings are
    # what make byte-identical replay achievable.
    general["tileSize"] = repr(float(parameters["tile_size_m"]))
    general["tileOverlap"] = repr(float(parameters["tile_overlap_m"]))
    general["cpuCount"] = "1"
    general["procPerCPUCore"] = "1"

    manifest = {
        "default_config_sha256": default_sha256,
        "general": {key: general[key] for key in sorted(general)},
        "output_files": sorted(REQUESTED_LAYERS),
        "output_no_data_value": UNAFFECTED_VALUE,
        "output_file_format": ".tif",
        "use_compression": False,
    }
    return general, manifest, default_sha256


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


def _read_layer(result_dir: Path, uid: str, time_string: str, layer: str) -> np.ndarray:
    stem = REQUESTED_LAYERS[layer]
    candidates = sorted(result_dir.glob(f"com4_{uid}_{time_string}_{stem}.tif"))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one {layer!r} raster, found {len(candidates)}.")
    with rasterio.open(candidates[0]) as source:
        return np.asarray(source.read(1), dtype=np.float64)


def _normalize(
    result_dir: Path,
    normalized_dir: Path,
    *,
    uid: str,
    time_string: str,
    grid: dict[str, object],
    terrain_mask: np.ndarray,
    release_cells: int,
    configuration: dict[str, object],
    environment: dict[str, object],
    modules: dict[str, object],
) -> dict[str, object]:
    layers = {name: _read_layer(result_dir, uid, time_string, name) for name in REQUESTED_LAYERS}
    shape = tuple(int(value) for value in grid["shape"])
    for name, values in layers.items():
        if values.shape != shape:
            raise RuntimeError(f"com4FlowPy {name!r} raster does not match the declared grid.")

    z_delta = layers["zDelta"]
    # "Unaffected" is the model's answer, so it becomes a valid zero rather than
    # an unknown cell.  Only unknown terrain stays masked.
    affected = (z_delta != UNAFFECTED_VALUE) & ~terrain_mask
    if np.any(affected & (z_delta < 0.0)):
        raise RuntimeError("com4FlowPy returned a negative energy-line height on an affected cell.")

    energy_line = np.where(affected, z_delta, 0.0).astype(np.float32)
    energy_line[terrain_mask] = 0.0
    runout = affected.copy()

    # A travel angle is only defined where a path actually arrived; publishing
    # zero elsewhere would invent a measurement, so those cells stay masked.
    travel_angle_mask = terrain_mask | ~affected
    travel_angle = np.where(affected, layers["fpTravelAngleMax"], 0.0).astype(np.float32)
    travel_angle[travel_angle_mask] = 0.0
    straight_line_angle = np.where(affected, layers["slTravelAngle"], 0.0).astype(np.float32)
    straight_line_angle[travel_angle_mask] = 0.0
    travel_length = np.where(affected, layers["travelLengthMax"], 0.0).astype(np.float32)
    travel_length[travel_angle_mask] = 0.0
    cell_counts = np.where(affected, layers["cellCounts"], 0.0).astype(np.float32)
    cell_counts[terrain_mask] = 0.0

    normalized_dir.mkdir(parents=True, exist_ok=False)
    np.save(normalized_dir / "mask.npy", terrain_mask, allow_pickle=False)
    np.save(normalized_dir / "travel-angle-mask.npy", travel_angle_mask, allow_pickle=False)
    np.save(normalized_dir / "runout.npy", runout, allow_pickle=False)
    np.save(normalized_dir / "energy-line-height.npy", energy_line, allow_pickle=False)
    np.save(normalized_dir / "travel-angle.npy", travel_angle, allow_pickle=False)
    np.save(normalized_dir / "straight-line-travel-angle.npy", straight_line_angle, allow_pickle=False)
    np.save(normalized_dir / "travel-length.npy", travel_length, allow_pickle=False)
    np.save(normalized_dir / "cell-counts.npy", cell_counts, allow_pickle=False)

    transform = rasterio.Affine(*(float(item) for item in grid["affine_transform"]))
    shapes = []
    if np.any(runout):
        shapes = [
            {"type": "Feature", "properties": {}, "geometry": geometry}
            for geometry, value in rasterio.features.shapes(
                runout.astype(np.uint8), mask=runout, transform=transform
            )
            if int(value) == 1
        ]
    _write_json(normalized_dir / "runout.geojson", {"type": "FeatureCollection", "features": shapes})
    _write_json(normalized_dir / "configuration.json", configuration)
    _write_json(normalized_dir / "environment.json", environment)
    _write_json(normalized_dir / "upstream-implementation.json", modules)

    valid = ~terrain_mask
    angle_valid = ~travel_angle_mask
    boundary_touched = bool(
        np.any(runout[0, :]) or np.any(runout[-1, :]) or np.any(runout[:, 0]) or np.any(runout[:, -1])
    )
    cell_area = float(grid["cell_size_x_m"]) * float(grid["cell_size_y_m"])

    def _range(values: np.ndarray, domain: np.ndarray) -> list[float]:
        if not np.any(domain):
            raise RuntimeError("A normalized field has no valid cells.")
        return [float(np.min(values[domain])), float(np.max(values[domain]))]

    metadata = {
        "schema_version": SCHEMA_VERSION,
        "engine_version": importlib.metadata.version("avaframe"),
        "configuration_sha256": _sha256(configuration),
        "environment_sha256": _sha256(environment),
        "upstream_implementation_sha256": _sha256(modules),
        "grid": grid,
        "valid_cells": int(np.count_nonzero(valid)),
        "masked_cells": int(np.count_nonzero(terrain_mask)),
        "travel_angle_valid_cells": int(np.count_nonzero(angle_valid)),
        "travel_angle_masked_cells": int(np.count_nonzero(travel_angle_mask)),
        "release_cells": release_cells,
        "affected_cells": int(np.count_nonzero(runout)),
        "runout_feature_count": len(shapes),
        "geometry_types": sorted({feature["geometry"]["type"] for feature in shapes}),
        "runout_area_m2": float(np.count_nonzero(runout) * cell_area),
        "boundary_touched": boundary_touched,
        "ranges": {
            "runout": [0.0, 1.0],
            "energy_line_height": _range(energy_line, valid),
            "travel_angle": _range(travel_angle, angle_valid) if np.any(angle_valid) else [0.0, 0.0],
        },
        "diagnostics": {
            "straight_line_travel_angle_range": (
                _range(straight_line_angle, angle_valid) if np.any(angle_valid) else [0.0, 0.0]
            ),
            "travel_length_range_m": (
                _range(travel_length, angle_valid) if np.any(angle_valid) else [0.0, 0.0]
            ),
            "maximum_cell_count": float(np.max(cell_counts[valid])),
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

    work = request_path.parent / "flowpy-project"
    if work.exists():
        raise FileExistsError(f"Isolated com4FlowPy project already exists: {work}")
    result_dir = work / "results"
    temp_dir = work / "results" / "temp"
    temp_dir.mkdir(parents=True)

    grid, terrain_mask, release_cells = _prepare_inputs(request, work)
    general, configuration, _ = _configuration(request)
    modules = _module_inventory()

    uid = "run"
    time_string = "run"
    com4FlowPy.com4FlowPyMain(
        {
            "outDir": result_dir,
            "workDir": work,
            "demPath": work / "dem.tif",
            "releasePath": work / "release.tif",
            "resDir": result_dir,
            "tempDir": temp_dir,
            "uid": uid,
            "timeString": time_string,
            "outputFiles": "|".join(sorted(REQUESTED_LAYERS)),
            "outputNoDataValue": UNAFFECTED_VALUE,
            "useCompression": False,
            "outputFileFormat": ".tif",
            "customDirs": "True",
            "deleteTemp": "False",
            "infraPath": "",
            "forestPath": "",
            "varUmaxPath": "",
            "varAlphaPath": "",
            "varExponentPath": "",
            "relIdPath": "",
        },
        general,
    )
    _normalize(
        result_dir,
        normalized_dir,
        uid=uid,
        time_string=time_string,
        grid=grid,
        terrain_mask=terrain_mask,
        release_cells=release_cells,
        configuration=configuration,
        environment=_environment_manifest(),
        modules=modules,
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
