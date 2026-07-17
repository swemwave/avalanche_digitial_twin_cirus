"""Test isolation for the settings cache.

`get_settings()` memoises Settings in a module global, and pytest imports every
test module at collection time. So the first thing that touches settings -- an
import, a fixture, anything -- freezes the environment for the whole session, and a
later test that monkeypatches `AVALANCHE_DATA_ROOT` to a temp directory finds its
patch silently ignored. It does not fail loudly; it quietly runs against the real
data root and asserts the wrong thing.

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
