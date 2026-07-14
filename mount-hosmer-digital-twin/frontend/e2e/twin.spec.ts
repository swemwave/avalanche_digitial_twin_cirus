import { test, expect } from "@playwright/test";

/**
 * The 3D twin renders, and the disclaimer is on screen.
 *
 * This does not run an analysis: that is ~90 s of numerical work plus a ~60 s
 * simulation, and a test suite nobody runs is worth nothing. The science is covered
 * by pytest. What is checked here is the thing only a browser can tell you -- that
 * MapLibre actually built a terrain mesh from our Terrain-RGB tiles.
 */
test("the 3D LiDAR terrain renders", async ({ page }) => {
  const tiles: string[] = [];
  const errors: string[] = [];
  page.on("request", (r) => { if (/terrain\/tiles/.test(r.url())) tiles.push(r.url()); });
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("/", { waitUntil: "domcontentloaded" });

  // The disclaimer is not dismissible and not optional.
  await expect(page.getByText(/Experimental and non-operational/i)).toBeVisible();
  await expect(page.getByText(/never a probability and never a forecast/i)).toBeVisible();

  await page.waitForTimeout(10000);
  expect(await page.locator("canvas.maplibregl-canvas").count()).toBeGreaterThan(0);
  expect(tiles.length).toBeGreaterThan(0);
  expect(errors).toEqual([]);
});

test("data health says presence is not usability", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Data Health" }).click();

  await expect(page.getByText(/empty file is missing data, never a reading of zero/i)).toBeVisible();
  await expect(page.getByText(/Uncalibrated/i).first()).toBeVisible();
});
