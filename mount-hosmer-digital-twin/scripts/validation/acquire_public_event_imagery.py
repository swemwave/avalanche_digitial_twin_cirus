"""Acquire blinded public-imagery chips for the frozen RegObs candidate cohort.

The script selects one catalogue-compatible pair per sensor using only frozen
metadata (shortest temporal baseline, then pair id), maps the Copernicus item to
the anonymous AWS Earth Search archive, and stores a 12 km square chip around the
public RegObs discovery point.  It never reads RegObs target coordinates,
attachments, model code, predictions, or holdout targets.

Raw STAC responses, small source metadata documents, and derived raster chips are
immutable in the gitignored cache.  Every cached file and every raster pixel
payload is SHA-256 identified.  ``--offline`` prohibits all network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-imagery-acquisition-v2"
ACQUISITION_ID = "public-event-imagery-acquisition-v2"
FROZEN_AT_UTC = "2026-08-13T00:00:00Z"
EARTH_SEARCH_ROOT = "https://earth-search.aws.element84.com/v1/"
EARTH_SEARCH_HOST = urllib.parse.urlparse(EARTH_SEARCH_ROOT).hostname
CHIP_RADIUS_M = 6_000
CHIP_RESOLUTION_M = 10
CHIP_SIZE = 2 * CHIP_RADIUS_M // CHIP_RESOLUTION_M
USER_AGENT = "avycore-public-event-imagery-acquisition/2"
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

SENSOR_COLLECTIONS = {
    "sentinel_1_grd": "sentinel-1-grd",
    "sentinel_2_l2a": "sentinel-2-c1-l2a",
}
SENSOR_ASSETS = {
    "sentinel_2_l2a": (
        "visual",
        "nir",
        "swir16",
        "scl",
        "cloud",
        "snow",
        "granule_metadata",
        "product_metadata",
        "tileinfo_metadata",
    ),
}
RASTER_ASSETS = frozenset(
    {"vv", "vh", "hh", "hv", "visual", "nir", "swir16", "scl", "cloud", "snow"}
)
NEAREST_ASSETS = frozenset({"scl", "cloud", "snow"})


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_path_reference(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _write_immutable(path: Path, value: bytes) -> None:
    if path.exists():
        if path.read_bytes() != value:
            raise ValueError(
                f"Immutable cache identity conflict at {path}; choose a new cache root."
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError(f"Concurrent immutable-cache conflict at {path}.")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Timestamp lacks an explicit offset: {value!r}.")
    return parsed.astimezone(timezone.utc)


def select_pair(sensor_result: dict[str, Any]) -> dict[str, Any] | None:
    """Select without pixels, cloud rank, model output, or target geometry."""

    accepted = sensor_result.get("accepted_pairs") or []
    if not accepted:
        return None
    return min(
        accepted,
        key=lambda pair: (
            int(pair["temporal_baseline_seconds"]),
            int(pair["pre_to_event_start_seconds"]),
            int(pair["event_end_to_post_seconds"]),
            str(pair["pair_id"]),
        ),
    )


def local_utm_epsg(longitude: float, latitude: float) -> int:
    """Return the WGS84 UTM zone, including Norway and Svalbard exceptions."""

    if not (-180 <= longitude <= 180 and 0 <= latitude < 84):
        raise ValueError("Public candidates require finite northern UTM coordinates.")
    zone = int(math.floor((longitude + 180) / 6)) + 1
    if 56 <= latitude < 64 and 3 <= longitude < 12:
        zone = 32
    elif 72 <= latitude < 84:
        if 0 <= longitude < 9:
            zone = 31
        elif 9 <= longitude < 21:
            zone = 33
        elif 21 <= longitude < 33:
            zone = 35
        elif 33 <= longitude < 42:
            zone = 37
    return 32600 + zone


def _request_descriptor(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "url": url,
        "method": method,
        "headers": {
            "Accept": "application/geo+json, application/json, application/xml, image/tiff",
            "Content-Type": "application/json" if body is not None else None,
            "User-Agent": USER_AGENT,
        },
        "body": body,
    }


def _validate_https_url(url: str, *, allowed_hosts: Iterable[str]) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in frozenset(allowed_hosts):
        raise ValueError(f"Refusing unexpected public-asset URL: {url!r}.")


def _fetch(descriptor: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    headers = {
        key: value
        for key, value in descriptor["headers"].items()
        if value is not None
    }
    body = descriptor.get("body")
    request = urllib.request.Request(
        descriptor["url"],
        data=_canonical_json(body) if body is not None else None,
        method=descriptor["method"],
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = response.read()
            response_metadata = {
                "status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "content_length_header": response.headers.get("Content-Length"),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "final_url": response.geturl(),
            }
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Anonymous public request failed for {descriptor['url']}: {exc}"
        ) from exc
    return payload, response_metadata


def _cache_request(
    cache_dir: Path,
    name: str,
    descriptor: dict[str, Any],
    *,
    offline: bool,
    allowed_hosts: Iterable[str],
) -> tuple[bytes, dict[str, Any]]:
    _validate_https_url(descriptor["url"], allowed_hosts=allowed_hosts)
    request_path = cache_dir / f"{name}-request.json"
    response_path = cache_dir / f"{name}-response"
    metadata_path = cache_dir / f"{name}-response-metadata.json"
    request_bytes = _canonical_json(descriptor)
    _write_immutable(request_path, request_bytes)
    paths = (response_path, metadata_path)
    if any(path.exists() for path in paths) and not all(path.exists() for path in paths):
        raise ValueError(f"Partial immutable response cache for {cache_dir / name}.")
    if response_path.exists():
        payload = response_path.read_bytes()
        response_metadata = json.loads(metadata_path.read_bytes())
    else:
        if offline:
            raise FileNotFoundError(f"Offline replay is missing {response_path}.")
        payload, response_metadata = _fetch(descriptor)
        _write_immutable(response_path, payload)
        _write_immutable(metadata_path, _canonical_json(response_metadata))
    return payload, {
        "request_cache_path": _stable_path_reference(request_path),
        "request_bytes": len(request_bytes),
        "request_sha256": _sha256_bytes(request_bytes),
        "response_cache_path": _stable_path_reference(response_path),
        "response_bytes": len(payload),
        "response_sha256": _sha256_bytes(payload),
        "response_metadata_cache_path": _stable_path_reference(metadata_path),
        "response_metadata_sha256": _sha256_file(metadata_path),
        "response_metadata": response_metadata,
    }


def earth_search_s1_id(copernicus_item_id: str) -> str:
    if not copernicus_item_id.endswith("_COG"):
        raise ValueError(f"Unexpected Copernicus Sentinel-1 COG id {copernicus_item_id!r}.")
    parts = copernicus_item_id.split("_")
    if len(parts) < 10 or parts[-1] != "COG":
        raise ValueError(f"Unexpected Copernicus Sentinel-1 COG id {copernicus_item_id!r}.")
    # CDSE appends its product checksum and COG marker. Earth Search identifies
    # the same sensing slice without either suffix and exposes its own source
    # product checksum through s1:product_identifier.
    return "_".join(parts[:-2])


def _s2_product_prefix(copernicus_item_id: str) -> str:
    parts = copernicus_item_id.split("_")
    if len(parts) < 3 or parts[1] != "MSIL2A":
        raise ValueError(f"Unexpected Copernicus Sentinel-2 id {copernicus_item_id!r}.")
    return "_".join(parts[:3])


def _s2_mgrs(properties: dict[str, Any]) -> str | None:
    zone = properties.get("mgrs:utm_zone")
    band = properties.get("mgrs:latitude_band")
    square = properties.get("mgrs:grid_square")
    if isinstance(zone, int) and isinstance(band, str) and isinstance(square, str):
        return f"{zone:02d}{band.upper()}{square.upper()}"
    return None


def match_s2_item(
    features: list[dict[str, Any]], copernicus_item_id: str, mgrs_tile: str
) -> dict[str, Any]:
    prefix = _s2_product_prefix(copernicus_item_id)
    matches = []
    for feature in features:
        properties = feature.get("properties") or {}
        product_uri = str(properties.get("s2:product_uri") or "").removesuffix(".SAFE")
        if product_uri.startswith(prefix) and _s2_mgrs(properties) == mgrs_tile:
            matches.append(feature)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one anonymous AWS Sentinel-2 acquisition match for {copernicus_item_id}; "
            f"found {len(matches)}."
        )
    return matches[0]


def _json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return value


def _earth_item(
    sensor: str,
    copernicus_item_id: str,
    acquisition_time_utc: str,
    mgrs_tile: str | None,
    longitude: float,
    latitude: float,
    cache_dir: Path,
    *,
    offline: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    collection = SENSOR_COLLECTIONS[sensor]
    allowed = (EARTH_SEARCH_HOST,)
    if sensor == "sentinel_1_grd":
        item_id = earth_search_s1_id(copernicus_item_id)
        url = urllib.parse.urljoin(
            EARTH_SEARCH_ROOT, f"collections/{collection}/items/{item_id}"
        )
        payload, cache_record = _cache_request(
            cache_dir,
            "item",
            _request_descriptor(url),
            offline=offline,
            allowed_hosts=allowed,
        )
        item = _json_object(payload, f"Earth Search item {item_id}")
        if item.get("id") != item_id:
            raise ValueError("Earth Search Sentinel-1 item identity mismatch.")
        mapping = {
            "mapping": "same_sensing_acquisition_cross_catalog_match",
            "copernicus_item_id": copernicus_item_id,
            "earth_search_item_id": item_id,
            "earth_search_product_identifier": (item.get("properties") or {}).get(
                "s1:product_identifier"
            ),
            "processing_product_equivalence": (
                "not byte-identical: CDSE and Earth Search expose the same Sentinel-1 "
                "sensing acquisition under catalogue-specific processing/checksum identities; "
                "the mismatch is retained as lineage"
            ),
        }
    else:
        acquired = _parse_utc(acquisition_time_utc)
        body = {
            "collections": [collection],
            "intersects": {
                "type": "Point",
                "coordinates": [longitude, latitude],
            },
            "datetime": (
                f"{_format_utc(acquired - timedelta(minutes=10))}/"
                f"{_format_utc(acquired + timedelta(minutes=10))}"
            ),
            "limit": 20,
        }
        payload, cache_record = _cache_request(
            cache_dir,
            "search",
            _request_descriptor(
                urllib.parse.urljoin(EARTH_SEARCH_ROOT, "search"),
                method="POST",
                body=body,
            ),
            offline=offline,
            allowed_hosts=allowed,
        )
        response = _json_object(payload, "Earth Search Sentinel-2 response")
        features = response.get("features")
        if not isinstance(features, list) or mgrs_tile is None:
            raise ValueError("Sentinel-2 search response or frozen MGRS tile is missing.")
        item = match_s2_item(features, copernicus_item_id, mgrs_tile)
        mapping = {
            "mapping": "same_sensing_acquisition_and_mgrs_tile_cross_catalog_match",
            "copernicus_item_id": copernicus_item_id,
            "earth_search_item_id": item["id"],
            "earth_search_product_uri": (item.get("properties") or {}).get(
                "s2:product_uri"
            ),
            "processing_product_equivalence": (
                "not identical: Earth Search serves a different Sentinel-2 processing-"
                "baseline product for the same sensing acquisition; this mismatch is "
                "retained as lineage and must be considered in pixel QA"
            ),
        }
    mapping["catalogue_cache"] = cache_record
    mapping["earth_search_item_canonical_sha256"] = _sha256_bytes(
        _canonical_json(item)
    )
    return item, mapping


def _s3_https(href: str) -> str:
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        return href
    region_by_bucket = {"sentinel-s1-l1c": "eu-central-1"}
    region = region_by_bucket.get(parsed.netloc)
    if region is None:
        raise ValueError(f"No reviewed anonymous HTTPS mapping for S3 bucket {parsed.netloc!r}.")
    quoted_path = urllib.parse.quote(parsed.path.lstrip("/"), safe="/-_.~")
    return f"https://{parsed.netloc}.s3.{region}.amazonaws.com/{quoted_path}"


def _asset_allowed_hosts(sensor: str) -> tuple[str, ...]:
    if sensor == "sentinel_1_grd":
        return ("sentinel-s1-l1c.s3.eu-central-1.amazonaws.com",)
    return ("e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com",)


def _sensor_assets(sensor: str, pair: dict[str, Any]) -> tuple[str, ...]:
    if sensor == "sentinel_2_l2a":
        return SENSOR_ASSETS[sensor]
    polarizations = pair.get("polarizations")
    if not isinstance(polarizations, list) or not polarizations:
        raise ValueError("Frozen Sentinel-1 pair lacks its polarization set.")
    normalized = tuple(sorted(str(value).lower() for value in polarizations))
    if any(value not in {"hh", "hv", "vh", "vv"} for value in normalized):
        raise ValueError(f"Unsupported Sentinel-1 polarization set {polarizations!r}.")
    return (
        *normalized,
        "safe-manifest",
        *(f"annotation-{value}" for value in normalized),
        *(f"schema-calibration-{value}" for value in normalized),
        *(f"schema-noise-{value}" for value in normalized),
    )


def _s1_annotation_href(measurement_href: str, polarization: str) -> str:
    """Derive the SAFE product annotation omitted by Earth Search's asset map.

    Earth Search currently labels the RFI document as ``schema-product-*``.
    The SAFE manifest and deterministic SAFE directory convention identify the
    actual product annotation beside ``annotation/calibration``.  Retaining the
    derived URL and the manifest hash makes this catalogue workaround auditable.
    """

    parsed = re.fullmatch(
        rf"(?P<root>.+)/measurement/(?P<mode>iw|ew|sm|wv)-{re.escape(polarization)}\.tiff",
        measurement_href,
    )
    if parsed is None:
        raise ValueError(
            f"Unexpected Sentinel-1 measurement path for {polarization!r}: "
            f"{measurement_href!r}."
        )
    return (
        f"{parsed.group('root')}/annotation/"
        f"{parsed.group('mode')}-{polarization}.xml"
    )


def _pixel_payload_sha256(
    arrays: Any, *, transform: Any, crs: Any, nodata: Any
) -> str:
    digest = hashlib.sha256()
    header = {
        "shape": list(arrays.shape),
        "dtype": str(arrays.dtype),
        "transform": [float(value) for value in transform],
        "crs": str(crs),
        "nodata": nodata,
        "byte_order": "C",
    }
    digest.update(_canonical_json(header))
    digest.update(arrays.tobytes(order="C"))
    return digest.hexdigest()


def _acquire_raster_chip(
    href: str,
    output_path: Path,
    *,
    target_epsg: int,
    center_x: float,
    center_y: float,
    asset_name: str,
    offline: bool,
    force_resampling: str | None = None,
) -> dict[str, Any]:
    resampling_name = force_resampling or (
        "nearest" if asset_name in NEAREST_ASSETS else "bilinear"
    )
    if resampling_name not in {"nearest", "bilinear"}:
        raise ValueError(f"Unsupported raster resampling {resampling_name!r}.")
    descriptor = {
        "source_href": href,
        "access": "anonymous HTTPS COG range reads; no account, token, or special terms",
        "target_crs": f"EPSG:{target_epsg}",
        "target_resolution_m": CHIP_RESOLUTION_M,
        "target_width": CHIP_SIZE,
        "target_height": CHIP_SIZE,
        "target_bounds_m": [
            center_x - CHIP_RADIUS_M,
            center_y - CHIP_RADIUS_M,
            center_x + CHIP_RADIUS_M,
            center_y + CHIP_RADIUS_M,
        ],
        "resampling": resampling_name,
        "georeferencing_rule": (
            "Use source affine CRS when present; otherwise use the complete source GCP "
            "set and its CRS with rasterio.warp.reproject. Never treat identity pixels "
            "from a GCP-only Sentinel-1 GRD TIFF as georeferenced coordinates."
        ),
        "source_subset_only": True,
        "full_source_asset_downloaded": False,
    }
    descriptor_path = output_path.with_suffix(output_path.suffix + ".request.json")
    georeferencing_path = output_path.with_suffix(
        output_path.suffix + ".source-georeferencing.json"
    )
    _write_immutable(descriptor_path, _canonical_json(descriptor))
    if not output_path.exists():
        if offline:
            raise FileNotFoundError(f"Offline replay is missing raster chip {output_path}.")
        try:
            import numpy as np
            import rasterio
            from affine import Affine
            from rasterio.enums import Resampling
            from rasterio.vrt import WarpedVRT
            from rasterio.warp import reproject
        except ImportError as exc:
            raise RuntimeError(
                "Raster acquisition requires bake-time rasterio dependencies."
            ) from exc

        transform = Affine(
            CHIP_RESOLUTION_M,
            0.0,
            center_x - CHIP_RADIUS_M,
            0.0,
            -CHIP_RESOLUTION_M,
            center_y + CHIP_RADIUS_M,
        )
        resampling = (
            Resampling.nearest
            if resampling_name == "nearest"
            else Resampling.bilinear
        )
        with rasterio.Env(
            AWS_NO_SIGN_REQUEST="YES",
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff",
        ):
            with rasterio.open(href) as source:
                source_nodata = source.nodata
                nodata = source_nodata if source_nodata is not None else 0
                gcps, gcp_crs = source.gcps
                if source.crs is None and gcps and gcp_crs is not None:
                    arrays = np.full(
                        (source.count, CHIP_SIZE, CHIP_SIZE),
                        nodata,
                        dtype=source.dtypes[0],
                    )
                    masks = np.zeros(
                        (source.count, CHIP_SIZE, CHIP_SIZE), dtype=np.uint8
                    )
                    for band_index in range(1, source.count + 1):
                        reproject(
                            source=rasterio.band(source, band_index),
                            destination=arrays[band_index - 1],
                            gcps=gcps,
                            src_crs=gcp_crs,
                            src_nodata=source_nodata,
                            dst_transform=transform,
                            dst_crs=f"EPSG:{target_epsg}",
                            dst_nodata=nodata,
                            resampling=resampling,
                        )
                        source_mask = source.read_masks(band_index)
                        reproject(
                            source=source_mask,
                            destination=masks[band_index - 1],
                            gcps=gcps,
                            src_crs=gcp_crs,
                            src_nodata=0,
                            dst_transform=transform,
                            dst_crs=f"EPSG:{target_epsg}",
                            dst_nodata=0,
                            resampling=Resampling.nearest,
                        )
                    profile = source.profile.copy()
                    source_georeferencing = {
                        "method": "GDAL GCP transformer via rasterio.warp.reproject",
                        "source_crs": None,
                        "gcp_crs": str(gcp_crs),
                        "gcp_count": len(gcps),
                        "reason": (
                            "Sentinel-1 GRD measurement TIFF has GCP geolocation but no "
                            "affine CRS; WarpedVRT alone produced an invalid empty chip"
                        ),
                    }
                else:
                    source_georeferencing = {
                        "method": "GDAL affine transformer via rasterio.WarpedVRT",
                        "source_crs": str(source.crs),
                        "gcp_crs": str(gcp_crs) if gcp_crs is not None else None,
                        "gcp_count": len(gcps),
                    }
                    with WarpedVRT(
                        source,
                        crs=f"EPSG:{target_epsg}",
                        transform=transform,
                        width=CHIP_SIZE,
                        height=CHIP_SIZE,
                        nodata=nodata,
                        resampling=resampling,
                    ) as vrt:
                        arrays = vrt.read(masked=False)
                        masks = vrt.read_masks()
                        profile = vrt.profile.copy()
        if not np.isfinite(arrays).all() and not np.issubdtype(arrays.dtype, np.floating):
            raise ValueError(f"Non-finite integer pixels in {href}.")
        profile.update(
            driver="GTiff",
            compress="DEFLATE",
            predictor=2 if np.issubdtype(arrays.dtype, np.integer) else 3,
            tiled=True,
            blockxsize=256,
            blockysize=256,
            transform=transform,
            crs=f"EPSG:{target_epsg}",
            width=CHIP_SIZE,
            height=CHIP_SIZE,
            count=arrays.shape[0],
            nodata=nodata,
        )
        temporary = output_path.with_suffix(output_path.suffix + f".tmp-{os.getpid()}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with rasterio.open(temporary, "w", **profile) as target:
                target.write(arrays)
                target.write_mask(np.min(masks, axis=0))
            _write_immutable(output_path, temporary.read_bytes())
            _write_immutable(
                georeferencing_path, _canonical_json(source_georeferencing)
            )
        finally:
            temporary.unlink(missing_ok=True)

    if not georeferencing_path.exists():
        raise ValueError(
            f"Raster chip lacks immutable source-georeferencing lineage: "
            f"{georeferencing_path}."
        )
    source_georeferencing = json.loads(georeferencing_path.read_bytes())

    try:
        import rasterio
    except ImportError as exc:
        raise RuntimeError("Raster verification requires rasterio.") from exc
    with rasterio.open(output_path) as source:
        arrays = source.read(masked=False)
        masks = source.read_masks()
        pixel_hash = _pixel_payload_sha256(
            arrays, transform=source.transform, crs=source.crs, nodata=source.nodata
        )
        raster_metadata = {
            "driver": source.driver,
            "width": source.width,
            "height": source.height,
            "count": source.count,
            "dtype": list(source.dtypes),
            "nodata": source.nodata,
            "crs": str(source.crs),
            "transform": [float(value) for value in source.transform],
            "bounds": [float(value) for value in source.bounds],
            "valid_pixel_count_all_bands": int((masks > 0).all(axis=0).sum()),
            "total_pixel_count": int(source.width * source.height),
            "source_georeferencing": source_georeferencing,
        }
    return {
        "asset_name": asset_name,
        "source_href": href,
        "cache_path": _stable_path_reference(output_path),
        "bytes": output_path.stat().st_size,
        "sha256": _sha256_file(output_path),
        "pixel_payload_sha256": pixel_hash,
        "request_descriptor_path": _stable_path_reference(descriptor_path),
        "request_descriptor_sha256": _sha256_file(descriptor_path),
        "source_georeferencing_path": _stable_path_reference(georeferencing_path),
        "source_georeferencing_sha256": _sha256_file(georeferencing_path),
        "raster": raster_metadata,
        "source_subset_only": True,
        "full_source_asset_downloaded": False,
    }


def _acquire_metadata_asset(
    href: str,
    output_path: Path,
    *,
    offline: bool,
    allowed_hosts: tuple[str, ...],
) -> dict[str, Any]:
    payload, record = _cache_request(
        output_path.parent,
        output_path.stem,
        _request_descriptor(href),
        offline=offline,
        allowed_hosts=allowed_hosts,
    )
    return {
        "asset_name": output_path.stem,
        "source_href": href,
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
        "cache": record,
        "full_source_asset_downloaded": True,
    }


def _candidate_acquisition(
    candidate: dict[str, Any], cache_root: Path, *, offline: bool
) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    if not SAFE_COMPONENT_RE.fullmatch(candidate_id):
        raise ValueError(f"Unsafe candidate id {candidate_id!r}.")
    point = candidate["catalogue_query_point"]
    longitude = float(point["longitude"])
    latitude = float(point["latitude"])
    target_epsg = local_utm_epsg(longitude, latitude)
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise RuntimeError("Imagery acquisition requires bake-time pyproj.") from exc
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True)
    center_x, center_y = transformer.transform(longitude, latitude)

    result: dict[str, Any] = {
        "candidate_id": candidate_id,
        "discovery_point": {
            "longitude": longitude,
            "latitude": latitude,
            "crs": "EPSG:4326",
            "role": "discovery-only chip centre; not a release/start/stop target",
        },
        "chip_grid": {
            "crs": f"EPSG:{target_epsg}",
            "units": "m",
            "coordinate_order": "easting_northing",
            "center": [center_x, center_y],
            "resolution_m": CHIP_RESOLUTION_M,
            "width": CHIP_SIZE,
            "height": CHIP_SIZE,
            "radius_m": CHIP_RADIUS_M,
        },
        "sentinel_1_grd": None,
        "sentinel_2_l2a": None,
    }
    for sensor in SENSOR_COLLECTIONS:
        pair = select_pair(candidate[sensor])
        if pair is None:
            result[sensor] = {
                "status": "not_acquired_no_qualifying_catalogue_pair",
                "selected_pair": None,
                "scenes": [],
            }
            continue
        selected_pair = {
            **pair,
            "selection_rule": (
                "Minimum temporal_baseline_seconds, then pre gap, post gap, and pair_id; "
                "no pixels, cloud rank, target geometry, model output, or prediction used."
            ),
        }
        scenes = []
        for position in ("pre", "post"):
            item_id = str(pair[f"{position}_item_id"])
            scene_cache = cache_root / candidate_id / sensor / position
            earth_item, mapping = _earth_item(
                sensor,
                item_id,
                str(pair[f"{position}_acquisition_time_utc"]),
                pair.get("mgrs_tile"),
                longitude,
                latitude,
                scene_cache,
                offline=offline,
            )
            assets = earth_item.get("assets") or {}
            acquired_assets = []
            for asset_name in _sensor_assets(sensor, pair):
                if asset_name.startswith("annotation-"):
                    polarization = asset_name.removeprefix("annotation-")
                    measurement = assets.get(polarization)
                    if not isinstance(measurement, dict) or not isinstance(
                        measurement.get("href"), str
                    ):
                        raise ValueError(
                            f"Earth Search item {earth_item.get('id')} lacks "
                            f"measurement asset {polarization!r}."
                        )
                    asset = {
                        "href": _s1_annotation_href(
                            measurement["href"], polarization
                        ),
                        "derived_from_asset": polarization,
                        "derivation": "reviewed Sentinel-1 SAFE directory convention",
                    }
                else:
                    asset = assets.get(asset_name)
                if not isinstance(asset, dict) or not isinstance(asset.get("href"), str):
                    raise ValueError(
                        f"Earth Search item {earth_item.get('id')} lacks asset {asset_name!r}."
                    )
                href = _s3_https(asset["href"])
                _validate_https_url(href, allowed_hosts=_asset_allowed_hosts(sensor))
                if asset_name in RASTER_ASSETS:
                    acquired_assets.append(
                        _acquire_raster_chip(
                            href,
                            scene_cache / "assets" / f"{asset_name}.tif",
                            target_epsg=target_epsg,
                            center_x=center_x,
                            center_y=center_y,
                            asset_name=asset_name,
                            offline=offline,
                        )
                    )
                else:
                    acquired_assets.append(
                        _acquire_metadata_asset(
                            href,
                            scene_cache / "assets" / f"{asset_name}.xml",
                            offline=offline,
                            allowed_hosts=_asset_allowed_hosts(sensor),
                        )
                    )
                acquired_assets[-1]["stac_asset_canonical_sha256"] = _sha256_bytes(
                    _canonical_json(asset)
                )
            scenes.append(
                {
                    "position": position,
                    "copernicus_item_id": item_id,
                    "earth_search_item_id": earth_item["id"],
                    "earth_search_collection": earth_item.get("collection"),
                    "acquisition_time_utc": pair[f"{position}_acquisition_time_utc"],
                    "mapping": mapping,
                    "assets": acquired_assets,
                }
            )
        result[sensor] = {
            "status": "acquired_requires_pixel_qa",
            "selected_pair": selected_pair,
            "scenes": scenes,
        }
    result["normalized_candidate_sha256"] = _sha256_bytes(_canonical_json(result))
    return result


def _candidate_worker(payload: tuple[dict[str, Any], str, bool]) -> dict[str, Any]:
    """Process-isolated candidate entry point for GDAL/remote COG safety."""

    candidate, cache_root, offline = payload
    return _candidate_acquisition(candidate, Path(cache_root), offline=offline)


def build_acquisition(
    preflight_path: Path, cache_root: Path, *, offline: bool, workers: int = 1
) -> dict[str, Any]:
    preflight_bytes = preflight_path.read_bytes()
    preflight = json.loads(preflight_bytes)
    if preflight.get("schema") != "avycore-public-event-imagery-preflight-v1":
        raise ValueError("Unexpected imagery preflight schema.")
    if preflight.get("predictions_generated") is not False:
        raise ValueError("Acquisition may not follow a prediction-generating preflight.")
    if workers < 1:
        raise ValueError("Acquisition workers must be at least one.")

    def acquire(candidate: dict[str, Any]) -> dict[str, Any]:
        result = _candidate_acquisition(candidate, cache_root, offline=offline)
        print(f"Completed immutable acquisition for {candidate['candidate_id']}.", flush=True)
        return result

    if workers == 1:
        candidates = [acquire(candidate) for candidate in preflight["candidates"]]
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        # Candidate cache roots never overlap. Separate processes avoid shared
        # GDAL remote-dataset state. Results are restored to frozen preflight
        # order regardless of completion order.
        inputs = [
            (candidate, str(cache_root), offline)
            for candidate in preflight["candidates"]
        ]
        ordered: list[dict[str, Any] | None] = [None] * len(inputs)
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_candidate_worker, payload): index
                for index, payload in enumerate(inputs)
            }
            for future in as_completed(futures):
                index = futures[future]
                ordered[index] = future.result()
                print(
                    "Completed immutable acquisition for "
                    f"{preflight['candidates'][index]['candidate_id']}.",
                    flush=True,
                )
        if any(candidate is None for candidate in ordered):
            raise RuntimeError("A process-isolated candidate result is missing.")
        candidates = [candidate for candidate in ordered if candidate is not None]
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "acquisition_id": ACQUISITION_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_preflight_sha256": _sha256_bytes(preflight_bytes),
        "stage": "anonymous_public_imagery_acquisition_before_target_access",
        "predictions_generated": False,
        "model_code_imported": False,
        "holdout_partition_assigned": False,
        "holdout_targets_accessed": False,
        "regobs_attachments_accessed": False,
        "regobs_start_stop_target_coordinates_accessed": False,
        "anonymous_public_access_only": True,
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Model scores are relative indices, not probabilities."
        ),
        "claim_boundary": (
            "Acquired image chips establish only that public pixels were retrieved. They do "
            "not establish avalanche visibility, observation validity, field accuracy, or "
            "validation eligibility. Sentinel-1 chips are correctly GCP-geocoded GRD DN "
            "values with complete SAFE calibration/noise/product metadata, but remain raw "
            "DN until the separately hashed processing stage."
        ),
        "selection_rule_frozen_before_pixel_access": True,
        "cache_reference": ".validation-cache/public-event-imagery-acquisition-v2",
        "cache_policy": (
            "Gitignored immutable request, response, source-metadata, and raster-chip bytes. "
            "Every acquired cache file and raster pixel payload is SHA-256 identified; "
            "differing bytes are never overwritten."
        ),
        "chip_policy": {
            "centre": "public RegObs discovery point only",
            "radius_m": CHIP_RADIUS_M,
            "resolution_m": CHIP_RESOLUTION_M,
            "source_subset_only": True,
            "full_source_rasters_downloaded": False,
            "small_source_metadata_documents_downloaded_in_full": True,
        },
        "counts": {
            "candidates": len(candidates),
            "sentinel_1_pairs_acquired": sum(
                candidate["sentinel_1_grd"]["status"] == "acquired_requires_pixel_qa"
                for candidate in candidates
            ),
            "sentinel_2_pairs_acquired": sum(
                candidate["sentinel_2_l2a"]["status"] == "acquired_requires_pixel_qa"
                for candidate in candidates
            ),
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
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache"
        / "public-event-imagery-acquisition-v2",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-imagery-acquisition-v2.json",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help=(
            "Process-isolated candidate workers; avoids shared GDAL state and preserves "
            "frozen manifest order."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    output = args.output.resolve()
    _assert_outside_protected(cache_root, "Validation cache")
    _assert_outside_protected(output, "Acquisition manifest")
    artifact = build_acquisition(
        args.preflight.resolve(),
        cache_root,
        offline=args.offline,
        workers=args.workers,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote {artifact['counts']['candidates']} candidates to {output}; "
        f"S1={artifact['counts']['sentinel_1_pairs_acquired']}, "
        f"S2={artifact['counts']['sentinel_2_pairs_acquired']}, predictions=0, holdouts=0."
    )


if __name__ == "__main__":
    main()
