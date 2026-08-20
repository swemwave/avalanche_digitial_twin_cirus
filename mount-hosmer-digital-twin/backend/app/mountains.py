r"""Storage layout, identity, and registry for user-uploaded mountains.

Mount Hosmer is baked from a reviewed pack under ``runtime/baked/`` and no request
path may ever write there. An uploaded mountain gets its own tree instead::

    runtime/mountains/
      registry.json                    id -> status, name, timestamps, error
      <id>/
        source/metadata/aoi.geojson    synthesized from the DEM footprint
        source/static/dem.tif          the upload, after validation
        source/static/landcover.tif    optional second upload
        pack.json                      synthesized MountainPack
        baked/                         promote_bake target
        bake.log

Two constraints shape this module, both verified against the running code rather
than assumed:

* ``bake_identity.source_lineage`` refuses any bake input outside the data root,
  so an upload must be *copied into* ``<id>/source/`` and baked from there. It can
  never be referenced where it landed.
* This module runs inside the serving process, which is deliberately free of
  rasterio, pyproj, and GDAL. It therefore never imports
  :mod:`app.processing.mountain_pack` -- that module's own docstring records that
  the running service never imports it, and pack synthesis happens out-of-process
  in the bake environment instead (see :mod:`app.processing.upload_probe`).

The slug pattern below is duplicated from ``MountainPack.slug_id`` for that same
reason. ``test_uploaded_mountain_id_matches_the_pack_slug_rule`` pins the two
together so the copy cannot drift.
"""

from __future__ import annotations

import json
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.core.settings import Settings

#: Same rule as ``MountainPack.slug_id``. An id reaches the filesystem, so it is
#: re-validated here before any path is built from it, not merely on the way in.
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: Hard ceiling on a single uploaded raster, enforced while streaming rather than
#: from the declared Content-Length -- ``api.middleware`` reads only the header and
#: its own docstring records that a chunked request skips the check entirely.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

#: Pixels in the *analysis* grid, after resolution snapping. This is the knob that
#: decides whether a bake takes seconds or many minutes. Measured on this codebase:
#: a 300x300 DEM-only bake is ~1.6 s, and Mount Hosmer's 2400x2400 grid with imagery
#: is ~132 s, so cost is close to linear at ~23 us/cell. 4M cells keeps an upload
#: under about 90 seconds.
MAX_ANALYSIS_CELLS = 4_000_000

#: Uploaded mountains coexist with Hosmer rather than replacing it, so the cap is
#: on disk rather than on correctness: Hosmer's bake is ~509 MB and an uploaded one
#: is smaller but not small. Replacing a mountain means a new id plus a delete --
#: never a re-bake in place, which on Windows hits the mmap PermissionError that
#: ``bake_identity.promote_bake`` documents.
MAX_UPLOADED_MOUNTAINS = 3

MountainStatus = Literal["validating", "baking", "ready", "failed"]

REGISTRY_SCHEMA = "avycore-uploaded-mountain-registry-v1"


class MountainError(ValueError):
    """A request-visible problem with an uploaded mountain or its id."""


class MountainNotFoundError(MountainError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Identity -----------------------------------------------------------------


def slugify(name: str) -> str:
    """Best-effort slug from a user-supplied display name.

    The result is *proposed*, never trusted: :func:`new_mountain_id` appends
    entropy and re-validates the whole string against :data:`SLUG_PATTERN`.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return hyphenated[:48].strip("-")


def new_mountain_id(name: str) -> str:
    """Generate a server-side id. User text never reaches a path unfiltered."""
    suffix = secrets.token_hex(3)
    stem = slugify(name) or "mountain"
    candidate = f"{stem}-{suffix}"
    return validate_mountain_id(candidate)


def validate_mountain_id(mountain_id: str) -> str:
    """Re-validate an id against the pack slug rule before it touches a path."""
    if not isinstance(mountain_id, str) or not SLUG_PATTERN.fullmatch(mountain_id):
        raise MountainError(
            f"Mountain id {mountain_id!r} is not a lowercase hyphenated slug."
        )
    if len(mountain_id) > 80:
        raise MountainError("Mountain id is longer than the pack allows (80 characters).")
    return mountain_id


# --- Layout -------------------------------------------------------------------


def mountains_root(settings: Settings) -> Path:
    return settings.runtime_root / "mountains"


def mountain_root(settings: Settings, mountain_id: str) -> Path:
    """The per-mountain tree. Validates the id first, then contains the result."""
    root = mountains_root(settings).resolve()
    resolved = (root / validate_mountain_id(mountain_id)).resolve()
    if resolved.parent != root:
        raise MountainError(f"Mountain id {mountain_id!r} escapes the mountains root.")
    return resolved


def source_root(settings: Settings, mountain_id: str) -> Path:
    """The per-upload read-only data root the bake reads from."""
    return mountain_root(settings, mountain_id) / "source"


def pack_path(settings: Settings, mountain_id: str) -> Path:
    return mountain_root(settings, mountain_id) / "pack.json"


def bake_log_path(settings: Settings, mountain_id: str) -> Path:
    return mountain_root(settings, mountain_id) / "bake.log"


def runtime_root_for(settings: Settings, mountain_id: str | None) -> Path:
    """Resolve which runtime root serves a request.

    ``None`` is Mount Hosmer -- the reviewed bake at ``settings.runtime_root`` --
    so every existing caller and both e2e specs keep their current behaviour
    without passing anything.
    """
    if mountain_id is None:
        return settings.runtime_root
    return mountain_root(settings, mountain_id)


# --- Registry -----------------------------------------------------------------


@dataclass(frozen=True)
class MountainRecord:
    id: str
    name: str
    status: MountainStatus
    created_at_utc: str
    updated_at_utc: str
    stage: str | None = None
    error: str | None = None
    pack_sha256: str | None = None
    bake_sha256: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: False when the upload carried no land cover. The release model requires a
    #: forest layer, so such a mountain renders in 3D but cannot be assessed at
    #: all -- coverage is 0%, not merely degraded. Recorded per mountain so the UI
    #: can say so up front rather than after an empty assessment.
    assessable: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "stage": self.stage,
            "error": self.error,
            "pack_sha256": self.pack_sha256,
            "bake_sha256": self.bake_sha256,
            "warnings": list(self.warnings),
            "assessable": self.assessable,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "MountainRecord":
        return cls(
            id=validate_mountain_id(str(payload["id"])),
            name=str(payload.get("name", "")),
            status=str(payload.get("status", "failed")),  # type: ignore[arg-type]
            created_at_utc=str(payload.get("created_at_utc", "")),
            updated_at_utc=str(payload.get("updated_at_utc", "")),
            stage=payload.get("stage"),
            error=payload.get("error"),
            pack_sha256=payload.get("pack_sha256"),
            bake_sha256=payload.get("bake_sha256"),
            warnings=tuple(str(item) for item in payload.get("warnings", ())),
            assessable=bool(payload.get("assessable", True)),
        )


def registry_path(settings: Settings) -> Path:
    return mountains_root(settings) / "registry.json"


def read_registry(settings: Settings) -> dict[str, MountainRecord]:
    """Load the registry. A missing or unreadable file is an empty registry.

    An unreadable registry must not take the whole API down: Mount Hosmer is
    served from a different root entirely and does not depend on this file.
    """
    path = registry_path(settings)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("mountains", [])
    except (OSError, ValueError):
        return {}
    records: dict[str, MountainRecord] = {}
    for entry in entries:
        try:
            record = MountainRecord.from_json(entry)
        except (MountainError, KeyError, TypeError):
            continue
        records[record.id] = record
    return records


def write_registry(settings: Settings, records: dict[str, MountainRecord]) -> None:
    """Replace the registry atomically, so a crash never leaves a partial file."""
    path = registry_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records.values(), key=lambda record: record.created_at_utc)
    document = {
        "schema": REGISTRY_SCHEMA,
        "mountains": [record.to_json() for record in ordered],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
    temporary.replace(path)


def put_record(settings: Settings, record: MountainRecord) -> MountainRecord:
    records = read_registry(settings)
    records[record.id] = record
    write_registry(settings, records)
    return record


def update_record(settings: Settings, mountain_id: str, **changes: Any) -> MountainRecord:
    records = read_registry(settings)
    existing = records.get(validate_mountain_id(mountain_id))
    if existing is None:
        raise MountainNotFoundError(f"No uploaded mountain {mountain_id!r}.")
    updated = MountainRecord(
        **{**existing.to_json(), **{"warnings": list(existing.warnings)}, **changes}  # type: ignore[arg-type]
    )
    records[updated.id] = updated
    write_registry(settings, records)
    return updated


def get_record(settings: Settings, mountain_id: str) -> MountainRecord:
    record = read_registry(settings).get(validate_mountain_id(mountain_id))
    if record is None:
        raise MountainNotFoundError(f"No uploaded mountain {mountain_id!r}.")
    return record


def delete_mountain(settings: Settings, mountain_id: str) -> None:
    """Remove a mountain's tree and its registry entry."""
    import shutil

    root = mountain_root(settings, mountain_id)
    records = read_registry(settings)
    if mountain_id not in records and not root.exists():
        raise MountainNotFoundError(f"No uploaded mountain {mountain_id!r}.")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    records.pop(mountain_id, None)
    write_registry(settings, records)


def sweep_orphaned_staging(settings: Settings) -> list[Path]:
    """Delete staging directories left by a killed bake.

    ``bake.py`` stages into ``.baked-build-*`` and promotes only a validated
    result, so a crash leaves no half-baked mountain -- but it does leave the
    staging directory behind. Called at startup.
    """
    import shutil

    removed: list[Path] = []
    root = mountains_root(settings)
    if not root.is_dir():
        return removed
    for mountain_dir in root.iterdir():
        if not mountain_dir.is_dir():
            continue
        for staging in mountain_dir.glob(".baked-build-*"):
            shutil.rmtree(staging, ignore_errors=True)
            removed.append(staging)
    return removed
