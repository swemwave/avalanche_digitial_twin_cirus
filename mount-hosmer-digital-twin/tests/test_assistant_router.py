"""The assistant's intent router and its safety rails.

:func:`app.assistant.chat` routes a message to one of four behaviours -- scenario /
question / chat / advice. These tests are hermetic: they stub ``_ollama_chat`` so no
live model is needed, and they never touch baked terrain (only the *scenario* path
runs the real assessment, and that is exercised through the API suite instead).
"""

from __future__ import annotations

import pytest

from app import assistant
from app import risk


# --- deterministic advice guard (a safety path -- never trusts the model) ------


@pytest.mark.parametrize(
    "message",
    [
        "is it safe to ski RZ001?",
        "should I ride the NE bowl today?",
        "can I ski this slope?",
        "would you go into that zone?",
        "is it a go / no-go?",
        "do you recommend skiing here?",
    ],
)
def test_advice_is_detected(message: str):
    assert assistant._is_advice(message) is True


@pytest.mark.parametrize("message", ["what if 40 cm snow?", "what does aspect mean?", "hi"])
def test_non_advice_is_not_flagged(message: str):
    assert assistant._is_advice(message) is False


def test_chat_declines_advice_without_calling_the_model(monkeypatch: pytest.MonkeyPatch):
    """An advice request is refused deterministically -- the model is never consulted."""

    def _boom(*args, **kwargs):
        raise AssertionError("the model must not be called for an advice request")

    monkeypatch.setattr(assistant, "_ollama_chat", _boom)

    result = assistant.chat(None, "is it safe to ski RZ001?", None)  # type: ignore[arg-type]

    assert result["kind"] == "advice"
    assert result["assessment"] is None
    assert result["parsed_conditions"] is None
    assert "not an avalanche forecast" in result["reply"].lower()


# --- routing to conversation (question / chat) ---------------------------------


def _stub(router_json: str, answer_text: str):
    """A fake ``_ollama_chat``: JSON for the router call, prose for the answer call."""

    def _fake(system: str, user: str, *, temperature: float = 0.2, force_json: bool = False) -> str:
        return router_json if force_json else answer_text

    return _fake


def test_a_question_is_answered_conversationally(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        assistant,
        "_ollama_chat",
        _stub('{"intent": "question"}', "Aspect is the compass direction a slope faces."),
    )

    result = assistant.chat(None, "what does aspect mean?", None)  # type: ignore[arg-type]

    assert result["kind"] == "answer"
    assert result["assessment"] is None
    assert "aspect" in result["reply"].lower()


def test_a_greeting_is_chat_not_a_bogus_assessment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        assistant,
        "_ollama_chat",
        _stub('{"intent": "chat"}', "Hi! I can run avalanche what-ifs or answer questions."),
    )

    result = assistant.chat(None, "hey there", None)  # type: ignore[arg-type]

    assert result["kind"] == "chat"
    assert result["assessment"] is None


# --- the router's fallback when the model returns unusable JSON ----------------


def test_route_falls_back_to_heuristic_on_bad_json(monkeypatch: pytest.MonkeyPatch):
    """If the router JSON can't be parsed, a scenario-looking message still routes to scenario."""
    monkeypatch.setattr(assistant, "_ollama_chat", lambda *a, **k: "this is not json")

    intent, _conditions, _detail = assistant._route("a strong SW wind", risk.Conditions(), None)
    assert intent == "scenario"

    intent, _c, _d = assistant._route("tell me about the model", risk.Conditions(), None)
    assert intent == "question"


def test_terrain_only_summary_never_invents_zero_conditions():
    summary = assistant._summarize(
        {
            "conditions": None,
            "release_potential_index": None,
            "release_potential_band": None,
            "release_zones": {"zone_count": 0},
            "runout": {"core_area_m2": None},
            "scenario": {"classification": "terrain_only"},
            "coverage": {"release_model": {"valid_fraction": 0}},
            "validation": {
                "field_validation": {"status": "unavailable", "eligible_observation_count": 0}
            },
        }
    )

    assert "incomplete/unknown" in summary
    assert "were replaced with zero" in summary
    assert "index: unavailable" in summary
    assert "0 cm new snow" not in summary
