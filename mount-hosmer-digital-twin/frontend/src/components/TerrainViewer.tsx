"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE_URL,
  type OsmPayload,
  type TerrainLayer,
  type TerrainLayersPayload,
  fetchJson,
} from "@/lib/api";

type LoadedState = {
  terrain?: TerrainLayersPayload;
  osm?: OsmPayload;
  loading: boolean;
  error?: string;
};

type VisibilityState = Record<string, boolean>;
type OpacityState = Record<string, number>;

const DEFAULT_VISIBLE = new Set(["hillshade", "slope", "landcover", "terrain_susceptibility"]);
const RISK_CONTROL_LAYER_IDS = ["terrain_susceptibility", "slope", "landcover"] as const;
const CONTEXT_LAYER_IDS = new Set(["hillshade"]);

function apiUrl(path: string) {
  return path.startsWith("http") ? path : `${API_BASE_URL}${path}`;
}

function formatValue(value: number | null | undefined, digits = 1) {
  return typeof value === "number" ? value.toFixed(digits) : "n/a";
}

function layerSort(layer: TerrainLayer) {
  const order = ["hillshade", "terrain_susceptibility", "slope", "landcover", "elevation", "aspect"];
  return order.indexOf(layer.id);
}

function riskControlLabel(layer: TerrainLayer) {
  if (layer.id === "terrain_susceptibility") {
    return {
      title: "Prototype risk areas",
      help: "Primary avalanche terrain susceptibility overlay.",
    };
  }
  if (layer.id === "slope") {
    return {
      title: "Slope steepness",
      help: "Shows where terrain is in steeper avalanche-relevant bands.",
    };
  }
  if (layer.id === "landcover") {
    return {
      title: "Open vs forested terrain",
      help: "Helps separate exposed/open slopes from forested cover.",
    };
  }
  return { title: layer.title, help: layer.group };
}

export function TerrainViewer() {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<unknown>(null);
  const [state, setState] = useState<LoadedState>({ loading: true });
  const [visible, setVisible] = useState<VisibilityState>({});
  const [opacity, setOpacity] = useState<OpacityState>({});
  const [showInfrastructure, setShowInfrastructure] = useState(true);
  const [cursor, setCursor] = useState<string>("Move over map");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [terrain, osm] = await Promise.all([
          fetchJson<TerrainLayersPayload>("/api/terrain/layers"),
          fetchJson<OsmPayload>("/api/terrain/osm"),
        ]);
        if (cancelled) {
          return;
        }
        const nextVisible: VisibilityState = {};
        const nextOpacity: OpacityState = {};
        for (const layer of terrain.layers) {
          nextVisible[layer.id] = DEFAULT_VISIBLE.has(layer.id);
          nextOpacity[layer.id] = layer.opacity;
        }
        setVisible(nextVisible);
        setOpacity(nextOpacity);
        setState({ terrain, osm, loading: false });
      } catch (err) {
        if (!cancelled) {
          setState({ loading: false, error: err instanceof Error ? err.message : "Failed to load terrain layers" });
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const layers = useMemo(() => [...(state.terrain?.layers ?? [])].sort((a, b) => layerSort(a) - layerSort(b)), [state.terrain]);
  const riskControlLayers = useMemo(
    () => RISK_CONTROL_LAYER_IDS.map((id) => layers.find((layer) => layer.id === id)).filter((layer): layer is TerrainLayer => Boolean(layer)),
    [layers],
  );
  const hiddenModelLayers = useMemo(() => layers.filter((layer) => !RISK_CONTROL_LAYER_IDS.includes(layer.id as (typeof RISK_CONTROL_LAYER_IDS)[number]) && !CONTEXT_LAYER_IDS.has(layer.id)), [layers]);
  const riskLayer = layers.find((layer) => layer.id === "terrain_susceptibility");
  const activeLegends = layers.filter((layer) => visible[layer.id] && RISK_CONTROL_LAYER_IDS.includes(layer.id as (typeof RISK_CONTROL_LAYER_IDS)[number]));

  useEffect(() => {
    if (!mapContainer.current || !state.terrain || mapRef.current) {
      return;
    }
    let removed = false;
    async function setupMap() {
      const maplibre = await import("maplibre-gl");
      if (removed || !mapContainer.current || !state.terrain) {
        return;
      }
      const bounds = state.terrain.layers[0]?.bounds_wgs84;
      const center: [number, number] = bounds
        ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]
        : [-115.011, 49.614];
      const map = new maplibre.Map({
        container: mapContainer.current,
        style: {
          version: 8,
          sources: {
            osmBase: {
              type: "raster",
              tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
              tileSize: 256,
              attribution: "OpenStreetMap",
            },
          },
          layers: [{ id: "osmBase", type: "raster", source: "osmBase", paint: { "raster-opacity": 0.45 } }],
        },
        center,
        zoom: 10,
      });
      mapRef.current = map;
      map.addControl(new maplibre.NavigationControl({ visualizePitch: false }), "top-right");
      map.on("mousemove", (event) => {
        setCursor(`${event.lngLat.lng.toFixed(5)}, ${event.lngLat.lat.toFixed(5)}`);
      });
      map.on("load", () => {
        for (const layer of state.terrain?.layers ?? []) {
          const sourceId = `src-${layer.id}`;
          const layerId = `layer-${layer.id}`;
          if (!map.getSource(sourceId)) {
            map.addSource(sourceId, {
              type: "image",
              url: apiUrl(layer.preview_url),
              coordinates: layer.coordinates,
            });
            map.addLayer({
              id: layerId,
              type: "raster",
              source: sourceId,
              layout: { visibility: visible[layer.id] ? "visible" : "none" },
              paint: { "raster-opacity": opacity[layer.id] ?? layer.opacity },
            });
          }
        }
        if (state.osm && !map.getSource("infrastructure")) {
          map.addSource("infrastructure", { type: "geojson", data: state.osm });
          map.addLayer({
            id: "infra-buildings",
            type: "fill",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "buildings"],
            paint: { "fill-color": "#e16d5a", "fill-opacity": 0.5 },
          });
          map.addLayer({
            id: "infra-roads",
            type: "line",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "roads"],
            paint: { "line-color": "#f2c14e", "line-width": 2.2 },
          });
          map.addLayer({
            id: "infra-trails",
            type: "line",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "trails"],
            paint: { "line-color": "#8fbc8f", "line-width": 1.8, "line-dasharray": [2, 2] },
          });
          map.addLayer({
            id: "infra-rail",
            type: "line",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "railways"],
            paint: { "line-color": "#b78cff", "line-width": 2 },
          });
          map.addLayer({
            id: "infra-power",
            type: "line",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "power"],
            paint: { "line-color": "#75bfff", "line-width": 1.8 },
          });
          map.addLayer({
            id: "infra-water",
            type: "line",
            source: "infrastructure",
            filter: ["==", ["get", "dt_category"], "waterways"],
            paint: { "line-color": "#5cc8ff", "line-width": 2 },
          });
          map.addLayer({
            id: "infra-points",
            type: "circle",
            source: "infrastructure",
            filter: ["==", ["geometry-type"], "Point"],
            paint: { "circle-color": "#ffffff", "circle-radius": 3, "circle-stroke-color": "#101415", "circle-stroke-width": 1 },
          });
        }
        if (bounds) {
          map.fitBounds(
            [
              [bounds[0], bounds[1]],
              [bounds[2], bounds[3]],
            ],
            { padding: 38, duration: 0 },
          );
        }
      });
    }
    setupMap();
    return () => {
      removed = true;
      const map = mapRef.current as { remove?: () => void } | null;
      map?.remove?.();
      mapRef.current = null;
    };
  }, [state.terrain, state.osm]);

  useEffect(() => {
    const map = mapRef.current as { getLayer?: (id: string) => unknown; setLayoutProperty?: (...args: unknown[]) => void } | null;
    if (!map?.getLayer) {
      return;
    }
    for (const [layerId, isVisible] of Object.entries(visible)) {
      const mapLayerId = `layer-${layerId}`;
      if (map.getLayer(mapLayerId)) {
        map.setLayoutProperty?.(mapLayerId, "visibility", isVisible ? "visible" : "none");
      }
    }
  }, [visible]);

  useEffect(() => {
    const map = mapRef.current as { getLayer?: (id: string) => unknown; setPaintProperty?: (...args: unknown[]) => void } | null;
    if (!map?.getLayer) {
      return;
    }
    for (const [layerId, layerOpacity] of Object.entries(opacity)) {
      const mapLayerId = `layer-${layerId}`;
      if (map.getLayer(mapLayerId)) {
        map.setPaintProperty?.(mapLayerId, "raster-opacity", layerOpacity);
      }
    }
  }, [opacity]);

  useEffect(() => {
    const map = mapRef.current as { getLayer?: (id: string) => unknown; setLayoutProperty?: (...args: unknown[]) => void } | null;
    if (!map?.getLayer) {
      return;
    }
    for (const id of ["infra-buildings", "infra-roads", "infra-trails", "infra-rail", "infra-power", "infra-water", "infra-points"]) {
      if (map.getLayer(id)) {
        map.setLayoutProperty?.(id, "visibility", showInfrastructure ? "visible" : "none");
      }
    }
  }, [showInfrastructure]);

  if (state.loading) {
    return <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-6 text-[var(--muted)]">Generating terrain layers...</section>;
  }
  if (state.error) {
    return <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-6 text-[#ffd8d1]">{state.error}</section>;
  }

  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <div>
            <h2 className="text-2xl font-semibold">Terrain And Prototype Susceptibility</h2>
            <p className="mt-1 text-sm text-[var(--muted)]">
              {state.terrain?.terrain_metadata.terrain_source_selected}. {state.terrain?.terrain_metadata.terrain_source_reason}
            </p>
          </div>
          <div className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 font-mono text-sm text-[var(--muted)]">
            {cursor}
          </div>
        </div>

        <div className="relative h-[680px] min-h-[520px] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--panel-strong)]">
          <div ref={mapContainer} className="h-full w-full" />
          <div className="absolute bottom-3 left-3 max-w-xl rounded-md border border-[#6f3b34] bg-[#271a18]/95 px-3 py-2 text-xs leading-relaxed text-[#ffd8d1]">
            {state.terrain?.terrain_metadata.disclaimer}
          </div>
        </div>
      </div>

      <aside className="flex flex-col gap-4">
        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Risk View Controls</h3>
          <p className="mt-1 text-sm text-[var(--muted)]">
            These are the map layers needed to understand where the prototype highlights avalanche-prone terrain.
          </p>
          <div className="mt-4 space-y-4">
            {riskControlLayers.map((layer) => {
              const copy = riskControlLabel(layer);
              return (
              <div key={layer.id} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
                <label className="flex cursor-pointer items-center justify-between gap-3">
                  <span>
                    <span className="block font-medium">{copy.title}</span>
                    <span className="text-xs text-[var(--muted)]">{copy.help}</span>
                  </span>
                  <input
                    aria-label={`Toggle ${copy.title}`}
                    type="checkbox"
                    checked={Boolean(visible[layer.id])}
                    onChange={(event) => setVisible((current) => ({ ...current, [layer.id]: event.target.checked }))}
                    className="h-5 w-5 accent-[var(--accent)]"
                  />
                </label>
                <label className="mt-3 block text-xs text-[var(--muted)]">
                  Strength {Math.round((opacity[layer.id] ?? layer.opacity) * 100)}%
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={opacity[layer.id] ?? layer.opacity}
                    onChange={(event) => setOpacity((current) => ({ ...current, [layer.id]: Number(event.target.value) }))}
                    className="mt-2 w-full accent-[var(--accent)]"
                  />
                </label>
              </div>
            );
            })}
            <label className="flex cursor-pointer items-center justify-between gap-3 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <span>
                <span className="block font-medium">Exposure context</span>
                <span className="text-xs text-[var(--muted)]">Roads, trails, rail, power, water, buildings</span>
              </span>
              <input
                aria-label="Toggle exposure context"
                type="checkbox"
                checked={showInfrastructure}
                onChange={(event) => setShowInfrastructure(event.target.checked)}
                className="h-5 w-5 accent-[var(--accent)]"
              />
            </label>
          </div>
          <div className="mt-4 rounded-md border border-[var(--border)] bg-[#111718] p-3 text-xs leading-relaxed text-[var(--muted)]">
            Hillshade is kept on as the terrain backdrop. Elevation and aspect are still used by the prototype model, but they are not shown as separate controls because they are secondary inputs for interpreting risk on the map.
          </div>
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Legend</h3>
          <div className="mt-3 space-y-4">
            {activeLegends.length ? (
              activeLegends.map((layer) => (
                <div key={layer.id}>
                  <div className="mb-2 text-sm font-medium">{layer.title}</div>
                  <div className="grid grid-cols-1 gap-2 text-xs">
                    {layer.legend.map((item) => (
                      <div key={`${layer.id}-${item.label}`} className="flex items-center gap-2 text-[var(--muted)]">
                        <span className="h-3 w-6 rounded-sm border border-white/20" style={{ backgroundColor: item.color }} />
                        <span>{item.label}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <p className="text-sm text-[var(--muted)]">Turn on a raster layer to see its legend.</p>
            )}
          </div>
        </div>

        <div className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4">
          <h3 className="text-lg font-semibold text-[#ffd8d1]">Experimental Terrain Susceptibility</h3>
          <p className="mt-2 text-sm leading-relaxed text-[#ffd8d1]">{state.terrain?.susceptibility.disclaimer}</p>
          <div className="mt-4 grid grid-cols-3 gap-2 text-sm">
            <div>
              <div className="text-xs uppercase text-[#efb6ac]">Min</div>
              <div className="font-mono">{formatValue(state.terrain?.susceptibility.stats.min)}</div>
            </div>
            <div>
              <div className="text-xs uppercase text-[#efb6ac]">Mean</div>
              <div className="font-mono">{formatValue(state.terrain?.susceptibility.stats.mean)}</div>
            </div>
            <div>
              <div className="text-xs uppercase text-[#efb6ac]">Max</div>
              <div className="font-mono">{formatValue(state.terrain?.susceptibility.stats.max)}</div>
            </div>
          </div>
          {riskLayer?.download_url ? (
            <a
              href={apiUrl(riskLayer.download_url)}
              className="mt-4 inline-flex rounded-md border border-[#9a5a4f] px-3 py-2 text-sm text-[#ffd8d1] hover:bg-[#3a211e]"
            >
              Download susceptibility GeoTIFF
            </a>
          ) : null}
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Factor Explanation</h3>
          {hiddenModelLayers.length ? (
            <p className="mt-1 text-sm text-[var(--muted)]">
              Additional hidden model inputs: {hiddenModelLayers.map((layer) => layer.title).join(", ")}.
            </p>
          ) : null}
          <div className="mt-3 space-y-3">
            {state.terrain?.susceptibility.factors.map((factor) => (
              <div key={factor.factor} className="border-b border-[var(--border)] pb-2 text-sm last:border-0">
                <div className="font-medium">{factor.factor}</div>
                <div className="mt-1 text-xs text-[var(--muted)]">{factor.source}</div>
                <p className="mt-1 text-sm text-[var(--muted)]">{factor.reason}</p>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </section>
  );
}
