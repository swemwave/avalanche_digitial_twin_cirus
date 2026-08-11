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
    assert body["bake_schema"] == "stage3-baked-v2"
    assert len(body["bake_sha256"]) == 64


def test_twin_meta_omits_reproject_and_carries_disclaimer(client: TestClient):
    response = client.get("/api/twin/meta")
    assert response.status_code == 200
    meta = response.json()
    # The reprojection lattice is a server-side concern -- the browser never sees it.
    assert "reproject" not in meta
    assert meta["disclaimer"]
    assert len(meta["identity"]["bake_sha256"]) == 64
    assert meta["terrain"]["source_codes"]
    assert meta["forest"]["source_codes"]
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
    assert body["schema_version"] == 3
    assert body["release_potential_index"] == body["hazard_score"]
    assert body["release_potential_band"] == body["risk_level"]
    assert "hazard_score" in body
    assert body["random_seed"] is None  # fast routing has no random generator
    assert body["is_operational_forecast"] is False
    assert body["is_probability"] is False
    assert body["disclaimer"]
    assert body["model"]["bake_sha256"]
    assert body["coverage"]["release_model"]["valid_fraction"] == 1.0
    assert body["coverage"]["runout_model"]["required_layers"] == ["elevation"]
    assert body["provenance"]["conditions"]["is_measurement"] is False
    assert body["scenario"]["classification"] == "hypothetical"
    assert body["scenario"]["conditions_used"] is True
    assert len(body["scenario"]["reproducibility"]["scenario_sha256"]) == 64
    assert len(body["scenario"]["reproducibility"]["numerical_replay_sha256"]) == 64
    assert body["provenance"]["imagery"]["used_in_release_or_runout"] is False
    release_sources = body["provenance"]["footprint_source_mix"]["release_zones"]
    assert release_sources["terrain"]["footprint_cell_count"] > 0
    assert release_sources["terrain"]["missing_source_cell_count"] == 0
    assert release_sources["terrain"]["fraction_of_footprint_by_source_label"] == {
        "Synthetic LiDAR": 1.0
    }
    assert body["validation"]["field_validation"]["status"] == "unavailable"
    assert body["validation"]["field_validation"]["eligible_observation_count"] == 0
    assert body["validation"]["software_verification"]["status"] == "characterized_benchmarks"
    contract = body["validation"]["validation_data_contract"]
    assert contract["canonical_geometry_rasterization"] is True
    assert contract["prediction_identity_required"] is True
    assert contract["code_reviewed_dataset_registry_required"] is True
    assert contract["trusted_dataset_count"] == 0
    assert contract["end_to_end_field_validation_ready"] is False
    assert body["uncertainty"]["runout"]["is_confidence_interval"] is False
    assert body["runout"]["uncertainty_is_confidence_interval"] is False
    assert "including the central footprint" in body["runout"]["uncertainty_area_definition"]


def test_twin_meta_publishes_the_exposure_layer_with_its_attribution(client: TestClient):
    meta = client.get("/api/twin/meta").json()
    exposure = meta["exposure"]

    assert exposure["url"] == "/api/twin/exposure"
    assert exposure["attribution"] == "© OpenStreetMap contributors"
    assert exposure["licence"] == "Open Database License (ODbL) 1.0"
    assert exposure["used_in_release_model"] is False
    assert exposure["used_in_runout_model"] is False
    assert exposure["limitation"]
    assert exposure["classes"][0]["label"] == "Trunk highway"


def test_twin_exposure_serves_the_baked_vector_unchanged(client: TestClient, tmp_path: Path):
    response = client.get("/api/twin/exposure")
    assert response.status_code == 200

    body = response.json()
    assert body["type"] == "FeatureCollection"
    assert body["attribution"] == "© OpenStreetMap contributors"
    assert body["licence"] == "Open Database License (ODbL) 1.0"
    assert body["derived_classes"] == ["inferred_settlement"]
    assert body["features"][0]["properties"]["exposure_class"] == "highway_major"
    # It is the file the bake wrote, byte for byte -- no computation in the route.
    on_disk = (tmp_path / "baked" / "exposure" / "features.geojson").read_bytes()
    assert response.content == on_disk


def test_twin_exposure_is_404_when_the_bake_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    write_synthetic_baked(tmp_path, exposure=False)
    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("AVALANCHE_DATA_ROOT", str(tmp_path))
    from app.core.settings import get_settings

    get_settings(refresh=True)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as bare:
        assert bare.get("/api/twin/exposure").status_code == 404
        assert bare.get("/api/twin/meta").json().get("exposure") is None


def test_assess_publishes_the_composite_index_and_its_components(client: TestClient):
    body = client.post(
        "/api/assess",
        json={"new_snow_cm": 50, "wind_speed_kmh": 60, "wind_direction_deg": 225},
    ).json()

    assert 0 <= body["area_hazard_index"] <= 100
    assert body["area_hazard_band"] and body["area_hazard_color"]
    assert body["peak_zone_id"] and body["peak_zone_index"] >= body["area_hazard_index"]
    assert body["peak_zone_basis"] in {
        "release_reach_exposure",
        "release_and_reach",
        "release_and_exposure",
        "release_only",
    }
    # The composite is a separate quantity from the release-side index.
    assert body["no_zone_release_percentile_index"] is None
    assert body["hazard_components"]["aggregation"] == "area_weighted_mean_of_zone_index"
    assert body["hazard_components"]["is_probability"] is False
    assert len(body["hazard_components"]["band_thresholds"]) == 5

    zone = body["zones"][0]
    assert 0 <= zone["hazard_index"] <= 100
    assert zone["hazard_color"].startswith("#")
    components = zone["hazard_components"]
    assert set(components["components_available"]) == {"release", "reach", "exposure"}
    assert components["exposure_uplift_points"] >= 0
    assert zone["hazard_index"] >= components["terrain_and_reach_index"]


def test_assess_rejects_unknown_release_size(client: TestClient):
    response = client.post("/api/assess", json={"release_size": "huge"})
    assert response.status_code in (400, 422)


def test_assess_unknown_loading_is_terrain_only_not_zero(client: TestClient):
    response = client.post("/api/assess", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["scenario"]["classification"] == "terrain_only"
    assert body["release_potential_index"] is None
    assert body["hazard_score"] is None
    assert body["runout"]["status"] == "unavailable_missing_inputs"
    assert body["runout"]["core_area_m2"] is None
    assert body["disclaimer"]


def test_structured_observation_scenario_round_trips_provenance(client: TestClient):
    observed_at = "2026-02-01T18:00:00Z"
    measured = {
        "status": "measured",
        "observed_at_utc": observed_at,
        "source": {"name": "Field notebook A", "kind": "measurement"},
        "uncertainty": {"kind": "quantified", "value": 2, "unit": "cm", "basis": "Probe spread"},
        "spatial_scope": {"kind": "whole_area"},
    }
    inputs = [
        {
            "input_id": "snow-observation",
            "category": "weather_loading",
            "parameter": "new_snow_depth",
            "value": 50,
            "unit": "cm",
            **measured,
        },
        {
            "input_id": "wind-observation",
            "category": "weather_loading",
            "parameter": "wind_speed",
            "value": 60,
            "unit": "km/h",
            **{
                **measured,
                "uncertainty": {"kind": "unknown", "basis": "Not characterized"},
            },
        },
        {
            "input_id": "direction-observation",
            "category": "weather_loading",
            "parameter": "wind_direction",
            "value": 225,
            "unit": "degree_true",
            **{
                **measured,
                "uncertainty": {"kind": "unknown", "basis": "Not characterized"},
            },
        },
        {
            "input_id": "release-assumption",
            "category": "release_assumptions",
            "parameter": "release_size",
            "value": "medium",
            "unit": "category",
            "status": "assumed",
            "source": {"name": "Research sensitivity choice", "kind": "user_assumption"},
            "uncertainty": {"kind": "not_provided", "basis": "Categorical assumption"},
            "spatial_scope": {"kind": "whole_area"},
        },
        {
            "input_id": "weak-layer-context",
            "category": "snowpack_weak_layers",
            "parameter": "weak_layer_type",
            "value": "surface_hoar",
            "unit": "category",
            "status": "measured",
            "observed_at_utc": observed_at,
            "source": {"name": "Field notebook A", "kind": "measurement"},
            "uncertainty": {"kind": "qualitative", "basis": "Visual grain identification"},
            "spatial_scope": {"kind": "whole_area"},
        },
    ]
    response = client.post(
        "/api/assess",
        json={
            "scenario": {
                "schema_version": "mount-hosmer-observation-scenario-v1",
                "mode": "advanced",
                "valid_at_utc": observed_at,
                "inputs": inputs,
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scenario"]["classification"] == "fully_specified_research_scenario"
    assert body["provenance"]["conditions"]["is_measurement"] is True
    assert body["scenario"]["inputs"][0]["observed_at_utc"] == observed_at
    assert body["scenario"]["unsupported_inputs"][0]["parameter"] == "weak_layer_type"
    assert body["scenario"]["unsupported_inputs"][0]["used_in_computation"] is False
    assert body["release_potential_index"] == 68.8
    assert body["disclaimer"]


def test_assess_rejects_mixed_legacy_and_structured_inputs(client: TestClient):
    response = client.post(
        "/api/assess",
        json={
            "new_snow_cm": 20,
            "scenario": {
                "mode": "advanced",
                "inputs": [],
            },
        },
    )
    assert response.status_code == 422


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
