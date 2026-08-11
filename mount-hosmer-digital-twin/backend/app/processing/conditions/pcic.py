"""Offline PCIC/PCDS station normalization and independent-provider comparison.

The selected observation history is ENV-AQN 585 / PCIC history 14942.  Its
original observing organization is Teck Coal Limited (Greenhills Operations),
not ECCC.  Network acquisition is an explicit offline operation; replay reads
only an immutable source-cache snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from avycore.conditions import (
    CANONICAL_UNITS,
    CONDITION_PACK_SCHEMA_VERSION,
    ConditionPack,
    ConditionPackDraft,
)
from avycore.conditions.units import convert_value

from .protocol import ConditionRequest


UTC = timezone.utc
PROVIDER_ID = "pcic-pcds-env-aqn"
SNAPSHOT_SCHEMA = "pcic-pcds-source-snapshot-v1"
QUALITY_SCHEMA = "pcic-pcds-forcing-quality-v1"
COMPARISON_SCHEMA = "pcic-eccc-provider-disagreement-v1"
SELECTION_SCHEMA = "pcic-station-selection-v1"

NETWORK_NAME = "ENV-AQN"
NETWORK_LONG_NAME = "BC Ministry of Environment and Parks - Air Quality Network"
PCIC_STATION_ID = "585"
PCIC_INTERNAL_STATION_ID = 13010
PCIC_HISTORY_ID = 14942
ORIGINAL_STATION_ID = "E290310"
ORIGINAL_SOURCE_ORGANIZATION = "Teck Coal Limited - Greenhills Operations"
ORIGINAL_STATION_NAME = "Elkford Rocky Mountain Elementary School"
ORIGINAL_OPERATOR_EVIDENCE_URI = (
    "https://www.teck.com/media/2017-Teck-Coal-Ltd.-Regional-Air-Monitoring-Program-Report.pdf"
)
STATION_LONGITUDE_DEG = -114.93342
STATION_LATITUDE_DEG = 50.0077944
STATION_ELEVATION_M = 1333.0

OGL_NAME = "Open Government Licence - British Columbia"
OGL_VERSION = "2.0"
OGL_URI = (
    "https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/"
    "open-data/open-government-licence-bc"
)
OGL_ATTRIBUTION = "Contains information licensed under the Open Government Licence - British Columbia."
DATASET_RECORD_ID = "01867404-ba2a-470e-94b7-0604607cfa30"
DATASET_RECORD_URI = (
    "https://catalogue.data.gov.bc.ca/dataset/"
    "air-quality-and-climate-monitoring-unverified-hourly-air-quality-and-"
    "meteorological-data"
)
DATASET_API_URI = (
    "https://catalogue.data.gov.bc.ca/api/3/action/package_show?id=" + DATASET_RECORD_ID
)
PCIC_TERMS_URI = "https://www.uvic.ca/pcic/about/terms-of-use/index.php"
PCIC_DISCLAIMER_URI = (
    "https://www.uvic.ca/pcic/data-analysis-tools/data-portal/station-data/index.php"
)
CRMP_URI = (
    "https://www2.gov.bc.ca/assets/gov/environment/research-monitoring-and-reporting/"
    "monitoring/emre/agreement_on_management_of_meteorological_networks.pdf"
)
PORTAL_DOCS_URI = "https://services.pacificclimate.org/portal/docs/mdp/root.html"

OBSERVATION_START_UTC = datetime(2025, 11, 1, 0, tzinfo=UTC)
OBSERVATION_END_UTC = datetime(2026, 5, 31, 23, tzinfo=UTC)
OBSERVATION_POLYGON = (
    "POLYGON ((-114.9340 50.0072, -114.9328 50.0072, -114.9328 50.0084, "
    "-114.9340 50.0084, -114.9340 50.0072))"
)

OBSERVATIONS_FILENAME = "pcic-observations.zip"
STATION_FILENAME = "pcic-station.json"
STATION_VARIABLES_FILENAME = "pcic-station-variables.json"
NETWORKS_FILENAME = "pcic-networks.json"
SOURCE_HISTORY_FILENAME = "original-operator-evidence.pdf"
LICENCE_RECORD_FILENAME = "ogl-dataset-record.json"
MANIFEST_FILENAME = "source-manifest.json"
OBSERVATION_ENTRY = f"{NETWORK_NAME}/{PCIC_STATION_ID}.csv"
VARIABLES_ENTRY = f"{NETWORK_NAME}/variables.csv"
EXPECTED_ARCHIVE_ENTRIES = {OBSERVATION_ENTRY, VARIABLES_ENTRY}

MAX_COMPRESSED_OBSERVATION_BYTES = 25 * 1024 * 1024
MAX_EXPANDED_OBSERVATION_BYTES = 100 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024

STATION_URI = (
    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/stations/"
    f"{PCIC_INTERNAL_STATION_ID}"
)
STATION_VARIABLES_URI = STATION_URI + "/variables"
NETWORKS_URI = (
    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
    "networks?provinces=BC"
)
SOURCE_HISTORY_URI = ORIGINAL_OPERATOR_EVIDENCE_URI


def _observation_uri() -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "from-date": "2025/11/01",
            "to-date": "2026/05/31",
            "network-name": NETWORK_NAME,
            "input-vars": "wind_from_direction_point,wind_speed_point",
            "input-freq": "",
            "input-polygon": OBSERVATION_POLYGON,
            "only-with-climatology": "",
            "download-timeseries": "Timeseries",
            "data-format": "csv",
            "cliptodate": "cliptodate",
        }
    )
    return (
        "https://services.pacificclimate.org/met-data-portal-pcds/api/data/"
        f"pcds/agg/?{query}"
    )


SOURCE_URLS = {
    LICENCE_RECORD_FILENAME: DATASET_API_URI,
    NETWORKS_FILENAME: NETWORKS_URI,
    OBSERVATIONS_FILENAME: _observation_uri(),
    SOURCE_HISTORY_FILENAME: SOURCE_HISTORY_URI,
    STATION_FILENAME: STATION_URI,
    STATION_VARIABLES_FILENAME: STATION_VARIABLES_URI,
}
MEDIA_TYPES = {
    LICENCE_RECORD_FILENAME: "application/json",
    NETWORKS_FILENAME: "application/json",
    OBSERVATIONS_FILENAME: "application/zip",
    SOURCE_HISTORY_FILENAME: "application/pdf",
    STATION_FILENAME: "application/json",
    STATION_VARIABLES_FILENAME: "application/json",
}

RAW_FIELDS = {
    "relative_humidity": (("HUMIDITY", "%"), ("avg_rel_hum_pst1hr", "%")),
    "wind_speed": (("WSPD_SCLR", "m/s"),),
    "wind_direction": (("WDIR_VECT", "degree"),),
    "air_temperature": (("TEMP_MEAN", "celsius"), ("avg_air_temp_pst1hr", "celsius")),
}
EXPECTED_OBSERVATION_COLUMNS = {
    "time",
    "WDIR_VECT",
    "WSPD_SCLR",
}
EXPECTED_VARIABLE_COLUMNS = {"variable", "standard_name", "cell_method", "unit"}


class PCICProviderError(ValueError):
    """Raised when PCIC source lineage, observations, or comparisons are ambiguous."""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise PCICProviderError("Acquisition timestamps must be timezone-aware UTC.")
    return value.astimezone(UTC).isoformat()


def _parse_manifest_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PCICProviderError(f"Malformed {field}: expected an ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PCICProviderError(f"Malformed {field}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PCICProviderError(f"{field} must be timezone-aware UTC.")
    return parsed.astimezone(UTC)


def _parse_raw_utc(value: str) -> datetime:
    """Parse the PCDS CSV UTC convention without accepting local offsets.

    The aggregate CSV omits a suffix.  The paired PCIC station-variable API
    represents the same observation axis with a trailing ``Z``.  That explicit
    metadata evidence is recorded in the snapshot manifest.
    """

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise PCICProviderError(
            f"PCIC observation time must be exact 'YYYY-MM-DD HH:MM:SS' UTC: {value!r}"
        ) from exc
    if parsed.minute or parsed.second or parsed.microsecond:
        raise PCICProviderError(f"PCIC observation time is not an exact UTC hour: {value!r}")
    return parsed


def _finite_or_none(value: str, field: str) -> float | None:
    cleaned = value.strip()
    if cleaned in {"", "None", "null", "NULL", "-9999"}:
        return None
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise PCICProviderError(f"Malformed PCIC numeric value {field}={value!r}.") from exc
    if not math.isfinite(number):
        raise PCICProviderError(f"Non-finite PCIC numeric value {field}={value!r}.")
    return number


def _timeline(start: datetime, end: datetime) -> tuple[datetime, ...]:
    count = int((end - start).total_seconds() // 3600) + 1
    return tuple(start + timedelta(hours=index) for index in range(count))


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class PCICCandidate:
    network: str
    station_id: str
    history_id: str
    name: str
    original_organization: str | None
    source_observation_id: str | None
    history_unambiguous: bool
    redistribution_permitted: bool
    overlap_hours: int
    comparable_variables: tuple[str, ...]
    qc_revision_score: int
    elevation_difference_m: float
    horizontal_distance_km: float
    rejection_reason: str | None = None

    def independence_rejection(self) -> str | None:
        organization = (self.original_organization or "").casefold()
        name = self.name.casefold()
        if self.network in {"EC", "EC_raw"}:
            return "ECCC/EC_raw network observations are expressly excluded."
        if self.source_observation_id == "1157631" or "sparwood (ec)" in name:
            return "Duplicate or renamed ECCC 1157631 lineage is expressly excluded."
        if any(token in organization for token in ("environment canada", "eccc")):
            return "Original observing organization is ECCC, not independent."
        if not self.original_organization:
            return "Original observing organization is not established."
        if not self.history_unambiguous:
            return "Station history or source lineage is ambiguous."
        return self.rejection_reason

    def eligible(self) -> bool:
        return self.independence_rejection() is None and self.redistribution_permitted

    def rank(self) -> tuple[Any, ...]:
        if not self.eligible():
            raise PCICProviderError(f"Ineligible candidate cannot be ranked: {self.station_id}")
        return (
            -self.overlap_hours,
            -len(self.comparable_variables),
            -self.qc_revision_score,
            abs(self.elevation_difference_m),
            self.horizontal_distance_km,
            self.network,
            self.station_id,
            self.history_id,
        )


def select_pcic_candidate(candidates: Iterable[PCICCandidate]) -> PCICCandidate:
    eligible = [candidate for candidate in candidates if candidate.eligible()]
    if not eligible:
        raise PCICProviderError("No genuinely independent, cache-eligible PCIC history remains.")
    return min(eligible, key=PCICCandidate.rank)


def default_pcic_candidate_audit(
    *, target_longitude_deg: float, target_latitude_deg: float, target_elevation_m: float
) -> tuple[PCICCandidate, ...]:
    """Return the fixed, source-documented screening set for this M2 increment."""

    def candidate(
        network: str,
        station_id: str,
        history_id: str,
        name: str,
        organization: str | None,
        source_id: str | None,
        history_ok: bool,
        licence_ok: bool,
        overlap: int,
        variables: tuple[str, ...],
        elevation_m: float,
        longitude_deg: float,
        latitude_deg: float,
        reason: str | None = None,
    ) -> PCICCandidate:
        return PCICCandidate(
            network=network,
            station_id=station_id,
            history_id=history_id,
            name=name,
            original_organization=organization,
            source_observation_id=source_id,
            history_unambiguous=history_ok,
            redistribution_permitted=licence_ok,
            overlap_hours=overlap,
            comparable_variables=variables,
            qc_revision_score=0,
            elevation_difference_m=elevation_m - target_elevation_m,
            horizontal_distance_km=_haversine_km(
                target_longitude_deg,
                target_latitude_deg,
                longitude_deg,
                latitude_deg,
            ),
            rejection_reason=reason,
        )

    return (
        candidate(
            "ENV-AQN",
            "585",
            "14942",
            ORIGINAL_STATION_NAME,
            ORIGINAL_SOURCE_ORGANIZATION,
            ORIGINAL_STATION_ID,
            True,
            True,
            2694,
            ("wind_direction", "wind_speed"),
            STATION_ELEVATION_M,
            STATION_LONGITUDE_DEG,
            STATION_LATITUDE_DEG,
        ),
        candidate(
            "ENV-AQN",
            "551",
            "14449",
            "Cranbrook Muriel Baxter_60",
            "BC Ministry of Environment and Parks",
            "551",
            True,
            True,
            2576,
            ("relative_humidity", "wind_direction", "wind_speed"),
            941.0,
            -115.753682,
            49.507103,
        ),
        candidate(
            "FLNRO-WMB",
            "886",
            "provider history",
            "Goathaven",
            "BC Wildfire Service",
            "886",
            True,
            False,
            0,
            (),
            1063.0,
            -115.2144,
            49.6673,
            "Exact observation record is Access Only; immutable redistribution is not permitted.",
        ),
        candidate(
            "BCH",
            "2C09Q",
            "provider history",
            "Morrissey Ridge 2C09Q",
            "BC Hydro",
            "2C09Q",
            True,
            False,
            0,
            (),
            1800.0,
            -114.967,
            49.45,
            "BC Hydro grants no reusable observation licence without case-specific permission.",
        ),
        candidate(
            "BCH",
            "2C21P",
            "absent",
            "Fernie 2C21P",
            "BC Hydro",
            "2C21P",
            False,
            False,
            0,
            (),
            1100.0,
            -115.07,
            49.49,
            "No PCIC BCH/2C21P history exists; BCH/FER is a different station and is not substituted.",
        ),
        candidate(
            "FLNRO-WMB",
            "412",
            "provider history",
            "Elko",
            "BC Wildfire Service",
            "412",
            True,
            False,
            0,
            (),
            876.0,
            -115.1545,
            49.2876,
            "Exact observation record is Access Only; immutable redistribution is not permitted.",
        ),
        candidate(
            "MoTIe",
            "36124",
            "provider history",
            "Morrissey",
            "Ministry of Transportation and Transit",
            "36124",
            True,
            False,
            0,
            (),
            960.0,
            -115.02306,
            49.39,
            "PAWS observations are Access Only; the separate OGL station-location record does not apply.",
        ),
        candidate(
            "ENV-ASP",
            "2C10P",
            "provider history",
            "Moyie Mountain",
            "BC Ministry of Environment and Parks",
            "2C10P",
            True,
            True,
            0,
            ("snow_depth", "snow_water_equivalent"),
            1800.0,
            -115.6,
            49.3,
            "PCIC history ends before the exact winter; no overlap.",
        ),
        candidate(
            "AGRI",
            "nearest screened histories",
            "network metadata histories",
            "Relevant southeast B.C. agriculture histories",
            "BC Ministry of Agriculture and Foods",
            "network screening",
            True,
            False,
            0,
            (),
            900.0,
            -116.0,
            49.2,
            "Nearest relevant PCIC agriculture history ends before 2025-11-01; no overlap.",
        ),
        candidate(
            "EC_raw",
            "1157631",
            "duplicate ECCC lineage",
            "Sparwood (EC)",
            "Environment and Climate Change Canada",
            "1157631",
            True,
            True,
            5085,
            ("wind_direction", "wind_speed"),
            1136.7,
            -114.8839,
            49.745,
        ),
    )


def _candidate_as_report(candidate: PCICCandidate) -> dict[str, Any]:
    rejection = candidate.independence_rejection()
    if rejection is None and not candidate.redistribution_permitted:
        rejection = "Licence does not permit immutable caching and fixture redistribution."
    return {
        "network": candidate.network,
        "station_id": candidate.station_id,
        "history_id": candidate.history_id,
        "name": candidate.name,
        "original_organization": candidate.original_organization,
        "source_observation_id": candidate.source_observation_id,
        "history_unambiguous": candidate.history_unambiguous,
        "redistribution_permitted": candidate.redistribution_permitted,
        "overlap_hours": candidate.overlap_hours,
        "comparable_variables": list(candidate.comparable_variables),
        "qc_revision_score": candidate.qc_revision_score,
        "elevation_difference_m": candidate.elevation_difference_m,
        "horizontal_distance_km": candidate.horizontal_distance_km,
        "eligible": candidate.eligible(),
        "rejection_reason": rejection,
        "selection_rank": list(candidate.rank()) if candidate.eligible() else None,
    }


@dataclass(frozen=True)
class PCICSnapshot:
    root: Path
    manifest: dict[str, Any]
    observations_path: Path

    @property
    def snapshot_id(self) -> str:
        return str(self.manifest["snapshot_id"])

    @property
    def observations_sha256(self) -> str:
        return str(self.manifest["files"][OBSERVATIONS_FILENAME]["sha256"])


@dataclass(frozen=True)
class ParsedPCICObservations:
    records: dict[datetime, dict[str, float | None]]
    raw_rows: dict[datetime, dict[str, str]]
    exact_duplicate_rows: int
    exact_duplicate_values: int
    calm_direction_masks: int
    variable_units: dict[str, str]


def _safe_archive_records(content: bytes) -> tuple[dict[str, dict[str, Any]], int]:
    if len(content) > MAX_COMPRESSED_OBSERVATION_BYTES:
        raise PCICProviderError("PCIC observation archive exceeds the 25 MiB compressed limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise PCICProviderError("PCIC observation download is not a valid ZIP archive.") from exc
    records: dict[str, dict[str, Any]] = {}
    expanded = 0
    with archive:
        names = [entry.filename for entry in archive.infolist()]
        if len(names) != len(set(names)):
            raise PCICProviderError("PCIC observation archive contains duplicate paths.")
        if set(names) != EXPECTED_ARCHIVE_ENTRIES:
            raise PCICProviderError(
                f"PCIC observation archive entries changed: {sorted(names)!r}."
            )
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or entry.is_dir():
                raise PCICProviderError(f"Unsafe PCIC archive entry: {entry.filename!r}.")
            if entry.flag_bits & 0x1:
                raise PCICProviderError("Encrypted PCIC archive entries are not supported.")
            expanded += entry.file_size
            if expanded > MAX_EXPANDED_OBSERVATION_BYTES:
                raise PCICProviderError("PCIC observation archive exceeds the 100 MiB expanded limit.")
            payload = archive.read(entry)
            if len(payload) != entry.file_size:
                raise PCICProviderError(f"Truncated PCIC archive entry: {entry.filename!r}.")
            records[entry.filename] = {
                "bytes": len(payload),
                "compressed_bytes": entry.compress_size,
                "sha256": _sha256_bytes(payload),
            }
    return dict(sorted(records.items())), expanded


def _json_document(content: bytes, filename: str) -> Any:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PCICProviderError(f"Invalid JSON metadata in {filename}.") from exc


def _validate_official_metadata(contents: Mapping[str, bytes]) -> None:
    station = _json_document(contents[STATION_FILENAME], STATION_FILENAME)
    if station.get("id") != PCIC_INTERNAL_STATION_ID or station.get("native_id") != PCIC_STATION_ID:
        raise PCICProviderError("PCIC station metadata identity changed.")
    if station.get("network_uri") != "/networks/9":
        raise PCICProviderError("PCIC station metadata network changed.")
    histories = station.get("histories")
    if not isinstance(histories, list) or len(histories) != 1:
        raise PCICProviderError("Selected PCIC station histories changed or became ambiguous.")
    history = histories[0]
    expected_history = {
        "id": PCIC_HISTORY_ID,
        "lat": STATION_LATITUDE_DEG,
        "lon": STATION_LONGITUDE_DEG,
    }
    for field, expected in expected_history.items():
        if history.get(field) != expected:
            raise PCICProviderError(f"PCIC station history has unexpected {field}.")
    variable_metadata = _json_document(
        contents[STATION_VARIABLES_FILENAME], STATION_VARIABLES_FILENAME
    )
    if variable_metadata.get("station_id") != PCIC_INTERNAL_STATION_ID:
        raise PCICProviderError("PCIC station-variable metadata identity changed.")
    variables = variable_metadata.get("variables")
    if not isinstance(variables, list):
        raise PCICProviderError("PCIC station-variable metadata is malformed.")
    current = {
        item.get("name"): (item.get("standard_name"), item.get("cell_method"), item.get("unit"))
        for item in variables
    }
    expected = {
        "WDIR_VECT": ("wind_from_direction", "time: point", "degree"),
        "WSPD_SCLR": ("wind_speed", "time: point", "m/s"),
    }
    for name, metadata in expected.items():
        if current.get(name) != metadata:
            raise PCICProviderError(f"PCIC variable metadata changed for {name}.")
    networks = _json_document(contents[NETWORKS_FILENAME], NETWORKS_FILENAME)
    matches = [item for item in networks if item.get("name") == NETWORK_NAME]
    if len(matches) != 1 or matches[0].get("long_name") != NETWORK_LONG_NAME:
        raise PCICProviderError("PCIC ENV-AQN network identity changed.")
    licence = _json_document(contents[LICENCE_RECORD_FILENAME], LICENCE_RECORD_FILENAME)
    if licence.get("success") is not True:
        raise PCICProviderError("B.C. Data Catalogue licence record request was unsuccessful.")
    result = licence.get("result", {})
    if result.get("id") != DATASET_RECORD_ID or result.get("license_id") not in {
        "OGL-BC",
        "22-OGL",
        "ogl-bc",
    }:
        # The catalogue API has changed licence identifiers before.  The exact
        # title is still required below; unknown identifiers are never guessed.
        title = str(result.get("license_title", ""))
        if title != OGL_NAME:
            raise PCICProviderError("Exact B.C. dataset record is not explicitly OGL-BC.")
    operator_evidence = contents[SOURCE_HISTORY_FILENAME]
    if not operator_evidence.startswith(b"%PDF-") or len(operator_evidence) < 100_000:
        raise PCICProviderError("Original Teck station-operator evidence is not the expected PDF.")


def _identity_payload(
    files: Mapping[str, Mapping[str, Any]],
    archive_entries: Mapping[str, Mapping[str, Any]],
    expanded_bytes: int,
) -> dict[str, Any]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "provider_id": PROVIDER_ID,
        "observation_window": {
            "valid_start_utc": OBSERVATION_START_UTC.isoformat(),
            "valid_end_utc": OBSERVATION_END_UTC.isoformat(),
        },
        "pcic_identity": {
            "network": NETWORK_NAME,
            "station_id": PCIC_STATION_ID,
            "station_internal_id": PCIC_INTERNAL_STATION_ID,
            "history_ids": [PCIC_HISTORY_ID],
        },
        "source_identity": {
            "organization": ORIGINAL_SOURCE_ORGANIZATION,
            "station_id": ORIGINAL_STATION_ID,
            "station_name": ORIGINAL_STATION_NAME,
            "operator_evidence_uri": ORIGINAL_OPERATOR_EVIDENCE_URI,
        },
        "licence": {
            "name": OGL_NAME,
            "version": OGL_VERSION,
            "uri": OGL_URI,
            "dataset_record_id": DATASET_RECORD_ID,
            "dataset_record_uri": DATASET_RECORD_URI,
            "attribution": OGL_ATTRIBUTION,
        },
        "files": dict(sorted((name, dict(record)) for name, record in files.items())),
        "archive": {
            "filename": OBSERVATIONS_FILENAME,
            "expanded_bytes": expanded_bytes,
            "entries": dict(sorted((name, dict(record)) for name, record in archive_entries.items())),
        },
    }


def _cache_snapshot_contents(
    contents: Mapping[str, bytes],
    runtime_root: str | Path,
    *,
    acquisition_start_utc: datetime,
    acquisition_end_utc: datetime,
) -> Path:
    if set(contents) != set(SOURCE_URLS):
        raise PCICProviderError("PCIC cache import requires exactly the declared source files.")
    if acquisition_end_utc < acquisition_start_utc:
        raise PCICProviderError("Acquisition end precedes acquisition start.")
    _validate_official_metadata(contents)
    archive_entries, expanded_bytes = _safe_archive_records(contents[OBSERVATIONS_FILENAME])
    files = {
        name: {
            "bytes": len(payload),
            "sha256": _sha256_bytes(payload),
            "source_url": SOURCE_URLS[name],
            "media_type": MEDIA_TYPES[name],
        }
        for name, payload in contents.items()
    }
    identity = _identity_payload(files, archive_entries, expanded_bytes)
    snapshot_id = f"snapshot-{_sha256_bytes(_canonical_json_bytes(identity))}"
    manifest = {
        **identity,
        "snapshot_id": snapshot_id,
        "acquisition_start_utc": _iso_utc(acquisition_start_utc),
        "acquisition_end_utc": _iso_utc(acquisition_end_utc),
        "terms": {
            "pcic_terms_uri": PCIC_TERMS_URI,
            "pcic_terms_version": "retrieved at acquisition; page publishes no version identifier",
            "pcic_disclaimer_uri": PCIC_DISCLAIMER_URI,
            "portal_documentation_uri": PORTAL_DOCS_URI,
            "crmp_agreement_uri": CRMP_URI,
            "crmp_agreement_version": "2018-04-01 through 2026-03-31",
            "licence_acceptance": "OGL-BC 2.0 accepted by use for the exact dataset record only",
        },
        "timestamp_basis": (
            "Aggregate CSV timestamps omit a suffix; the paired PCIC station-variable API "
            "serializes the same observation axis with Z. Replay interprets the documented "
            "aggregate clock as UTC and rejects every non-exact-hour spelling."
        ),
        "provider_qc": {
            "per_observation_fields": [],
            "status": "PCIC aggregate CSV exposes no provider QC field for this history",
        },
        "revision": {
            "per_observation_fields": [],
            "status": (
                "Source labels observations unverified, preliminary, and subject to change; "
                "the immutable snapshot captures one revision without claiming final QC."
            ),
        },
    }
    cache_root = Path(runtime_root).resolve() / "sources" / "conditions" / "pcic"
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / snapshot_id
    if target.exists():
        load_pcic_snapshot(target)
        return target
    staging = cache_root / f".source-acquire-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, payload in contents.items():
            (staging / name).write_bytes(payload)
        (staging / MANIFEST_FILENAME).write_bytes(_canonical_json_bytes(manifest))
        loaded = load_pcic_snapshot(staging)
        if loaded.snapshot_id != snapshot_id:
            raise PCICProviderError("Staged PCIC source snapshot identity changed during import.")
        staging.rename(target)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def import_pcic_snapshot(
    source_files: Mapping[str, str | Path],
    runtime_root: str | Path,
    *,
    acquisition_start_utc: datetime,
    acquisition_end_utc: datetime,
) -> Path:
    """Import already-downloaded official files into the strict immutable cache."""

    if set(source_files) != set(SOURCE_URLS):
        raise PCICProviderError("PCIC import paths must match the declared source file set.")
    contents: dict[str, bytes] = {}
    for name, source in source_files.items():
        path = Path(source).resolve()
        if not path.is_file():
            raise PCICProviderError(f"PCIC import source file does not exist: {path}")
        contents[name] = path.read_bytes()
    return _cache_snapshot_contents(
        contents,
        runtime_root,
        acquisition_start_utc=acquisition_start_utc,
        acquisition_end_utc=acquisition_end_utc,
    )


def _download_bytes(url: str, maximum: int) -> bytes:
    request = Request(url, headers={"User-Agent": "Mount-Hosmer-Digital-Twin/PCIC-offline-acquisition"})
    with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed official HTTPS URLs
        if response.status != 200:
            raise PCICProviderError(f"Official source returned HTTP {response.status}: {url}")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > maximum:
            raise PCICProviderError(f"Official source exceeds download limit: {url}")
        content = response.read(maximum + 1)
    if len(content) > maximum:
        raise PCICProviderError(f"Official source exceeds download limit: {url}")
    return content


def acquire_pcic_snapshot(
    runtime_root: str | Path,
    *,
    clock: Callable[[], datetime] | None = None,
    downloader: Callable[[str, int], bytes] = _download_bytes,
) -> Path:
    """Download only the selected clipped series and small official metadata."""

    now = clock or (lambda: datetime.now(UTC))
    acquisition_start = now()
    contents = {
        name: downloader(
            url,
            MAX_COMPRESSED_OBSERVATION_BYTES
            if name == OBSERVATIONS_FILENAME
            else MAX_METADATA_BYTES,
        )
        for name, url in SOURCE_URLS.items()
    }
    acquisition_end = now()
    return _cache_snapshot_contents(
        contents,
        runtime_root,
        acquisition_start_utc=acquisition_start,
        acquisition_end_utc=acquisition_end,
    )


def load_pcic_snapshot(path: str | Path) -> PCICSnapshot:
    root = Path(path).resolve()
    manifest_path = root / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PCICProviderError(f"Invalid PCIC snapshot manifest at {manifest_path}: {exc}") from exc
    expected_keys = {
        "schema",
        "provider_id",
        "snapshot_id",
        "acquisition_start_utc",
        "acquisition_end_utc",
        "observation_window",
        "pcic_identity",
        "source_identity",
        "licence",
        "terms",
        "timestamp_basis",
        "provider_qc",
        "revision",
        "files",
        "archive",
    }
    if set(manifest) != expected_keys or manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise PCICProviderError("PCIC snapshot manifest has unsupported or unexpected fields.")
    if manifest.get("provider_id") != PROVIDER_ID:
        raise PCICProviderError("PCIC snapshot provider identity changed.")
    start = _parse_manifest_utc(manifest["acquisition_start_utc"], "acquisition_start_utc")
    end = _parse_manifest_utc(manifest["acquisition_end_utc"], "acquisition_end_utc")
    if end < start:
        raise PCICProviderError("PCIC snapshot acquisition bounds are reversed.")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(SOURCE_URLS):
        raise PCICProviderError("PCIC snapshot file set changed.")
    expected_root_files = set(SOURCE_URLS) | {MANIFEST_FILENAME}
    actual_root_files = {item.name for item in root.iterdir() if item.is_file()}
    if actual_root_files != expected_root_files or any(item.is_dir() for item in root.iterdir()):
        raise PCICProviderError("PCIC snapshot contains untracked files or directories.")
    contents: dict[str, bytes] = {}
    for name in sorted(SOURCE_URLS):
        record = files.get(name)
        if not isinstance(record, dict) or set(record) != {
            "bytes",
            "sha256",
            "source_url",
            "media_type",
        }:
            raise PCICProviderError(f"Malformed PCIC file lineage for {name}.")
        if record["source_url"] != SOURCE_URLS[name] or record["media_type"] != MEDIA_TYPES[name]:
            raise PCICProviderError(f"PCIC source URL/media lineage changed for {name}.")
        content = (root / name).read_bytes()
        contents[name] = content
        if record["bytes"] != len(content) or record["sha256"] != _sha256_bytes(content):
            raise PCICProviderError(f"PCIC snapshot checksum mismatch for {name}.")
    _validate_official_metadata(contents)
    entries, expanded = _safe_archive_records(contents[OBSERVATIONS_FILENAME])
    identity = _identity_payload(files, entries, expanded)
    if any(manifest.get(key) != value for key, value in identity.items()):
        raise PCICProviderError("PCIC snapshot identity-bound lineage is inconsistent.")
    expected_id = f"snapshot-{_sha256_bytes(_canonical_json_bytes(identity))}"
    if manifest.get("snapshot_id") != expected_id:
        raise PCICProviderError("PCIC source snapshot identity does not match immutable content.")
    if root.name.startswith("snapshot-") and root.name != expected_id:
        raise PCICProviderError("PCIC cache directory conflicts with its content identity.")
    return PCICSnapshot(root=root, manifest=manifest, observations_path=root / OBSERVATIONS_FILENAME)


def _variable_metadata(archive: zipfile.ZipFile) -> dict[str, tuple[str, str, str]]:
    payload = archive.read(VARIABLES_ENTRY).decode("utf-8-sig")
    stream = io.StringIO(payload, newline="")
    if stream.readline().strip() != "variables":
        raise PCICProviderError("PCIC variables.csv group header changed.")
    reader = csv.DictReader(stream, skipinitialspace=True)
    if set(reader.fieldnames or ()) != EXPECTED_VARIABLE_COLUMNS:
        raise PCICProviderError("PCIC variables.csv columns changed.")
    result: dict[str, tuple[str, str, str]] = {}
    for row in reader:
        name = row["variable"].strip()
        value = (
            row["standard_name"].strip(),
            row["cell_method"].strip(),
            row["unit"].strip(),
        )
        previous = result.get(name)
        if previous is not None and previous != value:
            raise PCICProviderError(f"Conflicting PCIC variable metadata for {name}.")
        result[name] = value
    expected = {
        "WDIR_VECT": ("wind_from_direction", "time: point", "degree"),
        "WSPD_SCLR": ("wind_speed", "time: point", "m/s"),
    }
    for name, value in expected.items():
        if result.get(name) != value:
            raise PCICProviderError(f"PCIC archive unit/variable definition changed for {name}.")
    return result


def _coalesce_raw(
    row: Mapping[str, str], variable: str
) -> tuple[float | None, str | None, int]:
    observed: list[tuple[float, str, str]] = []
    for field, unit in RAW_FIELDS[variable]:
        if field not in row:
            continue
        value = _finite_or_none(row[field], field)
        if value is not None:
            observed.append((value, unit, field))
    if not observed:
        return None, None, 0
    first_value, first_unit, _first_field = observed[0]
    for value, unit, field in observed[1:]:
        if unit != first_unit or value != first_value:
            raise PCICProviderError(
                f"Conflicting duplicate/revision fields for {variable}: {observed!r}."
            )
    return first_value, first_unit, len(observed) - 1


def parse_pcic_observations(snapshot: PCICSnapshot) -> ParsedPCICObservations:
    raw_content = snapshot.observations_path.read_bytes()
    _safe_archive_records(raw_content)
    with zipfile.ZipFile(io.BytesIO(raw_content)) as archive:
        metadata = _variable_metadata(archive)
        text = archive.read(OBSERVATION_ENTRY).decode("utf-8-sig")
    stream = io.StringIO(text, newline="")
    group = stream.readline().strip()
    if group != "station_observations":
        raise PCICProviderError("PCIC station CSV group header changed.")
    reader = csv.DictReader(stream, skipinitialspace=True)
    fields = {field.strip() for field in (reader.fieldnames or ())}
    if fields != EXPECTED_OBSERVATION_COLUMNS:
        raise PCICProviderError(f"PCIC observation columns changed: {sorted(fields)!r}.")
    records: dict[datetime, dict[str, float | None]] = {}
    raw_rows: dict[datetime, dict[str, str]] = {}
    exact_rows = 0
    exact_values = 0
    calm_masks = 0
    for input_row in reader:
        row = {key.strip(): (value or "").strip() for key, value in input_row.items()}
        timestamp = _parse_raw_utc(row["time"])
        if timestamp < OBSERVATION_START_UTC or timestamp > OBSERVATION_END_UTC:
            raise PCICProviderError("PCIC aggregate download contains an out-of-window observation.")
        previous = raw_rows.get(timestamp)
        if previous is not None:
            if previous == row:
                exact_rows += 1
                continue
            raise PCICProviderError(
                f"Conflicting duplicate/revised PCIC row at {timestamp.isoformat()}."
            )
        canonical: dict[str, float | None] = {}
        for variable in RAW_FIELDS:
            value, _unit, duplicates = _coalesce_raw(row, variable)
            canonical[variable] = value
            exact_values += duplicates
        speed = canonical["wind_speed"]
        if speed == 0 and canonical["wind_direction"] is not None:
            canonical["wind_direction"] = None
            calm_masks += 1
        records[timestamp] = canonical
        raw_rows[timestamp] = row
    if not records:
        raise PCICProviderError("PCIC snapshot contains no selected-station observations.")
    units = {name: value[2] for name, value in metadata.items()}
    return ParsedPCICObservations(
        records=records,
        raw_rows=raw_rows,
        exact_duplicate_rows=exact_rows,
        exact_duplicate_values=exact_values,
        calm_direction_masks=calm_masks,
        variable_units=units,
    )


def _longest_gap(present: Iterable[bool]) -> int:
    longest = current = 0
    for item in present:
        if item:
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def _uncertainty() -> dict[str, Any]:
    return {
        "status": "not_provided",
        "standard_uncertainty": None,
        "unit": None,
        "basis": "PCIC aggregate CSV supplies no per-value measurement uncertainty.",
    }


def _staleness(missing: bool) -> dict[str, Any]:
    return {
        "status": "unknown" if missing else "not_applicable",
        "age_seconds": None,
        "threshold_seconds": None,
        "basis": "Immutable historical snapshot; no current-condition freshness claim.",
    }


class PCICStationProvider:
    """Normalize the selected independent PCIC/source-provider history."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        snapshot: PCICSnapshot,
        *,
        target_longitude_deg: float,
        target_latitude_deg: float,
        target_elevation_m: float,
    ) -> None:
        self.snapshot = snapshot
        self.target_longitude_deg = target_longitude_deg
        self.target_latitude_deg = target_latitude_deg
        self.target_elevation_m = target_elevation_m

    def quality_report(self, request: ConditionRequest) -> dict[str, Any]:
        parsed = parse_pcic_observations(self.snapshot)
        timeline = _timeline(request.valid_start_utc, request.valid_end_utc)
        candidates = default_pcic_candidate_audit(
            target_longitude_deg=self.target_longitude_deg,
            target_latitude_deg=self.target_latitude_deg,
            target_elevation_m=self.target_elevation_m,
        )
        selected_candidate = select_pcic_candidate(candidates)
        if (
            selected_candidate.network != NETWORK_NAME
            or selected_candidate.station_id != PCIC_STATION_ID
            or selected_candidate.history_id != str(PCIC_HISTORY_ID)
        ):
            raise PCICProviderError("Cached observations do not match deterministic station selection.")
        coverage: dict[str, Any] = {}
        for variable in CANONICAL_UNITS:
            present = [
                parsed.records.get(timestamp, {}).get(variable) is not None
                if variable in RAW_FIELDS
                else False
                for timestamp in timeline
            ]
            coverage[variable] = {
                "available_hours": sum(present),
                "missing_hours": len(timeline) - sum(present),
                "missing_fraction": 1.0 - sum(present) / len(timeline),
                "longest_gap_hours": _longest_gap(present),
            }
        return {
            "schema": QUALITY_SCHEMA,
            "snapshot_id": self.snapshot.snapshot_id,
            "valid_start_utc": request.valid_start_utc.isoformat(),
            "valid_end_utc": request.valid_end_utc.isoformat(),
            "station_selection": {
                "schema": SELECTION_SCHEMA,
                "ordering": (
                    "independent original organization/history; redistributable licence; overlap "
                    "hours; comparable variables; QC/revision information; elevation difference; "
                    "horizontal distance; network/station/history tie-breakers"
                ),
                "selected_network": selected_candidate.network,
                "selected_station_id": selected_candidate.station_id,
                "selected_history_id": selected_candidate.history_id,
                "candidates": [_candidate_as_report(candidate) for candidate in candidates],
            },
            "selected": {
                "pcic_network": NETWORK_NAME,
                "pcic_station_id": PCIC_STATION_ID,
                "pcic_internal_station_id": PCIC_INTERNAL_STATION_ID,
                "pcic_history_id": PCIC_HISTORY_ID,
                "original_station_id": ORIGINAL_STATION_ID,
                "original_organization": ORIGINAL_SOURCE_ORGANIZATION,
                "horizontal_distance_km": _haversine_km(
                    self.target_longitude_deg,
                    self.target_latitude_deg,
                    STATION_LONGITUDE_DEG,
                    STATION_LATITUDE_DEG,
                ),
                "elevation_difference_m": STATION_ELEVATION_M - self.target_elevation_m,
            },
            "forcing_coverage": coverage,
            "provider_qc": self.snapshot.manifest["provider_qc"],
            "revision": self.snapshot.manifest["revision"],
            "exact_duplicate_rows": parsed.exact_duplicate_rows,
            "exact_duplicate_values": parsed.exact_duplicate_values,
            "calm_wind_direction_masks": parsed.calm_direction_masks,
            "gap_fill_fraction": 0.0,
            "gap_policy": "No temporal or cross-station filling; every absent hour remains masked.",
            "representativeness": (
                "Elkford is about 44 km from Mount Hosmer and 1164 m below the reference "
                "elevation. Valley/community wind cannot establish ridge or terrain-scale wind."
            ),
        }

    def normalize(self, request: ConditionRequest) -> ConditionPackDraft:
        parsed = parse_pcic_observations(self.snapshot)
        timeline = _timeline(request.valid_start_utc, request.valid_end_utc)
        code_hash = _sha256_bytes(Path(__file__).read_bytes())
        transformations = (
            {
                "transformation_id": "pcic-units-v1",
                "method": "Explicit PCIC source-unit conversion to ConditionPack canonical units.",
                "version": "1",
                "code_sha256": code_hash,
                "parameters": {
                    "wind_speed": "m/s to m s-1 (identity)",
                    "wind_direction": "degree to meteorological degree_true",
                    "calm_direction": "mask direction when source wind speed is exactly zero",
                    "temperature": "celsius to K when present; no elevation correction",
                },
            },
        )
        station_id = f"pcic-{NETWORK_NAME.casefold()}-{PCIC_STATION_ID}-h{PCIC_HISTORY_ID}"
        variables: dict[str, Any] = {}
        for variable in CANONICAL_UNITS:
            values: list[dict[str, Any]] = []
            for timestamp in timeline:
                raw_row = parsed.raw_rows.get(timestamp)
                raw = parsed.records.get(timestamp, {}).get(variable)
                source_unit: str | None = None
                source_fields: tuple[str, ...] = ()
                if variable in RAW_FIELDS:
                    source_fields = tuple(field for field, _unit in RAW_FIELDS[variable])
                    for field, unit in RAW_FIELDS[variable]:
                        if raw_row is not None and _finite_or_none(raw_row.get(field, ""), field) is not None:
                            source_unit = unit
                            break
                if raw is None or source_unit is None:
                    value = None
                    transformation_ids: tuple[str, ...] = ()
                else:
                    normalized_unit = {
                        "m/s": "m s-1",
                        "degree": "degree_true",
                        "celsius": "degC",
                        "%": "%",
                    }[source_unit]
                    value = convert_value(variable, raw, normalized_unit)
                    transformation_ids = ("pcic-units-v1",)
                missing = value is None
                row_digest = (
                    _sha256_bytes(_canonical_json_bytes(raw_row))[:16]
                    if raw_row is not None
                    else "absent"
                )
                qc_flags: list[dict[str, str]] = []
                if (
                    variable == "wind_direction"
                    and raw_row is not None
                    and _finite_or_none(raw_row.get("WSPD_SCLR", ""), "WSPD_SCLR") == 0
                ):
                    qc_flags.append(
                        {
                            "code": "CALM",
                            "severity": "rejected",
                            "source": "PCIC WSPD_SCLR",
                            "meaning": "Wind direction is undefined for calm wind and is masked.",
                        }
                    )
                values.append(
                    {
                        "station_id": station_id,
                        "time_utc": timestamp,
                        "value": value,
                        "masked": missing,
                        "status": "missing" if missing else "observed",
                        "qc_flags": qc_flags,
                        "uncertainty": _uncertainty(),
                        "staleness": _staleness(missing),
                        "lineage": (
                            {
                                "source_file_sha256": self.snapshot.observations_sha256,
                                "source_record_id": (
                                    f"{NETWORK_NAME}/{PCIC_STATION_ID}/{PCIC_HISTORY_ID}:"
                                    f"{timestamp.isoformat()}:{row_digest}"
                                ),
                                "transformation_ids": transformation_ids,
                            },
                        ),
                    }
                )
            if variable in RAW_FIELDS:
                provenance = {
                    "kind": "direct",
                    "method": (
                        "PCIC aggregate station value normalized without temporal filling, spatial "
                        "transfer, or local correction. Conflicting duplicate source fields are rejected."
                    ),
                    "version": "1",
                    "source_variables": source_fields,
                    "citation": PORTAL_DOCS_URI,
                    "assumptions": (
                        "The station-variable API's Z timestamps establish the aggregate CSV clock as UTC.",
                        "Community/valley observations are not Mount Hosmer terrain truth.",
                    ),
                }
            else:
                provenance = {
                    "kind": "derived",
                    "method": "Selected PCIC history does not supply this variable; preserve as missing.",
                    "version": "1",
                    "source_variables": ("PCIC selected-history observation record",),
                    "citation": PORTAL_DOCS_URI,
                    "assumptions": ("No derivation, merging, or zero substitution is permitted.",),
                }
            variables[variable] = {
                "variable": variable,
                "unit": CANONICAL_UNITS[variable],
                "provenance": provenance,
                "values": values,
            }
        acquired_start = _parse_manifest_utc(
            self.snapshot.manifest["acquisition_start_utc"], "acquisition_start_utc"
        )
        acquired_end = _parse_manifest_utc(
            self.snapshot.manifest["acquisition_end_utc"], "acquisition_end_utc"
        )
        source_files = tuple(
            {
                "source_file_id": f"pcic-{Path(name).stem.replace('_', '-').replace('.', '-')}",
                "locator": f"source-cache://pcic/{self.snapshot.snapshot_id}/{name}",
                "sha256": record["sha256"],
                "bytes": record["bytes"],
                "media_type": record["media_type"],
            }
            for name, record in sorted(
                self.snapshot.manifest["files"].items(),
                key=lambda item: f"pcic-{Path(item[0]).stem.replace('_', '-').replace('.', '-')}",
            )
        )
        return ConditionPackDraft.model_validate(
            {
                "schema_version": CONDITION_PACK_SCHEMA_VERSION,
                "mountain_grid": request.mountain_grid.model_dump(mode="json"),
                "source": {
                    "provider_id": PROVIDER_ID,
                    "title": "PCIC PCDS ENV-AQN historical station observations",
                    "citation": (
                        f"PCIC PCDS {NETWORK_NAME} station {PCIC_STATION_ID}, history "
                        f"{PCIC_HISTORY_ID}; original observations by {ORIGINAL_SOURCE_ORGANIZATION}. "
                        + OGL_ATTRIBUTION
                    ),
                    "source_uri": SOURCE_URLS[OBSERVATIONS_FILENAME],
                    "licence": f"{OGL_NAME} {OGL_VERSION}",
                    "licence_uri": OGL_URI,
                    "permitted_use": (
                        "Copy, modify, publish, translate, adapt, distribute, or otherwise use for "
                        "a lawful purpose with source acknowledgement and no implied endorsement."
                    ),
                },
                "times": {
                    "acquisition_start_utc": acquired_start,
                    "acquisition_end_utc": acquired_end,
                    "publication_time_utc": acquired_end,
                    "valid_start_utc": request.valid_start_utc,
                    "valid_end_utc": request.valid_end_utc,
                    "staleness_reference_time_utc": acquired_end,
                    "cadence_seconds": 3600,
                },
                "stations": (
                    {
                        "station_id": station_id,
                        "name": ORIGINAL_STATION_NAME,
                        "longitude_deg": STATION_LONGITUDE_DEG,
                        "latitude_deg": STATION_LATITUDE_DEG,
                        "elevation_m": STATION_ELEVATION_M,
                        "coordinate_source": (
                            f"PCIC history {PCIC_HISTORY_ID} coordinates; elevation from original "
                            f"B.C. station {ORIGINAL_STATION_ID} metadata. Coordinate order is lon,lat."
                        ),
                        "horizontal_uncertainty_m": None,
                        "elevation_uncertainty_m": None,
                    },
                ),
                "source_files": source_files,
                "transformations": transformations,
                "normalization": {
                    "software": "mount-hosmer-pcic-pcds-normalizer",
                    "software_version": "1",
                    "method": "Strict cache-native UTC-hour normalization without gap filling.",
                    "code_sha256": code_hash,
                },
                "variables": variables,
                "limitations": (
                    "Experimental historical disagreement assessment, not an operational avalanche "
                    "forecast; never replaces Avalanche Canada guidance or field assessment.",
                    "Scores derived later remain relative indices, not probabilities.",
                    "Only wind speed and wind direction overlap; radiation, snow depth/SWE, "
                    "precipitation, temperature, humidity, and pressure are unavailable here.",
                    "The source is unverified and preliminary and may be revised; this immutable "
                    "snapshot is one captured revision with no per-observation PCIC QC fields.",
                    "No gap filling, station merging, terrain correction, elevation correction, "
                    "or local correction is applied.",
                    "The community station is horizontally and vertically separated from Mount "
                    "Hosmer and cannot establish terrain-scale representativeness.",
                ),
            }
        )


def _station_meta(pack: ConditionPack) -> dict[str, Any]:
    if len(pack.stations) != 1:
        raise PCICProviderError("Provider comparison requires exactly one station per ConditionPack.")
    station = pack.stations[0]
    return {
        "station_id": station.station_id,
        "name": station.name,
        "longitude_deg": station.longitude_deg,
        "latitude_deg": station.latitude_deg,
        "elevation_m": station.elevation_m,
    }


def _series_by_time(pack: ConditionPack, variable: str) -> dict[datetime, Any]:
    return {item.time_utc: item for item in pack.variables[variable].values}


def compare_pcic_to_eccc(
    pcic_pack: ConditionPack,
    eccc_pack: ConditionPack,
    *,
    target_longitude_deg: float,
    target_latitude_deg: float,
    target_elevation_m: float,
    eccc_original_organization: str,
) -> dict[str, Any]:
    """Compare only genuinely overlapping values; never merge the providers."""

    if pcic_pack.source.provider_id != PROVIDER_ID:
        raise PCICProviderError("PCIC comparison pack has the wrong provider identity.")
    if eccc_pack.source.provider_id != "eccc-historical-hourly":
        raise PCICProviderError("ECCC comparison pack has the wrong provider identity.")
    pcic_station = _station_meta(pcic_pack)
    eccc_station = _station_meta(eccc_pack)
    comparable = ("wind_direction", "wind_speed")
    variables: dict[str, Any] = {}
    for variable in comparable:
        pcic_values = _series_by_time(pcic_pack, variable)
        eccc_values = _series_by_time(eccc_pack, variable)
        timeline = sorted(set(pcic_values) | set(eccc_values))
        diffs: list[float] = []
        pcic_missing = eccc_missing = pcic_qc_excluded = eccc_qc_excluded = 0
        pcic_suspect = eccc_suspect = 0
        for timestamp in timeline:
            left = eccc_values.get(timestamp)
            right = pcic_values.get(timestamp)
            if left is None or left.masked or left.value is None:
                eccc_missing += 1
            if right is None or right.masked or right.value is None:
                pcic_missing += 1
            left_rejected = left is not None and any(
                flag.severity == "rejected" for flag in left.qc_flags
            )
            right_rejected = right is not None and any(
                flag.severity == "rejected" for flag in right.qc_flags
            )
            if left_rejected:
                eccc_qc_excluded += 1
            if right_rejected:
                pcic_qc_excluded += 1
            if left is not None:
                eccc_suspect += sum(flag.severity == "suspect" for flag in left.qc_flags)
            if right is not None:
                pcic_suspect += sum(flag.severity == "suspect" for flag in right.qc_flags)
            if (
                left is None
                or right is None
                or left.masked
                or right.masked
                or left.value is None
                or right.value is None
                or left_rejected
                or right_rejected
            ):
                continue
            diff = float(left.value) - float(right.value)
            if variable == "wind_direction":
                diff = (diff + 180.0) % 360.0 - 180.0
            diffs.append(diff)
        if variable == "wind_direction" and diffs:
            bias = math.degrees(
                math.atan2(
                    fmean(math.sin(math.radians(value)) for value in diffs),
                    fmean(math.cos(math.radians(value)) for value in diffs),
                )
            )
        else:
            bias = fmean(diffs) if diffs else None
        variables[variable] = {
            "pcic": {
                **pcic_station,
                "history_id": PCIC_HISTORY_ID,
                "network": NETWORK_NAME,
                "original_organization": ORIGINAL_SOURCE_ORGANIZATION,
                "source_unit": "degree" if variable == "wind_direction" else "m/s",
                "canonical_unit": CANONICAL_UNITS[variable],
                "missing_count": pcic_missing,
                "qc_exclusion_count": pcic_qc_excluded,
                "suspect_flag_count_retained": pcic_suspect,
                "horizontal_distance_km": _haversine_km(
                    target_longitude_deg,
                    target_latitude_deg,
                    pcic_station["longitude_deg"],
                    pcic_station["latitude_deg"],
                ),
                "elevation_difference_m": pcic_station["elevation_m"] - target_elevation_m,
            },
            "eccc": {
                **eccc_station,
                "history_id": "ECCC climate identifier 1157631 snapshot record",
                "network": "ECCC climate-hourly",
                "original_organization": eccc_original_organization,
                "source_unit": "10 degree_true" if variable == "wind_direction" else "km h-1",
                "canonical_unit": CANONICAL_UNITS[variable],
                "missing_count": eccc_missing,
                "qc_exclusion_count": eccc_qc_excluded,
                "suspect_flag_count_retained": eccc_suspect,
                "horizontal_distance_km": _haversine_km(
                    target_longitude_deg,
                    target_latitude_deg,
                    eccc_station["longitude_deg"],
                    eccc_station["latitude_deg"],
                ),
                "elevation_difference_m": eccc_station["elevation_m"] - target_elevation_m,
            },
            "overlap_count": len(diffs),
            "eccc_minus_pcic_bias": bias,
            "mae": fmean(abs(value) for value in diffs) if diffs else None,
            "rmse": math.sqrt(fmean(value * value for value in diffs)) if diffs else None,
            "metric_method": (
                "Shortest signed angular difference and circular-mean bias."
                if variable == "wind_direction"
                else "Arithmetic paired differences at exact UTC hours."
            ),
            "representativeness": (
                "Station disagreement only. Neither source is Mount Hosmer field truth; the PCIC "
                "history is a community/valley station and the ECCC source is also below the "
                "2496.78 m reference elevation."
            ),
        }
    return {
        "schema": COMPARISON_SCHEMA,
        "pcic_condition_id": pcic_pack.condition_id,
        "eccc_condition_id": eccc_pack.condition_id,
        "valid_start_utc": max(
            pcic_pack.times.valid_start_utc, eccc_pack.times.valid_start_utc
        ).isoformat(),
        "valid_end_utc": min(
            pcic_pack.times.valid_end_utc, eccc_pack.times.valid_end_utc
        ).isoformat(),
        "variables": variables,
        "excluded_variables": {
            "air_temperature": "Selected PCIC history has no winter temperature values.",
            "relative_humidity": "Selected PCIC history does not expose relative humidity.",
            "precipitation_amount": (
                "No comparable PCIC hourly precipitation; cumulative precipitation is not "
                "transformed or compared."
            ),
            "surface_pressure": "Selected PCIC history does not expose pressure.",
            "shortwave_radiation": "Unavailable.",
            "longwave_radiation": "Unavailable.",
            "snow_depth_swe": "No eligible selected-history snow-depth or SWE series.",
        },
        "claim_boundary": (
            "Independent source-provider disagreement characterization only; not validation, "
            "calibration, probability, current conditions, or evidence of improved accuracy."
        ),
    }


def write_json_report(report: Mapping[str, Any], output_path: str | Path) -> Path:
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_json_bytes(dict(report))
    if target.exists() and target.read_bytes() != content:
        raise PCICProviderError(f"Refusing to replace a different report: {target}")
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, target)
    return target
