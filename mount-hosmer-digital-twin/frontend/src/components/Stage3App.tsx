"use client";

import { useCallback, useEffect, useState } from "react";
import { AssistantPanel } from "@/components/AssistantPanel";
import { ConditionPanel } from "@/components/ConditionPanel";
import { ResultCard } from "@/components/ResultCard";
import { Stage3Map, type CameraPreset, type SurfaceView } from "@/components/Stage3Map";
import { getTwinMeta, postAssess, type AssessRequest, type AssessResult, type TwinMeta } from "@/lib/twin";

const CAMERAS: [CameraPreset, string][] = [
  ["overview", "Overview"],
  ["north", "North"],
  ["south", "South"],
  ["top", "Top-down"],
];

const SURFACES: [SurfaceView, string][] = [
  ["natural", "Satellite"],
  ["hillshade", "Hillshade"],
];

export function Stage3App() {
  const [meta, setMeta] = useState<TwinMeta | null>(null);
  const [result, setResult] = useState<AssessResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedZone, setSelectedZone] = useState<string | null>(null);
  const [camera, setCamera] = useState<CameraPreset>("overview");
  const [exaggeration, setExaggeration] = useState(1.5);
  const [surface, setSurface] = useState<SurfaceView>("natural");

  useEffect(() => {
    getTwinMeta()
      .then(setMeta)
      .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
  }, []);

  const assess = useCallback(async (request: AssessRequest) => {
    setRunning(true);
    setError(null);
    setSelectedZone(null);
    try {
      setResult(await postAssess(request));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRunning(false);
    }
  }, []);

  const lidar = meta?.terrain.lidar_fraction != null ? `${(meta.terrain.lidar_fraction * 100).toFixed(2)}%` : "—";
  const visibleSurface: SurfaceView = meta && !meta.imagery ? "hillshade" : surface;

  return (
    <main className="flex min-h-screen flex-col bg-[var(--field)] text-[var(--paper)]">
      {/* ===================================================================
          Masthead. One line. The old header spent ~180px of vertical space on
          a title plus a five-line disclaimer; the disclaimer now lives in the
          persistent strip at the bottom, where it is always on screen instead
          of scrolled past once and forgotten.
          =================================================================== */}
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--rule)] px-5 py-3 md:px-7">
        <div className="flex items-baseline gap-3">
          <h1
            className="text-[19px] font-bold tracking-tight text-[var(--paper)]"
            style={{ fontFamily: "var(--font-display)" }}
          >
            CIRUS AI Digital Twin
          </h1>
          <span className="hidden text-xs text-[var(--paper-faint)] sm:inline">
            avalanche terrain instrument
          </span>
        </div>

        {/* Provenance, stated as instrument readings rather than prose. */}
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
          <Reading label="LiDAR cover" value={lidar} />
          <Reading label="Grid" value="5 m" />
          <Reading label="Mode" value={running ? "running" : result ? "assessed" : "idle"} live={running} />
        </div>
      </header>

      {/* ===================================================================
          Working area: control rail · terrain · readout rail.
          =================================================================== */}
      <div className="grid flex-1 gap-4 px-5 py-4 md:px-7 xl:grid-cols-[290px_minmax(0,1fr)_370px]">
        {/* ---------------- control rail ---------------- */}
        <aside className="flex min-w-0 flex-col">
          <Region label="Conditions" />
          <div className="min-h-0 flex-1">
            <ConditionPanel running={running} onAssess={assess} />
          </div>
        </aside>

        {/* ---------------- terrain: the hero ----------------
            No card, no padding, no matching border — the mountain is the
            product, so it is the only element that goes edge to edge. View
            controls float ON it, the way every real map application does,
            which also frees a whole panel out of the left rail. */}
        <div className="relative min-h-[520px] xl:min-h-[calc(100vh-190px)]">
          <Stage3Map
            meta={meta}
            result={result}
            exaggeration={exaggeration}
            camera={camera}
            surface={visibleSurface}
            onZoneClick={setSelectedZone}
          />

          <div className="pointer-events-none absolute inset-x-3 top-3 flex flex-wrap items-start justify-between gap-2">
            <div className="pointer-events-auto flex gap-px overflow-hidden rounded-[3px] border border-[var(--rule-lit)] bg-[rgba(7,13,21,0.82)] backdrop-blur-sm">
              {SURFACES.map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setSurface(id)}
                  disabled={id === "natural" && !meta?.imagery}
                  className={`px-3 py-1.5 text-[11px] font-medium transition-colors ${
                    visibleSurface === id
                      ? "bg-[var(--signal)] text-[var(--field)]"
                      : "text-[var(--paper-dim)] hover:bg-[var(--field-2)] hover:text-[var(--paper)]"
                  } disabled:cursor-not-allowed disabled:opacity-35`}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="pointer-events-auto flex gap-px overflow-hidden rounded-[3px] border border-[var(--rule-lit)] bg-[rgba(7,13,21,0.82)] backdrop-blur-sm">
              {CAMERAS.map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setCamera(id)}
                  className={`px-3 py-1.5 text-[11px] font-medium transition-colors ${
                    camera === id
                      ? "bg-[var(--signal)] text-[var(--field)]"
                      : "text-[var(--paper-dim)] hover:bg-[var(--field-2)] hover:text-[var(--paper)]"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {/* Exaggeration reads like a lens adjustment, so it sits with the
              other view controls rather than in a settings panel. */}
          <div className="pointer-events-auto absolute bottom-3 left-3 w-[210px] rounded-[3px] border border-[var(--rule-lit)] bg-[rgba(7,13,21,0.82)] px-3 py-2 backdrop-blur-sm">
            <label className="flex flex-col gap-1">
              <span className="flex items-baseline justify-between">
                <span className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--paper-faint)]">
                  Vertical exaggeration
                </span>
                <span className="data text-[11px] text-[var(--signal)]">{exaggeration.toFixed(1)}×</span>
              </span>
              <input
                type="range"
                min={1}
                max={3}
                step={0.1}
                value={exaggeration}
                onChange={(event) => setExaggeration(Number(event.target.value))}
              />
            </label>
          </div>
        </div>

        {/* ---------------- readout rail ---------------- */}
        <aside className="flex min-w-0 flex-col gap-5">
          <div className="flex min-w-0 flex-col">
            <Region label="Assessment" />
            <ResultCard result={result} running={running} error={error} selectedZone={selectedZone} />
          </div>

          <div className="flex min-w-0 flex-col border-t border-[var(--rule)] pt-4">
            <Region label="Assistant" />
            <AssistantPanel result={result} onAssessment={setResult} />
          </div>
        </aside>
      </div>

      {/* ===================================================================
          The standing caveat. Always visible, never in the way.
          =================================================================== */}
      <footer className="shrink-0 border-t border-[var(--rule)] px-5 py-2.5 md:px-7">
        <p className="text-[11px] leading-relaxed text-[var(--paper-faint)]">
          <span className="font-semibold text-[var(--signal-deep)]">Experimental · non-operational.</span>{" "}
          Every score is a relative index, never a probability and never a forecast. It has not been
          validated against any observed Mount Hosmer avalanche, because no historical record exists.
          It must never replace Avalanche Canada forecasts or field assessment.
        </p>
      </footer>
    </main>
  );
}

/** A masthead instrument reading: quiet label, mono value. */
function Reading({ label, value, live = false }: { label: string; value: string; live?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--paper-faint)]">{label}</span>
      <span className={`data text-xs ${live ? "text-[var(--signal)]" : "text-[var(--paper-dim)]"}`}>
        {value}
      </span>
    </div>
  );
}

/** Region marker. A hairline rule that runs to the edge of the column, with the
 *  name sitting on it — it divides the instrument into named areas instead of
 *  boxing each one in an identical card. */
function Region({ label }: { label: string }) {
  return (
    <div className="mb-3 flex items-center gap-3">
      <span className="region-label whitespace-nowrap">{label}</span>
      <span className="h-px flex-1 bg-[var(--rule)]" />
    </div>
  );
}
