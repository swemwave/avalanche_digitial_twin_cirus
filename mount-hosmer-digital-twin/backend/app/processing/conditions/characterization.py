"""Deterministic, non-activating M2 forcing characterization.

This offline-only module relates an explicit forcing reference elevation to an
internally validated terrain bake and characterizes existing station transfers.
It never changes a ConditionPack, selects a new forcing source, fills a missing
value, or activates a local correction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping

import numpy as np

from avycore.conditions import ConditionPack

from app.bake_identity import (
    BAKE_SCHEMA,
    REQUIRED_BAKED_LAYERS,
    BakeCompatibilityError,
    bake_sha256,
    processing_manifest,
    sha256_file,
    validate_bake,
)
from app.baked import Reprojector

from .eccc import (
    ECCCSnapshot,
    _read_hourly,
    _read_stations,
    _raw_value,
    _timeline,
    audit_eccc_snapshot,
)
from .pcic import compare_pcic_to_eccc
from .protocol import ConditionRequest


CHARACTERIZATION_SCHEMA = "m2-forcing-characterization-v1"
CHARACTERIZATION_CONTRACT_REVISION = "m2-forcing-characterization-v1.2"
SUPPORTED_CHARACTERIZATION_REVISIONS = frozenset(
    {"m2-forcing-characterization-v1.1", CHARACTERIZATION_CONTRACT_REVISION}
)
STORAGE_SCHEMA = "m2-forcing-characterization-storage-v1"
REPORT_FILENAME = "report.json"
CHECKSUMS_FILENAME = "checksums.json"
DISCLAIMER = (
    "Experimental research prototype only; not an operational avalanche forecast, not a "
    "probability, and never a replacement for Avalanche Canada guidance or field assessment."
)


class ForcingCharacterizationError(ValueError):
    """Raised when an input or stored characterization is ambiguous or corrupt."""


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _report_id(report_without_id: Mapping[str, Any]) -> str:
    return f"characterization-{_sha256_bytes(_canonical_json_bytes(dict(report_without_id)))}"


def scientific_series_sha256(pack: ConditionPack) -> str:
    """Hash normalized variables only, excluding code-lineage identity fields."""

    variables = pack.model_dump(mode="json")["variables"]
    content = json.dumps(
        variables,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(content)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _metrics(differences: list[float], unit: str) -> dict[str, Any]:
    return {
        "overlap_hours": len(differences),
        "unit": unit,
        "bias": fmean(differences) if differences else None,
        "mae": fmean(abs(value) for value in differences) if differences else None,
        "rmse": (
            math.sqrt(fmean(value * value for value in differences))
            if differences
            else None
        ),
    }


def _distribution(values: np.ndarray, unit: str) -> dict[str, Any]:
    """Return deterministic masked-array summary statistics without rounding."""

    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ForcingCharacterizationError("A characterized terrain layer has no finite values.")
    return {
        "unit": unit,
        "count": int(finite.size),
        "minimum": float(np.min(finite)),
        "p05": float(np.quantile(finite, 0.05)),
        "p25": float(np.quantile(finite, 0.25)),
        "median": float(np.median(finite)),
        "mean": float(np.mean(finite)),
        "p75": float(np.quantile(finite, 0.75)),
        "p95": float(np.quantile(finite, 0.95)),
        "maximum": float(np.max(finite)),
    }


def _masked_layer(root: Path, record: Mapping[str, Any]) -> np.ma.MaskedArray:
    raw = np.load(root / str(record["file"]), mmap_mode="r")
    nodata = record.get("nodata")
    if nodata in (None, "NaN"):
        return np.ma.masked_invalid(raw, copy=False)
    return np.ma.masked_equal(raw, nodata, copy=False)


def _invert_reproject_lattice(
    meta: Mapping[str, Any], target_longitude_deg: float, target_latitude_deg: float
) -> tuple[float, float, float]:
    """Invert the baked pixel-edge lattice without importing a CRS library."""

    from scipy.optimize import least_squares

    grid = meta["grid"]
    lattice = meta["reproject"]
    cols = np.asarray(lattice["cols"], dtype="float64")
    rows = np.asarray(lattice["rows"], dtype="float64")
    lon = np.asarray(lattice["lon"], dtype="float64")
    lat = np.asarray(lattice["lat"], dtype="float64")
    if lon.shape != (len(rows), len(cols)) or lat.shape != lon.shape:
        raise ForcingCharacterizationError("Baked reprojection lattice shape is inconsistent.")
    if not np.all(np.isfinite(lon)) or not np.all(np.isfinite(lat)):
        raise ForcingCharacterizationError("Baked reprojection lattice contains non-finite values.")
    scale_lon = math.cos(math.radians(target_latitude_deg))
    distance = ((lon - target_longitude_deg) * scale_lon) ** 2 + (
        lat - target_latitude_deg
    ) ** 2
    initial_row_index, initial_col_index = np.unravel_index(np.argmin(distance), distance.shape)
    projector = Reprojector(cols, rows, lon, lat)

    def residual(pixel: np.ndarray) -> np.ndarray:
        actual_lon, actual_lat = projector(float(pixel[0]), float(pixel[1]))
        return np.asarray(
            [
                (actual_lon - target_longitude_deg) * scale_lon,
                actual_lat - target_latitude_deg,
            ],
            dtype="float64",
        )

    solved = least_squares(
        residual,
        x0=np.asarray([cols[initial_col_index], rows[initial_row_index]]),
        bounds=(
            np.asarray([0.0, 0.0]),
            np.asarray([float(grid["width"]), float(grid["height"])]),
        ),
        xtol=1e-14,
        ftol=1e-14,
        gtol=1e-14,
        max_nfev=100,
    )
    if not solved.success:
        raise ForcingCharacterizationError("Could not invert the baked reprojection lattice.")
    col, row = (float(value) for value in solved.x)
    recovered_lon, recovered_lat = projector(col, row)
    error_m = _haversine_km(
        target_longitude_deg, target_latitude_deg, recovered_lon, recovered_lat
    ) * 1000.0
    if error_m > 0.05:
        raise ForcingCharacterizationError(
            f"Baked lattice inversion residual {error_m:.6f} m exceeds 0.05 m."
        )
    return col, row, error_m


def _cell_record(
    elevation: np.ma.MaskedArray,
    terrain_source: np.ma.MaskedArray,
    row: int,
    col: int,
    *,
    west: float,
    north: float,
    resolution_m: float,
    source_labels: Mapping[str, str],
) -> dict[str, Any]:
    elevation_mask = bool(np.ma.getmaskarray(elevation)[row, col])
    source_mask = bool(np.ma.getmaskarray(terrain_source)[row, col])
    source_code = None if source_mask else int(terrain_source[row, col])
    return {
        "row": row,
        "col": col,
        "center_easting_m": west + (col + 0.5) * resolution_m,
        "center_northing_m": north - (row + 0.5) * resolution_m,
        "elevation_m": None if elevation_mask else float(elevation[row, col]),
        "elevation_masked": elevation_mask,
        "terrain_source_code": source_code,
        "terrain_source_label": (
            None if source_code is None else source_labels.get(str(source_code), "unknown")
        ),
    }


def characterize_bake_reference(
    meta: Mapping[str, Any],
    elevation: np.ma.MaskedArray,
    terrain_source: np.ma.MaskedArray,
    *,
    target_longitude_deg: float,
    target_latitude_deg: float,
    existing_reference_elevation_m: float,
) -> dict[str, Any]:
    """Relate one lon/lat target to baked cells and non-activating alternatives."""

    grid = meta["grid"]
    width, height = int(grid["width"]), int(grid["height"])
    if elevation.shape != (height, width) or terrain_source.shape != (height, width):
        raise ForcingCharacterizationError("Terrain layers do not match the baked grid shape.")
    transform = tuple(float(value) for value in grid["transform"])
    if len(transform) != 6:
        raise ForcingCharacterizationError("Baked affine transform must have six coefficients.")
    a, b, west, d, e, north = transform
    resolution_m = float(grid["resolution_m"])
    if b != 0 or d != 0 or a <= 0 or e >= 0 or not math.isclose(a, resolution_m) or not math.isclose(-e, resolution_m):
        raise ForcingCharacterizationError("Only the documented north-up square baked grid is supported.")

    edge_col, edge_row, inversion_error_m = _invert_reproject_lattice(
        meta, target_longitude_deg, target_latitude_deg
    )
    if not (0 <= edge_col < width and 0 <= edge_row < height):
        raise ForcingCharacterizationError("Target coordinate lies outside the baked grid.")
    projected_x = a * edge_col + b * edge_row + west
    projected_y = d * edge_col + e * edge_row + north
    containing_col, containing_row = int(math.floor(edge_col)), int(math.floor(edge_row))
    nearest_col = min(width - 1, max(0, int(round(edge_col - 0.5))))
    nearest_row = min(height - 1, max(0, int(round(edge_row - 0.5))))
    labels = meta.get("terrain", {}).get("source_codes", {})
    containing = _cell_record(
        elevation,
        terrain_source,
        containing_row,
        containing_col,
        west=west,
        north=north,
        resolution_m=resolution_m,
        source_labels=labels,
    )
    nearest = _cell_record(
        elevation,
        terrain_source,
        nearest_row,
        nearest_col,
        west=west,
        north=north,
        resolution_m=resolution_m,
        source_labels=labels,
    )
    nearest["target_to_cell_center_distance_m"] = math.hypot(
        projected_x - nearest["center_easting_m"],
        projected_y - nearest["center_northing_m"],
    )

    centered_col, centered_row = edge_col - 0.5, edge_row - 0.5
    left, top = int(math.floor(centered_col)), int(math.floor(centered_row))
    footprint: list[dict[str, Any]] = []
    bilinear_value: float | None = None
    bilinear_status = "outside_full_four_cell_footprint"
    if 0 <= left < width - 1 and 0 <= top < height - 1:
        x_fraction, y_fraction = centered_col - left, centered_row - top
        weights = (
            (top, left, (1 - x_fraction) * (1 - y_fraction)),
            (top, left + 1, x_fraction * (1 - y_fraction)),
            (top + 1, left, (1 - x_fraction) * y_fraction),
            (top + 1, left + 1, x_fraction * y_fraction),
        )
        total = 0.0
        all_valid = True
        for row, col, weight in weights:
            record = _cell_record(
                elevation,
                terrain_source,
                row,
                col,
                west=west,
                north=north,
                resolution_m=resolution_m,
                source_labels=labels,
            )
            record["weight"] = float(weight)
            footprint.append(record)
            if record["elevation_m"] is None:
                all_valid = False
            else:
                total += float(weight) * record["elevation_m"]
        if all_valid:
            bilinear_value = total
            bilinear_status = "available_all_four_cells_valid"
        else:
            bilinear_status = "masked_one_or_more_required_cells"

    midpoint = _cell_record(
        elevation,
        terrain_source,
        height // 2,
        width // 2,
        west=west,
        north=north,
        resolution_m=resolution_m,
        source_labels=labels,
    )
    midpoint_match = midpoint["elevation_m"] is not None and math.isclose(
        round(midpoint["elevation_m"], 2),
        existing_reference_elevation_m,
        abs_tol=1e-12,
    )
    alternatives = {
        "containing_cell_center": containing,
        "nearest_cell_center": nearest,
        "four_cell_bilinear_at_target": {
            "status": bilinear_status,
            "elevation_m": bilinear_value,
            "footprint": footprint,
        },
    }
    for value in alternatives.values():
        if isinstance(value, dict) and value.get("elevation_m") is not None:
            value["difference_from_existing_m"] = (
                float(value["elevation_m"]) - existing_reference_elevation_m
            )
    return {
        "grid": {
            "crs": grid["crs"],
            "axis_order": "projected (easting_m, northing_m)",
            "raster_index_order": "array[row, col]",
            "resolution_m": resolution_m,
            "width": width,
            "height": height,
        },
        "target": {
            "geographic_axis_order": "(longitude_deg, latitude_deg)",
            "longitude_deg": target_longitude_deg,
            "latitude_deg": target_latitude_deg,
            "projected_easting_m": projected_x,
            "projected_northing_m": projected_y,
            "pixel_edge_col": edge_col,
            "pixel_edge_row": edge_row,
            "lattice_inversion_residual_m": inversion_error_m,
        },
        "existing_reference": {
            "elevation_m": existing_reference_elevation_m,
            "documented_derivation_found": False,
            "integer_array_midpoint_cell": midpoint,
            "matches_midpoint_rounded_to_0_01_m": bool(midpoint_match),
            "origin_finding": (
                "The value has no durable derivation record. It exactly matches the elevation "
                "at array[height//2, width//2] after rounding to 0.01 m; this reconstructs the "
                "likely sampling operation but is not source lineage proof."
            ),
        },
        "target_compatible_alternatives": alternatives,
        "activation": {
            "status": "not_activated",
            "reason": (
                "No alternative is silently selected. A separate versioned reference-elevation "
                "contract and documented migration decision are required; the bake-wide vertical "
                "datum remains unknown/mixed."
            ),
        },
    }


def characterize_temperature_and_precipitation(
    snapshot: ECCCSnapshot,
    request: ConditionRequest,
    *,
    target_longitude_deg: float,
    target_latitude_deg: float,
    existing_reference_elevation_m: float,
    alternative_elevations_m: Mapping[str, float | None],
    selected_station_id: str | None = None,
    lapse_rate_k_per_m: float = 0.0065,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not 0 < lapse_rate_k_per_m < 0.02:
        raise ForcingCharacterizationError("lapse_rate_k_per_m must be in (0, 0.02).")
    quality = audit_eccc_snapshot(
        snapshot,
        request,
        target_longitude_deg=target_longitude_deg,
        target_latitude_deg=target_latitude_deg,
        target_elevation_m=existing_reference_elevation_m,
        selected_station_id=selected_station_id,
    )
    selected_id = str(quality["selected_station_id"])
    withheld_id = quality["withheld_station_id"]
    stations = _read_stations(snapshot)
    records, duplicate_report = _read_hourly(
        snapshot, request.valid_start_utc, request.valid_end_utc
    )
    timeline = _timeline(request.valid_start_utc, request.valid_end_utc)
    selected = stations[selected_id]
    raw_differences: list[float] = []
    corrected_differences: list[float] = []
    if withheld_id is not None:
        withheld = stations[str(withheld_id)]
        for timestamp in timeline:
            left, _ = _raw_value(records[selected_id].get(timestamp), "air_temperature")
            right, _ = _raw_value(records[str(withheld_id)].get(timestamp), "air_temperature")
            if left is None or right is None:
                continue
            raw_differences.append(left - right)
            left_corrected = left - lapse_rate_k_per_m * (
                existing_reference_elevation_m - selected.elevation_m
            )
            right_corrected = right - lapse_rate_k_per_m * (
                existing_reference_elevation_m - withheld.elevation_m
            )
            corrected_differences.append(left_corrected - right_corrected)
        withheld_record: dict[str, Any] | None = {
            "station_id": withheld.climate_id,
            "name": withheld.name,
            "operator": withheld.operator,
            "elevation_m": withheld.elevation_m,
        }
    else:
        withheld_record = None
    uncorrected = _metrics(raw_differences, "degC")
    corrected = _metrics(corrected_differences, "K difference")
    lapse_rate_sweep: list[dict[str, Any]] = []
    # Minder et al. (2010) measured strong seasonal/diurnal variability in free-air
    # temperature lapse rates over Cascade terrain.  These endpoints are used only
    # as a bounded sensitivity diagnostic; they are not Mount Hosmer parameters.
    for rate_k_per_km in (2.5, 3.9, 5.2, 6.5, 7.5):
        rate = rate_k_per_km / 1000.0
        if withheld_id is None:
            differences: list[float] = []
        else:
            differences = [
                value + rate * (selected.elevation_m - withheld.elevation_m)
                for value in raw_differences
            ]
        lapse_rate_sweep.append(
            {
                "lapse_rate_k_per_km": rate_k_per_km,
                "station_disagreement": _metrics(differences, "K difference"),
            }
        )
    sensitivity = []
    for name, elevation_m in sorted(alternative_elevations_m.items()):
        by_lapse_rate = []
        for rate_k_per_km in (2.5, 3.9, 5.2, 6.5, 7.5):
            by_lapse_rate.append(
                {
                    "lapse_rate_k_per_km": rate_k_per_km,
                    "selected_temperature_change_k": (
                        None
                        if elevation_m is None
                        else -(rate_k_per_km / 1000.0)
                        * (elevation_m - existing_reference_elevation_m)
                    ),
                }
            )
        sensitivity.append(
            {
                "reference_name": name,
                "elevation_m": elevation_m,
                "elevation_change_from_existing_m": (
                    None if elevation_m is None else elevation_m - existing_reference_elevation_m
                ),
                "selected_temperature_change_k": (
                    None
                    if elevation_m is None
                    else -lapse_rate_k_per_m * (
                        elevation_m - existing_reference_elevation_m
                    )
                ),
                "by_lapse_rate": by_lapse_rate,
            }
        )
    temperature = {
        "method": {
            "status": "active_existing_behavior_unchanged",
            "equation": "T_target = T_station - lapse_rate * (z_target - z_station)",
            "lapse_rate_k_per_m": lapse_rate_k_per_m,
            "lapse_rate_k_per_km": lapse_rate_k_per_m * 1000.0,
            "sign_check": "A higher target elevation produces a lower corrected temperature.",
            "provider_bound": "strictly greater than 0 and less than 0.02 K/m",
            "uncertainty": "No site-specific lapse-rate uncertainty is quantified.",
            "sensitivity_source": {
                "citation": (
                    "Minder, Mote, and Lundquist (2010), Surface temperature lapse rates "
                    "over complex terrain: Lessons from the Cascade Mountains"
                ),
                "doi": "10.1029/2009JD013493",
                "use_boundary": (
                    "The 2.5-7.5 K/km range is a literature-supported sensitivity envelope, "
                    "not a local calibration, correction, or validation result."
                ),
            },
        },
        "selected_station": {
            "station_id": selected.climate_id,
            "name": selected.name,
            "operator": selected.operator,
            "elevation_m": selected.elevation_m,
        },
        "withheld_station": withheld_record,
        "uncorrected_station_disagreement": uncorrected,
        "both_stations_transferred_to_existing_reference_disagreement": corrected,
        "lapse_rate_sweep": lapse_rate_sweep,
        "target_elevation_invariance": (
            "When both stations are transferred with the same lapse rate to the same target, "
            "their difference is algebraically independent of the chosen target elevation; "
            "the target elevation changes absolute transferred temperature only."
        ),
        "disagreement_change": {
            "bias_change": (
                None
                if uncorrected["bias"] is None or corrected["bias"] is None
                else corrected["bias"] - uncorrected["bias"]
            ),
            "mae_change": (
                None
                if uncorrected["mae"] is None or corrected["mae"] is None
                else corrected["mae"] - uncorrected["mae"]
            ),
            "rmse_change": (
                None
                if uncorrected["rmse"] is None or corrected["rmse"] is None
                else corrected["rmse"] - uncorrected["rmse"]
            ),
            "claim_boundary": (
                "A change in same-provider station disagreement is characterization only, not "
                "validation, calibration, or evidence that the transfer is more accurate."
            ),
        },
        "reference_elevation_sensitivity": sensitivity,
    }

    precip_counts = Counter()
    element_flags = Counter()
    record_flags = Counter()
    values: list[float] = []
    for timestamp in timeline:
        row = records[selected_id].get(timestamp)
        raw, flag = _raw_value(row, "precipitation_amount")
        if flag:
            element_flags[flag] += 1
        if row is not None and (row.get("FLAG") or "").strip():
            record_flags[(row.get("FLAG") or "").strip()] += 1
        if raw is None:
            precip_counts["missing"] += 1
        elif raw < 0:
            precip_counts["negative"] += 1
            values.append(raw)
        elif raw == 0:
            precip_counts["zero"] += 1
            values.append(raw)
        else:
            precip_counts["positive"] += 1
            values.append(raw)
    precipitation = {
        "source_field": "PRECIP_AMOUNT",
        "source_unit": "mm per hour",
        "canonical_unit": "kg m-2 h-1",
        "time_semantics": "hourly amount at exact UTC hours; not a cumulative gauge series",
        "counts": dict(sorted(precip_counts.items())),
        "minimum_source_value": min(values) if values else None,
        "maximum_source_value": max(values) if values else None,
        "element_qc_flags": dict(sorted(element_flags.items())),
        "record_qc_flags": dict(sorted(record_flags.items())),
        "trace_handling": (
            "Provider flags are retained. No trace is silently converted, distributed, or filled."
        ),
        "phase": {
            "status": "derived_existing_behavior_unchanged",
            "semantics": "categorical phase derived from elevation-transferred air temperature",
            "direct_observation": False,
            "counts": {},
        },
        "accumulation_reset_handling": (
            "No cumulative precipitation series is used, so no reset correction is applied."
        ),
        "orographic_correction": "disabled_no_source_supported_parameters_or_uncertainty",
        "gauge_undercatch_correction": "disabled_no_instrument_specific_parameters_or_uncertainty",
        "claim_boundary": "Station hourly amounts are not Mount Hosmer precipitation truth.",
    }
    station_audit = {
        "quality_schema": quality["schema"],
        "selected_station_id": selected_id,
        "withheld_station_id": withheld_id,
        "duplicate_records": duplicate_report,
        "gap_fill_fraction": quality["gap_fill_fraction"],
    }
    return temperature, precipitation, station_audit


def _station_terrain_record(
    elevation_values: np.ndarray,
    *,
    station: Mapping[str, Any],
    target_longitude_deg: float,
    target_latitude_deg: float,
    reference_elevation_m: float,
) -> dict[str, Any]:
    station_elevation = float(station["elevation_m"])
    return {
        "station_id": station["station_id"],
        "name": station["name"],
        "longitude_deg": float(station["longitude_deg"]),
        "latitude_deg": float(station["latitude_deg"]),
        "elevation_m": station_elevation,
        "horizontal_distance_km": _haversine_km(
            target_longitude_deg,
            target_latitude_deg,
            float(station["longitude_deg"]),
            float(station["latitude_deg"]),
        ),
        "elevation_difference_from_reference_m": station_elevation
        - reference_elevation_m,
        "terrain_at_or_below_station_fraction": float(
            np.count_nonzero(elevation_values <= station_elevation) / elevation_values.size
        ),
        "terrain_above_station_fraction": float(
            np.count_nonzero(elevation_values > station_elevation) / elevation_values.size
        ),
        "terrain_within_100m_fraction": float(
            np.count_nonzero(np.abs(elevation_values - station_elevation) <= 100.0)
            / elevation_values.size
        ),
        "terrain_within_250m_fraction": float(
            np.count_nonzero(np.abs(elevation_values - station_elevation) <= 250.0)
            / elevation_values.size
        ),
        "terrain_within_500m_fraction": float(
            np.count_nonzero(np.abs(elevation_values - station_elevation) <= 500.0)
            / elevation_values.size
        ),
    }


def _exact_value_map(pack: ConditionPack, variable: str) -> dict[Any, float]:
    values: dict[Any, float] = {}
    for item in pack.variables[variable].values:  # type: ignore[index]
        if item.masked or item.value is None:
            continue
        values[item.time_utc] = float(item.value)
    return values


def _calm_direction_sensitivity(
    eccc_pack: ConditionPack, pcic_pack: ConditionPack
) -> list[dict[str, Any]]:
    """Compare exact-time direction only when both speeds exceed a calm threshold."""

    e_speed = _exact_value_map(eccc_pack, "wind_speed")
    p_speed = _exact_value_map(pcic_pack, "wind_speed")
    e_direction = _exact_value_map(eccc_pack, "wind_direction")
    p_direction = _exact_value_map(pcic_pack, "wind_direction")
    speed_times = sorted(set(e_speed) & set(p_speed))
    result: list[dict[str, Any]] = []
    for threshold in (0.0, 0.5, 1.0, 2.0):
        eligible = [
            timestamp
            for timestamp in speed_times
            if e_speed[timestamp] > threshold and p_speed[timestamp] > threshold
        ]
        paired = [timestamp for timestamp in eligible if timestamp in e_direction and timestamp in p_direction]
        differences = [
            ((e_direction[timestamp] - p_direction[timestamp] + 180.0) % 360.0) - 180.0
            for timestamp in paired
        ]
        if differences:
            radians = np.radians(np.asarray(differences, dtype="float64"))
            circular_bias = math.degrees(
                math.atan2(float(np.mean(np.sin(radians))), float(np.mean(np.cos(radians))))
            )
            mae = float(np.mean(np.abs(differences)))
            rmse = float(np.sqrt(np.mean(np.square(differences))))
        else:
            circular_bias = mae = rmse = None
        result.append(
            {
                "calm_threshold_m_s": threshold,
                "exact_speed_pairs": len(speed_times),
                "masked_by_calm_threshold": len(speed_times) - len(eligible),
                "eligible_speed_pairs": len(eligible),
                "exact_direction_pairs": len(paired),
                "direction_missing_after_calm_mask": len(eligible) - len(paired),
                "circular_bias_deg": circular_bias,
                "circular_mae_deg": mae,
                "circular_rmse_deg": rmse,
            }
        )
    return result


def _terrain_characterization(
    root: Path,
    meta: Mapping[str, Any],
    layer_records: Mapping[str, Mapping[str, Any]],
    bake_reference: Mapping[str, Any],
) -> dict[str, Any]:
    layers = {name: _masked_layer(root, layer_records[name]) for name in REQUIRED_BAKED_LAYERS}
    masks = {name: np.ma.getmaskarray(layer) for name, layer in layers.items()}
    common_mask = np.logical_or.reduce(tuple(masks.values()))
    common_count = int(common_mask.size - np.count_nonzero(common_mask))
    if common_count == 0:
        raise ForcingCharacterizationError("No cells have all terrain characterization inputs.")

    elevation_values = np.asarray(layers["elevation"][~masks["elevation"]], dtype="float64")
    slope_values = np.asarray(layers["slope"][~masks["slope"]], dtype="float64")
    aspect_values = np.asarray(layers["aspect"][~masks["aspect"]], dtype="float64")
    forest_values = np.asarray(layers["forest_mask"][~masks["forest_mask"]], dtype="float64")
    if np.any((forest_values != 0.0) & (forest_values != 1.0)):
        raise ForcingCharacterizationError("Forest mask contains values outside {0,1}.")

    slope_bins = ((0.0, 15.0), (15.0, 25.0), (25.0, 30.0), (30.0, 35.0),
                  (35.0, 40.0), (40.0, 45.0), (45.0, 60.0), (60.0, 90.0))
    slope_histogram = []
    for lower, upper in slope_bins:
        include_upper = upper == 90.0
        count = int(np.count_nonzero((slope_values >= lower) & ((slope_values <= upper) if include_upper else (slope_values < upper))))
        slope_histogram.append(
            {"lower_deg": lower, "upper_deg": upper, "upper_inclusive": include_upper,
             "count": count, "fraction": count / slope_values.size}
        )

    sector_names = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    sectors = ((np.floor((aspect_values + 22.5) / 45.0).astype("int64")) % 8)
    aspect_sectors = [
        {"sector": name, "count": int(np.count_nonzero(sectors == index)),
         "fraction": float(np.count_nonzero(sectors == index) / sectors.size)}
        for index, name in enumerate(sector_names)
    ]

    categorical: dict[str, Any] = {}
    for layer_name, metadata_name in (("terrain_source", "terrain"), ("forest_source", "forest")):
        array = layers[layer_name]
        values = np.asarray(array[~masks[layer_name]], dtype="int64")
        labels = meta.get(metadata_name, {}).get("source_codes", {})
        categorical[layer_name] = [
            {
                "code": int(code),
                "label": str(labels.get(str(int(code)), "unknown")),
                "count": int(np.count_nonzero(values == code)),
                "fraction": float(np.count_nonzero(values == code) / values.size),
            }
            for code in sorted(np.unique(values))
        ]

    footprint_rows = bake_reference["target_compatible_alternatives"][
        "four_cell_bilinear_at_target"
    ]["footprint"]
    footprint: list[dict[str, Any]] = []
    for cell in footprint_rows:
        row, col = int(cell["row"]), int(cell["col"])
        record: dict[str, Any] = {"row": row, "col": col, "weight": float(cell["weight"])}
        for name in REQUIRED_BAKED_LAYERS:
            record[name] = None if masks[name][row, col] else float(layers[name][row, col])
        footprint.append(record)

    target_summary: dict[str, Any] = {"cells": footprint, "continuous_weighted": {}}
    if footprint and all(
        cell[name] is not None
        for cell in footprint
        for name in ("elevation", "slope", "forest_mask", "plan_curvature", "general_curvature")
    ):
        for name in ("elevation", "slope", "forest_mask", "plan_curvature", "general_curvature"):
            target_summary["continuous_weighted"][name] = sum(
                cell["weight"] * cell[name] for cell in footprint
            )

    return {
        "layer_file_sha256": {name: layer_records[name]["sha256"] for name in REQUIRED_BAKED_LAYERS},
        "per_layer_valid_and_masked": {
            name: {
                "valid_count": int(mask.size - np.count_nonzero(mask)),
                "masked_count": int(np.count_nonzero(mask)),
            }
            for name, mask in masks.items()
        },
        "all_layers_valid_count": common_count,
        "all_layers_masked_union_count": int(np.count_nonzero(common_mask)),
        "elevation": _distribution(elevation_values, "m"),
        "slope": _distribution(slope_values, "degree"),
        "slope_bins": slope_histogram,
        "aspect": {
            "unit": "degree_clockwise_from_true_north",
            "sector_rule": "eight 45-degree sectors centered on cardinal/intercardinal bearings",
            "sectors": aspect_sectors,
            "flat_cell_limitation": (
                "Aspect is numerically reported for all derivative-valid cells; no unsupported "
                "flat-slope threshold is used to convert it to missing."
            ),
        },
        "forest": {
            "semantics": "binary baked forest mask, not canopy height or density",
            "forested_count": int(np.count_nonzero(forest_values == 1.0)),
            "open_count": int(np.count_nonzero(forest_values == 0.0)),
            "forested_fraction": float(np.mean(forest_values)),
        },
        "plan_curvature": _distribution(
            np.asarray(layers["plan_curvature"][~masks["plan_curvature"]]), "baked_derivative_unit"
        ),
        "general_curvature": _distribution(
            np.asarray(layers["general_curvature"][~masks["general_curvature"]]), "baked_derivative_unit"
        ),
        "source_coverage": categorical,
        "target_interpolation_footprint": target_summary,
        "exposure_metric_status": (
            "unavailable; curvature, slope, and aspect are terrain-shape metrics and are not "
            "relabelled as atmospheric exposure"
        ),
        "canopy_metric_status": (
            "only a binary forest mask is available; canopy height, density, and interception "
            "are unavailable"
        ),
        "vertical_datum_limitation": (
            "The bake-wide vertical datum remains unknown/mixed. The target footprint is LiDAR, "
            "but no vertical transform identity is encoded in the source GeoTIFF lineage."
        ),
    }


def _validate_pack_reference(pack: ConditionPack, expected_elevation_m: float) -> None:
    transformations = {
        item.transformation_id: item for item in pack.transformations
    }
    transfer = transformations.get("temperature-elevation-v1")
    if transfer is None:
        raise ForcingCharacterizationError(
            "ECCC ConditionPack lacks temperature-elevation-v1 lineage."
        )
    recorded = float(transfer.parameters["target_elevation_m"])
    if not math.isclose(recorded, expected_elevation_m, abs_tol=1e-9):
        raise ForcingCharacterizationError(
            "ECCC ConditionPack target elevation conflicts with the characterization reference."
        )


def _load_characterization_bake(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify historical bake integrity even when it predates the current full contract.

    A historical artifact is never reported as current-contract compatible.  This
    narrower verification exists only so its immutable arrays can be characterized
    without rebuilding terrain or bypassing checksums.
    """

    try:
        meta = validate_bake(root, expected_processing_sha256=None)
        return meta, {
            "current_contract_valid": True,
            "current_contract_error": None,
            "current_fingerprint_matches_recorded_identity": True,
            "current_fingerprint_sha256": bake_sha256(meta),
        }
    except BakeCompatibilityError as exc:
        contract_error = str(exc)
    meta_path = root / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ForcingCharacterizationError(f"Historical bake metadata is unreadable: {exc}") from exc
    if meta.get("schema") != BAKE_SCHEMA:
        raise ForcingCharacterizationError("Historical bake schema is unsupported.")
    current_fingerprint_sha256 = bake_sha256(meta)
    fingerprint_matches = (
        meta.get("identity", {}).get("bake_sha256") == current_fingerprint_sha256
    )
    records = {item.get("name"): item for item in meta.get("layers", [])}
    missing = sorted(set(REQUIRED_BAKED_LAYERS) - set(records))
    if missing:
        raise ForcingCharacterizationError(
            f"Historical bake is missing required layers: {', '.join(missing)}"
        )
    resolved_root = root.resolve()
    for name in REQUIRED_BAKED_LAYERS:
        record = records[name]
        path = (root / str(record.get("file", ""))).resolve()
        if not path.is_relative_to(resolved_root) or not path.is_file():
            raise ForcingCharacterizationError(f"Historical baked layer {name!r} is unsafe or missing.")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise ForcingCharacterizationError(
                f"Historical baked layer {name!r} has an unexpected size."
            )
        expected_sha = record.get("sha256")
        if not expected_sha or sha256_file(path) != expected_sha:
            raise ForcingCharacterizationError(
                f"Historical baked layer {name!r} failed its SHA-256 check."
            )
    return meta, {
        "current_contract_valid": False,
        "current_contract_error": contract_error,
        "current_fingerprint_matches_recorded_identity": fingerprint_matches,
        "current_fingerprint_sha256": current_fingerprint_sha256,
    }


def build_m2_forcing_characterization(
    *,
    project_root: str | Path,
    bake_root: str | Path,
    eccc_snapshot: ECCCSnapshot,
    eccc_pack: ConditionPack,
    pcic_pack: ConditionPack,
    request: ConditionRequest,
    target_longitude_deg: float,
    target_latitude_deg: float,
    existing_reference_elevation_m: float,
    selected_eccc_station_id: str | None = None,
    lapse_rate_k_per_m: float = 0.0065,
) -> dict[str, Any]:
    """Build one deterministic report; all candidate corrections remain non-activating."""

    root = Path(bake_root).resolve()
    meta, contract_audit = _load_characterization_bake(root)
    layer_records = {item["name"]: item for item in meta["layers"]}
    elevation = _masked_layer(root, layer_records["elevation"])
    terrain_source = _masked_layer(root, layer_records["terrain_source"])
    bake_reference = characterize_bake_reference(
        meta,
        elevation,
        terrain_source,
        target_longitude_deg=target_longitude_deg,
        target_latitude_deg=target_latitude_deg,
        existing_reference_elevation_m=existing_reference_elevation_m,
    )
    checkout_processing = processing_manifest(Path(project_root).resolve())["sha256"]
    recorded_processing = str(meta["processing"]["sha256"])
    bake_reference["bake_identity"] = {
        "bake_sha256": meta["identity"]["bake_sha256"],
        "elevation_layer_sha256": layer_records["elevation"]["sha256"],
        "terrain_source_layer_sha256": layer_records["terrain_source"]["sha256"],
        "characterization_integrity_validated": True,
        "current_bake_contract_valid": contract_audit["current_contract_valid"],
        "current_bake_contract_error": contract_audit["current_contract_error"],
        "current_fingerprint_matches_recorded_identity": contract_audit[
            "current_fingerprint_matches_recorded_identity"
        ],
        "current_fingerprint_sha256": contract_audit["current_fingerprint_sha256"],
        "embedded_processing_sha256": recorded_processing,
        "checkout_processing_sha256": checkout_processing,
        "checkout_processing_compatible": recorded_processing == checkout_processing,
        "vertical_datum": "unknown_not_recorded_in_bake_metadata",
    }
    alternatives = bake_reference["target_compatible_alternatives"]
    alternative_elevations = {
        name: value.get("elevation_m") for name, value in alternatives.items()
    }
    _validate_pack_reference(eccc_pack, existing_reference_elevation_m)
    temperature, precipitation, eccc_station_audit = characterize_temperature_and_precipitation(
        eccc_snapshot,
        request,
        target_longitude_deg=target_longitude_deg,
        target_latitude_deg=target_latitude_deg,
        existing_reference_elevation_m=existing_reference_elevation_m,
        alternative_elevations_m=alternative_elevations,
        selected_station_id=selected_eccc_station_id,
        lapse_rate_k_per_m=lapse_rate_k_per_m,
    )
    phase_counts = Counter(
        "missing" if value.masked or value.value is None else str(value.value)
        for value in eccc_pack.variables["precipitation_phase"].values
    )
    precipitation["phase"]["counts"] = dict(sorted(phase_counts.items()))
    disagreement = compare_pcic_to_eccc(
        pcic_pack,
        eccc_pack,
        target_longitude_deg=target_longitude_deg,
        target_latitude_deg=target_latitude_deg,
        target_elevation_m=existing_reference_elevation_m,
        eccc_original_organization=temperature["selected_station"]["operator"],
    )
    wind = {
        "status": "observed_station_disagreement_existing_behavior_unchanged",
        "eccc_minus_pcic": {
            name: disagreement["variables"][name]
            for name in ("wind_speed", "wind_direction")
        },
        "direction_method": "shortest signed angular difference with circular-mean bias",
        "calm_direction": "masked by both normalizers when their source speed indicates calm",
        "calm_threshold_sensitivity": _calm_direction_sensitivity(eccc_pack, pcic_pack),
        "sensor_height_m": {"eccc": None, "pcic": None},
        "exposure_and_configuration_history": {"eccc": None, "pcic": None},
        "terrain_speed_up_or_exposure_correction": "disabled",
        "claim_boundary": (
            "Valley/community observations do not establish terrain-scale ridge speed or direction."
        ),
    }

    valid_elevation = np.asarray(elevation.compressed(), dtype="float64")
    if valid_elevation.size == 0:
        raise ForcingCharacterizationError("Baked elevation contains no valid terrain cells.")
    station_records = []
    for pack in (eccc_pack, pcic_pack):
        for station in pack.stations:
            station_records.append(
                _station_terrain_record(
                    valid_elevation,
                    station={
                        "station_id": station.station_id,
                        "name": station.name,
                        "longitude_deg": station.longitude_deg,
                        "latitude_deg": station.latitude_deg,
                        "elevation_m": station.elevation_m,
                    },
                    target_longitude_deg=target_longitude_deg,
                    target_latitude_deg=target_latitude_deg,
                    reference_elevation_m=existing_reference_elevation_m,
                )
            )
    terrain_layers = _terrain_characterization(root, meta, layer_records, bake_reference)
    terrain_representativeness = {
        "valid_cell_count": int(valid_elevation.size),
        "masked_cell_count": int(elevation.size - valid_elevation.size),
        "elevation_m": {
            "minimum": float(np.min(valid_elevation)),
            "p05": float(np.quantile(valid_elevation, 0.05)),
            "p25": float(np.quantile(valid_elevation, 0.25)),
            "median": float(np.median(valid_elevation)),
            "mean": float(np.mean(valid_elevation)),
            "p75": float(np.quantile(valid_elevation, 0.75)),
            "p95": float(np.quantile(valid_elevation, 0.95)),
            "maximum": float(np.max(valid_elevation)),
        },
        "stations": station_records,
        "baked_layer_characterization": terrain_layers,
        "limitations": (
            "Elevation fractions quantify only vertical separation over the baked AOI. They do "
            "not resolve exposure, cold-air pooling, ridge acceleration, precipitation gradients, "
            "radiation, canopy, or atmospheric-profile differences."
        ),
    }

    report_without_id: dict[str, Any] = {
        "schema": CHARACTERIZATION_SCHEMA,
        "characterization_contract_revision": CHARACTERIZATION_CONTRACT_REVISION,
        "disclaimer": DISCLAIMER,
        "valid_start_utc": request.valid_start_utc.isoformat(),
        "valid_end_utc": request.valid_end_utc.isoformat(),
        "target": {
            "longitude_deg": target_longitude_deg,
            "latitude_deg": target_latitude_deg,
            "existing_reference_elevation_m": existing_reference_elevation_m,
        },
        "lineage": {
            "eccc_snapshot_id": eccc_snapshot.snapshot_id,
            "eccc_source_files": eccc_snapshot.manifest["files"],
            "eccc_condition_id": eccc_pack.condition_id,
            "eccc_normalized_output_sha256": eccc_pack.normalized_output_sha256,
            "eccc_scientific_series_sha256": scientific_series_sha256(eccc_pack),
            "eccc_source": eccc_pack.source.model_dump(mode="json"),
            "eccc_condition_source_files": [
                item.model_dump(mode="json") for item in eccc_pack.source_files
            ],
            "pcic_condition_id": pcic_pack.condition_id,
            "pcic_normalized_output_sha256": pcic_pack.normalized_output_sha256,
            "pcic_scientific_series_sha256": scientific_series_sha256(pcic_pack),
            "pcic_source": pcic_pack.source.model_dump(mode="json"),
            "pcic_condition_source_files": [
                item.model_dump(mode="json") for item in pcic_pack.source_files
            ],
            "mountain_grid": request.mountain_grid.model_dump(mode="json"),
            "bake_sha256": meta["identity"]["bake_sha256"],
            "characterization_code_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        },
        "bake_reference_elevation": bake_reference,
        "temperature_correction": temperature,
        "precipitation_characterization": precipitation,
        "wind_characterization": wind,
        "eccc_station_audit": eccc_station_audit,
        "snow_depth_swe": {
            "status": "blocked_identity_history_qc_revision_semantics_not_proven",
            "direct_dataset": {
                "dataset_record_id": "3a34bdd1-61b2-4687-8b55-c5db5e13ff50",
                "title": "Current Season Automated Snow Weather Station Data",
                "licence": "Open Government Licence - British Columbia 2.0",
                "licence_uri": (
                    "https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/"
                    "open-data/open-government-licence-bc"
                ),
                "dataset_record_uri": (
                    "https://catalogue.data.gov.bc.ca/dataset/"
                    "3a34bdd1-61b2-4687-8b55-c5db5e13ff50"
                ),
                "swe_resource_id": "fe591e21-7ffd-45f4-b3b3-2291e4a6de15",
                "snow_depth_resource_id": "abba1811-dd9a-4447-a297-2b5f81410abd",
                "swe_resource_uri": (
                    "https://www.env.gov.bc.ca/wsd/data_searches/snow/asws/data/SW.csv"
                ),
                "swe_source_unit": "mm snow water equivalent",
                "time_semantics": "hourly rows labelled DATE(UTC)",
            },
            "candidate": {
                "original_organization": "BC Hydro",
                "network": "B.C. Automated Snow Weather Stations",
                "station_id": "2C09Q",
                "name": "Morrissey Ridge",
                "current_operator": "BC Hydro",
                "current_longitude_deg": -114.975,
                "current_latitude_deg": 49.447222,
                "current_elevation_m": 1860.0,
                "conflicting_bchydro_crosswalk": (
                    "BC Hydro's current station table maps MOR to 2C09P, not 2C09Q."
                ),
                "provincial_current_location_records": {
                    "2c09p": {
                        "active": False,
                        "longitude_deg": -114.973675,
                        "latitude_deg": 49.447028,
                        "elevation_m": 1860.0,
                    },
                    "2c09q": {
                        "active": True,
                        "longitude_deg": -114.975,
                        "latitude_deg": 49.447222,
                        "elevation_m": 1860.0,
                    },
                    "contract_limit": (
                        "The current-location feature service exposes no date-effective move or "
                        "configuration fields."
                    ),
                    "feature_service_uri": (
                        "https://openmaps.gov.bc.ca/geo/pub/ows?service=WFS&request=GetFeature&"
                        "typeName=WHSE_WATER_MANAGEMENT.SNOW_ASWS_STATIONS_SP&outputFormat="
                        "application/json"
                    ),
                },
                "separate_pcic_histories": {
                    "mor": {
                        "station_id": 2482,
                        "history_id": 2885,
                        "longitude_deg": -114.975,
                        "latitude_deg": 49.44722222,
                        "elevation_m": 1860.0,
                        "overall_period": "1983-10-07/2026-08-04",
                        "swe_period": "2021-04-03/2026-08-04",
                        "swe_semantics": "Snow_WE; point; mm",
                    },
                    "2c09p": {
                        "station_id": 2547,
                        "history_id": 2950,
                        "longitude_deg": -114.967,
                        "latitude_deg": 49.45,
                        "elevation_m": 1860.0,
                        "overall_period": "1979-10-15/1983-05-25",
                        "available_semantics": "snowfall; time:sum; cm",
                    },
                    "2c09q": {
                        "station_id": 2548,
                        "history_id": 2951,
                        "longitude_deg": -114.967,
                        "latitude_deg": 49.45,
                        "elevation_m": 1800.0,
                        "overall_period": "1983-10-07/2020-09-30",
                        "swe_semantics": [
                            "Snow Water Equivalent; point; mm; 1995-10-01/2020-09-30",
                            "Snow Water Equivalent; time:sum; mm; 2003-12-09/2020-09-30",
                        ],
                    },
                    "metadata_uri_template": (
                        "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
                        "stations/{station_id}"
                    ),
                },
                "station_location_dataset_record_id": (
                    "ebe546aa-ac34-491c-a828-fdc87fb70610"
                ),
                "station_location_dataset_record_uri": (
                    "https://catalogue.data.gov.bc.ca/dataset/"
                    "ebe546aa-ac34-491c-a828-fdc87fb70610"
                ),
                "pcic_station_metadata_uri": (
                    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
                    "stations/2482"
                ),
                "bc_hydro_station_table_uri": (
                    "https://www.bchydro.com/energy-in-bc/operations/"
                    "transmission-reservoir-data/hydrometeorologic-data.html"
                ),
            },
            "full_observation_downloaded_or_cached": False,
            "observation_values_used": False,
            "incidental_live_feed_response_encountered": True,
            "incidental_response_disposition": (
                "A BC Hydro station link unexpectedly returned a live current-value text response. "
                "It was not saved, normalized, joined, or used; later requests were metadata-only."
            ),
            "audit_as_of_utc_date": "2026-08-09",
            "comparison_metrics": None,
            "qc_revision_contract": {
                "status": "not_proven",
                "evidence": (
                    "The provincial catalogue documents an hourly near-real-time resource and warns "
                    "that published values may differ from approved or corrected records, but its "
                    "resource metadata does not define defensible per-value QC flags, revision IDs, "
                    "or a stable correction history."
                ),
            },
            "date_effective_identity_contract": "not_found_in_authoritative_metadata",
            "blocker": (
                "The original operator's current MOR-to-2C09P mapping conflicts with a "
                "MOR-to-2C09Q crosswalk, while provincial and PCIC records expose multiple "
                "elevations and separate histories without date-effective move/configuration "
                "boundaries. Current per-value QC and revision semantics are also undocumented. "
                "Merging or normalizing these records would silently assume identity and meaning."
            ),
        },
        "radiation": {
            "audit_as_of_utc_date": "2026-08-09",
            "pcic_metadata_documentation_uri": (
                "https://services.pacificclimate.org/portal/docs/mdp/root.html"
            ),
            "shortwave_observed": "unavailable_from_eligible_representative_exact_winter_history",
            "longwave_observed": "unavailable_from_eligible_representative_exact_winter_history",
            "nearest_partial_shortwave_history": {
                "station": "FLNRO-FERN Canoe Mountain station 12057 history 14113",
                "source_variable_semantics": "incoming shortwave W m-2 time:mean",
                "metadata_end_date": "2026-01-05",
                "approximate_target_distance_km": 451.0,
                "eligible": False,
                "reasons": ["partial winter only", "not terrain-representative"],
                "station_metadata_uri": (
                    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
                    "stations/12057"
                ),
            },
            "pcic_catalog_audit": {
                "target_window": "2025-11-01T00:00:00Z/2026-05-31T23:00:00Z",
                "required_components": [
                    "downwelling shortwave radiation",
                    "downwelling longwave radiation",
                ],
                "exact_window_history_count": 0,
                "exact_window_both_components_history_count": 0,
                "historical_both_components_history_count": 5,
                "latest_historical_both_components_end_date": "2020-07-01T00:00:00",
                "variables_metadata_uri": (
                    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
                    "variables"
                ),
                "stations_metadata_uri": (
                    "https://services.pacificclimate.org/met-data-portal-pcds/api/metadata/"
                    "stations"
                ),
                "eligibility": "none",
            },
            "eccc_archive_audit": {
                "main_hourly_collection": (
                    "The documented main hourly collection does not expose shortwave or longwave "
                    "radiation variables."
                ),
                "specialized_archive": (
                    "The HLY11 archive format defines global solar radiation element 061 and "
                    "incident longwave element 169 in 0.001 MJ m-2."
                ),
                "interval_semantics": (
                    "HLY11 radiation hour labels identify the beginning of a local-apparent-solar-"
                    "time interval; they are not interchangeable with UTC interval means."
                ),
                "representative_exact_window_station_found": False,
                "technical_documentation_uri": (
                    "https://climate.weather.gc.ca/doc/Technical_Documentation.pdf"
                ),
            },
            "era5_land_gap_fill_candidate": {
                "status": "not_acquired_or_scientifically_evaluated_access_blocked",
                "product": "Copernicus Climate Data Store ERA5-Land hourly time-series data",
                "variables": [
                    "surface_solar_radiation_downwards",
                    "surface_thermal_radiation_downwards",
                ],
                "source_unit": "J m-2",
                "grid": "0.1 degree regridded product; approximately 9 km native resolution",
                "catalogue_exact_window_available": True,
                "licence": "Creative Commons Attribution 4.0",
                "redistribution": "permitted with attribution and required source notice",
                "interval_contract_required": (
                    "The time-series guide labels radiation de-accumulated while the parameter "
                    "reference uses accumulated-energy units. A product-specific adapter must prove "
                    "the returned interval semantics and convert interval energy explicitly; it may "
                    "not relabel J m-2 as W m-2."
                ),
                "scientific_limit": (
                    "Modelled reanalysis at this scale is neither an observed Mount Hosmer series "
                    "nor terrain-scale validation truth."
                ),
                "access_blocker": (
                    "CDS requires a user account, personal API token, and manual acceptance of the "
                    "dataset terms. No token or accepted entitlement is available in this project "
                    "environment, and the project cannot create or accept them on a user's behalf."
                ),
                "dataset_uri": (
                    "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-timeseries"
                ),
                "api_access_uri": "https://cds.climate.copernicus.eu/how-to-api",
            },
            "derived_shortwave": "blocked_missing_site_supported_atmospheric_and_cloud_inputs",
            "derived_longwave": "blocked_missing_all_sky_cloud_and_target_humidity_inputs",
            "solar_geometry": (
                "method-supported and separable, but geometry alone is not irradiance and is not "
                "written into either radiation variable"
            ),
            "method_sources": {
                "shortwave": "FAO-56 Chapter 3; daily empirical methods are not hourly observations",
                "clear_sky_longwave": "Prata 1996 and Brutsaert 1975; not all-sky forcing",
                "solar_position": "NREL Solar Position Algorithm; geometry only",
            },
            "missing_dependencies": (
                "No eligible local direct series; no complete cloud/atmospheric inputs and "
                "site-characterized parameters for an interval-aware published derivation."
            ),
            "unit_and_interval_boundary": (
                "Instantaneous or interval-mean W m-2 must not be equated with accumulated "
                "MJ m-2 without explicit interval integration. Night remains physical missing/zero "
                "according to a source or method, never an invented replacement."
            ),
            "activation": "none_missing_values_remain_masked",
        },
        "terrain_representativeness": terrain_representativeness,
        "correction_activation": {
            "temperature": "existing_fixed_lapse_transfer_unchanged",
            "precipitation_orographic": "disabled",
            "precipitation_gauge_undercatch": "disabled",
            "wind_speed_up_or_exposure": "disabled",
            "radiation_derivation": "disabled",
            "gap_fill": "none",
        },
        "claim_boundary": (
            "Software and station-disagreement characterization only. This report is not field "
            "validation, calibration, current conditions, probability, operational forecasting, "
            "or evidence of improved accuracy."
        ),
    }
    report = {**report_without_id, "report_id": _report_id(report_without_id)}
    return validate_characterization_report(report)


def validate_characterization_report(report: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(report)
    expected_keys = {
        "schema",
        "report_id",
        "disclaimer",
        "valid_start_utc",
        "valid_end_utc",
        "target",
        "lineage",
        "bake_reference_elevation",
        "temperature_correction",
        "precipitation_characterization",
        "wind_characterization",
        "eccc_station_audit",
        "snow_depth_swe",
        "radiation",
        "terrain_representativeness",
        "correction_activation",
        "claim_boundary",
    }
    actual_keys = set(value)
    if value.get("characterization_contract_revision") is not None:
        expected_keys.add("characterization_contract_revision")
    if actual_keys != expected_keys or value.get("schema") != CHARACTERIZATION_SCHEMA:
        raise ForcingCharacterizationError(
            "Characterization report has an unsupported schema or unexpected fields."
        )
    report_id = value.pop("report_id")
    expected_id = _report_id(value)
    value["report_id"] = report_id
    if report_id != expected_id:
        raise ForcingCharacterizationError(
            "Characterization report identity does not match its canonical content."
        )
    if value["disclaimer"] != DISCLAIMER:
        raise ForcingCharacterizationError("Characterization disclaimer is missing or changed.")
    if value.get("characterization_contract_revision") is not None:
        _validate_characterization_revision_1_1(value)
    return value


def _validate_characterization_revision_1_1(value: Mapping[str, Any]) -> None:
    """Strict scientific invariants for reports produced under revisions 1.1 and 1.2.

    Revision-less v1 reports remain loadable historical evidence.  New reports
    opt into this validation and cannot use the old empty-section fixture shape.
    """

    revision = value.get("characterization_contract_revision")
    if revision not in SUPPORTED_CHARACTERIZATION_REVISIONS:
        raise ForcingCharacterizationError("Unsupported characterization contract revision.")
    required_mappings = (
        "target", "lineage", "bake_reference_elevation", "temperature_correction",
        "precipitation_characterization", "wind_characterization", "eccc_station_audit",
        "snow_depth_swe", "radiation", "terrain_representativeness", "correction_activation",
    )
    for name in required_mappings:
        if not isinstance(value.get(name), Mapping) or not value[name]:
            raise ForcingCharacterizationError(
                f"Characterization section {name!r} is empty or malformed."
            )
    try:
        start = str(value["valid_start_utc"])
        end = str(value["valid_end_utc"])
        if "+00:00" not in start or "+00:00" not in end or start > end:
            raise ValueError
        lineage = value["lineage"]
        for key in (
            "eccc_normalized_output_sha256", "eccc_scientific_series_sha256",
            "pcic_normalized_output_sha256", "pcic_scientific_series_sha256",
            "bake_sha256", "characterization_code_sha256",
        ):
            digest = str(lineage[key])
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError
        sweep = value["temperature_correction"]["lapse_rate_sweep"]
        if [item["lapse_rate_k_per_km"] for item in sweep] != [2.5, 3.9, 5.2, 6.5, 7.5]:
            raise ValueError
        calm = value["wind_characterization"]["calm_threshold_sensitivity"]
        if [item["calm_threshold_m_s"] for item in calm] != [0.0, 0.5, 1.0, 2.0]:
            raise ValueError
        for item in calm:
            if not (
                0 <= item["exact_direction_pairs"] <= item["eligible_speed_pairs"]
                <= item["exact_speed_pairs"]
            ):
                raise ValueError
        terrain = value["terrain_representativeness"]["baked_layer_characterization"]
        if set(terrain["layer_file_sha256"]) != set(REQUIRED_BAKED_LAYERS):
            raise ValueError
        slope_fraction = sum(item["fraction"] for item in terrain["slope_bins"])
        aspect_fraction = sum(item["fraction"] for item in terrain["aspect"]["sectors"])
        if not math.isclose(slope_fraction, 1.0, abs_tol=1e-12):
            raise ValueError
        if not math.isclose(aspect_fraction, 1.0, abs_tol=1e-12):
            raise ValueError
        if value["snow_depth_swe"]["comparison_metrics"] is not None:
            raise ValueError
        if value["radiation"]["activation"] != "none_missing_values_remain_masked":
            raise ValueError
        if revision == CHARACTERIZATION_CONTRACT_REVISION:
            _validate_external_evidence_boundaries(
                value["snow_depth_swe"], value["radiation"]
            )
        activation = value["correction_activation"]
        if any(
            activation[key] != expected
            for key, expected in {
                "precipitation_orographic": "disabled",
                "precipitation_gauge_undercatch": "disabled",
                "wind_speed_up_or_exposure": "disabled",
                "radiation_derivation": "disabled",
                "gap_fill": "none",
            }.items()
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ForcingCharacterizationError(
            "Characterization revision failed strict scientific validation."
        ) from exc


def _validate_external_evidence_boundaries(
    snow: Mapping[str, Any], radiation: Mapping[str, Any]
) -> None:
    """Prevent an evidence audit from silently becoming selected forcing or validation."""

    try:
        if (
            snow["status"] != "blocked_identity_history_qc_revision_semantics_not_proven"
            or snow["comparison_metrics"] is not None
            or snow["full_observation_downloaded_or_cached"] is not False
            or snow["observation_values_used"] is not False
            or snow["date_effective_identity_contract"]
            != "not_found_in_authoritative_metadata"
            or snow["qc_revision_contract"]["status"] != "not_proven"
            or snow["incidental_live_feed_response_encountered"] is not True
        ):
            raise ValueError
        candidate = snow["candidate"]
        locations = candidate["provincial_current_location_records"]
        histories = candidate["separate_pcic_histories"]
        if (
            locations["2c09p"]["active"] is not False
            or locations["2c09q"]["active"] is not True
            or histories["mor"]["history_id"] != 2885
            or histories["2c09p"]["history_id"] != 2950
            or histories["2c09q"]["history_id"] != 2951
        ):
            raise ValueError
        catalog = radiation["pcic_catalog_audit"]
        era5 = radiation["era5_land_gap_fill_candidate"]
        if (
            catalog["exact_window_history_count"] != 0
            or catalog["exact_window_both_components_history_count"] != 0
            or catalog["historical_both_components_history_count"] != 5
            or catalog["latest_historical_both_components_end_date"]
            != "2020-07-01T00:00:00"
            or catalog["eligibility"] != "none"
            or radiation["eccc_archive_audit"]["representative_exact_window_station_found"]
            is not False
            or era5["status"] != "not_acquired_or_scientifically_evaluated_access_blocked"
            or era5["catalogue_exact_window_available"] is not True
            or era5["source_unit"] != "J m-2"
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ForcingCharacterizationError(
            "Characterization external-evidence boundary is incomplete or activating."
        ) from exc


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def load_characterization_report(path: str | Path) -> dict[str, Any]:
    requested = Path(path).resolve()
    if requested.is_file():
        try:
            return validate_characterization_report(
                json.loads(requested.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ForcingCharacterizationError(f"Invalid characterization report: {exc}") from exc
    if not requested.is_dir():
        raise ForcingCharacterizationError(f"Characterization path does not exist: {requested}")
    report_path = requested / REPORT_FILENAME
    checksums_path = requested / CHECKSUMS_FILENAME
    try:
        report_bytes = report_path.read_bytes()
        report = validate_characterization_report(json.loads(report_bytes.decode("utf-8")))
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ForcingCharacterizationError(f"Invalid characterization storage: {exc}") from exc
    expected = {
        "schema": STORAGE_SCHEMA,
        "report_id": report["report_id"],
        "files": {
            REPORT_FILENAME: {
                "bytes": len(report_bytes),
                "sha256": _sha256_bytes(report_bytes),
            }
        },
    }
    if checksums != expected or requested.name != report["report_id"]:
        raise ForcingCharacterizationError(
            "Characterization storage identity or checksum manifest does not match."
        )
    return report


def write_characterization_report(
    report: Mapping[str, Any], runtime_root: str | Path
) -> Path:
    validated = validate_characterization_report(report)
    root = Path(runtime_root).resolve() / "reports" / "conditions" / "m2"
    root.mkdir(parents=True, exist_ok=True)
    target = root / validated["report_id"]
    if target.exists():
        if load_characterization_report(target) != validated:
            raise ForcingCharacterizationError(
                f"Characterization identity collision at {target}."
            )
        return target
    staging = root / f".m2-characterization-build-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        report_bytes = _canonical_json_bytes(validated)
        _write_fsynced(staging / REPORT_FILENAME, report_bytes)
        checksums = {
            "schema": STORAGE_SCHEMA,
            "report_id": validated["report_id"],
            "files": {
                REPORT_FILENAME: {
                    "bytes": len(report_bytes),
                    "sha256": _sha256_bytes(report_bytes),
                }
            },
        }
        _write_fsynced(staging / CHECKSUMS_FILENAME, _canonical_json_bytes(checksums))
        if load_characterization_report(staging / REPORT_FILENAME) != validated:
            raise ForcingCharacterizationError("Staged characterization report changed.")
        staged_checksums = json.loads(
            (staging / CHECKSUMS_FILENAME).read_text(encoding="utf-8")
        )
        if staged_checksums != checksums:
            raise ForcingCharacterizationError(
                "Staged characterization checksum manifest changed."
            )
        try:
            staging.rename(target)
        except OSError:
            if not target.exists() or load_characterization_report(target) != validated:
                raise
            shutil.rmtree(staging)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
