"""Triage must describe a partial delivery without ever relaxing the strict gate."""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from avycore.validation import (
    FieldValidationOwnerDelivery,
    triage_owner_delivery,
    triage_owner_delivery_cohort,
)
from avycore.validation.intake_triage import (
    COHORT_MINIMUMS,
    EVIDENCE_BLOCKS,
    INTAKE_TRIAGE_SCHEMA_VERSION,
    PROFILE_REQUIRED_BLOCKS,
    _check_block_map_covers_strict_model,
)

from test_field_validation_acquisition_request import _delivery, _event


ROOT = Path(__file__).resolve().parents[1]
TRIAGE_SCRIPT = ROOT / "scripts/validation/triage_field_validation_owner_delivery.py"
TRIAGE_SPEC = importlib.util.spec_from_file_location(
    "triage_field_validation_owner_delivery", TRIAGE_SCRIPT
)
assert TRIAGE_SPEC is not None and TRIAGE_SPEC.loader is not None
TRIAGE_MODULE = importlib.util.module_from_spec(TRIAGE_SPEC)
TRIAGE_SPEC.loader.exec_module(TRIAGE_MODULE)


def _profile(event, name: str):
    return next(item for item in event.profiles if item.profile == name)


def test_complete_event_supports_release_and_conditional_runout_only() -> None:
    report = triage_owner_delivery(_delivery("complete", [_event(0)]))
    assert report.schema_version == INTAKE_TRIAGE_SCHEMA_VERSION
    assert report.parsed_under_strict_contract is True
    assert report.delivery_findings == ()
    assert report.deferred_strict_checks == ()

    event = report.events[0]
    assert event.event_id == "event-0"
    assert event.parsed_under_strict_contract is True
    assert event.findings == ()
    assert event.exclusion_reasons == ()
    assert event.unusable_evidence_blocks == ()
    assert event.usable_evidence_blocks == EVIDENCE_BLOCKS
    assert event.supported_profiles == ("R", "C")

    # Survey coverage is intact, so negative-dependent metrics are admissible.
    assert _profile(event, "R").admissible_metric_class == (
        "includes_negative_dependent_metrics"
    )
    assert _profile(event, "C").admissible_metric_class == (
        "includes_negative_dependent_metrics"
    )

    # Profile E needs forcing and snow-state evidence this schema cannot carry,
    # so a structurally perfect delivery still cannot establish it.
    end_to_end = _profile(event, "E")
    assert end_to_end.supported is False
    assert end_to_end.in_schema_requirements_met is True
    assert end_to_end.unusable_evidence_blocks == ()
    assert end_to_end.admissible_metric_class == "not_assessable"
    assert len(end_to_end.evidence_required_outside_this_schema) == 3

    # Triage assigns nothing.
    assert report.assigns_eligibility is False
    assert report.assigns_trust is False
    assert report.partition_assigned is False
    assert report.predictions_authorized is False


def test_event_missing_only_release_density_supports_profile_r_alone() -> None:
    payload = _delivery("partial", [_event(0)])
    del payload["events"][0]["release_density"]

    report = triage_owner_delivery(payload)
    assert report.parsed_under_strict_contract is False
    event = report.events[0]
    assert event.supported_profiles == ("R",)
    assert event.unusable_evidence_blocks == ("release_density",)
    assert event.exclusion_reasons == (
        "release_density_not_event_specific_or_uncertain",
    )

    (finding,) = event.findings
    assert finding.schema_path == "events[0].release_density"
    assert finding.evidence_block == "release_density"
    assert finding.problem == "missing_required_field"
    assert finding.exclusion_reason == "release_density_not_event_specific_or_uncertain"

    release = _profile(event, "R")
    assert release.supported is True
    assert release.missing_or_unusable_fields == ()

    conditional = _profile(event, "C")
    assert conditional.supported is False
    assert conditional.unusable_evidence_blocks == ("release_density",)
    assert conditional.missing_or_unusable_fields == ("events[0].release_density",)
    assert conditional.exclusion_reasons == (
        "release_density_not_event_specific_or_uncertain",
    )
    assert _profile(event, "E").supported is False


def test_missing_release_geometry_blocks_every_profile() -> None:
    payload = _delivery("no-release", [_event(0)])
    del payload["events"][0]["release_geometry"]

    event = triage_owner_delivery(payload).events[0]
    assert event.supported_profiles == ()
    assert event.exclusion_reasons == (
        "release_geometry_not_independently_observed",
    )
    for name in ("R", "C", "E"):
        assert _profile(event, name).supported is False


def test_placeholder_text_is_named_as_a_substituted_required_value() -> None:
    payload = _delivery("placeholder", [_event(0), _event(1)])
    payload["events"][0]["event_time_confidence"] = "missing"
    payload["events"][1]["release_geometry"]["observation_method"] = "n/a"

    report = triage_owner_delivery(payload)

    first = report.events[0]
    (finding,) = first.findings
    assert finding.schema_path == "events[0].event_time_confidence"
    assert finding.problem == "placeholder_or_non_observed_value"
    assert finding.exclusion_reason == (
        "required_value_missing_inferred_substituted_or_model_derived"
    )
    # The block reason survives alongside the defect-kind reason.
    assert set(first.exclusion_reasons) == {
        "event_time_not_utc_or_not_independently_supported",
        "required_value_missing_inferred_substituted_or_model_derived",
    }
    assert first.supported_profiles == ()

    second = report.events[1]
    (nested,) = second.findings
    assert nested.schema_path == "events[1].release_geometry.observation_method"
    assert nested.evidence_block == "release_geometry"
    assert nested.problem == "placeholder_or_non_observed_value"
    assert nested.exclusion_reason == (
        "required_value_missing_inferred_substituted_or_model_derived"
    )


def test_structurally_broken_event_supports_nothing_and_is_still_reported() -> None:
    payload = _delivery("broken", [_event(0), "not-an-object"])

    report = triage_owner_delivery(payload)
    assert report.parsed_under_strict_contract is False
    assert len(report.events) == 2
    # The intact sibling is still assessed rather than lost in one error dump.
    assert report.events[0].supported_profiles == ("R", "C")

    broken = report.events[1]
    assert broken.event_id is None
    assert broken.schema_path == "events[1]"
    assert broken.supported_profiles == ()
    assert broken.usable_evidence_blocks == ()
    assert broken.unusable_evidence_blocks == EVIDENCE_BLOCKS
    (finding,) = broken.findings
    assert finding.problem == "event_is_not_an_object"
    assert finding.exclusion_reason == "other_protocol_exclusion"
    # Nothing inside the event is attributable, so each profile points at the event.
    assert _profile(broken, "C").missing_or_unusable_fields == ("events[1]",)


def test_invented_field_and_cross_field_inconsistency_are_reported_separately() -> None:
    invented = _delivery("invented", [_event(0)])
    invented["events"][0]["friction"] = 0.3
    event = triage_owner_delivery(invented).events[0]
    (finding,) = event.findings
    assert finding.schema_path == "events[0].friction"
    assert finding.problem == "unexpected_field"
    assert finding.exclusion_reason == "other_protocol_exclusion"
    # An invented field is a protocol violation to delete, not missing evidence,
    # so it must not make a real evidence block look unusable.
    assert finding.evidence_block == "unrecognized_field"
    assert event.unusable_evidence_blocks == ()
    assert event.supported_profiles == ("R", "C")
    assert event.parsed_under_strict_contract is False

    reordered = _delivery("reordered", [_event(0)])
    reordered["events"][0]["event_end_utc"] = "2025-02-02T00:00:00Z"
    event = triage_owner_delivery(reordered).events[0]
    (inconsistency,) = event.findings
    assert inconsistency.schema_path == "events[0]"
    assert inconsistency.problem == "cross_field_inconsistency"
    assert inconsistency.evidence_block == "event_time"
    assert event.supported_profiles == ()


def test_uncertainty_and_licence_defects_reuse_their_own_exclusion_reasons() -> None:
    uncertain = _delivery("uncertain", [_event(0)])
    del uncertain["events"][0]["terminal_observation"]["positional_uncertainty"][
        "confidence_level"
    ]
    (finding,) = triage_owner_delivery(uncertain).events[0].findings
    assert finding.evidence_block == "terminal_observation"
    assert finding.exclusion_reason == "observation_uncertainty_incomplete"

    unlicensed = _delivery("unlicensed", [_event(0)])
    del unlicensed["events"][0]["release_thickness"]["source"]["licence_uri"]
    (licence,) = triage_owner_delivery(unlicensed).events[0].findings
    assert licence.evidence_block == "release_thickness"
    assert licence.exclusion_reason == "source_identity_or_licence_incomplete"


def test_delivery_below_the_cohort_minimum_is_reported_as_such() -> None:
    payload = _delivery("short", [_event(index) for index in range(5)])
    del payload["events"][0]["terminal_observation"]

    report = triage_owner_delivery_cohort([payload])
    assert report.total_events == 5
    assert report.deliveries_parsed_under_strict_contract == 0
    # A delivery the strict contract rejected is never handed to the real gate.
    assert report.strict_cohort_gate is None

    rollups = {item.profile: item for item in report.profile_rollups}
    assert rollups["R"].required == COHORT_MINIMUMS == {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 3,
    }
    assert rollups["R"].observed["events"] == 5
    assert rollups["R"].reaches_cohort_minimum is False
    assert rollups["R"].checks == {
        "events": False,
        "independent_paths": False,
        "mountains": True,
        "storm_cycles": True,
    }
    # The event missing its terminal observation drops out of Profile C only.
    assert rollups["C"].observed["events"] == 4
    assert rollups["C"].event_ids == ("event-1", "event-2", "event-3", "event-4")
    assert rollups["E"].observed["events"] == 0
    assert rollups["E"].reaches_cohort_minimum is False

    assert report.assigns_eligibility is False
    assert report.partition_assigned is False
    assert report.predictions_authorized is False


def test_complete_cohort_reaches_the_minimum_and_defers_to_the_real_gate() -> None:
    payload = _delivery("full", [_event(index) for index in range(12)])
    report = triage_owner_delivery_cohort([payload])

    rollups = {item.profile: item for item in report.profile_rollups}
    assert rollups["R"].reaches_cohort_minimum is True
    assert rollups["C"].reaches_cohort_minimum is True
    assert rollups["E"].reaches_cohort_minimum is False

    # Triage feeds the strict gate; it does not restate or replace its verdict.
    gate = report.strict_cohort_gate
    assert gate is not None
    assert gate["passed_for_independent_scientific_review"] is True
    assert gate["partition_assigned"] is False
    assert gate["predictions_authorized"] is False
    assert report.predictions_authorized is False


def test_duplicate_event_ids_are_reported_across_deliveries() -> None:
    first = _delivery("first", [_event(index) for index in range(6)])
    second = _delivery("second", [_event(index) for index in range(6)])
    report = triage_owner_delivery_cohort([first, second])
    assert report.duplicate_event_ids == tuple(f"event-{index}" for index in range(6))


def test_triage_never_weakens_the_strict_contract() -> None:
    payload = _delivery("partial", [_event(0)])
    del payload["events"][0]["release_density"]

    # Triage reporting Profile R support must not make the delivery ingestible.
    assert triage_owner_delivery(payload).events[0].supported_profiles == ("R",)
    with pytest.raises(ValidationError, match="release_density"):
        FieldValidationOwnerDelivery.model_validate(payload)

    # The unmodified strict model still rejects every documented substitution.
    inferred = _delivery("inferred", [_event(0)])
    inferred["events"][0]["release_geometry"]["provenance"]["inferred_value"] = True
    with pytest.raises(ValidationError, match="inferred_value"):
        FieldValidationOwnerDelivery.model_validate(inferred)
    (finding,) = triage_owner_delivery(inferred).events[0].findings
    assert finding.exclusion_reason == (
        "required_value_missing_inferred_substituted_or_model_derived"
    )


def test_profile_requirements_match_the_documented_plan() -> None:
    # Profile C is conditional on an observed release, so it needs the release
    # mass evidence and the terminal observation but not the release survey.
    assert set(PROFILE_REQUIRED_BLOCKS["C"]) - set(PROFILE_REQUIRED_BLOCKS["R"]) == {
        "release_thickness",
        "release_density",
        "terminal_observation",
    }
    # Profile E is every applicable requirement from R and C.
    assert set(PROFILE_REQUIRED_BLOCKS["E"]) == set(PROFILE_REQUIRED_BLOCKS["R"]) | set(
        PROFILE_REQUIRED_BLOCKS["C"]
    )
    # Survey coverage gates negative-dependent metrics rather than a profile.
    assert "survey_coverage" not in PROFILE_REQUIRED_BLOCKS["E"]


def test_survey_coverage_gap_narrows_metrics_to_positive_unlabelled() -> None:
    payload = _delivery("unlabelled", [_event(0)])
    del payload["events"][0]["survey_coverage"]

    event = triage_owner_delivery(payload).events[0]
    assert event.supported_profiles == ("R", "C")
    assert _profile(event, "R").admissible_metric_class == "positive_unlabelled_only"
    assert _profile(event, "C").admissible_metric_class == "positive_unlabelled_only"
    assert event.exclusion_reasons == (
        "survey_coverage_or_detection_semantics_incomplete",
    )


def test_block_map_drift_against_the_strict_model_fails_loudly() -> None:
    # The guard is what stops triage silently under-reporting a new requirement.
    from avycore.validation import intake_triage

    original = dict(intake_triage._FIELD_TO_BLOCK)
    try:
        intake_triage._FIELD_TO_BLOCK.pop("survey_coverage")
        with pytest.raises(RuntimeError, match="drifted from"):
            _check_block_map_covers_strict_model()
    finally:
        intake_triage._FIELD_TO_BLOCK.clear()
        intake_triage._FIELD_TO_BLOCK.update(original)
    _check_block_map_covers_strict_model()


def test_delivery_level_defects_are_reported_and_flag_deferred_checks() -> None:
    placeholder_provider = _delivery("no-provider", [_event(0)])
    placeholder_provider["provider"] = "unknown"
    report = triage_owner_delivery(placeholder_provider)
    (finding,) = report.delivery_findings
    assert finding.schema_path == "provider"
    assert finding.evidence_block == "delivery"
    assert finding.exclusion_reason == (
        "required_value_missing_inferred_substituted_or_model_derived"
    )
    # The event itself is untouched, so it is still assessed.
    assert report.events[0].supported_profiles == ("R", "C")
    assert len(report.deferred_strict_checks) == 1

    unbound_licence = _delivery("unbound", [_event(0)])
    unbound_licence["events"][0]["release_density"]["source"][
        "licence_record_sha256"
    ] = "0" * 64
    (licence,) = triage_owner_delivery(unbound_licence).delivery_findings
    assert licence.schema_path == "(delivery)"
    assert licence.exclusion_reason == "source_identity_or_licence_incomplete"

    # A payload that is not a delivery object at all must not be mislabelled as
    # a licence defect, and must not raise.
    not_a_delivery = triage_owner_delivery(["nope"])
    assert not_a_delivery.delivery_id is None
    assert not_a_delivery.events == ()
    (structural,) = not_a_delivery.delivery_findings
    assert structural.schema_path == "(delivery)"
    assert structural.exclusion_reason == "other_protocol_exclusion"


def test_report_is_deterministic_and_json_serializable() -> None:
    payload = _delivery("determinism", [_event(0), _event(1), "broken"])
    payload["events"][1]["event_time_confidence"] = "tbd"

    first = triage_owner_delivery_cohort([deepcopy(payload)]).model_dump(mode="json")
    second = triage_owner_delivery_cohort([deepcopy(payload)]).model_dump(mode="json")
    assert first == second
    assert json.loads(json.dumps(first, sort_keys=True)) == first


def test_cli_writes_an_immutable_report_and_a_client_request(tmp_path: Path) -> None:
    payload = _delivery("cli-delivery", [_event(0), _event(1)])
    del payload["events"][1]["release_density"]
    manifest = tmp_path / "delivery.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = TRIAGE_MODULE.triage_manifests([manifest])
    assert report.deliveries[0].delivery_id == "cli-delivery"
    assert report.deliveries[0].events[1].supported_profiles == ("R",)

    output = tmp_path / "triage.json"
    TRIAGE_MODULE.write_immutable(output, TRIAGE_MODULE._pretty_json(report.model_dump(mode="json")))
    assert json.loads(output.read_bytes()) == report.model_dump(mode="json")
    # Rewriting identical bytes is a no-op; differing bytes are a conflict.
    TRIAGE_MODULE.write_immutable(output, TRIAGE_MODULE._pretty_json(report.model_dump(mode="json")))
    with pytest.raises(ValueError, match="Immutable triage output conflict"):
        TRIAGE_MODULE.write_immutable(output, b"{}\n")

    request_path = tmp_path / "client-request.md"
    rendered = TRIAGE_MODULE.render_client_request(report)
    TRIAGE_MODULE.write_immutable(request_path, rendered)
    text = request_path.read_text(encoding="utf-8")
    assert "events[1].release_density" in text
    assert "Evidence profiles this event could support: R." in text
    assert "assigns no eligibility" in text
    assert "do not fill any gap below by inferring" in text
    # Deterministic bytes, so the artifact can be committed and re-sent.
    assert TRIAGE_MODULE.render_client_request(report) == rendered
