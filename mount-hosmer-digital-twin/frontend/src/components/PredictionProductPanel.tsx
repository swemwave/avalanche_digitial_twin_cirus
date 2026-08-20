"use client";

import { useEffect, useState } from "react";
import {
  getPredictionComparison,
  getPredictionProduct,
  listPredictionProducts,
  type PredictionProductDetail,
  type PredictionProductSummary,
  type RunoutComparisonDetail,
} from "@/lib/twin";

/** Quantities a runout engine may publish, in the order they are shown. */
const OUTPUT_LABELS: Record<string, string> = {
  runout_extent: "Runout extent",
  flow_depth: "Flow depth",
  flow_velocity: "Flow velocity",
  flow_pressure: "Flow pressure",
  energy_line_height: "Energy-line height",
  travel_angle: "Travel angle",
  arrival_time: "Arrival time",
};
const OUTPUT_ORDER = Object.keys(OUTPUT_LABELS);

const STAGE_LABELS: Record<string, string> = {
  mountain_pack: "Terrain pack",
  condition_pack: "Weather forcing",
  snow_state_pack: "Snow state",
  release: "Release",
  runout: "Runout",
  comparison: "Engine comparison",
};

const STATUS_TONE: Record<string, string> = {
  completed: "text-[var(--foreground)]",
  skipped: "text-[var(--muted)]",
  unavailable: "text-[var(--accent-2)]",
  failed: "text-[var(--danger)]",
};

const formatMetric = (value: number | null, unit: string) => {
  // A missing metric prints as a word, never as 0 — the whole point of the
  // unsupported/not-applicable statuses is that a reader cannot mistake one for
  // a measured zero.
  if (value === null || value === undefined || !Number.isFinite(value)) return "unavailable";
  const magnitude = Math.abs(value);
  const digits = magnitude >= 100 ? 0 : magnitude >= 1 ? 2 : 4;
  return `${value.toFixed(digits)}${unit === "1" ? "" : ` ${unit}`}`;
};

export function PredictionProductPanel() {
  const [products, setProducts] = useState<PredictionProductSummary[] | null>(null);
  const [statement, setStatement] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<PredictionProductDetail | null>(null);
  const [comparison, setComparison] = useState<RunoutComparisonDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listPredictionProducts()
      .then((body) => {
        setProducts(body.products);
        setStatement(body.statement);
        setSelected((current) => current ?? body.products[0]?.product_id ?? null);
      })
      .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    // Every setState here runs after an await, so switching products never
    // triggers a synchronous cascade, and a stale response cannot overwrite a
    // newer selection.
    let cancelled = false;
    void (async () => {
      try {
        const body = await getPredictionProduct(selected);
        if (cancelled) return;
        setError(null);
        setDetail(body);
        const first = body.comparisons[0];
        const next = first ? await getPredictionComparison(selected, first.comparison_id) : null;
        if (!cancelled) setComparison(next);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selected]);

  if (error) {
    return (
      <p className="rounded-md border border-[var(--danger)] bg-[var(--panel-strong)] px-3 py-2 text-xs text-[var(--danger)]">
        {error}
      </p>
    );
  }
  if (products === null) {
    return <p className="text-sm text-[var(--muted)]">Looking for offline prediction products…</p>;
  }
  if (products.length === 0) {
    return (
      <p className="text-sm text-[var(--muted)]">
        No offline prediction product is published for this deployment. External research
        engines are run offline, never inside a request.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3 text-sm">
      <p className="text-xs text-[var(--accent-2)]">{statement}</p>

      {products.length > 1 && (
        <label className="flex flex-col gap-1 text-xs text-[var(--muted)]">
          Product
          <select
            className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1 text-xs text-[var(--foreground)]"
            value={selected ?? ""}
            onChange={(event) => setSelected(event.target.value)}
          >
            {products.map((product) => (
              <option key={product.product_id} value={product.product_id}>
                {product.product_id.slice(19, 31)} · {product.engine_ids.join(" + ")}
              </option>
            ))}
          </select>
        </label>
      )}

      {detail && (
        <>
          <section>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
              Pipeline stages
            </h4>
            <ul className="mt-1 flex flex-col gap-1">
              {detail.stages.map((stage) => (
                <li key={stage.stage} className="text-xs">
                  <span className="font-medium">{STAGE_LABELS[stage.stage] ?? stage.stage}</span>{" "}
                  <span className={STATUS_TONE[stage.status] ?? ""}>{stage.status}</span>
                  <p className="text-[var(--muted)]">{stage.reason}</p>
                </li>
              ))}
            </ul>
          </section>

          <section>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
              Release
            </h4>
            {detail.release === null ? (
              <p className="text-xs text-[var(--accent-2)]">
                No release result. See the stage reasons above; this is not a finding that nothing
                could release.
              </p>
            ) : (
              <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
                <dt className="text-[var(--muted)]">Engine</dt>
                <dd>{detail.release.engine_id}</dd>
                <dt className="text-[var(--muted)]">Area</dt>
                <dd>{detail.release.release_area_m2.toFixed(0)} m²</dd>
                <dt className="text-[var(--muted)]">Volume</dt>
                <dd>
                  {detail.release.release_volume_m3 === null
                    ? "unavailable"
                    : `${detail.release.release_volume_m3.toFixed(0)} m³`}
                </dd>
                <dt className="text-[var(--muted)]">Instability index</dt>
                <dd>
                  {detail.release.has_release_index
                    ? "published (relative, uncalibrated)"
                    : "unavailable"}
                </dd>
                <dt className="text-[var(--muted)]">Thickness</dt>
                <dd>{detail.release.has_release_thickness ? "published" : "unavailable"}</dd>
                <dt className="text-[var(--muted)]">Density</dt>
                <dd>{detail.release.has_release_density ? "published" : "unavailable"}</dd>
                <dt className="text-[var(--muted)]">Release probability</dt>
                <dd className="text-[var(--accent-2)]">unavailable</dd>
              </dl>
            )}
            {detail.release && (
              <p className="mt-1 text-[11px] text-[var(--muted)]">
                {detail.release.release_probability_unavailable_reason}
              </p>
            )}
          </section>

          <section>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
              Runout engines
            </h4>
            {detail.engines.length === 0 ? (
              <p className="text-xs text-[var(--accent-2)]">No runout engine produced a result.</p>
            ) : (
              detail.engines.map((engine) => {
                const unsupported = new Map(
                  engine.unsupported_outputs.map((item) => [item.quantity, item.reason]),
                );
                return (
                  <div
                    key={engine.engine_id}
                    className="mt-2 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2"
                  >
                    <p className="text-xs font-medium">
                      {engine.engine_id}{" "}
                      <span className="text-[var(--muted)]">
                        v{engine.engine_version} · {engine.license_spdx}
                      </span>
                    </p>
                    <p className="text-[11px] text-[var(--muted)]">
                      Runout {engine.runout_area_m2.toFixed(0)} m² ·{" "}
                      {engine.aoi_status === "truncated_at_domain"
                        ? "truncated at the computational domain"
                        : engine.aoi_status.replace(/_/g, " ")}
                    </p>
                    <ul className="mt-1 flex flex-col gap-0.5 text-[11px]">
                      {OUTPUT_ORDER.map((quantity) => {
                        const available = engine.available_outputs.includes(quantity);
                        const reason = unsupported.get(quantity);
                        if (!available && reason === undefined) return null;
                        return (
                          <li key={quantity}>
                            <span className="text-[var(--muted)]">
                              {OUTPUT_LABELS[quantity]}:
                            </span>{" "}
                            {available ? (
                              <span>available</span>
                            ) : (
                              <span className="text-[var(--accent-2)]" title={reason}>
                                unsupported
                              </span>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                );
              })
            )}
          </section>

          {detail.ensembles.length > 0 && (
            <section>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Sensitivity envelopes
              </h4>
              {detail.ensembles.map((ensemble) => (
                <div key={`${ensemble.engine_id}:${ensemble.parameter}`} className="mt-2 text-[11px]">
                  <p className="font-medium">
                    {ensemble.engine_id} · {ensemble.parameter.replace(/_/g, " ")}
                    <span className="text-[var(--muted)]">
                      {" "}
                      ·{" "}
                      {ensemble.varies === "release_input"
                        ? "varies the release input"
                        : "varies an engine parameter"}{" "}
                      · {ensemble.basis}
                    </span>
                    {detail.dominant_uncertainty_contributor ===
                      `${ensemble.engine_id}:${ensemble.parameter}` && (
                      <span className="text-[var(--accent-2)]"> · dominant contributor</span>
                    )}
                  </p>
                  <p className="text-[var(--muted)]">
                    Central {ensemble.central_runout_area_m2.toFixed(0)} m² · members{" "}
                    {ensemble.minimum_runout_area_m2.toFixed(0)}–
                    {ensemble.maximum_runout_area_m2.toFixed(0)} m² · outer envelope{" "}
                    {ensemble.envelope_area_m2.toFixed(0)} m²
                  </p>
                  <ul className="mt-0.5 flex flex-col gap-0.5 text-[var(--muted)]">
                    {ensemble.members.map((member) => (
                      <li key={member.member_id}>
                        {member.parameter} = {member.value}
                        {member.unit === "1" ? "" : ` ${member.unit}`}
                        {member.is_central ? " (central)" : ""} →{" "}
                        {member.runout_area_m2.toFixed(0)} m² ·{" "}
                        <code>{member.member_id}</code>
                      </li>
                    ))}
                  </ul>
                  <p className="mt-0.5 text-[var(--accent-2)]">{ensemble.member_frequency_note}</p>
                  <p className="text-[var(--muted)]">{ensemble.source}</p>
                </div>
              ))}
            </section>
          )}

          {detail.unsupported_ensembles.length > 0 && (
            <section>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Spans that were not swept
              </h4>
              {/* Shown rather than hidden: an omitted sweep reads as "this
                  parameter does not matter", which is a claim nobody made. */}
              {detail.unsupported_ensembles.map((declined) => (
                <div
                  key={`${declined.engine_id}:${declined.parameter}`}
                  className="mt-2 text-[11px]"
                >
                  <p className="font-medium">
                    {declined.engine_id} · {declined.parameter.replace(/_/g, " ")}
                  </p>
                  <p className="text-[var(--muted)]">{declined.reason}</p>
                  <p className="text-[var(--muted)]">
                    To enable: {declined.required_to_enable}
                  </p>
                </div>
              ))}
            </section>
          )}

          {comparison && (
            <section>
              <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Engine disagreement
              </h4>
              <p className="text-[11px] text-[var(--muted)]">
                {comparison.left_engine_id} vs {comparison.right_engine_id} on{" "}
                {comparison.common_valid_cells.toLocaleString()} common valid cells.
              </p>
              <ul className="mt-1 flex flex-col gap-0.5 text-[11px]">
                {comparison.metrics.map((metric) => (
                  <li key={metric.name} title={metric.semantics}>
                    <span className="text-[var(--muted)]">
                      {metric.name.replace(/_/g, " ")}:
                    </span>{" "}
                    {metric.status === "available" ? (
                      formatMetric(metric.value, metric.unit)
                    ) : (
                      <span className="text-[var(--accent-2)]">{metric.status.replace(/_/g, " ")}</span>
                    )}
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-[11px] text-[var(--accent-2)]">
                {comparison.limitations[0]}
              </p>
            </section>
          )}

          <section>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
              Validation and limitations
            </h4>
            <p className="text-[11px]">
              Validation level:{" "}
              <span className="text-[var(--accent-2)]">
                {detail.summary.validation_level.replace(/_/g, " ")}
              </span>{" "}
              · eligible field events: {detail.summary.eligible_field_events}
            </p>
            <ul className="mt-1 list-disc pl-4 text-[11px] text-[var(--muted)]">
              {detail.limitations.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            {detail.warnings.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-[11px] text-[var(--accent-2)]">
                {detail.warnings.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            )}
          </section>

          <p className="rounded-md border border-[var(--accent-2)] bg-[var(--panel-strong)] px-2 py-1 text-[11px] text-[var(--accent-2)]">
            {detail.summary.disclaimer}
          </p>
        </>
      )}
    </div>
  );
}
