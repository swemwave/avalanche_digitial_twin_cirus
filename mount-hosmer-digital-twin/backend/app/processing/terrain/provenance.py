"""Pure helpers for preserving forest-classification lineage and missingness."""

from __future__ import annotations

import numpy as np


FOREST_SOURCE_CODES = {"none": 0, "lidar_canopy": 1, "esa_worldcover": 2}
FOREST_SOURCE_LABELS = {
    0: "No forest-cover data",
    1: "Forest classification from LiDAR DSM minus DEM canopy height",
    2: "Forest classification from ESA WorldCover 2021",
}


def combine_forest_classifications(
    *,
    canopy_forest: np.ndarray,
    canopy_known: np.ndarray,
    landcover_forest: np.ndarray,
    landcover_known: np.ndarray,
    dem_mask: np.ndarray,
) -> tuple[np.ma.MaskedArray, np.ma.MaskedArray]:
    """Prefer LiDAR canopy, fall back to WorldCover, and mask unknown cells.

    A cell with neither source is missing; it must never become the safe-looking
    numeric value ``forest = 0``.
    """
    fallback = ~canopy_known & landcover_known
    source = np.zeros(canopy_known.shape, dtype="uint8")
    source[canopy_known] = FOREST_SOURCE_CODES["lidar_canopy"]
    source[fallback] = FOREST_SOURCE_CODES["esa_worldcover"]
    forest = np.where(canopy_known, canopy_forest, landcover_forest).astype("float32")
    mask = np.asarray(dem_mask, dtype=bool) | (source == FOREST_SOURCE_CODES["none"])
    return (
        np.ma.array(forest, mask=mask),
        np.ma.array(source, mask=(source == FOREST_SOURCE_CODES["none"])),
    )
