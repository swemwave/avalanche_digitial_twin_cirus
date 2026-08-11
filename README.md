# Mount Hosmer Avalanche Digital Twin

An offline-first research prototype for exploring avalanche release and runout on
Mount Hosmer near Fernie, British Columbia. It bakes a fixed 12 x 12 km terrain
model from local geospatial sources, then serves a 3D map, deterministic scenario
assessment, two runout engines, and an optional local AI explanation layer.

> This is not an operational avalanche forecast. Scores are uncalibrated relative
> indices, not probabilities, and must not replace Avalanche Canada guidance or
> field assessment.

## Current objective

Improve the Digital Twin's scientific fidelity and usefulness. Work should favor
data quality, explicit units and provenance, physically justified modeling,
validation, uncertainty, reproducibility, and characterized performance. UI or AI
changes are valuable when they make those properties clearer; they are not, by
themselves, evidence of accuracy.

AI coding agents must read [`AGENTS.md`](AGENTS.md) before changing the project.

## Repository layout

```text
Avalanche/
|-- AGENTS.md                     durable development rules
|-- DATA/mount_hosmer_data/       read-only source data; bake time only
|-- docs/                         source-data inventory and terminology
|-- mount-hosmer-digital-twin/    active application
|   |-- backend/                  FastAPI, bake, assessment orchestration
|   |-- frontend/                 Next.js and MapLibre interface
|   |-- packages/avycore/         canonical hazard and runout library source
|   |-- tests/                    hermetic numerical and API tests
|   |-- launcher/                 optional Windows launcher source
|   `-- runtime/                  generated bake and logs; ignored
|-- archive/                      superseded local material; ignored
`-- Tools/                        vendored QGIS tooling; ignored
```

Only `mount-hosmer-digital-twin/` is active code. Do not build new behavior on
`archive/`, and never modify source files under `DATA/`.

## Quick start

From PowerShell:

```powershell
cd mount-hosmer-digital-twin

py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements-dev.txt

cd frontend
npm install
cd ..
```

Validate an existing bake without reading `DATA/`:

```powershell
python -m app.check_bake
```

If the bake is missing or incompatible, install the bake dependencies and build
the offline artifacts explicitly:

```powershell
python -m pip install -r backend\requirements-bake.txt
python -m app.bake --force
```

Run the backend and frontend in separate terminals:

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

```powershell
cd frontend
$env:NEXT_PUBLIC_API_BASE_URL="http://localhost:8000"
npm run dev
```

Open `http://localhost:3000`.

## Verification

```powershell
python -m pytest
cd frontend
npm run lint
npm run build
```

The tests use a synthetic bake and never read the real dataset. Pytest imports the
local `packages/avycore/src` tree so core edits cannot be hidden by an installed
PyPI package.

## Durable documentation

- [`architecture.md`](mount-hosmer-digital-twin/docs/architecture.md): runtime and
  bake design, service boundaries, and invariants.
- [`limitations.md`](mount-hosmer-digital-twin/docs/limitations.md): scientific and
  operational limits; read before making model claims.
- [`data-footprint.md`](docs/data-footprint.md): authoritative bake-input allow-list.
- [`glossary.md`](docs/glossary.md): project-specific terrain and avalanche terms.
- [`deployment.md`](mount-hosmer-digital-twin/docs/deployment.md): optional service
  deployment runbook.
- [`windows-setup.md`](mount-hosmer-digital-twin/docs/windows-setup.md): local setup.

Generated binaries, session handoffs, milestone journals, presentations, and
superseded architecture references do not belong in the repository.
