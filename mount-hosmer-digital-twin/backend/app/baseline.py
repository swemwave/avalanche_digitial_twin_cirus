"""Hermetic M0 numerical baseline and two-engine comparison command support.

The cases in this module are synthetic software-verification fixtures. They are
not observations, calibration evidence, or physical validation. Performance
fields are measured on the machine running the command and are never acceptance
thresholds; deterministic input/output hashes and summaries are the frozen part.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from avycore.hazard import risk, runout
from avycore.hazard.conditions import Conditions
from avycore.hazard.zone import ReleaseZone

from app.assess import _config, assessment_model_identity


BASELINE_SCHEMA = "mount-hosmer-m0-baseline-v1"
CASE_NAMES = ("benign", "loaded", "missing_data", "aoi_boundary")
DEFAULT_SEED = 20260713


@dataclass(frozen=True)
class _Grid:
    shape: tuple[int, int]
    resolution_m: float = 5.0


class _Terrain:
    def __init__(self, layers: dict[str, np.ma.MaskedArray], resolution_m: float = 5.0) -> None:
        self._layers = layers
        self.grid = _Grid(next(iter(layers.values())).shape, resolution_m)

    def layer(self, name: str) -> np.ma.MaskedArray:
        return self._layers[name]

    @staticmethod
    def reproject(col: Any, row: Any) -> tuple[Any, Any]:
        col_values = np.asarray(col, dtype="float64")
        row_values = np.asarray(row, dtype="float64")
        lon = -115.0 + col_values * 0.00005
        lat = 50.0 - row_values * 0.00005
        if col_values.ndim == 0:
            return float(lon), float(lat)
        return lon, lat


@dataclass(frozen=True)
class _Case:
    name: str
    terrain: _Terrain
    zone: ReleaseZone
    conditions: Conditions
    release_size: str


def _case(name: str) -> _Case:
    if name not in CASE_NAMES:
        raise KeyError(f"Unknown baseline case {name!r}; expected one of {CASE_NAMES}.")

    shape = (42, 31) if name == "aoi_boundary" else (48, 31)
    rows, cols = shape
    resolution_m = 5.0
    row_index = np.arange(rows, dtype="float64")[:, None]
    elevation = np.broadcast_to(
        3000.0 - np.tan(np.deg2rad(35.0)) * resolution_m * row_index,
        shape,
    ).astype("float32", copy=True)
    arrays: dict[str, np.ndarray] = {
        "elevation": elevation,
        "slope": np.full(shape, 35.0, dtype="float32"),
        "aspect": np.full(shape, 180.0, dtype="float32"),
        "general_curvature": np.zeros(shape, dtype="float32"),
        "plan_curvature": np.zeros(shape, dtype="float32"),
        "forest_mask": np.zeros(shape, dtype="float32"),
    }
    masks = {layer: np.zeros(shape, dtype=bool) for layer in arrays}
    if name == "missing_data":
        masks["aspect"][8:12, 4:8] = True
        masks["general_curvature"][12:16, 8:12] = True
        masks["forest_mask"][16:20, 12:16] = True
        masks["elevation"][24:28, :] = True
        masks["plan_curvature"][24:28, :] = True
        masks["forest_mask"][24:28, :] = True

    layers = {
        layer: np.ma.array(values, mask=masks[layer], copy=True)
        for layer, values in arrays.items()
    }
    pixels = np.zeros(shape, dtype=bool)
    if name != "benign":
        pixels[2:4, cols // 2 - 1 : cols // 2 + 2] = True
    conditions = Conditions(0.0, 0.0, 0.0) if name == "benign" else Conditions(50.0, 60.0, 0.0)
    release_size = "very_large" if name == "aoi_boundary" else "medium"
    return _Case(
        name=name,
        terrain=_Terrain(layers, resolution_m),
        zone=ReleaseZone("RZ001", pixels, geometry=None),
        conditions=conditions,
        release_size=release_size,
    )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _update_array(digest: Any, name: str, array: np.ndarray | np.ma.MaskedArray) -> None:
    masked = np.ma.asarray(array)
    values = np.ascontiguousarray(np.asarray(masked.data))
    if values.dtype.byteorder == ">" or (values.dtype.byteorder == "=" and sys.byteorder == "big"):
        values = values.astype(values.dtype.newbyteorder("<"), copy=False)
    mask = np.ascontiguousarray(np.ma.getmaskarray(masked), dtype=np.uint8)
    digest.update(_json_bytes({"name": name, "shape": values.shape, "dtype": values.dtype.str}))
    digest.update(values.tobytes(order="C"))
    digest.update(mask.tobytes(order="C"))


def _input_sha256(case: _Case, seed: int) -> str:
    digest = hashlib.sha256()
    digest.update(
        _json_bytes(
            {
                "case": case.name,
                "conditions": case.conditions.clamped().to_dict(),
                "release_size": case.release_size,
                "seed": seed,
                "resolution_m": case.terrain.grid.resolution_m,
                "model": assessment_model_identity(),
            }
        )
    )
    for name in sorted(case.terrain._layers):
        _update_array(digest, name, case.terrain.layer(name))
    _update_array(digest, "release_zone", case.zone.pixels)
    return digest.hexdigest()


def _output_sha256(field: risk.RiskField, result: runout.RunoutResult) -> str:
    digest = hashlib.sha256()
    _update_array(digest, "release", field.release)
    _update_array(digest, "reached", result.reached)
    _update_array(digest, "uncertainty", result.uncertainty)
    _update_array(digest, "intensity", result.intensity)
    _update_array(digest, "velocity", result.velocity)
    digest.update(
        _json_bytes(
            {
                "metadata": result.metadata,
                "warnings": result.warnings,
                "stopping_points": result.stopping_points,
            }
        )
    )
    return digest.hexdigest()


def _coverage(case: _Case, field: risk.RiskField) -> dict[str, Any]:
    layers = case.terrain._layers

    def intersection(names: tuple[str, ...]) -> float:
        valid = np.ones(case.terrain.grid.shape, dtype=bool)
        for layer_name in names:
            valid &= ~np.ma.getmaskarray(layers[layer_name])
        return round(float(valid.mean()), 6)

    return {
        "grid_cell_count": int(np.prod(case.terrain.grid.shape)),
        "layer_valid_fraction": {
            name: round(float((~np.ma.getmaskarray(layer)).mean()), 6)
            for name, layer in sorted(layers.items())
        },
        "release_required_valid_fraction": round(
            float((~np.ma.getmaskarray(field.release)).mean()), 6
        ),
        "runout_required_valid_fraction": intersection(
            ("elevation", "forest_mask", "plan_curvature")
        ),
    }


def _current_rss_bytes() -> int:
    if os.name == "nt":
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    statm = Path("/proc/self/statm")
    if statm.is_file():
        pages = int(statm.read_text(encoding="ascii").split()[1])
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    try:
        import resource

        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return maximum if sys.platform == "darwin" else maximum * 1024
    except (ImportError, OSError, ValueError):
        return 0


class _PeakRssSampler:
    def __init__(self) -> None:
        self.start = _current_rss_bytes()
        self.peak = self.start
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self._stop.wait(0.002):
            self.peak = max(self.peak, _current_rss_bytes())

    def __enter__(self) -> "_PeakRssSampler":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.peak = max(self.peak, _current_rss_bytes())
        self._stop.set()
        self._thread.join()


def run_case(engine_name: str, case_name: str, *, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Run one engine/case pair and return deterministic and performance evidence."""
    case = _case(case_name)
    engine = runout.get_engine(engine_name)
    started = time.perf_counter()
    with _PeakRssSampler() as memory:
        field = risk.compute_release(case.terrain, case.conditions)
        result = engine.simulate(
            zone=case.zone,
            grid=case.terrain.grid,
            elevation=case.terrain.layer("elevation"),
            slope=case.terrain.layer("slope"),
            forest_mask=case.terrain.layer("forest_mask"),
            plan_curvature=case.terrain.layer("plan_curvature"),
            config=_config(),
            release_size=case.release_size,
            seed=seed,
        )
    runtime_seconds = time.perf_counter() - started
    valid_release = field.release.compressed()
    summary = {
        "release_valid_cells": int(valid_release.size),
        "release_min": round(float(valid_release.min()), 6) if valid_release.size else None,
        "release_max": round(float(valid_release.max()), 6) if valid_release.size else None,
        "release_mean": round(float(valid_release.mean()), 6) if valid_release.size else None,
        "reached_cells": int(result.reached.sum()),
        "uncertainty_cells": int(result.uncertainty.sum()),
        "runout_area_m2": result.metadata.get("runout_area_m2", 0.0),
        "uncertainty_area_m2": result.metadata.get("uncertainty_area_m2", 0.0),
        "maximum_velocity_ms": round(float(result.velocity.max()), 6),
        "particles_left_the_aoi": int(result.metadata.get("particles_left_the_aoi", 0)),
    }
    return {
        "engine": engine_name,
        "engine_implementation": engine.name,
        "case": case_name,
        "seed": seed,
        "input_sha256": _input_sha256(case, seed),
        "input_coverage": _coverage(case, field),
        "output_sha256": _output_sha256(field, result),
        "summary": summary,
        "performance": {
            "runtime_seconds": round(runtime_seconds, 6),
            "peak_rss_bytes": memory.peak,
            "peak_rss_delta_bytes": max(0, memory.peak - memory.start),
            "performance_is_acceptance_threshold": False,
        },
    }


def load_expectations(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != BASELINE_SCHEMA:
        raise ValueError(f"Unsupported M0 baseline schema in {path}.")
    return data


def compare_engines(
    baseline_engine: str,
    candidate_engine: str,
    *,
    seed: int = DEFAULT_SEED,
    expectations_path: Path | None = None,
) -> dict[str, Any]:
    """Run both engines over identical M0 cases and compare frozen expectations."""
    expectations = load_expectations(expectations_path) if expectations_path else None
    expected_results = expectations.get("results", {}) if expectations else {}
    comparisons = []
    mismatches: list[str] = []
    if expectations and expectations.get("model") != assessment_model_identity():
        mismatches.append("model_identity")
    if expectations and expectations.get("seed") != seed:
        mismatches.append("seed")
    for case_name in CASE_NAMES:
        baseline = run_case(baseline_engine, case_name, seed=seed)
        candidate = run_case(candidate_engine, case_name, seed=seed)
        if baseline["input_sha256"] != candidate["input_sha256"]:
            raise AssertionError(f"Engines received different inputs for case {case_name}.")
        for role, result in (("baseline", baseline), ("candidate", candidate)):
            expected = expected_results.get(result["engine"], {}).get(case_name)
            result["frozen_expectation"] = expected
            result["matches_frozen_baseline"] = (
                None
                if expected is None
                else result["input_sha256"] == expected["input_sha256"]
                and result["input_coverage"] == expected["input_coverage"]
                and result["output_sha256"] == expected["output_sha256"]
                and result["summary"] == expected["summary"]
            )
            if result["matches_frozen_baseline"] is False:
                mismatches.append(f"{role}:{result['engine']}:{case_name}")
        comparisons.append(
            {
                "case": case_name,
                "identical_input_sha256": baseline["input_sha256"],
                "baseline": baseline,
                "candidate": candidate,
                "outputs_identical": baseline["output_sha256"] == candidate["output_sha256"],
            }
        )
    return {
        "schema": BASELINE_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "synthetic software verification; not field validation",
        "model": assessment_model_identity(),
        "baseline_engine": baseline_engine,
        "candidate_engine": candidate_engine,
        "seed": seed,
        "cases": comparisons,
        "frozen_baseline_checked": expectations is not None,
        "frozen_baseline_mismatches": mismatches,
        "all_checked_results_match": not mismatches,
    }


def frozen_results(*, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Return the deterministic portion used to create the checked-in M0 manifest."""
    results: dict[str, Any] = {}
    for engine_name in sorted(runout.ENGINES):
        results[engine_name] = {}
        for case_name in CASE_NAMES:
            measured = run_case(engine_name, case_name, seed=seed)
            results[engine_name][case_name] = {
                "input_sha256": measured["input_sha256"],
                "input_coverage": measured["input_coverage"],
                "output_sha256": measured["output_sha256"],
                "summary": measured["summary"],
            }
    return {
        "schema": BASELINE_SCHEMA,
        "purpose": "synthetic software verification; not field validation",
        "seed": seed,
        "model": assessment_model_identity(),
        "results": results,
    }
