"""Code-reviewed validation dataset trust registry.

An identity may be added only after the immutable source lineage, scientific
method, survey completeness, event/scenario linkage, uncertainty, licensing, and
holdout independence have been reviewed.  Contract-valid data are not trusted by
default.  The empty registry accurately reflects the current absence of reviewed
Mount Hosmer field observations.
"""

from __future__ import annotations

# Exact ValidationDataset.dataset_identity_sha256 values approved in code review.
TRUSTED_DATASET_IDENTITIES_SHA256: frozenset[str] = frozenset()

__all__ = ["TRUSTED_DATASET_IDENTITIES_SHA256"]
