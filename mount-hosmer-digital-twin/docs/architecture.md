# Architecture (Stage 3)

How the Mount Hosmer digital twin is put together, and why.

**Related:** [`limitations.md`](limitations.md) (what it can't do) ·
[`../../docs/data-footprint.md`](../../docs/data-footprint.md) (the bake input contract) ·
[`../../CLAUDE.md`](../../CLAUDE.md) (orientation + invariants).

> Docs under this folder marked *(superseded)* — `backend-reference`, `frontend-reference`, `data-pipeline`,
> `data-dictionary`, `susceptibility-model` — describe the pre-Stage-3 build. Trust this file and the code.

---

## 1. The shape of the system

One pipeline, strictly one-directional, and a launcher. The key move is that **the geospatial work happens
once, offline, in the bake** — so the running service is a thin, dependency-light server over static files.

```
┌──────────────────────────────────────────────────────────────────────┐
│  DATA\mount_hosmer_data\     ⛔ READ-ONLY. Bake-time input ONLY.       │
│  ~6.5 GB LiDAR DEM/DSM · land cover · terrain fallback · metadata      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  python -m app.bake  (ONCE, offline)
                                │  rasterio + pyproj live HERE and nowhere else
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  runtime\baked\              🤖 GENERATED — the entire served surface  │
│  tiles\{z}\{x}\{y}.png  ·  layers\*.npy (7)  ·  meta.json (+ lattice)  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  loaded with plain numpy — NO rasterio, NO DATA\
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  backend\app\main.py         FastAPI · 127.0.0.1:8000                  │
│  health · twin/meta · twin/tiles · assess · assistant                 │
│  /assess does its numerical work inside the request and returns JSON  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  HTTP/JSON + PNG tiles
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  frontend\src\               Next.js · localhost:3000 · ONE screen     │
│  Stage3App · Stage3Map (MapLibre 3D mesh) · ConditionPanel ·          │
│  ResultCard · AssistantPanel · lib/twin.ts (typed client)             │
└──────────────────────────────────────────────────────────────────────┘

   launcher\  →  MountHosmerDigitalTwin.exe  bakes if needed, then starts both servers
```

---

## 2. Why it's built this way

**The bake is separated from serving** because the geospatial work is heavy (mosaicking 5 m LiDAR onto a
2400×2400 grid, hashing GB of input) and pulls a large dependency stack (rasterio/pyproj/GDAL). Doing it
once, offline, means the running service loads `.npy` with numpy and serves static PNGs — no source data,
no GDAL, a handful of light dependencies. That is the whole point of Stage 3: `import app.main` must not
pull rasterio, pyproj, pandas, geopandas, laspy, sqlalchemy, pillow, matplotlib, or pyyaml.

**Source data is immutable and bake-time only.** It is expensive to fetch and partly **irreplaceable** — the
Fernie `2C21P` archive returns HTTP 404 and cannot be re-downloaded. Only `bake.py` reads it, from the
allow-list in [`../../docs/data-footprint.md`](../../docs/data-footprint.md). Everything generated goes
under `runtime\`, which is always safe to delete and rebuild.

**Conditions are sliders, not feeds.** Stage 3 does no weather/snow/satellite ingestion. The user supplies
new snow, wind speed, wind direction, and release size; the model turns terrain capability × that loading
into a release estimate. This is what let the old `models/`/`jobs/`/`storage/`/`processing/weather/` stack
(and ~35 endpoints across five tabs) collapse to a few hundred lines.

**Missing data is never zero** because this is a safety-adjacent system. See §5.

---

## 3. Backend

FastAPI, ~1k LOC of runtime, plus a bake-time engine that is never imported by the running server.

| Piece | Path | Job |
|---|---|---|
| **App** | `app/main.py` | Builds FastAPI, installs middleware + error handlers, includes the one router, exposes `/api/health`. |
| **Routes** | `app/api/stage3.py` | The entire HTTP surface: meta, tiles, assess, assistant. Thin — parse, call, return. |
| **Bake** | `app/bake.py` | Offline. Reuses `processing/terrain/engine.compute()` → writes `runtime\baked\`. |
| **Baked loader** | `app/baked.py` | numpy loader for `.npy` layers (as masked arrays) + `Reprojector` (grid→WGS84 from the baked lattice, via scipy). |
| **Risk** | `app/risk.py` | One transparent release model + release-zone extraction. Owns the `ReleaseZone`-consuming data. |
| **Assess** | `app/assess.py` | sliders → release raster → zones → runout (top-N) → one JSON. Disclaimer attached here, in code. |
| **Geometry** | `app/geo.py` | rasterio-free mask→GeoJSON (shapely) and path→LineString. |
| **Assistant** | `app/assistant.py` | Local Ollama. `explain` narrates an assessment; `chat` parses to sliders → runs the real `/assess` → narrates. |
| **Simulation** | `app/simulation/{runout,zone}.py` | Runout engines (fast + advanced) + the neutral `ReleaseZone` value type. |
| **Core** | `app/core/` | `settings.py` (env-driven paths), `paths.py` (path-escape guards, bake-time), `model_config.py` (the `DISCLAIMER` + bake-time YAML loader). |
| **Bake-time engine** | `app/processing/*`, `app/services/{tiles,cache}.py` | ⚠️ rasterio/pyproj live here. Imported by `bake.py` only, never by `app.main`. |
| **Config** | `backend/config/avalanche_model.yaml` | Terrain/runout parameters, read at bake time. |

### The `app\` package shim — do not delete

An `app\__init__.py` at the **project root** (not `backend\`) contains only a `pkgutil.extend_path` call
that appends `backend\app` onto the `app` package's `__path__`. That is what makes `python -m app.bake`
and `uvicorn app.main:app` resolve from the project root even though the code lives in `backend\app\`. It
looks like an empty, deletable file. **It is not.** (The same commands also work with `backend\` as the
cwd, where `app` resolves directly — the launcher does that.)

---

## 4. The pipeline

```
DATA\ (allow-list)  ──►  python -m app.bake  ──►  runtime\baked\  ──►  serve
```

`bake.py`:

1. loads the analysis grid + AOI from `metadata\`,
2. runs the tested 5 m terrain engine, which **mosaics the LiDAR DEM/DSM to ~99.9 % AOI coverage**
   (Copernicus GLO-30 as gap-fill only), derives slope/aspect/curvature/forest, and renders terrain-RGB
   tiles,
3. writes 7 `.npy` layers (elevation, slope, aspect, plan_curvature, general_curvature, forest_mask,
   distance_to_ridge) as float32 with NaN in masked cells,
4. writes `meta.json` including a **21×21 grid→WGS84 control lattice** computed with pyproj — the runtime
   interpolates that lattice (scipy) instead of importing pyproj, accurate to <1 cm over the AOI.

At runtime, `/assess` does the whole thing synchronously: `risk.compute_release` → `risk.extract_release_zones`
→ `runout` for the top-scoring zones → `geo` builds all GeoJSON from the numpy masks → one JSON with the
hazard index, zones, runout footprints/uncertainty/paths, warnings, and the disclaimer.

**LiDAR IS used now.** Older docs describe a legacy 30 m pipeline that fell back to Copernicus at 61.9 %
coverage. Stage 3's 5 m engine reaches 99.9 %. The 171 `.laz` point clouds remain unused (the DEM rasters
are derived from them).

---

## 5. Invariants

Correctness and safety properties, not preferences. Full text in [`../../CLAUDE.md`](../../CLAUDE.md) §4.

- **Source data is read-only, and bake-time only.** Nothing writes to `DATA\`; the running service never
  reads it. All output goes under `runtime\`.
- **The app folder and `DATA\` must remain siblings.** `core/settings.py` and `launcher/Program.cs`
  independently resolve `<project_root>\..\DATA\mount_hosmer_data`. This only bites at bake time now, but
  relocating either folder still breaks the bake in two languages; `Program.cs` needs a .NET 9 rebuild.
- **Missing data is missing — never zero, never safe.** Baked layers load via `np.ma.masked_invalid`, so a
  NaN pixel is masked, not 0. And `assess.py` never reports a below-threshold day as zero hazard: it falls
  back to the 95th percentile of the release estimate on avalanche terrain and labels it. Never `fillna(0)`
  a measurement; never let a gap lower a hazard score.
- **The disclaimer is attached in code, on every hazard number, and never generated by the AI.**
- **Bake reuse is content-hashed (ex-I5).** The terrain engine writes SHA-256 input signatures to
  `runtime\cache\` and reuses baked output only when they still match; `python -m app.bake --force`
  rebuilds. The running service has no processors and no cache to invalidate.

---

## 6. Security boundary

The backend binds to `127.0.0.1`; CORS is restricted to `localhost:3000` / `127.0.0.1:3000`. Local-only
research tool, **no authentication** — do not expose it to a network without adding some. Browser input is
now just numbers (slider values) and integer tile coordinates; `twin/tiles` serves a file only if it exists
(404 otherwise), so there is no path-from-browser surface. `core/paths.py::safe_source_path()` still guards
the bake's reads of `DATA\`.

---

## 7. Frontend

Next.js (App Router) + React + MapLibre GL. One screen: `Stage3App` holds the state and composes
`Stage3Map` (the 3D LiDAR mesh draped with baked tiles, plus release-zone / runout overlays),
`ConditionPanel` (sliders + presets), `ResultCard` (hazard index + zones + disclaimer), and
`AssistantPanel` (the Ollama AI). There is no router, no global state library, no server-side fetching.

`src/lib/twin.ts` is the **contract mirror** of `api/stage3.py`: `API_BASE_URL`, a typed fetch per route,
and a TypeScript type for every response. Change a response shape in the backend and change it here, or the
UI breaks silently at runtime (types are compile-time only).

---

## 8. Runtime layout

```
runtime\
├── baked\            THE SERVED SURFACE (built by python -m app.bake)
│   ├── tiles\{z}\{x}\{y}.png   terrain-RGB tiles, z8–15
│   ├── layers\*.npy            7 terrain layers (float32, NaN = masked)
│   └── meta.json               grid/AOI/tiles + the grid→WGS84 lattice
├── cache\            SHA-256 input signatures written by the bake's terrain engine
└── logs\             launcher-bake / backend / frontend logs
```

Generated and gitignored. Delete it and run `python -m app.bake` to rebuild.

---

## 9. Launcher

`MountHosmerDigitalTwin.exe` (C#, .NET 9) is what users double-click. It:

1. finds the project root by searching upward for marker files (`backend\app\main.py` +
   `frontend\package.json`) — so it is relocatable,
2. loads `.env` if present,
3. defaults the data root to `<projectRoot>\..\DATA\mount_hosmer_data`,
4. **runs `python -m app.bake` if `runtime\baked\meta.json` is missing**,
5. starts uvicorn (:8000) and Next.js (:3000), logging to `runtime\logs\`,
6. opens the browser and holds the servers until you close it.

> ⚠️ The `.exe` is **committed and prebuilt**. Editing `launcher\Program.cs` has no effect until you rebuild
> with the .NET 9 SDK (`dotnet publish -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true
> -p:DebugType=None -p:DebugSymbols=false -o .`).
