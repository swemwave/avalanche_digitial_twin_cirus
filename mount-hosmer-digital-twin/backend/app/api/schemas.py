"""Request models for the v1 API.

Bounds are here because a scenario is user-supplied input to a hazard model, and
the model will faithfully compute a number from whatever it is given. A wind speed
of 5,000 km/h produces a wind-loading field, and it looks exactly as authoritative
as a real one. Rejecting it at the door is cheaper than explaining it afterwards.

Responses are deliberately NOT modelled. The analysis and simulation payloads carry
the full provenance, warnings, limitations and per-component explanation, and
pinning them into a response schema would mean either duplicating that structure in
two places, or -- far worse -- quietly dropping the fields the schema forgot. The
disclaimer and the warnings are the point; they do not get to be optional.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.processing.weather.features import ScenarioInput
from app.simulation.runout import RELEASE_SIZES

ReleaseSize = Literal["small", "medium", "large", "very_large"]
SimulationMode = Literal["fast", "advanced"]
AnalysisMode = Literal["historical", "current", "scenario"]


class ScenarioBody(BaseModel):
    """A hypothetical set of conditions. Every field is optional; supplied fields win."""

    model_config = {"extra": "forbid"}

    snowfall_24h_cm: float | None = Field(default=None, ge=0, le=300)
    snowfall_48h_cm: float | None = Field(default=None, ge=0, le=400)
    snowfall_72h_cm: float | None = Field(default=None, ge=0, le=500)
    rain_24h_mm: float | None = Field(default=None, ge=0, le=300)
    temperature_c: float | None = Field(default=None, ge=-60, le=40)
    temperature_change_24h_c: float | None = Field(default=None, ge=-40, le=40)
    wind_speed_kmh: float | None = Field(default=None, ge=0, le=250)
    wind_direction_deg: float | None = Field(default=None, ge=0, le=360)
    wind_gust_kmh: float | None = Field(default=None, ge=0, le=350)
    snow_depth_index: float | None = Field(default=None, ge=0, le=1)
    swe_index: float | None = Field(default=None, ge=0, le=1)
    freeze_thaw: bool | None = None
    release_size: ReleaseSize = "medium"
    label: str | None = Field(default=None, max_length=120)

    def to_input(self) -> ScenarioInput:
        return ScenarioInput(**self.model_dump())


class AnalysisRequest(BaseModel):
    model_config = {"extra": "forbid"}

    mode: AnalysisMode = "current"
    at: datetime | None = Field(
        default=None, description="Required for historical replay. ISO-8601, UTC."
    )
    scenario: ScenarioBody | None = None
    preset: str | None = Field(
        default=None, description="Name of a built-in scenario preset. Mutually exclusive with 'scenario'."
    )
    event_id: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _coherent(self) -> "AnalysisRequest":
        if self.mode == "historical" and self.at is None:
            raise ValueError(
                "Historical replay needs a datetime ('at'). Without one there is nothing to replay."
            )
        if self.mode == "scenario" and self.scenario is None and self.preset is None:
            raise ValueError(
                "Scenario mode needs either 'scenario' inputs or a 'preset' name. A scenario with "
                "no inputs is just the current conditions under a misleading label."
            )
        if self.scenario is not None and self.preset is not None:
            raise ValueError("Supply 'scenario' or 'preset', not both.")
        return self


class SimulationRequest(BaseModel):
    model_config = {"extra": "forbid"}

    analysis_id: str = Field(max_length=64)
    zone_ids: list[str] | None = Field(
        default=None,
        max_length=200,
        description="Which release zones to run. Omit to simulate every zone in the analysis.",
    )
    simulation_mode: SimulationMode = "fast"
    release_size: ReleaseSize | None = None
    seed: int | None = Field(
        default=None,
        ge=0,
        le=2**32 - 1,
        description="Fixing the seed makes the run reproducible bit-for-bit.",
    )
    idempotency_key: str | None = Field(default=None, max_length=128)


class RescanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    verify_checksums: bool = True


class ProcessRequest(BaseModel):
    model_config = {"extra": "forbid"}

    force: bool = Field(
        default=False,
        description=(
            "Rebuild even when the content-hash cache says the inputs are unchanged. Slow, and "
            "only needed when a processor's code has changed rather than its inputs."
        ),
    )


def known_release_sizes() -> tuple[str, ...]:
    return tuple(RELEASE_SIZES)


def job_accepted(job: dict[str, Any], poll_path: str) -> dict[str, Any]:
    """The 202 body for anything that runs in the background."""
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "progress": job.get("progress", 0),
        "deduplicated": bool(job.get("deduplicated")),
        "poll": poll_path,
        "note": (
            "This work runs in the background. Poll the 'poll' URL until state is 'succeeded' or "
            "'failed'. Re-posting with the same idempotency_key returns this same job rather than "
            "starting a second one."
        ),
    }
