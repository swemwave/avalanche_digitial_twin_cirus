# Mount Hosmer Avalanche Digital Twin

A local research prototype that builds a "digital twin" of **Mount Hosmer, British Columbia** (near Fernie)
from ~46 GB of public geospatial data, and serves it as a web app: terrain, satellite imagery, weather and
snowpack conditions, and an **experimental** avalanche-susceptibility score.

> ⚠️ **This is a research and decision-support prototype, not an operational avalanche forecast.**
> It does not replace Avalanche Canada forecasts or field assessment. See
> [`limitations.md`](mount-hosmer-digital-twin/docs/limitations.md).

**AI agents:** start with [`CLAUDE.md`](CLAUDE.md).

---

## Repository layout

```
Avalanche\
├── CLAUDE.md                    Orientation for AI agents — read first
├── README.md                    You are here
│
├── mount-hosmer-digital-twin\   ✅ THE APPLICATION — all active work
│   ├── backend\                 FastAPI + the processing pipeline (Python)
│   ├── frontend\                Next.js + MapLibre + Recharts (TypeScript)
│   ├── launcher\                One-click .exe launcher (C# / .NET 9)
│   ├── tests\                   20 pytest tests
│   ├── docs\                    Architecture, pipeline, model, limitations
│   ├── runtime\                 Generated output (gitignored, rebuildable)
│   └── MountHosmerDigitalTwin.exe   ← double-click this to run everything
│
├── DATA\mount_hosmer_data\      ⛔ READ-ONLY source data — 271 files, ~46 GB
├── docs\                        Repo-level docs (map, data inventory, glossary)
├── archive\                     ⛔ Superseded work, kept for reference only
│   ├── web-app-demo\            Earlier prototype (own git repo)
│   └── qgis-project\            QGIS project + raw DEM/DSM/derived rasters
└── Tools\QGIS\                  Vendored QGIS/OSGeo4W install (third-party)
```

Only `mount-hosmer-digital-twin\` is live code. `DATA\`, `archive\`, and `Tools\` are inputs and history.

---

## Quick start

**The easy way** — double-click:

```
mount-hosmer-digital-twin\MountHosmerDigitalTwin.exe
```

It locates the project, scans the data catalog if it is missing, starts the backend on
`http://127.0.0.1:8000` and the frontend on `http://127.0.0.1:3000`, and opens your browser. Leave the
launcher window open while you use the app; press Enter in it to shut both servers down.

**By hand:**

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin

# one-time setup
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
cd frontend; npm install; cd ..

# run
python -m uvicorn app.main:app --reload --port 8000    # terminal 1
cd frontend; npm run dev                               # terminal 2
```

Then open <http://localhost:3000>.

Full setup notes, including Rasterio/GeoPandas on Windows:
[`windows-setup.md`](mount-hosmer-digital-twin/docs/windows-setup.md).

---

## What the app shows

| View | Content |
|---|---|
| **Terrain & Risk** | Hillshade backdrop, prototype risk areas, slope steepness, open vs. forested land cover, OSM infrastructure, layer toggles and strength sliders |
| **Satellite Events** | Sentinel-2 and Landsat imagery for the two captured events — snow cover, moisture, surface temperature, cloud/validity masks |
| **Conditions** | ECCC weather and BC snow-station charts (temperature, precipitation, wind, snow depth, SWE) plus current Avalanche Canada forecast context |
| **Susceptibility** | Combined terrain + conditions score with per-component breakdown, missing-data warnings, and a downloadable GeoTIFF |
| **Data Overview** | Catalog, AOI, and source-file summary |

---

## The study area

| | |
|---|---|
| Location | Mount Hosmer, BC (near Fernie) |
| Analysis CRS | EPSG:26911 (UTM zone 11N) |
| AOI bounds (WGS84) | −115.0965, 49.5582 → −114.9261, 49.6689 |
| Fixed extent (UTM) | 637650, 5491570 → 649650, 5503570 (12 × 12 km) |
| Analysis grids | 30 m → 400 × 400 · 10 m → 1200 × 1200 |
| Data date range | 2025-11-01 → 2026-05-31 |
| Events captured | `MH_20260116T183016Z`, `MH_20260430T182949Z` |

---

## How it works

```
DATA\ (read-only)  →  processing services  →  runtime\ (generated)  →  FastAPI  →  Next.js UI
```

Source data is **never** modified. Every processor hashes its inputs (SHA-256) and writes a cache sidecar,
so re-running only rebuilds what actually changed; `--force` overrides. The pipeline stages are
`catalog → terrain → events → conditions → susceptibility`, and susceptibility depends on the three before it.

```powershell
python -m app.cli scan-data
python -m app.cli process-terrain
python -m app.cli process-events --all
python -m app.cli process-dynamic
python -m app.cli process-susceptibility --all
```

Details: [`architecture.md`](mount-hosmer-digital-twin/docs/architecture.md) ·
[`data-pipeline.md`](mount-hosmer-digital-twin/docs/data-pipeline.md) ·
[`susceptibility-model.md`](mount-hosmer-digital-twin/docs/susceptibility-model.md)

---

## Tests

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m pytest
```

20 tests covering the catalog, AOI, raster metadata, path security, OSM categorization, event discovery,
quality masks, weather/snow normalization, cache invalidation, API path security, and susceptibility
scoring. They run on generated fixtures and never touch the real `DATA\` tree.

---

## Documentation

| Doc | What it covers |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | **AI agents start here** — where to work, invariants, gotchas |
| [`docs/repository-map.md`](docs/repository-map.md) | Every directory, annotated |
| [`docs/data-inventory.md`](docs/data-inventory.md) | What the 271 source files are and which are actually used |
| [`docs/glossary.md`](docs/glossary.md) | Domain terms (SWE, NDSI, DEM vs DSM, AOI…) |
| [`.../docs/architecture.md`](mount-hosmer-digital-twin/docs/architecture.md) | System design, request/data flow, invariants |
| [`.../docs/backend-reference.md`](mount-hosmer-digital-twin/docs/backend-reference.md) | Module-by-module backend map |
| [`.../docs/frontend-reference.md`](mount-hosmer-digital-twin/docs/frontend-reference.md) | Component + API-client map |
| [`.../docs/data-pipeline.md`](mount-hosmer-digital-twin/docs/data-pipeline.md) | What each processing stage does |
| [`.../docs/susceptibility-model.md`](mount-hosmer-digital-twin/docs/susceptibility-model.md) | The scoring model and its weights |
| [`.../docs/limitations.md`](mount-hosmer-digital-twin/docs/limitations.md) | **What this cannot do** |
