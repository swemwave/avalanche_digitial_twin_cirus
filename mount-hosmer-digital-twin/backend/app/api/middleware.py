"""Correlation ids, structured logging, and a request-size limit.

Two small pieces of plumbing wrap every request:

* a correlation id, logged with the method/path/status/duration, and handed back
  on the response so a user reporting "it broke" gives one string that finds the
  exact request in the log; and
* a body-size ceiling, so a runaway upload is refused before it is read.

That is the whole middleware stack. In particular there is **no rate limiter**, and
that is now a considered trade rather than the tautology it used to be. The original
reasoning was "single-user `.exe`, no multi-tenant surface to protect" -- true then,
and no longer true: `deploy/` puts this behind a public, unauthenticated ALB, where
`POST /api/assess` is a ~5 s, ~1.5 GB request anyone who learns the hostname can call.

It stays unprotected anyway, for reasons that hold only under the actual usage:
Fargate bills per task-hour rather than per request, so load cannot amplify cost;
the worst case is the container hitting its 4 GB ceiling and ECS restarting it,
which is self-healing; the hostname is a random AWS DNS name that is not indexed;
and the stack is raised for a few hours at a time and then destroyed. Adding auth
would also break the one property the deployment is for -- open one URL and it works.

**If that changes -- a permanent deployment, a real hostname, or anything sensitive --
this is the first thing to revisit.** See `docs/limitations.md`.

Related and deliberately not fixed: the body-size limit below reads `Content-Length`,
so a chunked request declares no length and skips the check. Closing that means
consuming the stream inside a `BaseHTTPMiddleware`, which is fiddly enough to risk
breaking body handling on every route -- a worse trade than the hole it closes, for a
tool no browser sends chunked requests to.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request
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


def install(app: FastAPI) -> None:
    # Starlette runs middleware in reverse registration order, so the correlation
    # id must be added last to be outermost -- otherwise an oversized request is
    # rejected before it has an id to report.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(CorrelationMiddleware)
