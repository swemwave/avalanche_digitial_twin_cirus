"""Build the six static terrain layers consumed by the Stage 3 runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from app.core.model_config import ModelConfig
from app.core.settings import Settings
from app.processing.harmonization.grids import AnalysisGrid, terrain_grid
from app.processing.harmonization.raster_io import Semantics, read_aligned
from app.processing.mountain_pack import MountainPackError, load_mountain_pack
from app.processing.terrain import derivatives as dv
from app.processing.terrain.mosaic import build_dem, build_dsm
from app.processing.terrain.provenance import (
    FOREST_SOURCE_CODES,
    FOREST_SOURCE_LABELS,
    combine_forest_classifications,
)

FOREST_CLASSES = (10, 95)


def source_paths(settings: Settings) -> dict[str, Path]:
    pack, _ = load_mountain_pack(settings)
    asset = pack.assets["landcover"]
    if asset.adapter != "categorical_raster":
        raise MountainPackError("landcover currently requires adapter='categorical_raster'.")
    return {"landcover": pack.asset_path(settings.data_root, "landcover")}


class TerrainProducts:
    def __init__(self, grid: AnalysisGrid) -> None:
        self.layers: dict[str, np.ma.MaskedArray] = {}
        self.grid = grid
        self.warnings: list[str] = []

    def __getitem__(self, key: str) -> np.ma.MaskedArray:
        return self.layers[key]

    def __contains__(self, key: str) -> bool:
        return key in self.layers


def _forest_mask(
    settings: Settings,
    config: ModelConfig,
    grid: AnalysisGrid,
    dem: np.ma.MaskedArray,
    warnings: list[str],
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray, dict[str, Any]]:
    landcover_path = source_paths(settings)["landcover"]
    if landcover_path.exists():
        landcover = read_aligned(landcover_path, grid, Semantics.CATEGORICAL)
        has_landcover = True
    else:
        landcover = np.ma.masked_all(grid.shape, dtype="float32")
        has_landcover = False
        warnings.append(
            "The declared optional land-cover raster is missing; cells remain unknown "
            "unless covered by an available canopy surface."
        )

    dsm = build_dsm(settings, grid)
    if dsm is None:
        warnings.append(
            "No canopy surface is declared; cells remain unknown unless covered by the "
            "land-cover classification."
        )
        if not has_landcover:
            warnings.append(
                "No forest-cover source is available; the forest layer remains fully masked."
            )
        known = np.zeros(grid.shape, dtype=bool)
        canopy_forest = np.zeros(grid.shape, dtype=bool)
        dsm_sources: list[Path] = []
    else:
        warnings.extend(dsm.warnings)
        canopy = dv.canopy_height(dsm.elevation, dem)
        known = ~np.ma.getmaskarray(canopy)
        canopy_forest = np.asarray(canopy.filled(0.0)) >= float(
            config.require("terrain.forest_canopy_height_m")
        )
        dsm_sources = list(dsm.source_files)

    landcover_forest = np.isin(np.asarray(landcover.filled(-1)).astype(int), FOREST_CLASSES)
    landcover_known = ~np.ma.getmaskarray(landcover)
    forest, forest_source = combine_forest_classifications(
        canopy_forest=canopy_forest,
        canopy_known=known,
        landcover_forest=landcover_forest,
        landcover_known=landcover_known,
        dem_mask=np.ma.getmaskarray(dem),
    )
    source = np.asarray(forest_source.filled(FOREST_SOURCE_CODES["none"]), dtype="uint8")
    source_files = dsm_sources + ([landcover_path] if landcover_path.exists() else [])
    counts = {
        FOREST_SOURCE_LABELS[code]: round(float((source == code).sum()) / source.size, 6)
        for code in sorted(FOREST_SOURCE_LABELS)
    }
    return (
        forest,
        forest_source,
        {"coverage_by_source_label": counts, "source_files": source_files},
    )


def compute(settings: Settings, config: ModelConfig) -> tuple[TerrainProducts, dict[str, Any]]:
    """Read source surfaces once and derive only runtime-consumed products."""
    grid = terrain_grid(settings)
    products = TerrainProducts(grid)
    dem_model = build_dem(settings, grid)
    dem = dem_model.elevation
    products.warnings.extend(dem_model.warnings)
    if dem.count() == 0:
        raise RuntimeError("No elevation data could be assembled for the AOI.")

    slope, aspect = dv.slope_aspect(dem, grid)
    curvature = dv.curvatures(dem, grid)
    forest, forest_source, forest_meta = _forest_mask(
        settings, config, grid, dem, products.warnings
    )
    terrain_source = np.ma.array(dem_model.provenance, mask=(dem_model.provenance == 0))

    products.layers.update(
        elevation=dem,
        slope=slope,
        aspect=aspect,
        plan_curvature=curvature["plan_curvature"],
        general_curvature=curvature["general_curvature"],
        forest_mask=forest,
        terrain_source=terrain_source,
        forest_source=forest_source,
    )
    dem_description = dem_model.describe()
    return products, {
        "terrain_model": dem_description,
        "terrain_source_codes": dem_description["source_codes"],
        "forest_model": {
            "source_codes": {
                str(code): FOREST_SOURCE_LABELS[code] for code in sorted(FOREST_SOURCE_LABELS)
            },
            "coverage_by_source_label": forest_meta["coverage_by_source_label"],
        },
        "source_files": list(dem_model.source_files) + list(forest_meta["source_files"]),
    }
