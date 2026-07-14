"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  API_BASE_URL,
  type EventSusceptibilityPayload,
  type EventsPayload,
  type SusceptibilityComponent,
  fetchJson,
} from "@/lib/api";

type LoadState = {
  events?: EventsPayload;
  susceptibility?: EventSusceptibilityPayload;
  loading: boolean;
  error?: string;
};

function apiUrl(path: string) {
  return path.startsWith("http") ? path : `${API_BASE_URL}${path}`;
}

function formatNumber(value: number | null | undefined, digits = 1) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "n/a";
}

function formatPercent(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : "n/a";
}

function formatOriginal(component: SusceptibilityComponent) {
  if (component.original_value === null || component.original_value === undefined) {
    return "missing";
  }
  if (typeof component.original_value === "number") {
    return `${formatNumber(component.original_value, component.units === "percent" ? 1 : 2)} ${component.units}`;
  }
  return component.original_value;
}

function componentLabel(value: string) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function ScoreCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: number | null | undefined;
  detail: string;
}) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
      <div className="text-xs uppercase text-[var(--muted)]">{label}</div>
      <div className="mt-2 text-3xl font-semibold">{formatNumber(value)}</div>
      <p className="mt-2 text-sm text-[var(--muted)]">{detail}</p>
    </div>
  );
}

export function SusceptibilityPage() {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<unknown>(null);
  const [selectedEventId, setSelectedEventId] = useState("");
  const [state, setState] = useState<LoadState>({ loading: true });

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
    async function loadSusceptibility() {
      setState((current) => ({ ...current, loading: true, error: undefined }));
      try {
        const susceptibility = await fetchJson<EventSusceptibilityPayload>(`/api/susceptibility/events/${selectedEventId}`);
        if (!cancelled) {
          setState((current) => ({ ...current, susceptibility, loading: false }));
        }
      } catch (err) {
        if (!cancelled) {
          setState((current) => ({
            ...current,
            loading: false,
            error: err instanceof Error ? err.message : "Failed to load susceptibility model",
          }));
        }
      }
    }
    loadSusceptibility();
    return () => {
      cancelled = true;
    };
  }, [selectedEventId]);

  const layer = state.susceptibility?.combined_layer;
  const availableComponents = useMemo(
    () => state.susceptibility?.dynamic_condition_index.components.filter((component) => !component.missing_data && component.weight > 0) ?? [],
    [state.susceptibility],
  );
  const missingComponents = useMemo(
    () => state.susceptibility?.dynamic_condition_index.components.filter((component) => component.missing_data || component.status !== "available") ?? [],
    [state.susceptibility],
  );

  useEffect(() => {
    if (!mapContainer.current || !layer) {
      return;
    }
    let removed = false;
    async function setupMap() {
      const maplibre = await import("maplibre-gl");
      if (removed || !mapContainer.current || !layer) {
        return;
      }
      const bounds = layer.bounds_wgs84;
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
        center: [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2],
        zoom: 10,
      });
      mapRef.current = map;
      map.addControl(new maplibre.NavigationControl({ visualizePitch: false }), "top-right");
      map.on("load", () => {
        map.addSource("combined-susceptibility", {
          type: "image",
          url: apiUrl(layer.preview_url),
          coordinates: layer.coordinates,
        });
        map.addLayer({
          id: "combined-susceptibility",
          type: "raster",
          source: "combined-susceptibility",
          paint: { "raster-opacity": layer.opacity },
        });
        map.fitBounds(
          [
            [bounds[0], bounds[1]],
            [bounds[2], bounds[3]],
          ],
          { padding: 38, duration: 0 },
        );
      });
    }
    setupMap();
    return () => {
      removed = true;
      const map = mapRef.current as { remove?: () => void } | null;
      map?.remove?.();
      mapRef.current = null;
    };
  }, [layer?.preview_url]);

  if (state.error) {
    return <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-6 text-[#ffd8d1]">{state.error}</section>;
  }

  return (
    <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_420px]">
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h2 className="text-2xl font-semibold">Prototype Susceptibility</h2>
            <p className="mt-1 text-sm text-[var(--muted)]">
              Static terrain score plus event-specific dynamic condition inputs. This is not avalanche detection.
            </p>
          </div>
          <select
            aria-label="Select susceptibility event"
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
        </div>

        <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4 text-sm leading-relaxed text-[#ffd8d1]">
          {state.susceptibility?.disclaimer ??
            "Experimental research prototype. This output has not been validated for operational avalanche forecasting and must not be used as a replacement for professional avalanche forecasts or field assessment."}
        </section>

        <section className="grid gap-4 md:grid-cols-3">
          <ScoreCard
            label="Terrain mean"
            value={state.susceptibility?.terrain_susceptibility.stats.mean}
            detail={`${formatNumber((state.susceptibility?.terrain_susceptibility.weight_in_combined_index ?? 0) * 100, 0)}% configured combined weight`}
          />
          <ScoreCard
            label="Dynamic score"
            value={state.susceptibility?.dynamic_condition_index.score}
            detail={`${formatPercent(state.susceptibility?.dynamic_condition_index.available_weight_fraction)} dynamic input coverage`}
          />
          <ScoreCard
            label="Combined mean"
            value={state.susceptibility?.combined_index.stats?.mean}
            detail={state.susceptibility?.combined_index.available ? "Terrain raster adjusted by dynamic scalar" : "Combined layer unavailable"}
          />
        </section>

        <div className="relative h-[620px] min-h-[480px] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--panel-strong)]">
          {state.loading ? (
            <div className="flex h-full items-center justify-center text-[var(--muted)]">Loading susceptibility model...</div>
          ) : layer ? (
            <div ref={mapContainer} className="h-full w-full" />
          ) : (
            <div className="flex h-full items-center justify-center p-6 text-center text-[var(--muted)]">
              Combined susceptibility map is unavailable because dynamic input coverage is insufficient.
            </div>
          )}
          <div className="absolute bottom-3 left-3 max-w-xl rounded-md border border-[#6f3b34] bg-[#271a18]/95 px-3 py-2 text-xs leading-relaxed text-[#ffd8d1]">
            Experimental combined index, not a validated avalanche forecast.
          </div>
        </div>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Dynamic Condition Components</h3>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full min-w-[820px] border-collapse text-sm">
              <thead className="text-left text-xs uppercase text-[var(--muted)]">
                <tr>
                  <th className="border-b border-[var(--border)] py-2 pr-3">Component</th>
                  <th className="border-b border-[var(--border)] py-2 pr-3">Original</th>
                  <th className="border-b border-[var(--border)] py-2 pr-3">Normalized</th>
                  <th className="border-b border-[var(--border)] py-2 pr-3">Weight</th>
                  <th className="border-b border-[var(--border)] py-2">Source</th>
                </tr>
              </thead>
              <tbody>
                {(state.susceptibility?.dynamic_condition_index.components ?? []).map((component) => (
                  <tr key={component.component}>
                    <td className="border-b border-[var(--border)] py-2 pr-3">
                      <div className="font-medium">{componentLabel(component.component)}</div>
                      <div className="text-xs text-[var(--muted)]">{component.status}</div>
                    </td>
                    <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{formatOriginal(component)}</td>
                    <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{formatNumber(component.normalized_value)}</td>
                    <td className="border-b border-[var(--border)] py-2 pr-3 font-mono">{formatNumber(component.weight, 2)}</td>
                    <td className="border-b border-[var(--border)] py-2 text-xs text-[var(--muted)]">
                      <div>{component.source}</div>
                      <div className="mt-1">{component.reason}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <aside className="flex flex-col gap-4">
        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Combined Legend</h3>
          <div className="mt-3 space-y-2 text-sm">
            {(layer?.legend ?? []).map((item) => (
              <div key={item.label} className="flex items-center gap-2 text-[var(--muted)]">
                <span className="h-3 w-7 rounded-sm border border-white/20" style={{ backgroundColor: item.color }} />
                <span>{item.label}</span>
              </div>
            ))}
          </div>
          {layer?.download_url ? (
            <a
              href={apiUrl(layer.download_url)}
              className="mt-4 inline-flex rounded-md border border-[var(--border)] px-3 py-2 text-sm text-white hover:bg-[var(--panel-strong)]"
            >
              Download combined GeoTIFF
            </a>
          ) : null}
        </section>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Available Inputs</h3>
          <div className="mt-3 space-y-3">
            {availableComponents.map((component) => (
              <div key={component.component} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-sm">
                <div className="font-medium">{componentLabel(component.component)}</div>
                <div className="mt-1 font-mono text-[var(--accent)]">{formatNumber(component.normalized_value)} / 100</div>
                <p className="mt-1 text-xs text-[var(--muted)]">{component.reason}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Static Terrain Factors</h3>
          <div className="mt-3 space-y-3">
            {(state.susceptibility?.terrain_susceptibility.factors ?? []).map((factor) => (
              <div key={factor.factor} className="border-b border-[var(--border)] pb-2 text-sm last:border-0">
                <div className="font-medium">{factor.factor}</div>
                <div className="mt-1 text-xs text-[var(--muted)]">{factor.source}</div>
                <p className="mt-1 text-sm text-[var(--muted)]">{factor.reason}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="text-lg font-semibold">Configuration</h3>
          <div className="mt-3 space-y-2 text-sm">
            <div className="flex justify-between gap-3 border-b border-[var(--border)] pb-2">
              <span className="text-[var(--muted)]">Version</span>
              <span className="font-mono">{state.susceptibility?.configuration.version ?? "n/a"}</span>
            </div>
            <div className="flex justify-between gap-3 border-b border-[var(--border)] pb-2">
              <span className="text-[var(--muted)]">Weights file</span>
              <span className="font-mono text-xs">{state.susceptibility?.configuration.weights_file ?? "n/a"}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-[var(--muted)]">SHA-256</span>
              <span className="max-w-[220px] truncate font-mono text-xs">{state.susceptibility?.configuration.weights_sha256 ?? "n/a"}</span>
            </div>
          </div>
        </section>

        {missingComponents.length || state.susceptibility?.warnings.length ? (
          <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4">
            <h3 className="text-lg font-semibold text-[#ffd8d1]">Missing Data And Warnings</h3>
            <div className="mt-3 space-y-2 text-sm leading-relaxed text-[#ffd8d1]">
              {missingComponents.map((component) => (
                <p key={component.component}>
                  {componentLabel(component.component)}: {component.reason}
                </p>
              ))}
              {(state.susceptibility?.warnings ?? []).map((warning) => (
                <p key={warning}>{warning}</p>
              ))}
            </div>
          </section>
        ) : null}
      </aside>
    </section>
  );
}
