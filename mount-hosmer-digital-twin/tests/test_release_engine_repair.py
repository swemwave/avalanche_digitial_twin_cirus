"""Pin the three release-engine defects, and pin the repair that answers them.

Software verification only. Nothing here measures field accuracy, adds a
validation event, or licenses an accuracy claim.

The defects are documented in ``docs/release-engine-repair-plan.md``:

1. the storm wind reduced to a 72-hour by 9-point arithmetic mean, which put
   every frozen block below the transport threshold and zeroed the model's
   largest weight;
2. with transport identically zero, a release threshold of 55 that no cell
   could reach in three of the four frozen holdout blocks regardless of its
   terrain;
3. a fixed 3x3 opening that enforces a 8100 m^2 minimum zone at 30 m while the
   manifest advertises 2500 m^2.

The first half of this module reproduces each defect from committed artifacts
so it cannot silently return. The second half asserts that
:mod:`avycore.snowpack.release_v2` answers it, and -- most importantly -- that
the repaired module reproduces the frozen v1 engine exactly when it is handed
the frozen v1 configuration. Without that equivalence any later capture
difference would be unattributable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from avycore.hazard import risk
from avycore.hazard.conditions import Conditions
from avycore.snowpack import compute_regime_release, extract_regime_release_zones
from avycore.snowpack.release_v2 import (
    V1_FROZEN,
    V2_BASELINE,
    ReleaseConfigV2,
    derive_threshold,
    effective_minimum_zone_area_m2,
    guardrail_report,
    regime_scores,
    release_mask,
    required_capability,
    storm_window_wind_statistic,
)

SCIENTIFIC_USE = "software_verification"

ROOT = Path(__file__).resolve().parents[1]
SPOT_DEVELOPMENT = ROOT / "validation-data/results/spot-blind-swiss-v1-development.json"
SPOT_HOLDOUT = ROOT / "validation-data/results/spot-blind-swiss-v1-holdout.json"


def _frozen_spot_blocks() -> dict[str, dict[str, Any]]:
    """The conditions and zone counts the frozen SPOT experiment recorded."""
    blocks: dict[str, dict[str, Any]] = {}
    for path in (SPOT_DEVELOPMENT, SPOT_HOLDOUT):
        for block in json.loads(path.read_bytes())["block_results"]:
            summary = block["prediction_summary"]
            blocks[block["block_id"]] = {
                "conditions": summary["conditions"],
                "weather": summary["weather"],
                "zone_count": summary["release"]["zone_count"],
            }
    return blocks


# ---------------------------------------------------------------------------
# Defect 1 -- the wind statistic was an arithmetic mean
# ---------------------------------------------------------------------------


def test_every_frozen_block_wind_fell_below_the_transport_threshold():
    """The recorded block winds are all below ``WIND_TRANSPORT_MIN_KMH``.

    That is not a claim the Alps were calm during four mapped avalanche cycles.
    It is what averaging 72 hours across nine points does to a threshold
    process, and it set the model's largest weight to exactly zero everywhere.
    """
    blocks = _frozen_spot_blocks()
    assert blocks, "The frozen SPOT results must contain block records."
    for block_id, record in blocks.items():
        speed = float(record["conditions"]["wind_speed_kmh"])
        assert speed < risk.WIND_TRANSPORT_MIN_KMH, block_id
        transport = np.clip(
            (speed - risk.WIND_TRANSPORT_MIN_KMH)
            / (risk.WIND_TRANSPORT_FULL_KMH - risk.WIND_TRANSPORT_MIN_KMH),
            0.0,
            1.0,
        )
        assert float(transport) == 0.0, block_id


def test_the_recorded_wind_is_exactly_the_arithmetic_mean_statistic():
    """Identify the defect precisely: the runner published a plain mean."""
    for block_id, record in _frozen_spot_blocks().items():
        recorded = float(record["weather"]["wind_speed_kmh_scalar_mean"])
        assert round(recorded, 1) == pytest.approx(
            float(record["conditions"]["wind_speed_kmh"])
        ), block_id


def test_an_arithmetic_mean_hides_a_transporting_storm_and_the_repair_does_not():
    """Six hours at 45 km/h inside a calm 72-hour window.

    The mean reports about 6 km/h -- below every transport threshold. The
    transport-preserving statistics keep the burst visible. None of them is a
    measurement of ridgetop wind; they are proxies that do not destroy the
    signal before the model sees it.
    """
    repairs = ("quantile", "transporting_hours_mean", "drift_weighted_mean")
    speeds = np.full(72, 3.0)
    speeds[:12] = 45.0

    mean = storm_window_wind_statistic(speeds, statistic="arithmetic_mean")
    assert mean == pytest.approx(10.0)
    assert mean < risk.WIND_TRANSPORT_MIN_KMH

    for statistic in repairs:
        repaired = storm_window_wind_statistic(speeds, statistic=statistic)
        assert repaired > risk.WIND_TRANSPORT_MIN_KMH, statistic
        assert repaired <= speeds.max(), statistic

    # A genuinely calm storm must stay calm under every statistic. A statistic
    # that floors at the transport threshold would invent loading on a quiet
    # day, which is the same class of error in the opposite direction.
    calm = np.full(72, 3.0)
    for statistic in ("arithmetic_mean", *repairs):
        assert storm_window_wind_statistic(calm, statistic=statistic) == pytest.approx(
            3.0
        ), statistic


def test_missing_wind_is_refused_rather_than_gap_filled():
    speeds = np.full(24, 20.0)
    speeds[3] = np.nan
    with pytest.raises(ValueError, match="never gap-filled"):
        storm_window_wind_statistic(speeds, statistic="drift_weighted_mean")


# ---------------------------------------------------------------------------
# Defect 2 -- with transport zero the threshold was arithmetically unreachable
# ---------------------------------------------------------------------------


def test_the_frozen_zero_zone_blocks_required_an_impossible_capability():
    """Reproduce the saturation bound the plan derived, block by block.

    ``capability`` is a product of factors each bounded above by 1, so a
    required capability above 1.0 cannot be met by any terrain whatsoever. That
    is the mechanism behind "three of four blocks produced zero release zones";
    it is a saturation failure, not a subtle miscalibration.
    """
    blocks = _frozen_spot_blocks()
    unreachable = {"holdout_albula", "holdout_silvretta"}
    for block_id in unreachable:
        record = blocks[block_id]
        needed = required_capability(
            float(record["conditions"]["new_snow_cm"]), transport=0.0, config=V1_FROZEN
        )
        assert needed > 1.0, (block_id, needed)
        assert record["zone_count"] == 0, block_id

    # Gotthard needed a near-perfect cell and also produced nothing.
    gotthard = required_capability(
        float(blocks["holdout_gotthard"]["conditions"]["new_snow_cm"]),
        transport=0.0,
        config=V1_FROZEN,
    )
    assert 0.98 < gotthard <= 1.0
    assert blocks["holdout_gotthard"]["zone_count"] == 0

    # The two blocks whose snow cleared the bar are the two that produced zones.
    for block_id in ("holdout_glarus", "dev_western_bernese"):
        needed = required_capability(
            float(blocks[block_id]["conditions"]["new_snow_cm"]),
            transport=0.0,
            config=V1_FROZEN,
        )
        assert needed < 1.0, block_id
        assert blocks[block_id]["zone_count"] > 0, block_id


def test_no_terrain_at_all_can_release_under_the_frozen_albula_forcing():
    """Not "little terrain" -- none, on a deliberately ideal mountain.

    Every cell is 40 degrees (the peak of the slope response), unforested, and
    convex, facing straight into the lee of the recorded wind. The frozen
    engine still emits zero zones, because the loading term caps the score
    below the threshold before terrain is consulted.
    """
    shape = (40, 40)
    record = _frozen_spot_blocks()["holdout_albula"]
    conditions = Conditions(
        new_snow_cm=float(record["conditions"]["new_snow_cm"]),
        wind_speed_kmh=float(record["conditions"]["wind_speed_kmh"]),
        wind_direction_deg=float(record["conditions"]["wind_direction_deg"]),
    )
    terrain = _ideal_terrain(shape, conditions.wind_direction_deg)
    field = risk.compute_release(terrain, conditions)
    assert float(np.max(field.release)) < risk.RELEASE_THRESHOLD
    zone_set = risk.extract_release_zones(terrain, field, conditions)
    assert zone_set.zones == []
    assert any("NOT a statement that the mountain is safe" in w for w in zone_set.warnings)


def test_the_same_ideal_terrain_releases_once_the_wind_term_is_restored():
    """Control for the test above: terrain is not the reason it produced nothing."""
    shape = (40, 40)
    record = _frozen_spot_blocks()["holdout_albula"]
    repaired_wind = storm_window_wind_statistic(
        np.concatenate([np.full(12, 55.0), np.full(60, 3.0)]),
        statistic="drift_weighted_mean",
    )
    conditions = Conditions(
        new_snow_cm=float(record["conditions"]["new_snow_cm"]),
        wind_speed_kmh=repaired_wind,
        wind_direction_deg=float(record["conditions"]["wind_direction_deg"]),
    )
    terrain = _ideal_terrain(shape, conditions.wind_direction_deg)
    field = risk.compute_release(terrain, conditions)
    assert float(np.max(field.release)) >= risk.RELEASE_THRESHOLD


def _ideal_terrain(shape: tuple[int, int], wind_direction_deg: float):
    """Uniform 40-degree, unforested, convex ground facing the lee direction."""
    from test_release_regimes import _Terrain  # reuse the protocol stub

    lee = (wind_direction_deg + 180.0) % 360.0
    return _Terrain(
        {
            "elevation": np.ma.array(np.full(shape, 2400.0, dtype="float32")),
            "slope": np.ma.array(np.full(shape, 40.0, dtype="float32")),
            "aspect": np.ma.array(np.full(shape, lee, dtype="float32")),
            "general_curvature": np.ma.array(np.full(shape, 1.0, dtype="float32")),
            "plan_curvature": np.ma.array(np.full(shape, -1.0, dtype="float32")),
            "forest_mask": np.ma.array(np.zeros(shape, dtype="float32")),
        }
    )


def test_a_derived_threshold_records_the_operating_point_it_came_from():
    rng = np.random.default_rng(20260818)
    scores = [rng.uniform(0.0, 100.0, size=(50, 50))]
    admissible = [np.ones((50, 50), dtype=bool)]
    threshold, derivation = derive_threshold(
        scores, admissible, target_flagged_fraction=0.10
    )
    assert 85.0 < threshold < 95.0
    assert derivation["target_flagged_fraction_of_admissible_terrain"] == 0.10
    assert derivation["method"].startswith("quantile_of_admissible")
    assert derivation["pre_morphology"] is True
    with pytest.raises(ValueError):
        derive_threshold(scores, admissible, target_flagged_fraction=1.5)


# ---------------------------------------------------------------------------
# Defect 3 -- the undeclared minimum zone size
# ---------------------------------------------------------------------------


def test_the_frozen_manifest_understates_the_minimum_zone_area_it_enforces():
    """v1 advertises 2500 m^2 and enforces 8100 m^2 at the 30 m hindcast grid."""
    advertised = risk.parameter_manifest()["minimum_zone_area_m2"]
    assert advertised == 2500.0
    effective = effective_minimum_zone_area_m2(
        opening_structure="square3", minimum_zone_area_m2=advertised, resolution_m=30.0
    )
    assert effective == 8100.0
    assert effective > advertised

    # And the smoothing radius silently collapses to a 3x3 closing.
    assert max(1, int(round(risk.SMOOTHING_RADIUS_M / 30.0))) == 1


def test_a_solid_3x3_block_is_the_real_floor_under_the_frozen_opening():
    """Demonstrate it on a raster: an 8-cell L survives labelling but not opening."""
    from scipy import ndimage

    region = np.zeros((9, 9), dtype=bool)
    region[2:5, 2:4] = True  # 6 cells, wider than the 2500 m^2 / 900 = 3 cell floor
    assert region.sum() * 900.0 > 2500.0
    opened = ndimage.binary_opening(region, structure=np.ones((3, 3), dtype=bool))
    assert not opened.any()


def test_the_repaired_manifest_states_the_effective_minimum_area():
    manifest = V2_BASELINE.manifest(resolution_m=30.0)["morphology"]
    assert manifest["declared_minimum_zone_area_m2"] == 2500.0
    assert manifest["effective_minimum_zone_area_m2"] == effective_minimum_zone_area_m2(
        opening_structure=V2_BASELINE.opening_structure,
        minimum_zone_area_m2=V2_BASELINE.minimum_zone_area_m2,
        resolution_m=30.0,
    )
    assert manifest["closing_radius_px"] == 1
    assert manifest["radius_rounding"] == "ceil"

    # Every offered structure reports its own true floor rather than the
    # advertised one.
    assert effective_minimum_zone_area_m2(
        opening_structure="cross", minimum_zone_area_m2=2500.0, resolution_m=30.0
    ) == 4500.0
    assert effective_minimum_zone_area_m2(
        opening_structure="none", minimum_zone_area_m2=2500.0, resolution_m=30.0
    ) == 2700.0


# ---------------------------------------------------------------------------
# The repair reproduces the frozen engine exactly at the frozen configuration
# ---------------------------------------------------------------------------


def _synthetic_case(shape=(48, 48)):
    """A varied synthetic block: every regime active somewhere, some cells masked."""
    from test_release_regimes import _Terrain, _state

    rows, cols = np.indices(shape)
    slope = (20.0 + 45.0 * np.sin(rows / 7.0) ** 2 + 8.0 * np.cos(cols / 5.0)).astype("float32")
    aspect = ((rows * 13 + cols * 29) % 360).astype("float32")
    elevation = (1500.0 + 12.0 * rows + 5.0 * cols).astype("float32")
    general = (np.sin(rows / 4.0) * np.cos(cols / 6.0)).astype("float32")
    plan = (np.cos(rows / 5.0) * np.sin(cols / 3.0)).astype("float32")
    forest = np.clip(np.sin(cols / 9.0), 0.0, 1.0).astype("float32")
    missing = np.zeros(shape, dtype=bool)
    missing[40:44, 5:9] = True

    terrain = _Terrain(
        {
            "elevation": np.ma.array(elevation, mask=missing),
            "slope": np.ma.array(slope, mask=missing),
            "aspect": np.ma.array(aspect, mask=missing),
            "general_curvature": np.ma.array(general, mask=missing),
            "plan_curvature": np.ma.array(plan, mask=missing),
            "forest_mask": np.ma.array(forest, mask=missing),
        }
    )
    state = _state(
        shape,
        new_snow_index_cm=np.full(shape, 42.0, dtype="float32"),
        independent_peak_new_snow_index_cm=np.full(shape, 42.0, dtype="float32"),
        drift_index_normalized=np.clip(np.sin(rows / 6.0), 0.0, 1.0).astype("float32"),
        drift_from_direction_deg=((cols * 7) % 360).astype("float32"),
        rain_on_snow_mm=np.full(shape, 6.0, dtype="float32"),
        positive_degree_hours=np.full(shape, 14.0, dtype="float32"),
        antecedent_snow_depth_m=np.full(shape, 1.2, dtype="float32"),
        peak_temperature_c=np.full(shape, -4.0, dtype="float32"),
        mean_storm_temperature_c=np.full(shape, -3.0, dtype="float32"),
        mask=missing,
    )
    return terrain, state, missing


def _v1_release_mask(terrain, state) -> np.ndarray:
    field = compute_regime_release(terrain, state)
    zone_set = extract_regime_release_zones(terrain, field, state_layers={})
    mask = np.zeros(terrain.grid.shape, dtype=bool)
    for zone in zone_set.zones:
        mask |= zone.pixels
    return mask


def _v2_release_mask(terrain, state, config: ReleaseConfigV2) -> np.ndarray:
    from avycore.snowpack.release_v2 import SnowStateV2

    port = SnowStateV2(
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
    )
    layers = {
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
    scores, active, missing = regime_scores(
        slope=layers["slope"],
        aspect=layers["aspect"],
        general_curvature=layers["general_curvature"],
        plan_curvature=layers["plan_curvature"],
        forest=layers["forest_mask"],
        terrain_mask=terrain_mask,
        state=port,
        insolation=None,
        config=config,
    )
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


def test_the_repaired_module_reproduces_the_frozen_engine_at_the_frozen_config():
    """Equivalence is the whole basis for attributing any later difference.

    On the real frozen blocks this module also reproduces all five committed
    ``regime-hindcast-v1`` development release masks cell-for-cell; that check
    needs the external source archive, so the in-repo guarantee is this
    synthetic one.
    """
    terrain, state, _ = _synthetic_case()
    expected = _v1_release_mask(terrain, state)
    assert expected.any(), "The synthetic case must produce release terrain."
    actual = _v2_release_mask(terrain, state, V1_FROZEN)
    assert np.array_equal(expected, actual)


def test_the_repair_changes_the_result_only_where_it_claims_to():
    """A morphology-only repair must move cells, and only through morphology."""
    terrain, state, _ = _synthetic_case()
    frozen = _v2_release_mask(terrain, state, V1_FROZEN)
    repaired = _v2_release_mask(terrain, state, V2_BASELINE)
    assert not np.array_equal(frozen, repaired)
    assert repaired.sum() > frozen.sum()

    # With the per-regime zone cap lifted, dropping the opening can only add
    # terrain: an opening is anti-extensive, so removing it is a superset. Under
    # the cap it can also displace, because admitting more zones changes which
    # forty are the largest -- a property of the cap, not of the morphology.
    uncapped = {"maximum_zones_per_regime": 10_000}
    frozen_uncapped = _v2_release_mask(terrain, state, replace(V1_FROZEN, **uncapped))
    repaired_uncapped = _v2_release_mask(terrain, state, replace(V2_BASELINE, **uncapped))
    assert int((frozen_uncapped & ~repaired_uncapped).sum()) == 0


# ---------------------------------------------------------------------------
# Guardrails a configuration must pass regardless of its capture score
# ---------------------------------------------------------------------------


def test_a_benign_day_still_produces_no_release_terrain():
    """No new snow, no drift, no rain, no melt -- on capable terrain."""
    from test_release_regimes import _Terrain, _state

    shape = (48, 48)
    rows, _cols = np.indices(shape)
    terrain = _Terrain(
        {
            "elevation": np.ma.array((2000.0 + 10.0 * rows).astype("float32")),
            "slope": np.ma.array(np.full(shape, 38.0, dtype="float32")),
            "aspect": np.ma.array(np.full(shape, 90.0, dtype="float32")),
            "general_curvature": np.ma.array(np.ones(shape, dtype="float32")),
            "plan_curvature": np.ma.array(-np.ones(shape, dtype="float32")),
            "forest_mask": np.ma.array(np.zeros(shape, dtype="float32")),
        }
    )
    benign = _state(shape)
    for config in (V1_FROZEN, V2_BASELINE):
        assert not _v2_release_mask(terrain, benign, config).any(), config.config_id


def test_missing_input_cells_are_never_flagged():
    terrain, state, missing = _synthetic_case()
    for config in (V1_FROZEN, V2_BASELINE):
        mask = _v2_release_mask(terrain, state, config)
        assert not bool((mask & missing).any()), config.config_id


def test_the_guardrail_report_rejects_a_configuration_that_lights_up_a_calm_day():
    quiet = guardrail_report(
        benign_release_cell_count=0,
        benign_eligible_cell_count=100_000,
        flagged_outside_eligible_cell_count=0,
        flagged_on_missing_input_cell_count=0,
    )
    assert quiet["passed"] is True

    noisy = guardrail_report(
        benign_release_cell_count=5_000,
        benign_eligible_cell_count=100_000,
        flagged_outside_eligible_cell_count=0,
        flagged_on_missing_input_cell_count=0,
    )
    assert noisy["passed"] is False
    assert noisy["checks"]["benign_day_quiet"] is False

    leaky = guardrail_report(
        benign_release_cell_count=0,
        benign_eligible_cell_count=100_000,
        flagged_outside_eligible_cell_count=0,
        flagged_on_missing_input_cell_count=7,
    )
    assert leaky["passed"] is False
    assert leaky["checks"]["no_flag_on_missing_input"] is False


def test_the_frozen_v1_configuration_is_exactly_the_published_constants():
    """The v2 defaults must be v1, or the equivalence test proves nothing."""
    assert V1_FROZEN.release_threshold == risk.RELEASE_THRESHOLD
    assert V1_FROZEN.loading_base == risk.LOADING_BASE
    assert V1_FROZEN.snow_loading_weight == risk.SNOW_LOADING_WEIGHT
    assert V1_FROZEN.wind_loading_weight == risk.WIND_LOADING_WEIGHT
    assert V1_FROZEN.new_snow_full_cm == risk.NEW_SNOW_FULL_CM
    assert V1_FROZEN.forest_damping_max == risk.FOREST_DAMPING_MAX
    assert list(V1_FROZEN.slope_breakpoints_deg) == list(risk.SLOPE_BREAKPOINTS_DEG)
    assert list(V1_FROZEN.slope_scores) == list(risk.SLOPE_SCORES)
    assert V1_FROZEN.slope_min_deg == risk.SLOPE_MIN_DEG
    assert V1_FROZEN.slope_max_deg == risk.SLOPE_MAX_DEG
    assert V1_FROZEN.minimum_zone_area_m2 == risk.MIN_ZONE_AREA_M2
    assert V1_FROZEN.smoothing_radius_m == risk.SMOOTHING_RADIUS_M
    assert V1_FROZEN.maximum_zones_per_regime == risk.MAX_ZONES
    assert V1_FROZEN.opening_structure == "square3"


# ---------------------------------------------------------------------------
# The committed search artifact
# ---------------------------------------------------------------------------

SEARCH_RESULT = ROOT / "validation-data/results/release-config-search-v1.json"
SEARCH_LOG = ROOT / "validation-data/results/release-config-search-v1-sweep-log.jsonl"


def test_the_configuration_search_failed_and_spent_no_reserved_block():
    """The search's own artifact must state the failure and the sealed blocks."""
    result = json.loads(SEARCH_RESULT.read_bytes())
    assert result["status"] == "failed_predeclared_success_rule"
    assert result["acceptance"]["passed"] is False
    assert result["acceptance"]["reserved_block_spent"] is False
    assert sorted(result["acceptance"]["reserved_blocks_still_sealed"]) == sorted(
        ["row1col4", "row2col4", "row6col9", "row5col10"]
    )
    integrity = result["integrity"]
    assert integrity["reserved_blocks_predicted_or_scored"] is False
    assert integrity["reserved_block_outlines_opened"] is False
    assert integrity["frozen_experiment_specs_modified"] is False
    assert integrity["frozen_digests_rewritten"] is False
    assert result["partition"] == "development_only"

    # No configuration may be recorded as meeting the rule while the artifact
    # reports failure, and none may claim a margin the blocks do not support.
    assert not any(item["meets_success_rule"] for item in result["evaluations"])
    for item in result["evaluations"]:
        margins = [
            block["capture_margin_percentage_points"] for block in item["blocks"]
        ]
        assert item["minimum_margin_percentage_points"] == pytest.approx(min(margins))
        assert item["minimum_margin_percentage_points"] < 5.0


def test_the_search_log_accounts_for_every_configuration_evaluated():
    """The reported count must be the log's count, losers included."""
    result = json.loads(SEARCH_RESULT.read_bytes())
    screens = [
        json.loads(line)
        for line in SEARCH_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["record"] == "screen"
    ]
    assert len(screens) == result["stop_condition"]["configurations_evaluated"]
    assert len(screens) > 100, "The reported search must actually have been run."
    assert hashlib.sha256(SEARCH_LOG.read_bytes()).hexdigest() == result["sweep_log_sha256"]

    # PLATEAU means the running best stopped improving for the declared run of
    # configurations. Re-derive it from the log rather than trusting the label.
    assert result["stop_condition"]["reason"] == "PLATEAU"
    best, last_improvement = -float("inf"), 0
    for entry in screens:
        margin = entry["screen"]["capture_margin_percentage_points"]
        if margin > best:
            best, last_improvement = margin, entry["index"]
    assert screens[-1]["index"] - last_improvement == result["search_declaration"][
        "plateau_limit"
    ]
