from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.processing.harmonization.grids import terrain_grid
from app.processing.mountain_pack import (
    MountainPackError,
    load_mountain_pack,
    validate_declared_sources,
)


def _asset(href: str, adapter: str, *, purpose: str = "model_input") -> dict:
    return {
        "href": href,
        "adapter": adapter,
        "purpose": purpose,
        "required": True,
        "units": "metres" if "elevation" in href else "class code",
        "source": {
            "provider": "Synthetic test provider",
            "citation": "Synthetic portability fixture",
            "licence": "Test data only",
        },
    }


def _pack() -> dict:
    return {
        "schema_version": 1,
        "id": "synthetic-zone-10",
        "name": "Synthetic Zone 10 Mountain",
        "center_wgs84": [-123.1, 49.3],
        "grid": {
            "analysis_crs": "EPSG:32610",
            "coordinate_order": "easting,northing",
            "bounds": [490000.0, 5450000.0, 493000.0, 5452000.0],
            "resolution_m": 10.0,
            "vertical_datum": {"status": "unknown", "name": None},
        },
        "model_profile": "uncalibrated-test-profile",
        "model_calibrated_locally": False,
        "assets": {
            "aoi": _asset("metadata/aoi.geojson", "geojson"),
            "elevation_lidar": _asset(
                "static/lidar/dem", "geobc_lidar_year_tiles"
            ),
            "elevation_fallback": _asset(
                "static/fallback/dem.tif", "single_raster"
            ),
            "landcover": _asset(
                "static/landcover/classes.tif", "categorical_raster"
            ),
        },
    }


def _settings(tmp_path: Path, pack_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=tmp_path / "data",
        mountain_pack_path=pack_path,
    )


def test_pack_drives_a_non_hosmer_crs_extent_and_resolution(tmp_path: Path) -> None:
    pack_path = tmp_path / "zone10.pack.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")

    pack, identity = load_mountain_pack(_settings(tmp_path, pack_path))
    grid = terrain_grid(_settings(tmp_path, pack_path))

    assert pack.id == "synthetic-zone-10"
    assert identity["sha256"] and len(identity["sha256"]) == 64
    assert grid.crs_string == "EPSG:32610"
    assert grid.resolution_m == 10.0
    assert grid.shape == (200, 300)


def test_pack_rejects_asset_path_escape(tmp_path: Path) -> None:
    raw = _pack()
    raw["assets"]["landcover"]["href"] = "../untrusted.tif"
    pack_path = tmp_path / "unsafe.pack.json"
    pack_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MountainPackError, match="relative paths"):
        load_mountain_pack(_settings(tmp_path, pack_path))


@pytest.mark.parametrize("role", ["pois", "exposure_features"])
def test_pack_keeps_exposure_assets_out_of_hazard_inputs(tmp_path: Path, role: str) -> None:
    """An exposure asset may never be re-declared as a model input.

    Exposure is now allowed to reach the assessment, but only through the named
    consequence term of the composite hazard index. Binding these roles to
    ``purpose: exposure`` is what keeps that the only door.
    """
    raw = _pack()
    raw["assets"][role] = _asset(
        "exposure/features.geojson", "geojson", purpose="model_input"
    )
    pack_path = tmp_path / f"bad-{role}.pack.json"
    pack_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MountainPackError, match="must remain an exposure asset"):
        load_mountain_pack(_settings(tmp_path, pack_path))


def test_pack_rejects_exposure_purpose_on_a_hazard_role(tmp_path: Path) -> None:
    """The guardrail runs both ways: a terrain role cannot smuggle in exposure."""
    raw = _pack()
    raw["assets"]["landcover"]["purpose"] = "exposure"
    pack_path = tmp_path / "smuggled-exposure.pack.json"
    pack_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(MountainPackError, match="cannot declare purpose 'exposure'"):
        load_mountain_pack(_settings(tmp_path, pack_path))


def test_pack_accepts_a_declared_exposure_asset(tmp_path: Path) -> None:
    raw = _pack()
    raw["assets"]["exposure_features"] = _asset(
        "static/openstreetmap/features.geojson", "geojson", purpose="exposure"
    )
    raw["assets"]["exposure_features"]["required"] = False
    pack_path = tmp_path / "exposure.pack.json"
    pack_path.write_text(json.dumps(raw), encoding="utf-8")

    pack, _ = load_mountain_pack(_settings(tmp_path, pack_path))

    assert pack.assets["exposure_features"].purpose == "exposure"
    assert pack.assets["exposure_features"].required is False


def test_unknown_vertical_datum_is_reported_not_invented(tmp_path: Path) -> None:
    pack_path = tmp_path / "unknown-datum.pack.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")

    pack, _ = load_mountain_pack(_settings(tmp_path, pack_path))

    assert any("vertical datum is unknown" in warning for warning in pack.warnings())
    assert any("not calibrated" in warning for warning in pack.warnings())


def test_required_declared_source_must_exist(tmp_path: Path) -> None:
    pack_path = tmp_path / "missing-source.pack.json"
    pack_path.write_text(json.dumps(_pack()), encoding="utf-8")
    pack, _ = load_mountain_pack(_settings(tmp_path, pack_path))

    with pytest.raises(MountainPackError, match="Required 'aoi' asset is missing"):
        validate_declared_sources(pack, tmp_path / "data")


def test_geographic_degree_grid_is_rejected_before_bake(tmp_path: Path) -> None:
    raw = _pack()
    raw["grid"]["analysis_crs"] = "EPSG:4326"
    pack_path = tmp_path / "degrees.pack.json"
    pack_path.write_text(json.dumps(raw), encoding="utf-8")
    pack, _ = load_mountain_pack(_settings(tmp_path, pack_path))

    with pytest.raises(MountainPackError, match="projected with metre horizontal units"):
        validate_declared_sources(pack, tmp_path / "data")
