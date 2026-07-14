"""Relational schema.

Two decisions worth defending.

**Rasters are not in the database.** A GeoTIFF is a large binary that the science
code reads with rasterio, windowed, off the filesystem. Putting it in a BLOB
column would force it through the ORM to be read at all, break the content-hash
cache, and make the database unbackupable at its natural size. The database stores
the *path* and the *checksum*; the file is the artefact.

**Geometry is stored as GeoJSON text plus a bounding box.** This is the choice
that lets the same schema run on SQLite (the one-click offline launcher, which
cannot depend on a database server) and on PostgreSQL/PostGIS (a server
deployment) with no code change. The bbox columns give cheap spatial filtering on
both. If PostGIS is present, the geometry text can be promoted to a real geometry
column in a follow-on migration without touching the application.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JsonMixin:
    """Helpers for the JSON-in-TEXT columns.

    JSON is stored as TEXT rather than as a native JSON column so the schema is
    identical on SQLite and PostgreSQL.
    """

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, default=str)

    @staticmethod
    def loads(value: str | None) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None


class Dataset(Base, JsonMixin):
    """A logical source dataset (LiDAR DEM, ECCC hourly, Sentinel-2 event, ...)."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(255))
    provenance: Mapped[str] = mapped_column(String(32), default="downloaded")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    usable_by_model: Mapped[bool] = mapped_column(Boolean, default=True)
    quality_flags_json: Mapped[str | None] = mapped_column(Text)
    first_seen_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    entries: Mapped[list["CatalogEntry"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class CatalogEntry(Base, JsonMixin):
    """One physical file in the source data, with everything discovery found."""

    __tablename__ = "catalog_entries"
    __table_args__ = (
        Index("ix_catalog_category_usable", "category", "usable_by_model"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    catalog_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"))

    relative_path: Mapped[str] = mapped_column(String(512), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(64), index=True)
    file_type: Mapped[str] = mapped_column(String(32))

    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)

    crs: Mapped[str | None] = mapped_column(String(64))
    bounds_json: Mapped[str | None] = mapped_column(Text)
    resolution_m: Mapped[float | None] = mapped_column(Float)
    band_count: Mapped[int | None] = mapped_column(Integer)
    nodata_value: Mapped[float | None] = mapped_column(Float)
    units: Mapped[str | None] = mapped_column(String(32))

    acquisition_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporal_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provenance: Mapped[str] = mapped_column(String(32), default="downloaded")
    processing_status: Mapped[str] = mapped_column(String(32), default="discovered")

    is_empty: Mapped[bool] = mapped_column(Boolean, default=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    usable_by_model: Mapped[bool] = mapped_column(Boolean, default=True)
    inside_aoi: Mapped[bool | None] = mapped_column(Boolean)
    is_stale: Mapped[bool] = mapped_column(Boolean, default=False)

    quality_flags_json: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column(Text)

    scanned_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    dataset: Mapped[Dataset | None] = relationship(back_populates="entries")


class ProcessingRun(Base, JsonMixin):
    """One execution of a processor, with the content hash that gates its cache."""

    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    processor: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="succeeded")
    input_signature_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    model_version: Mapped[str | None] = mapped_column(String(32))
    started_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    outputs_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class SatelliteEvent(Base, JsonMixin):
    __tablename__ = "satellite_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    acquired_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sentinel_scene: Mapped[str | None] = mapped_column(String(255))
    landsat_scene: Mapped[str | None] = mapped_column(String(255))
    cloud_cover_percent: Mapped[float | None] = mapped_column(Float)
    snow_cover_percent: Mapped[float | None] = mapped_column(Float)
    scene_quality_score: Mapped[float | None] = mapped_column(Float)
    usable: Mapped[bool] = mapped_column(Boolean, default=True)
    summary_json: Mapped[str | None] = mapped_column(Text)


class WeatherSummary(Base, JsonMixin):
    __tablename__ = "weather_summaries"
    __table_args__ = (UniqueConstraint("station_key", "valid_time_utc", name="uq_weather_station_time"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    station_key: Mapped[str] = mapped_column(String(64), index=True)
    station_name: Mapped[str | None] = mapped_column(String(160))
    valid_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    distance_to_aoi_km: Mapped[float | None] = mapped_column(Float)
    features_json: Mapped[str | None] = mapped_column(Text)


class ModelVersion(Base, JsonMixin):
    """An immutable record of a model configuration that produced results."""

    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("version", "config_sha256", name="uq_model_version_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), index=True)
    config_sha256: Mapped[str] = mapped_column(String(64), index=True)
    config_json: Mapped[str] = mapped_column(Text)
    software_version: Mapped[str] = mapped_column(String(32))
    is_calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    calibration_note: Mapped[str | None] = mapped_column(Text)
    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Scenario(Base, JsonMixin):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_key: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(160))
    is_preset: Mapped[bool] = mapped_column(Boolean, default=False)
    inputs_json: Mapped[str] = mapped_column(Text)
    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Analysis(Base, JsonMixin):
    """One reproducible answer to 'what is the mountain doing under these conditions'."""

    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analysis_mode_time", "mode", "valid_time_utc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    mode: Mapped[str] = mapped_column(String(24), index=True)  # historical | current | scenario
    valid_time_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_id: Mapped[str | None] = mapped_column(String(64))
    scenario_id: Mapped[int | None] = mapped_column(ForeignKey("scenarios.id"))

    model_version: Mapped[str] = mapped_column(String(32), index=True)
    config_sha256: Mapped[str] = mapped_column(String(64))
    software_version: Mapped[str] = mapped_column(String(32))

    hazard_score: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    release_zone_count: Mapped[int] = mapped_column(Integer, default=0)
    instability_withheld: Mapped[bool] = mapped_column(Boolean, default=False)

    duration_seconds: Mapped[float | None] = mapped_column(Float)
    output_dir: Mapped[str] = mapped_column(String(512))
    payload_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)

    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    zones: Mapped[list["ReleaseZoneRecord"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )
    simulations: Mapped[list["Simulation"]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class ReleaseZoneRecord(Base, JsonMixin):
    __tablename__ = "release_zones"
    __table_args__ = (
        UniqueConstraint("analysis_id", "zone_id", name="uq_zone_per_analysis"),
        # Cheap spatial filtering that works identically on SQLite and PostgreSQL.
        Index("ix_zone_bbox", "min_lon", "min_lat", "max_lon", "max_lat"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)
    zone_id: Mapped[str] = mapped_column(String(24), index=True)

    geometry_geojson: Mapped[str] = mapped_column(Text)
    min_lon: Mapped[float] = mapped_column(Float)
    min_lat: Mapped[float] = mapped_column(Float)
    max_lon: Mapped[float] = mapped_column(Float)
    max_lat: Mapped[float] = mapped_column(Float)

    area_m2: Mapped[float] = mapped_column(Float)
    mean_slope_deg: Mapped[float | None] = mapped_column(Float)
    max_slope_deg: Mapped[float | None] = mapped_column(Float)
    dominant_aspect_deg: Mapped[float | None] = mapped_column(Float)
    dominant_aspect_compass: Mapped[str | None] = mapped_column(String(8))
    elevation_min_m: Mapped[float | None] = mapped_column(Float)
    elevation_max_m: Mapped[float | None] = mapped_column(Float)

    terrain_susceptibility_score: Mapped[float | None] = mapped_column(Float)
    dynamic_instability_score: Mapped[float | None] = mapped_column(Float)
    wind_loading_score: Mapped[float | None] = mapped_column(Float)
    snow_depth_index: Mapped[float | None] = mapped_column(Float)
    estimated_release_score: Mapped[float | None] = mapped_column(Float)

    properties_json: Mapped[str] = mapped_column(Text)

    analysis: Mapped[Analysis] = relationship(back_populates="zones")


class Simulation(Base, JsonMixin):
    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(primary_key=True)
    simulation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), index=True)

    simulation_mode: Mapped[str] = mapped_column(String(24), index=True)  # fast | advanced
    engine: Mapped[str] = mapped_column(String(64))
    release_size: Mapped[str] = mapped_column(String(16))
    random_seed: Mapped[int | None] = mapped_column(Integer)
    zone_ids_json: Mapped[str] = mapped_column(Text)

    model_version: Mapped[str] = mapped_column(String(32))
    config_sha256: Mapped[str] = mapped_column(String(64))

    hazard_score: Mapped[float | None] = mapped_column(Float)
    consequence_score: Mapped[float | None] = mapped_column(Float)
    combined_risk_score: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(24), index=True)

    runout_area_m2: Mapped[float | None] = mapped_column(Float)
    uncertainty_area_m2: Mapped[float | None] = mapped_column(Float)
    max_velocity_ms: Mapped[float | None] = mapped_column(Float)

    duration_seconds: Mapped[float | None] = mapped_column(Float)
    output_dir: Mapped[str] = mapped_column(String(512))
    payload_json: Mapped[str | None] = mapped_column(Text)

    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    analysis: Mapped[Analysis] = relationship(back_populates="simulations")
    assets: Mapped[list["ExposedAssetRecord"]] = relationship(
        back_populates="simulation", cascade="all, delete-orphan"
    )


class ExposedAssetRecord(Base, JsonMixin):
    __tablename__ = "exposed_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    simulation_id: Mapped[int] = mapped_column(ForeignKey("simulations.id"), index=True)

    asset_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    geometry_type: Mapped[str | None] = mapped_column(String(32))
    length_in_runout_m: Mapped[float | None] = mapped_column(Float)
    max_flow_intensity: Mapped[float | None] = mapped_column(Float)
    max_flow_velocity_ms: Mapped[float | None] = mapped_column(Float)
    distance_from_release_m: Mapped[float | None] = mapped_column(Float)
    properties_json: Mapped[str | None] = mapped_column(Text)

    simulation: Mapped[Simulation] = relationship(back_populates="assets")


class Job(Base, JsonMixin):
    """A unit of background work. The contract is deliberately broker-agnostic."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_job_state_created", "state", "created_utc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)

    # Two requests carrying the same idempotency key must not do the work twice.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)

    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[str | None] = mapped_column(String(255))

    parameters_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)

    result_id: Mapped[str | None] = mapped_column(String(64), index=True)
    generated_layer_ids_json: Mapped[str | None] = mapped_column(Text)

    model_version: Mapped[str | None] = mapped_column(String(32))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)


class AuditRecord(Base, JsonMixin):
    """Who or what ran which model, when, and with what result."""

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    model_version: Mapped[str | None] = mapped_column(String(32))
    detail_json: Mapped[str | None] = mapped_column(Text)
    created_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
