"""Provider-neutral, offline snow-state contracts.

Nothing in the serving assessment imports this package.  The contract is an
inactive M3 integration boundary, not a hazard or release-model input.
"""

from .contracts import (
    DISCLAIMER,
    SCIENTIFIC_REPLAY_SCHEMA_VERSION,
    SNOW_STATE_PACK_SCHEMA_VERSION,
    SnowStatePack,
    SnowStatePackDraft,
    build_snow_state_pack,
    canonical_snow_state_pack_bytes,
    scientific_replay_sha256,
)

__all__ = [
    "SNOW_STATE_PACK_SCHEMA_VERSION",
    "SCIENTIFIC_REPLAY_SCHEMA_VERSION",
    "DISCLAIMER",
    "SnowStatePack",
    "SnowStatePackDraft",
    "build_snow_state_pack",
    "canonical_snow_state_pack_bytes",
    "scientific_replay_sha256",
]
