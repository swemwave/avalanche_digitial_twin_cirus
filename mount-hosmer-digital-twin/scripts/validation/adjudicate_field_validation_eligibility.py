"""Create an immutable blinded field-event eligibility decision.

At least two isolated human review files are required. AI-authored or AI-assisted
reviews are rejected by schema and never count as an independent review. This
command imports no hazard, runout, assessment, prediction, or metric module.
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

from avycore.validation.acquisition import FieldValidationOwnerDelivery  # noqa: E402
from avycore.validation.field_workflow import (  # noqa: E402
    EligibilityConflictResolution,
    EligibilityReview,
    adjudicate_event,
)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable adjudication conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def adjudicate_files(
    delivery_path: Path,
    event_id: str,
    review_paths: list[Path],
    conflict_resolution_path: Path | None,
) -> dict[str, Any]:
    manifest_bytes = delivery_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    delivery = FieldValidationOwnerDelivery.model_validate(json.loads(manifest_bytes))
    if event_id not in {event.event_id for event in delivery.events}:
        raise ValueError(f"Event {event_id!r} does not occur in the owner delivery.")
    reviews = tuple(
        EligibilityReview.model_validate(_read_json(path)) for path in review_paths
    )
    for review in reviews:
        if review.event_id != event_id:
            raise ValueError(f"Review {review.review_id!r} refers to another event.")
        if review.delivery_manifest_sha256 != manifest_sha256:
            raise ValueError(
                f"Review {review.review_id!r} is not bound to the owner manifest bytes."
            )
    resolution = (
        EligibilityConflictResolution.model_validate(
            _read_json(conflict_resolution_path)
        )
        if conflict_resolution_path is not None
        else None
    )
    return adjudicate_event(reviews, resolution).model_dump(mode="json")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery", type=Path, required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--review", type=Path, action="append", required=True)
    parser.add_argument("--conflict-resolution", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = adjudicate_files(
        args.delivery.resolve(),
        args.event_id,
        [path.resolve() for path in args.review],
        args.conflict_resolution.resolve() if args.conflict_resolution else None,
    )
    _write_immutable(args.output.resolve(), _pretty_json(artifact))
    print(
        f"Eligibility decision frozen for {artifact['event_id']}: "
        f"{artifact['decision']}; predictions remain absent."
    )


if __name__ == "__main__":
    main()
