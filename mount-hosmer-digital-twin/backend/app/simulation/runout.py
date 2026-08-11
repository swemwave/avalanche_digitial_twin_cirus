"""Compatibility import for :mod:`avycore.hazard.runout`."""

import sys
from avycore.hazard import runout as _implementation

sys.modules[__name__] = _implementation
