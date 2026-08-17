"""Calibrate and terrain-normalize frozen public-event Sentinel-1 GRD chips.

The processor is intentionally offline.  It consumes only SHA-256-identified
acquisition and terrain-cache bytes, never imports model code, and never opens
RegObs target geometry.  Sentinel-1 SAFE product, calibration, and noise XML are
used to map target pixels back to source line/sample coordinates, subtract the
documented noise power, and apply sigma-nought calibration.  The public DTM is
then used for a reproducible local-incidence cosine normalization and explicit
layover/radar-shadow masks.

This is not an area-based Range-Doppler terrain-flattening implementation.  That
remaining algorithmic and event-surface limitation is preserved in the output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-sentinel1-processing-v1"
PROCESSING_ID = "public-event-sentinel1-processing-v1"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Immutable processing conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ET.Element) -> dict[str, ET.Element]:
    return {_local_name(child): child for child in element}


def _numbers(element: ET.Element) -> np.ndarray:
    return np.fromstring(element.text or "", sep=" ", dtype=np.float64)


def parse_product_geolocation(path: Path) -> dict[str, np.ndarray]:
    points: list[dict[str, float]] = []
    for element in ET.parse(path).getroot().iter():
        if _local_name(element) != "geolocationGridPoint":
            continue
        values = _children(element)
        points.append(
            {
                "line": float(values["line"].text or "nan"),
                "pixel": float(values["pixel"].text or "nan"),
                "longitude": float(values["longitude"].text or "nan"),
                "latitude": float(values["latitude"].text or "nan"),
                "height": float(values["height"].text or "nan"),
                "incidence_angle_deg": float(
                    values["incidenceAngle"].text or "nan"
                ),
            }
        )
    if len(points) < 4:
        raise ValueError(f"SAFE product annotation has too few geolocation points: {path}.")
    lines = np.array(sorted({point["line"] for point in points}))
    pixels = np.array(sorted({point["pixel"] for point in points}))
    by_coordinate = {(point["line"], point["pixel"]): point for point in points}
    if len(by_coordinate) != len(lines) * len(pixels):
        raise ValueError("SAFE product geolocation grid is not rectangular.")
    result: dict[str, np.ndarray] = {"lines": lines, "pixels": pixels}
    for name in ("longitude", "latitude", "height", "incidence_angle_deg"):
        result[name] = np.array(
            [by_coordinate[(line, pixel)][name] for line in lines for pixel in pixels],
            dtype=np.float64,
        ).reshape(len(lines), len(pixels))
    return result


def parse_rectangular_lut(
    path: Path, vector_name: str, value_name: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    vectors: list[tuple[float, np.ndarray, np.ndarray]] = []
    for element in ET.parse(path).getroot().iter():
        if _local_name(element) != vector_name:
            continue
        values = _children(element)
        pixels = _numbers(values["pixel"])
        lut = _numbers(values[value_name])
        if len(pixels) != len(lut):
            raise ValueError(f"{vector_name} pixel/LUT lengths differ in {path}.")
        vectors.append((float(values["line"].text or "nan"), pixels, lut))
    if len(vectors) < 2:
        raise ValueError(f"No usable {vector_name} grid in {path}.")
    vectors.sort(key=lambda value: value[0])
    reference_pixels = vectors[0][1]
    if any(not np.array_equal(reference_pixels, vector[1]) for vector in vectors[1:]):
        raise ValueError(f"{vector_name} pixels differ by azimuth line in {path}.")
    return (
        np.array([vector[0] for vector in vectors]),
        reference_pixels,
        np.stack([vector[2] for vector in vectors]),
    )


def parse_lut_vectors(
    path: Path, vector_name: str, value_name: str
) -> list[tuple[float, np.ndarray, np.ndarray]]:
    """Parse a SAFE LUT whose range-sample coordinates may vary by line."""

    vectors: list[tuple[float, np.ndarray, np.ndarray]] = []
    for element in ET.parse(path).getroot().iter():
        if _local_name(element) != vector_name:
            continue
        values = _children(element)
        pixels = _numbers(values["pixel"])
        lut = _numbers(values[value_name])
        if len(pixels) < 2 or len(pixels) != len(lut):
            raise ValueError(f"{vector_name} pixel/LUT lengths differ in {path}.")
        vectors.append((float(values["line"].text or "nan"), pixels, lut))
    vectors.sort(key=lambda value: value[0])
    if len(vectors) < 2 or any(
        not np.all(np.diff(vector[1]) > 0) for vector in vectors
    ):
        raise ValueError(f"No usable monotonic {vector_name} vectors in {path}.")
    return vectors


def irregular_bilinear_lut(
    vectors: list[tuple[float, np.ndarray, np.ndarray]],
    query_lines: np.ndarray,
    query_pixels: np.ndarray,
) -> np.ndarray:
    """Interpolate varying range grids without extrapolating any SAFE row."""

    lines = np.array([vector[0] for vector in vectors])
    result = np.full(query_lines.shape, np.nan, dtype=np.float64)
    valid = (
        np.isfinite(query_lines)
        & np.isfinite(query_pixels)
        & (query_lines >= lines[0])
        & (query_lines <= lines[-1])
    )
    if not valid.any():
        return result
    brackets = np.clip(
        np.searchsorted(lines, query_lines, side="right") - 1, 0, len(lines) - 2
    )
    for lower_index in np.unique(brackets[valid]):
        selected = valid & (brackets == lower_index)
        lower_line, lower_pixels, lower_values = vectors[int(lower_index)]
        upper_line, upper_pixels, upper_values = vectors[int(lower_index) + 1]
        pixels = query_pixels[selected]
        inside = (
            (pixels >= lower_pixels[0])
            & (pixels <= lower_pixels[-1])
            & (pixels >= upper_pixels[0])
            & (pixels <= upper_pixels[-1])
        )
        interpolated = np.full(pixels.shape, np.nan, dtype=np.float64)
        if inside.any():
            lower = np.interp(
                pixels[inside], lower_pixels, lower_values
            )
            upper = np.interp(
                pixels[inside], upper_pixels, upper_values
            )
            fraction = (
                query_lines[selected][inside] - lower_line
            ) / (upper_line - lower_line)
            interpolated[inside] = lower * (1 - fraction) + upper * fraction
        result[selected] = interpolated
    return result


def parse_noise_azimuth(path: Path) -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    for element in ET.parse(path).getroot().iter():
        if _local_name(element) != "noiseAzimuthVector":
            continue
        values = _children(element)
        lines = _numbers(values["line"])
        lut = _numbers(values["noiseAzimuthLut"])
        if len(lines) != len(lut):
            raise ValueError(f"Noise azimuth line/LUT lengths differ in {path}.")
        vectors.append(
            {
                "swath": values["swath"].text,
                "first_line": float(values["firstAzimuthLine"].text or "nan"),
                "last_line": float(values["lastAzimuthLine"].text or "nan"),
                "first_pixel": float(values["firstRangeSample"].text or "nan"),
                "last_pixel": float(values["lastRangeSample"].text or "nan"),
                "lines": lines,
                "lut": lut,
            }
        )
    if not vectors:
        raise ValueError(f"No noiseAzimuthVector values in {path}.")
    return vectors


def bilinear_lut(
    lines: np.ndarray,
    pixels: np.ndarray,
    values: np.ndarray,
    query_lines: np.ndarray,
    query_pixels: np.ndarray,
) -> np.ndarray:
    """Bilinearly interpolate a rectangular SAFE LUT without extrapolation."""

    result = np.full(query_lines.shape, np.nan, dtype=np.float64)
    valid = (
        np.isfinite(query_lines)
        & np.isfinite(query_pixels)
        & (query_lines >= lines[0])
        & (query_lines <= lines[-1])
        & (query_pixels >= pixels[0])
        & (query_pixels <= pixels[-1])
    )
    if not valid.any():
        return result
    ql = query_lines[valid]
    qp = query_pixels[valid]
    li = np.clip(np.searchsorted(lines, ql, side="right") - 1, 0, len(lines) - 2)
    pi = np.clip(
        np.searchsorted(pixels, qp, side="right") - 1, 0, len(pixels) - 2
    )
    lf = (ql - lines[li]) / (lines[li + 1] - lines[li])
    pf = (qp - pixels[pi]) / (pixels[pi + 1] - pixels[pi])
    top = values[li, pi] * (1 - pf) + values[li, pi + 1] * pf
    bottom = values[li + 1, pi] * (1 - pf) + values[li + 1, pi + 1] * pf
    result[valid] = top * (1 - lf) + bottom * lf
    return result


def noise_azimuth_factor(
    vectors: list[dict[str, Any]], source_lines: np.ndarray, source_pixels: np.ndarray
) -> np.ndarray:
    result = np.full(source_lines.shape, np.nan, dtype=np.float64)
    assignment_count = np.zeros(source_lines.shape, dtype=np.uint8)
    for vector in vectors:
        selected = (
            (source_lines >= vector["first_line"])
            & (source_lines <= vector["last_line"])
            & (source_pixels >= vector["first_pixel"])
            & (source_pixels <= vector["last_pixel"])
        )
        if not selected.any():
            continue
        result[selected] = np.interp(
            source_lines[selected], vector["lines"], vector["lut"]
        )
        assignment_count[selected] += 1
    result[assignment_count != 1] = np.nan
    return result


def calibrate_sigma0(
    dn: np.ndarray, sigma_lut: np.ndarray, noise_lut: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Apply the Sentinel-1 GRD power calibration/noise equation."""

    dn_power = dn.astype(np.float64) ** 2
    valid = (
        (dn > 0)
        & np.isfinite(sigma_lut)
        & (sigma_lut > 0)
        & np.isfinite(noise_lut)
    )
    corrected_power = np.maximum(dn_power - noise_lut, 0.0)
    sigma0 = np.full(dn.shape, np.nan, dtype=np.float64)
    sigma0[valid] = corrected_power[valid] / sigma_lut[valid] ** 2
    return sigma0, valid


def terrain_normalize(
    sigma0: np.ndarray,
    dem: np.ndarray,
    incidence_angle_deg: np.ndarray,
    look_azimuth_away_deg: float,
    resolution_m: float,
) -> dict[str, np.ndarray]:
    """Compute explicit local-incidence normalization and visibility masks.

    ``look_azimuth_away_deg`` is the horizontal near-to-far (sensor-to-ground)
    direction.  The surface normal is dotted with the opposite, ground-to-sensor
    vector.  Layover is flagged where the range-direction terrain slope facing
    the sensor is steeper than the ellipsoid incidence angle.
    """

    dem = dem.astype(np.float64)
    finite = np.isfinite(dem) & np.isfinite(incidence_angle_deg)
    filled = np.where(finite, dem, np.nan)
    row_gradient, east_gradient = np.gradient(filled, resolution_m, resolution_m)
    north_gradient = -row_gradient
    gradient_valid = finite.copy()
    gradient_valid[[0, -1], :] = False
    gradient_valid[:, [0, -1]] = False
    gradient_valid &= (
        np.roll(finite, 1, 0)
        & np.roll(finite, -1, 0)
        & np.roll(finite, 1, 1)
        & np.roll(finite, -1, 1)
    )

    away = math.radians(look_azimuth_away_deg)
    away_east = math.sin(away)
    away_north = math.cos(away)
    incidence = np.radians(incidence_angle_deg)
    normal_scale = np.sqrt(1 + east_gradient**2 + north_gradient**2)
    sensor_east = -np.sin(incidence) * away_east
    sensor_north = -np.sin(incidence) * away_north
    sensor_up = np.cos(incidence)
    cos_local = (
        -east_gradient * sensor_east
        - north_gradient * sensor_north
        + sensor_up
    ) / normal_scale
    local_incidence_deg = np.degrees(np.arccos(np.clip(cos_local, -1.0, 1.0)))

    facing_range_slope = np.arctan(
        east_gradient * away_east + north_gradient * away_north
    )
    layover = gradient_valid & (facing_range_slope >= incidence)
    radar_shadow = gradient_valid & (cos_local <= 0)
    usable = (
        gradient_valid
        & np.isfinite(sigma0)
        & (cos_local > 1.0e-3)
        & ~layover
        & ~radar_shadow
    )
    normalized = np.full(sigma0.shape, np.nan, dtype=np.float64)
    normalized[usable] = (
        sigma0[usable] * np.cos(incidence[usable]) / cos_local[usable]
    )
    local_incidence_deg[~gradient_valid] = np.nan
    return {
        "terrain_normalized_sigma0": normalized,
        "local_incidence_angle_deg": local_incidence_deg,
        "layover": layover,
        "radar_shadow": radar_shadow,
        "terrain_gradient_valid": gradient_valid,
        "terrain_usable": usable,
    }


def _geocode_product_grid(
    product: dict[str, np.ndarray], target: Any
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    import rasterio
    from rasterio.control import GroundControlPoint
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    rows, columns = product["longitude"].shape
    gcps = [
        GroundControlPoint(
            row=row,
            col=column,
            x=float(product["longitude"][row, column]),
            y=float(product["latitude"][row, column]),
            z=float(product["height"][row, column]),
        )
        for row in range(rows)
        for column in range(columns)
    ]
    outputs = []
    for values in (
        np.broadcast_to(product["lines"][:, None], (rows, columns)),
        np.broadcast_to(product["pixels"][None, :], (rows, columns)),
        product["incidence_angle_deg"],
    ):
        destination = np.full((target.height, target.width), np.nan, dtype=np.float64)
        reproject(
            source=values.astype(np.float64),
            destination=destination,
            gcps=gcps,
            src_crs="EPSG:4326",
            src_nodata=np.nan,
            dst_transform=target.transform,
            dst_crs=target.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        outputs.append(destination)
    return outputs[0], outputs[1], outputs[2]


def _look_azimuth(product: dict[str, np.ndarray], target_crs: Any) -> float:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    east, north = transformer.transform(product["longitude"], product["latitude"])
    delta_east = np.diff(east, axis=1)
    delta_north = np.diff(north, axis=1)
    azimuth = np.degrees(np.arctan2(delta_east, delta_north)) % 360
    return float(np.nanmedian(azimuth))


def _reproject_dem(terrain_path: Path, target: Any) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    destination = np.full((target.height, target.width), np.nan, dtype=np.float32)
    with rasterio.open(terrain_path) as source:
        source_values = source.read(1).astype(np.float32)
        if source.nodata is not None:
            source_values[source_values == source.nodata] = np.nan
        reproject(
            source=source_values,
            destination=destination,
            src_transform=source.transform,
            src_crs=source.crs,
            src_nodata=np.nan,
            dst_transform=target.transform,
            dst_crs=target.crs,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
    return destination


def _asset(scene: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [asset for asset in scene["assets"] if asset["asset_name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Expected one {name!r} asset; found {len(matches)}.")
    return matches[0]


def _asset_path(asset: dict[str, Any]) -> Path:
    reference = asset.get("cache_path") or (asset.get("cache") or {}).get(
        "response_cache_path"
    )
    if not isinstance(reference, str):
        raise ValueError(f"Asset lacks a cache path: {asset!r}.")
    return (REPOSITORY_ROOT / reference).resolve()


def _verify_asset(asset: dict[str, Any]) -> Path:
    path = _asset_path(asset)
    if _sha256_file(path) != asset["sha256"]:
        raise ValueError(f"Cached asset SHA-256 mismatch: {path}.")
    return path


def _write_stack(output: Path, profile: dict[str, Any], arrays: list[np.ndarray]) -> None:
    import rasterio

    temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
    write_profile = profile.copy()
    write_profile.update(
        driver="GTiff",
        dtype="float32",
        count=len(arrays),
        nodata=np.nan,
        compress="DEFLATE",
        predictor=3,
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(temporary, "w", **write_profile) as target:
            target.write(np.stack(arrays).astype(np.float32))
        _write_immutable(output, temporary.read_bytes())
    finally:
        temporary.unlink(missing_ok=True)


def process_scene(
    scene: dict[str, Any],
    nearest_scene: dict[str, Any],
    terrain: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    import rasterio

    annotation_assets = sorted(
        (
            asset
            for asset in scene["assets"]
            if asset["asset_name"].startswith("annotation-")
        ),
        key=lambda asset: asset["asset_name"],
    )
    if not annotation_assets:
        raise ValueError("Sentinel-1 scene has no SAFE product annotation.")
    if (
        nearest_scene["position"] != scene["position"]
        or nearest_scene["earth_search_item_id"] != scene["earth_search_item_id"]
    ):
        raise ValueError("Nearest-DN and SAFE scene identities differ.")
    terrain_path = (REPOSITORY_ROOT / terrain["terrain"]["raster_path"]).resolve()
    if _sha256_file(terrain_path) != terrain["terrain"]["raster_sha256"]:
        raise ValueError(f"Terrain cache SHA-256 mismatch: {terrain_path}.")

    results = []
    for annotation_asset in annotation_assets:
        polarization = annotation_asset["asset_name"].removeprefix("annotation-")
        dn_asset = _asset(nearest_scene, polarization)
        calibration_asset = _asset(scene, f"schema-calibration-{polarization}")
        noise_asset = _asset(scene, f"schema-noise-{polarization}")
        paths = {
            "dn": _verify_asset(dn_asset),
            "annotation": _verify_asset(annotation_asset),
            "calibration": _verify_asset(calibration_asset),
            "noise": _verify_asset(noise_asset),
            "terrain": terrain_path,
        }
        product = parse_product_geolocation(paths["annotation"])
        with rasterio.open(paths["dn"]) as source:
            dn = source.read(1)
            profile = source.profile.copy()
            source_lines, source_pixels, incidence = _geocode_product_grid(
                product, source
            )
            look_azimuth = _look_azimuth(product, source.crs)
            dem = _reproject_dem(paths["terrain"], source)

        calibration_lines, calibration_pixels, calibration_grid = (
            parse_rectangular_lut(
                paths["calibration"], "calibrationVector", "sigmaNought"
            )
        )
        sigma_lut = bilinear_lut(
            calibration_lines,
            calibration_pixels,
            calibration_grid,
            source_lines,
            source_pixels,
        )
        noise_range = irregular_bilinear_lut(
            parse_lut_vectors(
                paths["noise"], "noiseRangeVector", "noiseRangeLut"
            ),
            source_lines,
            source_pixels,
        )
        noise_azimuth = noise_azimuth_factor(
            parse_noise_azimuth(paths["noise"]), source_lines, source_pixels
        )
        noise_power = noise_range * noise_azimuth
        sigma0, calibration_valid = calibrate_sigma0(dn, sigma_lut, noise_power)
        terrain_fields = terrain_normalize(
            sigma0,
            dem,
            incidence,
            look_azimuth,
            abs(float(profile["transform"].a)),
        )
        output = output_dir / f"{scene['position']}-{polarization}.tif"
        arrays = [
            sigma0,
            terrain_fields["terrain_normalized_sigma0"],
            incidence,
            terrain_fields["local_incidence_angle_deg"],
            terrain_fields["layover"].astype(np.float32),
            terrain_fields["radar_shadow"].astype(np.float32),
            terrain_fields["terrain_gradient_valid"].astype(np.float32),
            calibration_valid.astype(np.float32),
        ]
        _write_stack(output, profile, arrays)
        counts = {
            "total_pixels": int(dn.size),
            "source_dn_valid": int((dn > 0).sum()),
            "calibration_valid": int(calibration_valid.sum()),
            "terrain_gradient_valid": int(
                terrain_fields["terrain_gradient_valid"].sum()
            ),
            "layover": int(terrain_fields["layover"].sum()),
            "radar_shadow": int(terrain_fields["radar_shadow"].sum()),
            "terrain_normalized_usable": int(
                terrain_fields["terrain_usable"].sum()
            ),
        }
        results.append(
            {
                "polarization": polarization,
                "output_path": _stable_path(output),
                "output_sha256": _sha256_file(output),
                "output_bytes": output.stat().st_size,
                "bands": [
                    "sigma0_linear_noise_removed",
                    "sigma0_local_incidence_normalized_linear",
                    "ellipsoid_incidence_angle_deg",
                    "local_incidence_angle_deg",
                    "layover_mask_1_true",
                    "radar_shadow_mask_1_true",
                    "terrain_gradient_valid_1_true",
                    "radiometric_calibration_valid_1_true",
                ],
                "look_azimuth_near_to_far_deg": look_azimuth,
                "counts": counts,
                "coverage": {
                    key: value / counts["total_pixels"]
                    for key, value in counts.items()
                    if key != "total_pixels"
                },
                "input_lineage": {
                    name: {"path": _stable_path(path), "sha256": _sha256_file(path)}
                    for name, path in paths.items()
                },
            }
        )
    return {
        "position": scene["position"],
        "earth_search_item_id": scene["earth_search_item_id"],
        "acquisition_time_utc": scene["acquisition_time_utc"],
        "polarizations": results,
    }


def build_processing(
    acquisition_path: Path,
    nearest_dn_path: Path,
    terrain_path: Path,
    cache_root: Path,
) -> dict[str, Any]:
    acquisition_bytes = acquisition_path.read_bytes()
    nearest_dn_bytes = nearest_dn_path.read_bytes()
    terrain_bytes = terrain_path.read_bytes()
    acquisition = json.loads(acquisition_bytes)
    nearest_dn = json.loads(nearest_dn_bytes)
    terrain = json.loads(terrain_bytes)
    if acquisition.get("schema") != "avycore-public-event-imagery-acquisition-v2":
        raise ValueError("Sentinel-1 processing requires acquisition schema v2.")
    if nearest_dn.get("schema") != "avycore-public-event-sentinel1-dn-nearest-v1":
        raise ValueError("Sentinel-1 processing requires nearest-DN acquisition v1.")
    if terrain.get("schema") != "avycore-public-event-terrain-acquisition-v1":
        raise ValueError("Unexpected terrain acquisition schema.")
    if acquisition.get("predictions_generated") is not False:
        raise ValueError("Refusing imagery selected after prediction access.")
    terrain_by_id = {candidate["candidate_id"]: candidate for candidate in terrain["candidates"]}
    nearest_by_id = {
        candidate["candidate_id"]: candidate for candidate in nearest_dn["candidates"]
    }
    candidates = []
    for candidate in acquisition["candidates"]:
        candidate_id = candidate["candidate_id"]
        terrain_candidate = terrain_by_id[candidate_id]
        nearest_candidate = nearest_by_id[candidate_id]
        sensor = candidate["sentinel_1_grd"]
        if sensor["status"] != "acquired_requires_pixel_qa":
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "status": "not_processed_no_acquired_sentinel1_pair",
                    "scenes": [],
                }
            )
            continue
        if not terrain_candidate.get("terrain_acquired"):
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "status": "not_processed_no_public_terrain",
                    "scenes": [],
                }
            )
            continue
        nearest_scenes = {
            scene["position"]: scene for scene in nearest_candidate["scenes"]
        }
        scenes = [
            process_scene(
                scene,
                nearest_scenes[scene["position"]],
                terrain_candidate,
                cache_root / candidate_id / "sentinel_1_grd",
            )
            for scene in sensor["scenes"]
        ]
        candidates.append(
            {
                "candidate_id": candidate_id,
                "status": "processed_requires_observation_qa_and_human_review",
                "scenes": scenes,
                "terrain_event_surface_mismatch": terrain_candidate[
                    "event_surface_mismatch"
                ],
            }
        )
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "processing_id": PROCESSING_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_acquisition_sha256": _sha256_bytes(acquisition_bytes),
        "source_nearest_dn_acquisition_sha256": _sha256_bytes(nearest_dn_bytes),
        "source_terrain_acquisition_sha256": _sha256_bytes(terrain_bytes),
        "predictions_generated": False,
        "model_results_opened": False,
        "holdout_targets_accessed": False,
        "radiometric_equation": (
            "sigma0_linear = max(DN^2 - noiseRangeLut*noiseAzimuthLut, 0) / "
            "sigmaNoughtLut^2; DN is nearest-sample GCP geocoded before squaring and SAFE "
            "LUTs are bilinearly interpolated in source line/sample"
        ),
        "terrain_normalization": (
            "sigma0_local = sigma0*cos(ellipsoid_incidence)/cos(local_incidence); "
            "masked for invalid DTM gradients, layover, radar shadow, and cos(local)<=0.001"
        ),
        "claim_boundary": (
            "Radiometric calibration, local-incidence normalization, and explicit masks are "
            "reproducible from hashed public bytes. This is not full area-based Range-Doppler "
            "terrain flattening; the screening DTM is bare earth rather than avalanche-day "
            "snow surface and cannot make an event validation-eligible."
        ),
        "candidates": candidates,
    }
    artifact["counts"] = {
        "candidates": len(candidates),
        "processed": sum(candidate["status"].startswith("processed") for candidate in candidates),
        "not_processed": sum(
            not candidate["status"].startswith("processed") for candidate in candidates
        ),
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--acquisition",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-imagery-acquisition-v2.json",
    )
    parser.add_argument(
        "--nearest-dn",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-sentinel1-dn-nearest-v1.json",
    )
    parser.add_argument(
        "--terrain",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-terrain-acquisition-v1.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache/public-event-sentinel1-processing-v1-nearest",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data/candidates/public-event-sentinel1-processing-v1.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    artifact = build_processing(
        args.acquisition.resolve(),
        args.nearest_dn.resolve(),
        args.terrain.resolve(),
        args.cache_root.resolve(),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(_pretty_json(artifact))
    print(
        f"Processed Sentinel-1 for {artifact['counts']['processed']} candidates; "
        f"not processed={artifact['counts']['not_processed']}."
    )


if __name__ == "__main__":
    main()
