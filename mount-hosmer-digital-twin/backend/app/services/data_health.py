"""Data health: what we have, what we are missing, and what is quietly broken.

This exists because the most dangerous failure mode in this system is not a crash.
It is an empty CSV that parses cleanly, produces a snow depth of zero, and lowers a
hazard score. A file that is present but empty is worse than a file that is
absent, because it looks like data.

So every check here is written to answer one question: **can the model actually
use this, and if not, does the model know that?**
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.paths import relative_source_path
from app.core.settings import Settings

try:  # pragma: no cover
    import rasterio
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]

#: AOI bounds in EPSG:26911, used to test whether a raster covers the mountain.
AOI_BOUNDS = (637650.0, 5491570.0, 649650.0, 5503570.0)

#: A "current conditions" file older than this is stale. The dataset is a fixed
#: historical download, so everything is stale by this rule -- and it should say so
#: rather than pretend to be live.
STALE_AFTER_HOURS = 72.0

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3}


@dataclass
class Issue:
    severity: str          # critical | warning | info
    code: str
    message: str
    path: str | None = None
    remedy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "remedy": self.remedy,
        }


@dataclass
class DatasetHealth:
    key: str
    title: str
    category: str
    present: bool
    usable_by_model: bool
    file_count: int = 0
    total_bytes: int = 0
    provenance: str = "downloaded"
    detail: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.present:
            return "missing"
        if any(issue.severity == "critical" for issue in self.issues):
            return "critical"
        if not self.usable_by_model:
            return "unusable"
        if any(issue.severity == "warning" for issue in self.issues):
            return "degraded"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "category": self.category,
            "status": self.status,
            "present": self.present,
            "usable_by_model": self.usable_by_model,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "provenance": self.provenance,
            "detail": self.detail,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _is_empty_csv(path: Path) -> tuple[bool, int]:
    """A CSV with a header and no rows is empty. So is a zero-byte file.

    This is the check that stops an empty snow-station file being read as a snow
    depth of zero.
    """
    if path.stat().st_size == 0:
        return True, 0
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.reader(handle)
            rows = 0
            for index, row in enumerate(reader):
                if index == 0:
                    continue  # header
                if any(cell.strip() for cell in row):
                    rows += 1
                if rows > 0:
                    break
        return rows == 0, rows
    except OSError:
        return True, 0


def _raster_health(path: Path) -> tuple[dict[str, Any], list[Issue]]:
    issues: list[Issue] = []
    detail: dict[str, Any] = {}
    if rasterio is None:
        return detail, issues
    try:
        with rasterio.open(path) as src:
            detail = {
                "crs": str(src.crs) if src.crs else None,
                "resolution_m": float(src.res[0]) if src.res else None,
                "band_count": src.count,
                "nodata": src.nodata,
                "width": src.width,
                "height": src.height,
                "bounds": list(src.bounds),
            }
            if src.crs is None:
                issues.append(
                    Issue(
                        "critical",
                        "missing_crs",
                        f"{path.name} has no coordinate reference system. It cannot be aligned to "
                        f"the analysis grid and is unusable.",
                        path.name,
                        "Re-download the file or assign the correct CRS with gdal_edit.",
                    )
                )
            if src.count == 0:
                issues.append(
                    Issue("critical", "no_bands", f"{path.name} contains no raster bands.", path.name)
                )
            if src.nodata is None:
                issues.append(
                    Issue(
                        "info",
                        "no_nodata",
                        f"{path.name} declares no NoData value. Gaps may be read as real values.",
                        path.name,
                    )
                )
    except Exception as exc:
        issues.append(
            Issue(
                "critical",
                "corrupt_raster",
                f"{path.name} could not be opened: {exc}",
                path.name,
                "Re-download the file.",
            )
        )
    return detail, issues


def _overlaps_aoi(bounds: list[float] | None) -> bool | None:
    if not bounds or len(bounds) != 4:
        return None
    west, south, east, north = bounds
    a_west, a_south, a_east, a_north = AOI_BOUNDS
    return not (east < a_west or west > a_east or north < a_south or south > a_north)


def _check_terrain(settings: Settings) -> list[DatasetHealth]:
    results: list[DatasetHealth] = []
    root = settings.data_root / "static"

    lidar_dem = root / "lidar_bc" / "downloads" / "LiDAR_DEM_Index_1_20_000"
    tiles = sorted(lidar_dem.glob("*.tif")) if lidar_dem.is_dir() else []
    years = sorted({name.stem.split("_")[-1] for name in tiles if name.stem.split("_")[-1].isdigit()})
    health = DatasetHealth(
        key="lidar_dem",
        title="BC LiDAR bare-earth DEM (1 m)",
        category="terrain",
        present=bool(tiles),
        usable_by_model=bool(tiles),
        file_count=len(tiles),
        total_bytes=sum(path.stat().st_size for path in tiles),
        provenance="downloaded",
        detail={
            "acquisition_years": years,
            "note": (
                "Neither acquisition covers the AOI alone (2022 reaches 62%, 2016 reaches 44%), but "
                "their nodata gaps are complementary. Merged newest-first they cover 99.9%. Do not "
                "'clean up' this dataset by keeping only the newest year."
            ),
        },
    )
    if len(years) < 2:
        health.issues.append(
            Issue(
                "warning",
                "single_lidar_epoch",
                f"Only one LiDAR acquisition year is present ({', '.join(years) or 'none'}). A "
                f"single epoch does not cover the AOI; coverage will fall back to the 30 m DEM.",
            )
        )
    results.append(health)

    dsm = root / "lidar_bc" / "downloads" / "LiDAR_DSM_Index_1_20_000"
    dsm_tiles = sorted(dsm.glob("*.tif")) if dsm.is_dir() else []
    results.append(
        DatasetHealth(
            key="lidar_dsm",
            title="BC LiDAR surface DSM (1 m)",
            category="terrain",
            present=bool(dsm_tiles),
            usable_by_model=bool(dsm_tiles),
            file_count=len(dsm_tiles),
            total_bytes=sum(path.stat().st_size for path in dsm_tiles),
            detail={"used_for": "Canopy height model (DSM minus DEM)."},
        )
    )

    fallback = root / "terrain_fallback"
    dem = fallback / "Copernicus_DEM_GLO30_EPSG26911_30m.tif"
    detail, issues = _raster_health(dem) if dem.exists() else ({}, [])
    results.append(
        DatasetHealth(
            key="copernicus_dem",
            title="Copernicus GLO-30 DEM (30 m)",
            category="terrain",
            present=dem.exists(),
            usable_by_model=dem.exists(),
            file_count=1 if dem.exists() else 0,
            total_bytes=dem.stat().st_size if dem.exists() else 0,
            detail={**detail, "role": "Fallback only. Used where LiDAR has no coverage (0.07% of the AOI)."},
            issues=issues,
        )
    )

    landcover = root / "landcover" / "ESA_WorldCover_2021_EPSG26911_10m.tif"
    detail, issues = _raster_health(landcover) if landcover.exists() else ({}, [])
    inside = _overlaps_aoi(detail.get("bounds"))
    if inside is False:
        issues.append(
            Issue("critical", "outside_aoi", "ESA WorldCover raster does not overlap the AOI.", landcover.name)
        )
    results.append(
        DatasetHealth(
            key="landcover",
            title="ESA WorldCover 2021 (10 m)",
            category="landcover",
            present=landcover.exists(),
            usable_by_model=landcover.exists() and inside is not False,
            file_count=1 if landcover.exists() else 0,
            total_bytes=landcover.stat().st_size if landcover.exists() else 0,
            detail=detail,
            issues=issues,
        )
    )
    return results


def _check_weather(settings: Settings) -> list[DatasetHealth]:
    root = settings.data_root / "dynamic" / "weather_eccc"
    results: list[DatasetHealth] = []

    for key, pattern, title in (
        ("weather_hourly", "climate-hourly_*.csv", "ECCC hourly weather"),
        ("weather_daily", "climate-daily_*.csv", "ECCC daily weather"),
        ("weather_stations", "climate-stations_*.csv", "ECCC station metadata"),
    ):
        files = sorted(root.glob(pattern)) if root.is_dir() else []
        issues: list[Issue] = []
        usable = bool(files)
        rows = 0

        for path in files:
            empty, row_count = _is_empty_csv(path)
            rows += row_count
            if empty:
                usable = False
                issues.append(
                    Issue(
                        "critical",
                        "empty_file",
                        f"{path.name} is empty (header only, or zero bytes). It is treated as MISSING "
                        f"data, never as observations of zero.",
                        relative_source_path(settings.data_root, path),
                        "Re-download the file from the ECCC climate API.",
                    )
                )

        results.append(
            DatasetHealth(
                key=key,
                title=title,
                category="weather",
                present=bool(files),
                usable_by_model=usable,
                file_count=len(files),
                total_bytes=sum(path.stat().st_size for path in files),
                provenance="downloaded",
                detail={"has_rows": rows > 0},
                issues=issues,
            )
        )
    return results


def _check_snow(settings: Settings) -> list[DatasetHealth]:
    """The most important check in this module.

    The current-season snow files exist and are empty. If anything treated them as
    observations, the model would report a snow depth of zero for a winter
    mountain -- and a hazard score to match.
    """
    root = settings.data_root / "dynamic" / "snow_bc"
    results: list[DatasetHealth] = []

    stations = {
        "2C09Q": "Morrissey Ridge snow pillow (1860 m, ~19 km from the AOI)",
        "2C21P": "Fernie snow pillow (988 m, valley bottom)",
    }

    for station_id, title in stations.items():
        folder = root / station_id
        files = sorted(folder.glob("*.csv")) if folder.is_dir() else []
        issues: list[Issue] = []
        usable = False
        detail: dict[str, Any] = {"files": {}}

        for path in files:
            empty, rows = _is_empty_csv(path)
            detail["files"][path.name] = {"empty": empty, "data_rows": rows, "bytes": path.stat().st_size}
            if empty:
                issues.append(
                    Issue(
                        "critical" if "current" in path.name else "warning",
                        "empty_snow_file",
                        f"{path.name} contains no observations. This is MISSING DATA. It is NOT a "
                        f"reading of zero snow, and the model does not treat it as one -- the snow "
                        f"depth and SWE indices fall back to being modelled, and say so.",
                        relative_source_path(settings.data_root, path),
                        "Re-download from the BC river forecast centre, if the season is published.",
                    )
                )
            else:
                usable = True

        if station_id == "2C21P" and not any(path.name.endswith("archive_raw.csv") for path in files):
            issues.append(
                Issue(
                    "warning",
                    "missing_archive",
                    "The Fernie station has no historical archive (the BC endpoint returned 404). "
                    "Only current-season data would be available, and that file is empty.",
                )
            )

        results.append(
            DatasetHealth(
                key=f"snow_{station_id}",
                title=title,
                category="snow",
                present=bool(files),
                usable_by_model=usable,
                file_count=len(files),
                total_bytes=sum(path.stat().st_size for path in files),
                provenance="observed",
                detail=detail,
                issues=issues,
            )
        )
    return results


def _check_events(settings: Settings) -> list[DatasetHealth]:
    root = settings.data_root / "events"
    results: list[DatasetHealth] = []
    if not root.is_dir():
        return results

    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        sentinel = sorted((folder / "sentinel2").glob("*.tif")) if (folder / "sentinel2").is_dir() else []
        landsat = sorted((folder / "landsat").glob("*.tif")) if (folder / "landsat").is_dir() else []
        issues: list[Issue] = []

        has_ndsi = any("NDSI" in path.name for path in sentinel + landsat)
        has_scl = any("SCL" in path.name for path in sentinel)
        if not has_ndsi:
            issues.append(
                Issue("warning", "missing_ndsi", f"{folder.name} has no NDSI band; snow cannot be observed from it.")
            )
        if not has_scl:
            issues.append(
                Issue(
                    "warning",
                    "missing_scene_classification",
                    f"{folder.name} has no Sentinel-2 SCL band, so cloud cannot be masked. Cloud is "
                    f"never read as snow, but unmasked cloud reduces the usable scene area.",
                )
            )

        results.append(
            DatasetHealth(
                key=f"event_{folder.name}",
                title=f"Satellite event {folder.name}",
                category="satellite",
                present=True,
                usable_by_model=has_ndsi,
                file_count=len(sentinel) + len(landsat),
                total_bytes=sum(path.stat().st_size for path in sentinel + landsat),
                detail={
                    "sentinel2_bands": len(sentinel),
                    "landsat_bands": len(landsat),
                    "has_ndsi": has_ndsi,
                    "has_cloud_mask": has_scl,
                },
                issues=issues,
            )
        )
    return results


def _check_infrastructure(settings: Settings) -> list[DatasetHealth]:
    path = settings.data_root / "static" / "openstreetmap" / "mount_hosmer_osm_features.geojson"
    issues: list[Issue] = []
    detail: dict[str, Any] = {}

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            from app.simulation.exposure import categorize

            counts: dict[str, int] = {}
            for feature in data.get("features", []):
                geometry = feature.get("geometry") or {}
                category = categorize(feature.get("properties") or {}, geometry.get("type", ""))
                counts[category] = counts.get(category, 0) + 1
            detail = {"feature_count": len(data.get("features", [])), "by_category": counts}

            buildings = counts.get("buildings", 0)
            if buildings <= 1:
                issues.append(
                    Issue(
                        "critical",
                        "incomplete_buildings",
                        f"Only {buildings} building feature exists in the OpenStreetMap extract for "
                        f"this AOI. OSM building coverage in rural BC is sparse. Every consequence "
                        f"score is therefore a LOWER BOUND: a low count of exposed buildings is not "
                        f"evidence that no buildings are exposed.",
                        remedy=(
                            "Import a municipal or provincial building footprint dataset, or survey "
                            "the exposed area."
                        ),
                    )
                )
        except json.JSONDecodeError as exc:
            issues.append(Issue("critical", "invalid_geojson", f"OSM GeoJSON could not be parsed: {exc}"))

    return [
        DatasetHealth(
            key="infrastructure_osm",
            title="OpenStreetMap infrastructure",
            category="infrastructure",
            present=path.exists(),
            usable_by_model=path.exists() and not any(i.code == "invalid_geojson" for i in issues),
            file_count=1 if path.exists() else 0,
            total_bytes=path.stat().st_size if path.exists() else 0,
            provenance="downloaded",
            detail=detail,
            issues=issues,
        )
    ]


def _check_forecast(settings: Settings) -> list[DatasetHealth]:
    root = settings.data_root / "dynamic" / "avalanche_canada"
    files = sorted(root.glob("*.json")) if root.is_dir() else []
    issues: list[Issue] = []

    newest = max((path.stat().st_mtime for path in files), default=None)
    age_hours = None
    if newest is not None:
        age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(newest, tz=timezone.utc)).total_seconds() / 3600.0
        if age_hours > STALE_AFTER_HOURS:
            issues.append(
                Issue(
                    "warning",
                    "stale_current_product",
                    f"The Avalanche Canada products were downloaded {age_hours / 24:.0f} days ago. "
                    f"They describe a CURRENT forecast that is no longer current.",
                    remedy="Re-download the Avalanche Canada current products.",
                )
            )

    issues.append(
        Issue(
            "info",
            "context_only",
            "Avalanche Canada data is a CURRENT regional forecast. It is never used as a historical "
            "label for a past date, and an off-season 'no forecast' is never read as 'no danger'. It "
            "contributes zero weight to every score.",
        )
    )

    return [
        DatasetHealth(
            key="avalanche_canada",
            title="Avalanche Canada current products",
            category="forecast",
            present=bool(files),
            usable_by_model=bool(files),
            file_count=len(files),
            total_bytes=sum(path.stat().st_size for path in files),
            provenance="downloaded",
            detail={"age_hours": round(age_hours, 1) if age_hours else None, "scored": False},
            issues=issues,
        )
    ]


#: Things the model needs and does not have. These cannot be fixed by writing code.
MISSING_DATASETS = [
    {
        "key": "avalanche_incidents",
        "title": "Historical avalanche occurrence records for Mount Hosmer",
        "impact": "critical",
        "consequence": (
            "Without observed avalanches there is nothing to calibrate against. Every score in this "
            "system is therefore a relative index, never a probability, and confidence is capped."
        ),
        "how_to_obtain": (
            "Avalanche Canada incident database (regional, sparse), BC MoTI highway avalanche "
            "records for Highway 3, or a local operator's occurrence log."
        ),
    },
    {
        "key": "snow_profiles",
        "title": "Snow pit / snow profile observations",
        "impact": "critical",
        "consequence": (
            "The model cannot see buried persistent weak layers -- the mechanism behind most "
            "avalanche fatalities. It models loading, not snowpack structure."
        ),
        "how_to_obtain": "Field observations, or the Avalanche Canada Mountain Information Network.",
    },
    {
        "key": "winter_2025_26_snow",
        "title": "Winter 2025-26 mountain snow depth and SWE",
        "impact": "critical",
        "consequence": (
            "Snow depth and SWE are reported as dimensionless 0-1 indices rather than centimetres and "
            "millimetres, because the data cannot support physical units."
        ),
        "how_to_obtain": "BC Snow Survey current-season data, once published, or an on-site snow stake.",
    },
    {
        "key": "runout_observations",
        "title": "Validated avalanche runout observations",
        "impact": "high",
        "consequence": (
            "Alpha angles and Voellmy friction coefficients are literature defaults, not values "
            "back-analysed from this mountain. Runout distances carry an explicit uncertainty envelope."
        ),
        "how_to_obtain": "Vegetation trimline mapping, historical air photos, or dendrochronology.",
    },
    {
        "key": "building_footprints",
        "title": "Complete building footprints",
        "impact": "high",
        "consequence": (
            "The consequence score is a lower bound. A low exposed-building count is not evidence "
            "that no buildings are exposed."
        ),
        "how_to_obtain": "Regional District of East Kootenay parcel data, or Microsoft Building Footprints.",
    },
    {
        "key": "ridge_top_wind",
        "title": "Ridge-top anemometer",
        "impact": "medium",
        "consequence": (
            "Wind is taken from a valley station 17 km away and assumed uniform across the mountain. "
            "Real ridge-top wind is stronger and terrain-steered."
        ),
        "how_to_obtain": "Install a remote weather station, or use a numerical wind model (WindNinja).",
    },
]


def data_health(settings: Settings) -> dict[str, Any]:
    """The full data-health report."""
    datasets: list[DatasetHealth] = []
    datasets.extend(_check_terrain(settings))
    datasets.extend(_check_weather(settings))
    datasets.extend(_check_snow(settings))
    datasets.extend(_check_events(settings))
    datasets.extend(_check_infrastructure(settings))
    datasets.extend(_check_forecast(settings))

    all_issues = [issue for dataset in datasets for issue in dataset.issues]
    critical = [issue for issue in all_issues if issue.severity == "critical"]
    warnings = [issue for issue in all_issues if issue.severity == "warning"]

    usable = sum(1 for dataset in datasets if dataset.usable_by_model)
    present = sum(1 for dataset in datasets if dataset.present)

    if critical:
        overall = "degraded"
    elif warnings:
        overall = "warning"
    else:
        overall = "ok"

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "overall_status": overall,
        "summary": {
            "datasets_checked": len(datasets),
            "datasets_present": present,
            "datasets_usable_by_model": usable,
            "critical_issues": len(critical),
            "warnings": len(warnings),
        },
        "datasets": [
            dataset.to_dict()
            for dataset in sorted(
                datasets, key=lambda item: (SEVERITY_ORDER.get(item.status, 9), item.key)
            )
        ],
        "issues": sorted(
            (issue.to_dict() for issue in all_issues),
            key=lambda item: SEVERITY_ORDER.get(item["severity"], 9),
        ),
        "missing_datasets": MISSING_DATASETS,
        "empty_file_policy": (
            "A file that exists but contains no observations is reported as MISSING, never as an "
            "observation of zero. This is the single most important rule in this module: an empty "
            "snow file read as 'zero snow' would silently lower the hazard score on a loaded winter "
            "mountain."
        ),
        "calibration_status": {
            "is_calibrated": False,
            "reason": (
                "No historical avalanche occurrence record exists for Mount Hosmer. No output of "
                "this system is a calibrated probability, and confidence is capped accordingly."
            ),
        },
    }
