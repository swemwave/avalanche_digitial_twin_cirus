"""Code-reviewed validation dataset trust registry.

An identity may be added only after the immutable source lineage, scientific
method, survey completeness, event/scenario linkage, uncertainty, licensing, and
holdout independence have been reviewed.  Contract-valid data are not trusted by
default.  The empty registry accurately reflects the current absence of an eligible
independent field-observation cohort; reviewed imagery interpretations remain
qualitative evidence and do not belong here.
"""

from __future__ import annotations

from types import MappingProxyType

# Exact ValidationDataset.dataset_identity_sha256 values approved in code review.
TRUSTED_DATASET_IDENTITIES_SHA256: frozenset[str] = frozenset()

# Contract-v3 trust is deliberately scoped to the component actually reviewed.
# The same immutable evidence package cannot acquire end-to-end trust merely
# because it was approved for release or conditional-runout evaluation.
TRUSTED_DATASET_IDENTITIES_BY_COMPONENT = MappingProxyType(
    {
        "release": frozenset(),
        "conditional_runout": frozenset(),
        "end_to_end": frozenset(),
    }
)

__all__ = [
    "TRUSTED_DATASET_IDENTITIES_BY_COMPONENT",
    "TRUSTED_DATASET_IDENTITIES_SHA256",
]
