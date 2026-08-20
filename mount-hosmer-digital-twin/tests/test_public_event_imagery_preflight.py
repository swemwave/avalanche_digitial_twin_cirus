from __future__ import annotations

import ast
import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    ROOT / "scripts" / "validation" / "build_public_event_imagery_preflight.py"
)
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "public_event_imagery_preflight"
SOURCE_INVENTORY_PATH = (
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
ARTIFACT_PATH = (
    ROOT
    / "validation-data"
    / "candidates"
    / "public-event-imagery-preflight-v1.json"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("public_event_imagery_preflight", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREFLIGHT = _load_module()


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def _prime_fixture_cache(cache_root: Path) -> None:
    inventory = _load(FIXTURE_ROOT / "candidate-inventory.json")
    candidate = PREFLIGHT.select_source_candidates(inventory)[0]
    normalized = PREFLIGHT.normalize_event_interval(candidate["event_time"])
    query_interval = PREFLIGHT._search_interval(normalized)
    assert query_interval is not None
    discovery = candidate["geographic_discovery"]
    candidate_id = candidate["candidate_id"]
    fixtures = {
        "sentinel_1_grd": "sentinel-1-grd-response.json",
        "sentinel_2_l2a": "sentinel-2-l2a-response.json",
    }
    for sensor, response_name in fixtures.items():
        definition = PREFLIGHT.SENSORS[sensor]
        body = PREFLIGHT.build_search_body(
            definition["collection"],
            discovery["observation_location_longitude"],
            discovery["observation_location_latitude"],
            query_interval,
        )
        descriptor = PREFLIGHT._request_descriptor(
            PREFLIGHT.STAC_SEARCH_URL, "POST", body
        )
        cache_dir = cache_root / "searches" / candidate_id / sensor
        PREFLIGHT._write_immutable(
            cache_dir / "page-000-request.json",
            PREFLIGHT._canonical_json(descriptor),
        )
        PREFLIGHT._write_immutable(
            cache_dir / "page-000-response.json",
            (FIXTURE_ROOT / response_name).read_bytes(),
        )


def _build_fixture(cache_root: Path) -> dict[str, Any]:
    return PREFLIGHT.build_preflight(
        FIXTURE_ROOT / "candidate-inventory.json",
        FIXTURE_ROOT / "experiment.json",
        cache_root,
        offline=True,
        cache_reference=".validation-cache/synthetic-imagery-preflight",
        fetcher=lambda _descriptor: pytest.fail("offline replay attempted network access"),
    )


def test_source_selection_derives_the_expected_26_candidates() -> None:
    selected = PREFLIGHT.select_source_candidates(_load(SOURCE_INVENTORY_PATH))

    assert len(selected) == 26
    assert len({candidate["candidate_id"] for candidate in selected}) == 26
    for candidate in selected:
        assert candidate["source_collection"] == "RegObs public API v5"
        assert (
            candidate["release_initial_condition_evidence"]["fracture_height_value"]
            is not None
        )
        geometry = candidate["geometry_availability"]
        assert geometry["stop_point_present"] or geometry["stop_extent_present"]


def test_utc_normalization_preserves_offsets_and_missing_earliest() -> None:
    normalized = PREFLIGHT.normalize_event_interval(
        {
            "provider_earliest": "2024-03-31T01:30:00+01:00",
            "provider_latest": "2024-03-31T04:30:00+02:00",
            "provider_observation_time": "2024-03-31T05:00:00+02:00",
        }
    )

    assert normalized["provider_original"]["earliest"] == "2024-03-31T01:30:00+01:00"
    assert normalized["normalized_provider_earliest_utc"] == "2024-03-31T00:30:00Z"
    assert normalized["normalized_provider_latest_utc"] == "2024-03-31T02:30:00Z"
    assert normalized["pairing_interval_utc"] == {
        "start": "2024-03-31T00:30:00Z",
        "end": "2024-03-31T02:30:00Z",
    }

    single = PREFLIGHT.normalize_event_interval(
        {
            "provider_earliest": None,
            "provider_latest": "2024-01-10T03:00:00-07:00",
            "provider_observation_time": "2024-01-10T03:05:00-07:00",
        }
    )
    assert single["normalized_provider_earliest_utc"] is None
    assert single["missing_fields"] == ["provider_earliest"]
    assert single["pairing_interval_utc"] == {
        "start": "2024-01-10T10:00:00Z",
        "end": "2024-01-10T10:00:00Z",
    }
    assert "single_instant" in single["pairing_interval_basis"]


def test_fixture_bracketing_and_sensor_compatibility_are_explicit(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    _prime_fixture_cache(cache_root)
    artifact = _build_fixture(cache_root)
    candidate = artifact["candidates"][0]

    assert candidate["event_interval"]["pairing_interval_utc"] == {
        "start": "2024-01-02T00:00:00Z",
        "end": "2024-01-02T02:00:00Z",
    }
    s1 = candidate["sentinel_1_grd"]
    assert s1["catalogue_pair_status"] == "catalogue_pair_found"
    assert s1["pixel_qa_status"] == "requires_pixel_qa"
    assert s1["counts"] == {
        "candidate_acquisitions": 4,
        "pre_event_acquisitions": 1,
        "during_event_interval_acquisitions": 1,
        "post_event_acquisitions": 2,
        "accepted_pairs": 1,
        "rejected_pairs": 1,
    }
    accepted = s1["accepted_pairs"][0]
    assert accepted["relative_orbit"] == 42
    assert accepted["acquisition_mode"] == "IW"
    assert accepted["polarizations"] == ["VH", "VV"]
    assert accepted["temporal_baseline_seconds"] == 4 * 86400
    rejected = s1["rejected_pairs"][0]
    assert set(rejected["rejection_reasons"]) == {
        "different_orbit_direction",
        "different_relative_orbit",
        "different_acquisition_mode",
        "different_polarization_set",
    }

    s2 = candidate["sentinel_2_l2a"]
    assert s2["catalogue_pair_status"] == "catalogue_pair_found"
    assert s2["pixel_qa_status"] == "requires_pixel_qa"
    assert s2["counts"]["accepted_pairs"] == 1
    assert s2["counts"]["rejected_pairs"] == 1
    assert s2["accepted_pairs"][0]["mgrs_tile"] == "32VNM"
    assert s2["accepted_pairs"][0]["pre_catalogue_cloud_cover_percent"] is None
    assert s2["accepted_pairs"][0]["post_catalogue_cloud_cover_percent"] == 92.5
    assert s2["accepted_pairs"][0]["pixel_qa_required"] is True
    assert s2["rejected_pairs"][0]["rejection_reasons"] == ["different_mgrs_tile"]
    pre = next(
        item
        for item in s2["candidate_acquisitions"]
        if item["item_id"] == "S2_PRE_MISSING_CLOUD"
    )
    assert pre["processing_level"] == "Level-2A (frozen collection identity)"
    assert pre["catalogue_cloud_cover_percent"] is None
    assert "catalogue_cloud_cover_percent" in pre["catalogue_metadata_missing"]


def test_no_qualifying_pair_and_missing_catalogue_results_are_not_negative() -> None:
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    empty = PREFLIGHT.pair_acquisitions([], "sentinel_1_grd", start, end)

    assert empty["catalogue_pair_status"] == "no_qualifying_pair"
    assert empty["pixel_qa_status"] == (
        "not_reached_because_no_qualifying_catalogue_pair"
    )
    assert empty["availability_reasons"] == [
        "no_catalogue_acquisitions_returned",
        "no_strictly_pre_event_acquisition_in_search_window",
        "no_strictly_post_event_acquisition_in_search_window",
    ]
    assert empty["accepted_pairs"] == []
    assert "negative" not in empty


def test_offline_cache_replay_is_byte_identical_and_hash_bound(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    _prime_fixture_cache(cache_root)

    first = _build_fixture(cache_root)
    second = _build_fixture(cache_root)

    assert PREFLIGHT._pretty_json(first) == PREFLIGHT._pretty_json(second)
    assert first["normalized_artifact_sha256"] == second["normalized_artifact_sha256"]
    unhashed = deepcopy(first)
    identity = unhashed.pop("normalized_artifact_sha256")
    assert PREFLIGHT._sha256_bytes(PREFLIGHT._canonical_json(unhashed)) == identity
    for sensor in ("sentinel_1_grd", "sentinel_2_l2a"):
        page = first["candidates"][0][sensor]["raw_catalogue_pages"][0]
        assert len(page["request_sha256"]) == 64
        assert len(page["response_sha256"]) == 64
        assert page["request_cache_reference"].startswith(
            ".validation-cache/synthetic-imagery-preflight/"
        )


def test_cache_identity_conflict_fails_instead_of_overwriting(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    _prime_fixture_cache(cache_root)
    inventory = _load(FIXTURE_ROOT / "candidate-inventory.json")
    changed = deepcopy(inventory)
    changed["candidates"][0]["geographic_discovery"][
        "observation_location_longitude"
    ] = 10.25
    changed_path = tmp_path / "changed-inventory.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(ValueError, match="Immutable cache identity conflict"):
        PREFLIGHT.build_preflight(
            changed_path,
            FIXTURE_ROOT / "experiment.json",
            cache_root,
            offline=True,
            cache_reference=".validation-cache/synthetic-imagery-preflight",
        )


def test_search_requests_are_metadata_only_and_spatially_bounded() -> None:
    body = PREFLIGHT.build_search_body(
        "sentinel-2-l2a",
        10.0,
        60.0,
        {"start": "2024-01-01T00:00:00Z", "end": "2024-01-10T00:00:00Z"},
    )

    assert body["collections"] == ["sentinel-2-l2a"]
    assert body["intersects"] == {
        "type": "Point",
        "coordinates": [10.0, 60.0],
    }
    assert body["fields"] == {"exclude": ["assets"]}
    assert "bbox" not in body


def test_committed_artifact_has_no_target_or_validation_leakage() -> None:
    artifact = _load(ARTIFACT_PATH)

    assert artifact["schema"] == "avycore-public-event-imagery-preflight-v1"
    assert artifact["counts"]["source_candidates_selected"] == 26
    assert artifact["predictions_generated"] is False
    assert artifact["model_code_imported"] is False
    assert artifact["holdout_partition_assigned"] is False
    assert artifact["holdout_targets_accessed"] is False
    assert artifact["regobs_attachments_accessed"] is False
    assert artifact["regobs_start_stop_target_coordinates_accessed"] is False
    assert artifact["raster_assets_requested_or_downloaded"] is False
    assert "does not confer eligibility" in artifact["claim_boundary"]
    assert "validation_contract_eligible_events" not in artifact["counts"]
    assert len(artifact["candidates"]) == 26
    for candidate in artifact["candidates"]:
        assert candidate["source_selection_evidence"]["target_coordinates_accessed"] is False
        assert candidate["source_selection_evidence"]["attachments_accessed"] is False
        assert candidate["validation_contract_eligibility"].startswith("not_evaluated")

    normalized_keys = {key.lower().replace("_", "") for key in _all_keys(artifact)}
    assert not {
        "startlat",
        "startlong",
        "stoplat",
        "stoplong",
        "startextent",
        "stopextent",
        "attachments",
    }.intersection(normalized_keys)


def test_builder_has_no_model_hazard_runout_or_raster_imports() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert not any(
        name.startswith(("avycore", "app", "backend")) for name in imported
    )
    assert not {
        "rasterio",
        "pyproj",
        "xdem",
        "gdal",
        "pandas",
        "geopandas",
        "laspy",
    }.intersection(imported)
    assert "hazard" not in imported
    assert "runout" not in imported
    assert "StartLat" not in source
    assert "StartLong" not in source
    assert "StopLat" not in source
    assert "StopLong" not in source

