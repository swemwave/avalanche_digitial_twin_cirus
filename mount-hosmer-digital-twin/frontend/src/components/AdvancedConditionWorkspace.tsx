"use client";

import { useMemo, useState } from "react";
import { ConditionsHelp } from "@/components/ConditionsHelp";
import type {
  AssessRequest,
  InputSource,
  InputUncertainty,
  ScenarioInput,
  SimulationMode,
  SpatialScope,
} from "@/lib/twin";

type Parameter = ScenarioInput["parameter"];
type Status = ScenarioInput["status"];
type Category = ScenarioInput["category"];
type SourceKind = InputSource["kind"];
type UncertaintyKind = InputUncertainty["kind"];

type Role = "active" | "advisory";

type Definition = {
  parameter: Parameter;
  category: Category;
  label: string;
  unit: string;
  kind: "number" | "category" | "boolean";
  options?: string[];
  /** "active" enters the equations; "advisory" can only raise a written warning. */
  role: Role;
  active: boolean;
  hint: string;
};

type Draft = {
  status: Status;
  value: string;
  observedAtUtc: string;
  sourceName: string;
  sourceKind: SourceKind;
  uncertaintyKind: UncertaintyKind;
  uncertaintyValue: string;
  uncertaintyBasis: string;
  scopeKind: NonNullable<SpatialScope["kind"]>;
  elevationMin: string;
  elevationMax: string;
  aspectMin: string;
  aspectMax: string;
  notes: string;
};

const DEFINITIONS: Definition[] = [
  {
    parameter: "new_snow_depth",
    category: "weather_loading",
    label: "New storm-snow depth",
    unit: "cm",
    kind: "number",
    role: "active",
    active: true,
    hint: "Main loading term. Saturates at 50 cm — more than that scores the same.",
  },
  {
    parameter: "wind_speed",
    category: "weather_loading",
    label: "Representative transport wind",
    unit: "km/h",
    kind: "number",
    role: "active",
    active: true,
    hint: "A representative sustained speed, not a gust. Nothing transports below 15 km/h; full transport by 40.",
  },
  {
    parameter: "wind_direction",
    category: "weather_loading",
    label: "Wind direction (FROM)",
    unit: "degree_true",
    kind: "number",
    role: "active",
    active: true,
    hint: "The direction wind blows FROM. A 225° wind loads NE-facing slopes. Optional only at ≤15 km/h.",
  },
  {
    parameter: "air_temperature",
    category: "weather_loading",
    label: "Air temperature",
    unit: "degC",
    kind: "number",
    role: "active",
    active: true,
    hint: "Splits new precipitation into snow and rain across 0–2 °C. Rain builds no dry slab, so loading drops — that is not a safer day.",
  },
  {
    parameter: "snow_depth",
    category: "snowpack_weak_layers",
    label: "Total snow depth",
    unit: "cm",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Snowpack context. Kept with provenance; no term in the equations.",
  },
  {
    parameter: "snow_water_equivalent",
    category: "snowpack_weak_layers",
    label: "Snow water equivalent",
    unit: "mm",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Snowpack context. Kept with provenance; no term in the equations.",
  },
  {
    parameter: "slab_depth",
    category: "snowpack_weak_layers",
    label: "Slab depth",
    unit: "cm",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Snowpack context. The model has no slab-layer mechanics.",
  },
  {
    parameter: "weak_layer_depth",
    category: "snowpack_weak_layers",
    label: "Weak-layer depth",
    unit: "cm",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Raises a warning on the result. The model has no snowpack layers and is blind to weak-layer failure.",
  },
  {
    parameter: "weak_layer_type",
    category: "snowpack_weak_layers",
    label: "Weak-layer type",
    unit: "category",
    kind: "category",
    options: ["surface_hoar", "facets", "depth_hoar", "crust_interface", "other"],
    role: "advisory",
    active: false,
    hint: "Surface hoar, facets and depth hoar raise a critical warning — they are what the model most needs and least has.",
  },
  {
    parameter: "stability_test_result",
    category: "snowpack_weak_layers",
    label: "Stability-test result",
    unit: "category",
    kind: "category",
    options: ["ECTP", "ECTN", "ECTX", "PST_end", "PST_arr", "compression_test", "other"],
    role: "advisory",
    active: false,
    hint: "Raises a warning. A propagating result (ECTP, PST end) raises a critical one.",
  },
  {
    parameter: "recent_avalanche_activity",
    category: "field_observations",
    label: "Recent avalanche activity",
    unit: "boolean",
    kind: "boolean",
    role: "advisory",
    active: false,
    hint: "A classic sign of current instability. Raises a critical warning above the number.",
  },
  {
    parameter: "shooting_cracks",
    category: "field_observations",
    label: "Shooting cracks",
    unit: "boolean",
    kind: "boolean",
    role: "advisory",
    active: false,
    hint: "A classic sign of current instability. Raises a critical warning above the number.",
  },
  {
    parameter: "whumpfing",
    category: "field_observations",
    label: "Whumpfing",
    unit: "boolean",
    kind: "boolean",
    role: "advisory",
    active: false,
    hint: "A classic sign of current instability. Raises a critical warning above the number.",
  },
  {
    parameter: "release_size",
    category: "release_assumptions",
    label: "Release-size class",
    unit: "category",
    kind: "category",
    options: ["small", "medium", "large", "very_large"],
    role: "active",
    active: true,
    hint: "Chooses the regional angle of reach for runout. An assumption you pick, not an observation.",
  },
  {
    parameter: "trigger_type",
    category: "release_assumptions",
    label: "Trigger type",
    unit: "category",
    kind: "category",
    options: ["natural", "skier", "remote", "explosive", "unknown_trigger"],
    role: "advisory",
    active: false,
    hint: "Release context. The model estimates whether terrain can release, not what triggers it.",
  },
  {
    parameter: "release_depth",
    category: "release_assumptions",
    label: "Release depth",
    unit: "cm",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Release context. Not a calibrated input to either release or runout.",
  },
  {
    parameter: "flow_regime",
    category: "flow_runout",
    label: "Observed/assumed flow regime",
    unit: "category",
    kind: "category",
    options: ["dry_slab", "wet_snow", "powder", "mixed", "unknown_regime"],
    role: "active",
    active: true,
    hint: "Points runout at a different published friction set. Wet stops sooner, powder runs further.",
  },
  {
    parameter: "avalanche_density",
    category: "flow_runout",
    label: "Avalanche density",
    unit: "kg/m3",
    kind: "number",
    role: "advisory",
    active: false,
    hint: "Flow context. The runout engines model geometry and friction, not mass.",
  },
  {
    parameter: "runout_alpha_angle",
    category: "flow_runout",
    label: "User alpha angle",
    unit: "degree",
    kind: "number",
    role: "active",
    active: true,
    hint: "Overrides the regional angle of reach, clamped to 15–40°. Smaller runs further. A sensitivity run, not a calibration.",
  },
];

/**
 * Grouped by what an input DOES, not by meteorological category.
 *
 * The old layout nested a per-parameter <details> inside a per-category <details>,
 * so setting new-snow depth took two disclosures and every row carried the full
 * provenance apparatus inline. Now the four required inputs are always open and
 * editable on one line each, and time/source/uncertainty/scope/notes live behind a
 * per-row toggle. Nothing was removed -- every field is still here and still
 * round-trips into the scenario contract.
 */
const GROUPS: { key: string; title: string; note: string; parameters: Parameter[] }[] = [
  {
    key: "required",
    title: "Required",
    note: "These four produce the result.",
    parameters: ["new_snow_depth", "wind_speed", "wind_direction", "release_size"],
  },
  {
    key: "optional",
    title: "Optional model inputs",
    note: "Each changes the result. Leave blank and it is as if you never set it.",
    parameters: ["air_temperature", "flow_regime", "runout_alpha_angle"],
  },
  {
    key: "evidence",
    title: "Field & snowpack evidence",
    note: "The model has no term for these, so recording one raises a written warning on the result instead of a silently invented adjustment.",
    parameters: [
      "whumpfing",
      "shooting_cracks",
      "recent_avalanche_activity",
      "weak_layer_type",
      "weak_layer_depth",
      "stability_test_result",
      "slab_depth",
      "snow_depth",
      "snow_water_equivalent",
      "trigger_type",
      "release_depth",
      "avalanche_density",
    ],
  },
];

const BY_PARAMETER = new Map(DEFINITIONS.map((definition) => [definition.parameter, definition]));

/** Contract unit strings are machine names; show people the readable form. */
const UNIT_LABELS: Record<string, string> = {
  degree_true: "° true",
  degree: "°",
  degC: "°C",
  "kg/m3": "kg/m³",
  boolean: "yes / no",
  category: "select",
};
const unitLabel = (unit: string) => UNIT_LABELS[unit] ?? unit;

const initialDraft = (definition: Definition): Draft => {
  const release = definition.parameter === "release_size";
  return {
    status: release ? "assumed" : "unknown",
    value: release ? "medium" : "",
    observedAtUtc: "",
    sourceName: "",
    sourceKind: "derived_from_measurement",
    uncertaintyKind: release ? "not_provided" : "unknown",
    uncertaintyValue: "",
    uncertaintyBasis: release ? "Explicit user-selected runout sensitivity assumption." : "Not characterized.",
    scopeKind: "whole_area",
    elevationMin: "",
    elevationMax: "",
    aspectMin: "",
    aspectMax: "",
    notes: "",
  };
};

/** Provenance a measured/estimated value owes before it can be submitted. */
const missingProvenance = (draft: Draft): boolean =>
  (draft.status === "measured" || draft.status === "estimated") &&
  (!draft.observedAtUtc || !draft.sourceName.trim());

type Props = {
  running: boolean;
  onAssess: (request: AssessRequest) => void;
  drawnArea: GeoJSON.Polygon | null;
  onRequestDraw: () => void;
  onDraftChange: () => void;
};

export function AdvancedConditionWorkspace({ running, onAssess, drawnArea, onRequestDraw, onDraftChange }: Props) {
  const [drafts, setDrafts] = useState<Record<Parameter, Draft>>(() =>
    Object.fromEntries(DEFINITIONS.map((definition) => [definition.parameter, initialDraft(definition)])) as Record<Parameter, Draft>,
  );
  const [expanded, setExpanded] = useState<Partial<Record<Parameter, boolean>>>({});
  const [simulationMode, setSimulationMode] = useState<SimulationMode>("fast");
  const [seed, setSeed] = useState("42");
  const [name, setName] = useState("");
  const [validAtUtc, setValidAtUtc] = useState("");
  const [error, setError] = useState<string | null>(null);

  const statusCounts = useMemo(
    () =>
      Object.values(drafts).reduce<Record<Status, number>>(
        (counts, draft) => ({ ...counts, [draft.status]: counts[draft.status] + 1 }),
        { measured: 0, estimated: 0, assumed: 0, unknown: 0 },
      ),
    [drafts],
  );

  const setCount = (parameters: Parameter[]) =>
    parameters.filter((parameter) => drafts[parameter]?.status !== "unknown").length;

  const update = (parameter: Parameter, value: Draft) => {
    setDrafts((current) => ({ ...current, [parameter]: value }));
    onDraftChange();
  };

  const submit = () => {
    setError(null);
    try {
      const inputs = DEFINITIONS.map((definition) =>
        buildInput(definition, drafts[definition.parameter], drawnArea),
      );
      const parsedSeed = simulationMode === "advanced" ? finiteNumber(seed, "Random seed") : null;
      onAssess({
        scenario: {
          schema_version: "mount-hosmer-observation-scenario-v1",
          mode: "advanced",
          name: name.trim() || null,
          valid_at_utc: validAtUtc ? utcIso(validAtUtc) : null,
          inputs,
        },
        simulation_mode: simulationMode,
        seed: parsedSeed,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const rowsFor = (parameters: Parameter[]) =>
    parameters.map((parameter) => {
      const definition = BY_PARAMETER.get(parameter);
      if (!definition) return null;
      return (
        <ConditionRow
          key={parameter}
          definition={definition}
          draft={drafts[parameter]}
          expanded={Boolean(expanded[parameter])}
          drawnArea={drawnArea}
          onRequestDraw={onRequestDraw}
          onToggle={() => setExpanded((current) => ({ ...current, [parameter]: !current[parameter] }))}
          onChange={(value) => update(parameter, value)}
        />
      );
    });

  return (
    <div className="flex flex-col gap-2.5">
      <div className="flex items-start justify-between gap-2 text-[11px] text-[var(--muted)]">
        <p>
          Blank stays <strong className="text-[var(--foreground)]">unknown</strong> — never zero, never
          a default guess. {statusCounts.measured} measured · {statusCounts.estimated} estimated ·{" "}
          {statusCounts.assumed} assumed · {statusCounts.unknown} unknown.
        </p>
        <ConditionsHelp />
      </div>

      {GROUPS.map((group, index) => {
        const filled = setCount(group.parameters);
        const rows = rowsFor(group.parameters);
        if (index === 0) {
          return (
            <div key={group.key} className="flex flex-col gap-1.5">
              <p className="text-[11px] font-semibold text-[var(--foreground)]">
                {group.title} <span className="font-normal text-[var(--muted)]">· {group.note}</span>
              </p>
              {rows}
            </div>
          );
        }
        return (
          <details key={group.key} className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2">
            <summary className="cursor-pointer text-[11px] font-semibold text-[var(--foreground)]">
              {group.title}{" "}
              <span className="font-normal text-[var(--muted)]">
                · {filled} of {group.parameters.length} set
              </span>
            </summary>
            <p className="mt-1 text-[11px] leading-relaxed text-[var(--muted)]">{group.note}</p>
            <div className="mt-2 flex flex-col gap-1.5">{rows}</div>
          </details>
        );
      })}

      <details className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2">
        <summary className="cursor-pointer text-[11px] font-semibold text-[var(--foreground)]">
          Scenario identity
        </summary>
        <div className="mt-2 grid grid-cols-1 gap-2 text-[11px] sm:grid-cols-2">
          <Field label="Scenario name (optional)" value={name} onChange={(value) => { setName(value); onDraftChange(); }} />
          <Field
            label="Scenario valid at (UTC)"
            type="datetime-local"
            value={validAtUtc}
            onChange={(value) => { setValidAtUtc(value); onDraftChange(); }}
          />
        </div>
      </details>

      <RunoutDefaults />

      <SimulationControls
        mode={simulationMode}
        seed={seed}
        onMode={(value) => { setSimulationMode(value); onDraftChange(); }}
        onSeed={(value) => { setSeed(value); onDraftChange(); }}
      />

      {error ? (
        <p role="alert" className="rounded-md border border-[var(--danger)] p-2 text-[11px] text-[var(--danger)]">
          {error}
        </p>
      ) : null}
      <button
        type="button"
        onClick={submit}
        disabled={running}
        className="rounded-md bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-[#101415] disabled:opacity-60"
      >
        {running ? "Assessing…" : "Assess advanced scenario"}
      </button>
    </div>
  );
}

/**
 * One parameter on one line: label, status, value. Everything the scenario contract
 * can carry is still here, one click away, rather than crowding the common case.
 */
function ConditionRow({
  definition,
  draft,
  expanded,
  drawnArea,
  onRequestDraw,
  onToggle,
  onChange,
}: {
  definition: Definition;
  draft: Draft;
  expanded: boolean;
  drawnArea: GeoJSON.Polygon | null;
  onRequestDraw: () => void;
  onToggle: () => void;
  onChange: (value: Draft) => void;
}) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => onChange({ ...draft, [key]: value });
  const present = draft.status !== "unknown";
  const provenanceRequired = draft.status === "measured" || draft.status === "estimated";
  const owes = missingProvenance(draft);

  return (
    <div
      className="rounded border bg-[var(--panel)] p-1.5 text-[11px]"
      style={{ borderColor: owes ? "var(--danger)" : "var(--border)" }}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="min-w-[8.5rem] flex-1 font-semibold text-[var(--foreground)]">
          {definition.label}
          <span
            className={`ml-1 font-normal ${definition.role === "active" ? "text-[var(--accent)]" : "text-[var(--accent-2)]"}`}
          >
            {definition.role === "active" ? "moves the number" : "raises a warning"}
          </span>
        </span>
        <label className="sr-only" htmlFor={`${definition.parameter}-status`}>
          Status
        </label>
        <select
          id={`${definition.parameter}-status`}
          aria-label={`${definition.label} status`}
          value={draft.status}
          onChange={(event) => set("status", event.target.value as Status)}
          className="input-control w-[6.5rem] shrink-0"
        >
          <option value="unknown">Unknown</option>
          <option value="assumed">Assumed</option>
          <option value="estimated">Estimated</option>
          <option value="measured">Measured</option>
        </select>
        <div className="w-[9rem] shrink-0">
          <ValueField definition={definition} draft={draft} disabled={!present} onChange={(value) => set("value", value)} />
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={`${definition.label} details`}
          className="shrink-0 rounded border border-[var(--border)] px-1.5 py-1 text-[10px] text-[var(--muted)] hover:text-[var(--foreground)]"
        >
          {expanded ? "Less" : "More"}
        </button>
      </div>

      {owes ? (
        <p className="mt-1 text-[10px] font-semibold text-[var(--danger)]">
          A {draft.status} value needs a UTC time and a source — open <em>More</em>.
        </p>
      ) : null}

      {expanded ? (
        <div className="mt-2 flex flex-col gap-2 border-t border-[var(--border)] pt-2">
          <p className="leading-relaxed text-[var(--muted)]">{definition.hint}</p>

          {provenanceRequired ? (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <Field label="Observed/derived at (UTC)" type="datetime-local" value={draft.observedAtUtc} onChange={(value) => set("observedAtUtc", value)} />
              <Field label="Source name / station / observer" value={draft.sourceName} onChange={(value) => set("sourceName", value)} />
              {draft.status === "estimated" ? (
                <label className="flex flex-col gap-1">
                  <span className="text-[var(--muted)]">Estimate lineage</span>
                  <select value={draft.sourceKind} onChange={(event) => set("sourceKind", event.target.value as SourceKind)} className="input-control">
                    <option value="derived_from_measurement">Derived from measurement</option>
                    <option value="model_or_analysis">Model / analysis</option>
                    <option value="expert_judgment">Expert judgment</option>
                    <option value="other">Other</option>
                  </select>
                </label>
              ) : null}
            </div>
          ) : null}

          {present ? (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--muted)]">Uncertainty</span>
                <select value={draft.uncertaintyKind} onChange={(event) => set("uncertaintyKind", event.target.value as UncertaintyKind)} className="input-control">
                  <option value="not_provided">Not provided</option>
                  <option value="unknown">Unknown</option>
                  <option value="qualitative">Qualitative</option>
                  <option value="quantified">Quantified (standard)</option>
                </select>
              </label>
              {draft.uncertaintyKind === "quantified" ? (
                <Field label={`Standard uncertainty (${unitLabel(definition.unit)})`} type="number" value={draft.uncertaintyValue} onChange={(value) => set("uncertaintyValue", value)} />
              ) : null}
              <Field label="Uncertainty basis" value={draft.uncertaintyBasis} onChange={(value) => set("uncertaintyBasis", value)} />
            </div>
          ) : null}

          <SpatialScopeEditor
            draft={draft}
            disabled={!present || definition.parameter === "release_size"}
            drawnArea={drawnArea}
            onRequestDraw={onRequestDraw}
            onChange={onChange}
          />
          <Field label="Notes / interval / method" value={draft.notes} onChange={(value) => set("notes", value)} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * The value control for one parameter.
 *
 * Its accessible name is the PARAMETER name, not "Value (cm)". Five parameters are
 * measured in centimetres, so the old shared label gave five different inputs the
 * same accessible name -- ambiguous for a screen reader and for any test trying to
 * address one of them. The visible label is dropped because the row already shows
 * the parameter; the unit rides along in the accessible name and the placeholder.
 */
function ValueField({ definition, draft, disabled, onChange }: { definition: Definition; draft: Draft; disabled: boolean; onChange: (value: string) => void }) {
  const name = `${definition.label} value (${definition.unit})`;
  const shown = unitLabel(definition.unit);

  if (definition.kind === "boolean") {
    return (
      <select
        aria-label={name}
        disabled={disabled}
        value={draft.value}
        onChange={(event) => onChange(event.target.value)}
        className="input-control w-full disabled:opacity-50"
      >
        <option value="">Value…</option>
        <option value="true">Observed present</option>
        <option value="false">Observed absent</option>
      </select>
    );
  }
  if (definition.kind === "category" && definition.options) {
    return (
      <select
        aria-label={name}
        disabled={disabled}
        value={draft.value}
        onChange={(event) => onChange(event.target.value)}
        className="input-control w-full disabled:opacity-50"
      >
        <option value="">Value…</option>
        {definition.options.map((option) => <option key={option} value={option}>{option.replaceAll("_", " ")}</option>)}
      </select>
    );
  }
  return (
    <input
      aria-label={name}
      type="number"
      placeholder={shown}
      disabled={disabled}
      value={draft.value}
      onChange={(event) => onChange(event.target.value)}
      className="input-control w-full disabled:opacity-50"
    />
  );
}

function SpatialScopeEditor({ draft, disabled, drawnArea, onRequestDraw, onChange }: { draft: Draft; disabled: boolean; drawnArea: GeoJSON.Polygon | null; onRequestDraw: () => void; onChange: (value: Draft) => void }) {
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => onChange({ ...draft, [key]: value });
  return (
    <div className="rounded border border-[var(--border)] p-2">
      <label className="flex flex-col gap-1">
        <span className="text-[var(--muted)]">Spatial applicability (grid-cell centres)</span>
        <select disabled={disabled} value={draft.scopeKind} onChange={(event) => set("scopeKind", event.target.value as Draft["scopeKind"])} className="input-control disabled:opacity-50">
          <option value="whole_area">Whole Mount Hosmer area</option>
          <option value="elevation_band">Elevation band</option>
          <option value="aspect_band">Aspect band</option>
          <option value="elevation_aspect_band">Elevation + aspect band</option>
          <option value="drawn_area">User-drawn WGS84 area</option>
        </select>
      </label>
      {draft.scopeKind.includes("elevation") ? (
        <div className="mt-2 grid grid-cols-2 gap-2">
          <Field label="Minimum elevation (m)" type="number" value={draft.elevationMin} onChange={(value) => set("elevationMin", value)} />
          <Field label="Maximum elevation (m)" type="number" value={draft.elevationMax} onChange={(value) => set("elevationMax", value)} />
        </div>
      ) : null}
      {draft.scopeKind.includes("aspect") ? (
        <div className="mt-2 grid grid-cols-2 gap-2">
          <Field label="Aspect start (° true)" type="number" value={draft.aspectMin} onChange={(value) => set("aspectMin", value)} />
          <Field label="Aspect end (° true)" type="number" value={draft.aspectMax} onChange={(value) => set("aspectMax", value)} />
          <p className="col-span-2 text-[10px] text-[var(--muted)]">Start greater than end wraps through north (for example 315°→45°).</p>
        </div>
      ) : null}
      {draft.scopeKind === "drawn_area" ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="text-[10px] text-[var(--muted)]">{drawnArea ? `${drawnArea.coordinates[0].length - 1} vertices · [longitude, latitude]` : "No area drawn"}</span>
          <button type="button" onClick={onRequestDraw} className="rounded border border-[var(--border)] px-2 py-1 text-[10px] text-[var(--accent)]">{drawnArea ? "Redraw on map" : "Draw on map"}</button>
        </div>
      ) : null}
    </div>
  );
}

function SimulationControls({ mode, seed, onMode, onSeed }: { mode: SimulationMode; seed: string; onMode: (mode: SimulationMode) => void; onSeed: (seed: string) => void }) {
  return (
    <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2 text-[11px]">
      <p className="font-semibold text-[var(--foreground)]">Runout engine</p>
      <div className="mt-1 flex gap-1">
        {(["fast", "advanced"] as SimulationMode[]).map((value) => (
          <button key={value} type="button" aria-pressed={mode === value} onClick={() => onMode(value)} className={`rounded px-2 py-1 ${mode === value ? "bg-[var(--accent)] text-[#101415]" : "border border-[var(--border)] text-[var(--muted)]"}`}>
            {value === "fast" ? "Fast alpha routing" : "Seeded particle ensemble"}
          </button>
        ))}
      </div>
      {mode === "advanced" ? <div className="mt-2"><Field label="Deterministic random seed" type="number" value={seed} onChange={onSeed} /></div> : null}
    </div>
  );
}

/**
 * The heading stays visible -- a reader must be able to see that the physics is
 * fixed without opening anything -- while the constants themselves fold away.
 */
function RunoutDefaults() {
  return (
    <details className="rounded border border-[var(--accent-2)] bg-[var(--panel)] p-2 text-[10px] leading-relaxed text-[var(--muted)]">
      <summary className="cursor-pointer font-semibold text-[var(--accent-2)]">
        Physics parameters are locked
      </summary>
      <p className="mt-1">Uncalibrated regional dry-snow defaults: alpha 32°/27°/23°/19° by size with a 4° sensitivity; μ open 0.20, forest 0.35, gully 0.16; ξ open 1200 and forest 500 m/s².</p>
      <p className="mt-1">They are shown for transparency, not exposed as arbitrary tuning controls. Changing them would be a sensitivity experiment, not evidence of greater accuracy.</p>
    </details>
  );
}

function Field({ label, value, onChange, type = "text", disabled = false }: { label: string; value: string; onChange: (value: string) => void; type?: string; disabled?: boolean }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[var(--muted)]">{label}</span>
      <input type={type} disabled={disabled} value={value} onChange={(event) => onChange(event.target.value)} className="input-control disabled:opacity-50" />
    </label>
  );
}

function buildInput(definition: Definition, draft: Draft, drawnArea: GeoJSON.Polygon | null): ScenarioInput {
  const unknown = draft.status === "unknown";
  const value = unknown ? null : parseValue(definition, draft.value);
  const provenanceRequired = draft.status === "measured" || draft.status === "estimated";
  if (provenanceRequired && (!draft.observedAtUtc || !draft.sourceName.trim())) {
    throw new Error(`${definition.label}: measured/estimated values require a UTC timestamp and source.`);
  }
  const uncertainty = buildUncertainty(definition, draft, unknown);
  return {
    input_id: `${definition.parameter}-primary`,
    category: definition.category,
    parameter: definition.parameter,
    value,
    unit: definition.unit,
    status: draft.status,
    observed_at_utc: provenanceRequired ? utcIso(draft.observedAtUtc) : null,
    source: unknown
      ? null
      : draft.status === "assumed"
        ? { name: "User-entered scenario assumption", kind: "user_assumption" }
        : {
            name: draft.sourceName.trim(),
            kind: draft.status === "measured" ? "measurement" : draft.sourceKind,
          },
    uncertainty,
    spatial_scope: buildScope(draft, drawnArea),
    notes: draft.notes.trim() || null,
  };
}

function parseValue(definition: Definition, value: string): number | string | boolean {
  if (!value.trim()) throw new Error(`${definition.label}: choose a value or mark the input unknown.`);
  if (definition.kind === "number") return finiteNumber(value, definition.label);
  if (definition.kind === "boolean") return value === "true";
  return value;
}

function buildUncertainty(definition: Definition, draft: Draft, unknown: boolean): InputUncertainty {
  if (unknown) return { kind: "unknown", basis: "The input value is unknown." };
  const basis = draft.uncertaintyBasis.trim() || "No uncertainty basis was supplied.";
  if (draft.uncertaintyKind === "quantified") {
    return {
      kind: "quantified",
      value: finiteNumber(draft.uncertaintyValue, `${definition.label} uncertainty`),
      unit: definition.unit,
      basis,
    };
  }
  return { kind: draft.uncertaintyKind, basis };
}

function buildScope(draft: Draft, drawnArea: GeoJSON.Polygon | null): SpatialScope {
  if (draft.scopeKind === "whole_area") return { kind: "whole_area" };
  if (draft.scopeKind === "drawn_area") {
    if (!drawnArea) throw new Error("A drawn-area scope was selected, but no polygon was finished on the map.");
    return { kind: "drawn_area", geometry: drawnArea as unknown as Record<string, unknown> };
  }
  const scope: SpatialScope = { kind: draft.scopeKind };
  if (draft.scopeKind.includes("elevation")) {
    scope.elevation_min_m = finiteNumber(draft.elevationMin, "Minimum elevation");
    scope.elevation_max_m = finiteNumber(draft.elevationMax, "Maximum elevation");
  }
  if (draft.scopeKind.includes("aspect")) {
    scope.aspect_min_deg = finiteNumber(draft.aspectMin, "Aspect start");
    scope.aspect_max_deg = finiteNumber(draft.aspectMax, "Aspect end");
  }
  return scope;
}

function finiteNumber(value: string, label: string): number {
  if (!value.trim()) throw new Error(`${label} is required.`);
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label} must be a finite number.`);
  return parsed;
}

function utcIso(value: string): string {
  return `${value.length === 16 ? value + ":00" : value}Z`;
}
