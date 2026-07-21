/**
 * Typed client for the Stage 3 API.
 *
 * The whole surface is: terrain meta + static tiles, one synchronous `/assess`,
 * and the local-AI assistant. No jobs, no polling. Every result carries its
 * disclaimer as a required field, so a component cannot render a hazard number and
 * forget what it means.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

const API = `${API_BASE_URL}/api`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string; error?: { message?: string } };
      message = body.detail ?? body.error?.message ?? message;
    } catch {
      // keep the status-line message
    }
    throw new TwinApiError(message, response.status);
  }
  return (await response.json()) as T;
}

export class TwinApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "TwinApiError";
    this.status = status;
  }
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

// --- Types -------------------------------------------------------------------

export type ReleaseSize = "small" | "medium" | "large" | "very_large";
export type SimulationMode = "fast" | "advanced";

export type TwinMeta = {
  schema: string;
  disclaimer: string;
  grid: { crs: string; resolution_m: number; width: number; height: number };
  aoi_bbox_wgs84: [number, number, number, number];
  aoi_corners_wgs84: [number, number][];
  center_wgs84: [number, number];
  tiles: {
    url_template: string;
    tile_size: number;
    min_zoom: number;
    max_zoom: number;
    encoding: string;
    count: number;
  };
  imagery?: {
    url_template: string;
    tile_size: number;
    min_zoom: number;
    max_zoom: number;
    count: number;
    kind: string;
    captured_at_utc: string;
    cloud_percent: number;
    source_resolution_m: number;
    visual_context_only: true;
  };
  terrain: {
    lidar_fraction: number | null;
    valid_fraction: number | null;
    effective_source_resolution_m: number | null;
  };
  warnings: string[];
};

export type Conditions = {
  new_snow_cm: number;
  wind_speed_kmh: number;
  wind_direction_deg: number;
  wind_direction_compass: string;
  release_size: ReleaseSize;
  provenance: "user_supplied";
};

export type ReleaseZoneProperties = {
  zone_id: string;
  area_m2: number;
  area_hectares: number;
  mean_slope_deg: number;
  max_slope_deg: number;
  dominant_aspect_deg: number | null;
  dominant_aspect_compass: string | null;
  elevation_min_m: number | null;
  elevation_max_m: number | null;
  elevation_mean_m: number | null;
  forest_fraction: number;
  estimated_release_score: number;
  main_reasons: string[];
};

export type ReleaseZoneCollection = {
  type: "FeatureCollection";
  features: {
    type: "Feature";
    id: string;
    geometry: GeoJSON.Polygon | GeoJSON.MultiPolygon;
    properties: ReleaseZoneProperties;
  }[];
  zone_count: number;
  disclaimer: string;
  warnings: string[];
};

export type AssessResult = {
  schema_version: number;
  generated_at_utc: string;
  duration_seconds: number;
  model: { model_version: string; config_sha256: string };
  conditions: Conditions;
  simulation_mode: SimulationMode;
  engine: string;
  release_size: ReleaseSize;
  random_seed: number;
  hazard_score: number;
  hazard_detail: { method: string; zone_count?: number; max_zone_score?: number };
  risk_level: string;
  risk_color: string;
  release: Record<string, unknown>;
  release_zones: ReleaseZoneCollection;
  runout: {
    core_area_m2: number;
    uncertainty_area_m2: number;
    max_velocity_ms: number | null;
    runout_polygons: (GeoJSON.Polygon | GeoJSON.MultiPolygon)[];
    uncertainty_polygons: (GeoJSON.Polygon | GeoJSON.MultiPolygon)[];
    main_paths: GeoJSON.LineString[];
    per_zone: {
      zone_id: string;
      runout_area_m2?: number;
      horizontal_reach_m?: number;
      vertical_drop_m?: number;
      max_velocity_ms?: number;
      alpha_angle_achieved_deg?: number | null;
      alpha_envelope_exceeded?: boolean;
    }[];
  };
  zones: ReleaseZoneProperties[];
  warnings: string[];
  disclaimer: string;
  is_probability: false;
  is_operational_forecast: false;
};

export type AssessRequest = {
  new_snow_cm: number;
  wind_speed_kmh: number;
  wind_direction_deg: number;
  release_size: ReleaseSize;
  simulation_mode: SimulationMode;
  seed?: number | null;
};

export type ExplainResult = { explanation: string; disclaimer: string; model: string };

/** One prior turn, sent back so the assistant can follow the conversation. */
export type ChatTurn = { role: "you" | "assistant"; text: string };

export type ChatResult = {
  reply: string;
  /**
   * "scenario" ran the model (and carries an assessment); "answer"/"chat" are
   * grounded conversational replies; "advice" is a declined safety question.
   */
  kind: "scenario" | "answer" | "chat" | "advice";
  parsed_conditions: Conditions | null;
  assessment: AssessResult | null;
  disclaimer: string;
  model: string;
};

// --- Calls -------------------------------------------------------------------

export const getTwinMeta = () => request<TwinMeta>("/twin/meta");
export const postAssess = (body: AssessRequest) => post<AssessResult>("/assess", body);
export const postExplain = (assessment: AssessResult) =>
  post<ExplainResult>("/assistant/explain", { assessment });
export const postChat = (
  message: string,
  assessment: AssessResult | null,
  history: ChatTurn[] = [],
) => post<ChatResult>("/assistant/chat", { message, assessment, history });

export const tileUrlTemplate = () => `${API_BASE_URL}/api/twin/tiles/{z}/{x}/{y}.png`;
export const imageryTileUrlTemplate = () => `${API_BASE_URL}/api/twin/imagery/{z}/{x}/{y}.png`;
