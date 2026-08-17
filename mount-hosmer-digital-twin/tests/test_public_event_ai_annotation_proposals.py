from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "build_public_event_ai_annotation_proposals.py"
)
SPEC = importlib.util.spec_from_file_location(
    "build_public_event_ai_annotation_proposals", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    acquisition = {
        "schema": "avycore-public-event-imagery-acquisition-v2",
        "predictions_generated": False,
        "candidates": [
            {
                "candidate_id": "event-1",
                "normalized_candidate_sha256": "a" * 64,
                "chip_grid": {"resolution_m": 10},
                "sentinel_1_grd": {
                    "selected_pair": {
                        "pre_to_event_start_seconds": 3600,
                        "event_end_to_post_seconds": 7200,
                        "temporal_baseline_seconds": 14400,
                    }
                },
                "sentinel_2_l2a": {"selected_pair": None},
            }
        ],
    }
    qa = {
        "schema": "avycore-public-event-pixel-qa-v3",
        "candidates": [
            {
                "candidate_id": "event-1",
                "normalized_candidate_sha256": "b" * 64,
                "sentinel_1_grd": {
                    "status": "failed_automatic_pixel_qa_unresolved_required_masks",
                    "automatic_pixel_qa_passed": False,
                    "total_pixels": 100,
                    "source_valid_pixels": 80,
                    "mask_stack": {"counts": {"usable": 0}},
                },
                "sentinel_2_l2a": {
                    "status": "not_reached_no_acquired_pair",
                    "automatic_pixel_qa_passed": False,
                    "total_pixels": None,
                    "source_valid_pixels": None,
                    "mask_stack": None,
                },
            }
        ],
    }
    acquisition_path = tmp_path / "acquisition.json"
    qa_path = tmp_path / "qa.json"
    acquisition_path.write_text(json.dumps(acquisition), encoding="utf-8")
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    return acquisition_path, qa_path


def test_two_blinded_passes_are_deterministic_and_never_become_ground_truth(
    tmp_path: Path,
) -> None:
    acquisition_path, qa_path = _artifacts(tmp_path)
    first = MODULE.build_proposals(acquisition_path, qa_path)
    second = MODULE.build_proposals(acquisition_path, qa_path)
    assert first == second
    candidate = first["candidates"][0]
    assert len(candidate["passes"]) == 2
    assert candidate["passes"][0]["normalized_pass_sha256"] != candidate["passes"][1][
        "normalized_pass_sha256"
    ]
    assert candidate["agreement"]["uncertainty_evidence_only"] is True
    assert candidate["agreement"]["can_satisfy_human_review_gate"] is False
    assert first["human_review_gate_satisfied"] is False


def test_every_component_is_ai_only_null_abstention(tmp_path: Path) -> None:
    acquisition_path, qa_path = _artifacts(tmp_path)
    artifact = MODULE.build_proposals(acquisition_path, qa_path)
    for annotation_pass in artifact["candidates"][0]["passes"]:
        assert annotation_pass["ai_generated_only"] is True
        assert annotation_pass["blinded"] is True
        for component in annotation_pass["components"]:
            assert component["ai_generated_only"] is True
            assert component["geometry"] is None
            assert component["observation_status"] == "abstain"
            assert component["reviewed_ground_truth"] is False
            assert component["can_satisfy_human_review_gate"] is False


def test_missing_coverage_and_uncertainty_are_not_converted_to_zero(
    tmp_path: Path,
) -> None:
    acquisition_path, qa_path = _artifacts(tmp_path)
    candidate = MODULE.build_proposals(acquisition_path, qa_path)["candidates"][0]
    s2 = candidate["scene_detection_coverage"]["sentinel_2_l2a"]
    assert s2["source_valid_fraction"] is None
    assert s2["usable_fraction_after_all_required_masks"] is None
    component = candidate["passes"][0]["components"][0]
    assert component["positional_uncertainty_m"] is None
    assert component["detection_coverage_fraction"] is None
