# Condition Pack contract

Condition Pack schema `mount-hosmer-condition-pack-v1` is the immutable,
provider-neutral hourly forcing interface introduced by scientific milestone M1.
It is an offline replay contract, not a live feed and not an operational
avalanche forecast.

## Contract

The canonical schema is `avycore.conditions.ConditionPack`. Unknown fields are
rejected and model instances are frozen. Every pack binds:

- the Mountain Pack and analysis-grid SHA-256 identities, CRS, axis order,
  metre units, dimensions, and resolution;
- source, citation, licence, permitted use, immutable source-file SHA-256 values,
  and a versioned normalization-code identity;
- UTC acquisition, publication, validity, and staleness-reference times;
- station longitude/latitude/elevation and coordinate uncertainty;
- exactly one value for every declared station and UTC hour for air temperature,
  relative humidity, wind speed, meteorological wind direction, precipitation
  phase and amount, surface pressure, shortwave radiation, and longwave
  radiation;
- explicit canonical units, direct-source or documented derivation metadata,
  per-value status, QC flags, mask, uncertainty, staleness, and source-record /
  transformation lineage; and
- limitations that travel with the forcing snapshot.

The canonical units are K, %, m s-1, meteorological degrees true, a controlled
precipitation-phase category, kg m-2 h-1 liquid-water equivalent, Pa, and W m-2.
Conversion is explicit; provider adapters cannot ask the contract to guess a
unit. A missing source value remains JSON `null` with `masked=true` and
`status="missing"`. A gap-filled value is numerical only when it is labelled
`gap_filled` and cites a declared transformation.

Each required variable must be present. A derived series must identify its input
variables, method, version, and citation. The contract does not silently derive
radiation, precipitation phase, or any other missing forcing.

## Identity and replay

`normalized_output_sha256` is SHA-256 over canonical JSON for all normalized
scientific content, excluding the two self-identifying fields. `condition_id` is
`condition-<normalized_output_sha256>`. Canonical JSON sorts object keys and
requires station/time ordering, so replaying the same immutable snapshot through
the same normalization produces the same identity and bytes. Changing a value,
mask, status, QC flag, uncertainty, time, unit, lineage record, source hash,
normalization version, grid, or limitation changes the identity.

The backend is the only storage owner. It writes a completed pack and checksum
manifest to a sibling staging directory, validates them, then atomically renames
the directory to:

```text
runtime/baked/conditions/<condition_id>/
  condition-pack.json
  checksums.json
```

An identical replay is idempotent. Existing corrupt or identity-conflicting
content is rejected rather than overwritten, and a failed promotion removes its
unpublished staging directory. Provider implementations belong under
`backend/app/processing/conditions/`; `avycore` contains no provider assumptions,
network client, or filesystem writer. M1 intentionally includes no live provider.

Validate a stored directory or standalone pack JSON with:

```powershell
python -m app.cli validate-condition-pack <path>
```

Validation verifies strict schema rules and content-derived identity. Directory
validation additionally verifies the stored file checksum and directory name.

## M2 ECCC historical-hourly provider

The first provider implementation is an offline adapter for the ECCC Historical
Hourly Climate Data collection. It is outside `avycore` at
`backend/app/processing/conditions/eccc.py`; the serving application neither
imports the adapter nor reads its raw sources. The source is governed by the
[ECCC Data Servers End-use Licence v2.1](https://eccc-msc.github.io/open-data/licence/readme_en/),
which permits use and redistribution subject to attribution, third-party
originator attribution where applicable, and no-endorsement terms. Variable
definitions and units follow the official
[historical-hourly technical documentation](https://www.canada.ca/en/environment-climate-change/services/climate-change/canadian-centre-climate-services/display-download/technical-documentation-hourly-data.html).

The explicit offline command imports station and hourly CSV files into an
immutable source cache, verifies hashes, ranks stations, writes a deterministic
forcing-quality report, normalizes twice to prove byte replay, and atomically
writes the resulting pack. Import is used once for provider-named source files:

```powershell
python -m app.cli replay-eccc-conditions `
  --stations <climate-stations.csv> `
  --hourly <climate-hourly.csv> `
  --start 2025-11-01T00:00:00Z `
  --end 2026-05-31T23:00:00Z `
  --target-elevation-m <explicit-reference-elevation>
```

Subsequent replay uses the immutable cache manifest directly. It does not
re-import the canonical cache filenames because original provider filenames and
acquisition-time evidence are part of the source-snapshot identity:

```powershell
python -m app.cli replay-eccc-conditions `
  --snapshot runtime/sources/conditions/eccc/<snapshot_id> `
  --start 2025-11-01T00:00:00Z `
  --end 2026-05-31T23:00:00Z `
  --target-elevation-m <explicit-reference-elevation>
```

The command rejects any source snapshot, source CSV, or output root inside the
configured read-only `DATA/` tree. Cache loading rejects altered provider,
licence, source, acquisition/publication-evidence, file-lineage, checksum, or
unexpected manifest fields.

Generated products are separated by role:

```text
runtime/sources/conditions/eccc/<snapshot_id>/   immutable raw cache
runtime/reports/conditions/eccc/                 derived quality reports
runtime/baked/conditions/<condition_id>/         serving-safe normalized packs
```

Station selection is a deterministic lexicographic ranking over direct-variable
availability, ECCC operation, missing fraction, elevation difference, horizontal
distance, and station ID. It reports every criterion, current metadata dates,
provider flags, nearby station IDs that may indicate relocation or network
changes, and a separate withheld-station comparison with explicit source units.
The quality report distinguishes the recommended station from the station
actually normalized. An explicit override does not change the ranking, but its
coverage and withheld comparison describe the override so the report remains
bound to the generated forcing.

`UTC_DATE` is interpreted only as UTC and must fall on an exact hour. Exact
duplicate rows are counted and deduplicated; conflicting duplicates/revisions
are rejected. ECCC `M` values and absent records remain null/masked. Other
element flags are retained as suspect rather than assigned a universal meaning
that ECCC does not document for every element. Zero precipitation remains an
observed zero. Calm wind direction (`0` in the source) becomes missing direction,
because it is an undefined direction rather than north.

Conversions are versioned: degrees Celsius to kelvin, kilometres per hour to
metres per second, tens of true degrees to meteorological degrees true,
kilopascals to pascals, and millimetres of hourly water equivalent to kilograms
per square metre per hour. No precipitation undercatch, wind, temporal, or
cross-station correction is applied. Temperature is transferred to the required
caller-supplied elevation using a fixed 6.5 K/km environmental lapse rate. Its
sign, units, bounds, reference sensitivity, and effect on the ECCC/NAV CANADA
station disagreement are now characterized, but the globally representative
rate is not validated at Mount Hosmer and remains especially uncertain across a
large valley-to-ridge difference or inversion. The pack keeps that uncertainty
unquantified and records both elevations in the transformation.

Precipitation phase uses an air-temperature uncertainty band: snow at or below
0 degrees C, rain at or above 2 degrees C, and mixed between those thresholds.
This is a deliberately coarse dual-threshold method consistent with published
evidence that rain/snow transition occupies a range rather than a single exact
temperature ([Alcott et al., 2012](https://doi.org/10.1175/JCLI-D-11-00084.1)).
It is not an observed phase and lacks a vertical temperature profile. Shortwave
and longwave radiation are not supplied by the selected ECCC collection and
remain explicitly missing for every hour.

Source-file mtimes are retained only as local acquisition-time evidence when the
upstream downloader manifest lacks an acquisition timestamp. Because ECCC does
not provide a publication timestamp in these CSVs, acquisition end is stored as
a conservative latest-publication bound and is explicitly documented as such;
it is not an asserted exact publication time.

The characterized full-winter source identity is
`snapshot-fdcd2d7b699eab9e4e4355c3b2b831b2e0adb46f83fbc8f17e8fc69e256174ef`.
Its station and hourly SHA-256 values are respectively
`96c270e6eea1eb06702f0df0341fbf33086aa642866ce830d11f21da7561b2aa` and
`0e336e718a7b8a268efdc9b876a0cef5b4b16edf17db8e85ef41c519f09942fe`.
The previously generated `condition-f5932933213d4772f2c095c2e634cc2f3df1be0dc753acca1e806c89c0a1aebe`
remains a valid pack produced by its embedded normalizer hash. Normalizer changes
produce a new content identity even when normalized values and masks are
numerically unchanged; comparisons must characterize that distinction. The
current pack is
`condition-9d79db2c7998a15d4069e0584892d882417a54ff3dbc3bf780c83a058edfd284`.
Both packs have the identical variables-only scientific-series SHA-256
`56a00c1ce69d4362b134da070559e8ecd3cac6de904b996d6065266ac7f64e0b`.
The identity change is normalizer-code lineage only, not a numerical change or
evidence of improved accuracy.

## M2 PCIC independent station-provider adapter

The second offline adapter is `backend/app/processing/conditions/pcic.py`. It
uses PCIC/PCDS as the distribution portal while preserving the original
observing organization and station history. PCIC portal ownership is not used
as evidence of independence. The deterministically selected history is
`ENV-AQN/585`, PCIC internal station `13010`, history `14942`: the original
station is Teck Coal Limited - Greenhills Operations station `E290310`, Elkford
Rocky Mountain Elementary School.

The fixed PCIC candidate audit excludes ECCC/`EC_raw`, ECCC 1157631 copies,
`Sparwood (EC)`, unknown or ambiguous histories, and non-redistributable
records. It records Goathaven and Elko (BC Wildfire Service), Morrissey Ridge
2C09Q and Fernie 2C21P (BC Hydro), Ministry of Transportation, automated snow,
air-quality, and agriculture histories. BC Wildfire and PAWS observations are
Access Only, and the PCIC-distributed BC Hydro observation records do not grant
the required reusable observation licence. The separate direct B.C. current-
season snow dataset is OGL-BC and overlaps the exact winter, but its `2C09Q`
column cannot be joined to PCIC's `BCH/MOR` history 2885. BC Hydro's current
station table maps `MOR` to `2C09P`, not `2C09Q`; PCIC retains MOR/2885,
2C09P/2950, and 2C09Q/2951 as separate station histories with different
elevations and point/sum semantics. No date-effective move/configuration record
or current per-value QC/revision contract resolves the conflict, so no values
were downloaded, normalized, or merged. Among the two OGL-eligible ENV-AQN histories,
station 585 has 2694 observed overlap hours versus 2576 for station 551, so it
ranks first before variable count, elevation, or distance.

The exact provincial dataset record is
[Air Quality and Climate Monitoring: Unverified Hourly Air Quality and Meteorological Data](https://catalogue.data.gov.bc.ca/dataset/air-quality-and-climate-monitoring-unverified-hourly-air-quality-and-meteorological-data),
which explicitly specifies [OGL-BC 2.0](https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/open-data/open-government-licence-bc).
The cache retains that record, the required attribution, PCIC network/station/
variable metadata, the original Teck operator/station evidence, the clipped ZIP,
source URLs, acquisition bounds, sizes, SHA-256 values, PCIC terms/disclaimer,
and the CRMP agreement version. The observation ZIP is also checked for its
exact entry set, path safety, per-entry hashes, and the 25 MiB compressed / 100
MiB expanded limits. The characterized source identity is
`snapshot-d2156798001f4f40ae0fe8e34d8b02bf6fd530cc02f4c5861d7fb29f12e085b6`.

One acquisition is explicit and offline:

```powershell
python -m app.cli replay-pcic-conditions `
  --acquire `
  --start 2025-11-01T00:00:00Z `
  --end 2026-05-31T23:00:00Z `
  --target-elevation-m 2496.78 `
  --eccc-condition-pack runtime/baked/conditions/<eccc_condition_id>
```

Every subsequent run is cache-native:

```powershell
python -m app.cli replay-pcic-conditions `
  --snapshot runtime/sources/conditions/pcic/<snapshot_id> `
  --start 2025-11-01T00:00:00Z `
  --end 2026-05-31T23:00:00Z `
  --target-elevation-m 2496.78 `
  --eccc-condition-pack runtime/baked/conditions/<eccc_condition_id>
```

The selected ZIP exposes only point wind speed (`m/s`) and wind direction
(`degree`) during this winter. They normalize to `m s-1` and meteorological
`degree_true`; a direction paired with zero wind speed is masked as calm. The
aggregate CSV has no per-observation PCIC QC or revision field, so that absence
and the provider's unverified/preliminary revision status are explicit. Exact
duplicate rows are counted; conflicting duplicates or source-variable
revisions are rejected. Missing hours and every unsupported variable remain
null/masked. There is no gap filling, provider merging, cumulative-to-hourly
precipitation conversion, temperature/elevation transfer, wind correction, or
local correction.

The comparison pairs only exact UTC timestamps from the selected PCIC pack and
ECCC 1157631 pack. It reports station/history/original-organization identity,
source and canonical units, missing and QC counts, distance and elevation
difference, overlap, ECCC-minus-PCIC bias, MAE, and RMSE for each comparable
variable. Wind direction uses shortest signed angular differences and a
circular-mean bias. Reports are stored below
`runtime/reports/conditions/pcic/`; they characterize independent
source-provider disagreement, not validation or calibration, and are not
merged into serving-time conditions.

## M2 forcing characterization report

`backend/app/processing/conditions/characterization.py` builds a separate,
strict `m2-forcing-characterization-v1` report. It is diagnostic and
non-activating: it does not edit a ConditionPack, infer serving-time conditions,
select a snow source, fill a gap, or enable a precipitation, wind, radiation, or
reference-elevation correction. The command reads only immutable condition
caches/packs and baked runtime artifacts, builds the report twice, requires
byte-identical replay, and publishes it atomically under its content-derived ID:

```powershell
python -m app.cli characterize-m2-forcing `
  --eccc-snapshot runtime/sources/conditions/eccc/<snapshot_id> `
  --eccc-condition-pack runtime/baked/conditions/<eccc_condition_id> `
  --pcic-condition-pack runtime/baked/conditions/<pcic_condition_id> `
  --start 2025-11-01T00:00:00Z `
  --end 2026-05-31T23:00:00Z `
  --target-longitude-deg -115.01138889 `
  --target-latitude-deg 49.61361111 `
  --target-elevation-m 2496.78
```

```text
runtime/reports/conditions/m2/<characterization_id>/
  report.json
  checksums.json
```

The report inverts the baked grid-to-WGS84 control lattice without `pyproj`,
records geographic `(longitude, latitude)`, projected `(easting, northing)`, and
raster `[row, col]` order, and preserves every required elevation mask. The
2496.78 m reference has no recorded derivation; it matches the value at
`array[height//2, width//2]` rounded to 0.01 m. At the requested coordinate, the
containing/nearest cell centre is 2500.3828125 m and the valid four-cell
bilinear value is 2499.3645924 m. A controlled current-code rebuild produced
bake `9204d5e427f3009c7b725a211171554fac1952712f4e8db53af6277d8182b4b5`;
all eight scientific/provenance arrays are byte-identical to the preserved bake,
so this is a processing/pack/model lineage change with zero terrain-value or
mask change. The deterministic comparison is
`bake-comparison-39d39c3e3a0fed58b42750448496be8a4295b6df7a371a2692e7169d1d031146`;
tiles, imagery, and all 16 ConditionPack files are also byte-identical. The sole
source-record removal is the legacy, no-longer-interpreted
`metadata/grid_and_aoi.json`; the 22 active source paths and hashes are unchanged.
The bake now passes `python -m app.check_bake`, but the alternative
reference remains inactive because migration requires a separate decision and
the bake-wide vertical datum remains unknown/mixed.

Revision `m2-forcing-characterization-v1.1` added strict nested scientific
validation, a 2.5--7.5 K/km literature-supported lapse-rate sensitivity sweep,
calm-wind direction thresholds, all-layer mask counts, elevation/slope/curvature
distributions, slope bins, aspect sectors, binary forest fractions, provenance
coverage, and target four-cell terrain footprints. Curvature is not relabelled
as exposure, and binary forest is not relabelled as canopy height. Lower
same-provider disagreement after a transfer is not validation. It
also records why the direct OGL-BC Morrissey Ridge SWE lane is blocked on station
history identity and why radiation remains masked rather than fabricated.

Revision `m2-forcing-characterization-v1.2` preserves v1.1 as loadable evidence
and adds an authoritative metadata-only blocker audit. The PCIC catalogue has
no shortwave or longwave history spanning the selected winter and no history
with both components after 2020-07-01. ECCC's specialized archive defines solar
and incident-longwave elements in accumulated-energy units and local apparent
solar time, but no representative exact-window station was identified. The
ERA5-Land hourly time-series product is an unselected modelled candidate with
both components, exact-window catalogue coverage and CC-BY-4.0 reuse; it has not
been acquired or scientifically evaluated because CDS requires a user account,
personal token and manual dataset-terms acceptance. Its J/m2 interval-energy
contract would require an explicit product adapter and may not be relabelled as
W/m2. No correction or gap fill is activated.

The snow audit separately records the Province's current inactive 2C09P and
active 2C09Q locations and the PCIC MOR/2885, 2C09P/2950 and 2C09Q/2951 history
periods, elevations and interval semantics. These sources still provide no
date-effective move/configuration boundary or defensible per-value QC/revision
contract for a join. A BC Hydro link unexpectedly returned a live current-value
response during the metadata audit; it was not saved, normalized, joined or
used, and no bulk observation resource was downloaded.

The current strict report is
`characterization-dbb6696c5f2c4ab8439055eb20514b8dd8e6710c56e74748a27749e91d5194f5`;
its canonical `report.json` SHA-256 is
`7f23511e2822cbcf7d8d42cc5cba3a5503e28f4b0d3b396ce70ebb2b5d09266d`.

## Reference-elevation contract

`derive-reference-elevation` publishes a provider-neutral
`reference-elevation-contract-v1` under
`runtime/reports/terrain/reference-elevations/<reference_elevation_id>/`. It
binds the compatible bake, Mountain Pack, processing and source identities,
layer hashes, longitude/latitude and easting/northing order, affine and cell
conventions, interpolation footprint, masks, units, uncertainty limitations,
legacy value, inactive migration state, and code hash. First/last cell centres,
internal-edge ties, nodata, interpolation, coordinate order, file/hash
corruption, and atomic byte-identical replay are tested. The current contract is
`reference-elevation-bb49fe57ce29dfc856de0b77d4d55487a98308810bbcdbe01e8ef78863be0af6`.
It proposes 2499.3645924 m and preserves 2496.78 m as
`legacy_pre_contract_reference_elevation`; it does not activate the proposal.

The isolated offline snow-model boundary is documented in
[`snow-state-packs.md`](snow-state-packs.md). It does not change ConditionPack,
serving, assessment, or frontend behavior.

## ERA5-Land full-product request and transformation contract

`backend/app/processing/conditions/era5_land.py` defines the offline
`era5-land-full-grib-hourly-v1` contract. It freezes credential-free monthly CDS
requests, audits only credential presence, and atomically stores a request under
`runtime/sources/conditions/era5-land/request-<sha256>/`. It does not import
`cdsapi`, ecCodes, or GRIB tooling and is never part of serving imports.

The contract is specific to the full hourly GRIB product's daily short-forecast
accumulations. At 01 UTC, step 1 is the interval value. At all other validity
hours, including 00 UTC (the previous forecast's step 24), the interval is the
current accumulation minus the exact preceding validity hour. Missing current
or boundary input remains masked. Negative increments are rejected except for a
versioned tiny roundoff tolerance, which is set to zero with an explicit status.
Complete 01--00 UTC interval groups must reconstruct step 24. Precipitation is
converted from m to mm and interval radiation from J m-2 to mean W m-2 by the
declared interval duration.

Relative humidity uses the ECMWF IFS saturation-vapour-pressure-over-water
method and is rejected, not clipped, outside [0,1]. Original temperature,
dewpoint and u/v components must remain in source lineage. Scalar wind direction
is meteorological direction-from degrees true; calm direction remains undefined.
Grid coordinates, geopotential-derived grid elevation, and `expver` must come
from the returned product and cannot be populated from rounded request points.

The frozen request for 2025-07-01 through 2026-05-31 is
`request-fd8365df1bd77c5ac5e61abb7425f35f933acf6cf51305e87bf01122f996e44a`.
It is a request contract only: the environment has neither CDS credentials nor
`cdsapi`, no retrieval has occurred, and it is not a ConditionPack.

## Scientific limitation

A valid Condition Pack proves schema conformance, lineage, and deterministic
software replay only. It does not establish provider representativeness,
measurement accuracy, snow-state accuracy, avalanche calibration, or current
conditions. Those require later milestones and eligible validation evidence.
