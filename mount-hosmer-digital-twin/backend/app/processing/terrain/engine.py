"""The terrain engine: builds every static terrain product for the AOI, once.

Everything here is time-invariant. Slope does not change between January and
April, so it is computed once, cached against a content hash of its inputs, and
reused by every analysis and every simulation. The time-varying half of the model
(snow, wind, weather) reads these products rather than recomputing them.

Outputs land on the terrain grid (5 m by default, from 1 m LiDAR) as tiled,
overview-bearing GeoTIFFs, each with a metadata sidecar recording its sources,
algorithm, parameters and checksum.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from app.core.model_config import DISCLAIMER, ModelConfig
from app.core.numerics import piecewise
from app.core.settings import Settings
from app.processing.harmonization.grids import AnalysisGrid, grid_set
from app.processing.harmonization.raster_io import Semantics, read_aligned
from app.processing.terrain import derivatives as dv
from app.processing.terrain.mosaic import build_dem, build_dsm

ESA_WORLDCOVER = {
    10: {"label": "Tree cover", "color": "#0b6b3a", "forest": True},
    20: {"label": "Shrubland", "color": "#b7a642", "forest": False},
    30: {"label": "Grassland", "color": "#d8d85d", "forest": False},
    40: {"label": "Cropland", "color": "#c48a4a", "forest": False},
    50: {"label": "Built-up", "color": "#c65f5f", "forest": False},
    60: {"label": "Bare / sparse vegetation", "color": "#d4c0a1", "forest": False},
    70: {"label": "Snow and ice", "color": "#e9f4ff", "forest": False},
    80: {"label": "Permanent water", "color": "#3f82c4", "forest": False},
    90: {"label": "Herbaceous wetland", "color": "#64a889", "forest": False},
    95: {"label": "Mangroves", "color": "#1d5d44", "forest": True},
    100: {"label": "Moss and lichen", "color": "#c5d7a0", "forest": False},
}


def source_paths(settings: Settings) -> dict[str, Path]:
    root = settings.data_root
    return {
        "lidar_dem_dir": root / "static" / "lidar_bc" / "downloads" / "LiDAR_DEM_Index_1_20_000",
        "lidar_dsm_dir": root / "static" / "lidar_bc" / "downloads" / "LiDAR_DSM_Index_1_20_000",
        "copernicus_dem": root / "static" / "terrain_fallback" / "Copernicus_DEM_GLO30_EPSG26911_30m.tif",
        "landcover": root / "static" / "landcover" / "ESA_WorldCover_2021_EPSG26911_10m.tif",
        "aoi": root / "metadata" / "mount_hosmer_aoi.geojson",
        "grid": root / "metadata" / "grid_and_aoi.json",
    }


def _percentile(array: np.ma.MaskedArray, q: float) -> float:
    values = array.compressed()
    return float(np.percentile(values, q)) if values.size else 0.0


class TerrainProducts:
    """Everything the terrain engine computed, in memory, for downstream models."""

    def __init__(self) -> None:
        self.layers: dict[str, np.ma.MaskedArray] = {}
        self.grid: AnalysisGrid | None = None
        self.warnings: list[str] = []

    def __getitem__(self, key: str) -> np.ma.MaskedArray:
        return self.layers[key]

    def __contains__(self, key: str) -> bool:
        return key in self.layers


def compute(settings: Settings, config: ModelConfig) -> tuple[TerrainProducts, dict[str, Any]]:
    """Derive every terrain product. Pure computation; no I/O beyond reading sources."""
    grids = grid_set(settings)
    grid = grids.terrain
    products = TerrainProducts()
    products.grid = grid

    dem_model = build_dem(settings, grid)
    dem = dem_model.elevation
    products.warnings.extend(dem_model.warnings)

    if dem.count() == 0:
        raise RuntimeError(
            "No elevation data could be assembled for the AOI. Check AVALANCHE_DATA_ROOT."
        )

    slope, aspect = dv.slope_aspect(dem, grid)
    curvature = dv.curvatures(dem, grid)
    rugged = dv.ruggedness(dem, grid)
    tpi = dv.topographic_position(dem, grid, (25.0, 100.0, 500.0))
    flow = dv.flow_network(dem, grid)
    mask = np.ma.getmaskarray(dem)

    products.layers.update(
        {
            "elevation": dem,
            "slope": slope,
            "aspect": aspect,
            "hillshade": dv.hillshade(dem, grid),
            **curvature,
            **rugged,
            **tpi,
            "flow_direction": np.ma.array(flow.direction.astype("float32"), mask=mask),
            "flow_accumulation": np.ma.array(flow.accumulation.astype("float32"), mask=mask),
        }
    )
    products.layers["terrain_source"] = np.ma.array(
        dem_model.provenance.astype("float32"), mask=(dem_model.provenance == 0)
    )

    # --- Land cover, on its native 10 m grid, resampled by mode to 5 m ---------
    landcover_path = source_paths(settings)["landcover"]
    if landcover_path.exists():
        landcover = read_aligned(landcover_path, grid, Semantics.CATEGORICAL)
    else:
        landcover = np.ma.array(np.zeros(grid.shape, dtype="float32"), mask=np.ones(grid.shape, bool))
        products.warnings.append("ESA WorldCover is missing; forest cover falls back to LiDAR canopy only.")
    products.layers["landcover"] = landcover

    # --- Canopy height, forest, open terrain ---------------------------------
    dsm_model = build_dsm(settings, grid)
    forest_canopy_m = float(config.require("terrain.forest_canopy_height_m"))
    open_canopy_m = float(config.require("terrain.open_canopy_height_m"))

    if dsm_model is not None:
        products.warnings.extend(dsm_model.warnings)
        chm = dv.canopy_height(dsm_model.elevation, dem)
        products.layers["canopy_height"] = chm
        products.layers["surface_elevation"] = dsm_model.elevation
        chm_values = np.asarray(chm.filled(0.0))
        chm_known = ~np.ma.getmaskarray(chm)
        forest = (chm_values >= forest_canopy_m) & chm_known
        open_terrain = (chm_values < open_canopy_m) & chm_known
        canopy_provenance = "derived"
    else:
        products.warnings.append(
            "No LiDAR DSM available; canopy height was not computed and forest cover is taken "
            "from ESA WorldCover class alone."
        )
        forest = np.zeros(grid.shape, dtype=bool)
        open_terrain = np.zeros(grid.shape, dtype=bool)
        chm_known = np.zeros(grid.shape, dtype=bool)
        canopy_provenance = "unavailable"

    # Where LiDAR canopy is unknown, fall back to the land-cover class. This is a
    # coarser answer, and it is recorded as such.
    lc = np.asarray(landcover.filled(-1)).astype(int)
    lc_forest = np.isin(lc, [code for code, item in ESA_WORLDCOVER.items() if item["forest"]])
    lc_open = np.isin(lc, [20, 30, 40, 60, 70, 100])
    forest = np.where(chm_known, forest, lc_forest)
    open_terrain = np.where(chm_known, open_terrain, lc_open)

    products.layers["forest_mask"] = np.ma.array(forest.astype("float32"), mask=mask)
    products.layers["open_terrain_mask"] = np.ma.array(open_terrain.astype("float32"), mask=mask)

    # --- Ridges, gullies, drainage -------------------------------------------
    tpi_broad = tpi["tpi_500m"]
    tpi_local = tpi["tpi_100m"]
    ridge_threshold = _percentile(tpi_broad, float(config.require("terrain.ridge_tpi_percentile")))
    gully_threshold = _percentile(tpi_local, float(config.require("terrain.gully_tpi_percentile")))
    accumulation_threshold = _percentile(
        products.layers["flow_accumulation"],
        float(config.require("terrain.drainage_accumulation_percentile")),
    )

    plan = curvature["plan_curvature"]
    ridges = (np.asarray(tpi_broad.filled(0)) >= ridge_threshold) & (
        np.asarray(curvature["general_curvature"].filled(0)) > 0
    )
    gullies = (np.asarray(tpi_local.filled(0)) <= gully_threshold) & (np.asarray(plan.filled(0)) < 0)
    drainage = np.asarray(products.layers["flow_accumulation"].filled(0)) >= accumulation_threshold

    products.layers["ridge_mask"] = np.ma.array(ridges.astype("float32"), mask=mask)
    products.layers["gully_mask"] = np.ma.array(gullies.astype("float32"), mask=mask)
    products.layers["drainage_mask"] = np.ma.array(drainage.astype("float32"), mask=mask)

    distance_to_ridge = dv.distance_to(ridges, grid)
    distance_to_drainage = dv.distance_to(drainage, grid)
    products.layers["distance_to_ridge"] = np.ma.array(
        np.where(np.isinf(distance_to_ridge), 9999.0, distance_to_ridge).astype("float32"), mask=mask
    )
    products.layers["distance_to_drainage"] = np.ma.array(
        np.where(np.isinf(distance_to_drainage), 9999.0, distance_to_drainage).astype("float32"),
        mask=mask,
    )

    # --- Elevation bands ------------------------------------------------------
    products.layers["elevation_band"] = dv.elevation_bands(
        dem,
        float(config.require("terrain.below_treeline_max_m")),
        float(config.require("terrain.treeline_max_m")),
    )

    # --- Continuity -----------------------------------------------------------
    continuity = dv.slope_band_continuity(
        slope,
        grid,
        low_deg=float(config.require("terrain.slope.min_deg")),
        high_deg=float(config.require("terrain.slope.max_deg")),
        radius_m=float(config.require("terrain.continuity_radius_m")),
    )
    products.layers["terrain_continuity"] = continuity

    # --- Terrain traps --------------------------------------------------------
    # A gully is only a trap if steep terrain feeds into it. A flat ditch in a
    # meadow is not a terrain trap.
    trap_plan = float(config.require("terrain.terrain_trap.convergent_plan_curvature"))
    slope_above_deg = float(config.require("terrain.terrain_trap.minimum_slope_above_deg"))
    steep = np.asarray(slope.filled(0.0)) >= slope_above_deg
    cells = max(1, int(round(150.0 / grid.resolution_m)))
    steep_nearby = ndimage.maximum_filter(steep.astype("float32"), size=2 * cells + 1, mode="nearest") > 0
    traps = (np.asarray(plan.filled(0.0)) <= trap_plan) & (gullies | drainage) & steep_nearby
    products.layers["terrain_trap_mask"] = np.ma.array(traps.astype("float32"), mask=mask)

    # --- Cornice terrain ------------------------------------------------------
    # Static cornice *potential*: a sharp crest with a steep drop nearby. Which
    # side actually grows a cornice depends on the wind, and that is decided by
    # the wind model, not here.
    cornice_distance = float(config.require("terrain.cornice.max_distance_to_ridge_m"))
    cornice_slope = float(config.require("terrain.cornice.minimum_lee_slope_deg"))
    steep_enough = ndimage.maximum_filter(
        (np.asarray(slope.filled(0.0)) >= cornice_slope).astype("float32"),
        size=2 * max(1, int(round(cornice_distance / grid.resolution_m))) + 1,
        mode="nearest",
    ) > 0
    cornice = (np.asarray(products.layers["distance_to_ridge"].filled(9999)) <= cornice_distance) & steep_enough
    products.layers["cornice_terrain"] = np.ma.array(cornice.astype("float32"), mask=mask)

    # --- Static terrain susceptibility ---------------------------------------
    susceptibility, explanation = _terrain_susceptibility(products, config, grid)
    products.layers["terrain_susceptibility"] = susceptibility

    # --- Starting zones (static component only) ------------------------------
    min_slope = float(config.require("terrain.slope.min_deg"))
    max_slope = float(config.require("terrain.slope.max_deg"))
    min_continuity = float(config.require("terrain.continuity_minimum_fraction"))
    slope_values = np.asarray(slope.filled(0.0))
    starting = (
        (slope_values >= min_slope)
        & (slope_values <= max_slope)
        & (np.asarray(continuity.filled(0.0)) >= min_continuity)
        & ~forest
    )
    products.layers["starting_zone_terrain"] = np.ma.array(starting.astype("float32"), mask=mask)

    metadata = {
        "terrain_model": dem_model.describe(),
        "flow": dv.summarize_flow(flow),
        "starting_zone_area_km2": round(
            float(starting.sum()) * grid.resolution_m**2 / 1_000_000.0, 4
        ),
        "susceptibility_explanation": explanation,
        "thresholds": {
            "ridge_tpi_500m": round(ridge_threshold, 3),
            "gully_tpi_100m": round(gully_threshold, 3),
            "drainage_accumulation_cells": round(accumulation_threshold, 1),
        },
        "canopy_provenance": canopy_provenance,
    }
    return products, metadata


def _terrain_susceptibility(
    products: TerrainProducts, config: ModelConfig, grid: AnalysisGrid
) -> tuple[np.ma.MaskedArray, dict[str, Any]]:
    """The static half of the release estimate. Time-invariant terrain only.

    No snow, no weather, no wind. This is the answer to "is this the kind of
    terrain that produces avalanches", not "will it avalanche today".
    """
    weights = config.normalized_weights("terrain_susceptibility.weights")
    slope = products["slope"]
    mask = np.ma.getmaskarray(slope)

    slope_score = piecewise(
        np.asarray(slope.filled(0.0)),
        list(config.require("terrain.slope.breakpoints_deg")),
        list(config.require("terrain.slope.scores")),
    )

    continuity_score = np.asarray(products["terrain_continuity"].filled(0.0)) * 100.0

    # Convex terrain puts a slab in tension. Concave terrain supports it. So the
    # curvature contribution is one-sided: convexity adds, concavity does not
    # subtract below neutral.
    general = np.asarray(products["general_curvature"].filled(0.0))
    scale = float(np.percentile(np.abs(products["general_curvature"].compressed()), 95)) or 1.0
    curvature_score = np.clip(general / scale, 0.0, 1.0) * 100.0

    # Dense forest anchors the snowpack. This is the one factor that *reduces*
    # susceptibility, so it is scored as the absence of forest.
    forest_score = (1.0 - np.asarray(products["forest_mask"].filled(0.0))) * 100.0

    band = np.asarray(products["elevation_band"].filled(1.0))
    elevation_score = np.select(
        [band >= 3.0, band >= 2.0], [100.0, 70.0], default=30.0
    ).astype("float32")

    # Rough ground anchors early-season snow, so high roughness *reduces*
    # susceptibility -- the opposite of the old prototype, which rewarded it.
    roughness = products["roughness"]
    roughness_scale = float(np.percentile(roughness.compressed(), 90)) if roughness.count() else 1.0
    roughness_score = (
        1.0 - np.clip(np.asarray(roughness.filled(0.0)) / (roughness_scale or 1.0), 0.0, 1.0)
    ) * 100.0

    ridge_distance = np.asarray(products["distance_to_ridge"].filled(9999.0))
    ridge_score = piecewise(ridge_distance, [0, 50, 150, 400, 1000], [100, 85, 55, 25, 5])

    aspect = np.asarray(products["aspect"].filled(-1.0))
    low, high = config.require("terrain_susceptibility.aspect_favoured_degrees")
    centre = np.deg2rad((float(low) + float(high)) / 2.0)
    delta = np.abs(np.angle(np.exp(1j * (np.deg2rad(aspect) - centre))))
    aspect_score = np.where(aspect < 0, 50.0, (1.0 - delta / np.pi) * 100.0).astype("float32")

    contributions = {
        "slope": slope_score,
        "continuity": continuity_score,
        "curvature": curvature_score,
        "forest_cover": forest_score,
        "elevation_band": elevation_score,
        "roughness": roughness_score,
        "ridge_proximity": ridge_score,
        "aspect": aspect_score,
    }

    total = np.zeros(grid.shape, dtype="float32")
    for name, weight in weights.items():
        if name not in contributions:
            raise KeyError(
                f"terrain_susceptibility.weights has an unknown factor {name!r}. "
                f"Known factors: {sorted(contributions)}"
            )
        total += contributions[name] * weight

    # Terrain outside the slope window cannot produce a slab at all, whatever the
    # other factors say. A 10-degree forested bench with perfect continuity is
    # still a 10-degree bench.
    min_slope = float(config.require("terrain.slope.min_deg"))
    max_slope = float(config.require("terrain.slope.max_deg"))
    slope_values = np.asarray(slope.filled(0.0))
    outside = (slope_values < min_slope) | (slope_values > max_slope)
    total[outside] *= 0.15

    score = np.ma.array(np.clip(total, 0.0, 100.0).astype("float32"), mask=mask)

    explanation = {
        "model_type": "Weighted, rules-based static terrain susceptibility (0-100 relative index)",
        "is_probability": False,
        "disclaimer": DISCLAIMER,
        "weights_normalized": {key: round(value, 4) for key, value in weights.items()},
        "factors": [
            {
                "factor": "Slope",
                "weight": round(weights.get("slope", 0.0), 4),
                "source": f"BC LiDAR 1 m DEM, resampled to {grid.resolution_m:g} m",
                "provenance": "derived",
                "reason": "Slab release concentrates between 34 and 45 degrees; below 25 and above 60 it is rare.",
            },
            {
                "factor": "Terrain continuity",
                "weight": round(weights.get("continuity", 0.0), 4),
                "source": "Fraction of the surrounding terrain also in the avalanche slope band",
                "provenance": "derived",
                "reason": "A steep pixel isolated among cliffs cannot support a propagating slab; an unbroken face can.",
            },
            {
                "factor": "Curvature",
                "weight": round(weights.get("curvature", 0.0), 4),
                "source": "Zevenbergen-Thorne general curvature",
                "provenance": "derived",
                "reason": "Convex rolls put a slab into tension and are common trigger points.",
            },
            {
                "factor": "Forest cover",
                "weight": round(weights.get("forest_cover", 0.0), 4),
                "source": "LiDAR canopy height model, with ESA WorldCover where LiDAR is absent",
                "provenance": "derived",
                "reason": "Dense standing timber anchors the snowpack and suppresses release. Open terrain does not.",
            },
            {
                "factor": "Elevation band",
                "weight": round(weights.get("elevation_band", 0.0), 4),
                "source": "DEM, banded at the configured treeline",
                "provenance": "derived",
                "reason": "Alpine terrain is colder, windier and more often loaded.",
            },
            {
                "factor": "Surface roughness",
                "weight": round(weights.get("roughness", 0.0), 4),
                "source": "3x3 relief on the LiDAR DEM",
                "provenance": "derived",
                "reason": "Boulders and stumps anchor early-season snow, so rough ground scores LOWER, not higher.",
            },
            {
                "factor": "Ridge proximity",
                "weight": round(weights.get("ridge_proximity", 0.0), 4),
                "source": "Distance to the nearest ridge crest",
                "provenance": "derived",
                "reason": "Wind-loaded start zones sit immediately below ridge crests.",
            },
            {
                "factor": "Aspect",
                "weight": round(weights.get("aspect", 0.0), 4),
                "source": "DEM aspect",
                "provenance": "derived",
                "reason": str(config.get("terrain_susceptibility.aspect_static_note", "")).strip(),
            },
        ],
        "limitations": [
            "This score describes terrain only. It says nothing about whether snow is present, "
            "whether it is unstable, or whether anything will release today.",
            "It has not been calibrated against observed Mount Hosmer avalanches, because none are recorded.",
            "It is a relative index from 0 to 100, not a probability.",
        ],
    }
    return score, explanation
