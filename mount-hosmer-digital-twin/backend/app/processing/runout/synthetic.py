"""Reproducible synthetic PRA-style release -> AvaFrame com1DFA example."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import rasterio.features
from rasterio.transform import from_origin

from avycore.engines import (
    AVYCORE_RELEASE_BASELINE,
    NormalizedComparisonResult,
    compare_runout_results,
    ENGINE_CONTRACT_SCHEMA_VERSION,
    NORMALIZED_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    AvalancheRegime,
    CRSContract,
    DeclaredInput,
    EngineRunRequest,
    EngineStage,
    ExecutionBoundary,
    GridContract,
    InputKind,
    MaskContract,
    NormalizedReleaseResult,
    NormalizedRunoutResult,
    OutputQuantity,
    RasterField,
    RunProvenance,
    VectorField,
    build_result,
    canonical_json_bytes,
    sha256_of_manifest,
)
from avycore.hazard.conditions import Conditions
from avycore.hazard.risk import RELEASE_THRESHOLD, compute_release, parameter_manifest

from .avaframe import AvaFrameCom1DFAAdapter
from .flowpy import AvaFrameCom4FlowPyAdapter
from .process import file_sha256


SYNTHETIC_CASE_VERSION = "pra-com1dfa-synthetic-v1"
SYNTHETIC_DISCLAIMER = (
    "Physics-informed estimate from an experimental synthetic avalanche-model integration. "
    "This is NOT an operational avalanche forecast and is NOT a calibrated avalanche probability. "
    "It uses no site observations and must never replace official avalanche guidance or field assessment."
)


@dataclass(frozen=True)
class _Grid:
    resolution_m: float
    shape: tuple[int, int]


class _Terrain:
    def __init__(self, layers: dict[str, np.ma.MaskedArray], resolution_m: float) -> None:
        self._layers = layers
        self.grid = _Grid(resolution_m, next(iter(layers.values())).shape)
        self.reproject = None

    def layer(self, name: str) -> np.ma.MaskedArray:
        return self._layers[name]


@dataclass(frozen=True)
class SyntheticExampleResult:
    release: NormalizedReleaseResult
    runout: NormalizedRunoutResult
    release_bundle: Path
    runout_bundle: Path


@dataclass(frozen=True)
class SyntheticEngineComparison:
    """Two independent runout engines driven from one normalized release."""

    release: NormalizedReleaseResult
    com1dfa: NormalizedRunoutResult
    flowpy: NormalizedRunoutResult
    comparison: NormalizedComparisonResult
    release_bundle: Path
    com1dfa_bundle: Path
    flowpy_bundle: Path
    comparison_bundle: Path


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _artifact(path: Path, *, uri: str | None = None, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        uri=uri or str(path.resolve()),
        sha256=file_sha256(path),
        byte_size=path.stat().st_size,
        media_type=media_type,
    )


def _inline_input(name: str, kind: InputKind, value: object, unit: str | None) -> DeclaredInput:
    source_sha256 = hashlib.sha256(
        canonical_json_bytes(
            {
                "case_version": SYNTHETIC_CASE_VERSION,
                "name": name,
                "unit": unit,
                "value": value,
            }
        )
    ).hexdigest()
    return DeclaredInput(
        name=name,
        kind=kind,
        unit=unit,
        value=value,
        status="provided",
        source_sha256=source_sha256,
    )


def _synthetic_fields() -> tuple[dict[str, np.ndarray], object, CRSContract, GridContract]:
    rows, columns, resolution = 180, 60, 5.0
    distance = np.arange(rows, dtype=np.float64)[:, None] * resolution
    upper_length = 280.0
    elevation_profile = np.where(
        distance <= upper_length,
        2300.0 - 0.67 * distance,
        2300.0 - 0.67 * upper_length - 0.07 * (distance - upper_length),
    )
    elevation = np.broadcast_to(elevation_profile, (rows, columns)).copy().astype(np.float32)
    downslope_gradient = -np.gradient(elevation.astype(np.float64), resolution, axis=0)
    slope = np.degrees(np.arctan(np.abs(downslope_gradient))).astype(np.float32)
    aspect = np.full((rows, columns), 180.0, dtype=np.float32)
    general_curvature = np.zeros((rows, columns), dtype=np.float32)
    plan_curvature = np.zeros((rows, columns), dtype=np.float32)
    forest = np.ones((rows, columns), dtype=np.float32)
    forest[8:28, 22:38] = 0.0
    mask = np.zeros((rows, columns), dtype=bool)
    transform = from_origin(500000.0, 5500000.0, resolution, resolution)
    crs = CRSContract(
        definition="EPSG:32611",
        projected=True,
        horizontal_unit="m",
        coordinate_order="x,y",
        vertical_datum=None,
        vertical_datum_status="unknown",
    )
    grid = GridContract(
        crs=crs,
        shape=(rows, columns),
        affine_transform=tuple(transform)[:6],
        cell_size_x_m=resolution,
        cell_size_y_m=resolution,
        origin_semantics="upper_left_outer_corner",
    )
    fields = {
        "elevation": elevation,
        "slope": slope,
        "aspect": aspect,
        "general_curvature": general_curvature,
        "plan_curvature": plan_curvature,
        "forest_mask": forest,
        "mask": mask,
    }
    return fields, transform, crs, grid


def _offset_extent(extent: np.ndarray, valid: np.ndarray, cells: int) -> np.ndarray:
    """Grow or shrink a boolean footprint by whole cells on the 8-neighbourhood.

    The release boundary is where a continuous relative index crosses a fixed
    cutoff on a 5 m grid, so its position is uncertain by at least one cell.
    Moving that boundary is the only honest way to vary release *extent* here:
    the alternative — sweeping the index threshold — moves nothing on terrain
    whose released cells all carry the same index value, and would report a
    zero sensitivity that is an artifact of the test surface, not a finding.
    """

    if cells == 0:
        return extent.copy()
    current = extent.copy()
    grow = cells > 0
    for _ in range(abs(cells)):
        shifted = current if grow else ~current
        neighbourhood = shifted.copy()
        for row_shift, column_shift in (
            (-1, -1), (-1, 0), (-1, 1),
            (0, -1), (0, 1),
            (1, -1), (1, 0), (1, 1),
        ):
            rolled = np.roll(np.roll(shifted, row_shift, axis=0), column_shift, axis=1)
            # np.roll wraps; the wrapped edge is outside the domain, so it must
            # not seed growth back in on the opposite side.
            if row_shift < 0:
                rolled[-1, :] = False
            elif row_shift > 0:
                rolled[0, :] = False
            if column_shift < 0:
                rolled[:, -1] = False
            elif column_shift > 0:
                rolled[:, 0] = False
            neighbourhood |= rolled
        current = (neighbourhood & valid) if grow else (~neighbourhood & valid)
    return current


def _make_release_bundle(
    root: Path, *, release_boundary_offset_m: float = 0.0
) -> tuple[NormalizedReleaseResult, dict[str, object]]:
    fields, transform, crs, grid = _synthetic_fields()
    source_mask = fields["mask"]
    layers = {
        name: np.ma.array(value, mask=source_mask)
        for name, value in fields.items()
        if name != "mask"
    }
    terrain = _Terrain(layers, grid.cell_size_x_m)
    conditions = Conditions(new_snow_cm=50.0, wind_speed_kmh=40.0, wind_direction_deg=0.0)
    risk = compute_release(terrain, conditions)
    release_valid = ~np.ma.getmaskarray(risk.release)
    release_extent = (
        np.asarray(risk.release.filled(0.0)) >= RELEASE_THRESHOLD
    ) & release_valid
    if not np.any(release_extent):
        raise RuntimeError("Synthetic PRA-style release generation produced no release cells.")

    cell_size = grid.cell_size_x_m
    offset_cells = release_boundary_offset_m / cell_size
    if offset_cells != int(offset_cells):
        raise ValueError(
            f"Release boundary offset {release_boundary_offset_m:g} m is not a whole "
            f"multiple of the {cell_size:g} m cell size."
        )
    release_extent = _offset_extent(release_extent, release_valid, int(offset_cells))
    if not np.any(release_extent):
        raise RuntimeError(
            f"A release boundary offset of {release_boundary_offset_m:g} m erodes the synthetic "
            "release to nothing. An empty release is a failed sweep member, not a zero-area result."
        )

    root.mkdir(parents=True, exist_ok=False)
    dem_path = root / "terrain-dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        width=grid.shape[1],
        height=grid.shape[0],
        count=1,
        dtype="float32",
        crs=crs.definition,
        transform=transform,
        nodata=-9999.0,
        compress="lzw",
    ) as target:
        target.write(np.where(source_mask, -9999.0, fields["elevation"]).astype(np.float32), 1)

    mask_path = root / "terrain-mask.npy"
    release_mask_path = root / "release-mask.npy"
    extent_path = root / "release-extent.npy"
    index_path = root / "release-index.npy"
    np.save(mask_path, source_mask, allow_pickle=False)
    np.save(release_mask_path, np.ma.getmaskarray(risk.release), allow_pickle=False)
    np.save(extent_path, release_extent, allow_pickle=False)
    np.save(index_path, np.asarray(risk.release.filled(0.0), dtype=np.float32), allow_pickle=False)

    features = [
        {"type": "Feature", "properties": {"source": "release.avycore_baseline"}, "geometry": geometry}
        for geometry, value in rasterio.features.shapes(
            release_extent.astype(np.uint8), mask=release_extent, transform=transform
        )
        if int(value) == 1
    ]
    release_path = root / "release.geojson"
    _write_json(release_path, {"type": "FeatureCollection", "features": features})

    mask_artifact = _artifact(
        release_mask_path, uri=release_mask_path.name, media_type="application/x-npy"
    )
    mask_contract = MaskContract(
        artifact=mask_artifact,
        valid_cells=int(np.count_nonzero(~np.ma.getmaskarray(risk.release))),
        masked_cells=int(np.count_nonzero(np.ma.getmaskarray(risk.release))),
        combined_from=(
            "slope",
            "aspect",
            "general_curvature",
            "plan_curvature",
            "forest_fraction",
        ),
    )
    extent_field = RasterField(
        quantity=OutputQuantity.RELEASE_EXTENT,
        unit="1",
        artifact=_artifact(extent_path, uri=extent_path.name, media_type="application/x-npy"),
        mask=mask_contract,
        grid=grid,
        dtype="bool",
        valid_min=0.0,
        valid_max=1.0,
        semantics=(
            f"Cells at or above the fixed relative-index threshold {RELEASE_THRESHOLD:g}, "
            f"with the resulting boundary moved by {release_boundary_offset_m:g} m."
        ),
    )
    valid_scores = np.asarray(risk.release.compressed(), dtype=np.float32)
    index_field = RasterField(
        quantity=OutputQuantity.RELEASE_INDEX,
        unit="1",
        artifact=_artifact(index_path, uri=index_path.name, media_type="application/x-npy"),
        mask=mask_contract,
        grid=grid,
        dtype="float32",
        valid_min=float(valid_scores.min()),
        valid_max=float(valid_scores.max()),
        semantics="AvyCore 0-100 deterministic relative release index; not a probability.",
    )
    polygon_field = VectorField(
        quantity=OutputQuantity.RELEASE_EXTENT,
        unit="1",
        artifact=_artifact(release_path, uri=release_path.name, media_type="application/geo+json"),
        crs=crs,
        geometry_types=tuple(sorted({feature["geometry"]["type"] for feature in features})),
        feature_count=len(features),
        semantics="Polygonization of the thresholded PRA-style relative-index cells.",
    )
    output_paths = (release_mask_path, extent_path, index_path, release_path)
    release_selection = {
        "policy": "explicit_synthetic_example",
        "selected_engine_id": AVYCORE_RELEASE_BASELINE.engine_id,
        "reason": "The example explicitly exercises the existing deterministic release baseline.",
    }
    selection_path = root / "release-selection.json"
    _write_json(selection_path, release_selection)
    output_manifest = {
        path.name: file_sha256(path) for path in (*output_paths, selection_path)
    }
    configuration = {
        "case_version": SYNTHETIC_CASE_VERSION,
        "release_parameters": parameter_manifest(),
        "conditions": conditions.to_dict(),
        "terrain_rule": "Synthetic open patch embedded in otherwise dense forest fraction.",
        # Recorded explicitly: parameter_manifest() reports the module-level
        # threshold, so without this the swept release would hash identically to
        # the central one and a member would be indistinguishable from it.
        "release_boundary_offset_m": float(release_boundary_offset_m),
    }
    input_manifest = {
        "terrain_generation": SYNTHETIC_CASE_VERSION,
        "dem_sha256": file_sha256(dem_path),
        "terrain_mask_sha256": file_sha256(mask_path),
        "conditions": conditions.to_dict(),
    }
    release_result = build_result(
        NormalizedReleaseResult,
        {
            "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
            "disclaimer": SYNTHETIC_DISCLAIMER,
            "site_id": "synthetic.utm11",
            "stage": EngineStage.RELEASE,
            "regime": AvalancheRegime.DRY_SLAB,
            "provenance": RunProvenance(
                engine_id=AVYCORE_RELEASE_BASELINE.engine_id,
                engine_version=AVYCORE_RELEASE_BASELINE.implementation_version,
                adapter_version="normalized-synthetic-example-v1",
                license_spdx=AVYCORE_RELEASE_BASELINE.license_spdx,
                execution_boundary=ExecutionBoundary.IN_PROCESS_BASELINE,
                executable_sha256=file_sha256(sys.executable),
                environment_sha256=sha256_of_manifest(
                    {
                        "python": sys.version,
                        "numpy": np.__version__,
                        "rasterio": rasterio.__version__,
                    }
                ),
                adapter_sha256=file_sha256(Path(__file__)),
                selection_sha256=sha256_of_manifest(release_selection),
                configuration_sha256=sha256_of_manifest(configuration),
                input_manifest_sha256=sha256_of_manifest(input_manifest),
                output_manifest_sha256=sha256_of_manifest(output_manifest),
                scenario_sha256=sha256_of_manifest(
                    {"configuration": configuration, "input_manifest": input_manifest}
                ),
                seed=None,
                source_urls=(AVYCORE_RELEASE_BASELINE.source_url,),
            ),
            "validation": AVYCORE_RELEASE_BASELINE.validation,
            "uncertainty": (),
            "warnings": (
                "Synthetic demonstration only; its terrain and scenario values are not site observations.",
            ),
            "limitations": (
                *AVYCORE_RELEASE_BASELINE.limitations,
                "No bounded sensitivity ensemble was supplied; no uncertainty bounds are reported.",
            ),
            "release_extent": extent_field,
            "release_polygons": polygon_field,
            "release_index": index_field,
            "release_thickness": None,
            "release_density": None,
            "release_area_m2": float(np.count_nonzero(release_extent) * 25.0),
            "release_volume_m3": None,
        },
    )
    _write_json(root / "release-result.json", release_result.model_dump(mode="json"))
    release_rows, release_columns = np.nonzero(release_extent)
    artifacts = {
        "dem": _artifact(dem_path, media_type="image/tiff; application=geotiff"),
        "terrain_mask": _artifact(mask_path, media_type="application/x-npy"),
        "release": _artifact(release_path, media_type="application/geo+json"),
        # The same release, as the raster com4FlowPy consumes.  Both engines are
        # driven from this one normalized release so a later comparison measures
        # the models rather than two differently prepared inputs.
        "release_extent": _artifact(extent_path, media_type="application/x-npy"),
        "release_mask": _artifact(release_mask_path, media_type="application/x-npy"),
        "release_valid_cells": int(np.count_nonzero(~np.ma.getmaskarray(risk.release))),
        "release_masked_cells": int(np.count_nonzero(np.ma.getmaskarray(risk.release))),
        "release_reference_cell": (
            int(round(float(np.mean(release_rows)))),
            int(round(float(np.mean(release_columns)))),
        ),
        "grid": grid,
        "crs": crs,
    }
    return release_result, artifacts


def _com1dfa_request(
    *,
    release_result: NormalizedReleaseResult,
    artifacts: dict[str, object],
    release_thickness_m: float,
    release_density_kg_m3: float,
    voellmy_mu: float,
    voellmy_xi_m_s2: float,
    simulation_time_s: float,
    time_step_s: float,
    seed: int,
) -> EngineRunRequest:
    """Build the com1DFA request from a prepared normalized release bundle."""

    grid = artifacts["grid"]
    crs = artifacts["crs"]
    terrain_source_sha256 = sha256_of_manifest(
        {
            "artifact": artifacts["dem"].sha256,
            "grid": grid.model_dump(mode="json"),
            "mask": artifacts["terrain_mask"].sha256,
        }
    )
    crs = artifacts["crs"]
    physical_parameters = {
        "release_thickness": (release_thickness_m, "m"),
        "release_density": (release_density_kg_m3, "kg m-3"),
        "voellmy_mu": (voellmy_mu, "1"),
        "voellmy_xi": (voellmy_xi_m_s2, "m s-2"),
        "entrainment_enabled": (False, None),
        "simulation_time": (simulation_time_s, "s"),
        "time_step": (time_step_s, "s"),
    }
    inputs = [
        DeclaredInput(
            name="terrain_dem",
            kind=InputKind.RASTER,
            unit="m",
            artifact=artifacts["dem"],
            grid=grid,
            mask=MaskContract(
                artifact=artifacts["terrain_mask"],
                valid_cells=grid.shape[0] * grid.shape[1],
                masked_cells=0,
                combined_from=("synthetic_dem_nodata",),
            ),
            status="provided",
            source_sha256=terrain_source_sha256,
        ),
        DeclaredInput(
            name="release_area",
            kind=InputKind.VECTOR,
            artifact=artifacts["release"],
            crs=crs,
            status="provided",
            source_sha256=release_result.result_id.rsplit("-", 1)[1],
        ),
        *(
            _inline_input(
                name,
                InputKind.FLAG if name == "entrainment_enabled" else InputKind.SCALAR,
                value,
                unit,
            )
            for name, (value, unit) in physical_parameters.items()
        ),
    ]
    scenario_sha256 = sha256_of_manifest(
        {
            "case_version": SYNTHETIC_CASE_VERSION,
            "release_result_id": release_result.result_id,
            "seed": seed,
            "physical_parameters": physical_parameters,
            "terrain_source_sha256": terrain_source_sha256,
        }
    )
    request = EngineRunRequest(
        schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
        site_id="synthetic.utm11",
        research_disclaimer=SYNTHETIC_DISCLAIMER,
        stage=EngineStage.RUNOUT,
        regime=AvalancheRegime.DENSE_DRY,
        inputs=tuple(inputs),
        requested_outputs=(
            OutputQuantity.RUNOUT_EXTENT,
            OutputQuantity.FLOW_DEPTH,
            OutputQuantity.FLOW_VELOCITY,
            OutputQuantity.FLOW_PRESSURE,
        ),
        requested_engine_id="runout.avaframe_com1dfa",
        scenario_sha256=scenario_sha256,
        seed=seed,
    )
    return request


def run_synthetic_example(
    *,
    avaframe_python: str | Path,
    output_root: str | Path,
    release_thickness_m: float = 0.8,
    release_density_kg_m3: float = 200.0,
    voellmy_mu: float = 0.155,
    voellmy_xi_m_s2: float = 4000.0,
    simulation_time_s: float = 40.0,
    time_step_s: float = 0.1,
    seed: int = 12345,
) -> SyntheticExampleResult:
    """Run the synthetic integration case with explicit, non-observed assumptions."""

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    input_root = root / "synthetic-inputs"
    if input_root.exists():
        raise FileExistsError(
            f"Synthetic input directory already exists; choose a new output root: {input_root}"
        )
    release_result, artifacts = _make_release_bundle(input_root)
    request = _com1dfa_request(
        release_result=release_result,
        artifacts=artifacts,
        release_thickness_m=release_thickness_m,
        release_density_kg_m3=release_density_kg_m3,
        voellmy_mu=voellmy_mu,
        voellmy_xi_m_s2=voellmy_xi_m_s2,
        simulation_time_s=simulation_time_s,
        time_step_s=time_step_s,
        seed=seed,
    )
    adapter = AvaFrameCom1DFAAdapter(avaframe_python)
    runout = adapter.run_runout(request, output_root=root / "runout-results")
    return SyntheticExampleResult(
        release=release_result,
        runout=runout,
        release_bundle=input_root,
        runout_bundle=root / "runout-results" / runout.result_id,
    )


def _flowpy_request(
    *,
    release_result: NormalizedReleaseResult,
    artifacts: dict[str, object],
    alpha_degrees: float,
    exponent: float,
    flux_threshold: float,
    max_energy_line_height_m: float,
) -> EngineRunRequest:
    """Build the com4FlowPy request from the same normalized release as com1DFA."""

    grid = artifacts["grid"]
    parameters = {
        "alpha_angle": (float(alpha_degrees), "degree"),
        "flowpy_exponent": (float(exponent), "1"),
        "flux_threshold": (float(flux_threshold), "1"),
        "max_energy_line_height": (float(max_energy_line_height_m), "m"),
    }
    terrain_source_sha256 = sha256_of_manifest(
        {
            "artifact": artifacts["dem"].sha256,
            "grid": grid.model_dump(mode="json"),
            "mask": artifacts["terrain_mask"].sha256,
        }
    )
    inputs = [
        DeclaredInput(
            name="terrain_dem",
            kind=InputKind.RASTER,
            unit="m",
            artifact=artifacts["dem"],
            grid=grid,
            mask=MaskContract(
                artifact=artifacts["terrain_mask"],
                valid_cells=grid.shape[0] * grid.shape[1],
                masked_cells=0,
                combined_from=("synthetic_dem_nodata",),
            ),
            status="provided",
            source_sha256=terrain_source_sha256,
        ),
        DeclaredInput(
            name="release_area",
            kind=InputKind.RASTER,
            unit="1",
            artifact=artifacts["release_extent"],
            grid=grid,
            mask=MaskContract(
                artifact=artifacts["release_mask"],
                valid_cells=artifacts["release_valid_cells"],
                masked_cells=artifacts["release_masked_cells"],
                combined_from=(
                    "slope",
                    "aspect",
                    "general_curvature",
                    "plan_curvature",
                    "forest_fraction",
                ),
            ),
            status="provided",
            source_sha256=release_result.result_id.rsplit("-", 1)[1],
        ),
        *(
            _inline_input(name, InputKind.SCALAR, value, unit)
            for name, (value, unit) in parameters.items()
        ),
    ]
    scenario_sha256 = sha256_of_manifest(
        {
            "case_version": SYNTHETIC_CASE_VERSION,
            "release_result_id": release_result.result_id,
            "routing_parameters": {name: value for name, (value, _) in parameters.items()},
            "terrain_source_sha256": terrain_source_sha256,
        }
    )
    return EngineRunRequest(
        schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
        site_id="synthetic.utm11",
        research_disclaimer=SYNTHETIC_DISCLAIMER,
        stage=EngineStage.RUNOUT,
        regime=AvalancheRegime.DENSE_DRY,
        inputs=tuple(inputs),
        requested_outputs=(
            OutputQuantity.RUNOUT_EXTENT,
            OutputQuantity.ENERGY_LINE_HEIGHT,
            OutputQuantity.TRAVEL_ANGLE,
        ),
        requested_engine_id="runout.avaframe_flowpy",
        scenario_sha256=scenario_sha256,
        seed=None,
    )


def run_synthetic_engine_comparison(
    *,
    avaframe_python: str | Path,
    output_root: str | Path,
    release_thickness_m: float = 0.8,
    release_density_kg_m3: float = 200.0,
    voellmy_mu: float = 0.155,
    voellmy_xi_m_s2: float = 4000.0,
    simulation_time_s: float = 40.0,
    time_step_s: float = 0.1,
    seed: int = 12345,
    alpha_degrees: float = 25.0,
    flowpy_exponent: float = 8.0,
    flux_threshold: float = 0.0003,
    max_energy_line_height_m: float = 270.0,
) -> SyntheticEngineComparison:
    """Run com1DFA and com4FlowPy on one release and report their disagreement.

    The two engines are independent models of different kinds: com1DFA solves a
    depth-averaged dense-flow problem, com4FlowPy routes flux along an
    energy line.  Neither output feeds the other, and the comparison measures
    where the answer depends on that choice.  It is not evidence that either is
    correct.
    """

    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    input_root = root / "synthetic-inputs"
    if input_root.exists():
        raise FileExistsError(
            f"Synthetic input directory already exists; choose a new output root: {input_root}"
        )
    release_result, artifacts = _make_release_bundle(input_root)

    com1dfa = AvaFrameCom1DFAAdapter(avaframe_python).run_runout(
        _com1dfa_request(
            release_result=release_result,
            artifacts=artifacts,
            release_thickness_m=release_thickness_m,
            release_density_kg_m3=release_density_kg_m3,
            voellmy_mu=voellmy_mu,
            voellmy_xi_m_s2=voellmy_xi_m_s2,
            simulation_time_s=simulation_time_s,
            time_step_s=time_step_s,
            seed=seed,
        ),
        output_root=root / "com1dfa-results",
    )
    flowpy = AvaFrameCom4FlowPyAdapter(avaframe_python).run_runout(
        _flowpy_request(
            release_result=release_result,
            artifacts=artifacts,
            alpha_degrees=alpha_degrees,
            exponent=flowpy_exponent,
            flux_threshold=flux_threshold,
            max_energy_line_height_m=max_energy_line_height_m,
        ),
        output_root=root / "flowpy-results",
    )
    com1dfa_bundle = root / "com1dfa-results" / com1dfa.result_id
    flowpy_bundle = root / "flowpy-results" / flowpy.result_id
    comparison = compare_runout_results(
        com1dfa,
        flowpy,
        left_bundle=com1dfa_bundle,
        right_bundle=flowpy_bundle,
        output_root=root / "comparisons",
        reference_cell=artifacts["release_reference_cell"],
    )
    return SyntheticEngineComparison(
        release=release_result,
        com1dfa=com1dfa,
        flowpy=flowpy,
        comparison=comparison,
        release_bundle=input_root,
        com1dfa_bundle=com1dfa_bundle,
        flowpy_bundle=flowpy_bundle,
        comparison_bundle=root / "comparisons" / comparison.comparison_id,
    )


__all__ = [
    "SYNTHETIC_CASE_VERSION",
    "SyntheticEngineComparison",
    "SyntheticExampleResult",
    "run_synthetic_engine_comparison",
    "run_synthetic_example",
]
