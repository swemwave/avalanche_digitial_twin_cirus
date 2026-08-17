"""Deterministic engine registration, applicability checks, and selection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from .contracts import (
    AvailabilityStatus,
    EngineAvailability,
    EngineDescriptor,
    EngineRunRequest,
    StrictModel,
    canonical_json_bytes,
)


class EngineSelectionError(RuntimeError):
    """Raised when deterministic selection cannot produce an executable engine."""

    def __init__(self, message: str, report: "SelectionReport") -> None:
        super().__init__(message)
        self.report = report


@runtime_checkable
class EnginePlugin(Protocol):
    """Common metadata seam implemented by every snow/release/runout plugin."""

    @property
    def descriptor(self) -> EngineDescriptor: ...

    def availability(self) -> EngineAvailability: ...


@runtime_checkable
class SnowStateEngine(EnginePlugin, Protocol):
    def run_snow_state(self, request: EngineRunRequest, *, output_root: str) -> object: ...


@runtime_checkable
class ReleaseEngine(EnginePlugin, Protocol):
    def run_release(self, request: EngineRunRequest, *, output_root: str) -> object: ...


@runtime_checkable
class RunoutEngine(EnginePlugin, Protocol):
    def run_runout(self, request: EngineRunRequest, *, output_root: str) -> object: ...


class SelectionPolicy(StrictModel):
    """Explicit fallback behavior; there is no implicit "best effort" mode."""

    engine_order: tuple[str, ...] = ()
    fallback_on_unavailable: bool = False

    @model_validator(mode="after")
    def unique_order(self) -> "SelectionPolicy":
        if len(self.engine_order) != len(set(self.engine_order)):
            raise ValueError("Selection policy engine_order contains duplicates.")
        return self


class ApplicabilityDecision(StrictModel):
    engine_id: str
    status: str
    reasons: tuple[str, ...] = Field(min_length=1)


class SelectionReport(StrictModel):
    selection_sha256: str
    requested_engine_id: str | None
    selected_engine_id: str | None
    decisions: tuple[ApplicabilityDecision, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identity(self) -> "SelectionReport":
        payload = self.model_dump(mode="json", exclude={"selection_sha256"})
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if self.selection_sha256 != expected:
            raise ValueError("Selection report identity does not match its content.")
        selected = [item for item in self.decisions if item.status == "selected"]
        if self.selected_engine_id is None and selected:
            raise ValueError("Unselected report contains a selected decision.")
        if self.selected_engine_id is not None:
            if len(selected) != 1 or selected[0].engine_id != self.selected_engine_id:
                raise ValueError("Selected engine conflicts with applicability decisions.")
        return self


def _report(
    request: EngineRunRequest,
    selected_engine_id: str | None,
    decisions: list[ApplicabilityDecision],
) -> SelectionReport:
    content = {
        "requested_engine_id": request.requested_engine_id,
        "selected_engine_id": selected_engine_id,
        "decisions": [item.model_dump(mode="json") for item in decisions],
    }
    identity = hashlib.sha256(canonical_json_bytes(content)).hexdigest()
    return SelectionReport.model_validate({"selection_sha256": identity, **content})


def _input_failures(descriptor: EngineDescriptor, request: EngineRunRequest) -> list[str]:
    supplied = request.input_map()
    failures: list[str] = []
    for required in descriptor.required_inputs:
        provided = supplied.get(required.name)
        if provided is None or provided.status == "missing":
            if required.required:
                failures.append(
                    f"missing required input {required.name!r}; missing policy is {required.missing_policy}"
                )
            continue
        if provided.kind != required.kind:
            failures.append(
                f"input {required.name!r} kind {provided.kind.value!r} does not match "
                f"required {required.kind.value!r}"
            )
        if provided.unit != required.unit:
            failures.append(
                f"input {required.name!r} unit {provided.unit!r} does not match "
                f"required {required.unit!r}"
            )
    for validity in descriptor.parameter_validity:
        provided = supplied.get(validity.input_name)
        if provided is None or provided.status == "missing" or isinstance(provided.value, bool):
            continue
        if not isinstance(provided.value, (int, float)):
            failures.append(f"input {validity.input_name!r} is not numeric")
            continue
        value = float(provided.value)
        lower_failed = validity.lower is not None and (
            value < validity.lower or (value == validity.lower and not validity.lower_inclusive)
        )
        upper_failed = validity.upper is not None and (
            value > validity.upper or (value == validity.upper and not validity.upper_inclusive)
        )
        if lower_failed or upper_failed:
            left = "[" if validity.lower_inclusive else "("
            right = "]" if validity.upper_inclusive else ")"
            failures.append(
                f"input {validity.input_name!r} value {value:g} {validity.unit} is outside "
                f"declared range {left}{validity.lower}, {validity.upper}{right}"
            )
    for spatial in descriptor.spatial_applicability:
        crs_values = []
        for name in spatial.input_names:
            provided = supplied.get(name)
            if provided is None or provided.status == "missing":
                continue
            crs = provided.grid.crs if provided.grid is not None else provided.crs
            if crs is None:
                failures.append(f"spatial input {name!r} has no declared CRS")
                continue
            crs_values.append((name, crs))
            if crs.coordinate_order != spatial.coordinate_order:
                failures.append(
                    f"spatial input {name!r} coordinate order {crs.coordinate_order!r} does not "
                    f"match required {spatial.coordinate_order!r}"
                )
            if spatial.require_projected_metre_crs and (
                not crs.projected or crs.horizontal_unit != "m"
            ):
                failures.append(f"spatial input {name!r} requires a projected metre-based CRS")
        if spatial.require_same_crs and crs_values:
            reference_name, reference = crs_values[0]
            for name, crs in crs_values[1:]:
                if crs != reference:
                    failures.append(
                        f"spatial inputs {reference_name!r} and {name!r} require identical CRS contracts"
                    )
    return failures


class EngineRegistry:
    """In-memory plugin registry with stable, declared selection semantics."""

    def __init__(self) -> None:
        self._plugins: dict[str, EnginePlugin] = {}

    def register(self, plugin: EnginePlugin) -> None:
        descriptor = plugin.descriptor
        if descriptor.engine_id in self._plugins:
            raise ValueError(f"Engine {descriptor.engine_id!r} is already registered.")
        self._plugins[descriptor.engine_id] = plugin

    def plugin(self, engine_id: str) -> EnginePlugin:
        try:
            return self._plugins[engine_id]
        except KeyError as exc:
            raise KeyError(
                f"Unknown engine {engine_id!r}; registered: {', '.join(sorted(self._plugins))}"
            ) from exc

    def inventory(self) -> tuple[tuple[EngineDescriptor, EngineAvailability], ...]:
        return tuple(
            (self._plugins[key].descriptor, self._plugins[key].availability())
            for key in sorted(self._plugins)
        )

    def select(
        self,
        request: EngineRunRequest,
        *,
        policy: SelectionPolicy | None = None,
    ) -> tuple[EnginePlugin, SelectionReport]:
        """Select one engine from declared applicability and explicit fallback policy.

        An applicable engine with missing inputs always fails visibly.  An
        unavailable engine fails too unless the caller explicitly enabled
        ``fallback_on_unavailable``.  This prevents a high-fidelity request from
        silently becoming a simpler baseline computation.
        """

        policy = policy or SelectionPolicy()
        if request.requested_engine_id:
            if request.requested_engine_id not in self._plugins:
                decision = ApplicabilityDecision(
                    engine_id=request.requested_engine_id,
                    status="unknown_engine",
                    reasons=("The requested engine is not registered.",),
                )
                report = _report(request, None, [decision])
                raise EngineSelectionError(decision.reasons[0], report)
            ordered_ids = (request.requested_engine_id,)
        elif policy.engine_order:
            unknown = [item for item in policy.engine_order if item not in self._plugins]
            if unknown:
                decisions = [
                    ApplicabilityDecision(
                        engine_id=item,
                        status="unknown_engine",
                        reasons=("Selection policy references an unregistered engine.",),
                    )
                    for item in unknown
                ]
                report = _report(request, None, decisions)
                raise EngineSelectionError(decisions[0].reasons[0], report)
            ordered_ids = policy.engine_order
        else:
            ordered_ids = tuple(
                descriptor.engine_id
                for descriptor in sorted(
                    (item.descriptor for item in self._plugins.values()),
                    key=lambda item: (item.selection_priority, item.engine_id),
                )
            )

        decisions: list[ApplicabilityDecision] = []
        saw_stage_candidate = False
        for engine_id in ordered_ids:
            plugin = self._plugins[engine_id]
            descriptor = plugin.descriptor
            if descriptor.stage != request.stage:
                decisions.append(
                    ApplicabilityDecision(
                        engine_id=engine_id,
                        status="wrong_stage",
                        reasons=(
                            f"Engine stage {descriptor.stage.value!r} does not match "
                            f"request stage {request.stage.value!r}.",
                        ),
                    )
                )
                continue
            saw_stage_candidate = True
            if request.regime not in descriptor.supported_regimes:
                decisions.append(
                    ApplicabilityDecision(
                        engine_id=engine_id,
                        status="unsupported_regime",
                        reasons=(
                            f"Regime {request.regime.value!r} is not declared by this engine.",
                        ),
                    )
                )
                continue
            unsupported_outputs = sorted(
                set(request.requested_outputs) - set(descriptor.output_capabilities),
                key=lambda item: item.value,
            )
            if unsupported_outputs:
                decisions.append(
                    ApplicabilityDecision(
                        engine_id=engine_id,
                        status="unsupported_outputs",
                        reasons=(
                            "Engine does not declare requested outputs: "
                            + ", ".join(item.value for item in unsupported_outputs),
                        ),
                    )
                )
                continue
            failures = _input_failures(descriptor, request)
            if failures:
                decision = ApplicabilityDecision(
                    engine_id=engine_id,
                    status="missing_or_invalid_inputs",
                    reasons=tuple(failures),
                )
                decisions.append(decision)
                report = _report(request, None, decisions)
                raise EngineSelectionError(
                    f"Engine {engine_id!r} cannot run: {'; '.join(failures)}", report
                )
            availability = plugin.availability()
            if availability.engine_id != engine_id:
                decision = ApplicabilityDecision(
                    engine_id=engine_id,
                    status="invalid_plugin",
                    reasons=("Availability record names a different engine.",),
                )
                decisions.append(decision)
                report = _report(request, None, decisions)
                raise EngineSelectionError(decision.reasons[0], report)
            if availability.status != AvailabilityStatus.AVAILABLE:
                decision = ApplicabilityDecision(
                    engine_id=engine_id,
                    status=availability.status.value,
                    reasons=(availability.reason,),
                )
                decisions.append(decision)
                if policy.fallback_on_unavailable and not request.requested_engine_id:
                    continue
                report = _report(request, None, decisions)
                raise EngineSelectionError(
                    f"Engine {engine_id!r} is {availability.status.value}: {availability.reason}",
                    report,
                )
            decisions.append(
                ApplicabilityDecision(
                    engine_id=engine_id,
                    status="selected",
                    reasons=("All declared applicability, input, output, and availability checks passed.",),
                )
            )
            return plugin, _report(request, engine_id, decisions)

        reason = (
            "No registered engine has the requested stage."
            if not saw_stage_candidate
            else "No engine declares the requested regime and output capabilities."
        )
        if not decisions:
            decisions.append(
                ApplicabilityDecision(
                    engine_id="registry",
                    status="no_candidates",
                    reasons=(reason,),
                )
            )
        report = _report(request, None, decisions)
        raise EngineSelectionError(reason, report)


@dataclass(frozen=True)
class StaticEnginePlugin:
    """Catalogue-only plugin for a built-in or intentionally unavailable engine."""

    descriptor: EngineDescriptor
    availability_record: EngineAvailability

    def availability(self) -> EngineAvailability:
        return self.availability_record


__all__ = [
    "ApplicabilityDecision",
    "EnginePlugin",
    "EngineRegistry",
    "EngineSelectionError",
    "ReleaseEngine",
    "RunoutEngine",
    "SelectionPolicy",
    "SelectionReport",
    "SnowStateEngine",
    "StaticEnginePlugin",
]
