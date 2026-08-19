"""Repaired, explicitly configured release engine (v2).

Why this is a new module rather than an edit
--------------------------------------------
``regime-hindcast-v1.json`` and ``spot-blind-swiss-v1.json`` bind the SHA-256 of
:mod:`avycore.snowpack.state`, :mod:`avycore.snowpack.regimes`,
:mod:`avycore.snowpack.zones`, :mod:`avycore.hazard.risk` and both parameter
manifests. Editing any of those bytes makes two already-published negative
results unreplayable, and rewriting the frozen digests to match is out of
bounds. So v1 stays exactly as frozen -- it is the record of the defect -- and
the repair lands here, in a module whose own manifest hash is what a v2
experiment freezes.

What is repaired
----------------
1. **The wind statistic.** :func:`storm_window_wind_statistic` replaces the
   ``mean(all hours, all points)`` scalar that made the transport term
   identically zero in every frozen block. Three transport-preserving
   alternatives are offered and the accumulating one is the default, following
   the plan's instruction to reuse the CERRA drift-potential index rather than
   invent a fourth statistic. All of them remain **proxies**: a 5.5 km CERRA or
   25 km ERA5 cell cannot resolve ridgetop wind, and none of them is transported
   mass.
2. **The threshold.** ``release_threshold`` is no longer a bare 55.0 inherited
   from nowhere. :func:`derive_threshold` picks it from an achievable-score
   distribution at a declared flagged-terrain operating point, and the chosen
   value plus its derivation travel in :meth:`ReleaseConfigV2.manifest`.
3. **The morphology.** The effective minimum zone area is computed and reported
   rather than advertised as 2500 m^2 while a fixed 3x3 opening silently
   enforced 8100 m^2 at 30 m. The closing radius rounding is an explicit,
   recorded decision instead of ``max(1, round(15/30)) == 1``.
4. **The saturation bound.** ``loading_base`` still keeps a benign day quiet,
   but :func:`required_capability` makes the arithmetic that produced
   0 zones in three of four frozen blocks checkable, and the guardrails in
   :func:`guardrail_report` reject any configuration that buys capture by
   letting a no-snow, no-wind day light up or by treating missing data as a
   neutral value.

Everything here is an UNCALIBRATED relative index on a 0-100 scale. None of it
is a probability, a forecast, or a danger rating, and none of it is fitted to an
observed avalanche outside the development blocks named in the experiment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

import numpy as np
from scipy import ndimage

from .regimes import (
    DRY_LOOSE,
    DRY_SLAB,
    FULL_DEPTH_GLIDE,
    INSOLATION_CLIP,
    LOOSE_ACTIVATION_NEW_SNOW_CM,
    LOOSE_FOREST_DAMPING_MAX,
    LOOSE_LOADING_BASE,
    LOOSE_MAX_TEMPERATURE_C,
    LOOSE_NEW_SNOW_FULL_CM,
    LOOSE_SLOPE_BREAKPOINTS_DEG,
    LOOSE_SLOPE_MAX_DEG,
    LOOSE_SLOPE_MIN_DEG,
    LOOSE_SLOPE_SCORES,
    REGIMES,
    WET_ACTIVATION_POSITIVE_DEGREE_HOURS,
    WET_ACTIVATION_RAIN_ON_SNOW_MM,
    WET_FOREST_DAMPING_MAX,
    WET_LOADING_BASE,
    WET_POSITIVE_DEGREE_HOURS_FULL,
    WET_RAIN_FULL_MM,
    WET_SLOPE_BREAKPOINTS_DEG,
    WET_SLOPE_MAX_DEG,
    WET_SLOPE_MIN_DEG,
    WET_SLOPE_SCORES,
    WET_SNOW,
)
from .state import (
    DRIFT_AVAILABLE_MIN_NEW_SNOW_CM,
    DRIFT_INDEX_FULL_M3_S3_H,
    DRIFT_SETTLEMENT_TIME_H,
    DRIFT_THRESHOLD_DRY_MS,
    DRIFT_THRESHOLD_WET_MS,
    NEW_SNOW_DENSITY_KG_M3,
    NEW_SNOW_SETTLEMENT_TIME_H,
    RAIN_SNOW_LOWER_C,
    RAIN_SNOW_UPPER_C,
    SNOW_PRESENT_DEPTH_M,
    TEMPERATURE_LAPSE_C_PER_KM,
    WEAKNESS_BURIAL_MIN_NEW_SNOW_CM,
    WEAKNESS_FULL_HOURS,
    WEAKNESS_MAX_PRECIPITATION_MM,
    WEAKNESS_MAX_TEMPERATURE_C,
    WEAKNESS_MAX_WIND_KMH,
)
from .zones import (
    DRY_SLAB_MAX_FOREST_FRACTION,
    LOOSE_MAX_FOREST_FRACTION,
    WET_MAX_FOREST_FRACTION,
)

__all__ = [
    "DRIFT_KERNELS",
    "OPENING_STRUCTURES",
    "RADIUS_ROUNDINGS",
    "ReleaseConfigV2",
    "V1_FROZEN",
    "V2_BASELINE",
    "SnowStateV2",
    "derive_threshold",
    "effective_minimum_zone_area_m2",
    "guardrail_report",
    "integrate_state",
    "regime_scores",
    "release_mask",
    "required_capability",
    "storm_window_wind_statistic",
]


# =============================================================================
# Defect 1 -- the scalar wind statistic
# =============================================================================

#: How an hour of wind is turned into a transport increment. Every kernel is
#: zero below its threshold, so none of them can manufacture transport out of a
#: calm hour, and all of them are proxies rather than transported mass.
DRIFT_KERNELS = ("cubic_excess", "linear_excess", "hours_above")


def storm_window_wind_statistic(
    wind_speed_kmh: np.ndarray,
    *,
    statistic: str,
    threshold_kmh: float = DRIFT_THRESHOLD_DRY_MS * 3.6,
    quantile: float = 0.95,
) -> float:
    """Reduce an hourly, multi-point wind field to one transport-relevant number.

    This is the repair for the defect at ``run_spot_blind_hindcast.py:417``,
    which handed the model ``mean(72 hours x 9 points)``. A storm that blows
    45 km/h for six hours and 3 km/h for the rest reduces to about 6 km/h under
    that mean, which is below ``WIND_TRANSPORT_MIN_KMH`` and therefore erases
    the largest weight in the model. Averaging is the wrong operator for a
    threshold-and-power process; every statistic offered here preserves the
    windy hours instead of diluting them.

    ``wind_speed_kmh`` is an ``(points, hours)`` matrix, or any array whose last
    axis is time. The reduction is over every element, because the nine sample
    points describe one block.

    None of these is a measurement of ridgetop wind. A 25 km ERA5 or 5.5 km
    CERRA cell does not resolve the terrain that accelerates it.
    """
    speeds = np.asarray(wind_speed_kmh, dtype="float64").reshape(-1)
    if speeds.size == 0:
        raise ValueError("wind_speed_kmh contains no hours.")
    if not np.isfinite(speeds).all():
        raise ValueError("Wind speed contains a missing hour; it is never gap-filled.")
    if statistic == "arithmetic_mean":
        # The frozen v1 behaviour, kept only so a test can pin the defect.
        return float(speeds.mean())

    # Every alternative below is a mean *of observed speeds*, never a threshold
    # plus an offset. That matters: a statistic with a floor at the transport
    # threshold would report a transporting wind for a dead-calm storm, which
    # is the mirror image of the defect being repaired. When no hour transports,
    # each of them falls back to the plain mean and the loading term stays zero.
    excess = np.maximum(speeds - threshold_kmh, 0.0)
    if statistic == "quantile":
        return float(np.quantile(speeds, quantile))
    if statistic == "transporting_hours_mean":
        transporting = speeds[excess > 0.0]
        return float(transporting.mean()) if transporting.size else float(speeds.mean())
    if statistic == "drift_weighted_mean":
        # Weighted by the cube of the excess, the same power dependence the
        # CERRA drift-potential index accumulates (Pomeroy & Gray 1990).
        weight = excess**3
        total = float(weight.sum())
        return float((speeds * weight).sum() / total) if total > 0.0 else float(speeds.mean())
    raise ValueError(f"Unknown wind statistic {statistic!r}.")


def required_capability(
    new_snow_cm: float,
    *,
    transport: float = 0.0,
    config: "ReleaseConfigV2 | None" = None,
) -> float:
    """Terrain capability a cell must reach to clear the release threshold.

    Defect 2 made concrete: with ``transport == 0`` the loading term is fixed by
    new snow alone, so the required capability is a pure function of the storm.
    Values above 1.0 are unreachable -- ``capability`` is a product of factors
    each bounded by 1 -- which is why three frozen blocks produced no zone at
    all regardless of their terrain.
    """
    settings = config or V1_FROZEN
    snow = min(new_snow_cm / settings.new_snow_full_cm, 1.0)
    loading = min(
        settings.snow_loading_weight * snow + settings.wind_loading_weight * transport,
        1.0,
    )
    combined = settings.loading_base + (1.0 - settings.loading_base) * loading
    if combined <= 0.0:
        return math.inf
    return settings.release_threshold / (100.0 * combined)


# =============================================================================
# Defect 3 -- the morphology
# =============================================================================

#: The structuring element the opening uses. ``square3`` is v1's fixed 3x3,
#: which silently enforces a 9-cell solid block; ``cross`` removes single-cell
#: spurs without demanding a solid square; ``none`` skips the opening and lets
#: the declared minimum area do the work it was advertised to do.
OPENING_STRUCTURES = ("square3", "cross", "none")

#: How ``smoothing_radius_m`` becomes a pixel radius. v1's ``max(1, round(r/res))``
#: turns 15 m at 30 m resolution into 1 px -- a 3x3 closing -- by accident.
RADIUS_ROUNDINGS = ("v1_max1_round", "ceil", "nearest_at_least_one")


def _structure(name: str) -> np.ndarray | None:
    if name == "square3":
        return np.ones((3, 3), dtype=bool)
    if name == "cross":
        return np.asarray(
            [[False, True, False], [True, True, True], [False, True, False]]
        )
    if name == "none":
        return None
    raise ValueError(f"Unknown opening structure {name!r}.")


def effective_minimum_zone_area_m2(
    *, opening_structure: str, minimum_zone_area_m2: float, resolution_m: float
) -> float:
    """The minimum area a zone must actually have, not the advertised one.

    A binary opening by a structuring element deletes every region that cannot
    contain that element, so the true floor is the larger of the declared area
    and the element's own footprint. v1 advertised 2500 m^2 and enforced
    8100 m^2 at 30 m; reporting this number is the honest half of the fix.
    """
    cell_area = resolution_m**2
    declared_px = max(1, int(round(minimum_zone_area_m2 / cell_area)))
    structure = _structure(opening_structure)
    structure_px = 0 if structure is None else int(np.count_nonzero(structure))
    return float(max(declared_px, structure_px) * cell_area)


# =============================================================================
# The configuration
# =============================================================================


@dataclass(frozen=True)
class ReleaseConfigV2:
    """Every tunable a release configuration search is allowed to move.

    Defaults reproduce the frozen v1 engine exactly, so :data:`V1_FROZEN` is a
    reference point rather than a separate code path.
    """

    config_id: str = "v1_frozen"

    # -- snow state / wind statistic (defect 1) -----------------------------
    drift_kernel: str = "cubic_excess"
    drift_threshold_dry_ms: float = DRIFT_THRESHOLD_DRY_MS
    drift_threshold_wet_ms: float = DRIFT_THRESHOLD_WET_MS
    drift_index_full: float = DRIFT_INDEX_FULL_M3_S3_H
    drift_settlement_time_h: float = DRIFT_SETTLEMENT_TIME_H
    new_snow_settlement_time_h: float = NEW_SNOW_SETTLEMENT_TIME_H
    drift_available_min_new_snow_cm: float = DRIFT_AVAILABLE_MIN_NEW_SNOW_CM

    # -- dry-slab loading and terrain response (defect 2) -------------------
    release_threshold: float = 55.0
    loading_base: float = 0.20
    snow_loading_weight: float = 0.60
    wind_loading_weight: float = 0.75
    new_snow_full_cm: float = 50.0
    slope_breakpoints_deg: tuple[float, ...] = (0, 20, 25, 30, 34, 40, 45, 50, 55, 65, 90)
    slope_scores: tuple[float, ...] = (0, 0, 15, 55, 85, 100, 95, 75, 45, 15, 0)
    slope_min_deg: float = 25.0
    slope_max_deg: float = 60.0
    forest_damping_max: float = 0.7
    convexity_weight: float = 0.15
    dry_slab_activation_new_snow_cm: float = 5.0
    dry_slab_max_mean_temperature_c: float = 1.0

    # -- morphology (defect 3) ----------------------------------------------
    minimum_zone_area_m2: float = 2500.0
    smoothing_radius_m: float = 15.0
    radius_rounding: str = "v1_max1_round"
    opening_structure: str = "square3"
    maximum_zones_per_regime: int = 40

    #: Threshold provenance. ``"inherited_v1"`` is the uncalibrated 55.0; a
    #: derived threshold records the operating point it came from.
    threshold_derivation: str = "inherited_v1_uncalibrated"

    def closing_radius_px(self, resolution_m: float) -> int:
        ratio = self.smoothing_radius_m / resolution_m
        if self.radius_rounding == "v1_max1_round":
            return max(1, int(round(ratio)))
        if self.radius_rounding == "ceil":
            return max(1, int(math.ceil(ratio)))
        if self.radius_rounding == "nearest_at_least_one":
            return max(1, int(round(ratio + 0.5)))
        raise ValueError(f"Unknown radius rounding {self.radius_rounding!r}.")

    def manifest(self, *, resolution_m: float = 30.0) -> dict[str, Any]:
        """Everything that influences a score, including the honest minimum area."""
        return {
            "schema": "avycore-release-v2-parameters-v1",
            "config_id": self.config_id,
            "wind": {
                "drift_kernel": self.drift_kernel,
                "drift_threshold_dry_ms": self.drift_threshold_dry_ms,
                "drift_threshold_wet_ms": self.drift_threshold_wet_ms,
                "drift_index_full": self.drift_index_full,
                "drift_settlement_time_h": self.drift_settlement_time_h,
                "drift_available_min_new_snow_cm": self.drift_available_min_new_snow_cm,
                "statistic_is_a_transport_proxy": True,
                "resolves_ridgetop_wind": False,
            },
            "dry_slab": {
                "release_threshold": self.release_threshold,
                "threshold_derivation": self.threshold_derivation,
                "loading_base": self.loading_base,
                "snow_loading_weight": self.snow_loading_weight,
                "wind_loading_weight": self.wind_loading_weight,
                "new_snow_full_cm": self.new_snow_full_cm,
                "new_snow_settlement_time_h": self.new_snow_settlement_time_h,
                "slope_breakpoints_deg": list(self.slope_breakpoints_deg),
                "slope_scores": list(self.slope_scores),
                "slope_min_deg": self.slope_min_deg,
                "slope_max_deg": self.slope_max_deg,
                "forest_damping_max": self.forest_damping_max,
                "convexity_weight": self.convexity_weight,
                "activation_new_snow_cm": self.dry_slab_activation_new_snow_cm,
                "max_mean_temperature_c": self.dry_slab_max_mean_temperature_c,
            },
            "morphology": {
                "declared_minimum_zone_area_m2": self.minimum_zone_area_m2,
                "effective_minimum_zone_area_m2": effective_minimum_zone_area_m2(
                    opening_structure=self.opening_structure,
                    minimum_zone_area_m2=self.minimum_zone_area_m2,
                    resolution_m=resolution_m,
                ),
                "opening_structure": self.opening_structure,
                "smoothing_radius_m": self.smoothing_radius_m,
                "radius_rounding": self.radius_rounding,
                "closing_radius_px": self.closing_radius_px(resolution_m),
                "resolution_m": resolution_m,
                "maximum_zones_per_regime": self.maximum_zones_per_regime,
            },
            "unchanged_from_v1": {
                "wet_snow": "activation, slope response and weights are v1",
                "dry_loose": "activation, slope response and weights are v1",
                "full_depth_glide": "unsupported, exactly as v1",
                "weak_interface_proxy": "diagnostic only, zero numerical effect",
            },
        }


#: The frozen v1 engine, expressed in v2's vocabulary. Used by the equivalence
#: test that proves this module reproduces the published prediction masks.
V1_FROZEN = ReleaseConfigV2()

#: Starting point for the search: the same physics with the morphology told
#: truthfully. Dropping the opening is what makes the declared minimum area the
#: operative one -- 3 cells, 2700 m^2 at 30 m, against the 8100 m^2 a 3x3
#: opening enforced while the manifest advertised 2500. ``cross`` stays
#: available to the search but is not the honest default: a smaller structuring
#: element erodes less *and* dilates less, so it is not simply more permissive,
#: it trades the corners of compact zones for thin ones. Nothing here is tuned.
V2_BASELINE = replace(
    V1_FROZEN,
    config_id="v2_baseline",
    opening_structure="none",
    radius_rounding="ceil",
)


# =============================================================================
# Snow state
# =============================================================================


@dataclass(frozen=True)
class SnowStateV2:
    """The state fields the regime scores consume, at each cell's peak hour."""

    new_snow_index_cm: np.ndarray
    drift_index_normalized: np.ndarray
    drift_from_direction_deg: np.ndarray
    rain_on_snow_mm: np.ndarray
    positive_degree_hours: np.ndarray
    antecedent_snow_depth_m: np.ndarray
    peak_temperature_c: np.ndarray
    mean_storm_temperature_c: np.ndarray
    buried_weak_interface_proxy: np.ndarray
    mask: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)


def _snow_fraction(temperature_c: np.ndarray) -> np.ndarray:
    span = RAIN_SNOW_UPPER_C - RAIN_SNOW_LOWER_C
    return np.clip((RAIN_SNOW_UPPER_C - temperature_c) / span, 0.0, 1.0)


def integrate_state(
    *,
    times_utc: Sequence[str],
    storm_start_exclusive_utc: str,
    air_temperature_c: np.ndarray,
    precipitation_mm: np.ndarray,
    wind_speed_10m_kmh: np.ndarray,
    wind_from_direction_deg: np.ndarray,
    snow_depth_m: np.ndarray | None,
    sample_elevation_m: np.ndarray,
    sample_index: np.ndarray,
    elevation_m: np.ndarray,
    supported: np.ndarray,
    config: ReleaseConfigV2,
) -> SnowStateV2:
    """Hourly forward integration with a pluggable drift kernel.

    Structurally identical to :func:`avycore.snowpack.state.integrate_snow_state`
    -- same decay, same co-temporal peak-loading snapshot, same refusal to
    gap-fill -- and reproduces it exactly when ``config.drift_kernel`` is
    ``"cubic_excess"`` at the v1 thresholds. Only the hourly transport increment
    is configurable, which is the one thing defect 1 is about.

    The forcing matrices are ``(sample, hour)``. ``sample_index`` maps each cell
    to its nearest sample and carries the source grid's piecewise-constant
    footprint; nothing here interpolates.
    """
    if config.drift_kernel not in DRIFT_KERNELS:
        raise ValueError(f"Unknown drift kernel {config.drift_kernel!r}.")
    shape = np.shape(elevation_m)
    index = np.asarray(sample_index, dtype=np.intp)
    height = np.asarray(elevation_m, dtype="float64")
    mask = ~np.asarray(supported, dtype=bool)

    lapse_offset_c = (
        (height - np.asarray(sample_elevation_m, dtype="float64")[index]) / 1000.0
    ) * TEMPERATURE_LAPSE_C_PER_KM

    zeros = np.zeros(shape, dtype="float64")
    new_snow_index = zeros.copy()
    drift_index = zeros.copy()
    drift_east = zeros.copy()
    drift_north = zeros.copy()
    rain_on_snow = zeros.copy()
    positive_degree_hours = zeros.copy()
    weakness_hours = zeros.copy()
    temperature_sum = zeros.copy()
    independent_peak_new_snow = zeros.copy()
    antecedent_depth = zeros.copy()

    best_combined = np.full(shape, -1.0, dtype="float64")
    snapshot_new_snow = zeros.copy()
    snapshot_drift = zeros.copy()
    snapshot_east = zeros.copy()
    snapshot_north = zeros.copy()
    snapshot_temperature = zeros.copy()

    new_snow_decay = math.exp(-1.0 / config.new_snow_settlement_time_h)
    drift_decay = math.exp(-1.0 / config.drift_settlement_time_h)
    depth_to_cm = 100.0 / NEW_SNOW_DENSITY_KG_M3
    storm_hour_count = 0

    for hour, timestamp in enumerate(times_utc):
        temperature = air_temperature_c[index, hour] - lapse_offset_c
        precipitation = precipitation_mm[index, hour]
        wind_kmh = wind_speed_10m_kmh[index, hour]
        wind_ms = wind_kmh / 3.6
        depth = None if snow_depth_m is None else snow_depth_m[index, hour]

        if timestamp <= storm_start_exclusive_utc:
            weakness_hours += (
                (temperature <= WEAKNESS_MAX_TEMPERATURE_C)
                & (wind_kmh <= WEAKNESS_MAX_WIND_KMH)
                & (precipitation <= WEAKNESS_MAX_PRECIPITATION_MM)
            )
            if depth is not None:
                antecedent_depth = depth.astype("float64", copy=True)
            continue

        storm_hour_count += 1
        fraction = _snow_fraction(temperature)
        new_snow_cm = precipitation * fraction * depth_to_cm
        rain_mm = precipitation * (1.0 - fraction)
        new_snow_index = new_snow_index * new_snow_decay + new_snow_cm

        if depth is not None:
            snow_present = depth >= SNOW_PRESENT_DEPTH_M
        else:
            snow_present = new_snow_index >= (SNOW_PRESENT_DEPTH_M * 100.0)
        rain_on_snow += rain_mm * snow_present
        positive_degree_hours += np.maximum(temperature, 0.0)
        temperature_sum += temperature

        threshold_ms = np.where(
            temperature > 0.0,
            config.drift_threshold_wet_ms,
            config.drift_threshold_dry_ms,
        )
        transportable = (
            new_snow_index >= config.drift_available_min_new_snow_cm
        ) & (temperature <= 0.0)
        excess = np.maximum(wind_ms - threshold_ms, 0.0)
        if config.drift_kernel == "cubic_excess":
            increment = excess * excess * excess
        elif config.drift_kernel == "linear_excess":
            increment = excess
        else:  # hours_above -- counts transporting hours, ignoring how hard
            increment = (excess > 0.0).astype("float64")
        drift_hourly = increment * transportable

        drift_index = drift_index * drift_decay + drift_hourly
        bearing = np.deg2rad(wind_from_direction_deg[index, hour])
        drift_east = drift_east * drift_decay + drift_hourly * np.sin(bearing)
        drift_north = drift_north * drift_decay + drift_hourly * np.cos(bearing)

        independent_peak_new_snow = np.maximum(independent_peak_new_snow, new_snow_index)
        combined = np.minimum(new_snow_index / 50.0, 1.0) + np.minimum(
            drift_index / config.drift_index_full, 1.0
        )
        improved = combined > best_combined
        best_combined = np.where(improved, combined, best_combined)
        snapshot_new_snow = np.where(improved, new_snow_index, snapshot_new_snow)
        snapshot_drift = np.where(improved, drift_index, snapshot_drift)
        snapshot_east = np.where(improved, drift_east, snapshot_east)
        snapshot_north = np.where(improved, drift_north, snapshot_north)
        snapshot_temperature = np.where(improved, temperature, snapshot_temperature)

    if storm_hour_count == 0:
        raise ValueError("The storm window contained no forcing hours.")

    magnitude = np.hypot(snapshot_east, snapshot_north)
    drift_direction = np.where(
        magnitude > 0.0,
        (np.degrees(np.arctan2(snapshot_east, snapshot_north)) + 360.0) % 360.0,
        -1.0,
    )
    weak_proxy = np.clip(weakness_hours / WEAKNESS_FULL_HOURS, 0.0, 1.0) * (
        independent_peak_new_snow >= WEAKNESS_BURIAL_MIN_NEW_SNOW_CM
    )

    def finish(values: np.ndarray) -> np.ndarray:
        return np.where(mask, 0.0, np.asarray(values, dtype="float32")).astype("float32")

    return SnowStateV2(
        new_snow_index_cm=finish(snapshot_new_snow),
        drift_index_normalized=finish(
            np.clip(snapshot_drift / config.drift_index_full, 0.0, 1.0)
        ),
        drift_from_direction_deg=finish(drift_direction),
        rain_on_snow_mm=finish(rain_on_snow),
        positive_degree_hours=finish(positive_degree_hours),
        antecedent_snow_depth_m=finish(antecedent_depth),
        peak_temperature_c=finish(snapshot_temperature),
        mean_storm_temperature_c=finish(temperature_sum / float(storm_hour_count)),
        buried_weak_interface_proxy=finish(weak_proxy),
        mask=mask,
        metadata={
            "hour_count_storm": storm_hour_count,
            "hour_count_total": len(times_utc),
            "drift_kernel": config.drift_kernel,
            "phase_classification_applications": 1,
            "wind_direction_convention": "meteorological_from_degrees_clockwise_from_north",
        },
    )


# =============================================================================
# Regime scores
# =============================================================================


def _piecewise(values: np.ndarray, breakpoints: Sequence[float], scores: Sequence[float]) -> np.ndarray:
    return np.interp(values, breakpoints, scores).astype("float64")


def _relative_scale(values: np.ndarray, valid: np.ndarray) -> float:
    selected = np.abs(values[valid & np.isfinite(values)])
    scale = float(np.percentile(selected, 95)) if selected.size else 1.0
    return scale or 1.0


def regime_scores(
    *,
    slope: np.ndarray,
    aspect: np.ndarray,
    general_curvature: np.ndarray,
    plan_curvature: np.ndarray,
    forest: np.ndarray,
    terrain_mask: np.ndarray,
    state: SnowStateV2,
    insolation: np.ndarray | None,
    config: ReleaseConfigV2,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], np.ndarray]:
    """Score every regime and return ``(scores, active_masks, missing_mask)``.

    Wet-snow and dry-loose keep their v1 formulations verbatim: the plan's
    defects are all in the dry-slab pathway and its extraction, and moving three
    mechanisms at once would make any capture change uninterpretable.
    """
    mask = np.asarray(terrain_mask, dtype=bool) | np.asarray(state.mask, dtype=bool)
    valid = ~mask
    insol = (
        np.ones(slope.shape, dtype="float64")
        if insolation is None
        else np.clip(np.asarray(insolation, dtype="float64"), *INSOLATION_CLIP)
    )
    convexity = np.clip(
        general_curvature / _relative_scale(general_curvature, valid), 0.0, 1.0
    )
    convergence = np.clip(
        -plan_curvature / _relative_scale(plan_curvature, valid), 0.0, 1.0
    )
    forest = np.clip(forest, 0.0, 1.0)

    # Every state field is widened once, here, so the arithmetic below runs at
    # the same precision the v1 regime scorer used.
    new_snow = state.new_snow_index_cm.astype("float64")
    drift = state.drift_index_normalized.astype("float64")
    drift_from = state.drift_from_direction_deg.astype("float64")
    rain_on_snow = state.rain_on_snow_mm.astype("float64")
    positive_degree_hours = state.positive_degree_hours.astype("float64")
    antecedent_depth = state.antecedent_snow_depth_m.astype("float64")
    peak_temperature = state.peak_temperature_c.astype("float64")
    mean_temperature = state.mean_storm_temperature_c.astype("float64")

    scores: dict[str, np.ndarray] = {}
    active: dict[str, np.ndarray] = {}

    # --- dry slab -----------------------------------------------------------
    lee_direction = np.where(drift_from < 0.0, 0.0, (drift_from + 180.0) % 360.0)
    delta = np.deg2rad(np.abs((aspect - lee_direction + 180.0) % 360.0 - 180.0))
    lee_factor = np.where(aspect < 0.0, 0.0, 0.5 * (1.0 + np.cos(delta)))
    lee_factor = np.where(drift_from < 0.0, 0.0, lee_factor)
    drift_load = drift * lee_factor * (0.7 + 0.3 * convergence)
    snow_load = np.clip(new_snow / config.new_snow_full_cm, 0.0, 1.0)
    loading = np.clip(
        config.snow_loading_weight * snow_load + config.wind_loading_weight * drift_load,
        0.0,
        1.0,
    )
    capability = (
        _piecewise(slope, config.slope_breakpoints_deg, config.slope_scores)
        / 100.0
        * (1.0 - config.forest_damping_max * forest)
        * (1.0 - config.convexity_weight + config.convexity_weight * convexity)
    )
    slab = 100.0 * capability * (
        config.loading_base + (1.0 - config.loading_base) * loading
    )
    slab = np.where(
        (slope < config.slope_min_deg) | (slope > config.slope_max_deg), slab * 0.1, slab
    )
    scores[DRY_SLAB] = np.clip(slab, 0.0, 100.0)
    active[DRY_SLAB] = (
        valid
        & (new_snow >= config.dry_slab_activation_new_snow_cm)
        & (mean_temperature <= config.dry_slab_max_mean_temperature_c)
    )

    # --- wet snow (v1, unchanged) -------------------------------------------
    melt = np.clip(
        positive_degree_hours * insol / WET_POSITIVE_DEGREE_HOURS_FULL, 0.0, 1.0
    )
    rain = np.clip(rain_on_snow / WET_RAIN_FULL_MM, 0.0, 1.0)
    wetting = np.clip(rain + melt, 0.0, 1.0)
    wet = 100.0 * (
        _piecewise(slope, WET_SLOPE_BREAKPOINTS_DEG, WET_SLOPE_SCORES)
        / 100.0
        * (1.0 - WET_FOREST_DAMPING_MAX * forest)
    ) * (WET_LOADING_BASE + (1.0 - WET_LOADING_BASE) * wetting)
    wet = np.where(
        (slope < WET_SLOPE_MIN_DEG) | (slope > WET_SLOPE_MAX_DEG), wet * 0.1, wet
    )
    scores[WET_SNOW] = np.clip(wet, 0.0, 100.0)
    active[WET_SNOW] = (
        valid
        & ((antecedent_depth > 0.0) | (new_snow >= 5.0))
        & (
            (rain_on_snow >= WET_ACTIVATION_RAIN_ON_SNOW_MM)
            | (positive_degree_hours >= WET_ACTIVATION_POSITIVE_DEGREE_HOURS)
        )
    )

    # --- dry loose (v1, unchanged) ------------------------------------------
    loose_loading = np.clip(new_snow / LOOSE_NEW_SNOW_FULL_CM, 0.0, 1.0) * (1.0 - drift)
    loose = 100.0 * (
        _piecewise(slope, LOOSE_SLOPE_BREAKPOINTS_DEG, LOOSE_SLOPE_SCORES)
        / 100.0
        * (1.0 - LOOSE_FOREST_DAMPING_MAX * forest)
    ) * (LOOSE_LOADING_BASE + (1.0 - LOOSE_LOADING_BASE) * loose_loading)
    loose = np.where(
        (slope < LOOSE_SLOPE_MIN_DEG) | (slope > LOOSE_SLOPE_MAX_DEG), loose * 0.1, loose
    )
    scores[DRY_LOOSE] = np.clip(loose, 0.0, 100.0)
    active[DRY_LOOSE] = (
        valid
        & (new_snow >= LOOSE_ACTIVATION_NEW_SNOW_CM)
        & (peak_temperature <= LOOSE_MAX_TEMPERATURE_C)
    )

    # --- full-depth glide: still refused ------------------------------------
    scores[FULL_DEPTH_GLIDE] = np.zeros(slope.shape, dtype="float64")
    active[FULL_DEPTH_GLIDE] = np.zeros(slope.shape, dtype=bool)

    return scores, active, mask


_EXTRACTION_FOREST_LIMIT = {
    DRY_SLAB: DRY_SLAB_MAX_FOREST_FRACTION,
    WET_SNOW: WET_MAX_FOREST_FRACTION,
    DRY_LOOSE: LOOSE_MAX_FOREST_FRACTION,
    FULL_DEPTH_GLIDE: 0.3,
}
_EXTRACTION_SLOPE_WINDOW = {
    WET_SNOW: (WET_SLOPE_MIN_DEG, WET_SLOPE_MAX_DEG),
    DRY_LOOSE: (LOOSE_SLOPE_MIN_DEG, LOOSE_SLOPE_MAX_DEG),
    FULL_DEPTH_GLIDE: (26.0, 52.0),
}


def release_mask(
    *,
    scores: dict[str, np.ndarray],
    active: dict[str, np.ndarray],
    missing: np.ndarray,
    slope: np.ndarray,
    aspect: np.ndarray,
    elevation: np.ndarray,
    forest: np.ndarray,
    resolution_m: float,
    config: ReleaseConfigV2,
    aspect_sector_deg: float = 45.0,
    elevation_band_m: float = 300.0,
    pad_border: bool = True,
) -> tuple[np.ndarray, dict[str, int]]:
    """Threshold, smooth, segment and size-filter into a release footprint.

    Returns the union of accepted zone pixels and the per-regime zone count. No
    polygon is built: the search scores rasters, and geometry only matters when
    a zone is handed to a runout engine.
    """
    cell_area = resolution_m**2
    min_pixels = max(1, int(round(config.minimum_zone_area_m2 / cell_area)))
    radius = config.closing_radius_px(resolution_m)
    closing_structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    opening_structure = _structure(config.opening_structure)

    sectors = max(1, int(round(360.0 / aspect_sector_deg)))
    width = 360.0 / sectors
    binned = np.floor(((aspect + width / 2.0) % 360.0) / width).astype(np.int64)
    keys = np.where(aspect >= 0, binned, -1).astype(np.int64)
    keys = keys * 100_000 + np.floor(
        np.nan_to_num(elevation, nan=0.0) / elevation_band_m
    ).astype(np.int64)

    union = np.zeros(slope.shape, dtype=bool)
    counts: dict[str, int] = {}
    for regime in REGIMES:
        low, high = _EXTRACTION_SLOPE_WINDOW.get(
            regime, (config.slope_min_deg, config.slope_max_deg)
        )
        admissible = (
            active[regime]
            & ~missing
            & (slope >= low)
            & (slope <= high)
            & (forest < _EXTRACTION_FOREST_LIMIT[regime])
        )
        candidate = admissible & (scores[regime] >= config.release_threshold)
        counts[regime] = 0
        if not candidate.any():
            continue
        # Morphology on a padded copy when this raster is a core cropped from a
        # wider simulation grid. That halo can never be admissible, so a
        # core-edge cell's true neighbourhood is False -- but SciPy's erosion
        # assumes the same about the *array* edge and would shave the boundary
        # ring. Padding with False restores the halo's actual value, which makes
        # a cropped run identical to the whole-grid run it stands in for.
        # ``pad_border=False`` is for a raster that really is the whole domain.
        pad = (2 * radius + 2) if pad_border else 0
        working = (
            np.pad(candidate, pad, mode="constant", constant_values=False)
            if pad
            else candidate
        )
        working = ndimage.binary_closing(working, structure=closing_structure)
        if opening_structure is not None:
            working = ndimage.binary_opening(working, structure=opening_structure)
        smoothed = working[pad:-pad, pad:-pad] if pad else working
        # Closing may bridge across ground outside the regime window; re-apply
        # every admissibility condition so smoothing cannot invent release
        # terrain or cross a missing-data hole.
        smoothed &= admissible
        if not smoothed.any():
            continue

        labels = np.zeros(slope.shape, dtype=np.int32)
        count = 0
        for key in np.unique(keys[smoothed]):
            component, found = ndimage.label(
                smoothed & (keys == key), structure=np.ones((3, 3), dtype=int)
            )
            if found:
                hit = component > 0
                labels[hit] = component[hit] + count
                count += found
        if count == 0:
            continue
        sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, count + 1))
        accepted = 0
        for index in np.argsort(sizes)[::-1]:
            if sizes[index] < min_pixels:
                continue
            if accepted >= config.maximum_zones_per_regime:
                break
            union |= labels == (int(index) + 1)
            accepted += 1
        counts[regime] = accepted
    return union, counts


# =============================================================================
# Threshold derivation and guardrails
# =============================================================================


def derive_threshold(
    score_samples: Sequence[np.ndarray],
    admissible_samples: Sequence[np.ndarray],
    *,
    target_flagged_fraction: float,
) -> tuple[float, dict[str, Any]]:
    """Choose the threshold from a declared flagged-terrain operating point.

    ``score_samples`` are the per-cell combined regime scores over development
    terrain and ``admissible_samples`` the matching admissibility masks. The
    threshold is the quantile of the admissible score distribution that leaves
    ``target_flagged_fraction`` of that terrain above it -- an operating point
    chosen in advance, not a value read off a capture curve.

    This is a pre-morphology estimate: closing adds cells and opening plus the
    minimum-area filter removes them, so the realised flagged fraction differs.
    The derivation is recorded so the number is auditable rather than asserted.
    """
    pooled = np.concatenate(
        [
            np.asarray(score, dtype="float64")[np.asarray(admissible, dtype=bool)]
            for score, admissible in zip(score_samples, admissible_samples)
        ]
    )
    if pooled.size == 0:
        raise ValueError("No admissible terrain to derive a threshold from.")
    if not 0.0 < target_flagged_fraction < 1.0:
        raise ValueError("target_flagged_fraction must be in (0, 1).")
    threshold = float(np.quantile(pooled, 1.0 - target_flagged_fraction))
    return threshold, {
        "method": "quantile_of_admissible_development_score_distribution",
        "target_flagged_fraction_of_admissible_terrain": target_flagged_fraction,
        "admissible_cell_count": int(pooled.size),
        "score_maximum": float(pooled.max()),
        "score_median": float(np.median(pooled)),
        "threshold": threshold,
        "pre_morphology": True,
    }


def guardrail_report(
    *,
    benign_release_cell_count: int,
    benign_eligible_cell_count: int,
    flagged_outside_eligible_cell_count: int,
    flagged_on_missing_input_cell_count: int,
    benign_maximum_fraction: float = 1e-4,
) -> dict[str, Any]:
    """Physical checks a configuration must pass regardless of its score.

    * A benign day -- no new snow, no wind, no melt, no rain -- must still
      produce essentially no release terrain. A configuration that lights up a
      calm mountain has stopped being a loading model.
    * Missing data must never become a flagged cell. Unknown is unknown; it is
      not a low score and it is not a neutral value.
    """
    benign_fraction = (
        benign_release_cell_count / benign_eligible_cell_count
        if benign_eligible_cell_count
        else 0.0
    )
    checks = {
        "benign_day_quiet": benign_fraction <= benign_maximum_fraction,
        "no_flag_outside_eligible": flagged_outside_eligible_cell_count == 0,
        "no_flag_on_missing_input": flagged_on_missing_input_cell_count == 0,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "benign_release_fraction": benign_fraction,
        "benign_maximum_fraction": benign_maximum_fraction,
    }
