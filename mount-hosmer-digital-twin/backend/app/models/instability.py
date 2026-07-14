"""Dynamic instability and the estimated release score.

This is where the time-varying half of the answer is assembled. The static
terrain susceptibility says "this is avalanche terrain". This says "and today it
is loaded".

The rule that governs the whole module, and the reason it is written the way it
is: **a missing component is dropped from the numerator AND the denominator.**

If snowfall is unknown, the model does not score snowfall as zero and average it
in -- that would silently drag the instability score down and make a dangerous day
look safe. Instead the snowfall weight is removed from the total, the remaining
components are reweighted among themselves, and the result carries a warning
saying which inputs were missing. If too little weight survives, the score is
withheld entirely rather than reported with false confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.model_config import DISCLAIMER, ModelConfig
from app.models.snow import SnowFields
from app.models.wind import WindFields
from app.processing.harmonization.grids import AnalysisGrid
from app.processing.weather.features import ConditionSet


@dataclass
class Component:
    """One contributor to the instability score."""

    name: str
    title: str
    weight: float
    field: np.ma.MaskedArray | None
    available: bool
    provenance: str
    reason: str
    missing_reason: str | None = None
    scalar_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "component": self.name,
            "title": self.title,
            "configured_weight": round(self.weight, 4),
            "available": self.available,
            "provenance": self.provenance,
            "reason": self.reason,
        }
        if not self.available:
            payload["missing_reason"] = self.missing_reason
        if self.field is not None and self.field.count():
            payload["mean_score"] = round(float(self.field.mean()), 2)
            payload["max_score"] = round(float(self.field.max()), 2)
        if self.scalar_value is not None:
            payload["input_value"] = round(self.scalar_value, 3)
        return payload


@dataclass
class InstabilityFields:
    dynamic_instability: np.ma.MaskedArray | None
    estimated_release_score: np.ma.MaskedArray | None
    components: list[Component]
    available: bool
    available_weight_fraction: float
    warnings: list[str] = field(default_factory=list)
    explanation: dict[str, Any] = field(default_factory=dict)

    def layers(self) -> dict[str, np.ma.MaskedArray]:
        layers: dict[str, np.ma.MaskedArray] = {}
        if self.dynamic_instability is not None:
            layers["dynamic_instability"] = self.dynamic_instability
        if self.estimated_release_score is not None:
            layers["estimated_release_score"] = self.estimated_release_score
        return layers


def _interp(values: np.ndarray, xs: list[float], ys: list[float]) -> np.ndarray:
    return np.interp(values, xs, ys).astype("float32")


def compute(
    *,
    config: ModelConfig,
    grid: AnalysisGrid,
    conditions: ConditionSet,
    snow: SnowFields,
    wind: WindFields,
    terrain_susceptibility: np.ma.MaskedArray,
    forecast_context: dict[str, Any] | None = None,
) -> InstabilityFields:
    mask = np.ma.getmaskarray(terrain_susceptibility)
    warnings: list[str] = []
    weights = config.get("dynamic_instability.weights", {}) or {}
    minimum_fraction = float(config.require("dynamic_instability.minimum_available_weight_fraction"))

    components: list[Component] = []

    # --- New-snow loading -----------------------------------------------------
    loading = snow.new_snow_loading
    loading_available = bool(loading.count())
    if loading_available:
        scores = _interp(
            np.asarray(loading.filled(0.0)),
            list(config.require("dynamic_instability.new_snow_loading_breakpoints")),
            list(config.require("dynamic_instability.new_snow_loading_scores")),
        )
        loading_field = np.ma.array(scores, mask=mask)
    else:
        loading_field = None
    components.append(
        Component(
            name="new_snow_loading",
            title="New-snow loading",
            weight=float(weights.get("new_snow_loading", 0.0)),
            field=loading_field,
            available=loading_available,
            provenance=snow.explanation.get("new_snow_loading_provenance", "unavailable"),
            reason="New snow adds load faster than the snowpack can gain strength to carry it.",
            missing_reason="Neither reported snowfall nor phase-partitioned snow water was available.",
        )
    )

    # --- Wind loading ---------------------------------------------------------
    components.append(
        Component(
            name="wind_loading",
            title="Wind loading",
            weight=float(weights.get("wind_loading", 0.0)),
            field=wind.wind_loading if wind.available else None,
            available=wind.available,
            provenance="modelled",
            reason=(
                "Wind moves snow from windward slopes into lee slopes, building slabs far thicker "
                "than the snowfall alone would produce."
            ),
            missing_reason="Wind speed or direction was unavailable.",
            scalar_value=wind.wind_speed_kmh,
        )
    )

    # --- Rapid warming --------------------------------------------------------
    warming = conditions.get("rapid_warming_24h_c")
    if warming is not None and warming.is_available:
        value = float(warming.numeric or 0.0)
        score = float(
            np.interp(
                value,
                list(config.require("dynamic_instability.rapid_warming_breakpoints_c")),
                list(config.require("dynamic_instability.rapid_warming_scores")),
            )
        )
        # Warming bites hardest where the sun is already working: it is scaled by
        # the melt index, which carries the aspect dependence.
        melt = np.asarray(snow.melt_index.filled(0.0)) if snow.melt_index.count() else np.ones(grid.shape, "float32")
        warming_field = np.ma.array(
            np.clip(score * (0.55 + 0.45 * melt), 0.0, 100.0).astype("float32"), mask=mask
        )
        warming_available = True
    else:
        warming_field = None
        warming_available = False
        value = None
    components.append(
        Component(
            name="rapid_warming",
            title="Rapid warming",
            weight=float(weights.get("rapid_warming", 0.0)),
            field=warming_field,
            available=warming_available,
            provenance=warming.provenance if warming else "unavailable",
            reason="A sharp temperature rise weakens bonds faster than the snowpack can adjust.",
            missing_reason="No 24 h temperature change was available.",
            scalar_value=value,
        )
    )

    # --- Wet snow -------------------------------------------------------------
    wet_available = bool(snow.wet_snow_index.count())
    components.append(
        Component(
            name="wet_snow",
            title="Wet snow",
            weight=float(weights.get("wet_snow", 0.0)),
            field=np.ma.array(
                (np.asarray(snow.wet_snow_index.filled(0.0)) * 100.0).astype("float32"), mask=mask
            )
            if wet_available
            else None,
            available=wet_available,
            provenance="modelled",
            reason="Free water between grains destroys the snowpack's shear strength.",
            missing_reason="No temperature record, so wet-snow conditions could not be modelled.",
        )
    )

    # --- Rain on snow ---------------------------------------------------------
    rain = conditions.get("rain_on_snow_mm")
    if rain is not None and rain.is_available:
        rain_mm = float(rain.numeric or 0.0)
        score = float(
            np.interp(
                rain_mm,
                list(config.require("dynamic_instability.rain_on_snow_breakpoints_mm")),
                list(config.require("dynamic_instability.rain_on_snow_scores")),
            )
        )
        # Rain only matters where there is snow to fall on.
        presence = np.asarray(snow.snow_presence.filled(0.0))
        rain_field = np.ma.array(
            np.clip(score * presence, 0.0, 100.0).astype("float32"), mask=mask
        )
        rain_available = True
    else:
        rain_field = None
        rain_available = False
        rain_mm = None
    components.append(
        Component(
            name="rain_on_snow",
            title="Rain on snow",
            weight=float(weights.get("rain_on_snow", 0.0)),
            field=rain_field,
            available=rain_available,
            provenance=rain.provenance if rain else "unavailable",
            reason=(
                "Rain adds load and lubricates weak layers simultaneously. Of everything this "
                "model can observe, it is the most reliable instability signal."
            ),
            missing_reason="Rain could not be estimated without both precipitation and temperature.",
            scalar_value=rain_mm,
        )
    )

    # --- Snow-cover evidence --------------------------------------------------
    presence_available = bool(snow.snow_presence.count())
    components.append(
        Component(
            name="snow_cover_evidence",
            title="Snow-cover evidence",
            weight=float(weights.get("snow_cover_evidence", 0.0)),
            field=np.ma.array(
                (np.asarray(snow.snow_presence.filled(0.0)) * 100.0).astype("float32"), mask=mask
            )
            if presence_available
            else None,
            available=presence_available,
            # Only claim "observed" when a satellite actually resolved most of the
            # AOI. The old test here was against a dict that is never empty, so
            # this always reported "observed" even for a fully modelled field.
            provenance=snow.explanation.get("snow_presence_dominant_provenance", "modelled"),
            reason="There cannot be an avalanche without snow. This confirms snow is actually there.",
            missing_reason="Snow presence could be neither observed nor modelled.",
        )
    )

    # --- Storm intensity ------------------------------------------------------
    storm = conditions.get("storm_loading_index_mm")
    if storm is not None and storm.is_available:
        storm_score = float(np.clip((storm.numeric or 0.0) / 40.0, 0.0, 1.0) * 100.0)
        storm_field = np.ma.array(
            np.clip(
                storm_score * np.asarray(snow.snow_presence.filled(0.0)), 0.0, 100.0
            ).astype("float32"),
            mask=mask,
        )
        storm_available = True
    else:
        storm_field = None
        storm_available = False
        storm_score = None
    components.append(
        Component(
            name="storm_intensity",
            title="Storm intensity",
            weight=float(weights.get("storm_intensity", 0.0)),
            field=storm_field,
            available=storm_available,
            provenance=storm.provenance if storm else "unavailable",
            reason="Precipitation delivered fast, and with wind to move it, builds slabs quickly.",
            missing_reason="Requires both precipitation and wind; at least one was unavailable.",
            scalar_value=storm_score,
        )
    )

    # --- Weighted combination, missing components excluded from BOTH sides -----
    configured_weight = sum(component.weight for component in components)
    available_components = [
        component for component in components if component.available and component.field is not None and component.weight > 0
    ]
    available_weight = sum(component.weight for component in available_components)
    fraction = round(available_weight / configured_weight, 4) if configured_weight else 0.0

    for component in components:
        if not component.available and component.weight > 0:
            warnings.append(
                f"'{component.title}' was unavailable ({component.missing_reason}). It has been "
                f"EXCLUDED from the instability score, not scored as zero."
            )

    dynamic: np.ma.MaskedArray | None = None
    release: np.ma.MaskedArray | None = None

    if not available_components or fraction < minimum_fraction:
        warnings.append(
            f"Only {fraction:.0%} of the configured instability weight had data available "
            f"(minimum {minimum_fraction:.0%}). The dynamic instability score and the estimated "
            f"release score are WITHHELD. A number computed from this little input would be "
            f"misleading."
        )
        available = False
    else:
        total = np.zeros(grid.shape, dtype="float32")
        for component in available_components:
            assert component.field is not None
            total += np.asarray(component.field.filled(0.0)) * (component.weight / available_weight)
        dynamic = np.ma.array(np.clip(total, 0.0, 100.0).astype("float32"), mask=mask)
        available = True

        terrain_weight = float(config.require("release_zones.terrain_weight"))
        dynamic_weight = float(config.require("release_zones.dynamic_weight"))
        norm = terrain_weight + dynamic_weight or 1.0
        terrain_weight, dynamic_weight = terrain_weight / norm, dynamic_weight / norm

        combined = (
            np.asarray(terrain_susceptibility.filled(0.0)) * terrain_weight
            + np.asarray(dynamic.filled(0.0)) * dynamic_weight
        )
        release = np.ma.array(np.clip(combined, 0.0, 100.0).astype("float32"), mask=mask)

    # --- Avalanche Canada context, deliberately NOT scored --------------------
    forecast_note = None
    if forecast_context:
        forecast_note = {
            "region": forecast_context.get("applicable_region"),
            "highest_danger": forecast_context.get("highest_danger"),
            "valid_until_utc": forecast_context.get("valid_until_utc"),
            "weight": 0.0,
            "status": "context_only_not_scored",
            "reason": (
                "Avalanche Canada publishes a CURRENT regional forecast. It is not a historical "
                "record, so it cannot be used as a label for a past date, and an off-season "
                "'no forecast' must never be read as 'no danger'. It is shown for context and "
                "contributes zero weight to the score."
            ),
        }

    explanation = {
        "model_type": "Weighted rules-based dynamic instability (0-100 relative index)",
        "is_probability": False,
        "is_forecast": False,
        "disclaimer": DISCLAIMER,
        "configured_weight_total": round(configured_weight, 4),
        "available_weight": round(available_weight, 4),
        "available_weight_fraction": fraction,
        "minimum_required_fraction": minimum_fraction,
        "score_withheld": not available,
        "components": [component.to_dict() for component in components],
        "missing_data_policy": (
            "A missing component is removed from both the numerator and the denominator. It is "
            "never scored as zero. If too little weight survives, the score is withheld entirely."
        ),
        "avalanche_canada_context": forecast_note,
        "limitations": [
            "The model has no knowledge of the snowpack's internal structure. Buried persistent "
            "weak layers -- the mechanism behind most avalanche fatalities -- are invisible to it, "
            "because no snow-profile observations exist for this mountain.",
            "It has not been calibrated against any observed Mount Hosmer avalanche.",
            "The score is a relative index. It is not a probability of release.",
        ],
        "warnings": warnings,
    }

    return InstabilityFields(
        dynamic_instability=dynamic,
        estimated_release_score=release,
        components=components,
        available=available,
        available_weight_fraction=fraction,
        warnings=warnings,
        explanation=explanation,
    )
