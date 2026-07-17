"""The Stage 3 CLI. One offline command: bake the terrain artifacts.

Everything else the app does at runtime is served from the bake, so the only
offline step is the bake itself. See ``app/bake.py``.

    python -m app.cli bake            # build runtime/baked/ (tiles + .npy + meta)
    python -m app.cli bake --force    # rebuild even if a bake already exists
"""

from __future__ import annotations

import argparse


def bake_command(args: argparse.Namespace) -> int:
    from app.bake import bake
    from app.core.settings import get_settings

    settings = get_settings()
    settings.validate(require_data_root=True)
    bake(settings, force=args.force)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bake = subparsers.add_parser("bake", help="Bake the terrain artifacts (offline, one-time)")
    bake.add_argument("--force", action="store_true", help="Rebuild even if a bake already exists")
    bake.set_defaults(func=bake_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
