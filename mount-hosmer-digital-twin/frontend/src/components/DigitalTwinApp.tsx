"use client";

import { useCallback, useState } from "react";
import { AvalancheTwin } from "@/components/AvalancheTwin";
import { ConditionsDashboard } from "@/components/ConditionsDashboard";
import { DataHealthPage } from "@/components/DataHealthPage";
import { EventViewer } from "@/components/EventViewer";
import { OverviewDashboard } from "@/components/OverviewDashboard";
import { SimulationHistory } from "@/components/SimulationHistory";
import { SusceptibilityPage } from "@/components/SusceptibilityPage";
import { TerrainViewer } from "@/components/TerrainViewer";
import type { SimulationSummary } from "@/lib/apiV1";

type View =
  | "twin"
  | "history"
  | "health"
  | "terrain"
  | "events"
  | "conditions"
  | "susceptibility"
  | "overview";

const TABS: [View, string][] = [
  ["twin", "Avalanche Twin"],
  ["history", "History"],
  ["health", "Data Health"],
  ["terrain", "Terrain"],
  ["events", "Satellite Events"],
  ["conditions", "Conditions"],
  ["susceptibility", "Susceptibility"],
  ["overview", "Data Overview"],
];

export function DigitalTwinApp() {
  const [view, setView] = useState<View>("twin");
  const [reopen, setReopen] = useState<SimulationSummary | null>(null);

  const onReproduce = useCallback((summary: SimulationSummary) => {
    setReopen(summary);
    setView("twin");
  }, []);

  const onReopened = useCallback(() => setReopen(null), []);

  return (
    <main className="min-h-screen bg-[var(--background)] px-5 py-6 text-[var(--foreground)] md:px-8">
      <div className="mx-auto flex max-w-[1800px] flex-col gap-6">
        <header className="flex flex-col gap-4 border-b border-[var(--border)] pb-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <p className="text-sm font-medium text-[var(--accent)]">
              Physics-informed terrain- and conditions-based avalanche digital twin
            </p>
            <h1 className="mt-1 text-3xl font-semibold">Mount Hosmer</h1>
            {/* The disclaimer lives in the header, on every view, always. */}
            <p className="mt-1 max-w-3xl text-xs leading-relaxed text-[var(--accent-2)]">
              Experimental and non-operational. Every score is a relative index, never a probability
              and never a forecast. It has not been validated against any observed Mount Hosmer
              avalanche, because no historical record exists. It must never replace Avalanche Canada
              forecasts or field assessment.
            </p>
          </div>
          <nav className="flex flex-wrap gap-1 rounded-lg border border-[var(--border)] bg-[var(--panel)] p-1">
            {TABS.map(([id, label]) => (
              <button
                key={id}
                type="button"
                onClick={() => setView(id)}
                className={`rounded-md px-3 py-2 text-sm ${
                  view === id
                    ? "bg-[var(--accent)] text-[#101415]"
                    : "text-[var(--muted)] hover:bg-[var(--panel-strong)]"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {view === "twin" ? (
          <AvalancheTwin reopen={reopen} onReopened={onReopened} />
        ) : view === "history" ? (
          <SimulationHistory onReproduce={onReproduce} />
        ) : view === "health" ? (
          <DataHealthPage />
        ) : view === "terrain" ? (
          <TerrainViewer />
        ) : view === "events" ? (
          <EventViewer />
        ) : view === "conditions" ? (
          <ConditionsDashboard />
        ) : view === "susceptibility" ? (
          <SusceptibilityPage />
        ) : (
          <OverviewDashboard embedded />
        )}
      </div>
    </main>
  );
}
