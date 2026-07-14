"""Spatial snow-condition model.

**Read this before using any number this module produces.**

The only snow instruments available for this mountain are two BC snow pillows.
The nearer one, Morrissey Ridge, is about 19 km away at 1860 m. The other, Fernie,
is in the valley at 988 m and has no historical archive. Neither is on Mount
Hosmer. There are no snow pits, no snow-depth transects, and no ridge-top
observations.

From that, it is not possible to state that there is 42 cm of snow on the
north face at 2100 m. Saying so would be a fabrication, and a dangerous one,
because the number would look like a measurement.

So this module produces **dimensionless indices from 0 to 1**, not centimetres and
not millimetres. An index of 0.8 means "much more than an index of 0.4 does", and
nothing more. Where a satellite actually saw the ground, the snow-presence mask is
`observed`; everything else is `modelled` or `interpolated`, and each pixel says
which in the accompanying provenance raster.

Two consequences worth stating explicitly:

* **Snow under trees is invisible to the satellite.** NDSI cannot see through a
  canopy. Low NDSI in forest means "we cannot tell", not "no snow". The model
  therefore does not let forest pixels drive snow *absence*.
* **An empty snow-station file is not a reading of zero.** It is a missing file.
  It is reported as unavailable and excluded from every average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core import provenance as prov
from app.core.model_config import ModelConfig
from app.processing.harmonization.grids import AnalysisGrid
from app.processing.weather.features import REFERENCE_ELEVATION_M, ConditionSet

#: Per-pixel provenance codes for the snow fields.
SNOW_PROVENANCE_CODES = {
    "observed_satellite": 1,   # a satellite actually saw snow or bare ground here
    "modelled": 2,             # inferred from weather + terrain
    "forest_unobservable": 3,  # under canopy; the satellite cannot see the ground
    "unavailable": 0,
}

SNOW_PROVENANCE_LABELS = {
    1: "Snow presence observed by satellite (NDSI)",
    2: "Snow condition modelled from interpolated weather and terrain",
    3: "Under forest canopy; snow presence cannot be observed from satellite",
    0: "No snow information available",
}


@dataclass
class SnowFields:
    snow_presence: np.ma.MaskedArray       # 0-1
    snow_depth_index: np.ma.MaskedArray    # 0-1, dimensionless
    swe_index: np.ma.MaskedArray           # 0-1, dimensionless
    new_snow_loading: np.ma.MaskedArray    # 0-1
    wet_snow_index: np.ma.MaskedArray      # 0-1
    melt_index: np.ma.MaskedArray          # 0-1
    freeze_thaw_index: np.ma.MaskedArray   # 0-1
    snow_persistence: np.ma.MaskedArray    # 0-1
    provenance_raster: np.ndarray          # uint8, SNOW_PROVENANCE_CODES
    available: bool
    confidence: float                      # 0-1
    warnings: list[str] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)
    inputs: list[prov.Value] = field(default_factory=list)

    def layers(self) -> dict[str, np.ma.MaskedArray]:
        return {
            "snow_presence": self.snow_presence,
            "snow_depth_index": self.snow_depth_index,
            "swe_index": self.swe_index,
            "snow_new_loading": self.new_snow_loading,
            "snow_wet_index": self.wet_snow_index,
            "snow_melt_index": self.melt_index,
            "snow_freeze_thaw_index": self.freeze_thaw_index,
            "snow_persistence": self.snow_persistence,
        }

    @property
    def transportable_snow(self) -> np.ma.MaskedArray:
        """How much snow the wind could actually pick up and move.

        Wet and refrozen snow does not blow. Dry new snow does. The wind model
        multiplies its whole field by this.
        """
        dry = 1.0 - np.clip(np.asarray(self.wet_snow_index.filled(0.0)), 0.0, 1.0)
        loose = np.clip(
            0.4 * np.asarray(self.snow_depth_index.filled(0.0))
            + 0.6 * np.asarray(self.new_snow_loading.filled(0.0)),
            0.0,
            1.0,
        )
        return np.ma.array(
            (loose * dry).astype("float32"), mask=np.ma.getmaskarray(self.snow_depth_index)
        )


def lapse_adjusted_temperature(
    surface_temp_c: float, station_elevation_m: float, elevation: np.ma.MaskedArray, lapse_c_per_km: float
) -> np.ma.MaskedArray:
    """Extrapolate a station temperature up the mountain by a fixed lapse rate.

    This is the single largest source of error in the snow model. The real lapse
    rate varies with humidity, and winter valley inversions routinely make the
    mountain *warmer* than the valley -- the exact opposite of what this assumes.
    We cannot detect an inversion with one valley station, so we cannot correct
    for one. The output is tagged `modelled` for this reason.
    """
    delta_km = (np.asarray(elevation.filled(station_elevation_m)) - station_elevation_m) / 1000.0
    return np.ma.array(
        (surface_temp_c + lapse_c_per_km * delta_km).astype("float32"),
        mask=np.ma.getmaskarray(elevation),
    )


def compute(
    *,
    config: ModelConfig,
    grid: AnalysisGrid,
    conditions: ConditionSet,
    elevation: np.ma.MaskedArray,
    slope: np.ma.MaskedArray,
    aspect: np.ma.MaskedArray,
    forest_mask: np.ma.MaskedArray,
    canopy_height: np.ma.MaskedArray | None = None,
    observed_ndsi: np.ma.MaskedArray | None = None,
    observed_cloud_mask: np.ma.MaskedArray | None = None,
) -> SnowFields:
    """Estimate snow conditions across the mountain."""
    mask = np.ma.getmaskarray(elevation)
    shape = grid.shape
    warnings: list[str] = list()
    inputs: list[prov.Value] = []

    zeros = np.ma.array(np.zeros(shape, dtype="float32"), mask=mask)
    snow_provenance = np.full(shape, SNOW_PROVENANCE_CODES["unavailable"], dtype="uint8")

    lapse = float(config.require("snow.lapse_rate_c_per_km"))
    ndsi_threshold = float(config.require("snow.ndsi_snow_threshold"))
    wet_temp = float(config.require("snow.wet_snow_surface_temp_c"))
    gradient = float(config.require("snow.precipitation_elevation_gradient_per_km"))

    station = conditions.station or {}
    station_elevation = float(station.get("elevation_m") or 1100.0)

    # --- Air temperature across the mountain ---------------------------------
    temp_value = conditions.get("temperature_mean_24h_c")
    if temp_value is None or not temp_value.is_available:
        warnings.append(
            "No air temperature was available, so snow phase, melt and wet-snow indices could not "
            "be estimated. They are reported as unavailable, not as zero."
        )
        temperature = None
    else:
        inputs.append(temp_value)
        temperature = lapse_adjusted_temperature(
            float(temp_value.numeric or 0.0), station_elevation, elevation, lapse
        )
        warnings.append(
            f"Air temperature was measured at {station.get('station_name', 'a distant station')} "
            f"({station_elevation:.0f} m) and extrapolated up the mountain at {lapse} K/km. Winter "
            f"valley inversions can invert this relationship and cannot be detected with one station."
        )

    # --- Snow presence --------------------------------------------------------
    # Where a satellite saw the ground, believe it. Where it did not, model it.
    presence = np.zeros(shape, dtype="float32")
    observed = np.zeros(shape, dtype=bool)

    if observed_ndsi is not None:
        ndsi = np.asarray(observed_ndsi.filled(np.nan))
        visible = ~np.ma.getmaskarray(observed_ndsi) & np.isfinite(ndsi)
        if observed_cloud_mask is not None:
            cloudy = np.asarray(observed_cloud_mask.filled(0.0)) > 0.5
            visible &= ~cloudy
            if cloudy.any():
                warnings.append(
                    f"{cloudy.mean():.0%} of the AOI was cloud covered in the satellite scene. "
                    f"Cloud is NEVER interpreted as snow; those pixels fall back to the model."
                )

        # A satellite cannot see the ground through a canopy. Low NDSI in forest
        # means "unknown", not "bare".
        canopy_limit = float(config.get("snow.forest_ndsi_unreliable_above_canopy_m", 3.0))
        if canopy_height is not None:
            under_canopy = np.asarray(canopy_height.filled(0.0)) >= canopy_limit
        else:
            under_canopy = np.asarray(forest_mask.filled(0.0)) > 0.5

        snow_seen = visible & (ndsi >= ndsi_threshold)
        ground_seen = visible & (ndsi < ndsi_threshold) & ~under_canopy

        presence[snow_seen] = 1.0
        observed |= snow_seen | ground_seen
        snow_provenance[snow_seen | ground_seen] = SNOW_PROVENANCE_CODES["observed_satellite"]
        snow_provenance[visible & under_canopy] = SNOW_PROVENANCE_CODES["forest_unobservable"]

        inputs.append(
            prov.observed(
                "satellite_snow_cover_fraction",
                round(float(snow_seen.sum()) / max(float(visible.sum()), 1.0), 4),
                "0-1",
                "Sentinel-2 / Landsat NDSI",
                conditions.valid_time_utc.isoformat(),
                "Fraction of cloud-free, non-forested pixels where NDSI indicates snow.",
            )
        )
        warnings.append(
            "Snow under forest canopy cannot be seen by satellite. Forested pixels with low NDSI "
            "are recorded as UNOBSERVABLE, not as snow-free."
        )
    else:
        inputs.append(
            prov.unavailable(
                "satellite_snow_cover_fraction",
                "0-1",
                "Sentinel-2 / Landsat NDSI",
                "No satellite scene was selected for this analysis.",
            )
        )

    # Model snow presence wherever the satellite could not resolve it: cold
    # enough to hold snow, and high enough.
    modelled_zone = ~observed & ~mask
    if temperature is not None:
        cold = np.asarray(temperature.filled(10.0)) < 1.0
        modelled_presence = np.clip(
            0.5 * cold.astype("float32")
            + 0.5 * np.clip((np.asarray(elevation.filled(0.0)) - 1200.0) / 900.0, 0.0, 1.0),
            0.0,
            1.0,
        )
        presence[modelled_zone] = modelled_presence[modelled_zone]
        snow_provenance[modelled_zone] = SNOW_PROVENANCE_CODES["modelled"]
    elif modelled_zone.any():
        warnings.append(
            "Snow presence could be neither observed nor modelled for part of the AOI; those "
            "pixels are unavailable."
        )

    # Steep rock sheds snow no matter the weather.
    steep = np.asarray(slope.filled(0.0)) > 65.0
    presence[steep] *= 0.2

    presence_field = np.ma.array(presence, mask=mask)

    # --- Snow depth and SWE indices (dimensionless) ---------------------------
    depth_value = conditions.get("snow_depth_index")
    swe_value = conditions.get("swe_index")

    if depth_value is not None and depth_value.is_available:
        base_depth = float(depth_value.numeric or 0.0)
        inputs.append(depth_value)
        depth_source = depth_value.provenance
    else:
        base_depth = None
        inputs.append(
            prov.unavailable(
                "snow_depth_index",
                "0-1",
                "BC snow stations",
                "No usable snow-depth observation. The current-season station files for "
                "2025-26 are empty; an empty file is NOT a reading of zero snow.",
            )
        )
        depth_source = "unavailable"

    if base_depth is None:
        # Fall back to modelling depth from elevation and snow presence. This is
        # a shape, not a measurement, and is tagged `modelled`.
        depth = np.clip(
            presence
            * np.clip((np.asarray(elevation.filled(0.0)) - 1100.0) / 1200.0, 0.0, 1.0)
            * (1.0 + gradient),
            0.0,
            1.0,
        ).astype("float32")
        warnings.append(
            "No snow-depth observation was available, so the snow-depth index is MODELLED from "
            "elevation and snow presence. It is a relative shape, not a measured depth, and it "
            "carries no units."
        )
        depth_provenance = "modelled"
    else:
        elevation_gain = np.clip(
            (np.asarray(elevation.filled(station_elevation)) - station_elevation) / 1000.0 * gradient,
            0.0,
            1.0,
        )
        depth = np.clip(presence * (base_depth + elevation_gain), 0.0, 1.0).astype("float32")
        depth_provenance = depth_source

    # Wind-scoured convexities hold less; leeward hollows hold more. That
    # redistribution is the wind model's job, and it reads this field.
    depth_field = np.ma.array(depth, mask=mask)

    if swe_value is not None and swe_value.is_available:
        inputs.append(swe_value)
        swe = np.clip(presence * float(swe_value.numeric or 0.0), 0.0, 1.0).astype("float32")
        swe_provenance = swe_value.provenance
    else:
        inputs.append(
            prov.unavailable(
                "swe_index",
                "0-1",
                "BC snow stations (Morrissey Ridge)",
                "No usable SWE observation for this period.",
            )
        )
        # SWE tracks depth but is denser where the snow is old and settled.
        swe = np.clip(depth * 0.9, 0.0, 1.0).astype("float32")
        swe_provenance = "modelled"
        warnings.append(
            "No SWE observation was available; the SWE index is MODELLED from the depth index. "
            "It is dimensionless and is not a measurement in millimetres."
        )
    swe_field = np.ma.array(swe, mask=mask)

    # --- New-snow loading -----------------------------------------------------
    new_snow = conditions.get("snowfall_72h_cm")
    snow_water = conditions.get("snow_water_equivalent_72h_mm")

    load_value: float | None = None
    load_provenance = "unavailable"
    if new_snow is not None and new_snow.is_available and (new_snow.numeric or 0.0) > 0:
        inputs.append(new_snow)
        # 50 cm in 72 h saturates the index. That is a large storm for this range.
        load_value = float(np.clip((new_snow.numeric or 0.0) / 50.0, 0.0, 1.0))
        load_provenance = new_snow.provenance
    elif snow_water is not None and snow_water.is_available:
        inputs.append(snow_water)
        # 40 mm of snow water equivalent in 72 h saturates.
        load_value = float(np.clip((snow_water.numeric or 0.0) / 40.0, 0.0, 1.0))
        load_provenance = "modelled"
    else:
        inputs.append(
            prov.unavailable(
                "new_snow_loading",
                "0-1",
                "ECCC weather",
                "Neither reported snowfall nor phase-partitioned snow water was available.",
            )
        )
        warnings.append(
            "New-snow loading could not be estimated. It is EXCLUDED from the instability score "
            "rather than scored as zero."
        )

    if load_value is None:
        loading_field = np.ma.array(np.zeros(shape, dtype="float32"), mask=np.ones(shape, dtype=bool))
    else:
        elevation_boost = 1.0 + np.clip(
            (np.asarray(elevation.filled(station_elevation)) - station_elevation) / 1000.0 * gradient,
            0.0,
            0.5,
        )
        loading = np.clip(load_value * elevation_boost * presence, 0.0, 1.0).astype("float32")
        loading_field = np.ma.array(loading, mask=mask)

    # --- Wet snow, melt, freeze-thaw -----------------------------------------
    if temperature is not None:
        temp_values = np.asarray(temperature.filled(-10.0))

        # Solar aspect matters: a south face in spring sun is a different snowpack
        # from a north face 100 m away.
        aspect_values = np.asarray(aspect.filled(-1.0))
        solar = np.where(
            aspect_values < 0,
            0.5,
            np.clip((np.cos(np.deg2rad(aspect_values - 180.0)) + 1.0) / 2.0, 0.0, 1.0),
        )
        solar_gain = 0.7 + 0.6 * solar  # south-facing gets up to ~1.3x the melt push

        wet = np.clip((temp_values - wet_temp) / 4.0, 0.0, 1.0) * solar_gain
        wet = np.clip(wet * presence, 0.0, 1.0).astype("float32")

        degree_day = float(config.require("snow.melt_degree_day_factor"))
        melt = np.clip(np.maximum(temp_values, 0.0) * degree_day / 30.0, 0.0, 1.0) * solar_gain
        melt = np.clip(melt * presence, 0.0, 1.0).astype("float32")

        wet_field = np.ma.array(wet, mask=mask)
        melt_field = np.ma.array(melt, mask=mask)
        thermal_available = True
    else:
        wet_field = np.ma.array(np.zeros(shape, "float32"), mask=np.ones(shape, dtype=bool))
        melt_field = np.ma.array(np.zeros(shape, "float32"), mask=np.ones(shape, dtype=bool))
        thermal_available = False

    cycles = conditions.get("freeze_thaw_cycles_24h")
    if cycles is not None and cycles.is_available:
        inputs.append(cycles)
        # Three or more crossings in a day is a strong melt-freeze signal.
        ft_value = float(np.clip((cycles.numeric or 0.0) / 3.0, 0.0, 1.0))
        ft_field = np.ma.array(
            np.clip(np.full(shape, ft_value, dtype="float32") * presence, 0.0, 1.0), mask=mask
        )
    else:
        inputs.append(
            prov.unavailable(
                "freeze_thaw_cycles_24h", "count", "ECCC weather", "No temperature record for the window."
            )
        )
        ft_field = np.ma.array(np.zeros(shape, "float32"), mask=np.ones(shape, dtype=bool))

    # --- Persistence ----------------------------------------------------------
    # How likely the snow here is to stick around: cold, shaded, high, sheltered.
    persistence = np.clip(
        0.45 * np.clip((np.asarray(elevation.filled(0.0)) - 1200.0) / 1200.0, 0.0, 1.0)
        + 0.35 * (1.0 - (np.asarray(aspect.filled(180.0)) >= 90) * (np.asarray(aspect.filled(180.0)) <= 270) * 0.8)
        + 0.20 * np.asarray(forest_mask.filled(0.0)),
        0.0,
        1.0,
    ).astype("float32")
    persistence_field = np.ma.array(persistence * presence, mask=mask)

    # --- Confidence -----------------------------------------------------------
    summary = prov.summarize(inputs)
    confidence = round(
        0.5 * summary["completeness"] + 0.5 * summary["mean_input_confidence"], 4
    )

    observable_fraction = float((snow_provenance == SNOW_PROVENANCE_CODES["observed_satellite"]).mean())
    # Snow presence is only "observed" where a satellite actually resolved the
    # ground. If most of the AOI was modelled, saying "observed" would overstate
    # what we know, so the dominant class is what gets reported downstream.
    presence_dominant = "observed" if observable_fraction >= 0.5 else "modelled"
    if observable_fraction < 0.2:
        warnings.append(
            f"Only {observable_fraction:.0%} of the AOI has satellite-observed snow presence. The "
            f"rest is modelled from interpolated weather."
        )

    explanation = {
        "units_note": (
            "Every snow output is a DIMENSIONLESS INDEX from 0 to 1. It is not centimetres and not "
            "millimetres. The available instruments -- two snow pillows, the nearest 19 km away at a "
            "different elevation -- cannot support a physical depth or SWE value for this mountain."
        ),
        "is_probability": False,
        "snow_presence_provenance": {
            SNOW_PROVENANCE_LABELS[code]: round(float((snow_provenance == code).mean()), 4)
            for code in sorted(set(int(value) for value in np.unique(snow_provenance)))
        },
        "snow_presence_dominant_provenance": presence_dominant,
        "snow_presence_observed_fraction": round(observable_fraction, 4),
        "depth_provenance": depth_provenance,
        "swe_provenance": swe_provenance,
        "new_snow_loading_provenance": load_provenance,
        "thermal_available": thermal_available,
        "confidence": confidence,
        "inputs": [value.to_dict() for value in inputs],
        "provenance_summary": summary,
        "limitations": [
            "No snow pit, snow profile, or snow-layer observation exists for Mount Hosmer. The "
            "model therefore has NO knowledge of buried weak layers or persistent slabs, which are "
            "the mechanism behind most fatal avalanches.",
            "The current-season (2025-26) snow-station files are empty. Empty files are treated as "
            "missing data, never as an observation of zero snow.",
            "Temperature is extrapolated from a valley station by a fixed lapse rate; winter "
            "inversions can invert that relationship and cannot be detected here.",
            "Snow beneath forest canopy cannot be observed by satellite and is recorded as "
            "unobservable, not as absent.",
        ],
        "warnings": warnings,
    }

    return SnowFields(
        snow_presence=presence_field,
        snow_depth_index=depth_field,
        swe_index=swe_field,
        new_snow_loading=loading_field,
        wet_snow_index=wet_field,
        melt_index=melt_field,
        freeze_thaw_index=ft_field,
        snow_persistence=persistence_field,
        provenance_raster=snow_provenance,
        available=bool(presence_field.count()),
        confidence=confidence,
        warnings=warnings,
        explanation=explanation,
        inputs=inputs,
    )
