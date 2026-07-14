"""Build the best available elevation model for the AOI, with per-pixel provenance.

The important thing this module does is *merge LiDAR across acquisition years*.

The BC LiDAR holdings for Mount Hosmer are four 1:20,000 mapsheets, each flown
twice (2016 and 2022). Neither flight is complete: each leaves nodata gaps, and
crucially the gaps are in **different places**. Measured against the 12x12 km AOI
grid:

    2022 tiles alone ................ 62.2 % coverage
    2016 tiles alone ................ 44.4 % coverage
    2022 preferred, 2016 gap-filling . 99.9 % coverage

The previous implementation selected one year per mapsheet, saw 62 %, decided
that was not enough, and silently fell back to the 30 m Copernicus DEM for the
entire mountain. The 1 m data was there the whole time.

So: newest-first, gap-filled by older acquisitions, and finally by Copernicus
only where no LiDAR of any vintage exists. Every pixel records which source it
came from in a companion provenance raster, because a 2016 surface and a 2022
surface are not the same measurement and the user is entitled to know which one
they are looking at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.core.settings import Settings
from app.processing.harmonization.grids import NODATA, AnalysisGrid
from app.processing.harmonization.raster_io import Semantics, read_aligned

#: Codes written into the provenance raster. Ascending code = descending
#: preference, so "lowest code wins" is the merge rule.
SOURCE_CODES = {
    "lidar_2022": 1,
    "lidar_2016": 2,
    "copernicus_glo30": 3,
    "none": 0,
}

SOURCE_LABELS = {
    1: "BC LiDAR 1 m, 2022 acquisition",
    2: "BC LiDAR 1 m, 2016 acquisition",
    3: "Copernicus GLO-30 30 m (no LiDAR coverage at this pixel)",
    0: "No elevation data",
}

SOURCE_RESOLUTION_M = {1: 1.0, 2: 1.0, 3: 30.0, 0: None}

YEAR_RE = re.compile(r"_(\d{4})(?:_dsm)?\.tif$", re.IGNORECASE)


@dataclass
class TerrainModel:
    """An elevation surface on an analysis grid, and where every pixel came from."""

    elevation: np.ma.MaskedArray
    provenance: np.ndarray  # uint8, values from SOURCE_CODES
    grid: AnalysisGrid
    source_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> dict[str, float]:
        total = float(self.provenance.size)
        return {
            label: round(float((self.provenance == code).sum()) / total, 6)
            for label, code in SOURCE_CODES.items()
        }

    @property
    def lidar_fraction(self) -> float:
        lidar = np.isin(self.provenance, [SOURCE_CODES["lidar_2022"], SOURCE_CODES["lidar_2016"]])
        return round(float(lidar.sum()) / float(self.provenance.size), 6)

    @property
    def valid_fraction(self) -> float:
        return round(float((self.provenance != 0).sum()) / float(self.provenance.size), 6)

    @property
    def effective_source_resolution_m(self) -> float | None:
        """Coverage-weighted native resolution of the sources actually used.

        Reported so nobody can claim "1 m terrain" when a tenth of the mountain
        came from a 30 m DEM.
        """
        valid = self.provenance[self.provenance != 0]
        if valid.size == 0:
            return None
        resolutions = np.array([SOURCE_RESOLUTION_M[int(code)] for code in valid], dtype="float64")
        return round(float(resolutions.mean()), 3)

    def describe(self) -> dict[str, object]:
        return {
            "grid": self.grid.describe(),
            "coverage_by_source": self.coverage,
            "coverage_by_source_label": {
                SOURCE_LABELS[code]: round(
                    float((self.provenance == code).sum()) / float(self.provenance.size), 6
                )
                for code in sorted(set(int(value) for value in np.unique(self.provenance)))
            },
            "lidar_fraction": self.lidar_fraction,
            "valid_fraction": self.valid_fraction,
            "effective_source_resolution_m": self.effective_source_resolution_m,
            "source_file_count": len(self.source_files),
            "warnings": self.warnings,
        }


def _tile_year(path: Path) -> int:
    match = YEAR_RE.search(path.name)
    return int(match.group(1)) if match else 0


def lidar_tiles(folder: Path) -> dict[int, list[Path]]:
    """Group LiDAR tiles by acquisition year, newest year first.

    Unlike the previous ``select_latest_lidar_tiles``, this keeps *every* tile.
    Older acquisitions are not redundant; they fill the newer flight's gaps.
    """
    if not folder.exists():
        return {}
    by_year: dict[int, list[Path]] = {}
    for path in sorted(folder.glob("*.tif")):
        year = _tile_year(path)
        if year == 0:
            continue
        by_year.setdefault(year, []).append(path)
    return dict(sorted(by_year.items(), reverse=True))


def _merge(
    target: np.ndarray,
    provenance: np.ndarray,
    incoming: np.ma.MaskedArray,
    code: int,
) -> None:
    """Fill only the pixels that are still empty. First writer wins."""
    fillable = (provenance == 0) & ~np.ma.getmaskarray(incoming)
    target[fillable] = np.asarray(incoming)[fillable]
    provenance[fillable] = code


def _mosaic_lidar(
    folder: Path,
    grid: AnalysisGrid,
    elevation: np.ndarray,
    provenance: np.ndarray,
    warnings: list[str],
    used: list[Path],
) -> None:
    by_year = lidar_tiles(folder)
    if not by_year:
        warnings.append(f"No BC LiDAR tiles found in {folder.name}.")
        return

    for year, paths in by_year.items():  # newest year first
        code = SOURCE_CODES.get(f"lidar_{year}")
        if code is None:
            # An acquisition year we have not assigned a code to. Rather than
            # drop the data, treat it as the oldest LiDAR tier.
            code = SOURCE_CODES["lidar_2016"]
            warnings.append(
                f"LiDAR acquisition year {year} has no dedicated provenance code; "
                f"recorded as the older LiDAR tier."
            )
        for path in paths:
            try:
                tile = read_aligned(path, grid, Semantics.CONTINUOUS)
            except Exception as exc:
                warnings.append(f"LiDAR tile could not be read: {path.name}: {exc}")
                continue
            before = int((provenance == 0).sum())
            _merge(elevation, provenance, tile, code)
            if int((provenance == 0).sum()) < before:
                used.append(path)


def _fill_from_copernicus(
    settings: Settings,
    grid: AnalysisGrid,
    elevation: np.ndarray,
    provenance: np.ndarray,
    warnings: list[str],
    used: list[Path],
) -> None:
    path = settings.data_root / "static" / "terrain_fallback" / "Copernicus_DEM_GLO30_EPSG26911_30m.tif"
    remaining = int((provenance == 0).sum())
    if remaining == 0:
        return
    if not path.exists():
        warnings.append(
            f"{remaining} pixels have no LiDAR coverage and the Copernicus fallback DEM is "
            f"missing; those pixels remain NoData."
        )
        return
    # Upsampling 30 m to a 5 m grid does not create 5 m information. It is
    # bilinear interpolation and is tagged as such in the provenance raster, so
    # downstream confidence scoring can discount these pixels.
    coarse = read_aligned(path, grid, Semantics.CONTINUOUS)
    _merge(elevation, provenance, coarse, SOURCE_CODES["copernicus_glo30"])
    used.append(path)
    filled = remaining - int((provenance == 0).sum())
    if filled:
        warnings.append(
            f"{filled} pixels ({filled / provenance.size:.2%} of the AOI) had no LiDAR coverage "
            f"and were filled from the 30 m Copernicus DEM, then interpolated to the "
            f"{grid.resolution_m:g} m grid. Terrain derivatives there are less reliable."
        )


def build_dem(settings: Settings, grid: AnalysisGrid) -> TerrainModel:
    """Best-available bare-earth elevation on ``grid``."""
    elevation = np.full(grid.shape, NODATA, dtype="float32")
    provenance = np.zeros(grid.shape, dtype="uint8")
    warnings: list[str] = []
    used: list[Path] = []

    folder = (
        settings.data_root
        / "static"
        / "lidar_bc"
        / "downloads"
        / "LiDAR_DEM_Index_1_20_000"
    )
    _mosaic_lidar(folder, grid, elevation, provenance, warnings, used)
    _fill_from_copernicus(settings, grid, elevation, provenance, warnings, used)

    masked = np.ma.array(elevation, mask=(provenance == 0))
    model = TerrainModel(masked, provenance, grid, used, warnings)

    if model.lidar_fraction < 0.5:
        warnings.append(
            f"Only {model.lidar_fraction:.1%} of the AOI is backed by LiDAR. Terrain derivatives "
            f"are dominated by the 30 m DEM and should be treated as coarse."
        )
    return model


def build_dsm(settings: Settings, grid: AnalysisGrid) -> TerrainModel | None:
    """Best-available surface (first-return) elevation on ``grid``.

    Returns ``None`` when no DSM tiles exist. There is no coarse fallback: a DSM
    has no meaningful 30 m equivalent, and inventing one would corrupt the canopy
    height model that is derived from it.
    """
    folder = (
        settings.data_root
        / "static"
        / "lidar_bc"
        / "downloads"
        / "LiDAR_DSM_Index_1_20_000"
    )
    if not folder.exists():
        return None

    elevation = np.full(grid.shape, NODATA, dtype="float32")
    provenance = np.zeros(grid.shape, dtype="uint8")
    warnings: list[str] = []
    used: list[Path] = []
    _mosaic_lidar(folder, grid, elevation, provenance, warnings, used)

    if not used:
        return None

    masked = np.ma.array(elevation, mask=(provenance == 0))
    model = TerrainModel(masked, provenance, grid, used, warnings)
    if model.valid_fraction < 1.0:
        warnings.append(
            f"LiDAR DSM covers {model.valid_fraction:.1%} of the AOI. Canopy height is only "
            f"computed where both a DSM and a DEM pixel exist; elsewhere it is NoData, not zero."
        )
    return model
