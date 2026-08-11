from __future__ import annotations

from pathlib import Path

import pytest

from app.bake_preservation import (
    BakePreservationError,
    inventory_directory,
    preserve_bake,
    validate_preservation,
)


def test_complete_bake_preservation_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    baked = tmp_path / "baked"
    (baked / "layers").mkdir(parents=True)
    (baked / "layers" / "elevation.npy").write_bytes(b"elevation")
    (baked / "meta.json").write_text(
        '{"identity":{"bake_sha256":"' + "a" * 64 + '"}}', encoding="utf-8"
    )
    initial = inventory_directory(baked)

    target = preserve_bake(baked, tmp_path)

    assert preserve_bake(baked, tmp_path) == target
    assert validate_preservation(target) == initial
    assert (target / "baked" / "layers" / "elevation.npy").read_bytes() == b"elevation"


def test_preserved_bake_rejects_corruption(tmp_path: Path) -> None:
    baked = tmp_path / "baked"
    baked.mkdir()
    (baked / "meta.json").write_text("{}", encoding="utf-8")
    target = preserve_bake(baked, tmp_path)
    (target / "baked" / "meta.json").write_text("corrupt", encoding="utf-8")

    with pytest.raises(BakePreservationError, match="unreadable|differ"):
        validate_preservation(target)
