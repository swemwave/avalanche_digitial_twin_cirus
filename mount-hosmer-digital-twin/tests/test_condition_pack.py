"""M1 Condition Pack contract and deterministic replay acceptance tests."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.cli import validate_condition_pack_command
from app.processing.conditions.protocol import ConditionRequest, replay_provider
from app.processing.conditions.storage import (
    ConditionPackStorageError,
    load_condition_pack,
    write_condition_pack,
)
from avycore.conditions import (
    CANONICAL_UNITS,
    CONDITION_PACK_SCHEMA_VERSION,
    REQUIRED_VARIABLES,
    ConditionPack,
    ConditionPackDraft,
    build_condition_pack,
    canonical_condition_pack_bytes,
    convert_value,
)
from avycore.conditions.units import UnitConversionError


UTC = timezone.utc
SOURCE_HASH = "a" * 64
CODE_HASH = "b" * 64
MOUNTAIN_HASH = "c" * 64
GRID_HASH = "d" * 64
TIMES = (
    datetime(2026, 1, 1, 0, tzinfo=UTC),
    datetime(2026, 1, 1, 1, tzinfo=UTC),
)


def _value(
    variable: str,
    timestamp: datetime,
    value: float | str | None,
    *,
    status: str = "observed",
) -> dict:
    missing = value is None
    unit = CANONICAL_UNITS[variable]
    return {
        "station_id": "station-1",
        "time_utc": timestamp.isoformat(),
        "value": value,
        "masked": missing,
        "status": "missing" if missing else status,
        "qc_flags": [],
        "uncertainty": {
            "status": (
                "unknown" if missing or variable == "precipitation_phase" else "quantified"
            ),
            "standard_uncertainty": (
                None if missing or variable == "precipitation_phase" else 0.5
            ),
            "unit": None if missing or variable == "precipitation_phase" else unit,
            "basis": "Synthetic M1 contract fixture; not an observation.",
        },
        "staleness": {
            "status": "unknown" if missing else "fresh",
            "age_seconds": None if missing else 3600,
            "threshold_seconds": None if missing else 7200,
            "basis": "Age at the immutable pack staleness-reference time.",
        },
        "lineage": [
            {
                "source_file_sha256": SOURCE_HASH,
                "source_record_id": f"station-1:{timestamp.isoformat()}:{variable}",
                "transformation_ids": ["normalize-v1"],
            }
        ],
    }


def _draft_dict(*, missing_temperature: bool = False) -> dict:
    sample_values: dict[str, tuple[float | str | None, float | str | None]] = {
        "air_temperature": (273.15, None if missing_temperature else 274.15),
        "relative_humidity": (75.0, 76.0),
        "wind_speed": (5.0, 6.0),
        "wind_direction": (225.0, 230.0),
        "precipitation_phase": ("snow", "snow"),
        "precipitation_amount": (1.0, 2.0),
        "surface_pressure": (85000.0, 85100.0),
        "shortwave_radiation": (0.0, 10.0),
        "longwave_radiation": (250.0, 252.0),
    }
    variables = {}
    for name in REQUIRED_VARIABLES:
        provenance = {
            "kind": "direct",
            "method": f"Normalize source field for {name} without spatial inference.",
            "version": "1",
            "source_variables": [f"raw_{name}"],
            "citation": "Synthetic M1 contract fixture; not field evidence.",
            "assumptions": [],
        }
        if name == "shortwave_radiation":
            provenance = {
                "kind": "derived",
                "method": "Synthetic derivation used only to exercise the contract.",
                "version": "1",
                "source_variables": ["synthetic_radiation_component"],
                "citation": "Synthetic M1 contract fixture; not a scientific method.",
                "assumptions": ["No scientific use."],
            }
        variables[name] = {
            "variable": name,
            "unit": CANONICAL_UNITS[name],
            "provenance": provenance,
            "values": [
                _value(name, TIMES[0], sample_values[name][0]),
                _value(name, TIMES[1], sample_values[name][1]),
            ],
        }
    return {
        "schema_version": CONDITION_PACK_SCHEMA_VERSION,
        "mountain_grid": {
            "mountain_pack_id": "mount-hosmer-v1",
            "mountain_pack_sha256": MOUNTAIN_HASH,
            "grid_sha256": GRID_HASH,
            "crs": "EPSG:26911",
            "axis_order": "easting_northing",
            "horizontal_units": "metre",
            "rows": 2400,
            "columns": 2400,
            "resolution_m": 5.0,
        },
        "source": {
            "provider_id": "synthetic-provider",
            "title": "Synthetic deterministic M1 snapshot",
            "citation": "Software verification fixture only.",
            "source_uri": None,
            "licence": "Test fixture",
            "licence_uri": None,
            "permitted_use": "Software verification only.",
        },
        "times": {
            "acquisition_start_utc": "2026-01-01T00:05:00Z",
            "acquisition_end_utc": "2026-01-01T01:05:00Z",
            "publication_time_utc": "2026-01-01T01:10:00Z",
            "valid_start_utc": "2026-01-01T00:00:00Z",
            "valid_end_utc": "2026-01-01T01:00:00Z",
            "staleness_reference_time_utc": "2026-01-01T02:00:00Z",
            "cadence_seconds": 3600,
        },
        "stations": [
            {
                "station_id": "station-1",
                "name": "Synthetic station",
                "longitude_deg": -114.95,
                "latitude_deg": 49.5,
                "elevation_m": 1800.0,
                "coordinate_source": "Synthetic fixture",
                "horizontal_uncertainty_m": 1.0,
                "elevation_uncertainty_m": 1.0,
            }
        ],
        "source_files": [
            {
                "source_file_id": "snapshot-1",
                "locator": "fixture://snapshot-1",
                "sha256": SOURCE_HASH,
                "bytes": 123,
                "media_type": "application/json",
            }
        ],
        "transformations": [
            {
                "transformation_id": "normalize-v1",
                "method": "Controlled unit normalization with missing-value preservation.",
                "version": "1",
                "code_sha256": CODE_HASH,
                "parameters": {"hourly_cadence_seconds": 3600},
            }
        ],
        "normalization": {
            "software": "avycore-test-normalizer",
            "software_version": "1",
            "method": "Synthetic M1 contract normalization.",
            "code_sha256": CODE_HASH,
        },
        "variables": variables,
        "limitations": [
            "Synthetic software-verification input; not measured weather and not a forecast."
        ],
    }


def test_strict_schema_rejects_malformed_unknown_and_incomplete_input() -> None:
    malformed = _draft_dict()
    malformed["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConditionPackDraft.model_validate(malformed)

    incomplete = _draft_dict()
    del incomplete["variables"]["longwave_radiation"]
    with pytest.raises(ValidationError, match="variable set is incomplete"):
        ConditionPackDraft.model_validate(incomplete)

    undocumented = _draft_dict()
    undocumented["variables"]["shortwave_radiation"]["provenance"]["source_variables"] = []
    with pytest.raises(ValidationError, match="derived variable must name"):
        ConditionPackDraft.model_validate(undocumented)

    wrong_unit = _draft_dict()
    wrong_unit["variables"]["air_temperature"]["unit"] = "degC"
    with pytest.raises(ValidationError, match="canonical unit"):
        ConditionPackDraft.model_validate(wrong_unit)

    untraceable_fill = _draft_dict()
    filled = untraceable_fill["variables"]["air_temperature"]["values"][0]
    filled["status"] = "gap_filled"
    filled["lineage"][0]["transformation_ids"] = []
    with pytest.raises(ValidationError, match="Gap-filled values require transformation lineage"):
        ConditionPackDraft.model_validate(untraceable_fill)

    non_utc = _draft_dict()
    non_utc["times"]["valid_start_utc"] = "2026-01-01T00:00:00-07:00"
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        ConditionPackDraft.model_validate(non_utc)


def test_missing_value_remains_null_masked_and_never_becomes_zero(tmp_path: Path) -> None:
    pack = build_condition_pack(_draft_dict(missing_temperature=True))
    missing = pack.variables["air_temperature"].values[1]
    assert missing.value is None
    assert missing.masked is True
    assert missing.status == "missing"

    target = write_condition_pack(pack, tmp_path)
    raw = json.loads((target / "condition-pack.json").read_text(encoding="utf-8"))
    assert raw["variables"]["air_temperature"]["values"][1]["value"] is None
    replay = load_condition_pack(target)
    assert replay.variables["air_temperature"].values[1].value is None

    unsafe = _draft_dict()
    unsafe_value = unsafe["variables"]["air_temperature"]["values"][1]
    unsafe_value.update({"value": 0.0, "masked": True, "status": "missing"})
    with pytest.raises(ValidationError, match="Missing values must be null"):
        ConditionPackDraft.model_validate(unsafe)


def test_controlled_unit_conversion_is_explicit_and_preserves_gaps() -> None:
    assert convert_value("air_temperature", 0.0, "degC") == pytest.approx(273.15)
    assert convert_value("wind_speed", 36.0, "km h-1") == pytest.approx(10.0)
    assert convert_value("wind_direction", 360.0, "degree_true") == 0.0
    assert convert_value("relative_humidity", 0.75, "fraction") == pytest.approx(75.0)
    assert convert_value("surface_pressure", 850.0, "hPa") == pytest.approx(85000.0)
    assert convert_value("precipitation_amount", 2.0, "mm h-1") == pytest.approx(2.0)
    assert convert_value("air_temperature", None, "degC") is None
    with pytest.raises(UnitConversionError, match="Unsupported unit conversion"):
        convert_value("wind_speed", 10.0, "knots")


def test_hash_identity_detects_every_normalized_content_change() -> None:
    first = build_condition_pack(_draft_dict())
    replay = build_condition_pack(_draft_dict())
    assert first.condition_id == replay.condition_id
    assert first.normalized_output_sha256 == replay.normalized_output_sha256
    assert canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(replay)

    changed = _draft_dict()
    changed["variables"]["wind_speed"]["values"][0]["value"] = 5.001
    candidate = build_condition_pack(changed)
    assert candidate.condition_id != first.condition_id

    tampered = first.model_dump(mode="json")
    tampered["variables"]["wind_speed"]["values"][0]["value"] = 99.0
    with pytest.raises(ValidationError, match="normalized_output_sha256"):
        ConditionPack.model_validate(tampered)


def test_atomic_write_is_idempotent_and_failed_promotion_is_invisible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = build_condition_pack(_draft_dict())
    first_target = write_condition_pack(first, tmp_path)
    first_bytes = (first_target / "condition-pack.json").read_bytes()
    assert write_condition_pack(first, tmp_path) == first_target
    assert (first_target / "condition-pack.json").read_bytes() == first_bytes

    changed = _draft_dict()
    changed["variables"]["wind_speed"]["values"][0]["value"] = 7.0
    second = build_condition_pack(changed)

    from app.processing.conditions import storage

    def fail_promotion(staging: Path, target: Path) -> None:
        raise OSError("injected atomic promotion failure")

    monkeypatch.setattr(storage, "_promote_condition_directory", fail_promotion)
    with pytest.raises(OSError, match="injected atomic promotion failure"):
        write_condition_pack(second, tmp_path)

    conditions = tmp_path / "baked" / "conditions"
    assert not (conditions / second.condition_id).exists()
    assert list(conditions.glob(".condition-build-*")) == []
    assert load_condition_pack(first_target) == first


class _Provider:
    provider_id = "synthetic-provider"

    def normalize(self, request: ConditionRequest) -> ConditionPackDraft:
        draft = _draft_dict()
        assert request.valid_start_utc == TIMES[0]
        assert request.valid_end_utc == TIMES[1]
        return ConditionPackDraft.model_validate(draft)


def test_provider_replay_and_storage_are_deterministic(tmp_path: Path) -> None:
    draft = ConditionPackDraft.model_validate(_draft_dict())
    request = ConditionRequest(draft.mountain_grid, TIMES[0], TIMES[1])
    first = replay_provider(_Provider(), request)
    replay = replay_provider(_Provider(), request)
    assert first == replay
    assert first.condition_id == replay.condition_id
    assert canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(replay)
    assert write_condition_pack(first, tmp_path) == write_condition_pack(replay, tmp_path)


def test_cli_validates_complete_pack_and_rejects_malformed_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = write_condition_pack(build_condition_pack(_draft_dict()), tmp_path)
    assert validate_condition_pack_command(Namespace(path=str(target))) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is True
    assert output["condition_id"] == target.name

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"schema_version":"wrong"}', encoding="utf-8")
    assert validate_condition_pack_command(Namespace(path=str(malformed))) == 2
    assert "invalid" in capsys.readouterr().err


def test_checksum_and_directory_identity_tampering_are_rejected(tmp_path: Path) -> None:
    target = write_condition_pack(build_condition_pack(_draft_dict()), tmp_path)
    checksums = target / "checksums.json"
    raw = json.loads(checksums.read_text(encoding="utf-8"))
    raw["files"]["condition-pack.json"]["sha256"] = "0" * 64
    checksums.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConditionPackStorageError, match="checksum manifest"):
        load_condition_pack(target)
