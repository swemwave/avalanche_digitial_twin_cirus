# Data Pipeline

> ⚠️ **Superseded (pre-Stage-3).** Stage 3 replaced this multi-stage processing pipeline
> (scan-data → terrain/events/dynamic → susceptibility) with a single one-time bake
> (`python -m app.bake`). See [`architecture.md`](architecture.md) §4 and
> [`../../docs/data-footprint.md`](../../docs/data-footprint.md). Kept for history only.

## Milestone 1 Discovery

1. Read configured `MOUNT_HOSMER_DATA_ROOT`.
2. Read known discovery files when present.
3. Parse `metadata\download_manifest.csv`.
4. Walk actual source files.
5. Inspect metadata by file type:
   - Raster: CRS, bounds, dimensions, resolution, bands, dtypes, NoData, sampled min/max
   - Vector GeoJSON: CRS when present, bounds, feature count, geometry types
   - CSV/TSV: columns, row count, timestamp column ranges
   - JSON/YAML: structural summary
   - LAS/LAZ: header metadata when `laspy` is available
6. Verify SHA-256 checksums when enabled and manifest checksums exist.
7. Write JSON, CSV, and warning outputs under `runtime\catalog`.

The initial checksum-enabled scan on July 5, 2026 verified 257 manifest checksums successfully and found no missing manifest files.

## Non-Destructive Rule

No source files are modified, renamed, moved, or overwritten. Processed outputs and logs are written only under the project `runtime` folder.

## Cache And Reproducibility

Processing commands write cache sidecars under `runtime\cache` and embed matching cache metadata into summary/index JSON files. The cache metadata records:

- source files used by the processor
- configuration files such as `backend\config\susceptibility_weights.yaml`
- processing parameters
- file size and modified time
- SHA-256 for every existing tracked source/configuration file
- an input signature used to decide whether cached outputs are reusable

Terrain, event, weather, snow, forecast, and susceptibility processors reuse existing outputs when the stored input signature still matches and required previews or normalized outputs exist. Use `--force` to rebuild deliberately.

Cache checks read the tracked source files to compute SHA-256 before accepting cached outputs. This is slower on large raster inputs but gives deterministic cache invalidation from file contents instead of file timestamps.

## Terrain Viewer Pass

The terrain processor uses this priority:

1. Attempt BC LiDAR DEM mosaic on the 30 m AOI grid.
2. Use LiDAR only when AOI grid coverage is sufficient.
3. Fall back to Copernicus GLO-30 DEM when LiDAR coverage is incomplete.

For the current local data, LiDAR DEM/DSM coverage is about 61.9%, so Copernicus fallback is used and surface height is skipped with a warning.

Generated outputs:

- PNG map overlays in `runtime\previews\layers`
- sidecar layer metadata in `runtime\processed\static`
- terrain GeoTIFF outputs in `runtime\processed\static`
- contours in `runtime\processed\static\contours.geojson`
- experimental terrain susceptibility GeoTIFF in `runtime\processed\static\terrain_susceptibility.tif`

The susceptibility score is a rules-based prototype, not a validated forecast. It uses slope as the dominant factor with additional elevation, aspect, land cover, ruggedness, curvature, and ridge/gully proxy components.

## Event Viewer Pass

The event processor discovers folders matching the local event ID pattern, reads `event_metadata.json`, and scans Sentinel-2 and Landsat subfolders for actual files.

Generated outputs per event:

- `runtime\processed\events\<event_id>\event_summary.json`
- `runtime\processed\events\<event_id>\*.metadata.json`
- `runtime\previews\events\<event_id>\*.png`

Current discovered events:

- `MH_20260116T183016Z`
- `MH_20260430T182949Z`

Generated Sentinel-2 products:

- true colour
- false-colour vegetation
- snow/moisture false colour
- NDVI
- NDSI
- NDMI
- cloud mask
- cloud-shadow mask
- valid-data mask
- snow-class mask

Generated Landsat products:

- true colour
- false colour
- surface temperature display
- NDVI
- NDSI
- NDMI
- cloud mask
- valid-data mask
- snow-quality mask

Landsat index previews are recomputed from scaled reflectance bands when the downloaded source index rasters contain out-of-range values. Those cases are recorded as event warnings.

## Weather, Snowpack, And Forecast Pass

The dynamic processor reads ECCC weather CSVs, BC snow-station CSVs, and Avalanche Canada current forecast JSON from `dynamic`.

Generated outputs:

- `runtime\processed\dynamic\weather_normalized.parquet`
- `runtime\processed\dynamic\snow_stations_normalized.parquet`
- `runtime\processed\dynamic\weather_summary.json`
- `runtime\processed\dynamic\snow_summary.json`
- `runtime\processed\dynamic\avalanche_forecast.json`

Weather normalization supports hourly and daily ECCC schemas and maps available columns into common fields:

- air temperature
- min/max temperature
- precipitation
- rainfall
- snowfall
- snow on ground
- wind speed
- wind direction
- wind gust
- relative humidity
- pressure
- station coordinates and elevation

Snow-station normalization maps station-specific BC snow columns:

- `2C09Q` Morrissey Ridge current `SW` is stored as SWE in millimetres.
- `2C21P` Fernie current `SD` is stored as snow depth in centimetres.
- `PC` is stored as precipitation in millimetres when present.
- Morrissey Ridge archive is parsed with its historical header.
- Fernie historical archive remains unavailable and is reported as a warning.

Avalanche Canada processing extracts the current point forecast context, validity period, danger ratings, avalanche problems, highlights, summaries, data freshness, and source metadata. It is displayed as current professional forecast context only, not as historical avalanche labels.

## Susceptibility Pass

The susceptibility processor reads:

- static terrain susceptibility from `runtime\processed\static\terrain_susceptibility.tif`
- event summaries from `runtime\processed\events\<event_id>\event_summary.json`
- weather event windows
- snow-station event windows
- Avalanche Canada current forecast context
- `backend\config\susceptibility_weights.yaml`

Generated outputs:

- `runtime\processed\events\<event_id>\susceptibility_summary.json`
- `runtime\processed\events\<event_id>\combined_susceptibility.tif`
- `runtime\processed\events\<event_id>\combined_susceptibility.metadata.json`
- `runtime\previews\events\<event_id>\combined_susceptibility.png`

Dynamic condition components are normalized to 0-100 where data exists. Missing components are flagged and excluded from the available-weight denominator; they are not interpreted as safe conditions.

The combined raster is calculated only when enough dynamic input coverage exists:

```text
combined_index =
terrain_susceptibility * terrain_weight
+ dynamic_condition_index * dynamic_weight
```
