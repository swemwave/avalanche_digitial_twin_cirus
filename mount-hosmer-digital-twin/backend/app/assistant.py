"""A small, local, offline AI assistant over the assessment -- via Ollama.

Two behaviours:

* :func:`explain` -- a plain-language read of an assessment result.
* :func:`chat` -- scenario questions ("what if 40 cm of new snow and a SW wind?").

The scenario path is deliberately NOT native tool-calling: a small local model is
not reliable at that. Instead it is **parse-to-params -> run deterministically ->
narrate**. The model only ever emits a JSON of slider values; we validate and clamp
them, run the *real* :func:`app.assess.assess` on them, and then ask the model to
describe the numbers the model itself did not produce. The model never computes the
hazard, and it never writes the safety disclaimer -- :data:`DISCLAIMER` is appended
here, in code, on every response.

Everything is local: Ollama on ``localhost:11434``. The only network access this
whole app needs is a one-time ``ollama pull llama3.1:8b``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from app.baked import BakedTerrain
from app.core.model_config import DISCLAIMER
from app import assess as assess_mod
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


def _parse_conditions(message: str, base: risk.Conditions) -> tuple[risk.Conditions, dict[str, Any]]:
    """Ask the model for slider values ONLY, as JSON. Validate and clamp them."""
    system = (
        SYSTEM_FRAMING
        + " You translate a described weather scenario into numeric slider values. Respond with "
        "ONLY a JSON object, no prose."
    )
    prompt = (
        "Convert this scenario into slider values. Output a JSON object with keys: "
        "new_snow_cm (0-300), wind_speed_kmh (0-200), wind_direction_deg (0-360, the compass "
        "direction the wind blows FROM; a 'SW wind' is 225), release_size "
        "('small'|'medium'|'large'|'very_large'). Start from these current values and change only "
        f"the fields the scenario mentions: {json.dumps(base.to_dict())}. Each value is an ABSOLUTE "
        "target, never an amount to add to the current value: '60 cm of new snow' means "
        "new_snow_cm = 60, not current + 60.\n\nScenario: " + message
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
    return conditions, {"raw_model_json": values}


def chat(bt: BakedTerrain, message: str, assessment: dict[str, Any] | None) -> dict[str, Any]:
    """Scenario chat: parse -> run the real assessment -> narrate the result.

    The model picks the slider values; the deterministic model computes the hazard.
    """
    base = risk.Conditions()
    if assessment and isinstance(assessment.get("conditions"), dict):
        cond = assessment["conditions"]
        base = risk.Conditions(
            new_snow_cm=float(cond.get("new_snow_cm", 0.0)),
            wind_speed_kmh=float(cond.get("wind_speed_kmh", 0.0)),
            wind_direction_deg=float(cond.get("wind_direction_deg", 225.0)),
            release_size=str(cond.get("release_size", "medium")),
        )

    conditions, parse_detail = _parse_conditions(message, base)

    # Run the REAL assessment on the parsed conditions. The model does not compute this.
    result = assess_mod.assess(bt, conditions, simulation_mode="fast")

    summary = _summarize(result)
    narrate_prompt = (
        f"The user asked: \"{message}\"\n\n"
        "The model was run with the conditions below and produced this result. In 2-4 sentences, "
        "describe what changed and what the terrain response is, in plain language. Do not give "
        "travel advice.\n\n" + summary
    )
    reply = _ollama_chat(SYSTEM_FRAMING, narrate_prompt, temperature=0.3)

    return {
        "reply": _with_disclaimer(reply),
        "parsed_conditions": conditions.to_dict(),
        "parse_detail": parse_detail,
        "assessment": result,
        "disclaimer": DISCLAIMER,
        "model": OLLAMA_MODEL,
        "is_operational_forecast": False,
    }
