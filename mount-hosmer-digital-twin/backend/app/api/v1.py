"""The v1 API.

Long work does not run inside a request. An analysis is ~90 s and a simulation ~60 s
of numerical work, so both return **202 with a job id** and are polled. The legacy
`/api/*` routes still run their processors synchronously, and they are left exactly
as they are: the current frontend depends on them, and breaking it to tidy the
routing table would be a poor trade.

Every payload that carries a score also carries its disclaimer, its warnings and the
model version that produced it. Those fields are not decoration and they are not
optional -- a number from this system is meaningless without them, and an API that
lets a caller fetch the number alone is an API that invites the number to be quoted
alone.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse

from app.api.errors import ApiError, not_found
from app.api.schemas import (
    AnalysisRequest,
    ProcessRequest,
    RescanRequest,
    SimulationRequest,
    job_accepted,
)
from app.core.model_config import DISCLAIMER, load_model_config
from app.core.paths import analyses_dir, simulations_dir
from app.core.settings import get_settings
from app.jobs import runner as jobs
from app.jobs import tasks
from app.processing.weather import presets as scenario_presets
from app.services import data_catalog
from app.services.events import get_event, list_events
from app.simulation.runout import RELEASE_SIZES
from app.storage import repository

router = APIRouter(prefix="/api/v1", tags=["v1"])


def _correlation(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


# --- Health and readiness -----------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness. Cheap, and says nothing about whether the data is any good."""
    settings = get_settings()
    return {
        "status": "ok",
        "application_version": settings.app_version,
        "model_version": settings.model_version,
        "environment": settings.app_env,
        "jobs_enabled": settings.jobs_enabled,
    }


@router.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    """Readiness. Can this deployment actually produce an analysis?

    A 503 here means the answer is no -- the data is not mounted, or the terrain has
    never been built. Reporting "ok" in that state would be a lie a load balancer
    would believe.
    """
    payload = data_catalog.readiness(get_settings())
    if not payload["ready"]:
        response.status_code = 503
    return payload


# --- Data ---------------------------------------------------------------------


@router.get("/data/catalog")
def catalog(
    compact: bool = Query(default=True, description="Omit the 271-file inventory."),
    refresh_health: bool = Query(default=False),
) -> dict[str, Any]:
    """The file inventory, joined to whether the model can actually use each dataset."""
    return data_catalog.enriched_catalog(
        get_settings(), compact=compact, refresh_health=refresh_health
    )


@router.get("/data/health")
def health_report(refresh: bool = Query(default=False)) -> dict[str, Any]:
    settings = get_settings()
    if not refresh:
        cached = data_catalog.cached_health(settings)
        if cached is not None:
            return cached
    return data_catalog.health_report(settings)


@router.post("/data/catalog/rescan", status_code=202)
def rescan(request: Request, body: RescanRequest) -> dict[str, Any]:
    job = jobs.get_runner(get_settings()).submit(
        "scan_catalog",
        tasks.TASKS["scan_catalog"],
        {"verify_checksums": body.verify_checksums},
        correlation_id=_correlation(request),
    )
    return job_accepted(job, f"/api/v1/jobs/{job['job_id']}")


# --- Layers -------------------------------------------------------------------


def _terrain_index() -> dict[str, Any]:
    """The 5 m engine's index -- 31 layers -- NOT the legacy 30 m terrain service."""
    import json

    from app.processing.terrain import engine

    path = engine.index_path(get_settings())
    if not path.exists():
        raise ApiError(
            409,
            "not_processed",
            "The terrain has not been built yet.",
            remedy="POST /api/v1/layers/process, or run: python -m app.cli process-terrain",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _find_layer(layer_id: str) -> dict[str, Any]:
    """Resolve a browser-supplied id against the layers we actually built.

    This is invariant I4. The id never becomes a path; it selects a record, and the
    record carries the path.
    """
    for record in _terrain_index().get("layers", []):
        if str(record.get("id")) == layer_id:
            return record
    raise not_found("Layer", layer_id)


@router.get("/layers")
def layers() -> dict[str, Any]:
    index = _terrain_index()
    return {
        "layers": index.get("layers", []),
        "layer_count": index.get("layer_count", 0),
        "terrain": index.get("terrain", {}),
        "warnings": index.get("warnings", []),
    }


@router.get("/layers/{layer_id}")
def layer(layer_id: str) -> dict[str, Any]:
    return _find_layer(layer_id)


@router.get("/layers/{layer_id}/preview")
def layer_preview(layer_id: str) -> FileResponse:
    from app.processing.terrain import engine

    _find_layer(layer_id)  # validates the id against the built layers first
    path = engine.preview_path(get_settings(), layer_id)
    if not path.exists():
        raise not_found("Layer preview", layer_id)
    return FileResponse(path, media_type="image/png")


@router.get("/terrain/tiles/{z}/{x}/{y}.png")
def terrain_tile(z: int, x: int, y: int) -> Response:
    """Terrain-RGB, so MapLibre can build a real 3D mesh of the mountain.

    The 1 m LiDAR fix is what makes this worth doing. Draped over a 30 m Copernicus
    DEM the mountain is a smooth lump; at 5 m the gullies and ridges that actually
    decide where an avalanche starts and where it goes are visible.
    """
    from app.services import tiles

    try:
        png = tiles.terrain_tile(get_settings(), z, x, y)
    except tiles.TileError as exc:
        raise ApiError(400, "bad_tile", str(exc)) from exc

    return Response(
        content=png,
        media_type="image/png",
        # The terrain is content-hash gated and only changes when it is rebuilt.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/terrain/meta")
def terrain_meta() -> dict[str, Any]:
    """What the map needs to place the mountain: the AOI, and how to ask for terrain."""
    from app.services.aoi import load_aoi, load_grid
    from app.services.tiles import MAX_ZOOM, MIN_ZOOM, TILE_SIZE

    settings = get_settings()
    grid = load_grid(settings)
    terrain = _terrain_index().get("terrain", {}) or {}

    return {
        "aoi": load_aoi(settings).get("geojson"),
        "bbox_wgs84": grid.get("aoi_bbox_wgs84"),
        "terrain_rgb": {
            "tiles": ["/api/v1/terrain/tiles/{z}/{x}/{y}.png"],
            "tile_size": TILE_SIZE,
            "min_zoom": MIN_ZOOM,
            "max_zoom": MAX_ZOOM,
            "encoding": "mapbox",
        },
        "lidar_fraction": terrain.get("lidar_fraction"),
        "effective_source_resolution_m": terrain.get("effective_source_resolution_m"),
        "analysis_resolution_m": settings.terrain_resolution_m,
    }


@router.post("/layers/process", status_code=202)
def process_layers(request: Request, body: ProcessRequest) -> dict[str, Any]:
    job = jobs.get_runner(get_settings()).submit(
        "process_terrain",
        tasks.TASKS["process_terrain"],
        {"force": body.force},
        correlation_id=_correlation(request),
    )
    return job_accepted(job, f"/api/v1/jobs/{job['job_id']}")


# --- Events -------------------------------------------------------------------


@router.get("/events")
def events() -> dict[str, Any]:
    return list_events(get_settings())


@router.get("/events/{event_id}")
def event(event_id: str) -> dict[str, Any]:
    try:
        return get_event(get_settings(), event_id)
    except (FileNotFoundError, KeyError) as exc:
        raise not_found("Event", event_id) from exc


# --- Analysis -----------------------------------------------------------------


@router.get("/analysis")
def list_analysis(
    limit: int = Query(default=50, ge=1, le=200),
    include_archived: bool = Query(default=False),
) -> dict[str, Any]:
    return {
        "analyses": repository.list_analyses(
            get_settings(), limit=limit, include_archived=include_archived
        ),
        "disclaimer": DISCLAIMER,
    }


@router.post("/analysis", status_code=202)
def create_analysis(request: Request, body: AnalysisRequest) -> dict[str, Any]:
    settings = get_settings()

    scenario: dict[str, Any] | None = None
    if body.preset:
        try:
            scenario = scenario_presets.preset(body.preset).to_dict()
        except KeyError as exc:
            raise ApiError(
                400,
                "unknown_preset",
                f"Unknown scenario preset {body.preset!r}.",
                available=list(scenario_presets.PRESETS),
            ) from exc
    elif body.scenario:
        scenario = body.scenario.to_input().to_dict()

    job = jobs.get_runner(settings).submit(
        "run_analysis",
        tasks.TASKS["run_analysis"],
        {
            "mode": body.mode,
            "at": body.at.isoformat() if body.at else None,
            "scenario": scenario,
            "event_id": body.event_id,
            "correlation_id": _correlation(request),
        },
        idempotency_key=body.idempotency_key,
        correlation_id=_correlation(request),
    )
    return job_accepted(job, f"/api/v1/jobs/{job['job_id']}")


@router.get("/analysis/presets")
def analysis_presets() -> dict[str, Any]:
    return {"presets": scenario_presets.list_presets()}


@router.get("/analysis/{analysis_id}")
def analysis(analysis_id: str) -> dict[str, Any]:
    payload = repository.get_analysis(get_settings(), analysis_id)
    if payload is None:
        raise not_found("Analysis", analysis_id)
    return payload


# --- Simulations --------------------------------------------------------------


@router.get("/simulations")
def list_simulations(
    limit: int = Query(default=50, ge=1, le=200),
    include_archived: bool = Query(default=False),
) -> dict[str, Any]:
    return {
        "simulations": repository.list_simulations(
            get_settings(), limit=limit, include_archived=include_archived
        ),
        "disclaimer": DISCLAIMER,
    }


@router.post("/simulations", status_code=202)
def create_simulation(request: Request, body: SimulationRequest) -> dict[str, Any]:
    settings = get_settings()

    if repository.get_analysis(settings, body.analysis_id) is None:
        raise not_found("Analysis", body.analysis_id)

    job = jobs.get_runner(settings).submit(
        "run_simulation",
        tasks.TASKS["run_simulation"],
        {
            "analysis_id": body.analysis_id,
            "zone_ids": body.zone_ids,
            "simulation_mode": body.simulation_mode,
            "release_size": body.release_size,
            "seed": body.seed,
            "correlation_id": _correlation(request),
        },
        idempotency_key=body.idempotency_key,
        correlation_id=_correlation(request),
    )
    return job_accepted(job, f"/api/v1/jobs/{job['job_id']}")


@router.get("/simulations/{simulation_id}")
def simulation(simulation_id: str) -> dict[str, Any]:
    payload = repository.get_simulation(get_settings(), simulation_id)
    if payload is None:
        raise not_found("Simulation", simulation_id)
    return payload


@router.get("/simulations/{simulation_id}/assets")
def simulation_assets(simulation_id: str) -> dict[str, Any]:
    settings = get_settings()
    assets = repository.exposed_assets(settings, simulation_id)
    if assets is None:
        raise not_found("Simulation", simulation_id)

    payload = repository.get_simulation(settings, simulation_id) or {}
    exposure = payload.get("exposure") or {}
    return {
        "simulation_id": simulation_id,
        "assets": assets,
        "summary": exposure.get("summary"),
        "completeness": exposure.get("completeness"),
        "consequence_score": exposure.get("consequence_score"),
        "consequence_class": exposure.get("consequence_class"),
        "warnings": exposure.get("warnings", []),
        "lower_bound_note": (
            "OpenStreetMap holds exactly ONE building for this AOI. Missing buildings are not "
            "evidence that no buildings exist, so the exposed-asset list and the consequence score "
            "are a LOWER BOUND on what is actually at risk."
        ),
        "disclaimer": DISCLAIMER,
    }


@router.post("/simulations/{simulation_id}/archive")
def archive(simulation_id: str) -> dict[str, Any]:
    if not repository.archive_simulation(get_settings(), simulation_id):
        raise not_found("Simulation", simulation_id)
    return {"simulation_id": simulation_id, "archived": True}


# --- Jobs ---------------------------------------------------------------------


@router.get("/jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = Query(default=None),
) -> dict[str, Any]:
    return {"jobs": jobs.get_runner(get_settings()).list(limit=limit, state=state)}


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    record = jobs.get_runner(get_settings()).get(job_id)
    if record is None:
        raise not_found("Job", job_id)
    return record


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    runner = jobs.get_runner(get_settings())
    if runner.get(job_id) is None:
        raise not_found("Job", job_id)
    cancelled = runner.cancel(job_id)
    return {
        "job_id": job_id,
        "cancelled": cancelled,
        "note": (
            "Only a queued job can be cancelled. A job already running is left to finish, because "
            "killing it midway would leave half-written rasters behind."
        ),
    }


# --- Model --------------------------------------------------------------------


@router.get("/model/config")
def model_config() -> dict[str, Any]:
    """Every scientific parameter, with the hash that identifies this exact set."""
    config = load_model_config(get_settings())
    return {
        **config.to_dict(),
        "release_sizes": list(RELEASE_SIZES),
        "disclaimer": DISCLAIMER,
        "is_calibrated": False,
        "calibration_note": (
            "None of these parameters is fitted to an observed Mount Hosmer avalanche, because no "
            "historical avalanche record exists for this mountain. They are published regional "
            "values. Changing them changes every score, and no observation can say which set is "
            "right."
        ),
    }


@router.get("/model/versions")
def model_versions() -> dict[str, Any]:
    settings = get_settings()
    config = load_model_config(settings)
    return {
        "current": config.provenance(),
        "software_version": settings.app_version,
        "history": repository.model_versions(settings),
        "note": (
            "Two results produced under different config hashes are not comparable. The hash is "
            "stored on every analysis and simulation so any number can be traced to the exact "
            "parameters that produced it."
        ),
    }


@router.get("/model/schedule")
def schedule() -> dict[str, Any]:
    return {"schedule": tasks.describe_schedule(get_settings())}


@router.get("/audit")
def audit(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"audit": repository.audit_trail(get_settings(), limit=limit)}
