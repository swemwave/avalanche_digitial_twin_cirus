# Progress

## Completed

- Confirmed the active source data root is `D:\school\capstone\Avalanche\DATA\mount_hosmer_data`.
- Inspected local data inventory:
  - 271 source files
  - about 48.8 GB total
  - 257 manifest rows
  - 2 satellite event folders
  - fallback terrain, WorldCover, OSM, weather, snow, Avalanche Canada, LiDAR DEM/DSM, and LAZ files present
- Created project structure under `D:\school\capstone\Avalanche\mount-hosmer-digital-twin`.
- Implemented `python -m app.cli scan-data`.
- Implemented safe path resolution so manifest paths cannot escape the configured source root.
- Implemented catalog outputs:
  - `runtime\catalog\data_catalog.json`
  - `runtime\catalog\data_catalog.csv`
  - `runtime\catalog\catalog_warnings.json`
- Implemented FastAPI endpoints:
  - `GET /api/health`
  - `GET /api/catalog`
  - `POST /api/catalog/rescan`
  - `GET /api/aoi`
- Implemented a Next.js overview page that reads backend catalog, health, and AOI data.
- Added tests for manifest parsing, catalog generation, AOI loading, raster metadata extraction, and path traversal rejection.
- Generated the initial checksum-verified catalog:
  - 271 files cataloged
  - 48,759,551,642 bytes
  - 257 manifest checksums matched
  - 14 files not in manifest
  - 0 missing files
  - 0 failed checks
  - 173 warnings, mostly optional LAZ header support plus recorded download warnings
- Started local development servers:
  - Backend: `http://127.0.0.1:8000`
  - Frontend: `http://127.0.0.1:3000`
- Verified browser rendering with Playwright smoke check and saved screenshot:
  - `runtime\logs\overview-screenshot-wait.png`
- Added and verified a Windows launcher executable:
  - `MountHosmerDigitalTwin.exe`
  - starts backend and frontend from a cold state
  - opens `http://127.0.0.1:3000`
  - writes service logs under `runtime\logs`
- Added terrain visual viewer:
  - map opens on `Terrain & Risk`
  - risk-focused controls for prototype risk areas, slope steepness, open/forested terrain, and exposure context
  - hillshade remains visible as the fixed terrain backdrop
  - elevation and aspect are hidden from the control panel but still used in the model explanation
  - OSM infrastructure overlay
  - legends and cursor coordinates
  - experimental susceptibility explanation and disclaimer
- Generated terrain preview overlays:
  - `runtime\previews\layers\elevation.png`
  - `runtime\previews\layers\hillshade.png`
  - `runtime\previews\layers\slope.png`
  - `runtime\previews\layers\aspect.png`
  - `runtime\previews\layers\landcover.png`
  - `runtime\previews\layers\profile_curvature.png`
  - `runtime\previews\layers\plan_curvature.png`
  - `runtime\previews\layers\tri.png`
  - `runtime\previews\layers\tpi.png`
  - `runtime\previews\layers\flow_direction.png`
  - `runtime\previews\layers\flow_accumulation.png`
  - `runtime\previews\layers\ridges.png`
  - `runtime\previews\layers\gullies.png`
  - `runtime\previews\layers\drainage.png`
  - `runtime\previews\layers\open_terrain_mask.png`
  - `runtime\previews\layers\forested_terrain_mask.png`
  - `runtime\previews\layers\bare_rock_mask.png`
  - `runtime\previews\layers\snow_ice_mask.png`
  - `runtime\previews\layers\terrain_susceptibility.png`
- Generated contours:
  - `runtime\processed\static\contours.geojson`
- Implemented LiDAR-first DEM selection:
  - LiDAR DEM mosaic was attempted first.
  - LiDAR DEM coverage was about 61.9%, so the processor fell back to the Copernicus 30 m DEM.
  - LiDAR DSM coverage was about 61.9%, so surface height was skipped and documented.
- Generated experimental susceptibility GeoTIFF:
  - `runtime\processed\static\terrain_susceptibility.tif`
- Added terrain/risk API endpoints:
  - `GET /api/terrain/layers`
  - `GET /api/terrain/metadata`
  - `POST /api/terrain/process`
  - `GET /api/terrain/osm`
  - `GET /api/susceptibility/terrain`
  - `GET /api/layers/{layer_id}/metadata`
  - `GET /api/layers/{layer_id}/preview`
  - `GET /api/download/{asset_id}`
- Implemented satellite event processing:
  - discovered `MH_20260116T183016Z`
  - discovered `MH_20260430T182949Z`
  - generated Sentinel-2 true colour, false-colour vegetation, snow/moisture, NDVI, NDSI, NDMI, cloud, shadow, valid-data, and snow-class previews
  - generated Landsat true colour, false colour, surface temperature, NDVI, NDSI, NDMI, cloud, valid-data, and snow-quality previews
  - wrote `runtime\processed\events\<event_id>\event_summary.json`
  - wrote event preview PNGs under `runtime\previews\events\<event_id>`
- Added event API endpoints:
  - `GET /api/events`
  - `GET /api/events/{event_id}`
  - `POST /api/events/{event_id}/process`
  - `POST /api/events/process`
  - `GET /api/events/{event_id}/layers/{layer_id}/metadata`
  - `GET /api/events/{event_id}/layers/{layer_id}/preview`
  - `GET /api/susceptibility/events/{event_id}`
- Added the `Satellite Events` frontend view with an event selector, compact avalanche-condition layer controls, event map, summary statistics, warnings, and prototype disclaimer.
- Implemented Milestone 4 dynamic-condition data processing:
  - normalized ECCC weather to `runtime\processed\dynamic\weather_normalized.parquet`
  - normalized BC snow stations to `runtime\processed\dynamic\snow_stations_normalized.parquet`
  - wrote `runtime\processed\dynamic\weather_summary.json`
  - wrote `runtime\processed\dynamic\snow_summary.json`
  - wrote `runtime\processed\dynamic\avalanche_forecast.json`
  - preserved the known `bc_snow:2C21P:archive` HTTP 404 as a warning
  - parsed Avalanche Canada current forecast context, danger ratings, validity period, highlights, and forecast warnings
- Added dynamic-condition API endpoints:
  - `GET /api/weather`
  - `GET /api/weather/summary`
  - `POST /api/weather/process`
  - `GET /api/snow`
  - `GET /api/snow/summary`
  - `POST /api/snow/process`
  - `GET /api/avalanche-forecast`
  - `POST /api/avalanche-forecast/process`
  - `POST /api/dynamic/process`
- Added the `Conditions` frontend view with:
  - temperature chart
  - precipitation and snowfall chart
  - wind speed and direction chart
  - snow depth/SWE/temperature chart
  - station comparison table
  - Avalanche Canada forecast panel
  - danger ratings
  - avalanche problems
  - data-coverage warnings
- Added `MILESTONE_PROCESS.md` to track the build process and checked-off work.
- Saved event viewer screenshot:
  - `runtime\logs\event-viewer-screenshot.png`
- Saved conditions dashboard screenshot:
  - `runtime\logs\conditions-dashboard-screenshot.png`
- Implemented Milestone 5 experimental susceptibility:
  - dynamic condition score for each discovered event
  - component explanations with source, timestamp, units, original value, normalized value, weight, and missing-data status
  - combined terrain/dynamic susceptibility raster per event
  - combined susceptibility PNG preview per event
  - event-specific `susceptibility_summary.json`
  - combined GeoTIFF download endpoint
  - configuration hash reporting from `backend\config\susceptibility_weights.yaml`
- Added susceptibility API endpoints:
  - `GET /api/susceptibility/events/{event_id}`
  - `POST /api/susceptibility/events/{event_id}/process`
  - `POST /api/susceptibility/events/process`
  - `GET /api/susceptibility/events/{event_id}/metadata`
  - `GET /api/susceptibility/events/{event_id}/preview`
  - `GET /api/susceptibility/events/{event_id}/download`
- Added the `Susceptibility` frontend view with:
  - event selector
  - combined susceptibility map
  - terrain, dynamic, and combined score cards
  - dynamic component table
  - available-input explanations
  - static terrain factor explanations
  - missing-data and warning panel
  - non-operational disclaimer
- Generated Milestone 5 outputs:
  - `runtime\processed\events\MH_20260116T183016Z\susceptibility_summary.json`
  - `runtime\processed\events\MH_20260116T183016Z\combined_susceptibility.tif`
  - `runtime\processed\events\MH_20260430T182949Z\susceptibility_summary.json`
  - `runtime\processed\events\MH_20260430T182949Z\combined_susceptibility.tif`
  - `runtime\previews\events\<event_id>\combined_susceptibility.png`
- Saved susceptibility page screenshot:
  - `runtime\logs\susceptibility-page-screenshot.png`
- Completed Milestone 6 polish:
  - added source/config cache fingerprints for terrain, events, weather, snow, Avalanche Canada forecast, and event susceptibility
  - wrote cache sidecars under `runtime\cache`
  - added a project-root Python package shim so `python -m app.cli ...` works from `D:\school\capstone\Avalanche\mount-hosmer-digital-twin`
  - added cache invalidation tests
  - added API path-security tests
  - added `npm run visual-qa` screenshot automation
  - refreshed terrain, event, dynamic, and susceptibility runtime products with cache metadata
- Fixed the prior cache caveat:
  - removed the large-file checksum skip from processor cache fingerprints
  - every existing processor-tracked source/configuration file now receives a SHA-256 checksum
  - added a same-size/same-mtime content-change test to prove cache invalidation is byte-based
  - refreshed runtime cache records so no `skipped_large_file` status remains
- Generated Milestone 6 visual QA outputs:
  - `runtime\logs\visual-qa-terrain.png`
  - `runtime\logs\visual-qa-events.png`
  - `runtime\logs\visual-qa-conditions.png`
  - `runtime\logs\visual-qa-susceptibility.png`
  - `runtime\logs\visual-qa-overview.png`
  - `runtime\logs\visual-qa-summary.json`

## Current Limitations

- The dynamic condition score is rules-based and has not been validated against avalanche occurrence labels.
- Missing dynamic inputs are reported and excluded from the weighted score denominator; they are not treated as safe zero values.
- LiDAR DEM/DSM mosaics are attempted, but current valid coverage is insufficient for AOI-wide terrain products, so Copernicus fallback is used.
- Avalanche Canada reports regular forecasts have ended for the season; the dashboard displays this as forecast context and does not treat it as a historical avalanche label.
- Avalanche Canada forecast context is recorded in the event susceptibility explanation, but it is not used as a historical label for the January/April satellite event dates.
- Fernie `2C21P` current snow data exists, but its historical archive is unavailable and remains a warning.
- GeoPandas and laspy were not installed in the system Python during initial inspection; Rasterio, FastAPI, Pandas, NumPy, Shapely, PyProj, Pillow, and PyYAML were available.
- The full checksum scan reads about 48.8 GB and can take several minutes.
- `npm install` reports 2 moderate vulnerabilities in transitive frontend dependencies. `npm audit fix --force` was not run because it can introduce breaking dependency changes.

## Milestone Status

- Milestone 1 Discovery: complete.
- Milestone 2 Terrain digital twin: complete for available source coverage. LiDAR DEM/DSM coverage was insufficient, so fallback handling is active and documented.
- Milestone 3 Event viewer: complete.
- Milestone 4 Weather, snowpack, and forecast: complete.
- Milestone 5 Experimental susceptibility: complete.
- Milestone 6 Polish: complete.

## Commands Tested

```powershell
python --version
node --version
npm --version
rg --files DATA\mount_hosmer_data
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli scan-data --skip-checksum
python -m app.cli scan-data
python -m app.cli --help
python -m pip install pytest
python -m pytest
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm install
npx playwright install chromium
npm run build
$env:SMOKE_URL="http://127.0.0.1:3000"
npm run smoke
dotnet publish launcher\MountHosmerDigitalTwin.Launcher.csproj -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -p:DebugType=None -p:DebugSymbols=false -o .
.\MountHosmerDigitalTwin.exe
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli process-terrain --force
python -m app.cli process-events --all --force
python -m app.cli process-dynamic --force
python -m app.cli process-susceptibility --all --force
python -m app.cli process-terrain
python -m app.cli process-susceptibility --all
python -m pytest
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm run build
$env:SMOKE_URL="http://127.0.0.1:3000"
npm run smoke
$env:VISUAL_QA_URL="http://127.0.0.1:3000"
npm run visual-qa
```

## Next Recommended Step

Optional hardening beyond the requested milestones: package a full offline installer, add automated API contract tests for every endpoint, and review frontend transitive dependency updates.
