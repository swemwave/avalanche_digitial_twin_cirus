"""Select the 1999 holdout mountain blocks without opening an avalanche outline.

The selection rule is fixed here in code and executed before any prediction
exists. It reads exactly four things:

1. a **fixed lattice** of non-overlapping 20.1 km LV95 cores anchored at a
   round-number origin, so tile placement cannot be nudged toward anything;
2. the **five previously scored cores** from the 2018/2019 SPOT experiment,
   which are excluded outright -- that terrain has already been used for
   development and cannot serve as an untouched holdout;
3. the campaign's **aerial-image acquisition footprint** and its cloud mask,
   which record where the camera looked, not where avalanches are; and
4. the **Copernicus DEM**, for a terrain criterion that has nothing to do with
   any observation.

The avalanche-outline shapefile is never opened by this script. Using the
acquisition footprint is a deliberate, disclosed choice and it is the *stricter*
one: terrain that was never photographed is not merely unlabelled, it is
unobserved, and leaving it in the eligible-terrain denominator would shrink the
model's flagged fraction and make the frozen area budget easier to pass.

Run:

    python scripts/validation/select_regime_holdout_blocks.py \
        --coverage-shapefile <area_images_1999_all.shp> \
        --cloud-shapefile <Clouds_1999.shp> \
        --dem-directory <copernicus-dem-30m> \
        --output <blocks.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Non-overlapping lattice of 20.1 km cores. The 20,100 m side is 670 cells at
#: 30 m, matching the previously frozen experiment's core geometry exactly so
#: the two campaigns remain directly comparable.
CORE_SIDE_M = 20_100.0
LATTICE_ORIGIN_EASTING_M = 2_540_000.0
LATTICE_ORIGIN_NORTHING_M = 1_080_000.0
LATTICE_COLUMNS = 16
LATTICE_ROWS = 8
#: Terrain halo that lets core-originating runout continue without meeting an
#: artificial edge. Identical to the previous experiment.
HALO_M = 5_100.0
RESOLUTION_M = 30.0

#: Cores already used and already scored. Reusing them would make a "holdout"
#: out of terrain whose results have been seen.
PREVIOUSLY_SCORED_CORES: tuple[tuple[float, float, float, float], ...] = (
    (2_600_000.0, 1_140_000.0, 2_620_100.0, 1_160_100.0),  # 2018 Western Bernese
    (2_680_000.0, 1_158_000.0, 2_700_100.0, 1_178_100.0),  # 2019 Gotthard
    (2_700_000.0, 1_190_000.0, 2_720_100.0, 1_210_100.0),  # 2019 Glarus
    (2_760_000.0, 1_158_000.0, 2_780_100.0, 1_178_100.0),  # 2019 Albula
    (2_800_000.0, 1_180_000.0, 2_820_100.0, 1_200_100.0),  # 2019 Silvretta
)

MINIMUM_COVERAGE_FRACTION = 0.60
MINIMUM_AVALANCHE_TERRAIN_FRACTION = 0.15
MINIMUM_TERRAIN_INPUT_COVERAGE_FRACTION = 0.999
AVALANCHE_TERRAIN_MIN_SLOPE_DEG = 28.0
AVALANCHE_TERRAIN_MAX_SLOPE_DEG = 55.0
AVALANCHE_TERRAIN_MIN_ELEVATION_M = 1_500.0
# Non-overlap is the scientific requirement.  Adjacent fixed tiles are permitted
# because they remain separately scored resampling blocks; they are not described
# as geographically independent mountains.
MINIMUM_BLOCK_SEPARATION_M = CORE_SIDE_M
BLOCK_COUNT = 5

#: Human-readable massif names, assigned from the block centroid after
#: selection. Labels only; they play no part in the rule.
MASSIF_LABELS: tuple[tuple[float, float, str], ...] = (
    (2_560_000.0, 1_140_000.0, "Wildhorn and western Bernese Alps"),
    (2_580_000.0, 1_100_000.0, "Val de Bagnes and Grand Combin"),
    (2_600_000.0, 1_100_000.0, "Grand Combin and Val d'Herens"),
    (2_600_000.0, 1_160_000.0, "Jungfrau and Lauterbrunnen"),
    (2_620_000.0, 1_100_000.0, "Val d'Herens and Mattertal"),
    (2_620_000.0, 1_120_000.0, "Lotschental and upper Valais"),
    (2_640_000.0, 1_160_000.0, "Grimsel and Haslital"),
    (2_720_000.0, 1_180_000.0, "Vorderrhein and Surselva"),
    (2_740_000.0, 1_180_000.0, "Surselva east"),
    (2_760_000.0, 1_180_000.0, "Vals and Safiental"),
    (2_780_000.0, 1_200_000.0, "Prattigau and Rhaetian Alps"),
    (2_800_000.0, 1_200_000.0, "Silvretta north"),
    (2_820_000.0, 1_180_000.0, "Lower Engadine"),
)


@dataclass(frozen=True)
class CandidateBlock:
    lattice_column: int
    lattice_row: int
    core_west_m: float
    core_south_m: float
    core_east_m: float
    core_north_m: float
    acquisition_coverage_fraction: float
    avalanche_terrain_fraction: float
    terrain_input_coverage_fraction: float
    mean_elevation_m: float | None
    rejected_reason: str | None
    selected: bool

    @property
    def centre(self) -> tuple[float, float]:
        return (
            (self.core_west_m + self.core_east_m) / 2.0,
            (self.core_south_m + self.core_north_m) / 2.0,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _shapefile_family(path: Path) -> list[dict[str, Any]]:
    """Identity every sidecar that the vector reader can consult implicitly."""

    return [_file_record(member) for member in sorted(path.parent.glob(f"{path.stem}.*"))]


def _dem_tile_ids(west: float, south: float, east: float, north: float) -> list[str]:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)
    eastings = np.linspace(west, east, 9)
    northings = np.linspace(south, north, 9)
    grid_east, grid_north = np.meshgrid(eastings, northings)
    longitude, latitude = transformer.transform(grid_east, grid_north)
    tiles = set()
    for lon, lat in zip(longitude.reshape(-1), latitude.reshape(-1)):
        tiles.add(
            f"Copernicus_DSM_COG_10_N{int(math.floor(lat)):02d}_00_"
            f"E{int(math.floor(lon)):03d}_00_DEM"
        )
    return sorted(tiles)


def _terrain_statistics(
    west: float,
    south: float,
    east: float,
    north: float,
    dem_directory: Path,
) -> tuple[float, float, float, list[str]]:
    """Fraction of the core that is avalanche terrain, from the DEM alone."""

    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import Resampling, reproject

    import sys

    backend = REPOSITORY_ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from app.processing.terrain import derivatives as terrain_derivatives

    tile_ids = _dem_tile_ids(west, south, east, north)
    width = int(round((east - west) / RESOLUTION_M))
    height = int(round((north - south) / RESOLUTION_M))
    transform = from_origin(west, north, RESOLUTION_M, RESOLUTION_M)
    destination = np.full((height, width), np.nan, dtype="float32")
    for tile in tile_ids:
        path = dem_directory / f"{tile}.tif"
        if not path.is_file():
            continue
        temporary = np.full((height, width), np.nan, dtype="float32")
        with rasterio.open(path) as source:
            reproject(
                source=rasterio.band(source, 1),
                destination=temporary,
                src_transform=source.transform,
                src_crs=source.crs,
                src_nodata=source.nodata,
                dst_transform=transform,
                dst_crs="EPSG:2056",
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
                num_threads=1,
            )
        fill = ~np.isfinite(destination) & np.isfinite(temporary)
        destination[fill] = temporary[fill]
    elevation = np.ma.array(destination, mask=~np.isfinite(destination))

    class _Grid:
        resolution_m = RESOLUTION_M
        shape = (height, width)

    slope, _aspect = terrain_derivatives.slope_aspect(elevation, _Grid())
    elevation_valid = ~np.ma.getmaskarray(elevation)
    valid = ~np.ma.getmaskarray(slope) & elevation_valid
    if not valid.any():
        return 0.0, 0.0, float("nan"), tile_ids
    slope_values = np.asarray(slope.filled(0.0))
    elevation_values = np.asarray(elevation.filled(0.0))
    avalanche = (
        valid
        & (slope_values >= AVALANCHE_TERRAIN_MIN_SLOPE_DEG)
        & (slope_values <= AVALANCHE_TERRAIN_MAX_SLOPE_DEG)
        & (elevation_values >= AVALANCHE_TERRAIN_MIN_ELEVATION_M)
    )
    return (
        float(np.count_nonzero(avalanche) / np.count_nonzero(valid)),
        float(np.count_nonzero(elevation_valid) / elevation_valid.size),
        float(elevation_values[valid].mean()),
        tile_ids,
    )


def _massif_label(centre_east: float, centre_north: float) -> str:
    best = min(
        MASSIF_LABELS,
        key=lambda item: math.hypot(centre_east - item[0], centre_north - item[1]),
    )
    return best[2]


def select_blocks(
    *,
    coverage_shapefile: Path,
    cloud_shapefile: Path,
    dem_directory: Path,
) -> dict[str, Any]:
    import geopandas as gpd
    from shapely import union_all
    from shapely.geometry import box

    coverage_frame = gpd.read_file(coverage_shapefile)
    cloud_frame = gpd.read_file(cloud_shapefile)
    for name, frame in (("coverage", coverage_frame), ("cloud", cloud_frame)):
        if str(frame.crs).upper() != "EPSG:2056":
            raise ValueError(f"The {name} layer must be EPSG:2056, not {frame.crs}.")
    footprint = union_all(coverage_frame.geometry.values).difference(
        union_all(cloud_frame.geometry.values)
    )
    previously_scored = [box(*bounds) for bounds in PREVIOUSLY_SCORED_CORES]

    candidates: list[CandidateBlock] = []
    dem_ids_consulted: set[str] = set()
    for column in range(LATTICE_COLUMNS):
        for row in range(LATTICE_ROWS):
            west = LATTICE_ORIGIN_EASTING_M + column * CORE_SIDE_M
            south = LATTICE_ORIGIN_NORTHING_M + row * CORE_SIDE_M
            east, north = west + CORE_SIDE_M, south + CORE_SIDE_M
            core = box(west, south, east, north)
            coverage_fraction = core.intersection(footprint).area / core.area
            reason: str | None = None
            if any(core.intersects(prior) for prior in previously_scored):
                reason = "overlaps a previously scored 2018/2019 core"
            elif coverage_fraction < MINIMUM_COVERAGE_FRACTION:
                reason = (
                    f"acquisition coverage {coverage_fraction:.3f} below "
                    f"{MINIMUM_COVERAGE_FRACTION}"
                )
            terrain_fraction, terrain_coverage, mean_elevation = 0.0, 0.0, None
            if reason is None:
                (
                    terrain_fraction,
                    terrain_coverage,
                    mean_elevation,
                    tile_ids,
                ) = _terrain_statistics(west, south, east, north, dem_directory)
                dem_ids_consulted.update(tile_ids)
                if terrain_coverage < MINIMUM_TERRAIN_INPUT_COVERAGE_FRACTION:
                    reason = (
                        f"terrain input coverage {terrain_coverage:.6f} below "
                        f"{MINIMUM_TERRAIN_INPUT_COVERAGE_FRACTION}"
                    )
                elif terrain_fraction < MINIMUM_AVALANCHE_TERRAIN_FRACTION:
                    reason = (
                        f"avalanche-terrain fraction {terrain_fraction:.3f} below "
                        f"{MINIMUM_AVALANCHE_TERRAIN_FRACTION}"
                    )
            candidates.append(
                CandidateBlock(
                    lattice_column=column,
                    lattice_row=row,
                    core_west_m=west,
                    core_south_m=south,
                    core_east_m=east,
                    core_north_m=north,
                    acquisition_coverage_fraction=coverage_fraction,
                    avalanche_terrain_fraction=terrain_fraction,
                    terrain_input_coverage_fraction=terrain_coverage,
                    mean_elevation_m=mean_elevation,
                    rejected_reason=reason,
                    selected=False,
                )
            )

    eligible = [item for item in candidates if item.rejected_reason is None]
    eligible.sort(
        key=lambda item: (
            -item.acquisition_coverage_fraction,
            item.core_west_m,
            item.core_south_m,
        )
    )
    selected: list[CandidateBlock] = []
    for candidate in eligible:
        if len(selected) >= BLOCK_COUNT:
            break
        east_c, north_c = candidate.centre
        if all(
            math.hypot(east_c - other.centre[0], north_c - other.centre[1])
            >= MINIMUM_BLOCK_SEPARATION_M
            for other in selected
        ):
            selected.append(candidate)

    if len(selected) != BLOCK_COUNT:
        raise RuntimeError(
            f"The frozen selection rule requested {BLOCK_COUNT} blocks but produced "
            f"{len(selected)}. Do not silently freeze a smaller holdout."
        )

    blocks = []
    for candidate in selected:
        west, south = candidate.core_west_m, candidate.core_south_m
        east, north = candidate.core_east_m, candidate.core_north_m
        simulation = (west - HALO_M, south - HALO_M, east + HALO_M, north + HALO_M)
        centre_east, centre_north = candidate.centre
        window = int(round(HALO_M / RESOLUTION_M))
        blocks.append(
            {
                "block_id": f"holdout_1999_c{candidate.lattice_column:02d}"
                f"r{candidate.lattice_row:02d}",
                "mountain_group": _massif_label(centre_east, centre_north),
                "lattice_column": candidate.lattice_column,
                "lattice_row": candidate.lattice_row,
                "core_grid": {
                    "crs": "EPSG:2056",
                    "bounds": [west, south, east, north],
                    "resolution_m": RESOLUTION_M,
                    "shape": [
                        int(round(CORE_SIDE_M / RESOLUTION_M)),
                        int(round(CORE_SIDE_M / RESOLUTION_M)),
                    ],
                },
                "simulation_grid": {
                    "crs": "EPSG:2056",
                    "bounds": list(simulation),
                    "resolution_m": RESOLUTION_M,
                    "shape": [
                        int(round((CORE_SIDE_M + 2 * HALO_M) / RESOLUTION_M)),
                        int(round((CORE_SIDE_M + 2 * HALO_M) / RESOLUTION_M)),
                    ],
                },
                "core_window_rowcol": [
                    window,
                    window + int(round(CORE_SIDE_M / RESOLUTION_M)),
                    window,
                    window + int(round(CORE_SIDE_M / RESOLUTION_M)),
                ],
                "dem_source_ids": _dem_tile_ids(*simulation),
                "acquisition_coverage_fraction": candidate.acquisition_coverage_fraction,
                "avalanche_terrain_fraction": candidate.avalanche_terrain_fraction,
                "terrain_input_coverage_fraction": (
                    candidate.terrain_input_coverage_fraction
                ),
                "mean_elevation_m": candidate.mean_elevation_m,
            }
        )

    payload = {
        "schema": "avycore-regime-holdout-block-selection-v1",
        "selection_used_avalanche_outlines": False,
        "selection_inputs": [
            "fixed LV95 lattice",
            "previously scored 2018/2019 cores",
            "aerial-image acquisition footprint",
            "cloud mask",
            "Copernicus DEM GLO-30",
        ],
        "acquisition_footprint_rationale": (
            "The footprint records where the camera looked, not where avalanches are. "
            "Restricting the evaluated domain to it is the stricter choice: unphotographed "
            "terrain is unobserved, and leaving it in the eligible-terrain denominator "
            "would shrink the model's flagged fraction and make the frozen area budget "
            "easier to pass."
        ),
        "rule": {
            "core_side_m": CORE_SIDE_M,
            "lattice_origin_m": [LATTICE_ORIGIN_EASTING_M, LATTICE_ORIGIN_NORTHING_M],
            "lattice_shape": [LATTICE_COLUMNS, LATTICE_ROWS],
            "halo_m": HALO_M,
            "resolution_m": RESOLUTION_M,
            "previously_scored_cores": [list(item) for item in PREVIOUSLY_SCORED_CORES],
            "minimum_coverage_fraction": MINIMUM_COVERAGE_FRACTION,
            "minimum_avalanche_terrain_fraction": MINIMUM_AVALANCHE_TERRAIN_FRACTION,
            "minimum_terrain_input_coverage_fraction": (
                MINIMUM_TERRAIN_INPUT_COVERAGE_FRACTION
            ),
            "avalanche_terrain_definition": (
                f"slope in [{AVALANCHE_TERRAIN_MIN_SLOPE_DEG}, "
                f"{AVALANCHE_TERRAIN_MAX_SLOPE_DEG}] degrees and elevation at or above "
                f"{AVALANCHE_TERRAIN_MIN_ELEVATION_M} m"
            ),
            "ordering": "coverage fraction descending, then easting, then northing",
            "minimum_block_separation_m": MINIMUM_BLOCK_SEPARATION_M,
            "block_count": BLOCK_COUNT,
        },
        "source_files": {
            "coverage_shapefile_family": _shapefile_family(coverage_shapefile),
            "cloud_shapefile_family": _shapefile_family(cloud_shapefile),
            "dem_tiles_consulted": [
                _file_record(dem_directory / f"{tile_id}.tif")
                for tile_id in sorted(dem_ids_consulted)
            ],
            "selection_script": _file_record(Path(__file__).resolve()),
        },
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "candidates": [asdict(item) for item in candidates if item.rejected_reason is None
                       or item.acquisition_coverage_fraction > 0.0],
        "blocks": blocks,
    }
    payload["selection_identity_sha256"] = _canonical_sha256(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-shapefile", type=Path, required=True)
    parser.add_argument("--cloud-shapefile", type=Path, required=True)
    parser.add_argument("--dem-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = select_blocks(
        coverage_shapefile=args.coverage_shapefile.resolve(),
        cloud_shapefile=args.cloud_shapefile.resolve(),
        dem_directory=args.dem_directory.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for block in payload["blocks"]:
        print(
            json.dumps(
                {
                    "block_id": block["block_id"],
                    "mountain_group": block["mountain_group"],
                    "core_bounds": block["core_grid"]["bounds"],
                    "acquisition_coverage_fraction": round(
                        block["acquisition_coverage_fraction"], 4
                    ),
                    "avalanche_terrain_fraction": round(
                        block["avalanche_terrain_fraction"], 4
                    ),
                    "terrain_input_coverage_fraction": round(
                        block["terrain_input_coverage_fraction"], 6
                    ),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
