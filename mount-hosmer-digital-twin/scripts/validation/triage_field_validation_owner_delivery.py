"""Triage a partial or malformed owner delivery and report what is still needed.

The owner request tells data owners to leave unavailable observations missing
rather than infer or substitute them, so an honest partial delivery is rejected
by the strict pre-ingestion contract as a single opaque error dump. This command
runs that same strict contract and then attributes every rejection to an event,
an evidence profile, a schema path, and an existing exclusion reason, so the
owner can be told exactly which events need what.

Triage is advisory. It establishes no eligibility, trust, partition membership,
calibration, field validation, or permission to predict, and a supported profile
here is not an accepted component. Eligibility still requires the complete
strict contract, two independent blinded human reviews, adjudication of every
event including exclusions, a frozen grouped split, and a sealed holdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SRC = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SRC) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SRC))

from avycore.validation.intake_triage import (  # noqa: E402
    PROFILE_E_EVIDENCE_OUTSIDE_THIS_SCHEMA,
    CohortTriage,
    triage_owner_delivery_cohort,
)


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def write_immutable(path: Path, payload: bytes) -> None:
    """Write once. A differing rerun is a conflict, never a silent overwrite."""

    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable triage output conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def triage_manifests(paths: list[Path]) -> CohortTriage:
    payloads = [json.loads(path.resolve().read_bytes()) for path in paths]
    return triage_owner_delivery_cohort(payloads)


def render_client_request(report: CohortTriage) -> bytes:
    """Render the deterministic per-event list to send back to the data owner."""

    lines: list[str] = [
        "# Field-validation delivery triage",
        "",
        report.claim_boundary,
        "",
        (
            "Please do not fill any gap below by inferring, substituting, "
            "back-calculating, or model-deriving a value. An event we exclude for "
            "missing evidence is a usable result; an event completed with a "
            "substituted value is not."
        ),
        "",
        (
            "Profile E (end-to-end release plus runout) is listed as unsupported "
            "for every event below, and that is not a gap in your data. This "
            "delivery schema has no field for the remaining Profile E evidence, "
            "so no delivery can establish it. That evidence is:"
        ),
        "",
    ]
    # Delivery-wide, so it is stated once rather than repeated under every event.
    for extra in PROFILE_E_EVIDENCE_OUTSIDE_THIS_SCHEMA:
        lines.append(f"- {extra}")
    lines.append("")

    for delivery in report.deliveries:
        name = delivery.delivery_id or "(delivery_id missing)"
        lines.append(f"## Delivery `{name}`")
        lines.append("")
        lines.append(
            "Strict pre-ingestion contract: "
            + ("accepted for independent review." if delivery.parsed_under_strict_contract
               else "rejected; the items below are why.")
        )
        lines.append("")

        if delivery.delivery_findings:
            lines.append("Delivery-level items:")
            lines.append("")
            for finding in delivery.delivery_findings:
                lines.append(
                    f"- `{finding.schema_path}` — {finding.detail} "
                    f"[{finding.problem}; {finding.exclusion_reason}]"
                )
            lines.append("")

        for event in delivery.events:
            label = event.event_id or "(event_id missing)"
            lines.append(f"### Event `{label}` (`{event.schema_path}`)")
            lines.append("")
            supported = ", ".join(event.supported_profiles) or "none"
            lines.append(f"Evidence profiles this event could support: {supported}.")
            lines.append("")
            if event.findings:
                lines.append("Still needed:")
                lines.append("")
                for finding in event.findings:
                    lines.append(
                        f"- `{finding.schema_path}` — {finding.detail} "
                        f"[{finding.evidence_block}; {finding.exclusion_reason}]"
                    )
            else:
                lines.append(
                    "No missing or unusable field was found in this event."
                )
            lines.append("")
            for profile in event.profiles:
                if profile.supported or not profile.unusable_evidence_blocks:
                    # A profile blocked only by out-of-schema evidence is covered
                    # by the delivery-wide note above.
                    continue
                blocks = ", ".join(profile.unusable_evidence_blocks)
                lines.append(
                    f"- Profile {profile.profile} not supported. Unusable evidence "
                    f"blocks: {blocks}."
                )
            lines.append("")

    lines.append("## Cohort totals")
    lines.append("")
    for rollup in report.profile_rollups:
        observed = ", ".join(
            f"{key} {rollup.observed[key]}/{rollup.required[key]}"
            for key in sorted(rollup.required)
        )
        lines.append(
            f"- Profile {rollup.profile}: {observed} — "
            + ("reaches the review minimum" if rollup.reaches_cohort_minimum
               else "below the review minimum")
            + "."
        )
    lines.append("")
    lines.append(
        "Reaching a minimum means only that enough events carry the shape of the "
        "evidence to be worth reviewing. It assigns no eligibility, no partition, "
        "and no permission to predict."
    )
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", type=Path, nargs="+")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write the triage report as an immutable JSON artifact.",
    )
    parser.add_argument(
        "--client-request",
        type=Path,
        help="Optionally write the per-event Markdown list to return to the owner.",
    )
    parser.add_argument(
        "--require-cohort-minimum",
        choices=("R", "C", "E"),
        help=(
            "Return a non-zero exit status unless the named evidence profile "
            "reaches the cohort minimum. This gates review, never prediction."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    report = triage_manifests(args.manifests)
    payload = report.model_dump(mode="json")
    if args.output is not None:
        write_immutable(args.output.resolve(), _pretty_json(payload))
    if args.client_request is not None:
        write_immutable(args.client_request.resolve(), render_client_request(report))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.require_cohort_minimum is not None:
        rollup = next(
            item
            for item in report.profile_rollups
            if item.profile == args.require_cohort_minimum
        )
        if not rollup.reaches_cohort_minimum:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
