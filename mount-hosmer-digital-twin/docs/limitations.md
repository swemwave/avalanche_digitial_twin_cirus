# Limitations (Stage 3)

**Read this before making any claim about what the model can do.** This is a research prototype, not an
operational tool.

## The headline

- **This is not an operational avalanche forecast.** It must never replace Avalanche Canada forecasts or
  field assessment. Every hazard/release number is a **relative index (0–100), never a probability and
  never a forecast**, and carries a disclaimer attached in code (`app/core/model_config.py::DISCLAIMER`).
- **Nothing is calibrated.** The risk model, the release threshold, and the runout alpha angles are
  UNCALIBRATED values from the avalanche-terrain literature and Canadian Rockies practice. **None** is
  fitted to an observed Mount Hosmer avalanche, because **no eligible local avalanche-observation dataset
  is currently available to this project** — so the model has not been field-validated against ground truth.

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
  uncertainty geometries itself on an exact bake-bound EPSG:26911 grid; callers cannot substitute arbitrary
  observation or survey masks. It requires the complete compatible holdout cohort and binds predictions to
  model/config/bake/engine/seed/scenario identities and deterministic artifact hashes. Endpoint metrics bind
  predictions to every registered observation ID, require characterized field uncertainty, and report
  missing predictions to prevent survivorship bias. Missing model inputs remain excluded and visible.
- Contract-valid field data still cannot claim independent validation until its exact immutable dataset
  identity is added through code review to the trust registry. That registry is intentionally empty. This is
  ingestion/evaluation scaffolding, not a claim of end-to-end field-validation readiness. Until eligible
  observations are obtained and reviewed, API results label the available evidence as **software
  verification only**, with field validation unavailable and zero eligible events.

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

## Runout simulation

- Two engines (fast alpha-angle routing; advanced particle ensemble). Neither is field-validated against an
  observed Mount Hosmer runout. Alpha angles are published Canadian Rockies ranges, not local
  back-analysis. Every runout carries an explicit sensitivity envelope. Its reported area includes the
  central footprint and is not a band-only area or a statistical confidence interval; a line drawn with
  false precision is worse than no line. Advanced runout treats missing elevation, forest, or plan-curvature
  friction inputs as barriers rather than silently assuming open/neutral terrain.
- For interactivity, a synchronous assessment simulates runout only for the highest-scoring zones
  (top-12 fast / top-6 advanced). All release zones are still shown; the others are simply not run out.

## Terrain and the bake

- The 3D mesh and all terrain layers are **baked once, offline**, from 5 m BC-LiDAR (mosaicked to ~99.9 %
  AOI coverage), with Copernicus GLO-30 as gap-fill only. The running service reads no source data.
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
