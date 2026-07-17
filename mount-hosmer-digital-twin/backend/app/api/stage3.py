r"""Stage 3 API: the whole running surface is health + terrain + assess + assistant.

No jobs, no polling, no database. ``/assess`` does its numerical work inside the
request and returns the finished result. Terrain is served as static baked tiles
and a small meta document -- nothing here touches ``DATA\`` or rasterio.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import assess as assess_mod
from app import risk
from app.assistant import AssistantError, chat as assistant_chat, explain as assistant_explain
from app.baked import BakeNotFoundError, load_baked, tile_path
from app.core.settings import get_settings

router = APIRouter(prefix="/api", tags=["stage3"])


def _bt():
    try:
        return load_baked(get_settings())
    except BakeNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --- Terrain ------------------------------------------------------------------


@router.get("/twin/meta")
def twin_meta() -> dict[str, Any]:
    """What the map needs to place the mountain and drape the mesh.

    The reprojection lattice is omitted -- it is a server-side concern used to build
    GeoJSON; the browser never needs it.
    """
    bt = _bt()
    meta = {key: value for key, value in bt.meta.items() if key != "reproject"}
    meta["tiles"] = dict(meta.get("tiles", {}))
    meta["tiles"]["url_template"] = "/api/twin/tiles/{z}/{x}/{y}.png"
    return meta


@router.get("/twin/tiles/{z}/{x}/{y}.png")
def twin_tile(z: int, x: int, y: int) -> FileResponse:
    """A static baked terrain-RGB tile. No rasterio: it is a file on disk."""
    path = tile_path(get_settings(), z, x, y)
    if not path.exists():
        # Tiles outside the 12x12 km AOI legitimately do not exist. MapLibre treats
        # a 404 as an empty tile, which is exactly right.
        raise HTTPException(status_code=404, detail="No tile here.")
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
    )


# --- Assess -------------------------------------------------------------------


class AssessRequest(BaseModel):
    """The slider conditions. Meteorological wind convention: direction FROM."""

    new_snow_cm: float = Field(default=0.0, ge=0, le=300)
    wind_speed_kmh: float = Field(default=0.0, ge=0, le=200)
    wind_direction_deg: float = Field(default=225.0, ge=0, le=360)
    release_size: str = Field(default="medium")
    simulation_mode: str = Field(default="fast")
    seed: int | None = Field(default=None)


@router.post("/assess")
def assess(body: AssessRequest) -> dict[str, Any]:
    """Slider conditions -> release zones + runout + hazard, in one synchronous call."""
    bt = _bt()
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


# --- Assistant (local Ollama) -------------------------------------------------


class ExplainRequest(BaseModel):
    assessment: dict[str, Any]


class ChatRequest(BaseModel):
    message: str
    assessment: dict[str, Any] | None = None


@router.post("/assistant/explain")
def assistant_explain_route(body: ExplainRequest) -> dict[str, Any]:
    """Plain-language read of an assessment. The disclaimer is appended in code."""
    try:
        return assistant_explain(body.assessment)
    except AssistantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/assistant/chat")
def assistant_chat_route(body: ChatRequest) -> dict[str, Any]:
    """Scenario chat: parse the message to slider values, re-run /assess, narrate."""
    bt = _bt()
    try:
        return assistant_chat(bt, body.message, body.assessment)
    except AssistantError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
