"""Isolated AvaFrame 2.1 ``avaSimilaritySol`` verification worker.

This module is intentionally standalone.  It runs under the selected AvaFrame
Python with ``-I`` and never imports the serving application or AvyCore.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import shutil
from pathlib import Path

import numpy as np
from avaframe.ana1Tests import analysisTools, simiSolTest
from avaframe.com1DFA import com1DFA
from avaframe.in1Data import getInput
from avaframe.in2Trans import rasterUtils
from avaframe.in3Utils import cfgUtils


SCHEMA_VERSION = "avycore-avaframe-similarity-worker-v1"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json(value) + b"\n")


def _environment_manifest() -> dict[str, object]:
    packages = sorted(
        (
            {
                "name": str(distribution.metadata.get("Name", "")).lower(),
                "version": str(distribution.version),
            }
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        ),
        key=lambda item: (item["name"], item["version"]),
    )
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_release": platform.release(),
        "platform_machine": platform.machine(),
        "packages": packages,
    }


def _configuration_manifest(cfg: object, cfg_main: object) -> dict[str, object]:
    module = {
        section: {key: cfg[section][key] for key in sorted(cfg[section])}
        for section in sorted(cfg.sections())
    }
    main = {
        section: {key: cfg_main[section][key] for key in sorted(cfg_main[section])}
        for section in sorted(cfg_main.sections())
    }
    main["MAIN"]["avalancheDir"] = "{isolated_avalanche_dir}"
    return {"module": module, "general": main}


def _copy_and_verify_inputs(
    source: Path,
    destination: Path,
    expected: dict[str, object],
) -> dict[str, object]:
    copied: dict[str, object] = {}
    for relative_name, identity in sorted(expected.items()):
        source_path = source / relative_name
        if not source_path.is_file():
            raise FileNotFoundError(f"Required upstream input is missing: {source_path}")
        size = source_path.stat().st_size
        digest = _file_sha256(source_path)
        if size != int(identity["byte_size"]) or digest != str(identity["sha256"]):
            raise ValueError(f"Upstream input identity mismatch: {relative_name}")
        target = destination / relative_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)
        copied[relative_name] = {"byte_size": size, "sha256": digest}
    return copied


def _apply_overrides(cfg: object, overrides: dict[str, object]) -> None:
    for section, settings in overrides.items():
        if section not in cfg:
            raise KeyError(f"Acceptance override references missing section {section!r}.")
        for key, value in settings.items():
            if key not in cfg[section]:
                raise KeyError(f"Acceptance override references missing option {section}.{key}.")
            cfg[section][key] = str(value)


def _read_mass_balance(avalanche_dir: Path, simulation_name: str) -> dict[str, float]:
    path = avalanche_dir / "Outputs" / "com1DFA" / f"mass_{simulation_name}.txt"
    if not path.is_file():
        candidates = sorted((avalanche_dir / "Outputs" / "com1DFA").glob("mass_*.txt"))
        if len(candidates) != 1:
            raise RuntimeError(f"Expected one mass-balance file, found {len(candidates)}.")
        path = candidates[0]
    values = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    if values.shape[1] != 7 or values.shape[0] < 2 or not np.isfinite(values).all():
        raise RuntimeError("AvaFrame mass-balance output is incomplete or invalid.")
    initial = float(values[0, 1])
    final = float(values[-1, 1])
    if initial <= 0.0:
        raise RuntimeError("AvaFrame reported a non-positive initial mass.")
    return {
        "initial_mass_kg": initial,
        "final_mass_kg": final,
        "absolute_error_kg": abs(final - initial),
        "relative_error": abs(final - initial) / initial,
        "maximum_absolute_entrained_mass_kg": float(np.max(np.abs(values[:, 2]))),
        "maximum_absolute_detrained_mass_kg": float(np.max(np.abs(values[:, 3]))),
        "source_artifact_sha256": _file_sha256(path),
    }


def _relative_speed_l2(
    analytical: np.ndarray,
    numerical: np.ndarray,
    support: np.ndarray,
    cell_size: float,
    cos_angle: float,
) -> float:
    analytical_supported = np.where(support, analytical, 0.0)
    numerical_supported = np.where(support, numerical, 0.0)
    _, relative, _, _ = analysisTools.normL2Scal(
        analytical_supported,
        numerical_supported,
        cell_size,
        cos_angle,
    )
    return float(relative)


def _front_position(field: np.ndarray, x_centres: np.ndarray) -> float:
    positive_columns = np.any(field > 0.0, axis=0)
    if not np.any(positive_columns):
        raise RuntimeError("Positive-thickness front is absent.")
    return float(np.max(x_centres[positive_columns]))


def _metric(value: float, maximum: float | None) -> dict[str, object]:
    finite = bool(math.isfinite(value))
    passed = finite if maximum is None else finite and value <= maximum
    return {"value": value, "maximum": maximum, "passed": passed}


def run(request_path: Path, output_dir: Path) -> None:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported similarity-worker request schema.")
    version = importlib.metadata.version("avaframe")
    if version != request["expected_engine_version"]:
        raise RuntimeError(
            f"AvaFrame version mismatch: expected {request['expected_engine_version']}, got {version}."
        )

    acceptance_path = Path(str(request["acceptance_path"]))
    acceptance_bytes = acceptance_path.read_bytes()
    if hashlib.sha256(acceptance_bytes).hexdigest() != request["acceptance_sha256"]:
        raise ValueError("Acceptance artifact changed after the parent process validated it.")
    acceptance = json.loads(acceptance_bytes)
    if acceptance.get("schema") != "avycore-avaframe-analytical-acceptance-v1":
        raise ValueError("Unsupported analytical acceptance schema.")
    if not acceptance.get("locked_before_first_run"):
        raise ValueError("Analytical acceptance criteria are not locked.")

    avalanche_dir = request_path.parent / "avaSimilaritySol"
    inputs_dir = avalanche_dir / "Inputs"
    inputs_dir.mkdir(parents=True, exist_ok=False)
    source_manifest = _copy_and_verify_inputs(
        Path(str(request["source_inputs_path"])),
        inputs_dir,
        acceptance["upstream_inputs"]["files"],
    )

    source_cfg = inputs_dir / "simiSol_com1DFACfg.ini"
    cfg = cfgUtils.getModuleConfig(com1DFA, fileOverride=source_cfg, toPrint=False)
    _apply_overrides(cfg, acceptance["case_configuration"]["overrides"])
    cfg["EXPORTS"]["exportData"] = "True"
    physics = acceptance["case_configuration"]["required_physics"]
    required_values = {
        "frictModel": physics["friction_model"],
        "rho": str(int(physics["release_density_kg_m3"])),
        "relThFromFile": "True",
    }
    for key, expected in required_values.items():
        if cfg["GENERAL"].get(key).lower() != str(expected).lower():
            raise ValueError(f"Effective analytical configuration violates {key}={expected!r}.")

    cfg_main = cfgUtils.getGeneralConfig()
    cfg_main["MAIN"]["avalancheDir"] = str(avalanche_dir)
    cfg_main["MAIN"]["nCPU"] = "1"
    cfg_main["MAIN"]["CPUPercent"] = "100"
    for key in tuple(cfg_main["FLAGS"]):
        if key.lower() in {
            "showplot",
            "saveplot",
            "createreport",
            "showonlinebackground",
            "reportonefile",
            "debugplot",
        }:
            cfg_main["FLAGS"][key] = "False"

    effective_cfg_path = inputs_dir / "simiSol_effective_com1DFACfg.ini"
    with effective_cfg_path.open("w", encoding="utf-8", newline="\n") as stream:
        cfg.write(stream)
    configuration = _configuration_manifest(cfg, cfg_main)

    dem_path = getInput.getDEMPath(avalanche_dir)
    simiSolTest.getReleaseThickness(avalanche_dir, cfg, dem_path)
    _, _, report_dicts, sim_df = com1DFA.com1DFAMain(cfg_main, cfgInfo=cfg)
    if len(sim_df.index) != 1 or len(report_dicts) != 1:
        raise RuntimeError(
            f"Acceptance case must produce exactly one simulation, got {len(sim_df.index)}."
        )
    sim_hash = str(sim_df.index[0])
    sim_name = str(sim_df.iloc[0]["simName"])

    fields_list, field_header, time_list = com1DFA.readFields(
        avalanche_dir,
        ["FT", "FV", "Vx", "Vy", "Vz"],
        simName=sim_name,
        flagAvaDir=True,
        comModule="com1DFA",
    )
    requested_time = float(acceptance["case_configuration"]["evaluation_time_s"])
    field_index = min(
        int(np.searchsorted(time_list, requested_time)),
        min(len(time_list) - 1, len(fields_list) - 1),
    )
    actual_time = float(time_list[field_index])
    field = fields_list[field_index]

    solution = simiSolTest.mainSimilaritySol(effective_cfg_path)
    analytical_index = min(
        int(np.searchsorted(solution["time"], actual_time)),
        len(solution["time"]) - 1,
    )
    analytical = simiSolTest.getSimiSolParameters(
        solution,
        field_header,
        analytical_index,
        cfg["SIMISOL"],
        cfg["SIMISOL"].getfloat("relTh"),
        cfg["GENERAL"].getfloat("gravAcc"),
    )

    numerical_depth = np.asarray(field["FT"], dtype=np.float64)
    numerical_speed = np.asarray(field["FV"], dtype=np.float64)
    numerical_components = {
        axis: np.asarray(field[name], dtype=np.float64)
        for axis, name in (("x", "Vx"), ("y", "Vy"), ("z", "Vz"))
    }
    analytical_depth = np.asarray(analytical["hSimi"], dtype=np.float64)
    analytical_speed = np.asarray(analytical["vSimi"], dtype=np.float64)
    analytical_components = {
        axis: np.asarray(analytical[name], dtype=np.float64)
        for axis, name in (("x", "vxSimi"), ("y", "vySimi"), ("z", "vzSimi"))
    }
    shape = numerical_depth.shape
    arrays = [
        numerical_depth,
        numerical_speed,
        analytical_depth,
        analytical_speed,
        *numerical_components.values(),
        *analytical_components.values(),
    ]
    if any(array.shape != shape for array in arrays):
        raise RuntimeError("Analytical and numerical comparison fields do not share one grid.")
    invalid = np.zeros(shape, dtype=bool)
    for array in arrays:
        invalid |= ~np.isfinite(array)

    cell_size = float(field_header["cellsize"])
    cos_angle = float(analytical["cos"])
    h_l2, h_l2_rel, h_linf, h_linf_rel = analysisTools.normL2Scal(
        analytical_depth,
        numerical_depth,
        cell_size,
        cos_angle,
    )
    analytical_momentum = {
        "fx": analytical_depth * analytical_components["x"],
        "fy": analytical_depth * analytical_components["y"],
        "fz": analytical_depth * analytical_components["z"],
    }
    numerical_momentum = {
        "fx": numerical_depth * numerical_components["x"],
        "fy": numerical_depth * numerical_components["y"],
        "fz": numerical_depth * numerical_components["z"],
    }
    vh_l2, vh_l2_rel, vh_linf, vh_linf_rel = analysisTools.normL2Vect(
        analytical_momentum,
        numerical_momentum,
        cell_size,
        cos_angle,
    )
    support = (analytical_depth > 0.0) | (numerical_depth > 0.0)
    speed_l2_rel = _relative_speed_l2(
        analytical_speed,
        numerical_speed,
        support,
        cell_size,
        cos_angle,
    )

    x_centres = (
        float(field_header["xllcenter"])
        + np.arange(int(field_header["ncols"]), dtype=np.float64) * cell_size
    )
    analytical_front = _front_position(analytical_depth, x_centres)
    numerical_front = _front_position(numerical_depth, x_centres)
    front_error = abs(numerical_front - analytical_front)
    mass = _read_mass_balance(avalanche_dir, sim_name)
    analytical_volume = (
        math.pi
        * float(cfg["SIMISOL"]["L_x"])
        * float(cfg["SIMISOL"]["L_y"])
        * float(cfg["SIMISOL"]["relTh"])
        / 2.0
    )
    numerical_volume = mass["initial_mass_kg"] / float(cfg["GENERAL"]["rho"])
    initial_volume_relative_error = abs(numerical_volume - analytical_volume) / analytical_volume

    thresholds = acceptance["acceptance_metrics"]
    metrics = {
        "front_downstream_absolute_error_m": _metric(
            front_error, float(thresholds["front_downstream_absolute_error_m"]["maximum"])
        ),
        "flow_thickness_relative_l2": _metric(
            float(h_l2_rel), float(thresholds["flow_thickness_relative_l2"]["maximum"])
        ),
        "flow_thickness_relative_linf": _metric(
            float(h_linf_rel), float(thresholds["flow_thickness_relative_linf"]["maximum"])
        ),
        "momentum_relative_l2": _metric(
            float(vh_l2_rel), float(thresholds["momentum_relative_l2"]["maximum"])
        ),
        "momentum_relative_linf": _metric(
            float(vh_linf_rel), float(thresholds["momentum_relative_linf"]["maximum"])
        ),
        "flow_velocity_relative_l2": _metric(speed_l2_rel, None),
        "solver_mass_balance_relative_error": _metric(
            mass["relative_error"],
            float(thresholds["solver_mass_balance_relative_error"]["maximum"]),
        ),
        "initial_volume_relative_error": _metric(
            initial_volume_relative_error,
            float(thresholds["initial_volume_relative_error"]["maximum"]),
        ),
    }

    grid = {
        "shape": [int(shape[0]), int(shape[1])],
        "nrows": int(field_header["nrows"]),
        "ncols": int(field_header["ncols"]),
        "xllcenter_m": float(field_header["xllcenter"]),
        "yllcenter_m": float(field_header["yllcenter"]),
        "cell_size_m": cell_size,
        "coordinate_order": "x,y",
        "origin_semantics": "lower_left_cell_center",
        "crs": None,
        "crs_status": "undefined_local_cartesian",
    }
    grid_passed = (
        shape == (grid["nrows"], grid["ncols"])
        and math.isclose(
            cell_size,
            float(thresholds["grid"]["cell_size_m"]),
            rel_tol=0.0,
            abs_tol=float(thresholds["grid"]["absolute_tolerance_m"]),
        )
    )
    mask_passed = int(np.count_nonzero(invalid)) == int(
        thresholds["masks"]["expected_invalid_cell_count"]
    )
    boundary_touched = bool(
        np.any(numerical_depth[0, :] > 0.0)
        or np.any(numerical_depth[-1, :] > 0.0)
        or np.any(numerical_depth[:, 0] > 0.0)
        or np.any(numerical_depth[:, -1] > 0.0)
    )
    invariants = {
        "grid": {"passed": grid_passed},
        "units": {"passed": True, **thresholds["units"]},
        "crs": {
            "passed": grid["crs_status"] == thresholds["crs"]["expected_status"],
            "status": grid["crs_status"],
        },
        "masks": {
            "passed": mask_passed,
            "invalid_cell_count": int(np.count_nonzero(invalid)),
            "valid_cell_count": int(invalid.size - np.count_nonzero(invalid)),
        },
        "domain_boundary": {"passed": not boundary_touched, "touched": boundary_touched},
        "mass_sources": {
            "passed": mass["maximum_absolute_entrained_mass_kg"] == 0.0
            and mass["maximum_absolute_detrained_mass_kg"] == 0.0,
            "maximum_absolute_entrained_mass_kg": mass[
                "maximum_absolute_entrained_mass_kg"
            ],
            "maximum_absolute_detrained_mass_kg": mass[
                "maximum_absolute_detrained_mass_kg"
            ],
        },
    }
    controlling_metrics_passed = all(
        item["passed"]
        for name, item in metrics.items()
        if thresholds[name].get("maximum") is not None
    )
    overall_passed = controlling_metrics_passed and all(
        item["passed"] for item in invariants.values()
    )

    output_dir.mkdir(parents=True, exist_ok=False)
    np.savez_compressed(
        output_dir / "comparison-fields.npz",
        analytical_depth_m=analytical_depth.astype(np.float32),
        analytical_speed_m_s=analytical_speed.astype(np.float32),
        analytical_vx_m_s=analytical_components["x"].astype(np.float32),
        analytical_vy_m_s=analytical_components["y"].astype(np.float32),
        analytical_vz_m_s=analytical_components["z"].astype(np.float32),
        numerical_depth_m=numerical_depth.astype(np.float32),
        numerical_speed_m_s=numerical_speed.astype(np.float32),
        numerical_vx_m_s=numerical_components["x"].astype(np.float32),
        numerical_vy_m_s=numerical_components["y"].astype(np.float32),
        numerical_vz_m_s=numerical_components["z"].astype(np.float32),
        invalid_mask=invalid,
    )
    _write_json(output_dir / "configuration.json", configuration)
    environment = _environment_manifest()
    _write_json(output_dir / "environment.json", environment)
    artifacts = {
        name: {
            "byte_size": path.stat().st_size,
            "sha256": _file_sha256(path),
        }
        for name, path in {
            "comparison_fields": output_dir / "comparison-fields.npz",
            "configuration": output_dir / "configuration.json",
            "environment": output_dir / "environment.json",
        }.items()
    }
    result_core = {
        "schema": "avycore-avaframe-analytical-result-v1",
        "benchmark_id": acceptance["benchmark_id"],
        "scientific_status": "software_verification_only",
        "engine": {
            "distribution": "avaframe",
            "version": version,
            "source_commit": acceptance["engine"]["source_commit"],
            "python_executable_sha256": request["python_executable_sha256"],
            "adapter_sha256": request["adapter_sha256"],
        },
        "acceptance_sha256": request["acceptance_sha256"],
        "source_inputs": source_manifest,
        "configuration_sha256": _sha256_json(configuration),
        "environment_sha256": _sha256_json(environment),
        "seed": int(cfg["GENERAL"]["seed"]),
        "upstream_execution_label": {
            "simulation_hash": sim_hash,
            "simulation_name": sim_name,
            "identity_role": (
                "recorded_but_excluded_from_result_id_because_avaframe_hashes_"
                "the_disposable_absolute_avalanche_directory"
            ),
        },
        "requested_evaluation_time_s": requested_time,
        "actual_evaluation_time_s": actual_time,
        "analytical_evaluation_time_s": float(solution["time"][analytical_index]),
        "grid": grid,
        "mask": {
            "invalid_cell_count": int(np.count_nonzero(invalid)),
            "valid_cell_count": int(invalid.size - np.count_nonzero(invalid)),
            "zero_thickness_is_valid": True,
        },
        "front": {
            "analytical_downstream_x_m": analytical_front,
            "numerical_downstream_x_m": numerical_front,
        },
        "mass_balance": {
            **mass,
            "analytical_initial_volume_m3": analytical_volume,
            "numerical_initial_volume_m3": numerical_volume,
            "initial_volume_relative_error": initial_volume_relative_error,
        },
        "absolute_errors": {
            "flow_thickness_l2": float(h_l2),
            "flow_thickness_linf_m": float(h_linf),
            "momentum_l2_m2_s": float(vh_l2),
            "momentum_linf_m2_s": float(vh_linf),
        },
        "metrics": metrics,
        "invariants": invariants,
        "pressure": {
            "status": "not_applicable",
            "reason": acceptance["not_applicable"]["pressure"],
        },
        "artifacts": artifacts,
        "overall_passed": overall_passed,
        "limitations": [
            acceptance["not_applicable"]["field_validation"],
            "The upstream analytical case has an undefined local Cartesian CRS.",
            "Direct scalar velocity error is diagnostic; upstream acceptance is based on thickness-integrated momentum.",
            "AvaFrame's internal simulation hash includes the disposable absolute avalanche-directory path; the exact label is retained but excluded from the deterministic scientific result identity.",
        ],
    }
    identity_core = {
        key: value
        for key, value in result_core.items()
        if key != "upstream_execution_label"
    }
    result_id = _sha256_json(identity_core)
    result = {**result_core, "result_id": result_id}
    _write_json(output_dir / "benchmark-result.json", result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    run(arguments.request.resolve(), arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
