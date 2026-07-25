r"""The **assistant** service: the local AI, and nothing else.

This is the light half. It carries no baked terrain, no ``.npy`` layers, no tiles,
and it never computes a hazard number -- when a scenario question needs one, it
calls the assess service over HTTP (``AVALANCHE_ASSESS_URL``).

    uvicorn app.main_assistant:app --host 0.0.0.0 --port 8001

Two outbound dependencies, both configured by environment variable and both
degrading to a clean 503 rather than a wrong answer:

* ``AVALANCHE_OLLAMA_URL``  -- the language model. Pointing this at a tunnel is
  what lets Ollama keep running on a laptop while this service runs in the cloud.
* ``AVALANCHE_ASSESS_URL``  -- the assess service. Unset means in-process, which
  only works where baked terrain is present (local development).
"""

from __future__ import annotations

import os

from app.api import assistant as assistant_routes
from app.service import assess_backend, create_app


def _health_extra() -> dict[str, object]:
    """Report both upstreams. Configuration only -- a health check must not be a
    liveness probe for someone else's service, or a laptop asleep at 3am reads as
    this service being down."""
    payload: dict[str, object] = {
        "ollama_model": os.environ.get("AVALANCHE_OLLAMA_MODEL", "llama3.1:8b"),
        "ollama_configured": bool(os.environ.get("AVALANCHE_OLLAMA_URL", "").strip()),
    }
    payload.update(assess_backend())
    return payload


app = create_app(
    title="Mount Hosmer Digital Twin -- Assistant Service",
    description=(
        "Local AI assistant over an avalanche terrain assessment: explains a result and "
        "runs what-if scenarios. It never invents a hazard number and never gives travel "
        "advice. Experimental and NOT an operational avalanche forecast."
    ),
    routers=[assistant_routes.router],
    health_extra=_health_extra,
)
