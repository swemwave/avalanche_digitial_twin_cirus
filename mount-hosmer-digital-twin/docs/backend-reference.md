# Backend Reference

A module-by-module map of `backend\app\`. Use this to answer **"where do I make this change?"**

Architecture and rationale: [`architecture.md`](architecture.md).

---

## Layout

```
backend\
├── requirements.txt
├── config\susceptibility_weights.yaml   Model weights (hashed into cache signatures)
└── app\
    ├── main.py          Routes only        — ~35 endpoints
    ├── cli.py           CLI only           — 8 subcommands
    ├── __main__.py      `python -m app`
    ├── core\
    │   ├── settings.py  Env-driven paths
    │   └── paths.py     Path-escape guards
    └── services\        ← ALL BUSINESS LOGIC
        ├── catalog.py        events.py        cache.py
        ├── terrain.py        conditions.py    aoi.py
        └── susceptibility.py                  json_utils.py
```

**The rule:** `main.py` and `cli.py` are thin adapters. They parse arguments, call a service, and translate
exceptions into HTTP codes or exit codes. **They contain no logic.** If you are adding behavior, it goes in
`services\`.

---

## `core\settings.py`

`Settings` — a frozen dataclass built by `get_settings()`, called per-request and per-command.

| Field | Default |
|---|---|
| `project_root` | The `mount-hosmer-digital-twin\` folder (3 parents up from `settings.py`) |
| `backend_root` | `<project>\backend` |
| `data_root` | `$MOUNT_HOSMER_DATA_ROOT`, else **`<project>\..\DATA\mount_hosmer_data`** |
| `runtime_root` | `$MOUNT_HOSMER_RUNTIME_ROOT`, else `<project>\runtime` |
| `app_version` | `"0.1.0"` — participates in every cache signature |

`_load_env_file()` reads a `.env` at the project root via `os.environ.setdefault`, so **real environment
variables win over `.env`**.

> ⚠️ The `data_root` default hardcodes the **sibling** relationship. `launcher\Program.cs` duplicates this
> logic independently. Changing one without the other silently breaks the app. See `architecture.md` §5.

> ⚠️ Bumping `app_version` invalidates **every** cache entry, forcing a full reprocess.

## `core\paths.py`

The path-security primitives. Small and load-bearing.

| Function | Purpose |
|---|---|
| `safe_source_path(data_root, candidate)` | Resolve a path and **raise `UnsafePathError` if it escapes `data_root`**. Every manifest-derived path goes through this. |
| `is_relative_to(path, root)` | Containment check. |
| `relative_source_path(data_root, path)` | Absolute → relative POSIX string (what the API exposes; absolute paths never leave the backend). |
| `ensure_runtime_dirs(runtime_root)` | Create the `runtime\` skeleton. |

Guarded by `tests/test_paths.py`.

---

## `services\catalog.py` (565 LOC)

Discovers, inspects, and checksums the 271 source files.

**Entry points:** `generate_catalog(settings, verify_checksums=True)` → writes
`runtime\catalog\data_catalog.{json,csv}` · `load_catalog(settings)` → reads it back
(raises `FileNotFoundError` if never scanned).

**Type-dispatched inspection** — `inspect_file()` fans out by extension:

| Function | Extracts |
|---|---|
| `inspect_raster` | CRS, bounds, dimensions, resolution, bands, dtypes, NoData, sampled min/max |
| `inspect_vector_geojson` | CRS, bounds, feature count, geometry types |
| `inspect_csv` | Columns, row count, timestamp ranges |
| `inspect_json` / `inspect_yaml` | Structural summary |
| `inspect_point_cloud` | LAS/LAZ header — **only if `laspy[lazrs]` is installed**; otherwise name + size only |

**Supporting:** `load_manifest()` (parses `download_manifest.csv` → `ManifestEntry`, the source of expected
checksums) · `sha256_file()` · `build_file_record()` · `read_download_errors()` (preserves the known
`2C21P` 404) · `discover_event_ids()` · `write_catalog_csv()`.

Sampled raster min/max are **approximate** on large rasters — by design, for speed.

## `services\terrain.py` (1097 LOC — the largest)

DEM → every static terrain product. Roughly four zones:

**1 — Path & ID helpers:** `terrain_paths()` (the single map of source→output locations),
`layer_index_path/metadata_path/preview_path`, `processed_raster_path`, **`validate_layer_id()`** (the
security gate for `/api/layers/{id}`).

**2 — Raster & rendering:** `read_raster`, `normalize`, `ramp`, `rgba_from_hex`, `save_png`,
`raster_coordinates`, `masked_stats`, `hillshade`, `landcover_to_rgba`, `aspect_to_rgba`,
`susceptibility_to_rgba`, `binary_rgba`.

**3 — The DEM decision (read this):**

| Function | What it does |
|---|---|
| `grid_profile(settings, resolution=30.0)` | Builds the canonical 30 m analysis profile — EPSG:26911, NoData `-9999.0`. **Everything is resampled onto this.** |
| `select_latest_lidar_tiles()` | Newest tile per LiDAR cell |
| `mosaic_lidar_to_grid()` | Stitch tiles onto the grid |
| **`choose_dem()`** | **Prefers LiDAR, requires > 95 % coverage. Local data = 61.9 % → always falls back to Copernicus GLO-30**, with a warning. |
| **`build_surface_height()`** | DSM − DEM. Same 95 % rule → **always skipped** on local data. |

**4 — Derivatives & susceptibility:** `terrain_derivatives()` (slope, aspect, curvature, ruggedness) ·
`slope_aspect_from_dem` · `flow_products` · `landcover_masks` (open vs. forested) · `write_contours` ·
**`terrain_susceptibility()`** (the weighted static score) · `score_aspect` · `write_susceptibility_tif` ·
`categorize_osm_feature` / `get_osm_infrastructure`.

**Orchestrator:** `generate_terrain_layers(settings, force=False)` — the whole static pass.
**Readers:** `get_terrain_layers`, `terrain_metadata`, `get_layer_metadata`, `get_layer_preview`,
`get_susceptibility`, `get_contours`, `get_download_asset`.

Weights from `config\susceptibility_weights.yaml` via `load_weights()` — **slope dominates at 0.45.**

## `services\events.py` (855 LOC)

Sentinel-2 + Landsat per event.

**Discovery & validation:** `discover_event_dirs()` (event IDs come from **folder names**, never hardcoded) ·
**`validate_event_id()` / `validate_layer_id()`** (the security gates for every `/api/events/...` route) ·
`discover_event_files()`.

**Imagery:** `composite_rgba` (true/false colour) · **`normalized_difference()`** — the `(A−B)/(A+B)` engine
behind NDVI, NDSI, NDMI · `index_rgba` · `thermal_rgba` (Landsat surface temperature) · `mask_rgba`.

**Quality masks (the important part):** `sentinel_scl_masks()` parses Sentinel-2's Scene Classification Layer
(cloud, shadow, snow, valid) and `landsat_qa_masks()` parses Landsat's QA bit flags.

> **Why this matters:** clouds are bright and white and will be read as fresh snow if not masked. Cloud and
> shadow pixels are **excluded**, not guessed. `aoi_inside_mask` / `valid_pixels` / `masked_from_valid`
> propagate validity into every statistic.

**Orchestrators:** `process_event(settings, event_id, force=False)` · `process_all_events()`.
**Readers:** `list_events`, `get_event`, `get_event_layer_metadata`, `get_event_layer_preview`.

> **Landsat quirk:** the downloaded Landsat NDVI/NDSI/NDMI rasters contain out-of-range values, so this module
> **recomputes them** from scaled reflectance bands and records an event warning. Intentional.

## `services\conditions.py` (870 LOC)

Weather, snowpack, and forecast → normalized tables.

**Weather (ECCC):** `discover_weather_sources` · `normalize_weather_frame()` — maps **both hourly and daily**
ECCC schemas into one common set of fields (air/min/max temp, precipitation, rainfall, snowfall, snow on
ground, wind speed/direction/gust, humidity, pressure, station coords, elevation) · `weather_event_windows()`
(slices around event dates — this is what susceptibility consumes) · `weather_station_summaries` ·
`dominant_wind_direction` · **`process_weather()`** → `weather_normalized.parquet` (~27,527 records).

**Snow (BC):** two stations with **different schemas**:

| Station | Function | Mapping |
|---|---|---|
| `2C09Q` Morrissey Ridge | `normalize_snow_current_frame` / `normalize_snow_archive_frame` | `SW` → **SWE** (mm) |
| `2C21P` Fernie | same | `SD` → **snow depth** (cm) · ⚠️ **archive is HTTP 404, permanently missing** |

`snow_event_windows()` · **`process_snow()`** → `snow_stations_normalized.parquet` (~10,586 records).
The 404 is preserved as a warning — **never backfilled**.

**Avalanche Canada:** `process_avalanche_forecast()` · `parse_danger_ratings()` (by elevation band) ·
`parse_avalanche_problems()` · `strip_html()`.

> ⚠️ This is the **current** forecast — live context for today, **not** a historical label for the 2026 event
> dates. It currently reports off-season, which is **not** an absence of risk.

**Orchestrator:** `process_dynamic()` runs weather + snow + forecast.
**Readers:** `get_weather`, `get_weather_summary`, `get_snow`, `get_snow_summary`, `get_avalanche_forecast`.

Change-rate helpers (`value_change`, `value_sum`, `value_min/max/mean`) exist because **rate of change**, not
absolute value, is what drives avalanche risk.

## `services\susceptibility.py` (551 LOC)

Scores conditions and fuses them with terrain. **This is where invariant I3 lives — read carefully.**

**Scoring:** `make_component()` (builds one scored component, or marks it missing) · `interp_score()`
(piecewise-linear → 0–100) · `best_window` / `max_station_window_value` (worst-case across stations).

**`score_dynamic_conditions()`** — the core. Components and weights (from
`config\susceptibility_weights.yaml`):

| Component | Weight |
|---|---:|
| recent snowfall | 0.18 |
| SWE change | 0.14 |
| rapid warming | 0.14 |
| strong wind | 0.14 |
| recent precipitation | 0.12 |
| satellite snow cover | 0.12 |
| snow-depth change | 0.10 |
| surface temperature | 0.06 |
| *Avalanche Canada forecast* | **non-scored context** (`avalanche_forecast_component`) |

**⚠️ The missing-data rule — `average_available()`:**

Missing components are **removed from the weighted denominator**, not scored as 0. A gap must never be able
to make conditions look safer than they are.

If the available weight drops below **`minimum_available_weight` (0.25)**, the module produces
**no combined index at all** — it declines to answer rather than answering wrongly.

**Fusion:** `write_combined_raster()` —

```
combined = terrain_susceptibility × 0.65  +  dynamic_condition_index × 0.35
```

**Orchestrators:** `process_event_susceptibility()` · `process_all_event_susceptibility()`.
**Readers:** `get_event_susceptibility` + `_metadata` / `_preview` / `_download`.

`config_hash()` puts the weights file's SHA-256 into every output, so any score is traceable to the exact
weights that produced it.

## `services\cache.py` (108 LOC)

Small, and it gates every processor.

| Function | Purpose |
|---|---|
| `source_fingerprint(settings, source_files, config_files, parameters)` | Builds the fingerprint: a `file_record` (path, size, mtime, **SHA-256**) per source and config file, plus parameters and `app_version` → one **`input_signature_sha256`** |
| `cache_matches(existing, expected)` | Compares signatures — the reuse decision |
| `write_cache_log(settings, name, payload)` | Writes `runtime\cache\<name>.json` |
| `file_sha256(path)` | Streaming SHA-256 (1 MB chunks) |
| `stable_json_hash(payload)` | Order-independent hash of a dict |

> ⚠️ **If you add an input to a processor, add it to `source_fingerprint`.** Otherwise the cache will serve
> stale output forever. Invalidation is by **content**, not mtime. Guarded by `tests/test_cache.py`.

## `services\aoi.py` (27) · `services\json_utils.py` (26)

`load_aoi()` → `metadata\mount_hosmer_aoi.geojson` · `load_grid()` → `metadata\grid_and_aoi.json` (the
analysis grid every raster is aligned to). `summarize_json()` → structural summary of arbitrary JSON, used by
the catalog.

---

## `main.py` — the API

FastAPI, CORS restricted to `localhost:3000` / `127.0.0.1:3000`. **No auth** — local only.

Exception mapping is uniform: `KeyError` / `FileNotFoundError` → **404**, anything else → **500**.

| Group | Endpoints |
|---|---|
| Health | `GET /api/health` |
| Catalog | `GET /api/catalog[?compact=true]` · `POST /api/catalog/rescan` |
| AOI | `GET /api/aoi` |
| Terrain | `GET /api/terrain/layers` · `/metadata` · `/osm` · `/contours` · `POST /api/terrain/process` |
| Events | `GET /api/events` · `/{id}` · `/{id}/layers/{layer_id}/metadata` · `/preview` · `POST /{id}/process` · `POST /api/events/process` |
| Weather | `GET /api/weather` · `/summary` · `POST /process` |
| Snow | `GET /api/snow` · `/summary` · `POST /process` |
| Forecast | `GET /api/avalanche-forecast` · `POST /process` |
| Dynamic | `POST /api/dynamic/process` |
| Susceptibility | `GET /api/susceptibility/terrain` · `/events/{id}` · `/metadata` · `/preview` · `/download` · `POST .../process` |
| Layers | `GET /api/layers/{id}/metadata` · `/preview` |
| Download | `GET /api/download/{asset_id}` |

All `POST /process` routes accept `?force=true`. Every path parameter is an **opaque validated ID** — never a
filesystem path.

## `cli.py` — offline commands

```powershell
python -m app.cli scan-data [--skip-checksum]
python -m app.cli inspect-point-clouds
python -m app.cli process-terrain [--force]
python -m app.cli process-events (--all | --event-id <ID>) [--force]
python -m app.cli process-dynamic [--force]          # weather + snow + forecast
python -m app.cli process-weather [--force]          # or individually
python -m app.cli process-snow [--force]
python -m app.cli process-forecast [--force]
python -m app.cli process-susceptibility (--all | --event-id <ID>) [--force]
```

Run from the project root (the `app\` shim makes it resolve) or from `backend\`.
**`process-susceptibility` must run last** — it consumes terrain, event, and dynamic output.

---

## Adding an endpoint — the checklist

1. **`services\<module>.py`** — the logic. Read source via `settings.data_root`; write only under
   `settings.runtime_root`.
2. If it's a processor: register its inputs with **`cache.source_fingerprint()`** and honour `force`.
3. If it takes an ID from the client: **validate it** (`validate_event_id` / `validate_layer_id` /
   `safe_source_path`). Never accept a path.
4. **`main.py`** — a thin route; map `KeyError`/`FileNotFoundError` → 404.
5. **`cli.py`** — a subcommand, if it should be runnable offline.
6. **`frontend\src\lib\api.ts`** — the TypeScript type + fetch call. **Types are compile-time only; a
   mismatch here fails silently at runtime.**
7. **`tests\`** — a test. Use generated fixtures; never read the real 46 GB `DATA\`.
