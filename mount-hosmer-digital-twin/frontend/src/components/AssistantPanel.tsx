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
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-[10px] uppercase tracking-[0.12em] text-[var(--paper-faint)]">
          <span className="h-1 w-1 rounded-full bg-[var(--signal)]" />
          Local · Ollama
        </span>
        <button
          type="button"
          onClick={explain}
          disabled={busy || !result}
          className="rounded-[3px] border border-[var(--rule)] px-2.5 py-1 text-[11px] text-[var(--paper-dim)] transition-colors hover:border-[var(--rule-lit)] hover:text-[var(--paper)] disabled:cursor-not-allowed disabled:opacity-40"
        >
          Explain this result
        </button>
      </div>

      {turns.length > 0 ? (
        // A transcript, not a messaging app: each turn is labelled in the
        // margin so a long technical answer keeps full column width.
        <div className="flex max-h-72 flex-col gap-3 overflow-y-auto pr-1">
          {turns.map((turn, index) => (
            <div key={index} className="flex flex-col gap-1">
              <span
                className={`text-[9px] font-semibold uppercase tracking-[0.14em] ${
                  turn.role === "you" ? "text-[var(--paper-faint)]" : "text-[var(--signal-deep)]"
                }`}
              >
                {turn.role === "you" ? "You" : "Assistant"}
              </span>
              <p
                className={`text-[11px] leading-relaxed ${
                  turn.role === "you"
                    ? "text-[var(--paper-dim)]"
                    : "border-l border-[var(--rule-lit)] pl-2.5 text-[var(--paper)]"
                }`}
                style={{ whiteSpace: "pre-wrap" }}
              >
                {turn.text}
              </p>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <p className="text-[11px] leading-relaxed text-[var(--paper-dim)]">
            Ask a what-if to re-run the model, or a question about the result.
          </p>
          <div className="flex flex-col gap-1">
            {[
              "what if 60 cm of new snow and a strong SW wind?",
              "why is RZ001 the hottest zone?",
            ].map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setMessage(example)}
                className="data rounded-[3px] border border-[var(--rule)] px-2.5 py-1.5 text-left text-[10px] text-[var(--paper-faint)] transition-colors hover:border-[var(--rule-lit)] hover:text-[var(--paper-dim)]"
              >
                {example}
              </button>
            ))}
          </div>
          <p className="text-[10px] leading-relaxed text-[var(--paper-faint)]">
            It never decides the hazard numbers, and never gives travel advice.
          </p>
        </div>
      )}

      {busy && pending ? (
        <p className="flex items-center gap-2 text-[10px] text-[var(--paper-faint)]">
          <span className="h-1 w-1 animate-ping rounded-full bg-[var(--signal)]" />
          {pending}
        </p>
      ) : null}

      {error ? (
        <p className="border-l-2 border-[var(--alert)] bg-[var(--field-1)] px-2.5 py-1.5 text-[10px] leading-relaxed text-[var(--paper-dim)]">
          {error}
        </p>
      ) : null}

      <div className="flex gap-1.5">
        <input
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !busy) send();
          }}
          placeholder="Ask a what-if or a question…"
          disabled={busy}
          className="min-w-0 flex-1 rounded-[3px] border border-[var(--rule)] bg-[var(--field-1)] px-2.5 py-2 text-[11px] text-[var(--paper)] transition-colors placeholder:text-[var(--paper-faint)] hover:border-[var(--rule-lit)] focus:border-[var(--signal)] focus:outline-none disabled:opacity-50"
        />
        <button
          type="button"
          onClick={send}
          disabled={busy || !message.trim()}
          className="rounded-[3px] border border-[var(--signal)] px-3 py-2 text-[11px] font-semibold text-[var(--signal)] transition-colors hover:bg-[var(--signal)] hover:text-[var(--field)] disabled:cursor-not-allowed disabled:border-[var(--rule)] disabled:text-[var(--paper-faint)] disabled:hover:bg-transparent"
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
