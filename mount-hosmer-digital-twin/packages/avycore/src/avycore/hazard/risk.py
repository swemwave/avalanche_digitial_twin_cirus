"""AvyCore's Stage 3 risk model: one simple, transparent release estimate.

This replaces the entire ``models/`` stack (snow, wind, instability, release-zone
and confidence modules -- ~2,300 LOC) with one readable file. The trade is
deliberate: Stage 3 takes its conditions from a handful of UI sliders, not from
weather ingestion. Structured serving-time observations are resolved by
``avycore.scenario``; unsupported snowpack and field records never enter these
equations. What remains is the physical core that actually decides where a slab can
release:

    release(slope, aspect, curvature, forest | new snow, wind speed, wind dir)

Every term is a documented, UNCALIBRATED rule from the avalanche-terrain
literature and Canadian Rockies practice. None of it is fitted to an observed
Mount Hosmer avalanche because no eligible local avalanche-observation dataset is
currently available to this project. It is a relative index from 0 to 100, never
a probability and never a forecast -- see :data:`DISCLAIMER`.

This module also owns :class:`ReleaseZone`, the small dataclass the runout engine
consumes (it reads only ``.pixels`` and ``.zone_id``); it lived in the old
``models/release_zones.py`` and moves here so ``models/`` can be deleted whole.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage

from . import geometry as geo
from .conditions import Conditions, compass_name
from .constants import DISCLAIMER
from .protocols import Terrain
from .zone import ReleaseZone, ReleaseZoneSet

__all__ = [
    "Conditions",
    "RiskField",
    "ReleaseZone",
    "ReleaseZoneSet",
    "compute_release",
    "extract_release_zones",
    "parameter_manifest",
]

# =============================================================================
# Tunable model constants -- UNCALIBRATED regional values, not fitted parameters.
# =============================================================================

#: Slab release concentrates 34-45 deg; rare below 25, sluffs above ~55. Piecewise
#: score (0-100) by physical slope breakpoints -- NOT a percentile stretch.
SLOPE_BREAKPOINTS_DEG = [0, 20, 25, 30, 34, 40, 45, 50, 55, 65, 90]
SLOPE_SCORES = [0, 0, 15, 55, 85, 100, 95, 75, 45, 15, 0]
SLOPE_MIN_DEG = 25.0
SLOPE_MAX_DEG = 60.0

#: Wind transport of dry snow: nothing below ~15 km/h, efficient by ~40 km/h.
WIND_TRANSPORT_MIN_KMH = 15.0
WIND_TRANSPORT_FULL_KMH = 40.0
#: A slope is fully "lee" when it faces directly downwind; cross-slopes load partly.
#: Convergent (gully-like) ground collects more wind-blown snow.

#: New snow that makes a storm slab. ~50 cm of new load saturates the term.
NEW_SNOW_FULL_CM = 50.0

#: Forest anchoring removes up to this fraction of the release potential.
FOREST_DAMPING_MAX = 0.7

#: How the static terrain capability and the dynamic loading combine. Even a
#: perfectly capable slope with no loading keeps only BASE of its score, so a
#: benign day (no snow, no wind) produces few or no release zones.
LOADING_BASE = 0.20
SNOW_LOADING_WEIGHT = 0.60
WIND_LOADING_WEIGHT = 0.75

#: Release-zone extraction.
RELEASE_THRESHOLD = 55.0          # a pixel must clear this to join a zone
MIN_ZONE_AREA_M2 = 2500.0         # and a zone must be big enough to hold a slab
SMOOTHING_RADIUS_M = 15.0
ASPECT_SECTOR_DEG = 45.0          # components never merge across aspect sectors
ELEVATION_BAND_M = 300.0          # nor across elevation bands
MAX_ZONES = 40


def parameter_manifest() -> dict[str, Any]:
    """Return every tunable that influences release scoring or zone extraction.

    The application hashes this with its runout parameters. Keeping the manifest
    beside the rules prevents a release-model change from retaining the same
    provenance fingerprint by accident.
    """
    from .conditions import RAIN_SNOW_LOWER_C, RAIN_SNOW_UPPER_C

    return {
        "rain_snow_lower_c": RAIN_SNOW_LOWER_C,
        "rain_snow_upper_c": RAIN_SNOW_UPPER_C,
        "slope_breakpoints_deg": list(SLOPE_BREAKPOINTS_DEG),
        "slope_scores": list(SLOPE_SCORES),
        "slope_min_deg": SLOPE_MIN_DEG,
        "slope_max_deg": SLOPE_MAX_DEG,
        "wind_transport_min_kmh": WIND_TRANSPORT_MIN_KMH,
        "wind_transport_full_kmh": WIND_TRANSPORT_FULL_KMH,
        "new_snow_full_cm": NEW_SNOW_FULL_CM,
        "forest_damping_max": FOREST_DAMPING_MAX,
        "loading_base": LOADING_BASE,
        "snow_loading_weight": SNOW_LOADING_WEIGHT,
        "wind_loading_weight": WIND_LOADING_WEIGHT,
        "release_threshold": RELEASE_THRESHOLD,
        "minimum_zone_area_m2": MIN_ZONE_AREA_M2,
        "smoothing_radius_m": SMOOTHING_RADIUS_M,
        "aspect_sector_deg": ASPECT_SECTOR_DEG,
        "elevation_band_m": ELEVATION_BAND_M,
        "maximum_zones": MAX_ZONES,
    }


# =============================================================================
# The release raster
# =============================================================================


@dataclass
class RiskField:
    """The release-estimate raster and the parts it was built from."""

    release: np.ma.MaskedArray            # 0-100 estimated release index
    slope_term: np.ndarray                # 0-1 diagnostics, for explanation/zones
    wind_load_term: np.ndarray
    loading: np.ndarray
    explanation: dict[str, Any]


def _piecewise(values: np.ndarray, breakpoints: list[float], scores: list[float]) -> np.ndarray:
    return np.interp(values, breakpoints, scores).astype("float32")


def compute_release(
    bt: Terrain,
    conditions: Conditions,
    *,
    condition_coverage: np.ndarray | None = None,
) -> RiskField:
    """Estimate release potential under explicit conditions.

    ``condition_coverage`` is the intersection of the spatial applicability of
    every active scenario input. Cells outside it are unknown, not low loading,
    and therefore join the required-terrain mask. The default ``None`` preserves
    the characterized whole-area scalar path exactly.
    """
    conditions = conditions.clamped()

    slope_layer = bt.layer("slope")
    aspect_layer = bt.layer("aspect")
    general_curv_layer = bt.layer("general_curvature")
    plan_curv_layer = bt.layer("plan_curvature")
    forest_layer = bt.layer("forest_mask")

    slope = np.asarray(slope_layer.filled(0.0))
    aspect = np.asarray(aspect_layer.filled(-1.0))
    general_curv = np.asarray(general_curv_layer.filled(0.0))
    plan_curv = np.asarray(plan_curv_layer.filled(0.0))
    forest = np.asarray(forest_layer.filled(0.0))

    # Every term is required to interpret a cell. A gap in aspect, curvature, or
    # canopy must not silently become a neutral numeric value and produce a
    # safe-looking score. Union the masks so missing data remains missing.
    mask = np.logical_or.reduce(
        [
            np.ma.getmaskarray(slope_layer),
            np.ma.getmaskarray(aspect_layer),
            np.ma.getmaskarray(general_curv_layer),
            np.ma.getmaskarray(plan_curv_layer),
            np.ma.getmaskarray(forest_layer),
        ]
    )
    if condition_coverage is not None:
        coverage = np.asarray(condition_coverage, dtype=bool)
        if coverage.shape != slope.shape:
            raise ValueError(
                f"Condition coverage shape {coverage.shape} does not match terrain {slope.shape}."
            )
        mask |= ~coverage
    valid = ~mask

    # --- Terrain capability (static): steep, convex, open ground ---------------
    slope_term = _piecewise(slope, SLOPE_BREAKPOINTS_DEG, SLOPE_SCORES) / 100.0

    curvature_values = np.abs(general_curv[valid & np.isfinite(general_curv)])
    curv_scale = float(np.percentile(curvature_values, 95)) if curvature_values.size else 1.0
    curv_scale = curv_scale or 1.0
    convex_term = np.clip(general_curv / curv_scale, 0.0, 1.0)  # convex rolls -> tension

    forest_damping = 1.0 - FOREST_DAMPING_MAX * np.clip(forest, 0.0, 1.0)

    # --- Loading (dynamic): supported explicit new-snow + wind inputs ----------
    # Air temperature, when supplied, classifies how much of the new precipitation
    # is snow rather than rain. This is a DRY-SLAB capability model, so rain does
    # not build a storm slab and must not be counted as one. It is not a claim that
    # a rain event is safer -- see the warning attached below.
    snow_fraction = conditions.snow_fraction
    snow_term = float(np.clip(conditions.effective_new_snow_cm / NEW_SNOW_FULL_CM, 0.0, 1.0))

    transport = float(
        np.clip(
            (conditions.wind_speed_kmh - WIND_TRANSPORT_MIN_KMH)
            / (WIND_TRANSPORT_FULL_KMH - WIND_TRANSPORT_MIN_KMH),
            0.0,
            1.0,
        )
    )
    # Lee slopes face downwind and collect the transported snow; windward slopes are
    # scoured. delta is the angle between a slope's aspect and the lee direction.
    lee_dir = (conditions.wind_direction_deg + 180.0) % 360.0
    delta = np.deg2rad(np.abs((aspect - lee_dir + 180.0) % 360.0 - 180.0))
    lee_factor = np.where(aspect < 0, 0.0, 0.5 * (1.0 + np.cos(delta)))  # 1 lee, 0.5 cross, 0 windward
    # Convergent cross-slope shape deepens wind deposits.
    plan_values = np.abs(plan_curv[valid & np.isfinite(plan_curv)])
    plan_scale = float(np.percentile(plan_values, 95)) if plan_values.size else 1.0
    plan_scale = plan_scale or 1.0
    convergence = np.clip(-plan_curv / plan_scale, 0.0, 1.0)
    wind_load_term = transport * lee_factor * (0.7 + 0.3 * convergence)

    loading = np.clip(
        SNOW_LOADING_WEIGHT * snow_term + WIND_LOADING_WEIGHT * wind_load_term, 0.0, 1.0
    )

    # --- Combine ---------------------------------------------------------------
    capability = slope_term * forest_damping * (0.85 + 0.15 * convex_term)
    release_values = 100.0 * capability * (LOADING_BASE + (1.0 - LOADING_BASE) * loading)

    # Terrain outside the slab window cannot release, whatever else is true.
    outside = (slope < SLOPE_MIN_DEG) | (slope > SLOPE_MAX_DEG)
    release_values[outside] *= 0.1
    release_values = np.clip(release_values, 0.0, 100.0).astype("float32")

    release = np.ma.array(release_values, mask=mask)

    explanation = {
        "model_type": "Simplified rules-based release estimate (0-100 relative index)",
        "is_probability": False,
        "is_operational_forecast": False,
        "disclaimer": DISCLAIMER,
        "conditions": conditions.to_dict(),
        "condition_scope": (
            "whole_area" if condition_coverage is None else "supported_area_only"
        ),
        "terms": {
            "slope_band": "Piecewise 34-45 deg optimal; zero outside 25-60 deg.",
            "wind_loading": (
                f"Wind {conditions.wind_speed_kmh:.0f} km/h from "
                f"{compass_name(conditions.wind_direction_deg)} loads the lee "
                f"({compass_name(lee_dir)}-facing) slopes; transport factor {transport:.2f}."
            ),
            "new_snow": (
                f"{conditions.new_snow_cm:.0f} cm new precipitation"
                + (
                    ""
                    if conditions.air_temperature_c is None
                    else (
                        f" at {conditions.air_temperature_c:.0f} degC -> "
                        f"{snow_fraction:.0%} classified as snow "
                        f"({conditions.effective_new_snow_cm:.0f} cm)"
                    )
                )
                + f" -> storm-slab load {snow_term:.2f}."
            ),
            "curvature": "Convex rolls add (slab tension); convergent gullies collect wind snow.",
            "forest": f"Forest anchoring removes up to {FOREST_DAMPING_MAX:.0%} of release potential.",
        },
        "limitations": [
            "Conditions are explicit user-entered scenario values. Their status and provenance "
            "are reported separately; the service does not verify representativeness.",
            *(
                []
                if snow_fraction >= 1.0
                else [
                    f"At {conditions.air_temperature_c:.0f} degC only {snow_fraction:.0%} of the "
                    "new precipitation is classified as snow, so the dry-slab loading term is "
                    "reduced. This is NOT a finding that the day is safer: rain-on-snow, wet-loose "
                    "and wet-slab avalanches are real hazards this dry-slab model does not "
                    "represent at all."
                ]
            ),
            "The model has no snow-profile data, so it cannot see buried weak layers -- the "
            "mechanism behind most avalanche fatalities.",
            "It is a relative index, not a probability, and is not calibrated against any observed "
            "Mount Hosmer avalanche.",
        ],
    }
    return RiskField(
        release=release,
        slope_term=slope_term,
        wind_load_term=np.asarray(wind_load_term, dtype="float32"),
        loading=np.asarray(loading, dtype="float32"),
        explanation=explanation,
    )


# =============================================================================
# Release zones
# =============================================================================


def vector_mean_aspect(aspect_values: np.ndarray) -> tuple[float | None, float | None]:
    """Dominant aspect as a vector mean (a bowl facing 350 and 10 deg faces N, not S)."""
    values = aspect_values[(aspect_values >= 0) & np.isfinite(aspect_values)]
    if values.size == 0:
        return None, None
    radians = np.deg2rad(values)
    east, north = float(np.sin(radians).mean()), float(np.cos(radians).mean())
    if abs(east) < 1e-12 and abs(north) < 1e-12:
        return None, 0.0
    direction = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    return round(direction, 1), round(math.hypot(east, north), 4)


def _segment(candidate: np.ndarray, aspect: np.ndarray, elevation: np.ndarray) -> tuple[np.ndarray, int]:
    """Label connected components WITHIN aspect sectors and elevation bands.

    Plain connected-component labelling fuses every face of a peak into one zone
    (the steep ground around a summit is topologically connected all the way
    round), producing a 'zone' that faces every direction and has no fall line.
    Growing components inside aspect sectors and elevation bands keeps each zone a
    coherent slab with a defined crown.
    """
    sectors = max(1, int(round(360.0 / ASPECT_SECTOR_DEG)))
    width = 360.0 / sectors
    binned = np.floor(((aspect + width / 2.0) % 360.0) / width).astype(np.int64)
    keys = np.where(aspect >= 0, binned, -1).astype(np.int64)
    bands = np.floor(np.nan_to_num(elevation, nan=0.0) / ELEVATION_BAND_M).astype(np.int64)
    keys = keys * 100_000 + bands

    labels = np.zeros(candidate.shape, dtype=np.int32)
    structure = np.ones((3, 3), dtype=int)
    count = 0
    for key in np.unique(keys[candidate]):
        component, found = ndimage.label(candidate & (keys == key), structure=structure)
        if found:
            hit = component > 0
            labels[hit] = component[hit] + count
            count += found
    return labels, count


def extract_release_zones(
    bt: Terrain, risk_field: RiskField, conditions: Conditions
) -> ReleaseZoneSet:
    """Threshold, smooth, label within aspect/elevation, and describe the zones."""
    grid = bt.grid
    release = risk_field.release
    slope = np.asarray(bt.layer("slope").filled(0.0))
    aspect = np.asarray(bt.layer("aspect").filled(-1.0))
    elevation = np.asarray(bt.layer("elevation").filled(np.nan))
    forest = np.asarray(bt.layer("forest_mask").filled(0.0))

    cell_area = grid.resolution_m**2
    min_pixels = max(1, int(round(MIN_ZONE_AREA_M2 / cell_area)))

    scores = np.asarray(release.filled(0.0))
    # A release zone must be terrain that can actually start a slab: above the score
    # threshold AND within the slope window AND not densely forested.
    candidate = (
        (scores >= RELEASE_THRESHOLD)
        & ~np.ma.getmaskarray(release)
        & (slope >= SLOPE_MIN_DEG)
        & (slope <= SLOPE_MAX_DEG)
        & (forest < 0.5)
    )

    warnings: list[str] = []
    if not candidate.any():
        warnings.append(
            f"No terrain reached the release threshold of {RELEASE_THRESHOLD:g} under these "
            f"conditions. This is NOT a statement that the mountain is safe."
        )
        return ReleaseZoneSet(zones=[], warnings=warnings, explanation={"zone_count": 0})

    # Close pinholes and shave single-pixel spurs so a zone is a coherent slope.
    radius = max(1, int(round(SMOOTHING_RADIUS_M / grid.resolution_m)))
    structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    smoothed = ndimage.binary_closing(candidate, structure=structure)
    smoothed = ndimage.binary_opening(smoothed, structure=np.ones((3, 3), dtype=bool))
    smoothed &= ~np.ma.getmaskarray(release)

    labels, count = _segment(smoothed, aspect, elevation)
    if count == 0:
        warnings.append("Candidate terrain did not survive smoothing; no release zones generated.")
        return ReleaseZoneSet(zones=[], warnings=warnings, explanation={"zone_count": 0})

    sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, count + 1))
    order = np.argsort(sizes)[::-1]

    zones: list[ReleaseZone] = []
    for index in order:
        if sizes[index] < min_pixels:
            continue
        if len(zones) >= MAX_ZONES:
            warnings.append(f"More than {MAX_ZONES} zones met the threshold; only the largest are shown.")
            break
        pixels = labels == (int(index) + 1)
        zone = _describe(bt, f"RZ{len(zones) + 1:03d}", pixels, release, slope, aspect, elevation, forest)
        if zone is not None:
            zones.append(zone)

    explanation = {
        "threshold": RELEASE_THRESHOLD,
        "minimum_area_m2": MIN_ZONE_AREA_M2,
        "aspect_sector_deg": ASPECT_SECTOR_DEG,
        "zone_count": len(zones),
        "total_release_area_km2": round(
            sum(zone.properties["area_m2"] for zone in zones) / 1_000_000.0, 4
        ),
        "is_probability": False,
        "disclaimer": DISCLAIMER,
        "limitations": [
            "A release zone is terrain the model considers capable of releasing under the given "
            "supported scenario conditions. It is NOT a prediction that an avalanche will occur there.",
            "The threshold is a configured value, not a calibrated one.",
        ],
    }
    return ReleaseZoneSet(zones=zones, warnings=warnings, explanation=explanation)


def _describe(
    bt: Terrain,
    zone_id: str,
    pixels: np.ndarray,
    release: np.ma.MaskedArray,
    slope: np.ndarray,
    aspect: np.ndarray,
    elevation: np.ndarray,
    forest: np.ndarray,
) -> ReleaseZone | None:
    geometry = geo.mask_to_geojson(pixels, bt.reproject, simplify_px=1.0)
    if geometry is None:
        return None

    area_m2 = float(pixels.sum()) * bt.grid.resolution_m**2
    dominant_aspect, aspect_consistency = vector_mean_aspect(aspect[pixels])
    elevations = elevation[pixels]
    elevations = elevations[np.isfinite(elevations)]
    mean_slope = round(float(slope[pixels].mean()), 1)
    max_slope = round(float(slope[pixels].max()), 1)
    mean_release = round(float(np.asarray(release.filled(0.0))[pixels].mean()), 1)
    forest_fraction = round(float(forest[pixels].mean()), 3)

    reasons: list[str] = []
    if 34 <= mean_slope <= 45:
        reasons.append(f"Mean slope {mean_slope:.0f} deg sits in the 34-45 deg slab-release band.")
    elif mean_slope > 45:
        reasons.append(f"Steep at {mean_slope:.0f} deg (up to {max_slope:.0f}).")
    else:
        reasons.append(f"Moderate slope {mean_slope:.0f} deg, low end of the release band.")
    if dominant_aspect is not None:
        reasons.append(f"Predominantly {compass_name(dominant_aspect)}-facing ({dominant_aspect:.0f} deg).")
    if forest_fraction < 0.15:
        reasons.append("Open terrain with little forest anchoring.")

    properties = {
        "zone_id": zone_id,
        "area_m2": round(area_m2, 1),
        "area_hectares": round(area_m2 / 10_000.0, 2),
        "mean_slope_deg": mean_slope,
        "max_slope_deg": max_slope,
        "dominant_aspect_deg": dominant_aspect,
        "dominant_aspect_compass": compass_name(dominant_aspect),
        "aspect_consistency": aspect_consistency,
        "elevation_min_m": round(float(elevations.min()), 1) if elevations.size else None,
        "elevation_max_m": round(float(elevations.max()), 1) if elevations.size else None,
        "elevation_mean_m": round(float(elevations.mean()), 1) if elevations.size else None,
        "forest_fraction": forest_fraction,
        "estimated_release_score": mean_release,
        "main_reasons": reasons,
        "is_probability": False,
        "score_type": "estimated_release_score (0-100 relative index)",
    }
    return ReleaseZone(zone_id=zone_id, pixels=pixels, geometry=geometry, properties=properties)
