# Mountain Packs

A Mountain Pack is the strict bake-time contract that makes terrain sources
portable without weakening provenance or missing-data handling. The default pack
is `backend/config/mount_hosmer.pack.json`; select another with
`AVALANCHE_MOUNTAIN_PACK`.

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

The first supported terrain adapter is `geobc_lidar_year_tiles`, preserving the
existing newest-first GeoBC DEM/DSM mosaic behavior. `single_raster`,
`categorical_raster`, and `geojson` cover the existing fallback, land-cover,
imagery, and AOI files. Additional providers require explicit adapters and tests;
the bake does not guess unfamiliar layouts.

AI-generated or model-inferred avalanche events are not field evidence. AI may
help discover, normalize, or review records from an identified provider, but the
validation contract still requires source lineage, permission, dates, survey
coverage, uncertainty, and eligible professional or authoritative evidence.

