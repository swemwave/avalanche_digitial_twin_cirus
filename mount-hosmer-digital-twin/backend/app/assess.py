"""The Stage 3 assessment: one synchronous answer to "what is this mountain doing
under these slider conditions?".

    sliders -> release raster -> release zones -> runout per zone -> one JSON

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
from pathlib import Path
from typing import Any

import numpy as np

from app.baked import BakedTerrain
from app.core.model_config import DISCLAIMER, ModelConfig
from app import geo, risk
from app.simulation import runout

#: The runout parameters. These are the ``runout:`` block of the legacy
#: avalanche_model.yaml -- published Canadian Rockies ranges for dry-slab
#: avalanches, UNCALIBRATED against any observed Mount Hosmer runout. Embedded here
#: so the running service needs no config file on disk.
RUNOUT_PARAMS: dict[str, Any] = {
    "alpha_angle_deg": {"small": 32.0, "medium": 27.0, "large": 23.0, "very_large": 19.0},
    "alpha_uncertainty_deg": 4.0,
    "friction": {"open_snow": 0.20, "forest": 0.35, "gully": 0.16, "xi_open": 1200.0, "xi_forest": 500.0},
    "fast_mode": {"spreading": 0.35, "max_path_length_m": 4000.0, "minimum_flux": 0.02},
    "advanced_mode": {
        "particles_per_zone": 300, "max_steps": 4000, "time_step_s": 0.2,
        "lateral_jitter": 0.35, "stopping_velocity_ms": 1.0,
        "random_seed": 20260713, "velocity_classes_ms": [5, 15, 25, 40],
    },
}

MODEL_VERSION = "stage3-1.0.0"

#: Combined-risk bands, for the result card. Relative ranking, not probability.
RISK_CLASSES = [
    (20, "Low", "#2f8a4c"),
    (40, "Moderate", "#d6c64b"),
    (60, "Considerable", "#dd8f35"),
    (80, "High", "#c23b35"),
    (100, "Extreme", "#4a1512"),
]

#: A synchronous request runs runout only for the highest-scoring zones. All
#: release zones are still shown; simulating and drawing 40 overlapping runout
#: cones is both slow and an unreadable red blob. The worst dozen are what matter.
#: Advanced (particle) mode is ~100x costlier, so it takes a tighter cap.
MAX_SIM_ZONES = 12
MAX_ADVANCED_ZONES = 6


def _config() -> ModelConfig:
    data = {"version": MODEL_VERSION, "runout": RUNOUT_PARAMS}
    sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    return ModelConfig(data, Path("stage3-embedded"), sha)


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


def _hazard_score(zones: list[risk.ReleaseZone], risk_field: risk.RiskField, bt: BakedTerrain) -> tuple[float, dict[str, Any]]:
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
        return 0.0, {"method": "No avalanche terrain in the AOI."}
    hazard = float(np.percentile(values, 95))
    return hazard, {
        "method": (
            "No release zone crossed the threshold. Hazard is the 95th percentile of the release "
            "estimate on avalanche terrain, so a below-threshold day is not reported as zero hazard."
        ),
        "zone_count": 0,
    }


def assess(
    bt: BakedTerrain,
    conditions: risk.Conditions,
    *,
    simulation_mode: str = "fast",
    seed: int | None = None,
) -> dict[str, Any]:
    """Run a full assessment and return the result payload."""
    started = time.perf_counter()
    conditions = conditions.clamped()
    config = _config()

    risk_field = risk.compute_release(bt, conditions)
    zone_set = risk.extract_release_zones(bt, risk_field, conditions)

    grid = bt.grid
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

    results: list[runout.RunoutResult] = []
    for zone in sim_zones:
        results.append(
            engine.simulate(
                zone=zone, grid=grid, elevation=elevation, slope=slope,
                forest_mask=forest, plan_curvature=plan_curvature,
                config=config, release_size=conditions.release_size, seed=seed,
            )
        )

    # Merge masks and build all geometry from them with app.geo (no rasterio).
    reached = np.zeros(grid.shape, dtype=bool)
    envelope = np.zeros(grid.shape, dtype=bool)
    velocity = np.zeros(grid.shape, dtype="float32")
    runout_polygons: list[dict[str, Any]] = []
    uncertainty_polygons: list[dict[str, Any]] = []
    main_paths: list[dict[str, Any]] = []

    # The elevation surface and its validity mask are the same for every zone's fall
    # line, so build them once here instead of once per zone inside the loop.
    z_elev = np.asarray(elevation.filled(np.nan), dtype="float64")
    valid_elev = ~np.ma.getmaskarray(elevation) & np.isfinite(z_elev)

    for zone, result in zip(sim_zones, results):
        reached |= result.reached
        envelope |= result.uncertainty
        velocity = np.maximum(velocity, result.velocity)
        footprint = geo.mask_to_geojson(result.reached, bt.reproject, simplify_px=1.5)
        if footprint:
            runout_polygons.append(footprint)
        band = geo.mask_to_geojson(result.uncertainty, bt.reproject, simplify_px=1.5)
        if band:
            uncertainty_polygons.append(band)
        path = _steepest_path_geojson(bt, zone, result.reached, elevation, z_elev, valid_elev)
        if path:
            main_paths.append(path)

    hazard, hazard_detail = _hazard_score(zone_set.zones, risk_field, bt)
    risk_level, risk_color = _classify(hazard)

    core_area = float(reached.sum()) * grid.resolution_m**2
    envelope_area = float(envelope.sum()) * grid.resolution_m**2

    warnings = sorted(
        set(
            zone_set.warnings
            + [w for result in results for w in result.warnings]
            + ([sim_note] if sim_note else [])
        )
    )

    payload = {
        "schema_version": 1,
        "generated_at_utc": _utc_now_iso(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "model": {"model_version": MODEL_VERSION, "config_sha256": config.sha256},
        "conditions": conditions.to_dict(),
        "simulation_mode": simulation_mode,
        "engine": engine.name,
        "release_size": conditions.release_size,
        "random_seed": seed if seed is not None else int(RUNOUT_PARAMS["advanced_mode"]["random_seed"]),
        "hazard_score": round(hazard, 1),
        "hazard_detail": hazard_detail,
        "risk_level": risk_level,
        "risk_color": risk_color,
        "release": risk_field.explanation,
        "release_zones": zone_set.to_geojson(),
        "release_zone_explanation": zone_set.explanation,
        "runout": {
            "core_area_m2": round(core_area, 1),
            "uncertainty_area_m2": round(envelope_area, 1),
            "max_velocity_ms": round(float(velocity.max()), 2) if velocity.any() else None,
            "runout_polygons": runout_polygons,
            "uncertainty_polygons": uncertainty_polygons,
            "main_paths": main_paths,
            "per_zone": [{"zone_id": r.zone_id, **r.metadata} for r in results],
        },
        "zones": [zone.properties for zone in zone_set.zones],
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
        "is_probability": False,
        "is_operational_forecast": False,
    }
    return payload


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
