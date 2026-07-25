"""The Stage 3 HTTP API: errors, middleware, and one router module per service.

Deliberately empty of imports. This package used to eagerly pull in every router,
which was harmless when there was one process serving all of them -- but now means
importing *any* route module drags in *all* of them, and the assistant service
would load the terrain reader and the runout engine it never calls. Submodules are
imported explicitly by whoever needs them (``from app.api import terrain``), which
works the same way and keeps each service's import graph honest.
"""
