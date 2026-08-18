"""Fail-closed availability adapter for runout engines not yet normalized.

Flow-Py now has a real adapter in ``flowpy.py``; only r.avaflow remains here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from avycore.engines import R_AVAFLOW, AvailabilityStatus, EngineAvailability

from .process import ExternalModelProcessError, file_sha256


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


__all__ = ["RAvaFlowAvailabilityAdapter"]
