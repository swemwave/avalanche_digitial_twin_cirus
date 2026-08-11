r"""The **assess** service: terrain, tiles, and the assessment model.

This is the heavy half. It carries the baked artifacts -- the 8 ``.npy`` layers,
``meta.json``, and the tile pyramid -- and it is the only service that computes a
hazard number, so it is also the only one that has to attach the ``DISCLAIMER``.

    uvicorn app.main_assess:app --host 0.0.0.0 --port 8000

Deliberately no assistant routes: this service holds no Ollama configuration and
makes no outbound calls at all.
"""

from __future__ import annotations

from app.api import assess as assess_routes
from app.api import terrain as terrain_routes
from app.service import baked_stamp, create_app

app = create_app(
    title="Mount Hosmer Digital Twin -- Assess Service",
    description=(
        "Terrain metadata, baked map tiles, and the simplified avalanche release/runout "
        "model. Experimental and NOT an operational avalanche forecast."
    ),
    routers=[terrain_routes.router, assess_routes.router],
    # `baked: false` means the bake has not run. The service still answers so it can
    # say so -- unbuilt is a different condition from broken (invariant I3).
    # `bake_generated_at` names *which* bake this image carries, so a stale deploy is
    # visible from outside rather than silently served as current.
    health_extra=baked_stamp,
)
