"""Path safety for the one place that reads source data: the bake.

Invariant I1 says ``DATA\\`` is strictly read-only, and that a source path may never
escape the configured data root. :func:`safe_source_path` is what enforces it.

This module used to carry the whole runtime directory layout of the pre-Stage-3
platform (``catalog/``, ``processed/*``, ``previews/``, ``exports/``,
``simulations/``, ``analyses/``) plus helpers to resolve and relativize paths
inside it. Stage 3 writes exactly one thing -- ``runtime/baked/`` -- and the bake
creates it directly, so all of that had no callers and has been removed.
"""

from __future__ import annotations

from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a source path would escape the configured data root."""


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_source_path(data_root: Path, candidate: str | Path) -> Path:
    """Resolve a path the application intends to *read* from the source data root.

    Traversal (``../``) and absolute paths pointing outside ``data_root`` are
    refused rather than clamped, so a bad input fails loudly instead of quietly
    reading the wrong file.
    """
    raw = Path(candidate)
    resolved = raw.resolve() if raw.is_absolute() else (data_root / raw).resolve()
    if not is_relative_to(resolved, data_root):
        raise UnsafePathError(f"path escapes data root: {candidate}")
    return resolved
