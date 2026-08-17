"""Run the frozen GEODAR along-thalweg dynamics consistency experiment.

The GEODAR repository provides processed runout-defining trajectories matched
to one-dimensional thalwegs.  This runner extrudes each measured thalweg into
a narrow, cross-slope-flat strip and exercises only AvyCore's open-snow,
``dynamics_only`` point-particle equation.  It does not construct release or
deposit observations, tune parameters, or claim geographic generalization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import h5py
import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AVYCORE_SOURCE = REPOSITORY_ROOT / "packages" / "avycore" / "src"
if str(AVYCORE_SOURCE) not in sys.path:
    sys.path.insert(0, str(AVYCORE_SOURCE))

from avycore.hazard.runout import DYNAMICS_ONLY, ParticleRunoutEngine  # noqa: E402
from avycore.hazard.zone import ReleaseZone  # noqa: E402


SPEC_RELATIVE_PATH = Path("validation-data/experiments/geodar-along-thalweg-v1.json")
RESULT_RELATIVE_PATH = Path("validation-data/results/geodar-along-thalweg-v1.json")
ENGINE_RELATIVE_PATH = Path("packages/avycore/src/avycore/hazard/runout.py")
PARAMETER_RELATIVE_PATH = Path("backend/config/m0-baseline.json")
RUNNER_RELATIVE_PATH = Path("scripts/validation/run_geodar_along_thalweg_experiment.py")


@dataclass(frozen=True)
class _Grid:
    shape: tuple[int, int]
    resolution_m: float


class _Config:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def require(self, dotted: str) -> Any:
        node: Any = self._values
        for part in dotted.split("."):
            node = node[part]
        return node


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_file(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_array(path: Path, name: str) -> np.ndarray:
    with h5py.File(path, "r") as source:
        if name not in source:
            raise ValueError(f"{path.name} has no /{name} dataset")
        result = np.asarray(source[name][()], dtype="float64").reshape(-1)
    return result


def _strictly_increasing_subset(*arrays: np.ndarray) -> tuple[np.ndarray, ...]:
    """Drop exact duplicate leading-coordinate samples without sorting data."""

    if not arrays or any(array.shape != arrays[0].shape for array in arrays):
        raise ValueError("Aligned observation arrays must have the same shape")
    keep = np.ones(arrays[0].size, dtype=bool)
    if keep.size > 1:
        keep[1:] = np.diff(arrays[0]) > 0.0
    selected = tuple(array[keep] for array in arrays)
    if selected[0].size > 1 and not bool(np.all(np.diff(selected[0]) > 0.0)):
        raise ValueError("Observation coordinate is not strictly increasing")
    return selected


def _configuration(spec: dict[str, Any]) -> _Config:
    frozen = spec["fixed_model"]
    mu = float(frozen["open_snow_mu"])
    xi = float(frozen["open_snow_xi_m_per_s2"])
    return _Config(
        {
            "runout": {
                "alpha_angle_deg": {
                    "small": 32.0,
                    "medium": 27.0,
                    "large": 23.0,
                    "very_large": 19.0,
                },
                "alpha_uncertainty_deg": 4.0,
                "flow_regime": {
                    "dry_slab": {
                        "mu_scale": 1.0,
                        "xi_scale": 1.0,
                        "alpha_shift_deg": 0.0,
                    }
                },
                "friction": {
                    "open_snow": mu,
                    "forest": mu,
                    "gully": mu,
                    "xi_open": xi,
                    "xi_forest": xi,
                },
                "advanced_mode": {
                    "particles_per_zone": int(frozen["particles"]),
                    "max_steps": int(frozen["maximum_steps"]),
                    "time_step_s": float(frozen["time_step_s"]),
                    "lateral_jitter": float(frozen["lateral_jitter"]),
                    "stopping_velocity_ms": float(frozen["stopping_velocity_ms"]),
                    "random_seed": int(frozen["random_seed"]),
                    "velocity_classes_ms": [5.0, 15.0, 25.0, 40.0],
                },
            }
        }
    )


def _profile_arrays(
    thalweg_path: Path, spec: dict[str, Any]
) -> tuple[
    np.ma.MaskedArray,
    np.ma.MaskedArray,
    np.ma.MaskedArray,
    np.ma.MaskedArray,
    np.ndarray,
    int,
]:
    x = _read_array(thalweg_path, "X")
    y = _read_array(thalweg_path, "Y")
    z = _read_array(thalweg_path, "Z")
    if min(x.size, y.size, z.size) < 3 or not (
        np.isfinite(x).all() and np.isfinite(y).all() and np.isfinite(z).all()
    ):
        raise ValueError("Thalweg requires at least three finite X/Y/Z samples")

    horizontal = np.r_[0.0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    surface = np.r_[
        0.0,
        np.cumsum(np.sqrt(np.diff(x) ** 2 + np.diff(y) ** 2 + np.diff(z) ** 2)),
    ]
    if not bool(np.all(np.diff(horizontal) > 0.0)):
        raise ValueError("Thalweg horizontal distance is not strictly increasing")

    reconstruction = spec["profile_reconstruction"]
    cell = float(reconstruction["horizontal_grid_resolution_m"])
    padding = int(reconstruction["release_row_padding_cells"])
    columns = int(reconstruction["strip_columns"])
    horizontal_grid = np.arange(
        0.0, math.ceil(horizontal[-1] / cell) * cell + 0.5 * cell, cell
    )
    z_grid = np.interp(horizontal_grid, horizontal, z)
    surface_grid = np.interp(horizontal_grid, horizontal, surface)

    initial_grade = (z_grid[1] - z_grid[0]) / cell
    padding_x = -cell * np.arange(padding, 0, -1, dtype="float64")
    padding_z = z_grid[0] + initial_grade * padding_x
    padding_surface = padding_x * math.sqrt(1.0 + initial_grade**2)
    z_rows = np.r_[padding_z, z_grid]
    surface_rows = np.r_[padding_surface, surface_grid]

    elevation_values = np.repeat(z_rows[:, None], columns, axis=1)
    elevation = np.ma.array(elevation_values, mask=np.zeros_like(elevation_values, dtype=bool))
    dz_drow, dz_dcol = np.gradient(elevation_values, cell, cell)
    slope_values = np.degrees(np.arctan(np.hypot(dz_drow, dz_dcol)))
    slope = np.ma.array(slope_values, mask=np.zeros_like(slope_values, dtype=bool))
    forest = np.ma.array(np.zeros_like(elevation_values), mask=np.zeros_like(elevation_values, dtype=bool))
    plan = np.ma.array(np.zeros_like(elevation_values), mask=np.zeros_like(elevation_values, dtype=bool))
    return elevation, slope, forest, plan, surface_rows, padding


def _model_elapsed_time(surface: np.ndarray, velocity: np.ndarray) -> np.ndarray:
    """Integrate elapsed time from the model's sampled along-path speed profile."""

    if surface.size != velocity.size or surface.size < 2:
        raise ValueError("At least two aligned model samples are required")
    mean_velocity = 0.5 * (velocity[:-1] + velocity[1:])
    if bool(np.any(mean_velocity <= 0.0)):
        raise ValueError("Non-positive speed prevents travel-time reconstruction")
    increments = np.diff(surface) / mean_velocity
    return np.r_[0.0, np.cumsum(increments)]


def _run_event(
    trajectory_path: Path,
    thalweg_path: Path,
    spec: dict[str, Any],
    config: _Config,
) -> dict[str, Any]:
    observed_t = _read_array(trajectory_path, "T")
    observed_s = _read_array(trajectory_path, "S")
    observed_v = _read_array(trajectory_path, "V")
    if not (
        np.isfinite(observed_t).all()
        and np.isfinite(observed_s).all()
        and np.isfinite(observed_v).all()
    ):
        raise ValueError("Trajectory contains non-finite T/S/V values")
    observed_t, observed_s, observed_v = _strictly_increasing_subset(
        observed_t, observed_s, observed_v
    )
    if observed_t.size < int(spec["selection_rule"]["minimum_samples"]):
        raise ValueError("Trajectory is shorter than the frozen minimum sample count")
    if not bool(np.all(np.diff(observed_s) > 0.0)):
        raise ValueError("Trajectory S is not strictly increasing")

    elevation, slope, forest, plan, surface_rows, padding = _profile_arrays(
        thalweg_path, spec
    )
    if observed_s[0] < 0.0 or observed_s[-1] > surface_rows[-1]:
        raise ValueError("Observed S interval is outside the reconstructed thalweg")
    zone_pixels = np.zeros(elevation.shape, dtype=bool)
    centre_col = elevation.shape[1] // 2
    zone_pixels[padding, centre_col] = True

    result = ParticleRunoutEngine(DYNAMICS_ONLY).simulate(
        zone=ReleaseZone("geodar-thalweg-release", zone_pixels, geometry=None),
        grid=_Grid(elevation.shape, float(spec["profile_reconstruction"]["horizontal_grid_resolution_m"])),
        elevation=elevation,
        slope=slope,
        forest_mask=forest,
        plan_curvature=plan,
        config=config,
        release_size=str(spec["fixed_model"]["release_size"]),
        seed=int(spec["fixed_model"]["random_seed"]),
        flow_regime=str(spec["fixed_model"]["flow_regime"]),
    )

    visited_rows = np.flatnonzero(np.any(result.uncertainty, axis=1))
    if visited_rows.size < 3:
        raise ValueError("The engine produced fewer than three visited profile rows")
    model_s = surface_rows[visited_rows]
    model_v = np.max(result.velocity[visited_rows], axis=1).astype("float64")
    positive = model_v > 0.0
    model_s = model_s[positive]
    model_v = model_v[positive]
    if model_s.size < 2:
        raise ValueError("The engine produced fewer than two positive-speed samples")

    model_s, model_v = _strictly_increasing_subset(model_s, model_v)
    full_observed_coverage = bool(model_s[0] <= observed_s[0] and model_s[-1] >= observed_s[-1])
    overlap = (observed_s >= model_s[0]) & (observed_s <= model_s[-1])
    overlap_fraction = float(overlap.mean())

    velocity_nrmse: float | None = None
    relative_travel_time_rmse: float | None = None
    if full_observed_coverage:
        model_at_observed = np.interp(observed_s, model_s, model_v)
        velocity_nrmse = float(
            np.sqrt(np.mean((model_at_observed - observed_v) ** 2))
            / max(float(np.max(observed_v)), 1.0e-12)
        )
        model_elapsed = _model_elapsed_time(model_s, model_v)
        predicted_elapsed = np.interp(observed_s, model_s, model_elapsed)
        predicted_elapsed -= predicted_elapsed[0]
        observed_elapsed = observed_t - observed_t[0]
        relative_travel_time_rmse = float(
            np.sqrt(np.mean((predicted_elapsed - observed_elapsed) ** 2))
            / max(float(observed_elapsed[-1]), 1.0e-12)
        )

    terminal_error = float(abs(model_s[-1] - observed_s[-1]) / observed_s[-1])
    thresholds = {entry["name"]: float(entry["threshold"]) for entry in spec["metrics"]}
    left_profile = int(result.metadata["particles_left_the_aoi"])
    at_cutoff = int(result.metadata["particles_still_moving_at_cutoff"])
    metric_pass = {
        "velocity_nrmse": velocity_nrmse is not None
        and velocity_nrmse <= thresholds["velocity_nrmse"],
        "relative_travel_time_rmse": relative_travel_time_rmse is not None
        and relative_travel_time_rmse <= thresholds["relative_travel_time_rmse"],
        "terminal_surface_distance_relative_error": terminal_error
        <= thresholds["terminal_surface_distance_relative_error"],
    }
    event_pass = bool(
        full_observed_coverage
        and left_profile == 0
        and at_cutoff == 0
        and all(metric_pass.values())
    )
    event_id = trajectory_path.name.removeprefix("GEODAR-").removesuffix(
        "-TRAJ-001.h5"
    )
    return {
        "event_id": event_id,
        "trajectory_file": trajectory_path.name,
        "trajectory_bytes": trajectory_path.stat().st_size,
        "trajectory_md5": _md5_file(trajectory_path),
        "trajectory_sha256": _sha256_file(trajectory_path),
        "thalweg_file": thalweg_path.name,
        "thalweg_bytes": thalweg_path.stat().st_size,
        "thalweg_md5": _md5_file(thalweg_path),
        "thalweg_sha256": _sha256_file(thalweg_path),
        "observation_samples": int(observed_s.size),
        "observed_surface_interval_m": [round(float(observed_s[0]), 6), round(float(observed_s[-1]), 6)],
        "observed_duration_s": round(float(observed_t[-1] - observed_t[0]), 6),
        "observed_peak_velocity_ms": round(float(np.max(observed_v)), 6),
        "model_surface_interval_m": [round(float(model_s[0]), 6), round(float(model_s[-1]), 6)],
        "model_peak_velocity_ms": round(float(np.max(model_v)), 6),
        "observed_interval_covered": full_observed_coverage,
        "observed_sample_overlap_fraction": round(overlap_fraction, 6),
        "particles_left_profile": left_profile,
        "particles_still_moving_at_cutoff": at_cutoff,
        "metrics": {
            "velocity_nrmse": round(velocity_nrmse, 9) if velocity_nrmse is not None else None,
            "relative_travel_time_rmse": (
                round(relative_travel_time_rmse, 9)
                if relative_travel_time_rmse is not None
                else None
            ),
            "terminal_surface_distance_relative_error": round(terminal_error, 9),
        },
        "metric_pass": metric_pass,
        "event_pass": event_pass,
    }


def _median(values: list[float]) -> float | None:
    return round(float(np.median(values)), 9) if values else None


def _verify_zenodo_record(source_dir: Path) -> dict[str, Any]:
    record_path = source_dir / "zenodo-record-1042108.json"
    if not record_path.exists():
        raise FileNotFoundError(
            "The exact Zenodo API record JSON is required beside the GEODAR files"
        )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if int(record.get("id", 0)) != 1042108:
        raise ValueError("Zenodo record JSON is not record 1042108")
    repository_files = {entry["key"]: entry for entry in record["files"]}
    local_files = [
        path
        for path in source_dir.iterdir()
        if path.is_file()
        and (
            path.name in {"data_table.csv", "geodar_repository.pdf"}
            or path.name.endswith(("-TRAJ-001.h5", "-THALWEG-001.h5"))
        )
    ]
    for path in local_files:
        entry = repository_files.get(path.name)
        if entry is None:
            raise ValueError(f"{path.name} is absent from Zenodo record 1042108")
        expected_md5 = str(entry["checksum"]).removeprefix("md5:")
        if path.stat().st_size != int(entry["size"]):
            raise ValueError(f"Zenodo byte-size mismatch for {path.name}")
        if _md5_file(path) != expected_md5:
            raise ValueError(f"Zenodo MD5 mismatch for {path.name}")
    return {
        "file": record_path.name,
        "bytes": record_path.stat().st_size,
        "sha256": _sha256_file(record_path),
        "record_id": int(record["id"]),
        "repository_files_verified": len(local_files),
        "repository_total_files": len(repository_files),
    }


def run(source_dir: Path, output_path: Path) -> dict[str, Any]:
    spec_path = REPOSITORY_ROOT / SPEC_RELATIVE_PATH
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    engine_path = REPOSITORY_ROOT / ENGINE_RELATIVE_PATH
    parameter_path = REPOSITORY_ROOT / PARAMETER_RELATIVE_PATH
    if _sha256_file(engine_path) != spec["fixed_model"]["engine_source_sha256_at_freeze"]:
        raise ValueError("Runout engine changed after the GEODAR experiment was frozen")
    if _sha256_file(parameter_path) != spec["fixed_model"]["parameter_file_sha256_at_freeze"]:
        raise ValueError("Parameter manifest changed after the GEODAR experiment was frozen")

    zenodo_record = _verify_zenodo_record(source_dir)
    trajectories = sorted(source_dir.glob("GEODAR-*-TRAJ-001.h5"))
    if not trajectories:
        raise FileNotFoundError(f"No GEODAR trajectory files found in {source_dir}")
    events: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    config = _configuration(spec)
    for trajectory in trajectories:
        thalweg = trajectory.with_name(
            trajectory.name.replace("-TRAJ-001.h5", "-THALWEG-001.h5")
        )
        if not thalweg.exists():
            continue
        try:
            events.append(_run_event(trajectory, thalweg, spec, config))
        except ValueError as error:
            rejected.append({"trajectory_file": trajectory.name, "reason": str(error)})

    pass_count = sum(bool(event["event_pass"]) for event in events)
    pass_fraction = pass_count / len(events) if events else 0.0
    minimum_events = int(spec["aggregate_acceptance"]["minimum_scoreable_events"])
    required_fraction = float(
        spec["aggregate_acceptance"]["required_event_pass_fraction"]
    )
    aggregate_pass = bool(len(events) >= minimum_events and pass_fraction >= required_fraction)
    metric_values: dict[str, list[float]] = {
        "velocity_nrmse": [],
        "relative_travel_time_rmse": [],
        "terminal_surface_distance_relative_error": [],
    }
    for event in events:
        for name in metric_values:
            value = event["metrics"][name]
            if value is not None:
                metric_values[name].append(float(value))

    result = {
        "schema": "avycore-geodar-along-thalweg-results-v1",
        "experiment_id": spec["experiment_id"],
        "spec_path": SPEC_RELATIVE_PATH.as_posix(),
        "spec_sha256": _sha256_file(spec_path),
        "scientific_use": spec["scientific_use"],
        "component_tested": spec["component_tested"],
        "engine_mode": spec["engine_mode"],
        "claim_boundary": spec["claim_boundary"],
        "source": {
            **spec["source"],
            "source_directory_not_committed": True,
            "zenodo_api_record": zenodo_record,
            "data_table": (
                {
                    "file": "data_table.csv",
                    "bytes": (source_dir / "data_table.csv").stat().st_size,
                    "md5": _md5_file(source_dir / "data_table.csv"),
                    "sha256": _sha256_file(source_dir / "data_table.csv"),
                }
                if (source_dir / "data_table.csv").exists()
                else None
            ),
        },
        "implementation": {
            "engine_source": ENGINE_RELATIVE_PATH.as_posix(),
            "engine_source_sha256": _sha256_file(engine_path),
            "parameter_file": PARAMETER_RELATIVE_PATH.as_posix(),
            "parameter_file_sha256": _sha256_file(parameter_path),
            "runner": RUNNER_RELATIVE_PATH.as_posix(),
            "runner_sha256": _sha256_file(REPOSITORY_ROOT / RUNNER_RELATIVE_PATH),
            "fixed_model": spec["fixed_model"],
            "profile_reconstruction": spec["profile_reconstruction"],
        },
        "selection": {
            "paired_events_evaluated": len(events),
            "rejected_before_scoring": rejected,
        },
        "aggregate": {
            "events_passed": pass_count,
            "events_evaluated": len(events),
            "event_pass_fraction": round(pass_fraction, 9),
            "required_event_pass_fraction": required_fraction,
            "minimum_scoreable_events": minimum_events,
            "median_metrics_over_available_values": {
                name: _median(values) for name, values in metric_values.items()
            },
            "aggregate_pass": aggregate_pass,
            "interpretation": (
                spec["aggregate_acceptance"]["interpretation_if_passed"]
                if aggregate_pass
                else spec["aggregate_acceptance"]["interpretation_if_failed"]
            ),
        },
        "events": events,
        "strict_field_validation_effect": {
            "eligible_holdout_events_added": 0,
            "is_validated": False,
            "trusted_registry_changed": False,
            "reason": "The source is one mountain and lacks the complete event inputs, raw-calibration uncertainty, release/deposit geometry, surveyed absence, and surface contract required by the strict holdout.",
        },
        "prohibited_claims": spec["prohibited_claims"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / RESULT_RELATIVE_PATH,
    )
    args = parser.parse_args()
    result = run(args.source_dir, args.output)
    print(json.dumps(result["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
