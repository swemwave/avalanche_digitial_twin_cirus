"""Offline integrity checks for the frozen GEODAR field-consistency result.

The test preserves a failed result as evidence.  It does not download the
external HDF5 inputs, rerun the engine, or promote the one-path comparison to
strict field validation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.bake_identity import parameter_manifest_sha256
from avycore.validation.trust import TRUSTED_DATASET_IDENTITIES_SHA256


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "validation-data/experiments/geodar-along-thalweg-v1.json"
RESULT_PATH = ROOT / "validation-data/results/geodar-along-thalweg-v1.json"

# The scalars the run drew from the M0 manifest, and where they live in it.  The
# runner builds its configuration from the frozen spec, so this is the mapping
# that decides whether the spec still describes the manifest it was taken from.
# particles and lateral_jitter are deliberately absent: the experiment overrides
# both to isolate a single thalweg-following particle.
MANIFEST_SOURCED_PARAMETERS = {
    "open_snow_mu": ("friction", "open_snow"),
    "open_snow_xi_m_per_s2": ("friction", "xi_open"),
    "maximum_steps": ("advanced_mode", "max_steps"),
    "time_step_s": ("advanced_mode", "time_step_s"),
    "stopping_velocity_ms": ("advanced_mode", "stopping_velocity_ms"),
    "random_seed": ("advanced_mode", "random_seed"),
}


def _load(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    return json.loads(raw), raw


def test_geodar_result_is_bound_to_the_frozen_engine_and_spec() -> None:
    spec, spec_bytes = _load(SPEC_PATH)
    result, _ = _load(RESULT_PATH)

    assert spec["frozen_before_model_results"] is True
    assert spec["scientific_use"] == "field_kinematic_consistency"
    assert spec["engine_mode"] == "dynamics_only"
    assert spec["fixed_model"]["parameter_tuning_permitted"] is False
    assert result["spec_sha256"] == hashlib.sha256(spec_bytes).hexdigest()
    assert result["scientific_use"] == spec["scientific_use"]
    assert result["component_tested"] == spec["component_tested"]
    assert result["engine_mode"] == spec["engine_mode"]
    assert result["claim_boundary"] == spec["claim_boundary"]
    assert result["prohibited_claims"] == spec["prohibited_claims"]

    implementation = result["implementation"]
    engine_path = ROOT / implementation["engine_source"]
    parameter_path = ROOT / implementation["parameter_file"]
    runner_path = ROOT / implementation["runner"]
    assert implementation["engine_source_sha256"] == hashlib.sha256(
        engine_path.read_bytes()
    ).hexdigest()
    # The result declares it consumed a SUBTREE of the parameter file
    # (model.parameter_manifest), not the whole file, so that subtree is what is
    # bound here.  The file also carries a separate results block of regression
    # snapshots, which 4edc0cf legitimately refroze after the particle
    # coordinate-integration correction without touching a single parameter.  A
    # whole-file digest conflates the two and reports an output refreeze as
    # parameter drift.  Every other frozen experiment in this repository already
    # binds this way; GEODAR was the outlier.
    baseline = json.loads(parameter_path.read_bytes())
    manifest = baseline["model"]["parameter_manifest"]
    assert implementation["parameter_manifest_pointer"] == (
        "backend/config/m0-baseline.json:model.parameter_manifest"
    )
    assert implementation["parameter_manifest_sha256"] == parameter_manifest_sha256(manifest)
    # The file's own declared manifest digest must agree, so a doctored manifest
    # cannot satisfy the line above by rewriting only the recorded value.
    assert baseline["model"]["sha256"] == implementation["parameter_manifest_sha256"]
    assert implementation["runner_sha256"] == hashlib.sha256(
        runner_path.read_bytes()
    ).hexdigest()
    assert implementation["fixed_model"] == spec["fixed_model"]
    assert implementation["profile_reconstruction"] == spec["profile_reconstruction"]


def test_geodar_parameter_binding_keeps_the_original_whole_file_digest_as_history() -> None:
    """The superseded digest stays on the record; it is history, not a live check.

    Overwriting it with today's file hash would assert the 71-event result was
    produced under the current M0 baseline, which it was not.  It is preserved
    verbatim, still mirrored by the frozen spec, and explicitly not re-asserted.
    """

    spec, _ = _load(SPEC_PATH)
    result, _ = _load(RESULT_PATH)
    implementation = result["implementation"]
    recorded = "eb95b69fb31da6add188389547bfde6dd6a75c4495cfa07e18ea276c705e21b4"

    assert implementation["parameter_file_sha256_at_run"] == recorded
    assert spec["fixed_model"]["parameter_file_sha256_at_freeze"] == recorded
    assert "parameter_file_sha256" not in implementation
    assert implementation["parameter_file_digest_note"].startswith("Historical record")

    # The recorded digest names a working-tree state that was never committed, so
    # it must not be mistaken for a reproducible identity.
    parameter_path = ROOT / implementation["parameter_file"]
    assert hashlib.sha256(parameter_path.read_bytes()).hexdigest() != recorded


def test_geodar_spec_scalars_still_match_the_manifest_they_came_from() -> None:
    """Guard the parameters themselves, not the file that happens to carry them."""

    spec, _ = _load(SPEC_PATH)
    result, _ = _load(RESULT_PATH)
    frozen = spec["fixed_model"]
    baseline = json.loads((ROOT / result["implementation"]["parameter_file"]).read_bytes())
    runout = baseline["model"]["parameter_manifest"]["runout"]

    for name, (section, key) in MANIFEST_SOURCED_PARAMETERS.items():
        assert float(frozen[name]) == float(runout[section][key]), name

    # Both are deliberate single-particle overrides, not manifest values.
    assert frozen["particles"] == 1
    assert frozen["particles"] != runout["advanced_mode"]["particles_per_zone"]
    assert frozen["lateral_jitter"] == 0.0
    assert frozen["lateral_jitter"] != runout["advanced_mode"]["lateral_jitter"]


def test_geodar_result_preserves_the_predeclared_failure() -> None:
    spec, _ = _load(SPEC_PATH)
    result, _ = _load(RESULT_PATH)
    aggregate = result["aggregate"]
    events = result["events"]

    assert result["selection"] == {
        "paired_events_evaluated": 71,
        "rejected_before_scoring": [],
    }
    assert aggregate["events_evaluated"] == 71
    assert aggregate["events_passed"] == 0
    assert aggregate["event_pass_fraction"] == 0.0
    assert aggregate["required_event_pass_fraction"] == 0.8
    assert aggregate["aggregate_pass"] is False
    assert aggregate["median_metrics_over_available_values"] == {
        "velocity_nrmse": 0.360606639,
        "relative_travel_time_rmse": 0.294471793,
        "terminal_surface_distance_relative_error": 0.695451056,
    }

    assert len(events) == 71
    assert len({event["event_id"] for event in events}) == 71
    assert all(event["observed_interval_covered"] for event in events)
    assert all(event["observed_sample_overlap_fraction"] == 1.0 for event in events)
    assert all(event["particles_left_profile"] == 1 for event in events)
    assert all(event["particles_still_moving_at_cutoff"] == 0 for event in events)
    assert not any(event["event_pass"] for event in events)
    assert sum(event["metric_pass"]["velocity_nrmse"] for event in events) == 0
    assert sum(
        event["metric_pass"]["relative_travel_time_rmse"] for event in events
    ) == 20
    assert sum(
        event["metric_pass"]["terminal_surface_distance_relative_error"]
        for event in events
    ) == 10

    thresholds = {metric["name"]: metric["threshold"] for metric in spec["metrics"]}
    for event in events:
        assert len(event["trajectory_md5"]) == 32
        assert len(event["trajectory_sha256"]) == 64
        assert len(event["thalweg_md5"]) == 32
        assert len(event["thalweg_sha256"]) == 64
        for name, value in event["metrics"].items():
            assert event["metric_pass"][name] == (value <= thresholds[name])


def test_geodar_failure_does_not_change_strict_validation_state() -> None:
    result, _ = _load(RESULT_PATH)

    assert result["strict_field_validation_effect"] == {
        "eligible_holdout_events_added": 0,
        "is_validated": False,
        "trusted_registry_changed": False,
        "reason": (
            "The source is one mountain and lacks the complete event inputs, "
            "raw-calibration uncertainty, release/deposit geometry, surveyed "
            "absence, and surface contract required by the strict holdout."
        ),
    }
    assert TRUSTED_DATASET_IDENTITIES_SHA256 == frozenset()
