"use client";

import { useCallback, useEffect, useState } from "react";
import { AnalysisControls } from "@/components/AnalysisControls";
import { LayerControl, type LayerRecord } from "@/components/LayerControl";
import { ResultsPanel } from "@/components/ResultsPanel";
import { TwinMap, type CameraPreset, type ImageCorners } from "@/components/TwinMap";
import {
  getAnalysis,
  getSimulation,
  listLayers,
  runAnalysis,
  runSimulation,
  type Analysis,
  type Job,
  type ReleaseSize,
  type Simulation,
  type SimulationMode,
  type SimulationSummary,
} from "@/lib/apiV1";

type Props = {
  /** A run selected in the History tab, to be reopened here. */
  reopen: SimulationSummary | null;
  onReopened: () => void;
};

export function AvalancheTwin({ reopen, onReopened }: Props) {
  const [layers, setLayers] = useState<LayerRecord[]>([]);
  const [layerId, setLayerId] = useState<string | null>("terrain_susceptibility");
  const [opacity, setOpacity] = useState(0.7);
  const [exaggeration, setExaggeration] = useState(1.5);
  const [camera, setCamera] = useState<CameraPreset>("overview");

  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // Through the typed client, which sets cache: "no-store". A raw fetch() here
    // let the browser serve a stale layer list from cache after the terrain was
    // rebuilt -- and it never showed up in the server log, which made it look like
    // the request was not happening at all.
    listLayers()
      .then((body) => setLayers(body.layers))
      .catch(() => setLayers([]));
  }, []);

  const run = useCallback(
    async (input: {
      mode: "current" | "historical" | "scenario";
      at?: string;
      preset?: string;
      scenario?: Record<string, unknown>;
      eventId?: string;
      simulationMode: SimulationMode;
      releaseSize: ReleaseSize;
    }) => {
      setRunning(true);
      setError(null);
      setSimulation(null);
      try {
        const produced = await runAnalysis(
          {
            mode: input.mode,
            at: input.at,
            preset: input.preset,
            scenario: input.scenario,
            event_id: input.eventId,
          },
          setJob,
        );
        setAnalysis(produced);

        // No zones is a real answer, not a failure. Simulating nothing would error.
        if (produced.release_zones.zone_count > 0) {
          const simulated = await runSimulation(
            {
              analysis_id: produced.analysis_id,
              simulation_mode: input.simulationMode,
              release_size: input.releaseSize,
              seed: 42,
            },
            setJob,
          );
          setSimulation(simulated);
        }
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setRunning(false);
        setJob(null);
      }
    },
    [],
  );

  // Reopen a stored run. Nothing is recomputed: the analysis and the simulation are
  // read back exactly as they were written, which is the whole point of storing the
  // seed and the config hash with them.
  useEffect(() => {
    if (!reopen) return;
    let cancelled = false;

    (async () => {
      setRunning(true);
      setError(null);
      try {
        const [storedAnalysis, storedSimulation] = await Promise.all([
          getAnalysis(String(reopen.analysis_id)),
          getSimulation(reopen.simulation_id),
        ]);
        if (cancelled) return;
        setAnalysis(storedAnalysis);
        setSimulation(storedSimulation);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        if (!cancelled) {
          setRunning(false);
          onReopened();
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [reopen, onReopened]);

  const active = layers.find((layer) => layer.id === layerId);

  return (
    <div className="grid gap-4 xl:grid-cols-[300px_minmax(0,1fr)_380px]">
      <aside className="flex flex-col gap-4">
        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
            Conditions
          </h3>
          <AnalysisControls running={running} job={job} onRun={run} />
        </section>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
            Terrain
          </h3>
          <LayerControl
            layers={layers}
            selected={layerId}
            opacity={opacity}
            exaggeration={exaggeration}
            camera={camera}
            onSelect={setLayerId}
            onOpacity={setOpacity}
            onExaggeration={setExaggeration}
            onCamera={setCamera}
          />
        </section>
      </aside>

      <div className="flex min-h-[560px] flex-col gap-2 xl:min-h-[760px]">
        {error ? (
          <p className="rounded-md border border-[var(--danger)] bg-[var(--danger)]/10 px-3 py-2 text-xs text-[var(--danger)]">
            {error}
          </p>
        ) : null}
        <TwinMap
          overlayId={layerId}
          overlayCoordinates={(active?.coordinates as ImageCorners | undefined) ?? null}
          overlayOpacity={opacity}
          exaggeration={exaggeration}
          analysis={analysis}
          simulation={simulation}
          camera={camera}
        />
        <p className="text-[11px] leading-relaxed text-[var(--muted)]">
          Terrain is a real 3D mesh built from <strong>1 m BC LiDAR</strong> (99.93% of the AOI) on a
          5 m analysis grid — not the 30 m fallback DEM. This is an experimental, non-operational
          research tool.
        </p>
      </div>

      <aside className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
        <h3 className="mb-3 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
          Results
        </h3>
        <ResultsPanel analysis={analysis} simulation={simulation} />
      </aside>
    </div>
  );
}
