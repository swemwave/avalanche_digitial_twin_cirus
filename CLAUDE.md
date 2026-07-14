# CLAUDE.md — Orientation for AI Agents

**Read this first. It tells you what this repository is, where to work, and which rules you must not break.**

---

## 1. What this project is

A **Mount Hosmer avalanche digital twin**: a local, offline research prototype that catalogs ~46 GB of
geospatial data for one mountain in British Columbia, processes it into terrain / satellite / weather layers,
and serves them to a web UI with an **experimental** avalanche-susceptibility score.

Mount Hosmer is near Fernie, BC. The area of interest (AOI) is fixed and small — this is a single-mountain
study, not a general-purpose GIS platform.

> **Safety rule, non-negotiable.** This is **not an operational avalanche forecast**. It must never be
> presented as one, and it must never be described as replacing Avalanche Canada forecasts or field
> assessment. Every user-facing surface that shows a susceptibility number must keep its
> "experimental / non-operational" disclaimer. Do not remove those disclaimers, and do not add language
> that implies operational reliability.

---

## 2. Where to work

```
D:\school\capstone\Avalanche\
├── mount-hosmer-digital-twin\   ← ✅ THE APP. ~99% of all work happens here.
├── DATA\                        ← ⛔ READ-ONLY source data (46 GB). Never write here.
├── archive\                     ← ⛔ Superseded work. Do not build on this.
├── Tools\                       ← ⛔ Vendored QGIS install. Not project source.
└── docs\                        ← Repo-level docs (map, data inventory, glossary).
```

**If you are asked to change the application, you are working in `mount-hosmer-digital-twin\`.**
Nothing else in the tree is live code.

Inside the app:

| Path | What it is | Work here when… |
|---|---|---|
| `backend\app\services\` | All processing + business logic (8 modules, ~4,100 LOC) | Changing how data is processed or scored |
| `backend\app\main.py` | Every HTTP route (~35 endpoints) | Adding/changing an API endpoint |
| `backend\app\cli.py` | Every CLI subcommand | Adding/changing an offline command |
| `backend\app\core\` | Settings + path safety (~95 LOC) | Changing config or path resolution |
| `backend\config\susceptibility_weights.yaml` | Model weights | Tuning the susceptibility model |
| `frontend\src\components\` | The 5 UI views | Changing the UI |
| `frontend\src\lib\api.ts` | Typed API client + all response types | Changing the API contract (**mirror backend changes here**) |
| `tests\` | 20 pytest tests | Always — add a test with behavior changes |
| `launcher\Program.cs` | C# one-click launcher | Rarely. See the warning in §6 |
| `runtime\` | ⛔ **Generated output.** Never hand-edit; it is rebuilt from source | Never |

Full annotated tree: [`docs/repository-map.md`](docs/repository-map.md).
Deep architecture: [`mount-hosmer-digital-twin/docs/architecture.md`](mount-hosmer-digital-twin/docs/architecture.md).

---

## 3. The five invariants

Break these and you break the project.

### I1 — `DATA\` is strictly read-only
Source data is never modified, renamed, moved, or overwritten. It is expensive to re-download, and some of
it (the Fernie `2C21P` archive) **cannot** be re-downloaded. Every generated artifact goes under
`mount-hosmer-digital-twin\runtime\`.

### I2 — The app folder and `DATA\` must stay siblings
Both `backend/app/core/settings.py` and `launcher/Program.cs` independently resolve the data root as
`<project_root>\..\DATA\mount_hosmer_data`. **Moving or renaming either folder breaks both**, in two
languages, silently. If you must relocate them, update *both* files, and remember `Program.cs` requires a
.NET 9 rebuild to take effect.

### I3 — Missing data is reported as missing, never as zero
This is a **safety property**, not a style preference. A missing snowfall reading is not "no snowfall" —
it is "unknown". Missing dynamic components are excluded from the weighted-score denominator
(`average_available` in `susceptibility.py`) and surfaced as warnings in the UI. Never `fillna(0)`,
never default a missing measurement to a safe-looking value, never let a gap silently lower a risk score.

### I4 — Browser input never becomes a filesystem path
Event IDs and layer IDs from the client are validated against discovered folders and known layer records
(`validate_event_id`, `validate_layer_id`), and `core/paths.py::safe_source_path` rejects anything that
escapes the data root. The API exposes catalog IDs and relative paths only. Never accept a raw path from
the frontend. `tests/test_api_security.py` and `tests/test_paths.py` guard this.

### I5 — Processing is cache-gated by content hash
Each processor writes a sidecar to `runtime\cache\` recording SHA-256 hashes of its sources, its config
files, and its parameters, combined into an `input_signature_sha256`. Cached output is reused **only** when
that signature still matches. Invalidation is by content, not timestamp. If you add a processor input, you
must add it to that fingerprint or the cache will serve stale results. Use `--force` to rebuild deliberately.

---

## 4. How it fits together

```
DATA\mount_hosmer_data\        (read-only source: LiDAR, satellite, weather, snow, forecast)
        │
        ▼
  backend\app\services\        catalog → terrain → events → conditions → susceptibility
        │                      (each one hashes its inputs and writes a cache sidecar)
        ▼
  runtime\                     catalog\ processed\ previews\ cache\ exports\ logs\
        │                      (GeoTIFFs, PNGs, Parquet, JSON summaries)
        ▼
  backend\app\main.py          FastAPI, ~35 endpoints, localhost:8000
        │
        ▼
  frontend\src\                Next.js + MapLibre + Recharts, localhost:3000
```

The pipeline is **strictly one-directional**: source → processed → served. The API only ever *reads*
`runtime\` (the `POST /process` endpoints trigger a processor, which writes it). The frontend never
touches the filesystem.

**Processing order matters.** `susceptibility` depends on outputs of `terrain`, `events`, and `conditions`.
Run in that order, or use the launcher, which handles it.

---

## 5. Common tasks

**Run everything (easiest):** double-click `mount-hosmer-digital-twin\MountHosmerDigitalTwin.exe`.
It resolves paths, scans the catalog if missing, starts both servers, and opens the browser.

**Run the pieces by hand:**
```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m uvicorn app.main:app --reload --port 8000     # backend
cd frontend; npm run dev                                # frontend
```

**Rebuild processed data** (from the app root; add `--force` to ignore cache):
```powershell
python -m app.cli scan-data              # catalog the 271 source files
python -m app.cli process-terrain        # DEM → slope/aspect/hillshade/susceptibility
python -m app.cli process-events         # --all | --event-id MH_20260430T182949Z
python -m app.cli process-dynamic        # weather + snow + Avalanche Canada forecast
python -m app.cli process-susceptibility # --all  (run this LAST)
```

**Test:** `python -m pytest` from the app root. All 20 must pass. They use generated temp fixtures and
never read the real 46 GB `DATA\`, so they are fast and safe.

**Adding an API endpoint:** add the service function in `services\`, wire the route in `main.py`,
add the type + fetch helper in `frontend\src\lib\api.ts`, add a test. Keep the four in sync.

---

## 6. Gotchas that will bite you

**The `app\` package shim.** There is an `app\__init__.py` at the app root whose only job is to
`extend_path` onto `backend\app`. That is why `python -m app.cli` and `uvicorn app.main:app` work from
the project root even though the real code lives in `backend\app\`. It looks like dead code. **It is not.**
Deleting it breaks every command and the launcher. (Commands also work from `backend\`, where `app` resolves
directly — the launcher does exactly that.)

**The launcher is a compiled binary.** `MountHosmerDigitalTwin.exe` is committed at the app root and is what
users actually double-click. Editing `launcher\Program.cs` changes **nothing** until you rebuild with the
.NET 9 SDK. If you change path logic in `settings.py`, you have likely also invalidated `Program.cs`.

**LiDAR silently isn't used.** `choose_dem()` requires **>95%** AOI grid coverage from BC LiDAR tiles. The
local data only reaches **61.9%**, so the app always falls back to the Copernicus GLO-30 30 m DEM and skips
surface height. This is expected and is recorded as a warning — it is not a bug to "fix". The 171 `.laz`
point-cloud files (the bulk of the 46 GB) are cataloged but **not used** in the terrain pipeline.

**Two events only.** `MH_20260116T183016Z` and `MH_20260430T182949Z`. Event IDs are discovered from folder
names, not hardcoded, but nothing else exists to discover.

**Avalanche Canada data is *current* context, not history.** It is a live forecast for today, and it
currently reports summer/off-season. It must **never** be used as a historical avalanche label for the 2026
event dates, and off-season must never be read as "no risk". `susceptibility.py` deliberately treats it as a
non-scored contextual component.

**Windows-only paths.** Everything is `D:\`-rooted and PowerShell-first. The stale `D:\Avalanche\...` paths
that used to be in the docs were wrong and have been fixed; the correct root is `D:\school\capstone\Avalanche\`.

---

## 7. Status

Milestones 1–6 are complete: discovery, terrain twin, satellite event viewer, conditions dashboard,
experimental susceptibility, polish/verification. 20/20 tests pass.

Detailed status lives in `mount-hosmer-digital-twin\PROGRESS.md` and `MILESTONE_PROCESS.md`.
Known limits are enumerated in `mount-hosmer-digital-twin\docs\limitations.md` — **read it before making
any claim about what the model can do.**
