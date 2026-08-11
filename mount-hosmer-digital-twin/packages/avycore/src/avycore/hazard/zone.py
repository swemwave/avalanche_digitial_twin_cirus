"""AvyCore's release-zone value type, shared by risk and runout.

A neutral home so neither depends on the other: the risk model produces these and
the runout model consumes them (it reads only ``.pixels`` and ``.zone_id``).
This is the small dataclass that used to live in the deleted ``models/`` package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .constants import DISCLAIMER


@dataclass
class ReleaseZone:
    """A discrete release zone. The runout engine reads only ``pixels`` + ``zone_id``."""

    zone_id: str
    pixels: np.ndarray                    # boolean mask on the analysis grid
    geometry: dict[str, Any] | None       # WGS84 GeoJSON polygon
    properties: dict[str, Any] = field(default_factory=dict)

    def to_feature(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": self.zone_id,
            "geometry": self.geometry,
            "properties": self.properties,
        }


@dataclass
class ReleaseZoneSet:
    zones: list[ReleaseZone] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)

    def to_geojson(self) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "features": [zone.to_feature() for zone in self.zones],
            "zone_count": len(self.zones),
            "disclaimer": DISCLAIMER,
            "warnings": self.warnings,
        }
