"""Named scenario presets.

Each preset is a hypothesis about a kind of day, not a record of one. They exist
so a user can ask "what would this mountain do in a rain-on-snow event?" without
having to know which numbers to type. The values are typical of the Canadian
Rockies in winter; they are not drawn from any specific observed Mount Hosmer day,
because no such record is available.
"""

from __future__ import annotations

from typing import Any

from app.processing.weather.features import ScenarioInput

PRESETS: dict[str, dict[str, Any]] = {
    "stable_winter": {
        "label": "Stable winter conditions",
        "description": (
            "Cold, calm, and settled. Days since the last snowfall, light winds, no warming. "
            "The kind of day the snowpack spends consolidating."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=0.0,
            snowfall_48h_cm=0.0,
            snowfall_72h_cm=1.0,
            rain_24h_mm=0.0,
            temperature_c=-9.0,
            temperature_change_24h_c=-1.0,
            wind_speed_kmh=8.0,
            wind_direction_deg=225.0,
            wind_gust_kmh=15.0,
            snow_depth_index=0.55,
            swe_index=0.50,
            freeze_thaw=False,
            release_size="small",
            label="Stable winter conditions",
        ),
    },
    "heavy_snowfall": {
        "label": "Heavy snowfall",
        "description": (
            "A big storm, but with light winds -- so the load lands where it falls rather than "
            "being blown into lee slopes. New-snow instability without a strong wind-slab signal."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=35.0,
            snowfall_48h_cm=55.0,
            snowfall_72h_cm=70.0,
            rain_24h_mm=0.0,
            temperature_c=-5.0,
            temperature_change_24h_c=1.0,
            wind_speed_kmh=12.0,
            wind_direction_deg=240.0,
            wind_gust_kmh=25.0,
            snow_depth_index=0.85,
            swe_index=0.80,
            freeze_thaw=False,
            release_size="large",
            label="Heavy snowfall",
        ),
    },
    "wind_loading": {
        "label": "Strong wind-loading event",
        "description": (
            "Moderate new snow driven hard by a sustained southwest gale. The snow ends up "
            "somewhere other than where it fell: stripped from windward slopes and deposited "
            "as slabs on northeast lees. This is the classic wind-slab setup for this range."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=15.0,
            snowfall_48h_cm=25.0,
            snowfall_72h_cm=30.0,
            rain_24h_mm=0.0,
            temperature_c=-7.0,
            temperature_change_24h_c=0.0,
            wind_speed_kmh=55.0,
            wind_direction_deg=225.0,
            wind_gust_kmh=85.0,
            snow_depth_index=0.75,
            swe_index=0.70,
            freeze_thaw=False,
            release_size="large",
            label="Strong wind-loading event",
        ),
    },
    "rapid_warming": {
        "label": "Rapid warming",
        "description": (
            "A sharp rise through freezing over a day. Bonds weaken, the snowpack loses strength "
            "faster than it loses load, and solar aspects go first."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=0.0,
            snowfall_48h_cm=5.0,
            snowfall_72h_cm=10.0,
            rain_24h_mm=0.0,
            temperature_c=3.0,
            temperature_change_24h_c=11.0,
            wind_speed_kmh=15.0,
            wind_direction_deg=200.0,
            wind_gust_kmh=30.0,
            snow_depth_index=0.70,
            swe_index=0.68,
            freeze_thaw=True,
            release_size="medium",
            label="Rapid warming",
        ),
    },
    "rain_on_snow": {
        "label": "Rain on snow",
        "description": (
            "Warm rain onto an existing snowpack. It adds load and lubricates weak layers at the "
            "same time. Of everything this model can actually observe, this is the most reliable "
            "instability signal there is."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=0.0,
            snowfall_48h_cm=10.0,
            snowfall_72h_cm=15.0,
            rain_24h_mm=25.0,
            temperature_c=4.0,
            temperature_change_24h_c=7.0,
            wind_speed_kmh=30.0,
            wind_direction_deg=210.0,
            wind_gust_kmh=50.0,
            snow_depth_index=0.75,
            swe_index=0.85,
            freeze_thaw=True,
            release_size="large",
            label="Rain on snow",
        ),
    },
    "spring_wet_snow": {
        "label": "Spring wet-snow cycle",
        "description": (
            "Late-season isothermal snowpack, repeated melt-freeze, strong daytime heating. "
            "Wet loose and wet slab activity building through the afternoon."
        ),
        "input": ScenarioInput(
            snowfall_24h_cm=0.0,
            snowfall_48h_cm=0.0,
            snowfall_72h_cm=0.0,
            rain_24h_mm=0.0,
            temperature_c=8.0,
            temperature_change_24h_c=5.0,
            wind_speed_kmh=10.0,
            wind_direction_deg=180.0,
            wind_gust_kmh=20.0,
            snow_depth_index=0.45,
            swe_index=0.60,
            freeze_thaw=True,
            release_size="medium",
            label="Spring wet-snow cycle",
        ),
    },
}


def preset(name: str) -> ScenarioInput:
    if name not in PRESETS:
        raise KeyError(f"Unknown scenario preset {name!r}. Available: {', '.join(sorted(PRESETS))}")
    return PRESETS[name]["input"]


def list_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": key,
            "label": item["label"],
            "description": item["description"],
            "inputs": item["input"].to_dict(),
        }
        for key, item in PRESETS.items()
    ]
