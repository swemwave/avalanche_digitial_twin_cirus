/**
 * Typed client for the v1 API.
 *
 * The legacy `api.ts` client is untouched: the five existing views depend on it,
 * and it keeps working against the legacy `/api/*` routes. This module is additive,
 * and the views migrate to it one at a time.
 *
 * Two things about this contract that are not incidental:
 *
 * 1. **Long work returns a job, not a result.** An analysis is ~90 s and a
 *    simulation ~60 s. `POST /analysis` and `POST /simulations` answer 202 with a
 *    job id, and you poll it. `runAnalysis`/`runSimulation` below wrap that loop.
 *
 * 2. **Every score carries its disclaimer, warnings and limitations.** They are
 *    required fields on these types, not optional ones, so a component cannot
 *    render a hazard number and forget to render what it means. If that feels
 *    inconvenient, that is the type doing its job.
 */

import { API_BASE_URL } from "./api";

export const V1 = `${API_BASE_URL}/api/v1`;

// --- Errors -----------------------------------------------------------------

export type ApiErrorBody = {
  error: {
    code: string;
    message: string;
    status: number;
    correlation_id: string | null;
    detail?: { errors?: { field: string; message: string; type: string }[] } & Record<string, unknown>;
  };
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly correlationId: string | null;
  readonly detail?: Record<string, unknown>;

  constructor(body: ApiErrorBody["error"]) {
    super(body.message);
    this.name = "ApiError";
    this.status = body.status;
    this.code = body.code;
    this.correlationId = body.correlation_id;
    this.detail = body.detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${V1}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      // fall through to a generic error
    }
    if (body?.error) throw new ApiError(body.error);
    throw new ApiError({
      code: "http_error",
      message: `${response.status} ${response.statusText}`,
      status: response.status,
      correlation_id: response.headers.get("X-Correlation-ID"),
    });
  }
  return (await response.json()) as T;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });

// --- Vocabulary --------------------------------------------------------------

/** Never `probability`, never `forecast`. See docs/production-upgrade-plan.md. */
export type Provenance =
  | "observed"
  | "downloaded"
  | "derived"
  | "interpolated"
  | "modelled"
  | "user_supplied"
  | "unavailable";

export type ReleaseSize = "small" | "medium" | "large" | "very_large";
export type SimulationMode = "fast" | "advanced";
export type AnalysisMode = "historical" | "current" | "scenario";
export type JobState = "queued" | "running" | "succeeded" | "failed" | "cancelled";

// --- Jobs --------------------------------------------------------------------

export type Job = {
  job_id: string;
  job_type: string;
  state: JobState;
  progress: number;
  progress_message: string | null;
  failure_reason: string | null;
  result: Record<string, unknown> | null;
  result_id: string | null;
  correlation_id: string | null;
  created_utc: string | null;
  duration_seconds: number | null;
};

export type JobAccepted = {
  job_id: string;
  state: JobState;
  progress: number;
  deduplicated: boolean;
  poll: string;
  note: string;
};

export const getJob = (jobId: string) => get<Job>(`/jobs/${jobId}`);
export const listJobs = () => get<{ jobs: Job[] }>("/jobs");
export const cancelJob = (jobId: string) =>
  post<{ job_id: string; cancelled: boolean }>(`/jobs/${jobId}/cancel`);

/**
 * Poll a job to completion.
 *
 * `onProgress` is called on every tick, so the UI can show the real stage the
 * model is at ("Modelling wind loading") rather than an indeterminate spinner --
 * a 90-second wait with no explanation reads as a hang.
 */
export async function awaitJob(
  jobId: string,
  onProgress?: (job: Job) => void,
  { intervalMs = 1000, timeoutMs = 900_000 }: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<Job> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const job = await getJob(jobId);
    onProgress?.(job);
    if (job.state === "succeeded") return job;
    if (job.state === "failed" || job.state === "cancelled") {
      throw new Error(job.failure_reason ?? `Job ${jobId} ${job.state}.`);
    }
    if (Date.now() > deadline) throw new Error(`Job ${jobId} did not finish within the timeout.`);
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

// --- Health and data ---------------------------------------------------------

export type Health = {
  status: string;
  application_version: string;
  model_version: string;
  environment: string;
  jobs_enabled: boolean;
};

export type Readiness = {
  ready: boolean;
  checks: Record<string, boolean>;
  blocking: string[];
  remedy: string[];
  data_health: string;
  note: string;
};

export type HealthIssue = {
  severity: "critical" | "warning" | "info";
  code: string;
  message: string;
};

export type DatasetHealth = {
  key: string;
  title: string;
  category: string;
  status: "ok" | "warning" | "degraded" | "critical";
  present: boolean;
  /** Presence is not usability. An empty snow file is present, and unusable. */
  usable_by_model: boolean;
  provenance: Provenance;
  detail: Record<string, unknown>;
  issues: HealthIssue[];
  source_paths: string[];
  catalog_file_count: number;
  catalog_total_bytes: number;
};

export type DataHealth = {
  generated_at_utc: string;
  overall_status: "ok" | "warning" | "degraded" | "critical";
  summary: {
    datasets_checked: number;
    datasets_present: number;
    datasets_usable_by_model: number;
    critical_issues: number;
    warnings: number;
  };
  datasets: DatasetHealth[];
  issues: HealthIssue[];
  missing_datasets: { name: string; why_it_matters: string }[];
  empty_file_policy: string;
  calibration_status: { is_calibrated: false; reason: string };
};

export type EnrichedCatalog = {
  schema_version: number;
  generated_at_utc: string;
  summary: Record<string, unknown>;
  health: Pick<DataHealth, "overall_status" | "summary" | "issues" | "empty_file_policy" | "calibration_status"> & {
    missing_datasets: DataHealth["missing_datasets"];
  };
  datasets: DatasetHealth[];
  datasets_with_no_matching_files: string[];
  presence_is_not_usability: string;
};

export const getHealth = () => get<Health>("/health");
export const getReadiness = () => get<Readiness>("/ready");
export const getCatalog = () => get<EnrichedCatalog>("/data/catalog?compact=true");
export const getDataHealth = (refresh = false) =>
  get<DataHealth>(`/data/health${refresh ? "?refresh=true" : ""}`);

// --- Model -------------------------------------------------------------------

export type ModelConfig = {
  version: string;
  source_file: string;
  sha256: string;
  parameters: Record<string, unknown>;
  release_sizes: ReleaseSize[];
  disclaimer: string;
  is_calibrated: false;
  calibration_note: string;
};

export type ModelVersions = {
  current: { model_version: string; config_sha256: string; config_file: string };
  software_version: string;
  history: { version: string; config_sha256: string; software_version: string }[];
  note: string;
};

export const getModelConfig = () => get<ModelConfig>("/model/config");
export const getModelVersions = () => get<ModelVersions>("/model/versions");

// --- Analysis ----------------------------------------------------------------

export type ScenarioInput = {
  snowfall_24h_cm?: number;
  snowfall_48h_cm?: number;
  snowfall_72h_cm?: number;
  rain_24h_mm?: number;
  temperature_c?: number;
  temperature_change_24h_c?: number;
  wind_speed_kmh?: number;
  /** Meteorological convention: the direction the wind blows FROM. */
  wind_direction_deg?: number;
  wind_gust_kmh?: number;
  snow_depth_index?: number;
  swe_index?: number;
  freeze_thaw?: boolean;
  release_size?: ReleaseSize;
  label?: string;
};

export type ScenarioPreset = {
  /** The key the API expects back as `preset`. Not `name` — that field does not exist. */
  id: string;
  label: string;
  description: string;
  inputs: ScenarioInput;
};

export type ProvenanceValue = {
  name: string;
  value: number | string | null;
  units: string;
  provenance: Provenance;
  provenance_label: string;
  source: string;
  timestamp_utc: string | null;
  detail: string | null;
  available: boolean;
  measured: boolean;
};

export type ReleaseZoneProperties = {
  zone_id: string;
  area_m2: number;
  area_hectares: number;
  mean_slope_deg: number;
  max_slope_deg: number;
  dominant_aspect_deg: number;
  dominant_aspect_compass: string;
  aspect_consistency: number;
  elevation_min_m: number;
  elevation_max_m: number;
  elevation_mean_m: number;
  terrain_susceptibility_score: number;
  dynamic_instability_score: number;
  wind_loading_score: number;
  snow_depth_index: number;
  estimated_release_score: number;
  main_reasons: string[];
  limitations: string[];
  is_probability: false;
  score_type: string;
};

export type ReleaseZoneCollection = {
  type: "FeatureCollection";
  features: {
    type: "Feature";
    id: string;
    geometry: GeoJSON.Polygon;
    properties: ReleaseZoneProperties;
  }[];
  zone_count: number;
  disclaimer: string;
  warnings: string[];
};

export type InstabilityComponent = {
  component: string;
  title: string;
  configured_weight: number;
  available: boolean;
  provenance: Provenance;
  reason: string;
  /** Present only when the component was unavailable and therefore EXCLUDED. */
  missing_reason?: string;
  mean_score?: number;
  max_score?: number;
  input_value?: number;
};

export type Analysis = {
  schema_version: number;
  analysis_id: string;
  generated_at_utc: string;
  duration_seconds: number;
  mode: AnalysisMode;
  valid_time_utc: string;
  event_id: string | null;
  model: { model_version: string; config_sha256: string; config_file: string };
  software_version: string;
  grid: Record<string, unknown>;
  terrain: { lidar_fraction: number; effective_source_resolution_m: number; source: unknown };
  conditions: { values: Record<string, ProvenanceValue> } & Record<string, unknown>;
  snow: Record<string, unknown>;
  wind: Record<string, unknown>;
  instability: {
    available_weight_fraction: number;
    /** True when too little input survived to report a number at all. */
    score_withheld: boolean;
    components: InstabilityComponent[];
    missing_data_policy: string;
    limitations: string[];
    warnings: string[];
  } & Record<string, unknown>;
  release_zones: ReleaseZoneCollection;
  hazard_score: number;
  confidence_score: number;
  confidence_breakdown: ConfidenceBreakdown;
  layers: { id: string; title: string; group: string; units: string; opacity: number }[];
  warnings: string[];
  disclaimer: string;
  is_probability: false;
  is_official_forecast: false;
};

export type AnalysisSummary = {
  analysis_id: string;
  mode: AnalysisMode;
  valid_time_utc: string | null;
  event_id: string | null;
  hazard_score: number | null;
  confidence_score: number | null;
  release_zone_count: number;
  instability_withheld: boolean;
  model_version: string;
  duration_seconds: number | null;
  created_utc: string | null;
  simulation_count: number;
};

export type AnalysisRequest = {
  mode: AnalysisMode;
  at?: string;
  scenario?: ScenarioInput;
  preset?: string;
  event_id?: string;
  idempotency_key?: string;
};

export const listAnalyses = () =>
  get<{ analyses: AnalysisSummary[]; disclaimer: string }>("/analysis");
export const getAnalysis = (id: string) => get<Analysis>(`/analysis/${id}`);
export const getPresets = () => get<{ presets: ScenarioPreset[] }>("/analysis/presets");

/** Starts the analysis, polls it to completion, and returns the finished record. */
export async function runAnalysis(
  body: AnalysisRequest,
  onProgress?: (job: Job) => void,
): Promise<Analysis> {
  const accepted = await post<JobAccepted>("/analysis", body);
  const job = await awaitJob(accepted.job_id, onProgress);
  return getAnalysis(String(job.result_id));
}

// --- Simulation --------------------------------------------------------------

export type ConfidenceBreakdown = {
  components: Record<string, { score: number; weight: number; detail: string }>;
  weighted_score_before_penalties: number;
  penalties: string[];
  /** Hard-capped while the model is uncalibrated. It cannot be raised by better weather data. */
  maximum_without_calibration: number;
  ceiling_applied: boolean;
  ceiling_reason: string;
  final_score: number;
};

export type RiskAssessment = {
  riskLevel: string;
  riskColor: string;
  hazardScore: number;
  consequenceScore: number;
  combinedRiskScore: number;
  confidenceScore: number;
  consequenceClass: string;
  mainReasons: string[];
  limitations: string[];
  confidenceBreakdown: ConfidenceBreakdown;
  warnings: string[];
  isProbability: false;
  isOfficialForecast: false;
  disclaimer: string;
};

export type ExposedAsset = {
  asset_id: string;
  category: string;
  name: string | null;
  geometry_type: string;
  length_in_runout_m: number | null;
  max_flow_intensity: number | null;
  max_flow_velocity_ms: number | null;
  distance_from_release_m: number | null;
  osm_properties: Record<string, unknown>;
};

export type Simulation = {
  schema_version: number;
  simulation_id: string;
  analysis_id: string;
  generated_at_utc: string;
  duration_seconds: number;
  simulation_mode: SimulationMode;
  engine: string;
  release_size: ReleaseSize;
  random_seed: number;
  zone_ids: string[];
  model: { model_version: string; config_sha256: string };
  reproducibility: { note: string; seed: number; model_version: string; config_sha256: string };
  runout: {
    core_area_m2: number;
    uncertainty_area_m2: number;
    uncertainty_ratio: number | null;
    max_velocity_ms: number | null;
    runout_polygons: GeoJSON.Polygon[];
    uncertainty_polygons: GeoJSON.Polygon[];
    main_paths: GeoJSON.LineString[];
    alternate_paths: GeoJSON.LineString[];
    per_zone: {
      zone_id: string;
      /** Angle of reach at the deposit tip. Bounded by the energy line. */
      alpha_angle_achieved_deg?: number | null;
      alpha_envelope_deg?: number;
      alpha_envelope_exceeded?: boolean;
      runout_length_m?: number;
      particles_left_the_aoi?: number;
    }[];
  };
  exposure: {
    assets: ExposedAsset[];
    summary: { by_category: Record<string, { exposed_count: number; title: string }> };
    completeness: Record<string, unknown>;
    consequence_score: number;
    consequence_class: string;
    warnings: string[];
  };
  assessment: RiskAssessment;
  zones: ReleaseZoneProperties[];
  warnings: string[];
  disclaimer: string;
  is_probability: false;
  is_official_forecast: false;
};

export type SimulationSummary = {
  simulation_id: string;
  analysis_id: string | null;
  simulation_mode: SimulationMode;
  engine: string;
  release_size: ReleaseSize;
  random_seed: number | null;
  zone_ids: string[];
  risk_level: string | null;
  hazard_score: number | null;
  consequence_score: number | null;
  combined_risk_score: number | null;
  confidence_score: number | null;
  runout_area_m2: number | null;
  max_velocity_ms: number | null;
  model_version: string;
  duration_seconds: number | null;
  created_utc: string | null;
  exposed_asset_count: number;
};

export type SimulationRequest = {
  analysis_id: string;
  zone_ids?: string[];
  simulation_mode?: SimulationMode;
  release_size?: ReleaseSize;
  seed?: number;
  idempotency_key?: string;
};

export type SimulationAssets = {
  simulation_id: string;
  assets: ExposedAsset[];
  summary: Simulation["exposure"]["summary"];
  consequence_score: number;
  consequence_class: string;
  warnings: string[];
  /** OSM has ONE building for this AOI. The asset list is a floor, not a ceiling. */
  lower_bound_note: string;
  disclaimer: string;
};

export const listSimulations = () =>
  get<{ simulations: SimulationSummary[]; disclaimer: string }>("/simulations");
export const getSimulation = (id: string) => get<Simulation>(`/simulations/${id}`);
export const getSimulationAssets = (id: string) =>
  get<SimulationAssets>(`/simulations/${id}/assets`);
export const archiveSimulation = (id: string) =>
  post<{ simulation_id: string; archived: boolean }>(`/simulations/${id}/archive`);

export async function runSimulation(
  body: SimulationRequest,
  onProgress?: (job: Job) => void,
): Promise<Simulation> {
  const accepted = await post<JobAccepted>("/simulations", body);
  const job = await awaitJob(accepted.job_id, onProgress);
  return getSimulation(String(job.result_id));
}

// --- Layers ------------------------------------------------------------------

export type TerrainLayer = {
  id: string;
  title: string;
  group: string;
  units: string;
  /** The four AOI corners, for MapLibre's image source. */
  coordinates?: [[number, number], [number, number], [number, number], [number, number]];
  stats?: { min?: number; max?: number };
};

export const listLayers = () =>
  get<{ layers: TerrainLayer[]; layer_count: number; terrain: Record<string, unknown> }>("/layers");
