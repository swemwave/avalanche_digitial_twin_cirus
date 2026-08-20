# Offline pipeline and prediction products

The durable contract for the staged offline pipeline, the immutable products it
writes, and the read-only API over them.

**Related:** [`architecture.md`](architecture.md) · [`limitations.md`](limitations.md) ·
[`runout-engines.md`](runout-engines.md) · [`../../AGENTS.md`](../../AGENTS.md).

This is an experimental research pipeline. Nothing it produces is an operational
avalanche forecast, a probability, or a calibrated result.

---

## 1. Stages

```text
Mountain/Terrain Pack
   -> Condition Pack            (immutable hourly forcing)
   -> Snow State Pack           (SNOWPACK, offline subprocess)
   -> Normalized Release Result
   -> Normalized Runout Result  (one per engine, isolated subprocess)
   -> Engine Comparison Result
   -> Prediction Product
```

`python -m app.pipeline` is the single entry point. Every stage validates its
complete inputs before it runs and publishes a `StageRecord` saying what it did:

| status | meaning |
|---|---|
| `completed` | The stage ran and published a result identity and artifact root. |
| `skipped` | The stage was deliberately not requested for this run. |
| `unavailable` | A required input, engine, or reviewed parameter does not exist. |
| `failed` | Something that should have worked did not. |

A `completed` record must carry a result identity; every other status must not.
**A stage that produced nothing is published as a named, reasoned absence — never
as a zero, a default, or a silently omitted field.**

## 2. Commands

```powershell
# Validate inputs and probe engines without running any physics.
python -m app.pipeline run --case synthetic --dry-run

# Run both engines from one normalized release and compare them.
python -m app.pipeline run --case synthetic `
  --engines runout.avaframe_com1dfa,runout.avaframe_flowpy `
  --avaframe-python .venv-avaframe\Scripts\python.exe `
  --seed 12345

# Add the bounded deterministic sensitivity sweeps.
python -m app.pipeline run --case synthetic --ensemble `
  --avaframe-python .venv-avaframe\Scripts\python.exe

# Reuse a stored engine bundle instead of re-executing it, when the request,
# adapter and engine identity all match. The published product is identical.
python -m app.pipeline run --case synthetic --ensemble --resume `
  --avaframe-python .venv-avaframe\Scripts\python.exe

# Probe the standalone Flow-Py identity against a checkout. It is expected to
# fail closed; see runout-engines.md for why no upstream release is drivable.
python -m app.pipeline run --case synthetic --dry-run `
  --engines runout.flowpy_upstream --flowpy-checkout <path-to-FlowPy-checkout>

python -m app.pipeline list
```

Behaviour that is part of the contract:

- **No implicit fallback.** Every requested engine is probed *before* any stage
  does work. An unavailable engine ends the run; it is never replaced by whichever
  engine happens to be installed.
- **Explicit engine selection.** `--engines` names exactly which runout engines
  run, in order.
- **Deterministic seeds.** `--seed` is bound into the run configuration and into
  every engine result identity.
- **Stage-specific failure reporting.** A `PipelineError` names the stage it came
  from, and the CLI prints `{"error": {"stage": ..., "message": ...}}` on stderr
  with exit code 2.
- **Dry-run/input validation.** `--dry-run` reports engine availability, the
  resolved configuration, and the condition-stage verdict without running physics.
- **Idempotent publication against verified artifacts.** A completed product is
  content addressed, so re-running an identical configuration re-derives the same
  `product_id`; the existing directory is revalidated and returned rather than
  rebuilt or overwritten, and a conflicting identity is an error rather than a
  silent replacement.
- **Opt-in input-keyed stage reuse.** Without `--resume` every requested engine
  executes, every time. With it, an engine run is replaced by a stored bundle
  only when a key over the *complete* identity matches — see §8.
- **`--case mount-hosmer` is refused.** A real-site case requires an eligible Snow
  State Pack and reviewed release thickness, density, and friction parameters.
  None exists, and the pipeline will not substitute synthetic values for a real
  site.

## 3. Where products live, and why not in `runtime\baked\`

```text
runtime\
├── baked\                        the served terrain surface (python -m app.bake)
├── stage-cache\runout\<sha256>\  --resume only; never read by the serving app
└── predictions\
    └── prediction-product-<sha256>\
        ├── prediction-product.json   the contract document
        ├── checksums.json            SHA-256 of every other file
        ├── configuration.json        the run configuration, minus disposable paths
        ├── release\                  the normalized release bundle
        ├── runouts\<engine_id>\      one normalized bundle per engine
        ├── comparisons\<comparison_id>\
        └── ensembles\<engine_id>\<parameter>\   members and the outer envelope
```

Products are a **sibling generated root, not a child of `runtime\baked\`.** The
bake validates `baked\` against `meta.json` — schema, layer set, file sizes, layer
checksums and a bake identity — and `python -m app.bake --force` builds a complete
staging directory and then replaces that whole tree atomically. A product stored
inside it would be destroyed by an unrelated terrain rebuild and would break the
bake's own checksum contract, since the bake does not know about product files.
A sibling root gives products their own lifecycle while preserving the serving
rule: the application still reads only immutable generated artifacts, never
`DATA\`, and never a mutable cache.

`app.predictions` is the runtime-safe reader. It imports pydantic and the standard
library and nothing else, so serving a product never pulls rasterio, pyproj,
AvaFrame, or SNOWPACK into a request. It refuses a product whose directory name
does not match its content identity, and `verify_prediction_product` additionally
re-checks every stored file against `checksums.json` and rejects both missing and
unexpected files.

## 4. Identity and replay

`product_id` is `prediction-product-<sha256>` over the canonical JSON of the whole
product except that field. Every stage result inside it carries its own content
identity, so changing any value, mask, unit, engine version, configuration,
seed, warning, or limitation changes the product identity.

Absolute paths are deliberately excluded from the configuration hash: they are
disposable, and including them would make an identical run on another machine look
like a different product. Verified replay on the development machine: running the
same configuration twice produced the identical `product_id`, and the second run's
directory was accepted as byte-compatible rather than rewritten.

## 5. Bounded sensitivity ensembles

`--ensemble` runs a deterministic sweep of each declared parameter per engine
around its central value, re-using the central member rather than re-running it:

| engine | parameter | varies | sweep | basis |
|---|---|---|---|---|
| `runout.avaframe_flowpy` | `alpha_angle` | engine parameter | central ±3° | literature |
| `runout.avaframe_flowpy` | `release_extent_offset` | release input | central ±5 m | numerical |
| `runout.avaframe_com1dfa` | `voellmy_mu` | engine parameter | central ±0.03 | literature |
| `runout.avaframe_com1dfa` | `release_thickness` | engine parameter | central ±0.3 m | literature |
| `runout.avaframe_com1dfa` | `release_density` | engine parameter | central ±50 kg m⁻³ | literature |
| `runout.avaframe_com1dfa` | `release_extent_offset` | release input | central ±5 m | numerical |

Each sweep publishes every member's parameter value, result identity, member
identity and runout area, the central area, the min/max member areas, and the
**outer envelope** (the union of every member footprint, which the contract
requires to contain every member). The product names the
`dominant_uncertainty_contributor` as the sweep with the largest area spread.

**A span with no stated basis cannot be declared.** `SweepSpecification` requires
`basis` and a `source` long enough to say where the range came from, and it
requires offsets that bracket the central value from both sides. Construction
fails at declaration, before any member is computed, so an unjustified span can
never reach an envelope. `EnsembleSummary` carries the same two fields into the
published product.

`varies` distinguishes the two kinds. An `engine_parameter` sweep changes one
scalar the engine is given. A `release_input` sweep rebuilds the whole normalized
release: `release_extent_offset` moves the thresholded release boundary by whole
cells on the 5 m grid, so the release result's own content identity moves with it
and two different releases can never be confused downstream. Sweeping the *index
threshold* instead would have reported a zero sensitivity on this terrain — every
released cell carries the same relative index — which would be an artifact of the
test surface rather than a finding.

Both friction spans and both release-property spans are **assumed literature
ranges, not values fitted to any observed avalanche**, which is why every bound
is labelled `literature` rather than `source` or `calibration`. The extent span is
labelled `numerical`: it is a sensitivity to a discretization, and there is no
literature about this project's uncalibrated index cutoff. Member frequency is
**model frequency over a deterministic sweep** — not a probability, a confidence
level, or a calibrated likelihood. The contract stores that sentence with the
sweep so it cannot be dropped downstream.

### Measured spread on the synthetic case

At the pipeline defaults (40 s simulation time, seed 12345, alpha 25°, Voellmy
`mu` 0.155 / `xi` 4000 m s⁻², release thickness 0.8 m, density 200 kg m⁻³),
runout-area spread across each sweep:

| engine | parameter | spread |
|---|---|---:|
| `runout.avaframe_flowpy` | `alpha_angle` | 35 075 m² |
| `runout.avaframe_com1dfa` | `release_thickness` | 24 950 m² |
| `runout.avaframe_com1dfa` | `release_extent_offset` | 15 050 m² |
| `runout.avaframe_com1dfa` | `voellmy_mu` | 13 400 m² |
| `runout.avaframe_flowpy` | `release_extent_offset` | 10 600 m² |
| `runout.avaframe_com1dfa` | `release_density` | **0 m²** |

**The density zero is a real model property, not a missing measurement.** With
`entrainment_enabled=false` and Voellmy friction, density cancels out of com1DFA's
depth-averaged momentum balance: particle mass scales with ρ and flow thickness is
`m / (area · ρ)`, so the trajectories and the extent are invariant. The three
members are byte-identical in depth (max 1.4263 m) and velocity (max 31.6760 m s⁻¹)
and differ *only* in peak pressure — 150.50, 200.67 and 250.84 kPa, in exact
proportion to 150 : 200 : 250, because `p = ρv²`. So the honest reading is
"release density does not move the footprint of this engine in this
configuration", **not** "release density does not matter": it moves the pressure
field, and the sweep reports area.

Both release-input sweeps are monotone, which is the ordering check they exist to
expose: a larger release must not produce a smaller footprint (com1DFA 49 000 /
56 700 / 64 050 m²; Flow-Py 68 625 / 73 700 / 79 225 m² for −5 / 0 / +5 m), and a
thicker release must not produce a smaller one (45 450 / 56 700 / 70 400 m²).

**These numbers are sensitivity, not accuracy.** They say how far the answer moves
when an assumed input moves within an assumed range. They say nothing about which
value is right, and nothing about any real slope.

### Spans that are asked for and refused

A sweep that cannot be run is published in `unsupported_ensembles` with a reason
and the exact action that would enable it. Omitting it would read as "this
parameter does not matter"; inventing a range for it would read as "this range is
known".

| engine | parameter | why not |
|---|---|---|
| `runout.avaframe_com1dfa` | `entrainment_thickness` | com1DFA entrainment needs an `ENT` entrainment-area shapefile and a per-feature entrainment thickness. This slice supplies no entrainment layer and runs `simTypeList=null`. Enabling it needs a reviewed entrainment area plus `entTh`, `rhoEnt`, `entEroEnergy` and the resistances — and adapter/worker support for `simTypeList=ent`. |
| `runout.avaframe_flowpy` | `release_thickness` | com4FlowPy routes a dimensionless flux and solves no depth-averaged mass balance, so it has no thickness term. A zero spread would be a claim about avalanches, not about the model. |
| `runout.avaframe_flowpy` | `release_density` | com4FlowPy carries no snow density; its routed quantity is flux, not mass. |

## 6. Read-only API

External engines never run inside a request. These routes open files and validate
them; `POST /api/assess` is unchanged and remains the interactive baseline.

| route | returns |
|---|---|
| `GET /api/predictions` | Every stored product, with its unavailable stages and validation status. |
| `GET /api/predictions/{product_id}` | Stages, release summary, per-engine outputs and unsupported outputs, ensembles, provenance, warnings, limitations. |
| `GET /api/predictions/{product_id}/comparisons/{comparison_id}` | Every comparison metric with its status, unit and semantics. |

Rules the API surface enforces:

- **`release_probability` is always `null`,** and travels with
  `release_probability_unavailable_reason`. It may become non-null only after a
  calibrated probabilistic release model and eligible independent validation exist.
- **An output an engine cannot produce is listed in `unsupported_outputs` with a
  reason,** never omitted and never served as zero. A *sweep* that could not run
  is served the same way, in `unsupported_ensembles`, with its
  `required_to_enable` action.
- **A comparison metric is `available`, `not_applicable`, or `unsupported`.** A
  non-available metric carries `value: null`; the contract forbids publishing a
  number for one.
- Every product and comparison carries the research disclaimer.

## 7. What the pipeline cannot currently do

- **No Snow State Pack.** The stage is always `unavailable`. Where a Condition
  Pack is selected, the reason names the missing variables or masked hours; where
  forcing were complete, the reason is the absent reviewed initial snow/soil state,
  ground boundary, roughness, canopy classification and site configuration.
- **No physics-informed release.** The release stage runs the existing
  uncalibrated AvyCore terrain/loading relative-index baseline, which contains no
  modelled snow instability, no slab depth, and no weak-layer term.
- **No cross-machine stage reuse.** `--resume` is bound to this machine's
  interpreter bytes and installed-package manifest by construction (§8), so a
  cache written on one machine is a guaranteed miss on another. That is the
  intended behaviour, not a gap to close.
- **No real-site case.** See §2.
- **No field validation.** Every product publishes
  `validation_level: software_verification_only` with `eligible_field_events: 0`.

## 8. Input-keyed stage reuse (`--resume`)

Off by default. When it is on, an engine run is replaced by a stored bundle only
if a SHA-256 over **every** one of these matches:

| component | where it comes from |
|---|---|
| `cache_schema_version`, `pipeline_version` | this module |
| `engine_id`, `engine_version` | the availability probe |
| `adapter_version`, `adapter_sha256` | `adapter.replay_identity()` — the adapter module *and* its worker scripts |
| `process_sha256` | `process.py`, the subprocess launch/isolation plumbing the adapter runs through |
| `executable_sha256` | the isolated interpreter's own bytes |
| `environment_sha256` | the isolated environment's installed-distribution manifest, probed before the run |
| `scenario_sha256`, `request_sha256` | the engine request, minus disposable artifact URIs |
| `seed` | the engine request |

**A miss is the default whenever any component is unknown.** If the probe cannot
resolve the engine version, the interpreter digest or the environment digest,
no key is formed at all and the engine runs. A key that silently dropped a
component it could not resolve would match runs it has no right to match.

**A hit is proved, not assumed.** Restoring re-checks every file in the stored
bundle against the entry's own checksum manifest, revalidates `result.json`
against its content-addressed identity, and then requires the restored result's
`RunProvenance` to equal the key's components field by field — engine id and
version, adapter version and digest, executable digest, environment digest,
scenario digest and seed. (`process_sha256` is not among them: a result does not
record it, and it is in the key only to make the key stricter.) Any mismatch,
missing file, failed checksum or
unreadable entry is downgraded to a miss and the engine is re-executed. Nothing
is repaired in place.

`environment_sha256` is the one component that cannot be read off the request.
It is probed by running the identical manifest computation inside the isolated
environment before the run. The workers execute under `-I`, so they cannot import
a shared helper and the computation exists in two places; a gated test pins the
probe against what a real run records, so drift fails a test rather than quietly
poisoning a key.

Reuse changes execution and nothing else: `--resume` is excluded from the
configuration hash, and a resumed run publishes the identical `product_id`. The
cache lives at `runtime\stage-cache\`, a sibling of `predictions\`, and the
serving application never reads it — the "serve only immutable generated
artifacts" rule is unchanged.
