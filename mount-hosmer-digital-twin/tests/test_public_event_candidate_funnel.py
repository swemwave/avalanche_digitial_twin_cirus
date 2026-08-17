from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FUNNEL_PATH = (
    ROOT
    / "validation-data"
    / "candidates"
    / "public-event-candidates-v1.json"
)
EXPERIMENT_PATH = (
    ROOT
    / "validation-data"
    / "experiments"
    / "public-data-field-validation-v1.json"
)
EXPERIMENT_V2_PATH = (
    ROOT
    / "validation-data"
    / "experiments"
    / "public-data-field-validation-v2.json"
)
SCRIPT_PATH = ROOT / "scripts" / "validation" / "build_public_event_candidate_funnel.py"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def test_experiment_freezes_component_specific_protocol_before_predictions() -> None:
    experiment = _load(EXPERIMENT_PATH)

    assert experiment["validation_contract"] == "avycore-validation-dataset-v3"
    assert experiment["predictions_generated"] is False
    assert experiment["holdout_targets_accessed"] is False
    assert set(experiment["component_profiles"]) == {"R", "C", "E"}
    assert experiment["component_profiles"]["R"]["component"] == "release"
    assert (
        experiment["component_profiles"]["C"]["component"]
        == "conditional_runout"
    )
    assert experiment["component_profiles"]["E"]["component"] == "end_to_end"
    assert experiment["component_profiles"]["E"]["observed_release_input_forbidden"]
    assert (
        experiment["dense_flow_footprint_definition"]["primary_threshold_m"]
        == 0.1
    )
    assert experiment["prior_evidence_policy"]["final_holdout_allowed"] is False
    assert len(experiment["prior_evidence_policy"]["development_only"]) == 5
    assert "not probabilities" in experiment["prototype_disclaimer"]


def test_field_validation_v2_freezes_requested_metrics_and_no_data_outcome() -> None:
    experiment = _load(EXPERIMENT_V2_PATH)

    assert experiment["protocol_status"] == "frozen_no_eligible_public_dataset"
    assert experiment["predictions_generated"] is False
    assert experiment["holdout_targets_accessed"] is False
    assert experiment["evaluation_scope"]["minimum_final_events"] == 12
    assert experiment["evaluation_scope"]["minimum_independent_paths"] == 6
    metrics = experiment["preregistered_metrics"]
    assert set(metrics) == {
        "runout_distance_error",
        "observed_area_overlap",
        "false_positive_area_m2",
        "false_negative_area_m2",
        "depth_velocity_pressure",
        "cohort_summary",
    }
    assert metrics["runout_distance_error"]["units"] == "m"
    assert metrics["false_positive_area_m2"]["implementation"].endswith(
        "binary_mask_metrics"
    )
    assert metrics["false_negative_area_m2"]["implementation"].endswith(
        "binary_mask_metrics"
    )
    assert experiment["data_blocker"]["model_run_status"] == "not_run"
    assert experiment["public_data_audit"]["strict_funnel"]["eligible_any_profile"] == 0
    assert "at least 12 dry dense-slab events" in experiment[
        "single_next_requirement_before_field_validation_claim"
    ]


def test_candidate_funnel_is_frozen_metadata_only_and_hash_linked() -> None:
    funnel = _load(FUNNEL_PATH)
    experiment_hash = hashlib.sha256(EXPERIMENT_PATH.read_bytes()).hexdigest()

    assert funnel["schema"] == "avycore-public-event-candidate-funnel-v1"
    assert funnel["experiment_spec_sha256"] == experiment_hash
    assert funnel["predictions_generated"] is False
    assert funnel["model_code_imported"] is False
    assert funnel["holdout_partition_assigned"] is False
    assert funnel["holdout_targets_accessed"] is False
    assert funnel["counts"] == {
        "total": 46,
        "regobs": 40,
        "avaframedata_development_only": 6,
        "regobs_with_stop_extent": 28,
        "regobs_with_fracture_height": 26,
        "regobs_with_weather_observation": 13,
        "regobs_with_snow_profile": 11,
        "regobs_with_nonempty_snow_density": 0,
        "candidates_with_release_density_evidence": 0,
        "contract_eligible_R": 0,
        "contract_eligible_C": 0,
        "contract_eligible_E": 0,
        "final_holdout_assigned": 0,
    }
    assert len(funnel["candidates"]) == 46
    assert len({item["candidate_id"] for item in funnel["candidates"]}) == 46
    assert datetime.fromisoformat(funnel["generated_at_utc"].replace("Z", "+00:00"))
    assert not {
        "prediction",
        "predicted_geometry",
        "predicted_values",
        "model_score",
        "hazard_score",
    }.intersection(_keys(funnel["candidates"]))


def test_regobs_candidates_preserve_lineage_and_unlabelled_semantics() -> None:
    funnel = _load(FUNNEL_PATH)
    acquisition = funnel["source_acquisition"]["regobs"]
    candidates = [
        item
        for item in funnel["candidates"]
        if item["source_collection"] == "RegObs public API v5"
    ]

    assert len(candidates) == 40
    assert acquisition["api_url"] == "https://api.regobs.no/v5/Search"
    assert acquisition["swagger_url"].startswith("https://api.regobs.no/")
    assert acquisition["terms_url"].startswith("https://www.varsom.no/")
    assert "NLOD" in acquisition["licence"]
    assert SHA256_PATTERN.fullmatch(acquisition["request_sha256"])
    assert SHA256_PATTERN.fullmatch(acquisition["response_sha256"])
    assert SHA256_PATTERN.fullmatch(acquisition["swagger_sha256"])
    assert acquisition["response_bytes"] > 0
    assert acquisition["request"]["ToDtObsTime"] == "2026-08-13T23:59:59Z"

    for candidate in candidates:
        assert candidate["regime"]["provider_type"] == "Dry slab avalanche"
        assert candidate["regime"]["contract_regime"] == "dry_dense_slab"
        assert candidate["geometry_availability"]["release_extent_present"]
        assert (
            candidate["geometry_availability"]["stop_extent_present"]
            or candidate["geometry_availability"]["stop_point_present"]
        )
        assert candidate["geometry_availability"]["source_crs"] == "EPSG:4326"
        assert candidate["geometry_availability"]["target_coordinates_embedded_in_inventory"] is False
        assert candidate["coverage_semantics"]["label_state"] == "positive_unlabelled"
        assert candidate["coverage_semantics"]["complete_search_claimed"] is False
        assert candidate["coverage_semantics"]["unreported_avalanches_are_negative"] is False
        assert candidate["terrain_surface"]["status"] == "not acquired"
        assert candidate["holdout_target_accessed"] is False
        assert set(candidate["eligibility_by_profile"].values()) == {
            "ineligible_as_published"
        }
        assert candidate["rejection_reasons"]
        assert SHA256_PATTERN.fullmatch(candidate["source_record_canonical_sha256"])
        assert candidate["source_response_sha256"] == acquisition["response_sha256"]


def test_previously_viewed_avaframe_cases_are_development_only() -> None:
    funnel = _load(FUNNEL_PATH)
    candidates = [
        item
        for item in funnel["candidates"]
        if item["source_collection"] == "AvaFrameData v1.0"
    ]

    assert {item["source_record_id"] for item in candidates} == {
        "Arzl",
        "Eiskar",
        "Filisur1",
        "Filisur2",
        "Kleiner_Oetscherbach",
        "Popeletzbach",
    }
    for candidate in candidates:
        assert candidate["partition"] == "development"
        assert candidate["development_only"] is True
        assert candidate["final_holdout_allowed"] is False
        assert candidate["holdout_target_accessed"] is False
        assert candidate["coverage_semantics"]["label_state"] == "positive_unlabelled"
        assert candidate["coverage_semantics"]["unreported_avalanches_are_negative"] is False
        assert set(candidate["eligibility_by_profile"].values()) == {
            "development_evidence_only_not_contract_eligible"
        }
        assert SHA256_PATTERN.fullmatch(candidate["source_archive_sha256"])
        assert any("development" in reason for reason in candidate["rejection_reasons"])


def test_public_source_audit_records_access_and_contract_failures() -> None:
    audit = _load(FUNNEL_PATH)["public_source_audit"]

    assert audit["primary_or_official_sources_only"] is True
    assert audit["outreach_sent"] is False
    assert audit["accounts_created"] is False
    assert audit["special_terms_accepted"] is False
    sources = {item["organization"]: item for item in audit["sources"]}
    assert {
        "WSL Institute for Snow and Avalanche Research SLF / EnviDat",
        "Norwegian Geotechnical Institute (NGI)",
        "Norwegian Water Resources and Energy Directorate (NVE)",
        "Parks Canada",
        "Colorado Avalanche Information Center (CAIC)",
        "Avalanche Canada",
    } == set(sources)
    assert sources[
        "WSL Institute for Snow and Avalanche Research SLF / EnviDat"
    ]["licence"] == "CC BY-SA 4.0"
    assert "Do not acquire or open" in sources[
        "WSL Institute for Snow and Avalanche Research SLF / EnviDat"
    ]["anti_leakage_action"]
    assert "non-fatal incidents are not reported" in sources[
        "Colorado Avalanche Information Center (CAIC)"
    ]["public_evidence"]
    assert sources["Avalanche Canada"]["external_commitment_required"] is True
    for source in sources.values():
        assert source["official_urls"]
        assert all(url.startswith("https://") for url in source["official_urls"])
        assert source["downloaded"] is False
        assert source["sha256"] is None
        assert source["reason_not_downloaded"]
        assert source["contract_assessment"]


def test_funnel_builder_has_no_model_imports_or_protected_output_defaults() -> None:
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any(
        name.startswith(("avycore", "app", "backend")) for name in imported
    )
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'REPOSITORY_ROOT / "DATA"' in source
    assert 'REPOSITORY_ROOT / "runtime"' in source
    assert ".validation-cache" in source
