"""Evaluate every public candidate against validation-contract v3 and stop gate.

This evaluator reads evidence artifacts and the validation-contract source only.
It never imports hazard/runout code, opens a prediction, assigns a holdout, or
repairs missing evidence.  Candidates that cannot form a contract-v3 dataset are
reported as excluded before ingestion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-strict-funnel-v5"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
EXPECTED_CONTRACT_VERSION = "avycore-validation-dataset-v3"
MINIMUM_EVENTS = 12
MINIMUM_PATHS = 6
MINIMUM_MOUNTAINS = 2
MINIMUM_STORMS = 3

CHECK_CATALOG = {
    "bounded_event_time_with_confidence": {
        "classification": "technically_resolvable_now",
        "predicate": "technical.checks.bounded_event_time_with_confidence is true",
        "required_evidence_fields": [
            "event_time.event_start_utc",
            "event_time.event_end_utc",
            "event_time.event_time_confidence",
            "event_time.confidence_basis",
        ],
    },
    "required_pixel_masks_resolved": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.required_pixel_masks_resolved is true",
        "required_evidence_fields": [
            "two isolated reviewer mask sets",
            "missing_data",
            "scene_edge",
            "detection_exclusion",
            "survey_coverage",
            "cloud",
            "cloud_shadow",
            "shadow",
            "forest",
            "water",
            "layover",
            "radar_shadow",
            "prior_deposit",
        ],
    },
    "blinded_packet_released_for_annotation": {
        "classification": "technically_resolvable_now",
        "predicate": "packet.released is true and packet.accepted_as_quantitative_evidence is false",
        "required_evidence_fields": [
            "packet.archive_sha256",
            "packet.packet_content_sha256",
            "packet.released",
            "packet.accepted_as_quantitative_evidence",
        ],
    },
    "independent_human_review_complete": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.independent_human_review_complete is true",
        "required_evidence_fields": [
            "two distinct human identity hashes",
            "two isolated submissions",
            "separate identity-verification record",
        ],
    },
    "accepted_quantitative_component_geometry": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.accepted_quantitative_components > 0",
        "required_evidence_fields": ["accepted component role", "projected geometry", "agreement metrics"],
    },
    "release_geometry_mapping_method_and_uncertainty_complete": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.release_geometry_mapping_method_and_uncertainty_complete is true",
        "required_evidence_fields": [
            "release geometry",
            "mapping method",
            "source resolution",
            "horizontal uncertainty",
            "uncertainty confidence level",
        ],
    },
    "projected_metre_target_crs_and_transform_frozen": {
        "classification": "requires_better_public_primary_evidence",
        "predicate": "technical and reviewed geometry both use a contract-v3 allowlisted projected-metre CRS with frozen transform lineage",
        "required_evidence_fields": ["target CRS", "axis order", "affine transform", "coordinate-operation lineage"],
    },
    "event_surface_terrain_eligible": {
        "classification": "requires_better_public_primary_evidence",
        "predicate": "technical.checks.event_surface_terrain_eligible is true",
        "required_evidence_fields": [
            "event-compatible terrain/snow surface",
            "horizontal and vertical uncertainty",
            "vertical datum",
            "epoch mismatch bound",
            "source SHA-256",
        ],
    },
    "path_mountain_storm_identities_frozen": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "independently reviewed path_id, mountain_id, and storm_cycle_id are all non-null",
        "required_evidence_fields": ["path_id", "mountain_id", "storm_cycle_id", "identity basis"],
    },
    "independent_reviewed_release_polygon": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.independent_reviewed_release_polygon is true",
        "required_evidence_fields": ["two blind human release polygons", "accepted agreement record"],
    },
    "normal_to_slope_release_thickness_evidence": {
        "classification": "requires_better_public_primary_evidence",
        "predicate": "technical.checks.normal_to_slope_release_thickness_evidence is true",
        "required_evidence_fields": ["normal-to-slope direction", "method", "metre value/bounds", "uncertainty", "source SHA-256"],
    },
    "release_density_transferability_accepted": {
        "classification": "requires_better_public_primary_evidence",
        "predicate": "technical release-density transfer uncertainty is quantitatively supported and both independent reviewers accept the frozen prior",
        "required_evidence_fields": ["density distribution", "source population", "numeric transfer uncertainty", "source SHA-256", "review acceptance"],
    },
    "reviewed_dense_flow_deposit_or_terminal_toe": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.reviewed_dense_flow_deposit_or_terminal_toe is true",
        "required_evidence_fields": ["reviewed dense-flow deposit polygon or terminal toe", "agreement metrics"],
    },
    "dense_flow_component_attribution_complete": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.dense_flow_component_attribution_complete is true",
        "required_evidence_fields": ["dense-flow scope", "terminal attribution", "overlap/prior-event exclusions"],
    },
    "survey_coverage_and_detection_masks_complete": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.survey_coverage_and_detection_masks_complete is true",
        "required_evidence_fields": ["complete-search survey polygon", "component detection masks", "known-absence semantics"],
    },
    "provenance_bearing_release_model_inputs_complete": {
        "classification": "requires_better_public_primary_evidence",
        "predicate": "technical.checks.provenance_bearing_release_model_inputs_complete is true",
        "required_evidence_fields": ["event forcing", "snow state", "units", "valid UTC interval", "uncertainty", "spatial representativeness", "source SHA-256"],
    },
    "release_to_runout_rule_frozen": {
        "classification": "technically_resolvable_now",
        "predicate": "technical.checks.release_to_runout_rule_frozen is true",
        "required_evidence_fields": ["immutable rule SHA-256", "missing-input behavior", "observed-release exclusion", "grid-to-geometry rule"],
    },
    "release_observation_confidence_quantified": {
        "classification": "requires_genuine_independent_human_action",
        "predicate": "human_review.release_observation_confidence_quantified is true",
        "required_evidence_fields": ["release confidence", "confidence basis", "resolution", "horizontal uncertainty and confidence level"],
    },
}


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


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read(path: Path, schema: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    parsed = json.loads(payload)
    if parsed.get("schema") != schema:
        raise ValueError(f"Unexpected schema in {path}: {parsed.get('schema')!r}.")
    return parsed, _sha256_bytes(payload)


def contract_source_identity() -> dict[str, Any]:
    source_root = REPOSITORY_ROOT / "packages" / "avycore" / "src" / "avycore" / "validation"
    status_path = source_root / "status.py"
    status_text = status_path.read_text(encoding="utf-8")
    match = re.search(r'^VALIDATION_CONTRACT_VERSION\s*=\s*"([^"]+)"', status_text, re.MULTILINE)
    if not match or match.group(1) != EXPECTED_CONTRACT_VERSION:
        raise ValueError("The validation-contract version is not the frozen v3 contract.")
    paths = [source_root / name for name in ("contracts.py", "status.py", "trust.py")]
    return {
        "version": match.group(1),
        "source_files": [
            {
                "path": path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix(),
                "sha256": _sha256_file(path),
            }
            for path in paths
        ],
        "evaluation_mode": (
            "pre-ingestion evidence checklist against v3 requirements; no incomplete "
            "ValidationDataset is fabricated"
        ),
    }


def evaluate_candidate(
    candidate_id: str,
    *,
    imagery: dict[str, Any],
    qa: dict[str, Any],
    packet: dict[str, Any],
    evidence: dict[str, Any],
    human_review: dict[str, Any],
    technical: dict[str, Any],
    release_state: dict[str, Any],
    terrain: dict[str, Any],
    s1_processing: dict[str, Any],
    ai_proposals: dict[str, Any],
) -> dict[str, Any]:
    reviewed = human_review["independent_human_review_complete"] is True
    accepted_components = human_review["accepted_quantitative_components"]
    technical_checks = technical["checks"]
    masks_resolved = human_review["required_pixel_masks_resolved"] is True
    release_thickness = technical_checks[
        "normal_to_slope_release_thickness_evidence"
    ] is True
    terrain_eligible = technical_checks["event_surface_terrain_eligible"] is True
    common_checks = {
        "dry_dense_slab_regime": True,
        "bounded_event_time_with_confidence": technical_checks[
            "bounded_event_time_with_confidence"
        ]
        is True,
        "required_pixel_masks_resolved": masks_resolved,
        "blinded_packet_released_for_annotation": (
            packet["released"] is True
            and packet["accepted_as_quantitative_evidence"] is False
        ),
        "independent_human_review_complete": reviewed,
        "accepted_quantitative_component_geometry": accepted_components > 0,
        "release_geometry_mapping_method_and_uncertainty_complete": human_review[
            "release_geometry_mapping_method_and_uncertainty_complete"
        ]
        is True,
        "projected_metre_target_crs_and_transform_frozen": (
            technical_checks["projected_metre_target_crs_and_transform_frozen"] is True
            and human_review["projected_metre_target_crs_and_transform_frozen"] is True
        ),
        "event_surface_terrain_eligible": terrain_eligible,
        "path_mountain_storm_identities_frozen": human_review[
            "path_mountain_storm_identities_frozen"
        ]
        is True,
    }
    profile_r = dict(common_checks)
    profile_r.update(
        {
            "independent_reviewed_release_polygon": human_review[
                "independent_reviewed_release_polygon"
            ]
            is True,
            "release_observation_confidence_quantified": human_review[
                "release_observation_confidence_quantified"
            ]
            is True,
        }
    )
    profile_c = dict(common_checks)
    profile_c.update(
        {
            "independent_reviewed_release_polygon": human_review[
                "independent_reviewed_release_polygon"
            ]
            is True,
            "normal_to_slope_release_thickness_evidence": release_thickness,
            "frozen_release_density_distribution": True,
            "release_density_transferability_accepted": (
                technical_checks["release_density_transferability_accepted"] is True
                and human_review["release_density_transferability_accepted"] is True
            ),
            "reviewed_dense_flow_deposit_or_terminal_toe": human_review[
                "reviewed_dense_flow_deposit_or_terminal_toe"
            ]
            is True,
            "dense_flow_component_attribution_complete": human_review[
                "dense_flow_component_attribution_complete"
            ]
            is True,
            "survey_coverage_and_detection_masks_complete": human_review[
                "survey_coverage_and_detection_masks_complete"
            ]
            is True,
        }
    )
    profile_e = dict(profile_c)
    profile_e.update(
        {
            "provenance_bearing_release_model_inputs_complete": technical_checks[
                "provenance_bearing_release_model_inputs_complete"
            ]
            is True,
            "release_to_runout_rule_frozen": technical_checks[
                "release_to_runout_rule_frozen"
            ]
            is True,
            "observed_release_excluded_from_prediction_path": True,
        }
    )

    def result(checks: dict[str, bool]) -> dict[str, Any]:
        failed = [name for name, passed in checks.items() if not passed]
        return {"eligible": not failed, "checks": checks, "failed_checks": failed}

    profiles = {"R": result(profile_r), "C": result(profile_c), "E": result(profile_e)}
    blockers = sorted({failed for profile in profiles.values() for failed in profile["failed_checks"]})
    processed_polarizations = [
        polarization
        for scene in s1_processing.get("scenes", [])
        for polarization in scene.get("polarizations", [])
    ]
    s2_metadata_verified = False
    s2_diagnostics = qa["sentinel_2_l2a"].get("diagnostics") or {}
    if "official_product_metadata" in s2_diagnostics:
        s2_metadata_verified = all(
            len(position.get("lineage", [])) == 3
            for position in s2_diagnostics["official_product_metadata"].values()
        )
    return {
        "candidate_id": candidate_id,
        "source_avalanche_type": evidence["event"]["avalanche_name"],
        "imagery_acquisition": {
            "sentinel_1_pair_acquired": bool(imagery["sentinel_1_grd"].get("scenes")),
            "sentinel_2_pair_acquired": bool(imagery["sentinel_2_l2a"].get("scenes")),
        },
        "attachment_count": evidence["counts"]["attachments"],
        "terrain_acquired": terrain["terrain_acquired"],
        "technical_evidence": {
            "frozen_event_time": technical["event_time"],
            "frozen_target_grid": technical["target_grid"],
            "sha256_lineage": technical["lineage"],
            "uncertainties": technical["uncertainties"],
            "release_to_runout_rule": technical["release_to_runout_rule"],
            "sentinel_1_processing_status": s1_processing["status"],
            "sentinel_1_processed_polarization_stacks": len(processed_polarizations),
            "sentinel_1_calibration_valid_pixels": sum(
                item["counts"]["calibration_valid"]
                for item in processed_polarizations
            ),
            "sentinel_1_terrain_gradient_valid_pixels": sum(
                item["counts"]["terrain_gradient_valid"]
                for item in processed_polarizations
            ),
            "sentinel_1_terrain_normalized_usable_pixels": sum(
                item["counts"]["terrain_normalized_usable"]
                for item in processed_polarizations
            ),
            "sentinel_2_official_metadata_verified": s2_metadata_verified,
            "ai_passes": len(ai_proposals["passes"]),
            "ai_geometry_proposals": sum(
                component.get("geometry") is not None
                for annotation_pass in ai_proposals["passes"]
                for component in annotation_pass["components"]
            ),
            "ai_agreement_uncertainty_only": ai_proposals["agreement"][
                "uncertainty_evidence_only"
            ],
            "scene_detection_coverage": ai_proposals["scene_detection_coverage"],
            "temporal_uncertainty": ai_proposals["temporal_uncertainty"],
        },
        "release_density_prior_frozen": True,
        "profiles": profiles,
        "eligible_any_profile": any(profile["eligible"] for profile in profiles.values()),
        "path_id": (human_review.get("accepted_event_grouping") or {}).get("path_id"),
        "mountain_id": (human_review.get("accepted_event_grouping") or {}).get("mountain_id"),
        "storm_cycle_id": (human_review.get("accepted_event_grouping") or {}).get("storm_cycle_id"),
        "blockers": blockers,
    }


def cohort_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [candidate for candidate in candidates if candidate["profiles"]["C"]["eligible"]]
    paths = {candidate["path_id"] for candidate in eligible if candidate["path_id"]}
    mountains = {candidate["mountain_id"] for candidate in eligible if candidate["mountain_id"]}
    storms = {candidate["storm_cycle_id"] for candidate in eligible if candidate["storm_cycle_id"]}
    checks = {
        "at_least_12_events": len(eligible) >= MINIMUM_EVENTS,
        "at_least_6_paths": len(paths) >= MINIMUM_PATHS,
        "at_least_2_mountains": len(mountains) >= MINIMUM_MOUNTAINS,
        "at_least_3_storms": len(storms) >= MINIMUM_STORMS,
    }
    return {
        "component": "conditional_runout",
        "evidence_profile": "C",
        "requirements": {
            "events": MINIMUM_EVENTS,
            "paths": MINIMUM_PATHS,
            "mountains": MINIMUM_MOUNTAINS,
            "storms": MINIMUM_STORMS,
        },
        "observed": {
            "eligible_events": len(eligible),
            "distinct_paths": len(paths),
            "distinct_mountains": len(mountains),
            "distinct_storms": len(storms),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def evaluate_funnel(
    acquisition_path: Path,
    qa_path: Path,
    packets_path: Path,
    evidence_path: Path,
    human_reviews_path: Path,
    technical_evidence_path: Path,
    release_state_path: Path,
    terrain_path: Path,
    nearest_dn_path: Path,
    s1_processing_path: Path,
    ai_proposals_path: Path,
    source_audit_path: Path,
) -> dict[str, Any]:
    acquisition, acquisition_sha = _read(acquisition_path, "avycore-public-event-imagery-acquisition-v2")
    qa, qa_sha = _read(qa_path, "avycore-public-event-pixel-qa-v3")
    packets, packets_sha = _read(packets_path, "avycore-blinded-observation-packets-v4")
    evidence, evidence_sha = _read(evidence_path, "avycore-public-regobs-blinded-evidence-v1")
    human_reviews, human_reviews_sha = _read(
        human_reviews_path, "avycore-public-event-human-review-status-v5"
    )
    technical_evidence, technical_evidence_sha = _read(
        technical_evidence_path, "avycore-public-event-technical-evidence-v2"
    )
    release_state, release_state_sha = _read(release_state_path, "avycore-public-release-state-evidence-v1")
    terrain, terrain_sha = _read(terrain_path, "avycore-public-event-terrain-acquisition-v1")
    nearest_dn, nearest_dn_sha = _read(
        nearest_dn_path, "avycore-public-event-sentinel1-dn-nearest-v1"
    )
    s1_processing, s1_processing_sha = _read(
        s1_processing_path, "avycore-public-event-sentinel1-processing-v1"
    )
    ai_proposals, ai_proposals_sha = _read(
        ai_proposals_path, "avycore-public-event-ai-annotation-proposals-v1"
    )
    source_audit, source_audit_sha = _read(
        source_audit_path, "avycore-public-validation-source-audit-v2"
    )
    collections = {
        "imagery": {item["candidate_id"]: item for item in acquisition["candidates"]},
        "qa": {item["candidate_id"]: item for item in qa["candidates"]},
        "packet": {item["candidate_id"]: item for item in packets["packets"]},
        "evidence": {item["candidate_id"]: item for item in evidence["candidates"]},
        "human_review": {
            item["candidate_id"]: item for item in human_reviews["candidates"]
        },
        "technical": {
            item["candidate_id"]: item for item in technical_evidence["candidates"]
        },
        "release_state": {item["candidate_id"]: item for item in release_state["crown_height_semantics"]},
        "terrain": {item["candidate_id"]: item for item in terrain["candidates"]},
        "s1_processing": {
            item["candidate_id"]: item for item in s1_processing["candidates"]
        },
        "ai_proposals": {
            item["candidate_id"]: item for item in ai_proposals["candidates"]
        },
    }
    identities = set(collections["imagery"])
    for label, collection in collections.items():
        if set(collection) != identities:
            raise ValueError(f"Candidate identities differ in {label}.")
    candidates = [
        evaluate_candidate(
            candidate_id,
            imagery=collections["imagery"][candidate_id],
            qa=collections["qa"][candidate_id],
            packet=collections["packet"][candidate_id],
            evidence=collections["evidence"][candidate_id],
            human_review=collections["human_review"][candidate_id],
            technical=collections["technical"][candidate_id],
            release_state=collections["release_state"][candidate_id],
            terrain=collections["terrain"][candidate_id],
            s1_processing=collections["s1_processing"][candidate_id],
            ai_proposals=collections["ai_proposals"][candidate_id],
        )
        for candidate_id in sorted(identities)
    ]
    gate = cohort_gate(candidates)
    if gate["passed"]:
        raise ValueError("The public cohort unexpectedly passed; continue with a frozen split instead of publishing failure.")
    profile_counts = {
        profile: sum(candidate["profiles"][profile]["eligible"] for candidate in candidates)
        for profile in ("R", "C", "E")
    }
    blocker_counts: dict[str, int] = {}
    for candidate in candidates:
        for blocker in candidate["blockers"]:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "validation_contract": contract_source_identity(),
        "failing_check_catalog": CHECK_CATALOG,
        "source_artifacts": {
            "imagery_acquisition_sha256": acquisition_sha,
            "pixel_qa_sha256": qa_sha,
            "blinded_packets_sha256": packets_sha,
            "regobs_evidence_sha256": evidence_sha,
            "human_review_status_sha256": human_reviews_sha,
            "technical_evidence_sha256": technical_evidence_sha,
            "release_state_evidence_sha256": release_state_sha,
            "terrain_acquisition_sha256": terrain_sha,
            "sentinel_1_nearest_dn_sha256": nearest_dn_sha,
            "sentinel_1_processing_sha256": s1_processing_sha,
            "ai_annotation_proposals_sha256": ai_proposals_sha,
            "public_source_audit_sha256": source_audit_sha,
        },
        "evaluation_order": (
            "anonymous source audit; imagery acquisition; radiometric/terrain processing; "
            "explicit pixel QA; immutable packet release; optional AI-only overlays; "
            "independent-human-review import; release-state and technical-evidence freeze; "
            "terrain acquisition; unchanged validation-contract-v3 evaluation"
        ),
        "model_code_imported": False,
        "predictions_generated": False,
        "holdout_partition_assigned": False,
        "holdout_targets_sealed": False,
        "counts": {
            "candidates_evaluated": len(candidates),
            "excluded_candidates": sum(not candidate["eligible_any_profile"] for candidate in candidates),
            "eligible_any_profile": sum(candidate["eligible_any_profile"] for candidate in candidates),
            "eligible_by_profile": profile_counts,
            "imagery_pairs_acquired": {
                "sentinel_1": acquisition["counts"]["sentinel_1_pairs_acquired"],
                "sentinel_2": acquisition["counts"]["sentinel_2_pairs_acquired"],
            },
            "automatic_pixel_qa_passes": {
                "sentinel_1": qa["counts"]["sentinel_1_automatic_pass"],
                "sentinel_2": qa["counts"]["sentinel_2_automatic_pass"],
            },
            "packets_released": packets["counts"]["packets_released"],
            "regobs_attachments_archived": evidence["counts"]["attachments_archived"],
            "independent_human_reviews": human_reviews["counts"]["independent_human_reviews_complete"],
            "release_thickness_evidence_eligible": release_state["counts"]["release_thickness_evidence_eligible"],
            "terrain_chips_acquired": terrain["counts"]["terrain_chips_acquired"],
            "terrain_evidence_eligible": terrain["counts"]["validation_contract_v3_terrain_eligible"],
            "sentinel_1_candidates_processed": s1_processing["counts"]["processed"],
            "sentinel_1_nearest_dn_assets": nearest_dn["counts"][
                "polarization_assets"
            ],
            "sentinel_1_nearest_dn_empty_assets": nearest_dn["counts"][
                "empty_assets"
            ],
            "sentinel_1_calibration_valid_pixels": sum(
                polarization["counts"]["calibration_valid"]
                for candidate in s1_processing["candidates"]
                for scene in candidate.get("scenes", [])
                for polarization in scene.get("polarizations", [])
            ),
            "sentinel_1_terrain_normalized_usable_pixels": sum(
                polarization["counts"]["terrain_normalized_usable"]
                for candidate in s1_processing["candidates"]
                for scene in candidate.get("scenes", [])
                for polarization in scene.get("polarizations", [])
            ),
            "ai_annotation_passes": ai_proposals["counts"]["ai_passes"],
            "ai_generated_component_records": ai_proposals["counts"][
                "component_records"
            ],
            "ai_geometry_proposals": ai_proposals["counts"]["geometry_proposals"],
            "public_sources_audited": source_audit["counts"]["sources_audited"],
            "new_public_source_eligible_events": source_audit["counts"][
                "new_eligible_events"
            ],
        },
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "candidates": candidates,
        "strict_cohort_gate": gate,
        "stop_decision": {
            "stopped_after_gate": 8,
            "reason": "Fewer than 12 eligible events across six paths, two mountains, and three storms.",
            "requirements_weakened": False,
            "gate_9_grouped_development_holdout_partition": "not_performed_by_required_stop_rule",
            "gate_10_avaframe_integration": "not_performed_by_required_stop_rule",
            "gate_11_avaframe_model_calibration": "not_performed_by_required_stop_rule",
            "gate_12_single_holdout_run": "not_performed_by_required_stop_rule",
            "gate_13_component_validation": "failed",
        },
        "is_validated": False,
        "mount_hosmer_validated": False,
        "operationally_validated": False,
        "claim_boundary": (
            "This is a failed public-evidence funnel, not a validation result. Sentinel-1 "
            "radiometric processing is evidence QA, not model calibration. No prediction, "
            "AvaFrame calibration, partitioning, or holdout evaluation was run."
        ),
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Scores are relative indices, not probabilities."
        ),
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    base = REPOSITORY_ROOT / "validation-data" / "candidates"
    parser.add_argument("--acquisition", type=Path, default=base / "public-event-imagery-acquisition-v2.json")
    parser.add_argument("--pixel-qa", type=Path, default=base / "public-event-pixel-qa-v3.json")
    parser.add_argument("--packets", type=Path, default=base / "blinded-observation-packets-v4.json")
    parser.add_argument("--evidence", type=Path, default=base / "public-regobs-blinded-evidence-v1.json")
    parser.add_argument(
        "--human-reviews",
        type=Path,
        default=base / "public-event-human-review-status-v5.json",
    )
    parser.add_argument(
        "--technical-evidence",
        type=Path,
        default=base / "public-event-technical-evidence-v2.json",
    )
    parser.add_argument(
        "--release-state",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "experiments" / "public-release-state-evidence-v1.json",
    )
    parser.add_argument("--terrain", type=Path, default=base / "public-event-terrain-acquisition-v1.json")
    parser.add_argument(
        "--nearest-dn",
        type=Path,
        default=base / "public-event-sentinel1-dn-nearest-v1.json",
    )
    parser.add_argument(
        "--sentinel1-processing",
        type=Path,
        default=base / "public-event-sentinel1-processing-v1.json",
    )
    parser.add_argument(
        "--ai-proposals",
        type=Path,
        default=base / "public-event-ai-annotation-proposals-v1.json",
    )
    parser.add_argument(
        "--source-audit",
        type=Path,
        default=base / "public-validation-source-audit-v2.json",
    )
    parser.add_argument("--output", type=Path, default=base / "public-event-strict-funnel-v5.json")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = evaluate_funnel(
        args.acquisition.resolve(),
        args.pixel_qa.resolve(),
        args.packets.resolve(),
        args.evidence.resolve(),
        args.human_reviews.resolve(),
        args.technical_evidence.resolve(),
        args.release_state.resolve(),
        args.terrain.resolve(),
        args.nearest_dn.resolve(),
        args.sentinel1_processing.resolve(),
        args.ai_proposals.resolve(),
        args.source_audit.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    observed = artifact["strict_cohort_gate"]["observed"]
    print(
        "Strict gate failed: "
        f"eligible={observed['eligible_events']}, paths={observed['distinct_paths']}, "
        f"mountains={observed['distinct_mountains']}, storms={observed['distinct_storms']}; "
        "stopped after gate 8."
    )


if __name__ == "__main__":
    main()
