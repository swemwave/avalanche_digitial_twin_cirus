# Public-Data Field-Validation Execution Plan

## Decision and claim boundary

No finite experiment can guarantee that an avalanche model will pass validation,
and validation in one region cannot guarantee accuracy at Mount Hosmer. This plan
can guarantee a reproducible and falsifiable test if its evidence gates are met.
The result may validate a narrowly named component, or it may demonstrate that the
component must be rejected or replaced.

The present result is unambiguous:

- `is_validated` is `false` and the strict field holdout contains **N = 0**
  eligible events;
- seven analytic/invariant tests verify selected numerical behavior but provide no
  field accuracy evidence;
- the frozen GEODAR point-particle test failed all-event acceptance: 0 of 71 events
  passed all three kinematic criteria;
- the frozen 1999 Swiss storm-window hindcast captured 24.81% of mapped events,
  compared with 62.40% for its same-area slope baseline, and therefore failed;
- eight greater-Davos events remain positive-only qualitative comparisons;
- the public-source audit did not find a ready-made dataset satisfying the current
  all-in-one strict contract.

These failed experiments are useful evidence. They must not be rerun with changed
parameters and relabelled as holdouts. Their events are development evidence from
now on.

The first attainable claim is deliberately narrow:

> On held-out, dry dense-slab events in the named external study regions, the named
> release or conditional-runout component did or did not outperform its frozen
> baseline within the documented observation and input uncertainty.

External success would not establish Mount Hosmer validity. A later local transfer
test using independent Mount Hosmer or nearby Elk Valley events is required for
that claim. This remains an experimental research prototype, not an operational
forecast, and it does not replace Avalanche Canada guidance or field assessment.
All existing scores remain relative indices rather than probabilities.

## How this differs from the current validation approach

| Dimension | Current repository method | Method adopted from the supplied research | Required change |
|---|---|---|---|
| Scientific scope | The serving model is a simplified dry-slab terrain/loading index, while the latest hindcast also explored several regimes. | Freeze one avalanche regime and one observable at a time. | Start with dry dense-slab avalanches; do not pool wet, loose, glide, or powder events. |
| Model decomposition | Engine modes and ablations are recorded, but the strict evidence contract still couples many event and scenario requirements. | Score release, conditional runout, and end-to-end behavior as separate experiments. | Add component-specific evidence profiles and validation statuses. |
| Release physics | `risk.py` is an uncalibrated slope/new-snow/wind rule without snowpack stability. The broader release hindcast failed its frozen baseline comparison. | Compare against a slope-only and a terrain/snow/wind potential-release baseline; require snow stability before claiming occurrence prediction. | Keep the current score as a baseline. Build a snow-state-driven candidate or limit the claim to potential release-area delineation. |
| Runout physics | Fast routing is bounded by empirical alpha; advanced routing uses dimensionless point particles with Voellmy friction and no mass balance, entrainment, flow depth, or deposition. Its GEODAR test failed. | Use a documented depth-integrated avalanche solver with observed-release conditional tests. | Integrate AvaFrame `com1DFA` offline as the primary candidate; retain alpha routing and the current particle engine as baselines/ablations. |
| Observations | Current strict validation rejects all remote-sensing interpretation and requires known-absence coverage plus a complete scenario at dataset level. No public package passed. | Use event-specific public observations, quality tiers, blind digitization, explicit uncertainty, and positive/unlabelled treatment when negatives are not credible. | Permit reviewed, independent remote-sensing reference geometry when its method and uncertainty are explicit; require known absence only for metrics that use negatives. |
| Event design | Existing work includes immutable blind splits, but the largest tests use cumulative storm-window positive outlines and coarse regional forcing. | Curate discrete events with identifiable release and runout evidence, then split by storm, mountain, and path. | Build a new event-level cohort before fitting. Existing viewed events cannot be final holdout. |
| Baselines | Random and same-area slope baselines were used in the large hindcasts, but not as a uniform component contract. | Every candidate must beat a declared simple baseline. | Require slope-only/PRA baselines for release and alpha/constant-parameter Voellmy baselines for runout. |
| Uncertainty | Current comparisons include parameter sweeps and strict masking, but not calibrated predictive ensembles. | Propagate observation, release, terrain, parameter, and structural uncertainty. | Add reproducible ensembles and report empirical interval coverage; ensemble frequency is not probability until calibrated. |
| Reproducibility | Hashes, immutable manifests, masks, identity binding, deterministic replay, and holdout isolation are already strong. | Freeze inputs, code, parameters, groups, seeds, and metrics before holdout. | Reuse and extend the current infrastructure; do not replace it. |

The largest practical change is therefore not a new metric. It is replacing the
single all-or-nothing validation gate with evidence profiles matched to the
component being tested, while preserving the repository's strong lineage,
masking, identity, and anti-leakage controls.

## Target cohort and study design

### Initial scope

- Avalanche regime: dry dense-slab only.
- Release target: observed crown/release area during a bounded event window.
- Conditional-runout target: dense-flow deposit footprint and distal toe, given
  an independently observed release polygon and a predeclared release-depth
  distribution.
- End-to-end target: the same runout observations, but initialized from the
  candidate release model rather than the observed release.
- Exclusions: wet-snow, dry-loose, glide/full-depth, cornice-dominated, powder-
  cloud-dominated, overlapping inseparable paths, unknown event windows, and
  runouts truncated by the evaluation AOI.

### Cohort sizes

Use two gates:

1. **Initial external-validation MVP:** at least 12 scoreable events from at
   least two independent mountains and three independent storm cycles, with at
   least six untouched holdout events and both mountains represented in
   development and holdout. This is an initial result, not broad validation.
2. **Defensible external study:** target 24–40 scoreable events from at least
   three mountains and four storm cycles, with at least 12 untouched holdout
   events. Split and bootstrap by storm/mountain/path, never by pixels.

If fewer than 12 events meet the applicable component evidence profile, stop.
Publish the acquisition/eligibility result and do not fit the model or claim
field validation.

## Component-specific evidence profiles

The validation contract must require only evidence relevant to the claim while
never weakening provenance or uncertainty requirements.

### Profile R — Release detection

Required for positive-location metrics:

- bounded event time, avalanche regime, and independent release geometry;
- release-boundary method, acquisition resolution, CRS/transform lineage, and
  quantified uncertainty;
- a DEM appropriate to release-terrain evaluation, with acquisition epoch and
  vertical/surface mismatch treatment;
- storm, mountain, path, and event identifiers.

Additionally required for precision, false-alarm, specificity, IoU over the
full survey, PR-AUC, Brier score, or calibration:

- an interpretable survey/imagery coverage polygon;
- explicit complete-search semantics for the declared avalanche target and
  time window;
- recorded cloud, shadow, layover, forest, and other detection masks.

When those negatives are unavailable, the event is positive/unlabelled. Only
positive coverage, centroid/boundary error, and other metrics that do not invent
negative labels may be reported.

### Profile C — Conditional runout

Required:

- independent release polygon;
- observed or bounded release thickness and density, or explicit distributions
  frozen without using the observed runout;
- dense-flow deposit polygon and/or terminal endpoint;
- feature-specific uncertainty and observation method;
- suitable DEM, AOI-completeness proof, CRS/units/lineage, and event regime.

A complete negative avalanche-occurrence survey and event new-snow/wind values
are not required when they are not inputs to this conditional simulation. A
surveyed deposit domain is still required for footprint false-positive/IoU
metrics. An endpoint can be scored without that polygon when the observation
method establishes that it is the terminal dense-flow toe.

### Profile E — End-to-end release plus runout

Required:

- every applicable requirement from Profiles R and C;
- event forcing and snow-state inputs actually used by the release model, with
  units, UTC intervals, uncertainty, and spatial representativeness;
- no observed release geometry supplied to the prediction path;
- a frozen rule for converting release output and uncertainty to solver initial
  conditions.

Passing Profile C does not imply passing Profile R or E. Every result and API
status must name the component, regime, region, model version, and evidence
profile that it actually tested.

### Pre-intake triage of partial deliveries

The owner request instructs data owners to leave an unavailable observation
missing rather than infer, substitute, back-calculate, or model-derive it. An
owner who complies produces a delivery that `FieldValidationOwnerEvent` rejects,
because every evidence block on that model is required. The only thing the
strict contract can return is one validation dump for the whole file, which does
not tell the owner which event to fix.

`avycore.validation.intake_triage` closes that gap without touching the gate. It
runs the unmodified strict contract and attributes each rejection to an event,
the evidence profile it blocks, the exact schema path, and one of the existing
`ExclusionReason` literals. `scripts/validation/triage_field_validation_owner_delivery.py`
writes the report as an immutable artifact and renders the per-event list to
return to the owner.

Triage is advisory and assigns nothing: no eligibility, no trust, no partition
membership, and no permission to predict. A profile reported as supported means
only that the evidence has the required shape; it is not an accepted component,
and eligibility still requires the complete strict contract, two independent
blinded human reviews, adjudication of every event including exclusions, a
frozen grouped split, and a sealed holdout. Triage never relaxes a validator,
and it hands the real cohort gate only deliveries the strict contract accepted.

Profile E is reported as unsupported for every delivery, because the
owner-delivery schema carries no field for event forcing, snow state, or the
release-to-solver conversion rule. That is a scope statement about the schema
rather than a shortfall in any owner's data, and the report says so.

## Ordered execution

### 1. Freeze the protocol before acquiring the final cohort

Create a versioned JSON experiment specification under
`validation-data/experiments/` containing:

- the scope and exclusions above;
- observable definitions, including the flow-depth/deposit threshold used to
  turn simulation output into a footprint;
- evidence profile, inclusion/exclusion rules, and quality tiers;
- draft acceptance criteria, metrics, baselines, ensemble variables, grouping
  rules, and random seeds;
- software and data identities that will be filled at the final freeze;
- a rule that any event viewed in a prediction/model overlay is permanently
  development data.

Do not use a holdout polygon to choose a flow threshold, alpha angle, release
threshold, friction parameter, DEM smoothing rule, or release-depth prior.

### 2. Implement validation-contract v3 before adding evidence

Update `packages/avycore/src/avycore/validation/` and its tests to add:

- `component_tested`: release, conditional runout, or end to end;
- `mountain_id`, `path_id`, `storm_cycle_id`, and `event_id` grouping fields;
- component-specific evidence profiles R, C, and E;
- per-feature observation method, confidence, horizontal uncertainty, survey
  date, source resolution, and detection limitations;
- release thickness/density values or distributions with units and provenance;
- DEM acquisition epoch, vertical datum, and event-surface mismatch statement;
- a reviewed remote-sensing evidence class that can be quantitative only when
  annotation was completed blind to model output and its uncertainty is
  characterized;
- an explicit positive/unlabelled state that cannot expose negative-dependent
  metrics;
- component-scoped trust and validation status instead of one global boolean.

Retain the current requirements for immutable source hashes, normalized
projected-metre coordinates, coordinate order, masks, complete lineage,
prediction identity, complete holdout membership, and rejection of AOI escape.

Add tests proving that each metric accepts only its required evidence profile
and rejects leakage, missing observations, invented negatives, unquantified
geometry, CRS/unit errors, and partial holdout cohorts.

### 3. Build a candidate funnel from public sources

Start with 40–60 candidates; do not select on preliminary model fit.

1. Use NVE RegObs as an event-discovery/time/type source, not automatically as
   final geometry truth.
2. Prioritize high-competence Norwegian records with distinct dry-slab crowns
   and stops, then find public pre/post optical or SAR imagery and the best
   available bare-earth terrain for those paths.
3. Use the inspected AvaFrameData events—especially Eiskar, Filisur, and
   Popeletzbach—as conditional-runout development candidates, not untouched
   holdout, because their contents have already been reviewed.
4. Retain the Davos, 1999 Swiss, and GEODAR events only for development,
   regression, and falsification tests.
5. Continue the SLF/NGI/NVE/Parks Canada/CAIC audit for newly released event
   packages. Do not send outreach, create accounts, or accept special terms
   without user approval.

For each candidate, record source URL/DOI, licence, record ID, event interval,
regime, trigger, geometry availability, observation method, terrain source,
release-depth evidence, weather/snow evidence, coverage semantics, uncertainty,
and exact exclusion reason.

Raw downloads go in an immutable, gitignored validation cache outside `DATA/`.
Check in only manifests, small redistributable reference data when licensed,
hashes, transformations, and scripts. The serving application must never read
that cache.

### 4. Create reference observations before running predictions

For each retained event:

1. archive source records and scene identifiers and compute SHA-256 hashes;
2. normalize to one reviewed local projected CRS with metre units;
3. digitize the release, dense-flow deposit, distal toe, interpretable coverage,
   and invalid-observation masks without opening model output;
4. repeat the digitization blind after a delay;
5. combine repeat-annotation disagreement with documented imagery/geolocation
   uncertainty; never treat repeatability alone as total accuracy;
6. record confidence and reject ambiguous or inseparable events;
7. create native, 2×, and 4× DEM versions without repeatedly resampling an
   already derived raster;
8. have the eligibility table reviewed and frozen before any event is assigned
   to final holdout.

The annotation procedure must not modify a source polygon to make a simulation
look more plausible.

### 5. Freeze grouped development and holdout partitions

Group in this order:

```text
mountain
  -> storm cycle
    -> avalanche path
      -> event
```

Assign whole groups, not pixels or neighboring events, to partitions. Use
development data for parameter screening and threshold selection. Use a small
model-selection partition only if the cohort supports it. Seal the final holdout
targets so prediction code cannot read them; hash the split and every target.

No currently scored or visually compared event may enter the final holdout.

### 6. Integrate and numerically verify AvaFrame before field fitting

Implement AvaFrame `com1DFA` as an offline external runner under
`backend/app/processing/runout/`; do not add AvaFrame, rasterio, GDAL, or other
bake-only dependencies to runtime imports.

Record the exact AvaFrame release/commit, container or environment digest,
configuration, DEM, release polygon, release thickness/density, friction,
entrainment, output thresholds, and transformations. Normalize its outputs into
new `avycore` physical result contracts without pretending they are the same as
the current relative-intensity particle fields.

Before any field calibration, pass:

- official/published AvaFrame verification examples;
- mass-balance and unit checks;
- deterministic replay;
- grid-resolution and grid-orientation studies;
- missing-terrain barrier and AOI escape tests;
- coordinate/vertical-datum checks;
- one code-to-code comparison against MinVoellmy or TITAN2D if feasible.

Keep `alpha_only` as the simple empirical runout baseline. Keep the current
point-particle engine as a characterized failed/experimental baseline; do not
make it the primary candidate merely by retuning the GEODAR events.

### 7. Calibrate conditional runout on development events only

Use the observed release polygon for every Profile C event. Screen release
thickness/density, friction, entrainment, DEM resolution, forest/resistance
treatment, and output threshold over physically defensible bounds.

Use a low-cost Latin-hypercube or Morris-style screen first, then refine only
in influential dimensions. Do not fit separate parameters per event. Compare
every configuration with:

- current alpha-only routing;
- a constant-parameter Voellmy baseline;
- the unchanged current point-particle engine where its outputs are comparable.

Freeze one parameter strategy, its uncertainty distributions, and all artifact
identities before holdout. If no transferable configuration beats the alpha
baseline on grouped development data, stop and report failure; do not spend the
holdout.

### 8. Run the conditional-runout holdout once

For each event, report:

- signed, absolute, and relative distal-runout error;
- runout-angle error;
- dense-flow inundation/deposit IoU and Dice where surveyed-domain semantics
  permit them;
- overprediction and underprediction area separately;
- lateral-width error when observable;
- AOI contact/escape, failed runs, and missing predictions;
- sensitivity to DEM resolution and release mass;
- 80% and 90% ensemble coverage of the observed toe/footprint.

Use the event as the scoring unit and a storm/mountain/path cluster bootstrap
for aggregate intervals. Compare models with paired error differences and
confidence intervals. Never validate velocity, pressure, depth, or arrival time
unless those quantities were independently measured.

The draft management gates, to be frozen after development and before holdout,
are:

- every holdout event produces a complete, untruncated result;
- the candidate improves on alpha-only in median absolute endpoint error and
  median footprint overlap;
- median absolute relative runout error is at most 10%;
- median deposit/inundation IoU is at least 0.50 where IoU is admissible;
- no practically important signed overrun or underrun remains after considering
  the clustered interval;
- empirical coverage of the 80% and 90% envelopes lies within 10 percentage
  points of nominal, subject to sample-size uncertainty;
- the conclusion survives the predeclared DEM, release-depth, and parameter
  sensitivity analysis.

These are project gates, not universal avalanche-industry thresholds. The frozen
protocol must also explain why they are meaningful relative to observation
uncertainty and the intended map scale.

### 9. Build and validate the dry-slab release candidate

Do not describe the existing `risk.py` result as an occurrence probability. Use
it as one transparent baseline beside:

- a slope-only potential-release mask;
- a published terrain/snow/wind potential-release-area formulation;
- a candidate that consumes a versioned snow state, including slab depth/density
  and weak-layer/failure/propagation diagnostics, if occurrence prediction is
  the intended claim.

Complete the existing offline ConditionPack/SNOWPACK work only after the event
cohort proves that the required forcing and snow-state observations can be
assembled. Missing snow state must exclude cells, not lower the score.

On Profile R development events, choose thresholds and any probability
calibration without holdout access. On the sealed holdout, report event capture,
release IoU/Dice, centroid and boundary distance, area bias, and credible
false-positive metrics only inside complete surveyed coverage. Report PR-AUC
and Brier Skill Score only if valid negatives exist and the output has been
explicitly calibrated as a probability. Otherwise retain relative-index or
positive/unlabelled language.

Required success is superiority to both release baselines with no major
mountain/storm-specific collapse. A candidate that fails remains rejected even
if one event map looks convincing.

### 10. Run end-to-end validation only after both components pass

Feed the frozen release candidate into the frozen AvaFrame configuration using
the predeclared conversion from release state to thickness/density/geometry.
Do not use observed holdout release geometry. Score Profile E events with the
same release and runout metrics, plus propagated uncertainty and failure rate.

The expected performance loss from conditional to end-to-end runout quantifies
error contributed by release prediction. A component pass does not survive if
the end-to-end chain fails its own predeclared gate.

### 11. Perform structural and out-of-domain checks

Run an independent depth-integrated solver on a representative subset using as
equivalent inputs as the formulations permit. Report solver disagreement as
structural uncertainty, not as a voting probability.

Then run leave-one-storm-out and leave-one-mountain/path-out development tests.
A result that transfers only within fitted paths must be reported as local
reconstruction, not spatial validation.

### 12. Decide whether to change the serving model

Do not replace serving behavior merely because AvaFrame is more complex.
Promote a component only when the frozen evidence demonstrates improvement and
the runtime/API meaning can remain explicit.

If promoted:

- keep AvaFrame and snow-model execution offline;
- bake or store versioned physical outputs for runtime consumption;
- update the canonical `avycore` contracts, backend API models, OpenAPI,
  generated frontend client, and `frontend/src/lib/twin.ts` together;
- keep current alpha and release results available as named baselines during the
  transition;
- preserve the disclaimer, relative-index language where applicable, missing-
  data masks, units, seeds, configuration identity, and uncertainty meaning.

### 13. Run a Mount Hosmer transfer test

External validation establishes only external-domain evidence. A Mount Hosmer
claim requires new, independent local observations with the relevant component
profile. Freeze the externally selected model first, then test local events
without tuning. If no eligible public/local observations exist, the honest final
status is “externally validated for the named domain; local transfer unknown,”
not “validated at Mount Hosmer.”

## Deliverables

The completed work must produce:

- a candidate and exclusion inventory with exact reasons;
- per-delivery intake triage reports naming, for every event, the profile it
  could support and the exact evidence still missing;
- immutable source manifests, licences, hashes, and transformation lineage;
- blinded reference release/deposit/endpoint geometries and uncertainty;
- component-specific trusted dataset registrations;
- frozen grouped split and experiment manifest;
- numerically verified AvaFrame runner and physical result contract;
- release and runout baseline artifacts;
- calibration artifacts that contain no holdout targets;
- one-time holdout predictions, failures, metrics, cluster-bootstrap intervals,
  and uncertainty-coverage results;
- a report that names the exact component, regime, region, scale, and model
  version that passed or failed;
- updated limitations and API validation status without broadening the claim.

The principal event table must include quality tier, evidence profile, release
overlap/offset, endpoint and relative runout error, footprint overlap, observation
uncertainty, prediction-interval coverage, AOI status, and failure reason. A
model-level table must compare the simple baseline, current implementation,
AvaFrame candidate, and optional independent solver with clustered confidence
intervals.

## Verification and completion gate

Before declaring this plan complete:

1. Rebuild every committed result from immutable inputs in a clean environment.
2. Verify identical deterministic identities for identical inputs and seeds.
3. Run `python -m pytest`.
4. Run frontend `npm run lint` and `npm run build` if any API or UI contract
   changed.
5. Confirm no file under `DATA/` was modified and no runtime-generated file was
   hand-edited.
6. Confirm failed, missing, cropped, and excluded cases are visible and no
   negative label was inferred from non-reporting.
7. Keep `is_validated=false` globally. Publish component-scoped validation only
   when the complete trusted holdout passes every frozen requirement.

## Immediate next action

Validation-contract v3 and the first anonymous public candidate funnel are now
implemented. The 14 August 2026 re-evaluation processed all 26 frozen candidates
and still produced zero eligible Profiles R, C, or E. Twenty-five immutable
source packets are released for external annotation, but zero has an independent
human review or accepted component. The immediate next actions are to obtain the
missing public primary event-surface/release-state evidence and complete the two-
reviewer procedure in `public-event-human-review-procedure.md`. Do not partition
the cohort, calibrate AvaFrame against field events or promote it into serving behaviour, or open holdout predictions unless
at least 12 events across six independent paths, two mountains, and three storms
subsequently pass every frozen evidence gate.
