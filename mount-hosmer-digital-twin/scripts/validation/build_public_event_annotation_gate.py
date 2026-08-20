"""Build the frozen human-review gate for public event observations.

This stage deliberately does not turn AI interpretation, provider geometry, or
unresolved satellite pixels into ground truth.  It creates one immutable,
blinded annotation template per candidate and records why no quantitative
observation has yet passed the gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-annotation-gate-v2"
TEMPLATE_SCHEMA = "avycore-public-event-annotation-template-v2"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
FEATURE_ROLES = {"release_or_crown", "dense_flow_deposit", "dense_flow_toe"}
CONFIDENCE_VALUES = {"low", "medium", "high"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def annotation_is_reviewed(annotation: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether an annotation meets the frozen independent-review gate."""

    failures: list[str] = []
    required_nonempty = (
        "component_id",
        "feature_role",
        "source_scene_ids",
        "observation_method",
        "confidence",
        "source_resolution_m",
        "horizontal_uncertainty_m",
        "temporal_uncertainty",
        "detection_limitations",
        "annotator_identity",
        "annotation_time_utc",
        "reviewer_identity",
        "review_time_utc",
    )
    for field in required_nonempty:
        value = annotation.get(field)
        if value is None or value == "" or value == []:
            failures.append(f"missing_{field}")
    if annotation.get("feature_role") not in FEATURE_ROLES:
        failures.append("invalid_feature_role")
    if annotation.get("confidence") not in CONFIDENCE_VALUES:
        failures.append("invalid_confidence")
    for field in ("source_resolution_m", "horizontal_uncertainty_m"):
        value = annotation.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            failures.append(f"invalid_{field}")
    if annotation.get("annotator_identity") == annotation.get("reviewer_identity"):
        failures.append("review_not_independent")
    if annotation.get("pass_1_geometry") is None:
        failures.append("missing_first_blind_pass")
    if annotation.get("pass_2_geometry") is None:
        failures.append("missing_second_blind_pass")
    if annotation.get("repeatability_comparison") is None:
        failures.append("missing_repeatability_comparison")
    if annotation.get("review_disposition") != "accepted":
        failures.append("independent_review_not_accepted")
    if annotation.get("ai_generated_only") is not False:
        failures.append("ai_only_annotation_not_ground_truth")
    ambiguity = annotation.get("ambiguity_exclusion_reason")
    if ambiguity is None:
        failures.append("ambiguity_exclusion_not_explicit")
    return not failures, failures


def _template(
    candidate_id: str,
    packet: dict[str, Any],
    evidence: dict[str, Any],
    ai_proposals: dict[str, Any],
) -> dict[str, Any]:
    template: dict[str, Any] = {
        "schema": TEMPLATE_SCHEMA,
        "candidate_id": candidate_id,
        "packet_id": packet["packet_id"],
        "annotation_status": "blocked_missing_independent_human_annotation_and_review",
        "source_lineage": {
            "blinded_packet_sha256": packet["sha256"],
            "regobs_source_record_sha256": evidence["source_record_file_sha256"],
            "attachment_sha256": [
                attachment["acquisition"]["sha256"]
                for attachment in evidence["attachments"]
            ],
            "ai_proposal_candidate_sha256": ai_proposals[
                "normalized_candidate_sha256"
            ],
        },
        "provider_geometry_role": (
            "Candidate source evidence only. Provider start/stop geometry is preserved but is "
            "not automatically accepted as release, deposit, or toe truth."
        ),
        "required_components": [
            "Separate every visually supportable release/crown component.",
            "Separate every visually supportable dense-flow deposit component.",
            "Record a distal dense-flow toe only when terminal attribution is supportable.",
        ],
        "required_fields": [
            "component_id",
            "feature_role",
            "source_scene_ids",
            "observation_method",
            "confidence",
            "source_resolution_m",
            "horizontal_uncertainty_m",
            "temporal_uncertainty",
            "detection_limitations",
            "ambiguity_exclusion_reason",
            "annotator_identity",
            "annotation_time_utc",
            "pass_1_geometry",
            "pass_2_geometry",
            "repeatability_comparison",
            "reviewer_identity",
            "review_time_utc",
            "review_disposition",
            "ai_generated_only",
        ],
        "ambiguity_exclusions_required": [
            "cloud_or_cloud_shadow",
            "radar_layover_or_shadow",
            "forest_occlusion",
            "water_or_wet_surface",
            "scene_edge_or_missing_data",
            "prior_deposit_or_overlapping_event",
            "component_or_terminal_toe_ambiguity",
        ],
        "review_gate": {
            "two_blind_passes_required": True,
            "independent_reviewer_required": True,
            "same_person_review_forbidden": True,
            "ai_generated_annotation_alone_is_ground_truth": False,
            "required_pixel_masks_resolved": False,
            "human_annotation_complete": False,
            "human_review_complete": False,
            "accepted_quantitative_components": 0,
        },
        "ai_proposal_summary": {
            "passes": len(ai_proposals["passes"]),
            "component_records": sum(
                len(annotation_pass["components"])
                for annotation_pass in ai_proposals["passes"]
            ),
            "all_annotations_ai_generated_only": all(
                annotation_pass.get("ai_generated_only") is True
                and all(
                    component.get("ai_generated_only") is True
                    for component in annotation_pass["components"]
                )
                for annotation_pass in ai_proposals["passes"]
            ),
            "geometry_proposals": sum(
                component.get("geometry") is not None
                for annotation_pass in ai_proposals["passes"]
                for component in annotation_pass["components"]
            ),
            "agreement_is_uncertainty_evidence_only": ai_proposals["agreement"][
                "uncertainty_evidence_only"
            ],
            "can_satisfy_human_review_gate": False,
        },
        "annotations": [],
    }
    template["normalized_template_sha256"] = _sha256_bytes(_canonical_json(template))
    return template


def build_annotation_gate(
    packet_path: Path,
    evidence_path: Path,
    ai_proposal_path: Path,
    cache_root: Path,
    triage_root: Path | None = None,
) -> dict[str, Any]:
    packet_bytes = packet_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    ai_proposal_bytes = ai_proposal_path.read_bytes()
    packets = json.loads(packet_bytes)
    evidence = json.loads(evidence_bytes)
    ai_proposals = json.loads(ai_proposal_bytes)
    if packets.get("schema") != "avycore-blinded-observation-packets-v1":
        raise ValueError("Unexpected blinded-packet schema.")
    if evidence.get("schema") != "avycore-public-regobs-blinded-evidence-v1":
        raise ValueError("Unexpected RegObs evidence schema.")
    if ai_proposals.get("schema") != "avycore-public-event-ai-annotation-proposals-v1":
        raise ValueError("Unexpected AI annotation-proposal schema.")
    packet_by_id = {item["candidate_id"]: item for item in packets["packets"]}
    evidence_by_id = {item["candidate_id"]: item for item in evidence["candidates"]}
    ai_by_id = {item["candidate_id"]: item for item in ai_proposals["candidates"]}
    if not (packet_by_id.keys() == evidence_by_id.keys() == ai_by_id.keys()):
        raise ValueError(
            "Blinded packets, RegObs evidence, and AI proposals have different candidates."
        )

    records: list[dict[str, Any]] = []
    for candidate_id in sorted(packet_by_id):
        template = _template(
            candidate_id,
            packet_by_id[candidate_id],
            evidence_by_id[candidate_id],
            ai_by_id[candidate_id],
        )
        path = cache_root / candidate_id / "annotation-template.json"
        payload = _pretty_json(template)
        if path.exists():
            if path.read_bytes() != payload:
                raise ValueError(f"Immutable annotation-template conflict at {path}.")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(payload)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError(f"Concurrent annotation-template conflict at {path}.")
        records.append(
            {
                "candidate_id": candidate_id,
                "annotation_status": template["annotation_status"],
                "template_cache_path": path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix(),
                "template_sha256": _sha256_bytes(payload),
                "human_annotation_complete": False,
                "independent_human_review_complete": False,
                "accepted_quantitative_components": 0,
                "ai_passes": len(ai_by_id[candidate_id]["passes"]),
                "ai_generated_component_records": sum(
                    len(annotation_pass["components"])
                    for annotation_pass in ai_by_id[candidate_id]["passes"]
                ),
                "ai_agreement_uncertainty_only": True,
            }
        )

    if triage_root is None:
        triage_root = (
            REPOSITORY_ROOT
            / ".validation-cache"
            / "public-regobs-blinded-evidence-v1"
            / "contact-sheets-ai-triage"
        )
    triage_sheets = [
        {
            "path": path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in sorted(triage_root.glob("sheet-*.jpg"))
    ]
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "stage": "blinded_observation_annotation_and_human_review_gate",
        "source_blinded_packet_manifest_sha256": _sha256_bytes(packet_bytes),
        "source_regobs_evidence_manifest_sha256": _sha256_bytes(evidence_bytes),
        "source_ai_annotation_proposals_sha256": _sha256_bytes(ai_proposal_bytes),
        "model_code_imported": False,
        "predictions_generated": False,
        "holdout_targets_accessed": False,
        "ai_attachment_inspection_role": (
            "AI inspection was limited to non-quantitative evidence triage. It did not create "
            "or review ground-truth geometry. Two isolated AI component passes abstained; "
            "their agreement is uncertainty evidence only."
        ),
        "ai_attachment_inspection": {
            "contact_sheets": triage_sheets,
            "observed_only_for_evidence_triage": True,
            "human_review": False,
            "quantitative_annotation": False,
            "summary": (
                "Attachments include field context, crowns, deposits, pits and provider-drawn "
                "marks, but also oblique, distant, flat-lit, forest-obscured and local-feature "
                "views. No geometry was inferred from the contact sheets."
            ),
        },
        "claim_boundary": (
            "No public event has a reviewed annotation. Empty templates and provider geometry "
            "are not quantitative ground truth."
        ),
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Scores are relative indices, not probabilities."
        ),
        "counts": {
            "candidate_templates": len(records),
            "ai_annotation_passes": sum(record["ai_passes"] for record in records),
            "ai_generated_component_records": sum(
                record["ai_generated_component_records"] for record in records
            ),
            "ai_geometry_proposals": 0,
            "ai_triage_contact_sheets": len(triage_sheets),
            "human_annotations_complete": 0,
            "independent_human_reviews_complete": 0,
            "accepted_quantitative_components": 0,
            "eligible_observations": 0,
        },
        "candidates": records,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packets",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "blinded-observation-packets-v1.json",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "public-regobs-blinded-evidence-v1.json",
    )
    parser.add_argument(
        "--ai-proposals",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-ai-annotation-proposals-v1.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache"
        / "public-event-annotation-gate-v2-nearest",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "public-event-annotation-gate-v2.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_annotation_gate(
        args.packets.resolve(),
        args.evidence.resolve(),
        args.ai_proposals.resolve(),
        args.cache_root.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote {artifact['counts']['candidate_templates']} immutable annotation templates; "
        "reviewed=0, eligible observations=0."
    )


if __name__ == "__main__":
    main()
