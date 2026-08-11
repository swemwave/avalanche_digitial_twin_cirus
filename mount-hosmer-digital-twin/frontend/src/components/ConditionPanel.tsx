"use client";

import { useState } from "react";
import { AdvancedConditionWorkspace } from "@/components/AdvancedConditionWorkspace";
import type { AssessRequest, ReleaseSize, SimulationMode } from "@/lib/twin";

const RELEASE_SIZES: [ReleaseSize, string][] = [
  ["small", "Small (size 1–2)"],
  ["medium", "Medium (size 2–3)"],
  ["large", "Large (size 3–4)"],
  ["very_large", "Very large (size 4–5)"],
];

type Preset = {
  id: string;
  label: string;
  newSnow: string;
  windSpeed: string;
  windDirection: string;
  releaseSize: ReleaseSize;
  /** Blank leaves temperature unknown, which reproduces the temperature-free result. */
  airTemperature: string;
};

const PRESETS: Preset[] = [
  { id: "storm_sw", label: "Storm slab · SW wind", newSnow: "40", windSpeed: "45", windDirection: "225", releaseSize: "medium", airTemperature: "" },
  { id: "big_storm", label: "Large storm sensitivity", newSnow: "70", windSpeed: "65", windDirection: "225", releaseSize: "large", airTemperature: "" },
  { id: "wind_event", label: "Wind-loading sensitivity", newSnow: "8", windSpeed: "60", windDirection: "270", releaseSize: "medium", airTemperature: "" },
  // Makes the phase gate discoverable: the index falls because rain builds no dry
  // slab, and the result carries a critical advisory saying that is not a safer day.
  { id: "rain_on_snow", label: "Rain on snow · +4 °C", newSnow: "30", windSpeed: "30", windDirection: "225", releaseSize: "medium", airTemperature: "4" },
];

type Props = {
  running: boolean;
  onAssess: (request: AssessRequest) => void;
  drawnArea: GeoJSON.Polygon | null;
  onRequestDraw: () => void;
  onDraftChange: () => void;
};

export function ConditionPanel({ running, onAssess, drawnArea, onRequestDraw, onDraftChange }: Props) {
  const [workspace, setWorkspace] = useState<"simple" | "advanced">("simple");

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="grid grid-cols-2 rounded-md border border-[var(--border)] p-1" aria-label="Condition workspace mode">
        {(["simple", "advanced"] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            aria-pressed={workspace === mode}
            onClick={() => {
              setWorkspace(mode);
              onDraftChange();
            }}
            className={`rounded px-2 py-1.5 text-xs ${workspace === mode ? "bg-[var(--accent)] font-semibold text-[#101415]" : "text-[var(--muted)]"}`}
          >
            {mode === "simple" ? "Simple scenario" : "Advanced conditions"}
          </button>
        ))}
      </div>

      {workspace === "simple" ? (
        <SimpleWorkspace running={running} onAssess={onAssess} onDraftChange={onDraftChange} />
      ) : (
        <AdvancedConditionWorkspace
          running={running}
          onAssess={onAssess}
          drawnArea={drawnArea}
          onRequestDraw={onRequestDraw}
          onDraftChange={onDraftChange}
        />
      )}
    </div>
  );
}

function SimpleWorkspace({ running, onAssess, onDraftChange }: Pick<Props, "running" | "onAssess" | "onDraftChange">) {
  const [newSnow, setNewSnow] = useState("");
  const [windSpeed, setWindSpeed] = useState("");
  const [windDirection, setWindDirection] = useState("");
  const [airTemperature, setAirTemperature] = useState("");
  const [releaseSize, setReleaseSize] = useState<ReleaseSize>("medium");
  const [simulationMode, setSimulationMode] = useState<SimulationMode>("fast");
  const [error, setError] = useState<string | null>(null);

  const applyPreset = (preset: Preset) => {
    setNewSnow(preset.newSnow);
    setWindSpeed(preset.windSpeed);
    setWindDirection(preset.windDirection);
    setAirTemperature(preset.airTemperature);
    setReleaseSize(preset.releaseSize);
    setError(null);
    onDraftChange();
  };

  const submit = () => {
    setError(null);
    try {
      onAssess({
        new_snow_cm: optionalNumber(newSnow, "New snow"),
        wind_speed_kmh: optionalNumber(windSpeed, "Wind speed"),
        wind_direction_deg: optionalNumber(windDirection, "Wind direction"),
        release_size: releaseSize,
        air_temperature_c: optionalNumber(airTemperature, "Air temperature"),
        simulation_mode: simulationMode,
        seed: simulationMode === "advanced" ? 42 : null,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  };

  const terrainOnly = !newSnow.trim() && !windSpeed.trim() && !windDirection.trim();

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2 text-[11px] leading-relaxed text-[var(--muted)]">
        <p className="font-semibold text-[var(--foreground)]">Blank means unknown</p>
        <p>Entered values are explicit assumptions. Unknown loading is never changed to 0.</p>
      </div>

      <div>
        <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--accent-2)]">Hypothetical examples</p>
        <div className="flex flex-wrap gap-1.5">
          {PRESETS.map((preset) => (
            <button
              key={preset.id}
              type="button"
              disabled={running}
              onClick={() => applyPreset(preset)}
              className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1 text-[11px] text-[var(--muted)] hover:text-[var(--foreground)] disabled:opacity-50"
            >
              {preset.label}
            </button>
          ))}
        </div>
      </div>

      <NumberInput label="New storm-snow depth" value={newSnow} unit="cm" placeholder="unknown" onChange={(value) => { setNewSnow(value); onDraftChange(); }} />
      <NumberInput label="Representative wind speed" value={windSpeed} unit="km/h" placeholder="unknown" onChange={(value) => { setWindSpeed(value); onDraftChange(); }} />
      <NumberInput label="Wind direction (FROM)" value={windDirection} unit="° true" placeholder="unknown" onChange={(value) => { setWindDirection(value); onDraftChange(); }} />
      <NumberInput label="Air temperature (optional)" value={airTemperature} unit="°C" placeholder="unknown" onChange={(value) => { setAirTemperature(value); onDraftChange(); }} />

      <p className="-mt-1 text-[10px] leading-relaxed text-[var(--muted)]">
        Temperature only splits new precipitation into snow and rain (0–2 °C). Above 2 °C it is
        rain, which builds no dry slab — a lower index there means less dry-slab loading, not a
        safer day.
      </p>

      <label className="flex flex-col gap-1">
        <span className="flex justify-between text-xs text-[var(--muted)]"><span>Release-size assumption</span><span>category</span></span>
        <select value={releaseSize} onChange={(event) => { setReleaseSize(event.target.value as ReleaseSize); onDraftChange(); }} className="input-control">
          {RELEASE_SIZES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>

      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="text-[var(--muted)]">Runout engine</span>
        <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
          {(["fast", "advanced"] as SimulationMode[]).map((mode) => (
            <button key={mode} type="button" aria-pressed={simulationMode === mode} onClick={() => { setSimulationMode(mode); onDraftChange(); }} className={`px-2.5 py-1 ${simulationMode === mode ? "bg-[var(--accent)] text-[#101415]" : "bg-[var(--panel-strong)] text-[var(--muted)]"}`}>
              {mode === "fast" ? "Fast alpha" : "Particle"}
            </button>
          ))}
        </div>
      </div>

      {error ? <p role="alert" className="text-[11px] text-[var(--danger)]">{error}</p> : null}
      <button type="button" data-testid="simple-assess" onClick={submit} disabled={running} className="rounded-md bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-[#101415] disabled:opacity-60">
        {running ? "Assessing…" : terrainOnly ? "Show terrain-only result" : "Assess hypothetical scenario"}
      </button>
      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        Simple entries have status <strong>assumed</strong>, whole-area applicability, and unquantified uncertainty. Use Advanced conditions for measurements, estimates, sources, times, uncertainty, snowpack/field context, or spatial scopes.
      </p>
    </div>
  );
}

function NumberInput({ label, value, unit, placeholder, onChange }: { label: string; value: string; unit: string; placeholder: string; onChange: (value: string) => void }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="flex justify-between text-xs text-[var(--muted)]"><span>{label}</span><span>{unit}</span></span>
      <input type="number" value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="input-control" />
    </label>
  );
}

function optionalNumber(value: string, label: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) throw new Error(`${label} must be a finite number.`);
  return parsed;
}
