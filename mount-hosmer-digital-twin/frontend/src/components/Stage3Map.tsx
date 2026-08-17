"use client";

import { useEffect, useRef, useState } from "react";
import Map, {
  Layer,
  NavigationControl,
  ScaleControl,
  Source,
  type MapRef,
} from "react-map-gl/maplibre";
import {
  exposureUrl,
  imageryTileUrlTemplate,
  tileUrlTemplate,
  type AssessResult,
  type TwinMeta,
} from "@/lib/twin";

export type CameraPreset = "overview" | "north" | "south" | "top";
export type SurfaceView = "natural" | "hillshade";
/** Absolute bands compare between runs; stretched reveals spread inside one run. */
export type ContrastMode = "absolute" | "stretched";

const CAMERAS: Record<CameraPreset, { pitch: number; bearing: number; zoom: number }> = {
  overview: { pitch: 62, bearing: -25, zoom: 11.6 },
  north: { pitch: 70, bearing: 180, zoom: 12.2 },
  south: { pitch: 70, bearing: 0, zoom: 12.2 },
  top: { pitch: 0, bearing: 0, zoom: 11.4 },
};

const BASE_STYLE = {
  version: 8 as const,
  sources: {},
  layers: [{ id: "sky", type: "background" as const, paint: { "background-color": "#0b0f10" } }],
};
const DEFAULT_CENTER: [number, number] = [-115.0113, 49.6136];

/** A zone whose index could not be computed is grey, never the lowest band. The
    ramp is light throughout now, which leaves a mid grey closer to it than it was
    to the old one, so these also take a dashed outline no band carries. */
const UNKNOWN_COLOR = "#8a8f93";
/** Band-ordinal secondary encodings, so separation does not rest on hue alone.
    These carry more weight than they used to: ending the ramp on a vivid red
    rather than a pale yellow costs lightness range, so the bands sit closer
    together in greyscale and under deuteranopia than the colours alone suggest. */
const BAND_FILL_OPACITY = [0.34, 0.44, 0.54, 0.64, 0.74];
const BAND_LINE_WIDTH = [0.6, 0.9, 1.3, 1.7, 2.2];

type Band = { upper: number; band: string; color: string };

type Props = {
  meta: TwinMeta | null;
  result: AssessResult | null;
  exaggeration: number;
  camera: CameraPreset;
  surface: SurfaceView;
  showExposure?: boolean;
  contrast?: ContrastMode;
  selectedZone?: string | null;
  drawingArea?: boolean;
  drawnArea?: GeoJSON.Polygon | null;
  onDrawnArea?: (polygon: GeoJSON.Polygon) => void;
  onCancelDrawing?: () => void;
  onZoneClick?: (zoneId: string) => void;
};

export function Stage3Map({
  meta,
  result,
  exaggeration,
  camera,
  surface,
  showExposure = true,
  contrast = "absolute",
  selectedZone = null,
  drawingArea = false,
  drawnArea = null,
  onDrawnArea,
  onCancelDrawing,
  onZoneClick,
}: Props) {
  const map = useRef<MapRef>(null);
  const [ready, setReady] = useState(false);
  const [hoveringZone, setHoveringZone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draftVertices, setDraftVertices] = useState<[number, number][]>([]);
  const centre: [number, number] = meta?.center_wgs84 ?? DEFAULT_CENTER;

  useEffect(() => {
    if (ready) map.current?.easeTo({ center: centre, ...CAMERAS[camera], duration: 900 });
  }, [camera, centre, ready]);

  const zones = (result?.release_zones ?? emptyCollection()) as never;
  const runout = wrap(result?.runout.runout_polygons);
  const envelope = wrap(result?.runout.uncertainty_polygons);
  const paths = wrap(result?.runout.main_paths);

  // The bands come from the API, colours and thresholds together, so the map,
  // the legend and the number in the result card can never drift apart.
  const bands = (result?.hazard_components?.band_thresholds ?? []) as Band[];
  const allIndices = (result?.zones ?? []).map((zone) => zone.hazard_index);
  const indices = allIndices.filter((value): value is number => typeof value === "number");
  const hasUnknown = indices.length < allIndices.length;
  const spread =
    indices.length > 1
      ? { low: Math.min(...indices), high: Math.max(...indices) }
      : null;
  const stretchable = contrast === "stretched" && spread !== null && spread.high - spread.low > 1;
  const exposureVisible = showExposure && meta?.exposure != null;

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-[var(--border)]">
      {meta ? (
        <Map
          ref={map}
          initialViewState={{ longitude: centre[0], latitude: centre[1], ...CAMERAS.overview }}
          mapStyle={BASE_STYLE}
          maxPitch={85}
          terrain={{ source: "terrain", exaggeration }}
          interactiveLayerIds={drawingArea ? [] : ["zones-fill"]}
          cursor={drawingArea ? "crosshair" : hoveringZone ? "pointer" : "grab"}
          onLoad={() => setReady(true)}
          onMouseEnter={() => setHoveringZone(true)}
          onMouseLeave={() => setHoveringZone(false)}
          onClick={(event) => {
            if (drawingArea) {
              setDraftVertices((vertices) => [...vertices, [event.lngLat.lng, event.lngLat.lat]]);
              return;
            }
            const zoneId = event.features?.[0]?.properties?.zone_id;
            if (typeof zoneId === "string") onZoneClick?.(zoneId);
          }}
          onError={(event) => {
            const message = event.error?.message ?? "The map failed to load.";
            if (!message.includes("404")) setError(message);
          }}
        >
          <NavigationControl position="top-right" visualizePitch />
          <ScaleControl position="bottom-left" unit="metric" />

          <Source
            id="terrain"
            type="raster-dem"
            tiles={[tileUrlTemplate()]}
            tileSize={meta.tiles.tile_size}
            minzoom={meta.tiles.min_zoom}
            maxzoom={meta.tiles.max_zoom}
            encoding="mapbox"
          >
            <Layer
              id="hillshade"
              type="hillshade"
              layout={{ visibility: surface === "hillshade" ? "visible" : "none" }}
              paint={{
                "hillshade-shadow-color": "#0a0e0f",
                "hillshade-highlight-color": "#dfe8e0",
                "hillshade-exaggeration": 0.55,
              }}
            />
          </Source>

          {meta.imagery ? (
            <Source
              id="imagery"
              type="raster"
              tiles={[imageryTileUrlTemplate()]}
              tileSize={meta.imagery.tile_size}
              minzoom={meta.imagery.min_zoom}
              maxzoom={meta.imagery.max_zoom}
              bounds={meta.aoi_bbox_wgs84}
            >
              <Layer
                id="natural-surface"
                type="raster"
                layout={{ visibility: surface === "natural" ? "visible" : "none" }}
                paint={{ "raster-opacity": 1, "raster-fade-duration": 0 }}
              />
            </Source>
          ) : null}

          <Source id="condition-scope" type="geojson" data={polygonCollection(drawnArea)}>
            <Layer id="condition-scope-fill" type="fill" paint={{ "fill-color": "#65b8d4", "fill-opacity": 0.13 }} />
            <Layer id="condition-scope-line" type="line" paint={{ "line-color": "#8bd5ee", "line-width": 2, "line-dasharray": [2, 1] }} />
          </Source>
          <Source id="condition-scope-draft" type="geojson" data={draftCollection(draftVertices)}>
            <Layer id="condition-scope-draft-fill" type="fill" paint={{ "fill-color": "#8bd5ee", "fill-opacity": 0.2 }} />
            <Layer id="condition-scope-draft-line" type="line" paint={{ "line-color": "#d9f5ff", "line-width": 2 }} />
            <Layer id="condition-scope-draft-points" type="circle" paint={{ "circle-radius": 4, "circle-color": "#d9f5ff", "circle-stroke-color": "#101415", "circle-stroke-width": 1 }} />
          </Source>

          {/* Runout sits UNDER the zones and is deliberately faint: at 0.42 it
              muddied every band it crossed, which is the one thing the zone
              colours have to survive. */}
          <Source id="envelope" type="geojson" data={envelope}>
            <Layer id="envelope-fill" type="fill" paint={{ "fill-color": "#7fd4e8", "fill-opacity": 0.07 }} />
            <Layer id="envelope-line" type="line" paint={{ "line-color": "#7fd4e8", "line-width": 1, "line-dasharray": [2, 2], "line-opacity": 0.55 }} />
          </Source>
          <Source id="runout" type="geojson" data={runout}>
            <Layer id="runout-fill" type="fill" paint={{ "fill-color": "#7fd4e8", "fill-opacity": 0.16 }} />
            <Layer id="runout-line" type="line" paint={{ "line-color": "#cfeef7", "line-width": 1.1, "line-opacity": 0.8 }} />
          </Source>
          <Source id="paths" type="geojson" data={paths}>
            <Layer id="paths-line" type="line" paint={{ "line-color": "#ffffff", "line-width": 1.4, "line-opacity": 0.6 }} />
          </Source>

          <Source id="zones" type="geojson" data={zones}>
            <Layer
              id="zones-fill"
              type="fill"
              paint={{
                "fill-color": fillColor(bands, stretchable ? spread : null),
                "fill-opacity": dimmed(bandScale(bands, BAND_FILL_OPACITY, 0.3), selectedZone, 0.2),
              }}
            />
            <Layer
              id="zones-line"
              type="line"
              filter={["==", ["typeof", ["get", "hazard_index"]], "number"]}
              paint={{
                "line-color": "#f4f7f4",
                "line-width": bandScale(bands, BAND_LINE_WIDTH, 0.6),
                "line-opacity": dimmed(0.85, selectedZone, 0.25),
              }}
            />
            <Layer
              id="zones-unknown-line"
              type="line"
              filter={["!=", ["typeof", ["get", "hazard_index"]], "number"]}
              paint={{
                "line-color": "#f4f7f4",
                "line-width": 1,
                "line-dasharray": [3, 2],
                "line-opacity": dimmed(0.85, selectedZone, 0.25),
              }}
            />
            {/* The selected zone gets a white halo so the number in the card and
                the shape on the mountain are visibly the same thing. */}
            <Layer
              id="zones-selected-halo"
              type="line"
              filter={["==", ["get", "zone_id"], selectedZone ?? " "]}
              paint={{ "line-color": "#ffffff", "line-width": 4.5, "line-opacity": 0.55, "line-blur": 1.5 }}
            />
            <Layer
              id="zones-selected-line"
              type="line"
              filter={["==", ["get", "zone_id"], selectedZone ?? " "]}
              paint={{ "line-color": "#0b0f10", "line-width": 1.6 }}
            />
          </Source>

          {/* Exposure last, so roads and rail stay legible where they cross a
              zone. Never in interactiveLayerIds: they must not steal zone clicks. */}
          {exposureVisible ? (
            <Source id="exposure" type="geojson" data={exposureUrl()}>
              <Layer
                id="exposure-settlement-fill"
                type="fill"
                filter={["==", ["get", "exposure_class"], "inferred_settlement"]}
                paint={{ "fill-color": "#f2f7fb", "fill-opacity": 0.12 }}
              />
              <Layer
                id="exposure-settlement-line"
                type="line"
                filter={["==", ["get", "exposure_class"], "inferred_settlement"]}
                paint={{ "line-color": "#f2f7fb", "line-width": 1.2, "line-dasharray": [4, 2], "line-opacity": 0.85 }}
              />
              <Layer
                id="exposure-building"
                type="fill"
                filter={["==", ["get", "exposure_class"], "building_mapped"]}
                paint={{ "fill-color": "#f2f7fb", "fill-opacity": 0.55 }}
              />
              <Layer
                id="exposure-track"
                type="line"
                filter={["==", ["get", "exposure_class"], "track_trail"]}
                paint={{ "line-color": "#cbd6de", "line-width": 0.7, "line-opacity": 0.65 }}
              />
              <Layer
                id="exposure-road"
                type="line"
                filter={["==", ["get", "exposure_class"], "road_local"]}
                paint={{ "line-color": "#dfe8ef", "line-width": 1.3, "line-opacity": 0.8 }}
              />
              <Layer
                id="exposure-rail"
                type="line"
                filter={["==", ["get", "exposure_class"], "railway"]}
                paint={{ "line-color": "#ffffff", "line-width": 1.8, "line-dasharray": [2, 2], "line-opacity": 0.9 }}
              />
              <Layer
                id="exposure-highway"
                type="line"
                filter={["==", ["get", "exposure_class"], "highway_major"]}
                paint={{ "line-color": "#ffffff", "line-width": 2.6, "line-opacity": 0.95 }}
              />
            </Source>
          ) : null}
        </Map>
      ) : null}

      {bands.length > 0 ? (
        <HazardLegend
          bands={bands}
          stretched={stretchable}
          spread={stretchable ? spread : null}
          hasUnknown={hasUnknown}
        />
      ) : null}

      {exposureVisible ? (
        <p className="pointer-events-none absolute bottom-2 right-2 z-10 rounded bg-[var(--background)]/80 px-1.5 py-0.5 text-[10px] text-[var(--muted)]">
          Exposure {meta?.exposure?.attribution ?? "© OpenStreetMap contributors"} ·{" "}
          {meta?.exposure?.licence ?? "ODbL 1.0"}
        </p>
      ) : null}

      {error ? (
        <div className="absolute inset-x-3 top-3 rounded-md border border-[var(--danger)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--danger)]">
          {error}
        </div>
      ) : null}
      {!ready && !error ? (
        <div className="absolute inset-0 grid place-items-center bg-[var(--background)]/70 text-sm text-[var(--muted)]">
          Building the 5 m LiDAR terrain mesh…
        </div>
      ) : null}
      {drawingArea ? (
        <div className="absolute left-3 top-3 z-10 max-w-xs rounded-md border border-[#8bd5ee] bg-[var(--panel)]/95 p-2 text-[11px] shadow-lg">
          <p className="font-semibold text-[#8bd5ee]">Draw condition applicability</p>
          <p className="mt-1 text-[var(--muted)]">Click at least three WGS84 vertices on the map. The polygon applies at baked grid-cell centres.</p>
          <div className="mt-2 flex flex-wrap gap-1">
            <button type="button" disabled={draftVertices.length === 0} onClick={() => setDraftVertices((vertices) => vertices.slice(0, -1))} className="rounded border border-[var(--border)] px-2 py-1 disabled:opacity-40">Undo</button>
            <button
              type="button"
              disabled={draftVertices.length < 3}
              onClick={() => {
                const ring = [...draftVertices, draftVertices[0]];
                onDrawnArea?.({ type: "Polygon", coordinates: [ring] });
                setDraftVertices([]);
              }}
              className="rounded bg-[#8bd5ee] px-2 py-1 font-semibold text-[#101415] disabled:opacity-40"
            >
              Finish area
            </button>
            <button type="button" onClick={() => { setDraftVertices([]); onCancelDrawing?.(); }} className="rounded border border-[var(--border)] px-2 py-1 text-[var(--muted)]">Cancel</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function HazardLegend({
  bands,
  stretched,
  spread,
  hasUnknown,
}: {
  bands: Band[];
  stretched: boolean;
  spread: { low: number; high: number } | null;
  hasUnknown: boolean;
}) {
  return (
    <div className="pointer-events-none absolute bottom-2 left-2 z-10 rounded-md border border-[var(--border)] bg-[var(--background)]/88 px-2 py-1.5 text-[10px] leading-tight shadow-lg backdrop-blur">
      <p className="font-semibold text-[var(--foreground)]">Zone hazard index</p>
      <p className="text-[var(--muted)]">
        {stretched && spread
          ? `Stretched to this run · ${spread.low.toFixed(0)}–${spread.high.toFixed(0)}`
          : "Absolute bands · comparable between runs"}
      </p>
      {/* The bar is the fill ramp itself, so the legend shows the same continuous
          climb the map draws; the rows below name where the bands fall on it. */}
      <div
        className="mt-1 h-2 w-full rounded-[2px] border border-[#0b0f10]"
        style={{ background: legendGradient(bands, stretched && spread ? spread : null) }}
      />
      <ul className="mt-1 space-y-0.5">
        {bands.map((band, index) => (
          <li key={band.band} className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-4 rounded-[2px] border border-[#0b0f10]"
              style={{ background: band.color }}
            />
            {/* In stretched mode a colour no longer means a band: violet is simply
                this run's lowest zone, whatever its absolute severity. Showing the
                band names here would be the exact misreading the mode invites. */}
            {stretched && spread ? (
              <span className="font-mono text-[var(--foreground)]">
                {(spread.low + (index * (spread.high - spread.low)) / (bands.length - 1)).toFixed(0)}
              </span>
            ) : (
              <>
                <span className="text-[var(--foreground)]">{band.band}</span>
                <span className="font-mono text-[var(--muted)]">
                  {index === 0 ? 0 : bands[index - 1].upper}–{band.upper}
                </span>
              </>
            )}
          </li>
        ))}
        {hasUnknown ? (
          <li className="flex items-center gap-1.5">
            <span
              className="inline-block h-2.5 w-4 rounded-[2px] border border-dashed border-[#f4f7f4]"
              style={{ background: UNKNOWN_COLOR }}
            />
            <span className="text-[var(--muted)]">No index — not a low one</span>
          </li>
        ) : null}
      </ul>
      <p className="mt-1 max-w-[13rem] text-[var(--muted)]">
        {stretched
          ? "Rescaled to this run only — a colour here is not a severity band and is not comparable with another run."
          : "Cooler and lighter is lower; bright red is highest. Uncalibrated relative index, not a danger rating."}
      </p>
    </div>
  );
}

/**
 * Both modes interpolate the API's band colours rather than stepping between
 * them, so a zone at 58 and a zone at 62 look almost the same instead of
 * snapping across a hard edge — the index is continuous, and five flat blocks
 * implied a precision the model does not have. Absolute mode anchors each band
 * colour at its midpoint on the fixed 0–100 scale; stretched mode spreads the
 * same five colours across this run's own range.
 */
function fillColor(bands: Band[], spread: { low: number; high: number } | null): never {
  if (bands.length < 2) {
    return ["coalesce", ["get", "hazard_color"], UNKNOWN_COLOR] as never;
  }
  return [
    "case",
    ["==", ["typeof", ["get", "hazard_index"]], "number"],
    ["interpolate", ["linear"], ["get", "hazard_index"], ...rampStops(bands, spread)],
    UNKNOWN_COLOR,
  ] as never;
}

/** Value/colour pairs for the fill ramp, and for the legend gradient beside it. */
function rampStops(bands: Band[], spread: { low: number; high: number } | null): (number | string)[] {
  if (spread) {
    const step = (spread.high - spread.low) / (bands.length - 1);
    return bands.flatMap((band, index) => [spread.low + index * step, band.color]);
  }
  return bands.flatMap((band, index) => [
    ((index === 0 ? 0 : bands[index - 1].upper) + band.upper) / 2,
    band.color,
  ]);
}

/** The fill ramp as CSS, built from the same stops, so the two cannot drift. */
function legendGradient(bands: Band[], spread: { low: number; high: number } | null): string {
  if (bands.length < 2) return UNKNOWN_COLOR;
  const stops = rampStops(bands, spread);
  const low = spread ? spread.low : 0;
  const high = spread ? spread.high : 100;
  const parts: string[] = [];
  for (let index = 0; index < stops.length; index += 2) {
    const position = ((Number(stops[index]) - low) / (high - low)) * 100;
    parts.push(`${stops[index + 1]} ${position.toFixed(1)}%`);
  }
  return `linear-gradient(to right, ${parts.join(", ")})`;
}

/** Map each band to an ordinal-scaled value by matching the API's own colours. */
function bandScale(bands: Band[], values: number[], fallback: number): never {
  if (bands.length === 0) return fallback as never;
  const cases = bands.flatMap((band, index) => [band.color, values[index] ?? fallback]);
  return ["match", ["coalesce", ["get", "hazard_color"], ""], ...cases, fallback] as never;
}

/** Push everything but the selected zone back, so the selection reads instantly. */
function dimmed(active: unknown, selectedZone: string | null, faded: number): never {
  if (!selectedZone) return active as never;
  return ["case", ["==", ["get", "zone_id"], selectedZone], active, faded] as never;
}

function emptyCollection(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function wrap(
  geometries: (GeoJSON.Polygon | GeoJSON.MultiPolygon | GeoJSON.LineString)[] | undefined,
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (geometries ?? []).map((geometry) => ({ type: "Feature", geometry, properties: {} })),
  };
}

function polygonCollection(polygon: GeoJSON.Polygon | null): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: polygon ? [{ type: "Feature", geometry: polygon, properties: { role: "condition_scope" } }] : [],
  };
}

function draftCollection(vertices: [number, number][]): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  if (vertices.length > 0) {
    features.push({
      type: "Feature",
      geometry: { type: "MultiPoint", coordinates: vertices },
      properties: { role: "vertices" },
    });
  }
  if (vertices.length >= 2) {
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: vertices },
      properties: { role: "edge" },
    });
  }
  if (vertices.length >= 3) {
    features.push({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [[...vertices, vertices[0]]] },
      properties: { role: "preview" },
    });
  }
  return { type: "FeatureCollection", features };
}
