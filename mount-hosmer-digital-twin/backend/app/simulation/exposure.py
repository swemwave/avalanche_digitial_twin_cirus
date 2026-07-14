"""What the simulated avalanche hits, and how much that matters.

The infrastructure data for this AOI is OpenStreetMap. It contains roads, tracks,
trails, a railway and power infrastructure -- and exactly **one** building.

That is almost certainly not because there is one building near Mount Hosmer. It
is because OSM building coverage in rural British Columbia is sparse. So this
module is built around a rule it states in every single result it returns:

    **A missing building is not evidence that no building exists.**

The consequence score is therefore reported alongside an explicit completeness
warning, and the API and UI are required to show it. An exposure analysis that
quietly reports "0 buildings affected" from incomplete data is worse than no
analysis, because it manufactures false reassurance.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.model_config import ModelConfig
from app.core.settings import Settings
from app.processing.harmonization.grids import AnalysisGrid

try:  # pragma: no cover
    from pyproj import Transformer
except Exception:  # pragma: no cover
    Transformer = None  # type: ignore[assignment]

CATEGORY_TITLES = {
    "buildings": "Buildings",
    "roads": "Roads",
    "tracks": "Tracks",
    "trails": "Trails and paths",
    "railways": "Railway",
    "power": "Power infrastructure",
    "aerial_lifts": "Aerial lifts",
    "waterways": "Waterways",
    "other": "Other features",
}

LINEAR_CATEGORIES = {"roads", "tracks", "trails", "railways", "power", "waterways"}


@dataclass
class ExposedAsset:
    asset_id: str
    category: str
    name: str | None
    geometry_type: str
    length_in_runout_m: float | None
    intersects: bool
    max_intensity: float
    max_velocity_ms: float | None
    distance_from_release_m: float | None
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "category": self.category,
            "category_title": CATEGORY_TITLES.get(self.category, self.category),
            "name": self.name,
            "geometry_type": self.geometry_type,
            "length_in_runout_m": (
                round(self.length_in_runout_m, 1) if self.length_in_runout_m is not None else None
            ),
            "intersects_runout": self.intersects,
            "max_flow_intensity": round(self.max_intensity, 3),
            "max_flow_velocity_ms": (
                round(self.max_velocity_ms, 2) if self.max_velocity_ms is not None else None
            ),
            "distance_from_release_m": (
                round(self.distance_from_release_m, 1)
                if self.distance_from_release_m is not None
                else None
            ),
            "osm_properties": self.properties,
        }


@dataclass
class ExposureResult:
    assets: list[ExposedAsset]
    summary: dict[str, Any]
    consequence_score: float
    consequence_class: str
    warnings: list[str] = field(default_factory=list)
    completeness: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "consequence_score": round(self.consequence_score, 1),
            "consequence_class": self.consequence_class,
            "exposed_asset_count": len(self.assets),
            "summary": self.summary,
            "assets": [asset.to_dict() for asset in self.assets],
            "data_completeness": self.completeness,
            "warnings": self.warnings,
        }


def categorize(properties: dict[str, Any], geometry_type: str) -> str:
    if properties.get("building"):
        return "buildings"
    if properties.get("railway"):
        return "railways"
    if properties.get("aerialway"):
        return "aerial_lifts"
    if properties.get("power"):
        return "power"
    if properties.get("waterway") or properties.get("natural") == "water":
        return "waterways"
    highway = properties.get("highway")
    if highway in {"path", "footway", "cycleway", "bridleway"}:
        return "trails"
    if highway == "track":
        return "tracks"
    if highway:
        return "roads"
    return "other"


def load_infrastructure(settings: Settings) -> tuple[list[dict[str, Any]], list[str]]:
    """Read the OSM feature collection and categorize it."""
    path = settings.data_root / "static" / "openstreetmap" / "mount_hosmer_osm_features.geojson"
    warnings: list[str] = []
    if not path.exists():
        return [], [f"OpenStreetMap infrastructure file is missing: {path.name}"]

    data = json.loads(path.read_text(encoding="utf-8"))
    features_out: list[dict[str, Any]] = []
    for index, feature in enumerate(data.get("features", [])):
        geometry = feature.get("geometry") or {}
        if not geometry.get("coordinates"):
            continue
        properties = dict(feature.get("properties") or {})
        category = categorize(properties, geometry.get("type", ""))
        features_out.append(
            {
                "id": str(feature.get("id") or properties.get("id") or f"osm_{index}"),
                "category": category,
                "name": properties.get("name"),
                "geometry": geometry,
                "properties": properties,
            }
        )
    return features_out, warnings


def _to_grid_coords(
    coordinates: list, transformer: "Transformer", grid: AnalysisGrid
) -> list[tuple[float, float]]:
    """Flatten any GeoJSON coordinate nesting into (row, col) on the analysis grid."""
    points: list[tuple[float, float]] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(value, (int, float)) for value in node[:2])
        ):
            x, y = transformer.transform(float(node[0]), float(node[1]))
            col = (x - grid.west) / grid.resolution_m
            row = (grid.north - y) / grid.resolution_m
            points.append((row, col))
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(coordinates)
    return points


def _densify(points: list[tuple[float, float]], step: float = 0.5) -> list[tuple[float, float]]:
    """Insert intermediate vertices so a long segment cannot skip over the runout.

    A 300 m road segment defined by two endpoints would otherwise be tested at two
    pixels and could pass straight through an avalanche path undetected.
    """
    if len(points) < 2:
        return points
    dense: list[tuple[float, float]] = [points[0]]
    for start, end in zip(points, points[1:]):
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        steps = max(1, int(distance / step))
        for index in range(1, steps + 1):
            fraction = index / steps
            dense.append(
                (
                    start[0] + (end[0] - start[0]) * fraction,
                    start[1] + (end[1] - start[1]) * fraction,
                )
            )
    return dense


def analyze(
    *,
    settings: Settings,
    config: ModelConfig,
    grid: AnalysisGrid,
    reached: np.ndarray,
    intensity: np.ndarray,
    velocity: np.ndarray | None,
    release_pixels: np.ndarray,
) -> ExposureResult:
    """Intersect the simulated runout with every known asset."""
    if Transformer is None:  # pragma: no cover
        raise RuntimeError("pyproj is required for exposure analysis")

    infrastructure, warnings = load_infrastructure(settings)
    transformer = Transformer.from_crs("EPSG:4326", grid.crs_string, always_xy=True)

    rows, cols = grid.shape
    cell = grid.resolution_m

    release_rows, release_cols = np.nonzero(release_pixels)
    has_release = release_rows.size > 0

    assets: list[ExposedAsset] = []
    totals: dict[str, dict[str, float]] = {}
    inventory: dict[str, int] = {}

    for feature in infrastructure:
        category = feature["category"]
        inventory[category] = inventory.get(category, 0) + 1

        geometry_type = feature["geometry"].get("type", "")
        points = _to_grid_coords(feature["geometry"]["coordinates"], transformer, grid)
        if not points:
            continue
        points = _densify(points)

        hit_length = 0.0
        max_intensity = 0.0
        max_velocity = 0.0
        hit = False

        previous: tuple[float, float] | None = None
        for row, col in points:
            r, c = int(round(row)), int(round(col))
            if not (0 <= r < rows and 0 <= c < cols):
                previous = None
                continue
            inside = bool(reached[r, c])
            if inside:
                hit = True
                max_intensity = max(max_intensity, float(intensity[r, c]))
                if velocity is not None:
                    max_velocity = max(max_velocity, float(velocity[r, c]))
                if previous is not None:
                    hit_length += (
                        math.hypot(row - previous[0], col - previous[1]) * cell
                    )
            previous = (row, col) if inside else None

        if not hit:
            continue

        distance = None
        if has_release:
            first = points[0]
            distances = np.hypot(release_rows - first[0], release_cols - first[1]) * cell
            distance = float(distances.min())

        assets.append(
            ExposedAsset(
                asset_id=feature["id"],
                category=category,
                name=feature["name"],
                geometry_type=geometry_type,
                length_in_runout_m=hit_length if category in LINEAR_CATEGORIES else None,
                intersects=True,
                max_intensity=max_intensity,
                max_velocity_ms=max_velocity if velocity is not None else None,
                distance_from_release_m=distance,
                properties={
                    key: value
                    for key, value in feature["properties"].items()
                    if key in {"name", "highway", "railway", "power", "building", "surface", "ref"}
                },
            )
        )

        bucket = totals.setdefault(category, {"count": 0.0, "length_m": 0.0, "max_intensity": 0.0})
        bucket["count"] += 1
        bucket["length_m"] += hit_length
        bucket["max_intensity"] = max(bucket["max_intensity"], max_intensity)

    # --- Consequence ---------------------------------------------------------
    severity = config.require("consequence.asset_severity")
    intensity_weight = float(config.require("consequence.intensity_weight"))
    exposure_weight = float(config.require("consequence.exposure_weight"))

    if assets:
        worst = 0.0
        for category, bucket in totals.items():
            base = float(severity.get(category, severity.get("other", 20)))
            # More of a thing exposed is worse, but with diminishing returns: the
            # second road crossed matters less than the first.
            extent = 1.0 + math.log1p(bucket["count"] - 1) * 0.25 if bucket["count"] > 1 else 1.0
            score = base * min(extent, 1.6) * (
                exposure_weight + intensity_weight * bucket["max_intensity"]
            )
            worst = max(worst, score)
        consequence = float(np.clip(worst, 0.0, 100.0))
    else:
        consequence = 0.0

    classes = config.require("consequence.classes")
    consequence_class = next(
        (item["label"] for item in classes if consequence <= float(item["max"])),
        classes[-1]["label"],
    )

    # --- Completeness --------------------------------------------------------
    building_count = inventory.get("buildings", 0)
    completeness_warnings: list[str] = list(warnings)

    if building_count <= 1:
        completeness_warnings.append(
            f"CRITICAL DATA GAP: only {building_count} building is present in the OpenStreetMap "
            f"extract for this area. OSM building coverage in rural British Columbia is sparse. "
            f"A low or zero count of exposed buildings in this result is NOT evidence that no "
            f"buildings are exposed -- it is evidence that the building data is incomplete. Do not "
            f"read the consequence score as an upper bound."
        )
    if not infrastructure:
        completeness_warnings.append(
            "No infrastructure data was loaded at all. The consequence score is meaningless."
        )
    if consequence == 0.0 and assets == []:
        completeness_warnings.append(
            "The simulated runout did not intersect any KNOWN asset. Given the incompleteness of "
            "the building data, this must not be read as 'nothing is at risk'."
        )

    nearest = (
        min(
            (asset for asset in assets if asset.distance_from_release_m is not None),
            key=lambda asset: asset.distance_from_release_m or math.inf,
            default=None,
        )
        if assets
        else None
    )

    runout_area_km2 = float(reached.sum()) * cell**2 / 1_000_000.0
    summary = {
        "by_category": {
            category: {
                "exposed_count": int(bucket["count"]),
                "length_in_runout_m": round(bucket["length_m"], 1)
                if category in LINEAR_CATEGORIES
                else None,
                "max_flow_intensity": round(bucket["max_intensity"], 3),
                "title": CATEGORY_TITLES.get(category, category),
            }
            for category, bucket in sorted(totals.items())
        },
        "buildings_in_runout": int(totals.get("buildings", {}).get("count", 0)),
        "road_length_in_runout_m": round(totals.get("roads", {}).get("length_m", 0.0), 1),
        "track_length_in_runout_m": round(totals.get("tracks", {}).get("length_m", 0.0), 1),
        "trail_length_in_runout_m": round(totals.get("trails", {}).get("length_m", 0.0), 1),
        "railway_length_in_runout_m": round(totals.get("railways", {}).get("length_m", 0.0), 1),
        "power_length_in_runout_m": round(totals.get("power", {}).get("length_m", 0.0), 1),
        "nearest_exposed_asset": nearest.to_dict() if nearest else None,
        "exposure_density_per_km2": (
            round(len(assets) / runout_area_km2, 2) if runout_area_km2 > 0 else 0.0
        ),
        "runout_area_km2": round(runout_area_km2, 4),
    }

    completeness = {
        "source": "OpenStreetMap (mount_hosmer_osm_features.geojson)",
        "provenance": "downloaded",
        "features_in_aoi_by_category": inventory,
        "building_features_in_aoi": building_count,
        "building_data_is_complete": False,
        "note": (
            "Absence of a feature in OpenStreetMap is not evidence of absence on the ground. "
            "Building coverage in this area is known to be incomplete."
        ),
    }

    return ExposureResult(
        assets=sorted(assets, key=lambda asset: asset.max_intensity, reverse=True),
        summary=summary,
        consequence_score=consequence,
        consequence_class=consequence_class,
        warnings=completeness_warnings,
        completeness=completeness,
    )
