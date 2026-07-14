# Data Inventory

What is in `DATA\mount_hosmer_data\`, where it came from, and — importantly — **which parts the app
actually uses**.

> ⛔ **Read-only.** Source data is never modified, renamed, moved, or overwritten. All generated output goes
> to `mount-hosmer-digital-twin\runtime\`. Some of this data cannot be re-downloaded (see *Known gaps*).

---

## At a glance

| | |
|---|---|
| Files | **271** |
| Size | **~46 GB** |
| Date range | 2025-11-01 → 2026-05-31 |
| Analysis CRS | EPSG:26911 (UTM 11N) |
| Catalog | `runtime\catalog\data_catalog.json` (regenerate: `python -m app.cli scan-data`) |

| Extension | Count | Notes |
|---|---:|---|
| `.laz` | 171 | LiDAR point clouds — **the bulk of the 46 GB, and not used by the pipeline** |
| `.tif` | 62 | Rasters: LiDAR DEM/DSM tiles, Copernicus fallback, land cover |
| `.json` | 20 | Metadata, Avalanche Canada forecast, OSM, LiDAR index metadata |
| `.csv` | 10 | ECCC weather, BC snow stations, manifests |
| `.geojson` | 5 | AOI, OSM features, weather station locations |
| `.yaml` / `.txt` / `.log` | 3 | Download config, readme, download log |

---

## `metadata\` — the keys to everything

| File | What it is |
|---|---|
| `mount_hosmer_aoi.geojson` | The AOI polygon. Served by `GET /api/aoi`. |
| `grid_and_aoi.json` | **The analysis grid definition.** CRS, bounds, and the 10 m / 30 m grid shapes. Every raster in the pipeline is resampled onto this grid. |
| `download_manifest.csv` / `.json` | What was downloaded, with SHA-256 checksums — the catalog verifies against these. |
| `event_pairs.csv` / `.json` | The satellite event pairings. |
| `config_used.yaml` | The download configuration that produced this tree. |

The grid (from `grid_and_aoi.json`):

```json
{
  "analysis_crs": "EPSG:26911",
  "aoi_bbox_wgs84": [-115.09653134, 49.55821487, -114.92612743, 49.66893841],
  "fixed_extent_analysis_crs": [637650.0, 5491570.0, 649650.0, 5503570.0],
  "grid_30m": { "width": 400,  "height": 400  },
  "grid_10m": { "width": 1200, "height": 1200 },
  "date_range": { "start": "2025-11-01", "end": "2026-05-31" }
}
```

A 12 × 12 km box over Mount Hosmer. Terrain processing runs on the **30 m** grid.

---

## `static\` — terrain and land cover

### `terrain_fallback\` — ✅ what the app actually uses

| File | Role |
|---|---|
| `Copernicus_DEM_GLO30_EPSG26911_30m.tif` | **The elevation source in practice.** |
| `Copernicus_DEM_Slope_Degrees_EPSG26911_30m.tif` | Slope |
| `Copernicus_DEM_Aspect_Degrees_EPSG26911_30m.tif` | Aspect |

Despite the name "fallback", this is the live DEM — see below.

### `lidar_bc\` — 📉 cataloged but *not* used

BC LiDAR DEM and DSM raster tiles, plus **171 `.laz` point-cloud files** and index shapefiles/metadata.

**Why it isn't used:** `terrain.py::choose_dem()` prefers BC LiDAR and mosaics the tiles onto the 30 m AOI
grid — but it requires **> 95 %** grid coverage to accept them. The local tiles only reach **61.9 %**, so the
processor falls back to the Copernicus DEM and records a warning. `build_surface_height()` applies the same
95 % rule to the DSM, so **surface height (DSM − DEM) is skipped entirely**.

This is expected behavior, not a bug. To actually use LiDAR you would need to download the missing tiles
covering the rest of the AOI. The `.laz` point clouds are only inventoried
(`python -m app.cli inspect-point-clouds`, needs `laspy[lazrs]`) — nothing in the terrain pipeline reads them.

> **They are ~most of the 46 GB.** If disk becomes a problem, this is the data that is costing you the most
> and doing the least.

### `landcover\`

`ESA_WorldCover_2021_EPSG26911_10m.tif` — 10 m global land cover, resampled to the 30 m grid and reduced to
open vs. forested masks. Forested terrain is scored as less susceptible.

### `openstreetmap\`

`mount_hosmer_osm_features.geojson` + the raw Overpass response. Categorized by
`terrain.py::categorize_osm_feature()` into infrastructure classes and served by `GET /api/terrain/osm`.

---

## `dynamic\` — time-varying conditions

### `weather_eccc\`

Environment and Climate Change Canada station data, **hourly and daily**, 2025-11-01 → 2026-05-31, plus a
GeoJSON of station locations. Normalized by `conditions.py` into a common schema (air temp, min/max temp,
precipitation, rainfall, snowfall, snow on ground, wind speed/direction/gust, humidity, pressure, station
coordinates and elevation) and written to `runtime\processed\dynamic\weather_normalized.parquet`.

Current local processing yields **~27,527 weather records**.

### `snow_bc\` — two stations, two different schemas

| Station | Name | Key measurement | Note |
|---|---|---|---|
| `2C09Q` | Morrissey Ridge | `SW` → **SWE** (mm) | Current + historical archive both available |
| `2C21P` | Fernie | `SD` → **snow depth** (cm) | ⚠️ **Archive is permanently missing** |

`PC` → precipitation (mm) where present. Yields **~10,586 snow records**.

### `avalanche_canada\`

The **current** Avalanche Canada point forecast: danger ratings by elevation band, avalanche problems,
highlights, validity window, and source metadata.

> ⚠️ **This is live context, not history.** It describes conditions *today*, not conditions on the 2026 event
> dates. It must never be used as a historical avalanche label. It currently reports **summer / off-season**,
> which must never be interpreted as "no avalanche risk". `susceptibility.py` deliberately carries it as a
> **non-scored contextual component**.

---

## `events\` — the two satellite captures

| Event ID | Date |
|---|---|
| `MH_20260116T183016Z` | 2026-01-16 |
| `MH_20260430T182949Z` | 2026-04-30 |

Each contains `event_metadata.json` plus `sentinel2\` and `landsat\` subfolders. Event IDs are **discovered
from folder names**, never hardcoded — but these two are all that exist.

Sentinel-2 products generated: true colour, false-colour vegetation, snow/moisture false colour, NDVI, NDSI,
NDMI, cloud mask, cloud-shadow mask, valid-data mask, snow-class mask.

Landsat products generated: true colour, false colour, surface temperature, NDVI, NDSI, NDMI, cloud mask,
valid-data mask, snow-quality mask.

> **Landsat index quirk:** the downloaded Landsat NDVI/NDSI/NDMI rasters contain out-of-range values, so
> `events.py` **recomputes them** from the scaled reflectance bands and records a warning on the event. This
> is intentional.

---

## Known gaps

These are real, documented, and must be preserved as warnings rather than "fixed" by filling in values:

- **`bc_snow:2C21P:archive` → HTTP 404.** The Fernie historical snow archive is unavailable and could not be
  downloaded. It surfaces as a dashboard warning. It is **not** backfilled with zeros.
- **LiDAR coverage 61.9 %** (< the 95 % threshold) → Copernicus DEM used, surface height skipped.
- **Landsat source indices out of range** → recomputed from reflectance bands.
- **Avalanche Canada is off-season** → shown as context, never as an absence of risk.

The governing rule (see `CLAUDE.md` §3, invariant I3): **missing data is reported as missing, never as zero,
never as safe.** Missing components are excluded from the susceptibility denominator, not scored as 0.
