"""Offline AvaFrame com1DFA plugin with normalized, replayable outputs."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from avycore.engines import (
    AVAFRAME_COM1DFA,
    ENGINE_CONTRACT_SCHEMA_VERSION,
    NORMALIZED_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    AvailabilityStatus,
    EngineAvailability,
    EngineRegistry,
    EngineRunRequest,
    EngineSelectionError,
    EngineStage,
    ExecutionBoundary,
    GridContract,
    MaskContract,
    NormalizedRunoutResult,
    OutputQuantity,
    RasterField,
    RunProvenance,
    SelectionPolicy,
    UnsupportedOutput,
    VectorField,
    build_result,
    canonical_json_bytes,
    sha256_of_manifest,
)

from . import process
from .process import (
    ExternalModelProcessError,
    file_sha256,
    probe_python_distribution,
    run_isolated_worker,
    verify_artifact,
)


WORKER_SCHEMA_VERSION = "avycore-avaframe-worker-v1"
SIMILARITY_WORKER_SCHEMA_VERSION = "avycore-avaframe-similarity-worker-v1"

# com1DFA solves a depth-averaged dense-flow problem, so quantities belonging to
# the energy-line family are absent by construction rather than merely not
# requested.  Declaring them keeps a cross-engine comparison able to say *why* a
# metric is missing instead of reporting a silent gap.
UNSUPPORTED_COM1DFA_OUTPUTS = (
    UnsupportedOutput(
        quantity=OutputQuantity.ENERGY_LINE_HEIGHT,
        reason="com1DFA integrates depth-averaged flow equations and publishes no energy-line height.",
    ),
    UnsupportedOutput(
        quantity=OutputQuantity.TRAVEL_ANGLE,
        reason=(
            "com1DFA can export a peak travel angle (pta), and it is not the same quantity as "
            "Flow-Py's fpTravelAngleMax. Both are arctan(drop / horizontal path length), but com1DFA "
            "divides by each particle's own realized trajectory length integrated over the "
            "simulation and takes a maximum over particles and over time, while com4FlowPy divides "
            "by the shortest 8-connected raster path from the release cell and takes a maximum over "
            "release cells with no time dimension. Publishing them as one quantity would compare a "
            "time-peak of a dynamics-dependent trajectory against a static shortest-path minimum. "
            "See docs/runout-engines.md section 2.1."
        ),
    ),
    UnsupportedOutput(
        quantity=OutputQuantity.ARRIVAL_TIME,
        reason="com1DFA publishes no arrival-time raster in the result types this adapter requests.",
    ),
)


@dataclass(frozen=True)
class AvaFrameSimilarityBenchmarkRun:
    """Validated analytical benchmark bundle returned by the offline adapter."""

    result_id: str
    bundle_path: Path
    report: dict[str, Any]


def _artifact(path: Path, *, uri: str, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        uri=uri,
        sha256=file_sha256(path),
        byte_size=path.stat().st_size,
        media_type=media_type,
    )


def _inline_hash(name: str, value: object, unit: str | None) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"name": name, "unit": unit, "value": value})
    ).hexdigest()


def _adapter_sha256() -> str:
    paths = (
        Path(__file__).resolve(),
        Path(__file__).with_name("_avaframe_worker.py").resolve(),
        Path(__file__).with_name("_avaframe_similarity_worker.py").resolve(),
    )
    return sha256_of_manifest({path.name: file_sha256(path) for path in paths})


@dataclass(frozen=True)
class AvaFrameCom1DFAAdapter:
    """Version-bound com1DFA adapter; no AvaFrame import occurs in this process."""

    python_executable: str | Path
    timeout_seconds: float = 600.0

    @property
    def descriptor(self):
        return AVAFRAME_COM1DFA


    def replay_identity(self) -> dict[str, str]:
        """Adapter-side identity an input-keyed cache needs *before* a run.

        The first two are the fields every result of this adapter also records in
        its provenance, so a restored bundle can be checked against them. The
        third is not: ``process.py`` holds the subprocess launch, isolation flags
        and environment this adapter executes through, and editing it can change
        what the engine does. Including it only ever makes the key stricter, so a
        cache cannot outlive a change to the plumbing it ran on.
        """

        return {
            "adapter_version": self.descriptor.adapter_version,
            "adapter_sha256": _adapter_sha256(),
            "process_sha256": file_sha256(Path(process.__file__).resolve()),
        }

    def availability(self) -> EngineAvailability:
        availability = probe_python_distribution(
            self.python_executable,
            engine_id=self.descriptor.engine_id,
            distribution="avaframe",
            import_name="avaframe.com1DFA.com1DFA",
        )
        if (
            availability.status == AvailabilityStatus.AVAILABLE
            and availability.detected_version != self.descriptor.implementation_version
        ):
            return EngineAvailability(
                engine_id=self.descriptor.engine_id,
                status=AvailabilityStatus.MISCONFIGURED,
                reason=(
                    f"Adapter requires AvaFrame {self.descriptor.implementation_version}, "
                    f"but detected {availability.detected_version}."
                ),
                detected_version=availability.detected_version,
                executable_sha256=availability.executable_sha256,
            )
        return availability

    def run_similarity_benchmark(
        self,
        *,
        source_inputs: str | Path,
        acceptance_path: str | Path,
        output_root: str | Path,
    ) -> AvaFrameSimilarityBenchmarkRun:
        """Run the locked upstream ``avaSimilaritySol`` case in isolation.

        The acceptance artifact and every upstream input are hash/size checked
        before the numerical worker starts.  A scientific threshold failure is
        retained as a result bundle; malformed or untraceable output fails
        closed as a process error.
        """

        availability = self.availability()
        if availability.status != AvailabilityStatus.AVAILABLE:
            raise ExternalModelProcessError(
                "engine_unavailable",
                f"{self.descriptor.engine_id} is {availability.status.value}: {availability.reason}",
            )
        acceptance_source = Path(acceptance_path).resolve()
        if not acceptance_source.is_file():
            raise ExternalModelProcessError(
                "missing_acceptance_artifact",
                f"Analytical acceptance artifact is missing: {acceptance_source}",
            )
        try:
            acceptance = json.loads(acceptance_source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalModelProcessError(
                "invalid_acceptance_artifact", f"Acceptance artifact is invalid: {exc}"
            ) from exc
        if (
            acceptance.get("schema") != "avycore-avaframe-analytical-acceptance-v1"
            or acceptance.get("locked_before_first_run") is not True
            or acceptance.get("engine", {}).get("version")
            != self.descriptor.implementation_version
        ):
            raise ExternalModelProcessError(
                "invalid_acceptance_artifact",
                "Acceptance schema, lock state, or AvaFrame version is invalid.",
            )
        source_root = Path(source_inputs).resolve()
        source_files = acceptance.get("upstream_inputs", {}).get("files")
        if not isinstance(source_files, dict) or not source_files:
            raise ExternalModelProcessError(
                "invalid_acceptance_artifact", "Acceptance artifact has no upstream input manifest."
            )

        verified: dict[str, Path] = {}
        for relative_name, identity in sorted(source_files.items()):
            relative = Path(relative_name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ExternalModelProcessError(
                    "invalid_acceptance_artifact", "Upstream input manifest contains an unsafe path."
                )
            try:
                expected_hash = str(identity["sha256"])
                expected_size = int(identity["byte_size"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExternalModelProcessError(
                    "invalid_acceptance_artifact",
                    f"Upstream identity is invalid for {relative_name!r}.",
                ) from exc
            verified[relative_name] = verify_artifact(
                source_root / relative,
                expected_hash,
                expected_size,
            )

        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        acceptance_sha256 = file_sha256(acceptance_source)
        adapter_sha256 = _adapter_sha256()
        with tempfile.TemporaryDirectory(prefix="avaframe-similarity-", dir=root) as temp_name:
            work = Path(temp_name)
            copied_acceptance = work / "acceptance.json"
            shutil.copyfile(acceptance_source, copied_acceptance)
            copied_inputs = work / "verified-inputs"
            for relative_name, source in verified.items():
                target = copied_inputs / relative_name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            worker_request = {
                "schema_version": SIMILARITY_WORKER_SCHEMA_VERSION,
                "expected_engine_version": self.descriptor.implementation_version,
                "acceptance_path": str(copied_acceptance),
                "acceptance_sha256": acceptance_sha256,
                "source_inputs_path": str(copied_inputs),
                "python_executable_sha256": availability.executable_sha256,
                "adapter_sha256": adapter_sha256,
            }
            request_path = work / "worker-request.json"
            request_path.write_bytes(canonical_json_bytes(worker_request) + b"\n")
            normalized = work / "normalized"
            capture = run_isolated_worker(
                self.python_executable,
                Path(__file__).with_name("_avaframe_similarity_worker.py"),
                (str(request_path), str(normalized)),
                cwd=work,
                timeout_seconds=self.timeout_seconds,
            )
            if capture.executable_sha256 != availability.executable_sha256:
                raise ExternalModelProcessError(
                    "executable_identity_changed",
                    "AvaFrame Python executable changed between version probe and benchmark run.",
                )
            report = self._validate_similarity_bundle(
                normalized,
                acceptance=acceptance,
                acceptance_sha256=acceptance_sha256,
                executable_sha256=capture.executable_sha256,
                adapter_sha256=adapter_sha256,
            )
            destination = root / report["result_id"]
            if destination.exists():
                existing_path = destination / "benchmark-result.json"
                if not existing_path.is_file():
                    raise FileExistsError(
                        f"Existing analytical result directory is incomplete: {destination}"
                    )
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
                existing_identity = {
                    key: value
                    for key, value in existing.items()
                    if key not in {"result_id", "upstream_execution_label"}
                }
                report_identity = {
                    key: value
                    for key, value in report.items()
                    if key not in {"result_id", "upstream_execution_label"}
                }
                if existing_identity != report_identity:
                    raise FileExistsError(f"Analytical result identity collision at {destination}")
                self._validate_similarity_bundle(
                    destination,
                    acceptance=acceptance,
                    acceptance_sha256=acceptance_sha256,
                    executable_sha256=capture.executable_sha256,
                    adapter_sha256=adapter_sha256,
                )
                return AvaFrameSimilarityBenchmarkRun(
                    result_id=report["result_id"],
                    bundle_path=destination,
                    report=report,
                )
            normalized.replace(destination)
            return AvaFrameSimilarityBenchmarkRun(
                result_id=report["result_id"],
                bundle_path=destination,
                report=report,
            )

    def _validate_similarity_bundle(
        self,
        bundle: Path,
        *,
        acceptance: dict[str, Any],
        acceptance_sha256: str,
        executable_sha256: str,
        adapter_sha256: str,
    ) -> dict[str, Any]:
        try:
            report = json.loads((bundle / "benchmark-result.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExternalModelProcessError(
                "invalid_output", f"Analytical benchmark report is invalid: {exc}"
            ) from exc
        if (
            report.get("schema") != "avycore-avaframe-analytical-result-v1"
            or report.get("benchmark_id") != acceptance.get("benchmark_id")
            or report.get("acceptance_sha256") != acceptance_sha256
            or report.get("engine", {}).get("version")
            != self.descriptor.implementation_version
            or report.get("engine", {}).get("python_executable_sha256") != executable_sha256
            or report.get("engine", {}).get("adapter_sha256") != adapter_sha256
        ):
            raise ExternalModelProcessError(
                "invalid_output", "Analytical benchmark identity or provenance is invalid."
            )
        for name, identity in report.get("artifacts", {}).items():
            if name not in {"comparison_fields", "configuration", "environment"}:
                raise ExternalModelProcessError(
                    "invalid_output", f"Analytical result has an unexpected artifact {name!r}."
                )
            filename = {
                "comparison_fields": "comparison-fields.npz",
                "configuration": "configuration.json",
                "environment": "environment.json",
            }[name]
            verify_artifact(bundle / filename, identity["sha256"], identity["byte_size"])
        if set(report.get("artifacts", {})) != {
            "comparison_fields",
            "configuration",
            "environment",
        }:
            raise ExternalModelProcessError(
                "invalid_output", "Analytical result artifact manifest is incomplete."
            )
        upstream_label = report.get("upstream_execution_label", {})
        if (
            set(upstream_label)
            != {"simulation_hash", "simulation_name", "identity_role"}
            or not upstream_label["simulation_hash"]
            or not upstream_label["simulation_name"]
            or "excluded_from_result_id" not in upstream_label["identity_role"]
        ):
            raise ExternalModelProcessError(
                "invalid_output", "Analytical benchmark execution label is invalid."
            )
        result_core = {
            key: value
            for key, value in report.items()
            if key not in {"result_id", "upstream_execution_label"}
        }
        expected_result_id = hashlib.sha256(canonical_json_bytes(result_core)).hexdigest()
        if report.get("result_id") != expected_result_id:
            raise ExternalModelProcessError(
                "invalid_output", "Analytical benchmark result identity is invalid."
            )
        expected_metrics = set(acceptance.get("acceptance_metrics", {})) - {
            "grid",
            "units",
            "crs",
            "masks",
            "domain_boundary",
        }
        if set(report.get("metrics", {})) != expected_metrics:
            raise ExternalModelProcessError(
                "invalid_output", "Analytical benchmark metric set differs from preregistration."
            )
        return report

    def run_runout(
        self,
        request: EngineRunRequest,
        *,
        output_root: str | Path,
    ) -> NormalizedRunoutResult:
        registry = EngineRegistry()
        registry.register(self)
        selected, selection = registry.select(
            request,
            policy=SelectionPolicy(engine_order=(self.descriptor.engine_id,)),
        )
        if selected is not self:
            raise AssertionError("Engine registry returned a different plugin instance.")
        if request.stage != EngineStage.RUNOUT:
            raise EngineSelectionError("AvaFrame adapter only runs runout requests.", selection)
        if request.seed is None:
            raise ValueError("AvaFrame deterministic replay requires an explicit non-negative seed.")

        inputs = request.input_map()
        parameters = {
            name: inputs[name].value
            for name in (
                "release_thickness",
                "release_density",
                "voellmy_mu",
                "voellmy_xi",
                "entrainment_enabled",
                "simulation_time",
                "time_step",
            )
        }
        self._validate_parameters(parameters)
        terrain = inputs["terrain_dem"]
        release = inputs["release_area"]
        if terrain.grid is None or terrain.mask is None or release.crs is None:
            raise ValueError("Spatial input metadata was not retained after request validation.")
        if not terrain.grid.crs.projected:
            raise ValueError("AvaFrame com1DFA requires a projected metre-based DEM and release CRS.")
        if terrain.grid.crs != release.crs:
            raise ValueError("DEM and release-area CRS contracts must match exactly.")

        terrain_path = verify_artifact(
            terrain.artifact.uri, terrain.artifact.sha256, terrain.artifact.byte_size
        )
        terrain_mask_path = verify_artifact(
            terrain.mask.artifact.uri,
            terrain.mask.artifact.sha256,
            terrain.mask.artifact.byte_size,
        )
        release_path = verify_artifact(
            release.artifact.uri, release.artifact.sha256, release.artifact.byte_size
        )

        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        availability = self.availability()
        if availability.status != AvailabilityStatus.AVAILABLE:
            raise ExternalModelProcessError(
                "engine_unavailable",
                f"{self.descriptor.engine_id} is {availability.status.value}: {availability.reason}",
            )
        with tempfile.TemporaryDirectory(prefix="avaframe-com1dfa-", dir=root) as temp_name:
            work = Path(temp_name)
            copied_dem = work / "terrain-dem.tif"
            copied_mask = work / "terrain-mask.npy"
            copied_release = work / "release.geojson"
            shutil.copyfile(terrain_path, copied_dem)
            shutil.copyfile(terrain_mask_path, copied_mask)
            shutil.copyfile(release_path, copied_release)
            worker_request = {
                "schema_version": WORKER_SCHEMA_VERSION,
                "expected_engine_version": self.descriptor.implementation_version,
                "terrain_dem_path": str(copied_dem),
                "terrain_mask_path": str(copied_mask),
                "release_geojson_path": str(copied_release),
                "terrain_grid": terrain.grid.model_dump(mode="json"),
                "release_crs": release.crs.model_dump(mode="json"),
                "parameters": {
                    **parameters,
                    "seed": request.seed,
                    "mesh_cell_size": terrain.grid.cell_size_x_m,
                },
            }
            request_path = work / "worker-request.json"
            request_path.write_bytes(canonical_json_bytes(worker_request) + b"\n")
            normalized = work / "normalized"
            capture = run_isolated_worker(
                self.python_executable,
                Path(__file__).with_name("_avaframe_worker.py"),
                (str(request_path), str(normalized)),
                cwd=work,
                timeout_seconds=self.timeout_seconds,
            )
            (normalized / "selection.json").write_bytes(
                canonical_json_bytes(selection.model_dump(mode="json")) + b"\n"
            )
            result = self._build_result(
                request=request,
                normalized=normalized,
                terrain_grid=terrain.grid,
                availability=availability,
                executable_sha256=capture.executable_sha256,
                selection_sha256=selection.selection_sha256,
            )
            result_path = normalized / "result.json"
            result_path.write_bytes(canonical_json_bytes(result.model_dump(mode="json")) + b"\n")
            destination = root / result.result_id
            if destination.exists():
                existing_path = destination / "result.json"
                if not existing_path.is_file():
                    raise FileExistsError(f"Existing result directory is incomplete: {destination}")
                existing = NormalizedRunoutResult.model_validate_json(existing_path.read_bytes())
                if existing != result:
                    raise FileExistsError(f"Result identity collision at {destination}")
                return existing
            normalized.replace(destination)
            return result

    @staticmethod
    def _validate_parameters(parameters: dict[str, Any]) -> None:
        positive = (
            "release_thickness",
            "release_density",
            "voellmy_mu",
            "voellmy_xi",
            "simulation_time",
            "time_step",
        )
        for name in positive:
            value = parameters[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be an explicit positive number.")
        if float(parameters["time_step"]) > float(parameters["simulation_time"]):
            raise ValueError("time_step cannot exceed simulation_time.")
        if parameters["entrainment_enabled"] is not False:
            raise ValueError("This adapter slice supports only explicit entrainment_enabled=false.")

    def _build_result(
        self,
        *,
        request: EngineRunRequest,
        normalized: Path,
        terrain_grid: GridContract,
        availability: EngineAvailability,
        executable_sha256: str,
        selection_sha256: str,
    ) -> NormalizedRunoutResult:
        metadata_path = normalized / "worker-metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("schema_version") != WORKER_SCHEMA_VERSION:
            raise ExternalModelProcessError("invalid_output", "Worker metadata schema is invalid.")
        if metadata.get("engine_version") != self.descriptor.implementation_version:
            raise ExternalModelProcessError("invalid_output", "Worker engine version is invalid.")
        self._validate_normalized_outputs(
            normalized,
            metadata,
            selection_sha256=selection_sha256,
        )

        grid_payload = metadata["grid"]
        grid = GridContract(
            crs={
                **terrain_grid.crs.model_dump(mode="json"),
                "definition": grid_payload["crs"],
            },
            shape=tuple(grid_payload["shape"]),
            affine_transform=tuple(grid_payload["transform"]),
            cell_size_x_m=grid_payload["cell_size_x_m"],
            cell_size_y_m=grid_payload["cell_size_y_m"],
            origin_semantics="upper_left_outer_corner",
        )
        mask_artifact = _artifact(
            normalized / "mask.npy", uri="mask.npy", media_type="application/x-npy"
        )
        mask = MaskContract(
            artifact=mask_artifact,
            valid_cells=metadata["valid_cells"],
            masked_cells=metadata["masked_cells"],
            combined_from=("terrain_dem", "terrain_mask", "avaframe_peak_nodata"),
        )

        quantities = {
            "runout": (OutputQuantity.RUNOUT_EXTENT, "1", "bool", "Positive peak flow-thickness footprint."),
            "depth": (OutputQuantity.FLOW_DEPTH, "m", "float32", "AvaFrame pft peak flow thickness."),
            "velocity": (OutputQuantity.FLOW_VELOCITY, "m s-1", "float32", "AvaFrame pfv peak flow velocity."),
            "pressure": (OutputQuantity.FLOW_PRESSURE, "kPa", "float32", "AvaFrame ppr peak flow pressure."),
        }
        rasters: dict[str, RasterField] = {}
        for name, (quantity, unit, dtype, semantics) in quantities.items():
            path = normalized / f"{name}.npy"
            valid_min, valid_max = metadata["ranges"][name]
            rasters[name] = RasterField(
                quantity=quantity,
                unit=unit,
                artifact=_artifact(path, uri=path.name, media_type="application/x-npy"),
                mask=mask,
                grid=grid,
                dtype=dtype,
                valid_min=valid_min,
                valid_max=valid_max,
                semantics=semantics,
            )

        polygon_path = normalized / "runout.geojson"
        polygons = VectorField(
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="1",
            artifact=_artifact(
                polygon_path, uri=polygon_path.name, media_type="application/geo+json"
            ),
            crs=grid.crs,
            geometry_types=tuple(metadata["geometry_types"] or ["Polygon"]),
            feature_count=metadata["runout_feature_count"],
            semantics="Vectorization of cells with positive AvaFrame peak flow thickness.",
        )
        configuration_artifact = _artifact(
            normalized / "configuration.json",
            uri="configuration.json",
            media_type="application/json",
        )
        environment_artifact = _artifact(
            normalized / "environment.json", uri="environment.json", media_type="application/json"
        )
        metadata_artifact = _artifact(
            metadata_path, uri="worker-metadata.json", media_type="application/json"
        )
        selection_artifact = _artifact(
            normalized / "selection.json", uri="selection.json", media_type="application/json"
        )
        output_manifest = {
            name: file_sha256(path)
            for name, path in sorted(
                {
                    "configuration": normalized / "configuration.json",
                    "depth": normalized / "depth.npy",
                    "environment": normalized / "environment.json",
                    "mask": normalized / "mask.npy",
                    "metadata": metadata_path,
                    "polygons": polygon_path,
                    "pressure": normalized / "pressure.npy",
                    "runout": normalized / "runout.npy",
                    "selection": normalized / "selection.json",
                    "velocity": normalized / "velocity.npy",
                }.items()
            )
        }
        input_manifest = {
            item.name: {
                "source_sha256": item.source_sha256,
                "status": item.status,
                "unit": item.unit,
                "value": item.value,
                "artifact_sha256": item.artifact.sha256 if item.artifact else None,
                "mask_sha256": item.mask.artifact.sha256 if item.mask else None,
            }
            for item in sorted(request.inputs, key=lambda value: value.name)
        }
        warnings = (
            ("Positive runout touches the computational-domain boundary; extent is truncated.",)
            if metadata["boundary_touched"]
            else ()
        )
        content = {
            "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
            "disclaimer": request.research_disclaimer,
            "site_id": request.site_id,
            "stage": EngineStage.RUNOUT,
            "regime": request.regime,
            "provenance": RunProvenance(
                engine_id=self.descriptor.engine_id,
                engine_version=availability.detected_version,
                adapter_version=self.descriptor.adapter_version,
                license_spdx=self.descriptor.license_spdx,
                execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
                executable_sha256=executable_sha256,
                environment_sha256=metadata["environment_sha256"],
                adapter_sha256=_adapter_sha256(),
                selection_sha256=selection_sha256,
                configuration_sha256=metadata["configuration_sha256"],
                input_manifest_sha256=sha256_of_manifest(input_manifest),
                output_manifest_sha256=sha256_of_manifest(output_manifest),
                scenario_sha256=request.scenario_sha256,
                seed=request.seed,
                source_urls=(self.descriptor.source_url,),
            ),
            "validation": self.descriptor.validation,
            "uncertainty": (),
            "warnings": warnings,
            "limitations": (
                *self.descriptor.limitations,
                "No bounded sensitivity ensemble was supplied; this result has no propagated uncertainty bounds.",
                f"Configuration artifact: {configuration_artifact.uri}; environment artifact: {environment_artifact.uri}; selection artifact: {selection_artifact.uri}; worker metadata: {metadata_artifact.uri}.",
            ),
            "runout_extent": rasters["runout"],
            "runout_polygons": polygons,
            "flow_depth": rasters["depth"],
            "flow_velocity": rasters["velocity"],
            "flow_pressure": rasters["pressure"],
            "energy_line_height": None,
            "travel_angle": None,
            "arrival_time": None,
            "unsupported_outputs": UNSUPPORTED_COM1DFA_OUTPUTS,
            "runout_area_m2": metadata["runout_area_m2"],
            "aoi_status": (
                "truncated_at_domain" if metadata["boundary_touched"] else "complete_within_domain"
            ),
        }
        return build_result(NormalizedRunoutResult, content)

    @staticmethod
    def _validate_normalized_outputs(
        normalized: Path,
        metadata: dict[str, Any],
        *,
        selection_sha256: str,
    ) -> None:
        try:
            shape = tuple(int(value) for value in metadata["grid"]["shape"])
            mask = np.load(normalized / "mask.npy", allow_pickle=False)
            runout = np.load(normalized / "runout.npy", allow_pickle=False)
            fields = {
                name: np.load(normalized / f"{name}.npy", allow_pickle=False)
                for name in ("depth", "velocity", "pressure")
            }
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ExternalModelProcessError(
                "invalid_output", f"Normalized worker arrays could not be validated: {exc}"
            ) from exc
        if mask.dtype != np.dtype("bool") or runout.dtype != np.dtype("bool"):
            raise ExternalModelProcessError("invalid_output", "Runout and mask arrays must be bool.")
        if mask.shape != shape or runout.shape != shape:
            raise ExternalModelProcessError("invalid_output", "Runout or mask shape is invalid.")
        valid = ~mask
        if not np.any(valid):
            raise ExternalModelProcessError(
                "invalid_output", "Normalized worker output has no valid cells."
            )
        if int(np.count_nonzero(valid)) != metadata.get("valid_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker valid-cell count is invalid.")
        if int(np.count_nonzero(mask)) != metadata.get("masked_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker masked-cell count is invalid.")
        for name, values in fields.items():
            if values.dtype != np.dtype("float32") or values.shape != shape:
                raise ExternalModelProcessError(
                    "invalid_output", f"Normalized {name} array has an invalid dtype or shape."
                )
            if np.any(~np.isfinite(values[valid])) or np.any(values[valid] < 0.0):
                raise ExternalModelProcessError(
                    "invalid_output", f"Normalized {name} contains invalid valid-domain values."
                )
            if np.any(values[mask] != 0.0):
                raise ExternalModelProcessError(
                    "invalid_output", f"Normalized {name} does not zero storage under its mask."
                )
            actual_range = [float(np.min(values[valid])), float(np.max(values[valid]))]
            if actual_range != metadata.get("ranges", {}).get(name):
                raise ExternalModelProcessError(
                    "invalid_output", f"Normalized {name} range conflicts with worker metadata."
                )
        if not np.array_equal(runout, (fields["depth"] > 0.0) & valid):
            raise ExternalModelProcessError(
                "invalid_output", "Runout extent conflicts with positive peak flow thickness."
            )
        configuration = json.loads((normalized / "configuration.json").read_text(encoding="utf-8"))
        if sha256_of_manifest(configuration) != metadata.get("configuration_sha256"):
            raise ExternalModelProcessError(
                "invalid_output", "Configuration artifact conflicts with worker metadata."
            )
        environment = json.loads((normalized / "environment.json").read_text(encoding="utf-8"))
        if sha256_of_manifest(environment) != metadata.get("environment_sha256"):
            raise ExternalModelProcessError(
                "invalid_output", "Environment artifact conflicts with worker metadata."
            )
        selection = json.loads((normalized / "selection.json").read_text(encoding="utf-8"))
        if selection.get("selection_sha256") != selection_sha256:
            raise ExternalModelProcessError(
                "invalid_output", "Selection artifact conflicts with deterministic selection."
            )


__all__ = [
    "AvaFrameCom1DFAAdapter",
    "UNSUPPORTED_COM1DFA_OUTPUTS",
    "AvaFrameSimilarityBenchmarkRun",
    "SIMILARITY_WORKER_SCHEMA_VERSION",
    "WORKER_SCHEMA_VERSION",
]
