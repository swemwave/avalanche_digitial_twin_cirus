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
from app.api import mountains as mountain_routes
from app.api import predictions as prediction_routes
from app.api import terrain as terrain_routes
from app.mountains import sweep_orphaned_staging
from app.service import baked_stamp, create_app
from app.core.settings import get_settings


def _clear_abandoned_bakes() -> None:
    """Drop staging directories left behind by a bake that was killed.

    Mirrors :mod:`app.main`'s own startup hook: uploaded-mountain staging lives
    under the shared EFS mount both this service and ``bakeworker`` write to, so
    a bake killed mid-run on either side leaves its ``.baked-build-*`` directory
    for whichever service starts next to clean up.
    """
    sweep_orphaned_staging(get_settings())


app = create_app(
    title="Mount Hosmer Digital Twin -- Assess Service",
    description=(
        "Terrain metadata, baked map tiles, and the simplified avalanche release/runout "
        "model. Experimental and NOT an operational avalanche forecast."
    ),
    # Prediction products are read-only files. They belong here rather than in the
    # assistant because the ALB routes everything under /api/* to this service, and
    # because reading a product must sit beside the model it describes. Serving them
    # launches no engine: app.predictions imports pydantic and the standard library.
    #
    # mountain_routes is what exposes uploads here at all: this image ships without
    # rasterio, so AVALANCHE_BAKE_WORKER_URL (set on this service's task definition)
    # is what lets app.mountain_jobs dispatch probe/bake work to the bakeworker
    # service instead of running it locally -- see that module's docstring.
    routers=[terrain_routes.router, assess_routes.router, prediction_routes.router, mountain_routes.router],
    # `baked: false` means the bake has not run. The service still answers so it can
    # say so -- unbuilt is a different condition from broken (invariant I3).
    # `bake_generated_at` names *which* bake this image carries, so a stale deploy is
    # visible from outside rather than silently served as current.
    health_extra=baked_stamp,
    on_startup=_clear_abandoned_bakes,
)
