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


def _make_release_bundle(root: Path) -> tuple[NormalizedReleaseResult, dict[str, object]]:
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
    release_extent = (
        (np.asarray(risk.release.filled(0.0)) >= RELEASE_THRESHOLD)
        & ~np.ma.getmaskarray(risk.release)
    )
    if not np.any(release_extent):
        raise RuntimeError("Synthetic PRA-style release generation produced no release cells.")

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
        semantics=f"Cells at or above the fixed relative-index threshold {RELEASE_THRESHOLD:g}.",
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
    artifacts = {
        "dem": _artifact(dem_path, media_type="image/tiff; application=geotiff"),
        "terrain_mask": _artifact(mask_path, media_type="application/x-npy"),
        "release": _artifact(release_path, media_type="application/geo+json"),
        "grid": grid,
        "crs": crs,
    }
    return release_result, artifacts


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
    grid = artifacts["grid"]
    crs = artifacts["crs"]
    terrain_source_sha256 = sha256_of_manifest(
        {
            "artifact": artifacts["dem"].sha256,
            "grid": grid.model_dump(mode="json"),
            "mask": artifacts["terrain_mask"].sha256,
        }
    )
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
    adapter = AvaFrameCom1DFAAdapter(avaframe_python)
    runout = adapter.run_runout(request, output_root=root / "runout-results")
    return SyntheticExampleResult(
        release=release_result,
        runout=runout,
        release_bundle=input_root,
        runout_bundle=root / "runout-results" / runout.result_id,
    )


__all__ = ["SYNTHETIC_CASE_VERSION", "SyntheticExampleResult", "run_synthetic_example"]
