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
  ["natural", "Satellite / snow"],
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
    <main className="min-h-screen bg-[var(--background)] px-5 py-6 text-[var(--foreground)] md:px-8">
      <div className="mx-auto flex max-w-[1800px] flex-col gap-5">
        <header className="border-b border-[var(--border)] pb-4">
          <p className="text-sm font-medium text-[var(--accent)]">
            Mount Hosmer avalanche digital twin
          </p>
          <h1 className="mt-1 text-3xl font-semibold">Terrain · Runout · Risk</h1>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-[var(--muted)]">
            Experimental and non-operational. Every score is a relative index, never a probability
            and never a forecast. It has not been validated against any observed Mount Hosmer
            avalanche, because no historical record exists. It must never replace Avalanche Canada
            forecasts or field assessment.
          </p>
        </header>

        <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)_380px]">
          {/* Left: conditions */}
          <aside className="flex flex-col gap-4">
            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Conditions
              </h3>
              <ConditionPanel running={running} onAssess={assess} />
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
                <p className="text-[11px] leading-relaxed text-[var(--muted)]">
                  Real 3D mesh from 1 m BC LiDAR ({lidar} of the AOI) on a 5 m grid.
                </p>
              </div>
            </section>
          </aside>

          {/* Centre: the mountain */}
          <div className="flex min-h-[560px] flex-col gap-2 xl:min-h-[760px]">
            <Stage3Map
              meta={meta}
              result={result}
              exaggeration={exaggeration}
              camera={camera}
              surface={visibleSurface}
              onZoneClick={setSelectedZone}
            />
          </div>

          {/* Right: result + assistant */}
          <aside className="flex flex-col gap-4">
            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Assessment
              </h3>
              <ResultCard result={result} running={running} error={error} selectedZone={selectedZone} />
            </section>

            <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
                Assistant
              </h3>
              <AssistantPanel result={result} onAssessment={setResult} />
            </section>
          </aside>
        </div>
      </div>
    </main>
  );
}
