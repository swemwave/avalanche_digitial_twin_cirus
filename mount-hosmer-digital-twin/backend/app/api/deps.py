r"""Shared route dependencies.

Two things every router needs and neither should construct for itself: the baked
terrain, and whatever runs an assessment. Keeping them here is what lets the same
router modules serve both the combined dev app and the split services.
"""

from __future__ import annotations

import os

from fastapi import HTTPException

from app.assess_client import AssessClient, AssessUnavailableError, from_environment
from app.core.settings import get_settings


def baked():
    """The baked terrain, or a 409 telling the caller to run the bake.

    409 rather than 500: an unbuilt bake is a missing prerequisite, not a fault.

    ``app.baked`` is imported here rather than at module scope so the assistant
    service -- which mounts this module for :func:`assess_client` but ships no
    baked artifacts at all -- never loads the terrain reader.
    """
    from app.baked import BakeIncompatibleError, BakeNotFoundError, load_baked

    try:
        return load_baked(get_settings())
    except (BakeNotFoundError, BakeIncompatibleError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def assess_client() -> AssessClient:
    """In-process assessment locally; the assess service when it is configured.

    ``AVALANCHE_ASSESS_URL`` decides. When it is unset we need baked terrain, so
    that lookup happens here and can still 409 exactly as the terrain routes do.
    """
    if os.environ.get("AVALANCHE_ASSESS_URL", "").strip():
        return from_environment()
    try:
        return from_environment(baked())
    except AssessUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
