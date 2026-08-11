"use client";

import type { AssessResult, AssessZone, ScenarioAdvisory, ZoneHazardComponents } from "@/lib/twin";

type Props = {
  result: AssessResult | null;
  running: boolean;
  error: string | null;
  selectedZone: string | null;
  onClearZone?: () => void;
  stale: boolean;
};

const UNKNOWN_COLOR = "#8a8f93";

const EMPTY_COMPONENTS = {
  method: "",
  zone_count: 0,
  zone_count_by_basis: {},
  contributing_zone_count: {},
  area_weighted_component_mean: {},
  exposure_layer: {},
  band_thresholds: [],
} as unknown as NonNullable<AssessResult["hazard_components"]>;

export function ResultCard({ result, running, error, selectedZone, onClearZone, stale }: Props) {
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
  const releaseCoverage = (result.coverage.release_model.valid_fraction * 100).toFixed(2);
  const conditionCoverage = (result.scenario.condition_coverage.joint_supported_fraction * 100).toFixed(2);
  const completeness = result.scenario.completeness;
  const reportInputs = result.scenario.inputs as ReportInput[];
  const classification = CLASSIFICATIONS[result.scenario.classification];
  // Defensive: a cached or third-party assessment payload must degrade to a
  // thinner card, never to a blank screen with the disclaimer gone.
  const components = result.hazard_components ?? EMPTY_COMPONENTS;

  // One headline, one scope. Nothing selected reads the whole area; a selected
  // zone reads that zone. Band and colour always come from the API.
  const headline = highlighted
    ? {
        scope: `Zone ${highlighted.zone_id} hazard index`,
        value: highlighted.hazard_index,
        band: highlighted.hazard_band,
        color: highlighted.hazard_color ?? UNKNOWN_COLOR,
        note: highlighted.hazard_components?.basis_label ?? null,
      }
    : {
        scope: `Whole area · average of ${zones.length} zone${zones.length === 1 ? "" : "s"}`,
        value: result.area_hazard_index,
        band: result.area_hazard_band,
        color: result.area_hazard_color ?? UNKNOWN_COLOR,
        note: null,
      };
  const available = headline.value != null;

  return (
    <div className="flex flex-col gap-4 text-sm">
      {stale ? (
        <p className="rounded-md border border-[var(--accent-2)] bg-[var(--panel-strong)] p-2 text-[11px] font-semibold text-[var(--accent-2)]">
          Inputs changed after this assessment. This is the previous result; reassess before comparing it with the current draft.
        </p>
      ) : null}

      {/* Field evidence sits ABOVE the number on purpose. If someone recorded a
          whumpf, that is the most important thing on this card and the index is
          not allowed to be read first. */}
      <AdvisoryBlock advisories={(result.scenario.advisories ?? []) as ScenarioAdvisory[]} />

      {/* The headline: scope, number, band. Everything else is below it. */}
      <div className="rounded-lg border p-3" style={{ borderColor: headline.color }}>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-xs uppercase tracking-wide text-[var(--muted)]">{headline.scope}</span>
          {highlighted ? (
            <button
              type="button"
              onClick={onClearZone}
              className="shrink-0 rounded border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--muted)] hover:text-[var(--foreground)]"
            >
              Show whole area
            </button>
          ) : null}
        </div>
        {available ? (
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-4xl font-semibold" style={{ color: headline.color }}>
              {headline.value}
            </span>
            <span className="text-base text-[var(--muted)]">/100</span>
            {headline.band ? (
              <span
                className="ml-auto rounded px-2 py-0.5 text-xs font-semibold text-[#101415]"
                style={{ background: headline.color }}
              >
                {headline.band}
              </span>
            ) : null}
          </div>
        ) : (
          <div className="mt-2 text-lg font-semibold text-[var(--accent-2)]">
            {/* Two different absences. A below-threshold day HAS a release
                estimate and simply produced no zone; an incomplete scenario has
                no number at all. The percentile field is what tells them apart —
                zone count alone is zero in both. */}
            {result.no_zone_release_percentile_index != null
              ? "No release zone crossed the threshold"
              : "Unavailable — conditions unknown"}
          </div>
        )}
        <p className="mt-1 text-[11px] leading-snug text-[var(--muted)]">
          Uncalibrated relative index, not a danger rating and not a probability.
          {headline.note ? ` ${headline.note}.` : ""}
        </p>
      </div>

      <ComponentBars zone={highlighted ?? null} result={result} />

      {!highlighted && result.peak_zone_index != null ? (
        <p className="text-[11px] leading-relaxed text-[var(--muted)]">
          The area number is an area-weighted mean, so a large mild zone outweighs a small severe
          one. The worst single zone here is <strong>{result.peak_zone_id}</strong> at{" "}
          <strong>{result.peak_zone_index}</strong>. Click a zone on the map to read it on its own.
        </p>
      ) : null}

      {result.no_zone_release_percentile_index != null ? (
        <p className="rounded-md border border-[var(--accent-2)] bg-[var(--panel-strong)] p-2 text-[11px] leading-relaxed text-[var(--muted)]">
          No zone crossed the release threshold, so there is no hazard index. The release estimate on
          avalanche terrain reaches{" "}
          <strong>{result.no_zone_release_percentile_index}</strong> at the 95th percentile — a
          different quantity, published separately. A quiet day is a low number with a reason, never
          a statement that the mountain is safe.
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-2 text-xs">
        <Stat label="Release zones" value={String(result.release_zones.zone_count)} />
        <Stat
          label="Release potential"
          value={result.release_potential_index != null ? `${result.release_potential_index}/100` : "unavailable"}
        />
        <Stat label="Runout footprint" value={runout.core_area_m2 != null ? `${(runout.core_area_m2 / 1e6).toFixed(2)} km²` : "unavailable"} />
        <Stat label="Sensitivity envelope" value={runout.uncertainty_area_m2 != null ? `${(runout.uncertainty_area_m2 / 1e6).toFixed(2)} km²` : "unavailable"} />
      </dl>

      {/* Scope, coverage and validation status. Compressed, but nothing that
          qualifies the number is hidden behind a click. */}
      <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-[11px] leading-relaxed text-[var(--muted)]">
        <div className="flex items-center justify-between gap-2">
          <span className="font-semibold text-[var(--accent-2)]">{classification.label}</span>
          <span className="font-mono">{completeness.known_required_input_count}/{completeness.required_input_count} active inputs</span>
        </div>
        <p className="mt-1">{classification.description}</p>
        {completeness.unknown_required_parameters.length > 0 ? (
          <p className="mt-1 font-semibold text-[var(--danger)]">
            Unknown required inputs: {completeness.unknown_required_parameters.join(", ").replaceAll("_", " ")}.
          </p>
        ) : null}
        <p className="mt-1">
          Coverage: {conditionCoverage}% condition support, {releaseCoverage}% complete release
          inputs. Missing cells remain excluded, never interpreted as low hazard.
        </p>
        <p className="mt-1">
          <span className="font-semibold text-[var(--foreground)]">Field validation unavailable.</span>{" "}
          Characterized benchmarks verify software behaviour only, not physical accuracy.
        </p>
      </div>

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

      {/* Three stacked disclosures became one. The reader who wants the method,
          the provenance table or the replay hashes finds them all in one place;
          the reader who does not is no longer scrolling past three closed panels. */}
      <details className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2 text-[11px]">
        <summary className="cursor-pointer text-[var(--muted)]">
          Method, provenance and replay identity
        </summary>
        <div className="mt-2 space-y-3 text-[var(--muted)]">
          <div className="space-y-1">
            <p className="font-semibold text-[var(--foreground)]">How this index is built</p>
            <p>{components.method}</p>
            {components.dilution ? <p>{components.dilution}</p> : null}
            {components.comparability ? <p>{components.comparability}</p> : null}
            {components.simulation_cap ? <p>{components.simulation_cap}</p> : null}
            <p>
              Zones contributing each term — release {components.contributing_zone_count?.release ?? 0},
              reach {components.contributing_zone_count?.reach ?? 0}, exposure{" "}
              {components.contributing_zone_count?.exposure ?? 0} of {components.zone_count}.
            </p>
            <p>{String(components.exposure_layer?.boundary ?? "")}</p>
            {components.exposure_layer?.limitation ? (
              <p>{String(components.exposure_layer.limitation)}</p>
            ) : (
              <p>{String(components.exposure_layer?.reason ?? "")}</p>
            )}
            {components.exposure_layer?.attribution ? (
              <p>
                Exposure data {String(components.exposure_layer.attribution)},{" "}
                {String(components.exposure_layer.licence ?? "")}.
              </p>
            ) : null}
            <p>{result.hazard_detail.method}</p>
          </div>

          <div className="space-y-1">
            <p className="font-semibold text-[var(--foreground)]">Inputs, assumptions and uncertainty</p>
            {result.scenario.assumptions.length > 0 ? (
              <ul className="list-disc pl-4">
                {result.scenario.assumptions.map((assumption) => <li key={assumption}>{assumption}</li>)}
              </ul>
            ) : null}
            <p>{result.uncertainty.runout.interpretation}</p>
            <p>{result.uncertainty.release_potential.reason}</p>
            <p>{result.scenario.uncertainty_statement}</p>
            <div className="mt-1 overflow-x-auto">
              <table className="w-full border-collapse text-left text-[10px]">
                <thead><tr className="text-[var(--foreground)]"><th className="pr-2">Input</th><th className="pr-2">Status/value</th><th className="pr-2">Time/source</th><th>Scope</th></tr></thead>
                <tbody>
                  {reportInputs.map((input) => (
                    <tr key={input.input_id} className="border-t border-[var(--border)] align-top">
                      <td className="py-1 pr-2">{input.label ?? input.parameter}</td>
                      <td className="py-1 pr-2">{input.status}{input.value != null ? ` · ${String(input.value)} ${input.unit}` : " · null"}</td>
                      <td className="py-1 pr-2">{input.observed_at_utc ?? "—"}<br />{input.source?.name ?? "—"}</td>
                      <td className="py-1">{input.spatial_scope?.kind?.replaceAll("_", " ") ?? "whole area"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p>Imagery is fixed visual context only and excluded from release/runout calculations.</p>
          </div>

          <div className="space-y-1">
            <p className="font-semibold text-[var(--foreground)]">Reproducibility and model identity</p>
            <p>{result.scenario.reproducibility.statement}</p>
            <p className="font-mono">
              model {result.model.model_version} · config {result.model.config_sha256.slice(0, 12)} · bake{" "}
              {result.model.bake_sha256.slice(0, 12)}
            </p>
            <p className="break-all font-mono">scenario {result.scenario.reproducibility.scenario_sha256}</p>
            <p className="break-all font-mono">replay {result.scenario.reproducibility.numerical_replay_sha256}</p>
            <p className="font-mono">engine {result.engine} · seed {result.random_seed ?? "none"} · generated {result.generated_at_utc}</p>
          </div>
        </div>
      </details>

      <p className="border-t border-[var(--border)] pt-2 text-[11px] leading-relaxed text-[var(--muted)]">
        ⚠ {result.disclaimer}
      </p>
    </div>
  );
}

const ADVISORY_STYLE = {
  critical: { border: "var(--danger)", text: "var(--danger)", icon: "⚠" },
  warning: { border: "var(--accent-2)", text: "var(--accent-2)", icon: "!" },
  note: { border: "var(--border)", text: "var(--muted)", icon: "·" },
} as const;

/** Deterministic advisories from records the equations cannot use. */
function AdvisoryBlock({ advisories }: { advisories: ScenarioAdvisory[] }) {
  if (advisories.length === 0) return null;
  const loud = advisories.filter((item) => item.severity !== "note");
  const quiet = advisories.filter((item) => item.severity === "note");
  const overriding = advisories.some((item) => item.overrides_model);

  return (
    <div className="flex flex-col gap-2">
      {overriding ? (
        <p className="rounded-md border border-[var(--danger)] bg-[var(--panel-strong)] p-2 text-[11px] font-semibold leading-relaxed text-[var(--danger)]">
          You have recorded evidence this model cannot see. Standard practice treats that evidence
          as outranking a terrain model — read the index below as terrain context, not as the
          answer.
        </p>
      ) : null}
      {loud.map((item) => {
        const style = ADVISORY_STYLE[item.severity];
        return (
          <div
            key={item.advisory_id}
            className="rounded-md border bg-[var(--panel-strong)] p-2 text-[11px] leading-relaxed"
            style={{ borderColor: style.border }}
          >
            <p className="font-semibold" style={{ color: style.text }}>
              {style.icon} {item.title}
            </p>
            <p className="mt-0.5 text-[var(--muted)]">{item.detail}</p>
          </div>
        );
      })}
      {quiet.length > 0 ? (
        <details className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2 text-[11px]">
          <summary className="cursor-pointer text-[var(--muted)]">
            {quiet.length} further record{quiet.length === 1 ? "" : "s"} retained
          </summary>
          <div className="mt-2 space-y-1 text-[var(--muted)]">
            {quiet.map((item) => (
              <p key={item.advisory_id}>{item.detail}</p>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

/** Release, reach and exposure as three bars, one plain sentence each. */
function ComponentBars({ zone, result }: { zone: AssessZone | null; result: AssessResult }) {
  const components = zone?.hazard_components ?? null;
  const areaMeans = result.hazard_components?.area_weighted_component_mean ?? {};
  const rows: { key: string; label: string; value: number | null; sentence: string }[] = [
    {
      key: "release",
      label: "Release",
      value: components ? components.release.value : numberOrNull(areaMeans.release),
      sentence: releaseSentence(zone, result),
    },
    {
      key: "reach",
      label: "Reach",
      value: components ? components.reach.value ?? null : numberOrNull(areaMeans.reach),
      sentence: reachSentence(components, result),
    },
    {
      key: "exposure",
      label: "Exposure",
      value: components ? components.exposure.value ?? null : numberOrNull(areaMeans.exposure),
      sentence: exposureSentence(components, result),
    },
  ];

  return (
    <div className="flex flex-col gap-2">
      {rows.map((row) => (
        <div key={row.key}>
          <div className="flex items-baseline justify-between text-xs">
            <span className="text-[var(--foreground)]">{row.label}</span>
            <span className="font-mono text-[var(--muted)]">
              {row.value != null ? row.value.toFixed(2) : "unknown"}
            </span>
          </div>
          <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-[var(--panel-strong)]">
            {row.value != null ? (
              <div
                className="h-full rounded-full bg-[var(--accent)]"
                style={{ width: `${Math.max(2, Math.min(100, row.value * 100))}%` }}
              />
            ) : (
              <div className="h-full w-full bg-[repeating-linear-gradient(45deg,var(--border),var(--border)3px,transparent_3px,transparent_6px)]" />
            )}
          </div>
          <p className="mt-0.5 text-[11px] leading-snug text-[var(--muted)]">{row.sentence}</p>
        </div>
      ))}
    </div>
  );
}

function numberOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" ? value : null;
}

function releaseSentence(zone: AssessZone | null, result: AssessResult): string {
  if (zone) {
    return zone.main_reasons[0] ?? `Estimated release score ${zone.estimated_release_score}/100.`;
  }
  const count = result.zones.length;
  return count > 0
    ? `Terrain and loading across ${count} release zone${count === 1 ? "" : "s"}.`
    : "No terrain crossed the release threshold under these conditions.";
}

function reachSentence(components: ZoneHazardComponents | null, result: AssessResult): string {
  if (components) {
    if (!components.reach.available) return String(components.reach.unavailable_reason ?? "Reach unknown.");
    const measured = components.reach.measurements ?? {};
    const distance = numberOrNull(measured.horizontal_reach_m as number | null);
    const drop = numberOrNull(measured.vertical_drop_m as number | null);
    const speed = numberOrNull(measured.max_velocity_ms as number | null);
    const parts: string[] = [];
    if (distance != null) parts.push(`run ${(distance / 1000).toFixed(2)} km`);
    if (drop != null) parts.push(`fall ${drop.toFixed(0)} m`);
    if (speed != null) parts.push(`reach ${speed.toFixed(0)} m/s`);
    return parts.length ? `Modelled to ${parts.join(", ")}.` : "Reach measured, but no distance reported.";
  }
  const contributing = result.hazard_components?.contributing_zone_count?.reach ?? 0;
  const total = result.hazard_components?.zone_count ?? 0;
  if (contributing === 0) return "No zone has a simulated runout, so reach is unknown — not zero.";
  return `Averaged over the ${contributing} of ${total} zones whose runout was simulated.`;
}

function exposureSentence(components: ZoneHazardComponents | null, result: AssessResult): string {
  if (components) {
    if (!components.exposure.available) {
      return String(components.exposure.unavailable_reason ?? "Exposure unknown.");
    }
    const classes = components.exposure.classes ?? [];
    if (classes.length === 0) {
      return "The exposure extract covers this path and maps nothing in it, so nothing was added.";
    }
    return `Path crosses ${joinList(classes.map((label) => label.toLowerCase()))}.`;
  }
  const exposed = result.hazard_components?.zones_with_mapped_exposure;
  const contributing = result.hazard_components?.contributing_zone_count?.exposure ?? 0;
  if (contributing === 0) {
    return "No exposure evidence in this bake, so nothing was added to any zone.";
  }
  return `${exposed ?? 0} of ${contributing} measured zones cross something mapped.`;
}

function joinList(items: string[]): string {
  if (items.length === 1) return items[0];
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

type ReportInput = {
  input_id: string;
  parameter: string;
  label?: string;
  status: string;
  value?: unknown;
  unit: string;
  observed_at_utc?: string | null;
  source?: { name?: string } | null;
  spatial_scope?: { kind?: string };
};

const CLASSIFICATIONS = {
  terrain_only: {
    label: "Terrain-only",
    description: "Static LiDAR terrain is available, but current loading is not evaluable and no release/runout number is claimed.",
  },
  hypothetical: {
    label: "Hypothetical scenario",
    description: "Active values are assumptions or non-observational estimates.",
  },
  partially_observation_constrained: {
    label: "Partially observation-constrained",
    description: "At least one active input is measurement-traceable, but assumptions, unknowns, or incomplete spatial coverage remain.",
  },
  fully_specified_research_scenario: {
    label: "Fully specified research scenario",
    description: "Complete only for this simplified model—not a complete snowpack, validation, probability, or operational forecast.",
  },
} as const;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2.5 py-2">
      <dt className="text-[var(--muted)]">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm text-[var(--foreground)]">{value}</dd>
    </div>
  );
}
