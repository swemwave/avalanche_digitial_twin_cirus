"""Deterministic solar geometry for slope-and-aspect-dependent forcing.

Only what the snow state needs: solar altitude and azimuth for a UTC hour at a
point, and the cosine of the incidence angle on a tilted surface. The
implementation follows the standard NOAA/Meeus low-precision solar-position
equations, which are accurate to roughly 0.01 degrees over the relevant date
range -- far finer than the terrain, forcing, or snow assumptions around them.

Two limitations travel with every number produced here:

* Solar position is evaluated at a single reference longitude/latitude per
  block. Over a 20 km block the true position varies by well under a quarter of
  a degree, which is negligible beside the reanalysis grid spacing, but it is an
  approximation rather than a per-cell computation.
* ``cos_incidence`` describes the geometry of a tilted plane. It contains no
  cast shadows, no horizon shading from neighbouring peaks, no sky-view factor,
  and no direct/diffuse partition. It is a relative geometric weight, not a
  radiation balance.
"""

from __future__ import annotations

import datetime as _dt
import math

import numpy as np

__all__ = [
    "solar_position",
    "cos_incidence",
    "insolation_index",
]

_ISO_FORMATS = ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def _parse_utc(timestamp: str) -> _dt.datetime:
    for pattern in _ISO_FORMATS:
        try:
            return _dt.datetime.strptime(timestamp, pattern).replace(
                tzinfo=_dt.timezone.utc
            )
        except ValueError:
            continue
    raise ValueError(f"Unrecognised UTC timestamp {timestamp!r}.")


def _julian_day(moment: _dt.datetime) -> float:
    year = moment.year
    month = moment.month
    day = (
        moment.day
        + (moment.hour + (moment.minute + moment.second / 60.0) / 60.0) / 24.0
    )
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return (
        math.floor(365.25 * (year + 4716))
        + math.floor(30.6001 * (month + 1))
        + day
        + b
        - 1524.5
    )


def solar_position(
    timestamps_utc: tuple[str, ...] | list[str],
    *,
    latitude_deg: float,
    longitude_deg: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Solar altitude and azimuth in degrees for each UTC timestamp.

    Altitude is measured up from the geometric horizon and may be negative at
    night. Azimuth uses the same convention as terrain aspect: degrees clockwise
    from north, so 180 is due south.
    """

    altitudes = np.empty(len(timestamps_utc), dtype="float64")
    azimuths = np.empty(len(timestamps_utc), dtype="float64")
    latitude = math.radians(float(latitude_deg))
    for index, timestamp in enumerate(timestamps_utc):
        julian_century = (_julian_day(_parse_utc(timestamp)) - 2451545.0) / 36525.0
        geometric_mean_longitude = math.radians(
            (280.46646 + julian_century * (36000.76983 + julian_century * 0.0003032))
            % 360.0
        )
        geometric_mean_anomaly = math.radians(
            357.52911 + julian_century * (35999.05029 - 0.0001537 * julian_century)
        )
        eccentricity = 0.016708634 - julian_century * (
            0.000042037 + 0.0000001267 * julian_century
        )
        equation_of_centre = math.radians(
            math.sin(geometric_mean_anomaly)
            * (1.914602 - julian_century * (0.004817 + 0.000014 * julian_century))
            + math.sin(2.0 * geometric_mean_anomaly)
            * (0.019993 - 0.000101 * julian_century)
            + math.sin(3.0 * geometric_mean_anomaly) * 0.000289
        )
        true_longitude = geometric_mean_longitude + equation_of_centre
        omega = math.radians(125.04 - 1934.136 * julian_century)
        apparent_longitude = true_longitude - math.radians(
            0.00569 + 0.00478 * math.sin(omega)
        )
        mean_obliquity = math.radians(
            23.0
            + (
                26.0
                + (
                    21.448
                    - julian_century
                    * (46.815 + julian_century * (0.00059 - julian_century * 0.001813))
                )
                / 60.0
            )
            / 60.0
        )
        obliquity = mean_obliquity + math.radians(0.00256 * math.cos(omega))
        declination = math.asin(math.sin(obliquity) * math.sin(apparent_longitude))

        variation = math.tan(obliquity / 2.0) ** 2
        equation_of_time = 4.0 * math.degrees(
            variation * math.sin(2.0 * geometric_mean_longitude)
            - 2.0 * eccentricity * math.sin(geometric_mean_anomaly)
            + 4.0
            * eccentricity
            * variation
            * math.sin(geometric_mean_anomaly)
            * math.cos(2.0 * geometric_mean_longitude)
            - 0.5 * variation * variation * math.sin(4.0 * geometric_mean_longitude)
            - 1.25
            * eccentricity
            * eccentricity
            * math.sin(2.0 * geometric_mean_anomaly)
        )
        moment = _parse_utc(timestamp)
        minutes_utc = moment.hour * 60.0 + moment.minute + moment.second / 60.0
        true_solar_time = (
            minutes_utc + equation_of_time + 4.0 * float(longitude_deg)
        ) % 1440.0
        hour_angle = math.radians(
            true_solar_time / 4.0 - 180.0
            if true_solar_time / 4.0 >= 0.0
            else true_solar_time / 4.0 + 180.0
        )
        cos_zenith = math.sin(latitude) * math.sin(declination) + math.cos(
            latitude
        ) * math.cos(declination) * math.cos(hour_angle)
        cos_zenith = max(-1.0, min(1.0, cos_zenith))
        zenith = math.acos(cos_zenith)
        altitudes[index] = 90.0 - math.degrees(zenith)
        sin_zenith = math.sin(zenith)
        if sin_zenith < 1e-9:
            azimuths[index] = 180.0
            continue
        cos_azimuth = (
            math.sin(latitude) * cos_zenith - math.sin(declination)
        ) / (math.cos(latitude) * sin_zenith)
        cos_azimuth = max(-1.0, min(1.0, cos_azimuth))
        # NOAA convention: this ``acos`` measures the angle away from due south,
        # so an afternoon hour angle folds it forward and a morning one folds it
        # back. Getting the two branches the wrong way round silently mirrors
        # every aspect east-west, which is why the sunrise/sunset regression test
        # checks the azimuth and not only the altitude.
        azimuth = math.degrees(math.acos(cos_azimuth))
        azimuths[index] = (
            (azimuth + 180.0) % 360.0
            if hour_angle > 0.0
            else (540.0 - azimuth) % 360.0
        )
    return altitudes, azimuths


def cos_incidence(
    *,
    slope_deg: np.ndarray,
    aspect_deg: np.ndarray,
    solar_altitude_deg: float,
    solar_azimuth_deg: float,
) -> np.ndarray:
    """Cosine of the sun's incidence angle on each tilted cell.

    ``aspect_deg`` uses the terrain convention: the downslope compass bearing,
    degrees clockwise from north. Cells with a negative aspect are flat or
    undefined and are treated as horizontal. Negative results are clipped to
    zero: a surface facing away from the sun receives no direct beam, it does
    not receive a negative one.
    """

    if solar_altitude_deg <= 0.0:
        return np.zeros(np.shape(slope_deg), dtype="float64")
    slope = np.deg2rad(np.clip(np.asarray(slope_deg, dtype="float64"), 0.0, 90.0))
    aspect_values = np.asarray(aspect_deg, dtype="float64")
    slope = np.where(aspect_values < 0.0, 0.0, slope)
    aspect = np.deg2rad(np.where(aspect_values < 0.0, 0.0, aspect_values))
    altitude = math.radians(float(solar_altitude_deg))
    azimuth = math.radians(float(solar_azimuth_deg))
    value = np.cos(slope) * math.sin(altitude) + np.sin(slope) * math.cos(
        altitude
    ) * np.cos(azimuth - aspect)
    return np.clip(value, 0.0, 1.0)


def insolation_index(
    *,
    slope_deg: np.ndarray,
    aspect_deg: np.ndarray,
    timestamps_utc: tuple[str, ...] | list[str],
    shortwave_w_m2: np.ndarray,
    latitude_deg: float,
    longitude_deg: float,
    minimum_altitude_deg: float = 3.0,
) -> np.ndarray:
    """Terrain insolation relative to a horizontal surface, dimensionless.

    ``sum_h SW_h * cos_incidence_h`` divided by ``sum_h SW_h * sin(altitude_h)``.
    Flat ground returns 1 by construction, a sun-facing slope exceeds 1, and a
    slope in geometric shade returns 0. Hours with the sun below
    ``minimum_altitude_deg`` are dropped from both sums because the tilted-plane
    geometry degenerates at grazing incidence.

    The result is a *relative geometric weight on the supplied global
    irradiance*, not an energy balance: the source series is global horizontal
    irradiance with no direct/diffuse split, and no horizon shading, sky-view
    factor, albedo, or longwave term is applied. If every hour is below the
    altitude cut-off the index is 1 everywhere, meaning "no usable geometric
    signal", never "no radiation".
    """

    radiation = np.asarray(shortwave_w_m2, dtype="float64")
    if radiation.shape != (len(timestamps_utc),):
        raise ValueError("shortwave_w_m2 must align with timestamps_utc.")
    altitudes, azimuths = solar_position(
        timestamps_utc, latitude_deg=latitude_deg, longitude_deg=longitude_deg
    )
    numerator = np.zeros(np.shape(slope_deg), dtype="float64")
    denominator = 0.0
    for index, altitude in enumerate(altitudes):
        energy = float(radiation[index])
        if altitude < minimum_altitude_deg or energy <= 0.0:
            continue
        numerator += energy * cos_incidence(
            slope_deg=slope_deg,
            aspect_deg=aspect_deg,
            solar_altitude_deg=float(altitude),
            solar_azimuth_deg=float(azimuths[index]),
        )
        denominator += energy * math.sin(math.radians(float(altitude)))
    if denominator <= 0.0:
        return np.ones(np.shape(slope_deg), dtype="float64")
    return numerator / denominator
