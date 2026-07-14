"""The v1 API contract: job flow, validation, error shape, and the limits.

The job flow is tested against a *stub* task, not a real analysis. A real one is 90
seconds of numerical work, and a test suite that takes ten minutes is a test suite
nobody runs. The stub exercises the part that can actually break -- 202, poll,
progress, idempotency, result handoff -- while the science itself is covered by the
model tests, which do not need an HTTP server to be wrong.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.middleware import MAX_BODY_BYTES, reset_rate_limits
from app.jobs import tasks
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    reset_rate_limits()
    with TestClient(app) as test_client:
        yield test_client
    reset_rate_limits()


@pytest.fixture()
def stub_task(monkeypatch: pytest.MonkeyPatch):
    """Replace run_analysis with something that finishes in milliseconds."""
    calls: list[dict[str, Any]] = []

    def fake(settings, *, progress=None, **kwargs) -> dict[str, Any]:
        calls.append(kwargs)
        if progress:
            progress(50, "Halfway")
        return {"analysis_id": "AN_STUB_0001", "hazard_score": 42.0, "mode": kwargs.get("mode")}

    monkeypatch.setitem(tasks.TASKS, "run_analysis", fake)
    return calls


# --- Health and readiness -----------------------------------------------------


def test_health_is_cheap_and_says_nothing_about_the_data(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["model_version"]


def test_readiness_reports_what_is_blocking_it(client: TestClient) -> None:
    response = client.get("/api/v1/ready")
    body = response.json()
    assert response.status_code in {200, 503}
    assert body["ready"] is (response.status_code == 200)
    # Whatever blocks readiness must come with the command that fixes it.
    assert len(body["remedy"]) == len(body["blocking"])


def test_every_response_carries_a_correlation_id(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.headers["X-Correlation-ID"]

    # And an id supplied by the caller is honoured, so a trace survives a hop.
    mine = "abc123deadbeef"
    echoed = client.get("/api/v1/health", headers={"X-Correlation-ID": mine})
    assert echoed.headers["X-Correlation-ID"] == mine


# --- The legacy API must keep working -----------------------------------------


def test_legacy_routes_still_respond(client: TestClient) -> None:
    """The current frontend depends on these. v1 is additive, not a replacement."""
    for path in ("/api/health", "/api/catalog?compact=true", "/api/aoi"):
        assert client.get(path).status_code == 200, f"legacy route {path} broke"


# --- Data ---------------------------------------------------------------------


def test_catalog_joins_file_counts_onto_the_health_verdict(client: TestClient) -> None:
    body = client.get("/api/v1/data/catalog").json()

    # The join is by relative path. If it silently matches nothing, the catalogue
    # reports a confident zero for a dataset that is sitting right there on disk.
    assert body["datasets_with_no_matching_files"] == [], (
        "The catalogue/health join matched no files for these datasets. It is joining on the "
        "wrong key."
    )
    assert body["datasets"], "no datasets reported"
    for dataset in body["datasets"]:
        assert "usable_by_model" in dataset, "presence is not usability; say which"


def test_data_health_never_reads_an_empty_file_as_zero(client: TestClient) -> None:
    body = client.get("/api/v1/data/health").json()
    assert "never as an observation of zero" in body["empty_file_policy"]
    assert body["calibration_status"]["is_calibrated"] is False


# --- Model --------------------------------------------------------------------


def test_model_config_is_hashed_and_admits_it_is_uncalibrated(client: TestClient) -> None:
    body = client.get("/api/v1/model/config").json()
    assert len(body["sha256"]) == 64
    assert body["is_calibrated"] is False
    assert "NOT an operational avalanche forecast" in body["disclaimer"]


# --- The job contract ---------------------------------------------------------


def test_long_work_returns_a_job_and_polls_to_a_result(
    client: TestClient, stub_task: list[dict[str, Any]]
) -> None:
    accepted = client.post("/api/v1/analysis", json={"mode": "current"})
    assert accepted.status_code == 202, "long work must not run inside the request"

    job_id = accepted.json()["job_id"]
    assert accepted.json()["poll"] == f"/api/v1/jobs/{job_id}"

    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)

    assert job["state"] == "succeeded", job.get("failure_reason")
    assert job["progress"] == 100
    assert job["result_id"] == "AN_STUB_0001"
    assert stub_task[0]["mode"] == "current"


def test_the_same_idempotency_key_does_not_start_a_second_run(
    client: TestClient, stub_task: list[dict[str, Any]]
) -> None:
    """Two clicks on "Run" must not run two 90-second analyses."""
    body = {"mode": "current", "idempotency_key": "SAME"}
    first = client.post("/api/v1/analysis", json=body).json()
    second = client.post("/api/v1/analysis", json=body).json()

    assert first["job_id"] == second["job_id"]
    assert second["deduplicated"] is True


def test_a_failing_job_reports_why(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(settings, *, progress=None, **kwargs):
        raise ValueError("the terrain is on fire")

    monkeypatch.setitem(tasks.TASKS, "run_analysis", boom)
    job_id = client.post("/api/v1/analysis", json={"mode": "current"}).json()["job_id"]

    for _ in range(100):
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["state"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)

    assert job["state"] == "failed"
    assert "the terrain is on fire" in job["failure_reason"]
    # The traceback belongs in the log, not in the API response.
    assert "Traceback" not in (job["failure_reason"] or "")


# --- Validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "because"),
    [
        ({"mode": "historical"}, "historical replay needs a datetime"),
        ({"mode": "scenario"}, "a scenario needs inputs"),
        (
            {"mode": "scenario", "preset": "wind_loading", "scenario": {"wind_speed_kmh": 10}},
            "scenario and preset are mutually exclusive",
        ),
        ({"mode": "current", "nonsense": 1}, "unknown fields are rejected"),
        ({"mode": "scenario", "scenario": {"wind_speed_kmh": 5000}}, "a 5000 km/h wind is not real"),
        ({"mode": "scenario", "scenario": {"snow_depth_index": 7}}, "the index is 0-1"),
    ],
)
def test_incoherent_requests_are_refused(client: TestClient, body: dict, because: str) -> None:
    """A hazard model will faithfully compute a number from nonsense, and the number
    looks exactly as authoritative as a real one. Refuse it at the door."""
    response = client.post("/api/v1/analysis", json=body)
    assert response.status_code == 422, because
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["detail"]["errors"], "say WHICH field, and why"


def test_an_unknown_preset_lists_the_real_ones(client: TestClient) -> None:
    response = client.post("/api/v1/analysis", json={"mode": "scenario", "preset": "blizzard_of_oz"})
    assert response.status_code == 400
    assert response.json()["error"]["detail"]["available"]


# --- Errors -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/analysis/NOPE",
        "/api/v1/simulations/NOPE",
        "/api/v1/simulations/NOPE/assets",
        "/api/v1/jobs/NOPE",
        "/api/v1/layers/NOPE",
    ],
)
def test_missing_things_are_404_not_500(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "not_found"
    assert body["correlation_id"], "an error must be traceable to its request"


def test_simulating_an_analysis_that_does_not_exist_is_a_404(client: TestClient) -> None:
    response = client.post("/api/v1/simulations", json={"analysis_id": "AN_NOPE"})
    assert response.status_code == 404


# --- Limits -------------------------------------------------------------------


def test_an_oversized_body_is_refused_before_it_is_read(client: TestClient) -> None:
    response = client.post(
        "/api/v1/analysis", json={"mode": "current", "event_id": "x" * (MAX_BODY_BYTES + 1024)}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_the_expensive_route_is_rate_limited(client: TestClient) -> None:
    """A simulation is a minute of numerical work. A handful of impatient clicks
    must fail fast and legibly, not queue enough work to bury the machine."""
    codes = [
        client.post("/api/v1/simulations", json={"analysis_id": "AN_NOPE"}).status_code
        for _ in range(9)
    ]
    assert 429 in codes, "POST /simulations is not rate limited"

    limited = client.post("/api/v1/simulations", json={"analysis_id": "AN_NOPE"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"], "a 429 must say when to come back"
    assert limited.json()["error"]["detail"]["retry_after_seconds"] >= 1
