r"""One way to build a Stage 3 FastAPI app, however many of them we deploy.

There are three entrypoints -- the combined app (:mod:`app.main`) plus one per
service (:mod:`app.main_assess`, :mod:`app.main_assistant`) -- and they must not
drift on CORS, error shape, body limits, or the disclaimer note on ``/health``.
So they all come through :func:`create_app` and differ only in which routers they
mount and what their health check reports.

Nothing here imports rasterio or pyproj, and nothing here loads baked terrain --
the assistant service has neither.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors as api_errors
from app.api import middleware as api_middleware
from app.core.settings import get_settings

logger = logging.getLogger("avalanche.api")

#: On every service, on every health check. A deployment that answers about hazard
#: must never look like an operational forecast, not even in its liveness probe.
NON_OPERATIONAL_NOTE = (
    "Experimental, non-operational research tool. Not an avalanche forecast."
)


def baked_present() -> bool:
    """Whether the bake exists. Cheap enough for a liveness probe."""
    return (get_settings().runtime_root / "baked" / "meta.json").exists()


#: Parsed once, on the first health check that finds a bake. Under compose the
#: volume may still be filling when the service starts, so this is not cached until
#: the read actually succeeds.
_bake_stamp: dict[str, object] | None = None


def baked_stamp() -> dict[str, object]:
    """Whether the bake is present, and *which* bake it is.

    ``bake_generated_at`` is ``meta.json``'s ``generated_at_utc``. It matters
    because the assess image copies the baked terrain in rather than mounting it
    (Cloud Run has no named volumes), so a redeploy is the only thing that refreshes
    it. Reporting the bake's own timestamp is what keeps a stale image *visible*
    rather than silently served as current -- see the note in ``.dockerignore``.
    """
    global _bake_stamp

    meta_path = get_settings().runtime_root / "baked" / "meta.json"
    if not meta_path.exists():
        return {
            "baked": False,
            "bake_generated_at": None,
            "bake_schema": None,
            "bake_sha256": None,
        }
    if _bake_stamp is None:
        import json

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            _bake_stamp = {
                "bake_generated_at": meta.get("generated_at_utc"),
                "bake_schema": meta.get("schema"),
                "bake_sha256": meta.get("identity", {}).get("bake_sha256"),
            }
        except (OSError, ValueError):
            # A present-but-unreadable meta.json is a real problem, but a health
            # check is the wrong place to fail: report the gap, don't hide it.
            return {
                "baked": True,
                "bake_generated_at": None,
                "bake_schema": None,
                "bake_sha256": None,
            }
    return {"baked": True, **_bake_stamp}


def create_app(
    *,
    title: str,
    description: str,
    routers: Sequence[APIRouter],
    health_extra: Callable[[], dict[str, Any]] | None = None,
    on_startup: Callable[[], None] | None = None,
) -> FastAPI:
    """Assemble a service: shared middleware and error handling, then its routers.

    ``on_startup`` runs once before the first request. Only the combined app uses
    it, and a failure there must not stop the service from serving: it is
    housekeeping, not a prerequisite.
    """
    settings = get_settings()

    lifespan = None
    if on_startup is not None:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_: FastAPI):  # noqa: F811 - the name is the parameter
            try:
                on_startup()
            except Exception:
                logger.warning("Startup housekeeping failed; serving anyway.", exc_info=True)
            yield

    app = FastAPI(
        title=title, version=settings.app_version, description=description, lifespan=lifespan
    )

    # Cloud Run puts each service on its own origin, so the browser calling the
    # assistant service is cross-origin even though it is the same application.
    # AVALANCHE_CORS_ORIGINS is how a deployment names the frontend's real origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_middleware.install(app)
    api_errors.install(app)
    for router in routers:
        app.include_router(router)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "ok",
            "application_version": settings.app_version,
            "note": NON_OPERATIONAL_NOTE,
        }
        if health_extra is not None:
            payload.update(health_extra())
        return payload

    return app


def assess_backend() -> dict[str, Any]:
    """Which assessment backend this process will use, for the health payload."""
    remote = os.environ.get("AVALANCHE_ASSESS_URL", "").strip()
    return {"assess_backend": remote or "in-process"}
