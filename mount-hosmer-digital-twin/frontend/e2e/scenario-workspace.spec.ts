import { expect, test } from "@playwright/test";
import type { AssessResult } from "../src/lib/twin";

const meta = {
  schema: "stage3-baked-v2",
  disclaimer: "Experimental research prototype.",
  grid: { crs: "EPSG:26911", resolution_m: 5, width: 120, height: 120 },
  aoi_bbox_wgs84: [-115.1, 49.5, -114.9, 49.7],
  aoi_corners_wgs84: [[-115.1, 49.5], [-114.9, 49.5], [-114.9, 49.7], [-115.1, 49.7]],
  center_wgs84: [-115, 49.6],
  tiles: { url_template: "/api/twin/tiles/{z}/{x}/{y}.png", tile_size: 256, min_zoom: 8, max_zoom: 15, count: 0 },
  imagery: null,
  identity: { bake_sha256: "b".repeat(64) },
  terrain: { lidar_fraction: 0.999, valid_fraction: 1, effective_source_resolution_m: 1, coverage_by_source_label: {}, source_codes: {} },
  forest: { source_codes: {}, coverage_by_source_label: {} },
  warnings: [],
};

const terrainOnlyResult = {
  schema_version: 3,
  generated_at_utc: "2026-08-10T12:00:00Z",
  duration_seconds: 0.01,
  model: { model_version: "stage3-test", config_sha256: "c".repeat(64), bake_sha256: "b".repeat(64) },
  conditions: null,
  simulation_mode: "fast",
  engine: "fast_routing_alpha",
  release_size: "medium",
  random_seed: null,
  release_potential_index: null,
  release_potential_band: null,
  release_potential_color: null,
  hazard_score: null,
  hazard_detail: { method: "Unknown values were not converted to zero.", zone_count: 0 },
  risk_level: null,
  risk_color: null,
  area_hazard_index: null,
  area_hazard_band: null,
  area_hazard_color: null,
  peak_zone_index: null,
  peak_zone_id: null,
  peak_zone_basis: null,
  no_zone_release_percentile_index: null,
  hazard_components: {
    method: "Unavailable because no release zone was computed at all.",
    zone_count: 0,
    zone_count_by_basis: {},
    contributing_zone_count: { release: 0, reach: 0, exposure: 0 },
    area_weighted_component_mean: {},
    exposure_layer: {
      available: false,
      role: "consequence_term_of_composite_hazard_index_only",
      used_in_release_model: false,
      used_in_runout_model: false,
      boundary: "Exposure never enters the release model.",
      reason: "This bake carries no exposure layer.",
    },
    band_thresholds: [],
    is_probability: false,
    is_calibrated: false,
  },
  release: {},
  release_zones: { type: "FeatureCollection", features: [], zone_count: 0, disclaimer: "Experimental", warnings: [] },
  runout: {
    status: "unavailable_missing_inputs",
    core_area_m2: null,
    uncertainty_area_m2: null,
    uncertainty_area_definition: "Unavailable; null is not zero.",
    uncertainty_is_confidence_interval: false,
    max_velocity_ms: null,
    runout_polygons: [],
    uncertainty_polygons: [],
    main_paths: [],
    per_zone: [],
  },
  zones: [],
  coverage: {
    denominator: "grid",
    release_model: { required_layers: [], grid_cell_count: 14400, valid_cell_count: 0, missing_cell_count: 14400, valid_fraction: 0 },
    runout_model: { required_layers: [], grid_cell_count: 14400, valid_cell_count: 0, missing_cell_count: 14400, valid_fraction: 0 },
  },
  provenance: {
    conditions: { kind: "legacy_simple_scenario", is_measurement: false, scenario_sha256: "s".repeat(64), classification: "terrain_only" },
    terrain_bake: { bake_sha256: "b".repeat(64) },
    terrain_source_coverage_by_label: {}, forest_source_coverage_by_label: {},
    footprint_source_mix: {}, imagery: { role: "visual_context_only", used_in_release_or_runout: false }, serving_data_source: "runtime_baked_only",
  },
  uncertainty: {
    conditions: { quantified: false, reason: "Missing" },
    release_potential: { quantified: false, reason: "Unavailable rather than zero." },
    runout: { kind: "unavailable", is_confidence_interval: false, envelope_includes_core: true, core_area_m2: null, envelope_area_m2: null, interpretation: "No runout envelope." },
  },
  validation: {
    field_validation: { status: "unavailable", eligible_observation_count: 0, dataset_ids: [], reason: "No eligible local events." },
    calibration: { status: "not_calibrated", eligible_observation_count: 0, reason: "Not calibrated." },
    software_verification: { status: "characterized_benchmarks", benchmark_version: "test", scope: [], interpretation: "Software only." },
    validation_data_contract: {
      status: "ingestion_scaffolding", schema_version: "test", normalized_projected_coordinates_required: true,
      explicit_calibration_holdout_partitions: true, canonical_geometry_rasterization: true,
      prediction_identity_required: true, code_reviewed_dataset_registry_required: true,
      trusted_dataset_count: 0, end_to_end_field_validation_ready: false,
    },
  },
  scenario: {
    schema_version: "mount-hosmer-observation-scenario-v1",
    mode: "simple",
    classification: "terrain_only",
    classification_basis: ["No evaluable loading."],
    conditions_used: false,
    result_scope: "unavailable_missing_inputs",
    completeness: {
      required_parameters: ["new_snow_depth", "wind_speed", "wind_direction", "release_size"],
      required_input_count: 4, known_required_input_count: 1,
      unknown_required_parameters: ["new_snow_depth", "wind_speed", "wind_direction"],
      provenance_complete_count: 1, unsupported_supplied_input_count: 0,
    },
    condition_coverage: {
      denominator: "grid", grid_cell_count: 14400, joint_supported_cell_count: 0,
      joint_unsupported_cell_count: 14400, joint_supported_fraction: 0, per_input: {},
      scope_rasterization_version: "cell-centres-v1", cell_inclusion_rule: "cell centre",
    },
    inputs: [
      { input_id: "snow", parameter: "new_snow_depth", label: "New snow", status: "unknown", value: null, unit: "cm", spatial_scope: { kind: "whole_area" } },
    ],
    unsupported_inputs: [], assumptions: ["Release-size class: medium category"],
    uncertainty_propagated: false, uncertainty_statement: "Not propagated.", warnings: [], limitations: [],
    reproducibility: {
      scenario_sha256: "s".repeat(64), scenario_contract_sha256: "k".repeat(64),
      numerical_replay_sha256: "r".repeat(64), canonicalization_version: "v1",
      deterministic: true, model_version: "stage3-test", config_sha256: "c".repeat(64), bake_sha256: "b".repeat(64),
      engine: "fast_routing_alpha", random_seed: null, statement: "Replay identity excludes wall-clock fields.",
    },
  },
  warnings: ["Missing values were not converted to zero."],
  disclaimer: "Experimental research prototype; never replaces Avalanche Canada or field assessment.",
  is_probability: false,
  is_operational_forecast: false,
} as unknown as AssessResult;

test.beforeEach(async ({ page }) => {
  await page.route("**/api/twin/meta", (route) => route.fulfill({ json: meta }));
  await page.route(/\/api\/twin\/(tiles|imagery)\//, (route) => route.fulfill({ status: 404 }));
});

test("blank simple inputs remain null and render an unavailable terrain-only result", async ({ page }) => {
  let submitted: Record<string, unknown> | null = null;
  await page.route("**/api/assess", async (route) => {
    submitted = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: terrainOnlyResult });
  });
  await page.goto("/");

  await expect(page.getByText(/Blank means unknown/i)).toBeVisible();
  // The label still says what pressing it will do; the click uses a stable hook so
  // a re-render between locate and click cannot detach the node under load.
  await expect(page.getByTestId("simple-assess")).toHaveText("Show terrain-only result");
  await page.getByTestId("simple-assess").click();
  await expect(page.getByText("Unavailable — conditions unknown")).toBeVisible();
  await expect(page.getByText("Terrain-only", { exact: true })).toBeVisible();
  expect(submitted).toMatchObject({
    new_snow_cm: null,
    wind_speed_kmh: null,
    wind_direction_deg: null,
    release_size: "medium",
  });
  await expect(page.getByText(/never replaces Avalanche Canada guidance/i)).toBeVisible();
});

test("advanced measured input requires timestamp and source before submission", async ({ page }) => {
  let requestCount = 0;
  await page.route("**/api/assess", async (route) => {
    requestCount += 1;
    await route.fulfill({ json: terrainOnlyResult });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Advanced conditions" }).click();

  // The locked physics defaults stay disclosed for transparency.
  await expect(page.getByText("Physics parameters are locked")).toBeVisible();

  // The four required inputs are editable immediately -- no disclosure to open.
  const status = page.getByLabel("New storm-snow depth status");
  await expect(status).toBeVisible();
  await status.selectOption("measured");
  await page.getByLabel("New storm-snow depth value (cm)").fill("25");

  // Declaring it measured without a time and source must block submission and say so.
  await expect(page.getByText(/needs a UTC time and a source/i)).toBeVisible();
  await page.getByRole("button", { name: "Assess advanced scenario" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "New storm-snow depth" })).toContainText(/require a UTC timestamp and source/i);
  expect(requestCount).toBe(0);

  // ...and the provenance fields that satisfy it are one click away, not gone.
  await page.getByRole("button", { name: "New storm-snow depth details" }).click();
  await page.getByLabel("Observed/derived at (UTC)").fill("2026-02-01T18:00");
  await page.getByLabel("Source name / station / observer").fill("Field notebook A");
  await expect(page.getByText(/needs a UTC time and a source/i)).toBeHidden();
  await page.getByRole("button", { name: "Assess advanced scenario" }).click();
  await expect.poll(() => requestCount).toBe(1);
});

test("every advisory parameter is still reachable and still records provenance", async ({ page }) => {
  // Decluttering must not delete capability: the 12 advisory inputs live behind
  // one group disclosure, and each still exposes the full provenance record.
  await page.route("**/api/assess", (route) => route.fulfill({ json: terrainOnlyResult }));
  await page.goto("/");
  await page.getByRole("button", { name: "Advanced conditions" }).click();

  const evidence = page.locator("details").filter({ hasText: "Field & snowpack evidence" }).first();
  await evidence.locator(":scope > summary").click();

  for (const label of [
    "Whumpfing",
    "Shooting cracks",
    "Recent avalanche activity",
    "Weak-layer type",
    "Weak-layer depth",
    "Stability-test result",
    "Slab depth",
    "Total snow depth",
    "Snow water equivalent",
    "Trigger type",
    "Release depth",
    "Avalanche density",
  ]) {
    await expect(page.getByLabel(`${label} status`)).toBeVisible();
  }

  // Spatial scope, uncertainty and notes all survive behind the per-row toggle.
  await page.getByRole("button", { name: "Whumpfing details" }).click();
  await page.getByLabel("Whumpfing status").selectOption("assumed");
  await expect(page.getByText("Spatial applicability (grid-cell centres)")).toBeVisible();
  await expect(page.getByText("Uncertainty", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Notes / interval / method")).toBeVisible();
});

test("recorded field evidence renders above the number and never changes it", async ({ page }) => {
  // A result the model computed normally, carrying advisories derived from records
  // the equations cannot use. The contract under test: the advisories appear ABOVE
  // the index, say they changed nothing, and say the evidence outranks the model.
  const withAdvisories = {
    ...terrainOnlyResult,
    release_potential_index: 56.6,
    release_potential_band: "Elevated index",
    release_potential_color: "#e35a26",
    hazard_score: 56.6,
    risk_level: "Elevated index",
    risk_color: "#e35a26",
    area_hazard_index: 52.1,
    area_hazard_band: "Elevated index",
    area_hazard_color: "#e35a26",
    peak_zone_index: 64.9,
    peak_zone_id: "RZ015",
    peak_zone_basis: "release_reach_exposure",
    hazard_components: {
      ...terrainOnlyResult.hazard_components,
      method: "Area-weighted mean of the per-zone composite hazard index.",
      zone_count: 40,
      contributing_zone_count: { release: 40, reach: 12, exposure: 12 },
      area_weighted_component_mean: { release: 0.57, reach: 0.57, exposure: 0.01 },
    },
    scenario: {
      ...terrainOnlyResult.scenario,
      classification: "hypothetical",
      result_scope: "whole_area",
      advisories: [
        {
          advisory_id: "direct_instability_signs",
          severity: "critical",
          title: "2 direct instability signs recorded",
          detail:
            "You recorded: Whumpfing (collapsing) underfoot; Shooting cracks. The model cannot see any of these.",
          parameters: ["whumpfing", "shooting_cracks"],
          overrides_model: true,
          changed_the_number: false,
        },
        {
          advisory_id: "context_recorded",
          severity: "note",
          title: "1 further record retained",
          detail: "Retained with full provenance: Total snow depth.",
          parameters: ["snow_depth"],
          overrides_model: false,
          changed_the_number: false,
        },
      ],
      advisory_summary: {
        count: 2,
        count_by_severity: { critical: 1, warning: 0, note: 1 },
        field_evidence_overrides_model: true,
        statement: "None of them changed the computed index.",
      },
    },
  } as unknown as AssessResult;

  await page.route("**/api/assess", (route) => route.fulfill({ json: withAdvisories }));
  await page.goto("/");
  await page.getByRole("button", { name: "Show terrain-only result" }).click();

  const critical = page.getByText("2 direct instability signs recorded");
  await expect(critical).toBeVisible();
  await expect(
    page.getByText(/Standard practice treats that evidence as outranking a terrain model/i),
  ).toBeVisible();

  // The advisory must sit ABOVE the headline number in document order.
  const order = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll("p, span"));
    const advisory = nodes.findIndex((n) => n.textContent?.includes("direct instability signs recorded"));
    const number = nodes.findIndex((n) => n.textContent?.trim() === "52.1");
    return { advisory, number };
  });
  expect(order.advisory).toBeGreaterThanOrEqual(0);
  expect(order.number).toBeGreaterThanOrEqual(0);
  expect(order.advisory).toBeLessThan(order.number);

  // Notes are collapsed, not shouted.
  await expect(page.getByText("1 further record retained")).toBeVisible();
  await expect(page.getByText("Retained with full provenance: Total snow depth.")).toBeHidden();
});

test("the simple panel sends air temperature and surfaces the rain advisory", async ({ page }) => {
  let submitted: Record<string, unknown> | null = null;
  await page.route("**/api/assess", async (route) => {
    submitted = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        ...terrainOnlyResult,
        release_potential_index: 44.2,
        hazard_score: 44.2,
        conditions: {
          new_snow_cm: 30, wind_speed_kmh: 30, wind_direction_deg: 225,
          wind_direction_compass: "SW", release_size: "medium",
          provenance: "user_supplied_simple_scenario", air_temperature_c: 4,
          precipitation_snow_fraction: 0, effective_new_snow_cm: 0,
        },
        scenario: {
          ...terrainOnlyResult.scenario,
          classification: "hypothetical",
          advisories: [{
            advisory_id: "rain_on_snow",
            severity: "critical",
            title: "Precipitation classified as rain at 4 degC",
            detail: "That is a statement about dry-slab loading, NOT about hazard.",
            parameters: ["air_temperature", "new_snow_depth"],
            overrides_model: true,
            changed_the_number: false,
          }],
          advisory_summary: {
            count: 1, count_by_severity: { critical: 1, warning: 0, note: 0 },
            field_evidence_overrides_model: true, statement: "No advisory changed the index.",
          },
        },
      },
    });
  });
  await page.goto("/");

  // Temperature is entered here, not only in the expert panel.
  await expect(page.getByLabel(/Air temperature/)).toBeVisible();
  await page.getByRole("button", { name: "Rain on snow · +4 °C" }).click();
  await expect(page.getByLabel(/Air temperature/)).toHaveValue("4");

  await expect(page.getByTestId("simple-assess")).toHaveText("Assess hypothetical scenario");
  await page.getByTestId("simple-assess").click();
  await expect.poll(() => submitted?.air_temperature_c).toBe(4);

  // The lower number arrives with the advisory that stops it being misread.
  await expect(page.getByText("Precipitation classified as rain at 4 degC")).toBeVisible();
  await expect(page.getByText(/NOT about hazard/)).toBeVisible();
});

test("a preset without temperature clears a previously entered one", async ({ page }) => {
  await page.route("**/api/assess", (route) => route.fulfill({ json: terrainOnlyResult }));
  await page.goto("/");

  await page.getByRole("button", { name: "Rain on snow · +4 °C" }).click();
  await expect(page.getByLabel(/Air temperature/)).toHaveValue("4");
  // Switching preset must not leave a stale temperature silently applied.
  await page.getByRole("button", { name: "Storm slab · SW wind" }).click();
  await expect(page.getByLabel(/Air temperature/)).toHaveValue("");
});
