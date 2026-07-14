from __future__ import annotations

from app.services.susceptibility import score_dynamic_conditions


def test_dynamic_condition_score_records_missing_inputs() -> None:
    event = {
        "event_id": "MH_20260430T182949Z",
        "summary": {
            "landsat_datetime_utc": "2026-04-30T18:29:49+00:00",
            "sensors": {
                "sentinel2": {"snow_cover_percent": 40.0},
                "landsat": {
                    "snow_cover_percent": 30.0,
                    "surface_temperature_c": {"mean": 2.0},
                },
            },
        },
    }
    weather = {
        "event_windows": {
            "MH_20260430T182949Z": {
                "windows": {
                    "previous_72_hours": {
                        "total_snowfall_cm": 12.0,
                        "total_precipitation_mm": 8.0,
                        "maximum_wind_speed_kmh": 35.0,
                        "maximum_gust_kmh": None,
                    },
                    "previous_24_hours": {"temperature_change_c": 3.0},
                }
            }
        },
        "warnings": [],
    }
    snow = {"event_windows": {"MH_20260430T182949Z": {"stations": {}}}, "warnings": ["bc_snow:2C21P:archive returned HTTP 404"]}
    forecast = {"highest_danger": {"display": "Summer Conditions"}, "valid_until_utc": "2026-10-01T23:00:00+00:00", "warnings": []}
    weights = {
        "dynamic": {
            "minimum_available_weight": 0.25,
            "recent_snowfall_weight": 0.18,
            "recent_precipitation_weight": 0.12,
            "swe_change_weight": 0.14,
            "snow_depth_change_weight": 0.10,
            "rapid_warming_weight": 0.14,
            "strong_wind_weight": 0.14,
            "satellite_snow_cover_weight": 0.12,
            "surface_temperature_weight": 0.06,
        }
    }
    result = score_dynamic_conditions(event=event, weather=weather, snow=snow, forecast=forecast, weights=weights)
    by_name = {component["component"]: component for component in result["components"]}
    assert result["score"] is not None
    assert result["available_weight_fraction"] == 0.76
    assert by_name["swe_change"]["missing_data"] is True
    assert by_name["snow_depth_change"]["missing_data"] is True
    assert by_name["recent_snowfall"]["normalized_value"] > 0
    assert by_name["avalanche_canada_current_forecast_context"]["status"] == "not_applicable_to_event"


def test_dynamic_condition_score_withholds_when_all_weighted_inputs_missing() -> None:
    event = {"event_id": "MH_20260430T182949Z", "summary": {"sensors": {}}}
    result = score_dynamic_conditions(
        event=event,
        weather={"event_windows": {}, "warnings": []},
        snow={"event_windows": {}, "warnings": []},
        forecast={"warnings": []},
        weights={"dynamic": {"minimum_available_weight": 0.25}},
    )
    assert result["score"] is None
    assert result["available_weight_fraction"] == 0
    assert any("No dynamic condition inputs" in warning for warning in result["warnings"])
