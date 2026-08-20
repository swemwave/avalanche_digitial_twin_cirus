# Architecture (Stage 3)

How the Mount Hosmer digital twin is put together, and why.

**Related:** [`limitations.md`](limitations.md) (what it cannot do) ·
[`prediction-products.md`](prediction-products.md) (the offline pipeline and its products) ·
[`runout-engines.md`](runout-engines.md) (which upstream engine runs, and what it can produce) ·
[`../../docs/data-footprint.md`](../../docs/data-footprint.md) (the bake input contract) ·
[`../../AGENTS.md`](../../AGENTS.md) (development objective and invariants).

---

## 1. The shape of the system

One pipeline, strictly one-directional, and a launcher. The key move is that **the geospatial work happens
once, offline, in the bake** — so the running service is a thin, dependency-light server over static files.

```
┌──────────────────────────────────────────────────────────────────────┐
│  DATA\mount_hosmer_data\     ⛔ READ-ONLY. Bake-time input ONLY.       │
│  ~6.5 GB LiDAR · land cover · fallback · fixed Sentinel-2 RGB scene   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  python -m app.bake  (ONCE, offline)
                                │  rasterio + pyproj live HERE and nowhere else
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  runtime\baked\              🤖 GENERATED — the entire served surface  │
│  tiles\... terrain RGB · imagery\... natural colour · layers · meta   │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  loaded with plain numpy — NO rasterio, NO DATA\
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  backend\app\main.py         FastAPI · 127.0.0.1:8000                  │
│  health · twin/meta · twin/tiles · twin/exposure · assess · assistant  │
│  /assess does its numerical work inside the request and returns JSON  │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  HTTP/JSON + PNG tiles
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  frontend\out\               static export served at 127.0.0.1:8000    │
│  Stage3App · Stage3Map (MapLibre 3D mesh) · ConditionPanel ·          │
│  ResultCard · AssistantPanel · lib/twin.ts (typed client)             │
└──────────────────────────────────────────────────────────────────────┘

   launcher\  ->  optional .NET source that builds/bakes/starts one server
```

---

## 2. Why it's built this way

**The bake is separated from serving** because the geospatial work is heavy (mosaicking 5 m LiDAR onto a
2400×2400 grid, hashing GB of input) and pulls a large dependency stack (rasterio/pyproj/GDAL). Doing it
once, offline, means the running service loads `.npy` with numpy and serves static PNGs — no source data,
no GDAL, a handful of light dependencies. That is the whole point of Stage 3: `import app.main` must not
pull rasterio, pyproj, pandas, geopandas, laspy, sqlalchemy, pillow, matplotlib, or pyyaml.

**Source data is immutable and bake-time only.** It is expensive to fetch and partly **irreplaceable** — the
Fernie `2C21P` archive returns HTTP 404 and cannot be re-downloaded. Only `bake.py` reads it, from the
allow-list in [`../../docs/data-footprint.md`](../../docs/data-footprint.md). Everything generated goes
under `runtime\`, which is always safe to delete and rebuild.

**Conditions are explicit user-entered scenarios, not feeds.** Stage 3 does no live weather/snow or dynamic
satellite ingestion. One fixed Sentinel-2 capture is baked as visual context only and never enters the
model. Simple inputs are assumptions; the advanced workspace records status, units, UTC time, source,
uncertainty and whole-area/elevation/aspect/drawn-area applicability. Seven inputs are active in the
current equations — new snow, wind speed/direction, release size, and the optional air temperature, flow
regime and user alpha angle — and every optional one is inert when left unknown. Snowpack, weak-layer and
field records are preserved and drive **advisories** rather than being translated into invented hazard
adjustments: the model has no term for them, and inventing a coefficient to give it one would be
fabricated accuracy. See [`limitations.md`](limitations.md).

**Missing data is never zero** because this is a safety-adjacent system. See §5.

---

## 3. Backend

FastAPI, ~1k LOC of runtime, plus a bake-time engine that is never imported by the running server.

| Piece | Path | Job |
|---|---|---|
| **App** | `app/main.py` | Builds FastAPI, installs middleware + error handlers, includes the one router, exposes `/api/health`. |
| **Routes** | `app/api/{terrain,assess,assistant}.py` | The HTTP surface: meta, tiles, exposure, assess, assistant. Thin — parse, call, return. |
| **Bake** | `app/bake.py` | Offline. Reuses `processing/terrain/engine.compute()` → writes `runtime\baked\`. |
| **Baked loader** | `app/baked.py` | numpy loader for `.npy` layers (as masked arrays) + `Reprojector` (grid→WGS84 from the baked lattice, via scipy). |
| **AvyCore — hazard** | `packages/avycore/src/avycore/hazard` | Release estimate, zone extraction, geometry and both runout engines. |
| **AvyCore — research snow/regimes** | `packages/avycore/src/avycore/snowpack` | Runtime-safe hourly forcing contract, evolving snow-state indices, solar geometry, separate dry-slab/wet-snow/dry-loose release paths, explicit unsupported-glide result, and regime-aware zone extraction. Used by frozen validation runners only; not wired to `/api/assess`. |
| **Scenario contract** | `packages/avycore/src/avycore/scenario.py` | Strict user inputs, status/provenance/uncertainty, spatial support, classification and canonical identity. Each parameter is `active` (enters an equation) or `advisory` (cannot). |
| **Advisories** | `packages/avycore/src/avycore/advisories.py` | Deterministic rules turning snowpack/field records into ranked written statements. Never numeric — every advisory publishes `changed_the_number: false`. |
| **Release coupling contract** | `packages/avycore/src/avycore/release_coupling.py` | The SnowState-to-Release input contract for dry-slab coupling. Decides eligibility and names what is missing; it computes no release value and has no path from terrain alone to an instability result. |
| **Prediction products** | `app/predictions.py`, `app/api/predictions.py` | Runtime-safe reader and read-only routes over `runtime\predictions\`. Pydantic and the standard library only; no engine is imported or run in a request. |
| **Offline pipeline** | `app/pipeline.py` | ⚠️ Offline. The one entry point that runs Mountain Pack → Condition Pack → Snow State → Release → Runout per engine → Comparison → Prediction Product. |
| **Assess** | `app/assess.py` | scenario → supported-condition mask → release raster → zones → runout (top-N) → one JSON. Disclaimer attached here, in code. |
| **AvyCore — assistant** | `packages/avycore/src/avycore/assistant` | Local Ollama. `explain` narrates an assessment; `chat` parses to sliders → runs the real `/assess` → narrates. |
| **Compatibility facades** | `app/{risk,geo,assistant}.py`, `app/simulation/*` | Keep existing imports and monkeypatch points working while delegating to the packages. |
| **Core** | `app/core/` | `settings.py` (env-driven paths), `paths.py` (path-escape guards, bake-time), `model_config.py` (the `DISCLAIMER` + bake-time YAML loader). |
| **Bake-time engine** | `app/processing/*`, xDEM, rio-tiler | ⚠️ rasterio/pyproj live here. Imported by `bake.py` only, never by `app.main`. |
| **Bake-time exposure** | `app/processing/exposure.py` | ⚠️ pyproj + shapely. Reprojects, classifies and buffers the declared OSM extract, derives built-up outlines, rasterises by cell centre. |
| **AvyCore — composite index** | `packages/avycore/src/avycore/hazard/composite.py` | Per-zone release/reach/exposure terms, their combination, and the area-weighted aggregate. |
| **Offline conditions** | `app/processing/conditions/` | Strict ECCC and PCIC source-cache import, provider normalization, disagreement reports, non-activating M2 forcing characterization, and atomic publication. Not imported by `app.main`; network access exists only in explicit acquisition commands. |
| **Offline snow integration** | `packages/avycore/src/avycore/snow/`, `app/processing/snow/` | Provider-neutral SnowStatePack plus strict SMET adapter, bounded external-process runner and atomic storage. Isolated from serving imports and inactive in assessments. |
| **Config** | `backend/config/avalanche_model.yaml` | Active terrain parameters, read at bake time. |

### The `app\` package shim — do not delete

An `app\__init__.py` at the **project root** (not `backend\`) contains only a `pkgutil.extend_path` call
that appends `backend\app` onto the `app` package's `__path__`. That is what makes `python -m app.bake`
and `uvicorn app.main:app` resolve from the project root even though the code lives in `backend\app\`. It
looks like an empty, deletable file. **It is not.** (The same commands also work with `backend\` as the
cwd, where `app` resolves directly — the launcher does that.)

---

## 4. The pipeline

```
DATA\ (allow-list)  ──►  python -m app.bake  ──►  runtime\baked\  ──►  serve
```

That is the unchanged Mount Hosmer default. An alternate Mountain Pack is baked
with explicit `--pack`, `--data-root`, and `--runtime-root` arguments. Using a
distinct root such as `runtime\mountains\mountain-id` writes that mountain to its
own `baked\` child; the server selects one generated surface at a time through
the matching `AVALANCHE_RUNTIME_ROOT`. See [`mountain-packs.md`](mountain-packs.md).

`bake.py`:

1. validates the configured Mountain Pack and loads its projected grid + AOI,
2. runs the tested 5 m terrain engine, which **mosaics the LiDAR DEM/DSM to ~99.9 % AOI coverage**
   (Copernicus GLO-30 as gap-fill only), derives slope/aspect/curvature/forest, and renders terrain-RGB
   tiles,
3. renders a fixed winter Sentinel-2 capture into natural-colour tiles for visual context only,
4. writes six model `.npy` layers (elevation, slope, aspect, plan curvature, general curvature and forest)
   plus categorical terrain-source and forest-source rasters; missing model values are NaN and missing
   source codes are zero and masked by the runtime,
5. optionally builds **exposure** from the declared OSM extract — reproject to the analysis CRS, clip to
   the AOI, classify and buffer by class, derive built-up outlines from residential/service road
   clustering, then rasterise by cell centre (chunked shapely predicates, no new dependency). It writes
   `layers/exposure_weight.npy` (float32 0–1, NaN outside the AOI), `layers/exposure_class.npy` (uint8
   class code, 255 for unknown — a **zero here is the measurement** "the extract maps nothing on this
   cell"), and `exposure/features.geojson` for display. A pack with no exposure asset simply skips this,
6. writes `meta.json` including per-layer units/checksums, source lineage, an `exposure` block (source,
   licence, attribution, derivation rule, class weights, per-class counts, limitation), a bake SHA-256
   identity, and a **21×21 grid→WGS84 control lattice** computed with pyproj — the runtime
   interpolates that lattice (scipy) instead of importing pyproj, accurate to <1 cm over the AOI.

Exposure is a **consequence term and nothing else**. `avycore.hazard.risk` never imports, receives or sees
it; it enters only the named exposure term of the composite hazard index, where it can raise a zone's index
and never lower one. `mountain_pack.py` enforces that boundary in both directions — see
[`limitations.md`](limitations.md).

The portable bake-input contract is documented in [`mountain-packs.md`](mountain-packs.md). A pack
selects source roles, grid, CRS, identity, units and licence; it does not change the deterministic model
profile or turn unverified data into validation evidence.

Historical conditions form a separate offline-only pipeline and never read `DATA/`:

```text
official source snapshot -> runtime/sources/conditions/<provider>/<snapshot_id>/
                         -> offline normalization + disagreement reports
                         -> runtime/baked/conditions/<condition_id>/
```

The source cache and reports are not read by the serving application. A validated ConditionPack can be
stored on the served surface, but Stage 3 assessment still uses explicit slider scenarios and does not
silently select, merge, or ingest a historical provider.

`characterize-m2-forcing` is an additional offline, cache-native diagnostic. It reads immutable ECCC/PCIC
artifacts plus the baked arrays, preserves masks, builds twice for deterministic replay, and atomically
publishes `runtime/reports/conditions/m2/<characterization_id>/`. It does not alter the bake or any
ConditionPack and is never imported by serving code. The inverse target lookup uses the baked reprojection
lattice and scipy, so `pyproj` remains bake-only.

`derive-reference-elevation` publishes an inactive, content-addressed contract below
`runtime/reports/terrain/reference-elevations/`. The controlled rebuild preserves `runtime/baked/conditions`
through whole-directory promotion. A complete pre-rebuild inventory is retained under
`runtime/verification/bake-preservation/`; the active rebuild is a zero-array-difference lineage refresh.

The M3 code path is deliberately disconnected:

```text
validated ConditionPack + reference/terrain contract
  -> offline strict SMET adapter (complete forcing only)
  -> version/hash-bound disposable external process
  -> validated atomic SnowStatePack
  -X-> serving imports / default assessment
```

At runtime, `/assess` first resolves the canonical scenario and its spatial support, then does the numerical
work synchronously: `risk.compute_release` → `risk.extract_release_zones`
→ `runout` for the top-scoring zones → `geo` builds all GeoJSON from the numpy masks → one JSON with the
release-potential index, zones, runout footprints/sensitivity envelopes/paths, required-input coverage,
source and bake provenance, uncertainty meaning, validation status, warnings, and the disclaimer.

Reach and exposure are measured **inside** the runout loop, while each `RunoutResult`'s full-grid masks are
still alive — each holds ~55 MiB on the real grid, so nothing is accumulated for post-processing. Every
zone then gets a composite `hazard_index`, `hazard_band`, `hazard_color` and its decomposed
`hazard_components`; the area-weighted mean is published as `area_hazard_index` alongside `peak_zone_index`
and `peak_zone_id`. These are new fields: `release_potential_index` and the legacy `hazard_score` /
`risk_level` aliases keep exactly their previous meaning and become components of the composite rather than
being redefined by it.

Assessment schema v3 publishes nullable `release_potential_index`; terrain-only/incomplete scenarios leave
it null and produce no condition-dependent runout. The deprecated JSON field `hazard_score` remains
for API compatibility but has exactly the same uncalibrated relative-index meaning. Fast routing reports no
random seed; particle mode reports the configured or supplied seed that reproduces its ensemble.

**LiDAR IS used now.** Older docs describe a legacy 30 m pipeline that fell back to Copernicus at 61.9 %
coverage. Stage 3's 5 m engine reaches 99.9 %. The 171 `.laz` point clouds remain unused (the DEM rasters
are derived from them).

---

## 5. Invariants

Correctness and safety properties, not preferences. See [`../../AGENTS.md`](../../AGENTS.md).

- **Source data is read-only, and bake-time only.** Nothing writes to `DATA\`; the running service never
  reads it. All output goes under `runtime\`.
- **The default Mount Hosmer layout keeps the app folder and `DATA\` as siblings.**
  `core/settings.py` and `launcher/Program.cs` independently default to
  `<project_root>\..\DATA\mount_hosmer_data`; relocating the default layout still requires an override
  (and the launcher retains its compiled default). Explicit alternate-pack CLI paths do not have this
  sibling constraint. No runtime/output root may be placed inside its read-only source root.
- **Missing data is missing — never zero, never safe.** Continuous layers mask NaN and categorical
  provenance layers mask their explicit zero NoData code. `assess.py` never reports a below-threshold day
  as a zero release-potential index: it falls
  back to the 95th percentile of the release estimate on avalanche terrain and labels it. Never `fillna(0)`
  a measurement; never let a gap lower a release estimate.
- **The disclaimer is attached in code, on every release-potential number, and never generated by the AI.**
- **Bake reuse requires proof of compatibility.** Schema, processing/config hash, required layer set,
  file sizes, layer checksums and the bake identity are validated before reuse or runtime loading. A stale
  bake fails visibly and `python -m app.bake --force` builds a complete staging directory before promoting
  it atomically. The running service has no processors or mutable scientific cache.

---

## 6. Security boundary

The backend binds to `127.0.0.1`; CORS is restricted to `localhost:3000` / `127.0.0.1:3000`. Local-only
research tool, **no authentication** — do not expose it to a network without adding some. Browser input is
now just numbers (slider values) and integer tile coordinates; `twin/tiles` serves a file only if it exists
(404 otherwise), so there is no path-from-browser surface. `core/paths.py::safe_source_path()` still guards
the bake's reads of `DATA\`.

---

## 7. Frontend

Next.js (App Router) + React + MapLibre GL. One screen: `Stage3App` holds the state and composes
`Stage3Map` (the 3D LiDAR mesh with fixed-RGB/hillshade views, release/runout overlays and drawn condition
scope), `ConditionPanel` (simple assumptions + advanced condition workspace), `ResultCard` (classification,
completeness, release result, provenance/replay identity + disclaimer), and
`AssistantPanel` (the Ollama AI). There is no router, no global state library, no server-side fetching.

Pydantic output models are the canonical API contract. Hey API generates `src/generated/` from the
exported OpenAPI schema; `src/lib/twin.ts` is a small application adapter over that generated SDK.

---

## 8. Runtime layout

```
runtime\
├── baked\            THE SERVED SURFACE (built by python -m app.bake)
│   ├── tiles\{z}\{x}\{y}.png   terrain-RGB tiles, z8–15
│   ├── imagery\{z}\{x}\{y}.png winter Sentinel-2 natural-colour tiles, z8–15
│   ├── exposure\features.geojson  classified WGS84 exposure vectors (optional)
│   ├── layers\*.npy            6 model layers + 2 provenance layers (+ 2 optional exposure)
│   └── meta.json               lineage/checksums/identity + grid/AOI/tiles/exposure/reprojection
├── predictions\        immutable offline prediction products (python -m app.pipeline)
│   └── prediction-product-<sha256>\  contract document + checksums + stage bundles
├── stage-cache\runout\  --resume only; offline engine reuse, never read by the service
├── sources\conditions\  immutable offline ECCC/PCIC source snapshots
├── reports\conditions\  generated coverage, disagreement, and M2 characterization reports
├── reports\terrain\     generated inactive reference-elevation contracts
├── snow-state-packs\    generated inactive offline M3 products (none from SNOWPACK yet)
├── verification\        complete pre-rebuild bake preservation and inventories
└── logs\             launcher setup and backend logs
```

Optional alternate mountains coexist under separately selected runtime roots,
for example `runtime\mountains\colorado-event-site\baked\`; they are generated
surfaces, not source data. The default server continues to read `runtime\baked\`.

Generated and gitignored. Delete it and run `python -m app.bake` to rebuild.

### Offline plugin engine boundary

`packages/avycore/src/avycore/engines/` defines dependency-light contracts for
snow-state, release and runout plugins. Descriptors declare stage, supported
avalanche regimes, required input kinds and units, machine-checked execution
ranges, CRS rules, output capabilities, versions, licence, validation status and
limitations. `EngineRegistry` applies a stable priority/ID order or a caller's
explicit order. Missing inputs, unsupported regimes/outputs and unavailable
engines are errors; an unavailable high-fidelity engine never silently becomes a
baseline run unless the caller explicitly permits availability fallback.
Every request/result also carries an explicit portable `site_id`; the caller
supplies a site-appropriate research disclaimer whose required non-operational,
non-probability and field-assessment warnings are contract-validated. Hosmer
continues to use its existing full disclaimer text.

External physics remains behind `backend/app/processing/`. The com1DFA adapter
launches an exact AvaFrame 2.1 environment with `python -I`, `shell=False`, a
timeout and bounded captured output. It verifies input size/hash, grid, mask, CRS
and coordinate order before running, then normalizes AvaFrame `pft`, `pfv` and
`ppr` to metre, metre-per-second and kPa arrays. Result bundles contain the
combined unknown-data mask, vectorized positive-thickness extent, full
configuration, engine/executable/adapter/input/output hashes, seed, validation
status and limitations. A portable environment inventory records Python,
platform and every installed distribution/version under its own hash. Random
staging paths are excluded from replay identity;
the effective numerical configuration is retained.

The adjacent analytical-verification worker executes the official AvaFrame 2.1
`avaSimilaritySol` inputs and upstream analytical solution inside the same
isolated environment. Its acceptance document is hash- and size-checked before
launch, so thresholds and source identities cannot be silently changed after a
result is seen. It retains the effective configuration, environment inventory,
analytical and numerical comparison fields, mass history, grid/unit/CRS/mask
invariants, artifact hashes and per-metric pass/fail status in a content-addressed
bundle. This path is offline validation tooling only and is not imported by any
serving entry point. AvaFrame's internal simulation label hashes the disposable
absolute avalanche-directory path; that exact label is retained as execution
provenance but is excluded from the scientific result ID. Replay still requires
byte-identical comparison fields, normalized configuration and environment
artifacts and identical scientific report content.

### The offline pipeline and its products

`python -m app.pipeline` is the single offline entry point. It runs the compatible
stages in order, publishes a content-addressed `PredictionProduct`, and refuses to
substitute anything for a stage that cannot run. Products are written to
`runtime\predictions\<product_id>\`, a **sibling generated root and deliberately
not a child of `runtime\baked\`**: the bake validates `baked\` against
`meta.json` and `--force` replaces that whole tree atomically, so a product stored
inside it would be destroyed by an unrelated terrain rebuild. The durable contract
— stage statuses, replay identity, storage layout, the read-only API, and the
bounded sensitivity ensembles — is in
[`prediction-products.md`](prediction-products.md).

`--resume` may replace one engine execution with a stored bundle, but only when a
key over the complete run identity — engine and adapter digests, the isolated
interpreter's bytes and its installed-package manifest, and the engine request
minus disposable paths — matches, and only after the stored bundle re-verifies
against its own checksums and provenance. The cache lives at
`runtime\stage-cache\`, a third sibling root; the serving application never reads
it, and a resumed run publishes the identical `product_id`.

`GET /api/predictions`, `GET /api/predictions/{product_id}` and
`GET /api/predictions/{product_id}/comparisons/{comparison_id}` serve those
products read-only. They open files and validate them; no external engine is
imported or executed inside a request, and `POST /api/assess` is unchanged.

Flow-Py now has a real, characterized adapter rather than an availability stub.
`runout.avaframe_flowpy` executes AvaFrame's `com4FlowPy` port and records the
hashes of the module files that ran; `runout.flowpy_upstream` is the separate
identity for the archived GPL-3.0 standalone distribution and stays fail-closed.
Which upstream implementation produced a result, what each engine can and cannot
output, and the analytical energy-line verification case are documented in
[`runout-engines.md`](runout-engines.md). r.avaflow still has an isolated
availability boundary and no enabled runner.

The reproducible integration example is:

```powershell
python -m venv .venv-avaframe
.venv-avaframe\Scripts\python -m pip install -r backend\requirements-avaframe.txt
python scripts\run_synthetic_avaframe.py `
  --avaframe-python .venv-avaframe\Scripts\python.exe `
  --output-root <new-output-directory>
```

It creates synthetic projected terrain, runs the existing uncalibrated PRA-style
relative release index, and supplies explicit synthetic thickness, density,
Voellmy parameters, timestep and seed to com1DFA. It is a software-integration
case, not a Mount Hosmer scenario or a validation case.
`scripts\run_synthetic_engine_comparison.py` extends it by driving com1DFA and
com4FlowPy from that *same* normalized release — polygons to one, raster to the
other, neither consuming the other's output — and reporting their disagreement.
The existing release, alpha-angle and particle engines remain the serving
baselines for `POST /api/assess`.

---

## 9. Launcher

The optional C#/.NET 9 launcher can be built into `MountHosmerDigitalTwin.exe`. It:

1. finds the project root by searching upward for marker files (`backend\app\main.py` +
   `frontend\package.json`) — so it is relocatable,
2. loads `.env` if present,
3. defaults the data root to `<projectRoot>\..\DATA\mount_hosmer_data`,
4. runs the serving-dependency-only `python -m app.check_bake` when a bake exists, or `python -m app.bake` when it is missing;
   stale or corrupted artifacts stop startup with an explicit rebuild instruction,
5. uses the generated API client, exports the frontend, and starts uvicorn (:8000),
6. opens the combined app and holds the server until you close it.

The generated `.exe` is ignored. Build it from `launcher/` with the .NET 9 SDK:

```powershell
dotnet publish -c Release -r win-x64 --self-contained false -p:PublishSingleFile=true -p:DebugType=None -p:DebugSymbols=false
```
