# Production Upgrade — Implementation Audit and Plan

Status legend: `[ ]` not started · `[~]` in progress · `[x]` done

---

## Part 1 — Implementation audit (what existed before)

Performed by inspecting actual files, not documentation. Baseline captured 2026-07-13.

### Stack as found

| Concern | Finding |
|---|---|
| Backend framework | FastAPI (`backend/app/main.py`), ~35 routes, all unversioned `/api/*`, all synchronous |
| Backend entry | `uvicorn app.main:app`; `app/__init__.py` shim `extend_path`s onto `backend/app` |
| CLI | `argparse`, 9 subcommands (`backend/app/cli.py`) |
| Geospatial libs | rasterio, pyproj, numpy, scipy.ndimage, shapely (indirect), PIL, matplotlib (contours), pandas, PyYAML |
| Database | **None.** All state is JSON/GeoTIFF/Parquet under `runtime/` |
| Cache | Content-hash sidecars in `runtime/cache/` (`input_signature_sha256`). Sound design; kept and extended |
| Job system | **None.** Long processing runs block the HTTP request |
| Frontend | Next.js 16 (App Router), React 19, TypeScript, Tailwind 4, MapLibre GL 5, Recharts 3 |
| Frontend views | 5 components: Overview, TerrainViewer, EventViewer, ConditionsDashboard, SusceptibilityPage |
| 3D visualization | **None.** MapLibre is used in 2D only; raster layers are PNG image overlays |
| API client | `frontend/src/lib/api.ts` — hand-written types, `fetchJson<T>` |
| Tests | 20 pytest tests, all passing at baseline |
| Launcher | C# .NET 9, compiled `MountHosmerDigitalTwin.exe`, independently resolves `../DATA/mount_hosmer_data` |
| Config | `core/settings.py`; env vars `MOUNT_HOSMER_DATA_ROOT`, `MOUNT_HOSMER_RUNTIME_ROOT`; default data root hard-coded relative to project |
| Path safety | `core/paths.py::safe_source_path` — good; no browser input becomes a path |

### Existing processing code

- `services/catalog.py` — scans 271 source files, inspects rasters/CSV/JSON/point clouds, SHA-256, writes `runtime/catalog/data_catalog.json`.
- `services/terrain.py` (~1100 LOC) — the core. Builds a **30 m** grid, derives slope/aspect/hillshade/TPI/TRI/curvature/D8 flow/ridges/gullies/drainage/landcover masks, and a rules-based static susceptibility raster.
- `services/events.py` — per-event Sentinel-2 + Landsat processing: RGB composite, NDSI/NDVI/NDMI, SCL and QA_PIXEL cloud/snow masks, thermal.
- `services/conditions.py` — ECCC hourly/daily normalization, BC snow stations, Avalanche Canada current forecast parsing, per-event 24/48/72/168 h windows.
- `services/susceptibility.py` — dynamic condition index + combined index; correctly excludes missing components from the denominator (`average_available`).

### Data reality (verified against the files)

| Dataset | Verified state |
|---|---|
| BC LiDAR DEM | 8 tiles = 4 mapsheets × {2016, 2022}, **EPSG:2955**, **1 m**, nodata −32767 |
| BC LiDAR DSM | 8 tiles, same layout |
| Copernicus GLO-30 | DEM + slope + aspect, EPSG:26911, 30 m |
| ESA WorldCover 2021 | 10 m, EPSG:26911 |
| Sentinel-2 | B02/03/04/08/11/12 + NDSI/NDVI/NDMI + SCL, 10 m, per event |
| Landsat | blue/green/red/nir08/swir16/swir22/lwir11 + NDSI/NDVI/NDMI + QA_PIXEL, 30 m, per event |
| ECCC weather | hourly + daily + stations CSV/GeoJSON, 2025-11-01 → 2026-05-31 |
| BC snow | `2C09Q` (Morrissey Ridge: archive + current), `2C21P` (Fernie: current only; archive 404) |
| Avalanche Canada | current point/products/metadata/areas JSON — **current context only, not history** |
| OSM | `mount_hosmer_osm_features.geojson` (1.1 MB) + raw Overpass JSON |
| AOI | 12 km × 12 km, EPSG:26911, `[637650, 5491570, 649650, 5503570]` |

### 🔴 Critical audit finding — the LiDAR was never actually unusable

`terrain.py::select_latest_lidar_tiles()` keys tiles by mapsheet and keeps **one year per mapsheet**, preferring 2022. `choose_dem()` then requires >95 % AOI coverage and, getting only ~62 %, falls back to the 30 m Copernicus DEM. This was documented as a data limitation. It is not.

Measured directly:

| Source | AOI grid coverage |
|---|---|
| 2022 tiles alone | **62.20 %** |
| 2016 tiles alone | **44.43 %** |
| 2022 preferred, 2016 gap-filling | **99.93 %** |

The two acquisitions have **complementary nodata gaps**. Merging them across years — not just across mapsheets — yields effectively full 1 m coverage of the AOI. This single fix is what makes a genuine high-resolution digital twin possible, and it directly satisfies the requirement *"do not use 30 m terrain when valid 1 m terrain is available."*

### Other audit findings

1. **No 3D.** MapLibre is present but terrain/`hillshade` is 2D image overlay only. Needs a real terrain-mesh source.
2. **Curvature exists but is not persisted as a final product**, and there is no *general* curvature — only profile/plan.
3. **Flow accumulation is a Python `for` loop over every cell** (`flow_products`). At 30 m/400×400 that is tolerable; at 5 m/2400×2400 it is not. Must be vectorized.
4. **`normalize()` uses 2–98 percentile stretch by default** — fine for display, dangerous if reused for scoring. Scoring paths must use explicit physical breakpoints.
5. **Weather windows only exist relative to the two events.** No arbitrary-datetime replay, no "current", no scenario.
6. **Wind direction is stored but never used spatially.** No wind loading anywhere.
7. **No release zones, no runout, no exposure, no consequence, no confidence score.**
8. **Landsat `lwir11` is read but surface temperature is only a summary stat**, not a persisted layer.
9. **Avalanche Canada is correctly quarantined** as non-scored context. Preserve this.
10. **`.exe` launcher hard-codes the sibling-folder assumption** in a second language. Any path change must be mirrored.

### Baseline confirmed working

- `python -m pytest` → **20 passed**.
- Backend + frontend start; catalog, terrain, events, conditions, susceptibility endpoints all respond.

---

## Part 2 — Architecture decisions

| Decision | Rationale |
|---|---|
| **Keep FastAPI + Next.js.** Extend in place. | No concrete blocker. Replacing either would discard working, tested code. |
| **SQLite via SQLAlchemy as the default DB; `DATABASE_URL` selects PostgreSQL/PostGIS.** | The app ships as a **one-click offline `.exe`**. Requiring a Postgres server would break the primary delivery mode. Geometry is stored as GeoJSON text + bbox columns, which works identically on both engines. PostGIS remains a drop-in for server deployment. |
| **In-process job runner (thread pool) backed by a DB `jobs` table.** Not Celery/Redis. | Same reason: offline-first. `REDIS_URL` is reserved in config for a future distributed runner. The job *contract* (state, progress, failure reason, idempotency key) is broker-agnostic, so swapping in Celery later touches one module. |
| **Terrain analysis grid = 5 m** (configurable), environmental grid = 10 m, 30 m fallback retained. | 1 m over 12 × 12 km is 144 M px/raster — ~25 derived layers would be ~14 GB and minutes per run. 5 m (2400 × 2400) preserves the LiDAR signal that matters for avalanche terrain (slope, curvature, gullies, ridges) at ~23 MB/layer. The 1 m source is average-resampled, **not** point-sampled, so it is a true aggregation. The pipeline is resolution-parameterized, so 2 m or 1 m is a config change, not a rewrite. |
| **Per-pixel provenance rasters throughout.** | Enforces invariant I3 and the scientific-honesty requirement: every value knows whether it was observed / derived / interpolated / modelled / unavailable. |
| **Rasters stay on the filesystem**, DB holds references + checksums. | Explicitly required; also keeps the content-hash cache (I5) authoritative. |
| **Layered package structure inside `backend/app/`** rather than a repo reorganization. | The `app/` shim, launcher, and imports all depend on the current layout. New responsibilities become new subpackages: `processing/`, `models/`, `simulation/`, `storage/`, `jobs/`, `ingestion/`. |
| **Legacy `/api/*` routes are preserved**; `/api/v1/*` is added alongside. | The existing frontend keeps working while it is migrated view by view. |

### Terminology (enforced in code, API, and UI)

The system is **an advanced physics-informed, terrain- and conditions-based avalanche digital twin and simulation platform.** Outputs are `estimated_release_score`, `terrain_susceptibility`, `dynamic_instability`, `estimated_runout`, `simulated_avalanche_path`, `modelled_snow_condition`, `confidence_score`. Never `probability`, never `forecast`.

Provenance vocabulary: `observed` · `downloaded` · `derived` · `interpolated` · `modelled` · `user_supplied` · `unavailable`.

---

## Part 3 — The plan

### P0 — Config + path foundation ✅
- [x] Implementation audit written (this document)
- [x] `core/settings.py`: `AVALANCHE_*` env vars, typed settings, model version constants, `validate()` with actionable startup errors
- [x] `core/paths.py`: centralized resolver, `safe_output_path`, runtime dir contract
- [x] `core/provenance.py`: the 7-value vocabulary, `Value` wrapper, confidence weights
- [x] `core/model_config.py` + `backend/config/avalanche_model.yaml`: every scientific parameter in one versioned, hashed file
- [x] `.env.example` rewritten; backward-compat with `MOUNT_HOSMER_*` retained

### P1 — Geospatial harmonization ✅
- [x] `processing/harmonization/grids.py` — 5 m terrain / 10 m environmental / 30 m fallback grids, shared origin and pixel alignment
- [x] `processing/harmonization/raster_io.py` — resampling chosen by band *semantics* (average/bilinear for continuous, mode/nearest for classes), NoData preserved end to end
- [x] COG output: tiled, DEFLATE, internal overviews
- [x] Metadata sidecar writer (sources, algorithm, params, CRS, res, time, version, SHA-256)

### P2 — High-resolution terrain engine ✅
- [x] **Multi-year LiDAR mosaic fixed** (2022 preferred ∪ 2016 gap-filling) + per-pixel provenance raster
- [x] Vectorized D8 flow accumulation (topological peeling; ~0.5 s on 5.76 M cells vs. minutes for the old per-cell loop)
- [x] Full derivative suite: 31 layers
- [x] Unit tests on synthetic surfaces with analytically known answers (`tests/test_terrain_derivatives.py`, 13 tests)

**Measured result of P2, on the real data:**

| Metric | Before | After |
|---|---|---|
| Terrain source | Copernicus GLO-30 | **BC LiDAR 1 m** |
| LiDAR-backed AOI | 0 % (rejected at 61.9 %) | **99.93 %** |
| Effective source resolution | 30 m | **1.02 m** |
| Analysis grid | 400 × 400 | **2400 × 2400** |
| Persisted terrain layers | 20 | **31** |
| Copernicus fill remaining | 100 % | 0.07 % |

Runtime: 94 s cold, cache hit thereafter. Output: 412 MB of COGs + 47 MB of previews.

### P3 — Weather feature engine ✅
- [x] Window features at any datetime (6/12/24/48/72 h)
- [x] Freeze-thaw, rapid-warming, storm-loading, rain-on-snow indices
- [x] Replay / current / scenario modes + forecast adapter interface

### P4 — Snow condition model ✅
- [x] Snow presence, depth index, SWE index, new-snow loading, wet-snow, melt, freeze-thaw, persistence
- [x] Confidence + provenance per output; empty files never treated as observations

### P5 — Wind-loading model ✅
- [x] Vector wind math, Winstral Sx exposure, lee/windward/cross-loading, cornice potential
- [x] Configurable weights + documented assumptions
- [x] **Verified against the real terrain** (see "Wind direction verification" below)

### P6 — Release-zone model ✅
- [x] `terrain_susceptibility.tif`, `dynamic_instability.tif`, `estimated_release_score.tif`
- [x] Release-zone polygons with full per-zone attribution and limitations
- [x] **Aspect-sector segmentation** (see 🔴 finding below)

### P7 — Runout simulation ✅
- [x] Fast interactive mode (routing + alpha-angle stopping)
- [x] Advanced particle-ensemble mode (friction, spreading, velocity, deposition, uncertainty, seeded)
- [x] Pluggable engine interface

### P8 — Exposure + consequence ✅
### P9 — Risk + confidence ✅
### P10 — Storage + migrations ✅
### P11 — Jobs + scheduler ✅

**P3–P11 executed end to end on the real data for the first time.** `run_analysis` →
`run_simulation` (both engines) → `data_health` → DB round-trip → job runner all run. Four defects
were found and fixed; they are recorded below. Tests: **79 passing** (was 33).

### P12 — API v1 + reliability/security ✅
- [x] `/api/v1/*` — 25 routes: health, ready, data/catalog, data/health, layers, layers/{id}, events,
      events/{id}, analysis, analysis/{id}, analysis/presets, simulations, simulations/{id},
      simulations/{id}/assets, jobs, jobs/{id}, model/config, model/versions, model/schedule, audit
- [x] **Long work returns a job.** `POST /analysis` and `POST /simulations` answer **202** with a job
      id and are polled — an analysis is ~90 s and a simulation ~60 s of numerical work, and neither
      belongs inside an HTTP request. Progress is reported per stage ("Modelling wind loading"), so a
      90-second wait reads as work rather than a hang.
- [x] Pydantic validation with **physical bounds**. A hazard model will faithfully compute a number
      from a 5,000 km/h wind, and it looks exactly as authoritative as a real one.
- [x] Structured logging + correlation ids (accepted from the caller, echoed on every response and
      every error), centralized exception handler, one error envelope.
- [x] Rate limits on the expensive routes, request-size limit, readiness check, graceful shutdown
      (`reset_orphans()` at startup, `shutdown_runner()` on exit, migrations applied in-process).
- [x] **Legacy `/api/*` routes untouched and tested** — the current frontend still depends on them.
- [x] `frontend/src/lib/apiV1.ts` mirrors the contract (additive; `api.ts` is untouched).
- [x] CLI: `run-analysis`, `run-simulation`, `data-health`, `migrate`.

### P13 — Catalogue + data health ✅
- [x] `services/data_catalog.py` — the file inventory joined to the health verdict, so every dataset
      reports `usable_by_model` next to its file count. **Presence is not usability**: the 2025-26
      snow files are present, well-formed, catalogued — and empty.
- [x] Exposed at `/api/v1/data/catalog` and `/api/v1/data/health`, persisted to
      `runtime/health/data_health.json` so the readiness probe need not re-walk 46 GB.
- [x] Readiness distinguishes "the process is alive" from "this deployment can actually produce an
      analysis", and names the command that fixes each blocker.

### P14 — Frontend ✅
- [x] **Real 3D terrain.** `services/tiles.py` serves Mapbox Terrain-RGB tiles reprojected from the
      5 m COG, so MapLibre builds an actual mesh. This is what the LiDAR fix was for: draped over the
      30 m Copernicus DEM the mountain is a smooth lump; at 5 m the gullies and ridges that decide
      where an avalanche starts are visible. Tiles are disk-cached (~0.15 s cold, ~0.01 s warm).
- [x] Layer selector (all **31** engine layers), opacity, legend with real min/max, vertical
      exaggeration, four camera presets.
- [x] Release zones (coloured by release score, clickable), runout, dashed uncertainty envelope,
      flow paths.
- [x] Analysis controls: current / historical replay / scenario, the 6 presets, manual wind
      (direction as a compass, stated as the direction wind blows *from*), snowfall, temperature,
      simulation mode, release size.
- [x] Results panel: risk / hazard / consequence / confidence, `mainReasons`, limitations, the
      per-component provenance table showing which inputs were **EXCLUDED** (never "scored as zero"),
      the confidence breakdown with its uncalibrated ceiling, and the model version + config hash.
- [x] Data-health page (presence vs. usability), simulation history (reproduce / compare / archive,
      refusing to compare across model versions).
- [x] `frontend/src/lib/apiV1.ts` types the whole contract; `e2e/twin.spec.ts` drives a real browser.

### P15 — Docker + deployment docs

---

## Part 3d — Defects found while building P14

**🔴 Transparent NoData decoded to −10,000 m.** The obvious way to represent a gap in a terrain tile
is a transparent pixel. It is wrong, and it fails silently: the Terrain-RGB decoder **ignores the
alpha channel** and reads the colour, so a transparent pixel is RGB(0,0,0) = −10,000 m. The result
was not a hole in the mesh — it was the entire mountain standing on a cliff plunging ten kilometres
into an abyss. This is the same "missing became zero" failure the whole codebase is built to prevent,
wearing a different hat. Gaps are now filled to the AOI valley floor (1,024 m): a visibly flat plinth,
used only for *rendering*. No model reads these tiles.

**`WarpedVRT` cannot do boundless reads** — and every tile at the edge of a 12 × 12 km AOI is a
boundless read. Replaced with a direct `reproject` into the tile's own grid.

**v1 `/layers` was serving the legacy 30 m terrain service**, not the 5 m engine — 20 old layers
instead of the 31 new ones, on the wrong grid.

**The preset API returns `id`/`inputs`; the TypeScript type said `name`/`input`.** Every
`<option value>` rendered as `undefined`, so choosing a preset silently sent the wrong key. Types now
match the payload.

**Duplicate React keys.** `mainReasons` legitimately repeats — two zones on the same aspect produce
the same sentence — so the text is not a unique key.

**A raw `fetch()` for `/layers`** let the browser serve a stale layer list from cache after the
terrain was rebuilt. It now goes through the typed client, which sets `cache: "no-store"`.

---

## Part 3c — Defects found while building P12/P13

**The catalogue/health join was matching nothing.** The first cut joined on the catalogue's
`category` field, which is `null` on every one of the 271 records, and its `dataset` field, which is
free prose ("Sentinel-2 derived NDSI"). Both "worked" — they simply matched zero files and reported a
confident zero. Now joined on `relative_path`, the only stable key the catalogue has, with a
`datasets_with_no_matching_files` guard that makes an empty join **visible instead of plausible**. It
immediately caught two wrong prefixes.

**A 422 came back as a 500.** A failing Pydantic `model_validator` puts the raw `ValueError` object
into `ctx`, which is not JSON-serialisable — so rendering the validation error raised *inside the
error handler*, and every invalid request got an opaque 500 instead of a message naming the bad field.

**Job parameters were about to be poisoned with a `Settings` blob.** `Job.dumps` uses
`default=str`, so passing `Settings` through the job parameters would not have crashed: it would have
silently stringified the whole object — absolute filesystem paths and all — into the persisted record
of every job. The runner now injects settings instead, and the stored parameters stay a clean,
replayable set of arguments.

**🔴 The settings cache was silently disabling a security test.** `get_settings()` memoises Settings
in a module global, and pytest imports every test module at collection. So the first import to touch
settings froze the environment for the entire session. `test_api_security` monkeypatches
`DATA_ROOT` to an empty temp directory and asserts it sees zero events — but it was getting the
**real** data root and the two real events, and had been passing for the wrong reason. It did not
fail loudly; it just stopped testing anything. `tests/conftest.py` now clears the cache around every
test, so each one reads the environment it actually configured.

---

## Part 3b — Defects found on first execution

These modules had been written but never run. Four bugs, none of which crashed anything — every one
produced plausible-looking output, which is what made them worth writing tests for.

### 🔴 Release zones fused across every aspect of the mountain

`release_zones.extract()` ran `ndimage.label()` straight across the thresholded mask. The steep
ground encircling a peak is **topologically connected the whole way round it**, so one component
flooded across the north, east and south faces alike.

| | Before | Aspect sectors | + elevation bands |
|---|---|---|---|
| Largest zone | **1,614 ha** | 75 ha | **23 ha** |
| Vertical extent | 1,100 m | 707 m | **300 m** |
| Aspect consistency | **0.16** (faces the entire compass) | 0.98 | 0.98 |
| Zones > 100 ha | 2, holding **89% of all release area** | 0 | 0 |

A zone facing every direction has no fall line, so it cannot be simulated, and its "dominant aspect"
is a vector mean over the whole compass. Fixed by growing connected components **within aspect
sectors** and never merging across them. Aspect is the physically discriminating variable — loading
and solar both key off it — and two patches of steep ground that face different ways are two slopes,
however they touch.

**Aspect alone was not enough**, which only became visible once the runout work forced a closer look:
a zone could hold a coherent aspect while stretching **3.2 km along the massif and spanning 707 m of
vertical**. A slab does not do that, and a zone that tall has no meaningful crown to release from. So
components are also cut into **elevation bands** (`release_zones.elevation_band_m`, 300 m). A band
boundary is an arbitrary line and a slab straddling one is reported as two zones — that is the smaller
cost. Zones are now genuine slabs: median 6.8 ha, 278 m of vertical, aspect consistency 0.98, mean
slope 33–51°.

### 🔴 Fast runout inundated a quarter of the AOI

The fast engine routes flow to every downhill neighbour, and marked a cell "reached" on **geometry
alone** — if it sat inside the alpha cone, it counted, regardless of how much snow got there. But
multi-directional routing leaks a vanishing trickle into every cell in the catchment, so a single
medium release covered **3,538 ha of the 14,400 ha AOI**.

The alpha angle bounds how *far* snow can run; it does not say snow reaches everywhere within that
bound. Fixed with a `runout.fast_mode.minimum_flux` stopping criterion (flow thinner than 2% of one
release cell's snow has stopped). Runout: 3,538 → 1,572 ha, and the "fast" engine is now **3.5×
faster** (211 s → 61 s) because it no longer visits the whole catchment. Verified: every zone now
stops at 27.0–29.5°, against a configured alpha of 27°.

### 🔴 The advanced Voellmy engine — three compounding bugs

It silently outran its own uncertainty envelope on **10 of 60 zones**. Chasing that down uncovered a
chain of three defects, none of which crashed.

**1. Particles had no momentum.** The killer, and the one that hid the others:

```python
norm = np.where(gradient > 1e-9, gradient, 1.0)
dir_row = -gy / norm      # on flat ground gy = gx = 0
dir_col = -gx / norm      # → direction is (0, 0)
```

A particle was advanced along the *local fall line*, so on flat ground its direction was `(0, 0)` and
it **froze on the spot, still carrying 25 m/s**. An avalanche could not run out onto a valley floor —
which is the entire purpose of a runout model. On the synthetic slope-then-flat test the deposit
landed exactly at the slope break at **every** release size (runout onto the flat: 0 m, for all four).
Real terrain is never perfectly flat, so it never crashed; the flow just quietly died wherever the
ground eased off.

Velocity is now a **vector**: gravity acts down the fall line, friction opposes the direction of
actual travel. Verified against the analytical Voellmy stopping distance `x = (ξ/2g)·ln(1 + v₀²/μξ)`:

| release size | μ | runout onto flat | analytical |
|---|---|---|---|
| small | 0.24 | **60 m** | ~62 m |
| very_large | 0.144 | **90 m** | ~92 m |

Momentum also lets debris climb a counter-slope instead of stopping dead in the bottom of a gully.

**2. No stopping rule, and no value of `mu` could supply one.** In Voellmy a particle keeps moving
while `tan θ > μ`, so μ sets the *local* slope at which it may finally rest: `μ = 0.20` means it
coasts until the ground flattens below 11°. Several drainages below Mount Hosmer keep falling more
steeply than that for kilometres, so a particle that found one rode it to the floor of the AOI.
Sweeping μ from 0.20 → 0.60 on the real terrain moved the *median* angle of reach from 26.6° to 31.7°
but left the worst zone pinned near 13°. **The tail was never a friction problem.**

It is a missing-physics problem: a dimensionless particle carries no mass, so it cannot shed energy by
spreading, thinning and depositing the way a real avalanche does. The classical remedy
(Perla–Cheng–McClung, and standard practice) is to let the dynamics supply the velocity field and let
the empirical angle of reach supply the stopping rule. A parcel may not travel below the **energy
line** drawn from its own release point at the alpha envelope.

The line is anchored *per particle*, not at the crown: a zone with real extent puts its own toe far
from its crown at little vertical drop, and a crown-anchored line would retire a particle that had
barely moved. This also resolves the earlier safety worry — truncating at alpha is not an
under-estimate of the hazard, because **release size sets alpha and alpha bounds runout. If you want
the longer runout, you simulate the bigger avalanche.**

**3. The alpha diagnostic reported the ensemble's worst dud.** `nanmin` over all particles: one that
had crept 10 m across a bench while dropping 1 m has an angle of reach near zero, so the stalled
parcel was being reported as the runout of the avalanche. It now measures the **deposit tip** — the
furthest-travelling particle.

Also fixed: particles that ran off the grid were **clipped back onto the boundary** and kept moving,
re-marking the same edge cell every step, piling the runout up against the side of the AOI. They are
now retired and counted, and the simulation says the avalanche left the map (`particles_left_the_aoi`).

**Result:** angle of reach at the deposit tip is now min 23.0° / median 26.9° against an energy line of
23.0° — **0 of 60 zones breach it** (was 10/60). The two engines now agree within **1.8×** (was 5.7×);
the residual gap is the expected difference between a spreading flow-routing model and a channelised
particle model. Friction still stops most zones naturally — the energy line only binds where Voellmy
would have coasted forever.

### 🟠 Two honesty/robustness bugs

- **`instability.py` always reported snow cover as `observed`.** It truth-tested a dict that is never
  empty, so a fully *modelled* snow field was labelled as measured. Now reports the dominant
  provenance (`modelled` when satellite resolved < 50% of the AOI). This is invariant I3 territory.
- **`repository.save_analysis` was not idempotent** — re-saving one `analysis_id` raised
  `IntegrityError`, which made the job runner's idempotency key useless the moment it was needed
  (a retry). Both saves now upsert and rebuild their child rows.

### ✅ Wind direction verification (the flagged risk — no bug)

A sign error in the wind vector math would not crash; it would quietly put every modelled lee slope
on the wrong side of the mountain, and the output would still look plausible. Checked at two levels
against the real terrain, for the `wind_loading` preset (SW, 225°, 55 km/h):

| Check | Expected | Measured |
|---|---|---|
| Peak lee deposition | NE (45°) | **NE** (25.9 mean) |
| Peak windward erosion | SW (225°) | **SW** (23.3 mean) |
| Release-zone aspect enrichment vs. background terrain | NE highest | **NE 1.60×** (highest of any aspect) |

The wind math is correct. The raw lee/windward *area* ratio looks flat only because N and NW are
depleted (0.48×, 0.66×) — they sit outside the lee cone — and because wind is deliberately ~10% of
the release score (terrain susceptibility is 55%). That weighting is defensible: terrain is known
from 1 m LiDAR, wind is interpolated from a station 17.2 km away, so the well-constrained input is
weighted higher. Locked in by `tests/test_wind_and_release_zones.py`, which asserts lee/windward at
four bearings including across the 0/360 seam.

---

## Part 4 — Scientific limitations (permanent, restated in API + UI)

These do not go away by writing more code. They require **external data**.

1. **No historical avalanche occurrence records for Mount Hosmer.** Therefore no supervised model is trained, no probability is calibrated, and no output may be called a probability. Every score is a *relative index*.
2. **No snowpit / snow-layer observations.** Snowpack layering, weak layers, and persistent slabs are **not modelled**. This is the single largest scientific gap; a real forecast depends on them.
3. **No reliable winter 2025–26 mountain-wide snow depth or SWE.** Snow outputs are dimensionless 0–1 indices, not centimetres or millimetres.
4. **Nearest snow station (Morrissey Ridge) is ~19 km away and 1860 m elevation.** Its readings are *interpolated* to the AOI, never *observed* there.
5. **Only one building is present in OSM for the AOI.** Missing buildings are **not** evidence that no buildings exist; the consequence engine emits a completeness warning.
6. **Runout model is uncalibrated.** No validated physical runout observations exist for this mountain, so alpha angles come from published regional ranges, not from local back-analysis.
7. **Avalanche Canada data is current-season context.** It reports off-season and must never be used as a historical label for the 2026 event dates, nor read as "no risk".
8. **Two satellite events only.** Change detection has a sample size of two.
