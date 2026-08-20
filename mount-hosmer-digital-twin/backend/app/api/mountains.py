r"""Upload a mountain: receive, validate, bake, and list.

The reviewed Mount Hosmer bake under ``runtime/baked/`` is read-only to every
route here. An upload gets its own tree under ``runtime/mountains/<id>/`` and is
baked from a data root that contains nothing but its own files -- which is not a
tidiness preference: ``bake_identity.source_lineage`` refuses any bake input
outside the data root, so an upload that stayed where it landed could not be baked
at all.

Three properties this module is responsible for:

* **The id never comes from the client.** It is generated server-side and
  re-validated against the pack's own slug rule before any path is built from it.
* **The byte ceiling is enforced while streaming.** ``api.middleware`` checks only
  the declared ``Content-Length`` and its docstring admits a chunked request skips
  the check entirely, so the real limit is applied here, chunk by chunk, and the
  partial file is removed when it is passed.
* **Nothing geospatial is imported.** Validation and baking both happen in the
  bake environment via :mod:`app.mountain_jobs`, so this service keeps its
  zero-rasterio property.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from app.api.models import ApiModel
from app.core.settings import Settings, get_settings
from app.mountains import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOADED_MOUNTAINS,
    MountainError,
    MountainNotFoundError,
    MountainRecord,
    delete_mountain,
    get_record,
    mountain_root,
    new_mountain_id,
    put_record,
    read_registry,
    source_root,
    utc_now_iso,
)

router = APIRouter(prefix="/api", tags=["mountains"])

#: Read from the client in 1 MB pieces. Large enough that a 500 MB upload is not
#: half a million iterations, small enough that the ceiling is enforced promptly.
_CHUNK_BYTES = 1024 * 1024

#: Guards the create path so two simultaneous uploads cannot both pass the cap
#: check and both write. The bake itself is serialized separately, in
#: :mod:`app.mountain_jobs`.
_create_lock = threading.Lock()


class MountainSummary(ApiModel):
    """One mountain's state. Every field is always sent, so none is optional.

    Nullable fields stay required-but-null rather than absent: a client that has
    to guess whether ``warnings`` is missing or empty will eventually guess that
    an absent list means "no caveats", which is exactly the wrong default here.
    """

    id: str
    name: str
    status: str
    created_at_utc: str
    updated_at_utc: str
    stage: str | None
    error: str | None
    bake_sha256: str | None
    warnings: list[str]
    #: False when no land cover was supplied. The release model requires a forest
    #: layer, so such a mountain renders in 3D and assesses nothing at all.
    assessable: bool
    #: Uploaded terrain carries none of the provenance the reviewed mountain has:
    #: no acquisition manifest, so ``source_lineage`` stamps every file
    #: ``computed_by_bake`` rather than ``verified_download_manifest``. The flag
    #: exists so the UI can never present the two as equivalent.
    verified_source: bool


class MountainList(ApiModel):
    mountains: list[MountainSummary]
    upload_available: bool
    upload_unavailable_reason: str | None
    max_mountains: int
    max_upload_bytes: int


def _summary(record: MountainRecord) -> MountainSummary:
    return MountainSummary(
        id=record.id,
        name=record.name,
        status=record.status,
        created_at_utc=record.created_at_utc,
        updated_at_utc=record.updated_at_utc,
        stage=record.stage,
        error=record.error,
        bake_sha256=record.bake_sha256,
        warnings=list(record.warnings),
        assessable=record.assessable,
        verified_source=False,
    )


def _stream_to_disk(upload: UploadFile, destination: Path, *, remaining_budget: int) -> int:
    """Write an upload to disk, aborting past the ceiling. Returns bytes written."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with destination.open("wb") as sink:
            while True:
                chunk = upload.file.read(_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > remaining_budget:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"The upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit. "
                            "Clip the raster to the area you want to model."
                        ),
                    )
                sink.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"The upload could not be stored: {exc}") from exc
    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    return written


@router.get("/mountains", response_model=MountainList, operation_id="listMountains")
def list_mountains() -> MountainList:
    """Every uploaded mountain, plus whether this deployment accepts uploads."""
    from app.mountain_jobs import upload_available

    settings = get_settings()
    available, reason = upload_available(settings)
    records = read_registry(settings)
    return MountainList(
        mountains=[_summary(record) for record in sorted(
            records.values(), key=lambda item: item.created_at_utc
        )],
        upload_available=available,
        upload_unavailable_reason=None if available else reason,
        max_mountains=MAX_UPLOADED_MOUNTAINS,
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )


@router.get("/mountains/{mountain_id}", response_model=MountainSummary, operation_id="getMountain")
def get_mountain(mountain_id: str) -> MountainSummary:
    """Status, current stage, warnings, and on failure the captured error."""
    try:
        return _summary(get_record(get_settings(), mountain_id))
    except MountainNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MountainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/mountains",
    response_model=MountainSummary,
    status_code=202,
    operation_id="createMountain",
)
def create_mountain(
    background: BackgroundTasks,
    dem: Annotated[UploadFile, File(description="Single-band elevation raster (GeoTIFF).")],
    name: Annotated[str, Form(min_length=1, max_length=160)],
    provider: Annotated[str, Form(min_length=1, max_length=200)],
    citation: Annotated[str, Form(min_length=1, max_length=500)],
    licence: Annotated[str, Form(min_length=1, max_length=200)],
    elevations_are_metres: Annotated[bool, Form()] = False,
    resolution_m: Annotated[float | None, Form(gt=0)] = None,
    landcover: Annotated[UploadFile | None, File(
        description="Optional ESA WorldCover-style categorical raster."
    )] = None,
) -> MountainSummary:
    """Accept an upload and start building it. Returns immediately with 202.

    ``provider``, ``citation``, and ``licence`` are required because
    ``SourceStatement`` requires all three: a pack cannot be constructed without
    them, so there is no point accepting an upload that could never be baked.
    """
    from app.mountain_jobs import build_mountain, upload_available

    settings = get_settings()
    available, reason = upload_available(settings)
    if not available:
        raise HTTPException(status_code=503, detail=reason)

    with _create_lock:
        records = read_registry(settings)
        if len(records) >= MAX_UPLOADED_MOUNTAINS:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This instance holds the maximum of {MAX_UPLOADED_MOUNTAINS} uploaded "
                    "mountains. Delete one before adding another."
                ),
            )
        if any(record.status == "baking" for record in records.values()):
            raise HTTPException(
                status_code=409, detail="Another mountain is baking. Try again when it finishes."
            )
        mountain_id = new_mountain_id(name)
        root = mountain_root(settings, mountain_id)
        root.mkdir(parents=True, exist_ok=False)
        record = put_record(settings, MountainRecord(
            id=mountain_id,
            name=name.strip(),
            status="validating",
            created_at_utc=utc_now_iso(),
            updated_at_utc=utc_now_iso(),
            stage="Receiving upload",
        ))

    try:
        static = source_root(settings, mountain_id) / "static"
        written = _stream_to_disk(dem, static / "dem.tif", remaining_budget=MAX_UPLOAD_BYTES)
        if landcover is not None and landcover.filename:
            _stream_to_disk(
                landcover, static / "landcover.tif", remaining_budget=MAX_UPLOAD_BYTES - written
            )
    except HTTPException:
        shutil.rmtree(root, ignore_errors=True)
        _forget(settings, mountain_id)
        raise

    background.add_task(
        build_mountain,
        settings,
        record=record,
        provider=provider.strip(),
        citation=citation.strip(),
        licence=licence.strip(),
        vertical_units_affirmed=bool(elevations_are_metres),
        resolution_m=resolution_m,
    )
    return _summary(record)


@router.delete("/mountains/{mountain_id}", status_code=204, operation_id="deleteMountain")
def remove_mountain(mountain_id: str) -> None:
    """Delete an uploaded mountain and everything baked from it.

    Replacing a mountain is a delete plus a fresh upload under a new id, never a
    re-bake in place: ``bake_identity.promote_bake`` raises ``PermissionError`` on
    Windows when the service still has the target's layers memory-mapped.
    """
    settings = get_settings()
    try:
        record = get_record(settings, mountain_id)
    except MountainNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MountainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if record.status == "baking":
        raise HTTPException(
            status_code=409, detail="This mountain is baking. Wait for it to finish before deleting it."
        )
    delete_mountain(settings, mountain_id)


def _forget(settings: Settings, mountain_id: str) -> None:
    """Drop a registry entry for an upload that never got off the ground."""
    from app.mountains import write_registry

    records = read_registry(settings)
    if records.pop(mountain_id, None) is not None:
        write_registry(settings, records)


__all__: list[str] = ["router"]
