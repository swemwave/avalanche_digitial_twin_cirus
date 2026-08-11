"""AvyCore's composite area hazard index: release x reach, raised by exposure.

The release estimate in :mod:`avycore.hazard.risk` answers one question -- can a
slab start here? It is deliberately blind to everything downhill of the crown, so
a zone that stops on a bench 200 m below its start scores exactly like one that
runs 2 km to the valley floor and crosses a highway. That is the right shape for a
*release* model and the wrong shape for a hazard index, which is what a reader
actually looks at.

This module decomposes the per-zone index into three normalised 0-1 terms and
publishes every one of them beside the total:

``release``
    The zone's existing ``estimated_release_score / 100``. Computed by
    :mod:`avycore.hazard.risk` and **not touched here** -- exposure never reaches
    the release model, by contract.
``reach``
    Destructive potential taken from the zone's own runout result: how far it ran,
    how far it fell, and how fast it was moving.
``exposure``
    What the baked exposure layer maps under the runout footprint and its
    sensitivity envelope.

They combine so that exposure can only ever RAISE a zone's index::

    zone_index = 100 * release * (REACH_BASE + (1 - REACH_BASE) * reach)
    zone_index *= 1 + EXPOSURE_MAX_UPLIFT * exposure
    zone_index = clip(zone_index, 0, 100)

A zone with nothing mapped in its path keeps its terrain-and-reach index exactly.
That is the safety-critical property: an empty valley never reads as *safer* than
the model would otherwise say, only as less additionally consequential. It is also
why the exposure term is a multiplicative uplift rather than a weighted average --
an average would let a blank map pull a genuinely dangerous slope down.

Missing components are never zero. Runout is simulated only for the highest-scoring
zones, so most zones have no runout result at all; defaulting their reach and
exposure to 0 would convert missing data into a low-hazard number. Instead every
term carries an availability flag, an unsimulated zone is published as
``release_only`` under its own basis label, and the caller reports how many zones
contributed each component.

Every constant here is an UNCALIBRATED scaling choice, not a fitted parameter. The
result is a relative index from 0 to 100 -- never a probability, never a forecast,
never an avalanche danger rating.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

__all__ = [
    "EXPOSURE_MAX_UPLIFT",
    "REACH_BASE",
    "AreaHazard",
    "ExposureComponent",
    "ReachComponent",
    "ZoneHazard",
    "aggregate_area_hazard",
    "exposure_component",
    "exposure_unavailable",
    "parameter_manifest",
    "reach_component",
    "reach_unavailable",
    "zone_hazard_index",
]

# =============================================================================
# Tunable model constants -- UNCALIBRATED scaling choices, not fitted parameters.
# =============================================================================

#: Reach normalisation. A runout is scored against these reference magnitudes, not
#: against the distribution of this run -- so the same zone scores the same on any
#: day and between bakes. The references are the scale of a large Elk Valley path,
#: chosen to sit inside the fast engine's own 4000 m path cap rather than at it, so
#: an ordinary large avalanche is not compressed into the top few percent.
REACH_FULL_DISTANCE_M = 2000.0
#: Mount Hosmer holds roughly 1500 m of relief above the valley floor; 1000 m of
#: vertical drop is therefore a very large event here, not a theoretical maximum.
REACH_FULL_DROP_M = 1000.0
#: Matches the top velocity class the particle engine already reports (40 m/s).
REACH_FULL_VELOCITY_MS = 40.0

#: How the three reach measurements combine. Distance leads because it is what
#: decides whether an avalanche gets to anything at all; velocity is weighted least
#: because only the particle engine measures it (fast routing reports no velocity,
#: and a missing measurement is renormalised away rather than scored as zero).
REACH_DISTANCE_WEIGHT = 0.45
REACH_DROP_WEIGHT = 0.35
REACH_VELOCITY_WEIGHT = 0.20

#: What a zone keeps when its runout goes nowhere. Reach modulates the release
#: index; it does not replace it. A slab that releases and stops immediately is
#: still a slab that released, so the index floor stays high.
REACH_BASE = 0.55

#: Exposure normalisation. This much mapped exposure under the central footprint
#: counts as fully covered -- about 10 000 m2, the order of a trunk-road corridor
#: crossing a runout a few hundred metres wide. More than that does not make the
#: consequence term grow without bound.
EXPOSURE_FULL_COVERAGE_M2 = 10_000.0

#: How the three exposure measurements combine. The peak weight under the central
#: footprint dominates, because what an avalanche hits matters more than how much
#: of the path is covered. The sensitivity envelope contributes less than the core:
#: it is where the avalanche *could* reach, not where the central estimate puts it.
EXPOSURE_PEAK_WEIGHT = 0.55
EXPOSURE_EXTENT_WEIGHT = 0.20
EXPOSURE_ENVELOPE_WEIGHT = 0.25

#: The most exposure can raise a zone's index, as a fraction. Deliberately bounded
#: and deliberately one-directional: exposure is a consequence term bolted onto a
#: terrain result, not evidence about whether a slab will release.
EXPOSURE_MAX_UPLIFT = 0.35

#: Published basis labels. A reader must never compare a release-only number with a
#: fully-decomposed one without being told they are built from different terms.
BASIS_LABELS = {
    "release_reach_exposure": "Release, reach and exposure",
    "release_and_reach": "Release and reach; exposure unavailable",
    "release_and_exposure": "Release and exposure; reach unavailable",
    "release_only": "Release only; runout was not simulated for this zone",
}


def parameter_manifest() -> dict[str, Any]:
    """Return every tunable that influences the composite hazard index."""
    return {
        "reach_full_distance_m": REACH_FULL_DISTANCE_M,
        "reach_full_drop_m": REACH_FULL_DROP_M,
        "reach_full_velocity_ms": REACH_FULL_VELOCITY_MS,
        "reach_distance_weight": REACH_DISTANCE_WEIGHT,
        "reach_drop_weight": REACH_DROP_WEIGHT,
        "reach_velocity_weight": REACH_VELOCITY_WEIGHT,
        "reach_base": REACH_BASE,
        "exposure_full_coverage_m2": EXPOSURE_FULL_COVERAGE_M2,
        "exposure_peak_weight": EXPOSURE_PEAK_WEIGHT,
        "exposure_extent_weight": EXPOSURE_EXTENT_WEIGHT,
        "exposure_envelope_weight": EXPOSURE_ENVELOPE_WEIGHT,
        "exposure_max_uplift": EXPOSURE_MAX_UPLIFT,
        "aggregation": "area_weighted_mean_of_zone_index",
        "is_probability": False,
        "is_calibrated": False,
    }


def _finite(value: Any) -> float | None:
    """Return a finite float, or None for anything that is not a measurement."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


# =============================================================================
# Reach
# =============================================================================


@dataclass(frozen=True)
class ReachComponent:
    """How destructive this zone's runout was, normalised to 0-1."""

    value: float | None
    available: bool
    parts: dict[str, float | None] = field(default_factory=dict)
    measurements: dict[str, float | None] = field(default_factory=dict)
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4) if self.value is not None else None,
            "available": self.available,
            "parts": {
                name: (round(part, 4) if part is not None else None)
                for name, part in self.parts.items()
            },
            "measurements": dict(self.measurements),
            "unavailable_reason": self.unavailable_reason,
        }


def reach_unavailable(reason: str) -> ReachComponent:
    """A zone whose runout was never simulated. Explicitly missing, never zero."""
    return ReachComponent(
        value=None,
        available=False,
        parts={"distance": None, "drop": None, "velocity": None},
        measurements={
            "horizontal_reach_m": None,
            "vertical_drop_m": None,
            "max_velocity_ms": None,
        },
        unavailable_reason=reason,
    )


def reach_component(
    *,
    horizontal_reach_m: Any = None,
    vertical_drop_m: Any = None,
    max_velocity_ms: Any = None,
) -> ReachComponent:
    """Combine the runout's distance, drop and speed into one 0-1 term.

    Any measurement the engine did not produce is dropped and the remaining
    weights are renormalised, rather than being scored as zero. Fast routing
    reports no velocity at all, so scoring its missing velocity as 0 would cap
    every fast-mode zone at 80% of the reach term and quietly make the fast engine
    look less consequential than the advanced one.
    """
    distance = _finite(horizontal_reach_m)
    drop = _finite(vertical_drop_m)
    velocity = _finite(max_velocity_ms)

    parts: dict[str, float | None] = {
        "distance": _clip01(distance / REACH_FULL_DISTANCE_M) if distance is not None else None,
        "drop": _clip01(drop / REACH_FULL_DROP_M) if drop is not None else None,
        "velocity": _clip01(velocity / REACH_FULL_VELOCITY_MS) if velocity is not None else None,
    }
    weights = {
        "distance": REACH_DISTANCE_WEIGHT,
        "drop": REACH_DROP_WEIGHT,
        "velocity": REACH_VELOCITY_WEIGHT,
    }
    measurements = {
        "horizontal_reach_m": distance,
        "vertical_drop_m": drop,
        "max_velocity_ms": velocity,
    }

    available = {name: part for name, part in parts.items() if part is not None}
    if not available:
        return ReachComponent(
            value=None,
            available=False,
            parts=parts,
            measurements=measurements,
            unavailable_reason=(
                "The runout result carried no reach measurement, so reach is unknown rather "
                "than zero."
            ),
        )

    total_weight = sum(weights[name] for name in available)
    value = sum(weights[name] * part for name, part in available.items()) / total_weight
    missing = sorted(name for name in parts if parts[name] is None)
    return ReachComponent(
        value=_clip01(value),
        available=True,
        parts=parts,
        measurements=measurements,
        unavailable_reason=(
            None
            if not missing
            else (
                f"This engine does not measure {', '.join(missing)}; the remaining reach "
                "weights were renormalised rather than treating it as zero."
            )
        ),
    )


# =============================================================================
# Exposure
# =============================================================================


@dataclass(frozen=True)
class ExposureComponent:
    """What the baked exposure layer maps under this zone's runout, 0-1."""

    value: float | None
    available: bool
    parts: dict[str, float | None] = field(default_factory=dict)
    #: Human-readable labels of the exposure classes found under the runout, in
    #: descending weight. Deliberately class labels and not feature names: the
    #: baked layer is a per-cell class raster, so "a trunk highway" is what it can
    #: honestly support and "Highway 3" is not.
    classes: tuple[str, ...] = ()
    measurements: dict[str, Any] = field(default_factory=dict)
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 4) if self.value is not None else None,
            "available": self.available,
            "parts": {
                name: (round(part, 4) if part is not None else None)
                for name, part in self.parts.items()
            },
            "classes": list(self.classes),
            "measurements": dict(self.measurements),
            "unavailable_reason": self.unavailable_reason,
        }


def exposure_unavailable(reason: str) -> ExposureComponent:
    """No exposure evidence for this zone. Explicitly missing, never zero."""
    return ExposureComponent(
        value=None,
        available=False,
        parts={"peak_core": None, "extent_core": None, "peak_envelope": None},
        unavailable_reason=reason,
    )


def exposure_component(
    *,
    peak_core: float,
    peak_envelope: float,
    covered_area_m2: float,
    classes: Sequence[str] = (),
    known_cell_count: int = 0,
    unknown_cell_count: int = 0,
) -> ExposureComponent:
    """Combine peak weight, covered extent and envelope peak into one 0-1 term.

    ``peak_core`` and ``peak_envelope`` are the largest exposure weight found under
    the central footprint and under the sensitivity envelope. ``covered_area_m2``
    is the ground area under the central footprint carrying any exposure weight at
    all. A measured zero here is a real statement -- the exposure layer covers this
    path and maps nothing in it -- and is distinct from the unavailable case.
    """
    core = _clip01(_finite(peak_core) or 0.0)
    envelope = _clip01(_finite(peak_envelope) or 0.0)
    area = max(0.0, _finite(covered_area_m2) or 0.0)
    extent = _clip01(area / EXPOSURE_FULL_COVERAGE_M2)

    value = _clip01(
        EXPOSURE_PEAK_WEIGHT * core
        + EXPOSURE_EXTENT_WEIGHT * extent
        + EXPOSURE_ENVELOPE_WEIGHT * envelope
    )
    return ExposureComponent(
        value=value,
        available=True,
        parts={"peak_core": core, "extent_core": extent, "peak_envelope": envelope},
        classes=tuple(classes),
        measurements={
            "peak_weight_under_runout": round(core, 4),
            "peak_weight_under_envelope": round(envelope, 4),
            "exposed_area_under_runout_m2": round(area, 1),
            "known_exposure_cells": int(known_cell_count),
            "unknown_exposure_cells": int(unknown_cell_count),
        },
        unavailable_reason=None,
    )


# =============================================================================
# Per-zone index
# =============================================================================


@dataclass(frozen=True)
class ZoneHazard:
    """One release zone's composite index, with every part it was built from."""

    zone_id: str
    area_m2: float
    index: float
    release_score: float
    terrain_and_reach_index: float
    basis: str
    reach: ReachComponent
    exposure: ExposureComponent

    @property
    def components_available(self) -> dict[str, bool]:
        return {
            "release": True,
            "reach": self.reach.available,
            "exposure": self.exposure.available,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "hazard_index": round(self.index, 1),
            "basis": self.basis,
            "basis_label": BASIS_LABELS[self.basis],
            "components_available": self.components_available,
            "release": {
                "value": round(_clip01(self.release_score / 100.0), 4),
                "available": True,
                "estimated_release_score": self.release_score,
            },
            "reach": self.reach.to_dict(),
            "exposure": self.exposure.to_dict(),
            "terrain_and_reach_index": round(self.terrain_and_reach_index, 1),
            "exposure_uplift_points": round(self.index - self.terrain_and_reach_index, 1),
            "is_probability": False,
            "is_calibrated": False,
        }


def zone_hazard_index(
    *,
    zone_id: str,
    area_m2: float,
    release_score: float,
    reach: ReachComponent,
    exposure: ExposureComponent,
) -> ZoneHazard:
    """Combine one zone's three components into its 0-100 composite index."""
    release = _clip01((_finite(release_score) or 0.0) / 100.0)

    if reach.available and reach.value is not None:
        terrain_and_reach = 100.0 * release * (REACH_BASE + (1.0 - REACH_BASE) * reach.value)
    else:
        # No runout for this zone. The index is the release estimate alone, on its
        # own labelled basis -- never the reach formula with reach silently at 0,
        # which would report an unsimulated zone as the least hazardous thing on
        # the mountain.
        terrain_and_reach = 100.0 * release

    if exposure.available and exposure.value is not None:
        index = terrain_and_reach * (1.0 + EXPOSURE_MAX_UPLIFT * exposure.value)
    else:
        index = terrain_and_reach

    if reach.available and exposure.available:
        basis = "release_reach_exposure"
    elif reach.available:
        basis = "release_and_reach"
    elif exposure.available:
        basis = "release_and_exposure"
    else:
        basis = "release_only"

    return ZoneHazard(
        zone_id=zone_id,
        area_m2=float(area_m2),
        index=min(100.0, max(0.0, index)),
        release_score=float(release_score),
        terrain_and_reach_index=min(100.0, max(0.0, terrain_and_reach)),
        basis=basis,
        reach=reach,
        exposure=exposure,
    )


# =============================================================================
# Whole-area aggregate
# =============================================================================


@dataclass(frozen=True)
class AreaHazard:
    """The area-weighted mean of the per-zone indices, and what diluted it."""

    index: float | None
    peak_zone_index: float | None
    peak_zone_id: str | None
    #: Which components the peak zone's index was built from. Without it, a peak
    #: that happens to be an unsimulated release-only zone reads as a fully
    #: decomposed one.
    peak_zone_basis: str | None
    zone_count: int
    components: dict[str, Any]


def aggregate_area_hazard(zones: Sequence[ZoneHazard]) -> AreaHazard:
    """Area-weight the per-zone indices and publish what the mean hid.

    A plain area-weighted mean is what the label says it is, so it stays. But one
    large mild zone can bury a small severe one inside it, so the peak zone is
    published beside the mean rather than left for the reader to discover.
    """
    if not zones:
        return AreaHazard(
            index=None,
            peak_zone_index=None,
            peak_zone_id=None,
            peak_zone_basis=None,
            zone_count=0,
            components={
                "method": (
                    "No release zone crossed the threshold, so no per-zone hazard index exists "
                    "and no area hazard index was computed. This is not a hazard of zero."
                ),
                "zone_count": 0,
            },
        )

    total_area = sum(zone.area_m2 for zone in zones)
    if total_area <= 0:
        weights = [1.0 / len(zones)] * len(zones)
    else:
        weights = [zone.area_m2 / total_area for zone in zones]

    index = sum(weight * zone.index for weight, zone in zip(weights, zones))
    peak = max(zones, key=lambda zone: zone.index)

    by_basis: dict[str, int] = {}
    for zone in zones:
        by_basis[zone.basis] = by_basis.get(zone.basis, 0) + 1
    reach_zones = [zone for zone in zones if zone.reach.available]
    exposure_zones = [zone for zone in zones if zone.exposure.available]
    exposed_zones = [
        zone for zone in exposure_zones if (zone.exposure.value or 0.0) > 0.0
    ]

    def _weighted(selected: Sequence[ZoneHazard], value) -> float | None:
        if not selected:
            return None
        area = sum(zone.area_m2 for zone in selected)
        if area <= 0:
            return round(sum(value(zone) for zone in selected) / len(selected), 4)
        return round(sum(zone.area_m2 * value(zone) for zone in selected) / area, 4)

    components = {
        "method": (
            "Area-weighted mean of the per-zone composite hazard index across every release "
            "zone. Each zone index is 100 x release x (reach floor + reach), raised by up to "
            f"{EXPOSURE_MAX_UPLIFT:.0%} where the baked exposure layer maps something under its "
            "runout. Exposure can only raise a zone index, never lower it."
        ),
        "aggregation": "area_weighted_mean_of_zone_index",
        "zone_count": len(zones),
        "zone_count_by_basis": by_basis,
        "contributing_zone_count": {
            "release": len(zones),
            "reach": len(reach_zones),
            "exposure": len(exposure_zones),
        },
        "zones_with_mapped_exposure": len(exposed_zones),
        "area_weighted_component_mean": {
            "release": _weighted(zones, lambda zone: _clip01(zone.release_score / 100.0)),
            "reach": _weighted(reach_zones, lambda zone: zone.reach.value or 0.0),
            "exposure": _weighted(exposure_zones, lambda zone: zone.exposure.value or 0.0),
        },
        "area_weighted_terrain_and_reach_index": _weighted(
            zones, lambda zone: zone.terrain_and_reach_index
        ),
        "dilution": (
            "The area-weighted mean is what its name says: a large mild zone outweighs a small "
            "severe one. peak_zone_index is published beside it so that dilution is visible."
        ),
        "is_probability": False,
        "is_calibrated": False,
    }
    if len(by_basis) > 1:
        components["comparability"] = (
            "Zone indices in this result were built from different components. A release-only "
            "zone carries no reach or exposure information and is not directly comparable with "
            "a zone whose runout was simulated."
        )

    return AreaHazard(
        index=min(100.0, max(0.0, index)),
        peak_zone_index=round(peak.index, 1),
        peak_zone_id=peak.zone_id,
        peak_zone_basis=peak.basis,
        zone_count=len(zones),
        components=components,
    )
