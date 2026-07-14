"use client";

import { useEffect, useState } from "react";
import {
  getPresets,
  type AnalysisMode,
  type Job,
  type ReleaseSize,
  type ScenarioInput,
  type ScenarioPreset,
  type SimulationMode,
} from "@/lib/apiV1";

type Props = {
  running: boolean;
  job: Job | null;
  onRun: (input: {
    mode: AnalysisMode;
    at?: string;
    preset?: string;
    scenario?: ScenarioInput;
    eventId?: string;
    simulationMode: SimulationMode;
    releaseSize: ReleaseSize;
  }) => void;
};

const EVENTS = ["MH_20260116T183016Z", "MH_20260430T182949Z"];

const COMPASS: [string, number][] = [
  ["N", 0], ["NE", 45], ["E", 90], ["SE", 135],
  ["S", 180], ["SW", 225], ["W", 270], ["NW", 315],
];

function Field({ label, children, note }: { label: string; children: React.ReactNode; note?: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">{label}</span>
      {children}
      {note ? <span className="text-[10px] leading-snug text-[var(--muted)]">{note}</span> : null}
    </label>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={`${label} — ${value} ${unit}`}>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="accent-[var(--accent)]"
      />
    </Field>
  );
}

const select =
  "rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1.5 text-sm text-[var(--foreground)]";

export function AnalysisControls({ running, job, onRun }: Props) {
  const [presets, setPresets] = useState<ScenarioPreset[]>([]);
  const [mode, setMode] = useState<AnalysisMode>("scenario");
  const [presetName, setPresetName] = useState("wind_loading");
  const [at, setAt] = useState("2026-01-16T18:30");
  const [eventId, setEventId] = useState<string>(EVENTS[0]);
  const [manual, setManual] = useState(false);

  const [snowfall, setSnowfall] = useState(35);
  const [wind, setWind] = useState(55);
  const [windDir, setWindDir] = useState(225);
  const [temperature, setTemperature] = useState(-6);
  const [tempChange, setTempChange] = useState(0);

  const [simulationMode, setSimulationMode] = useState<SimulationMode>("fast");
  const [releaseSize, setReleaseSize] = useState<ReleaseSize>("medium");

  useEffect(() => {
    getPresets()
      .then((body) => setPresets(body.presets))
      .catch(() => setPresets([]));
  }, []);

  const active = presets.find((preset) => preset.id === presetName);

  function run() {
    onRun({
      mode,
      at: mode === "historical" ? new Date(at).toISOString() : undefined,
      preset: mode === "scenario" && !manual ? presetName : undefined,
      scenario:
        mode === "scenario" && manual
          ? {
              snowfall_72h_cm: snowfall,
              wind_speed_kmh: wind,
              wind_direction_deg: windDir,
              temperature_c: temperature,
              temperature_change_24h_c: tempChange,
              release_size: releaseSize,
              label: "Manual scenario",
            }
          : undefined,
      eventId: eventId || undefined,
      simulationMode,
      releaseSize,
    });
  }

  return (
    <div className="flex flex-col gap-3 text-sm">
      <Field label="Conditions">
        <div className="grid grid-cols-3 gap-1 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-1">
          {(["current", "historical", "scenario"] as AnalysisMode[]).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => setMode(option)}
              className={`rounded px-2 py-1.5 text-xs capitalize ${
                mode === option
                  ? "bg-[var(--accent)] text-[#101415]"
                  : "text-[var(--muted)] hover:bg-[var(--panel)]"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </Field>

      {mode === "historical" ? (
        <Field
          label="Replay at"
          note="Weather is replayed from the ECCC record at this time. Avalanche Canada is NOT used — it is a current forecast, never a historical label."
        >
          <input
            type="datetime-local"
            value={at}
            onChange={(event) => setAt(event.target.value)}
            className={select}
          />
        </Field>
      ) : null}

      {mode === "scenario" ? (
        <>
          <div className="flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">Scenario</span>
            <button
              type="button"
              onClick={() => setManual((value) => !value)}
              className="text-[11px] text-[var(--accent)] underline underline-offset-2"
            >
              {manual ? "use a preset" : "set values manually"}
            </button>
          </div>

          {!manual ? (
            <>
              <select value={presetName} onChange={(e) => setPresetName(e.target.value)} className={select}>
                {presets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.label}
                  </option>
                ))}
              </select>
              {active ? (
                <p className="text-[11px] leading-relaxed text-[var(--muted)]">{active.description}</p>
              ) : null}
            </>
          ) : (
            <div className="flex flex-col gap-2.5 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2.5">
              <Slider label="Snowfall 72 h" value={snowfall} min={0} max={150} step={5} unit="cm" onChange={setSnowfall} />
              <Slider label="Wind speed" value={wind} min={0} max={120} step={5} unit="km/h" onChange={setWind} />
              <Field label="Wind from">
                <div className="grid grid-cols-4 gap-1">
                  {COMPASS.map(([name, degrees]) => (
                    <button
                      key={name}
                      type="button"
                      onClick={() => setWindDir(degrees)}
                      className={`rounded px-1 py-1 text-xs ${
                        windDir === degrees
                          ? "bg-[var(--accent)] text-[#101415]"
                          : "bg-[var(--panel)] text-[var(--muted)] hover:text-[var(--foreground)]"
                      }`}
                    >
                      {name}
                    </button>
                  ))}
                </div>
                <span className="text-[10px] leading-snug text-[var(--muted)]">
                  The direction the wind blows <em>from</em>. Snow is stripped from this side and
                  loaded onto the opposite (lee) slopes.
                </span>
              </Field>
              <Slider label="Temperature" value={temperature} min={-30} max={15} step={1} unit="°C" onChange={setTemperature} />
              <Slider label="24 h change" value={tempChange} min={-20} max={20} step={1} unit="°C" onChange={setTempChange} />
              <p className="text-[10px] leading-relaxed text-[var(--accent-2)]">
                These are hypothetical inputs, not observations. The result describes what the model
                would say <em>if</em> these conditions held.
              </p>
            </div>
          )}
        </>
      ) : null}

      <Field
        label="Satellite snow"
        note="Gives the snow model an observed NDSI scene. Only two events exist."
      >
        <select value={eventId} onChange={(e) => setEventId(e.target.value)} className={select}>
          <option value="">None (model snow from weather)</option>
          {EVENTS.map((event) => (
            <option key={event} value={event}>
              {event}
            </option>
          ))}
        </select>
      </Field>

      <div className="grid grid-cols-2 gap-2">
        <Field label="Simulation">
          <select
            value={simulationMode}
            onChange={(e) => setSimulationMode(e.target.value as SimulationMode)}
            className={select}
          >
            <option value="fast">Fast (alpha)</option>
            <option value="advanced">Advanced (Voellmy)</option>
          </select>
        </Field>
        <Field label="Release size">
          <select
            value={releaseSize}
            onChange={(e) => setReleaseSize(e.target.value as ReleaseSize)}
            className={select}
          >
            <option value="small">Small (size 1–2)</option>
            <option value="medium">Medium (size 2–3)</option>
            <option value="large">Large (size 3–4)</option>
            <option value="very_large">Very large (size 4–5)</option>
          </select>
        </Field>
      </div>
      <p className="text-[10px] leading-relaxed text-[var(--muted)]">
        Release size sets the angle of reach, and the angle of reach bounds the runout. A bigger
        release runs further — so if you want the longer runout, simulate the bigger avalanche.
      </p>

      <button
        type="button"
        onClick={run}
        disabled={running}
        className="mt-1 rounded-md bg-[var(--accent)] px-3 py-2.5 text-sm font-semibold text-[#101415] disabled:cursor-not-allowed disabled:opacity-50"
      >
        {running ? "Running…" : "Run analysis + simulation"}
      </button>

      {running && job ? (
        <div className="flex flex-col gap-1">
          <div className="h-1.5 overflow-hidden rounded-full bg-[var(--panel-strong)]">
            <div
              className="h-full rounded-full bg-[var(--accent)] transition-[width] duration-500"
              style={{ width: `${job.progress}%` }}
            />
          </div>
          <p className="text-[11px] text-[var(--muted)]">
            {job.progress}% · {job.progress_message ?? "…"}
          </p>
        </div>
      ) : null}
    </div>
  );
}
