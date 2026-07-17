# Repository Map

Every directory in this repository, what it is, and whether you should touch it.

**Legend:** ✅ active · 📦 input data · 🗄️ archived · 🔧 third-party · 🤖 generated

---

## Top level

```
D:\school\capstone\Avalanche\
├── CLAUDE.md                     ✅ AI agent orientation — the entry point
├── README.md                     ✅ Human entry point
├── .gitignore                    ✅ Excludes DATA/, runtime/, deps, build output
├── docs\                         ✅ Repo-level documentation (this file lives here)
├── mount-hosmer-digital-twin\    ✅ THE APPLICATION
├── DATA\                         📦 Read-only source data (271 files, ~46 GB)
├── archive\                      🗄️ Superseded work
└── Tools\                        🔧 Vendored QGIS install
```

> **History:** this tree used to nest the app and data under `APP\`, carried a full **46 GB duplicate** of
> `DATA\` at the top level, and documented its own location as `D:\Avalanche\...` — a path that never
> existed. The duplicate was deleted, the app and data were promoted to the top level, legacy work was moved
> into `archive\`, and every stale path was corrected.

---

## ✅ `mount-hosmer-digital-twin\` — the application

**This is where essentially all work happens.**

> **Stage 3 ("Ultra").** The app was radically simplified to a single-screen twin with four features (3D
> LiDAR mesh, runout simulation, a slider-driven risk model, a local Ollama AI). The runtime is
> rasterio-free and reads no source data — it serves a one-time **bake** under `runtime\baked\`. The tree
> below reflects Stage 3; anything describing five research tabs / jobs / a database is superseded.

```
mount-hosmer-digital-twin\
├── MountHosmerDigitalTwin.exe   Compiled one-click launcher (bakes if needed, then starts both servers)
├── README.md                    App usage: setup, bake, run, endpoints
├── PROGRESS.md                  Stage 3 status + pre-Stage-3 history
├── MILESTONE_PROCESS.md         Milestone checklist / process
├── .env.example                 Copy to .env to override data/runtime roots, Ollama, CORS
├── pyproject.toml               Root shim config + pytest config
├── docker-compose.yml           Container option (bake one-shot + slim runtime; not the primary path)
│
├── app\
│   └── __init__.py              ⚠️ NOT DEAD CODE. A pkgutil shim that extends the `app`
│                                package onto backend\app so `python -m app.bake` and
│                                `uvicorn app.main:app` work from the project root.
│
├── backend\
│   ├── requirements.txt         RUNTIME deps ONLY (fastapi, uvicorn, pydantic, numpy, scipy, shapely, httpx)
│   ├── requirements-bake.txt    Adds the bake-time geospatial stack (rasterio, pyproj, pillow, pyyaml)
│   ├── config\
│   │   └── avalanche_model.yaml   Terrain/runout parameters, read at BAKE time
│   └── app\
│       ├── main.py              ✅ FastAPI app (health + the stage3 router)
│       ├── bake.py              ✅ The one-time offline bake (`python -m app.bake`)
│       ├── baked.py             ✅ numpy loader for baked .npy + grid→WGS84 Reprojector
│       ├── risk.py              ✅ Simplified release model + release-zone extraction
│       ├── assess.py            ✅ sliders → release → zones → runout → one JSON
│       ├── geo.py               ✅ rasterio-free mask→GeoJSON / path→LineString (shapely)
│       ├── assistant.py         ✅ Local Ollama: explain + scenario chat
│       ├── cli.py               ✅ One CLI command: bake
│       ├── __main__.py          Entry point for `python -m app`
│       ├── api\
│       │   ├── stage3.py        ✅ ALL HTTP routes (health/meta/tiles/assess/assistant)
│       │   ├── errors.py        One error envelope + exception→HTTP mapping
│       │   └── middleware.py    Correlation ids, body-size limit, logging
│       ├── core\
│       │   ├── settings.py      Env-driven paths (data root, runtime root)
│       │   ├── paths.py         Path-escape guards (safe_source_path; bake-time)
│       │   └── model_config.py  The DISCLAIMER + bake-time YAML loader
│       ├── simulation\
│       │   ├── runout.py        Fast (alpha) + advanced (particle) runout engines
│       │   └── zone.py          The neutral ReleaseZone value type
│       ├── processing\          ⚠️ BAKE-TIME ONLY (rasterio/pyproj live here)
│       └── services\
│           ├── tiles.py         ⚠️ BAKE-TIME ONLY — terrain-RGB tiling
│           └── cache.py         ⚠️ BAKE-TIME ONLY — SHA-256 input signatures
│
├── frontend\
│   ├── package.json             Next 16, React, MapLibre, Tailwind
│   ├── e2e\twin.spec.ts         Playwright smoke (mesh + assess + disclaimer)
│   └── src\
│       ├── app\                 App Router shell (layout.tsx, page.tsx → Stage3App)
│       ├── lib\twin.ts          ✅ Typed API client + every response type
│       └── components\          ✅ THE ONE SCREEN
│           ├── Stage3App.tsx        The screen (state + layout)
│           ├── Stage3Map.tsx        MapLibre 3D mesh + result overlays
│           ├── ConditionPanel.tsx   Sliders + presets
│           ├── ResultCard.tsx       Hazard index + zones + disclaimer
│           └── AssistantPanel.tsx   The Ollama AI
│
├── launcher\
│   ├── Program.cs               ⚠️ Source of the .exe. Editing this changes NOTHING
│   │                              until rebuilt with the .NET 9 SDK.
│   └── MountHosmerDigitalTwin.Launcher.csproj
│
├── tests\                       ✅ pytest (hermetic synthetic bake; never reads real DATA\)
│   ├── synthetic_baked.py       Writes a tiny synthetic bake (no rasterio)
│   ├── test_risk_assess.py  test_stage3_api.py
│   └── test_geo.py  test_paths.py
│
├── docs\                        ✅ App documentation (see index below)
│
└── runtime\                     🤖 GENERATED — never hand-edit, safe to delete & rebuild
    ├── baked\                   THE SERVED SURFACE: tiles\{z}\{x}\{y}.png, layers\*.npy, meta.json
    ├── cache\                   SHA-256 input-signature sidecars (bake cache gating)
    └── logs\                    launcher-bake / backend / frontend logs
```

### App docs index

| File | Covers |
|---|---|
| `docs\architecture.md` | Stage 3 system design, the bake→baked→serve pipeline, invariants |
| `docs\limitations.md` | **What this cannot do — read before making claims** |
| `../docs/data-footprint.md` | The bake input allow-list; what is archived (not deleted) |
| `docs\backend-reference.md` *(superseded)* | Pre-Stage-3 module map |
| `docs\frontend-reference.md` *(superseded)* | Pre-Stage-3 component map |
| `docs\data-pipeline.md` *(superseded)* | Pre-Stage-3 processing stages |
| `docs\data-dictionary.md` *(superseded)* | Pre-Stage-3 field definitions |
| `docs\susceptibility-model.md` *(superseded)* | The removed susceptibility model |
| `docs\windows-setup.md` | Windows/Rasterio setup notes (still useful for the bake) |

---

## 📦 `DATA\mount_hosmer_data\` — source data

**Read-only. Never write here.** 271 files, ~46 GB. Expensive to re-download; parts are unrecoverable.

> **Stage 3 uses only a ~6.5 GB bake-time allow-list, and nothing at runtime.** After the bake, `DATA\`
> is no longer consumed; the LiDAR `.laz` (38.7 GB) and all of `dynamic\`/`events\` can be **archived, not
> deleted** (invariant I1; the Fernie `2C21P` archive is un-re-downloadable). See
> [`data-footprint.md`](data-footprint.md).

```
DATA\mount_hosmer_data\
├── metadata\        AOI geometry, analysis grid, download manifest   ← bake input
├── static\          Terrain & land cover that does not change over time
│   ├── lidar_bc\        BC LiDAR DEM/DSM tiles ← bake input (the 5 m mesh source, 99.9% coverage)
│   │                    + 171 .laz point clouds (38.7 GB, unused — archive candidate)
│   ├── terrain_fallback\ Copernicus GLO-30 DEM  ← bake input, gap-fill ONLY now
│   ├── landcover\       ESA WorldCover 2021 (10 m)  ← bake input (forest mask)
│   └── openstreetmap\   OSM infrastructure features  (optional bake input)
├── dynamic\         Time-varying conditions  ← NOT USED by Stage 3 (conditions are sliders)
├── events\          Two satellite captures   ← NOT USED by Stage 3 (event viewer removed)
└── logs\            Download logs
```

Full breakdown: [`data-inventory.md`](data-inventory.md). Bake contract: [`data-footprint.md`](data-footprint.md).

---

## 🗄️ `archive\` — superseded, do not build on

| Path | What it is | Why it's here |
|---|---|---|
| `archive\web-app-demo\` | An **earlier prototype** of the same idea — upload a DEM, get slope/risk rasters. Has its own git repo (remote: `swemwave/avalanche_prediction_model`) and its own committed `.venv`. | Superseded by `mount-hosmer-digital-twin`. Kept for reference and because it is the version pushed to GitHub. Its git history is intact. |
| `archive\qgis-project\` | The original QGIS work: `avalanche_digital_twin.qgz`, raw `DEM.tif` / `DSM.tif` / `Point Index.laz`, and derived `slope`/`aspect`/`hillshade` rasters (~4.8 GB). | The manual/desktop precursor to the automated pipeline. The app now computes these derivatives itself from `DATA\`. |

Neither is wired into the app. Do not import from them; do not "fix" them.

---

## 🔧 `Tools\`

A full vendored **QGIS / OSGeo4W** installation (`Tools\QGIS\`, plus a `Tools\QGISFIX\`). Third-party
desktop software, not project source. It was left in place deliberately — installed GIS stacks bake absolute
paths into their config, and moving it risks breaking it. Ignore it unless you are doing manual QGIS work.
