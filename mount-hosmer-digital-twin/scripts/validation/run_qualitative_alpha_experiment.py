"""Reproduce the frozen lower-rigor alpha-routing comparison on Davos events.

This program intentionally computes mapped-positive coverage, not IoU.  The
source inventories do not supply a surveyed known-absence domain, so cells
outside a mapped polygon are never counted as false positives.  See the frozen
experiment specification before changing any rule in this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import shape

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SOURCE = REPOSITORY_ROOT / "packages" / "avycore" / "src"
ENGINE_SOURCE_RELATIVE_PATH = Path("packages/avycore/src/avycore/hazard/runout.py")
RUNNER_SOURCE_RELATIVE_PATH = Path(
    "scripts/validation/run_qualitative_alpha_experiment.py"
)
if str(AVYCORE_SOURCE) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SOURCE))

from avycore.hazard.runout import FastRunoutEngine  # noqa: E402
from avycore.hazard.zone import ReleaseZone  # noqa: E402
from avycore.validation import (  # noqa: E402
    EvaluationGrid,
    QualitativePredictionContext,
    ValidationDataset,
    load_validation_dataset,
    positive_only_polygon_metrics,
)


SPEC_RELATIVE_PATH = Path(
    "validation-data/experiments/alpha-only-real-events-v1.json"
)
RESULT_RELATIVE_PATH = Path(
    "validation-data/results/alpha-only-real-events-v1.json"
)
BRAMA_MANIFEST_RELATIVE_PATH = Path(
    "validation-data/braemabuehl-2019-qualitative/manifest.json"
)
SPOT_MANIFEST_RELATIVE_PATH = Path(
    "validation-data/davos-spot-2019-qualitative/manifest.json"
)
RELEASE_SIZES = ("small", "medium", "large", "very_large")
SHAPEFILE_COMPONENT_SUFFIXES = frozenset({".shp", ".shx", ".dbf", ".prj", ".cpg"})
BRAMA_SOURCE_PREFIX_BY_EVENT = {
    "braemabuehl-2019-wildi": "Wildi",
    "braemabuehl-2019-ruechi": "Rüchi",
}
BRAMA_DTM_MD5 = "680930cdd4af3410551909810a66ca54"
COPERNICUS_DEM_SHA256 = (
    "6a7eccb6d198f01a1fdfcca0e1cef837ef294456fb7243ec4d0966e089b1e7fc"
)
COPERNICUS_DEM_URL = (
    "https://copernicus-dem-30m.s3.amazonaws.com/"
    "Copernicus_DSM_COG_10_N46_00_E009_00_DEM/"
    "Copernicus_DSM_COG_10_N46_00_E009_00_DEM.tif"
)


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "crs": self.crs_string,
            "bounds": [self.west, self.south, self.east, self.north],
            "resolution_m": self.resolution_m,
            "shape": list(self.shape),
        }


@dataclass(frozen=True)
class RegisteredEventEvidence:
    dataset: ValidationDataset
    observation_id: str
    observation_type: str
    mapped_positive_geometry: Any
    release_observation_id: str | None


class EvidenceBindingError(ValueError):
    """Raised when raw source evidence and the committed contract diverge."""


class FrozenParameters:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def require(self, dotted: str) -> Any:
        node: Any = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"Frozen parameter {dotted!r} is missing")
            node = node[part]
        return node


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parameter_manifest_sha256(value: Any) -> str:
    """Use the established runtime parameter-manifest encoding."""

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _bool_mask_sha256(array: np.ndarray) -> str:
    """Hash a boolean mask independently of numpy strides and byte layout."""

    contiguous = np.ascontiguousarray(array, dtype=np.bool_)
    header = json.dumps(
        {"format": "packed-bool-little-v1", "shape": list(contiguous.shape)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    packed = np.packbits(contiguous.reshape(-1), bitorder="little").tobytes()
    digest = hashlib.sha256()
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    digest.update(packed)
    return digest.hexdigest()


def _masked_float32_sha256(array: np.ma.MaskedArray) -> str:
    """Hash valid float32 terrain values plus their explicit missing-data mask."""

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


def _aligned_grid(
    bounds: Iterable[float], *, resolution_m: float, buffer_m: float = 0.0
) -> ExperimentGrid:
    west, south, east, north = (float(value) for value in bounds)
    west = math.floor((west - buffer_m) / resolution_m) * resolution_m
    south = math.floor((south - buffer_m) / resolution_m) * resolution_m
    east = math.ceil((east + buffer_m) / resolution_m) * resolution_m
    north = math.ceil((north + buffer_m) / resolution_m) * resolution_m
    return ExperimentGrid(west, south, east, north, resolution_m)


def _read_dem_on_grid(path: Path, grid: ExperimentGrid) -> np.ma.MaskedArray:
    destination = np.full(grid.shape, np.nan, dtype="float32")
    with rasterio.open(path) as source:
        reproject(
            source=rasterio.band(source, 1),
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=source.nodata,
            dst_transform=grid.transform,
            dst_crs=grid.crs_string,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
            num_threads=1,
        )
    valid = np.isfinite(destination)
    return np.ma.array(destination, mask=~valid, copy=False)


def _polygon_mask(geometry: Any, grid: ExperimentGrid) -> np.ndarray:
    """Rasterize at cell centres using the public metric contract's convention."""

    rows, cols = grid.shape
    rows_per_chunk = max(1, 1_000_000 // cols)
    x = grid.west + (np.arange(cols, dtype="float64") + 0.5) * grid.resolution_m
    result = np.zeros(grid.shape, dtype=bool)
    shapely.prepare(geometry)
    for row_start in range(0, rows, rows_per_chunk):
        row_stop = min(rows, row_start + rows_per_chunk)
        row_numbers = np.arange(row_start, row_stop, dtype="float64")
        y = grid.north - (row_numbers + 0.5) * grid.resolution_m
        xx = np.broadcast_to(x, (row_stop - row_start, cols))
        yy = np.broadcast_to(y[:, None], xx.shape)
        result[row_start:row_stop] = shapely.intersects_xy(geometry, xx, yy)
    return result


def _slope_degrees(elevation: np.ma.MaskedArray, resolution_m: float) -> np.ndarray:
    values = np.asarray(elevation.filled(np.nan), dtype="float64")
    dz_drow, dz_dcol = np.gradient(values, resolution_m, resolution_m)
    slope = np.degrees(np.arctan(np.hypot(dz_drow, dz_dcol)))
    slope[~np.isfinite(slope)] = np.nan
    return slope


def _mask_touches_grid_or_data_boundary(
    mask: np.ndarray, valid: np.ndarray
) -> bool:
    if not np.any(mask):
        return False
    if (
        np.any(mask[0, :])
        or np.any(mask[-1, :])
        or np.any(mask[:, 0])
        or np.any(mask[:, -1])
    ):
        return True
    invalid = ~valid
    adjacent_invalid = np.zeros_like(invalid)
    for drow in (-1, 0, 1):
        for dcol in (-1, 0, 1):
            if drow == 0 and dcol == 0:
                continue
            source_rows = slice(max(0, -drow), invalid.shape[0] - max(0, drow))
            source_cols = slice(max(0, -dcol), invalid.shape[1] - max(0, dcol))
            target_rows = slice(max(0, drow), invalid.shape[0] - max(0, -drow))
            target_cols = slice(max(0, dcol), invalid.shape[1] - max(0, -dcol))
            adjacent_invalid[target_rows, target_cols] |= invalid[
                source_rows, source_cols
            ]
    return bool(np.any(mask & adjacent_invalid))


def _bundle_hashes(shapefile: Path) -> dict[str, str]:
    """Hash each physical Shapefile component exactly once.

    ``Path.exists`` is case-insensitive on Windows, so probing both ``.cpg`` and
    ``.CPG`` recorded one file twice under two synthetic names.  Directory
    enumeration preserves the physical source name and grouping by case-folded
    suffix makes the result identical on case-sensitive and insensitive hosts.
    """

    by_suffix: dict[str, Path] = {}
    for candidate in shapefile.parent.iterdir():
        suffix = candidate.suffix.casefold()
        if (
            candidate.is_file()
            and candidate.stem.casefold() == shapefile.stem.casefold()
            and suffix in SHAPEFILE_COMPONENT_SUFFIXES
        ):
            if suffix in by_suffix:
                raise ValueError(
                    f"Ambiguous Shapefile bundle: multiple {suffix} components for "
                    f"{shapefile.name!r}."
                )
            by_suffix[suffix] = candidate
    missing = SHAPEFILE_COMPONENT_SUFFIXES - set(by_suffix)
    if missing:
        raise ValueError(
            f"Incomplete Shapefile bundle for {shapefile.name!r}; missing "
            f"{sorted(missing)}."
        )
    hashes = {
        candidate.name: _sha256_file(candidate)
        for candidate in by_suffix.values()
    }
    return dict(sorted(hashes.items()))


def _registered_observation(
    dataset: ValidationDataset,
    *,
    event_id: str,
    observation_type: str,
) -> Any:
    matches = [
        item
        for item in dataset.observations
        if item.event_id == event_id and item.observation_type == observation_type
    ]
    if len(matches) != 1:
        raise EvidenceBindingError(
            f"Expected exactly one committed {observation_type!r} for event "
            f"{event_id!r}; found {len(matches)}."
        )
    observation = matches[0]
    if observation.partition != "qualitative":
        raise EvidenceBindingError(
            f"Committed observation {observation.observation_id!r} is not qualitative."
        )
    return observation


def _assert_raw_geometry_matches(
    raw_geometry: Any,
    observation: Any,
    *,
    source_label: str,
) -> Any:
    """Fail closed unless raw and committed normalized coordinates are identical."""

    registered_geometry = shape(observation.geometry)
    if not raw_geometry.equals_exact(registered_geometry, tolerance=0.0):
        raise EvidenceBindingError(
            f"Raw geometry {source_label!r} differs from committed observation "
            f"{observation.observation_id!r}."
        )
    raw_wkb_sha256 = hashlib.sha256(raw_geometry.wkb).hexdigest()
    expected_wkb_sha256 = observation.properties.get("source_geometry_wkb_sha256")
    if raw_wkb_sha256 != expected_wkb_sha256:
        raise EvidenceBindingError(
            f"Raw geometry WKB identity for {source_label!r} does not match committed "
            f"observation {observation.observation_id!r}."
        )
    return registered_geometry


def _evaluation_artifact_sha256(
    *,
    dem_sha256: str,
    grid: ExperimentGrid,
    elevation: np.ma.MaskedArray,
) -> str:
    """Bind the exact resampled terrain input used by one event run."""

    return _canonical_sha256(
        {
            "schema": "avycore-qualitative-evaluation-terrain-v1",
            "source_dem_sha256": dem_sha256,
            "grid": grid.to_dict(),
            "resampling": "rasterio bilinear, one thread",
            "masked_elevation_sha256": _masked_float32_sha256(elevation),
        }
    )


def _run_configuration_sha256(
    *,
    experiment_spec_sha256: str,
    event: dict[str, Any],
    release_size: str,
    alpha_angle_deg: float,
    release_mask: np.ndarray,
) -> str:
    """Bind assumptions without representing them as observed event conditions."""

    return _canonical_sha256(
        {
            "schema": "avycore-qualitative-run-configuration-v1",
            "experiment_spec_sha256": experiment_spec_sha256,
            "event_id": event["event_id"],
            "engine": "fast_routing_alpha",
            "engine_mode": "alpha_only",
            "release_size_sensitivity_setting": release_size,
            "alpha_angle_deg": alpha_angle_deg,
            "flow_regime_assumption": "dry_slab_unverified",
            "release_source": event["release_source"],
            "release_mask_sha256": _bool_mask_sha256(release_mask),
        }
    )


def _dataset_provenance(dataset: ValidationDataset) -> dict[str, Any]:
    return {
        "dataset_id": dataset.manifest.dataset_id,
        "dataset_identity_sha256": dataset.dataset_identity_sha256,
        "manifest_path": dataset.manifest_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "manifest_sha256": dataset.manifest_sha256,
        "observations_path": dataset.observations_path.relative_to(
            REPOSITORY_ROOT
        ).as_posix(),
        "observations_sha256": dataset.manifest.observations_sha256,
        "scientific_use": dataset.manifest.scientific_use,
        "evidence_type": dataset.manifest.evidence_type,
        "coverage_semantics": dataset.manifest.coverage_semantics,
        "absence_semantics": dataset.manifest.absence_semantics,
    }


def _validate_spec_dataset_binding(
    spec: dict[str, Any],
    *,
    brama_dataset: ValidationDataset,
    spot_dataset: ValidationDataset,
) -> None:
    """Require one complete, exact registry-to-experiment event mapping."""

    spec_brama = {
        event["event_id"]
        for event in spec["events"]
        if event["event_id"].startswith("braemabuehl-")
    }
    spec_spot = {
        event["event_id"]
        for event in spec["events"]
        if event["event_id"].startswith("davos-spot-")
    }
    dataset_brama = {item.event_id for item in brama_dataset.observations}
    dataset_spot = {item.event_id for item in spot_dataset.observations}
    if spec_brama != dataset_brama or spec_spot != dataset_spot:
        raise EvidenceBindingError(
            "Frozen experiment event IDs do not exactly match the two committed "
            "ValidationDataset registries."
        )
    if len(spec_brama) != 2 or len(spec_spot) != 6:
        raise EvidenceBindingError(
            "The frozen qualitative experiment must remain bound to two Brämabühl "
            "events and six Davos SPOT events."
        )


def _dummy_layer(elevation: np.ma.MaskedArray) -> np.ma.MaskedArray:
    return np.ma.array(
        np.zeros(elevation.shape, dtype="float32"),
        mask=np.ma.getmaskarray(elevation),
    )


def _simulate_sweep(
    *,
    event: dict[str, Any],
    grid: ExperimentGrid,
    elevation: np.ma.MaskedArray,
    release_mask: np.ndarray,
    mapped_positive_mask: np.ndarray,
    evidence: RegisteredEventEvidence,
    parameters: FrozenParameters,
    experiment_spec_sha256: str,
    config_sha256: str,
    model_version: str,
    evaluation_source_artifact_sha256: str,
) -> list[dict[str, Any]]:
    valid = ~np.ma.getmaskarray(elevation) & np.isfinite(elevation.filled(np.nan))
    mapped_count = int(np.count_nonzero(mapped_positive_mask))
    mapped_valid_count = int(np.count_nonzero(mapped_positive_mask & valid))
    release_count = int(np.count_nonzero(release_mask))
    release_mapped_count = int(
        np.count_nonzero(release_mask & mapped_positive_mask & valid)
    )
    empty_layer = _dummy_layer(elevation)
    evaluation_grid = EvaluationGrid(
        crs=grid.crs_string,
        west=grid.west,
        north=grid.north,
        resolution_m=grid.resolution_m,
        shape=grid.shape,
        source_artifact_sha256=evaluation_source_artifact_sha256,
    )
    runs: list[dict[str, Any]] = []

    for release_size in RELEASE_SIZES:
        alpha_angle_deg = float(
            parameters.require(f"runout.alpha_angle_deg.{release_size}")
        )
        run_configuration_sha256 = _run_configuration_sha256(
            experiment_spec_sha256=experiment_spec_sha256,
            event=event,
            release_size=release_size,
            alpha_angle_deg=alpha_angle_deg,
            release_mask=release_mask,
        )
        base = {
            "event_id": event["event_id"],
            "analysis_group": event["analysis_group"],
            "partition": event["partition"],
            "validation_partition": "qualitative",
            "release_size": release_size,
            "alpha_angle_deg": alpha_angle_deg,
            "component_tested": "empirical_alpha_angle_plus_routing",
            "engine": "fast_routing_alpha",
            "engine_mode": "alpha_only",
            "random_seed": None,
            "flow_regime_assumption": "dry_slab_unverified",
            "metric_scope": "mapped_positive_coverage_only",
            "supports_independent_validation_claim": False,
            "negative_evidence_used": False,
            "unmapped_cells_treated_as_negative": False,
            "release_depth_sensitivity": "unsupported_by_engine",
            "dataset_id": evidence.dataset.manifest.dataset_id,
            "dataset_identity_sha256": evidence.dataset.dataset_identity_sha256,
            "mapped_positive_observation_id": evidence.observation_id,
            "mapped_positive_observation_type": evidence.observation_type,
            "release_observation_id": evidence.release_observation_id,
            "mapped_positive_geometry_source": (
                "committed_normalized_validation_observation"
            ),
            "run_configuration_sha256": run_configuration_sha256,
            "evaluation_grid_identity_sha256": (
                evaluation_grid.grid_identity_sha256
            ),
            "evaluation_source_artifact_sha256": (
                evaluation_source_artifact_sha256
            ),
            "mapped_positive_cell_count": mapped_count,
            "mapped_positive_comparable_cell_count": mapped_valid_count,
            "release_cell_count": release_count,
            "intersecting_mapped_positive_release_cell_count": release_mapped_count,
            "mapped_positive_coverage_includes_release_cells": release_mapped_count > 0,
            "cell_area_m2": grid.resolution_m**2,
            "predicted_mask_sha256": None,
            "positive_only_metric": None,
        }
        if mapped_count == 0 or release_count == 0:
            reason = (
                "mapped polygon contains no 10 m cell centres"
                if mapped_count == 0
                else "release geometry rule produced no 10 m cells"
            )
            runs.append(
                {
                    **base,
                    "status": "unscoreable_input_geometry",
                    "unscoreable_reason": reason,
                    "particles_left_the_aoi": 0,
                    "aoi_boundary_contact": False,
                    "mapped_positive_coverage": None,
                    "predicted_to_mapped_area_ratio": None,
                }
            )
            continue
        if mapped_valid_count != mapped_count or not np.all(valid[release_mask]):
            runs.append(
                {
                    **base,
                    "status": "unscoreable_incomplete_dem_coverage",
                    "unscoreable_reason": (
                        "DEM does not cover every mapped-positive and release cell"
                    ),
                    "particles_left_the_aoi": 0,
                    "aoi_boundary_contact": False,
                    "mapped_positive_coverage": None,
                    "predicted_to_mapped_area_ratio": None,
                }
            )
            continue

        result = FastRunoutEngine().simulate(
            zone=ReleaseZone(
                zone_id=event["event_id"],
                pixels=release_mask,
                geometry=None,
            ),
            grid=grid,
            elevation=elevation,
            slope=empty_layer,
            forest_mask=empty_layer,
            plan_curvature=empty_layer,
            config=parameters,
            release_size=release_size,
            seed=None,
            flow_regime="dry_slab",
        )
        if result.mode != "alpha_only" or result.metadata["engine_mode"] != "alpha_only":
            raise RuntimeError(
                "FastRunoutEngine returned a result that is not explicitly alpha_only."
            )
        if result.metadata["engine"] != "fast_routing_alpha":
            raise RuntimeError("FastRunoutEngine returned an unexpected engine identity.")
        predicted = np.asarray(result.reached, dtype=bool) & valid
        boundary_contact = _mask_touches_grid_or_data_boundary(
            np.asarray(result.reached | result.uncertainty, dtype=bool), valid
        )
        particles_left = int(result.metadata["particles_left_the_aoi"])
        if particles_left:
            status = "unscoreable_particles_left_the_aoi"
            reason = f"{particles_left} particles_left_the_aoi"
        elif boundary_contact:
            status = "unscoreable_aoi_boundary_contact"
            reason = (
                "alpha-routing reached or uncertainty cells touch the AOI/data boundary"
            )
        else:
            status = "scoreable_qualitative"
            reason = None

        metric = None
        if status == "scoreable_qualitative":
            prediction_context = QualitativePredictionContext(
                event_id=event["event_id"],
                model_version=model_version,
                config_sha256=config_sha256,
                bake_sha256=evaluation_source_artifact_sha256,
                engine="fast_routing_alpha",
                engine_mode="alpha_only",
                random_seed=None,
                particles_left_the_aoi=particles_left,
                aoi_boundary_contact=boundary_contact,
                run_configuration_sha256=run_configuration_sha256,
            )
            metric = positive_only_polygon_metrics(
                predicted,
                valid_mask=valid,
                evaluation_grid=evaluation_grid,
                prediction_context=prediction_context,
                dataset=evidence.dataset,
                partition="qualitative",
                observation_type=evidence.observation_type,
                observation_ids=[evidence.observation_id],
            )
            if metric.component_tested != "empirical_alpha_angle_plus_routing":
                raise RuntimeError("Public metric returned an unexpected component identity.")
            if metric.mapped_positive_cell_count != mapped_count:
                raise RuntimeError(
                    "Public metric rasterization differs from the committed-input mask."
                )
            if metric.mapped_positive_comparable_cell_count != mapped_valid_count:
                raise RuntimeError(
                    "Mapped-positive denominator changed in the public metric path."
                )
            predicted_count = metric.predicted_positive_valid_cell_count
            intersection_count = metric.intersecting_mapped_positive_cell_count
            predicted_mask_sha256 = metric.predicted_mask_sha256
        else:
            predicted_count = int(np.count_nonzero(predicted & valid))
            intersection_count = int(
                np.count_nonzero(predicted & mapped_positive_mask & valid)
            )
            predicted_mask_sha256 = _bool_mask_sha256(predicted)
        if release_mapped_count > intersection_count:
            raise RuntimeError(
                "Initialized mapped-positive release cells exceed total mapped-positive "
                "intersection."
            )
        metrics_permitted = status == "scoreable_qualitative"
        runs.append(
            {
                **base,
                "status": status,
                "unscoreable_reason": reason,
                "particles_left_the_aoi": particles_left,
                "aoi_boundary_contact": boundary_contact,
                "predicted_positive_valid_cell_count": predicted_count,
                "intersecting_mapped_positive_cell_count": intersection_count,
                "intersecting_mapped_positive_nonrelease_cell_count": (
                    intersection_count - release_mapped_count
                ),
                "predicted_mask_sha256": predicted_mask_sha256,
                "mapped_positive_coverage": (
                    round(metric.mapped_positive_coverage_fraction, 6)
                    if metrics_permitted
                    else None
                ),
                "predicted_to_mapped_area_ratio": (
                    round(predicted_count / mapped_valid_count, 6)
                    if metrics_permitted
                    else None
                ),
                "predicted_area_m2": predicted_count * grid.resolution_m**2,
                "mapped_positive_area_m2": mapped_count * grid.resolution_m**2,
                "intersection_area_m2": intersection_count * grid.resolution_m**2,
                "positive_only_metric": (
                    metric.to_dict() if metric is not None else None
                ),
                "alpha_source": result.metadata["alpha_source"],
                "minimum_flux": result.metadata["minimum_flux"],
            }
        )
    return runs


def _event_summary(event: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    scoreable = [run for run in runs if run["status"] == "scoreable_qualitative"]
    coverages = [float(run["mapped_positive_coverage"]) for run in scoreable]
    ratios = [float(run["predicted_to_mapped_area_ratio"]) for run in scoreable]
    return {
        "event_id": event["event_id"],
        "analysis_group": event["analysis_group"],
        "partition": event["partition"],
        "release_source": event["release_source"],
        "mapped_positive_source": event["mapped_positive_source"],
        "scoreable_sweep_count": len(scoreable),
        "unscoreable_sweep_count": len(runs) - len(scoreable),
        "mapped_positive_coverage_range": (
            [round(min(coverages), 6), round(max(coverages), 6)]
            if coverages
            else None
        ),
        "predicted_to_mapped_area_ratio_range": (
            [round(min(ratios), 6), round(max(ratios), 6)] if ratios else None
        ),
        "all_release_sizes_scoreable": len(scoreable) == len(RELEASE_SIZES),
        "nonzero_overlap_at_all_release_sizes": (
            len(scoreable) == len(RELEASE_SIZES) and min(coverages) > 0.0
            if coverages
            else False
        ),
        "runs": runs,
    }


def _require_epsg2056(frame: gpd.GeoDataFrame, *, source_label: str) -> None:
    if frame.crs is None or frame.crs.to_epsg() != 2056:
        raise EvidenceBindingError(
            f"Raw source {source_label!r} must declare EPSG:2056 exactly."
        )


def _load_brama_event_inputs(
    event: dict[str, Any], brama_dir: Path, dataset: ValidationDataset
) -> tuple[
    ExperimentGrid,
    np.ma.MaskedArray,
    np.ndarray,
    np.ndarray,
    RegisteredEventEvidence,
    dict[str, Any],
]:
    dem_path = brama_dir / "Braemabuehl_DTM_1m.tif"
    if _md5_file(dem_path) != BRAMA_DTM_MD5:
        raise ValueError("Brama DTM MD5 does not match the reviewed Zenodo record")
    try:
        prefix = BRAMA_SOURCE_PREFIX_BY_EVENT[event["event_id"]]
    except KeyError as exc:
        raise ValueError(
            f"No frozen Brämabühl source mapping for event {event['event_id']!r}."
        ) from exc
    release_path = brama_dir / f"{prefix}_RelArea.shp"
    deposit_path = brama_dir / f"{prefix}_DepoArea.shp"
    releases = gpd.read_file(release_path)
    deposits = gpd.read_file(deposit_path)
    _require_epsg2056(releases, source_label=release_path.name)
    _require_epsg2056(deposits, source_label=deposit_path.name)
    if len(releases) != 1 or len(deposits) != 1:
        raise EvidenceBindingError(
            "Each reviewed Brämabühl source Shapefile must contain exactly one feature."
        )
    release_observation = _registered_observation(
        dataset,
        event_id=event["event_id"],
        observation_type="release_polygon",
    )
    deposit_observation = _registered_observation(
        dataset,
        event_id=event["event_id"],
        observation_type="deposit_polygon",
    )
    release_geometry = _assert_raw_geometry_matches(
        releases.geometry.iloc[0],
        release_observation,
        source_label=release_path.name,
    )
    deposit_geometry = _assert_raw_geometry_matches(
        deposits.geometry.iloc[0],
        deposit_observation,
        source_label=deposit_path.name,
    )
    with rasterio.open(dem_path) as source:
        grid = _aligned_grid(source.bounds, resolution_m=10.0)
    elevation = _read_dem_on_grid(dem_path, grid)
    release_mask = _polygon_mask(release_geometry, grid)
    mapped_mask = _polygon_mask(deposit_geometry, grid)
    evidence = RegisteredEventEvidence(
        dataset=dataset,
        observation_id=deposit_observation.observation_id,
        observation_type="deposit_polygon",
        mapped_positive_geometry=deposit_geometry,
        release_observation_id=release_observation.observation_id,
    )
    lineage = {
        "dem_sha256": _sha256_file(dem_path),
        "dem_md5": BRAMA_DTM_MD5,
        "release_geometry_files_sha256": _bundle_hashes(release_path),
        "mapped_positive_geometry_files_sha256": _bundle_hashes(deposit_path),
        "raw_geometry_exact_match_to_committed_observations": True,
        "release_observation_id": release_observation.observation_id,
        "mapped_positive_observation_id": deposit_observation.observation_id,
        "geometry_used_by_model": "committed_normalized_validation_observation",
    }
    return grid, elevation, release_mask, mapped_mask, evidence, lineage


def _load_spot_event_inputs(
    event: dict[str, Any],
    spot_shapefile: Path,
    dem_path: Path,
    dataset: ValidationDataset,
) -> tuple[
    ExperimentGrid,
    np.ma.MaskedArray,
    np.ndarray,
    np.ndarray,
    RegisteredEventEvidence,
    dict[str, Any],
]:
    source = gpd.read_file(spot_shapefile)
    _require_epsg2056(source, source_label=spot_shapefile.name)
    selected = source[source["OBJECTID"] == event["source_objectid"]]
    if len(selected) != 1:
        raise ValueError(
            f"Expected exactly one SPOT OBJECTID {event['source_objectid']}"
        )
    row = selected.iloc[0]
    if row["typ"] != "SLAB" or int(row["aval_shape"]) != 1:
        raise ValueError("Frozen SPOT event is not an exact SLAB source feature")
    footprint_observation = _registered_observation(
        dataset,
        event_id=event["event_id"],
        observation_type="avalanche_footprint",
    )
    expected_source_feature_id = (
        f"SPOT_2019_perimeter:OBJECTID={event['source_objectid']}"
    )
    if (
        footprint_observation.properties.get("source_feature_id")
        != expected_source_feature_id
    ):
        raise EvidenceBindingError(
            f"Committed observation {footprint_observation.observation_id!r} does not "
            "match the frozen SPOT source feature ID."
        )
    footprint = _assert_raw_geometry_matches(
        row.geometry,
        footprint_observation,
        source_label=expected_source_feature_id,
    )
    grid = _aligned_grid(footprint.bounds, resolution_m=10.0, buffer_m=4500.0)
    elevation = _read_dem_on_grid(dem_path, grid)
    mapped_mask = _polygon_mask(footprint, grid)
    valid = ~np.ma.getmaskarray(elevation) & np.isfinite(elevation.filled(np.nan))
    candidate = mapped_mask & valid
    if np.any(candidate):
        values = np.asarray(elevation.filled(np.nan), dtype="float64")
        minimum = float(np.min(values[candidate]))
        maximum = float(np.max(values[candidate]))
        threshold = minimum + 0.8 * (maximum - minimum)
        slope = _slope_degrees(elevation, grid.resolution_m)
        release_mask = candidate & (values >= threshold) & (slope >= 27.0)
    else:
        minimum = maximum = threshold = None
        release_mask = np.zeros(grid.shape, dtype=bool)
    evidence = RegisteredEventEvidence(
        dataset=dataset,
        observation_id=footprint_observation.observation_id,
        observation_type="avalanche_footprint",
        mapped_positive_geometry=footprint,
        release_observation_id=None,
    )
    lineage = {
        "dem_sha256": _sha256_file(dem_path),
        "source_geometry_files_sha256": _bundle_hashes(spot_shapefile),
        "source_objectid": int(event["source_objectid"]),
        "raw_geometry_exact_match_to_committed_observation": True,
        "mapped_positive_observation_id": footprint_observation.observation_id,
        "geometry_used_by_model": "committed_normalized_validation_observation",
        "source_attributes": {
            "typ": str(row["typ"]),
            "aval_shape": int(row["aval_shape"]),
            "trigger_type": str(row["trg_typ"]),
            "reported_size": int(row["sze"]),
        },
        "derived_release_rule_values": {
            "mapped_min_elevation_m": (
                round(minimum, 6) if minimum is not None else None
            ),
            "mapped_max_elevation_m": (
                round(maximum, 6) if maximum is not None else None
            ),
            "upper_20_percent_threshold_m": (
                round(threshold, 6) if threshold is not None else None
            ),
            "minimum_slope_deg": 27.0,
        },
    }
    return grid, elevation, release_mask, mapped_mask, evidence, lineage


def run_experiment(
    *,
    spec_path: Path,
    brama_dir: Path,
    spot_shapefile: Path,
    spot_dem: Path,
) -> dict[str, Any]:
    spec_bytes = spec_path.read_bytes()
    spec = json.loads(spec_bytes)
    experiment_spec_sha256 = hashlib.sha256(spec_bytes).hexdigest()
    brama_dataset = load_validation_dataset(
        REPOSITORY_ROOT / BRAMA_MANIFEST_RELATIVE_PATH
    )
    spot_dataset = load_validation_dataset(
        REPOSITORY_ROOT / SPOT_MANIFEST_RELATIVE_PATH
    )
    _validate_spec_dataset_binding(
        spec,
        brama_dataset=brama_dataset,
        spot_dataset=spot_dataset,
    )
    baseline = json.loads(
        (REPOSITORY_ROOT / "backend/config/m0-baseline.json").read_text(
            encoding="utf-8"
        )
    )
    parameter_manifest = baseline["model"]["parameter_manifest"]
    model_version = str(parameter_manifest["version"])
    parameters = FrozenParameters(parameter_manifest)
    expected_parameter_hash = spec["fixed_model"]["parameter_manifest_sha256"]
    computed_parameter_hash = _parameter_manifest_sha256(parameter_manifest)
    if (
        baseline["model"]["sha256"] != expected_parameter_hash
        or computed_parameter_hash != expected_parameter_hash
    ):
        raise ValueError(
            "Frozen parameter-manifest hash does not match the actual m0-baseline.json "
            "parameter content."
        )
    if tuple(spec["sensitivity_grid"]["release_size"]) != RELEASE_SIZES:
        raise ValueError("Frozen release-size sweep does not match the runner order.")
    for release_size in RELEASE_SIZES:
        configured_alpha = float(
            parameters.require(f"runout.alpha_angle_deg.{release_size}")
        )
        if configured_alpha != float(
            spec["sensitivity_grid"]["alpha_angle_deg"][release_size]
        ):
            raise ValueError(
                f"Frozen alpha angle for {release_size!r} does not match the parameter "
                "manifest."
            )
    fixed_fast_parameters = {
        "runout.fast_mode.spreading": "fast_mode_spreading",
        "runout.fast_mode.max_path_length_m": "fast_mode_max_path_length_m",
        "runout.fast_mode.minimum_flux": "fast_mode_minimum_flux",
    }
    for parameter_path, spec_field in fixed_fast_parameters.items():
        if float(parameters.require(parameter_path)) != float(
            spec["fixed_model"][spec_field]
        ):
            raise ValueError(
                f"Frozen {spec_field!r} does not match the parameter manifest."
            )
    if _sha256_file(spot_dem) != COPERNICUS_DEM_SHA256:
        raise ValueError("SPOT DEM SHA-256 does not match the frozen acquired artifact")

    summaries: list[dict[str, Any]] = []
    for event in spec["events"]:
        try:
            if event["event_id"].startswith("braemabuehl-"):
                dataset = brama_dataset
                (
                    grid,
                    elevation,
                    release,
                    mapped,
                    evidence,
                    lineage,
                ) = _load_brama_event_inputs(
                    event, brama_dir, dataset
                )
            else:
                dataset = spot_dataset
                (
                    grid,
                    elevation,
                    release,
                    mapped,
                    evidence,
                    lineage,
                ) = _load_spot_event_inputs(
                    event, spot_shapefile, spot_dem, dataset
                )
            evaluation_source_artifact_sha256 = _evaluation_artifact_sha256(
                dem_sha256=lineage["dem_sha256"],
                grid=grid,
                elevation=elevation,
            )
            runs = _simulate_sweep(
                event=event,
                grid=grid,
                elevation=elevation,
                release_mask=release,
                mapped_positive_mask=mapped,
                evidence=evidence,
                parameters=parameters,
                experiment_spec_sha256=experiment_spec_sha256,
                config_sha256=computed_parameter_hash,
                model_version=model_version,
                evaluation_source_artifact_sha256=(
                    evaluation_source_artifact_sha256
                ),
            )
            summary = _event_summary(event, runs)
            summary["grid"] = grid.to_dict()
            summary["evaluation_source_artifact_sha256"] = (
                evaluation_source_artifact_sha256
            )
            summary["dataset_id"] = dataset.manifest.dataset_id
            summary["dataset_identity_sha256"] = dataset.dataset_identity_sha256
            summary["mapped_positive_observation_id"] = evidence.observation_id
            summary["mapped_positive_observation_type"] = evidence.observation_type
            summary["release_observation_id"] = evidence.release_observation_id
            valid = ~np.ma.getmaskarray(elevation) & np.isfinite(
                elevation.filled(np.nan)
            )
            summary["input_mask_sha256"] = {
                "valid": _bool_mask_sha256(valid),
                "release": _bool_mask_sha256(release),
                "mapped_positive": _bool_mask_sha256(mapped),
            }
            summary["input_lineage"] = lineage
        except EvidenceBindingError:
            raise
        except Exception as exc:
            summary = {
                "event_id": event["event_id"],
                "analysis_group": event["analysis_group"],
                "partition": event["partition"],
                "release_source": event["release_source"],
                "mapped_positive_source": event["mapped_positive_source"],
                "scoreable_sweep_count": 0,
                "unscoreable_sweep_count": len(RELEASE_SIZES),
                "mapped_positive_coverage_range": None,
                "predicted_to_mapped_area_ratio_range": None,
                "all_release_sizes_scoreable": False,
                "nonzero_overlap_at_all_release_sizes": False,
                "status": "unscoreable_source_or_processing_error",
                "unscoreable_reason": f"{type(exc).__name__}: {exc}",
                "runs": [],
            }
        summaries.append(summary)

    all_runs = [run for summary in summaries for run in summary["runs"]]
    scoreable_runs = [run for run in all_runs if run["status"] == "scoreable_qualitative"]
    artifact = {
        "schema": "avycore-qualitative-alpha-results-v2",
        "experiment_id": spec["experiment_id"],
        "experiment_spec_sha256": experiment_spec_sha256,
        "original_pre_amendment_frozen_spec_sha256": spec[
            "post_freeze_metadata_amendments"
        ][0]["original_frozen_spec_sha256"],
        "post_freeze_metadata_amendments": spec[
            "post_freeze_metadata_amendments"
        ],
        "scientific_use": "qualitative_comparison",
        "component_tested": "empirical_alpha_angle_plus_routing",
        "engine": "fast_routing_alpha",
        "engine_mode": "alpha_only",
        "flow_regime_assumption": "dry_slab_unverified",
        "flow_regime_assumption_scope": (
            "Model assumption only. Source SLAB classifications do not establish snow "
            "humidity or verify a dry-slab event regime."
        ),
        "model_identity": {
            "parameter_manifest_sha256": computed_parameter_hash,
            "engine_source_path": ENGINE_SOURCE_RELATIVE_PATH.as_posix(),
            "engine_source_sha256": _sha256_file(
                REPOSITORY_ROOT / ENGINE_SOURCE_RELATIVE_PATH
            ),
        },
        "regeneration_environment": {
            "scope": (
                "Reproduction provenance only; library and runner versions are not "
                "scientific evidence or model-validation identity."
            ),
            "runner_source_path": RUNNER_SOURCE_RELATIVE_PATH.as_posix(),
            "runner_source_sha256": _sha256_file(
                REPOSITORY_ROOT / RUNNER_SOURCE_RELATIVE_PATH
            ),
            "library_versions": {
                "numpy": np.__version__,
                "rasterio": rasterio.__version__,
                "geopandas": gpd.__version__,
                "shapely": shapely.__version__,
            },
        },
        "is_field_validation": False,
        "is_validated": False,
        "claim_boundary": spec["claim_boundary"],
        "analysis_group_scope": (
            "analysis_group is a bookkeeping label only and is not evidence of "
            "independent mountains, summit identity, or geographic independence. The "
            "post-freeze mountain_area-to-analysis_group correction changed no event "
            "membership, partition, model parameter, geometry rule, or score."
        ),
        "split_frozen_before_results": bool(spec["frozen_before_results"]),
        "parameters_tuned": False,
        "release_depth_sensitivity": {
            "status": "unsupported",
            "reason": spec["sensitivity_grid"]["release_depth"]["reason"],
        },
        "metric_method": {
            "implementation": (
                "avycore.validation.metrics.positive_only_polygon_metrics"
            ),
            "prediction_context": "QualitativePredictionContext",
            "public_metric_artifact_schema": (
                "avycore-positive-only-polygon-prediction-artifact-v2"
            ),
            "mapped_positive_coverage_source": (
                "Public positive-only metric output bound to one committed normalized "
                "ValidationDataset observation and an explicit evaluation grid/context."
            ),
            "mapped_positive_coverage_formula": (
                "count(predicted AND mapped_positive AND valid) / "
                "count(mapped_positive AND valid)"
            ),
            "predicted_to_mapped_area_ratio_formula": (
                "count(predicted AND valid) / count(mapped_positive AND valid)"
            ),
            "predicted_to_mapped_area_ratio_scope": (
                "Separately derived extent-scale diagnostic; not emitted by the public "
                "positive-only metric, not precision, and not a false-positive rate."
            ),
            "boundary_uncertainty_handling": (
                "No boundary band is excluded because both source manifests explicitly "
                "record positional uncertainty as unknown rather than quantified."
            ),
            "release_cell_overlap_caveat": (
                "FastRunoutEngine initializes valid release cells as reached. Reported "
                "mapped-positive coverage therefore includes any mapped-positive release "
                "cells; each run decomposes intersection into release and non-release cells."
            ),
            "release_overlap_diagnostic_scope": (
                "Separately derived initialization-overlap diagnostic; it is not a second "
                "validation metric."
            ),
            "historical_scenario_handling": (
                "No complete historical scenario is invented. QualitativePredictionContext "
                "binds the prespecified sensitivity run while the public metric reports "
                "registered scenario missingness."
            ),
            "negative_evidence_used": False,
        },
        "validation_datasets": {
            "braemabuehl": _dataset_provenance(brama_dataset),
            "davos_spot": _dataset_provenance(spot_dataset),
        },
        "sources": {
            "brama": spec["sources"]["brama"],
            "spot": spec["sources"]["spot"],
            "spot_dem": {
                "product": "Copernicus DEM GLO-30 public COG tile N46 E009",
                "url": COPERNICUS_DEM_URL,
                "sha256": COPERNICUS_DEM_SHA256,
                "source_crs": "EPSG:4326",
                "native_sampling_arc_seconds": 1.0,
                "surface_type": "digital_surface_model",
                "experiment_resampling": "bilinear to 10 m EPSG:2056; this does not add terrain detail",
                "dem_caveat": "The 2011-2015 Copernicus DSM is not the 2019 event-day snow surface or a bare-earth DTM.",
            },
        },
        "terrain_error_characterization": {
            "brama": {
                "documented_new_snow_m": 0.6,
                "new_snow_description": (
                    "The event report states roughly 0.60 m of new snow plus wind-drifted "
                    "accumulations over the summer bare-earth DTM. Prior snow and drift depth "
                    "are not quantified."
                ),
                "alpha_horizontal_scale_m_from_documented_new_snow": {
                    release_size: round(
                        0.6
                        / math.tan(
                            math.radians(
                                float(
                                    parameters.require(
                                        f"runout.alpha_angle_deg.{release_size}"
                                    )
                                )
                            )
                        ),
                        2,
                    )
                    for release_size in RELEASE_SIZES
                },
                "interpretation": (
                    "These 0.96-1.74 m values are geometry scales for the documented 0.60 m "
                    "layer, not error bounds. Wind drift, prior snow, surface change, and total "
                    "event-day vertical mismatch remain unbounded; the comparison remains "
                    "qualitative."
                ),
            },
            "spot_copernicus_glo30": {
                "official_product_handbook": (
                    "https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/"
                    "geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf"
                ),
                "handbook_version": "5.0 (2022-11-29)",
                "source_acquisition_period": "2011-2015",
                "event_period": "2019-01",
                "absolute_vertical_accuracy_specification": (
                    "<4 m LE90; global arithmetic-mean specification, with local deviations possible"
                ),
                "relative_vertical_accuracy_specification_steep_terrain": (
                    "<4 m LE90 point-to-point for slope >20%; not a site-specific bound"
                ),
                "absolute_horizontal_accuracy_specification": "<6 m CE90",
                "vertical_reference": "EGM2008 (EPSG:3855)",
                "alpha_horizontal_scale_m_from_4m_relative_vertical_specification": {
                    release_size: round(
                        4.0
                        / math.tan(
                            math.radians(
                                float(
                                    parameters.require(
                                        f"runout.alpha_angle_deg.{release_size}"
                                    )
                                )
                            )
                        ),
                        2,
                    )
                    for release_size in RELEASE_SIZES
                },
                "interpretation": (
                    "The 6.40-11.62 m translation is a scale implied by the product's steep-"
                    "terrain relative-vertical specification, not a Davos uncertainty bound. "
                    "The DSM includes vegetation and structures, predates the event by roughly "
                    "four to eight years, and does not represent event-day snow. Local terrain, "
                    "vegetation, and snow-surface mismatch remain unbounded."
                ),
            },
        },
        "counts": {
            "registered_event_count": len(spec["events"]),
            "analysis_group_count": len(
                {event["analysis_group"] for event in spec["events"]}
            ),
            "planned_run_count": len(spec["events"]) * len(RELEASE_SIZES),
            "executed_run_count": len(all_runs),
            "scoreable_qualitative_run_count": len(scoreable_runs),
            "unscoreable_run_count": len(spec["events"]) * len(RELEASE_SIZES)
            - len(scoreable_runs),
            "field_validation_holdout_n": 0,
        },
        "partitions": spec["partitions"],
        "events": summaries,
    }
    artifact["artifact_identity_scope"] = (
        "canonical SHA-256 of the complete result object excluding only "
        "artifact_identity_sha256"
    )
    artifact["artifact_identity_sha256"] = _canonical_sha256(artifact)
    return artifact


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec",
        type=Path,
        default=REPOSITORY_ROOT / SPEC_RELATIVE_PATH,
    )
    parser.add_argument("--brama-dir", type=Path, required=True)
    parser.add_argument("--spot-shapefile", type=Path, required=True)
    parser.add_argument("--spot-dem", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / RESULT_RELATIVE_PATH,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = run_experiment(
        spec_path=args.spec.resolve(),
        brama_dir=args.brama_dir.resolve(),
        spot_shapefile=args.spot_shapefile.resolve(),
        spot_dem=args.spot_dem.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "counts": result["counts"],
                "artifact_identity_sha256": result["artifact_identity_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
