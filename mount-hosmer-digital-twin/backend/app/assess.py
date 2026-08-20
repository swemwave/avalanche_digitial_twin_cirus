"""The Stage 3 assessment: one synchronous observation-scenario evaluation.

    explicit scenario -> supported-area mask -> release zones -> runout -> one JSON

It is fast enough (fast routing over the reached cells only) to run inside a
request, so unlike the legacy pipeline there is no job queue, no SQLite, no polling.
Geometry is built by :mod:`app.geo` from the returned numpy masks -- no rasterio,
no pyproj. The safety disclaimer is attached here, in code; it is never generated
by a model and never optional.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from avycore.hazard import composite
from avycore.hazard import geometry as geo
from avycore.hazard import risk, runout
from avycore.scenario import (
    SCENARIO_CANONICALIZATION_VERSION,
    ResolvedScenario,
    Scenario,
    parameter_catalog_manifest,
    resolve_scenario,
    scenario_from_legacy_conditions,
)
from avycore.validation.status import model_validation_status

from app.baked import BakedTerrain
from app.bake_identity import parameter_manifest_sha256
from app.core.model_config import DISCLAIMER, ModelConfig

#: The runout parameters. These are the ``runout:`` block of the legacy
#: avalanche_model.yaml -- published Canadian Rockies ranges for dry-slab
#: avalanches, UNCALIBRATED against any observed Mount Hosmer runout. Embedded here
#: so the running service needs no config file on disk.
RUNOUT_PARAMS: dict[str, Any] = {
    "alpha_angle_deg": {"small": 32.0, "medium": 27.0, "large": 23.0, "very_large": 19.0},
    "alpha_uncertainty_deg": 4.0,
    #: A user-supplied alpha angle replaces the configured regional value for a
    #: sensitivity run. It is bounded to a reviewed envelope so the override cannot
    #: express an angle outside the range the literature describes for any release
    #: size, and every result that used one says so. UNCALIBRATED, like the defaults
    #: it replaces.
    "alpha_override_bounds_deg": {"minimum": 15.0, "maximum": 40.0},
    #: Flow-regime friction sets. ``dry_slab`` reproduces the historical values
    #: exactly. The others are published literature ranges for their flow type --
    #: wet flow runs on higher Coulomb friction and stops sooner; powder runs on
    #: lower friction and further. None is calibrated to Mount Hosmer, and the
    #: runout engines remain dry-snow models being pointed at different constants,
    #: not wet-snow or powder-cloud physics.
    "flow_regime": {
        "dry_slab": {"mu_scale": 1.00, "xi_scale": 1.00, "alpha_shift_deg": 0.0},
        "wet_snow": {"mu_scale": 1.45, "xi_scale": 0.70, "alpha_shift_deg": 3.0},
        "powder": {"mu_scale": 0.75, "xi_scale": 1.40, "alpha_shift_deg": -2.0},
        "mixed": {"mu_scale": 1.10, "xi_scale": 0.90, "alpha_shift_deg": 1.0},
    },
    "friction": {"open_snow": 0.20, "forest": 0.35, "gully": 0.16, "xi_open": 1200.0, "xi_forest": 500.0},
    "fast_mode": {"spreading": 0.35, "max_path_length_m": 4000.0, "minimum_flux": 0.02},
    "advanced_mode": {
        "particles_per_zone": 300, "max_steps": 4000, "time_step_s": 0.2,
        "lateral_jitter": 0.35, "stopping_velocity_ms": 1.0,
        "random_seed": 20260713, "velocity_classes_ms": [5, 15, 25, 40],
    },
}

MODEL_VERSION = "stage3-1.2.0"

#: Index bands, shared by the release-potential index and the composite hazard
#: index. These deliberately do not use the operational avalanche-danger labels
#: Low/Moderate/Considerable/High/Extreme.
#:
#: The colours are a single perceptual sweep from light blue up to bright red,
#: with MONOTONICALLY DECREASING lightness, so the bands stay ordered in greyscale
#: and under colour-vision deficiency. The hue path is eased through lilac and
#: rose rather than run straight from blue to red, which would cross a muddy
#: near-grey middle where two adjacent bands would differ by lightness alone.
#: Validated in OKLab: monotone lightness, adjacent dL >= 0.080, adjacent dE >=
#: 11.0, worst all-pairs separation 8.8 dE (x100) under protanopia/deuteranopia.
#:
#: Bright rather than dark red at the top matters on a dark hillshade: the top
#: band still carries 4.36:1 against the map background, so the worst zones stay
#: the most saturated thing on the map instead of sinking into it. The lightness
#: fall across the whole ramp is shallower than a light-to-dark ramp would give
#: (0.080 per step, not 0.12), which is the price of ending on a vivid red; that
#: is why the map keeps band-ordinal fill opacity and line width as a second,
#: non-colour encoding. The client is handed the colour per zone so it never
#: re-derives a threshold, and interpolates between these five for the fill, so
#: the index reads as a continuous climb rather than five steps.
RISK_CLASSES = [
    (20, "Very low index", "#b9f5ff"),
    (40, "Low index", "#dfbfff"),
    (60, "Elevated index", "#eb92cf"),
    (80, "High index", "#ef6389"),
    (100, "Very high index", "#ea231a"),
]

#: A synchronous request runs runout only for the highest-scoring zones. All
#: release zones are still shown; simulating and drawing 40 overlapping runout
#: cones is both slow and an unreadable red blob. The worst dozen are what matter.
#: Advanced (particle) mode is ~100x costlier, so it takes a tighter cap.
MAX_SIM_ZONES = 12
MAX_ADVANCED_ZONES = 6

RELEASE_REQUIRED_LAYERS = (
    "slope",
    "aspect",
    "general_curvature",
    "plan_curvature",
    "forest_mask",
)


def assessment_parameter_manifest() -> dict[str, Any]:
    """Return every parameter that defines the deterministic assessment model."""
    return {
        "version": MODEL_VERSION,
        "release": risk.parameter_manifest(),
        "runout": deepcopy(RUNOUT_PARAMS),
        "composite_hazard_index": composite.parameter_manifest(),
    }


def assessment_model_identity() -> dict[str, Any]:
    """Return the parameter manifest and the exact hash published by assessments."""
    parameters = assessment_parameter_manifest()
    return {"parameter_manifest": parameters, "sha256": parameter_manifest_sha256(parameters)}


def _config() -> ModelConfig:
    identity = assessment_model_identity()
    return ModelConfig(
        identity["parameter_manifest"], Path("stage3-embedded"), identity["sha256"]
    )


def _classify(score: float) -> tuple[str, str]:
    for upper, label, color in RISK_CLASSES:
        if score <= upper:
            return label, color
    return RISK_CLASSES[-1][1], RISK_CLASSES[-1][2]


def _steepest_path_geojson(
    bt: BakedTerrain,
    zone: risk.ReleaseZone,
    reached: np.ndarray,
    elevation: np.ma.MaskedArray,
    z: np.ndarray,
    valid: np.ndarray,
) -> dict[str, Any] | None:
    """Trace the fall line through the runout, without rasterio (via app.geo).

    ``z`` and ``valid`` are the elevation surface and its validity mask -- the same
    for every zone, so the caller computes them once and passes them in rather than
    rebuilding two full-grid arrays per zone.
    """
    start_row, start_col, _ = runout._start_point(zone, elevation)
    path = runout._steepest_path((start_row, start_col), z, valid, reached)
    return geo.path_to_geojson(path, bt.reproject)


#: Why a zone has no reach or exposure term. Both are stated as missing, never
#: substituted with zero -- a zone nobody simulated is unknown, not harmless.
UNSIMULATED_REACH_REASON = (
    "Runout was not simulated for this zone (only the highest-scoring zones are run out in "
    "a synchronous assessment), so its reach is unknown. It was not recorded as zero."
)
UNSIMULATED_EXPOSURE_REASON = (
    "Runout was not simulated for this zone, so there is no footprint to measure exposure "
    "under. Exposure is unknown, not zero."
)
NO_EXPOSURE_LAYER_REASON = (
    "This bake carries no exposure layer, so nothing is known about what lies in any runout "
    "path. Every zone keeps its terrain-and-reach index unchanged; no zone was scored as "
    "having an empty valley below it."
)


def _exposure_context(bt: BakedTerrain) -> dict[str, Any] | None:
    """Load the baked exposure rasters, or None when this bake carries none.

    An older bake, or a pack that declares no exposure asset, must still assess.
    Missing here means the exposure term is reported unavailable for every zone --
    it never means the valley is empty.
    """
    meta = bt.meta.get("exposure")
    if not isinstance(meta, dict):
        return None
    try:
        weight_layer = bt.layer("exposure_weight")
        class_layer = bt.layer("exposure_class")
    except KeyError:
        return None

    classes = meta.get("classes", [])
    label_by_code = {
        int(item["code"]): str(item["label"])
        for item in classes
        if isinstance(item, dict) and "code" in item and "label" in item
    }
    weight_by_code = {
        int(item["code"]): float(item.get("weight", 0.0))
        for item in classes
        if isinstance(item, dict) and "code" in item
    }
    return {
        # ``np.asarray`` on the masked array yields the underlying values, which
        # carry NaN exactly where the mask is set -- so the known mask below is the
        # only thing that decides what counts as a measurement.
        "values": np.asarray(weight_layer),
        "codes": np.asarray(class_layer),
        "known": ~np.ma.getmaskarray(weight_layer),
        "label_by_code": label_by_code,
        "weight_by_code": weight_by_code,
        "meta": meta,
    }


def _class_labels(context: dict[str, Any], codes: np.ndarray) -> list[str]:
    """Baked class codes under a footprint, as labels, most consequential first."""
    label_by_code = context["label_by_code"]
    weight_by_code = context["weight_by_code"]
    present = {int(code) for code in np.unique(codes) if int(code) in label_by_code}
    return [
        label_by_code[code]
        for code in sorted(present, key=lambda item: -weight_by_code.get(item, 0.0))
    ]


def _zone_exposure(
    context: dict[str, Any] | None,
    reached: np.ndarray,
    envelope: np.ndarray,
    cell_area_m2: float,
) -> composite.ExposureComponent:
    """Measure the baked exposure layer under one zone's runout, in the loop.

    Called while ``result.reached`` and ``result.uncertainty`` are still live, so
    no full-grid mask is retained past the iteration that produced it.
    """
    if context is None:
        return composite.exposure_unavailable(NO_EXPOSURE_LAYER_REASON)

    known = context["known"]
    core_known = reached & known
    core_cells = int(np.count_nonzero(reached))
    known_cells = int(np.count_nonzero(core_known))
    if core_cells == 0:
        return composite.exposure_unavailable(
            "This zone produced no runout footprint, so there is nothing to measure exposure "
            "under."
        )
    if known_cells == 0:
        return composite.exposure_unavailable(
            "Every cell under this zone's runout lies outside the exposure layer's coverage, "
            "so exposure is unknown rather than zero."
        )

    values = context["values"]
    core_values = values[core_known]
    peak_core = float(core_values.max())
    covered_area = float(np.count_nonzero(core_values > 0.0)) * cell_area_m2

    envelope_known = envelope & known
    peak_envelope = (
        float(values[envelope_known].max()) if bool(envelope_known.any()) else peak_core
    )

    codes = context["codes"]
    core_labels = _class_labels(context, codes[core_known])
    envelope_labels = _class_labels(context, codes[envelope_known])

    component = composite.exposure_component(
        peak_core=peak_core,
        peak_envelope=peak_envelope,
        covered_area_m2=covered_area,
        classes=core_labels,
        known_cell_count=known_cells,
        unknown_cell_count=core_cells - known_cells,
    )
    component.measurements["classes_under_sensitivity_envelope"] = envelope_labels
    return component


def _release_potential_index(
    zones: list[risk.ReleaseZone], risk_field: risk.RiskField, bt: BakedTerrain
) -> tuple[float | None, dict[str, Any]]:
    """Area-weighted mean release across the zones; a percentile fallback if none.

    A below-threshold day must not silently read as hazard zero -- so where no zone
    crosses the threshold, the hazard is the high percentile of the release estimate
    on avalanche terrain, and it is labelled as such.
    """
    if zones:
        areas = np.array([z.properties["area_m2"] for z in zones], dtype="float64")
        scores = np.array([z.properties["estimated_release_score"] for z in zones], dtype="float64")
        hazard = float((areas * scores).sum() / areas.sum())
        return hazard, {
            "method": "Area-weighted mean estimated release score across all release zones.",
            "zone_count": len(zones),
            "max_zone_score": round(float(scores.max()), 1),
        }

    slope = np.asarray(bt.layer("slope").filled(0.0))
    forest = np.asarray(bt.layer("forest_mask").filled(0.0))
    terrain = (slope >= risk.SLOPE_MIN_DEG) & (slope <= risk.SLOPE_MAX_DEG) & (forest < 0.5)
    values = np.asarray(risk_field.release.filled(np.nan))[terrain]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None, {
            "method": (
                "No complete avalanche-terrain cells fall within the supported condition scope. "
                "Release potential is unavailable; missing coverage was not converted to zero."
            )
        }
    hazard = float(np.percentile(values, 95))
    return hazard, {
        "method": (
            "No release zone crossed the threshold. Hazard is the 95th percentile of the release "
            "estimate on avalanche terrain, so a below-threshold day is not reported as zero hazard."
        ),
        "zone_count": 0,
    }


def _coverage_scope(required_layers: tuple[str, ...], valid: np.ndarray) -> dict[str, Any]:
    cells = int(valid.size)
    valid_cells = int(np.count_nonzero(valid))
    return {
        "required_layers": list(required_layers),
        "grid_cell_count": cells,
        "valid_cell_count": valid_cells,
        "missing_cell_count": cells - valid_cells,
        "valid_fraction": round(valid_cells / cells, 6) if cells else 0.0,
    }


def _assessment_coverage(
    bt: BakedTerrain,
    risk_field: risk.RiskField,
    simulation_mode: str,
) -> dict[str, Any]:
    """Coverage of every required model input on the rectangular analysis grid."""

    release_valid = ~np.ma.getmaskarray(risk_field.release)
    elevation = bt.layer("elevation")
    elevation_valid = ~np.ma.getmaskarray(elevation)
    if simulation_mode == "advanced":
        runout_layers = ("elevation", "forest_mask", "plan_curvature")
        runout_valid = (
            elevation_valid
            & ~np.ma.getmaskarray(bt.layer("forest_mask"))
            & ~np.ma.getmaskarray(bt.layer("plan_curvature"))
        )
    else:
        runout_layers = ("elevation",)
        runout_valid = elevation_valid
    return {
        "denominator": (
            "All cells in the rectangular baked analysis grid. This is model-input coverage, "
            "not independently surveyed avalanche coverage."
        ),
        "release_model": _coverage_scope(RELEASE_REQUIRED_LAYERS, release_valid),
        "runout_model": _coverage_scope(runout_layers, runout_valid),
    }


def _categorical_source_mix(
    bt: BakedTerrain,
    footprint: np.ndarray,
    *,
    layer_name: str,
    metadata_name: str,
) -> dict[str, Any]:
    """Per-source counts under one model footprint, including unknown coverage."""

    layer = bt.layer(layer_name)
    values = np.asarray(layer.filled(0))
    footprint_cells = int(np.count_nonzero(footprint))
    known = footprint & ~np.ma.getmaskarray(layer)
    known_cells = int(np.count_nonzero(known))
    source_codes = bt.meta.get(metadata_name, {}).get("source_codes", {})
    counts: dict[str, int] = {}
    fractions: dict[str, float] = {}
    if known_cells:
        codes, code_counts = np.unique(values[known], return_counts=True)
        for code, count in zip(codes, code_counts):
            label = str(source_codes.get(str(int(code)), f"unlabelled_code_{int(code)}"))
            counts[label] = int(count)
            fractions[label] = round(int(count) / footprint_cells, 6)
    return {
        "footprint_cell_count": footprint_cells,
        "known_source_cell_count": known_cells,
        "missing_source_cell_count": footprint_cells - known_cells,
        "cell_count_by_source_label": counts,
        "fraction_of_footprint_by_source_label": fractions,
    }


def _footprint_source_mix(bt: BakedTerrain, footprint: np.ndarray) -> dict[str, Any]:
    return {
        "terrain": _categorical_source_mix(
            bt, footprint, layer_name="terrain_source", metadata_name="terrain"
        ),
        "forest": _categorical_source_mix(
            bt, footprint, layer_name="forest_source", metadata_name="forest"
        ),
    }


def _assessment_provenance(
    bt: BakedTerrain,
    *,
    resolved: ResolvedScenario,
    release_footprint: np.ndarray,
    runout_core: np.ndarray,
    runout_envelope: np.ndarray,
) -> dict[str, Any]:
    terrain = bt.meta.get("terrain", {})
    forest = bt.meta.get("forest", {})
    sources = bt.meta.get("sources", {})
    processing = bt.meta.get("processing", {})
    active_inputs = [
        item for item in resolved.scenario.inputs
        if item.parameter in {"new_snow_depth", "wind_speed", "wind_direction"}
        and item.status != "unknown"
    ]
    return {
        "conditions": {
            "kind": (
                "legacy_simple_scenario"
                if resolved.scenario.mode == "simple"
                else "structured_observation_scenario"
            ),
            "is_measurement": bool(active_inputs) and all(
                item.status == "measured" for item in active_inputs
            ),
            "scenario_sha256": resolved.scenario_sha256,
            "classification": resolved.classification,
        },
        "terrain_bake": {
            "bake_sha256": bt.bake_sha256,
            "source_fingerprint_sha256": sources.get("sha256"),
            "processing_sha256": processing.get("sha256"),
        },
        "terrain_source_coverage_by_label": dict(
            terrain.get("coverage_by_source_label", {})
        ),
        "forest_source_coverage_by_label": dict(
            forest.get("coverage_by_source_label", {})
        ),
        "footprint_source_mix": {
            "release_zones": _footprint_source_mix(bt, release_footprint),
            "runout_core": _footprint_source_mix(bt, runout_core),
            "runout_envelope": _footprint_source_mix(bt, runout_envelope),
        },
        "imagery": {
            "role": "visual_context_only",
            "used_in_release_or_runout": False,
        },
        "exposure": _exposure_provenance(bt),
        "serving_data_source": "runtime_baked_only",
    }


def _exposure_provenance(bt: BakedTerrain) -> dict[str, Any]:
    """Where the exposure layer came from, and the boundary it may not cross."""
    meta = bt.meta.get("exposure")
    base = {
        "role": "consequence_term_of_composite_hazard_index_only",
        "used_in_release_model": False,
        "used_in_runout_model": False,
        "boundary": (
            "Exposure never enters the release model: avycore.hazard.risk does not import it, "
            "is not passed it, and never sees it. It may only raise the exposure term of the "
            "composite hazard index, and can never lower a zone's index."
        ),
    }
    if not isinstance(meta, dict):
        return {**base, "available": False, "reason": NO_EXPOSURE_LAYER_REASON}
    return {
        **base,
        "available": True,
        "source": meta.get("source"),
        "licence": meta.get("licence"),
        "attribution": meta.get("attribution"),
        "settlement_derivation": meta.get("settlement_derivation", {}).get("rule"),
        "class_weight_by_name": meta.get("class_weight_by_name", {}),
        "is_calibrated": False,
        "limitation": meta.get("limitation"),
    }


def _effective_seed(simulation_mode: str, seed: int | None) -> int | None:
    if simulation_mode != "advanced":
        return None
    return (
        int(seed)
        if seed is not None
        else int(RUNOUT_PARAMS["advanced_mode"]["random_seed"])
    )


def _scenario_report(
    resolved: ResolvedScenario,
    *,
    model_version: str,
    config_sha256: str,
    bake_sha256: str,
    engine: str,
    random_seed: int | None,
) -> dict[str, Any]:
    report = deepcopy(resolved.report)
    scenario_contract_sha256 = hashlib.sha256(
        json.dumps(
            parameter_catalog_manifest(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    replay_payload = {
        "scenario_sha256": resolved.scenario_sha256,
        "scenario_contract_sha256": scenario_contract_sha256,
        "model_version": model_version,
        "config_sha256": config_sha256,
        "bake_sha256": bake_sha256,
        "engine": engine,
        "random_seed": random_seed,
    }
    replay_sha256 = hashlib.sha256(
        json.dumps(
            replay_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    report["reproducibility"] = {
        **replay_payload,
        "numerical_replay_sha256": replay_sha256,
        "canonicalization_version": SCENARIO_CANONICALIZATION_VERSION,
        "deterministic": True,
        "statement": (
            "This identity binds the normalized scenario, model/config, terrain bake, engine, "
            "and effective seed. Wall-clock generation time and duration are excluded."
        ),
    }
    return report


def _scenario_release_size(resolved: ResolvedScenario) -> str | None:
    record = next(
        (item for item in resolved.scenario.inputs if item.parameter == "release_size"), None
    )
    if record is None or record.status == "unknown" or not isinstance(record.value, str):
        return None
    return record.value


def _unavailable_assessment(
    bt: BakedTerrain,
    *,
    resolved: ResolvedScenario,
    config: ModelConfig,
    simulation_mode: str,
    seed: int | None,
    started: float,
) -> dict[str, Any]:
    """Return transparency/provenance without fabricating condition-dependent numerics."""

    engine = runout.get_engine(simulation_mode)
    actual_seed = _effective_seed(simulation_mode, seed)
    scenario_report = _scenario_report(
        resolved,
        model_version=MODEL_VERSION,
        config_sha256=config.sha256,
        bake_sha256=bt.bake_sha256,
        engine=engine.name,
        random_seed=actual_seed,
    )
    empty = np.zeros(bt.grid.shape, dtype=bool)
    provenance = _assessment_provenance(
        bt,
        resolved=resolved,
        release_footprint=empty,
        runout_core=empty,
        runout_envelope=empty,
    )
    release_valid = np.zeros(bt.grid.shape, dtype=bool)
    coverage = {
        "denominator": (
            "All cells in the rectangular baked analysis grid. Condition-dependent model "
            "coverage is zero when required conditions are unavailable; this is not a low score."
        ),
        "release_model": _coverage_scope(
            RELEASE_REQUIRED_LAYERS
            + (
                "condition:new_snow_depth",
                "condition:wind_speed",
                "condition:wind_direction_when_transporting",
                "assumption:release_size",
            ),
            release_valid,
        ),
        "runout_model": _coverage_scope(("elevation", "release_zone"), release_valid),
    }
    warnings = sorted(
        set(
            list(resolved.report["warnings"])
            + [
                "No condition-dependent release zones or runout were produced. Current loading "
                "remains unknown or unsupported; this is NOT a statement that the mountain is safe."
            ]
        )
    )
    release_size = _scenario_release_size(resolved)
    return {
        "schema_version": 3,
        "generated_at_utc": _utc_now_iso(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "model": {
            "model_version": MODEL_VERSION,
            "config_sha256": config.sha256,
            "bake_sha256": bt.bake_sha256,
        },
        "conditions": None,
        "simulation_mode": simulation_mode,
        "engine": engine.name,
        "release_size": release_size,
        "random_seed": actual_seed,
        "release_potential_index": None,
        "release_potential_band": None,
        "release_potential_color": None,
        "hazard_score": None,
        "hazard_detail": {
            "method": (
                "Unavailable because the implemented release model lacks complete supported "
                "current-condition inputs. Unknown values were not converted to zero."
            ),
            "zone_count": 0,
        },
        "risk_level": None,
        "risk_color": None,
        "area_hazard_index": None,
        "area_hazard_band": None,
        "area_hazard_color": None,
        "peak_zone_index": None,
        "peak_zone_id": None,
        "peak_zone_basis": None,
        "no_zone_release_percentile_index": None,
        "hazard_components": {
            "method": (
                "Unavailable because no release zone was computed at all. Release, reach and "
                "exposure are all unknown; none was converted to zero."
            ),
            "zone_count": 0,
            "contributing_zone_count": {"release": 0, "reach": 0, "exposure": 0},
            "exposure_layer": _exposure_provenance(bt),
            "is_probability": False,
            "is_calibrated": False,
        },
        "release": {
            "status": "unavailable_missing_inputs",
            "model_type": "Terrain foundation without condition-dependent release result",
            "is_probability": False,
            "is_operational_forecast": False,
            "disclaimer": DISCLAIMER,
            "limitations": [
                "Static LiDAR-derived terrain remains available, but current loading and weak-layer "
                "state are not sufficiently specified for a release result."
            ],
        },
        "release_zones": {
            "type": "FeatureCollection",
            "features": [],
            "zone_count": 0,
            "disclaimer": DISCLAIMER,
            "warnings": warnings,
        },
        "release_zone_explanation": {
            "zone_count": 0,
            "status": "unavailable_missing_inputs",
            "disclaimer": DISCLAIMER,
        },
        "runout": {
            "status": "unavailable_missing_inputs",
            "core_area_m2": None,
            "uncertainty_area_m2": None,
            "uncertainty_area_definition": (
                "Unavailable because no condition-dependent release zone was computed; null is not zero."
            ),
            "uncertainty_is_confidence_interval": False,
            "max_velocity_ms": None,
            "runout_polygons": [],
            "uncertainty_polygons": [],
            "main_paths": [],
            "per_zone": [],
        },
        "zones": [],
        "coverage": coverage,
        "provenance": provenance,
        "uncertainty": {
            "conditions": {
                "quantified": False,
                "reason": (
                    "Required condition values or their spatial applicability are incomplete. "
                    "Missing inputs remain explicit and no score distribution is available."
                ),
            },
            "release_potential": {
                "quantified": False,
                "reason": "Release potential is unavailable rather than represented by zero.",
            },
            "runout": {
                "kind": "unavailable_missing_release_input",
                "is_confidence_interval": False,
                "envelope_includes_core": True,
                "core_area_m2": None,
                "envelope_area_m2": None,
                "interpretation": (
                    "No runout sensitivity envelope exists because no supported release result was computed."
                ),
            },
        },
        "validation": model_validation_status(),
        "scenario": scenario_report,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        "is_probability": False,
        "is_operational_forecast": False,
    }


def assess(
    bt: BakedTerrain,
    conditions: risk.Conditions | None = None,
    *,
    scenario: Scenario | None = None,
    simulation_mode: str = "fast",
    seed: int | None = None,
) -> dict[str, Any]:
    """Run a full assessment and return the result payload."""
    started = time.perf_counter()
    if conditions is not None and scenario is not None:
        raise ValueError("Supply either legacy Conditions or a structured Scenario, not both.")
    if scenario is None:
        if conditions is None:
            raise ValueError("An explicit scenario or legacy Conditions object is required.")
        scenario = scenario_from_legacy_conditions(conditions)
    resolved = resolve_scenario(bt, scenario)
    config = _config()
    if not resolved.can_compute:
        return _unavailable_assessment(
            bt,
            resolved=resolved,
            config=config,
            simulation_mode=simulation_mode,
            seed=seed,
            started=started,
        )

    conditions = resolved.conditions
    assert conditions is not None

    risk_field = risk.compute_release(
        bt, conditions, condition_coverage=resolved.condition_coverage
    )
    zone_set = risk.extract_release_zones(bt, risk_field, conditions)

    # Materialize every all-zone output before selecting runout zones. ReleaseZone
    # objects retain one full-grid boolean mask each; the real terrain can produce
    # 40 zones (~220 MiB of masks), while only a small subset is simulated.
    hazard, hazard_detail = _release_potential_index(zone_set.zones, risk_field, bt)
    if hazard is None:
        report = deepcopy(resolved.report)
        report["warnings"] = list(report["warnings"]) + [hazard_detail["method"]]
        resolved = ResolvedScenario(
            scenario=resolved.scenario,
            conditions=resolved.conditions,
            condition_coverage=resolved.condition_coverage,
            can_compute=False,
            classification=resolved.classification,
            report=report,
            scenario_sha256=resolved.scenario_sha256,
        )
        return _unavailable_assessment(
            bt,
            resolved=resolved,
            config=config,
            simulation_mode=simulation_mode,
            seed=seed,
            started=started,
        )
    risk_level, risk_color = _classify(hazard)
    coverage = _assessment_coverage(bt, risk_field, simulation_mode)
    release_explanation = risk_field.explanation
    release_zones_payload = zone_set.to_geojson()
    release_zone_explanation = zone_set.explanation
    zones_payload = [zone.properties for zone in zone_set.zones]
    zone_warnings = list(zone_set.warnings)
    release_footprint = np.zeros(bt.grid.shape, dtype=bool)
    for release_zone in zone_set.zones:
        release_footprint |= release_zone.pixels
    if zone_set.zones:
        del release_zone

    grid = bt.grid
    cell_area_m2 = grid.resolution_m**2
    exposure_context = _exposure_context(bt)
    elevation = bt.layer("elevation")
    slope = bt.layer("slope")
    forest = bt.layer("forest_mask")
    plan_curvature = bt.layer("plan_curvature")

    engine = runout.get_engine(simulation_mode)

    # Simulate runout for the highest-scoring zones only, so the request stays
    # interactive and the map stays readable. Every release zone is still returned.
    cap = MAX_ADVANCED_ZONES if simulation_mode == "advanced" else MAX_SIM_ZONES
    sim_zones = zone_set.zones
    sim_note = None
    if len(sim_zones) > cap:
        sim_zones = sorted(
            zone_set.zones, key=lambda z: z.properties["estimated_release_score"], reverse=True
        )[:cap]
        sim_note = (
            f"Runout was simulated for the {cap} highest-scoring of {len(zone_set.zones)} release "
            f"zones. The others are shown as release zones but were not run out."
        )

    # Keep only masks that will actually seed a runout. Geometry/properties for all
    # zones have already been serialized above. Risk diagnostics are likewise no
    # longer needed once the index and coverage have been computed.
    zone_set.zones = []
    del zone_set
    del risk_field

    # Merge masks and build all geometry from them with app.geo (no rasterio).
    reached = np.zeros(grid.shape, dtype=bool)
    envelope = np.zeros(grid.shape, dtype=bool)
    max_velocity = 0.0
    runout_polygons: list[dict[str, Any]] = []
    uncertainty_polygons: list[dict[str, Any]] = []
    main_paths: list[dict[str, Any]] = []
    per_zone: list[dict[str, Any]] = []
    runout_warnings: list[str] = []
    # Reach and exposure are measured inside the streaming loop below, while each
    # result's full-grid masks are still alive. Only these small per-zone summaries
    # survive the iteration.
    zone_components: dict[str, tuple[composite.ReachComponent, composite.ExposureComponent]] = {}

    # The elevation surface and its validity mask are the same for every zone's fall
    # line, so build them once here instead of once per zone inside the loop.
    z_elev = np.asarray(elevation.filled(np.nan), dtype="float64")
    valid_elev = ~np.ma.getmaskarray(elevation) & np.isfinite(z_elev)

    # Stream one RunoutResult at a time. Each real-grid result is ~55 MiB, so
    # retaining all twelve fast results until aggregation used roughly 659 MiB.
    for zone in sim_zones:
        result = engine.simulate(
            zone=zone,
            grid=grid,
            elevation=elevation,
            slope=slope,
            forest_mask=forest,
            plan_curvature=plan_curvature,
            config=config,
            release_size=conditions.release_size,
            seed=seed,
            alpha_override_deg=conditions.alpha_angle_override_deg,
            flow_regime=conditions.flow_regime,
        )
        reached |= result.reached
        envelope |= result.uncertainty
        max_velocity = max(max_velocity, float(result.velocity.max()))
        footprint = geo.mask_to_geojson(result.reached, bt.reproject, simplify_px=1.5)
        if footprint:
            runout_polygons.append(footprint)
        band = geo.mask_to_geojson(result.uncertainty, bt.reproject, simplify_px=1.5)
        if band:
            uncertainty_polygons.append(band)
        path = _steepest_path_geojson(bt, zone, result.reached, elevation, z_elev, valid_elev)
        if path:
            main_paths.append(path)
        zone_components[result.zone_id] = (
            composite.reach_component(
                horizontal_reach_m=result.metadata.get("horizontal_reach_m"),
                vertical_drop_m=result.metadata.get("vertical_drop_m"),
                max_velocity_ms=result.metadata.get("max_velocity_ms"),
            ),
            _zone_exposure(exposure_context, result.reached, result.uncertainty, cell_area_m2),
        )
        per_zone.append({"zone_id": result.zone_id, **result.metadata})
        runout_warnings.extend(result.warnings)
        del result

    # The loop variable otherwise retains the last full-grid zone mask.
    if sim_zones:
        del zone
    sim_zones.clear()

    # Compose every zone's index. ``zones_payload`` holds the SAME property dicts
    # the GeoJSON features carry, so writing the composite fields here publishes
    # them on both without re-serializing any geometry.
    unsimulated = (
        composite.reach_unavailable(UNSIMULATED_REACH_REASON),
        composite.exposure_unavailable(
            UNSIMULATED_EXPOSURE_REASON
            if exposure_context is not None
            else NO_EXPOSURE_LAYER_REASON
        ),
    )
    zone_hazards: list[composite.ZoneHazard] = []
    for properties in zones_payload:
        reach, exposure = zone_components.get(properties["zone_id"], unsimulated)
        hazard_zone = composite.zone_hazard_index(
            zone_id=str(properties["zone_id"]),
            area_m2=float(properties["area_m2"]),
            release_score=float(properties["estimated_release_score"]),
            reach=reach,
            exposure=exposure,
        )
        zone_band, zone_color = _classify(hazard_zone.index)
        properties["hazard_index"] = round(hazard_zone.index, 1)
        properties["hazard_band"] = zone_band
        properties["hazard_color"] = zone_color
        properties["hazard_components"] = hazard_zone.to_dict()
        zone_hazards.append(hazard_zone)
    zone_components.clear()

    area_hazard = composite.aggregate_area_hazard(zone_hazards)
    if zone_hazards:
        # How many zones actually contributed each term. Without this, a result in
        # which 3 of 40 zones carry reach and exposure is indistinguishable from one
        # in which all 40 do.
        hazard_detail["component_contributing_zone_count"] = dict(
            area_hazard.components["contributing_zone_count"]
        )
        hazard_detail["zone_count_by_basis"] = dict(
            area_hazard.components["zone_count_by_basis"]
        )
    area_band, area_color = (
        _classify(area_hazard.index) if area_hazard.index is not None else (None, None)
    )
    hazard_components = dict(area_hazard.components)
    hazard_components["exposure_layer"] = _exposure_provenance(bt)
    hazard_components["band_thresholds"] = [
        {"upper": upper, "band": label, "color": color} for upper, label, color in RISK_CLASSES
    ]
    if sim_note:
        hazard_components["simulation_cap"] = sim_note
    # A below-threshold day has no zones and therefore no composite index. The
    # percentile fallback still exists, but it is a DIFFERENT quantity on the same
    # 0-100 scale, so it is published under its own name and never as area hazard.
    no_zone_percentile = None if zone_hazards else round(hazard, 1)
    if not zone_hazards:
        hazard_components["no_zone_fallback"] = {
            "field": "no_zone_release_percentile_index",
            "value": no_zone_percentile,
            "method": (
                "No release zone crossed the threshold, so no per-zone hazard index exists and "
                "area_hazard_index is null. release_potential_index falls back to the 95th "
                "percentile of the release estimate on avalanche terrain, which is published "
                "here under its own name: it measures terrain-and-loading only, shares no terms "
                "with the composite index, and must never be read as one."
            ),
        }
        hazard_components["method"] = hazard_components["no_zone_fallback"]["method"]

    core_area = float(reached.sum()) * grid.resolution_m**2
    envelope_area = float(envelope.sum()) * grid.resolution_m**2
    provenance = _assessment_provenance(
        bt,
        resolved=resolved,
        release_footprint=release_footprint,
        runout_core=reached,
        runout_envelope=envelope,
    )
    del release_footprint, reached, envelope, z_elev, valid_elev
    uncertainty_kind = (
        "alpha_angle_parameter_sensitivity_envelope"
        if simulation_mode == "fast"
        else "seeded_particle_ensemble_sensitivity_envelope"
    )

    composite_warnings: list[str] = []
    if zone_hazards and exposure_context is None:
        composite_warnings.append(
            "No exposure layer is available in this bake, so the hazard index carries no "
            "consequence term. This is not a finding that the runout paths are empty."
        )
    release_only = area_hazard.components.get("zone_count_by_basis", {}).get("release_only", 0)
    if zone_hazards and release_only:
        composite_warnings.append(
            f"{release_only} of {len(zone_hazards)} zones have no simulated runout, so their "
            "hazard index is release-only and carries no reach or exposure information. Those "
            "zones are labelled, not scored as low."
        )

    warnings = sorted(
        set(
            zone_warnings
            + runout_warnings
            + composite_warnings
            + list(resolved.report["warnings"])
            + ([sim_note] if sim_note else [])
        )
    )

    actual_seed = _effective_seed(simulation_mode, seed)
    scenario_report = _scenario_report(
        resolved,
        model_version=MODEL_VERSION,
        config_sha256=config.sha256,
        bake_sha256=bt.bake_sha256,
        engine=engine.name,
        random_seed=actual_seed,
    )
    conditions_payload = conditions.to_dict()
    conditions_payload["provenance"] = (
        "user_supplied_simple_scenario"
        if resolved.scenario.mode == "simple"
        else "structured_observation_scenario"
    )

    payload = {
        "schema_version": 3,
        "generated_at_utc": _utc_now_iso(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "model": {
            "model_version": MODEL_VERSION,
            "config_sha256": config.sha256,
            "bake_sha256": bt.bake_sha256,
        },
        "conditions": conditions_payload,
        "simulation_mode": simulation_mode,
        "engine": engine.name,
        "release_size": conditions.release_size,
        "random_seed": actual_seed,
        # The release-side index, unchanged in meaning: area-weighted mean of the
        # per-zone estimated release score, or the percentile fallback when no zone
        # crosses the threshold. It is now one component of the composite below.
        "release_potential_index": round(hazard, 1),
        "release_potential_band": risk_level,
        "release_potential_color": risk_color,
        # Legacy aliases retained for older clients; their meaning is the same
        # uncalibrated relative release-potential index, never avalanche danger.
        "hazard_score": round(hazard, 1),
        "hazard_detail": hazard_detail,
        "risk_level": risk_level,
        "risk_color": risk_color,
        # The composite: release x reach, raised by exposure, area-weighted across
        # zones. A separate quantity from release_potential_index, on its own names.
        "area_hazard_index": (
            round(area_hazard.index, 1) if area_hazard.index is not None else None
        ),
        "area_hazard_band": area_band,
        "area_hazard_color": area_color,
        "peak_zone_index": area_hazard.peak_zone_index,
        "peak_zone_id": area_hazard.peak_zone_id,
        "peak_zone_basis": area_hazard.peak_zone_basis,
        "no_zone_release_percentile_index": no_zone_percentile,
        "hazard_components": hazard_components,
        "release": release_explanation,
        "release_zones": release_zones_payload,
        "release_zone_explanation": release_zone_explanation,
        "runout": {
            "status": "computed" if per_zone else "no_release_zone",
            "core_area_m2": round(core_area, 1),
            "uncertainty_area_m2": round(envelope_area, 1),
            "uncertainty_area_definition": (
                "Total outer sensitivity-envelope area including the central footprint; it is "
                "not a band-only area and not a statistical confidence interval."
            ),
            "uncertainty_is_confidence_interval": False,
            "max_velocity_ms": round(max_velocity, 2) if max_velocity > 0 else None,
            "runout_polygons": runout_polygons,
            "uncertainty_polygons": uncertainty_polygons,
            "main_paths": main_paths,
            "per_zone": per_zone,
        },
        "zones": zones_payload,
        "coverage": coverage,
        "provenance": provenance,
        "uncertainty": {
            "conditions": {
                "quantified": False,
                "reason": (
                    "Input status, provenance, and supplied uncertainty are reported in the "
                    "scenario record, but the current deterministic equations do not propagate "
                    "condition uncertainty into a score distribution."
                ),
            },
            "release_potential": {
                "quantified": False,
                "reason": (
                    "No eligible local avalanche observations or snow-profile measurements are "
                    "available to estimate score uncertainty."
                ),
            },
            "runout": {
                "kind": uncertainty_kind,
                "is_confidence_interval": False,
                "envelope_includes_core": True,
                "core_area_m2": round(core_area, 1),
                "envelope_area_m2": round(envelope_area, 1),
                "interpretation": (
                    "This is sensitivity to configured parameters or ensemble routing, not a "
                    "probability distribution or confidence interval on real runout."
                ),
            },
        },
        "validation": model_validation_status(),
        "scenario": scenario_report,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        "is_probability": False,
        "is_operational_forecast": False,
    }
    return payload


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
