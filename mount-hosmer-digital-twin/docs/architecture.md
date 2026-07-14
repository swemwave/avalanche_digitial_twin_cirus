# Architecture

How the Mount Hosmer digital twin is put together, and why.

**Related:** [`backend-reference.md`](backend-reference.md) (module-by-module) ·
[`frontend-reference.md`](frontend-reference.md) (components) ·
[`data-pipeline.md`](data-pipeline.md) (stage-by-stage) · [`limitations.md`](limitations.md) (what it can't do)

---

## 1. The shape of the system

Three tiers plus a launcher:

```
┌──────────────────────────────────────────────────────────────────────┐
│  DATA\mount_hosmer_data\            ⛔ READ-ONLY, 271 files, ~46 GB   │
│  metadata · static · dynamic · events                                 │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  read only, never written
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  backend\app\services\              PROCESSING                        │
│                                                                       │
│    catalog ──► terrain ──┐                                            │
│                events ───┼──► susceptibility                          │
│            conditions ───┘                                            │
│                                                                       │
│  every processor: hash inputs (SHA-256) → compare to cache sidecar    │
│                   → reuse or rebuild → write output + new sidecar     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  writes
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  runtime\                           🤖 GENERATED, disposable          │
│  catalog\ · cache\ · processed\ · previews\ · exports\ · logs\        │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  reads
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  backend\app\main.py                FastAPI · 127.0.0.1:8000          │
│  ~35 routes · CORS locked to localhost:3000 · IDs in, files out       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  HTTP/JSON + PNG + GeoTIFF
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  frontend\src\                      Next.js · 127.0.0.1:3000          │
│  5 views · MapLibre (maps) · Recharts (charts)                        │
└──────────────────────────────────────────────────────────────────────┘

   launcher\  →  MountHosmerDigitalTwin.exe  wires up env + starts both servers
```

**The flow is strictly one-directional: source → processed → served.** The API never computes from source
data on the fly; it serves what the processors already wrote. The frontend never touches the filesystem.

---

## 2. Why it's built this way

**Processing is separated from serving** because the work is heavy (mosaicking rasters, hashing 46 GB) and
must not happen inside an HTTP request. Processors run offline via the CLI and write to `runtime\`; the API
reads those artifacts. The `POST /api/*/process` endpoints exist to *trigger* a processor, not to compute
inline.

**Source data is immutable** because it is ~46 GB, slow to fetch, and partly **irreplaceable** — the Fernie
`2C21P` archive returns HTTP 404 and cannot be re-downloaded. Treating `DATA\` as read-only means the whole
`runtime\` tree can be deleted and rebuilt with no risk.

**Caching is content-addressed, not timestamp-based**, because file mtimes lie (a copy or a re-download
changes mtime without changing content, and vice versa). Hashing inputs gives deterministic, reproducible
invalidation — which matters for a research artifact where you must be able to say exactly which inputs
produced a given output.

**Missing data is never zero** because this is a safety-adjacent system. See §5.

---

## 3. Backend

FastAPI. ~4,800 LOC. Layered:

| Layer | Path | Rule |
|---|---|---|
| **Routes** | `backend\app\main.py` | Thin. Parse args, call a service, map exceptions to HTTP codes. **No business logic.** |
| **CLI** | `backend\app\cli.py` | Thin. Same services, offline. Every processor is reachable both ways. |
| **Services** | `backend\app\services\` | **All logic lives here.** Pure-ish functions taking `Settings`. |
| **Core** | `backend\app\core\` | `settings.py` (env-driven paths) · `paths.py` (path-escape guards) |
| **Config** | `backend\config\susceptibility_weights.yaml` | Model weights, hashed into the cache signature |

Services and their one-line job:

| Module | LOC | Job |
|---|---:|---|
| `catalog.py` | 565 | Walk, inspect, and checksum the 271 source files → `data_catalog.json` |
| `terrain.py` | 1097 | DEM → slope, aspect, hillshade, curvature, land cover, OSM, terrain susceptibility |
| `events.py` | 855 | Sentinel-2 + Landsat → composites, spectral indices, quality masks, previews |
| `conditions.py` | 870 | ECCC weather + BC snow + Avalanche Canada → normalized Parquet + summaries |
| `susceptibility.py` | 551 | Score dynamic conditions, combine with terrain → per-event raster |
| `cache.py` | 108 | SHA-256 fingerprints, cache sidecars, hit/miss decisions |
| `aoi.py` | 27 | Load AOI polygon + analysis grid |
| `json_utils.py` | 26 | Structural summaries of arbitrary JSON |

### The `app\` package shim — do not delete

There is an `app\__init__.py` at the **project root** (not in `backend\`) containing only a
`pkgutil.extend_path` call that appends `backend\app` to the `app` package's `__path__`.

That is what makes this work from the project root:

```powershell
python -m app.cli process-terrain
python -m uvicorn app.main:app --reload --port 8000
```

…even though the actual code lives in `backend\app\`. It looks like an empty, deletable file. **It is not.**
Removing it breaks every CLI command, the uvicorn entry point, and the launcher.

(The same commands also work with `backend\` as the working directory, where `app` resolves directly with no
shim. The launcher does exactly that — it runs Python with `cwd = backend\`.)

---

## 4. The processing pipeline

Five stages. **Order matters** — susceptibility consumes the output of the three before it.

```
scan-data ──► process-terrain ──┐
              process-events ───┼──► process-susceptibility
              process-dynamic ──┘
```

| Stage | Reads | Writes |
|---|---|---|
| `scan-data` | all of `DATA\` + `download_manifest.csv` | `runtime\catalog\data_catalog.{json,csv}`, warnings |
| `process-terrain` | DEM, land cover, OSM | `runtime\processed\static\*.tif`, `contours.geojson`, `terrain_susceptibility.tif`, `runtime\previews\layers\*.png` |
| `process-events` | `events\<id>\sentinel2\|landsat\` | `runtime\processed\events\<id>\event_summary.json`, `runtime\previews\events\<id>\*.png` |
| `process-dynamic` | ECCC, BC snow, Avalanche Canada | `runtime\processed\dynamic\*.parquet`, `*_summary.json`, `avalanche_forecast.json` |
| `process-susceptibility` | terrain + event + dynamic outputs, weights YAML | `runtime\processed\events\<id>\susceptibility_summary.json`, `combined_susceptibility.tif` + `.png` |

### Terrain: the DEM choice (a real gotcha)

`terrain.py::choose_dem()` **prefers BC LiDAR**: it selects the latest tiles, mosaics them onto the 30 m AOI
grid, and measures coverage. It accepts LiDAR only at **> 95 %** coverage.

Local LiDAR reaches **61.9 %** → **it always falls back to the Copernicus GLO-30 DEM**, and records a warning.
`build_surface_height()` applies the same 95 % rule to the DSM, so **surface height is always skipped**.

This is correct, intended behavior. Do not "fix" it by lowering the threshold — a mosaic with 38 % holes
would produce a terrain model full of NoData gaps. Fixing it properly means downloading the missing tiles.

Consequence: the **171 `.laz` point clouds — the bulk of the 46 GB — are cataloged but never used** by the
pipeline.

---

## 5. Invariants

These are correctness and safety properties, not preferences. They are enforced by tests.

### Source data is read-only
Nothing writes to `DATA\`. All output goes under `runtime\`, which is therefore always safe to delete.

### The app folder and `DATA\` must remain siblings
`core/settings.py` and `launcher/Program.cs` **independently** resolve the data root as
`<project_root>\..\DATA\mount_hosmer_data`:

```python
# settings.py
default_data_root = project.parent / "DATA" / "mount_hosmer_data"
```
```csharp
// Program.cs
Path.GetFullPath(Path.Combine(projectRoot, "..", "DATA", "mount_hosmer_data"))
```

Relocating either folder breaks both, in two languages, silently. Override with `MOUNT_HOSMER_DATA_ROOT`
(env or `.env`) rather than moving folders. Note that changing `Program.cs` requires a **.NET 9 rebuild** —
the committed `.exe` will not pick it up.

### Missing data is missing — never zero, never safe
The core safety property. A missing snowfall reading is **"unknown"**, not "no snowfall".

Mechanically: each dynamic component is scored 0–100 only where data exists. Missing components are
**excluded from the weighted denominator** (`average_available` in `susceptibility.py`) rather than
contributing a 0, and are surfaced as warnings in the UI. If available weight falls below
`minimum_available_weight` (0.25), **no combined index is produced at all** — the app declines to answer
rather than answering wrongly.

NoData in rasters (`-9999.0`) is carried through `numpy.ma` masked arrays for the same reason.

**Never** `fillna(0)` a measurement. **Never** let a data gap lower a risk score.

### Browser input never becomes a filesystem path
Event IDs and layer IDs from the client are validated against discovered folders and known layer records
(`validate_event_id`, `validate_layer_id`) before any file is opened. `core/paths.py::safe_source_path()`
resolves and rejects anything escaping the data root (`UnsafePathError`). The API accepts **catalog IDs
only** — never raw paths. Guarded by `tests/test_api_security.py` and `tests/test_paths.py`.

### Cache reuse is gated on a content hash
`cache.py::source_fingerprint()` records, for each processor: every source file (path, size, mtime, SHA-256),
every config file, and the processing parameters — reduced to one `input_signature_sha256`. Output is reused
only when the stored signature still matches (`cache_matches()`). Sidecars live in `runtime\cache\`.

**If you add an input to a processor, add it to the fingerprint** — otherwise the cache will happily serve
stale output. `--force` rebuilds unconditionally. Guarded by `tests/test_cache.py`.

---

## 6. Security boundary

The backend binds to `127.0.0.1` and CORS is restricted to `localhost:3000` / `127.0.0.1:3000`. This is a
local-only research tool; there is **no authentication**, so do not expose it to a network without adding
some.

The trust boundary is the API surface. Everything crossing it from the browser is an **opaque ID**, validated
against a whitelist derived from the filesystem — never a path. Files leave via `FileResponse` only after the
ID resolves to a known, in-bounds artifact.

---

## 7. Frontend

Next.js (App Router) + React 19. `DigitalTwinApp.tsx` is a shell holding one `useState` view switch across
five client components; there is no router, no global state library, and no server-side data fetching — each
view fetches from the API on mount.

- **Maps:** MapLibre GL — terrain layers and susceptibility rasters served as PNG overlays.
- **Charts:** Recharts — weather and snowpack time series.
- **API client:** `src/lib/api.ts` — one `fetchJson<T>()` plus a TypeScript type for every response.

`api.ts` is the **contract mirror** of `main.py`. Change a response shape in the backend and you must change
it here, or the UI breaks silently at runtime (types are compile-time only).

---

## 8. Runtime layout

```
runtime\
├── catalog\      data_catalog.json/.csv, catalog_warnings.json, point_cloud_inventory.json
├── cache\        <processor>.json — SHA-256 input signatures
├── processed\
│   ├── static\   Terrain GeoTIFFs, contours.geojson, terrain_susceptibility.tif
│   ├── events\<event_id>\   event_summary.json, combined_susceptibility.tif + .metadata.json
│   └── dynamic\  weather_normalized.parquet, snow_stations_normalized.parquet,
│                 weather_summary.json, snow_summary.json, avalanche_forecast.json
├── previews\
│   ├── layers\           Terrain PNG overlays
│   └── events\<id>\      Event PNG overlays
├── exports\      User-facing downloads
└── logs\         backend.{out,err}.log, frontend.{out,err}.log, visual-qa-*
```

All of it is generated and gitignored. Delete it and run the `process-*` commands to rebuild.

---

## 9. Launcher

`MountHosmerDigitalTwin.exe` (C#, .NET 9) is what users actually double-click. It:

1. finds the project root by **searching upward for marker files** (`backend\app\main.py` +
   `frontend\package.json`) — so it is relocatable,
2. loads `.env` if present,
3. defaults `MOUNT_HOSMER_DATA_ROOT` to `<projectRoot>\..\DATA\mount_hosmer_data`,
4. runs a fast catalog scan if `runtime\catalog\data_catalog.json` is missing,
5. starts uvicorn (:8000) and Next.js (:3000), logging to `runtime\logs\`,
6. opens the browser and holds the servers until you close it.

> ⚠️ The `.exe` is **committed and prebuilt**. Editing `launcher\Program.cs` has no effect until you rebuild
> it with the .NET 9 SDK.
