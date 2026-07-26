import { test, expect } from "@playwright/test";

/**
 * Stage 3 smoke: the single screen loads, the 3D LiDAR mesh builds from the baked
 * Terrain-RGB tiles, an assessment runs end to end, and the non-operational
 * disclaimer is on screen the whole time.
 *
 * The heavy science is covered by pytest. What only a browser can tell you is that
 * MapLibre actually drapes a mesh from our /api/twin/tiles PNGs and that a real
 * /api/assess round-trip renders a hazard index. The backend must be running on
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
  await expect(page.getByText(/Experimental and non-operational/i)).toBeVisible();
  await expect(page.getByText(/never a probability and never a forecast/i)).toBeVisible();

  // The mesh: a MapLibre canvas, built from baked terrain tiles.
  await expect(page.locator("canvas.maplibregl-canvas")).toHaveCount(1, { timeout: 20_000 });
  await page.waitForTimeout(8000); // let tiles stream in
  expect(tileStatuses.length).toBeGreaterThan(0);
  expect(tileStatuses.some((s) => s === 200)).toBe(true);
  // The only non-200 the tile endpoint may return is a legitimate 404 (no tile there).
  expect(tileStatuses.every((s) => s === 200 || s === 404)).toBe(true);

  // The natural surface is optional: it exists only if the bake was run with the
  // Sentinel-2 step, and `meta.json` says so by carrying an `imagery` key. Asserting
  // it unconditionally made this spec fail against any imagery-free bake -- which is
  // exactly what `docs/limitations.md` documents the current one as being. So ask the
  // API which bake is being served, and hold it to that.
  // Note the absolute URL: `baseURL` is the frontend (:3000), but in local dev the
  // API is a separate origin (:8000) -- the same split `NEXT_PUBLIC_API_BASE_URL`
  // encodes for the browser. Behind the ALB both are one origin and this still works.
  const apiBase = process.env.PLAYWRIGHT_API_URL ?? "http://localhost:8000";
  const meta = await (await page.request.get(`${apiBase}/api/twin/meta`)).json();
  const hasImagery = Boolean(meta.imagery);

  if (hasImagery) {
    await expect(page.getByRole("button", { name: "Satellite / snow" })).toBeEnabled();
    expect(imageryStatuses.some((s) => s === 200)).toBe(true);
    await page.getByRole("button", { name: "Hillshade" }).click();
    await page.getByRole("button", { name: "Satellite / snow" }).click();
  } else {
    // No imagery baked: the control must be disabled rather than offering a surface
    // that would 404, and the mesh falls back to hillshade.
    await expect(page.getByRole("button", { name: "Satellite / snow" })).toBeDisabled();
    expect(imageryStatuses).toEqual([]);
  }

  // Run a real assessment (fast mode, default storm-slab sliders).
  const assessResponse = page.waitForResponse(
    (r) => /\/api\/assess$/.test(r.url()) && r.request().method() === "POST",
    { timeout: 60_000 },
  );
  await page.getByRole("button", { name: "Assess" }).click();
  const response = await assessResponse;
  expect(response.status()).toBe(200);

  // The hazard index and its per-result disclaimer show up.
  await expect(page.getByText("Hazard index")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("/100")).toBeVisible();

  // No JavaScript exceptions, and no console errors other than the benign 404 of a
  // tile that legitimately does not exist outside the AOI.
  expect(pageErrors).toEqual([]);
  const realConsoleErrors = consoleErrors.filter(
    (t) => !/\/api\/twin\/(tiles|imagery)\//.test(t) && !/404/.test(t),
  );
  expect(realConsoleErrors).toEqual([]);
});
