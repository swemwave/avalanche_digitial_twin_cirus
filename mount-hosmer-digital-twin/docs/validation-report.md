# Avalanche runout verification and validation report

**Status:** `is_validated` remains `false`. The strict field-validation holdout
contains **N = 0 eligible events**, so no field-validation claim can be made for
any engine component. The completed work consists of software verification on
analytic terrain, a lower-rigor positive-only comparison on real terrain, a
failed 71-event field-kinematic consistency test at one instrumented path, and
a failed frozen blind release-plus-runout storm-window hindcast against 1,025
mapped-positive observations in four Swiss mountain blocks. A subsequent
anonymous-public RegObs/Sentinel/Høydedata funnel also failed before prediction:
all 26 candidates were excluded by the frozen evidence gates.
The requested validation on two or more mountains is therefore **not achieved**:
the registered events lie on several named greater-Davos slopes, but the source
evidence cannot support a strict holdout on even one mountain, much less an
independent multi-mountain claim.

This Digital Twin is an experimental research prototype, not an operational
avalanche forecast. It does not replace Avalanche Canada guidance or field
assessment, and its hazard and release scores are relative indices rather than
probabilities.

## AvaFrame 2.1 analytical verification

The official `avaSimilaritySol` case was executed through the isolated com1DFA
adapter only after its acceptance record was frozen. The record identifies the
AvaFrame 2.1 source commit
`9c3cefb89d626f5c0fb4c0329b41bc0c492ce175`, exact upstream input hashes, the
published 3 m / `aPPK=-3` / `nPPK0=15` / `cMax=0.02` comparison configuration,
seed 12345, metric formulas and limits. Its SHA-256 is
`4b3d9c724feae75183a4965f6959b4faf4b448bc37f277e22d470851fc873fa3`.

| Metric or invariant | Frozen acceptance | Measured at numerical time 20.04 s | Result |
|---|---:|---:|---|
| Downstream-front absolute error | <= 6 m (two cells) | 0 m | Pass |
| Flow-thickness relative L2 / L-infinity | <= 0.5 / 0.75 | 0.044353 / 0.079506 | Pass |
| Momentum relative L2 / L-infinity | <= 0.5 / 0.75 | 0.041823 / 0.067005 | Pass |
| Solver mass-balance relative error | <= 1e-12 | 0 | Pass |
| Analytical versus initialized-grid volume error | <= 0.05 | 0.0009325 | Pass |
| Grid / units / CRS / masks / boundary | exact identities; 3 m; local Cartesian CRS undefined; no invalid cells or boundary touch | 167 x 667, 3 m; expected units; CRS null and explicit; 0 invalid cells; no touch | Pass |

Direct speed relative L2 was 0.212955 but is diagnostic, not controlling: the
upstream similarity verification uses momentum to avoid dividing by near-zero
thickness at the front. Pressure is not applicable because this analytical case
defines no pressure target. Initial and final particle mass were both
8,034,977.497582 kg. The immutable result ID is
`da2d1446b3254510f5bd109e53d149e0687f3a98ec17cd7e96403a370f47bb55`;
its bundle contains the exact configuration, environment and distribution
versions, executable/adapter/input/output hashes, comparison arrays and
per-check pass/fail report. A second execution produced the same result ID and
byte-identical scientific artifacts. AvaFrame's path-dependent internal
simulation label is retained but excluded from that ID. This is analytical
software verification on idealized terrain, not calibration or evidence of
field accuracy.

## Flow-Py analytical energy-line verification

Flow-Py was executed through AvaFrame 2.1's `com4FlowPy` port in the isolated
offline adapter, against a closed-form solution rather than a stored snapshot.
The model routes flux with
`z_delta(next) = z_delta(current) + dz - ds*tan(alpha)`, clipped to `[0, max_z]`
(Neuhauser et al., 2022, Geosci. Model Dev. 15, 2423-2442,
doi:10.5194/gmd-15-2423-2022). Along a straight path from the release cell the
intermediate terms telescope, so on a planar slope the energy-line height is
exactly `(z_release - z) - s*tan(alpha)` and the flow stops at the last cell where
that value is positive. Both quantities are analytic.

The acceptance record was frozen before the graded run and carries its own content
hash, `02fde08629dcd572a747be1dc5139666ec54799914f4eef8f5e3ccedad0b6097`, which the
adapter verifies before launching the engine. It fixes the geometry (a 35 degree
plane for 60 cells followed by a 5 degree runout plane, 5 m grid, 120 x 41 cells,
EPSG:32611, single release cell at row 2), the parameters (`alpha` 25 degrees,
`exp` 8, `flux_threshold` 3e-4, `max_z` 8848 m, chosen so the clip is not
exercised), and the metric limits.

| Metric or invariant | Frozen acceptance | Measured | Result |
|---|---:|---:|---|
| Energy-line height, maximum absolute error | <= 0.01 m | 3.3357e-06 m | Pass |
| Stopping-cell difference from the analytical angle-of-reach intersection | 0 cells | 0 cells | Pass |
| Straight-line travel angle, maximum absolute error | <= 0.01 degree | 1.8980e-06 degree | Pass |
| Travel length, maximum absolute error | <= 0.01 m | 0 m | Pass |
| Units / CRS / coordinate order / masks / domain boundary | extent 1, energy line m, travel angle degree; projected metre CRS; x,y order; unreached cells are a modelled zero rather than masked; no boundary touch | as specified | Pass |
| Unsupported outputs declared | flow depth, flow velocity, flow pressure, arrival time | exactly those four, each with a reason | Pass |

The analytic and modelled last reached rows were both 95. The immutable result ID
is `flowpy-energy-line-28b1a18dc2e68aa36babddce8ebca52d7b7f1b77bb138b054a4adca11b367d91`
and the normalized runout result ID is
`runout-result-46b0eb589e227fd37cb0d87ea2ee87312696da091569878e3efd505f10c1d509`;
a second execution reproduced both identities and byte-identical arrays. The
bundle under `validation-data/benchmarks/flowpy-energy-line/` retains the effective
configuration, the environment inventory, and the executed `com4FlowPy` module
inventory, so the upstream implementation that produced the result is provable.

The canonical standalone Flow-Py distribution
([github.com/avaframe/FlowPy](https://github.com/avaframe/FlowPy), GPL-3.0-or-later)
was verified from primary sources and is a **separate, fail-closed engine
identity**: it is archived read-only since 2024-09-17, and its released `v1.0.3`
(commit `7b061599355cef584491d69eae2686307d286901`) reassigns `argv` to a hardcoded
example so the released command line ignores its arguments. Only the untagged
master commit `27ad81d3e804e4e9d85a9773fca10ee7dc428183` comments that out. See
`runout-engines.md`.

This is analytical software verification on idealized terrain. It is not
calibration and not evidence of field accuracy.

## AvaFrame com1DFA versus Flow-Py intercomparison

Both engines were driven from one normalized release and one terrain on the
synthetic case: com1DFA received the release polygons, com4FlowPy the release
raster, and neither consumed the other's output. Settings were the comparison
script's defaults (com1DFA Voellmy `mu` 0.155, `xi` 4000 m s-2, release thickness
0.8 m, density 200 kg m-3, 40 s simulation time, 0.1 s timestep, seed 12345;
Flow-Py `alpha` 25 degrees, `exp` 8, `flux_threshold` 3e-4, `max_z` 270 m).

| Metric | Value |
|---|---:|
| Extent intersection over union | 0.429 |
| Symmetric-difference area | 52 100 m2 |
| com1DFA-only area | 17 550 m2 |
| Flow-Py-only area | 34 550 m2 |
| Signed extent-area difference (com1DFA minus Flow-Py) | -17 000 m2 |
| Maximum reach, com1DFA | 455.4 m |
| Maximum reach, Flow-Py | 313.2 m |
| Maximum reach difference | +142.2 m |
| Common valid coverage | 1.00 |
| Single-result unknown area | 0 m2 |

Depth, velocity, pressure and arrival-time comparisons are reported `unsupported`
with the producing engine's reason attached, not as zero differences.

A bounded deterministic sensitivity sweep of one friction parameter per engine
(Flow-Py `alpha` +/- 3 degrees, com1DFA `voellmy_mu` +/- 0.03, both assumed
literature spans) gave, at 25 s simulation time: Flow-Py 58 050 / 73 700 /
93 125 m2 across 22 / 25 / 28 degrees with a 93 500 m2 outer envelope, and com1DFA
49 600 / 53 150 / 56 850 m2 across `mu` 0.185 / 0.155 / 0.125 with a 57 050 m2
envelope. The dominant contributor by area spread is the Flow-Py angle of reach
(35 075 m2 versus 7 250 m2).

**These are disagreement and sensitivity measurements only.** They quantify how
much the answer depends on the model and parameter choice. They are not accuracy,
they do not identify which engine is correct, and member frequency across a
deterministic sweep is model frequency, never a probability. Both engines remain
uncalibrated and unvalidated for any real site.

## Executed anonymous-public field-evidence funnel

The public-only route in `strict-field-validation-plan.md` was re-executed through
its mandatory gate 8 stop on 14 August 2026. No hazard/runout output was opened,
no parameter was fit, no development/holdout identity was assigned, and no
target was sealed as a holdout. The complete machine-readable disposition is
[`public-event-strict-funnel-v5.json`](../validation-data/candidates/public-event-strict-funnel-v5.json),
with its SHA-256 listed in the artifact table below.

| Evidence gate | Executed result | Eligible result |
|---|---:|---:|
| Frozen RegObs candidates | 26 | 0 |
| Sentinel-1 pairs GCP-geocoded with SAFE metadata | 26 / 26 | 25 terrain-processed; 0 pixel-QA passes |
| Sentinel-2 pairs with official product/granule/tile metadata | 23 / 26 | 0 pixel-QA passes |
| Anonymous primary public sources audited | 7 | 0 new eligible events |
| Isolated blinded AI passes | 52 passes / 156 component records | 0 geometry proposals; 0 human reviews |
| Immutable blinded annotation packets | 26 built / 26 released | 0 accepted as evidence |
| RegObs original attachments archived | 139 across 25 candidates | 0 reviewed quantitative observations |
| Independent human reviews | 0 | 0 accepted release/deposit/toe components |
| Reported crown heights | 26 | 0 eligible normal-to-slope thickness records |
| Pre-event Høydedata screening terrain | 25 / 26 | 0 event-surface-eligible DEMs |
| Validation-contract v3 Profiles R / C / E | 26 evaluated per profile | 0 / 0 / 0 |

The missing Sentinel-2 pairs are `regobs-354318`, `regobs-358192`, and
`regobs-448389`. The corrected Sentinel-1 GCP transformer reduced empty
polarization chips from 102 / 104 in v1 to 0 / 104 in v2 and produced
134,027,096 valid source pixels. A separate offline-replayable artifact then
reacquired all 104 polarization chips with nearest GCP resampling so source
amplitudes are not bilinearly mixed before `DN²`. SAFE calibration and noise
LUTs yielded 129,784,704 calibrated pixels. Relative to the preserved
bilinear-amplitude cache, 91.82% of overlapping calibrated float32 pixels changed;
the exact mean absolute linear-sigma change was 0.0107718. A deterministic
1-in-1,000 sample had median relative change 16.41% and 95th percentile 79.65%;
relative changes near zero backscatter are unstable and are not accuracy metrics.
The visibility-mask bands were byte-identical. Public screening terrain supported valid local
gradients on 1,663,044 processed pixel-observations, including 42,572 layover
and 36,474 radar-shadow pixels; 1,544,426 remained usable after radiometric and
terrain-visibility processing. These counts are processing coverage, not event
validation. The method is explicit local-incidence cosine normalization, not a
full area-based Range-Doppler terrain-flattening implementation.

Pixel-QA v3 now carries thirteen separately named bands: missing data, scene
edge, detection exclusion, survey coverage, cloud, cloud shadow,
topographic/cast shadow, forest, water, layover, radar shadow, prior deposit,
and usable. Inclusion and exclusion masks are distinct. An unknown forest or
prior deposit is retained under detection exclusion rather than falsely
labelled as a confirmed feature; unasserted survey coverage is null in the
evidence report rather than reported as measured zero. Sentinel-2 QA hash-binds
official product, granule, and tile metadata and preserves SCL meanings.
Sentinel-1 layover and radar shadow are now separate bands. All 26 events still
fail complete observation QA because independent component detection and survey
coverage are absent.

The anonymous primary-source refresh hash-froze seven official repository
records. In addition to AvalCD, the Kneib et al. glacier-deposit outlines, the
Brämabühl campaign, and the Lim et al. Davos UAS archive, it now includes
[AvaFrameData 1.0](https://zenodo.org/records/20701552), the
[Kitchener Avalanche Path mapping](https://zenodo.org/records/15233461), and
[Vallée de la Sionne 20243024](https://zenodo.org/records/17104410). These are
useful primary evidence, but none supplies the full per-event regime,
release/deposit/toe attribution, normal-to-slope thickness, event-surface,
complete-detection, cohort-diversity, and independent-review fields required by
v3. Kitchener represents one avalanche cycle plus rain runoff; VdlS 20243024 is
one instrumented path; AvaFrameData contains six solver-input events but not a
complete strict cohort. Provider metadata was not treated as inspected ground
truth. The refresh adds zero eligible events.

The RegObs attachments and provider start/stop geometry were inspected only as
source evidence. Twenty-six immutable, blinded ZIPs are now released for human
annotation. Together the archives contain 1,069 hash-checked
source files and total 772,546,248 bytes. Each released ZIP contains source
imagery and attachments, metadata, explicit QA masks, instructions, separate
reviewer-A/B forms, uncertainty fields, and release/deposit/toe tasks. It omits
provider target geometry, internal candidate identity, evaluated outputs, and
peer submissions. Release for annotation is not acceptance as evidence.

Two isolated AI passes were regenerated against pixel-QA v3. All 156 component
records remain null abstentions with `ai_generated_only=true`. They are stored
separately and cannot satisfy the human-review importer. Concordant abstention is
uncertainty evidence only, never an observed empty component or human review.
The external procedure and machine command are in
[`public-event-human-review-procedure.md`](public-event-human-review-procedure.md).

NVE defines `Bruddhøyde` as the height of the crown edge. That verifies the
field's plain-language meaning, but not a normal-to-slope measurement direction,
method, or uncertainty. The centimetre values were retained and unit-converted
for audit only; no release thickness was invented. Separately, a transferred
dry-slab density prior was frozen as Uniform(200, 250) kg/m³ before any runout
result access. Its bounds follow Stethem and Perla's field-measured density band;
the uniform shape is an explicit project choice, not a claimed empirical
distribution or event measurement. Sources: [NVE Varsom field definition](https://www.varsom.no/snoskred/snoskredskolen/del-informasjon/ulykke-nestenulykke/)
and [Stethem and Perla (1980)](https://www.cambridge.org/core/journals/journal-of-glaciology/article/snowslab-studies-at-whistler-mountain-british-columbia-canada/8D0B62C03C0C8873DC2F9FDCE9D11F50).

Kartverket Høydedata supplied 25 anonymous, hashed 10 m screening chips selected
from the latest dated source project not after each event. `regobs-448389` had no
dated pre-event project in that service. Every acquired DTM retains project CRS,
metre units, height-reference EPSG identity, flight epoch, transformation and
raster hash. None is an avalanche-day snow surface, and snow-depth, residual
classification, horizontal, and vertical accuracy terms were not quantitatively
available. The screening chips therefore remain ineligible solver terrain.

Gate 8 observed **0 eligible events, 0 frozen paths, 0 mountains, and 0 storms**
against the immutable minimum of **12 events, six paths, two mountains, and
three storms**. Requirements were not weakened. Gates 9–13 were stopped as
required: there is no grouped split, AvaFrame integration for this cohort,
AvaFrame model calibration, one-time holdout run, or component-validation
promotion. Sentinel-1 radiometric calibration above is evidence QA, not model
calibration. The trust
registry remains empty and `is_validated=false`.

The exact 18 original failing predicates are now embedded in the v5 funnel artifact with
their code predicates, required evidence fields, and classifications. Three
technically resolvable predicates were legitimately closed: all 26 event times
were normalized to UTC with frozen low/medium confidence and original offsets;
the release-to-runout rule was frozen for all 26; and all 26 packets were
released for annotation. Fifteen distinct blockers remain, and every candidate
has 15 blockers. Of the original predicates, three were technically resolvable,
five require better public primary evidence, and ten require genuine independent
human action; the complete per-check predicates and required fields are embedded
in the strict artifact.

Key execution artifacts are byte-identical under offline replay where supported:

| Artifact | SHA-256 |
|---|---|
| Imagery acquisition v2 | `4a019bb04ff5b74766942a4b8376218597bd80b2c4b30e3fc5dbf8853da731a9` |
| Sentinel-1 nearest-DN acquisition | `b9618818688ab54370707b477504d4e512ff248e0ef8e8956b2373d7d5a17c1b` |
| Sentinel-1 processing | `7d1c9ea383302dbfde79e161d6c358335f3c624d7461f936c0ed7b0f6390b763` |
| Pixel QA v3 | `b0f64381ea989eae06bf87e157e2b418110a92958641e2ee6fe0fba506ddc0cb` |
| Public-source audit v2 | `a4c1ef834d0f1da97f43d939c17da692f355a054066fa28ea85dc5c37e4b66ae` |
| AI annotation proposals | `43a1537d196ee4c64674c5ba8310fa15d847005692eddfd4731334fcda80bffc` |
| Blinded packets v4 | `8a1b68091a23a6b53a9a4f33a2b7f7115088a5dcd4a62055d7606f0a7d73337d` |
| RegObs evidence | `87ea8956f8d5340de7d9eb3dd82f3d674022f2c487e0462e5b0d964b24086fe5` |
| Technical evidence freeze v2 | `74f792e62356c94f52580bb29849dc4d4dbb0eba7b6fcacfd0fcb71325a22647` |
| Human-review status v5 (zero reviews) | `dd059056f6314ada22048b6fb9bdd978a077bf3bc81abece87f20a9dbf24086f` |
| Release-to-runout rule | `56de2b5c1ffe6895add75def7084fad3884d69542b3438e940b3ac7c535754d0` |
| Release-state evidence | `7525a802aefb02bc0159b551aa2b26c6a5b0cf5b9937967fd5455f0fd661c6f7` |
| Terrain acquisition | `35b17fac59423e69e86aeb0a7e4e876d5f47f614633e5a10804fa620b835c692` |
| Strict funnel v5 | `ecd03c92b6b6bc1e10da6d5833c31f15ab2610beb2dd9436482f697714bd0827` |

## Claim boundary

The component under test must travel with every result in the serialized fields
`engine_mode` and `component_tested`:

| Engine mode | Component a result can test |
|---|---|
| `alpha_only` | The empirical alpha angle together with downslope grid routing and the minimum-flux cutoff. There are no Voellmy velocity dynamics in this mode. |
| `dynamics_only` | The particle integrator, Voellmy friction, terrain sampling, and seeded path spreading, without the empirical alpha stopping line. |
| `hybrid` | The particle dynamics plus the empirical alpha energy-line stop. Extent is still bounded by alpha and is not an independent validation of Voellmy runout extent. |

The external offline engines carry the same boundary in their own terms. A
`runout.avaframe_com1dfa` result tests the depth-averaged dense-flow solver under
the supplied Voellmy parameters; a `runout.avaframe_flowpy` result tests
angle-of-reach routing and its spreading rule, and its energy-line height is not a
flow depth or a simulated velocity. A comparison between the two tests neither: it
measures how far apart two uncalibrated models are on the same input.

Accordingly, the real-event extent comparisons below test the **empirical alpha
angle plus fast routing**, not Voellmy dynamics. The separate GEODAR comparison
tests velocity and along-thalweg travel of the point-particle dynamics, but it is
ineligible for the strict holdout and fails its own frozen criteria. See
[validation-scope.md](validation-scope.md) for the durable metric contract.

## Phase 1: software verification, not field validation

Seven synthetic tests pass with `scientific_use="software_verification"`. These
tests establish that the implemented equations and invariants behave as stated
on controlled terrain; they do not establish agreement with avalanches in the
field. The values below are from
[`tests/test_physics_verification.py`](../tests/test_physics_verification.py).

| Component tested | Mode | Analytic or invariant target | Observed result | Predeclared acceptance | Result |
|---|---|---|---|---|---|
| Coulomb stopping distance in the particle integrator | `dynamics_only` | 96.225045 m | 96.500000 m; absolute error 0.274955 m | absolute error ≤ 0.50 m (two 0.25 m cells) | Pass |
| Voellmy terminal velocity | `dynamics_only` | 4.958302295 m/s | 4.958302021 m/s; relative error 5.53 × 10⁻⁸ | relative error ≤ 0.5% | Pass |
| Specific-energy dissipation and flat-ground kinetic energy | `dynamics_only` | no positive stepwise energy change | maximum total-specific-energy change −0.035201267 J/kg; maximum flat-ground kinetic-energy change −0.035201267 J/kg | every checked change ≤ 0 J/kg | Pass |
| Directional surface-to-map displacement projection | `dynamics_only` | projection factors on a 30° plane: 0.866025 fall-line, 0.925820 at 45° oblique, 1.000000 along contour | matched all three analytic directional-grade factors within 1 × 10⁻¹² | absolute error ≤ 1 × 10⁻¹² | Pass |
| Endpoint grid convergence | `dynamics_only` | analytic endpoint 769.800359 m and first-order spatial convergence | endpoints 785.6, 777.7, and 773.7 m at 20, 10, and 5 m; errors 15.799641, 7.899641, and 3.899641 m; observed orders 1.000033 and 1.018446 | both orders in 0.8–1.2 and 5 m-grid error ≤ 5 m | Pass |
| Seeded particle replay | `hybrid` | identical seed and terrain produce identical results | reached, intensity, velocity, and uncertainty arrays were byte-identical; stopping points, metadata, and warnings were equal | bit-identical replay | Pass |
| Fast-routing flux conservation | `alpha_only` | initial flux 1.0; loss no greater than the exercised minimum-flux cutoff bound | terminal flux 0.998772006; deficit 0.001227994 (0.122799%); cutoff-loss bound 0.002 | deficit ≤ 0.002 + 10⁻⁶ | Pass |

The convergence result characterizes first-order grid error for this sharp
ramp-to-flat fixture. It is not permission to attach the same error to real,
irregular terrain. The energy result tests one monotone synthetic centreline,
and the flux result tests routing conservation only within the documented
cutoff; neither is a field-accuracy result.

## Phase 2: portable bake verification

**Component tested:** the offline, provider-neutral terrain-ingestion and bake
contract—not release scoring or runout physics.

The published Brämabühl 1 m DTM was also run end-to-end through a new
`MountainPack` using `elevation_primary` with the `single_raster` adapter. The
non-BC Swiss input baked to a 300 × 300, 10 m EPSG:2056 grid, wrote 34 terrain
tiles and eight runtime layers, and passed the bake compatibility check. A
second execution produced the same bake identity,
`4aa541789e5f2b7f4da46e6dec8b2051646ab9ac17811d9f0746c03a73d8fdc4`.
The source DTM and generated runtime are deliberately not committed; their
source checksums, pack, AOI, commands, layer hashes, and result are recorded in
[`validation-data/bake-verification/braemabuehl-2019/run-record.json`](../validation-data/bake-verification/braemabuehl-2019/run-record.json).

No land-cover raster was acquired. The bake therefore retained all 90,000
forest-cover cells as missing, with source code zero meaning missing; it did not
turn unknown forest cover into a safe-looking non-forest value. `lidar_fraction`
is `null` because the generic adapter does not invent acquisition provenance.
This satisfies the real non-BC portable-bake criterion only. It does not show
that an avalanche remains inside this 3 km × 3 km AOI, and it supplies no field
accuracy evidence for any runout component.

## Real-event evidence and eligibility

Eight events were registered in EPSG:2056 across real slopes in the greater
Davos region. The frozen experiment groups them under four analysis labels:
Brämabühl (2 events), Stillbergalp sector (2), Totalp sector (3), and
Schwarzseealp sector (1). Those are experiment grouping labels, not source
claims that the observations form four independent mountains. The source
registry retains the more specific published-coordinate localities:
Brämabühl, Witibärgweg, Jakobshorn, Dorftälliweg, Schwarzseealpstrasse, and
Parsennfurggawäg. This evidence must not be described as independent validation
on two mountain ranges or two distinct terrain regimes.

The Brämabühl source contributes Wildi and Rüchi on 15 January 2019. It provides
published release and dense-deposit polygons interpreted from drone imagery
acquired one day later, an approximately 60 cm regional new-snow report, and an
artificial-helicopter-control trigger. It does not provide a complete surveyed
negative domain or quantified horizontal positional uncertainty. Source:
[Glaus, *Brämabühl Avalanche Jan 2019 Dataset*](https://doi.org/10.5281/zenodo.15796703).

The satellite source contributes six visible `SLAB` footprints from the bounded
13–16 January 2019 period. Each is a whole-avalanche outline from start through
deposit, not a separate release or deposit polygon. The experiment therefore
uses a model-independent rule fixed in advance to derive a release proxy: cells
inside the footprint's upper 20% elevation band with slope at least 27°. The
source humidity is `UNKNOWN`, and the roughly 100 cm new-snow value describes
the regional 60-hour storm rather than event-specific release depth.

The accompanying study used more than 900 ground and helicopter photographs to
check avalanche occurrence, but explicitly states that those non-orthorectified
photos could not validate outline accuracy. Photo coverage was 74% in 2019, and
validation points were not sampled where neither mapping method detected an
avalanche. Consequently the coverage polygon is not a cellwise surveyed domain
with known absence. The reported sub-2 m image-control RMSE characterizes SPOT
image localization, not interpreter boundary or deposit-endpoint uncertainty.
The DAvalMap polygons are described as approximate and incomplete. None of
these fields supplies quantified outline uncertainty or repairs the missing
negative domain. Sources: [Hafner et al., *The Cryosphere* 15, 983–1004
(2021)](https://tc.copernicus.org/articles/15/983/2021/) and the
[EnviDat data record](https://www.envidat.ch/metadata/satellite-avalanche-mapping-validation).

Both registries are therefore `scientific_use="qualitative_comparison"`. They
remain outside the trusted field-validation identity registry. This is an
evidence limitation, not a failed schema check: preserving the positive
geometries and their lineage does not make them strict ground truth.

## Candidate eligibility table and public-source audit

This audit was extended on 13 August 2026 **before any new model prediction,
parameter fitting, MountainPack, or bake was created**. Its purpose was to find
8–15 events on at least two geographically independent mountains. A source is
eligible only when the downloadable public payload supports every required
event field: release geometry; deposit geometry and/or a measured endpoint;
exact event date/time and confidence; complete surveyed-domain/known-absence
semantics; quantified positional uncertainty with method, confidence, survey
date and instrument/calibration information; release size/depth, new snow, wind
speed and meteorological wind-from direction with source and uncertainty;
licence, stable URL, hashes, CRS and complete lineage; and an event-day DEM or a
quantitative bound on terrain/snow-surface epoch error.

The 13 August extension also inspected three newly published authoritative
packages that were not in the earlier audit: BFW/OpenNHM AvaFrameData v1.0, the
SLF Vallée de la Sionne avalanche 20243024 dynamics archive, and NGI's 2024/25
Ryggfonn technical note. They are closer to the contract than an inventory or
paper figure, but none passes it. The candidate table was updated with those
failures before any prediction was created or viewed for this extension.

No reviewed public source passed that gate. `Partial` below means that related
information exists but does not satisfy the strict field definition; it must
not be interpreted as half-credit or imputed data.

| Candidate public payload | Release geometry | Deposit / endpoint | Event date/time + confidence | Surveyed domain + known absence | Positional uncertainty, method + confidence | Scenario inputs | DEM / surface error | Licence, URL, hash, CRS + lineage | Eligible independent-mountain cohort |
|---|---|---|---|---|---|---|---|---|---|
| SLF Vallée de la Sionne GEODAR v1 | Partial | Partial | Partial | No | No | Partial | No | Partial | No: one mountain and failed event fields |
| SLF Vallée de la Sionne avalanche 20243024 archive | No | No | Partial | No | No | Partial | No | Partial | No: one event on one mountain and failed geometry/coverage fields |
| BFW/OpenNHM AvaFrameData v1.0 | Partial | Partial | Partial | No | No | Partial | No | Partial | No: six events, below the cohort minimum, and every event fails required fields |
| SLF Hafner 2023 reliability archive, greater Davos | Partial | Partial | No | No | Partial | No | No | Partial | No: one region and failed event fields |
| EnviDat satellite avalanche mapping validation v1.0, greater Davos | Partial | Partial | Partial | Partial | No | No | No | Partial | No: selected detection-validation locations, not an exhaustive known-absence domain |
| EnviDat 1999 Swiss Alps avalanche outlines v1.0 | Partial | Partial | No | Partial | No | No | No | Partial | No: many mountains, but acquisition-period whole-avalanche outlines rather than eligible dated events |
| Lim et al. Davos UAS campaign archive | No | No | No | No | No | No | No | Partial | No: raw mapping-flight data at one site, not avalanche observations |
| NGI Ryggfonn 2019/20, 2023/24 and 2024/25 technical notes | Partial | Partial | Partial | No | No | Partial | No | Partial | No: one mountain and report-only payloads |
| Nordkette Seilbahnrinne 22 February 2022 supplement | Partial | No | Partial | No | Partial | Partial | No | Partial | No: one event on one mountain |
| NVE RegObs API v5 | Partial | Partial | Partial | No | No | Partial | No | Partial | No: failed event fields despite many mountains |
| Parks Canada Rogers Pass public material | No | No | No | No | No | No | No | No | No: no public event package |
| CAIC November 2024 accident workbook and reports | No | No | Partial | No | No | No | No | Partial | No: failed event fields despite many mountains |
| French EPA/CLPA-derived ELA/AUP v1 | No | No | No | No | No | No | No | Partial | No: path envelopes, not dated event geometry |
| Existing Brämabühl and greater-Davos observations | Partial | Partial | Partial | No | No | Partial | No | Partial | No: qualitative evidence only |

The following records the inspected files and exact downgrade reasons. A paper
saying that measurements exist is not equivalent to a downloadable, licensed,
event-linked validation package.

| Candidate | Inspected authoritative payload and lineage | Strict disposition and exact missing fields |
|---|---|---|
| [SLF Vallée de la Sionne GEODAR v1](https://doi.org/10.5281/zenodo.1042108), Switzerland | The CC BY 4.0 repository contains 77 timestamped radar events, processed front trajectories, thalwegs, runout/stopping information, and velocity-sensitive observations. The 10,161-byte `data_table.csv` has repository MD5 `0e262b7b66de5bd3752f02b6eeb8922b` and inspected SHA-256 `31300be21792b8975efc5e18880fb28419bc970439a77459868f619aba95111b`. The associated paper says release location was estimated from photographs, FMCW radar, seismic data and GEODAR, while meteorological/snowpack supporting data are available only on request. | **Ineligible for strict extent or endpoint validation as published.** Release is a named or representative location and thalweg, not an event release polygon with boundary uncertainty. The moving-radar observation range is not a complete surveyed deposit/endpoint domain with known-absence semantics. The payload has no event-linked deposit polygon, positional confidence statement, complete scenario, or event-day snow-surface DEM/error bound. The trajectories and velocities remain a strong `dynamics_only` lead if the withheld geometry, coverage, uncertainty, scenario and terrain contracts are supplied. |
| [SLF Vallée de la Sionne avalanche 20243024 archive](https://doi.org/10.5281/zenodo.17104410), Switzerland | The Zenodo record, published 29 September 2025 and updated 8 July 2026, exposes 14 files totalling 1,881,446,257 bytes: processed optical velocity, pressure, GEODAR images, high-speed-camera products, videos, a MATLAB workspace/script and `readme.docx`. The API has no `metadata.version` value and its versions endpoint returns only this record, but the record does assign CC BY 4.0 to the deposit. The published paper fixes detection at 2023-12-02 05:19:39 and documents GEODAR, optical sensors and three pylon cameras. The paper says acquisition is automatically triggered and the recordings are synchronized, while the repository README separately warns that precise time/space synchronization must be defined before broader use; the residual alignment error is not quantified in the package. Zenodo supplies an MD5 for every file. The README also leaves sensor position relative to the changing snow surface, deposit/detachment effects, signal stability and measurement validity to be established. The payload has range/time and height/time coordinates, not event GIS or a declared map CRS. | **Ineligible for strict extent, endpoint or dynamics validation as published.** This is one event at one mountain. There is no release polygon, deposit polygon, terminal endpoint, survey-coverage polygon or known-absence statement. No boundary/endpoint uncertainty exists because those geometries are absent. Dynamics measurements are a strong partial lead, but the public archive lacks a complete calibration/uncertainty record and leaves residual synchronization and sensor-to-sliding-surface position unresolved. It also lacks release size/depth, new snow, wind speed and meteorological wind-from direction with uncertainty, a pre-event snow-surface/error bound, original map coordinates/CRS and full raw-radar lineage. The public licence is no longer a blocker for the 14 deposited files, but it does not repair the missing scientific fields or establish permission for additional non-deposited source files. |
| [BFW/OpenNHM AvaFrameData v1.0](https://doi.org/10.5281/zenodo.20701552), Austria and Switzerland | The 13,416,107-byte Zenodo archive (repository MD5 `8dbef03caa20ff30213561f4c22b1d60`; inspected SHA-256 `487f18310a33291588b65b50b775df79d7c917b5ec1bf4006f0544e7ee01bef0`) is bound to release `1.0`, Git commit `fa839ca4dab403f14f7c49596e8e3ccb16f0b3e8` and Software Heritage directory `swh:1:dir:ab5478eacc66e935f0e36b2d56bdc875860b546e`. Its bundled licence is CC BY 4.0. It contains six events: Arzl, Eiskar, Filisur 1/2, Kleiner Oetscherbach and Popeletzbach. Release layers exist for all six; Eiskar, Filisur 1/2 and Popeletzbach have deposit layers, while Kleiner Oetscherbach has only a whole-event area and Arzl only a textual runout statement. Inspected CRS records are EPSG:31254 for Arzl, EPSG:31287 for Eiskar/Kleiner Oetscherbach/Popeletzbach, and EPSG:2056 for Filisur. EPSG:31254/31287 declare northing/easting CRS axes but the stored GIS data map X/Y to axis order `2,1`; EPSG:2056 stores easting/northing as `1,2`. Filisur was transformed from EPSG:21781 to EPSG:2056, but the exact coordinate operation is not recorded. | **All six events are ineligible.** No event has a scored survey-coverage polygon with explicit known-absence semantics or quantified release/deposit boundary uncertainty, confidence, survey date and complete instrument/calibration record. Arzl has no exact event date or deposit GIS; Kleiner Oetscherbach lacks a distinct deposit; the Filisur release-depth attributes are null and the documented 1 m value was an assumed modeling input; the Popeletzbach `ci95`, release-thickness and deposit-thickness/volume fields are null. Eiskar is the closest individual event, with drone/laser-derived release thickness and several deposit outlines, but its report calls footprints/volumes estimates and supplies no boundary confidence, complete event scenario, bundled pre-event surface or quantitative surface-error bound. No event has complete new-snow and wind inputs with uncertainty, and the six-event package is below the required cohort size even before field failures. |
| [SLF Hafner 2023 reliability archive](https://doi.org/10.16904/envidat.423), greater Davos | The actual 1,135,023,988-byte archive was inspected at SHA-256 `e947d0ca0a9db37eb4892161b0403f17bf2a2e604b4bff2a7cf109eabfa1cc80`. Study 2 contains 60 participant whole-avalanche polygons (`Outlines_from_photo.shp`, SHA-256 `c79f876208f1ce19b2f783fe2d9d98d2074d434bf182179fd302fabb5dca7b52`) and six reference polygons (`Reference.shp`, SHA-256 `afb98b2b1fd696b34b96c0d507ce2c484a21446a5d6a7cb5d56a42dc12cea9c2`), all EPSG:2056. Study 3 has two image-area polygons (`AOI_study3.shp`, SHA-256 `c83d841da89f950b66f84bb2c179a4e9aea06ed7dd01aced0c72baae37893388`), orthophotos at several resolutions, five interpreter mappings and shade masks. | **Ineligible and not an upgrade of the existing Davos observations.** Study 2 gives six whole-avalanche interpretation outlines, not separate release and deposit geometries; it identifies winter 2020/21 but not an exact date for each event, and supplies no surveyed known-absence domain. Study 3 dates are image-acquisition dates, not event dates, and its AOI/shade masks do not declare cellwise absence. Mapper disagreement is an aggregate reliability experiment, not event-specific boundary uncertainty with method and confidence. Neither study supplies scenario inputs, an event-day surface/error treatment, or a second independent mountain. The archive requires citation, but no separate data-package reuse licence was found that could be substituted by the article's CC BY 4.0 licence. |
| [EnviDat satellite avalanche mapping validation v1.0](https://doi.org/10.16904/envidat.202), greater Davos | The authoritative ODbL/DbCL record was modified 31 July 2025. Its 5,673,425-byte download was inspected at SHA-256 `5529482a35823f4b3f2d870df7da52e2c73af151f6eb9c12d3869353379368bb`; the nested 4,116,616-byte data ZIP has SHA-256 `8b91b9f9fcc3d1d2321b705fac1b98e4f1f4f1beb462b0eb7296bc4696610c4f`. It contains EPSG:2056 SPOT outlines (368 in 2018, 118 in 2019), DAvalMap outlines, Sentinel detections, validation points (536/197) and one photo-coverage polygon per year. The dictionary defines validation code `0` as no avalanche, but no delivered row has value `0`; actual `Aval_vali` counts also contain undocumented value `3` (39/18 rows). | **Ineligible for strict release/runout validation.** Coverage records where ground/helicopter photographs existed, not exhaustive event-target inspection with known absence. The study reports SPOT probability of detection 0.74 and explicitly says the non-orthorectified photographs could confirm occurrence but not outline accuracy. Its sub-2 m image-registration RMSE is not avalanche-boundary uncertainty, and `frac_wdh_a` is fracture-width accuracy rather than polygon-boundary uncertainty. The shapes are whole-avalanche outlines and dates are multi-day or day-level windows rather than exact event times. This package can support a labelled detection-method or positive-only terrain-consistency benchmark, not precision/recall for this model or a strict field-accuracy claim. |
| [EnviDat 1999 Swiss Alps avalanche outlines v1.0](https://doi.org/10.16904/envidat.579) | The CC BY-SA record was modified 22 August 2025. Its 150,084,348-byte archive was inspected at SHA-256 `7a456616f8dfd01c39c8a7b945abf9ebe46436f084b4c13a7f09f2651fd64427`; the nested 148,108,558-byte ZIP has SHA-256 `43b1203e812bfb69a448944fcd0ca2e75763d0052ee78fc41306f9b44bc98c02`. It supplies 11,120 EPSG:2056 whole-avalanche polygons plus 66 image-coverage polygons and 48 cloud polygons. Quality labels are 997 exact, 8,719 estimated and 1,404 created. The only populated dates are aerial-image acquisition dates from 25 February to 1 March 1999; event-date fields are null. | **Ineligible for strict event validation despite its geographic breadth.** The package does not separate release and deposit, identify event release date/time, assert exhaustive avalanche detection within image coverage, encode known absence, quantify outline-boundary uncertainty, provide event-specific release/weather inputs, or supply a pre-event snow surface/error bound. It is potentially useful as a multi-mountain positive-only terrain-susceptibility consistency set with the acquisition/quality limitations preserved. |
| [Lim et al. Davos UAS archive](https://doi.org/10.5281/zenodo.18198188) | The CC BY 4.0 record exposes one 4,711,778,174-byte `NHESS-Data.zip` with repository MD5 `d0bcf66adc5e3a0e2d34b60b4b5f535b`. A complete ZIP64 central-directory inspection enumerated 224 entries: raw `.ulg` flight logs, ROS `.bag` files and geotagged JPG images under `CoverageMapping` and `ActiveMapping`. It contains no shapefile, GeoPackage, GeoJSON, avalanche outline, event observation or declared validation-coverage product. Record metadata says collection on 25 April 2025, while archive paths say `2024-04-25`, an unresolved lineage-date conflict. | **Ineligible and not an avalanche-event cohort.** This is a terrain-mapping-methods flight archive at one site. It supplies none of the required event release/deposit geometry, timing, known absence, boundary uncertainty, scenario, event-day surface relation or cohort membership. It may inform a future survey design, but does not close any current event-level requirement. |
| [NGI Ryggfonn 2019/2020 note](https://www.ngi.no/globalassets/dokumenter/prosjekter/snoskredforskning/2020/20200017-05-tn_rgf2019-2020.pdf), [2023/2024 note](https://hdl.handle.net/11250/3213805) and [2024/2025 note](https://hdl.handle.net/11250/5529207), Norway | The 2019/20 note lists 13 timestamps, seven numeric inter-sensor velocities and three brief field-survey deposit maps. The NVA record for 2023/24 still exposes only one 15,504,106-byte PDF, inspected SHA-256 `da38923da32aae3332c1063b59231765ee24bdfd7b3a42de024ef5a024d0dabf`, under `COPYRIGHT-ACT` general terms. The new 2024/25 NVA record was published 16 June 2026 and exposes only `20230100-05-TN.pdf`, artifact `2ff1b17b-174d-4480-b09b-189401ab51cf`, 33,563,918 bytes, repository MD5 `2b09ea3b1a619c62668617a907bf24d4`, inspected SHA-256 `693e10e678a32762eec4e65c0ad3706430e5fa2166bf0bc56dc998b6bac12a69`, again under `COPYRIGHT-ACT`. It lists 15 natural avalanches with exact CET timestamps and selected radar/instrument velocities. Its multi-event map labels rough estimated release, deposition or impacted areas derived from photographs; it includes limited laser-snow-depth evidence for some release periods, not separable source geometry for every event. Field investigations were limited by weather. NGI and radar clocks are not synchronized and radar time is generally UTC. Sensor positions are described as approximate WGS 84 / UTM zone 32N, while the review page instead prints zone 33 with an impossible conventional UTM easting of 954190; the record/body title says 2024/25 but the review-page document title says 2023/24. | **Ineligible as public field-validation data.** The PDFs provide figures, not source GIS, laser products or sensor files. They do not provide event release polygons, machine-readable deposit polygons/endpoints, a complete surveyed domain with known absence, boundary/endpoint uncertainty and confidence, complete per-event new-snow/wind inputs, or event-day snow-surface/DEM error bounds. Numeric velocity observations are incomplete and lack a packaged synchronization/calibration uncertainty contract. The public records do not supply reusable data rights, file-level CRS metadata and hashes for the underlying observations, or their transformations; the 2024/25 report also contains unresolved time and CRS inconsistencies. The sketches cannot be digitized into invented validation observations. The 22 events across 2023/24 and 2024/25 make Ryggfonn the strongest independent-mountain acquisition partner, but event eligibility remains zero until original packages pass review. |
| [Nordkette 22 February 2022 experiment and supplement](https://doi.org/10.5194/nhess-25-4185-2025), Austria | The actual 3,064,624-byte supplement was inspected at SHA-256 `7680a6b86e8bfe1df852b6a3951e812e15cd5017ae23ec750a6dfa6dcf39ea67`. It contains one release polygon (`release_epsg31254.shp`, SHA-256 `a29b2f95fcf4bf41ce2636d5b10c28f4f85ebb62ea4731251eb842428ba43b95`) in MGI / Austria GK West (EPSG:31254), a 5 m simulation DEM (`Nordkette.asc`, SHA-256 `bed9573974c5f83e6661cce00251c74bb1d358dc0950fcb9ff5520e4e2a6768b`), path/split geometries and best-fit simulation configurations. The paper reports three GNSS AvaNodes with 2.5 m position standard deviation, 0.05 m/s velocity standard deviation and radar-front uncertainty of about 1–2 m. | **Ineligible.** This is one event on one mountain. The supplement contains simulation inputs and tuned outputs, not the raw radar/AvaNode observation payload. The documented radar field of view did not capture the lower full runout, and there is no measured deposit polygon/terminal endpoint or surveyed known-absence coverage. Sensor trajectory uncertainty does not quantify the release/deposit boundary. Release thickness is a configured model input, not a surveyed release-depth observation. Complete wind scenario, event-day DEM/snow-surface error, and observed-geometry derivation lineage are absent. |
| [NVE RegObs API v5](https://api.regobs.no/v5/swagger/ui/index), Norway | The schema can carry `StartExtent`, `StopExtent`, whole-avalanche `Extent`, start/stop points, event time, crown dimensions, trigger and observation-location uncertainty. Data are under NLOD, but [NVE states that observations are supplied “as is” and may contain errors and omissions](https://www.varsom.no/en/about/regobs/regobs-about-data-terms-of-service-and-privacy-policy/). A bounded `POST /v5/Search` audit used `GeoHazard=10`, registration type 81 subtype 26, competence IDs 120/130/150 (`***`, `****`, forecaster `*****`), `NumberOfRecords=2000`, `Offset=0`, descending observation time and end time `2026-08-13T23:59:59Z`. It returned 34 registrations with both start and stop polygons, 51 with start plus a stop polygon or coordinate, only one of those 51 with non-null `ObsLocation.Uncertainty`, and nine with event-linked wind speed/direction. The 19,067,782-byte response SHA-256 was `62186a15c1ae31517b286121f4e60789d47d59d8b9651d11477d8679e9315220`; it is a mutable retrieval-audit hash, not a trusted identity. The closest inspected record, [442574](https://api.regobs.no/v5/Registration/442574/2), has start and stop polygons and `Uncertainty=5`, but the uncertainty describes the smartphone observation location, not either polygon boundary; the event interval and trigger are uncertain, and weather, snow-surface, snow-cover and profile objects are null. | **Ineligible.** No event-level surveyed coverage polygon or known-absence declaration is exposed. Observation-location uncertainty does not characterize drawn release/deposit boundaries or endpoints, and no boundary method/confidence is supplied. Even the closest high-competence record lacks a complete event date/time, scenario, event-day surface and DEM-error treatment. Multiple mountains do not repair these event-level omissions. At that audit stage no observation was registered or scored; the later executed 26-event public funnel above also yielded zero eligible observations. |
| [Parks Canada Rogers Pass avalanche-control programme](https://parks.canada.ca/pn-np/bc/glacier/visit/hiver-winter/ski) and [public terrain files](https://parks.canada.ca/pn-np/bc/glacier/visit/hiver-winter/ski/terrain-aval), British Columbia | Parks Canada describes daily weather, snowpack, snowfall and avalanche-path observations, fixed artillery targets and many avalanche paths. The downloadable public KML/GPX and PDF material inspected consists of Winter Permit System boundaries, generic major-runout zones and terrain traps. | **Blocked at access, not treated as public validation data.** No dated event-level payload was found. Operational permit and terrain-zone boundaries are not release/deposit observations and cannot establish a surveyed event-specific absence domain. The public files supply none of the required event geometry, uncertainty, scenario-to-event join, event-day surface error, data licence, event-file hashes or observation lineage. Rogers Pass paths would also require explicit independent-mountain identities; path sectors alone are not independent mountains. |
| [CAIC accident workbook and reports](https://avalanche.state.co.us/accidents/statistics-and-reporting), Colorado | The actual [November 2024 workbook](https://avalanche.state.co.us/sites/default/files/2025-02/CAIC_Accident_Data_Nov_2024.xlsx) was inspected: 147,365 bytes, SHA-256 `63b53ee38cb409ebe6fe11ce835f8beee8f220d0e7c5d360d0c88f6d529d2152`, 996 data records and only 14 fields: `AvyYear`, `YYYY`, `MM`, `DD`, `Location`, `Setting`, `State`, `lat`, `lon`, `PrimaryActivity`, `TravelMode`, `Killed`, `Description` and `Date`. Its metadata asks users to cite CAIC 2024. CAIC states that almost all fatal accidents are investigated but non-fatal incidents are underreported. Individual reports may include crown dimensions, weather and snowpack narrative. | **Ineligible.** The workbook has no release/deposit/endpoint geometry, surveyed domain, positional uncertainty, scenario measurements, DEM/surface, CRS declaration or geometry transformation lineage, and it does not state a reusable data licence. Report narratives are not a cohort-wide downloadable survey package. Underreporting precludes interpreting the incident collection as known absence. The [public report form](https://avalanche.state.co.us/observations/observation-report) may displace hidden locations by 2–7 miles, so public points cannot become validation geometry. |
| [French EPA/CLPA-derived ELA/AUP v1](https://doi.org/10.57745/3IAE59) and [public EPA](https://www.avalanches.fr/donnees-publiques-epa/) | EPA supplies event chronicles on monitored paths; CLPA supplies maximum known historical envelopes rather than event-specific deposits. The Etalab-2.0 derived file has 54,524 data rows and only `Department`, `Type`, `AUP` and `ELA`; repository MD5 is `d29097e90f55e77c0134a741b3a4c835` and inspected SHA-256 is `fa6f91ec5533caaa43de19eb1c9974144ea93b07485a57f67d8d636e75921472`. | **Ineligible.** The derived file has no event ID, date, coordinate, release polygon, deposit polygon, endpoint, surveyed domain, uncertainty, scenario or DEM lineage. EPA site maps and CLPA maximum envelopes cannot be relabelled as release/deposit observations for a dated event. |
| Existing Brämabühl and greater-Davos observations in this repository | The eight registered events described above remain useful positive-only evidence. | **No upgrade.** No reviewed source supplies their missing known-absence coverage, event-specific positional uncertainty, complete scenario and bounded event-day terrain error. Their scientific use and partitions remain unchanged. |

### 13 August 2026 authoritative-repository recheck

The repositories were rechecked at their APIs and the current downloadable
payloads were inspected rather than relying on search summaries:

- [GEODAR v1](https://doi.org/10.5281/zenodo.1042108) remains a single-record,
  CC BY 4.0 deposit last updated 2 August 2024: 382 files and 19,919,928,729
  bytes. No new version or event-geometry attachment is exposed. The inspected
  repository guide says trajectory files are picked and smoothed representations
  of a front or major surge, and the thalweg files are steepest-descent lines from
  a release area. It declares CH03/LV03 (EPSG:21781) for thalweg X/Y/Z, but the
  inspected HDF5 files carry no embedded root or dataset attributes, so the CRS
  depends on the external PDF. Processing metadata and a commit hash do not add
  release/deposit boundary uncertainty or a surveyed absence domain.
- [VdS 20243024](https://doi.org/10.5281/zenodo.17104410) remains one record with
  the same 14 files and byte/checksum identities, updated 8 July 2026. The
  authoritative API assigns CC BY 4.0; this corrects the earlier no-licence
  reading of the licence-free `readme.docx`. The associated article is now
  published as [Calic, Coletti and Sovilla (2026)](https://doi.org/10.1029/2025JF008912),
  but neither article nor deposit adds the required event GIS, surveyed absence
  domain, complete scenario, surface-mismatch bound or resolved dynamics
  calibration/uncertainty.
- [AvaFrameData v1.0](https://doi.org/10.5281/zenodo.20701552) remains the only
  Zenodo version and its one ZIP is byte-identical to the earlier inspection.
  Release tag `1.0` is still commit
  `fa839ca4dab403f14f7c49596e8e3ccb16f0b3e8`. Branch `main` at
  `6fe8951dd48d9a0afad81d07d4e5b53b2962949c` is two commits ahead but changes
  only the root README. Unreleased branch `addAuthors` at
  `12b20307d5201769c5f4fb205a78b62c02edd302` is five commits ahead and changes
  only `README.md` and `.zenodo.json`; it identifies acquisition/preparation
  contributors but adds no event payload. It identifies WLV acquisition for
  Arzl and Eiskar, Frank Perzl as the acquisition source for Kleiner
  Oetscherbach/Popeletzbach, and leaves the original Filisur authors unfinished.
- The original [Feistl et al. (2014) Filisur source article](https://doi.org/10.3189/2014JoG13J055)
  was rechecked at its publisher PDF: 5,752,876 bytes, SHA-256
  `8bd131c75ff84dc63b2e5a9df06f631103a93d679bfa60e46676d5855fa4b29e`.
  Its table dates both Filisur events only approximately (`~23 Feb. 2012`), marks
  deposit mapping as handheld differential-GNSS but not release mapping, and says
  the two probable release areas were inferred by 2 m terrain analysis with
  accuracy only at the several-metre scale. It describes differential-GNSS error
  generically as a few centimetres but supplies no event-specific confidence,
  surveyed coverage/known absence, calibration record, original GIS files, CRS,
  transform, per-file hash or complete scenario. The paper figures are not a
  substitute for the original SLF source records.
- The current authoritative [WLV avalanche-catchment collection](https://gis.lfrz.gv.at/api/geodata/i000901/ogc/features/v1/collections/i000901%3Awlv_ezg_la)
  exposes CC BY 4.0 raw data as
  [`WLV_EZG_LA_GPKG.zip`](https://inspire.lfrz.gv.at/000901/ds/WLV_EZG_LA_GPKG.zip).
  The inspected 9,399,382-byte ZIP has SHA-256
  `0f751b74605f097133f163c9101e3328284172ac1ca71ac1809d08d4f7b3b48c`
  and contains one 15,347,712-byte EPSG:31287 GeoPackage last changed 19 February
  2026 with 8,443 path polygons. It includes `Popeletzbach-Lawine` under current
  WLK-ID `406860.0` and `Kleine Ötscherbachlawine` under `431503.0`. These are
  generic catchment/path polygons, not dated event 6534/6591 source records,
  release/deposit observations, survey coverage or known absence. The changed
  identifiers do not establish the missing source-to-v1.0 lineage.
- The authoritative BFW presentation
  [`Hofburggespraech19_Adams_HBG.pdf`](https://www.bfw.gv.at/wp-content/uploads/Hofburggespraech19_Adams_HBG.pdf)
  is 10,341,283 bytes at inspected SHA-256
  `8d091c96c0c00aed80060ccbffe5f94ac06e6ec5807e9a8eff31f991a26b41ae`.
  It documents an Arzl deposition-focused UAS campaign on 17 January 2019 over
  0.5 km2, orthophoto GSD 0.04 m, surface-model GSD 0.16 m, and estimated deposit
  area 1.8 ha, mean height 3.1 m and volume 57,000 m3. It exposes slides, not the
  original imagery, point cloud, surface model, deposit geometry, coverage
  semantics, boundary uncertainty, CRS/transformations, calibration or file-level
  lineage. It therefore sharpens the BFW/WLV acquisition request but does not make
  Arzl eligible. No reusable data licence is stated in the slide payload.
- The actual EnviDat validation payload, rather than its search metadata, confirms
  that the apparent `GroundTruthCoverage` lead does not supply known absence. The
  data dictionary defines a false/no-avalanche value, but neither validation-point
  layer contains one. The published 0.74 SPOT probability of detection also
  demonstrates missing avalanches inside the study, while the authors state that
  their photographs could not validate mapped outline accuracy.
- The separate 1999 Swiss Alps archive materially expands geographic breadth to
  11,120 mapped outlines, but not eligible event membership: event dates are null,
  image-acquisition dates span several days, outlines are not split into release
  and deposit, and neither exhaustive detection nor boundary uncertainty is
  declared. Its coverage and cloud layers support careful positive-only analysis,
  not negative labels.
- The 4.7 GB Lim et al. UAS download was closed without transferring the entire
  archive by enumerating its remote ZIP64 central directory. All 224 entries are
  mapping-flight logs, ROS bags or geotagged photographs; no avalanche-event GIS
  or validation layer is present. This removes it as a near-term cohort lead.

The following matrix applies all twelve strict requirements to the rechecked
paths. `Partial` means that related information exists but the requirement is
not satisfied.

| # | Strict requirement | Ryggfonn 2024/25 | GEODAR v1 | VdS 20243024 | AvaFrameData v1.0 plus public BFW/WLV sources |
|---:|---|---|---|---|---|
| 1 | Original release geometry, method and boundary uncertainty | Partial: photo/laser evidence, figure only; no boundary uncertainty | Partial: named/representative release and thalweg; no polygon uncertainty | No release GIS | Partial: polygons exist; no boundary uncertainty; some methods/values estimated |
| 2 | Original deposit geometry and/or measured endpoint | Partial: rough figure, no source GIS | Partial: processed radar stop/trajectory, not a surveyed terminal or deposit | No | Partial: missing for Arzl and distinct Kleiner Oetscherbach deposit; other boundaries lack strict qualification |
| 3 | Exact event date/time and confidence | Partial: exact CET timestamps, no confidence; unsynchronized UTC radar | Partial: timestamps, no stated confidence | Partial: exact detection time, no complete timing uncertainty | Partial: exact time absent or inconsistent for most events |
| 4 | Surveyed coverage for every scored target | No | No | No | No; imagery footprints are not declared target coverage |
| 5 | Explicit known absence within coverage | No | No | No | No |
| 6 | Horizontal uncertainty, confidence, method, survey date, instruments and calibration | Partial method/instruments for some events; no boundary uncertainty/confidence and incomplete calibration | Partial processing method; no boundary/endpoint uncertainty contract | Partial sensor descriptions; no geometry and unresolved space/time/surface alignment | Partial methods/dates for some events; no boundary confidence and incomplete calibration |
| 7 | Release size/depth, new snow, wind speed and meteorological wind-from direction, with source and uncertainty | Partial and not complete per event | Partial table fields; no complete event wind/scenario uncertainty | Partial temperature narrative; required release/snow/wind package absent | Partial event-dependent values; no event has the complete uncertainty-bearing scenario |
| 8 | Pre-event/event-day snow surface or quantitative mismatch bound | No | No | No | No; Eiskar has post-event laser evidence and Arzl/Eiskar products are not packaged with a mismatch bound |
| 9 | Licence or written reuse permission | No for underlying observations; NVA PDF has `COPYRIGHT-ACT` terms only | Pass for deposited files: CC BY 4.0 | Pass for 14 deposited files: CC BY 4.0 | Pass for v1.0 and WLV path layer: CC BY 4.0; original-source rights remain unestablished |
| 10 | Stable version/URL, original CRS/datum/axis, transformations, per-file SHA-256 and complete lineage | Partial: stable report/hash, contradictory CRS and no source-file lineage | Partial: stable DOI and CH03/LV03 in external guide, no embedded HDF5 CRS or complete observation lineage | Partial: stable DOI/checksums, no version value/map CRS/full raw lineage | Partial: immutable v1.0/hash/CRS, but missing exact transformations, source record versions and original-to-normalized derivations |
| 11 | Calibrated raw dynamics observations for a dynamics claim | Partial: selected velocities, unsynchronized clocks and no complete calibration uncertainty | Partial: processed/picked/smoothed trajectories, not a complete raw calibrated uncertainty package | Partial: strong observations, but raw-radar lineage, synchronization, calibration, valid intervals and sensor-to-surface uncertainty remain incomplete | No eligible raw dynamics package; simulation output is not observation evidence |
| 12 | Enough eligible events for 8-15 events across at least two independent mountains | No: 15 new plus seven prior events, but one mountain and all fail event fields | No: 77 events at one mountain and failed event fields | No: one event | No: six events and every event fails at least one mandatory field |

No rechecked source passes every event-level requirement. Registration,
prediction and independent eligibility/lineage review are therefore not
triggered.

### Additional 13 August 2026 payload identities

The following are the newly downloaded files whose byte length and SHA-256 were
verified locally. Archive-relative AvaFrameData paths refer to the immutable
v1.0 Zenodo ZIP above. These hashes document candidate inspection only; they are
not trusted dataset identities.

| Candidate | Exact downloaded file | Bytes | Inspected SHA-256 | CRS / lineage note |
|---|---|---:|---|---|
| NGI Ryggfonn 2024/25 | [`20230100-05-TN.pdf`](https://hdl.handle.net/11250/5529207) | 33,563,918 | `693e10e678a32762eec4e65c0ad3706430e5fa2166bf0bc56dc998b6bac12a69` | NVA artifact `2ff1b17b-174d-4480-b09b-189401ab51cf`; `COPYRIGHT-ACT`; report-only source with conflicting zone/title metadata. |
| GEODAR v1 | [`geodar_repository.pdf`](https://zenodo.org/api/records/1042108/files/geodar_repository.pdf/content) | 234,003 | `1d6ba10c1eb34c5d23885aacbc95178832e61ad1d04c88c5b5fa85a368665724` | External format/CRS guide; CH03/LV03 (EPSG:21781) for thalweg X/Y/Z. |
| GEODAR v1 | [`GEODAR-2010-12-06-07-46-21-THALWEG-001.h5`](https://zenodo.org/api/records/1042108/files/GEODAR-2010-12-06-07-46-21-THALWEG-001.h5/content) | 108,168 | `abbbcee519dc2cb0c06d7a5b89c01e60f5259fe98f891fe57a6551a25195b4f8` | Sample thalweg HDF5; coordinates depend on the external guide and carry no embedded attributes. |
| GEODAR v1 | [`GEODAR-2010-12-06-07-46-21-TRAJ-001.h5`](https://zenodo.org/api/records/1042108/files/GEODAR-2010-12-06-07-46-21-TRAJ-001.h5/content) | 158,208 | `409a019757d2ecbc8e098a1f4fcce5172c41229df4a68ddcd7ffb8fe33d978f7` | Sample picked/smoothed trajectory HDF5; no embedded attributes or observation uncertainty. |
| AvaFrameData / Filisur source | [`Feistl et al. (2014)`](https://doi.org/10.3189/2014JoG13J055) | 5,752,876 | `8bd131c75ff84dc63b2e5a9df06f631103a93d679bfa60e46676d5855fa4b29e` | Publisher PDF; approximate Filisur dates, differential-GNSS deposits and terrain-inferred release areas; no original GIS payload. |
| WLV public path layer | [`WLV_EZG_LA_GPKG.zip`](https://inspire.lfrz.gv.at/000901/ds/WLV_EZG_LA_GPKG.zip) | 9,399,382 | `0f751b74605f097133f163c9101e3328284172ac1ca71ac1809d08d4f7b3b48c` | CC BY 4.0; one EPSG:31287 catchment/path layer, not original event 6534/6591 observations. |
| BFW Arzl source lead | [`Hofburggespraech19_Adams_HBG.pdf`](https://www.bfw.gv.at/wp-content/uploads/Hofburggespraech19_Adams_HBG.pdf) | 10,341,283 | `8d091c96c0c00aed80060ccbffe5f94ac06e6ec5807e9a8eff31f991a26b41ae` | Public presentation with no stated reusable data licence; documents a 17 January 2019 UAS campaign but exposes no original survey files. |
| EnviDat Davos validation v1.0 | [`davos_satellitemappingevaluation.zip`](https://www.envidat.ch/dataset/3b40a59d-3e47-4c7f-968c-9852c0c3c55c/resource/93b0b4c1-0201-4734-b0e0-410af7c5cbbd/download/davos_satellitemappingevaluation.zip) | 5,673,425 | `5529482a35823f4b3f2d870df7da52e2c73af151f6eb9c12d3869353379368bb` | ODbL/DbCL record v1.0; nested data and EPSG:2056 GIS, but no delivered known-absence point or outline uncertainty. |
| EnviDat Davos validation v1.0 | `DataDescription_EvalSatMappingMethods.pdf` | 1,598,430 | `07f7251ae397a56de156ac8460989986a399bfebf55652825c61d0827d7a24b0` | Data dictionary; defines absent value `0`, which is unused in the delivered validation-point attributes. |
| EnviDat Davos validation v1.0 | `data/GroundTruthCoverage_2018.shp` | 6,040 | `77310f3d24dbb02e91539c0bb69e99b31b8bbf2d66c2f6549d2dbbdb204090eb` | EPSG:2056 photo-coverage polygon; not declared exhaustive known-absence coverage. |
| EnviDat Davos validation v1.0 | `data/GroundTruthCoverage_2019.shp` | 11,136 | `ce686a9661b65b1a702fb40226130f99741f7d51592e1aaef215f1fbcfe95d63` | EPSG:2056 photo-coverage polygon; not declared exhaustive known-absence coverage. |
| EnviDat Davos validation v1.0 | `data/SPOT_2018_perimeter.shp` | 4,033,956 | `c9b3f2976ee5e3e72b2a8a407c8c3146816f39efd08729a5ad18e110091bf9f3` | EPSG:2056; 368 whole-avalanche polygons. |
| EnviDat Davos validation v1.0 | `data/SPOT_2019_perimeter.shp` | 2,232,380 | `e23dac9036dbf30e26a15de4982b34acebd838aa717835def753e622c60ca937` | EPSG:2056; 118 whole-avalanche polygons. |
| EnviDat Swiss Alps 1999 v1.0 | [`avalanche_data_1999_all.zip`](https://www.envidat.ch/dataset/ac52bb46-c042-429b-83e2-feb5f397db99/resource/a86944ff-6bb2-4b9d-b7d2-26b4571be620/download/avalanche_data_1999_all.zip) | 150,084,348 | `7a456616f8dfd01c39c8a7b945abf9ebe46436f084b4c13a7f09f2651fd64427` | CC BY-SA record v1.0; contains nested GIS ZIP and mapping keys. |
| EnviDat Swiss Alps 1999 v1.0 | `data/avalanches1999_endversion1.shp` | 204,860,304 | `eaf3a4478f6b9990d9bb4f89fac1bbfd32968e512462f484aded2b9a2c8b82e6` | EPSG:2056; 11,120 whole-avalanche polygons with null event dates. |
| EnviDat Swiss Alps 1999 v1.0 | `data/area_images_1999_all.shp` | 32,718,008 | `ec09a902b4239b5f8275315b3f561c3a065b3ffe08480a4a128c25a720a7ae07` | EPSG:2056; 66 image-coverage polygons, not known-absence surveys. |
| EnviDat Swiss Alps 1999 v1.0 | `data/Clouds_1999.shp` | 7,158,800 | `53ecca6ed199d83b85b7d1a2c7496f4dcbda1d30a13b2e31c5a9ca8aca3e9075` | EPSG:2056 Polygon Z; 48 cloud masks. |
| AvaFrameData v1.0 | [`OpenNHM/AvaFrameData-1.0.zip`](https://zenodo.org/api/records/20701552/files/OpenNHM/AvaFrameData-1.0.zip/content) | 13,416,107 | `487f18310a33291588b65b50b775df79d7c917b5ec1bf4006f0544e7ee01bef0` | Immutable release archive; CC BY 4.0 bundled at archive root. |
| AvaFrameData v1.0 | `avaArzl/releaseWLV.shp` | 492 | `c5d38e354350317666f6597a83e0c188b1893ca98e3142b721d79f32e70abe6e` | EPSG:31254; one release polygon. |
| AvaFrameData v1.0 | `avaEiskar/TB_Eiskarlawine_Jan2019.pdf` | 12,480,326 | `1f425a28f6e56b837c9294dbe6f13cf48324bc6728190f68b0fd3814d9d624b6` | WLV technical report; describes estimates and source methods, not survey uncertainty. |
| AvaFrameData v1.0 | `avaEiskar/releaseEvent20190115.gpkg` | 98,304 | `2f533d3d33126572c3af63c4feb5ae5a88a90d6db690c51c748eadea39432ee1` | EPSG:31287; two 3D release polygons, mapped 18 January 2019. |
| AvaFrameData v1.0 | `avaEiskar/depositionMaxOutlineEvent20190115.gpkg` | 110,592 | `44ed2102f440a1c0d35a867991da5c7f7ae71ac701674392ebf14add97f40d1b` | EPSG:31287; one laser-derived maximum-deposit polygon. |
| AvaFrameData v1.0 | `avaFilisur1/avaFilisur1_release_area.gpkg` | 106,496 | `6bf7d86cd991f0becfb381a4ad4098900e1d1d54b90f4d89b6bc103385005ee9` | EPSG:2056; transformed from EPSG:21781 without an exact operation record. |
| AvaFrameData v1.0 | `avaFilisur1/avaFilisur1_deposition_area.gpkg` | 110,592 | `9b568cdbe2f865f8c10a3da81bb1fc54fb986e576c5789dae3b05fccb4082de3` | EPSG:2056; deposit polygon, null thickness. |
| AvaFrameData v1.0 | `avaFilisur2/avaFilisur2_release_area.gpkg` | 106,496 | `67779a91cc4e71ee98b636098af39a957deecd9c2ba83e73861b8346fdc07f0a` | EPSG:2056; transformed from EPSG:21781 without an exact operation record. |
| AvaFrameData v1.0 | `avaFilisur2/avaFilisur2_deposition_area.gpkg` | 110,592 | `2dc117e9f6bf58d3e37f0596f04bd52e114c3e1ae0233ad3d1fac629ee6c8ec8` | EPSG:2056; deposit polygon, null thickness. |
| AvaFrameData v1.0 | `avaKleinerOetscherbach/releaseArea20090225.shp` | 1,348 | `cb493aa7e30dcdb8aad21d7709e24bf80f6697f990c415c688b83340e574d0a5` | EPSG:31287; `thickness` and `ci95` are null. |
| AvaFrameData v1.0 | `avaKleinerOetscherbach/eventArea20090225.shp` | 1,660 | `73843ad83f685510b566356079656d030a0d1ebd580255f7c0c1ceb02da6be25` | EPSG:31287; whole-event area, not a distinct deposit or survey domain. |
| AvaFrameData v1.0 | `avaPopeletzbach/releaseArea20090407.shp` | 412 | `b5d401154186fca9acdafd30ef31683823f0c2cd92928578edef2ef1fef575b1` | EPSG:31287; `thickness` and `ci95` are null. |
| AvaFrameData v1.0 | `avaPopeletzbach/eventDepositionArea20090407.shp` | 892 | `377855443c8d01bc47479d17909291721032ad80c25bbd68aa5535e45f38095b` | EPSG:31287; deposit `volume` and `thickness` are null. |
| VdS 20243024 | [`readme.docx`](https://zenodo.org/api/records/17104410/files/readme.docx/content) | 15,801 | `9c48af4848756f44cff098ac865be1a7ad3e2e58c77831f48f689c1149afbd9c` | No map CRS; record-level CC BY 4.0; documents unresolved synchronization and measurement-validity obligations. |
| VdS 20243024 | [`20243024_velocity_4000_200.dat`](https://zenodo.org/api/records/17104410/files/20243024_velocity_4000_200.dat/content) | 2,040,881 | `ee9f4f7b1155d92fb3f0a94cc97b3e3906ac1ba8bf31db8e9c58ae310f617f72` | Time/height velocity table; no map coordinates or per-value uncertainty. |
| VdS 20243024 | [`pressure.txt`](https://zenodo.org/api/records/17104410/files/pressure.txt/content) | 35,708,066 | `dd02007b009aa6fe551274357cf83bfa26d8f240093d2f881e6e31ba4bd0e6d8` | Time/pressure series; calibration and uncertainty are not packaged. |
| VdS 20243024 | [`GEODAR-2023-12-02-05-19-57-ch-04.png.jpg`](https://zenodo.org/api/records/17104410/files/GEODAR-2023-12-02-05-19-57-ch-04.png.jpg/content) | 619,597 | `e83ce3e457eed00b1fb98ed152d1007a87c091d8f4694638bcdc9ed2aea8dfc9` | Rendered range/time image, not raw radar or GIS. |
| VdS 20243024 | [`GEODAR-2023-12-02-05-19-57-mti-ch-00.png`](https://zenodo.org/api/records/17104410/files/GEODAR-2023-12-02-05-19-57-mti-ch-00.png/content) | 4,748,718 | `43b9cc5b2e94d5f5520e64607b37f65d09adfda1a6f2e9464157dcbb6967926a` | Rendered moving-target-indication range/time image, not raw radar or GIS. |

The other nine VdS files were inventoried from the authoritative Zenodo API but
were not downloaded solely to manufacture a SHA-256 list. Their exact repository
identities are:

| Metadata-inspected VdS 20243024 file | Bytes | Zenodo repository MD5 |
|---|---:|---|
| [`overview_high.mp4`](https://zenodo.org/api/records/17104410/files/overview_high.mp4/content) | 400,836,543 | `51334716f1f6de880de99431f11d595b` |
| [`overview_low.mp4`](https://zenodo.org/api/records/17104410/files/overview_low.mp4/content) | 334,748,178 | `bd5b7166f85d1465692fb13076300825` |
| [`overview_mid.mp4`](https://zenodo.org/api/records/17104410/files/overview_mid.mp4/content) | 961,028,974 | `3ff50d06cbcb2fd6d197122dd28ae5bb` |
| [`panoramic_image_part_earlySurge.jpg`](https://zenodo.org/api/records/17104410/files/panoramic_image_part_earlySurge.jpg/content) | 30,434 | `b64ae62769234ad864ae4deee6fadfbc` |
| [`panoramic_image_part_I.jpg`](https://zenodo.org/api/records/17104410/files/panoramic_image_part_I.jpg/content) | 98,034 | `e8b5e69b8557a14836e51e42e54d4853` |
| [`panoramic_image_part_II.jpg`](https://zenodo.org/api/records/17104410/files/panoramic_image_part_II.jpg/content) | 151,568 | `1139d5480cc3575ba94bf33496e7049b` |
| [`panoramic_image_part_III.jpg`](https://zenodo.org/api/records/17104410/files/panoramic_image_part_III.jpg/content) | 142,675 | `5f0dccc82e7791fc9d9bbf2e8a6556b8` |
| [`scriptPlots.mlx`](https://zenodo.org/api/records/17104410/files/scriptPlots.mlx/content) | 1,339,564 | `fc615ff89a9ef44be9e949b042adbd8a` |
| [`WorkspacePublication.mat`](https://zenodo.org/api/records/17104410/files/WorkspacePublication.mat/content) | 139,937,224 | `df9c355d07c0087180e900c43c6771bf` |

No local SHA-256 is claimed for those nine files. That does not affect the
eligibility decision: the repository itself lacks the required event geometry,
coverage, uncertainty and surface contracts.

### AvaFrameData v1.0 event-by-event eligibility

| Event | Release and deposit evidence | Date/time | Surveyed coverage / known absence | Boundary/endpoint uncertainty | Scenario and surface | Strict decision |
|---|---|---|---|---|---|---|
| Arzl | One WLV-estimated release polygon with 1.76 m thickness; no deposit GIS; runout only described as reaching the catchment-dam base. | README says `.01.2009` while the event label and cited documentation indicate January 2019; no exact time/confidence. | None. | None. | No complete event weather; bundled 5 m DTM has no acquisition epoch or snow-surface error bound. | Ineligible. |
| Eiskar | Two drone/laser-derived release polygons with 2.7 m and 2.2 m estimated thickness; visual deposit line and DFA/PSA/maximum deposit polygons. | Event about 01:00 on 15 January 2019; mapping/laser flight 18 January; no stated time confidence. | None; an imagery/laser footprint is not declared as surveyed known absence for every target. | None for release or deposit boundaries/endpoints. | Partial weather narrative and station plot; no complete uncertainty-bearing scenario. Aircraft DGM and post-event snow map are described but not packaged with a quantitative pre-event/event-day mismatch bound. | Ineligible; closest individual AvaFrameData event. |
| Filisur 1 | One release and one deposit polygon supplied by SLF. | 23 February 2012; no event time or confidence. | None. | None. | Release thickness is undocumented; the prior 1 m modeling value is explicitly standardized/assumed. Current swissALTI3D link is not an event surface or error treatment. | Ineligible. |
| Filisur 2 | One release and one deposit polygon supplied by SLF. | 23 February 2012; no event time or confidence. | None. | None. | Release thickness is undocumented; the prior 1 m modeling value is explicitly standardized/assumed. Current swissALTI3D link is not an event surface or error treatment. | Ineligible. |
| Kleiner Oetscherbach | One release polygon and one whole-event polygon; no distinct deposit/endpoint. | 25 February 2009; no event time or confidence. | None. | `ci95` is null; no method/confidence. | No complete scenario; only a generic Austrian DTM link and no surface-error treatment. | Ineligible. |
| Popeletzbach | One release, one whole-event and one deposit polygon. | 7 April 2009; no event time or confidence. | None. | `ci95` is null; no method/confidence. | Release thickness and deposit thickness/volume are null; no complete weather scenario or surface-error treatment. | Ineligible. |

All downloaded hashes above document this audit only. Mutable API responses,
summary tables and report files were not added to the trusted identity registry,
and none was used to select an event or view a model result.

### Data-owner contacts and precise acquisition blocker

The follow-on public search also checked primary descriptions of the
[18-event SLF/Monte Pizzac mass-balance study](https://doi.org/10.1029/2005JF000391),
the [Lautaret full-scale archive](https://doi.org/10.1016/j.coldregions.2015.03.005),
the [Ryggfonn full-scale archive](https://doi.org/10.1016/j.coldregions.2016.02.009),
and the [Seehore field archive](https://doi.org/10.3390/geosciences9110471).
These are strong holdings leads: their publications describe measured release
state, terrain/surface surveys, runout, or dynamics. They do not expose a
versioned public per-event package containing all original geometry, licences,
hashes, event-surface lineage, component attribution, quantified uncertainty,
and survey/detection masks required by the frozen protocol. They therefore add
zero eligible events and do not change the stop decision.

The exact owner request is now a machine-readable artifact at
[`field-validation-owner-request-v1.json`](../validation-data/acquisition/field-validation-owner-request-v1.json).
The owner-specific, send-ready messages are rendered at
[`field-validation-owner-requests-v1.md`](../validation-data/acquisition/field-validation-owner-requests-v1.md).
Its fillable delivery contract is
[`field-validation-owner-delivery-v1.schema.json`](../validation-data/acquisition/field-validation-owner-delivery-v1.schema.json),
implemented by `avycore.validation.acquisition`. A returned delivery can be
checked without model imports or predictions using:

```powershell
python scripts/validation/validate_field_validation_owner_delivery.py `
  <owner-delivery-1.json> <owner-delivery-2.json> `
  --require-complete-cohort --output <immutable-preflight.json>
```

This preflight verifies structure, original byte counts, SHA-256 identities,
immutable licence-record bindings, UTC times, DEM CRS/datum lineage, direct
release/thickness/density/terminal observations and uncertainty, and explicit
observed-negative versus unknown survey-mask semantics. Required observations
declared missing, inferred, substituted, assumed, back-calculated or
model-derived are rejected. It also checks the
12-event/six-path/two-mountain/three-storm minimum. Passing it permits independent
scientific review only. It does not register trust, assign a split, authorize a
prediction, or establish validation.

The request tells owners to leave an unavailable observation missing rather than
infer or substitute it, so a partial delivery is expected and the strict contract
rejects it as one opaque error. To answer the owner per event instead:

```powershell
python scripts/validation/triage_field_validation_owner_delivery.py `
  <owner-delivery-1.json> <owner-delivery-2.json> `
  --output <immutable-triage.json> --client-request <what-we-still-need.md>
```

Triage runs the same unmodified contract and attributes every rejection to an
event, the evidence profile it blocks, the exact schema path, and one of the
existing `ExclusionReason` literals, then rolls the result up against the same
cohort minimum. It is advisory: it assigns no eligibility, trust, partition
membership, or permission to predict, a profile reported as supported is not an
accepted component, and only deliveries the strict contract accepted are handed
to the real cohort gate. Profile E is reported unsupported for every delivery
because this schema carries no forcing, snow-state, or release-to-solver
conversion evidence at all.

Eligibility then requires at least two isolated, identity-verified human reviews
per event. Reviewers are blind to predictions, other reviews and any holdout
assignment; AI output cannot satisfy the human-review schema. Disagreements bind
both original review hashes to a third independent human's conflict-resolution
record. Every event in the verified delivery cohort must have an eligible or
ineligible decision, including an exclusion reason where applicable, before the
cohort can be sealed. The immutable adjudication command is:

```powershell
python scripts/validation/adjudicate_field_validation_eligibility.py `
  --delivery <owner-delivery.json> --event-id <event-id> `
  --review <review-1.json> `
  --review <review-2.json> [--conflict-resolution <resolution.json>] `
  --output <eligibility-decision.json>
```

The no-event split procedure is preregistered at
[`field-validation-group-split-preregistration-v1.json`](../validation-data/acquisition/field-validation-group-split-preregistration-v1.json).
It freezes seed `20260815` and
`connected-group-components-balanced-dp-v1`, assigns whole connected components
sharing any path, mountain or storm cycle, and contains no real event IDs or
assignments. Only after a fully adjudicated eligible cohort passes 12/6/2/3 can
`freeze_field_validation_cohort_split.py` create an accepted-cohort record, split
and holdout-observation seal. The prediction and metric loaders reject absent,
incomplete or identity-mismatched cohort/split/seal/prediction records before
loading protected code. All recalculated file hashes and the unchanged zero-event
status are recorded in
[`field-validation-acquisition-integrity-v1.json`](../validation-data/acquisition/field-validation-acquisition-integrity-v1.json).

Official contact routes were rechecked on 15 August 2026 UTC. No email or form was
sent:

| Holding | Current official route used by the request artifact |
|---|---|
| SLF, including Monte Pizzac and Vallée de la Sionne | [SLF data service](https://www.slf.ch/fr/services-et-produits/service-de-donnees-du-slf/), `data@slf.ch` |
| INRAE Lautaret | [INRAE snow-monitoring portal](https://monitoring-stations.ara.inrae.fr/), `florence.naaim@inrae.fr`, `herve.bellot@inrae.fr` |
| NGI Ryggfonn | [NGI Ryggfonn programme](https://www.ngi.no/en/research-and-consulting/natural-hazards-container/avalanches-and-slides/avalanches-and-slush-flows/ryggfonn/), `heidi.hefre@ngi.no`, `peter.gauer@ngi.no` |
| Seehore | [University of Turin profile](https://unifind.unito.it/individual?uri=http%3A%2F%2Firises.unito.it%2Fresource%2Fperson%2F9997) and [Polytechnic University of Turin profile](https://www.polito.it/personale?p=monica.barbero), `michele.freppaz@unito.it`, `monica.barbero@polito.it` |
| BFW / WLV | [BFW Snow and Avalanches](https://www.bfw.gv.at/en/departments-en/natural-hazards/snow-avalanches/) and [WLV avalanche centre](https://www.bmluk.gv.at/themen/wald/wald-und-naturgefahren/wildbach--und-lawinenverbauung/organisation-kontakt/fz_geologie_lawinen.html), `felix.oesterle@bfw.gv.at`, `schneelawine@die-wildbach.at` |
| Parks Canada, Rogers Pass | [Glacier National Park contact](https://www.parks.canada.ca/pn-np/bc/glacier/info/contact), `mrg.information@pc.gc.ca` |

- Vallée de la Sionne / SLF: [`data@slf.ch`](mailto:data@slf.ch) and, for the
  20243024 archive, [`ivan.calic@slf.ch`](mailto:ivan.calic@slf.ch). Request a
  versioned package for at least six candidate events with event-specific release
  polygons, deposit polygons and surveyed endpoints, complete observation-domain
  polygons, boundary/endpoint uncertainty and confidence, event scenario inputs,
  and pre-event snow-surface products. Ask specifically for the event-level source
  geometry behind the GEODAR release/runout interpretation rather than a digitized
  paper figure. Velocity/path observations should retain their sensor calibration
  and uncertainty so they can test `dynamics_only`. For event 20243024, request the
  raw radar/time-position payload rather than rendered PNGs; the clock-alignment
  procedure and residual error; sensor coordinates and height above the changing
  sliding surface; calibration, valid intervals and per-variable uncertainty; and
  confirmation that CC BY 4.0 covers every deposited file and written permission
  for any additional non-public source files. For the Hafner archive, request
  exact Study 2 event dates, distinct release/deposit interpretation layers, the
  survey/interpretation footprint and event-specific boundary-confidence model;
  the existing whole-event outlines alone remain insufficient.
- Ryggfonn / NGI: [`heidi.hefre@ngi.no`](mailto:heidi.hefre@ngi.no) (Head of
  Section Snow and Rock Hazards), [`peter.gauer@ngi.no`](mailto:peter.gauer@ngi.no)
  (Ryggfonn WP lead), or [`ngi@ngi.no`](mailto:ngi@ngi.no). Request six to eight
  strongest candidate events drawn from the seven listed for 2023/24 and 15 listed
  for 2024/25, and the source GIS, laser products and field notes behind the report
  figures rather than the figures themselves. The package
  must add event release and deposit/endpoint surveys, surveyed footprints and
  known-absence semantics, boundary uncertainty/confidence, complete scenarios,
  pre-event surfaces/error bounds, raw radar/pressure/velocity data with clock
  synchronization and calibration, reuse permission, CRS and file hashes. The
  public 2019/20 and 2023/24 notes are not sufficient by themselves.
- AvaFrameData / BFW and WLV: [`felix.oesterle@bfw.gv.at`](mailto:felix.oesterle@bfw.gv.at),
  [`anna.wirbel@bfw.gv.at`](mailto:anna.wirbel@bfw.gv.at),
  [`frank.perzl@bfw.gv.at`](mailto:frank.perzl@bfw.gv.at), and
  [`schneelawine@die-wildbach.at`](mailto:schneelawine@die-wildbach.at). Ask whether
  the six v1.0 events can be released with original, pre-conversion survey files;
  exact event times and confidence; field/remote-sensing methods, dates,
  instruments and calibration; boundary uncertainty/confidence; survey-coverage
  polygons with known-absence semantics; complete event snow/weather inputs with
  uncertainty; and pre-event surfaces/error bounds. Request the exact
  EPSG:21781-to-EPSG:2056 operation for Filisur and source-to-v1.0 derivation for
  every geometry. Request the original WLV event records identified as 6534 and
  6591, the original Eiskar drone/laser products promised for future publication,
  and the Arzl 17 January 2019 UAS imagery, point cloud, orthophoto, surface model
  and deposit geometry documented in the BFW presentation. The current public WLV
  path-catchment GeoPackage is not a substitute. Also ask whether the collection
  can supply at least five fully
  documented events on each of two independent mountains, so both can retain
  three calibration and two untouched holdout events after eligibility review.
- Nordkette / BFW: [`jt.fischer@bfw.gv.at`](mailto:jt.fischer@bfw.gv.at). Ask for
  the raw AvaNode and radar observations used by the 22 February 2022 paper, the
  exact radar/survey coverage including the uncaptured lower runout, a surveyed
  terminal/deposit geometry, release and deposit boundary uncertainty, measured
  release depth, full wind scenario, and the DEM/snow-surface epoch and error
  model. One completed Nordkette event cannot meet the multi-event gate, so this
  would be supplementary unless comparable independent events can be released.
- RegObs / NVE: [`regobs@nve.no`](mailto:regobs@nve.no). Ask whether a
  professionally reviewed subset has polygon-specific uncertainty, a formal
  completeness/known-absence survey domain, exact event-time confidence, and
  event-linked snow/weather and terrain-surface provenance not exposed by API v5.
  `ObsLocation.Uncertainty` is not a substitute for release/deposit boundary
  uncertainty.
- Rogers Pass / Parks Canada: [`mrg.information@pc.gc.ca`](mailto:mrg.information@pc.gc.ca).
  Ask the avalanche programme whether research access can be granted to a
  de-identified, licensed event package and whether the records identify distinct
  mountains rather than only path sectors.
- CAIC: [`caic@state.co.us`](mailto:caic@state.co.us). Ask whether staff-surveyed
  post-event geometries and survey footprints exist behind selected final reports,
  with release/deposit separation, original coordinates/CRS, field-method and
  boundary uncertainty, complete event scenario, pre-event surface/error treatment,
  versioned files, permission and hashes suitable for research release. The
  accident workbook and narrative reports alone are insufficient.
- French EPA/CLPA derivative: [`marion.momber@inrae.fr`](mailto:marion.momber@inrae.fr).
  Ask whether the source event/profile join, event dates and coordinates can be
  released with event-specific observation geometry and quality metadata; maximum
  site envelopes alone remain unusable.

The single next requirement is one independently code-reviewed,
licence-compatible, untouched cohort of at least 12 eligible dry dense-slab
events spanning at least six independent paths, two mountains and three storm
cycles, with calibration and holdout groups frozen before any prediction. More
than the minimum number of candidates should be requested because an event must
be dropped before freezing if any required field is absent. For each retained
event, the owner must provide:

1. original source files, per-file byte counts and SHA-256 identities, copyright
   holder, licence or written permission, permitted use, redistribution status,
   and an immutable permission-record binding;
2. independently supported UTC event start/end times and dry dense-slab
   classification evidence;
3. a pre-event snow-surface DEM with acquisition times, horizontal and vertical
   CRS/datum realizations, epoch, axis order, transformation lineage and
   horizontal/vertical uncertainty;
4. an independently observed release geometry plus event-specific slope-normal
   release thickness and density measurements, each with bounds, confidence,
   method and immutable direct-observation provenance;
5. a component-attributed terminal dense-flow deposit polygon or endpoint with
   original geometry, CRS lineage, method and positional uncertainty;
6. survey coverage and detection/occlusion masks that declare detections,
   observed negatives inside complete coverage, and unknown cells outside,
   masked or occluded, with detection limits and uncertainty;
7. independently supported path, mountain and storm-cycle grouping; and
8. for a dynamics claim, calibrated time-position/velocity/path observations with
   measurement uncertainty. Extent alone can select/test alpha but cannot
   identify `mu` or `xi` or validate Voellmy dynamics.

After acquisition, every delivered event must be adjudicated by the blinded
multi-human process before registration. The complete eligible cohort, grouped
calibration/holdout assignment, holdout observation seal, pass criteria,
sensitivity ranges, source hashes, bakes, and parameters must then be frozen
before any prediction is viewed. The metric record is
`validation-data/experiments/public-data-field-validation-v2.json`: endpoint
distance error, footprint IoU, false-positive area and false-negative area are
predeclared; depth, velocity or pressure can be activated only when independent
observations provide matching semantics, units, masks and uncertainty.
Until that package exists, no strict MountainPack/bake, calibration, holdout run,
IoU, endpoint error, or within-uncertainty fraction is scientifically available.

At the conclusion of this source-audit phase, the strict holdout remained
**N = 0**,
`is_validated=false`, and `TRUSTED_DATASET_IDENTITIES_SHA256` remains an empty
`frozenset`. No candidate identity was registered or trusted. No strict
MountainPack, bake, calibration, holdout prediction, sensitivity run or scoring
was started. No new or strict model prediction was generated or inspected; only
the pre-existing documented qualitative results in this required report were
read as context. A subsequent blind positive-only storm-window hindcast was run
and is reported below; because its source mapping lacks verified negatives and
an exact machine-readable survey footprint, it does not change that strict
holdout count or registry status.

## Frozen split and parameters

The qualitative experiment specification was frozen on 12 August 2026 before
results were generated. The original frozen file's SHA-256 is
`ec7f104cb68b9bdba064ba2e5806fe126cb342da1a0a5ee0ecac434eefff7f85`.
After results existed, a recorded, result-independent schema amendment renamed
`mountain_area` to `analysis_group`; it changed no event membership, partition,
parameter, geometry rule, or scoring rule. The amended specification's SHA-256
is `5c2bd2e80afdbc11a91df5fab10915b0184931b3df481ed1dd1a84375b010190`,
and it preserves the original identity explicitly. The preassigned partitions
are:

- Qualitative calibration: Wildi, SPOT 732, SPOT 820, and SPOT 837.
- Qualitative check: Rüchi, SPOT 754, SPOT 840, and SPOT 1034.

The word “calibration” names the partition only. **No calibration or parameter
fitting was performed**, because neither partition is eligible field evidence.
The check partition is likewise not a field-validation holdout. The frozen
`alpha_only` configuration used the baseline parameter manifest (SHA-256
`aaafd6f9fc6e8d598cb56c479bcc072374ee94ba17613c0ac2df0594f8059628`),
minimum flux 0.02, spreading 0.35, maximum path length 4,000 m, and configured
alpha angles 32°, 27°, 23°, and 19° for the four release-size labels. Parameter
tuning was forbidden, and `FastRunoutEngine` makes no stochastic draws.

This experiment cannot tune or test Voellmy `mu` or `xi`, because those dynamics
are absent from `alpha_only`. It also cannot sweep release depth:
`FastRunoutEngine` has no release-depth input. Changing the release-size label
changes alpha and must be read as an **alpha sensitivity sweep**, not as release
depth or released-mass sensitivity.

## Strict holdout result

**Component this table would test:** alpha-controlled runout/deposit extent plus
routing. All strict metrics are unavailable because the eligible holdout N is
zero, not because a missing value was replaced with a favorable score.

| Frozen analysis grouping | Registered qualitative events | Strict holdout N | Deposit IoU | Endpoint error (m) | Within-uncertainty fraction | Reason unavailable |
|---|---:|---:|---:|---:|---:|---|
| Brämabühl | 2 | 0 | unavailable | unavailable | unavailable | No complete surveyed negative domain, quantified positional uncertainty, or registered endpoint observation. |
| Stillbergalp sector | 2 | 0 | unavailable | unavailable | unavailable | Whole SPOT footprints only; release is derived, deposit is not separate, and absence and positional uncertainty are unknown. |
| Totalp sector | 3 | 0 | unavailable | unavailable | unavailable | Whole SPOT footprints only; release is derived, deposit is not separate, and absence and positional uncertainty are unknown. |
| Schwarzseealp sector | 1 | 0 | unavailable | unavailable | unavailable | Whole SPOT footprint only; release is derived, deposit is not separate, and absence and positional uncertainty are unknown. |
| **Total** | **8** | **0** | **unavailable** | **unavailable** | **unavailable** | **No event satisfies the strict independent-holdout contract.** |

Therefore there are no defensible holdout IoU, endpoint-error, or
within-uncertainty numbers to report.

## Blind national SPOT storm-window hindcast (failed)

**Task:** positive-only storm-window avalanche susceptibility and runout-
footprint hindcasting, not exact event-time prediction. Exact release times are
not available in the mapping packages. The frozen
[experiment specification](../validation-data/experiments/spot-blind-swiss-v1.json),
[runner](../scripts/validation/run_spot_blind_hindcast.py),
[development result](../validation-data/results/spot-blind-swiss-v1-development.json),
and [holdout result](../validation-data/results/spot-blind-swiss-v1-holdout.json)
record the complete machine-readable experiment. Their file SHA-256 values are,
respectively, `f208e81e7d6bd6dabe8f92c99859da272d5c3dda432e4e7fcfc5a9145bc18075`,
`b73e6c7f84f46cc0fc3b6127dce88412ca666fd02f7f165ebd8289e66a63b6de`,
`8af542a4b89387937607956a67ec4a63cebba0eff9b96b93f88923a6332fc857`,
and `e0a8b30c031a6b4a8aa58883310bb9de710c7caecaf0d20063490a894777dce5`.

The 2018 campaign was assigned to development and the 2019 campaign to the
one-time final holdout. Every core is a predeclared 670 × 670 cell, 30 m
EPSG:2056 tile; a 5.1 km terrain halo allowed runout to leave the core without
allowing halo terrain to generate release cells. The exact split was:

| Partition | Fixed mountain block | EPSG:2056 core bounds, metres | Storm window, UTC |
|---|---|---|---|
| Development, 24 January 2018 mapping | Western Bernese Alps | `[2600000, 1140000, 2620100, 1160100]` | 21 January 06:00 to 24 January 06:00, 72 preceding hourly bins |
| Holdout, 16 January 2019 mapping | Gotthard Alps | `[2680000, 1158000, 2700100, 1178100]` | 13 January 06:00 to 16 January 06:00, 72 preceding hourly bins |
| Holdout, 16 January 2019 mapping | Glarus Alps | `[2700000, 1190000, 2720100, 1210100]` | same |
| Holdout, 16 January 2019 mapping | Albula Alps | `[2760000, 1158000, 2780100, 1178100]` | same |
| Holdout, 16 January 2019 mapping | Silvretta and Lower Engadine | `[2800000, 1180000, 2820100, 1200100]` | same |

The prediction stage did not import GeoPandas or open an outline file. It read
only terrain, land cover, the frozen historical weather payload, and the
production parameter manifest. The existing deterministic release score chose
release cells and zones itself; the hybrid, alpha-only, and dynamics-only
engines then started only from those predicted zones. Target files were opened
only by the later scoring stage. No outline supplied a release proxy, seed,
direction, threshold, tile, stopping point, or other model input. The primary
system was `hybrid`, with six predicted zones simulated per block; its particle
paths and velocity use the Voellmy integrator, while extent remains partly
bounded by the empirical alpha energy line. The uniform frozen scenario was
`medium` and `dry_slab`; mapped size and type attributes were never model
inputs.

### Frozen source lineage

| Source | Version, licence, CRS/units and transformation | Frozen payload identity |
|---|---|---|
| SLF/EnviDat national SPOT avalanche mapping | 24 January 2018 and 16 January 2019 packages; ODbL 1.0/DbCL; EPSG:2056 metres; evaluation only, with invalid geometry repair, centroid-to-fixed-core assignment and all-touched rasterization after prediction | 2018 ZIP: 157,117,977 bytes, `087c036f1a3e4213c2332fad4497fd292c6af6f0df1629c5aaa887a45387c2f5`; 2019 ZIP: 56,256,600 bytes, `af4099d949fb567c0bc07b3e46cbca40e6b8f7c4340a6fbe7311e2104e93251f`. Extracted SHP/SHX/DBF/PRJ/CPG and key-PDF hashes are frozen individually in the specification. |
| Copernicus DEM GLO-30 | GLO-30 1 arc-second DSM, acquired 2011–2015; Copernicus DEM free-licence terms; EPSG:4326, EGM2008 orthometric metres; bilinear warp to EPSG:2056 at 30 m, then production Horn slope/aspect and Zevenbergen–Thorne curvature | N46E007 `426e3d1492d94e4f23d444843f852ff5be675da5c4c14e661b5642006864047d`; N46E008 `47769664394ee1ff3b51f5c1b43a3441d7f7bce8d086d0ed2f3a892b895fae3e`; N46E009 `6a7eccb6d198f01a1fdfcca0e1cef837ef294456fb7243ec4d0966e089b1e7fc`; N46E010 `d24d707f394668ef429b28b22d1afc51f7ac5047293de177e46d9dc186c6b448`; N47E008 `4e2917429d65e049a4f1ddee2093f7fc50f11d22d0fd7cee85f5c8334186d7fe`; N47E009 `ca0b7567f5e12b8c814500bcf434855fb2a1084248bb80d3c80a91169887817c`. |
| ESA/UCLouvain GlobCover | GlobCover 2009 v2.3, 300 m; ESA/UCLouvain educational/scientific-use policy; EPSG:4326 Plate Carrée categorical classes; nearest-neighbour warp to EPSG:2056 at 30 m and frozen class-to-forest-fraction mapping; class 0 remained masked | ZIP: 380,992,056 bytes, `3a5e46b589f6b650759308d4ccb2d62d906a8ffc6f44c6595545e18702a3f7c6`; TIF: 392,327,352 bytes, `db48c60d22d959a93dc4137abac9ed1946d45d2469a4e07eeed8947a68b6c06a`. |
| ERA5 through the Open-Meteo Historical Weather API | ERA5 hourly, explicitly `models=era5`; Open-Meteo API data CC BY 4.0 with upstream Copernicus/ECMWF attribution; regular WGS84 0.25° grid. Nine fixed points per core; 72-hour snowfall sums averaged spatially; temperature retained as a diagnostic; scalar 10 m wind averaged and wind-from reconstructed from mean components. Any required null would stop the block. | Development `0f6d806663c4c0357c92bfbed60536bd21b7915c19b5dd3b1fad696c17d32092`; Gotthard `d20ae9520efb096123286b8774a0eed1bd560306bbaa511030284dffe14c9483`; Glarus `4c9bfc889d7d79d61f31718edff69fdd342d14cc2795f687a3c5c88e53252e16`; Albula `1ebd06ee85ff959a96269c53b14f1038fe366b6d90158fa07e16bfdb0b1b0d2b`; Silvretta `55e914d5bd90e11ef7f17eac8ee2151fe52c8ab7474c370f65db7d155958ce13`. Exact request URLs and response sizes are in the specification. |

The aggregated inputs were 60.9 cm new snow, 3.6 km/h wind from 169.0° for
development, and respectively 36.8/6.4/313.4°, 50.5/5.9/277.1°,
29.3/5.2/342.4°, and 33.8 cm/5.3 km/h/295.7° for the four holdout blocks.
Temperature ranges and all returned grid-point metadata remain in the hashed
artifacts. Temperature was not passed into `Conditions`: the source snowfall
was already phase-classified, so applying the model's temperature classifier a
second time would double-count phase.

### Frozen evaluation and result

An event was captured only when at least 10% of its mapped-positive raster cells
intersected the prediction. Outlines crossing a core boundary were retained and
forced to fail rather than silently excluded. The same-area baselines were the
highest-slope cells and 256 seeded uniform draws without replacement. The
predeclared rule required every qualifying holdout block to have at least five
events, capture at least 70%, flag no more than 20% of eligible terrain, beat
both the slope-only capture and the random 97.5th percentile, have complete
inputs, and avoid runout-domain escape.

Development captured 15/282 events (5.32%), covered 5.71% of mapped-positive
cells, and flagged 1.55% of eligible terrain. This already failed the 70%
criterion. The experiment was nevertheless frozen and the holdout executed once
to obtain the requested falsifying test; no scientific parameter or threshold
was changed.

| Held-out block | Events | Predicted release zones | Hybrid captured | Mapped-positive coverage | Eligible terrain flagged | Slope-only capture | Random capture mean / 97.5th percentile | Block result |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Gotthard | 241 | 0 | 0 (0%) | 0% | 0% | 0% | 0% / 0% | Fail |
| Glarus | 433 | 40 | 22 (5.08%) | 2.28% | 1.22% | 6.00% | 2.00% / 3.46% | Fail; worse than slope-only |
| Albula | 182 | 0 | 0 (0%) | 0% | 0% | 0% | 0% / 0% | Fail |
| Silvretta/Lower Engadine | 169 | 0 | 0 (0%) | 0% | 0% | 0% | 0% / 0% | Fail |

All four blocks qualified. Aggregate primary-hybrid capture was 22/1,025,
2.15%, with a seeded 10,000-replicate mountain-block bootstrap 95% interval of
0%–4.46%. Mapped-positive footprint coverage was 0.640% (95% interval
0%–1.85%); 0.304% of the complete eligible terrain was flagged (95% interval
0%–0.913%); and predicted:mapped area was 0.0591×. The low flagged fraction is
not evidence of safety or successful specificity: unmapped terrain is unknown,
not verified negative.

| Frozen component or ablation | Captured events | Capture | Mapped-positive coverage | Eligible terrain flagged | Predicted:mapped area |
|---|---:|---:|---:|---:|---:|
| Predicted release cells only | 22/1,025 | 2.15% | 0.531% | 0.247% | 0.0481× |
| Hybrid routed non-release cells only | 1/1,025 | 0.098% | 0.108% | 0.0569% | 0.0111× |
| Primary hybrid end to end | 22/1,025 | 2.15% | 0.640% | 0.304% | 0.0591× |
| Alpha-only end to end | 25/1,025 | 2.44% | 1.066% | 0.480% | 0.0932× |
| Dynamics-only end to end | 22/1,025 | 2.15% | 0.640% | 0.304% | 0.0590× |

The frozen acceptance rule **failed**. Thirty-seven boundary-crossing target
outlines remained in the denominator as uncaptured. No event had incomplete
model inputs; every terrain core and halo had complete required coverage; and
no runout engine reached the simulation boundary or lost particles from the
AOI. A post-score replay wrote to a separate temporary directory and reproduced
all four holdout NPZ files byte-for-byte, including prediction identities
`9b857c86…` (Gotthard), `0c5a6842…` (Glarus), `61762740…` (Albula), and
`5c131a62…` (Silvretta).

Failure is dominated by release localization/loading: the frozen score produced
no release zone in three of four blocks, while in Glarus the 22 release-only
captures were the same 22 events captured by the primary end-to-end mask.
Hybrid routing added mapped-positive footprint but no additional captured event;
the routed-non-release mask alone captured one event. Alpha-only added three
captures but remained far below acceptance. Contributing limitations include
the approximately 25 km meteorology, a 30 m historical DSM rather than an
event-day snow surface, 300 m land cover, a uniform medium dry-slab scenario,
and no weak-layer, evolving snowpack, full-depth, loose-snow, wet-snow,
entrainment or deposition physics. Of the 1,025 targets, 261 were mapped as
full-depth, 55 as loose snow, and 141 as unknown, yet all were retained. The
archives also lack an exact machine-readable acquisition-footprint polygon, so
even the fixed blocks cannot establish known avalanche absence.

This failed experiment supports no field-validation or operational-performance
claim. It adds **zero** events to the strict field holdout: strict N remains 0,
`is_validated` remains `false`, and the trusted dataset identity registry remains
empty. No parameter was changed after viewing holdout results, no holdout was
rerun with tuned parameters, and no email or other external message was sent.

## Frozen multi-regime CERRA hindcast (failed)

**Status:** failed. This experiment improves the temporal and mechanistic
representation relative to the earlier scalar-forcing SPOT hindcast, but it
does not validate release or runout. The frozen primary end-to-end result
captured 446 of 1,798 positive mapped avalanches (24.81%) while flagging 5.12%
of eligible terrain. It failed the 70% capture requirement in every block,
underperformed both same-area baselines in every block, and had 35 mapped
events with incomplete aerial-image/cloud support. No parameter was changed
after holdout results were viewed and the holdout prediction was not rerun.

The committed artifacts are the
[selection](../validation-data/experiments/regime-hindcast-v1-holdout-blocks.json),
[acquisition record](../validation-data/experiments/regime-hindcast-v1-acquisition.json),
[frozen specification](../validation-data/experiments/regime-hindcast-v1.json),
[development result](../validation-data/results/regime-hindcast-v1-development.json),
[holdout result](../validation-data/results/regime-hindcast-v1-holdout.json), and
[derived source/result report](../validation-data/results/regime-hindcast-v1-summary.json).
Their file SHA-256 identities are, respectively,
`ba9b09179975786d5f128e7e9b85b620971f3bd20adc9fb7830f3bff56b94f75`,
`76e702e8125bf96429a903ed8be0094c2d7951282689d09d1c112a51127e1d5e`,
`44efb44b5d5a47ff776a684d9c3b784ad25fc8d22efc026bd984da5c26877be6`,
`d8319607408e719a0c2a79dd2bb950fb9c650a7e99f5b6c933cbde30a56c224f`,
`ee26d4038ac7d2022181729bbee88b1f717a04ba3bf93305f066686584986b81`,
and `d34442e15c1a0ffa62e4793b6d51cc05c966135664eaf41ab8432282524d80fa`.
The frozen specification binds the runner, runout code, all five snow/regime
modules, both parameter manifests, and every source byte.

### Scientific behavior and limits

The experiment changes the research-only AvyCore path, not the serving
application's previously frozen release path:

- CERRA replaces ERA5 forcing. CERRA is an hourly 5.5 km regional reanalysis,
  retrieved as nine nearest native-cell samples per 30.3 km simulation tile
  with API elevation downscaling disabled. Its temperature, total
  precipitation, modelled snow depth, wind-from direction and shortwave
  radiation enter the state calculation; provider snowfall is retained only as
  a diagnostic so precipitation phase is not counted twice. The source is
  [CERRA single levels, DOI 10.24381/cds.622a565a](https://doi.org/10.24381/cds.622a565a),
  CC BY 4.0. CERRA is finer than ERA5's approximately 25 km grid, but resampling
  it to 30 m adds no meteorological information.
- An hourly state accumulates and settles recent new snow, computes a bounded
  drift-potential index only during transportable cold-snow hours, preserves
  the meteorological wind-**from** convention through a drift-weighted circular
  mean, and tracks rain on snow, positive degree-hours and modelled snow depth.
  The wind thresholds are associated with snow-surface state in
  [Li and Pomeroy (1997)](https://doi.org/10.1175/1520-0450(1997)036%3C0205:EOTWSF%3E2.0.CO;2);
  the accumulated cubic excess-wind index and its saturation scale remain
  uncalibrated transport-potential heuristics, not transported mass.
- A pre-storm cold/calm/dry-and-buried indicator is reported as a
  weak-interface **diagnostic only**. Surface-hoar formation and persistence
  depend on coupled vapour, radiation, wind, precipitation, terrain and
  vegetation processes; see
  [Wever et al. (2024)](https://doi.org/10.5194/tc-18-2557-2024).
  The diagnostic observes no layer or stability test and has exactly zero
  numerical effect on release.
- Dry slab, surface-wetting susceptibility, and dry-loose release are evaluated
  separately, from target-independent forcing and terrain, with different
  activation masks and slope responses. Wet snow is not split into wet slab
  and wet loose because CERRA has no internal liquid-water profile. Dry-loose
  runout uses the existing dry-slab friction factors because no calibrated
  loose-snow factors are available.
- Full-depth/glide remains a distinct but explicitly unsupported regime. Air
  temperature, rain and surface snow depth are not substituted for basal
  liquid water, smooth ground or glide-crack observations. This refusal follows
  SLF's description of liquid water at the snow/soil interface as essential and
  glide timing as exceptionally difficult to predict; see the
  [SLF glide-snow project](https://www.slf.ch/en/projects/glide-snow-avalanches/).
- Release-zone smoothing is re-intersected with the required terrain,
  mechanism and missing-data masks. Missing terrain or forcing cannot be
  bridged into a release zone. Identical inputs and seeds replay
  deterministically on the characterized machine.

Every score remains an uncalibrated relative index, never a probability. This
is an experimental research prototype, not an operational forecast, and it
does not replace Avalanche Canada guidance or field assessment.

### Frozen split, lineage and target isolation

Development used all already-viewed 24 January 2018 and 16 January 2019 SPOT
blocks: Western Bernese, Gotthard, Glarus, Albula and Silvretta. The new holdout
used the separate 25 February–1 March 1999 panchromatic aerial campaign. Its
five 20.1 km cores came from a fixed LV95 lattice and were ordered by
acquisition coverage, then coordinate, after excluding every previously scored
core and requiring at least 60% photographed/cloud-free coverage, at least 15%
DEM-derived avalanche terrain, and essentially complete DEM coverage. The
selection opened the acquisition footprint, cloud layer and DEM, but never the
1999 avalanche outline.

The 1999 imagery records a cumulative extreme winter rather than exact event
times. The frozen prediction therefore unions three independently integrated
cycles documented by SLF: 26–29 January, 5–10 February and 17–24 February;
see [SLF's 25-year retrospective](https://www.slf.ch/en/news/25-years-since-the-avalanche-winter-of-1999/).
The evaluation target is EnviDat v1.0, DOI
[10.16904/envidat.579](https://doi.org/10.16904/envidat.579), CC BY-SA 4.0,
containing 11,120 outlines mapped from imagery acquired 25 February–1 March.

Prediction and scoring were separate commands. Prediction source resolution
explicitly skipped every item whose role began with `evaluation_target`; each
NPZ records `held_out_outlines_opened=false`, binds the exact frozen spec hash,
and hashes every mask. Only after all five holdout NPZs existed was the outline
layer opened. Holdout prediction SHA-256 identities are:

| Block | Prediction SHA-256 |
|---|---|
| `holdout_1999_c04r04` | `3a77e1645c386ad2dcce2097b1a4f31dafb071fbeb33e9208f7205a83d72de80` |
| `holdout_1999_c09r05` | `976107f8d844a165d82f63aaf85cb51ee6a656d237977888d566eac24d439065` |
| `holdout_1999_c05r04` | `88718b24c8b966d5d218c2a64bd57e755909567b7cfa1779f383659a33c75637` |
| `holdout_1999_c03r01` | `61eafafd14a702dd783c9eea5cebfee1ce120002c177bf64ab111e5fc0774eeb` |
| `holdout_1999_c04r03` | `60b9938a31d7d30e814c3b59caacbf674020540c20709d77278698355ca5b2e4` |

The derived report enumerates all 65 used archive, shapefile-sidecar,
land-cover, DEM and CERRA artifacts with source/version, licence, CRS, unit,
transformation, missing-value rule, byte size and SHA-256. The important product
semantics are: target/coverage vectors are EPSG:2056 metre coordinates;
Copernicus GLO-30 is an EPSG:4326 one-arc-second DSM in metres, bilinearly
reprojected to EPSG:2056 at 30 m; GlobCover v2.3 is an EPSG:4326 categorical
300 m raster, nearest-neighbour reprojected; CERRA is native Lambert conformal
conic at 5.5 km and is assigned to cells by nearest sample without spatial
interpolation. CERRA snow depth is a model estimate, not an observation.

### Development and untouched holdout results

Development failed before the final freeze:

| Development block | Captured / mapped | Capture | Eligible terrain flagged | Same-area slope | Random p97.5 |
|---|---:|---:|---:|---:|---:|
| Western Bernese 2018 | 70 / 282 | 24.82% | 3.68% | 40.43% | 32.27% |
| Gotthard 2019 | 41 / 241 | 17.01% | 3.33% | 27.80% | 22.41% |
| Glarus 2019 | 96 / 433 | 22.17% | 4.02% | 48.04% | 34.87% |
| Albula 2019 | 31 / 182 | 17.03% | 2.33% | 35.16% | 15.93% |
| Silvretta 2019 | 23 / 169 | 13.61% | 2.93% | 37.28% | 21.30% |

The untouched holdout then failed without parameter changes:

| Fixed terrain block | Captured / mapped | Capture | Eligible terrain flagged | Same-area slope | Random p97.5 | Incomplete events |
|---|---:|---:|---:|---:|---:|---:|
| c04r04 | 133 / 446 | 29.82% | 4.43% | 67.94% | 41.26% | 2 |
| c09r05 | 92 / 427 | 21.55% | 4.67% | 60.19% | 41.45% | 3 |
| c05r04 | 64 / 276 | 23.19% | 5.36% | 66.67% | 54.35% | 8 |
| c03r01 | 52 / 235 | 22.13% | 5.41% | 53.19% | 58.72% | 3 |
| c04r03 | 105 / 414 | 25.36% | 5.87% | 61.11% | 61.59% | 19 |
| **Aggregate** | **446 / 1,798** | **24.81%** | **5.12%** | **62.40%** | per-block rule | **35** |

The fixed-terrain-block bootstrap—not a claim that the blocks are independent
mountain ranges—gave these 95% intervals over 10,000 seeded resamples:

- event capture: 24.81%, interval 22.00%–27.85%;
- mapped-positive footprint coverage: 8.79%, interval 6.77%–11.55%;
- eligible terrain flagged: 5.12%, interval 4.69%–5.58%.

All terrain and CERRA inputs were numerically complete in every core. The 35
incomplete events crossed the photographed-minus-cloud support boundary; they
remained in the denominator and were forced uncaptured. No particle left any
simulation AOI, no predicted or uncertainty mask reached a simulation
boundary, and the domain-escape rate was 0/5 blocks. The terrain-budget check
passed in all blocks, but the completeness, event-capture, slope-baseline and
random-baseline checks failed in all blocks.

### Ablations and regime strata

| Frozen output | Captured / 1,798 | Capture | Mapped-positive footprint coverage | Eligible terrain flagged |
|---|---:|---:|---:|---:|
| Release only | 440 | 24.47% | 8.13% | 4.82% |
| Routed non-release only | 37 | 2.06% | 0.65% | 0.31% |
| Hybrid end to end | 446 | 24.81% | 8.79% | 5.12% |
| Alpha-only end to end | 501 | 27.86% | 11.19% | 6.22% |
| Dynamics-only end to end | 446 | 24.81% | 8.79% | 5.14% |
| Dry-slab release only | 192 | 10.68% | 4.15% | 2.00% |
| Surface-wetting release only | 140 | 7.79% | 1.13% | 1.39% |
| Dry-loose release only | 240 | 13.35% | 4.33% | 2.14% |
| Full-depth/glide release only | 0 | 0% | 0% | 0% |

The regime release rows overlap and must not be added. Target-independent
mechanisms were also scored against mapped post-hoc type labels only after
prediction: slab 225/787 (28.59%), full-depth 133/660 (20.15%), loose snow
16/74 (21.62%), and unknown 72/277 (25.99%). Capturing full-depth-labelled
outlines does not mean the unsupported glide mechanism worked; those positives
were spatially intersected by another represented mechanism.

Release-only captured 440 events and hybrid end-to-end captured 446. Routed
non-release-only capture was 37, and alpha-only exceeded the primary hybrid but
still reached only 27.86%. The dominant failure therefore remains release
localization; no runout ablation approaches the acceptance threshold. The
strongest supported claim is limited: on this lower-rigor, positive-only 1999
comparison, the multi-regime/CERRA experiment captured substantially more
events than the earlier frozen scalar-forcing experiment, but it failed its
predeclared acceptance rule and underperformed simple same-budget controls.
It is not field validation.

Strict field holdout N remains **0**, `is_validated` remains `false`, and
`TRUSTED_DATASET_IDENTITIES_SHA256` remains empty. No dataset identity was
added, no email or external message was sent, no Mount Hosmer operational
prediction or real bake was read or run, and no parameter changed after the
holdout result was viewed.

## Release-engine repair and configuration search (failed its success rule)

**Status:** failed the predeclared success rule. The three defects documented in
[`release-engine-repair-plan.md`](release-engine-repair-plan.md) were repaired
and 128 release configurations were evaluated on development blocks. None beat
the same-area slope-only baseline on all five development blocks by the required
5 percentage points. The search therefore stopped on its predeclared PLATEAU
condition and **no reserved block was spent**: all four remain sealed.

The committed artifacts are the
[search result](../validation-data/results/release-config-search-v1.json) and its
[full sweep log](../validation-data/results/release-config-search-v1-sweep-log.jsonl),
which records every configuration evaluated, including the losers.

### What was repaired, and where

The repair lives in `avycore/snowpack/release_v2.py`, a new module. It is not an
edit to `risk.py`, `state.py`, `regimes.py` or `zones.py`, because
`spot-blind-swiss-v1` and `regime-hindcast-v1` bind those files by SHA-256:
editing them would make two already-published negative results unreplayable, and
rewriting the frozen digests to match is out of bounds. Every frozen source,
spec and digest in this repository is therefore unchanged, and both earlier
experiments still replay.

| Defect | Frozen v1 behaviour | Repair |
|---|---|---|
| Wind statistic | `mean(72 hours x 9 points)`, so every block's wind was below `WIND_TRANSPORT_MIN_KMH` and the largest weight in the model contributed exactly zero | the search path never scalarizes wind at all: `integrate_state` consumes the hourly `(sample, hour)` field per cell and accumulates the CERRA drift-potential index with a configurable kernel. `storm_window_wind_statistic` additionally offers a high quantile, a transporting-hours mean and a drift-weighted mean for any caller that must reduce a storm to one number; no configuration below used it |
| Unreachable threshold | `RELEASE_THRESHOLD = 55` needed a terrain capability of 1.143 (Albula) and 1.049 (Silvretta) with transport zero, and capability is bounded by 1 | the threshold became a searched parameter: eight values from 25 to 60, drawn from a declared ladder and a seeded grid. `derive_threshold` implements the plan's operating-point derivation and `required_capability` makes the bound checkable, but both are exercised by tests only and neither produced a number in the artifact below |
| Undeclared minimum zone area | a fixed 3x3 opening enforced 8100 m² at 30 m while the manifest advertised 2500 m² | `effective_minimum_zone_area_m2` computes and publishes the real floor; the repaired default drops the opening so the declared area is the operative one (2700 m² at 30 m) |

Each defect is pinned by a test in `tests/test_release_engine_repair.py` built
from committed artifacts, so none of them can silently return. The repaired
module reproduces all five committed `regime-hindcast-v1` development release
masks **cell-for-cell** at its `V1_FROZEN` configuration, which is what makes
every difference below attributable to a configuration change rather than to a
reimplementation.

### Search design, declared before any score was seen

Every candidate was screened on one predeclared development block (Western
Bernese); the top decile was promoted to all five. The same-area slope-only
baseline was **pinned** to the `regime-hindcast-v1` published slope response
rather than read from the candidate, because a search allowed to move its own
baseline measures nothing. Physical guardrails were vetoes, not tie-breakers: a
benign day had to stay quiet, missing data could never become a flagged cell,
and flagged terrain could not exceed the frozen 20% budget. Stop conditions were
SUCCESS, FUTILITY, BUDGET and PLATEAU, whichever fired first.

128 configurations were evaluated, 0 were rejected by a guardrail, and 13 were
promoted to the full five-block evaluation. PLATEAU fired: the running best
screening margin last improved at configuration 78 and the following 50
configurations did not beat it.

### Result

| Configuration | W. Bernese | Albula | Glarus | Gotthard | Silvretta | Worst block |
|---|---:|---:|---:|---:|---:|---:|
| Frozen v1 engine | −13.12 | −14.84 | −24.94 | −9.96 | −21.89 | −24.94 |
| Repaired morphology only | −9.57 | −17.58 | −24.94 | −10.37 | −21.89 | −24.94 |
| Best searched configuration | **+1.77** | **+2.20** | −9.24 | **+9.54** | −4.14 | −9.24 |

Margins are percentage points of release-only event capture against the
same-area slope-only baseline. The best configuration beat the baseline on three
of five blocks and lost on two. The rule required at least +5 points on all
five, so it failed. The rule was not weakened after the score was seen, and the
reserved block was not spent.

### Why the slope baseline is hard to beat

The best configuration and its same-area slope baseline select almost disjoint
terrain — spatial agreement is 5.2% to 10.1% — at a similar mean slope: the
baseline sits at 40.1–40.4° on every block and the model at 39.4–41.0° on
four of the five. Glarus is the exception at 43.8°, and it is also the
worst-margin block. On the metric the model does better:

| Block | Model coverage of mapped positives | Slope baseline coverage |
|---|---:|---:|
| W. Bernese | 12.00% | 4.47% |
| Albula | 6.53% | 3.23% |
| Glarus | 7.21% | 4.85% |
| Gotthard | 4.18% | 1.83% |
| Silvretta | 5.99% | 3.80% |

At equal terrain budget the repaired model intersects 1.5 to 2.7 times more
mapped-avalanche area than the slope baseline, on every block. It still loses on
event capture, and the two facts are consistent: an event counts as captured
when just 5% of its cells are flagged, so the metric rewards touching many
outlines slightly rather than covering any one of them well. Spreading a fixed
budget thinly across all steep ground is an efficient way to clip many outlines;
concentrating it into coherent, physically-motivated start zones is not.

That is a property of a positive-only capture metric evaluated against outlines
that include track and deposit, not evidence that a slope threshold is a better
release model. It also means this comparison cannot be won by tuning loading
parameters, which is the substantive finding: across 128 configurations spanning
three drift kernels, eight release thresholds, four loading bases, four snow and
four wind weights, three slope responses and three morphologies, none produced a
configuration that beats a slope ranking on all five blocks. The remaining gap is
not parameterisation. It is that the model has no snowpack-stratigraphy or
weak-layer information, which is the variable that separates steep terrain that
released from steep terrain that did not — and no amount of loading-parameter
search can supply it.

That last claim was afterwards tested rather than left standing. It did not
hold: see [the stratigraphy search](#stratigraphy-search-failed-its-success-rule-and-the-effect-ran-backwards)
below, where supplying the predicted variable made the worst-block margin
monotonically worse. The paragraph above is preserved as it was written, before
that test existed.

### Scope

This is a development search. It adds **zero** events to the strict field
holdout, strict N remains 0, `is_validated` remains `false`, and the trusted
dataset identity registry remains empty. All five development blocks were
already scored and viewed in the two frozen experiments, so every number here is
a development number and is labelled as one. No reserved block was predicted,
scored, or had its outlines opened; `row1col4`, `row2col4`, `row6col9` and
`row5col10` all remain sealed. No frozen artifact, spec or digest was modified,
and no email or external message was sent.

## Repaired engine on the ERA5 forcing that produced the zero-release condition (does not transfer)

**Status:** development rescoring of already-burned blocks. The configuration
search above ran entirely on CERRA forcing. This section re-tests the repaired
engine on the ERA5 forcing that produced the original zero-release-zone
failure, and the honest headline is a **failure to transfer, not a repair**: the
configuration selected on CERRA produces **no dry-slab release terrain on any of
the five SPOT blocks**. The committed artifact is
[`release-v2-spot-forcing-v1.json`](../validation-data/results/release-v2-spot-forcing-v1.json).

Nothing here was searched. Four configurations were enumerated before any score
was seen — the frozen v1 mask, the frozen v1 parameters on hourly forcing, the
v2 morphology baseline, and the search winner used byte-for-byte as frozen — and
no parameter was re-tuned for SPOT forcing.

### Provenance: the rebuilt cache reproduces the frozen inputs

`spot-blind-swiss-v1` shipped prediction artifacts but not the per-cell forcing,
so the cache was rebuilt from the same frozen sources. It reproduces the frozen
prediction artifacts' `eligible` mask and slope layer **cell-for-cell** on all
five blocks, and reproduces the frozen scalar storm conditions exactly:

| Block | Frozen new snow | Frozen scalar wind |
|---|---:|---:|
| W. Bernese (dev) | 60.92 cm | 3.57 km/h |
| Albula | 29.28 cm | 5.21 km/h |
| Glarus | 50.52 cm | 5.88 km/h |
| Gotthard | 36.77 cm | 6.43 km/h |
| Silvretta | 33.78 cm | 5.32 km/h |

That is what makes every difference below attributable to the engine rather than
to a rebuilt input.

### Correction: defect 1's stated mechanism does not apply to these blocks

The repair plan diagnosed the wind term as inert because a 72-hour by 9-point
scalar mean dilutes a short windy burst into a calm average. On the SPOT blocks
that mechanism is **not** what happened. There were no windy hours to dilute:

| Block | Mean | p95 | Max single hour | Hours ≥ 15 km/h | Hours ≥ 27.7 km/h |
|---|---:|---:|---:|---:|---:|
| W. Bernese (dev) | 3.57 | 6.50 | 8.1 | 0 | 0 |
| Albula | 5.21 | 9.40 | 10.4 | 0 | 0 |
| Glarus | 5.88 | 10.06 | 11.9 | 0 | 0 |
| Gotthard | 6.43 | 10.10 | 11.6 | 0 | 0 |
| Silvretta | 5.32 | 9.60 | 12.3 | 0 | 0 |

Not one hourly ERA5 value at any of the 45 sample points in any block reaches
either transport threshold — v1's `WIND_TRANSPORT_MIN_KMH` of 15 km/h or v2's
7.7 m/s (27.7 km/h). All four wind statistics the repaired module offers sit
below both on every block, so the choice among them cannot matter, and the
observed transport term is `0.0000` for every configuration on every block. The
forcing repair is correct and it is **inert on this data**: it moves no number
here.

The plan's mechanism does apply on CERRA. Over the same mountains the cached
CERRA forcing field carries a mean of 12.8–22.0 km/h, a maximum of 47.9–59.0
km/h, and 32.8–68.2% of hours at or above 15 km/h (8.1–28.8% at or above 27.7
km/h) — the windows are not the same length, 264 hours against 72, but the
contrast is not a windowing artefact. That difference is the whole reason the
CERRA search could trade snow-loading weight for wind weight, and the reason
this forcing cannot follow it.

This corrects a written diagnosis. It is recorded here rather than quietly
amended in the plan.

### The dry-slab zero-zone failure is not repaired on ERA5

`spot-blind-swiss-v1` ran a single release score; `release_v2` is the four-regime
engine. Only the dry-slab footprint is comparable to the frozen mask, and only
the dry-slab pathway is touched by the three documented defects. Flagged cells
in the eligible core:

| Block | Frozen v1 | v1 params, hourly forcing | v2 morphology baseline | Search winner |
|---|---:|---:|---:|---:|
| W. Bernese (dev) | 5710 | 5526 | 6097 | **0** |
| Albula | 0 | 0 | 0 | **0** |
| Glarus | 4441 | 3049 | 3715 | **0** |
| Gotthard | 0 | 0 | 198 | **0** |
| Silvretta | 0 | 0 | 0 | **0** |

The arithmetic says why. With transport at zero the terrain capability a cell
must reach is fixed by new snow alone, and capability is a product of factors
each bounded by 1, so any requirement above 1.0 is unreachable by any terrain:

| Block | v1 params, hourly | v2 baseline | Search winner |
|---|---:|---:|---:|
| W. Bernese (dev) | 0.809 | 0.809 | **1.087** |
| Albula | 1.116 | 1.116 | **1.361** |
| Glarus | 0.822 | 0.822 | **1.087** |
| Gotthard | 0.956 | 0.956 | **1.122** |
| Silvretta | 1.029 | 1.029 | **1.236** |

The searched configuration is unreachable on **all five** blocks. It traded
`snow_loading_weight` 0.6 → 0.4 and `loading_base` 0.20 → 0.10 for wind weight,
which is a good trade on forcing that supplies wind and a strictly losing one on
forcing that supplies none. Zero-release blocks on the dry-slab pathway go 3 →
3 under the v1 parameters, 3 → 2 once the morphology is also repaired, and 3 →
**5** under the searched configuration.

### Union capture improves, but not through the compared pathway

Across all four regimes the picture inverts, and it should not be headlined. At
SPOT's own frozen 10% capture rule the union beats the same-area pinned slope
baseline on four of five blocks:

| Block | Frozen v1 | v1 params, hourly | v2 baseline | Search winner |
|---|---:|---:|---:|---:|
| W. Bernese (dev) | +2.84 | +1.77 | +3.90 | +7.80 |
| Albula | 0.00 | +4.95 | +6.04 | +6.04 |
| Glarus | −0.46 | −5.54 | −4.39 | 0.00 |
| Gotthard | 0.00 | +9.13 | +7.88 | +7.47 |
| Silvretta | 0.00 | +6.51 | +8.28 | +8.28 |

Every one of those flagged cells is `dry_loose` or `wet_snow` — regimes the
frozen SPOT engine did not have at all. The union is not a repair of the failure
this section set out to re-test; it is a different model answering a different
question, and the dry-slab column above is the like-for-like comparison.

The metric also swings. At the configuration search's 5% rule the same searched
configuration scores −8.87 / −8.79 / −14.32 / −1.24 / 0.00 on the same blocks
and the same flagged cells. Both rules were declared before scoring and both are
in the artifact for every block and configuration. The 10% → 5% reversal is a
property of the capture metric, not of the model: a lower overlap threshold
rewards a baseline that spreads its budget thinly across all steep ground.

### Scope

Every SPOT block was predicted, scored and had its outlines opened in
`spot-blind-swiss-v1`, so every number here is a development number. This adds
**zero** events to the strict field holdout, strict N remains 0, `is_validated`
remains `false`, and the trusted dataset identity registry remains empty. No
frozen artifact, spec or digest was modified. No reserved block was predicted,
scored, or had its outlines opened; `row1col4`, `row2col4`, `row6col9` and
`row5col10` all remain sealed. No email or external message was sent.

## Stratigraphy search (failed its success rule, and the effect ran backwards)

**Status:** failed the predeclared success rule — the same +5 percentage-point
rule as the first search, unchanged. The first search concluded in writing that
its remaining gap was not parameterisation but the absence of snowpack
stratigraphy. This search added exactly that variable and changed nothing else,
so the conclusion would be tested rather than restated. **It did not hold in the
direction it predicted.** Giving the reconstructed weak-interface index loading
weight made the worst-block margin monotonically *worse* at every weight tried,
and the best configuration in the whole search is the stratigraphy-free winner
of the first one.

No reserved block was spent; all four remain sealed. The committed artifacts are
the [search result](../validation-data/results/release-stratigraphy-search-v1.json),
its [sweep log](../validation-data/results/release-stratigraphy-search-v1-sweep-log.jsonl),
and the [log of the superseded first execution](../validation-data/results/release-stratigraphy-search-v1-aborted-sweep-log.jsonl).

### What was added

`avycore/snowpack/stratigraphy.py` builds a bounded buried weak-interface index
as a product of three factors, each able to zero the result independently:
formation (the stronger of kinetic-growth faceting hours or cold/calm/dry
surface hours — alternatives, not addends), persistence (decayed by antecedent
positive degree-hours and rain), and burial (gated on storm new snow over a
pre-storm pack). Every constant is a literature value and none is fitted. The
bulk gradient is `(0 °C − T_air_lapsed) / depth` with the denominator clamped at
0.20 m; both that and the lapsed-air-temperature surrogate deliberately
understate the driver.

It is a reconstruction from antecedent surface meteorology and modelled snow
depth. It contains no snow profile, no stability test, no grain-type observation
and no measurement of any buried layer, and it cannot be verified from the data
that produces it.

**Unknown is missing input, never zero.** Without a snow-depth series the
gradient mechanism is unevaluable, and the index says so through a `known` mask.
A configuration with non-zero weight removes such a cell from the dry-slab
admissible set rather than scoring it as though the unmeasured interface were
absent. This is why the term cannot be evaluated at all on the SPOT blocks: the
frozen ERA5 request carries no snow-depth series, while CERRA does.

`release_v2.py` gained flat `weak_*` fields whose weight defaults to **0.0**. At
zero every field is inert, and this is verified rather than asserted:
`release-config-search-v1.json` still replays byte-for-byte from its committed
sweep log against the modified module, to SHA-256
`ec7e65c8f26f1cfc0fcdfff100ecbe241cdddaf5f17a20cea1a590f4fd27d1b8`. That
equivalence is the entire basis on which any difference below is attributed to
stratigraphy. `manifest()` grows a `stratigraphy` section and retracts its
"diagnostic only, zero numerical effect" line **only** at non-zero weight, where
that line would be false.

### Held identical, and what moved

Identical to the first search, on purpose: the acceptance rule, the screening
block, the promotion fraction, the pinned slope baseline, the configuration
budget (200), the compute budget (4 h), the plateau limit (50), the terrain and
benign-day vetoes, and the five already-burned development blocks.

Changed: the space gained the stratigraphy dimensions; a **second** physical
veto was added rather than substituted — a full weak interface carrying **no**
load must still produce no release terrain, because a weak layer is not a hazard
by itself; and the seed is new (20260819) because the sampler draws more values
per configuration and the old stream cannot be continued.

The critical temperature gradient (10 K/m) and the minimum depth in the gradient
estimate were deliberately **not** searched. They are literature regime
boundaries, and fitting a physical threshold to a capture score is how a search
launders a tuned constant into a citation.

### A harness defect, and the run it ended

Both searches sort candidates by snow-state key so each hourly integration
happens once. State-key order has nothing to do with promise, so a plateau
counted across it can stop a run before its own declared anchors are scored. The
first execution of this search did exactly that: it stopped at 66 configurations
having evaluated two of eight declared points and none of the weak-weight
ladder. Its log is committed and its digest is in the artifact, because a run
that happened is a run that gets reported. It also failed the rule.

The repair evaluates the declared anchors and the ladder first and counts the
plateau over the sampled portion only. Nothing else moved — same seed, same
budget, same plateau limit, same acceptance rule, same blocks. That the repaired
harness scores configurations identically is checkable from the logs and from
the table below: `anchor_v1_frozen`, `anchor_v2_baseline` and
`anchor_search_v1_best` reproduce the first search's three published rows
exactly, block for block.

### Result

58 configurations were evaluated, 0 were rejected by a guardrail, and 6 were
promoted to the full five-block evaluation. PLATEAU fired: none of the 50
sampled configurations improved on the best declared screening margin.

| Configuration | Weak weight | W. Bernese | Albula | Glarus | Gotthard | Silvretta | Worst block |
|---|---:|---:|---:|---:|---:|---:|---:|
| Search v1 winner (anchor) | 0.0 | +1.77 | +2.20 | −9.24 | +9.54 | −4.14 | **−9.24** |
| Ladder | 0.2 | 0.00 | 0.00 | −10.85 | +6.64 | −8.28 | −10.85 |
| Ladder | 0.4 | −1.42 | −2.75 | −13.16 | +6.64 | −12.43 | −13.16 |
| Ladder | 0.7 | −3.55 | −9.89 | −15.94 | +4.56 | −13.61 | −15.94 |
| Best sampled configuration | 0.2 | −6.74 | −8.79 | −16.86 | −1.24 | −14.20 | −16.86 |
| Ladder | 1.0 | −7.45 | −12.09 | −18.71 | +4.56 | −14.79 | −18.71 |
| Ladder | 1.5 | −13.12 | −22.53 | −18.94 | +4.15 | −14.79 | −22.53 |
| Frozen v1 engine (anchor) | 0.0 | −13.12 | −14.84 | −24.94 | −9.96 | −21.89 | −24.94 |
| Repaired morphology only (anchor) | 0.0 | −9.57 | −17.58 | −24.94 | −10.37 | −21.89 | −24.94 |

Margins are percentage points of release-only event capture against the
same-area slope-only baseline. The rule required at least +5 on all five blocks;
the best worst-block margin in the search is −9.24, achieved at weak weight
**zero**. The rule was not weakened after the score was seen, the seed was not
re-rolled, and the budget was not widened.

The ladder is the informative slice: holding the first search's winner fixed and
moving only the weak-interface weight, the worst-block margin falls
monotonically −9.24 → −10.85 → −13.16 → −15.94 → −18.71 → −22.53 as the weight
rises 0 → 1.5. The best configuration that uses stratigraphy is 1.62 points
**worse** than the best that uses none.

### What that does and does not mean

The predicted variable was added and the score got worse, monotonically, at
every weight. Three readings are consistent with that and this experiment cannot
separate them:

1. The reconstruction is not the variable. An index built from antecedent
   surface meteorology and a modelled depth may carry too little of what an
   observed weak layer is to help at 30 m resolution.
2. The mechanism is real but the metric cannot see it. Weighting stratigraphy
   concentrates the fixed terrain budget onto fewer, more specific slopes, and a
   5%-overlap positive-only capture rule penalises exactly that — the same
   effect already documented for the first search.
3. Both.

What the result does bound is the first search's written claim. "The remaining
gap is the absence of stratigraphy" is now a tested statement rather than an
inference, and in this formulation, on this forcing, against this metric, it is
**not supported**: supplying a reconstructed stratigraphy term does not close the
gap and does not narrow it. That bounds what this reconstruction can do. It does
not bound what an observed weak layer could do, and no observed weak layer is
available here.

### Scope

This is a development search. All five blocks were already burned in the two
frozen experiments, so every number is a development number. It adds **zero**
events to the strict field holdout, strict N remains 0, `is_validated` remains
`false`, and the trusted dataset identity registry remains empty. Beating or
losing to a slope baseline is not validation. No frozen source, artifact, spec
or digest was modified, and `release-config-search-v1.json` still replays. No
reserved block was predicted, scored, or had its outlines opened. No email or
external message was sent.

## Lower-rigor real-event comparison

**Component tested:** empirical alpha angle plus fast downslope routing.
`mapped-positive coverage` is
`area(predicted ∩ mapped positive) / area(mapped positive)`. Unmapped cells are
unknown, never negatives, so this is not IoU, precision, or a false-positive
rate. `predicted:mapped area` is an extent-scale diagnostic, not precision. The
committed result is
[`validation-data/results/alpha-only-real-events-v1.json`](../validation-data/results/alpha-only-real-events-v1.json).
Its canonical full-payload identity is
`8594712eaec4d26e9d27b31270eb2f2d0402f0f8c10e2bb2fbfaf3d2c58276c0`;
the committed file SHA-256 is
`85ac2849fe38e83b3d745179893b9d597a785323cba36d3ec794dd5a0e564986`.
The artifact also binds the experiment, parameter manifest, engine and runner
source, terrain and observation inputs, valid/release/mapped masks, and every
prediction mask. Dependency versions are regeneration provenance, not evidence
of scientific validity.

Each coverage record was produced by the public
`positive_only_polygon_metrics` path with a `QualitativePredictionContext`, a
committed dataset identity, and the registered observation ID. Missing
historical scenario fields remain explicit; no zero-valued scenario was
invented to pass the stricter calibration or field-validation contracts.

All 32 planned runs executed, remained inside their explicitly buffered AOIs,
and were scoreable on the positive-only path. That statement tests comparison
completeness and boundary handling; it does not establish physical accuracy.

| Event | Frozen analysis grouping | Preassigned partition | Mapped-positive source | Coverage over 32°→19° alpha sweep | Predicted:mapped area over sweep |
|---|---|---|---|---:|---:|
| Wildi | Brämabühl | qualitative calibration | published dense-deposit polygon | 77.04%–98.71% | 1.273×–2.252× |
| Rüchi | Brämabühl | qualitative check | published dense-deposit polygon | 15.64%–77.80% | 0.261×–1.438× |
| SPOT 732 | Stillbergalp sector | qualitative calibration | published whole-avalanche footprint | 16.74%–81.97% | 0.167×–0.820× |
| SPOT 754 | Stillbergalp sector | qualitative check | published whole-avalanche footprint | 46.12%–95.66% | 0.594×–5.795× |
| SPOT 820 | Totalp sector | qualitative calibration | published whole-avalanche footprint | 47.54%–81.97% | 0.492×–1.574× |
| SPOT 837 | Schwarzseealp sector | qualitative calibration | published whole-avalanche footprint | 17.65%–100.00% | 0.176×–19.706× |
| SPOT 840 | Totalp sector | qualitative check | published whole-avalanche footprint | 80.77%–84.62% | 2.615×–24.385× |
| SPOT 1034 | Totalp sector | qualitative check | published whole-avalanche footprint | 70.00%–97.14% | 0.914×–3.486× |

The complete four-event qualitative-check subset changes as follows; each range
is the minimum and maximum across all four check events at that fixed alpha:

| Release-size label | Alpha | Check N | Mapped-positive coverage range | Predicted:mapped area range |
|---|---:|---:|---:|---:|
| small | 32° | 4 | 15.64%–80.77% | 0.261×–2.615× |
| medium | 27° | 4 | 76.10%–87.14% | 1.341×–4.731× |
| large | 23° | 4 | 76.92%–95.71% | 1.390×–13.269× |
| very large | 19° | 4 | 77.80%–97.14% | 1.438×–24.385× |

The sensitivity result does not support a robust match. Lowering alpha generally
increases mapped-positive coverage, but it can increase predicted area far more:
SPOT 840 changes only from 80.77% to 84.62% coverage while its area ratio grows
from 2.615× to 24.385×. Rüchi at 32° reaches only 15.64% of the mapped deposit,
while SPOT 754 reaches 95.66% at 19° only with a 5.795× area ratio. These are
reported misses and equifinality signals for the alpha-plus-routing component,
not candidates for post-hoc parameter selection. No acceptance threshold was
invented after seeing the results.

For the six SPOT events, mapped-positive coverage includes the derived release
cells that initialize routing inside the same whole-avalanche target. Depending
on the event, that fixed seed overlap is 8.20%–19.23% of the target. The artifact
therefore separates seed-cell intersection from intersection reached outside
the release proxy. The sharpest warning is SPOT 837 at 32°: all 17.65% reported
coverage is the six initialized release cells and routed non-release coverage is
0%. Across the full sweep, routed non-release coverage ranges from 0% to 84.02%
for individual SPOT runs. Brämabühl uses distinct published release and deposit
polygons, so its deposit coverage contains no release-cell overlap. This
decomposition prevents seed overlap from being misreported as runout success.

## Frozen GEODAR field-kinematic consistency test

The 48-hour alternative-validation audit was re-evaluated against the engine
that actually exists. Analytic dam-break and similarity solutions require a
depth-averaged continuum solver with flow depth, spreading, and mass balance;
they cannot validate this engine's dimensionless point particles. Published
AvaFrame real-topography reference results use SAMOS-AT variants and can provide
code-to-code intercomparison, not ground truth for this implementation. Positive
avalanche outlines also cannot supply precision, specificity, or critical
success index because unmapped terrain is not observed absence. None of those
shortcuts changes the strict field-validation result.

One immediately executable field test was compatible with the current model.
The public [GEODAR v1 record](https://zenodo.org/records/1042108) supplies paired
processed front-or-major-surge trajectories and measured thalwegs for 71 events
at Vallée de la Sionne. The CC BY 4.0 repository guide defines trajectory time,
surface distance, velocity, and CH03/LV03 coordinates (EPSG:21781), and states a
0.75 m radar range-bin spacing. It does not give a per-trajectory position or
velocity uncertainty. The source is one instrumented path and contains neither
the event-specific release state nor the two-dimensional event-day snow surface
needed to reproduce each avalanche.

Before the first score, the experiment froze the existing open-snow baseline
(`mu=0.2`, `xi=1200 m/s²`), `dynamics_only` mode, one particle, no lateral
jitter, no alpha stopping line, a 0.2 s step, the complete file-pair selection,
three metrics, and all thresholds. The measured thalweg was extruded into a
cross-slope-flat 5 m strip so the test isolated along-path kinematics. All 150
locally acquired source files were checked against their Zenodo sizes and MD5
values; the result additionally records per-file SHA-256. The inputs remain an
external reproducibility dependency and were not committed. The frozen
[experiment specification](../validation-data/experiments/geodar-along-thalweg-v1.json),
[runner](../scripts/validation/run_geodar_along_thalweg_experiment.py), and
[result](../validation-data/results/geodar-along-thalweg-v1.json) preserve the
complete contract and lineage.

The component failed its predeclared criteria:

| Metric | Frozen event threshold | Events passing | Median over 71 events |
|---|---:|---:|---:|
| Velocity NRMSE, normalized by observed peak speed | ≤ 0.10 | 0 | 0.360606639 |
| Relative travel-time RMSE | ≤ 0.15 | 20 | 0.294471793 |
| Terminal surface-distance relative error | ≤ 0.15 | 10 | 0.695451056 |
| All three metrics and no domain/cutoff failure | all required | 0 | 0% pass fraction |

All 71 observations lay completely inside their reconstructed source thalweg,
and no run hit the numerical step cutoff. Nevertheless, the particle left the
available profile in every event rather than stopping within it. This agrees
with the implementation's known limitation: a dimensionless particle has no
flow-depth evolution, spreading/thinning, deposition, or mass-dependent stop.
The hybrid engine can impose an alpha energy-line boundary, but then runout
extent is controlled by that empirical boundary and is not an independent
validation of the Voellmy dynamics.

This is a falsifying component test under declared assumptions, not a strict
event validation. Missing event-specific release speed/depth, snow properties,
entrainment, flow-regime classification, event-day surface, processing
uncertainty, and a second independent mountain prevent geographic or
operational claims. No parameter was tuned after observing the scores. The test
adds zero eligible holdout events, leaves `is_validated=false`, and does not add
any identity to the trusted registry.

### Resolved: the parameter binding now names the subtree it consumed

`implementation.parameter_file_sha256` recorded
`eb95b69fb31da6add188389547bfde6dd6a75c4495cfa07e18ea276c705e21b4` for
`backend/config/m0-baseline.json`. That digest matches **neither committed
version of the file, in either line-ending form**: `d0cc6dd` hashes to
`c136d4bf…` (LF) or `917521d8…` (CRLF), and `4edc0cf` to `f07f36d4…` (LF) or
`b2c858…` (CRLF). Hashing every object in the repository — including dangling
blobs, in raw, LF, and CRLF form — returns no match either. It is therefore not
the line-ending defect above; the digest names a working-tree state that was
never committed, and no byte form of it survives.

The binding that carries the science is intact and does check out. The result
binds its parameters to `backend/config/m0-baseline.json:model.parameter_manifest`,
and that object is **byte-identical across both committed revisions**. Under the
repository's established `parameter_manifest_sha256` encoding
(`json.dumps(value, sort_keys=True)`, the helper in `backend/app/bake_identity.py`
that every other frozen experiment uses) its digest is
`aaafd6f9fc6e8d598cb56c479bcc072374ee94ba17613c0ac2df0594f8059628`, which is also
the value the file itself declares as `model.sha256`. The only thing `4edc0cf`
changed was the file's separate `results` block, refrozen after the particle
coordinate-integration correction. The declared open-snow `mu = 0.2` and
`xi = 1200 m s-2` are unchanged, as are every other scalar the spec drew from the
manifest: `max_steps`, `time_step_s`, `stopping_velocity_ms`, and `random_seed`.

The whole-file digest was the wrong granularity for this binding: it covers a
baseline-results block that legitimately changes without any parameter changing,
and it reported that output refreeze as parameter drift. The record has been
rebound to the manifest subtree it actually names. `implementation` now carries
`parameter_manifest_pointer`, `parameter_manifest_sha256`, and the superseded
whole-file digest verbatim as `parameter_file_sha256_at_run`, alongside a note
stating that it is history and is not re-asserted. The digest was **not** rewritten
to today's file hash — that would assert the 71-event result was produced under
the current M0 baseline, which it was not — and the frozen spec's
`parameter_file_sha256_at_freeze` twin is untouched, so the original claim remains
on the record exactly as made.

`tests/test_geodar_along_thalweg_artifact.py` now checks the subtree digest
against a recomputation from the working tree, cross-checks it against the file's
own declared `model.sha256` so a doctored manifest cannot satisfy it, asserts the
historical digest is still recorded and still does not match today's file, and
verifies every manifest-sourced spec scalar against the live manifest.

One consequence is deliberate and left alone: `run_geodar_along_thalweg_experiment.py`
still guards on the whole-file digest and would refuse to re-run against today's
baseline. The runner was not edited, because `implementation.runner_sha256` binds
this result to the exact source that produced it; changing that source to fix the
guard would break the record of what actually ran. The experiment is archival — it
needs external Zenodo HDF5 inputs that are not in this repository — so the guard is
not on any live path.

## DEM and event-day surface caveats

**Components affected:** alpha-line extent, DEM-driven routing, and, for SPOT,
the derived release proxy.

For Brämabühl, the 1 m DTM is not the event-day snow surface. Applying only the
documented 0.60 m new-snow layer to alpha geometry gives horizontal scales of
0.96, 1.18, 1.41, and 1.74 m at 32°, 27°, 23°, and 19°. These are geometric
translations, not uncertainty bounds. Prior snow, wind drift, erosion, and total
event-day vertical mismatch are unquantified, so event-day terrain error remains
unbounded.

For the SPOT events, the Copernicus GLO-30 input is a digital surface model
acquired during 2011–2015, approximately four to eight years before the 2019
events, and includes vegetation and structures. Its native one-arc-second sample
was bilinearly resampled to 10 m; this does not create 10 m terrain detail. The
[Copernicus DEM Product Handbook v5.0](https://dataspace.copernicus.eu/sites/default/files/media/files/2024-06/geo1988-copernicusdem-spe-002_producthandbook_i5.0.pdf)
specifies absolute horizontal accuracy below 6 m CE90, absolute vertical
accuracy below 4 m LE90 as a global arithmetic-mean specification, and
point-to-point relative vertical accuracy below 4 m LE90 on slopes above 20%.
Translating 4 m vertically through the four alpha angles gives horizontal scales
of 6.40, 7.85, 9.42, and 11.62 m. Those values are not site-specific Davos error
bounds; local DSM, vegetation, and event-day snow mismatch remain unbounded.

## Frozen-digest re-derivation over canonical LF bytes (18 August 2026)

**No scientific number in this report changed.** Every metric, count, threshold,
pass fraction and coordinate is exactly what it was. What changed is a set of
SHA-256 values, and only because they had been frozen over the wrong byte form
of files whose content was never in question.

The repository is authored on Windows with `core.autocrlf=true`, so an unpinned
text file is checked out as CRLF while git stores it as LF. Digests frozen on
such a checkout describe the checkout, not the repository. Commit `4d7e5b6`
pinned `validation-data/**/*.{json,geojson}` to `eol=lf` and thereby exposed the
inconsistency: ten tests began failing because a recorded digest no longer
matched the file it named — in one direction because the file had become LF
while its digest was CRLF-derived, and in the other because the digest was
LF-derived while the file was still delivered as CRLF.

`.gitattributes` now pins the whole repository (`* text=auto eol=lf`, with
`*.npz binary`). A per-file allowlist was rejected: it only ever covers the files
someone remembered to add, and the next frozen digest silently reintroduces the
defect. `tests/test_frozen_identity_line_endings.py` fails if any tracked text
file carries CRLF, if the repository-wide pin is removed, or if any recorded
digest matches the CRLF rendering of a repository file rather than its LF
rendering.

The change is content-neutral, and the neutrality is checked rather than
asserted:

- No committed blob in this repository has ever contained a CR byte. The
  canonical bytes were always LF; only the working tree differed. Normalizing
  252 tracked files to LF produced **zero** staged content change under
  `git add --renormalize`.
- Both qualitative observation GeoJSONs and the candidate-funnel head parse to
  objects identical to their CRLF forms; `risk.py` is the same source text.
- Across the 41 rewritten evidence files, everything outside a 64-hex digest
  token is byte-identical to the committed version, and the number of digest
  tokens per file is unchanged.
- The five SPOT prediction bundles were rewritten only to rebind
  `experiment_spec_sha256`. Every array is bit-identical to the committed
  version, entry order is preserved, and no other metadata field differs. The
  runner computes `prediction_identity_sha256` before attaching the spec digest,
  so that identity is unaffected by construction.
- Derived identities were re-derived by their own published definitions, not
  re-run: `dataset_identity_sha256` from
  `sha256(schema_version \0 manifest \0 observations \0 original_source)`, and
  `artifact_identity_sha256` as the canonical SHA-256 of the result object
  excluding itself. The acquisition artifacts were regenerated by their own
  builders to a fixpoint.

Anyone holding an earlier copy of this report should expect its digests to
differ and its numbers not to.

## Supported conclusion and remaining gaps

**Explicit scope:** No terrain type, mountain, event regime, or scale is
presently field validated. In particular, the engine is not validated for
dry-slab, wet-snow, or powder-flow events.

The particle dynamics have synthetic verification for Coulomb stopping,
Voellmy terminal speed, directional coordinate projection, energy dissipation,
convergence, and deterministic replay. They do **not** have field validation of velocity magnitude, velocity
fields, path shape, lateral spreading, or stopping distance. The real-event
comparison exercises only alpha plus fast routing. The separate blind national
SPOT hindcast exercised deterministic release localization and primary hybrid
runout end to end, but failed its frozen acceptance rule and cannot supply a
positive validation claim.

Entrainment, erosion/deposition mass balance, released-mass or release-depth
sensitivity, impact pressure, wet-snow physics, and the powder-cloud regime are
not implemented or validated by this evidence. The Brämabühl source explicitly
documents a powder component, which was excluded rather than treated as a model
success. A future field claim requires a preassigned independent holdout with
surveyed-domain/known-absence semantics, quantified positional uncertainty,
registered release and deposit or endpoint observations, event-specific scenario
metadata, bounded DEM temporal error, and no AOI escape.
