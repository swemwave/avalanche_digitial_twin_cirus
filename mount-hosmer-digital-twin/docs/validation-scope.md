# Runout validation scope

This document defines which runout component each metric can test. The Digital
Twin is an experimental research prototype, not an operational avalanche
forecast. It does not replace Avalanche Canada guidance or field assessment.
Release and hazard scores are relative indices, not probabilities.

## Required engine modes

Every validation prediction and every resulting metric record names both the
implementation (`engine`) and one of these component modes (`engine_mode`):

| `engine_mode` | Implementation | Active runout components |
|---|---|---|
| `alpha_only` | `fast_routing_alpha` | Downslope flux routing bounded by the empirical alpha angle and minimum-flux cutoff; no velocity dynamics |
| `dynamics_only` | `particle_ensemble_voellmy` | Voellmy particle dynamics, terrain-dependent friction, and seeded path spreading; no empirical alpha energy-line stop |
| `hybrid` | `particle_ensemble_voellmy` | The same particle dynamics plus the empirical per-particle alpha energy-line stop |

The serving application's `fast` alias selects `alpha_only`; its `advanced`
alias selects `hybrid`. `dynamics_only` is an explicit library/validation
ablation and is not currently a serving-API option.

## What a metric tests

Runout endpoint, horizontal-reach, and deposit-extent metrics from `alpha_only`
primarily test the configured empirical alpha angle, subject to DEM routing and
the minimum-flux cutoff. The same extent metrics from `hybrid` also primarily
test alpha because alpha supplies the particle stopping bound. They must not be
reported as validation of independent Voellmy runout extent.

Velocity and velocity-class metrics test the implemented Voellmy dynamics. Path
shape and lateral-spread metrics test the dynamics, terrain gradients,
terrain-dependent friction, and stochastic path-spreading implementation. In a
`hybrid` run, alpha can truncate those paths, so the metric record must retain
the mode and the interpretation must acknowledge that bound.

`dynamics_only` isolates the dynamics for software verification and ablation.
Its extent is not automatically a more physical runout prediction: the particle
model has no entrainment, spreading/thinning mass loss, or deposition physics,
and it may remain moving at the step cutoff or leave the AOI. Those outcomes are
failures or truncations, not endpoints.

## Evidence rigor and scoreability

`binary_mask_metrics` remains the strict known-absence path. Precision, F1, and
intersection-over-union require a complete registered survey domain with
explicit known-absence semantics. Positional uncertainty and missing model
inputs remain excluded and reported; neither is converted to a safe-looking
zero.

For a field holdout containing more than one event,
`binary_mask_cohort_metrics` requires exactly one prediction case for every
registered holdout event and every target and compatible survey-coverage polygon
within each event. It retains the strict per-event records and deliberately does
not pool IoU across grids or resolutions. The original single-event function
still rejects a partial multi-event cohort.

Strict field-holdout endpoint metrics likewise require a valid prediction for
every registered endpoint. A failed, escaped, truncated, or otherwise missing
prediction cannot be dropped while scoring the remaining endpoints; the cohort
is rejected and the missing run must be reported as a failure. Missing-prediction
coverage remains available for non-holdout software and diagnostic uses.

`positive_only_polygon_metrics` is a separate lower-rigor path for
`calibration_only` or `qualitative_comparison` evidence. It reports only the
fraction of comparable mapped-positive cells intersected by the prediction.
Unmapped cells are unknown, never negatives; consequently this record contains
no false-positive, precision, F1, or IoU field and cannot support an independent
field-validation claim. `calibration_only` retains the complete documented
scenario requirement used by the strict context. A `qualitative_comparison`
instead uses `QualitativePredictionContext`: it binds event, model, config,
bake, engine mode, AOI status, and an immutable run-configuration artifact but
does not require invented event-day snow or wind. Its metric record lists the
documented and missing historical scenario fields for every observation.

Any particle prediction with `particles_left_the_aoi > 0` is unscoreable in
either path. Any fast or particle footprint that contacts the evaluation-grid
boundary (`aoi_boundary_contact=true`) is also unscoreable even when no particle
escape count exists. The AOI must be enlarged and the event rerun. A map
boundary is never accepted as a runout endpoint, and a cropped footprint is
never compared as though it were complete.

The checked-in `alpha-only-real-events-v1` result was regenerated through
`positive_only_polygon_metrics` with a `QualitativePredictionContext` for every
scoreable run. Each metric is bound to a committed normalized observation,
dataset identity, evaluation grid, AOI status, and run-configuration identity.
The separately labelled predicted-to-mapped area ratio and release-cell overlap
decomposition remain diagnostics outside the public metric contract.

Validation manifests and evaluation grids currently accept only the reviewed
projected-metre CRS allowlist `EPSG:2056`, `EPSG:26911`, and `EPSG:32613`.
Allowlist membership does not verify a dataset's datum, epoch, axis order, or
coordinate transform; those remain lineage-review obligations. Adding another
CRS requires explicit review rather than accepting an arbitrary EPSG code.

Synthetic inclined planes, analytical solutions, determinism checks, and
resolution-convergence tests are **software verification**. Only preassigned,
held-out real events with reviewed lineage and appropriate observation quality
can support **field validation**, and only for the component and event regime
actually tested.

## Intentional numerical change — particle coordinate integration (dynamics)

**Component tested/changed:** particle coordinate integration in the Voellmy
dynamics, in both `dynamics_only` and `hybrid` modes. The particle integrator
previously advanced horizontal grid coordinates by the full slope-tangential
distance. It now projects that distance using the terrain grade along the
particle's actual travel direction. The factor equals the cosine of local slope
on the fall line and one along a contour; using the full fall-line slope for an
oblique path would incorrectly shorten lateral travel. This is a
coordinate-definition correction, not a validation improvement or evidence that
the engine is accurate. With seed `20260713`, the checked-in synthetic M0
advanced-engine summaries changed as follows; release summaries and all
fast-engine numerical summaries remained identical:

| Synthetic case | Core area (m²) | Envelope area (m²) | Peak velocity (m/s) | Particles leaving AOI |
|---|---:|---:|---:|---:|
| loaded | 5,675 → 5,425 (-4.405%) | 9,375 → 8,950 (-4.533%) | 21.547728 → 21.666599 (+0.552%) | 300 → 300 |
| missing-data barrier | 1,625 → 3,200 (+96.923%) | 2,675 → 5,350 (+100.000%) | 19.695065 → 23.243616 (+18.017%) | 0 → 26 |
| AOI boundary | 4,675 → 4,575 (-2.139%) | 7,775 → 7,525 (-3.215%) | 22.460300 → 22.776915 (+1.410%) | 300 → 300 |

The discontinuous missing-data-barrier fixture changes trajectory branches
nonlinearly after the coordinate correction. Its new AOI escapes are reported,
not hidden; under the validation contract that event is unscoreable. Fast-engine
artifact hashes also changed because the artifact now records the explicit
`alpha_only` mode, although its masks and summaries did not change.

An intermediate implementation applied the full fall-line cosine to every path.
The oblique software-verification case exposed that contour-parallel travel must
not be shortened. Replacing it with the directional-grade projection changed the
three M0 cases as follows; this second characterization is retained so the audit
trail does not hide the refinement:

| Synthetic case | Core area (m²) | Envelope area (m²) | Peak velocity (m/s) | Particles leaving AOI | Advanced output SHA-256 |
|---|---:|---:|---:|---:|---|
| loaded | 5,475 → 5,425 (-0.913%) | 8,950 → 8,950 (0%) | 21.666599 → 21.666599 (0%) | 300 → 300 | `013ab060...d41d0` → `89cae4d1...020e3` |
| missing-data barrier | 3,225 → 3,200 (-0.775%) | 5,375 → 5,350 (-0.465%) | 23.044790 → 23.243616 (+0.863%) | 16 → 26 | `bd5a4fcd...aa66e` → `4eabef8c...d1b8a` |
| AOI boundary | 4,600 → 4,575 (-0.543%) | 7,525 → 7,525 (0%) | 22.776915 → 22.776915 (0%) | 300 → 300 | `cadae1c2...aca5` → `f8c98dc4...3df4` |

A portable-AOI wording correction changed only the warning serialized in
three advanced-engine artifacts: “12 x 12 km AOI” became “configured AOI”. This
is component metadata for AOI truncation, not a dynamics change or validation
improvement. Replaying with the prior sentence reproduces each prior hash
exactly; all arrays, stopping points, other metadata, and numerical summaries
are unchanged:

| Synthetic case | Advanced output SHA-256 |
|---|---|
| loaded | `8d596028...37d659` → `013ab060...d41d0` |
| missing-data barrier | `0b80c069...79f8` → `bd5a4fcd...aa66e` |
| AOI boundary | `71d69048...9284` → `cadae1c2...aca5` |

## Outside the present claim

Extent agreement does not validate velocity fields, entrainment, deposition
mass, impact pressure, wet-snow physics, or the powder-cloud regime. No result
may broaden a validation claim from one component, mountain, terrain class,
event type, or scale to another without corresponding held-out evidence.
