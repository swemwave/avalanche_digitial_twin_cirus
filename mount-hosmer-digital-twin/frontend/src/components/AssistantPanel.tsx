"use client";

import { useState } from "react";
import { postChat, postExplain, TwinApiError, type AssessResult } from "@/lib/twin";

type Turn = { role: "you" | "assistant"; text: string };

type Props = {
  result: AssessResult | null;
  /** A scenario reply carries a fresh assessment; lift it so the map updates too. */
  onAssessment: (assessment: AssessResult) => void;
};

export function AssistantPanel({ result, onAssessment }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const explain = async () => {
    if (!result) return;
    setBusy(true);
    setError(null);
    try {
      const body = await postExplain(result);
      setTurns((prev) => [...prev, { role: "assistant", text: body.explanation }]);
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    const text = message.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    setTurns((prev) => [...prev, { role: "you", text }]);
    setMessage("");
    try {
      const body = await postChat(text, result);
      onAssessment(body.assessment); // re-runs the map on the parsed scenario
      const parsed = body.parsed_conditions;
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant",
          text:
            `Ran: ${parsed.new_snow_cm} cm snow, wind ${parsed.wind_speed_kmh} km/h from ` +
            `${parsed.wind_direction_compass}, ${parsed.release_size}.\n\n${body.reply}`,
        },
      ]);
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs text-[var(--muted)]">Local AI · offline (Ollama)</span>
        <button
          type="button"
          onClick={explain}
          disabled={busy || !result}
          className="rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2.5 py-1 text-xs text-[var(--muted)] hover:text-[var(--foreground)] disabled:opacity-50"
        >
          Explain this result
        </button>
      </div>

      {turns.length > 0 ? (
        <div className="flex max-h-64 flex-col gap-2 overflow-y-auto pr-1">
          {turns.map((turn, index) => (
            <div
              key={index}
              className={`rounded-md px-2.5 py-1.5 text-xs leading-relaxed ${
                turn.role === "you"
                  ? "self-end bg-[var(--accent)] text-[#101415]"
                  : "bg-[var(--panel-strong)] text-[var(--foreground)]"
              }`}
              style={{ whiteSpace: "pre-wrap" }}
            >
              {turn.text}
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[11px] leading-relaxed text-[var(--muted)]">
          Ask a what-if — e.g. “what if 60 cm of new snow and a strong SW wind?” The assistant sets
          the sliders, runs the real model, and describes the result. It never decides the numbers,
          and never gives travel advice.
        </p>
      )}

      {error ? (
        <p className="rounded-md border border-[var(--danger)] bg-[var(--panel-strong)] px-2.5 py-1.5 text-[11px] text-[var(--danger)]">
          {error}
        </p>
      ) : null}

      <div className="flex gap-2">
        <input
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !busy) send();
          }}
          placeholder="what if 60 cm snow + SW wind?"
          disabled={busy}
          className="min-w-0 flex-1 rounded-md border border-[var(--border)] bg-[var(--panel-strong)] px-2.5 py-1.5 text-xs"
        />
        <button
          type="button"
          onClick={send}
          disabled={busy || !message.trim()}
          className="rounded-md bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-[#101415] disabled:opacity-50"
        >
          {busy ? "…" : "Ask"}
        </button>
      </div>
    </div>
  );
}

function describe(caught: unknown): string {
  if (caught instanceof TwinApiError && caught.status === 503) {
    return `${caught.message}`;
  }
  return caught instanceof Error ? caught.message : String(caught);
}
