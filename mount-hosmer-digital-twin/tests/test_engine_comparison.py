"""Normalized comparisons preserve masks and make unsupported fields explicit."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from avycore.engines import (
    AVAFRAME_COM1DFA,
    DISCLAIMER,
    NORMALIZED_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    AvalancheRegime,
    CRSContract,
    EngineStage,
    ExecutionBoundary,
    GridContract,
    MaskContract,
    NormalizedRunoutResult,
    OutputQuantity,
    RasterField,
    RunProvenance,
    build_result,
    compare_runout_results,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(path: Path) -> ArtifactRef:
    return ArtifactRef(
        uri=path.name,
        sha256=_sha(path),
        byte_size=path.stat().st_size,
        media_type="application/x-npy",
    )


def _bundle(root: Path, extent: np.ndarray, mask: np.ndarray, marker: str) -> NormalizedRunoutResult:
    root.mkdir()
    extent_path = root / "runout.npy"
    mask_path = root / "mask.npy"
    np.save(extent_path, extent.astype(bool), allow_pickle=False)
    np.save(mask_path, mask.astype(bool), allow_pickle=False)
    crs = CRSContract(
        definition="EPSG:32611",
        projected=True,
        horizontal_unit="m",
        coordinate_order="x,y",
        vertical_datum=None,
        vertical_datum_status="unknown",
    )
    grid = GridContract(
        crs=crs,
        shape=extent.shape,
        affine_transform=(5.0, 0.0, 0.0, 0.0, -5.0, 10.0),
        cell_size_x_m=5.0,
        cell_size_y_m=5.0,
        origin_semantics="upper_left_outer_corner",
    )
    mask_contract = MaskContract(
        artifact=_artifact(mask_path),
        valid_cells=int(np.count_nonzero(~mask)),
        masked_cells=int(np.count_nonzero(mask)),
        combined_from=(f"{marker}_source",),
    )
    field = RasterField(
        quantity=OutputQuantity.RUNOUT_EXTENT,
        unit="1",
        artifact=_artifact(extent_path),
        mask=mask_contract,
        grid=grid,
        dtype="bool",
        valid_min=0.0,
        valid_max=1.0,
        semantics="test extent",
    )
    digest = hashlib.sha256(marker.encode()).hexdigest()
    return build_result(
        NormalizedRunoutResult,
        {
            "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
            "disclaimer": DISCLAIMER,
            "site_id": "synthetic.utm11",
            "stage": EngineStage.RUNOUT,
            "regime": AvalancheRegime.DENSE_DRY,
            "provenance": RunProvenance(
                engine_id=AVAFRAME_COM1DFA.engine_id,
                engine_version="2.1",
                adapter_version="test",
                license_spdx="EUPL-1.2",
                execution_boundary=ExecutionBoundary.OFFLINE_SUBPROCESS,
                executable_sha256=digest,
                environment_sha256=digest,
                adapter_sha256=digest,
                selection_sha256=digest,
                configuration_sha256=digest,
                input_manifest_sha256=digest,
                output_manifest_sha256=digest,
                scenario_sha256=digest,
                seed=1,
                source_urls=(AVAFRAME_COM1DFA.source_url,),
            ),
            "validation": AVAFRAME_COM1DFA.validation,
            "uncertainty": (),
            "warnings": (),
            "limitations": ("test only",),
            "runout_extent": field,
            "runout_polygons": None,
            "flow_depth": None,
            "flow_velocity": None,
            "flow_pressure": None,
            "runout_area_m2": float(np.count_nonzero(extent & ~mask) * 25.0),
            "aoi_status": "complete_within_domain",
        },
    )


def test_comparison_excludes_unknown_cells_and_is_replayable(tmp_path: Path):
    left = _bundle(
        tmp_path / "left",
        np.array([[1, 1, 0], [0, 0, 0]], dtype=bool),
        np.array([[1, 0, 0], [0, 0, 0]], dtype=bool),
        "left",
    )
    right = _bundle(
        tmp_path / "right",
        np.array([[0, 1, 1], [0, 0, 0]], dtype=bool),
        np.zeros((2, 3), dtype=bool),
        "right",
    )
    output = tmp_path / "comparisons"
    first = compare_runout_results(
        left,
        right,
        left_bundle=tmp_path / "left",
        right_bundle=tmp_path / "right",
        output_root=output,
    )
    second = compare_runout_results(
        left,
        right,
        left_bundle=tmp_path / "left",
        right_bundle=tmp_path / "right",
        output_root=output,
    )
    assert first == second
    assert first.common_mask.masked_cells == 1
    metrics = {item.name: item for item in first.metrics}
    assert metrics["extent_intersection_over_union"].value == pytest.approx(0.5)
    assert metrics["extent_symmetric_difference_area"].value == pytest.approx(25.0)
    assert any("depth comparison unavailable" in warning for warning in first.warnings)


def test_comparison_rejects_tampered_artifact(tmp_path: Path):
    result = _bundle(
        tmp_path / "bundle",
        np.zeros((2, 3), dtype=bool),
        np.zeros((2, 3), dtype=bool),
        "same",
    )
    (tmp_path / "bundle" / "runout.npy").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="wrong size"):
        compare_runout_results(
            result,
            result,
            left_bundle=tmp_path / "bundle",
            right_bundle=tmp_path / "bundle",
            output_root=tmp_path / "comparisons",
        )
