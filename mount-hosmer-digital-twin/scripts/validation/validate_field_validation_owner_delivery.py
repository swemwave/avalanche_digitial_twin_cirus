"""Validate owner-delivery manifests and original file identities before ingestion.

This command performs structural, byte-count and SHA-256 checks only. Passing it
permits independent scientific review; it does not establish eligibility, trust,
partition membership, calibration, field validation, or permission to predict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SRC = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SRC) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SRC))

from avycore.validation.acquisition import (  # noqa: E402
    FieldValidationOwnerDelivery,
    ImmutableAsset,
    owner_delivery_cohort_gate,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def write_preflight_report(path: Path, report: dict[str, Any]) -> None:
    payload = _pretty_json(report)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable preflight output conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _iter_assets(value: Any) -> Iterator[ImmutableAsset]:
    if isinstance(value, ImmutableAsset):
        yield value
    elif isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            yield from _iter_assets(getattr(value, field_name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_assets(item)


def verify_delivery_files(
    manifest_path: Path,
    delivery: FieldValidationOwnerDelivery,
) -> dict[str, Any]:
    root = manifest_path.resolve().parent
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    seen: dict[str, tuple[int, str]] = {}
    errors: list[str] = []
    checked = 0
    for asset in _iter_assets(delivery):
        checked += 1
        relative = Path(*Path(asset.relative_path).parts)
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            errors.append(f"{asset.relative_path}: path escapes delivery root")
            continue
        if not candidate.is_file():
            errors.append(f"{asset.relative_path}: file is missing")
            continue
        actual_bytes = candidate.stat().st_size
        actual_sha256 = _sha256_file(candidate)
        if actual_bytes != asset.bytes:
            errors.append(
                f"{asset.relative_path}: bytes expected={asset.bytes} actual={actual_bytes}"
            )
        if actual_sha256 != asset.sha256:
            errors.append(
                f"{asset.relative_path}: sha256 expected={asset.sha256} actual={actual_sha256}"
            )
        identity = (asset.bytes, asset.sha256)
        prior = seen.get(asset.relative_path)
        if prior is not None and prior != identity:
            errors.append(
                f"{asset.relative_path}: conflicting identities occur in one delivery manifest"
            )
        seen[asset.relative_path] = identity
    licence_record = delivery.permission_or_licence_record
    licence_record_verified = not any(
        error.startswith(f"{licence_record.relative_path}:") for error in errors
    )
    receipt: dict[str, Any] = {
        "delivery_id": delivery.delivery_id,
        "manifest_sha256": manifest_sha256,
        "manifest_bytes": len(manifest_bytes),
        "event_ids": sorted(event.event_id for event in delivery.events),
        "all_source_file_identities_verified": not errors,
        "immutable_licence_record_verified": licence_record_verified,
    }
    receipt["preflight_record_sha256"] = _sha256_value(receipt)
    return {
        "manifest": manifest_path.resolve().as_posix(),
        "delivery_id": delivery.delivery_id,
        "manifest_bytes": len(manifest_bytes),
        "manifest_sha256": manifest_sha256,
        "assets_referenced": checked,
        "unique_paths": len(seen),
        "all_files_verified": not errors,
        "immutable_licence_record_verified": licence_record_verified,
        "verification_receipt": receipt,
        "errors": errors,
    }


def validate_manifests(paths: list[Path]) -> dict[str, Any]:
    deliveries: list[FieldValidationOwnerDelivery] = []
    file_reports: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.resolve()
        payload = json.loads(resolved.read_bytes())
        delivery = FieldValidationOwnerDelivery.model_validate(payload)
        deliveries.append(delivery)
        file_reports.append(verify_delivery_files(resolved, delivery))
    cohort = owner_delivery_cohort_gate(tuple(deliveries))
    files_verified = all(item["all_files_verified"] for item in file_reports)
    return {
        "schema": "avycore-field-validation-owner-delivery-preflight-v1",
        "deliveries": file_reports,
        "all_files_verified": files_verified,
        "all_licence_records_verified": all(
            item["immutable_licence_record_verified"] for item in file_reports
        ),
        "cohort": cohort,
        "ready_for_independent_scientific_review": (
            files_verified and cohort["passed_for_independent_scientific_review"]
        ),
        "partition_assigned": False,
        "predictions_authorized": False,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", type=Path, nargs="+")
    parser.add_argument(
        "--require-complete-cohort",
        action="store_true",
        help="Return a non-zero exit status unless all files and cohort minima pass.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write the immutable preflight record used by later gates.",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    report = validate_manifests(args.manifests)
    if args.output is not None:
        write_preflight_report(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_complete_cohort and not report["ready_for_independent_scientific_review"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
