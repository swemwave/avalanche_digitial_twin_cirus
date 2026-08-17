"""Blinded eligibility, cohort sealing, and leakage-safe field split workflow.

This module contains no hazard, runout, assessment, or metric imports. Prediction
and metric code can only be loaded through the guards after the complete human-
adjudicated cohort, frozen group split, and holdout seal have been verified.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .acquisition import OWNER_DELIVERY_SCHEMA_VERSION


ELIGIBILITY_REVIEW_SCHEMA_VERSION = "avycore-field-eligibility-review-v1"
ELIGIBILITY_DECISION_SCHEMA_VERSION = "avycore-field-eligibility-decision-v1"
GROUP_SPLIT_SCHEMA_VERSION = "avycore-field-group-split-preregistration-v1"
FROZEN_SPLIT_SCHEMA_VERSION = "avycore-field-group-split-v1"
ACCEPTED_COHORT_SCHEMA_VERSION = "avycore-field-accepted-cohort-v1"
HOLDOUT_SEAL_SCHEMA_VERSION = "avycore-field-holdout-observation-seal-v1"
FIELD_PREDICTION_MANIFEST_VERSION = "avycore-field-prediction-manifest-v1"
GROUP_SPLIT_ALGORITHM = "connected-group-components-balanced-dp-v1"
GROUP_SPLIT_SEED = 20260815

ExclusionReason = Literal[
    "source_identity_or_licence_incomplete",
    "event_time_not_utc_or_not_independently_supported",
    "event_surface_dem_lineage_or_uncertainty_incomplete",
    "release_geometry_not_independently_observed",
    "release_thickness_not_event_specific_normal_to_slope",
    "release_density_not_event_specific_or_uncertain",
    "terminal_dense_flow_observation_incomplete_or_unattributed",
    "observation_uncertainty_incomplete",
    "survey_coverage_or_detection_semantics_incomplete",
    "required_value_missing_inferred_substituted_or_model_derived",
    "path_mountain_or_storm_grouping_unsupported",
    "avalanche_regime_not_dry_dense_slab",
    "other_protocol_exclusion",
]


class WorkflowGateError(ValueError):
    """Raised before any protected prediction or metric code is loaded."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must carry an explicit UTC offset.")
    return value


class ArtifactIdentity(StrictModel):
    path: str = Field(min_length=1)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdjudicatedGrouping(StrictModel):
    mountain_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    path_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    storm_cycle_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,127}$")
    evidence_basis: str = Field(min_length=1)


class EligibilityCriteria(StrictModel):
    immutable_source_hashes_and_licences: bool
    utc_event_time_supported: bool
    event_surface_dem_crs_datum_lineage_complete: bool
    release_geometry_independently_observed: bool
    release_thickness_event_specific_normal_to_slope: bool
    release_density_event_specific_with_uncertainty: bool
    terminal_dense_flow_observation_component_attributed: bool
    observation_uncertainties_complete: bool
    survey_coverage_detection_masks_complete: bool
    no_missing_inferred_substituted_or_model_derived_required_values: bool
    path_mountain_storm_grouping_supported: bool
    dry_dense_slab_regime_supported: bool

    @property
    def all_pass(self) -> bool:
        return all(bool(value) for value in self.model_dump().values())


class EligibilityReview(StrictModel):
    schema_version: Literal[ELIGIBILITY_REVIEW_SCHEMA_VERSION]
    review_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    delivery_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_delivery_schema_version: Literal[OWNER_DELIVERY_SCHEMA_VERSION]
    reviewer_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_identity_verified: Literal[True]
    identity_verification_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_kind: Literal["human"]
    reviewer_independent: Literal[True]
    ai_generated_or_ai_assisted: Literal[False]
    blinded_to_model_predictions: Literal[True]
    blinded_to_other_reviews: Literal[True]
    holdout_assignment_unavailable: Literal[True]
    review_started_utc: datetime
    review_submitted_utc: datetime
    grouping: AdjudicatedGrouping
    criteria: EligibilityCriteria
    decision: Literal["eligible", "ineligible"]
    exclusion_reasons: tuple[ExclusionReason, ...]
    notes: str = Field(min_length=1)

    @field_validator("review_started_utc", "review_submitted_utc")
    @classmethod
    def require_utc(cls, value: datetime, info: Any) -> datetime:
        return _require_utc(value, info.field_name)

    @model_validator(mode="after")
    def validate_decision(self) -> "EligibilityReview":
        if self.review_submitted_utc < self.review_started_utc:
            raise ValueError("review_submitted_utc precedes review_started_utc.")
        if self.decision == "eligible":
            if not self.criteria.all_pass:
                raise ValueError("An eligible review requires every criterion to pass.")
            if self.exclusion_reasons:
                raise ValueError("An eligible review cannot contain exclusion reasons.")
        else:
            if self.criteria.all_pass:
                raise ValueError("An ineligible review must identify a failed criterion.")
            if not self.exclusion_reasons:
                raise ValueError("An ineligible review requires an exclusion reason.")
        if len(set(self.exclusion_reasons)) != len(self.exclusion_reasons):
            raise ValueError("Review exclusion reasons must be unique.")
        return self


class EligibilityConflictResolution(StrictModel):
    schema_version: Literal["avycore-field-eligibility-conflict-resolution-v1"]
    resolution_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    event_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
    delivery_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_review_sha256: tuple[str, ...] = Field(min_length=2)
    resolver_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_identity_verified: Literal[True]
    identity_verification_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolver_kind: Literal["human"]
    resolver_independent: Literal[True]
    ai_generated_or_ai_assisted: Literal[False]
    blinded_to_model_predictions: Literal[True]
    holdout_assignment_unavailable: Literal[True]
    resolved_at_utc: datetime
    grouping: AdjudicatedGrouping
    criteria: EligibilityCriteria
    decision: Literal["eligible", "ineligible"]
    exclusion_reasons: tuple[ExclusionReason, ...]
    rationale: str = Field(min_length=1)

    @field_validator("input_review_sha256")
    @classmethod
    def validate_review_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in values):
            raise ValueError("input_review_sha256 values must be lowercase SHA-256.")
        if len(set(values)) != len(values):
            raise ValueError("Conflict-resolution review hashes must be unique.")
        return values

    @field_validator("resolved_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "resolved_at_utc")

    @model_validator(mode="after")
    def validate_decision(self) -> "EligibilityConflictResolution":
        if self.decision == "eligible":
            if not self.criteria.all_pass or self.exclusion_reasons:
                raise ValueError(
                    "An eligible conflict resolution requires all criteria and no exclusions."
                )
        elif self.criteria.all_pass or not self.exclusion_reasons:
            raise ValueError(
                "An ineligible conflict resolution requires a failed criterion and exclusion."
            )
        return self


class EligibilityDecisionRecord(StrictModel):
    schema_version: Literal[ELIGIBILITY_DECISION_SCHEMA_VERSION]
    event_id: str
    delivery_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    independent_human_review_count: int = Field(ge=2)
    reviewer_identity_sha256: tuple[str, ...]
    review_sha256: tuple[str, ...]
    conflict_detected: bool
    conflict_resolution_sha256: str | None
    grouping: AdjudicatedGrouping
    criteria: EligibilityCriteria
    decision: Literal["eligible", "ineligible"]
    exclusion_reasons: tuple[ExclusionReason, ...]
    ai_counted_as_independent_review: Literal[False]
    predictions_imported_or_run: Literal[False]
    decision_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "EligibilityDecisionRecord":
        payload = self.model_dump(mode="json")
        supplied = payload.pop("decision_record_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Eligibility decision record SHA-256 does not match its content.")
        return self


def _review_sha256(review: EligibilityReview) -> str:
    return _sha256(review.model_dump(mode="json"))


def _review_signature(review: EligibilityReview) -> bytes:
    return _canonical_json(
        {
            "grouping": review.grouping.model_dump(mode="json"),
            "criteria": review.criteria.model_dump(mode="json"),
            "decision": review.decision,
            "exclusion_reasons": sorted(review.exclusion_reasons),
        }
    )


def adjudicate_event(
    reviews: tuple[EligibilityReview, ...],
    conflict_resolution: EligibilityConflictResolution | None = None,
) -> EligibilityDecisionRecord:
    """Create one auditable decision from at least two independent human reviews."""

    if len(reviews) < 2:
        raise WorkflowGateError("At least two independent human reviews are required.")
    event_ids = {review.event_id for review in reviews}
    delivery_hashes = {review.delivery_manifest_sha256 for review in reviews}
    protocol_hashes = {review.protocol_sha256 for review in reviews}
    audit_hashes = {review.source_audit_sha256 for review in reviews}
    reviewer_ids = [review.reviewer_identity_sha256 for review in reviews]
    review_ids = [review.review_id for review in reviews]
    if any(len(values) != 1 for values in (event_ids, delivery_hashes, protocol_hashes, audit_hashes)):
        raise WorkflowGateError("Reviews do not refer to one identical event and frozen evidence set.")
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise WorkflowGateError("Independent reviews require distinct human identity hashes.")
    if len(set(review_ids)) != len(review_ids):
        raise WorkflowGateError("Review IDs must be unique.")

    review_hashes = tuple(sorted(_review_sha256(review) for review in reviews))
    conflict = len({_review_signature(review) for review in reviews}) != 1
    resolution_hash: str | None = None
    if conflict:
        if conflict_resolution is None:
            raise WorkflowGateError(
                "Reviewer decisions conflict; a separate human conflict resolution is required."
            )
        if conflict_resolution.event_id != next(iter(event_ids)):
            raise WorkflowGateError("Conflict resolution refers to a different event.")
        if conflict_resolution.delivery_manifest_sha256 != next(iter(delivery_hashes)):
            raise WorkflowGateError("Conflict resolution refers to a different delivery.")
        if tuple(sorted(conflict_resolution.input_review_sha256)) != review_hashes:
            raise WorkflowGateError("Conflict resolution is not bound to every review hash.")
        if conflict_resolution.resolver_identity_sha256 in reviewer_ids:
            raise WorkflowGateError("Conflict resolver must be a third independent human.")
        grouping = conflict_resolution.grouping
        criteria = conflict_resolution.criteria
        decision = conflict_resolution.decision
        exclusions = tuple(sorted(conflict_resolution.exclusion_reasons))
        resolution_hash = _sha256(conflict_resolution.model_dump(mode="json"))
    else:
        if conflict_resolution is not None:
            raise WorkflowGateError("A conflict resolution was supplied when reviews agree.")
        first = reviews[0]
        grouping = first.grouping
        criteria = first.criteria
        decision = first.decision
        exclusions = tuple(sorted(first.exclusion_reasons))

    payload: dict[str, Any] = {
        "schema_version": ELIGIBILITY_DECISION_SCHEMA_VERSION,
        "event_id": next(iter(event_ids)),
        "delivery_manifest_sha256": next(iter(delivery_hashes)),
        "protocol_sha256": next(iter(protocol_hashes)),
        "source_audit_sha256": next(iter(audit_hashes)),
        "independent_human_review_count": len(reviews),
        "reviewer_identity_sha256": tuple(sorted(reviewer_ids)),
        "review_sha256": review_hashes,
        "conflict_detected": conflict,
        "conflict_resolution_sha256": resolution_hash,
        "grouping": grouping.model_dump(mode="json"),
        "criteria": criteria.model_dump(mode="json"),
        "decision": decision,
        "exclusion_reasons": exclusions,
        "ai_counted_as_independent_review": False,
        "predictions_imported_or_run": False,
    }
    payload["decision_record_sha256"] = _sha256(payload)
    return EligibilityDecisionRecord.model_validate(payload)


class DeliveryVerificationReceipt(StrictModel):
    delivery_id: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_bytes: int = Field(gt=0)
    event_ids: tuple[str, ...] = Field(min_length=1)
    all_source_file_identities_verified: Literal[True]
    immutable_licence_record_verified: Literal[True]
    preflight_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "DeliveryVerificationReceipt":
        if len(set(self.event_ids)) != len(self.event_ids):
            raise ValueError("Delivery receipt event IDs must be unique.")
        payload = self.model_dump(mode="json")
        supplied = payload.pop("preflight_record_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Delivery preflight receipt SHA-256 does not match its content.")
        return self


class AcceptedCohortEvent(StrictModel):
    event_id: str
    delivery_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grouping: AdjudicatedGrouping


class AcceptedCohort(StrictModel):
    schema_version: Literal[ACCEPTED_COHORT_SCHEMA_VERSION]
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_at_utc: datetime
    events: tuple[AcceptedCohortEvent, ...]
    excluded_decision_record_sha256: tuple[str, ...]
    observed: dict[str, int]
    required: dict[str, int]
    all_eligibility_decisions_auditable: Literal[True]
    all_source_files_and_licences_verified: Literal[True]
    cohort_gate_passed: Literal[True]
    cohort_sealed: Literal[True]
    predictions_generated: Literal[False]
    holdout_targets_accessed: Literal[False]
    cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("sealed_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "sealed_at_utc")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "AcceptedCohort":
        payload = self.model_dump(mode="json")
        supplied = payload.pop("cohort_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Accepted-cohort SHA-256 does not match its content.")
        return self


def accept_eligible_cohort(
    decisions: tuple[EligibilityDecisionRecord, ...],
    receipts: tuple[DeliveryVerificationReceipt, ...],
    *,
    sealed_at_utc: datetime,
) -> AcceptedCohort:
    """Seal the complete eligible cohort; fail without the full 12/6/2/3 gate."""

    if not decisions:
        raise WorkflowGateError("No eligibility decisions exist; cohort acceptance is blocked.")
    protocol_hashes = {decision.protocol_sha256 for decision in decisions}
    audit_hashes = {decision.source_audit_sha256 for decision in decisions}
    if len(protocol_hashes) != 1 or len(audit_hashes) != 1:
        raise WorkflowGateError("Eligibility decisions do not share one frozen protocol and audit.")
    receipt_by_manifest = {receipt.manifest_sha256: receipt for receipt in receipts}
    if len(receipt_by_manifest) != len(receipts):
        raise WorkflowGateError("Delivery verification receipts contain duplicate manifests.")
    all_decision_event_ids = [decision.event_id for decision in decisions]
    if len(set(all_decision_event_ids)) != len(all_decision_event_ids):
        raise WorkflowGateError("Eligibility decisions contain duplicate event IDs.")
    delivered_event_ids = [event_id for receipt in receipts for event_id in receipt.event_ids]
    if len(set(delivered_event_ids)) != len(delivered_event_ids):
        raise WorkflowGateError("Verified owner deliveries contain duplicate event IDs.")
    missing_decision_receipts = sorted(
        {
            decision.delivery_manifest_sha256
            for decision in decisions
            if decision.delivery_manifest_sha256 not in receipt_by_manifest
        }
    )
    if missing_decision_receipts:
        raise WorkflowGateError(
            "Eligibility decisions lack verified delivery receipts: "
            + ", ".join(missing_decision_receipts)
        )
    mismatched_decisions = sorted(
        decision.event_id
        for decision in decisions
        if decision.event_id
        not in receipt_by_manifest[decision.delivery_manifest_sha256].event_ids
    )
    if mismatched_decisions:
        raise WorkflowGateError(
            "Eligibility decisions are not bound to their delivery receipts: "
            + ", ".join(mismatched_decisions)
        )
    if set(all_decision_event_ids) != set(delivered_event_ids):
        missing = sorted(set(delivered_event_ids) - set(all_decision_event_ids))
        extra = sorted(set(all_decision_event_ids) - set(delivered_event_ids))
        raise WorkflowGateError(
            "Every event in the verified delivery cohort must be adjudicated before "
            f"cohort sealing; missing={missing}, extra={extra}."
        )
    eligible = [decision for decision in decisions if decision.decision == "eligible"]
    event_ids = [decision.event_id for decision in eligible]
    observed = {
        "events": len(eligible),
        "independent_paths": len({item.grouping.path_id for item in eligible}),
        "mountains": len({item.grouping.mountain_id for item in eligible}),
        "storm_cycles": len({item.grouping.storm_cycle_id for item in eligible}),
    }
    required = {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 3,
    }
    failed = [key for key, minimum in required.items() if observed[key] < minimum]
    if failed:
        detail = ", ".join(
            f"{key}={observed[key]}/{required[key]}" for key in failed
        )
        raise WorkflowGateError(f"Complete eligible cohort gate failed: {detail}.")
    events = tuple(
        AcceptedCohortEvent(
            event_id=decision.event_id,
            delivery_manifest_sha256=decision.delivery_manifest_sha256,
            decision_record_sha256=decision.decision_record_sha256,
            grouping=decision.grouping,
        )
        for decision in sorted(eligible, key=lambda item: item.event_id)
    )
    payload: dict[str, Any] = {
        "schema_version": ACCEPTED_COHORT_SCHEMA_VERSION,
        "protocol_sha256": next(iter(protocol_hashes)),
        "source_audit_sha256": next(iter(audit_hashes)),
        "sealed_at_utc": sealed_at_utc.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "events": [event.model_dump(mode="json") for event in events],
        "excluded_decision_record_sha256": tuple(
            sorted(
                decision.decision_record_sha256
                for decision in decisions
                if decision.decision == "ineligible"
            )
        ),
        "observed": observed,
        "required": required,
        "all_eligibility_decisions_auditable": True,
        "all_source_files_and_licences_verified": True,
        "cohort_gate_passed": True,
        "cohort_sealed": True,
        "predictions_generated": False,
        "holdout_targets_accessed": False,
    }
    payload["cohort_sha256"] = _sha256(payload)
    return AcceptedCohort.model_validate(payload)


class GroupwiseSplitPreregistration(StrictModel):
    schema_version: Literal[GROUP_SPLIT_SCHEMA_VERSION]
    status: Literal["frozen_before_predictions_no_real_event_assignments"]
    frozen_at_utc: datetime
    protocol: ArtifactIdentity
    source_audit: ArtifactIdentity
    strict_public_funnel: ArtifactIdentity
    implementation: ArtifactIdentity
    algorithm: Literal[GROUP_SPLIT_ALGORITHM]
    seed: Literal[GROUP_SPLIT_SEED]
    seed_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grouping_keys: tuple[
        Literal["path_id"], Literal["mountain_id"], Literal["storm_cycle_id"]
    ]
    target_holdout_fraction: Literal[0.5]
    minimum_calibration_events: Literal[6]
    minimum_holdout_events: Literal[6]
    minimum_paths_per_partition: Literal[3]
    minimum_mountains_per_partition: Literal[1]
    minimum_storm_cycles_per_partition: Literal[1]
    optimization_order: tuple[str, ...]
    group_leakage_permitted: Literal[False]
    event_assignments: tuple[str, ...]
    real_event_ids_exposed: Literal[False]
    predictions_generated: Literal[False]
    holdout_targets_accessed: Literal[False]
    normalized_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("frozen_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "frozen_at_utc")

    @model_validator(mode="after")
    def validate_preregistration(self) -> "GroupwiseSplitPreregistration":
        if self.event_assignments:
            raise ValueError("Preregistration must not assign or expose real events.")
        expected_seed_binding = _sha256(
            {
                "algorithm": self.algorithm,
                "seed": self.seed,
                "protocol_sha256": self.protocol.sha256,
                "source_audit_sha256": self.source_audit.sha256,
                "implementation_sha256": self.implementation.sha256,
            }
        )
        if self.seed_binding_sha256 != expected_seed_binding:
            raise ValueError("Split seed binding does not match its frozen inputs.")
        payload = self.model_dump(mode="json")
        supplied = payload.pop("normalized_artifact_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Split preregistration SHA-256 does not match its content.")
        return self


class PartitionAssignment(StrictModel):
    event_id: str
    partition: Literal["calibration", "holdout"]
    grouping: AdjudicatedGrouping
    connected_component_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FrozenGroupwiseSplit(StrictModel):
    schema_version: Literal[FROZEN_SPLIT_SCHEMA_VERSION]
    cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    procedure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignments: tuple[PartitionAssignment, ...]
    calibration_counts: dict[str, int]
    holdout_counts: dict[str, int]
    group_leakage_detected: Literal[False]
    cohort_gate_passed_before_assignment: Literal[True]
    holdout_observations_accessed: Literal[False]
    predictions_generated: Literal[False]
    split_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "FrozenGroupwiseSplit":
        payload = self.model_dump(mode="json")
        supplied = payload.pop("split_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Frozen split SHA-256 does not match its content.")
        return self


def _connected_components(
    events: tuple[AcceptedCohortEvent, ...],
) -> list[tuple[AcceptedCohortEvent, ...]]:
    parents = list(range(len(events)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    seen: dict[tuple[str, str], int] = {}
    for index, event in enumerate(events):
        keys = (
            ("path_id", event.grouping.path_id),
            ("mountain_id", event.grouping.mountain_id),
            ("storm_cycle_id", event.grouping.storm_cycle_id),
        )
        for key in keys:
            prior = seen.setdefault(key, index)
            union(index, prior)
    grouped: dict[int, list[AcceptedCohortEvent]] = {}
    for index, event in enumerate(events):
        grouped.setdefault(find(index), []).append(event)
    return [
        tuple(sorted(component, key=lambda item: item.event_id))
        for component in sorted(
            grouped.values(), key=lambda items: min(item.event_id for item in items)
        )
    ]


def _component_identity(component: tuple[AcceptedCohortEvent, ...]) -> str:
    return _sha256(
        {
            "events": [event.event_id for event in component],
            "paths": sorted({event.grouping.path_id for event in component}),
            "mountains": sorted({event.grouping.mountain_id for event in component}),
            "storm_cycles": sorted(
                {event.grouping.storm_cycle_id for event in component}
            ),
            "decision_records": sorted(
                event.decision_record_sha256 for event in component
            ),
        }
    )


def _partition_counts(events: Iterable[AcceptedCohortEvent]) -> dict[str, int]:
    materialized = tuple(events)
    return {
        "events": len(materialized),
        "independent_paths": len({event.grouping.path_id for event in materialized}),
        "mountains": len({event.grouping.mountain_id for event in materialized}),
        "storm_cycles": len(
            {event.grouping.storm_cycle_id for event in materialized}
        ),
    }


def freeze_groupwise_split(
    cohort: AcceptedCohort,
    procedure: GroupwiseSplitPreregistration,
) -> FrozenGroupwiseSplit:
    """Assign whole connected path/mountain/storm groups after cohort acceptance."""

    if cohort.protocol_sha256 != procedure.protocol.sha256:
        raise WorkflowGateError("Accepted cohort does not match the frozen split protocol.")
    if cohort.source_audit_sha256 != procedure.source_audit.sha256:
        raise WorkflowGateError("Accepted cohort does not match the frozen source audit.")
    components = _connected_components(cohort.events)
    if len(components) < 2:
        raise WorkflowGateError(
            "All eligible events form one connected group; a leakage-free split is impossible."
        )
    identities = [_component_identity(component) for component in components]
    component_counts = [_partition_counts(component) for component in components]
    total = _partition_counts(cohort.events)

    # One deterministic candidate subset is retained for every attainable count state.
    states: dict[tuple[int, int, int, int], tuple[int, ...]] = {(0, 0, 0, 0): ()}
    for index, counts in enumerate(component_counts):
        updated = dict(states)
        for state, selected in states.items():
            new_state = (
                state[0] + counts["events"],
                state[1] + counts["independent_paths"],
                state[2] + counts["mountains"],
                state[3] + counts["storm_cycles"],
            )
            candidate = (*selected, index)
            prior = updated.get(new_state)
            candidate_token = _sha256(
                {
                    "seed_binding_sha256": procedure.seed_binding_sha256,
                    "holdout_components": sorted(identities[item] for item in candidate),
                }
            )
            prior_token = (
                _sha256(
                    {
                        "seed_binding_sha256": procedure.seed_binding_sha256,
                        "holdout_components": sorted(
                            identities[item] for item in prior
                        ),
                    }
                )
                if prior is not None
                else None
            )
            if prior is None or candidate_token < prior_token:
                updated[new_state] = candidate
        states = updated

    candidates: list[tuple[tuple[Any, ...], tuple[int, ...]]] = []
    for state, selected in states.items():
        holdout = {
            "events": state[0],
            "independent_paths": state[1],
            "mountains": state[2],
            "storm_cycles": state[3],
        }
        calibration = {key: total[key] - holdout[key] for key in total}
        if holdout["events"] < procedure.minimum_holdout_events:
            continue
        if calibration["events"] < procedure.minimum_calibration_events:
            continue
        if min(holdout["independent_paths"], calibration["independent_paths"]) < procedure.minimum_paths_per_partition:
            continue
        if min(holdout["mountains"], calibration["mountains"]) < procedure.minimum_mountains_per_partition:
            continue
        if min(holdout["storm_cycles"], calibration["storm_cycles"]) < procedure.minimum_storm_cycles_per_partition:
            continue
        token = _sha256(
            {
                "seed_binding_sha256": procedure.seed_binding_sha256,
                "holdout_components": sorted(identities[item] for item in selected),
            }
        )
        score = (
            abs(holdout["events"] - total["events"] * procedure.target_holdout_fraction),
            -min(holdout["independent_paths"], calibration["independent_paths"]),
            -min(holdout["storm_cycles"], calibration["storm_cycles"]),
            -min(holdout["mountains"], calibration["mountains"]),
            token,
        )
        candidates.append((score, selected))
    if not candidates:
        raise WorkflowGateError(
            "No leakage-free calibration/holdout assignment meets the preregistered "
            "per-partition gates; acquire a more diverse eligible cohort."
        )

    holdout_component_indexes = set(min(candidates, key=lambda item: item[0])[1])
    assignments: list[PartitionAssignment] = []
    for index, component in enumerate(components):
        partition = "holdout" if index in holdout_component_indexes else "calibration"
        for event in component:
            assignments.append(
                PartitionAssignment(
                    event_id=event.event_id,
                    partition=partition,
                    grouping=event.grouping,
                    connected_component_sha256=identities[index],
                )
            )
    assignments.sort(key=lambda item: item.event_id)
    calibration_events = [
        event
        for event in cohort.events
        if next(item for item in assignments if item.event_id == event.event_id).partition
        == "calibration"
    ]
    holdout_events = [
        event
        for event in cohort.events
        if next(item for item in assignments if item.event_id == event.event_id).partition
        == "holdout"
    ]
    for attribute in ("path_id", "mountain_id", "storm_cycle_id"):
        calibration_groups = {
            getattr(event.grouping, attribute) for event in calibration_events
        }
        holdout_groups = {getattr(event.grouping, attribute) for event in holdout_events}
        if calibration_groups & holdout_groups:
            raise AssertionError(f"Internal split error: {attribute} leaked.")

    payload: dict[str, Any] = {
        "schema_version": FROZEN_SPLIT_SCHEMA_VERSION,
        "cohort_sha256": cohort.cohort_sha256,
        "procedure_sha256": procedure.normalized_artifact_sha256,
        "seed_binding_sha256": procedure.seed_binding_sha256,
        "assignments": [assignment.model_dump(mode="json") for assignment in assignments],
        "calibration_counts": _partition_counts(calibration_events),
        "holdout_counts": _partition_counts(holdout_events),
        "group_leakage_detected": False,
        "cohort_gate_passed_before_assignment": True,
        "holdout_observations_accessed": False,
        "predictions_generated": False,
    }
    payload["split_sha256"] = _sha256(payload)
    return FrozenGroupwiseSplit.model_validate(payload)


class HoldoutObservationSeal(StrictModel):
    schema_version: Literal[HOLDOUT_SEAL_SCHEMA_VERSION]
    cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    holdout_event_ids_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_vault_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sealed_before_calibration: Literal[True]
    holdout_targets_accessible_to_calibration: Literal[False]
    holdout_targets_accessed_during_calibration: Literal[False]
    seal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "HoldoutObservationSeal":
        payload = self.model_dump(mode="json")
        supplied = payload.pop("seal_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Holdout seal SHA-256 does not match its content.")
        return self


def seal_holdout_observations(
    cohort: AcceptedCohort,
    split: FrozenGroupwiseSplit,
    *,
    observation_vault_manifest_sha256: str,
) -> HoldoutObservationSeal:
    if split.cohort_sha256 != cohort.cohort_sha256:
        raise WorkflowGateError("Cannot seal holdout observations for a different cohort.")
    holdout_ids = tuple(
        sorted(
            assignment.event_id
            for assignment in split.assignments
            if assignment.partition == "holdout"
        )
    )
    payload: dict[str, Any] = {
        "schema_version": HOLDOUT_SEAL_SCHEMA_VERSION,
        "cohort_sha256": cohort.cohort_sha256,
        "split_sha256": split.split_sha256,
        "holdout_event_ids_sha256": _sha256(holdout_ids),
        "observation_vault_manifest_sha256": observation_vault_manifest_sha256,
        "sealed_before_calibration": True,
        "holdout_targets_accessible_to_calibration": False,
        "holdout_targets_accessed_during_calibration": False,
    }
    payload["seal_sha256"] = _sha256(payload)
    return HoldoutObservationSeal.model_validate(payload)


class FieldPredictionManifest(StrictModel):
    schema_version: Literal[FIELD_PREDICTION_MANIFEST_VERSION]
    cohort_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    split_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition: Literal["holdout"]
    prediction_artifact_sha256_by_event: dict[str, str]
    predictions_complete: Literal[True]
    frozen_before_holdout_observations_opened: Literal[True]
    prediction_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record_hash(self) -> "FieldPredictionManifest":
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in self.prediction_artifact_sha256_by_event.values()
        ):
            raise ValueError("Every prediction artifact requires a lowercase SHA-256.")
        payload = self.model_dump(mode="json")
        supplied = payload.pop("prediction_manifest_sha256")
        if _sha256(payload) != supplied:
            raise ValueError("Prediction-manifest SHA-256 does not match its content.")
        return self


def _verify_prediction_gates(
    cohort: AcceptedCohort | None,
    split: FrozenGroupwiseSplit | None,
    seal: HoldoutObservationSeal | None,
) -> None:
    if cohort is None or split is None or seal is None:
        raise WorkflowGateError(
            "Prediction code remains sealed until cohort acceptance, grouped split, and "
            "holdout observation seal all exist."
        )
    if not cohort.cohort_gate_passed or not cohort.cohort_sealed:
        raise WorkflowGateError("Prediction code refused: eligible cohort is incomplete or unsealed.")
    if split.cohort_sha256 != cohort.cohort_sha256:
        raise WorkflowGateError("Prediction code refused: split does not match cohort.")
    if seal.cohort_sha256 != cohort.cohort_sha256 or seal.split_sha256 != split.split_sha256:
        raise WorkflowGateError("Prediction code refused: holdout seal does not match split.")
    expected_holdout_ids = tuple(
        sorted(
            item.event_id for item in split.assignments if item.partition == "holdout"
        )
    )
    if seal.holdout_event_ids_sha256 != _sha256(expected_holdout_ids):
        raise WorkflowGateError("Prediction code refused: holdout seal event identity mismatch.")


T = TypeVar("T")


def guarded_prediction_loader(
    loader: Callable[[], T],
    *,
    cohort: AcceptedCohort | None,
    split: FrozenGroupwiseSplit | None,
    seal: HoldoutObservationSeal | None,
) -> T:
    """Verify all gates before invoking a loader that may import prediction code."""

    _verify_prediction_gates(cohort, split, seal)
    return loader()


def guarded_prediction_import(
    module_name: str,
    *,
    cohort: AcceptedCohort | None,
    split: FrozenGroupwiseSplit | None,
    seal: HoldoutObservationSeal | None,
) -> Any:
    return guarded_prediction_loader(
        lambda: importlib.import_module(module_name),
        cohort=cohort,
        split=split,
        seal=seal,
    )


def _verify_metrics_gates(
    cohort: AcceptedCohort | None,
    split: FrozenGroupwiseSplit | None,
    seal: HoldoutObservationSeal | None,
    predictions: FieldPredictionManifest | None,
) -> None:
    _verify_prediction_gates(cohort, split, seal)
    if predictions is None:
        raise WorkflowGateError("Field metrics refused: complete frozen holdout predictions are absent.")
    assert cohort is not None and split is not None
    if (
        predictions.cohort_sha256 != cohort.cohort_sha256
        or predictions.split_sha256 != split.split_sha256
    ):
        raise WorkflowGateError("Field metrics refused: prediction manifest identity mismatch.")
    expected = {
        item.event_id for item in split.assignments if item.partition == "holdout"
    }
    observed = set(predictions.prediction_artifact_sha256_by_event)
    if expected != observed:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise WorkflowGateError(
            f"Field metrics refused: incomplete holdout predictions; missing={missing}, extra={extra}."
        )


def guarded_metrics_loader(
    loader: Callable[[], T],
    *,
    cohort: AcceptedCohort | None,
    split: FrozenGroupwiseSplit | None,
    seal: HoldoutObservationSeal | None,
    predictions: FieldPredictionManifest | None,
) -> T:
    """Verify sealed cohort and complete frozen predictions before loading metrics."""

    _verify_metrics_gates(cohort, split, seal, predictions)
    return loader()


__all__ = [
    "ACCEPTED_COHORT_SCHEMA_VERSION",
    "ELIGIBILITY_DECISION_SCHEMA_VERSION",
    "ELIGIBILITY_REVIEW_SCHEMA_VERSION",
    "FIELD_PREDICTION_MANIFEST_VERSION",
    "FROZEN_SPLIT_SCHEMA_VERSION",
    "GROUP_SPLIT_ALGORITHM",
    "GROUP_SPLIT_SCHEMA_VERSION",
    "GROUP_SPLIT_SEED",
    "HOLDOUT_SEAL_SCHEMA_VERSION",
    "AcceptedCohort",
    "AcceptedCohortEvent",
    "AdjudicatedGrouping",
    "ArtifactIdentity",
    "DeliveryVerificationReceipt",
    "EligibilityConflictResolution",
    "EligibilityCriteria",
    "EligibilityDecisionRecord",
    "EligibilityReview",
    "FieldPredictionManifest",
    "FrozenGroupwiseSplit",
    "GroupwiseSplitPreregistration",
    "HoldoutObservationSeal",
    "PartitionAssignment",
    "WorkflowGateError",
    "accept_eligible_cohort",
    "adjudicate_event",
    "freeze_groupwise_split",
    "guarded_metrics_loader",
    "guarded_prediction_import",
    "guarded_prediction_loader",
    "seal_holdout_observations",
]
