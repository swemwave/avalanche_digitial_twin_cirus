"""Universal, explicit-applicability engine contracts and registry."""

from .catalog import (
    AVAFRAME_COM1DFA,
    AVAFRAME_FLOWPY,
    AVYCORE_ALPHA_BASELINE,
    AVYCORE_PARTICLE_BASELINE,
    AVYCORE_RELEASE_BASELINE,
    BC_PRA,
    ENGINE_DESCRIPTORS,
    R_AVAFLOW,
    SNOWPACK,
    UPSTREAM_FLOWPY,
    canonical_engine_registry,
    descriptor_by_id,
)
from .comparison import COMPARATOR_VERSION, compare_runout_results
from . import contracts as _contracts
from .contracts import *  # noqa: F403 - this package intentionally re-exports its contracts
from .registry import (
    ApplicabilityDecision,
    EnginePlugin,
    EngineRegistry,
    EngineSelectionError,
    ReleaseEngine,
    RunoutEngine,
    SelectionPolicy,
    SelectionReport,
    SnowStateEngine,
    StaticEnginePlugin,
)

__all__ = [
    *_contracts.__all__,
    "AVAFRAME_COM1DFA",
    "AVAFRAME_FLOWPY",
    "AVYCORE_ALPHA_BASELINE",
    "AVYCORE_PARTICLE_BASELINE",
    "AVYCORE_RELEASE_BASELINE",
    "ApplicabilityDecision",
    "BC_PRA",
    "COMPARATOR_VERSION",
    "ENGINE_DESCRIPTORS",
    "EnginePlugin",
    "EngineRegistry",
    "EngineSelectionError",
    "R_AVAFLOW",
    "ReleaseEngine",
    "RunoutEngine",
    "SNOWPACK",
    "SelectionPolicy",
    "SelectionReport",
    "SnowStateEngine",
    "StaticEnginePlugin",
    "UPSTREAM_FLOWPY",
    "canonical_engine_registry",
    "compare_runout_results",
    "descriptor_by_id",
]
