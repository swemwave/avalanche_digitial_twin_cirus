from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.core.settings import Settings
from app.services.events import landsat_qa_masks, list_events, sentinel_scl_masks, validate_event_id


def event_settings(tmp_path: Path) -> Settings:
    root = tmp_path / "mount_hosmer_data"
    event = root / "events" / "MH_20260116T183016Z"
    event.mkdir(parents=True)
    (event / "event_metadata.json").write_text(json.dumps({"event_id": event.name}), encoding="utf-8")
    return Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=root,
    )


def test_event_discovery_and_path_security(tmp_path: Path) -> None:
    settings = event_settings(tmp_path)
    payload = list_events(settings)
    assert payload["count"] == 1
    assert payload["events"][0]["event_id"] == "MH_20260116T183016Z"
    assert validate_event_id(settings, "MH_20260116T183016Z") == "MH_20260116T183016Z"
    with pytest.raises(KeyError):
        validate_event_id(settings, "../MH_20260116T183016Z")


def test_sentinel_scl_masks() -> None:
    scl = np.ma.array([[4, 8], [3, 11]], mask=False)
    inside = np.ones((2, 2), dtype=bool)
    masks = sentinel_scl_masks(scl, inside)
    assert int(masks["cloud"].sum()) == 1
    assert int(masks["shadow"].sum()) == 1
    assert int(masks["snow"].sum()) == 1
    assert int(masks["valid"].sum()) == 2
    assert masks["cloud_percent"] == 25.0
    assert masks["valid_percent"] == 50.0
    assert masks["snow_percent"] == 50.0


def test_landsat_qa_masks() -> None:
    qa = np.ma.array([[0, 1 << 3], [1 << 4, 1 << 5]], mask=False)
    inside = np.ones((2, 2), dtype=bool)
    masks = landsat_qa_masks(qa, inside)
    assert int(masks["cloud"].sum()) == 1
    assert int(masks["shadow"].sum()) == 1
    assert int(masks["snow"].sum()) == 1
    assert int(masks["valid"].sum()) == 2
    assert masks["cloud_percent"] == 25.0
    assert masks["valid_percent"] == 50.0
    assert masks["snow_percent"] == 50.0
