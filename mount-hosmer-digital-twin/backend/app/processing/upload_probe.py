r"""Validate an uploaded raster and synthesize a mountain pack, out of process.

Launched as ``python -I -m app.processing.upload_probe`` against the *bake*
environment, exactly like the runout workers, so rasterio, pyproj, and GDAL never
load inside the serving process. Everything it decides is reported as JSON on
stdout; nothing is inferred silently.

Why the pack is built here rather than in the request handler: constructing the
real :class:`~app.processing.mountain_pack.MountainPack` is what proves an upload
is validated by the identical code that validates Mount Hosmer. That module's
docstring records that the running service never imports it, so the synthesis
happens on this side of the process boundary and the handler only ever sees JSON.

The checks run cheapest-first and header-only where possible, so a hostile file is
rejected before any array is read:

1. opens as a raster at all; exactly one band; numeric dtype
2. header-only pixel budget -- the decompression-bomb guard
3. a CRS that exists and resolves
4. square cells, which AvaFrame hard-requires downstream
5. an analysis CRS: the source's own if projected in metres, else a UTM zone
6. an analysis resolution that never claims more detail than the source has
7. data sanity: valid fraction, non-constant, real relief, plausible range
8. vertical units -- detected and warned about, never converted

Step 8 is the one failure mode that is silent and plausible. A US-survey-feet DEM
produces a beautiful 3D mountain with slopes inflated by ~3.28x and authoritative
looking runout. There is no fully reliable automatic check, so this reports
suspicion and the pack records what the uploader affirmed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

#: Reject before reading arrays. A 20000x20000 float32 is 1.6 GB expanded from a
#: few MB compressed, so this is checked from the header alone.
MAX_SOURCE_PIXELS = 250_000_000

#: Cells in the snapped *analysis* grid. Overridden from ``--max-cells`` so this
#: default and the API's budget in :mod:`app.mountains` cannot disagree.
MAX_ANALYSIS_CELLS_LIMIT = 4_000_000

#: Physically plausible ground elevations, in metres. Outside this the file is not
#: an elevation model in metres, whatever it claims.
MIN_ELEVATION_M = -500.0
MAX_ELEVATION_M = 9000.0

#: Below this the tile has no avalanche terrain to simulate at all.
MIN_RELIEF_M = 50.0

#: At least this fraction of the grid must carry data.
MIN_VALID_FRACTION = 0.25

#: Terrain steeper than this, over a whole DEM, is the signature of vertical units
#: that are not metres -- feet inflate every slope by a factor of ~3.28.
SUSPICIOUS_SLOPE_P95_DEG = 60.0

FEET_PER_METRE = 3.28084


class ProbeRejection(Exception):
    """An upload that must not become a mountain. The message is user-visible."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utm_epsg(longitude: float, latitude: float) -> str:
    """The UTM zone containing a point, as an EPSG code."""
    zone = int(math.floor((longitude + 180.0) / 6.0)) + 1
    zone = min(max(zone, 1), 60)
    return f"EPSG:{(32600 if latitude >= 0 else 32700) + zone}"


def _is_metre_projected(crs) -> bool:
    """Projected with metre horizontal units.

    Mirrors the axis-unit check ``validate_declared_sources`` applies to the
    analysis CRS, so a CRS chosen here cannot fail the pack validator later.
    """
    return bool(
        crs.is_projected
        and crs.axis_info
        and all(axis.unit_conversion_factor == 1.0 for axis in crs.axis_info[:2])
    )


def inspect_raster(path: Path) -> dict[str, Any]:
    """Header and statistics for one uploaded raster. Raises ProbeRejection."""
    import numpy as np
    import rasterio
    from pyproj import CRS

    try:
        dataset = rasterio.open(path)
    except Exception as exc:  # rasterio raises several unrelated types here
        raise ProbeRejection(
            "not_a_raster",
            f"The file could not be opened as a raster: {exc}",
        ) from exc

    with dataset:
        if dataset.count != 1:
            raise ProbeRejection(
                "not_single_band",
                f"An elevation model must have exactly one band; this file has {dataset.count}.",
            )
        dtype = str(dataset.dtypes[0])
        if dtype.startswith(("complex", "bool")):
            raise ProbeRejection("not_numeric", f"Band data type {dtype!r} is not elevation data.")

        pixels = int(dataset.width) * int(dataset.height)
        if pixels > MAX_SOURCE_PIXELS:
            raise ProbeRejection(
                "too_many_pixels",
                f"The raster is {dataset.width} x {dataset.height} = {pixels:,} pixels, over the "
                f"{MAX_SOURCE_PIXELS:,} limit. Clip it to the area you want to model.",
            )
        if pixels < 4:
            raise ProbeRejection("too_small", "The raster has no usable extent.")

        if dataset.crs is None:
            raise ProbeRejection(
                "no_crs",
                "The raster declares no coordinate reference system, so its cells cannot be "
                "placed on the Earth. Re-export it with a CRS.",
            )
        try:
            source_crs = CRS.from_user_input(dataset.crs)
        except Exception as exc:
            raise ProbeRejection("unresolvable_crs", f"The raster CRS could not be resolved: {exc}") from exc

        transform = dataset.transform
        pixel_width, pixel_height = abs(transform.a), abs(transform.e)
        if not (math.isfinite(pixel_width) and math.isfinite(pixel_height)) or min(pixel_width, pixel_height) <= 0:
            raise ProbeRejection("degenerate_transform", "The raster geotransform is degenerate.")
        if abs(pixel_width - pixel_height) > 1e-6 * max(pixel_width, pixel_height):
            raise ProbeRejection(
                "non_square_cells",
                f"Cells must be square; this raster is {pixel_width:g} x {pixel_height:g}. "
                "Resample it to a square grid before uploading.",
            )
        if transform.b or transform.d:
            raise ProbeRejection(
                "rotated_raster",
                "The raster is rotated or sheared; only north-up rasters are supported.",
            )

        bounds = tuple(float(value) for value in dataset.bounds)
        values = dataset.read(1, masked=True)

    valid_fraction = float(values.count()) / float(values.size) if values.size else 0.0
    if valid_fraction <= 0.0:
        raise ProbeRejection("all_nodata", "Every pixel in the raster is NoData.")
    if valid_fraction < MIN_VALID_FRACTION:
        raise ProbeRejection(
            "mostly_nodata",
            f"Only {valid_fraction:.1%} of the raster carries data, below the "
            f"{MIN_VALID_FRACTION:.0%} minimum.",
        )

    finite = values.compressed().astype("float64")
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ProbeRejection("all_nodata", "The raster contains no finite elevation values.")
    minimum, maximum = float(finite.min()), float(finite.max())
    relief = maximum - minimum
    if relief <= 0.0:
        raise ProbeRejection(
            "constant_elevation",
            "Every pixel has the same elevation, so the file describes no terrain.",
        )

    return {
        "path": str(path),
        "width": int(dataset.width),
        "height": int(dataset.height),
        "dtype": dtype,
        "source_crs": source_crs.to_string(),
        "source_crs_is_metre_projected": _is_metre_projected(source_crs),
        # In source-CRS units, which are only metres when the CRS is projected in
        # metres. The metre-valued figure is grid.native_resolution_m.
        "pixel_size": pixel_width,
        "bounds": bounds,
        "nodata": None if dataset.nodata is None else float(dataset.nodata),
        "valid_fraction": valid_fraction,
        "elevation_min": minimum,
        "elevation_max": maximum,
        "relief_m": relief,
        "_values": values,
    }


def check_vertical_units(report: dict[str, Any], native_resolution_m: float) -> list[str]:
    """Flag elevations that do not behave like metres. Never converts them."""
    import numpy as np

    warnings: list[str] = []
    minimum, maximum = report["elevation_min"], report["elevation_max"]

    if minimum < MIN_ELEVATION_M or maximum > MAX_ELEVATION_M:
        raise ProbeRejection(
            "implausible_elevation_range",
            f"Elevations run from {minimum:,.0f} to {maximum:,.0f}, outside the plausible "
            f"{MIN_ELEVATION_M:,.0f} to {MAX_ELEVATION_M:,.0f} metre range. If this DEM is in "
            "feet, convert it to metres before uploading.",
        )
    if report["relief_m"] < MIN_RELIEF_M:
        raise ProbeRejection(
            "insufficient_relief",
            f"Total relief is {report['relief_m']:.0f} m, below the {MIN_RELIEF_M:.0f} m minimum. "
            "There is no avalanche terrain here to simulate.",
        )

    # Slope is the discriminating signal: feet-valued elevations over a metre-valued
    # grid inflate every gradient by ~3.28, which shows up as terrain far steeper
    # than real mountains are.
    values = report["_values"]
    filled = values.filled(np.nan).astype("float64")
    dy, dx = np.gradient(filled, native_resolution_m)
    slope_deg = np.degrees(np.arctan(np.hypot(dx, dy)))
    slope_deg = slope_deg[np.isfinite(slope_deg)]
    if slope_deg.size:
        p95 = float(np.percentile(slope_deg, 95))
        report["slope_p95_deg"] = p95
        if p95 > SUSPICIOUS_SLOPE_P95_DEG:
            implied = math.degrees(math.atan(math.tan(math.radians(p95)) / FEET_PER_METRE))
            warnings.append(
                f"Terrain is implausibly steep: the 95th-percentile slope is {p95:.0f} degrees. "
                f"If these elevations are in feet rather than metres, the true value would be "
                f"about {implied:.0f} degrees. Elevations are used exactly as supplied and are "
                "never converted, so an incorrect unit silently inflates every slope, release "
                "zone, and runout distance in this mountain."
            )

    metre_equivalent = report["relief_m"] / FEET_PER_METRE
    if report["relief_m"] > 1500.0 and metre_equivalent < 1200.0:
        warnings.append(
            f"Relief of {report['relief_m']:,.0f} would be {metre_equivalent:,.0f} m if these "
            "values are in feet. Confirm the vertical unit before trusting any result."
        )
    return warnings


def _resolve_grid(report: dict[str, Any], requested_resolution_m: float | None) -> dict[str, Any]:
    """Choose the analysis CRS, resolution, and snapped bounds."""
    from pyproj import CRS, Transformer

    source_crs = CRS.from_user_input(report["source_crs"])
    west, south, east, north = report["bounds"]

    if report["source_crs_is_metre_projected"]:
        analysis_crs = source_crs
        native_resolution_m = float(report["pixel_size"])
        bounds_m = (west, south, east, north)
        to_wgs84 = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        centre_lon, centre_lat = to_wgs84.transform((west + east) / 2.0, (south + north) / 2.0)
    else:
        # Geographic (or a non-metre projection): pick the UTM zone under the
        # centroid. read_aligned reprojects the source for us, so the upload does
        # not have to be in the analysis CRS -- only the analysis CRS must be
        # projected in metres, because resolution_m and runout distances are metres.
        to_wgs84 = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        centre_lon, centre_lat = to_wgs84.transform((west + east) / 2.0, (south + north) / 2.0)
        if not (math.isfinite(centre_lon) and math.isfinite(centre_lat)):
            raise ProbeRejection(
                "unprojectable",
                "The raster's centre could not be expressed in longitude and latitude.",
            )
        analysis_crs = CRS.from_user_input(_utm_epsg(centre_lon, centre_lat))
        to_analysis = Transformer.from_crs(source_crs, analysis_crs, always_xy=True)
        xs, ys = to_analysis.transform([west, east, west, east], [south, south, north, north])
        if not all(math.isfinite(value) for value in (*xs, *ys)):
            raise ProbeRejection(
                "unprojectable",
                "The raster extent could not be projected into a metre-based CRS.",
            )
        bounds_m = (min(xs), min(ys), max(xs), max(ys))
        native_resolution_m = min(
            (bounds_m[2] - bounds_m[0]) / report["width"],
            (bounds_m[3] - bounds_m[1]) / report["height"],
        )

    if not _is_metre_projected(analysis_crs):
        raise ProbeRejection(
            "no_metre_crs",
            f"No metre-based analysis CRS could be derived for this raster ({analysis_crs}).",
        )

    # Never claim more detail than the source carries: upsampling creates no
    # information, which is what mosaic.py already says about the fallback DEM.
    resolution = max(float(requested_resolution_m or native_resolution_m), native_resolution_m)

    west_m, south_m, east_m, north_m = bounds_m
    width = max(int(round((east_m - west_m) / resolution)), 1)
    height = max(int(round((north_m - south_m) / resolution)), 1)
    # Grow the cell size until the snapped grid fits the budget, rather than
    # cropping the user's extent: a coarser whole mountain beats a sharp corner.
    while width * height > MAX_ANALYSIS_CELLS_LIMIT:
        resolution *= 1.25
        width = max(int(round((east_m - west_m) / resolution)), 1)
        height = max(int(round((north_m - south_m) / resolution)), 1)
    resolution = round(resolution, 4)

    return {
        "analysis_crs": analysis_crs.to_string(),
        "bounds": [west_m, south_m, east_m, north_m],
        "resolution_m": resolution,
        "native_resolution_m": round(native_resolution_m, 4),
        "width": width,
        "height": height,
        "cells": width * height,
        "centre_wgs84": [centre_lon, centre_lat],
        "resampled": resolution > native_resolution_m * 1.001,
    }


def _aoi_document(grid: dict[str, Any]) -> dict[str, Any]:
    """The DEM footprint as WGS84 GeoJSON.

    WGS84 specifically: ``exposure.build_exposure`` constructs its transformer as
    ``Transformer.from_crs("EPSG:4326", grid.crs_string)`` and never reads a
    GeoJSON ``crs`` member, so an AOI written in the analysis CRS would be
    misread as longitude/latitude the moment an exposure asset is declared.
    """
    from pyproj import CRS, Transformer

    west, south, east, north = grid["bounds"]
    to_wgs84 = Transformer.from_crs(CRS.from_user_input(grid["analysis_crs"]), "EPSG:4326", always_xy=True)
    ring = [
        list(to_wgs84.transform(x, y))
        for x, y in ((west, north), (east, north), (east, south), (west, south), (west, north))
    ]
    return {
        "type": "FeatureCollection",
        "name": "uploaded-dem-extent",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "basis": "Exact uploaded DEM footprint, reprojected to WGS84",
                    "purpose": "Uploaded-mountain analysis extent; not a surveyed avalanche domain",
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        ],
    }


def _pack_document(
    *,
    mountain_id: str,
    name: str,
    grid: dict[str, Any],
    source: dict[str, str],
    has_landcover: bool,
    vertical_units_affirmed: bool,
) -> dict[str, Any]:
    assets: dict[str, Any] = {
        "aoi": {
            "href": "metadata/aoi.geojson",
            "adapter": "geojson",
            "purpose": "model_input",
            "required": True,
            "units": "WGS84 longitude,latitude degrees",
            "source": source,
        },
        "elevation_primary": {
            "href": "static/dem.tif",
            "adapter": "single_raster",
            "purpose": "model_input",
            "required": True,
            "units": "metres" if vertical_units_affirmed else "metres (declared by uploader, unverified)",
            "native_resolution_m": grid["native_resolution_m"],
            "source": source,
        },
    }
    if has_landcover:
        assets["landcover"] = {
            "href": "static/landcover.tif",
            "adapter": "categorical_raster",
            "purpose": "model_input",
            "required": True,
            "units": "categorical class code",
            "native_resolution_m": None,
            "source": source,
        }
    else:
        # The pack schema requires the landcover role to be declared, but permits
        # it to be optional and absent. That is the only legal way to express "no
        # forest data": validate_declared_sources warns, and terrain._forest_mask
        # masks every cell rather than inventing non-forest zeroes.
        assets["landcover"] = {
            "href": "static/landcover-not-supplied.tif",
            "adapter": "categorical_raster",
            "purpose": "model_input",
            "required": False,
            "units": "categorical class code",
            "native_resolution_m": None,
            "source": {
                "provider": "No source used",
                "citation": (
                    "No land cover was supplied with this upload; the forest layer must "
                    "remain masked rather than default to non-forest."
                ),
                "licence": "Not applicable (missing optional input)",
            },
        }
    return {
        "schema_version": 1,
        "id": mountain_id,
        "name": name,
        "center_wgs84": grid["centre_wgs84"],
        "grid": {
            "analysis_crs": grid["analysis_crs"],
            "coordinate_order": "easting,northing",
            "bounds": grid["bounds"],
            "resolution_m": grid["resolution_m"],
            # An uploaded raster carries no reviewed vertical-datum statement, and
            # inventing one would be worse than admitting its absence.
            "vertical_datum": {"status": "unknown", "name": None},
        },
        "model_profile": "canadian-rockies-uncalibrated-stage3-v1",
        "model_calibrated_locally": False,
        "assets": assets,
    }


def probe(
    *,
    source_root: Path,
    mountain_id: str,
    name: str,
    provider: str,
    citation: str,
    licence: str,
    vertical_units_affirmed: bool,
    requested_resolution_m: float | None,
    backend_root: Path,
) -> dict[str, Any]:
    import hashlib

    sys.path.insert(0, str(backend_root))
    from app.processing.mountain_pack import MountainPack, validate_declared_sources

    dem_path = source_root / "static" / "dem.tif"
    landcover_path = source_root / "static" / "landcover.tif"
    has_landcover = landcover_path.is_file()

    report = inspect_raster(dem_path)
    grid = _resolve_grid(report, requested_resolution_m)
    warnings = check_vertical_units(report, grid["native_resolution_m"])
    report.pop("_values", None)

    if grid["resampled"]:
        warnings.append(
            f"The analysis grid is {grid['resolution_m']:g} m while the source is about "
            f"{grid['native_resolution_m']:g} m, so the extent fits the {MAX_ANALYSIS_CELLS_LIMIT:,} "
            "cell budget. Detail finer than the analysis grid is not used."
        )
    if not has_landcover:
        warnings.append(
            "No land cover was supplied. The release model requires a forest layer, so this "
            "mountain will render in 3D but cannot be assessed: coverage will be 0% of cells "
            "and no release zones or runout will be produced."
        )
    if not vertical_units_affirmed:
        warnings.append(
            "The uploader did not affirm that elevations are in metres. They are used exactly "
            "as supplied."
        )

    source = {"provider": provider, "citation": citation, "licence": licence}
    pack_document = _pack_document(
        mountain_id=mountain_id,
        name=name,
        grid=grid,
        source=source,
        has_landcover=has_landcover,
        vertical_units_affirmed=vertical_units_affirmed,
    )

    # Validate with the identical model that validates Mount Hosmer. A pack that
    # cannot be constructed here must never reach the bake.
    try:
        pack = MountainPack.model_validate(pack_document)
    except Exception as exc:
        raise ProbeRejection("invalid_pack", f"The synthesized mountain pack is not valid: {exc}") from exc

    aoi_path = source_root / "metadata" / "aoi.geojson"
    aoi_path.parent.mkdir(parents=True, exist_ok=True)
    aoi_path.write_text(json.dumps(_aoi_document(grid), indent=2), encoding="utf-8")

    pack_path = source_root.parent / "pack.json"
    pack_bytes = json.dumps(pack_document, indent=2).encode("utf-8")
    pack_path.write_bytes(pack_bytes)

    warnings.extend(validate_declared_sources(pack, source_root))
    warnings.extend(pack.warnings())

    return {
        "ok": True,
        "mountain_id": mountain_id,
        "raster": report,
        "grid": grid,
        "has_landcover": has_landcover,
        "assessable": has_landcover,
        "pack_path": str(pack_path),
        "pack_sha256": hashlib.sha256(pack_bytes).hexdigest(),
        "warnings": sorted(set(warnings)),
    }


def main(argv: list[str] | None = None) -> int:
    global MAX_ANALYSIS_CELLS_LIMIT

    parser = argparse.ArgumentParser(description="Validate an uploaded DEM and synthesize its pack.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--mountain-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--citation", required=True)
    parser.add_argument("--licence", required=True)
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--vertical-units-affirmed", action="store_true")
    parser.add_argument("--resolution-m", type=float, default=None)
    parser.add_argument("--max-cells", type=int, default=MAX_ANALYSIS_CELLS_LIMIT)
    args = parser.parse_args(argv)
    MAX_ANALYSIS_CELLS_LIMIT = int(args.max_cells)

    try:
        payload = probe(
            source_root=args.source_root.resolve(),
            mountain_id=args.mountain_id,
            name=args.name,
            provider=args.provider,
            citation=args.citation,
            licence=args.licence,
            vertical_units_affirmed=bool(args.vertical_units_affirmed),
            requested_resolution_m=args.resolution_m,
            backend_root=args.backend_root.resolve(),
        )
    except ProbeRejection as exc:
        json.dump({"ok": False, "code": exc.code, "message": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:  # unexpected: still answer in JSON, still non-zero
        json.dump({"ok": False, "code": "probe_failed", "message": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
