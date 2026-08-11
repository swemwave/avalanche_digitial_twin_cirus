# Windows Setup

> **Stage 3 note.** The geospatial stack (rasterio/GeoPandas/laspy) is now needed only to **build the bake**
> (`pip install -r backend\requirements-bake.txt`; run `python -m app.bake --force`).
> Validate an existing bake without access to the source tree with `python -m app.check_bake`.

## PowerShell Environment

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
Copy-Item .env.example .env
```

Edit `.env` only if the source data moves.

## Python

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements-dev.txt
```

## Backend

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m uvicorn app.main:app --reload --port 8000
```

## Frontend

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm install
$env:NEXT_PUBLIC_API_BASE_URL="http://localhost:8000"
npm run dev
```

## Tests

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
python -m pip install -r backend\requirements-dev.txt
python -m pytest
```

## Browser Verification

With backend and frontend servers running:

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin\frontend
npm run smoke
npm run visual-qa
```
