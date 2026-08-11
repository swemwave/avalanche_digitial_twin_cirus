"""Versioned identity and validation for scientific bake artifacts.

This module intentionally has only standard-library dependencies so both the
offline bake and the lightweight runtime can verify the same contract.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
from pathlib import Path
from typing import Any, Iterable


BAKE_SCHEMA = "stage3-baked-v2"
PROCESSING_MANIFEST_PATHS = (
    "backend/app/bake.py",
    "backend/app/bake_identity.py",
    "backend/app/core/model_config.py",
    "backend/app/core/paths.py",
    "backend/app/core/settings.py",
    "backend/app/processing/__init__.py",
    "backend/app/processing/exposure.py",
    "backend/app/processing/mountain_pack.py",
    "backend/app/processing/harmonization/__init__.py",
    "backend/app/processing/harmonization/grids.py",
    "backend/app/processing/harmonization/raster_io.py",
    "backend/app/processing/terrain/__init__.py",
    "backend/app/processing/terrain/derivatives.py",
    "backend/app/processing/terrain/engine.py",
    "backend/app/processing/terrain/mosaic.py",
    "backend/app/processing/terrain/provenance.py",
    "backend/config/avalanche_model.yaml",
)
REQUIRED_BAKED_LAYERS = (
    "elevation",
    "slope",
    "aspect",
    "plan_curvature",
    "general_curvature",
    "forest_mask",
    "terrain_source",
    "forest_source",
)
#: Layers a bake MAY carry. They are validated exactly like the required ones when
#: present, and their absence is a supported bake rather than a stale one -- a pack
#: without an exposure asset, or an older bake built before exposure existed, must
#: still load and be assessed with the exposure term reported as unavailable.
OPTIONAL_BAKED_LAYERS = (
    "exposure_weight",
    "exposure_class",
)
#: The NoData contract per layer. Continuous layers mask NaN. Provenance layers
#: mask an explicit zero code. ``exposure_class`` is the one categorical layer whose
#: zero is a MEASUREMENT -- "the exposure extract covers this cell and maps nothing
#: on it" -- so it takes 255 for unknown instead, and a blank valley stays a
#: recorded blank rather than becoming missing data.
LAYER_NODATA: dict[str, int | str] = {
    "terrain_source": 0,
    "forest_source": 0,
    "exposure_class": 255,
}


class BakeCompatibilityError(RuntimeError):
    """Raised when a bake cannot be proven compatible with the current code."""


def remove_generated_directory(path: Path, runtime_root: Path) -> None:
    """Remove one exact bake staging/backup directory after validating its scope."""
    resolved = path.resolve()
    runtime = runtime_root.resolve()
    if resolved.parent != runtime or not (
        resolved.name.startswith(".baked-build-") or resolved.name == ".baked-previous"
    ):
        raise ValueError(f"Refusing to remove a path outside the generated bake scope: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def promote_bake(staging: Path, target: Path, runtime_root: Path) -> None:
    """Atomically expose a validated bake, restoring the previous bake on failure."""
    runtime = runtime_root.resolve()
    staging = staging.resolve()
    target = target.resolve()
    if staging.parent != runtime or not staging.name.startswith(".baked-build-"):
        raise ValueError(f"Invalid bake staging directory: {staging}")
    if target.parent != runtime or target.name != "baked":
        raise ValueError(f"Invalid active bake directory: {target}")

    backup = runtime / ".baked-previous"
    remove_generated_directory(backup, runtime)
    moved_previous = False
    if target.exists():
        try:
            target.rename(backup)
        except PermissionError as exc:
            # Windows refuses to rename a directory while any process holds a
            # handle inside it, and the runtime memory-maps every .npy layer it
            # has loaded. A running server therefore fails the promotion at the
            # very last step, after the expensive part is already done. Say so,
            # rather than surfacing a bare WinError 5.
            raise BakeCompatibilityError(
                f"Could not replace the existing bake at {target}: {exc}. Another process is "
                "holding it open -- most often a running server (uvicorn app.main / "
                "app.main_assess), which memory-maps the baked layers. Stop it and re-run "
                "the bake."
            ) from exc
        moved_previous = True
    try:
        staging.rename(target)
    except Exception:
        if moved_previous and backup.exists() and not target.exists():
            backup.rename(target)
        raise
    if backup.exists():
        remove_generated_directory(backup, runtime)


def preserve_additive_baked_children(
    existing: Path,
    staging: Path,
    *,
    child_names: Iterable[str] = ("conditions",),
) -> list[dict[str, Any]]:
    """Copy content-addressed non-terrain products through a terrain rebuild.

    ``runtime/baked`` also owns immutable ConditionPacks.  Replacing the terrain
    directory must not silently delete them.  Only explicitly named direct
    children are copied, symbolic links are rejected, and every copied file is
    re-hashed before promotion.
    """

    existing = existing.resolve()
    staging = staging.resolve()
    records: list[dict[str, Any]] = []
    for name in child_names:
        if not name or Path(name).name != name or name.startswith("."):
            raise ValueError(f"Invalid additive baked child name: {name!r}")
        source = existing / name
        destination = staging / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_dir():
            raise BakeCompatibilityError(
                f"Additive baked child {name!r} is not a regular directory."
            )
        if destination.exists():
            raise BakeCompatibilityError(
                f"Bake staging unexpectedly already contains additive child {name!r}."
            )
        before: list[dict[str, Any]] = []
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_symlink():
                raise BakeCompatibilityError(
                    f"Additive baked child {name!r} contains a symbolic link."
                )
            if path.is_file():
                before.append(
                    {
                        "path": path.relative_to(source).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        after: list[dict[str, Any]] = []
        for path in sorted(destination.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if path.is_symlink() or not path.is_file():
                if path.is_symlink():
                    raise BakeCompatibilityError(
                        f"Copied additive baked child {name!r} contains a symbolic link."
                    )
                continue
            after.append(
                {
                    "path": path.relative_to(destination).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        if before != after:
            raise BakeCompatibilityError(
                f"Additive baked child {name!r} changed while it was being preserved."
            )
        records.append(
            {
                "name": name,
                "file_count": len(before),
                "bytes": sum(record["bytes"] for record in before),
                "inventory_sha256": sha256_json(before),
            }
        )
    return records


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def parameter_manifest_sha256(value: Any) -> str:
    """Hash assessment parameters with the established runtime config encoding."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def sha256_file(path: Path, *, normalize_text: bool = False) -> str:
    digest = hashlib.sha256()
    if normalize_text:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        digest.update(text.encode("utf-8"))
        return digest.hexdigest()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def processing_manifest(project_root: Path) -> dict[str, Any]:
    """Hash only code and config that can change terrain-bake output.

    Provider adapters, forcing characterization, reference-elevation sampling,
    and future snow-model runners are offline consumers of a bake.  Including
    them here would make a scientifically unchanged terrain artifact stale for
    unrelated M2/M3 edits.
    """
    candidates = [project_root / relative for relative in PROCESSING_MANIFEST_PATHS]
    records = []
    for path in candidates:
        if not path.is_file():
            raise FileNotFoundError(f"Bake implementation file is missing: {path}")
        records.append(
            {
                "path": path.relative_to(project_root).as_posix(),
                "sha256": sha256_file(path, normalize_text=True),
            }
        )
    return {"files": records, "sha256": sha256_json(records)}


def _download_manifest_index(data_root: Path) -> tuple[dict[str, dict[str, str]], str | None]:
    manifest_path = data_root / "metadata" / "download_manifest.csv"
    if not manifest_path.is_file():
        return {}, None
    index: dict[str, dict[str, str]] = {}
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            local_path = str(row.get("local_path", "")).replace("\\", "/").strip("/")
            if local_path:
                index[local_path.casefold()] = {str(key): str(value or "") for key, value in row.items()}
    return index, sha256_file(manifest_path, normalize_text=True)


def source_lineage(data_root: Path, paths: Iterable[Path]) -> dict[str, Any]:
    """Hash every bake input and verify acquisition-manifest hashes when present."""
    root = data_root.resolve()
    manifest, manifest_sha = _download_manifest_index(root)
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sorted((Path(path).resolve() for path in paths), key=lambda value: str(value).casefold()):
        try:
            relative = source.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Bake source is outside the read-only data root: {source}") from exc
        key = relative.casefold()
        if key in seen:
            continue
        seen.add(key)
        if not source.is_file():
            raise FileNotFoundError(f"Bake source is missing: {source}")
        declared = manifest.get(key, {})
        declared_sha = declared.get("sha256", "").strip().lower()
        actual_sha = sha256_file(source)
        if declared_sha and actual_sha != declared_sha:
            raise BakeCompatibilityError(
                f"Source file {relative!r} does not match its acquisition-manifest SHA-256."
            )
        record: dict[str, Any] = {
            "path": relative,
            "bytes": source.stat().st_size,
            "sha256": actual_sha,
            "sha256_origin": (
                "verified_download_manifest" if declared_sha else "computed_by_bake"
            ),
        }
        for field in (
            "dataset",
            "source",
            "source_url",
            "item_id",
            "acquisition_datetime_utc",
            "crs",
            "resolution_m",
        ):
            value = declared.get(field, "").strip()
            if value:
                record[field] = value
        records.append(record)
    return {
        "download_manifest_sha256": manifest_sha,
        "files": records,
        "sha256": sha256_json(records),
    }


def bake_fingerprint_payload(meta: dict[str, Any]) -> dict[str, Any]:
    """The stable scientific content covered by ``identity.bake_sha256``."""
    return {
        "schema": meta.get("schema"),
        "mountain_pack": meta.get("mountain_pack"),
        "grid": meta.get("grid"),
        "layers": meta.get("layers"),
        "sources": meta.get("sources"),
        "processing": meta.get("processing"),
        "model": meta.get("model"),
        # The exposure rasters are covered by their layer checksums above, but the
        # display vector, its licence/attribution and the settlement derivation rule
        # are not -- and an assessment that publishes an exposure term must be bound
        # to the exact rule that produced it.
        "exposure": meta.get("exposure"),
    }


def bake_sha256(meta: dict[str, Any]) -> str:
    return sha256_json(bake_fingerprint_payload(meta))


def validate_bake(
    root: Path,
    *,
    expected_processing_sha256: str | None = None,
    expected_mountain_pack_sha256: str | None = None,
    verify_layer_checksums: bool = True,
) -> dict[str, Any]:
    """Validate schema, code compatibility, required layers, and artifact hashes."""
    meta_path = root / "meta.json"
    if not meta_path.is_file():
        raise BakeCompatibilityError(f"Bake metadata is missing: {meta_path}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BakeCompatibilityError(f"Bake metadata is unreadable: {meta_path}: {exc}") from exc

    schema = meta.get("schema")
    if schema != BAKE_SCHEMA:
        raise BakeCompatibilityError(
            f"Bake schema {schema!r} is incompatible with required schema {BAKE_SCHEMA!r}."
        )
    grid = meta.get("grid", {})
    try:
        width = int(grid["width"])
        height = int(grid["height"])
        resolution_m = float(grid["resolution_m"])
        west = float(grid["west"])
        south = float(grid["south"])
        east = float(grid["east"])
        north = float(grid["north"])
        transform = tuple(float(value) for value in grid["transform"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BakeCompatibilityError("Bake grid contract is incomplete or malformed.") from exc
    if (
        width <= 0
        or height <= 0
        or resolution_m <= 0
        or len(transform) != 6
        or grid.get("coordinate_order") != "easting,northing"
        or not str(grid.get("crs", "")).startswith("EPSG:")
        or not math.isclose(east - west, width * resolution_m, abs_tol=1e-9)
        or not math.isclose(north - south, height * resolution_m, abs_tol=1e-9)
        or transform
        != (resolution_m, 0.0, west, 0.0, -resolution_m, north)
    ):
        raise BakeCompatibilityError("Bake grid bounds, affine, units, or coordinate order conflict.")
    vertical_datum = grid.get("vertical_datum")
    if not isinstance(vertical_datum, dict) or set(vertical_datum) != {"status", "name"}:
        raise BakeCompatibilityError("Bake vertical-datum contract is missing or not strict.")
    if vertical_datum["status"] not in {"known", "unknown"} or (
        vertical_datum["status"] == "known"
    ) != bool(vertical_datum["name"]):
        raise BakeCompatibilityError("Bake vertical-datum status and name conflict.")
    recorded_processing = meta.get("processing", {}).get("sha256")
    if expected_processing_sha256 and recorded_processing != expected_processing_sha256:
        raise BakeCompatibilityError(
            "The bake was produced by different terrain-processing code or configuration."
        )
    recorded_pack = meta.get("mountain_pack", {}).get("sha256")
    if expected_mountain_pack_sha256 and recorded_pack != expected_mountain_pack_sha256:
        raise BakeCompatibilityError("The bake was produced from a different mountain pack.")
    model = meta.get("model", {})
    parameter_manifest = model.get("parameter_manifest")
    recorded_model_sha = model.get("sha256")
    if not isinstance(parameter_manifest, dict) or not recorded_model_sha:
        raise BakeCompatibilityError("Bake assessment parameter identity is missing.")
    if parameter_manifest_sha256(parameter_manifest) != recorded_model_sha:
        raise BakeCompatibilityError("Bake assessment parameter manifest does not match its hash.")

    layer_records = {record.get("name"): record for record in meta.get("layers", [])}
    missing = sorted(set(REQUIRED_BAKED_LAYERS) - set(layer_records))
    if missing:
        raise BakeCompatibilityError(f"Bake is missing required layers: {', '.join(missing)}")
    root_resolved = root.resolve()
    import numpy as np

    present_optional = [name for name in OPTIONAL_BAKED_LAYERS if name in layer_records]
    for name in (*REQUIRED_BAKED_LAYERS, *present_optional):
        record = layer_records[name]
        relative = Path(str(record.get("file", "")))
        path = (root / relative).resolve()
        if not path.is_relative_to(root_resolved) or not path.is_file():
            raise BakeCompatibilityError(f"Baked layer {name!r} is missing or escapes the bake: {relative}")
        expected_bytes = record.get("bytes")
        if expected_bytes is not None and path.stat().st_size != int(expected_bytes):
            raise BakeCompatibilityError(f"Baked layer {name!r} has an unexpected file size.")
        expected_sha = record.get("sha256")
        if verify_layer_checksums and expected_sha and sha256_file(path) != expected_sha:
            raise BakeCompatibilityError(f"Baked layer {name!r} failed its SHA-256 check.")
        try:
            array = np.load(path, mmap_mode="r", allow_pickle=False)
        except Exception as exc:
            raise BakeCompatibilityError(f"Baked layer {name!r} has an invalid NPY header.") from exc
        if list(array.shape) != [height, width] or record.get("shape") != [height, width]:
            raise BakeCompatibilityError(f"Baked layer {name!r} has an unexpected array shape.")
        if str(array.dtype) != record.get("dtype"):
            raise BakeCompatibilityError(f"Baked layer {name!r} has an unexpected dtype.")
        expected_nodata = LAYER_NODATA.get(name, "NaN")
        if record.get("nodata") != expected_nodata:
            raise BakeCompatibilityError(f"Baked layer {name!r} has incompatible NoData semantics.")
        valid = (
            int(np.count_nonzero(np.isfinite(array)))
            if expected_nodata == "NaN"
            else int(np.count_nonzero(array != expected_nodata))
        )
        if valid != record.get("valid_pixels") or not math.isclose(
            round(valid / array.size, 4),
            float(record.get("valid_fraction", -1.0)),
            abs_tol=1e-12,
        ):
            raise BakeCompatibilityError(f"Baked layer {name!r} mask statistics conflict.")

    recorded_bake_sha = meta.get("identity", {}).get("bake_sha256")
    actual_bake_sha = bake_sha256(meta)
    if not recorded_bake_sha or recorded_bake_sha != actual_bake_sha:
        raise BakeCompatibilityError("Bake identity is missing or does not match its metadata.")
    return meta
