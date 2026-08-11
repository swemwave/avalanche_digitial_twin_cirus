# Avalanche Digital Twin agent guide

## Objective

Make the Mount Hosmer Avalanche Digital Twin as scientifically faithful,
reproducible, transparent, and useful as the available data permits.

Accuracy work takes priority in this order:

1. Preserve source-data fidelity, masks, units, coordinate systems, and lineage.
2. Improve physically justified release and runout behavior.
3. Add validation data, characterized benchmarks, and uncertainty reporting.
4. Improve performance without changing numerical behavior unintentionally.
5. Improve the API and interface while keeping model meaning explicit.

Do not describe a cleaner interface, a more complex algorithm, or an AI-generated
explanation as improved accuracy. Accuracy claims require evidence from a trusted
reference, field observation, published method, or characterized numerical test.

## Scope

- `mount-hosmer-digital-twin/` is the active application.
- `mount-hosmer-digital-twin/packages/avycore/` is the canonical source for the
  deterministic hazard, geometry, runout, and grounded-assistant library.
- `DATA/mount_hosmer_data/` is read-only bake input.
- `archive/` is historical material and must not be used as current source code.
- `Tools/` is a vendored QGIS installation, not project source.
- `mount-hosmer-digital-twin/runtime/` is generated and must not be hand-edited.

## Non-negotiable safety and data rules

- This is an experimental research prototype, not an operational avalanche
  forecast. Keep the deterministic disclaimer on every hazard/release result.
- Never imply that the model replaces Avalanche Canada guidance or field
  assessment.
- Scores are relative indices, not probabilities.
- Never convert missing terrain or condition data to a safe-looking zero. Combine
  masks across required inputs and make incomplete coverage visible.
- Never modify, move, rename, or delete files under `DATA/`.
- Only the offline bake may read `DATA/`; the serving application must operate
  solely from `runtime/baked/`.
- Keep runtime imports free of rasterio, pyproj, xDEM, GDAL, pandas, GeoPandas,
  laspy, and other bake-only dependencies.
- The AI assistant may explain deterministic output or request a new deterministic
  scenario. It must never invent hazard values.

Read these before changing scientific behavior:

- `mount-hosmer-digital-twin/docs/limitations.md`
- `mount-hosmer-digital-twin/docs/architecture.md`
- `docs/data-footprint.md`

## Code map

| Area | Canonical location |
|---|---|
| Release scoring and zone extraction | `packages/avycore/src/avycore/hazard/risk.py` |
| Runout engines | `packages/avycore/src/avycore/hazard/runout.py` |
| Geometry conversion | `packages/avycore/src/avycore/hazard/geometry.py` |
| Full assessment orchestration | `backend/app/assess.py` |
| Offline terrain bake | `backend/app/bake.py`, `backend/app/processing/` |
| Runtime baked-data reader | `backend/app/baked.py` |
| API contract and routes | `backend/app/api/models.py`, `backend/app/api/` |
| Frontend API adapter | `frontend/src/lib/twin.ts` |
| Main UI | `frontend/src/components/` |
| Synthetic fixtures and regression tests | `tests/` |

The modules at `backend/app/{risk,geo,assistant}.py` and
`backend/app/simulation/` are compatibility facades. Put new core behavior in
`avycore`, not in those facades.

## Development workflow

Run commands from `mount-hosmer-digital-twin/` unless noted otherwise.

```powershell
python -m pip install -r backend/requirements-dev.txt
python -m pytest

cd frontend
npm install
npm run lint
npm run build
```

Pytest is configured to import the repository's `packages/avycore/src`, not an
installed PyPI copy. Do not remove the import-origin regression test.

For bake changes, install `backend/requirements-bake.txt` and use a disposable
runtime path or the synthetic fixtures first. A real rebuild is:

```powershell
python -m app.bake --force
```

It is expensive and reads the large source dataset, so do not run it unless the
change requires real-data verification.

When an API model changes, update `backend/app/api/models.py`, export OpenAPI,
regenerate the frontend client, adapt `frontend/src/lib/twin.ts`, then run both
backend and frontend checks.

## Definition of done

- Add or update a test for changed model behavior.
- Characterize intentional numerical changes; do not casually update snapshots.
- Check units, coordinate order, wind-direction convention, masks, and bounds.
- Preserve deterministic results for identical inputs and seeds.
- Report uncertainty and model limitations with the result.
- Run `python -m pytest`, `npm run lint`, and `npm run build` when applicable.
- Update documentation only when a durable contract, limitation, or operating
  procedure changed. Do not create session logs, handoff notes, progress journals,
  presentation files, or speculative planning documents in the repository.

