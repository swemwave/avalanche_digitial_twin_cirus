"""Deterministic advisories from scenario records the release model cannot use.

The release model reasons about *terrain capability* under new snow and wind. It
has no snowpack layers, so it cannot see a buried weak layer, an ECTP result, or a
whumpf underfoot -- which are, between them, the evidence that matters most in real
avalanche decision-making.

The obvious-looking fix is to multiply the index by some weak-layer factor. This
module deliberately does not do that. There is no published mapping from a
stability-test score to *this* model's index and no Mount Hosmer calibration, so any
such coefficient would be invented, and an index that silently contains an invented
snowpack term is worse than one that admits it has none.

Instead every such record produces an **advisory**: a deterministic, rule-derived
statement of what was recorded, what the model does with it (nothing, numerically),
and what standard practice says about that evidence. Advisories change what the user
reads; they never change what the model computes. The rules below are pure
bookkeeping over the recorded inputs plus long-standing avalanche-education
practice -- they introduce no new physics and no new hazard scale.

Severity orders presentation only:

``critical``
    Direct evidence of instability, or a term the user should not misread. The
    recorded observation outranks a terrain model in standard practice.
``warning``
    Recorded evidence the model cannot ingest, or an active input whose effect is
    easy to misread.
``note``
    Context retained for provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from .hazard.conditions import RAIN_SNOW_UPPER_C

__all__ = ["Advisory", "SEVERITY_ORDER", "build_advisories"]

Severity = Literal["critical", "warning", "note"]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "warning": 1, "note": 2}

#: Weak-layer types that persist and are the classic drivers of destructive slab
#: avalanches. Naming them is avalanche-education vocabulary, not a model term.
PERSISTENT_WEAK_LAYERS = frozenset({"surface_hoar", "facets", "depth_hoar"})

#: Stability-test results that indicate propagation. Again: vocabulary, not a score.
PROPAGATING_TEST_RESULTS = frozenset({"ECTP", "PST_end"})

#: The three classic "obvious clues". Any one of them is direct evidence that the
#: snowpack is currently unstable.
DIRECT_INSTABILITY_SIGNS = {
    "whumpfing": "Whumpfing (collapsing) underfoot",
    "shooting_cracks": "Shooting cracks",
    "recent_avalanche_activity": "Recent avalanche activity",
}


@dataclass(frozen=True)
class Advisory:
    """One deterministic statement derived from the recorded scenario."""

    advisory_id: str
    severity: Severity
    title: str
    detail: str
    parameters: tuple[str, ...] = ()
    #: True where standard practice treats the recorded evidence as outranking a
    #: terrain model's number. Never means the number was altered.
    overrides_model: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisory_id": self.advisory_id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "parameters": list(self.parameters),
            "overrides_model": self.overrides_model,
            "changed_the_number": False,
        }


def _value(records: Mapping[str, Any], parameter: str) -> Any:
    record = records.get(parameter)
    if record is None:
        return None
    status = getattr(record, "status", None) or record.get("status")
    if status == "unknown":
        return None
    value = getattr(record, "value", None)
    if value is None and isinstance(record, Mapping):
        value = record.get("value")
    return value


def _label(records: Mapping[str, Any], parameter: str) -> str:
    record = records.get(parameter)
    if isinstance(record, Mapping):
        return str(record.get("label") or parameter)
    return str(getattr(record, "label", None) or parameter)


def build_advisories(
    records: Mapping[str, Any],
    *,
    new_snow_cm: float | None = None,
    snow_fraction: float | None = None,
) -> list[Advisory]:
    """Derive every advisory implied by one resolved scenario.

    ``records`` maps parameter name -> the scenario input (anything exposing
    ``.status`` and ``.value``). Order is deterministic: severity first, then
    advisory id, so identical inputs always produce an identical list.
    """
    advisories: list[Advisory] = []

    # --- Direct instability signs ------------------------------------------
    observed = [
        label
        for parameter, label in DIRECT_INSTABILITY_SIGNS.items()
        if _value(records, parameter) is True
    ]
    if observed:
        listed = "; ".join(observed)
        advisories.append(
            Advisory(
                advisory_id="direct_instability_signs",
                severity="critical",
                title=(
                    f"{len(observed)} direct instability sign"
                    f"{'' if len(observed) == 1 else 's'} recorded"
                ),
                detail=(
                    f"You recorded: {listed}. The model cannot see any of these -- it reasons "
                    "about terrain capability under new snow and wind, and its index does not "
                    "reflect them. These are direct evidence that the snowpack is unstable "
                    "right now, and standard practice treats them as overriding a terrain "
                    "model. Read the number below as terrain context only."
                ),
                parameters=tuple(
                    parameter
                    for parameter in DIRECT_INSTABILITY_SIGNS
                    if _value(records, parameter) is True
                ),
                overrides_model=True,
            )
        )

    # --- Persistent weak layers --------------------------------------------
    weak_type = _value(records, "weak_layer_type")
    weak_depth = _value(records, "weak_layer_depth")
    if weak_type is not None or weak_depth is not None:
        persistent = isinstance(weak_type, str) and weak_type in PERSISTENT_WEAK_LAYERS
        where = f" at {float(weak_depth):.0f} cm" if weak_depth is not None else ""
        described = str(weak_type).replace("_", " ") if weak_type is not None else "a weak layer"
        advisories.append(
            Advisory(
                advisory_id="weak_layer_recorded",
                severity="critical" if persistent else "warning",
                title=(
                    f"Persistent weak layer recorded: {described}{where}"
                    if persistent
                    else f"Weak layer recorded: {described}{where}"
                ),
                detail=(
                    f"You recorded {described}{where}. The model has no snowpack layers and "
                    "cannot resolve weak-layer failure, so this record did not change the "
                    "index by any amount."
                    + (
                        " Persistent weak layers are the mechanism behind most avalanche "
                        "fatalities and can keep terrain dangerous long after a storm ends; "
                        "the model is blind to exactly that."
                        if persistent
                        else ""
                    )
                ),
                parameters=tuple(
                    parameter
                    for parameter in ("weak_layer_type", "weak_layer_depth")
                    if _value(records, parameter) is not None
                ),
                overrides_model=persistent,
            )
        )

    # --- Stability tests -----------------------------------------------------
    test_result = _value(records, "stability_test_result")
    if test_result is not None:
        propagating = isinstance(test_result, str) and test_result in PROPAGATING_TEST_RESULTS
        advisories.append(
            Advisory(
                advisory_id="stability_test_recorded",
                severity="critical" if propagating else "warning",
                title=f"Stability test recorded: {test_result}",
                detail=(
                    f"A {test_result} result is retained with its full provenance but is not "
                    "numerically ingested -- no test score changes this index."
                    + (
                        " A propagating result is direct evidence that a fracture can travel, "
                        "which is what turns a failure into a slab avalanche."
                        if propagating
                        else ""
                    )
                ),
                parameters=("stability_test_result",),
                overrides_model=propagating,
            )
        )

    # --- Rain on snow --------------------------------------------------------
    temperature = _value(records, "air_temperature")
    if (
        temperature is not None
        and float(temperature) >= RAIN_SNOW_UPPER_C
        and (new_snow_cm or 0.0) > 0.0
    ):
        advisories.append(
            Advisory(
                advisory_id="rain_on_snow",
                severity="critical",
                title=f"Precipitation classified as rain at {float(temperature):.0f} degC",
                detail=(
                    f"At {float(temperature):.0f} degC none of the "
                    f"{float(new_snow_cm or 0.0):.0f} cm of new precipitation is classified as "
                    "snow, so the dry-slab loading term dropped and the index fell. That is a "
                    "statement about dry-slab loading, NOT about hazard: rain-on-snow, wet-loose "
                    "and wet-slab avalanches are real and this dry-slab model does not represent "
                    "them at all. Do not read the lower number as a safer day."
                ),
                parameters=("air_temperature", "new_snow_depth"),
                overrides_model=True,
            )
        )
    elif (
        temperature is not None
        and snow_fraction is not None
        and 0.0 < float(snow_fraction) < 1.0
    ):
        advisories.append(
            Advisory(
                advisory_id="mixed_precipitation",
                severity="warning",
                title=f"Mixed rain and snow at {float(temperature):.0f} degC",
                detail=(
                    f"{float(snow_fraction):.0%} of the new precipitation is classified as snow, "
                    "so the dry-slab loading term is reduced proportionally. The remainder is "
                    "treated as rain, whose wet-snow consequences this model does not represent."
                ),
                parameters=("air_temperature", "new_snow_depth"),
            )
        )

    # --- Runout overrides ----------------------------------------------------
    regime = _value(records, "flow_regime")
    if isinstance(regime, str) and regime not in ("", "dry_slab", "unknown_regime"):
        advisories.append(
            Advisory(
                advisory_id="flow_regime_selected",
                severity="warning",
                title=f"Runout using the {regime.replace('_', ' ')} friction set",
                detail=(
                    f"The runout engines were pointed at the published {regime.replace('_', ' ')} "
                    "friction constants instead of the dry-snow defaults. They remain dry-snow "
                    "engines running on different UNCALIBRATED numbers -- this does not add "
                    "wet-snow or powder-cloud physics, and none of the sets is fitted to Mount "
                    "Hosmer."
                ),
                parameters=("flow_regime",),
            )
        )

    alpha = _value(records, "runout_alpha_angle")
    if alpha is not None:
        advisories.append(
            Advisory(
                advisory_id="alpha_angle_override",
                severity="warning",
                title=f"Angle of reach overridden to {float(alpha):.0f} degrees",
                detail=(
                    "This runout used your angle of reach instead of the configured regional "
                    "value, clamped to the reviewed 15-40 degree envelope. It is a sensitivity "
                    "run: your angle is no more calibrated to Mount Hosmer than the default it "
                    "replaced, and a smaller angle always produces a longer runout."
                ),
                parameters=("runout_alpha_angle",),
            )
        )

    # --- Retained context ----------------------------------------------------
    context = [
        parameter
        for parameter in (
            "snow_depth",
            "snow_water_equivalent",
            "slab_depth",
            "trigger_type",
            "release_depth",
            "avalanche_density",
        )
        if _value(records, parameter) is not None
    ]
    if context:
        advisories.append(
            Advisory(
                advisory_id="context_recorded",
                severity="note",
                title=f"{len(context)} further record{'' if len(context) == 1 else 's'} retained",
                detail=(
                    "Retained with full provenance and reported in the scenario record: "
                    + ", ".join(_label(records, parameter) for parameter in context)
                    + ". None of them is an input to the current release or runout equations."
                ),
                parameters=tuple(context),
            )
        )

    advisories.sort(key=lambda item: (SEVERITY_ORDER[item.severity], item.advisory_id))
    return advisories


def advisory_summary(advisories: Iterable[Advisory]) -> dict[str, Any]:
    """Counts and the headline flag the result card leads with."""
    items = list(advisories)
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for item in items:
        counts[item.severity] += 1
    return {
        "count": len(items),
        "count_by_severity": counts,
        "field_evidence_overrides_model": any(item.overrides_model for item in items),
        "statement": (
            "Advisories are derived deterministically from what you recorded. None of them "
            "changed the computed index -- the model has no term for this evidence, and no "
            "coefficient was invented to give it one."
        ),
    }
