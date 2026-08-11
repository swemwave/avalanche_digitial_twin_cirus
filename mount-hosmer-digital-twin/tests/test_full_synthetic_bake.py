from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

# This module drives the REAL bake, so it needs the offline geospatial stack
# (rasterio/pyproj/GDAL). That stack is deliberately absent from the serving runtime
# and from requirements-dev.txt -- keeping it out is the point of the bake/serve
# split -- so CI, which installs only the dev requirements, does not have it. Skip
# cleanly there instead of failing collection for the whole suite. Install
# backend/requirements-bake.txt to actually run this.
pytest.importorskip("rasterio", reason="bake-only dependency; see backend/requirements-bake.txt")

import rasterio  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from app.bake import bake  # noqa: E402
from app.bake_identity import bake_fingerprint_payload, sha256_file, sha256_json
from app.core.settings import Settings


def _write_raster(
    path: Path,
    values: np.ndarray,
    *,
    transform,
    nodata: float,
    area_or_point: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=str(values.dtype),
        crs="EPSG:26911",
        transform=transform,
        nodata=nodata,
        compress="deflate",
    ) as dataset:
        dataset.write(values, 1)
        dataset.update_tags(AREA_OR_POINT=area_or_point)


def _synthetic_settings(tmp_path: Path, runtime_name: str) -> Settings:
    project_root = Path(__file__).parents[1]
    data_root = tmp_path / "synthetic-data"
    west, south, east, north = 640000.0, 5490000.0, 640160.0, 5490160.0
    lidar = np.add.outer(
        np.arange(160, dtype="float32") * -0.0625,
        np.arange(160, dtype="float32") * 0.03125,
    ) + 2500.0
    lidar[60:80, 60:80] = -32767.0
    _write_raster(
        data_root / "static" / "lidar" / "synthetic_2022.tif",
        lidar.astype("float32"),
        transform=from_origin(west, north, 1.0, 1.0),
        nodata=-32767.0,
        area_or_point="Point",
    )
    fallback = np.full((32, 32), 2400.0, dtype="float32")
    _write_raster(
        data_root / "static" / "fallback.tif",
        fallback,
        transform=from_origin(west, north, 5.0, 5.0),
        nodata=-9999.0,
        area_or_point="Area",
    )
    landcover = np.full((16, 16), 10, dtype="uint8")
    landcover[0, 0] = 40
    _write_raster(
        data_root / "static" / "landcover.tif",
        landcover,
        transform=from_origin(west, north, 10.0, 10.0),
        nodata=0,
        area_or_point="Area",
    )
    aoi = data_root / "metadata" / "aoi.geojson"
    aoi.parent.mkdir(parents=True, exist_ok=True)
    aoi.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-115.0, 49.6],
                                    [-114.999, 49.6],
                                    [-114.999, 49.599],
                                    [-115.0, 49.599],
                                    [-115.0, 49.6],
                                ]
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pack_path = tmp_path / "synthetic.pack.json"
    source = {
        "provider": "Synthetic redistributable test fixture",
        "citation": "Generated analytical plane",
        "licence": "CC0-1.0",
    }
    pack_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "synthetic-bake",
                "name": "Synthetic bake verification",
                "center_wgs84": [-115.0, 49.6],
                "grid": {
                    "analysis_crs": "EPSG:26911",
                    "coordinate_order": "easting,northing",
                    "bounds": [west, south, east, north],
                    "resolution_m": 5.0,
                    "vertical_datum": {"status": "unknown", "name": None},
                },
                "model_profile": "synthetic-software-verification-only",
                "model_calibrated_locally": False,
                "assets": {
                    "aoi": {
                        "href": "metadata/aoi.geojson",
                        "adapter": "geojson",
                        "purpose": "model_input",
                        "required": True,
                        "units": "longitude,latitude degrees",
                        "source": source,
                    },
                    "elevation_lidar": {
                        "href": "static/lidar",
                        "adapter": "geobc_lidar_year_tiles",
                        "purpose": "model_input",
                        "required": True,
                        "units": "metres",
                        "native_resolution_m": 1.0,
                        "source": source,
                    },
                    "elevation_fallback": {
                        "href": "static/fallback.tif",
                        "adapter": "single_raster",
                        "purpose": "model_input",
                        "required": True,
                        "units": "metres",
                        "native_resolution_m": 5.0,
                        "source": source,
                    },
                    "landcover": {
                        "href": "static/landcover.tif",
                        "adapter": "categorical_raster",
                        "purpose": "model_input",
                        "required": True,
                        "units": "synthetic categorical class",
                        "native_resolution_m": 10.0,
                        "source": source,
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return Settings(
        project_root=project_root,
        backend_root=project_root / "backend",
        runtime_root=tmp_path / runtime_name,
        data_root=data_root,
        mountain_pack_path=pack_path,
    )


def _stable_file_inventory(root: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and path.name != "meta.json"
    ]


def test_actual_disposable_raster_bake_replays_stable_scientific_bytes(
    tmp_path: Path,
) -> None:
    first_settings = _synthetic_settings(tmp_path, "runtime-a")
    second_settings = first_settings.model_copy(
        update={"runtime_root": (tmp_path / "runtime-b").resolve()}
    )

    first = bake(first_settings, force=True)
    second = bake(second_settings, force=True)

    assert first["identity"]["bake_sha256"] == second["identity"]["bake_sha256"]
    assert bake_fingerprint_payload(first) == bake_fingerprint_payload(second)
    first_inventory = _stable_file_inventory(first_settings.runtime_root / "baked")
    second_inventory = _stable_file_inventory(second_settings.runtime_root / "baked")
    assert first_inventory == second_inventory
    assert sha256_json(first_inventory) == sha256_json(second_inventory)
    assert first["grid"]["coordinate_order"] == "easting,northing"
    assert first["grid"]["vertical_datum"] == {"status": "unknown", "name": None}
    assert first["grid"]["transform"] == [5.0, 0.0, 640000.0, 0.0, -5.0, 5490160.0]
    assert first["terrain"]["coverage_by_source_label"][
        "Copernicus GLO-30 30 m (no LiDAR coverage at this pixel)"
    ] > 0.0
    terrain_source_a = np.load(
        first_settings.runtime_root / "baked" / "layers" / "terrain_source.npy"
    )
    terrain_source_b = np.load(
        second_settings.runtime_root / "baked" / "layers" / "terrain_source.npy"
    )
    np.testing.assert_array_equal(terrain_source_a, terrain_source_b)
    assert set(np.unique(terrain_source_a)) == {1, 3}
