from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "build_public_event_annotation_gate.py"
)
SPEC = importlib.util.spec_from_file_location("build_public_event_annotation_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _complete_annotation() -> dict[str, object]:
    return {
        "component_id": "deposit-1",
        "feature_role": "dense_flow_deposit",
        "source_scene_ids": ["scene-a"],
        "observation_method": "human visual interpretation",
        "confidence": "medium",
        "source_resolution_m": 10.0,
        "horizontal_uncertainty_m": 20.0,
        "temporal_uncertainty": "event window",
        "detection_limitations": ["forest occlusion"],
        "ambiguity_exclusion_reason": [],
        "annotator_identity": "reviewer-a",
        "annotation_time_utc": "2026-08-13T01:00:00Z",
        "pass_1_geometry": {"type": "Polygon", "coordinates": []},
        "pass_2_geometry": {"type": "Polygon", "coordinates": []},
        "repeatability_comparison": {"boundary_distance_m": 12.0},
        "reviewer_identity": "reviewer-b",
        "review_time_utc": "2026-08-13T02:00:00Z",
        "review_disposition": "accepted",
        "ai_generated_only": False,
    }


def test_complete_independently_reviewed_annotation_passes() -> None:
    accepted, failures = MODULE.annotation_is_reviewed(_complete_annotation())
    assert accepted is True
    assert failures == []


def test_ai_only_or_same_person_review_never_passes() -> None:
    annotation = _complete_annotation()
    annotation["ai_generated_only"] = True
    annotation["reviewer_identity"] = annotation["annotator_identity"]
    accepted, failures = MODULE.annotation_is_reviewed(annotation)
    assert accepted is False
    assert "ai_only_annotation_not_ground_truth" in failures
    assert "review_not_independent" in failures


def test_repeatability_and_ambiguity_fields_are_mandatory() -> None:
    annotation = _complete_annotation()
    annotation.pop("pass_2_geometry")
    annotation.pop("repeatability_comparison")
    annotation.pop("ambiguity_exclusion_reason")
    accepted, failures = MODULE.annotation_is_reviewed(annotation)
    assert accepted is False
    assert "missing_second_blind_pass" in failures
    assert "missing_repeatability_comparison" in failures
    assert "ambiguity_exclusion_not_explicit" in failures


def test_ai_proposals_are_counted_but_cannot_enter_human_annotations() -> None:
    packet = {"packet_id": "packet-1", "sha256": "a" * 64}
    evidence = {
        "source_record_file_sha256": "b" * 64,
        "attachments": [],
    }
    ai = {
        "normalized_candidate_sha256": "c" * 64,
        "passes": [
            {
                "ai_generated_only": True,
                "components": [
                    {
                        "ai_generated_only": True,
                        "geometry": None,
                    }
                ],
            },
            {
                "ai_generated_only": True,
                "components": [
                    {
                        "ai_generated_only": True,
                        "geometry": None,
                    }
                ],
            },
        ],
        "agreement": {"uncertainty_evidence_only": True},
    }
    template = MODULE._template("event-1", packet, evidence, ai)
    assert template["ai_proposal_summary"]["passes"] == 2
    assert template["ai_proposal_summary"]["all_annotations_ai_generated_only"] is True
    assert template["ai_proposal_summary"]["geometry_proposals"] == 0
    assert template["ai_proposal_summary"]["can_satisfy_human_review_gate"] is False
    assert template["review_gate"]["human_review_complete"] is False
    assert template["annotations"] == []
