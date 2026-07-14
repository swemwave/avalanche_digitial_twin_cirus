"""Job handlers, and the scheduled-refresh registry.

Every task here is **rerun-safe**. Running it twice produces the same result and
does not duplicate outputs: the processors are gated by a content hash of their
inputs (invariant I5), and the analysis/simulation writers use unique ids. That is
what makes a scheduler safe to point at them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.core.settings import Settings
from app.processing.weather.features import ScenarioInput
from app.storage import repository

logger = logging.getLogger(__name__)

TASKS: dict[str, Callable[..., dict[str, Any]]] = {}


def register(name: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def decorator(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        TASKS[name] = function
        return function

    return decorator


def _noop(percent: int, message: str) -> None:
    return None


# --- Processing tasks ---------------------------------------------------------


@register("scan_catalog")
def scan_catalog(settings: Settings, *, verify_checksums: bool = True, progress=_noop) -> dict[str, Any]:
    from app.services.catalog import generate_catalog

    progress(10, "Scanning source data")
    result = generate_catalog(settings, verify_checksums=verify_checksums)
    progress(100, "Catalog written")
    return {"run_id": "catalog", "summary": result.get("summary")}


@register("process_terrain")
def process_terrain(settings: Settings, *, force: bool = False, progress=_noop) -> dict[str, Any]:
    from app.processing.terrain import engine

    progress(5, "Building terrain model")
    result = engine.generate(settings, force=force)
    progress(100, "Terrain layers ready")
    return {
        "run_id": "terrain",
        "layer_count": result.get("layer_count"),
        "layers": result.get("layers", []),
        "warnings": result.get("warnings", []),
    }


@register("process_dynamic")
def process_dynamic(settings: Settings, *, force: bool = False, progress=_noop) -> dict[str, Any]:
    from app.services.conditions import process_dynamic as run

    progress(10, "Normalizing weather, snow and forecast data")
    result = run(settings, force=force)
    progress(100, "Dynamic data ready")
    return {"run_id": "dynamic", **result}


@register("process_events")
def process_events(settings: Settings, *, force: bool = False, progress=_noop) -> dict[str, Any]:
    from app.services.events import process_all_events

    progress(10, "Processing satellite events")
    result = process_all_events(settings, force=force)
    progress(100, "Events processed")
    return {"run_id": "events", **result}


# --- Analysis and simulation --------------------------------------------------


@register("run_analysis")
def run_analysis_task(
    settings: Settings,
    *,
    mode: str = "current",
    at: str | None = None,
    scenario: dict[str, Any] | None = None,
    event_id: str | None = None,
    correlation_id: str | None = None,
    progress=_noop,
) -> dict[str, Any]:
    from app.services.analysis import run_analysis

    parsed_at = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None
    scenario_input = ScenarioInput(**scenario) if scenario else None

    payload = run_analysis(
        settings,
        mode=mode,
        at=parsed_at,
        scenario=scenario_input,
        event_id=event_id,
        progress=progress,
    )
    repository.save_analysis(settings, payload, correlation_id=correlation_id)
    return payload


@register("run_simulation")
def run_simulation_task(
    settings: Settings,
    *,
    analysis_id: str,
    zone_ids: list[str] | None = None,
    simulation_mode: str = "fast",
    release_size: str | None = None,
    seed: int | None = None,
    correlation_id: str | None = None,
    progress=_noop,
) -> dict[str, Any]:
    from app.services.analysis import run_simulation

    payload = run_simulation(
        settings,
        analysis_id=analysis_id,
        zone_ids=zone_ids,
        simulation_mode=simulation_mode,
        release_size=release_size,
        seed=seed,
        progress=progress,
    )
    repository.save_simulation(settings, payload, correlation_id=correlation_id)
    return payload


# --- Maintenance --------------------------------------------------------------


@register("check_data_health")
def check_data_health(settings: Settings, *, progress=_noop) -> dict[str, Any]:
    from app.services.data_health import data_health

    progress(20, "Checking data health")
    report = data_health(settings)
    progress(100, "Data health checked")
    return {"run_id": "data_health", **report}


@register("clean_temp")
def clean_temp(settings: Settings, *, max_age_hours: float = 24.0, progress=_noop) -> dict[str, Any]:
    import time
    from pathlib import Path

    tmp = settings.runtime_root / "tmp"
    removed = 0
    if tmp.exists():
        cutoff = time.time() - max_age_hours * 3600
        for path in tmp.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Could not remove temp file %s: %s", path, exc)
    progress(100, f"Removed {removed} temporary files")
    return {"run_id": "clean_temp", "removed": removed}


# --- Scheduled refresh --------------------------------------------------------


@dataclass(frozen=True)
class ScheduledJob:
    name: str
    task: str
    interval_seconds: float
    parameters: dict[str, Any]
    description: str
    #: Off by default when it reaches the network. This is an offline research
    #: prototype; unattended outbound calls must be an explicit opt-in.
    requires_network: bool = False


SCHEDULE: tuple[ScheduledJob, ...] = (
    ScheduledJob(
        name="catalog_scan",
        task="scan_catalog",
        interval_seconds=6 * 3600,
        parameters={"verify_checksums": False},
        description="Re-scan the source data folder for new, changed or corrupted files.",
    ),
    ScheduledJob(
        name="data_health",
        task="check_data_health",
        interval_seconds=3600,
        parameters={},
        description="Re-evaluate data completeness, staleness and validity.",
    ),
    ScheduledJob(
        name="dynamic_refresh",
        task="process_dynamic",
        interval_seconds=3 * 3600,
        parameters={"force": False},
        description="Re-normalize weather, snow and Avalanche Canada products.",
    ),
    ScheduledJob(
        name="stale_derived_products",
        task="process_terrain",
        interval_seconds=24 * 3600,
        parameters={"force": False},
        description=(
            "Regenerate derived terrain products if their source fingerprint changed. Cheap when "
            "nothing changed, because the content-hash cache short-circuits it."
        ),
    ),
    ScheduledJob(
        name="temp_cleanup",
        task="clean_temp",
        interval_seconds=12 * 3600,
        parameters={"max_age_hours": 24.0},
        description="Delete temporary files older than a day.",
    ),
)


def describe_schedule(settings: Settings) -> list[dict[str, Any]]:
    return [
        {
            "name": job.name,
            "task": job.task,
            "interval_seconds": job.interval_seconds,
            "interval_human": f"every {job.interval_seconds / 3600:.0f} h",
            "parameters": job.parameters,
            "description": job.description,
            "requires_network": job.requires_network,
            "enabled": settings.scheduler_enabled,
            "rerun_safe": True,
        }
        for job in SCHEDULE
    ]
