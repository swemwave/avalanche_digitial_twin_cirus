"""Build the protocol-bound field-validation data-owner request package.

The package records the public-data stop decision and the exact original files
and metadata needed from data owners. It never assigns a partition, opens a
target, imports a model, or generates a prediction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SRC = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SRC) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SRC))

from avycore.validation.acquisition import (  # noqa: E402
    OWNER_DELIVERY_SCHEMA_VERSION,
    FieldValidationOwnerDelivery,
)


SCHEMA = "avycore-field-validation-owner-request-v1"
REQUEST_ID = "dry-dense-slab-field-validation-owner-request-v1"
FROZEN_AT_UTC = "2026-08-15T00:39:57Z"
CONTACTS_VERIFIED_DATE_UTC = "2026-08-15"
PROTOCOL_PATH = Path("validation-data/experiments/public-data-field-validation-v2.json")
STRICT_FUNNEL_PATH = Path("validation-data/candidates/public-event-strict-funnel-v5.json")
SOURCE_AUDIT_PATH = Path("validation-data/candidates/public-validation-source-audit-v2.json")


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


def _identity(path: Path) -> dict[str, Any]:
    resolved = REPOSITORY_ROOT / path
    payload = resolved.read_bytes()
    return {
        "path": path.as_posix(),
        "bytes": len(payload),
        "sha256": _sha256_bytes(payload),
    }


def _send_ready_message(
    *,
    owner: str,
    to: list[str],
    cc: list[str],
    candidate_scope: str,
    owner_specific_files: list[str],
    licence_terms: str,
) -> dict[str, Any]:
    file_lines = "\n".join(f"- {item}" for item in owner_specific_files)
    body = f"""Hello,

I am requesting original field-observation files from {owner} for a non-operational research validation of dry dense-slab avalanche runout. The prototype does not replace public avalanche guidance or field assessment, and its scores are relative indices, not probabilities. {candidate_scope} Please do not select events based on agreement with any model.

For every candidate event, please include the original owner files and a manifest conforming to field-validation-owner-delivery-v1.schema.json. The required files are: (1) the permission/licence record; (2) UTC event-time and dry dense-slab classification evidence; (3) pre-event snow-surface DEM plus CRS, horizontal/vertical datum realization, epoch and transformation record; (4) independently observed release geometry; (5) event-specific slope-normal release-thickness measurement; (6) event-specific release-density measurement; (7) component-attributed terminal dense-flow deposit polygon or endpoint; and (8) survey-coverage and detection/occlusion masks that distinguish observed negatives from unknown areas. Each physical observation needs its method and quantified uncertainty. Missing values must remain missing: please do not infer, substitute, back-calculate, or derive any required observation from a model.

Owner-specific originals requested:
{file_lines}

For every delivered file, please give the unchanged relative path, byte count, lowercase SHA-256, stable source URI, copyright holder, licence name/URI, permitted use, redistribution status, and the path/hash of the licence or written-permission record covering that file. {licence_terms} The requested permission should explicitly cover private research storage, verification, format-preserving normalization, independent human review, deterministic calibration/holdout evaluation, publication of derived aggregate metrics/figures, reproducibility archiving, and whether original bytes may be redistributed. If redistribution is not permitted, we will keep originals private and publish only permitted metadata, hashes, and derived results.

We need at least 12 eligible events after two independent human reviews, spanning at least six paths, two mountains, and three storm cycles; 24–40 candidates are preferred because incomplete events will be excluded. Every event in the verified delivery cohort will be adjudicated, including exclusions, so none can be silently selected or omitted. Reviewer identity-verification records will be hashed; AI output never counts as a review, and disagreements require a separately verified third human. Please provide owner-proposed path/mountain/storm grouping evidence but no calibration or holdout labels. Holdout observations will remain sealed during calibration.

Thank you."""
    return {
        "to": to,
        "cc": cc,
        "subject": "Research data request: dry dense-slab release/runout field cohort",
        "body": body,
    }


def build_request() -> dict[str, Any]:
    protocol = json.loads((REPOSITORY_ROOT / PROTOCOL_PATH).read_bytes())
    if protocol.get("schema") != "avycore-public-data-field-validation-experiment-v2":
        raise ValueError("The owner request must bind to field-validation protocol v2.")
    if protocol.get("predictions_generated") is not False:
        raise ValueError("Refusing to build an acquisition request after predictions exist.")
    if protocol.get("holdout_targets_accessed") is not False:
        raise ValueError("Refusing to build an acquisition request after holdout access.")

    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "request_id": REQUEST_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "status": "no_eligible_public_cohort_owner_request_ready",
        "prototype_disclaimer": protocol["prototype_disclaimer"],
        "claim_boundary": (
            "This is an acquisition and pre-ingestion package, not validation evidence. "
            "A returned delivery remains untrusted until independent scientific and code "
            "review, normalization, grouped split freeze, and the complete untouched holdout."
        ),
        "frozen_inputs": {
            "protocol": _identity(PROTOCOL_PATH),
            "strict_public_funnel": _identity(STRICT_FUNNEL_PATH),
            "public_source_audit": _identity(SOURCE_AUDIT_PATH),
        },
        "current_evidence_state": {
            "public_candidates_audited": 26,
            "new_source_payloads_downloaded": 0,
            "eligible_new_downloads": 0,
            "eligible_events": 0,
            "eligible_independent_paths": 0,
            "eligible_mountains": 0,
            "eligible_storm_cycles": 0,
            "calibration_holdout_partition_assigned": False,
            "holdout_targets_accessed": False,
            "predictions_generated": False,
            "field_metrics_run": False,
            "field_validation_available": False,
        },
        "cohort_request": {
            "avalanche_regime": "dry_dense_slab",
            "minimum_eligible_events": 12,
            "minimum_independent_paths": 6,
            "minimum_mountains": 2,
            "minimum_storm_cycles": 3,
            "candidate_target_before_attrition": "24-40",
            "split_seed_after_eligibility_review": 20260815,
            "grouping_rule": (
                "No path_id or storm_cycle_id may cross calibration and holdout; keep "
                "mountain groups together when event independence cannot be justified."
            ),
            "split_status": "not_permitted_until_the_complete_cohort_passes_review",
        },
        "delivery_contract": {
            "schema": OWNER_DELIVERY_SCHEMA_VERSION,
            "json_schema_path": (
                "validation-data/acquisition/field-validation-owner-delivery-v1.schema.json"
            ),
            "python_model": (
                "avycore.validation.acquisition.FieldValidationOwnerDelivery"
            ),
            "cohort_gate": "avycore.validation.acquisition.owner_delivery_cohort_gate",
            "important_limit": (
                "Structural acceptance permits independent review only; it does not make "
                "the data eligible, trusted, calibrated, or validated."
            ),
        },
        "eligibility_and_split_workflow": {
            "review_schema_path": (
                "validation-data/acquisition/field-validation-eligibility-review-v1.schema.json"
            ),
            "conflict_schema_path": (
                "validation-data/acquisition/field-validation-eligibility-conflict-v1.schema.json"
            ),
            "decision_schema_path": (
                "validation-data/acquisition/field-validation-eligibility-decision-v1.schema.json"
            ),
            "adjudication_command": (
                "python scripts/validation/adjudicate_field_validation_eligibility.py"
            ),
            "minimum_independent_human_reviews_per_event": 2,
            "ai_counts_as_independent_review": False,
            "group_split_preregistration_path": (
                "validation-data/acquisition/field-validation-group-split-preregistration-v1.json"
            ),
            "real_event_assignments_exposed": False,
            "predictions_authorized": False,
        },
        "required_per_event_delivery": [
            {
                "item": "licence_and_immutable_identity",
                "requirement": (
                    "Original bytes for every source file, byte count, lowercase SHA-256, "
                    "stable source URI, copyright holder, licence name/URI, explicit "
                    "permitted scientific use, redistribution status, and a path/hash "
                    "binding to the immutable licence or written-permission record."
                ),
            },
            {
                "item": "event_identity_and_time",
                "requirement": (
                    "Unique event, path, mountain, and storm-cycle IDs; dry dense-slab "
                    "classification evidence; grouping evidence; UTC start/end and "
                    "confidence evidence. All are direct owner observations or records, "
                    "not model output."
                ),
            },
            {
                "item": "event_surface_dem",
                "requirement": (
                    "Pre-event snow-surface DEM valid for the event, acquisition UTC, "
                    "original/delivery CRS, horizontal and vertical datum realization/epoch, "
                    "height type, metre units, axis order, immutable transformation-lineage "
                    "record, and quantified horizontal/vertical uncertainty."
                ),
            },
            {
                "item": "observed_release_geometry",
                "requirement": (
                    "Original independent release geometry, observation time and method, CRS "
                    "lineage, and quantified horizontal uncertainty; no model-derived geometry."
                ),
            },
            {
                "item": "normal_to_slope_release_thickness",
                "requirement": (
                    "Event-specific measurement explicitly normal to slope, with estimate, "
                    "lower/upper uncertainty bounds, confidence level, method, time, and source."
                ),
            },
            {
                "item": "release_density",
                "requirement": (
                    "Event-specific kg/m3 measurement with estimate, lower/upper uncertainty "
                    "bounds, confidence level, method, time, and source."
                ),
            },
            {
                "item": "terminal_dense_flow_observation",
                "requirement": (
                    "Original terminal deposit polygon or endpoint, explicit dense-flow and "
                    "terminal attribution, observation method/time, CRS lineage, and quantified "
                    "horizontal uncertainty."
                ),
            },
            {
                "item": "survey_coverage_and_detection",
                "requirement": (
                    "Coverage geometry plus detection/occlusion mask; explicit complete-search "
                    "method, mapping UTC, positional uncertainty, detection limit, units and "
                    "confidence; non-detections inside valid coverage are observed negatives, "
                    "while outside/masked cells remain unknown."
                ),
            },
            {
                "item": "direct_observation_declaration",
                "requirement": (
                    "Every required physical observation explicitly declares direct owner "
                    "origin, availability of the original measurement, measurement-preserving "
                    "processing, independence from prediction, and false for missing-value "
                    "supply, inference, substitution, and model derivation."
                ),
            },
        ],
        "forbidden_substitutions": [
            "Do not estimate missing release thickness from crown height without measured direction and uncertainty.",
            "Do not substitute a literature density prior for an event-specific owner measurement in this request.",
            "Do not back-calculate release state or friction from the observed runout.",
            "Do not treat a bare-earth DEM as an event snow surface without a quantitative, reviewed mismatch model.",
            "Do not treat an imagery footprint, mapped positives, or non-reporting as a known-absence survey mask.",
            "Do not digitize a paper figure when the original survey geometry exists.",
            "Do not assign calibration or holdout membership in an owner delivery.",
            "Do not replace an original licence/permission record with an informal citation or assumed public-data status.",
        ],
        "public_search_claim_boundary": (
            "The following primary URLs support discovery and owner-holdings triage only. "
            "No new source payload was downloaded or promoted to immutable validation "
            "evidence, and publication descriptions do not prove that the required event "
            "files, licence, masks, or uncertainties are publicly available."
        ),
        "authoritative_public_search_update": [
            {
                "source_id": "slf-sovilla-mass-balance-18-events",
                "provider": "WSL Institute for Snow and Avalanche Research SLF / ETH Zurich",
                "primary_urls": [
                    "https://doi.org/10.1029/2005JF000391",
                    "https://doi.org/10.3929/ethz-a-004784844",
                ],
                "public_evidence": (
                    "The paper describes 18 field-investigated avalanches and explicitly "
                    "defines slope-normal fracture depth, release density, release/deposit "
                    "mass, photogrammetry, and field measurements."
                ),
                "eligible_events_added": 0,
                "public_download_status": "no_complete_event_delivery_published",
                "blocking_fields": [
                    "original_event_geometry_and_hashes",
                    "event_surface_dem_with_datum_lineage",
                    "component_terminal_geometry",
                    "survey_coverage_and_detection_masks",
                    "feature_uncertainty",
                    "data_reuse_licence_for_underlying_measurements",
                ],
            },
            {
                "source_id": "inrae-lautaret-field-archive",
                "provider": "INRAE Lautaret avalanche test site",
                "primary_urls": [
                    "https://doi.org/10.1016/j.coldregions.2015.03.005",
                    "https://www.inrae.fr/en/reports/understanding-avalanche-risk/multi-faceted-research",
                ],
                "public_evidence": (
                    "The primary site description reports roughly 50 release operations, "
                    "mostly dry cold avalanches, two principal paths, snow-pit density, "
                    "pre/post laser scanning, photogrammetric fronts, and runout records."
                ),
                "eligible_events_added": 0,
                "public_download_status": "described_holdings_not_a_versioned_event_package",
                "blocking_fields": [
                    "versioned_event_inventory",
                    "original_release_and_terminal_geometry",
                    "event_specific_thickness_density_and_uncertainty",
                    "event_surface_dem_and_crs_datum_lineage",
                    "complete_search_detection_masks",
                    "reuse_permission_and_sha256",
                ],
            },
            {
                "source_id": "ngi-ryggfonn-field-archive",
                "provider": "Norwegian Geotechnical Institute Ryggfonn",
                "primary_urls": [
                    "https://doi.org/10.1016/j.coldregions.2016.02.009"
                ],
                "public_evidence": (
                    "The primary summary describes four decades of full-scale avalanche "
                    "observations, including runout, velocity, impact and mass-balance data."
                ),
                "eligible_events_added": 0,
                "public_download_status": "summary_public_source_event_files_not_publicly_complete",
                "blocking_fields": [
                    "original_event_geometries",
                    "event_release_state_and_uncertainty",
                    "event_surface_dem",
                    "coverage_masks",
                    "licence_and_immutable_hashes",
                    "multi_path_cohort_diversity",
                ],
            },
            {
                "source_id": "seehore-field-archive",
                "provider": "University of Turin / Polytechnic University of Turin / Aosta Valley partners",
                "primary_urls": [
                    "https://doi.org/10.1016/j.coldregions.2012.09.006",
                    "https://doi.org/10.3390/geosciences9110471",
                ],
                "public_evidence": (
                    "The site papers describe original release/deposit surveys, snow "
                    "properties, GPS, laser scanning, imagery, and five reported impact events."
                ),
                "eligible_events_added": 0,
                "public_download_status": "papers_and_supplements_not_a_complete_owner_delivery",
                "blocking_fields": [
                    "at_least_12_eligible_events",
                    "original_gis_and_surface_files",
                    "release_thickness_density_uncertainty",
                    "terminal_component_attribution",
                    "coverage_detection_masks",
                    "event_file_licence_and_hashes",
                ],
            },
        ],
        "owner_routes": [
            {
                "owner_id": "wsl-slf",
                "contacts": ["data@slf.ch"],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://www.slf.ch/fr/services-et-produits/service-de-donnees-du-slf/",
                        "basis": (
                            "Official SLF data-service page names data@slf.ch for data "
                            "delivery questions and states the service's CC BY 4.0 terms."
                        ),
                    }
                ],
                "candidate_request": (
                    "All retainable dry dense-slab source events behind the Sovilla Monte "
                    "Pizzac 18-event mass-balance study, GEODAR archive, and later Vallée de "
                    "la Sionne records; "
                    "preferably 24 or more candidates before QA attrition."
                ),
                "specific_originals": [
                    "Event-level photogrammetric and field release/deposit geometry rather than paper figures",
                    "Slope-normal crown/fracture-depth and release-density measurement sheets with uncertainty",
                    "Pre-event snow surfaces and post-event surveys with CRS/datum/calibration lineage",
                    "Survey footprints, occlusion/detection masks, terminal dense-flow attribution, and timing",
                ],
                "send_ready_message": _send_ready_message(
                    owner="WSL Institute for Snow and Avalanche Research SLF",
                    to=["data@slf.ch"],
                    cc=[],
                    candidate_scope=(
                        "Please provide all retainable candidate events behind the Sovilla "
                        "Monte Pizzac 18-event mass-balance study and compatible Vallée de "
                        "la Sionne/GEODAR campaigns, preferably at least 24 candidates."
                    ),
                    owner_specific_files=[
                        "original event-level Monte Pizzac and Vallée de la Sionne photogrammetric/field release and deposit geometries, not digitized figures",
                        "slope-normal crown/fracture-depth and release-density measurement sheets with sampling layout and uncertainty",
                        "pre-event snow surfaces, post-event surveys, control points, calibration and coordinate-operation records",
                        "survey footprints, occlusion/detection masks, terminal dense-flow attribution, event clocks and any clock-alignment residuals",
                    ],
                    licence_terms=(
                        "The official SLF data-service page states CC BY 4.0 for its service "
                        "data; please confirm in writing whether that licence covers every "
                        "underlying file in this special delivery and provide any additional "
                        "or different terms file-by-file."
                    ),
                ),
            },
            {
                "owner_id": "inrae-lautaret",
                "contacts": ["florence.naaim@inrae.fr", "herve.bellot@inrae.fr"],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://monitoring-stations.ara.inrae.fr/",
                        "basis": (
                            "Official INRAE monitoring portal directs snow-site data "
                            "requests to Florence Naaim and Herve Bellot."
                        ),
                    },
                    {
                        "url": "https://www.inrae.fr/en/reports/understanding-avalanche-risk/multi-faceted-research",
                        "basis": "Official INRAE description identifies the Lautaret avalanche archive and measurements.",
                    },
                ],
                "candidate_request": (
                    "All dry dense-slab events with paired snow pits, laser scans, release "
                    "volume/geometry, terminal runout, and photogrammetry across the site's paths; "
                    "preferably 24 or more candidates before QA attrition."
                ),
                "specific_originals": [
                    "Raw and processed pre/post event laser scans plus transformation/control records",
                    "Original release and terminal/deposit mappings with survey coverage",
                    "Event snow-pit thickness/density measurements and their uncertainty",
                    "Exact UTC release records, path grouping, licence, and immutable file inventory",
                ],
                "send_ready_message": _send_ready_message(
                    owner="INRAE/IGE Lautaret avalanche test site",
                    to=["florence.naaim@inrae.fr", "herve.bellot@inrae.fr"],
                    cc=[],
                    candidate_scope=(
                        "Please provide all dry dense-slab Lautaret events with paired snow "
                        "pits, release-state surveys, pre/post laser products and terminal "
                        "runout observations, preferably at least 24 candidates across the "
                        "site's distinct paths and storm cycles."
                    ),
                    owner_specific_files=[
                        "raw and processed pre/post-event laser scans, snow-surface DEMs, control networks and transformation/calibration records",
                        "original release, terminal deposit and endpoint mappings plus the complete searched survey domain",
                        "event snow-pit slope-normal release thickness and density sheets with uncertainty and sampling times",
                        "exact UTC trigger/release logs, path and storm-cycle evidence, imagery/photogrammetry products and detection/occlusion masks",
                    ],
                    licence_terms=(
                        "Please state the applicable INRAE licence for the underlying event "
                        "files or include signed written research permission; a publication's "
                        "licence will not be assumed to cover unpublished measurements."
                    ),
                ),
            },
            {
                "owner_id": "ngi-ryggfonn",
                "contacts": ["heidi.hefre@ngi.no", "peter.gauer@ngi.no", "ngi@ngi.no"],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://www.ngi.no/en/research-and-consulting/natural-hazards-container/avalanches-and-slides/avalanches-and-slush-flows/ryggfonn/",
                        "basis": (
                            "Official NGI Ryggfonn page names Heidi Hefre and Peter Gauer "
                            "and describes the current instrumented archive."
                        ),
                    }
                ],
                "candidate_request": (
                    "All strongest dry dense-slab candidates from the full-scale archive, "
                    "including recent campaigns; preferably 24 or more before QA attrition."
                ),
                "specific_originals": [
                    "Original release/deposit surveys and event-surface laser products",
                    "Survey coverage and known-absence semantics rather than report figures",
                    "Release thickness/density, uncertainty, radar/pressure calibration, and clocks",
                    "Reuse permission, original CRS/datum lineage, and per-file SHA-256",
                ],
                "send_ready_message": _send_ready_message(
                    owner="Norwegian Geotechnical Institute Ryggfonn programme",
                    to=["heidi.hefre@ngi.no", "peter.gauer@ngi.no"],
                    cc=["ngi@ngi.no"],
                    candidate_scope=(
                        "Please provide the strongest complete dry dense-slab candidates from "
                        "the full Ryggfonn archive, including the 2023/24 and 2024/25 campaigns, "
                        "preferably 24 candidates before eligibility attrition."
                    ),
                    owner_specific_files=[
                        "source GIS, pre-event laser snow surfaces, post-event deposit surveys and field notes behind report figures",
                        "release geometry, slope-normal release thickness, release density and their sampling/uncertainty records",
                        "survey footprints and known-absence/detection/occlusion masks with terminal dense-flow component attribution",
                        "raw radar, pressure and velocity files with sensor coordinates, calibration, clock synchronization, valid intervals and uncertainty",
                    ],
                    licence_terms=(
                        "The public annual reports do not establish reusable rights for the "
                        "underlying measurements; please include the exact licence or written "
                        "research permission covering every delivered source file."
                    ),
                ),
            },
            {
                "owner_id": "seehore-partners",
                "contacts": ["michele.freppaz@unito.it", "monica.barbero@polito.it"],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://unifind.unito.it/individual?uri=http%3A%2F%2Firises.unito.it%2Fresource%2Fperson%2F9997",
                        "basis": "Current official Universita di Torino profile for Michele Freppaz.",
                    },
                    {
                        "url": "https://www.polito.it/personale?p=monica.barbero",
                        "basis": "Current official Politecnico di Torino profile for Monica Barbero.",
                    },
                    {
                        "url": "https://iris.polito.it/handle/11583/2765872",
                        "basis": "Official institutional record for the five-event Seehore measurements paper.",
                    },
                ],
                "candidate_request": (
                    "Every dry dense-slab event for which original release, snow-property, "
                    "laser/GPS, and terminal-deposit records survive; do not preselect on model fit."
                ),
                "specific_originals": [
                    "GPS/RTK release and deposit perimeters, benchmarks and transformations",
                    "Pre/post laser scans, snow-surface products, and calibration/accuracy records",
                    "Normal-to-slope release depth and event release density with uncertainty",
                    "Event timing, survey domain/detection masks, licence, and hashes",
                ],
                "send_ready_message": _send_ready_message(
                    owner="Universita di Torino / Politecnico di Torino Seehore partners",
                    to=["michele.freppaz@unito.it", "monica.barbero@polito.it"],
                    cc=[],
                    candidate_scope=(
                        "Please provide every dry dense-slab Seehore event for which original "
                        "release, snow-property, laser/GPS and terminal-deposit records survive; "
                        "please do not limit the delivery to the five published impact events."
                    ),
                    owner_specific_files=[
                        "original GPS/RTK release and terminal-deposit perimeters, benchmarks and transformations",
                        "pre/post laser scans, event snow-surface products, raw imagery and instrument calibration/accuracy records",
                        "normal-to-slope release depth and event-specific release density with sampling locations and uncertainty",
                        "UTC event/trigger logs, pressure records, survey domain and detection/occlusion masks with dense-flow attribution",
                    ],
                    licence_terms=(
                        "The open-access article licence does not by itself license the "
                        "underlying event measurements; please identify the owner of each file "
                        "and provide an applicable data licence or written research permission."
                    ),
                ),
            },
            {
                "owner_id": "bfw-wlv-avaframedata",
                "contacts": [
                    "felix.oesterle@bfw.gv.at",
                    "anna.wirbel@bfw.gv.at",
                    "frank.perzl@bfw.gv.at",
                    "schneelawine@die-wildbach.at",
                ],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://www.bfw.gv.at/en/departments-en/natural-hazards/snow-avalanches/",
                        "basis": (
                            "Current official BFW Snow & Avalanches directory lists Felix "
                            "Oesterle, Anna Wirbel, and Frank Perzl."
                        ),
                    },
                    {
                        "url": "https://www.bmluk.gv.at/themen/wald/wald-und-naturgefahren/wildbach--und-lawinenverbauung/organisation-kontakt/fz_geologie_lawinen.html",
                        "basis": (
                            "Official WLV avalanche centre lists schneelawine@die-wildbach.at "
                            "and describes its documented-event data pool."
                        ),
                    },
                ],
                "candidate_request": (
                    "Original, pre-conversion records for AvaFrameData events and additional "
                    "fully documented events on independent mountains; preferably 24 or more "
                    "candidates before QA attrition."
                ),
                "specific_originals": [
                    "Source GIS and exact CRS operations, especially Filisur EPSG:21781 to EPSG:2056",
                    "Original WLV event records 6534 and 6591 and Eiskar/Arzl UAS-laser products",
                    "Event-specific thickness, density, uncertainty and survey known-absence domain",
                    "Pre-event snow surface, timing, permission, stable version, and file hashes",
                ],
                "send_ready_message": _send_ready_message(
                    owner="BFW Snow & Avalanches and WLV Fachbereich Lawinen",
                    to=["felix.oesterle@bfw.gv.at", "schneelawine@die-wildbach.at"],
                    cc=["anna.wirbel@bfw.gv.at", "frank.perzl@bfw.gv.at"],
                    candidate_scope=(
                        "Please provide original pre-conversion records for the six AvaFrameData "
                        "v1.0 events and additional complete events from independent mountains, "
                        "preferably 24 candidates before review attrition."
                    ),
                    owner_specific_files=[
                        "source GIS and exact CRS operations, especially the Filisur EPSG:21781-to-EPSG:2056 operation",
                        "original WLV event records 6534 and 6591, Eiskar drone/laser products, and Arzl UAS imagery, point cloud, orthophoto, surface model and deposit geometry",
                        "event-specific slope-normal release thickness, density, uncertainty, UTC timing and pre-event snow surfaces",
                        "survey known-absence domains, detection/occlusion masks and source-to-AvaFrameData derivation records",
                    ],
                    licence_terms=(
                        "The public AvaFrameData record states CC BY 4.0; please confirm whether "
                        "that licence covers each original pre-conversion BFW/WLV source file "
                        "and supply different owner permissions where it does not."
                    ),
                ),
            },
            {
                "owner_id": "parks-canada-rogers-pass",
                "contacts": ["mrg.information@pc.gc.ca"],
                "contacts_verified_date_utc": CONTACTS_VERIFIED_DATE_UTC,
                "contact_verification": [
                    {
                        "url": "https://www.parks.canada.ca/pn-np/bc/glacier/info/contact",
                        "basis": (
                            "Current official Glacier National Park contact page lists "
                            "mrg.information@pc.gc.ca and the Revelstoke office."
                        ),
                    },
                    {
                        "url": "https://parks.canada.ca/pn-np/mtn/securiteenmontagne-mountainsafety/avalanche/routes-highways.aspx",
                        "basis": (
                            "Official Parks Canada page describes the Rogers Pass highway "
                            "programme and 135 avalanche paths."
                        ),
                    },
                ],
                "candidate_request": (
                    "A de-identified research cohort across independent named paths and storm "
                    "cycles, preferably 24-40 candidates before QA attrition."
                ),
                "specific_originals": [
                    "Field release/deposit/endpoint surveys and explicit survey coverage",
                    "Event snow-surface terrain, release thickness/density, timing and uncertainty",
                    "Path/mountain/storm grouping evidence without personal information",
                    "Research reuse terms and immutable file inventory",
                ],
                "send_ready_message": _send_ready_message(
                    owner="Parks Canada Glacier National Park / Rogers Pass avalanche programme",
                    to=["mrg.information@pc.gc.ca"],
                    cc=[],
                    candidate_scope=(
                        "Please refer this request to the Rogers Pass avalanche-program data "
                        "steward and provide a de-identified cohort across independent named "
                        "paths and storm cycles, preferably 24–40 candidates."
                    ),
                    owner_specific_files=[
                        "original event-level field release/deposit polygons or surveyed terminal endpoints and explicit searched survey coverage",
                        "pre-event snow-surface terrain, slope-normal release thickness, release density, UTC control/event timing and uncertainty",
                        "path, mountain and storm-cycle grouping evidence without personal information",
                        "detection/occlusion masks, observation methods, control records and immutable source-file inventory",
                    ],
                    licence_terms=(
                        "Please provide the applicable Government of Canada/Parks Canada data "
                        "licence or written research permission for these non-public operational "
                        "records, including any confidentiality, attribution and redistribution restrictions."
                    ),
                ),
            },
        ],
        "request_message": {
            "subject": (
                "Research data request: dry dense-slab avalanche release/runout validation cohort"
            ),
            "opening": (
                "We are requesting original field-observation data for a non-operational, "
                "experimental avalanche-model validation study. We will not treat model scores "
                "as probabilities or replace public avalanche guidance or field assessment."
            ),
            "scope": (
                "Please provide all candidate events meeting, or potentially meeting, the "
                "attached per-event delivery contract. We need at least 12 eligible events after "
                "independent review across six paths, two mountains and three storm cycles, so "
                "24-40 candidates are preferred. Please do not select events based on model fit."
            ),
            "licence_request": (
                "Please identify the owner, licence or written research permission, citation, "
                "stable version/URI, and original byte count and SHA-256 for every delivered file."
            ),
            "blindness_request": (
                "Do not include calibration/holdout labels. We will freeze grouped assignments "
                "only after independent eligibility review and before generating any predictions."
            ),
        },
        "preregistered_metrics_after_a_complete_frozen_split": {
            "terminal_endpoint_euclidean_error_m": (
                "sqrt((predicted_x-observed_x)^2 + (predicted_y-observed_y)^2)"
            ),
            "intersection_over_union": (
                "intersection_cell_count / union_cell_count on the common valid evaluation mask"
            ),
            "false_positive_area_m2": (
                "count(predicted_positive and observed_negative and valid) * cell_area_m2"
            ),
            "false_negative_area_m2": (
                "count(observed_positive and predicted_negative and valid) * cell_area_m2"
            ),
            "execution_status": "not_run_no_eligible_frozen_cohort",
        },
        "after_delivery": [
            "Verify every byte count and SHA-256 without altering source files.",
            "Obtain two isolated, identity-verified human eligibility reviews bound to the same source bytes; AI output never counts as a review.",
            "Resolve any disagreement through a third independent human and retain both reviews, the resolution, exclusion reasons, and record hashes.",
            "Adjudicate every event in the verified delivery cohort, including exclusions; do not silently select or omit candidates.",
            "Independently review licence, source independence, component attribution, uncertainties, CRS/datum/units, surface validity, coverage and grouping.",
            "Reject incomplete events; never infer missing physical or observation values.",
            "Normalize accepted evidence into validation-contract v3 and code-review its exact trust identity.",
            "Only if the full 12/6/2/3 eligible cohort gate passes, apply the already frozen connected-group split procedure; do not expose real assignments earlier.",
            "Keep holdout observation files sealed and inaccessible throughout calibration; freeze model/configuration and complete holdout predictions before opening them for metrics.",
            "Run all preregistered metrics once on the complete untouched holdout and retain failures.",
        ],
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def render_owner_requests(request: dict[str, Any]) -> bytes:
    request_bytes = _pretty_json(request)
    schema_bytes = _pretty_json(FieldValidationOwnerDelivery.model_json_schema())
    lines = [
        "# Field-validation owner requests",
        "",
        "These messages are ready for the project owner to send. They have not been sent. ",
        "They request candidate data only; no returned event becomes evidence until byte/hash ",
        "verification and two independent human reviews pass the frozen protocol. AI output ",
        "cannot count as an independent review.",
        "",
        "Current eligible event/path/mountain/storm counts: **0 / 0 / 0 / 0**. Field ",
        "predictions and metrics remain unauthorized.",
        "",
        "## Recalculated identities",
        "",
        "| Artifact | Bytes | SHA-256 |",
        "|---|---:|---|",
    ]
    for identity in request["frozen_inputs"].values():
        lines.append(
            f"| `{identity['path']}` | {identity['bytes']} | `{identity['sha256']}` |"
        )
    lines.extend(
        [
            f"| `validation-data/acquisition/field-validation-owner-request-v1.json` | {len(request_bytes)} | `{_sha256_bytes(request_bytes)}` |",
            f"| `validation-data/acquisition/field-validation-owner-delivery-v1.schema.json` | {len(schema_bytes)} | `{_sha256_bytes(schema_bytes)}` |",
            "",
            "The complete acquisition/workflow hash inventory is regenerated at ",
            "`validation-data/acquisition/field-validation-acquisition-integrity-v1.json`.",
        ]
    )
    for route in request["owner_routes"]:
        message = route["send_ready_message"]
        lines.extend(
            [
                "",
                f"## {route['owner_id']}",
                "",
                f"Verified: {route['contacts_verified_date_utc']} UTC date",
                "",
                f"To: {', '.join(message['to'])}",
                f"Cc: {', '.join(message['cc']) if message['cc'] else '—'}",
                f"Subject: {message['subject']}",
                "",
                message["body"],
                "",
                "Official route verification:",
                "",
            ]
        )
        for source in route["contact_verification"]:
            lines.append(f"- {source['url']} — {source['basis']}")
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request-output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/acquisition/field-validation-owner-request-v1.json",
    )
    parser.add_argument(
        "--schema-output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/acquisition/field-validation-owner-delivery-v1.schema.json",
    )
    parser.add_argument(
        "--messages-output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/acquisition/field-validation-owner-requests-v1.md",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    request = build_request()
    schema = FieldValidationOwnerDelivery.model_json_schema()
    args.request_output.parent.mkdir(parents=True, exist_ok=True)
    args.schema_output.parent.mkdir(parents=True, exist_ok=True)
    args.messages_output.parent.mkdir(parents=True, exist_ok=True)
    args.request_output.write_bytes(_pretty_json(request))
    args.schema_output.write_bytes(_pretty_json(schema))
    args.messages_output.write_bytes(render_owner_requests(request))
    print(
        "Prepared field-validation owner request; eligible public events=0; "
        "partitions and predictions remain absent."
    )


if __name__ == "__main__":
    main()
