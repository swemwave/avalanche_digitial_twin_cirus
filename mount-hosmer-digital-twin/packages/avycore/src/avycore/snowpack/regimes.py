"""Separate deterministic release scores for distinct avalanche regimes.

The production release model represents exactly one mechanism: a dry storm slab
loaded by new snow and wind. That single score was asked to stand in for every
avalanche in a mapped campaign, including full-depth glide, wet-snow and dry
loose events, which it does not represent at all. This module keeps the dry-slab
score intact and adds three siblings, each with its own terrain response, its own
loading term, and its own **activation mask derived only from pre-event
forcing**.

The model chooses which regimes apply. A held-out outline's mapped avalanche
type is never an input; regime membership comes from the snow state and terrain
alone. Mapped type may only be used after predictions are frozen, to stratify
the evaluation.

Regimes
-------
``dry_slab``
    Unchanged terrain response and unchanged loading weights, imported directly
    from :mod:`avycore.hazard.risk` so there is a single definition. What changes
    is the *source* of the loading: a per-cell, co-temporal new-snow and
    drift-index state instead of one scalar for a whole mountain block, plus a
    bounded uplift from the buried-weak-interface proxy.

``wet_snow``
    Wet slab and wet loose. Activated only by liquid water reaching an existing
    snow cover -- rain on snow, or melt expressed as positive degree-hours
    weighted by terrain insolation. Its slope response peaks lower than the dry
    slab because wet snow fails at gentler angles (Baggi & Schweizer, 2009,
    *Nat. Hazards* 50, 97; Mitterer & Schweizer, 2013). Convexity is dropped: wet
    failures are not driven by slab tension over a convex roll.

``dry_loose``
    Point-release sluffs in cold, low-cohesion new snow. Its slope response peaks
    much steeper than a slab, and it is *suppressed* by drifting, because
    wind-worked snow forms cohesive slabs rather than loose sluffs.

``full_depth_glide``
    Full-depth release on smooth ground. Requires a deep antecedent snowpack, an
    indicator of basal wetting, and the absence of forest. Glide-avalanche timing
    is famously not predictable from these variables; this regime identifies
    terrain *susceptible* to full-depth release, and nothing more.

Everything here is an UNCALIBRATED relative index on a 0-100 scale. None of it is
a probability, a forecast, or a danger rating, and none of it is fitted to an
observed avalanche.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from ..hazard.protocols import Terrain
from ..hazard.risk import (
    FOREST_DAMPING_MAX,
    LOADING_BASE,
    NEW_SNOW_FULL_CM,
    RELEASE_THRESHOLD,
    SLOPE_BREAKPOINTS_DEG,
    SLOPE_MAX_DEG,
    SLOPE_MIN_DEG,
    SLOPE_SCORES,
    SNOW_LOADING_WEIGHT,
    WIND_LOADING_WEIGHT,
)
from .state import SnowState

__all__ = [
    "REGIMES",
    "RegimeScore",
    "RegimeReleaseField",
    "compute_regime_release",
    "regime_parameter_manifest",
]

DRY_SLAB = "dry_slab"
WET_SNOW = "wet_snow"
DRY_LOOSE = "dry_loose"
FULL_DEPTH_GLIDE = "full_depth_glide"
REGIMES: tuple[str, ...] = (DRY_SLAB, WET_SNOW, DRY_LOOSE, FULL_DEPTH_GLIDE)

# =============================================================================
# Regime constants. UNCALIBRATED literature-informed judgements, not fitted.
# =============================================================================

#: Minimum recent new snow before a dry slab is considered to exist at all.
DRY_SLAB_ACTIVATION_NEW_SNOW_CM = 5.0
#: Above this storm-mean temperature the storm is not building a dry slab.
DRY_SLAB_MAX_MEAN_TEMPERATURE_C = 1.0

#: Wet-snow slope response: peaks at 35-40 deg and retains substantial score at
#: 30 and 45 deg, lower than the dry-slab optimum.
WET_SLOPE_BREAKPOINTS_DEG = [0, 20, 25, 30, 35, 40, 45, 50, 60, 90]
WET_SLOPE_SCORES = [0, 0, 20, 70, 100, 100, 90, 70, 25, 0]
WET_SLOPE_MIN_DEG = 22.0
WET_SLOPE_MAX_DEG = 55.0
WET_FOREST_DAMPING_MAX = 0.6
WET_RAIN_FULL_MM = 20.0
WET_POSITIVE_DEGREE_HOURS_FULL = 48.0
WET_ACTIVATION_RAIN_ON_SNOW_MM = 5.0
WET_ACTIVATION_POSITIVE_DEGREE_HOURS = 12.0
WET_LOADING_BASE = 0.20

#: Dry-loose slope response: sluffs need steep ground, peaking near 50 deg.
LOOSE_SLOPE_BREAKPOINTS_DEG = [0, 30, 35, 40, 45, 50, 60, 70, 90]
LOOSE_SLOPE_SCORES = [0, 0, 20, 60, 95, 100, 80, 40, 0]
LOOSE_SLOPE_MIN_DEG = 33.0
LOOSE_SLOPE_MAX_DEG = 75.0
LOOSE_FOREST_DAMPING_MAX = 0.5
LOOSE_NEW_SNOW_FULL_CM = 25.0
LOOSE_ACTIVATION_NEW_SNOW_CM = 5.0
LOOSE_MAX_TEMPERATURE_C = -2.0
LOOSE_LOADING_BASE = 0.25

#: Full-depth/glide slope response: peaks on the sustained 35-42 deg planar
#: ground where glide cracks are observed, and falls away on very steep faces
#: where a full-depth pack does not persist.
GLIDE_SLOPE_BREAKPOINTS_DEG = [0, 25, 28, 35, 42, 50, 55, 90]
GLIDE_SLOPE_SCORES = [0, 0, 30, 100, 100, 60, 20, 0]
GLIDE_SLOPE_MIN_DEG = 26.0
GLIDE_SLOPE_MAX_DEG = 52.0
GLIDE_ACTIVATION_SNOW_DEPTH_M = 0.8
GLIDE_ACTIVATION_POSITIVE_DEGREE_HOURS = 24.0
GLIDE_ACTIVATION_RAIN_ON_SNOW_MM = 10.0
GLIDE_MAX_FOREST_FRACTION = 0.3
GLIDE_SNOW_DEPTH_FULL_M = 2.0
GLIDE_POSITIVE_DEGREE_HOURS_FULL = 72.0
GLIDE_RAIN_FULL_MM = 20.0
GLIDE_LOADING_BASE = 0.20

#: Terrain insolation is a bounded relative geometric weight; clip it so a very
#: steep sun-facing face cannot multiply the melt term without limit.
INSOLATION_CLIP = (0.0, 2.0)


def regime_parameter_manifest() -> dict[str, Any]:
    """Every tunable that influences regime release scoring."""

    return {
        "regimes": list(REGIMES),
        "release_threshold": RELEASE_THRESHOLD,
        "dry_slab": {
            "slope_breakpoints_deg": list(SLOPE_BREAKPOINTS_DEG),
            "slope_scores": list(SLOPE_SCORES),
            "slope_min_deg": SLOPE_MIN_DEG,
            "slope_max_deg": SLOPE_MAX_DEG,
            "forest_damping_max": FOREST_DAMPING_MAX,
            "new_snow_full_cm": NEW_SNOW_FULL_CM,
            "snow_loading_weight": SNOW_LOADING_WEIGHT,
            "wind_loading_weight": WIND_LOADING_WEIGHT,
            "loading_base": LOADING_BASE,
            "activation_new_snow_cm": DRY_SLAB_ACTIVATION_NEW_SNOW_CM,
            "max_mean_temperature_c": DRY_SLAB_MAX_MEAN_TEMPERATURE_C,
        },
        "wet_snow": {
            "slope_breakpoints_deg": list(WET_SLOPE_BREAKPOINTS_DEG),
            "slope_scores": list(WET_SLOPE_SCORES),
            "slope_min_deg": WET_SLOPE_MIN_DEG,
            "slope_max_deg": WET_SLOPE_MAX_DEG,
            "forest_damping_max": WET_FOREST_DAMPING_MAX,
            "rain_full_mm": WET_RAIN_FULL_MM,
            "positive_degree_hours_full": WET_POSITIVE_DEGREE_HOURS_FULL,
            "activation_rain_on_snow_mm": WET_ACTIVATION_RAIN_ON_SNOW_MM,
            "activation_positive_degree_hours": WET_ACTIVATION_POSITIVE_DEGREE_HOURS,
            "loading_base": WET_LOADING_BASE,
            "insolation_clip": list(INSOLATION_CLIP),
        },
        "dry_loose": {
            "slope_breakpoints_deg": list(LOOSE_SLOPE_BREAKPOINTS_DEG),
            "slope_scores": list(LOOSE_SLOPE_SCORES),
            "slope_min_deg": LOOSE_SLOPE_MIN_DEG,
            "slope_max_deg": LOOSE_SLOPE_MAX_DEG,
            "forest_damping_max": LOOSE_FOREST_DAMPING_MAX,
            "new_snow_full_cm": LOOSE_NEW_SNOW_FULL_CM,
            "activation_new_snow_cm": LOOSE_ACTIVATION_NEW_SNOW_CM,
            "max_temperature_c": LOOSE_MAX_TEMPERATURE_C,
            "loading_base": LOOSE_LOADING_BASE,
        },
        "full_depth_glide": {
            "implemented": False,
            "reason": (
                "The available forcing has no basal liquid-water observation and the "
                "terrain has no smooth-ground or glide-crack observation."
            ),
            "slope_breakpoints_deg": list(GLIDE_SLOPE_BREAKPOINTS_DEG),
            "slope_scores": list(GLIDE_SLOPE_SCORES),
            "slope_min_deg": GLIDE_SLOPE_MIN_DEG,
            "slope_max_deg": GLIDE_SLOPE_MAX_DEG,
            "activation_snow_depth_m": GLIDE_ACTIVATION_SNOW_DEPTH_M,
            "activation_positive_degree_hours": GLIDE_ACTIVATION_POSITIVE_DEGREE_HOURS,
            "activation_rain_on_snow_mm": GLIDE_ACTIVATION_RAIN_ON_SNOW_MM,
            "max_forest_fraction": GLIDE_MAX_FOREST_FRACTION,
            "snow_depth_full_m": GLIDE_SNOW_DEPTH_FULL_M,
            "positive_degree_hours_full": GLIDE_POSITIVE_DEGREE_HOURS_FULL,
            "rain_full_mm": GLIDE_RAIN_FULL_MM,
            "loading_base": GLIDE_LOADING_BASE,
        },
    }


@dataclass(frozen=True)
class RegimeScore:
    """One regime's release score and the mask of where it applies at all."""

    regime: str
    score: np.ndarray
    active: np.ndarray
    supported: bool
    unsupported_reason: str | None
    explanation: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegimeReleaseField:
    """The combined multi-regime release estimate.

    ``release`` is the per-cell maximum over the regimes that are *active* at
    that cell. A cell where no regime activates scores a computed zero, which is
    a model statement that no represented mechanism applies -- it is **not** a
    statement that the cell is safe, and it is distinct from ``release.mask``,
    which marks cells whose inputs are missing and whose score is unknown.
    """

    release: np.ma.MaskedArray
    dominant_regime: np.ndarray
    regime_scores: dict[str, RegimeScore]
    explanation: dict[str, Any]

    def regime_code(self, regime: str) -> int:
        return REGIMES.index(regime) + 1


def _piecewise(
    values: np.ndarray, breakpoints: Sequence[float], scores: Sequence[float]
) -> np.ndarray:
    """Score by explicit physical breakpoints. Never a percentile stretch."""

    return np.interp(values, breakpoints, scores).astype("float64")


def _relative_scale(values: np.ndarray, valid: np.ndarray) -> float:
    """95th percentile of |values| over valid cells, as the production model uses."""

    selected = np.abs(values[valid & np.isfinite(values)])
    scale = float(np.percentile(selected, 95)) if selected.size else 1.0
    return scale or 1.0


def compute_regime_release(
    bt: Terrain,
    state: SnowState,
    *,
    insolation_index: np.ndarray | None = None,
    supported: np.ndarray | None = None,
) -> RegimeReleaseField:
    """Score every regime, then combine the active ones.

    ``insolation_index`` is the dimensionless terrain-insolation weight from
    :func:`avycore.snowpack.solar.insolation_index`. When it is ``None`` the
    melt terms fall back to a flat weight of 1.0, which reproduces a
    no-radiation-information result exactly rather than assuming shade or sun.
    """

    slope_layer = bt.layer("slope")
    aspect_layer = bt.layer("aspect")
    general_layer = bt.layer("general_curvature")
    plan_layer = bt.layer("plan_curvature")
    forest_layer = bt.layer("forest_mask")

    slope = np.asarray(slope_layer.filled(0.0), dtype="float64")
    aspect = np.asarray(aspect_layer.filled(-1.0), dtype="float64")
    general_curvature = np.asarray(general_layer.filled(0.0), dtype="float64")
    plan_curvature = np.asarray(plan_layer.filled(0.0), dtype="float64")
    forest = np.clip(np.asarray(forest_layer.filled(0.0), dtype="float64"), 0.0, 1.0)

    # Every terrain term is required to interpret a cell, and so is the snow
    # state. A gap in any of them stays missing rather than becoming a neutral
    # numeric value that would produce a safe-looking score.
    mask = np.logical_or.reduce(
        [
            np.ma.getmaskarray(slope_layer),
            np.ma.getmaskarray(aspect_layer),
            np.ma.getmaskarray(general_layer),
            np.ma.getmaskarray(plan_layer),
            np.ma.getmaskarray(forest_layer),
            np.asarray(state.mask, dtype=bool),
        ]
    )
    if supported is not None:
        support = np.asarray(supported, dtype=bool)
        if support.shape != slope.shape:
            raise ValueError("supported must match the terrain raster shape.")
        mask |= ~support
    valid = ~mask

    if insolation_index is None:
        insolation = np.ones(slope.shape, dtype="float64")
        insolation_available = False
    else:
        insolation = np.clip(
            np.asarray(insolation_index, dtype="float64"), *INSOLATION_CLIP
        )
        if insolation.shape != slope.shape:
            raise ValueError("insolation_index must match the terrain raster shape.")
        insolation_available = True

    curvature_scale = _relative_scale(general_curvature, valid)
    plan_scale = _relative_scale(plan_curvature, valid)
    convexity = np.clip(general_curvature / curvature_scale, 0.0, 1.0)
    convergence = np.clip(-plan_curvature / plan_scale, 0.0, 1.0)

    new_snow = np.asarray(state.new_snow_index_cm, dtype="float64")
    drift = np.asarray(state.drift_index_normalized, dtype="float64")
    drift_from = np.asarray(state.drift_from_direction_deg, dtype="float64")
    weak_proxy = np.asarray(state.buried_weak_interface_proxy, dtype="float64")
    rain_on_snow = np.asarray(state.rain_on_snow_mm, dtype="float64")
    positive_degree_hours = np.asarray(state.positive_degree_hours, dtype="float64")
    antecedent_depth = np.asarray(state.antecedent_snow_depth_m, dtype="float64")
    peak_temperature = np.asarray(state.peak_temperature_c, dtype="float64")
    mean_temperature = np.asarray(state.mean_storm_temperature_c, dtype="float64")

    scores: dict[str, RegimeScore] = {}

    # --- dry slab -----------------------------------------------------------
    # Lee loading points away from where the drift came FROM. A cell with no
    # drift has direction -1 and a zero drift magnitude, so the factor is
    # irrelevant there; guarding the angle keeps the arithmetic finite anyway.
    lee_direction = np.where(drift_from < 0.0, 0.0, (drift_from + 180.0) % 360.0)
    delta = np.deg2rad(np.abs((aspect - lee_direction + 180.0) % 360.0 - 180.0))
    lee_factor = np.where(aspect < 0.0, 0.0, 0.5 * (1.0 + np.cos(delta)))
    lee_factor = np.where(drift_from < 0.0, 0.0, lee_factor)
    drift_load = drift * lee_factor * (0.7 + 0.3 * convergence)
    snow_load = np.clip(new_snow / NEW_SNOW_FULL_CM, 0.0, 1.0)
    base_loading = np.clip(
        SNOW_LOADING_WEIGHT * snow_load + WIND_LOADING_WEIGHT * drift_load, 0.0, 1.0
    )
    # The weather-derived buried-interface field is deliberately diagnostic.
    # There is no observed weak layer or calibrated mapping from the proxy to a
    # release-index increment, so assigning one would manufacture information.
    slab_loading = base_loading
    slab_capability = (
        _piecewise(slope, SLOPE_BREAKPOINTS_DEG, SLOPE_SCORES)
        / 100.0
        * (1.0 - FOREST_DAMPING_MAX * forest)
        * (0.85 + 0.15 * convexity)
    )
    slab_score = 100.0 * slab_capability * (LOADING_BASE + (1.0 - LOADING_BASE) * slab_loading)
    slab_score = np.where(
        (slope < SLOPE_MIN_DEG) | (slope > SLOPE_MAX_DEG), slab_score * 0.1, slab_score
    )
    slab_active = (
        valid
        & (new_snow >= DRY_SLAB_ACTIVATION_NEW_SNOW_CM)
        & (mean_temperature <= DRY_SLAB_MAX_MEAN_TEMPERATURE_C)
    )
    scores[DRY_SLAB] = RegimeScore(
        regime=DRY_SLAB,
        score=np.clip(slab_score, 0.0, 100.0),
        active=slab_active,
        supported=bool(slab_active.any()),
        unsupported_reason=(
            None
            if slab_active.any()
            else (
                "No cell reached the dry-slab activation state: recent new snow below "
                f"{DRY_SLAB_ACTIVATION_NEW_SNOW_CM:g} cm, or a storm-mean temperature above "
                f"{DRY_SLAB_MAX_MEAN_TEMPERATURE_C:g} degC."
            )
        ),
        explanation={
            "mechanism": "Dry storm slab loaded by new snow and wind-drifted snow.",
            "terrain_response": "Production 34-45 deg optimum, unchanged.",
            "loading_terms": ["new_snow_index", "drift_index"],
            "weak_interface_role": (
                "Diagnostic only. The reported proxy observes no weak layer and has no "
                "numerical effect on release because no defensible calibration is available."
            ),
            "buried_weak_interface_proxy_mean_active": (
                float(weak_proxy[slab_active].mean()) if slab_active.any() else None
            ),
        },
    )

    # --- wet snow -----------------------------------------------------------
    melt_term = np.clip(
        positive_degree_hours * insolation / WET_POSITIVE_DEGREE_HOURS_FULL, 0.0, 1.0
    )
    rain_term = np.clip(rain_on_snow / WET_RAIN_FULL_MM, 0.0, 1.0)
    wetting = np.clip(rain_term + melt_term, 0.0, 1.0)
    wet_capability = (
        _piecewise(slope, WET_SLOPE_BREAKPOINTS_DEG, WET_SLOPE_SCORES)
        / 100.0
        * (1.0 - WET_FOREST_DAMPING_MAX * forest)
    )
    wet_score = 100.0 * wet_capability * (
        WET_LOADING_BASE + (1.0 - WET_LOADING_BASE) * wetting
    )
    wet_score = np.where(
        (slope < WET_SLOPE_MIN_DEG) | (slope > WET_SLOPE_MAX_DEG),
        wet_score * 0.1,
        wet_score,
    )
    snow_cover_present = (antecedent_depth > 0.0) | (new_snow >= 5.0)
    wet_active = (
        valid
        & snow_cover_present
        & (
            (rain_on_snow >= WET_ACTIVATION_RAIN_ON_SNOW_MM)
            | (positive_degree_hours >= WET_ACTIVATION_POSITIVE_DEGREE_HOURS)
        )
    )
    scores[WET_SNOW] = RegimeScore(
        regime=WET_SNOW,
        score=np.clip(wet_score, 0.0, 100.0),
        active=wet_active,
        supported=bool(wet_active.any()),
        unsupported_reason=(
            None
            if wet_active.any()
            else (
                "No cell received enough liquid water: rain on snow below "
                f"{WET_ACTIVATION_RAIN_ON_SNOW_MM:g} mm and positive degree-hours below "
                f"{WET_ACTIVATION_POSITIVE_DEGREE_HOURS:g}. Wet-snow avalanches remain "
                "unrepresented for this window rather than being folded into the dry-slab score."
            )
        ),
        explanation={
            "mechanism": (
                "Surface-wetting susceptibility proxy for wet slab and wet loose release."
            ),
            "terrain_response": "Optimum shifted to 35-40 deg; convexity term dropped.",
            "loading_terms": ["rain_on_snow", "positive_degree_hours", "terrain_insolation"],
            "insolation_available": insolation_available,
        },
    )

    # --- dry loose ----------------------------------------------------------
    loose_loading = np.clip(new_snow / LOOSE_NEW_SNOW_FULL_CM, 0.0, 1.0) * (1.0 - drift)
    loose_capability = (
        _piecewise(slope, LOOSE_SLOPE_BREAKPOINTS_DEG, LOOSE_SLOPE_SCORES)
        / 100.0
        * (1.0 - LOOSE_FOREST_DAMPING_MAX * forest)
    )
    loose_score = 100.0 * loose_capability * (
        LOOSE_LOADING_BASE + (1.0 - LOOSE_LOADING_BASE) * loose_loading
    )
    loose_score = np.where(
        (slope < LOOSE_SLOPE_MIN_DEG) | (slope > LOOSE_SLOPE_MAX_DEG),
        loose_score * 0.1,
        loose_score,
    )
    loose_active = (
        valid
        & (new_snow >= LOOSE_ACTIVATION_NEW_SNOW_CM)
        & (peak_temperature <= LOOSE_MAX_TEMPERATURE_C)
    )
    scores[DRY_LOOSE] = RegimeScore(
        regime=DRY_LOOSE,
        score=np.clip(loose_score, 0.0, 100.0),
        active=loose_active,
        supported=bool(loose_active.any()),
        unsupported_reason=(
            None
            if loose_active.any()
            else (
                "No cell held cold, undrifted new snow: recent new snow below "
                f"{LOOSE_ACTIVATION_NEW_SNOW_CM:g} cm or temperature above "
                f"{LOOSE_MAX_TEMPERATURE_C:g} degC at the peak-loading hour."
            )
        ),
        explanation={
            "mechanism": "Cold dry point-release sluffs in low-cohesion new snow.",
            "terrain_response": "Optimum near 50 deg, far steeper than a slab.",
            "loading_terms": ["new_snow_index", "inverse drift_index"],
            "drift_role": "Drifting suppresses this regime: wind-worked snow forms slabs.",
        },
    )

    # --- full-depth glide ---------------------------------------------------
    # Surface temperature, rain and modelled snow depth cannot establish the
    # required liquid water at the snow/ground interface. Nor does this terrain
    # product contain smooth-ground or observed glide-crack information. Keep a
    # separate regime in the contract, but refuse to turn those proxies into a
    # full-depth/glide prediction.
    glide_score = np.zeros(slope.shape, dtype="float64")
    glide_active = np.zeros(slope.shape, dtype=bool)
    glide_reason = (
        "Full-depth/glide release is unsupported: the inputs contain no basal "
        "liquid-water observation, smooth-ground classification, or glide-crack "
        "observation. Surface air temperature, rain and snow depth are not silently "
        "substituted for those missing mechanism variables."
    )
    scores[FULL_DEPTH_GLIDE] = RegimeScore(
        regime=FULL_DEPTH_GLIDE,
        score=np.clip(glide_score, 0.0, 100.0),
        active=glide_active,
        supported=bool(glide_active.any()),
        unsupported_reason=glide_reason,
        explanation={
            "mechanism": "Full-depth release requires basal glide at the snow/ground interface.",
            "implemented": False,
            "loading_terms": [],
        },
    )

    combined = np.zeros(slope.shape, dtype="float64")
    dominant = np.zeros(slope.shape, dtype="uint8")
    for position, regime in enumerate(REGIMES, start=1):
        candidate = np.where(scores[regime].active, scores[regime].score, 0.0)
        improved = candidate > combined
        combined = np.where(improved, candidate, combined)
        dominant = np.where(improved, np.uint8(position), dominant)

    release = np.ma.array(
        np.clip(combined, 0.0, 100.0).astype("float32"), mask=mask, copy=False
    )
    explanation = {
        "model_type": "Multi-regime deterministic release estimate (0-100 relative index)",
        "is_probability": False,
        "is_operational_forecast": False,
        "regime_selection": (
            "Every regime's activation mask is derived from pre-event terrain and snow "
            "state only. No mapped avalanche type, size, outline, or other target "
            "attribute is an input to any regime."
        ),
        "combination_rule": "Per-cell maximum over active regimes.",
        "zero_semantics": (
            "A complete-input cell where no regime activates scores a computed zero: no "
            "represented mechanism applies. That is not a statement that the cell is safe, "
            "and it is distinct from a masked cell, whose score is unknown."
        ),
        "insolation_available": insolation_available,
        "regime_active_cell_counts": {
            regime: int(np.count_nonzero(scores[regime].active)) for regime in REGIMES
        },
        "regime_supported": {regime: scores[regime].supported for regime in REGIMES},
        "regime_unsupported_reason": {
            regime: scores[regime].unsupported_reason
            for regime in REGIMES
            if scores[regime].unsupported_reason is not None
        },
        "dominant_regime_cell_counts": {
            regime: int(np.count_nonzero(dominant == position))
            for position, regime in enumerate(REGIMES, start=1)
        },
        "parameters": regime_parameter_manifest(),
        "limitations": [
            "Every regime score is an uncalibrated relative index, never a probability.",
            "The buried-weak-interface diagnostic is built from pre-storm surface weather. "
            "It observes no weak layer, cannot be verified from these data, and has no "
            "numerical effect on release.",
            "The wet-snow score is a surface-wetting susceptibility proxy; it does not "
            "resolve liquid water within the snowpack or distinguish wet slab from wet loose.",
            "A regime reported unsupported is genuinely unrepresented for this window; its "
            "avalanches are not folded into another regime's score.",
            "Entrainment, deposition, mass balance, and powder-cloud behaviour are absent.",
        ],
    }
    return RegimeReleaseField(
        release=release,
        dominant_regime=np.where(mask, np.uint8(0), dominant).astype("uint8"),
        regime_scores=scores,
        explanation=explanation,
    )
