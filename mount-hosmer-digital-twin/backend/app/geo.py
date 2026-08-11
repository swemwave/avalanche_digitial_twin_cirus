"""Compatibility import for :mod:`avycore.hazard.geometry`."""

import sys
from avycore.hazard import geometry as _implementation

sys.modules[__name__] = _implementation
