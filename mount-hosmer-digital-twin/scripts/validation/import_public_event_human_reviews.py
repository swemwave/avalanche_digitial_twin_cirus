"""Validate and import two genuinely independent blinded human reviews.

The importer never creates annotations, reviewer identities, signatures,
geometries, or acceptance decisions.  It verifies supplied review payloads,
packet hashes, isolation attestations, a separate human identity-verification
record, component agreement, uncertainty, masks, CRS, and lineage.  AI-only or
self-reviewed payloads cannot satisfy the gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shapely.geometry import shape


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-human-review-status-v5"
SUBMISSION_SCHEMA = "avycore-independent-observation-review-v1"
VERIFICATION_SCHEMA = "avycore-independent-reviewer-verification-v1"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
REVIEWED_PROJECTED_METRE_CRS = {"EPSG:2056", "EPSG:26911", "EPSG:32613"}
FEATURE_ROLES = {"release", "dense_flow_deposit", "terminal_dense_flow_toe"}
MASK_NAMES = {
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
}
MASK_STATUSES = {"mapped_present", "checked_absent", "not_applicable"}


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
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable human-review status conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _utc(value: Any, field: str, failures: list[str]) -> None:
    if not isinstance(value, str):
        failures.append(f"missing_{field}")
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        failures.append(f"invalid_{field}")
        return
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        failures.append(f"{field}_not_utc")


def _finite_positive(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _packet_payload(record: dict[str, Any]) -> dict[str, Any]:
    archive_path = Path(record["archive_path"])
    if not archive_path.is_absolute():
        archive_path = REPOSITORY_ROOT / archive_path
    if _sha256_file(archive_path) != record["archive_sha256"]:
        raise ValueError(f"Blinded packet archive hash mismatch at {archive_path}.")
    with zipfile.ZipFile(archive_path) as archive:
        packet_bytes = archive.read("packet.json")
    packet = json.loads(packet_bytes)
    content_hash = packet.pop("packet_content_sha256")
    if _sha256_bytes(_canonical_json(packet)) != content_hash:
        raise ValueError(f"Blinded packet content hash mismatch for {record['packet_id']}.")
    packet["packet_content_sha256"] = content_hash
    return packet


def _validate_geometry(
    value: Any,
    *,
    allowed_types: set[str],
    field: str,
    failures: list[str],
) -> Any | None:
    if not isinstance(value, dict):
        failures.append(f"missing_{field}")
        return None
    try:
        parsed = shape(value)
    except Exception:
        failures.append(f"invalid_{field}")
        return None
    if parsed.geom_type not in allowed_types or parsed.is_empty or not parsed.is_valid:
        failures.append(f"invalid_{field}")
        return None
    if parsed.has_z or not all(math.isfinite(number) for number in parsed.bounds):
        failures.append(f"invalid_{field}")
        return None
    return parsed


def _validate_masks(component: dict[str, Any], failures: list[str]) -> bool:
    masks = component.get("observation_masks")
    if not isinstance(masks, dict) or set(masks) != MASK_NAMES:
        failures.append("incomplete_observation_masks")
        return False
    complete = True
    for name in sorted(MASK_NAMES):
        record = masks[name]
        if not isinstance(record, dict) or record.get("status") not in MASK_STATUSES:
            failures.append(f"invalid_mask_status_{name}")
            complete = False
            continue
        if not isinstance(record.get("basis"), str) or not record["basis"].strip():
            failures.append(f"missing_mask_basis_{name}")
            complete = False
        geometries = record.get("geometries")
        if not isinstance(geometries, list):
            failures.append(f"invalid_mask_geometries_{name}")
            complete = False
            continue
        if record["status"] == "mapped_present" and not geometries:
            failures.append(f"missing_mask_geometry_{name}")
            complete = False
        if record["status"] != "mapped_present" and geometries:
            failures.append(f"unexpected_mask_geometry_{name}")
            complete = False
        for index, geometry in enumerate(geometries):
            _validate_geometry(
                geometry,
                allowed_types={"Polygon", "MultiPolygon"},
                field=f"mask_{name}_{index}",
                failures=failures,
            )
    if masks["survey_coverage"].get("status") != "mapped_present":
        failures.append("survey_coverage_not_mapped")
        complete = False
    return complete


def validate_submission(
    raw: Any,
    *,
    packet_record: dict[str, Any],
    packet: dict[str, Any],
    source_path: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    failures: list[str] = []
    if not isinstance(raw, dict) or raw.get("schema") != SUBMISSION_SCHEMA:
        return None, ["invalid_submission_schema"]
    expected = {
        "packet_id": packet_record["packet_id"],
        "packet_content_sha256": packet_record["packet_content_sha256"],
        "packet_archive_sha256": packet_record["archive_sha256"],
    }
    for name, value in expected.items():
        if raw.get(name) != value:
            failures.append(f"{name}_mismatch")
    if raw.get("reviewer_slot") not in {"A", "B"}:
        failures.append("invalid_reviewer_slot")
    for field in ("reviewer_identity", "reviewer_organization", "reviewer_contact"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            failures.append(f"missing_{field}")
    _utc(raw.get("completed_at_utc"), "completed_at_utc", failures)
    for field, required in (
        ("human_completed", True),
        ("independence_attestation", True),
        ("blind_to_evaluated_outputs", True),
        ("peer_submission_accessed", False),
        ("ai_generated_only", False),
    ):
        if raw.get(field) is not required:
            failures.append(f"invalid_{field}")
    grouping = raw.get("event_grouping")
    if not isinstance(grouping, dict):
        failures.append("missing_event_grouping")
        grouping = {}
    for field in ("path_id", "mountain_id", "storm_cycle_id", "identity_basis"):
        if not isinstance(grouping.get(field), str) or not grouping[field].strip():
            failures.append(f"missing_event_grouping_{field}")
    density_transfer = raw.get("release_density_transferability")
    if not isinstance(density_transfer, dict):
        failures.append("missing_release_density_transferability")
        density_transfer = {}
    if density_transfer.get("disposition") not in {"accepted", "rejected", "not_assessed"}:
        failures.append("invalid_release_density_transferability_disposition")
    for field in ("basis", "transfer_uncertainty_statement"):
        if not isinstance(density_transfer.get(field), str) or not density_transfer[field].strip():
            failures.append(f"missing_release_density_transferability_{field}")
    density_transfer_uncertainty = density_transfer.get("transfer_uncertainty_kg_m3")
    if density_transfer.get("disposition") == "accepted" and not _finite_positive(
        density_transfer_uncertainty
    ):
        failures.append("invalid_release_density_transfer_uncertainty_kg_m3")
    elif density_transfer_uncertainty is not None and not _finite_positive(
        density_transfer_uncertainty
    ):
        failures.append("invalid_release_density_transfer_uncertainty_kg_m3")
    scene_ids = {
        item["scene_id"] for item in packet["source_imagery"] if item.get("scene_id")
    }
    target_crs = packet["chip_grid"]["crs"]
    components = raw.get("components")
    if not isinstance(components, list) or len(components) != len(FEATURE_ROLES):
        failures.append("invalid_components")
        components = []
    if {item.get("feature_role") for item in components if isinstance(item, dict)} != FEATURE_ROLES:
        failures.append("component_roles_must_be_exactly_once")
    normalized_components: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            failures.append("invalid_component_record")
            continue
        role = component.get("feature_role")
        status = component.get("observation_status")
        if role not in FEATURE_ROLES:
            failures.append("invalid_feature_role")
            continue
        if status not in {"observed", "not_supportable"}:
            failures.append(f"invalid_observation_status_{role}")
            continue
        if status == "not_supportable":
            if component.get("geometry") is not None:
                failures.append(f"unsupported_component_has_geometry_{role}")
            normalized_components.append({"feature_role": role, "observation_status": status})
            continue
        required_strings = (
            "component_id",
            "observation_method",
            "confidence_basis",
            "resolution_uncertainty_statement",
            "component_attribution",
            "normalization_method",
        )
        for field in required_strings:
            if not isinstance(component.get(field), str) or not component[field].strip():
                failures.append(f"missing_{field}_{role}")
        if component.get("confidence") not in {"low", "medium", "high"}:
            failures.append(f"invalid_confidence_{role}")
        for field in (
            "source_resolution_m",
            "horizontal_uncertainty_m",
            "horizontal_uncertainty_confidence_level",
            "temporal_uncertainty_seconds",
        ):
            if not _finite_positive(component.get(field)):
                failures.append(f"invalid_{field}_{role}")
        level = component.get("horizontal_uncertainty_confidence_level")
        if _finite_positive(level) and float(level) > 1:
            failures.append(f"invalid_horizontal_uncertainty_confidence_level_{role}")
        sources = component.get("source_scene_ids")
        if not isinstance(sources, list) or not sources or not set(sources).issubset(scene_ids):
            failures.append(f"invalid_source_scene_ids_{role}")
        for field in ("detection_limitations", "ambiguity_exclusions"):
            if not isinstance(component.get(field), list):
                failures.append(f"invalid_{field}_{role}")
        if component.get("geometry_crs") != target_crs:
            failures.append(f"geometry_crs_mismatch_{role}")
        if component.get("coordinate_order") != "easting_northing":
            failures.append(f"coordinate_order_mismatch_{role}")
        allowed = {"Point"} if role == "terminal_dense_flow_toe" else {"Polygon", "MultiPolygon"}
        parsed = _validate_geometry(
            component.get("geometry"),
            allowed_types=allowed,
            field=f"geometry_{role}",
            failures=failures,
        )
        masks_complete = _validate_masks(component, failures)
        if component.get("review_disposition") != "accepted":
            failures.append(f"review_disposition_not_accepted_{role}")
        normalized_components.append(
            {
                **component,
                "parsed_geometry": parsed,
                "masks_complete": masks_complete,
            }
        )
    identity = raw.get("reviewer_identity")
    normalized = {
        "source_path": _stable_path(source_path),
        "source_sha256": _sha256_file(source_path),
        "reviewer_slot": raw.get("reviewer_slot"),
        "reviewer_identity_sha256": (
            _sha256_bytes(str(identity).strip().encode("utf-8"))
            if isinstance(identity, str) and identity.strip()
            else None
        ),
        "event_grouping": grouping,
        "release_density_transferability": density_transfer,
        "components": normalized_components,
    }
    return (normalized if not failures else None), failures


def _verification(
    path: Path,
    *,
    packet_id: str,
    reviewer_hashes: list[str],
) -> tuple[bool, list[str], dict[str, Any] | None]:
    failures: list[str] = []
    if not path.is_file():
        return False, ["missing_independent_identity_verification"], None
    raw = json.loads(path.read_bytes())
    if raw.get("schema") != VERIFICATION_SCHEMA:
        failures.append("invalid_identity_verification_schema")
    if raw.get("packet_id") != packet_id:
        failures.append("identity_verification_packet_mismatch")
    if sorted(raw.get("reviewer_identity_sha256s") or []) != sorted(reviewer_hashes):
        failures.append("identity_verification_reviewer_mismatch")
    for field in ("verifier_identity", "verification_method"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            failures.append(f"missing_{field}")
    _utc(raw.get("verified_at_utc"), "verified_at_utc", failures)
    for field in (
        "reviewers_are_genuine_humans",
        "reviewers_independent_of_project_model",
        "reviewers_independent_of_each_other",
    ):
        if raw.get(field) is not True:
            failures.append(f"{field}_not_verified")
    return not failures, failures, raw


def _component_agreement(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    role = first["feature_role"]
    if first["observation_status"] != "observed" or second["observation_status"] != "observed":
        return {
            "feature_role": role,
            "accepted": False,
            "reason": "both_reviewers_did_not_independently_support_the_component",
        }
    a = first["parsed_geometry"]
    b = second["parsed_geometry"]
    uncertainty_sum = float(first["horizontal_uncertainty_m"]) + float(
        second["horizontal_uncertainty_m"]
    )
    if role == "terminal_dense_flow_toe":
        distance = float(a.distance(b))
        passed = distance <= uncertainty_sum
        comparison = {
            "distance_m": distance,
            "maximum_distance_m": uncertainty_sum,
        }
    else:
        union_area = float(a.union(b).area)
        iou = float(a.intersection(b).area / union_area) if union_area > 0 else 0.0
        hausdorff = float(a.hausdorff_distance(b))
        passed = iou >= 0.5 and hausdorff <= uncertainty_sum
        comparison = {
            "intersection_over_union": iou,
            "minimum_intersection_over_union": 0.5,
            "hausdorff_distance_m": hausdorff,
            "maximum_hausdorff_distance_m": uncertainty_sum,
        }
    masks_complete = first["masks_complete"] and second["masks_complete"]
    accepted = passed and masks_complete
    accepted_component = (
        {key: value for key, value in first.items() if key != "parsed_geometry"}
        if accepted
        else None
    )
    return {
        "feature_role": role,
        "accepted": accepted,
        "agreement": comparison,
        "both_mask_sets_complete": masks_complete,
        "accepted_geometry_source": "reviewer_slot_A" if accepted else None,
        "accepted_component_evidence": accepted_component,
    }


def build_review_status(
    packets_path: Path,
    review_root: Path,
    verification_root: Path,
) -> dict[str, Any]:
    packet_bytes = packets_path.read_bytes()
    packets = json.loads(packet_bytes)
    if packets.get("schema") != "avycore-blinded-observation-packets-v4":
        raise ValueError("Unexpected blinded-packet schema.")
    by_packet = {record["packet_id"]: record for record in packets["packets"]}
    submission_files = sorted(review_root.rglob("*.json")) if review_root.is_dir() else []
    grouped: dict[str, list[Path]] = {packet_id: [] for packet_id in by_packet}
    unknown_submissions: list[str] = []
    for path in submission_files:
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            unknown_submissions.append(_stable_path(path))
            continue
        packet_id = raw.get("packet_id")
        if packet_id not in grouped:
            unknown_submissions.append(_stable_path(path))
        else:
            grouped[packet_id].append(path)
    candidates: list[dict[str, Any]] = []
    for packet_id, record in sorted(by_packet.items()):
        packet = _packet_payload(record)
        valid: list[dict[str, Any]] = []
        failures: list[str] = []
        submission_lineage: list[dict[str, Any]] = []
        for path in grouped[packet_id]:
            raw = json.loads(path.read_bytes())
            normalized, item_failures = validate_submission(
                raw, packet_record=record, packet=packet, source_path=path
            )
            failures.extend(f"{path.name}:{failure}" for failure in item_failures)
            submission_lineage.append(
                {
                    "source_path": _stable_path(path),
                    "source_sha256": _sha256_file(path),
                    "valid": normalized is not None,
                    "failures": sorted(set(item_failures)),
                }
            )
            if normalized is not None:
                valid.append(normalized)
        slots = [item["reviewer_slot"] for item in valid]
        identity_hashes = [
            item["reviewer_identity_sha256"]
            for item in valid
            if item["reviewer_identity_sha256"]
        ]
        pair_valid = (
            len(valid) == 2
            and sorted(slots) == ["A", "B"]
            and len(set(identity_hashes)) == 2
        )
        if len(valid) == 2 and len(set(identity_hashes)) != 2:
            failures.append("reviewer_identities_not_independent")
        verification_path = verification_root / f"{packet_id}.json"
        verified, verification_failures, verification = _verification(
            verification_path,
            packet_id=packet_id,
            reviewer_hashes=identity_hashes,
        )
        failures.extend(verification_failures)
        agreements: list[dict[str, Any]] = []
        if pair_valid and verified:
            by_slot = {item["reviewer_slot"]: item for item in valid}
            a = {item["feature_role"]: item for item in by_slot["A"]["components"]}
            b = {item["feature_role"]: item for item in by_slot["B"]["components"]}
            agreements = [_component_agreement(a[role], b[role]) for role in sorted(FEATURE_ROLES)]
        accepted_roles = {
            item["feature_role"] for item in agreements if item["accepted"]
        }
        review_complete = pair_valid and verified
        target_crs = packet["chip_grid"]["crs"]
        groupings_match = False
        density_transfer_accepted = False
        accepted_grouping: dict[str, Any] | None = None
        if pair_valid and verified:
            by_slot = {item["reviewer_slot"]: item for item in valid}
            first_grouping = by_slot["A"]["event_grouping"]
            second_grouping = by_slot["B"]["event_grouping"]
            groupings_match = all(
                first_grouping.get(field) == second_grouping.get(field)
                for field in ("path_id", "mountain_id", "storm_cycle_id")
            )
            if groupings_match:
                accepted_grouping = first_grouping
            density_transfer_accepted = all(
                by_slot[slot]["release_density_transferability"].get("disposition")
                == "accepted"
                for slot in ("A", "B")
            )
        candidates.append(
            {
                "candidate_id": record["candidate_id"],
                "packet_id": packet_id,
                "submission_files_received": len(grouped[packet_id]),
                "review_submission_lineage": submission_lineage,
                "valid_isolated_human_submissions": len(valid),
                "independent_identity_verification_complete": verified,
                "identity_verification_sha256": (
                    _sha256_file(verification_path) if verified else None
                ),
                "identity_verification_path": (
                    _stable_path(verification_path) if verified else None
                ),
                "independent_human_review_complete": review_complete,
                "accepted_quantitative_components": len(accepted_roles),
                "accepted_component_roles": sorted(accepted_roles),
                "component_agreement": agreements,
                "required_pixel_masks_resolved": bool(accepted_roles)
                and all(item["both_mask_sets_complete"] for item in agreements if item["accepted"]),
                "release_geometry_mapping_method_and_uncertainty_complete": "release" in accepted_roles,
                "projected_metre_target_crs_and_transform_frozen": (
                    bool(accepted_roles) and target_crs in REVIEWED_PROJECTED_METRE_CRS
                ),
                "path_mountain_storm_identities_frozen": groupings_match,
                "accepted_event_grouping": accepted_grouping,
                "release_density_transferability_accepted": density_transfer_accepted,
                "independent_reviewed_release_polygon": "release" in accepted_roles,
                "release_observation_confidence_quantified": "release" in accepted_roles,
                "reviewed_dense_flow_deposit_or_terminal_toe": bool(
                    {"dense_flow_deposit", "terminal_dense_flow_toe"} & accepted_roles
                ),
                "dense_flow_component_attribution_complete": bool(
                    {"dense_flow_deposit", "terminal_dense_flow_toe"} & accepted_roles
                ),
                "survey_coverage_and_detection_masks_complete": bool(accepted_roles)
                and all(item["both_mask_sets_complete"] for item in agreements if item["accepted"]),
                "failures": sorted(set(failures)),
                "ai_generated_only": False,
                "human_evidence_fabricated_by_importer": False,
            }
        )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_packet_manifest_sha256": _sha256_bytes(packet_bytes),
        "review_root": _stable_path(review_root),
        "identity_verification_root": _stable_path(verification_root),
        "unknown_or_invalid_submission_paths": unknown_submissions,
        "importer_created_geometry": False,
        "importer_created_reviewer_identity": False,
        "ai_can_satisfy_human_review": False,
        "counts": {
            "packets": len(candidates),
            "submission_files": len(submission_files),
            "independent_human_reviews_complete": sum(
                item["independent_human_review_complete"] for item in candidates
            ),
            "accepted_quantitative_components": sum(
                item["accepted_quantitative_components"] for item in candidates
            ),
        },
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    base = REPOSITORY_ROOT / "validation-data" / "candidates"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, default=base / "blinded-observation-packets-v4.json")
    parser.add_argument("--review-root", type=Path, required=True)
    parser.add_argument("--identity-verification-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=base / "public-event-human-review-status-v5.json")
    parser.add_argument("--minimum-complete", type=int, default=0)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def _supplied_review_failures_present(artifact: dict[str, Any]) -> bool:
    return bool(artifact["unknown_or_invalid_submission_paths"]) or any(
        candidate["failures"] and candidate["submission_files_received"] > 0
        for candidate in artifact["candidates"]
    )


def main() -> None:
    args = _arguments()
    artifact = build_review_status(
        args.packets.resolve(),
        args.review_root.resolve(),
        args.identity_verification_root.resolve(),
    )
    if _supplied_review_failures_present(artifact):
        raise ValueError("One or more supplied review records failed validation.")
    complete = artifact["counts"]["independent_human_reviews_complete"]
    if complete < args.minimum_complete:
        raise ValueError(
            f"Only {complete} independent review pairs passed; minimum requested is {args.minimum_complete}."
        )
    if not args.check_only:
        _write_immutable(args.output.resolve(), _pretty_json(artifact))
    print(
        f"Validated {complete} independent review pairs and "
        f"{artifact['counts']['accepted_quantitative_components']} components; "
        f"check_only={args.check_only}."
    )


if __name__ == "__main__":
    main()
