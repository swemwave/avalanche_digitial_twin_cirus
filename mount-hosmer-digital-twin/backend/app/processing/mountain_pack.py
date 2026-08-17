"""Strict bake-time contract for portable mountain source data.

The running service never imports this module. A mountain pack identifies the
analysis grid and the allow-listed source assets used by the offline bake. It is
deliberately small: data without a declared scientific purpose, source, licence,
units, and path cannot silently enter the model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.paths import safe_source_path
from app.core.settings import Settings

PACK_SCHEMA_VERSION = 1

AssetRole = Literal[
    "aoi",
    "elevation_primary",
    "elevation_lidar",
    "elevation_fallback",
    "surface_lidar",
    "landcover",
    "imagery_red",
    "imagery_green",
    "imagery_blue",
    "observations",
    "pois",
    "exposure_features",
]
#: Roles that describe what is in the valley rather than what the mountain is. They
#: are bound by :meth:`MountainPack.validate_asset_meaning` to purpose ``exposure``.
EXPOSURE_ROLES = frozenset({"pois", "exposure_features"})
AssetPurpose = Literal["model_input", "display_only", "validation", "exposure"]
AssetAdapter = Literal[
    "geojson",
    "single_raster",
    "categorical_raster",
    "geobc_lidar_year_tiles",
]


class MountainPackError(ValueError):
    """Raised when a pack cannot be trusted as bake input."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceStatement(StrictModel):
    provider: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    licence: str = Field(min_length=1)


class VerticalDatum(StrictModel):
    status: Literal["known", "unknown"]
    name: str | None = None

    @model_validator(mode="after")
    def validate_name(self) -> "VerticalDatum":
        if self.status == "known" and not self.name:
            raise ValueError("A known vertical datum requires its name.")
        if self.status == "unknown" and self.name is not None:
            raise ValueError("An unknown vertical datum must not carry an invented name.")
        return self


class GridSpec(StrictModel):
    analysis_crs: str = Field(pattern=r"^EPSG:\d+$")
    coordinate_order: Literal["easting,northing"] = "easting,northing"
    bounds: tuple[float, float, float, float]
    resolution_m: float = Field(gt=0)
    vertical_datum: VerticalDatum

    @model_validator(mode="after")
    def validate_bounds(self) -> "GridSpec":
        west, south, east, north = self.bounds
        if not all(math.isfinite(value) for value in self.bounds):
            raise ValueError("Grid bounds must be finite.")
        if west >= east or south >= north:
            raise ValueError("Grid bounds must be ordered west, south, east, north.")
        return self


class PackAsset(StrictModel):
    href: str = Field(min_length=1)
    adapter: AssetAdapter
    purpose: AssetPurpose
    required: bool
    units: str = Field(min_length=1)
    native_resolution_m: float | None = Field(default=None, gt=0)
    captured_at_utc: str | None = None
    cloud_percent: float | None = Field(default=None, ge=0, le=100)
    visual_context_only: bool = False
    source: SourceStatement

    @field_validator("href")
    @classmethod
    def relative_safe_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Asset hrefs must be relative paths inside the read-only data root.")
        if any(character in value for character in ("*", "?", "[", "]")):
            raise ValueError("Asset hrefs must identify one file or directory, not a glob.")
        return path.as_posix()


class MountainPack(StrictModel):
    schema_version: Literal[1]
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    center_wgs84: tuple[float, float]
    grid: GridSpec
    model_profile: str = Field(min_length=1)
    model_calibrated_locally: bool = False
    assets: dict[AssetRole, PackAsset]

    @field_validator("id")
    @classmethod
    def slug_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("Pack id must be a lowercase hyphenated slug.")
        return value

    @field_validator("center_wgs84")
    @classmethod
    def valid_center(cls, value: tuple[float, float]) -> tuple[float, float]:
        longitude, latitude = value
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("center_wgs84 must use longitude, latitude order.")
        return value

    @model_validator(mode="after")
    def validate_asset_meaning(self) -> "MountainPack":
        required_roles = {"aoi", "landcover"}
        missing = sorted(required_roles - set(self.assets))
        if missing:
            raise ValueError(f"Mountain pack is missing required asset roles: {missing}.")

        elevation_roles = {"elevation_primary", "elevation_lidar"} & set(self.assets)
        if len(elevation_roles) != 1:
            raise ValueError(
                "Mountain pack must declare exactly one primary elevation role: "
                "'elevation_primary' for a portable single DEM, or the legacy "
                "'elevation_lidar' role for GeoBC year tiles."
            )
        primary_elevation_role = next(iter(elevation_roles))
        primary_elevation = self.assets[primary_elevation_role]
        if not primary_elevation.required or primary_elevation.purpose != "model_input":
            raise ValueError(
                f"{primary_elevation_role} must be a required model_input; missing elevation "
                "cannot be treated as an optional or display-only surface."
            )

        if "elevation_primary" in self.assets:
            asset = self.assets["elevation_primary"]
            if asset.adapter != "single_raster":
                raise ValueError(
                    "elevation_primary requires adapter='single_raster'; provider-specific "
                    "tile layouts require their own reviewed adapter."
                )
        if "elevation_lidar" in self.assets:
            asset = self.assets["elevation_lidar"]
            if asset.adapter != "geobc_lidar_year_tiles":
                raise ValueError(
                    "The legacy elevation_lidar role requires "
                    "adapter='geobc_lidar_year_tiles'. Use elevation_primary with "
                    "adapter='single_raster' for a portable plain DEM."
                )
        if "elevation_fallback" in self.assets:
            asset = self.assets["elevation_fallback"]
            if asset.adapter != "single_raster":
                raise ValueError("elevation_fallback requires adapter='single_raster'.")
        if "surface_lidar" in self.assets:
            asset = self.assets["surface_lidar"]
            if asset.adapter != "geobc_lidar_year_tiles":
                raise ValueError(
                    "surface_lidar currently requires adapter='geobc_lidar_year_tiles'."
                )

        imagery_roles = {"imagery_red", "imagery_green", "imagery_blue"}
        present_imagery = imagery_roles & set(self.assets)
        if present_imagery and present_imagery != imagery_roles:
            raise ValueError("Natural-colour imagery requires red, green, and blue assets together.")

        for role, asset in self.assets.items():
            if role.startswith("imagery_") and (
                asset.purpose != "display_only" or not asset.visual_context_only
            ):
                raise ValueError(
                    f"{role} must be display_only and visual_context_only; imagery cannot "
                    "silently enter hazard calculations."
                )
            if role.startswith("imagery_") and not asset.captured_at_utc:
                raise ValueError(f"{role} requires an acquisition timestamp.")
            if role.startswith("imagery_") and (
                asset.native_resolution_m is None or asset.cloud_percent is None
            ):
                raise ValueError(f"{role} requires source resolution and measured cloud cover.")
            if role == "observations" and asset.purpose != "validation":
                raise ValueError("Observations may support validation, not direct hazard input.")
            # Exposure data says what is in the valley. It is now permitted to reach
            # the assessment, but only through one named door: it never enters the
            # release model -- avycore.hazard.risk does not import it, is not passed
            # it, and never sees it -- and it may act only as the exposure term of
            # the composite hazard index, where it can raise a zone's index and can
            # never lower one. Binding the role to purpose 'exposure' (and the
            # purpose to these roles) is what keeps that door the only one, so the
            # relaxation is enforced here rather than removed. See
            # docs/limitations.md.
            if role in EXPOSURE_ROLES and asset.purpose != "exposure":
                raise ValueError(
                    f"{role} must remain an exposure asset, separate from hazard. Exposure may "
                    "raise the consequence term of the composite index and must never enter the "
                    "release model."
                )
            if asset.purpose == "exposure" and role not in EXPOSURE_ROLES:
                raise ValueError(
                    f"Role {role!r} cannot declare purpose 'exposure'; exposure data must be "
                    f"declared under one of {sorted(EXPOSURE_ROLES)} so it stays separable from "
                    "hazard inputs."
                )
        return self

    def asset_path(self, data_root: Path, role: AssetRole) -> Path:
        try:
            asset = self.assets[role]
        except KeyError as exc:
            raise MountainPackError(f"Mountain pack {self.id!r} has no {role!r} asset.") from exc
        return safe_source_path(data_root, asset.href)

    def warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.grid.vertical_datum.status == "unknown":
            warnings.append(
                "Source elevation vertical datum is unknown; elevations must not be presented "
                "as tied to a named vertical reference surface."
            )
        if not self.model_calibrated_locally:
            warnings.append(
                f"Model profile {self.model_profile!r} is not calibrated to observed events "
                f"for {self.name}."
            )
        return warnings


def load_mountain_pack(settings: Settings) -> tuple[MountainPack, dict[str, str]]:
    """Load and hash the configured pack without opening any declared asset."""
    path = settings.mountain_pack_path
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        pack = MountainPack.model_validate(parsed)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MountainPackError(f"Invalid mountain pack {path}: {exc}") from exc
    return pack, {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": str(pack.schema_version),
        "id": pack.id,
    }


def validate_declared_sources(pack: MountainPack, data_root: Path) -> list[str]:
    """Check declared source presence without reading or modifying source data."""
    try:
        from pyproj import CRS

        crs = CRS.from_user_input(pack.grid.analysis_crs)
    except Exception as exc:
        raise MountainPackError(
            f"Analysis CRS {pack.grid.analysis_crs!r} could not be resolved: {exc}"
        ) from exc
    if not crs.is_projected or not crs.axis_info or any(
        axis.unit_conversion_factor != 1.0 for axis in crs.axis_info[:2]
    ):
        raise MountainPackError(
            "Analysis CRS must be projected with metre horizontal units; geographic degrees "
            "cannot be used with resolution_m or runout distances."
        )

    warnings: list[str] = []
    for role, asset in pack.assets.items():
        path = pack.asset_path(data_root, role)
        if not path.exists():
            if asset.required:
                raise MountainPackError(f"Required {role!r} asset is missing: {path}")
            warnings.append(
                f"Optional {role!r} asset is missing and will be unavailable: "
                f"{asset.href} (relative to the selected data root)"
            )
            continue
        if asset.adapter == "geobc_lidar_year_tiles":
            if not path.is_dir() or not any(path.glob("*.tif")):
                raise MountainPackError(f"{role!r} must be a directory containing GeoTIFF tiles.")
        elif not path.is_file():
            raise MountainPackError(f"{role!r} must identify one file: {path}")
    return warnings
