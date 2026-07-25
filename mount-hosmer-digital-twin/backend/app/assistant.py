"""A small, local, offline AI assistant over the assessment -- via Ollama.

Two entry points:

* :func:`explain` -- a plain-language read of an assessment result.
* :func:`chat` -- a conversational turn that :func:`_route` sorts into one of four
  intents: **scenario** ("what if 40 cm of new snow and a SW wind?"), **question**
  ("why is RZ001 the hottest zone?", "what does aspect mean?"), **chat** (greetings /
  meta), or **advice** (a go/no-go or "is it safe?" -- always declined).

The safety rails are the same on every path, and they are not the model's job:

* The scenario path is deliberately NOT native tool-calling (a small local model is
  not reliable at that). It is **parse-to-params -> run deterministically -> narrate**:
  the model only emits slider values, we clamp them and run the *real*
  :func:`app.assess.assess`, then ask the model to describe numbers it did not produce.
* Question / chat answers are **grounded** in :data:`_KNOWLEDGE` plus the current
  result -- the model answers from that context and never fabricates a hazard number.
* Advice is refused, first by a deterministic guard (:func:`_is_advice`) and again by
  the router and the system prompt.
* The model never writes the safety disclaimer -- :data:`DISCLAIMER` is appended here,
  in code, wherever a reply leans on a hazard number.

Everything is local: Ollama on ``localhost:11434``. The only network access this
whole app needs is a one-time ``ollama pull llama3.1:8b``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from app.assess_client import AssessClient
from app.core.model_config import DISCLAIMER
from app import risk

OLLAMA_URL = os.environ.get("AVALANCHE_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("AVALANCHE_OLLAMA_MODEL", "llama3.1:8b")
TIMEOUT_S = float(os.environ.get("AVALANCHE_OLLAMA_TIMEOUT_S", "120"))

#: Prepended to every model turn, so the assistant cannot be talked out of the
#: framing. The disclaimer is still appended separately in code regardless.
SYSTEM_FRAMING = (
    "You are a cautious assistant for an EXPERIMENTAL, NON-OPERATIONAL avalanche terrain model "
    "for Mount Hosmer, BC. It is a research prototype, not an avalanche forecast, and it must "
    "never be presented as one or as a substitute for Avalanche Canada or field assessment. "
    "Never give a go / no-go or 'safe to travel' decision. Never invent numbers; use only the "
    "numbers you are given. Be brief and plain-spoken."
)


class AssistantError(RuntimeError):
    """Raised when the local model is unreachable or misbehaves."""


def _ollama_chat(system: str, user: str, *, temperature: float = 0.2, force_json: bool = False) -> str:
    """One non-streaming turn against the local Ollama server."""
    import httpx

    payload: dict[str, Any] = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    if force_json:
        payload["format"] = "json"

    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            response = client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.ConnectError as exc:
        raise AssistantError(
            f"Could not reach the local AI model at {OLLAMA_URL}. Start it with 'ollama serve' "
            f"and 'ollama pull {OLLAMA_MODEL}'."
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.json().get("error", "")
        except Exception:
            detail = exc.response.text[:200]
        if "not found" in detail.lower() or exc.response.status_code == 404:
            raise AssistantError(
                f"The model '{OLLAMA_MODEL}' is not installed in Ollama. Run: ollama pull {OLLAMA_MODEL}"
            ) from exc
        raise AssistantError(f"The local AI model returned an error: {detail}") from exc
    except httpx.HTTPError as exc:
        raise AssistantError(f"The local AI model request failed: {exc}") from exc

    return str(data.get("message", {}).get("content", "")).strip()


# --- Summarising an assessment for the model ---------------------------------


def _summarize(assessment: dict[str, Any]) -> str:
    """A compact text digest of an assessment. GeoJSON is never sent to the model."""
    cond = assessment.get("conditions", {})
    zones = assessment.get("zones", []) or []
    runout = assessment.get("runout", {}) or {}
    lines = [
        f"Conditions: {cond.get('new_snow_cm', 0)} cm new snow; wind "
        f"{cond.get('wind_speed_kmh', 0)} km/h from {cond.get('wind_direction_compass', '?')}; "
        f"release size {cond.get('release_size', '?')}.",
        f"Overall hazard index: {assessment.get('hazard_score')}/100 "
        f"(relative band: {assessment.get('risk_level')}).",
        f"Release zones found: {assessment.get('release_zones', {}).get('zone_count', 0)}.",
        f"Simulated runout footprint: {round(runout.get('core_area_m2', 0) / 1e6, 2)} km^2"
        + (f", peak modelled speed {runout.get('max_velocity_ms')} m/s." if runout.get("max_velocity_ms") else "."),
    ]
    for zone in zones[:4]:
        lines.append(
            f"- Zone {zone.get('zone_id')}: {zone.get('area_hectares')} ha, mean slope "
            f"{zone.get('mean_slope_deg')} deg, {zone.get('dominant_aspect_compass')}-facing, "
            f"release index {zone.get('estimated_release_score')}."
        )
    warnings = assessment.get("warnings", []) or []
    if warnings:
        lines.append("Model warnings: " + " | ".join(warnings[:3]))
    return "\n".join(lines)


def _with_disclaimer(text: str) -> str:
    """Append the canonical disclaimer. Model output never stands alone."""
    return f"{text.strip()}\n\n⚠ {DISCLAIMER}"


# --- Public behaviours -------------------------------------------------------


def explain(assessment: dict[str, Any]) -> dict[str, Any]:
    """Plain-language read of an assessment result."""
    summary = _summarize(assessment)
    prompt = (
        "Here is the model's output. In 2-4 sentences, explain in plain language what it is saying "
        "about the terrain and the conditions, and name the single biggest source of uncertainty. "
        "Do not give any travel advice.\n\n" + summary
    )
    reply = _ollama_chat(SYSTEM_FRAMING, prompt, temperature=0.2)
    return {
        "explanation": _with_disclaimer(reply),
        "disclaimer": DISCLAIMER,
        "model": OLLAMA_MODEL,
        "is_operational_forecast": False,
    }


#: Keys the model is allowed to set. Anything else it emits is ignored.
_PARAM_KEYS = ("new_snow_cm", "wind_speed_kmh", "wind_direction_deg", "release_size")

#: Words that signal an actual weather scenario. A message with none of these and
#: no digit is treated as chatter (a greeting, thanks, an off-topic question) and
#: gets a fast guidance reply -- instead of spending ~30s running the real model
#: on unchanged conditions and narrating a hazard number nobody asked for.
_SCENARIO_WORDS = frozenset(
    {
        "snow", "snowfall", "wind", "windy", "gust", "gusts", "gusty", "storm",
        "blizzard", "dump", "loading", "load", "loaded", "rain", "warm", "warming",
        "thaw", "melt", "cornice", "slab", "release", "mild", "calm", "breeze",
        "gale", "north", "northerly", "south", "southerly", "east", "easterly",
        "west", "westerly", "small", "medium", "large", "huge", "massive", "tiny",
        "ne", "nw", "se", "sw", "cm", "kmh", "mph",
    }
)

#: Curated, factual grounding the model may draw on to answer questions about the
#: tool. Kept accurate on purpose -- the model answers FROM this, so it does not
#: have to invent how the twin works.
_KNOWLEDGE = (
    "Reference facts about this tool (use these to answer; do not contradict them):\n"
    "- It is an EXPERIMENTAL, NON-OPERATIONAL avalanche terrain digital twin for Mount "
    "Hosmer, BC -- a single ~12x12 km mountain near Fernie. A research prototype, not a forecast.\n"
    "- Conditions come from UI sliders/presets, not live weather: new snow (cm), wind speed "
    "(km/h), wind direction (the compass direction the wind blows FROM), and release size "
    "(small / medium / large / very_large).\n"
    "- How a run works: the terrain is fixed (a 5 m LiDAR mesh). Given the slider conditions a "
    "transparent release model estimates where snow could release; zones that cross a threshold "
    "are extracted (labelled RZ001, RZ002, ... by release index); runout is then simulated for "
    "the top zones.\n"
    "- Two runout engines: 'fast' (alpha-angle routing, quick) and 'advanced' (particle-ensemble, "
    "slower and more detailed).\n"
    "- Terrain layers: elevation, slope angle, aspect (the compass direction a slope faces), plan "
    "and general curvature (how the surface bends -- concave gullies concentrate flow, convex rolls "
    "shed it), a forest mask (trees anchor snow), and distance-to-ridge (wind loading / exposure).\n"
    "- The hazard number is a RELATIVE index (0-100) with a band label -- NOT a probability and NOT "
    "calibrated.\n"
    "- Key limitation: there is NO historical avalanche record for Mount Hosmer, so nothing here is "
    "validated against real avalanches. It must never replace Avalanche Canada or field assessment."
)

#: Deterministic guard for advice-seeking. This is a safety path, so it does not
#: rely on the model classifying correctly -- these phrasings always get the refusal.
_ADVICE_RE = re.compile(
    r"\b(should i|safe to|is it safe|are we safe|ok to (ski|ride|go|travel|climb|hike)|"
    r"can i (ski|ride|go|travel|climb|hike)|go[\s/-]*no[\s/-]*go|worth (skiing|riding|going)|"
    r"recommend|do you recommend|would you (ski|ride|go))\b",
    re.IGNORECASE,
)

#: Returned whenever the user asks for a go/no-go or safety decision.
_ADVICE_REPLY = (
    "I can't give travel, go/no-go, or 'is it safe' advice -- this is an experimental terrain "
    "model, not an avalanche forecast, and it's no substitute for Avalanche Canada or a trained "
    "field assessment. I'm happy to explain what the model shows, or run a what-if scenario for you."
)


def _looks_like_scenario(message: str) -> bool:
    """Fallback scenario heuristic, used only if the router's JSON is unusable.

    A digit or any scenario word qualifies ("60 cm snow", "strong SW wind").
    """
    text = message.lower()
    if any(char.isdigit() for char in text):
        return True
    return bool(set(re.findall(r"[a-z]+", text)) & _SCENARIO_WORDS)


def _is_advice(message: str) -> bool:
    """True if the message is asking for a safety / go-no-go decision."""
    return _ADVICE_RE.search(message) is not None


def _base_conditions(assessment: dict[str, Any] | None) -> risk.Conditions:
    """The conditions a scenario should start from: the current result's, or defaults."""
    if assessment and isinstance(assessment.get("conditions"), dict):
        cond = assessment["conditions"]
        return risk.Conditions(
            new_snow_cm=float(cond.get("new_snow_cm", 0.0)),
            wind_speed_kmh=float(cond.get("wind_speed_kmh", 0.0)),
            wind_direction_deg=float(cond.get("wind_direction_deg", 225.0)),
            release_size=str(cond.get("release_size", "medium")),
        )
    return risk.Conditions()


def _format_history(history: list[dict[str, str]] | None) -> str:
    """The last few conversation turns, compacted for the model. Empty if none."""
    lines: list[str] = []
    for turn in (history or [])[-6:]:
        role = "User" if turn.get("role") == "you" else "Assistant"
        text = str(turn.get("text", "")).strip()[:500]
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _route(
    message: str, base: risk.Conditions, history: list[dict[str, str]] | None
) -> tuple[str, risk.Conditions, dict[str, Any]]:
    """Classify the message and, if it's a scenario, extract slider values -- one call.

    Returns ``(intent, conditions, detail)`` where ``intent`` is one of
    ``scenario`` / ``question`` / ``chat`` / ``advice``. ``conditions`` is only
    meaningful for a scenario (it clamps the model's slider values onto ``base``).
    """
    system = (
        SYSTEM_FRAMING
        + " You are a router for the assistant. Respond with ONLY a JSON object, no prose."
    )
    convo = _format_history(history)
    prompt = (
        "Classify the user's latest message into an intent and, if it is a weather scenario, "
        "extract slider values. Output a JSON object with keys:\n"
        '  "intent": one of "scenario" (they describe hypothetical conditions to simulate, e.g. '
        '"what if 60 cm of snow"), "question" (they ask about the model, the terrain, or the '
        'current result), "chat" (greeting / thanks / meta), or "advice" (they ask whether it is '
        "safe, or for a go/no-go or travel decision).\n"
        '  "new_snow_cm" (0-300), "wind_speed_kmh" (0-200), "wind_direction_deg" (0-360, the '
        "compass direction the wind blows FROM; a 'SW wind' is 225), \"release_size\" "
        "('small'|'medium'|'large'|'very_large') -- fill these ONLY for a scenario, as ABSOLUTE "
        "targets (not amounts to add), starting from the current values and changing only what the "
        f"message mentions. Current values: {json.dumps(base.to_dict())}.\n"
        + (f"\nRecent conversation:\n{convo}\n" if convo else "")
        + "\nLatest message: "
        + message
    )
    raw = _ollama_chat(system, prompt, temperature=0.0, force_json=True)

    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = {}

    intent = str(parsed.get("intent", "")).strip().lower()
    if intent not in {"scenario", "question", "chat", "advice"}:
        # The router misbehaved: fall back to the deterministic heuristic.
        intent = "scenario" if _looks_like_scenario(message) else "question"

    values = {key: parsed[key] for key in _PARAM_KEYS if key in parsed}

    def _num(key: str, default: float) -> float:
        try:
            return float(values[key])
        except (KeyError, TypeError, ValueError):
            return default

    conditions = risk.Conditions(
        new_snow_cm=_num("new_snow_cm", base.new_snow_cm),
        wind_speed_kmh=_num("wind_speed_kmh", base.wind_speed_kmh),
        wind_direction_deg=_num("wind_direction_deg", base.wind_direction_deg),
        release_size=str(values.get("release_size", base.release_size)),
    ).clamped()
    return intent, conditions, {"raw_model_json": values, "intent": intent}


def _answer(
    message: str,
    assessment: dict[str, Any] | None,
    history: list[dict[str, str]] | None,
    *,
    grounded_in_result: bool,
) -> str:
    """A conversational reply, grounded in the reference facts + the current result.

    The model answers only from the context it is given; it never fabricates hazard
    numbers. If the reply leans on the current assessment, the disclaimer is appended.
    """
    context = [_KNOWLEDGE]
    if assessment:
        context.append("Current model result:\n" + _summarize(assessment))
    convo = _format_history(history)
    if convo:
        context.append("Recent conversation:\n" + convo)

    system = (
        SYSTEM_FRAMING
        + " Answer the user's message using ONLY the reference facts and current result below. "
        "If the answer is not in them, say you are not sure rather than guessing. Never invent "
        "hazard numbers -- quote only numbers that appear in the context. Never give travel, "
        "go/no-go, or 'is it safe' advice. Keep it to 2-4 short sentences, plain language."
    )
    prompt = "\n\n".join(context) + f'\n\nUser message: "{message}"'
    reply = _ollama_chat(system, prompt, temperature=0.3)
    return _with_disclaimer(reply) if grounded_in_result else reply


def _envelope(
    reply: str,
    kind: str,
    *,
    parsed_conditions: dict[str, Any] | None = None,
    assessment: dict[str, Any] | None = None,
    parse_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The common chat response shape. Only a ``scenario`` carries an assessment."""
    return {
        "reply": reply,
        "kind": kind,
        "parsed_conditions": parsed_conditions,
        "parse_detail": parse_detail,
        "assessment": assessment,
        "disclaimer": DISCLAIMER,
        "model": OLLAMA_MODEL,
        "is_operational_forecast": False,
    }


def chat(
    assess: AssessClient,
    message: str,
    assessment: dict[str, Any] | None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The assistant's conversational turn. Routes to one of four behaviours.

    * ``scenario`` -- the model picks slider values, the deterministic model computes
      the hazard, the model narrates it (it never computes the numbers itself).
    * ``question`` / ``chat`` -- a grounded conversational reply about the result, the
      terrain, or the tool. No new numbers are invented.
    * ``advice`` -- a go/no-go or safety question is declined (a hard safety boundary).
    """
    # Safety first: an advice request is refused deterministically, before the model.
    if _is_advice(message):
        return _envelope(_ADVICE_REPLY, "advice")

    base = _base_conditions(assessment)
    intent, conditions, parse_detail = _route(message, base, history)

    if intent == "advice":
        return _envelope(_ADVICE_REPLY, "advice")

    if intent == "scenario":
        # Run the REAL assessment on the parsed conditions. The model does not compute
        # this -- and `assess` may be in-process or a call to the assess service; the
        # assistant deliberately cannot tell which, so neither path can drift.
        result = assess(conditions, simulation_mode="fast")
        summary = _summarize(result)
        narrate_prompt = (
            f'The user asked: "{message}"\n\n'
            "The model was run with the conditions below and produced this result. In 2-4 "
            "sentences, describe what changed and what the terrain response is, in plain language. "
            "Do not give travel advice.\n\n" + summary
        )
        reply = _ollama_chat(SYSTEM_FRAMING, narrate_prompt, temperature=0.3)
        return _envelope(
            _with_disclaimer(reply),
            "scenario",
            parsed_conditions=conditions.to_dict(),
            assessment=result,
            parse_detail=parse_detail,
        )

    # A question about the result, or ordinary chat: answer conversationally.
    reply = _answer(
        message, assessment, history, grounded_in_result=(intent == "question" and bool(assessment))
    )
    return _envelope(reply, "answer" if intent == "question" else "chat")
