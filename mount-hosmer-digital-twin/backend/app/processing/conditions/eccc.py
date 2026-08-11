"""Offline ECCC historical-hourly normalization for M2.

This module reads only an immutable source-cache snapshot. Importing source
files into that cache is a separate, explicit offline operation. Nothing here
is imported by the serving application.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from avycore.conditions import CANONICAL_UNITS, CONDITION_PACK_SCHEMA_VERSION, ConditionPackDraft
from avycore.conditions.contracts import MountainGridIdentity
from avycore.conditions.units import convert_value

from .protocol import ConditionRequest


UTC = timezone.utc
PROVIDER_ID = "eccc-historical-hourly"
SNAPSHOT_SCHEMA = "eccc-historical-source-snapshot-v1"
QUALITY_SCHEMA = "eccc-historical-forcing-quality-v2"
STATIONS_FILENAME = "climate-stations.csv"
HOURLY_FILENAME = "climate-hourly.csv"

ECCC_HOURLY_URI = (
    "https://api.weather.gc.ca/collections/climate-hourly/items"
)
ECCC_LICENCE_URI = (
    "https://eccc-msc.github.io/open-data/licence/readme_en/"
)
ECCC_LICENCE = "ECCC Data Servers End-use Licence v2.1 (September 2022)"
ECCC_TECHNICAL_URI = (
    "https://www.canada.ca/en/environment-climate-change/services/climate-change/"
    "canadian-centre-climate-services/display-download/technical-documentation-hourly-data.html"
)
PHASE_CITATION = "https://doi.org/10.1175/JCLI-D-11-00084.1"
LAPSE_CITATION = "https://www.gfdl.noaa.gov/blog_held/19-radiative-convective-equilibrium/"
ACQUISITION_TIME_EVIDENCE = (
    "Original file mtimes retained from the existing local downloader snapshot; "
    "the downloader manifest did not record acquisition timestamps."
)
PUBLICATION_TIME_EVIDENCE = (
    "Provider publication timestamps are absent; acquisition end is used only as "
    "a conservative latest-publication bound in the ConditionPack."
)

REQUIRED_STATION_FIELDS = {
    "STATION_NAME",
    "CLIMATE_IDENTIFIER",
    "ELEVATION",
    "ENG_STN_OPERATOR_NAME",
    "HLY_FIRST_DATE",
    "HLY_LAST_DATE",
    "longitude",
    "latitude",
}
REQUIRED_HOURLY_FIELDS = {
    "CLIMATE_IDENTIFIER",
    "ID",
    "UTC_DATE",
    "TEMP",
    "TEMP_FLAG",
    "PRECIP_AMOUNT",
    "PRECIP_AMOUNT_FLAG",
    "RELATIVE_HUMIDITY",
    "RELATIVE_HUMIDITY_FLAG",
    "STATION_PRESSURE",
    "STATION_PRESSURE_FLAG",
    "WIND_DIRECTION",
    "WIND_DIRECTION_FLAG",
    "WIND_SPEED",
    "WIND_SPEED_FLAG",
    "FLAG",
}

DIRECT_FIELDS: dict[str, tuple[str, str, str]] = {
    "air_temperature": ("TEMP", "TEMP_FLAG", "degC"),
    "relative_humidity": ("RELATIVE_HUMIDITY", "RELATIVE_HUMIDITY_FLAG", "%"),
    "wind_speed": ("WIND_SPEED", "WIND_SPEED_FLAG", "km h-1"),
    "wind_direction": ("WIND_DIRECTION", "WIND_DIRECTION_FLAG", "10 degree_true"),
    "precipitation_amount": ("PRECIP_AMOUNT", "PRECIP_AMOUNT_FLAG", "mm h-1"),
    "surface_pressure": ("STATION_PRESSURE", "STATION_PRESSURE_FLAG", "kPa"),
}
COMPARISON_UNITS = {
    "air_temperature": "degC",
    "relative_humidity": "%",
    "wind_speed": "km h-1",
    "wind_direction": "degree_true",
    "precipitation_amount": "mm h-1",
    "surface_pressure": "kPa",
}


class ECCCProviderError(ValueError):
    """Raised when an ECCC snapshot is malformed or ambiguous."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ECCCProviderError(f"Malformed {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        # GeoMet's UTC_DATE field is explicitly UTC even when serialized without Z.
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ECCCProviderError(f"{field} must be UTC, got {value!r}")
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ECCCProviderError(f"{field} must lie on an exact UTC hour: {value!r}")
    return parsed


def _parse_manifest_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ECCCProviderError(f"Malformed {field}: expected an ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ECCCProviderError(f"Malformed {field}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ECCCProviderError(f"{field} must be timezone-aware UTC, got {value!r}")
    return parsed.astimezone(UTC)


def _finite_float(value: str, field: str) -> float | None:
    if value.strip() == "":
        return None
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ECCCProviderError(f"Malformed numeric {field}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ECCCProviderError(f"Non-finite numeric {field}: {value!r}")
    return parsed


def _require_headers(path: Path, actual: Iterable[str] | None, required: set[str]) -> None:
    fields = set(actual or ())
    missing = sorted(required - fields)
    if missing:
        raise ECCCProviderError(f"{path.name} is missing required columns: {missing}")


@dataclass(frozen=True)
class ECCCStation:
    climate_id: str
    name: str
    longitude_deg: float
    latitude_deg: float
    elevation_m: float
    operator: str
    hourly_first: str
    hourly_last: str


@dataclass(frozen=True)
class ECCCSnapshot:
    root: Path
    manifest: dict[str, Any]
    stations_path: Path
    hourly_path: Path
    stations_sha256: str
    hourly_sha256: str

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])


def import_eccc_snapshot(
    stations_path: str | Path,
    hourly_path: str | Path,
    runtime_root: str | Path,
) -> Path:
    """Copy two provider files into an immutable, content-addressed source cache.

    Original mtimes are recorded as the only locally available acquisition-time
    evidence. They are explicitly not represented as provider publication times.
    """

    sources = {
        STATIONS_FILENAME: Path(stations_path).resolve(),
        HOURLY_FILENAME: Path(hourly_path).resolve(),
    }
    records: dict[str, dict[str, Any]] = {}
    for target_name, source in sources.items():
        if not source.is_file():
            raise ECCCProviderError(f"Source file does not exist: {source}")
        content = source.read_bytes()
        records[target_name] = {
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
            "source_name": source.name,
            "source_mtime_utc": datetime.fromtimestamp(source.stat().st_mtime, UTC).isoformat(),
        }
    identity_payload = {"schema": SNAPSHOT_SCHEMA, "files": records}
    snapshot_id = f"snapshot-{_sha256_bytes(_canonical_json_bytes(identity_payload))}"
    manifest = {
        **identity_payload,
        "snapshot_id": snapshot_id,
        "provider_id": PROVIDER_ID,
        "source_uri": ECCC_HOURLY_URI,
        "licence": ECCC_LICENCE,
        "licence_uri": ECCC_LICENCE_URI,
        "acquisition_time_evidence": ACQUISITION_TIME_EVIDENCE,
        "publication_time_evidence": PUBLICATION_TIME_EVIDENCE,
    }
    cache_root = Path(runtime_root).resolve() / "sources" / "conditions" / "eccc"
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / snapshot_id
    if target.exists():
        load_eccc_snapshot(target)
        return target
    staging = cache_root / f".source-import-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for target_name, source in sources.items():
            shutil.copy2(source, staging / target_name)
        (staging / "source-manifest.json").write_bytes(_canonical_json_bytes(manifest))
        loaded = load_eccc_snapshot(staging)
        if loaded.snapshot_id != snapshot_id:
            raise ECCCProviderError("Staged source snapshot identity changed during import.")
        staging.rename(target)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_eccc_snapshot(path: str | Path) -> ECCCSnapshot:
    root = Path(path).resolve()
    manifest_path = root / "source-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ECCCProviderError(f"Invalid ECCC snapshot manifest at {manifest_path}: {exc}") from exc
    if manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise ECCCProviderError("Unsupported ECCC source snapshot schema.")
    expected_keys = {
        "schema",
        "snapshot_id",
        "provider_id",
        "source_uri",
        "licence",
        "licence_uri",
        "acquisition_time_evidence",
        "publication_time_evidence",
        "files",
    }
    if set(manifest) != expected_keys:
        raise ECCCProviderError("ECCC snapshot manifest has missing or unexpected fields.")
    fixed_lineage = {
        "provider_id": PROVIDER_ID,
        "source_uri": ECCC_HOURLY_URI,
        "licence": ECCC_LICENCE,
        "licence_uri": ECCC_LICENCE_URI,
        "acquisition_time_evidence": ACQUISITION_TIME_EVIDENCE,
        "publication_time_evidence": PUBLICATION_TIME_EVIDENCE,
    }
    for field, expected in fixed_lineage.items():
        if manifest.get(field) != expected:
            raise ECCCProviderError(f"ECCC snapshot manifest has invalid {field} lineage.")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {STATIONS_FILENAME, HOURLY_FILENAME}:
        raise ECCCProviderError("ECCC snapshot must contain exactly stations and hourly files.")
    for name, record in files.items():
        if not isinstance(record, dict) or set(record) != {
            "bytes",
            "sha256",
            "source_name",
            "source_mtime_utc",
        }:
            raise ECCCProviderError(f"ECCC snapshot has malformed file lineage for {name}.")
        source_name = record["source_name"]
        if (
            not isinstance(source_name, str)
            or not source_name
            or Path(source_name).name != source_name
        ):
            raise ECCCProviderError(f"ECCC snapshot has invalid source_name for {name}.")
        _parse_manifest_utc(record["source_mtime_utc"], f"{name}.source_mtime_utc")
        content = (root / name).read_bytes()
        if record != {
            "bytes": len(content),
            "sha256": _sha256_bytes(content),
            "source_name": source_name,
            "source_mtime_utc": record["source_mtime_utc"],
        }:
            raise ECCCProviderError(f"ECCC snapshot checksum mismatch for {name}.")
    expected_identity = {
        "schema": SNAPSHOT_SCHEMA,
        "files": files,
    }
    expected_id = f"snapshot-{_sha256_bytes(_canonical_json_bytes(expected_identity))}"
    if manifest.get("snapshot_id") != expected_id:
        raise ECCCProviderError("ECCC source snapshot identity does not match immutable files.")
    return ECCCSnapshot(
        root=root,
        manifest=manifest,
        stations_path=root / STATIONS_FILENAME,
        hourly_path=root / HOURLY_FILENAME,
        stations_sha256=files[STATIONS_FILENAME]["sha256"],
        hourly_sha256=files[HOURLY_FILENAME]["sha256"],
    )


def _read_stations(snapshot: ECCCSnapshot) -> dict[str, ECCCStation]:
    result: dict[str, ECCCStation] = {}
    with snapshot.stations_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        _require_headers(snapshot.stations_path, reader.fieldnames, REQUIRED_STATION_FIELDS)
        for row in reader:
            climate_id = row["CLIMATE_IDENTIFIER"].strip()
            if not climate_id:
                continue
            candidate = ECCCStation(
                climate_id=climate_id,
                name=row["STATION_NAME"].strip(),
                longitude_deg=float(row["longitude"]),
                latitude_deg=float(row["latitude"]),
                elevation_m=float(row["ELEVATION"]),
                operator=row["ENG_STN_OPERATOR_NAME"].strip() or "not provided",
                hourly_first=row["HLY_FIRST_DATE"].strip(),
                hourly_last=row["HLY_LAST_DATE"].strip(),
            )
            previous = result.get(climate_id)
            if previous is not None and previous != candidate:
                raise ECCCProviderError(f"Conflicting station metadata for {climate_id}.")
            result[climate_id] = candidate
    return result


def _read_hourly(
    snapshot: ECCCSnapshot,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, dict[datetime, dict[str, str]]], dict[str, int]]:
    by_station: dict[str, dict[datetime, dict[str, str]]] = defaultdict(dict)
    exact_duplicates = 0
    rows_in_window = 0
    with snapshot.hourly_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        _require_headers(snapshot.hourly_path, reader.fieldnames, REQUIRED_HOURLY_FIELDS)
        for row in reader:
            timestamp = _parse_utc(row["UTC_DATE"], "UTC_DATE")
            if timestamp < start or timestamp > end:
                continue
            rows_in_window += 1
            climate_id = row["CLIMATE_IDENTIFIER"].strip()
            if not climate_id or not row["ID"].strip():
                raise ECCCProviderError("Hourly rows require CLIMATE_IDENTIFIER and ID.")
            previous = by_station[climate_id].get(timestamp)
            if previous is not None:
                if previous == row:
                    exact_duplicates += 1
                    continue
                raise ECCCProviderError(
                    f"Conflicting duplicate/revised records for {climate_id} at {timestamp.isoformat()}."
                )
            by_station[climate_id][timestamp] = row
    if rows_in_window == 0:
        raise ECCCProviderError("ECCC snapshot contains no rows in the requested UTC window.")
    return dict(by_station), {"exact_duplicates": exact_duplicates, "rows_in_window": rows_in_window}


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _raw_value(row: dict[str, str] | None, variable: str) -> tuple[float | None, str]:
    if row is None:
        return None, ""
    field, flag_field, _unit = DIRECT_FIELDS[variable]
    flag = (row.get(flag_field) or "").strip()
    if flag == "M":
        return None, flag
    value = _finite_float(row.get(field, ""), field)
    if variable == "wind_direction" and value == 0:
        # ECCC explicitly defines zero as calm, so it is not a north direction.
        return None, flag or "CALM"
    return value, flag


def _longest_gap(present: list[bool]) -> int:
    longest = current = 0
    for item in present:
        if item:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _candidate_report(
    station: ECCCStation,
    records: dict[datetime, dict[str, str]],
    timeline: tuple[datetime, ...],
    target_lon: float,
    target_lat: float,
    target_elevation_m: float,
) -> dict[str, Any]:
    availability: dict[str, int] = {}
    longest_gap_hours: dict[str, int] = {}
    flags: dict[str, int] = defaultdict(int)
    for variable in DIRECT_FIELDS:
        count = 0
        present: list[bool] = []
        for timestamp in timeline:
            value, flag = _raw_value(records.get(timestamp), variable)
            present.append(value is not None)
            if value is not None:
                count += 1
            if flag:
                flags[flag] += 1
        availability[variable] = count
        longest_gap_hours[variable] = _longest_gap(present)
    total_cells = len(timeline) * len(DIRECT_FIELDS)
    available_cells = sum(availability.values())
    missing_hours = sum(timestamp not in records for timestamp in timeline)
    direct_variable_count = sum(count > 0 for count in availability.values())
    operator_priority = 0 if "Environment and Climate Change Canada" in station.operator else 1
    rank = [
        -direct_variable_count,
        operator_priority,
        round(1.0 - available_cells / total_cells, 12),
        abs(station.elevation_m - target_elevation_m),
        _haversine_km(target_lon, target_lat, station.longitude_deg, station.latitude_deg),
        station.climate_id,
    ]
    return {
        "station_id": station.climate_id,
        "name": station.name,
        "operator": station.operator,
        "longitude_deg": station.longitude_deg,
        "latitude_deg": station.latitude_deg,
        "elevation_m": station.elevation_m,
        "horizontal_distance_km": rank[4],
        "elevation_difference_m": station.elevation_m - target_elevation_m,
        "temporal_resolution_seconds": 3600,
        "expected_hours": len(timeline),
        "recorded_hours": len(records),
        "missing_hours": missing_hours,
        "missing_hour_fraction": missing_hours / len(timeline),
        "variable_available_hours": availability,
        "variable_missing_fraction": {
            name: 1.0 - count / len(timeline) for name, count in availability.items()
        },
        "provider_qc_flags": dict(sorted(flags.items())),
        "qc_excluded_values": flags.get("M", 0),
        "longest_gap_hours": longest_gap_hours,
        "hourly_first_date": station.hourly_first,
        "hourly_last_date": station.hourly_last,
        "metadata_change_evidence": (
            "This snapshot supplies current station metadata only; movement/equipment history "
            "must be checked against provider station-history records before calibration use."
        ),
        "selection_rank": rank,
        "selection_basis": (
            "Lexicographic: available direct-variable count, ECCC operation, total missing "
            "fraction, elevation difference, distance, station ID."
        ),
    }


def _comparison_metrics(
    selected_records: dict[datetime, dict[str, str]],
    withheld_records: dict[datetime, dict[str, str]],
    timeline: tuple[datetime, ...],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for variable in DIRECT_FIELDS:
        diffs: list[float] = []
        for timestamp in timeline:
            selected, _ = _raw_value(selected_records.get(timestamp), variable)
            withheld, _ = _raw_value(withheld_records.get(timestamp), variable)
            if selected is None or withheld is None:
                continue
            if variable == "wind_direction":
                selected = (selected * 10.0) % 360.0
                withheld = (withheld * 10.0) % 360.0
                diff = (selected - withheld + 180.0) % 360.0 - 180.0
            else:
                diff = selected - withheld
            diffs.append(diff)
        result[variable] = {
            "overlap_hours": len(diffs),
            "comparison_unit": COMPARISON_UNITS[variable],
            "selected_minus_withheld_bias_source_units": fmean(diffs) if diffs else None,
            "mae_source_units": fmean(abs(value) for value in diffs) if diffs else None,
            "rmse_source_units": (
                math.sqrt(fmean(value * value for value in diffs)) if diffs else None
            ),
            "comparison_role": (
                "Station-to-station disagreement; the withheld station is not Mount Hosmer truth."
            ),
        }
    return result


def audit_eccc_snapshot(
    snapshot: ECCCSnapshot,
    request: ConditionRequest,
    *,
    target_longitude_deg: float,
    target_latitude_deg: float,
    target_elevation_m: float,
    selected_station_id: str | None = None,
) -> dict[str, Any]:
    stations = _read_stations(snapshot)
    records, duplicate_report = _read_hourly(
        snapshot, request.valid_start_utc, request.valid_end_utc
    )
    timeline = _timeline(request.valid_start_utc, request.valid_end_utc)
    candidates = [
        _candidate_report(
            stations[station_id],
            station_records,
            timeline,
            target_longitude_deg,
            target_latitude_deg,
            target_elevation_m,
        )
        for station_id, station_records in records.items()
        if station_id in stations
    ]
    if not candidates:
        raise ECCCProviderError("No hourly station has matching station metadata.")
    candidates.sort(key=lambda item: item["selection_rank"])
    recommended = candidates[0]
    candidates_by_id = {item["station_id"]: item for item in candidates}
    if selected_station_id is not None and selected_station_id not in candidates_by_id:
        raise ECCCProviderError(f"Selected station {selected_station_id!r} is unavailable.")
    selected = candidates_by_id.get(selected_station_id, recommended)
    for candidate in candidates:
        candidate["nearby_station_ids_within_1km"] = sorted(
            other["station_id"]
            for other in candidates
            if other["station_id"] != candidate["station_id"]
            and _haversine_km(
                candidate["longitude_deg"],
                candidate["latitude_deg"],
                other["longitude_deg"],
                other["latitude_deg"],
            )
            <= 1.0
        )
    alternatives = [item for item in candidates if item["station_id"] != selected["station_id"]]
    withheld = (
        min(
            alternatives,
            key=lambda item: (
                sum(item["variable_missing_fraction"].values()),
                item["horizontal_distance_km"],
                abs(item["elevation_difference_m"]),
                item["station_id"],
            ),
        )
        if alternatives
        else None
    )
    comparisons = (
        _comparison_metrics(
            records[selected["station_id"]], records[withheld["station_id"]], timeline
        )
        if withheld is not None
        else {}
    )
    return {
        "schema": QUALITY_SCHEMA,
        "snapshot_id": snapshot.snapshot_id,
        "valid_start_utc": request.valid_start_utc.isoformat(),
        "valid_end_utc": request.valid_end_utc.isoformat(),
        "target": {
            "longitude_deg": target_longitude_deg,
            "latitude_deg": target_latitude_deg,
            "elevation_m": target_elevation_m,
            "elevation_basis": "Caller-supplied forcing reference; not inferred by this provider.",
        },
        "selection_algorithm_version": "eccc-station-selection-v1",
        "recommended_station_id": recommended["station_id"],
        "selected_station_id": selected["station_id"],
        "station_override_applied": selected["station_id"] != recommended["station_id"],
        "withheld_station_id": withheld["station_id"] if withheld else None,
        "candidates": candidates,
        "duplicate_records": duplicate_report,
        "gap_fill_fraction": 0.0,
        "gap_policy": "No temporal or cross-station filling; missing hours remain masked.",
        "forcing_coverage": {
            **{
                variable: {
                    "available_hours": selected["variable_available_hours"][variable],
                    "missing_fraction": selected["variable_missing_fraction"][variable],
                    "longest_gap_hours": selected["longest_gap_hours"][variable],
                }
                for variable in DIRECT_FIELDS
            },
            "precipitation_phase": {
                "available_hours": selected["variable_available_hours"]["precipitation_amount"],
                "missing_fraction": selected["variable_missing_fraction"]["precipitation_amount"],
                "longest_gap_hours": selected["longest_gap_hours"]["precipitation_amount"],
            },
            "shortwave_radiation": {
                "available_hours": 0,
                "missing_fraction": 1.0,
                "longest_gap_hours": len(timeline),
            },
            "longwave_radiation": {
                "available_hours": 0,
                "missing_fraction": 1.0,
                "longest_gap_hours": len(timeline),
            },
        },
        "qc_excluded_values": selected["qc_excluded_values"],
        "staleness": "Historical immutable snapshot; current-condition staleness is not applicable.",
        "withheld_station_comparison": comparisons,
        "provider_disagreement": (
            "Only one provider is implemented. The station comparison is not an independent "
            "provider comparison and cannot satisfy that M2 acceptance criterion."
        ),
        "representativeness": (
            "All candidates are valley/airport stations below the Mount Hosmer reference; "
            "orographic precipitation, ridge wind, inversions, and local radiation are unresolved."
        ),
    }


def _timeline(start: datetime, end: datetime) -> tuple[datetime, ...]:
    from datetime import timedelta

    count = int((end - start).total_seconds() // 3600) + 1
    return tuple(start + timedelta(hours=index) for index in range(count))


def _qc_flags(flag: str, record_flag: str = "") -> tuple[dict[str, str], ...]:
    values: list[dict[str, str]] = []
    for code in (flag, record_flag):
        if not code:
            continue
        values.append(
            {
                "code": code,
                "severity": "rejected" if code == "M" else "suspect",
                "source": "ECCC source field",
                "meaning": (
                    "ECCC missing value; value excluded."
                    if code == "M"
                    else "Provider flag retained without inventing a universal element-specific meaning."
                ),
            }
        )
    return tuple(values)


def _uncertainty(basis: str) -> dict[str, Any]:
    return {
        "status": "unknown",
        "standard_uncertainty": None,
        "unit": None,
        "basis": basis,
    }


def _staleness(missing: bool) -> dict[str, Any]:
    return {
        "status": "unknown" if missing else "not_applicable",
        "age_seconds": None,
        "threshold_seconds": None,
        "basis": "Immutable historical forcing; no current-condition freshness claim.",
    }


class ECCCHistoricalProvider:
    """Normalize one cached ECCC historical snapshot into the M1 contract."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        snapshot: ECCCSnapshot,
        *,
        target_longitude_deg: float,
        target_latitude_deg: float,
        target_elevation_m: float,
        selected_station_id: str | None = None,
        lapse_rate_k_per_m: float = 0.0065,
        snow_max_c: float = 0.0,
        rain_min_c: float = 2.0,
    ) -> None:
        if not 0 < lapse_rate_k_per_m < 0.02:
            raise ECCCProviderError("lapse_rate_k_per_m must be in (0, 0.02).")
        if rain_min_c <= snow_max_c:
            raise ECCCProviderError("rain_min_c must exceed snow_max_c.")
        self.snapshot = snapshot
        self.target_longitude_deg = target_longitude_deg
        self.target_latitude_deg = target_latitude_deg
        self.target_elevation_m = target_elevation_m
        self.selected_station_id = selected_station_id
        self.lapse_rate_k_per_m = lapse_rate_k_per_m
        self.snow_max_c = snow_max_c
        self.rain_min_c = rain_min_c

    def quality_report(self, request: ConditionRequest) -> dict[str, Any]:
        return audit_eccc_snapshot(
            self.snapshot,
            request,
            target_longitude_deg=self.target_longitude_deg,
            target_latitude_deg=self.target_latitude_deg,
            target_elevation_m=self.target_elevation_m,
            selected_station_id=self.selected_station_id,
        )

    def normalize(self, request: ConditionRequest) -> ConditionPackDraft:
        quality = self.quality_report(request)
        selected_id = self.selected_station_id or quality["selected_station_id"]
        stations = _read_stations(self.snapshot)
        all_records, _duplicates = _read_hourly(
            self.snapshot, request.valid_start_utc, request.valid_end_utc
        )
        if selected_id not in stations or selected_id not in all_records:
            raise ECCCProviderError(f"Selected station {selected_id!r} is unavailable.")
        station = stations[selected_id]
        records = all_records[selected_id]
        timeline = _timeline(request.valid_start_utc, request.valid_end_utc)
        code_hash = _sha256_bytes(Path(__file__).read_bytes())
        hourly_size = self.snapshot.hourly_path.stat().st_size
        station_size = self.snapshot.stations_path.stat().st_size
        file_times = [
            datetime.fromisoformat(record["source_mtime_utc"]).astimezone(UTC)
            for record in self.snapshot.manifest["files"].values()
        ]
        transformations = [
            {
                "transformation_id": "eccc-units-v1",
                "method": "Explicit ECCC source-unit conversion to M1 canonical units.",
                "version": "1",
                "code_sha256": code_hash,
                "parameters": {
                    "temperature": "degC to K",
                    "wind_speed": "km h-1 to m s-1",
                    "wind_direction": "tens of degrees true to degrees true",
                    "precipitation": "mm water equivalent per hour to kg m-2 h-1",
                    "pressure": "kPa to Pa",
                },
            },
            {
                "transformation_id": "precipitation-phase-v1",
                "method": "Air-temperature dual-threshold categorical phase partition.",
                "version": "1",
                "code_sha256": code_hash,
                "parameters": {
                    "snow_at_or_below_degC": self.snow_max_c,
                    "rain_at_or_above_degC": self.rain_min_c,
                    "between": "mixed",
                    "citation": PHASE_CITATION,
                },
            },
            {
                "transformation_id": "temperature-elevation-v1",
                "method": "Fixed environmental lapse-rate transfer to caller-supplied target elevation.",
                "version": "1",
                "code_sha256": code_hash,
                "parameters": {
                    "lapse_rate_k_per_m": self.lapse_rate_k_per_m,
                    "source_elevation_m": station.elevation_m,
                    "target_elevation_m": self.target_elevation_m,
                    "citation": LAPSE_CITATION,
                },
            },
        ]
        variables: dict[str, Any] = {}
        raw_cache: dict[datetime, dict[str, tuple[float | None, str]]] = {}
        corrected_temperature_c: dict[datetime, float | None] = {}
        for timestamp in timeline:
            row = records.get(timestamp)
            raw_cache[timestamp] = {
                variable: _raw_value(row, variable) for variable in DIRECT_FIELDS
            }
            raw_temp, _ = raw_cache[timestamp]["air_temperature"]
            corrected_temperature_c[timestamp] = (
                None
                if raw_temp is None
                else raw_temp
                - self.lapse_rate_k_per_m * (self.target_elevation_m - station.elevation_m)
            )

        for variable in CANONICAL_UNITS:
            values: list[dict[str, Any]] = []
            for timestamp in timeline:
                row = records.get(timestamp)
                record_id = (
                    row["ID"].strip()
                    if row is not None
                    else f"{selected_id}:{timestamp.isoformat()}:absent"
                )
                record_flag = (row.get("FLAG") or "").strip() if row else ""
                flag = ""
                transformation_ids: list[str] = []
                if variable in DIRECT_FIELDS:
                    raw, flag = raw_cache[timestamp][variable]
                    if variable == "air_temperature":
                        raw = corrected_temperature_c[timestamp]
                    if variable == "wind_direction" and raw is not None:
                        raw = (raw * 10.0) % 360.0
                    source_unit = DIRECT_FIELDS[variable][2]
                    if variable == "wind_direction":
                        source_unit = "degree_true"
                    value = convert_value(variable, raw, source_unit)
                    transformation_ids.append("eccc-units-v1")
                    if variable == "air_temperature":
                        transformation_ids.append("temperature-elevation-v1")
                elif variable == "precipitation_phase":
                    precip, precip_flag = raw_cache[timestamp]["precipitation_amount"]
                    flag = precip_flag
                    temperature_c = corrected_temperature_c[timestamp]
                    if precip is None:
                        value = None
                    elif precip == 0:
                        value = "none"
                    elif temperature_c is None:
                        value = "unknown"
                    elif temperature_c <= self.snow_max_c:
                        value = "snow"
                    elif temperature_c >= self.rain_min_c:
                        value = "rain"
                    else:
                        value = "mixed"
                    transformation_ids.extend(
                        ["eccc-units-v1", "precipitation-phase-v1", "temperature-elevation-v1"]
                    )
                else:
                    value = None
                missing = value is None
                values.append(
                    {
                        "station_id": f"eccc-{selected_id}",
                        "time_utc": timestamp,
                        "value": value,
                        "masked": missing,
                        "status": "missing" if missing else "observed",
                        "qc_flags": _qc_flags(flag, record_flag),
                        "uncertainty": _uncertainty(
                            "ECCC supplies no per-value measurement uncertainty; elevation transfer "
                            "and phase classification uncertainty are not quantified."
                        ),
                        "staleness": _staleness(missing),
                        "lineage": (
                            {
                                "source_file_sha256": self.snapshot.hourly_sha256,
                                "source_record_id": record_id,
                                "transformation_ids": tuple(sorted(set(transformation_ids))),
                            },
                        ),
                    }
                )
            if variable in DIRECT_FIELDS:
                provenance = {
                    "kind": "direct",
                    "method": (
                        "ECCC hourly field normalized without temporal gap filling; air temperature "
                        "is additionally transferred to target elevation."
                    ),
                    "version": "1",
                    "source_variables": (DIRECT_FIELDS[variable][0],),
                    "citation": ECCC_TECHNICAL_URI,
                    "assumptions": (
                        "Station observation represents regional forcing only; not terrain-scale truth.",
                    ),
                }
            elif variable == "precipitation_phase":
                provenance = {
                    "kind": "derived",
                    "method": "Dual-threshold phase from elevation-corrected air temperature.",
                    "version": "1",
                    "source_variables": ("TEMP", "PRECIP_AMOUNT"),
                    "citation": PHASE_CITATION,
                    "assumptions": (
                        "Snow at <=0 C, rain at >=2 C, mixed within the uncertainty band.",
                        "No vertical atmospheric profile or direct phase observation is available.",
                    ),
                }
            else:
                provenance = {
                    "kind": "derived",
                    "method": "Provider does not supply this radiation variable; preserve as missing.",
                    "version": "1",
                    "source_variables": ("ECCC hourly observation record",),
                    "citation": ECCC_TECHNICAL_URI,
                    "assumptions": ("No radiation derivation or zero substitution is permitted.",),
                }
            variables[variable] = {
                "variable": variable,
                "unit": CANONICAL_UNITS[variable],
                "provenance": provenance,
                "values": values,
            }

        return ConditionPackDraft.model_validate(
            {
                "schema_version": CONDITION_PACK_SCHEMA_VERSION,
                "mountain_grid": request.mountain_grid.model_dump(mode="json"),
                "source": {
                    "provider_id": PROVIDER_ID,
                    "title": "ECCC Historical Hourly Climate Station Data",
                    "citation": (
                        "Environment and Climate Change Canada, Historical Hourly Climate Data; "
                        "snapshot extracted from the ECCC GeoMet climate-hourly collection."
                    ),
                    "source_uri": ECCC_HOURLY_URI,
                    "licence": ECCC_LICENCE,
                    "licence_uri": ECCC_LICENCE_URI,
                    "permitted_use": (
                        "Use, reproduce, modify, publish, translate, and distribute with required "
                        "source attribution, no endorsement, and applicable third-party attribution."
                    ),
                },
                "times": {
                    "acquisition_start_utc": min(file_times),
                    "acquisition_end_utc": max(file_times),
                    "publication_time_utc": max(file_times),
                    "valid_start_utc": request.valid_start_utc,
                    "valid_end_utc": request.valid_end_utc,
                    "staleness_reference_time_utc": max(file_times),
                    "cadence_seconds": 3600,
                },
                "stations": (
                    {
                        "station_id": f"eccc-{selected_id}",
                        "name": f"{station.name} (temperature transferred to target elevation)",
                        "longitude_deg": station.longitude_deg,
                        "latitude_deg": station.latitude_deg,
                        "elevation_m": station.elevation_m,
                        "coordinate_source": (
                            "ECCC climate-stations snapshot; coordinates/elevation describe source "
                            "station, while the temperature transformation records target elevation."
                        ),
                        "horizontal_uncertainty_m": None,
                        "elevation_uncertainty_m": None,
                    },
                ),
                "source_files": (
                    {
                        "source_file_id": "eccc-hourly",
                        "locator": f"source-cache://eccc/{self.snapshot.snapshot_id}/{HOURLY_FILENAME}",
                        "sha256": self.snapshot.hourly_sha256,
                        "bytes": hourly_size,
                        "media_type": "text/csv",
                    },
                    {
                        "source_file_id": "eccc-stations",
                        "locator": f"source-cache://eccc/{self.snapshot.snapshot_id}/{STATIONS_FILENAME}",
                        "sha256": self.snapshot.stations_sha256,
                        "bytes": station_size,
                        "media_type": "text/csv",
                    },
                ),
                "transformations": transformations,
                "normalization": {
                    "software": "mount-hosmer-eccc-historical-normalizer",
                    "software_version": "1",
                    "method": "Strict UTC-hour normalization through the M1 ConditionPack contract.",
                    "code_sha256": code_hash,
                },
                "variables": variables,
                "limitations": (
                    "Experimental historical reconstruction, not an operational avalanche forecast; "
                    "never replaces Avalanche Canada guidance or field assessment.",
                    "Scores derived later from this forcing remain relative indices, not probabilities.",
                    "Radiation is unavailable and remains explicitly masked for every hour.",
                    "No temporal, cross-station, reanalysis, undercatch, or wind correction is applied.",
                    "A fixed 6.5 K/km elevation transfer over a large valley-to-mountain elevation "
                    "difference is unvalidated at Mount Hosmer and may fail during inversions.",
                    "The 0-2 C phase transition is an uncertainty category, not observed phase.",
                    "Provider publication time was absent; local source mtime is retained as an "
                    "acquisition bound and must not be interpreted as exact publication time.",
                ),
            }
        )


def mountain_grid_from_pack(path: str | Path) -> MountainGridIdentity:
    """Build a grid identity from the current mountain-pack contract, not stale runtime."""

    pack_path = Path(path).resolve()
    raw_bytes = pack_path.read_bytes()
    raw = json.loads(raw_bytes.decode("utf-8"))
    grid = raw["grid"]
    west, south, east, north = (float(value) for value in grid["bounds"])
    resolution = float(grid["resolution_m"])
    columns = round((east - west) / resolution)
    rows = round((north - south) / resolution)
    grid_payload = {
        "analysis_crs": grid["analysis_crs"],
        "coordinate_order": grid["coordinate_order"],
        "bounds": [west, south, east, north],
        "resolution_m": resolution,
        "rows": rows,
        "columns": columns,
    }
    return MountainGridIdentity(
        mountain_pack_id=str(raw["id"]),
        mountain_pack_sha256=_sha256_bytes(raw_bytes),
        grid_sha256=_sha256_bytes(_canonical_json_bytes(grid_payload)),
        crs=str(grid["analysis_crs"]),
        axis_order="easting_northing",
        horizontal_units="metre",
        rows=rows,
        columns=columns,
        resolution_m=resolution,
    )


def write_quality_report(report: dict[str, Any], output_path: str | Path) -> Path:
    """Write a deterministic derived report without modifying the source snapshot."""

    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_json_bytes(report)
    if target.exists() and target.read_bytes() != content:
        raise ECCCProviderError(f"Refusing to replace a different quality report: {target}")
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    return target
