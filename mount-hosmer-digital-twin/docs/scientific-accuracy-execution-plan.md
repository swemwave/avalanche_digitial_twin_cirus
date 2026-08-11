# Scientific Accuracy Execution Plan

## 1. Objective

Evolve the Mount Hosmer Avalanche Digital Twin from a static-terrain,
slider-driven research model into a reproducible physics-based hindcast and
scenario system, with an optional near-real-time data path.

The target is the greatest scientific fidelity supported by available evidence.
This plan does **not** authorize calling the application an operational forecast,
claiming probabilities, or replacing Avalanche Canada guidance and field
assessment.

## 2. Accuracy ceiling

High-resolution terrain alone cannot determine present avalanche conditions. The
largest current uncertainties are:

1. No simulated snowpack history or weak-layer state.
2. No measured or reconstructed spatial loading state.
3. Uncalibrated release rules and thresholds.
4. Runout without release mass, slab thickness, entrainment, or locally fitted
   friction parameters.
5. No trusted local avalanche-event dataset for calibration and holdout testing.

AI may discover, normalize, test, and explain authoritative data. AI-generated,
model-inferred, or image-guessed avalanche events are not field evidence and
must never be registered as such.

## 3. Target architecture

```text
Mountain Pack (static terrain)
        |
        v
Terrain bake: DEM, derivatives, canopy, masks and provenance

Historical/live weather providers
        |
        v
Condition Pack: immutable hourly forcing snapshot
        |
        v
SNOWPACK first; Alpine3D only after a measured benefit
        |
        v
Snow State Pack: stratigraphy, weak layers, slab depth/density and uncertainty
        |
        v
Release model: terrain capability + modeled snow instability
        |
        v
AvaFrame high-fidelity runout + existing engines as independent baselines
        |
        v
Ensemble, validation metrics, provenance, limitations and concise UI
```

All network and geospatial processing remains offline. The serving application
continues to read only immutable products from `runtime/baked/` and retains no
runtime dependency on GDAL, rasterio, pyproj, SNOWPACK, Alpine3D, AvaFrame,
pandas, or GeoPandas.

## 4. Execution rules

### AI may execute autonomously

- Inspect and modify project code within the requested milestone.
- Build strict schemas, provider adapters, offline workers, tests and synthetic
  fixtures.
- Retrieve public metadata and small test samples from documented providers.
- Run software verification, numerical characterization, lint and builds.
- Create source-lineage records, checksums, missing-data masks and coverage
  reports.
- Draft data-access requests for review.
- Update durable contracts and limitations when behavior changes.

### AI must stop or request approval before

- Downloading unusually large datasets or running an expensive real-data rebuild
  when a synthetic or clipped test can answer the question first.
- Sending messages, accepting special licence terms, creating accounts, or
  contacting an external organization on the client's behalf.
- Publishing, deploying, or exposing the service externally.
- Changing a scientific threshold merely to improve a result or snapshot.
- Calling any result calibrated, validated, probabilistic, or operational without
  eligible evidence.

### AI must never

- Modify files under `DATA/`.
- Convert missing inputs into zero, open terrain, or a safe-looking score.
- Invent weather, snow profiles, avalanche observations, release values or
  runout measurements.
- Use the model's own output as calibration or field validation.
- Mix observations from the same avalanche event across calibration and holdout
  partitions.
- Let imagery, POIs or AI narrative silently influence physical hazard terms.

## 5. Milestones

Milestones are sequential unless explicitly marked as parallel. A later model
must not enter the default assessment until its acceptance gate passes.

### M0 — Freeze and characterize the current baseline

Purpose: ensure later changes are measurable and reversible.

AI execution:

- Record the current release and runout parameter manifests in bake identity.
- Preserve synthetic numerical benchmarks for benign, loaded, missing-data and
  AOI-boundary cases.
- Add a reproducible comparison command that runs old and candidate engines on
  identical terrain, inputs and seeds.
- Record runtime, peak memory, input coverage and output hashes.
- Confirm wind direction, coordinate order, metre units and masks.

Acceptance gate:

- Existing Mount Hosmer numerical behavior remains unchanged.
- `python -m pytest`, frontend lint and frontend build pass.
- Every later numerical change can be compared against this baseline.

### M1 — Condition Pack contract and replay interface

Purpose: give offline historical data and future live data one deterministic
input format.

AI execution:

- Add a strict `ConditionPack` schema containing:
  - mountain-pack and grid identity;
  - source and licence statements;
  - acquisition, publication and valid times;
  - hourly variables with explicit units;
  - station coordinates and elevation;
  - observed, analyzed, forecast and gap-filled status per value;
  - QC flags, masks, uncertainty and staleness;
  - source-file and normalized-output hashes.
- Require temperature, humidity, wind speed/direction, precipitation phase and
  amount, pressure, shortwave and longwave radiation or an explicit documented
  derivation.
- Add a provider protocol that returns normalized data but cannot write to
  `DATA/`.
- Write completed packs to `runtime/baked/conditions/<condition_id>/` atomically.
- Add CLI validation and malformed/missing/unit-conversion tests.

Acceptance gate:

- Replaying the same source snapshot produces the same Condition Pack identity.
- Gaps remain missing or explicitly gap-filled with lineage.
- No provider-specific assumptions enter `avycore`.

### M2 — Historical meteorological forcing

Purpose: reconstruct full-winter forcing without a live connection.

Provider order:

1. ECCC and PCIC station observations near the AOI.
2. BC snow stations for snow depth and SWE evaluation where representative.
3. HRDPA precipitation analyses where archived coverage is available.
4. ERA5-Land for temporally complete gap-fill, never as terrain-scale truth.

AI execution:

- Implement one provider at a time with recorded licences and sample fixtures.
- Build a station-selection report using horizontal distance, elevation
  difference, available variables, period coverage and QC status.
- Harmonize to UTC and one hourly timeline without interpolation across long
  gaps.
- Downscale temperature by documented elevation correction.
- Separate rain and snow using a characterized method with an uncertainty band.
- Correct precipitation and wind only through explicit, versioned algorithms.
- Compare overlapping providers; never silently choose whichever produces a
  desired loading result.
- Produce a forcing-quality report for each snapshot.

Acceptance gate:

- A complete selected winter can be replayed from immutable sources.
- Coverage, gap-fill fraction and provider disagreement are reported.
- Withheld station comparisons report bias and error for available variables.
- No current-condition claim is made from historical or climatological data.

Current characterized increment: immutable ECCC full-winter replay, an ECCC/NAV CANADA withheld-station
comparison, and an independent PCIC/source-provider wind disagreement assessment are implemented. The
PCIC selection is ENV-AQN 585/history 14942, originally Teck Coal Limited - Greenhills Operations station
E290310, under the exact OGL-BC 2.0 provincial dataset record. A separate content-addressed report now
reconstructs the 2496.78 m reference as a rounded integer-array midpoint, samples the requested coordinate
with explicit cell-centre and four-cell footprints, characterizes lapse-rate sign/bounds/sensitivity and
same-provider temperature disagreement, distinguishes hourly precipitation/derived phase, preserves calm
wind circular handling, and quantifies station elevation coverage across the baked AOI. It activates no
new correction. The direct OGL-BC Morrissey Ridge SWE record is blocked on an undocumented 2C09Q-to-MOR
history boundary; BC Hydro's current MOR-to-2C09P mapping now directly conflicts with a MOR-to-2C09Q
merge. A BC Hydro metadata link unexpectedly returned a live current-value response, but it was not saved,
normalized, joined, or used and no bulk observation resource was downloaded. No eligible representative
radiation series or complete derivation inputs were found. The v1.2 audit finds no PCIC radiation history
spanning the winter and identifies ERA5-Land only as an unacquired modelled candidate: CDS access requires
a user account, personal token and manual terms acceptance, and its interval-energy semantics need a
reviewed adapter. A controlled current-code rebuild now passes the bake contract and is
byte-identical across all eight terrain/provenance arrays to the preserved bake. A provider-neutral
reference-elevation contract proposes the bilinear value without activation. The v1.2 characterization
adds a bounded literature-supported lapse-rate sweep, calm-wind masks and distributions for elevation,
slope, aspect, forest, curvature, source coverage and the target footprint. M2 remains incomplete:
snow-depth/SWE comparison, radiation, terrain-scale observations, and field validation remain unresolved.

### M3 — SNOWPACK representative-slope integration

Purpose: replace slider loading with physically evolved snow stratigraphy.

AI execution:

- Package SNOWPACK as an offline external process or container; do not add it to
  runtime imports.
- Review and record the SNOWPACK licence and exact version/commit.
- Generate SMET or another documented input format from a Condition Pack.
- Start simulations before winter snow accumulation so layer history is not
  invented at the requested scenario date.
- Run representative columns across:
  - elevation bands;
  - eight aspect sectors;
  - open and forested terrain;
  - selected ridge, bowl and valley terrain classes.
- Normalize outputs into a versioned `SnowStatePack` containing snow height,
  SWE, layers, density, temperature, liquid water, grain/weak-layer attributes,
  slab depth and available stability diagnostics.
- Preserve original output hashes and SNOWPACK configuration.
- Add failure, timeout, incomplete-output and deterministic replay tests.

Acceptance gate:

- The same terrain class, Condition Pack and configuration reproduce the same
  Snow State Pack.
- Snow depth/SWE are compared with withheld stations where available.
- Energy/mass and unit sanity checks pass.
- Modelled weak layers remain model output, not field observations.

Current isolated increment: the provider-neutral `SnowStatePack` v2 schema, strict complete-forcing
ConditionPack-to-SMET v1 adapter, bounded disposable process runner, corrupt/missing/timeout rejection,
atomic storage and synthetic redistributable fixtures are implemented outside serving imports. Exact
binary closure covers the executable and adjacent project-local DLLs; explicit configuration, forcing,
initial-state and site-parameter inventories are required. Full raw-run identity is separate from a
scientific-replay identity over exact model inputs and normalized physical output. Official SNOWPACK 3.7.0
is LGPL-3.0 at commit `349b857af07ddb090b3e7b36fb6a45ec87ec2338`. Two unchanged official-example
runs pass the executable/parser smoke gate and produce identical normalized physical output despite
run-history differences. This is software verification, not a Hosmer simulation. The current ECCC pack is
rejected because both radiation variables are missing; three other hours are also masked, so radiation
alone would not make it runnable. The v2 pack also remains intentionally limited to snow height, SWE and
surface temperature. The M3 acceptance gate remains incomplete and blocked on eligible complete forcing,
reviewed Hosmer initialization/site parameters and configuration, expanded snow-state outputs, energy/mass
checks, and independent snow-depth/SWE evaluation.

### M4 — Spatial snow-state mapping and Alpine3D decision gate

Purpose: map representative snow state onto terrain without pretending the
weather forcing has 5 m detail.

AI execution:

- Map terrain cells to snow-state classes by elevation, aspect, canopy and
  terrain position while preserving unknown coverage.
- Keep snow-state computation on a scientifically defensible coarser grid and
  retain the 5 m grid for release geometry and runout.
- Add topographic radiation and shading where not already represented.
- Quantify discontinuities introduced by terrain-class boundaries.
- Prototype Alpine3D only on a clipped synthetic/real test area.
- Compare Alpine3D against the representative-column approach on accuracy,
  reproducibility, compute cost and memory.

Acceptance gate:

- Spatialization improves a predeclared snow-depth/SWE validation metric or
  resolves a documented physical deficiency.
- Alpine3D is adopted only if its measured benefit justifies operational cost.
- No algorithm is selected because its map merely looks more realistic.

### M5 — Physics-informed release model

Purpose: require both capable terrain and modeled snow instability.

Initial scope: dry-slab release. Wet-snow, loose-snow, cornice and glide
avalanches remain separate future model types, not extra weights in one score.

AI execution:

- Define a release input contract for slab depth/density, candidate weak layer,
  failure-initiation diagnostic, crack-propagation diagnostic, loading rate and
  snow-state uncertainty.
- Retain slope, aspect, curvature, canopy and coherent-zone geometry.
- Derive release thickness and initial volume ranges from the snow state.
- Replace percentile-normalized curvature scales with characterized physical
  scales where supporting evidence exists.
- Keep the current release model as a baseline engine.
- Add synthetic tests for weak-layer continuity, aspect transitions, elevation
  bands, missing snow state and no-snow conditions.
- Publish an instability index until event calibration supports any stronger
  interpretation.

Acceptance gate:

- Terrain alone cannot create a high loaded-snow release result.
- Missing snow state excludes a cell and reduces coverage; it never lowers the
  result toward safety.
- Release depth, density, area and volume carry units and uncertainty.
- Intentional numerical changes are characterized against M0.

### M6 — AvaFrame high-fidelity runout

Purpose: add mass- and momentum-based dense-flow simulation.

AI execution:

- Integrate AvaFrame `com1DFA` as an offline external runner.
- Record exact software version, configuration, release polygon, thickness,
  density, friction, entrainment and DEM identity.
- Convert outputs into the existing GeoJSON/API geometry contract without
  changing their physical meaning.
- Preserve current alpha-angle routing as an independent empirical baseline.
- Preserve the existing particle engine as an experimental comparison until a
  review determines whether it still adds information.
- Add analytical/synthetic convergence tests, grid-orientation tests,
  missing-terrain barriers and deterministic replay checks.
- Reject or visibly truncate simulations that leave the AOI.

Acceptance gate:

- Mass balance and published numerical verification cases are reproduced within
  documented tolerance.
- Runout differences from existing engines are visible and explained.
- Velocity, depth and pressure remain simulation outputs, not validated impact
  predictions.
- Local accuracy remains unclaimed until M8 evidence exists.

### M7 — Uncertainty and ensemble execution

Purpose: replace one falsely precise output with traceable sensitivity.

AI execution:

- Define versioned distributions or bounded sweeps for forcing, precipitation
  phase, weak-layer strength, slab thickness/density, release extent,
  entrainment and friction.
- Use source uncertainty where available; otherwise label bounds as expert or
  literature assumptions.
- Ensure deterministic member seeds and identities.
- Report central footprint, full sensitivity envelope, parameter sensitivity and
  disagreement among independent runout engines.
- Keep ensemble frequency labelled as model frequency, not probability.

Acceptance gate:

- Every envelope is reproducible from member identities.
- The central footprint is included in the outer envelope.
- Dominant uncertainty contributors are reported.
- No confidence interval or probability wording appears without calibration.

### M8 — Trusted observations and physical validation

Purpose: measure closeness to real avalanches. This workstream starts in parallel
with M1 and remains the gate for all accuracy claims.

Human/client execution:

- Approve outreach and any data-sharing terms.
- Request professionally mapped events from relevant authorities, operators,
  transportation agencies, researchers or land managers.
- Confirm that supplied data may be used and redistributed as required by the
  open-source project.

AI execution:

- Discover authoritative inventories and draft outreach requests for approval.
- Register only data that satisfies `avycore.validation` and code-reviewed trust
  registry requirements.
- Preserve release polygons, deposits, endpoints, event dates, triggers, size,
  survey coverage and positional uncertainty.
- Partition by event, storm and season to prevent leakage.
- Evaluate only the complete compatible holdout cohort.
- Report:
  - release precision/recall and overlap inside surveyed coverage;
  - missed observed events and false releases;
  - runout endpoint distance error;
  - runout/deposit overlap;
  - depth or velocity error only where independently measured;
  - uncertainty coverage and failure cases.

Acceptance gate:

- At least one eligible calibration partition and an independent holdout
  partition exist before local validation claims.
- Model selection and thresholds use calibration data only.
- Holdout results include negative and failed cases, not selected successes.
- The trust registry and limitations are updated through code review.

### M9 — Optional near-real-time providers

Purpose: use the same Condition Pack interface for current data without making
request handling network-dependent.

Candidate feeds:

- ECCC HRDPS: approximately 2.5 km, four runs daily, 48-hour forecasts.
- ECCC HREPA/HRDPA: precipitation analyses and confidence/ensemble information.
- BC automated snow weather stations: snow depth, SWE, temperature and
  precipitation where available.
- PCIC station portal: multi-network BC observations.

AI execution:

- Implement scheduled provider polling outside the web request path.
- Validate publication time, valid time, station movement, units, duplicate
  records and provider QC flags.
- Save raw responses and immutable normalized snapshots with hashes.
- Build the current Snow State Pack by continuing from a hashed prior state or
  replaying sufficient history.
- Mark forecast, analysis and observation values distinctly.
- Expose feed age, expected update cadence and stale status.
- Fall back only to an explicitly selected historical/manual scenario; never
  present stale data as current.

Acceptance gate:

- Disconnecting all feeds cannot corrupt the last valid snapshot or affect
  static terrain service.
- Duplicate polling produces no duplicate scientific state.
- Staleness and partial-provider failure are visible in API and UI.
- Live and historical snapshots use the same deterministic downstream code.

### M10 — API and concise interface

Purpose: expose the improved science without adding ungrounded narrative.

AI execution:

- Add explicit scenario types: manual, historical hindcast and current snapshot.
- Require snapshot identity and valid time on every snow-informed assessment.
- Present only decision-relevant fields by default:
  - modeled instability index and meaning;
  - release zones and release volume range;
  - central runout and sensitivity envelope;
  - required-input coverage;
  - snapshot age;
  - validation status;
  - key limitations and deterministic disclaimer.
- Put detailed provenance and parameter manifests behind expandable panels or
  downloadable JSON.
- Restrict the assistant to deterministic result explanation and scenario
  requests through the real assessment service.

Acceptance gate:

- API, generated client and UI use the same schema.
- No probability, danger rating or operational wording is introduced.
- Missing, stale and unvalidated states are obvious without opening metadata.
- Backend tests, frontend lint and frontend build pass.

## 6. Evaluation metrics

No target threshold will be invented before available evidence and baseline
performance are characterized.

| Layer | Metrics |
|---|---|
| Meteorological forcing | coverage, gap-fill fraction, bias, MAE/RMSE, elevation representativeness, provider disagreement |
| Snow state | snow-depth and SWE bias/error, accumulation/melt timing, density and weak-layer agreement where observed |
| Release | precision, recall, IoU/overlap within surveyed domain, missed-event rate, false-release area |
| Runout | endpoint distance error, footprint/deposit overlap, area bias, AOI exits, mass balance |
| Uncertainty | holdout coverage by sensitivity envelope, calibration where eligible, dominant-parameter sensitivity |
| Software | deterministic hashes, numerical convergence, runtime, peak memory, mask and unit invariants |

Software verification, physical calibration and independent validation remain
separate statuses in all reports.

## 7. Data-source policy

Approved provider adapters must document source, licence, variables, units,
spatial/temporal resolution, update behavior, QC flags and known limitations.

Initial authoritative references:

- [SNOWPACK](https://snowpack.slf.ch/)
- [Alpine3D](https://alpine3d.slf.ch/Getting-started/)
- [AvaFrame](https://docs.avaframe.org/en/latest/)
- [ECCC HRDPS](https://eccc-msc.github.io/open-data/msc-data/nwp_hrdps/readme_hrdps-datamart_en/)
- [ECCC HREPA](https://eccc-msc.github.io/open-data/msc-data/nwp_hrepa/readme_hrepa_en/)
- [BC automated snow stations](https://www2.gov.bc.ca/gov/content/environment/air-land-water/water/water-science-data/water-data-tools/snow-survey-data/automated-snow-weather-stations-list)
- [PCIC data portal](https://www.pacificclimate.org/data)
- [ERA5-Land](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land?tab=overview)

Avalanche Canada forecasts and imagery-derived interpretations may provide
context. They are not independent field-validation observations unless their
exact records, permissions, evidence class and uncertainty satisfy the existing
validation contract.

## 8. Repository placement

New canonical behavior belongs in:

- `packages/avycore/src/avycore/conditions/` — normalized Condition Pack types.
- `packages/avycore/src/avycore/snow/` — normalized snow-state contracts and
  release inputs, without external model dependencies.
- `packages/avycore/src/avycore/hazard/` — release and runout orchestration.
- `backend/app/processing/conditions/` — provider adapters and downscaling.
- `backend/app/processing/snow/` — SNOWPACK/Alpine3D offline runners.
- `backend/app/processing/runout/` — AvaFrame offline runner and conversion.
- `tests/` — synthetic fixtures, provider samples, characterized numerical and
  validation tests.

External scientific packages remain offline tools. Compatibility facades must
not become canonical implementations.

## 9. Immediate AI execution sequence

1. Complete M0 baseline characterization without changing scientific output.
2. Implement and test the M1 Condition Pack contract.
3. Audit available Hosmer-area stations and historical coverage using metadata
   only.
4. Implement the first historical provider and one clipped winter replay.
5. Produce a forcing-quality report before integrating SNOWPACK.
6. Integrate one SNOWPACK representative column, then expand by terrain class.
7. Stop at each acceptance gate and report evidence, numerical changes,
   limitations and the next dependency.

M6 runout replacement and M9 live feeds must not jump ahead of the historical
forcing and snow-state gates. A faster interface or more complex algorithm is not
an accuracy improvement unless the relevant metrics demonstrate it.
