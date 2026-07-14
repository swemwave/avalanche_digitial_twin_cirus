from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from shapely.geometry import shape
from shapely.ops import unary_union

from app.core.paths import UnsafePathError, ensure_runtime_dirs, relative_source_path, safe_source_path
from app.core.settings import Settings
from app.services.json_utils import summarize_json

try:
    import rasterio
except Exception:  # pragma: no cover - exercised only when rasterio is unavailable
    rasterio = None  # type: ignore[assignment]

try:
    import laspy
except Exception:  # pragma: no cover - optional dependency
    laspy = None  # type: ignore[assignment]


EXPECTED_DISCOVERY_FILES = [
    "metadata/download_manifest.csv",
    "metadata/download_manifest.json",
    "metadata/grid_and_aoi.json",
    "metadata/mount_hosmer_aoi.geojson",
    "metadata/event_pairs.csv",
    "metadata/event_pairs.json",
    "metadata/config_used.yaml",
    "logs/download_errors.json",
    "download.log",
]

EVENT_FOLDER_RE = re.compile(r"^MH_\d{8}T?\d{6}Z?$")
DATE_COLUMN_RE = re.compile(r"(date|time|timestamp|datetime)", re.IGNORECASE)
RASTER_EXTENSIONS = {".tif", ".tiff"}
VECTOR_EXTENSIONS = {".geojson"}
CSV_EXTENSIONS = {".csv", ".tsv"}
JSON_EXTENSIONS = {".json", ".geojson"}
POINT_CLOUD_EXTENSIONS = {".las", ".laz"}


@dataclass
class ManifestEntry:
    dataset: str | None = None
    source: str | None = None
    status: str | None = None
    local_path: str | None = None
    source_url: str | None = None
    item_id: str | None = None
    acquisition_datetime_utc: str | None = None
    crs: str | None = None
    resolution_m: str | None = None
    bbox_wgs84: str | None = None
    sha256: str | None = None
    notes: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_catalog_id(relative_path: str) -> str:
    digest = hashlib.sha1(relative_path.lower().encode("utf-8")).hexdigest()[:16]
    return f"cat_{digest}"


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if pd.isna(value):
        return None
    return value


def load_manifest(data_root: Path) -> tuple[dict[str, ManifestEntry], dict[str, Any]]:
    manifest_path = data_root / "metadata" / "download_manifest.csv"
    entries: dict[str, ManifestEntry] = {}
    summary: dict[str, Any] = {"exists": manifest_path.exists(), "rows": 0, "status_counts": {}}
    if not manifest_path.exists():
        return entries, summary

    df = pd.read_csv(manifest_path, dtype=str, keep_default_na=False)
    summary["rows"] = int(len(df))
    if "status" in df.columns:
        summary["status_counts"] = {
            str(key): int(value) for key, value in df["status"].fillna("").value_counts().items()
        }
    for _, row in df.iterrows():
        raw = {str(key): _clean_value(value) for key, value in row.to_dict().items()}
        local_path = raw.get("local_path")
        if not local_path:
            continue
        normalized = Path(str(local_path).replace("\\", "/")).as_posix()
        entries[normalized] = ManifestEntry(
            dataset=raw.get("dataset"),
            source=raw.get("source"),
            status=raw.get("status"),
            local_path=normalized,
            source_url=raw.get("source_url"),
            item_id=raw.get("item_id"),
            acquisition_datetime_utc=raw.get("acquisition_datetime_utc"),
            crs=raw.get("crs"),
            resolution_m=raw.get("resolution_m"),
            bbox_wgs84=raw.get("bbox_wgs84"),
            sha256=raw.get("sha256"),
            notes=raw.get("notes"),
            raw=raw,
        )
    return entries, summary


def inspect_raster(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if rasterio is None:
        return {"type": "raster", "metadata_error": "rasterio is not installed"}, ["rasterio unavailable"]
    try:
        with rasterio.open(path) as src:
            meta: dict[str, Any] = {
                "type": "raster",
                "crs": src.crs.to_string() if src.crs else None,
                "bounds": list(src.bounds),
                "width": src.width,
                "height": src.height,
                "dimensions": [src.width, src.height],
                "resolution": [abs(src.transform.a), abs(src.transform.e)],
                "band_count": src.count,
                "dtypes": list(src.dtypes),
                "nodata": src.nodata,
                "transform": list(src.transform)[:6],
            }
            if src.count:
                sample_height = min(src.height, 512)
                sample_width = min(src.width, 512)
                data = src.read(
                    1,
                    out_shape=(sample_height, sample_width),
                    masked=True,
                    resampling=rasterio.enums.Resampling.nearest,
                )
                if np.ma.is_masked(data):
                    compressed = data.compressed()
                else:
                    compressed = np.asarray(data).ravel()
                compressed = compressed[np.isfinite(compressed)]
                if compressed.size:
                    meta["value_sample"] = {
                        "band": 1,
                        "min": float(np.min(compressed)),
                        "max": float(np.max(compressed)),
                        "sample_shape": [int(sample_width), int(sample_height)],
                        "approximate": True,
                    }
                else:
                    meta["value_sample"] = {"band": 1, "valid_values": 0, "approximate": True}
            return meta, warnings
    except Exception as exc:
        return {"type": "raster", "metadata_error": str(exc)}, [f"raster metadata failed: {exc}"]


def inspect_vector_geojson(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        features = data.get("features", []) if isinstance(data, dict) else []
        geometries = []
        geometry_types: set[str] = set()
        for feature in features:
            geom_data = feature.get("geometry") if isinstance(feature, dict) else None
            if not geom_data:
                continue
            geom = shape(geom_data)
            if geom.is_empty:
                continue
            geometries.append(geom)
            geometry_types.add(geom.geom_type)
        bounds = None
        if geometries:
            bounds = list(unary_union(geometries).bounds)
        crs = None
        if isinstance(data, dict) and data.get("crs"):
            crs = data["crs"]
        return {
            "type": "vector",
            "format": "GeoJSON",
            "crs": crs,
            "bounds": bounds,
            "feature_count": len(features),
            "geometry_types": sorted(geometry_types),
            "json_summary": summarize_json(data),
        }, warnings
    except Exception as exc:
        return {"type": "vector", "metadata_error": str(exc)}, [f"vector metadata failed: {exc}"]


def inspect_csv(path: Path) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        separator = "\t" if path.suffix.lower() == ".tsv" else ","
        df = pd.read_csv(path, sep=separator, low_memory=False)
        date_ranges: dict[str, dict[str, str | None]] = {}
        for column in df.columns:
            if not DATE_COLUMN_RE.search(str(column)):
                continue
            parsed = pd.to_datetime(df[column], errors="coerce", utc=True)
            valid = parsed.dropna()
            if not valid.empty:
                date_ranges[str(column)] = {
                    "min": valid.min().isoformat(),
                    "max": valid.max().isoformat(),
                }
        return {
            "type": "table",
            "format": path.suffix.lower().lstrip("."),
            "row_count": int(len(df)),
            "columns": [str(column) for column in df.columns],
            "date_ranges": date_ranges,
        }, warnings
    except Exception as exc:
        return {"type": "table", "metadata_error": str(exc)}, [f"csv metadata failed: {exc}"]


def inspect_json(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"type": "json", "json_summary": summarize_json(data)}, []
    except Exception as exc:
        return {"type": "json", "metadata_error": str(exc)}, [f"json metadata failed: {exc}"]


def inspect_yaml(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return {"type": "yaml", "json_summary": summarize_json(data)}, []
    except Exception as exc:
        return {"type": "yaml", "metadata_error": str(exc)}, [f"yaml metadata failed: {exc}"]


def inspect_text(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return {
            "type": "text",
            "line_count": len(lines),
            "head": lines[:10],
            "tail": lines[-10:],
        }, []
    except Exception as exc:
        return {"type": "text", "metadata_error": str(exc)}, [f"text metadata failed: {exc}"]


def inspect_point_cloud(path: Path) -> tuple[dict[str, Any], list[str]]:
    if laspy is None:
        return {
            "type": "point_cloud",
            "metadata_error": "laspy is not installed; file presence and size were cataloged only",
        }, ["laspy unavailable for LAS/LAZ header inspection"]
    try:
        with laspy.open(path) as reader:
            header = reader.header
            mins = [float(value) for value in header.mins]
            maxs = [float(value) for value in header.maxs]
            area = max((maxs[0] - mins[0]) * (maxs[1] - mins[1]), 0.0)
            point_count = int(header.point_count)
            density = point_count / area if area else None
            crs = None
            try:
                parsed_crs = header.parse_crs()
                crs = parsed_crs.to_string() if parsed_crs else None
            except Exception:
                crs = None
            return {
                "type": "point_cloud",
                "point_count": point_count,
                "point_format": str(header.point_format),
                "las_version": f"{header.version.major}.{header.version.minor}",
                "crs": crs,
                "xyz_bounds": {"min": mins, "max": maxs},
                "return_counts": [int(value) for value in getattr(header, "number_of_points_by_return", [])],
                "approx_point_density_per_m2": density,
            }, []
    except Exception as exc:
        return {"type": "point_cloud", "metadata_error": str(exc)}, [f"point-cloud metadata failed: {exc}"]


def inspect_file(path: Path) -> tuple[dict[str, Any], list[str]]:
    suffix = path.suffix.lower()
    if suffix in RASTER_EXTENSIONS:
        return inspect_raster(path)
    if suffix in VECTOR_EXTENSIONS:
        return inspect_vector_geojson(path)
    if suffix in CSV_EXTENSIONS:
        return inspect_csv(path)
    if suffix == ".json":
        return inspect_json(path)
    if suffix in {".yaml", ".yml"}:
        return inspect_yaml(path)
    if suffix in POINT_CLOUD_EXTENSIONS:
        return inspect_point_cloud(path)
    return inspect_text(path)


def read_download_errors(data_root: Path) -> list[dict[str, Any]]:
    errors_path = data_root / "logs" / "download_errors.json"
    if not errors_path.exists():
        return []
    try:
        data = json.loads(errors_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else [data]
    except Exception as exc:
        return [{"module": "download_errors", "error": f"could not parse download_errors.json: {exc}"}]


def discover_event_ids(data_root: Path) -> list[str]:
    events_dir = data_root / "events"
    if not events_dir.exists():
        return []
    return sorted(path.name for path in events_dir.iterdir() if path.is_dir() and EVENT_FOLDER_RE.match(path.name))


def _manifest_key_for_path(data_root: Path, path: Path) -> str:
    return relative_source_path(data_root, path)


def build_file_record(
    data_root: Path,
    path: Path,
    manifest_entry: ManifestEntry | None,
    verify_checksums: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    relative_path = relative_source_path(data_root, path)
    warnings: list[str] = []
    failed_checks: list[dict[str, Any]] = []
    metadata, metadata_warnings = inspect_file(path)
    warnings.extend(metadata_warnings)

    expected_sha = manifest_entry.sha256 if manifest_entry else None
    actual_sha = None
    checksum_status = "not_in_manifest"
    if expected_sha:
        if verify_checksums:
            try:
                actual_sha = sha256_file(path)
                checksum_status = "match" if actual_sha.lower() == expected_sha.lower() else "mismatch"
                if checksum_status == "mismatch":
                    failed_checks.append(
                        {
                            "relative_path": relative_path,
                            "check": "sha256",
                            "expected": expected_sha,
                            "actual": actual_sha,
                        }
                    )
            except Exception as exc:
                checksum_status = "error"
                failed_checks.append({"relative_path": relative_path, "check": "sha256", "error": str(exc)})
        else:
            checksum_status = "not_verified"

    record: dict[str, Any] = {
        "id": stable_catalog_id(relative_path),
        "relative_path": relative_path,
        "name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "manifest": manifest_entry.raw if manifest_entry else None,
        "manifest_status": manifest_entry.status if manifest_entry else None,
        "dataset": manifest_entry.dataset if manifest_entry else None,
        "source": manifest_entry.source if manifest_entry else None,
        "checksum": {
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
            "status": checksum_status,
        },
        "metadata": metadata,
        "warnings": warnings,
    }
    return record, failed_checks, warnings


def write_catalog_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fieldnames = [
        "id",
        "relative_path",
        "name",
        "extension",
        "size_bytes",
        "manifest_status",
        "dataset",
        "source",
        "checksum_status",
        "type",
        "crs",
        "bounds",
        "dimensions",
        "row_count",
        "feature_count",
        "metadata_error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            metadata = record.get("metadata") or {}
            writer.writerow(
                {
                    "id": record.get("id"),
                    "relative_path": record.get("relative_path"),
                    "name": record.get("name"),
                    "extension": record.get("extension"),
                    "size_bytes": record.get("size_bytes"),
                    "manifest_status": record.get("manifest_status"),
                    "dataset": record.get("dataset"),
                    "source": record.get("source"),
                    "checksum_status": (record.get("checksum") or {}).get("status"),
                    "type": metadata.get("type"),
                    "crs": metadata.get("crs"),
                    "bounds": json.dumps(metadata.get("bounds")) if metadata.get("bounds") is not None else None,
                    "dimensions": json.dumps(metadata.get("dimensions")) if metadata.get("dimensions") is not None else None,
                    "row_count": metadata.get("row_count"),
                    "feature_count": metadata.get("feature_count"),
                    "metadata_error": metadata.get("metadata_error"),
                }
            )


def generate_catalog(settings: Settings, verify_checksums: bool = True) -> dict[str, Any]:
    start = time.perf_counter()
    data_root = settings.data_root
    ensure_runtime_dirs(settings.runtime_root)
    catalog_dir = settings.runtime_root / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []
    missing_files: list[dict[str, Any]] = []

    if not data_root.exists():
        raise FileNotFoundError(f"configured data root does not exist: {data_root}")

    manifest_entries, manifest_summary = load_manifest(data_root)

    for expected in EXPECTED_DISCOVERY_FILES:
        expected_path = data_root / expected
        if not expected_path.exists():
            missing_files.append({"relative_path": expected, "reason": "expected discovery file missing"})

    for relative_path, entry in sorted(manifest_entries.items()):
        try:
            resolved = safe_source_path(data_root, relative_path)
        except UnsafePathError as exc:
            failed_checks.append({"relative_path": relative_path, "check": "path_safety", "error": str(exc)})
            continue
        if not resolved.exists():
            missing_files.append(
                {
                    "relative_path": relative_path,
                    "dataset": entry.dataset,
                    "manifest_status": entry.status,
                    "reason": "manifest path missing",
                }
            )

    file_paths = sorted(path for path in data_root.rglob("*") if path.is_file())
    records: list[dict[str, Any]] = []
    for path in file_paths:
        relative_path = _manifest_key_for_path(data_root, path)
        manifest_entry = manifest_entries.get(relative_path)
        record, record_failed_checks, record_warnings = build_file_record(
            data_root=data_root,
            path=path,
            manifest_entry=manifest_entry,
            verify_checksums=verify_checksums,
        )
        records.append(record)
        failed_checks.extend(record_failed_checks)
        for message in record_warnings:
            warnings.append({"relative_path": relative_path, "warning": message})

    download_errors = read_download_errors(data_root)
    for error in download_errors:
        warnings.append({"relative_path": "logs/download_errors.json", "warning": error})

    type_counts: dict[str, int] = {}
    extension_counts: dict[str, int] = {}
    checksum_counts: dict[str, int] = {}
    for record in records:
        type_name = (record.get("metadata") or {}).get("type", "unknown")
        extension = record.get("extension") or "<none>"
        checksum_status = (record.get("checksum") or {}).get("status", "unknown")
        type_counts[type_name] = type_counts.get(type_name, 0) + 1
        extension_counts[extension] = extension_counts.get(extension, 0) + 1
        checksum_counts[checksum_status] = checksum_counts.get(checksum_status, 0) + 1

    event_ids = discover_event_ids(data_root)
    catalog = {
        "schema_version": 1,
        "application_version": settings.app_version,
        "generated_at_utc": utc_now_iso(),
        "data_root_label": data_root.name,
        "checksum_verification": "enabled" if verify_checksums else "disabled",
        "summary": {
            "file_count": len(records),
            "total_size_bytes": int(sum(record.get("size_bytes", 0) for record in records)),
            "manifest": manifest_summary,
            "type_counts": dict(sorted(type_counts.items())),
            "extension_counts": dict(sorted(extension_counts.items())),
            "checksum_counts": dict(sorted(checksum_counts.items())),
            "event_ids": event_ids,
            "event_count": len(event_ids),
            "missing_file_count": len(missing_files),
            "failed_check_count": len(failed_checks),
            "warning_count": len(warnings),
            "scan_duration_seconds": round(time.perf_counter() - start, 3),
        },
        "files": records,
        "missing_files": missing_files,
        "failed_checks": failed_checks,
        "download_errors": download_errors,
    }

    (catalog_dir / "data_catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    write_catalog_csv(catalog_dir / "data_catalog.csv", records)
    warnings_doc = {
        "generated_at_utc": catalog["generated_at_utc"],
        "missing_files": missing_files,
        "failed_checks": failed_checks,
        "warnings": warnings,
        "download_errors": download_errors,
    }
    (catalog_dir / "catalog_warnings.json").write_text(json.dumps(warnings_doc, indent=2), encoding="utf-8")
    return catalog


def load_catalog(settings: Settings) -> dict[str, Any]:
    catalog_path = settings.runtime_root / "catalog" / "data_catalog.json"
    if not catalog_path.exists():
        raise FileNotFoundError("data catalog has not been generated; run python -m app.cli scan-data")
    return json.loads(catalog_path.read_text(encoding="utf-8"))
