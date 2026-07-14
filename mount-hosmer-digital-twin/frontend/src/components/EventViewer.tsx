"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE_URL,
  type EventDetail,
  type EventLayer,
  type EventsPayload,
  fetchJson,
} from "@/lib/api";

type LoadState = {
  events?: EventsPayload;
  detail?: EventDetail;
  loading: boolean;
  error?: string;
};

type VisibilityState = Record<string, boolean>;
type OpacityState = Record<string, number>;

const IMPORTANT_LAYER_IDS = [
  "s2_true_color",
  "s2_ndsi",
  "s2_ndmi",
  "s2_snow_class_mask",
  "landsat_surface_temperature",
  "s2_cloud_mask",
  "landsat_cloud_mask",
  "landsat_valid_data_mask",
] as const;

const DEFAULT_VISIBLE = new Set(["s2_true_color", "s2_ndsi"]);

const LAYER_COPY: Record<string, { title: string; help: string }> = {
  s2_true_color: {
    title: "Scene context",
    help: "Natural-colour Sentinel-2 imagery for visible snow, cloud, and terrain context.",
  },
  s2_ndsi: {
    title: "Snow cover signal",
    help: "NDSI highlights snow-covered surfaces where the satellite view is valid.",
  },
  s2_ndmi: {
    title: "Moisture signal",
    help: "NDMI helps show relative snow/vegetation moisture conditions.",
  },
  s2_snow_class_mask: {
    title: "Classified snow",
    help: "Sentinel-2 scene classification pixels labelled as snow or ice.",
  },
  landsat_surface_temperature: {
    title: "Surface temperature",
    help: "Landsat thermal layer converted to degrees Celsius for warm/cold surface context.",
  },
  s2_cloud_mask: {
    title: "Sentinel clouds",
    help: "Cloud pixels that can hide the terrain and reduce event confidence.",
  },
  landsat_cloud_mask: {
    title: "Landsat clouds",
    help: "Quality-mask cloud flags from the Landsat scene.",
  },
  landsat_valid_data_mask: {
    title: "Landsat valid data",
    help: "Pixels not flagged as fill, cloud, cloud shadow, or cirrus.",
  },
};

function apiUrl(path: string) {
  return path.startsWith("http") ? path : `${API_BASE_URL}${path}`;
}

function formatDate(value?: string) {
  if (!value) {
    return "Unavailable";
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatNumber(value: number | null | undefined, digits = 1) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "n/a";
}

function formatPercent(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)}%` : "n/a";
}

function sensorLabel(sensor: string) {
  return sensor === "sentinel2" ? "Sentinel-2" : sensor === "landsat" ? "Landsat" : sensor;
}

function layerControlLabel(layer: EventLayer) {
  return LAYER_COPY[layer.id] ?? { title: layer.title, help: layer.sensor };
}

export function EventViewer() {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<unknown>(null);
  const [state, setState] = useState<LoadState>({ loading: true });
  const [selectedEventId, setSelectedEventId] = useState<string>("");
  const [visible, setVisible] = useState<VisibilityState>({});
  const [opacity, setOpacity] = useState<OpacityState>({});
  const [cursor, setCursor] = useState("Move over map");

  useEffect(() => {
    let cancelled = false;
    async function loadEvents() {
      try {
        const events = await fetchJson<EventsPayload>("/api/events");
        const latest = [...events.events].sort((a, b) => b.event_id.localeCompare(a.event_id))[0];
        if (!cancelled) {
          setSelectedEventId((current) => current || latest?.event_id || "");
          setState((current) => ({ ...current, events, loading: !latest }));
        }
      } catch (err) {
        if (!cancelled) {
          setState({ loading: false, error: err instanceof Error ? err.message : "Failed to load events" });
        }
      }
    }
    loadEvents();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedEventId) {
      return;
    }
    let cancelled = false;
    async function loadDetail() {
      setState((current) => ({ ...current, loading: true, error: undefined }));
      try {
        const detail = await fetchJson<EventDetail>(`/api/events/${selectedEventId}`);
        if (cancelled) {
          return;
        }
        const nextVisible: VisibilityState = {};
        const nextOpacity: OpacityState = {};
        for (const layer of detail.layers) {
          nextVisible[layer.id] = DEFAULT_VISIBLE.has(layer.id);
          nextOpacity[layer.id] = layer.opacity;
        }
        setVisible(nextVisible);
        setOpacity(nextOpacity);
        setState((current) => ({ ...current, detail, loading: false }));
      } catch (err) {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            loading: false,
            error: err instanceof Error ? err.message : "Failed to load event detail",
          }));
        }
      }
    }
    loadDetail();
    return () => {
      cancelled = true;
    };
  }, [selectedEventId]);

  const importantLayers = useMemo(() => {
    const byId = new Map((state.detail?.layers ?? []).map((layer) => [layer.id, layer]));
    return IMPORTANT_LAYER_IDS.map((id) => byId.get(id)).filter((layer): layer is EventLayer => Boolean(layer));
  }, [state.detail]);

  const activeLegends = importantLayers.filter((layer) => visible[layer.id]);

  useEffect(() => {
    if (!mapContainer.current || !state.detail) {
      return;
    }
    let removed = false;
    async function setupMap() {
      const maplibre = await import("maplibre-gl");
      if (removed || !mapContainer.current || !state.detail) {
        return;
      }
      const bounds = state.detail.layers[0]?.bounds_wgs84;
      const center: [number, number] = bounds
        ? [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]
        : [-115.011, 49.614];
      const existing = mapRef.current as { remove?: () => void } | null;
      existing?.remove?.();
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
          layers: [{ id: "osmBase", type: "raster", source: "osmBase", paint: { "raster-opacity": 0.35 } }],
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
        for (const layer of importantLayers) {
          const sourceId = `event-src-${layer.id}`;
          const layerId = `event-layer-${layer.id}`;
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
  }, [state.detail?.event_id, importantLayers]);

  useEffect(() => {
    const map = mapRef.current as { getLayer?: (id: string) => unknown; setLayoutProperty?: (...args: unknown[]) => void } | null;
    if (!map?.getLayer) {
      return;
    }
    for (const [layerId, isVisible] of Object.entries(visible)) {
      const mapLayerId = `event-layer-${layerId}`;
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
      const mapLayerId = `event-layer-${layerId}`;
      if (map.getLayer(mapLayerId)) {
        map.setPaintProperty?.(mapLayerId, "raster-opacity", layerOpacity);
      }
    }
  }, [opacity]);

  const sensors = state.detail?.summary.sensors ?? {};

  if (state.error) {
    return <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-6 text-[#ffd8d1]">{state.error}</section>;
  }

  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_390px]">
      <div className="flex flex-col gap-3">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h2 className="text-2xl font-semibold">Satellite Event Viewer</h2>
            <p className="mt-1 text-sm text-[var(--muted)]">
              Select a discovered event date and inspect snow, moisture, temperature, and quality layers.
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <select
              aria-label="Select satellite event"
              value={selectedEventId}
              onChange={(event) => setSelectedEventId(event.target.value)}
              className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-sm text-white"
            >
              {(state.events?.events ?? []).map((event) => (
                <option key={event.event_id} value={event.event_id}>
                  {event.date_label} ({event.event_id})
                </option>
              ))}
            </select>
            <div className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 font-mono text-sm text-[var(--muted)]">
              {cursor}
            </div>
          </div>
        </div>

        <div className="relative h-[680px] min-h-[520px] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--panel-strong)]">
          {state.loading ? (
            <div className="flex h-full items-center justify-center text-[var(--muted)]">Loading satellite event layers...</div>
          ) : (
            <div ref={mapContainer} className="h-full w-full" />
          )}
          <div className="absolute bottom-3 left-3 max-w-xl rounded-md border border-[#6f3b34] bg-[#271a18]/95 px-3 py-2 text-xs leading-relaxed text-[#ffd8d1]">
            {state.detail?.disclaimer}
          </div>
        </div>
      </div>

      <aside className="flex flex-col gap-4">
        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Event Layers</h3>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Only the event layers needed for avalanche-condition context are exposed here.
          </p>
          <div className="mt-4 space-y-4">
            {importantLayers.map((layer) => {
              const copy = layerControlLabel(layer);
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
          </div>
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Event Summary</h3>
          <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
            <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Sentinel time</div>
              <div className="mt-1 text-sm">{formatDate(state.detail?.summary.sentinel_datetime_utc)}</div>
            </div>
            <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Landsat time</div>
              <div className="mt-1 text-sm">{formatDate(state.detail?.summary.landsat_datetime_utc)}</div>
            </div>
            <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Time gap</div>
              <div className="mt-1 font-mono">{formatNumber(state.detail?.summary.time_difference_hours, 2)} h</div>
            </div>
            <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
              <div className="text-xs uppercase text-[var(--muted)]">Layers</div>
              <div className="mt-1 font-mono">{state.detail?.layers.length ?? 0}</div>
            </div>
          </div>

          <div className="mt-4 space-y-3">
            {Object.entries(sensors).map(([sensorName, sensor]) => (
              <div key={sensorName} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-sm">
                <div className="font-medium">{sensorLabel(sensorName)}</div>
                <div className="mt-2 grid grid-cols-3 gap-2">
                  <div>
                    <div className="text-xs text-[var(--muted)]">Cloud</div>
                    <div className="font-mono">{formatPercent(sensor.cloud_percent_calculated ?? sensor.metadata_cloud_percent)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--muted)]">Valid</div>
                    <div className="font-mono">{formatPercent(sensor.valid_pixel_percent)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--muted)]">Snow</div>
                    <div className="font-mono">{formatPercent(sensor.snow_cover_percent)}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Legend</h3>
          <div className="mt-3 space-y-3">
            {activeLegends.length ? (
              activeLegends.map((layer) => (
                <div key={layer.id}>
                  <div className="mb-2 text-sm font-medium">{layerControlLabel(layer).title}</div>
                  {layer.legend.map((item) => (
                    <div key={`${layer.id}-${item.label}`} className="flex items-center gap-2 text-xs text-[var(--muted)]">
                      <span className="h-3 w-6 rounded-sm border border-white/20" style={{ backgroundColor: item.color }} />
                      <span>{item.label}</span>
                    </div>
                  ))}
                </div>
              ))
            ) : (
              <p className="text-sm text-[var(--muted)]">Turn on a layer to see its legend.</p>
            )}
          </div>
        </div>

        {state.detail?.warnings.length ? (
          <div className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4">
            <h3 className="text-lg font-semibold text-[#ffd8d1]">Event Warnings</h3>
            <div className="mt-3 max-h-48 space-y-2 overflow-auto text-xs leading-relaxed text-[#ffd8d1]">
              {state.detail.warnings.map((warning) => (
                <p key={warning}>{warning}</p>
              ))}
            </div>
          </div>
        ) : null}
      </aside>
    </section>
  );
}
