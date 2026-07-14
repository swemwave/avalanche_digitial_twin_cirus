"""Runout stopping, missing-data exclusion, and idempotent persistence.

The runout tests exist because of a failure that looked like a working model. The
fast engine routes flow to every downhill neighbour and used to mark a cell as
"reached" on geometry alone -- if the cell sat inside the alpha cone, it counted.
But multi-directional routing leaks a vanishing trickle into every cell in the
catchment, so "inside the alpha cone" became "reached", and a single medium release
inundated 3,538 hectares: a quarter of the entire 12x12 km AOI. Nothing crashed.
The polygons looked like avalanche paths.

The alpha angle bounds how FAR snow can run. It does not say snow reaches
everywhere within that bound. Flow that has thinned to nothing has stopped.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.model_config import ModelConfig, load_model_config
from app.core.settings import get_settings
from app.models.release_zones import ReleaseZone
from app.processing.harmonization.grids import AnalysisGrid
from app.simulation import runout

GRID = AnalysisGrid("test", 10.0, 0.0, 0.0, 1200.0, 1200.0, "EPSG:26911")


def config() -> ModelConfig:
    return load_model_config(get_settings())


def uniform_slope(angle_deg: float = 35.0) -> np.ma.MaskedArray:
    """A planar slope falling to the south, so the fall line is unambiguous."""
    rows, _ = np.mgrid[0:120, 0:120].astype("float64")
    drop_per_cell = np.tan(np.deg2rad(angle_deg)) * GRID.resolution_m
    return np.ma.array(
        (2500.0 - rows * drop_per_cell).astype("float32"),
        mask=np.zeros((120, 120), dtype=bool),
    )


def slope_then_flat(steep_deg: float = 38.0, break_row: int = 40) -> np.ma.MaskedArray:
    """Steep above, dead flat below. An avalanche must stop out on the flat."""
    rows, _ = np.mgrid[0:120, 0:120].astype("float64")
    drop = np.tan(np.deg2rad(steep_deg)) * GRID.resolution_m
    z = np.where(rows <= break_row, 2500.0 - rows * drop, 2500.0 - break_row * drop)
    return np.ma.array(z.astype("float32"), mask=np.zeros((120, 120), dtype=bool))


def zone_at_top(elevation: np.ma.MaskedArray, zone_id: str = "RZ001") -> ReleaseZone:
    pixels = np.zeros(elevation.shape, dtype=bool)
    pixels[2:8, 55:65] = True  # a small slab near the crest
    return ReleaseZone(
        zone_id=zone_id,
        geometry={},
        geometry_utm={},
        properties={"zone_id": zone_id, "estimated_release_score": 70.0},
        pixels=pixels,
    )


def flat(shape) -> np.ma.MaskedArray:
    return np.ma.array(np.zeros(shape, dtype="float32"), mask=np.zeros(shape, dtype=bool))


def run_fast(elevation: np.ma.MaskedArray, cfg: ModelConfig) -> runout.RunoutResult:
    zone = zone_at_top(elevation)
    return runout.get_engine("fast").simulate(
        zone=zone,
        grid=GRID,
        elevation=elevation,
        slope=flat(elevation.shape),
        forest_mask=flat(elevation.shape),
        plan_curvature=flat(elevation.shape),
        config=cfg,
        release_size="medium",
        seed=42,
    )


# --- Runout stopping ----------------------------------------------------------


def test_runout_does_not_flood_the_whole_catchment() -> None:
    """Being inside the alpha cone is necessary to be reached, not sufficient."""
    elevation = slope_then_flat()
    result = run_fast(elevation, config())

    reached_area = float(result.reached.sum()) * GRID.resolution_m**2
    total_area = float(elevation.size) * GRID.resolution_m**2
    fraction = reached_area / total_area

    assert fraction < 0.35, (
        f"The runout covered {fraction:.0%} of the grid from one small slab. Flow is leaking "
        f"into every downhill cell instead of stopping when it thins out."
    )
    assert result.reached.any(), "The avalanche has to go somewhere."


def test_runout_stops_at_the_alpha_angle() -> None:
    """The furthest point reached must sit at or above the configured alpha."""
    cfg = config()
    elevation = slope_then_flat()
    result = run_fast(elevation, cfg)

    alpha = cfg.require("runout.alpha_angle_deg")["medium"]
    envelope = alpha - float(cfg.require("runout.alpha_uncertainty_deg"))

    zone = zone_at_top(elevation)
    start_row, start_col, start_z = runout._start_point(zone, elevation)
    z = np.asarray(elevation.filled(np.nan), dtype="float64")

    outside = result.reached & ~zone.pixels
    assert outside.any(), "Nothing ran out of the release zone at all."

    pixels = np.argwhere(outside)
    horizontal = np.hypot(
        (pixels[:, 0] - start_row) * GRID.resolution_m,
        (pixels[:, 1] - start_col) * GRID.resolution_m,
    )
    furthest = pixels[int(np.argmax(horizontal))]
    run = float(horizontal.max())
    drop = start_z - float(z[furthest[0], furthest[1]])
    achieved = float(np.degrees(np.arctan(drop / run)))

    assert achieved >= envelope - 1.0, (
        f"The runout reached an angle of reach of {achieved:.1f} deg, past the {envelope:.0f} deg "
        f"envelope. The alpha stopping criterion is not being applied."
    )


def test_a_thinner_flow_cutoff_never_grows_the_runout() -> None:
    """minimum_flux is a stopping criterion: raising it can only shorten the runout.

    If a higher cutoff ever produced a LARGER runout, flux would be being counted
    somewhere it is not being propagated, and the field would be incoherent.
    """
    import copy

    elevation = slope_then_flat()
    base = load_model_config(get_settings())
    areas = []
    for cutoff in (0.005, 0.02, 0.10):
        raw = copy.deepcopy(base._data)  # noqa: SLF001 - test needs a perturbed config
        raw["runout"]["fast_mode"]["minimum_flux"] = cutoff
        cfg = ModelConfig(raw, base.path, f"test-{cutoff}")
        areas.append(int(run_fast(elevation, cfg).reached.sum()))

    assert areas == sorted(areas, reverse=True), (
        f"Raising the flux cutoff did not monotonically shrink the runout: {areas}"
    )


def test_an_empty_release_zone_produces_no_runout() -> None:
    elevation = uniform_slope()
    zone = ReleaseZone("RZ_EMPTY", {}, {}, {"zone_id": "RZ_EMPTY"}, np.zeros(elevation.shape, bool))
    result = runout.get_engine("fast").simulate(
        zone=zone,
        grid=GRID,
        elevation=elevation,
        slope=flat(elevation.shape),
        forest_mask=flat(elevation.shape),
        plan_curvature=flat(elevation.shape),
        config=config(),
        release_size="medium",
        seed=1,
    )
    assert not result.reached.any()
    assert result.warnings


def _advanced(elevation: np.ma.MaskedArray, cfg: ModelConfig, size: str = "medium"):
    zone = zone_at_top(elevation)
    return runout.get_engine("advanced").simulate(
        zone=zone,
        grid=GRID,
        elevation=elevation,
        slope=flat(elevation.shape),
        forest_mask=flat(elevation.shape),
        plan_curvature=flat(elevation.shape),
        config=cfg,
        release_size=size,
        seed=42,
    )


def test_the_energy_line_bounds_the_advanced_runout() -> None:
    """Voellmy alone cannot stop these particles, and no choice of mu fixes it.

    A particle keeps moving while tan(theta) > mu, so mu sets the local slope at
    which it may finally rest -- mu = 0.20 means it coasts until the ground flattens
    below 11 degrees. A dimensionless particle carries no mass, so it cannot shed
    energy by spreading, thinning and depositing the way a real avalanche does, and
    one that finds a steep drainage rides it to the floor of the AOI. Sweeping mu
    from 0.20 to 0.60 on the real terrain never fixed the tail.

    The energy line is what stops it: a parcel may not travel below the line drawn
    from its own release point at the alpha envelope.
    """
    cfg = config()
    result = _advanced(slope_then_flat(steep_deg=40.0, break_row=25), cfg)

    alpha = cfg.require("runout.alpha_angle_deg")["medium"]
    envelope = alpha - float(cfg.require("runout.alpha_uncertainty_deg"))

    achieved = result.metadata["alpha_angle_achieved_deg"]
    assert achieved is not None, "The ensemble never ran anywhere."
    assert achieved >= envelope - 1.0, (
        f"The deposit tip reached {achieved:.1f} deg, below the {envelope:.0f} deg energy line. "
        f"The bound is not holding and the runout has no natural end."
    )
    assert result.metadata["alpha_envelope_exceeded"] is False


def test_a_bigger_release_is_allowed_to_run_further() -> None:
    """Release size sets alpha, and alpha bounds the runout.

    This is what makes truncating at the energy line safe rather than an
    under-estimate of the hazard: if you want the longer runout, you simulate the
    bigger avalanche. A very large release has a shallower alpha and must reach
    further than a small one over the same terrain.
    """
    cfg = config()
    elevation = slope_then_flat(steep_deg=40.0, break_row=25)

    small = _advanced(elevation, cfg, "small").metadata
    very_large = _advanced(elevation, cfg, "very_large").metadata

    assert very_large["runout_length_m"] > small["runout_length_m"], (
        "A very large release must run further than a small one on the same slope."
    )
    assert very_large["alpha_angle_achieved_deg"] < small["alpha_angle_achieved_deg"], (
        "A very large release must reach a shallower angle than a small one."
    )


def test_the_runout_tip_is_not_measured_from_a_stalled_particle() -> None:
    """The angle of reach describes the deposit tip, not the ensemble's worst dud.

    Most particles never run anywhere. One that has crept ten metres across a bench
    while dropping one has an angle of reach near zero; reporting the minimum over
    all particles would present that stalled parcel as the runout of the avalanche.
    """
    result = _advanced(slope_then_flat(), config())
    achieved = result.metadata["alpha_angle_achieved_deg"]
    assert achieved is not None
    assert 15.0 < achieved < 60.0, (
        f"An angle of reach of {achieved} is not a runout; it is a particle that never moved."
    )
    assert result.metadata["runout_length_m"] > GRID.resolution_m * 5


def test_advanced_engine_is_reproducible_from_its_seed() -> None:
    elevation = slope_then_flat()
    cfg = config()
    runs = []
    for _ in range(2):
        zone = zone_at_top(elevation)
        runs.append(
            runout.get_engine("advanced").simulate(
                zone=zone,
                grid=GRID,
                elevation=elevation,
                slope=flat(elevation.shape),
                forest_mask=flat(elevation.shape),
                plan_curvature=flat(elevation.shape),
                config=cfg,
                release_size="medium",
                seed=7,
            ).reached
        )
    assert np.array_equal(runs[0], runs[1]), "The same seed must replay bit-for-bit."


# --- Persistence --------------------------------------------------------------


def test_saving_the_same_analysis_twice_is_idempotent(tmp_path) -> None:
    """An analysis id names one reproducible computation, not one INSERT.

    A retried job re-saves the same analysis. Before this, that raised an
    IntegrityError on the unique constraint -- which made the job runner's
    idempotency key, whose entire purpose is to let work be safely repeated,
    useless the moment it was actually needed.
    """
    import dataclasses

    from app.storage import database, repository

    # Settings is frozen, and a throwaway database keeps the real one untouched.
    settings = dataclasses.replace(
        get_settings(), database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    database.get_database(settings, refresh=True).create_all()

    payload = {
        "analysis_id": "AN_TEST_0001",
        "mode": "scenario",
        "valid_time_utc": "2026-01-16T18:30:16+00:00",
        "event_id": None,
        "model": {"model_version": "1.0.0", "config_sha256": "abc"},
        "hazard_score": 58.4,
        "confidence_score": 50.1,
        "duration_seconds": 1.0,
        "instability": {"score_withheld": False},
        "release_zones": {
            "features": [
                {
                    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
                    "properties": {"zone_id": "RZ001", "area_m2": 1000.0},
                }
            ]
        },
        "warnings": [],
    }

    first = repository.save_analysis(settings, payload, correlation_id="c1")
    second = repository.save_analysis(settings, payload, correlation_id="c2")

    assert first == second, "Re-saving one analysis id must not create a second row."

    listed = repository.list_analyses(settings, limit=10)
    ids = [item["analysis_id"] for item in listed]
    assert ids.count("AN_TEST_0001") == 1, "The analysis was duplicated in the database."
    assert listed[0]["release_zone_count"] == 1, "Release zones were duplicated on re-save."

    # Drop the throwaway engine so the next test does not inherit it.
    database.get_database(get_settings(), refresh=True)
