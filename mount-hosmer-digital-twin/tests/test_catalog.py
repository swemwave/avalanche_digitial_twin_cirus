from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.services.aoi import load_aoi, load_grid
from app.services.catalog import generate_catalog, inspect_raster, load_manifest


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tiny_data_root(tmp_path: Path) -> Path:
    root = tmp_path / "mount_hosmer_data"
    (root / "metadata").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "events" / "MH_20260116T183016Z").mkdir(parents=True)
    write_text(
        root / "metadata" / "mount_hosmer_aoi.geojson",
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "test"},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[-115.1, 49.5], [-115.0, 49.5], [-115.0, 49.6], [-115.1, 49.6], [-115.1, 49.5]]],
                        },
                    }
                ],
            }
        ),
    )
    write_text(
        root / "metadata" / "grid_and_aoi.json",
        json.dumps({"analysis_crs": "EPSG:26911", "grid_10m": {"width": 2, "height": 2}}),
    )
    write_text(root / "metadata" / "event_pairs.json", json.dumps([]))
    write_text(root / "metadata" / "event_pairs.csv", "event_id\nMH_20260116T183016Z\n")
    write_text(root / "metadata" / "config_used.yaml", "analysis_crs: EPSG:26911\n")
    write_text(root / "metadata" / "download_manifest.json", json.dumps([]))
    write_text(root / "logs" / "download_errors.json", json.dumps([]))
    write_text(root / "download.log", "test log\n")
    data_csv = root / "dynamic" / "weather_eccc" / "weather.csv"
    write_text(data_csv, "timestamp,temp_c\n2026-01-01T00:00:00Z,-4\n2026-01-02T00:00:00Z,-1\n")
    digest = hashlib.sha256(data_csv.read_bytes()).hexdigest()
    with (root / "metadata" / "download_manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "dataset",
                "source",
                "status",
                "local_path",
                "source_url",
                "item_id",
                "acquisition_datetime_utc",
                "crs",
                "resolution_m",
                "bbox_wgs84",
                "sha256",
                "notes",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "dataset": "weather",
                "source": "test",
                "status": "downloaded",
                "local_path": "dynamic\\weather_eccc\\weather.csv",
                "source_url": "",
                "item_id": "",
                "acquisition_datetime_utc": "",
                "crs": "",
                "resolution_m": "",
                "bbox_wgs84": "",
                "sha256": digest,
                "notes": "",
            }
        )
    return root


def test_manifest_parsing_normalizes_paths(tmp_path: Path) -> None:
    root = tiny_data_root(tmp_path)
    entries, summary = load_manifest(root)
    assert summary["rows"] == 1
    assert "dynamic/weather_eccc/weather.csv" in entries
    assert entries["dynamic/weather_eccc/weather.csv"].status == "downloaded"


def test_generate_catalog_writes_json_csv_and_warnings(tmp_path: Path) -> None:
    root = tiny_data_root(tmp_path)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=root,
    )
    catalog = generate_catalog(settings, verify_checksums=True)
    assert catalog["summary"]["file_count"] >= 9
    assert catalog["summary"]["checksum_counts"]["match"] == 1
    assert catalog["summary"]["event_ids"] == ["MH_20260116T183016Z"]
    assert (settings.runtime_root / "catalog" / "data_catalog.json").exists()
    assert (settings.runtime_root / "catalog" / "data_catalog.csv").exists()
    assert (settings.runtime_root / "catalog" / "catalog_warnings.json").exists()


def test_aoi_and_grid_loading(tmp_path: Path) -> None:
    root = tiny_data_root(tmp_path)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=root,
    )
    aoi = load_aoi(settings)
    grid = load_grid(settings)
    assert aoi["geojson"]["type"] == "FeatureCollection"
    assert grid["analysis_crs"] == "EPSG:26911"


def test_raster_metadata_extraction(tmp_path: Path) -> None:
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    raster_path = tmp_path / "tiny.tif"
    data = np.array([[1, 2], [3, 4]], dtype="float32")
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=from_origin(500000, 5500000, 10, 10),
        nodata=-9999,
    ) as dst:
        dst.write(data, 1)
    metadata, warnings = inspect_raster(raster_path)
    assert warnings == []
    assert metadata["crs"] == "EPSG:26911"
    assert metadata["dimensions"] == [2, 2]
    assert metadata["value_sample"]["min"] == 1.0
    assert metadata["value_sample"]["max"] == 4.0
