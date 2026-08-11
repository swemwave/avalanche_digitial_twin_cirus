"""M2 elevation, local-correction, and report-integrity characterization tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from app.cli import build_parser
from app.processing.conditions.characterization import (
    CHARACTERIZATION_CONTRACT_REVISION,
    CHARACTERIZATION_SCHEMA,
    DISCLAIMER,
    ForcingCharacterizationError,
    _report_id,
    _validate_external_evidence_boundaries,
    characterize_bake_reference,
    characterize_temperature_and_precipitation,
    load_characterization_report,
    scientific_series_sha256,
    validate_characterization_report,
    write_characterization_report,
)
from app.processing.conditions.eccc import (
    import_eccc_snapshot,
    load_eccc_snapshot,
    mountain_grid_from_pack,
)
from app.processing.conditions.protocol import ConditionRequest
from app.processing.conditions.storage import load_condition_pack


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "conditions" / "eccc"
MOUNTAIN_PACK = Path(__file__).parents[1] / "backend" / "config" / "mount_hosmer.pack.json"


def _linear_bake() -> tuple[dict, np.ma.MaskedArray, np.ma.MaskedArray]:
    # Geographic target mapping is deliberately linear: lon=col, lat=4-row.
    meta = {
        "grid": {
            "crs": "TEST:metres",
            "resolution_m": 1.0,
            "width": 4,
            "height": 4,
            "transform": [1.0, 0.0, 0.0, 0.0, -1.0, 4.0],
        },
        "reproject": {
            "cols": [0.0, 4.0],
            "rows": [0.0, 4.0],
            "lon": [[0.0, 4.0], [0.0, 4.0]],
            "lat": [[4.0, 4.0], [0.0, 0.0]],
        },
        "terrain": {"source_codes": {"0": "missing", "2": "synthetic lidar"}},
    }
    elevation = np.ma.masked_array(
        np.arange(100.0, 116.0, dtype="float64").reshape(4, 4), mask=False
    )
    source = np.ma.masked_array(np.full((4, 4), 2, dtype="uint8"), mask=False)
    return meta, elevation, source


def test_bake_reference_respects_lon_lat_projected_order_cell_centres_and_units() -> None:
    meta, elevation, source = _linear_bake()
    report = characterize_bake_reference(
        meta,
        elevation,
        source,
        target_longitude_deg=2.25,
        target_latitude_deg=1.75,
        existing_reference_elevation_m=110.0,
    )
    target = report["target"]
    assert target["geographic_axis_order"] == "(longitude_deg, latitude_deg)"
    assert target["projected_easting_m"] == pytest.approx(2.25)
    assert target["projected_northing_m"] == pytest.approx(1.75)
    assert target["pixel_edge_col"] == pytest.approx(2.25)
    assert target["pixel_edge_row"] == pytest.approx(2.25)
    assert report["grid"]["axis_order"] == "projected (easting_m, northing_m)"
    assert report["grid"]["raster_index_order"] == "array[row, col]"

    legacy = report["existing_reference"]
    assert legacy["integer_array_midpoint_cell"]["row"] == 2
    assert legacy["integer_array_midpoint_cell"]["col"] == 2
    assert legacy["matches_midpoint_rounded_to_0_01_m"] is True
    containing = report["target_compatible_alternatives"]["containing_cell_center"]
    assert (containing["row"], containing["col"], containing["elevation_m"]) == (2, 2, 110.0)
    bilinear = report["target_compatible_alternatives"]["four_cell_bilinear_at_target"]
    assert bilinear["status"] == "available_all_four_cells_valid"
    assert sum(item["weight"] for item in bilinear["footprint"]) == pytest.approx(1.0)
    assert bilinear["elevation_m"] == pytest.approx(108.75)
    assert report["activation"]["status"] == "not_activated"


def test_bilinear_elevation_preserves_required_masks_and_never_substitutes_zero() -> None:
    meta, elevation, source = _linear_bake()
    elevation.mask[2, 2] = True
    report = characterize_bake_reference(
        meta,
        elevation,
        source,
        target_longitude_deg=2.25,
        target_latitude_deg=1.75,
        existing_reference_elevation_m=110.0,
    )
    bilinear = report["target_compatible_alternatives"]["four_cell_bilinear_at_target"]
    assert bilinear["status"] == "masked_one_or_more_required_cells"
    assert bilinear["elevation_m"] is None
    assert any(item["elevation_masked"] for item in bilinear["footprint"])


def test_target_outside_bake_is_rejected_instead_of_extrapolated() -> None:
    meta, elevation, source = _linear_bake()
    with pytest.raises(ForcingCharacterizationError, match="residual|outside"):
        characterize_bake_reference(
            meta,
            elevation,
            source,
            target_longitude_deg=20.0,
            target_latitude_deg=20.0,
            existing_reference_elevation_m=110.0,
        )


def test_temperature_sign_bounds_disagreement_and_reference_sensitivity(tmp_path: Path) -> None:
    snapshot = load_eccc_snapshot(
        import_eccc_snapshot(
            FIXTURE / "climate-stations.csv",
            FIXTURE / "climate-hourly.csv",
            tmp_path,
        )
    )
    request = ConditionRequest(
        mountain_grid_from_pack(MOUNTAIN_PACK),
        datetime(2025, 11, 1, 0, tzinfo=UTC),
        datetime(2025, 11, 1, 2, tzinfo=UTC),
    )
    temperature, precipitation, audit = characterize_temperature_and_precipitation(
        snapshot,
        request,
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        existing_reference_elevation_m=2496.78,
        alternative_elevations_m={"higher": 2500.78, "masked": None},
    )
    assert temperature["method"]["lapse_rate_k_per_km"] == 6.5
    assert "higher target" in temperature["method"]["sign_check"]
    assert temperature["uncorrected_station_disagreement"]["overlap_hours"] == 2
    assert temperature["uncorrected_station_disagreement"]["bias"] == pytest.approx(-1.4)
    assert temperature["both_stations_transferred_to_existing_reference_disagreement"][
        "bias"
    ] == pytest.approx(-1.53975)
    assert [
        item["lapse_rate_k_per_km"] for item in temperature["lapse_rate_sweep"]
    ] == [2.5, 3.9, 5.2, 6.5, 7.5]
    assert "independent" in temperature["target_elevation_invariance"]
    sensitivity = {item["reference_name"]: item for item in temperature["reference_elevation_sensitivity"]}
    assert sensitivity["higher"]["selected_temperature_change_k"] == pytest.approx(-0.026)
    assert sensitivity["masked"]["selected_temperature_change_k"] is None
    assert "not validation" in temperature["disagreement_change"]["claim_boundary"]
    assert precipitation["counts"] == {"positive": 1, "zero": 2}
    assert precipitation["orographic_correction"].startswith("disabled")
    assert precipitation["gauge_undercatch_correction"].startswith("disabled")
    assert audit["gap_fill_fraction"] == 0.0
    with pytest.raises(ForcingCharacterizationError, match="lapse_rate"):
        characterize_temperature_and_precipitation(
            snapshot,
            request,
            target_longitude_deg=-115.0,
            target_latitude_deg=49.6,
            existing_reference_elevation_m=2496.78,
            alternative_elevations_m={},
            lapse_rate_k_per_m=-0.0065,
        )


def _minimal_report() -> dict:
    value = {
        "schema": CHARACTERIZATION_SCHEMA,
        "disclaimer": DISCLAIMER,
        "valid_start_utc": "2025-11-01T00:00:00+00:00",
        "valid_end_utc": "2025-11-01T01:00:00+00:00",
        "target": {},
        "lineage": {},
        "bake_reference_elevation": {},
        "temperature_correction": {},
        "precipitation_characterization": {},
        "wind_characterization": {},
        "eccc_station_audit": {},
        "snow_depth_swe": {},
        "radiation": {},
        "terrain_representativeness": {},
        "correction_activation": {},
        "claim_boundary": "synthetic software-verification fixture",
    }
    return {**value, "report_id": _report_id(value)}


def test_report_schema_identity_hash_corruption_and_atomic_replay(tmp_path: Path) -> None:
    report = _minimal_report()
    assert validate_characterization_report(report) == report
    first = write_characterization_report(report, tmp_path)
    second = write_characterization_report(report, tmp_path)
    assert first == second
    assert load_characterization_report(first) == report
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()

    corrupt = json.loads((first / "report.json").read_text(encoding="utf-8"))
    corrupt["target"] = {"changed": True}
    (first / "report.json").write_text(json.dumps(corrupt), encoding="utf-8")
    with pytest.raises(ForcingCharacterizationError, match="identity|checksum"):
        load_characterization_report(first)


def test_manifest_is_strict_and_cli_exposes_cache_native_characterization() -> None:
    report = _minimal_report()
    report["unexpected"] = True
    with pytest.raises(ForcingCharacterizationError, match="unexpected"):
        validate_characterization_report(report)
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert "characterize-m2-forcing" in choices


def test_current_revision_rejects_empty_scientific_sections() -> None:
    report = _minimal_report()
    without_id = {key: value for key, value in report.items() if key != "report_id"}
    without_id["characterization_contract_revision"] = CHARACTERIZATION_CONTRACT_REVISION
    revised = {**without_id, "report_id": _report_id(without_id)}
    with pytest.raises(ForcingCharacterizationError, match="empty or malformed"):
        validate_characterization_report(revised)


def test_external_evidence_boundary_stays_blocked_and_non_activating() -> None:
    snow = {
        "status": "blocked_identity_history_qc_revision_semantics_not_proven",
        "comparison_metrics": None,
        "full_observation_downloaded_or_cached": False,
        "observation_values_used": False,
        "incidental_live_feed_response_encountered": True,
        "date_effective_identity_contract": "not_found_in_authoritative_metadata",
        "qc_revision_contract": {"status": "not_proven"},
        "candidate": {
            "provincial_current_location_records": {
                "2c09p": {"active": False},
                "2c09q": {"active": True},
            },
            "separate_pcic_histories": {
                "mor": {"history_id": 2885},
                "2c09p": {"history_id": 2950},
                "2c09q": {"history_id": 2951},
            },
        },
    }
    radiation = {
        "pcic_catalog_audit": {
            "exact_window_history_count": 0,
            "exact_window_both_components_history_count": 0,
            "historical_both_components_history_count": 5,
            "latest_historical_both_components_end_date": "2020-07-01T00:00:00",
            "eligibility": "none",
        },
        "eccc_archive_audit": {"representative_exact_window_station_found": False},
        "era5_land_gap_fill_candidate": {
            "status": "not_acquired_or_scientifically_evaluated_access_blocked",
            "catalogue_exact_window_available": True,
            "source_unit": "J m-2",
        },
    }
    _validate_external_evidence_boundaries(snow, radiation)

    selected = json.loads(json.dumps(radiation))
    selected["pcic_catalog_audit"]["eligibility"] = "selected"
    with pytest.raises(ForcingCharacterizationError, match="incomplete or activating"):
        _validate_external_evidence_boundaries(snow, selected)

    compared = json.loads(json.dumps(snow))
    compared["comparison_metrics"] = {"rmse": 0.0}
    with pytest.raises(ForcingCharacterizationError, match="incomplete or activating"):
        _validate_external_evidence_boundaries(compared, radiation)


def test_preserved_revision_1_1_report_remains_loadable() -> None:
    preserved = Path(__file__).parents[1] / "runtime" / "reports" / "conditions" / "m2" / (
        "characterization-cfe9bcab991ca9fbdff07cb2a30a1031648c97ccd9402b7b193c16b07a9e8d5a"
    )
    if not preserved.is_dir():
        pytest.skip("Preserved M2 characterization is not present in this checkout.")
    report = load_characterization_report(preserved)
    assert report["characterization_contract_revision"] == "m2-forcing-characterization-v1.1"


def test_authoritative_eccc_series_hash_excludes_normalizer_lineage_when_present() -> None:
    runtime_conditions = Path(__file__).parents[1] / "runtime" / "baked" / "conditions"
    current = runtime_conditions / (
        "condition-9d79db2c7998a15d4069e0584892d882417a54ff3dbc3bf780c83a058edfd284"
    )
    previous = runtime_conditions / (
        "condition-f5932933213d4772f2c095c2e634cc2f3df1be0dc753acca1e806c89c0a1aebe"
    )
    if not current.is_dir() or not previous.is_dir():
        pytest.skip("Authoritative generated ECCC packs are not present in this checkout.")
    expected = "56a00c1ce69d4362b134da070559e8ecd3cac6de904b996d6065266ac7f64e0b"
    assert scientific_series_sha256(load_condition_pack(current)) == expected
    assert scientific_series_sha256(load_condition_pack(previous)) == expected


def test_serving_code_has_no_characterization_or_provider_network_dependency() -> None:
    backend = Path(__file__).parents[1] / "backend" / "app"
    serving = [backend / "assess.py", backend / "baked.py", *list((backend / "api").glob("*.py"))]
    for path in serving:
        text = path.read_text(encoding="utf-8")
        assert "processing.conditions.characterization" not in text
        assert "processing.conditions.eccc" not in text
        assert "processing.conditions.pcic" not in text
    module = (backend / "processing" / "conditions" / "characterization.py").read_text(
        encoding="utf-8"
    ).casefold()
    for forbidden in ("rasterio", "pyproj", "xdem", "gdal", "pandas", "geopandas", "laspy"):
        assert forbidden not in module
