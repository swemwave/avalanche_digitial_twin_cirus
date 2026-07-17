"use client";

import type { AssessResult } from "@/lib/twin";

type Props = {
  result: AssessResult | null;
  running: boolean;
  error: string | null;
  selectedZone: string | null;
};

export function ResultCard({ result, running, error, selectedZone }: Props) {
  if (error) {
    return (
      <p className="rounded-md border border-[var(--danger)] bg-[var(--panel-strong)] px-3 py-2 text-xs text-[var(--danger)]">
        {error}
      </p>
    );
  }
  if (running && !result) {
    return <p className="text-sm text-[var(--muted)]">Running the release model and runout…</p>;
  }
  if (!result) {
    return (
      <p className="text-sm text-[var(--muted)]">
        Set the conditions and press <strong>Assess</strong> to model where slabs could release and
        how far they might run.
      </p>
    );
  }

  const { runout } = result;
  const zones = result.zones;
  const highlighted = selectedZone ? zones.find((zone) => zone.zone_id === selectedZone) : null;

  return (
    <div className="flex flex-col gap-4 text-sm">
      {/* The headline number, with its band and its disclaimer directly under it. */}
      <div className="rounded-lg border border-[var(--border)] p-3" style={{ borderColor: result.risk_color }}>
        <div className="flex items-baseline justify-between">
          <span className="text-xs uppercase tracking-wide text-[var(--muted)]">Hazard index</span>
          <span className="rounded px-2 py-0.5 text-xs font-semibold text-[#101415]" style={{ background: result.risk_color }}>
            {result.risk_level}
          </span>
        </div>
        <div className="mt-1 text-3xl font-semibold" style={{ color: result.risk_color }}>
          {result.hazard_score}
          <span className="text-base text-[var(--muted)]">/100</span>
        </div>
        <p className="mt-1 text-[11px] leading-snug text-[var(--muted)]">
          Relative index, not a probability. {result.hazard_detail.method}
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-2 text-xs">
        <Stat label="Release zones" value={String(result.release_zones.zone_count)} />
        <Stat label="Runout footprint" value={`${(runout.core_area_m2 / 1e6).toFixed(2)} km²`} />
        <Stat label="Uncertainty band" value={`${(runout.uncertainty_area_m2 / 1e6).toFixed(2)} km²`} />
        <Stat
          label="Peak modelled speed"
          value={runout.max_velocity_ms != null ? `${runout.max_velocity_ms} m/s` : "—"}
        />
      </dl>

      {highlighted ? (
        <div className="rounded-md border border-[var(--accent)] bg-[var(--panel-strong)] p-3 text-xs">
          <p className="font-semibold text-[var(--accent)]">Zone {highlighted.zone_id}</p>
          <p className="mt-1 text-[var(--muted)]">
            {highlighted.area_hectares} ha · slope {highlighted.mean_slope_deg}° · {highlighted.dominant_aspect_compass}
            -facing · release index {highlighted.estimated_release_score}
          </p>
          <ul className="mt-1 list-disc pl-4 text-[var(--muted)]">
            {highlighted.main_reasons.slice(0, 3).map((reason, index) => (
              <li key={index}>{reason}</li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-[11px] text-[var(--muted)]">Click a release zone on the map to inspect it.</p>
      )}

      {result.warnings.length > 0 ? (
        <details className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2 text-[11px]">
          <summary className="cursor-pointer text-[var(--muted)]">
            {result.warnings.length} model warning{result.warnings.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 list-disc pl-4 text-[var(--muted)]">
            {result.warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </details>
      ) : null}

      <p className="border-t border-[var(--border)] pt-2 text-[11px] leading-relaxed text-[var(--muted)]">
        ⚠ {result.disclaimer}
      </p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2.5 py-2">
      <dt className="text-[var(--muted)]">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm text-[var(--foreground)]">{value}</dd>
    </div>
  );
}
