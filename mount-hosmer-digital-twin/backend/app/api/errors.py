"""One error shape, and one place that decides what an exception means.

Every failure the API can produce comes out of here in the same envelope, carrying
the correlation id, so a user reporting "it broke" hands over one string that finds
the exact request in the log.

The mapping matters more than it looks. A `FileNotFoundError` from a service is a
404, not a 500: it means the analysis has not been run, which is the caller's
situation, not a server fault. A `ModelConfigError` is a 500 even though it is a
configuration mistake, because the deployment is genuinely broken and must not
quietly serve numbers from a fallback -- there is no fallback for a scientific
parameter.

**What is deliberately NOT mapped.** There used to be blanket `KeyError -> 404` and
`ValueError -> 400` handlers, from when `/api/v1/layers/{name}` let a caller name a
layer and an unknown one really was "not found". Stage 3 has no such route: every
`bt.layer()` call passes a string literal, and the release size and simulation mode
are validated by the request model before anything downstream sees them. So those
handlers could no longer fire for a caller's mistake -- only for OUR bug, which they
then disguised as a 404 the client "caused", with no traceback logged. An internal
`KeyError` now falls through to the unhandled handler: a 500, a correlation id, and
a full traceback in the log. That is the whole point of having one.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.model_config import ModelConfigError
from app.core.settings import ConfigurationError

logger = logging.getLogger("avalanche.api")


def envelope(
    request: Request, status_code: int, code: str, message: str, detail: dict[str, Any] | None = None
) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", None)
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "status": status_code,
            "correlation_id": correlation_id,
        }
    }
    if detail:
        body["error"]["detail"] = detail
    return JSONResponse(
        status_code=status_code,
        content=body,
        headers={"X-Correlation-ID": correlation_id} if correlation_id else None,
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        return envelope(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() is not JSON-serialisable as it stands: a failing
        # `model_validator` puts the raw ValueError object into `ctx`, and rendering
        # it raises inside the error handler itself -- so a 422 came back as a 500
        # with no useful message at all. Only the fields worth showing are kept.
        errors = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ()) if part != "body"),
                "message": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
            for item in exc.errors()
        ]
        return envelope(
            request,
            422,
            "invalid_request",
            "The request did not validate.",
            {"errors": errors},
        )

    @app.exception_handler(FileNotFoundError)
    async def _missing(request: Request, exc: FileNotFoundError) -> JSONResponse:
        # Not a server fault: the thing has not been produced yet.
        return envelope(request, 404, "not_found", str(exc))

    @app.exception_handler(ModelConfigError)
    async def _model_config(request: Request, exc: ModelConfigError) -> JSONResponse:
        # Deliberately a 500. A missing scientific parameter is a broken deployment,
        # and the system must not paper over it with a default.
        logger.error("model configuration is unusable: %s", exc)
        return envelope(request, 500, "model_config_error", str(exc))

    @app.exception_handler(ConfigurationError)
    async def _configuration(request: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("configuration is unusable: %s", exc)
        return envelope(request, 500, "configuration_error", str(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = getattr(request.state, "correlation_id", None)
        logger.error(
            "unhandled exception on %s %s [%s]\n%s",
            request.method,
            request.url.path,
            correlation_id,
            traceback.format_exc(),
        )
        return envelope(
            request,
            500,
            "internal_error",
            "The request failed unexpectedly. The correlation id identifies it in the server log.",
        )
