r"""The split deployment: three apps, one API contract.

Stage 3 can run as one process (``app.main``) or as separate assess and assistant
services. These tests pin the two properties that make that safe:

1. Each service serves **exactly** its own routes -- so a deployment cannot
   accidentally expose the assistant from the assess service, or vice versa, and
   the assistant image genuinely has no reason to carry baked terrain.
2. The assistant reaches the assessment through an injected client, so the
   in-process and over-HTTP paths cannot drift. The scenario path must run the
   *real* assessment either way -- the language model never computes a hazard
   number, which is the safety property the whole split has to preserve.

Hermetic: no sockets, no Ollama, no baked terrain.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import risk
from app.assess_client import (
    AssessUnavailableError,
    HttpAssessClient,
    LocalAssessClient,
    from_environment,
)


# --- each service serves only its own routes ----------------------------------


def _paths(module_name: str) -> set[str]:
    import importlib

    module = importlib.import_module(module_name)
    return set(module.app.openapi()["paths"])


def test_assess_service_serves_terrain_and_assess_but_no_assistant():
    paths = _paths("app.main_assess")
    assert "/api/assess" in paths
    assert "/api/twin/meta" in paths
    assert "/api/twin/tiles/{z}/{x}/{y}.png" in paths
    assert not any(p.startswith("/api/assistant") for p in paths)


def test_assistant_service_serves_only_the_assistant():
    paths = _paths("app.main_assistant")
    assert "/api/assistant/chat" in paths
    assert "/api/assistant/explain" in paths
    # No terrain, no assess: this service holds no baked artifacts and computes no
    # hazard number, so it must not offer a route that implies otherwise.
    assert "/api/assess" not in paths
    assert not any(p.startswith("/api/twin") for p in paths)


def test_combined_app_still_serves_everything():
    """The one-process shape stays intact -- local dev and the launcher rely on it."""
    paths = _paths("app.main")
    for expected in ("/api/assess", "/api/twin/meta", "/api/assistant/chat", "/api/health"):
        assert expected in paths


def test_assistant_service_does_not_import_the_runout_engine():
    """The light half stays light: importing it must not drag in the heavy half.

    ``app/api/__init__.py`` used to import every router eagerly, which quietly made
    this false. Pin it so the import graph cannot regress unnoticed.
    """
    import subprocess
    import sys

    probe = (
        "import sys, app.main_assistant;"
        "bad=[m for m in ('app.assess','app.simulation.runout','app.api.terrain')"
        " if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"assistant service imported: {out.stdout.strip()}"


@pytest.mark.parametrize("entrypoint", ["app.main", "app.main_assess", "app.main_assistant"])
def test_runtime_entrypoints_do_not_import_bake_only_dependencies(entrypoint: str):
    """Every serving shape stays independent of DATA/geospatial bake libraries."""
    import subprocess
    import sys

    banned = (
        "rasterio",
        "pyproj",
        "xdem",
        "osgeo",
        "pandas",
        "geopandas",
        "laspy",
        "cdsapi",
        "eccodes",
        "cfgrib",
        "PIL",
        "matplotlib",
        "yaml",
        "sqlalchemy",
    )
    probe = (
        "import importlib,sys;"
        f"importlib.import_module({entrypoint!r});"
        f"bad=[name for name in {banned!r} if name in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"{entrypoint} imported: {out.stdout.strip()}"


def test_combined_serving_import_is_socket_blocked_and_offline_processing_free():
    """Serving import must neither open a socket nor load M2/M3 offline adapters."""
    import subprocess
    import sys

    forbidden_modules = (
        # Exposure is built once, offline, with pyproj and shapely. The serving
        # app reads the two baked rasters and the static vector and nothing else.
        "app.processing.exposure",
        "app.processing.conditions.eccc",
        "app.processing.conditions.pcic",
        "app.processing.conditions.era5_land",
        "app.processing.snow",
        "app.processing.snow.official_example",
        "app.processing.snow.snowpack_output",
        "cdsapi",
        "eccodes",
        "cfgrib",
    )
    probe = f"""
import fastapi, socket, sys
class BlockedSocket(socket.socket):
    def connect(self, *args, **kwargs):
        raise RuntimeError('socket connect attempted during import')
    def connect_ex(self, *args, **kwargs):
        raise RuntimeError('socket connect_ex attempted during import')
    def bind(self, *args, **kwargs):
        raise RuntimeError('socket bind attempted during import')
def blocked_connection(*args, **kwargs):
    raise RuntimeError('socket create_connection attempted during import')
socket.socket = BlockedSocket
socket.create_connection = blocked_connection
import app.main
bad = [name for name in {forbidden_modules!r} if name in sys.modules]
print(','.join(bad))
"""
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", f"serving import loaded offline modules: {out.stdout.strip()}"


# --- the assess client -------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        return self._payload


def test_http_client_posts_clamped_conditions(monkeypatch: pytest.MonkeyPatch):
    """Out-of-range slider values are clamped before they cross the wire.

    The assess service validates them again, but sending a value we know is out of
    range would turn a recoverable parse into a 422 the user never sees.
    """
    captured: dict[str, Any] = {}

    def _fake_post(url: str, *, json: Any, headers: Any, timeout: float):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200, {"hazard": {"score": 42}})

    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)
    # No metadata server in a test: the client must fall back to unauthenticated.
    monkeypatch.setattr("app.assess_client._id_token", lambda audience: None)

    client = HttpAssessClient("https://assess.example.com/")
    result = client(risk.Conditions(new_snow_cm=999.0, wind_speed_kmh=50.0), simulation_mode="fast")

    assert result == {"hazard": {"score": 42}}
    assert captured["url"] == "https://assess.example.com/api/assess"
    assert captured["json"]["new_snow_cm"] == 300.0  # clamped from 999
    assert captured["json"]["simulation_mode"] == "fast"


def test_http_client_reports_a_failed_assessment_as_failed(monkeypatch: pytest.MonkeyPatch):
    """A dead assess service raises -- it never returns a zero-hazard-looking result.

    Invariant I3: missing is reported as missing. A 503 the user can see beats a
    quietly benign number.
    """
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(500, None, "boom")
    )
    monkeypatch.setattr("app.assess_client._id_token", lambda audience: None)

    with pytest.raises(AssessUnavailableError):
        HttpAssessClient("https://assess.example.com")(risk.Conditions())


def test_http_client_reports_an_unreachable_service_as_failed(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    monkeypatch.setattr("app.assess_client._id_token", lambda audience: None)

    with pytest.raises(AssessUnavailableError):
        HttpAssessClient("https://assess.example.com")(risk.Conditions())


def test_from_environment_picks_remote_when_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AVALANCHE_ASSESS_URL", "https://assess.example.com")
    assert isinstance(from_environment(), HttpAssessClient)


def test_from_environment_picks_local_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AVALANCHE_ASSESS_URL", raising=False)
    assert isinstance(from_environment(baked=object()), LocalAssessClient)


def test_from_environment_refuses_to_guess_with_no_backend(monkeypatch: pytest.MonkeyPatch):
    """No terrain and no remote URL is a misconfiguration, not a silent no-op."""
    monkeypatch.delenv("AVALANCHE_ASSESS_URL", raising=False)
    with pytest.raises(AssessUnavailableError):
        from_environment()


# --- the assistant still runs the real model, through whichever client it holds ---


def test_scenario_narrates_the_injected_assessment(monkeypatch: pytest.MonkeyPatch):
    """The model describes numbers the deterministic assessment produced.

    This is the safety property the split has to keep: swapping an in-process call
    for an HTTP one must not let the language model start inventing hazard numbers.
    """
    from app import assistant

    calls: list[tuple[risk.Conditions, str]] = []

    def _fake_assess(conditions: risk.Conditions, *, simulation_mode: str = "fast"):
        calls.append((conditions, simulation_mode))
        return {
            "hazard": {"score": 61.0, "label": "High"},
            "release_zones": {"features": []},
            "conditions": conditions.to_dict(),
            "disclaimer": "Experimental. Not an avalanche forecast.",
        }

    def _fake_ollama(system: str, user: str, **kwargs: Any) -> str:
        # First call routes the intent; the second narrates the result.
        if "scenario" in user or "classify" in user.lower() or "intent" in user.lower():
            return '{"intent": "scenario", "new_snow_cm": 40, "wind_speed_kmh": 30}'
        return "More snow loads the lee slopes and the modelled release area grows."

    monkeypatch.setattr(assistant, "_ollama_chat", _fake_ollama)

    result = assistant.chat(_fake_assess, "what if 40 cm of new snow and a SW wind?", None)

    assert calls, "the scenario path must run the real assessment"
    assert calls[0][1] == "fast"
    assert result["assessment"]["hazard"]["score"] == 61.0
    # The disclaimer is attached in code, not generated by the model -- assert the
    # real constant so a reworded DISCLAIMER cannot silently stop being enforced.
    from app.core.model_config import DISCLAIMER

    assert DISCLAIMER in result["reply"]


def test_local_client_delegates_to_the_real_assess(monkeypatch: pytest.MonkeyPatch):
    """LocalAssessClient is a thin adapter, not a second implementation."""
    import app.assess as assess_mod

    seen: dict[str, Any] = {}

    def _fake(baked, conditions, *, simulation_mode="fast", seed=None):
        seen["baked"] = baked
        seen["simulation_mode"] = simulation_mode
        return {"ok": True}

    monkeypatch.setattr(assess_mod, "assess", _fake)

    sentinel = object()
    assert LocalAssessClient(sentinel)(risk.Conditions(), simulation_mode="advanced") == {"ok": True}
    assert seen["baked"] is sentinel
    assert seen["simulation_mode"] == "advanced"
