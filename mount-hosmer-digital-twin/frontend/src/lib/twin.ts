/** Thin application adapter over the SDK generated from FastAPI's OpenAPI contract. */

import {
  getPredictionComparison as getPredictionComparisonGenerated,
  getPredictionProduct as getPredictionProductGenerated,
  getTwinMeta as getTwinMetaGenerated,
  listPredictionProducts as listPredictionProductsGenerated,
  postAssess as postAssessGenerated,
  postChat as postChatGenerated,
  postExplain as postExplainGenerated,
} from "@/generated";
import type {
  AreaHazardComponents,
  AssessRequest as GeneratedAssessRequest,
  AssessResultOutput,
  ChatResult,
  ChatTurn,
  Conditions,
  ExplainResult,
  ExposureFeatureCollection,
  ExposureMeta,
  InputSource,
  InputUncertainty,
  PredictionEngineOutput,
  PredictionEnsembleMember,
  PredictionEnsembleSummary,
  PredictionProductDetail,
  PredictionProductList,
  PredictionProductSummary,
  PredictionStageRecord,
  PredictionUnsupportedSweep,
  ReleaseZoneOutput,
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
export type AssessResult = AssessResultOutput;
export type AssessZone = ReleaseZoneOutput;
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

export const getTwinMeta = () =>
  unwrap<TwinMeta>(getTwinMetaGenerated({ baseUrl: API_BASE_URL, cache: "no-store" }));

export const postAssess = (body: AssessRequest) =>
  unwrap<AssessResult>(postAssessGenerated({ baseUrl: API_BASE_URL, body, cache: "no-store" }));

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

export const tileUrlTemplate = () => `${API_BASE_URL}/api/twin/tiles/{z}/{x}/{y}.png`;
export const imageryTileUrlTemplate = () => `${API_BASE_URL}/api/twin/imagery/{z}/{x}/{y}.png`;

/** Static baked exposure vectors. MapLibre fetches this URL directly. */
export const exposureUrl = () => `${API_BASE_URL}/api/twin/exposure`;
