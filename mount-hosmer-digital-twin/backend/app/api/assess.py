r"""The assessment route.

This is the **only** place in the system that turns conditions into a hazard
number, and therefore the only place the ``DISCLAIMER`` has to be attached (it is,
in ``app.assess``). The assistant does not compute hazard -- it calls here. Keeping
that true is what makes the safety invariant hold across a split deployment.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import assess as assess_mod
from app import risk
from app.api.deps import baked

router = APIRouter(prefix="/api", tags=["assess"])


class AssessRequest(BaseModel):
    """The slider conditions. Meteorological wind convention: direction FROM.

    ``release_size`` and ``simulation_mode`` are ``Literal`` rather than ``str`` with
    a hand-written check in the handler. Same rejection, but pydantic does it before
    the handler runs, and -- the actual reason -- the valid values land in the OpenAPI
    schema. Typed as bare ``str`` the schema advertised "any string", so the generated
    docs and any client built from them could not know what the endpoint accepts.
    """

    new_snow_cm: float = Field(default=0.0, ge=0, le=300)
    wind_speed_kmh: float = Field(default=0.0, ge=0, le=200)
    wind_direction_deg: float = Field(default=225.0, ge=0, le=360)
    release_size: Literal["small", "medium", "large", "very_large"] = "medium"
    simulation_mode: Literal["fast", "advanced"] = "fast"
    seed: int | None = Field(default=None)


@router.post("/assess")
def assess(body: AssessRequest, bt=Depends(baked)) -> dict[str, Any]:
    """Slider conditions -> release zones + runout + hazard, in one synchronous call."""
    conditions = risk.Conditions(
        new_snow_cm=body.new_snow_cm,
        wind_speed_kmh=body.wind_speed_kmh,
        wind_direction_deg=body.wind_direction_deg,
        release_size=body.release_size,
    )
    return assess_mod.assess(bt, conditions, simulation_mode=body.simulation_mode, seed=body.seed)
