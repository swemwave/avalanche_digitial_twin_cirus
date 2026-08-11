"""Compatibility import for :mod:`avycore.hazard.risk`."""

import sys
from avycore.hazard import risk as _implementation

sys.modules[__name__] = _implementation
