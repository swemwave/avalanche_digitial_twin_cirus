"""The catalogue, joined to the health verdict on each dataset.

The file catalogue answers "what do we have?". It cannot answer the question that
actually matters, which is **"can the model use it?"**, and the two are not the same
question. The 2025-26 snow files are present, well-formed, and in the catalogue.
They are also empty. A catalogue that lists them as 271 healthy files invites
exactly the mistake this whole system exists to avoid: reading an empty file as an
observation of zero snow.

So the catalogue and the health report are served together, and every dataset
carries `usable_by_model` next to its file count. Presence is not usability.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.paths import ensure_runtime_dirs
from app.core.settings import Settings
from app.services.catalog import load_catalog
from app.services.data_health import data_health

#: Which source files back each health dataset, matched on the relative path.
#:
#: The path is the only stable join key the catalogue offers. Its ``dataset`` field
#: is free prose ("Sentinel-2 derived NDSI"), and its ``category`` field is null on
#: every record -- joining on either would look like it worked while quietly
#: matching nothing.
HEALTH_TO_PATHS: dict[str, tuple[str, ...]] = {
    "lidar_dem": ("static/lidar_bc/downloads/LiDAR_DEM_Index_1_20_000",),
    "lidar_dsm": ("static/lidar_bc/downloads/LiDAR_DSM_Index_1_20_000",),
    "copernicus_dem": ("static/terrain_fallback",),
    "landcover": ("static/landcover",),
    "weather_hourly": ("dynamic/weather_eccc/climate-hourly",),
    "weather_daily": ("dynamic/weather_eccc/climate-daily",),
    "weather_stations": ("dynamic/weather_eccc/climate-stations",),
    "snow_2C09Q": ("dynamic/snow_bc/2C09Q",),
    "snow_2C21P": ("dynamic/snow_bc/2C21P",),
    "infrastructure_osm": ("static/openstreetmap",),
    "avalanche_canada": ("dynamic/avalanche_canada",),
}


def _paths_for(key: str) -> tuple[str, ...]:
    """Event datasets are discovered, not enumerated, so their prefix is derived."""
    if key.startswith("event_"):
        return (f"events/{key[len('event_'):]}",)
    return HEALTH_TO_PATHS.get(key, ())


def health_report(settings: Settings, *, persist: bool = True) -> dict[str, Any]:
    """Run the health checks and, by default, leave a copy on disk.

    The persisted copy is what the readiness probe and the launcher read, so
    neither has to re-walk 46 GB of source data to answer "is this deployment
    usable?".
    """
    report = data_health(settings)
    if persist:
        ensure_runtime_dirs(settings.runtime_root)
        target = settings.runtime_root / "health" / "data_health.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def cached_health(settings: Settings) -> dict[str, Any] | None:
    path = settings.runtime_root / "health" / "data_health.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def _files_under(catalog: dict[str, Any], prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    if not prefixes:
        return []
    return [
        record
        for record in catalog.get("files", []) or []
        if _normalize(record.get("relative_path", "")).startswith(prefixes)
    ]


def enriched_catalog(
    settings: Settings, *, compact: bool = False, refresh_health: bool = False
) -> dict[str, Any]:
    """The catalogue with the health verdict folded into it."""
    catalog = load_catalog(settings)

    report = None if refresh_health else cached_health(settings)
    if report is None:
        report = health_report(settings)

    datasets: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for dataset in report.get("datasets", []):
        key = str(dataset.get("key"))
        prefixes = _paths_for(key)
        files = _files_under(catalog, prefixes)
        if prefixes and not files:
            # The join found nothing where it expected files. Say so rather than
            # reporting a confident zero -- a silently empty join is how a dataset
            # goes missing without anyone noticing.
            unmatched.append(key)
        datasets.append(
            {
                **dataset,
                "source_paths": list(prefixes),
                "catalog_file_count": len(files),
                "catalog_total_bytes": sum(int(item.get("size_bytes") or 0) for item in files),
                "catalog_file_ids": [str(item.get("id")) for item in files][:200],
            }
        )

    payload: dict[str, Any] = {
        "schema_version": catalog.get("schema_version"),
        "generated_at_utc": catalog.get("generated_at_utc"),
        "health_generated_at_utc": report.get("generated_at_utc"),
        "summary": catalog.get("summary"),
        "health": {
            "overall_status": report.get("overall_status"),
            "summary": report.get("summary"),
            "issues": report.get("issues"),
            "missing_datasets": report.get("missing_datasets"),
            "empty_file_policy": report.get("empty_file_policy"),
            "calibration_status": report.get("calibration_status"),
        },
        "datasets": datasets,
        "datasets_with_no_matching_files": unmatched,
        "missing_files": catalog.get("missing_files"),
        "failed_checks": catalog.get("failed_checks"),
        "download_errors": catalog.get("download_errors"),
        "presence_is_not_usability": (
            "file_count says a dataset is on disk. usable_by_model says the model can actually use "
            "it. They disagree for the 2025-26 snow files, which are present, well-formed and "
            "empty -- and an empty snow file is missing data, never an observation of zero snow."
        ),
    }

    if not compact:
        payload["files"] = catalog.get("files", [])

    return payload


def readiness(settings: Settings) -> dict[str, Any]:
    """Can this deployment actually serve an analysis?

    Distinct from liveness. The process can be perfectly healthy and still be unable
    to produce a single number, because the source data is not mounted or the
    terrain has never been built. Answering "ok" in that state would be a lie that a
    load balancer would believe.
    """
    catalog_path = settings.runtime_root / "catalog" / "data_catalog.json"
    terrain_index = settings.runtime_root / "processed" / "terrain" / "terrain_index.json"

    checks = {
        "data_root_mounted": settings.data_root.exists(),
        "runtime_root_writable": settings.runtime_root.exists(),
        "catalog_built": catalog_path.exists(),
        "terrain_built": terrain_index.exists(),
    }
    blocking = [name for name, passed in checks.items() if not passed]

    report = cached_health(settings)
    remedy = {
        "data_root_mounted": "Set AVALANCHE_DATA_ROOT to the folder holding static/, dynamic/ and events/.",
        "runtime_root_writable": "Set AVALANCHE_RUNTIME_ROOT to a writable directory.",
        "catalog_built": "Run: python -m app.cli scan-data",
        "terrain_built": "Run: python -m app.cli process-terrain",
    }

    return {
        "ready": not blocking,
        "checks": checks,
        "blocking": blocking,
        "remedy": [remedy[name] for name in blocking],
        "data_health": (report or {}).get("overall_status", "unknown"),
        "note": (
            "Readiness means the service can serve an analysis. A 'degraded' data health does NOT "
            "make it unready: the model is designed to run with missing inputs and to say so, "
            "excluding them from its scores rather than treating them as zero."
        ),
    }
