"use client";

import { useState } from "react";
import type { AssessRequest, ReleaseSize, SimulationMode } from "@/lib/twin";

/** Conditions come from these sliders/presets — Stage 3 does no weather ingestion. */

const RELEASE_SIZES: [ReleaseSize, string][] = [
  ["small", "Small (sz 1–2)"],
  ["medium", "Medium (sz 2–3)"],
  ["large", "Large (sz 3–4)"],
  ["very_large", "Very large (sz 4–5)"],
];

type Preset = { id: string; label: string; value: Omit<AssessRequest, "simulation_mode" | "seed"> };

const PRESETS: Preset[] = [
  { id: "calm", label: "Calm / off-season", value: { new_snow_cm: 0, wind_speed_kmh: 0, wind_direction_deg: 225, release_size: "medium" } },
  { id: "storm_sw", label: "Storm slab, SW wind", value: { new_snow_cm: 40, wind_speed_kmh: 45, wind_direction_deg: 225, release_size: "medium" } },
  { id: "big_storm", label: "Big storm + strong wind", value: { new_snow_cm: 70, wind_speed_kmh: 65, wind_direction_deg: 225, release_size: "large" } },
  { id: "wind_event", label: "Wind event, little snow", value: { new_snow_cm: 8, wind_speed_kmh: 60, wind_direction_deg: 270, release_size: "medium" } },
];

const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
const compass = (deg: number) => COMPASS[Math.round((deg % 360) / 22.5) % 16];

type Props = {
  running: boolean;
  onAssess: (request: AssessRequest) => void;
};

export function ConditionPanel({ running, onAssess }: Props) {
  const [newSnow, setNewSnow] = useState(40);
  const [windSpeed, setWindSpeed] = useState(45);
  const [windDir, setWindDir] = useState(225);
  const [releaseSize, setReleaseSize] = useState<ReleaseSize>("medium");
  const [simMode, setSimMode] = useState<SimulationMode>("fast");

  const applyPreset = (preset: Preset) => {
    setNewSnow(preset.value.new_snow_cm);
    setWindSpeed(preset.value.wind_speed_kmh);
    setWindDir(preset.value.wind_direction_deg);
    setReleaseSize(preset.value.release_size);
  };

  const submit = () =>
    onAssess({
      new_snow_cm: newSnow,
      wind_speed_kmh: windSpeed,
      wind_direction_deg: windDir,
      release_size: releaseSize,
      simulation_mode: simMode,
      seed: 42,
    });

  return (
    <div className="flex flex-col gap-5">
      {/* Presets are shortcuts, so they read as a quiet list of starting points
          rather than four buttons competing with the primary action below. */}
      <div className="flex flex-col gap-px overflow-hidden rounded-[3px] border border-[var(--rule)]">
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            disabled={running}
            onClick={() => applyPreset(preset)}
            className="group flex items-center justify-between bg-[var(--field-1)] px-3 py-2 text-left text-xs text-[var(--paper-dim)] transition-colors hover:bg-[var(--field-2)] hover:text-[var(--paper)] disabled:cursor-not-allowed disabled:opacity-45"
          >
            <span>{preset.label}</span>
            <span className="data text-[10px] text-[var(--paper-faint)] transition-colors group-hover:text-[var(--signal)]">
              {preset.value.new_snow_cm}cm · {preset.value.wind_speed_kmh}km/h
            </span>
          </button>
        ))}
      </div>

      <div className="flex flex-col gap-4">
        <Slider label="New snow" value={newSnow} min={0} max={120} step={1} unit="cm" onChange={setNewSnow} />
        <Slider label="Wind speed" value={windSpeed} min={0} max={120} step={1} unit="km/h" onChange={setWindSpeed} />
        <Slider
          label="Wind from"
          value={windDir}
          min={0}
          max={360}
          step={5}
          unit="°"
          badge={compass(windDir)}
          onChange={setWindDir}
        />
      </div>

      <label className="flex flex-col gap-1.5">
        <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--paper-faint)]">
          Release size
        </span>
        <select
          value={releaseSize}
          onChange={(event) => setReleaseSize(event.target.value as ReleaseSize)}
          className="rounded-[3px] border border-[var(--rule)] bg-[var(--field-2)] px-2.5 py-2 pr-8 text-xs text-[var(--paper)] transition-colors hover:border-[var(--rule-lit)]"
        >
          {RELEASE_SIZES.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>

      <div className="flex flex-col gap-1.5">
        <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--paper-faint)]">
          Runout model
        </span>
        <div className="flex gap-px overflow-hidden rounded-[3px] border border-[var(--rule)]">
          {([
            ["fast", "Fast", "alpha"],
            ["advanced", "Advanced", "particle"],
          ] as [SimulationMode, string, string][]).map(([mode, label, note]) => (
            <button
              key={mode}
              type="button"
              onClick={() => setSimMode(mode)}
              className={`flex flex-1 flex-col items-center py-1.5 text-[11px] font-medium transition-colors ${
                simMode === mode
                  ? "bg-[var(--signal)] text-[var(--field)]"
                  : "bg-[var(--field-2)] text-[var(--paper-dim)] hover:bg-[var(--field-3)] hover:text-[var(--paper)]"
              }`}
            >
              {label}
              <span className={`data text-[9px] ${simMode === mode ? "opacity-70" : "text-[var(--paper-faint)]"}`}>
                {note}
              </span>
            </button>
          ))}
        </div>
      </div>

      {/* The one primary action in the whole interface, and the only place a
          filled amber block this large is used. */}
      <button
        type="button"
        onClick={submit}
        disabled={running}
        className="group relative mt-1 overflow-hidden rounded-[3px] bg-[var(--signal)] px-4 py-3 text-sm font-semibold tracking-wide text-[var(--field)] transition-all hover:bg-[#ffc476] disabled:cursor-wait disabled:opacity-55"
        style={{ fontFamily: "var(--font-display)" }}
      >
        {running ? "Assessing…" : "Assess terrain"}
        {running ? (
          <span className="absolute inset-x-0 bottom-0 h-[2px] animate-pulse bg-[var(--field)]/40" />
        ) : null}
      </button>
    </div>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  unit,
  badge,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit: string;
  badge?: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] text-[var(--paper-dim)]">{label}</span>
        <span className="flex items-baseline gap-1.5">
          {badge ? (
            <span className="data rounded-[2px] bg-[var(--signal-wash)] px-1.5 py-px text-[10px] text-[var(--signal)]">
              {badge}
            </span>
          ) : null}
          <span className="data text-[13px] text-[var(--paper)]">
            {value}
            <span className="text-[var(--paper-faint)]">{unit}</span>
          </span>
        </span>
      </span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}
