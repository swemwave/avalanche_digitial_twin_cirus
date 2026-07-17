"""The Stage 3 HTTP surface, end to end, against the synthetic baked terrain.

The whole running API is health + terrain + assess + assistant. These tests drive
it through a ``TestClient`` with the runtime pointed at a hermetic synthetic bake --
no rasterio, no ``DATA\\``, no real tiles. The assistant routes need a live Ollama and
are not exercised here (they degrade to 503 by design when it is absent).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


def test_tile_outside_aoi_is_404(client: TestClient):
    # The synthetic bake writes no tiles; MapLibre treats a 404 as an empty tile.
    response = client.get("/api/twin/tiles/13/0/0.png")
    assert response.status_code == 404


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
