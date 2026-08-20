"""Freeze an anonymous, reusable primary-source audit for strict validation data.

Only public landing/API metadata are acquired.  Large archives are not silently
treated as reviewed evidence: their provider checksums, sizes, licenses, stated
contents, and unresolved validation-contract fields are retained verbatim in an
immutable, gitignored cache.  No account, outreach, special terms, prediction,
or model result is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-validation-source-audit-v2"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
USER_AGENT = "avycore-public-validation-source-audit/1"

SOURCES = (
    {
        "source_id": "zenodo-15863589-avalcd",
        "url": "https://zenodo.org/api/records/15863589",
        "landing_page": "https://zenodo.org/records/15863589",
        "evidence_types": [
            "sentinel_1_pre_post",
            "local_incidence_angle",
            "copernicus_dem",
            "slope_aspect",
            "binary_avalanche_masks",
            "avalanche_polygons",
        ],
        "assessment": (
            "Useful reusable SAR and binary-outline benchmark across four regions. "
            "The primary record does not establish per-event release/deposit/toe "
            "components, snow-depth profiles, dry dense-slab regime, or two independent "
            "human reviews required by validation-contract v3."
        ),
        "blocking_contract_fields": [
            "event_level_release_component",
            "event_level_deposit_component",
            "terminal_toe_component",
            "release_thickness_profile",
            "deposit_thickness_profile",
            "dry_dense_slab_regime_confirmation",
            "two_independent_human_reviews",
        ],
    },
    {
        "source_id": "zenodo-10895011-glacier-deposits",
        "url": "https://zenodo.org/api/records/10895011",
        "landing_page": "https://zenodo.org/records/10895011",
        "evidence_types": [
            "sentinel_1_scene_dates",
            "manual_updated_deposit_outlines",
            "glacier_visibility_area",
        ],
        "assessment": (
            "Provides reusable deposit outlines and visibility accounting for glacier "
            "avalanches. It lacks release polygons, a distinct terminal-toe observation, "
            "thickness profiles, the frozen dry dense-slab regime, and v3 reviews."
        ),
        "blocking_contract_fields": [
            "release_component",
            "terminal_toe_component",
            "release_thickness_profile",
            "deposit_thickness_profile",
            "dry_dense_slab_regime_confirmation",
            "two_independent_human_reviews",
        ],
    },
    {
        "source_id": "zenodo-15796703-braemabuehl",
        "url": "https://zenodo.org/api/records/15796703",
        "landing_page": "https://zenodo.org/records/15796703",
        "evidence_types": [
            "three_cold_avalanche_outlines",
            "post_event_orthophoto",
            "snow_surface_height",
            "one_metre_dtm",
            "field_measurement_tables",
        ],
        "assessment": (
            "Strong public event-surface evidence for three artificial cold avalanches "
            "at one site and one January 2019 campaign. It cannot by itself meet the frozen "
            "12-event, six-path, two-mountain, three-storm diversity gate, and the primary "
            "record does not claim validation-contract-v3 independent reviews."
        ),
        "blocking_contract_fields": [
            "cohort_event_count_12",
            "six_independent_paths",
            "two_mountains",
            "three_storms",
            "two_independent_human_reviews",
            "contract_specific_component_attribution_audit",
        ],
    },
    {
        "source_id": "zenodo-18198188-uas-monitoring",
        "url": "https://zenodo.org/api/records/18198188",
        "landing_page": "https://zenodo.org/records/18198188",
        "evidence_types": ["uas_mapping", "davos_field_experiment"],
        "assessment": (
            "Potential high-resolution imagery/terrain lead from a 2025 Davos field "
            "experiment. The landing record alone does not enumerate v3 event components, "
            "thickness profiles, independent paths/storms, or independent human reviews; "
            "the 4.7 GB archive was not treated as inspected ground truth."
        ),
        "blocking_contract_fields": [
            "archive_component_inventory_unreviewed",
            "release_deposit_toe_attribution",
            "release_thickness_profile",
            "deposit_thickness_profile",
            "cohort_diversity",
            "two_independent_human_reviews",
        ],
    },
    {
        "source_id": "zenodo-20701552-avaframedata",
        "url": "https://zenodo.org/api/records/20701552",
        "landing_page": "https://zenodo.org/records/20701552",
        "evidence_types": [
            "six_documented_avalanche_solver_inputs",
            "dem",
            "release_area",
            "simulation_configuration",
        ],
        "assessment": (
            "Anonymous reusable solver-input package for six events in Austria and Switzerland. "
            "It does not publish a validation-contract-v3 complete surveyed dense-flow target, "
            "event-specific release-state uncertainty, or two independent human reviews, and "
            "six events cannot satisfy the frozen 12-event cohort gate."
        ),
        "blocking_contract_fields": [
            "surveyed_dense_flow_deposit_or_terminal_toe",
            "complete_detection_masks",
            "release_state_uncertainty",
            "two_independent_human_reviews",
            "cohort_event_count_12",
        ],
    },
    {
        "source_id": "zenodo-15233461-kitchener",
        "url": "https://zenodo.org/api/records/15233461",
        "landing_page": "https://zenodo.org/records/15233461",
        "evidence_types": [
            "post_cycle_debris_polygon",
            "post_melt_uav_lidar_dsm",
            "difference_of_dem",
        ],
        "assessment": (
            "Open primary mapping for one New Zealand avalanche path. The debris is attributed "
            "to a July 2022 avalanche cycle plus rain runoff rather than one bounded dry dense-"
            "slab event, and the record supplies no independent release polygon, event release "
            "state, terminal attribution, complete-search masks, or two independent reviews."
        ),
        "blocking_contract_fields": [
            "single_bounded_dry_dense_slab_event",
            "independent_release_polygon",
            "normal_to_slope_release_thickness",
            "release_density",
            "terminal_dense_flow_attribution",
            "complete_detection_masks",
            "two_independent_human_reviews",
        ],
    },
    {
        "source_id": "zenodo-17104410-vdl20243024",
        "url": "https://zenodo.org/api/records/17104410",
        "landing_page": "https://zenodo.org/records/17104410",
        "evidence_types": [
            "high_speed_imagery",
            "optical_velocity",
            "geodar",
            "pressure",
            "panoramic_imagery",
        ],
        "assessment": (
            "Strong primary dynamics evidence for one Vallée de la Sionne avalanche, but the "
            "public package does not establish the complete independent release/deposit/toe, "
            "release-state, event-surface, survey-mask, and two-review evidence required for "
            "Profiles C or E. One event at one path also fails the cohort diversity gate."
        ),
        "blocking_contract_fields": [
            "independent_release_polygon",
            "surveyed_dense_flow_deposit_or_terminal_toe",
            "normal_to_slope_release_thickness",
            "release_density",
            "event_surface_terrain",
            "complete_detection_masks",
            "two_independent_human_reviews",
            "cohort_diversity",
        ],
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


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable public-source cache conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _validate_source_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "zenodo.org":
        raise ValueError(f"Refusing non-reviewed source URL: {url!r}.")
    if not parsed.path.startswith("/api/records/"):
        raise ValueError(f"Refusing non-record Zenodo API path: {url!r}.")


def _fetch(source: dict[str, Any], cache_root: Path, offline: bool) -> dict[str, Any]:
    _validate_source_url(source["url"])
    source_cache = cache_root / source["source_id"]
    request = {
        "url": source["url"],
        "method": "GET",
        "headers": {"Accept": "application/json", "User-Agent": USER_AGENT},
    }
    request_bytes = _canonical_json(request)
    request_path = source_cache / "request.json"
    response_path = source_cache / "response.json"
    response_metadata_path = source_cache / "response-metadata.json"
    _write_immutable(request_path, request_bytes)
    if response_path.exists() != response_metadata_path.exists():
        raise ValueError(f"Partial immutable source response cache at {source_cache}.")
    if not response_path.exists():
        if offline:
            raise FileNotFoundError(f"Offline source audit lacks {response_path}.")
        request_object = urllib.request.Request(
            source["url"], headers=request["headers"], method="GET"
        )
        with urllib.request.urlopen(request_object, timeout=60) as response:
            payload = response.read()
            response_metadata = {
                "status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "content_length_header": response.headers.get("Content-Length"),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "final_url": response.geturl(),
            }
        parsed = json.loads(payload)
        canonical_payload = _canonical_json(parsed)
        _write_immutable(response_path, canonical_payload)
        _write_immutable(response_metadata_path, _canonical_json(response_metadata))
    payload = response_path.read_bytes()
    metadata = json.loads(response_metadata_path.read_bytes())
    record = json.loads(payload)
    files = [
        {
            "key": item.get("key"),
            "size_bytes": item.get("size"),
            "provider_checksum": item.get("checksum"),
        }
        for item in record.get("files", [])
    ]
    metadata_record = record.get("metadata") or {}
    return {
        **source,
        "anonymous_access_verified": metadata.get("status") == 200,
        "license": (metadata_record.get("rights") or [{}])[0].get("id")
        if isinstance(metadata_record.get("rights"), list)
        else metadata_record.get("license"),
        "publication_date": metadata_record.get("publication_date"),
        "title": metadata_record.get("title"),
        "files": files,
        "large_archives_downloaded": False,
        "source_meets_validation_contract_v3": False,
        "cache": {
            "request_path": _stable_path(request_path),
            "request_sha256": _sha256_bytes(request_bytes),
            "response_path": _stable_path(response_path),
            "response_sha256": _sha256_bytes(payload),
            "response_metadata_path": _stable_path(response_metadata_path),
            "response_metadata_sha256": _sha256_bytes(
                response_metadata_path.read_bytes()
            ),
            "response_metadata": metadata,
        },
    }


def build_audit(cache_root: Path, offline: bool) -> dict[str, Any]:
    sources = [_fetch(source, cache_root, offline) for source in SOURCES]
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "stage": "anonymous_reusable_public_source_search_before_model_results",
        "anonymous_public_access_only": True,
        "accounts_used": False,
        "outreach_used": False,
        "special_terms_accepted": False,
        "model_results_opened": False,
        "predictions_generated": False,
        "claim_boundary": (
            "A source lead is not an eligible validation event. Provider metadata and "
            "checksums establish discoverability and stated contents only; uninspected "
            "archives, absent component fields, and absent reviews remain absent evidence."
        ),
        "sources": sources,
        "counts": {
            "sources_audited": len(sources),
            "anonymous_access_verified": sum(
                source["anonymous_access_verified"] for source in sources
            ),
            "validation_contract_v3_sources": 0,
            "new_eligible_events": 0,
        },
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT / ".validation-cache/public-validation-source-audit-v2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-validation-source-audit-v2.json",
    )
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_audit(args.cache_root.resolve(), args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Audited {artifact['counts']['sources_audited']} primary public sources; "
        "new eligible v3 events=0."
    )


if __name__ == "__main__":
    main()
