"""Stable labels separating software checks from physical validation evidence."""

from __future__ import annotations

from typing import Any

from .trust import TRUSTED_DATASET_IDENTITIES_SHA256

VALIDATION_CONTRACT_VERSION = "avycore-validation-dataset-v1"
SOFTWARE_BENCHMARK_VERSION = "avycore-software-benchmarks-v1"


def model_validation_status() -> dict[str, Any]:
    """Return the validation status attached to every deterministic assessment.

    This is deliberately explicit and conservative. Characterized numerical tests
    demonstrate repeatable software behaviour; they do not establish that the
    release or runout model matches avalanches on Mount Hosmer.
    """

    return {
        "field_validation": {
            "status": "unavailable",
            "eligible_observation_count": 0,
            "dataset_ids": [],
            "reason": (
                "No trusted historical Mount Hosmer release polygons, deposits, or runout "
                "endpoints are registered for comparison with the model."
            ),
        },
        "calibration": {
            "status": "not_calibrated",
            "eligible_observation_count": 0,
            "reason": (
                "Release thresholds, score weights, alpha angles, and friction parameters have "
                "not been fitted to observed Mount Hosmer avalanches."
            ),
        },
        "software_verification": {
            "status": "characterized_benchmarks",
            "benchmark_version": SOFTWARE_BENCHMARK_VERSION,
            "scope": [
                "release scoring and mask propagation",
                "release-zone extraction",
                "mask geometry construction and row/column coordinate order",
                "fast alpha-angle routing",
                "seeded particle-ensemble runout",
            ],
            "interpretation": (
                "Software verification only. These benchmarks test deterministic numerical "
                "behaviour and invariants; they are not field validation or evidence of physical "
                "accuracy."
            ),
        },
        "validation_data_contract": {
            "status": "ingestion_scaffolding",
            "schema_version": VALIDATION_CONTRACT_VERSION,
            "normalized_projected_coordinates_required": True,
            "explicit_calibration_holdout_partitions": True,
            "canonical_geometry_rasterization": True,
            "prediction_identity_required": True,
            "code_reviewed_dataset_registry_required": True,
            "trusted_dataset_count": len(TRUSTED_DATASET_IDENTITIES_SHA256),
            "end_to_end_field_validation_ready": False,
        },
    }
