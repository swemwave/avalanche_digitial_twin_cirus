import { chromium } from "@playwright/test";

const url = process.env.SMOKE_URL ?? "http://127.0.0.1:3000";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") {
    consoleErrors.push(message.text());
  }
});

await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
const initialText = await page.locator("body").innerText();
await page.getByRole("button", { name: "Satellite Events" }).click();
await page.getByText("Satellite Event Viewer").waitFor({ timeout: 60000 });
const eventText = await page.locator("body").innerText();
await page.getByRole("button", { name: "Conditions" }).click();
await page.getByText("Weather, Snowpack, And Forecast").waitFor({ timeout: 60000 });
await page.getByText("Avalanche Canada Forecast").waitFor({ timeout: 60000 });
await page.waitForTimeout(500);
const conditionsText = await page.locator("body").innerText();
await page.getByRole("button", { name: "Susceptibility" }).click();
await page.getByText("Prototype Susceptibility").waitFor({ timeout: 60000 });
await page.getByText("Dynamic Condition Components").waitFor({ timeout: 60000 });
await page.waitForTimeout(500);
const susceptibilityText = await page.locator("body").innerText();
const overlayCount = await page
  .locator("[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay")
  .count();
const result = {
  url,
  hasContent: initialText.trim().length > 0 && eventText.trim().length > 0 && conditionsText.trim().length > 0 && susceptibilityText.trim().length > 0,
  containsCatalog: initialText.includes("Data Overview"),
  containsAoi: initialText.includes("Terrain And Prototype Susceptibility"),
  containsRisk: initialText.includes("Experimental Terrain Susceptibility"),
  containsEvents: eventText.includes("Satellite Event Viewer"),
  containsConditions: conditionsText.includes("Weather, Snowpack, And Forecast"),
  containsForecast: conditionsText.includes("Avalanche Canada Forecast"),
  containsSusceptibility: susceptibilityText.includes("Prototype Susceptibility"),
  containsDynamicComponents: susceptibilityText.includes("Dynamic Condition Components"),
  overlayCount,
  consoleErrors,
};
console.log(JSON.stringify(result, null, 2));
await browser.close();

if (!result.hasContent || !result.containsCatalog || !result.containsAoi || !result.containsRisk || !result.containsEvents || !result.containsConditions || !result.containsForecast || !result.containsSusceptibility || !result.containsDynamicComponents || overlayCount > 0 || consoleErrors.length > 0) {
  process.exitCode = 1;
}
