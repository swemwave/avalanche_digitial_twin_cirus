from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_api_rejects_path_like_ids(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "mount_hosmer_data"
    runtime_root = tmp_path / "runtime"
    (data_root / "events").mkdir(parents=True)
    monkeypatch.setenv("MOUNT_HOSMER_DATA_ROOT", str(data_root))
    monkeypatch.setenv("MOUNT_HOSMER_RUNTIME_ROOT", str(runtime_root))

    from app.main import app

    client = TestClient(app)

    assert client.get("/api/events").json()["count"] == 0
    assert client.get("/api/events/not_an_event").status_code == 404
    assert client.get("/api/susceptibility/events/not_an_event").status_code == 404
    assert client.get("/api/download/..%2Fsecret").status_code == 404
