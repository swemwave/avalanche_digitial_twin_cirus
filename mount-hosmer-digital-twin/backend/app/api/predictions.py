r"""Read-only routes over immutable offline prediction products.

These routes open files under ``runtime\predictions\`` and validate them against
the ``avycore.products`` contract. They run no engine and import no engine: the
external models execute offline, and ``POST /api/assess`` is untouched by this
module. Anything a product could not produce is served as an explicit
unavailable record with a reason, never as a zero.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.models import (
    PredictionProductDetail,
    PredictionProductList,
    PredictionProductSummary,
    RunoutComparisonDetail,
)
from app.core.settings import get_settings
from app.predictions import (
    PredictionProductError,
    list_prediction_products,
    load_prediction_product,
    prediction_product_root,
)

router = APIRouter(prefix="/api", tags=["predictions"])


def _load(product_id: str):
    settings = get_settings()
    try:
        root = prediction_product_root(settings.runtime_root, product_id)
    except PredictionProductError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"No prediction product {product_id}.")
    try:
        return load_prediction_product(root)
    except PredictionProductError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _summary(product) -> PredictionProductSummary:
    return PredictionProductSummary.model_validate(
        {
            "product_id": product.product_id,
            "site_id": product.site_id,
            "regime": product.regime,
            "generated_from": product.generated_from,
            "pipeline_version": product.provenance.pipeline_version,
            "engine_ids": list(product.engine_ids()),
            "comparison_ids": [item.comparison_id for item in product.comparisons],
            "has_release": product.release is not None,
            "has_snow_state": product.snow_state is not None,
            "unavailable_stages": [
                {
                    "stage": record.stage.value,
                    "status": record.status.value,
                    "reason": record.reason,
                }
                for record in product.unavailable_stages
            ],
            "validation_level": product.validation.level.value,
            "eligible_field_events": product.validation.eligible_field_events,
            "disclaimer": product.disclaimer,
        }
    )


@router.get(
    "/predictions",
    response_model=PredictionProductList,
    operation_id="listPredictionProducts",
)
def prediction_products() -> PredictionProductList:
    """Every stored product. An empty list means none has been generated yet."""

    settings = get_settings()
    try:
        products = list_prediction_products(settings.runtime_root)
    except PredictionProductError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PredictionProductList(
        products=[_summary(item) for item in products],
        statement=(
            "Offline research products. Not an operational avalanche forecast and not a "
            "calibrated probability."
        ),
    )


@router.get(
    "/predictions/{product_id}",
    response_model=PredictionProductDetail,
    operation_id="getPredictionProduct",
)
def prediction_product(product_id: str) -> PredictionProductDetail:
    """One product: stages, per-engine outputs, unsupported outputs, limitations."""

    product = _load(product_id)
    engines = []
    for record in product.runouts:
        result = record.result
        engines.append(
            {
                "engine_id": record.engine_id,
                "engine_version": record.engine_version,
                "license_spdx": record.license_spdx,
                "result_id": result.result_id,
                "artifact_root": record.artifact_root,
                "runout_area_m2": result.runout_area_m2,
                "aoi_status": result.aoi_status,
                "available_outputs": _available_outputs(result),
                "unsupported_outputs": [
                    {"quantity": item.quantity.value, "reason": item.reason}
                    for item in result.unsupported_outputs
                ],
                "warnings": list(result.warnings),
                "limitations": list(result.limitations),
                "seed": result.provenance.seed,
                "configuration_sha256": result.provenance.configuration_sha256,
                "environment_sha256": result.provenance.environment_sha256,
            }
        )
    release = product.release
    return PredictionProductDetail.model_validate(
        {
            "summary": _summary(product).model_dump(),
            "stages": [
                {
                    "stage": record.stage.value,
                    "status": record.status.value,
                    "engine_id": record.engine_id,
                    "result_id": record.result_id,
                    "artifact_root": record.artifact_root,
                    "reason": record.reason,
                }
                for record in product.stages
            ],
            "release": None
            if release is None
            else {
                "result_id": release.result_id,
                "engine_id": release.provenance.engine_id,
                "artifact_root": product.release_artifact_root,
                "release_area_m2": release.release_area_m2,
                "release_volume_m3": release.release_volume_m3,
                "has_release_index": release.release_index is not None,
                "has_release_thickness": release.release_thickness is not None,
                "has_release_density": release.release_density is not None,
                # Release probability stays unavailable until a calibrated
                # probabilistic model and eligible holdout evidence exist.
                "release_probability": None,
                "release_probability_unavailable_reason": (
                    "No calibrated probabilistic release model and no eligible independent "
                    "validation cohort exist, so a probability cannot be published."
                ),
                "limitations": list(release.limitations),
            },
            "engines": engines,
            "comparisons": [
                {
                    "comparison_id": item.comparison_id,
                    "left_engine_id": item.left_engine_id,
                    "right_engine_id": item.right_engine_id,
                    "comparator_version": item.comparator_version,
                }
                for item in product.comparisons
            ],
            "uncertainty": [
                {
                    "parameter": bound.parameter,
                    "unit": bound.unit,
                    "lower": bound.lower,
                    "central": bound.central,
                    "upper": bound.upper,
                    "basis": bound.basis,
                    "source": bound.source,
                    "interpretation": bound.interpretation,
                }
                for bound in product.uncertainty
            ],
            "ensembles": [
                {
                    "engine_id": summary.engine_id,
                    "parameter": summary.parameter,
                    "unit": summary.unit,
                    "varies": summary.varies,
                    "basis": summary.basis,
                    "source": summary.source,
                    "members": [
                        {
                            "member_id": member.member_id,
                            "parameter": member.parameter,
                            "unit": member.unit,
                            "value": member.value,
                            "is_central": member.is_central,
                            "result_id": member.result_id,
                            "runout_area_m2": member.runout_area_m2,
                            "aoi_status": member.aoi_status,
                        }
                        for member in summary.members
                    ],
                    "central_runout_area_m2": summary.central_runout_area_m2,
                    "minimum_runout_area_m2": summary.minimum_runout_area_m2,
                    "maximum_runout_area_m2": summary.maximum_runout_area_m2,
                    "envelope_area_m2": summary.envelope_area_m2,
                    "envelope_artifact_root": summary.envelope_artifact_root,
                    "area_spread_m2": summary.area_spread_m2,
                    "interpretation": summary.interpretation,
                    "member_frequency_note": summary.member_frequency_note,
                }
                for summary in product.ensembles
            ],
            # A sweep that was asked for and not run is served, not omitted: an
            # absent entry would read as "this parameter does not matter".
            "unsupported_ensembles": [
                {
                    "engine_id": item.engine_id,
                    "parameter": item.parameter,
                    "reason": item.reason,
                    "required_to_enable": item.required_to_enable,
                }
                for item in product.unsupported_ensembles
            ],
            "dominant_uncertainty_contributor": product.dominant_uncertainty_contributor,
            "provenance": {
                "mountain_pack_sha256": product.provenance.mountain_pack_sha256,
                "bake_sha256": product.provenance.bake_sha256,
                "condition_pack_id": product.provenance.condition_pack_id,
                "snow_state_pack_id": product.provenance.snow_state_pack_id,
                "pipeline_version": product.provenance.pipeline_version,
                "pipeline_sha256": product.provenance.pipeline_sha256,
                "configuration_sha256": product.provenance.configuration_sha256,
                "seed": product.provenance.seed,
            },
            "warnings": list(product.warnings),
            "limitations": list(product.limitations),
        }
    )


@router.get(
    "/predictions/{product_id}/comparisons/{comparison_id}",
    response_model=RunoutComparisonDetail,
    operation_id="getPredictionComparison",
)
def prediction_comparison(product_id: str, comparison_id: str) -> RunoutComparisonDetail:
    """One engine comparison. Every metric measures disagreement, not correctness."""

    product = _load(product_id)
    for comparison in product.comparisons:
        if comparison.comparison_id == comparison_id:
            return RunoutComparisonDetail.model_validate(
                {
                    "comparison_id": comparison.comparison_id,
                    "left_engine_id": comparison.left_engine_id,
                    "right_engine_id": comparison.right_engine_id,
                    "left_result_id": comparison.left_result_id,
                    "right_result_id": comparison.right_result_id,
                    "comparator_version": comparison.comparator_version,
                    "common_valid_cells": comparison.common_mask.valid_cells,
                    "common_masked_cells": comparison.common_mask.masked_cells,
                    "metrics": [
                        {
                            "name": metric.name,
                            "quantity": metric.quantity.value,
                            "unit": metric.unit,
                            "status": metric.status,
                            "value": metric.value,
                            "valid_cells": metric.valid_cells,
                            "semantics": metric.semantics,
                        }
                        for metric in comparison.metrics
                    ],
                    "warnings": list(comparison.warnings),
                    "limitations": list(comparison.limitations),
                    "disclaimer": comparison.disclaimer,
                }
            )
    raise HTTPException(
        status_code=404, detail=f"Product {product_id} has no comparison {comparison_id}."
    )


def _available_outputs(result) -> list[str]:
    published = {
        "runout_extent": result.runout_extent,
        "flow_depth": result.flow_depth,
        "flow_velocity": result.flow_velocity,
        "flow_pressure": result.flow_pressure,
        "energy_line_height": result.energy_line_height,
        "travel_angle": result.travel_angle,
        "arrival_time": result.arrival_time,
    }
    return sorted(name for name, field in published.items() if field is not None)
