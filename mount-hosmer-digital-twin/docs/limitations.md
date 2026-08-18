# Limitations (Stage 3)

**Read this before making any claim about what the model can do.** This is a research prototype, not an
operational tool.

## The headline

- **This is not an operational avalanche forecast.** It must never replace Avalanche Canada forecasts or
  field assessment. Every hazard/release number is a **relative index (0–100), never a probability and
  never a forecast**, and carries a disclaimer attached in code (`app/core/model_config.py::DISCLAIMER`).
- **Nothing is calibrated.** The risk model, the release threshold, and the runout alpha angles are
  UNCALIBRATED values from the avalanche-terrain literature and Canadian Rockies practice. **None** is
  fitted to an eligible field-observation cohort. Eight reviewed Davos-area events provide qualitative
  mapped-positive comparisons only; the strict independent holdout remains N=0, so the model is not
  field-validated against ground truth.

## Validation evidence

- The available Avalanche Canada holdings are regional forecast products, not avalanche-occurrence
  observations. LiDAR, Copernicus DEM, and land cover drive the model and therefore are not independent
  hazard validation. Sentinel-2/Landsat imagery is visual context only; an unverified image interpretation
  is not ground truth. Synthetic terrain is used only for deterministic software verification.
- `avycore.validation` defines the normalized evidence contract for any future independent observations.
  It requires immutable hashes and lineage, source permissions, original and normalized CRS, metre units,
  coordinate order, acquisition dates, spatial coverage semantics, positional uncertainty, structured
  observation-method/evidence class, and event-level calibration versus holdout assignment. It rejects
  imagery, synthetic data, and model output when labelled as field validation, inconsistent event dates or
  scenarios, and any event that leaks across calibration and holdout partitions.
- The accompanying overlap evaluator rasterizes the registered target, survey-coverage, and positional-
  uncertainty geometries itself on an exact bake-bound, metre-based projected grid matching the evidence;
  callers cannot substitute arbitrary observation or survey masks. It requires the complete compatible
  holdout cohort and binds predictions to model/config/bake/engine/seed/scenario identities and
  deterministic artifact hashes. Endpoint metrics bind
  predictions to every registered observation ID, require characterized field uncertainty, and report
  missing predictions to prevent survivorship bias. Missing model inputs remain excluded and visible.
- Contract-valid field data still cannot claim independent validation until its exact immutable dataset
  identity is added through code review to the trust registry. That registry is intentionally empty. This is
  ingestion/evaluation scaffolding, not a claim of end-to-end field-validation readiness. Until eligible
  observations are obtained and reviewed, API results label the available evidence as **software
  verification only**, with field validation unavailable and zero eligible events.
- The anonymous-public RegObs/Sentinel/Høydedata route was executed through its frozen gate 8 and failed:
  26 candidates were evaluated, zero passed complete observation QA, zero received independent human review,
  zero had eligible normal-to-slope release thickness, and zero had an event-surface-eligible DEM. Twenty-five
  immutable source packets are released for external annotation, but packet release is not evidence acceptance.
  The resulting Profile R/C/E counts remain 0/0/0. Because the required 12 events across six paths, two
  mountains, and three storms were unavailable, no split, AvaFrame integration, calibration, or holdout run
  was performed. See `docs/validation-report.md`, `docs/public-event-human-review-procedure.md`, and
  `validation-data/candidates/public-event-strict-funnel-v5.json`.
- Owner-data intake now has a strict immutable-file/licence schema, byte/hash preflight, two-human blinded
  adjudication with third-human conflict resolution, complete-candidate decision gate, and a preregistered
  leakage-safe path/mountain/storm split. The split artifact contains no event assignments, and prediction
  and field-metric modules remain sealed while the eligible cohort is 0 events, 0 paths, 0 mountains and
  0 storm cycles. These controls improve reproducibility and prevent selection leakage; they are not field
  evidence and do not improve or establish model accuracy.
- A separate positive-only evaluator accepts qualitative or calibration evidence whose unmapped space is
  explicitly unknown. It reports mapped-positive coverage but deliberately has no IoU, precision, F1, or
  independent-validation flag. The frozen 32-run Davos-area comparison shows both misses and strong
  sensitivity/overprediction signals; see `docs/validation-report.md`. Those results do not change the
  field-validation status or populate the trust registry.

## The risk model

- **Serving-time conditions are explicitly user-entered research scenarios, not a live feed or forecast.**
  Simple inputs are labelled assumptions. The advanced workspace can preserve user-labelled measured,
  estimated, assumed, and unknown records with units, UTC times, sources, uncertainty and spatial
  applicability. The serving app does not ingest the offline M2 Condition Packs, weather/snow feeds or
  satellite measurements, and it cannot verify that a user-entered measurement is representative.
- Seven inputs are active in the current equations: new storm-snow depth, representative wind speed,
  meteorological wind FROM direction, the release-size sensitivity assumption, and three optional ones —
  air temperature, flow regime and a user alpha angle. **Every optional input defaults to unknown, and an
  unknown value reproduces the result you would get without it exactly**; nothing is given a default guess.
  Whole-area structured values use the same characterized scalar equations as the former sliders.
  Elevation/aspect/user-drawn scopes exclude unsupported grid cells; they do not extrapolate a value
  outside its stated applicability.
- **Air temperature only classifies precipitation phase.** It splits new precipitation into snow and rain
  across the same 0–2 °C band the offline M2 work uses, and rain does not build a dry storm slab, so the
  loading term falls. **A lower index there means less dry-slab loading, not less hazard** — rain-on-snow,
  wet-loose and wet-slab avalanches are real and this dry-slab model does not represent them at all. That
  is stated in a critical advisory whenever it happens, and there is no temperature term anywhere else.
- **Flow regime and the user alpha angle are sensitivity controls, not new physics.** Flow regime points
  the same dry-snow engines at published wet/powder/mixed friction constants; it does not add wet-snow or
  powder-cloud physics. The alpha override replaces the configured regional angle of reach, clamped to a
  reviewed 15–40° envelope — and is no more calibrated to Mount Hosmer than the default it replaces. Both
  are bound into `config_sha256` and the replay identity.
- **No snowpack profile.** Weak-layer, snowpack, stability-test, field-red-flag and extra flow records are
  retained with full provenance and **cannot change the numerical result by any amount**. The model cannot
  see buried surface hoar, facets or crusts — the mechanism behind most avalanche fatalities. It reasons
  only about *terrain capability* (slope, aspect, curvature, forest) modulated by the supported new-snow
  and wind-loading terms.
- **Those records drive advisories instead.** `avycore.advisories` turns them into deterministic,
  rule-derived statements published in the scenario report and rendered above the number. Recording
  whumpfing, shooting cracks or recent avalanche activity raises a critical advisory stating that the
  model cannot see them and that standard practice treats such evidence as outranking a terrain model;
  persistent weak layers and propagating stability tests do the same. Every advisory carries
  `changed_the_number: false`, because it is true.
- **A coupling contract now exists, and nothing satisfies it.**
  `avycore.release_coupling` defines what a modelled snow state must supply before
  a dry-slab release model may be coupled to terrain: slab depth and density, a
  candidate weak layer, a failure-initiation diagnostic, a crack-propagation
  diagnostic, a loading rate over a declared window, and a bounded uncertainty span
  and declared unit for each. It requires the simulation to start before the valid
  time it describes, so a profile invented at the requested time is refused. Three
  rules are encoded rather than left to callers: there is **no code path from
  terrain capability to an instability result without a snow-state term**; an
  ineligible terrain class is **removed from supported coverage** and the contract
  has no way to express "missing snow state, lower score"; and non-dry-slab regimes
  are refused as separate model types rather than approximated. Since no eligible
  Snow State Pack exists, every terrain class is currently ineligible. This is
  scaffolding that makes a future coupling checkable — it is not a physics-informed
  release model, and it changes no number today.

- **This is deliberate, not unfinished.** Mapping a stability-test score or a weak-layer type onto this
  index would require a coefficient, and there is no published mapping for this model and no local
  calibration from which to fit one. Such a coefficient would be invented, and an index that silently
  contains an invented snowpack term is more dangerous than one that admits it has none.
- **It is a release estimate, not an occurrence prediction.** A "release zone" is terrain the model
  considers capable of releasing under the given sliders. It is **not** a statement that an avalanche will
  occur there, and a below-threshold day is **not** a statement that the mountain is safe (see I3 below).

## The composite hazard index

- `area_hazard_index` and the per-zone `hazard_index` are a **different quantity** from
  `release_potential_index`, published under separate names on the same 0–100 scale. Each zone index is
  `100 x release x (REACH_BASE + (1 - REACH_BASE) x reach)`, then raised by up to `EXPOSURE_MAX_UPLIFT`
  where exposure is mapped under its runout, then clipped to 0–100. Every constant is an **uncalibrated
  scaling choice** in `avycore.hazard.composite` and hashed into `config_sha256`. None is fitted to an
  observed event. It remains a relative index, never a probability, forecast or danger rating.
- **Exposure can only raise a zone's index, never lower it.** A zone with nothing mapped in its path keeps
  its terrain-and-reach index exactly. An empty valley therefore never reads as *safer* than the terrain
  model would otherwise say — only as less additionally consequential. This is why the exposure term is a
  bounded multiplicative uplift and not a weighted average: an average would let a blank map pull a
  genuinely dangerous slope down.
- **Reach is measured, not assumed.** It normalises the runout's horizontal reach, vertical drop and peak
  velocity against documented reference magnitudes. Fast routing computes no velocity, so its velocity
  sub-term is dropped and the remaining weights are renormalised — a missing measurement is never scored
  as zero.
- **Unsimulated zones are labelled, not zeroed.** Runout runs for the top 12 (fast) or 6 (advanced) zones
  only. The rest publish `basis: release_only`, carry `components_available.reach = false`, and take the
  release estimate as their index. A release-only number and a fully decomposed one are **not directly
  comparable**; `hazard_detail` reports how many zones contributed each component, and `peak_zone_basis`
  says which kind of number the published peak is.
- The area index is a **plain area-weighted mean**, so one large mild zone genuinely outweighs a small
  severe one. `peak_zone_index` and `peak_zone_id` are published beside it so that dilution is visible
  rather than hidden.
- When no zone crosses the threshold there is **no composite index at all** (`area_hazard_index` is null).
  The 95th-percentile fallback still exists and is published under its own name,
  `no_zone_release_percentile_index`, because it shares no terms with the composite and must never be read
  as one.

## Exposure: roads, rail and inferred living areas

- The exposure layer is an **OpenStreetMap extract, not a survey**, licensed under the **Open Database
  License (ODbL) 1.0** and attributed to **© OpenStreetMap contributors** wherever it is displayed. OSM
  completeness varies and is not guaranteed: a cell with no exposure weight means *OSM maps nothing there*,
  **not** that nothing is there. Ground outside the AOI is masked as unknown, never written as weight zero.
- **"Important living areas" are derived and labelled derived.** This AOI's extract contains exactly one
  `building` way, no `landuse` and no `place` polygon, so there is nothing to draw directly. Built-up
  outlines are inferred by buffering every `highway=residential`/`highway=service` way by 60 m, unioning the
  buffers, and keeping each cluster whose contained residential/service road length reaches 1500 m. On the
  real extract that accepts 2 of 7 candidate clusters (5321 m and 4468 m of road) and rejects the rest
  (843 m and below). **An outline is a proxy for residential road density, not a survey of occupied
  structures, and the absence of an outline is not evidence that nobody lives there.** No place is named
  that OSM does not name.
- Class weights (`inferred_settlement` 1.00, `highway_major` 0.90, `building_mapped` 0.90, `railway` 0.75,
  `road_local` 0.50, `track_trail` 0.20) are **uncalibrated relative judgements**, not a casualty, damage or
  loss model. Buffers give mapped centrelines a plausible ground width; they are not surveyed extents.
  Waterways and power infrastructure are deliberately excluded and the exclusion is recorded in
  `meta.json`; power is a known omission, not an assertion that a struck tower does not matter.
- **The pack guardrail was relaxed in the open, not deleted.** `mountain_pack.py` still binds the `pois`
  and `exposure_features` roles to `purpose: exposure`, and now also refuses `purpose: exposure` on any
  other role. The rationale: exposure must stay **entirely out of the release model** —
  `avycore.hazard.risk` does not import it, is not passed it, and never sees it — and may enter only the
  named consequence term of the composite index, where it can raise a zone's index and never lower one.
  That boundary is enforced by a test, not just by convention.

### Scenario completeness and classification

- Missing active inputs remain null. A terrain-only or otherwise incomplete scenario returns no release-
  potential index and no runout; it never substitutes zero, a default calm day, or a safe-looking empty
  footprint. A measured zero is distinct from an unknown value.
- Results are deterministically classified as terrain-only, hypothetical, partially observation-
  constrained, or fully specified for the implemented research model. “Fully specified” does not mean a
  complete snowpack description, field validation, calibrated physics, probability, danger rating or
  operational forecast.
- Supplied input uncertainty and provenance are reported but are not statistically propagated through the
  release index. Spatially incomplete results are labelled supported-area-only, with unsupported cell
  counts visible. Elevation scopes inherit the bake's unknown/mixed vertical-datum limitation.
- Reproducibility binds a canonical scenario hash to the model/config identity, bake identity, engine and
  effective seed. Response generation time and duration are not part of numerical replay identity.

## Historical meteorological forcing (M2)

- The offline ECCC adapter reconstructs an hourly timeline from an immutable historical source snapshot;
  it is not a live feed and makes no current-condition claim. Provider-valid observations are still not
  Mount Hosmer terrain-scale truth.
- The selected ECCC-operated Sparwood CS station is about 17.3 km horizontally from the AOI center and
  about 1.36 km below the temporary mountain-reference elevation used for characterization. Orographic
  precipitation, ridge wind, radiation, cold-air pooling, and inversions are unresolved. The fixed
  6.5 K/km temperature correction has not been validated at Mount Hosmer.
- The selected winter represents every UTC hour, but three hours have absent station records and remain
  masked. No gap filling is applied. Shortwave and longwave radiation are missing for the entire winter;
  zero is never substituted.
- Rain/snow phase is a 0-2 degrees C air-temperature classification band, not a phase observation. No
  precipitation undercatch correction, wind correction, or vertical atmospheric-profile method is used.
- Withheld NAV CANADA station 1157635 versus selected ECCC-operated 1157631 quantifies nearby
  station-to-station disagreement inside the ECCC historical collection only. Different original station
  operators do not make this a terrain-scale or field-truth comparison. Over 5085 exact-hour temperature
  pairs, uncorrected ECCC-minus-NAV CANADA bias/MAE/RMSE are 0.202/0.228/0.270 degrees C; transferring both
  stations with the fixed lapse rate gives 0.062/0.136/0.190 K difference. The reduced disagreement is not
  validation, calibration, or evidence that the transfer is more accurate.
- The independent PCIC/PCDS comparison uses ENV-AQN 585/history 14942, originally observed by Teck Coal
  Limited - Greenhills Operations at Elkford E290310. The exact source record is OGL-BC 2.0. The station is
  about 44.2 km from the AOI center and 1.164 km below the 2496.78 m reference elevation. It supplies only
  wind for part of the winter, ending at 2026-02-21T23:00:00Z; all later hours and unsupported variables
  remain masked. Its aggregate download supplies no per-observation PCIC QC/revision field and is labelled
  unverified, preliminary, and subject to revision.
- Exact-hour ECCC-minus-PCIC disagreement is characterized over 2691 wind-speed pairs (bias 0.590 m/s,
  MAE 1.613 m/s, RMSE 2.185 m/s) and 2274 wind-direction pairs (circular bias -29.367 degrees, circular
  MAE 68.452 degrees, circular RMSE 86.794 degrees). These are station disagreement metrics, not Mount
  Hosmer error, validation, calibration, or evidence that either provider is more accurate.
- The 2496.78 m reference has no historical derivation record. It exactly matches baked elevation
  `array[1200,1200]` (2496.780029 m) rounded to 0.01 m, an integer-array midpoint rather than the cell
  containing the requested coordinate. The containing/nearest cell is `array[1199,1200]` at 2500.3828125
  m; a four-cell, all-valid interpolation at the coordinate is 2499.3645924 m. Relative to existing
  behavior these would cool the transferred temperature by only 0.02342 K and 0.01680 K, respectively.
  A new versioned contract proposes the bilinear value but keeps 2496.78 m separately named and active in
  existing packs. Migration is not authorized. The compatible rebuilt bake is numerically identical to
  the preserved terrain arrays, but its vertical datum remains unknown/mixed: the target cells are 2016
  LiDAR with indirect CGVD2013 evidence, while 4222 fallback cells are Copernicus EGM2008 and no vertical
  transformation identity is recorded.
- Full-AOI elevation fractions quantify some representativeness limits: 86.0% of valid terrain cells are
  above Sparwood CS and 69.4% are above Elkford E290310; only 36.2% and 44.4%, respectively, lie within 250
  m vertically of those station elevations. These fractions do not quantify exposure, ridge wind,
  inversions, precipitation gradients, radiation, canopy, or atmospheric-profile differences.
- The direct B.C. current-season snow record explicitly licenses hourly SWE in mm and snow depth in cm
  under OGL-BC, but Morrissey Ridge is not eligible for ingestion. BC Hydro maps `MOR` to `2C09P`, not
  `2C09Q`; the Province marks 2C09Q active, while PCIC stores MOR/2885, 2C09P/2950, and 2C09Q/2951 as
  separate histories with different elevations and point/sum semantics. No official date-effective
  identity/configuration boundary or current per-value QC/revision contract resolves these conflicts.
  No official date-effective join was inferred and no histories were merged. A BC Hydro metadata link
  unexpectedly returned a live current-value response; it was not saved, normalized or used, and no bulk
  observation resource was downloaded.
- No eligible representative exact-winter shortwave or longwave history was found. The available forcing
  also lacks the complete cloud/atmospheric inputs and site-characterized parameters needed for an
  interval-aware published derivation. Radiation remains masked; W/m2 is never equated with accumulated
  MJ/m2 without explicit interval integration, and longwave is not manufactured from air temperature.
- PCIC metadata contains no radiation history spanning the selected winter; its latest history carrying
  both downwelling components ends 2020-07-01. ERA5-Land is an unselected modelled candidate with both
  components, exact-window catalogue coverage and CC-BY-4.0 reuse, but it is not an observed or terrain-
  scale validation series. It has not been acquired or evaluated because CDS access requires a user
  account, personal API token and manual terms acceptance. Its J/m2 interval semantics also require a
  dedicated reviewed adapter before any conversion to W/m2.
- Independent provider disagreement, a bounded lapse-rate sweep, calm-direction sensitivity, target
  footprint, elevation, slope, aspect, forest, curvature and source-coverage representativeness are now
  characterized. Atmospheric exposure, canopy height/density, sensor heights and station configuration
  histories remain unavailable. Eligible snow-depth/SWE comparison, radiation,
  terrain-scale observations, and field validation remain unavailable. M2 therefore remains incomplete
  and cannot support calibration, validation, probability, or improved-accuracy claims.

## Offline snow-state integration (M3)

- `SnowStatePack` and ConditionPack-to-SMET are inactive offline contracts. They are not imported by the
  server and cannot affect release scores, runout, API fields, or sliders.
- The authoritative ECCC pack is correctly rejected by the SMET adapter because shortwave and longwave
  radiation are missing for every hour. Missing required forcing is never written as SMET nodata for a
  model run, interpolated, threshold-generated, or replaced with zero.
- The official project-local SNOWPACK 3.7.0 executable/parser smoke gate passes for two unchanged official
  example runs. Raw SMET differs because of run-stamped history, while normalized physical output is
  identical. The inactive v2 contract now separates complete run-artifact identity from scientific replay
  identity and derives executable-plus-DLL and complete model-input inventories. This is software replay
  evidence only. Radiation remains unavailable, three non-radiation hours remain masked, and no reviewed
  Hosmer initial snow/soil state, ground boundary, roughness, canopy classification, configuration or
  independent snow-depth/SWE comparison exists. No Hosmer SNOWPACK output or physical-improvement claim
  exists.

## Research-only hourly snow and release-regime hindcast

- `avycore.snowpack` is a deterministic, runtime-safe research library used by
  the frozen Swiss hindcast. It is not connected to `/api/assess`, the Mount
  Hosmer bake, sliders, or operational conditions. Its addition does not make
  the serving model more accurate.
- CERRA input is hourly regional reanalysis at 5.5 km. Nearest-sample
  assignment makes its coarse footprint explicit; the 30 m output grid contains
  no 30 m meteorological information. The API-reported elevation used for
  lapse transfer is not exposed model orography, and the lapse rate is fixed
  rather than observed for each storm.
- Recent-snow settlement, excess-wind drift potential, wetting thresholds,
  degree-hour scales and regime score curves are deterministic,
  literature-informed but uncalibrated relative indices. Drift potential is
  not transported mass. Provider snowfall remains diagnostic and cannot be
  added to temperature-partitioned precipitation.
- The buried-interface field is only a weather proxy. It observes no grain
  type, weak layer, snow profile or stability test and has no numerical effect
  on release. The wet-snow field is a surface-wetting susceptibility proxy; it
  has no internal snow liquid-water state and cannot distinguish wet slab from
  wet loose.
- Full-depth/glide release is explicitly unsupported because basal liquid
  water, smooth-ground class and glide cracks are absent. Other regimes can
  spatially intersect a mapped full-depth outline; such an intersection is not
  evidence that glide physics was represented.
- The frozen 1999 result captured 24.81% of positive mapped events and failed
  its capture, completeness and same-budget baseline rules. Positive-only
  outlines provide no verified negatives. The library is therefore neither
  calibrated nor field validated for any regime; see `validation-report.md`.

## Runout simulation

- A version-bound offline adapter now runs AvaFrame 2.1 `com1DFA` for declared
  `dense_dry` scenarios only. The current slice supports one release collection,
  projected metre-based input, explicit positive release thickness/density,
  explicit Voellmy `mu`/`xi`, a fixed timestep and `entrainment_enabled=false`.
  These positivity checks are execution constraints, not physically validated
  parameter ranges. No missing thickness, density, friction or entrainment value
  is synthesized.
- The synthetic PRA-style release-to-com1DFA case verifies process isolation,
  unit mapping, output normalization, mask/CRS preservation and same-machine
  deterministic replay. Its synthetic values and outputs provide no evidence of
  accuracy, calibration or end-to-end field validity. No bounded sensitivity
  ensemble has been run, so normalized output reports no propagated uncertainty
  bounds and says so explicitly.
- **Flow-Py now runs, through AvaFrame's `com4FlowPy` port, and is a different
  model from com1DFA rather than a second opinion about the same one.** com1DFA
  solves a depth-averaged dense-flow problem; Flow-Py routes a dimensionless flux
  along an energy line. Neither consumes the other's output. See
  [`runout-engines.md`](runout-engines.md).
- **Flow-Py produces no flow depth, velocity, pressure, or arrival time, and those
  outputs are published as `unsupported` with a reason rather than omitted or
  zeroed.** Its `z_delta` is an **energy-line height**, not a depth. Upstream's own
  configuration documents the sliding-block bound `max_v = sqrt(max_z * 19.62)`;
  that is a limit derived from an assumed friction model, not a simulated flow
  velocity, so it is not published as one.
- **AvaFrame documents com4FlowPy as under heavy development and outside its
  automatic test coverage.** That has not changed, and it is why the module's
  executed file hashes are recorded with every result.
- **com1DFA's peak travel angle `pta` is not Flow-Py's `fpTravelAngleMax`, and
  the cross-engine travel-angle comparison stays unsupported because of it.**
  Both are `arctan(drop / horizontal path length)` in degrees, but com1DFA
  divides by each particle's own realized trajectory length accumulated over the
  simulation and takes a maximum over particles *and over time*, while com4FlowPy
  divides by the **shortest** 8-connected raster path from the release cell and
  takes a maximum over release cells with no time dimension. The two also differ
  in what sets the path (Voellmy friction and the SPH pressure gradient versus
  routing persistence and alpha), in discretization bias, and in how an unreached
  cell is written (`0`, indistinguishable from a real 0°, versus `-9999`). This
  was read from the pinned AvaFrame 2.1 sources, not inferred from whether the
  numbers agree; the derivation and the source digests are in
  [`runout-engines.md`](runout-engines.md) §2.1. Publishing them as one quantity
  would compare a time-peak of a dynamics-dependent trajectory against a static
  shortest-path extremum.
- **The canonical standalone Flow-Py distribution is a separate engine identity
  (`runout.flowpy_upstream`) and remains fail-closed.** The repository
  ([github.com/avaframe/FlowPy](https://github.com/avaframe/FlowPy), GPL-3.0-or-later)
  is archived read-only since 2024-09-17, and its latest release `v1.0.3`
  (commit `7b061599355cef584491d69eae2686307d286901`) **reassigns `argv` to a
  hardcoded example inside `main.py`, so the released command line ignores its
  arguments.** Only the later untagged master commit
  `27ad81d3e804e4e9d85a9773fca10ee7dc428183` comments that out, and `main.py`
  imports PyQt5 unconditionally. The adapter hashes an operator-supplied
  checkout's `main.py` against both reviewed commits and never substitutes the
  AvaFrame port for the standalone distribution.
- The Flow-Py adapter runs a single tile in a single process. Upstream otherwise
  tiles the domain and merges overlapping tiles with max/sum reductions, and
  distributes release cells over a multiprocessing pool; both settings are what
  make byte-identical replay achievable. Forest, infrastructure, variable-alpha,
  variable-exponent and variable-uMax modules are disabled because this slice
  supplies no layer for them.
- The **Flow-Py planar energy-line analytical case passes** its preregistered
  limits: energy-line height error 3.34e-06 m (limit 0.01), stopping-cell
  difference 0 cells (limit 0), straight-line travel-angle error 1.90e-06 degrees
  (limit 0.01), travel-length error 0.0 m (limit 0.01), with unit, CRS,
  coordinate-order, mask, truncation and unsupported-output invariants passing and
  identical replay identity across two runs. The frozen record is under
  `validation-data/benchmarks/flowpy-energy-line/`. **This verifies one idealized
  planar case in software.** It is not calibration and not field validation.
- **The two engines disagree substantially on the synthetic case**, which is the
  point of running both. At the comparison script's default settings (40 s
  simulation time, seed 12345): extent IoU 0.429, symmetric-difference area 52 100 m²,
  and a maximum-reach difference of 142.2 m (com1DFA 455.4 m, Flow-Py 313.2 m) on
  identical release and terrain inputs. **Disagreement is not a measurement of
  either engine's accuracy, and agreement between two uncalibrated models would
  not be evidence either.** Both remain uncalibrated for any real site.
- r.avaflow still returns `unavailable` until a version-bound image/executable,
  exact redistribution licence record, configuration mapping and normalized output
  parser are reviewed. It returns no placeholder physics.
- The published BC PRA [paper](https://nhess.copernicus.org/articles/22/3247/2022/)
  and [OSF project](https://doi.org/10.17605/OSF.IO/YQ5S3) (`yq5s3`) were located.
  The OSF
  project declares GPL-3.0, but its principal grid-search bundle is about 1.52 GB
  and the contained source files/file-level notices have not been inspected. No
  published PRA code was copied; the current AvyCore relative-index release model
  remains the explicit uncalibrated baseline.
- The AvaFrame 2.1 `avaSimilaritySol` analytical case now passes the locked
  software-verification gate through the isolated adapter. At 20.04 s on the
  upstream 3 m local Cartesian grid, downstream-front error was 0 m, relative
  L2/L-infinity errors were 0.04435/0.07951 for thickness and
  0.04182/0.06700 for momentum, solver mass-balance error was 0, and initial
  volume error was 0.000933. All were below the pre-run limits of 6 m,
  0.5/0.75, 0.5/0.75, 1e-12 and 0.05 respectively. Units, grid, undefined local
  CRS, masks and no-boundary-touch invariants also passed. The frozen acceptance
  record and immutable run are under
  `validation-data/benchmarks/avaframe-2.1-avaSimilaritySol/`. This verifies one
  idealized analytical case, not Mount Hosmer accuracy. Direct scalar speed is
  diagnostic because the upstream reference evaluates momentum at the
  zero-thickness front; pressure has no analytical target in this case.
- com1DFA must not replace the default assessment engine or support a field-
  accuracy claim. The exact next evidence requirement is the independently
  reviewed, licence-compatible 12-event cohort frozen in
  `validation-data/experiments/public-data-field-validation-v2.json`: at least
  six paths, two mountains and three storms, with separate calibration and
  untouched holdout groups and complete release-state, event-surface, target,
  uncertainty, CRS, lineage and surveyed-coverage evidence for every event.

- The runout library exposes three scientific component modes: `alpha_only` fast routing,
  `dynamics_only` particle integration without the alpha stop, and the serving application's `hybrid`
  particle mode with its alpha energy line. Extent from `alpha_only` and `hybrid` primarily tests the
  empirical alpha angle; velocity and path shape test particle dynamics. None is field-validated.
  Alpha angles are published Canadian Rockies ranges, not local back-analysis. Every runout carries an
  explicit sensitivity envelope. Its reported area includes the central footprint and is not a band-only
  area or a statistical confidence interval; a line drawn with false precision is worse than no line.
  Advanced runout treats missing elevation, forest, or plan-curvature friction inputs as barriers rather
  than silently assuming open/neutral terrain.
- For interactivity, a synchronous assessment simulates runout only for the highest-scoring zones
  (top-12 fast / top-6 advanced). All release zones are still shown; the others are simply not run out.

## Offline pipeline and prediction products

- `python -m app.pipeline` is **offline**. External engines never run inside
  `POST /api/assess`, and the serving routes over `runtime\predictions\` open
  files and validate them without importing any engine. The interactive
  slider-driven assessment is unchanged and remains the named baseline.
- **Only the synthetic case is runnable.** `--case mount-hosmer` is refused with a
  stage-attributed error: a real-site case requires an eligible Snow State Pack and
  reviewed release thickness, density and friction parameters, none of which exist.
  The pipeline will not substitute synthetic values for a real site.
- The release stage runs the **existing uncalibrated AvyCore terrain/loading
  relative-index baseline**. It contains no modelled snow instability, no slab
  depth, and no weak-layer term, so a product's release extent inherits every
  limitation of that baseline.
- **The Snow State stage is always `unavailable`.** Where a Condition Pack is
  selected, the reason names the missing variables or masked hours; the authoritative
  ECCC pack still has no shortwave or longwave radiation at all. Where forcing were
  complete, the reason would be the absent reviewed initial snow/soil state, ground
  boundary, roughness, canopy classification and site configuration.
- **`release_probability` is always null**, and travels with a machine-readable
  reason. It may become non-null only after a calibrated probabilistic release model
  and eligible independent validation exist.
- Products publish `validation_level: software_verification_only` with
  `eligible_field_events: 0`. Nothing in a product is calibrated or field validated.
- **`--resume` reuses a stored engine run, and it is bound to one machine.** Its
  key includes the isolated interpreter's own bytes and that environment's
  installed-distribution manifest, so a cache written on one machine is a
  guaranteed miss on another — by construction, not by accident. A miss is also
  the default whenever any identity component cannot be resolved, and a hit is
  re-verified against the stored checksums and against the result's own recorded
  provenance before it is used. Reuse changes execution only: a resumed run
  publishes the identical `product_id`. Measured on the development machine, the
  full two-engine ensemble took about 50 minutes cold and 13.6 s with 14 of 14
  cache hits.
- **Bounded sensitivity ensembles are sweeps of assumed ranges, not fitted
  values.** `--ensemble` now sweeps six spans: Flow-Py's angle of reach (±3°) and
  release extent (±5 m), and com1DFA's Voellmy `mu` (±0.03), release thickness
  (±0.3 m), release density (±50 kg m⁻³) and release extent (±5 m). Runout-area
  spread on the synthetic case at the pipeline defaults: angle of reach 35 075 m²,
  release thickness 24 950 m², com1DFA release extent 15 050 m², `mu` 13 400 m²,
  Flow-Py release extent 10 600 m², release density **0 m²**. **Member frequency is
  model frequency over a deterministic sweep — never a probability, a confidence
  level, or a calibrated likelihood** — and the contract stores that sentence with
  every sweep so it cannot be dropped downstream. The envelope is the union of
  member footprints, not a statistical confidence region.
- **A span with no stated basis cannot be declared.** `SweepSpecification`
  requires a `basis` and a `source` statement, and requires offsets that bracket
  the central value; the construction fails before any member is computed, so an
  unjustified span cannot reach a published envelope. Five of the six spans are
  assumed literature ranges; the release-extent span is labelled `numerical`,
  because moving a thresholded boundary by one 5 m cell is a sensitivity to a
  discretization and there is no literature about this project's uncalibrated
  index cutoff.
- **The zero density spread is a model property, not a missing number.** With
  entrainment disabled and Voellmy friction, density cancels out of com1DFA's
  depth-averaged momentum balance, so the three members are byte-identical in
  depth (max 1.4263 m) and velocity (max 31.6760 m s⁻¹) and differ only in peak
  pressure — 150.50 / 200.67 / 250.84 kPa, exactly proportional to the density,
  because `p = rho*v^2`. It means density does not move *this engine's footprint in
  this configuration*; it does not mean density does not matter.
- **Entrainment is still not swept, and is published as a refusal rather than
  omitted.** com1DFA entrainment requires an `ENT` entrainment-area shapefile with
  a per-feature entrainment thickness, and this slice supplies no entrainment
  layer and runs `simTypeList=null`. Flow-Py release thickness and density are
  refused for a different reason: com4FlowPy routes a dimensionless flux and
  carries neither quantity. All three appear in the product's
  `unsupported_ensembles` with a reason and the exact action that would enable
  them.
- Forcing and snow state are still **not** varied, and the sweeps remain
  one-at-a-time with no interaction terms, so the published envelope is a lower
  bound on sensitivity, not a total uncertainty budget.

## Terrain and the bake

- The default Mount Hosmer 3D mesh and terrain layers are **baked once, offline**, from 5 m BC-LiDAR
  (mosaicked to ~99.9 % AOI coverage), with Copernicus GLO-30 as gap-fill only. An alternate Mountain Pack
  may instead declare a provider-neutral `single_raster` primary DEM and its own generated runtime root.
  The running service reads no source data.
- The 171 `.laz` point clouds are not used (the DEM rasters are already derived from them). Re-deriving a
  finer-than-1 m surface is out of scope.
- Baked layers load as masked arrays, so a masked/NaN pixel stays missing, not zero. Terrain and forest
  source codes are persisted per pixel; a forest cell with neither canopy nor WorldCover coverage remains
  masked instead of becoming open terrain.
- The current bake includes a fixed winter Sentinel-2 natural-colour drape as visual context only. Its
  capture time, source resolution, cloud percentage, source hash and display stretch are recorded in
  `meta.json`; no imagery value enters the release or runout model.
- The optional exposure layers (`exposure_weight`, `exposure_class`) and `exposure/features.geojson` are
  baked from the declared OSM extract at bake time with pyproj and shapely. A pack that declares no
  exposure asset still bakes, and an assessment against a bake without the layer succeeds with the exposure
  term reported unavailable — never as zero.
- Bake schema v2 binds every assessment to SHA-256 identities for its scientific layers, source lineage,
  grid, configuration and processing code. A v1, incomplete, corrupted or code-incompatible bake is
  rejected and must be rebuilt explicitly with `python -m app.bake --force`.

## Performance and resource use

- A characterized cold-process `fast` assessment on the real 2400×2400 (5.8 M cell) bake, using 50 cm
  new snow, 60 km/h wind from 225°, and a medium release, took 11.61 s and 13.45 s in two final runs and
  peaked at 605.1–605.2 MiB RSS on the development machine. Before streaming zone/runout aggregation, the
  same scenario took 11.92 s and peaked at 1337.5 MiB; its release index, zone counts, and core/envelope
  areas were unchanged. This demonstrates the memory reduction, not a reliable speed change. These are
  implementation benchmarks on one machine, not physical-validation results or a deployment guarantee.
- CPython may retain freed array memory: the characterized process ended at 272.8 MiB RSS despite a
  72.0 MiB pre-assessment baseline. Hosting still needs headroom for warm workers and concurrent requests.
- `advanced` particle-ensemble performance was not re-characterized in this pass and must not be inferred
  from the `fast` measurement. It remains capped at 6 zones (`MAX_ADVANCED_ZONES`) versus 12 for fast mode.
- Assessments are **deterministic**: identical conditions produce an identical hazard score across
  machines and architectures (verified locally and on x86 cloud hardware).
- **That claim is broader than the evidence, and the frozen M0 baseline does not currently hold across
  machines.** `test_baseline_comparison.py` asserts that `m0-baseline.json`'s SHA-256 hashes over raw
  float arrays reproduce exactly. They do on the machine the baseline was frozen on, and they do **not**
  on GitHub Actions — on either Ubuntu or Windows runners, and **with numpy and scipy pinned to the exact
  versions used to freeze it** (2.2.6 / 1.16.3), which rules out package drift as the cause. The
  remaining candidates are the BLAS/LAPACK build and the CPU instruction set the wheels dispatch to;
  neither has been isolated yet.
- **Frozen digests are taken over LF bytes, and that is now enforced rather than
  assumed.** This repository is authored on Windows with `core.autocrlf=true`, so
  an unpinned text file is checked out as CRLF while git stores it as LF. Nine
  frozen-evidence tests used to fail on that alone, in both directions: a digest
  frozen over CRLF bytes for a file that had since been pinned to LF, and a digest
  frozen over LF bytes for a file still delivered as CRLF. `.gitattributes` now
  pins the whole repository with `* text=auto eol=lf` (plus `*.npz binary`), the
  affected digests were re-derived over the canonical LF bytes, and
  `test_frozen_identity_line_endings.py` fails if the pin is removed, if any
  tracked text file carries CRLF, or if any recorded digest matches the CRLF
  rendering of a repository file. No committed blob in this repository has ever
  contained a CR byte, so no number changed; see
  [`validation-report.md`](validation-report.md) for the neutrality evidence.
- **One frozen binding remains unverifiable, and it is not a line-ending
  problem.** The GEODAR result records
  `parameter_file_sha256 = eb95b69f…` for `backend/config/m0-baseline.json`, which
  matches neither committed version of that file in either line-ending form. It
  names a working-tree state from before this repository's first commit. The
  parameter manifest the result actually binds to is byte-identical across both
  revisions, so the declared friction parameters are intact; only the whole-file
  digest — which also covers a baseline-results block that was legitimately
  refrozen in `4edc0cf` — cannot be reproduced.
  `test_geodar_along_thalweg_artifact.py::test_geodar_result_is_bound_to_the_frozen_engine_and_spec`
  fails on this and is the repository's only failing test. Rewriting the digest to
  today's file would assert the result was produced under the current M0 baseline,
  which is false, so it awaits a human decision.

- **What did reproduce is the science.** On the same runs, the release field and every published summary
  were identical — `release_valid_cells` 1302 and release min/max/mean 74.375 on both sides — with only
  the runout ensemble's array hashes differing. So this is last-place floating-point movement, **not** a
  changed answer, and the published index is not in question. But until it is isolated, treat exact
  `output_sha256` replay as a property of one machine rather than of the model, and do not cite the
  baseline as cross-machine numerical evidence. Re-freezing it elsewhere would only move which machine
  is privileged, and AGENTS.md requires characterizing such a change rather than updating the snapshot.

## Deployment

See [`deployment.md`](deployment.md) for the full runbook. Limitations specific to the deployed form:

- **The public app uses HTTPS at `avalanche.gotlost.xyz`.** The raw ALB hostname intentionally retains
  plain HTTP for the assistant service's internal call to assess; it is not the QR-code-facing address.
- **The API is unauthenticated and unthrottled, and the condition for revisiting that has now been met.**
  `POST /api/assess` is a ~5 s, ~1.5 GB request that anyone who can reach the service may call. This was
  accepted while the only address was a random, unindexed AWS DNS name and the stack was raised for a few
  hours at a time; the earlier note said to revisit it **"the moment the deployment becomes permanent,
  gets a real hostname, or carries anything sensitive."** It now has a real, stable, QR-code-facing
  hostname, so the unguessable-address half of that reasoning no longer applies and **adding
  authentication or rate limiting is the first thing to change here.** What still holds is the bounded
  blast radius: Fargate bills per task-hour so request volume cannot amplify cost, `DesiredCount` is
  capped at 2, and the worst case is a container reaching its 4 GB ceiling and being restarted by ECS,
  which is self-healing. The same gap covers the body-size limit, which reads `Content-Length` and so
  does not bound a chunked request.
- **The AI assistant depends on an operator's own machine being awake**, reached through a Cloudflare
  Tunnel. The map, terrain and hazard model are unaffected by its absence; only the assistant degrades,
  to a clean 503. A quick tunnel is also **public and unauthenticated** — the hostname is unguessable but
  anyone who learns it can send prompts to that machine, so it should be raised for a session and taken
  down afterwards.
- The deployed hazard model and the local one are the same code path; the assistant calls the assess
  service rather than computing anything itself, so the single-place-attaches-the-disclaimer property
  (invariant I3, below) holds identically in both shapes.

## Data provenance and safety

- **Missing data is reported as missing, never converted to zero or read as "safe" (invariant I3).** When
  no release zone crosses the threshold, hazard falls back to the 95th percentile of the release estimate
  on avalanche terrain and is labelled as such — a quiet day is a low number with a reason, not a zero.
  The same rule governs the composite index: an unsimulated zone's reach, an absent exposure layer, and a
  runout footprint entirely outside exposure coverage are each reported unavailable with a stated reason,
  and none of them lowers a number.
- **The AI assistant never invents the safety disclaimer or the numbers.** It explains an assessment the
  deterministic code produced, and scenario chat is parse-to-sliders → run the real `/assess` → narrate.
  The disclaimer is appended in code, never generated. Without a local Ollama server the assistant returns
  503 and the rest of the app is unaffected.
- Any output here requires independent validation before any operational use.
