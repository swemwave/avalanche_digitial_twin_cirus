"""Validate the baked runtime without importing offline geospatial dependencies."""

from __future__ import annotations

from app.bake_identity import processing_manifest, validate_bake
from app.core.settings import get_settings


def main() -> None:
    settings = get_settings()
    meta = validate_bake(
        settings.runtime_root / "baked",
        expected_processing_sha256=processing_manifest(settings.project_root)["sha256"],
    )
    print(f"Valid bake: {meta['identity']['bake_sha256']}")


if __name__ == "__main__":
    main()
