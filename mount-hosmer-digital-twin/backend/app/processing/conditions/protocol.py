"""Provider-neutral replay interface for offline condition normalization.

Provider implementations belong beside this module, never in :mod:`avycore`.
The protocol returns normalized content and deliberately has no storage method,
runtime path, or DATA path. Persistence is exclusively the storage layer's job.
M1 defines no live or provider-specific implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avycore.conditions import ConditionPack, ConditionPackDraft, build_condition_pack
from avycore.conditions.contracts import MountainGridIdentity


@dataclass(frozen=True)
class ConditionRequest:
    """Read-only request surface available to a provider normalizer."""

    mountain_grid: MountainGridIdentity
    valid_start_utc: datetime
    valid_end_utc: datetime

    def __post_init__(self) -> None:
        for name in ("valid_start_utc", "valid_end_utc"):
            value = getattr(self, name)
            if (
                value.tzinfo is None
                or value.utcoffset() is None
                or value.utcoffset().total_seconds() != 0
            ):
                raise ValueError(f"{name} must be timezone-aware UTC.")
        if self.valid_end_utc < self.valid_start_utc:
            raise ValueError("valid_end_utc must not precede valid_start_utc.")


class ConditionProvider(Protocol):
    """Normalize one immutable provider snapshot without persisting it."""

    @property
    def provider_id(self) -> str: ...

    def normalize(self, request: ConditionRequest) -> ConditionPackDraft:
        """Return canonical normalized content for the supplied snapshot."""
        ...


def replay_provider(provider: ConditionProvider, request: ConditionRequest) -> ConditionPack:
    """Normalize and content-address one snapshot through the shared replay path."""

    draft = provider.normalize(request)
    if draft.source.provider_id != provider.provider_id:
        raise ValueError(
            "Normalized source provider_id does not match the provider protocol identity."
        )
    return build_condition_pack(draft)
