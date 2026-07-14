"""Persisting analyses, simulations and audit records.

The on-disk JSON stays the source of truth for the full payload (it holds the
rasters' neighbours and is what a scientist would archive). The database holds the
*queryable* projection of it: scores, geometry bboxes, model versions, timestamps.
That way "show me every High-risk simulation from January" is one indexed query,
and "give me everything about simulation X" is one file read.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.core.paths import analyses_dir, simulations_dir
from app.core.settings import Settings
from app.storage.database import session_scope
from app.storage.models import (
    Analysis,
    AuditRecord,
    ExposedAssetRecord,
    ModelVersion,
    ReleaseZoneRecord,
    Simulation,
)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    lons: list[float] = []
    lats: list[float] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(value, (int, float)) for value in node[:2])
        ):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates", []))
    if not lons:
        return 0.0, 0.0, 0.0, 0.0
    return min(lons), min(lats), max(lons), max(lats)


def record_model_version(session: Session, payload: dict[str, Any], settings: Settings) -> None:
    model = payload.get("model", {})
    version = model.get("model_version")
    sha = model.get("config_sha256")
    if not version or not sha:
        return
    existing = session.scalar(
        select(ModelVersion).where(
            ModelVersion.version == version, ModelVersion.config_sha256 == sha
        )
    )
    if existing:
        return
    session.add(
        ModelVersion(
            version=version,
            config_sha256=sha,
            config_json=json.dumps(model),
            software_version=settings.app_version,
            is_calibrated=False,
            calibration_note=(
                "This model has never been validated against an observed Mount Hosmer avalanche. "
                "No historical avalanche record exists for this mountain."
            ),
        )
    )


def save_analysis(settings: Settings, payload: dict[str, Any], *, correlation_id: str | None = None) -> int:
    """Persist the queryable projection of an analysis. Returns its row id.

    Saving is idempotent on ``analysis_id``. An analysis id names a specific,
    reproducible computation, so re-saving one means "this record was recomputed",
    not "here is a second, different analysis". The old projection is replaced.
    Without this, a retried job or a duplicated request raises an IntegrityError on
    the unique constraint, and the job runner's idempotency key -- which exists
    precisely so that work can be safely repeated -- would have nothing to lean on.
    """
    with session_scope(settings) as session:
        record_model_version(session, payload, settings)

        model = payload.get("model", {})
        instability = payload.get("instability", {}) or {}
        zones = (payload.get("release_zones") or {}).get("features", [])

        existing = session.scalar(
            select(Analysis).where(Analysis.analysis_id == payload["analysis_id"])
        )
        if existing is not None:
            # Drop the child rows and rebuild them, so a recomputed analysis cannot
            # leave stale release zones from the previous run behind.
            session.execute(
                delete(ReleaseZoneRecord).where(ReleaseZoneRecord.analysis_id == existing.id)
            )
            session.delete(existing)
            session.flush()

        analysis = Analysis(
            analysis_id=payload["analysis_id"],
            mode=payload["mode"],
            valid_time_utc=_parse_time(payload.get("valid_time_utc")) or datetime.now(),
            event_id=payload.get("event_id"),
            model_version=model.get("model_version", "unknown"),
            config_sha256=model.get("config_sha256", ""),
            software_version=payload.get("software_version", settings.app_version),
            hazard_score=payload.get("hazard_score"),
            confidence_score=payload.get("confidence_score"),
            release_zone_count=len(zones),
            instability_withheld=bool(instability.get("score_withheld")),
            duration_seconds=payload.get("duration_seconds"),
            output_dir=str((settings.runtime_root / "analyses" / payload["analysis_id"]).as_posix()),
            payload_json=json.dumps(_slim_analysis(payload)),
            warnings_json=json.dumps(payload.get("warnings", [])),
        )
        session.add(analysis)
        session.flush()

        for feature in zones:
            properties = feature.get("properties", {})
            geometry = feature.get("geometry") or {}
            min_lon, min_lat, max_lon, max_lat = _bbox(geometry)
            session.add(
                ReleaseZoneRecord(
                    analysis_id=analysis.id,
                    zone_id=str(properties.get("zone_id")),
                    geometry_geojson=json.dumps(geometry),
                    min_lon=min_lon,
                    min_lat=min_lat,
                    max_lon=max_lon,
                    max_lat=max_lat,
                    area_m2=float(properties.get("area_m2", 0.0)),
                    mean_slope_deg=properties.get("mean_slope_deg"),
                    max_slope_deg=properties.get("max_slope_deg"),
                    dominant_aspect_deg=properties.get("dominant_aspect_deg"),
                    dominant_aspect_compass=properties.get("dominant_aspect_compass"),
                    elevation_min_m=properties.get("elevation_min_m"),
                    elevation_max_m=properties.get("elevation_max_m"),
                    terrain_susceptibility_score=properties.get("terrain_susceptibility_score"),
                    dynamic_instability_score=properties.get("dynamic_instability_score"),
                    wind_loading_score=properties.get("wind_loading_score"),
                    snow_depth_index=properties.get("snow_depth_index"),
                    estimated_release_score=properties.get("estimated_release_score"),
                    properties_json=json.dumps(properties),
                )
            )

        session.add(
            AuditRecord(
                action="analysis.run",
                entity_type="analysis",
                entity_id=payload["analysis_id"],
                actor="api",
                correlation_id=correlation_id,
                model_version=model.get("model_version"),
                detail_json=json.dumps(
                    {
                        "mode": payload.get("mode"),
                        "valid_time_utc": payload.get("valid_time_utc"),
                        "hazard_score": payload.get("hazard_score"),
                        "confidence_score": payload.get("confidence_score"),
                        "zone_count": len(zones),
                        "duration_seconds": payload.get("duration_seconds"),
                    }
                ),
            )
        )
        return analysis.id


def save_simulation(settings: Settings, payload: dict[str, Any], *, correlation_id: str | None = None) -> int:
    with session_scope(settings) as session:
        analysis = session.scalar(
            select(Analysis).where(Analysis.analysis_id == payload["analysis_id"])
        )
        if analysis is None:
            raise LookupError(
                f"Cannot store simulation {payload['simulation_id']}: its analysis "
                f"{payload['analysis_id']} is not in the database."
            )

        assessment = payload.get("assessment", {}) or {}
        runout = payload.get("runout", {}) or {}
        model = payload.get("model", {})

        # Idempotent on simulation_id, for the same reason save_analysis is: a
        # simulation id names one reproducible run, and re-running it with the same
        # seed must overwrite rather than collide.
        previous = session.scalar(
            select(Simulation).where(Simulation.simulation_id == payload["simulation_id"])
        )
        if previous is not None:
            session.execute(
                delete(ExposedAssetRecord).where(ExposedAssetRecord.simulation_id == previous.id)
            )
            session.delete(previous)
            session.flush()

        simulation = Simulation(
            simulation_id=payload["simulation_id"],
            analysis_id=analysis.id,
            simulation_mode=payload["simulation_mode"],
            engine=payload.get("engine", ""),
            release_size=payload.get("release_size", "medium"),
            random_seed=payload.get("random_seed"),
            zone_ids_json=json.dumps(payload.get("zone_ids", [])),
            model_version=model.get("model_version", "unknown"),
            config_sha256=model.get("config_sha256", ""),
            hazard_score=assessment.get("hazardScore"),
            consequence_score=assessment.get("consequenceScore"),
            combined_risk_score=assessment.get("combinedRiskScore"),
            confidence_score=assessment.get("confidenceScore"),
            risk_level=assessment.get("riskLevel"),
            runout_area_m2=runout.get("core_area_m2"),
            uncertainty_area_m2=runout.get("uncertainty_area_m2"),
            max_velocity_ms=runout.get("max_velocity_ms"),
            duration_seconds=payload.get("duration_seconds"),
            output_dir=str(
                (settings.runtime_root / "simulations" / payload["simulation_id"]).as_posix()
            ),
            payload_json=json.dumps(_slim_simulation(payload)),
        )
        session.add(simulation)
        session.flush()

        for asset in (payload.get("exposure", {}) or {}).get("assets", []):
            session.add(
                ExposedAssetRecord(
                    simulation_id=simulation.id,
                    asset_id=str(asset.get("asset_id")),
                    category=str(asset.get("category")),
                    name=asset.get("name"),
                    geometry_type=asset.get("geometry_type"),
                    length_in_runout_m=asset.get("length_in_runout_m"),
                    max_flow_intensity=asset.get("max_flow_intensity"),
                    max_flow_velocity_ms=asset.get("max_flow_velocity_ms"),
                    distance_from_release_m=asset.get("distance_from_release_m"),
                    properties_json=json.dumps(asset.get("osm_properties", {})),
                )
            )

        session.add(
            AuditRecord(
                action="simulation.run",
                entity_type="simulation",
                entity_id=payload["simulation_id"],
                actor="api",
                correlation_id=correlation_id,
                model_version=model.get("model_version"),
                detail_json=json.dumps(
                    {
                        "analysis_id": payload.get("analysis_id"),
                        "mode": payload.get("simulation_mode"),
                        "release_size": payload.get("release_size"),
                        "seed": payload.get("random_seed"),
                        "risk_level": assessment.get("riskLevel"),
                        "confidence": assessment.get("confidenceScore"),
                    }
                ),
            )
        )
        return simulation.id


def _slim_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky fields; the full record is on disk."""
    slim = {key: value for key, value in payload.items() if key not in {"release_zones", "conditions"}}
    slim["conditions_summary"] = {
        key: value
        for key, value in (payload.get("conditions") or {}).items()
        if key != "values"
    }
    return slim


def _slim_simulation(payload: dict[str, Any]) -> dict[str, Any]:
    slim = {key: value for key, value in payload.items() if key not in {"runout", "exposure"}}
    runout = payload.get("runout", {}) or {}
    slim["runout_summary"] = {
        key: value
        for key, value in runout.items()
        if key
        not in {"runout_polygons", "uncertainty_polygons", "main_paths", "alternate_paths"}
    }
    slim["exposure_summary"] = (payload.get("exposure", {}) or {}).get("summary")
    return slim


def list_analyses(settings: Settings, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        query = select(Analysis).order_by(desc(Analysis.created_utc)).limit(limit)
        if not include_archived:
            query = query.where(Analysis.archived.is_(False))
        return [
            {
                "analysis_id": row.analysis_id,
                "mode": row.mode,
                "valid_time_utc": row.valid_time_utc.isoformat() if row.valid_time_utc else None,
                "event_id": row.event_id,
                "hazard_score": row.hazard_score,
                "confidence_score": row.confidence_score,
                "release_zone_count": row.release_zone_count,
                "instability_withheld": row.instability_withheld,
                "model_version": row.model_version,
                "duration_seconds": row.duration_seconds,
                "created_utc": row.created_utc.isoformat() if row.created_utc else None,
                "simulation_count": len(row.simulations),
            }
            for row in session.scalars(query)
        ]


def list_simulations(settings: Settings, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        query = select(Simulation).order_by(desc(Simulation.created_utc)).limit(limit)
        if not include_archived:
            query = query.where(Simulation.archived.is_(False))
        return [
            {
                "simulation_id": row.simulation_id,
                "analysis_id": row.analysis.analysis_id if row.analysis else None,
                "simulation_mode": row.simulation_mode,
                "engine": row.engine,
                "release_size": row.release_size,
                "random_seed": row.random_seed,
                "zone_ids": json.loads(row.zone_ids_json or "[]"),
                "risk_level": row.risk_level,
                "hazard_score": row.hazard_score,
                "consequence_score": row.consequence_score,
                "combined_risk_score": row.combined_risk_score,
                "confidence_score": row.confidence_score,
                "runout_area_m2": row.runout_area_m2,
                "max_velocity_ms": row.max_velocity_ms,
                "model_version": row.model_version,
                "duration_seconds": row.duration_seconds,
                "created_utc": row.created_utc.isoformat() if row.created_utc else None,
                "exposed_asset_count": len(row.assets),
            }
            for row in session.scalars(query)
        ]


def get_analysis(settings: Settings, analysis_id: str) -> dict[str, Any] | None:
    """The full analysis payload.

    Read from disk, not from the database. The on-disk JSON is the record a
    scientist would archive -- it carries the release-zone geometry and every input
    with its provenance -- while the database holds only the queryable projection of
    it. Serving the projection here would silently return a thinner object than the
    one the analysis actually produced.
    """
    path = analyses_dir(settings) / analysis_id / "analysis.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def get_simulation(settings: Settings, simulation_id: str) -> dict[str, Any] | None:
    path = simulations_dir(settings) / simulation_id / "simulation.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def exposed_assets(settings: Settings, simulation_id: str) -> list[dict[str, Any]] | None:
    """Everything the simulated runout intersects. None when the simulation is unknown."""
    payload = get_simulation(settings, simulation_id)
    if payload is None:
        return None
    return list((payload.get("exposure") or {}).get("assets") or [])


def archive_simulation(settings: Settings, simulation_id: str) -> bool:
    with session_scope(settings) as session:
        row = session.scalar(select(Simulation).where(Simulation.simulation_id == simulation_id))
        if row is None:
            return False
        row.archived = True
        session.add(
            AuditRecord(
                action="simulation.archive",
                entity_type="simulation",
                entity_id=simulation_id,
                actor="api",
            )
        )
        return True


def audit_trail(settings: Settings, limit: int = 100) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        rows = session.scalars(
            select(AuditRecord).order_by(desc(AuditRecord.created_utc)).limit(limit)
        )
        return [
            {
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "actor": row.actor,
                "model_version": row.model_version,
                "correlation_id": row.correlation_id,
                "detail": json.loads(row.detail_json) if row.detail_json else None,
                "created_utc": row.created_utc.isoformat() if row.created_utc else None,
            }
            for row in rows
        ]


def model_versions(settings: Settings) -> list[dict[str, Any]]:
    with session_scope(settings) as session:
        rows = session.scalars(select(ModelVersion).order_by(desc(ModelVersion.created_utc)))
        return [
            {
                "version": row.version,
                "config_sha256": row.config_sha256,
                "software_version": row.software_version,
                "is_calibrated": row.is_calibrated,
                "calibration_note": row.calibration_note,
                "created_utc": row.created_utc.isoformat() if row.created_utc else None,
            }
            for row in rows
        ]
