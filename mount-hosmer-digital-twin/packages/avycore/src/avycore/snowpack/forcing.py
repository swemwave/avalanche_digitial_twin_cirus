"""Hourly meteorological forcing contract for the deterministic snow state.

This module owns the *units, conventions, and missing-value semantics* of every
series the evolving-snow integrator consumes. It deliberately refuses to accept
a non-finite value: a gap in required forcing is missing data, and the calling
experiment must stop rather than silently substitute zero, a climatology, or an
interpolated guess.

Conventions fixed here and relied on everywhere downstream:

* ``air_temperature_c`` is an **instantaneous** 2 m air temperature in degrees
  Celsius at the sample point.
* ``precipitation_mm`` is a **preceding-hour sum** of total precipitation in
  millimetres of water equivalent, i.e. the bin labelled ``t`` covers
  ``(t - 1 h, t]``. It is the *total* of all phases; the rain/snow split is
  applied exactly once, downstream, by :mod:`avycore.snowpack.state`.
* ``provider_snowfall_cm`` is the provider's own already-phase-classified
  snowfall depth, retained **as a cross-check diagnostic only**. Feeding it into
  the loading terms alongside a second temperature classification would double
  count phase, so the integrator never consumes it.
* ``wind_speed_10m_kmh`` is an instantaneous scalar 10 m wind speed.
* ``wind_from_direction_deg`` is the **meteorological FROM convention**: the
  compass bearing, degrees clockwise from north, that the wind blows *from*.
  0/360 is a northerly. This is the same convention as
  :class:`avycore.hazard.conditions.Conditions`.
* ``snow_depth_m`` is an instantaneous total snow depth in metres, or ``None``
  when the product does not supply one. ``None`` means unknown, never zero.
* ``shortwave_radiation_w_m2`` is a preceding-hour **mean** global horizontal
  irradiance in W/m^2, or ``None`` when unavailable. W/m^2 is never silently
  equated with an accumulated MJ/m^2.

Nothing here is a measurement claim. A gridded reanalysis value is a model
estimate for a grid cell of the stated size, not an observation at the sample
coordinate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Sequence

import numpy as np

__all__ = [
    "HourlyForcing",
    "ForcingSampleGrid",
    "MissingForcingError",
]


class MissingForcingError(ValueError):
    """Raised when a required forcing value is absent or non-finite.

    Deliberately a hard error. The project's I3 invariant forbids converting a
    missing required input into a safe-looking number, and a silently filled
    storm hour would do exactly that.
    """


def _require_series(
    values: Any,
    *,
    name: str,
    length: int,
    minimum: float | None = None,
    maximum: float | None = None,
) -> np.ndarray:
    array = np.asarray(values, dtype="float64")
    if array.ndim != 1:
        raise MissingForcingError(f"{name} must be a one-dimensional series.")
    if array.size != length:
        raise MissingForcingError(
            f"{name} has {array.size} hours; the forcing declares {length}."
        )
    if not np.isfinite(array).all():
        missing = int(np.count_nonzero(~np.isfinite(array)))
        raise MissingForcingError(
            f"{name} contains {missing} missing or non-finite hours. Required "
            "forcing is never gap-filled; the affected block is incomplete."
        )
    if minimum is not None and float(array.min()) < minimum:
        raise MissingForcingError(f"{name} has a value below {minimum}.")
    if maximum is not None and float(array.max()) > maximum:
        raise MissingForcingError(f"{name} has a value above {maximum}.")
    return array


@dataclass(frozen=True)
class HourlyForcing:
    """One validated hourly series at one forcing sample point.

    ``sample_elevation_m`` is the terrain elevation the provider reports for the
    requested coordinate. It is **not** the reanalysis model's own orography,
    which these products do not expose through the retrieval API used here. Any
    elevation transfer built on it therefore assumes the reported value is
    representative of the height the grid-cell temperature describes, and that
    assumption is unquantified.
    """

    times_utc: tuple[str, ...]
    latitude_deg: float
    longitude_deg: float
    sample_elevation_m: float
    air_temperature_c: np.ndarray
    precipitation_mm: np.ndarray
    wind_speed_10m_kmh: np.ndarray
    wind_from_direction_deg: np.ndarray
    provider_snowfall_cm: np.ndarray | None = None
    snow_depth_m: np.ndarray | None = None
    shortwave_radiation_w_m2: np.ndarray | None = None

    def __post_init__(self) -> None:
        length = len(self.times_utc)
        if length < 2:
            raise MissingForcingError("A forcing series needs at least two hours.")
        if len(set(self.times_utc)) != length:
            raise MissingForcingError("Forcing timestamps must be unique.")
        if list(self.times_utc) != sorted(self.times_utc):
            raise MissingForcingError("Forcing timestamps must be ascending.")
        try:
            parsed_times = tuple(
                datetime.strptime(timestamp, "%Y-%m-%dT%H:%M")
                for timestamp in self.times_utc
            )
        except (TypeError, ValueError) as error:
            raise MissingForcingError(
                "Forcing timestamps must use the UTC form YYYY-MM-DDTHH:MM."
            ) from error
        if any(
            later - earlier != timedelta(hours=1)
            for earlier, later in zip(parsed_times, parsed_times[1:])
        ):
            raise MissingForcingError(
                "Forcing timestamps must be consecutive hourly UTC samples; gaps and "
                "sub-hourly steps are incomplete forcing, not values to interpolate."
            )
        for value, name in (
            (self.latitude_deg, "latitude_deg"),
            (self.longitude_deg, "longitude_deg"),
            (self.sample_elevation_m, "sample_elevation_m"),
        ):
            if not np.isfinite(value):
                raise MissingForcingError(f"{name} must be finite.")
        if not -90.0 <= float(self.latitude_deg) <= 90.0:
            raise MissingForcingError("latitude_deg must lie in [-90, 90].")
        if not -180.0 <= float(self.longitude_deg) <= 360.0:
            raise MissingForcingError("longitude_deg must lie in [-180, 360].")

        object.__setattr__(
            self,
            "air_temperature_c",
            _require_series(
                self.air_temperature_c,
                name="air_temperature_c",
                length=length,
                minimum=-90.0,
                maximum=60.0,
            ),
        )
        object.__setattr__(
            self,
            "precipitation_mm",
            _require_series(
                self.precipitation_mm,
                name="precipitation_mm",
                length=length,
                minimum=0.0,
            ),
        )
        object.__setattr__(
            self,
            "wind_speed_10m_kmh",
            _require_series(
                self.wind_speed_10m_kmh,
                name="wind_speed_10m_kmh",
                length=length,
                minimum=0.0,
            ),
        )
        direction = _require_series(
            self.wind_from_direction_deg,
            name="wind_from_direction_deg",
            length=length,
            minimum=-360.0,
            maximum=720.0,
        )
        object.__setattr__(self, "wind_from_direction_deg", direction % 360.0)
        for name, minimum in (
            ("provider_snowfall_cm", 0.0),
            ("snow_depth_m", 0.0),
            ("shortwave_radiation_w_m2", 0.0),
        ):
            optional = getattr(self, name)
            if optional is None:
                continue
            object.__setattr__(
                self,
                name,
                _require_series(optional, name=name, length=length, minimum=minimum),
            )

    @property
    def hour_count(self) -> int:
        return len(self.times_utc)

    def window(self, start_exclusive_utc: str, end_inclusive_utc: str) -> "HourlyForcing":
        """Return the ``(start, end]`` sub-series.

        The half-open convention matches preceding-hour accumulation bins: the
        bin labelled ``t`` describes the hour that *ended* at ``t``, so a window
        beginning at ``start`` must exclude the bin labelled ``start`` itself.
        """

        selected = [
            index
            for index, timestamp in enumerate(self.times_utc)
            if start_exclusive_utc < timestamp <= end_inclusive_utc
        ]
        if not selected:
            raise MissingForcingError(
                f"No forcing hours fall in ({start_exclusive_utc}, {end_inclusive_utc}]."
            )
        index = np.asarray(selected, dtype=np.intp)

        def take(values: np.ndarray | None) -> np.ndarray | None:
            return None if values is None else np.asarray(values)[index]

        return HourlyForcing(
            times_utc=tuple(self.times_utc[position] for position in selected),
            latitude_deg=self.latitude_deg,
            longitude_deg=self.longitude_deg,
            sample_elevation_m=self.sample_elevation_m,
            air_temperature_c=self.air_temperature_c[index],
            precipitation_mm=self.precipitation_mm[index],
            wind_speed_10m_kmh=self.wind_speed_10m_kmh[index],
            wind_from_direction_deg=self.wind_from_direction_deg[index],
            provider_snowfall_cm=take(self.provider_snowfall_cm),
            snow_depth_m=take(self.snow_depth_m),
            shortwave_radiation_w_m2=take(self.shortwave_radiation_w_m2),
        )

    def summary(self) -> dict[str, Any]:
        """Reportable aggregates. Diagnostics, not model inputs."""

        payload: dict[str, Any] = {
            "hour_count": self.hour_count,
            "start_utc": self.times_utc[0],
            "end_utc": self.times_utc[-1],
            "latitude_deg": float(self.latitude_deg),
            "longitude_deg": float(self.longitude_deg),
            "sample_elevation_m": float(self.sample_elevation_m),
            "air_temperature_c_mean": float(self.air_temperature_c.mean()),
            "air_temperature_c_minimum": float(self.air_temperature_c.min()),
            "air_temperature_c_maximum": float(self.air_temperature_c.max()),
            "precipitation_mm_total": float(self.precipitation_mm.sum()),
            "wind_speed_10m_kmh_mean": float(self.wind_speed_10m_kmh.mean()),
            "wind_speed_10m_kmh_maximum": float(self.wind_speed_10m_kmh.max()),
        }
        if self.provider_snowfall_cm is not None:
            payload["provider_snowfall_cm_total"] = float(
                self.provider_snowfall_cm.sum()
            )
        if self.snow_depth_m is not None:
            payload["snow_depth_m_first"] = float(self.snow_depth_m[0])
            payload["snow_depth_m_last"] = float(self.snow_depth_m[-1])
            payload["snow_depth_m_maximum"] = float(self.snow_depth_m.max())
        if self.shortwave_radiation_w_m2 is not None:
            payload["shortwave_radiation_w_m2_mean"] = float(
                self.shortwave_radiation_w_m2.mean()
            )
        return payload


@dataclass(frozen=True)
class ForcingSampleGrid:
    """A set of sample points and the nearest-sample assignment for a raster.

    The assignment is deliberately **nearest neighbour**, not bilinear. A
    reanalysis value describes a grid cell of its own native size; smoothly
    interpolating it across a 30 m raster would draw a gradient the product does
    not contain and would make the forcing *look* finer than it is. Nearest
    assignment keeps the piecewise-constant footprint of the native cells
    visible, so the delivered spatial detail is honestly the sample spacing.
    """

    sample_easting_m: np.ndarray
    sample_northing_m: np.ndarray
    forcings: tuple[HourlyForcing, ...]
    crs: str

    def __post_init__(self) -> None:
        easting = np.asarray(self.sample_easting_m, dtype="float64")
        northing = np.asarray(self.sample_northing_m, dtype="float64")
        if easting.shape != northing.shape or easting.ndim != 1:
            raise MissingForcingError("Sample coordinates must be aligned 1-D arrays.")
        if easting.size != len(self.forcings):
            raise MissingForcingError(
                "Every sample coordinate needs exactly one forcing series."
            )
        if easting.size == 0:
            raise MissingForcingError("At least one forcing sample is required.")
        if not (np.isfinite(easting).all() and np.isfinite(northing).all()):
            raise MissingForcingError("Sample coordinates must be finite.")
        hours = {forcing.times_utc for forcing in self.forcings}
        if len(hours) != 1:
            raise MissingForcingError(
                "Every forcing sample must share an identical hourly timeline."
            )
        object.__setattr__(self, "sample_easting_m", easting)
        object.__setattr__(self, "sample_northing_m", northing)

    @property
    def times_utc(self) -> tuple[str, ...]:
        return self.forcings[0].times_utc

    @property
    def sample_count(self) -> int:
        return int(self.sample_easting_m.size)

    def nearest_sample_index(
        self, easting_m: np.ndarray, northing_m: np.ndarray
    ) -> np.ndarray:
        """Index of the nearest sample for every raster cell centre.

        Ties break toward the lowest sample index so the assignment is
        reproducible on every machine.
        """

        east = np.asarray(easting_m, dtype="float64")
        north = np.asarray(northing_m, dtype="float64")
        if east.shape != north.shape:
            raise MissingForcingError("Cell coordinate arrays must have equal shapes.")
        best_index = np.zeros(east.shape, dtype=np.int32)
        best_distance = np.full(east.shape, np.inf, dtype="float64")
        for index in range(self.sample_count):
            delta_east = east - self.sample_easting_m[index]
            delta_north = north - self.sample_northing_m[index]
            distance = delta_east * delta_east + delta_north * delta_north
            closer = distance < best_distance
            best_distance = np.where(closer, distance, best_distance)
            best_index = np.where(closer, np.int32(index), best_index)
        return best_index

    def stack(self, attribute: str) -> np.ndarray:
        """Sample-major ``(sample, hour)`` matrix for one forcing attribute."""

        series: list[np.ndarray] = []
        for forcing in self.forcings:
            values = getattr(forcing, attribute)
            if values is None:
                raise MissingForcingError(
                    f"Forcing attribute {attribute!r} is unavailable for this product; "
                    "it is unknown, not zero."
                )
            series.append(np.asarray(values, dtype="float64"))
        return np.vstack(series)

    def has(self, attribute: str) -> bool:
        return all(getattr(forcing, attribute) is not None for forcing in self.forcings)

    def window(
        self, start_exclusive_utc: str, end_inclusive_utc: str
    ) -> "ForcingSampleGrid":
        return ForcingSampleGrid(
            sample_easting_m=self.sample_easting_m,
            sample_northing_m=self.sample_northing_m,
            forcings=tuple(
                forcing.window(start_exclusive_utc, end_inclusive_utc)
                for forcing in self.forcings
            ),
            crs=self.crs,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "crs": self.crs,
            "sample_count": self.sample_count,
            "hour_count": len(self.times_utc),
            "start_utc": self.times_utc[0],
            "end_utc": self.times_utc[-1],
            "spatial_assignment": "nearest_sample_no_interpolation",
            "spatial_assignment_note": (
                "Nearest-sample assignment preserves the piecewise-constant footprint of "
                "the source grid. Resampling a coarse product onto a fine raster adds no "
                "spatial information."
            ),
            "samples": [forcing.summary() for forcing in self.forcings],
        }


def sample_lattice(
    *,
    west_m: float,
    south_m: float,
    east_m: float,
    north_m: float,
    count_per_axis: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Evenly spaced interior sample coordinates for a projected bounding box.

    Points sit at ``(2k + 1) / (2 n)`` of each side, i.e. at the centres of an
    ``n x n`` partition, so no sample lands exactly on a block edge and the
    lattice is symmetric. Row-major ordering (north to south, west to east)
    fixes the sample index for reproducibility.
    """

    if count_per_axis < 1:
        raise ValueError("count_per_axis must be at least 1.")
    if not (east_m > west_m and north_m > south_m):
        raise ValueError("Sample lattice bounds must be positively oriented.")
    fractions = (np.arange(count_per_axis, dtype="float64") * 2.0 + 1.0) / (
        2.0 * count_per_axis
    )
    eastings = west_m + fractions * (east_m - west_m)
    northings = north_m - fractions * (north_m - south_m)
    grid_north, grid_east = np.meshgrid(northings, eastings, indexing="ij")
    return grid_east.reshape(-1), grid_north.reshape(-1)
