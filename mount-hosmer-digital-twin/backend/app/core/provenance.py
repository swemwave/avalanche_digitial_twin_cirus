"""Provenance vocabulary and value wrappers.

Every important input to the models carries a provenance tag so that the API and
the UI can always answer "was this measured, or did we make it up?". The
distinction is not cosmetic: an interpolated SWE index and an observed SWE
measurement must never be presentable as the same thing.

Ordering matters. :data:`PROVENANCE_CONFIDENCE` maps each tag to the confidence
weight it contributes; ``unavailable`` contributes nothing and is *excluded from
the denominator* rather than being scored as zero (invariant I3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Provenance = Literal[
    "observed",
    "downloaded",
    "derived",
    "interpolated",
    "modelled",
    "user_supplied",
    "unavailable",
]

PROVENANCE_VALUES: tuple[Provenance, ...] = (
    "observed",
    "downloaded",
    "derived",
    "interpolated",
    "modelled",
    "user_supplied",
    "unavailable",
)

#: How much each provenance class is trusted, 0-1. Used by the confidence
#: engine. An observation at the point of interest is the gold standard; a value
#: modelled from a distant proxy is not.
PROVENANCE_CONFIDENCE: dict[Provenance, float] = {
    "observed": 1.00,
    "downloaded": 0.90,
    "derived": 0.85,
    "interpolated": 0.55,
    "modelled": 0.45,
    "user_supplied": 0.30,
    "unavailable": 0.00,
}

PROVENANCE_LABELS: dict[Provenance, str] = {
    "observed": "Measured at a station",
    "downloaded": "Downloaded from an authoritative source",
    "derived": "Computed from measured data",
    "interpolated": "Interpolated from a distant or sparse source",
    "modelled": "Estimated by a model, not measured",
    "user_supplied": "Entered by the user for a hypothetical scenario",
    "unavailable": "No data available",
}


class ProvenanceError(ValueError):
    """Raised when a value is tagged with an unknown provenance."""


def check(provenance: str) -> Provenance:
    if provenance not in PROVENANCE_VALUES:
        raise ProvenanceError(
            f"Unknown provenance {provenance!r}. Expected one of: {', '.join(PROVENANCE_VALUES)}"
        )
    return provenance  # type: ignore[return-value]


@dataclass(frozen=True)
class Value:
    """A single model input, with everything needed to defend it.

    ``value`` is ``None`` exactly when ``provenance == "unavailable"``. Callers
    must branch on ``is_available`` rather than substituting a default, because a
    missing snowfall reading means "unknown", not "no snowfall".
    """

    name: str
    value: float | str | None
    units: str
    provenance: Provenance
    source: str
    timestamp_utc: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        check(self.provenance)
        if self.provenance == "unavailable" and self.value is not None:
            raise ProvenanceError(
                f"{self.name!r} is tagged 'unavailable' but carries a value ({self.value!r})."
            )
        if self.provenance != "unavailable" and self.value is None:
            raise ProvenanceError(
                f"{self.name!r} has no value but is tagged {self.provenance!r}; "
                f"use provenance='unavailable' for missing data."
            )

    @property
    def is_available(self) -> bool:
        return self.provenance != "unavailable"

    @property
    def is_measured(self) -> bool:
        """True only for values actually measured somewhere, not estimated."""
        return self.provenance in {"observed", "downloaded"}

    @property
    def confidence(self) -> float:
        return PROVENANCE_CONFIDENCE[self.provenance]

    @property
    def numeric(self) -> float | None:
        return float(self.value) if isinstance(self.value, (int, float)) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "units": self.units,
            "provenance": self.provenance,
            "provenance_label": PROVENANCE_LABELS[self.provenance],
            "source": self.source,
            "timestamp_utc": self.timestamp_utc,
            "detail": self.detail,
            "available": self.is_available,
            "measured": self.is_measured,
        }


def unavailable(name: str, units: str, source: str, reason: str) -> Value:
    """Build a missing-data Value. The reason is surfaced to the user."""
    return Value(
        name=name,
        value=None,
        units=units,
        provenance="unavailable",
        source=source,
        detail=reason,
    )


def observed(
    name: str,
    value: float | str,
    units: str,
    source: str,
    timestamp_utc: str | None = None,
    detail: str | None = None,
) -> Value:
    return Value(name, value, units, "observed", source, timestamp_utc, detail)


def derived(
    name: str,
    value: float | str,
    units: str,
    source: str,
    timestamp_utc: str | None = None,
    detail: str | None = None,
) -> Value:
    return Value(name, value, units, "derived", source, timestamp_utc, detail)


def interpolated(
    name: str,
    value: float | str,
    units: str,
    source: str,
    timestamp_utc: str | None = None,
    detail: str | None = None,
) -> Value:
    return Value(name, value, units, "interpolated", source, timestamp_utc, detail)


def modelled(
    name: str,
    value: float | str,
    units: str,
    source: str,
    timestamp_utc: str | None = None,
    detail: str | None = None,
) -> Value:
    return Value(name, value, units, "modelled", source, timestamp_utc, detail)


def user_supplied(
    name: str,
    value: float | str,
    units: str,
    detail: str | None = None,
) -> Value:
    return Value(
        name,
        value,
        units,
        "user_supplied",
        source="User-defined scenario input",
        detail=detail or "Hypothetical value entered by the user; not a measurement.",
    )


def summarize(values: list[Value]) -> dict[str, Any]:
    """Aggregate a set of inputs into a provenance/confidence summary.

    Unavailable inputs are counted and reported but are *not* averaged into the
    confidence figure as zeros -- they reduce ``completeness`` instead. This
    keeps "we have three good inputs" distinguishable from "we have three good
    inputs and seven we invented".
    """
    total = len(values)
    available = [value for value in values if value.is_available]
    counts: dict[str, int] = {name: 0 for name in PROVENANCE_VALUES}
    for value in values:
        counts[value.provenance] += 1

    mean_confidence = (
        sum(value.confidence for value in available) / len(available) if available else 0.0
    )
    return {
        "input_count": total,
        "available_count": len(available),
        "missing_count": total - len(available),
        "measured_count": sum(1 for value in values if value.is_measured),
        "completeness": round(len(available) / total, 4) if total else 0.0,
        "mean_input_confidence": round(mean_confidence, 4),
        "counts_by_provenance": counts,
        "missing_inputs": [
            {"name": value.name, "reason": value.detail} for value in values if not value.is_available
        ],
    }
