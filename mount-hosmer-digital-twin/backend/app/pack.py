"""Read-only command for validating a Mountain Pack and its declared sources."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.settings import get_settings
from app.processing.mountain_pack import load_mountain_pack, validate_declared_sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a mountain pack without baking it.")
    parser.add_argument("--pack", type=Path, help="Pack JSON; defaults to configured pack.")
    parser.add_argument("--data-root", type=Path, help="Read-only asset root override.")
    args = parser.parse_args(argv)

    settings = get_settings()
    updates = {}
    if args.pack:
        updates["mountain_pack_path"] = args.pack.resolve()
    if args.data_root:
        updates["data_root"] = args.data_root.resolve()
    if updates:
        settings = settings.model_copy(update=updates)

    settings.validate(require_data_root=True)
    pack, identity = load_mountain_pack(settings)
    warnings = [*pack.warnings(), *validate_declared_sources(pack, settings.data_root)]
    print(
        f"Valid mountain pack {pack.id!r}: {pack.grid.analysis_crs}, "
        f"{pack.grid.resolution_m:g} m, {len(pack.assets)} declared assets, "
        f"sha256={identity['sha256']}"
    )
    for warning in warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

