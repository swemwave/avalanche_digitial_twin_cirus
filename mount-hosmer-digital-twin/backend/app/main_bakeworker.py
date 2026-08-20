r"""The **bakeworker** service: a network front door onto the local bake stack.

Built from the ``bake`` Docker target -- the only image in the fleet with
rasterio, pyproj, PIL, and pyyaml -- this is what lets the always-slim
``runtime``/``assess`` image delegate mountain-upload probe and bake work
instead of running it in a process that deliberately carries none of the
geospatial stack. See :mod:`app.mountain_jobs`'s module docstring for the full
split: this service's route handlers call :func:`app.mountain_jobs.run_probe`
and :func:`app.mountain_jobs.run_bake` exactly as the local dev launcher does
(this process never sets ``AVALANCHE_BAKE_WORKER_URL`` on itself, so those
functions take their unmodified local-subprocess branch here) -- there is no
second copy of probe/bake logic anywhere in this file.

What this service deliberately does NOT do, unlike ``main_assess``/``main_assistant``:

* It is never reached through the ALB. Only the assess service calls it, over
  the internal network, so its routes carry no ``/api`` prefix and it needs no
  CORS middleware -- nothing browser-facing ever sees this service directly.
* It does not go through :func:`app.service.create_app`, which hardcodes
  ``/api/health`` and adds that browser-facing CORS middleware. This is a small
  standalone ``FastAPI()`` app instead, kept in the same spirit: structured
  logging, a clear health endpoint, JSON error bodies.
* It holds no registry of its own. A mountain's durable status lives in the
  assess side's ``runtime/mountains/registry.json`` (see :mod:`app.mountains`);
  this service's in-memory job dict exists only so ``GET /bake/{id}`` has
  something to report while a bake it started is still running, and does not
  need to survive a restart.

    uvicorn app.main_bakeworker:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.settings import get_settings
from app.mountain_jobs import BAKE_STAGES, BakeBusyError, MountainJobError, run_bake, run_probe

logger = logging.getLogger("avalanche.bakeworker")

app = FastAPI(
    title="Mount Hosmer Digital Twin -- Bake Worker",
    description=(
        "Internal-only service carrying the offline geospatial stack (rasterio, pyproj, "
        "PIL, pyyaml). Validates and bakes uploaded mountains on behalf of the assess "
        "service, which ships none of that stack itself."
    ),
)

#: One bake job's last-known state, keyed by mountain id. Written by the background
#: thread a POST starts and read by GET; this process is the only writer and reader,
#: so a plain dict behind one lock is enough -- see the module docstring for why this
#: does not need to survive a restart.
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}

#: The mountain id currently baking, or ``None``. Guarded by ``_JOBS_LOCK`` alongside
#: ``_JOBS`` so a POST can check-and-set it atomically -- see ``start_bake`` for why.
_ACTIVE_MOUNTAIN_ID: str | None = None


# --- Health ---------------------------------------------------------------------


@app.get("/health")
def health() -> JSONResponse:
    """Liveness plus a check that this image actually carries its geospatial stack.

    Imported at request time, not at module import time, so a broken install
    reports itself here instead of crashing the process before it can say why.
    """
    try:
        import pyproj  # noqa: F401
        import rasterio  # noqa: F401

        importable = True
    except ImportError:
        importable = False
    status_code = 200 if importable else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if importable else "error", "rasterio_importable": importable},
    )


# --- Probe ------------------------------------------------------------------------


class ProbeRequest(BaseModel):
    mountain_id: str
    name: str
    provider: str
    citation: str
    licence: str
    vertical_units_affirmed: bool = False
    resolution_m: float | None = None


@app.post("/probe")
def probe(request: ProbeRequest) -> JSONResponse:
    """Validate an uploaded raster in-process, where rasterio actually lives.

    Always 200 when :func:`run_probe` returns at all: its ``ProbeOutcome.payload``
    already carries its own ``ok`` for validation success/rejection, which is a
    business outcome, not a transport failure. Only a raised
    :class:`MountainJobError` -- timeout, launch failure, no output -- is a
    non-200 here, as a top-level ``{"code", "message"}`` body (not FastAPI's
    ``HTTPException`` ``{"detail": ...}`` wrapping) so ``mountain_jobs._worker_error``
    on the assess side can parse it without knowing anything about this framework.
    """
    try:
        outcome = run_probe(
            get_settings(),
            mountain_id=request.mountain_id,
            name=request.name,
            provider=request.provider,
            citation=request.citation,
            licence=request.licence,
            vertical_units_affirmed=request.vertical_units_affirmed,
            resolution_m=request.resolution_m,
        )
    except MountainJobError as exc:
        status_code = 504 if exc.code == "probe_timeout" else 500
        logger.warning("Probe failed for %s: %s (%s)", request.mountain_id, exc, exc.code)
        return JSONResponse(status_code=status_code, content={"code": exc.code, "message": str(exc)})
    return JSONResponse(content=outcome.payload)


# --- Bake -------------------------------------------------------------------------


def _initial_job() -> dict[str, Any]:
    return {
        "status": "baking",
        "stage": BAKE_STAGES[0][1],
        "bake_sha256": None,
        "warnings": [],
        "code": None,
        "error": None,
    }


def _run_bake_job(mountain_id: str) -> None:
    """Run one bake to completion on a background thread and record its outcome.

    Every exit path -- including an exception :func:`run_bake` was not written to
    raise -- writes a terminal status, so a job is never left reported as
    ``"baking"`` after this thread ends, mirroring
    :func:`app.mountain_jobs.build_mountain`'s own guarantee for the local path.
    """
    global _ACTIVE_MOUNTAIN_ID

    def on_stage(label: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(mountain_id)
            if job is not None:
                job["stage"] = label

    try:
        result = run_bake(get_settings(), mountain_id=mountain_id, on_stage=on_stage)
    except MountainJobError as exc:
        logger.warning("Bake failed for %s: %s (%s)", mountain_id, exc, exc.code)
        with _JOBS_LOCK:
            _JOBS[mountain_id] = {**_initial_job(), "status": "failed", "stage": None, "code": exc.code, "error": str(exc)}
    except BakeBusyError as exc:
        # Should not happen: `start_bake` below serializes entry via
        # _ACTIVE_MOUNTAIN_ID before a second thread can ever be started. Handled
        # anyway so a race can never leave a job stuck reporting "baking" forever.
        logger.error("Unexpected BakeBusyError for %s: %s", mountain_id, exc)
        with _JOBS_LOCK:
            _JOBS[mountain_id] = {**_initial_job(), "status": "failed", "stage": None, "code": "bake_busy", "error": str(exc)}
    except Exception as exc:  # never leave a job stuck at status="baking"
        logger.exception("Unexpected failure baking %s", mountain_id)
        with _JOBS_LOCK:
            _JOBS[mountain_id] = {
                **_initial_job(), "status": "failed", "stage": None, "code": "bake_failed",
                "error": f"Unexpected failure while baking: {exc}",
            }
    else:
        logger.info("Bake ready for %s (stage=%s)", mountain_id, result["stage"])
        with _JOBS_LOCK:
            _JOBS[mountain_id] = {
                "status": "ready", "stage": result["stage"], "bake_sha256": result["bake_sha256"],
                "warnings": result["warnings"], "code": None, "error": None,
            }
    finally:
        with _JOBS_LOCK:
            _ACTIVE_MOUNTAIN_ID = None


@app.post("/bake/{mountain_id}")
def start_bake(mountain_id: str) -> JSONResponse:
    """Start a bake on a background thread and answer immediately.

    Judgment call on the 409-timing question the contract leaves open: rather
    than let :func:`run_bake`'s own lock discover busyness inside the background
    thread (which would mean POST always answers 202 and the caller only learns
    it was rejected on the next GET poll), this checks and sets
    ``_ACTIVE_MOUNTAIN_ID`` synchronously, under ``_JOBS_LOCK``, before returning
    -- so a second POST while one bake is running gets a true synchronous 409,
    matching the contract's "409 immediately" wording exactly. This is
    deliberately a *separate* flag from :mod:`app.mountain_jobs`'s own
    ``_bake_lock`` rather than reaching into that private module lock: it is
    this service's own single-flight guarantee (bakeworker handles one
    mountain's bake at a time by construction), so it owns its own lock.
    """
    global _ACTIVE_MOUNTAIN_ID
    with _JOBS_LOCK:
        if _ACTIVE_MOUNTAIN_ID is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "bake_busy",
                    "message": "Another mountain is baking. Try again when it finishes.",
                },
            )
        _ACTIVE_MOUNTAIN_ID = mountain_id
        _JOBS[mountain_id] = _initial_job()

    logger.info("Bake starting for %s", mountain_id)
    threading.Thread(target=_run_bake_job, args=(mountain_id,), daemon=True).start()
    return JSONResponse(status_code=202, content={"status": "accepted"})


@app.get("/bake/{mountain_id}")
def get_bake(mountain_id: str) -> JSONResponse:
    """Current status of a bake job started on this bakeworker process."""
    with _JOBS_LOCK:
        job = _JOBS.get(mountain_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"code": "bake_unknown", "message": f"No bake job found for mountain {mountain_id!r}."},
        )
    return JSONResponse(content=job)
