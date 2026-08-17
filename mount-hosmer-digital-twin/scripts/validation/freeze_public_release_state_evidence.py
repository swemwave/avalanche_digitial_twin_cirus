"""Freeze crown-height semantics and a literature-transferred density prior.

This artifact is created without importing model code or reading any evaluated
runout result.  Unit conversion of the provider's centimetre field is retained,
but it is not promoted to normal-to-slope slab thickness because the provider
does not specify that geometry or a measurement uncertainty.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-release-state-evidence-v1"
FROZEN_AT_UTC = "2026-08-13T00:00:00Z"


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


def crown_height_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    value_cm = candidate["event"].get("fracture_height_cm")
    value_m = value_cm / 100.0 if isinstance(value_cm, (int, float)) else None
    return {
        "candidate_id": candidate["candidate_id"],
        "provider_field": "AvalancheObs.FractureHeight",
        "provider_value_cm": value_cm,
        "provider_value_m_unit_conversion_only": value_m,
        "provider_semantics_source": {
            "publisher": "Norwegian Water Resources and Energy Directorate (NVE), Varsom",
            "url": "https://www.varsom.no/snoskred/snoskredskolen/del-informasjon/ulykke-nestenulykke/",
            "norwegian_label": "Bruddhøyde: hvor høy var løsnekanten?",
            "english_interpretation": "Fracture height: how high was the crown edge?",
        },
        "semantics_verified_as_crown_edge_height": True,
        "semantics_verified_as_normal_to_slope_slab_thickness": False,
        "measurement_direction_supplied": False,
        "measurement_method_supplied": False,
        "measurement_uncertainty_supplied": False,
        "conversion_to_release_thickness_permitted": False,
        "validation_contract_v3_release_thickness_evidence_eligible": False,
        "exclusion_reason": (
            "The public field describes crown-edge height, but does not establish a "
            "normal-to-slope measurement direction, method, or uncertainty."
        ),
        "source_record_sha256": candidate["source_record_file_sha256"],
    }


def build_release_state_evidence(evidence_path: Path) -> dict[str, Any]:
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    if evidence.get("schema") != "avycore-public-regobs-blinded-evidence-v1":
        raise ValueError("Unexpected RegObs evidence schema.")
    candidates = [crown_height_evidence(item) for item in evidence["candidates"]]
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "frozen_at_utc": FROZEN_AT_UTC,
        "stage": "release_state_evidence_freeze_before_any_runout_evaluation",
        "source_regobs_evidence_manifest_sha256": _sha256_bytes(evidence_bytes),
        "model_code_imported": False,
        "runout_results_accessed": False,
        "predictions_generated": False,
        "release_density_evidence": {
            "representation": "distribution",
            "distribution_family": "continuous_uniform",
            "lower": 200.0,
            "upper": 250.0,
            "unit": "kg m-3",
            "role": "transferred_literature_prior_not_event_measurement",
            "source": {
                "authors": "C. Stethem and R. Perla",
                "title": "Snow-Slab Studies at Whistler Mountain, British Columbia, Canada",
                "journal": "Journal of Glaciology 26(94), 85-91 (1980)",
                "doi": "10.3189/S0022143000010613",
                "url": "https://www.cambridge.org/core/journals/journal-of-glaciology/article/snowslab-studies-at-whistler-mountain-british-columbia-canada/8D0B62C03C0C8873DC2F9FDCE9D11F50",
                "source_population": "30 dry slab avalanche fracture-zone investigations at Whistler Mountain",
                "reported_slab_density_mean_kg_m3": 220.0,
                "source_supported_density_band_kg_m3": [200.0, 250.0],
                "measurement_method": (
                    "Cylindrical samples cut at the crown across the entire slab layer and "
                    "weighed in situ."
                ),
            },
            "distribution_choice": (
                "Equal density over the paper's 200-250 kg m-3 band is a transparent project "
                "choice; the paper does not report this as an empirical uniform distribution."
            ),
            "applicability": "dry slab release snow only",
            "transfer_limitations": [
                "Whistler measurements are not event-specific measurements for Norway or Svalbard.",
                "The source sample is biased toward older, deeper dry slabs.",
                "No quantitative geographic or event-regime transfer error is available.",
                "The prior cannot compensate for missing release-thickness evidence.",
            ],
            "selected_from_runout_performance": False,
            "event_specific_measurement": False,
            "frozen_for_any_later_development_or_holdout_use": True,
        },
        "crown_height_semantics": candidates,
        "counts": {
            "candidates": len(candidates),
            "reported_crown_heights": sum(item["provider_value_cm"] is not None for item in candidates),
            "normal_to_slope_thickness_semantics_verified": 0,
            "measurement_uncertainties_supplied": 0,
            "release_thickness_evidence_eligible": 0,
        },
        "claim_boundary": (
            "A frozen transferred density prior is not an event density measurement and does "
            "not make any candidate eligible without defensible release-thickness evidence."
        ),
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Scores are relative indices, not probabilities."
        ),
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "candidates" / "public-regobs-blinded-evidence-v1.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / "validation-data" / "experiments" / "public-release-state-evidence-v1.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_release_state_evidence(args.evidence.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = _pretty_json(artifact)
    if args.output.exists() and args.output.read_bytes() != payload:
        raise ValueError(f"Frozen release-state evidence conflict at {args.output}.")
    if not args.output.exists():
        with args.output.open("xb") as stream:
            stream.write(payload)
    print(
        f"Froze density prior and {artifact['counts']['candidates']} crown records; "
        "eligible thickness evidence=0."
    )


if __name__ == "__main__":
    main()
