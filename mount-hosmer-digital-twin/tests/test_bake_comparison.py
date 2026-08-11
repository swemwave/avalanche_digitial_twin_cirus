"""Controlled-rebuild numerical comparison contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from app.bake_comparison import (
    BakeComparisonError,
    compare_bakes,
    load_bake_comparison,
    validate_bake_comparison,
    write_bake_comparison,
)
from app.bake_identity import BAKE_SCHEMA, REQUIRED_BAKED_LAYERS


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bake(root: Path, *, changed: str | None = None, masked: str | None = None) -> None:
    (root / "layers").mkdir(parents=True)
    records = []
    for index, name in enumerate(sorted(REQUIRED_BAKED_LAYERS)):
        if name in {"terrain_source", "forest_source"}:
            array = np.full((2, 2), index + 1, dtype="uint8")
            nodata: str | int = 0
            if masked == name:
                array[0, 0] = 0
        else:
            array = np.full((2, 2), float(index + 1), dtype="float32")
            nodata = "NaN"
            if masked == name:
                array[0, 0] = np.nan
        if changed == name:
            array[1, 1] += 1
        path = root / "layers" / f"{name}.npy"
        np.save(path, array, allow_pickle=False)
        records.append(
            {"name": name, "file": f"layers/{name}.npy", "bytes": path.stat().st_size,
             "sha256": _sha(path), "dtype": str(array.dtype), "shape": [2, 2], "nodata": nodata}
        )
    meta = {
        "schema": BAKE_SCHEMA,
        "layers": records,
        "sources": {"files": [{"path": "source.tif", "sha256": "1" * 64}]},
        "processing": {"sha256": "2" * 64},
        "mountain_pack": {"sha256": "3" * 64},
        "model": {"sha256": "4" * 64},
        "identity": {"bake_sha256": "5" * 64},
    }
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_equal_bakes_are_classified_as_lineage_only_and_publish_deterministically(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    _bake(old)
    _bake(new)
    report = compare_bakes(old, new)
    assert report["scientific_summary"]["terrain_arrays_numerically_identical"] is True
    assert report["scientific_summary"]["total_mask_changed_count"] == 0
    assert report["scientific_summary"]["total_value_changed_count"] == 0
    assert validate_bake_comparison(report) == report
    target = write_bake_comparison(report, tmp_path / "runtime")
    assert write_bake_comparison(compare_bakes(old, new), tmp_path / "runtime") == target
    assert load_bake_comparison(target) == report
    checksums = target / "checksums.json"
    manifest = json.loads(checksums.read_text(encoding="utf-8"))
    manifest["files"]["report.json"]["sha256"] = "0" * 64
    checksums.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BakeComparisonError, match="checksum"):
        load_bake_comparison(target)


def test_value_and_mask_changes_are_separate_and_quantified(tmp_path: Path) -> None:
    old, value_changed, mask_changed = tmp_path / "old", tmp_path / "value", tmp_path / "mask"
    _bake(old)
    _bake(value_changed, changed="elevation")
    value_report = compare_bakes(old, value_changed)
    assert value_report["layers"]["elevation"]["value_changed_count"] == 1
    assert value_report["layers"]["elevation"]["maximum_absolute_difference"] == 1.0
    assert value_report["scientific_summary"]["terrain_arrays_numerically_identical"] is False

    _bake(mask_changed, masked="elevation")
    mask_report = compare_bakes(old, mask_changed)
    assert mask_report["layers"]["elevation"]["mask_changed_count"] == 1
    assert mask_report["layers"]["elevation"]["value_changed_count"] == 0
