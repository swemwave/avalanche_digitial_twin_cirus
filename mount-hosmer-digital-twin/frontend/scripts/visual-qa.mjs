import { chromium } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";

const url = process.env.VISUAL_QA_URL ?? "http://127.0.0.1:3000";
const outputDir = path.resolve(process.env.VISUAL_QA_DIR ?? path.join(process.cwd(), "..", "runtime", "logs"));

await fs.mkdir(outputDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") {
    consoleErrors.push(message.text());
  }
});

async function capture(name, tabName, expectedText) {
  await page.getByRole("button", { name: tabName }).click();
  await page.getByText(expectedText).waitFor({ timeout: 60000 });
  await page.waitForLoadState("networkidle", { timeout: 60000 }).catch(() => {});
  await page.waitForTimeout(750);
  const bodyText = await page.locator("body").innerText();
  const imageCount = await page.locator("img").count();
  const visualElementCount = await page
    .locator("canvas, img, svg, .maplibregl-canvas, [style*='background-image']")
    .count();
  const overlayCount = await page
    .locator("[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay")
    .count();
  const screenshotPath = path.join(outputDir, `visual-qa-${name}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  return {
    name,
    tabName,
    expectedText,
    screenshotPath,
    textLength: bodyText.trim().length,
    imageCount,
    visualElementCount,
    overlayCount,
    hasExpectedText: bodyText.includes(expectedText),
  };
}

await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
const results = [];
results.push(await capture("terrain", "Terrain & Risk", "Terrain And Prototype Susceptibility"));
results.push(await capture("events", "Satellite Events", "Satellite Event Viewer"));
results.push(await capture("conditions", "Conditions", "Weather, Snowpack, And Forecast"));
results.push(await capture("susceptibility", "Susceptibility", "Prototype Susceptibility"));
results.push(await capture("overview", "Data Overview", "Data Sources"));

const result = {
  url,
  generatedAtUtc: new Date().toISOString(),
  outputDir,
  results,
  consoleErrors,
};
await fs.writeFile(path.join(outputDir, "visual-qa-summary.json"), JSON.stringify(result, null, 2), "utf-8");
console.log(JSON.stringify(result, null, 2));
await browser.close();

if (
  consoleErrors.length > 0 ||
  results.some((item) => item.overlayCount > 0 || !item.hasExpectedText || item.textLength === 0) ||
  results
    .filter((item) => ["terrain", "events"].includes(item.name))
    .some((item) => item.visualElementCount === 0)
) {
  process.exitCode = 1;
}
