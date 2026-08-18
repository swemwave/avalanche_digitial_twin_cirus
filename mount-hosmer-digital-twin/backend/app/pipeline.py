r"""The one offline entry point that runs compatible pipeline stages in order.

    Mountain/Terrain Pack -> Condition Pack -> Snow State Pack
      -> Normalized Release -> Normalized Runout (per engine)
      -> Engine Comparison -> Prediction Product

Every stage validates its complete inputs before it runs and records what it did
or, when it could not run, exactly why.  There is no implicit fallback: asking for
an engine that is unavailable fails the run rather than quietly substituting a
baseline, and a stage whose upstream input is missing is published as
``unavailable`` rather than filled with a default.

This module belongs to the offline side.  It imports rasterio and the external
engine adapters, so no serving entry point may import it; the serving process
reads only the immutable product this writes.

    python -m app.pipeline run --case synthetic --avaframe-python <python.exe>
    python -m app.pipeline run --case synthetic --dry-run
    python -m app.pipeline list
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from avycore.engines import (
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
    AVYCORE_RELEASE_BASELINE,
    UPSTREAM_FLOWPY,
    AvailabilityStatus,
    UncertaintyBound,
    ValidationLevel,
    ValidationStatus,
    canonical_json_bytes,
    compare_runout_results,
    descriptor_by_id,
    sha256_of_manifest,
)
from avycore.products import (
    PREDICTION_PRODUCT_SCHEMA_VERSION,
    EngineRunRecord,
    EnsembleMember,
    EnsembleSummary,
    PipelineStage,
    PredictionProduct,
    ProductProvenance,
    StageRecord,
    StageStatus,
    SweepSpecification,
    UnsupportedSweep,
    build_prediction_product,
)

from app.core.settings import Settings, get_settings
from app.predictions import (
    PREDICTIONS_DIRECTORY,
    load_prediction_product,
    prediction_product_root,
)
from app.processing.runout.avaframe import AvaFrameCom1DFAAdapter
from app.processing.runout.flowpy import AvaFrameCom4FlowPyAdapter, UpstreamFlowPyAdapter
from app.processing.runout.process import ExternalModelProcessError, file_sha256

PIPELINE_VERSION = "avycore-offline-pipeline-v1"

SUPPORTED_RUNOUT_ENGINES = (
    AVAFRAME_COM1DFA.engine_id,
    AVAFRAME_FLOWPY.engine_id,
    UPSTREAM_FLOWPY.engine_id,
)
DEFAULT_RUNOUT_ENGINES = (AVAFRAME_COM1DFA.engine_id, AVAFRAME_FLOWPY.engine_id)

# Explicit synthetic-case values. They are assumed inputs for a software case, not
# measurements, and the sweep reads its central value from here so the two cannot
# drift apart.
SYNTHETIC_RELEASE_THICKNESS_M = 0.8
SYNTHETIC_RELEASE_DENSITY_KG_M3 = 200.0
SYNTHETIC_VOELLMY_MU = 0.155
SYNTHETIC_VOELLMY_XI_M_S2 = 4000.0
SYNTHETIC_TIME_STEP_S = 0.1
SYNTHETIC_FLOWPY_EXPONENT = 8.0
SYNTHETIC_FLUX_THRESHOLD = 0.0003
SYNTHETIC_MAX_ENERGY_LINE_HEIGHT_M = 270.0

PRODUCT_VALIDATION = ValidationStatus(
    level=ValidationLevel.SOFTWARE_VERIFICATION_ONLY,
    evidence=(
        "Deterministic software tests and published analytical cases only.",
        "The AvaFrame com1DFA avaSimilaritySol case and the Flow-Py planar energy-line case pass their preregistered limits.",
    ),
    eligible_field_events=0,
    limitations=(
        "Software verification is not physical calibration or independent field validation.",
        "Engine agreement is not evidence that either engine is correct.",
    ),
)

# Bounded parameter sweeps, declared as validated specifications rather than as a
# loose table.  ``SweepSpecification`` requires a basis and a stated source, so a
# span nobody can justify fails here — at declaration, before any member has been
# computed — instead of reaching a published envelope.
#
# Every offset below is an *assumed* range.  None is fitted to an observed
# avalanche at any site, which is why none is labelled ``calibration``.
ENSEMBLE_SWEEPS: tuple[SweepSpecification, ...] = (
    SweepSpecification(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="alpha_angle",
        unit="degree",
        varies="engine_parameter",
        offsets=(-3.0, 0.0, 3.0),
        basis="literature",
        source=(
            "Angle-of-reach envelope for dry-snow avalanche paths; Flow-Py documents alpha as the "
            "sliding-block Coulomb friction, and this +/-3 degree span is an assumed literature "
            "range, not a locally fitted value."
        ),
    ),
    SweepSpecification(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="release_extent_offset",
        unit="m",
        varies="release_input",
        offsets=(-5.0, 0.0, 5.0),
        basis="numerical",
        source=(
            "One-cell dilation and erosion of the release boundary on the 5 m grid. The boundary is "
            "where an uncalibrated relative index crosses a fixed cutoff, so its position is "
            "uncertain by at least the cell size. This is a numerical sensitivity to that "
            "discretization, not a physical range and not a fitted one."
        ),
    ),
    SweepSpecification(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="voellmy_mu",
        unit="1",
        varies="engine_parameter",
        offsets=(-0.03, 0.0, 0.03),
        basis="literature",
        source=(
            "Voellmy Coulomb-friction span used as an assumed sensitivity range for dense dry "
            "flow; it is not calibrated to any observed event at any site."
        ),
    ),
    SweepSpecification(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="release_thickness",
        unit="m",
        varies="engine_parameter",
        offsets=(-0.3, 0.0, 0.3),
        basis="literature",
        source=(
            "Assumed dry-slab release-thickness span around the synthetic case's 0.8 m. AvaFrame "
            "expects a per-feature thickness with its own ci95 confidence value and this case has "
            "none, so the span is an assumed literature range rather than the engine's own "
            "documented rangefromci variation."
        ),
    ),
    SweepSpecification(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="release_density",
        unit="kg m-3",
        varies="engine_parameter",
        offsets=(-50.0, 0.0, 50.0),
        basis="literature",
        source=(
            "Assumed dry-slab density span around AvaFrame com1DFA's own default rho = 200 kg m-3 "
            "(com1DFACfg.ini). The default is the engine's, the span around it is an assumed "
            "literature range, and neither is a measurement at any site."
        ),
    ),
    SweepSpecification(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="release_extent_offset",
        unit="m",
        varies="release_input",
        offsets=(-5.0, 0.0, 5.0),
        basis="numerical",
        source=(
            "One-cell dilation and erosion of the release boundary on the 5 m grid. The boundary is "
            "where an uncalibrated relative index crosses a fixed cutoff, so its position is "
            "uncertain by at least the cell size. This is a numerical sensitivity to that "
            "discretization, not a physical range and not a fitted one."
        ),
    ),
)

# Central values every sweep offsets from.  Reading them here rather than
# re-declaring them keeps a sweep from silently drifting away from the run it
# claims to bracket.
SWEEP_CENTRAL_VALUES = {
    "alpha_angle": lambda request: float(request.alpha_degrees),
    "voellmy_mu": lambda request: SYNTHETIC_VOELLMY_MU,
    "release_thickness": lambda request: SYNTHETIC_RELEASE_THICKNESS_M,
    "release_density": lambda request: SYNTHETIC_RELEASE_DENSITY_KG_M3,
    "release_extent_offset": lambda request: 0.0,
}

# Spans that were asked for and deliberately not run.  Publishing them is the
# point: a silently omitted sweep reads as "this parameter does not matter", and
# an invented span reads as "this range is known". Neither is true here.
DECLINED_SWEEPS: tuple[UnsupportedSweep, ...] = (
    UnsupportedSweep(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="entrainment_thickness",
        reason=(
            "com1DFA supports entrainment, but only for simTypeList entries that read an ENT "
            "entrainment-area shapefile from the avalanche project, with an entrainment thickness "
            "per feature. This slice supplies no entrainment layer and runs simTypeList=null, so "
            "there is no entrainment depth to vary. Synthesizing an entrainment area, thickness, "
            "density and erosion energy would be inventing a snow-cover distribution and four "
            "physical parameters, not measuring a sensitivity."
        ),
        required_to_enable=(
            "A reviewed entrainment area for the case, with entrainment thickness (entTh), "
            "entrainment density (rhoEnt, AvaFrame default 100 kg m-3), erosion energy "
            "(entEroEnergy, default 5000) and the shear/deformation resistances, plus adapter and "
            "worker support for simTypeList=ent and its mass-balance normalization."
        ),
    ),
    UnsupportedSweep(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="release_thickness",
        reason=(
            "com4FlowPy routes a dimensionless flux along an energy line and solves no "
            "depth-averaged mass balance. It has no release-thickness term, so there is nothing to "
            "vary; reporting a zero spread would state that thickness does not matter, which is a "
            "claim about avalanches rather than about this model."
        ),
        required_to_enable=(
            "Nothing in this adapter. A release-thickness sensitivity for the Flow-Py family would "
            "require a different model that carries mass and depth."
        ),
    ),
    UnsupportedSweep(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="release_density",
        reason=(
            "com4FlowPy carries no snow density: its routed quantity is dimensionless flux, not "
            "mass. A density span would have no term to enter."
        ),
        required_to_enable=(
            "Nothing in this adapter. A density sensitivity for the Flow-Py family would require a "
            "different model that carries mass."
        ),
    ),
)

MEMBER_FREQUENCY_NOTE = (
    "Member frequency is model frequency over a deterministic parameter sweep. It is not a "
    "probability, a confidence level, or a calibrated likelihood."
)

PRODUCT_LIMITATIONS = (
    "This is an experimental research product, not an operational avalanche forecast.",
    "Every index is a relative, uncalibrated quantity; none is a probability or a danger rating.",
    "No result here is calibrated to, or validated against, observed avalanches at any site.",
    "Engine comparison measures disagreement between models, not the correctness of either.",
    "Stages published as unavailable produced nothing; their absence is not a benign or safe result.",
    "Sensitivity envelopes come from bounded sweeps of assumed literature ranges; member frequency is model frequency, not probability.",
)


class PipelineError(RuntimeError):
    """A visible, stage-attributed pipeline failure."""

    def __init__(self, stage: PipelineStage, message: str) -> None:
        super().__init__(message)
        self.stage = stage


@dataclass(frozen=True)
class PipelineRequest:
    """Everything one pipeline run needs, resolved before any stage executes."""

    case: str
    engines: tuple[str, ...]
    avaframe_python: Path | None
    flowpy_checkout: Path | None
    runtime_root: Path
    seed: int
    dry_run: bool
    ensemble: bool
    condition_pack_id: str | None
    simulation_time_s: float
    alpha_degrees: float
    resume: bool = False

    def configuration(self) -> dict[str, Any]:
        # Absolute, machine-specific paths are deliberately excluded: they are
        # disposable, and including them would make an identical run on another
        # machine look like a different product.
        return {
            "case": self.case,
            "engines": list(self.engines),
            "ensemble": self.ensemble,
            "seed": self.seed,
            "condition_pack_id": self.condition_pack_id,
            "simulation_time_s": self.simulation_time_s,
            "alpha_degrees": self.alpha_degrees,
            "pipeline_version": PIPELINE_VERSION,
        }


def _pipeline_sha256() -> str:
    """Hash the offline code whose behaviour the numbers depend on."""

    from app.processing.runout import avaframe, flowpy, synthetic

    modules = {
        "pipeline.py": Path(__file__).resolve(),
        "synthetic.py": Path(synthetic.__file__).resolve(),
        "avaframe.py": Path(avaframe.__file__).resolve(),
        "flowpy.py": Path(flowpy.__file__).resolve(),
        "_avaframe_worker.py": Path(avaframe.__file__).with_name("_avaframe_worker.py").resolve(),
        "_flowpy_worker.py": Path(flowpy.__file__).with_name("_flowpy_worker.py").resolve(),
    }
    return sha256_of_manifest({name: file_sha256(path) for name, path in modules.items()})


STAGE_CACHE_DIRECTORY = "stage-cache"
STAGE_CACHE_SCHEMA_VERSION = "avycore-runout-stage-cache-v1"


def _request_identity(request) -> str:
    """Hash one engine request, minus the disposable paths inside it.

    Artifact URIs are absolute staging paths that change on every run, so hashing
    them would make every key unique and every lookup a miss.  What identifies an
    input is its SHA-256 and byte size, and both stay in the payload.
    """

    def strip(value):
        if isinstance(value, dict):
            return {key: (None if key == "uri" else strip(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return sha256_of_manifest(strip(request.model_dump(mode="json")))


def _runout_cache_key(
    *, engine_id: str, engine_request, adapter, availability
) -> tuple[str, dict[str, Any]] | None:
    """Bind a cached run to every identity component a result already records.

    Returns ``None`` -- a guaranteed miss -- as soon as any component is unknown.
    That is the whole safety property: a key that silently dropped a component it
    could not resolve would match runs it has no right to match.
    """

    identity = adapter.replay_identity()
    components = {
        "cache_schema_version": STAGE_CACHE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "engine_id": engine_id,
        "engine_version": availability.detected_version,
        "adapter_version": identity.get("adapter_version"),
        "adapter_sha256": identity.get("adapter_sha256"),
        "executable_sha256": availability.executable_sha256,
        "environment_sha256": availability.environment_sha256,
        "scenario_sha256": engine_request.scenario_sha256,
        "request_sha256": _request_identity(engine_request),
        "seed": engine_request.seed,
    }
    # ``seed`` is legitimately null for an engine that takes none; everything else
    # being unknown means the run cannot be identified, so there is no key.
    unknown = sorted(
        name for name, value in components.items() if value is None and name != "seed"
    )
    if unknown:
        return None
    return sha256_of_manifest(components), components


@dataclass
class RunoutStageCache:
    """Input-keyed reuse of a completed engine run, off unless ``--resume`` asks.

    The serving application never reads this; it is an offline convenience whose
    only correctness requirement is that a hit be indistinguishable from having
    run the engine.  Every hit is therefore re-verified against the stored
    checksums *and* against the identity components the key was built from, and
    anything that does not line up is downgraded to a miss and re-executed.
    """

    root: Path | None = None
    report: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.report is None:
            self.report = []

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def record(self, engine_id: str, outcome: str, reason: str, *, result_id: str | None) -> None:
        assert self.report is not None
        self.report.append(
            {"engine_id": engine_id, "outcome": outcome, "reason": reason, "result_id": result_id}
        )

    def restore(self, *, engine_id: str, key: str, components: dict[str, Any], output_root: Path):
        """Return a verified cached result, or ``None`` for any reason whatsoever."""

        from avycore.engines import NormalizedRunoutResult

        entry = self.root / key if self.root is not None else None
        if entry is None or not (entry / "cache-entry.json").is_file():
            self.record(engine_id, "miss", "No cache entry for this input key.", result_id=None)
            return None
        try:
            record = json.loads((entry / "cache-entry.json").read_text(encoding="utf-8"))
            if record.get("schema_version") != STAGE_CACHE_SCHEMA_VERSION:
                raise ValueError("cache entry schema version does not match")
            if record.get("key") != key or record.get("components") != components:
                raise ValueError("cache entry does not restate the key it was stored under")
            bundle = entry / "bundle"
            checksums = record["checksums"]
            present = {
                str(path.relative_to(bundle)).replace(os.sep, "/")
                for path in bundle.rglob("*")
                if path.is_file()
            }
            if present != set(checksums):
                raise ValueError("cached bundle file set differs from its manifest")
            for relative, expected in sorted(checksums.items()):
                if file_sha256(bundle / relative) != expected:
                    raise ValueError(f"cached artifact {relative} failed its checksum")
            result = NormalizedRunoutResult.model_validate_json((bundle / "result.json").read_bytes())
            if result.result_id != record.get("result_id"):
                raise ValueError("cached result identity does not match its entry")
            provenance = result.provenance
            recorded = {
                "engine_id": provenance.engine_id,
                "engine_version": provenance.engine_version,
                "adapter_version": provenance.adapter_version,
                "adapter_sha256": provenance.adapter_sha256,
                "executable_sha256": provenance.executable_sha256,
                "environment_sha256": provenance.environment_sha256,
                "scenario_sha256": provenance.scenario_sha256,
                "seed": provenance.seed,
            }
            mismatched = sorted(name for name, value in recorded.items() if components[name] != value)
            if mismatched:
                raise ValueError("provenance disagrees with the key on " + ", ".join(mismatched))
        except (OSError, ValueError, KeyError) as exc:
            self.record(engine_id, "miss", f"Cache entry rejected: {exc}.", result_id=None)
            return None

        destination = output_root / result.result_id
        if destination.exists():
            self.record(
                engine_id,
                "miss",
                f"Output directory already holds {result.result_id}.",
                result_id=None,
            )
            return None
        output_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, destination)
        self.record(
            engine_id,
            "hit",
            "Request, adapter and engine identity all matched a stored bundle.",
            result_id=result.result_id,
        )
        return result

    def store(self, *, key: str, components: dict[str, Any], bundle: Path, result) -> None:
        assert self.root is not None
        entry = self.root / key
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="cache-", dir=self.root))
        try:
            shutil.copytree(bundle, staging / "bundle")
            checksums = {
                str(path.relative_to(staging / "bundle")).replace(os.sep, "/"): file_sha256(path)
                for path in sorted((staging / "bundle").rglob("*"))
                if path.is_file()
            }
            (staging / "cache-entry.json").write_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": STAGE_CACHE_SCHEMA_VERSION,
                        "key": key,
                        "components": components,
                        "result_id": result.result_id,
                        "checksums": checksums,
                    }
                )
                + b"\n"
            )
            if entry.exists():
                shutil.rmtree(entry)
            staging.replace(entry)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _execute_runout(
    *,
    engine_id: str,
    adapter,
    availability,
    engine_request,
    output_root: Path,
    cache: RunoutStageCache,
    label: str,
):
    """Run one engine, or restore a stored run proved to have the same identity."""

    keyed = None
    if cache.enabled:
        keyed = _runout_cache_key(
            engine_id=engine_id,
            engine_request=engine_request,
            adapter=adapter,
            availability=availability,
        )
        if keyed is None:
            cache.record(
                engine_id,
                "miss",
                "An identity component of this run is unknown, so no key could be formed.",
                result_id=None,
            )
        else:
            restored = cache.restore(
                engine_id=engine_id,
                key=keyed[0],
                components=keyed[1],
                output_root=output_root,
            )
            if restored is not None:
                return restored
    try:
        result = adapter.run_runout(engine_request, output_root=output_root)
    except ExternalModelProcessError as exc:
        raise PipelineError(PipelineStage.RUNOUT, f"{engine_id} ({label}): {exc}") from exc
    if keyed is not None:
        cache.store(
            key=keyed[0],
            components=keyed[1],
            bundle=output_root / result.result_id,
            result=result,
        )
    return result


def _runout_adapter(engine_id: str, request: PipelineRequest):
    if engine_id == AVAFRAME_COM1DFA.engine_id:
        if request.avaframe_python is None:
            return None, "No AvaFrame Python environment was supplied with --avaframe-python."
        return AvaFrameCom1DFAAdapter(request.avaframe_python), None
    if engine_id == AVAFRAME_FLOWPY.engine_id:
        if request.avaframe_python is None:
            return None, "No AvaFrame Python environment was supplied with --avaframe-python."
        return AvaFrameCom4FlowPyAdapter(request.avaframe_python), None
    if engine_id == UPSTREAM_FLOWPY.engine_id:
        return UpstreamFlowPyAdapter(request.flowpy_checkout), None
    raise PipelineError(PipelineStage.RUNOUT, f"Unsupported runout engine {engine_id!r}.")


def check_engines(request: PipelineRequest) -> list[dict[str, Any]]:
    """Probe every requested engine without running any physics."""

    report: list[dict[str, Any]] = []
    for engine_id in request.engines:
        descriptor = descriptor_by_id(engine_id)
        adapter, reason = _runout_adapter(engine_id, request)
        if adapter is None:
            report.append(
                {
                    "engine_id": engine_id,
                    "status": AvailabilityStatus.UNAVAILABLE.value,
                    "reason": reason,
                    "license_spdx": descriptor.license_spdx,
                    "detected_version": None,
                }
            )
            continue
        availability = adapter.availability()
        report.append(
            {
                "engine_id": engine_id,
                "status": availability.status.value,
                "reason": availability.reason,
                "license_spdx": descriptor.license_spdx,
                "detected_version": availability.detected_version,
                "environment_sha256": availability.environment_sha256,
            }
        )
    return report


def _condition_stage(request: PipelineRequest) -> StageRecord:
    """Report the real state of the hourly forcing, never a convenient default."""

    if request.condition_pack_id is None:
        return StageRecord(
            stage=PipelineStage.CONDITION_PACK,
            status=StageStatus.SKIPPED,
            reason=(
                "No Condition Pack was selected. This run uses explicit scenario parameters and "
                "makes no current-condition or hindcast claim."
            ),
        )
    from app.processing.conditions.storage import load_condition_pack

    root = request.runtime_root / "baked" / "conditions" / request.condition_pack_id
    if not root.is_dir():
        raise PipelineError(
            PipelineStage.CONDITION_PACK, f"Condition Pack directory does not exist: {root}"
        )
    pack = load_condition_pack(root)
    missing = sorted(
        name
        for name, series in pack.variables.items()
        if all(value.value is None for value in series.values)
    )
    if missing:
        return StageRecord(
            stage=PipelineStage.CONDITION_PACK,
            status=StageStatus.UNAVAILABLE,
            reason=(
                f"Condition Pack {request.condition_pack_id} has no values at all for: "
                f"{', '.join(missing)}. Snow-model forcing requires every variable, and a missing "
                "one is never written as zero or interpolated."
            ),
        )
    gaps = sum(
        1
        for series in pack.variables.values()
        for value in series.values
        if value.value is None
    )
    if gaps:
        return StageRecord(
            stage=PipelineStage.CONDITION_PACK,
            status=StageStatus.UNAVAILABLE,
            reason=(
                f"Condition Pack {request.condition_pack_id} has {gaps} masked hourly values. "
                "The strict SMET adapter refuses partial forcing rather than gap-filling it."
            ),
        )
    return StageRecord(
        stage=PipelineStage.CONDITION_PACK,
        status=StageStatus.COMPLETED,
        result_id=pack.condition_id,
        artifact_root=f"conditions/{pack.condition_id}",
        reason="Complete hourly forcing was validated for every required variable.",
    )


def _snow_state_stage(condition: StageRecord) -> StageRecord:
    if condition.status != StageStatus.COMPLETED:
        return StageRecord(
            stage=PipelineStage.SNOW_STATE_PACK,
            status=StageStatus.UNAVAILABLE,
            engine_id="snow.snowpack",
            reason=(
                "SNOWPACK cannot run without a complete Condition Pack. Upstream stage "
                f"{condition.stage.value} is {condition.status.value}: {condition.reason}"
            ),
        )
    return StageRecord(
        stage=PipelineStage.SNOW_STATE_PACK,
        status=StageStatus.UNAVAILABLE,
        engine_id="snow.snowpack",
        reason=(
            "Forcing is complete, but no reviewed initial snow/soil state, ground boundary, "
            "roughness, canopy classification, or site configuration exists for this site, so no "
            "SNOWPACK column is scientifically eligible."
        ),
    )


def run_pipeline(
    request: PipelineRequest, *, cache: "RunoutStageCache | None" = None
) -> PredictionProduct:
    """Run the compatible stages in order and publish one immutable product.

    ``cache`` only changes whether an engine is re-executed or a proven-identical
    stored bundle is restored.  It never reaches the product, which is why a
    resumed run must publish the identical ``product_id``.
    """

    if request.case != "synthetic":
        raise PipelineError(
            PipelineStage.MOUNTAIN_PACK,
            (
                f"Case {request.case!r} is not runnable. A real-site case requires an eligible "
                "Snow State Pack and reviewed release thickness, density, and friction parameters; "
                "none exist, and the pipeline will not substitute synthetic values for a real site."
            ),
        )
    unknown = [item for item in request.engines if item not in SUPPORTED_RUNOUT_ENGINES]
    if unknown:
        raise PipelineError(
            PipelineStage.RUNOUT, f"Unknown runout engine(s): {', '.join(sorted(unknown))}."
        )

    stages: list[StageRecord] = [
        StageRecord(
            stage=PipelineStage.MOUNTAIN_PACK,
            status=StageStatus.SKIPPED,
            reason=(
                "The synthetic case generates its own projected terrain and reads no Mountain "
                "Pack, so its outputs describe no real location."
            ),
        )
    ]
    condition = _condition_stage(request)
    stages.append(condition)
    stages.append(_snow_state_stage(condition))

    # Every engine is probed before any stage does work.  Discovering an
    # unavailable engine halfway through would leave a partially computed run and
    # tempt a fallback; refusing up front keeps "no implicit fallback" cheap.
    adapters = {}
    availabilities = {}
    for engine_id in request.engines:
        adapter, reason = _runout_adapter(engine_id, request)
        if adapter is None:
            raise PipelineError(PipelineStage.RUNOUT, f"{engine_id}: {reason}")
        availability = adapter.availability()
        if availability.status != AvailabilityStatus.AVAILABLE:
            raise PipelineError(
                PipelineStage.RUNOUT,
                f"{engine_id} is {availability.status.value}: {availability.reason}",
            )
        adapters[engine_id] = adapter
        availabilities[engine_id] = availability

    runout_cache = cache if cache is not None else RunoutStageCache()
    if request.resume and runout_cache.root is None:
        runout_cache.root = request.runtime_root / STAGE_CACHE_DIRECTORY / "runout"

    from app.processing.runout.synthetic import (
        SYNTHETIC_CASE_VERSION,
        SYNTHETIC_DISCLAIMER,
        _com1dfa_request,
        _flowpy_request,
        _make_release_bundle,
    )

    root = request.runtime_root / PREDICTIONS_DIRECTORY
    root.mkdir(parents=True, exist_ok=True)
    configuration = request.configuration()

    with tempfile.TemporaryDirectory(prefix="pipeline-", dir=root) as temp_name:
        work = Path(temp_name)
        release_bundle = work / "release"
        release, artifacts = _make_release_bundle(release_bundle)
        stages.append(
            StageRecord(
                stage=PipelineStage.RELEASE,
                status=StageStatus.COMPLETED,
                engine_id=AVYCORE_RELEASE_BASELINE.engine_id,
                result_id=release.result_id,
                artifact_root="release",
                reason=(
                    "The uncalibrated AvyCore terrain/loading relative-index baseline produced the "
                    "release extent. It contains no modelled snow instability."
                ),
                inputs_sha256=release.provenance.input_manifest_sha256,
            )
        )

        runouts: list[EngineRunRecord] = []
        bundles: dict[str, Path] = {}
        builders = (_com1dfa_request, _flowpy_request)
        for engine_id in request.engines:
            descriptor = descriptor_by_id(engine_id)
            adapter = adapters[engine_id]
            engine_request = _engine_request(
                engine_id,
                release=release,
                artifacts=artifacts,
                request=request,
                builders=builders,
            )
            result = _execute_runout(
                engine_id=engine_id,
                adapter=adapter,
                availability=availabilities[engine_id],
                engine_request=engine_request,
                output_root=work / engine_id,
                cache=runout_cache,
                label="central",
            )
            bundle = work / engine_id / result.result_id
            bundles[engine_id] = bundle
            runouts.append(
                EngineRunRecord(
                    engine_id=engine_id,
                    engine_version=result.provenance.engine_version,
                    license_spdx=descriptor.license_spdx,
                    artifact_root=f"runouts/{engine_id}",
                    result=result,
                )
            )

        # One StageRecord per stage is the contract, so per-engine detail lives in
        # the runout records and the stage row summarises the set.
        if runouts:
            stages.append(
                StageRecord(
                    stage=PipelineStage.RUNOUT,
                    status=StageStatus.COMPLETED,
                    result_id=runouts[0].result.result_id,
                    artifact_root="runouts",
                    reason=(
                        "Ran "
                        + ", ".join(record.engine_id for record in runouts)
                        + " from the same normalized release and terrain, in isolated processes."
                    ),
                )
            )

        comparisons = []
        comparison_roots: list[str] = []
        if len(runouts) >= 2:
            reference_cell = artifacts["release_reference_cell"]
            for index in range(len(runouts) - 1):
                left, right = runouts[index], runouts[index + 1]
                comparison = compare_runout_results(
                    left.result,
                    right.result,
                    left_bundle=bundles[left.engine_id],
                    right_bundle=bundles[right.engine_id],
                    output_root=work / "comparisons",
                    reference_cell=reference_cell,
                )
                comparisons.append(comparison)
                comparison_roots.append(f"comparisons/{comparison.comparison_id}")
            stages.append(
                StageRecord(
                    stage=PipelineStage.COMPARISON,
                    status=StageStatus.COMPLETED,
                    result_id=comparisons[0].comparison_id,
                    artifact_root="comparisons",
                    reason=(
                        f"Compared {len(runouts)} engines on the identical valid domain. "
                        "Metrics report disagreement, not correctness."
                    ),
                )
            )
        else:
            stages.append(
                StageRecord(
                    stage=PipelineStage.COMPARISON,
                    status=StageStatus.SKIPPED,
                    reason=(
                        "Engine comparison needs at least two runout engines; "
                        f"{len(runouts)} ran."
                    ),
                )
            )

        ensembles: list[EnsembleSummary] = []
        declined: list[UnsupportedSweep] = []
        if request.ensemble:
            engines_that_ran = {record.engine_id for record in runouts}
            for record in runouts:
                for specification in ENSEMBLE_SWEEPS:
                    if specification.engine_id != record.engine_id:
                        continue
                    ensembles.append(
                        _run_bounded_sweep(
                            specification=specification,
                            adapter=adapters[record.engine_id],
                            availability=availabilities[record.engine_id],
                            release=release,
                            artifacts=artifacts,
                            request=request,
                            central=record.result,
                            work=work,
                            builders=builders,
                            cache=runout_cache,
                        )
                    )
            declined = [
                item for item in DECLINED_SWEEPS if item.engine_id in engines_that_ran
            ]
        dominant = None
        if ensembles:
            leader = max(ensembles, key=lambda item: item.area_spread_m2)
            dominant = f"{leader.engine_id}:{leader.parameter}"

        warnings = [
            "Synthetic terrain and explicit assumed parameters: this product describes no real location.",
        ]
        if request.ensemble and not ensembles:
            warnings.append(
                "A bounded sensitivity sweep was requested but no engine supports one, so this "
                "product reports no propagated uncertainty."
            )
        for item in declined:
            warnings.append(
                f"No {item.parameter} sweep was run for {item.engine_id}: {item.reason}"
            )
        for record in stages:
            if record.status in {StageStatus.UNAVAILABLE, StageStatus.FAILED}:
                warnings.append(f"{record.stage.value} is {record.status.value}: {record.reason}")

        product = build_prediction_product(
            {
                "schema_version": PREDICTION_PRODUCT_SCHEMA_VERSION,
                "site_id": release.site_id,
                "disclaimer": SYNTHETIC_DISCLAIMER,
                "regime": "dense_dry",
                "generated_from": "synthetic_case",
                "provenance": ProductProvenance(
                    mountain_pack_sha256=sha256_of_manifest(
                        {"synthetic_case_version": SYNTHETIC_CASE_VERSION}
                    ),
                    bake_sha256=None,
                    condition_pack_id=request.condition_pack_id,
                    snow_state_pack_id=None,
                    pipeline_version=PIPELINE_VERSION,
                    pipeline_sha256=_pipeline_sha256(),
                    configuration_sha256=sha256_of_manifest(configuration),
                    seed=request.seed,
                ),
                "stages": tuple(stages),
                "snow_state": None,
                "release": release,
                "release_artifact_root": "release",
                "runouts": tuple(runouts),
                "comparisons": tuple(comparisons),
                "comparison_artifact_roots": tuple(comparison_roots),
                "uncertainty": _distinct_bounds(ensembles),
                "ensembles": tuple(ensembles),
                "unsupported_ensembles": tuple(declined),
                "dominant_uncertainty_contributor": dominant,
                "validation": PRODUCT_VALIDATION,
                "warnings": tuple(warnings),
                "limitations": PRODUCT_LIMITATIONS,
            }
        )
        staging = work / "product"
        _assemble_product_directory(
            staging,
            product=product,
            configuration=configuration,
            release_bundle=release_bundle,
            runout_bundles=bundles,
            comparison_root=work / "comparisons",
            comparison_ids=[item.comparison_id for item in comparisons],
            ensembles=ensembles,
            ensemble_root=work / "ensembles",
        )
        destination = prediction_product_root(request.runtime_root, product.product_id)
        if destination.exists():
            existing = load_prediction_product(destination)
            if existing != product:
                raise PipelineError(
                    PipelineStage.COMPARISON,
                    f"Prediction product identity collision at {destination}",
                )
            return existing
        staging.replace(destination)
        return product


def _engine_request(
    engine_id: str,
    *,
    release,
    artifacts: dict[str, Any],
    request: PipelineRequest,
    builders,
    overrides: dict[str, float] | None = None,
):
    """Build one engine request, with named scalar overrides for sweep members.

    Central values live in one place and every member is an offset from them, so
    a sweep cannot quietly bracket a different run from the one it publishes.
    """

    com1dfa_request, flowpy_request = builders
    values: dict[str, float] = {
        "release_thickness": SYNTHETIC_RELEASE_THICKNESS_M,
        "release_density": SYNTHETIC_RELEASE_DENSITY_KG_M3,
        "voellmy_mu": SYNTHETIC_VOELLMY_MU,
        "alpha_angle": float(request.alpha_degrees),
    }
    values.update(overrides or {})
    if engine_id == AVAFRAME_COM1DFA.engine_id:
        return com1dfa_request(
            release_result=release,
            artifacts=artifacts,
            release_thickness_m=values["release_thickness"],
            release_density_kg_m3=values["release_density"],
            voellmy_mu=values["voellmy_mu"],
            voellmy_xi_m_s2=SYNTHETIC_VOELLMY_XI_M_S2,
            simulation_time_s=request.simulation_time_s,
            time_step_s=SYNTHETIC_TIME_STEP_S,
            seed=request.seed,
        )
    return flowpy_request(
        release_result=release,
        artifacts=artifacts,
        alpha_degrees=values["alpha_angle"],
        exponent=SYNTHETIC_FLOWPY_EXPONENT,
        flux_threshold=SYNTHETIC_FLUX_THRESHOLD,
        max_energy_line_height_m=SYNTHETIC_MAX_ENERGY_LINE_HEIGHT_M,
    )


def _distinct_bounds(ensembles: Sequence[EnsembleSummary]) -> tuple[UncertaintyBound, ...]:
    """One bound per distinct span.

    The same release span is swept for every engine that consumes it. Listing it
    once per engine would read as several independent uncertainties rather than
    one input whose effect was measured on several models; the per-engine detail
    stays in ``ensembles``.
    """

    bounds: list[UncertaintyBound] = []
    for item in ensembles:
        bound = UncertaintyBound(
            parameter=item.parameter,
            unit=item.unit,
            lower=min(member.value for member in item.members),
            central=next(member.value for member in item.members if member.is_central),
            upper=max(member.value for member in item.members),
            basis=item.basis,
            source=item.source,
        )
        if bound not in bounds:
            bounds.append(bound)
    return tuple(bounds)


def _run_bounded_sweep(
    *,
    specification: SweepSpecification,
    adapter,
    availability,
    release,
    artifacts: dict[str, Any],
    request: PipelineRequest,
    central,
    work: Path,
    builders,
    cache: "RunoutStageCache",
) -> EnsembleSummary:
    """Run one deterministic sweep of one parameter around its central value.

    The central member is re-used rather than re-run, so the sweep cannot drift
    from the result the product already publishes.  Every member keeps its own
    normalized bundle and identity, which is what makes the envelope reproducible
    from the member list alone.

    A ``release_input`` sweep rebuilds the whole normalized release for each
    member: the release identity is derived from its contents, so moving the
    release boundary has to move that identity too, or two different releases
    would be indistinguishable downstream.
    """

    engine_id = specification.engine_id
    parameter = specification.parameter
    base = SWEEP_CENTRAL_VALUES[parameter](request)
    root = work / "ensembles" / engine_id / parameter
    root.mkdir(parents=True, exist_ok=True)

    from app.processing.runout.synthetic import _make_release_bundle

    members: list[EnsembleMember] = []
    envelope: np.ndarray | None = None
    cell_area: float | None = None
    for index, offset in enumerate(specification.offsets):
        value = float(base + offset)
        is_central = offset == 0.0
        if is_central:
            result = central
            bundle = work / engine_id / result.result_id
            member_root = f"runouts/{engine_id}"
        else:
            if specification.varies == "release_input":
                member_release, member_artifacts = _make_release_bundle(
                    root / f"release-{index}", release_boundary_offset_m=value
                )
                member_overrides: dict[str, float] = {}
            else:
                member_release, member_artifacts = release, artifacts
                member_overrides = {parameter: value}
            member_request = _engine_request(
                engine_id,
                release=member_release,
                artifacts=member_artifacts,
                request=request,
                builders=builders,
                overrides=member_overrides,
            )
            result = _execute_runout(
                engine_id=engine_id,
                adapter=adapter,
                availability=availability,
                engine_request=member_request,
                output_root=root,
                cache=cache,
                label=f"sweep member {parameter}={value:g}",
            )
            bundle = root / result.result_id
            member_root = f"ensembles/{engine_id}/{parameter}"

        extent = np.load(bundle / "runout.npy", allow_pickle=False)
        grid = result.runout_extent.grid
        cell_area = grid.cell_size_x_m * grid.cell_size_y_m
        envelope = extent.copy() if envelope is None else (envelope | extent)
        members.append(
            EnsembleMember(
                member_id="member-"
                + sha256_of_manifest(
                    {"engine": engine_id, "parameter": parameter, "value": value}
                )[:16],
                engine_id=engine_id,
                parameter=parameter,
                unit=specification.unit,
                value=value,
                is_central=is_central,
                result_id=result.result_id,
                artifact_root=member_root,
                runout_area_m2=result.runout_area_m2,
                aoi_status=result.aoi_status,
            )
        )

    assert envelope is not None and cell_area is not None
    np.save(root / "envelope.npy", envelope, allow_pickle=False)
    areas = [member.runout_area_m2 for member in members]
    central_area = next(member.runout_area_m2 for member in members if member.is_central)
    return EnsembleSummary(
        engine_id=engine_id,
        parameter=parameter,
        unit=specification.unit,
        varies=specification.varies,
        basis=specification.basis,
        source=specification.source,
        members=tuple(members),
        central_runout_area_m2=central_area,
        minimum_runout_area_m2=min(areas),
        maximum_runout_area_m2=max(areas),
        envelope_artifact_root=f"ensembles/{engine_id}/{parameter}",
        envelope_area_m2=float(int(np.count_nonzero(envelope)) * cell_area),
        member_frequency_note=MEMBER_FREQUENCY_NOTE,
    )


def _assemble_product_directory(
    staging: Path,
    *,
    product: PredictionProduct,
    configuration: dict[str, Any],
    release_bundle: Path,
    runout_bundles: dict[str, Path],
    comparison_root: Path,
    comparison_ids: Sequence[str],
    ensembles: Sequence[EnsembleSummary] = (),
    ensemble_root: Path | None = None,
) -> None:
    """Lay out one self-contained product; every path inside it is relative."""

    staging.mkdir(parents=True, exist_ok=False)
    shutil.copytree(release_bundle, staging / "release")
    for engine_id, bundle in runout_bundles.items():
        shutil.copytree(bundle, staging / "runouts" / engine_id)
    for comparison_id in comparison_ids:
        shutil.copytree(comparison_root / comparison_id, staging / "comparisons" / comparison_id)
    for summary in ensembles:
        assert ensemble_root is not None
        source = ensemble_root / summary.engine_id / summary.parameter
        shutil.copytree(source, staging / summary.envelope_artifact_root)
    (staging / "configuration.json").write_bytes(canonical_json_bytes(configuration) + b"\n")
    (staging / "prediction-product.json").write_bytes(
        canonical_json_bytes(product.model_dump(mode="json")) + b"\n"
    )
    checksums = {
        str(path.relative_to(staging)).replace(os.sep, "/"): file_sha256(path)
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.name != "checksums.json"
    }
    (staging / "checksums.json").write_bytes(canonical_json_bytes(checksums) + b"\n")


def _build_request(arguments: argparse.Namespace, settings: Settings) -> PipelineRequest:
    engines = tuple(
        item.strip() for item in str(arguments.engines).split(",") if item.strip()
    ) or DEFAULT_RUNOUT_ENGINES
    return PipelineRequest(
        case=arguments.case,
        engines=engines,
        avaframe_python=Path(arguments.avaframe_python).resolve()
        if arguments.avaframe_python
        else None,
        flowpy_checkout=Path(arguments.flowpy_checkout).resolve()
        if arguments.flowpy_checkout
        else None,
        runtime_root=Path(arguments.runtime_root).resolve()
        if arguments.runtime_root
        else settings.runtime_root,
        seed=int(arguments.seed),
        dry_run=bool(arguments.dry_run),
        ensemble=bool(getattr(arguments, "ensemble", False)),
        condition_pack_id=arguments.condition_pack,
        simulation_time_s=float(arguments.simulation_time_s),
        alpha_degrees=float(arguments.alpha_degrees),
        resume=bool(getattr(arguments, "resume", False)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pipeline", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the pipeline and publish a prediction product.")
    run.add_argument("--case", default="synthetic", help="Case to run. Only 'synthetic' is runnable.")
    run.add_argument(
        "--engines",
        default=",".join(DEFAULT_RUNOUT_ENGINES),
        help=f"Comma-separated runout engines. Supported: {', '.join(SUPPORTED_RUNOUT_ENGINES)}.",
    )
    run.add_argument("--avaframe-python", default=None)
    run.add_argument("--flowpy-checkout", default=None)
    run.add_argument("--runtime-root", default=None)
    run.add_argument("--condition-pack", default=None)
    run.add_argument("--seed", type=int, default=12345)
    run.add_argument("--simulation-time-s", type=float, default=40.0)
    run.add_argument("--alpha-degrees", type=float, default=25.0)
    run.add_argument(
        "--ensemble",
        action="store_true",
        help=(
            "Also run every declared bounded deterministic sweep per engine -- friction, release "
            "thickness, release density and release extent, where the model carries the quantity "
            "-- and publish each outer sensitivity envelope. A span an engine cannot vary is "
            "published as a refusal with a reason, never as a zero. Member frequency is model "
            "frequency, never probability."
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse a stored engine bundle whose request, adapter and engine identity all match, "
            "instead of re-executing it. A hit is verified against the stored checksums and the "
            "result's own provenance; anything unknown or unproven is a miss. The published "
            "product is identical either way."
        ),
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and engine availability without running any physics.",
    )

    listing = subparsers.add_parser("list", help="List published prediction products.")
    listing.add_argument("--runtime-root", default=None)

    arguments = parser.parse_args(argv)
    settings = get_settings()

    if arguments.command == "list":
        root = (
            Path(arguments.runtime_root).resolve()
            if arguments.runtime_root
            else settings.runtime_root
        ) / PREDICTIONS_DIRECTORY
        if not root.is_dir():
            print(json.dumps({"products": []}, indent=2))
            return 0
        products = []
        for directory in sorted(root.iterdir()):
            # A crashed run can leave its staging directory behind, and the cache
            # is a sibling root; neither is a product, and neither should make
            # listing fail.
            if not directory.is_dir() or not directory.name.startswith("prediction-product-"):
                continue
            product = load_prediction_product(directory)
            products.append(
                {
                    "product_id": product.product_id,
                    "site_id": product.site_id,
                    "engines": list(product.engine_ids()),
                    "unavailable_stages": [
                        record.stage.value for record in product.unavailable_stages
                    ],
                }
            )
        print(json.dumps({"products": products}, indent=2))
        return 0

    request = _build_request(arguments, settings)
    engine_report = check_engines(request)
    if request.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry_run",
                    "case": request.case,
                    "runtime_root": str(request.runtime_root),
                    "configuration": request.configuration(),
                    "engines": engine_report,
                    "condition_stage": _condition_stage(request).model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if all(item["status"] == "available" for item in engine_report) else 1

    cache = RunoutStageCache()
    try:
        product = run_pipeline(request, cache=cache)
    except PipelineError as exc:
        print(
            json.dumps(
                {"error": {"stage": exc.stage.value, "message": str(exc)}}, indent=2, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "product_id": product.product_id,
                "root": str(prediction_product_root(request.runtime_root, product.product_id)),
                "engines": list(product.engine_ids()),
                "stages": [
                    {"stage": record.stage.value, "status": record.status.value}
                    for record in product.stages
                ],
                "comparisons": [item.comparison_id for item in product.comparisons],
                "ensembles": [
                    {
                        "engine_id": item.engine_id,
                        "parameter": item.parameter,
                        "members": len(item.members),
                        "area_spread_m2": item.area_spread_m2,
                    }
                    for item in product.ensembles
                ],
                "dominant_uncertainty_contributor": product.dominant_uncertainty_contributor,
                "unsupported_ensembles": [
                    {"engine_id": item.engine_id, "parameter": item.parameter, "reason": item.reason}
                    for item in product.unsupported_ensembles
                ],
                "stage_cache": {
                    "enabled": request.resume,
                    "hits": sum(1 for item in cache.report or () if item["outcome"] == "hit"),
                    "misses": sum(1 for item in cache.report or () if item["outcome"] == "miss"),
                    "decisions": list(cache.report or ()),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
