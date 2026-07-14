"""Hazard, consequence, combined risk -- and, kept strictly apart from all of them,
confidence.

The four numbers answer four different questions and are never blended:

    hazard       How likely is this terrain to produce an avalanche, given the
                 conditions? (from terrain + instability)
    consequence  If it does, what does it hit? (from the runout + exposure)
    risk         The two combined.
    confidence   How much should you believe the three numbers above?

Confidence is about the **inputs**, never about the answer. A hazard score of 85
computed from three-day-old weather measured 17 km away, over terrain with no snow
observations and against a model that has never been checked against a real
avalanche, is still a low-confidence 85. The system says so.

There is a hard ceiling. Until historical Mount Hosmer avalanche observations
exist to validate against, no result may report a confidence above
``confidence.maximum_without_calibration``. No amount of good input data can make
an unvalidated model trustworthy, and allowing 95 % confidence here would be the
single most dangerous thing this software could do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core import provenance as prov
from app.core.model_config import DISCLAIMER, ModelConfig


@dataclass
class RiskAssessment:
    hazard_score: float
    consequence_score: float
    combined_risk_score: float
    confidence_score: float
    risk_level: str
    risk_color: str
    consequence_class: str
    main_reasons: list[str]
    limitations: list[str]
    confidence_breakdown: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "riskLevel": self.risk_level,
            "riskColor": self.risk_color,
            "hazardScore": round(self.hazard_score),
            "consequenceScore": round(self.consequence_score),
            "combinedRiskScore": round(self.combined_risk_score),
            "confidenceScore": round(self.confidence_score),
            "consequenceClass": self.consequence_class,
            "mainReasons": self.main_reasons,
            "limitations": self.limitations,
            "confidenceBreakdown": self.confidence_breakdown,
            "warnings": self.warnings,
            "isProbability": False,
            "isOfficialForecast": False,
            "disclaimer": DISCLAIMER,
        }


def _interp(value: float, xs: list[float], ys: list[float]) -> float:
    return float(np.interp(value, xs, ys))


def score_confidence(
    *,
    config: ModelConfig,
    inputs: list[prov.Value],
    observation_age_hours: float | None,
    station_distance_km: float | None,
    lidar_fraction: float | None,
    cloud_cover_percent: float | None,
    infrastructure_complete: bool,
    instability_weight_fraction: float,
    simulation_uncertainty_ratio: float | None,
    is_scenario: bool,
) -> tuple[float, dict[str, Any]]:
    """Score how much the inputs justify believing the result.

    Every component that cannot be evaluated is EXCLUDED from the weighted mean
    rather than scored as zero -- the same rule the instability model follows, for
    the same reason.
    """
    weights = config.get("confidence.weights", {}) or {}
    ceiling = float(config.require("confidence.maximum_without_calibration"))

    components: dict[str, dict[str, Any]] = {}

    summary = prov.summarize(inputs)

    components["input_completeness"] = {
        "score": round(summary["completeness"] * 100.0, 1),
        "weight": float(weights.get("input_completeness", 0)),
        "detail": (
            f"{summary['available_count']} of {summary['input_count']} model inputs were available."
        ),
    }

    components["input_provenance"] = {
        "score": round(summary["mean_input_confidence"] * 100.0, 1),
        "weight": float(weights.get("input_provenance", 0)),
        "detail": (
            f"{summary['measured_count']} of {summary['input_count']} inputs were actually measured; "
            f"the rest were interpolated, modelled, or user-supplied."
        ),
    }

    if observation_age_hours is not None:
        components["observation_age"] = {
            "score": round(
                _interp(
                    observation_age_hours,
                    list(config.require("confidence.observation_age_hours")),
                    list(config.require("confidence.observation_age_scores")),
                ),
                1,
            ),
            "weight": float(weights.get("observation_age", 0)),
            "detail": f"Newest observation is {observation_age_hours:.0f} h old.",
        }

    if station_distance_km is not None:
        components["station_distance"] = {
            "score": round(
                _interp(
                    station_distance_km,
                    list(config.require("confidence.station_distance_km")),
                    list(config.require("confidence.station_distance_scores")),
                ),
                1,
            ),
            "weight": float(weights.get("station_distance", 0)),
            "detail": (
                f"Nearest weather station is {station_distance_km:.0f} km from the AOI. No "
                f"instrument is on the mountain itself."
            ),
        }

    if lidar_fraction is not None:
        components["terrain_resolution"] = {
            "score": round(lidar_fraction * 100.0, 1),
            "weight": float(weights.get("terrain_resolution", 0)),
            "detail": (
                f"{lidar_fraction:.0%} of the analysed terrain is backed by 1 m LiDAR; the "
                f"remainder is interpolated from a 30 m DEM."
            ),
        }

    if cloud_cover_percent is not None:
        components["cloud_cover"] = {
            "score": round(
                _interp(
                    cloud_cover_percent,
                    list(config.require("confidence.cloud_cover_percent")),
                    list(config.require("confidence.cloud_cover_scores")),
                ),
                1,
            ),
            "weight": float(weights.get("cloud_cover", 0)),
            "detail": f"{cloud_cover_percent:.0f}% of the satellite scene was cloud covered.",
        }

    components["infrastructure_completeness"] = {
        "score": 100.0 if infrastructure_complete else 25.0,
        "weight": float(weights.get("infrastructure_completeness", 0)),
        "detail": (
            "Infrastructure inventory is complete."
            if infrastructure_complete
            else "OpenStreetMap building coverage for this area is known to be incomplete, so the "
            "consequence score may understate what is exposed."
        ),
    }

    # The honest one. This is pinned low, on purpose, and it cannot be raised by
    # collecting better weather data.
    components["model_calibration"] = {
        "score": 15.0,
        "weight": float(weights.get("model_calibration", 0)),
        "detail": (
            "The release and runout models have NEVER been validated against an observed Mount "
            "Hosmer avalanche, because no historical avalanche record exists for this mountain. "
            "This is a permanent cap on how much any output here can be trusted."
        ),
    }

    total_weight = sum(component["weight"] for component in components.values())
    if total_weight <= 0:
        return 0.0, {"components": components, "error": "No confidence weights are configured."}

    raw = sum(component["score"] * component["weight"] for component in components.values()) / total_weight

    penalties: list[str] = []

    # A score built from half its inputs does not deserve full confidence, even if
    # the half it has is excellent.
    if instability_weight_fraction < 1.0:
        raw *= 0.6 + 0.4 * instability_weight_fraction
        penalties.append(
            f"Only {instability_weight_fraction:.0%} of the instability model's inputs were "
            f"available; confidence reduced accordingly."
        )

    # A simulation whose uncertainty envelope is far larger than its core is
    # telling you it does not know where the avalanche stops.
    if simulation_uncertainty_ratio is not None and simulation_uncertainty_ratio > 1.2:
        factor = float(np.clip(1.4 / simulation_uncertainty_ratio, 0.5, 1.0))
        raw *= factor
        penalties.append(
            f"The simulated uncertainty envelope is {simulation_uncertainty_ratio:.1f}x the size of "
            f"the core runout, so the stopping distance is poorly constrained."
        )

    if is_scenario:
        raw *= 0.75
        penalties.append(
            "This is a user-defined hypothetical scenario, not an observed state. Confidence is "
            "reduced because the inputs are assumptions."
        )

    capped = min(raw, ceiling)
    hit_ceiling = raw > ceiling

    breakdown = {
        "components": components,
        "weighted_score_before_penalties": round(raw, 1),
        "penalties": penalties,
        "maximum_without_calibration": ceiling,
        "ceiling_applied": hit_ceiling,
        "ceiling_reason": (
            "No output of this system may report a confidence above "
            f"{ceiling:.0f}/100 while the model remains uncalibrated against observed avalanches."
        ),
        "final_score": round(capped, 1),
    }
    return float(capped), breakdown


def assess(
    *,
    config: ModelConfig,
    hazard_score: float,
    consequence_score: float,
    consequence_class: str,
    confidence_score: float,
    confidence_breakdown: dict[str, Any],
    reasons: list[str],
    limitations: list[str],
    warnings: list[str] | None = None,
) -> RiskAssessment:
    """Combine hazard and consequence into risk. Confidence is passed through untouched."""
    hazard_weight = float(config.require("risk.hazard_weight"))
    consequence_weight = float(config.require("risk.consequence_weight"))
    total = hazard_weight + consequence_weight or 1.0

    combined = (
        hazard_score * (hazard_weight / total) + consequence_score * (consequence_weight / total)
    )
    combined = float(np.clip(combined, 0.0, 100.0))

    classes = config.require("risk.classes")
    match = next((item for item in classes if combined <= float(item["max"])), classes[-1])

    all_warnings = list(warnings or [])

    # The one combination that must always be called out. A high risk number that
    # nobody should trust is a trap, and saying so is the whole job.
    if combined >= 60 and confidence_score < 50:
        all_warnings.append(
            f"HIGH RISK, LOW CONFIDENCE. The model reports a combined risk of {combined:.0f}/100, "
            f"but its confidence in that number is only {confidence_score:.0f}/100. Treat this as a "
            f"flag to investigate, NOT as a quantified hazard. Consult Avalanche Canada and make a "
            f"field assessment."
        )

    return RiskAssessment(
        hazard_score=hazard_score,
        consequence_score=consequence_score,
        combined_risk_score=combined,
        confidence_score=confidence_score,
        risk_level=str(match["label"]),
        risk_color=str(match.get("color", "#888888")),
        consequence_class=consequence_class,
        main_reasons=reasons,
        limitations=limitations,
        confidence_breakdown=confidence_breakdown,
        warnings=all_warnings,
    )
