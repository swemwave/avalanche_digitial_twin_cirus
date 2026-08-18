"""The SnowState-to-Release coupling contract and its refusal rules."""

from __future__ import annotations

import pytest

from avycore.release_coupling import (
    RELEASE_COUPLING_SCHEMA_VERSION,
    REQUIRED_COUPLING_FIELDS,
    CoupledRegime,
    CouplingEligibility,
    ModelledQuantity,
    ReleaseCouplingInputs,
    SnowStateProvenance,
    WeakLayerCandidate,
    evaluate_coupling_eligibility,
)


def _quantity(value: float, unit: str, spread: float = 0.1) -> ModelledQuantity:
    return ModelledQuantity(
        value=value,
        unit=unit,
        lower=value * (1.0 - spread),
        upper=value * (1.0 + spread),
        basis="numerical",
    )


def _provenance(**overrides) -> SnowStateProvenance:
    defaults = {
        "snow_state_pack_id": "snow-state-pack-0001",
        "snow_state_sha256": "a" * 64,
        "condition_pack_id": "condition-0001",
        "engine_id": "snow.snowpack",
        "engine_version": "3.7.0",
        "terrain_class_mapping_version": "terrain-class-v1",
        "simulation_start_utc": "2025-10-01T00:00:00Z",
        "valid_time_utc": "2026-02-01T00:00:00Z",
    }
    return SnowStateProvenance(**{**defaults, **overrides})


def _weak_layer(depth_m: float = 0.4) -> WeakLayerCandidate:
    return WeakLayerCandidate(
        layer_id="wl-001",
        depth_below_surface=_quantity(depth_m, "m"),
        grain_type="faceted_crystals",
        grain_size=_quantity(1.5, "mm"),
        hardness=_quantity(2.0, "1"),
        is_persistent=True,
    )


def _inputs(**overrides) -> ReleaseCouplingInputs:
    defaults = {
        "schema_version": RELEASE_COUPLING_SCHEMA_VERSION,
        "regime": CoupledRegime.DRY_SLAB,
        "terrain_class_id": "e2200-ne-open",
        "provenance": _provenance(),
        "slab_depth": _quantity(0.6, "m"),
        "slab_density": _quantity(220.0, "kg m-3"),
        "weak_layer": _weak_layer(),
        "failure_initiation_index": _quantity(1.2, "1"),
        "crack_propagation_index": _quantity(0.35, "m"),
        "loading_rate": _quantity(1.8, "kg m-2 h-1"),
        "loading_window_hours": 24.0,
    }
    return ReleaseCouplingInputs(**{**defaults, **overrides})


def test_a_complete_contract_couples_and_derives_slab_mass():
    inputs = _inputs()
    central, lower, upper = inputs.slab_mass_per_area_kg_m2()
    assert central == pytest.approx(0.6 * 220.0)
    assert lower < central < upper
    assert inputs.provenance.values_are_modelled_not_observed is True


def test_every_coupled_quantity_must_carry_its_declared_unit():
    with pytest.raises(ValueError, match="slab_depth must be declared in 'm'"):
        _inputs(slab_depth=_quantity(0.6, "cm"))
    with pytest.raises(ValueError, match="slab_density must be declared in 'kg m-3'"):
        _inputs(slab_density=_quantity(220.0, "g cm-3"))
    with pytest.raises(ValueError, match="crack_propagation_index must be declared in 'm'"):
        _inputs(crack_propagation_index=_quantity(0.35, "cm"))
    with pytest.raises(ValueError, match="loading_rate must be declared in 'kg m-2 h-1'"):
        _inputs(loading_rate=_quantity(1.8, "mm h-1"))


def test_a_quantity_needs_an_ordered_finite_sensitivity_span():
    with pytest.raises(ValueError, match="lower <= value <= upper"):
        ModelledQuantity(value=1.0, unit="m", lower=2.0, upper=3.0, basis="literature")
    with pytest.raises(ValueError, match="must be finite"):
        ModelledQuantity(value=float("nan"), unit="m", lower=0.0, upper=1.0, basis="literature")
    span = ModelledQuantity(value=1.0, unit="m", lower=0.5, upper=1.5, basis="literature")
    assert span.interpretation == "bounded_sensitivity_not_probability"


def test_a_snow_profile_invented_at_the_requested_time_is_refused():
    with pytest.raises(ValueError, match="no evolved layer history"):
        _provenance(
            simulation_start_utc="2026-02-01T00:00:00Z",
            valid_time_utc="2026-02-01T00:00:00Z",
        )
    with pytest.raises(ValueError, match="no evolved layer history"):
        _provenance(
            simulation_start_utc="2026-03-01T00:00:00Z",
            valid_time_utc="2026-02-01T00:00:00Z",
        )


def test_a_weak_layer_below_the_deepest_slab_is_inconsistent():
    with pytest.raises(ValueError, match="below the deepest modelled slab"):
        _inputs(weak_layer=_weak_layer(depth_m=5.0))


def test_terrain_alone_is_removed_from_coverage_and_never_scored():
    decision = evaluate_coupling_eligibility(
        "e2200-ne-open", regime="dry_slab", snow_state=None
    )
    assert decision.eligible is False
    assert decision.coverage_effect == "removed_from_supported_coverage"
    assert set(decision.missing_fields) == set(REQUIRED_COUPLING_FIELDS)
    assert "Terrain capability alone cannot" in decision.reasons[0]


def test_a_partial_snow_state_is_refused_and_names_what_is_missing():
    decision = evaluate_coupling_eligibility(
        "e2200-ne-open",
        regime="dry_slab",
        snow_state={"slab_depth": _quantity(0.6, "m"), "slab_density": _quantity(220.0, "kg m-3")},
    )
    assert decision.eligible is False
    assert set(decision.missing_fields) == {
        "weak_layer",
        "failure_initiation_index",
        "crack_propagation_index",
        "loading_rate",
    }
    assert decision.coverage_effect == "removed_from_supported_coverage"


def test_other_regimes_are_separate_model_types_not_extra_weights():
    for regime in ("wet_snow", "dry_loose", "glide", "powder_cloud", "mixed"):
        decision = evaluate_coupling_eligibility(
            "e2200-ne-open", regime=regime, snow_state=_inputs()
        )
        assert decision.eligible is False
        assert "separate model types" in decision.reasons[0]
        assert decision.coverage_effect == "removed_from_supported_coverage"


def test_a_snow_state_for_a_different_terrain_class_does_not_transfer():
    decision = evaluate_coupling_eligibility(
        "e1800-sw-forested", regime="dry_slab", snow_state=_inputs()
    )
    assert decision.eligible is False
    assert "not 'e1800-sw-forested'" in decision.reasons[0]


def test_a_complete_state_is_eligible_and_still_labelled_uncalibrated():
    decision = evaluate_coupling_eligibility(
        "e2200-ne-open", regime="dry_slab", snow_state=_inputs()
    )
    assert decision.eligible is True
    assert decision.coverage_effect == "supported"
    assert decision.missing_fields == ()
    assert "not field observations" in decision.reasons[0]
    assert "uncalibrated" in decision.reasons[0]


def test_an_ineligible_class_cannot_claim_to_be_supported_coverage():
    """The contract has no way to express "missing snow state, lower score"."""

    with pytest.raises(ValueError, match="never lower a result toward safety"):
        CouplingEligibility(
            terrain_class_id="e2200-ne-open",
            eligible=False,
            missing_fields=("slab_depth",),
            reasons=("missing",),
            coverage_effect="supported",
        )
    with pytest.raises(ValueError, match="cannot report missing fields"):
        CouplingEligibility(
            terrain_class_id="e2200-ne-open",
            eligible=True,
            missing_fields=("slab_depth",),
            reasons=("eligible but incomplete",),
            coverage_effect="supported",
        )
