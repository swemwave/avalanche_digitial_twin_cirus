import { test, expect } from "@playwright/test";
import { createHash } from "node:crypto";

/**
 * Stage 3 smoke: the single screen loads, the 3D LiDAR mesh builds from the baked
 * Terrain-RGB tiles, an assessment runs end to end, and the non-operational
 * disclaimer is on screen the whole time.
 *
 * The heavy science is covered by pytest. What only a browser can tell you is that
 * MapLibre actually drapes a mesh from our /api/twin/tiles PNGs and that a real
 * /api/assess round-trip renders a release-potential index. The backend must be running on
 * :8000 and the frontend dev server on :3000 (localhost, not 127.0.0.1 -- Next 16
 * blocks dev resources cross-origin).
 */
test("3D terrain renders and an assessment runs", async ({ page }) => {
  const tileStatuses: number[] = [];
  const imageryStatuses: number[] = [];
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];

  page.on("response", (r) => {
    if (/\/api\/twin\/tiles\//.test(r.url())) tileStatuses.push(r.status());
    if (/\/api\/twin\/imagery\//.test(r.url())) imageryStatuses.push(r.status());
  });
  page.on("pageerror", (e) => pageErrors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });

  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  // The disclaimer is not dismissible and not optional (safety rule).
  await expect(page.getByText(/Experimental research scenario/i)).toBeVisible();
  await expect(page.getByText(/never replaces Avalanche Canada guidance/i)).toBeVisible();

  // The mesh: a MapLibre canvas, built from baked terrain tiles.
  await expect(page.locator("canvas.maplibregl-canvas")).toHaveCount(1, { timeout: 20_000 });
  // Polls rather than a fixed sleep: freshly-deployed containers (a cold ECS task
  // that just passed its health check) can take well over 8s for the first tile to
  // stream in, while a warm deployment answers in a couple of seconds -- a fixed
  // wait was either too slow for the common case or too short for a fresh rollout.
  await expect.poll(() => tileStatuses.length, { timeout: 30_000 }).toBeGreaterThan(0);
  expect(tileStatuses.some((s) => s === 200)).toBe(true);
  // The only non-200 the tile endpoint may return is a legitimate 404 (no tile there).
  expect(tileStatuses.every((s) => s === 200 || s === 404)).toBe(true);

  // The default natural surface is the fixed baked Sentinel-2 winter capture.
  const satellite = page.getByRole("button", { name: "Fixed satellite RGB" });
  const hillshade = page.getByRole("button", { name: "Hillshade" });
  await expect(satellite).toBeEnabled();
  await expect(satellite).toHaveAttribute("aria-pressed", "true");
  await expect(hillshade).toHaveAttribute("aria-pressed", "false");
  // The claim that imagery never enters the model stays on screen unconditionally;
  // its capture metadata is detail and lives one disclosure away.
  await expect(page.getByText(/Visual context only; it does not affect release or runout/i).first()).toBeVisible();
  await page.getByRole("group").filter({ hasText: "About these layers" }).locator("summary").click();
  await expect(page.getByText(/Fixed winter satellite capture \d{4}-\d{2}-\d{2}/)).toBeVisible();
  expect(imageryStatuses.some((s) => s === 200)).toBe(true);
  const canvas = page.locator("canvas.maplibregl-canvas");
  const satelliteImage = await canvas.screenshot();
  await hillshade.click();
  await expect(hillshade).toHaveAttribute("aria-pressed", "true");
  await expect(satellite).toHaveAttribute("aria-pressed", "false");
  await page.waitForTimeout(750);
  const hillshadeImage = await canvas.screenshot();
  const digest = (value: Buffer) => createHash("sha256").update(value).digest("hex");
  expect(digest(hillshadeImage)).not.toBe(digest(satelliteImage));
  await satellite.click();
  await expect(satellite).toHaveAttribute("aria-pressed", "true");
  await expect(hillshade).toHaveAttribute("aria-pressed", "false");

  // Run a real assessment from an explicitly labelled hypothetical example.
  await page.getByRole("button", { name: "Storm slab · SW wind" }).click();
  const assessResponse = page.waitForResponse(
    (r) => /\/api\/assess$/.test(r.url()) && r.request().method() === "POST",
    { timeout: 60_000 },
  );
  await page.getByRole("button", { name: "Assess hypothetical scenario" }).click();
  const response = await assessResponse;
  expect(response.status()).toBe(200);

  // The headline is the composite index, scoped to the whole area by default, and
  // the release-potential index remains published beside it as a component.
  await expect(page.getByText(/Whole area · average of \d+ zones?/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("/100").first()).toBeVisible();
  await expect(page.getByText(/Uncalibrated relative index, not a danger rating/i).first()).toBeVisible();
  await expect(page.getByText("Release potential", { exact: true }).first()).toBeVisible();

  // The three components are broken out, and each says what it means.
  for (const label of ["Release", "Reach", "Exposure"]) {
    await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
  }

  // The on-map legend names the active contrast mode, so a stretched view can
  // never be mistaken for absolute severity.
  await expect(page.getByText("Zone hazard index").first()).toBeVisible();
  await expect(page.getByText(/Absolute bands · comparable between runs/).first()).toBeVisible();
  await page.getByRole("button", { name: "Stretch to this run" }).click();
  await expect(page.getByText(/Stretched to this run · \d+–\d+/).first()).toBeVisible();
  await expect(page.getByText(/not a severity band and is not comparable with another run/i).first()).toBeVisible();
  await page.getByRole("button", { name: "Absolute bands" }).click();
  await expect(page.getByText(/Absolute bands · comparable between runs/).first()).toBeVisible();

  // Exposure is served from the bake and carries its ODbL attribution wherever
  // the layer is visible.
  const exposure = await page.request.get("/api/twin/exposure");
  expect([200, 404]).toContain(exposure.status());
  if (exposure.status() === 200) {
    const collection = await exposure.json();
    expect(collection.type).toBe("FeatureCollection");
    expect(collection.licence).toMatch(/ODbL/i);
    await expect(
      page.getByText(/Exposure © OpenStreetMap contributors/).first(),
    ).toBeVisible();
    await expect(page.getByText(/never enters the release model/i).first()).toBeVisible();
  }

  // No JavaScript exceptions, and no console errors other than the benign 404 of a
  // tile that legitimately does not exist outside the AOI.
  expect(pageErrors).toEqual([]);
  const realConsoleErrors = consoleErrors.filter(
    (t) => !/\/api\/twin\/(tiles|imagery)\//.test(t) && !/404/.test(t),
  );
  expect(realConsoleErrors).toEqual([]);
});
