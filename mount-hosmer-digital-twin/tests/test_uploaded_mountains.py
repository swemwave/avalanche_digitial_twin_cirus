r"""The uploaded-mountain path: identity, containment, rejection, and one bake.

The rejection corpus is the point of this file. Every way a raster can be wrong is
built here as a real file and pushed through the real probe, because the failure
modes are the feature: a DEM that opens fine and is silently in the wrong vertical
unit produces an authoritative-looking mountain with every slope inflated ~3.28x.

Like ``test_full_synthetic_bake``, the probe tests need the offline geospatial
stack and skip cleanly where it is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.core.settings import Settings
from app.mountains import (
    MountainError,
    SLUG_PATTERN,
    mountain_root,
    new_mountain_id,
    read_registry,
    runtime_root_for,
    slugify,
    sweep_orphaned_staging,
    validate_mountain_id,
)

rasterio = pytest.importorskip(
    "rasterio", reason="bake-only dependency; see backend/requirements-bake.txt"
)
from rasterio.transform import from_origin  # noqa: E402

from app.processing.upload_probe import (  # noqa: E402
    ProbeRejection,
    _utm_epsg,
    inspect_raster,
    probe,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"

# Colorado UTM 13N. A real place-shaped grid, entirely synthetic values.
CRS = "EPSG:32613"
WEST, NORTH = 456000.0, 4402000.0


def _write(path: Path, values: np.ndarray, **overrides) -> Path:
    options = {
        "driver": "GTiff",
        "width": values.shape[1],
        "height": values.shape[0],
        "count": 1,
        "dtype": str(values.dtype),
        "crs": CRS,
        "transform": from_origin(WEST, NORTH, 10.0, 10.0),
        "nodata": -9999.0,
        "compress": "deflate",
    }
    options.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **options) as dataset:
        dataset.write(values, 1)
    return path


def _mountain_dem(size: int = 120) -> np.ndarray:
    """A ridge with real relief and plausible slopes."""
    y, x = np.mgrid[0:size, 0:size].astype("float32")
    return (2600.0 + 700.0 * np.exp(-(((x - size / 2) / (size / 4)) ** 2)) - y * 1.5).astype("float32")


def _source_root(tmp_path: Path, dem: np.ndarray | None = None, **overrides) -> Path:
    root = tmp_path / "mountain" / "source"
    _write(root / "static" / "dem.tif", _mountain_dem() if dem is None else dem, **overrides)
    return root


def _probe(root: Path, **overrides):
    arguments = {
        "source_root": root,
        "mountain_id": "fixture-mountain",
        "name": "Fixture Mountain",
        "provider": "Synthetic fixture",
        "citation": "Generated for upload verification",
        "licence": "CC0-1.0",
        "vertical_units_affirmed": True,
        "requested_resolution_m": None,
        "backend_root": BACKEND_ROOT,
    }
    arguments.update(overrides)
    return probe(**arguments)


# --- Identity and containment -------------------------------------------------


def test_uploaded_mountain_id_matches_the_pack_slug_rule() -> None:
    """The duplicated slug rule must not drift from the pack's own.

    ``app.mountains`` cannot import ``MountainPack`` -- the serving process never
    loads that module -- so the regex is copied. This is what stops the copy from
    silently diverging.
    """
    from app.processing.mountain_pack import MountainPack

    for candidate in ("mount-hosmer", "a", "peak-2-b", "x9"):
        assert SLUG_PATTERN.fullmatch(candidate)
        assert MountainPack.slug_id(candidate) == candidate

    for bad in ("Mount-Hosmer", "mount_hosmer", "-leading", "trailing-", "two--hyphens", "", "a b"):
        assert not SLUG_PATTERN.fullmatch(bad)
        with pytest.raises(ValueError):
            MountainPack.slug_id(bad)


@pytest.mark.parametrize(
    "hostile",
    ["../escape", "..", "a/b", "a\\b", "C:/windows", "/etc/passwd", "mount hosmer", "MOUNT", ""],
)
def test_hostile_mountain_ids_never_reach_the_filesystem(tmp_path: Path, hostile: str) -> None:
    settings = Settings(runtime_root=tmp_path, data_root=tmp_path / "data")
    with pytest.raises(MountainError):
        validate_mountain_id(hostile)
    with pytest.raises(MountainError):
        mountain_root(settings, hostile)


def test_generated_ids_are_slugs_even_from_hostile_names() -> None:
    for name in ("../../etc/passwd", "Mount Hosmer", "Brämabühl 2019", "!!!", "  "):
        generated = new_mountain_id(name)
        assert SLUG_PATTERN.fullmatch(generated), generated
        assert validate_mountain_id(generated) == generated


def test_slugify_strips_accents_and_punctuation() -> None:
    assert slugify("Brämabühl 2019") == "bramabuhl-2019"
    assert slugify("!!!") == ""


def test_runtime_root_for_defaults_to_the_reviewed_bake(tmp_path: Path) -> None:
    """No mountain id means Mount Hosmer, so existing clients are unaffected."""
    settings = Settings(runtime_root=tmp_path, data_root=tmp_path / "data")
    assert runtime_root_for(settings, None) == tmp_path
    assert runtime_root_for(settings, "other-peak") == tmp_path / "mountains" / "other-peak"
    # An uploaded mountain can never resolve onto the reviewed bake.
    assert runtime_root_for(settings, "other-peak") != runtime_root_for(settings, None)


def test_sweep_removes_orphaned_staging_directories(tmp_path: Path) -> None:
    settings = Settings(runtime_root=tmp_path, data_root=tmp_path / "data")
    mountain = tmp_path / "mountains" / "abandoned-peak"
    staging = mountain / ".baked-build-xyz"
    staging.mkdir(parents=True)
    (staging / "partial.npy").write_bytes(b"x")
    keep = mountain / "baked"
    keep.mkdir()
    (keep / "meta.json").write_text("{}", encoding="utf-8")

    removed = sweep_orphaned_staging(settings)

    assert staging in removed
    assert not staging.exists()
    assert (keep / "meta.json").exists()


def test_registry_survives_a_corrupt_file(tmp_path: Path) -> None:
    """An unreadable registry must not take Mount Hosmer down with it."""
    settings = Settings(runtime_root=tmp_path, data_root=tmp_path / "data")
    registry = tmp_path / "mountains" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    assert read_registry(settings) == {}


# --- The rejection corpus -----------------------------------------------------


def test_a_valid_dem_is_accepted(tmp_path: Path) -> None:
    result = _probe(_source_root(tmp_path))
    assert result["ok"] is True
    assert result["grid"]["analysis_crs"] == CRS
    assert result["assessable"] is False  # no land cover supplied
    assert Path(result["pack_path"]).is_file()


def test_missing_landcover_is_declared_optional_and_absent(tmp_path: Path) -> None:
    """The one legal way to say 'no forest data', and it must stay legal.

    A pack must declare the landcover role, so a DEM-only upload declares it
    optional and absent. The consequence is severe and must be stated rather than
    swallowed: forest_mask is a required layer of the release model, so the
    mountain renders but assesses nothing at all.
    """
    result = _probe(_source_root(tmp_path))
    pack = json.loads(Path(result["pack_path"]).read_text(encoding="utf-8"))
    landcover = pack["assets"]["landcover"]

    assert landcover["required"] is False
    assert result["assessable"] is False
    assert any("cannot be assessed" in warning for warning in result["warnings"])
    assert any("0%" in warning for warning in result["warnings"])


def test_supplied_landcover_makes_the_mountain_assessable(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    classes = np.full((120, 120), 60, dtype="uint8")
    classes[80:, :] = 10  # ESA WorldCover tree cover
    _write(root / "static" / "landcover.tif", classes, dtype="uint8", nodata=0)

    result = _probe(root)

    assert result["assessable"] is True
    pack = json.loads(Path(result["pack_path"]).read_text(encoding="utf-8"))
    assert pack["assets"]["landcover"]["required"] is True


def test_raster_without_a_crs_is_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path, crs=None)
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(root / "static" / "dem.tif")
    assert caught.value.code == "no_crs"


def test_non_square_cells_are_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path, transform=from_origin(WEST, NORTH, 10.0, 25.0))
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(root / "static" / "dem.tif")
    assert caught.value.code == "non_square_cells"


def test_multi_band_raster_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rgb.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path, "w", driver="GTiff", width=16, height=16, count=3, dtype="uint8",
        crs=CRS, transform=from_origin(WEST, NORTH, 10.0, 10.0),
    ) as dataset:
        dataset.write(np.zeros((3, 16, 16), dtype="uint8"))
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(path)
    assert caught.value.code == "not_single_band"


def test_all_nodata_raster_is_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path, dem=np.full((64, 64), -9999.0, dtype="float32"))
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(root / "static" / "dem.tif")
    assert caught.value.code == "all_nodata"


def test_constant_elevation_is_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path, dem=np.full((64, 64), 1500.0, dtype="float32"))
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(root / "static" / "dem.tif")
    assert caught.value.code == "constant_elevation"


def test_a_renamed_png_is_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "mountain" / "source" / "static" / "dem.tif"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(fake)
    assert caught.value.code == "not_a_raster"


def test_a_truncated_tiff_is_rejected(tmp_path: Path) -> None:
    root = _source_root(tmp_path)
    dem = root / "static" / "dem.tif"
    dem.write_bytes(dem.read_bytes()[:200])
    with pytest.raises(ProbeRejection):
        inspect_raster(dem)


def test_a_mostly_nodata_raster_is_rejected(tmp_path: Path) -> None:
    values = _mountain_dem()
    values[10:, :] = -9999.0
    root = _source_root(tmp_path, dem=values)
    with pytest.raises(ProbeRejection) as caught:
        inspect_raster(root / "static" / "dem.tif")
    assert caught.value.code == "mostly_nodata"


def test_a_flat_tile_has_no_terrain_to_simulate(tmp_path: Path) -> None:
    y, _ = np.mgrid[0:64, 0:64].astype("float32")
    root = _source_root(tmp_path, dem=(1500.0 + y * 0.1).astype("float32"))
    with pytest.raises(ProbeRejection) as caught:
        _probe(root)
    assert caught.value.code == "insufficient_relief"


def test_elevations_far_outside_the_plausible_range_are_rejected(tmp_path: Path) -> None:
    """A metre-labelled DEM whose values cannot be metres."""
    root = _source_root(tmp_path, dem=(_mountain_dem() * 12.0).astype("float32"))
    with pytest.raises(ProbeRejection) as caught:
        _probe(root)
    assert caught.value.code == "implausible_elevation_range"


def test_feet_valued_elevations_are_flagged_but_never_converted(tmp_path: Path) -> None:
    """The one failure that is silent and plausible, so it is warned about loudly.

    Feet over a metre grid inflate every gradient by ~3.28. The values must be
    passed through exactly as supplied -- detect and warn, never auto-convert,
    because a wrong guess corrupts the model as badly as the original error.
    """
    # Real relief that stays inside the plausible metre range while producing
    # impossible slopes: only the gradient betrays the unit.
    size = 120
    y, x = np.mgrid[0:size, 0:size].astype("float32")
    steep = (1500.0 + 2000.0 * np.exp(-(((x - 60) / 6.0) ** 2)) - y * 2.0).astype("float32")
    root = _source_root(tmp_path, dem=steep)

    result = _probe(root)

    assert result["ok"] is True, "a suspicious unit must warn, not reject"
    assert any("feet" in warning for warning in result["warnings"])
    pack = json.loads(Path(result["pack_path"]).read_text(encoding="utf-8"))
    # The elevations reach the bake untouched.
    assert pack["assets"]["elevation_primary"]["units"].startswith("metres")


def test_an_unaffirmed_vertical_unit_is_recorded_in_the_pack(tmp_path: Path) -> None:
    result = _probe(_source_root(tmp_path), vertical_units_affirmed=False)
    pack = json.loads(Path(result["pack_path"]).read_text(encoding="utf-8"))
    assert "unverified" in pack["assets"]["elevation_primary"]["units"]
    assert any("did not affirm" in warning for warning in result["warnings"])


# --- Grid derivation ----------------------------------------------------------


def test_a_geographic_raster_gets_a_metre_utm_grid(tmp_path: Path) -> None:
    """read_aligned reprojects, so only the analysis CRS must be metre-based."""
    root = tmp_path / "mountain" / "source"
    _write(
        root / "static" / "dem.tif",
        _mountain_dem(),
        crs="EPSG:4326",
        transform=from_origin(-105.51, 39.75, 0.0005, 0.0005),
    )
    result = _probe(root)

    assert result["ok"] is True
    assert result["grid"]["analysis_crs"] != "EPSG:4326"
    assert result["grid"]["analysis_crs"].startswith("EPSG:326")


def test_utm_zone_selection() -> None:
    assert _utm_epsg(-105.5, 39.7) == "EPSG:32613"
    assert _utm_epsg(9.85, 46.79) == "EPSG:32632"
    assert _utm_epsg(9.85, -46.79) == "EPSG:32732"


def test_the_synthesized_aoi_is_wgs84(tmp_path: Path) -> None:
    """exposure.build_exposure hardcodes EPSG:4326 for the AOI and ignores the
    GeoJSON ``crs`` member, so an AOI in the analysis CRS would be silently
    misread as longitude/latitude the moment an exposure asset is declared."""
    root = _source_root(tmp_path)
    _probe(root)
    aoi = json.loads((root / "metadata" / "aoi.geojson").read_text(encoding="utf-8"))
    ring = aoi["features"][0]["geometry"]["coordinates"][0]

    assert all(-180.0 <= x <= 180.0 and -90.0 <= y <= 90.0 for x, y in ring)


def test_the_pixel_budget_coarsens_rather_than_crops(tmp_path: Path) -> None:
    """A budget overrun must not quietly discard part of the user's extent."""
    import app.processing.upload_probe as upload_probe

    original = upload_probe.MAX_ANALYSIS_CELLS_LIMIT
    upload_probe.MAX_ANALYSIS_CELLS_LIMIT = 400
    try:
        root = _source_root(tmp_path)
        result = _probe(root)
    finally:
        upload_probe.MAX_ANALYSIS_CELLS_LIMIT = original

    grid = result["grid"]
    assert grid["cells"] <= 400
    assert grid["resolution_m"] > grid["native_resolution_m"]
    # The full footprint survives: only the cell size changed.
    assert grid["bounds"][2] - grid["bounds"][0] == pytest.approx(1200.0, rel=0.05)
    assert any("cell budget" in warning for warning in result["warnings"])


def test_the_analysis_grid_never_claims_more_detail_than_the_source(tmp_path: Path) -> None:
    result = _probe(_source_root(tmp_path), requested_resolution_m=1.0)
    grid = result["grid"]
    assert grid["resolution_m"] >= grid["native_resolution_m"]


# --- Through the API ----------------------------------------------------------


def _upload(client, tmp_path: Path, *, with_landcover: bool, name: str = "API Test Peak"):
    dem = _write(tmp_path / "upload" / "dem.tif", _mountain_dem())
    files = {"dem": ("dem.tif", dem.read_bytes(), "image/tiff")}
    if with_landcover:
        classes = np.full((120, 120), 60, dtype="uint8")
        classes[80:, :] = 10
        landcover = _write(tmp_path / "upload" / "landcover.tif", classes, dtype="uint8", nodata=0)
        files["landcover"] = ("landcover.tif", landcover.read_bytes(), "image/tiff")
    return client.post(
        "/api/mountains",
        files=files,
        data={
            "name": name,
            "provider": "Synthetic fixture",
            "citation": "Generated for upload verification",
            "licence": "CC0-1.0",
            "elevations_are_metres": "true",
        },
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The combined app, serving from an empty disposable runtime root."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.delenv("AVALANCHE_ASSESS_URL", raising=False)
    from app.core import settings as settings_module

    settings_module._cached = None
    from app.main import app

    with TestClient(app) as active:
        yield active


def test_uploading_bakes_serves_and_assesses_a_second_mountain(client, tmp_path: Path) -> None:
    """The whole path, through the real API: upload, bake, serve tiles, assess."""
    response = _upload(client, tmp_path, with_landcover=True)
    assert response.status_code == 202, response.text
    mountain_id = response.json()["id"]

    record = client.get(f"/api/mountains/{mountain_id}").json()
    assert record["status"] == "ready", record.get("error")
    assert record["assessable"] is True
    # Uploaded terrain has no acquisition manifest, so it can never be presented
    # with the same standing as the reviewed mountain.
    assert record["verified_source"] is False

    meta = client.get(f"/api/twin/meta?mountain={mountain_id}").json()
    assert meta["grid"]["crs"] == CRS
    assert meta["imagery"] is None  # an uploaded mountain never has imagery
    template = meta["tiles"]["url_template"]
    assert f"mountain={mountain_id}" in template

    minimum, maximum = meta["tiles"]["min_zoom"], meta["tiles"]["max_zoom"]
    served = [
        zoom for zoom in range(minimum, maximum + 1)
        if client.get(template.format(z=zoom, **_tile_xy(meta, zoom))).status_code == 200
    ]
    assert served, "no terrain tile was served for the uploaded mountain"

    assessment = client.post(
        f"/api/assess?mountain={mountain_id}",
        json={"new_snow_cm": 40, "wind_speed_kmh": 35, "wind_direction_deg": 270,
              "release_size": "medium"},
    )
    assert assessment.status_code == 200, assessment.text
    assert assessment.json()["coverage"]["release_model"]["valid_fraction"] > 0.9


def _tile_xy(meta: dict, zoom: int) -> dict[str, int]:
    """WebMercatorQuad tile containing the mountain centre at one zoom."""
    import math

    longitude, latitude = meta["center_wgs84"]
    count = 2**zoom
    radians = math.radians(latitude)
    return {
        "x": int((longitude + 180.0) / 360.0 * count),
        "y": int(
            (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0 * count
        ),
    }


def test_a_dem_only_upload_bakes_but_reports_itself_unassessable(client, tmp_path: Path) -> None:
    """The blank-mountain case, stated up front rather than after an empty result.

    forest_mask is a required layer of the release model, so with no land cover
    every cell is missing and the assessment produces no zones at all. That is the
    honest outcome -- missing data must never become a safe zero -- but the UI has
    to be able to say so before the user runs anything.
    """
    response = _upload(client, tmp_path, with_landcover=False, name="Bare Peak")
    mountain_id = response.json()["id"]
    record = client.get(f"/api/mountains/{mountain_id}").json()

    assert record["status"] == "ready"
    assert record["assessable"] is False
    assert any("cannot be assessed" in warning for warning in record["warnings"])

    assessment = client.post(
        f"/api/assess?mountain={mountain_id}",
        json={"new_snow_cm": 40, "wind_speed_kmh": 35, "wind_direction_deg": 270,
              "release_size": "medium"},
    ).json()
    assert assessment["coverage"]["release_model"]["valid_cell_count"] == 0
    assert assessment["area_hazard_index"] is None


def test_an_unknown_mountain_is_404_not_a_silent_fallback_to_hosmer(client) -> None:
    """Rendering one mountain's result over another's terrain is the worst bug
    available to this feature, so an id that does not resolve must never quietly
    become the default mountain."""
    assert client.get("/api/twin/meta?mountain=../../etc").status_code == 404
    assert client.get("/api/twin/tiles/12/1/1.png?mountain=not%20a%20slug").status_code == 404
    assert client.get("/api/mountains/Not-A-Slug").status_code == 400


def test_the_upload_cap_is_enforced_while_streaming(client, tmp_path: Path, monkeypatch) -> None:
    """The middleware only reads Content-Length, so the real ceiling is here."""
    import app.api.mountains as mountains_api

    monkeypatch.setattr(mountains_api, "MAX_UPLOAD_BYTES", 1024)
    response = client.post(
        "/api/mountains",
        files={"dem": ("dem.tif", b"\x00" * 8192, "image/tiff")},
        data={
            "name": "Too Big", "provider": "p", "citation": "c", "licence": "l",
            "elevations_are_metres": "true",
        },
    )
    assert response.status_code == 413
    # The partial file and its registry entry are both gone.
    assert client.get("/api/mountains").json()["mountains"] == []


def test_a_rejected_raster_fails_the_mountain_with_the_validator_message(
    client, tmp_path: Path
) -> None:
    response = client.post(
        "/api/mountains",
        files={"dem": ("dem.tif", b"\x89PNG\r\n\x1a\n" + b"\x00" * 512, "image/tiff")},
        data={
            "name": "Renamed PNG", "provider": "p", "citation": "c", "licence": "l",
            "elevations_are_metres": "true",
        },
    )
    assert response.status_code == 202
    record = client.get(f"/api/mountains/{response.json()['id']}").json()
    assert record["status"] == "failed"
    assert "could not be opened as a raster" in record["error"]


def test_the_mountain_cap_is_enforced(client, tmp_path: Path, monkeypatch) -> None:
    import app.api.mountains as mountains_api

    monkeypatch.setattr(mountains_api, "MAX_UPLOADED_MOUNTAINS", 1)
    assert _upload(client, tmp_path, with_landcover=False, name="First").status_code == 202
    second = _upload(client, tmp_path, with_landcover=False, name="Second")
    assert second.status_code == 409
    assert "maximum" in second.json()["error"]["message"]
