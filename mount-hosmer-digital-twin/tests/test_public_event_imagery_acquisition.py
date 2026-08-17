from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "acquire_public_event_imagery.py"
)
SPEC = importlib.util.spec_from_file_location("acquire_public_event_imagery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pair_selection_is_metadata_only_and_deterministic() -> None:
    pairs = [
        {
            "pair_id": "later-tie",
            "temporal_baseline_seconds": 100,
            "pre_to_event_start_seconds": 40,
            "event_end_to_post_seconds": 60,
        },
        {
            "pair_id": "winner",
            "temporal_baseline_seconds": 90,
            "pre_to_event_start_seconds": 80,
            "event_end_to_post_seconds": 10,
            "pre_catalogue_cloud_cover_percent": 100,
        },
        {
            "pair_id": "cloud-selected-only-if-rule-were-wrong",
            "temporal_baseline_seconds": 110,
            "pre_to_event_start_seconds": 50,
            "event_end_to_post_seconds": 60,
            "pre_catalogue_cloud_cover_percent": 0,
        },
    ]
    assert MODULE.select_pair({"accepted_pairs": pairs})["pair_id"] == "winner"
    assert MODULE.select_pair({"accepted_pairs": []}) is None


def test_cross_catalogue_item_matching_preserves_processing_mismatch() -> None:
    requested = "S2B_MSIL2A_20230202T110159_N0510_R094_T32VLP_20240815T142640"
    matching = {
        "id": "S2B_T32VLP_20230202T110153_L2A",
        "properties": {
            "s2:product_uri": (
                "S2B_MSIL2A_20230202T110159_N0509_R094_T32VLP_20230202T122454.SAFE"
            ),
            "mgrs:utm_zone": 32,
            "mgrs:latitude_band": "V",
            "mgrs:grid_square": "LP",
        },
    }
    unrelated = {
        "id": "unrelated",
        "properties": {
            "s2:product_uri": "S2B_MSIL2A_20230202T110159_N0509_R094_T32VKN_X.SAFE",
            "mgrs:utm_zone": 32,
            "mgrs:latitude_band": "V",
            "mgrs:grid_square": "KN",
        },
    }
    assert MODULE.match_s2_item([unrelated, matching], requested, "32VLP") is matching
    with pytest.raises(ValueError, match="found 0"):
        MODULE.match_s2_item([unrelated], requested, "32VLP")


def test_s1_mapping_and_norway_svalbard_utm_exceptions() -> None:
    source_id = (
        "S1A_IW_GRDH_1SDV_20230204T055547_20230204T055612_047082_05A5E3_AA60_COG"
    )
    assert MODULE.earth_search_s1_id(source_id) == (
        "S1A_IW_GRDH_1SDV_20230204T055547_20230204T055612_047082_05A5E3"
    )
    assert MODULE.local_utm_epsg(6.7, 61.9) == 32632
    assert MODULE.local_utm_epsg(14.2, 78.0) == 32633
    assets = MODULE._sensor_assets("sentinel_1_grd", {"polarizations": ["HV", "HH"]})
    assert assets[:2] == ("hh", "hv")
    assert "schema-calibration-hh" in assets
    assert "annotation-hh" in assets
    assert "schema-product-hh" not in assets
    assert "vv" not in assets


def test_s1_annotation_href_targets_product_annotation_not_rfi_document() -> None:
    measurement = (
        "s3://sentinel-s1-l1c/GRD/2026/4/2/IW/DV/product/measurement/iw-vv.tiff"
    )
    assert MODULE._s1_annotation_href(measurement, "vv") == (
        "s3://sentinel-s1-l1c/GRD/2026/4/2/IW/DV/product/annotation/iw-vv.xml"
    )
    ew_measurement = (
        "s3://sentinel-s1-l1c/GRD/2026/2/15/EW/DH/product/measurement/ew-hh.tiff"
    )
    assert MODULE._s1_annotation_href(ew_measurement, "hh") == (
        "s3://sentinel-s1-l1c/GRD/2026/2/15/EW/DH/product/annotation/ew-hh.xml"
    )
    with pytest.raises(ValueError, match="Unexpected Sentinel-1 measurement path"):
        MODULE._s1_annotation_href(measurement, "vh")


def test_gcp_only_s1_raster_is_reprojected_without_empty_chip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rasterio = pytest.importorskip("rasterio")
    pyproj = pytest.importorskip("pyproj")
    from rasterio.control import GroundControlPoint

    source_path = tmp_path / "gcp-only.tif"
    profile = {
        "driver": "GTiff",
        "width": 20,
        "height": 20,
        "count": 1,
        "dtype": "uint16",
        "nodata": 0,
    }
    lon, lat = 6.7, 61.9
    gcps = [
        GroundControlPoint(row=0, col=0, x=lon - 0.003, y=lat + 0.002),
        GroundControlPoint(row=0, col=19, x=lon + 0.003, y=lat + 0.002),
        GroundControlPoint(row=19, col=0, x=lon - 0.003, y=lat - 0.002),
        GroundControlPoint(row=19, col=19, x=lon + 0.003, y=lat - 0.002),
    ]
    with rasterio.open(source_path, "w", **profile) as target:
        target.write(np.full((1, 20, 20), 100, dtype=np.uint16))
        target.gcps = (gcps, rasterio.crs.CRS.from_epsg(4326))

    monkeypatch.setattr(MODULE, "CHIP_RADIUS_M", 100)
    monkeypatch.setattr(MODULE, "CHIP_RESOLUTION_M", 10)
    monkeypatch.setattr(MODULE, "CHIP_SIZE", 20)
    epsg = MODULE.local_utm_epsg(lon, lat)
    transformer = pyproj.Transformer.from_crs(
        "EPSG:4326", f"EPSG:{epsg}", always_xy=True
    )
    center_x, center_y = transformer.transform(lon, lat)
    record = MODULE._acquire_raster_chip(
        str(source_path),
        tmp_path / "chip.tif",
        target_epsg=epsg,
        center_x=center_x,
        center_y=center_y,
        asset_name="vv",
        offline=False,
    )
    assert record["raster"]["valid_pixel_count_all_bands"] > 0
    assert record["raster"]["source_georeferencing"]["gcp_count"] == 4
    assert "GCP transformer" in record["raster"]["source_georeferencing"]["method"]
    assert record["source_georeferencing_sha256"]


def test_immutable_cache_never_overwrites_different_bytes(tmp_path: Path) -> None:
    path = tmp_path / "asset.bin"
    MODULE._write_immutable(path, b"first")
    MODULE._write_immutable(path, b"first")
    with pytest.raises(ValueError, match="Immutable cache identity conflict"):
        MODULE._write_immutable(path, b"different")
    assert path.read_bytes() == b"first"


def test_checked_in_cache_references_are_repository_relative() -> None:
    path = MODULE.REPOSITORY_ROOT / ".validation-cache" / "asset.bin"
    assert MODULE._stable_path_reference(path) == ".validation-cache/asset.bin"


def test_candidate_acquisition_preserves_frozen_manifest_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preflight = {
        "schema": "avycore-public-event-imagery-preflight-v1",
        "predictions_generated": False,
        "candidates": [
            {"candidate_id": "event-3"},
            {"candidate_id": "event-1"},
            {"candidate_id": "event-2"},
        ],
    }
    preflight_path = tmp_path / "preflight.json"
    preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

    def fake_acquire(candidate: dict, cache_root: Path, *, offline: bool) -> dict:
        return {
            "candidate_id": candidate["candidate_id"],
            "sentinel_1_grd": {"status": "not_acquired"},
            "sentinel_2_l2a": {"status": "not_acquired"},
        }

    monkeypatch.setattr(MODULE, "_candidate_acquisition", fake_acquire)
    artifact = MODULE.build_acquisition(
        preflight_path, tmp_path / "cache", offline=True, workers=1
    )
    assert [candidate["candidate_id"] for candidate in artifact["candidates"]] == [
        "event-3",
        "event-1",
        "event-2",
    ]
