from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import numpy as np
import pytest

from app.assess import assessment_model_identity
from app.bake_identity import (
    BakeCompatibilityError,
    PROCESSING_MANIFEST_PATHS,
    preserve_additive_baked_children,
    processing_manifest,
    promote_bake,
    source_lineage,
    validate_bake,
)
from app.processing.terrain.provenance import combine_forest_classifications
from synthetic_baked import write_synthetic_baked


def test_synthetic_bake_identity_and_layer_hashes_validate(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)

    meta = validate_bake(root)

    assert len(meta["identity"]["bake_sha256"]) == 64
    assert {"terrain_source", "forest_source"} <= {
        record["name"] for record in meta["layers"]
    }
    assert meta["model"] == assessment_model_identity()


def test_bake_identity_changes_with_assessment_parameter_manifest(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)
    original = validate_bake(root)
    changed = dict(original)
    changed["model"] = {
        **original["model"],
        "parameter_manifest": {
            **original["model"]["parameter_manifest"],
            "runout": {
                **original["model"]["parameter_manifest"]["runout"],
                "alpha_uncertainty_deg": 999.0,
            },
        },
    }

    from app.bake_identity import bake_sha256

    assert bake_sha256(changed) != original["identity"]["bake_sha256"]


def test_mismatched_assessment_parameter_hash_is_rejected(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)
    meta_path = root / "meta.json"
    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["model"]["parameter_manifest"]["runout"]["alpha_uncertainty_deg"] = 999.0
    from app.bake_identity import bake_sha256

    meta["identity"]["bake_sha256"] = bake_sha256(meta)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(BakeCompatibilityError, match="parameter manifest does not match"):
        validate_bake(root)


def test_corrupted_baked_layer_is_rejected(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)
    layer = root / "layers" / "slope.npy"
    with layer.open("ab") as stream:
        stream.write(b"corruption")

    with pytest.raises(BakeCompatibilityError, match="file size"):
        validate_bake(root)


def test_processing_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)

    with pytest.raises(BakeCompatibilityError, match="different terrain-processing"):
        validate_bake(root, expected_processing_sha256="0" * 64)


def test_mountain_pack_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    root = write_synthetic_baked(tmp_path)

    with pytest.raises(BakeCompatibilityError, match="different mountain pack"):
        validate_bake(root, expected_mountain_pack_sha256="0" * 64)


def test_validated_staging_bake_replaces_old_generated_bake(tmp_path: Path) -> None:
    target = tmp_path / "baked"
    target.mkdir()
    (target / "marker.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / ".baked-build-test"
    staging.mkdir()
    (staging / "marker.txt").write_text("new", encoding="utf-8")

    promote_bake(staging, target, tmp_path)

    assert (target / "marker.txt").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / ".baked-previous").exists()


def test_condition_packs_are_preserved_byte_for_byte_through_terrain_rebuild(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "baked"
    condition = existing / "conditions" / "condition-test"
    condition.mkdir(parents=True)
    (condition / "condition-pack.json").write_bytes(b"immutable pack bytes")
    (condition / "checksums.json").write_bytes(b"immutable checksum bytes")
    staging = tmp_path / ".baked-build-test"
    staging.mkdir()

    records = preserve_additive_baked_children(existing, staging)

    assert records == [
        {
            "name": "conditions",
            "file_count": 2,
            "bytes": len(b"immutable pack bytes") + len(b"immutable checksum bytes"),
            "inventory_sha256": records[0]["inventory_sha256"],
        }
    ]
    assert (staging / "conditions" / "condition-test" / "condition-pack.json").read_bytes() == b"immutable pack bytes"
    assert (staging / "conditions" / "condition-test" / "checksums.json").read_bytes() == b"immutable checksum bytes"


def test_processing_identity_excludes_offline_consumers_of_the_bake() -> None:
    assert not any("processing/conditions" in path for path in PROCESSING_MANIFEST_PATHS)
    assert not any("reference_elevation" in path for path in PROCESSING_MANIFEST_PATHS)
    assert not any("processing/snow" in path for path in PROCESSING_MANIFEST_PATHS)
    assert "backend/app/processing/mountain_pack.py" in PROCESSING_MANIFEST_PATHS
    manifest = processing_manifest(Path(__file__).parents[1])
    assert [record["path"] for record in manifest["files"]] == list(
        PROCESSING_MANIFEST_PATHS
    )


def test_source_lineage_prefers_acquisition_manifest_hash(tmp_path: Path) -> None:
    data = tmp_path / "data"
    source = data / "static" / "terrain.tif"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    metadata = data / "metadata"
    metadata.mkdir()
    with (metadata / "download_manifest.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["dataset", "local_path", "sha256", "crs", "resolution_m"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "dataset": "Synthetic terrain",
                "local_path": "static\\terrain.tif",
                "sha256": hashlib.sha256(b"source bytes").hexdigest(),
                "crs": "EPSG:26911",
                "resolution_m": "5",
            }
        )

    lineage = source_lineage(data, [source])

    assert lineage["files"][0]["sha256"] == hashlib.sha256(b"source bytes").hexdigest()
    assert lineage["files"][0]["sha256_origin"] == "verified_download_manifest"
    assert lineage["files"][0]["bytes"] == len(b"source bytes")


def test_missing_forest_inputs_remain_masked_not_open_terrain() -> None:
    shape = (2, 2)
    canopy_known = np.array([[True, False], [False, False]])
    landcover_known = np.array([[True, True], [False, True]])
    forest, source = combine_forest_classifications(
        canopy_forest=np.array([[True, False], [False, False]]),
        canopy_known=canopy_known,
        landcover_forest=np.array([[False, True], [False, False]]),
        landcover_known=landcover_known,
        dem_mask=np.zeros(shape, dtype=bool),
    )

    assert float(forest[0, 0]) == 1.0  # LiDAR wins where both sources exist.
    assert float(forest[0, 1]) == 1.0  # WorldCover fills a missing canopy cell.
    assert bool(np.ma.getmaskarray(forest)[1, 0])
    assert bool(np.ma.getmaskarray(source)[1, 0])
