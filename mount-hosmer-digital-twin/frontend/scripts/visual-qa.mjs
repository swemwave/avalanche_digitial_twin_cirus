import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const url = process.env.VISUAL_QA_URL ?? "http://127.0.0.1:3000";
const output = path.resolve(process.env.VISUAL_QA_DIR ?? "../runtime/logs");
await fs.mkdir(output, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errors = [];
const imageryStatuses = [];
page.on("pageerror", (error) => errors.push(error.message));
page.on("response", (response) => {
  if (/\/api\/twin\/imagery\//.test(response.url())) imageryStatuses.push(response.status());
});
page.on("console", (message) => {
  if (message.type() === "error" && !/404/.test(message.text())) {
    errors.push(message.text());
  }
});

await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
const canvas = page.locator("canvas.maplibregl-canvas");
const satellite = page.getByRole("button", { name: "Satellite / snow" });
const hillshade = page.getByRole("button", { name: "Hillshade" });
await canvas.waitFor({ timeout: 30_000 });
await page.waitForTimeout(5_000);

const satelliteEnabled = await satellite.isEnabled();
const satelliteImage = await canvas.screenshot();
await hillshade.click();
await page.waitForTimeout(500);
const hillshadeImage = await canvas.screenshot();
const digest = (value) => createHash("sha256").update(value).digest("hex");
const surfaceViewsDiffer = digest(satelliteImage) !== digest(hillshadeImage);
if (satelliteEnabled) {
  await satellite.click();
  await page.waitForTimeout(500);
}

await page.getByRole("button", { name: "Assess" }).click();
await page.getByText("Release potential index").waitFor({ timeout: 60_000 });
const screenshot = path.join(output, "visual-qa-stage3.png");
await page.screenshot({ path: screenshot, fullPage: true });
const result = {
  url,
  screenshot,
  disclaimerVisible: await page.getByText(/never replaces Avalanche Canada guidance/i).isVisible(),
  satelliteEnabled,
  imageryTileLoaded: imageryStatuses.some((status) => status === 200),
  surfaceViewsDiffer,
  errors,
};
await fs.writeFile(path.join(output, "visual-qa-summary.json"), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result, null, 2));
await browser.close();
if (
  !result.disclaimerVisible ||
  !result.satelliteEnabled ||
  !result.imageryTileLoaded ||
  !result.surfaceViewsDiffer ||
  errors.length
) process.exitCode = 1;
