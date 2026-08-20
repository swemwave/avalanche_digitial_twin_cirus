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


def baked(mountain: str | None = None):
    """The baked terrain, or a 409 telling the caller to run the bake.

    409 rather than 500: an unbuilt bake is a missing prerequisite, not a fault.

    ``?mountain=`` selects an uploaded mountain; omitting it serves Mount Hosmer,
    so existing clients keep working without change. An unknown or malformed id is
    a 404 rather than a silent fall back to Hosmer -- rendering one mountain's
    result over another's terrain is the worst failure this feature could have.

    ``app.baked`` is imported here rather than at module scope so the assistant
    service -- which mounts this module for :func:`assess_client` but ships no
    baked artifacts at all -- never loads the terrain reader.
    """
    from app.baked import BakeIncompatibleError, BakeNotFoundError, load_baked
    from app.mountains import MountainError

    try:
        return load_baked(get_settings(), mountain)
    except MountainError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (BakeNotFoundError, BakeIncompatibleError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def assess_client(mountain: str | None = None) -> AssessClient:
    """In-process assessment locally; the assess service when it is configured.

    ``AVALANCHE_ASSESS_URL`` decides. When it is unset we need baked terrain, so
    that lookup happens here and can still 409 exactly as the terrain routes do.

    ``mountain`` has to be threaded through rather than defaulted: ``/assistant/chat``
    re-runs a real assessment, so without it a question asked while viewing an
    uploaded mountain would be answered from Mount Hosmer's terrain and returned
    as though it described the mountain on screen.
    """
    if os.environ.get("AVALANCHE_ASSESS_URL", "").strip():
        if mountain is not None:
            # The remote assess service is built around the reviewed bake it ships
            # with and knows nothing of a locally uploaded mountain. Answering from
            # its terrain would be worse than refusing.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Uploaded mountains are assessed by the local application only; this "
                    "instance delegates assessment to a remote service."
                ),
            )
        return from_environment()
    try:
        return from_environment(baked(mountain))
    except AssessUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
