"""Run the analytical Flow-Py energy-line verification case offline.

The case executes AvaFrame's com4FlowPy port of Flow-Py inside an explicitly
supplied, isolated Python environment and grades it against the closed-form
energy-line solution.  Acceptance thresholds are read from a preregistered,
self-hashed document, so they cannot be relaxed after a result has been seen.

    python -m venv .venv-avaframe
    .venv-avaframe\\Scripts\\python -m pip install -r backend\\requirements-avaframe.txt
    python scripts\\run_flowpy_energy_line_benchmark.py ^
      --avaframe-python .venv-avaframe\\Scripts\\python.exe ^
      --output-root <new-output-directory>

This is offline software verification.  It is not calibration, not field
validation, and not evidence of accuracy at any real site.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACCEPTANCE = (
    PROJECT_ROOT / "validation-data" / "benchmarks" / "flowpy-energy-line" / "acceptance.json"
)

for candidate in (PROJECT_ROOT / "backend", PROJECT_ROOT / "packages" / "avycore" / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--avaframe-python",
        required=True,
        type=Path,
        help="Python executable of the isolated AvaFrame 2.1 environment.",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        type=Path,
        help="Directory that receives the content-addressed benchmark bundle.",
    )
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_ACCEPTANCE)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    arguments = parser.parse_args()

    from app.processing.runout.flowpy_benchmark import run_energy_line_benchmark

    run = run_energy_line_benchmark(
        avaframe_python=arguments.avaframe_python,
        acceptance_path=arguments.acceptance,
        output_root=arguments.output_root,
        timeout_seconds=arguments.timeout_seconds,
    )
    print(json.dumps(run.report, indent=2, sort_keys=True))
    print(f"\nbundle: {run.bundle_path}")
    return 0 if run.report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
