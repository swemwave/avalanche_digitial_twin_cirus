"use client";

import { useState } from "react";

/**
 * The in-app answer to "what is all this and how do I use it?".
 *
 * Every parameter in the advanced workspace appears here with what it does, in
 * the same three-way grouping the workspace uses: it moves the number, it raises
 * a warning, or it is recorded for provenance. The point of the page is that a
 * reader should never have to guess which of those a field is.
 */

type Entry = { term: string; body: string };

const ROLES: { key: string; label: string; colour: string; blurb: string }[] = [
  {
    key: "active",
    label: "Moves the number",
    colour: "var(--accent)",
    blurb:
      "This value goes into the release or runout equations. Change it and the index or the runout footprint changes.",
  },
  {
    key: "advisory",
    label: "Raises a warning",
    colour: "var(--accent-2)",
    blurb:
      "The model has no term for this, so it cannot change the number. Recording it produces a written advisory on the result instead — for the strongest evidence, one that says to trust your observation over the model.",
  },
];

const ACTIVE: Entry[] = [
  {
    term: "New storm-snow depth (cm)",
    body: "How much new snow the storm has left. This is the main loading term. It saturates at 50 cm — beyond that the loading term is already at its maximum, so 80 cm and 200 cm score the same.",
  },
  {
    term: "Representative transport wind (km/h)",
    body: "A representative sustained speed, not a peak gust. Below 15 km/h nothing is transported; by 40 km/h transport is at its configured maximum. This is what builds wind slabs on lee slopes.",
  },
  {
    term: "Wind direction FROM (° true)",
    body: "The meteorological convention: the direction the wind blows FROM, clockwise from true north. A 225° wind is a southwesterly and loads the northeast-facing slopes. You can leave it blank only when wind is 15 km/h or under, because at that speed it changes nothing.",
  },
  {
    term: "Release-size assumption",
    body: "Which regional angle-of-reach the runout uses. Bigger classes run further. It is an explicit assumption you are choosing, not an observation or a prediction of what size will occur.",
  },
  {
    term: "Air temperature (°C)",
    body: "Classifies how much of the new precipitation is snow rather than rain, using a 0–2 °C band: at or below 0 °C all snow, at or above 2 °C all rain, interpolating between. Rain does not build a dry storm slab, so the loading term drops. Leave it blank and the result is identical to not using temperature at all.",
  },
  {
    term: "Flow regime",
    body: "Points the runout engines at a different published friction set — wet snow stops sooner, powder runs further. They stay dry-snow engines running on different uncalibrated constants; choosing 'powder' does not add powder-cloud physics.",
  },
  {
    term: "User alpha angle (°)",
    body: "Replaces the regional angle of reach for a sensitivity run, clamped to 15–40°. A smaller angle runs further. Your angle is no more calibrated to Mount Hosmer than the default it replaces.",
  },
];

const ADVISORY: Entry[] = [
  {
    term: "Whumpfing, shooting cracks, recent avalanche activity",
    body: "The three classic signs that the snowpack is unstable right now. Recording any of them produces the strongest advisory on the card, above the number, saying the model cannot see them and that this evidence outranks a terrain model.",
  },
  {
    term: "Weak-layer depth and type",
    body: "Surface hoar, facets and depth hoar are persistent layers that keep terrain dangerous long after a storm. The model has no snowpack layers at all, so it is blind to precisely this — which is why recording one produces a critical advisory rather than a number change.",
  },
  {
    term: "Stability-test result",
    body: "ECT, PST and compression results are kept with full provenance. A propagating result (ECTP, PST end) raises a critical advisory, because propagation is what turns a failure into a slab avalanche.",
  },
  {
    term: "Total snow depth, snow water equivalent, slab depth",
    body: "Retained as snowpack context. They are not inputs to the current equations, and they appear in the collapsed 'further records retained' section.",
  },
  {
    term: "Trigger type, release depth",
    body: "Retained as release context. The model estimates whether terrain can release, not what triggers it or how deep the crown would be.",
  },
  {
    term: "Avalanche density",
    body: "Retained as flow context. The runout engines model geometry and friction, not mass, so density has nothing to attach to.",
  },
];

const FIELDS: Entry[] = [
  {
    term: "Status — measured / estimated / assumed / unknown",
    body: "How you came by the value. 'Measured' and 'estimated' require a UTC time and a source, because a number claiming to be a measurement has to say where it came from. 'Assumed' is a what-if. 'Unknown' means blank, and blank is never silently turned into zero.",
  },
  {
    term: "Observed at (UTC)",
    body: "When the observation was taken. Required for measured and estimated values. Time is not part of the model — it is provenance, so a reader can judge whether a 6 a.m. reading still applies.",
  },
  {
    term: "Source",
    body: "Who or what produced the value: a weather station, a field notebook, a forecast, your own judgement. Required alongside a UTC time for measured and estimated values.",
  },
  {
    term: "Uncertainty",
    body: "Quantified (±, with units and a basis), qualitative, unknown, or not provided. It is reported with the result but is NOT propagated into the index — the model does not produce error bars.",
  },
  {
    term: "Applies to — whole area / elevation band / aspect range / drawn area",
    body: "Where on the mountain the value applies. Cells outside every active input's scope are excluded from the result rather than assumed. A drawn area is applied at baked grid-cell centres; draw it on the map with the condition-scope tool.",
  },
];

const QUESTIONS: Entry[] = [
  {
    term: "Why can't weak layers change the number?",
    body: "Because there is no honest way to make them. The release model is release(slope, aspect, curvature, forest | new snow, wind, direction) — it has no snowpack layers. To make 'ECTP down 40 cm on surface hoar' move the index, someone has to pick a coefficient, and there is no published mapping for this model and no Mount Hosmer calibration to fit one. That coefficient would be invented. An index that silently contains an invented weak-layer term is more dangerous than one that admits it has none, so the app says so loudly instead.",
  },
  {
    term: "Then what is the point of recording them?",
    body: "Three things. They produce advisories that sit above the number and change how you are told to read it. They are preserved with full provenance in the scenario record and its replay hash. And they are exactly the inputs a future calibrated model would need, captured in a contract that already validates units, times and sources.",
  },
  {
    term: "The index went DOWN when I entered a warm temperature. Is the day safer?",
    body: "No, and the app says so in a critical advisory. Above 2 °C the precipitation is classified as rain, so it does not build a dry slab and the dry-slab loading term falls. This is a dry-slab model; it does not represent rain-on-snow, wet-loose or wet-slab avalanches at all. A lower number there means 'less dry-slab loading', not 'less hazard'.",
  },
  {
    term: "What does 'not calibrated' actually mean here?",
    body: "Every constant — the slope band, the release threshold, the alpha angles, the friction sets, the exposure weights — comes from avalanche-terrain literature and Canadian Rockies practice. None is fitted to an observed Mount Hosmer avalanche, because no eligible local observation dataset is available to the project. So the numbers are relative and comparable to each other, never probabilities and never a forecast.",
  },
  {
    term: "Do I have to fill all of this in?",
    body: "No. Four inputs are required: new snow, wind speed, wind direction and release size. Everything else is optional, and leaving a field blank reproduces exactly the result you would get without it — blank is unknown, never a default guess.",
  },
  {
    term: "What is the difference between the simple and advanced panels?",
    body: "Simple records the four required inputs as whole-area assumptions with no provenance. Advanced lets you say where each value came from, when, how certain it is, and where on the mountain it applies — and lets you record the snowpack and field evidence that drives the advisories.",
  },
];

function Section({ title, entries }: { title: string; entries: Entry[] }) {
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--accent)]">
        {title}
      </h4>
      <dl className="space-y-2">
        {entries.map((entry) => (
          <div key={entry.term}>
            <dt className="text-[12px] font-semibold text-[var(--foreground)]">{entry.term}</dt>
            <dd className="text-[11px] leading-relaxed text-[var(--muted)]">{entry.body}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export function ConditionsHelp() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] text-[var(--muted)] hover:text-[var(--foreground)]"
      >
        What do these mean?
      </button>

      {open ? (
        <div
          className="fixed inset-0 z-[100] flex items-start justify-center overflow-y-auto bg-[#000]/70 p-4 backdrop-blur-sm"
          role="dialog"
          aria-modal="true"
          aria-label="Advanced conditions help"
          onClick={(event) => {
            if (event.target === event.currentTarget) setOpen(false);
          }}
        >
          <div className="my-6 w-full max-w-2xl rounded-lg border border-[var(--border)] bg-[var(--panel)] p-5 shadow-2xl">
            <div className="mb-3 flex items-start justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold text-[var(--foreground)]">
                  Using the conditions panel
                </h3>
                <p className="mt-0.5 text-[11px] text-[var(--muted)]">
                  What each input is, and which of them can change the number.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="shrink-0 rounded border border-[var(--border)] px-2 py-1 text-xs text-[var(--muted)] hover:text-[var(--foreground)]"
              >
                Close
              </button>
            </div>

            <div className="mb-4 grid gap-2 sm:grid-cols-2">
              {ROLES.map((role) => (
                <div
                  key={role.key}
                  className="rounded-md border bg-[var(--panel-strong)] p-2"
                  style={{ borderColor: role.colour }}
                >
                  <p className="text-[12px] font-semibold" style={{ color: role.colour }}>
                    {role.label}
                  </p>
                  <p className="mt-0.5 text-[11px] leading-relaxed text-[var(--muted)]">
                    {role.blurb}
                  </p>
                </div>
              ))}
            </div>

            <div className="space-y-4">
              <Section title="Inputs that move the number" entries={ACTIVE} />
              <Section title="Inputs that raise a warning" entries={ADVISORY} />
              <Section title="The fields on every record" entries={FIELDS} />
              <Section title="Common questions" entries={QUESTIONS} />
            </div>

            <p className="mt-4 border-t border-[var(--border)] pt-3 text-[11px] leading-relaxed text-[var(--muted)]">
              This is an experimental research prototype. Every score is a relative index, never a
              probability and never a forecast, and it must never replace Avalanche Canada forecasts
              or field assessment.
            </p>
          </div>
        </div>
      ) : null}
    </>
  );
}
