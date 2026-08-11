"""Path containment guard retained for source-manifest integrations."""

from pathlib import Path


class UnsafePathError(ValueError):
    pass


def safe_source_path(data_root: Path, candidate: str | Path) -> Path:
    root = data_root.resolve()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise UnsafePathError(f"Source path escapes the data root: {candidate}")
    return resolved
