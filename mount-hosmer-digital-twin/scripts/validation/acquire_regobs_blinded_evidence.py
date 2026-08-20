"""Acquire RegObs target-source records and attachments after packet blinding.

This stage is the explicit target-access boundary.  It reads only the frozen
public RegObs source response, start/stop geometry, and public attachment URLs.
It archives original attachment bytes with attribution and hashes for blinded
evidence construction.  Provider geometry is retained exactly and is not
relabeled as a release polygon, deposit polygon, or terminal dense-flow toe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-regobs-blinded-evidence-v1"
EVIDENCE_ID = "public-regobs-blinded-evidence-v1"
FROZEN_AT_UTC = "2026-08-13T00:00:00Z"
REGOBS_HOST = "api.regobs.no"
USER_AGENT = "avycore-public-regobs-blinded-evidence/1"


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
            raise ValueError(f"Immutable RegObs evidence conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError(f"Concurrent immutable RegObs evidence conflict at {path}.")


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _extension(url: str, mime_type: str | None) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 8:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed or ".bin"


def _download(
    url: str, cache_path: Path, *, offline: bool
) -> tuple[bytes, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != REGOBS_HOST:
        raise ValueError(f"Refusing unexpected RegObs attachment URL {url!r}.")
    request = {
        "url": url,
        "method": "GET",
        "headers": {"Accept": "*/*", "User-Agent": USER_AGENT},
        "access": "anonymous public HTTPS read; no account or token",
    }
    request_path = cache_path.with_suffix(cache_path.suffix + ".request.json")
    metadata_path = cache_path.with_suffix(cache_path.suffix + ".response-metadata.json")
    _write_immutable(request_path, _canonical_json(request))
    if cache_path.exists() != metadata_path.exists():
        raise ValueError(f"Partial immutable attachment cache at {cache_path}.")
    if cache_path.exists():
        payload = cache_path.read_bytes()
        metadata = json.loads(metadata_path.read_bytes())
    else:
        if offline:
            raise FileNotFoundError(f"Offline replay is missing {cache_path}.")
        http_request = urllib.request.Request(
            url, headers=request["headers"], method="GET"
        )
        try:
            with urllib.request.urlopen(http_request, timeout=180) as response:
                payload = response.read()
                metadata = {
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "content_length_header": response.headers.get("Content-Length"),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "final_url": response.geturl(),
                }
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Public RegObs attachment request failed: {url}: {exc}") from exc
        final = urllib.parse.urlparse(metadata["final_url"])
        if final.scheme != "https":
            raise ValueError(f"RegObs attachment redirected away from HTTPS: {url}.")
        _write_immutable(cache_path, payload)
        _write_immutable(metadata_path, _canonical_json(metadata))
    return payload, {
        "cache_path": _stable_path(cache_path),
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
        "request_path": _stable_path(request_path),
        "request_sha256": _sha256_file(request_path),
        "response_metadata_path": _stable_path(metadata_path),
        "response_metadata_sha256": _sha256_file(metadata_path),
        "response_metadata": metadata,
    }


def _image_metadata(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Attachment verification requires Pillow.") from exc
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
            }
    except Exception as exc:
        raise ValueError(f"Attachment is not a readable image: {path}.") from exc


def source_geometry(record: dict[str, Any]) -> dict[str, Any]:
    avalanche = record.get("AvalancheObs") or {}
    start_extent = avalanche.get("StartExtent") or []
    stop_extent = avalanche.get("StopExtent") or []
    start_point = None
    stop_point = None
    if avalanche.get("StartLong") is not None and avalanche.get("StartLat") is not None:
        start_point = [avalanche["StartLong"], avalanche["StartLat"]]
    if avalanche.get("StopLong") is not None and avalanche.get("StopLat") is not None:
        stop_point = [avalanche["StopLong"], avalanche["StopLat"]]
    return {
        "source_crs": "EPSG:4326",
        "coordinate_order": "longitude_latitude",
        "provider_start_extent_ring": start_extent,
        "provider_stop_extent_ring": stop_extent,
        "provider_start_point": start_point,
        "provider_stop_point": stop_point,
        "provider_semantics": {
            "start_extent": (
                "Provider-drawn avalanche start extent; mapping method and boundary "
                "uncertainty are not supplied. It is candidate evidence, not yet a trusted "
                "release polygon."
            ),
            "stop_extent": (
                "Provider-drawn avalanche stop extent; it is not automatically a dense-flow "
                "deposit boundary or complete surveyed footprint."
            ),
            "start_point": "Provider start position; point-placement uncertainty is not supplied.",
            "stop_point": (
                "Provider stopping position; terminal dense-flow toe attribution and point-"
                "placement uncertainty are not supplied."
            ),
        },
        "geometry_modified_to_fit_any_evaluated_result": False,
    }


def _attachment_record(
    attachment: dict[str, Any], cache_dir: Path, *, offline: bool
) -> dict[str, Any]:
    attachment_id = attachment.get("AttachmentId")
    if not isinstance(attachment_id, int):
        raise ValueError("RegObs attachment lacks an integer AttachmentId.")
    formats = attachment.get("UrlFormats") or {}
    url = formats.get("Original") or attachment.get("Url")
    if not isinstance(url, str):
        raise ValueError(f"RegObs attachment {attachment_id} lacks a public original URL.")
    mime = attachment.get("AttachmentMimeType")
    path = cache_dir / f"attachment-{attachment_id}-original{_extension(url, mime)}"
    payload, acquisition = _download(url, path, offline=offline)
    image = _image_metadata(path)
    return {
        "attachment_id": attachment_id,
        "source_url": url,
        "mime_type": mime,
        "registration_name": attachment.get("RegistrationName"),
        "photographer": attachment.get("Photographer"),
        "copyright": attachment.get("Copyright"),
        "comment": attachment.get("Comment"),
        "is_main_attachment": attachment.get("IsMainAttachment"),
        "acquisition": acquisition,
        "image": image,
        "content_interpretation_status": (
            "archived_for_blinded_evidence_construction; not independently human reviewed"
        ),
        "quantitative_ground_truth": False,
        "payload_sha256_recheck": _sha256_bytes(payload),
    }


def build_evidence(
    preflight_path: Path,
    packet_manifest_path: Path,
    regobs_response_path: Path,
    cache_root: Path,
    *,
    offline: bool,
) -> dict[str, Any]:
    preflight_bytes = preflight_path.read_bytes()
    packet_bytes = packet_manifest_path.read_bytes()
    response_bytes = regobs_response_path.read_bytes()
    preflight = json.loads(preflight_bytes)
    packets = json.loads(packet_bytes)
    records = json.loads(response_bytes)
    if preflight.get("schema") != "avycore-public-event-imagery-preflight-v1":
        raise ValueError("Unexpected imagery preflight schema.")
    if packets.get("schema") != "avycore-blinded-observation-packets-v1":
        raise ValueError("Blinded packets must be frozen before RegObs target access.")
    if packets["counts"]["packets_built"] != len(preflight["candidates"]):
        raise ValueError("Every target-access candidate requires a prior blinded packet.")
    if not isinstance(records, list):
        raise ValueError("Frozen RegObs source response must be an array.")
    by_id = {str(record.get("RegId")): record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Frozen RegObs response contains duplicate record ids.")

    candidates = []
    for source_candidate in preflight["candidates"]:
        identity = source_candidate["public_discovery_identity"]
        source_id = str(identity["source_record_id"])
        record = by_id.get(source_id)
        if record is None:
            raise ValueError(f"Frozen RegObs response lacks record {source_id}.")
        canonical_hash = _sha256_bytes(_canonical_json(record))
        if canonical_hash != identity["source_record_canonical_sha256"]:
            raise ValueError(f"RegObs canonical record hash mismatch for {source_id}.")
        candidate_id = source_candidate["candidate_id"]
        candidate_cache = cache_root / candidate_id
        record_path = candidate_cache / "source-record.json"
        _write_immutable(record_path, _pretty_json(record))
        attachments = [
            _attachment_record(attachment, candidate_cache, offline=offline)
            for attachment in (record.get("Attachments") or [])
        ]
        geometry = source_geometry(record)
        result = {
            "candidate_id": candidate_id,
            "source_record_id": source_id,
            "source_record_url": identity["source_record_url"],
            "source_record_cache_path": _stable_path(record_path),
            "source_record_bytes": record_path.stat().st_size,
            "source_record_file_sha256": _sha256_file(record_path),
            "source_record_canonical_sha256": canonical_hash,
            "source_geometry": geometry,
            "event": {
                "avalanche_name": (record.get("AvalancheObs") or {}).get(
                    "AvalancheName"
                ),
                "provider_time": (record.get("AvalancheObs") or {}).get(
                    "DtAvalancheTime"
                ),
                "provider_earliest_time": (record.get("AvalancheObs") or {}).get(
                    "DtEarliestAvalancheTime"
                ),
                "fracture_height_cm": (record.get("AvalancheObs") or {}).get(
                    "FractureHeight"
                ),
                "fracture_width_m": (record.get("AvalancheObs") or {}).get(
                    "FractureWidth"
                ),
                "trajectory_name": (record.get("AvalancheObs") or {}).get(
                    "Trajectory"
                ),
            },
            "observer_attribution": {
                "nickname": (record.get("Observer") or {}).get("NickName"),
                "competence_level": (record.get("Observer") or {}).get(
                    "CompetenceLevelName"
                ),
            },
            "attachments": attachments,
            "counts": {
                "attachments": len(attachments),
                "start_extent_vertices": len(
                    geometry["provider_start_extent_ring"]
                ),
                "stop_extent_vertices": len(geometry["provider_stop_extent_ring"]),
                "start_point_present": geometry["provider_start_point"] is not None,
                "stop_point_present": geometry["provider_stop_point"] is not None,
            },
            "prediction_or_evaluated_output_accessed": False,
            "human_evidence_review_complete": False,
            "validation_contract_eligible": False,
        }
        result["normalized_candidate_sha256"] = _sha256_bytes(_canonical_json(result))
        candidates.append(result)

    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_id": EVIDENCE_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_preflight_sha256": _sha256_bytes(preflight_bytes),
        "source_blinded_packet_manifest_sha256": _sha256_bytes(packet_bytes),
        "source_regobs_response_sha256": _sha256_bytes(response_bytes),
        "stage": "regobs_public_target_source_access_after_packet_blinding",
        "predictions_generated": False,
        "model_code_imported": False,
        "holdout_partition_assigned": False,
        "holdout_targets_accessed": False,
        "regobs_attachments_accessed": True,
        "regobs_start_stop_target_coordinates_accessed": True,
        "anonymous_public_access_only": True,
        "licence": "Norsk lisens for offentlige data (NLOD); RegObs and observer/photo attribution retained",
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Model scores are relative indices, not probabilities."
        ),
        "claim_boundary": (
            "Public target-source access does not make provider start/stop drawings or photos "
            "reviewed release/deposit/toe ground truth. Mapping method, component attribution, "
            "and feature uncertainty remain unresolved until independent blind human review."
        ),
        "cache_reference": ".validation-cache/public-regobs-blinded-evidence-v1",
        "counts": {
            "candidates": len(candidates),
            "attachments_archived": sum(
                candidate["counts"]["attachments"] for candidate in candidates
            ),
            "candidates_with_attachments": sum(
                candidate["counts"]["attachments"] > 0 for candidate in candidates
            ),
            "candidates_with_start_extent": sum(
                candidate["counts"]["start_extent_vertices"] > 0
                for candidate in candidates
            ),
            "candidates_with_stop_extent": sum(
                candidate["counts"]["stop_extent_vertices"] > 0
                for candidate in candidates
            ),
            "candidates_with_stop_point": sum(
                candidate["counts"]["stop_point_present"] for candidate in candidates
            ),
            "human_reviews_complete": 0,
            "quantitative_observations": 0,
            "predictions_generated": 0,
            "holdouts_assigned": 0,
        },
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-imagery-preflight-v1.json",
    )
    parser.add_argument(
        "--packets",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "blinded-observation-packets-v1.json",
    )
    parser.add_argument(
        "--regobs-response",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache"
        / "public-event-funnel-v1"
        / "regobs-search-response.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache"
        / "public-regobs-blinded-evidence-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-regobs-blinded-evidence-v1.json",
    )
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    output = args.output.resolve()
    _assert_outside_protected(cache_root, "RegObs evidence cache")
    _assert_outside_protected(output, "RegObs evidence manifest")
    artifact = build_evidence(
        args.preflight.resolve(),
        args.packets.resolve(),
        args.regobs_response.resolve(),
        cache_root,
        offline=args.offline,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote {artifact['counts']['candidates']} candidates to {output}; "
        f"attachments={artifact['counts']['attachments_archived']}, "
        "human reviews=0, quantitative observations=0."
    )


if __name__ == "__main__":
    main()
