"""The Stage 3 risk + assess core, exercised on the synthetic baked cone.

No rasterio, no real bake, no ``DATA\\``: the fixture in ``synthetic_baked`` writes a
hermetic cone terrain that ``app.baked`` loads with plain numpy, and everything here
runs against that. The assertions guard the safety-critical behaviour -- above all
invariant I3 (a benign day is reported as low, never as *zero* hazard).
"""

from __future__ import annotations

import hashlib
import json
import math
import gc
import weakref
from pathlib import Path

import avycore
import numpy as np
import pytest
from avycore.hazard import runout as runout_mod

from app import assess as assess_mod
from app import baked as baked_mod
from app import risk
from app.core.settings import Settings
from synthetic_baked import LAT_BOTTOM, LAT_TOP, LON0, LON1, write_synthetic_baked


def _load(tmp_path: Path) -> baked_mod.BakedTerrain:
    write_synthetic_baked(tmp_path)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path,
        data_root=tmp_path,
    )
    return baked_mod.load_baked(settings)


def _all_points(geometry: dict) -> list[list[float]]:
    out: list[list[float]] = []

    def rec(node) -> None:
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            out.append(node)
        elif isinstance(node, (list, tuple)):
            for item in node:
                rec(item)

    rec(geometry["coordinates"])
    return out


def _angular_distance(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


# --- Conditions ---------------------------------------------------------------


def test_conditions_clamped():
    clamped = risk.Conditions(new_snow_cm=999, wind_speed_kmh=-5, wind_direction_deg=400).clamped()
    assert clamped.new_snow_cm == 300.0          # capped at the physical maximum
    assert clamped.wind_speed_kmh == 0.0         # a negative wind speed is meaningless -> 0
    assert 0.0 <= clamped.wind_direction_deg < 360.0
    assert clamped.wind_direction_deg == 40.0    # 400 mod 360


@pytest.mark.parametrize(
    "conditions",
    [
        risk.Conditions(new_snow_cm=float("nan")),
        risk.Conditions(wind_speed_kmh=float("inf")),
        risk.Conditions(wind_direction_deg=float("-inf")),
        risk.Conditions(release_size="unknown"),
    ],
)
def test_conditions_reject_nonphysical_values(conditions: risk.Conditions):
    with pytest.raises(ValueError):
        conditions.clamped()


def test_tests_import_repository_avycore():
    """A green suite must exercise this checkout, not a site-packages release."""
    package_root = Path(__file__).parents[1] / "packages" / "avycore" / "src"
    assert Path(avycore.__file__).resolve().is_relative_to(package_root.resolve())


def test_release_unions_required_layer_masks(tmp_path: Path):
    """A gap in any required terrain term remains missing in the release field."""
    bt = _load(tmp_path)
    aspect = bt.layer("aspect").copy()
    aspect.mask = np.ma.getmaskarray(aspect).copy()
    aspect.mask[10, 10] = True
    bt._cache["aspect"] = aspect

    field = risk.compute_release(bt, risk.Conditions(20, 30, 225))

    assert bool(np.ma.getmaskarray(field.release)[10, 10])


def test_assessment_reports_required_input_intersection_without_filling_gaps(tmp_path: Path):
    bt = _load(tmp_path)
    aspect = bt.layer("aspect").copy()
    aspect.mask = np.ma.getmaskarray(aspect).copy()
    aspect.mask[10, 10] = True
    bt._cache["aspect"] = aspect

    result = assess_mod.assess(bt, risk.Conditions(20, 30, 225), simulation_mode="fast")
    coverage = result["coverage"]["release_model"]

    assert coverage["grid_cell_count"] == 120 * 120
    assert coverage["valid_cell_count"] == 120 * 120 - 1
    assert coverage["missing_cell_count"] == 1
    assert coverage["valid_fraction"] == round((120 * 120 - 1) / (120 * 120), 6)
    assert result["release_potential_index"] == result["hazard_score"]
    assert result["validation"]["field_validation"]["eligible_observation_count"] == 0
    assert result["uncertainty"]["release_potential"]["quantified"] is False


def test_layer_unknown_raises_keyerror(tmp_path: Path):
    bt = _load(tmp_path)
    with pytest.raises(KeyError):
        bt.layer("nonexistent")


# --- Benign day: invariant I3 (missing/low is never a silent zero) ------------


def test_benign_day_is_low_not_zero(tmp_path: Path):
    """No snow, no wind: no release zones, but hazard is NOT reported as zero.

    This is the safety property (I3). Where nothing crosses the release threshold,
    the hazard falls back to a percentile of the release estimate on avalanche
    terrain, and is labelled as such -- a below-threshold day is not "safe".
    """
    bt = _load(tmp_path)
    result = assess_mod.assess(bt, risk.Conditions(0, 0, 225), simulation_mode="fast")

    assert result["zones"] == []
    assert result["hazard_detail"]["zone_count"] == 0
    # The fallback path, not the area-weighted one, and not an error.
    assert "percentile" in result["hazard_detail"]["method"].lower()
    assert result["hazard_score"] > 0.0          # <-- I3: low, never zero
    assert result["risk_level"] == "Very low index"
    assert result["is_probability"] is False
    assert result["is_operational_forecast"] is False
    assert result["disclaimer"]


# --- Loaded day: zones + runout, all geometry inside the AOI ------------------


def test_loaded_day_produces_zones_and_runout(tmp_path: Path):
    bt = _load(tmp_path)
    result = assess_mod.assess(bt, risk.Conditions(50, 60, 225), simulation_mode="fast")

    assert len(result["zones"]) > 0
    assert len(result["runout"]["runout_polygons"]) > 0
    assert result["hazard_score"] > result["hazard_detail"].get("max_zone_score", 0) - 100  # sane
    assert result["is_probability"] is False
    assert result["is_operational_forecast"] is False
    assert result["disclaimer"]


def test_model_fingerprint_covers_release_and_runout_parameters(tmp_path: Path):
    bt = _load(tmp_path)
    result = assess_mod.assess(bt, risk.Conditions(20, 30, 225), simulation_mode="fast")
    expected_config = assess_mod.assessment_parameter_manifest()
    expected_sha = hashlib.sha256(
        json.dumps(expected_config, sort_keys=True).encode("utf-8")
    ).hexdigest()

    assert result["model"] == {
        "model_version": assess_mod.MODEL_VERSION,
        "config_sha256": expected_sha,
        "bake_sha256": bt.bake_sha256,
    }

    # Every release-zone vertex must fall inside the baked AOI bbox. A vertex outside
    # it means the reprojection lattice was misapplied.
    for feature in result["release_zones"]["features"]:
        if not feature["geometry"]:
            continue
        for lon, lat in _all_points(feature["geometry"]):
            assert LON0 <= lon <= LON1, f"lon {lon} outside AOI"
            assert LAT_BOTTOM <= lat <= LAT_TOP, f"lat {lat} outside AOI"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fast", (276600.0, 276650.0, None, 12, 12, 12)),
        # Advanced density uses true particle multiplicity (np.add.at). The old
        # 116,325 m² core came from numpy fancy-index += collapsing duplicate
        # particles in a cell; correcting that bug changes the 40th-percentile
        # core by 1,100 m² (-0.95 %) while leaving the outer envelope and peak
        # velocity unchanged.
        ("advanced", (115225.0, 133125.0, 21.69, 6, 6, 5)),
    ],
)
def test_runout_engines_match_characterized_output(tmp_path: Path, mode: str, expected: tuple):
    """Pin both numerical engines before any library-backed implementation swap."""
    bt = _load(tmp_path)
    result = assess_mod.assess(bt, risk.Conditions(50, 60, 225), simulation_mode=mode)
    runout = result["runout"]

    assert result["hazard_score"] == 68.8
    assert len(result["zones"]) == 14
    assert (
        runout["core_area_m2"],
        runout["uncertainty_area_m2"],
        runout["max_velocity_ms"],
        len(runout["runout_polygons"]),
        len(runout["uncertainty_polygons"]),
        len(runout["main_paths"]),
    ) == expected
    if mode == "fast":
        assert result["random_seed"] is None
    else:
        assert result["random_seed"] == assess_mod.RUNOUT_PARAMS["advanced_mode"]["random_seed"]


def test_assessment_streams_one_full_grid_runout_result_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the real-grid ~659 MiB retained-result memory defect."""

    class StreamingProbeEngine:
        name = "streaming_probe"

        def __init__(self) -> None:
            self.previous: weakref.ReferenceType | None = None
            self.calls = 0

        def simulate(self, *, grid, zone, **kwargs):
            if self.previous is not None:
                gc.collect()
                assert self.previous() is None, "the prior RunoutResult is still retained"
            zeros = np.zeros(grid.shape, dtype="float32")
            result = runout_mod.RunoutResult(
                zone_id=zone.zone_id,
                mode=self.name,
                reached=np.zeros(grid.shape, dtype=bool),
                intensity=zeros,
                velocity=zeros,
                uncertainty=np.zeros(grid.shape, dtype=bool),
                metadata={"call": self.calls},
            )
            self.previous = weakref.ref(result)
            self.calls += 1
            return result

    engine = StreamingProbeEngine()
    monkeypatch.setattr(assess_mod.runout, "get_engine", lambda mode: engine)
    bt = _load(tmp_path)

    result = assess_mod.assess(bt, risk.Conditions(50, 60, 225), simulation_mode="fast")
    gc.collect()

    assert engine.calls == assess_mod.MAX_SIM_ZONES
    assert engine.previous is not None and engine.previous() is None
    assert [item["call"] for item in result["runout"]["per_zone"]] == list(
        range(assess_mod.MAX_SIM_ZONES)
    )


def test_sw_wind_loads_the_ne_flank(tmp_path: Path):
    """A meteorological SW wind (225 deg, blowing FROM the SW) loads the lee NE slopes.

    The dominant release zones should sit on the ~45 deg (NE) flank, not the windward
    SW flank -- the circular mean of the zone aspects lands near NE.
    """
    bt = _load(tmp_path)
    result = assess_mod.assess(bt, risk.Conditions(50, 60, 225), simulation_mode="fast")

    aspects = [
        z["dominant_aspect_deg"] for z in result["zones"] if z["dominant_aspect_deg"] is not None
    ]
    assert aspects, "loaded conditions should yield zones with a dominant aspect"
    radians = np.deg2rad(aspects)
    circular_mean = (
        math.degrees(math.atan2(float(np.sin(radians).mean()), float(np.cos(radians).mean()))) + 360.0
    ) % 360.0

    # The loaded flank is NE (45), not windward SW (225).
    assert _angular_distance(circular_mean, 45.0) <= 90.0
    assert _angular_distance(circular_mean, 45.0) < _angular_distance(circular_mean, 225.0)
