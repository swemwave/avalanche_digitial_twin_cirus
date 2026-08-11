# Offline SnowStatePack and SNOWPACK boundary

This is an isolated research-prototype integration contract. It is not imported
by the serving application, is not read by the default assessment, and does not
change release scoring, runout, API meaning, frontend behavior, or the slider
baseline.

## Upstream contract

[SNOWPACK](https://snowpack.slf.ch/) is an open-source physical snow-cover model.
The latest official packaged release found in the SLF repository is 3.7.0,
released 2024-04-29 at commit
`349b857af07ddb090b3e7b36fb6a45ec87ec2338`. Its tagged `License.txt` is the GNU
Lesser General Public License version 3 or later. The project advertises Windows, Linux,
and macOS packages. Source, executable, configuration, forcing, output, adapter,
and timeout identities must all be recorded for any real run.

Official input documentation requires air temperature, relative humidity, wind,
precipitation and radiation/energy-boundary information. SMET uses UTC when
`tz=0`; temperature is kelvin, relative humidity is a 0--1 fraction, radiation
is W m-2, pressure is Pa, and `PSUM` is millimetres accumulated over the last
timestep. `PSUM_PH` is liquid fraction from 0 (solid) to 1 (liquid). Flux and
accumulated-energy semantics are not interchangeable. See the official
[data requirements](https://snowpack.slf.ch/doc-release/html/requirements.html),
[data-format guide](https://snowpack.slf.ch/doc-release/html/snowpackio.html), and
[SMET specification](https://meteoio.slf.ch/doc-release/SMET_specifications.pdf).

## Implemented inactive contracts

- `packages/avycore/src/avycore/snow/contracts.py` defines strict, immutable,
  content-addressed SnowStatePack v2 output for snow height, SWE and surface
  temperature. Modeled and missing values are distinct; missing remains null and
  masked. Every output binds ConditionPack, bake, reference elevation, forcing,
  executable, complete adjacent-DLL inventory, complete model-input inventory,
  configuration, adapter, output parser, command, timeout and process hashes.
  The full artifact identity preserves raw output and process-log changes, while
  a separate scientific-replay identity excludes that run noise and binds the
  normalized physical output. Model output is never labelled observed or
  validated. This limited contract does not yet contain stratigraphy, density,
  liquid water, grain type, weak layers or stability diagnostics required by the
  M3 acceptance target.
- `backend/app/processing/snow/smet.py` converts a complete single-station hourly
  ConditionPack to deterministic `SMET 1.1 ASCII`. It converts RH percent to
  fraction, writes hourly kg m-2 h-1 as numerically equivalent millimetres over
  that one-hour interval, and maps explicit phase to liquid fraction. Unknown
  phase, a gap-filled value, discontinuity, missing input, invalid coordinate,
  or unit conflict aborts the adapter. It performs no interpolation or generator
  fallback.
- `backend/app/processing/snow/execution.py` runs an explicitly supplied
  executable in a disposable directory with version capture, executable hash,
  argv, timeout, stderr/stdout capture, safe paths, exit checks and output-size
  bounds. Tests cover deterministic output, failure, timeout, absent and corrupt
  output.
- `backend/app/processing/snow/run_evidence.py` derives evidence rather than
  accepting caller-asserted hashes. It inventories the executable and every
  adjacent project-local runtime DLL, requires configuration, forcing, initial
  state and site-parameter files to be non-empty and assigned exactly once,
  records a per-role and complete input inventory, rejects host-specific argv,
  and verifies complete hourly UTC forcing without nodata. Raw-run identity and
  normalized model-input replay identity remain separate.
- `backend/app/processing/snow/storage.py` atomically stores and revalidates a
  SnowStatePack under its content identity. Synthetic fixtures are software
  verification only and are not SNOWPACK results.
- `backend/app/processing/snow/snowpack_output.py` strictly parses real
  SNOWPACK 3.7.0 SMET time-series output. It applies each declared
  `units_multiplier` and `units_offset`, converts declared local timestamps to
  UTC, preserves nodata as missing, and maps only the real `HS_mod`, `SWE`, and
  `TSS_mod` fields into SnowStatePack v2 semantics. Raw-output SHA-256 remains
  separate from a normalized physical-output SHA-256 that excludes only the
  run-stamped creator/date/history headers.
- `backend/app/processing/snow/official_example.py` checks the exact pinned git
  commit, copies the committed example blobs without modification, runs the
  official `res1exp` command, applies the official smoke failure expression,
  parses its real SMET output, and atomically preserves inputs, raw outputs,
  stdout/stderr, report and checksums under `runtime/reports/snowpack/`.

The official Windows 3.7.0 package is installed project-locally for offline use.
`snowpack.exe` has SHA-256
`9400a6b50b5fdb716c4fc4f649c7772e36d0ddf0279b0716ca867cf47df82896` and reports
SNOWPACK 3.7.0, libsnowpack 3.7.0 and MeteoIO 2.11.0. The derived binary-closure
inventory SHA-256 is
`f780a8d6873555b22e414f345164cd91a1067d54d04590eeb6e4d58f1e6a2754`; it binds
the executable plus adjacent `libgcc_s_seh-1.dll`, `libmeteoio.dll`,
`libsnowpack.dll`, `libstdc++-6.dll` and `libwinpthread-1.dll`, including each
filename, byte count and file hash. Two independently
preserved unchanged-example runs pass the official smoke condition. Their raw
SMET hashes differ because the official writer stamps run history, while their
normalized physical-output hash is identically
`ef6c1ce7d0f260db67d495350a830eb994e7507b5e0823b71c21322305b1a549`.
This is executable/parser reproducibility evidence, not a Mount Hosmer result.

The pinned MeteoIO 2.11 example uses the older
`[Input] PSUM_PH::create = PRECSPLITTING` syntax, not the newer unverified
`[Generators] ... RANGE` lead. The project retains a single phase-ownership
boundary: the ConditionPack adapter supplies explicit `PSUM_PH`, so a custom
configuration must not also derive phase through PRECSPLITTING.

## Current blockers and non-activation

The authoritative ECCC ConditionPack has no shortwave or longwave values and
three other hours contain missing source records. The adapter therefore rejects
it at the first missing required hour; it does not write nodata as runnable
forcing or derive radiation. A characterized ERA5-Land interval adapter and
credential-free request now exist, but acquisition cannot occur until the user
manually accepts the CDS dataset terms and provides local CDS credentials; no
returned grid coordinates, elevation, `expver`, or complete forcing therefore
exists.

The executable and real-output parser gates pass only for the unchanged official
example. A custom run remains blocked independently by unproven initial snow and
soil state, ground boundary, roughness, canopy/open classification, and the
terrain-versus-grid elevation relationship. Swiss example values are not Hosmer
evidence. Current-season BC observation resources also lack a defensible
per-value historical QC and revision/finality contract, so values were not
downloaded and no station comparison is eligible. Consequently no custom
SNOWPACK execution, real SnowStatePack, Mount Hosmer experiment, calibration,
validation, or improved-accuracy claim is made.
