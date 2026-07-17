r"""The Stage 3 FastAPI app.

The whole running surface is health + terrain + assess + assistant, and it serves
entirely off the baked artifacts under ``runtime/baked/`` -- no jobs, no database,
no ``DATA\`` access, no rasterio. See ``app/api/stage3.py`` for the routes and
``app/bake.py`` for the one-time offline bake that produces what this serves.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors as api_errors
from app.api import middleware as api_middleware
from app.api import stage3 as api_stage3
from app.core.settings import get_settings

logger = logging.getLogger("avalanche.api")

app = FastAPI(
    title="Mount Hosmer Avalanche Digital Twin API",
    version=get_settings().app_version,
    description=(
        "An experimental, terrain- and conditions-based avalanche digital twin: 3D terrain, "
        "runout simulation, a simplified release estimate, and a local AI assistant. "
        "NOT an operational avalanche forecast."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_middleware.install(app)
api_errors.install(app)
app.include_router(api_stage3.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    settings = get_settings()
    baked = settings.runtime_root / "baked" / "meta.json"
    return {
        "status": "ok",
        "baked": baked.exists(),
        "application_version": settings.app_version,
        "note": "Experimental, non-operational research tool. Not an avalanche forecast.",
    }
