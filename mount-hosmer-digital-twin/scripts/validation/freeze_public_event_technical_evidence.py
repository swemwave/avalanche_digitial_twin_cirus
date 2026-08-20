"""Freeze technically derivable public-event evidence without inventing observations.

This stage normalizes provider times to UTC, freezes source/grid identities and
the release-to-runout rule, verifies SHA-256 lineage, and reports every numeric
uncertainty available from the immutable inputs.  Unknown ambiguity,
attribution, coverage, terrain mismatch, thickness, and density-transfer error
remain explicit nulls and failed gates rather than safe-looking zeroes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-technical-evidence-v2"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
REVIEWED_PROJECTED_METRE_CRS = {"EPSG:2056", "EPSG:26911", "EPSG:32613"}


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


def _read(path: Path, schema: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    parsed = json.loads(payload)
    if parsed.get("schema") != schema:
        raise ValueError(f"Unexpected schema in {path}: {parsed.get('schema')!r}.")
    return parsed, _sha256_bytes(payload)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Provider event time lacks an explicit offset: {value!r}.")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _event_time(candidate: dict[str, Any]) -> dict[str, Any]:
    source = candidate["event_time"]
    latest_raw = source.get("provider_latest")
    if not isinstance(latest_raw, str):
        return {
            "status": "unknown",
            "event_start_utc": None,
            "event_end_utc": None,
            "event_time_confidence": None,
            "confidence_basis": "Provider latest-event time is absent.",
            "interval_seconds": None,
            "contract_ready": False,
        }
    end = _utc(latest_raw)
    earliest_raw = source.get("provider_earliest")
    start = _utc(earliest_raw) if isinstance(earliest_raw, str) else end
    if end < start:
        return {
            "status": "invalid_provider_order",
            "event_start_utc": _utc_text(start),
            "event_end_utc": _utc_text(end),
            "event_time_confidence": None,
            "confidence_basis": "Provider latest-event time precedes earliest-event time.",
            "interval_seconds": None,
            "contract_ready": False,
        }
    interval_seconds = int((end - start).total_seconds())
    confidence = "medium" if earliest_raw and interval_seconds <= 72 * 3600 else "low"
    return {
        "status": "bounded" if interval_seconds else "known",
        "event_start_utc": _utc_text(start),
        "event_end_utc": _utc_text(end),
        "event_time_confidence": confidence,
        "confidence_basis": (
            "Medium denotes an explicit provider earliest/latest interval no longer than "
            "72 hours. Low denotes a single provider event time or a wider interval. "
            "Neither category is independent timing verification."
        ),
        "interval_seconds": interval_seconds,
        "provider_earliest_preserved": earliest_raw,
        "provider_latest_preserved": latest_raw,
        "provider_observation_time_preserved": source.get("provider_observation_time"),
        "contract_ready": True,
    }


def _selected_pair_uncertainty(acquisition: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for sensor in ("sentinel_1_grd", "sentinel_2_l2a"):
        pair = (acquisition[sensor] or {}).get("selected_pair")
        if pair:
            result.append(
                {
                    "sensor": sensor,
                    "pre_to_event_start_seconds": pair.get("pre_to_event_start_seconds"),
                    "event_end_to_post_seconds": pair.get("event_end_to_post_seconds"),
                    "temporal_baseline_seconds": pair.get("temporal_baseline_seconds"),
                }
            )
    return result


def _mask_uncertainty(qa: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for sensor in ("sentinel_1_grd", "sentinel_2_l2a"):
        sensor_qa = qa[sensor]
        stack = sensor_qa.get("mask_stack")
        result[sensor] = {
            "status": sensor_qa["status"],
            "counts": (stack or {}).get("counts"),
            "total_pixels": sensor_qa.get("total_pixels"),
            "source_valid_pixels": sensor_qa.get("source_valid_pixels"),
            "required_masks_resolved": sensor_qa.get("required_masks_resolved", False),
            "survey_coverage_fraction": None,
            "component_detection_coverage_fraction": None,
            "null_fraction_reason": (
                "No independent component geometry or complete-search survey polygon exists; "
                "the zero-valued inclusion raster means unasserted coverage, not measured zero coverage."
            ),
        }
    return result


def _verify_packet(record: dict[str, Any]) -> dict[str, Any]:
    path = Path(record["archive_path"])
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    actual = _sha256_file(path)
    if actual != record["archive_sha256"]:
        raise ValueError(f"Blinded packet SHA-256 mismatch at {path}.")
    return {
        "path": path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix(),
        "sha256": actual,
        "bytes": path.stat().st_size,
        "verified": True,
    }


def build_technical_evidence(
    candidate_path: Path,
    acquisition_path: Path,
    qa_path: Path,
    packet_path: Path,
    evidence_path: Path,
    terrain_path: Path,
    release_state_path: Path,
    rule_path: Path,
) -> dict[str, Any]:
    candidates, candidate_sha = _read(
        candidate_path, "avycore-public-event-candidate-funnel-v1"
    )
    acquisition, acquisition_sha = _read(
        acquisition_path, "avycore-public-event-imagery-acquisition-v2"
    )
    qa, qa_sha = _read(qa_path, "avycore-public-event-pixel-qa-v3")
    packets, packet_sha = _read(packet_path, "avycore-blinded-observation-packets-v4")
    evidence, evidence_sha = _read(
        evidence_path, "avycore-public-regobs-blinded-evidence-v1"
    )
    terrain, terrain_sha = _read(
        terrain_path, "avycore-public-event-terrain-acquisition-v1"
    )
    release_state, release_state_sha = _read(
        release_state_path, "avycore-public-release-state-evidence-v1"
    )
    rule_bytes = rule_path.read_bytes()
    rule = json.loads(rule_bytes)
    if rule.get("schema") != "avycore-release-to-runout-rule-v1":
        raise ValueError("Unexpected release-to-runout rule schema.")
    rule_sha = _sha256_bytes(rule_bytes)
    collections = {
        "candidate": {item["candidate_id"]: item for item in candidates["candidates"]},
        "acquisition": {item["candidate_id"]: item for item in acquisition["candidates"]},
        "qa": {item["candidate_id"]: item for item in qa["candidates"]},
        "packet": {item["candidate_id"]: item for item in packets["packets"]},
        "evidence": {item["candidate_id"]: item for item in evidence["candidates"]},
        "terrain": {item["candidate_id"]: item for item in terrain["candidates"]},
        "release_state": {
            item["candidate_id"]: item for item in release_state["crown_height_semantics"]
        },
    }
    identities = set(collections["acquisition"])
    for label, collection in collections.items():
        if label == "candidate":
            if not identities.issubset(collection):
                raise ValueError("Candidate inventory lacks acquired candidate identities.")
        elif set(collection) != identities:
            raise ValueError(f"Technical-evidence candidate identities differ in {label}.")
    records: list[dict[str, Any]] = []
    for candidate_id in sorted(identities):
        candidate = collections["candidate"][candidate_id]
        acquired = collections["acquisition"][candidate_id]
        candidate_qa = collections["qa"][candidate_id]
        packet = collections["packet"][candidate_id]
        source = collections["evidence"][candidate_id]
        terrain_record = collections["terrain"][candidate_id]
        thickness = collections["release_state"][candidate_id]
        event_time = _event_time(candidate)
        target_grid = acquired["chip_grid"]
        crs_allowlisted = target_grid["crs"] in REVIEWED_PROJECTED_METRE_CRS
        packet_lineage = _verify_packet(packet)
        source_record_path = Path(source["source_record_cache_path"])
        if not source_record_path.is_absolute():
            source_record_path = REPOSITORY_ROOT / source_record_path
        if _sha256_file(source_record_path) != source["source_record_file_sha256"]:
            raise ValueError(f"RegObs source-record SHA-256 mismatch at {source_record_path}.")
        terrain_mismatch = terrain_record.get("event_surface_mismatch") or {}
        records.append(
            {
                "candidate_id": candidate_id,
                "event_time": event_time,
                "identities": {
                    "event_id": candidate_id,
                    "provider_region_candidate_id": candidate.get("mountain_group_id"),
                    "path_id": None,
                    "mountain_id": None,
                    "storm_cycle_id": None,
                    "status": (
                        "event identity frozen; independent path, mountain, and storm "
                        "identities require human/source review"
                    ),
                },
                "target_grid": {
                    **target_grid,
                    "transform": next(
                        (
                            asset["raster"]["transform"]
                            for scene in acquired["sentinel_1_grd"].get("scenes") or []
                            for asset in scene.get("assets") or []
                            if asset.get("raster")
                        ),
                        None,
                    ),
                    "source_geometry_crs": "EPSG:4326",
                    "coordinate_operation": (
                        "PROJ always_xy longitude/latitude to the frozen per-candidate UTM grid; "
                        "no provider target geometry is accepted by this operation"
                    ),
                    "contract_v3_crs_allowlisted": crs_allowlisted,
                },
                "release_to_runout_rule": {
                    "path": rule_path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix(),
                    "sha256": rule_sha,
                    "frozen_without_runout_target": rule[
                        "observed_release_geometry_allowed_on_prediction_path"
                    ]
                    is False,
                },
                "lineage": {
                    "source_record_sha256": source["source_record_file_sha256"],
                    "attachment_sha256": [
                        item["acquisition"]["sha256"] for item in source["attachments"]
                    ],
                    "packet_archive": packet_lineage,
                    "acquisition_candidate_sha256": acquired["normalized_candidate_sha256"],
                    "pixel_qa_candidate_sha256": candidate_qa["normalized_candidate_sha256"],
                    "terrain_candidate_sha256": terrain_record["normalized_candidate_sha256"],
                    "release_state_source_record_sha256": thickness["source_record_sha256"],
                    "all_declared_local_sha256_verified": True,
                },
                "uncertainties": {
                    "event_interval_seconds": event_time["interval_seconds"],
                    "imagery_temporal_gaps": _selected_pair_uncertainty(acquired),
                    "nominal_imagery_resolution_m": target_grid["resolution_m"],
                    "pixel_masks": _mask_uncertainty(candidate_qa),
                    "component_ambiguity": {
                        "status": "unknown_pending_independent_human_review",
                        "numeric_bound": None,
                    },
                    "component_attribution": {
                        "status": "unknown_pending_independent_human_review",
                        "numeric_bound": None,
                    },
                    "terrain_surface_mismatch": terrain_mismatch,
                    "release_thickness": {
                        "reported_crown_height_m_unit_conversion_only": thickness.get(
                            "provider_value_m_unit_conversion_only"
                        ),
                        "normal_to_slope_thickness_m": None,
                        "measurement_uncertainty_m": None,
                        "eligible": thickness[
                            "validation_contract_v3_release_thickness_evidence_eligible"
                        ],
                        "reason": thickness["exclusion_reason"],
                    },
                    "release_density": {
                        "lower_kg_m3": release_state["release_density_evidence"]["lower"],
                        "upper_kg_m3": release_state["release_density_evidence"]["upper"],
                        "event_specific": False,
                        "geographic_transfer_error_kg_m3": None,
                        "transfer_limitations": release_state["release_density_evidence"][
                            "transfer_limitations"
                        ],
                    },
                    "missing_numeric_bounds_are_null_not_zero": True,
                },
                "checks": {
                    "bounded_event_time_with_confidence": event_time["contract_ready"],
                    "projected_metre_target_crs_and_transform_frozen": bool(
                        target_grid.get("crs")
                        and target_grid.get("coordinate_order") == "easting_northing"
                        and crs_allowlisted
                    ),
                    "event_surface_terrain_eligible": terrain_record[
                        "validation_contract_v3_terrain_eligible"
                    ]
                    is True,
                    "path_mountain_storm_identities_frozen": False,
                    "normal_to_slope_release_thickness_evidence": thickness[
                        "validation_contract_v3_release_thickness_evidence_eligible"
                    ]
                    is True,
                    "release_density_transferability_accepted": False,
                    "provenance_bearing_release_model_inputs_complete": False,
                    "release_to_runout_rule_frozen": True,
                },
            }
        )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_artifacts": {
            "candidate_inventory_sha256": candidate_sha,
            "imagery_acquisition_sha256": acquisition_sha,
            "pixel_qa_sha256": qa_sha,
            "packet_manifest_sha256": packet_sha,
            "regobs_evidence_sha256": evidence_sha,
            "terrain_acquisition_sha256": terrain_sha,
            "release_state_evidence_sha256": release_state_sha,
            "release_to_runout_rule_sha256": rule_sha,
        },
        "event_time_confidence_rule": (
            "medium for explicit provider earliest/latest intervals <=72 h; low for a single "
            "provider time or a wider interval; neither means independently verified"
        ),
        "claim_boundary": (
            "Technical freezing and complete hashes do not create human-reviewed geometry, "
            "survey coverage, release state, event-surface terrain, or forcing evidence."
        ),
        "counts": {
            "candidates": len(records),
            "bounded_event_times": sum(
                item["checks"]["bounded_event_time_with_confidence"] for item in records
            ),
            "contract_allowlisted_target_grids": sum(
                item["checks"]["projected_metre_target_crs_and_transform_frozen"]
                for item in records
            ),
            "event_surface_terrain_eligible": sum(
                item["checks"]["event_surface_terrain_eligible"] for item in records
            ),
            "release_thickness_eligible": sum(
                item["checks"]["normal_to_slope_release_thickness_evidence"]
                for item in records
            ),
            "release_to_runout_rules_frozen": sum(
                item["checks"]["release_to_runout_rule_frozen"] for item in records
            ),
        },
        "candidates": records,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    base = REPOSITORY_ROOT / "validation-data" / "candidates"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=base / "public-event-candidates-v1.json")
    parser.add_argument("--acquisition", type=Path, default=base / "public-event-imagery-acquisition-v2.json")
    parser.add_argument("--pixel-qa", type=Path, default=base / "public-event-pixel-qa-v3.json")
    parser.add_argument("--packets", type=Path, default=base / "blinded-observation-packets-v4.json")
    parser.add_argument("--evidence", type=Path, default=base / "public-regobs-blinded-evidence-v1.json")
    parser.add_argument("--terrain", type=Path, default=base / "public-event-terrain-acquisition-v1.json")
    parser.add_argument(
        "--release-state",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "experiments" / "public-release-state-evidence-v1.json",
    )
    parser.add_argument(
        "--release-to-runout-rule",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "protocols" / "public-event-release-to-runout-rule-v1.json",
    )
    parser.add_argument("--output", type=Path, default=base / "public-event-technical-evidence-v2.json")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_technical_evidence(
        args.candidates.resolve(),
        args.acquisition.resolve(),
        args.pixel_qa.resolve(),
        args.packets.resolve(),
        args.evidence.resolve(),
        args.terrain.resolve(),
        args.release_state.resolve(),
        args.release_to_runout_rule.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Froze technical evidence for {artifact['counts']['candidates']} candidates; "
        f"bounded times={artifact['counts']['bounded_event_times']}, "
        f"v3 target grids={artifact['counts']['contract_allowlisted_target_grids']}, "
        "human observations created=0."
    )


if __name__ == "__main__":
    main()
