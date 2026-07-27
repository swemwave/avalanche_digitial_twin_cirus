"use client";

import { useEffect, useRef, useState } from "react";
import type { Map as MapLibreMap } from "maplibre-gl";
import { imageryTileUrlTemplate, tileUrlTemplate, type AssessResult, type TwinMeta } from "@/lib/twin";

export type CameraPreset = "overview" | "north" | "south" | "top";
export type SurfaceView = "natural" | "hillshade";

const CAMERAS: Record<CameraPreset, { pitch: number; bearing: number; zoom: number }> = {
  overview: { pitch: 62, bearing: -25, zoom: 11.6 },
  north: { pitch: 70, bearing: 180, zoom: 12.2 },
  south: { pitch: 70, bearing: 0, zoom: 12.2 },
  top: { pitch: 0, bearing: 0, zoom: 11.4 },
};

/** Colour a zone by its release score, not by rank — rank hides how close a 90 is to a 40. */
const ZONE_COLOR: unknown = [
  "interpolate",
  ["linear"],
  ["get", "estimated_release_score"],
  55, "#f2d16b",
  70, "#e08a4a",
  85, "#c23b35",
];
// These props are passed to the Stage3Map component, which renders the 3D map of the mountain and the modelled release zones and runout. 
// The map uses MapLibre GL JS to render the terrain and imagery tiles, and to display the zones, runout, envelope, and paths as GeoJSON layers.
type Props = {
  meta: TwinMeta | null;
  result: AssessResult | null;
  exaggeration: number;
  camera: CameraPreset;
  surface: SurfaceView;
  onZoneClick?: (zoneId: string) => void;
};

export function Stage3Map({ meta, result, exaggeration, camera, surface, onZoneClick }: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const centre: [number, number] = meta?.center_wgs84 ?? [-115.0113, 49.6136];

  // --- Build the map once (after meta is known) ------------------------------
  useEffect(() => {
    if (!meta) return;
    let cancelled = false;

    (async () => {
      if (!container.current || mapRef.current) return; // already built
      try {
        const maplibre = await import("maplibre-gl");
        if (cancelled || !container.current) return;

        const map = new maplibre.Map({
          container: container.current,
          center: centre,
          zoom: CAMERAS.overview.zoom,
          pitch: CAMERAS.overview.pitch,
          bearing: CAMERAS.overview.bearing,
          maxPitch: 85,
          style: {
            version: 8,
            sources: {
              // A real 3D mesh from the baked 5 m LiDAR terrain-RGB tiles. No live
              // rasterio: these are static PNGs written once by bake.py.
              terrain: {
                type: "raster-dem",
                tiles: [tileUrlTemplate()],
                tileSize: meta.tiles.tile_size,
                minzoom: meta.tiles.min_zoom,
                maxzoom: meta.tiles.max_zoom,
                encoding: "mapbox",
              },
              ...(meta.imagery
                ? {
                    imagery: {
                      type: "raster" as const,
                      tiles: [imageryTileUrlTemplate()],
                      tileSize: meta.imagery.tile_size,
                      minzoom: meta.imagery.min_zoom,
                      maxzoom: meta.imagery.max_zoom,
                      bounds: meta.aoi_bbox_wgs84,
                    },
                  }
                : {}),
            },
            // The layers are the visual elements of the map. The sky layer is a solid background color. 
            // The hillshade layer is the shaded relief of the terrain, which is only visible when the surface view is set to "hillshade". 
            // The zones, runout, envelope, and paths layers are the modelled release zones and runout, which are always visible.
            layers: [
              { id: "sky", type: "background", paint: { "background-color": "#0b0f10" } },
              ...(meta.imagery
                ? [
                    {
                      id: "natural-surface" as const,
                      type: "raster" as const,
                      source: "imagery",
                      layout: {
                        visibility: (surface === "natural" ? "visible" : "none") as
                          | "visible"
                          | "none",
                      },
                      paint: { "raster-opacity": 1, "raster-fade-duration": 0 },
                    },
                  ]
                : []),
              {
                id: "hillshade",
                type: "hillshade",
                source: "terrain",
                layout: { visibility: surface === "hillshade" ? "visible" : "none" },
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
          if (message.includes("404")) return; // tiles outside the AOI legitimately 404
          setError(message || "The map failed to load.");
        });
        // This event listener is called when the map has finished loading. It sets the terrain exaggeration, adds the sources and layers for the zones, runout, envelope, and paths, 
        // and sets up the click and hover interactions for the zones. Finally, it sets the ready state to true.
        map.on("load", () => {
          map.setTerrain({ source: "terrain", exaggeration });

          for (const id of ["zones", "runout", "envelope", "paths"]) {
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

          map.on("click", "zones-fill", (event) => {
            const zoneId = event.features?.[0]?.properties?.zone_id;
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
    // Built once, when meta arrives. Everything else is driven by the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta]);

  // --- Terrain exaggeration ---------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.setTerrain({ source: "terrain", exaggeration });
  }, [exaggeration, ready]);

  // --- Visible surface -------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    if (map.getLayer("natural-surface")) {
      map.setLayoutProperty("natural-surface", "visibility", surface === "natural" ? "visible" : "none");
    }
    map.setLayoutProperty("hillshade", "visibility", surface === "hillshade" ? "visible" : "none");
  }, [surface, ready]);

  // --- Camera -----------------------------------------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    map.easeTo({ center: centre, ...CAMERAS[camera], duration: 900 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, ready]);

  // --- Result: zones, runout, envelope, paths ---------------------------------
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    // These const variables are the sources for the zones, runout, envelope, and paths layers. They are used to update the data for these layers when the result changes.
    const zones = map.getSource("zones") as maplibregl.GeoJSONSource | undefined;
    const runout = map.getSource("runout") as maplibregl.GeoJSONSource | undefined;
    const envelope = map.getSource("envelope") as maplibregl.GeoJSONSource | undefined;
    const paths = map.getSource("paths") as maplibregl.GeoJSONSource | undefined;
    // If there is no result, clear the sources. Otherwise, set the sources with the new result data. 
    // The zones source is set with the release zones from the result. The runout source is set with the runout polygons from the result. 
    // The envelope source is set with the uncertainty polygons from the result. The paths source is set with the main paths from the result.
    if (!result) {
      zones?.setData(emptyCollection());
      runout?.setData(emptyCollection());
      envelope?.setData(emptyCollection());
      paths?.setData(emptyCollection());
      return;
    }

    zones?.setData(result.release_zones as never);
    runout?.setData(wrap(result.runout.runout_polygons));
    envelope?.setData(wrap(result.runout.uncertainty_polygons));
    paths?.setData(wrap(result.runout.main_paths));
  }, [result, ready]);

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
          Building the 5 m LiDAR terrain mesh…
        </div>
      ) : null}
    </div>
  );
}

function emptyCollection(): GeoJSON.FeatureCollection {
  return { type: "FeatureCollection", features: [] };
}

function wrap(
  geometries: (GeoJSON.Polygon | GeoJSON.MultiPolygon | GeoJSON.LineString)[] | undefined,
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: (geometries ?? []).map((geometry) => ({
      type: "Feature" as const,
      geometry,
      properties: {},
    })),
  };
}
