/** Thin application adapter over the SDK generated from FastAPI's OpenAPI contract. */

import {
  createMountain as createMountainGenerated,
  deleteMountain as deleteMountainGenerated,
  getMountain as getMountainGenerated,
  getPredictionComparison as getPredictionComparisonGenerated,
  getPredictionProduct as getPredictionProductGenerated,
  getTwinMeta as getTwinMetaGenerated,
  listMountains as listMountainsGenerated,
  listPredictionProducts as listPredictionProductsGenerated,
  postAssess as postAssessGenerated,
  postChat as postChatGenerated,
  postExplain as postExplainGenerated,
} from "@/generated";
import type {
  AreaHazardComponents,
  AssessRequest as GeneratedAssessRequest,
  AssessResult as GeneratedAssessResult,
  ChatResult,
  ChatTurn,
  Conditions,
  ExplainResult,
  ExposureFeatureCollection,
  ExposureMeta,
  InputSource,
  InputUncertainty,
  MountainList,
  MountainSummary,
  PredictionEngineOutput,
  PredictionEnsembleMember,
  PredictionEnsembleSummary,
  PredictionProductDetail,
  PredictionProductList,
  PredictionProductSummary,
  PredictionStageRecord,
  PredictionUnsupportedSweep,
  ReleaseZone as GeneratedReleaseZone,
  RunoutComparisonDetail,
  RunoutComparisonMetric,
  UnsupportedOutputRecord,
  Scenario,
  ScenarioAdvisory,
  ScenarioInput,
  ScenarioReport,
  SpatialScope,
  TwinMeta,
  ZoneHazardComponents,
} from "@/generated";

const base = (value: string | undefined, fallback: string) =>
  value === undefined ? fallback : value.trim().replace(/\/+$/, "");

export const API_BASE_URL = base(process.env.NEXT_PUBLIC_API_BASE_URL, "");
export const ASSISTANT_BASE_URL = base(process.env.NEXT_PUBLIC_ASSISTANT_BASE_URL, API_BASE_URL);

export type ReleaseSize = NonNullable<GeneratedAssessRequest["release_size"]>;
export type SimulationMode = NonNullable<GeneratedAssessRequest["simulation_mode"]>;
export type AssessRequest = GeneratedAssessRequest;
export type AssessResult = GeneratedAssessResult;
export type AssessZone = GeneratedReleaseZone;
export type {
  AreaHazardComponents,
  ChatResult,
  ChatTurn,
  Conditions,
  ExplainResult,
  ExposureFeatureCollection,
  ExposureMeta,
  InputSource,
  InputUncertainty,
  MountainList,
  MountainSummary,
  PredictionEngineOutput,
  PredictionEnsembleMember,
  PredictionEnsembleSummary,
  PredictionProductDetail,
  PredictionProductList,
  PredictionProductSummary,
  PredictionStageRecord,
  PredictionUnsupportedSweep,
  RunoutComparisonDetail,
  RunoutComparisonMetric,
  Scenario,
  ScenarioAdvisory,
  ScenarioInput,
  ScenarioReport,
  SpatialScope,
  TwinMeta,
  UnsupportedOutputRecord,
  ZoneHazardComponents,
};

type GeneratedResponse<T> = Promise<{
  data?: T;
  error?: unknown;
  response?: Response;
}>;

export class TwinApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "TwinApiError";
  }
}

const message = (error: unknown): string => {
  if (typeof error === "string") return error;
  if (error && typeof error === "object") {
    const body = error as { detail?: unknown; error?: { message?: unknown } };
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.error?.message === "string") return body.error.message;
  }
  return "The API request failed.";
};

async function unwrap<T>(request: GeneratedResponse<T>): Promise<T> {
  const result = await request;
  if (result.error !== undefined || result.data === undefined) {
    throw new TwinApiError(message(result.error), result.response?.status ?? 0);
  }
  return result.data;
}

/**
 * Which mountain a call is about. `null` is the reviewed Mount Hosmer bake.
 *
 * Every call that touches terrain takes one, because an uploaded mountain and the
 * demo mountain are different terrain: a request that forgot the id would answer
 * from Mount Hosmer and look entirely plausible doing it.
 */
export type MountainId = string | null;

const scope = (mountain: MountainId) => (mountain ? { query: { mountain } } : {});

export const getTwinMeta = (mountain: MountainId = null) =>
  unwrap<TwinMeta>(
    getTwinMetaGenerated({ baseUrl: API_BASE_URL, cache: "no-store", ...scope(mountain) }),
  );

export const postAssess = (body: AssessRequest, mountain: MountainId = null) =>
  unwrap<AssessResult>(
    postAssessGenerated({ baseUrl: API_BASE_URL, body, cache: "no-store", ...scope(mountain) }),
  );

export const postExplain = (assessment: AssessResult) =>
  unwrap<ExplainResult>(
    postExplainGenerated({
      baseUrl: ASSISTANT_BASE_URL,
      body: { assessment },
      cache: "no-store",
    }),
  );

export const postChat = (
  text: string,
  assessment: AssessResult | null,
  history: ChatTurn[] = [],
) =>
  unwrap<ChatResult>(
    postChatGenerated({
      baseUrl: ASSISTANT_BASE_URL,
      body: { message: text, assessment, history },
      cache: "no-store",
    }),
  );

/** Immutable offline prediction products. These run no engine; they read files the
 *  offline pipeline already wrote. A product that carries no result still lists the
 *  stages that produced nothing and why, so "unavailable" is never rendered as zero. */
export const listPredictionProducts = () =>
  unwrap<PredictionProductList>(
    listPredictionProductsGenerated({ baseUrl: API_BASE_URL, cache: "no-store" }),
  );

export const getPredictionProduct = (productId: string) =>
  unwrap<PredictionProductDetail>(
    getPredictionProductGenerated({
      baseUrl: API_BASE_URL,
      path: { product_id: productId },
      cache: "no-store",
    }),
  );

export const getPredictionComparison = (productId: string, comparisonId: string) =>
  unwrap<RunoutComparisonDetail>(
    getPredictionComparisonGenerated({
      baseUrl: API_BASE_URL,
      path: { product_id: productId, comparison_id: comparisonId },
      cache: "no-store",
    }),
  );

// MapLibre fetches these URLs itself, so the mountain has to travel in the query
// string -- there is no request hook to put it anywhere else.
const suffix = (mountain: MountainId) => (mountain ? `?mountain=${encodeURIComponent(mountain)}` : "");

export const tileUrlTemplate = (mountain: MountainId = null) =>
  `${API_BASE_URL}/api/twin/tiles/{z}/{x}/{y}.png${suffix(mountain)}`;
export const imageryTileUrlTemplate = (mountain: MountainId = null) =>
  `${API_BASE_URL}/api/twin/imagery/{z}/{x}/{y}.png${suffix(mountain)}`;

/** Static baked exposure vectors. MapLibre fetches this URL directly. */
export const exposureUrl = (mountain: MountainId = null) =>
  `${API_BASE_URL}/api/twin/exposure${suffix(mountain)}`;

// --- Uploaded mountains -------------------------------------------------------

export const listMountains = () =>
  unwrap<MountainList>(listMountainsGenerated({ baseUrl: API_BASE_URL, cache: "no-store" }));

export const getMountain = (mountainId: string) =>
  unwrap<MountainSummary>(
    getMountainGenerated({
      baseUrl: API_BASE_URL,
      path: { mountain_id: mountainId },
      cache: "no-store",
    }),
  );

export type NewMountain = {
  name: string;
  provider: string;
  citation: string;
  licence: string;
  elevationsAreMetres: boolean;
  dem: File;
  landcover?: File | null;
};

export const createMountain = (upload: NewMountain) =>
  unwrap<MountainSummary>(
    createMountainGenerated({
      baseUrl: API_BASE_URL,
      body: {
        dem: upload.dem,
        landcover: upload.landcover ?? undefined,
        name: upload.name,
        provider: upload.provider,
        citation: upload.citation,
        licence: upload.licence,
        elevations_are_metres: upload.elevationsAreMetres,
      },
      cache: "no-store",
    }),
  );

export const deleteMountain = async (mountainId: string) => {
  const result = await deleteMountainGenerated({
    baseUrl: API_BASE_URL,
    path: { mountain_id: mountainId },
    cache: "no-store",
  });
  if (result.error !== undefined) {
    throw new TwinApiError(message(result.error), result.response?.status ?? 0);
  }
};
