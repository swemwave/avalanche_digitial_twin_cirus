# Data Dictionary

> ⚠️ **Superseded (pre-Stage-3).** Field definitions here (catalog records, event summaries, dynamic
> conditions, susceptibility) describe the removed pipeline. For the current shapes see
> `frontend/src/lib/twin.ts` and `backend/app/assess.py`. Kept for history only.

## Catalog Record

- `id`: stable catalog ID derived from relative source path
- `relative_path`: source path relative to `MOUNT_HOSMER_DATA_ROOT`
- `size_bytes`: file size
- `manifest_status`: status from `download_manifest.csv`
- `dataset`: dataset label from the manifest
- `source`: source label from the manifest
- `checksum`: expected, actual, and status fields
- `metadata`: type-specific metadata
- `warnings`: file-specific warnings

## Catalog Summary

- `file_count`: number of actual source files found
- `total_size_bytes`: total size of cataloged files
- `manifest`: manifest existence, row count, and status counts
- `type_counts`: counts by detected metadata type
- `extension_counts`: counts by file extension
- `checksum_counts`: counts by checksum status
- `event_ids`: discovered satellite event folders
- `missing_file_count`: expected or manifest files not found
- `failed_check_count`: checksum and path-safety failures
- `warning_count`: non-fatal warnings

## Weather Normalized Record

- `source_file`: source CSV relative to `MOUNT_HOSMER_DATA_ROOT`
- `cadence`: `hourly` or `daily`
- `station_key`: station identifier used by the dashboard
- `station_name`: ECCC station name
- `timestamp_utc`: normalized UTC timestamp
- `latitude`, `longitude`, `station_elevation_m`: station location metadata
- `air_temperature_c`, `temperature_min_c`, `temperature_max_c`: temperature values when available
- `precipitation_mm`, `rainfall_mm`, `snowfall_cm`, `snow_on_ground_cm`: precipitation and snow fields when available
- `wind_speed_kmh`, `wind_direction_degrees`, `wind_gust_kmh`: wind fields when available
- `relative_humidity_percent`, `station_pressure_kpa`: additional weather fields when available

## Snow Normalized Record

- `source_file`: source CSV relative to `MOUNT_HOSMER_DATA_ROOT`
- `source_type`: `current` or `archive`
- `station_id`: BC snow station ID
- `station_name`: station display name
- `timestamp_utc`: normalized UTC timestamp
- `snow_depth_cm`: snow depth when available
- `swe_mm`: snow water equivalent when available
- `air_temperature_c`: current feed air temperature when available
- `temperature_min_c`, `temperature_max_c`: archive temperature fields when available
- `precipitation_mm`, `accumulated_precipitation_mm`: precipitation fields when available

## Avalanche Forecast Summary

- `applicable_region`: Avalanche Canada region/title from the current forecast product
- `publication_time_utc`: forecast issue time
- `valid_until_utc`: forecast validity endpoint
- `highest_danger`: highest available danger rating metadata
- `danger_ratings`: ratings by date and elevation band
- `avalanche_problems`: listed avalanche problems when present
- `highlights`, `summaries`: forecast text context
- `freshness`: current validity status and age metadata
- `warnings`: stale/off-season/missing-data warnings
