"""Deterministic scientific comparison of preserved and rebuilt bake products."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from app.bake_identity import BAKE_SCHEMA, REQUIRED_BAKED_LAYERS, sha256_file
from app.bake_preservation import inventory_directory


SCHEMA = "terrain-bake-comparison-v1"
STORAGE_SCHEMA = "terrain-bake-comparison-storage-v1"
DISCLAIMER = (
    "Experimental research prototype only; not an operational avalanche forecast, not a "
    "probability, and never a replacement for Avalanche Canada guidance or field assessment."
)


class BakeComparisonError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_meta(root: Path) -> dict[str, Any]:
    try:
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise BakeComparisonError(f"Bake metadata is unreadable: {root}") from exc
    if meta.get("schema") != BAKE_SCHEMA:
        raise BakeComparisonError("Bake comparison requires the current metadata schema.")
    records = {item.get("name"): item for item in meta.get("layers", [])}
    if set(records) != set(REQUIRED_BAKED_LAYERS):
        raise BakeComparisonError("Bake layer set is incomplete or unexpected.")
    for record in records.values():
        path = (root / str(record.get("file", ""))).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise BakeComparisonError("Bake layer path is unsafe or missing.")
        if path.stat().st_size != record.get("bytes") or sha256_file(path) != record.get("sha256"):
            raise BakeComparisonError("Bake layer failed its recorded size/SHA-256 check.")
    return meta


def _mask(array: np.ndarray, nodata: Any) -> np.ndarray:
    if nodata in (None, "NaN"):
        return ~np.isfinite(array)
    return array == nodata


def _directory_surface(root: Path, name: str) -> dict[str, Any]:
    path = root / name
    if not path.is_dir():
        return {"present": False, "file_count": 0, "total_bytes": 0, "inventory_sha256": None}
    inventory = inventory_directory(path)
    return {
        "present": True,
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "inventory_sha256": inventory["inventory_sha256"],
    }


def compare_bakes(old_bake: str | Path, new_bake: str | Path) -> dict[str, Any]:
    old_root, new_root = Path(old_bake).resolve(), Path(new_bake).resolve()
    old_meta, new_meta = _load_meta(old_root), _load_meta(new_root)
    old_records = {item["name"]: item for item in old_meta["layers"]}
    new_records = {item["name"]: item for item in new_meta["layers"]}
    layers: dict[str, Any] = {}
    total_mask_changes = total_value_changes = 0
    for name in sorted(REQUIRED_BAKED_LAYERS):
        left_record, right_record = old_records[name], new_records[name]
        left = np.load(old_root / left_record["file"], mmap_mode="r")
        right = np.load(new_root / right_record["file"], mmap_mode="r")
        if left.shape != right.shape or left.dtype != right.dtype:
            raise BakeComparisonError(f"Layer {name} shape or dtype changed; explicit migration required.")
        left_mask, right_mask = _mask(left, left_record.get("nodata")), _mask(right, right_record.get("nodata"))
        mask_changes = int(np.count_nonzero(left_mask != right_mask))
        common = ~(left_mask | right_mask)
        differences = np.asarray(right[common], dtype="float64") - np.asarray(left[common], dtype="float64")
        value_changes = int(np.count_nonzero(differences != 0.0))
        total_mask_changes += mask_changes
        total_value_changes += value_changes
        layers[name] = {
            "dtype": str(left.dtype),
            "shape": list(left.shape),
            "old_sha256": left_record["sha256"],
            "new_sha256": right_record["sha256"],
            "file_bytes_identical": bool(left_record["sha256"] == right_record["sha256"]),
            "old_masked_count": int(np.count_nonzero(left_mask)),
            "new_masked_count": int(np.count_nonzero(right_mask)),
            "mask_changed_count": mask_changes,
            "common_valid_count": int(np.count_nonzero(common)),
            "value_changed_count": value_changes,
            "maximum_absolute_difference": float(np.max(np.abs(differences))) if differences.size else None,
            "mean_absolute_difference": float(np.mean(np.abs(differences))) if differences.size else None,
        }
    old_sources = {item["path"]: item["sha256"] for item in old_meta.get("sources", {}).get("files", [])}
    new_sources = {item["path"]: item["sha256"] for item in new_meta.get("sources", {}).get("files", [])}
    report_without_id = {
        "schema_version": SCHEMA,
        "disclaimer": DISCLAIMER,
        "old_bake": {
            "bake_sha256": old_meta["identity"]["bake_sha256"],
            "processing_sha256": old_meta.get("processing", {}).get("sha256"),
            "mountain_pack_sha256": old_meta.get("mountain_pack", {}).get("sha256"),
            "model_sha256": old_meta.get("model", {}).get("sha256"),
        },
        "new_bake": {
            "bake_sha256": new_meta["identity"]["bake_sha256"],
            "processing_sha256": new_meta.get("processing", {}).get("sha256"),
            "mountain_pack_sha256": new_meta.get("mountain_pack", {}).get("sha256"),
            "model_sha256": new_meta.get("model", {}).get("sha256"),
        },
        "source_lineage": {
            "old_count": len(old_sources),
            "new_count": len(new_sources),
            "common_identical_count": sum(old_sources.get(path) == digest for path, digest in new_sources.items()),
            "removed_paths": sorted(set(old_sources) - set(new_sources)),
            "added_paths": sorted(set(new_sources) - set(old_sources)),
            "changed_paths": sorted(path for path in set(old_sources) & set(new_sources) if old_sources[path] != new_sources[path]),
        },
        "layers": layers,
        "surfaces": {
            name: {"old": _directory_surface(old_root, name), "new": _directory_surface(new_root, name)}
            for name in ("tiles", "imagery", "conditions")
        },
        "scientific_summary": {
            "total_mask_changed_count": total_mask_changes,
            "total_value_changed_count": total_value_changes,
            "terrain_arrays_numerically_identical": total_mask_changes == 0 and total_value_changes == 0,
            "classification": (
                "processing_pack_model_lineage_only_zero_terrain_numerical_change"
                if total_mask_changes == 0 and total_value_changes == 0
                else "terrain_numerical_change_requires_scientific_review"
            ),
            "claim_boundary": "Numerical identity is reproducibility evidence, not field validation or improved accuracy.",
        },
    }
    identity = f"bake-comparison-{_sha(_canonical(report_without_id))}"
    return {**report_without_id, "comparison_id": identity}


def validate_bake_comparison(report: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(report)
    comparison_id = value.pop("comparison_id", None)
    if value.get("schema_version") != SCHEMA or comparison_id != f"bake-comparison-{_sha(_canonical(value))}":
        raise BakeComparisonError("Bake-comparison schema or content identity is invalid.")
    if set(value.get("layers", {})) != set(REQUIRED_BAKED_LAYERS):
        raise BakeComparisonError("Bake-comparison layer set is not exact.")
    value["comparison_id"] = comparison_id
    return value


def load_bake_comparison(path: str | Path) -> dict[str, Any]:
    requested = Path(path).resolve()
    if requested.is_file():
        try:
            return validate_bake_comparison(json.loads(requested.read_text(encoding="utf-8")))
        except Exception as exc:
            raise BakeComparisonError(f"Bake-comparison report is invalid: {exc}") from exc
    try:
        report_bytes = (requested / "report.json").read_bytes()
        report = validate_bake_comparison(json.loads(report_bytes.decode("utf-8")))
        checksums = json.loads((requested / "checksums.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise BakeComparisonError(f"Bake-comparison storage is invalid: {exc}") from exc
    expected = {
        "schema_version": STORAGE_SCHEMA,
        "comparison_id": report["comparison_id"],
        "files": {"report.json": {"bytes": len(report_bytes), "sha256": _sha(report_bytes)}},
    }
    if checksums != expected or requested.name != report["comparison_id"]:
        raise BakeComparisonError("Bake-comparison checksum or directory identity conflicts.")
    return report


def write_bake_comparison(report: Mapping[str, Any], runtime_root: str | Path) -> Path:
    validated = validate_bake_comparison(report)
    root = Path(runtime_root).resolve() / "reports" / "terrain" / "bake-comparisons"
    root.mkdir(parents=True, exist_ok=True)
    target = root / validated["comparison_id"]
    report_bytes = _canonical(validated)
    checksums = {"schema_version": STORAGE_SCHEMA, "comparison_id": validated["comparison_id"], "files": {"report.json": {"bytes": len(report_bytes), "sha256": _sha(report_bytes)}}}
    if target.exists():
        if load_bake_comparison(target) != validated or (target / "report.json").read_bytes() != report_bytes:
            raise BakeComparisonError("Existing bake-comparison identity conflicts.")
        return target
    staging = root / f".bake-comparison-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, content in (("report.json", report_bytes), ("checksums.json", _canonical(checksums))):
            with (staging / name).open("xb") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    load_bake_comparison(target)
    return target
