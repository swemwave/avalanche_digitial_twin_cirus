r"""The combined Stage 3 app -- terrain + assess + assistant in one process.

This is the local-development and one-click-launcher entrypoint, and it is
unchanged in behaviour: ``uvicorn app.main:app`` still serves the whole API off
the baked artifacts under ``runtime/baked/`` -- no jobs, no database, no ``DATA\``
access, no rasterio.

For a split deployment the same routers are served by :mod:`app.main_assess` and
:mod:`app.main_assistant` instead. Route paths are identical in both shapes.
"""

from __future__ import annotations

from app.api import stage3 as api_stage3
from app.service import baked_stamp, create_app
from app.core.settings import get_settings

app = create_app(
    title="Mount Hosmer Avalanche Digital Twin API",
    description=(
        "An experimental, terrain- and conditions-based avalanche digital twin: 3D terrain, "
        "runout simulation, a simplified release estimate, and a local AI assistant. "
        "NOT an operational avalanche forecast."
    ),
    routers=[api_stage3.router],
    health_extra=baked_stamp,
)

# The one-screen Next app is a static export. Keep this mount last so every API
# and documentation route wins before the SPA fallback.
frontend = get_settings().project_root / "frontend" / "out"
if frontend.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
