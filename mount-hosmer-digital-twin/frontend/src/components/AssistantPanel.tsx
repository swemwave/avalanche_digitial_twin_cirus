"use client";

import { useState } from "react";
import { postChat, postExplain, TwinApiError, type AssessResult } from "@/lib/twin";

type Turn = { role: "you" | "assistant"; text: string };

type Props = {
  result: AssessResult | null;
  /** A scenario reply carries a fresh assessment; lift it so the map updates too. */
  onAssessment: (assessment: AssessResult) => void;
};
// The assistant panel shows a chat interface for asking what-ifs and questions about the current result. It runs on the user's machine, so it can read the result and terrain without sending them to a server.
export function AssistantPanel({ result, onAssessment }: Props) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

// The explain button runs a local model that reads the result and terrain, and returns a text explanation of the result. It does not change the map or hazard numbers.
  const explain = async () => {
    if (!result) return;
    setBusy(true);
    setPending("Reading the result on your machine…");
    setError(null);
    try {
      const body = await postExplain(result);
      setTurns((prev) => [...prev, { role: "assistant", text: body.explanation }]);
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  // The send button sends the user's message to the assistant, which can either run a what-if scenario (and return a new assessment) or answer a question (without changing the map).
  const send = async () => {
    const text = message.trim();
    if (!text) return;
    const history = turns; // the prior conversation, so the assistant can follow along
    setBusy(true);
    setPending("Thinking on your machine — a what-if runs the full model (~20–30s)…");
    setError(null);
    setTurns((prev) => [...prev, { role: "you", text }]);
    setMessage("");
    try {
      const body = await postChat(text, result, history);
      // Only a scenario carries a fresh assessment; it re-runs the map. A question,
      // chat, or declined-advice reply is just shown, leaving the map untouched.
      if (body.kind === "scenario" && body.assessment && body.parsed_conditions) {
        onAssessment(body.assessment);
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
      } else {
        setTurns((prev) => [...prev, { role: "assistant", text: body.reply }]);
      }
    } catch (caught) {
      setError(describe(caught));
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  return (
    <div className="flex flex-col gap-3 text-sm">
      <div className="flex items-center justify-between">
        <span className="text-xs text-[var(--muted)]">Local AI · runs on your machine (Ollama)</span>
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
          Ask a what-if — “what if 60 cm of new snow and a strong SW wind?” — or a question —
          “why is RZ001 the hottest zone?”, “what does aspect mean?” A what-if runs the real
          model; questions are answered from the model and terrain. It never decides the hazard
          numbers, and never gives travel advice.
        </p>
      )}

      {busy && pending ? (
        <p className="animate-pulse rounded-md bg-[var(--panel-strong)] px-2.5 py-1.5 text-[11px] text-[var(--muted)]">
          {pending}
        </p>
      ) : null}

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
          placeholder="ask a what-if or a question…"
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
// This function stringifies the body and sets the content-type header.
function describe(caught: unknown): string {
  if (caught instanceof TwinApiError && caught.status === 503) {
    return `${caught.message}`;
  }
  return caught instanceof Error ? caught.message : String(caught);
}
