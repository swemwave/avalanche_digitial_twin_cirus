"""Offline Flow-Py runout adapters with normalized, replayable outputs.

Two upstream implementations of the same published model exist:

* the archived standalone Flow-Py distribution (GPL-3.0-or-later), and
* AvaFrame's ``com4FlowPy`` port (EUPL-1.2).

They are kept as separate engine identities on purpose.  ``AvaFrameCom4FlowPyAdapter``
executes the AvaFrame port and records the hashes of the module files that ran,
so a normalized result can be traced back to the implementation that produced it.
``UpstreamFlowPyAdapter`` is the identity for the standalone distribution and
stays fail-closed; it never falls back to the AvaFrame port.
"""

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
    AVAFRAME_FLOWPY,
    NORMALIZED_RESULT_SCHEMA_VERSION,
    UPSTREAM_FLOWPY,
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


WORKER_SCHEMA_VERSION = "avycore-flowpy-worker-v1"

# The tagged v1.0.3 release of the standalone distribution reassigns ``argv`` to a
# hardcoded example inside its ``__main__`` block, so the released command line
# discards whatever an adapter passes it.  Only the later untagged master commit
# comments that line out.  Both identities are recorded so an operator-supplied
# checkout is recognised rather than guessed at.
#
# The recorded digests are over LF-normalized text, which is what git stores and
# therefore what the same commit yields on every platform.  Hashing raw bytes
# would reject a perfectly valid checkout on Windows, where git hands the file
# back with CRLF line endings.
UPSTREAM_FLOWPY_REVIEWED_COMMITS: dict[str, dict[str, str]] = {
    "7b061599355cef584491d69eae2686307d286901": {
        "ref": "v1.0.3",
        "main_py_sha256": "200ec899a12a4c1eebbbd6b3d0c49efd2b10f25418650312d3d16cf351169e76",
        "status": "rejected",
        "reason": (
            "Released tag v1.0.3 reassigns argv to a hardcoded Osttirol example in main.py, "
            "so the command line ignores adapter arguments and cannot be driven reproducibly."
        ),
    },
    "27ad81d3e804e4e9d85a9773fca10ee7dc428183": {
        "ref": "master@2022-06-20",
        "main_py_sha256": "6171fd592acc83ba4285e2de2b72456334c72604f9c65e1b90f09c2e7d4096f1",
        "status": "reviewed_untagged",
        "reason": (
            "The argv reassignment is commented out on this untagged master commit, but no "
            "upstream release carries the fix and the repository is archived read-only."
        ),
    },
}

# Upstream tiles the domain and merges overlapping tiles with max/sum reductions.
# A single tile that comfortably contains the grid removes that reduction from
# the numerical answer.  Both values are metres, matching com4FlowPyCfg.ini.
DEFAULT_TILE_SIZE_M = 15000.0
DEFAULT_TILE_OVERLAP_M = 5000.0

UNSUPPORTED_FLOWPY_OUTPUTS = (
    UnsupportedOutput(
        quantity=OutputQuantity.FLOW_DEPTH,
        reason="Flow-Py routes a dimensionless flux and solves no depth-averaged mass balance, so it produces no flow depth.",
    ),
    UnsupportedOutput(
        quantity=OutputQuantity.FLOW_VELOCITY,
        reason=(
            "Flow-Py produces an energy-line height. Upstream documents the sliding-block bound "
            "max_v = sqrt(2 g z_delta), which is a limit rather than a simulated flow velocity."
        ),
    ),
    UnsupportedOutput(
        quantity=OutputQuantity.FLOW_PRESSURE,
        reason="Flow-Py has no flow density or depth, so no impact pressure can be derived from its output.",
    ),
    UnsupportedOutput(
        quantity=OutputQuantity.ARRIVAL_TIME,
        reason="Flow-Py is a time-independent routing model and computes no arrival time.",
    ),
)


def normalized_text_sha256(path: str | Path) -> str:
    """SHA-256 over LF-normalized file text.

    Upstream identities are recorded once and checked on whatever platform the
    operator runs.  Git rewrites line endings on checkout, so hashing raw bytes
    would make the same commit produce different digests on Windows and Linux.
    Normalizing first is what makes the recorded digest a property of the commit
    rather than of the checkout.
    """

    raw = Path(path).read_bytes()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def _artifact(path: Path, *, uri: str, media_type: str) -> ArtifactRef:
    return ArtifactRef(
        uri=uri,
        sha256=file_sha256(path),
        byte_size=path.stat().st_size,
        media_type=media_type,
    )


def _adapter_sha256() -> str:
    paths = (
        Path(__file__).resolve(),
        Path(__file__).with_name("_flowpy_worker.py").resolve(),
    )
    return sha256_of_manifest({path.name: file_sha256(path) for path in paths})


@dataclass(frozen=True)
class UpstreamFlowPyAdapter:
    """Identity for the archived standalone Flow-Py distribution.

    A checkout is accepted only when its ``main.py`` matches a reviewed commit
    whose command line actually honours its arguments.  Nothing here ever routes
    a request to the AvaFrame port instead.
    """

    checkout_path: str | Path | None = None
    python_executable: str | Path | None = None

    @property
    def descriptor(self):
        return UPSTREAM_FLOWPY

    def availability(self) -> EngineAvailability:
        engine_id = self.descriptor.engine_id
        if self.checkout_path is None:
            return EngineAvailability(
                engine_id=engine_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=(
                    "No standalone Flow-Py checkout is configured. The upstream repository is "
                    "archived read-only and its released v1.0.3 command line ignores its arguments."
                ),
            )
        root = Path(self.checkout_path).resolve()
        main_py = root / "main.py"
        if not main_py.is_file():
            return EngineAvailability(
                engine_id=engine_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=f"Configured Flow-Py checkout has no main.py: {root}",
            )
        try:
            digest = normalized_text_sha256(main_py)
        except OSError as exc:
            return EngineAvailability(
                engine_id=engine_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=f"Configured Flow-Py main.py could not be read: {exc}",
            )
        for commit, record in UPSTREAM_FLOWPY_REVIEWED_COMMITS.items():
            if record["main_py_sha256"] and record["main_py_sha256"] == digest:
                return EngineAvailability(
                    engine_id=engine_id,
                    status=AvailabilityStatus.MISCONFIGURED,
                    reason=(
                        f"Flow-Py checkout matches reviewed commit {commit} ({record['ref']}), "
                        f"which is {record['status']}: {record['reason']}"
                    ),
                    detected_version=self.descriptor.implementation_version,
                    executable_sha256=digest,
                )
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=(
                f"Flow-Py main.py LF-normalized SHA-256 {digest} matches no reviewed upstream "
                "commit. Add the exact commit identity through code review before enabling "
                "execution."
            ),
            executable_sha256=digest,
        )

    def run_runout(self, *args: Any, **kwargs: Any) -> NormalizedRunoutResult:
        raise ExternalModelProcessError("adapter_disabled", self.availability().reason)


@dataclass(frozen=True)
class AvaFrameCom4FlowPyAdapter:
    """Version-bound com4FlowPy adapter; no AvaFrame import occurs in this process."""

    python_executable: str | Path
    timeout_seconds: float = 1800.0

    @property
    def descriptor(self):
        return AVAFRAME_FLOWPY


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
            import_name="avaframe.com4FlowPy.com4FlowPy",
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
            raise EngineSelectionError("com4FlowPy adapter only runs runout requests.", selection)

        inputs = request.input_map()
        parameters = {
            name: inputs[name].value
            for name in (
                "alpha_angle",
                "flowpy_exponent",
                "flux_threshold",
                "max_energy_line_height",
            )
        }
        self._validate_parameters(parameters)

        terrain = inputs["terrain_dem"]
        release = inputs["release_area"]
        if terrain.grid is None or terrain.mask is None:
            raise ValueError("Spatial input metadata was not retained after request validation.")
        if release.grid is None or release.mask is None:
            raise ValueError("The release raster requires an explicit grid and mask.")
        if not terrain.grid.crs.projected:
            raise ValueError("com4FlowPy requires a projected metre-based DEM and release grid.")
        if terrain.grid != release.grid:
            raise ValueError("DEM and release-raster grid contracts must match exactly.")

        terrain_path = verify_artifact(
            terrain.artifact.uri, terrain.artifact.sha256, terrain.artifact.byte_size
        )
        terrain_mask_path = verify_artifact(
            terrain.mask.artifact.uri, terrain.mask.artifact.sha256, terrain.mask.artifact.byte_size
        )
        release_path = verify_artifact(
            release.artifact.uri, release.artifact.sha256, release.artifact.byte_size
        )
        release_mask_path = verify_artifact(
            release.mask.artifact.uri, release.mask.artifact.sha256, release.mask.artifact.byte_size
        )

        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        availability = self.availability()
        if availability.status != AvailabilityStatus.AVAILABLE:
            raise ExternalModelProcessError(
                "engine_unavailable",
                f"{self.descriptor.engine_id} is {availability.status.value}: {availability.reason}",
            )

        tile_size_m, tile_overlap_m = self._tiling(terrain.grid)
        with tempfile.TemporaryDirectory(prefix="avaframe-com4flowpy-", dir=root) as temp_name:
            work = Path(temp_name)
            copied_dem = work / "terrain-dem.tif"
            copied_terrain_mask = work / "terrain-mask.npy"
            copied_release = work / "release.npy"
            copied_release_mask = work / "release-mask.npy"
            shutil.copyfile(terrain_path, copied_dem)
            shutil.copyfile(terrain_mask_path, copied_terrain_mask)
            shutil.copyfile(release_path, copied_release)
            shutil.copyfile(release_mask_path, copied_release_mask)
            worker_request = {
                "schema_version": WORKER_SCHEMA_VERSION,
                "expected_engine_version": self.descriptor.implementation_version,
                "terrain_dem_path": str(copied_dem),
                "terrain_mask_path": str(copied_terrain_mask),
                "release_path": str(copied_release),
                "release_mask_path": str(copied_release_mask),
                "terrain_grid": terrain.grid.model_dump(mode="json"),
                "parameters": {
                    **parameters,
                    "tile_size_m": tile_size_m,
                    "tile_overlap_m": tile_overlap_m,
                },
            }
            request_path = work / "worker-request.json"
            request_path.write_bytes(canonical_json_bytes(worker_request) + b"\n")
            normalized = work / "normalized"
            capture = run_isolated_worker(
                self.python_executable,
                Path(__file__).with_name("_flowpy_worker.py"),
                (str(request_path), str(normalized)),
                cwd=work,
                timeout_seconds=self.timeout_seconds,
            )
            if capture.executable_sha256 != availability.executable_sha256:
                raise ExternalModelProcessError(
                    "executable_identity_changed",
                    "com4FlowPy Python executable changed between version probe and run.",
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
    def _tiling(grid: GridContract) -> tuple[float, float]:
        """Return one tile that contains the whole grid with room to spare.

        Upstream divides ``tileSize`` by the cell size to get tile dimensions in
        cells, so the value is metres.  Choosing a single tile keeps the merge
        step (a max/sum reduction over overlapping tiles) out of the answer.
        """

        span_y = grid.shape[0] * grid.cell_size_y_m
        span_x = grid.shape[1] * grid.cell_size_x_m
        tile_size = max(DEFAULT_TILE_SIZE_M, 2.0 * max(span_x, span_y))
        return float(tile_size), float(min(DEFAULT_TILE_OVERLAP_M, tile_size / 3.0))

    @staticmethod
    def _validate_parameters(parameters: dict[str, Any]) -> None:
        alpha = parameters["alpha_angle"]
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0.0 < float(alpha) < 90.0:
            raise ValueError("alpha_angle must be an explicit angle strictly inside (0, 90) degrees.")
        exponent = parameters["flowpy_exponent"]
        if isinstance(exponent, bool) or not isinstance(exponent, (int, float)) or float(exponent) < 1.0:
            raise ValueError("flowpy_exponent must be an explicit value of at least 1.")
        if float(exponent) != int(exponent):
            raise ValueError("com4FlowPy casts the spreading exponent to an integer; supply a whole number.")
        threshold = parameters["flux_threshold"]
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0.0 < float(threshold) < 1.0
        ):
            raise ValueError("flux_threshold must be an explicit fraction strictly inside (0, 1).")
        max_z = parameters["max_energy_line_height"]
        if isinstance(max_z, bool) or not isinstance(max_z, (int, float)) or float(max_z) <= 0.0:
            raise ValueError("max_energy_line_height must be an explicit positive value in metres.")

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
        self._validate_normalized_outputs(normalized, metadata, selection_sha256=selection_sha256)

        grid_payload = metadata["grid"]
        grid = GridContract(
            crs={
                **terrain_grid.crs.model_dump(mode="json"),
                "definition": grid_payload["crs"]["definition"],
            },
            shape=tuple(grid_payload["shape"]),
            affine_transform=tuple(grid_payload["affine_transform"]),
            cell_size_x_m=grid_payload["cell_size_x_m"],
            cell_size_y_m=grid_payload["cell_size_y_m"],
            origin_semantics="upper_left_outer_corner",
        )
        terrain_mask = MaskContract(
            artifact=_artifact(normalized / "mask.npy", uri="mask.npy", media_type="application/x-npy"),
            valid_cells=metadata["valid_cells"],
            masked_cells=metadata["masked_cells"],
            combined_from=("terrain_dem", "terrain_mask"),
        )
        angle_mask = MaskContract(
            artifact=_artifact(
                normalized / "travel-angle-mask.npy",
                uri="travel-angle-mask.npy",
                media_type="application/x-npy",
            ),
            valid_cells=metadata["travel_angle_valid_cells"],
            masked_cells=metadata["travel_angle_masked_cells"],
            combined_from=("terrain_dem", "terrain_mask", "com4flowpy_unreached_cells"),
        )

        runout_range = metadata["ranges"]["runout"]
        energy_range = metadata["ranges"]["energy_line_height"]
        angle_range = metadata["ranges"]["travel_angle"]
        runout_field = RasterField(
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="1",
            artifact=_artifact(normalized / "runout.npy", uri="runout.npy", media_type="application/x-npy"),
            mask=terrain_mask,
            grid=grid,
            dtype="bool",
            valid_min=runout_range[0],
            valid_max=runout_range[1],
            semantics="Cells reached by routed flux; an unreached valid cell is a modelled zero, not unknown.",
        )
        energy_field = RasterField(
            quantity=OutputQuantity.ENERGY_LINE_HEIGHT,
            unit="m",
            artifact=_artifact(
                normalized / "energy-line-height.npy",
                uri="energy-line-height.npy",
                media_type="application/x-npy",
            ),
            mask=terrain_mask,
            grid=grid,
            dtype="float32",
            valid_min=energy_range[0],
            valid_max=energy_range[1],
            semantics="com4FlowPy z-delta: energy-line height above terrain. Not a flow depth or a velocity.",
        )
        angle_field = RasterField(
            quantity=OutputQuantity.TRAVEL_ANGLE,
            unit="degree",
            artifact=_artifact(
                normalized / "travel-angle.npy", uri="travel-angle.npy", media_type="application/x-npy"
            ),
            mask=angle_mask,
            grid=grid,
            dtype="float32",
            valid_min=angle_range[0],
            valid_max=angle_range[1],
            semantics="com4FlowPy fpTravelAngleMax: maximum flow-path travel angle from a release cell.",
        )
        polygon_path = normalized / "runout.geojson"
        polygons = VectorField(
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="1",
            artifact=_artifact(polygon_path, uri=polygon_path.name, media_type="application/geo+json"),
            crs=grid.crs,
            geometry_types=tuple(metadata["geometry_types"] or ["Polygon"]),
            feature_count=metadata["runout_feature_count"],
            semantics="Vectorization of cells reached by routed com4FlowPy flux.",
        )

        output_manifest = {
            name: file_sha256(normalized / name)
            for name in sorted(
                (
                    "cell-counts.npy",
                    "configuration.json",
                    "energy-line-height.npy",
                    "environment.json",
                    "mask.npy",
                    "runout.geojson",
                    "runout.npy",
                    "selection.json",
                    "straight-line-travel-angle.npy",
                    "travel-angle-mask.npy",
                    "travel-angle.npy",
                    "travel-length.npy",
                    "upstream-implementation.json",
                    "worker-metadata.json",
                )
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
        upstream = json.loads((normalized / "upstream-implementation.json").read_text(encoding="utf-8"))
        warnings: list[str] = []
        if metadata["boundary_touched"]:
            warnings.append(
                "Routed flux reaches the computational-domain boundary; the extent is truncated."
            )
        if metadata["affected_cells"] == metadata["release_cells"]:
            warnings.append(
                "No cell outside the release area was reached; check the angle of reach and terrain."
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
                source_urls=(self.descriptor.source_url, UPSTREAM_FLOWPY.source_url),
            ),
            "validation": self.descriptor.validation,
            "uncertainty": (),
            "warnings": tuple(warnings),
            "limitations": (
                *self.descriptor.limitations,
                f"Executed implementation: {upstream['provider']} from AvaFrame {upstream['avaframe_version']}; "
                f"module inventory SHA-256 {metadata['upstream_implementation_sha256']}.",
                "No bounded sensitivity ensemble was supplied; this result has no propagated uncertainty bounds.",
            ),
            "runout_extent": runout_field,
            "runout_polygons": polygons,
            "flow_depth": None,
            "flow_velocity": None,
            "flow_pressure": None,
            "energy_line_height": energy_field,
            "travel_angle": angle_field,
            "arrival_time": None,
            "unsupported_outputs": UNSUPPORTED_FLOWPY_OUTPUTS,
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
            angle_mask = np.load(normalized / "travel-angle-mask.npy", allow_pickle=False)
            runout = np.load(normalized / "runout.npy", allow_pickle=False)
            energy = np.load(normalized / "energy-line-height.npy", allow_pickle=False)
            angle = np.load(normalized / "travel-angle.npy", allow_pickle=False)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ExternalModelProcessError(
                "invalid_output", f"Normalized worker arrays could not be validated: {exc}"
            ) from exc

        for name, array, dtype in (
            ("mask", mask, "bool"),
            ("travel-angle-mask", angle_mask, "bool"),
            ("runout", runout, "bool"),
            ("energy-line-height", energy, "float32"),
            ("travel-angle", angle, "float32"),
        ):
            if array.dtype != np.dtype(dtype) or array.shape != shape:
                raise ExternalModelProcessError(
                    "invalid_output", f"Normalized {name} array has an invalid dtype or shape."
                )

        valid = ~mask
        if not np.any(valid):
            raise ExternalModelProcessError("invalid_output", "Normalized output has no valid cells.")
        if int(np.count_nonzero(valid)) != metadata.get("valid_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker valid-cell count is invalid.")
        if int(np.count_nonzero(mask)) != metadata.get("masked_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker masked-cell count is invalid.")
        if int(np.count_nonzero(~angle_mask)) != metadata.get("travel_angle_valid_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker travel-angle valid count is invalid.")

        # The travel-angle domain is exactly the reached cells, so a reader can
        # never mistake "no path arrived" for "the angle happened to be zero".
        if not np.array_equal(~angle_mask, runout & valid):
            raise ExternalModelProcessError(
                "invalid_output", "Travel-angle mask does not equal the reached valid domain."
            )
        if np.any(~np.isfinite(energy[valid])) or np.any(energy[valid] < 0.0):
            raise ExternalModelProcessError(
                "invalid_output", "Normalized energy-line height contains invalid valid-domain values."
            )
        if np.any(energy[mask] != 0.0) or np.any(angle[angle_mask] != 0.0):
            raise ExternalModelProcessError(
                "invalid_output", "Normalized fields do not zero their storage under their masks."
            )
        if np.any(runout & ~valid):
            raise ExternalModelProcessError(
                "invalid_output", "Runout extent claims cells outside the valid terrain domain."
            )
        if int(np.count_nonzero(runout)) != metadata.get("affected_cells"):
            raise ExternalModelProcessError("invalid_output", "Worker affected-cell count is invalid.")
        actual_energy_range = [float(np.min(energy[valid])), float(np.max(energy[valid]))]
        if actual_energy_range != metadata.get("ranges", {}).get("energy_line_height"):
            raise ExternalModelProcessError(
                "invalid_output", "Normalized energy-line range conflicts with worker metadata."
            )

        for name in ("configuration", "environment", "upstream-implementation"):
            payload = json.loads((normalized / f"{name}.json").read_text(encoding="utf-8"))
            key = {
                "configuration": "configuration_sha256",
                "environment": "environment_sha256",
                "upstream-implementation": "upstream_implementation_sha256",
            }[name]
            if sha256_of_manifest(payload) != metadata.get(key):
                raise ExternalModelProcessError(
                    "invalid_output", f"{name} artifact conflicts with worker metadata."
                )
        selection = json.loads((normalized / "selection.json").read_text(encoding="utf-8"))
        if selection.get("selection_sha256") != selection_sha256:
            raise ExternalModelProcessError(
                "invalid_output", "Selection artifact conflicts with deterministic selection."
            )
        upstream = json.loads((normalized / "upstream-implementation.json").read_text(encoding="utf-8"))
        if upstream.get("provider") != "avaframe.com4FlowPy":
            raise ExternalModelProcessError(
                "invalid_output", "The executed implementation is not the AvaFrame com4FlowPy port."
            )


def flowpy_energy_line_reference(
    *,
    elevation: np.ndarray,
    release_row: int,
    release_column: int,
    cell_size_m: float,
    alpha_degrees: float,
    max_energy_line_height_m: float,
) -> np.ndarray:
    """Closed-form energy-line height along one straight downslope column.

    Flow-Py's routing rule is ``z_delta(next) = z_delta(current) + dz - ds tan(alpha)``
    (Neuhauser et al., 2022), clipped at zero and at ``max_z``.  Summed along a
    straight path that starts at the release cell, the intermediate terms cancel
    and the height reduces to ``(z_release - z) - s tan(alpha)``.  That identity is
    what the analytical verification case checks the engine against.
    """

    rows = elevation.shape[0]
    distance = (np.arange(rows, dtype=np.float64) - float(release_row)) * float(cell_size_m)
    drop = float(elevation[release_row, release_column]) - elevation[:, release_column].astype(np.float64)
    height = drop - distance * np.tan(np.deg2rad(float(alpha_degrees)))
    height[:release_row] = 0.0
    return np.clip(height, 0.0, float(max_energy_line_height_m))


__all__ = [
    "AvaFrameCom4FlowPyAdapter",
    "DEFAULT_TILE_OVERLAP_M",
    "DEFAULT_TILE_SIZE_M",
    "UNSUPPORTED_FLOWPY_OUTPUTS",
    "UPSTREAM_FLOWPY_REVIEWED_COMMITS",
    "UpstreamFlowPyAdapter",
    "WORKER_SCHEMA_VERSION",
    "flowpy_energy_line_reference",
    "normalized_text_sha256",
]
