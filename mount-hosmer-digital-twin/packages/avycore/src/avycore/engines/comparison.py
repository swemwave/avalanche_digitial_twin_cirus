"""Deterministic comparison of normalized runout bundles on one declared grid.

Everything here measures *disagreement*.  A high overlap means two models say
similar things, which is not evidence that either is right; a low overlap tells
you where the answer depends on the model choice.  Nothing in this module can
promote a comparison into validation.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import numpy as np

from .contracts import (
    NORMALIZED_COMPARISON_SCHEMA_VERSION,
    ArtifactRef,
    ComparisonMetric,
    MaskContract,
    NormalizedComparisonResult,
    NormalizedRunoutResult,
    OutputQuantity,
    RasterField,
    build_comparison,
    canonical_json_bytes,
)


COMPARATOR_VERSION = "avycore-grid-comparison-v2"

# Quantities compared cell by cell whenever both results publish them.  A
# quantity one engine does not support is reported as unsupported, never as a
# zero difference.
COMPARABLE_QUANTITIES: tuple[tuple[str, OutputQuantity], ...] = (
    ("depth", OutputQuantity.FLOW_DEPTH),
    ("velocity", OutputQuantity.FLOW_VELOCITY),
    ("pressure", OutputQuantity.FLOW_PRESSURE),
    ("energy_line_height", OutputQuantity.ENERGY_LINE_HEIGHT),
    ("travel_angle", OutputQuantity.TRAVEL_ANGLE),
    ("arrival_time", OutputQuantity.ARRIVAL_TIME),
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_verified(root: Path, artifact: ArtifactRef) -> Path:
    relative = Path(artifact.uri)
    if relative.is_absolute():
        raise ValueError("Normalized bundle artifacts must use portable relative URIs.")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Artifact URI escapes its normalized bundle: {artifact.uri!r}") from exc
    if not path.is_file() or path.stat().st_size != artifact.byte_size:
        raise ValueError(f"Normalized artifact is missing or has the wrong size: {artifact.uri!r}")
    if _file_sha256(path) != artifact.sha256:
        raise ValueError(f"Normalized artifact hash mismatch: {artifact.uri!r}")
    return path


def _load_array(root: Path, field: RasterField) -> tuple[np.ndarray, np.ndarray]:
    values = np.load(_resolve_verified(root, field.artifact), allow_pickle=False)
    mask = np.load(_resolve_verified(root, field.mask.artifact), allow_pickle=False)
    if values.shape != field.grid.shape or mask.shape != field.grid.shape or mask.dtype != np.dtype("bool"):
        raise ValueError("Normalized field array or mask conflicts with its declared grid/dtype.")
    try:
        declared_dtype = np.dtype(field.dtype)
    except TypeError as exc:
        raise ValueError(f"Normalized field declares an invalid dtype: {field.dtype!r}") from exc
    if values.dtype != declared_dtype:
        raise ValueError(
            f"Normalized field dtype {values.dtype!s} conflicts with declared {field.dtype!r}."
        )
    return values, mask


def _metric(
    *,
    name: str,
    quantity: OutputQuantity,
    unit: str,
    value: float | None,
    valid_cells: int,
    status: str = "available",
    semantics: str,
) -> ComparisonMetric:
    return ComparisonMetric(
        name=name,
        quantity=quantity,
        unit=unit,
        status=status,
        value=value,
        valid_cells=valid_cells,
        semantics=semantics,
    )


def _unsupported_reason(result: NormalizedRunoutResult, quantity: OutputQuantity) -> str | None:
    for declared in result.unsupported_outputs:
        if declared.quantity == quantity:
            return declared.reason
    return None


def _reach_metrics(
    extent: np.ndarray,
    grid,
    reference_row_column: tuple[int, int],
) -> tuple[float, float]:
    """Return the maximum and mean planimetric reach from a reference cell."""

    rows, columns = np.nonzero(extent)
    if rows.size == 0:
        return 0.0, 0.0
    dy = (rows.astype(np.float64) - float(reference_row_column[0])) * grid.cell_size_y_m
    dx = (columns.astype(np.float64) - float(reference_row_column[1])) * grid.cell_size_x_m
    distance = np.hypot(dx, dy)
    return float(np.max(distance)), float(np.mean(distance))


def compare_runout_results(
    left: NormalizedRunoutResult,
    right: NormalizedRunoutResult,
    *,
    left_bundle: str | Path,
    right_bundle: str | Path,
    output_root: str | Path,
    reference_cell: tuple[int, int] | None = None,
) -> NormalizedComparisonResult:
    """Compare two results without treating unknown cells as no-runout zeros.

    ``reference_cell`` is the ``(row, column)`` both engines started from — the
    release cell or its centroid.  Reach metrics need a common origin, and
    guessing one from a footprint would silently invent it, so they are reported
    as not applicable when the caller cannot supply it.
    """

    if left.runout_extent.grid != right.runout_extent.grid:
        raise ValueError("Runout comparison requires identical declared grids and CRS.")
    if left.site_id != right.site_id:
        raise ValueError("Runout comparison requires the same declared site identity.")
    if left.disclaimer != right.disclaimer:
        raise ValueError("Runout comparison requires the same site research disclaimer.")
    if left.regime != right.regime:
        raise ValueError("Runout comparison requires the same declared avalanche regime.")

    grid = left.runout_extent.grid
    if reference_cell is not None:
        row, column = reference_cell
        if not (0 <= row < grid.shape[0] and 0 <= column < grid.shape[1]):
            raise ValueError("The comparison reference cell lies outside the declared grid.")

    left_root = Path(left_bundle).resolve()
    right_root = Path(right_bundle).resolve()
    left_extent, left_mask = _load_array(left_root, left.runout_extent)
    right_extent, right_mask = _load_array(right_root, right.runout_extent)
    common_mask = left_mask | right_mask
    valid = ~common_mask
    valid_cells = int(np.count_nonzero(valid))
    if valid_cells == 0:
        raise ValueError("Runout results have no common valid cells; comparison is unavailable.")

    left_binary = np.asarray(left_extent, dtype=bool) & valid
    right_binary = np.asarray(right_extent, dtype=bool) & valid
    intersection = int(np.count_nonzero(left_binary & right_binary))
    union = int(np.count_nonzero(left_binary | right_binary))
    cell_area = grid.cell_size_x_m * grid.cell_size_y_m
    total_cells = grid.shape[0] * grid.shape[1]
    only_one_unknown = int(np.count_nonzero(left_mask ^ right_mask))

    metrics: list[ComparisonMetric] = [
        _metric(
            name="extent_intersection_over_union",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="1",
            value=(intersection / union if union else None),
            valid_cells=valid_cells,
            status=("available" if union else "not_applicable"),
            semantics="Jaccard overlap on the intersection of both valid-data domains.",
        ),
        _metric(
            name="extent_symmetric_difference_area",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="m2",
            value=float(np.count_nonzero(left_binary ^ right_binary) * cell_area),
            valid_cells=valid_cells,
            semantics="Area classified as runout by exactly one result, excluding unknown cells.",
        ),
        _metric(
            name="left_only_extent_area",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="m2",
            value=float(np.count_nonzero(left_binary & ~right_binary) * cell_area),
            valid_cells=valid_cells,
            semantics="Area the left engine reaches and the right engine does not.",
        ),
        _metric(
            name="right_only_extent_area",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="m2",
            value=float(np.count_nonzero(right_binary & ~left_binary) * cell_area),
            valid_cells=valid_cells,
            semantics="Area the right engine reaches and the left engine does not.",
        ),
        _metric(
            name="extent_area_difference",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="m2",
            value=float(
                (np.count_nonzero(left_binary) - np.count_nonzero(right_binary)) * cell_area
            ),
            valid_cells=valid_cells,
            semantics="Left minus right runout area on the common valid domain; sign is meaningful.",
        ),
        _metric(
            name="common_valid_coverage_fraction",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="1",
            value=float(valid_cells / total_cells),
            valid_cells=valid_cells,
            semantics="Share of the declared grid where both results have known data.",
        ),
        _metric(
            name="single_result_unknown_area",
            quantity=OutputQuantity.RUNOUT_EXTENT,
            unit="m2",
            value=float(only_one_unknown * cell_area),
            valid_cells=valid_cells,
            semantics="Area unknown to exactly one result and therefore excluded from every metric.",
        ),
    ]

    warnings: list[str] = []
    if only_one_unknown:
        warnings.append(
            f"{only_one_unknown} cells are unknown to exactly one result and are excluded from all metrics."
        )
    for label, result in (("left", left), ("right", right)):
        if result.aoi_status == "truncated_at_domain":
            warnings.append(
                f"The {label} result ({result.provenance.engine_id}) is truncated at the computational "
                "domain, so its extent is a lower bound and every extent metric inherits that truncation."
            )
        elif result.aoi_status == "unknown":
            warnings.append(
                f"The {label} result ({result.provenance.engine_id}) does not declare whether it reached "
                "the domain boundary."
            )

    for name, quantity, value, semantics in (
        (
            "left_maximum_reach_distance",
            OutputQuantity.RUNOUT_EXTENT,
            None if reference_cell is None else _reach_metrics(left_binary, grid, reference_cell)[0],
            "Farthest reached cell of the left result from the common reference cell.",
        ),
        (
            "right_maximum_reach_distance",
            OutputQuantity.RUNOUT_EXTENT,
            None if reference_cell is None else _reach_metrics(right_binary, grid, reference_cell)[0],
            "Farthest reached cell of the right result from the common reference cell.",
        ),
    ):
        metrics.append(
            _metric(
                name=name,
                quantity=quantity,
                unit="m",
                value=value,
                valid_cells=valid_cells,
                status="available" if value is not None else "not_applicable",
                semantics=semantics,
            )
        )
    if reference_cell is not None:
        left_reach = _reach_metrics(left_binary, grid, reference_cell)[0]
        right_reach = _reach_metrics(right_binary, grid, reference_cell)[0]
        metrics.append(
            _metric(
                name="maximum_reach_difference",
                quantity=OutputQuantity.RUNOUT_EXTENT,
                unit="m",
                value=float(left_reach - right_reach),
                valid_cells=valid_cells,
                semantics="Left minus right farthest planimetric reach from the common reference cell.",
            )
        )
    else:
        warnings.append(
            "No common reference cell was supplied, so endpoint and reach differences are not applicable."
        )

    for label, quantity in COMPARABLE_QUANTITIES:
        left_field = left.field_for(quantity)
        right_field = right.field_for(quantity)
        if left_field is None or right_field is None:
            reasons = [
                f"{side}: {reason}"
                for side, reason in (
                    ("left", _unsupported_reason(left, quantity)),
                    ("right", _unsupported_reason(right, quantity)),
                )
                if reason
            ]
            detail = "; ".join(reasons) if reasons else "at least one engine did not publish it"
            metrics.append(
                _metric(
                    name=f"{label}_mean_absolute_difference",
                    quantity=quantity,
                    unit=left_field.unit if left_field else (right_field.unit if right_field else "1"),
                    value=None,
                    valid_cells=0,
                    status="unsupported",
                    semantics=f"Unavailable because {detail}.",
                )
            )
            warnings.append(f"{label} comparison unavailable because {detail}.")
            continue
        if left_field.grid != right_field.grid or left_field.unit != right_field.unit:
            raise ValueError(f"{label} fields have incompatible grids or units.")
        left_values, left_field_mask = _load_array(left_root, left_field)
        right_values, right_field_mask = _load_array(right_root, right_field)
        field_valid = ~(left_field_mask | right_field_mask)
        field_valid_cells = int(np.count_nonzero(field_valid))
        if field_valid_cells == 0:
            metrics.append(
                _metric(
                    name=f"{label}_mean_absolute_difference",
                    quantity=quantity,
                    unit=left_field.unit,
                    value=None,
                    valid_cells=0,
                    status="not_applicable",
                    semantics=f"{label} comparison has no common valid cells.",
                )
            )
            warnings.append(f"{label} comparison has no common valid cells.")
            continue
        absolute_difference = np.abs(
            np.asarray(left_values, dtype=np.float64) - np.asarray(right_values, dtype=np.float64)
        )[field_valid]
        metrics.extend(
            (
                _metric(
                    name=f"{label}_mean_absolute_difference",
                    quantity=quantity,
                    unit=left_field.unit,
                    value=float(np.mean(absolute_difference)),
                    valid_cells=field_valid_cells,
                    semantics=f"Mean absolute {label} difference over common valid cells.",
                ),
                _metric(
                    name=f"{label}_maximum_absolute_difference",
                    quantity=quantity,
                    unit=left_field.unit,
                    value=float(np.max(absolute_difference)),
                    valid_cells=field_valid_cells,
                    semantics=f"Maximum absolute {label} difference over common valid cells.",
                ),
            )
        )

    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runout-comparison-", dir=output) as temp_name:
        staging = Path(temp_name)
        mask_path = staging / "comparison-mask.npy"
        np.save(mask_path, common_mask, allow_pickle=False)
        artifact = ArtifactRef(
            uri=mask_path.name,
            sha256=_file_sha256(mask_path),
            byte_size=mask_path.stat().st_size,
            media_type="application/x-npy",
        )
        result = build_comparison(
            {
                "schema_version": NORMALIZED_COMPARISON_SCHEMA_VERSION,
                "disclaimer": left.disclaimer,
                "site_id": left.site_id,
                "left_result_id": left.result_id,
                "right_result_id": right.result_id,
                "left_engine_id": left.provenance.engine_id,
                "right_engine_id": right.provenance.engine_id,
                "comparator_version": COMPARATOR_VERSION,
                "grid": grid,
                "common_mask": MaskContract(
                    artifact=artifact,
                    valid_cells=valid_cells,
                    masked_cells=int(np.count_nonzero(common_mask)),
                    combined_from=("left_result_mask", "right_result_mask"),
                ),
                "metrics": tuple(metrics),
                "warnings": tuple(warnings),
                "limitations": (
                    "Comparison metrics characterize model disagreement; they do not identify which model is physically correct.",
                    "Intersection-over-union, areas, and differences are deterministic diagnostics, not probabilities.",
                    "Agreement between two uncalibrated models is not evidence of accuracy for either.",
                ),
            }
        )
        result_path = staging / "comparison.json"
        result_path.write_bytes(canonical_json_bytes(result.model_dump(mode="json")) + b"\n")
        destination = output / result.comparison_id
        if destination.exists():
            existing = NormalizedComparisonResult.model_validate_json(
                (destination / "comparison.json").read_bytes()
            )
            if existing != result:
                raise FileExistsError(f"Comparison identity collision at {destination}")
            return existing
        staging.replace(destination)
        return result


__all__ = ["COMPARABLE_QUANTITIES", "COMPARATOR_VERSION", "compare_runout_results"]
