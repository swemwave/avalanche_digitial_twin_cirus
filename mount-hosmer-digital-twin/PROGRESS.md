# Progress

## Stage 3 ("Ultra") — current

The project was radically simplified to a single-screen twin with four features (3D LiDAR mesh, runout
simulation, a simplified slider-driven risk model, a local Ollama AI). The runtime is rasterio/pyproj-free
and reads no source data — it serves a one-time bake under `runtime\baked\`.

**Done and verified:**

- **Bake** (`backend\app\bake.py`, `python -m app.bake`): reuses the tested 5 m terrain engine to write
  `runtime\baked\` — 414 terrain-RGB tiles (z8–15), 7 `.npy` layers (elevation, slope, aspect,
  plan/general curvature, forest_mask, distance_to_ridge), and `meta.json` with a 21×21 grid→WGS84
  control lattice. LiDAR coverage 99.9 %.
- **Rasterio-free runtime**: `baked.py` (numpy loader + `Reprojector` from the lattice, accurate to <1 cm
  vs pyproj), `geo.py` (mask→GeoJSON / path→LineString via shapely), `risk.py` (one release model + zone
  extraction; owns `ReleaseZone`), `assess.py` (sliders→release→zones→runout→JSON, disclaimer in code),
  `assistant.py` (local Ollama; explain + scenario chat), `api/stage3.py` (the routes).
- **Frontend**: one screen — `Stage3App` / `Stage3Map` / `ConditionPanel` / `ResultCard` / `AssistantPanel`
  + `lib/twin.ts`.
- **Legacy removed**: the 5 research tabs + Data Health, the `jobs/` queue, `storage/` (SQLite), the
  `models/` physics zoo, `processing/weather/`, provenance/model-versioning, the `services/*` processors,
  `api/v1` + `api/schemas`, and the whole legacy frontend. Backend runtime dropped from ~16.7k to ~1k LOC.
- **Dependency trim**: runtime needs only fastapi/uvicorn/pydantic/numpy/scipy/shapely/httpx
  (`backend\requirements.txt`); rasterio/pyproj/pillow/pyyaml are bake-time only
  (`backend\requirements-bake.txt`). `import app.main` was verified with rasterio, pyproj, pandas,
  pyarrow, geopandas, laspy, sqlalchemy, pillow, matplotlib, and pyyaml all hard-blocked.
- **Tests** rewritten for Stage 3 against a hermetic synthetic bake: `test_risk_assess.py`,
  `test_stage3_api.py`, `test_geo.py`, plus the kept `test_paths.py`. Full suite green.
- **Verified end to end**: backend live (`/api/assess` returns hazard + zones + runout + disclaimer,
  `is_operational_forecast: false`; assistant degrades to 503 without Ollama); frontend `tsc` clean;
  Playwright smoke drives the live screen (mesh + tiles 200 + assess 200 + disclaimer, no JS errors).
- **Launcher** (`launcher\Program.cs`) updated to run the bake (not the old catalog scan) and rebuilt to
  `MountHosmerDigitalTwin.exe` with the .NET 9 SDK.

## Known follow-ups

- **Assess latency ~10–15 s** (a big storm lights up ~40 zones; runout runs for the top-12). A perf pass
  is open — this is tuning, not a bug.
- **Ollama assistant text generation** is exercised via the 503 path only when Ollama is absent; with
  `ollama serve` + `ollama pull llama3.1:8b` running, exercise `/api/assistant/explain` and `/chat`.
- Optional: package a full offline installer.

## Pre-Stage-3 history

Milestones 1–6 (discovery, terrain twin, satellite event viewer, conditions dashboard, experimental
susceptibility, polish) were completed on the pre-Stage-3 build and are recorded in the git history and in
`MILESTONE_PROCESS.md`. That build cataloged 271 source files (~48.8 GB), ran a job-queued processing
pipeline over weather/snow/satellite data, and served ~35 endpoints across five research tabs. Stage 3
supersedes it; see `../docs/data-footprint.md` for what is archived (not deleted) as a result.
