r"""Bake-time exposure: what is in the valley, from OpenStreetMap.

⚠️ **Bake-time only.** This module imports pyproj and shapely and reads ``DATA\``.
The running service never imports it; it reads the two artifacts written here out
of ``runtime/baked/``.

The runout model already knows how far an avalanche can run. It has never known
what is down there. This turns the project's OpenStreetMap extract into two baked
products:

    runtime/baked/exposure/features.geojson   WGS84 classified lines/polygons, for display
    runtime/baked/layers/exposure_weight.npy  float32 0-1 per analysis-grid cell
    runtime/baked/layers/exposure_class.npy   uint8 class code per cell (0 = nothing mapped)

**Exposure is a consequence term and nothing else.** It never enters
:mod:`avycore.hazard.risk`; the release model does not import it, receive it, or
see it. It enters only the named exposure term of the composite hazard index, where
it can raise a zone's index and never lower it. See ``docs/limitations.md``.

**"Important living areas" are derived, not surveyed.** This AOI's OSM extract
contains exactly one ``building`` way, no ``landuse`` and no ``place`` polygon, so
there is nothing to draw directly. The built-up outlines here are inferred from
residential/service road clustering by the documented rule below. They are a proxy
for residential road density -- not a survey of occupied structures -- and the
absence of an outline is not evidence that nobody lives there. No place is named
that OSM does not name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from app.processing.harmonization.grids import AnalysisGrid

#: Cells tested per rasterisation chunk. Mirrors the bounded cell-centre approach
#: in ``avycore.validation.metrics._rasterize_cell_centers`` -- a Shapely-2 vector
#: predicate over cell centres, so the bake needs no new rasterisation dependency.
RASTER_CHUNK_CELLS = 4_000_000

#: Exposure classes, most consequential first. ``weight`` is an UNCALIBRATED
#: relative judgement of what it costs to be hit, not a casualty or damage model.
#: ``buffer_m`` widens a mapped centreline to something with real ground extent;
#: OSM lines carry no width, and a 5 m analysis cell would otherwise miss a road
#: whose centreline threads between two cell centres.
EXPOSURE_CLASSES: tuple[dict[str, Any], ...] = (
    {
        "name": "inferred_settlement",
        "label": "Inferred built-up area",
        "code": 1,
        "weight": 1.00,
        "buffer_m": None,
        "tags": "derived from residential/service road clustering",
        "derived": True,
    },
    {
        "name": "highway_major",
        "label": "Trunk highway",
        "code": 2,
        "weight": 0.90,
        "buffer_m": 15.0,
        "tags": "highway=trunk, highway=trunk_link",
        "derived": False,
    },
    {
        "name": "building_mapped",
        "label": "Mapped building",
        "code": 3,
        "weight": 0.90,
        "buffer_m": 5.0,
        "tags": "building=*",
        "derived": False,
    },
    {
        "name": "railway",
        "label": "Railway",
        "code": 4,
        "weight": 0.75,
        "buffer_m": 10.0,
        "tags": "railway=rail",
        "derived": False,
    },
    {
        "name": "road_local",
        "label": "Local road",
        "code": 5,
        "weight": 0.50,
        "buffer_m": 8.0,
        "tags": "highway=residential, unclassified, service, rest_area",
        "derived": False,
    },
    {
        "name": "track_trail",
        "label": "Track or trail",
        "code": 6,
        "weight": 0.20,
        "buffer_m": 4.0,
        "tags": "highway=track, highway=path",
        "derived": False,
    },
)

CLASS_BY_NAME = {item["name"]: item for item in EXPOSURE_CLASSES}

#: Tag -> class routing. Anything not listed is not exposure and is not baked.
_HIGHWAY_CLASSES = {
    "trunk": "highway_major",
    "trunk_link": "highway_major",
    "residential": "road_local",
    "unclassified": "road_local",
    "service": "road_local",
    "rest_area": "road_local",
    "track": "track_trail",
    "path": "track_trail",
}
_RAILWAY_CLASSES = {"rail": "railway"}

#: Tags deliberately excluded from the exposure index, and why. Recorded in
#: meta.json so the omission is a decision on the record rather than an oversight.
EXCLUDED_TAGS = {
    "waterway": (
        "Streams, rivers and canals are terrain, not people or assets. Including them "
        "would mark every gully on the mountain as exposed."
    ),
    "power": (
        "Transmission towers and lines are real infrastructure exposure, but this pass "
        "weights only what people travel on and live in. Power is a known omission, not "
        "an assertion that a struck tower does not matter."
    ),
    "node_features": (
        "Point nodes (railway switches and level crossings, highway turning circles, "
        "power towers and poles) add no ground extent beyond the line features that "
        "already carry them, so they are not buffered into the raster."
    ),
}

#: Derivation rule for the inferred built-up outlines. Every number is published.
SETTLEMENT_CLUSTER_BUFFER_M = 60.0
SETTLEMENT_MIN_ROAD_LENGTH_M = 1500.0
SETTLEMENT_SOURCE_TAGS = ("highway=residential", "highway=service")

ATTRIBUTION = "© OpenStreetMap contributors"
LICENCE = "Open Database License (ODbL) 1.0"

LIMITATION = (
    "Exposure is an OpenStreetMap extract, not a survey. OSM completeness varies and is "
    "not guaranteed: a cell with no exposure weight means OSM maps nothing there, NOT that "
    "nothing is there. Built-up outlines are INFERRED from residential/service road "
    "clustering, not from mapped buildings or parcels -- this AOI's extract contains one "
    "building way, no landuse and no place polygon -- so an outline is a proxy for "
    "residential road density and is not a survey of occupied structures. Class weights are "
    "uncalibrated relative judgements, not a casualty, damage or loss model. Exposure never "
    "enters the release model; it can only raise the consequence term of the composite "
    "hazard index, never lower it."
)


class ExposureError(RuntimeError):
    """Raised when declared exposure data cannot be turned into a baked layer."""


@dataclass
class ExposureProducts:
    """Everything the exposure bake produced, ready to be written."""

    weight: np.ma.MaskedArray
    class_code: np.ma.MaskedArray
    features: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


# --- Source reading ----------------------------------------------------------


def _load_feature_collection(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExposureError(f"Exposure source is unreadable: {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ExposureError(f"Exposure source is not a GeoJSON FeatureCollection: {path}")
    features = document.get("features")
    if not isinstance(features, list):
        raise ExposureError(f"Exposure source has no feature list: {path}")
    return features


def _classify(properties: dict[str, Any], geometry_type: str) -> str | None:
    """Route one OSM feature to an exposure class, or to nothing at all."""
    if geometry_type == "Point":
        # Nodes carry no ground extent the parent line does not already cover.
        return None
    if properties.get("building"):
        return "building_mapped"
    highway = properties.get("highway")
    if isinstance(highway, str) and highway in _HIGHWAY_CLASSES:
        return _HIGHWAY_CLASSES[highway]
    railway = properties.get("railway")
    if isinstance(railway, str) and railway in _RAILWAY_CLASSES:
        return _RAILWAY_CLASSES[railway]
    return None


def _project_geometry(geometry: dict[str, Any], transformer) -> Any:
    """Rebuild one GeoJSON geometry in the analysis CRS as a Shapely object."""
    from shapely.geometry import shape
    from shapely.ops import transform as shapely_transform

    def _forward(x, y, z=None):
        easting, northing = transformer.transform(x, y)
        return (easting, northing)

    return shapely_transform(_forward, shape(geometry))


# --- Derived settlement outlines ---------------------------------------------


def _infer_settlements(residential_lines: Sequence[Any]) -> tuple[list[Any], dict[str, Any]]:
    """Cluster residential/service roads into built-up outlines.

    The rule, in full: buffer every residential and service road by
    ``SETTLEMENT_CLUSTER_BUFFER_M``, union the buffers, and keep each resulting
    cluster whose contained residential/service road length reaches
    ``SETTLEMENT_MIN_ROAD_LENGTH_M``. A lone driveway or a single access road never
    clears the threshold; a townsite's street grid does.
    """
    import shapely

    report: dict[str, Any] = {
        "rule": (
            f"Buffer every {' and '.join(SETTLEMENT_SOURCE_TAGS)} way by "
            f"{SETTLEMENT_CLUSTER_BUFFER_M:g} m, union the buffers, and keep each cluster whose "
            f"contained residential/service road length reaches "
            f"{SETTLEMENT_MIN_ROAD_LENGTH_M:g} m."
        ),
        "cluster_buffer_m": SETTLEMENT_CLUSTER_BUFFER_M,
        "minimum_road_length_m": SETTLEMENT_MIN_ROAD_LENGTH_M,
        "source_tags": list(SETTLEMENT_SOURCE_TAGS),
        "candidate_cluster_count": 0,
        "accepted_cluster_count": 0,
        "accepted_road_length_m": [],
        "rejected_road_length_m": [],
        "is_survey_of_structures": False,
    }
    if not residential_lines:
        return [], report

    merged = shapely.union_all([line.buffer(SETTLEMENT_CLUSTER_BUFFER_M) for line in residential_lines])
    clusters = list(getattr(merged, "geoms", [merged]))
    report["candidate_cluster_count"] = len(clusters)

    accepted: list[Any] = []
    for cluster in clusters:
        shapely.prepare(cluster)
        length = 0.0
        for line in residential_lines:
            if cluster.intersects(line):
                length += float(line.intersection(cluster).length)
        if length >= SETTLEMENT_MIN_ROAD_LENGTH_M:
            accepted.append(cluster)
            report["accepted_road_length_m"].append(round(length, 1))
        else:
            report["rejected_road_length_m"].append(round(length, 1))
    report["accepted_cluster_count"] = len(accepted)
    report["accepted_road_length_m"].sort(reverse=True)
    report["rejected_road_length_m"].sort(reverse=True)
    return accepted, report


# --- Rasterisation -----------------------------------------------------------


def _rasterize_cell_centers(
    geometry: Any, grid: AnalysisGrid, *, max_chunk_cells: int = RASTER_CHUNK_CELLS
) -> np.ndarray:
    """Rasterize by testing north-up cell centres in bounded Shapely 2 chunks.

    The same technique the validation overlap evaluator uses
    (``avycore.validation.metrics._rasterize_cell_centers``): a vector point-in-
    polygon test over cell centres, chunked so a 2400x2400 grid never builds one
    5.8 M-point array. It keeps the bake free of a second rasterisation library and
    keeps the cell-inclusion rule identical to the one already documented.
    """
    import shapely

    rows, cols = grid.shape
    if max_chunk_cells <= 0:
        raise ValueError("max_chunk_cells must be positive.")
    rows_per_chunk = max(1, max_chunk_cells // cols)
    x = grid.west + (np.arange(cols, dtype="float64") + 0.5) * grid.resolution_m
    result = np.zeros(grid.shape, dtype=bool)
    shapely.prepare(geometry)
    for row_start in range(0, rows, rows_per_chunk):
        row_stop = min(rows, row_start + rows_per_chunk)
        row_numbers = np.arange(row_start, row_stop, dtype="float64")
        y = grid.north - (row_numbers + 0.5) * grid.resolution_m
        xx = np.broadcast_to(x, (row_stop - row_start, cols))
        yy = np.broadcast_to(y[:, None], xx.shape)
        result[row_start:row_stop] = shapely.intersects_xy(geometry, xx, yy)
    return result


# --- Orchestration -----------------------------------------------------------


def _to_wgs84_feature(
    geometry: Any, transformer_back, *, exposure_class: str, properties: dict[str, Any]
) -> dict[str, Any] | None:
    """Emit one display feature back in WGS84, keeping any mapped name."""
    from shapely.geometry import mapping
    from shapely.ops import transform as shapely_transform

    if geometry.is_empty:
        return None

    def _back(x, y, z=None):
        longitude, latitude = transformer_back.transform(x, y)
        return (longitude, latitude)

    spec = CLASS_BY_NAME[exposure_class]
    kept = {
        key: properties[key]
        for key in ("name", "ref", "operator", "osm_type", "osm_id")
        if properties.get(key) not in (None, "")
    }
    return {
        "type": "Feature",
        "geometry": mapping(shapely_transform(_back, geometry)),
        "properties": {
            **kept,
            "exposure_class": exposure_class,
            "exposure_label": spec["label"],
            "exposure_weight": spec["weight"],
            "derived": spec["derived"],
        },
    }


def build_exposure(
    *,
    source_path: Path,
    aoi_path: Path,
    grid: AnalysisGrid,
    source_statement: dict[str, str],
) -> ExposureProducts:
    """Reproject, classify, buffer, derive, rasterise. Bake-time only."""
    import shapely
    from pyproj import Transformer
    from shapely.geometry import shape

    to_grid = Transformer.from_crs("EPSG:4326", grid.crs_string, always_xy=True)
    to_wgs84 = Transformer.from_crs(grid.crs_string, "EPSG:4326", always_xy=True)

    aoi_features = _load_feature_collection(aoi_path)
    if not aoi_features:
        raise ExposureError(f"AOI GeoJSON contains no features: {aoi_path}")
    aoi = shapely.union_all(
        [_project_geometry(item["geometry"], to_grid) for item in aoi_features]
    )

    features = _load_feature_collection(source_path)
    by_class: dict[str, list[Any]] = {item["name"]: [] for item in EXPOSURE_CLASSES}
    display: list[dict[str, Any]] = []
    settlement_sources: list[Any] = []
    counts = {item["name"]: 0 for item in EXPOSURE_CLASSES}
    skipped = 0

    for feature in features:
        geometry_spec = feature.get("geometry") or {}
        geometry_type = geometry_spec.get("type")
        properties = feature.get("properties") or {}
        exposure_class = _classify(properties, str(geometry_type))
        if exposure_class is None:
            skipped += 1
            continue
        try:
            projected = _project_geometry(geometry_spec, to_grid)
        except Exception as exc:  # pragma: no cover - malformed source geometry
            raise ExposureError(f"Exposure feature {feature.get('id')} is unusable: {exc}") from exc
        if projected.is_empty:
            skipped += 1
            continue

        # Cluster settlements from the FULL extract, before clipping: the extract
        # covers the AOI bounding box, and a townsite that straddles the analysis
        # boundary must not be split into two sub-threshold halves.
        if exposure_class == "road_local" and properties.get("highway") in {
            "residential",
            "service",
        }:
            settlement_sources.append(projected)

        clipped = projected.intersection(aoi)
        if clipped.is_empty:
            continue
        by_class[exposure_class].append(clipped)
        counts[exposure_class] += 1
        display_feature = _to_wgs84_feature(
            clipped, to_wgs84, exposure_class=exposure_class, properties=properties
        )
        if display_feature is not None:
            display.append(display_feature)

    settlements, settlement_report = _infer_settlements(settlement_sources)
    for outline in settlements:
        clipped = outline.intersection(aoi)
        if clipped.is_empty:
            continue
        by_class["inferred_settlement"].append(clipped)
        counts["inferred_settlement"] += 1
        display_feature = _to_wgs84_feature(
            clipped,
            to_wgs84,
            exposure_class="inferred_settlement",
            properties={},
        )
        if display_feature is not None:
            display.append(display_feature)

    # Rasterise least consequential first, so the more consequential class wins the
    # cell. Weight is a max, never a sum: a cell holding both a highway and a track
    # is as exposed as its worst occupant, and summing would push past 1.
    weight = np.zeros(grid.shape, dtype="float32")
    class_code = np.zeros(grid.shape, dtype="uint8")
    raster_cells = {item["name"]: 0 for item in EXPOSURE_CLASSES}
    for spec in sorted(EXPOSURE_CLASSES, key=lambda item: item["weight"]):
        geometries = by_class[spec["name"]]
        if not geometries:
            continue
        buffer_m = spec["buffer_m"]
        prepared = [
            geometry.buffer(buffer_m) if buffer_m else geometry for geometry in geometries
        ]
        covered = _rasterize_cell_centers(shapely.union_all(prepared), grid)
        raster_cells[spec["name"]] = int(np.count_nonzero(covered))
        hit = covered & (weight < spec["weight"])
        weight[hit] = spec["weight"]
        class_code[hit] = spec["code"]

    # Outside the AOI there is no exposure evidence at all, so the cell is unknown
    # rather than a weight of zero. Inside it, zero is a real measurement: the OSM
    # extract covers this ground and maps nothing on it.
    # The underlying values carry the NoData sentinel too, not just the mask, so a
    # caller that reaches past the mask still cannot read unsurveyed ground as a
    # weight of zero.
    inside = _rasterize_cell_centers(aoi, grid)
    weight[~inside] = np.nan
    class_code[~inside] = 255
    weight_masked = np.ma.array(weight, mask=~inside)
    class_masked = np.ma.array(class_code, mask=~inside)

    inside_cells = int(np.count_nonzero(inside))
    exposed_cells = int(np.count_nonzero((weight > 0) & inside))
    cell_area = grid.resolution_m**2

    meta = {
        "source": dict(source_statement),
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
        "analysis_crs": grid.crs_string,
        "reprojected_at": "bake_time",
        "classes": [
            {
                "name": spec["name"],
                "label": spec["label"],
                "code": spec["code"],
                "weight": spec["weight"],
                "buffer_m": spec["buffer_m"],
                "tags": spec["tags"],
                "derived": spec["derived"],
                "feature_count": counts[spec["name"]],
                "grid_cell_count": raster_cells[spec["name"]],
            }
            for spec in EXPOSURE_CLASSES
        ],
        "class_weight_by_name": {
            spec["name"]: spec["weight"] for spec in EXPOSURE_CLASSES
        },
        "class_label_by_code": {
            str(spec["code"]): spec["label"] for spec in EXPOSURE_CLASSES
        },
        "settlement_derivation": settlement_report,
        "excluded_tags": dict(EXCLUDED_TAGS),
        "source_feature_count": len(features),
        "classified_feature_count": sum(counts.values()),
        "unclassified_feature_count": skipped,
        "aoi_cell_count": inside_cells,
        "exposed_cell_count": exposed_cells,
        "exposed_area_m2": round(exposed_cells * cell_area, 1),
        "exposed_fraction_of_aoi": (
            round(exposed_cells / inside_cells, 6) if inside_cells else 0.0
        ),
        "cell_inclusion_rule": (
            "A grid cell carries a class if its centre falls inside that class's buffered "
            "geometry, tested with Shapely 2 vector predicates in bounded chunks."
        ),
        "weight_combination_rule": "max over classes; never a sum",
        "used_in_release_model": False,
        "used_in_runout_model": False,
        "role": "consequence_term_of_composite_hazard_index_only",
        "is_calibrated": False,
        "limitation": LIMITATION,
    }

    return ExposureProducts(
        weight=weight_masked,
        class_code=class_masked,
        features={
            "type": "FeatureCollection",
            "attribution": ATTRIBUTION,
            "licence": LICENCE,
            "derived_classes": ["inferred_settlement"],
            "limitation": LIMITATION,
            "features": display,
        },
        meta=meta,
    )


