"""Run AvaFrame com1DFA and Flow-Py on one synthetic release and compare them.

Both engines receive the same normalized release and the same terrain: com1DFA
gets the release polygons, com4FlowPy gets the release raster, and neither
consumes the other's output.  The comparison reports where the two models
disagree; it is not evidence that either is correct, and the synthetic terrain
and parameters are not site observations.

    python -m venv .venv-avaframe
    .venv-avaframe\\Scripts\\python -m pip install -r backend\\requirements-avaframe.txt
    python scripts\\run_synthetic_engine_comparison.py ^
      --avaframe-python .venv-avaframe\\Scripts\\python.exe ^
      --output-root <new-output-directory>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (PROJECT_ROOT / "backend", PROJECT_ROOT / "packages" / "avycore" / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avaframe-python", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--alpha-degrees", type=float, default=25.0)
    parser.add_argument("--flowpy-exponent", type=float, default=8.0)
    parser.add_argument("--flux-threshold", type=float, default=0.0003)
    parser.add_argument("--max-energy-line-height-m", type=float, default=270.0)
    parser.add_argument("--simulation-time-s", type=float, default=40.0)
    parser.add_argument("--seed", type=int, default=12345)
    arguments = parser.parse_args()

    from app.processing.runout.synthetic import run_synthetic_engine_comparison

    run = run_synthetic_engine_comparison(
        avaframe_python=arguments.avaframe_python,
        output_root=arguments.output_root,
        alpha_degrees=arguments.alpha_degrees,
        flowpy_exponent=arguments.flowpy_exponent,
        flux_threshold=arguments.flux_threshold,
        max_energy_line_height_m=arguments.max_energy_line_height_m,
        simulation_time_s=arguments.simulation_time_s,
        seed=arguments.seed,
    )
    summary = {
        "release_result_id": run.release.result_id,
        "release_area_m2": run.release.release_area_m2,
        "engines": {
            run.com1dfa.provenance.engine_id: {
                "result_id": run.com1dfa.result_id,
                "runout_area_m2": run.com1dfa.runout_area_m2,
                "aoi_status": run.com1dfa.aoi_status,
                "unsupported_outputs": [
                    item.quantity.value for item in run.com1dfa.unsupported_outputs
                ],
            },
            run.flowpy.provenance.engine_id: {
                "result_id": run.flowpy.result_id,
                "runout_area_m2": run.flowpy.runout_area_m2,
                "aoi_status": run.flowpy.aoi_status,
                "unsupported_outputs": [
                    item.quantity.value for item in run.flowpy.unsupported_outputs
                ],
            },
        },
        "comparison_id": run.comparison.comparison_id,
        "metrics": [
            {
                "name": metric.name,
                "status": metric.status,
                "unit": metric.unit,
                "value": metric.value,
            }
            for metric in run.comparison.metrics
        ],
        "warnings": list(run.comparison.warnings),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nrelease bundle:    {run.release_bundle}")
    print(f"com1DFA bundle:    {run.com1dfa_bundle}")
    print(f"Flow-Py bundle:    {run.flowpy_bundle}")
    print(f"comparison bundle: {run.comparison_bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
