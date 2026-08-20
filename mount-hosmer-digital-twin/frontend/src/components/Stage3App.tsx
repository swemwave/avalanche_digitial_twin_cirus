"use client";

import { useCallback, useEffect, useState } from "react";
import { AssistantPanel } from "@/components/AssistantPanel";
import { MountainPicker } from "@/components/MountainPicker";
import { ConditionPanel } from "@/components/ConditionPanel";
import { PredictionProductPanel } from "@/components/PredictionProductPanel";
import { ResultCard } from "@/components/ResultCard";
import { Stage3Map, type CameraPreset, type ContrastMode, type SurfaceView } from "@/components/Stage3Map";
import {
  getTwinMeta,
  postAssess,
  type AssessRequest,
  type AssessResult,
  type MountainId,
  type TwinMeta,
} from "@/lib/twin";

const CAMERAS: [CameraPreset, string][] = [
  ["overview", "Overview"],
  ["north", "North"],
  ["south", "South"],
  ["top", "Top-down"],
];

const SURFACES: [SurfaceView, string][] = [
  ["natural", "Fixed satellite RGB"],
  ["hillshade", "Hillshade"],
];

const CONTRASTS: [ContrastMode, string][] = [
  ["absolute", "Absolute bands"],
  ["stretched", "Stretch to this run"],
];

export function Stage3App() {
  const [mountain, setMountain] = useState<MountainId>(null);
  const [meta, setMeta] = useState<TwinMeta | null>(null);
  const [result, setResult] = useState<AssessResult | null>(null);
  const [resultStale, setResultStale] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedZone, setSelectedZone] = useState<string | null>(null);
  const [camera, setCamera] = useState<CameraPreset>("overview");
  const [exaggeration, setExaggeration] = useState(1.5);
  const [surface, setSurface] = useState<SurfaceView>("natural");
  const [showExposure, setShowExposure] = useState(true);
  const [contrast, setContrast] = useState<ContrastMode>("absolute");
  const [drawingArea, setDrawingArea] = useState(false);
  const [drawnArea, setDrawnArea] = useState<GeoJSON.Polygon | null>(null);

  useEffect(() => {
    getTwinMeta(mountain)
      .then(setMeta)
      .catch((caught) => setError(caught instanceof Error ? caught.message : String(caught)));
  }, [mountain]);

  /**
   * Switch mountains, discarding everything bound to the previous one.
   *
   * Every result, zone selection and drawn area belongs to one specific bake.
   * Drawing Mount Hosmer's release zones over another mountain's terrain would
   * be the worst bug this feature could produce, so the state is cleared here
   * rather than left to each panel to notice.
   */
  const selectMountain = useCallback((next: MountainId) => {
    setMountain(next);
    setMeta(null);
    setResult(null);
    setResultStale(false);
    setSelectedZone(null);
    setDrawnArea(null);
    setDrawingArea(false);
    setError(null);
  }, []);

  const assess = useCallback(async (request: AssessRequest) => {
    setRunning(true);
    setError(null);
    setSelectedZone(null);
    try {
      setResult(await postAssess(request, mountain));
      setResultStale(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRunning(false);
    }
  }, [mountain]);

  const lidar = meta?.terrain.lidar_fraction != null ? `${(meta.terrain.lidar_fraction * 100).toFixed(2)}%` : "—";
  const visibleSurface: SurfaceView = meta && !meta.imagery ? "hillshade" : surface;
  // The claim -- that no imagery value enters the model -- stays on screen. The
  // capture metadata behind it is detail, and moves into the disclosure.
  const imageryClaim = meta?.imagery
    ? "Visual context only; it does not affect release or runout."
    : meta
      ? "Satellite imagery is unavailable in this bake; hillshade is shown instead."
      : "Checking this bake for satellite imagery…";
  const imagerySummary = meta?.imagery
    ? `Fixed winter satellite capture ${meta.imagery.captured_at_utc.slice(0, 10)} · ${meta.imagery.source_resolution_m.toFixed(0)} m source resolution · ${meta.imagery.cloud_percent.toFixed(1)}% scene cloud. Visual context only; it does not affect release or runout.`
    : imageryClaim;
  const exposureSummary = meta?.exposure
    ? `${meta.exposure.feature_count ?? 0} mapped feature${meta.exposure.feature_count === 1 ? "" : "s"}, ${meta.exposure.attribution}, ${meta.exposure.licence}. Built-up outlines are inferred from residential road density, not surveyed. Exposure only raises the consequence term of the hazard index; it never enters the release model.`
    : meta
      ? "This bake carries no exposure layer, so nothing is known about what lies in the runout paths. That is not a finding that they are empty."
      : "Checking this bake for exposure data…";
  const sourceResolution = meta?.terrain.effective_source_resolution_m;
  const terrainSummary = meta
    ? `Baked terrain uses ${sourceResolution != null ? `${sourceResolution.toFixed(0)} m effective source resolution` : "documented source resolution"} on a ${meta.grid.resolution_m.toFixed(0)} m analysis grid; LiDAR contributes ${lidar} of valid terrain coverage.`
    : "Loading baked terrain provenance…";

  return (
    <main className="min-h-screen bg-[var(--background)] px-5 py-6 text-[var(--foreground)] md:px-8">
      <div className="mx-auto flex max-w-[1800px] flex-col gap-5">
        <div className="sticky top-0 z-50 -mx-2 rounded-md border border-[var(--accent-2)] bg-[var(--background)]/95 px-3 py-2 text-center text-xs font-semibold text-[var(--accent-2)] shadow-lg backdrop-blur">
          Experimental research scenario · not operational · not a probability · never replaces Avalanche Canada guidance or field assessment
        </div>
        <div className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)_400px]">
          {/* Left: which mountain, then conditions */}
          <aside className="flex flex-col gap-4">
            <MountainPicker mountain={mountain} onSelect={selectMountain} />
            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Conditions
              </h3>
              <ConditionPanel
                running={running}
                onAssess={assess}
                drawnArea={drawnArea}
                onRequestDraw={() => setDrawingArea(true)}
                onDraftChange={() => {
                  if (result) setResultStale(true);
                }}
              />
            </section>

            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Terrain view
              </h3>
              <div className="flex flex-col gap-3 text-sm">
                <div className="grid grid-cols-2 gap-1">
                  {SURFACES.map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() => setSurface(id)}
                      disabled={id === "natural" && !meta?.imagery}
                      aria-pressed={visibleSurface === id}
                      className={`rounded-md px-2.5 py-1 text-xs ${
                        visibleSurface === id
                          ? "bg-[var(--accent)] text-[#101415]"
                          : "border border-[var(--border)] bg-[var(--panel-strong)] text-[var(--muted)]"
                      } disabled:cursor-not-allowed disabled:opacity-40`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <p className="text-[11px] leading-relaxed text-[var(--muted)]">{imageryClaim}</p>
                <div className="flex flex-wrap gap-1">
                  {CAMERAS.map(([id, label]) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() => setCamera(id)}
                      className={`rounded-md px-2.5 py-1 text-xs ${
                        camera === id
                          ? "bg-[var(--accent)] text-[#101415]"
                          : "border border-[var(--border)] bg-[var(--panel-strong)] text-[var(--muted)]"
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                <label className="flex flex-col gap-1">
                  <span className="flex items-baseline justify-between text-xs text-[var(--muted)]">
                    <span>Vertical exaggeration</span>
                    <span className="font-mono text-[var(--foreground)]">{exaggeration.toFixed(1)}×</span>
                  </span>
                  <input
                    type="range"
                    min={1}
                    max={3}
                    step={0.1}
                    value={exaggeration}
                    onChange={(event) => setExaggeration(Number(event.target.value))}
                    className="accent-[var(--accent)]"
                  />
                </label>
                <div className="border-t border-[var(--border)] pt-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs text-[var(--muted)]">Roads, rail and living areas</span>
                    <button
                      type="button"
                      onClick={() => setShowExposure((visible) => !visible)}
                      disabled={!meta?.exposure}
                      aria-pressed={Boolean(meta?.exposure) && showExposure}
                      className={`rounded-md px-2.5 py-1 text-xs ${
                        meta?.exposure && showExposure
                          ? "bg-[var(--accent)] text-[#101415]"
                          : "border border-[var(--border)] bg-[var(--panel-strong)] text-[var(--muted)]"
                      } disabled:cursor-not-allowed disabled:opacity-40`}
                    >
                      {meta?.exposure && showExposure ? "Shown" : "Hidden"}
                    </button>
                  </div>
                </div>

                <div className="border-t border-[var(--border)] pt-3">
                  <span className="text-xs text-[var(--muted)]">Hazard colour scale</span>
                  <div className="mt-1 grid grid-cols-2 gap-1">
                    {CONTRASTS.map(([id, label]) => (
                      <button
                        key={id}
                        type="button"
                        onClick={() => setContrast(id)}
                        aria-pressed={contrast === id}
                        className={`rounded-md px-2.5 py-1 text-xs ${
                          contrast === id
                            ? "bg-[var(--accent)] text-[#101415]"
                            : "border border-[var(--border)] bg-[var(--panel-strong)] text-[var(--muted)]"
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Four paragraphs of standing prose became one disclosure. The
                    map legend still states the active contrast mode on screen, so
                    a stretched view can never be mistaken for absolute severity
                    without opening anything. */}
                <details className="border-t border-[var(--border)] pt-3 text-[11px] leading-relaxed text-[var(--muted)]">
                  <summary className="cursor-pointer text-xs text-[var(--muted)]">
                    About these layers
                  </summary>
                  <div className="mt-2 space-y-2">
                    <p>{imagerySummary}</p>
                    <p>{terrainSummary}</p>
                    <p>{exposureSummary}</p>
                    <p>
                      Absolute bands use the published index thresholds, so colours mean the same
                      thing in every run. Stretching rescales to this run&apos;s own range to reveal
                      differences between zones — the same colour then means a different index, so
                      the legend says which mode is active.
                    </p>
                  </div>
                </details>
              </div>
            </section>
          </aside>

          {/* Centre: the mountain */}
          <div className="flex min-h-[560px] flex-col gap-2 xl:min-h-[760px]">
            <Stage3Map
              key={mountain ?? "hosmer"}
              meta={meta}
              mountain={mountain}
              result={result}
              exaggeration={exaggeration}
              camera={camera}
              surface={visibleSurface}
              showExposure={showExposure}
              contrast={contrast}
              selectedZone={selectedZone}
              drawingArea={drawingArea}
              drawnArea={drawnArea}
              onDrawnArea={(polygon) => {
                setDrawnArea(polygon);
                setDrawingArea(false);
              }}
              onCancelDrawing={() => setDrawingArea(false)}
              // Clicking the selected zone again clears the selection, so the
              // headline goes back to the whole area without hunting for a control.
              onZoneClick={(zoneId) =>
                setSelectedZone((current) => (current === zoneId ? null : zoneId))
              }
            />
          </div>

          {/* Right: result + assistant */}
          <aside className="flex flex-col gap-4">
            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Assessment
              </h3>
              <ResultCard
                result={result}
                running={running}
                error={error}
                selectedZone={selectedZone}
                onClearZone={() => setSelectedZone(null)}
                stale={resultStale}
              />
            </section>

            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Offline prediction products
              </h3>
              {/* Read-only view of what the offline pipeline produced. External
                  engines never run inside a request, so this panel reads files and
                  shows unavailable stages and outputs as such. */}
              <PredictionProductPanel />
            </section>

            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Assistant
              </h3>
              <AssistantPanel
                result={result}
                onAssessment={(assessment) => {
                  setResult(assessment);
                  setResultStale(false);
                }}
              />
            </section>
          </aside>
        </div>
      </div>
    </main>
  );
}
