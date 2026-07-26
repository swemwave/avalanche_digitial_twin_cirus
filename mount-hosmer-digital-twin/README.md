# Mount Hosmer Avalanche Digital Twin — Stage 3 ("Ultra")

A local, offline, single-screen avalanche digital twin for **Mount Hosmer** near Fernie, BC. Stage 3
keeps four features and nothing else:

1. **3D terrain mesh** — a real 5 m BC-LiDAR mesh (~99.9 % AOI coverage), switchable between
   analytical hillshade and a baked winter Sentinel-2 natural-colour surface.
2. **Runout simulation** — fast (alpha-angle) and advanced (particle-ensemble) engines.
3. **A simplified risk model** — one transparent release estimate driven by UI sliders (new snow, wind
   speed, wind direction, release size), not weather ingestion.
4. **A local AI assistant** — Ollama (`llama3.1:8b`), fully offline; explains an assessment and runs
   what-if scenarios.

> This is a research and decision-support prototype. **It is not an operational avalanche forecast** and
> must never replace Avalanche Canada forecasts or field assessment. Every hazard number carries a
> non-operational disclaimer, attached in code.

**AI agents: read [`../CLAUDE.md`](../CLAUDE.md) first.**

## How it works: bake → baked → serve

Stage 3 has one, strictly one-directional pipeline. A **one-time offline bake** reads ~6.5 GB of LiDAR
(plus land cover, terrain fallback, metadata, and one fixed Sentinel-2 RGB capture) from `DATA\` and writes
`runtime\baked\` (terrain-RGB mesh tiles + natural-colour surface tiles + 7 `.npy` terrain layers +
`meta.json`). **After that, the running service reads no
source data at all** — it loads the `.npy` layers with plain numpy and serves the static tiles, so
`rasterio`/`pyproj` and the rest of the geospatial stack are **bake-time-only** dependencies.

Authoritative input contract: [`../docs/data-footprint.md`](../docs/data-footprint.md).

## Documentation

| Doc | Covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Stage 3 system design, the bake→baked→serve pipeline, invariants |
| [`docs/limitations.md`](docs/limitations.md) | **What this cannot do — read before making any claim about the model** |
| [`../docs/data-footprint.md`](../docs/data-footprint.md) | The bake input allow-list; what is archived, not deleted |
| [`../docs/repository-map.md`](../docs/repository-map.md) | Annotated tree |
| [`../docs/glossary.md`](../docs/glossary.md) | Domain terms (SWE, NDSI, DEM vs DSM, AOI…) |

Docs under `docs/` marked *(superseded)* describe the pre-Stage-3 build and are kept only for history.

## Setup (Windows / PowerShell)

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
Copy-Item .env.example .env

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip

# To RUN the app (rasterio-free runtime):
python -m pip install -r backend\requirements.txt
# To BUILD the bake as well (adds rasterio/pyproj/pillow/pyyaml):
python -m pip install -r backend\requirements-bake.txt

# Frontend:
cd frontend
npm install
npx playwright install chromium
```

## Run

### One-click launcher (easiest)

```text
D:\school\capstone\Avalanche\mount-hosmer-digital-twin\MountHosmerDigitalTwin.exe
```

Double-clicking it will:

- use `..\DATA\mount_hosmer_data` as the source data root unless `.env` overrides it,
- **run the one-time bake if `runtime\baked\meta.json` is missing** (reads the LiDAR allow-list; needs
  the bake-time deps),
- start the FastAPI backend on `http://127.0.0.1:8000` and the Next.js frontend on `http://127.0.0.1:3000`,
- open the app in your browser, and keep a small window open so you can stop it.

Startup logs: `runtime\logs\{launcher-bake,backend,frontend}.{out,err}.log`.

> The committed `MountHosmerDigitalTwin.exe` is a compiled binary. Editing `launcher\Program.cs` changes
> nothing until you rebuild with the .NET 9 SDK (`dotnet publish -c Release -r win-x64 --self-contained
> false -p:PublishSingleFile=true` → copy the exe to the app root).

### By hand

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin

# ONE-TIME bake (add --force to rebuild). Needs backend\requirements-bake.txt installed.
python -m app.bake

# Backend (needs runtime\baked\; rasterio-free):
python -m uvicorn app.main:app --reload --port 8000

# Frontend (use localhost, not 127.0.0.1 — Next 16 blocks dev resources cross-origin):
cd frontend
$env:NEXT_PUBLIC_API_BASE_URL="http://localhost:8000"
npm run dev
```

Open `http://localhost:3000`.

### Local AI assistant (optional, fully offline)

Install [Ollama](https://ollama.com), then:

```powershell
ollama serve
ollama pull llama3.1:8b
```

Without Ollama, the `/api/assistant/*` routes return a clean 503 and the rest of the app is unaffected.
Override with `AVALANCHE_OLLAMA_URL` / `AVALANCHE_OLLAMA_MODEL`.

## API

The entire running surface (see `backend/app/api/stage3.py`):

- `GET  /api/health`
- `GET  /api/twin/meta` — grid/AOI/tile metadata for the map
- `GET  /api/twin/tiles/{z}/{x}/{y}.png` — static baked terrain-RGB tiles
- `GET  /api/twin/imagery/{z}/{x}/{y}.png` — static baked Sentinel-2 natural-colour tiles
- `POST /api/assess` — sliders → release zones + runout + hazard, in one synchronous call
- `GET  /api/assistant/health` — assistant status; reachable behind a path-routing proxy,
  where `/api/health` reaches the assess service instead
- `POST /api/assistant/explain` — plain-language read of an assessment (Ollama)
- `POST /api/assistant/chat` — scenario chat: parse to sliders → re-run `/assess` → narrate (Ollama)

Types + fetch helpers live in `frontend/src/lib/twin.ts` — keep them in sync with the routes.

## Tests

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m pip install -r backend\requirements-dev.txt
python -m pytest
```

The suite uses a hermetic **synthetic bake** (`tests/synthetic_baked.py`) — no rasterio, no real `DATA\`.
It covers the risk model + assessment (`test_risk_assess.py`), the HTTP surface
(`test_stage3_api.py`), the rasterio-free geometry (`test_geo.py`), the assistant's intent router and
safety rails (`test_assistant_router.py`), the one-process/split-service contract
(`test_service_split.py`), and path safety (`test_paths.py`).

Frontend type-check + browser smoke (needs backend on :8000 and frontend on :3000):

```powershell
cd frontend
npx tsc --noEmit
npx playwright test        # e2e/twin.spec.ts: mesh renders, /api/assess runs, disclaimer visible
```
