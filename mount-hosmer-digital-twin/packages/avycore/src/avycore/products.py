"""Immutable prediction products: the last stage of the offline pipeline.

A product is the one artifact a serving process is allowed to read.  It carries
the normalized results of every stage that ran, the identity of every stage that
did not, the engine comparison, uncertainty, validation status, warnings and
limitations.  It contains no geospatial or external-model imports, so the API can
validate and serve one without pulling GDAL, rasterio, AvaFrame, or SNOWPACK into
the request path.

The rule the whole schema exists to enforce: a stage that could not run is
published as a named, reasoned absence.  It never becomes a zero, a default, or a
silently omitted field.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .engines.contracts import (
    NormalizedComparisonResult,
    NormalizedReleaseResult,
    NormalizedRunoutResult,
    NormalizedSnowStateResult,
    UncertaintyBound,
    ValidationStatus,
    canonical_json_bytes,
    validate_research_disclaimer,
)


PREDICTION_PRODUCT_SCHEMA_VERSION = "avycore-prediction-product-v2"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PipelineStage(StrEnum):
    MOUNTAIN_PACK = "mountain_pack"
    CONDITION_PACK = "condition_pack"
    SNOW_STATE_PACK = "snow_state_pack"
    RELEASE = "release"
    RUNOUT = "runout"
    COMPARISON = "comparison"


class StageStatus(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class StageRecord(StrictModel):
    """What one pipeline stage did, or why it produced nothing.

    ``unavailable`` and ``failed`` are distinct on purpose: the first means a
    required input or engine does not exist yet, the second means something that
    should have worked did not.  Both must name a reason a reader can act on.
    """

    stage: PipelineStage
    status: StageStatus
    engine_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    result_id: str | None = None
    artifact_root: str | None = None
    reason: str = Field(min_length=1)
    inputs_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def completion_requires_a_result(self) -> "StageRecord":
        if self.status == StageStatus.COMPLETED:
            if not self.result_id or not self.artifact_root:
                raise ValueError("A completed stage must publish a result identity and artifact root.")
        elif self.result_id is not None or self.artifact_root is not None:
            raise ValueError("Only a completed stage may publish a result identity or artifact root.")
        return self


class EngineRunRecord(StrictModel):
    """One normalized runout result, kept beside the engine that produced it."""

    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    engine_version: str = Field(min_length=1)
    license_spdx: str = Field(min_length=1)
    artifact_root: str = Field(min_length=1)
    result: NormalizedRunoutResult

    @model_validator(mode="after")
    def engine_matches_result(self) -> "EngineRunRecord":
        if self.result.provenance.engine_id != self.engine_id:
            raise ValueError("Engine run record names a different engine than its result.")
        return self


SweepBasis = Literal["source", "literature", "expert", "numerical"]
SweepTarget = Literal["engine_parameter", "release_input"]

# A stated basis has to be long enough to name where the span came from.  The
# bound is deliberately blunt: it is not a proof of provenance, it just makes an
# empty or placeholder justification fail at declaration time instead of being
# published as though a range had been reasoned about.
MINIMUM_STATED_BASIS_CHARACTERS = 40


class SweepSpecification(StrictModel):
    """A declared parameter span, validated before anything is run.

    Every field here is required.  A span that names no basis, or whose basis is
    an empty placeholder, cannot be constructed, so it can never reach a product:
    the failure happens where the sweep is declared rather than after members
    have been computed and an envelope drawn around them.
    """

    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parameter: str = Field(pattern=IDENTIFIER_PATTERN)
    unit: str = Field(min_length=1)
    varies: SweepTarget
    offsets: tuple[float, ...] = Field(min_length=3)
    basis: SweepBasis
    source: str = Field(min_length=MINIMUM_STATED_BASIS_CHARACTERS)

    @model_validator(mode="after")
    def bracketing_finite_offsets(self) -> "SweepSpecification":
        import math

        if not all(math.isfinite(offset) for offset in self.offsets):
            raise ValueError("Sweep offsets must be finite.")
        if len(set(self.offsets)) != len(self.offsets):
            raise ValueError("Sweep offsets must be distinct.")
        if self.offsets.count(0.0) != 1:
            raise ValueError("A sweep must contain exactly one zero offset: the central member.")
        if min(self.offsets) >= 0.0 or max(self.offsets) <= 0.0:
            raise ValueError("A sweep must bracket its central value from both sides.")
        if not self.source.strip():
            raise ValueError("A sweep must state the basis of its span.")
        return self


class UnsupportedSweep(StrictModel):
    """A span that was asked for and deliberately not run.

    Publishing this is the point.  A sweep silently omitted reads as though the
    parameter did not matter; a sweep published with an invented range reads as
    though its span were known.  Neither is true, so the absence is named,
    reasoned, and carries the exact action that would remove it.
    """

    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parameter: str = Field(pattern=IDENTIFIER_PATTERN)
    reason: str = Field(min_length=1)
    required_to_enable: str = Field(min_length=1)


class EnsembleMember(StrictModel):
    """One deterministic member of a bounded parameter sweep."""

    member_id: str = Field(pattern=r"^member-[0-9a-f]{16}$")
    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parameter: str = Field(pattern=IDENTIFIER_PATTERN)
    unit: str = Field(min_length=1)
    value: float
    is_central: bool
    result_id: str = Field(min_length=1)
    artifact_root: str = Field(min_length=1)
    runout_area_m2: float = Field(ge=0)
    aoi_status: Literal["complete_within_domain", "truncated_at_domain", "unknown"]

    @model_validator(mode="after")
    def finite_value(self) -> "EnsembleMember":
        import math

        if not math.isfinite(self.value):
            raise ValueError("An ensemble member parameter value must be finite.")
        return self


class EnsembleSummary(StrictModel):
    """A bounded sweep of one parameter, reported as sensitivity, never probability.

    ``member_frequency_note`` exists because the temptation to read "3 of 5 members
    reached this cell" as a probability is exactly what this contract refuses. The
    members are a deterministic sweep of an assumed range; their frequency carries
    no calibrated meaning.
    """

    engine_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parameter: str = Field(pattern=IDENTIFIER_PATTERN)
    unit: str = Field(min_length=1)
    # Which stage the span moved.  A friction constant and the release the engine
    # was handed are both "uncertainty", but they are not the same claim, and a
    # reader cannot tell them apart from a parameter name alone.
    varies: SweepTarget
    basis: SweepBasis
    source: str = Field(min_length=MINIMUM_STATED_BASIS_CHARACTERS)
    members: tuple[EnsembleMember, ...] = Field(min_length=2)
    central_runout_area_m2: float = Field(ge=0)
    minimum_runout_area_m2: float = Field(ge=0)
    maximum_runout_area_m2: float = Field(ge=0)
    envelope_artifact_root: str = Field(min_length=1)
    envelope_area_m2: float = Field(ge=0)
    interpretation: Literal["bounded_sensitivity_not_probability"] = (
        "bounded_sensitivity_not_probability"
    )
    member_frequency_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_members(self) -> "EnsembleSummary":
        if any(member.engine_id != self.engine_id for member in self.members):
            raise ValueError("Every ensemble member must belong to the summary's engine.")
        if any(member.parameter != self.parameter for member in self.members):
            raise ValueError("Every ensemble member must sweep the summary's parameter.")
        central = [member for member in self.members if member.is_central]
        if len(central) != 1:
            raise ValueError("A bounded sweep requires exactly one central member.")
        if len({member.member_id for member in self.members}) != len(self.members):
            raise ValueError("Ensemble member identities must be unique.")
        if len({member.value for member in self.members}) != len(self.members):
            raise ValueError("Ensemble member parameter values must be distinct.")
        areas = [member.runout_area_m2 for member in self.members]
        if self.minimum_runout_area_m2 != min(areas) or self.maximum_runout_area_m2 != max(areas):
            raise ValueError("Ensemble area bounds do not match its members.")
        if self.central_runout_area_m2 != central[0].runout_area_m2:
            raise ValueError("Central area does not match the central member.")
        # The union of every member must contain the central footprint; an
        # envelope smaller than its own central result would be incoherent.
        if self.envelope_area_m2 < self.maximum_runout_area_m2:
            raise ValueError("The outer envelope must contain every member footprint.")
        return self

    @property
    def area_spread_m2(self) -> float:
        return self.maximum_runout_area_m2 - self.minimum_runout_area_m2


class ProductProvenance(StrictModel):
    mountain_pack_sha256: str = Field(pattern=SHA256_PATTERN)
    bake_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    condition_pack_id: str | None = None
    snow_state_pack_id: str | None = None
    pipeline_version: str = Field(min_length=1)
    pipeline_sha256: str = Field(pattern=SHA256_PATTERN)
    configuration_sha256: str = Field(pattern=SHA256_PATTERN)
    seed: int | None = Field(default=None, ge=0)


class PredictionProduct(StrictModel):
    """Content-addressed output of one offline pipeline run."""

    schema_version: Literal["avycore-prediction-product-v2"]
    product_id: str = Field(pattern=r"^prediction-product-[0-9a-f]{64}$")
    site_id: str = Field(pattern=IDENTIFIER_PATTERN)
    disclaimer: str = Field(min_length=80)
    regime: str = Field(min_length=1)
    generated_from: Literal["synthetic_case", "mountain_pack"]
    provenance: ProductProvenance
    stages: tuple[StageRecord, ...] = Field(min_length=1)
    snow_state: NormalizedSnowStateResult | None = None
    release: NormalizedReleaseResult | None = None
    release_artifact_root: str | None = None
    runouts: tuple[EngineRunRecord, ...] = ()
    comparisons: tuple[NormalizedComparisonResult, ...] = ()
    comparison_artifact_roots: tuple[str, ...] = ()
    uncertainty: tuple[UncertaintyBound, ...] = ()
    ensembles: tuple[EnsembleSummary, ...] = ()
    unsupported_ensembles: tuple[UnsupportedSweep, ...] = ()
    dominant_uncertainty_contributor: str | None = None
    validation: ValidationStatus
    warnings: tuple[str, ...] = ()
    limitations: tuple[str, ...] = Field(min_length=1)

    @property
    def unavailable_stages(self) -> tuple[StageRecord, ...]:
        return tuple(
            record
            for record in self.stages
            if record.status in {StageStatus.UNAVAILABLE, StageStatus.FAILED}
        )

    def stage(self, stage: PipelineStage) -> StageRecord | None:
        for record in self.stages:
            if record.stage == stage:
                return record
        return None

    def engine_ids(self) -> tuple[str, ...]:
        return tuple(record.engine_id for record in self.runouts)

    @model_validator(mode="after")
    def consistent_and_content_addressed(self) -> "PredictionProduct":
        validate_research_disclaimer(self.disclaimer)
        stages = [record.stage for record in self.stages]
        if len(stages) != len(set(stages)):
            raise ValueError("Each pipeline stage may appear at most once.")
        if (self.release is None) != (self.release_artifact_root is None):
            raise ValueError("A release result and its artifact root must be published together.")
        if len(self.comparisons) != len(self.comparison_artifact_roots):
            raise ValueError("Every comparison requires exactly one artifact root.")

        engine_ids = [record.engine_id for record in self.runouts]
        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("Each runout engine may appear at most once in a product.")
        result_ids = {record.result.result_id for record in self.runouts}
        for comparison in self.comparisons:
            if comparison.site_id != self.site_id:
                raise ValueError("A comparison must belong to the product's site.")
            if not {comparison.left_result_id, comparison.right_result_id} <= result_ids:
                raise ValueError("A comparison references a runout result this product does not carry.")
        for record in self.runouts:
            if record.result.site_id != self.site_id:
                raise ValueError("A runout result must belong to the product's site.")
            if record.result.regime.value != self.regime:
                raise ValueError("A runout result must match the product's declared regime.")
        if self.release is not None and self.release.site_id != self.site_id:
            raise ValueError("The release result must belong to the product's site.")
        ensemble_keys = [(item.engine_id, item.parameter) for item in self.ensembles]
        if len(ensemble_keys) != len(set(ensemble_keys)):
            raise ValueError("Each engine/parameter sweep may appear at most once.")
        declined_keys = [(item.engine_id, item.parameter) for item in self.unsupported_ensembles]
        if len(declined_keys) != len(set(declined_keys)):
            raise ValueError("Each declined engine/parameter sweep may appear at most once.")
        # A span cannot be both published and declined; that would let a reader
        # take whichever of the two statements they preferred.
        if set(declined_keys) & set(ensemble_keys):
            raise ValueError("A sweep may not be published and declined at the same time.")
        for record in self.unsupported_ensembles:
            if record.engine_id not in set(engine_ids):
                raise ValueError("A declined sweep must name an engine this product ran.")
        if self.dominant_uncertainty_contributor is not None:
            if not self.ensembles:
                raise ValueError(
                    "A dominant uncertainty contributor requires at least one bounded sweep."
                )
            if self.dominant_uncertainty_contributor not in {
                f"{engine}:{parameter}" for engine, parameter in ensemble_keys
            }:
                raise ValueError(
                    "The dominant uncertainty contributor must name a published sweep."
                )
        elif self.ensembles:
            raise ValueError("A product with bounded sweeps must name its dominant contributor.")

        # A product that carries no result at all still has to say so through a
        # stage record, so an empty product can never read as a quiet success.
        if self.release is None and not self.runouts:
            if not self.unavailable_stages:
                raise ValueError(
                    "A product with no release or runout must record why its stages produced nothing."
                )
        payload = self.model_dump(mode="json", exclude={"product_id"})
        expected = f"prediction-product-{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"
        if self.product_id != expected:
            raise ValueError("Prediction product identity does not match its content.")
        return self


def build_prediction_product(content: dict[str, object]) -> PredictionProduct:
    """Build a product and derive its immutable content identity."""

    from pydantic_core import to_jsonable_python

    without_identity = dict(content)
    without_identity.pop("product_id", None)
    for name, field in PredictionProduct.model_fields.items():
        if name == "product_id" or name in without_identity or field.is_required():
            continue
        without_identity[name] = field.get_default(call_default_factory=True)
    identity = hashlib.sha256(
        canonical_json_bytes(to_jsonable_python(without_identity))
    ).hexdigest()
    return PredictionProduct.model_validate(
        {**without_identity, "product_id": f"prediction-product-{identity}"}
    )


__all__ = [
    "EngineRunRecord",
    "EnsembleMember",
    "EnsembleSummary",
    "MINIMUM_STATED_BASIS_CHARACTERS",
    "PREDICTION_PRODUCT_SCHEMA_VERSION",
    "PipelineStage",
    "PredictionProduct",
    "ProductProvenance",
    "StageRecord",
    "StageStatus",
    "SweepBasis",
    "SweepSpecification",
    "SweepTarget",
    "UnsupportedSweep",
    "build_prediction_product",
]
