# Windows Setup

> **Stage 3 note.** The geospatial stack (rasterio/GeoPandas/laspy) is now needed only to **build the bake**
> (`pip install -r backend\requirements-bake.txt`; run `python -m app.bake`). The running service needs only
> `backend\requirements.txt`. The setup notes below still apply to the bake environment.

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
python -m pip install -r backend\requirements.txt
```

## Backend

```powershell
cd D:\school\capstone\Avalanche\mount-hosmer-digital-twin
$env:MOUNT_HOSMER_DATA_ROOT="D:\school\capstone\Avalanche\DATA\mount_hosmer_data"
python -m app.cli scan-data
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
