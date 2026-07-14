export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export type HealthPayload = {
  status: string;
  data_root_exists: boolean;
  runtime_root_exists: boolean;
  catalog_exists: boolean;
  data_root_label: string;
  application_version: string;
};

export type CatalogSummary = {
  file_count: number;
  total_size_bytes: number;
  manifest: {
    exists: boolean;
    rows: number;
    status_counts: Record<string, number>;
  };
  type_counts: Record<string, number>;
  extension_counts: Record<string, number>;
  checksum_counts: Record<string, number>;
  event_ids: string[];
  event_count: number;
  missing_file_count: number;
  failed_check_count: number;
  warning_count: number;
  scan_duration_seconds: number;
};

export type CatalogPayload = {
  schema_version: number;
  generated_at_utc: string;
  summary: CatalogSummary;
  missing_files: Array<Record<string, unknown>>;
  failed_checks: Array<Record<string, unknown>>;
  download_errors: Array<Record<string, unknown>>;
};

export type AoiPayload = {
  source: string;
  geojson: GeoJSON.FeatureCollection;
  grid?: {
    analysis_crs?: string;
    aoi_bbox_wgs84?: number[];
    fixed_extent_analysis_crs?: number[];
    grid_10m?: { width: number; height: number };
    grid_30m?: { width: number; height: number };
    date_range?: { start: string; end: string };
  };
};

export type TerrainLayer = {
  id: string;
  title: string;
  group: string;
  kind: "raster_overlay";
  preview_url: string;
  metadata_url: string;
  download_url?: string | null;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  bounds_wgs84: [number, number, number, number];
  opacity: number;
  legend: Array<{ label: string; color: string }>;
  stats: { min: number | null; max: number | null; mean: number | null; valid_pixels: number };
  source_files: string[];
  warnings: string[];
  disclaimer?: string;
};

export type EventListItem = {
  event_id: string;
  date_label: string;
  metadata: Record<string, unknown>;
  available_sensors: string[];
  available_layers: Record<string, string[]>;
  processed: boolean;
  summary_url: string;
  warnings: string[];
};

export type EventsPayload = {
  events: EventListItem[];
  count: number;
};

export type EventLayer = {
  id: string;
  title: string;
  sensor: string;
  kind: "raster_overlay";
  preview_url: string;
  metadata_url: string;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  bounds_wgs84: [number, number, number, number];
  opacity: number;
  legend: Array<{ label: string; color: string }>;
  stats: {
    min: number | null;
    max: number | null;
    mean: number | null;
    median?: number | null;
    valid_pixels: number;
  };
  source_files: string[];
  warnings: string[];
};

export type EventDetail = {
  schema_version: number;
  event_id: string;
  date_label: string;
  generated_at_utc: string;
  metadata: {
    landsat_datetime_utc?: string;
    sentinel_datetime_utc?: string;
    time_difference_hours?: number;
    landsat_cloud_percent?: number;
    sentinel_cloud_percent?: number;
    [key: string]: unknown;
  };
  summary: {
    landsat_datetime_utc?: string;
    sentinel_datetime_utc?: string;
    time_difference_hours?: number;
    sensors: Record<
      string,
      {
        available: boolean;
        available_layers: string[];
        metadata_cloud_percent?: number;
        cloud_percent_calculated?: number | null;
        valid_pixel_percent?: number | null;
        snow_cover_percent?: number | null;
        indices?: Record<string, EventLayer["stats"]>;
        surface_temperature_c?: EventLayer["stats"];
      }
    >;
  };
  layers: EventLayer[];
  warnings: string[];
  disclaimer: string;
};

export type SusceptibilityComponent = {
  component: string;
  source: string;
  timestamp: string | null;
  units: string;
  original_value: number | string | null;
  normalized_value: number | null;
  weight: number;
  weighted_value: number | null;
  missing_data: boolean;
  status: string;
  reason: string;
};

export type CombinedSusceptibilityLayer = {
  id: string;
  title: string;
  kind: "raster_overlay";
  preview_url: string;
  metadata_url: string;
  download_url: string;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  bounds_wgs84: [number, number, number, number];
  opacity: number;
  legend: Array<{ label: string; color: string }>;
  stats: { min: number | null; max: number | null; mean: number | null; valid_pixels: number };
  processing: {
    terrain_weight: number;
    dynamic_weight: number;
    dynamic_condition_index: number;
    output_sha256?: string;
  };
  warnings: string[];
};

export type EventSusceptibilityPayload = {
  schema_version: number;
  event_id: string;
  date_label: string;
  generated_at_utc: string;
  model_type: string;
  terrain_susceptibility: {
    stats: { min: number | null; max: number | null; mean: number | null; valid_pixels: number };
    model_type: string;
    factors: Array<{ factor: string; source: string; reason: string }>;
    weight_in_combined_index: number;
  };
  dynamic_condition_index: {
    score: number | null;
    available_weight: number;
    total_configured_weight: number;
    available_weight_fraction: number;
    minimum_available_weight_fraction: number;
    components: SusceptibilityComponent[];
    warnings: string[];
  };
  combined_index: {
    available: boolean;
    stats: { min: number | null; max: number | null; mean: number | null; valid_pixels: number } | null;
    terrain_weight?: number;
    dynamic_weight?: number;
    output_raster?: string;
    output_sha256?: string;
  };
  combined_layer: CombinedSusceptibilityLayer | null;
  configuration: {
    version: string;
    weights_file: string;
    weights_sha256: string | null;
    weights: Record<string, unknown>;
  };
  warnings: string[];
  disclaimer: string;
};

export type WeatherStation = {
  station_key: string;
  station_name: string;
  latitude: number | null;
  longitude: number | null;
  elevation_m: number | null;
  distance_to_aoi_km: number | null;
  daily_records: number;
  hourly_records: number;
  latest_timestamp_utc: string;
  variables_available: string[];
};

export type WeatherRecord = {
  timestamp_utc: string;
  station_key: string;
  station_name: string;
  air_temperature_c?: number | null;
  temperature_min_c?: number | null;
  temperature_max_c?: number | null;
  precipitation_mm?: number | null;
  rainfall_mm?: number | null;
  snowfall_cm?: number | null;
  snow_on_ground_cm?: number | null;
  wind_gust_kmh?: number | null;
  wind_speed_kmh?: number | null;
  wind_direction_degrees?: number | null;
  relative_humidity_percent?: number | null;
  station_pressure_kpa?: number | null;
};

export type WeatherPayload = {
  schema_version: number;
  generated_at_utc: string;
  record_count: number;
  latest_weather_date: string;
  station_count: number;
  default_station_key: string | null;
  stations: WeatherStation[];
  daily_series: WeatherRecord[];
  hourly_recent_series: WeatherRecord[];
  event_windows: Record<string, { event_time_utc: string; windows: Record<string, Record<string, number | null>> }>;
  warnings: string[];
};

export type SnowStation = {
  station_id: string;
  station_name: string;
  latitude: number;
  longitude: number;
  elevation_m: number;
  record_count: number;
  current_record_count: number;
  archive_record_count: number;
  date_range_utc: { start: string | null; end: string | null };
  latest_snow_depth_cm: number | null;
  latest_swe_mm: number | null;
  variables_available: string[];
};

export type SnowRecord = {
  timestamp_utc: string;
  station_id: string;
  station_name: string;
  source_type: string;
  snow_depth_cm?: number | null;
  swe_mm?: number | null;
  snow_density_kg_m3?: number | null;
  air_temperature_c?: number | null;
  temperature_min_c?: number | null;
  temperature_max_c?: number | null;
  precipitation_mm?: number | null;
  accumulated_precipitation_mm?: number | null;
};

export type SnowPayload = {
  schema_version: number;
  generated_at_utc: string;
  record_count: number;
  stations: SnowStation[];
  series: SnowRecord[];
  event_windows: Record<string, unknown>;
  warnings: string[];
};

export type ForecastPayload = {
  schema_version: number;
  generated_at_utc: string;
  applicable_region: string | null;
  forecast_url?: string | null;
  publication_time_utc: string | null;
  valid_until_utc: string | null;
  timezone?: string | null;
  freshness: {
    valid_now: boolean;
    status: string;
    as_of_utc: string;
    age_hours: number | null;
  };
  highest_danger?: { value?: string; display?: string; colour?: string } | null;
  confidence?: { value?: string; display?: string } | null;
  highlights?: string | null;
  summaries: Array<{ type: string | null; content: string | null }>;
  danger_ratings: Array<{
    date: string | null;
    date_display: string | null;
    ratings: Record<string, { display?: string | null; value?: string | null; colour?: string | null }>;
  }>;
  avalanche_problems: Array<Record<string, unknown>>;
  terrain_and_travel_advice: Array<string | null>;
  warnings: string[];
  disclaimer: string;
};

export type TerrainLayersPayload = {
  generated_at_utc: string;
  terrain_metadata: {
    analysis_crs: string;
    terrain_source_selected: string;
    terrain_source_reason: string;
    available_sources: Record<string, boolean>;
    disclaimer: string;
  };
  layers: TerrainLayer[];
  susceptibility: SusceptibilityPayload;
};

export type SusceptibilityPayload = {
  disclaimer: string;
  model_type: string;
  not_used: string;
  weights_normalized: Record<string, number>;
  factors: Array<{ factor: string; source: string; reason: string }>;
  classes: Array<{ min: number; max: number; label: string; color: string }>;
  output_raster?: string;
  output_sha256?: string;
  stats: { min: number | null; max: number | null; mean: number | null; valid_pixels: number };
};

export type OsmPayload = GeoJSON.FeatureCollection & {
  categories?: Record<string, number>;
  source?: string;
};

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  return response.json() as Promise<T>;
}
