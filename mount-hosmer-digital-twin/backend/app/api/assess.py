r"""The assessment route.

This is the **only** place in the system that turns conditions into a hazard
number, and therefore the only place the ``DISCLAIMER`` has to be attached (it is,
in ``app.assess``). The assistant does not compute hazard -- it calls here. Keeping
that true is what makes the safety invariant hold across a split deployment.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from avycore.scenario import scenario_from_simple_values

from app import assess as assess_mod
from app.api.deps import baked
from app.api.models import AssessRequest, AssessResult

router = APIRouter(prefix="/api", tags=["assess"])

@router.post("/assess", response_model=AssessResult, operation_id="postAssess")
def assess(body: AssessRequest, bt=Depends(baked)) -> AssessResult:
    """Explicit research scenario -> release zones + runout, when inputs support it."""
    scenario = body.scenario or scenario_from_simple_values(
        new_snow_cm=body.new_snow_cm,
        wind_speed_kmh=body.wind_speed_kmh,
        wind_direction_deg=body.wind_direction_deg,
        release_size=body.release_size,
        air_temperature_c=body.air_temperature_c,
        flow_regime=body.flow_regime,
        alpha_angle_override_deg=body.alpha_angle_override_deg,
    )
    return AssessResult.model_validate(
        assess_mod.assess(bt, scenario=scenario, simulation_mode=body.simulation_mode, seed=body.seed)
    )
