from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "freeze_public_event_technical_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("freeze_public_event_technical_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_provider_offset_is_normalized_and_original_values_are_preserved() -> None:
    candidate = {
        "event_time": {
            "provider_earliest": "2026-03-28T12:00:00+01:00",
            "provider_latest": "2026-03-28T12:43:00+01:00",
            "provider_observation_time": "2026-03-28T16:19:17+01:00",
        }
    }
    result = MODULE._event_time(candidate)
    assert result["event_start_utc"] == "2026-03-28T11:00:00Z"
    assert result["event_end_utc"] == "2026-03-28T11:43:00Z"
    assert result["interval_seconds"] == 2580
    assert result["event_time_confidence"] == "medium"
    assert result["provider_latest_preserved"] == "2026-03-28T12:43:00+01:00"
    assert result["contract_ready"] is True


def test_single_provider_time_is_low_confidence_not_unknown() -> None:
    result = MODULE._event_time(
        {
            "event_time": {
                "provider_earliest": None,
                "provider_latest": "2026-03-28T12:43:00+01:00",
                "provider_observation_time": "2026-03-28T16:19:17+01:00",
            }
        }
    )
    assert result["status"] == "known"
    assert result["event_time_confidence"] == "low"
    assert result["interval_seconds"] == 0
    assert result["contract_ready"] is True


def test_missing_provider_time_remains_null_and_fails() -> None:
    result = MODULE._event_time(
        {
            "event_time": {
                "provider_earliest": None,
                "provider_latest": None,
                "provider_observation_time": None,
            }
        }
    )
    assert result["event_start_utc"] is None
    assert result["interval_seconds"] is None
    assert result["contract_ready"] is False


def test_checked_in_evidence_preserves_unknown_uncertainties_as_null() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "validation-data"
        / "candidates"
        / "public-event-technical-evidence-v2.json"
    )
    artifact = json.loads(path.read_bytes())
    assert artifact["schema"] == "avycore-public-event-technical-evidence-v2"
    assert artifact["counts"]["bounded_event_times"] == 26
    assert artifact["counts"]["contract_allowlisted_target_grids"] == 0
    assert artifact["counts"]["release_to_runout_rules_frozen"] == 26
    for candidate in artifact["candidates"]:
        uncertainty = candidate["uncertainties"]
        assert uncertainty["component_ambiguity"]["numeric_bound"] is None
        assert uncertainty["component_attribution"]["numeric_bound"] is None
        assert uncertainty["release_thickness"]["normal_to_slope_thickness_m"] is None
        assert uncertainty["release_density"]["geographic_transfer_error_kg_m3"] is None
        assert uncertainty["missing_numeric_bounds_are_null_not_zero"] is True
        assert candidate["lineage"]["all_declared_local_sha256_verified"] is True
