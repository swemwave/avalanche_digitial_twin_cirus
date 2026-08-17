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
from app.processing.mountain_pack import (
    MountainPack,
    MountainPackError,
    PackAsset,
    load_mountain_pack,
)

#: Codes written into the provenance raster. Ascending code = descending
#: preference, so "lowest code wins" is the merge rule.
SOURCE_CODES = {
    "lidar_2022": 1,
    "lidar_2016": 2,
    # Compatibility name for the default Mount Hosmer pack. New packs use the
    # provider-neutral name; both intentionally encode the same fallback tier.
    "copernicus_glo30": 3,
    "fallback_dem": 3,
    "single_raster_dem": 4,
    "none": 0,
}

SOURCE_LABELS = {
    1: "BC LiDAR 1 m, 2022 acquisition",
    2: "BC LiDAR 1 m, 2016 acquisition",
    3: "Copernicus GLO-30 30 m (no LiDAR coverage at this pixel)",
    4: "Pack-declared single-raster primary DEM",
    0: "No elevation data",
}

SOURCE_RESOLUTION_M = {1: 1.0, 2: 1.0, 3: 30.0, 4: None, 0: None}

YEAR_RE = re.compile(r"_(\d{4})(?:_dsm)?\.tif$", re.IGNORECASE)


@dataclass
class TerrainModel:
    """An elevation surface on an analysis grid, and where every pixel came from."""

    elevation: np.ma.MaskedArray
    provenance: np.ndarray  # uint8, values from SOURCE_CODES
    grid: AnalysisGrid
    source_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_labels: dict[int, str] = field(default_factory=lambda: dict(SOURCE_LABELS))
    source_resolutions_m: dict[int, float | None] = field(
        default_factory=lambda: dict(SOURCE_RESOLUTION_M)
    )
    lidar_codes: frozenset[int] | None = field(
        default_factory=lambda: frozenset(
            {SOURCE_CODES["lidar_2022"], SOURCE_CODES["lidar_2016"]}
        )
    )

    @property
    def coverage(self) -> dict[str, float]:
        total = float(self.provenance.size)
        if self.lidar_codes is not None:
            named_codes = {
                "lidar_2022": SOURCE_CODES["lidar_2022"],
                "lidar_2016": SOURCE_CODES["lidar_2016"],
                "copernicus_glo30": SOURCE_CODES["fallback_dem"],
                "none": SOURCE_CODES["none"],
            }
        else:
            named_codes = {
                "single_raster_dem": SOURCE_CODES["single_raster_dem"],
                **(
                    {"fallback_dem": SOURCE_CODES["fallback_dem"]}
                    if SOURCE_CODES["fallback_dem"] in self.source_labels
                    else {}
                ),
                "none": SOURCE_CODES["none"],
            }
        return {
            name: round(float((self.provenance == code).sum()) / total, 6)
            for name, code in named_codes.items()
        }

    @property
    def lidar_fraction(self) -> float | None:
        if self.lidar_codes is None:
            return None
        lidar = np.isin(self.provenance, tuple(self.lidar_codes))
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
        raw_resolutions = [self.source_resolutions_m.get(int(code)) for code in valid]
        if any(value is None for value in raw_resolutions):
            return None
        resolutions = np.asarray(raw_resolutions, dtype="float64")
        return round(float(resolutions.mean()), 3)

    def describe(self) -> dict[str, object]:
        return {
            "grid": self.grid.describe(),
            "coverage_by_source": self.coverage,
            "coverage_by_source_label": {
                self.source_labels[code]: round(
                    float((self.provenance == code).sum()) / float(self.provenance.size), 6
                )
                for code in sorted(set(int(value) for value in np.unique(self.provenance)))
            },
            "source_codes": {
                str(code): label for code, label in sorted(self.source_labels.items())
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


def _asset_source_label(asset: PackAsset, *, role: str) -> str:
    """Name a provenance code from the pack instead of assuming one provider."""
    resolution = (
        f"{asset.native_resolution_m:g} m"
        if asset.native_resolution_m is not None
        else "unknown resolution"
    )
    return f"{asset.source.provider}: {asset.source.citation} ({role}, {resolution})"


def _fill_from_fallback(
    settings: Settings,
    pack: MountainPack,
    grid: AnalysisGrid,
    elevation: np.ndarray,
    provenance: np.ndarray,
    warnings: list[str],
    used: list[Path],
) -> tuple[str | None, float | None]:
    fallback = pack.assets.get("elevation_fallback")
    if fallback is None:
        remaining = int((provenance == 0).sum())
        if remaining:
            warnings.append(
                f"{remaining} pixels ({remaining / provenance.size:.2%} of the AOI) are outside "
                "the primary DEM coverage and no fallback DEM is declared; those pixels remain "
                "NoData."
            )
        return None, None
    path = pack.asset_path(settings.data_root, "elevation_fallback")
    remaining = int((provenance == 0).sum())
    if remaining == 0:
        return _asset_source_label(fallback, role="fallback DEM"), fallback.native_resolution_m
    if not path.exists():
        warnings.append(
            f"{remaining} pixels are outside the primary DEM coverage and the declared fallback "
            f"DEM is missing; those pixels remain NoData."
        )
        return _asset_source_label(fallback, role="fallback DEM"), fallback.native_resolution_m
    # Upsampling a coarse source to the analysis grid does not create finer
    # information. It is bilinear interpolation and carries its own provenance
    # code so downstream reporting can retain that distinction.
    coarse = read_aligned(path, grid, Semantics.CONTINUOUS)
    _merge(elevation, provenance, coarse, SOURCE_CODES["fallback_dem"])
    used.append(path)
    filled = remaining - int((provenance == 0).sum())
    if filled:
        source_resolution = (
            f"{fallback.native_resolution_m:g} m"
            if fallback.native_resolution_m is not None
            else "unknown-resolution"
        )
        warnings.append(
            f"{filled} pixels ({filled / provenance.size:.2%} of the AOI) were outside the "
            f"primary DEM coverage and filled from the declared {source_resolution} fallback "
            f"DEM, then interpolated to the "
            f"{grid.resolution_m:g} m grid. Terrain derivatives there are less reliable."
        )
    return _asset_source_label(fallback, role="fallback DEM"), fallback.native_resolution_m


def build_dem(settings: Settings, grid: AnalysisGrid) -> TerrainModel:
    """Best-available bare-earth elevation on ``grid``."""
    elevation = np.full(grid.shape, NODATA, dtype="float32")
    provenance = np.zeros(grid.shape, dtype="uint8")
    warnings: list[str] = []
    used: list[Path] = []

    pack, _ = load_mountain_pack(settings)
    uses_lidar_tiles = "elevation_lidar" in pack.assets
    if uses_lidar_tiles:
        source_labels = {
            SOURCE_CODES["none"]: SOURCE_LABELS[SOURCE_CODES["none"]],
            SOURCE_CODES["lidar_2022"]: SOURCE_LABELS[SOURCE_CODES["lidar_2022"]],
            SOURCE_CODES["lidar_2016"]: SOURCE_LABELS[SOURCE_CODES["lidar_2016"]],
        }
        source_resolutions_m = {
            SOURCE_CODES["none"]: None,
            SOURCE_CODES["lidar_2022"]: SOURCE_RESOLUTION_M[SOURCE_CODES["lidar_2022"]],
            SOURCE_CODES["lidar_2016"]: SOURCE_RESOLUTION_M[SOURCE_CODES["lidar_2016"]],
        }
    else:
        source_labels = {SOURCE_CODES["none"]: SOURCE_LABELS[SOURCE_CODES["none"]]}
        source_resolutions_m = {SOURCE_CODES["none"]: None}

    if uses_lidar_tiles:
        folder = pack.asset_path(settings.data_root, "elevation_lidar")
        _mosaic_lidar(folder, grid, elevation, provenance, warnings, used)
    else:
        primary = pack.assets["elevation_primary"]
        path = pack.asset_path(settings.data_root, "elevation_primary")
        if path.exists():
            raster = read_aligned(path, grid, Semantics.CONTINUOUS)
            before = int((provenance == 0).sum())
            _merge(
                elevation,
                provenance,
                raster,
                SOURCE_CODES["single_raster_dem"],
            )
            if int((provenance == 0).sum()) < before:
                used.append(path)
        source_labels[SOURCE_CODES["single_raster_dem"]] = _asset_source_label(
            primary, role="primary DEM"
        )
        source_resolutions_m[SOURCE_CODES["single_raster_dem"]] = (
            primary.native_resolution_m
        )

    fallback_label, fallback_resolution = _fill_from_fallback(
        settings, pack, grid, elevation, provenance, warnings, used
    )
    if fallback_label is not None:
        source_labels[SOURCE_CODES["fallback_dem"]] = fallback_label
        source_resolutions_m[SOURCE_CODES["fallback_dem"]] = fallback_resolution

    masked = np.ma.array(elevation, mask=(provenance == 0))
    model = TerrainModel(
        elevation=masked,
        provenance=provenance,
        grid=grid,
        source_files=used,
        warnings=warnings,
        source_labels=source_labels,
        source_resolutions_m=source_resolutions_m,
        lidar_codes=(
            frozenset({SOURCE_CODES["lidar_2022"], SOURCE_CODES["lidar_2016"]})
            if uses_lidar_tiles
            else None
        ),
    )

    if uses_lidar_tiles and model.lidar_fraction < 0.5:
        warnings.append(
            f"Only {model.lidar_fraction:.1%} of the AOI is backed by LiDAR. Terrain derivatives "
            "are dominated by the fallback DEM and should be treated as coarse."
        )
    return model


def build_dsm(settings: Settings, grid: AnalysisGrid) -> TerrainModel | None:
    """Best-available surface (first-return) elevation on ``grid``.

    Returns ``None`` when no DSM tiles exist. There is no coarse fallback: a DSM
    has no meaningful 30 m equivalent, and inventing one would corrupt the canopy
    height model that is derived from it.
    """
    pack, _ = load_mountain_pack(settings)
    surface = pack.assets.get("surface_lidar")
    if surface is None:
        return None
    if surface.adapter != "geobc_lidar_year_tiles":
        raise MountainPackError(
            "surface_lidar currently requires adapter='geobc_lidar_year_tiles'."
        )
    folder = pack.asset_path(settings.data_root, "surface_lidar")
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
