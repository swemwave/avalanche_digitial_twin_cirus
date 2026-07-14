from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.settings import Settings
from app.services.json_utils import summarize_json


def load_aoi(settings: Settings) -> dict[str, Any]:
    path = settings.data_root / "metadata" / "mount_hosmer_aoi.geojson"
    if not path.exists():
        raise FileNotFoundError("metadata/mount_hosmer_aoi.geojson is missing")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "source": "metadata/mount_hosmer_aoi.geojson",
        "geojson": data,
        "summary": summarize_json(data),
    }


def load_grid(settings: Settings) -> dict[str, Any]:
    path = settings.data_root / "metadata" / "grid_and_aoi.json"
    if not path.exists():
        raise FileNotFoundError("metadata/grid_and_aoi.json is missing")
    return json.loads(path.read_text(encoding="utf-8"))
