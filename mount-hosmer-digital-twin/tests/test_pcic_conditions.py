"""M2 PCIC independent-provider acquisition, replay, and comparison tests."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.processing.conditions.eccc import mountain_grid_from_pack
from app.processing.conditions.pcic import (
    DATASET_RECORD_ID,
    EXPECTED_ARCHIVE_ENTRIES,
    LICENCE_RECORD_FILENAME,
    MANIFEST_FILENAME,
    NETWORKS_FILENAME,
    OBSERVATION_ENTRY,
    OBSERVATIONS_FILENAME,
    OGL_ATTRIBUTION,
    OGL_NAME,
    OGL_VERSION,
    ORIGINAL_SOURCE_ORGANIZATION,
    PCIC_HISTORY_ID,
    PCIC_INTERNAL_STATION_ID,
    PCIC_STATION_ID,
    PROVIDER_ID,
    SOURCE_HISTORY_FILENAME,
    SOURCE_URLS,
    STATION_FILENAME,
    STATION_LATITUDE_DEG,
    STATION_LONGITUDE_DEG,
    STATION_VARIABLES_FILENAME,
    VARIABLES_ENTRY,
    PCICCandidate,
    PCICProviderError,
    PCICStationProvider,
    acquire_pcic_snapshot,
    compare_pcic_to_eccc,
    default_pcic_candidate_audit,
    import_pcic_snapshot,
    load_pcic_snapshot,
    parse_pcic_observations,
    select_pcic_candidate,
)
from app.processing.conditions.protocol import ConditionRequest, replay_provider
from app.processing.conditions.storage import load_condition_pack, write_condition_pack
from avycore.conditions import build_condition_pack, canonical_condition_pack_bytes


UTC = timezone.utc
START = datetime(2025, 11, 1, 0, tzinfo=UTC)
END = datetime(2025, 11, 1, 2, tzinfo=UTC)
MOUNTAIN_PACK = Path(__file__).parents[1] / "backend" / "config" / "mount_hosmer.pack.json"
FIXTURE = Path(__file__).parent / "fixtures" / "conditions" / "pcic"


def _zip_bytes(observation_text: str, variable_text: str | None = None) -> bytes:
    from io import BytesIO

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            (OBSERVATION_ENTRY, observation_text),
            (
                VARIABLES_ENTRY,
                variable_text
                if variable_text is not None
                else (FIXTURE / "variables.csv").read_text(encoding="utf-8"),
            ),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 8, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content.encode("utf-8"))
    return output.getvalue()


def _contents(
    *,
    observations: str | None = None,
    variables_csv: str | None = None,
    station_histories: list[dict] | None = None,
    station_variable_unit: str = "m/s",
) -> dict[str, bytes]:
    history = {
        "elevation": None,
        "freq": None,
        "id": PCIC_HISTORY_ID,
        "lat": STATION_LATITUDE_DEG,
        "lon": STATION_LONGITUDE_DEG,
        "max_obs_time": "2026-02-21T23:00:00",
        "min_obs_time": "2021-12-14T14:00:00",
        "province": "BC",
        "station_name": "ELKFORD ROCKY MOUNTAIN SCHOOL",
        "variable_ids": [476, 477],
    }
    station = {
        "histories": station_histories if station_histories is not None else [history],
        "id": PCIC_INTERNAL_STATION_ID,
        "native_id": PCIC_STATION_ID,
        "network_uri": "/networks/9",
        "uri": f"/stations/{PCIC_INTERNAL_STATION_ID}",
    }
    station_variables = {
        "station_id": PCIC_INTERNAL_STATION_ID,
        "variables": [
            {
                "cell_method": "time: point",
                "display_name": "Wind Direction (Point)",
                "id": 476,
                "max_obs_time": "2026-02-21T23:00:00Z",
                "min_obs_time": "2021-12-14T14:00:00Z",
                "name": "WDIR_VECT",
                "network_uri": "/networks/9",
                "precision": None,
                "short_name": "wind_from_direction_point",
                "standard_name": "wind_from_direction",
                "station_id": PCIC_INTERNAL_STATION_ID,
                "tags": ["observation"],
                "unit": "degree",
                "uri": "/variables/476",
            },
            {
                "cell_method": "time: point",
                "display_name": "Wind Speed (Point)",
                "id": 477,
                "max_obs_time": "2026-02-21T23:00:00Z",
                "min_obs_time": "2024-05-02T00:00:00Z",
                "name": "WSPD_SCLR",
                "network_uri": "/networks/9",
                "precision": None,
                "short_name": "wind_speed_point",
                "standard_name": "wind_speed",
                "station_id": PCIC_INTERNAL_STATION_ID,
                "tags": ["observation"],
                "unit": station_variable_unit,
                "uri": "/variables/477",
            },
        ],
    }
    networks = [
        {
            "color": "#B03060",
            "id": 9,
            "long_name": "BC Ministry of Environment and Parks - Air Quality Network",
            "name": "ENV-AQN",
            "publish": True,
            "station_count": 138,
            "uri": "/networks/9",
            "virtual": None,
        }
    ]
    licence = {
        "success": True,
        "result": {
            "id": DATASET_RECORD_ID,
            "license_id": "2",
            "license_title": OGL_NAME,
            "license_url": "https://www2.gov.bc.ca/gov/content?id=A519A56BC2BF44E4A008B33FCF527F61",
        },
    }
    operator_evidence = b"%PDF-1.7\n" + b"synthetic operator evidence fixture\n" * 4000
    obs = observations or (FIXTURE / "observations.csv").read_text(encoding="utf-8")
    return {
        OBSERVATIONS_FILENAME: _zip_bytes(obs, variables_csv),
        STATION_FILENAME: json.dumps(station, sort_keys=True).encode(),
        STATION_VARIABLES_FILENAME: json.dumps(station_variables, sort_keys=True).encode(),
        NETWORKS_FILENAME: json.dumps(networks, sort_keys=True).encode(),
        SOURCE_HISTORY_FILENAME: operator_evidence,
        LICENCE_RECORD_FILENAME: json.dumps(licence, sort_keys=True).encode(),
    }


def _snapshot(tmp_path: Path, **content_options):
    contents = _contents(**content_options)
    sources = {}
    source_dir = tmp_path / "downloads"
    source_dir.mkdir(parents=True)
    for name, payload in contents.items():
        path = source_dir / name
        path.write_bytes(payload)
        sources[name] = path
    root = import_pcic_snapshot(
        sources,
        tmp_path / "runtime",
        acquisition_start_utc=datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
        acquisition_end_utc=datetime(2026, 8, 9, 0, 1, tzinfo=UTC),
    )
    return load_pcic_snapshot(root), contents


def _request() -> ConditionRequest:
    return ConditionRequest(mountain_grid_from_pack(MOUNTAIN_PACK), START, END)


def _provider(snapshot) -> PCICStationProvider:
    return PCICStationProvider(
        snapshot,
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2496.78,
    )


def test_synthetic_fixture_records_licence_attribution_identity_time_and_hashes() -> None:
    manifest = json.loads((FIXTURE / "fixture-manifest.json").read_text(encoding="utf-8"))
    assert manifest["synthetic"] is True
    assert "contains no PCIC" in manifest["attribution"]
    assert manifest["source_identity"].startswith("Schema emulator")
    assert datetime.fromisoformat(manifest["acquisition_utc"]).utcoffset() == timezone.utc.utcoffset(None)
    for name, expected in manifest["files"].items():
        content = (FIXTURE / name).read_bytes()
        assert len(content) == expected["bytes"]
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]


def test_deterministic_selection_and_required_network_screening() -> None:
    candidates = default_pcic_candidate_audit(
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2496.78,
    )
    selected = select_pcic_candidate(candidates)
    assert (selected.network, selected.station_id, selected.history_id) == (
        "ENV-AQN",
        "585",
        "14942",
    )
    by_name = {candidate.name: candidate for candidate in candidates}
    assert by_name["Cranbrook Muriel Baxter_60"].overlap_hours == 2576
    assert by_name["Goathaven"].eligible() is False
    assert by_name["Morrissey Ridge 2C09Q"].eligible() is False
    assert by_name["Fernie 2C21P"].history_unambiguous is False
    assert by_name["Elko"].eligible() is False
    assert by_name["Moyie Mountain"].overlap_hours == 0
    assert by_name["Relevant southeast B.C. agriculture histories"].overlap_hours == 0
    assert select_pcic_candidate(tuple(reversed(candidates))) == selected


def test_original_provider_unknown_history_and_duplicate_eccc_are_rejected() -> None:
    base = dict(
        history_id="h1",
        name="Candidate",
        history_unambiguous=True,
        redistribution_permitted=True,
        overlap_hours=100,
        comparable_variables=("wind_speed",),
        qc_revision_score=0,
        elevation_difference_m=-1000,
        horizontal_distance_km=10,
    )
    candidates = [
        PCICCandidate(
            network="ENV-AQN",
            station_id="unknown",
            original_organization=None,
            source_observation_id="x",
            **base,
        ),
        PCICCandidate(
            network="EC_raw",
            station_id="copy",
            original_organization="Environment and Climate Change Canada",
            source_observation_id="1157631",
            **base,
        ),
        PCICCandidate(
            network="OTHER",
            station_id="move",
            original_organization="Independent operator",
            source_observation_id="x",
            **{**base, "history_unambiguous": False},
        ),
    ]
    assert all(candidate.eligible() is False for candidate in candidates)
    with pytest.raises(PCICProviderError, match="No genuinely independent"):
        select_pcic_candidate(candidates)


def test_station_history_changes_are_preserved_and_ambiguous_overlap_is_rejected(
    tmp_path: Path,
) -> None:
    first = {
        "id": PCIC_HISTORY_ID,
        "lat": STATION_LATITUDE_DEG,
        "lon": STATION_LONGITUDE_DEG,
        "variable_ids": [476, 477],
    }
    moved = {
        "id": PCIC_HISTORY_ID + 1,
        "lat": STATION_LATITUDE_DEG + 0.01,
        "lon": STATION_LONGITUDE_DEG,
        "variable_ids": [476, 477],
    }
    with pytest.raises(PCICProviderError, match="histories changed or became ambiguous"):
        _snapshot(tmp_path, station_histories=[first, moved])


@pytest.mark.parametrize(
    "timestamp",
    [
        "2025-11-01 00:30:00",
        "2025-11-01T00:00:00Z",
        "2025-11-01 00:00:00-07:00",
    ],
)
def test_strict_utc_spelling_and_exact_hour_are_enforced(tmp_path: Path, timestamp: str) -> None:
    text = (
        "station_observations\nWDIR_VECT,time,WSPD_SCLR\n"
        f"350,{timestamp},2\n"
    )
    snapshot, _ = _snapshot(tmp_path, observations=text)
    with pytest.raises(PCICProviderError, match="exact.*UTC"):
        parse_pcic_observations(snapshot)


def test_units_coordinate_order_missing_masks_and_no_gap_filling(tmp_path: Path) -> None:
    text = (
        "station_observations\nWDIR_VECT,time,WSPD_SCLR\n"
        "350,2025-11-01 00:00:00,2\n"
        "10,2025-11-01 02:00:00,4\n"
    )
    snapshot, _ = _snapshot(tmp_path, observations=text)
    pack = replay_provider(_provider(snapshot), _request())
    station = pack.stations[0]
    assert (station.longitude_deg, station.latitude_deg) == (
        STATION_LONGITUDE_DEG,
        STATION_LATITUDE_DEG,
    )
    assert pack.variables["wind_speed"].unit == "m s-1"
    assert pack.variables["wind_direction"].unit == "degree_true"
    middle = pack.variables["wind_speed"].values[1]
    assert middle.value is None and middle.masked and middle.status == "missing"
    assert not any(
        value.status == "gap_filled"
        for series in pack.variables.values()
        for value in series.values
    )
    assert all(value.value is None and value.masked for value in pack.variables["air_temperature"].values)


def test_provider_units_are_not_guessed(tmp_path: Path) -> None:
    with pytest.raises(PCICProviderError, match="variable metadata changed for WSPD_SCLR"):
        _snapshot(tmp_path, station_variable_unit="km/h")


def test_qc_absence_is_explicit_and_calm_wind_direction_is_masked(tmp_path: Path) -> None:
    text = (
        "station_observations\nWDIR_VECT,time,WSPD_SCLR\n"
        "90,2025-11-01 00:00:00,0\n"
    )
    snapshot, _ = _snapshot(tmp_path, observations=text)
    provider = _provider(snapshot)
    parsed = parse_pcic_observations(snapshot)
    assert parsed.calm_direction_masks == 1
    pack = replay_provider(provider, _request())
    direction = pack.variables["wind_direction"].values[0]
    assert direction.value is None and direction.masked
    assert direction.qc_flags[0].code == "CALM"
    assert direction.qc_flags[0].severity == "rejected"
    quality = provider.quality_report(_request())
    assert quality["provider_qc"]["per_observation_fields"] == []
    assert "preliminary" in quality["revision"]["status"]


def test_exact_duplicate_is_counted_and_conflicting_revision_is_rejected(tmp_path: Path) -> None:
    exact = (
        "station_observations\nWDIR_VECT,time,WSPD_SCLR\n"
        "350,2025-11-01 00:00:00,2\n"
        "350,2025-11-01 00:00:00,2\n"
    )
    snapshot, _ = _snapshot(tmp_path / "exact", observations=exact)
    assert parse_pcic_observations(snapshot).exact_duplicate_rows == 1

    conflict = (
        "station_observations\nWDIR_VECT,time,WSPD_SCLR\n"
        "350,2025-11-01 00:00:00,2\n"
        "10,2025-11-01 00:00:00,2\n"
    )
    conflict_snapshot, _ = _snapshot(tmp_path / "conflict", observations=conflict)
    with pytest.raises(PCICProviderError, match="Conflicting duplicate/revised"):
        parse_pcic_observations(conflict_snapshot)


def test_manifest_hardening_hash_corruption_and_identity_conflict(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path / "manifest")
    manifest_path = snapshot.root / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["untracked"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PCICProviderError, match="unexpected fields"):
        load_pcic_snapshot(snapshot.root)

    snapshot, _ = _snapshot(tmp_path / "hash")
    snapshot.observations_path.write_bytes(snapshot.observations_path.read_bytes() + b"tamper")
    with pytest.raises(PCICProviderError, match="checksum mismatch"):
        load_pcic_snapshot(snapshot.root)

    snapshot, _ = _snapshot(tmp_path / "identity")
    wrong = snapshot.root.parent / ("snapshot-" + "0" * 64)
    shutil.copytree(snapshot.root, wrong)
    with pytest.raises(PCICProviderError, match="directory conflicts"):
        load_pcic_snapshot(wrong)


def test_cache_acquisition_is_content_addressed_atomic_and_replay_is_network_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contents = _contents()
    by_url = {SOURCE_URLS[name]: payload for name, payload in contents.items()}
    times = iter(
        [
            datetime(2026, 8, 9, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 9, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 9, 1, 0, tzinfo=UTC),
            datetime(2026, 8, 9, 1, 1, tzinfo=UTC),
        ]
    )

    def downloader(url: str, maximum: int) -> bytes:
        assert len(by_url[url]) <= maximum
        return by_url[url]

    first = acquire_pcic_snapshot(
        tmp_path,
        clock=lambda: next(times),
        downloader=downloader,
    )
    second = acquire_pcic_snapshot(
        tmp_path,
        clock=lambda: next(times),
        downloader=downloader,
    )
    assert first == second
    assert len(list((tmp_path / "sources" / "conditions" / "pcic").glob("snapshot-*"))) == 1
    snapshot = load_pcic_snapshot(first)

    import app.processing.conditions.pcic as pcic_module

    monkeypatch.setattr(pcic_module, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError))
    pack = replay_provider(_provider(snapshot), _request())
    assert pack.source.provider_id == PROVIDER_ID


def test_replay_and_atomic_publication_are_byte_identical(tmp_path: Path) -> None:
    snapshot, _ = _snapshot(tmp_path / "snapshot")
    provider = _provider(snapshot)
    first = replay_provider(provider, _request())
    second = replay_provider(provider, _request())
    assert canonical_condition_pack_bytes(first) == canonical_condition_pack_bytes(second)
    target = write_condition_pack(first, tmp_path / "published")
    again = write_condition_pack(second, tmp_path / "published")
    assert target == again
    assert load_condition_pack(target) == first


def _eccc_like_pack(pcic_pack):
    draft = pcic_pack.model_dump(
        mode="json", exclude={"condition_id", "normalized_output_sha256"}
    )
    draft["source"].update(
        {
            "provider_id": "eccc-historical-hourly",
            "title": "Synthetic ECCC comparison",
            "citation": "Synthetic comparison fixture",
            "source_uri": None,
            "licence": "Synthetic fixture",
            "licence_uri": None,
            "permitted_use": "Software verification",
        }
    )
    old_id = draft["stations"][0]["station_id"]
    new_id = "eccc-1157631"
    draft["stations"][0].update(
        {
            "station_id": new_id,
            "name": "SPARWOOD CS",
            "longitude_deg": -114.8839,
            "latitude_deg": 49.745,
            "elevation_m": 1136.7,
        }
    )
    for series in draft["variables"].values():
        for value in series["values"]:
            assert value["station_id"] == old_id
            value["station_id"] = new_id
    for variable, numbers in {
        "wind_direction": (10.0, 350.0, 100.0),
        "wind_speed": (3.0, 1.0, 5.0),
    }.items():
        for value, number in zip(draft["variables"][variable]["values"], numbers, strict=True):
            value.update({"value": number, "masked": False, "status": "observed", "qc_flags": []})
    return build_condition_pack(draft)


def test_comparison_bias_mae_rmse_circular_metrics_overlap_and_missing_counts(
    tmp_path: Path,
) -> None:
    snapshot, _ = _snapshot(tmp_path)
    pcic_pack = replay_provider(_provider(snapshot), _request())
    eccc_pack = _eccc_like_pack(pcic_pack)
    report = compare_pcic_to_eccc(
        pcic_pack,
        eccc_pack,
        target_longitude_deg=-115.01138889,
        target_latitude_deg=49.61361111,
        target_elevation_m=2496.78,
        eccc_original_organization="Environment and Climate Change Canada",
    )
    speed = report["variables"]["wind_speed"]
    assert speed["overlap_count"] == 3
    assert speed["eccc_minus_pcic_bias"] == pytest.approx(0.0)
    assert speed["mae"] == pytest.approx(4 / 3)
    assert speed["rmse"] == pytest.approx(math.sqrt(2))
    assert speed["pcic"]["missing_count"] == 0
    assert speed["eccc"]["missing_count"] == 0
    assert speed["pcic"]["original_organization"] == ORIGINAL_SOURCE_ORGANIZATION
    assert speed["eccc"]["station_id"] == "eccc-1157631"
    assert speed["pcic"]["source_unit"] == "m/s"
    assert speed["pcic"]["canonical_unit"] == "m s-1"
    direction = report["variables"]["wind_direction"]
    diffs = (20.0, -20.0, 10.0)
    expected_bias = math.degrees(
        math.atan2(
            sum(math.sin(math.radians(value)) for value in diffs) / 3,
            sum(math.cos(math.radians(value)) for value in diffs) / 3,
        )
    )
    assert direction["overlap_count"] == 3
    assert direction["eccc_minus_pcic_bias"] == pytest.approx(expected_bias)
    assert direction["mae"] == pytest.approx(50 / 3)
    assert direction["rmse"] == pytest.approx(math.sqrt(300))
    assert "circular-mean" in direction["metric_method"]
    assert "not validation" in report["claim_boundary"]
    assert "not transformed" in report["excluded_variables"]["precipitation_amount"]


def test_manifest_records_exact_ogl_lineage_source_identity_acquisition_and_hashes(
    tmp_path: Path,
) -> None:
    snapshot, contents = _snapshot(tmp_path)
    manifest = snapshot.manifest
    assert manifest["licence"]["name"] == OGL_NAME
    assert manifest["licence"]["version"] == OGL_VERSION
    assert manifest["licence"]["dataset_record_id"] == DATASET_RECORD_ID
    assert manifest["licence"]["attribution"] == OGL_ATTRIBUTION
    assert manifest["source_identity"]["organization"] == ORIGINAL_SOURCE_ORGANIZATION
    assert manifest["pcic_identity"]["history_ids"] == [PCIC_HISTORY_ID]
    assert datetime.fromisoformat(manifest["acquisition_start_utc"]).tzinfo is not None
    assert set(manifest["archive"]["entries"]) == EXPECTED_ARCHIVE_ENTRIES
    for name, payload in contents.items():
        record = manifest["files"][name]
        assert record["source_url"] == SOURCE_URLS[name]
        assert record["bytes"] == len(payload)
        assert record["sha256"] == hashlib.sha256(payload).hexdigest()


def test_serving_application_has_no_pcic_provider_or_network_dependency() -> None:
    backend = Path(__file__).parents[1] / "backend" / "app"
    serving_paths = [backend / "assess.py", backend / "baked.py", *list((backend / "api").glob("*.py"))]
    for path in serving_paths:
        text = path.read_text(encoding="utf-8")
        assert "processing.conditions.pcic" not in text
        assert "urllib" not in text
    pcic_source = (backend / "processing" / "conditions" / "pcic.py").read_text(encoding="utf-8")
    for forbidden in ("rasterio", "pyproj", "xdem", "gdal", "pandas", "geopandas", "laspy"):
        assert forbidden not in pcic_source.casefold()
