"""Build the no-event split preregistration and workflow schemas.

The builder refuses to expose or assign real events. It binds the deterministic
group-wise algorithm and seed to the unchanged frozen protocol, source audit,
strict public-funnel stop, and implementation before any prediction exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SRC = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SRC) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SRC))

from avycore.validation.field_workflow import (  # noqa: E402
    ELIGIBILITY_DECISION_SCHEMA_VERSION,
    ELIGIBILITY_REVIEW_SCHEMA_VERSION,
    GROUP_SPLIT_ALGORITHM,
    GROUP_SPLIT_SCHEMA_VERSION,
    GROUP_SPLIT_SEED,
    EligibilityConflictResolution,
    EligibilityDecisionRecord,
    EligibilityReview,
    GroupwiseSplitPreregistration,
)


FROZEN_AT_UTC = "2026-08-15T04:20:00Z"
PROTOCOL_PATH = Path("validation-data/experiments/public-data-field-validation-v2.json")
SOURCE_AUDIT_PATH = Path(
    "validation-data/candidates/public-validation-source-audit-v2.json"
)
STRICT_FUNNEL_PATH = Path(
    "validation-data/candidates/public-event-strict-funnel-v5.json"
)
IMPLEMENTATION_PATH = Path(
    "packages/avycore/src/avycore/validation/field_workflow.py"
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(path: Path, payload: bytes | None = None) -> dict[str, Any]:
    content = payload if payload is not None else (REPOSITORY_ROOT / path).read_bytes()
    return {"path": path.as_posix(), "bytes": len(content), "sha256": _sha256_bytes(content)}


def build_preregistration() -> dict[str, Any]:
    protocol = json.loads((REPOSITORY_ROOT / PROTOCOL_PATH).read_bytes())
    source_audit = json.loads((REPOSITORY_ROOT / SOURCE_AUDIT_PATH).read_bytes())
    strict_funnel = json.loads((REPOSITORY_ROOT / STRICT_FUNNEL_PATH).read_bytes())
    if protocol.get("schema") != "avycore-public-data-field-validation-experiment-v2":
        raise ValueError("Split preregistration requires frozen field protocol v2.")
    if protocol["evaluation_scope"]["split_seed"] != GROUP_SPLIT_SEED:
        raise ValueError("Implementation seed differs from the frozen protocol seed.")
    if protocol.get("predictions_generated") is not False:
        raise ValueError("Refusing to preregister a split after predictions exist.")
    if source_audit.get("predictions_generated") is not False:
        raise ValueError("Source audit is not prediction-blind.")
    if strict_funnel.get("predictions_generated") is not False:
        raise ValueError("Strict public funnel is not prediction-blind.")

    protocol_identity = _identity(PROTOCOL_PATH)
    source_audit_identity = _identity(SOURCE_AUDIT_PATH)
    implementation_identity = _identity(IMPLEMENTATION_PATH)
    seed_binding = _sha256_bytes(
        _canonical_json(
            {
                "algorithm": GROUP_SPLIT_ALGORITHM,
                "seed": GROUP_SPLIT_SEED,
                "protocol_sha256": protocol_identity["sha256"],
                "source_audit_sha256": source_audit_identity["sha256"],
                "implementation_sha256": implementation_identity["sha256"],
            }
        )
    )
    artifact: dict[str, Any] = {
        "schema_version": GROUP_SPLIT_SCHEMA_VERSION,
        "status": "frozen_before_predictions_no_real_event_assignments",
        "frozen_at_utc": FROZEN_AT_UTC,
        "protocol": protocol_identity,
        "source_audit": source_audit_identity,
        "strict_public_funnel": _identity(STRICT_FUNNEL_PATH),
        "implementation": implementation_identity,
        "algorithm": GROUP_SPLIT_ALGORITHM,
        "seed": GROUP_SPLIT_SEED,
        "seed_binding_sha256": seed_binding,
        "grouping_keys": ["path_id", "mountain_id", "storm_cycle_id"],
        "target_holdout_fraction": 0.5,
        "minimum_calibration_events": 6,
        "minimum_holdout_events": 6,
        "minimum_paths_per_partition": 3,
        "minimum_mountains_per_partition": 1,
        "minimum_storm_cycles_per_partition": 1,
        "optimization_order": [
            "minimize_absolute_event_count_distance_from_half",
            "maximize_smaller_partition_independent_path_count",
            "maximize_smaller_partition_storm_cycle_count",
            "maximize_smaller_partition_mountain_count",
            "break_remaining_ties_by_sha256_of_seed_binding_and_holdout_components",
        ],
        "group_leakage_permitted": False,
        "event_assignments": [],
        "real_event_ids_exposed": False,
        "predictions_generated": False,
        "holdout_targets_accessed": False,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(
        _canonical_json(artifact)
    )
    return GroupwiseSplitPreregistration.model_validate(artifact).model_dump(mode="json")


def build_integrity_manifest(generated: dict[Path, bytes]) -> dict[str, Any]:
    static_paths = (
        PROTOCOL_PATH,
        SOURCE_AUDIT_PATH,
        STRICT_FUNNEL_PATH,
        Path("validation-data/acquisition/field-validation-owner-request-v1.json"),
        Path("validation-data/acquisition/field-validation-owner-delivery-v1.schema.json"),
        Path("validation-data/acquisition/field-validation-owner-requests-v1.md"),
    )
    implementation_paths = (
        Path("packages/avycore/src/avycore/validation/acquisition.py"),
        IMPLEMENTATION_PATH,
        Path("scripts/validation/build_field_validation_owner_request.py"),
        Path("scripts/validation/validate_field_validation_owner_delivery.py"),
        Path("scripts/validation/adjudicate_field_validation_eligibility.py"),
        Path("scripts/validation/build_field_validation_workflow_preregistration.py"),
        Path("scripts/validation/freeze_field_validation_cohort_split.py"),
    )
    artifact: dict[str, Any] = {
        "schema": "avycore-field-validation-acquisition-integrity-v1",
        "recalculated_at_utc": FROZEN_AT_UTC,
        "status": "zero_eligible_events_prediction_and_metrics_blocked",
        "frozen_source_artifacts": [_identity(path) for path in static_paths[:3]],
        "derived_acquisition_artifacts": [
            _identity(path) for path in static_paths[3:]
        ]
        + [_identity(path, payload) for path, payload in sorted(generated.items())],
        "implementation_artifacts": [_identity(path) for path in implementation_paths],
        "current_counts": {
            "eligible_events": 0,
            "eligible_paths": 0,
            "eligible_mountains": 0,
            "eligible_storm_cycles": 0,
        },
        "prediction_authorized": False,
        "metrics_authorized": False,
        "claim_boundary": (
            "Hashes establish deterministic artifact identity only. They do not make "
            "an event eligible or establish field accuracy."
        ),
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(
        _canonical_json(artifact)
    )
    return artifact


def _arguments() -> argparse.Namespace:
    base = REPOSITORY_ROOT / "validation-data" / "acquisition"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--split-output",
        type=Path,
        default=base / "field-validation-group-split-preregistration-v1.json",
    )
    parser.add_argument(
        "--review-schema-output",
        type=Path,
        default=base / "field-validation-eligibility-review-v1.schema.json",
    )
    parser.add_argument(
        "--conflict-schema-output",
        type=Path,
        default=base / "field-validation-eligibility-conflict-v1.schema.json",
    )
    parser.add_argument(
        "--decision-schema-output",
        type=Path,
        default=base / "field-validation-eligibility-decision-v1.schema.json",
    )
    parser.add_argument(
        "--integrity-output",
        type=Path,
        default=base / "field-validation-acquisition-integrity-v1.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    canonical_payloads = {
        Path("validation-data/acquisition/field-validation-group-split-preregistration-v1.json"): _pretty_json(build_preregistration()),
        Path("validation-data/acquisition/field-validation-eligibility-review-v1.schema.json"): _pretty_json(EligibilityReview.model_json_schema()),
        Path("validation-data/acquisition/field-validation-eligibility-conflict-v1.schema.json"): _pretty_json(EligibilityConflictResolution.model_json_schema()),
        Path("validation-data/acquisition/field-validation-eligibility-decision-v1.schema.json"): _pretty_json(EligibilityDecisionRecord.model_json_schema()),
    }
    output_payloads = dict(
        zip(
            (
                args.split_output.resolve(),
                args.review_schema_output.resolve(),
                args.conflict_schema_output.resolve(),
                args.decision_schema_output.resolve(),
            ),
            canonical_payloads.values(),
            strict=True,
        )
    )
    for path, payload in output_payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    integrity = build_integrity_manifest(canonical_payloads)
    args.integrity_output.parent.mkdir(parents=True, exist_ok=True)
    args.integrity_output.write_bytes(_pretty_json(integrity))
    print(
        "Frozen group-wise split procedure and human-review schemas with zero event "
        "assignments; predictions and metrics remain blocked."
    )


if __name__ == "__main__":
    main()
