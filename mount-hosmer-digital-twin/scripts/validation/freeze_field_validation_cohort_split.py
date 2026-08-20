"""Seal an accepted field cohort, freeze its grouped split, and seal holdout targets.

This command is unavailable until a saved owner-delivery preflight and human
decision set pass every gate. It imports no prediction or metric implementation.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SRC = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SRC) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SRC))

from avycore.validation.field_workflow import (  # noqa: E402
    DeliveryVerificationReceipt,
    EligibilityDecisionRecord,
    GroupwiseSplitPreregistration,
    accept_eligible_cohort,
    freeze_groupwise_split,
    seal_holdout_observations,
)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable field-workflow output conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def freeze_from_files(
    *,
    preflight_path: Path,
    decision_paths: list[Path],
    procedure_path: Path,
    sealed_at_utc: datetime,
    observation_vault_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    preflight = _read(preflight_path)
    if preflight.get("ready_for_independent_scientific_review") is not True:
        raise ValueError("Owner-delivery preflight has not passed the complete cohort gate.")
    if preflight.get("all_files_verified") is not True:
        raise ValueError("Owner-delivery source files have not all passed hash verification.")
    if preflight.get("all_licence_records_verified") is not True:
        raise ValueError("Owner-delivery licence records have not all passed verification.")
    receipts = tuple(
        DeliveryVerificationReceipt.model_validate(item["verification_receipt"])
        for item in preflight["deliveries"]
    )
    decisions = tuple(
        EligibilityDecisionRecord.model_validate(_read(path))
        for path in decision_paths
    )
    procedure = GroupwiseSplitPreregistration.model_validate(_read(procedure_path))
    cohort = accept_eligible_cohort(decisions, receipts, sealed_at_utc=sealed_at_utc)
    split = freeze_groupwise_split(cohort, procedure)
    seal = seal_holdout_observations(
        cohort,
        split,
        observation_vault_manifest_sha256=observation_vault_manifest_sha256,
    )
    return (
        cohort.model_dump(mode="json"),
        split.model_dump(mode="json"),
        seal.model_dump(mode="json"),
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--decision", type=Path, action="append", required=True)
    parser.add_argument(
        "--procedure",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/acquisition/field-validation-group-split-preregistration-v1.json",
    )
    parser.add_argument("--sealed-at-utc", required=True)
    parser.add_argument("--observation-vault-manifest-sha256", required=True)
    parser.add_argument("--cohort-output", type=Path, required=True)
    parser.add_argument("--split-output", type=Path, required=True)
    parser.add_argument("--holdout-seal-output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    sealed_at_utc = datetime.fromisoformat(args.sealed_at_utc.replace("Z", "+00:00"))
    cohort, split, seal = freeze_from_files(
        preflight_path=args.preflight.resolve(),
        decision_paths=[path.resolve() for path in args.decision],
        procedure_path=args.procedure.resolve(),
        sealed_at_utc=sealed_at_utc,
        observation_vault_manifest_sha256=args.observation_vault_manifest_sha256,
    )
    for path, payload in (
        (args.cohort_output.resolve(), cohort),
        (args.split_output.resolve(), split),
        (args.holdout_seal_output.resolve(), seal),
    ):
        _write_immutable(path, _pretty_json(payload))
    print(
        "Accepted cohort, leakage-safe split, and calibration-time holdout seal "
        "frozen; no prediction or metric code was imported."
    )


if __name__ == "__main__":
    main()
