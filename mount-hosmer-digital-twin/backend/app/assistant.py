"""Compatibility import for :mod:`avycore.assistant.core`."""

import sys
from avycore.assistant import core as _implementation

sys.modules[__name__] = _implementation
