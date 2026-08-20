"""Deterministic evolving-snow state from hourly meteorological forcing.

What this is
------------
An hourly forward integration of a small set of **named, bounded state
variables** that avalanche-forecasting practice actually uses, evaluated per
raster cell so that elevation and terrain enter the forcing rather than a single
scalar standing in for a whole mountain block:

``new_snow_index``
    Depth of recent new snow that has not yet lost its identity as a storm slab,
    accumulated hourly and relaxed toward zero with an exponential settlement
    time. It generalises the operational 1-day/3-day new-snow sums (HN24/HN72)
    that the Swiss danger scale is built around -- Schweizer, Jamieson &
    Schneebeli (2003, *Rev. Geophys.* 41, 1016) review the ~30-50 cm three-day
    new-snow range associated with widespread dry-slab activity -- while
    remaining well defined over a storm window longer than three days, where a
    fixed-length sum either truncates or double counts.

``drift_index``
    Accumulated drifting-snow potential. Blowing-snow transport rate scales with
    the cube of shear velocity above a threshold (Pomeroy & Gray, 1990), and the
    10 m threshold wind speed for dry loose snow is about 7.7 m/s, rising to
    about 9.9 m/s once the surface is wet (Li & Pomeroy, 1997,
    *J. Geophys. Res.* 102, 21955). The index sums ``max(0, u - u_threshold)^3``
    over hours when transportable snow is actually present, then relaxes with the
    same settlement time. This is the term a 72-hour *mean* wind speed destroys:
    averaging a storm's windy hours with its calm ones can put the mean below any
    transport threshold even when many individual hours were far above it.

``rain_on_snow`` and ``positive_degree_hours``
    Liquid-water input to an existing snow cover, and the degree-hour sum above
    freezing. Both are wetting indicators for the wet-snow regimes.

``antecedent_snow_depth`` and ``peak_snow_depth``
    The pre-storm base and the maximum depth reached. Terrain with no snow
    cannot produce a wet-snow release. These modelled surface-depth fields do
    not reveal liquid water at the snow/ground interface and therefore cannot
    support a full-depth/glide release calculation.

``buried_weak_interface_proxy``
    **This is a proxy, not a weak layer.** It counts pre-storm hours that were
    simultaneously cold, calm, and precipitation-free -- the surface conditions
    under which near-surface faceting and surface-hoar growth are favoured
    (Birkeland, 1998, *Arctic Alpine Res.* 30, 193; Föhn, 2001) -- and reports a
    bounded 0-1 index only when the storm then buried that surface. It contains
    no snow-profile observation, no stability test, no grain type, and no
    measurement of any actual buried layer. It cannot be verified from the data
    that produces it, and it must never be described as weak-layer physics.

Conventions and refusals
------------------------
* The rain/snow phase classification is applied **exactly once**, here, to the
  total precipitation series, using per-cell temperature. A provider's own
  already-classified snowfall series is never also consumed; doing both would
  count phase twice.
* Wind direction stays in the meteorological FROM convention throughout, and the
  drift-weighted mean direction is a circular mean of FROM bearings weighted by
  hourly drift, not a mean of the scalar bearings.
* Every state variable is a relative deterministic index or a physical
  accumulation with stated units. None is a probability, and none is calibrated
  against an observed avalanche.
* Missing forcing is rejected upstream by :mod:`avycore.snowpack.forcing`; this
  module never sees, and never invents, a gap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .forcing import ForcingSampleGrid

__all__ = [
    "SnowState",
    "integrate_snow_state",
    "snowpack_parameter_manifest",
    "NEW_SNOW_DENSITY_KG_M3",
    "NEW_SNOW_SETTLEMENT_TIME_H",
    "DRIFT_THRESHOLD_DRY_MS",
    "DRIFT_THRESHOLD_WET_MS",
    "TEMPERATURE_LAPSE_C_PER_KM",
]

# =============================================================================
# Named constants. Every one is an UNCALIBRATED literature or practice value.
# None is fitted to an observed avalanche in this project.
# =============================================================================

#: Fresh-snow bulk density used to convert precipitation water equivalent into a
#: new-snow depth. 100 kg/m^3 is the round value inside the commonly reported
#: 70-120 kg/m^3 range for freshly fallen alpine snow, and it makes 1 mm water
#: equivalent equal 1.0 cm of new snow. Providers that publish a "snowfall in cm"
#: field often assume a denser 143 kg/m^3 (7 cm per 10 mm); that series is kept
#: as a diagnostic and never mixed with this one.
NEW_SNOW_DENSITY_KG_M3 = 100.0

#: Exponential relaxation time of the new-snow index, in hours. 48 h gives the
#: index an effective memory close to the operational three-day new-snow sum
#: while decaying smoothly instead of dropping a whole day at a window edge.
NEW_SNOW_SETTLEMENT_TIME_H = 48.0

#: Same relaxation applied to accumulated drift, since a wind slab loses its
#: identity on a comparable timescale.
DRIFT_SETTLEMENT_TIME_H = 48.0

#: 10 m threshold wind speeds for blowing-snow transport (Li & Pomeroy 1997).
DRIFT_THRESHOLD_DRY_MS = 7.7
DRIFT_THRESHOLD_WET_MS = 9.9

#: Transport needs loose snow to move. Below this much recent new snow the
#: surface is treated as unavailable for transport.
DRIFT_AVAILABLE_MIN_NEW_SNOW_CM = 0.5

#: Reference drift accumulation that saturates the normalised drift term, in
#: (m/s)^3 hours. A storm holding 10 m/s for 24 hours over available snow gives
#: (10 - 7.7)^3 * 24 = 292; the reference is set an order above a single such
#: burst so that only sustained, strong transport saturates the term.
DRIFT_INDEX_FULL_M3_S3_H = 2000.0

#: Dry-adiabatic-ish environmental lapse rate used to transfer sample-point air
#: temperature to cell elevation. Same 6.5 K/km the project's M2 forcing work
#: uses. It is a standard value, not a locally validated one.
TEMPERATURE_LAPSE_C_PER_KM = 6.5

#: Rain/snow classification band, identical to
#: :mod:`avycore.hazard.conditions`. A classification, not a phase observation.
RAIN_SNOW_LOWER_C = 0.0
RAIN_SNOW_UPPER_C = 2.0

#: Snow must already be on the ground for rain to be "rain on snow".
SNOW_PRESENT_DEPTH_M = 0.05

#: Pre-storm surface-weakening conditions: cold, calm, and dry.
WEAKNESS_MAX_TEMPERATURE_C = -3.0
WEAKNESS_MAX_WIND_KMH = 10.0
WEAKNESS_MAX_PRECIPITATION_MM = 0.05
#: Hours of such conditions that saturate the proxy.
WEAKNESS_FULL_HOURS = 120.0
#: The proxy stays zero unless the storm actually buried the weakened surface.
WEAKNESS_BURIAL_MIN_NEW_SNOW_CM = 10.0


def snowpack_parameter_manifest() -> dict[str, Any]:
    """Every tunable that influences the evolving snow state."""

    return {
        "new_snow_density_kg_m3": NEW_SNOW_DENSITY_KG_M3,
        "new_snow_settlement_time_h": NEW_SNOW_SETTLEMENT_TIME_H,
        "drift_settlement_time_h": DRIFT_SETTLEMENT_TIME_H,
        "drift_threshold_dry_ms": DRIFT_THRESHOLD_DRY_MS,
        "drift_threshold_wet_ms": DRIFT_THRESHOLD_WET_MS,
        "drift_available_min_new_snow_cm": DRIFT_AVAILABLE_MIN_NEW_SNOW_CM,
        "drift_index_full_m3_s3_h": DRIFT_INDEX_FULL_M3_S3_H,
        "temperature_lapse_c_per_km": TEMPERATURE_LAPSE_C_PER_KM,
        "rain_snow_lower_c": RAIN_SNOW_LOWER_C,
        "rain_snow_upper_c": RAIN_SNOW_UPPER_C,
        "snow_present_depth_m": SNOW_PRESENT_DEPTH_M,
        "weakness_max_temperature_c": WEAKNESS_MAX_TEMPERATURE_C,
        "weakness_max_wind_kmh": WEAKNESS_MAX_WIND_KMH,
        "weakness_max_precipitation_mm": WEAKNESS_MAX_PRECIPITATION_MM,
        "weakness_full_hours": WEAKNESS_FULL_HOURS,
        "weakness_burial_min_new_snow_cm": WEAKNESS_BURIAL_MIN_NEW_SNOW_CM,
    }


@dataclass(frozen=True)
class SnowState:
    """Per-cell evolving-snow state, sampled at each cell's own peak-loading hour.

    ``new_snow_index_cm`` and ``drift_index`` are reported **co-temporally**: for
    every cell the integrator records the hour at which the normalised sum of the
    two is largest and stores that hour's state. Reporting each variable's own
    independent maximum would describe a loading that never simultaneously
    existed. The independent maxima are still published as diagnostics.

    Every array shares the raster shape. ``mask`` is True where the state is
    unknown -- outside the supported domain or missing a required terrain input.
    A masked cell is unknown, never a calm, snow-free, safe-looking zero.
    """

    new_snow_index_cm: np.ndarray
    drift_index: np.ndarray
    drift_index_normalized: np.ndarray
    drift_from_direction_deg: np.ndarray
    peak_hour_index: np.ndarray
    storm_new_snow_cm: np.ndarray
    storm_rain_mm: np.ndarray
    rain_on_snow_mm: np.ndarray
    positive_degree_hours: np.ndarray
    antecedent_snow_depth_m: np.ndarray
    peak_snow_depth_m: np.ndarray
    peak_temperature_c: np.ndarray
    mean_storm_temperature_c: np.ndarray
    minimum_storm_temperature_c: np.ndarray
    buried_weak_interface_proxy: np.ndarray
    independent_peak_new_snow_index_cm: np.ndarray
    independent_peak_drift_index: np.ndarray
    mask: np.ndarray
    metadata: dict[str, Any]

    @property
    def shape(self) -> tuple[int, ...]:
        return self.new_snow_index_cm.shape

    def summary(self) -> dict[str, Any]:
        """Reportable aggregates over the unmasked cells."""

        valid = ~self.mask
        if not valid.any():
            return {"valid_cell_count": 0, **self.metadata}

        def stats(name: str, values: np.ndarray) -> dict[str, float]:
            selected = np.asarray(values)[valid]
            return {
                f"{name}_mean": float(selected.mean()),
                f"{name}_minimum": float(selected.min()),
                f"{name}_maximum": float(selected.max()),
            }

        payload: dict[str, Any] = {
            "valid_cell_count": int(np.count_nonzero(valid)),
            **stats("new_snow_index_cm", self.new_snow_index_cm),
            **stats("drift_index", self.drift_index),
            **stats("drift_index_normalized", self.drift_index_normalized),
            **stats("storm_new_snow_cm", self.storm_new_snow_cm),
            **stats("rain_on_snow_mm", self.rain_on_snow_mm),
            **stats("positive_degree_hours", self.positive_degree_hours),
            **stats("antecedent_snow_depth_m", self.antecedent_snow_depth_m),
            **stats("buried_weak_interface_proxy", self.buried_weak_interface_proxy),
            **stats("mean_storm_temperature_c", self.mean_storm_temperature_c),
        }
        payload.update(self.metadata)
        return payload


def _snow_fraction(temperature_c: np.ndarray) -> np.ndarray:
    """Fraction of precipitation classified as snow. Applied exactly once."""

    span = RAIN_SNOW_UPPER_C - RAIN_SNOW_LOWER_C
    return np.clip((RAIN_SNOW_UPPER_C - temperature_c) / span, 0.0, 1.0)


def integrate_snow_state(
    samples: ForcingSampleGrid,
    *,
    elevation_m: np.ma.MaskedArray | np.ndarray,
    sample_index: np.ndarray,
    storm_start_exclusive_utc: str,
    supported: np.ndarray | None = None,
) -> SnowState:
    """Integrate the hourly snow state over a raster.

    ``samples`` must span the antecedent period *and* the storm window on a
    single hourly timeline. Hours at or before ``storm_start_exclusive_utc``
    build only the pre-storm weakening counter and the antecedent snow depth;
    hours after it drive loading. Splitting the series this way keeps the
    weakening proxy strictly pre-storm, so it can never be inflated by the storm
    it is supposed to precede.

    ``sample_index`` maps every raster cell to its nearest forcing sample. It is
    produced by :meth:`ForcingSampleGrid.nearest_sample_index` and carries the
    piecewise-constant footprint of the source grid; this function adds no
    interpolation of its own.

    ``supported`` optionally restricts the domain (for example to a fixed
    evaluation core). Cells outside it, and cells with missing elevation, are
    masked as unknown.
    """

    times = samples.times_utc
    if storm_start_exclusive_utc >= times[-1]:
        raise ValueError("The storm window must contain at least one forcing hour.")

    elevation = np.ma.asarray(elevation_m)
    shape = elevation.shape
    if np.shape(sample_index) != shape:
        raise ValueError("sample_index must match the elevation raster shape.")
    index = np.asarray(sample_index, dtype=np.intp)
    if index.min() < 0 or index.max() >= samples.sample_count:
        raise ValueError("sample_index refers to a sample that does not exist.")

    mask = np.ma.getmaskarray(elevation).copy()
    if supported is not None:
        support = np.asarray(supported, dtype=bool)
        if support.shape != shape:
            raise ValueError("supported must match the elevation raster shape.")
        mask |= ~support
    height = np.asarray(elevation.filled(0.0), dtype="float64")

    temperature_matrix = samples.stack("air_temperature_c")
    precipitation_matrix = samples.stack("precipitation_mm")
    wind_matrix = samples.stack("wind_speed_10m_kmh")
    direction_matrix = samples.stack("wind_from_direction_deg")
    has_depth = samples.has("snow_depth_m")
    depth_matrix = samples.stack("snow_depth_m") if has_depth else None
    sample_elevation = np.asarray(
        [forcing.sample_elevation_m for forcing in samples.forcings], dtype="float64"
    )
    #: Height of every cell above the elevation its forcing sample reports.
    lapse_offset_c = (
        (height - sample_elevation[index]) / 1000.0
    ) * TEMPERATURE_LAPSE_C_PER_KM

    zeros = np.zeros(shape, dtype="float64")
    new_snow_index = zeros.copy()
    drift_index = zeros.copy()
    drift_east = zeros.copy()
    drift_north = zeros.copy()
    storm_new_snow = zeros.copy()
    storm_rain = zeros.copy()
    rain_on_snow = zeros.copy()
    positive_degree_hours = zeros.copy()
    peak_snow_depth = zeros.copy()
    weakness_hours = zeros.copy()
    temperature_sum = zeros.copy()
    minimum_temperature = np.full(shape, np.inf, dtype="float64")
    independent_peak_new_snow = zeros.copy()
    independent_peak_drift = zeros.copy()
    antecedent_depth = zeros.copy()
    antecedent_depth_seen = False

    best_combined = np.full(shape, -1.0, dtype="float64")
    snapshot_new_snow = zeros.copy()
    snapshot_drift = zeros.copy()
    snapshot_east = zeros.copy()
    snapshot_north = zeros.copy()
    snapshot_temperature = zeros.copy()
    snapshot_hour = np.zeros(shape, dtype="int32")

    new_snow_decay = math.exp(-1.0 / NEW_SNOW_SETTLEMENT_TIME_H)
    drift_decay = math.exp(-1.0 / DRIFT_SETTLEMENT_TIME_H)
    depth_to_cm = 100.0 / NEW_SNOW_DENSITY_KG_M3
    storm_hour_count = 0

    for hour, timestamp in enumerate(times):
        temperature = temperature_matrix[index, hour] - lapse_offset_c
        precipitation = precipitation_matrix[index, hour]
        wind_kmh = wind_matrix[index, hour]
        wind_ms = wind_kmh / 3.6
        depth_m = depth_matrix[index, hour] if has_depth else None

        in_storm = timestamp > storm_start_exclusive_utc
        if not in_storm:
            quiet = (
                (temperature <= WEAKNESS_MAX_TEMPERATURE_C)
                & (wind_kmh <= WEAKNESS_MAX_WIND_KMH)
                & (precipitation <= WEAKNESS_MAX_PRECIPITATION_MM)
            )
            weakness_hours += quiet
            if depth_m is not None:
                antecedent_depth = depth_m.astype("float64", copy=True)
                peak_snow_depth = np.maximum(peak_snow_depth, depth_m)
                antecedent_depth_seen = True
            continue

        storm_hour_count += 1
        snow_fraction = _snow_fraction(temperature)
        new_snow_cm = precipitation * snow_fraction * depth_to_cm
        rain_mm = precipitation * (1.0 - snow_fraction)

        new_snow_index = new_snow_index * new_snow_decay + new_snow_cm
        storm_new_snow += new_snow_cm
        storm_rain += rain_mm

        if depth_m is not None:
            snow_present = depth_m >= SNOW_PRESENT_DEPTH_M
            peak_snow_depth = np.maximum(peak_snow_depth, depth_m)
        else:
            snow_present = new_snow_index >= (SNOW_PRESENT_DEPTH_M * 100.0)
        rain_on_snow += rain_mm * snow_present
        positive_degree_hours += np.maximum(temperature, 0.0)
        temperature_sum += temperature
        minimum_temperature = np.minimum(minimum_temperature, temperature)

        threshold_ms = np.where(
            temperature > 0.0, DRIFT_THRESHOLD_WET_MS, DRIFT_THRESHOLD_DRY_MS
        )
        transportable = (
            new_snow_index >= DRIFT_AVAILABLE_MIN_NEW_SNOW_CM
        ) & (temperature <= 0.0)
        excess = np.maximum(wind_ms - threshold_ms, 0.0)
        drift_hourly = (excess * excess * excess) * transportable
        drift_index = drift_index * drift_decay + drift_hourly
        bearing = np.deg2rad(direction_matrix[index, hour])
        # Decay the directional components with the scalar drift state. Without
        # this, an early transporting hour retains full directional weight after
        # its contribution to ``drift_index`` has settled away.
        drift_east = drift_east * drift_decay + drift_hourly * np.sin(bearing)
        drift_north = drift_north * drift_decay + drift_hourly * np.cos(bearing)

        independent_peak_new_snow = np.maximum(independent_peak_new_snow, new_snow_index)
        independent_peak_drift = np.maximum(independent_peak_drift, drift_index)

        combined = np.minimum(new_snow_index / 50.0, 1.0) + np.minimum(
            drift_index / DRIFT_INDEX_FULL_M3_S3_H, 1.0
        )
        improved = combined > best_combined
        best_combined = np.where(improved, combined, best_combined)
        snapshot_new_snow = np.where(improved, new_snow_index, snapshot_new_snow)
        snapshot_drift = np.where(improved, drift_index, snapshot_drift)
        snapshot_east = np.where(improved, drift_east, snapshot_east)
        snapshot_north = np.where(improved, drift_north, snapshot_north)
        snapshot_temperature = np.where(improved, temperature, snapshot_temperature)
        snapshot_hour = np.where(improved, np.int32(hour), snapshot_hour)

    if storm_hour_count == 0:
        raise ValueError("The storm window contained no forcing hours.")

    magnitude = np.hypot(snapshot_east, snapshot_north)
    #: With no drift at all there is no drift-weighted direction. Reporting an
    #: arbitrary bearing would look like a measurement, so the direction is set
    #: to the sentinel -1 and the regime terms must treat it as "no loading
    #: direction", which they do because the drift magnitude is then zero.
    drift_direction = np.where(
        magnitude > 0.0,
        (np.degrees(np.arctan2(snapshot_east, snapshot_north)) + 360.0) % 360.0,
        -1.0,
    )
    weak_proxy = np.clip(weakness_hours / WEAKNESS_FULL_HOURS, 0.0, 1.0) * (
        independent_peak_new_snow >= WEAKNESS_BURIAL_MIN_NEW_SNOW_CM
    )
    mean_temperature = temperature_sum / float(storm_hour_count)
    minimum_temperature = np.where(
        np.isfinite(minimum_temperature), minimum_temperature, 0.0
    )

    def finish(values: np.ndarray, dtype: str = "float32") -> np.ndarray:
        result = np.asarray(values, dtype=dtype)
        return np.where(mask, 0 if dtype.startswith("int") else 0.0, result).astype(dtype)

    antecedent_hour_count = len(times) - storm_hour_count
    metadata = {
        "schema": "avycore-snow-state-v1",
        "hour_count_total": len(times),
        "hour_count_antecedent": antecedent_hour_count,
        "hour_count_storm": storm_hour_count,
        "antecedent_start_utc": times[0],
        "storm_start_exclusive_utc": storm_start_exclusive_utc,
        "storm_end_inclusive_utc": times[-1],
        "snow_depth_available": bool(has_depth),
        "antecedent_snow_depth_available": bool(antecedent_depth_seen),
        "phase_classification_applications": 1,
        "phase_classification_note": (
            "Rain/snow phase is applied once, to total precipitation, using "
            "lapse-transferred cell temperature. A provider's own pre-classified "
            "snowfall series is never additionally consumed."
        ),
        "wind_direction_convention": "meteorological_from_degrees_clockwise_from_north",
        "drift_direction_semantics": (
            "Drift-weighted circular mean of hourly wind-FROM bearings up to the "
            "cell's peak-loading hour; -1 means no drift occurred and there is no "
            "loading direction."
        ),
        "weak_interface_semantics": (
            "PROXY ONLY. Counts pre-storm cold, calm, precipitation-free hours and "
            "requires subsequent burial. It observes no weak layer, grain type, or "
            "stability test and cannot be verified from these data."
        ),
        "co_temporal_snapshot": (
            "new_snow_index_cm and drift_index are reported at each cell's own "
            "peak-combined-loading hour so they describe a loading that existed "
            "simultaneously."
        ),
        "parameters": snowpack_parameter_manifest(),
        "forcing": samples.summary(),
    }

    return SnowState(
        new_snow_index_cm=finish(snapshot_new_snow),
        drift_index=finish(snapshot_drift),
        drift_index_normalized=finish(
            np.clip(snapshot_drift / DRIFT_INDEX_FULL_M3_S3_H, 0.0, 1.0)
        ),
        drift_from_direction_deg=finish(drift_direction),
        peak_hour_index=finish(snapshot_hour, dtype="int32"),
        storm_new_snow_cm=finish(storm_new_snow),
        storm_rain_mm=finish(storm_rain),
        rain_on_snow_mm=finish(rain_on_snow),
        positive_degree_hours=finish(positive_degree_hours),
        antecedent_snow_depth_m=finish(antecedent_depth),
        peak_snow_depth_m=finish(peak_snow_depth),
        peak_temperature_c=finish(snapshot_temperature),
        mean_storm_temperature_c=finish(mean_temperature),
        minimum_storm_temperature_c=finish(minimum_temperature),
        buried_weak_interface_proxy=finish(weak_proxy),
        independent_peak_new_snow_index_cm=finish(independent_peak_new_snow),
        independent_peak_drift_index=finish(independent_peak_drift),
        mask=mask,
        metadata=metadata,
    )
