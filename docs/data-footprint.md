# Data footprint

What the active Digital Twin consumes from `DATA\mount_hosmer_data\`, what it does
not use, and what may be archived off the working disk without destroying anything.

> ⛔ **`DATA\` is read-only (invariant I1).** "No longer consumed by the app" is **not** the same as "safe to
> delete." Some of this data cannot be re-downloaded. The correct action for unused bulk is **archive, not
> delete** — see [Archiving, not deleting](#archiving-not-deleting).

The active application keeps the 3D terrain model, runout simulation, a simplified
release analysis, and a local Ollama assistant. Dynamic satellite events,
weather/snow ingestion, Avalanche Canada context, and the old susceptibility
pipeline are not runtime inputs. One fixed winter Sentinel-2 RGB capture remains
as bake-time-only visual context.

---

## At a glance

| | Entire source holding | Active Digital Twin |
|---|---:|---:|
| Source data the app *reads* | ~46 GB | **~6.5 GB, bake-time only** |
| Source data the app reads *at runtime* | ~46 GB (indirectly, via processors) | **0 GB** (runs off baked artifacts) |
| No longer consumed | — | **~38.9 GB (~84%)** |

The single most important line: **after the one-time bake, a running Stage-3 app needs nothing from `DATA\`.**
The 6.5 GB below is an input to `bake.py` only.

---

## What each dataset is used for under Stage 3

| Source (`DATA\mount_hosmer_data\…`) | Size | Files | Stage 3 role |
|---|---:|---:|---|
| `static\lidar_bc\*.laz` (point clouds) | **38.74 GB** | 171 | ❌ **Not used** — already unused today; the DEM rasters were derived from these, so the app never reads them |
| `static\lidar_bc\*.tif` (LiDAR DEM/DSM) | **6.53 GB** | 20 | ⚠️ **Bake-time only** — source for the 5 m mesh, `.npy` terrain layers, and terrain-RGB tiles |
| `events\` (Sentinel-2 + Landsat scenes) | 92 MB | — | ➖ **One fixed RGB capture only** — three 10 m Sentinel-2 bands are baked as visual context; no values enter risk |
| `dynamic\` (ECCC weather, BC snow, Avalanche Canada) | 48 MB | — | ❌ **Not used** — conditions are explicit user-entered simple/advanced research-scenario records |
| `static\terrain_fallback\` (Copernicus GLO-30) | 1.7 MB | 3 | ⚠️ **Bake-time only** — gap-fill where LiDAR has holes |
| `static\landcover\` (ESA WorldCover 10 m) | 104 KB | 1 | ⚠️ **Bake-time only** — forest mask for risk damping |
| `static\openstreetmap\` (OSM features) | 2.3 MB | 2 | ⚠️ **Bake-time only** — the GeoJSON becomes the exposure layer (roads, rail, derived built-up outlines); the raw Overpass JSON is not read |
| `metadata\` (grid, AOI, manifests) | 306 KB | — | ⚠️ **Bake-time only** — the AOI and acquisition manifest are consumed; legacy grid metadata is preserved |
| `logs\`, `download.log` | ~0 | — | keep (provenance) |

The active 5 m terrain engine mosaics the LiDAR DEM/DSM tiles to approximately
99.9% AOI coverage. Copernicus is gap fill only; the raw `.laz` point clouds are
not read because the DEM/DSM rasters were derived from them.

---

## `bake.py` input contract

`bake.py` runs **once, offline**, and reads from this **explicit allow-list and nothing else.** Every other
path in `DATA\` (all `.laz`, all other event rasters, all of `dynamic\`) is off-limits and must never be
opened by the bake.

**Reads (inputs):**

```
DATA\mount_hosmer_data\metadata\mount_hosmer_aoi.geojson   # AOI polygon
DATA\mount_hosmer_data\static\lidar_bc\*.tif               # LiDAR DEM/DSM  (~6.5 GB, the bulk of bake input)
DATA\mount_hosmer_data\static\terrain_fallback\*.tif       # Copernicus GLO-30 gap-fill
DATA\mount_hosmer_data\static\landcover\ESA_WorldCover_2021_EPSG26911_10m.tif
DATA\mount_hosmer_data\events\MH_20260116T183016Z\sentinel2\*_B02_EPSG26911_10m.tif  # blue
DATA\mount_hosmer_data\events\MH_20260116T183016Z\sentinel2\*_B03_EPSG26911_10m.tif  # green
DATA\mount_hosmer_data\events\MH_20260116T183016Z\sentinel2\*_B04_EPSG26911_10m.tif  # red
DATA\mount_hosmer_data\static\openstreetmap\mount_hosmer_osm_features.geojson   # exposure (ODbL 1.0)
```

The OpenStreetMap extract is **exposure, not a hazard input**: it never enters the release model and acts
only as the consequence term of the composite hazard index. It is © OpenStreetMap contributors under the
Open Database License (ODbL) 1.0, and the attribution is carried through the bake into the served vector
and rendered wherever the layer is visible.

The analysis grid, CRS, asset allow-list, units and source statements come from
`mount-hosmer-digital-twin/backend/config/mount_hosmer.pack.json`. Its SHA-256
identity is bound into every new bake. `metadata/grid_and_aoi.json` remains
preserved source metadata but is no longer interpreted by the bake.

This allow-list describes the default Mount Hosmer pack. A separate mountain uses
its own reviewed pack and read-only data root, selected explicitly with
`python -m app.bake --pack ... --data-root ... --runtime-root ...`. Its generated
surface belongs under a separate runtime root (for example
`runtime\mountains\mountain-id\baked\`), never under either source tree. A portable
plain DEM is declared as `elevation_primary` with `adapter: single_raster`; the
legacy `elevation_lidar` role remains specific to GeoBC year tiles.

**Writes (outputs) — all under `runtime\`, never `DATA\` (invariant I1):**

```
runtime\baked\tiles\{z}\{x}\{y}.png        # terrain-RGB tiles for the 3D MapLibre mesh
runtime\baked\imagery\{z}\{x}\{y}.png      # winter Sentinel-2 natural-colour surface
runtime\baked\exposure\features.geojson    # classified exposure vectors for display (optional)
runtime\baked\layers\*.npy                 # six model layers + terrain/forest source-code rasters
                                           #   + optional exposure_weight / exposure_class
runtime\baked\meta.json                    # lineage, checksums, bake identity, grid/AOI/tile metadata
runtime\reports\terrain\reference-elevations\... # inactive bake-bound elevation contracts
runtime\verification\bake-preservation\...       # complete pre-rebuild inventory/copy
runtime\snow-state-packs\...                      # inactive offline M3 outputs, when eligible
```

High-fidelity engine runs are also offline products, but they are not part of the
serving surface unless a future reviewed bake step explicitly promotes a stable
schema into `runtime/baked/`. The AvaFrame synthetic example writes only to the
operator-supplied output directory. Its isolated environment comes from
`backend/requirements-avaframe.txt`; AvaFrame, rasterio, GDAL/pyproj and external
engine executables are absent from the serving dependency closure. Every engine
input is copied from a hash/size-verified artifact into a disposable work
directory. No engine adapter reads from or writes to `DATA/` implicitly.

The runtime app loads the `.npy` layers with plain **numpy** and serves the static tiles — so `rasterio`,
`pyproj`, and the other geospatial libraries become **bake-time-only dependencies** and drop out of the
running service. This is the mechanism that takes runtime's `DATA\` dependency to zero.

---

## What Stage 3 stops using (~38.9 GB)

- **`static\lidar_bc\*.laz` — 38.74 GB.** The heavyweight. Not read today, not read by Stage 3. The DEM/DSM
  rasters are already derived from these; the point clouds are only needed if someone ever wants to
  re-derive a *finer-than-1 m* surface, which is out of scope.
- **Most of `events\` — ~78 MB.** No satellite viewer or NDSI-based snow presence; only the fixed winter
  B02/B03/B04 rasters are read to build the optional natural-colour surface.
- **`dynamic\` — 48 MB.** No serving-time weather/snow ingestion; condition observations and assumptions
  are explicitly entered by the user with visible provenance and missingness.

---

## `runtime\` also shrinks

`runtime\` is generated and **is** safe to delete/regenerate.

- **Removed:** `processed\events\`, `processed\dynamic\`, legacy `processed\static\` previews, the 271-file
  `catalog\`, the SQLite database, cache sidecars, most of `previews\` and `exports\`.
- **Kept / new:** `baked\` (tiles + `.npy` + meta) and a handful of assessment result JSONs.

---

## Archiving, not deleting

The 38.9 GB is not consumed by the active application, but some sources are
irreplaceable. Reduce the working footprint **without destroying anything**:

- **Move `static\lidar_bc\*.laz` (38.7 GB) to cold storage** — an external drive or archive. Keep them
  *somewhere*; they are the only path back to the raw point cloud. Just not on the working disk.
- **Keep `events\` and `dynamic\` in place** (140 MB — trivial). In particular the Fernie **`2C21P`** snow
  archive is **permanently un-re-downloadable** (HTTP 404, see *Known gaps*); never delete it.
- **After baking, the DEM rasters can also move offline** — they are needed only to re-bake.

Net effect: a running Stage-3 app's working footprint drops from **~46 GB → a few hundred MB** of baked
artifacts, with **zero irreplaceable bytes destroyed.**
