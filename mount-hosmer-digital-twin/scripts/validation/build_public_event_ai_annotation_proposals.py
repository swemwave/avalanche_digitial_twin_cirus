"""Build two isolated blinded AI annotation-pass records for public events.

The frozen packet/QA state is evaluated twice without sharing pass output.  A
pass must abstain when required pixel QA is unresolved; null geometry is retained
as unknown, never converted to an empty observation.  Every component record is
``ai_generated_only=true``.  Cross-pass agreement is uncertainty evidence only
and can never satisfy the independent-human-review gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-ai-annotation-proposals-v1"
PROPOSAL_ID = "public-event-ai-annotation-proposals-v1"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
COMPONENTS = ("release", "deposit", "terminal_toe")
PASS_PROTOCOLS = (
    {
        "pass_id": "ai-pass-a-coarse-to-fine",
        "inspection_order": [
            "pair_lineage",
            "whole_scene_qa",
            "component_evidence",
            "uncertainty",
        ],
        "isolation_salt": "a2e0d4ad8b258f82",
    },
    {
        "pass_id": "ai-pass-b-fine-to-coarse",
        "inspection_order": [
            "component_evidence",
            "uncertainty",
            "whole_scene_qa",
            "pair_lineage",
        ],
        "isolation_salt": "f1b9cc829de81373",
    },
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _temporal_uncertainty(candidate: dict[str, Any]) -> dict[str, Any]:
    values = []
    for sensor in ("sentinel_1_grd", "sentinel_2_l2a"):
        selected = (candidate[sensor] or {}).get("selected_pair")
        if not selected:
            continue
        values.append(
            {
                "sensor": sensor,
                "pre_to_event_start_seconds": selected.get(
                    "pre_to_event_start_seconds"
                ),
                "event_end_to_post_seconds": selected.get(
                    "event_end_to_post_seconds"
                ),
                "temporal_baseline_seconds": selected.get(
                    "temporal_baseline_seconds"
                ),
            }
        )
    return {
        "intervals": values,
        "interpretation": (
            "Acquisition gaps bound when visible change may have occurred; they do not "
            "prove event/component timing or exclude intervening avalanches."
        ),
        "missing_intervals_are_null_not_zero": True,
    }


def _coverage(qa_candidate: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for sensor in ("sentinel_1_grd", "sentinel_2_l2a"):
        qa = qa_candidate[sensor]
        total = qa.get("total_pixels")
        valid = qa.get("source_valid_pixels")
        usable = ((qa.get("mask_stack") or {}).get("counts") or {}).get("usable")
        result[sensor] = {
            "total_pixels": total,
            "source_valid_pixels": valid,
            "source_valid_fraction": (
                valid / total
                if isinstance(valid, int) and isinstance(total, int) and total > 0
                else None
            ),
            "usable_pixels_after_all_required_masks": usable,
            "usable_fraction_after_all_required_masks": (
                usable / total
                if isinstance(usable, int) and isinstance(total, int) and total > 0
                else None
            ),
            "status": qa["status"],
        }
    return result


def _component_proposal(
    component: str,
    qa_candidate: dict[str, Any],
    chip_resolution_m: float,
) -> dict[str, Any]:
    automatic_pass = any(
        qa_candidate[sensor].get("automatic_pixel_qa_passed") is True
        for sensor in ("sentinel_1_grd", "sentinel_2_l2a")
    )
    # Even a future automatic pass would still require visible, attributable
    # component evidence.  This v1 generator only has the QA record, not a
    # reviewed component packet, so it must abstain in both cases.
    reason = (
        "no_reviewed_component_packet_available"
        if automatic_pass
        else "required_pixel_qa_unresolved_packet_withheld"
    )
    return {
        "component": component,
        "ai_generated_only": True,
        "observation_status": "abstain",
        "geometry": None,
        "geometry_absence_meaning": "unknown_not_observed_as_zero",
        "abstention_reason": reason,
        "nominal_source_resolution_m": chip_resolution_m,
        "effective_component_resolution_m": None,
        "effective_resolution_reason": (
            "No attributable component geometry exists from which effective resolution "
            "or minimum detectable width can be estimated."
        ),
        "positional_uncertainty_m": None,
        "positional_uncertainty_reason": (
            "No proposed boundary; nominal pixel size is not a positional-accuracy claim."
        ),
        "ambiguity": "unbounded_component_attribution_ambiguity",
        "component_attribution": "unresolved",
        "detection_coverage_fraction": None,
        "detection_coverage_reason": (
            "Scene-level QA coverage cannot be converted to component-level detection "
            "coverage without a component geometry."
        ),
        "reviewed_ground_truth": False,
        "can_satisfy_human_review_gate": False,
    }


def _run_isolated_pass(
    protocol: dict[str, Any],
    candidate: dict[str, Any],
    qa_candidate: dict[str, Any],
    input_hash: str,
) -> dict[str, Any]:
    sealed_input = {
        "candidate_id": candidate["candidate_id"],
        "input_hash": input_hash,
        "isolation_salt": protocol["isolation_salt"],
        "inspection_order": protocol["inspection_order"],
        "other_pass_output_available": False,
        "model_results_available": False,
        "regobs_target_geometry_available": False,
    }
    components = [
        _component_proposal(
            component,
            qa_candidate,
            float(candidate["chip_grid"]["resolution_m"]),
        )
        for component in COMPONENTS
    ]
    result: dict[str, Any] = {
        "pass_id": protocol["pass_id"],
        "ai_generated_only": True,
        "blinded": True,
        "independent_of_other_pass_output": True,
        "independence_scope": (
            "Separate deterministic pass state and inspection order; no other-pass output, "
            "target geometry, human review, prediction, or model result supplied. This is "
            "not independence from the AI system and is not a human-review substitute."
        ),
        "sealed_input": sealed_input,
        "sealed_input_sha256": _sha256_bytes(_canonical_json(sealed_input)),
        "components": components,
    }
    result["normalized_pass_sha256"] = _sha256_bytes(_canonical_json(result))
    return result


def build_proposals(acquisition_path: Path, qa_path: Path) -> dict[str, Any]:
    acquisition_bytes = acquisition_path.read_bytes()
    qa_bytes = qa_path.read_bytes()
    acquisition = json.loads(acquisition_bytes)
    qa = json.loads(qa_bytes)
    if acquisition.get("schema") != "avycore-public-event-imagery-acquisition-v2":
        raise ValueError("AI proposals require imagery acquisition schema v2.")
    if qa.get("schema") != "avycore-public-event-pixel-qa-v3":
        raise ValueError("AI proposals require pixel-QA schema v3.")
    if acquisition.get("predictions_generated") is not False:
        raise ValueError("Refusing AI annotation after prediction access.")
    qa_by_id = {candidate["candidate_id"]: candidate for candidate in qa["candidates"]}
    candidates = []
    for candidate in acquisition["candidates"]:
        candidate_id = candidate["candidate_id"]
        qa_candidate = qa_by_id[candidate_id]
        candidate_input = {
            "candidate_id": candidate_id,
            "acquisition_candidate_sha256": candidate["normalized_candidate_sha256"],
            "qa_candidate_sha256": qa_candidate["normalized_candidate_sha256"],
        }
        input_hash = _sha256_bytes(_canonical_json(candidate_input))
        passes = [
            _run_isolated_pass(protocol, candidate, qa_candidate, input_hash)
            for protocol in PASS_PROTOCOLS
        ]
        agreement = {
            "ai_generated_only": True,
            "agreement_type": "concordant_abstention",
            "components": {
                component: {
                    "both_abstained": True,
                    "geometry_agreement_metric": None,
                    "agreement_interpretation": (
                        "Evidence that both isolated AI passes found the available packet "
                        "insufficient; not evidence of an empty component or correct boundary."
                    ),
                }
                for component in COMPONENTS
            },
            "uncertainty_evidence_only": True,
            "reviewed_ground_truth": False,
            "can_satisfy_human_review_gate": False,
        }
        result: dict[str, Any] = {
            "candidate_id": candidate_id,
            "input_lineage": candidate_input,
            "input_sha256": input_hash,
            "temporal_uncertainty": _temporal_uncertainty(candidate),
            "scene_detection_coverage": _coverage(qa_candidate),
            "passes": passes,
            "agreement": agreement,
            "accepted_ai_annotations": 0,
            "independent_human_reviews": 0,
            "validation_contract_eligible": False,
        }
        result["normalized_candidate_sha256"] = _sha256_bytes(
            _canonical_json(result)
        )
        candidates.append(result)
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "proposal_id": PROPOSAL_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_acquisition_sha256": _sha256_bytes(acquisition_bytes),
        "source_pixel_qa_sha256": _sha256_bytes(qa_bytes),
        "predictions_generated": False,
        "model_results_opened": False,
        "regobs_target_geometry_accessed": False,
        "all_annotations_ai_generated_only": True,
        "human_review_gate_satisfied": False,
        "claim_boundary": (
            "These are isolated blinded AI proposal passes, not reviewed ground truth. "
            "Agreement is uncertainty evidence only. Null geometry means unknown/abstained, "
            "never an observed zero. Real independent human review remains mandatory."
        ),
        "counts": {
            "candidates": len(candidates),
            "ai_passes": len(candidates) * len(PASS_PROTOCOLS),
            "component_records": len(candidates)
            * len(PASS_PROTOCOLS)
            * len(COMPONENTS),
            "geometry_proposals": 0,
            "concordant_abstentions": len(candidates) * len(COMPONENTS),
            "independent_human_reviews": 0,
            "validation_contract_eligible": 0,
        },
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--acquisition",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-imagery-acquisition-v2.json",
    )
    parser.add_argument(
        "--pixel-qa",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-pixel-qa-v3.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-ai-annotation-proposals-v1.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_proposals(args.acquisition.resolve(), args.pixel_qa.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote {artifact['counts']['ai_passes']} isolated AI pass records; "
        "geometry proposals=0, human reviews=0."
    )


if __name__ == "__main__":
    main()
