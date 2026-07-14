"""Turn the estimated release-score raster into discrete, described release zones.

A raster is not something a person can act on. A polygon with an area, a mean
slope, a dominant aspect, and a written reason for its score is. This module does
that conversion, and it attaches to every zone the evidence for -- and the
limitations of -- its own number.

The dominant aspect of a zone is computed as a **vector mean**, not a modal bin.
A bowl whose pixels face 350 and 10 degrees faces north, not south.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import ndimage

from app.core.model_config import DISCLAIMER, ModelConfig
from app.processing.harmonization.grids import AnalysisGrid

try:  # pragma: no cover
    import rasterio
    from rasterio import features
    from rasterio.warp import transform_geom
except Exception:  # pragma: no cover
    rasterio = None  # type: ignore[assignment]

COMPASS = [
    (0, "N"), (22.5, "NNE"), (45, "NE"), (67.5, "ENE"),
    (90, "E"), (112.5, "ESE"), (135, "SE"), (157.5, "SSE"),
    (180, "S"), (202.5, "SSW"), (225, "SW"), (247.5, "WSW"),
    (270, "W"), (292.5, "WNW"), (315, "NW"), (337.5, "NNW"),
]


def compass_name(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    index = int(((degrees % 360.0) + 11.25) // 22.5) % 16
    return COMPASS[index][1]


def vector_mean_aspect(aspect_values: np.ndarray) -> tuple[float | None, float | None]:
    """Dominant aspect as a vector mean, with a consistency figure.

    Flat pixels (-1) are excluded rather than counted as north.
    """
    values = aspect_values[(aspect_values >= 0) & np.isfinite(aspect_values)]
    if values.size == 0:
        return None, None
    radians = np.deg2rad(values)
    east = float(np.sin(radians).mean())
    north = float(np.cos(radians).mean())
    if abs(east) < 1e-12 and abs(north) < 1e-12:
        return None, 0.0
    direction = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    return round(direction, 1), round(math.hypot(east, north), 4)


@dataclass
class ReleaseZone:
    zone_id: str
    geometry: dict[str, Any]          # GeoJSON polygon, WGS84
    geometry_utm: dict[str, Any]      # same polygon in the analysis CRS
    properties: dict[str, Any]
    pixels: np.ndarray                # boolean mask on the analysis grid

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

    def by_id(self, zone_id: str) -> ReleaseZone | None:
        return next((zone for zone in self.zones if zone.zone_id == zone_id), None)


def aspect_sector(aspect_values: np.ndarray, sector_deg: float) -> np.ndarray:
    """Bin aspect into sectors, with flat terrain in its own sector (-1).

    Sectors are *centred* on north, so that a north-facing slope straddling the
    0/360 seam lands in one sector instead of being split in two.

    Flat pixels have no aspect. They are given their own sector rather than being
    folded into north, because otherwise a flat col could act as a bridge joining
    two slopes that face opposite ways -- which is exactly the merge this function
    exists to prevent.
    """
    sectors = max(1, int(round(360.0 / sector_deg)))
    width = 360.0 / sectors
    binned = np.floor(((aspect_values + width / 2.0) % 360.0) / width).astype(np.int32)
    return np.where(aspect_values >= 0, binned, -1).astype(np.int32)


def segment_release_zones(
    candidate: np.ndarray,
    aspect_values: np.ndarray,
    sector_deg: float,
    elevation_values: np.ndarray | None = None,
    band_m: float = 0.0,
) -> tuple[np.ndarray, int]:
    """Label connected components within aspect sectors, and within elevation bands.

    Plain connected-component labelling is wrong here, and quietly so. The steep
    ground encircling a peak is topologically connected all the way round it, so a
    single label floods across the north face, the east face and the south face
    alike. The result is one enormous "release zone" spanning every aspect, whose
    mean aspect is a vector average of the entire compass and therefore means
    nothing. It cannot be simulated either, because a zone that faces every
    direction has no defined fall line.

    A release zone is a coherent slab. Two patches of steep ground that face
    different ways are two slopes, however they touch: they are loaded by different
    winds, lit by different sun, and they release independently. So components are
    grown inside aspect sectors and are never allowed to merge across them.

    Aspect alone is not enough. A band of steep ground at one aspect can still run
    the whole height of a face: on the real mountain this produced a zone that held
    a coherent aspect while stretching 3.2 km along the massif and spanning 707 m of
    vertical. A slab does not do that. Start zones are a few hundred metres of
    vertical at most, so components are also cut into elevation bands, which bounds
    the vertical extent and with it the size and shape of a zone.

    A band boundary is an arbitrary line, and a slab that straddles one is reported
    as two adjacent zones. That is a real cost, and it is the smaller one: a single
    zone spanning an entire mountain face has no meaningful crown, no fall line, and
    no defensible release point to simulate from.
    """
    labels = np.zeros(candidate.shape, dtype=np.int32)
    keys = aspect_sector(aspect_values, sector_deg).astype(np.int64)

    if elevation_values is not None and band_m > 0:
        bands = np.floor(np.nan_to_num(elevation_values, nan=0.0) / band_m).astype(np.int64)
        # Combine into one key. Aspect sectors are few, so shifting the band clear of
        # them keeps the pair unique.
        keys = keys * 100_000 + bands

    structure = np.ones((3, 3), dtype=int)
    count = 0
    for key in np.unique(keys[candidate]):
        component, found = ndimage.label(candidate & (keys == key), structure=structure)
        if found:
            hit = component > 0
            labels[hit] = component[hit] + count
            count += found
    return labels, count


def _mean_in(array: np.ma.MaskedArray | None, pixels: np.ndarray) -> float | None:
    if array is None:
        return None
    values = np.asarray(array.filled(np.nan))[pixels]
    values = values[np.isfinite(values)]
    return round(float(values.mean()), 2) if values.size else None


def _max_in(array: np.ma.MaskedArray | None, pixels: np.ndarray) -> float | None:
    if array is None:
        return None
    values = np.asarray(array.filled(np.nan))[pixels]
    values = values[np.isfinite(values)]
    return round(float(values.max()), 2) if values.size else None


def extract(
    *,
    config: ModelConfig,
    grid: AnalysisGrid,
    release_score: np.ma.MaskedArray,
    terrain_susceptibility: np.ma.MaskedArray,
    dynamic_instability: np.ma.MaskedArray | None,
    slope: np.ma.MaskedArray,
    aspect: np.ma.MaskedArray,
    elevation: np.ma.MaskedArray,
    plan_curvature: np.ma.MaskedArray,
    landcover: np.ma.MaskedArray,
    starting_zone_terrain: np.ma.MaskedArray,
    forest_mask: np.ma.MaskedArray,
    wind_loading: np.ma.MaskedArray | None,
    snow_depth_index: np.ma.MaskedArray | None,
    terrain_source: np.ma.MaskedArray | None = None,
    max_zones: int = 60,
) -> ReleaseZoneSet:
    """Threshold, smooth, label and describe the release zones."""
    if rasterio is None:  # pragma: no cover
        raise RuntimeError("rasterio is required for release-zone extraction")

    threshold = float(config.require("release_zones.minimum_release_score"))
    min_area = float(config.require("release_zones.minimum_area_m2"))
    smoothing_m = float(config.require("release_zones.smoothing_radius_m"))

    warnings: list[str] = []
    cell_area = grid.resolution_m**2
    min_pixels = max(1, int(round(min_area / cell_area)))

    scores = np.asarray(release_score.filled(0.0))
    candidate = (scores >= threshold) & ~np.ma.getmaskarray(release_score)

    # A release zone must be terrain that can actually start an avalanche. A high
    # score on a 15-degree forested bench is a scoring artefact, not a start zone.
    candidate &= np.asarray(starting_zone_terrain.filled(0.0)) > 0.5

    if not candidate.any():
        warnings.append(
            f"No pixel reached the minimum release score of {threshold:g} on terrain capable of "
            f"starting an avalanche. No release zones were generated."
        )
        return ReleaseZoneSet(
            zones=[],
            warnings=warnings,
            explanation={
                "threshold": threshold,
                "minimum_area_m2": min_area,
                "zone_count": 0,
                "disclaimer": DISCLAIMER,
            },
        )

    # Close pinholes and shave off single-pixel spurs, so a zone is a coherent
    # piece of slope rather than a spray of noise.
    radius = max(1, int(round(smoothing_m / grid.resolution_m)))
    structure = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    smoothed = ndimage.binary_closing(candidate, structure=structure)
    smoothed = ndimage.binary_opening(smoothed, structure=np.ones((3, 3), dtype=bool))
    smoothed &= ~np.ma.getmaskarray(release_score)

    # Grow components inside aspect sectors AND elevation bands. Labelling straight
    # across the mask would fuse every face of a peak into one zone, and aspect
    # alone still lets a zone run the whole height of a face -- see
    # segment_release_zones.
    sector_deg = float(config.get("release_zones.aspect_sector_deg", 45.0))
    band_m = float(config.get("release_zones.elevation_band_m", 0.0))
    labels, count = segment_release_zones(
        smoothed,
        np.asarray(aspect.filled(-1.0)),
        sector_deg,
        np.asarray(elevation.filled(np.nan)),
        band_m,
    )
    if count == 0:
        warnings.append("Candidate pixels did not survive smoothing; no release zones were generated.")
        return ReleaseZoneSet(zones=[], warnings=warnings, explanation={"zone_count": 0})

    sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, count + 1))
    order = np.argsort(sizes)[::-1]

    zones: list[ReleaseZone] = []
    for rank, index in enumerate(order):
        label_id = int(index) + 1
        if sizes[index] < min_pixels:
            continue
        if len(zones) >= max_zones:
            warnings.append(
                f"More than {max_zones} release zones met the threshold; only the {max_zones} "
                f"largest are returned."
            )
            break

        pixels = labels == label_id
        zone = _describe(
            zone_id=f"RZ{len(zones) + 1:03d}",
            pixels=pixels,
            grid=grid,
            config=config,
            release_score=release_score,
            terrain_susceptibility=terrain_susceptibility,
            dynamic_instability=dynamic_instability,
            slope=slope,
            aspect=aspect,
            elevation=elevation,
            plan_curvature=plan_curvature,
            landcover=landcover,
            forest_mask=forest_mask,
            wind_loading=wind_loading,
            snow_depth_index=snow_depth_index,
            terrain_source=terrain_source,
        )
        if zone is not None:
            zones.append(zone)

    # A zone far larger than any real slab means components have merged across
    # terrain they should not have. Surface it rather than letting it pass as a
    # plausible-looking number.
    maximum_area = float(config.get("release_zones.maximum_expected_area_m2", 1_000_000.0))
    oversized = [zone for zone in zones if zone.properties["area_m2"] > maximum_area]
    if oversized:
        warnings.append(
            f"{len(oversized)} release zone(s) exceed {maximum_area / 10_000:.0f} hectares, which is "
            f"larger than a single coherent slab is expected to be. Treat their aspect and their "
            f"score as an average over heterogeneous terrain rather than as one slope."
        )

    explanation = {
        "threshold": threshold,
        "minimum_area_m2": min_area,
        "smoothing_radius_m": smoothing_m,
        "aspect_sector_deg": sector_deg,
        "zone_count": len(zones),
        "total_release_area_km2": round(
            sum(zone.properties["area_m2"] for zone in zones) / 1_000_000.0, 4
        ),
        "method": (
            "Pixels above the release-score threshold that also sit on terrain capable of starting "
            "an avalanche, morphologically closed and opened, then connected-component labelled "
            f"WITHIN {sector_deg:g}-degree aspect sectors, and filtered by minimum area. Components "
            "are never merged across aspect sectors: the steep ground around a peak is topologically "
            "connected all the way round it, so labelling across aspects would fuse every face of the "
            "mountain into a single zone with no defined fall line."
        ),
        "is_probability": False,
        "disclaimer": DISCLAIMER,
        "limitations": [
            "A release zone is a region of terrain the model considers capable of releasing under "
            "the given conditions. It is NOT a prediction that an avalanche will occur there.",
            "The threshold is a configured value, not a calibrated one. Moving it changes the "
            "number of zones and has no observational basis.",
            f"Zones are cut at {sector_deg:g}-degree aspect-sector boundaries. A single slab whose "
            f"aspect rotates across a sector boundary is reported as two adjacent zones.",
        ],
    }
    return ReleaseZoneSet(zones=zones, warnings=warnings, explanation=explanation)


def _describe(
    *,
    zone_id: str,
    pixels: np.ndarray,
    grid: AnalysisGrid,
    config: ModelConfig,
    release_score: np.ma.MaskedArray,
    terrain_susceptibility: np.ma.MaskedArray,
    dynamic_instability: np.ma.MaskedArray | None,
    slope: np.ma.MaskedArray,
    aspect: np.ma.MaskedArray,
    elevation: np.ma.MaskedArray,
    plan_curvature: np.ma.MaskedArray,
    landcover: np.ma.MaskedArray,
    forest_mask: np.ma.MaskedArray,
    wind_loading: np.ma.MaskedArray | None,
    snow_depth_index: np.ma.MaskedArray | None,
    terrain_source: np.ma.MaskedArray | None,
) -> ReleaseZone | None:
    from app.processing.terrain.engine import ESA_WORLDCOVER

    shapes = list(
        features.shapes(
            pixels.astype("uint8"), mask=pixels, transform=grid.transform, connectivity=8
        )
    )
    if not shapes:
        return None
    geometry_utm = max(shapes, key=lambda item: _polygon_area(item[0]))[0]
    geometry_wgs84 = transform_geom(grid.crs, "EPSG:4326", geometry_utm, precision=6)

    area_m2 = float(pixels.sum()) * grid.resolution_m**2
    aspect_values = np.asarray(aspect.filled(-1.0))[pixels]
    dominant_aspect, aspect_consistency = vector_mean_aspect(aspect_values)

    elevations = np.asarray(elevation.filled(np.nan))[pixels]
    elevations = elevations[np.isfinite(elevations)]

    lc_values = np.asarray(landcover.filled(-1)).astype(int)[pixels]
    lc_counts: dict[str, float] = {}
    for code in np.unique(lc_values):
        if int(code) in ESA_WORLDCOVER:
            lc_counts[ESA_WORLDCOVER[int(code)]["label"]] = round(
                float((lc_values == code).sum()) / float(lc_values.size), 3
            )

    mean_release = _mean_in(release_score, pixels) or 0.0
    mean_terrain = _mean_in(terrain_susceptibility, pixels) or 0.0
    mean_dynamic = _mean_in(dynamic_instability, pixels)
    mean_slope = _mean_in(slope, pixels) or 0.0
    max_slope = _max_in(slope, pixels) or 0.0
    mean_wind = _mean_in(wind_loading, pixels)
    mean_snow = _mean_in(snow_depth_index, pixels)
    mean_plan = _mean_in(plan_curvature, pixels)
    forest_fraction = round(float(np.asarray(forest_mask.filled(0.0))[pixels].mean()), 3)

    lidar_fraction = None
    if terrain_source is not None:
        codes = np.asarray(terrain_source.filled(0))[pixels]
        lidar_fraction = round(float(np.isin(codes, [1, 2]).mean()), 3)

    reasons = _reasons(
        mean_slope=mean_slope,
        max_slope=max_slope,
        dominant_aspect=dominant_aspect,
        mean_wind=mean_wind,
        mean_dynamic=mean_dynamic,
        mean_plan=mean_plan,
        forest_fraction=forest_fraction,
        elevation_range=(float(elevations.min()), float(elevations.max())) if elevations.size else None,
        config=config,
    )

    limitations = [
        "This score is a relative index, not a probability that this slope will release.",
        "The model has no snow-profile data, so it cannot see buried weak layers.",
    ]
    if mean_dynamic is None:
        limitations.append(
            "The dynamic half of the score was withheld for lack of input data; this zone is "
            "described by terrain alone."
        )
    if lidar_fraction is not None and lidar_fraction < 0.95:
        limitations.append(
            f"Only {lidar_fraction:.0%} of this zone is backed by 1 m LiDAR; the rest is "
            f"interpolated from a 30 m DEM and its slope and curvature are less reliable."
        )
    if mean_snow is None:
        limitations.append("No snow-depth index was available for this zone.")

    properties = {
        "zone_id": zone_id,
        "area_m2": round(area_m2, 1),
        "area_hectares": round(area_m2 / 10_000.0, 2),
        "mean_slope_deg": mean_slope,
        "max_slope_deg": max_slope,
        "dominant_aspect_deg": dominant_aspect,
        "dominant_aspect_compass": compass_name(dominant_aspect),
        "aspect_consistency": aspect_consistency,
        "elevation_min_m": round(float(elevations.min()), 1) if elevations.size else None,
        "elevation_max_m": round(float(elevations.max()), 1) if elevations.size else None,
        "elevation_mean_m": round(float(elevations.mean()), 1) if elevations.size else None,
        "mean_plan_curvature": mean_plan,
        "curvature_character": (
            "convergent (gully-like)" if (mean_plan or 0) < -0.2
            else "divergent (spur-like)" if (mean_plan or 0) > 0.2
            else "planar"
        ),
        "landcover_fractions": lc_counts,
        "forest_fraction": forest_fraction,
        "lidar_fraction": lidar_fraction,
        "terrain_susceptibility_score": mean_terrain,
        "dynamic_instability_score": mean_dynamic,
        "wind_loading_score": mean_wind,
        "snow_depth_index": mean_snow,
        "estimated_release_score": mean_release,
        "main_reasons": reasons,
        "limitations": limitations,
        "is_probability": False,
        "score_type": "estimated_release_score (0-100 relative index)",
    }

    return ReleaseZone(
        zone_id=zone_id,
        geometry=geometry_wgs84,
        geometry_utm=geometry_utm,
        properties=properties,
        pixels=pixels,
    )


def _polygon_area(geometry: dict[str, Any]) -> float:
    """Shoelace area of the exterior ring. Used only to pick the largest ring."""
    coordinates = geometry.get("coordinates") or []
    if not coordinates:
        return 0.0
    ring = coordinates[0]
    total = 0.0
    for index in range(len(ring) - 1):
        x1, y1 = ring[index][0], ring[index][1]
        x2, y2 = ring[index + 1][0], ring[index + 1][1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _reasons(
    *,
    mean_slope: float,
    max_slope: float,
    dominant_aspect: float | None,
    mean_wind: float | None,
    mean_dynamic: float | None,
    mean_plan: float | None,
    forest_fraction: float,
    elevation_range: tuple[float, float] | None,
    config: ModelConfig,
) -> list[str]:
    """Plain-language explanation of why this zone scored what it scored."""
    reasons: list[str] = []
    low = float(config.get("terrain.slope.optimal_low_deg", 34.0))
    high = float(config.get("terrain.slope.optimal_high_deg", 45.0))

    compass = compass_name(dominant_aspect)
    if low <= mean_slope <= high:
        reasons.append(
            f"Mean slope of {mean_slope:.0f} degrees sits in the {low:.0f}-{high:.0f} degree band "
            f"where slab avalanches are most common."
        )
    elif mean_slope > high:
        reasons.append(
            f"Steep terrain at {mean_slope:.0f} degrees (up to {max_slope:.0f}); steep enough to "
            f"release, though very steep slopes tend to sluff rather than build deep slabs."
        )
    else:
        reasons.append(f"Moderate slope of {mean_slope:.0f} degrees, at the lower end of the release band.")

    if compass:
        reasons.append(f"Predominantly {compass}-facing terrain ({dominant_aspect:.0f} degrees).")

    if mean_wind is not None and mean_wind > 40:
        reasons.append(
            f"Strong modelled wind loading (score {mean_wind:.0f}/100): the wind is depositing snow "
            f"onto this aspect."
        )
    elif mean_wind is not None and mean_wind > 15:
        reasons.append(f"Moderate modelled wind loading (score {mean_wind:.0f}/100).")

    if mean_plan is not None and mean_plan < -0.2:
        reasons.append(
            "Convergent, gully-like cross-slope shape: moving snow is channelled and deepens rather "
            "than spreading out."
        )

    if forest_fraction < 0.15:
        reasons.append("Open terrain with little forest anchoring.")
    elif forest_fraction > 0.5:
        reasons.append(
            f"{forest_fraction:.0%} forested; standing timber partially anchors the snowpack here."
        )

    if elevation_range:
        reasons.append(
            f"Spans {elevation_range[0]:.0f}-{elevation_range[1]:.0f} m elevation."
        )

    if mean_dynamic is not None and mean_dynamic > 55:
        reasons.append(
            f"Current conditions score {mean_dynamic:.0f}/100 for instability, raising the terrain "
            f"score substantially."
        )
    elif mean_dynamic is not None and mean_dynamic < 25:
        reasons.append(
            f"Current conditions are relatively benign (instability {mean_dynamic:.0f}/100); the "
            f"score here is driven mainly by the terrain itself."
        )

    return reasons
