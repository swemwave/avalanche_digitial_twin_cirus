"""Compatibility import for :mod:`avycore.hazard.zone`."""

import sys
from avycore.hazard import zone as _implementation

sys.modules[__name__] = _implementation
