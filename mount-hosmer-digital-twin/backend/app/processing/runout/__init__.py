"""Offline-only external runout engine adapters.

Adapters are intentionally not imported here: callers opt into a specific
offline dependency closure by importing its module.  Serving entrypoints never
import this package.
"""

from .process import ExternalModelProcessError

__all__ = ["ExternalModelProcessError"]
