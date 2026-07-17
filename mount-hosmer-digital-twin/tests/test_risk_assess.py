"""The Stage 3 risk + assess core, exercised on the synthetic baked cone.

No rasterio, no real bake, no ``DATA\\``: the fixture in ``synthetic_baked`` writes a
hermetic cone terrain that ``app.baked`` loads with plain numpy, and everything here
runs against that. The assertions guard the safety-critical behaviour -- above all
invariant I3 (a benign day is reported as low, never as *zero* hazard).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

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
    assert result["risk_level"] == "Low"
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

    # Every release-zone vertex must fall inside the baked AOI bbox. A vertex outside
    # it means the reprojection lattice was misapplied.
    for feature in result["release_zones"]["features"]:
        if not feature["geometry"]:
            continue
        for lon, lat in _all_points(feature["geometry"]):
            assert LON0 <= lon <= LON1, f"lon {lon} outside AOI"
            assert LAT_BOTTOM <= lat <= LAT_TOP, f"lat {lat} outside AOI"


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
