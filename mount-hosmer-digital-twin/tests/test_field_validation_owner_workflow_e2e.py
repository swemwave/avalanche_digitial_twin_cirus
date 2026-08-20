from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from avycore.validation import (
    ELIGIBILITY_REVIEW_SCHEMA_VERSION,
    FIELD_PREDICTION_MANIFEST_VERSION,
    DeliveryVerificationReceipt,
    EligibilityConflictResolution,
    EligibilityCriteria,
    EligibilityReview,
    FieldPredictionManifest,
    FieldValidationOwnerDelivery,
    GroupwiseSplitPreregistration,
    WorkflowGateError,
    accept_eligible_cohort,
    adjudicate_event,
    freeze_groupwise_split,
    guarded_metrics_loader,
    guarded_prediction_import,
    guarded_prediction_loader,
    seal_holdout_observations,
)
from avycore.validation.acquisition import OWNER_DELIVERY_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SCRIPT = ROOT / "scripts/validation/validate_field_validation_owner_delivery.py"
SPEC = importlib.util.spec_from_file_location("field_delivery_validator_e2e", VALIDATOR_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

WORKFLOW_BUILDER_SCRIPT = (
    ROOT / "scripts/validation/build_field_validation_workflow_preregistration.py"
)
WORKFLOW_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "field_workflow_builder_e2e", WORKFLOW_BUILDER_SCRIPT
)
assert WORKFLOW_BUILDER_SPEC is not None and WORKFLOW_BUILDER_SPEC.loader is not None
WORKFLOW_BUILDER = importlib.util.module_from_spec(WORKFLOW_BUILDER_SPEC)
WORKFLOW_BUILDER_SPEC.loader.exec_module(WORKFLOW_BUILDER)

PROTOCOL_SHA256 = hashlib.sha256(
    (ROOT / "validation-data/experiments/public-data-field-validation-v2.json").read_bytes()
).hexdigest()
SOURCE_AUDIT_SHA256 = hashlib.sha256(
    (ROOT / "validation-data/candidates/public-validation-source-audit-v2.json").read_bytes()
).hexdigest()


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _canonical_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _asset(label: str) -> dict:
    return {
        "relative_path": f"source/{label}.json",
        "bytes": len(label.encode("utf-8")),
        "sha256": _sha(label),
        "media_type": "application/json",
        "source_uri": f"https://example.invalid/synthetic/{label}",
        "copyright_holder": "Synthetic pytest owner",
        "licence": "Synthetic test-only licence",
        "licence_uri": "https://example.invalid/synthetic/licence",
        "permitted_use": "Software testing only; never field evidence.",
        "redistribution_permitted": False,
        "licence_record_relative_path": "source/licence.json",
        "licence_record_sha256": _sha("licence"),
        "original_owner_delivered_bytes": True,
    }


def _provenance(label: str) -> dict:
    return {
        "evidence_origin": "direct_owner_observation",
        "original_measurement_available": True,
        "missing_value_supplied": False,
        "inferred_value": False,
        "substituted_value": False,
        "model_derived_value": False,
        "independent_of_prediction": True,
        "measurement_preserving_processing": f"Synthetic identity step for {label}.",
    }


def _uncertainty(label: str) -> dict:
    return {
        "magnitude": 1.0,
        "units": "metre",
        "confidence_level": 0.95,
        "method": f"Synthetic test-only uncertainty for {label}.",
    }


def _measurement(value: float, label: str) -> dict:
    return {
        "estimate": value,
        "lower": value * 0.8,
        "upper": value * 1.2,
        "confidence_level": 0.95,
        "method": f"Synthetic test-only bounded measurement for {label}.",
    }


def _crs(label: str) -> dict:
    return {
        "lineage_record": _asset(f"{label}-crs-lineage"),
        "original_crs": "EPSG:26911",
        "horizontal_datum": "Synthetic NAD83 declaration",
        "horizontal_datum_realization": "Synthetic NAD83(CSRS) realization",
        "vertical_datum": "Synthetic CGVD2013 declaration",
        "vertical_datum_realization": "Synthetic CGVD2013 epoch 2010 realization",
        "vertical_coordinate_type": "orthometric_height",
        "horizontal_units": "metre",
        "vertical_units": "metre",
        "axis_order": "easting_northing",
        "coordinate_epoch": "Synthetic static epoch 2010.0",
        "transformation_to_delivery_crs": f"Synthetic identity transform for {label}",
        "delivery_crs": "EPSG:26911",
    }


def _grouping(index: int) -> tuple[str, str, str]:
    component = "a" if index < 6 else "b"
    local = index if index < 6 else index - 6
    return (
        f"mountain-{component}",
        f"path-{component}-{local % 3}",
        f"storm-{component}-{local % 2}",
    )


def _event(index: int) -> dict:
    label = f"synthetic-event-{index:02d}"
    mountain, path, storm = _grouping(index)
    crs = _crs(label)
    return {
        "event_id": label,
        "mountain_id": mountain,
        "path_id": path,
        "storm_cycle_id": storm,
        "grouping_evidence": _asset(f"{label}-grouping"),
        "grouping_method": "Synthetic grouping for leakage tests only.",
        "avalanche_regime": "dry_dense_slab",
        "regime_evidence": _asset(f"{label}-regime"),
        "regime_provenance": _provenance("regime"),
        "regime_classification_method": "Synthetic dry dense-slab label for testing.",
        "event_start_utc": "2025-02-02T01:00:00Z",
        "event_end_utc": "2025-02-02T01:10:00Z",
        "event_time_confidence": "Synthetic exact-time test declaration.",
        "event_time_evidence": _asset(f"{label}-time"),
        "event_time_provenance": _provenance("event time"),
        "release_geometry": {
            "geometry": _asset(f"{label}-release"),
            "crs_lineage": crs,
            "provenance": _provenance("release geometry"),
            "observation_time_utc": "2025-02-02T12:00:00Z",
            "observation_method": "Synthetic direct survey fixture.",
            "positional_uncertainty": _uncertainty("release"),
            "independently_observed": True,
            "independent_of_model_output": True,
        },
        "release_thickness": {
            "measurement_m": _measurement(0.8, "thickness"),
            "source": _asset(f"{label}-thickness"),
            "provenance": _provenance("release thickness"),
            "measurement_time_utc": "2025-02-02T12:00:00Z",
            "normal_to_slope": True,
            "event_specific": True,
        },
        "release_density": {
            "measurement_kg_m3": _measurement(220.0, "density"),
            "source": _asset(f"{label}-density"),
            "provenance": _provenance("release density"),
            "measurement_time_utc": "2025-02-02T12:00:00Z",
            "event_specific": True,
        },
        "event_surface_dem": {
            "dem": _asset(f"{label}-dem"),
            "crs_lineage": crs,
            "provenance": _provenance("event surface DEM"),
            "acquisition_start_utc": "2025-02-01T10:00:00Z",
            "acquisition_end_utc": "2025-02-01T11:00:00Z",
            "surface_type": "pre_event_snow_surface",
            "vertical_uncertainty": _uncertainty("DEM vertical"),
            "horizontal_uncertainty": _uncertainty("DEM horizontal"),
            "valid_at_event": True,
            "validity_method": "Synthetic pre-event fixture.",
        },
        "terminal_observation": {
            "observation_type": "terminal_deposit_polygon",
            "geometry": _asset(f"{label}-terminal"),
            "crs_lineage": crs,
            "provenance": _provenance("terminal observation"),
            "observation_time_utc": "2025-02-02T12:00:00Z",
            "observation_method": "Synthetic direct terminal survey fixture.",
            "component_attribution_method": "Synthetic dense-flow attribution fixture.",
            "positional_uncertainty": _uncertainty("terminal"),
            "component": "dense_flow",
            "terminal_feature_verified": True,
            "independent_of_model_output": True,
        },
        "survey_coverage": {
            "survey_coverage_geometry": _asset(f"{label}-coverage"),
            "detection_mask": _asset(f"{label}-detection"),
            "crs_lineage": crs,
            "provenance": _provenance("coverage"),
            "mapping_time_utc": "2025-02-02T12:00:00Z",
            "coverage_positional_uncertainty": _uncertainty("coverage"),
            "declared_target": "terminal_dense_flow_deposit_or_endpoint",
            "complete_search_inside_coverage": True,
            "non_detection_inside_coverage": "observed_negative",
            "outside_coverage": "unknown",
            "masked_or_occluded_cells": "unknown",
            "detection_limit": "Synthetic one-metre detection limit.",
            "detection_limit_units": "metre",
            "detection_confidence_level": 0.95,
            "completeness_method": "Synthetic complete-search fixture.",
        },
    }


def _delivery() -> dict:
    return {
        "schema_version": OWNER_DELIVERY_SCHEMA_VERSION,
        "delivery_id": "synthetic-e2e-owner-delivery",
        "provider": "Synthetic pytest provider; never evidence",
        "provider_contact": "nobody@example.invalid",
        "delivery_version": "synthetic-test-only-v1",
        "delivery_licence": "Synthetic test-only licence",
        "permitted_use": "Software testing only; never field evidence.",
        "permission_or_licence_record": _asset("licence"),
        "grouping_status": "provider_proposed_requires_independent_review",
        "partition_status": "unassigned_until_complete_cohort_review",
        "predictions_generated": False,
        "holdout_targets_accessed": False,
        "events": [_event(index) for index in range(12)],
    }


def _write_assets(root: Path, value: object) -> None:
    if isinstance(value, dict):
        if {"relative_path", "bytes", "sha256"}.issubset(value):
            path = root / str(value["relative_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            label = path.stem
            path.write_bytes(label.encode("utf-8"))
        else:
            for item in value.values():
                _write_assets(root, item)
    elif isinstance(value, list):
        for item in value:
            _write_assets(root, item)


def _review(event: dict, reviewer: str, decision: str = "eligible") -> EligibilityReview:
    criteria = {name: True for name in EligibilityCriteria.model_fields}
    exclusions: list[str] = []
    if decision == "ineligible":
        criteria["observation_uncertainties_complete"] = False
        exclusions = ["observation_uncertainty_incomplete"]
    return EligibilityReview.model_validate(
        {
            "schema_version": ELIGIBILITY_REVIEW_SCHEMA_VERSION,
            "review_id": f"{event['event_id']}-{reviewer}",
            "event_id": event["event_id"],
            "delivery_manifest_sha256": event["manifest_sha256"],
            "protocol_sha256": PROTOCOL_SHA256,
            "source_audit_sha256": SOURCE_AUDIT_SHA256,
            "owner_delivery_schema_version": OWNER_DELIVERY_SCHEMA_VERSION,
            "reviewer_identity_sha256": _sha(reviewer),
            "human_identity_verified": True,
            "identity_verification_record_sha256": _sha(
                f"identity-record-{reviewer}"
            ),
            "reviewer_kind": "human",
            "reviewer_independent": True,
            "ai_generated_or_ai_assisted": False,
            "blinded_to_model_predictions": True,
            "blinded_to_other_reviews": True,
            "holdout_assignment_unavailable": True,
            "review_started_utc": "2026-08-15T01:00:00Z",
            "review_submitted_utc": "2026-08-15T02:00:00Z",
            "grouping": {
                "mountain_id": event["mountain_id"],
                "path_id": event["path_id"],
                "storm_cycle_id": event["storm_cycle_id"],
                "evidence_basis": "Synthetic grouping reviewed for tests only.",
            },
            "criteria": criteria,
            "decision": decision,
            "exclusion_reasons": exclusions,
            "notes": "Synthetic independent human-review fixture; never evidence.",
        }
    )


def _prediction_manifest(cohort, split, event_ids: list[str]) -> FieldPredictionManifest:
    payload = {
        "schema_version": FIELD_PREDICTION_MANIFEST_VERSION,
        "cohort_sha256": cohort.cohort_sha256,
        "split_sha256": split.split_sha256,
        "partition": "holdout",
        "prediction_artifact_sha256_by_event": {
            event_id: _sha(f"synthetic-prediction-{event_id}") for event_id in event_ids
        },
        "predictions_complete": True,
        "frozen_before_holdout_observations_opened": True,
    }
    payload["prediction_manifest_sha256"] = _canonical_sha(payload)
    return FieldPredictionManifest.model_validate(payload)


def test_synthetic_owner_delivery_to_sealed_group_split_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic values exercise controls only and are never registered as evidence."""

    payload = _delivery()
    FieldValidationOwnerDelivery.model_validate(payload)
    _write_assets(tmp_path, payload)
    manifest_path = tmp_path / "synthetic-owner-delivery.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    preflight = VALIDATOR.validate_manifests([manifest_path])
    assert preflight["all_files_verified"] is True
    assert preflight["all_licence_records_verified"] is True
    assert preflight["cohort"]["observed"] == {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 4,
    }
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    decisions = []
    for index, source_event in enumerate(payload["events"]):
        event = {**source_event, "manifest_sha256": manifest_sha256}
        first = _review(event, "reviewer-a")
        second = _review(
            event, "reviewer-b", "ineligible" if index == 0 else "eligible"
        )
        if index == 0:
            review_hashes = tuple(
                sorted(
                    _canonical_sha(review.model_dump(mode="json"))
                    for review in (first, second)
                )
            )
            resolution = EligibilityConflictResolution.model_validate(
                {
                    "schema_version": "avycore-field-eligibility-conflict-resolution-v1",
                    "resolution_id": "synthetic-event-00-resolution",
                    "event_id": event["event_id"],
                    "delivery_manifest_sha256": manifest_sha256,
                    "input_review_sha256": review_hashes,
                    "resolver_identity_sha256": _sha("reviewer-c"),
                    "human_identity_verified": True,
                    "identity_verification_record_sha256": _sha(
                        "identity-record-reviewer-c"
                    ),
                    "resolver_kind": "human",
                    "resolver_independent": True,
                    "ai_generated_or_ai_assisted": False,
                    "blinded_to_model_predictions": True,
                    "holdout_assignment_unavailable": True,
                    "resolved_at_utc": "2026-08-15T03:00:00Z",
                    "grouping": first.grouping.model_dump(mode="json"),
                    "criteria": {name: True for name in EligibilityCriteria.model_fields},
                    "decision": "eligible",
                    "exclusion_reasons": [],
                    "rationale": "Synthetic third-human conflict resolution for testing.",
                }
            )
            decision = adjudicate_event((first, second), resolution)
            assert decision.conflict_detected is True
        else:
            decision = adjudicate_event((first, second))
            assert decision.conflict_detected is False
        decisions.append(decision)

    invalid_ai_review = deepcopy(first.model_dump(mode="json"))
    invalid_ai_review["ai_generated_or_ai_assisted"] = True
    with pytest.raises(ValidationError, match="ai_generated_or_ai_assisted"):
        EligibilityReview.model_validate(invalid_ai_review)

    receipt = DeliveryVerificationReceipt.model_validate(
        preflight["deliveries"][0]["verification_receipt"]
    )
    with pytest.raises(WorkflowGateError, match="must be adjudicated"):
        accept_eligible_cohort(
            tuple(decisions[:-1]),
            (receipt,),
            sealed_at_utc=datetime(2026, 8, 15, 3, 30, tzinfo=timezone.utc),
        )
    cohort = accept_eligible_cohort(
        tuple(decisions),
        (receipt,),
        sealed_at_utc=datetime(2026, 8, 15, 3, 30, tzinfo=timezone.utc),
    )
    assert cohort.observed == {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 4,
    }
    procedure = GroupwiseSplitPreregistration.model_validate_json(
        (
            ROOT
            / "validation-data/acquisition/field-validation-group-split-preregistration-v1.json"
        ).read_bytes()
    )
    split = freeze_groupwise_split(cohort, procedure)
    assert split == freeze_groupwise_split(cohort, procedure)
    assert split.calibration_counts == {
        "events": 6,
        "independent_paths": 3,
        "mountains": 1,
        "storm_cycles": 2,
    }
    assert split.holdout_counts == split.calibration_counts
    for field in ("path_id", "mountain_id", "storm_cycle_id"):
        calibration = {
            getattr(item.grouping, field)
            for item in split.assignments
            if item.partition == "calibration"
        }
        holdout = {
            getattr(item.grouping, field)
            for item in split.assignments
            if item.partition == "holdout"
        }
        assert calibration.isdisjoint(holdout)

    import_calls: list[str] = []

    def fake_import(name: str) -> str:
        import_calls.append(name)
        return "synthetic-loader-only"

    monkeypatch.setattr("avycore.validation.field_workflow.importlib.import_module", fake_import)
    with pytest.raises(WorkflowGateError, match="Prediction code remains sealed"):
        guarded_prediction_import(
            "synthetic.prediction.module", cohort=None, split=None, seal=None
        )
    assert import_calls == []
    with pytest.raises(WorkflowGateError, match="Prediction code remains sealed"):
        guarded_prediction_loader(
            lambda: "must-not-run", cohort=cohort, split=split, seal=None
        )

    seal = seal_holdout_observations(
        cohort,
        split,
        observation_vault_manifest_sha256=_sha("synthetic-holdout-vault"),
    )
    assert guarded_prediction_import(
        "synthetic.prediction.module", cohort=cohort, split=split, seal=seal
    ) == "synthetic-loader-only"
    assert import_calls == ["synthetic.prediction.module"]

    holdout_ids = sorted(
        item.event_id for item in split.assignments if item.partition == "holdout"
    )
    incomplete = _prediction_manifest(cohort, split, holdout_ids[:-1])
    metric_calls: list[str] = []
    with pytest.raises(WorkflowGateError, match="incomplete holdout predictions"):
        guarded_metrics_loader(
            lambda: metric_calls.append("ran"),
            cohort=cohort,
            split=split,
            seal=seal,
            predictions=incomplete,
        )
    with pytest.raises(WorkflowGateError, match="Prediction code remains sealed"):
        guarded_metrics_loader(
            lambda: metric_calls.append("ran"),
            cohort=cohort,
            split=split,
            seal=None,
            predictions=None,
        )
    assert metric_calls == []

    complete = _prediction_manifest(cohort, split, holdout_ids)
    assert guarded_metrics_loader(
        lambda: "synthetic-metric-loader-only",
        cohort=cohort,
        split=split,
        seal=seal,
        predictions=complete,
    ) == "synthetic-metric-loader-only"


def test_incomplete_synthetic_cohort_cannot_reach_prediction_import() -> None:
    loader_calls: list[str] = []
    with pytest.raises(WorkflowGateError, match="No eligibility decisions"):
        accept_eligible_cohort(
            (), (), sealed_at_utc=datetime(2026, 8, 15, tzinfo=timezone.utc)
        )
    with pytest.raises(WorkflowGateError, match="Prediction code remains sealed"):
        guarded_prediction_loader(
            lambda: loader_calls.append("ran"), cohort=None, split=None, seal=None
        )
    assert loader_calls == []


def test_workflow_preregistration_and_integrity_artifacts_rebuild_exactly() -> None:
    base = Path("validation-data/acquisition")
    artifacts = {
        base / "field-validation-group-split-preregistration-v1.json": (
            WORKFLOW_BUILDER._pretty_json(WORKFLOW_BUILDER.build_preregistration())
        ),
        base / "field-validation-eligibility-review-v1.schema.json": (
            WORKFLOW_BUILDER._pretty_json(EligibilityReview.model_json_schema())
        ),
        base / "field-validation-eligibility-conflict-v1.schema.json": (
            WORKFLOW_BUILDER._pretty_json(
                EligibilityConflictResolution.model_json_schema()
            )
        ),
        base / "field-validation-eligibility-decision-v1.schema.json": (
            WORKFLOW_BUILDER._pretty_json(
                WORKFLOW_BUILDER.EligibilityDecisionRecord.model_json_schema()
            )
        ),
    }
    for path, rebuilt in artifacts.items():
        assert rebuilt == (ROOT / path).read_bytes()

    checked_integrity = json.loads(
        (
            ROOT
            / "validation-data/acquisition/field-validation-acquisition-integrity-v1.json"
        ).read_bytes()
    )
    assert WORKFLOW_BUILDER.build_integrity_manifest(artifacts) == checked_integrity
    assert checked_integrity["current_counts"] == {
        "eligible_events": 0,
        "eligible_mountains": 0,
        "eligible_paths": 0,
        "eligible_storm_cycles": 0,
    }
    assert checked_integrity["prediction_authorized"] is False
    assert checked_integrity["metrics_authorized"] is False
