# Frontend Reference

> ⚠️ **Superseded (pre-Stage-3).** This describes the old 5-tab frontend. Stage 3 is one screen:
> `Stage3App` / `Stage3Map` / `ConditionPanel` / `ResultCard` / `AssistantPanel` + `lib/twin.ts`. See
> [`architecture.md`](architecture.md) §7. Kept for history only.

A map of `frontend\src\`. Use this to answer **"which component do I change?"**

---

## Stack

| | |
|---|---|
| Framework | **Next.js 16** (App Router) + **React 19** |
| Maps | **MapLibre GL 5** — raster PNG overlays from the API |
| Charts | **Recharts 3** — weather / snowpack time series |
| Styling | **Tailwind CSS 4**, with CSS custom properties (`var(--accent)`, `var(--panel)`…) |
| Types | TypeScript 5.9 |
| Browser tests | Playwright (`npm run smoke`, `npm run visual-qa`) |

```powershell
npm run dev         # dev server on :3000
npm run build       # production build
npm run smoke       # browser smoke check (backend + frontend must be running)
npm run visual-qa   # screenshots → runtime\logs\visual-qa-*.png
```

---

## Layout

```
frontend\src\
├── app\                       App Router shell
│   ├── layout.tsx             Root layout (19 LOC)
│   └── page.tsx               Renders <DigitalTwinApp/> (5 LOC)
├── lib\
│   └── api.ts                 ✅ API client + EVERY response type (372 LOC)
└── components\
    ├── DigitalTwinApp.tsx     Shell + view switcher (76)
    ├── TerrainViewer.tsx      "Terrain & Risk"      (434) ← default view
    ├── EventViewer.tsx        "Satellite Events"    (446)
    ├── ConditionsDashboard.tsx "Conditions"         (404)
    ├── SusceptibilityPage.tsx "Susceptibility"      (378)
    ├── OverviewDashboard.tsx  "Data Overview"       (211)
    └── AoiMap.tsx             Shared AOI map        (106)
```

**No router, no global state, no SSR data fetching.** `DigitalTwinApp` holds a single
`useState<View>` and swaps between five client components. Each view fetches what it needs from the API on
mount. This is deliberate — it is a five-screen local tool, and it does not need more.

---

## `lib\api.ts` — the contract

One helper plus a TypeScript type for every backend response:

```ts
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
export async function fetchJson<T>(path: string): Promise<T>;
```

Exported types mirror the backend payloads one-for-one:

`HealthPayload` · `CatalogPayload` / `CatalogSummary` · `AoiPayload` · `TerrainLayer` /
`TerrainLayersPayload` · `EventListItem` / `EventsPayload` / `EventLayer` / `EventDetail` ·
`SusceptibilityComponent` / `CombinedSusceptibilityLayer` / `EventSusceptibilityPayload` /
`SusceptibilityPayload` · `WeatherStation` / `WeatherRecord` / `WeatherPayload` · `SnowStation` /
`SnowRecord` / `SnowPayload` · `ForecastPayload` · `OsmPayload`

> ⚠️ **This file is the mirror of `backend\app\main.py`.** TypeScript types are erased at runtime — they
> validate nothing. If you change a response shape in the backend and don't change it here, the build still
> passes and the **UI breaks silently in the browser**. Change both, together.

Images (`/preview`) and files (`/download`) are **not** fetched through `fetchJson` — they are used directly
as `<img>`/MapLibre source URLs built from `API_BASE_URL`.

---

## The five views

### `TerrainViewer.tsx` — "Terrain & Risk" *(default)*
The main map. Hillshade backdrop (fixed), with toggleable overlays: prototype risk areas, slope steepness,
open vs. forested land cover, OSM infrastructure. Layer opacity sliders, legends, factor explanations, and a
GeoTIFF download of the experimental susceptibility raster.

Consumes `GET /api/terrain/layers` · `/metadata` · `/osm` · `/contours` · `/api/susceptibility/terrain` ·
`/api/layers/{id}/preview`.

> Elevation and aspect feed the model but are **not** exposed as map toggles — they are secondary to
> understanding risk, and the view is deliberately kept risk-focused rather than exhaustive.

### `EventViewer.tsx` — "Satellite Events"
Event-date selector + satellite layer viewer for the two events. Shows only the avalanche-relevant layers:
Sentinel-2 scene context, snow-cover signal, moisture signal, classified snow; Landsat surface temperature;
and the cloud/valid-data quality masks.

Consumes `GET /api/events` · `/{id}` · `/{id}/layers/{layer_id}/preview` + `/metadata`.

### `ConditionsDashboard.tsx` — "Conditions"
Recharts dashboard: ECCC station selector, BC snow-station selector, event-date markers, and charts for
temperature; precipitation + snowfall; wind speed + direction; snow depth + SWE + air temp. Plus a station
comparison table, current Avalanche Canada forecast context, danger ratings by elevation band, avalanche
problems, and **data-coverage warnings** (including the permanent `2C21P` archive gap).

Consumes `GET /api/weather` · `/summary` · `/api/snow` · `/summary` · `/api/avalanche-forecast`.

### `SusceptibilityPage.tsx` — "Susceptibility"
Event selector; terrain / dynamic / combined score cards; the combined susceptibility map; a per-component
table; explanations of which inputs were available; the **missing-data and warning panel**; the config
version and weights-file hash; and the non-operational disclaimer.

Consumes `GET /api/susceptibility/events/{id}` + `/preview` · `/metadata` · `/download`.

> ⚠️ The missing-data panel and the disclaimer are **not decoration** — they are how invariant I3 reaches the
> user (see `architecture.md` §5). Missing inputs must stay visible, and the "experimental / not an
> operational forecast" language must stay on this page.

### `OverviewDashboard.tsx` — "Data Overview"
Catalog, AOI, and source summary: file counts by type, sizes, checksum status, missing files, download
errors. Consumes `GET /api/catalog?compact=true` · `/api/aoi` · `/api/health`.

### `AoiMap.tsx`
Shared MapLibre component rendering the AOI polygon; reused across views.

---

## Conventions

- Every view is a **client component** (`"use client"`) — they all use hooks.
- Fetch on mount, hold `loading` / `error` locally; there is no shared cache or query library.
- Styling is Tailwind utilities plus CSS variables for theme colors — match the existing
  `var(--panel)` / `var(--accent)` / `var(--muted)` vocabulary rather than introducing new hex values.
- API failures should surface as visible messages, not silent empty states — a blank chart is
  indistinguishable from "no avalanche risk", which is exactly the confusion this project must avoid.

## Changing the API contract

1. Change the backend service + `main.py`.
2. Update the matching type in `lib\api.ts` — **in the same commit**.
3. Update the consuming component.
4. `npm run build` (catches type errors) then `npm run smoke` (catches runtime breakage).
