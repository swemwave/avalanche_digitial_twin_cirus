r"""Coverage for the bakeworker HTTP-dispatch branch.

``tests/test_uploaded_mountains.py`` owns the rejection corpus and the
local-subprocess path (``AVALANCHE_BAKE_WORKER_URL`` unset). This file is
specifically about the other branch: ``app.main_bakeworker``'s own routes, and
``app.mountain_jobs``'s ``_upload_available_http`` / ``_run_probe_http`` /
``_run_bake_http`` helpers that dispatch to it.

Three layers, cheapest first:

1. ``TestClient(app.main_bakeworker.app)`` directly -- fast, in-process, no
   real network, exercises the service's own routes.
2. A REAL ``uvicorn app.main_bakeworker:app`` subprocess bound to
   ``127.0.0.1:<free port>``, driven through ``app.mountain_jobs``'s HTTP
   helpers -- real HTTP between two real processes, the same wire protocol
   production uses, just without Docker (unavailable in this environment).
3. One end-to-end test that is the process-level equivalent of "assess
   container + bakeworker container + shared EFS volume": a real bakeworker
   subprocess, plus a real ``TestClient(app.main:app)`` (the combined
   local-dev app that already serves the full ``/api/mountains`` pipeline)
   pointed at it via ``AVALANCHE_BAKE_WORKER_URL``, both resolving
   ``AVALANCHE_RUNTIME_ROOT`` to the same directory on disk -- which is
   functionally what two containers sharing an EFS mount gives them.

Like ``test_uploaded_mountains.py``, everything here needs the offline
geospatial stack and skips cleanly where it is absent.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pytest

from app.core.settings import Settings

rasterio = pytest.importorskip(
    "rasterio", reason="bake-only dependency; see backend/requirements-bake.txt"
)
from rasterio.transform import from_origin  # noqa: E402

import app.mountain_jobs as mountain_jobs  # noqa: E402
from app.mountains import mountain_root, source_root  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"

# Colorado UTM 13N, same synthetic place as test_uploaded_mountains.py.
CRS = "EPSG:32613"
WEST, NORTH = 456000.0, 4402000.0


# --- Synthetic raster fixtures (smaller siblings of test_uploaded_mountains.py's) --


def _write(path: Path, values: np.ndarray, **overrides: Any) -> Path:
    options = {
        "driver": "GTiff",
        "width": values.shape[1],
        "height": values.shape[0],
        "count": 1,
        "dtype": str(values.dtype),
        "crs": CRS,
        "transform": from_origin(WEST, NORTH, 10.0, 10.0),
        "nodata": -9999.0,
        "compress": "deflate",
    }
    options.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **options) as dataset:
        dataset.write(values, 1)
    return path


def _dem(size: int) -> np.ndarray:
    """A ridge with real relief and plausible slopes -- see _mountain_dem's twin
    in test_uploaded_mountains.py."""
    y, x = np.mgrid[0:size, 0:size].astype("float32")
    return (2600.0 + 700.0 * np.exp(-(((x - size / 2) / (size / 4)) ** 2)) - y * 1.5).astype("float32")


def _landcover(size: int) -> np.ndarray:
    classes = np.full((size, size), 60, dtype="uint8")
    classes[size * 2 // 3 :, :] = 10  # ESA WorldCover tree cover
    return classes


def _seed_mountain(settings: Settings, mountain_id: str, *, with_landcover: bool = True, size: int = 80) -> Path:
    root = source_root(settings, mountain_id)
    _write(root / "static" / "dem.tif", _dem(size))
    if with_landcover:
        _write(root / "static" / "landcover.tif", _landcover(size), dtype="uint8", nodata=0)
    return root


def _seed_bad_mountain(settings: Settings, mountain_id: str) -> Path:
    """A renamed PNG as ``.tif`` -- the cheapest reliable rejection fixture,
    mirrors test_uploaded_mountains.py::test_a_renamed_png_is_rejected."""
    fake = source_root(settings, mountain_id) / "static" / "dem.tif"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
    return fake


def _probe_body(mountain_id: str) -> dict[str, Any]:
    return {
        "mountain_id": mountain_id,
        "name": mountain_id,
        "provider": "Synthetic fixture",
        "citation": "Generated for bakeworker dispatch verification",
        "licence": "CC0-1.0",
        "vertical_units_affirmed": True,
        "resolution_m": None,
    }


def _settings_for(runtime_root: Path) -> Settings:
    return Settings(runtime_root=runtime_root, data_root=runtime_root.parent / "data")


# --- Real bakeworker subprocess plumbing ---------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_healthy(base_url: str, process: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            raise RuntimeError(
                f"bakeworker subprocess exited early (code {process.returncode}) "
                f"before becoming healthy:\n{detail[-2000:]}"
            )
        try:
            response = httpx.get(f"{base_url}/health", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_exc = exc
        time.sleep(0.25)
    detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    raise TimeoutError(
        f"bakeworker never became healthy at {base_url} within {timeout:g}s "
        f"(last error: {last_exc}):\n{detail[-2000:]}"
    )


def _start_bakeworker(runtime_root: Path, log_path: Path) -> tuple[subprocess.Popen, str, Any]:
    """Launch a real ``uvicorn app.main_bakeworker:app`` subprocess and wait for
    it to report healthy. Returns ``(process, base_url, log_file)``; the caller
    must ``_terminate(process, log_file)`` it, even on a failing test."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = dict(os.environ)
    env.pop("AVALANCHE_BAKE_WORKER_URL", None)  # this process must take the local path
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(BACKEND_ROOT), existing_pythonpath) if p)
    env["AVALANCHE_RUNTIME_ROOT"] = str(runtime_root)

    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main_bakeworker:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(base_url, process, log_path)
    except Exception:
        _terminate(process, log_file)
        raise
    return process, base_url, log_file


def _terminate(process: subprocess.Popen, log_file: Any = None) -> None:
    """Never leak a listening bakeworker process, even when a test fails."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    if log_file is not None:
        log_file.close()


def _poll_bake(get_fn, mountain_id: str, *, timeout: float = 60.0, interval: float = 0.3) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = get_fn(f"/bake/{mountain_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in ("ready", "failed"):
            return payload
        time.sleep(interval)
    raise TimeoutError(f"bake for {mountain_id!r} did not finish within {timeout:g}s: last={payload}")


@pytest.fixture(scope="module")
def bakeworker(tmp_path_factory: pytest.TempPathFactory):
    """One real bakeworker subprocess shared by the tests in this module that
    drive it through app.mountain_jobs's HTTP helpers. Distinct mountain ids
    per test keep them from colliding on the shared runtime root or the
    service's single-flight bake lock."""
    runtime_root = tmp_path_factory.mktemp("bakeworker-runtime")
    log_path = tmp_path_factory.mktemp("bakeworker-logs") / "bakeworker.log"
    process, base_url, log_file = _start_bakeworker(runtime_root, log_path)
    try:
        yield SimpleNamespace(process=process, base_url=base_url, runtime_root=runtime_root)
    finally:
        _terminate(process, log_file)


# =================================================================================
# 1. app.main_bakeworker's own routes, in-process via TestClient
# =================================================================================


def test_health_endpoint_reports_rasterio_importable() -> None:
    from fastapi.testclient import TestClient

    from app.main_bakeworker import app as bakeworker_app

    with TestClient(bakeworker_app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["rasterio_importable"] is True


def test_bake_status_for_an_unknown_mountain_is_404() -> None:
    from fastapi.testclient import TestClient

    from app.main_bakeworker import app as bakeworker_app

    with TestClient(bakeworker_app) as client:
        response = client.get("/bake/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "bake_unknown"


def test_probe_endpoint_returns_ok_false_for_a_rejected_raster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main_bakeworker import app as bakeworker_app

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(runtime_root))
    settings = _settings_for(runtime_root)
    _seed_bad_mountain(settings, "renamed-png")

    with TestClient(bakeworker_app) as client:
        response = client.post("/probe", json=_probe_body("renamed-png"))

    # ProbeRejection is a validation outcome, not a transport failure: always 200.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "not_a_raster"


def test_a_second_bake_while_one_is_running_on_the_same_client_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main_bakeworker import app as bakeworker_app

    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(runtime_root))
    settings = _settings_for(runtime_root)
    for mountain_id in ("busy-a", "busy-b"):
        _seed_mountain(settings, mountain_id, size=48)

    with TestClient(bakeworker_app) as client:
        for mountain_id in ("busy-a", "busy-b"):
            probed = client.post("/probe", json=_probe_body(mountain_id))
            assert probed.status_code == 200 and probed.json()["ok"], probed.text

        started = client.post("/bake/busy-a")
        assert started.status_code == 202, started.text

        second = client.post("/bake/busy-b")
        assert second.status_code == 409
        assert second.json()["code"] == "bake_busy"

        # Drain busy-a's real background bake so no subprocess is left running
        # past this test.
        final = _poll_bake(client.get, "busy-a", timeout=60.0)
        assert final["status"] == "ready", final


# =================================================================================
# 2. app.mountain_jobs's HTTP-dispatch helpers, against a REAL bakeworker process
# =================================================================================


def test_upload_available_returns_true_while_the_bakeworker_is_up(bakeworker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", bakeworker.base_url)
    settings = _settings_for(bakeworker.runtime_root)

    available, reason = mountain_jobs.upload_available(settings)

    assert available is True, reason


def test_run_probe_over_http_validates_a_real_dem(bakeworker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", bakeworker.base_url)
    settings = _settings_for(bakeworker.runtime_root)
    _seed_mountain(settings, "http-probe-ok", with_landcover=True)

    outcome = mountain_jobs.run_probe(
        settings, mountain_id="http-probe-ok", name="HTTP Probe OK", provider="p",
        citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
    )

    assert outcome.ok is True, outcome.message
    assert outcome.payload["assessable"] is True
    assert Path(outcome.payload["pack_path"]).is_file()


def test_run_probe_over_http_surfaces_the_validators_own_rejection(
    bakeworker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", bakeworker.base_url)
    settings = _settings_for(bakeworker.runtime_root)
    _seed_bad_mountain(settings, "http-probe-bad")

    outcome = mountain_jobs.run_probe(
        settings, mountain_id="http-probe-bad", name="HTTP Probe Bad", provider="p",
        citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
    )

    # The validator's own rejection, round-tripped over HTTP -- not a transport
    # error (which would show up as probe_launch_failed / probe_timeout instead).
    assert outcome.ok is False
    assert outcome.code == "not_a_raster"
    assert "could not be opened as a raster" in outcome.message


def test_run_bake_over_http_produces_a_real_baked_mountain(bakeworker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", bakeworker.base_url)
    settings = _settings_for(bakeworker.runtime_root)
    mountain_id = "http-bake-ok"
    _seed_mountain(settings, mountain_id, with_landcover=True)

    probed = mountain_jobs.run_probe(
        settings, mountain_id=mountain_id, name="HTTP Bake OK", provider="p",
        citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
    )
    assert probed.ok, probed.message

    stages: list[str] = []
    result = mountain_jobs.run_bake(settings, mountain_id=mountain_id, on_stage=stages.append)

    assert set(result) == {"stage", "bake_sha256", "warnings"}
    assert len(result["bake_sha256"]) == 64
    assert stages, "on_stage should have observed at least one real stage label"

    meta_path = mountain_root(settings, mountain_id) / "baked" / "meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta.get("identity", {}).get("bake_sha256") == result["bake_sha256"]


def test_run_bake_over_http_raises_bakebusy_on_a_concurrent_call(
    bakeworker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", bakeworker.base_url)
    settings = _settings_for(bakeworker.runtime_root)
    for mountain_id in ("busy-http-a", "busy-http-b"):
        _seed_mountain(settings, mountain_id)
        outcome = mountain_jobs.run_probe(
            settings, mountain_id=mountain_id, name=mountain_id, provider="p",
            citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
        )
        assert outcome.ok, outcome.message

    result_box: dict[str, Any] = {}

    def _bake_a() -> None:
        result_box["a"] = mountain_jobs.run_bake(settings, mountain_id="busy-http-a")

    thread = threading.Thread(target=_bake_a, daemon=True)
    thread.start()
    try:
        # start_bake sets the busy flag synchronously before it answers the POST,
        # so as soon as GET /bake/busy-http-a stops 404ing, a second POST is
        # guaranteed to see it. Capped wait, not a fixed sleep.
        deadline = time.monotonic() + 10.0
        registered = False
        while time.monotonic() < deadline:
            probe_response = httpx.get(f"{bakeworker.base_url}/bake/busy-http-a", timeout=2.0)
            if probe_response.status_code == 200:
                registered = True
                break
            time.sleep(0.05)
        assert registered, "busy-http-a's bake never registered on the bakeworker"

        with pytest.raises(mountain_jobs.BakeBusyError):
            mountain_jobs.run_bake(settings, mountain_id="busy-http-b")
    finally:
        thread.join(timeout=60.0)

    assert not thread.is_alive(), "busy-http-a's bake thread did not finish in time"
    assert "a" in result_box, "busy-http-a's run_bake never returned a result"
    assert len(result_box["a"]["bake_sha256"]) == 64


def test_upload_available_fails_fast_when_the_bakeworker_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_port = _free_port()  # nothing is listening here
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", f"http://127.0.0.1:{dead_port}")
    settings = _settings_for(tmp_path / "runtime")

    started = time.monotonic()
    available, reason = mountain_jobs.upload_available(settings)
    elapsed = time.monotonic() - started

    assert available is False, reason
    # The health check uses a 5s timeout; a dead port refuses the connection
    # near-instantly, so this must not be anywhere close to the 60s a hung local
    # subprocess health check would take.
    assert elapsed < 10.0, f"upload_available took {elapsed:.1f}s against a dead port"


def test_run_probe_and_run_bake_raise_mountain_job_error_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_port = _free_port()
    monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", f"http://127.0.0.1:{dead_port}")
    settings = _settings_for(tmp_path / "runtime")

    with pytest.raises(mountain_jobs.MountainJobError) as probe_exc:
        mountain_jobs.run_probe(
            settings, mountain_id="unreachable", name="n", provider="p",
            citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
        )
    assert probe_exc.value.code == "probe_launch_failed"

    with pytest.raises(mountain_jobs.MountainJobError) as bake_exc:
        mountain_jobs.run_bake(settings, mountain_id="unreachable")
    assert bake_exc.value.code == "bake_launch_failed"


# =================================================================================
# 3. The "byte-for-byte identical when unset" contract, stated directly
# =================================================================================


def test_bake_worker_base_url_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AVALANCHE_BAKE_WORKER_URL", raising=False)
    assert mountain_jobs._bake_worker_base_url() is None


def test_local_subprocess_path_is_unchanged_when_the_env_var_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quick, direct smoke call through the ORIGINAL branch, so this contract
    does not rest entirely on inference from test_uploaded_mountains.py passing."""
    monkeypatch.delenv("AVALANCHE_BAKE_WORKER_URL", raising=False)
    settings = _settings_for(tmp_path / "runtime")
    _seed_mountain(settings, "local-smoke", with_landcover=True)

    outcome = mountain_jobs.run_probe(
        settings, mountain_id="local-smoke", name="Local Smoke", provider="p",
        citation="c", licence="l", vertical_units_affirmed=True, resolution_m=None,
    )

    assert outcome.ok is True, outcome.message
    assert outcome.payload["assessable"] is True


# =================================================================================
# 4. Full integration: assess-equivalent + bakeworker over real HTTP, shared disk
# =================================================================================


def test_uploading_through_the_combined_app_dispatches_to_a_real_bakeworker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process-level stand-in for 'assess container + bakeworker container +
    shared EFS volume'. Docker is unavailable in this environment, but the code
    path is identical either way: a real bakeworker subprocess, a real
    TestClient(app.main:app) with AVALANCHE_BAKE_WORKER_URL pointed at it, and
    both resolving AVALANCHE_RUNTIME_ROOT to the SAME directory on disk -- which
    is functionally what an EFS bind-mount gives two containers. Only container
    isolation is missing, not behaviour.
    """
    from fastapi.testclient import TestClient

    shared_runtime = tmp_path / "runtime"
    log_path = tmp_path / "bakeworker.log"
    process, base_url, log_file = _start_bakeworker(shared_runtime, log_path)
    try:
        monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(shared_runtime))
        monkeypatch.setenv("AVALANCHE_BAKE_WORKER_URL", base_url)
        monkeypatch.delenv("AVALANCHE_ASSESS_URL", raising=False)
        from app.core import settings as settings_module

        settings_module._cached = None
        from app.main import app as combined_app

        with TestClient(combined_app) as client:
            dem = _write(tmp_path / "upload" / "dem.tif", _dem(120))
            landcover = _write(tmp_path / "upload" / "landcover.tif", _landcover(120), dtype="uint8", nodata=0)
            files = {
                "dem": ("dem.tif", dem.read_bytes(), "image/tiff"),
                "landcover": ("landcover.tif", landcover.read_bytes(), "image/tiff"),
            }

            start = time.monotonic()
            response = client.post(
                "/api/mountains",
                files=files,
                data={
                    "name": "Bakeworker E2E Peak",
                    "provider": "Synthetic fixture",
                    "citation": "Generated for bakeworker dispatch verification",
                    "licence": "CC0-1.0",
                    "elevations_are_metres": "true",
                },
            )
            assert response.status_code == 202, response.text
            mountain_id = response.json()["id"]

            statuses_seen: list[str] = []
            record: dict[str, Any] | None = None
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                record = client.get(f"/api/mountains/{mountain_id}").json()
                if not statuses_seen or statuses_seen[-1] != record["status"]:
                    statuses_seen.append(record["status"])
                if record["status"] in ("ready", "failed"):
                    break
                time.sleep(0.5)
            else:
                pytest.fail(f"mountain {mountain_id} never reached a terminal status: {record}")

            elapsed = time.monotonic() - start
            assert record is not None
            assert record["status"] == "ready", record.get("error")
            assert record["assessable"] is True
            assert record["verified_source"] is False

            assessment = client.post(
                f"/api/assess?mountain={mountain_id}",
                json={"new_snow_cm": 40, "wind_speed_kmh": 35, "wind_direction_deg": 270,
                      "release_size": "medium"},
            )
            assert assessment.status_code == 200, assessment.text
            body = assessment.json()
            assert body["coverage"]["release_model"]["valid_fraction"] > 0.9
            assert body["disclaimer"]

            print(
                f"[bakeworker e2e] mountain_id={mountain_id} statuses={statuses_seen} "
                f"elapsed={elapsed:.1f}s valid_fraction="
                f"{body['coverage']['release_model']['valid_fraction']:.3f}"
            )
    finally:
        _terminate(process, log_file)
