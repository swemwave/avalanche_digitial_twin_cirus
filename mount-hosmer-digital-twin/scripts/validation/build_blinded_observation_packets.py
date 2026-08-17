"""Build immutable, portable, blinded public-observation annotation packets.

Each released ZIP contains the acquired source imagery, source attachments,
explicit QA masks, hash-bearing metadata, instructions, and blank uncertainty
and component forms.  It contains no evaluated output, provider target geometry,
other reviewer work, fitted setting, or acceptance record.  Release for human
annotation is intentionally distinct from acceptance as quantitative evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-blinded-observation-packets-v4"
PACKET_SCHEMA = "avycore-blinded-observation-packet-v4"
FORM_SCHEMA = "avycore-independent-observation-review-v1"
PACKET_SET_ID = "public-event-blinded-observation-packets-v4"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
FORBIDDEN_PACKET_TOKENS = (
    "model layer",
    "model result",
    "prediction",
    "simulation",
    "alpha line",
    "parameter result",
    "hazard score",
    "runout result",
    "other reviewer",
)


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


def _write_immutable(path: Path, value: bytes) -> None:
    if path.exists():
        if path.read_bytes() != value:
            raise ValueError(f"Immutable blinded-packet conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError(f"Concurrent blinded-packet conflict at {path}.")


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _assert_outside_protected(path: Path, label: str) -> None:
    resolved = path.resolve()
    for protected in (
        REPOSITORY_ROOT / "runtime",
        REPOSITORY_ROOT / "DATA",
        REPOSITORY_ROOT.parent / "DATA",
    ):
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        raise ValueError(f"{label} may not be under protected path {protected}.")


def _assert_blind(packet: dict[str, Any]) -> None:
    serialized = _canonical_json(packet).decode("utf-8").lower()
    leaked = [token for token in FORBIDDEN_PACKET_TOKENS if token in serialized]
    if leaked:
        raise ValueError(f"Blinded packet contains forbidden content: {leaked}.")
    if packet.get("schema") != PACKET_SCHEMA:
        raise ValueError("Unexpected blinded packet schema.")
    if "candidate_id" in packet or "regobs-" in serialized:
        raise ValueError("Blinded packet leaks the internal candidate identity.")


def _cache_path(reference: str, expected_sha256: str) -> Path:
    path = Path(reference)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to((REPOSITORY_ROOT / ".validation-cache").resolve())
    except ValueError as exc:
        raise ValueError(f"Packet source is outside the validation cache: {reference!r}.") from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"Packet source is missing: {resolved}.")
    if _sha256_file(resolved) != expected_sha256:
        raise ValueError(f"Packet source SHA-256 mismatch at {resolved}.")
    return resolved


def _suffix(asset_name: str, path: Path) -> str:
    suffixes = "".join(path.suffixes)
    return suffixes or f"-{asset_name}.bin"


def _imagery_sources(acquisition: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    records: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    for sensor_name in ("sentinel_1_grd", "sentinel_2_l2a"):
        sensor = acquisition[sensor_name]
        for scene in sensor.get("scenes") or []:
            for index, asset in enumerate(scene.get("assets") or []):
                reference = asset.get("cache_path") or (asset.get("cache") or {}).get(
                    "response_cache_path"
                )
                if not reference:
                    continue
                path = _cache_path(reference, asset["sha256"])
                archive_path = (
                    f"source-imagery/{sensor_name}/{scene['position']}/"
                    f"{index:02d}-{asset['asset_name']}{_suffix(asset['asset_name'], path)}"
                )
                files.append((archive_path, path))
                records.append(
                    {
                        "sensor": sensor_name,
                        "scene_position": scene["position"],
                        "scene_id": scene.get("copernicus_item_id")
                        or scene.get("earth_search_item_id"),
                        "acquisition_time_utc": scene["acquisition_time_utc"],
                        "asset_name": asset["asset_name"],
                        "archive_path": archive_path,
                        "bytes": path.stat().st_size,
                        "sha256": asset["sha256"],
                        "source_href": asset.get("source_href"),
                        "raster": asset.get("raster"),
                    }
                )
    return records, files


def _attachment_sources(evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    records: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    for index, attachment in enumerate(evidence.get("attachments") or []):
        acquisition = attachment["acquisition"]
        path = _cache_path(acquisition["cache_path"], acquisition["sha256"])
        archive_path = f"source-attachments/{index:03d}{path.suffix.lower()}"
        files.append((archive_path, path))
        records.append(
            {
                "source_attachment_id": attachment["attachment_id"],
                "archive_path": archive_path,
                "bytes": path.stat().st_size,
                "sha256": acquisition["sha256"],
                "mime_type": attachment.get("mime_type"),
                "source_url": attachment.get("source_url"),
                "comment": attachment.get("comment"),
                "image": attachment.get("image"),
                "source_marks_are_candidate_evidence_only": True,
            }
        )
    return records, files


def _mask_sources(qa: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, Path]]]:
    records: list[dict[str, Any]] = []
    files: list[tuple[str, Path]] = []
    for sensor_name in ("sentinel_1_grd", "sentinel_2_l2a"):
        sensor = qa[sensor_name]
        stack = sensor.get("mask_stack")
        if not stack:
            continue
        path = _cache_path(stack["cache_path"], stack["sha256"])
        archive_path = f"qa-masks/{sensor_name}-masks.tif"
        files.append((archive_path, path))
        records.append(
            {
                "sensor": sensor_name,
                "archive_path": archive_path,
                "sha256": stack["sha256"],
                "mask_payload_sha256": stack["mask_payload_sha256"],
                "bands": stack["bands"],
                "counts": stack["counts"],
                "semantics": sensor["mask_semantics"],
                "technical_pixel_qa_complete": sensor.get(
                    "technical_pixel_qa_complete", False
                ),
                "required_masks_resolved": sensor.get(
                    "required_masks_resolved", False
                ),
            }
        )
    return records, files


def _blank_review_form(packet_id: str, packet_content_sha256: str, slot: str) -> dict[str, Any]:
    mask_names = (
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
    )
    return {
        "schema": FORM_SCHEMA,
        "packet_id": packet_id,
        "packet_content_sha256": packet_content_sha256,
        "packet_archive_sha256": None,
        "reviewer_slot": slot,
        "reviewer_identity": None,
        "reviewer_organization": None,
        "reviewer_contact": None,
        "completed_at_utc": None,
        "human_completed": None,
        "independence_attestation": None,
        "blind_to_evaluated_outputs": None,
        "peer_submission_accessed": None,
        "ai_generated_only": None,
        "event_grouping": {
            "path_id": None,
            "mountain_id": None,
            "storm_cycle_id": None,
            "identity_basis": None,
        },
        "release_density_transferability": {
            "disposition": None,
            "basis": None,
            "transfer_uncertainty_statement": None,
            "transfer_uncertainty_kg_m3": None,
        },
        "components": [
            {
                "component_id": role,
                "feature_role": role,
                "observation_status": None,
                "source_scene_ids": [],
                "observation_method": None,
                "confidence": None,
                "confidence_basis": None,
                "source_resolution_m": None,
                "horizontal_uncertainty_m": None,
                "horizontal_uncertainty_confidence_level": None,
                "temporal_uncertainty_seconds": None,
                "resolution_uncertainty_statement": None,
                "detection_limitations": [],
                "ambiguity_exclusions": [],
                "component_attribution": None,
                "geometry_crs": None,
                "coordinate_order": "easting_northing",
                "normalization_method": None,
                "geometry": None,
                "observation_masks": {
                    name: {"status": None, "geometries": [], "basis": None}
                    for name in mask_names
                },
                "review_disposition": None,
            }
            for role in ("release", "dense_flow_deposit", "terminal_dense_flow_toe")
        ],
    }


INSTRUCTIONS = """# Independent avalanche-observation annotation

Work only from this packet. Do not open evaluated outputs or peer submissions.
Complete exactly one reviewer form in your assigned slot.

For release, dense-flow deposit, and terminal dense-flow toe, record either an
observed geometry or `not_supportable`; null never means an observed absence.
Map survey coverage and every invalid area caused by missing pixels, scene edge,
detection limits, cloud, cloud shadow, topographic/cast shadow, forest, water,
layover, radar shadow, prior deposits, overlap, or attribution ambiguity. For
each named mask use `mapped_present`, `checked_absent`, or `not_applicable`, give
a basis, and include geometries for `mapped_present`. Survey coverage must be a
mapped polygon; it is never inferred from the image boundary.

Record source scenes, method, effective resolution, horizontal uncertainty and
confidence level, temporal uncertainty, limitations, attribution, CRS, axis
order, and coordinate-operation lineage. Do not reuse provider marks without
declaring them. If accepting the transferred density prior, quantify its
transfer uncertainty in kg/m3. Do not use AI-created geometry as a human
observation.
"""


def _zip_bytes(entries: Iterable[tuple[str, bytes | Path]]) -> bytes:
    import io

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for archive_path, value in sorted(entries, key=lambda item: item[0]):
            payload = value.read_bytes() if isinstance(value, Path) else value
            info = zipfile.ZipInfo(archive_path, date_time=(2026, 8, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload)
    return output.getvalue()


def _packet(
    acquisition: dict[str, Any], qa: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    candidate_id = acquisition["candidate_id"]
    if candidate_id != qa["candidate_id"] or candidate_id != evidence["candidate_id"]:
        raise ValueError("Packet input candidate identities differ.")
    identity_seed = {
        "candidate_id": candidate_id,
        "acquisition_sha256": acquisition["normalized_candidate_sha256"],
        "qa_sha256": qa["normalized_candidate_sha256"],
        "evidence_sha256": evidence["normalized_candidate_sha256"],
    }
    packet_id = f"blind-{_sha256_bytes(_canonical_json(identity_seed))[:20]}"
    imagery, imagery_files = _imagery_sources(acquisition)
    attachments, attachment_files = _attachment_sources(evidence)
    masks, mask_files = _mask_sources(qa)
    packet: dict[str, Any] = {
        "schema": PACKET_SCHEMA,
        "packet_id": packet_id,
        "frozen_at_utc": FROZEN_AT_UTC,
        "chip_grid": acquisition["chip_grid"],
        "source_imagery": imagery,
        "source_attachments": attachments,
        "qa_masks": masks,
        "tasks": ["release", "dense_flow_deposit", "terminal_dense_flow_toe"],
        "uncertainty_form_fields": [
            "effective source resolution",
            "horizontal boundary uncertainty and confidence level",
            "temporal uncertainty",
            "detection and survey coverage",
            "ambiguity exclusions",
            "component attribution",
            "coordinate-operation lineage",
        ],
        "blinding": {
            "evaluated_outputs_included": False,
            "peer_submissions_included": False,
            "provider_target_geometry_included": False,
            "internal_candidate_identity_included": False,
        },
        "release_scope": (
            "Released for independent annotation only. It is not accepted quantitative "
            "evidence and cannot satisfy validation-contract v3 without two genuine, "
            "isolated human reviews and all remaining source-evidence gates."
        ),
    }
    _assert_blind(packet)
    content_sha = _sha256_bytes(_canonical_json(packet))
    packet["packet_content_sha256"] = content_sha
    return packet, imagery_files + attachment_files + mask_files


def build_packets(
    acquisition_path: Path,
    qa_path: Path,
    evidence_path: Path,
    cache_root: Path,
) -> dict[str, Any]:
    acquisition_bytes = acquisition_path.read_bytes()
    qa_bytes = qa_path.read_bytes()
    evidence_bytes = evidence_path.read_bytes()
    acquisition = json.loads(acquisition_bytes)
    qa = json.loads(qa_bytes)
    evidence = json.loads(evidence_bytes)
    if acquisition.get("schema") != "avycore-public-event-imagery-acquisition-v2":
        raise ValueError("Unexpected imagery acquisition schema.")
    if qa.get("schema") != "avycore-public-event-pixel-qa-v3":
        raise ValueError("Unexpected pixel-QA schema.")
    if evidence.get("schema") != "avycore-public-regobs-blinded-evidence-v1":
        raise ValueError("Unexpected source-evidence schema.")
    qa_by_id = {item["candidate_id"]: item for item in qa["candidates"]}
    evidence_by_id = {item["candidate_id"]: item for item in evidence["candidates"]}
    records: list[dict[str, Any]] = []
    for acquisition_candidate in acquisition["candidates"]:
        candidate_id = acquisition_candidate["candidate_id"]
        packet, source_files = _packet(
            acquisition_candidate, qa_by_id[candidate_id], evidence_by_id[candidate_id]
        )
        content_sha = packet["packet_content_sha256"]
        entries: list[tuple[str, bytes | Path]] = [
            ("packet.json", _pretty_json(packet)),
            ("instructions.md", INSTRUCTIONS.encode("utf-8")),
            (
                "forms/reviewer-a.json",
                _pretty_json(_blank_review_form(packet["packet_id"], content_sha, "A")),
            ),
            (
                "forms/reviewer-b.json",
                _pretty_json(_blank_review_form(packet["packet_id"], content_sha, "B")),
            ),
        ]
        entries.extend(source_files)
        archive_bytes = _zip_bytes(entries)
        archive_path = cache_root / f"{packet['packet_id']}.zip"
        _write_immutable(archive_path, archive_bytes)
        technical_sources = [
            sensor
            for sensor in ("sentinel_1_grd", "sentinel_2_l2a")
            if qa_by_id[candidate_id][sensor].get("technical_pixel_qa_complete") is True
        ]
        released = bool(packet["source_imagery"] or packet["source_attachments"])
        records.append(
            {
                "candidate_id": candidate_id,
                "packet_id": packet["packet_id"],
                "packet_status": (
                    "released_for_independent_human_annotation"
                    if released
                    else "withheld_missing_source_payload"
                ),
                "archive_path": _stable_path(archive_path),
                "archive_bytes": len(archive_bytes),
                "archive_sha256": _sha256_bytes(archive_bytes),
                "packet_content_sha256": content_sha,
                "source_file_count": len(source_files),
                "technical_qa_sensors": technical_sources,
                "released": released,
                "accepted_as_quantitative_evidence": False,
                "human_review_complete": False,
            }
        )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "packet_set_id": PACKET_SET_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_acquisition_sha256": _sha256_bytes(acquisition_bytes),
        "source_pixel_qa_sha256": _sha256_bytes(qa_bytes),
        "source_evidence_sha256": _sha256_bytes(evidence_bytes),
        "predictions_generated": False,
        "evaluated_outputs_accessed": False,
        "peer_reviews_bundled": False,
        "claim_boundary": (
            "Released packets are blinded annotation inputs, not reviewed observations. "
            "Unresolved masks remain explicit and no missing area is treated as clear."
        ),
        "cache_reference": _stable_path(cache_root),
        "counts": {
            "packets_built": len(records),
            "packets_released": sum(record["released"] for record in records),
            "accepted_quantitative_observations": 0,
            "human_reviews_complete": 0,
        },
        "packets": records,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    base = REPOSITORY_ROOT / "validation-data" / "candidates"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--acquisition", type=Path, default=base / "public-event-imagery-acquisition-v2.json"
    )
    parser.add_argument("--pixel-qa", type=Path, default=base / "public-event-pixel-qa-v3.json")
    parser.add_argument(
        "--evidence", type=Path, default=base / "public-regobs-blinded-evidence-v1.json"
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT / ".validation-cache" / "blinded-observation-packets-v4",
    )
    parser.add_argument(
        "--output", type=Path, default=base / "blinded-observation-packets-v4.json"
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    output = args.output.resolve()
    _assert_outside_protected(cache_root, "Blinded-packet cache")
    _assert_outside_protected(output, "Blinded-packet manifest")
    artifact = build_packets(
        args.acquisition.resolve(),
        args.pixel_qa.resolve(),
        args.evidence.resolve(),
        cache_root,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_pretty_json(artifact))
    print(
        f"Built {artifact['counts']['packets_built']} immutable packets; "
        f"released for annotation={artifact['counts']['packets_released']}; "
        "accepted evidence=0."
    )


if __name__ == "__main__":
    main()
