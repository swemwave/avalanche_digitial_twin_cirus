# Public validation evidence: acquisition routes and no-outreach methods

Last reviewed: 2026-08-14

## Claim boundary

No source listed here is trusted merely because it is public. An event becomes
quantitatively usable only after it passes validation-contract v3, its immutable
identity is code-reviewed, and its complete protected cohort passes the frozen
experiment. The global validation status remains false.

This is an experimental research prototype. It does not replace Avalanche Canada
guidance or field assessment. Scores are relative indices, not probabilities.

## Bottom line

There is no known ready-made public download containing 12 dry dense-slab events
that satisfies Profile C. The public-only construction route below was executed
through the frozen gate 8 on 13 August 2026 and failed with zero eligible events:

1. use untouched professional RegObs records for independent release geometry,
   crown-height evidence, timing, grouping discovery, and event photographs;
2. create new runout targets blind to model output from open pre/post Sentinel-1
   and Sentinel-2 imagery;
3. use open Norwegian terrain with acquisition and vertical-datum lineage;
4. use a predeclared, source-supported density distribution or bounded interval,
   which validation-contract v3 explicitly permits, rather than pretending that
   a generic density is an event measurement; and
5. initially score terminal-endpoint error only, because it does not invent
   negative labels or require a complete deposit-survey polygon.

This route failed eligibility. In particular, a RegObs crown height is not
accepted as normal-to-slope release thickness, a mapped stop is not automatically
the terminal dense-flow toe, and neither feature has quantified boundary
uncertainty as published. Those fields must be established by a reproducible,
blind observation method or the event must be excluded.

The executed funnel acquired 26/26 Sentinel-1 pairs, 23/26 Sentinel-2 pairs,
139 original RegObs attachments across 25 candidates, and 25/26 dated pre-event
Høydedata screening chips. It produced zero complete observation-QA passes, zero
independent human reviews, zero eligible release-thickness records, zero
event-surface-eligible DEMs, and zero Profile R/C/E events. The exact disposition
and hashes are in
[`public-event-strict-funnel-v5.json`](../validation-data/candidates/public-event-strict-funnel-v5.json).
Twenty-five immutable packets are released for external annotation; release is
not evidence acceptance. See
[`public-event-human-review-procedure.md`](public-event-human-review-procedure.md).
The 12-event/six-path/two-mountain/three-storm cohort gate was not weakened, so
the workflow stopped before partitioning, AvaFrame integration, calibration, or
holdout evaluation.

## Existing untouched public candidate pool

The frozen inventory at
[`validation-data/candidates/public-event-candidates-v1.json`](../validation-data/candidates/public-event-candidates-v1.json)
contains 40 high-competence RegObs dry-slab candidates. Without opening a model
prediction or assigning a holdout, the inventory shows:

- 26 records with a reported fracture/crown height and a stop point;
- 21 of those 26 also with a stop extent;
- 39 records with one or more attachments;
- 14 distinct forecast-region identifiers; and
- zero events currently eligible for Profiles R, C, or E.

The public [RegObs API v5](https://api.nve.no/doc/regobs/) is openly queryable for
observation searches and is licensed under NLOD. Its data are supplied as-is, so
professional observer competence does not replace event-level evidence review.

A second public discovery set,
[Central Spitsbergen Snow Avalanche Activity 2016–2020](https://doi.org/10.17632/dv4m9bbn9y.2),
contains 632 manually reviewed RegObs-derived avalanche records. It is useful for
finding events and storm groups, but its tabular points are not release/deposit
geometry and are not Profile C evidence by themselves.

## Implemented satellite-catalogue preflight

The versioned metadata-only preflight is implemented by
[`scripts/validation/build_public_event_imagery_preflight.py`](../scripts/validation/build_public_event_imagery_preflight.py).
Run it from `mount-hosmer-digital-twin/`:

```powershell
python scripts/validation/build_public_event_imagery_preflight.py
python scripts/validation/build_public_event_imagery_preflight.py --offline
```

The first command anonymously queries the current official [Copernicus Data
Space Ecosystem STAC v1 catalogue](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)
for the `sentinel-1-grd` and `sentinel-2-l2a` collections when an immutable
response is not already cached. The second command prohibits network access and
requires complete replay from
`.validation-cache/public-event-imagery-preflight-v1/`. The cache is gitignored;
request and raw-response bytes are immutable and hash-bound, and a differing
request fails instead of replacing cached bytes. STAC requests exclude `assets`,
and no raster URL is followed.

The source selection is derived rather than hard-coded: RegObs candidates must
have a non-null reported fracture/crown height and either published stop-point or
stop-extent presence. This selects 26 of the 40 frozen RegObs candidates. The
script reads only the public observation-location discovery point for catalogue
intersection. It does not read attachments or start/stop target coordinates.

The pairing rules were frozen before candidate availability was examined:

- preserve the provider's original offset-bearing event timestamps and convert
  the same instants to UTC using ISO-8601 offset arithmetic;
- preserve a missing earliest time as null and transparently use the provider's
  latest time as a zero-duration pairing instant;
- search 18 days before the event start through 18 days after the event end;
- require every returned footprint to contain the discovery point and require a
  strictly pre-start and strictly post-end acquisition;
- for Sentinel-1, require identical non-missing orbit direction, relative orbit,
  acquisition mode, and polarization set; and
- for Sentinel-2 Level-2A, require the same non-missing MGRS tile while recording
  acquisition time, processing level, and catalogue cloud percentage.

Every bracketing combination is retained with its temporal baseline,
compatibility checks, and all rejection reasons. No pair is selected as “best.”
`catalogue_pair_found`, `requires_pixel_qa`, and `no_qualifying_pair` are separate
states. In particular, catalogue cloud percentage describes the whole product;
it is not local clear-sky proof and is never used as a usability threshold.

The frozen 2026-08-13 acquisition produced
[`validation-data/candidates/public-event-imagery-preflight-v1.json`](../validation-data/candidates/public-event-imagery-preflight-v1.json)
with these exact counts:

| Result | Sentinel-1 GRD | Sentinel-2 Level-2A |
|---|---:|---:|
| Source candidates queried | 26 | 26 |
| Candidate acquisitions | 636 | 621 |
| Candidates with at least one catalogue pair | 26 | 23 |
| Candidates with no qualifying pair | 0 | 3 |
| Qualifying pair combinations | 676 | 3,426 |
| Rejected pair combinations | 3,484 | 1,492 |
| Candidates requiring later pixel QA | 26 | 23 |

The three Sentinel-2 no-pair states remain explicit: `regobs-354318` and
`regobs-358192` have no strictly post-event catalogue acquisition in the frozen
window, while `regobs-448389` has no strictly pre-event acquisition. The live
artifact and immutable-cache replay were byte-identical (file SHA-256
`10496db7a7ae8bab4314efb3c00ed9568126281ead7f23701540e54179810866`).

These counts establish catalogue availability only. No imagery pixels were
opened, no avalanche was interpreted, and no event became contract-eligible or
a holdout. The preflight generated no prediction and provides no field-validation
or improved-accuracy evidence.

## Public-only Profile C construction method

### 1. Freeze selection without model output

Build a versioned candidate-funnel revision from source metadata only. Retain an
event only when dry dense-slab regime, a bounded event interval, release extent,
crown-height evidence, a plausible terminal observation, and enough public
imagery are all present. Selection must never use preliminary model fit.

Derive path and storm groups from public spatial and temporal metadata, then
review them manually. The final cohort still requires at least 12 scoreable
events, six independent paths, two mountains, three storm cycles, and six
untouched holdout events.

### 2. Acquire imagery without provider outreach

The [Sentinel-1 AWS open archive](https://registry.opendata.aws/sentinel-1/)
provides the global GRD archive as cloud-optimized GeoTIFFs and supports unsigned
downloads without an AWS account. Use same-orbit pre/post pairs bracketing the
event. Sentinel-2 cloud-optimized imagery is also available through the
[AWS open-data registry](https://registry.opendata.aws/sentinel-2-l2a-cogs/).

Record product IDs, acquisition times, orbit, polarization/bands, processing
level, checksums, pixel spacing, projection, and every preprocessing operation.
Create explicit masks for SAR layover/shadow, low-backscatter areas, optical
cloud/shadow, forest, water, prior deposits, and scene boundaries.

Published Sentinel-1 work supports change-based deposit mapping, but also defines
important limits. Sentinel-1 primarily sees rough deposit zones rather than the
whole release–track–deposit footprint, has an approximate lower detection size,
and can miss dry deposits or confuse snow-wetness changes. See
[Kneib et al. (2024)](https://doi.org/10.5194/tc-18-2809-2024) and
[Eckerstorfer et al. (2020)](https://doi.org/10.5194/nhess-20-1783-2020).
The open [AvalCD dataset](https://doi.org/10.5281/zenodo.15863589) supplies
aligned pre/post Sentinel-1 channels, terrain layers, avalanche masks, and a
reproducible patch generator that can be used to verify the preprocessing and
annotation tooling. Its existing masks are not automatically event-specific
release or terminal-toe observations for this experiment.

### 3. Create blinded runout observations

Before any evaluated output exists, prepare annotation packets containing only source
imagery, terrain context, acquisition metadata, and observation instructions.
Do not include evaluated layers, fitted settings, or another reviewer's work.

For each candidate:

1. have two genuine reviewers separately annotate release, terminal dense-flow deposit, and distal toe;
2. record whether dense-flow attribution is clear or ambiguous;
3. keep both reviewers blind to evaluated outputs and one another's submission;
4. combine inter-reviewer disagreement with pixel size, product geolocation, terrain
   projection, temporal-window, and interpretation uncertainty;
5. preserve both annotations, the consensus rule, exclusions, and hashes; and
6. reject rather than repair ambiguous, overlapping, truncated, or powder-only
   events.

Endpoint-only evidence is the attainable first target. A terminal toe with
quantified uncertainty can support signed and absolute endpoint error without a
complete negative survey. Deposit IoU, precision, or false-positive area remains
unavailable unless an interpretable survey footprint and detection mask justify
known absence.

### 4. Construct the observed release state conservatively

Treat the RegObs release extent as candidate source geometry, not automatically
as a trusted polygon. Preserve its original EPSG:4326 coordinates, source record,
observer competence, mapping method when available, and attachment lineage.

The reported crown height may be converted to normal-to-slope thickness only if
the provider field semantics are verified. The conversion rule, local slope
source, units, and propagated uncertainty must be frozen without access to the
runout target. If the field semantics or uncertainty cannot be established, the
event is excluded.

Validation-contract v3 accepts `measured_value`, `bounded_interval`, or
`distribution` representations for release thickness and density. A public-only
experiment can therefore predeclare a literature-derived density distribution.
It must be represented honestly as transferred prior evidence, never as an
event measurement. Useful primary evidence includes:

- [Stethem and Perla (1980), 30 Whistler slab avalanches](https://doi.org/10.3189/S0022143000010613),
  which reports field-measured slab densities and an average near 220 kg/m³;
- [Lautaret full-scale test site](https://doi.org/10.1016/j.coldregions.2015.03.005),
  which reports roughly 80–160 kg/m³ for early-winter dry release snow; and
- [Sovilla et al. mass-balance measurements](https://doi.org/10.3189/172756401781819058),
  which documents directly measured release and deposit mass-balance cases.

Do not select a density range from model performance. Extract the source event
values and applicability criteria first, define a conservative distribution and
transfer-uncertainty statement, hash that artifact, and then keep it fixed for
all development and holdout predictions. A sensitivity result based on this
prior is conditional on the prior; it does not become an event-specific density
validation.

### 5. Acquire terrain with public lineage

Norway's [Høydedata service](https://www.kartverket.no/api-og-data/terrengdata)
provides free DTM/DSM and point-cloud downloads plus APIs. Prefer the best
pre-event terrain project for each path. Record project/acquisition dates,
horizontal CRS, vertical datum, resolution, file hash, and transformations.

A bare-earth DTM is not the avalanche-day snow surface. Quantify or conservatively
bound snow-depth, vegetation, and acquisition-epoch mismatch; never declare the
surface error zero. Exclude an event when no defensible mismatch treatment is
possible.

### 6. Seal and score

Complete eligibility review before partitioning. Seal target imagery,
annotations, and observation manifests so prediction code cannot read them.
Freeze all group memberships, source hashes, model/configuration/bake identities,
release-state priors, seeds, and parameter ranges. Use development events to
choose any threshold or parameter strategy, then run the complete protected
holdout once.

If fewer than 12 events pass, publish the failed funnel. Do not weaken uncertainty,
component attribution, path independence, or missing-data rules to reach the
number.

## Open packages that remain useful without being final field evidence

| Resource | Public use | Scientific role here |
|---|---|---|
| [AvaFrameData v1.0](https://doi.org/10.5281/zenodo.20701552) | Six event simulation packages with release layers and several deposit layers | Development only; all targets were already viewed and key uncertainty/release-state fields remain absent. |
| [Brämabühl January 2019](https://doi.org/10.5281/zenodo.15796703) | Three cold controlled avalanches, drone snow-height product, orthophoto, terrain, and release/deposit layers | Development and observation-tool verification; viewed events cannot be final holdout. |
| [Vallée de la Sionne GEODAR](https://doi.org/10.5281/zenodo.1042108) | Radar data and front trajectories for 77 events | Dynamics falsification/development; not terminal-deposit Profile C evidence as published. |
| [Vallée de la Sionne event 20243024](https://doi.org/10.5281/zenodo.17104410) | Optical velocity, high-speed camera, GEODAR, pressure, images, and video | Sensor-processing development; one event without public GIS release/deposit target. |
| [OpenFOAM-avalanche Wolfsgruben tutorial](https://doi.org/10.5194/gmd-17-6545-2024) | Open solver plus CC BY 3.0 real-case tutorial data | Code-to-code/numerical benchmark, not a new independent holdout. |
| [Avalanche deposits on mountain glaciers](https://doi.org/10.5281/zenodo.10895011) | Sentinel-1 deposit outlines and mapping code | Remote-sensing method validation; source release state and event pairing are incomplete for Profile C. |
| [AvalCD](https://doi.org/10.5281/zenodo.15863589) | Multi-region pre/post SAR, terrain, polygons, masks, and patch code | Preprocessing/annotation benchmark and possible candidate discovery, not automatically Profile R/C evidence. |

These packages can improve software verification, characterize numerical
behavior, and test the ingestion/annotation workflow. They must not be pooled
and labelled as 12 independent field-validation events merely to satisfy a count.

## Remaining public-only failure modes

The no-outreach route still fails if any of these cannot be resolved from public
artifacts:

- source release polygons have no defensible mapping method or uncertainty;
- crown-height semantics cannot support slope-normal release thickness;
- a literature density prior is not transferable to the declared regime;
- the image does not show an unambiguous terminal dense-flow toe;
- the observation window contains overlapping events or a persistent old deposit;
- SAR/optical terrain distortion cannot be bounded;
- terrain epoch, vertical datum, or event-surface mismatch is unresolved;
- path and storm groups cannot be established without leakage; or
- fewer than 12 events across six paths survive review.

## Provider-held fallback, retained for future use

If outreach becomes possible, the most promising holders are:

| Holder | Public contact | Likely holdings |
|---|---|---|
| BC Ministry of Transportation and Transit | `TTWebmaster@gov.bc.ca`, requesting referral to the Central Avalanche Program/SAWSx data steward | Quality-reviewed BC highway occurrence, weather, snowpack, control, closure, and path records. |
| Parks Canada, Rogers Pass | `mrg.information@pc.gc.ca`, requesting referral to the avalanche program | Daily snow/weather observations and operational records across 135 paths. |
| WSL/SLF | `data@slf.ch` | Vallée de la Sionne measurements and manually reviewed destructive-avalanche records. |
| NGI Ryggfonn | `heidi.hefre@ngi.no` or `peter.gauer@ngi.no` | Full-scale release, runout, velocity, pressure, camera, laser, and weather records. |
| Avalanche Canada | `research@avalanche.ca` or its data-request form | Research batch requests and possible coordination with Canadian operations. |

For any future request, ask for more candidates than the 12-event minimum and
require original release/deposit/endpoint geometry, release thickness and density,
feature uncertainty, observation methods and dates, coverage semantics, event
time, terrain/snow-surface lineage, source CRS and transformations, licence,
stable versions, and file hashes.

The send-ready, protocol-bound request and fillable JSON Schema are checked in as
[`field-validation-owner-request-v1.json`](../validation-data/acquisition/field-validation-owner-request-v1.json)
and
[`field-validation-owner-delivery-v1.schema.json`](../validation-data/acquisition/field-validation-owner-delivery-v1.schema.json).
Owner-specific messages for SLF/Monte Pizzac, INRAE Lautaret, NGI/Ryggfonn,
Seehore, BFW/WLV and Parks Canada are rendered verbatim in
[`field-validation-owner-requests-v1.md`](../validation-data/acquisition/field-validation-owner-requests-v1.md);
they have not been sent. Their contact routes were rechecked against the owners'
official sites on 15 August 2026 UTC.
The corresponding preflight verifies returned file sizes and SHA-256 values and
immutable licence bindings and reports cohort diversity, but deliberately leaves
trust, eligibility, calibration/holdout assignment, and predictions false. The
subsequent workflow requires two identity-verified humans per event, third-human
conflict resolution, exclusion records for ineligible events and adjudication of
every delivered candidate. AI output is structurally barred from counting as a
review. The frozen no-event grouped-split procedure and its implementation hashes
are recorded in
[`field-validation-acquisition-integrity-v1.json`](../validation-data/acquisition/field-validation-acquisition-integrity-v1.json).
