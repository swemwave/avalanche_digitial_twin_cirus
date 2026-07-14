"use client";

import { useCallback, useEffect, useState } from "react";
import {
  archiveSimulation,
  listSimulations,
  type SimulationSummary,
} from "@/lib/apiV1";

/**
 * Simulation history: reproduce, compare, archive.
 *
 * "Reproduce" is not a nicety. Every run stores its analysis id, its seed, its
 * model version and the SHA-256 of the config that produced it, so a number can be
 * traced back to the exact parameters behind it. Two runs made under different
 * config hashes are **not comparable**, and the compare view says so rather than
 * quietly putting them side by side.
 */

type Props = {
  onReproduce: (simulation: SimulationSummary) => void;
};

export function SimulationHistory({ onReproduce }: Props) {
  const [rows, setRows] = useState<SimulationSummary[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listSimulations()
      .then((body) => setRows(body.simulations))
      .catch((caught: Error) => setError(caught.message));
  }, []);

  useEffect(refresh, [refresh]);

  const chosen = rows.filter((row) => selected.includes(row.simulation_id));
  const mixedVersions = new Set(chosen.map((row) => row.model_version)).size > 1;

  function toggle(id: string) {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id].slice(-2),
    );
  }

  async function archive(id: string) {
    await archiveSimulation(id);
    refresh();
  }

  if (error) return <p className="text-sm text-[var(--danger)]">{error}</p>;

  return (
    <div className="flex flex-col gap-4">
      {chosen.length === 2 ? (
        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
            Comparing two runs
          </h3>
          {mixedVersions ? (
            <p className="mb-2 rounded border border-[var(--danger)] bg-[var(--danger)]/10 p-2 text-[11px] text-[var(--danger)]">
              These two runs were produced by <strong>different model versions</strong>. They are not
              comparable: a difference between them may be a change in the mountain, or it may just be
              a change in the model.
            </p>
          ) : null}
          <table className="w-full text-sm">
            <tbody>
              {(
                [
                  ["Risk", (row: SimulationSummary) => row.combined_risk_score],
                  ["Hazard", (row: SimulationSummary) => row.hazard_score],
                  ["Consequence", (row: SimulationSummary) => row.consequence_score],
                  ["Confidence", (row: SimulationSummary) => row.confidence_score],
                  ["Runout (ha)", (row: SimulationSummary) => (row.runout_area_m2 ?? 0) / 10_000],
                  ["Engine", (row: SimulationSummary) => row.engine],
                  ["Release size", (row: SimulationSummary) => row.release_size],
                  ["Seed", (row: SimulationSummary) => row.random_seed],
                ] as [string, (row: SimulationSummary) => unknown][]
              ).map(([label, read]) => {
                const left = read(chosen[0]);
                const right = read(chosen[1]);
                const differs = String(left) !== String(right);
                return (
                  <tr key={label} className="border-b border-[var(--border)] last:border-0">
                    <td className="py-1.5 pr-4 text-[11px] uppercase tracking-wide text-[var(--muted)]">
                      {label}
                    </td>
                    <td className="py-1.5 tabular-nums">{format(left)}</td>
                    <td className={`py-1.5 tabular-nums ${differs ? "text-[var(--accent-2)]" : ""}`}>
                      {format(right)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : null}

      <section className="overflow-x-auto rounded-lg border border-[var(--border)] bg-[var(--panel)]">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="border-b border-[var(--border)] text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
            <tr>
              <th className="px-3 py-2">Compare</th>
              <th className="px-3 py-2">Simulation</th>
              <th className="px-3 py-2">Engine</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2 text-right">Risk</th>
              <th className="px-3 py-2 text-right">Confidence</th>
              <th className="px-3 py-2 text-right">Runout</th>
              <th className="px-3 py-2">Model</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.simulation_id} className="border-b border-[var(--border)] last:border-0">
                <td className="px-3 py-2">
                  <input
                    type="checkbox"
                    checked={selected.includes(row.simulation_id)}
                    onChange={() => toggle(row.simulation_id)}
                    className="accent-[var(--accent)]"
                  />
                </td>
                <td className="px-3 py-2">
                  <p className="font-mono text-[11px]">{row.simulation_id}</p>
                  <p className="text-[10px] text-[var(--muted)]">
                    {row.created_utc?.slice(0, 16).replace("T", " ")}
                  </p>
                </td>
                <td className="px-3 py-2 text-[11px] text-[var(--muted)]">{row.engine}</td>
                <td className="px-3 py-2 text-[11px]">{row.release_size}</td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {row.combined_risk_score != null ? Math.round(row.combined_risk_score) : "—"}
                  <span className="ml-1 text-[10px] text-[var(--muted)]">{row.risk_level}</span>
                </td>
                <td
                  className={`px-3 py-2 text-right tabular-nums ${
                    (row.confidence_score ?? 100) < 50 ? "text-[var(--danger)]" : ""
                  }`}
                >
                  {row.confidence_score != null ? Math.round(row.confidence_score) : "—"}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)]">
                  {row.runout_area_m2 ? `${(row.runout_area_m2 / 10_000).toFixed(0)} ha` : "—"}
                </td>
                <td className="px-3 py-2 font-mono text-[10px] text-[var(--muted)]">{row.model_version}</td>
                <td className="px-3 py-2">
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => onReproduce(row)}
                      className="text-[11px] text-[var(--accent)] underline underline-offset-2"
                    >
                      reproduce
                    </button>
                    <button
                      type="button"
                      onClick={() => archive(row.simulation_id)}
                      className="text-[11px] text-[var(--muted)] underline underline-offset-2 hover:text-[var(--danger)]"
                    >
                      archive
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {!rows.length ? (
              <tr>
                <td colSpan={9} className="px-3 py-6 text-center text-sm text-[var(--muted)]">
                  No simulations yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </section>

      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        <strong>Reproduce</strong> re-runs a simulation with the same analysis, zones, mode, release
        size and seed. With the same model version it reproduces the result exactly — that is what the
        stored config hash is for.
      </p>
    </div>
  );
}

function format(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(1);
  return String(value);
}
