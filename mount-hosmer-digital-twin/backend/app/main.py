from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors as api_errors
from app.api import middleware as api_middleware
from app.api import v1 as api_v1
from app.core.settings import get_settings
from app.services.aoi import load_aoi, load_grid
from app.services.catalog import generate_catalog, load_catalog
from app.services.conditions import (
    get_avalanche_forecast,
    get_snow,
    get_snow_summary,
    get_weather,
    get_weather_summary,
    process_avalanche_forecast,
    process_dynamic,
    process_snow,
    process_weather,
)
from app.services.events import (
    get_event,
    get_event_layer_metadata,
    get_event_layer_preview,
    list_events,
    process_all_events,
    process_event,
)
from app.services.susceptibility import (
    get_event_susceptibility,
    get_event_susceptibility_download,
    get_event_susceptibility_metadata,
    get_event_susceptibility_preview,
    process_all_event_susceptibility,
    process_event_susceptibility,
)
from app.services.terrain import (
    generate_terrain_layers,
    get_download_asset,
    get_contours,
    get_layer_metadata,
    get_layer_preview,
    get_osm_infrastructure,
    get_susceptibility,
    get_terrain_layers,
    terrain_metadata,
)

logger = logging.getLogger("avalanche.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Come up loudly if the deployment is broken; go down without losing work."""
    settings = get_settings()

    # A misconfigured deployment must fail at startup, not silently serve empty
    # results forever. validate() raises when the layout cannot work at all.
    for warning in settings.validate(require_data_root=False):
        logger.warning("configuration: %s", warning)

    from app.jobs import runner as jobs
    from app.storage.database import get_database

    # Migrations run in-process. The person who double-clicks the .exe cannot be
    # asked to run `alembic upgrade head`.
    database = get_database(settings)
    database.create_all()
    applied = database.migrate()
    if applied:
        logger.info("applied migrations: %s", applied)

    # A job left RUNNING in the table is a job whose process died. Nothing is
    # executing it any more, so leaving it as 'running' would strand it forever.
    orphans = jobs.get_runner(settings).reset_orphans()
    if orphans:
        logger.warning("reset %d orphaned job(s) left running by a previous process", orphans)

    try:
        yield
    finally:
        # Let in-flight work finish. Killing a simulation midway leaves half-written
        # rasters behind, and a half-written raster is indistinguishable from a real
        # one on disk.
        jobs.shutdown_runner()
        logger.info("job runner shut down cleanly")


app = FastAPI(
    title="Mount Hosmer Avalanche Digital Twin API",
    version=get_settings().app_version,
    description=(
        "An advanced physics-informed, terrain- and conditions-based avalanche digital twin and "
        "simulation platform. NOT an operational avalanche forecast."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

api_middleware.install(app)
api_errors.install(app)
app.include_router(api_v1.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    settings = get_settings()
    catalog_path = settings.runtime_root / "catalog" / "data_catalog.json"
    return {
        "status": "ok",
        "data_root_exists": settings.data_root.exists(),
        "runtime_root_exists": settings.runtime_root.exists(),
        "catalog_exists": catalog_path.exists(),
        "data_root_label": settings.data_root.name,
        "application_version": settings.app_version,
    }


@app.get("/api/catalog")
def catalog(compact: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        data = load_catalog(settings)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if compact:
        return {
            "schema_version": data.get("schema_version"),
            "generated_at_utc": data.get("generated_at_utc"),
            "summary": data.get("summary"),
            "missing_files": data.get("missing_files"),
            "failed_checks": data.get("failed_checks"),
            "download_errors": data.get("download_errors"),
        }
    return data


@app.post("/api/catalog/rescan")
def rescan_catalog(verify_checksums: bool = Query(default=True)) -> dict[str, object]:
    settings = get_settings()
    return generate_catalog(settings, verify_checksums=verify_checksums)


@app.get("/api/aoi")
def aoi() -> dict[str, object]:
    settings = get_settings()
    try:
        payload = load_aoi(settings)
        payload["grid"] = load_grid(settings)
        return payload
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/terrain/layers")
def terrain_layers() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_terrain_layers(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/terrain/metadata")
def get_terrain_metadata() -> dict[str, object]:
    settings = get_settings()
    return terrain_metadata(settings)


@app.post("/api/terrain/process")
def process_terrain(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return generate_terrain_layers(settings, force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/terrain/osm")
def terrain_osm() -> dict[str, object]:
    settings = get_settings()
    return get_osm_infrastructure(settings)


@app.get("/api/terrain/contours")
def terrain_contours() -> dict[str, object]:
    settings = get_settings()
    return get_contours(settings)


@app.get("/api/events")
def events() -> dict[str, object]:
    settings = get_settings()
    return list_events(settings)


@app.get("/api/events/{event_id}")
def event_detail(event_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        return get_event(settings, event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/events/{event_id}/process")
def event_process(event_id: str, force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_event(settings, event_id, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/events/process")
def events_process_all(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    return process_all_events(settings, force=force)


@app.get("/api/events/{event_id}/layers/{layer_id}/metadata")
def event_layer_metadata(event_id: str, layer_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        return get_event_layer_metadata(settings, event_id, layer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/events/{event_id}/layers/{layer_id}/preview")
def event_layer_preview(event_id: str, layer_id: str) -> FileResponse:
    settings = get_settings()
    try:
        return FileResponse(get_event_layer_preview(settings, event_id, layer_id), media_type="image/png")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/weather")
def weather() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_weather(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/weather/summary")
def weather_summary() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_weather_summary(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/weather/process")
def weather_process(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_weather(settings, force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/snow")
def snow() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_snow(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/snow/summary")
def snow_summary() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_snow_summary(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/snow/process")
def snow_process(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_snow(settings, force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/avalanche-forecast")
def avalanche_forecast() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_avalanche_forecast(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/avalanche-forecast/process")
def avalanche_forecast_process(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_avalanche_forecast(settings, force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/dynamic/process")
def dynamic_process(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_dynamic(settings, force=force)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/susceptibility/terrain")
def susceptibility_terrain() -> dict[str, object]:
    settings = get_settings()
    try:
        return get_susceptibility(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/susceptibility/events/{event_id}")
def susceptibility_event(event_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        return get_event_susceptibility(settings, event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/susceptibility/events/{event_id}/process")
def susceptibility_event_process(event_id: str, force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    try:
        return process_event_susceptibility(settings, event_id, force=force)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/susceptibility/events/process")
def susceptibility_events_process_all(force: bool = Query(default=False)) -> dict[str, object]:
    settings = get_settings()
    return process_all_event_susceptibility(settings, force=force)


@app.get("/api/susceptibility/events/{event_id}/metadata")
def susceptibility_event_metadata(event_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        return get_event_susceptibility_metadata(settings, event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/susceptibility/events/{event_id}/preview")
def susceptibility_event_preview(event_id: str) -> FileResponse:
    settings = get_settings()
    try:
        return FileResponse(get_event_susceptibility_preview(settings, event_id), media_type="image/png")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/susceptibility/events/{event_id}/download")
def susceptibility_event_download(event_id: str) -> FileResponse:
    settings = get_settings()
    try:
        path = get_event_susceptibility_download(settings, event_id)
        return FileResponse(path, media_type="image/tiff", filename=path.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/layers/{layer_id}/metadata")
def layer_metadata(layer_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        return get_layer_metadata(settings, layer_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/layers/{layer_id}/preview")
def layer_preview(layer_id: str) -> FileResponse:
    settings = get_settings()
    try:
        return FileResponse(get_layer_preview(settings, layer_id), media_type="image/png")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/download/{asset_id}")
def download_asset(asset_id: str) -> FileResponse:
    settings = get_settings()
    try:
        path = get_download_asset(settings, asset_id)
        media_type = "application/geo+json" if path.suffix.lower() == ".geojson" else "image/tiff"
        return FileResponse(path, media_type=media_type, filename=path.name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
