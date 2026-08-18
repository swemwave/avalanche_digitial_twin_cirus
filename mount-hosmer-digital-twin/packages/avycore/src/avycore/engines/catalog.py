"""Built-in catalogue for baseline and isolated external model engines."""

from __future__ import annotations

from .contracts import (
    ENGINE_CONTRACT_SCHEMA_VERSION,
    AvailabilityStatus,
    AvalancheRegime,
    EngineAvailability,
    EngineDescriptor,
    EngineStage,
    ExecutionBoundary,
    InputKind,
    InputRequirement,
    OutputQuantity,
    ParameterValidityRange,
    SpatialApplicability,
    ValidationLevel,
    ValidationStatus,
)
from .registry import EngineRegistry, StaticEnginePlugin


NO_FIELD_VALIDATION = ValidationStatus(
    level=ValidationLevel.SOFTWARE_VERIFICATION_ONLY,
    evidence=(
        "Deterministic software tests only; no eligible local calibration or independent holdout events.",
    ),
    eligible_field_events=0,
    limitations=(
        "Software verification is not physical calibration or independent field validation.",
    ),
)


def _req(
    name: str,
    kind: InputKind,
    unit: str | None,
    description: str,
    *,
    required: bool = True,
) -> InputRequirement:
    return InputRequirement(
        name=name,
        kind=kind,
        unit=unit,
        required=required,
        missing_policy="fail" if required else "not_applicable",
        description=description,
    )


SNOWPACK = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="snow.snowpack",
    display_name="SNOWPACK offline boundary",
    stage=EngineStage.SNOW_STATE,
    implementation_version="3.7.0",
    adapter_version="condition-pack-to-smet-v1/snow-state-pack-v2",
    execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
    source_url="https://gitlabext.wsl.ch/snow-models/snowpack",
    license_spdx="LGPL-3.0-only",
    supported_regimes=(
        AvalancheRegime.DRY_SLAB,
        AvalancheRegime.DRY_LOOSE,
        AvalancheRegime.WET_SNOW,
    ),
    required_inputs=(
        _req("condition_pack", InputKind.PACK, None, "Complete immutable hourly forcing pack."),
        _req("terrain_class", InputKind.PACK, None, "Representative-slope site and terrain contract."),
        _req("initial_snow_state", InputKind.PACK, None, "Reviewed initial snow and soil state."),
        _req("snowpack_configuration", InputKind.PACK, None, "Explicit SNOWPACK configuration and site parameters."),
    ),
    output_capabilities=(OutputQuantity.SNOW_STATE,),
    deterministic=True,
    selection_priority=10,
    applicability=(
        "Representative columns with complete forcing, initialization, and reviewed site parameters.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "The current Hosmer forcing lacks radiation and three other hours; no Hosmer run is eligible.",
        "Modelled weak layers are model output, never field observations.",
    ),
)


BC_PRA = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="release.bc_sfu_pra",
    display_name="Sykes-Haegeli-Buehler BC PRA candidate",
    stage=EngineStage.RELEASE,
    implementation_version="osf-yq5s3-2021",
    adapter_version="not-integrated-v1",
    execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
    source_url="https://doi.org/10.17605/OSF.IO/YQ5S3",
    license_spdx="GPL-3.0-only",
    supported_regimes=(AvalancheRegime.DRY_SLAB,),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected bare-earth or declared surface DEM."),
        _req("forest_density", InputKind.RASTER, "1", "Ordinal forest-density input with preserved mask."),
        _req("snow_depth", InputKind.RASTER, "m", "Observed or modelled snow depth with lineage."),
        _req("release_parameters", InputKind.PACK, None, "Locally selected and validation-scoped PRA parameters."),
    ),
    output_capabilities=(OutputQuantity.RELEASE_EXTENT,),
    deterministic=True,
    selection_priority=10,
    applicability=(
        "Terrain-indication PRA workflow; parameters require calibration evidence for the target domain.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "OSF project licence is confirmed as GPL-3.0, but the published code is inside a 1.52 GB bundle whose file-level notices have not been inspected.",
        "No source code has been copied into this repository and the engine remains unavailable.",
    ),
)


AVYCORE_RELEASE_BASELINE = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="release.avycore_baseline",
    display_name="AvyCore PRA-style relative-index baseline",
    stage=EngineStage.RELEASE,
    implementation_version="avycore-release-v1",
    adapter_version="legacy-api-v1",
    execution_boundary=ExecutionBoundary.IN_PROCESS_BASELINE,
    source_url="repository://packages/avycore/src/avycore/hazard/risk.py",
    license_spdx="NOASSERTION",
    supported_regimes=(AvalancheRegime.DRY_SLAB,),
    required_inputs=(
        _req("slope", InputKind.RASTER, "degree", "Terrain slope with source mask."),
        _req("aspect", InputKind.RASTER, "degree", "Downslope aspect clockwise from true north."),
        _req("general_curvature", InputKind.RASTER, "m-1", "General curvature with source mask."),
        _req("plan_curvature", InputKind.RASTER, "m-1", "Plan curvature with source mask."),
        _req("forest_fraction", InputKind.RASTER, "1", "Forest fraction with source mask."),
        _req("new_snow_depth", InputKind.SCALAR, "cm", "Explicit scenario new-snow depth."),
        _req("wind_speed", InputKind.SCALAR, "km h-1", "Explicit representative wind speed."),
        _req("wind_from_direction", InputKind.SCALAR, "degree", "Meteorological wind FROM direction."),
    ),
    output_capabilities=(OutputQuantity.RELEASE_INDEX, OutputQuantity.RELEASE_EXTENT),
    deterministic=True,
    selection_priority=100,
    applicability=(
        "Uncalibrated dry-slab terrain/loading relative-index baseline only.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "Relative index, not a probability or occurrence prediction.",
        "Does not produce release thickness, density, mass, or a snow-profile instability result.",
    ),
)


AVAFRAME_COM1DFA = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.avaframe_com1dfa",
    display_name="AvaFrame com1DFA dense-flow",
    stage=EngineStage.RUNOUT,
    implementation_version="2.1",
    adapter_version="avycore-avaframe-com1dfa-v1",
    execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
    source_url="https://github.com/OpenNHM/AvaFrame",
    license_spdx="EUPL-1.2",
    supported_regimes=(AvalancheRegime.DENSE_DRY,),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected metre-based DEM with an explicit mask and CRS."),
        _req("release_area", InputKind.VECTOR, None, "Projected release Polygon or MultiPolygon."),
        _req("release_thickness", InputKind.SCALAR, "m", "Release thickness measured normal to slope."),
        _req("release_density", InputKind.SCALAR, "kg m-3", "Explicit release snow density."),
        _req("voellmy_mu", InputKind.SCALAR, "1", "Explicit Coulomb friction coefficient."),
        _req("voellmy_xi", InputKind.SCALAR, "m s-2", "Explicit Voellmy turbulent friction coefficient."),
        _req("entrainment_enabled", InputKind.FLAG, None, "Explicit entrainment choice; false requires no entrainment input."),
        _req("simulation_time", InputKind.SCALAR, "s", "Explicit maximum simulation time."),
        _req("time_step", InputKind.SCALAR, "s", "Explicit fixed numerical time step."),
    ),
    parameter_validity=tuple(
        ParameterValidityRange(
            input_name=name,
            unit=unit,
            lower=0.0,
            lower_inclusive=False,
            basis="adapter_execution_domain",
            interpretation="Positive-value execution constraint; not an accuracy or calibration range.",
        )
        for name, unit in (
            ("release_thickness", "m"),
            ("release_density", "kg m-3"),
            ("voellmy_mu", "1"),
            ("voellmy_xi", "m s-2"),
            ("simulation_time", "s"),
            ("time_step", "s"),
        )
    ),
    spatial_applicability=(
        SpatialApplicability(
            input_names=("terrain_dem", "release_area"),
            require_projected_metre_crs=True,
            require_same_crs=True,
            coordinate_order="x,y",
            interpretation="com1DFA inputs are prepared on one projected metre-based x,y domain.",
        ),
    ),
    output_capabilities=(
        OutputQuantity.RUNOUT_EXTENT,
        OutputQuantity.FLOW_DEPTH,
        OutputQuantity.FLOW_VELOCITY,
        OutputQuantity.FLOW_PRESSURE,
    ),
    deterministic=True,
    selection_priority=10,
    applicability=(
        "Dense dry-flow scenarios with explicit release mass inputs and Voellmy parameters.",
        "Current vertical slice supports no entrainment and one release scenario per isolated run.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "Velocity, depth, and pressure are simulation outputs, not validated impact predictions.",
        "No Mount Hosmer friction calibration, mass-balance acceptance result, or field holdout exists.",
    ),
)


AVAFRAME_FLOWPY = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.avaframe_flowpy",
    display_name="Flow-Py routing via AvaFrame com4FlowPy",
    stage=EngineStage.RUNOUT,
    implementation_version="2.1",
    adapter_version="avycore-flowpy-com4-v1",
    execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
    source_url="https://docs.avaframe.org/en/latest/moduleCom4FlowPy.html",
    license_spdx="EUPL-1.2",
    supported_regimes=(AvalancheRegime.DENSE_DRY, AvalancheRegime.DEBRIS_FLOW),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected metre-based DEM with an explicit mask and CRS."),
        _req("release_area", InputKind.RASTER, "1", "Release-cell raster with preserved unknown mask; positive cells release."),
        _req("alpha_angle", InputKind.SCALAR, "degree", "Explicit angle of reach; tan(alpha) is the sliding-block Coulomb friction."),
        _req("flowpy_exponent", InputKind.SCALAR, "1", "Explicit lateral-spreading exponent; larger values confine the path."),
        _req("flux_threshold", InputKind.SCALAR, "1", "Explicit minimum routed flux fraction retained per cell."),
        _req("max_energy_line_height", InputKind.SCALAR, "m", "Explicit z-delta (energy-line height) limit."),
    ),
    parameter_validity=(
        ParameterValidityRange(
            input_name="alpha_angle",
            unit="degree",
            lower=0.0,
            upper=90.0,
            lower_inclusive=False,
            upper_inclusive=False,
            basis="upstream_documentation",
            interpretation="com4FlowPy needs an angle of reach inside (0, 90) degrees; this is not a calibrated range.",
        ),
        ParameterValidityRange(
            input_name="flowpy_exponent",
            unit="1",
            lower=1.0,
            basis="upstream_documentation",
            interpretation="Upstream treats the spreading exponent as an integer of at least one; not a calibrated range.",
        ),
        ParameterValidityRange(
            input_name="flux_threshold",
            unit="1",
            lower=0.0,
            upper=1.0,
            lower_inclusive=False,
            upper_inclusive=False,
            basis="upstream_documentation",
            interpretation="Routed flux is a fraction of the release flux, so the cut-off lies strictly inside (0, 1).",
        ),
        ParameterValidityRange(
            input_name="max_energy_line_height",
            unit="m",
            lower=0.0,
            lower_inclusive=False,
            basis="adapter_execution_domain",
            interpretation="Positive-value execution constraint; not a calibrated velocity limit.",
        ),
    ),
    spatial_applicability=(
        SpatialApplicability(
            input_names=("terrain_dem", "release_area"),
            require_projected_metre_crs=True,
            require_same_crs=True,
            coordinate_order="x,y",
            interpretation="com4FlowPy requires every input raster on one projected metre-based x,y grid.",
        ),
    ),
    output_capabilities=(
        OutputQuantity.RUNOUT_EXTENT,
        OutputQuantity.ENERGY_LINE_HEIGHT,
        OutputQuantity.TRAVEL_ANGLE,
    ),
    deterministic=True,
    selection_priority=20,
    applicability=(
        "Regional-scale statistical routing and stopping of gravitational mass flows.",
        "Not a time-dependent, mass- and momentum-conserving dense-flow solution.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "This engine executes AvaFrame's com4FlowPy port of Flow-Py, not the archived standalone Flow-Py distribution; the executed module file hashes are recorded per run.",
        "AvaFrame documents com4FlowPy as under heavy development and outside its automatic test coverage.",
        "Flow depth, flow velocity, flow pressure, and arrival time are not produced and are published as unsupported.",
        "z-delta is an energy-line height. Upstream documents the sliding-block bound max_v = sqrt(2 g z_delta); that bound is not a simulated flow velocity.",
        "Forest, infrastructure, variable-alpha, variable-exponent, and variable-uMax modules are disabled in this slice.",
    ),
)


UPSTREAM_FLOWPY = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.flowpy_upstream",
    display_name="Flow-Py standalone upstream distribution",
    stage=EngineStage.RUNOUT,
    implementation_version="1.0.3",
    adapter_version="avycore-flowpy-upstream-v1",
    execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
    source_url="https://github.com/avaframe/FlowPy",
    license_spdx="GPL-3.0-or-later",
    supported_regimes=(AvalancheRegime.DENSE_DRY, AvalancheRegime.DEBRIS_FLOW),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected metre-based DEM with an explicit mask and CRS."),
        _req("release_area", InputKind.RASTER, "1", "Release-cell raster with preserved unknown mask; positive cells release."),
        _req("alpha_angle", InputKind.SCALAR, "degree", "Explicit angle of reach passed as the first positional argument."),
        _req("flowpy_exponent", InputKind.SCALAR, "1", "Explicit lateral-spreading exponent passed as the second positional argument."),
        _req("flux_threshold", InputKind.SCALAR, "1", "Explicit minimum routed flux fraction (flux= keyword argument)."),
        _req("max_energy_line_height", InputKind.SCALAR, "m", "Explicit z-delta limit (max_z= keyword argument)."),
    ),
    spatial_applicability=(
        SpatialApplicability(
            input_names=("terrain_dem", "release_area"),
            require_projected_metre_crs=True,
            require_same_crs=True,
            coordinate_order="x,y",
            interpretation="Upstream requires identical resolution, extent, and CRS for every input raster.",
        ),
    ),
    output_capabilities=(
        OutputQuantity.RUNOUT_EXTENT,
        OutputQuantity.ENERGY_LINE_HEIGHT,
        OutputQuantity.TRAVEL_ANGLE,
    ),
    deterministic=True,
    selection_priority=25,
    applicability=(
        "Reference identity for the canonical standalone Flow-Py distribution, kept distinct from the AvaFrame port.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "The upstream repository is archived read-only since 2024-09-17; its last release is v1.0.3 (2022-06-14).",
        "In released tag v1.0.3 (commit 7b061599355cef584491d69eae2686307d286901) main.py reassigns argv to a hardcoded example, so the released command line ignores its arguments and cannot be driven as a bounded adapter.",
        "Only the later untagged master commit 27ad81d3e804e4e9d85a9773fca10ee7dc428183 comments that assignment out; no release carries the fix.",
        "main.py imports PyQt5 unconditionally, so even terminal mode needs a GUI toolkit in the isolated environment.",
        "This adapter therefore stays fail-closed and never substitutes the AvaFrame com4FlowPy port for the standalone distribution.",
    ),
)


R_AVAFLOW = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.r_avaflow",
    display_name="r.avaflow isolated candidate",
    stage=EngineStage.RUNOUT,
    implementation_version="4.0-revision-7",
    adapter_version="availability-only-v1",
    execution_boundary=ExecutionBoundary.OFFLINE_CONTAINER,
    source_url="https://www.landslidemodels.org/r.avaflow/",
    license_spdx="NOASSERTION",
    supported_regimes=(
        AvalancheRegime.DENSE_DRY,
        AvalancheRegime.WET_SNOW,
        AvalancheRegime.DEBRIS_FLOW,
    ),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected metre-based DEM."),
        _req("release_height", InputKind.RASTER, "m", "Explicit solid release-height raster."),
        _req("phase_parameters", InputKind.PACK, None, "Complete version-bound phase and friction configuration."),
        _req("entrainment_enabled", InputKind.FLAG, None, "Explicit entrainment choice."),
    ),
    output_capabilities=(
        OutputQuantity.RUNOUT_EXTENT,
        OutputQuantity.FLOW_DEPTH,
        OutputQuantity.FLOW_VELOCITY,
        OutputQuantity.FLOW_PRESSURE,
    ),
    deterministic=True,
    selection_priority=30,
    applicability=(
        "Complex mass-flow scenarios only after exact phase/configuration applicability review.",
    ),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "No isolated image, exact redistribution licence record, or normalized parser is configured.",
        "The engine must not be selected until its executable closure and output semantics are verified.",
    ),
)


AVYCORE_ALPHA_BASELINE = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.avycore_alpha",
    display_name="AvyCore alpha-angle routing baseline",
    stage=EngineStage.RUNOUT,
    implementation_version="avycore-alpha-v1",
    adapter_version="legacy-api-v1",
    execution_boundary=ExecutionBoundary.IN_PROCESS_BASELINE,
    source_url="repository://packages/avycore/src/avycore/hazard/runout.py",
    license_spdx="NOASSERTION",
    supported_regimes=(AvalancheRegime.DENSE_DRY,),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected DEM with preserved mask."),
        _req("release_area", InputKind.RASTER, "1", "Release-zone cells."),
        _req("alpha_angle", InputKind.SCALAR, "degree", "Explicit or declared regional angle of reach."),
    ),
    output_capabilities=(OutputQuantity.RUNOUT_EXTENT,),
    deterministic=True,
    selection_priority=100,
    applicability=("Empirical angle-of-reach routing baseline.",),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "No flow depth, velocity, pressure, mass conservation, or entrainment physics.",
        "Regional alpha ranges are not locally calibrated to Mount Hosmer.",
    ),
)


AVYCORE_PARTICLE_BASELINE = EngineDescriptor(
    schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
    engine_id="runout.avycore_particle",
    display_name="AvyCore experimental particle baseline",
    stage=EngineStage.RUNOUT,
    implementation_version="avycore-particle-v1",
    adapter_version="legacy-api-v1",
    execution_boundary=ExecutionBoundary.IN_PROCESS_BASELINE,
    source_url="repository://packages/avycore/src/avycore/hazard/runout.py",
    license_spdx="NOASSERTION",
    supported_regimes=(AvalancheRegime.DENSE_DRY,),
    required_inputs=(
        _req("terrain_dem", InputKind.RASTER, "m", "Projected DEM with preserved mask."),
        _req("forest_fraction", InputKind.RASTER, "1", "Forest fraction with preserved mask."),
        _req("plan_curvature", InputKind.RASTER, "m-1", "Plan curvature with preserved mask."),
        _req("release_area", InputKind.RASTER, "1", "Release-zone cells."),
        _req("release_density", InputKind.SCALAR, "kg m-3", "Explicit density if normalized as a mass run."),
        _req("friction_parameters", InputKind.PACK, None, "Explicit particle-friction parameter manifest."),
    ),
    output_capabilities=(OutputQuantity.RUNOUT_EXTENT, OutputQuantity.FLOW_VELOCITY),
    deterministic=True,
    selection_priority=110,
    applicability=("Experimental comparison baseline on complete required terrain.",),
    validation=NO_FIELD_VALIDATION,
    limitations=(
        "Not a mass-conserving depth-averaged solver and not field validated.",
        "Serving hybrid mode includes an empirical alpha energy line.",
    ),
)


ENGINE_DESCRIPTORS = (
    SNOWPACK,
    BC_PRA,
    AVYCORE_RELEASE_BASELINE,
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
    UPSTREAM_FLOWPY,
    R_AVAFLOW,
    AVYCORE_ALPHA_BASELINE,
    AVYCORE_PARTICLE_BASELINE,
)


def canonical_engine_registry() -> EngineRegistry:
    """Return the portable catalogue without probing or importing external tools."""

    available_baselines = {
        AVYCORE_RELEASE_BASELINE.engine_id,
        AVYCORE_ALPHA_BASELINE.engine_id,
        AVYCORE_PARTICLE_BASELINE.engine_id,
    }
    reasons = {
        SNOWPACK.engine_id: "The reusable offline boundary exists, but no complete eligible Hosmer forcing/configuration is bound to this request.",
        BC_PRA.engine_id: "Project-level GPL-3.0 licence is confirmed; source files and file-level notices have not been inspected, so no code is integrated.",
        AVAFRAME_COM1DFA.engine_id: "AvaFrame must be probed by the offline adapter with an explicit Python environment.",
        AVAFRAME_FLOWPY.engine_id: "The com4FlowPy adapter must probe an explicit AvaFrame Python environment; the portable catalogue never launches one.",
        UPSTREAM_FLOWPY.engine_id: "No reviewed standalone Flow-Py checkout is configured, and the released v1.0.3 command line ignores its arguments.",
        R_AVAFLOW.engine_id: "No version-bound isolated image, exact licence record, or normalized output parser is configured.",
    }
    registry = EngineRegistry()
    for descriptor in ENGINE_DESCRIPTORS:
        is_baseline = descriptor.engine_id in available_baselines
        availability = EngineAvailability(
            engine_id=descriptor.engine_id,
            status=(AvailabilityStatus.AVAILABLE if is_baseline else AvailabilityStatus.UNAVAILABLE),
            reason=(
                "Canonical in-process baseline is available through its existing AvyCore API."
                if is_baseline
                else reasons[descriptor.engine_id]
            ),
            detected_version=(descriptor.implementation_version if is_baseline else None),
        )
        registry.register(StaticEnginePlugin(descriptor, availability))
    return registry


def descriptor_by_id(engine_id: str) -> EngineDescriptor:
    for descriptor in ENGINE_DESCRIPTORS:
        if descriptor.engine_id == engine_id:
            return descriptor
    raise KeyError(f"Unknown canonical engine descriptor {engine_id!r}.")


__all__ = [
    "AVAFRAME_COM1DFA",
    "AVAFRAME_FLOWPY",
    "AVYCORE_ALPHA_BASELINE",
    "AVYCORE_PARTICLE_BASELINE",
    "AVYCORE_RELEASE_BASELINE",
    "BC_PRA",
    "ENGINE_DESCRIPTORS",
    "R_AVAFLOW",
    "SNOWPACK",
    "UPSTREAM_FLOWPY",
    "canonical_engine_registry",
    "descriptor_by_id",
]
