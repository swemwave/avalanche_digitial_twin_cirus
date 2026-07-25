r"""The assessment route.

This is the **only** place in the system that turns conditions into a hazard
number, and therefore the only place the ``DISCLAIMER`` has to be attached (it is,
in ``app.assess``). The assistant does not compute hazard -- it calls here. Keeping
that true is what makes the safety invariant hold across a split deployment.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import assess as assess_mod
from app import risk
from app.api.deps import baked

router = APIRouter(prefix="/api", tags=["assess"])


class AssessRequest(BaseModel):
    """The slider conditions. Meteorological wind convention: direction FROM."""

    new_snow_cm: float = Field(default=0.0, ge=0, le=300)
    wind_speed_kmh: float = Field(default=0.0, ge=0, le=200)
    wind_direction_deg: float = Field(default=225.0, ge=0, le=360)
    release_size: str = Field(default="medium")
    simulation_mode: str = Field(default="fast")
    seed: int | None = Field(default=None)


@router.post("/assess")
def assess(body: AssessRequest, bt=Depends(baked)) -> dict[str, Any]:
    """Slider conditions -> release zones + runout + hazard, in one synchronous call."""
    if body.simulation_mode not in ("fast", "advanced"):
        raise HTTPException(status_code=400, detail="simulation_mode must be 'fast' or 'advanced'.")
    if body.release_size not in risk.RELEASE_SIZES:
        raise HTTPException(
            status_code=400,
            detail=f"release_size must be one of {', '.join(risk.RELEASE_SIZES)}.",
        )
    conditions = risk.Conditions(
        new_snow_cm=body.new_snow_cm,
        wind_speed_kmh=body.wind_speed_kmh,
        wind_direction_deg=body.wind_direction_deg,
        release_size=body.release_size,
    )
    return assess_mod.assess(bt, conditions, simulation_mode=body.simulation_mode, seed=body.seed)
