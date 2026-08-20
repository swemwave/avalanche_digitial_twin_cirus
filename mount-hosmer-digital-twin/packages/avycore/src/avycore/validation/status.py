"""Stable labels separating software checks from physical validation evidence."""

from __future__ import annotations

from typing import Any

from .trust import (
    TRUSTED_DATASET_IDENTITIES_BY_COMPONENT,
    TRUSTED_DATASET_IDENTITIES_SHA256,
)

VALIDATION_CONTRACT_VERSION = "avycore-validation-dataset-v3"
LEGACY_VALIDATION_CONTRACT_VERSIONS = ("avycore-validation-dataset-v2",)
SOFTWARE_BENCHMARK_VERSION = "avycore-software-benchmarks-v2"


def model_validation_status() -> dict[str, Any]:
    """Return the validation status attached to every deterministic assessment.

    This is deliberately explicit and conservative. Characterized numerical tests
    demonstrate repeatable software behaviour; they do not establish that the
    release or runout model matches historical avalanches in the intended domain.
    """

    return {
        "field_validation": {
            "status": "unavailable",
            "eligible_observation_count": 0,
            "dataset_ids": [],
            "reason": (
                "No code-reviewed dataset currently satisfies the strict independent holdout "
                "contract. Previously inspected remote-sensing polygons remain qualitative "
                "because their mapping independence and geometry uncertainty do not satisfy "
                "the component-specific v3 evidence profile."
            ),
        },
        "calibration": {
            "status": "not_calibrated",
            "eligible_observation_count": 0,
            "reason": (
                "Release thresholds, score weights, alpha angles, and friction parameters have "
                "not been fitted and frozen against an eligible field-observation cohort."
            ),
        },
        "component_field_validation": {
            "release": {
                "component_tested": "release",
                "evidence_profile": "R",
                "status": "unavailable",
                "eligible_observation_count": 0,
                "trusted_dataset_count": len(
                    TRUSTED_DATASET_IDENTITIES_BY_COMPONENT["release"]
                ),
                "reason": (
                    "No complete code-reviewed Profile R holdout is registered. Positive/"
                    "unlabelled observations cannot expose negative-dependent metrics."
                ),
            },
            "conditional_runout": {
                "component_tested": "conditional_runout",
                "evidence_profile": "C",
                "status": "unavailable",
                "eligible_observation_count": 0,
                "trusted_dataset_count": len(
                    TRUSTED_DATASET_IDENTITIES_BY_COMPONENT["conditional_runout"]
                ),
                "reason": (
                    "No complete code-reviewed Profile C holdout with independent release state "
                    "and observed dense-flow runout is registered."
                ),
            },
            "end_to_end": {
                "component_tested": "end_to_end",
                "evidence_profile": "E",
                "status": "unavailable",
                "eligible_observation_count": 0,
                "trusted_dataset_count": len(
                    TRUSTED_DATASET_IDENTITIES_BY_COMPONENT["end_to_end"]
                ),
                "reason": (
                    "No complete code-reviewed Profile E holdout is registered; component "
                    "evidence does not transfer automatically to the end-to-end chain."
                ),
            },
        },
        "software_verification": {
            "status": "characterized_benchmarks",
            "benchmark_version": SOFTWARE_BENCHMARK_VERSION,
            "scope": [
                "release scoring and mask propagation",
                "release-zone extraction",
                "mask geometry construction and row/column coordinate order",
                "fast alpha-angle routing",
                "Coulomb stopping and Voellmy terminal velocity on analytic terrain",
                "directional surface-to-grid coordinate projection",
                "energy monotonicity on one synthetic centreline and first-order grid "
                "convergence on one ramp-to-flat fixture",
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
            "legacy_read_versions": list(LEGACY_VALIDATION_CONTRACT_VERSIONS),
            "normalized_projected_coordinates_required": True,
            "explicit_calibration_holdout_partitions": True,
            "canonical_geometry_rasterization": True,
            "prediction_identity_required": True,
            "code_reviewed_dataset_registry_required": True,
            "trusted_dataset_count": len(TRUSTED_DATASET_IDENTITIES_SHA256),
            "trusted_dataset_count_by_component": {
                component: len(identities)
                for component, identities in TRUSTED_DATASET_IDENTITIES_BY_COMPONENT.items()
            },
            "component_specific_evidence_profiles": True,
            "positive_unlabelled_state_is_explicit": True,
            "end_to_end_field_validation_ready": False,
        },
    }
