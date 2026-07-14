"""Spatial wind-loading model.

Wind is what turns a uniform snowfall into a slab in one place and bare ground
100 m away. This model answers: given a wind of this speed from this direction,
where does the snow end up?

Two rules govern everything here.

**Direction is a vector, never a string.** Nowhere in this module is a cardinal
name compared to another cardinal name. "Is a northeast slope in the lee of a
southwest wind?" is answered by taking the angle between two unit vectors, which
is correct at every bearing and has no special cases at 0/360.

**Wind cannot load a slope that has no snow.** A 90 km/h gale over bare August
rock loads nothing. Every score here is multiplied by snow availability, so the
model cannot report a wind slab where there is no snow to make one from.

The terrain exposure term is a Winstral-style Sx: for each cell, look upwind and
find the steepest angle to the horizon within a search distance. A cell in the
shelter of a big upwind ridge is scoured less and receives more deposited snow; a
cell standing proud of its surroundings gets stripped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage

from app.core.model_config import ModelConfig
from app.processing.harmonization.grids import AnalysisGrid


@dataclass
class WindFields:
    """Every spatial wind product, plus how it was made."""

    terrain_exposure: np.ma.MaskedArray      # Winstral Sx, degrees. + sheltered, - exposed
    windward_erosion: np.ma.MaskedArray      # 0-100, snow removed
    lee_deposition: np.ma.MaskedArray        # 0-100, snow added
    cross_loading: np.ma.MaskedArray         # 0-100, snow moved across the slope
    ridge_exposure: np.ma.MaskedArray        # 0-100, exposure at ridge crests
    cornice_potential: np.ma.MaskedArray     # 0-100
    wind_loading: np.ma.MaskedArray          # 0-100, the combined answer
    available: bool
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_consistency: float | None
    warnings: list[str] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)

    def layers(self) -> dict[str, np.ma.MaskedArray]:
        return {
            "wind_terrain_exposure": self.terrain_exposure,
            "wind_windward_erosion": self.windward_erosion,
            "wind_lee_deposition": self.lee_deposition,
            "wind_cross_loading": self.cross_loading,
            "wind_ridge_exposure": self.ridge_exposure,
            "wind_cornice_potential": self.cornice_potential,
            "wind_loading": self.wind_loading,
        }


def angular_difference(a: np.ndarray, b: float) -> np.ndarray:
    """Smallest angle between bearings, 0-180 degrees. Correct across the 0/360 seam.

    ``np.angle(np.exp(1j * theta))`` maps any angle into (-pi, pi], which is what
    makes 350 deg and 10 deg come out 20 deg apart rather than 340.
    """
    delta = np.deg2rad(a) - np.deg2rad(b)
    return np.abs(np.rad2deg(np.angle(np.exp(1j * delta))))


def terrain_exposure_sx(
    elevation: np.ma.MaskedArray,
    grid: AnalysisGrid,
    wind_from_deg: float,
    search_distance_m: float,
    sector_width_deg: float,
    sector_samples: int = 5,
) -> np.ma.MaskedArray:
    """Winstral Sx: the maximum upwind horizon angle, averaged over a wind sector.

    Positive = the cell is sheltered by higher terrain upwind (a deposition
    zone). Negative = the cell stands above its upwind surroundings and is
    exposed to scouring.
    """
    z = np.asarray(elevation.filled(np.nan), dtype="float32")
    mask = np.ma.getmaskarray(elevation)
    # Fill holes so the horizon search does not trip over NoData.
    if mask.any() and not mask.all():
        indices = ndimage.distance_transform_edt(mask, return_distances=False, return_indices=True)
        z = z[tuple(indices)]
    z = np.nan_to_num(z, nan=float(np.nanmin(z)) if np.isfinite(z).any() else 0.0)

    cell = grid.resolution_m
    steps = max(2, int(round(search_distance_m / cell)))
    sector = np.linspace(
        wind_from_deg - sector_width_deg / 2.0,
        wind_from_deg + sector_width_deg / 2.0,
        sector_samples,
    )

    sector_max = np.full((sector_samples, *z.shape), -90.0, dtype="float32")

    for index, bearing in enumerate(sector):
        # Bearing is where the wind comes FROM, so the upwind direction is the
        # bearing itself. Convert to grid offsets: north is -row, east is +col.
        radians = np.deg2rad(bearing)
        drow_unit = -np.cos(radians)
        dcol_unit = np.sin(radians)

        best = np.full(z.shape, -90.0, dtype="float32")
        for step in range(1, steps + 1):
            drow = int(round(drow_unit * step))
            dcol = int(round(dcol_unit * step))
            if drow == 0 and dcol == 0:
                continue
            distance = np.hypot(drow * cell, dcol * cell)
            upwind = _shift(z, drow, dcol)
            angle = np.rad2deg(np.arctan((upwind - z) / distance))
            best = np.maximum(best, angle.astype("float32"))
        sector_max[index] = best

    sx = sector_max.mean(axis=0)
    return np.ma.array(sx.astype("float32"), mask=mask)


def _shift(array: np.ndarray, drow: int, dcol: int) -> np.ndarray:
    rows, cols = array.shape
    row_index = np.clip(np.arange(rows) + drow, 0, rows - 1)
    col_index = np.clip(np.arange(cols) + dcol, 0, cols - 1)
    return array[np.ix_(row_index, col_index)]


def compute(
    *,
    config: ModelConfig,
    grid: AnalysisGrid,
    elevation: np.ma.MaskedArray,
    slope: np.ma.MaskedArray,
    aspect: np.ma.MaskedArray,
    plan_curvature: np.ma.MaskedArray,
    distance_to_ridge: np.ma.MaskedArray,
    forest_mask: np.ma.MaskedArray,
    snow_availability: np.ma.MaskedArray | None,
    wind_speed_kmh: float | None,
    wind_direction_deg: float | None,
    wind_consistency: float | None,
) -> WindFields:
    """Compute the wind-loading fields for one wind state."""
    mask = np.ma.getmaskarray(elevation)
    empty = np.ma.array(np.zeros(grid.shape, dtype="float32"), mask=mask)
    warnings: list[str] = []

    if wind_speed_kmh is None or wind_direction_deg is None:
        warnings.append(
            "Wind speed or direction was unavailable, so no wind-loading field was computed. "
            "The wind component is EXCLUDED from the instability score rather than scored as zero."
        )
        return WindFields(
            terrain_exposure=empty,
            windward_erosion=empty,
            lee_deposition=empty,
            cross_loading=empty,
            ridge_exposure=empty,
            cornice_potential=empty,
            wind_loading=empty,
            available=False,
            wind_speed_kmh=wind_speed_kmh,
            wind_direction_deg=wind_direction_deg,
            wind_consistency=wind_consistency,
            warnings=warnings,
            explanation={"available": False, "reason": warnings[-1]},
        )

    transport = float(config.require("wind.transport_threshold_kmh"))
    strong = float(config.require("wind.strong_transport_kmh"))
    lee_tolerance = float(config.require("wind.lee_tolerance_deg"))
    cross_tolerance = float(config.require("wind.cross_loading_tolerance_deg"))
    search_distance = float(config.require("wind.exposure_search_distance_m"))
    sector_width = float(config.require("wind.exposure_sector_width_deg"))
    weights = config.normalized_weights("wind.weights")

    # How much snow is the wind actually able to move? Below the saltation
    # threshold, none. This scales the entire field.
    transport_capacity = float(np.clip((wind_speed_kmh - transport) / max(strong - transport, 1e-6), 0.0, 1.0))
    if transport_capacity <= 0.0:
        warnings.append(
            f"Wind speed {wind_speed_kmh:.0f} km/h is below the {transport:.0f} km/h snow-transport "
            f"threshold. Snow is not being redistributed, so wind loading is zero -- not missing, "
            f"but genuinely zero."
        )

    # A wind that swung through every point of the compass loads no single slope.
    consistency = float(wind_consistency) if wind_consistency is not None else 0.7
    if wind_consistency is None:
        warnings.append(
            "Wind direction consistency was unavailable; assumed 0.7. A variable wind loads lee "
            "slopes less effectively than a steady one of the same speed."
        )

    aspect_values = np.asarray(aspect.filled(-1.0))
    slope_values = np.asarray(slope.filled(0.0))
    is_flat = aspect_values < 0

    # --- Geometry: which slopes are lee, windward, or cross-loaded? -----------
    # The wind blows FROM `wind_direction_deg`. A slope in the lee therefore FACES
    # the downwind direction, i.e. its aspect is opposite the wind bearing.
    downwind = (wind_direction_deg + 180.0) % 360.0

    to_downwind = angular_difference(aspect_values, downwind)     # 0 = perfectly lee
    to_windward = angular_difference(aspect_values, wind_direction_deg)  # 0 = perfectly windward

    lee_alignment = np.clip(1.0 - to_downwind / lee_tolerance, 0.0, 1.0)
    windward_alignment = np.clip(1.0 - to_windward / lee_tolerance, 0.0, 1.0)
    # Cross-loading peaks where the slope faces perpendicular to the wind, i.e.
    # 90 degrees away from both the lee and the windward alignment.
    perpendicular = np.abs(to_downwind - 90.0)
    cross_alignment = np.clip(1.0 - perpendicular / cross_tolerance, 0.0, 1.0)

    for field_ in (lee_alignment, windward_alignment, cross_alignment):
        field_[is_flat] = 0.0

    # --- Terrain exposure (Winstral Sx) ---------------------------------------
    sx = terrain_exposure_sx(elevation, grid, wind_direction_deg, search_distance, sector_width)
    sx_values = np.asarray(sx.filled(0.0))
    # Positive Sx = sheltered = deposition. Negative = exposed = scoured.
    shelter = np.clip(sx_values / 20.0, -1.0, 1.0)
    deposition_shelter = np.clip(shelter, 0.0, 1.0)
    exposure_scour = np.clip(-shelter, 0.0, 1.0)

    # --- Slope steepness gate -------------------------------------------------
    # Snow does not build a slab on a 5-degree meadow or a 70-degree cliff.
    slope_gate = np.clip((slope_values - 20.0) / 15.0, 0.0, 1.0) * np.clip(
        (60.0 - slope_values) / 15.0, 0.0, 1.0
    )

    # --- Forest gate ----------------------------------------------------------
    # Wind does not scour or load inside a dense forest canopy.
    in_forest = np.asarray(forest_mask.filled(0.0)) > 0.5
    forest_gate = np.where(in_forest, 0.25, 1.0)

    # --- Curvature ------------------------------------------------------------
    # Concave (convergent) terrain collects blown snow; convex sheds it.
    plan = np.asarray(plan_curvature.filled(0.0))
    plan_scale = float(np.percentile(np.abs(plan_curvature.compressed()), 90)) if plan_curvature.count() else 1.0
    concavity = np.clip(-plan / (plan_scale or 1.0), 0.0, 1.0)

    # --- Ridge proximity ------------------------------------------------------
    ridge_distance = np.asarray(distance_to_ridge.filled(9999.0))
    ridge_factor = np.clip(1.0 - ridge_distance / 300.0, 0.0, 1.0)

    # --- Assemble -------------------------------------------------------------
    scale = transport_capacity * consistency

    erosion = windward_alignment * exposure_scour * slope_gate * forest_gate * scale * 100.0
    deposition = lee_alignment * (0.6 + 0.4 * deposition_shelter) * slope_gate * forest_gate * scale * 100.0
    cross = cross_alignment * (0.5 + 0.5 * concavity) * slope_gate * forest_gate * scale * 100.0
    ridge_exposure = ridge_factor * exposure_scour * scale * 100.0

    # A cornice needs a ridge, a lee side, and wind able to move snow over it.
    cornice = (
        np.clip(1.0 - ridge_distance / 40.0, 0.0, 1.0)
        * lee_alignment
        * scale
        * 100.0
    )

    combined = (
        weights.get("lee_deposition", 0.0) * deposition
        + weights.get("terrain_exposure", 0.0) * (deposition_shelter * scale * 100.0)
        + weights.get("cross_loading", 0.0) * cross
        + weights.get("ridge_proximity", 0.0) * ridge_factor * scale * 100.0
        + weights.get("curvature", 0.0) * concavity * scale * 100.0
    )
    combined = combined * slope_gate * forest_gate

    # --- Snow availability gate ----------------------------------------------
    # The rule that stops this model reporting a wind slab on bare rock.
    if bool(config.get("wind.requires_available_snow", True)):
        if snow_availability is None:
            warnings.append(
                "Snow availability was unknown, so wind loading could not be gated on the presence "
                "of transportable snow. The field shows where wind WOULD load snow if snow were "
                "there -- it is a terrain-and-wind result, not an observation of loading."
            )
        else:
            snow = np.clip(np.asarray(snow_availability.filled(0.0)), 0.0, 1.0)
            erosion *= snow
            deposition *= snow
            cross *= snow
            cornice *= snow
            combined *= snow

    def wrap(values: np.ndarray) -> np.ma.MaskedArray:
        return np.ma.array(np.clip(values, 0.0, 100.0).astype("float32"), mask=mask)

    explanation = {
        "available": True,
        "wind_speed_kmh": round(wind_speed_kmh, 1),
        "wind_direction_from_deg": round(wind_direction_deg, 1),
        "downwind_direction_deg": round(downwind, 1),
        "wind_consistency": round(consistency, 3),
        "transport_capacity": round(transport_capacity, 3),
        "transport_threshold_kmh": transport,
        "weights_normalized": {key: round(value, 4) for key, value in weights.items()},
        "method": (
            "Lee/windward/cross alignment from the angle between slope aspect and the wind vector; "
            "terrain shelter from a Winstral-style upwind horizon angle (Sx) over a "
            f"{search_distance:.0f} m search distance in a {sector_width:.0f} degree sector."
        ),
        "assumptions": [
            "The wind measured at the valley station is assumed to apply across the whole mountain. "
            "In reality ridge-top wind is stronger and is steered by the terrain; there is no "
            "ridge-top anemometer in this dataset, so that gradient cannot be observed.",
            "Snow transport begins at the configured threshold and saturates at the strong-wind "
            "threshold. The real relationship is nonlinear and depends on snow age, temperature "
            "and crusting, none of which we can measure here.",
            "Wind loading is multiplied by snow availability, so no loading is reported where there "
            "is no snow to transport.",
        ],
        "warnings": warnings,
    }

    return WindFields(
        terrain_exposure=sx,
        windward_erosion=wrap(erosion),
        lee_deposition=wrap(deposition),
        cross_loading=wrap(cross),
        ridge_exposure=wrap(ridge_exposure),
        cornice_potential=wrap(cornice),
        wind_loading=wrap(combined),
        available=True,
        wind_speed_kmh=wind_speed_kmh,
        wind_direction_deg=wind_direction_deg,
        wind_consistency=consistency,
        warnings=warnings,
        explanation=explanation,
    )
