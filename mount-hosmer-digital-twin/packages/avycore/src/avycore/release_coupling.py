"""The SnowState-to-Release input contract for dry-slab release coupling.

A physics-informed release model needs **both** capable terrain and modelled snow
instability.  This module defines what a snow state must supply before it may be
coupled to terrain, and refuses anything less.  It computes no release value: it
decides eligibility and reports, in machine-readable form, exactly what is missing.

Three rules are encoded here rather than left to callers, because getting any of
them wrong is a safety problem and not a modelling preference:

1. **Terrain alone cannot produce a loaded-snow release result.** There is no code
   path from terrain capability to an instability result without a snow-state
   term, so a missing snow state cannot be papered over with terrain.
2. **A missing snow state removes cells from supported coverage.** It never lowers
   a score toward safety — an unsupported cell is unknown, not benign.
3. **Only dry-slab release is in scope.** Wet-snow, dry-loose, glide, cornice,
   powder-cloud and mixed regimes are separate model types, not extra weights in
   one score, and are refused rather than approximated.

Everything a snow state supplies here is **modelled output, never an observation**,
and the contract requires each field to say so.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RELEASE_COUPLING_SCHEMA_VERSION = "avycore-release-coupling-v1"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoupledRegime(StrEnum):
    """The only regime this coupling contract covers."""

    DRY_SLAB = "dry_slab"


class SnowStateProvenance(StrictModel):
    """Where a coupled snow state came from, and what it is not."""

    snow_state_pack_id: str = Field(min_length=1)
    snow_state_sha256: str = Field(pattern=SHA256_PATTERN)
    condition_pack_id: str = Field(min_length=1)
    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    engine_version: str = Field(min_length=1)
    terrain_class_mapping_version: str = Field(min_length=1)
    values_are_modelled_not_observed: Literal[True] = True
    simulation_start_utc: str = Field(min_length=1)
    valid_time_utc: str = Field(min_length=1)

    @model_validator(mode="after")
    def history_precedes_validity(self) -> "SnowStateProvenance":
        # A profile invented at the requested time has no layer history.  The
        # simulation must start before the state it claims to describe.
        if self.simulation_start_utc >= self.valid_time_utc:
            raise ValueError(
                "The snow simulation must start before the valid time it describes; a profile "
                "generated at the requested time has no evolved layer history."
            )
        return self


class ModelledQuantity(StrictModel):
    """One modelled snow-state quantity with units and an uncertainty span.

    ``lower``/``upper`` are a bounded sensitivity span, not a confidence interval.
    They are required: a coupled quantity with no stated span would present false
    precision to the release model downstream.
    """

    value: float
    unit: str = Field(min_length=1)
    lower: float
    upper: float
    basis: Literal["source", "literature", "expert", "numerical"]
    interpretation: Literal["bounded_sensitivity_not_probability"] = (
        "bounded_sensitivity_not_probability"
    )

    @model_validator(mode="after")
    def ordered_finite(self) -> "ModelledQuantity":
        values = (self.lower, self.value, self.upper)
        if not all(math.isfinite(item) for item in values):
            raise ValueError("Modelled snow-state quantities must be finite.")
        if not self.lower <= self.value <= self.upper:
            raise ValueError("Modelled snow-state bounds must satisfy lower <= value <= upper.")
        return self


class WeakLayerCandidate(StrictModel):
    """A modelled candidate weak layer. Never a field observation of one."""

    layer_id: str = Field(pattern=IDENTIFIER_PATTERN)
    depth_below_surface: ModelledQuantity
    grain_type: str = Field(min_length=1)
    grain_size: ModelledQuantity
    hardness: ModelledQuantity
    is_persistent: bool

    @model_validator(mode="after")
    def declared_units(self) -> "WeakLayerCandidate":
        if self.depth_below_surface.unit != "m":
            raise ValueError("Weak-layer depth must be declared in metres.")
        if self.grain_size.unit != "mm":
            raise ValueError("Weak-layer grain size must be declared in millimetres.")
        return self


class ReleaseCouplingInputs(StrictModel):
    """The complete set a dry-slab release model may be coupled to.

    Every field is required.  There is no partially specified variant, because a
    release model that silently ran on half a snow state would produce a number
    that looks the same as one that ran on a complete state.
    """

    schema_version: Literal["avycore-release-coupling-v1"]
    regime: CoupledRegime
    terrain_class_id: str = Field(pattern=IDENTIFIER_PATTERN)
    provenance: SnowStateProvenance

    slab_depth: ModelledQuantity
    slab_density: ModelledQuantity
    weak_layer: WeakLayerCandidate
    failure_initiation_index: ModelledQuantity
    crack_propagation_index: ModelledQuantity
    loading_rate: ModelledQuantity
    loading_window_hours: float = Field(gt=0)

    @model_validator(mode="after")
    def units_and_ranges(self) -> "ReleaseCouplingInputs":
        expected = {
            "slab_depth": "m",
            "slab_density": "kg m-3",
            "failure_initiation_index": "1",
            "crack_propagation_index": "m",
            "loading_rate": "kg m-2 h-1",
        }
        for name, unit in expected.items():
            quantity: ModelledQuantity = getattr(self, name)
            if quantity.unit != unit:
                raise ValueError(f"{name} must be declared in {unit!r}, got {quantity.unit!r}.")
        if self.slab_depth.lower <= 0.0:
            raise ValueError("A coupled slab must have a positive lower-bound depth.")
        if self.slab_density.lower <= 0.0:
            raise ValueError("A coupled slab must have a positive lower-bound density.")
        if self.crack_propagation_index.lower <= 0.0:
            raise ValueError("Critical crack length must be positive.")
        if self.loading_rate.lower < 0.0:
            raise ValueError("Loading rate cannot be negative.")
        if self.weak_layer.depth_below_surface.value > self.slab_depth.upper:
            raise ValueError(
                "The candidate weak layer lies below the deepest modelled slab; the pair is "
                "inconsistent and cannot be coupled."
            )
        return self

    def slab_mass_per_area_kg_m2(self) -> tuple[float, float, float]:
        """Central, lower and upper slab mass per unit area, in kg m-2."""

        return (
            self.slab_depth.value * self.slab_density.value,
            self.slab_depth.lower * self.slab_density.lower,
            self.slab_depth.upper * self.slab_density.upper,
        )


REQUIRED_COUPLING_FIELDS: tuple[str, ...] = (
    "slab_depth",
    "slab_density",
    "weak_layer",
    "failure_initiation_index",
    "crack_propagation_index",
    "loading_rate",
)


class CouplingEligibility(StrictModel):
    """Whether a terrain class may be coupled, and what is missing if not."""

    terrain_class_id: str = Field(pattern=IDENTIFIER_PATTERN)
    eligible: bool
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = Field(min_length=1)
    coverage_effect: Literal["supported", "removed_from_supported_coverage"]

    @model_validator(mode="after")
    def ineligible_is_removed_never_lowered(self) -> "CouplingEligibility":
        if self.eligible:
            if self.missing_fields:
                raise ValueError("An eligible terrain class cannot report missing fields.")
            if self.coverage_effect != "supported":
                raise ValueError("An eligible terrain class must be supported coverage.")
        else:
            # The only admissible consequence of an ineligible class is removal
            # from supported coverage.  There is deliberately no enum member that
            # would let a missing snow state express itself as a lower score.
            if self.coverage_effect != "removed_from_supported_coverage":
                raise ValueError(
                    "An ineligible terrain class is removed from supported coverage; a missing "
                    "snow state must never lower a result toward safety."
                )
        return self


def evaluate_coupling_eligibility(
    terrain_class_id: str,
    *,
    regime: str,
    snow_state: object | None,
) -> CouplingEligibility:
    """Decide whether one terrain class may enter a coupled dry-slab release.

    ``snow_state`` is either a complete :class:`ReleaseCouplingInputs` or anything
    else — including ``None`` and a partially populated mapping.  Anything that is
    not a complete, validated contract instance is ineligible.
    """

    if regime != CoupledRegime.DRY_SLAB.value:
        return CouplingEligibility(
            terrain_class_id=terrain_class_id,
            eligible=False,
            missing_fields=(),
            reasons=(
                f"Regime {regime!r} has no coupling contract. Wet-snow, dry-loose, glide, cornice, "
                "powder-cloud and mixed release are separate model types, not extra weights on the "
                "dry-slab score.",
            ),
            coverage_effect="removed_from_supported_coverage",
        )
    if snow_state is None:
        return CouplingEligibility(
            terrain_class_id=terrain_class_id,
            eligible=False,
            missing_fields=REQUIRED_COUPLING_FIELDS,
            reasons=(
                "No modelled snow state is bound to this terrain class, so it is removed from "
                "supported coverage. Terrain capability alone cannot produce a loaded-snow "
                "instability result.",
            ),
            coverage_effect="removed_from_supported_coverage",
        )
    if not isinstance(snow_state, ReleaseCouplingInputs):
        present = set()
        if isinstance(snow_state, dict):
            present = {name for name in REQUIRED_COUPLING_FIELDS if snow_state.get(name) is not None}
        missing = tuple(name for name in REQUIRED_COUPLING_FIELDS if name not in present)
        return CouplingEligibility(
            terrain_class_id=terrain_class_id,
            eligible=False,
            missing_fields=missing or REQUIRED_COUPLING_FIELDS,
            reasons=(
                "The supplied snow state is not a complete validated coupling contract. A release "
                "model must not run on a partial snow state, because its output would be "
                "indistinguishable from one computed on a complete state.",
            ),
            coverage_effect="removed_from_supported_coverage",
        )
    if snow_state.terrain_class_id != terrain_class_id:
        return CouplingEligibility(
            terrain_class_id=terrain_class_id,
            eligible=False,
            missing_fields=(),
            reasons=(
                f"The supplied snow state describes terrain class "
                f"{snow_state.terrain_class_id!r}, not {terrain_class_id!r}.",
            ),
            coverage_effect="removed_from_supported_coverage",
        )
    return CouplingEligibility(
        terrain_class_id=terrain_class_id,
        eligible=True,
        missing_fields=(),
        reasons=(
            "A complete modelled dry-slab snow state is bound to this terrain class. Its values "
            "are model output, not field observations, and remain uncalibrated.",
        ),
        coverage_effect="supported",
    )


__all__ = [
    "CoupledRegime",
    "CouplingEligibility",
    "ModelledQuantity",
    "RELEASE_COUPLING_SCHEMA_VERSION",
    "REQUIRED_COUPLING_FIELDS",
    "ReleaseCouplingInputs",
    "SnowStateProvenance",
    "WeakLayerCandidate",
    "evaluate_coupling_eligibility",
]
