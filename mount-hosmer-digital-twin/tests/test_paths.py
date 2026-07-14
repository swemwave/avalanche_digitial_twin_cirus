from __future__ import annotations

from pathlib import Path

import pytest

from app.core.paths import UnsafePathError, safe_source_path


def test_safe_source_path_allows_paths_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    resolved = safe_source_path(root, "metadata/download_manifest.csv")
    assert resolved == (root / "metadata" / "download_manifest.csv").resolve()


def test_safe_source_path_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        safe_source_path(root, "../outside.txt")
