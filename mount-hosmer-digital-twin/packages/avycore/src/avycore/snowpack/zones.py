"""Regime-aware release-zone extraction.

Mirrors :func:`avycore.hazard.risk.extract_release_zones` exactly in structure --
same threshold, same closing/opening radii, same segmentation inside aspect
sectors and elevation bands, same minimum zone area, same zone cap -- and differs
only where a regime genuinely needs a different candidate window. The production
extractor hard-codes a dry-slab candidate window (25-60 deg, forest below 0.5);
applying it to a dry-loose sluff would discard the 60-75 deg ground where sluffs
actually start, and applying it to wet loose would discard partly forested slopes
that do produce wet-snow avalanches.

The segmentation helper is duplicated rather than imported from a private name,
and a regression test asserts that it labels an identical fixture identically, so
the two cannot drift apart silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import ndimage

from ..hazard import geometry as geo
from ..hazard.conditions import compass_name
from ..hazard.constants import DISCLAIMER
from ..hazard.protocols import Terrain
from ..hazard.risk import (
    ASPECT_SECTOR_DEG,
    ELEVATION_BAND_M,
    MAX_ZONES,
    MIN_ZONE_AREA_M2,
    RELEASE_THRESHOLD,
    SLOPE_MAX_DEG,
    SLOPE_MIN_DEG,
    SMOOTHING_RADIUS_M,
    vector_mean_aspect,
)
from ..hazard.zone import ReleaseZone, ReleaseZoneSet
from .regimes import (
    DRY_LOOSE,
    DRY_SLAB,
    FULL_DEPTH_GLIDE,
    GLIDE_MAX_FOREST_FRACTION,
    GLIDE_SLOPE_MAX_DEG,
    GLIDE_SLOPE_MIN_DEG,
    LOOSE_SLOPE_MAX_DEG,
    LOOSE_SLOPE_MIN_DEG,
    REGIMES,
    RegimeReleaseField,
    WET_SLOPE_MAX_DEG,
    WET_SLOPE_MIN_DEG,
    WET_SNOW,
)

__all__ = [
    "RegimeExtractionRule",
    "REGIME_EXTRACTION_RULES",
    "extract_regime_release_zones",
    "segment_within_aspect_and_elevation",
]

#: Production dry-slab candidate forest limit, reused verbatim.
DRY_SLAB_MAX_FOREST_FRACTION = 0.5
#: Wet loose and dry loose both run in open forest, so their candidate windows
#: are less restrictive than the slab window. These remain uncalibrated relative
#: judgements, not measured anchoring efficiencies.
WET_MAX_FOREST_FRACTION = 0.6
LOOSE_MAX_FOREST_FRACTION = 0.6


@dataclass(frozen=True)
class RegimeExtractionRule:
    """The candidate window a regime's zones are grown inside."""

    slope_min_deg: float
    slope_max_deg: float
    maximum_forest_fraction: float


REGIME_EXTRACTION_RULES: dict[str, RegimeExtractionRule] = {
    DRY_SLAB: RegimeExtractionRule(
        SLOPE_MIN_DEG, SLOPE_MAX_DEG, DRY_SLAB_MAX_FOREST_FRACTION
    ),
    WET_SNOW: RegimeExtractionRule(
        WET_SLOPE_MIN_DEG, WET_SLOPE_MAX_DEG, WET_MAX_FOREST_FRACTION
    ),
    DRY_LOOSE: RegimeExtractionRule(
        LOOSE_SLOPE_MIN_DEG, LOOSE_SLOPE_MAX_DEG, LOOSE_MAX_FOREST_FRACTION
    ),
    FULL_DEPTH_GLIDE: RegimeExtractionRule(
        GLIDE_SLOPE_MIN_DEG, GLIDE_SLOPE_MAX_DEG, GLIDE_MAX_FOREST_FRACTION
    ),
}


def segment_within_aspect_and_elevation(
    candidate: np.ndarray, aspect: np.ndarray, elevation: np.ndarray
) -> tuple[np.ndarray, int]:
    """Label connected components within aspect sectors and elevation bands.

    Identical in behaviour to the production segmentation: plain connected-
    component labelling fuses every face of a peak into one zone, producing a
    "zone" that faces every direction and has no fall line.
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


def _describe(
    bt: Terrain,
    zone_id: str,
    regime: str,
    pixels: np.ndarray,
    score: np.ndarray,
    slope: np.ndarray,
    aspect: np.ndarray,
    elevation: np.ndarray,
    forest: np.ndarray,
    state_properties: dict[str, Any],
) -> ReleaseZone | None:
    geometry = geo.mask_to_geojson(pixels, bt.reproject, simplify_px=1.0)
    if geometry is None:
        return None

    area_m2 = float(pixels.sum()) * bt.grid.resolution_m**2
    dominant_aspect, aspect_consistency = vector_mean_aspect(aspect[pixels])
    elevations = elevation[pixels]
    elevations = elevations[np.isfinite(elevations)]
    properties = {
        "zone_id": zone_id,
        "release_regime": regime,
        "area_m2": round(area_m2, 1),
        "area_hectares": round(area_m2 / 10_000.0, 2),
        "mean_slope_deg": round(float(slope[pixels].mean()), 1),
        "max_slope_deg": round(float(slope[pixels].max()), 1),
        "dominant_aspect_deg": dominant_aspect,
        "dominant_aspect_compass": compass_name(dominant_aspect),
        "aspect_consistency": aspect_consistency,
        "elevation_min_m": round(float(elevations.min()), 1) if elevations.size else None,
        "elevation_max_m": round(float(elevations.max()), 1) if elevations.size else None,
        "elevation_mean_m": round(float(elevations.mean()), 1) if elevations.size else None,
        "forest_fraction": round(float(forest[pixels].mean()), 3),
        "estimated_release_score": round(float(score[pixels].mean()), 1),
        "is_probability": False,
        "score_type": "estimated_release_score (0-100 relative index)",
        **{key: value for key, value in state_properties.items()},
    }
    return ReleaseZone(
        zone_id=zone_id, pixels=pixels, geometry=geometry, properties=properties
    )


def extract_regime_release_zones(
    bt: Terrain,
    field: RegimeReleaseField,
    *,
    state_layers: dict[str, np.ndarray] | None = None,
    threshold: float = RELEASE_THRESHOLD,
    maximum_zones_per_regime: int = MAX_ZONES,
) -> ReleaseZoneSet:
    """Extract zones separately for every regime, tagged with the regime.

    Zones are grown inside each regime's own candidate window and are never
    merged across regimes: a dry slab and a wet-snow start zone occupying the
    same ground are different mechanisms and are reported as different zones.
    """

    grid = bt.grid
    slope = np.asarray(bt.layer("slope").filled(0.0), dtype="float64")
    aspect = np.asarray(bt.layer("aspect").filled(-1.0), dtype="float64")
    elevation = np.asarray(bt.layer("elevation").filled(np.nan), dtype="float64")
    forest = np.asarray(bt.layer("forest_mask").filled(0.0), dtype="float64")
    missing = np.ma.getmaskarray(field.release)

    cell_area = grid.resolution_m**2
    min_pixels = max(1, int(round(MIN_ZONE_AREA_M2 / cell_area)))
    radius = max(1, int(round(SMOOTHING_RADIUS_M / grid.resolution_m)))
    closing_structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    opening_structure = np.ones((3, 3), dtype=bool)

    zones: list[ReleaseZone] = []
    warnings: list[str] = []
    per_regime: dict[str, Any] = {}
    for regime in REGIMES:
        regime_score = field.regime_scores[regime]
        rule = REGIME_EXTRACTION_RULES[regime]
        score = np.asarray(regime_score.score, dtype="float64")
        candidate = (
            regime_score.active
            & ~missing
            & (score >= threshold)
            & (slope >= rule.slope_min_deg)
            & (slope <= rule.slope_max_deg)
            & (forest < rule.maximum_forest_fraction)
        )
        summary: dict[str, Any] = {
            "supported": regime_score.supported,
            "active_cell_count": int(np.count_nonzero(regime_score.active)),
            "candidate_cell_count": int(np.count_nonzero(candidate)),
            "slope_window_deg": [rule.slope_min_deg, rule.slope_max_deg],
            "maximum_forest_fraction": rule.maximum_forest_fraction,
        }
        if not candidate.any():
            summary["zone_count"] = 0
            summary["reason"] = regime_score.unsupported_reason or (
                f"No cell in the {regime} candidate window reached the release threshold of "
                f"{threshold:g}. This is NOT a statement that the terrain is safe."
            )
            per_regime[regime] = summary
            continue

        smoothed = ndimage.binary_closing(candidate, structure=closing_structure)
        smoothed = ndimage.binary_opening(smoothed, structure=opening_structure)
        # Morphological closing may bridge across cells outside the physical
        # regime/candidate window. Re-apply every admissibility condition so
        # smoothing cannot invent release terrain or cross a missing-data hole.
        smoothed &= (
            regime_score.active
            & ~missing
            & (slope >= rule.slope_min_deg)
            & (slope <= rule.slope_max_deg)
            & (forest < rule.maximum_forest_fraction)
        )
        labels, count = segment_within_aspect_and_elevation(smoothed, aspect, elevation)
        if count == 0:
            summary["zone_count"] = 0
            summary["reason"] = "Candidate terrain did not survive smoothing."
            per_regime[regime] = summary
            continue

        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, count + 1))
        order = np.argsort(sizes)[::-1]
        accepted = 0
        for index in order:
            if sizes[index] < min_pixels:
                continue
            if accepted >= maximum_zones_per_regime:
                warnings.append(
                    f"More than {maximum_zones_per_regime} {regime} zones met the threshold; "
                    "only the largest are reported."
                )
                break
            pixels = labels == (int(index) + 1)
            state_properties = {
                key: round(float(np.asarray(values, dtype="float64")[pixels].mean()), 4)
                for key, values in (state_layers or {}).items()
            }
            zone = _describe(
                bt,
                f"{regime.upper()[:2]}{len(zones) + 1:03d}",
                regime,
                pixels,
                score,
                slope,
                aspect,
                elevation,
                forest,
                state_properties,
            )
            if zone is not None:
                zones.append(zone)
                accepted += 1
        summary["zone_count"] = accepted
        per_regime[regime] = summary

    if not zones:
        warnings.append(
            f"No terrain reached the release threshold of {threshold:g} in any regime under "
            "this storm window. This is NOT a statement that the mountain is safe."
        )

    explanation = {
        "threshold": threshold,
        "minimum_area_m2": MIN_ZONE_AREA_M2,
        "aspect_sector_deg": ASPECT_SECTOR_DEG,
        "elevation_band_m": ELEVATION_BAND_M,
        "maximum_zones_per_regime": maximum_zones_per_regime,
        "zone_count": len(zones),
        "per_regime": per_regime,
        "total_release_area_km2": round(
            sum(zone.properties["area_m2"] for zone in zones) / 1_000_000.0, 4
        ),
        "is_probability": False,
        "disclaimer": DISCLAIMER,
        "limitations": [
            "A release zone is terrain the model considers capable of releasing under the "
            "given storm-window forcing. It is NOT a prediction that an avalanche will "
            "occur there.",
            "The threshold is the configured production value, not a calibrated one.",
            "Zones from different regimes are never merged; overlapping ground can carry "
            "more than one mechanism.",
        ],
    }
    return ReleaseZoneSet(zones=zones, warnings=warnings, explanation=explanation)
