"""A miniature Mount Hosmer, built from scratch in a temp directory.

The real source data is 46 GB, read-only, and partly irreplaceable (the Fernie
2C21P archive cannot be re-downloaded). A test suite that reads it is slow, is not
portable, and -- worst -- would let a bug in a processor touch data we cannot get
back. So the end-to-end tests build their own mountain instead.

What is synthesized is a 1.2 x 1.2 km AOI rather than the real 12 x 12 km one, which
at the 5 m terrain grid is 240 x 240 px instead of 2400 x 2400 -- a hundredth of the
pixels, and seconds instead of 94 s. The AOI extent is read from
``metadata/grid_and_aoi.json``, so shrinking it needs no production code change and
no special "test mode": the engine runs exactly the code path it runs in production.

The terrain is a cone with a flat apron: a peak at 2,100 m falling at 40 degrees to a
valley floor at 1,600 m. That is deliberate, not decorative --

  * 40 degrees puts the flanks inside the 30-50 degree band where slabs release, so
    release zones actually form and there is something to simulate;
  * the flat apron gives the avalanche somewhere to stop, which is the whole point of
    a runout model and is precisely what the old broken Voellmy engine could not do
    (its particles froze at the slope break, still carrying 25 m/s).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

CRS = "EPSG:26911"

#: A 1.2 km box anchored at the real AOI's south-west corner, so coordinates stay
#: plausible for the CRS even though the area is tiny.
EXTENT = (637650.0, 5491570.0, 638850.0, 5492770.0)

PEAK_ELEVATION_M = 2100.0
VALLEY_FLOOR_M = 1600.0
FLANK_ANGLE_DEG = 40.0

LIDAR_NODATA = -32767.0


def _cone(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """A cone peaking at the centre of the AOI, flattening onto a valley floor."""
    west, south, east, north = EXTENT
    centre_x = (west + east) / 2.0
    centre_y = (south + north) / 2.0
    radius = np.hypot(xs - centre_x, ys - centre_y)
    drop_per_m = np.tan(np.deg2rad(FLANK_ANGLE_DEG))
    return np.maximum(PEAK_ELEVATION_M - radius * drop_per_m, VALLEY_FLOOR_M)


def _coords(resolution: float) -> tuple[np.ndarray, np.ndarray]:
    """Pixel-centre coordinates for a raster covering EXTENT at ``resolution``."""
    west, south, east, north = EXTENT
    width = int(round((east - west) / resolution))
    height = int(round((north - south) / resolution))
    # Pixel centres, not edges: a half-pixel offset here would shift the whole
    # mountain and silently bias every slope.
    x = west + (np.arange(width) + 0.5) * resolution
    y = north - (np.arange(height) + 0.5) * resolution
    return np.meshgrid(x, y)


def _write(path: Path, data: np.ndarray, resolution: float, nodata: float, dtype: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    west, _, _, north = EXTENT
    height, width = data.shape
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype=dtype,
        crs=CRS,
        transform=from_origin(west, north, resolution, resolution),
        nodata=nodata,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
    ) as dst:
        dst.write(data.astype(dtype), 1)


def build_data_root(root: Path) -> Path:
    """Write a complete, minimal source-data tree and return its path."""
    root.mkdir(parents=True, exist_ok=True)

    west, south, east, north = EXTENT

    # --- metadata: this is what shrinks the AOI -------------------------------
    (root / "metadata").mkdir(parents=True, exist_ok=True)
    (root / "metadata" / "grid_and_aoi.json").write_text(
        json.dumps({"fixed_extent_analysis_crs": list(EXTENT), "analysis_crs": CRS}, indent=2),
        encoding="utf-8",
    )
    (root / "metadata" / "mount_hosmer_aoi.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "Synthetic AOI"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [west, south],
                                    [east, south],
                                    [east, north],
                                    [west, north],
                                    [west, south],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # --- LiDAR DEM, 1 m ------------------------------------------------------
    # The filename MUST carry the acquisition year: mosaic.py's YEAR_RE parses it
    # out to decide provenance and merge order, and a tile it cannot date is skipped
    # entirely. `_2022` maps to the newest-acquisition tier.
    xs, ys = _coords(1.0)
    dem = _cone(xs, ys).astype("float32")

    # Punch a nodata hole in the 2022 tile and have the 2016 tile cover it. This is
    # the real dataset's defining property in miniature: the two acquisitions have
    # COMPLEMENTARY gaps, and merging across years -- not just across mapsheets -- is
    # what took AOI coverage from 62% to 99.93%. If a future change ever regresses to
    # picking one year per mapsheet, the synthetic terrain grows a hole and the
    # end-to-end test notices.
    dem_2022 = dem.copy()
    dem_2022[200:400, 200:400] = LIDAR_NODATA

    dem_2016 = dem.copy()
    dem_2016[600:900, 600:900] = LIDAR_NODATA  # a gap somewhere else entirely

    lidar_dem_dir = root / "static" / "lidar_bc" / "downloads" / "LiDAR_DEM_Index_1_20_000"
    _write(lidar_dem_dir / "synthetic_mapsheet_2022.tif", dem_2022, 1.0, LIDAR_NODATA, "float32")
    _write(lidar_dem_dir / "synthetic_mapsheet_2016.tif", dem_2016, 1.0, LIDAR_NODATA, "float32")

    # --- LiDAR DSM, 1 m: bare earth plus a canopy on the forested lower slopes --
    canopy = np.where(dem < 1800.0, 18.0, 0.0)
    dsm = (dem + canopy).astype("float32")
    lidar_dsm_dir = root / "static" / "lidar_bc" / "downloads" / "LiDAR_DSM_Index_1_20_000"
    _write(lidar_dsm_dir / "synthetic_mapsheet_2022.tif", dsm, 1.0, LIDAR_NODATA, "float32")

    # --- Copernicus GLO-30 fallback, 30 m -------------------------------------
    xs30, ys30 = _coords(30.0)
    _write(
        root / "static" / "terrain_fallback" / "Copernicus_DEM_GLO30_EPSG26911_30m.tif",
        _cone(xs30, ys30).astype("float32"),
        30.0,
        -9999.0,
        "float32",
    )

    # --- ESA WorldCover, 10 m -------------------------------------------------
    # Real WorldCover class codes: 10 tree cover, 30 grassland, 60 bare/sparse.
    xs10, ys10 = _coords(10.0)
    elevation10 = _cone(xs10, ys10)
    landcover = np.full(elevation10.shape, 30, dtype="uint8")
    landcover[elevation10 < 1800.0] = 10   # forest on the lower slopes and apron
    landcover[elevation10 > 2000.0] = 60   # bare rock near the summit
    _write(
        root / "static" / "landcover" / "ESA_WorldCover_2021_EPSG26911_10m.tif",
        landcover,
        10.0,
        0,
        "uint8",
    )

    # --- OpenStreetMap infrastructure ----------------------------------------
    (root / "static" / "openstreetmap").mkdir(parents=True, exist_ok=True)
    (root / "static" / "openstreetmap" / "mount_hosmer_osm_features.geojson").write_text(
        json.dumps(osm_features()), encoding="utf-8"
    )

    return root


def osm_features() -> dict:
    """A road across the apron and a building beside it, in WGS84.

    The exposure engine transforms these from EPSG:4326 to the analysis CRS, so they
    are written in lon/lat exactly as OSM would supply them -- the transform is part
    of what is under test.
    """
    import pyproj

    to_wgs84 = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    west, south, east, north = EXTENT
    centre_y = (south + north) / 2.0

    # An east-west road 500 m south of the peak: out on the flat apron, where an
    # avalanche off the south flank would reach it.
    road = [
        to_wgs84.transform(west + 50.0, centre_y - 500.0),
        to_wgs84.transform(east - 50.0, centre_y - 500.0),
    ]
    building = to_wgs84.transform((west + east) / 2.0 + 30.0, centre_y - 500.0)

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "way/1",
                "properties": {"highway": "secondary", "name": "Synthetic Valley Road"},
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in road]},
            },
            {
                "type": "Feature",
                "id": "way/2",
                "properties": {"building": "yes", "name": "Synthetic Cabin"},
                "geometry": {"type": "Point", "coordinates": list(building)},
            },
        ],
    }
