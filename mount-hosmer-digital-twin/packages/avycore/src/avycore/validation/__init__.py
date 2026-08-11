"""Validation evidence contracts and mask-aware numerical metrics."""

from .contracts import (
    ValidationContractError,
    ValidationDataset,
    ValidationDatasetManifest,
    load_validation_dataset,
)
from .metrics import (
    BinaryMaskMetrics,
    EvaluationGrid,
    EndpointMetrics,
    PredictionContext,
    PredictionScenario,
    binary_mask_metrics,
    paired_endpoint_metrics,
)
from .status import (
    SOFTWARE_BENCHMARK_VERSION,
    VALIDATION_CONTRACT_VERSION,
    model_validation_status,
)
from .trust import TRUSTED_DATASET_IDENTITIES_SHA256

__all__ = [
    "BinaryMaskMetrics",
    "EvaluationGrid",
    "EndpointMetrics",
    "PredictionContext",
    "PredictionScenario",
    "SOFTWARE_BENCHMARK_VERSION",
    "TRUSTED_DATASET_IDENTITIES_SHA256",
    "VALIDATION_CONTRACT_VERSION",
    "ValidationContractError",
    "ValidationDataset",
    "ValidationDatasetManifest",
    "binary_mask_metrics",
    "load_validation_dataset",
    "model_validation_status",
    "paired_endpoint_metrics",
]
