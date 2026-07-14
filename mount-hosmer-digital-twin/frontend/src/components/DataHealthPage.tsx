"use client";

import { useEffect, useState } from "react";
import { getDataHealth, getReadiness, type DataHealth, type Readiness } from "@/lib/apiV1";

/**
 * Data health.
 *
 * The point of this page is the distinction the file catalogue cannot make:
 * **presence is not usability.** The 2025-26 snow files are present, well-formed,
 * catalogued — and empty. A page that showed "271 files, all present" would be
 * telling the truth and communicating a lie.
 */

const STATUS_COLOR: Record<string, string> = {
  ok: "text-[var(--accent)]",
  warning: "text-[var(--accent-2)]",
  degraded: "text-[var(--accent-2)]",
  critical: "text-[var(--danger)]",
};

export function DataHealthPage() {
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getDataHealth(), getReadiness()])
      .then(([healthBody, readinessBody]) => {
        setHealth(healthBody);
        setReadiness(readinessBody);
      })
      .catch((caught: Error) => setError(caught.message));
  }, []);

  if (error) {
    return <p className="text-sm text-[var(--danger)]">{error}</p>;
  }
  if (!health) {
    return <p className="text-sm text-[var(--muted)]">Checking every dataset…</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-end justify-between gap-3 rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-[var(--muted)]">Overall</p>
          <p className={`text-2xl font-semibold capitalize ${STATUS_COLOR[health.overall_status]}`}>
            {health.overall_status}
          </p>
        </div>
        <dl className="flex flex-wrap gap-5 text-sm">
          <div>
            <dt className="text-[11px] text-[var(--muted)]">Usable by model</dt>
            <dd className="tabular-nums">
              {health.summary.datasets_usable_by_model} / {health.summary.datasets_checked}
            </dd>
          </div>
          <div>
            <dt className="text-[11px] text-[var(--muted)]">Critical</dt>
            <dd className="tabular-nums text-[var(--danger)]">{health.summary.critical_issues}</dd>
          </div>
          <div>
            <dt className="text-[11px] text-[var(--muted)]">Warnings</dt>
            <dd className="tabular-nums text-[var(--accent-2)]">{health.summary.warnings}</dd>
          </div>
          <div>
            <dt className="text-[11px] text-[var(--muted)]">Serving</dt>
            <dd className={readiness?.ready ? "text-[var(--accent)]" : "text-[var(--danger)]"}>
              {readiness?.ready ? "ready" : "not ready"}
            </dd>
          </div>
        </dl>
      </header>

      {readiness && !readiness.ready ? (
        <div className="rounded-md border border-[var(--danger)] bg-[var(--danger)]/10 p-3 text-xs text-[var(--danger)]">
          <p className="font-semibold">This deployment cannot produce an analysis.</p>
          <ul className="mt-1 list-disc pl-4">
            {readiness.remedy.map((remedy) => (
              <li key={remedy}>{remedy}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="rounded-md border border-[var(--accent-2)] bg-[var(--accent-2)]/10 p-3 text-xs leading-relaxed text-[var(--accent-2)]">
        <strong>An empty file is missing data, never a reading of zero.</strong>{" "}
        {health.empty_file_policy}
      </div>

      <section className="overflow-x-auto rounded-lg border border-[var(--border)] bg-[var(--panel)]">
        <table className="w-full min-w-[720px] text-sm">
          <thead className="border-b border-[var(--border)] text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="px-4 py-2">Dataset</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Usable</th>
              <th className="px-4 py-2 text-right">Files</th>
              <th className="px-4 py-2">Provenance</th>
            </tr>
          </thead>
          <tbody>
            {health.datasets.map((dataset) => (
              <tr key={dataset.key} className="border-b border-[var(--border)] align-top last:border-0">
                <td className="px-4 py-2">
                  <p>{dataset.title}</p>
                  {dataset.issues
                    .filter((issue) => issue.severity !== "info")
                    .map((issue) => (
                      <p
                        key={issue.code}
                        className={`mt-1 text-[11px] leading-snug ${STATUS_COLOR[issue.severity] ?? "text-[var(--muted)]"}`}
                      >
                        {issue.message}
                      </p>
                    ))}
                </td>
                <td className={`px-4 py-2 capitalize ${STATUS_COLOR[dataset.status]}`}>{dataset.status}</td>
                <td className="px-4 py-2">
                  {dataset.usable_by_model ? (
                    <span className="text-[var(--accent)]">yes</span>
                  ) : (
                    <span className="text-[var(--danger)]">no</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right tabular-nums text-[var(--muted)]">
                  {dataset.catalog_file_count}
                </td>
                <td className="px-4 py-2 text-[var(--muted)]">{dataset.provenance}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
          Data that does not exist
        </h3>
        <p className="mt-1 text-xs leading-relaxed text-[var(--muted)]">
          These gaps do not close by writing more code. They need instruments and observations that
          nobody has collected for this mountain.
        </p>
        <ul className="mt-2 flex flex-col gap-2">
          {health.missing_datasets.map((missing) => (
            <li key={missing.name} className="text-xs">
              <p className="text-[var(--foreground)]">{missing.name}</p>
              <p className="leading-snug text-[var(--muted)]">{missing.why_it_matters}</p>
            </li>
          ))}
        </ul>
        <p className="mt-3 border-t border-[var(--border)] pt-3 text-xs leading-relaxed text-[var(--danger)]">
          <strong>Uncalibrated.</strong> {health.calibration_status.reason}
        </p>
      </section>
    </div>
  );
}
