"""Advisory pre-intake triage for partial or malformed owner deliveries.

The owner request asks data owners to leave unavailable values missing rather
than infer, substitute, back-calculate, or model-derive them. An owner who
follows that instruction produces a delivery that
:class:`~avycore.validation.acquisition.FieldValidationOwnerEvent` rejects
outright, because every evidence block on that model is required. Without this
layer the only thing we could send back is one opaque validation dump for the
whole file, which tells the owner nothing about which event to fix.

This module reads such a delivery and reports, per event, which component
evidence profile it could support, exactly which fields are missing or unusable,
and the matching exclusion reason drawn from the existing
:data:`~avycore.validation.field_workflow.ExclusionReason` vocabulary.

It is strictly advisory and strictly additive:

* it never relaxes, replaces, or shadows the strict contract — it runs the real
  :class:`~avycore.validation.acquisition.FieldValidationOwnerDelivery`
  validator and reports what that validator said;
* it assigns no eligibility, trust, partition membership, or permission to
  predict, and a supported profile here is not an accepted component;
* eligibility still requires the two blinded human reviews, adjudication, and
  cohort/split/seal gates in :mod:`avycore.validation.field_workflow`.

Profiles R, C, and E follow ``docs/strict-field-validation-plan.md``. Profile E
additionally needs event forcing and snow-state evidence that the owner-delivery
schema does not carry at all, so a delivery alone can never establish it; that
is reported explicitly rather than silently treated as a shortfall of the owner.
"""

from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .acquisition import (
    _UNACCEPTABLE_REQUIRED_TEXT,
    FieldValidationOwnerDelivery,
    FieldValidationOwnerEvent,
    ImmutableAsset,
    owner_delivery_cohort_gate,
)
from .field_workflow import ExclusionReason


INTAKE_TRIAGE_SCHEMA_VERSION = "avycore-field-validation-owner-intake-triage-v1"

EvidenceProfile = Literal["R", "C", "E"]

TriageProblem = Literal[
    "missing_required_field",
    "placeholder_or_non_observed_value",
    "unexpected_field",
    "invalid_value",
    "cross_field_inconsistency",
    "event_is_not_an_object",
]

#: Metric classes an intact profile could admit. This names what the evidence
#: would allow, not what has been authorized.
AdmissibleMetricClass = Literal[
    "not_assessable",
    "positive_unlabelled_only",
    "includes_negative_dependent_metrics",
]

CLAIM_BOUNDARY = (
    "Triage is advisory pre-intake feedback only. It assigns no eligibility, no "
    "trust, no partition membership, and no permission to predict, and a "
    "supported profile is not an accepted component. Eligibility still requires "
    "the full strict contract, two independent blinded human reviews, "
    "adjudication of every event including exclusions, a frozen grouped split, "
    "and a sealed holdout."
)

# Evidence blocks, in the order a report should read them. Every field of the
# strict event model belongs to exactly one block; the drift check below fails
# loudly if the strict model gains a field this layer has not been taught about,
# so triage can never quietly under-report a new requirement.
_IDENTITY = "identity_and_grouping"
_REGIME = "avalanche_regime"
_TIME = "event_time"
_RELEASE_GEOMETRY = "release_geometry"
_RELEASE_THICKNESS = "release_thickness"
_RELEASE_DENSITY = "release_density"
_DEM = "event_surface_dem"
_TERMINAL = "terminal_observation"
_SURVEY = "survey_coverage"

EVIDENCE_BLOCKS: tuple[str, ...] = (
    _IDENTITY,
    _REGIME,
    _TIME,
    _RELEASE_GEOMETRY,
    _RELEASE_THICKNESS,
    _RELEASE_DENSITY,
    _DEM,
    _TERMINAL,
    _SURVEY,
)

_FIELD_TO_BLOCK: dict[str, str] = {
    "event_id": _IDENTITY,
    "mountain_id": _IDENTITY,
    "path_id": _IDENTITY,
    "storm_cycle_id": _IDENTITY,
    "grouping_evidence": _IDENTITY,
    "grouping_method": _IDENTITY,
    "avalanche_regime": _REGIME,
    "regime_evidence": _REGIME,
    "regime_provenance": _REGIME,
    "regime_classification_method": _REGIME,
    "event_start_utc": _TIME,
    "event_end_utc": _TIME,
    "event_time_confidence": _TIME,
    "event_time_evidence": _TIME,
    "event_time_provenance": _TIME,
    "release_geometry": _RELEASE_GEOMETRY,
    "release_thickness": _RELEASE_THICKNESS,
    "release_density": _RELEASE_DENSITY,
    "event_surface_dem": _DEM,
    "terminal_observation": _TERMINAL,
    "survey_coverage": _SURVEY,
}

_BLOCK_EXCLUSION_REASON: dict[str, ExclusionReason] = {
    _IDENTITY: "path_mountain_or_storm_grouping_unsupported",
    _REGIME: "avalanche_regime_not_dry_dense_slab",
    _TIME: "event_time_not_utc_or_not_independently_supported",
    _RELEASE_GEOMETRY: "release_geometry_not_independently_observed",
    _RELEASE_THICKNESS: "release_thickness_not_event_specific_normal_to_slope",
    _RELEASE_DENSITY: "release_density_not_event_specific_or_uncertain",
    _DEM: "event_surface_dem_lineage_or_uncertainty_incomplete",
    _TERMINAL: "terminal_dense_flow_observation_incomplete_or_unattributed",
    _SURVEY: "survey_coverage_or_detection_semantics_incomplete",
}

# Profile R scores release detection; Profile C scores runout conditioned on an
# observed release, so it needs the release mass evidence and the terminal
# observation but not the release-detection survey. Both need the grouping
# identifiers and the bounded event window, because the cohort is split by
# mountain/storm/path and every observation is bounded by the event interval.
_PROFILE_R_BLOCKS = (_IDENTITY, _REGIME, _TIME, _RELEASE_GEOMETRY, _DEM)
_PROFILE_C_BLOCKS = (
    _IDENTITY,
    _REGIME,
    _TIME,
    _RELEASE_GEOMETRY,
    _RELEASE_THICKNESS,
    _RELEASE_DENSITY,
    _DEM,
    _TERMINAL,
)
PROFILE_REQUIRED_BLOCKS: dict[str, tuple[str, ...]] = {
    "R": _PROFILE_R_BLOCKS,
    "C": _PROFILE_C_BLOCKS,
    "E": tuple(
        block for block in EVIDENCE_BLOCKS
        if block in set(_PROFILE_R_BLOCKS) | set(_PROFILE_C_BLOCKS)
    ),
}

#: Survey coverage is what separates positive/unlabelled reporting from metrics
#: that need credible negatives (precision, false alarm, specificity, IoU,
#: PR-AUC, Brier). Its absence narrows the admissible metrics; it never turns an
#: unknown cell into a negative.
PROFILE_NEGATIVE_METRIC_BLOCKS: dict[str, tuple[str, ...]] = {
    "R": (_SURVEY,),
    "C": (_SURVEY,),
    "E": (_SURVEY,),
}

#: Profile E requires evidence the owner-delivery schema has no field for, so a
#: delivery can never carry it. This is a scope statement about the schema, not
#: a defect in any owner's data.
PROFILE_E_EVIDENCE_OUTSIDE_THIS_SCHEMA: tuple[str, ...] = (
    "event forcing and snow-state inputs actually used by the release model, "
    "with units, UTC intervals, uncertainty, and spatial representativeness",
    "a frozen rule converting release output and uncertainty into solver "
    "initial conditions",
    "proof that no observed release geometry was supplied to the prediction path",
)

#: Label for a field the strict event model does not define at all. It is
#: deliberately not one of EVIDENCE_BLOCKS: an invented field is a protocol
#: violation the owner must delete, not evidence that has gone missing, so it
#: must not make a real evidence block look unusable.
_UNRECOGNIZED_FIELD = "unrecognized_field"

_ASSET_DESCRIPTOR_FIELDS = frozenset(ImmutableAsset.model_fields)


def _check_block_map_covers_strict_model() -> None:
    strict_fields = set(FieldValidationOwnerEvent.model_fields)
    mapped = set(_FIELD_TO_BLOCK)
    if strict_fields != mapped:
        missing = sorted(strict_fields - mapped)
        stale = sorted(mapped - strict_fields)
        raise RuntimeError(
            "avycore.validation.intake_triage has drifted from "
            "FieldValidationOwnerEvent and would under-report required evidence. "
            f"Unmapped strict fields: {missing}. Fields no longer on the strict "
            f"model: {stale}."
        )


_check_block_map_covers_strict_model()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TriageFinding(StrictModel):
    """One named defect, at the schema path the owner has to act on."""

    schema_path: str = Field(min_length=1)
    evidence_block: str = Field(min_length=1)
    problem: TriageProblem
    detail: str = Field(min_length=1)
    exclusion_reason: ExclusionReason


class ProfileAssessment(StrictModel):
    profile: EvidenceProfile
    supported: bool
    in_schema_requirements_met: bool
    required_evidence_blocks: tuple[str, ...]
    unusable_evidence_blocks: tuple[str, ...]
    missing_or_unusable_fields: tuple[str, ...]
    exclusion_reasons: tuple[ExclusionReason, ...]
    admissible_metric_class: AdmissibleMetricClass
    evidence_required_outside_this_schema: tuple[str, ...]


class EventTriage(StrictModel):
    event_index: int = Field(ge=0)
    event_id: str | None
    schema_path: str = Field(min_length=1)
    parsed_under_strict_contract: bool
    usable_evidence_blocks: tuple[str, ...]
    unusable_evidence_blocks: tuple[str, ...]
    supported_profiles: tuple[EvidenceProfile, ...]
    profiles: tuple[ProfileAssessment, ...]
    findings: tuple[TriageFinding, ...]
    exclusion_reasons: tuple[ExclusionReason, ...]


class DeliveryTriage(StrictModel):
    schema_version: Literal[INTAKE_TRIAGE_SCHEMA_VERSION]
    delivery_id: str | None
    parsed_under_strict_contract: bool
    delivery_findings: tuple[TriageFinding, ...]
    deferred_strict_checks: tuple[str, ...]
    events: tuple[EventTriage, ...]
    assigns_eligibility: Literal[False] = False
    assigns_trust: Literal[False] = False
    partition_assigned: Literal[False] = False
    predictions_authorized: Literal[False] = False
    claim_boundary: Literal[CLAIM_BOUNDARY] = CLAIM_BOUNDARY


class ProfileCohortRollup(StrictModel):
    profile: EvidenceProfile
    required: dict[str, int]
    observed: dict[str, int]
    checks: dict[str, bool]
    reaches_cohort_minimum: bool
    event_ids: tuple[str, ...]


class CohortTriage(StrictModel):
    schema_version: Literal[INTAKE_TRIAGE_SCHEMA_VERSION]
    deliveries: tuple[DeliveryTriage, ...]
    total_events: int = Field(ge=0)
    deliveries_parsed_under_strict_contract: int = Field(ge=0)
    duplicate_event_ids: tuple[str, ...]
    profile_rollups: tuple[ProfileCohortRollup, ...]
    strict_cohort_gate: dict[str, Any] | None
    assigns_eligibility: Literal[False] = False
    assigns_trust: Literal[False] = False
    partition_assigned: Literal[False] = False
    predictions_authorized: Literal[False] = False
    claim_boundary: Literal[CLAIM_BOUNDARY] = CLAIM_BOUNDARY


def _cohort_minimums() -> dict[str, int]:
    """Read the cohort minima off the strict gate itself.

    Calling the real gate with no deliveries keeps 12/6/2/3 defined in exactly
    one place, so triage cannot drift into a parallel, softer threshold.
    """

    return dict(owner_delivery_cohort_gate(())["required"])


COHORT_MINIMUMS = _cohort_minimums()


def _format_path(loc: Sequence[Any]) -> str:
    if not loc:
        return "(delivery)"
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered


def _is_placeholder(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return " ".join(value.strip().lower().split()) in _UNACCEPTABLE_REQUIRED_TEXT


def _classify_problem(error: dict[str, Any]) -> TriageProblem:
    error_type = error.get("type", "")
    if error_type == "missing":
        return "missing_required_field"
    if error_type == "extra_forbidden":
        return "unexpected_field"
    if _is_placeholder(error.get("input")):
        return "placeholder_or_non_observed_value"
    return "invalid_value"


def _exclusion_reason(
    block: str, tail: Sequence[Any], problem: TriageProblem
) -> ExclusionReason:
    """Pick the reason that most specifically names the defect.

    Defect kind wins over location: a placeholder inside the density block is
    reported as a substituted required value, and the block's own reason is
    still recovered at event level, so nothing is lost by the ordering.
    """

    if problem == "unexpected_field":
        return "other_protocol_exclusion"
    parts = [str(part) for part in tail if not isinstance(part, int)]
    if problem == "placeholder_or_non_observed_value" or any(
        part == "provenance" or part.endswith("_provenance") for part in parts
    ):
        return "required_value_missing_inferred_substituted_or_model_derived"
    if any("uncertainty" in part for part in parts):
        return "observation_uncertainty_incomplete"
    if any(part in _ASSET_DESCRIPTOR_FIELDS for part in parts):
        return "source_identity_or_licence_incomplete"
    return _BLOCK_EXCLUSION_REASON[block]


def _finding_sort_key(finding: TriageFinding) -> tuple[str, str, str]:
    return (finding.schema_path, finding.problem, finding.exclusion_reason)


def _event_finding(error: dict[str, Any]) -> TriageFinding:
    loc = tuple(error["loc"])
    # loc is ("events", index, <field>, ...); the field names the block.
    remainder = loc[2:]
    if remainder:
        problem: TriageProblem = _classify_problem(error)
        field = str(remainder[0])
        if field in _FIELD_TO_BLOCK:
            block = _FIELD_TO_BLOCK[field]
            reason = _exclusion_reason(block, remainder[1:], problem)
        else:
            block = _UNRECOGNIZED_FIELD
            reason = "other_protocol_exclusion"
    else:
        # A model-level validator on the event itself. Pydantic runs these only
        # once every field has validated, so this is a real cross-field
        # inconsistency. Every such validator on the strict event model checks
        # the event window against an observation time, so the finding lands on
        # the event-time block and conservatively blocks all three profiles.
        block = _TIME
        problem = "cross_field_inconsistency"
        reason = _BLOCK_EXCLUSION_REASON[_TIME]
    return TriageFinding(
        schema_path=_format_path(loc),
        evidence_block=block,
        problem=problem,
        detail=error["msg"],
        exclusion_reason=reason,
    )


def _delivery_finding(error: dict[str, Any]) -> TriageFinding:
    loc = tuple(error["loc"])
    problem = _classify_problem(error)
    parts = [str(part) for part in loc if not isinstance(part, int)]
    if problem == "unexpected_field":
        reason: ExclusionReason = "other_protocol_exclusion"
    elif problem == "placeholder_or_non_observed_value":
        reason = "required_value_missing_inferred_substituted_or_model_derived"
    elif any(part in _ASSET_DESCRIPTOR_FIELDS for part in parts) or (
        not loc and error.get("type") == "value_error"
    ):
        # An empty loc with a value_error is the delivery-level validator that
        # binds every asset to the one immutable licence record. An empty loc
        # with any other type means the payload is not a delivery object at all.
        reason = "source_identity_or_licence_incomplete"
    else:
        reason = "other_protocol_exclusion"
    return TriageFinding(
        schema_path=_format_path(loc),
        evidence_block="delivery",
        problem=problem,
        detail=error["msg"],
        exclusion_reason=reason,
    )


def _assess_profile(
    profile: EvidenceProfile,
    unusable_blocks: set[str],
    findings_by_block: dict[str, list[TriageFinding]],
    event_path: str,
) -> ProfileAssessment:
    required = PROFILE_REQUIRED_BLOCKS[profile]
    unusable_required = tuple(block for block in required if block in unusable_blocks)
    fields: list[str] = []
    reasons: set[ExclusionReason] = set()
    for block in unusable_required:
        block_findings = findings_by_block.get(block, ())
        for finding in block_findings:
            fields.append(finding.schema_path)
            reasons.add(finding.exclusion_reason)
        if not block_findings:
            # The block is unusable but nothing could be attributed inside it,
            # which happens when the whole event is unreadable. Point the owner
            # at the event rather than leaving the field list empty.
            fields.append(event_path)
        reasons.add(_BLOCK_EXCLUSION_REASON[block])

    negative_blocks = PROFILE_NEGATIVE_METRIC_BLOCKS[profile]
    in_schema_supported = not unusable_required
    # Profile E's remaining evidence lives outside this schema entirely, so an
    # owner delivery can complete its in-schema half and still not support E.
    supported = in_schema_supported and profile != "E"
    if not in_schema_supported:
        metric_class: AdmissibleMetricClass = "not_assessable"
    elif profile == "E":
        metric_class = "not_assessable"
    elif any(block in unusable_blocks for block in negative_blocks):
        metric_class = "positive_unlabelled_only"
    else:
        metric_class = "includes_negative_dependent_metrics"

    return ProfileAssessment(
        profile=profile,
        supported=supported,
        in_schema_requirements_met=in_schema_supported,
        required_evidence_blocks=required,
        unusable_evidence_blocks=unusable_required,
        missing_or_unusable_fields=tuple(sorted(set(fields))),
        exclusion_reasons=tuple(sorted(reasons)),
        admissible_metric_class=metric_class,
        evidence_required_outside_this_schema=(
            PROFILE_E_EVIDENCE_OUTSIDE_THIS_SCHEMA if profile == "E" else ()
        ),
    )


def _event_id_of(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("event_id")
        if isinstance(value, str):
            return value
    return None


def triage_owner_delivery(payload: Any) -> DeliveryTriage:
    """Report what a possibly partial owner delivery could support, per event.

    The strict contract is run unchanged; this only attributes what it rejected.
    """

    delivery_errors: list[dict[str, Any]] = []
    event_errors: dict[int, list[dict[str, Any]]] = {}
    parsed = False
    try:
        FieldValidationOwnerDelivery.model_validate(payload)
        parsed = True
    except ValidationError as exc:
        for error in exc.errors():
            loc = tuple(error["loc"])
            if len(loc) >= 2 and loc[0] == "events" and isinstance(loc[1], int):
                event_errors.setdefault(loc[1], []).append(error)
            elif loc == ("events",) and error.get("type") == "too_short":
                # Pydantic drops the events it could not build and then reports
                # the shortened tuple. That is an artifact of the per-event
                # failures already recorded, not a separate defect.
                continue
            else:
                delivery_errors.append(error)

    raw_events: list[Any] = []
    if isinstance(payload, dict) and isinstance(payload.get("events"), (list, tuple)):
        raw_events = list(payload["events"])

    events: list[EventTriage] = []
    for index, raw_event in enumerate(raw_events):
        events.append(_triage_event(index, raw_event, event_errors.get(index, [])))

    deferred: list[str] = []
    if not parsed:
        deferred.append(
            "Delivery-level cross-checks (unique event_id values, and every "
            "asset bound to the one immutable licence record) run only after "
            "every field-level defect above is resolved, so this report may not "
            "yet list them."
        )
        if any(not event.parsed_under_strict_contract for event in events):
            deferred.append(
                "Per-event consistency checks (event window ordering, "
                "pre-event DEM acquisition, and observations timed at or after "
                "the event start) run only after that event's fields validate."
            )

    return DeliveryTriage(
        schema_version=INTAKE_TRIAGE_SCHEMA_VERSION,
        delivery_id=(
            payload.get("delivery_id")
            if isinstance(payload, dict) and isinstance(payload.get("delivery_id"), str)
            else None
        ),
        parsed_under_strict_contract=parsed,
        delivery_findings=tuple(
            sorted((_delivery_finding(error) for error in delivery_errors), key=_finding_sort_key)
        ),
        deferred_strict_checks=tuple(deferred),
        events=tuple(events),
    )


def _triage_event(
    index: int, raw_event: Any, errors: list[dict[str, Any]]
) -> EventTriage:
    findings = [_event_finding(error) for error in errors]

    if not isinstance(raw_event, dict):
        # Nothing can be attributed to a block, so every block is unusable and
        # the event supports no profile at all.
        findings = [
            TriageFinding(
                schema_path=_format_path(("events", index)),
                evidence_block="delivery",
                problem="event_is_not_an_object",
                detail=(
                    errors[0]["msg"]
                    if errors
                    else "Event entry is not an object and carries no evidence."
                ),
                exclusion_reason="other_protocol_exclusion",
            )
        ]
        unusable = set(EVIDENCE_BLOCKS)
    else:
        unusable = {finding.evidence_block for finding in findings} & set(EVIDENCE_BLOCKS)

    findings.sort(key=_finding_sort_key)
    findings_by_block: dict[str, list[TriageFinding]] = {}
    for finding in findings:
        findings_by_block.setdefault(finding.evidence_block, []).append(finding)

    event_path = _format_path(("events", index))
    profiles = tuple(
        _assess_profile(profile, unusable, findings_by_block, event_path)
        for profile in ("R", "C", "E")
    )
    reasons: set[ExclusionReason] = {finding.exclusion_reason for finding in findings}
    reasons.update(_BLOCK_EXCLUSION_REASON[block] for block in unusable)

    return EventTriage(
        event_index=index,
        event_id=_event_id_of(raw_event),
        schema_path=_format_path(("events", index)),
        parsed_under_strict_contract=not errors,
        usable_evidence_blocks=tuple(
            block for block in EVIDENCE_BLOCKS if block not in unusable
        ),
        unusable_evidence_blocks=tuple(
            block for block in EVIDENCE_BLOCKS if block in unusable
        ),
        supported_profiles=tuple(
            profile.profile for profile in profiles if profile.supported
        ),
        profiles=profiles,
        findings=tuple(findings),
        exclusion_reasons=tuple(sorted(reasons)),
    )


def _rollup(
    profile: EvidenceProfile, events: Iterable[tuple[EventTriage, Any]]
) -> ProfileCohortRollup:
    qualifying = [
        (triage, raw)
        for triage, raw in events
        if any(item.profile == profile and item.supported for item in triage.profiles)
    ]
    def distinct(field: str) -> int:
        return len(
            {
                raw[field]
                for _, raw in qualifying
                if isinstance(raw.get(field), str)
            }
        )

    observed = {
        "events": len(qualifying),
        "independent_paths": distinct("path_id"),
        "mountains": distinct("mountain_id"),
        "storm_cycles": distinct("storm_cycle_id"),
    }
    checks = {
        key: observed[key] >= minimum for key, minimum in COHORT_MINIMUMS.items()
    }
    return ProfileCohortRollup(
        profile=profile,
        required=dict(COHORT_MINIMUMS),
        observed=observed,
        checks=checks,
        reaches_cohort_minimum=all(checks.values()),
        event_ids=tuple(
            sorted(
                triage.event_id
                for triage, _ in qualifying
                if triage.event_id is not None
            )
        ),
    )


def triage_owner_delivery_cohort(payloads: Sequence[Any]) -> CohortTriage:
    """Triage every delivery and roll the result up against the cohort minima.

    Reaching a minimum here means only that enough events carry the *shape* of
    the evidence to be worth reviewing. It is not eligibility and never
    authorizes a split or a prediction.
    """

    deliveries = tuple(triage_owner_delivery(payload) for payload in payloads)

    pairs: list[tuple[EventTriage, Any]] = []
    event_ids: list[str] = []
    for payload, delivery in zip(payloads, deliveries):
        raw_events = (
            list(payload["events"])
            if isinstance(payload, dict)
            and isinstance(payload.get("events"), (list, tuple))
            else []
        )
        for triage in delivery.events:
            raw = raw_events[triage.event_index]
            pairs.append((triage, raw if isinstance(raw, dict) else {}))
            if triage.event_id is not None:
                event_ids.append(triage.event_id)

    # Only deliveries that pass the strict contract may reach the real gate;
    # triage never feeds it a delivery the contract rejected.
    strict_deliveries = tuple(
        FieldValidationOwnerDelivery.model_validate(payload)
        for payload, delivery in zip(payloads, deliveries)
        if delivery.parsed_under_strict_contract
    )
    strict_gate = (
        owner_delivery_cohort_gate(strict_deliveries)
        if len(strict_deliveries) == len(deliveries) and deliveries
        else None
    )

    return CohortTriage(
        schema_version=INTAKE_TRIAGE_SCHEMA_VERSION,
        deliveries=deliveries,
        total_events=len(pairs),
        deliveries_parsed_under_strict_contract=len(strict_deliveries),
        duplicate_event_ids=tuple(
            sorted({value for value in event_ids if event_ids.count(value) > 1})
        ),
        profile_rollups=tuple(_rollup(profile, pairs) for profile in ("R", "C", "E")),
        strict_cohort_gate=strict_gate,
    )


__all__ = [
    "CLAIM_BOUNDARY",
    "COHORT_MINIMUMS",
    "EVIDENCE_BLOCKS",
    "INTAKE_TRIAGE_SCHEMA_VERSION",
    "PROFILE_E_EVIDENCE_OUTSIDE_THIS_SCHEMA",
    "PROFILE_NEGATIVE_METRIC_BLOCKS",
    "PROFILE_REQUIRED_BLOCKS",
    "CohortTriage",
    "DeliveryTriage",
    "EventTriage",
    "ProfileAssessment",
    "ProfileCohortRollup",
    "TriageFinding",
    "triage_owner_delivery",
    "triage_owner_delivery_cohort",
]
