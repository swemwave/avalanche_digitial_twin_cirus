"""Evidence contract for the real, non-BC portable bake verification.

This does not rerun the bake because the 30 MB published DTM is deliberately not
committed.  The full synthetic bake test remains the CI execution test; this file
binds the separately executed real-data record to the reviewed pack and AOI.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.bake_identity import processing_manifest
from app.processing.mountain_pack import MountainPack


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = (
    PROJECT_ROOT / "validation-data" / "bake-verification" / "braemabuehl-2019"
)
PUBLISHED_DTM_SHA256 = (
    "a61440091aefa455be7733d5298302484b7c24d841831cb26c8e5d7043d1c0c1"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_non_bc_bake_record_is_bound_to_pack_and_aoi() -> None:
    pack_path = EVIDENCE_ROOT / "mountain.pack.json"
    aoi_path = EVIDENCE_ROOT / "aoi.geojson"
    record = json.loads((EVIDENCE_ROOT / "run-record.json").read_text(encoding="utf-8"))
    pack = MountainPack.model_validate(json.loads(pack_path.read_text(encoding="utf-8")))
    aoi = json.loads(aoi_path.read_text(encoding="utf-8"))

    assert record["scientific_use"] == "software_verification"
    assert record["component_tested"] == "provider_neutral_single_raster_offline_bake"
    assert "not avalanche field validation" in record["claim_boundary"]
    assert record["mountain"]["is_synthetic"] is False
    assert record["mountain"]["is_bc"] is False

    assert record["inputs"]["mountain_pack"]["sha256"] == _sha256(pack_path)
    assert record["inputs"]["aoi"]["sha256"] == _sha256(aoi_path)
    assert record["source_dem"]["sha256"] == PUBLISHED_DTM_SHA256
    assert record["reproduction"]["raw_dem_committed"] is False
    assert record["reproduction"]["runtime_committed"] is False
    assert (EVIDENCE_ROOT / ".gitignore").read_text(encoding="utf-8").strip() == (
        "/Braemabuehl_DTM_1m.tif"
    )

    assert pack.id == record["mountain"]["id"]
    assert pack.grid.analysis_crs == "EPSG:2056"
    assert pack.grid.bounds == tuple(record["source_dem"]["bounds"])
    assert pack.grid.resolution_m == record["result"]["grid"]["resolution_m"]
    assert pack.assets["elevation_primary"].adapter == "single_raster"
    assert "geobc" not in pack.assets["elevation_primary"].source.provider.lower()
    assert "elevation_lidar" not in pack.assets
    assert pack.grid.vertical_datum.status == "unknown"

    coordinates = aoi["features"][0]["geometry"]["coordinates"][0]
    xs = [point[0] for point in coordinates]
    ys = [point[1] for point in coordinates]
    assert [min(xs), min(ys), max(xs), max(ys)] == list(pack.grid.bounds)


def test_real_non_bc_bake_preserved_missing_forest_cover() -> None:
    record = json.loads(
        (EVIDENCE_ROOT / "run-record.json").read_text(encoding="utf-8")
    )
    execution = record["execution"]
    result = record["result"]
    terrain = result["terrain"]
    missing = result["missing_data_audit"]

    assert execution["bake_exit_code"] == 0
    assert execution["compatibility_check_exit_code"] == 0
    assert execution["replay_bake_exit_code"] == 0
    assert len(execution["bake_sha256"]) == 64
    assert execution["replay_bake_sha256"] == execution["bake_sha256"]
    assert execution["replay_identity_matched"] is True
    assert execution["processing_sha256"] == processing_manifest(PROJECT_ROOT)["sha256"]
    assert result["grid"]["cell_count"] == 300 * 300
    assert terrain["valid_cells"] == result["grid"]["cell_count"]
    assert terrain["missing_cells"] == 0
    assert terrain["valid_fraction"] == 1.0
    assert terrain["lidar_fraction"] is None
    assert terrain["provider_specific_adapter_used"] is False

    assert missing["forest_valid_cells"] == 0
    assert missing["forest_missing_cells"] == result["grid"]["cell_count"]
    assert missing["forest_source_known_cells"] == 0
    assert missing["forest_source_missing_code"] == 0
    assert missing["missing_landcover_became_safe_zero"] is False
    assert result["outputs"]["layer_count"] == len(
        result["outputs"]["layer_sha256"]
    )
