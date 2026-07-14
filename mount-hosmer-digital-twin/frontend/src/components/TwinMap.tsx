"use client";

import { useEffect, useRef, useState } from "react";
import type { Map as MapLibreMap } from "maplibre-gl";
import { V1 } from "@/lib/apiV1";
import type { Analysis, Simulation } from "@/lib/apiV1";

export type CameraPreset = "overview" | "north" | "south" | "top";

/** MapLibre pins an image source by its four corners, clockwise from top-left. */
export type ImageCorners = [
  [number, number],
  [number, number],
  [number, number],
  [number, number],
];

type Props = {
  /** Terrain layer draped over the mesh. Null shows bare hillshade. */
  overlayId: string | null;
  /** The four corners of the AOI, as MapLibre's image source requires. */
  overlayCoordinates: ImageCorners | null;
  overlayOpacity: number;
  exaggeration: number;
  analysis: Analysis | null;
  simulation: Simulation | null;
  camera: CameraPreset;
  onZoneClick?: (zoneId: string) => void;
};

const CAMERAS: Record<CameraPreset, { pitch: number; bearing: number; zoom: number }> = {
  overview: { pitch: 62, bearing: -25, zoom: 11.6 },
  north: { pitch: 70, bearing: 180, zoom: 12.2 },
  south: { pitch: 70, bearing: 0, zoom: 12.2 },
  top: { pitch: 0, bearing: 0, zoom: 11.4 },
};

const CENTRE: [number, number] = [-115.0113, 49.6136];

/** Colour a zone by its release score, not by rank. Rank hides how close a 90 is to a 40. */
const ZONE_COLOR: unknown = [
  "interpolate",
  ["linear"],
  ["get", "estimated_release_score"],
  55, "#f2d16b",
  70, "#e08a4a",
  85, "#c23b35",
];

export function TwinMap({
  overlayId,
  overlayCoordinates,
  overlayOpacity,
  exaggeration,
  analysis,
  simulation,
  camera,
  onZoneClick,
}: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // --- Build the map once -----------------------------------------------------
  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!container.current || mapRef.current) return;
      try {
        const maplibre = await import("maplibre-gl");
        if (cancelled || !container.current) return;

        const map = new maplibre.Map({
          container: container.current,
          center: CENTRE,
          zoom: CAMERAS.overview.zoom,
          pitch: CAMERAS.overview.pitch,
          bearing: CAMERAS.overview.bearing,
          maxPitch: 85,
          style: {
            version: 8,
            sources: {
              // The whole point of the LiDAR fix. Draped over the 30 m Copernicus
              // DEM this mountain is a smooth lump; at 5 m the gullies and ridges
              // that decide where an avalanche starts are actually visible.
              terrain: {
                type: "raster-dem",
                tiles: [`${V1}/terrain/tiles/{z}/{x}/{y}.png`],
                tileSize: 256,
                minzoom: 8,
                maxzoom: 16,
                encoding: "mapbox",
              },
            },
            layers: [
              { id: "sky", type: "background", paint: { "background-color": "#0b0f10" } },
              {
                id: "hillshade",
                type: "hillshade",
                source: "terrain",
                paint: {
                  "hillshade-shadow-color": "#0a0e0f",
                  "hillshade-highlight-color": "#dfe8e0",
                  "hillshade-exaggeration": 0.55,
                },
              },
            ],
          },
        });

        mapRef.current = map;
        map.addControl(new maplibre.NavigationControl({ visualizePitch: true }), "top-right");
        map.addControl(new maplibre.ScaleControl({ unit: "metric" }), "bottom-left");

        map.on("error", (event) => {
          const message = (event as { error?: { message?: string } }).error?.message ?? "";
          // Tiles outside the 12 x 12 km AOI legitimately have no data.
          if (message.includes("404")) return;
          setError(message || "The map failed to load.");
        });

        map.on("load", () => {
          map.setTerrain({ source: "terrain", exaggeration });

          // Empty sources up front, so the render effects below only ever setData.
          for (const id of ["zones", "runout", "envelope", "paths", "assets"]) {
            map.addSource(id, { type: "geojson", data: emptyCollection() });
          }

          map.addLayer({
            id: "envelope-fill",
            type: "fill",
            source: "envelope",
            paint: { "fill-color": "#e16d5a", "fill-opacity": 0.14 },
          });
          map.addLayer({
            id: "envelope-line",
            type: "line",
            source: "envelope",
            paint: { "line-color": "#e16d5a", "line-width": 1, "line-dasharray": [2, 2], "line-opacity": 0.7 },
          });
          map.addLayer({
            id: "runout-fill",
            type: "fill",
            source: "runout",
            paint: { "fill-color": "#e16d5a", "fill-opacity": 0.42 },
          });
          // The runout is drawn in red, and so is most of the hazard ramp it sits on
          // top of. Without an outline the two blend and the runout disappears into
          // the layer beneath it, which is the one thing it must never do.
          map.addLayer({
            id: "runout-line",
            type: "line",
            source: "runout",
            paint: { "line-color": "#ffd9d2", "line-width": 1.4 },
          });
          map.addLayer({
            id: "paths-line",
            type: "line",
            source: "paths",
            paint: { "line-color": "#ffffff", "line-width": 1.6, "line-opacity": 0.75 },
          });
          map.addLayer({
            id: "zones-fill",
            type: "fill",
            source: "zones",
            paint: { "fill-color": ZONE_COLOR as never, "fill-opacity": 0.6 },
          });
          map.addLayer({
            id: "zones-line",
            type: "line",
            source: "zones",
            paint: { "line-color": "#fff3d6", "line-width": 1.2 },
          });
          map.addLayer({
            id: "assets-point",
            type: "circle",
            source: "assets",
            paint: {
              "circle-radius": 5,
              "circle-color": "#8fbc8f",
              "circle-stroke-color": "#101415",
              "circle-stroke-width": 1.5,
            },
          });

          map.on("click", "zones-fill", (event) => {
            const feature = event.features?.[0];
            const zoneId = feature?.properties?.zone_id;
            if (typeof zoneId === "string") onZoneClick?.(zoneId);
          });
          map.on("mouseenter", "zones-fill", () => {
            map.getCanvas().style.cursor = "pointer";
          });
          map.on("mouseleave", "zones-fill", () => {
            map.getCanvas().style.cursor = "";
          });

          setReady(true);
        });
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();

    return () => {
      cancelled = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
    // Built once. Everything else is driven by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Terrain exaggeration ---------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setTerrain({ source: "terrain", exaggeration });
  }, [exaggeration, ready]);

  // --- Camera -----------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const target = CAMERAS[camera];
    map.easeTo({ center: CENTRE, ...target, duration: 900 });
  }, [camera, ready]);

  // --- The draped model layer -------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    if (map.getLayer("overlay")) map.removeLayer("overlay");
    if (map.getSource("overlay")) map.removeSource("overlay");
    if (!overlayId || !overlayCoordinates) return;

    map.addSource("overlay", {
      type: "image",
      url: `${V1}/layers/${overlayId}/preview`,
      coordinates: overlayCoordinates,
    });
    map.addLayer(
      {
        id: "overlay",
        type: "raster",
        source: "overlay",
        paint: { "raster-opacity": overlayOpacity, "raster-fade-duration": 200 },
      },
      // Under the model results: the release zones and the runout must never be
      // hidden by a decorative raster.
      map.getLayer("envelope-fill") ? "envelope-fill" : undefined,
    );
  }, [overlayId, overlayCoordinates, ready, overlayOpacity]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready || !map.getLayer("overlay")) return;
    map.setPaintProperty("overlay", "raster-opacity", overlayOpacity);
  }, [overlayOpacity, ready]);

  // --- Release zones ----------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const source = map.getSource("zones") as maplibregl.GeoJSONSource | undefined;
    source?.setData(analysis ? (analysis.release_zones as never) : emptyCollection());
  }, [analysis, ready]);

  // --- Runout, envelope, paths, exposed assets --------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;

    const runout = map.getSource("runout") as maplibregl.GeoJSONSource | undefined;
    const envelope = map.getSource("envelope") as maplibregl.GeoJSONSource | undefined;
    const paths = map.getSource("paths") as maplibregl.GeoJSONSource | undefined;
    const assets = map.getSource("assets") as maplibregl.GeoJSONSource | undefined;

    if (!simulation) {
      runout?.setData(emptyCollection());
      envelope?.setData(emptyCollection());
      paths?.setData(emptyCollection());
      assets?.setData(emptyCollection());
      return;
    }

    runout?.setData(wrap(simulation.runout.runout_polygons));
    // The envelope is the "could run further" band. It is drawn UNDER the core and
    // dashed, so it never reads as a hard boundary -- because it is not one.
    envelope?.setData(wrap(simulation.runout.uncertainty_polygons));
    paths?.setData(wrap(simulation.runout.main_paths));
    assets?.setData(emptyCollection());
  }, [simulation, ready]);

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-[var(--border)]">
      <div ref={container} className="h-full w-full" />
      {error ? (
        <div className="absolute inset-x-3 top-3 rounded-md border border-[var(--danger)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--danger)]">
          {error}
        </div>
      ) : null}
      {!ready && !error ? (
        <div className="absolute inset-0 grid place-items-center bg-[var(--background)]/70 text-sm text-[var(--muted)]">
          Building the 1 m LiDAR terrain mesh…
        </div>
      ) : null}
    </div>
  );
}

function emptyCollection(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function wrap(geometries: (GeoJSON.Polygon | GeoJSON.LineString)[] | undefined): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (geometries ?? []).map((geometry) => ({
      type: "Feature" as const,
      geometry,
      properties: {},
    })),
  };
}
