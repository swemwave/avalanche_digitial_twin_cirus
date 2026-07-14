# Mount Hosmer Avalanche Digital Twin Prototype

Local web prototype for cataloging and viewing Mount Hosmer avalanche-relevant datasets. The current implementation completes Milestones 1-6: discovery, terrain digital twin, satellite event viewer, weather/snowpack/forecast dashboard, experimental susceptibility, and polish/verification.

This is a research and decision-support prototype. It is not an operational avalanche forecast and must not replace Avalanche Canada forecasts or field assessment.

Build progress is tracked in `MILESTONE_PROCESS.md`; completed items are checked off as milestones are verified.

## Documentation

Start here if you are new to the codebase (**AI agents: read [`../CLAUDE.md`](../CLAUDE.md) first**):

| Doc | Covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | System design, data flow, and the five invariants you must not break |
| [`docs/backend-reference.md`](docs/backend-reference.md) | Module-by-module backend map — *"where do I make this change?"* |
| [`docs/frontend-reference.md`](docs/frontend-reference.md) | Component and API-client map |
| [`docs/data-pipeline.md`](docs/data-pipeline.md) | What each processing stage reads and writes |
| [`docs/data-dictionary.md`](docs/data-dictionary.md) | Field-level definitions |
| [`docs/susceptibility-model.md`](docs/susceptibility-model.md) | The scoring model and its weights |
| [`docs/limitations.md`](docs/limitations.md) | **What this cannot do — read before making any claim about the model** |
| [`docs/windows-setup.md`](docs/windows-setup.md) | Windows / Rasterio / GeoPandas setup notes |
| [`../docs/glossary.md`](../docs/glossary.md) | Domain terms (SWE, NDSI, DEM vs DSM, AOI…) |
| [`../docs/data-inventory.md`](../docs/data-inventory.md) | What the 271 source files are, and which are actually used |

## Observed Local Data

The active source data root for this workstation is:

```powershell
D:\school\capstone\Avalanche\DATA\mount_hosmer_data
```

Initial inspection found 271 files, about 48.8 GB total:

- 171 `.laz` point-cloud files
- 62 `.tif` rasters
- 20 `.json` files
- 10 `.csv` tables
- 5 `.geojson` files
- 2 satellite event folders: `MH_20260116T183016Z`, `MH_20260430T182949Z`
- Copernicus fallback DEM, slope, and aspect
- BC LiDAR DEM/DSM raster tiles and point-cloud tiles
- ESA WorldCover, OpenStreetMap, ECCC weather, BC snow station, and Avalanche Canada files
- One known download error: `bc_snow:2C21P:archive` returned HTTP 404

## Windows Setup

From PowerShell:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
Copy-Item .env.example .env
```

Create and activate a Python environment:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
```

If you prefer the system Python that already has Rasterio installed, install only missing test/runtime tools:

```powershell
python -m pip install pytest laspy[lazrs] geopandas
```

Install the frontend:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm install
npx playwright install chromium
```

## Data Scan

Run the catalog scan from the project root:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli scan-data
```

For a faster development scan that skips SHA-256 verification:

```powershell
python -m app.cli scan-data --skip-checksum
```

Catalog outputs are written to:

```text
runtime\catalog\data_catalog.json
runtime\catalog\data_catalog.csv
runtime\catalog\catalog_warnings.json
```

Optional point-cloud header inspection:

```powershell
python -m app.cli inspect-point-clouds
```

If `laspy` or LAZ support is missing, point-cloud files are still cataloged by file name and size.

## Processing Commands

Regenerate terrain products:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli process-terrain --force
```

Regenerate all satellite event products:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli process-events --all --force
```

Process one event:

```powershell
python -m app.cli process-events --event-id MH_20260430T182949Z --force
```

Regenerate weather, snow, and forecast products:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli process-dynamic --force
```

Regenerate dynamic and combined susceptibility products:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli process-susceptibility --all --force
```

Processing is cache-aware. Each processor records SHA-256 source/config fingerprints under `runtime\cache`; running without `--force` reuses outputs only when the input signature still matches.

## Run Locally

### One-Click Launcher

Use the executable in the project root:

```text
D:\school\capstone\Avalanche\mount-hosmer-digital-twin\MountHosmerDigitalTwin.exe
```

Double-clicking it will:

- use `D:\school\capstone\Avalanche\DATA\mount_hosmer_data` as the source data root unless `.env` overrides it
- run a quick catalog scan if `runtime\catalog\data_catalog.json` is missing
- start the FastAPI backend on `http://127.0.0.1:8000`
- start the Next.js frontend on `http://127.0.0.1:3000`
- open the app in your default browser
- keep a small launcher window open so you can stop the app when you are done

Startup logs are written to:

```text
runtime\logs\backend.out.log
runtime\logs\backend.err.log
runtime\logs\frontend.out.log
runtime\logs\frontend.err.log
```

Leave the launcher window open while using the app. Press Enter in that window, press Ctrl+C, or close the window to stop the backend and frontend servers.

Backend:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m uvicorn app.main:app --reload --port 8000
```

Frontend:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
$env:NEXT_PUBLIC_API_BASE_URL="http://localhost:8000"
npm run dev
```

Open:

```text
http://localhost:3000
```

## API Endpoints Implemented

- `GET /api/health`
- `GET /api/catalog`
- `GET /api/catalog?compact=true`
- `POST /api/catalog/rescan`
- `GET /api/aoi`
- `GET /api/terrain/layers`
- `GET /api/terrain/metadata`
- `POST /api/terrain/process`
- `GET /api/terrain/osm`
- `GET /api/terrain/contours`
- `GET /api/events`
- `GET /api/events/{event_id}`
- `POST /api/events/{event_id}/process`
- `POST /api/events/process`
- `GET /api/events/{event_id}/layers/{layer_id}/metadata`
- `GET /api/events/{event_id}/layers/{layer_id}/preview`
- `GET /api/weather`
- `GET /api/weather/summary`
- `POST /api/weather/process`
- `GET /api/snow`
- `GET /api/snow/summary`
- `POST /api/snow/process`
- `GET /api/avalanche-forecast`
- `POST /api/avalanche-forecast/process`
- `POST /api/dynamic/process`
- `GET /api/susceptibility/terrain`
- `GET /api/susceptibility/events/{event_id}`
- `POST /api/susceptibility/events/{event_id}/process`
- `POST /api/susceptibility/events/process`
- `GET /api/susceptibility/events/{event_id}/metadata`
- `GET /api/susceptibility/events/{event_id}/preview`
- `GET /api/susceptibility/events/{event_id}/download`
- `GET /api/layers/{layer_id}/metadata`
- `GET /api/layers/{layer_id}/preview`
- `GET /api/download/{asset_id}`

## Terrain Viewer

The app now opens on the `Terrain & Risk` view. It displays:

- terrain hillshade as a fixed map backdrop
- prototype risk areas
- slope steepness
- open vs forested land-cover context
- OpenStreetMap infrastructure
- risk-focused toggles
- layer strength sliders
- legends
- factor explanations
- downloadable experimental susceptibility GeoTIFF

Elevation and aspect are still used by the experimental model, but they are not shown as separate map controls because they are secondary inputs for understanding risk areas.

Terrain previews are generated under:

```text
runtime\previews\layers
```

The experimental susceptibility raster is written to:

```text
runtime\processed\static\terrain_susceptibility.tif
```

Terrain processing attempts BC LiDAR DEM first. For the available local data, LiDAR DEM and DSM mosaic coverage over the AOI grid is about 61.9%, so the processor falls back to Copernicus DEM and documents that surface height was skipped.

## Satellite Event Viewer

The `Satellite Events` view discovers event folders dynamically and currently finds:

- `MH_20260116T183016Z`
- `MH_20260430T182949Z`

Each processed event writes:

```text
runtime\processed\events\<event_id>\event_summary.json
runtime\previews\events\<event_id>\*.png
```

The event viewer exposes only the layers needed for avalanche-condition context:

- Sentinel-2 scene context
- Sentinel-2 snow-cover signal
- Sentinel-2 moisture signal
- Sentinel-2 classified snow
- Landsat surface temperature
- Sentinel and Landsat cloud/valid-data quality masks

Landsat NDVI, NDSI, and NDMI previews are recomputed from scaled reflectance bands when the source index rasters contain out-of-range values. This is documented in event warnings.

## Conditions Dashboard

The `Conditions` view displays:

- ECCC weather station selector
- BC snow station selector
- event-date marker selector
- temperature chart
- precipitation and snowfall chart
- wind speed and direction chart
- snow depth, SWE, and air-temperature chart
- station comparison table
- current Avalanche Canada forecast context
- danger ratings by elevation band
- avalanche problems when listed
- data coverage warnings

Generated dynamic outputs:

```text
runtime\processed\dynamic\weather_normalized.parquet
runtime\processed\dynamic\snow_stations_normalized.parquet
runtime\processed\dynamic\weather_summary.json
runtime\processed\dynamic\snow_summary.json
runtime\processed\dynamic\avalanche_forecast.json
```

Current local processing generated 27,527 weather records and 10,586 snow-station records. The known Fernie `2C21P` historical archive HTTP 404 is preserved as a dashboard warning. Avalanche Canada currently reports summer/off-season conditions; the app displays this as forecast context, not an avalanche observation label.

## Prototype Susceptibility

The `Susceptibility` view displays:

- event selector
- terrain, dynamic, and combined score cards
- combined susceptibility map
- dynamic component table
- available-input explanations
- static terrain factor explanations
- missing-data and warning panel
- configuration version and weights-file hash
- non-operational disclaimer

Dynamic components currently include:

- recent snowfall
- recent precipitation
- SWE change
- snow-depth change
- rapid warming
- strong wind
- satellite snow cover
- Landsat surface-temperature signal
- Avalanche Canada current forecast context as a non-scored contextual component

Missing dynamic inputs are reported and excluded from the weighted score denominator. They are not treated as safe zero values. Avalanche Canada current forecast context is not used as a historical avalanche observation label for event dates.

Generated susceptibility outputs:

```text
runtime\processed\events\<event_id>\susceptibility_summary.json
runtime\processed\events\<event_id>\combined_susceptibility.tif
runtime\processed\events\<event_id>\combined_susceptibility.metadata.json
runtime\previews\events\<event_id>\combined_susceptibility.png
```

## Tests

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m pytest
```

The tests use generated temporary data and a tiny generated GeoTIFF. They do not copy or modify the downloaded source data. The current suite has 20 tests covering catalog, AOI, raster metadata, path security, OSM categorization, event discovery, quality-mask interpretation, weather normalization, snow normalization, missing snow-archive behavior, SHA-256 cache invalidation, API path security, and dynamic susceptibility scoring.

Frontend build and browser smoke check:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm run build
$env:SMOKE_URL="http://127.0.0.1:3000"
npm run smoke
```

The smoke check expects the backend and frontend dev servers to already be running.

Visual QA screenshots:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm run visual-qa
```

This writes screenshots and a summary to `runtime\logs\visual-qa-*.png` and `runtime\logs\visual-qa-summary.json`.

## Troubleshooting

Rasterio and GeoPandas on Windows should usually install from wheels. If installation tries to build GDAL from source, update `pip` first and install into a fresh virtual environment.

```powershell
python -m pip install --upgrade pip setuptools wheel
python -m pip install rasterio geopandas
```

For LAZ support:

```powershell
python -m pip install "laspy[lazrs]"
```

For Parquet support:

```powershell
python -m pip install pyarrow
```

If the frontend cannot load data, confirm the backend is running and that `runtime\catalog\data_catalog.json` exists.
