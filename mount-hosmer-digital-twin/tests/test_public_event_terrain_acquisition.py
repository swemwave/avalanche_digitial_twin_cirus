from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "acquire_public_event_terrain.py"
)
SPEC = importlib.util.spec_from_file_location("acquire_public_event_terrain", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _feature(objectid: int, flight_ms: int | None, resolution: float, *, category: int = 1, project: int | None = 1) -> dict[str, object]:
    return {
        "attributes": {
            "OBJECTID": objectid,
            "CATEGORY": category,
            "LAS_PROJECT_ID": project,
            "SISTEFLYDATO": flight_ms,
            "OPPLOSNING": resolution,
        }
    }


def test_pre_event_selection_uses_latest_then_finest_then_identity() -> None:
    event = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    day = 86_400_000
    features = [
        _feature(9, int(event.timestamp() * 1000) - 2 * day, 0.25),
        _feature(8, int(event.timestamp() * 1000) - day, 1.0),
        _feature(7, int(event.timestamp() * 1000) - day, 0.5),
        _feature(6, int(event.timestamp() * 1000) - day, 0.5),
        _feature(1, int(event.timestamp() * 1000) + day, 0.25),
    ]
    selected = MODULE.select_pre_event_project(features, event)
    assert selected is not None
    assert selected["OBJECTID"] == 6


def test_selection_rejects_undated_overview_and_post_event_projects() -> None:
    event = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    features = [
        _feature(1, None, 1.0),
        _feature(2, int(event.timestamp() * 1000) + 1, 1.0),
        _feature(3, int(event.timestamp() * 1000) - 1, 1.0, category=2),
        _feature(4, int(event.timestamp() * 1000) - 1, 1.0, project=None),
    ]
    assert MODULE.select_pre_event_project(features, event) is None


def test_immutable_cache_never_overwrites_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "terrain.tif"
    MODULE._write_immutable(path, b"one")
    MODULE._write_immutable(path, b"one")
    with pytest.raises(ValueError, match="Immutable terrain-cache conflict"):
        MODULE._write_immutable(path, b"two")


def test_public_read_rejects_non_hoydedata_host() -> None:
    with pytest.raises(ValueError, match="unexpected terrain host"):
        MODULE._public_get("https://example.com/terrain.tif")
