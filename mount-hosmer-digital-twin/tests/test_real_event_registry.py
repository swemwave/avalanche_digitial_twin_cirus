"""Integrity checks for registered real-event evidence.

These datasets are qualitative remote-sensing interpretations. Loading them proves
that their geometry and lineage satisfy the ingestion contract; it is not a field-
validation result and must not be presented as one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from shapely import to_wkb
from shapely.geometry import shape

from avycore.validation import load_validation_dataset
from avycore.validation.trust import TRUSTED_DATASET_IDENTITIES_SHA256


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_DATA = REPO_ROOT / "validation-data"


def _load(dataset_directory: str):
    return load_validation_dataset(VALIDATION_DATA / dataset_directory / "manifest.json")


def test_braemabuehl_registry_loads_as_qualitative_positive_only_evidence() -> None:
    dataset = _load("braemabuehl-2019-qualitative")

    assert dataset.manifest.dataset_id == "braemabuehl-2019-qualitative-v1"
    assert dataset.manifest.crs == "EPSG:2056"
    assert dataset.manifest.evidence_type == "remote_sensing_interpretation"
    assert dataset.manifest.scientific_use == "qualitative_comparison"
    assert dataset.manifest.coverage_semantics == "positive_observations_only"
    assert dataset.manifest.positional_uncertainty.status == "unknown"
    assert dataset.partition_counts == {"qualitative": 4}
    assert {item.event_id for item in dataset.observations} == {
        "braemabuehl-2019-wildi",
        "braemabuehl-2019-ruechi",
    }
    assert {item.observation_type for item in dataset.observations} == {
        "release_polygon",
        "deposit_polygon",
    }
    assert all(item.properties["event_date_status"] == "known" for item in dataset.observations)
    assert all(
        item.properties["scenario_inputs"]["new_snow_cm"] == 60.0
        for item in dataset.observations
    )
    assert dataset.dataset_identity_sha256 not in TRUSTED_DATASET_IDENTITIES_SHA256


def test_spot_registry_preserves_six_whole_footprints_without_deposit_relabelling() -> None:
    dataset = _load("davos-spot-2019-qualitative")

    assert dataset.manifest.dataset_id == "davos-spot-2019-qualitative-v1"
    assert dataset.manifest.crs == "EPSG:2056"
    assert dataset.manifest.evidence_type == "remote_sensing_interpretation"
    assert dataset.manifest.scientific_use == "qualitative_comparison"
    assert dataset.manifest.coverage_semantics == "positive_observations_only"
    assert dataset.manifest.positional_uncertainty.status == "unknown"
    assert dataset.partition_counts == {"qualitative": 6}
    assert len({item.event_id for item in dataset.observations}) == 6
    assert {item.observation_type for item in dataset.observations} == {
        "avalanche_footprint"
    }
    assert {
        item.properties["source_feature_id"] for item in dataset.observations
    } == {
        f"SPOT_2019_perimeter:OBJECTID={object_id}"
        for object_id in (732, 754, 820, 837, 840, 1034)
    }
    assert all(item.properties["event_date_status"] == "bounded" for item in dataset.observations)
    assert all(item.properties["source_outline_quality"] == 1 for item in dataset.observations)
    assert all(item.properties["source_avalanche_type"] == "SLAB" for item in dataset.observations)
    assert dataset.dataset_identity_sha256 not in TRUSTED_DATASET_IDENTITIES_SHA256


def test_registered_geojson_is_valid_2d_geometry_with_source_vertex_counts() -> None:
    braemabuehl = _load("braemabuehl-2019-qualitative")
    spot = _load("davos-spot-2019-qualitative")

    assert [item.properties["source_coordinate_count"] for item in braemabuehl.observations] == [
        33,
        292,
        59,
        469,
    ]
    assert [item.properties["source_interior_ring_count"] for item in braemabuehl.observations] == [
        0,
        1,
        0,
        0,
    ]
    assert [item.properties["source_coordinate_count"] for item in spot.observations] == [
        549,
        892,
        439,
        239,
        243,
        318,
    ]
    registered = (*braemabuehl.observations, *spot.observations)
    assert all(not shape(item.geometry).has_z for item in registered)
    assert all(
        hashlib.sha256(
            to_wkb(shape(item.geometry), byte_order=1, include_srid=False)
        ).hexdigest()
        == item.properties["source_geometry_wkb_sha256"]
        for item in registered
    )


def test_braemabuehl_source_collection_digest_is_reproducibly_defined() -> None:
    source_index_path = (
        VALIDATION_DATA / "braemabuehl-2019-qualitative" / "source-files.json"
    )
    source_index = json.loads(source_index_path.read_text(encoding="utf-8"))

    canonical_files = json.dumps(
        source_index["files"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert hashlib.sha256(canonical_files).hexdigest() == source_index["source_collection_sha256"]
    assert source_index["source_collection_sha256"] == (
        _load("braemabuehl-2019-qualitative").manifest.original_source_sha256
    )
