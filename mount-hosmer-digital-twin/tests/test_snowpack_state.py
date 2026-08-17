"""Characterization tests for the evolving snow state and its forcing contract.

These are software-verification tests on controlled inputs. They establish that
the implemented equations, units, conventions and refusals behave as documented.
They establish nothing about agreement with avalanches in the field.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from avycore.snowpack import solar
from avycore.snowpack.forcing import (
    ForcingSampleGrid,
    HourlyForcing,
    MissingForcingError,
    sample_lattice,
)
from avycore.snowpack.state import (
    DRIFT_INDEX_FULL_M3_S3_H,
    DRIFT_THRESHOLD_DRY_MS,
    NEW_SNOW_DENSITY_KG_M3,
    NEW_SNOW_SETTLEMENT_TIME_H,
    TEMPERATURE_LAPSE_C_PER_KM,
    integrate_snow_state,
)

SCIENTIFIC_USE = "software_verification"


#: A label strictly before the first generated hour. ``storm_start`` is
#: *exclusive*, matching the preceding-hour accumulation convention, so passing
#: this makes every generated hour a storm hour.
BEFORE_FIRST_HOUR = "2019-01-01T23:00"


def _hours(count: int, *, start_day: int = 2, start_hour: int = 0) -> tuple[str, ...]:
    stamps = []
    day, hour = start_day, start_hour
    for _ in range(count):
        stamps.append(f"2019-01-{day:02d}T{hour:02d}:00")
        hour += 1
        if hour == 24:
            hour = 0
            day += 1
    return tuple(stamps)


def _forcing(
    times: tuple[str, ...],
    *,
    temperature: float | np.ndarray = -5.0,
    precipitation: float | np.ndarray = 0.0,
    wind_kmh: float | np.ndarray = 0.0,
    wind_from: float | np.ndarray = 270.0,
    snow_depth: float | np.ndarray | None = None,
    elevation: float = 2000.0,
    latitude: float = 46.8,
    longitude: float = 9.8,
) -> HourlyForcing:
    count = len(times)

    def series(value):
        return np.full(count, value, dtype="float64") if np.isscalar(value) else np.asarray(value, dtype="float64")

    return HourlyForcing(
        times_utc=times,
        latitude_deg=latitude,
        longitude_deg=longitude,
        sample_elevation_m=elevation,
        air_temperature_c=series(temperature),
        precipitation_mm=series(precipitation),
        wind_speed_10m_kmh=series(wind_kmh),
        wind_from_direction_deg=series(wind_from),
        snow_depth_m=None if snow_depth is None else series(snow_depth),
    )


def _single_sample_grid(forcing: HourlyForcing) -> ForcingSampleGrid:
    return ForcingSampleGrid(
        sample_easting_m=np.array([0.0]),
        sample_northing_m=np.array([0.0]),
        forcings=(forcing,),
        crs="EPSG:2056",
    )


def _uniform_state(forcing: HourlyForcing, *, storm_start: str, elevation_m: float = 2000.0):
    grid = _single_sample_grid(forcing)
    elevation = np.ma.array(np.full((3, 3), elevation_m, dtype="float64"), mask=False)
    return integrate_snow_state(
        grid,
        elevation_m=elevation,
        sample_index=np.zeros((3, 3), dtype=np.intp),
        storm_start_exclusive_utc=storm_start,
    )


# ---------------------------------------------------------------------------
# Forcing contract
# ---------------------------------------------------------------------------


def test_missing_forcing_hour_is_rejected_not_filled():
    times = _hours(6)
    values = np.zeros(6)
    values[3] = np.nan
    with pytest.raises(MissingForcingError, match="missing or non-finite"):
        _forcing(times, precipitation=values)


def test_forcing_requires_ascending_unique_timestamps():
    with pytest.raises(MissingForcingError, match="ascending"):
        _forcing(("2019-01-01T02:00", "2019-01-01T01:00"))
    with pytest.raises(MissingForcingError, match="unique"):
        _forcing(("2019-01-01T01:00", "2019-01-01T01:00"))


def test_forcing_rejects_a_timestamp_gap_instead_of_interpolating_it():
    with pytest.raises(MissingForcingError, match="consecutive hourly"):
        _forcing(("2019-01-01T01:00", "2019-01-01T03:00"))


def test_forcing_window_is_start_exclusive_end_inclusive():
    times = _hours(6)
    forcing = _forcing(times, precipitation=np.arange(6, dtype="float64"))
    window = forcing.window(times[1], times[4])
    assert window.times_utc == times[2:5]
    assert window.precipitation_mm.tolist() == [2.0, 3.0, 4.0]


def test_wind_direction_is_normalised_into_the_from_convention():
    forcing = _forcing(_hours(3), wind_from=np.array([-90.0, 450.0, 360.0]))
    assert forcing.wind_from_direction_deg.tolist() == [270.0, 90.0, 0.0]


def test_unavailable_optional_series_is_unknown_not_zero():
    grid = _single_sample_grid(_forcing(_hours(4)))
    assert grid.has("snow_depth_m") is False
    with pytest.raises(MissingForcingError, match="unknown, not zero"):
        grid.stack("snow_depth_m")


def test_sample_lattice_is_interior_and_row_major():
    east, north = sample_lattice(
        west_m=0.0, south_m=0.0, east_m=100.0, north_m=100.0, count_per_axis=2
    )
    assert east.tolist() == [25.0, 75.0, 25.0, 75.0]
    assert north.tolist() == [75.0, 75.0, 25.0, 25.0]


def test_nearest_sample_assignment_is_piecewise_constant_and_tie_stable():
    grid = ForcingSampleGrid(
        sample_easting_m=np.array([0.0, 10.0]),
        sample_northing_m=np.array([0.0, 0.0]),
        forcings=(_forcing(_hours(3)), _forcing(_hours(3))),
        crs="EPSG:2056",
    )
    east = np.array([[0.0, 4.0, 5.0, 6.0, 10.0]])
    north = np.zeros_like(east)
    index = grid.nearest_sample_index(east, north)
    # The exact midpoint ties and must resolve to the lowest sample index.
    assert index.tolist() == [[0, 0, 0, 1, 1]]


# ---------------------------------------------------------------------------
# Accumulation, settlement and phase
# ---------------------------------------------------------------------------


def test_new_snow_index_matches_the_analytic_settlement_steady_state():
    """Constant hourly input relaxes to h / (1 - exp(-1/tau))."""

    count = 400
    hours = _hours(count)
    forcing = _forcing(hours, precipitation=1.0, temperature=-8.0)
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    decay = math.exp(-1.0 / NEW_SNOW_SETTLEMENT_TIME_H)
    # 1 mm/h water equivalent is 1.0 cm/h of new snow at 100 kg/m^3, so the
    # closed form of the geometric series is exact, transient included.
    analytic_after_n = (1.0 - decay**count) / (1.0 - decay)
    steady_state = 1.0 / (1.0 - decay)
    observed = float(state.independent_peak_new_snow_index_cm[0, 0])
    assert observed == pytest.approx(analytic_after_n, rel=1e-6)
    assert observed < steady_state


def test_settlement_decays_an_undisturbed_index_exponentially():
    hours = _hours(50)
    precipitation = np.zeros(50)
    precipitation[0] = 10.0
    forcing = _forcing(hours, precipitation=precipitation, temperature=-8.0)
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    # The peak is at the first hour; the co-temporal snapshot therefore records
    # the undecayed value, while the storm total stays at the deposited amount.
    assert float(state.independent_peak_new_snow_index_cm[0, 0]) == pytest.approx(10.0)
    assert float(state.storm_new_snow_cm[0, 0]) == pytest.approx(10.0)
    assert float(state.new_snow_index_cm[0, 0]) == pytest.approx(10.0)


def test_precipitation_phase_is_applied_exactly_once_and_conserves_water():
    hours = _hours(4)
    forcing = _forcing(hours, precipitation=10.0, temperature=1.0)
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    # At 1 degC the 0-2 degC band classifies half the precipitation as snow.
    assert float(state.storm_new_snow_cm[0, 0]) == pytest.approx(4 * 10.0 * 0.5)
    assert float(state.storm_rain_mm[0, 0]) == pytest.approx(4 * 10.0 * 0.5)
    # Convert depth back to water equivalent explicitly. At the fixed 100 kg/m3
    # fresh-snow density the numerical cm and mm values happen to be equal, but
    # they are not the same unit.
    snow_water_equivalent_mm = (
        float(state.storm_new_snow_cm[0, 0]) * NEW_SNOW_DENSITY_KG_M3 / 100.0
    )
    water_equivalent = snow_water_equivalent_mm + float(state.storm_rain_mm[0, 0])
    assert water_equivalent == pytest.approx(40.0)
    assert state.metadata["phase_classification_applications"] == 1


def test_lapse_transfer_cools_higher_cells_at_the_documented_rate():
    hours = _hours(4)
    forcing = _forcing(hours, temperature=1.0, precipitation=10.0, elevation=2000.0)
    grid = _single_sample_grid(forcing)
    elevation = np.ma.array(np.array([[2000.0, 3000.0]]), mask=False)
    state = integrate_snow_state(
        grid,
        elevation_m=elevation,
        sample_index=np.zeros((1, 2), dtype=np.intp),
        storm_start_exclusive_utc=BEFORE_FIRST_HOUR,
    )
    low, high = state.mean_storm_temperature_c[0]
    assert float(low) == pytest.approx(1.0, abs=1e-4)
    assert float(high) == pytest.approx(1.0 - TEMPERATURE_LAPSE_C_PER_KM, abs=1e-4)
    # The colder, higher cell therefore banks all the precipitation as snow.
    assert float(state.storm_new_snow_cm[0, 1]) == pytest.approx(40.0)
    assert float(state.storm_new_snow_cm[0, 0]) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Drifting snow
# ---------------------------------------------------------------------------


def test_drift_is_zero_below_the_transport_threshold():
    hours = _hours(48)
    below = (DRIFT_THRESHOLD_DRY_MS - 0.1) * 3.6
    forcing = _forcing(hours, precipitation=1.0, temperature=-8.0, wind_kmh=below)
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    assert float(state.independent_peak_drift_index[0, 0]) == 0.0
    assert float(state.drift_index_normalized[0, 0]) == 0.0
    assert float(state.drift_from_direction_deg[0, 0]) == -1.0


def test_drift_accumulates_as_the_cube_of_the_excess_wind_speed():
    hours = _hours(3)
    speed_ms = DRIFT_THRESHOLD_DRY_MS + 2.0
    forcing = _forcing(
        hours, precipitation=5.0, temperature=-8.0, wind_kmh=speed_ms * 3.6
    )
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    decay = math.exp(-1.0 / NEW_SNOW_SETTLEMENT_TIME_H)
    # Snow falling within an hourly bin is available for transport in that same
    # bin -- deposition and wind are concurrent inside the hour -- so all three
    # hours contribute (2 m/s excess cubed = 8), each decayed once per later hour.
    expected = 8.0 * (decay**2 + decay + 1.0)
    assert float(state.independent_peak_drift_index[0, 0]) == pytest.approx(
        expected, rel=1e-6
    )


def test_drift_needs_transportable_snow_and_a_cold_surface():
    hours = _hours(24)
    strong = (DRIFT_THRESHOLD_DRY_MS + 5.0) * 3.6
    dry_no_snow = _forcing(hours, precipitation=0.0, temperature=-8.0, wind_kmh=strong)
    assert float(_uniform_state(dry_no_snow, storm_start=BEFORE_FIRST_HOUR).independent_peak_drift_index[0, 0]) == 0.0
    warm_with_snow = _forcing(hours, precipitation=5.0, temperature=3.0, wind_kmh=strong)
    assert float(_uniform_state(warm_with_snow, storm_start=BEFORE_FIRST_HOUR).independent_peak_drift_index[0, 0]) == 0.0


def test_drift_direction_is_the_drift_weighted_circular_mean_of_from_bearings():
    """One strong hour from the west must dominate many calm hours from the east.

    A plain arithmetic mean of the hourly bearings would land near the calm
    easterly; the drift weighting is exactly what keeps a storm's transporting
    hours from being averaged away.
    """

    hours = _hours(24)
    wind = np.full(24, 5.0)
    wind[12] = (DRIFT_THRESHOLD_DRY_MS + 6.0) * 3.6
    bearings = np.full(24, 90.0)
    bearings[12] = 270.0
    forcing = _forcing(
        hours, precipitation=2.0, temperature=-8.0, wind_kmh=wind, wind_from=bearings
    )
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    assert float(state.drift_from_direction_deg[0, 0]) == pytest.approx(270.0, abs=1e-6)
    # The unweighted mean of the same bearings sits within 8 degrees of the calm
    # easterly, which is the loading direction the previous scalar aggregation
    # would have handed the release model.
    assert float(np.mean(bearings)) == pytest.approx(97.5)


def test_drift_normalisation_saturates_at_the_documented_reference():
    hours = _hours(200)
    forcing = _forcing(
        hours,
        precipitation=2.0,
        temperature=-10.0,
        wind_kmh=(DRIFT_THRESHOLD_DRY_MS + 15.0) * 3.6,
    )
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    assert float(state.independent_peak_drift_index[0, 0]) > DRIFT_INDEX_FULL_M3_S3_H
    assert float(state.drift_index_normalized[0, 0]) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Wetting, antecedent depth and the weak-interface proxy
# ---------------------------------------------------------------------------


def test_rain_counts_as_rain_on_snow_only_where_snow_exists():
    hours = _hours(4)
    without = _forcing(hours, precipitation=10.0, temperature=5.0, snow_depth=0.0)
    with_snow = _forcing(hours, precipitation=10.0, temperature=5.0, snow_depth=1.5)
    assert float(_uniform_state(without, storm_start=BEFORE_FIRST_HOUR).rain_on_snow_mm[0, 0]) == 0.0
    assert float(
        _uniform_state(with_snow, storm_start=BEFORE_FIRST_HOUR).rain_on_snow_mm[0, 0]
    ) == pytest.approx(40.0)


def test_positive_degree_hours_only_count_above_freezing():
    hours = _hours(4)
    temperatures = np.array([-3.0, 0.0, 2.0, 5.0])
    state = _uniform_state(
        _forcing(hours, temperature=temperatures), storm_start=BEFORE_FIRST_HOUR
    )
    assert float(state.positive_degree_hours[0, 0]) == pytest.approx(7.0)


def test_antecedent_depth_is_taken_before_the_storm_window():
    hours = _hours(10)
    depth = np.array([1.0, 1.1, 1.2, 1.3, 1.4, 2.5, 3.0, 3.4, 3.8, 4.0])
    state = _uniform_state(
        _forcing(hours, snow_depth=depth, temperature=-6.0), storm_start=hours[4]
    )
    assert float(state.antecedent_snow_depth_m[0, 0]) == pytest.approx(1.4)
    assert float(state.peak_snow_depth_m[0, 0]) == pytest.approx(4.0)
    assert state.metadata["hour_count_antecedent"] == 5
    assert state.metadata["hour_count_storm"] == 5


def test_storm_only_depth_is_not_mislabelled_as_antecedent_depth():
    hours = _hours(4)
    state = _uniform_state(
        _forcing(hours, snow_depth=np.array([1.0, 1.2, 1.4, 1.6])),
        storm_start=BEFORE_FIRST_HOUR,
    )
    assert state.metadata["antecedent_snow_depth_available"] is False
    assert float(state.antecedent_snow_depth_m[0, 0]) == 0.0
    assert float(state.peak_snow_depth_m[0, 0]) == pytest.approx(1.6)


def test_weak_interface_proxy_counts_only_pre_storm_hours_and_requires_burial():
    quiet = _hours(200)
    # 150 cold, calm, dry pre-storm hours, then a burying storm.
    temperature = np.full(200, -10.0)
    precipitation = np.zeros(200)
    precipitation[150:] = 2.0
    forcing = _forcing(quiet, temperature=temperature, precipitation=precipitation)
    buried = _uniform_state(forcing, storm_start=quiet[149])
    assert float(buried.buried_weak_interface_proxy[0, 0]) == pytest.approx(1.0)

    # The same weather with no burying storm leaves the proxy at zero.
    unburied = _uniform_state(
        _forcing(quiet, temperature=temperature, precipitation=np.zeros(200)),
        storm_start=quiet[149],
    )
    assert float(unburied.buried_weak_interface_proxy[0, 0]) == 0.0

    # Storm hours must not be able to inflate the counter: making the whole
    # series the storm window removes every pre-storm hour.
    storm_only = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    assert float(storm_only.buried_weak_interface_proxy[0, 0]) == 0.0


def test_weak_interface_proxy_is_suppressed_by_wind_or_precipitation():
    hours = _hours(200)
    precipitation = np.zeros(200)
    precipitation[150:] = 2.0
    windy = _forcing(
        hours, temperature=-10.0, precipitation=precipitation, wind_kmh=40.0
    )
    assert float(
        _uniform_state(windy, storm_start=hours[149]).buried_weak_interface_proxy[0, 0]
    ) == 0.0


# ---------------------------------------------------------------------------
# Masks, co-temporality and determinism
# ---------------------------------------------------------------------------


def test_missing_elevation_stays_masked_and_never_becomes_a_calm_zero():
    hours = _hours(24)
    grid = _single_sample_grid(_forcing(hours, precipitation=3.0, temperature=-8.0))
    elevation = np.ma.array(
        np.array([[2000.0, 2000.0]]), mask=np.array([[False, True]])
    )
    state = integrate_snow_state(
        grid,
        elevation_m=elevation,
        sample_index=np.zeros((1, 2), dtype=np.intp),
        storm_start_exclusive_utc=BEFORE_FIRST_HOUR,
    )
    assert state.mask.tolist() == [[False, True]]
    assert float(state.new_snow_index_cm[0, 0]) > 0.0


def test_supported_domain_masks_cells_outside_it():
    hours = _hours(24)
    grid = _single_sample_grid(_forcing(hours, precipitation=3.0, temperature=-8.0))
    elevation = np.ma.array(np.full((1, 3), 2000.0), mask=False)
    state = integrate_snow_state(
        grid,
        elevation_m=elevation,
        sample_index=np.zeros((1, 3), dtype=np.intp),
        storm_start_exclusive_utc=BEFORE_FIRST_HOUR,
        supported=np.array([[True, False, True]]),
    )
    assert state.mask.tolist() == [[False, True, False]]


def test_snapshot_is_co_temporal_at_the_peak_combined_loading_hour():
    hours = _hours(60)
    precipitation = np.zeros(60)
    precipitation[:20] = 3.0
    wind = np.zeros(60)
    forcing = _forcing(
        hours, precipitation=precipitation, temperature=-8.0, wind_kmh=wind
    )
    state = _uniform_state(forcing, storm_start=BEFORE_FIRST_HOUR)
    peak_hour = int(state.peak_hour_index[0, 0])
    assert peak_hour == 19
    # The reported new-snow index is the value at that hour, not a later decayed
    # one, and the drift index is that same hour's value.
    assert float(state.new_snow_index_cm[0, 0]) == pytest.approx(
        float(state.independent_peak_new_snow_index_cm[0, 0])
    )
    assert float(state.drift_index[0, 0]) == 0.0


def test_integration_is_deterministic_for_identical_inputs():
    hours = _hours(72)
    forcing = _forcing(
        hours, precipitation=2.0, temperature=-6.0, wind_kmh=45.0, snow_depth=1.2
    )
    first = _uniform_state(forcing, storm_start=hours[11])
    second = _uniform_state(forcing, storm_start=hours[11])
    for name in (
        "new_snow_index_cm",
        "drift_index",
        "drift_from_direction_deg",
        "rain_on_snow_mm",
        "buried_weak_interface_proxy",
    ):
        np.testing.assert_array_equal(getattr(first, name), getattr(second, name))


def test_storm_window_must_contain_at_least_one_hour():
    hours = _hours(4)
    with pytest.raises(ValueError, match="at least one forcing hour"):
        _uniform_state(_forcing(hours), storm_start=hours[-1])


# ---------------------------------------------------------------------------
# Solar geometry
# ---------------------------------------------------------------------------


def test_solar_noon_altitude_matches_the_analytic_solstice_value():
    latitude = 46.8
    altitude, azimuth = solar.solar_position(
        ("1999-06-21T11:35",), latitude_deg=latitude, longitude_deg=9.8
    )
    assert float(altitude[0]) == pytest.approx(90.0 - latitude + 23.44, abs=0.2)
    assert float(azimuth[0]) == pytest.approx(180.0, abs=10.0)


def test_solar_azimuth_sweeps_eastward_to_westward_through_the_day():
    stamps = tuple(f"1999-02-20T{hour:02d}:00" for hour in range(7, 17))
    altitude, azimuth = solar.solar_position(
        stamps, latitude_deg=46.8, longitude_deg=9.8
    )
    assert np.all(np.diff(azimuth) > 0.0)
    assert float(azimuth[0]) < 180.0 < float(azimuth[-1])
    assert np.all(altitude > 0.0)


def test_cos_incidence_matches_analytic_tilted_plane_geometry():
    slope = np.array([30.0, 30.0, 0.0])
    aspect = np.array([180.0, 0.0, -1.0])
    values = solar.cos_incidence(
        slope_deg=slope,
        aspect_deg=aspect,
        solar_altitude_deg=30.0,
        solar_azimuth_deg=180.0,
    )
    assert float(values[0]) == pytest.approx(math.cos(math.radians(30.0)))
    assert float(values[1]) == 0.0  # facing away: no direct beam, never negative
    assert float(values[2]) == pytest.approx(math.sin(math.radians(30.0)))


def test_insolation_index_is_one_on_flat_ground_and_zero_in_geometric_shade():
    stamps = tuple(f"1999-02-20T{hour:02d}:00" for hour in range(24))
    radiation = np.zeros(24)
    radiation[8:16] = 400.0
    slope = np.array([0.0, 80.0])
    aspect = np.array([-1.0, 0.0])
    index = solar.insolation_index(
        slope_deg=slope,
        aspect_deg=aspect,
        timestamps_utc=stamps,
        shortwave_w_m2=radiation,
        latitude_deg=46.8,
        longitude_deg=9.8,
    )
    assert float(index[0]) == pytest.approx(1.0)
    assert float(index[1]) == pytest.approx(0.0, abs=1e-12)


def test_insolation_index_without_usable_radiation_is_neutral_not_shaded():
    stamps = tuple(f"1999-02-20T{hour:02d}:00" for hour in range(6))
    index = solar.insolation_index(
        slope_deg=np.array([35.0]),
        aspect_deg=np.array([180.0]),
        timestamps_utc=stamps,
        shortwave_w_m2=np.zeros(6),
        latitude_deg=46.8,
        longitude_deg=9.8,
    )
    assert float(index[0]) == 1.0
