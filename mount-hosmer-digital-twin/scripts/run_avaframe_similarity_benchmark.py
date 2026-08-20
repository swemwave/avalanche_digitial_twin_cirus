"""Run the preregistered AvaFrame 2.1 ``avaSimilaritySol`` benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "avycore" / "src"))

from app.processing.runout.avaframe import AvaFrameCom1DFAAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--avaframe-python", type=Path, required=True)
    parser.add_argument(
        "--source-inputs",
        type=Path,
        required=True,
        help=(
            "Exact OpenNHM/AvaFrame 2.1 "
            "avaframe/data/avaSimilaritySol/Inputs directory."
        ),
    )
    parser.add_argument(
        "--acceptance",
        type=Path,
        default=(
            PROJECT_ROOT
            / "validation-data"
            / "benchmarks"
            / "avaframe-2.1-avaSimilaritySol"
            / "acceptance.json"
        ),
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    args = parser.parse_args()
    run = AvaFrameCom1DFAAdapter(
        args.avaframe_python,
        timeout_seconds=args.timeout_seconds,
    ).run_similarity_benchmark(
        source_inputs=args.source_inputs,
        acceptance_path=args.acceptance,
        output_root=args.output_root,
    )
    print(
        json.dumps(
            {
                "result_id": run.result_id,
                "bundle_path": str(run.bundle_path),
                "overall_passed": run.report["overall_passed"],
                "metrics": run.report["metrics"],
                "invariants": run.report["invariants"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if run.report["overall_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
