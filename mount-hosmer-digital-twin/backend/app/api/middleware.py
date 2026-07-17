"""Correlation ids, structured logging, request-size limits, and rate limiting.

The rate limiter is in-process and per-IP, which is exactly right for what this is:
a single-process research prototype that ships as a double-clicked `.exe`. It is not
protecting against a distributed attacker; it is protecting the machine from itself.
One simulation is a 60-second, 2400x2400-cell numerical run, so a handful of
impatient clicks on "Simulate" can queue enough work to bury the box. The limiter
makes that fail fast and legibly instead of slowly and mysteriously.

If this ever runs behind more than one process, the limiter must move to Redis --
`REDIS_URL` is reserved in settings for exactly that. Until then, saying "per-IP,
in-process" out loud is more honest than implying a guarantee we do not have.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.errors import envelope

logger = logging.getLogger("avalanche.api")

CORRELATION_HEADER = "X-Correlation-ID"

#: A request body larger than this is refused before it is read. Every endpoint
#: takes a small JSON object -- EXCEPT the assistant endpoints below.
MAX_BODY_BYTES = 256 * 1024

#: The one legitimate exception. ``/api/assistant/explain`` and ``/api/assistant/chat``
#: receive a whole assessment result (the release-zone and runout GeoJSON the client
#: already holds) so the model can summarize it -- ~1 MB for a big storm, ~1.4 MB in
#: advanced mode. The 256 KB cap would 413 every non-trivial assessment, so these
#: paths get a larger ceiling. The geometry is never forwarded to the model; it is
#: digested server-side in app.assistant._summarize.
MAX_ASSISTANT_BODY_BYTES = 4 * 1024 * 1024
_ASSISTANT_PREFIX = "/api/assistant/"

#: Expensive routes, and how many calls per window. A simulation is a minute of
#: numerical work, so this is deliberately tight.
RATE_LIMITS: dict[str, tuple[int, float]] = {
    "POST /api/v1/simulations": (5, 60.0),
    "POST /api/v1/analysis": (10, 60.0),
    "POST /api/v1/data/catalog/rescan": (2, 300.0),
}


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Give every request an id, log it, and hand the id back on the response."""

    async def dispatch(self, request: Request, call_next: Callable):
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex[:16]
        request.state.correlation_id = correlation_id

        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        response.headers[CORRELATION_HEADER] = correlation_id
        logger.info(
            "%s %s -> %d in %.0f ms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            extra={
                "correlation_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 1),
            },
        )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_bytes = int(declared)
            except ValueError:
                return envelope(request, 400, "invalid_request", "Content-Length is not a number.")
            limit = (
                MAX_ASSISTANT_BODY_BYTES
                if request.url.path.startswith(_ASSISTANT_PREFIX)
                else MAX_BODY_BYTES
            )
            if declared_bytes > limit:
                return envelope(
                    request,
                    413,
                    "request_too_large",
                    f"The request body exceeds {limit // 1024} KB.",
                )
        return await call_next(request)


#: The live limiter, so tests can clear its window. The app is a module-level
#: singleton, so without this one test's POSTs would rate-limit the next one's.
_limiter: "RateLimitMiddleware | None" = None


def reset_rate_limits() -> None:
    if _limiter is not None:
        _limiter._hits.clear()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A sliding window per (client, route). In-process; see the module docstring."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        global _limiter
        _limiter = self

    async def dispatch(self, request: Request, call_next: Callable):
        route = f"{request.method} {request.url.path}"
        limit = RATE_LIMITS.get(route)
        if limit is None:
            return await call_next(request)

        allowed, window = limit
        client = request.client.host if request.client else "unknown"
        key = (client, route)
        now = time.monotonic()

        hits = self._hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()

        if len(hits) >= allowed:
            retry_after = max(1, int(window - (now - hits[0])))
            response: JSONResponse = envelope(
                request,
                429,
                "rate_limited",
                f"Too many calls to {route}. This route runs a numerical simulation and is "
                f"limited to {allowed} per {window:.0f} s. Poll the job you already started "
                f"instead of starting another.",
                {"retry_after_seconds": retry_after, "limit": allowed, "window_seconds": window},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response

        hits.append(now)
        return await call_next(request)


def install(app: FastAPI) -> None:
    # Starlette runs middleware in reverse registration order, so the correlation
    # id must be added last to be outermost -- otherwise a rate-limited or
    # oversized request is rejected before it has an id to report.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(CorrelationMiddleware)
