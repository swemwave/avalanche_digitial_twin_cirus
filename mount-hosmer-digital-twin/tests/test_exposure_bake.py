"""The bake-time exposure build: classification, derivation, and rasterisation.

Bake-time only -- this module imports pyproj and shapely, exactly like the code it
tests. The fixture is a hand-built OSM-shaped extract on a small projected grid, so
every assertion is about the rule, not about the real Elk Valley.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.processing.exposure import (
    EXPOSURE_CLASSES,
    SETTLEMENT_CLUSTER_BUFFER_M,
    SETTLEMENT_MIN_ROAD_LENGTH_M,
    ExposureError,
    build_exposure,
)
from app.processing.harmonization.grids import AnalysisGrid

SOURCE = {
    "provider": "OpenStreetMap contributors",
    "citation": "Synthetic Overpass-shaped extract",
    "licence": "Open Database License (ODbL) 1.0",
}


def _grid() -> AnalysisGrid:
    return AnalysisGrid(
        name="terrain",
        resolution_m=5.0,
        west=640000.0,
        south=5490000.0,
        east=641000.0,
        north=5491000.0,
        crs_string="EPSG:26911",
    )


def _to_wgs84(points: list[tuple[float, float]]) -> list[list[float]]:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:26911", "EPSG:4326", always_xy=True)
    return [list(transformer.transform(easting, northing)) for easting, northing in points]


def _write(path: Path, features: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
    )
    return path


def _line(properties: dict, points: list[tuple[float, float]]) -> dict:
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "LineString", "coordinates": _to_wgs84(points)},
    }


def _aoi(tmp_path: Path, *, inset: float = 0.0) -> Path:
    grid = _grid()
    box = [
        (grid.west + inset, grid.south + inset),
        (grid.east - inset, grid.south + inset),
        (grid.east - inset, grid.north - inset),
        (grid.west + inset, grid.north - inset),
        (grid.west + inset, grid.south + inset),
    ]
    return _write(
        tmp_path / "aoi.geojson",
        [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {"type": "Polygon", "coordinates": [_to_wgs84(box)]},
            }
        ],
    )


def _build(tmp_path: Path, features: list[dict], *, inset: float = 0.0):
    return build_exposure(
        source_path=_write(tmp_path / "osm.geojson", features),
        aoi_path=_aoi(tmp_path, inset=inset),
        grid=_grid(),
        source_statement=SOURCE,
    )


# --- Classification ----------------------------------------------------------


def test_each_tag_routes_to_its_documented_class(tmp_path: Path) -> None:
    products = _build(
        tmp_path,
        [
            _line({"highway": "trunk", "name": "Trunk"}, [(640100, 5490100), (640900, 5490100)]),
            _line({"highway": "trunk_link"}, [(640100, 5490150), (640300, 5490150)]),
            _line({"railway": "rail"}, [(640100, 5490200), (640900, 5490200)]),
            _line({"highway": "residential"}, [(640100, 5490300), (640400, 5490300)]),
            _line({"highway": "unclassified"}, [(640100, 5490350), (640400, 5490350)]),
            _line({"highway": "service"}, [(640100, 5490400), (640200, 5490400)]),
            _line({"highway": "track"}, [(640100, 5490500), (640500, 5490500)]),
            _line({"highway": "path"}, [(640100, 5490550), (640500, 5490550)]),
        ],
    )
    counts = {item["name"]: item["feature_count"] for item in products.meta["classes"]}
    assert counts["highway_major"] == 2
    assert counts["railway"] == 1
    assert counts["road_local"] == 3
    assert counts["track_trail"] == 2


def test_waterways_power_and_point_nodes_are_excluded_on_the_record(tmp_path: Path) -> None:
    products = _build(
        tmp_path,
        [
            _line({"waterway": "stream"}, [(640100, 5490100), (640900, 5490100)]),
            _line({"power": "line"}, [(640100, 5490200), (640900, 5490200)]),
            {
                "type": "Feature",
                "properties": {"railway": "switch"},
                "geometry": {"type": "Point", "coordinates": _to_wgs84([(640500, 5490500)])[0]},
            },
        ],
    )
    assert products.meta["classified_feature_count"] == 0
    assert products.meta["unclassified_feature_count"] == 3
    # The omission is a recorded decision, not an accident.
    assert set(products.meta["excluded_tags"]) == {"waterway", "power", "node_features"}
    assert float(np.asarray(products.weight.filled(0.0)).max()) == 0.0


def test_the_single_mapped_building_is_kept(tmp_path: Path) -> None:
    ring = [
        (640500, 5490500),
        (640520, 5490500),
        (640520, 5490520),
        (640500, 5490520),
        (640500, 5490500),
    ]
    products = _build(
        tmp_path,
        [
            {
                "type": "Feature",
                "properties": {"building": "yes"},
                "geometry": {"type": "Polygon", "coordinates": [_to_wgs84(ring)]},
            }
        ],
    )
    counts = {item["name"]: item["feature_count"] for item in products.meta["classes"]}
    assert counts["building_mapped"] == 1
    assert float(np.asarray(products.weight.filled(0.0)).max()) == pytest.approx(0.9)


# --- Weights and rasterisation ----------------------------------------------


def test_the_most_consequential_class_wins_a_shared_cell(tmp_path: Path) -> None:
    """Weight is a max over classes, never a sum -- so it stays inside 0-1."""
    track = {"highway": "track"}
    trunk = {"highway": "trunk"}
    line = [(640100, 5490500), (640900, 5490500)]
    products = _build(tmp_path, [_line(track, line), _line(trunk, line)])

    weight = np.asarray(products.weight.filled(0.0))
    codes = np.asarray(products.class_code.filled(255))
    covered = weight > 0
    assert weight.max() == pytest.approx(0.9)
    assert weight[covered].min() >= 0.2
    assert weight.max() <= 1.0
    # Every cell on the shared centreline carries the trunk code, not the track's.
    trunk_code = next(item["code"] for item in EXPOSURE_CLASSES if item["name"] == "highway_major")
    assert codes[weight == 0.9].min() == trunk_code


def test_the_class_buffer_gives_a_centreline_real_ground_width(tmp_path: Path) -> None:
    grid = _grid()
    products = _build(
        tmp_path,
        [_line({"highway": "trunk"}, [(640100, 5490500), (640900, 5490500)])],
    )
    weight = np.asarray(products.weight.filled(0.0))
    covered_rows = np.unique(np.nonzero(weight > 0)[0])
    # A 15 m buffer on a 5 m grid must light up more than one row of cells.
    assert len(covered_rows) >= 3
    assert products.meta["exposed_area_m2"] == pytest.approx(
        int(np.count_nonzero(weight > 0)) * grid.resolution_m**2
    )


def test_ground_outside_the_aoi_is_unknown_not_zero_weight(tmp_path: Path) -> None:
    products = _build(
        tmp_path,
        [_line({"highway": "trunk"}, [(640100, 5490500), (640900, 5490500)])],
        inset=200.0,
    )
    mask = np.ma.getmaskarray(products.weight)
    assert mask.any(), "an inset AOI must leave the surrounding cells masked"
    assert not mask.all()
    # Masked cells are NaN in the array the bake writes, never 0.0.
    assert np.isnan(np.asarray(products.weight)[mask]).all()
    assert np.ma.getmaskarray(products.class_code).tolist() == mask.tolist()
    assert products.meta["aoi_cell_count"] == int(np.count_nonzero(~mask))


def test_a_blank_cell_inside_the_aoi_is_a_measured_zero(tmp_path: Path) -> None:
    products = _build(
        tmp_path, [_line({"highway": "trunk"}, [(640100, 5490500), (640900, 5490500)])]
    )
    weight = products.weight
    blank = ~np.ma.getmaskarray(weight) & (np.asarray(weight.filled(np.nan)) == 0.0)
    assert blank.any()
    # Inside the AOI the extract covers the ground, so 0 records "nothing mapped
    # here" and is distinct from the masked unknown above.
    assert products.meta["exposed_fraction_of_aoi"] < 1.0


# --- The derived settlement outlines ----------------------------------------


def _street_grid(easting: float, northing: float, streets: int, length: float) -> list[dict]:
    return [
        _line(
            {"highway": "residential"},
            [(easting, northing + index * 30.0), (easting + length, northing + index * 30.0)],
        )
        for index in range(streets)
    ]


def test_a_dense_street_cluster_becomes_an_inferred_settlement(tmp_path: Path) -> None:
    products = _build(tmp_path, _street_grid(640100.0, 5490100.0, streets=8, length=250.0))
    report = products.meta["settlement_derivation"]

    assert report["accepted_cluster_count"] == 1
    assert report["accepted_road_length_m"][0] >= SETTLEMENT_MIN_ROAD_LENGTH_M
    assert report["cluster_buffer_m"] == SETTLEMENT_CLUSTER_BUFFER_M
    assert report["is_survey_of_structures"] is False
    counts = {item["name"]: item["feature_count"] for item in products.meta["classes"]}
    assert counts["inferred_settlement"] == 1
    assert float(np.asarray(products.weight.filled(0.0)).max()) == pytest.approx(1.0)


def test_a_lone_access_road_never_clears_the_threshold(tmp_path: Path) -> None:
    products = _build(
        tmp_path, [_line({"highway": "residential"}, [(640100, 5490500), (640300, 5490500)])]
    )
    report = products.meta["settlement_derivation"]

    assert report["accepted_cluster_count"] == 0
    assert report["rejected_road_length_m"] == [200.0]
    counts = {item["name"]: item["feature_count"] for item in products.meta["classes"]}
    assert counts["inferred_settlement"] == 0


def test_the_derived_outline_is_labelled_derived_everywhere_it_is_published(
    tmp_path: Path,
) -> None:
    products = _build(tmp_path, _street_grid(640100.0, 5490100.0, streets=8, length=250.0))

    outlines = [
        feature
        for feature in products.features["features"]
        if feature["properties"]["exposure_class"] == "inferred_settlement"
    ]
    assert outlines and all(feature["properties"]["derived"] is True for feature in outlines)
    assert products.features["derived_classes"] == ["inferred_settlement"]
    assert "INFERRED" in products.features["limitation"]
    assert "not a survey of occupied structures" in products.meta["limitation"]
    # No place is named that OSM did not name.
    assert all("name" not in feature["properties"] for feature in outlines)


# --- Display output, licence and provenance ---------------------------------


def test_display_features_are_wgs84_and_keep_their_names(tmp_path: Path) -> None:
    products = _build(
        tmp_path,
        [
            _line(
                {"highway": "trunk", "name": "Crowsnest Highway", "ref": "3"},
                [(640100, 5490500), (640900, 5490500)],
            )
        ],
    )
    feature = products.features["features"][0]

    assert feature["properties"]["name"] == "Crowsnest Highway"
    assert feature["properties"]["ref"] == "3"
    assert feature["properties"]["exposure_class"] == "highway_major"
    assert feature["properties"]["exposure_label"] == "Trunk highway"
    longitude, latitude = feature["geometry"]["coordinates"][0]
    assert -116.0 < longitude < -114.0 and 49.0 < latitude < 50.0


def test_the_licence_and_attribution_travel_with_the_data(tmp_path: Path) -> None:
    products = _build(
        tmp_path, [_line({"highway": "trunk"}, [(640100, 5490500), (640900, 5490500)])]
    )
    assert products.meta["licence"] == "Open Database License (ODbL) 1.0"
    assert products.meta["attribution"] == "© OpenStreetMap contributors"
    assert products.meta["source"] == SOURCE
    assert products.features["attribution"] == products.meta["attribution"]
    assert products.features["licence"] == products.meta["licence"]
    assert products.meta["used_in_release_model"] is False
    assert products.meta["used_in_runout_model"] is False
    assert products.meta["is_calibrated"] is False


def test_the_build_is_deterministic_for_identical_input(tmp_path: Path) -> None:
    features = [
        _line({"highway": "trunk"}, [(640100, 5490500), (640900, 5490500)]),
        *_street_grid(640100.0, 5490100.0, streets=8, length=250.0),
    ]
    first = _build(tmp_path / "a", features)
    second = _build(tmp_path / "b", features)

    assert np.array_equal(
        np.asarray(first.weight.filled(-1.0)), np.asarray(second.weight.filled(-1.0))
    )
    assert first.features == second.features
    assert first.meta == second.meta


# --- The bake wiring ---------------------------------------------------------


def _pack(tmp_path: Path, *, with_exposure: bool) -> object:
    from app.processing.mountain_pack import MountainPack

    grid = _grid()
    asset = {
        "adapter": "geojson",
        "purpose": "model_input",
        "required": True,
        "units": "test",
        "source": SOURCE,
    }
    assets = {
        "aoi": {**asset, "href": "aoi.geojson"},
        "elevation_lidar": {**asset, "href": "lidar", "adapter": "geobc_lidar_year_tiles"},
        "elevation_fallback": {**asset, "href": "fallback.tif", "adapter": "single_raster"},
        "landcover": {**asset, "href": "landcover.tif", "adapter": "categorical_raster"},
    }
    if with_exposure:
        assets["exposure_features"] = {
            **asset,
            "href": "osm.geojson",
            "purpose": "exposure",
            "required": False,
        }
    return MountainPack.model_validate(
        {
            "schema_version": 1,
            "id": "exposure-bake-fixture",
            "name": "Exposure bake fixture",
            "center_wgs84": [-115.0, 49.55],
            "grid": {
                "analysis_crs": grid.crs_string,
                "coordinate_order": "easting,northing",
                "bounds": [grid.west, grid.south, grid.east, grid.north],
                "resolution_m": grid.resolution_m,
                "vertical_datum": {"status": "unknown", "name": None},
            },
            "model_profile": "synthetic-software-verification-only",
            "model_calibrated_locally": False,
            "assets": assets,
        }
    )


def test_the_bake_writes_the_layers_the_vector_and_the_meta_block(tmp_path: Path) -> None:
    from app.bake import _bake_exposure
    from app.core.settings import Settings

    data_root = tmp_path / "data"
    _write(
        data_root / "osm.geojson",
        [
            _line({"highway": "trunk", "name": "Trunk"}, [(640100, 5490500), (640900, 5490500)]),
            *_street_grid(640100.0, 5490100.0, streets=8, length=250.0),
        ],
    )
    _aoi(data_root)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=data_root,
    )
    out = tmp_path / "staging"
    (out / "layers").mkdir(parents=True)

    records, meta, source = _bake_exposure(settings, _pack(tmp_path, with_exposure=True), _grid(), out)

    assert [record["name"] for record in records] == ["exposure_weight", "exposure_class"]
    assert records[0]["nodata"] == "NaN" and records[0]["dtype"] == "float32"
    assert records[1]["nodata"] == 255 and records[1]["dtype"] == "uint8"
    assert (out / "layers" / "exposure_weight.npy").is_file()
    assert (out / "exposure" / "features.geojson").is_file()
    assert source == data_root / "osm.geojson"
    assert meta is not None
    assert meta["features_path"] == "exposure/features.geojson"
    assert len(meta["features_sha256"]) == 64
    assert meta["feature_count"] == len(json.loads(
        (out / "exposure" / "features.geojson").read_text(encoding="utf-8")
    )["features"])
    # The layer really is on disk with NaN outside the AOI, never 0.
    written = np.load(out / "layers" / "exposure_weight.npy")
    assert np.nanmax(written) == pytest.approx(1.0)


def test_a_pack_without_an_exposure_asset_still_bakes(tmp_path: Path) -> None:
    from app.bake import _bake_exposure
    from app.core.settings import Settings

    data_root = tmp_path / "data"
    _aoi(data_root)
    settings = Settings(
        project_root=tmp_path,
        backend_root=tmp_path / "backend",
        runtime_root=tmp_path / "runtime",
        data_root=data_root,
    )
    out = tmp_path / "staging"
    (out / "layers").mkdir(parents=True)

    records, meta, source = _bake_exposure(
        settings, _pack(tmp_path, with_exposure=False), _grid(), out
    )

    assert records == [] and meta is None and source is None
    assert not (out / "exposure").exists()


def test_a_tampered_exposure_layer_fails_bake_validation(tmp_path: Path) -> None:
    """The optional layers are validated exactly like the required ones."""
    import json as json_module

    from app.bake_identity import BakeCompatibilityError, bake_sha256, sha256_file, validate_bake
    from synthetic_baked import write_synthetic_baked

    baked = write_synthetic_baked(tmp_path)
    validate_bake(baked)  # the untouched bake is fine

    # Blank out a known cell and re-sign the file, so only the layer's own mask
    # statistics can catch it -- the optional layers must get the same scrutiny as
    # the required ones, not just a file hash.
    path = baked / "layers" / "exposure_weight.npy"
    values = np.load(path)
    values[10, 10] = np.nan
    np.save(path, values)
    meta = json_module.loads((baked / "meta.json").read_text(encoding="utf-8"))
    for record in meta["layers"]:
        if record["name"] == "exposure_weight":
            record["sha256"] = sha256_file(path)
    meta["identity"] = {"bake_sha256": bake_sha256(meta)}
    (baked / "meta.json").write_text(json_module.dumps(meta), encoding="utf-8")

    with pytest.raises(BakeCompatibilityError, match="exposure_weight"):
        validate_bake(baked)


def test_a_malformed_source_is_rejected_rather_than_silently_empty(tmp_path: Path) -> None:
    path = tmp_path / "broken.geojson"
    path.write_text(json.dumps({"type": "Topology"}), encoding="utf-8")
    with pytest.raises(ExposureError, match="not a GeoJSON FeatureCollection"):
        build_exposure(
            source_path=path,
            aoi_path=_aoi(tmp_path),
            grid=_grid(),
            source_statement=SOURCE,
        )
