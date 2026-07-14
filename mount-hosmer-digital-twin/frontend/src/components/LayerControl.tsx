"use client";

import type { CameraPreset, ImageCorners } from "@/components/TwinMap";

export type LayerRecord = {
  id: string;
  title: string;
  group: string;
  units: string;
  coordinates?: ImageCorners;
  stats?: { min?: number; max?: number };
};

type Props = {
  layers: LayerRecord[];
  selected: string | null;
  opacity: number;
  exaggeration: number;
  camera: CameraPreset;
  onSelect: (id: string | null) => void;
  onOpacity: (value: number) => void;
  onExaggeration: (value: number) => void;
  onCamera: (preset: CameraPreset) => void;
};

const CAMERAS: [CameraPreset, string][] = [
  ["overview", "Overview"],
  ["north", "North face"],
  ["south", "South face"],
  ["top", "Plan"],
];

export function LayerControl({
  layers,
  selected,
  opacity,
  exaggeration,
  camera,
  onSelect,
  onOpacity,
  onExaggeration,
  onCamera,
}: Props) {
  const groups = layers.reduce<Record<string, LayerRecord[]>>((accumulator, layer) => {
    (accumulator[layer.group] ??= []).push(layer);
    return accumulator;
  }, {});

  const active = layers.find((layer) => layer.id === selected);

  return (
    <div className="flex flex-col gap-3 text-sm">
      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
          Draped layer ({layers.length} available)
        </span>
        <select
          value={selected ?? ""}
          onChange={(event) => onSelect(event.target.value || null)}
          className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2 py-1.5 text-sm"
        >
          <option value="">None (bare hillshade)</option>
          {Object.entries(groups).map(([group, items]) => (
            <optgroup key={group} label={group}>
              {items.map((layer) => (
                <option key={layer.id} value={layer.id}>
                  {layer.title}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
      </label>

      {active ? (
        <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2.5">
          <div className="flex items-baseline justify-between">
            <span className="text-xs text-[var(--foreground)]">{active.title}</span>
            <span className="text-[10px] text-[var(--muted)]">{active.units}</span>
          </div>
          {/* A legend, because a colour ramp with no scale is decoration, not data. */}
          <div className="mt-1.5 h-2 rounded-sm bg-gradient-to-r from-[#2c3e50] via-[#f2d16b] to-[#c23b35]" />
          <div className="mt-1 flex justify-between text-[10px] tabular-nums text-[var(--muted)]">
            <span>{active.stats?.min?.toFixed?.(1) ?? "low"}</span>
            <span>{active.stats?.max?.toFixed?.(1) ?? "high"}</span>
          </div>
        </div>
      ) : null}

      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
          Opacity — {Math.round(opacity * 100)}%
        </span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={opacity}
          onChange={(event) => onOpacity(Number(event.target.value))}
          className="accent-[var(--accent)]"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
          Vertical exaggeration — {exaggeration.toFixed(1)}×
        </span>
        <input
          type="range"
          min={1}
          max={2.5}
          step={0.1}
          value={exaggeration}
          onChange={(event) => onExaggeration(Number(event.target.value))}
          className="accent-[var(--accent)]"
        />
        <span className="text-[10px] leading-snug text-[var(--muted)]">
          Exaggeration makes the terrain easier to read. It does <strong>not</strong> change any
          slope angle the model used — those come from the 5 m grid, not the picture.
        </span>
      </label>

      <div className="flex flex-col gap-1">
        <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">Camera</span>
        <div className="grid grid-cols-4 gap-1">
          {CAMERAS.map(([preset, label]) => (
            <button
              key={preset}
              type="button"
              onClick={() => onCamera(preset)}
              className={`rounded px-1 py-1.5 text-[11px] ${
                camera === preset
                  ? "bg-[var(--accent)] text-[#101415]"
                  : "bg-[var(--panel-strong)] text-[var(--muted)] hover:text-[var(--foreground)]"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] p-2.5 text-[10px] leading-relaxed text-[var(--muted)]">
        <p className="mb-1 font-semibold text-[var(--foreground)]">Map key</p>
        <p>
          <span className="text-[var(--accent-2)]">▪</span> Release zones — coloured by estimated
          release score. Click one to select it.
        </p>
        <p>
          <span className="text-[var(--danger)]">▪</span> Runout — where the simulated avalanche
          reaches.
        </p>
        <p>
          <span className="text-[var(--danger)]">▫</span> Dashed envelope — the &ldquo;could run
          further&rdquo; band. It is not a hard boundary.
        </p>
      </div>
    </div>
  );
}
