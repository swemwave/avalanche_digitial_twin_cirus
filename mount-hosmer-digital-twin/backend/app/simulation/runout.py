"""Runout simulation: fast routing, and an advanced particle ensemble.

**Fast mode** propagates flow downslope from the release zone with configurable
lateral spreading, and stops it at the *angle of reach* (alpha): the angle from
the top of the start zone to the toe of the deposit. Alpha is the oldest and most
robust empirical result in avalanche runout -- it is the model a practitioner
sketches on a map -- and it needs nothing but a DEM.

**Advanced mode** runs an ensemble of particles under Voellmy friction (a Coulomb
term plus a velocity-squared turbulent term), with terrain-dependent friction,
directional spreading, and a seeded random generator so a run is reproducible
exactly. It produces velocities, an intensity field, and an uncertainty envelope
from the spread of the ensemble.

Neither is calibrated. The alpha values and friction coefficients are published
regional defaults. The uncertainty envelope is wide on purpose.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from scipy import ndimage

from app.core.model_config import DISCLAIMER, ModelConfig
from app.simulation.zone import ReleaseZone
from app.processing.harmonization.grids import AnalysisGrid

try:  # pragma: no cover
    import rasterio
    from rasterio import features
    from rasterio.warp import transform_geom
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]

GRAVITY = 9.81

NEIGHBOURS = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]
#: Unit (cellless) distance to each neighbour -- constant, so precomputed once
#: rather than re-evaluating ``np.hypot`` per cell in the steepest-descent trace.
NEIGHBOUR_UNIT_DIST = tuple(float(np.hypot(dr, dc)) for dr, dc in NEIGHBOURS)

RELEASE_SIZES = ("small", "medium", "large", "very_large")


@dataclass
class RunoutResult:
    """Everything one simulated avalanche produced."""

    zone_id: str
    mode: str
    reached: np.ndarray               # boolean, cells the flow reached
    intensity: np.ndarray             # 0-1 relative flow intensity
    velocity: np.ndarray              # m/s, advanced mode only; zeros in fast mode
    uncertainty: np.ndarray           # boolean, the "could run further" envelope
    runout_polygon: dict[str, Any] | None = None
    uncertainty_polygon: dict[str, Any] | None = None
    main_path: dict[str, Any] | None = None
    alternate_paths: list[dict[str, Any]] = field(default_factory=list)
    stopping_points: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def runout_area_m2(self) -> float:
        return float(self.metadata.get("runout_area_m2", 0.0))


class RunoutEngine(Protocol):
    """The seam where a validated external engine can replace this one."""

    name: str

    def simulate(
        self,
        *,
        zone: ReleaseZone,
        grid: AnalysisGrid,
        elevation: np.ma.MaskedArray,
        slope: np.ma.MaskedArray,
        forest_mask: np.ma.MaskedArray,
        plan_curvature: np.ma.MaskedArray,
        config: ModelConfig,
        release_size: str,
        seed: int | None,
    ) -> RunoutResult: ...


# --- Shared helpers -----------------------------------------------------------


def _alpha_for(config: ModelConfig, release_size: str) -> float:
    angles = config.require("runout.alpha_angle_deg")
    if release_size not in angles:
        raise KeyError(
            f"Unknown release size {release_size!r}. Configured sizes: {sorted(angles)}"
        )
    return float(angles[release_size])


def _start_point(zone: ReleaseZone, elevation: np.ma.MaskedArray) -> tuple[int, int, float]:
    """The highest point in the release zone. Alpha is measured from here."""
    values = np.where(zone.pixels, np.asarray(elevation.filled(-np.inf)), -np.inf)
    index = int(np.argmax(values))
    row, col = divmod(index, elevation.shape[1])
    return row, col, float(values[row, col])


def _friction_field(
    config: ModelConfig,
    forest_mask: np.ma.MaskedArray,
    plan_curvature: np.ma.MaskedArray,
) -> tuple[np.ndarray, np.ndarray]:
    """Coulomb mu and turbulent xi, varying with vegetation and channelling.

    Standing timber costs a moving avalanche a great deal of energy; a confined
    gully costs it very little. Both effects are large enough that ignoring them
    would misplace the toe of the deposit by hundreds of metres.
    """
    forest = np.asarray(forest_mask.filled(0.0)) > 0.5
    plan = np.asarray(plan_curvature.filled(0.0))
    channelled = plan < -0.3

    mu = np.full(forest.shape, float(config.require("runout.friction.open_snow")), dtype="float32")
    mu[channelled] = float(config.require("runout.friction.gully"))
    mu[forest] = float(config.require("runout.friction.forest"))

    xi = np.full(forest.shape, float(config.require("runout.friction.xi_open")), dtype="float32")
    xi[forest] = float(config.require("runout.friction.xi_forest"))
    return mu, xi


def _polygonize(mask: np.ndarray, grid: AnalysisGrid) -> dict[str, Any] | None:
    # Skip when rasterio is unavailable, when the mask is empty, or when the grid
    # cannot supply a rasterio transform (the Stage 3 baked grid deliberately has
    # none -- geometry there is built by app.geo from the returned masks instead,
    # with no rasterio at all). A real AnalysisGrid has .transform, so the legacy
    # path is unchanged.
    if rasterio is None or getattr(grid, "transform", None) is None or not mask.any():
        return None
    # Close small holes so the runout reads as one body of snow, not lace.
    cleaned = ndimage.binary_closing(mask, structure=np.ones((3, 3), dtype=bool))
    shapes = [
        shape
        for shape, value in features.shapes(
            cleaned.astype("uint8"), mask=cleaned, transform=grid.transform, connectivity=8
        )
        if value == 1
    ]
    if not shapes:
        return None
    if len(shapes) == 1:
        geometry = shapes[0]
    else:
        geometry = {"type": "MultiPolygon", "coordinates": [shape["coordinates"] for shape in shapes]}
    return transform_geom(grid.crs, "EPSG:4326", geometry, precision=6)


def _line_to_geojson(path: list[tuple[int, int]], grid: AnalysisGrid) -> dict[str, Any] | None:
    if rasterio is None or getattr(grid, "transform", None) is None or len(path) < 2:
        return None
    transform = grid.transform
    coordinates = []
    for row, col in path:
        x, y = transform * (col + 0.5, row + 0.5)
        coordinates.append([x, y])
    return transform_geom(
        grid.crs, "EPSG:4326", {"type": "LineString", "coordinates": coordinates}, precision=6
    )


def _steepest_path(
    start: tuple[int, int],
    elevation: np.ndarray,
    valid: np.ndarray,
    reached: np.ndarray,
    max_steps: int = 5000,
) -> list[tuple[int, int]]:
    """Trace the single steepest-descent line through the reached area."""
    rows, cols = elevation.shape
    path = [start]
    row, col = start
    for _ in range(max_steps):
        best = None
        best_drop = 0.0
        for i in range(8):
            drow, dcol = NEIGHBOURS[i]
            r, c = row + drow, col + dcol
            if not (0 <= r < rows and 0 <= c < cols) or not valid[r, c] or not reached[r, c]:
                continue
            drop = (elevation[row, col] - elevation[r, c]) / NEIGHBOUR_UNIT_DIST[i]
            if drop > best_drop:
                best_drop = drop
                best = (r, c)
        if best is None:
            break
        path.append(best)
        row, col = best
    return path


# --- Fast interactive mode ----------------------------------------------------


class FastRunoutEngine:
    """Downslope routing with lateral spreading, stopped by the alpha angle.

    Cells are processed strictly highest-first using a heap, so when a cell is
    popped every cell that could feed it has already been processed and its flux
    is final. That makes this a single-pass O(n log n) over the *reached* cells
    only -- not over the whole 5.76 M-cell grid -- which is what makes it fast
    enough to sit behind an interactive control.
    """

    name = "fast_routing_alpha"

    def simulate(
        self,
        *,
        zone: ReleaseZone,
        grid: AnalysisGrid,
        elevation: np.ma.MaskedArray,
        slope: np.ma.MaskedArray,
        forest_mask: np.ma.MaskedArray,
        plan_curvature: np.ma.MaskedArray,
        config: ModelConfig,
        release_size: str,
        seed: int | None = None,
    ) -> RunoutResult:
        rows, cols = grid.shape
        z = np.asarray(elevation.filled(np.nan), dtype="float64")
        valid = ~np.ma.getmaskarray(elevation) & np.isfinite(z)

        alpha = _alpha_for(config, release_size)
        uncertainty_deg = float(config.require("runout.alpha_uncertainty_deg"))
        spreading = float(config.require("runout.fast_mode.spreading"))
        max_length = float(config.require("runout.fast_mode.max_path_length_m"))
        # Flow that has thinned below this is no longer an avalanche. Without it,
        # multi-directional routing leaks a vanishing trickle into every downhill
        # cell in the catchment, and because a cell used to be counted as "reached"
        # on geometry alone, the runout flooded the entire alpha cone. Each release
        # cell starts with one unit of flux, so this is a fraction of the snow from
        # a single release cell.
        minimum_flux = float(config.require("runout.fast_mode.minimum_flux"))

        start_row, start_col, start_z = _start_point(zone, elevation)
        cell = grid.resolution_m
        # Distance to each of the 8 neighbours is fixed; evaluate it once instead of
        # calling np.hypot per neighbour for every one of the ~500k cells popped.
        neighbour_dist = [float(np.hypot(drow * cell, dcol * cell)) for drow, dcol in NEIGHBOURS]
        # Flux-spreading exponent depends only on `spreading`, so it is constant
        # across the whole routing pass -- lift it out of the per-cell loop.
        exponent = 1.0 + 8.0 * (1.0 - np.clip(spreading, 0.0, 1.0))

        # The envelope uses a *shallower* alpha, i.e. an avalanche that runs
        # further than the central estimate. That is the direction that matters
        # for safety, so it is the direction the uncertainty is reported in.
        alpha_far = alpha - uncertainty_deg

        flux = np.zeros((rows, cols), dtype="float32")
        reached = np.zeros((rows, cols), dtype=bool)
        envelope = np.zeros((rows, cols), dtype=bool)
        stopped: list[tuple[int, int, float]] = []
        depleted = 0  # cells the flow reached, but too thinly to count as runout

        release_cells = np.argwhere(zone.pixels)
        if release_cells.size == 0:
            return RunoutResult(
                zone_id=zone.zone_id,
                mode=self.name,
                reached=reached,
                intensity=flux,
                velocity=np.zeros((rows, cols), dtype="float32"),
                uncertainty=envelope,
                warnings=["The release zone contained no cells."],
            )

        heap: list[tuple[float, int, int]] = []
        for row, col in release_cells:
            flux[row, col] = 1.0
            reached[row, col] = True
            envelope[row, col] = True
            heapq.heappush(heap, (-z[row, col], int(row), int(col)))

        finalized = np.zeros((rows, cols), dtype=bool)

        while heap:
            _, row, col = heapq.heappop(heap)
            if finalized[row, col]:
                continue
            finalized[row, col] = True

            # Horizontal distance from the start point. Used both to cap the path
            # length and (with the drop) to measure the angle of reach, so it is
            # computed once with the scalar math module -- numpy scalar ufuncs cost
            # ~10x more, and this runs for every one of ~500k popped cells.
            horizontal = math.hypot((row - start_row) * cell, (col - start_col) * cell)
            if horizontal > max_length:
                continue

            # Cells are popped in descending elevation, so every upslope
            # contributor has already been processed and this cell's flux is final.
            current_flux = float(flux[row, col])
            if current_flux < minimum_flux:
                # The flow has thinned to nothing here. Being inside the alpha cone
                # is necessary for the avalanche to reach a cell, but it is not
                # sufficient -- snow has to actually get there.
                if current_flux > 0.0:
                    depleted += 1
                continue

            # The angle of reach from the top of the start zone to this cell.
            if horizontal < 1e-6:
                angle = 90.0
            else:
                angle = math.degrees(math.atan((start_z - z[row, col]) / horizontal))
            in_core = angle >= alpha
            in_envelope = angle >= alpha_far

            if not in_envelope:
                # Past even the pessimistic reach: the avalanche has stopped.
                if current_flux > 0.02:
                    stopped.append((row, col, current_flux))
                continue

            if in_core:
                reached[row, col] = True
            envelope[row, col] = True

            # Distribute this cell's flux to its lower neighbours, weighted by
            # slope. `spreading` controls how readily flow leaves the fall line:
            # 0 gives a single-thread path, 1 spreads it evenly across every
            # downhill neighbour.
            zrc = z[row, col]
            drops: list[tuple[int, int, float]] = []
            for i in range(8):
                drow, dcol = NEIGHBOURS[i]
                r, c = row + drow, col + dcol
                if not (0 <= r < rows and 0 <= c < cols) or not valid[r, c]:
                    continue
                drop = (zrc - z[r, c]) / neighbour_dist[i]
                if drop > 0:
                    drops.append((r, c, drop))

            if not drops:
                # A local pit: there is nowhere lower to go, so the snow stops here.
                if current_flux > 0.02:
                    stopped.append((row, col, current_flux))
                continue

            # Slope-weighted split of this cell's flux to its lower neighbours. Kept
            # in pure Python (the arrays are <=8 long, so numpy's per-call overhead
            # dominated); the arithmetic order matches the old vectorized form.
            weights = [drop**exponent for _, _, drop in drops]
            total_weight = 0.0
            for weight in weights:
                total_weight += weight
            for (r, c, _), weight in zip(drops, weights):
                flux[r, c] += current_flux * float(weight / total_weight)
                if not finalized[r, c]:
                    heapq.heappush(heap, (-z[r, c], r, c))

        intensity = flux.copy()
        if intensity.max() > 0:
            # Log scaling: flow concentrates enormously in the channel, and a
            # linear stretch would render everything but the thread as zero.
            intensity = np.log1p(intensity) / np.log1p(intensity.max())
        intensity[~envelope] = 0.0

        area = float(reached.sum()) * cell**2
        envelope_area = float(envelope.sum()) * cell**2

        main_path = _steepest_path((start_row, start_col), z, valid, reached)
        stopped.sort(key=lambda item: item[2], reverse=True)

        metadata = {
            "engine": self.name,
            "release_size": release_size,
            "alpha_angle_deg": alpha,
            "alpha_uncertainty_deg": uncertainty_deg,
            "alpha_envelope_deg": alpha_far,
            "spreading": spreading,
            "start_elevation_m": round(start_z, 1),
            "runout_area_m2": round(area, 1),
            "uncertainty_area_m2": round(envelope_area, 1),
            "reached_cells": int(reached.sum()),
            "minimum_flux": minimum_flux,
            "cells_below_minimum_flux": depleted,
            "stopping_criteria": (
                "A cell is part of the runout only if the flow both reaches it within the alpha "
                "angle AND still carries at least the minimum flux. The alpha angle bounds how far "
                "snow can run; it does not mean snow reaches everywhere inside that bound."
            ),
            "horizontal_reach_m": round(
                float(
                    np.max(
                        np.hypot(
                            (np.argwhere(reached)[:, 0] - start_row) * cell,
                            (np.argwhere(reached)[:, 1] - start_col) * cell,
                        )
                    )
                )
                if reached.any()
                else 0.0,
                1,
            ),
            "vertical_drop_m": round(
                start_z - float(np.nanmin(np.where(reached, z, np.nan))) if reached.any() else 0.0, 1
            ),
            "method": (
                "Downslope multiple-flow-direction routing from the release zone, terminated at the "
                "angle of reach (alpha) measured from the highest point of the start zone."
            ),
            "is_validated": False,
            "calibration": (
                "NOT CALIBRATED. Alpha angles are published Canadian Rockies ranges for dry-slab "
                "avalanches, not values back-analysed from observed Mount Hosmer runouts. No such "
                "observations exist."
            ),
            "disclaimer": DISCLAIMER,
        }

        return RunoutResult(
            zone_id=zone.zone_id,
            mode=self.name,
            reached=reached,
            intensity=intensity.astype("float32"),
            velocity=np.zeros((rows, cols), dtype="float32"),
            uncertainty=envelope,
            runout_polygon=_polygonize(reached, grid),
            uncertainty_polygon=_polygonize(envelope, grid),
            main_path=_line_to_geojson(main_path, grid),
            stopping_points=[
                {
                    "row": int(row),
                    "col": int(col),
                    "flux": round(value, 4),
                    "elevation_m": round(float(z[row, col]), 1),
                }
                for row, col, value in stopped[:20]
            ],
            metadata=metadata,
            warnings=[
                "Fast mode does not compute velocity. Use the advanced mode for velocity and "
                "intensity classes."
            ],
        )


# --- Advanced particle ensemble ----------------------------------------------


class ParticleRunoutEngine:
    """A Voellmy-friction particle ensemble.

    Each particle carries a velocity and is decelerated by a Coulomb term
    (``mu * g * cos(theta)``) and a turbulent term (``g * v**2 / xi``). Friction
    varies with the ground it is crossing -- forest costs a great deal of energy,
    a confined gully very little.

    Particles are launched from every part of the start zone and take a slightly
    randomized path, so the ensemble spreads the way real avalanche debris does.
    The random generator is seeded from the configuration, so an identical
    simulation replays bit-for-bit.
    """

    name = "particle_ensemble_voellmy"

    def simulate(
        self,
        *,
        zone: ReleaseZone,
        grid: AnalysisGrid,
        elevation: np.ma.MaskedArray,
        slope: np.ma.MaskedArray,
        forest_mask: np.ma.MaskedArray,
        plan_curvature: np.ma.MaskedArray,
        config: ModelConfig,
        release_size: str,
        seed: int | None = None,
    ) -> RunoutResult:
        rows, cols = grid.shape
        cell = grid.resolution_m
        z = np.asarray(elevation.filled(np.nan), dtype="float64")
        valid = ~np.ma.getmaskarray(elevation) & np.isfinite(z)

        count = int(config.require("runout.advanced_mode.particles_per_zone"))
        max_steps = int(config.require("runout.advanced_mode.max_steps"))
        dt = float(config.require("runout.advanced_mode.time_step_s"))
        jitter = float(config.require("runout.advanced_mode.lateral_jitter"))
        stop_velocity = float(config.require("runout.advanced_mode.stopping_velocity_ms"))
        velocity_classes = list(config.require("runout.advanced_mode.velocity_classes_ms"))
        configured_seed = int(config.require("runout.advanced_mode.random_seed"))

        rng = np.random.default_rng(seed if seed is not None else configured_seed)
        mu_field, xi_field = _friction_field(config, forest_mask, plan_curvature)

        # A bigger release entrains more snow and carries further. This is a
        # crude scaling of the friction, standing in for a mass-balance model we
        # do not have the data to build.
        size_factor = {"small": 1.20, "medium": 1.0, "large": 0.85, "very_large": 0.72}.get(
            release_size, 1.0
        )

        release_cells = np.argwhere(zone.pixels)
        if release_cells.size == 0:
            zeros = np.zeros((rows, cols), dtype="float32")
            return RunoutResult(
                zone_id=zone.zone_id,
                mode=self.name,
                reached=np.zeros((rows, cols), dtype=bool),
                intensity=zeros,
                velocity=zeros,
                uncertainty=np.zeros((rows, cols), dtype=bool),
                warnings=["The release zone contained no cells."],
            )

        picks = rng.integers(0, len(release_cells), size=count)
        positions = release_cells[picks].astype("float64")
        # Scatter within the source cell so particles do not all start on the
        # same lattice point and follow the same line.
        positions += rng.uniform(-0.5, 0.5, size=positions.shape)

        # --- The energy line ------------------------------------------------
        # Voellmy alone cannot stop these particles in the right place, and no
        # choice of mu fixes it. A particle keeps moving while tan(theta) > mu, so
        # mu sets the LOCAL slope at which it may finally rest: mu = 0.20 means it
        # coasts until the ground flattens below 11 degrees. Several drainages below
        # Mount Hosmer keep falling more steeply than that for kilometres, so a
        # particle that finds one rides it to the floor of the AOI. Sweeping mu from
        # 0.20 to 0.60 on the real terrain moved the median angle of reach from 26.6
        # to 31.7 degrees but left the worst zone pinned near 13 degrees: the tail is
        # not a friction problem.
        #
        # It is a missing-physics problem. A real avalanche also decelerates because
        # it spreads, thins and deposits mass along its track. A dimensionless
        # particle does none of that -- it simply conserves momentum. So the
        # dynamics understate dissipation, and the runout has no natural end.
        #
        # The classical remedy (Perla-Cheng-McClung, and standard practice) is to let
        # the dynamics supply the velocity field and let the empirical angle of reach
        # supply the stopping rule. A parcel of snow cannot travel below the energy
        # line drawn from where it released at the alpha angle; doing so would mean
        # dissipating more energy than it ever had.
        #
        # The line is anchored PER PARTICLE, at its own release point, not at the
        # crown of the zone. A zone can run a long way along the contour, so a
        # particle released 500 m from the crown at the same elevation has almost no
        # drop relative to the crown and a crown-anchored line would kill it the
        # instant it moved. Anchoring each parcel to its own origin is also the more
        # faithful statement: the snow high on the face carries more energy and runs
        # further, so the toe of the deposit is still set by the particles from the
        # top, which is exactly what the alpha angle describes.
        origins = positions.copy()
        origin_rows = np.clip(np.round(origins[:, 0]).astype(int), 0, rows - 1)
        origin_cols = np.clip(np.round(origins[:, 1]).astype(int), 0, cols - 1)
        origin_z = z[origin_rows, origin_cols]

        # Particles run to the pessimistic edge of the range (alpha minus the
        # uncertainty), so the ensemble spans the full "could run further" envelope
        # rather than being trimmed to the central estimate.
        alpha_stop = _alpha_for(config, release_size) - float(
            config.require("runout.alpha_uncertainty_deg")
        )
        # Do not test the line until a particle has actually gone somewhere, or the
        # ratio is 0/0 at launch.
        minimum_travel_m = 2.0 * cell

        # Velocity is a vector in (row, col), so a particle carries momentum and can
        # coast across flat ground and up a counter-slope.
        velocity = np.zeros((count, 2), dtype="float64")
        alive = np.ones(count, dtype=bool)

        visits = np.zeros((rows, cols), dtype="float32")
        max_velocity = np.zeros((rows, cols), dtype="float32")
        stopping: list[tuple[int, int, float]] = []
        tracks: list[list[tuple[int, int]]] = [[] for _ in range(min(count, 12))]
        spent_on_energy_line = 0
        left_the_aoi = 0
        # The angle of reach each particle finally achieved, measured from its own
        # release point. This -- not the angle from the crown of the zone -- is what
        # the energy line constrains, and measuring it from the crown gives a wrong
        # answer for any zone with real extent: a zone stretching a kilometre along
        # the face puts its own toe far from its crown at little vertical drop, so a
        # particle that has barely moved appears to have run out at a shallow angle.
        particle_reach = np.full(count, np.nan, dtype="float64")
        particle_travelled = np.full(count, 0.0, dtype="float64")

        # Gradients drive the particles. Computed once on the filled surface.
        filled = np.where(valid, z, np.nanmax(z[valid]) if valid.any() else 0.0)
        dz_dy, dz_dx = np.gradient(filled, cell, cell)

        for step in range(max_steps):
            if not alive.any():
                break

            rows_i = np.clip(np.round(positions[:, 0]).astype(int), 0, rows - 1)
            cols_i = np.clip(np.round(positions[:, 1]).astype(int), 0, cols - 1)

            off_grid = ~valid[rows_i, cols_i]
            alive &= ~off_grid

            active = np.flatnonzero(alive)
            if active.size == 0:
                break

            r = rows_i[active]
            c = cols_i[active]

            visits[r, c] += 1.0
            current = np.hypot(velocity[active, 0], velocity[active, 1])
            np.maximum.at(max_velocity, (r, c), current.astype("float32"))

            for index, track_index in enumerate(active[: len(tracks)]):
                if index < len(tracks):
                    tracks[index].append((int(rows_i[track_index]), int(cols_i[track_index])))

            gx = dz_dx[r, c]
            gy = dz_dy[r, c]
            gradient = np.hypot(gx, gy)
            theta = np.arctan(gradient)

            # Downslope unit vector, in (row, col). Row increases southward, and
            # the surface falls in the direction opposite the gradient.
            norm = np.where(gradient > 1e-9, gradient, 1.0)
            dir_row = -gy / norm
            dir_col = -gx / norm

            if jitter > 0:
                angle = rng.normal(0.0, jitter, size=active.size)
                cos_a, sin_a = np.cos(angle), np.sin(angle)
                rotated_row = dir_row * cos_a - dir_col * sin_a
                rotated_col = dir_row * sin_a + dir_col * cos_a
                dir_row, dir_col = rotated_row, rotated_col

            mu = mu_field[r, c] * size_factor
            xi = xi_field[r, c]

            # Voellmy, integrated as a VECTOR. Gravity pulls the particle down the
            # local fall line; friction opposes the direction it is actually moving.
            #
            # Those are not the same direction, and treating them as one is what
            # broke this. A particle used to be advanced along the local downslope
            # unit vector, which on flat ground is (0, 0) -- so the instant an
            # avalanche reached the valley floor it froze on the spot, carrying 25 m/s
            # of velocity that had nowhere to go. Runout onto the flat is the whole
            # point of a runout model, and there wasn't any: the deposit landed
            # exactly at the slope break, at every release size. Real terrain is never
            # perfectly flat, so it never crashed -- the flow just quietly died
            # wherever the ground eased off.
            #
            # With momentum, a particle carries on across the flat and decelerates,
            # and it can climb a counter-slope, which is what lets debris run up the
            # far side of a gully instead of stopping dead in the bottom of it.
            speed = np.hypot(velocity[active, 0], velocity[active, 1])
            in_motion = speed > 1e-6
            # A particle at rest has no direction of travel yet, so it starts down
            # the fall line.
            motion_row = np.where(in_motion, velocity[active, 0] / np.maximum(speed, 1e-9), dir_row)
            motion_col = np.where(in_motion, velocity[active, 1] / np.maximum(speed, 1e-9), dir_col)

            driving = GRAVITY * np.sin(theta)
            coulomb = mu * GRAVITY * np.cos(theta)
            turbulent = GRAVITY * speed**2 / np.maximum(xi, 1e-6)
            resistance = coulomb + turbulent

            accel_row = driving * dir_row - resistance * motion_row
            accel_col = driving * dir_col - resistance * motion_col

            new_row_v = velocity[active, 0] + accel_row * dt
            new_col_v = velocity[active, 1] + accel_col * dt

            # Friction removes momentum; it never reverses it. If a step would push
            # the particle backwards along its own track, it has come to rest instead.
            reversed_ = (new_row_v * motion_row + new_col_v * motion_col) < 0.0
            new_row_v = np.where(reversed_, 0.0, new_row_v)
            new_col_v = np.where(reversed_, 0.0, new_col_v)
            new_velocity = np.hypot(new_row_v, new_col_v)

            # A particle that has come to rest on ground that is not steep enough
            # to restart it has stopped for good.
            resting = (new_velocity < stop_velocity) & (driving <= coulomb)

            # And a particle that has dropped below its own energy line has run as
            # far as its energy allows, whatever the local slope still says.
            travelled = np.hypot(
                (positions[active, 0] - origins[active, 0]) * cell,
                (positions[active, 1] - origins[active, 1]) * cell,
            )
            descended = origin_z[active] - z[r, c]
            reach_angle = np.degrees(np.arctan2(descended, np.maximum(travelled, 1e-6)))
            has_travelled = travelled > minimum_travel_m

            # The energy line only governs snow that has actually left the start
            # zone. Inside it, the slab has not begun its runout: a particle creeping
            # across a bench within the release area has descended almost nothing
            # over the little ground it has covered, so its angle of reach is
            # meaninglessly shallow, and testing it against the line would retire it
            # on the spot -- possibly just short of the steep step it was about to
            # drop off.
            has_left_zone = ~zone.pixels[r, c]
            beyond_energy_line = has_left_zone & has_travelled & (reach_angle < alpha_stop)

            # Record how far each particle has run, and at what angle of reach.
            moved = active[has_travelled]
            particle_reach[moved] = reach_angle[has_travelled]
            particle_travelled[moved] = travelled[has_travelled]

            spent = resting | beyond_energy_line
            spent_on_energy_line += int((beyond_energy_line & ~resting).sum())

            stopped_now = active[spent]
            for index in stopped_now:
                row_s = int(np.clip(round(positions[index, 0]), 0, rows - 1))
                col_s = int(np.clip(round(positions[index, 1]), 0, cols - 1))
                stopping.append(
                    (row_s, col_s, float(np.hypot(velocity[index, 0], velocity[index, 1])))
                )
            alive[stopped_now] = False

            # Move along the velocity vector, not the fall line.
            positions[active, 0] += new_row_v * dt / cell
            positions[active, 1] += new_col_v * dt / cell
            velocity[active, 0] = new_row_v
            velocity[active, 1] = new_col_v

            # A particle that runs off the edge has left the study area. Clipping it
            # back onto the boundary would pin it there, still moving, still marking
            # the same edge cell every step -- which is how a runout came to pile up
            # against the side of the AOI. It is retired instead, and counted, so the
            # simulation can say that the avalanche ran out of map rather than
            # pretending it stopped at the frame.
            escaped = active[
                (positions[active, 0] < 0)
                | (positions[active, 0] > rows - 1)
                | (positions[active, 1] < 0)
                | (positions[active, 1] > cols - 1)
            ]
            if escaped.size:
                left_the_aoi += int(escaped.size)
                alive[escaped] = False

            positions[:, 0] = np.clip(positions[:, 0], 0, rows - 1)
            positions[:, 1] = np.clip(positions[:, 1], 0, cols - 1)

        reached = visits > 0
        # The core is where a meaningful share of the ensemble went; the envelope
        # is anywhere any particle reached. The gap between them IS the
        # uncertainty, and it is reported rather than hidden.
        if visits.max() > 0:
            core_threshold = max(1.0, float(np.percentile(visits[reached], 40)))
            core = visits >= core_threshold
        else:
            core = reached

        intensity = visits.copy()
        if intensity.max() > 0:
            intensity = np.log1p(intensity) / np.log1p(intensity.max())

        stopping.sort(key=lambda item: item[2], reverse=True)
        start_row, start_col, start_z = _start_point(zone, elevation)

        # How far did the ensemble actually run, expressed as an angle of reach?
        #
        # This engine is pure Voellmy: a particle stops when friction has taken its
        # velocity, and nothing here imposes an alpha angle. That is deliberate --
        # Voellmy and the empirical alpha model are two independent approaches and
        # they are expected to disagree. But the disagreement must be VISIBLE. A
        # Coulomb mu of 0.20 permits coasting to arctan(0.20) ~= 11 degrees, well
        # past the alpha envelope, and a runout that quietly exceeds the documented
        # envelope while claiming the same release size would mislead.
        #
        # So the achieved alpha is measured and reported, and exceeding the
        # envelope raises a warning. The runout is NOT truncated to alpha: doing so
        # would shrink the hazard footprint, which is the unsafe direction -- it
        # could drop assets that a longer-running avalanche would in fact reach.
        alpha_expected = _alpha_for(config, release_size)
        alpha_envelope = alpha_stop
        engine_warnings: list[str] = []

        # The angle of reach of the DEPOSIT TIP: the particle that ran furthest,
        # measured from its own release point. This is the number the energy line
        # bounds, so it is the one that says whether the bound held.
        #
        # It is deliberately not the minimum over all particles. Most of the ensemble
        # never runs anywhere -- a particle that has crept ten metres across a bench
        # while dropping one has an angle of reach near zero, and taking the minimum
        # would report that stalled parcel as the runout of the avalanche.
        alpha_achieved: float | None = None
        if bool(np.isfinite(particle_reach).any()):
            tip = int(np.argmax(particle_travelled))
            if np.isfinite(particle_reach[tip]):
                alpha_achieved = float(particle_reach[tip])

        if alpha_achieved is not None and alpha_achieved < alpha_envelope - 1.0:
            engine_warnings.append(
                f"A particle ran to an angle of reach of {alpha_achieved:.1f} degrees, past the "
                f"{alpha_envelope:.0f} degree energy line for a '{release_size}' release. The "
                f"energy-line bound is not holding; the runout is longer than the model's own "
                f"stated envelope and should not be trusted."
            )

        classes = []
        for index, bound in enumerate(velocity_classes):
            lower = velocity_classes[index - 1] if index else 0
            classes.append(
                {
                    "class": index + 1,
                    "min_ms": lower,
                    "max_ms": bound,
                    "cells": int(((max_velocity > lower) & (max_velocity <= bound)).sum()),
                }
            )
        classes.append(
            {
                "class": len(velocity_classes) + 1,
                "min_ms": velocity_classes[-1],
                "max_ms": None,
                "cells": int((max_velocity > velocity_classes[-1]).sum()),
            }
        )

        metadata = {
            "engine": self.name,
            "release_size": release_size,
            "particles": count,
            "random_seed": int(seed if seed is not None else configured_seed),
            "time_step_s": dt,
            "max_steps": max_steps,
            "lateral_jitter": jitter,
            "stopping_velocity_ms": stop_velocity,
            "size_friction_factor": size_factor,
            "friction_model": (
                "Voellmy (Coulomb mu + turbulent xi), terrain dependent, bounded by a per-particle "
                "energy line at the alpha envelope"
            ),
            "alpha_angle_achieved_deg": round(alpha_achieved, 1) if alpha_achieved is not None else None,
            "alpha_angle_expected_deg": alpha_expected,
            "alpha_envelope_deg": alpha_envelope,
            "alpha_measured_from": (
                "each particle's own release point, not the crown of the zone -- a zone with real "
                "extent puts its own toe far from its crown, and measuring from the crown would "
                "report a particle that had barely moved as a shallow-angle runout"
            ),
            "alpha_envelope_exceeded": bool(
                alpha_achieved is not None and alpha_achieved < alpha_envelope - 1.0
            ),
            "particles_stopped_on_energy_line": spent_on_energy_line,
            "particles_left_the_aoi": left_the_aoi,
            "runout_length_m": round(float(particle_travelled.max()), 1),
            "alpha_note": (
                "The Voellmy dynamics supply the velocity field; the empirical angle of reach supplies "
                "the stopping rule. A particle may not travel below the energy line drawn from its own "
                "release point at the alpha envelope, because a point particle carries no mass and so "
                "cannot lose energy by spreading, thinning and depositing the way a real avalanche "
                "does. Without that bound the runout has no natural end and a particle that finds a "
                "steep drainage rides it to the floor of the AOI."
            ),
            "start_elevation_m": round(start_z, 1),
            "runout_area_m2": round(float(core.sum()) * cell**2, 1),
            "uncertainty_area_m2": round(float(reached.sum()) * cell**2, 1),
            "max_velocity_ms": round(float(max_velocity.max()), 2),
            "mean_velocity_ms": round(
                float(max_velocity[reached].mean()) if reached.any() else 0.0, 2
            ),
            "velocity_classes": classes,
            "particles_stopped": len(stopping),
            "particles_still_moving_at_cutoff": int(alive.sum()),
            "vertical_drop_m": round(
                start_z - float(np.nanmin(np.where(reached, z, np.nan))) if reached.any() else 0.0, 1
            ),
            "is_validated": False,
            "calibration": (
                "NOT CALIBRATED. Voellmy mu and xi are literature values for dry-flowing avalanches, "
                "not values fitted to observed Mount Hosmer events. The ensemble spread is a measure "
                "of the model's own sensitivity, NOT a confidence interval on reality."
            ),
            "reproducible": (
                "Re-running with the same seed, model version and terrain reproduces this result exactly."
            ),
            "disclaimer": DISCLAIMER,
        }

        warnings: list[str] = list(engine_warnings)
        if int(alive.sum()) > count * 0.05:
            warnings.append(
                f"{int(alive.sum())} of {count} particles were still moving when the step limit was "
                f"reached. The runout may be truncated; increase runout.advanced_mode.max_steps."
            )
        if left_the_aoi > count * 0.02:
            warnings.append(
                f"{left_the_aoi} of {count} particles ran off the edge of the study area. The "
                f"simulated avalanche leaves the 12 x 12 km AOI, so its full runout and everything it "
                f"might reach beyond the boundary are NOT modelled. The exposed-asset count for this "
                f"zone is a lower bound."
            )

        alternates = [
            path
            for path in (_line_to_geojson(track, grid) for track in tracks[1:6] if len(track) > 5)
            if path is not None
        ]

        return RunoutResult(
            zone_id=zone.zone_id,
            mode=self.name,
            reached=core,
            intensity=intensity.astype("float32"),
            velocity=max_velocity,
            uncertainty=reached,
            runout_polygon=_polygonize(core, grid),
            uncertainty_polygon=_polygonize(reached, grid),
            main_path=_line_to_geojson(tracks[0], grid) if tracks and tracks[0] else None,
            alternate_paths=alternates,
            stopping_points=[
                {
                    "row": int(row),
                    "col": int(col),
                    "impact_velocity_ms": round(value, 2),
                    "elevation_m": round(float(z[row, col]), 1),
                }
                for row, col, value in stopping[:20]
            ],
            metadata=metadata,
            warnings=warnings,
        )


ENGINES: dict[str, RunoutEngine] = {
    "fast": FastRunoutEngine(),
    "advanced": ParticleRunoutEngine(),
}


def get_engine(mode: str) -> RunoutEngine:
    if mode not in ENGINES:
        raise KeyError(f"Unknown simulation mode {mode!r}. Available: {', '.join(sorted(ENGINES))}")
    return ENGINES[mode]
