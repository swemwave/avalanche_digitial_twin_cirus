"""Buried weak-interface index: the variable the loading search did not have.

Why this module exists
----------------------
A 128-configuration search over drift kernels, release thresholds, loading
bases, snow and wind weights, slope responses and morphologies failed to beat a
same-area slope ranking on all five development blocks
(``release-config-search-v1.json``). The written conclusion was that the
remaining gap is not parameterisation: the model has no snowpack-stratigraphy
information, and stratigraphy is the variable that separates steep terrain that
released from steep terrain that did not. This module supplies a first,
explicitly bounded formulation of that variable so the claim can be tested
rather than asserted.

What this is, and what it is emphatically not
---------------------------------------------
It is an **index built from antecedent surface meteorology and modelled snow
depth**. It contains no snow profile, no stability test, no grain-type
observation, no shear-frame or ECT result, and no measurement of any actual
buried layer. It cannot be verified from the data that produces it. It must
never be described as weak-layer physics, as a probability, or as a stability
rating. ``avycore.snowpack.state`` already says this about its own cruder
proxy; nothing here weakens that statement, and this module is a refinement of
that proxy's *form*, not a promotion of its epistemic status.

The three mechanisms it does represent
--------------------------------------
``buried_weak_layer_index`` is a product of three bounded factors, each of
which can independently zero the result:

1. **Formation.** Two named surface-weakening mechanisms, combined by taking
   the stronger of the two rather than by summing, because they are
   alternatives rather than additive contributions:

   *Kinetic-growth (near-surface faceting)* -- a temperature gradient across
   the snowpack above roughly 10 K/m drives vapour transport and faceted-crystal
   growth (Colbeck 1982, *Rev. Geophys.* 20, 45; Armstrong 1985; Birkeland
   1998, *Arctic Alpine Res.* 30, 193). The gradient is estimated from the
   modelled snow depth and the air temperature lapsed to the cell, so it needs
   a snow-depth series and is **unknown without one**.

   *Cold, calm, dry surface hours* -- the conditions under which surface hoar
   and radiation-recrystallised near-surface facets form. This is exactly the
   count ``avycore.snowpack.state.buried_weak_interface_proxy`` already made,
   preserved unchanged so the refinement is additive.

2. **Persistence.** Facets and surface hoar are destroyed by wetting. Positive
   degree-hours and rain during the antecedent window decay the index toward
   zero, and enough of either removes it entirely.

3. **Burial.** A weak interface is only a weak *interface* once a slab sits on
   it. Below a minimum storm accumulation the index is zero regardless of how
   weak the surface was.

Refusals this module keeps
--------------------------
* **Missing input is unknown, never zero.** Without a snow-depth series the
  gradient mechanism cannot be evaluated, and the returned ``known`` mask says
  so. A caller that gives the index numerical weight must treat an unknown cell
  as missing input -- never as a cell with no weak layer, which is the
  favourable reading of an absent measurement and is exactly the failure mode
  the rest of this package refuses.
* **No constant here is fitted.** Every threshold is a literature or practice
  value, or an explicitly declared bound. None was chosen by looking at a
  capture score.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "BURIAL_FULL_NEW_SNOW_CM",
    "BURIAL_MINIMUM_NEW_SNOW_CM",
    "CRITICAL_TEMPERATURE_GRADIENT_K_PER_M",
    "FACETING_FULL_HOURS",
    "GRADIENT_MINIMUM_SNOW_DEPTH_M",
    "MELT_DESTRUCTION_POSITIVE_DEGREE_HOURS",
    "RAIN_DESTRUCTION_MM",
    "SLAB_MINIMUM_ANTECEDENT_DEPTH_M",
    "StratigraphyConfig",
    "bulk_temperature_gradient_k_per_m",
    "buried_weak_layer_index",
    "stratigraphy_parameter_manifest",
]


# =============================================================================
# Named constants. Every one is an UNCALIBRATED literature or practice value.
# =============================================================================

#: Snowpack temperature gradient above which kinetic (faceted) crystal growth
#: dominates equilibrium growth. About 10 K/m is the value quoted throughout the
#: snow-metamorphism literature (Colbeck 1982; Armstrong 1985; Birkeland 1998).
#: It is a regime boundary, not a sharp physical switch, and it is applied here
#: as one because a bounded index needs a declared reference.
CRITICAL_TEMPERATURE_GRADIENT_K_PER_M = 10.0

#: Depth used in place of a thinner modelled pack when estimating the bulk
#: gradient. Dividing a temperature difference by a few centimetres manufactures
#: gradients of hundreds of K/m, which the bulk formula cannot support. Clamping
#: the denominator can only *lower* the estimated gradient, so this is the
#: conservative direction. A genuinely thin early-season pack is a well known
#: faceting regime that this formulation therefore understates rather than
#: overstates.
GRADIENT_MINIMUM_SNOW_DEPTH_M = 0.20

#: Antecedent hours above the critical gradient that saturate the faceting
#: term. Recognisable near-surface facet growth takes days, not hours; three
#: days is a declared reference point, not a measured growth rate.
FACETING_FULL_HOURS = 72.0

#: Wetting destroys facets and surface hoar. This many antecedent positive
#: degree-hours removes the index entirely -- roughly a day at +1 C, or a few
#: hours of strong melt.
MELT_DESTRUCTION_POSITIVE_DEGREE_HOURS = 24.0

#: Antecedent rain on the surface that removes the index entirely.
RAIN_DESTRUCTION_MM = 5.0

#: A weak interface is only an interface once a slab is on top of it. Below this
#: much storm snow the index is zero. Same 10 cm the v1 proxy used.
BURIAL_MINIMUM_NEW_SNOW_CM = 10.0

#: Storm accumulation at which the burial factor saturates. Above this the
#: factor stays 1: for a *natural* release a deeper slab is more load, not less,
#: so no upper taper is applied. An upper taper would encode human-trigger
#: accessibility, which this model does not represent.
BURIAL_FULL_NEW_SNOW_CM = 40.0

#: A buried interface needs a pack to be buried in. Below this modelled
#: pre-storm depth the concept does not apply and the index is zero.
SLAB_MINIMUM_ANTECEDENT_DEPTH_M = 0.20

#: Snow at the base of a seasonal pack sits at or near the melting point. This
#: is the standard first-order assumption behind a bulk-gradient estimate; it is
#: not a measurement, and it fails for a shallow pack over frozen ground.
BASAL_SNOW_TEMPERATURE_C = 0.0


@dataclass(frozen=True)
class StratigraphyConfig:
    """Every tunable in the buried-weak-interface index.

    Defaults are the literature values above. ``loading_weight`` of zero makes
    the whole index numerically inert, which is what keeps the frozen v1
    equivalence exact.
    """

    #: Weight the index carries in the dry-slab loading sum, parallel to the
    #: snow and wind weights. Zero reproduces every published result exactly.
    loading_weight: float = 0.0
    critical_gradient_k_per_m: float = CRITICAL_TEMPERATURE_GRADIENT_K_PER_M
    faceting_full_hours: float = FACETING_FULL_HOURS
    gradient_minimum_snow_depth_m: float = GRADIENT_MINIMUM_SNOW_DEPTH_M
    melt_destruction_positive_degree_hours: float = (
        MELT_DESTRUCTION_POSITIVE_DEGREE_HOURS
    )
    rain_destruction_mm: float = RAIN_DESTRUCTION_MM
    burial_minimum_new_snow_cm: float = BURIAL_MINIMUM_NEW_SNOW_CM
    burial_full_new_snow_cm: float = BURIAL_FULL_NEW_SNOW_CM
    slab_minimum_antecedent_depth_m: float = SLAB_MINIMUM_ANTECEDENT_DEPTH_M

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": "avycore-stratigraphy-parameters-v1",
            "loading_weight": self.loading_weight,
            "critical_gradient_k_per_m": self.critical_gradient_k_per_m,
            "faceting_full_hours": self.faceting_full_hours,
            "gradient_minimum_snow_depth_m": self.gradient_minimum_snow_depth_m,
            "melt_destruction_positive_degree_hours": (
                self.melt_destruction_positive_degree_hours
            ),
            "rain_destruction_mm": self.rain_destruction_mm,
            "burial_minimum_new_snow_cm": self.burial_minimum_new_snow_cm,
            "burial_full_new_snow_cm": self.burial_full_new_snow_cm,
            "slab_minimum_antecedent_depth_m": self.slab_minimum_antecedent_depth_m,
            "is_a_snow_profile_observation": False,
            "is_a_probability": False,
            "requires_snow_depth_series_for_gradient_mechanism": True,
            "unknown_is_missing_input_not_zero": True,
        }


def bulk_temperature_gradient_k_per_m(
    surface_temperature_c: np.ndarray,
    snow_depth_m: np.ndarray,
    *,
    minimum_depth_m: float = GRADIENT_MINIMUM_SNOW_DEPTH_M,
) -> np.ndarray:
    """Bulk snowpack temperature gradient, in kelvin per metre.

    ``(T_base - T_surface) / depth`` with the base at the melting point. Three
    things this is not:

    * It is not the *near-surface* gradient, which is what actually drives
      near-surface faceting and is typically several times larger under a clear
      night sky. The bulk gradient is the one the available forcing can support,
      and it understates the driver.
    * It is not computed from a snow-surface temperature. Air temperature is
      used, and under clear skies the radiating snow surface is colder than the
      air, so this again understates the gradient.
    * It is not defined for a pack thinner than ``minimum_depth_m``; the
      denominator is clamped there, which caps rather than inflates the result.

    Both approximations point the same way, so the index this feeds is a lower
    bound on the gradient forcing, not a flattering one.
    """
    depth = np.maximum(np.asarray(snow_depth_m, dtype="float64"), minimum_depth_m)
    difference = BASAL_SNOW_TEMPERATURE_C - np.asarray(
        surface_temperature_c, dtype="float64"
    )
    return np.maximum(difference, 0.0) / depth


def buried_weak_layer_index(
    *,
    faceting_hours: np.ndarray | None,
    surface_weakening_fraction: np.ndarray,
    antecedent_positive_degree_hours: np.ndarray,
    antecedent_rain_mm: np.ndarray,
    new_snow_index_cm: np.ndarray,
    antecedent_snow_depth_m: np.ndarray | None,
    config: StratigraphyConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """Formation x persistence x burial, and the mask of where it is knowable.

    Returns ``(index, known)``. ``index`` is bounded to [0, 1] and is zero
    wherever any factor is zero. ``known`` is False wherever the inputs cannot
    support the calculation -- specifically wherever no snow-depth series was
    available, which makes both the gradient mechanism and the slab-presence
    test unevaluable. A caller giving the index weight must treat an unknown
    cell as missing input.

    ``surface_weakening_fraction`` is the already-bounded cold/calm/dry hour
    fraction the v1 proxy counts. It is accepted rather than recomputed so that
    the refinement cannot silently change the mechanism it builds on.
    """
    hoar = np.clip(np.asarray(surface_weakening_fraction, dtype="float64"), 0.0, 1.0)
    depth_available = (
        antecedent_snow_depth_m is not None and faceting_hours is not None
    )
    if depth_available:
        facets = np.clip(
            np.asarray(faceting_hours, dtype="float64") / config.faceting_full_hours,
            0.0,
            1.0,
        )
        # Alternatives, not contributions: a surface can be weakened by kinetic
        # growth through the pack or by radiative recrystallisation at the top,
        # and a storm buries whichever happened. Summing them would let two
        # partial mechanisms manufacture a fully weak interface.
        formation = np.maximum(facets, hoar)
        antecedent_depth = np.asarray(antecedent_snow_depth_m, dtype="float64")
        slab_possible = antecedent_depth >= config.slab_minimum_antecedent_depth_m
        known = np.ones(formation.shape, dtype=bool)
    else:
        # No depth series: the gradient mechanism is unevaluable and so is the
        # "is there a pack to bury it in" test. Falling back to the hoar term
        # alone would report a *smaller* index for a cell whose faceting is
        # simply unmeasured, which is treating an absent input as a favourable
        # one. The value is therefore unknown, and the caller must refuse it.
        formation = hoar
        slab_possible = np.ones(hoar.shape, dtype=bool)
        known = np.zeros(hoar.shape, dtype=bool)

    melt = np.clip(
        np.asarray(antecedent_positive_degree_hours, dtype="float64")
        / config.melt_destruction_positive_degree_hours,
        0.0,
        1.0,
    )
    rain = np.clip(
        np.asarray(antecedent_rain_mm, dtype="float64") / config.rain_destruction_mm,
        0.0,
        1.0,
    )
    persistence = np.clip(1.0 - np.maximum(melt, rain), 0.0, 1.0)

    span = config.burial_full_new_snow_cm - config.burial_minimum_new_snow_cm
    burial = (
        np.clip(
            (np.asarray(new_snow_index_cm, dtype="float64") - config.burial_minimum_new_snow_cm)
            / span,
            0.0,
            1.0,
        )
        if span > 0.0
        else (
            np.asarray(new_snow_index_cm, dtype="float64")
            >= config.burial_minimum_new_snow_cm
        ).astype("float64")
    )

    index = formation * persistence * burial * slab_possible
    return np.clip(index, 0.0, 1.0), known


def stratigraphy_parameter_manifest() -> dict[str, Any]:
    """Every constant this module defines, for a provenance hash."""
    return {
        "critical_temperature_gradient_k_per_m": CRITICAL_TEMPERATURE_GRADIENT_K_PER_M,
        "gradient_minimum_snow_depth_m": GRADIENT_MINIMUM_SNOW_DEPTH_M,
        "faceting_full_hours": FACETING_FULL_HOURS,
        "melt_destruction_positive_degree_hours": (
            MELT_DESTRUCTION_POSITIVE_DEGREE_HOURS
        ),
        "rain_destruction_mm": RAIN_DESTRUCTION_MM,
        "burial_minimum_new_snow_cm": BURIAL_MINIMUM_NEW_SNOW_CM,
        "burial_full_new_snow_cm": BURIAL_FULL_NEW_SNOW_CM,
        "slab_minimum_antecedent_depth_m": SLAB_MINIMUM_ANTECEDENT_DEPTH_M,
        "basal_snow_temperature_c": BASAL_SNOW_TEMPERATURE_C,
        "every_constant_is_uncalibrated": True,
        "contains_no_snow_profile_observation": True,
    }
