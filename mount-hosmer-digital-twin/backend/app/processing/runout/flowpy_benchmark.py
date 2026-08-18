"""Analytical energy-line verification case for the offline Flow-Py adapter.

Flow-Py routes flux with ``z_delta(next) = z_delta(current) + dz - ds*tan(alpha)``,
clipped at zero and at ``max_z`` (Neuhauser et al., 2022,
https://doi.org/10.5194/gmd-15-2423-2022).  Summed along a straight path from the
release cell the intermediate terms telescope, so on a planar slope the
energy-line height is exactly ``(z_release - z) - s*tan(alpha)`` and the flow
stops at the last cell where that value is positive.  Both are closed-form, which
makes this a real analytical check rather than a snapshot comparison.

The acceptance thresholds live in a preregistered JSON document that carries its
own content hash and is verified before the engine runs, so a limit cannot be
relaxed after a result has been seen.  Passing verifies the software on one idealized case; it says
nothing about accuracy at Mount Hosmer or anywhere else.
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import rasterio.features
from rasterio.transform import from_origin

from avycore.engines import (
    ENGINE_CONTRACT_SCHEMA_VERSION,
    AvalancheRegime,
    CRSContract,
    DeclaredInput,
    EngineRunRequest,
    EngineStage,
    GridContract,
    InputKind,
    MaskContract,
    NormalizedRunoutResult,
    OutputQuantity,
    canonical_json_bytes,
    sha256_of_manifest,
)

from .flowpy import AvaFrameCom4FlowPyAdapter, flowpy_energy_line_reference
from .process import ExternalModelProcessError, file_sha256


BENCHMARK_SCHEMA_VERSION = "avycore-flowpy-analytical-result-v1"
BENCHMARK_ID = "flowpy-energy-line-planar-v1"
BENCHMARK_DISCLAIMER = (
    "Software verification of an analytical routing case. This is NOT an operational avalanche "
    "forecast and is NOT a calibrated avalanche probability. It uses no site observations and must "
    "never replace official avalanche guidance or field assessment."
)


@dataclass(frozen=True)
class EnergyLineBenchmarkRun:
    """Validated analytical benchmark bundle returned by the offline adapter."""

    result_id: str
    bundle_path: Path
    report: dict[str, Any]
    runout: NormalizedRunoutResult


@dataclass(frozen=True)
class EnergyLineCase:
    """Synthetic planar-slope geometry with a closed-form energy-line solution."""

    rows: int = 120
    columns: int = 41
    cell_size_m: float = 5.0
    upper_slope_degrees: float = 35.0
    runout_slope_degrees: float = 5.0
    break_row: int = 60
    crest_elevation_m: float = 2000.0
    release_row: int = 2
    origin_easting_m: float = 500000.0
    origin_northing_m: float = 5500000.0
    crs_definition: str = "EPSG:32611"

    @property
    def release_column(self) -> int:
        return self.columns // 2

    def elevation(self) -> np.ndarray:
        distance = np.arange(self.rows, dtype=np.float64) * self.cell_size_m
        upper = math.tan(math.radians(self.upper_slope_degrees))
        lower = math.tan(math.radians(self.runout_slope_degrees))
        break_distance = self.break_row * self.cell_size_m
        profile = np.where(
            distance <= break_distance,
            self.crest_elevation_m - upper * distance,
            self.crest_elevation_m - upper * break_distance - lower * (distance - break_distance),
        )
        return np.broadcast_to(profile[:, None], (self.rows, self.columns)).astype(np.float32).copy()

    def grid(self) -> tuple[GridContract, CRSContract, Any]:
        transform = from_origin(
            self.origin_easting_m, self.origin_northing_m, self.cell_size_m, self.cell_size_m
        )
        crs = CRSContract(
            definition=self.crs_definition,
            projected=True,
            horizontal_unit="m",
            coordinate_order="x,y",
            vertical_datum=None,
            vertical_datum_status="unknown",
        )
        grid = GridContract(
            crs=crs,
            shape=(self.rows, self.columns),
            affine_transform=tuple(transform)[:6],
            cell_size_x_m=self.cell_size_m,
            cell_size_y_m=self.cell_size_m,
            origin_semantics="upper_left_outer_corner",
        )
        return grid, crs, transform


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _inline_input(name: str, kind: InputKind, value: object, unit: str | None) -> DeclaredInput:
    source_sha256 = hashlib.sha256(
        canonical_json_bytes({"benchmark_id": BENCHMARK_ID, "name": name, "unit": unit, "value": value})
    ).hexdigest()
    return DeclaredInput(
        name=name,
        kind=kind,
        unit=unit,
        value=value,
        status="provided",
        source_sha256=source_sha256,
    )


def build_energy_line_inputs(root: Path, case: EnergyLineCase) -> dict[str, Any]:
    """Write the synthetic DEM, masks, and single-cell release for the case."""

    root.mkdir(parents=True, exist_ok=False)
    grid, crs, transform = case.grid()
    elevation = case.elevation()
    terrain_mask = np.zeros((case.rows, case.columns), dtype=bool)
    release = np.zeros((case.rows, case.columns), dtype=np.float32)
    release[case.release_row, case.release_column] = 1.0
    release_mask = np.zeros((case.rows, case.columns), dtype=bool)

    dem_path = root / "terrain-dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=case.columns,
        height=case.rows,
        count=1,
        dtype="float32",
        crs=crs.definition,
        transform=transform,
        nodata=-9999.0,
        compress="lzw",
    ) as target:
        target.write(elevation, 1)

    terrain_mask_path = root / "terrain-mask.npy"
    release_path = root / "release.npy"
    release_mask_path = root / "release-mask.npy"
    np.save(terrain_mask_path, terrain_mask, allow_pickle=False)
    np.save(release_path, release, allow_pickle=False)
    np.save(release_mask_path, release_mask, allow_pickle=False)

    features = [
        {"type": "Feature", "properties": {"source": "flowpy-energy-line-case"}, "geometry": geometry}
        for geometry, value in rasterio.features.shapes(
            (release > 0.0).astype(np.uint8), mask=release > 0.0, transform=transform
        )
        if int(value) == 1
    ]
    release_geojson_path = root / "release.geojson"
    _write_json(release_geojson_path, {"type": "FeatureCollection", "features": features})

    return {
        "case": case,
        "grid": grid,
        "crs": crs,
        "transform": transform,
        "elevation": elevation,
        "dem_path": dem_path,
        "terrain_mask_path": terrain_mask_path,
        "release_path": release_path,
        "release_mask_path": release_mask_path,
        "release_geojson_path": release_geojson_path,
    }


def energy_line_request(
    inputs: dict[str, Any],
    *,
    site_id: str,
    disclaimer: str,
    alpha_degrees: float,
    exponent: float,
    flux_threshold: float,
    max_energy_line_height_m: float,
    scenario_extra: dict[str, Any] | None = None,
) -> EngineRunRequest:
    """Build the com4FlowPy request for a prepared synthetic case."""

    grid: GridContract = inputs["grid"]
    cells = grid.shape[0] * grid.shape[1]
    terrain_mask = MaskContract(
        artifact=_file_artifact(inputs["terrain_mask_path"], "application/x-npy"),
        valid_cells=cells,
        masked_cells=0,
        combined_from=("synthetic_dem_nodata",),
    )
    release_mask = MaskContract(
        artifact=_file_artifact(inputs["release_mask_path"], "application/x-npy"),
        valid_cells=cells,
        masked_cells=0,
        combined_from=("synthetic_release_extent",),
    )
    parameters = {
        "alpha_angle": (float(alpha_degrees), "degree"),
        "flowpy_exponent": (float(exponent), "1"),
        "flux_threshold": (float(flux_threshold), "1"),
        "max_energy_line_height": (float(max_energy_line_height_m), "m"),
    }
    declared = [
        DeclaredInput(
            name="terrain_dem",
            kind=InputKind.RASTER,
            unit="m",
            artifact=_file_artifact(inputs["dem_path"], "image/tiff; application=geotiff"),
            grid=grid,
            mask=terrain_mask,
            status="provided",
            source_sha256=sha256_of_manifest(
                {
                    "artifact": file_sha256(inputs["dem_path"]),
                    "grid": grid.model_dump(mode="json"),
                    "mask": file_sha256(inputs["terrain_mask_path"]),
                }
            ),
        ),
        DeclaredInput(
            name="release_area",
            kind=InputKind.RASTER,
            unit="1",
            artifact=_file_artifact(inputs["release_path"], "application/x-npy"),
            grid=grid,
            mask=release_mask,
            status="provided",
            source_sha256=sha256_of_manifest(
                {
                    "artifact": file_sha256(inputs["release_path"]),
                    "grid": grid.model_dump(mode="json"),
                    "mask": file_sha256(inputs["release_mask_path"]),
                }
            ),
        ),
        *(
            _inline_input(name, InputKind.SCALAR, value, unit)
            for name, (value, unit) in parameters.items()
        ),
    ]
    scenario = {
        "benchmark_id": BENCHMARK_ID,
        "parameters": {name: value for name, (value, _) in parameters.items()},
        "terrain_sha256": file_sha256(inputs["dem_path"]),
        "release_sha256": file_sha256(inputs["release_path"]),
        **(scenario_extra or {}),
    }
    return EngineRunRequest(
        schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
        site_id=site_id,
        research_disclaimer=disclaimer,
        stage=EngineStage.RUNOUT,
        regime=AvalancheRegime.DENSE_DRY,
        inputs=tuple(declared),
        requested_outputs=(
            OutputQuantity.RUNOUT_EXTENT,
            OutputQuantity.ENERGY_LINE_HEIGHT,
            OutputQuantity.TRAVEL_ANGLE,
        ),
        requested_engine_id="runout.avaframe_flowpy",
        scenario_sha256=sha256_of_manifest(scenario),
        seed=None,
    )


def _file_artifact(path: Path, media_type: str):
    from avycore.engines import ArtifactRef

    return ArtifactRef(
        uri=str(path.resolve()),
        sha256=file_sha256(path),
        byte_size=path.stat().st_size,
        media_type=media_type,
    )


def _load_acceptance(path: Path) -> tuple[dict[str, Any], str]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise ExternalModelProcessError(
            "missing_acceptance", f"Analytical acceptance document is missing: {resolved}"
        )
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("benchmark_id") != BENCHMARK_ID:
        raise ExternalModelProcessError(
            "invalid_acceptance", "Acceptance document names a different benchmark."
        )
    if payload.get("schema") != BENCHMARK_SCHEMA_VERSION:
        raise ExternalModelProcessError(
            "invalid_acceptance", "Acceptance document schema does not match this benchmark."
        )
    declared = payload.get("self_sha256")
    without_self = {key: value for key, value in payload.items() if key != "self_sha256"}
    actual = sha256_of_manifest(without_self)
    if declared != actual:
        raise ExternalModelProcessError(
            "invalid_acceptance",
            "Acceptance thresholds do not match their recorded self-identity; they may have been edited.",
        )
    return payload, actual


def run_energy_line_benchmark(
    *,
    avaframe_python: str | Path,
    acceptance_path: str | Path,
    output_root: str | Path,
    timeout_seconds: float = 1800.0,
) -> EnergyLineBenchmarkRun:
    """Run the analytical case through the real adapter and grade it.

    A threshold failure is reported as a failed result rather than raised, so the
    numbers stay inspectable; only broken software or an edited acceptance
    document raises. The thresholds are read and verified before the engine runs.
    """

    acceptance, acceptance_sha256 = _load_acceptance(Path(acceptance_path))
    case = EnergyLineCase(**acceptance["case"])
    parameters = acceptance["parameters"]
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    adapter = AvaFrameCom4FlowPyAdapter(avaframe_python, timeout_seconds=timeout_seconds)
    with tempfile.TemporaryDirectory(prefix="flowpy-energy-line-", dir=root) as temp_name:
        work = Path(temp_name)
        inputs = build_energy_line_inputs(work / "inputs", case)
        request = energy_line_request(
            inputs,
            site_id=acceptance["site_id"],
            disclaimer=BENCHMARK_DISCLAIMER,
            alpha_degrees=parameters["alpha_angle"],
            exponent=parameters["flowpy_exponent"],
            flux_threshold=parameters["flux_threshold"],
            max_energy_line_height_m=parameters["max_energy_line_height"],
            scenario_extra={"acceptance_sha256": acceptance_sha256},
        )
        runout = adapter.run_runout(request, output_root=work / "runs")
        bundle = work / "runs" / runout.result_id
        report = _grade(
            case=case,
            acceptance=acceptance,
            acceptance_sha256=acceptance_sha256,
            parameters=parameters,
            elevation=inputs["elevation"],
            bundle=bundle,
            runout=runout,
        )
        staging = work / "normalized"
        staging.mkdir()
        _write_json(staging / "benchmark-result.json", report)
        for name in ("energy-line-height.npy", "runout.npy", "travel-angle.npy",
                     "straight-line-travel-angle.npy", "travel-length.npy",
                     "configuration.json", "environment.json",
                     "upstream-implementation.json", "result.json"):
            (staging / name).write_bytes((bundle / name).read_bytes())
        destination = root / report["result_id"]
        if destination.exists():
            existing = json.loads((destination / "benchmark-result.json").read_text(encoding="utf-8"))
            if existing != report:
                raise FileExistsError(f"Analytical result identity collision at {destination}")
            return EnergyLineBenchmarkRun(
                result_id=report["result_id"], bundle_path=destination, report=report, runout=runout
            )
        staging.replace(destination)
        return EnergyLineBenchmarkRun(
            result_id=report["result_id"], bundle_path=destination, report=report, runout=runout
        )


def _grade(
    *,
    case: EnergyLineCase,
    acceptance: dict[str, Any],
    acceptance_sha256: str,
    parameters: dict[str, Any],
    elevation: np.ndarray,
    bundle: Path,
    runout: NormalizedRunoutResult,
) -> dict[str, Any]:
    energy = np.load(bundle / "energy-line-height.npy", allow_pickle=False)
    extent = np.load(bundle / "runout.npy", allow_pickle=False)
    angle_mask = np.load(bundle / "travel-angle-mask.npy", allow_pickle=False)
    straight_angle = np.load(bundle / "straight-line-travel-angle.npy", allow_pickle=False)
    travel_length = np.load(bundle / "travel-length.npy", allow_pickle=False)

    column = case.release_column
    alpha = float(parameters["alpha_angle"])
    reference = flowpy_energy_line_reference(
        elevation=elevation,
        release_row=case.release_row,
        release_column=column,
        cell_size_m=case.cell_size_m,
        alpha_degrees=alpha,
        max_energy_line_height_m=float(parameters["max_energy_line_height"]),
    )
    reached = reference > 0.0
    reached[case.release_row] = True
    analytic_last_row = int(np.max(np.nonzero(reached)[0]))

    modelled_column = np.asarray(energy[:, column], dtype=np.float64)
    modelled_reached = np.asarray(extent[:, column], dtype=bool)
    modelled_last_row = int(np.max(np.nonzero(modelled_reached)[0])) if modelled_reached.any() else -1

    compare = np.zeros(case.rows, dtype=bool)
    compare[case.release_row : analytic_last_row + 1] = True
    energy_error = float(np.max(np.abs(modelled_column[compare] - reference[compare])))

    rows = np.arange(case.rows, dtype=np.float64)
    path_distance = (rows - case.release_row) * case.cell_size_m
    drop = float(elevation[case.release_row, column]) - elevation[:, column].astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        analytic_angle = np.degrees(np.arctan(np.divide(drop, path_distance)))
    downstream = compare.copy()
    downstream[case.release_row] = False
    angle_error = float(
        np.max(np.abs(np.asarray(straight_angle[:, column], dtype=np.float64)[downstream] - analytic_angle[downstream]))
    )
    length_error = float(
        np.max(np.abs(np.asarray(travel_length[:, column], dtype=np.float64)[downstream] - path_distance[downstream]))
    )

    limits = acceptance["acceptance_metrics"]
    metrics = {
        "energy_line_height_max_absolute_error_m": energy_error,
        "stopping_row_difference_cells": float(modelled_last_row - analytic_last_row),
        "straight_line_travel_angle_max_absolute_error_degree": angle_error,
        "travel_length_max_absolute_error_m": length_error,
    }
    passed = {
        name: bool(abs(value) <= float(limits[name]))
        for name, value in metrics.items()
    }
    invariants = {
        "runout_extent_unit": runout.runout_extent.unit == "1",
        "energy_line_unit_m": runout.energy_line_height is not None
        and runout.energy_line_height.unit == "m",
        "travel_angle_unit_degree": runout.travel_angle is not None
        and runout.travel_angle.unit == "degree",
        "projected_metre_crs": runout.runout_extent.grid.crs.projected
        and runout.runout_extent.grid.crs.horizontal_unit == "m",
        "coordinate_order_xy": runout.runout_extent.grid.crs.coordinate_order == "x,y",
        "no_domain_truncation": runout.aoi_status == "complete_within_domain",
        "unreached_cells_are_zero_not_masked": bool(
            np.count_nonzero(~angle_mask) == np.count_nonzero(extent)
        ),
        "velocity_depth_pressure_arrival_unsupported": {
            item.quantity.value for item in runout.unsupported_outputs
        }
        == {"flow_depth", "flow_velocity", "flow_pressure", "arrival_time"},
    }
    core = {
        "schema": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "acceptance_sha256": acceptance_sha256,
        "case": {
            "rows": case.rows,
            "columns": case.columns,
            "cell_size_m": case.cell_size_m,
            "upper_slope_degrees": case.upper_slope_degrees,
            "runout_slope_degrees": case.runout_slope_degrees,
            "break_row": case.break_row,
            "release_row": case.release_row,
            "release_column": column,
        },
        "parameters": dict(parameters),
        "analytic_last_reached_row": analytic_last_row,
        "modelled_last_reached_row": modelled_last_row,
        "metrics": metrics,
        "acceptance_limits": {name: float(limits[name]) for name in metrics},
        "metric_passed": passed,
        "invariants": invariants,
        "runout_result_id": runout.result_id,
        "engine": {
            "engine_id": runout.provenance.engine_id,
            "version": runout.provenance.engine_version,
            "adapter_version": runout.provenance.adapter_version,
            "license_spdx": runout.provenance.license_spdx,
            "configuration_sha256": runout.provenance.configuration_sha256,
            "environment_sha256": runout.provenance.environment_sha256,
            "adapter_sha256": runout.provenance.adapter_sha256,
        },
        "passed": bool(all(passed.values()) and all(_flatten_bool(invariants))),
        "interpretation": (
            "One idealized analytical routing case verified in software. This is not calibration, "
            "not field validation, and not evidence of accuracy at any real site."
        ),
    }
    identity = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
    return {**core, "result_id": f"flowpy-energy-line-{identity}"}


def _flatten_bool(value: Any) -> list[bool]:
    if isinstance(value, bool):
        return [value]
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _flatten_bool(entry)]
    return [bool(value)]


def verify_stored_benchmark(bundle: str | Path, acceptance_path: str | Path) -> dict[str, Any]:
    """Re-validate a stored analytical bundle without running the engine."""

    acceptance, acceptance_sha256 = _load_acceptance(Path(acceptance_path))
    root = Path(bundle).resolve()
    report = json.loads((root / "benchmark-result.json").read_text(encoding="utf-8"))
    if report.get("acceptance_sha256") != acceptance_sha256:
        raise ExternalModelProcessError(
            "invalid_output", "Stored benchmark is bound to different acceptance thresholds."
        )
    core = {key: value for key, value in report.items() if key != "result_id"}
    expected = f"flowpy-energy-line-{hashlib.sha256(canonical_json_bytes(core)).hexdigest()}"
    if report.get("result_id") != expected:
        raise ExternalModelProcessError("invalid_output", "Stored benchmark identity is invalid.")
    if report.get("benchmark_id") != acceptance["benchmark_id"]:
        raise ExternalModelProcessError("invalid_output", "Stored benchmark names a different case.")
    return report


__all__ = [
    "BENCHMARK_DISCLAIMER",
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "EnergyLineBenchmarkRun",
    "EnergyLineCase",
    "build_energy_line_inputs",
    "energy_line_request",
    "run_energy_line_benchmark",
    "verify_stored_benchmark",
]
