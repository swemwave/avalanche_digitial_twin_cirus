"""Build the public-data field-validation candidate funnel without predictions.

The script queries only the public RegObs search API, freezes the exact response
bytes outside ``DATA/`` and ``runtime/``, and writes a metadata-only candidate
inventory. It neither imports model code nor opens a final holdout target.

The default cache is intentionally immutable. To make a genuinely new public
acquisition, provide a new ``--cache-root`` and preserve the old acquisition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REGOBS_SEARCH_URL = "https://api.regobs.no/v5/Search"
REGOBS_SWAGGER_URL = "https://api.regobs.no/v5/swagger/docs/V5"
REGOBS_SEARCH_CRITERIA_URL = "https://api.regobs.no/v5/Search/SearchCriteria/10/2"
REGOBS_TERMS_URL = (
    "https://www.varsom.no/en/about/regobs/"
    "regobs-about-data-terms-of-service-and-privacy-policy/"
)
NVE_TERRAIN_CANDIDATE_URL = "https://hoydedata.no/LaserInnsyn2/"
AVAFRAME_DATA_DOI = "https://doi.org/10.5281/zenodo.20701552"
AVAFRAME_DATA_SHA256 = (
    "487f390d01d0c38bdb972848a5c3d76d91a4203b2c8738165b163e6b9174bef0"
)
AVAFRAME_DATA_BYTES = 13_416_107
AVAFRAME_DATA_COMMIT = "fa839c"
EXPECTED_REGOBS_CANDIDATES = 40

SEARCH_QUERY: dict[str, Any] = {
    "SelectedGeoHazards": [10],
    "SelectedRegistrationTypes": [{"Id": 81, "SubTypes": [26]}],
    "ObserverCompetence": [120, 130, 150],
    "NumberOfRecords": 2000,
    "Offset": 0,
    "LangKey": 2,
    "OrderBy": "DtObsTime",
    "AscendingOrder": False,
    "ToDtObsTime": "2026-08-13T23:59:59Z",
}

PUBLIC_SOURCE_AUDIT: tuple[dict[str, Any], ...] = (
    {
        "organization": "WSL Institute for Snow and Avalanche Research SLF / EnviDat",
        "official_urls": [
            "https://www.envidat.ch/metadata/comparing-human-forecasts-with-model-predictions",
            "https://doi.org/10.16904/envidat.535",
        ],
        "version": "1.0; metadata modified 2026-04-27",
        "licence": "CC BY-SA 4.0",
        "public_evidence": "Dry natural- and human-triggered-avalanche CSV resources for 2022/23 and 2023/24 provide dates and Swiss-coordinate points. The same package separately publishes model-prediction resources.",
        "contract_assessment": "Discovery-only: no release/deposit polygons, component attribution, survey/detection mask, feature uncertainty, release thickness/density, or event-surface terrain lineage.",
        "anti_leakage_action": "Do not acquire or open either model-prediction resource when screening the observation CSVs for a future cohort.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "The current 46-record funnel is already frozen and this point-only package cannot satisfy Profile C as published.",
        "external_commitment_required": False,
    },
    {
        "organization": "Norwegian Geotechnical Institute (NGI)",
        "official_urls": ["https://www.ngi.no/en/projects/avalanche-research/"],
        "version": "official page audited 2026-08-13",
        "licence": None,
        "public_evidence": "The official research page describes Ryggfonn full-scale observations and directs users to publications, but exposes no versioned machine-readable event package with release/deposit targets.",
        "contract_assessment": "No public Profile R, C, or E package identified.",
        "anti_leakage_action": "None; no target was acquired.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "No public event-data download or reuse licence was identified on the official page.",
        "external_commitment_required": True,
    },
    {
        "organization": "Norwegian Water Resources and Energy Directorate (NVE)",
        "official_urls": [
            "https://api.nve.no/doc/regobs/",
            "https://data.norge.no/nn/datasets/c516a557-9bfa-343c-8ba3-87389c6d7aea/skredhendelser",
            "https://publikasjoner.nve.no/rapport/2021/rapport2021_25.pdf",
        ],
        "version": "RegObs API v5; public-source audit 2026-08-13",
        "licence": "NLOD for NVE service data; individual RegObs attribution requirements apply",
        "public_evidence": "RegObs and the national avalanche-event database provide event discovery. NVE's Sentinel-1 service documentation states that detections describe debris, not release area/track, have 20 m pixels and temporal uncertainty commonly around six days, and perform best for wet slabs.",
        "contract_assessment": "RegObs supplies the 40 Norwegian candidates in this funnel, but neither the registry nor radar detections automatically supply component-specific quantitative geometry under v3.",
        "anti_leakage_action": "Treat all non-reported areas as unlabelled and keep radar detections qualitative unless feature-level method, independence, coverage, and uncertainty are established.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "The RegObs response was acquired separately and hashed; no additional NVE target package met the component contract.",
        "external_commitment_required": False,
    },
    {
        "organization": "Parks Canada",
        "official_urls": [
            "https://parks.canada.ca/pn-np/mtn/securiteenmontagne-mountainsafety/avalanche/routes-highways",
            "https://parks.canada.ca/pn-np/bc/glacier/nature/controle-avalanche-control/fact",
        ],
        "version": "official pages audited 2026-08-13",
        "licence": None,
        "public_evidence": "Official pages describe 135 Rogers Pass avalanche paths, control operations, and daily weather/snowpack/path observations, but no event-level release/deposit reference package or reuse licence was found.",
        "contract_assessment": "No public Profile R, C, or E package identified.",
        "anti_leakage_action": "None; no target was acquired.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "The audited public pages are program descriptions, not downloadable event targets.",
        "external_commitment_required": True,
    },
    {
        "organization": "Colorado Avalanche Information Center (CAIC)",
        "official_urls": [
            "https://prod.avalanche.state.co.us/accidents/statistics-and-reporting",
            "https://prod.avalanche.state.co.us/accidents/colorado",
        ],
        "version": "public Excel labelled CAIC_Accident_Data_Nov_2024.xlsx; official pages audited 2026-08-13",
        "licence": "No explicit reuse licence on the audited download page; CAIC citation requested",
        "public_evidence": "The official database and downloadable workbook characterize reported accidents and final investigations; CAIC explicitly states that most non-fatal incidents are not reported.",
        "contract_assessment": "Useful event discovery and narrative evidence, but no complete-search negatives or consistent release/deposit geometry, component attribution, and boundary uncertainty.",
        "anti_leakage_action": "Never infer avalanche absence from missing accident reports.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "The public workbook cannot satisfy Profile C geometry and uncertainty requirements as published and its reuse licence needs clarification before redistribution.",
        "external_commitment_required": False,
    },
    {
        "organization": "Avalanche Canada",
        "official_urls": [
            "https://avalanche.ca/news/fatal-avalanche-incidents-database",
            "https://avalanche.ca/data-sharing-policy",
        ],
        "version": "fatal incident database announced 2025-12-12; sharing policy updated 2026-02-12",
        "licence": "Dataset-specific permission not established",
        "public_evidence": "The new national fatal-incident database offers public interactive search and expert reports. Batch research data may require a request, use disclosure, or signed agreement.",
        "contract_assessment": "Potential discovery source, but no openly licensed component-specific geometry package was identified.",
        "anti_leakage_action": "None; no target was acquired.",
        "downloaded": False,
        "sha256": None,
        "reason_not_downloaded": "Proceeding may require an account, data request, or agreement, which is outside current authorization.",
        "external_commitment_required": True,
    },
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
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
            raise ValueError(
                f"Immutable acquisition differs at {path}; choose a new cache root."
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _fetch(request: urllib.request.Request) -> tuple[bytes, dict[str, str]]:
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        selected_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower()
            in {"content-type", "date", "etag", "last-modified", "sunset"}
        }
    return body, selected_headers


def _acquire(cache_root: Path) -> tuple[bytes, bytes, dict[str, Any]]:
    query_bytes = _canonical_json(SEARCH_QUERY)
    query_path = cache_root / "regobs-search-request.json"
    swagger_path = cache_root / "regobs-swagger.json"
    response_path = cache_root / "regobs-search-response.json"
    metadata_path = cache_root / "http-metadata.json"
    _write_immutable(query_path, query_bytes)

    if swagger_path.exists() and response_path.exists() and metadata_path.exists():
        return (
            swagger_path.read_bytes(),
            response_path.read_bytes(),
            json.loads(metadata_path.read_text(encoding="utf-8")),
        )

    swagger_request = urllib.request.Request(
        REGOBS_SWAGGER_URL,
        headers={"User-Agent": "avycore-public-validation-candidate-funnel/1"},
    )
    search_request = urllib.request.Request(
        REGOBS_SEARCH_URL,
        data=query_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "avycore-public-validation-candidate-funnel/1",
        },
    )
    swagger_bytes, swagger_headers = _fetch(swagger_request)
    response_bytes, search_headers = _fetch(search_request)
    acquired_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "acquired_at_utc": acquired_at,
        "swagger": {"url": REGOBS_SWAGGER_URL, "headers": swagger_headers},
        "search": {"url": REGOBS_SEARCH_URL, "headers": search_headers},
    }
    _write_immutable(swagger_path, swagger_bytes)
    _write_immutable(response_path, response_bytes)
    _write_immutable(metadata_path, _canonical_json(metadata))
    return swagger_bytes, response_bytes, metadata


def _regobs_rejection_reasons(record: dict[str, Any]) -> list[str]:
    avalanche = record["AvalancheObs"]
    reasons = [
        "The provider event-time offset is preserved but has not yet been normalized and independently verified as UTC.",
        "The start/stop extent drawing method, interpreter independence, and feature-level horizontal uncertainty are not documented in the API record.",
        "No explicit complete-search or detection mask is supplied; areas without a report remain unlabelled, never negative.",
        "A stop extent is not independently attributed to the terminal dense-flow deposit or endpoint.",
        "No event-era terrain surface with CRS, units, vertical datum, epoch, and mismatch uncertainty has been acquired.",
        "Release density and runout-independent release-thickness evidence are absent.",
        "End-to-end release-model forcing inputs, uncertainties, and spatial representativeness are incomplete.",
        "Path and storm group identities have not been independently established for leakage-safe partitioning.",
    ]
    if avalanche.get("FractureHeight") is None:
        reasons.append("No crown/fracture-height observation is reported.")
    return reasons


def _regobs_candidate(record: dict[str, Any], response_sha256: str) -> dict[str, Any]:
    avalanche = record["AvalancheObs"]
    location = record.get("ObsLocation") or {}
    observer = record.get("Observer") or {}
    weather = record.get("WeatherObservation")
    record_hash = _sha256_bytes(_canonical_json(record))
    start_extent = avalanche.get("StartExtent") or []
    stop_extent = avalanche.get("StopExtent") or []
    candidate_id = f"regobs-{record['RegId']}"
    return {
        "candidate_id": candidate_id,
        "source_collection": "RegObs public API v5",
        "source_record_id": str(record["RegId"]),
        "source_record_url": f"https://api.regobs.no/v5/Registration/{record['RegId']}/2",
        "source_collection_url": REGOBS_SEARCH_URL,
        "source_terms_url": REGOBS_TERMS_URL,
        "licence": "Norwegian Licence for Open Government Data (NLOD) 2.0, compatible with CC BY 4.0; RegObs terms apply",
        "source_response_sha256": response_sha256,
        "source_record_canonical_sha256": record_hash,
        "partition": "candidate_screening",
        "development_only": False,
        "final_holdout_allowed": "not_assigned_and_not_yet_eligible",
        "holdout_target_accessed": False,
        "event_group_id": candidate_id,
        "path_group_id": None,
        "storm_group_id": None,
        "mountain_group_id": f"regobs-region-{location.get('ForecastRegionTID')}",
        "geographic_discovery": {
            "country": location.get("CountryName"),
            "forecast_region": location.get("ForecastRegionName"),
            "municipality": location.get("MunicipalName"),
            "observation_location_latitude": location.get("Latitude"),
            "observation_location_longitude": location.get("Longitude"),
            "coordinate_order": "latitude_longitude for the discovery point only",
            "role": "Candidate discovery only; not a release seed, endpoint target, or prediction input.",
        },
        "event_time": {
            "provider_earliest": avalanche.get("DtEarliestAvalancheTime"),
            "provider_latest": avalanche.get("DtAvalancheTime"),
            "provider_observation_time": record.get("DtObsTime"),
            "utc_start": None,
            "utc_end": None,
            "status": "provider offsets preserved; independent UTC normalization pending",
        },
        "regime": {
            "provider_type": avalanche.get("AvalancheName"),
            "contract_regime": "dry_dense_slab",
            "dense_flow_component_confirmed": False,
            "trigger": avalanche.get("AvalancheTriggerName"),
            "destructive_size": avalanche.get("DestructiveSizeName"),
        },
        "geometry_availability": {
            "release_extent_present": bool(start_extent),
            "release_extent_vertex_count": len(start_extent),
            "stop_extent_present": bool(stop_extent),
            "stop_extent_vertex_count": len(stop_extent),
            "start_point_present": (
                avalanche.get("StartLat") is not None
                and avalanche.get("StartLong") is not None
            ),
            "stop_point_present": (
                avalanche.get("StopLat") is not None
                and avalanche.get("StopLong") is not None
            ),
            "source_crs": "EPSG:4326",
            "extent_coordinate_order": "longitude_latitude",
            "canonical_units": "decimal degrees",
            "transformations": [],
            "target_coordinates_embedded_in_inventory": False,
        },
        "observation_method": {
            "observer_competence_id": observer.get("CompetenceLevelTID"),
            "observer_competence_label": observer.get("CompetenceLevelName"),
            "location_capture_method": location.get("UTMSourceName"),
            "extent_mapping_method": None,
            "independent_of_model": "not established",
            "blind_to_model_output": "not established",
            "annotation_protocol_sha256": None,
            "attachment_count": len(record.get("Attachments") or []),
        },
        "terrain_surface": {
            "status": "not acquired",
            "candidate_official_source": NVE_TERRAIN_CANDIDATE_URL,
            "candidate_source_licence": "CC BY 4.0",
            "crs": None,
            "horizontal_units": None,
            "vertical_units": None,
            "vertical_datum": None,
            "epoch": None,
            "event_surface_mismatch": None,
            "transformations": [],
        },
        "release_initial_condition_evidence": {
            "fracture_height_value": avalanche.get("FractureHeight"),
            "fracture_height_unit": "cm" if avalanche.get("FractureHeight") is not None else None,
            "fracture_height_role": "crown height only; not accepted as normal-to-slope release thickness",
            "release_thickness_m": None,
            "release_density_kg_m3": None,
            "frozen_without_runout_target": False,
        },
        "weather_and_snow_inputs": {
            "weather_observation_present": weather is not None,
            "snow_profile_present": record.get("SnowProfile2") is not None,
            "complete_release_model_inputs_present": False,
            "acquisition_interval": None,
            "units_verified": False,
            "uncertainty_documented": False,
            "spatial_representativeness_documented": False,
        },
        "coverage_semantics": {
            "label_state": "positive_unlabelled",
            "survey_footprint_present": False,
            "complete_search_claimed": False,
            "detection_mask_present": False,
            "detection_limitations_documented": False,
            "unreported_avalanches_are_negative": False,
        },
        "observation_uncertainty": {
            "observation_location_uncertainty_provider_value": location.get("Uncertainty"),
            "observation_location_uncertainty_unit": "m (provider field; applicability to extents not established)",
            "release_boundary_horizontal_uncertainty_m": None,
            "stop_boundary_horizontal_uncertainty_m": None,
            "confidence": None,
            "confidence_basis": None,
        },
        "eligibility_by_profile": {
            "R": "ineligible_as_published",
            "C": "ineligible_as_published",
            "E": "ineligible_as_published",
        },
        "quality_tier": "C",
        "rejection_reasons": _regobs_rejection_reasons(record),
    }


AVAFRAME_EVENTS: tuple[dict[str, Any], ...] = (
    {
        "name": "Arzl",
        "event_time": "README conflicts between January 2009 and January 2019",
        "geometry": "release polygon; no deposit GIS target",
        "release_thickness_m": 1.76,
        "release_thickness_basis": "dataset input; observation provenance still requires confirmation",
        "method": "AvaFrameData case files and README",
        "reasons": [
            "The event year conflicts within the supplied documentation.",
            "No deposit polygon or terminal dense-flow endpoint GIS target is supplied.",
            "Terrain epoch, vertical datum, coverage, and feature uncertainty are absent.",
        ],
    },
    {
        "name": "Eiskar",
        "event_time": "approximately 2019-01-15T01:00 local; mapped 2019-01-18",
        "geometry": "two release polygons and visual/DFA/PSA/maximum deposit polygons",
        "release_thickness_m": [2.7, 2.2],
        "release_thickness_basis": "estimated release depths documented by the dataset",
        "method": "field/remote deposit interpretations distributed in AvaFrameData",
        "reasons": [
            "Already viewed during development and therefore prohibited from the final holdout.",
            "Deposit alternatives lack complete-search masks, boundary uncertainty, confidence, and a frozen interpretation rule.",
            "Release density and event-surface terrain lineage are incomplete.",
        ],
    },
    {
        "name": "Filisur1",
        "event_time": "approximately 2012-02-23; exact interval unavailable",
        "geometry": "release and deposit polygons",
        "release_thickness_m": None,
        "release_thickness_basis": "1 m is a model assumption, not an observation",
        "method": "AvaFrameData case files",
        "reasons": [
            "Already viewed during development and therefore prohibited from the final holdout.",
            "Exact event time, observed release thickness/density, coverage, and uncertainty are absent.",
            "Available terrain is not established as the event surface.",
        ],
    },
    {
        "name": "Filisur2",
        "event_time": "approximately 2012-02-23; exact interval unavailable",
        "geometry": "release and deposit polygons",
        "release_thickness_m": None,
        "release_thickness_basis": "1 m is a model assumption, not an observation",
        "method": "AvaFrameData case files",
        "reasons": [
            "Already viewed during development and therefore prohibited from the final holdout.",
            "Exact event time, observed release thickness/density, coverage, and uncertainty are absent.",
            "Available terrain is not established as the event surface.",
        ],
    },
    {
        "name": "Kleiner_Oetscherbach",
        "event_time": "2009-02-25; exact interval unavailable",
        "geometry": "release polygon and whole-event outline; no distinct deposit/endpoint",
        "release_thickness_m": None,
        "release_thickness_basis": "not supplied",
        "method": "AvaFrameData case files",
        "reasons": [
            "Already viewed during development and therefore prohibited from the final holdout.",
            "No separately attributed terminal dense-flow deposit or endpoint is supplied.",
            "Release thickness, density, coverage, and uncertainty are absent.",
        ],
    },
    {
        "name": "Popeletzbach",
        "event_time": "2009-04-07; exact interval unavailable",
        "geometry": "release polygon, whole-event outline, and deposit polygon",
        "release_thickness_m": None,
        "release_thickness_basis": "not supplied",
        "method": "AvaFrameData case files",
        "reasons": [
            "Already viewed during development and therefore prohibited from the final holdout.",
            "Release thickness, density, volume, confidence, coverage, and uncertainty are absent.",
            "The deposit mapping method and terrain epoch require further primary-source evidence.",
        ],
    },
)


def _avaframe_candidate(event: dict[str, Any]) -> dict[str, Any]:
    name = event["name"]
    candidate_id = f"avaframedata-{name.lower().replace('_', '-')}"
    return {
        "candidate_id": candidate_id,
        "source_collection": "AvaFrameData v1.0",
        "source_record_id": name,
        "source_record_url": AVAFRAME_DATA_DOI,
        "source_collection_url": AVAFRAME_DATA_DOI,
        "source_terms_url": AVAFRAME_DATA_DOI,
        "licence": "CC BY 4.0",
        "source_archive_sha256": AVAFRAME_DATA_SHA256,
        "source_archive_bytes": AVAFRAME_DATA_BYTES,
        "source_repository_commit": AVAFRAME_DATA_COMMIT,
        "feature_file_hashes": None,
        "partition": "development",
        "development_only": True,
        "final_holdout_allowed": False,
        "holdout_target_accessed": False,
        "event_group_id": candidate_id,
        "path_group_id": f"avaframedata-path-{name.lower().replace('_', '-')}",
        "storm_group_id": None,
        "mountain_group_id": None,
        "event_time": {
            "provider_description": event["event_time"],
            "utc_start": None,
            "utc_end": None,
            "status": "exact UTC bounds not established",
        },
        "regime": {
            "provider_type": "dense snow avalanche case",
            "contract_regime": "dry_dense_slab",
            "dense_flow_component_confirmed": "requires event-specific source review",
            "trigger": None,
        },
        "geometry_availability": {
            "description": event["geometry"],
            "source_crs": "event-specific; preserved in archive but not normalized in this candidate inventory",
            "transformations": [],
            "target_coordinates_embedded_in_inventory": False,
        },
        "observation_method": {
            "description": event["method"],
            "independent_of_model": "not established for every feature",
            "blind_to_model_output": "not established",
            "annotation_protocol_sha256": None,
        },
        "terrain_surface": {
            "status": "available case terrain, but event epoch and mismatch are not fully documented",
            "crs": "event-specific",
            "horizontal_units": "m where documented by the case",
            "vertical_units": "m where documented by the case",
            "vertical_datum": None,
            "epoch": None,
            "event_surface_mismatch": None,
            "transformations": [],
        },
        "release_initial_condition_evidence": {
            "release_thickness_m": event["release_thickness_m"],
            "release_thickness_basis": event["release_thickness_basis"],
            "release_density_kg_m3": None,
            "frozen_without_runout_target": False,
        },
        "weather_and_snow_inputs": {
            "complete_release_model_inputs_present": False,
            "acquisition_interval": None,
            "units_verified": False,
            "uncertainty_documented": False,
            "spatial_representativeness_documented": False,
        },
        "coverage_semantics": {
            "label_state": "positive_unlabelled",
            "survey_footprint_present": False,
            "complete_search_claimed": False,
            "detection_mask_present": False,
            "detection_limitations_documented": False,
            "unreported_avalanches_are_negative": False,
        },
        "observation_uncertainty": {
            "release_boundary_horizontal_uncertainty_m": None,
            "deposit_boundary_horizontal_uncertainty_m": None,
            "endpoint_horizontal_uncertainty_m": None,
            "confidence": None,
            "confidence_basis": None,
        },
        "eligibility_by_profile": {
            "R": "development_evidence_only_not_contract_eligible",
            "C": "development_evidence_only_not_contract_eligible",
            "E": "development_evidence_only_not_contract_eligible",
        },
        "quality_tier": "C",
        "rejection_reasons": list(
            dict.fromkeys(
                [
                    "Already viewed during development and therefore prohibited from the final holdout.",
                    *event["reasons"],
                ]
            )
        ),
    }


def _validate_inventory(inventory: dict[str, Any]) -> None:
    candidates = inventory["candidates"]
    if not 40 <= len(candidates) <= 60:
        raise ValueError("Candidate funnel must contain 40-60 records.")
    ids = [candidate["candidate_id"] for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("Candidate IDs must be unique.")
    if inventory["predictions_generated"]:
        raise ValueError("Candidate discovery may not generate predictions.")
    if inventory["holdout_targets_accessed"]:
        raise ValueError("Candidate discovery may not access a holdout target.")
    for candidate in candidates:
        if candidate["coverage_semantics"]["unreported_avalanches_are_negative"]:
            raise ValueError("Unreported avalanches may never become negative labels.")
        if candidate["coverage_semantics"]["label_state"] != "positive_unlabelled":
            raise ValueError("This funnel contains only positive-unlabelled candidates.")
        if not candidate["rejection_reasons"]:
            raise ValueError("Every currently ineligible candidate needs exact reasons.")


def build_inventory(cache_root: Path, experiment_path: Path) -> dict[str, Any]:
    swagger_bytes, response_bytes, http_metadata = _acquire(cache_root)
    swagger = json.loads(swagger_bytes)
    records = json.loads(response_bytes)
    if not isinstance(records, list):
        raise ValueError("The RegObs search response is not a JSON array.")
    selected = [
        record
        for record in records
        if record.get("AvalancheObs")
        and record["AvalancheObs"].get("AvalancheName") == "Dry slab avalanche"
        and record["AvalancheObs"].get("StartExtent")
        and (
            record["AvalancheObs"].get("StopExtent")
            or (
                record["AvalancheObs"].get("StopLat") is not None
                and record["AvalancheObs"].get("StopLong") is not None
            )
        )
    ]
    if len(selected) != EXPECTED_REGOBS_CANDIDATES:
        raise ValueError(
            "The frozen selection was expected to yield "
            f"{EXPECTED_REGOBS_CANDIDATES} RegObs records, got {len(selected)}. "
            "Do not silently change the cohort."
        )

    response_sha256 = _sha256_bytes(response_bytes)
    candidates = [
        _regobs_candidate(record, response_sha256)
        for record in sorted(selected, key=lambda value: int(value["RegId"]), reverse=True)
    ]
    candidates.extend(_avaframe_candidate(event) for event in AVAFRAME_EVENTS)
    inventory = {
        "schema": "avycore-public-event-candidate-funnel-v1",
        "candidate_funnel_id": "public-event-candidates-v1",
        "experiment_id": "public-data-field-validation-v1",
        "experiment_spec_sha256": _sha256_file(experiment_path),
        "generated_at_utc": http_metadata["acquired_at_utc"],
        "stage": "candidate_discovery_and_metadata_gap_screening",
        "predictions_generated": False,
        "model_code_imported": False,
        "holdout_partition_assigned": False,
        "holdout_targets_accessed": False,
        "prototype_disclaimer": "Experimental research prototype only. It does not replace Avalanche Canada guidance or field assessment. Model scores are relative indices, not probabilities.",
        "selection_rule": {
            "regobs": "From the fixed 2,000-record response: AvalancheName is Dry slab avalanche, StartExtent is present, and StopExtent or a stop coordinate is present. Observer competence is one of ***, ****, or *****/forecaster through the fixed request. No model result was used.",
            "avaframedata": "Include the six previously inspected public cases only to expose development evidence and metadata gaps; they are permanently excluded from the final holdout.",
        },
        "source_acquisition": {
            "regobs": {
                "api_url": REGOBS_SEARCH_URL,
                "search_criteria_url": REGOBS_SEARCH_CRITERIA_URL,
                "swagger_url": REGOBS_SWAGGER_URL,
                "swagger_version": (swagger.get("info") or {}).get("version"),
                "terms_url": REGOBS_TERMS_URL,
                "licence": "NLOD 2.0, compatible with CC BY 4.0; RegObs terms apply",
                "request": SEARCH_QUERY,
                "request_sha256": _sha256_bytes(_canonical_json(SEARCH_QUERY)),
                "response_bytes": len(response_bytes),
                "response_sha256": response_sha256,
                "swagger_bytes": len(swagger_bytes),
                "swagger_sha256": _sha256_bytes(swagger_bytes),
                "http_metadata": http_metadata,
                "cache_location": cache_root.relative_to(REPOSITORY_ROOT).as_posix(),
                "cache_policy": "immutable and ignored by git; use a new root for a new acquisition",
            },
            "avaframedata": {
                "version": "1.0",
                "doi": AVAFRAME_DATA_DOI,
                "licence": "CC BY 4.0",
                "archive_bytes": AVAFRAME_DATA_BYTES,
                "archive_sha256": AVAFRAME_DATA_SHA256,
                "repository_commit": AVAFRAME_DATA_COMMIT,
                "acquisition_role": "previously inspected development evidence only",
            },
        },
        "public_source_audit": {
            "audited_at_utc": "2026-08-13T00:00:00Z",
            "primary_or_official_sources_only": True,
            "outreach_sent": False,
            "accounts_created": False,
            "special_terms_accepted": False,
            "sources": list(PUBLIC_SOURCE_AUDIT),
        },
        "counts": {
            "total": len(candidates),
            "regobs": len(selected),
            "avaframedata_development_only": len(AVAFRAME_EVENTS),
            "regobs_with_stop_extent": sum(
                bool(record["AvalancheObs"].get("StopExtent")) for record in selected
            ),
            "regobs_with_fracture_height": sum(
                record["AvalancheObs"].get("FractureHeight") is not None
                for record in selected
            ),
            "regobs_with_weather_observation": sum(
                record.get("WeatherObservation") is not None for record in selected
            ),
            "regobs_with_snow_profile": sum(
                record.get("SnowProfile2") is not None for record in selected
            ),
            "regobs_with_nonempty_snow_density": sum(
                bool((record.get("SnowProfile2") or {}).get("SnowDensity"))
                for record in selected
            ),
            "candidates_with_release_density_evidence": 0,
            "contract_eligible_R": 0,
            "contract_eligible_C": 0,
            "contract_eligible_E": 0,
            "final_holdout_assigned": 0,
        },
        "funnel_result": "Forty-six candidates were discovered. None supplies release-density evidence, none is currently contract-eligible, and none is a final holdout. Missing evidence remains explicit.",
        "candidates": candidates,
    }
    _validate_inventory(inventory)
    return inventory


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT / ".validation-cache" / "public-event-funnel-v1",
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
        "--output",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "validation-data"
            / "candidates"
            / "public-event-candidates-v1.json"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    experiment_path = args.experiment.resolve()
    output_path = args.output.resolve()
    for protected in (REPOSITORY_ROOT / "DATA", REPOSITORY_ROOT / "runtime"):
        try:
            cache_root.relative_to(protected.resolve())
        except ValueError:
            pass
        else:
            raise ValueError(f"Acquisition cache may not be written under {protected}.")
    inventory = build_inventory(cache_root, experiment_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {inventory['counts']['total']} candidates to {output_path}; "
        "predictions=0, holdout assignments=0."
    )


if __name__ == "__main__":
    main()
