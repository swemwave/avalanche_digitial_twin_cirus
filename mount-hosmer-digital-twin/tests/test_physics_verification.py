"""Analytic software verification for the deterministic runout engines.

The terrain in this module is synthetic and every result is labelled
``scientific_use="software_verification"`` in pytest's per-test metadata.  These
checks verify equations, integration behaviour, conservation, and replay; they
are not field validation and say nothing about accuracy on a real avalanche.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Final

import numpy as np
import pytest

from avycore.hazard import runout
from avycore.hazard.zone import ReleaseZone


SCIENTIFIC_USE: Final = "software_verification"
SEED: Final = 9127


@dataclass(frozen=True)
class _Grid:
    shape: tuple[int, int]
    resolution_m: float


class _Config:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def require(self, dotted: str) -> Any:
        node: Any = self._values
        for part in dotted.split("."):
            node = node[part]
        return node


@dataclass(frozen=True)
class _Case:
    grid: _Grid
    elevation: np.ma.MaskedArray
    slope: np.ma.MaskedArray
    forest: np.ma.MaskedArray
    plan_curvature: np.ma.MaskedArray
    zone: ReleaseZone


def _record_evidence(
    request: pytest.FixtureRequest,
    *,
    component: str,
    engine_mode: runout.RunoutEngineMode,
) -> None:
    """Attach the claim boundary and component to machine-readable test output."""

    _record_property(request, "scientific_use", SCIENTIFIC_USE)
    _record_property(request, "component_under_test", component)
    _record_property(request, "engine_mode", engine_mode)


def _record_property(request: pytest.FixtureRequest, name: str, value: Any) -> None:
    """Append a JUnit-compatible property without pytest's xunit2 fixture warning."""

    request.node.user_properties.append((name, value))


def _config(
    *,
    mu: float = 0.30,
    xi: float = 1.0e30,
    particles: int = 1,
    max_steps: int = 20_000,
    dt_s: float = 0.02,
    jitter_rad: float = 0.0,
    stopping_velocity_ms: float = 0.01,
    minimum_flux: float = 0.001,
    spreading: float = 1.0,
) -> _Config:
    return _Config(
        {
            "runout": {
                "alpha_angle_deg": {
                    "small": 32.0,
                    "medium": 27.0,
                    "large": 23.0,
                    "very_large": 19.0,
                },
                "alpha_uncertainty_deg": 4.0,
                "friction": {
                    "open_snow": mu,
                    "forest": mu,
                    "gully": mu,
                    "xi_open": xi,
                    "xi_forest": xi,
                },
                "fast_mode": {
                    "spreading": spreading,
                    "max_path_length_m": 1_000_000.0,
                    "minimum_flux": minimum_flux,
                },
                "advanced_mode": {
                    "particles_per_zone": particles,
                    "max_steps": max_steps,
                    "time_step_s": dt_s,
                    "lateral_jitter": jitter_rad,
                    "stopping_velocity_ms": stopping_velocity_ms,
                    "random_seed": SEED,
                    "velocity_classes_ms": [5.0, 15.0, 25.0, 40.0],
                },
            }
        }
    )


def _ramp_to_flat(
    *,
    resolution_m: float,
    slope_deg: float,
    release_x_m: float,
    ramp_after_release_m: float,
    extent_m: float,
    columns: int = 3,
) -> _Case:
    """Return a planar incline followed by a flat, with one release cell."""

    rows = int(round(extent_m / resolution_m)) + 1
    x_m = np.arange(rows, dtype="float64") * resolution_m
    toe_x_m = release_x_m + ramp_after_release_m
    profile = 3000.0 - math.tan(math.radians(slope_deg)) * np.minimum(x_m, toe_x_m)
    elevation = np.broadcast_to(profile[:, None], (rows, columns)).copy()
    zeros = np.zeros(elevation.shape, dtype="float32")
    slope = np.where(x_m[:, None] < toe_x_m, slope_deg, 0.0)
    slope = np.broadcast_to(slope, elevation.shape).astype("float32", copy=True)

    release_row = int(round(release_x_m / resolution_m))
    release_col = columns // 2
    pixels = np.zeros(elevation.shape, dtype=bool)
    pixels[release_row, release_col] = True
    return _Case(
        grid=_Grid(elevation.shape, resolution_m),
        elevation=np.ma.array(elevation),
        slope=np.ma.array(slope),
        forest=np.ma.array(zeros),
        plan_curvature=np.ma.array(zeros.copy()),
        zone=ReleaseZone("verification-release", pixels, geometry=None),
    )


def _constant_slope(
    *,
    resolution_m: float,
    slope_deg: float,
    length_m: float,
    columns: int = 5,
) -> _Case:
    rows = int(round(length_m / resolution_m)) + 1
    x_m = np.arange(rows, dtype="float64") * resolution_m
    profile = 3000.0 - math.tan(math.radians(slope_deg)) * x_m
    elevation = np.broadcast_to(profile[:, None], (rows, columns)).copy()
    zeros = np.zeros(elevation.shape, dtype="float32")
    pixels = np.zeros(elevation.shape, dtype=bool)
    pixels[2, columns // 2] = True
    return _Case(
        grid=_Grid(elevation.shape, resolution_m),
        elevation=np.ma.array(elevation),
        slope=np.ma.array(np.full(elevation.shape, slope_deg, dtype="float32")),
        forest=np.ma.array(zeros),
        plan_curvature=np.ma.array(zeros.copy()),
        zone=ReleaseZone("verification-release", pixels, geometry=None),
    )


def _simulate_particle(
    case: _Case,
    config: _Config,
    *,
    engine_mode: str = runout.DYNAMICS_ONLY,
    seed: int = SEED,
) -> runout.RunoutResult:
    return runout.ParticleRunoutEngine(engine_mode).simulate(
        zone=case.zone,
        grid=case.grid,
        elevation=case.elevation,
        slope=case.slope,
        forest_mask=case.forest,
        plan_curvature=case.plan_curvature,
        config=config,
        release_size="medium",
        seed=seed,
    )


def test_coulomb_particle_stops_at_the_analytic_ramp_and_flat_distance(
    request: pytest.FixtureRequest,
) -> None:
    """Dynamics-only extent tests constant-mu Coulomb integration, not alpha."""

    _record_evidence(
        request,
        component="particle_dynamics_coulomb_stopping_distance",
        engine_mode=runout.DYNAMICS_ONLY,
    )
    slope_deg = 30.0
    mu = 0.30
    ramp_m = 50.0
    cell_m = 0.25
    case = _ramp_to_flat(
        resolution_m=cell_m,
        slope_deg=slope_deg,
        release_x_m=1.0,
        ramp_after_release_m=ramp_m,
        extent_m=200.0,
    )

    result = _simulate_particle(case, _config(mu=mu, xi=1.0e30, dt_s=0.02))

    # On the ramp, v_b**2 = 2*g*L*(tan(theta)-mu).  Coulomb friction then
    # dissipates that kinetic energy over v_b**2/(2*mu*g) metres of flat.
    expected_m = ramp_m * math.tan(math.radians(slope_deg)) / mu
    actual_m = float(result.metadata["runout_length_m"])
    # The DEM's sharp slope break occupies one centred-gradient cell and the
    # reported stopping location is rasterized to a cell.  Two 0.25 m cells is a
    # pre-declared discretization bound; the configured time step contributes
    # less than 0.01 m at the stopping threshold.
    tolerance_m = 2.0 * cell_m
    _record_property(request, "analytic_stop_m", expected_m)
    _record_property(request, "simulated_stop_m", actual_m)
    _record_property(request, "absolute_tolerance_m", tolerance_m)

    assert result.mode == runout.DYNAMICS_ONLY
    assert result.metadata["engine_mode"] == runout.DYNAMICS_ONLY
    assert result.metadata["particles_stopped_on_energy_line"] == 0
    assert result.metadata["particles_still_moving_at_cutoff"] == 0
    assert result.metadata["particles_left_the_aoi"] == 0
    assert actual_m == pytest.approx(expected_m, abs=tolerance_m)


def test_voellmy_terminal_velocity_matches_the_closed_form(
    request: pytest.FixtureRequest,
) -> None:
    """Dynamics-only velocity tests Voellmy mu/xi, not alpha-controlled extent."""

    _record_evidence(
        request,
        component="particle_dynamics_voellmy_terminal_velocity",
        engine_mode=runout.DYNAMICS_ONLY,
    )
    slope_deg = 35.0
    mu = 0.20
    xi = 60.0
    case = _constant_slope(
        resolution_m=1.0,
        slope_deg=slope_deg,
        length_m=2100.0,
    )

    result = _simulate_particle(case, _config(mu=mu, xi=xi, dt_s=0.02))

    theta = math.radians(slope_deg)
    expected_ms = math.sqrt(xi * (math.sin(theta) - mu * math.cos(theta)))
    actual_ms = float(result.velocity.max())
    # The forward velocity update is first order in time.  At dt=0.02 s its
    # characterized terminal-speed error is below 0.1%; 0.5% leaves a stated
    # margin for float32 output storage without weakening the physical equation.
    relative_tolerance = 0.005
    _record_property(request, "closed_form_terminal_velocity_ms", expected_ms)
    _record_property(request, "simulated_terminal_velocity_ms", actual_ms)
    _record_property(request, "relative_tolerance", relative_tolerance)

    assert result.mode == runout.DYNAMICS_ONLY
    assert result.metadata["engine_mode"] == runout.DYNAMICS_ONLY
    assert result.metadata["particles_stopped_on_energy_line"] == 0
    assert actual_ms == pytest.approx(expected_ms, rel=relative_tolerance)


def test_specific_energy_never_increases_and_flat_ground_adds_no_energy(
    request: pytest.FixtureRequest,
) -> None:
    """Dynamics-only path energy tests dissipation and flat-ground momentum."""

    _record_evidence(
        request,
        component="particle_dynamics_specific_energy_dissipation",
        engine_mode=runout.DYNAMICS_ONLY,
    )
    cell_m = 0.25
    toe_x_m = 51.0
    case = _ramp_to_flat(
        resolution_m=cell_m,
        slope_deg=30.0,
        release_x_m=1.0,
        ramp_after_release_m=50.0,
        extent_m=200.0,
    )
    result = _simulate_particle(case, _config(mu=0.30, xi=1.0e30, dt_s=0.02))

    centre = case.grid.shape[1] // 2
    velocity = result.velocity[:, centre].astype("float64")
    visited_rows = np.flatnonzero(velocity > 0.0)
    assert visited_rows.size > 2
    specific_energy = (
        0.5 * velocity[visited_rows] ** 2
        + runout.GRAVITY * np.asarray(case.elevation)[visited_rows, centre]
    )
    energy_changes = np.diff(specific_energy)

    flat_rows = visited_rows[visited_rows * cell_m >= toe_x_m]
    flat_velocity = velocity[flat_rows]
    flat_kinetic_changes = np.diff(0.5 * flat_velocity**2)
    _record_property(
        request, "maximum_specific_energy_change_j_per_kg", float(energy_changes.max())
    )
    _record_property(
        request,
        "maximum_flat_kinetic_energy_change_j_per_kg",
        float(flat_kinetic_changes.max()),
    )

    # One particle follows one monotone centreline, so the per-cell maximum
    # velocity samples are ordered along its path.  Total specific mechanical
    # energy must fall under Coulomb friction, and on the flat the kinetic term
    # alone must never rise.
    assert np.all(energy_changes <= 0.0)
    assert flat_velocity.size > 2
    assert np.all(flat_kinetic_changes <= 0.0)


def test_surface_velocity_uses_the_grade_along_its_actual_travel_direction(
    request: pytest.FixtureRequest,
) -> None:
    """Oblique projection tests map displacement after lateral path rotation."""

    _record_evidence(
        request,
        component="particle_directional_horizontal_coordinate_projection",
        engine_mode=runout.DYNAMICS_ONLY,
    )
    slope_deg = 30.0
    grade = math.tan(math.radians(slope_deg))
    inverse_sqrt_two = 1.0 / math.sqrt(2.0)
    # The plane rises only in the column direction. Travel directions are,
    # respectively, fall-line, 45 degrees oblique, and exactly along contour.
    dz_drow = np.array([0.0, 0.0, 0.0])
    dz_dcol = np.array([grade, grade, grade])
    row_velocity = np.array([0.0, inverse_sqrt_two, 1.0]) * 10.0
    col_velocity = np.array([1.0, inverse_sqrt_two, 0.0]) * 10.0

    observed = runout._directional_horizontal_projection_scale(
        dz_drow,
        dz_dcol,
        row_velocity,
        col_velocity,
    )
    directional_grades = np.array(
        [grade, grade * inverse_sqrt_two, 0.0], dtype="float64"
    )
    expected = 1.0 / np.sqrt(1.0 + directional_grades**2)
    _record_property(request, "fall_line_horizontal_scale", float(observed[0]))
    _record_property(request, "oblique_horizontal_scale", float(observed[1]))
    _record_property(request, "contour_horizontal_scale", float(observed[2]))

    assert observed == pytest.approx(expected, rel=0.0, abs=1.0e-12)
    assert observed[0] == pytest.approx(math.cos(math.radians(slope_deg)))
    assert observed[0] < observed[1] < observed[2]
    assert observed[2] == pytest.approx(1.0)


def test_runout_endpoint_has_first_order_grid_convergence(
    request: pytest.FixtureRequest,
) -> None:
    """Dynamics-only 20/10/5 m endpoints test spatial discretization, not alpha."""

    _record_evidence(
        request,
        component="particle_dynamics_grid_resolution_convergence",
        engine_mode=runout.DYNAMICS_ONLY,
    )
    slope_deg = 30.0
    mu = 0.30
    ramp_m = 400.0
    exact_m = ramp_m * math.tan(math.radians(slope_deg)) / mu
    endpoints: dict[float, float] = {}

    for resolution_m in (20.0, 10.0, 5.0):
        case = _ramp_to_flat(
            resolution_m=resolution_m,
            slope_deg=slope_deg,
            release_x_m=100.0,
            ramp_after_release_m=ramp_m,
            extent_m=1600.0,
        )
        result = _simulate_particle(case, _config(mu=mu, xi=1.0e30, dt_s=0.01))
        assert result.metadata["engine_mode"] == runout.DYNAMICS_ONLY
        assert result.metadata["particles_left_the_aoi"] == 0
        assert result.metadata["particles_still_moving_at_cutoff"] == 0
        endpoints[resolution_m] = float(result.metadata["runout_length_m"])
        _record_property(
            request,
            f"runout_endpoint_{resolution_m:g}m_grid_m",
            endpoints[resolution_m],
        )

    errors = {resolution: abs(value - exact_m) for resolution, value in endpoints.items()}
    coarse_rate = math.log2(errors[20.0] / errors[10.0])
    fine_rate = math.log2(errors[10.0] / errors[5.0])
    _record_property(request, "analytic_endpoint_m", exact_m)
    _record_property(request, "observed_order_20m_to_10m", coarse_rate)
    _record_property(request, "observed_order_10m_to_5m", fine_rate)

    # Nearest-cell terrain sampling and the centred gradient at the sharp ramp toe
    # are first-order spatial operations.  The pre-declared 0.8--1.2 order band
    # checks that halving h approximately halves error; it is not a fitted answer.
    assert errors[20.0] > errors[10.0] > errors[5.0]
    assert errors[5.0] <= 5.0
    assert coarse_rate == pytest.approx(1.0, abs=0.2)
    assert fine_rate == pytest.approx(1.0, abs=0.2)


def test_particle_engine_is_bit_identical_for_the_same_seed_and_terrain(
    request: pytest.FixtureRequest,
) -> None:
    """Hybrid replay tests seeded ensemble determinism, including the alpha line."""

    _record_evidence(
        request,
        component="particle_hybrid_seeded_replay",
        engine_mode=runout.HYBRID,
    )
    case = _ramp_to_flat(
        resolution_m=5.0,
        slope_deg=35.0,
        release_x_m=20.0,
        ramp_after_release_m=300.0,
        extent_m=800.0,
        columns=61,
    )
    config = _config(
        mu=0.20,
        xi=1200.0,
        particles=48,
        max_steps=1200,
        dt_s=0.05,
        jitter_rad=0.18,
    )

    first = _simulate_particle(case, config, engine_mode=runout.HYBRID, seed=424242)
    replay = _simulate_particle(case, config, engine_mode=runout.HYBRID, seed=424242)

    assert first.mode == runout.HYBRID
    assert first.metadata["engine_mode"] == runout.HYBRID
    for name in ("reached", "intensity", "velocity", "uncertainty"):
        first_array = getattr(first, name)
        replay_array = getattr(replay, name)
        assert first_array.dtype == replay_array.dtype
        assert first_array.shape == replay_array.shape
        assert first_array.tobytes(order="C") == replay_array.tobytes(order="C")
    assert first.stopping_points == replay.stopping_points
    assert first.metadata == replay.metadata
    assert first.warnings == replay.warnings


def test_fast_routing_conserves_flux_with_only_the_documented_cutoff_loss(
    request: pytest.FixtureRequest,
) -> None:
    """Alpha-only branching routing tests flux conservation, not runout accuracy."""

    _record_evidence(
        request,
        component="fast_alpha_routing_flux_conservation",
        engine_mode=runout.ALPHA_ONLY,
    )
    shape = (9, 9)
    row, col = np.indices(shape)
    # Every row descends and the cross-slope parabola has one terminal cell.  Flow
    # splits across a 61-cell fan, reconverges, and finally reaches (8, 4).
    elevation = (1000.0 - 10.0 * row + (col - 4) ** 2).astype("float64")
    zeros = np.zeros(shape, dtype="float32")
    pixels = np.zeros(shape, dtype=bool)
    source = (0, 4)
    sink = (8, 4)
    pixels[source] = True
    cutoff = 0.001
    config = _config(minimum_flux=cutoff, spreading=1.0)

    result = runout.FastRunoutEngine().simulate(
        zone=ReleaseZone("verification-release", pixels, geometry=None),
        grid=_Grid(shape, 5.0),
        elevation=np.ma.array(elevation),
        slope=np.ma.array(zeros),
        forest_mask=np.ma.array(zeros),
        plan_curvature=np.ma.array(zeros),
        config=config,
        release_size="medium",
        seed=None,
    )

    # Fast mode publishes log-scaled intensity.  The source has exactly one raw
    # flux unit and no upstream contributor, so its published value identifies the
    # scale denominator and permits an exact inverse for every retained cell.
    log_denominator = math.log(2.0) / float(result.intensity[source])
    raw_flux = np.expm1(result.intensity.astype("float64") * log_denominator)
    terminal_flux = float(raw_flux[sink])
    depleted_cells = int(result.metadata["cells_below_minimum_flux"])
    deficit = 1.0 - terminal_flux
    maximum_cutoff_loss = depleted_cells * cutoff
    _record_property(request, "initial_flux_units", 1.0)
    _record_property(request, "terminal_flux_units", terminal_flux)
    _record_property(request, "observed_flux_deficit_units", deficit)
    _record_property(request, "cutoff_loss_upper_bound_units", maximum_cutoff_loss)

    assert result.mode == runout.ALPHA_ONLY
    assert result.metadata["engine_mode"] == runout.ALPHA_ONLY
    assert int(result.reached.sum()) > 50  # exercise splitting and reconvergence
    assert depleted_cells > 0  # exercise, rather than merely configure, the cutoff
    assert result.stopping_points[0]["row"] == sink[0]
    assert result.stopping_points[0]["col"] == sink[1]
    # Each depleted cell holds less than minimum_flux and is never propagated, so
    # their count times the cutoff is a conservative, topology-independent loss
    # bound. Float32 routing/log publication contributes only the 1e-6 allowance.
    assert deficit >= -1.0e-6
    assert deficit <= maximum_cutoff_loss + 1.0e-6
