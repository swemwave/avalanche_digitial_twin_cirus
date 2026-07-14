"use client";

import { useEffect, useMemo, useState } from "react";
import { AoiMap } from "@/components/AoiMap";
import {
  type AoiPayload,
  type CatalogPayload,
  type HealthPayload,
  fetchJson,
} from "@/lib/api";

type LoadState = {
  health?: HealthPayload;
  catalog?: CatalogPayload;
  aoi?: AoiPayload;
  error?: string;
  loading: boolean;
};

function formatBytes(bytes?: number) {
  if (!bytes) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
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

function Metric({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string | number;
  tone?: "default" | "warning" | "danger";
}) {
  const color =
    tone === "danger" ? "text-[var(--danger)]" : tone === "warning" ? "text-[var(--accent-2)]" : "text-white";
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
      <div className="text-xs uppercase tracking-wide text-[var(--muted)]">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${color}`}>{value}</div>
    </div>
  );
}

export function OverviewDashboard({ embedded = false }: { embedded?: boolean }) {
  const [state, setState] = useState<LoadState>({ loading: true });

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [health, catalog, aoi] = await Promise.all([
          fetchJson<HealthPayload>("/api/health"),
          fetchJson<CatalogPayload>("/api/catalog?compact=true"),
          fetchJson<AoiPayload>("/api/aoi"),
        ]);
        if (!cancelled) {
          setState({ health, catalog, aoi, loading: false });
        }
      } catch (err) {
        if (!cancelled) {
          setState({
            loading: false,
            error: err instanceof Error ? err.message : "Failed to load backend data",
          });
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const summary = state.catalog?.summary;
  const eventDates = useMemo(() => {
    return summary?.event_ids.map((id) => id.replace(/^MH_/, "")) ?? [];
  }, [summary]);
  const manifestStatuses = summary?.manifest.status_counts ?? {};
  const typeCounts = summary?.type_counts ?? {};

  const content = (
    <div className="flex flex-col gap-6">
      <div className="flex justify-end">
        <div className="rounded-md border border-[var(--border)] bg-[var(--panel)] px-3 py-2 text-sm text-[var(--muted)]">
          Backend: {state.health?.status ?? "unknown"} | Catalog: {state.health?.catalog_exists ? "available" : "missing"}
        </div>
      </div>

        <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-4 text-sm text-[#ffd8d1]">
          Experimental research prototype. This output has not been validated for operational avalanche forecasting and must
          not be used as a replacement for professional avalanche forecasts or field assessment.
        </section>

        {state.loading ? (
          <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-6 text-[var(--muted)]">
            Loading catalog summary from the local backend...
          </section>
        ) : state.error ? (
          <section className="rounded-lg border border-[#6f3b34] bg-[#271a18] p-6 text-[#ffd8d1]">
            {state.error}
          </section>
        ) : (
          <>
            <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Metric label="Cataloged files" value={summary?.file_count ?? 0} />
              <Metric label="Source data size" value={formatBytes(summary?.total_size_bytes)} />
              <Metric label="Events discovered" value={summary?.event_count ?? 0} />
              <Metric
                label="Warnings"
                value={summary?.warning_count ?? 0}
                tone={summary?.warning_count ? "warning" : "default"}
              />
            </section>

            <section className="grid gap-5 lg:grid-cols-[1.2fr_0.8fr]">
              <div className="flex flex-col gap-4">
                <div>
                  <h2 className="text-xl font-semibold">AOI</h2>
                  <p className="mt-1 text-sm text-[var(--muted)]">
                    Analysis CRS {state.aoi?.grid?.analysis_crs ?? "unavailable"} with 10 m and 30 m grids from the source
                    metadata.
                  </p>
                </div>
                <AoiMap aoi={state.aoi} />
              </div>

              <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-5">
                <h2 className="text-xl font-semibold">Data Sources</h2>
                <div className="mt-4 space-y-3 text-sm">
                  {Object.entries(typeCounts).map(([key, value]) => (
                    <div key={key} className="flex items-center justify-between border-b border-[var(--border)] pb-2">
                      <span className="capitalize text-[var(--muted)]">{key.replace("_", " ")}</span>
                      <span className="font-mono text-white">{value}</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="grid gap-5 lg:grid-cols-3">
              <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-5">
                <h2 className="text-lg font-semibold">Manifest</h2>
                <p className="mt-1 text-sm text-[var(--muted)]">
                  {summary?.manifest.rows ?? 0} manifest rows, generated catalog {formatDate(state.catalog?.generated_at_utc)}.
                </p>
                <div className="mt-4 space-y-2 text-sm">
                  {Object.entries(manifestStatuses).map(([status, count]) => (
                    <div key={status} className="flex justify-between">
                      <span className="text-[var(--muted)]">{status || "blank"}</span>
                      <span className="font-mono">{count}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-5">
                <h2 className="text-lg font-semibold">Available Events</h2>
                <div className="mt-4 flex flex-wrap gap-2">
                  {eventDates.length ? (
                    eventDates.map((event) => (
                      <span key={event} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1 font-mono text-sm">
                        {event}
                      </span>
                    ))
                  ) : (
                    <span className="text-sm text-[var(--muted)]">No event folders discovered.</span>
                  )}
                </div>
              </div>

              <div className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-5">
                <h2 className="text-lg font-semibold">Known Issues</h2>
                <div className="mt-4 space-y-3 text-sm text-[var(--muted)]">
                  <p>Missing files: {summary?.missing_file_count ?? 0}</p>
                  <p>Failed checks: {summary?.failed_check_count ?? 0}</p>
                  <p>Download errors: {state.catalog?.download_errors.length ?? 0}</p>
                </div>
              </div>
            </section>
          </>
        )}
      </div>
  );

  if (embedded) {
    return content;
  }

  return (
    <main className="min-h-screen bg-[var(--background)] px-5 py-6 text-[var(--foreground)] md:px-8">
      <div className="mx-auto max-w-7xl">{content}</div>
    </main>
  );
}
