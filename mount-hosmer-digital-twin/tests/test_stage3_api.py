"""The Stage 3 HTTP surface, end to end, against the synthetic baked terrain.

The whole running API is health + terrain + assess + assistant. These tests drive
it through a ``TestClient`` with the runtime pointed at a hermetic synthetic bake --
no rasterio, no ``DATA\\``, no real tiles. The assistant routes need a live Ollama and
are not exercised here (they degrade to 503 by design when it is absent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import risk
from app.simulation import runout
from synthetic_baked import write_synthetic_baked


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose runtime root is a freshly written synthetic bake."""
    write_synthetic_baked(tmp_path)
    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("AVALANCHE_DATA_ROOT", str(tmp_path))

    # Rebuild the cached settings against this temp environment before app.main
    # reads them, so the app serves the synthetic bake and not the real runtime.
    from app.core.settings import get_settings

    get_settings(refresh=True)

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_health_reports_baked(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["baked"] is True


def test_twin_meta_omits_reproject_and_carries_disclaimer(client: TestClient):
    response = client.get("/api/twin/meta")
    assert response.status_code == 200
    meta = response.json()
    # The reprojection lattice is a server-side concern -- the browser never sees it.
    assert "reproject" not in meta
    assert meta["disclaimer"]
    assert meta["tiles"]["url_template"] == "/api/twin/tiles/{z}/{x}/{y}.png"
    assert meta["imagery"]["url_template"] == "/api/twin/imagery/{z}/{x}/{y}.png"
    assert meta["imagery"]["visual_context_only"] is True


def test_assess_returns_hazard_and_is_not_operational(client: TestClient):
    response = client.post(
        "/api/assess",
        json={"new_snow_cm": 50, "wind_speed_kmh": 60, "wind_direction_deg": 225},
    )
    assert response.status_code == 200
    body = response.json()
    assert "hazard_score" in body
    assert body["is_operational_forecast"] is False
    assert body["is_probability"] is False
    assert body["disclaimer"]


def test_assess_rejects_unknown_release_size(client: TestClient):
    response = client.post("/api/assess", json={"release_size": "huge"})
    assert response.status_code in (400, 422)


def test_assess_rejects_unknown_simulation_mode(client: TestClient):
    response = client.post("/api/assess", json={"simulation_mode": "turbo"})
    assert response.status_code in (400, 422)


def test_openapi_advertises_the_valid_enum_values(client: TestClient):
    """The schema must name the accepted values, not just say "string".

    These were plain ``str`` fields validated by hand inside the handler, so the
    generated schema told a client nothing about what it could send. Typed as
    ``Literal`` they are self-documenting -- pin that so nobody widens them back.
    """
    schema = client.get("/openapi.json").json()
    request_model = schema["components"]["schemas"]["AssessRequest"]["properties"]
    assert set(request_model["release_size"]["enum"]) == set(risk.RELEASE_SIZES)
    assert set(request_model["simulation_mode"]["enum"]) == set(runout.ENGINES)


def test_chat_rejects_an_unbounded_message(client: TestClient):
    """``message`` is the field that reaches the model's prompt, so it is bounded.

    The 4 MB body ceiling sizes the *assessment* payload; without a limit here a
    caller could put megabytes of prose in front of the language model.
    """
    assert client.post("/api/assistant/chat", json={"message": "x" * 4001}).status_code == 422
    assert client.post("/api/assistant/chat", json={"message": ""}).status_code == 422


def test_an_internal_keyerror_is_a_500_not_a_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Our bug must look like our bug.

    A blanket ``KeyError -> 404`` handler used to turn any internal dict-key mistake
    into a "not found" that blamed the caller and logged no traceback. Nothing
    user-supplied can reach a KeyError any more (every ``bt.layer()`` call passes a
    literal; release size and mode are validated by the request model), so the
    handler only ever disguised our own faults. It is gone.
    """
    from app import assess as assess_mod

    def _boom(*args, **kwargs):
        raise KeyError("some_internal_layer")

    monkeypatch.setattr(assess_mod, "assess", _boom)

    response = client.post("/api/assess", json={"new_snow_cm": 10})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


def test_tile_outside_aoi_is_404(client: TestClient):
    # The synthetic bake writes no tiles; MapLibre treats a 404 as an empty tile.
    response = client.get("/api/twin/tiles/13/0/0.png")
    assert response.status_code == 404
    assert client.get("/api/twin/imagery/13/0/0.png").status_code == 404


def test_chat_scenario_runs_the_real_model(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """A scenario routes through the REAL assessment on the synthetic terrain and returns it."""
    from app import assistant

    def _fake(system, user, *, temperature=0.2, force_json=False):
        # Router call -> scenario + sliders; narration call -> prose.
        if force_json:
            return json.dumps(
                {
                    "intent": "scenario",
                    "new_snow_cm": 50,
                    "wind_speed_kmh": 40,
                    "wind_direction_deg": 225,
                    "release_size": "medium",
                }
            )
        return "The added snow raised the modelled hazard."

    monkeypatch.setattr(assistant, "_ollama_chat", _fake)

    response = client.post(
        "/api/assistant/chat",
        json={"message": "what if 50 cm of new snow and a SW wind?", "history": []},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "scenario"
    assert body["assessment"] is not None  # the deterministic model actually ran
    assert body["parsed_conditions"]["new_snow_cm"] == 50
    assert body["disclaimer"]


def test_chat_declines_advice_via_the_route(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The advice refusal is reached through the HTTP route, model untouched."""
    from app import assistant

    def _no_model(*args, **kwargs):
        raise AssertionError("the model must not be called for an advice request")

    monkeypatch.setattr(assistant, "_ollama_chat", _no_model)

    response = client.post("/api/assistant/chat", json={"message": "is it safe to ski RZ001?"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "advice"
    assert body["assessment"] is None


def test_body_size_limit_is_path_aware(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The 256 KB cap holds everywhere EXCEPT the assistant endpoints, which legitimately
    receive a whole ~1 MB assessment (its release-zone/runout GeoJSON) to summarize."""
    from app import assistant

    def _no_ollama(*args, **kwargs):  # keep the test hermetic -- no live model needed
        raise assistant.AssistantError("no local model in tests")

    monkeypatch.setattr(assistant, "_ollama_chat", _no_ollama)

    # A normal endpoint keeps the tight 256 KB limit: an oversized body is refused
    # by the middleware before the handler (and before pydantic) ever runs.
    over_256kb = client.post("/api/assess", json={"new_snow_cm": 50, "_pad": "x" * (300 * 1024)})
    assert over_256kb.status_code == 413

    # The assistant path admits a >256 KB body: it gets past the size gate and only
    # then 503s because the model is stubbed out here -- crucially, NOT a 413.
    one_mb = client.post("/api/assistant/explain", json={"assessment": {"_pad": "y" * (1024 * 1024)}})
    assert one_mb.status_code == 503

    # But the assistant allowance is bounded, not unlimited: over 4 MB is still refused.
    over_4mb = client.post("/api/assistant/explain", json={"assessment": {"_pad": "z" * (5 * 1024 * 1024)}})
    assert over_4mb.status_code == 413
