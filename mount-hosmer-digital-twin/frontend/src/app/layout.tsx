import type { Metadata } from "next";

/* ---------------------------------------------------------------------------
 * Typography — three faces, three jobs.
 *
 * These come from the `@fontsource*` npm packages rather than `next/font/google`
 * ON PURPOSE. next/font downloads from fonts.googleapis.com during `next build`,
 * so any build environment that cannot reach Google — an offline machine, a
 * locked-down CI runner, a corporate proxy, an Azure build agent behind a
 * firewall — fails the build outright. The font files here are already inside
 * node_modules, so `npm ci && next build` needs no network beyond the npm
 * registry it was always going to use, and the running app serves the fonts
 * itself.
 *
 *   Archivo Variable — display. Headings and the hazard readout. A sturdy,
 *                      squarish grotesque that reads like equipment labelling.
 *   IBM Plex Sans    — body. Drawn for technical interfaces; holds up at the
 *                      10–12px this instrument lives at.
 *   IBM Plex Mono    — data. Every number, bearing and unit, with tabular
 *                      figures so values don't shift as they change.
 * ------------------------------------------------------------------------- */
import "@fontsource-variable/archivo";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";

import "./globals.css";

export const metadata: Metadata = {
  title: "Mount Hosmer Digital Twin",
  description: "Local research prototype for Mount Hosmer avalanche data discovery.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
