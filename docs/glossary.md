# Glossary

Domain terms used throughout this codebase. If you are an AI agent or a developer without a GIS/avalanche
background, read this before changing anything in `services\terrain.py`, `services\events.py`, or
`services\susceptibility.py` — several of these distinctions are load-bearing.

---

## Terrain

**DEM — Digital Elevation Model**
Height of the **bare ground**, with trees and buildings removed. This is the terrain surface. The app's DEM
is Copernicus GLO-30 at 30 m resolution.

**DSM — Digital Surface Model**
Height of the **top of everything** — ground, tree canopy, buildings. Always ≥ DEM.

**Surface height (DSM − DEM)**
Canopy/structure height. A proxy for forest cover: tall canopy anchors snow and lowers avalanche
susceptibility. **Currently skipped** in this app, because LiDAR DSM coverage over the AOI (61.9 %) falls
below the 95 % threshold required to use it.

**Slope**
Steepness in degrees. **The dominant avalanche factor** — it carries the largest weight (0.45) in the terrain
model. Slab avalanches release most readily on roughly 30–45° slopes: shallower will not slide, steeper
sluffs continuously without accumulating a slab.

**Aspect**
The compass direction a slope faces. Matters because it controls sun exposure (melt-freeze cycles) and
wind loading — lee slopes accumulate wind-drifted slabs.

**Hillshade**
A shaded-relief image computed from the DEM by simulating a light source. Purely a **visualization**
backdrop; it carries no analytic meaning.

**Curvature**
Whether the surface is convex (a ridge/roll) or concave (a bowl/gully). Convex rolls are common slab release
points; concave gullies are terrain traps that channel and deepen debris.

**Ruggedness**
Local terrain roughness. Rough, broken terrain anchors snow; smooth terrain lets it slide.

**Contours**
Lines of constant elevation, generated to `runtime\processed\static\contours.geojson`.

**Point cloud (`.laz`)**
Raw LiDAR returns — millions of individual 3-D points, before being gridded into a DEM/DSM. The 171 `.laz`
files are the bulk of the 46 GB of source data and are **cataloged but not used** by the pipeline.

---

## Geospatial

**AOI — Area of Interest**
The fixed study boundary. Here: a 12 × 12 km box over Mount Hosmer, BC.

**CRS — Coordinate Reference System**
How coordinates map to locations on Earth.

- **EPSG:26911** — UTM zone 11N. The **analysis CRS**: metre-based, so distances and slopes are computable.
- **EPSG:4326 / WGS84** — familiar lat/long degrees. Used for display and web maps.

**Analysis grid**
The fixed raster lattice every layer is resampled onto so pixels line up. 30 m → 400 × 400;
10 m → 1200 × 1200. Defined in `DATA\mount_hosmer_data\metadata\grid_and_aoi.json`.

**Raster**
Grid-of-pixels data (a `.tif` / GeoTIFF). Elevation, slope, satellite bands.

**Vector**
Points/lines/polygons data (a `.geojson`). The AOI boundary, OSM roads and infrastructure.

**Resampling / mosaicking**
Resampling = re-gridding a raster to a different resolution or alignment. Mosaicking = stitching adjacent
tiles into one continuous raster. `terrain.py` mosaics LiDAR tiles onto the AOI grid — and rejects the
result if coverage is under 95 %.

**NoData**
Pixels with no valid measurement (`-9999.0` here). **Critically distinct from zero.** A NoData elevation
means "unknown", not "sea level". Masked arrays (`numpy.ma`) are used throughout to keep this distinction.

---

## Satellite

**Sentinel-2** — ESA optical satellite, 10 m resolution, frequent revisits. The primary imagery source.

**Landsat** — NASA/USGS satellite, 30 m, but carries a **thermal band** → surface temperature, which
Sentinel-2 lacks.

**Band** — one wavelength range captured by the sensor (red, green, blue, near-infrared, shortwave-infrared,
thermal…). Indices are ratios of bands.

**Spectral indices** — all follow the *normalized difference* form `(A − B) / (A + B)`, giving −1…+1:

| Index | Formula basis | Detects | Used for |
|---|---|---|---|
| **NDSI** — Snow Index | green vs. shortwave-IR | **Snow** (snow is bright in green, dark in SWIR — this is how snow is told apart from cloud) | Snow cover → susceptibility |
| **NDMI** — Moisture Index | near-IR vs. shortwave-IR | Moisture/wetness | Wet-snow signal |
| **NDVI** — Vegetation Index | near-IR vs. red | Live vegetation | Forest context |

**Surface temperature (LST)** — from Landsat's thermal band. Near/above 0 °C signals melt and wet-snow
instability.

**SCL — Scene Classification Layer** — Sentinel-2's per-pixel class map (cloud, shadow, snow, vegetation,
water…). Parsed by `events.py::sentinel_scl_masks()`.

**QA band / quality mask** — Landsat's equivalent bit-flag band. Parsed by `landsat_qa_masks()`.

**Cloud / cloud-shadow / valid-data masks**
Which pixels are trustworthy. **This matters enormously**: a cloud is bright and white and will masquerade
as fresh snow if you do not mask it. Cloud-obscured pixels are excluded, not guessed.

---

## Snow & weather

**SWE — Snow Water Equivalent**
The depth of water you would get by melting the snowpack. **The most meaningful snowpack measure**, because
snow density varies hugely — 30 cm of dense wet snow and 100 cm of dry powder can hold the same water.
Reported in mm. Station `2C09Q` (Morrissey Ridge) measures it as `SW`.

**Snow depth**
Simple physical depth of snow on the ground (cm). Station `2C21P` (Fernie) measures it as `SD`.

**SWE change / depth change**
The *rate of change* is what drives avalanche risk — rapid loading (a big storm) stresses the snowpack
faster than it can stabilize. Scored as dynamic components.

**Rapid warming**
A fast temperature rise weakens bonds in the snowpack and can trigger wet-slab release. A scored component.

**Wind loading**
Wind moves snow from windward slopes onto lee slopes, building dense, cohesive **wind slabs** — a classic
avalanche problem. `strong_wind` is a scored component.

**ECCC** — Environment and Climate Change Canada. The weather-station data source.

**Avalanche Canada** — the national avalanche forecasting agency. Provides **danger ratings** by elevation
band (alpine / treeline / below treeline) on a 1–5 scale, plus **avalanche problems** (wind slab, persistent
slab, loose wet…).

> ⚠️ In this app, the Avalanche Canada feed is the **current** forecast — live context for today, **not** a
> historical record of what happened on the 2026 event dates. It is deliberately carried as a *non-scored*
> contextual component. Off-season/summer status must never be read as "no risk".

---

## This codebase

**Digital twin** — a data-driven virtual model of a physical place, assembled from many sources.

**Event** — one satellite capture date over the AOI, e.g. `MH_20260430T182949Z`. Discovered from folder
names, never hardcoded.

**Static vs. dynamic**
*Static* = terrain and land cover; doesn't change between events. *Dynamic* = weather, snowpack, forecast;
changes constantly. The susceptibility model combines them: **65 % terrain, 35 % dynamic**.

**Susceptibility (as used here)**
A 0–100 **experimental, rules-based** score of how prone terrain is to avalanche, given conditions. It is
**not a probability, and not a forecast.** See `docs\susceptibility-model.md` and `docs\limitations.md`.

**Terrain susceptibility** — the static component (slope-dominated). One raster for the whole AOI.
**Dynamic condition index** — the conditions component, scored per event from weather/snow/satellite inputs.
**Combined index** — `terrain × 0.65 + dynamic × 0.35`, per event.

**Available-weight denominator**
The heart of invariant I3. When a dynamic input is missing, its weight is **removed from the denominator**
rather than the component being scored 0 — so missing data cannot silently drag a risk score down and make
conditions look safer than they are. If too little input is available, no combined raster is produced at all.

**Cache sidecar / input signature**
A JSON file in `runtime\cache\` holding SHA-256 hashes of a processor's sources, config, and parameters,
reduced to one `input_signature_sha256`. Cached output is reused only if that signature still matches —
invalidation is by **content**, not timestamp.

**Runtime** — `mount-hosmer-digital-twin\runtime\`. Everything generated. Safe to delete; rebuildable from
`DATA\` with the `process-*` CLI commands.
