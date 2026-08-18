"""Offline pipeline stages, immutable prediction products, and their read-only API."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from avycore.engines import (
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
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
    UnsupportedOutput,
    ValidationLevel,
    ValidationStatus,
    build_result,
    canonical_json_bytes,
)
from avycore.products import (
    PREDICTION_PRODUCT_SCHEMA_VERSION,
    EngineRunRecord,
    PipelineStage,
    PredictionProduct,
    ProductProvenance,
    StageRecord,
    StageStatus,
    build_prediction_product,
)

from app.core.settings import get_settings
from app.predictions import (
    PredictionProductError,
    load_prediction_product,
    prediction_product_root,
    verify_prediction_product,
)


PRODUCT_DISCLAIMER = (
    "Experimental research product from an offline avalanche-model pipeline. This is NOT an "
    "operational avalanche forecast and is NOT a calibrated avalanche probability. It must never "
    "replace official avalanche guidance or field assessment."
)
NO_FIELD_VALIDATION = ValidationStatus(
    level=ValidationLevel.SOFTWARE_VERIFICATION_ONLY,
    evidence=("Deterministic software tests only.",),
    eligible_field_events=0,
    limitations=("Software verification is not field validation.",),
)
BASE_LIMITATIONS = ("Research prototype; not an operational forecast.",)


def _digest(marker: str) -> str:
    return hashlib.sha256(marker.encode()).hexdigest()


def _runout(root: Path, engine_id: str, extent: np.ndarray, marker: str) -> NormalizedRunoutResult:
    root.mkdir(parents=True)
    extent_path = root / "runout.npy"
    mask_path = root / "mask.npy"
    mask = np.zeros(extent.shape, dtype=bool)
    np.save(extent_path, extent.astype(bool), allow_pickle=False)
    np.save(mask_path, mask, allow_pickle=False)

    def artifact(path: Path) -> ArtifactRef:
        return ArtifactRef(
            uri=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            byte_size=path.stat().st_size,
            media_type="application/x-npy",
        )

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
        artifact=artifact(mask_path),
        valid_cells=int(extent.size),
        masked_cells=0,
        combined_from=("test_source",),
    )
    field = RasterField(
        quantity=OutputQuantity.RUNOUT_EXTENT,
        unit="1",
        artifact=artifact(extent_path),
        mask=mask_contract,
        grid=grid,
        dtype="bool",
        valid_min=0.0,
        valid_max=1.0,
        semantics="test extent",
    )
    digest = _digest(marker)
    return build_result(
        NormalizedRunoutResult,
        {
            "schema_version": NORMALIZED_RESULT_SCHEMA_VERSION,
            "disclaimer": PRODUCT_DISCLAIMER,
            "site_id": "synthetic.utm11",
            "stage": EngineStage.RUNOUT,
            "regime": AvalancheRegime.DENSE_DRY,
            "provenance": RunProvenance(
                engine_id=engine_id,
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
                source_urls=("https://example.invalid/engine",),
            ),
            "validation": NO_FIELD_VALIDATION,
            "uncertainty": (),
            "warnings": (),
            "limitations": ("test only",),
            "runout_extent": field,
            "runout_polygons": None,
            "flow_depth": None,
            "flow_velocity": None,
            "flow_pressure": None,
            "unsupported_outputs": (
                UnsupportedOutput(
                    quantity=OutputQuantity.FLOW_VELOCITY,
                    reason="This test engine solves no momentum equation and produces no velocity.",
                ),
            ),
            "runout_area_m2": float(np.count_nonzero(extent) * 25.0),
            "aoi_status": "complete_within_domain",
        },
    )


def _product(runouts: tuple[EngineRunRecord, ...], stages: tuple[StageRecord, ...]) -> PredictionProduct:
    return build_prediction_product(
        {
            "schema_version": PREDICTION_PRODUCT_SCHEMA_VERSION,
            "site_id": "synthetic.utm11",
            "disclaimer": PRODUCT_DISCLAIMER,
            "regime": "dense_dry",
            "generated_from": "synthetic_case",
            "provenance": ProductProvenance(
                mountain_pack_sha256=_digest("pack"),
                pipeline_version="test",
                pipeline_sha256=_digest("pipeline"),
                configuration_sha256=_digest("config"),
                seed=7,
            ),
            "stages": stages,
            "runouts": runouts,
            "validation": NO_FIELD_VALIDATION,
            "limitations": BASE_LIMITATIONS,
        }
    )


def _write_product(runtime_root: Path, product: PredictionProduct) -> Path:
    root = runtime_root / "predictions" / product.product_id
    root.mkdir(parents=True)
    (root / "prediction-product.json").write_bytes(
        canonical_json_bytes(product.model_dump(mode="json")) + b"\n"
    )
    checksums = {
        "prediction-product.json": hashlib.sha256(
            (root / "prediction-product.json").read_bytes()
        ).hexdigest()
    }
    (root / "checksums.json").write_bytes(canonical_json_bytes(checksums) + b"\n")
    return root


# --- contract rules ---------------------------------------------------------


def test_completed_stage_must_publish_a_result_and_others_must_not():
    with pytest.raises(ValueError):
        StageRecord(
            stage=PipelineStage.RELEASE, status=StageStatus.COMPLETED, reason="no identity"
        )
    with pytest.raises(ValueError):
        StageRecord(
            stage=PipelineStage.RELEASE,
            status=StageStatus.UNAVAILABLE,
            reason="unavailable but claims a result",
            result_id="release-result-" + "0" * 64,
            artifact_root="release",
        )
    ok = StageRecord(
        stage=PipelineStage.SNOW_STATE_PACK,
        status=StageStatus.UNAVAILABLE,
        reason="No eligible forcing exists.",
    )
    assert ok.result_id is None


def test_empty_product_must_explain_itself_rather_than_read_as_success():
    with pytest.raises(ValueError):
        _product(
            (),
            (
                StageRecord(
                    stage=PipelineStage.RELEASE,
                    status=StageStatus.SKIPPED,
                    reason="Nothing was requested.",
                ),
            ),
        )
    explained = _product(
        (),
        (
            StageRecord(
                stage=PipelineStage.SNOW_STATE_PACK,
                status=StageStatus.UNAVAILABLE,
                reason="No complete forcing exists, so no snow state was produced.",
            ),
        ),
    )
    assert [record.stage for record in explained.unavailable_stages] == [
        PipelineStage.SNOW_STATE_PACK
    ]


def test_product_identity_is_content_addressed(tmp_path: Path):
    runout = _runout(tmp_path / "a", AVAFRAME_COM1DFA.engine_id, np.ones((2, 2), bool), "a")
    record = EngineRunRecord(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        engine_version="2.1",
        license_spdx="EUPL-1.2",
        artifact_root="runouts/com1dfa",
        result=runout,
    )
    stages = (
        StageRecord(
            stage=PipelineStage.RUNOUT,
            status=StageStatus.COMPLETED,
            result_id=runout.result_id,
            artifact_root="runouts",
            reason="ran",
        ),
    )
    product = _product((record,), stages)
    assert product.product_id.startswith("prediction-product-")
    assert _product((record,), stages).product_id == product.product_id
    payload = product.model_dump(mode="json")
    payload["regime"] = "wet_snow"
    with pytest.raises(ValueError):
        PredictionProduct.model_validate(payload)


def test_engine_run_record_must_name_the_engine_that_produced_its_result(tmp_path: Path):
    runout = _runout(tmp_path / "a", AVAFRAME_COM1DFA.engine_id, np.ones((2, 2), bool), "a")
    with pytest.raises(ValueError):
        EngineRunRecord(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            engine_version="2.1",
            license_spdx="EUPL-1.2",
            artifact_root="runouts/flowpy",
            result=runout,
        )


# --- storage ----------------------------------------------------------------


@pytest.mark.parametrize(
    "product_id",
    [
        "../escape",
        "prediction-product-" + "z" * 64,
        "prediction-product-short",
        "not-a-product",
    ],
)
def test_product_identifier_cannot_escape_its_root(tmp_path: Path, product_id: str):
    with pytest.raises(PredictionProductError):
        prediction_product_root(tmp_path, product_id)


def test_stored_product_verifies_and_a_tampered_artifact_is_refused(tmp_path: Path):
    product = _product(
        (),
        (
            StageRecord(
                stage=PipelineStage.SNOW_STATE_PACK,
                status=StageStatus.UNAVAILABLE,
                reason="No complete forcing exists.",
            ),
        ),
    )
    root = _write_product(tmp_path, product)
    assert verify_prediction_product(root).product_id == product.product_id

    (root / "stray.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(PredictionProductError, match="file set differs"):
        verify_prediction_product(root)
    (root / "stray.txt").unlink()

    manifest = json.loads((root / "checksums.json").read_text(encoding="utf-8"))
    manifest["prediction-product.json"] = "0" * 64
    (root / "checksums.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
    with pytest.raises(PredictionProductError, match="failed its checksum"):
        verify_prediction_product(root)


def test_directory_name_must_match_the_product_identity(tmp_path: Path):
    product = _product(
        (),
        (
            StageRecord(
                stage=PipelineStage.SNOW_STATE_PACK,
                status=StageStatus.UNAVAILABLE,
                reason="No complete forcing exists.",
            ),
        ),
    )
    root = _write_product(tmp_path, product)
    renamed = root.with_name("prediction-product-" + "b" * 64)
    root.rename(renamed)
    with pytest.raises(PredictionProductError, match="does not match directory"):
        load_prediction_product(renamed)


# --- pipeline ---------------------------------------------------------------


def _request(tmp_path: Path, **overrides):
    from app.pipeline import PipelineRequest

    defaults = {
        "case": "synthetic",
        "engines": (AVAFRAME_COM1DFA.engine_id, AVAFRAME_FLOWPY.engine_id),
        "avaframe_python": None,
        "flowpy_checkout": None,
        "runtime_root": tmp_path,
        "seed": 12345,
        "dry_run": True,
        "ensemble": False,
        "condition_pack_id": None,
        "simulation_time_s": 40.0,
        "alpha_degrees": 25.0,
        "resume": False,
    }
    return PipelineRequest(**{**defaults, **overrides})


def test_dry_run_reports_engine_unavailability_without_running_physics(tmp_path: Path):
    from app.pipeline import check_engines

    report = {item["engine_id"]: item for item in check_engines(_request(tmp_path))}
    assert set(report) == {AVAFRAME_COM1DFA.engine_id, AVAFRAME_FLOWPY.engine_id}
    for item in report.values():
        assert item["status"] == "unavailable"
        assert "AvaFrame Python environment" in item["reason"]
        assert item["detected_version"] is None
    # Nothing may be written before an engine is confirmed available.
    assert not (tmp_path / "predictions").exists()


def test_pipeline_refuses_a_real_site_case_with_a_stage_attributed_reason(tmp_path: Path):
    from app.pipeline import PipelineError, run_pipeline

    with pytest.raises(PipelineError) as caught:
        run_pipeline(_request(tmp_path, case="mount-hosmer", dry_run=False))
    assert caught.value.stage == PipelineStage.MOUNTAIN_PACK
    assert "Snow State Pack" in str(caught.value)


def test_pipeline_refuses_an_unknown_engine(tmp_path: Path):
    from app.pipeline import PipelineError, run_pipeline

    with pytest.raises(PipelineError) as caught:
        run_pipeline(_request(tmp_path, engines=("runout.made_up",), dry_run=False))
    assert caught.value.stage == PipelineStage.RUNOUT
    assert "Unknown runout engine" in str(caught.value)


def test_pipeline_never_falls_back_to_an_available_engine(tmp_path: Path):
    """A requested engine that cannot run ends the run; it is not substituted."""

    from app.pipeline import PipelineError, run_pipeline

    with pytest.raises(PipelineError) as caught:
        run_pipeline(_request(tmp_path, dry_run=False))
    assert caught.value.stage == PipelineStage.RUNOUT
    assert AVAFRAME_COM1DFA.engine_id in str(caught.value)
    # Availability is probed before any stage runs, so nothing is published and
    # no release bundle is left behind for a later run to mistake for input.
    assert not list((tmp_path / "predictions").glob("prediction-product-*"))


def test_condition_stage_reports_missing_forcing_instead_of_defaulting(tmp_path: Path):
    from app.pipeline import PipelineError, _condition_stage

    skipped = _condition_stage(_request(tmp_path))
    assert skipped.status == StageStatus.SKIPPED
    assert "makes no current-condition" in skipped.reason

    with pytest.raises(PipelineError) as caught:
        _condition_stage(_request(tmp_path, condition_pack_id="condition-" + "0" * 64))
    assert caught.value.stage == PipelineStage.CONDITION_PACK


# --- read-only API ----------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AVALANCHE_RUNTIME_ROOT", str(tmp_path))
    get_settings(refresh=True)
    from app.api import predictions as prediction_routes
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(prediction_routes.router)
    try:
        yield TestClient(app)
    finally:
        monkeypatch.delenv("AVALANCHE_RUNTIME_ROOT", raising=False)
        get_settings(refresh=True)


def test_api_lists_nothing_when_no_product_exists(client: TestClient):
    response = client.get("/api/predictions")
    assert response.status_code == 200
    body = response.json()
    assert body["products"] == []
    assert "not an operational avalanche forecast" in body["statement"].lower()


def test_api_publishes_unavailable_stages_and_never_a_release_probability(
    client: TestClient, tmp_path: Path
):
    runout = _runout(
        tmp_path / "bundle", AVAFRAME_FLOWPY.engine_id, np.ones((2, 2), bool), "flowpy"
    )
    record = EngineRunRecord(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        engine_version="2.1",
        license_spdx="EUPL-1.2",
        artifact_root="runouts/runout.avaframe_flowpy",
        result=runout,
    )
    product = _product(
        (record,),
        (
            StageRecord(
                stage=PipelineStage.SNOW_STATE_PACK,
                status=StageStatus.UNAVAILABLE,
                reason="No complete hourly forcing exists, so no snow state was modelled.",
            ),
            StageRecord(
                stage=PipelineStage.RUNOUT,
                status=StageStatus.COMPLETED,
                result_id=runout.result_id,
                artifact_root="runouts",
                reason="ran",
            ),
        ),
    )
    _write_product(tmp_path, product)

    listing = client.get("/api/predictions").json()
    assert [item["product_id"] for item in listing["products"]] == [product.product_id]
    assert listing["products"][0]["unavailable_stages"][0]["stage"] == "snow_state_pack"
    assert listing["products"][0]["eligible_field_events"] == 0

    detail = client.get(f"/api/predictions/{product.product_id}").json()
    assert detail["release"] is None
    assert detail["engines"][0]["engine_id"] == AVAFRAME_FLOWPY.engine_id
    assert detail["engines"][0]["available_outputs"] == ["runout_extent"]
    unsupported = {item["quantity"]: item["reason"] for item in detail["engines"][0]["unsupported_outputs"]}
    assert "flow_velocity" in unsupported
    assert len(unsupported["flow_velocity"]) > 20
    assert detail["uncertainty"] == []
    assert any("not an operational" in item.lower() for item in detail["limitations"])

    missing = client.get("/api/predictions/prediction-product-" + "a" * 64)
    assert missing.status_code == 404
    assert client.get("/api/predictions/not-a-product").status_code == 400


def test_api_serves_comparison_metrics_as_disagreement(client: TestClient, tmp_path: Path):
    from avycore.engines import compare_runout_results

    left = _runout(
        tmp_path / "left", AVAFRAME_COM1DFA.engine_id, np.array([[1, 1], [0, 0]], bool), "left"
    )
    right = _runout(
        tmp_path / "right", AVAFRAME_FLOWPY.engine_id, np.array([[0, 1], [1, 0]], bool), "right"
    )
    comparison = compare_runout_results(
        left,
        right,
        left_bundle=tmp_path / "left",
        right_bundle=tmp_path / "right",
        output_root=tmp_path / "comparisons",
        reference_cell=(0, 0),
    )
    records = tuple(
        EngineRunRecord(
            engine_id=result.provenance.engine_id,
            engine_version="2.1",
            license_spdx="EUPL-1.2",
            artifact_root=f"runouts/{result.provenance.engine_id}",
            result=result,
        )
        for result in (left, right)
    )
    product = build_prediction_product(
        {
            "schema_version": PREDICTION_PRODUCT_SCHEMA_VERSION,
            "site_id": "synthetic.utm11",
            "disclaimer": PRODUCT_DISCLAIMER,
            "regime": "dense_dry",
            "generated_from": "synthetic_case",
            "provenance": ProductProvenance(
                mountain_pack_sha256=_digest("pack"),
                pipeline_version="test",
                pipeline_sha256=_digest("pipeline"),
                configuration_sha256=_digest("config"),
                seed=7,
            ),
            "stages": (
                StageRecord(
                    stage=PipelineStage.COMPARISON,
                    status=StageStatus.COMPLETED,
                    result_id=comparison.comparison_id,
                    artifact_root="comparisons",
                    reason="compared",
                ),
            ),
            "runouts": records,
            "comparisons": (comparison,),
            "comparison_artifact_roots": (f"comparisons/{comparison.comparison_id}",),
            "validation": NO_FIELD_VALIDATION,
            "limitations": BASE_LIMITATIONS,
        }
    )
    _write_product(tmp_path, product)

    body = client.get(
        f"/api/predictions/{product.product_id}/comparisons/{comparison.comparison_id}"
    ).json()
    metrics = {item["name"]: item for item in body["metrics"]}
    assert metrics["extent_intersection_over_union"]["status"] == "available"
    assert metrics["maximum_reach_difference"]["status"] == "available"
    assert metrics["velocity_mean_absolute_difference"]["status"] == "unsupported"
    assert metrics["velocity_mean_absolute_difference"]["value"] is None
    assert any("disagreement" in item for item in body["limitations"])
    assert client.get(
        f"/api/predictions/{product.product_id}/comparisons/comparison-result-" + "c" * 64
    ).status_code == 404


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_pipeline_publishes_a_verifiable_replayable_product(tmp_path: Path):
    from app.pipeline import run_pipeline

    request = _request(
        tmp_path,
        dry_run=False,
        avaframe_python=Path(os.environ["AVAFRAME_TEST_PYTHON"]),
        simulation_time_s=25.0,
    )
    first = run_pipeline(request)
    second = run_pipeline(request)
    assert first.product_id == second.product_id
    assert first.engine_ids() == (AVAFRAME_COM1DFA.engine_id, AVAFRAME_FLOWPY.engine_id)
    assert len(first.comparisons) == 1

    root = prediction_product_root(tmp_path, first.product_id)
    verified = verify_prediction_product(root)
    assert verified == first
    assert (root / "runouts" / AVAFRAME_FLOWPY.engine_id / "result.json").is_file()
    assert (root / "release" / "release-result.json").is_file()

    snow = first.stage(PipelineStage.SNOW_STATE_PACK)
    assert snow is not None and snow.status == StageStatus.UNAVAILABLE
    assert first.validation.eligible_field_events == 0


# --- bounded sensitivity sweeps ---------------------------------------------


def _member(
    value: float,
    area: float,
    *,
    central: bool,
    engine: str = AVAFRAME_FLOWPY.engine_id,
    marker: str = "",
):
    from avycore.products import EnsembleMember

    return EnsembleMember(
        member_id="member-" + hashlib.sha256(f"{engine}{value}{marker}".encode()).hexdigest()[:16],
        engine_id=engine,
        parameter="alpha_angle",
        unit="degree",
        value=value,
        is_central=central,
        result_id="runout-result-" + "0" * 64,
        artifact_root="ensembles/flowpy/alpha_angle",
        runout_area_m2=area,
        aoi_status="complete_within_domain",
    )


def _summary(members, *, envelope: float):
    from avycore.products import EnsembleSummary

    areas = [item.runout_area_m2 for item in members]
    central = next(item for item in members if item.is_central)
    return EnsembleSummary(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="alpha_angle",
        unit="degree",
        varies="engine_parameter",
        basis="literature",
        source=(
            "Assumed literature angle-of-reach range for dry-snow avalanche paths; not fitted to "
            "any observed event at any site."
        ),
        members=tuple(members),
        central_runout_area_m2=central.runout_area_m2,
        minimum_runout_area_m2=min(areas),
        maximum_runout_area_m2=max(areas),
        envelope_artifact_root="ensembles/flowpy/alpha_angle",
        envelope_area_m2=envelope,
        member_frequency_note="Model frequency over a deterministic sweep; not a probability.",
    )


def test_a_sweep_needs_exactly_one_central_member_and_distinct_values():
    good = _summary(
        [_member(22.0, 90.0, central=False), _member(25.0, 70.0, central=True), _member(28.0, 50.0, central=False)],
        envelope=95.0,
    )
    assert good.area_spread_m2 == 40.0

    with pytest.raises(ValueError, match="exactly one central member"):
        _summary([_member(22.0, 90.0, central=True), _member(25.0, 70.0, central=True)], envelope=95.0)
    with pytest.raises(ValueError, match="identities must be unique"):
        duplicate = _member(22.0, 90.0, central=False)
        _summary([duplicate, duplicate, _member(25.0, 70.0, central=True)], envelope=95.0)
    with pytest.raises(ValueError, match="values must be distinct"):
        _summary(
            [
                _member(25.0, 90.0, central=False, marker="a"),
                _member(25.0, 70.0, central=True, marker="b"),
            ],
            envelope=95.0,
        )


def test_the_outer_envelope_must_contain_every_member_footprint():
    members = [_member(22.0, 90.0, central=False), _member(25.0, 70.0, central=True)]
    with pytest.raises(ValueError, match="must contain every member"):
        _summary(members, envelope=80.0)
    assert _summary(members, envelope=90.0).envelope_area_m2 == 90.0


def test_a_sweep_is_labelled_sensitivity_and_never_probability():
    summary = _summary(
        [_member(22.0, 90.0, central=False), _member(25.0, 70.0, central=True)], envelope=95.0
    )
    assert summary.interpretation == "bounded_sensitivity_not_probability"
    assert summary.basis == "literature"
    assert "not a probability" in summary.member_frequency_note.lower()


def test_a_product_with_sweeps_must_name_its_dominant_contributor(tmp_path: Path):
    runout = _runout(tmp_path / "a", AVAFRAME_FLOWPY.engine_id, np.ones((2, 2), bool), "a")
    record = EngineRunRecord(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        engine_version="2.1",
        license_spdx="EUPL-1.2",
        artifact_root="runouts/flowpy",
        result=runout,
    )
    stages = (
        StageRecord(
            stage=PipelineStage.RUNOUT,
            status=StageStatus.COMPLETED,
            result_id=runout.result_id,
            artifact_root="runouts",
            reason="ran",
        ),
    )
    summary = _summary(
        [_member(22.0, 90.0, central=False), _member(25.0, 70.0, central=True)], envelope=95.0
    )
    content = {
        "schema_version": PREDICTION_PRODUCT_SCHEMA_VERSION,
        "site_id": "synthetic.utm11",
        "disclaimer": PRODUCT_DISCLAIMER,
        "regime": "dense_dry",
        "generated_from": "synthetic_case",
        "provenance": ProductProvenance(
            mountain_pack_sha256=_digest("pack"),
            pipeline_version="test",
            pipeline_sha256=_digest("pipeline"),
            configuration_sha256=_digest("config"),
            seed=7,
        ),
        "stages": stages,
        "runouts": (record,),
        "ensembles": (summary,),
        "validation": NO_FIELD_VALIDATION,
        "limitations": BASE_LIMITATIONS,
    }
    with pytest.raises(ValueError, match="dominant contributor"):
        build_prediction_product(content)
    with pytest.raises(ValueError, match="must name a published sweep"):
        build_prediction_product({**content, "dominant_uncertainty_contributor": "other:thing"})
    product = build_prediction_product(
        {
            **content,
            "dominant_uncertainty_contributor": f"{AVAFRAME_FLOWPY.engine_id}:alpha_angle",
        }
    )
    assert product.ensembles[0].area_spread_m2 == 20.0


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_real_bounded_sweep_brackets_the_central_result(tmp_path: Path):
    from app.pipeline import run_pipeline

    product = run_pipeline(
        _request(
            tmp_path,
            dry_run=False,
            ensemble=True,
            engines=(AVAFRAME_FLOWPY.engine_id,),
            avaframe_python=Path(os.environ["AVAFRAME_TEST_PYTHON"]),
        )
    )
    sweeps = {item.parameter: item for item in product.ensembles}
    assert set(sweeps) == {"alpha_angle", "release_extent_offset"}
    sweep = sweeps["alpha_angle"]
    assert sweep.engine_id == AVAFRAME_FLOWPY.engine_id
    assert len(sweep.members) == 3
    # A larger angle of reach must not produce a larger footprint; that ordering
    # is the physical sanity check the sweep exists to expose.
    ordered = sorted(sweep.members, key=lambda item: item.value)
    areas = [item.runout_area_m2 for item in ordered]
    assert areas == sorted(areas, reverse=True)
    assert sweep.minimum_runout_area_m2 <= sweep.central_runout_area_m2 <= sweep.maximum_runout_area_m2
    assert sweep.envelope_area_m2 >= sweep.maximum_runout_area_m2
    assert product.dominant_uncertainty_contributor == f"{AVAFRAME_FLOWPY.engine_id}:alpha_angle"
    bound = next(item for item in product.uncertainty if item.parameter == "alpha_angle")
    assert (bound.lower, bound.central, bound.upper) == (22.0, 25.0, 28.0)
    assert bound.basis == "literature"
    assert bound.interpretation == "bounded_sensitivity_not_probability"


# --- declared spans ---------------------------------------------------------


def _specification(**overrides):
    from avycore.products import SweepSpecification

    content = {
        "engine_id": AVAFRAME_FLOWPY.engine_id,
        "parameter": "alpha_angle",
        "unit": "degree",
        "varies": "engine_parameter",
        "offsets": (-3.0, 0.0, 3.0),
        "basis": "literature",
        "source": (
            "Assumed literature angle-of-reach range for dry-snow avalanche paths; not fitted to "
            "any observed event."
        ),
    }
    content.update(overrides)
    return SweepSpecification(**content)


def test_a_span_with_no_stated_basis_is_rejected_before_it_can_be_published():
    """The whole point of the specification: an unjustified span never runs."""

    from avycore.products import SweepSpecification

    assert _specification().basis == "literature"

    with pytest.raises(ValidationError, match="basis"):
        SweepSpecification(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            parameter="alpha_angle",
            unit="degree",
            varies="engine_parameter",
            offsets=(-3.0, 0.0, 3.0),
            source="A span with no declared basis at all, which must not be constructible.",
        )
    with pytest.raises(ValidationError):
        _specification(basis="vibes")
    # A basis label with no statement behind it is the same failure wearing a
    # label, so an empty or token source is refused too.
    with pytest.raises(ValidationError):
        _specification(source="")
    with pytest.raises(ValidationError):
        _specification(source="literature")


def test_a_span_must_bracket_its_central_value_with_distinct_finite_offsets():
    with pytest.raises(ValidationError, match="exactly one zero offset"):
        _specification(offsets=(-3.0, 1.0, 3.0))
    with pytest.raises(ValidationError, match="distinct"):
        _specification(offsets=(0.0, 0.0, 3.0))
    with pytest.raises(ValidationError, match="exactly one zero offset"):
        _specification(offsets=(-3.0, 1.0, 3.0, -1.0))
    with pytest.raises(ValidationError, match="bracket"):
        _specification(offsets=(0.0, 1.0, 3.0))
    with pytest.raises(ValidationError, match="finite"):
        _specification(offsets=(-3.0, 0.0, float("inf")))


def test_every_declared_pipeline_sweep_states_a_basis_and_a_central_value():
    from app.pipeline import ENSEMBLE_SWEEPS, SWEEP_CENTRAL_VALUES

    assert len(ENSEMBLE_SWEEPS) >= 6
    swept = {(item.engine_id, item.parameter) for item in ENSEMBLE_SWEEPS}
    # The widened set: friction, release thickness, release density and release
    # extent, on every engine whose model actually carries the quantity.
    assert (AVAFRAME_COM1DFA.engine_id, "release_thickness") in swept
    assert (AVAFRAME_COM1DFA.engine_id, "release_density") in swept
    assert (AVAFRAME_COM1DFA.engine_id, "release_extent_offset") in swept
    assert (AVAFRAME_FLOWPY.engine_id, "release_extent_offset") in swept
    for item in ENSEMBLE_SWEEPS:
        assert item.basis in {"source", "literature", "expert", "numerical"}
        assert len(item.source.strip()) >= 40
        assert item.parameter in SWEEP_CENTRAL_VALUES


def test_declined_sweeps_name_a_reason_and_the_action_that_would_enable_them():
    from app.pipeline import DECLINED_SWEEPS

    declined = {(item.engine_id, item.parameter) for item in DECLINED_SWEEPS}
    assert (AVAFRAME_COM1DFA.engine_id, "entrainment_thickness") in declined
    assert (AVAFRAME_FLOWPY.engine_id, "release_thickness") in declined
    assert (AVAFRAME_FLOWPY.engine_id, "release_density") in declined
    entrainment = next(
        item for item in DECLINED_SWEEPS if item.parameter == "entrainment_thickness"
    )
    assert "ENT" in entrainment.reason
    assert "entTh" in entrainment.required_to_enable


def test_a_sweep_cannot_be_both_published_and_declined(tmp_path: Path):
    from avycore.products import UnsupportedSweep

    runout = _runout(tmp_path / "a", AVAFRAME_FLOWPY.engine_id, np.ones((2, 2), bool), "a")
    record = EngineRunRecord(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        engine_version="2.1",
        license_spdx="EUPL-1.2",
        artifact_root="runouts/flowpy",
        result=runout,
    )
    summary = _summary(
        [_member(22.0, 90.0, central=False), _member(25.0, 70.0, central=True)], envelope=95.0
    )
    content = {
        "schema_version": PREDICTION_PRODUCT_SCHEMA_VERSION,
        "site_id": "synthetic.utm11",
        "disclaimer": PRODUCT_DISCLAIMER,
        "regime": "dense_dry",
        "generated_from": "synthetic_case",
        "provenance": ProductProvenance(
            mountain_pack_sha256=_digest("pack"),
            pipeline_version="test",
            pipeline_sha256=_digest("pipeline"),
            configuration_sha256=_digest("config"),
            seed=7,
        ),
        "stages": (
            StageRecord(
                stage=PipelineStage.RUNOUT,
                status=StageStatus.COMPLETED,
                result_id=runout.result_id,
                artifact_root="runouts",
                reason="ran",
            ),
        ),
        "runouts": (record,),
        "ensembles": (summary,),
        "dominant_uncertainty_contributor": f"{AVAFRAME_FLOWPY.engine_id}:alpha_angle",
        "validation": NO_FIELD_VALIDATION,
        "limitations": BASE_LIMITATIONS,
    }
    clash = UnsupportedSweep(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        parameter="alpha_angle",
        reason="whatever",
        required_to_enable="whatever",
    )
    with pytest.raises(ValueError, match="published and declined"):
        build_prediction_product({**content, "unsupported_ensembles": (clash,)})

    stranger = UnsupportedSweep(
        engine_id=AVAFRAME_COM1DFA.engine_id,
        parameter="release_thickness",
        reason="this engine never ran in this product",
        required_to_enable="run it",
    )
    with pytest.raises(ValueError, match="an engine this product ran"):
        build_prediction_product({**content, "unsupported_ensembles": (stranger,)})

    accepted = build_prediction_product(
        {
            **content,
            "unsupported_ensembles": (
                UnsupportedSweep(
                    engine_id=AVAFRAME_FLOWPY.engine_id,
                    parameter="release_thickness",
                    reason="com4FlowPy routes a dimensionless flux and carries no thickness term.",
                    required_to_enable="A model that carries mass and depth.",
                ),
            ),
        }
    )
    assert accepted.unsupported_ensembles[0].parameter == "release_thickness"


def test_moving_the_release_boundary_changes_the_release_and_its_identity(tmp_path: Path):
    # Writing the release bundle rasterizes, so this one genuinely needs the
    # bake-time geospatial stack. requirements-dev.txt does not install it, so
    # skip rather than pretend the dependency is present.
    pytest.importorskip("rasterio")

    from app.processing.runout.synthetic import _make_release_bundle

    areas = {}
    identities = {}
    for offset in (-5.0, 0.0, 5.0):
        release, _ = _make_release_bundle(
            tmp_path / f"release{offset:+.0f}", release_boundary_offset_m=offset
        )
        areas[offset] = release.release_area_m2
        identities[offset] = release.result_id
    assert areas[-5.0] < areas[0.0] < areas[5.0]
    # Two different releases must never share an identity, or a swept member
    # would be indistinguishable from the central run downstream.
    assert len(set(identities.values())) == 3

    with pytest.raises(ValueError, match="whole multiple"):
        _make_release_bundle(tmp_path / "fractional", release_boundary_offset_m=2.5)


def test_the_release_boundary_offset_never_leaves_the_valid_domain():
    from app.processing.runout.synthetic import _offset_extent

    extent = np.zeros((7, 7), bool)
    extent[3, 3] = True
    valid = np.ones((7, 7), bool)
    valid[2, 2] = False

    grown = _offset_extent(extent, valid, 1)
    assert grown[3, 3] and grown[2, 3] and grown[4, 4]
    # A masked cell is unknown ground, not free space to grow into.
    assert not grown[2, 2]
    assert int(np.count_nonzero(grown)) == 8

    # The domain edge does not wrap: a footprint on one border must not appear
    # on the opposite one.
    edge = np.zeros((7, 7), bool)
    edge[0, 0] = True
    wrapped = _offset_extent(edge, np.ones((7, 7), bool), 1)
    assert not wrapped[6, :].any() and not wrapped[:, 6].any()

    assert not np.any(_offset_extent(extent, valid, -1))
    assert np.array_equal(_offset_extent(extent, valid, 0), extent)


# --- input-keyed stage cache ------------------------------------------------


def _availability(**overrides):
    from avycore.engines import AvailabilityStatus, EngineAvailability

    content = {
        "engine_id": AVAFRAME_FLOWPY.engine_id,
        "status": AvailabilityStatus.AVAILABLE,
        "reason": "probe ok",
        "detected_version": "2.1",
        "executable_sha256": _digest("python"),
        "environment_sha256": _digest("environment"),
    }
    content.update(overrides)
    return EngineAvailability(**content)


class _StubAdapter:
    def __init__(self, identity: dict[str, str]):
        self._identity = identity

    def replay_identity(self) -> dict[str, str]:
        return self._identity


def _stub_request(seed: int = 7, scenario: str = "scenario"):
    from avycore.engines import ENGINE_CONTRACT_SCHEMA_VERSION, EngineRunRequest

    return EngineRunRequest(
        schema_version=ENGINE_CONTRACT_SCHEMA_VERSION,
        site_id="synthetic.utm11",
        research_disclaimer=PRODUCT_DISCLAIMER,
        stage=EngineStage.RUNOUT,
        regime=AvalancheRegime.DENSE_DRY,
        inputs=(),
        requested_outputs=(OutputQuantity.RUNOUT_EXTENT,),
        scenario_sha256=_digest(scenario),
        seed=seed,
    )


def test_a_cache_key_is_refused_whenever_any_identity_component_is_unknown():
    from app.pipeline import _runout_cache_key

    adapter = _StubAdapter({"adapter_version": "v1", "adapter_sha256": _digest("adapter")})
    keyed = _runout_cache_key(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        engine_request=_stub_request(),
        adapter=adapter,
        availability=_availability(),
    )
    assert keyed is not None
    key, components = keyed
    assert len(key) == 64
    assert components["environment_sha256"] == _digest("environment")

    # Every unknown component collapses to "no key", which is a guaranteed miss.
    for missing in ("executable_sha256", "environment_sha256"):
        assert (
            _runout_cache_key(
                engine_id=AVAFRAME_FLOWPY.engine_id,
                engine_request=_stub_request(),
                adapter=adapter,
                availability=_availability(**{missing: None}),
            )
            is None
        )
    assert (
        _runout_cache_key(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            engine_request=_stub_request(),
            adapter=_StubAdapter({"adapter_version": "v1"}),
            availability=_availability(),
        )
        is None
    )
    # An engine that takes no seed still keys, because a null seed is a stated
    # fact about the engine rather than an unresolved component.
    assert (
        _runout_cache_key(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            engine_request=_stub_request(seed=None),
            adapter=adapter,
            availability=_availability(),
        )
        is not None
    )


def test_the_cache_key_moves_with_the_request_the_adapter_and_the_environment():
    from app.pipeline import _runout_cache_key

    adapter = _StubAdapter({"adapter_version": "v1", "adapter_sha256": _digest("adapter")})

    def key(**overrides):
        return _runout_cache_key(
            engine_id=overrides.pop("engine_id", AVAFRAME_FLOWPY.engine_id),
            engine_request=overrides.pop("engine_request", _stub_request()),
            adapter=overrides.pop("adapter", adapter),
            availability=overrides.pop("availability", _availability()),
        )[0]

    baseline = key()
    assert key() == baseline
    assert key(engine_request=_stub_request(scenario="other")) != baseline
    assert key(engine_request=_stub_request(seed=8)) != baseline
    assert key(availability=_availability(environment_sha256=_digest("upgraded"))) != baseline
    assert key(availability=_availability(detected_version="2.2")) != baseline
    assert (
        key(adapter=_StubAdapter({"adapter_version": "v1", "adapter_sha256": _digest("edited")}))
        != baseline
    )
    assert key(engine_id=AVAFRAME_COM1DFA.engine_id) != baseline


def _seed_cache(tmp_path: Path, marker: str = "cached"):
    from app.pipeline import RunoutStageCache, _runout_cache_key

    result = _runout(tmp_path / "bundle", AVAFRAME_FLOWPY.engine_id, np.ones((2, 2), bool), marker)
    bundle = tmp_path / "bundle" / result.result_id
    bundle.mkdir(parents=True)
    for name in ("runout.npy", "mask.npy"):
        (tmp_path / "bundle" / name).replace(bundle / name)
    (bundle / "result.json").write_bytes(
        canonical_json_bytes(result.model_dump(mode="json")) + b"\n"
    )
    adapter = _StubAdapter(
        {
            "adapter_version": result.provenance.adapter_version,
            "adapter_sha256": result.provenance.adapter_sha256,
        }
    )
    availability = _availability(
        detected_version=result.provenance.engine_version,
        executable_sha256=result.provenance.executable_sha256,
        environment_sha256=result.provenance.environment_sha256,
    )
    cache = RunoutStageCache(root=tmp_path / "stage-cache")
    keyed = _runout_cache_key(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        engine_request=_stub_request(seed=result.provenance.seed, scenario=marker),
        adapter=adapter,
        availability=availability,
    )
    assert keyed is not None
    key, components = keyed
    cache.store(key=key, components=components, bundle=bundle, result=result)
    return cache, key, components, result


def test_a_cache_hit_restores_a_verified_bundle_and_nothing_else(tmp_path: Path):
    cache, key, components, result = _seed_cache(tmp_path)

    restored = cache.restore(
        engine_id=AVAFRAME_FLOWPY.engine_id,
        key=key,
        components=components,
        output_root=tmp_path / "out",
    )
    assert restored is not None and restored.result_id == result.result_id
    assert (tmp_path / "out" / result.result_id / "result.json").is_file()
    assert cache.report[-1]["outcome"] == "hit"

    # A key that does not exist is a miss, never a nearest match.
    assert (
        cache.restore(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            key="0" * 64,
            components=components,
            output_root=tmp_path / "out2",
        )
        is None
    )
    assert "No cache entry" in cache.report[-1]["reason"]


def test_a_tampered_or_mismatched_cache_entry_is_a_miss_not_a_result(tmp_path: Path):
    cache, key, components, result = _seed_cache(tmp_path)

    # Provenance that disagrees with the key it was stored under.
    lying = dict(components)
    lying["environment_sha256"] = _digest("someone-upgraded-numpy")
    assert (
        cache.restore(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            key=key,
            components=lying,
            output_root=tmp_path / "out-lying",
        )
        is None
    )
    assert "does not restate the key" in cache.report[-1]["reason"]

    stored = tmp_path / "stage-cache" / key / "bundle" / "result.json"
    stored.write_bytes(stored.read_bytes().replace(b"synthetic.utm11", b"synthetic.utm12"))
    assert (
        cache.restore(
            engine_id=AVAFRAME_FLOWPY.engine_id,
            key=key,
            components=components,
            output_root=tmp_path / "out-tampered",
        )
        is None
    )
    assert "failed its checksum" in cache.report[-1]["reason"]
    assert not (tmp_path / "out-tampered").exists()


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_resume_reuses_a_stored_run_and_publishes_the_identical_product(tmp_path: Path):
    from app.pipeline import RunoutStageCache, run_pipeline

    request = _request(
        tmp_path,
        dry_run=False,
        resume=True,
        engines=(AVAFRAME_FLOWPY.engine_id,),
        avaframe_python=Path(os.environ["AVAFRAME_TEST_PYTHON"]),
    )
    first_cache = RunoutStageCache()
    first = run_pipeline(request, cache=first_cache)
    assert [item["outcome"] for item in first_cache.report] == ["miss"]

    second_cache = RunoutStageCache()
    second = run_pipeline(request, cache=second_cache)
    assert [item["outcome"] for item in second_cache.report] == ["hit"]
    # Reuse is a statement about execution, never about the answer.
    assert second.product_id == first.product_id
    assert second.runouts[0].result == first.runouts[0].result


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_the_environment_probe_matches_what_a_real_run_records(tmp_path: Path):
    """The cache key's environment digest must be the one the worker writes.

    A worker runs under ``-I`` and cannot import a shared helper, so the manifest
    exists twice. If the copies drift, every hit would be rejected — or worse, a
    changed environment would stop being noticed.
    """

    from app.pipeline import run_pipeline
    from app.processing.runout.flowpy import AvaFrameCom4FlowPyAdapter

    python = Path(os.environ["AVAFRAME_TEST_PYTHON"])
    probed = AvaFrameCom4FlowPyAdapter(python).availability()
    assert probed.environment_sha256 is not None

    product = run_pipeline(
        _request(
            tmp_path,
            dry_run=False,
            engines=(AVAFRAME_FLOWPY.engine_id,),
            avaframe_python=python,
        )
    )
    assert product.runouts[0].result.provenance.environment_sha256 == probed.environment_sha256


@pytest.mark.skipif(
    not os.environ.get("AVAFRAME_TEST_PYTHON"),
    reason="Set AVAFRAME_TEST_PYTHON to an isolated AvaFrame 2.1 Python executable.",
)
def test_a_release_extent_sweep_moves_the_release_not_a_friction_constant(tmp_path: Path):
    from app.pipeline import run_pipeline

    product = run_pipeline(
        _request(
            tmp_path,
            dry_run=False,
            ensemble=True,
            resume=True,
            engines=(AVAFRAME_FLOWPY.engine_id,),
            avaframe_python=Path(os.environ["AVAFRAME_TEST_PYTHON"]),
        )
    )
    parameters = {item.parameter: item for item in product.ensembles}
    assert set(parameters) == {"alpha_angle", "release_extent_offset"}
    extent = parameters["release_extent_offset"]
    assert extent.varies == "release_input"
    assert extent.basis == "numerical"
    assert parameters["alpha_angle"].varies == "engine_parameter"
    # A larger release must not produce a smaller footprint.
    ordered = sorted(extent.members, key=lambda item: item.value)
    areas = [item.runout_area_m2 for item in ordered]
    assert areas == sorted(areas)
    assert extent.envelope_area_m2 >= extent.maximum_runout_area_m2

    # Spans this model has no term for are published as refusals, not as zeros.
    declined = {item.parameter for item in product.unsupported_ensembles}
    assert declined == {"release_thickness", "release_density"}
    for item in product.unsupported_ensembles:
        assert item.engine_id == AVAFRAME_FLOWPY.engine_id
        assert item.reason and item.required_to_enable


def test_the_travel_angle_comparison_reports_unsupported_rather_than_a_number(tmp_path: Path):
    """A quantity only one engine defines must never become a difference."""

    from avycore.engines import OutputQuantity, compare_runout_results

    left = _runout(tmp_path / "left", "runout.avaframe_com1dfa", np.ones((4, 4), bool), "left")
    right = _runout(tmp_path / "right", "runout.avaframe_flowpy", np.ones((4, 4), bool), "right")
    comparison = compare_runout_results(
        left,
        right,
        left_bundle=tmp_path / "left",
        right_bundle=tmp_path / "right",
        output_root=tmp_path / "comparison",
        reference_cell=(0, 0),
    )
    travel = [
        metric
        for metric in comparison.metrics
        if metric.quantity == OutputQuantity.TRAVEL_ANGLE
    ]
    assert travel, "the travel-angle metric must be published, not omitted"
    for metric in travel:
        assert metric.status == "unsupported"
        assert metric.value is None
