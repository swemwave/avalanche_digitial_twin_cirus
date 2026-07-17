# Data Footprint (Stage 3 — Ultra)

What the **Stage 3 "Ultra"** simplification actually consumes from `DATA\mount_hosmer_data\`, what it stops
using, and — importantly — **what may be moved off the working disk without destroying anything.**

> ⛔ **`DATA\` is read-only (invariant I1).** "No longer consumed by the app" is **not** the same as "safe to
> delete." Some of this data cannot be re-downloaded. The correct action for unused bulk is **archive, not
> delete** — see [Archiving, not deleting](#archiving-not-deleting).

Companion to [`data-inventory.md`](data-inventory.md), which describes the full tree and the *current* app.
This doc describes the **target** footprint once Stage 3 lands. Stage 3 keeps only: the 3D terrain model, the
runout simulation, a simplified risk analysis, and a local (Ollama) AI. Everything that fed the five research
tabs — satellite events, weather/snow ingestion, the Avalanche Canada panel, susceptibility — is removed.

---

## At a glance

| | Today | Stage 3 target |
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
| `events\` (Sentinel-2 + Landsat scenes) | 92 MB | — | ❌ **Not used** — event viewer + NDSI snow observation are cut |
| `dynamic\` (ECCC weather, BC snow, Avalanche Canada) | 48 MB | — | ❌ **Not used** — conditions now come from UI sliders/presets |
| `static\terrain_fallback\` (Copernicus GLO-30) | 1.7 MB | 3 | ⚠️ **Bake-time only** — gap-fill where LiDAR has holes |
| `static\landcover\` (ESA WorldCover 10 m) | 104 KB | 1 | ⚠️ **Bake-time only** — forest mask for risk damping |
| `static\openstreetmap\` (OSM features) | 2.3 MB | 2 | ➖ **Optional** — only if runout keeps exposure/consequence |
| `metadata\` (grid, AOI, manifests) | 306 KB | — | ⚠️ **Bake-time only** — `grid_and_aoi.json`, `mount_hosmer_aoi.geojson` |
| `logs\`, `download.log` | ~0 | — | keep (provenance) |

> **LiDAR-usage note.** [`data-inventory.md`](data-inventory.md) says LiDAR is *not* used (61.9 % coverage,
> 30 m Copernicus fallback). That describes the **legacy 30 m** pipeline. Stage 3 adopts the newer **5 m
> terrain engine**, which mosaics the LiDAR DEM/DSM tiles to ~99.9 % AOI coverage — so under Stage 3 the
> 6.5 GB of `lidar_bc\*.tif` **is** the terrain source, and Copernicus becomes gap-fill only.

---

## `bake.py` input contract

`bake.py` runs **once, offline**, and reads from this **explicit allow-list and nothing else.** Every other
path in `DATA\` (all `.laz`, all of `events\`, all of `dynamic\`) is off-limits and must never be opened by
the bake.

**Reads (inputs):**

```
DATA\mount_hosmer_data\metadata\grid_and_aoi.json          # analysis grid + CRS + bounds
DATA\mount_hosmer_data\metadata\mount_hosmer_aoi.geojson   # AOI polygon
DATA\mount_hosmer_data\static\lidar_bc\*.tif               # LiDAR DEM/DSM  (~6.5 GB, the bulk of bake input)
DATA\mount_hosmer_data\static\terrain_fallback\*.tif       # Copernicus GLO-30 gap-fill
DATA\mount_hosmer_data\static\landcover\ESA_WorldCover_2021_EPSG26911_10m.tif
DATA\mount_hosmer_data\static\openstreetmap\mount_hosmer_osm_features.geojson   # only if exposure is kept
```

**Writes (outputs) — all under `runtime\`, never `DATA\` (invariant I1):**

```
runtime\baked\tiles\{z}\{x}\{y}.png        # terrain-RGB tiles for the 3D MapLibre mesh
runtime\baked\layers\*.npy                 # slope, aspect, curvature, elevation, forest_mask
runtime\baked\meta.json                    # grid/AOI/tile metadata the app serves
```

The runtime app loads the `.npy` layers with plain **numpy** and serves the static tiles — so `rasterio`,
`pyproj`, and the other geospatial libraries become **bake-time-only dependencies** and drop out of the
running service. This is the mechanism that takes runtime's `DATA\` dependency to zero.

---

## What Stage 3 stops using (~38.9 GB)

- **`static\lidar_bc\*.laz` — 38.74 GB.** The heavyweight. Not read today, not read by Stage 3. The DEM/DSM
  rasters are already derived from these; the point clouds are only needed if someone ever wants to
  re-derive a *finer-than-1 m* surface, which is out of scope.
- **`events\` — 92 MB.** No satellite viewer, no NDSI-based snow presence.
- **`dynamic\` — 48 MB.** No weather/snow ingestion; conditions are user-supplied.

---

## `runtime\` also shrinks

`runtime\` is generated and **is** safe to delete/regenerate.

- **Removed:** `processed\events\`, `processed\dynamic\`, legacy `processed\static\` previews, the 271-file
  `catalog\`, the SQLite database, cache sidecars, most of `previews\` and `exports\`.
- **Kept / new:** `baked\` (tiles + `.npy` + meta) and a handful of assessment result JSONs.

---

## Archiving, not deleting

The 38.9 GB is dead weight for Stage 3, but invariant I1 and the *Known gaps* in
[`data-inventory.md`](data-inventory.md) still bind. Reduce footprint **without destroying anything**:

- **Move `static\lidar_bc\*.laz` (38.7 GB) to cold storage** — an external drive or archive. Keep them
  *somewhere*; they are the only path back to the raw point cloud. Just not on the working disk.
- **Keep `events\` and `dynamic\` in place** (140 MB — trivial). In particular the Fernie **`2C21P`** snow
  archive is **permanently un-re-downloadable** (HTTP 404, see *Known gaps*); never delete it.
- **After baking, the DEM rasters can also move offline** — they are needed only to re-bake.

Net effect: a running Stage-3 app's working footprint drops from **~46 GB → a few hundred MB** of baked
artifacts, with **zero irreplaceable bytes destroyed.**
