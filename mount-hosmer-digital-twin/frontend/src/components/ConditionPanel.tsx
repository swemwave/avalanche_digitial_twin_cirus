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
// The presets are common what-if scenarios that the user can quickly apply to the sliders. They are not based on real weather data, but rather on typical avalanche conditions.
const PRESETS: Preset[] = [
  { id: "calm", label: "Calm / off-season", value: { new_snow_cm: 0, wind_speed_kmh: 0, wind_direction_deg: 225, release_size: "medium" } },
  { id: "storm_sw", label: "Storm slab, SW wind", value: { new_snow_cm: 40, wind_speed_kmh: 45, wind_direction_deg: 225, release_size: "medium" } },
  { id: "big_storm", label: "Big storm + strong wind", value: { new_snow_cm: 70, wind_speed_kmh: 65, wind_direction_deg: 225, release_size: "large" } },
  { id: "wind_event", label: "Wind event, little snow", value: { new_snow_cm: 8, wind_speed_kmh: 60, wind_direction_deg: 270, release_size: "medium" } },
];
// The compass function converts a wind direction in degrees to a compass direction (N, NE, E, SE, S, SW, W, NW). It is used to display the wind direction in the UI.
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
    <div className="flex flex-col gap-4 text-sm">
      <div className="flex flex-wrap gap-1.5">
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            disabled={running}
            onClick={() => applyPreset(preset)}
            className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2.5 py-1 text-xs text-[var(--muted)] hover:text-[var(--foreground)] disabled:opacity-50"
          >
            {preset.label}
          </button>
        ))}
      </div>

      <Slider label="New snow" value={newSnow} min={0} max={120} step={1} unit="cm" onChange={setNewSnow} />
      <Slider label="Wind speed" value={windSpeed} min={0} max={120} step={1} unit="km/h" onChange={setWindSpeed} />
      <Slider
        label="Wind direction (FROM)"
        value={windDir}
        min={0}
        max={360}
        step={5}
        unit={`° ${compass(windDir)}`}
        onChange={setWindDir}
      />

      <label className="flex flex-col gap-1">
        <span className="text-xs text-[var(--muted)]">Release size</span>
        <select
          value={releaseSize}
          onChange={(event) => setReleaseSize(event.target.value as ReleaseSize)}
          className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1.5 text-sm"
        >
          {RELEASE_SIZES.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>

      <div className="flex items-center gap-2 text-xs">
        <span className="text-[var(--muted)]">Runout model</span>
        <div className="flex overflow-hidden rounded-md border border-[var(--border)]">
          {(["fast", "advanced"] as SimulationMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setSimMode(mode)}
              className={`px-2.5 py-1 ${
                simMode === mode ? "bg-[var(--accent)] text-[#101415]" : "bg-[var(--panel-strong)] text-[var(--muted)]"
              }`}
            >
              {mode === "fast" ? "Fast (alpha)" : "Advanced (particle)"}
            </button>
          ))}
        </div>
      </div>

      <button
        type="button"
        onClick={submit}
        disabled={running}
        className="mt-1 rounded-md bg-[var(--accent)] px-3 py-2 text-sm font-semibold text-[#101415] disabled:opacity-60"
      >
        {running ? "Assessing…" : "Assess"}
      </button>
      <p className="text-[11px] leading-relaxed text-[var(--muted)]">
        Conditions are what-if slider values, not measurements. This is an experimental,
        non-operational research tool.
      </p>
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
    <label className="flex flex-col gap-1">
      <span className="flex items-baseline justify-between text-xs text-[var(--muted)]">
        <span>{label}</span>
        <span className="font-mono text-[var(--foreground)]">
          {value}
          {unit}
        </span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="accent-[var(--accent)]"
      />
    </label>
  );
}
