"""Deterministic evolving-snow state and separate avalanche release regimes.

This subpackage adds three things the single-scalar dry-slab release model could
not express:

1. an hourly **evolving snow state** (accumulation, settlement, drifting-snow
   loading, wetting, antecedent depth, and an explicitly named buried-weak-
   interface *proxy*) evaluated per raster cell rather than as one number for a
   whole mountain;
2. **separate release regimes** -- dry slab, wet snow, dry loose, and full-depth
   glide -- each with its own terrain response and its own activation mask
   derived only from pre-event forcing; and
3. **slope-and-aspect-dependent forcing** through solar geometry and elevation
   transfer of air temperature.

Nothing here imports rasterio, pyproj, GDAL, xDEM, pandas, or GeoPandas, so it
remains importable in the serving process. It is research code for the frozen
validation experiments; the serving application's production release path in
:mod:`avycore.hazard.risk` is deliberately untouched, so previously frozen
experiments replay byte-for-byte.

Every number produced here is an uncalibrated relative index, never a
probability, a forecast, or a danger rating.
"""

from __future__ import annotations

from .forcing import (
    ForcingSampleGrid,
    HourlyForcing,
    MissingForcingError,
    sample_lattice,
)
from .regimes import (
    DRY_LOOSE,
    DRY_SLAB,
    FULL_DEPTH_GLIDE,
    REGIMES,
    RegimeReleaseField,
    RegimeScore,
    WET_SNOW,
    compute_regime_release,
    regime_parameter_manifest,
)
from .solar import cos_incidence, insolation_index, solar_position
from .state import (
    SnowState,
    integrate_snow_state,
    snowpack_parameter_manifest,
)
from .zones import (
    REGIME_EXTRACTION_RULES,
    RegimeExtractionRule,
    extract_regime_release_zones,
    segment_within_aspect_and_elevation,
)

__all__ = [
    "DRY_LOOSE",
    "DRY_SLAB",
    "FULL_DEPTH_GLIDE",
    "REGIMES",
    "REGIME_EXTRACTION_RULES",
    "ForcingSampleGrid",
    "HourlyForcing",
    "MissingForcingError",
    "RegimeExtractionRule",
    "RegimeReleaseField",
    "RegimeScore",
    "SnowState",
    "WET_SNOW",
    "compute_regime_release",
    "cos_incidence",
    "extract_regime_release_zones",
    "insolation_index",
    "integrate_snow_state",
    "regime_parameter_manifest",
    "sample_lattice",
    "segment_within_aspect_and_elevation",
    "snowpack_parameter_manifest",
    "solar_position",
]


def parameter_manifest() -> dict:
    """Complete tunable manifest for the snow state and the regime scores."""

    return {
        "snow_state": snowpack_parameter_manifest(),
        "release_regimes": regime_parameter_manifest(),
    }
