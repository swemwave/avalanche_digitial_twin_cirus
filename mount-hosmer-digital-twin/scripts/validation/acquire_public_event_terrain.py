"""Acquire immutable public Høydedata terrain screening chips with lineage.

The chips are evidence-screening assets, not solver-ready event snow surfaces.
They retain the selected source-project metadata, CRS and height reference,
acquisition epoch, requested transformation, raster hashes, and unresolved
surface-mismatch terms.  Anonymous reads are restricted to hoydedata.no.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-terrain-acquisition-v1"
FROZEN_AT_UTC = "2026-08-13T00:00:00Z"
SERVICE_ROOT = "https://hoydedata.no/arcgis/rest/services/DTM/ImageServer"
ALLOWED_HOST = "hoydedata.no"
OUTPUT_CRS = "EPSG:25833"
OUTPUT_RESOLUTION_M = 10.0
AOI_BUFFER_M = 500.0
USER_AGENT = "avycore-public-event-terrain-acquisition/1"


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


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable terrain-cache conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError(f"Concurrent immutable terrain-cache conflict at {path}.")


def _stable_path(path: Path) -> str:
    return path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()


def _request_url(endpoint: str, parameters: dict[str, str]) -> str:
    return endpoint + "?" + urllib.parse.urlencode(sorted(parameters.items()))


def _public_get(url: str) -> tuple[bytes, dict[str, Any]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
        raise ValueError(f"Refusing unexpected terrain host in {url!r}.")
    request = urllib.request.Request(
        url, headers={"Accept": "*/*", "User-Agent": USER_AGENT}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
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
        raise RuntimeError(f"Public Høydedata request failed: {url}: {exc}") from exc
    final = urllib.parse.urlparse(metadata["final_url"])
    if final.scheme != "https" or final.hostname != ALLOWED_HOST:
        raise ValueError(f"Terrain request redirected to an unexpected host: {url!r}.")
    return payload, metadata


def _cached_json_get(
    url: str, cache_stem: Path, *, offline: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    request_path = cache_stem.with_suffix(".request.json")
    response_path = cache_stem.with_suffix(".response.json")
    metadata_path = cache_stem.with_suffix(".response-metadata.json")
    request_record = {
        "url": url,
        "method": "GET",
        "headers": {"Accept": "*/*", "User-Agent": USER_AGENT},
        "access": "anonymous public HTTPS read; no account or token",
    }
    _write_immutable(request_path, _canonical_json(request_record))
    present = [response_path.exists(), metadata_path.exists()]
    if any(present) and not all(present):
        raise ValueError(f"Partial immutable JSON cache at {cache_stem}.")
    if response_path.exists():
        payload = response_path.read_bytes()
        metadata = json.loads(metadata_path.read_bytes())
    else:
        if offline:
            raise FileNotFoundError(f"Offline replay is missing {response_path}.")
        payload, metadata = _public_get(url)
        parsed = json.loads(payload)
        if "error" in parsed:
            raise RuntimeError(f"Høydedata service error: {parsed['error']!r}.")
        payload = _canonical_json(parsed)
        _write_immutable(response_path, payload)
        _write_immutable(metadata_path, _canonical_json(metadata))
    parsed = json.loads(payload)
    if "error" in parsed:
        raise RuntimeError(f"Cached Høydedata service error: {parsed['error']!r}.")
    return parsed, {
        "request_path": _stable_path(request_path),
        "request_sha256": _sha256_file(request_path),
        "response_path": _stable_path(response_path),
        "response_sha256": _sha256_file(response_path),
        "response_metadata_path": _stable_path(metadata_path),
        "response_metadata_sha256": _sha256_file(metadata_path),
        "response_metadata": metadata,
    }


def select_pre_event_project(
    features: list[dict[str, Any]], event_time: dt.datetime
) -> dict[str, Any] | None:
    event_ms = event_time.timestamp() * 1000.0
    candidates = []
    for feature in features:
        attributes = feature.get("attributes") or {}
        flight_ms = attributes.get("SISTEFLYDATO")
        if (
            attributes.get("CATEGORY") != 1
            or attributes.get("LAS_PROJECT_ID") is None
            or not isinstance(flight_ms, (int, float))
            or flight_ms > event_ms
        ):
            continue
        candidates.append(attributes)
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: (
            -float(item["SISTEFLYDATO"]),
            float(item.get("OPPLOSNING") or math.inf),
            int(item["OBJECTID"]),
        ),
    )


def _event_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Event time lacks an offset: {value!r}.")
    return parsed


def _geometry_points(candidate: dict[str, Any]) -> list[tuple[float, float]]:
    source = candidate["source_geometry"]
    points: list[tuple[float, float]] = []
    for field in ("provider_start_extent_ring", "provider_stop_extent_ring"):
        for point in source.get(field) or []:
            if len(point) >= 2:
                points.append((float(point[0]), float(point[1])))
    for field in ("provider_start_point", "provider_stop_point"):
        point = source.get(field)
        if point and len(point) >= 2:
            points.append((float(point[0]), float(point[1])))
    if not points:
        raise ValueError(f"Candidate {candidate['candidate_id']} has no source geometry.")
    return points


def _output_bounds(candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise RuntimeError("Terrain acquisition requires pyproj.") from exc
    transformer = Transformer.from_crs("EPSG:4326", OUTPUT_CRS, always_xy=True)
    projected = [transformer.transform(lon, lat) for lon, lat in _geometry_points(candidate)]
    x = [point[0] for point in projected]
    y = [point[1] for point in projected]
    return (
        math.floor((min(x) - AOI_BUFFER_M) / OUTPUT_RESOLUTION_M) * OUTPUT_RESOLUTION_M,
        math.floor((min(y) - AOI_BUFFER_M) / OUTPUT_RESOLUTION_M) * OUTPUT_RESOLUTION_M,
        math.ceil((max(x) + AOI_BUFFER_M) / OUTPUT_RESOLUTION_M) * OUTPUT_RESOLUTION_M,
        math.ceil((max(y) + AOI_BUFFER_M) / OUTPUT_RESOLUTION_M) * OUTPUT_RESOLUTION_M,
    )


def _query(candidate: dict[str, Any], cache_dir: Path, *, offline: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    start = candidate["source_geometry"].get("provider_start_point")
    if not start:
        start = _geometry_points(candidate)[0]
    parameters = {
        "f": "json",
        "where": "1=1",
        "geometry": f"{float(start[0]):.10f},{float(start[1]):.10f}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": (
            "OBJECTID,NAME,CATEGORY,LAS_PROJECT_ID,LAS_PROJECT_NAME,OPPDRAGSGIVER,"
            "DEKNINGSNUMMER,FKB_LASERSTANDARD,FLYFIRMA,TYPE,TILGANG,PROSJEKTNR,"
            "KOORDINATSYSTEM,HOYDESYSTEM,PUNKTTETTHET,AARSTALL,SISTEFLYDATO,"
            "PUBLISERT,RAPPORT,OPPLOSNING,OBJEKTKATALOG"
        ),
        "returnGeometry": "false",
        "resultRecordCount": "200",
    }
    return _cached_json_get(
        _request_url(f"{SERVICE_ROOT}/query", parameters),
        cache_dir / "source-project-query",
        offline=offline,
    )


def _vertical_reference(value: Any) -> dict[str, Any]:
    if value is None:
        return {"epsg": None, "name": None, "resolved": False}
    try:
        epsg = int(value)
        from pyproj import CRS

        name = CRS.from_epsg(epsg).name
    except (TypeError, ValueError):
        return {"epsg": None, "source_value": value, "name": None, "resolved": False}
    return {"epsg": epsg, "name": name, "resolved": True}


def _raster_metadata(path: Path) -> dict[str, Any]:
    try:
        import numpy as np
        import rasterio
    except ImportError as exc:
        raise RuntimeError("Terrain verification requires rasterio and numpy.") from exc
    with rasterio.open(path) as dataset:
        values = dataset.read(1, masked=True)
        valid = values.compressed()
        return {
            "driver": dataset.driver,
            "crs": dataset.crs.to_string() if dataset.crs else None,
            "transform": list(dataset.transform)[:6],
            "width": dataset.width,
            "height": dataset.height,
            "count": dataset.count,
            "dtype": dataset.dtypes[0],
            "nodata": dataset.nodata,
            "valid_pixels": int(valid.size),
            "missing_pixels": int(values.size - valid.size),
            "minimum_m": float(np.min(valid)) if valid.size else None,
            "maximum_m": float(np.max(valid)) if valid.size else None,
        }


def _export(
    candidate: dict[str, Any], project: dict[str, Any], cache_dir: Path, *, offline: bool
) -> dict[str, Any]:
    bounds = _output_bounds(candidate)
    width = int(round((bounds[2] - bounds[0]) / OUTPUT_RESOLUTION_M))
    height = int(round((bounds[3] - bounds[1]) / OUTPUT_RESOLUTION_M))
    if width <= 0 or height <= 0 or width > 15000 or height > 15000:
        raise ValueError(f"Terrain chip dimensions are invalid: {width}x{height}.")
    mosaic_rule = _canonical_json(
        {
            "ascending": True,
            "lockRasterIds": [int(project["OBJECTID"])],
            "mosaicMethod": "esriMosaicLockRaster",
        }
    ).decode("utf-8")
    parameters = {
        "f": "json",
        "bbox": ",".join(f"{value:.3f}" for value in bounds),
        "bboxSR": "25833",
        "imageSR": "25833",
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "F32",
        "interpolation": "RSP_BilinearInterpolation",
        "mosaicRule": mosaic_rule,
        "noData": "-9999",
    }
    request_record = {
        "parameters": parameters,
        "selected_project_objectid": int(project["OBJECTID"]),
        "selection_role": "latest source project with a flight date not after the event",
        "access": "anonymous public HTTPS read; no account or token",
    }
    request_path = cache_dir / "export.request.json"
    response_path = cache_dir / "export.response.json"
    response_metadata_path = cache_dir / "export.response-metadata.json"
    raster_path = cache_dir / "terrain-screening-10m.tif"
    raster_metadata_path = cache_dir / "terrain-screening-10m.metadata.json"
    _write_immutable(request_path, _canonical_json(request_record))
    present = [
        response_path.exists(),
        response_metadata_path.exists(),
        raster_path.exists(),
        raster_metadata_path.exists(),
    ]
    if any(present) and not all(present):
        raise ValueError(f"Partial immutable terrain export cache at {cache_dir}.")
    if not raster_path.exists():
        if offline:
            raise FileNotFoundError(f"Offline replay is missing {raster_path}.")
        response_payload, response_http = _public_get(
            _request_url(f"{SERVICE_ROOT}/exportImage", parameters)
        )
        response = json.loads(response_payload)
        if "error" in response or not isinstance(response.get("href"), str):
            raise RuntimeError(f"Høydedata export error: {response!r}.")
        raster_payload, raster_http = _public_get(response["href"])
        _write_immutable(response_path, _canonical_json(response))
        _write_immutable(
            response_metadata_path,
            _canonical_json({"export_response": response_http, "raster_response": raster_http}),
        )
        _write_immutable(raster_path, raster_payload)
        verified = _raster_metadata(raster_path)
        if verified["crs"] != OUTPUT_CRS:
            raise ValueError(f"Unexpected output terrain CRS: {verified['crs']!r}.")
        if verified["width"] != width or verified["height"] != height:
            raise ValueError("Terrain export dimensions differ from the frozen request.")
        _write_immutable(raster_metadata_path, _canonical_json(verified))
    verified = _raster_metadata(raster_path)
    if _canonical_json(verified) != raster_metadata_path.read_bytes():
        raise ValueError(f"Terrain raster metadata changed at {raster_path}.")
    return {
        "requested_bounds": list(bounds),
        "requested_crs": OUTPUT_CRS,
        "requested_resolution_m": OUTPUT_RESOLUTION_M,
        "request_path": _stable_path(request_path),
        "request_sha256": _sha256_file(request_path),
        "response_path": _stable_path(response_path),
        "response_sha256": _sha256_file(response_path),
        "response_metadata_path": _stable_path(response_metadata_path),
        "response_metadata_sha256": _sha256_file(response_metadata_path),
        "raster_path": _stable_path(raster_path),
        "raster_bytes": raster_path.stat().st_size,
        "raster_sha256": _sha256_file(raster_path),
        "raster_metadata_path": _stable_path(raster_metadata_path),
        "raster_metadata_sha256": _sha256_file(raster_metadata_path),
        "raster": verified,
    }


def _candidate_record(candidate: dict[str, Any], cache_root: Path, *, offline: bool) -> dict[str, Any]:
    candidate_id = candidate["candidate_id"]
    cache_dir = cache_root / candidate_id
    query, query_lineage = _query(candidate, cache_dir, offline=offline)
    event_time = _event_time(candidate["event"]["provider_time"])
    project = select_pre_event_project(query.get("features") or [], event_time)
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "event_time": candidate["event"]["provider_time"],
        "source_geometry_sha256": _sha256_bytes(_canonical_json(candidate["source_geometry"])),
        "source_project_query": query_lineage,
        "source_project_count_returned": len(query.get("features") or []),
        "selected_project": project,
        "terrain_acquired": False,
        "validation_contract_v3_terrain_eligible": False,
    }
    if project is None:
        base.update(
            {
                "status": "excluded_no_dated_pre_event_public_terrain_project",
                "blockers": ["no_dated_pre_event_public_terrain_project"],
                "event_surface_mismatch": None,
                "terrain": None,
            }
        )
    else:
        flight_time = dt.datetime.fromtimestamp(
            float(project["SISTEFLYDATO"]) / 1000.0, tz=dt.timezone.utc
        )
        terrain = _export(candidate, project, cache_dir, offline=offline)
        source_crs = f"EPSG:{project['KOORDINATSYSTEM']}" if project.get("KOORDINATSYSTEM") else None
        vertical = _vertical_reference(project.get("HOYDESYSTEM"))
        base.update(
            {
                "status": "acquired_screening_terrain_not_event_surface_eligible",
                "terrain_acquired": True,
                "source_project_lineage": {
                    "objectid": project["OBJECTID"],
                    "las_project_id": project["LAS_PROJECT_ID"],
                    "name": project.get("LAS_PROJECT_NAME") or project.get("NAME"),
                    "flight_time_utc": flight_time.isoformat().replace("+00:00", "Z"),
                    "year": project.get("AARSTALL"),
                    "source_horizontal_crs": source_crs,
                    "source_horizontal_units": "metre" if source_crs else None,
                    "vertical_reference": vertical,
                    "source_resolution_m": project.get("OPPLOSNING"),
                    "point_density_m2": project.get("PUNKTTETTHET"),
                    "project_report": project.get("RAPPORT"),
                    "access": "anonymous public Høydedata ImageServer read",
                    "provider": "Kartverket Høydedata DTM ImageServer",
                    "provider_url": SERVICE_ROOT,
                },
                "transformations": [
                    {
                        "operation": "candidate EPSG:4326 longitude/latitude to output grid",
                        "library": "PROJ via pyproj",
                        "target_crs": OUTPUT_CRS,
                        "coordinate_order": "always_xy",
                    },
                    {
                        "operation": "ImageServer source project mosaic to requested output grid",
                        "source_crs": source_crs,
                        "target_crs": OUTPUT_CRS,
                        "horizontal_resampling": "bilinear",
                        "vertical_transformation": "none; elevations retain the selected project's height reference",
                    },
                ],
                "event_surface_mismatch": {
                    "signed_event_minus_flight_days": round(
                        (event_time.astimezone(dt.timezone.utc) - flight_time).total_seconds() / 86400.0,
                        6,
                    ),
                    "surface_type": "bare-earth DTM, not avalanche-day snow surface",
                    "snow_depth_uncertainty_m": None,
                    "vegetation_or_classification_residual_uncertainty_m": None,
                    "vertical_accuracy_uncertainty_m": None,
                    "horizontal_accuracy_uncertainty_m": None,
                    "quantitative_mismatch_treatment_available": False,
                    "missing_uncertainties_remain_masked": True,
                },
                "terrain": terrain,
                "blockers": [
                    "screening_10m_chip_is_not_native_solver_terrain",
                    "avalanche_day_snow_surface_mismatch_unquantified",
                    "vertical_accuracy_uncertainty_unavailable",
                    "horizontal_accuracy_uncertainty_unavailable",
                ],
            }
        )
    base["normalized_candidate_sha256"] = _sha256_bytes(_canonical_json(base))
    return base


def acquire_terrain(evidence_path: Path, cache_root: Path, *, offline: bool) -> dict[str, Any]:
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    if evidence.get("schema") != "avycore-public-regobs-blinded-evidence-v1":
        raise ValueError("Unexpected RegObs evidence schema.")
    service_parameters = {"f": "pjson"}
    service, service_lineage = _cached_json_get(
        _request_url(SERVICE_ROOT, service_parameters),
        cache_root / "service-metadata",
        offline=offline,
    )
    service_crs = service.get("spatialReference", {}).get("latestWkid")
    if service_crs != 25833:
        raise ValueError(f"Unexpected Høydedata service CRS: {service_crs!r}.")
    candidates = [
        _candidate_record(candidate, cache_root, offline=offline)
        for candidate in evidence["candidates"]
    ]
    acquired = sum(candidate["terrain_acquired"] for candidate in candidates)
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_regobs_evidence_manifest_sha256": _sha256_bytes(evidence_bytes),
        "public_service": {
            "provider": "Kartverket Høydedata",
            "url": SERVICE_ROOT,
            "service_metadata_lineage": service_lineage,
            "service_crs": "EPSG:25833",
            "service_pixel_type": service.get("pixelType"),
            "service_data_type": service.get("serviceDataType"),
        },
        "selection_rule": (
            "At the provider start point, choose the latest category-1 Høydedata project with "
            "a non-null LAS project identity and flight date not after the event; break ties by "
            "finer declared resolution then OBJECTID. No evaluated output is accessed."
        ),
        "asset_role": (
            "Ten-metre buffered evidence-screening chip only. Native, 2x, and 4x solver grids "
            "would be frozen only after an event passed the observation funnel."
        ),
        "model_code_imported": False,
        "predictions_generated": False,
        "runtime_modified": False,
        "counts": {
            "candidates": len(candidates),
            "terrain_chips_acquired": acquired,
            "no_dated_pre_event_project": len(candidates) - acquired,
            "event_surface_mismatch_quantified": 0,
            "validation_contract_v3_terrain_eligible": 0,
        },
        "candidates": candidates,
        "claim_boundary": (
            "Archived public terrain lineage does not establish an avalanche-day surface. "
            "Unquantified snow, epoch, classification, and accuracy terms keep all events ineligible."
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
    parser.add_argument(
        "--evidence",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "public-regobs-blinded-evidence-v1.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT / ".validation-cache" / "public-event-terrain-acquisition-v1",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "public-event-terrain-acquisition-v1.json",
    )
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = acquire_terrain(args.evidence.resolve(), args.cache_root.resolve(), offline=args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote terrain acquisition: acquired={artifact['counts']['terrain_chips_acquired']}/"
        f"{artifact['counts']['candidates']}, eligible=0."
    )


if __name__ == "__main__":
    main()
