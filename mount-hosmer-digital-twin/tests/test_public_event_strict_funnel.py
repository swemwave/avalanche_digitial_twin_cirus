from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "evaluate_public_event_strict_funnel.py"
)
SPEC = importlib.util.spec_from_file_location("evaluate_public_event_strict_funnel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _candidate(index: int, *, eligible: bool = True) -> dict[str, object]:
    return {
        "candidate_id": f"event-{index}",
        "profiles": {"C": {"eligible": eligible}},
        "path_id": f"path-{index % 6}",
        "mountain_id": f"mountain-{index % 2}",
        "storm_cycle_id": f"storm-{index % 3}",
    }


def test_strict_cohort_gate_does_not_weaken_requirements() -> None:
    failed = MODULE.cohort_gate([_candidate(index) for index in range(11)])
    assert failed["passed"] is False
    assert failed["requirements"] == {
        "events": 12,
        "paths": 6,
        "mountains": 2,
        "storms": 3,
    }

    passed = MODULE.cohort_gate([_candidate(index) for index in range(12)])
    assert passed["passed"] is True


def test_zero_eligible_candidates_produce_zero_group_counts() -> None:
    gate = MODULE.cohort_gate([_candidate(index, eligible=False) for index in range(26)])
    assert gate["observed"] == {
        "eligible_events": 0,
        "distinct_paths": 0,
        "distinct_mountains": 0,
        "distinct_storms": 0,
    }
    assert gate["passed"] is False


def test_contract_source_identity_is_v3_and_hashed() -> None:
    identity = MODULE.contract_source_identity()
    assert identity["version"] == "avycore-validation-dataset-v3"
    assert [item["path"].split("/")[-1] for item in identity["source_files"]] == [
        "contracts.py",
        "status.py",
        "trust.py",
    ]
    assert all(len(item["sha256"]) == 64 for item in identity["source_files"])


def test_every_failing_check_has_exact_predicate_fields_and_classification() -> None:
    assert len(MODULE.CHECK_CATALOG) == 18
    classifications = {
        "technically_resolvable_now",
        "requires_better_public_primary_evidence",
        "requires_genuine_independent_human_action",
    }
    for definition in MODULE.CHECK_CATALOG.values():
        assert definition["classification"] in classifications
        assert definition["predicate"]
        assert definition["required_evidence_fields"]

    technical = {
        name
        for name, definition in MODULE.CHECK_CATALOG.items()
        if definition["classification"] == "technically_resolvable_now"
    }
    assert technical == {
        "bounded_event_time_with_confidence",
        "blinded_packet_released_for_annotation",
        "release_to_runout_rule_frozen",
    }
    public_evidence = {
        name
        for name, definition in MODULE.CHECK_CATALOG.items()
        if definition["classification"] == "requires_better_public_primary_evidence"
    }
    assert public_evidence == {
        "projected_metre_target_crs_and_transform_frozen",
        "event_surface_terrain_eligible",
        "normal_to_slope_release_thickness_evidence",
        "release_density_transferability_accepted",
        "provenance_bearing_release_model_inputs_complete",
    }


def test_checked_in_v3_funnel_closes_only_supported_technical_checks() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "validation-data"
        / "candidates"
        / "public-event-strict-funnel-v5.json"
    )
    artifact = json.loads(path.read_bytes())
    assert artifact["schema"] == "avycore-public-event-strict-funnel-v5"
    assert artifact["is_validated"] is False
    assert artifact["counts"]["eligible_by_profile"] == {"R": 0, "C": 0, "E": 0}
    assert artifact["counts"]["packets_released"] == 26
    assert artifact["counts"]["independent_human_reviews"] == 0
    for candidate in artifact["candidates"]:
        for profile in candidate["profiles"].values():
            checks = profile["checks"]
            assert checks["bounded_event_time_with_confidence"] is True
            assert checks["independent_human_review_complete"] is False
        assert candidate["profiles"]["E"]["checks"]["release_to_runout_rule_frozen"] is True
        assert candidate["eligible_any_profile"] is False
