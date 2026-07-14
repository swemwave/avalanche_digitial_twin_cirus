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

```
mount-hosmer-digital-twin\
├── MountHosmerDigitalTwin.exe   Compiled one-click launcher (what users double-click)
├── README.md                    App usage: setup, commands, endpoints
├── PROGRESS.md                  Milestone status log
├── MILESTONE_PROCESS.md         Milestone checklist / process
├── .env.example                 Copy to .env to override data/runtime roots
├── pyproject.toml               Root shim config
├── docker-compose.yml           Container option (not the primary path)
│
├── app\
│   └── __init__.py              ⚠️ NOT DEAD CODE. A pkgutil shim that extends the `app`
│                                package onto backend\app so `python -m app.cli` and
│                                `uvicorn app.main:app` work from the project root.
│
├── backend\
│   ├── requirements.txt         Python deps (FastAPI, rasterio, geopandas, pandas…)
│   ├── config\
│   │   └── susceptibility_weights.yaml   Model weights (hashed into the cache signature)
│   └── app\
│       ├── main.py              ✅ ALL ~35 HTTP routes
│       ├── cli.py               ✅ ALL CLI subcommands
│       ├── __main__.py          Entry point for `python -m app`
│       ├── core\
│       │   ├── settings.py      Env-driven paths (data root, runtime root)
│       │   └── paths.py         Path-escape guards (safe_source_path)
│       └── services\            ✅ ALL PROCESSING + BUSINESS LOGIC
│           ├── catalog.py       (565) Scan/inspect/checksum the 271 source files
│           ├── terrain.py       (1097) DEM → slope/aspect/hillshade/susceptibility
│           ├── events.py        (855) Sentinel-2 + Landsat event processing
│           ├── conditions.py    (870) Weather, snow, Avalanche Canada forecast
│           ├── susceptibility.py (551) Dynamic scoring + combined raster
│           ├── cache.py         (108) SHA-256 fingerprints, cache sidecars
│           ├── aoi.py           (27) Load AOI + grid metadata
│           └── json_utils.py    (26) JSON structure summaries
│
├── frontend\
│   ├── package.json             Next 16, React 19, MapLibre 5, Recharts 3, Tailwind 4
│   ├── scripts\                 browser-smoke.mjs, visual-qa.mjs (Playwright)
│   └── src\
│       ├── app\                 App Router shell (layout.tsx, page.tsx)
│       ├── lib\api.ts           ✅ Typed API client + every response type
│       └── components\          ✅ THE 5 VIEWS
│           ├── DigitalTwinApp.tsx      Shell + view switcher
│           ├── TerrainViewer.tsx       "Terrain & Risk" (default view)
│           ├── EventViewer.tsx         "Satellite Events"
│           ├── ConditionsDashboard.tsx "Conditions"
│           ├── SusceptibilityPage.tsx  "Susceptibility"
│           ├── OverviewDashboard.tsx   "Data Overview"
│           └── AoiMap.tsx              Shared AOI map component
│
├── launcher\
│   ├── Program.cs               ⚠️ Source of the .exe. Editing this changes NOTHING
│   │                              until rebuilt with the .NET 9 SDK.
│   └── MountHosmerDigitalTwin.Launcher.csproj
│
├── tests\                       ✅ 20 pytest tests (generated fixtures; never read real DATA\)
│   ├── test_catalog.py  test_terrain.py   test_events.py
│   ├── test_conditions.py  test_susceptibility.py
│   └── test_cache.py  test_paths.py  test_api_security.py
│
├── docs\                        ✅ App documentation (see index below)
│
└── runtime\                     🤖 GENERATED — never hand-edit, safe to delete & rebuild
    ├── catalog\                 data_catalog.json/.csv, warnings, point-cloud inventory
    ├── cache\                   SHA-256 input-signature sidecars (cache gating)
    ├── processed\
    │   ├── static\              Terrain GeoTIFFs, contours, terrain_susceptibility.tif
    │   ├── events\<id>\         event_summary.json, combined_susceptibility.tif
    │   └── dynamic\             weather/snow Parquet + summary JSON, forecast JSON
    ├── previews\                PNG overlays (layers\ and events\<id>\)
    ├── exports\                 User-facing downloads
    └── logs\                    backend/frontend logs, visual-QA output
```

### App docs index

| File | Covers |
|---|---|
| `docs\architecture.md` | System design, data flow, invariants, security boundary |
| `docs\backend-reference.md` | Module-by-module backend map |
| `docs\frontend-reference.md` | Component + API-client map |
| `docs\data-pipeline.md` | What each processing stage reads and writes |
| `docs\data-dictionary.md` | Field-level definitions |
| `docs\susceptibility-model.md` | The scoring model |
| `docs\limitations.md` | **What this cannot do — read before making claims** |
| `docs\windows-setup.md` | Windows/Rasterio/GeoPandas setup notes |

---

## 📦 `DATA\mount_hosmer_data\` — source data

**Read-only. Never write here.** 271 files, ~46 GB. Expensive to re-download; parts are unrecoverable.

```
DATA\mount_hosmer_data\
├── metadata\        AOI geometry, analysis grid, download manifest, event pairs, config
├── static\          Terrain & land cover that does not change over time
│   ├── lidar_bc\        BC LiDAR DEM/DSM tiles + 171 .laz point clouds (the bulk of the 46 GB)
│   ├── terrain_fallback\ Copernicus GLO-30 DEM + slope + aspect  ← what the app ACTUALLY uses
│   ├── landcover\       ESA WorldCover 2021 (10 m)
│   └── openstreetmap\   OSM infrastructure features
├── dynamic\         Time-varying conditions
│   ├── weather_eccc\    ECCC hourly + daily station CSVs
│   ├── snow_bc\         BC snow stations 2C09Q (Morrissey Ridge), 2C21P (Fernie)
│   └── avalanche_canada\ Current forecast JSON (live context, NOT history)
├── events\          Two satellite captures
│   ├── MH_20260116T183016Z\  sentinel2\ + landsat\
│   └── MH_20260430T182949Z\  sentinel2\ + landsat\
└── logs\            Download logs
```

Full breakdown: [`data-inventory.md`](data-inventory.md).

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
