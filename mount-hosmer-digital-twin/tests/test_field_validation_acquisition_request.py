from __future__ import annotations

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from avycore.validation import (
    OWNER_DELIVERY_SCHEMA_VERSION,
    FieldValidationOwnerDelivery,
    owner_delivery_cohort_gate,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/validation/build_field_validation_owner_request.py"
SPEC = importlib.util.spec_from_file_location("build_field_validation_owner_request", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

VALIDATOR_SCRIPT = ROOT / "scripts/validation/validate_field_validation_owner_delivery.py"
VALIDATOR_SPEC = importlib.util.spec_from_file_location(
    "validate_field_validation_owner_delivery", VALIDATOR_SCRIPT
)
assert VALIDATOR_SPEC is not None and VALIDATOR_SPEC.loader is not None
VALIDATOR_MODULE = importlib.util.module_from_spec(VALIDATOR_SPEC)
VALIDATOR_SPEC.loader.exec_module(VALIDATOR_MODULE)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _asset(label: str) -> dict:
    return {
        "relative_path": f"source/{label}.json",
        "bytes": len(label.encode("utf-8")),
        "sha256": _sha(label),
        "media_type": "application/json",
        "source_uri": f"https://example.invalid/{label}",
        "copyright_holder": "Synthetic pytest owner",
        "licence": "Synthetic test-only licence",
        "licence_uri": "https://example.invalid/licence",
        "permitted_use": "Contract verification only; not field evidence.",
        "redistribution_permitted": False,
        "licence_record_relative_path": "source/licence.json",
        "licence_record_sha256": _sha("licence"),
        "original_owner_delivered_bytes": True,
    }


def _crs(label: str) -> dict:
    return {
        "lineage_record": _asset(f"{label}-crs-lineage"),
        "original_crs": "EPSG:26911",
        "horizontal_datum": "Synthetic NAD83 test declaration",
        "horizontal_datum_realization": "Synthetic NAD83(CSRS) test realization",
        "vertical_datum": "Synthetic CGVD2013 test declaration",
        "vertical_datum_realization": "Synthetic CGVD2013 epoch 2010 realization",
        "vertical_coordinate_type": "orthometric_height",
        "horizontal_units": "metre",
        "vertical_units": "metre",
        "axis_order": "easting_northing",
        "coordinate_epoch": "Synthetic static test epoch",
        "transformation_to_delivery_crs": f"Identity operation for {label}",
        "delivery_crs": "EPSG:26911",
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
        "measurement_preserving_processing": (
            f"Synthetic identity processing declaration for {label}."
        ),
    }


def _uncertainty(units: str, label: str) -> dict:
    return {
        "magnitude": 2.0,
        "units": units,
        "confidence_level": 0.95,
        "method": f"Synthetic quantified uncertainty for {label}.",
    }


def _measurement(estimate: float, label: str) -> dict:
    return {
        "estimate": estimate,
        "lower": estimate * 0.8,
        "upper": estimate * 1.2,
        "confidence_level": 0.95,
        "method": f"Synthetic bounded measurement for {label}.",
    }


def _event(index: int) -> dict:
    label = f"event-{index}"
    crs = _crs(label)
    return {
        "event_id": label,
        "mountain_id": f"mountain-{index % 2}",
        "path_id": f"path-{index % 6}",
        "storm_cycle_id": f"storm-{index % 3}",
        "grouping_evidence": _asset(f"{label}-grouping"),
        "grouping_method": "Synthetic owner grouping record for contract testing.",
        "avalanche_regime": "dry_dense_slab",
        "regime_evidence": _asset(f"{label}-regime"),
        "regime_provenance": _provenance("regime"),
        "regime_classification_method": "Synthetic test classification only.",
        "event_start_utc": "2025-02-02T01:00:00Z",
        "event_end_utc": "2025-02-02T01:10:00Z",
        "event_time_confidence": "Synthetic exact-time contract fixture.",
        "event_time_evidence": _asset(f"{label}-time"),
        "event_time_provenance": _provenance("event time"),
        "release_geometry": {
            "geometry": _asset(f"{label}-release-geometry"),
            "crs_lineage": crs,
            "provenance": _provenance("release geometry"),
            "observation_time_utc": "2025-02-02T12:00:00Z",
            "observation_method": "Synthetic independent field mapping fixture.",
            "positional_uncertainty": _uncertainty("metre", "release geometry"),
            "independently_observed": True,
            "independent_of_model_output": True,
        },
        "release_thickness": {
            "measurement_m": _measurement(0.8, "release thickness"),
            "source": _asset(f"{label}-release-thickness"),
            "provenance": _provenance("release thickness"),
            "measurement_time_utc": "2025-02-02T12:00:00Z",
            "normal_to_slope": True,
            "event_specific": True,
        },
        "release_density": {
            "measurement_kg_m3": _measurement(220.0, "release density"),
            "source": _asset(f"{label}-release-density"),
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
            "vertical_uncertainty": _uncertainty("metre", "DEM vertical"),
            "horizontal_uncertainty": _uncertainty("metre", "DEM horizontal"),
            "valid_at_event": True,
            "validity_method": "Synthetic pre-event validity fixture.",
        },
        "terminal_observation": {
            "observation_type": "terminal_deposit_polygon",
            "geometry": _asset(f"{label}-terminal"),
            "crs_lineage": crs,
            "provenance": _provenance("terminal observation"),
            "observation_time_utc": "2025-02-02T12:00:00Z",
            "observation_method": "Synthetic ground-survey fixture.",
            "component_attribution_method": "Synthetic dense-flow attribution fixture.",
            "positional_uncertainty": _uncertainty("metre", "terminal geometry"),
            "component": "dense_flow",
            "terminal_feature_verified": True,
            "independent_of_model_output": True,
        },
        "survey_coverage": {
            "survey_coverage_geometry": _asset(f"{label}-coverage"),
            "detection_mask": _asset(f"{label}-detection-mask"),
            "crs_lineage": crs,
            "provenance": _provenance("survey coverage"),
            "mapping_time_utc": "2025-02-02T12:00:00Z",
            "coverage_positional_uncertainty": _uncertainty(
                "metre", "survey coverage"
            ),
            "declared_target": "terminal_dense_flow_deposit_or_endpoint",
            "complete_search_inside_coverage": True,
            "non_detection_inside_coverage": "observed_negative",
            "outside_coverage": "unknown",
            "masked_or_occluded_cells": "unknown",
            "detection_limit": "Synthetic explicit detection limit.",
            "detection_limit_units": "metre",
            "detection_confidence_level": 0.95,
            "completeness_method": "Synthetic complete-search contract fixture.",
        },
    }


def _delivery(delivery_id: str, events: list[dict]) -> dict:
    return {
        "schema_version": OWNER_DELIVERY_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "provider": "Synthetic pytest provider",
        "provider_contact": "nobody@example.invalid",
        "delivery_version": "synthetic-v1",
        "delivery_licence": "Synthetic test-only licence",
        "permitted_use": "Contract verification only; not field evidence.",
        "permission_or_licence_record": _asset("licence"),
        "grouping_status": "provider_proposed_requires_independent_review",
        "partition_status": "unassigned_until_complete_cohort_review",
        "predictions_generated": False,
        "holdout_targets_accessed": False,
        "events": events,
    }


def test_checked_in_request_rebuilds_and_binds_frozen_protocol() -> None:
    checked_in = json.loads(
        (ROOT / "validation-data/acquisition/field-validation-owner-request-v1.json").read_bytes()
    )
    rebuilt = MODULE.build_request()
    assert rebuilt == checked_in
    assert checked_in["status"] == "no_eligible_public_cohort_owner_request_ready"
    assert checked_in["current_evidence_state"]["eligible_events"] == 0
    assert checked_in["current_evidence_state"]["predictions_generated"] is False
    assert checked_in["preregistered_metrics_after_a_complete_frozen_split"][
        "execution_status"
    ] == "not_run_no_eligible_frozen_cohort"

    protocol_identity = checked_in["frozen_inputs"]["protocol"]
    protocol_path = ROOT / protocol_identity["path"]
    assert protocol_identity["sha256"] == hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    assert protocol_identity["bytes"] == protocol_path.stat().st_size
    for name in ("strict_public_funnel", "public_source_audit"):
        identity = checked_in["frozen_inputs"][name]
        path = ROOT / identity["path"]
        assert identity["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert identity["bytes"] == path.stat().st_size

    messages = ROOT / "validation-data/acquisition/field-validation-owner-requests-v1.md"
    assert MODULE.render_owner_requests(rebuilt) == messages.read_bytes()
    checked_schema = (
        ROOT
        / "validation-data/acquisition/field-validation-owner-delivery-v1.schema.json"
    ).read_bytes()
    assert MODULE._pretty_json(FieldValidationOwnerDelivery.model_json_schema()) == checked_schema
    seehore = next(
        route for route in checked_in["owner_routes"] if route["owner_id"] == "seehore-partners"
    )
    assert seehore["contacts"] == [
        "michele.freppaz@unito.it",
        "monica.barbero@polito.it",
    ]


def test_owner_delivery_contract_keeps_missingness_and_split_boundary_strict() -> None:
    payload = _delivery("synthetic-owner-delivery", [_event(index) for index in range(12)])
    delivery = FieldValidationOwnerDelivery.model_validate(payload)
    gate = owner_delivery_cohort_gate((delivery,))
    assert gate["passed_for_independent_scientific_review"] is True
    assert gate["required"] == {
        "events": 12,
        "independent_paths": 6,
        "mountains": 2,
        "storm_cycles": 3,
    }
    assert gate["partition_assigned"] is False
    assert gate["predictions_authorized"] is False

    missing_event = deepcopy(payload)
    del missing_event["events"][0]["release_density"]
    with pytest.raises(ValidationError, match="release_density"):
        FieldValidationOwnerDelivery.model_validate(missing_event)

    invented_friction = deepcopy(payload)
    invented_friction["events"][0]["friction"] = 0.3
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FieldValidationOwnerDelivery.model_validate(invented_friction)

    inferred_release = deepcopy(payload)
    inferred_release["events"][0]["release_geometry"]["provenance"][
        "inferred_value"
    ] = True
    with pytest.raises(ValidationError, match="inferred_value"):
        FieldValidationOwnerDelivery.model_validate(inferred_release)

    inferred_method = deepcopy(payload)
    inferred_method["events"][0]["release_geometry"]["observation_method"] = (
        "Release polygon inferred from model runout."
    )
    with pytest.raises(ValidationError, match="observation_method"):
        FieldValidationOwnerDelivery.model_validate(inferred_method)

    unbound_licence = deepcopy(payload)
    unbound_licence["events"][0]["release_density"]["source"][
        "licence_record_sha256"
    ] = _sha("different-licence")
    with pytest.raises(ValidationError, match="asset licence identity"):
        FieldValidationOwnerDelivery.model_validate(unbound_licence)


def test_owner_delivery_rejects_release_or_coverage_semantic_substitution() -> None:
    payload = _delivery("synthetic-owner-delivery", [_event(0)])

    vertical_thickness = deepcopy(payload)
    vertical_thickness["events"][0]["release_thickness"]["normal_to_slope"] = False
    with pytest.raises(ValidationError, match="normal_to_slope"):
        FieldValidationOwnerDelivery.model_validate(vertical_thickness)

    unknown_as_negative = deepcopy(payload)
    unknown_as_negative["events"][0]["survey_coverage"]["outside_coverage"] = (
        "observed_negative"
    )
    with pytest.raises(ValidationError, match="outside_coverage"):
        FieldValidationOwnerDelivery.model_validate(unknown_as_negative)

    local_time = deepcopy(payload)
    local_time["events"][0]["event_start_utc"] = "2025-02-01T18:00:00-07:00"
    with pytest.raises(ValidationError, match="event_start_utc must carry an explicit UTC offset"):
        FieldValidationOwnerDelivery.model_validate(local_time)

    absolute_asset = deepcopy(payload)
    absolute_asset["permission_or_licence_record"]["relative_path"] = (
        "C:\\outside\\licence.json"
    )
    with pytest.raises(ValidationError, match="relative_path"):
        FieldValidationOwnerDelivery.model_validate(absolute_asset)


def test_owner_delivery_cohort_gate_rejects_short_or_duplicate_cohorts() -> None:
    short = FieldValidationOwnerDelivery.model_validate(
        _delivery("short-owner-delivery", [_event(index) for index in range(11)])
    )
    assert owner_delivery_cohort_gate((short,))[
        "passed_for_independent_scientific_review"
    ] is False

    first = FieldValidationOwnerDelivery.model_validate(
        _delivery("first-owner-delivery", [_event(index) for index in range(6)])
    )
    second = FieldValidationOwnerDelivery.model_validate(
        _delivery("second-owner-delivery", [_event(index) for index in range(6)])
    )
    duplicate_gate = owner_delivery_cohort_gate((first, second))
    assert duplicate_gate["checks"]["unique_event_ids_across_deliveries"] is False
    assert duplicate_gate["duplicate_event_ids"] == [f"event-{index}" for index in range(6)]
    assert duplicate_gate["predictions_authorized"] is False


def test_owner_delivery_preflight_verifies_original_bytes_and_hashes(tmp_path: Path) -> None:
    payload = _delivery("synthetic-owner-delivery", [_event(0)])
    source_root = tmp_path / "source"
    source_root.mkdir()

    def write_assets(value: object) -> None:
        if isinstance(value, dict):
            if {"relative_path", "bytes", "sha256"}.issubset(value):
                relative_path = Path(str(value["relative_path"]))
                label = relative_path.stem
                (tmp_path / relative_path).write_bytes(label.encode("utf-8"))
            else:
                for item in value.values():
                    write_assets(item)
        elif isinstance(value, list):
            for item in value:
                write_assets(item)

    write_assets(payload)
    manifest_path = tmp_path / "delivery.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = VALIDATOR_MODULE.validate_manifests([manifest_path])
    assert report["all_files_verified"] is True
    assert report["all_licence_records_verified"] is True
    receipt = report["deliveries"][0]["verification_receipt"]
    assert receipt["manifest_sha256"] == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert receipt["event_ids"] == ["event-0"]
    assert receipt["all_source_file_identities_verified"] is True
    assert receipt["immutable_licence_record_verified"] is True
    assert report["ready_for_independent_scientific_review"] is False
    assert report["partition_assigned"] is False
    assert report["predictions_authorized"] is False

    preflight_path = tmp_path / "preflight.json"
    VALIDATOR_MODULE.write_preflight_report(preflight_path, report)
    assert json.loads(preflight_path.read_bytes()) == report
    VALIDATOR_MODULE.write_preflight_report(preflight_path, report)
    altered = {**report, "predictions_authorized": True}
    with pytest.raises(ValueError, match="Immutable preflight output conflict"):
        VALIDATOR_MODULE.write_preflight_report(preflight_path, altered)

    (source_root / "event-0-dem.json").write_bytes(b"corrupt")
    failed = VALIDATOR_MODULE.validate_manifests([manifest_path])
    assert failed["all_files_verified"] is False
    assert failed["deliveries"][0]["all_files_verified"] is False
