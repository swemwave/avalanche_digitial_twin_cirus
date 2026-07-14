"use client";

import type { Analysis, InstabilityComponent, Simulation } from "@/lib/apiV1";

/**
 * The results panel.
 *
 * Rule for everything here: a number never appears without what it means. The
 * disclaimer, the limitations, the confidence and the list of inputs that were
 * EXCLUDED for being missing are rendered alongside the score, not behind a
 * disclosure triangle. A hazard of 63/100 with a confidence of 46/100 and no
 * snow-profile data is a very different statement from "63", and the panel must not
 * let anyone read the second one.
 */

type Props = {
  analysis: Analysis | null;
  simulation: Simulation | null;
};

function Score({
  label,
  value,
  suffix = "/ 100",
  tone = "default",
  hint,
}: {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  tone?: "default" | "hazard" | "confidence";
  hint?: string;
}) {
  const withheld = value === null || value === undefined;
  const color =
    tone === "hazard"
      ? value != null && value >= 70
        ? "text-[var(--danger)]"
        : value != null && value >= 50
          ? "text-[var(--accent-2)]"
          : "text-[var(--accent)]"
      : tone === "confidence"
        ? value != null && value < 50
          ? "text-[var(--danger)]"
          : "text-[var(--accent-2)]"
        : "text-[var(--foreground)]";

  return (
    <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3">
      <p className="text-[11px] uppercase tracking-wide text-[var(--muted)]">{label}</p>
      <p className={`mt-1 text-2xl font-semibold ${withheld ? "text-[var(--muted)]" : color}`}>
        {withheld ? "withheld" : Math.round(value)}
        {!withheld ? <span className="ml-1 text-xs font-normal text-[var(--muted)]">{suffix}</span> : null}
      </p>
      {hint ? <p className="mt-1 text-[11px] leading-snug text-[var(--muted)]">{hint}</p> : null}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-md border border-[var(--border)] bg-[var(--panel)] p-3">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">{title}</h4>
      {children}
    </section>
  );
}

export function ResultsPanel({ analysis, simulation }: Props) {
  if (!analysis) {
    return (
      <div className="rounded-md border border-dashed border-[var(--border)] p-6 text-sm text-[var(--muted)]">
        No analysis yet. Choose conditions on the left and run one — it takes about 90 seconds, and
        the progress bar shows which stage the model is at.
      </div>
    );
  }

  const assessment = simulation?.assessment;
  const withheld = analysis.instability.score_withheld;
  const excluded = analysis.instability.components.filter((component) => !component.available);
  const included = analysis.instability.components.filter((component) => component.available);

  return (
    <div className="flex flex-col gap-3 text-sm">
      {/* The one combination that is a trap: a big number nobody should trust. */}
      {assessment && assessment.combinedRiskScore >= 60 && assessment.confidenceScore < 50 ? (
        <div className="rounded-md border border-[var(--danger)] bg-[var(--danger)]/10 p-3 text-xs leading-relaxed text-[var(--danger)]">
          <strong>High risk, low confidence.</strong> The model reports{" "}
          {Math.round(assessment.combinedRiskScore)}/100 risk but only{" "}
          {Math.round(assessment.confidenceScore)}/100 confidence in that number. Treat this as a flag
          to investigate, not as a quantified hazard. Consult Avalanche Canada and make a field
          assessment.
        </div>
      ) : null}

      {withheld ? (
        <div className="rounded-md border border-[var(--accent-2)] bg-[var(--accent-2)]/10 p-3 text-xs leading-relaxed text-[var(--accent-2)]">
          <strong>Score withheld.</strong> Too little of the instability model&apos;s input was
          available ({Math.round(analysis.instability.available_weight_fraction * 100)}% of the
          configured weight). A number computed from this little input would be misleading, so none is
          reported. <strong>This is not a hazard of zero</strong> — it is an absence of information.
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <Score
          label="Risk"
          value={assessment?.combinedRiskScore}
          tone="hazard"
          hint={assessment ? assessment.riskLevel : "Run a simulation"}
        />
        <Score label="Hazard" value={analysis.hazard_score} tone="hazard" hint="Estimated release score" />
        <Score
          label="Consequence"
          value={assessment?.consequenceScore}
          tone="hazard"
          hint={assessment ? `${assessment.consequenceClass} · lower bound` : "Run a simulation"}
        />
        <Score
          label="Confidence"
          value={analysis.confidence_score}
          tone="confidence"
          hint={`Capped at ${analysis.confidence_breakdown.maximum_without_calibration} — uncalibrated`}
        />
      </div>

      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        These are <strong>relative indices, not probabilities</strong>. Nothing here is a forecast.
      </p>

      {assessment?.mainReasons?.length ? (
        <Section title="Why">
          <ul className="flex list-disc flex-col gap-1 pl-4 text-xs leading-relaxed text-[var(--foreground)]">
            {assessment.mainReasons.slice(0, 6).map((reason, index) => (
              // The text is not a key: two zones on the same aspect genuinely produce
              // the same reason, and React was colliding on the duplicate.
              <li key={`${index}-${reason}`}>{reason}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title={`Model inputs (${included.length} used, ${excluded.length} excluded)`}>
        <ul className="flex flex-col gap-1.5">
          {analysis.instability.components.map((component) => (
            <ComponentRow key={component.component} component={component} />
          ))}
        </ul>
        <p className="mt-2 border-t border-[var(--border)] pt-2 text-[11px] leading-relaxed text-[var(--muted)]">
          {analysis.instability.missing_data_policy}
        </p>
      </Section>

      <Section title="Confidence breakdown">
        <ul className="flex flex-col gap-1">
          {Object.entries(analysis.confidence_breakdown.components).map(([key, item]) => (
            <li key={key} className="text-xs">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[var(--foreground)]">{key.replace(/_/g, " ")}</span>
                <span className="tabular-nums text-[var(--muted)]">
                  {Math.round(item.score)} · w{item.weight.toFixed(2)}
                </span>
              </div>
              <p className="text-[11px] leading-snug text-[var(--muted)]">{item.detail}</p>
            </li>
          ))}
        </ul>
        {analysis.confidence_breakdown.penalties.length ? (
          <ul className="mt-2 flex list-disc flex-col gap-1 border-t border-[var(--border)] pl-4 pt-2 text-[11px] text-[var(--accent-2)]">
            {analysis.confidence_breakdown.penalties.map((penalty, index) => (
              <li key={`${index}-${penalty}`}>{penalty}</li>
            ))}
          </ul>
        ) : null}
        <p className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">
          {analysis.confidence_breakdown.ceiling_reason}
        </p>
      </Section>

      <Section title="Limitations">
        <ul className="flex list-disc flex-col gap-1 pl-4 text-[11px] leading-relaxed text-[var(--muted)]">
          {(assessment?.limitations ?? analysis.instability.limitations).map((limitation, index) => (
            <li key={`${index}-${limitation}`}>{limitation}</li>
          ))}
        </ul>
      </Section>

      {analysis.warnings.length ? (
        <Section title={`Warnings (${analysis.warnings.length})`}>
          <ul className="flex list-disc flex-col gap-1 pl-4 text-[11px] leading-relaxed text-[var(--accent-2)]">
            {analysis.warnings.map((warning, index) => (
              <li key={`${index}-${warning}`}>{warning}</li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section title="Provenance">
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
          <dt className="text-[var(--muted)]">Model version</dt>
          <dd className="tabular-nums">{analysis.model.model_version}</dd>
          <dt className="text-[var(--muted)]">Config hash</dt>
          <dd className="truncate font-mono">{analysis.model.config_sha256.slice(0, 16)}</dd>
          <dt className="text-[var(--muted)]">Terrain</dt>
          <dd>
            {(analysis.terrain.lidar_fraction * 100).toFixed(1)}% LiDAR @{" "}
            {analysis.terrain.effective_source_resolution_m?.toFixed?.(2) ?? "?"} m
          </dd>
          <dt className="text-[var(--muted)]">Valid time</dt>
          <dd>{new Date(analysis.valid_time_utc).toISOString().slice(0, 16).replace("T", " ")}Z</dd>
          {simulation ? (
            <>
              <dt className="text-[var(--muted)]">Engine</dt>
              <dd>{simulation.engine}</dd>
              <dt className="text-[var(--muted)]">Seed</dt>
              <dd className="tabular-nums">{simulation.random_seed}</dd>
            </>
          ) : null}
        </dl>
        {simulation ? (
          <p className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">
            {simulation.reproducibility.note}
          </p>
        ) : null}
      </Section>

      <p className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-3 text-[11px] leading-relaxed text-[var(--muted)]">
        {analysis.disclaimer}
      </p>
    </div>
  );
}

function ComponentRow({ component }: { component: InstabilityComponent }) {
  return (
    <li className="text-xs">
      <div className="flex items-baseline justify-between gap-2">
        <span className={component.available ? "text-[var(--foreground)]" : "text-[var(--muted)] line-through"}>
          {component.title}
        </span>
        <span className="shrink-0 tabular-nums text-[var(--muted)]">
          {component.available ? (
            <>
              w{component.configured_weight.toFixed(2)} · {component.provenance}
            </>
          ) : (
            <span className="text-[var(--accent-2)]">excluded</span>
          )}
        </span>
      </div>
      {!component.available ? (
        // Not "scored as zero". Excluded from both sides of the weighted mean.
        <p className="text-[11px] leading-snug text-[var(--accent-2)]">
          {component.missing_reason} Excluded from the score — <strong>not scored as zero</strong>.
        </p>
      ) : null}
    </li>
  );
}
