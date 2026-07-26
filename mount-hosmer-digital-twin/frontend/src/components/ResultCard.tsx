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
      <div className="rounded-[3px] border-l-2 border-[var(--alert)] bg-[var(--field-1)] px-3 py-2.5">
        <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-[var(--alert)]">
          Assessment failed
        </p>
        <p className="mt-1 text-xs leading-relaxed text-[var(--paper-dim)]">{error}</p>
      </div>
    );
  }

  if (running && !result) {
    return (
      <div className="flex flex-col gap-2 py-1">
        <p className="text-xs text-[var(--paper-dim)]">Running release model and runout…</p>
        <span className="h-px w-full overflow-hidden bg-[var(--rule)]">
          <span className="block h-full w-1/3 animate-pulse bg-[var(--signal)]" />
        </span>
      </div>
    );
  }

  if (!result) {
    // An empty state is an instruction, not an apology.
    return (
      <p className="text-xs leading-relaxed text-[var(--paper-dim)]">
        Set conditions, then{" "}
        <span className="font-semibold text-[var(--signal)]">Assess terrain</span> to model where
        slabs could release and how far they might run.
      </p>
    );
  }

  const { runout } = result;
  const zones = result.zones;
  const highlighted = selectedZone ? zones.find((zone) => zone.zone_id === selectedZone) : null;

  return (
    <div className="flex flex-col gap-5">
      <HazardGauge
        score={result.hazard_score}
        level={result.risk_level}
        color={result.risk_color}
        method={result.hazard_detail.method}
      />

      <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
        <Stat label="Release zones" value={String(result.release_zones.zone_count)} />
        <Stat label="Runout footprint" value={(runout.core_area_m2 / 1e6).toFixed(2)} unit="km²" />
        <Stat label="Uncertainty band" value={(runout.uncertainty_area_m2 / 1e6).toFixed(2)} unit="km²" />
        <Stat
          label="Peak speed"
          value={runout.max_velocity_ms != null ? String(runout.max_velocity_ms) : "—"}
          unit={runout.max_velocity_ms != null ? "m/s" : ""}
        />
      </dl>

      {highlighted ? (
        <div className="border-l-2 border-[var(--signal)] bg-[var(--field-1)] px-3 py-2.5">
          <div className="flex items-baseline justify-between gap-2">
            <span className="data text-xs font-medium text-[var(--signal)]">{highlighted.zone_id}</span>
            <span className="data text-[11px] text-[var(--paper-dim)]">
              index {highlighted.estimated_release_score}
            </span>
          </div>
          <p className="data mt-1.5 text-[11px] text-[var(--paper-dim)]">
            {highlighted.area_hectares} ha · {highlighted.mean_slope_deg}° ·{" "}
            {highlighted.dominant_aspect_compass}-facing
          </p>
          <ul className="mt-2 flex flex-col gap-1">
            {highlighted.main_reasons.slice(0, 3).map((reason, index) => (
              <li key={index} className="flex gap-2 text-[11px] leading-relaxed text-[var(--paper-dim)]">
                <span className="mt-[7px] h-px w-2 shrink-0 bg-[var(--rule-lit)]" />
                <span>{reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-[11px] text-[var(--paper-faint)]">
          Select a release zone on the terrain to inspect it.
        </p>
      )}

      {result.warnings.length > 0 ? (
        <details className="group border-t border-[var(--rule)] pt-2.5">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-[11px] text-[var(--paper-dim)] hover:text-[var(--paper)]">
            <span className="data text-[var(--signal-deep)] transition-transform group-open:rotate-90">›</span>
            {result.warnings.length} model warning{result.warnings.length === 1 ? "" : "s"}
          </summary>
          <ul className="mt-2 flex flex-col gap-1 pl-4">
            {result.warnings.map((warning, index) => (
              <li key={index} className="text-[11px] leading-relaxed text-[var(--paper-faint)]">
                {warning}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------------------
 * SIGNATURE ELEMENT — the hazard gauge.
 *
 * The old version was a big coloured number in a bordered box, which is what a
 * dashboard does. This is what an instrument does: a machined 0–100 scale with
 * real tick marks, the reading struck against it, and the band named beneath.
 * The colour is whatever the backend sent in `risk_color` — the UI never
 * decides a hazard colour for itself.
 * ------------------------------------------------------------------------- */
function HazardGauge({
  score,
  level,
  color,
  method,
}: {
  score: number;
  level: string;
  color: string;
  method: string;
}) {
  const clamped = Math.max(0, Math.min(100, score));

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-end justify-between gap-3">
        <div className="flex items-baseline gap-1">
          <span
            className="data text-[42px] leading-none font-medium"
            style={{ color, fontFeatureSettings: '"tnum"' }}
          >
            {score}
          </span>
          <span className="data text-sm text-[var(--paper-faint)]">/100</span>
        </div>
        <span
          className="mb-1 rounded-[2px] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--field)]"
          style={{ background: color }}
        >
          {level}
        </span>
      </div>

      {/* the scale */}
      <div className="relative">
        <div className="h-[6px] w-full overflow-hidden rounded-[1px] bg-[var(--field-2)]">
          <div
            className="h-full transition-[width] duration-500 ease-out"
            style={{ width: `${clamped}%`, background: color }}
          />
        </div>

        {/* the reading marker, struck across the scale */}
        <div
          className="absolute top-[-3px] h-[12px] w-[2px] transition-[left] duration-500 ease-out"
          style={{ left: `calc(${clamped}% - 1px)`, background: "var(--paper)" }}
        />

        {/* tick marks every 20 — the scale is readable without a legend */}
        <div className="mt-1 flex justify-between">
          {[0, 20, 40, 60, 80, 100].map((tick) => (
            <span key={tick} className="data text-[9px] text-[var(--paper-faint)]">
              {tick}
            </span>
          ))}
        </div>
      </div>

      <p className="text-[10px] leading-relaxed text-[var(--paper-faint)]">
        Relative index, not a probability. {method}
      </p>
    </div>
  );
}

function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="flex flex-col gap-0.5 border-l border-[var(--rule)] pl-2.5">
      <dt className="text-[10px] uppercase tracking-[0.12em] text-[var(--paper-faint)]">{label}</dt>
      <dd className="data text-sm text-[var(--paper)]">
        {value}
        {unit ? <span className="ml-0.5 text-[10px] text-[var(--paper-faint)]">{unit}</span> : null}
      </dd>
    </div>
  );
}
