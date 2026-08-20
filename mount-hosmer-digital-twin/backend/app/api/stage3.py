r"""The combined Stage 3 API surface: terrain + assess + assistant on one router.

The routes themselves now live in three sibling modules, one per deployable
service. This module is what mounts all three in a single process, and it is what
the local ``uvicorn app.main:app`` and the one-click launcher serve -- so running
the whole thing on a laptop stays exactly as simple as it was.

The split versions are :mod:`app.main_assess` and :mod:`app.main_assistant`. The
route paths are identical either way: which process answers a request is a
deployment decision, never something the frontend or the tests encode.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import assess as assess_routes
from app.api import assistant as assistant_routes
from app.api import predictions as prediction_routes
from app.api import terrain as terrain_routes

router = APIRouter()
router.include_router(terrain_routes.router)
router.include_router(assess_routes.router)
router.include_router(prediction_routes.router)
router.include_router(assistant_routes.router)
