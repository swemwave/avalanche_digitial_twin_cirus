"""Pin the buried weak-interface index and the two results it was built for.

Software verification only. Nothing here measures field accuracy, adds a
validation event, or licenses an accuracy claim. ``is_validated`` stays false
and strict N stays 0 regardless of every assertion below.

Three things are pinned:

1. **The physics of the index.** Each of its three factors must be able to zero
   it on its own, the two formation mechanisms must combine as alternatives
   rather than as contributions, and an unevaluable mechanism must report
   *unknown* rather than a favourable zero.
2. **That adding it changed nothing at zero weight.** The whole basis for
   attributing any capture difference to stratigraphy is that
   ``weak_loading_weight = 0`` leaves :mod:`avycore.snowpack.release_v2`
   bit-identical to the module that produced ``release-config-search-v1``.
3. **That the two committed artifacts say what the write-up says they say.**
   The SPOT hourly-forcing re-score and the stratigraphy search are both
   development results, both failures on their own terms, and neither spent a
   reserved block.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from avycore.snowpack.regimes import DRY_LOOSE, DRY_SLAB, WET_SNOW
from avycore.snowpack.release_v2 import (
    V1_FROZEN,
    V2_BASELINE,
    ReleaseConfigV2,
    SnowStateV2,
    regime_scores,
    release_mask,
)
from avycore.snowpack.stratigraphy import (
    BASAL_SNOW_TEMPERATURE_C,
    CRITICAL_TEMPERATURE_GRADIENT_K_PER_M,
    GRADIENT_MINIMUM_SNOW_DEPTH_M,
    StratigraphyConfig,
    bulk_temperature_gradient_k_per_m,
    buried_weak_layer_index,
    stratigraphy_parameter_manifest,
)

SCIENTIFIC_USE = "software_verification"

ROOT = Path(__file__).resolve().parents[1]
SPOT_FORCING_RESULT = ROOT / "validation-data/results/release-v2-spot-forcing-v1.json"
STRATIGRAPHY_RESULT = (
    ROOT / "validation-data/results/release-stratigraphy-search-v1.json"
)
CONFIG_SEARCH_RESULT = (
    ROOT / "validation-data/results/release-config-search-v1.json"
)
RESERVED_BLOCKS = {"row1col4", "row2col4", "row6col9", "row5col10"}


def _index(**overrides: Any) -> tuple[np.ndarray, np.ndarray]:
    """A single-cell index evaluation with everything favourable by default."""
    arguments: dict[str, Any] = {
        "faceting_hours": np.array([72.0]),
        "surface_weakening_fraction": np.array([0.0]),
        "antecedent_positive_degree_hours": np.array([0.0]),
        "antecedent_rain_mm": np.array([0.0]),
        "new_snow_index_cm": np.array([40.0]),
        "antecedent_snow_depth_m": np.array([1.5]),
        "config": StratigraphyConfig(),
    }
    arguments.update(overrides)
    return buried_weak_layer_index(**arguments)


# ---------------------------------------------------------------------------
# The gradient estimate
# ---------------------------------------------------------------------------


def test_the_gradient_is_the_bulk_pack_gradient_not_a_near_surface_one():
    """A -10 C surface over a 1 m pack is 10 K/m, the kinetic-growth boundary."""
    gradient = bulk_temperature_gradient_k_per_m(np.array([-10.0]), np.array([1.0]))
    assert gradient[0] == pytest.approx(10.0)
    assert gradient[0] == pytest.approx(
        (BASAL_SNOW_TEMPERATURE_C - -10.0) / 1.0
    )
    # The same surface over a deeper pack drives less: the gradient is what
    # matters, not the temperature.
    deeper = bulk_temperature_gradient_k_per_m(np.array([-10.0]), np.array([2.5]))
    assert deeper[0] == pytest.approx(4.0)
    assert deeper[0] < CRITICAL_TEMPERATURE_GRADIENT_K_PER_M


def test_a_surface_warmer_than_the_base_drives_no_faceting():
    """No negative gradients. Warm-over-cold is not kinetic growth here."""
    assert bulk_temperature_gradient_k_per_m(np.array([4.0]), np.array([1.0]))[0] == 0.0


def test_a_thin_pack_is_clamped_downward_never_inflated():
    """The depth floor caps the reported gradient; it cannot manufacture one.

    A 5 cm pack at -5 C is arithmetically 100 K/m. Reporting that would let a
    formula built for a bulk pack dominate the index on the thinnest, least
    representative terrain, so the denominator is clamped -- which can only make
    the estimate smaller.
    """
    clamped = bulk_temperature_gradient_k_per_m(np.array([-5.0]), np.array([0.05]))
    assert clamped[0] == pytest.approx(5.0 / GRADIENT_MINIMUM_SNOW_DEPTH_M)
    assert clamped[0] < 5.0 / 0.05


# ---------------------------------------------------------------------------
# Each factor can zero the index on its own
# ---------------------------------------------------------------------------


def test_a_fully_faceted_surface_with_no_burial_is_not_a_buried_weak_layer():
    index, known = _index(new_snow_index_cm=np.array([0.0]))
    assert known.all()
    assert index[0] == 0.0


def test_antecedent_melt_destroys_the_interface():
    config = StratigraphyConfig()
    survived, _ = _index()
    destroyed, _ = _index(
        antecedent_positive_degree_hours=np.array(
            [config.melt_destruction_positive_degree_hours]
        )
    )
    partial, _ = _index(
        antecedent_positive_degree_hours=np.array(
            [config.melt_destruction_positive_degree_hours / 2.0]
        )
    )
    assert survived[0] > 0.0
    assert destroyed[0] == 0.0
    assert partial[0] == pytest.approx(survived[0] / 2.0)


def test_antecedent_rain_destroys_the_interface():
    config = StratigraphyConfig()
    destroyed, _ = _index(antecedent_rain_mm=np.array([config.rain_destruction_mm]))
    assert destroyed[0] == 0.0


def test_no_pre_storm_pack_means_no_interface_to_bury():
    index, _ = _index(antecedent_snow_depth_m=np.array([0.0]))
    assert index[0] == 0.0


def test_the_index_is_bounded_and_never_negative():
    index, _ = _index(
        faceting_hours=np.array([10_000.0]),
        surface_weakening_fraction=np.array([5.0]),
        new_snow_index_cm=np.array([500.0]),
    )
    assert 0.0 <= index[0] <= 1.0
    assert index[0] == 1.0


# ---------------------------------------------------------------------------
# The two formation mechanisms are alternatives, not contributions
# ---------------------------------------------------------------------------


def test_two_half_strength_mechanisms_do_not_make_one_full_weak_layer():
    """A surface is weakened by one process or the other, and a storm buries
    whichever happened. Summing them would let two partial mechanisms
    manufacture an interface neither of them produced."""
    config = StratigraphyConfig()
    half_hours = np.array([config.faceting_full_hours / 2.0])
    both, _ = _index(
        faceting_hours=half_hours, surface_weakening_fraction=np.array([0.5])
    )
    facets_only, _ = _index(
        faceting_hours=half_hours, surface_weakening_fraction=np.array([0.0])
    )
    assert both[0] == pytest.approx(facets_only[0])
    assert both[0] < 1.0


def test_the_stronger_mechanism_is_the_one_that_counts():
    strong_hoar, _ = _index(
        faceting_hours=np.array([0.0]), surface_weakening_fraction=np.array([1.0])
    )
    strong_facets, _ = _index(
        faceting_hours=np.array([1_000.0]),
        surface_weakening_fraction=np.array([0.0]),
    )
    assert strong_hoar[0] == pytest.approx(strong_facets[0])


# ---------------------------------------------------------------------------
# Missing input is unknown, never a favourable zero
# ---------------------------------------------------------------------------


def test_without_a_snow_depth_series_the_index_is_unknown_not_zero():
    """The gradient mechanism cannot be evaluated, so the answer is 'unknown'.

    Falling back to the surface-hoar term alone would report a *smaller* index
    for a cell whose faceting is simply unmeasured. That is the favourable
    reading of an absent input, which is the failure mode this package refuses
    everywhere else.
    """
    index, known = _index(faceting_hours=None, antecedent_snow_depth_m=None)
    assert not known.any()
    assert index.shape == known.shape


def test_a_weighted_configuration_refuses_a_state_that_cannot_supply_the_term():
    """No silent zero when the state was never integrated for stratigraphy."""
    terrain, state, _ = _synthetic_case()
    bare = _port_state(state, antecedent=False)
    with pytest.raises(ValueError, match="weak_loading_weight is non-zero"):
        _score(terrain, bare, replace(V2_BASELINE, weak_loading_weight=0.5))


def test_an_unknown_interface_removes_dry_slab_terrain_and_leaves_the_rest():
    """Unknown is missing input, and a missing input is never a flagged cell.

    It is scoped to dry-slab because wet-snow and dry-loose never read the
    field; suppressing them for a gap in a term they do not use would be a
    different change wearing this one's justification.
    """
    terrain, state, _ = _synthetic_case()
    unknown = _port_state(state, antecedent=True, snow_depth_series_available=False)
    config = replace(V2_BASELINE, weak_loading_weight=0.5)
    scores, active, _ = _score_fields(terrain, unknown, config)
    assert not active[DRY_SLAB].any()
    assert active[WET_SNOW].any() or active[DRY_LOOSE].any()


# ---------------------------------------------------------------------------
# Zero weight is exactly the published engine
# ---------------------------------------------------------------------------


def _synthetic_case():
    from test_release_engine_repair import _synthetic_case as build

    return build()


def _port_state(
    state, *, antecedent: bool, snow_depth_series_available: bool = True
) -> SnowStateV2:
    """The synthetic v1 state as a v2 state, with or without antecedent fields."""
    shape = np.asarray(state.new_snow_index_cm).shape
    extra: dict[str, Any] = {}
    if antecedent:
        extra = {
            "surface_weakening_fraction": np.full(shape, 0.8, dtype="float32"),
            "faceting_hours": (
                np.full(shape, 60.0, dtype="float32")
                if snow_depth_series_available
                else None
            ),
            "antecedent_positive_degree_hours": np.zeros(shape, dtype="float32"),
            "antecedent_rain_mm": np.zeros(shape, dtype="float32"),
            "snow_depth_series_available": snow_depth_series_available,
        }
    return SnowStateV2(
        new_snow_index_cm=state.new_snow_index_cm,
        drift_index_normalized=state.drift_index_normalized,
        drift_from_direction_deg=state.drift_from_direction_deg,
        rain_on_snow_mm=state.rain_on_snow_mm,
        positive_degree_hours=state.positive_degree_hours,
        antecedent_snow_depth_m=state.antecedent_snow_depth_m,
        peak_temperature_c=state.peak_temperature_c,
        mean_storm_temperature_c=state.mean_storm_temperature_c,
        buried_weak_interface_proxy=state.buried_weak_interface_proxy,
        mask=np.asarray(state.mask, dtype=bool),
        **extra,
    )


def _layers(terrain) -> dict[str, np.ndarray]:
    return {
        name: np.asarray(terrain.layer(name).filled(default), dtype="float64")
        for name, default in (
            ("slope", 0.0),
            ("aspect", -1.0),
            ("general_curvature", 0.0),
            ("plan_curvature", 0.0),
            ("forest_mask", 0.0),
            ("elevation", np.nan),
        )
    }


def _score_fields(terrain, state: SnowStateV2, config: ReleaseConfigV2):
    layers = _layers(terrain)
    terrain_mask = np.logical_or.reduce(
        [
            np.ma.getmaskarray(terrain.layer(name))
            for name in (
                "elevation",
                "slope",
                "aspect",
                "general_curvature",
                "plan_curvature",
                "forest_mask",
            )
        ]
    )
    return regime_scores(
        slope=layers["slope"],
        aspect=layers["aspect"],
        general_curvature=layers["general_curvature"],
        plan_curvature=layers["plan_curvature"],
        forest=layers["forest_mask"],
        terrain_mask=terrain_mask,
        state=state,
        insolation=None,
        config=config,
    )


def _score(terrain, state: SnowStateV2, config: ReleaseConfigV2) -> np.ndarray:
    layers = _layers(terrain)
    scores, active, missing = _score_fields(terrain, state, config)
    mask, _ = release_mask(
        scores=scores,
        active=active,
        missing=missing,
        slope=layers["slope"],
        aspect=layers["aspect"],
        elevation=layers["elevation"],
        forest=layers["forest_mask"],
        resolution_m=terrain.grid.resolution_m,
        config=config,
        pad_border=False,
    )
    return mask


def test_stratigraphy_parameters_are_inert_while_the_weight_is_zero():
    """Every weak_* field can move without changing a single cell at weight 0.

    This is what makes ``release-config-search-v1`` still replayable: its sweep
    log carries no weak_* keys at all, so it loads at the defaults, and the
    defaults are numerically absent.
    """
    terrain, state, _ = _synthetic_case()
    port = _port_state(state, antecedent=True)
    reference = _score(terrain, port, V2_BASELINE)
    moved = replace(
        V2_BASELINE,
        weak_faceting_full_hours=1.0,
        weak_burial_minimum_new_snow_cm=0.0,
        weak_burial_full_new_snow_cm=1.0,
        weak_slab_minimum_antecedent_depth_m=0.0,
        weak_rain_destruction_mm=1000.0,
    )
    assert moved.weak_loading_weight == 0.0
    assert np.array_equal(reference, _score(terrain, port, moved))


def test_a_weighted_interface_adds_terrain_and_the_weight_is_what_does_it():
    terrain, state, _ = _synthetic_case()
    port = _port_state(state, antecedent=True)
    without = _score(terrain, port, V2_BASELINE)
    with_weight = _score(terrain, port, replace(V2_BASELINE, weak_loading_weight=1.0))
    assert not np.array_equal(without, with_weight)
    assert with_weight.sum() >= without.sum()


def test_a_weak_interface_carrying_no_load_still_produces_nothing():
    """The veto the stratigraphy search added. A weak layer is not a hazard.

    A configuration that flags terrain for a fully weakened interface under a
    storm that delivered no snow has learned to score the antecedent period
    instead of the storm.
    """
    terrain, state, _ = _synthetic_case()
    shape = np.asarray(state.new_snow_index_cm).shape
    zero = np.zeros(shape, dtype="float32")
    loaded_nothing = SnowStateV2(
        new_snow_index_cm=zero.copy(),
        drift_index_normalized=zero.copy(),
        drift_from_direction_deg=np.full(shape, -1.0, dtype="float32"),
        rain_on_snow_mm=zero.copy(),
        positive_degree_hours=zero.copy(),
        antecedent_snow_depth_m=np.full(shape, 1.0, dtype="float32"),
        peak_temperature_c=np.full(shape, -8.0, dtype="float32"),
        mean_storm_temperature_c=np.full(shape, -8.0, dtype="float32"),
        buried_weak_interface_proxy=zero.copy(),
        mask=np.asarray(state.mask, dtype=bool),
        surface_weakening_fraction=np.ones(shape, dtype="float32"),
        faceting_hours=np.full(shape, 1000.0, dtype="float32"),
        antecedent_positive_degree_hours=zero.copy(),
        antecedent_rain_mm=zero.copy(),
        snow_depth_series_available=True,
    )
    for weight in (0.0, 0.5, 1.5):
        config = replace(V2_BASELINE, weak_loading_weight=weight)
        assert not _score(terrain, loaded_nothing, config).any()


def test_the_manifest_stops_calling_the_proxy_inert_once_it_carries_weight():
    inert = V1_FROZEN.manifest()
    assert "stratigraphy" not in inert
    assert inert["unchanged_from_v1"]["weak_interface_proxy"] == (
        "diagnostic only, zero numerical effect"
    )

    weighted = replace(V1_FROZEN, weak_loading_weight=0.4).manifest()
    assert weighted["stratigraphy"]["loading_weight"] == 0.4
    assert "superseded" in weighted["unchanged_from_v1"]["weak_interface_proxy"]
    # The rest of the manifest is untouched, so a diff between the two is
    # exactly the stratigraphy term and nothing else.
    assert weighted["dry_slab"] == inert["dry_slab"]
    assert weighted["wind"] == inert["wind"]
    assert weighted["morphology"] == inert["morphology"]


def test_the_committed_search_log_still_loads_without_any_weak_fields():
    """The first search's log predates these fields and must still round-trip."""
    log = CONFIG_SEARCH_RESULT.with_name(
        CONFIG_SEARCH_RESULT.stem + "-sweep-log.jsonl"
    )
    seen = 0
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("record") != "screen":
            continue
        assert not any(key.startswith("weak_") for key in entry["config"])
        config = dict(entry["config"])
        for key in ("slope_breakpoints_deg", "slope_scores"):
            config[key] = tuple(config[key])
        assert ReleaseConfigV2(**config).weak_loading_weight == 0.0
        seen += 1
    assert seen == 128


def test_the_stratigraphy_manifest_refuses_to_overclaim():
    manifest = stratigraphy_parameter_manifest()
    assert manifest["every_constant_is_uncalibrated"] is True
    assert manifest["contains_no_snow_profile_observation"] is True
    config = StratigraphyConfig().manifest()
    assert config["is_a_snow_profile_observation"] is False
    assert config["is_a_probability"] is False
    assert config["unknown_is_missing_input_not_zero"] is True


# ---------------------------------------------------------------------------
# The committed artifacts say what the write-up says they say
# ---------------------------------------------------------------------------


def test_the_spot_hourly_rescore_is_labelled_development_and_seals_nothing():
    payload = json.loads(SPOT_FORCING_RESULT.read_bytes())
    assert payload["partition"] == "development_only"
    assert payload["scope"]["is_validated"] is False
    assert payload["scope"]["strict_field_holdout_events_added"] == 0
    integrity = payload["integrity"]
    assert integrity["frozen_experiment_specs_modified"] is False
    assert integrity["frozen_artifacts_or_digests_modified"] is False
    assert integrity["reserved_blocks_predicted_or_scored"] is False
    assert set(integrity["reserved_blocks_still_sealed"]) == RESERVED_BLOCKS
    assert integrity["eligible_mask_matches_frozen_prediction"] is True
    assert integrity["emails_sent"] is False


def test_no_era5_hour_in_any_spot_block_reaches_a_transport_threshold():
    """The wind repair cannot help on this forcing, and the artifact says so.

    This is the finding that separates the two forcings: the plan's defect 1 is
    that a mean dilutes a storm's windy hours below the transport threshold, and
    on ERA5 there are no windy hours to dilute. Every offered statistic, and the
    single strongest hour, sit below both the v1 and the v2 thresholds.
    """
    payload = json.loads(SPOT_FORCING_RESULT.read_bytes())
    assert payload["blocks"], "The artifact scored no blocks."
    for block in payload["blocks"]:
        wind = block["wind"]
        assert wind["hours_at_or_above_v2_dry_transport_threshold"] == 0
        assert wind["hours_at_or_above_v1_wind_transport_minimum"] == 0
        assert wind["every_statistic_below_every_threshold"] is True
        assert wind["maximum_single_hour_kmh"] < wind["v1_wind_transport_minimum_kmh"]


def test_the_searched_configuration_cannot_release_a_dry_slab_on_spot_forcing():
    """Reported as a failure of transfer, not hidden behind the union capture.

    The configuration selected on CERRA traded snow-loading weight for
    wind-loading weight. With ERA5 supplying no transport at all, the terrain
    capability it requires exceeds 1.0 on every block, and a capability above
    1.0 is unreachable because it is a product of factors each bounded by 1.
    """
    payload = json.loads(SPOT_FORCING_RESULT.read_bytes())
    evaluations = {item["configuration_id"]: item for item in payload["evaluations"]}
    searched = evaluations["search_best_configuration"]
    for block in searched["blocks"]:
        saturation = block["dry_slab_saturation"]
        assert saturation["observed_transport_term"] == 0.0
        assert saturation["required_terrain_capability_at_best_cell"] > 1.0
        assert saturation["capability_is_reachable"] is False
        assert block["regime_footprints"][DRY_SLAB]["flagged_eligible_cell_count"] == 0


def test_the_stratigraphy_search_failed_and_spent_no_reserved_block():
    payload = json.loads(STRATIGRAPHY_RESULT.read_bytes())
    assert payload["status"] == "failed_predeclared_success_rule"
    assert payload["partition"] == "development_only"
    acceptance = payload["acceptance"]
    assert acceptance["passed"] is False
    assert acceptance["reserved_block_spent"] is False
    assert acceptance["rule_is_identical_to"] == "release-config-search-v1"
    assert set(acceptance["reserved_blocks_still_sealed"]) == RESERVED_BLOCKS
    assert not any(item["meets_success_rule"] for item in payload["evaluations"])

    # The acceptance rule is the first search's, to the letter and to the number.
    previous = json.loads(CONFIG_SEARCH_RESULT.read_bytes())
    for key in (
        "success_margin_percentage_points",
        "maximum_flagged_eligible_terrain_fraction",
        "plateau_limit",
        "promotion_fraction",
        "configuration_budget",
        "screen_block_id",
    ):
        assert (
            payload["search_declaration"][key] == previous["search_declaration"][key]
        ), key


def test_the_stratigraphy_search_actually_evaluated_its_declared_points():
    """The defect that ended the first execution must not be able to return.

    A plateau counted across a state-key ordering can stop a run before its own
    anchors are scored. Every declared point must appear in the artifact.
    """
    payload = json.loads(STRATIGRAPHY_RESULT.read_bytes())
    declared = set(payload["search_declaration"]["declared_configuration_ids"])
    assert "anchor_search_v1_best" in declared
    assert any(name.startswith("ladder_weak_weight_") for name in declared)
    evaluated = {item["config_id"] for item in payload["evaluations"]}
    assert declared <= evaluated


def test_the_stratigraphy_search_reports_both_sides_of_its_own_question():
    """A comparison is only a comparison if the control is in the artifact."""
    payload = json.loads(STRATIGRAPHY_RESULT.read_bytes())
    effect = payload["stratigraphy_effect"]
    assert effect["best_configuration_using_stratigraphy"] is not None
    assert effect["best_configuration_using_none"] is not None
    assert effect["configurations_using_stratigraphy"] > 0
    assert effect["configurations_using_none"] > 0
    assert effect["best_configuration_using_stratigraphy"]["weak_loading_weight"] != 0.0
    assert effect["best_configuration_using_none"]["weak_loading_weight"] == 0.0
    assert effect["worst_block_margin_change_percentage_points"] == pytest.approx(
        effect["best_configuration_using_stratigraphy"][
            "minimum_margin_percentage_points"
        ]
        - effect["best_configuration_using_none"]["minimum_margin_percentage_points"]
    )
