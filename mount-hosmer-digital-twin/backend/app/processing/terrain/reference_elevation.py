"""Offline, provider-neutral reference-elevation derivation and storage.

The contract deliberately does not activate a forcing migration.  It binds an
explicit geographic target and sampling convention to a fully compatible
terrain bake, preserves the legacy reference separately, and publishes a
content-addressed artifact that can be reviewed before any ConditionPack is
regenerated with a different elevation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.bake_identity import sha256_file, validate_bake
from app.baked import Reprojector


REFERENCE_ELEVATION_SCHEMA = "reference-elevation-contract-v1"
REFERENCE_ELEVATION_STORAGE_SCHEMA = "reference-elevation-storage-v1"
REFERENCE_ELEVATION_FILENAME = "reference-elevation.json"
CHECKSUMS_FILENAME = "checksums.json"
SAMPLING_METHOD = "four-cell-bilinear-at-cell-centres"
SAMPLING_METHOD_VERSION = "four-cell-bilinear-at-cell-centres-v1"
DISCLAIMER = (
    "Experimental research prototype only; not an operational avalanche forecast, not a "
    "probability, and never a replacement for Avalanche Canada guidance or field assessment."
)


class ReferenceElevationError(ValueError):
    """Raised when derivation inputs or stored artifacts are ambiguous or corrupt."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BakeLineage(StrictModel):
    bake_schema: str
    bake_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mountain_pack_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_count: int = Field(gt=0)
    elevation_layer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    terrain_source_layer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grid_crs: str
    horizontal_datum: str
    vertical_datum_status: Literal["known", "unknown"]
    vertical_datum_name: str | None
    source_unit: Literal["metres"]
    output_unit: Literal["metres"]

    @model_validator(mode="after")
    def datum_is_consistent(self) -> "BakeLineage":
        if self.vertical_datum_status == "known" and not self.vertical_datum_name:
            raise ValueError("A known vertical datum requires a name.")
        if self.vertical_datum_status == "unknown" and self.vertical_datum_name is not None:
            raise ValueError("An unknown vertical datum cannot have a name.")
        return self


class GridContract(StrictModel):
    coordinate_order: Literal["easting,northing"]
    raster_index_order: Literal["row,col"]
    affine_order: Literal["a,b,c,d,e,f"]
    pixel_convention: Literal["affine maps pixel edges; array values represent cell centres"]
    nearest_internal_edge_tie_convention: Literal[
        "select east cell for column ties and south cell for row ties"
    ]
    resolution_m: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    transform: tuple[float, float, float, float, float, float]


class TargetCoordinate(StrictModel):
    geographic_coordinate_order: Literal["longitude,latitude"]
    longitude_deg: float = Field(ge=-180, le=180)
    latitude_deg: float = Field(ge=-90, le=90)
    projected_coordinate_order: Literal["easting,northing"]
    projected_easting_m: float
    projected_northing_m: float
    pixel_edge_col: float
    pixel_edge_row: float
    lattice_inversion_residual_m: float = Field(ge=0)


class FootprintCell(StrictModel):
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    center_easting_m: float
    center_northing_m: float
    elevation_m: float | None
    elevation_masked: bool
    terrain_source_code: int | None
    terrain_source_label: str | None
    terrain_source_masked: bool
    required_inputs_masked: bool
    weight: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def mask_is_consistent(self) -> "FootprintCell":
        if self.required_inputs_masked != (
            self.weight > 0 and (self.elevation_masked or self.terrain_source_masked)
        ):
            raise ValueError("The combined footprint mask is inconsistent.")
        if self.elevation_masked != (self.elevation_m is None):
            raise ValueError("Masked elevation must remain null.")
        if self.terrain_source_masked != (self.terrain_source_code is None):
            raise ValueError("Masked terrain source must remain null.")
        return self


class SamplingCandidate(StrictModel):
    method: Literal[
        "containing-cell-centre",
        "nearest-cell-centre",
        "four-cell-bilinear-at-cell-centres",
    ]
    method_version: str
    status: Literal["available", "masked_required_input", "outside_full_footprint"]
    elevation_m: float | None
    footprint: tuple[FootprintCell, ...]
    weight_sum: float

    @model_validator(mode="after")
    def availability_is_consistent(self) -> "SamplingCandidate":
        available = self.status == "available"
        if available != (self.elevation_m is not None):
            raise ValueError("Sampling status and elevation availability conflict.")
        expected = 4 if self.method == SAMPLING_METHOD else 1
        if self.status != "outside_full_footprint" and len(self.footprint) != expected:
            raise ValueError("Sampling footprint has an unexpected cell count.")
        if available and not math.isclose(self.weight_sum, 1.0, abs_tol=1e-12):
            raise ValueError("Available sampling weights must sum to one.")
        if available and any(cell.required_inputs_masked for cell in self.footprint):
            raise ValueError("Available sampling cannot include a masked required input.")
        return self


class LegacyReference(StrictModel):
    name: Literal["legacy_pre_contract_reference_elevation"]
    elevation_m: float
    derivation_status: Literal["reconstructed_not_proven"]
    reconstruction: str
    activated_in_existing_condition_packs: bool


class Selection(StrictModel):
    selected_method: Literal["four-cell-bilinear-at-cell-centres"]
    selected_method_version: Literal["four-cell-bilinear-at-cell-centres-v1"]
    proposed_reference_elevation_m: float | None
    difference_from_legacy_m: float | None
    activation_status: Literal["not_activated_requires_documented_migration_decision"]


class UncertaintyContract(StrictModel):
    numeric_vertical_uncertainty_m: float | None
    vertical_datum_limitation: str
    horizontal_reprojection_limitation: str
    sampling_limitation: str
    source_resolution_limitation: str


class ReferenceElevationDraft(StrictModel):
    schema_version: Literal["reference-elevation-contract-v1"]
    disclaimer: str
    input_bake: BakeLineage
    grid: GridContract
    target: TargetCoordinate
    candidates: dict[str, SamplingCandidate]
    legacy_reference: LegacyReference
    selection: Selection
    uncertainty: UncertaintyContract
    algorithm_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def candidate_keys_are_exact(self) -> "ReferenceElevationDraft":
        required = {"containing_cell", "nearest_cell", "bilinear_four_cell"}
        if set(self.candidates) != required:
            raise ValueError("Reference-elevation candidate set is not exact.")
        return self


class ReferenceElevationContract(ReferenceElevationDraft):
    reference_elevation_id: str = Field(
        pattern=r"^reference-elevation-[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def identity_matches_content(self) -> "ReferenceElevationContract":
        content = self.model_dump(mode="json", exclude={"reference_elevation_id"})
        expected = f"reference-elevation-{_sha256_bytes(_canonical_json_bytes(content))}"
        if self.reference_elevation_id != expected:
            raise ValueError("Reference-elevation identity does not match its content.")
        return self


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_reference_elevation_bytes(contract: ReferenceElevationContract) -> bytes:
    return _canonical_json_bytes(contract.model_dump(mode="json"))


def _invert_reprojection_lattice(
    meta: Mapping[str, Any], longitude_deg: float, latitude_deg: float
) -> tuple[float, float, float]:
    from scipy.optimize import least_squares

    if not -180.0 <= longitude_deg <= 180.0 or not -90.0 <= latitude_deg <= 90.0:
        raise ReferenceElevationError(
            "Target must be provided in (longitude, latitude) degree order."
        )
    lattice = meta.get("reproject", {})
    cols = np.asarray(lattice.get("cols"), dtype="float64")
    rows = np.asarray(lattice.get("rows"), dtype="float64")
    lon = np.asarray(lattice.get("lon"), dtype="float64")
    lat = np.asarray(lattice.get("lat"), dtype="float64")
    if cols.ndim != 1 or rows.ndim != 1 or len(cols) < 2 or len(rows) < 2:
        raise ReferenceElevationError("Baked reprojection lattice axes are invalid.")
    if lon.shape != (len(rows), len(cols)) or lat.shape != lon.shape:
        raise ReferenceElevationError("Baked reprojection lattice shape is inconsistent.")
    if not all(np.all(np.isfinite(value)) for value in (cols, rows, lon, lat)):
        raise ReferenceElevationError("Baked reprojection lattice contains non-finite values.")
    projector = Reprojector(cols, rows, lon, lat)
    longitude_scale = math.cos(math.radians(latitude_deg))
    distance = ((lon - longitude_deg) * longitude_scale) ** 2 + (lat - latitude_deg) ** 2
    initial_row, initial_col = np.unravel_index(np.argmin(distance), distance.shape)

    def residual(pixel: np.ndarray) -> np.ndarray:
        actual_lon, actual_lat = projector(float(pixel[0]), float(pixel[1]))
        return np.asarray(
            [
                (actual_lon - longitude_deg) * longitude_scale,
                actual_lat - latitude_deg,
            ],
            dtype="float64",
        )

    grid = meta["grid"]
    result = least_squares(
        residual,
        x0=np.asarray([cols[initial_col], rows[initial_row]], dtype="float64"),
        bounds=(
            np.asarray([0.0, 0.0]),
            np.asarray([float(grid["width"]), float(grid["height"])]),
        ),
        xtol=1e-14,
        ftol=1e-14,
        gtol=1e-14,
        max_nfev=100,
    )
    if not result.success:
        raise ReferenceElevationError("Could not invert the baked reprojection lattice.")
    col, row = (float(item) for item in result.x)
    actual_lon, actual_lat = projector(col, row)
    mean_radius_m = 6_371_008.8
    dlat = math.radians(actual_lat - latitude_deg)
    dlon = math.radians(actual_lon - longitude_deg)
    lat1 = math.radians(latitude_deg)
    lat2 = math.radians(actual_lat)
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(
        dlon / 2
    ) ** 2
    residual_m = 2 * mean_radius_m * math.asin(math.sqrt(hav))
    if residual_m > 0.05:
        raise ReferenceElevationError(
            f"Baked lattice inversion residual {residual_m:.6f} m exceeds 0.05 m."
        )
    return col, row, residual_m


def _cell(
    elevation: np.ma.MaskedArray,
    terrain_source: np.ma.MaskedArray,
    *,
    row: int,
    col: int,
    west: float,
    north: float,
    resolution_m: float,
    source_labels: Mapping[str, str],
    weight: float,
) -> FootprintCell:
    elevation_masked = bool(np.ma.getmaskarray(elevation)[row, col])
    source_masked = bool(np.ma.getmaskarray(terrain_source)[row, col])
    code = None if source_masked else int(terrain_source[row, col])
    return FootprintCell(
        row=row,
        col=col,
        center_easting_m=west + (col + 0.5) * resolution_m,
        center_northing_m=north - (row + 0.5) * resolution_m,
        elevation_m=None if elevation_masked else float(elevation[row, col]),
        elevation_masked=elevation_masked,
        terrain_source_code=code,
        terrain_source_label=(
            None if code is None else source_labels.get(str(code), "unknown")
        ),
        terrain_source_masked=source_masked,
        required_inputs_masked=(weight > 0 and (elevation_masked or source_masked)),
        weight=float(weight),
    )


def _single_cell_candidate(method: str, cell: FootprintCell) -> SamplingCandidate:
    available = not cell.required_inputs_masked
    return SamplingCandidate(
        method=method,
        method_version=f"{method}-v1",
        status="available" if available else "masked_required_input",
        elevation_m=cell.elevation_m if available else None,
        footprint=(cell,),
        weight_sum=1.0,
    )


def derive_reference_elevation(
    meta: Mapping[str, Any],
    elevation: np.ma.MaskedArray,
    terrain_source: np.ma.MaskedArray,
    *,
    target_longitude_deg: float,
    target_latitude_deg: float,
    legacy_elevation_m: float,
) -> ReferenceElevationContract:
    """Derive a non-activating reference contract from validated in-memory inputs."""

    grid = meta.get("grid", {})
    width, height = int(grid.get("width", 0)), int(grid.get("height", 0))
    if elevation.shape != (height, width) or terrain_source.shape != (height, width):
        raise ReferenceElevationError("Terrain layers do not match the baked grid shape.")
    transform = tuple(float(value) for value in grid.get("transform", ()))
    if len(transform) != 6:
        raise ReferenceElevationError("Baked affine transform must have six coefficients.")
    a, b, west, d, e, north = transform
    resolution_m = float(grid.get("resolution_m", 0.0))
    if (
        resolution_m <= 0
        or b != 0.0
        or d != 0.0
        or not math.isclose(a, resolution_m, abs_tol=1e-12)
        or not math.isclose(e, -resolution_m, abs_tol=1e-12)
    ):
        raise ReferenceElevationError("Only a north-up square-metre grid is supported.")
    edge_col, edge_row, residual_m = _invert_reprojection_lattice(
        meta, target_longitude_deg, target_latitude_deg
    )
    if not (0.0 <= edge_col < width and 0.0 <= edge_row < height):
        raise ReferenceElevationError("Target coordinate lies outside the baked grid.")
    projected_easting = a * edge_col + b * edge_row + west
    projected_northing = d * edge_col + e * edge_row + north
    labels = meta.get("terrain", {}).get("source_codes", {})

    containing_row, containing_col = int(math.floor(edge_row)), int(math.floor(edge_col))
    containing_cell = _cell(
        elevation,
        terrain_source,
        row=containing_row,
        col=containing_col,
        west=west,
        north=north,
        resolution_m=resolution_m,
        source_labels=labels,
        weight=1.0,
    )
    nearest_row = min(height - 1, max(0, int(math.floor(edge_row))))
    nearest_col = min(width - 1, max(0, int(math.floor(edge_col))))
    nearest_cell = _cell(
        elevation,
        terrain_source,
        row=nearest_row,
        col=nearest_col,
        west=west,
        north=north,
        resolution_m=resolution_m,
        source_labels=labels,
        weight=1.0,
    )

    centered_col, centered_row = edge_col - 0.5, edge_row - 0.5
    if math.isclose(centered_col, 0.0, abs_tol=1e-10):
        centered_col = 0.0
    if math.isclose(centered_row, 0.0, abs_tol=1e-10):
        centered_row = 0.0
    if math.isclose(centered_col, width - 1, abs_tol=1e-10):
        centered_col = float(width - 1)
    if math.isclose(centered_row, height - 1, abs_tol=1e-10):
        centered_row = float(height - 1)
    if not (0.0 <= centered_col <= width - 1 and 0.0 <= centered_row <= height - 1):
        bilinear = SamplingCandidate(
            method=SAMPLING_METHOD,
            method_version=SAMPLING_METHOD_VERSION,
            status="outside_full_footprint",
            elevation_m=None,
            footprint=(),
            weight_sum=0.0,
        )
    else:
        left = min(width - 2, int(math.floor(centered_col)))
        top = min(height - 2, int(math.floor(centered_row)))
        x_fraction, y_fraction = centered_col - left, centered_row - top
        weighted_cells = (
            (top, left, (1.0 - x_fraction) * (1.0 - y_fraction)),
            (top, left + 1, x_fraction * (1.0 - y_fraction)),
            (top + 1, left, (1.0 - x_fraction) * y_fraction),
            (top + 1, left + 1, x_fraction * y_fraction),
        )
        cells = tuple(
            _cell(
                elevation,
                terrain_source,
                row=row,
                col=col,
                west=west,
                north=north,
                resolution_m=resolution_m,
                source_labels=labels,
                weight=weight,
            )
            for row, col, weight in weighted_cells
        )
        all_valid = not any(cell.required_inputs_masked for cell in cells)
        value = (
            sum(
                float(cell.elevation_m) * cell.weight
                for cell in cells
                if cell.weight > 0
            )
            if all_valid
            else None
        )
        bilinear = SamplingCandidate(
            method=SAMPLING_METHOD,
            method_version=SAMPLING_METHOD_VERSION,
            status="available" if all_valid else "masked_required_input",
            elevation_m=value,
            footprint=cells,
            weight_sum=sum(cell.weight for cell in cells),
        )

    layers = {record["name"]: record for record in meta.get("layers", [])}
    pack = meta.get("mountain_pack", {})
    processing = meta.get("processing", {})
    sources = meta.get("sources", {})
    datum = grid.get("vertical_datum", {})
    if layers.get("elevation", {}).get("units") != "m above sea level":
        raise ReferenceElevationError("Baked elevation source unit is not the expected metre unit.")
    proposed = bilinear.elevation_m
    draft = ReferenceElevationDraft(
        schema_version=REFERENCE_ELEVATION_SCHEMA,
        disclaimer=DISCLAIMER,
        input_bake=BakeLineage(
            bake_schema=str(meta.get("schema")),
            bake_sha256=str(meta.get("identity", {}).get("bake_sha256", "")),
            mountain_pack_sha256=str(pack.get("sha256", "")),
            processing_sha256=str(processing.get("sha256", "")),
            source_lineage_sha256=str(sources.get("sha256", "")),
            source_file_count=len(sources.get("files", [])),
            elevation_layer_sha256=str(layers.get("elevation", {}).get("sha256", "")),
            terrain_source_layer_sha256=str(
                layers.get("terrain_source", {}).get("sha256", "")
            ),
            grid_crs=str(grid.get("crs")),
            horizontal_datum=(
                "The analysis grid EPSG:26911 is NAD83 / UTM zone 11N. Source-to-grid "
                "datum transformations and source geolocation accuracy remain separate, "
                "incompletely characterized limitations."
            ),
            vertical_datum_status=str(datum.get("status", "unknown")),
            vertical_datum_name=datum.get("name"),
            source_unit="metres",
            output_unit="metres",
        ),
        grid=GridContract(
            coordinate_order="easting,northing",
            raster_index_order="row,col",
            affine_order="a,b,c,d,e,f",
            pixel_convention="affine maps pixel edges; array values represent cell centres",
            nearest_internal_edge_tie_convention=(
                "select east cell for column ties and south cell for row ties"
            ),
            resolution_m=resolution_m,
            width=width,
            height=height,
            transform=transform,
        ),
        target=TargetCoordinate(
            geographic_coordinate_order="longitude,latitude",
            longitude_deg=target_longitude_deg,
            latitude_deg=target_latitude_deg,
            projected_coordinate_order="easting,northing",
            projected_easting_m=projected_easting,
            projected_northing_m=projected_northing,
            pixel_edge_col=edge_col,
            pixel_edge_row=edge_row,
            lattice_inversion_residual_m=residual_m,
        ),
        candidates={
            "containing_cell": _single_cell_candidate(
                "containing-cell-centre", containing_cell
            ),
            "nearest_cell": _single_cell_candidate("nearest-cell-centre", nearest_cell),
            "bilinear_four_cell": bilinear,
        },
        legacy_reference=LegacyReference(
            name="legacy_pre_contract_reference_elevation",
            elevation_m=legacy_elevation_m,
            derivation_status="reconstructed_not_proven",
            reconstruction=(
                "The legacy value matches the historical integer-array midpoint rounded to "
                "0.01 m, but no contemporaneous derivation record proves that origin."
            ),
            activated_in_existing_condition_packs=True,
        ),
        selection=Selection(
            selected_method=SAMPLING_METHOD,
            selected_method_version=SAMPLING_METHOD_VERSION,
            proposed_reference_elevation_m=proposed,
            difference_from_legacy_m=(
                None if proposed is None else proposed - legacy_elevation_m
            ),
            activation_status="not_activated_requires_documented_migration_decision",
        ),
        uncertainty=UncertaintyContract(
            numeric_vertical_uncertainty_m=None,
            vertical_datum_limitation=(
                "The Mountain Pack records an unknown vertical datum and the wider bake can "
                "mix LiDAR with Copernicus fallback without a vertical transformation. "
                "Footprint source codes remain explicit, but station-to-terrain differences "
                "are not datum-harmonized and no numeric vertical uncertainty is claimed."
            ),
            horizontal_reprojection_limitation=(
                "The reported residual characterizes inversion of the baked control lattice, "
                "not source geolocation accuracy."
            ),
            sampling_limitation=(
                "Bilinear interpolation is a deterministic grid sampling convention; it does "
                "not add terrain information or validate the DEM."
            ),
            source_resolution_limitation=(
                "Each footprint cell retains its terrain-source code. Mixed or fallback-source "
                "footprints inherit the limitations of every contributing source. The current "
                "provenance raster identifies a source class/acquisition year, not the exact "
                "winning source tile for each cell."
            ),
        ),
        algorithm_code_sha256=sha256_file(Path(__file__)),
    )
    content = draft.model_dump(mode="json")
    identity = f"reference-elevation-{_sha256_bytes(_canonical_json_bytes(content))}"
    return ReferenceElevationContract(**content, reference_elevation_id=identity)


def derive_reference_elevation_from_bake(
    bake_root: str | Path,
    *,
    expected_processing_sha256: str,
    expected_mountain_pack_sha256: str,
    target_longitude_deg: float,
    target_latitude_deg: float,
    legacy_elevation_m: float,
) -> ReferenceElevationContract:
    root = Path(bake_root).resolve()
    meta = validate_bake(
        root,
        expected_processing_sha256=expected_processing_sha256,
        expected_mountain_pack_sha256=expected_mountain_pack_sha256,
    )
    records = {record["name"]: record for record in meta["layers"]}
    elevation_raw = np.load(root / records["elevation"]["file"], mmap_mode="r")
    source_raw = np.load(root / records["terrain_source"]["file"], mmap_mode="r")
    elevation = np.ma.masked_invalid(elevation_raw, copy=False)
    source_nodata = records["terrain_source"].get("nodata")
    terrain_source = (
        np.ma.masked_equal(source_raw, source_nodata, copy=False)
        if source_nodata not in (None, "NaN")
        else np.ma.masked_invalid(source_raw, copy=False)
    )
    return derive_reference_elevation(
        meta,
        elevation,
        terrain_source,
        target_longitude_deg=target_longitude_deg,
        target_latitude_deg=target_latitude_deg,
        legacy_elevation_m=legacy_elevation_m,
    )


def load_reference_elevation(path: str | Path) -> ReferenceElevationContract:
    source = Path(path)
    if source.is_dir():
        contract_path = source / REFERENCE_ELEVATION_FILENAME
        checksums_path = source / CHECKSUMS_FILENAME
        if not checksums_path.is_file():
            raise ReferenceElevationError("Reference-elevation checksum manifest is missing.")
        try:
            manifest = json.loads(checksums_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReferenceElevationError("Reference-elevation checksum manifest is invalid.") from exc
        if set(manifest) != {"schema", "files"} or manifest.get("schema") != REFERENCE_ELEVATION_STORAGE_SCHEMA:
            raise ReferenceElevationError("Reference-elevation checksum manifest is not strict.")
        expected_files = {REFERENCE_ELEVATION_FILENAME}
        if set(manifest.get("files", {})) != expected_files:
            raise ReferenceElevationError("Reference-elevation checksum file set is not exact.")
        if not contract_path.is_file():
            raise ReferenceElevationError("Reference-elevation contract file is missing.")
        actual = sha256_file(contract_path)
        if manifest["files"][REFERENCE_ELEVATION_FILENAME] != actual:
            raise ReferenceElevationError("Reference-elevation contract failed SHA-256 validation.")
    else:
        contract_path = source
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
        contract = ReferenceElevationContract.model_validate(payload)
    except Exception as exc:
        raise ReferenceElevationError(f"Reference-elevation contract is invalid: {exc}") from exc
    if source.is_dir() and source.name != contract.reference_elevation_id:
        raise ReferenceElevationError("Reference-elevation directory conflicts with its identity.")
    return contract


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def write_reference_elevation(
    contract: ReferenceElevationContract, runtime_root: str | Path
) -> Path:
    root = Path(runtime_root).resolve()
    parent = root / "reports" / "terrain" / "reference-elevations"
    target = parent / contract.reference_elevation_id
    content = canonical_reference_elevation_bytes(contract)
    checksum = _sha256_bytes(content)
    manifest = _canonical_json_bytes(
        {
            "schema": REFERENCE_ELEVATION_STORAGE_SCHEMA,
            "files": {REFERENCE_ELEVATION_FILENAME: checksum},
        }
    )
    if target.exists():
        existing = load_reference_elevation(target)
        if canonical_reference_elevation_bytes(existing) != content:
            raise ReferenceElevationError(
                "Existing reference-elevation identity conflicts with stored content."
            )
        return target
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{contract.reference_elevation_id}.{uuid.uuid4().hex}.staging"
    try:
        staging.mkdir()
        _write_fsynced(staging / REFERENCE_ELEVATION_FILENAME, content)
        _write_fsynced(staging / CHECKSUMS_FILENAME, manifest)
        load_reference_elevation(staging / REFERENCE_ELEVATION_FILENAME)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    stored = load_reference_elevation(target)
    if canonical_reference_elevation_bytes(stored) != content:
        raise ReferenceElevationError("Published reference-elevation bytes changed.")
    return target
