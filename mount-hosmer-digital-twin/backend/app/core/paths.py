from __future__ import annotations

from pathlib import Path

from app.core.settings import Settings

#: Every directory the application is allowed to write to, relative to the
#: runtime root. Source data (``settings.data_root``) is never in this list --
#: it is strictly read-only (invariant I1).
RUNTIME_DIRS = (
    "catalog",
    "cache",
    "processed/static",
    "processed/terrain",
    "processed/events",
    "processed/dynamic",
    "processed/models",
    "previews",
    "previews/layers",
    "exports",
    "logs",
    "tiles",
    "derived",
    "simulations",
    "analyses",
    "tmp",
)


class UnsafePathError(ValueError):
    """Raised when a source path would escape the configured data root."""


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_source_path(data_root: Path, candidate: str | Path) -> Path:
    raw = Path(candidate)
    resolved = raw.resolve() if raw.is_absolute() else (data_root / raw).resolve()
    if not is_relative_to(resolved, data_root):
        raise UnsafePathError(f"path escapes data root: {candidate}")
    return resolved


def safe_output_path(runtime_root: Path, candidate: str | Path) -> Path:
    """Resolve a path that the application intends to *write*.

    Mirrors :func:`safe_source_path` for the output side, so a caller can never
    be tricked into writing outside the runtime root.
    """
    raw = Path(candidate)
    resolved = raw.resolve() if raw.is_absolute() else (runtime_root / raw).resolve()
    if not is_relative_to(resolved, runtime_root):
        raise UnsafePathError(f"path escapes runtime root: {candidate}")
    return resolved


def relative_source_path(data_root: Path, path: Path) -> str:
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def relative_runtime_path(settings: Settings, path: Path) -> str:
    """Path relative to the project root, for display and for API payloads.

    Falls back to the bare filename when the path lives outside the project (for
    example a runtime root mounted on a different volume in a container), so an
    absolute host path is never leaked through the API.
    """
    try:
        return path.resolve().relative_to(settings.project_root.resolve()).as_posix()
    except ValueError:
        return path.name


def ensure_runtime_dirs(runtime_root: Path) -> None:
    for child in RUNTIME_DIRS:
        (runtime_root / child).mkdir(parents=True, exist_ok=True)


def terrain_dir(settings: Settings) -> Path:
    return settings.runtime_root / "processed" / "terrain"


def models_dir(settings: Settings) -> Path:
    return settings.runtime_root / "processed" / "models"


def analyses_dir(settings: Settings) -> Path:
    return settings.runtime_root / "analyses"


def simulations_dir(settings: Settings) -> Path:
    return settings.runtime_root / "simulations"
