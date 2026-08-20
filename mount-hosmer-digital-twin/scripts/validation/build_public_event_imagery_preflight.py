"""Build a metadata-only satellite-imagery preflight for public RegObs candidates.

This script reads the frozen public candidate inventory, selects RegObs records
with reported crown/fracture height and stop evidence, and queries only public
Copernicus Data Space Ecosystem STAC metadata. It never requests raster assets,
opens RegObs attachments or target geometry, imports model code, assigns a
holdout, or evaluates validation-contract eligibility.

Every STAC request and raw response is stored in an immutable, gitignored cache.
Use ``--offline`` to require complete cache replay without network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-imagery-preflight-v1"
PREFLIGHT_ID = "public-event-imagery-preflight-v1"
EXPERIMENT_ID = "public-data-field-validation-v1"
FROZEN_ACQUISITION_AT_UTC = "2026-08-13T00:00:00Z"

CATALOGUE_DOCUMENTATION_URL = (
    "https://documentation.dataspace.copernicus.eu/APIs/STAC.html"
)
STAC_ROOT_URL = "https://stac.dataspace.copernicus.eu/v1/"
STAC_SEARCH_URL = urllib.parse.urljoin(STAC_ROOT_URL, "search")
STAC_HOST = urllib.parse.urlparse(STAC_ROOT_URL).hostname
STAC_LIMIT = 100
SEARCH_MARGIN_DAYS = 18
USER_AGENT = "avycore-public-event-imagery-preflight/1"

SENSORS: dict[str, dict[str, str]] = {
    "sentinel_1_grd": {
        "collection": "sentinel-1-grd",
        "name": "Sentinel-1 GRD",
        "collection_url": urllib.parse.urljoin(
            STAC_ROOT_URL, "collections/sentinel-1-grd"
        ),
        "queryables_url": urllib.parse.urljoin(
            STAC_ROOT_URL, "collections/sentinel-1-grd/queryables"
        ),
    },
    "sentinel_2_l2a": {
        "collection": "sentinel-2-l2a",
        "name": "Sentinel-2 Level-2A",
        "collection_url": urllib.parse.urljoin(
            STAC_ROOT_URL, "collections/sentinel-2-l2a"
        ),
        "queryables_url": urllib.parse.urljoin(
            STAC_ROOT_URL, "collections/sentinel-2-l2a/queryables"
        ),
    },
}

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TARGET_COORDINATE_KEYS = {
    "startlat",
    "startlong",
    "stoplat",
    "stoplong",
    "startlatitude",
    "startlongitude",
    "stoplatitude",
    "stoplongitude",
    "startextent",
    "stopextent",
    "attachments",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


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
            raise ValueError(
                f"Immutable cache identity conflict at {path}; choose a new cache root."
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _parse_offset_datetime(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty offset-bearing timestamp.")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO-8601 timestamp: {value!r}.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must preserve an explicit UTC offset: {value!r}.")
    return parsed


def _format_utc(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    timespec = "microseconds" if utc.microsecond else "seconds"
    return utc.isoformat(timespec=timespec).replace("+00:00", "Z")


def normalize_event_interval(event_time: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider offsets with ISO-8601 arithmetic and preserve missingness."""

    provider_earliest = event_time.get("provider_earliest")
    provider_latest = event_time.get("provider_latest")
    provider_observation = event_time.get("provider_observation_time")
    result: dict[str, Any] = {
        "provider_original": {
            "earliest": provider_earliest,
            "latest": provider_latest,
            "observation_time": provider_observation,
        },
        "normalization_rule": (
            "Parse each original ISO-8601 value using its explicit numeric offset and convert "
            "the same instant to UTC. When provider_earliest is missing, preserve that null "
            "and use provider_latest as a transparent zero-duration pairing interval."
        ),
        "normalized_provider_earliest_utc": None,
        "normalized_provider_latest_utc": None,
        "normalized_provider_observation_time_utc": None,
        "pairing_interval_utc": {"start": None, "end": None},
        "pairing_interval_basis": None,
        "status": "missing_provider_latest",
        "missing_fields": [],
    }

    if provider_earliest is None:
        result["missing_fields"].append("provider_earliest")
    else:
        result["normalized_provider_earliest_utc"] = _format_utc(
            _parse_offset_datetime(provider_earliest, "provider_earliest")
        )
    if provider_latest is None:
        result["missing_fields"].append("provider_latest")
    else:
        result["normalized_provider_latest_utc"] = _format_utc(
            _parse_offset_datetime(provider_latest, "provider_latest")
        )
    if provider_observation is None:
        result["missing_fields"].append("provider_observation_time")
    else:
        result["normalized_provider_observation_time_utc"] = _format_utc(
            _parse_offset_datetime(provider_observation, "provider_observation_time")
        )

    if provider_latest is None:
        return result

    latest = _parse_offset_datetime(provider_latest, "provider_latest").astimezone(
        timezone.utc
    )
    if provider_earliest is None:
        earliest = latest
        basis = "provider_latest_as_single_instant_because_provider_earliest_is_missing"
    else:
        earliest = _parse_offset_datetime(
            provider_earliest, "provider_earliest"
        ).astimezone(timezone.utc)
        basis = "provider_earliest_to_provider_latest"
    if earliest > latest:
        result["status"] = "invalid_provider_interval"
        result["pairing_interval_basis"] = basis
        return result
    result["pairing_interval_utc"] = {
        "start": _format_utc(earliest),
        "end": _format_utc(latest),
    }
    result["pairing_interval_basis"] = basis
    result["status"] = "normalized"
    return result


def select_source_candidates(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive the preflight cohort from published inventory metadata only."""

    selected = []
    for candidate in inventory.get("candidates", []):
        if candidate.get("source_collection") != "RegObs public API v5":
            continue
        release = candidate.get("release_initial_condition_evidence") or {}
        geometry = candidate.get("geometry_availability") or {}
        if release.get("fracture_height_value") is None:
            continue
        if not (geometry.get("stop_point_present") or geometry.get("stop_extent_present")):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: str(item["candidate_id"]))


def _search_interval(normalized_event: dict[str, Any]) -> dict[str, str] | None:
    if normalized_event["status"] != "normalized":
        return None
    pairing = normalized_event["pairing_interval_utc"]
    start = _parse_offset_datetime(pairing["start"], "pairing start").astimezone(
        timezone.utc
    )
    end = _parse_offset_datetime(pairing["end"], "pairing end").astimezone(
        timezone.utc
    )
    margin = timedelta(days=SEARCH_MARGIN_DAYS)
    return {
        "start": _format_utc(start - margin),
        "end": _format_utc(end + margin),
    }


def build_search_body(
    collection: str,
    longitude: float,
    latitude: float,
    query_interval: dict[str, str],
) -> dict[str, Any]:
    return {
        "collections": [collection],
        "datetime": f"{query_interval['start']}/{query_interval['end']}",
        "intersects": {
            "type": "Point",
            "coordinates": [longitude, latitude],
        },
        "limit": STAC_LIMIT,
        "fields": {"exclude": ["assets"]},
    }


def _request_descriptor(
    url: str,
    method: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "url": url,
        "method": method.upper(),
        "headers": {
            "Accept": "application/geo+json, application/json",
            "Content-Type": "application/json" if body is not None else None,
            "User-Agent": USER_AGENT,
        },
        "body": body,
    }


def _validate_catalogue_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != STAC_HOST:
        raise ValueError(f"Refusing unexpected STAC pagination URL: {url!r}.")


def _fetch_descriptor(descriptor: dict[str, Any]) -> bytes:
    _validate_catalogue_url(descriptor["url"])
    body = descriptor["body"]
    data = _canonical_json(body) if body is not None else None
    headers = {
        key: value
        for key, value in descriptor["headers"].items()
        if value is not None
    }
    request = urllib.request.Request(
        descriptor["url"],
        data=data,
        method=descriptor["method"],
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Public STAC metadata request failed for {descriptor['url']}: {exc}"
        ) from exc


def _cache_page(
    cache_dir: Path,
    page_index: int,
    descriptor: dict[str, Any],
    *,
    offline: bool,
    fetcher: Any = _fetch_descriptor,
) -> tuple[bytes, dict[str, Any]]:
    request_path = cache_dir / f"page-{page_index:03d}-request.json"
    response_path = cache_dir / f"page-{page_index:03d}-response.json"
    request_bytes = _canonical_json(descriptor)

    if request_path.exists() != response_path.exists():
        if response_path.exists():
            raise ValueError(
                f"Partial cache at {cache_dir}: response exists without its request identity."
            )
        if offline:
            raise FileNotFoundError(
                f"Offline replay is missing response {response_path}."
            )

    if offline and not response_path.exists():
        raise FileNotFoundError(
            f"Offline replay is missing immutable response {response_path}."
        )

    _write_immutable(request_path, request_bytes)
    if response_path.exists():
        response_bytes = response_path.read_bytes()
    else:
        if offline:
            raise FileNotFoundError(
                f"Offline replay is missing immutable response {response_path}."
            )
        response_bytes = fetcher(descriptor)
        try:
            parsed = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("STAC response is not valid JSON; it was not cached.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("STAC response must be a JSON object; it was not cached.")
        _write_immutable(response_path, response_bytes)

    return response_bytes, {
        "page": page_index,
        "request_cache_reference": request_path.name,
        "request_bytes": len(request_bytes),
        "request_sha256": _sha256_bytes(request_bytes),
        "response_cache_reference": response_path.name,
        "response_bytes": len(response_bytes),
        "response_sha256": _sha256_bytes(response_bytes),
    }


def _next_descriptor(
    response: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any] | None:
    next_links = [
        link
        for link in response.get("links", [])
        if isinstance(link, dict) and link.get("rel") == "next"
    ]
    if not next_links:
        return None
    if len(next_links) != 1:
        raise ValueError("STAC response has more than one rel=next link.")
    link = next_links[0]
    url = urllib.parse.urljoin(previous["url"], str(link.get("href", "")))
    _validate_catalogue_url(url)
    method = str(link.get("method", "GET")).upper()
    if method not in {"GET", "POST"}:
        raise ValueError(f"Unsupported STAC pagination method {method!r}.")
    body = link.get("body")
    if method == "POST" and body is None:
        body = previous["body"]
    if body is not None and not isinstance(body, dict):
        raise ValueError("STAC pagination body must be a JSON object.")
    return _request_descriptor(url, method, body)


def _ensure_no_extra_cache_pages(cache_dir: Path, used_page_count: int) -> None:
    for path in cache_dir.glob("page-*-*.json"):
        match = re.fullmatch(r"page-([0-9]{3})-(?:request|response)\.json", path.name)
        if match and int(match.group(1)) >= used_page_count:
            raise ValueError(
                f"Cache identity conflict: unexpected stale page remains at {path}."
            )


def acquire_search(
    cache_dir: Path,
    body: dict[str, Any],
    *,
    offline: bool,
    cache_reference_prefix: str,
    fetcher: Any = _fetch_descriptor,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    descriptor = _request_descriptor(STAC_SEARCH_URL, "POST", body)
    pages: list[dict[str, Any]] = []
    features_by_id: dict[str, dict[str, Any]] = {}
    seen_requests: set[str] = set()

    while descriptor is not None:
        request_sha256 = _sha256_bytes(_canonical_json(descriptor))
        if request_sha256 in seen_requests:
            raise ValueError("STAC pagination contains a request cycle.")
        seen_requests.add(request_sha256)
        page_index = len(pages)
        response_bytes, page = _cache_page(
            cache_dir,
            page_index,
            descriptor,
            offline=offline,
            fetcher=fetcher,
        )
        response = json.loads(response_bytes)
        features = response.get("features")
        if not isinstance(features, list):
            raise ValueError("STAC response has no FeatureCollection features array.")
        for feature in features:
            if not isinstance(feature, dict) or not isinstance(feature.get("id"), str):
                raise ValueError("Every STAC feature must be an object with a string id.")
            item_id = feature["id"]
            existing = features_by_id.get(item_id)
            if existing is not None and _canonical_json(existing) != _canonical_json(feature):
                raise ValueError(f"STAC item {item_id!r} differs across response pages.")
            features_by_id[item_id] = feature
        relative_prefix = cache_reference_prefix.rstrip("/")
        page["request_cache_reference"] = (
            f"{relative_prefix}/{page['request_cache_reference']}"
        )
        page["response_cache_reference"] = (
            f"{relative_prefix}/{page['response_cache_reference']}"
        )
        page["feature_count"] = len(features)
        pages.append(page)
        descriptor = _next_descriptor(response, descriptor)

    _ensure_no_extra_cache_pages(cache_dir, len(pages))
    return (
        sorted(features_by_id.values(), key=lambda item: str(item["id"])),
        pages,
    )


def _point_on_segment(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> bool:
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    tolerance = 1e-12 * max(1.0, abs(px), abs(py), abs(ax), abs(ay), abs(bx), abs(by))
    if abs(cross) > tolerance:
        return False
    return (
        min(ax, bx) - tolerance <= px <= max(ax, bx) + tolerance
        and min(ay, by) - tolerance <= py <= max(ay, by) + tolerance
    )


def _ring_contains(point: tuple[float, float], ring: Any) -> bool:
    if not isinstance(ring, list) or len(ring) < 4:
        return False
    px, py = point
    inside = False
    for index in range(len(ring)):
        first = ring[index - 1]
        second = ring[index]
        if (
            not isinstance(first, list)
            or not isinstance(second, list)
            or len(first) < 2
            or len(second) < 2
        ):
            return False
        ax, ay = float(first[0]), float(first[1])
        bx, by = float(second[0]), float(second[1])
        if _point_on_segment(px, py, ax, ay, bx, by):
            return True
        if (ay > py) != (by > py):
            intersect_x = (bx - ax) * (py - ay) / (by - ay) + ax
            if px < intersect_x:
                inside = not inside
    return inside


def _polygon_contains(point: tuple[float, float], polygon: Any) -> bool:
    if not isinstance(polygon, list) or not polygon:
        return False
    if not _ring_contains(point, polygon[0]):
        return False
    return not any(_ring_contains(point, hole) for hole in polygon[1:])


def geometry_intersects_point(
    geometry: Any,
    longitude: float,
    latitude: float,
) -> bool | None:
    if not isinstance(geometry, dict):
        return None
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    point = (longitude, latitude)
    if geometry_type == "Polygon":
        return _polygon_contains(point, coordinates)
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return any(_polygon_contains(point, polygon) for polygon in coordinates)
    return None


def _string_or_none(value: Any, *, upper: bool = False, lower: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    result = value.strip()
    if upper:
        result = result.upper()
    if lower:
        result = result.lower()
    return result


def _integer_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _polarizations_or_none(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    normalized = [_string_or_none(item, upper=True) for item in value]
    if any(item is None for item in normalized):
        return None
    return sorted(set(item for item in normalized if item is not None))


def _cloud_cover_or_none(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
        return None
    return value


def _mgrs_tile(properties: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = _string_or_none(properties.get("grid:code"), upper=True)
    if raw is None:
        raw = _string_or_none(properties.get("s2:mgrs_tile"), upper=True)
    if raw is None:
        return None, None
    tile = raw[5:] if raw.startswith("MGRS-") else raw
    return raw, tile or None


def normalize_acquisition(
    feature: dict[str, Any],
    sensor: str,
    longitude: float,
    latitude: float,
    event_start: datetime,
    event_end: datetime,
) -> dict[str, Any]:
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    raw_datetime = properties.get("datetime") or properties.get("start_datetime")
    normalized_datetime: str | None = None
    temporal_position = "missing_acquisition_time"
    missing: list[str] = []
    if raw_datetime is None:
        missing.append("acquisition_time")
    else:
        try:
            acquired = _parse_offset_datetime(
                raw_datetime, f"STAC item {feature.get('id')} datetime"
            ).astimezone(timezone.utc)
        except ValueError:
            missing.append("valid_offset_bearing_acquisition_time")
        else:
            normalized_datetime = _format_utc(acquired)
            if acquired < event_start:
                temporal_position = "pre_event"
            elif acquired > event_end:
                temporal_position = "post_event"
            else:
                temporal_position = "during_event_interval"

    spatial = geometry_intersects_point(feature.get("geometry"), longitude, latitude)
    if spatial is None:
        missing.append("supported_polygon_or_multipolygon_geometry")

    base: dict[str, Any] = {
        "item_id": str(feature.get("id")),
        "collection": feature.get("collection"),
        "acquisition_time_original": raw_datetime,
        "acquisition_time_utc": normalized_datetime,
        "temporal_position": temporal_position,
        "spatially_intersects_discovery_point": spatial,
        "platform": _string_or_none(properties.get("platform"), lower=True),
        "product_type": _string_or_none(properties.get("product:type")),
        "processing_level_catalogue": _string_or_none(
            properties.get("processing:level")
        ),
        "catalogue_metadata_missing": missing,
    }

    if sensor == "sentinel_1_grd":
        base.update(
            {
                "processing_level": "Level-1 GRD (frozen collection identity)",
                "orbit_direction": _string_or_none(
                    properties.get("sat:orbit_state"), lower=True
                ),
                "relative_orbit": _integer_or_none(
                    properties.get("sat:relative_orbit")
                ),
                "acquisition_mode": _string_or_none(
                    properties.get("sar:instrument_mode"), upper=True
                ),
                "polarizations": _polarizations_or_none(
                    properties.get("sar:polarizations")
                ),
            }
        )
        for field in (
            "orbit_direction",
            "relative_orbit",
            "acquisition_mode",
            "polarizations",
        ):
            if base[field] is None:
                missing.append(field)
    elif sensor == "sentinel_2_l2a":
        grid_code, mgrs_tile = _mgrs_tile(properties)
        cloud_cover = _cloud_cover_or_none(properties.get("eo:cloud_cover"))
        base.update(
            {
                "processing_level": "Level-2A (frozen collection identity)",
                "grid_code": grid_code,
                "mgrs_tile": mgrs_tile,
                "catalogue_cloud_cover_percent": cloud_cover,
                "catalogue_cloud_cover_interpretation": (
                    "Whole-product catalogue metadata only; not local clear-sky proof."
                ),
            }
        )
        if mgrs_tile is None:
            missing.append("mgrs_tile")
        if cloud_cover is None:
            missing.append("catalogue_cloud_cover_percent")
    else:
        raise ValueError(f"Unsupported sensor {sensor!r}.")
    return base


def _pair_id(sensor: str, pre_id: str, post_id: str) -> str:
    digest = _sha256_bytes(
        _canonical_json({"sensor": sensor, "pre": pre_id, "post": post_id})
    )
    return f"{sensor}-pair-{digest[:20]}"


def _metadata_pair_reasons(
    pre: dict[str, Any],
    post: dict[str, Any],
    sensor: str,
) -> tuple[list[str], dict[str, Any]]:
    reasons: list[str] = []
    checks: dict[str, Any] = {
        "pre_spatial_intersection": pre["spatially_intersects_discovery_point"],
        "post_spatial_intersection": post["spatially_intersects_discovery_point"],
        "brackets_complete_event_interval": (
            pre["temporal_position"] == "pre_event"
            and post["temporal_position"] == "post_event"
        ),
    }
    if pre["spatially_intersects_discovery_point"] is not True:
        reasons.append("pre_acquisition_does_not_confirm_discovery_point_intersection")
    if post["spatially_intersects_discovery_point"] is not True:
        reasons.append("post_acquisition_does_not_confirm_discovery_point_intersection")

    if sensor == "sentinel_1_grd":
        fields = (
            ("orbit_direction", "orbit_direction"),
            ("relative_orbit", "relative_orbit"),
            ("acquisition_mode", "acquisition_mode"),
            ("polarizations", "polarization_set"),
        )
        for field, label in fields:
            pre_value = pre.get(field)
            post_value = post.get(field)
            checks[f"same_{label}"] = (
                pre_value is not None and post_value is not None and pre_value == post_value
            )
            if pre_value is None:
                reasons.append(f"pre_missing_{label}")
            if post_value is None:
                reasons.append(f"post_missing_{label}")
            if pre_value is not None and post_value is not None and pre_value != post_value:
                reasons.append(f"different_{label}")
    else:
        pre_tile = pre.get("mgrs_tile")
        post_tile = post.get("mgrs_tile")
        checks["same_mgrs_tile"] = (
            pre_tile is not None and post_tile is not None and pre_tile == post_tile
        )
        checks["both_level_2a_collection_items"] = (
            pre.get("collection") == SENSORS[sensor]["collection"]
            and post.get("collection") == SENSORS[sensor]["collection"]
        )
        checks["catalogue_cloud_metadata_complete"] = (
            pre.get("catalogue_cloud_cover_percent") is not None
            and post.get("catalogue_cloud_cover_percent") is not None
        )
        if pre_tile is None:
            reasons.append("pre_missing_mgrs_tile")
        if post_tile is None:
            reasons.append("post_missing_mgrs_tile")
        if pre_tile is not None and post_tile is not None and pre_tile != post_tile:
            reasons.append("different_mgrs_tile")
        if not checks["both_level_2a_collection_items"]:
            reasons.append("pair_not_entirely_from_sentinel_2_l2a_collection")
    return reasons, checks


def pair_acquisitions(
    acquisitions: list[dict[str, Any]],
    sensor: str,
    event_start: datetime,
    event_end: datetime,
) -> dict[str, Any]:
    pre_items = [item for item in acquisitions if item["temporal_position"] == "pre_event"]
    post_items = [item for item in acquisitions if item["temporal_position"] == "post_event"]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for pre in pre_items:
        for post in post_items:
            pre_time = _parse_offset_datetime(
                pre["acquisition_time_utc"], "pre acquisition time"
            ).astimezone(timezone.utc)
            post_time = _parse_offset_datetime(
                post["acquisition_time_utc"], "post acquisition time"
            ).astimezone(timezone.utc)
            baseline_seconds = int((post_time - pre_time).total_seconds())
            reasons, checks = _metadata_pair_reasons(pre, post, sensor)
            pair = {
                "pair_id": _pair_id(sensor, pre["item_id"], post["item_id"]),
                "pre_item_id": pre["item_id"],
                "post_item_id": post["item_id"],
                "pre_acquisition_time_utc": pre["acquisition_time_utc"],
                "post_acquisition_time_utc": post["acquisition_time_utc"],
                "temporal_baseline_seconds": baseline_seconds,
                "temporal_baseline_days": round(baseline_seconds / 86400, 6),
                "pre_to_event_start_seconds": int(
                    (event_start - pre_time).total_seconds()
                ),
                "event_end_to_post_seconds": int(
                    (post_time - event_end).total_seconds()
                ),
                "compatibility_checks": checks,
                "rejection_reasons": reasons,
            }
            if sensor == "sentinel_2_l2a":
                pair["mgrs_tile"] = pre.get("mgrs_tile")
                pair["pre_catalogue_cloud_cover_percent"] = pre.get(
                    "catalogue_cloud_cover_percent"
                )
                pair["post_catalogue_cloud_cover_percent"] = post.get(
                    "catalogue_cloud_cover_percent"
                )
                pair["pixel_qa_required"] = True
                pair["pixel_qa_reason"] = (
                    "Catalogue cloud percentage is product-wide metadata and cannot prove "
                    "local clear sky, visible snow, or an interpretable avalanche deposit."
                )
            else:
                pair["orbit_direction"] = pre.get("orbit_direction")
                pair["relative_orbit"] = pre.get("relative_orbit")
                pair["acquisition_mode"] = pre.get("acquisition_mode")
                pair["polarizations"] = pre.get("polarizations")
                pair["pixel_qa_required"] = True
                pair["pixel_qa_reason"] = (
                    "Catalogue compatibility does not establish usable local SAR geometry, "
                    "signal, layover/shadow masking, or avalanche attribution."
                )
            (accepted if not reasons else rejected).append(pair)

    result_reasons: list[str] = []
    if not acquisitions:
        result_reasons.append("no_catalogue_acquisitions_returned")
    if not pre_items:
        result_reasons.append("no_strictly_pre_event_acquisition_in_search_window")
    if not post_items:
        result_reasons.append("no_strictly_post_event_acquisition_in_search_window")
    if pre_items and post_items and not accepted:
        result_reasons.append("all_bracketing_pairs_rejected_by_frozen_compatibility_rules")

    catalogue_pair_status = (
        "catalogue_pair_found" if accepted else "no_qualifying_pair"
    )
    pixel_qa_status = (
        "requires_pixel_qa"
        if accepted
        else "not_reached_because_no_qualifying_catalogue_pair"
    )
    return {
        "catalogue_pair_status": catalogue_pair_status,
        "pixel_qa_status": pixel_qa_status,
        "availability_reasons": result_reasons,
        "accepted_pairs": accepted,
        "rejected_pairs": rejected,
        "counts": {
            "candidate_acquisitions": len(acquisitions),
            "pre_event_acquisitions": len(pre_items),
            "during_event_interval_acquisitions": sum(
                item["temporal_position"] == "during_event_interval"
                for item in acquisitions
            ),
            "post_event_acquisitions": len(post_items),
            "accepted_pairs": len(accepted),
            "rejected_pairs": len(rejected),
        },
    }


def _normalize_and_pair(
    features: list[dict[str, Any]],
    sensor: str,
    longitude: float,
    latitude: float,
    normalized_event: dict[str, Any],
) -> dict[str, Any]:
    pairing = normalized_event["pairing_interval_utc"]
    event_start = _parse_offset_datetime(pairing["start"], "event start").astimezone(
        timezone.utc
    )
    event_end = _parse_offset_datetime(pairing["end"], "event end").astimezone(
        timezone.utc
    )
    acquisitions = [
        normalize_acquisition(
            feature,
            sensor,
            longitude,
            latitude,
            event_start,
            event_end,
        )
        for feature in features
    ]
    acquisitions.sort(
        key=lambda item: (
            item["acquisition_time_utc"] is None,
            item["acquisition_time_utc"] or "",
            item["item_id"],
        )
    )
    result = pair_acquisitions(acquisitions, sensor, event_start, event_end)
    result["candidate_acquisitions"] = acquisitions
    result["normalized_sensor_result_sha256"] = _sha256_bytes(_canonical_json(result))
    return result


def _missing_sensor_result(reason: str) -> dict[str, Any]:
    result = {
        "catalogue_pair_status": "no_qualifying_pair",
        "pixel_qa_status": "not_reached_because_no_qualifying_catalogue_pair",
        "availability_reasons": [reason],
        "accepted_pairs": [],
        "rejected_pairs": [],
        "counts": {
            "candidate_acquisitions": 0,
            "pre_event_acquisitions": 0,
            "during_event_interval_acquisitions": 0,
            "post_event_acquisitions": 0,
            "accepted_pairs": 0,
            "rejected_pairs": 0,
        },
        "candidate_acquisitions": [],
        "raw_catalogue_pages": [],
    }
    result["normalized_sensor_result_sha256"] = _sha256_bytes(_canonical_json(result))
    return result


def _finite_coordinate(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < minimum or numeric > maximum:
        return None
    return numeric


def _candidate_result(
    candidate: dict[str, Any],
    cache_root: Path,
    *,
    offline: bool,
    cache_reference: str,
    fetcher: Any,
) -> dict[str, Any]:
    normalized_event = normalize_event_interval(candidate.get("event_time") or {})
    discovery = candidate.get("geographic_discovery") or {}
    latitude = _finite_coordinate(
        discovery.get("observation_location_latitude"), -90, 90
    )
    longitude = _finite_coordinate(
        discovery.get("observation_location_longitude"), -180, 180
    )
    candidate_id = str(candidate["candidate_id"])
    if not SAFE_COMPONENT_RE.fullmatch(candidate_id):
        raise ValueError(f"Candidate ID is unsafe for cache paths: {candidate_id!r}.")

    public_identity = {
        "candidate_id": candidate_id,
        "source_collection": candidate.get("source_collection"),
        "source_record_id": candidate.get("source_record_id"),
        "source_record_url": candidate.get("source_record_url"),
        "source_record_canonical_sha256": candidate.get(
            "source_record_canonical_sha256"
        ),
    }
    query_point = {
        "longitude": longitude,
        "latitude": latitude,
        "coordinate_order": "longitude_latitude",
        "crs": "EPSG:4326",
        "role": (
            "Public RegObs observation-location discovery point used only for STAC "
            "catalogue intersection queries; not a release/start/stop target."
        ),
    }
    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "public_discovery_identity": public_identity,
        "catalogue_query_point": query_point,
        "source_selection_evidence": {
            "reported_fracture_or_crown_height_present": True,
            "stop_point_or_extent_present": True,
            "target_coordinates_accessed": False,
            "attachments_accessed": False,
        },
        "event_interval": normalized_event,
        "validation_contract_eligibility": (
            "not_evaluated; imagery availability does not confer eligibility"
        ),
        "sentinel_1_grd": None,
        "sentinel_2_l2a": None,
    }

    query_interval = _search_interval(normalized_event)
    if query_interval is None:
        for sensor in SENSORS:
            result[sensor] = _missing_sensor_result(
                "catalogue_not_queried_because_event_interval_is_missing_or_invalid"
            )
    elif latitude is None or longitude is None:
        for sensor in SENSORS:
            result[sensor] = _missing_sensor_result(
                "catalogue_not_queried_because_discovery_coordinates_are_missing_or_invalid"
            )
    else:
        for sensor, definition in SENSORS.items():
            body = build_search_body(
                definition["collection"], longitude, latitude, query_interval
            )
            relative_dir = f"searches/{candidate_id}/{sensor}"
            features, pages = acquire_search(
                cache_root / "searches" / candidate_id / sensor,
                body,
                offline=offline,
                cache_reference_prefix=f"{cache_reference.rstrip('/')}/{relative_dir}",
                fetcher=fetcher,
            )
            sensor_result = _normalize_and_pair(
                features,
                sensor,
                longitude,
                latitude,
                normalized_event,
            )
            sensor_result["collection"] = definition["collection"]
            sensor_result["query_interval_utc"] = query_interval
            sensor_result["request_body_sha256"] = _sha256_bytes(_canonical_json(body))
            sensor_result["raw_catalogue_pages"] = pages
            unhashed = dict(sensor_result)
            unhashed.pop("normalized_sensor_result_sha256", None)
            sensor_result["normalized_sensor_result_sha256"] = _sha256_bytes(
                _canonical_json(unhashed)
            )
            result[sensor] = sensor_result

    unhashed_candidate = dict(result)
    result["normalized_candidate_sha256"] = _sha256_bytes(
        _canonical_json(unhashed_candidate)
    )
    return result


def _sensor_counts(candidates: Iterable[dict[str, Any]], sensor: str) -> dict[str, int]:
    results = [candidate[sensor] for candidate in candidates]
    return {
        "candidates_catalogue_pair_found": sum(
            result["catalogue_pair_status"] == "catalogue_pair_found"
            for result in results
        ),
        "candidates_no_qualifying_pair": sum(
            result["catalogue_pair_status"] == "no_qualifying_pair"
            for result in results
        ),
        "candidates_requiring_pixel_qa": sum(
            result["pixel_qa_status"] == "requires_pixel_qa" for result in results
        ),
        "candidate_acquisitions": sum(
            result["counts"]["candidate_acquisitions"] for result in results
        ),
        "accepted_pairs": sum(result["counts"]["accepted_pairs"] for result in results),
        "rejected_pairs": sum(result["counts"]["rejected_pairs"] for result in results),
    }


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(str(key) for key in value).union(
            *(_all_keys(item) for item in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def _validate_artifact(artifact: dict[str, Any]) -> None:
    if artifact["schema"] != SCHEMA:
        raise ValueError("Unexpected preflight schema.")
    for field in (
        "predictions_generated",
        "model_code_imported",
        "holdout_partition_assigned",
        "holdout_targets_accessed",
    ):
        if artifact[field] is not False:
            raise ValueError(f"Metadata-only preflight requires {field}=false.")
    keys = {key.lower().replace("_", "") for key in _all_keys(artifact)}
    leaked = keys.intersection(TARGET_COORDINATE_KEYS)
    if leaked:
        raise ValueError(f"Target-coordinate or attachment keys leaked into artifact: {leaked}.")
    if "does not confer" not in artifact["claim_boundary"]:
        raise ValueError("Preflight must preserve the validation-eligibility claim boundary.")
    identity = artifact.get("normalized_artifact_sha256")
    if not isinstance(identity, str) or not SHA256_RE.fullmatch(identity):
        raise ValueError("Preflight requires a normalized artifact SHA-256 identity.")
    unhashed = deepcopy(artifact)
    unhashed.pop("normalized_artifact_sha256")
    if _sha256_bytes(_canonical_json(unhashed)) != identity:
        raise ValueError("Normalized artifact SHA-256 does not match its content.")


def build_preflight(
    candidate_inventory_path: Path,
    experiment_path: Path,
    cache_root: Path,
    *,
    offline: bool,
    cache_reference: str = ".validation-cache/public-event-imagery-preflight-v1",
    fetcher: Any = _fetch_descriptor,
) -> dict[str, Any]:
    inventory_bytes = candidate_inventory_path.read_bytes()
    experiment_bytes = experiment_path.read_bytes()
    inventory = json.loads(inventory_bytes)
    experiment = json.loads(experiment_bytes)
    if inventory.get("candidate_funnel_id") != "public-event-candidates-v1":
        raise ValueError("Unexpected public candidate inventory identity.")
    if experiment.get("experiment_id") != EXPERIMENT_ID:
        raise ValueError("Unexpected public validation experiment identity.")
    selected = select_source_candidates(inventory)

    candidates = [
        _candidate_result(
            candidate,
            cache_root,
            offline=offline,
            cache_reference=cache_reference,
            fetcher=fetcher,
        )
        for candidate in selected
    ]
    counts = {
        "source_candidates_selected": len(candidates),
        "sentinel_1_grd": _sensor_counts(candidates, "sentinel_1_grd"),
        "sentinel_2_l2a": _sensor_counts(candidates, "sentinel_2_l2a"),
        "candidates_with_catalogue_pairs_for_both_sensors": sum(
            candidate["sentinel_1_grd"]["catalogue_pair_status"]
            == "catalogue_pair_found"
            and candidate["sentinel_2_l2a"]["catalogue_pair_status"]
            == "catalogue_pair_found"
            for candidate in candidates
        ),
        "holdout_partitions_assigned": 0,
        "predictions_generated": 0,
    }
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "preflight_id": PREFLIGHT_ID,
        "experiment_id": EXPERIMENT_ID,
        "experiment_spec_sha256": _sha256_bytes(experiment_bytes),
        "source_candidate_inventory_id": inventory["candidate_funnel_id"],
        "source_candidate_inventory_sha256": _sha256_bytes(inventory_bytes),
        "catalogue_acquisition_frozen_at_utc": FROZEN_ACQUISITION_AT_UTC,
        "stage": "metadata_only_satellite_catalogue_availability_preflight",
        "predictions_generated": False,
        "model_code_imported": False,
        "holdout_partition_assigned": False,
        "holdout_targets_accessed": False,
        "regobs_attachments_accessed": False,
        "regobs_start_stop_target_coordinates_accessed": False,
        "raster_assets_requested_or_downloaded": False,
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Model scores are relative indices, not probabilities."
        ),
        "claim_boundary": (
            "This artifact reports catalogue metadata availability only. A catalogue pair does "
            "not establish visible avalanche evidence, usable ground truth, model accuracy, or "
            "validation-contract eligibility; imagery availability does not confer eligibility."
        ),
        "source_selection_rule": (
            "Derive RegObs candidates from the frozen public-event-candidates-v1 inventory where "
            "source_collection is RegObs public API v5, fracture_height_value is non-null, and "
            "stop_point_present or stop_extent_present is true. Do not open attachments or "
            "start/stop coordinates."
        ),
        "catalogue": {
            "provider": "Copernicus Data Space Ecosystem",
            "documentation_url": CATALOGUE_DOCUMENTATION_URL,
            "endpoint_verified_at_utc": FROZEN_ACQUISITION_AT_UTC,
            "stac_root_url": STAC_ROOT_URL,
            "search_url": STAC_SEARCH_URL,
            "access": "anonymous public HTTPS metadata requests; no account or token",
            "collections": SENSORS,
            "cache_reference": cache_reference,
            "cache_policy": (
                "Immutable request/response bytes in a gitignored cache. Differing bytes fail; "
                "offline replay requires every cached request and response."
            ),
            "assets_policy": (
                "STAC fields excludes assets; no asset href is followed and no raster is requested."
            ),
        },
        "frozen_query_and_pairing_rules": {
            "rules_frozen_before_candidate_availability_was_examined": True,
            "search_margin_before_event_days": SEARCH_MARGIN_DAYS,
            "search_margin_after_event_days": SEARCH_MARGIN_DAYS,
            "spatial_rule": (
                "The STAC search uses an EPSG:4326 Point at the public RegObs discovery location, "
                "and each returned Polygon/MultiPolygon must independently contain that point."
            ),
            "event_interval_rule": (
                "Use provider_earliest through provider_latest after explicit-offset UTC "
                "conversion. Preserve a missing provider_earliest and use provider_latest as a "
                "zero-duration pairing instant; a missing provider_latest prevents querying."
            ),
            "bracketing_rule": (
                "Pre acquisition time must be strictly earlier than event start and post "
                "acquisition time strictly later than event end. During-interval acquisitions "
                "are recorded but never used as pre or post."
            ),
            "sentinel_1_grd_pair_rule": (
                "Both items must intersect the discovery point and have identical non-missing "
                "orbit direction, relative orbit, acquisition mode, and polarization set."
            ),
            "sentinel_2_l2a_pair_rule": (
                "Both items must come from sentinel-2-l2a, intersect the discovery point, and "
                "have the same non-missing MGRS tile. Catalogue cloud percentage is recorded "
                "but is not a compatibility threshold."
            ),
            "temporal_baseline_rule": (
                "Record exact baseline seconds and derived days for every accepted and rejected "
                "bracketing combination; do not rank or select pairs using availability results."
            ),
            "rejection_rule": (
                "Retain every tested bracketing combination and all deterministic rejection "
                "reason codes. Missing metadata remains null and explicit."
            ),
            "pixel_qa_rule": (
                "Every catalogue pair requires later pixel QA. In particular, Sentinel-2 "
                "product-wide cloud percentage is not proof of local clear sky or usable ground "
                "truth. This preflight does not perform that QA."
            ),
        },
        "counts": counts,
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    _validate_artifact(artifact)
    return artifact


def _assert_outside_protected(path: Path, label: str) -> None:
    resolved = path.resolve()
    protected_paths = (
        REPOSITORY_ROOT / "runtime",
        REPOSITORY_ROOT / "DATA",
        REPOSITORY_ROOT.parent / "DATA",
    )
    for protected in protected_paths:
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        raise ValueError(f"{label} may not be under protected path {protected}.")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-inventory",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "validation-data"
            / "candidates"
            / "public-event-candidates-v1.json"
        ),
    )
    parser.add_argument(
        "--experiment",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "validation-data"
            / "experiments"
            / "public-data-field-validation-v1.json"
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / ".validation-cache"
            / "public-event-imagery-preflight-v1"
        ),
    )
    parser.add_argument(
        "--cache-reference",
        default=".validation-cache/public-event-imagery-preflight-v1",
        help="Stable repository-relative label written to the artifact.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "validation-data"
            / "candidates"
            / "public-event-imagery-preflight-v1.json"
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require immutable-cache replay; never issue a network request.",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    output_path = args.output.resolve()
    _assert_outside_protected(cache_root, "Validation cache")
    _assert_outside_protected(output_path, "Preflight output")
    artifact = build_preflight(
        args.candidate_inventory.resolve(),
        args.experiment.resolve(),
        cache_root,
        offline=args.offline,
        cache_reference=args.cache_reference,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(_pretty_json(artifact))
    s1 = artifact["counts"]["sentinel_1_grd"]
    s2 = artifact["counts"]["sentinel_2_l2a"]
    print(
        f"Wrote {artifact['counts']['source_candidates_selected']} candidates to "
        f"{output_path}; S1 pairs={s1['candidates_catalogue_pair_found']}, "
        f"S2 pairs={s2['candidates_catalogue_pair_found']}, predictions=0, holdouts=0."
    )


if __name__ == "__main__":
    main()
