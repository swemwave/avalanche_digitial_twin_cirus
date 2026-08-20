from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "freeze_public_release_state_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("freeze_public_release_state_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_crown_field_is_not_promoted_to_slope_normal_thickness() -> None:
    candidate = {
        "candidate_id": "regobs-test",
        "event": {"fracture_height_cm": 75},
        "source_record_file_sha256": "a" * 64,
    }
    result = MODULE.crown_height_evidence(candidate)
    assert result["provider_value_m_unit_conversion_only"] == 0.75
    assert result["semantics_verified_as_crown_edge_height"] is True
    assert result["semantics_verified_as_normal_to_slope_slab_thickness"] is False
    assert result["conversion_to_release_thickness_permitted"] is False
    assert result["validation_contract_v3_release_thickness_evidence_eligible"] is False


def test_frozen_density_prior_is_bounded_and_not_event_specific() -> None:
    artifact = MODULE.build_release_state_evidence(
        Path(__file__).resolve().parents[1]
        / "validation-data"
        / "candidates"
        / "public-regobs-blinded-evidence-v1.json"
    )
    prior = artifact["release_density_evidence"]
    assert prior["distribution_family"] == "continuous_uniform"
    assert prior["lower"] == 200.0
    assert prior["upper"] == 250.0
    assert prior["unit"] == "kg m-3"
    assert prior["event_specific_measurement"] is False
    assert prior["selected_from_runout_performance"] is False
    assert artifact["runout_results_accessed"] is False
    assert artifact["counts"]["release_thickness_evidence_eligible"] == 0
