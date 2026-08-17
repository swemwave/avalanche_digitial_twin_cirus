"""Run the offline synthetic PRA-style release -> AvaFrame example."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT / "packages" / "avycore" / "src"))

from app.processing.runout.synthetic import run_synthetic_example


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--avaframe-python", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--release-thickness-m", type=float, default=0.8)
    parser.add_argument("--release-density-kg-m3", type=float, default=200.0)
    parser.add_argument("--voellmy-mu", type=float, default=0.155)
    parser.add_argument("--voellmy-xi-m-s2", type=float, default=4000.0)
    parser.add_argument("--simulation-time-s", type=float, default=40.0)
    parser.add_argument("--time-step-s", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args()
    result = run_synthetic_example(
        avaframe_python=args.avaframe_python,
        output_root=args.output_root,
        release_thickness_m=args.release_thickness_m,
        release_density_kg_m3=args.release_density_kg_m3,
        voellmy_mu=args.voellmy_mu,
        voellmy_xi_m_s2=args.voellmy_xi_m_s2,
        simulation_time_s=args.simulation_time_s,
        time_step_s=args.time_step_s,
        seed=args.seed,
    )
    print(
        json.dumps(
            {
                "release_result_id": result.release.result_id,
                "runout_result_id": result.runout.result_id,
                "release_area_m2": result.release.release_area_m2,
                "runout_area_m2": result.runout.runout_area_m2,
                "aoi_status": result.runout.aoi_status,
                "release_bundle": str(result.release_bundle),
                "runout_bundle": str(result.runout_bundle),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
