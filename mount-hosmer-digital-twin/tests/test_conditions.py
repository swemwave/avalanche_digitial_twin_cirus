from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.settings import Settings
from app.services.conditions import normalize_snow_current_frame, normalize_weather_frame, process_snow, strip_html


def test_weather_column_normalization_hourly() -> None:
    raw = pd.DataFrame(
        {
            "STATION_NAME": ["FERNIE"],
            "CLIMATE_IDENTIFIER": ["1152850"],
            "STN_ID": [1180],
            "UTC_DATE": ["2026-01-16T18:00:00"],
            "LOCAL_DATE": ["2026-01-16 11:00:00"],
            "TEMP": [-4.5],
            "PRECIP_AMOUNT": [2.0],
            "WIND_DIRECTION": [27],
            "WIND_SPEED": [18],
            "RELATIVE_HUMIDITY": [82],
            "latitude": [49.4887],
            "longitude": [-115.0733],
        }
    )
    normalized = normalize_weather_frame(raw, "hourly", "weather.csv")
    row = normalized.iloc[0]
    assert row["station_key"] == "1152850"
    assert row["air_temperature_c"] == -4.5
    assert row["precipitation_mm"] == 2.0
    assert row["wind_direction_degrees"] == 270
    assert row["wind_speed_kmh"] == 18


def test_snow_current_normalization_maps_station_specific_columns() -> None:
    morrissey = pd.DataFrame(
        {
            "Location ID": ["2C09Q"],
            "Location Name": ["Morrissey Ridge"],
            "Latitude": [49.447222],
            "Longitude": [-114.975],
            "Elevation": [1860],
            "DateTime": ["2026-07-03T22:00:00+00:00"],
            "SW": [12],
            "TA": [-2],
            "PC": [251],
        }
    )
    fernie = pd.DataFrame(
        {
            "Location ID": ["2C21P"],
            "Location Name": ["Fernie"],
            "Latitude": [49.48825],
            "Longitude": [-115.0726111],
            "Elevation": [988],
            "DateTime": ["2026-07-03T22:00:00+00:00"],
            "SD": [35],
            "TA": [-1],
            "PC": [217],
        }
    )
    morrissey_row = normalize_snow_current_frame(morrissey, "2C09Q", "2C09Q.csv").iloc[0]
    fernie_row = normalize_snow_current_frame(fernie, "2C21P", "2C21P.csv").iloc[0]
    assert morrissey_row["swe_mm"] == 12
    assert pd.isna(morrissey_row["snow_depth_cm"])
    assert fernie_row["snow_depth_cm"] == 35
    assert pd.isna(fernie_row["swe_mm"])


def test_process_snow_reports_missing_fernie_archive(tmp_path: Path) -> None:
    root = tmp_path / "mount_hosmer_data"
    station_dir = root / "dynamic" / "snow_bc" / "2C21P"
    station_dir.mkdir(parents=True)
    (station_dir / "2C21P_current_raw.csv").write_text(
        "Location ID,Location Name,Status,Latitude,Longitude,Elevation,DateTime,SD,TA,PC\n"
        "2C21P,Fernie,Active,49.48825,-115.0726111,988,2026-07-03T22:00:00+00:00,35,-1,217\n",
        encoding="utf-8",
    )
    settings = Settings(project_root=tmp_path, backend_root=tmp_path / "backend", runtime_root=tmp_path / "runtime", data_root=root)
    payload = process_snow(settings, force=True)
    assert payload["record_count"] == 1
    assert any("2C21P:archive" in warning for warning in payload["warnings"])
    assert (settings.runtime_root / "processed" / "dynamic" / "snow_stations_normalized.parquet").exists()


def test_strip_html_forecast_text() -> None:
    assert strip_html("<p>Regular <b>forecast</b></p>") == "Regular forecast"
