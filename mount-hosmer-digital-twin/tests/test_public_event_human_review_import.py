from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "import_public_event_human_reviews.py"
)
SPEC = importlib.util.spec_from_file_location("import_public_event_human_reviews", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _polygon(offset: float = 0.0) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [offset, 0.0],
                [10.0 + offset, 0.0],
                [10.0 + offset, 10.0],
                [offset, 10.0],
                [offset, 0.0],
            ]
        ],
    }


def _masks() -> dict[str, object]:
    masks = {
        name: {"status": "checked_absent", "geometries": [], "basis": "Inspected source pair."}
        for name in MODULE.MASK_NAMES
    }
    masks["survey_coverage"] = {
        "status": "mapped_present",
        "geometries": [_polygon()],
        "basis": "Manually mapped complete-search area for this component.",
    }
    return masks


def _submission(slot: str = "A", *, ai_only: bool = False) -> dict[str, object]:
    release = {
        "component_id": "release",
        "feature_role": "release",
        "observation_status": "observed",
        "source_scene_ids": ["scene-1"],
        "observation_method": "Independent human visual interpretation",
        "confidence": "medium",
        "confidence_basis": "Visible boundary in both source views.",
        "source_resolution_m": 10.0,
        "horizontal_uncertainty_m": 20.0,
        "horizontal_uncertainty_confidence_level": 0.95,
        "temporal_uncertainty_seconds": 3600.0,
        "resolution_uncertainty_statement": "Effective boundary resolution is one pixel.",
        "detection_limitations": [],
        "ambiguity_exclusions": [],
        "component_attribution": "Release crown and flank boundary only.",
        "geometry_crs": "EPSG:32632",
        "coordinate_order": "easting_northing",
        "normalization_method": "Identity on packet grid.",
        "geometry": _polygon(),
        "observation_masks": _masks(),
        "review_disposition": "accepted",
    }
    unsupported = [
        {
            "component_id": role,
            "feature_role": role,
            "observation_status": "not_supportable",
            "geometry": None,
        }
        for role in ("dense_flow_deposit", "terminal_dense_flow_toe")
    ]
    return {
        "schema": MODULE.SUBMISSION_SCHEMA,
        "packet_id": "blind-test",
        "packet_content_sha256": "a" * 64,
        "packet_archive_sha256": "b" * 64,
        "reviewer_slot": slot,
        "reviewer_identity": f"human-{slot}",
        "reviewer_organization": f"organization-{slot}",
        "reviewer_contact": f"reviewer-{slot}@example.org",
        "completed_at_utc": "2026-08-14T12:00:00Z",
        "human_completed": True,
        "independence_attestation": True,
        "blind_to_evaluated_outputs": True,
        "peer_submission_accessed": False,
        "ai_generated_only": ai_only,
        "event_grouping": {
            "path_id": "path-1",
            "mountain_id": "mountain-1",
            "storm_cycle_id": "storm-1",
            "identity_basis": "Independent interpretation of source location and event time.",
        },
        "release_density_transferability": {
            "disposition": "not_assessed",
            "basis": "No event-specific density evidence was supplied.",
            "transfer_uncertainty_statement": "Geographic transfer error is unknown.",
            "transfer_uncertainty_kg_m3": None,
        },
        "components": [release, *unsupported],
    }


def _validate(tmp_path: Path, submission: dict[str, object]):
    path = tmp_path / "submission.json"
    path.write_text(json.dumps(submission), encoding="utf-8")
    packet_record = {
        "packet_id": "blind-test",
        "packet_content_sha256": "a" * 64,
        "archive_sha256": "b" * 64,
    }
    packet = {
        "chip_grid": {"crs": "EPSG:32632"},
        "source_imagery": [{"scene_id": "scene-1"}],
    }
    return MODULE.validate_submission(
        submission,
        packet_record=packet_record,
        packet=packet,
        source_path=path,
    )


def test_complete_human_submission_validates_without_becoming_v3_crs_evidence(
    tmp_path: Path,
) -> None:
    normalized, failures = _validate(tmp_path, _submission())
    assert failures == []
    assert normalized is not None
    assert "EPSG:32632" not in MODULE.REVIEWED_PROJECTED_METRE_CRS


def test_ai_only_submission_cannot_satisfy_human_gate(tmp_path: Path) -> None:
    normalized, failures = _validate(tmp_path, _submission(ai_only=True))
    assert normalized is None
    assert "invalid_ai_generated_only" in failures


def test_missing_explicit_mask_is_rejected(tmp_path: Path) -> None:
    submission = _submission()
    submission["components"][0]["observation_masks"].pop("radar_shadow")
    normalized, failures = _validate(tmp_path, submission)
    assert normalized is None
    assert "incomplete_observation_masks" in failures


def test_component_agreement_uses_geometry_and_declared_uncertainty(tmp_path: Path) -> None:
    first, first_failures = _validate(tmp_path, _submission("A"))
    second_submission = _submission("B")
    second_submission["components"][0]["geometry"] = _polygon(1.0)
    second_path = tmp_path / "second"
    second_path.mkdir()
    second, second_failures = _validate(second_path, second_submission)
    assert first_failures == second_failures == []
    agreement = MODULE._component_agreement(first["components"][0], second["components"][0])
    assert agreement["accepted"] is True
    assert agreement["agreement"]["intersection_over_union"] >= 0.5
    assert agreement["accepted_component_evidence"]["geometry"] == _polygon()
    assert "parsed_geometry" not in agreement["accepted_component_evidence"]


def test_density_transfer_acceptance_requires_numeric_transfer_uncertainty(
    tmp_path: Path,
) -> None:
    submission = _submission()
    submission["release_density_transferability"]["disposition"] = "accepted"
    normalized, failures = _validate(tmp_path, submission)
    assert normalized is None
    assert "invalid_release_density_transfer_uncertainty_kg_m3" in failures

    submission["release_density_transferability"]["transfer_uncertainty_kg_m3"] = 35.0
    normalized, failures = _validate(tmp_path, submission)
    assert failures == []
    assert normalized is not None


def test_invalid_supplied_record_is_not_ignored_when_zero_submissions_validate() -> None:
    artifact = {
        "unknown_or_invalid_submission_paths": [],
        "candidates": [
            {
                "submission_files_received": 1,
                "valid_isolated_human_submissions": 0,
                "failures": ["review.json:invalid_submission_schema"],
            }
        ],
    }
    assert MODULE._supplied_review_failures_present(artifact) is True

    artifact["candidates"][0]["submission_files_received"] = 0
    artifact["candidates"][0]["failures"] = ["missing_independent_identity_verification"]
    assert MODULE._supplied_review_failures_present(artifact) is False
