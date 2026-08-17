# Mountain Packs

A Mountain Pack is the strict bake-time contract that makes terrain sources
portable without weakening provenance or missing-data handling. The default pack
is `backend/config/mount_hosmer.pack.json`; select another with the command-line
options below or `AVALANCHE_MOUNTAIN_PACK`.

Keep independent mountains in independent generated runtime roots:

```powershell
python -m app.bake --pack path\to\mountain.pack.json `
  --data-root path\to\read-only-source-root `
  --runtime-root runtime\mountains\mountain-id --force
```

That writes `runtime\mountains\mountain-id\baked\`. A serving process selects the
same surface by setting `AVALANCHE_RUNTIME_ROOT` to that mountain's runtime root.
With no options or environment overrides, the existing Mount Hosmer behavior and
`runtime\baked\` location are unchanged. The runtime root must never be inside the
read-only source root.

Validate a pack before baking:

```powershell
python -m app.pack --pack path\to\mountain.pack.json --data-root path\to\read-only-data
```

The pack declares the mountain identity, projected metre grid, vertical-datum
status, model profile, and an allow-list of assets. Every asset has one role,
purpose, adapter, source, licence, units, and path inside the read-only data
root. Unknown vertical datum remains unknown. Imagery is display-only,
observations are validation evidence, and POIs are exposure data; none can
silently become a release-model input.

For a portable plain DEM (for example a prepared USGS 3DEP or Copernicus DEM),
declare the `elevation_primary` role with `adapter: single_raster`. The raster may
use any pack-declared projected metre CRS; the bake aligns it to the declared grid
and preserves its NoData mask. No fallback is required. If a separate fallback is
available, declare `elevation_fallback`, also with `single_raster`, and its own
source statement and native resolution. The generic adapter does not infer whether
a DEM was LiDAR-derived from its filename or provider name, so `lidar_fraction`
remains unknown unless a provider-specific adapter supplies per-cell evidence.

The legacy `elevation_lidar` role is intentionally restricted to
`geobc_lidar_year_tiles`, preserving Mount Hosmer's newest-first GeoBC DEM/DSM
mosaic behavior. `categorical_raster` and `geojson` cover land cover and AOI files.
Additional tiled provider layouts require explicit adapters and tests; the bake
does not guess unfamiliar filenames, years, or acquisition priority.

AI-generated or model-inferred avalanche events are not field evidence. AI may
help discover, normalize, or review records from an identified provider, but the
validation contract still requires source lineage, permission, dates, survey
coverage, uncertainty, and eligible professional or authoritative evidence.

