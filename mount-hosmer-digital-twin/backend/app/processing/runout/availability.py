"""Fail-closed availability adapters for runout engines not yet normalized."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from avycore.engines import (
    AVAFRAME_FLOWPY,
    R_AVAFLOW,
    AvailabilityStatus,
    EngineAvailability,
)

from .process import ExternalModelProcessError, file_sha256, probe_python_distribution


@dataclass(frozen=True)
class AvaFrameFlowPyAvailabilityAdapter:
    """Detect com4FlowPy, but refuse physics until its outputs are characterized."""

    python_executable: str | Path

    @property
    def descriptor(self):
        return AVAFRAME_FLOWPY

    def availability(self) -> EngineAvailability:
        probe = probe_python_distribution(
            self.python_executable,
            engine_id=self.descriptor.engine_id,
            distribution="avaframe",
            import_name="avaframe.com4FlowPy.com4FlowPy",
        )
        if probe.status != AvailabilityStatus.AVAILABLE:
            return probe
        return EngineAvailability(
            engine_id=self.descriptor.engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=(
                "AvaFrame com4FlowPy is installed, but this repository has no characterized, "
                "unit-verified normalized output adapter; execution is disabled."
            ),
            detected_version=probe.detected_version,
            executable_sha256=probe.executable_sha256,
        )

    def run_runout(self, *args, **kwargs):
        raise ExternalModelProcessError("adapter_disabled", self.availability().reason)


@dataclass(frozen=True)
class RAvaFlowAvailabilityAdapter:
    """Isolated executable boundary; never guesses a configuration or output map."""

    executable: str | Path | None = None

    @property
    def descriptor(self):
        return R_AVAFLOW

    def availability(self) -> EngineAvailability:
        if self.executable is None:
            return EngineAvailability(
                engine_id=self.descriptor.engine_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason="No version-bound r.avaflow executable or container image is configured.",
            )
        path = Path(self.executable).resolve()
        if not path.is_file():
            return EngineAvailability(
                engine_id=self.descriptor.engine_id,
                status=AvailabilityStatus.UNAVAILABLE,
                reason=f"Configured r.avaflow executable does not exist: {path}",
            )
        return EngineAvailability(
            engine_id=self.descriptor.engine_id,
            status=AvailabilityStatus.MISCONFIGURED,
            reason=(
                "An r.avaflow executable exists, but its exact version/licence closure and "
                "normalized output parser have not been verified; execution is disabled."
            ),
            executable_sha256=file_sha256(path),
        )

    def run_runout(self, *args, **kwargs):
        raise ExternalModelProcessError("adapter_disabled", self.availability().reason)


__all__ = ["AvaFrameFlowPyAvailabilityAdapter", "RAvaFlowAvailabilityAdapter"]
