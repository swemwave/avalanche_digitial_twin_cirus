"""Test isolation for the two module-level caches.

`get_settings()` memoises Settings in a module global, and pytest imports every
test module at collection time. So the first thing that touches settings -- an
import, a fixture, anything -- freezes the environment for the whole session, and a
later test that monkeypatches `AVALANCHE_DATA_ROOT` to a temp directory finds its
patch silently ignored. It does not fail loudly; it quietly runs against the real
46 GB data root and asserts the wrong thing. `test_api_security` was doing exactly
that: pointing at a temp root with no events, and getting back the two real ones.

Clearing the cache before every test makes each one read the environment it
actually set up.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings() -> None:
    """Every test reads the environment it configured, not the one collection saw."""
    from app.core import settings as settings_module

    settings_module._cached = None
    yield
    settings_module._cached = None


@pytest.fixture(autouse=True)
def _isolate_rate_limits() -> None:
    """The FastAPI app is a module-level singleton, so its limiter is shared."""
    from app.api.middleware import reset_rate_limits

    reset_rate_limits()
    yield
    reset_rate_limits()
