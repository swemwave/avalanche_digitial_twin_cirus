"""The versioned HTTP API. The legacy ``/api/*`` routes live in ``app.main``."""

from app.api import errors, middleware, schemas, v1

__all__ = ["errors", "middleware", "schemas", "v1"]
