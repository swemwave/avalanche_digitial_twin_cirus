# Mount Hosmer Digital Twin Milestone Process

This file tracks the requested build process. It should be updated as work is completed and verified.

## Stage 3 ("Ultra") simplification — current

The build pivoted to Stage 3: a single-screen twin with four features (3D LiDAR mesh, runout simulation,
a slider-driven risk model, a local Ollama AI), serving a one-time bake with a rasterio-free runtime.
**Milestones 1–6 below describe the superseded pre-Stage-3 build** and are kept for history.

- [x] Bake pipeline (`python -m app.bake`) → `runtime\baked\` (414 tiles z8–15, 7 `.npy` layers, meta.json).
- [x] Rasterio-free runtime: `baked.py`, `geo.py`, `risk.py`, `assess.py`, `assistant.py`, `api/stage3.py`.
- [x] Single-screen frontend: Stage3App / Stage3Map / ConditionPanel / ResultCard / AssistantPanel + `lib/twin.ts`.
- [x] Deleted the legacy platform (jobs/DB/weather/models/susceptibility/5 tabs/`api/v1`) and legacy frontend.
- [x] Trimmed deps: runtime = fastapi/uvicorn/pydantic/numpy/scipy/shapely/httpx; rasterio/pyproj/pillow/pyyaml = bake-time only.
- [x] Rewrote tests against a synthetic bake (`test_risk_assess.py`, `test_stage3_api.py`, `test_geo.py`); full suite green.
- [x] Verified live: backend `/api/assess` (hazard + zones + runout + disclaimer), assistant 503 without Ollama, `tsc` clean, Playwright smoke green.
- [x] Updated the launcher to run the bake and rebuilt `MountHosmerDigitalTwin.exe` (.NET 9).
- [x] Rewrote CLAUDE.md, README, PROGRESS, architecture, limitations, docs, `.env.example`, and Docker for Stage 3.

Known follow-ups: assess latency ~10–15 s (tuning); Ollama text generation exercised only via the 503
path when Ollama is absent.

---

## Ground Rules (pre-Stage-3)

- [x] Use `D:\school\capstone\Avalanche\DATA\mount_hosmer_data` as the source data root.
- [x] Do not modify, rename, move, or overwrite source data.
- [x] Write generated products only under `runtime`.
- [x] Keep outputs clearly labelled as a research prototype.
- [x] Never present susceptibility as a validated avalanche forecast.

## Milestone 1: Discovery

- [x] Create project structure.
- [x] Read manifest, AOI, grid, events, logs, and config metadata.
- [x] Generate `runtime\catalog\data_catalog.json`.
- [x] Generate `runtime\catalog\data_catalog.csv`.
- [x] Generate `runtime\catalog\catalog_warnings.json`.
- [x] Verify manifest SHA-256 checksums.
- [x] Add `python -m app.cli scan-data`.
- [x] Add health/catalog/AOI APIs.
- [x] Add frontend catalog overview.
- [x] Add tests for catalog, AOI, raster metadata, and path security.

## Milestone 2: Terrain Digital Twin

- [x] Create initial terrain map viewer.
- [x] Display hillshade as fixed context.
- [x] Display slope, land cover, infrastructure, and experimental terrain susceptibility.
- [x] Add risk-focused controls.
- [x] Generate initial terrain susceptibility GeoTIFF.
- [x] Select terrain source using LiDAR priority where practical.
- [x] Generate/register elevation.
- [x] Generate/register hillshade.
- [x] Generate/register slope.
- [x] Generate/register aspect.
- [x] Generate contours.
- [x] Generate profile curvature.
- [x] Generate plan curvature.
- [x] Generate Terrain Ruggedness Index.
- [x] Generate Topographic Position Index.
- [x] Generate flow direction.
- [x] Generate flow accumulation.
- [x] Generate approximate ridges.
- [x] Generate approximate gullies/drainage lines.
- [x] Generate land-cover masks.
- [x] Add surface-height validation and output when DEM/DSM align.
- [x] Add LiDAR DEM/DSM metadata summaries.
- [x] Add downloadable processed terrain outputs.
- [x] Verify Milestone 2 API and UI.

Milestone 2 note: LiDAR DEM/DSM mosaicking was attempted first. Valid coverage over the AOI grid was about 61.9%, so the processor correctly fell back to the Copernicus 30 m DEM and skipped DSM minus DEM surface height.

## Milestone 3: Event Viewer

- [x] Discover event folders dynamically.
- [x] Read event metadata.
- [x] Register Sentinel-2 bands and indices.
- [x] Register Landsat bands and indices.
- [x] Generate Sentinel-2 true colour preview.
- [x] Generate Sentinel-2 false-colour vegetation preview.
- [x] Generate Sentinel-2 snow/moisture preview.
- [x] Generate Landsat true colour preview.
- [x] Generate Landsat false-colour preview.
- [x] Generate Landsat surface-temperature preview when available.
- [x] Register NDVI, NDSI, NDMI previews.
- [x] Generate cloud/valid masks where possible.
- [x] Calculate event summary statistics.
- [x] Write `runtime\processed\events\<event_id>\event_summary.json`.
- [x] Add event APIs.
- [x] Add frontend event selector and event map.
- [x] Verify Milestone 3 API and UI.

Milestone 3 note: two events are discovered from source folders: `MH_20260116T183016Z` and `MH_20260430T182949Z`. Each currently generates 19 browser-preview layers and an event summary.

## Milestone 4: Weather, Snowpack, Forecast

- [x] Normalize ECCC weather.
- [x] Normalize BC snow station records.
- [x] Handle 2C21P archive 404 gracefully in dashboard.
- [x] Display weather and snowpack charts.
- [x] Display Avalanche Canada forecast panel.

Milestone 4 note: weather normalization wrote 27,527 records, snow normalization wrote 10,586 records, and the dashboard displays ECCC station charts, BC snow-station charts, station comparison, and current Avalanche Canada forecast context.

## Milestone 5: Experimental Susceptibility

- [x] Implement static terrain susceptibility prototype.
- [x] Add factor explanations and disclaimer.
- [x] Implement dynamic condition score.
- [x] Implement combined terrain/dynamic index.
- [x] Add event-specific susceptibility summaries.

Milestone 5 note: event-specific dynamic scores are rules-based and use available weather, snow-station, satellite, and forecast-context inputs. Missing inputs are reported and are not converted to zero. Current Avalanche Canada forecast context is recorded but not used as a historical event label.

## Milestone 6: Polish

- [x] Add Windows executable launcher.
- [x] Add browser smoke check.
- [x] Add setup and pipeline docs.
- [x] Add broader integration tests.
- [x] Improve cache invalidation metadata.
- [x] Add more visual QA screenshots.

## Verification Log

- [x] `python -m pytest` passed with 7 tests.
- [x] `npm run build` passed after risk-focused control update.
- [x] `npm run smoke` passed after risk-focused control update.
- [x] Screenshot saved: `runtime\logs\risk-focused-view-screenshot.png`.
- [x] `python -m app.cli process-terrain --force` generated 19 terrain raster layers plus contours.
- [x] `python -m app.cli process-events --all --force` generated 2 event summaries with 19 layers each.
- [x] `python -m pytest` passed with 10 tests after event tests were added.
- [x] Backend API smoke check passed for health, terrain, events, event detail, and event susceptibility endpoints.
- [x] `npm run build` passed after the event viewer was added.
- [x] `npm run smoke` passed after switching to the Satellite Events tab.
- [x] Screenshot saved: `runtime\logs\event-viewer-screenshot.png`.
- [x] `python -m app.cli process-dynamic --force` generated weather, snow, and forecast summaries.
- [x] `python -m pytest` passed with 14 tests after Milestone 4 tests were added.
- [x] `npm run build` passed after the Conditions dashboard was added.
- [x] `npm run smoke` passed across Terrain, Satellite Events, and Conditions.
- [x] Screenshot saved: `runtime\logs\conditions-dashboard-screenshot.png`.
- [x] `python -m app.cli process-susceptibility --all --force` generated event susceptibility summaries and combined GeoTIFFs.
- [x] `python -m pytest` passed with 16 tests after Milestone 5 tests were added.
- [x] Susceptibility API smoke check passed for event summary, metadata, preview, and GeoTIFF download.
- [x] `npm run build` passed after the Susceptibility page was added.
- [x] `npm run smoke` passed across Terrain, Satellite Events, Conditions, and Susceptibility.
- [x] Screenshot saved: `runtime\logs\susceptibility-page-screenshot.png`.
- [x] Added root-level Python package shim so `python -m app.cli ...` works from the project root.
- [x] Added source/config cache fingerprints and `runtime\cache` sidecars for terrain, event, weather, snow, forecast, and susceptibility processors.
- [x] `python -m app.cli process-terrain --force` regenerated 19 terrain layers with cache metadata.
- [x] `python -m app.cli process-events --all --force` regenerated 2 event summaries with 19 layers each.
- [x] `python -m app.cli process-dynamic --force` regenerated 27,527 weather records, 10,586 snow records, and Avalanche Canada forecast context.
- [x] `python -m app.cli process-susceptibility --all --force` regenerated 2 combined event susceptibility outputs.
- [x] `python -m pytest` passed with 19 tests after the initial Milestone 6 cache/API tests were added.
- [x] `npm run build` passed on Next.js 16.2.10.
- [x] `npm run smoke` passed across Terrain, Satellite Events, Conditions, Forecast, and Susceptibility with 0 console errors and 0 overlays.
- [x] `npm run visual-qa` passed and wrote screenshots plus `runtime\logs\visual-qa-summary.json`.
- [x] Visual QA screenshots saved:
  - `runtime\logs\visual-qa-terrain.png`
  - `runtime\logs\visual-qa-events.png`
  - `runtime\logs\visual-qa-conditions.png`
  - `runtime\logs\visual-qa-susceptibility.png`
  - `runtime\logs\visual-qa-overview.png`
- [x] Removed large-file checksum skipping from cache fingerprints.
- [x] `python -m pytest` passed with 20 tests after adding same-size/same-mtime SHA-256 cache invalidation coverage.
- [x] Refreshed runtime cache records; no `skipped_large_file` cache status remains.
- [x] `python -m app.cli process-terrain` reused the refreshed SHA-256 cache signature with unchanged generated timestamp.

## Current Milestone Position

- [x] Milestone 1 complete.
- [x] Milestone 2 complete for the available source data, with documented Copernicus fallback due partial LiDAR coverage.
- [x] Milestone 3 complete.
- [x] Milestone 4 complete.
- [x] Milestone 5 complete.
- [x] Milestone 6 complete.
