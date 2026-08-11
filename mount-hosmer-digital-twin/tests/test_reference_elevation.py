from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from app.processing.terrain.reference_elevation import (
    ReferenceElevationError,
    canonical_reference_elevation_bytes,
    derive_reference_elevation,
    load_reference_elevation,
    write_reference_elevation,
)


def _inputs() -> tuple[dict, np.ma.MaskedArray, np.ma.MaskedArray]:
    elevation = np.ma.array(np.arange(16, dtype="float32").reshape(4, 4))
    source = np.ma.array(np.ones((4, 4), dtype="uint8"))
    meta = {
        "schema": "stage3-baked-v2",
        "mountain_pack": {"sha256": "a" * 64},
        "processing": {"sha256": "b" * 64},
        "sources": {"sha256": "f" * 64, "files": [{"path": "synthetic.tif"}]},
        "identity": {"bake_sha256": "c" * 64},
        "grid": {
            "crs": "EPSG:26911",
            "coordinate_order": "easting,northing",
            "resolution_m": 10.0,
            "west": 1000.0,
            "south": 1960.0,
            "east": 1040.0,
            "north": 2000.0,
            "width": 4,
            "height": 4,
            "transform": [10.0, 0.0, 1000.0, 0.0, -10.0, 2000.0],
            "vertical_datum": {"status": "unknown", "name": None},
        },
        "layers": [
            {"name": "elevation", "sha256": "d" * 64, "units": "m above sea level"},
            {"name": "terrain_source", "sha256": "e" * 64},
        ],
        "terrain": {"source_codes": {"1": "synthetic measured terrain"}},
        "reproject": {
            "cols": [0.0, 2.0, 4.0],
            "rows": [0.0, 2.0, 4.0],
            "lon": [[-1.0, 0.0, 1.0], [-1.0, 0.0, 1.0], [-1.0, 0.0, 1.0]],
            "lat": [[1.0, 1.0, 1.0], [0.0, 0.0, 0.0], [-1.0, -1.0, -1.0]],
        },
    }
    return meta, elevation, source


def test_reference_contract_records_coordinate_orders_cells_and_bilinear_value() -> None:
    meta, elevation, source = _inputs()

    contract = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=0.0,
        target_latitude_deg=0.0,
        legacy_elevation_m=7.0,
    )

    assert contract.target.projected_easting_m == pytest.approx(1020.0)
    assert contract.target.projected_northing_m == pytest.approx(1980.0)
    assert contract.grid.coordinate_order == "easting,northing"
    assert contract.grid.raster_index_order == "row,col"
    assert contract.grid.nearest_internal_edge_tie_convention.startswith("select east")
    assert contract.candidates["containing_cell"].footprint[0].row == 2
    assert contract.candidates["containing_cell"].footprint[0].col == 2
    assert contract.candidates["containing_cell"].elevation_m == pytest.approx(10.0)
    bilinear = contract.candidates["bilinear_four_cell"]
    assert [(cell.row, cell.col) for cell in bilinear.footprint] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]
    assert [cell.weight for cell in bilinear.footprint] == pytest.approx([0.25] * 4)
    assert bilinear.elevation_m == pytest.approx(7.5)
    assert contract.selection.difference_from_legacy_m == pytest.approx(0.5)
    assert contract.selection.activation_status.startswith("not_activated")


def test_reference_contract_rejects_swapped_lon_lat_order() -> None:
    meta, elevation, source = _inputs()
    with pytest.raises(ReferenceElevationError, match="longitude, latitude"):
        derive_reference_elevation(
            meta,
            elevation,
            source,
            target_longitude_deg=49.6,
            target_latitude_deg=-115.0,
            legacy_elevation_m=7.0,
        )


def test_bilinear_requires_every_elevation_and_source_mask() -> None:
    meta, elevation, source = _inputs()
    source.mask = np.zeros(source.shape, dtype=bool)
    source.mask[1, 1] = True

    contract = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=0.0,
        target_latitude_deg=0.0,
        legacy_elevation_m=7.0,
    )

    candidate = contract.candidates["bilinear_four_cell"]
    assert candidate.status == "masked_required_input"
    assert candidate.elevation_m is None
    assert contract.selection.proposed_reference_elevation_m is None
    assert any(cell.terrain_source_masked for cell in candidate.footprint)


def test_edge_target_does_not_extrapolate_or_shrink_bilinear_footprint() -> None:
    meta, elevation, source = _inputs()
    contract = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=-0.9,
        target_latitude_deg=0.9,
        legacy_elevation_m=7.0,
    )

    bilinear = contract.candidates["bilinear_four_cell"]
    assert bilinear.status == "outside_full_footprint"
    assert bilinear.elevation_m is None
    assert bilinear.footprint == ()


@pytest.mark.parametrize(
    ("longitude", "latitude", "expected_row", "expected_col", "expected_value"),
    [(-0.75, 0.75, 0, 0, 0.0), (0.75, -0.75, 3, 3, 15.0)],
)
def test_first_and_last_cell_centres_are_supported_without_extrapolation(
    longitude: float,
    latitude: float,
    expected_row: int,
    expected_col: int,
    expected_value: float,
) -> None:
    meta, elevation, source = _inputs()
    contract = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=longitude,
        target_latitude_deg=latitude,
        legacy_elevation_m=7.0,
    )
    bilinear = contract.candidates["bilinear_four_cell"]
    assert bilinear.status == "available"
    assert bilinear.elevation_m == pytest.approx(expected_value)
    positive = [cell for cell in bilinear.footprint if cell.weight > 0]
    assert [(cell.row, cell.col) for cell in positive] == [
        (expected_row, expected_col)
    ]


def test_reference_storage_is_atomic_idempotent_and_rejects_hash_corruption(
    tmp_path: Path,
) -> None:
    meta, elevation, source = _inputs()
    first = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=0.0,
        target_latitude_deg=0.0,
        legacy_elevation_m=7.0,
    )
    replay = derive_reference_elevation(
        meta,
        elevation,
        source,
        target_longitude_deg=0.0,
        target_latitude_deg=0.0,
        legacy_elevation_m=7.0,
    )
    assert canonical_reference_elevation_bytes(first) == canonical_reference_elevation_bytes(
        replay
    )

    target = write_reference_elevation(first, tmp_path)
    assert write_reference_elevation(replay, tmp_path) == target
    assert load_reference_elevation(target) == first
    assert not list(
        (tmp_path / "reports" / "terrain" / "reference-elevations").glob("*.staging")
    )

    contract_path = target / "reference-elevation.json"
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    payload["selection"]["proposed_reference_elevation_m"] = 999.0
    contract_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReferenceElevationError, match="SHA-256"):
        load_reference_elevation(target)
