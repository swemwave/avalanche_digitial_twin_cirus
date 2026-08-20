"""Build conservative, reproducible pixel-QA masks for public event imagery.

This stage reads only the immutable anonymous imagery cache.  It emits explicit
missing-data, scene-edge, detection-exclusion, survey-coverage, cloud,
cloud-shadow, topographic/cast-shadow, forest, water, SAR layover,
radar-shadow, prior-deposit, and usable masks.  Inclusion and exclusion masks
are deliberately distinct.  Unknown detection or survey coverage can therefore
invalidate every source pixel without falsely labelling it as forest, water,
layover, radar shadow, or a prior deposit.

The unresolved forest, prior-deposit, and SAR-terrain masks intentionally keep
all candidates out of the validation contract until public terrain/land-cover
lineage and independent human review are complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "avycore-public-event-pixel-qa-v3"
QA_ID = "public-event-pixel-qa-v3"
FROZEN_AT_UTC = "2026-08-14T00:00:00Z"
MASK_BANDS = (
    "missing_data",
    "scene_edge",
    "detection_exclusion",
    "survey_coverage",
    "cloud",
    "cloud_shadow",
    "shadow",
    "forest",
    "water",
    "layover",
    "radar_shadow",
    "prior_deposit",
    "usable",
)
EXCLUSION_MASK_BANDS = (
    "missing_data",
    "scene_edge",
    "detection_exclusion",
    "cloud",
    "cloud_shadow",
    "shadow",
    "forest",
    "water",
    "layover",
    "radar_shadow",
    "prior_deposit",
)
S2_CLOUD_CLASSES = (8, 9, 10)
S2_CLOUD_SHADOW_CLASS = 3
S2_CAST_SHADOW_CLASS = 2
S2_NO_DATA_CLASS = 0
S2_SATURATED_DEFECTIVE_CLASS = 1
S2_UNCLASSIFIED_CLASS = 7
S2_WATER_CLASS = 6
S2_VEGETATION_CLASS = 4
S2_SNOW_CLASS = 11


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_immutable(path: Path, value: bytes) -> None:
    if path.exists():
        if path.read_bytes() != value:
            raise ValueError(f"Immutable pixel-QA cache conflict at {path}.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(value)
    except FileExistsError:
        if path.read_bytes() != value:
            raise ValueError(f"Concurrent immutable pixel-QA conflict at {path}.")


def _stable_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _resolve_cache_path(reference: str) -> Path:
    path = Path(reference)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    resolved = path.resolve()
    try:
        resolved.relative_to((REPOSITORY_ROOT / ".validation-cache").resolve())
    except ValueError as exc:
        raise ValueError(f"Pixel QA may read only the validation cache: {reference!r}.") from exc
    return resolved


def _asset(scene: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [asset for asset in scene["assets"] if asset["asset_name"] == name]
    if len(matches) != 1:
        raise ValueError(
            f"Scene {scene.get('earth_search_item_id')} expected one asset {name!r}; "
            f"found {len(matches)}."
        )
    return matches[0]


def _read_asset(scene: dict[str, Any], name: str) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    try:
        import rasterio
    except ImportError as exc:
        raise RuntimeError("Pixel QA requires bake-time rasterio.") from exc
    record = _asset(scene, name)
    path = _resolve_cache_path(record["cache_path"])
    if _sha256_file(path) != record["sha256"]:
        raise ValueError(f"Acquired imagery hash mismatch at {path}.")
    with rasterio.open(path) as source:
        data = source.read(masked=False)
        valid = source.read_masks() > 0
        metadata = {
            "profile": source.profile.copy(),
            "transform": source.transform,
            "crs": source.crs,
            "width": source.width,
            "height": source.height,
            "count": source.count,
        }
    return data, valid, metadata


def _read_metadata_asset(scene: dict[str, Any], name: str) -> tuple[bytes, dict[str, Any]]:
    record = _asset(scene, name)
    reference = (record.get("cache") or {}).get("response_cache_path")
    if not isinstance(reference, str):
        raise ValueError(f"Metadata asset {name!r} lacks an immutable response path.")
    path = _resolve_cache_path(reference)
    payload = path.read_bytes()
    if _sha256_bytes(payload) != record["sha256"]:
        raise ValueError(f"Acquired metadata hash mismatch at {path}.")
    return payload, {
        "asset_name": name,
        "cache_path": _stable_path(path),
        "sha256": record["sha256"],
        "bytes": len(payload),
        "source_href": record["source_href"],
    }


def _xml_values(payload: bytes, wanted: set[str]) -> dict[str, list[str]]:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(payload)
    result = {name: [] for name in wanted}
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1]
        if name in wanted and element.text and element.text.strip():
            result[name].append(element.text.strip())
    return {name: values for name, values in result.items() if values}


def official_s2_metadata(scene: dict[str, Any]) -> dict[str, Any]:
    product, product_lineage = _read_metadata_asset(scene, "product_metadata")
    granule, granule_lineage = _read_metadata_asset(scene, "granule_metadata")
    tileinfo, tileinfo_lineage = _read_metadata_asset(scene, "tileinfo_metadata")
    tile = json.loads(tileinfo)
    if not isinstance(tile, dict):
        raise ValueError("Sentinel-2 tileInfo metadata must be a JSON object.")
    return {
        "lineage": [product_lineage, granule_lineage, tileinfo_lineage],
        "product_fields": _xml_values(
            product,
            {
                "PRODUCT_URI",
                "PROCESSING_BASELINE",
                "PRODUCT_START_TIME",
                "PRODUCT_STOP_TIME",
                "BOA_QUANTIFICATION_VALUE",
                "BOA_ADD_OFFSET",
                "Cloud_Coverage_Assessment",
            },
        ),
        "granule_fields": _xml_values(
            granule,
            {
                "TILE_ID",
                "SENSING_TIME",
                "HORIZONTAL_CS_NAME",
                "HORIZONTAL_CS_CODE",
                "ZENITH_ANGLE",
                "AZIMUTH_ANGLE",
            },
        ),
        "tileinfo": {
            key: tile.get(key)
            for key in ("path", "timestamp", "utmZone", "latitudeBand", "gridSquare")
            if key in tile
        },
    }


def compute_s2_masks(
    pre_scl: np.ndarray,
    post_scl: np.ndarray,
    source_valid: np.ndarray,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    if pre_scl.shape != post_scl.shape or pre_scl.shape != source_valid.shape:
        raise ValueError("Sentinel-2 QA arrays must share one frozen grid.")
    scl_no_data = (pre_scl == S2_NO_DATA_CLASS) | (post_scl == S2_NO_DATA_CLASS)
    saturated_or_defective = (pre_scl == S2_SATURATED_DEFECTIVE_CLASS) | (
        post_scl == S2_SATURATED_DEFECTIVE_CLASS
    )
    unclassified = (pre_scl == S2_UNCLASSIFIED_CLASS) | (
        post_scl == S2_UNCLASSIFIED_CLASS
    )
    missing = ~source_valid | scl_no_data | saturated_or_defective
    cloud = np.isin(pre_scl, S2_CLOUD_CLASSES) | np.isin(
        post_scl, S2_CLOUD_CLASSES
    )
    cloud_shadow = (pre_scl == S2_CLOUD_SHADOW_CLASS) | (
        post_scl == S2_CLOUD_SHADOW_CLASS
    )
    shadow = (pre_scl == S2_CAST_SHADOW_CLASS) | (
        post_scl == S2_CAST_SHADOW_CLASS
    )
    water = (pre_scl == S2_WATER_CLASS) | (post_scl == S2_WATER_CLASS)
    zeros = np.zeros(source_valid.shape, dtype=bool)
    definite_forest_or_dense_vegetation = (
        (pre_scl == S2_VEGETATION_CLASS) | (post_scl == S2_VEGETATION_CLASS)
    ) & source_valid
    clear_nonforest = np.isin(pre_scl, (5, 6)) | np.isin(post_scl, (5, 6))
    forest_unknown = (
        source_valid & ~definite_forest_or_dense_vegetation & ~clear_nonforest
    )
    prior_deposit_unknown = source_valid.copy()
    survey_coverage = np.zeros(source_valid.shape, dtype=bool)
    masks = {
        "missing_data": missing,
        "scene_edge": ~source_valid | scl_no_data,
        "detection_exclusion": forest_unknown | unclassified | prior_deposit_unknown,
        "survey_coverage": survey_coverage,
        "cloud": cloud & source_valid,
        "cloud_shadow": cloud_shadow & source_valid,
        "shadow": shadow & source_valid,
        "forest": definite_forest_or_dense_vegetation,
        "water": water & source_valid,
        "layover": zeros,
        "radar_shadow": zeros,
        "prior_deposit": zeros,
    }
    invalid = np.logical_or.reduce([masks[name] for name in EXCLUSION_MASK_BANDS])
    masks["usable"] = source_valid & survey_coverage & ~invalid
    diagnostics = {
        "pre_scl_vegetation_pixels": int(
            ((pre_scl == S2_VEGETATION_CLASS) & source_valid).sum()
        ),
        "post_scl_vegetation_pixels": int(
            ((post_scl == S2_VEGETATION_CLASS) & source_valid).sum()
        ),
        "pre_scl_snow_pixels": int(((pre_scl == S2_SNOW_CLASS) & source_valid).sum()),
        "post_scl_snow_pixels": int(
            ((post_scl == S2_SNOW_CLASS) & source_valid).sum()
        ),
        "scl_no_data_pixels": int((scl_no_data & source_valid).sum()),
        "scl_saturated_or_defective_pixels": int(
            (saturated_or_defective & source_valid).sum()
        ),
        "scl_unclassified_pixels": int((unclassified & source_valid).sum()),
        "definite_forest_or_dense_vegetation_pixels": int(
            definite_forest_or_dense_vegetation.sum()
        ),
        "forest_unknown_pixels": int(forest_unknown.sum()),
        "prior_deposit_unknown_pixels": int(prior_deposit_unknown.sum()),
        "survey_coverage_pixels": 0,
        "survey_coverage_status": "unknown_pending_independent_human_review",
        "detection_exclusion_status": (
            "unresolved_forest_prior_deposit_and_component_detection_ambiguity"
        ),
        "unknown_is_invalid_not_zero": True,
    }
    return masks, diagnostics


def compute_s1_masks(source_valid: np.ndarray) -> dict[str, np.ndarray]:
    if source_valid.dtype != np.bool_:
        source_valid = source_valid.astype(bool)
    missing = ~source_valid
    zeros = np.zeros(source_valid.shape, dtype=bool)
    unresolved = source_valid.copy()
    masks = {
        "missing_data": missing,
        "scene_edge": missing,
        "detection_exclusion": unresolved,
        "survey_coverage": zeros,
        "cloud": zeros,
        "cloud_shadow": zeros,
        "shadow": zeros,
        "forest": zeros,
        "water": zeros,
        "layover": zeros,
        "radar_shadow": zeros,
        "prior_deposit": zeros,
    }
    invalid = np.logical_or.reduce([masks[name] for name in EXCLUSION_MASK_BANDS])
    masks["usable"] = source_valid & masks["survey_coverage"] & ~invalid
    return masks


def compute_processed_s1_masks(
    calibration_valid: np.ndarray,
    terrain_valid: np.ndarray,
    layover: np.ndarray,
    radar_shadow: np.ndarray,
) -> dict[str, np.ndarray]:
    """Use processed SAR masks while retaining unresolved observation masks."""

    shapes = {
        value.shape
        for value in (calibration_valid, terrain_valid, layover, radar_shadow)
    }
    if len(shapes) != 1:
        raise ValueError("Processed Sentinel-1 QA arrays must share one frozen grid.")
    calibrated = calibration_valid.astype(bool)
    terrain = terrain_valid.astype(bool)
    missing = ~calibrated | (calibrated & ~terrain)
    unresolved_terrain = calibrated & ~terrain
    source_valid = calibrated & terrain
    zeros = np.zeros(source_valid.shape, dtype=bool)
    unresolved_observation = source_valid.copy()
    masks = {
        "missing_data": missing,
        "scene_edge": ~calibrated,
        "detection_exclusion": unresolved_observation,
        "survey_coverage": zeros,
        "cloud": zeros,
        "cloud_shadow": zeros,
        "shadow": zeros,
        "forest": zeros,
        "water": zeros,
        "layover": layover.astype(bool),
        "radar_shadow": radar_shadow.astype(bool),
        "prior_deposit": zeros,
    }
    invalid = np.logical_or.reduce([masks[name] for name in EXCLUSION_MASK_BANDS])
    masks["usable"] = source_valid & masks["survey_coverage"] & ~invalid
    return masks


def _read_processed_s1(
    processing_candidate: dict[str, Any],
) -> tuple[dict[str, np.ndarray], dict[str, Any], dict[str, Any]]:
    try:
        import rasterio
    except ImportError as exc:
        raise RuntimeError("Pixel QA requires bake-time rasterio.") from exc

    combined: dict[str, np.ndarray] = {}
    reference_metadata: dict[str, Any] | None = None
    lineage = []
    for scene in processing_candidate["scenes"]:
        for polarization in scene["polarizations"]:
            path = _resolve_cache_path(polarization["output_path"])
            if _sha256_file(path) != polarization["output_sha256"]:
                raise ValueError(f"Processed Sentinel-1 hash mismatch at {path}.")
            with rasterio.open(path) as source:
                values = source.read(masked=False)
                if source.count != 8:
                    raise ValueError("Processed Sentinel-1 stack must contain eight bands.")
                metadata = {
                    "profile": source.profile.copy(),
                    "transform": source.transform,
                    "crs": source.crs,
                    "width": source.width,
                    "height": source.height,
                    "count": source.count,
                }
            current = {
                "calibration_valid": values[7] == 1,
                "terrain_valid": values[6] == 1,
                "layover": values[4] == 1,
                "radar_shadow": values[5] == 1,
            }
            for name, value in current.items():
                if name in {"calibration_valid", "terrain_valid"}:
                    combined[name] = (
                        value if name not in combined else combined[name] & value
                    )
                else:
                    combined[name] = (
                        value if name not in combined else combined[name] | value
                    )
            if reference_metadata is None:
                reference_metadata = metadata
            elif (
                metadata["width"] != reference_metadata["width"]
                or metadata["height"] != reference_metadata["height"]
                or metadata["transform"] != reference_metadata["transform"]
                or metadata["crs"] != reference_metadata["crs"]
            ):
                raise ValueError("Processed Sentinel-1 stacks do not share one grid.")
            lineage.append(
                {
                    "position": scene["position"],
                    "polarization": polarization["polarization"],
                    "path": polarization["output_path"],
                    "sha256": polarization["output_sha256"],
                    "counts": polarization["counts"],
                }
            )
    if reference_metadata is None:
        raise ValueError("Processed Sentinel-1 candidate has no raster stacks.")
    return combined, reference_metadata, {"processing_lineage": lineage}


def _qa_processed_s1(
    candidate_id: str,
    processing_candidate: dict[str, Any],
    cache_root: Path,
) -> dict[str, Any]:
    if not processing_candidate["status"].startswith("processed"):
        return {
            "status": "not_reached_no_processed_sentinel1_pair",
            "mask_stack": None,
            "technical_pixel_qa_complete": False,
            "required_masks_resolved": False,
            "automatic_pixel_qa_passed": False,
            "human_review_complete": False,
        }
    arrays, metadata, diagnostics = _read_processed_s1(processing_candidate)
    masks = compute_processed_s1_masks(**arrays)
    stack = _write_mask_stack(
        cache_root / candidate_id / "sentinel_1_grd-masks.tif", masks, metadata
    )
    diagnostics.update(
        {
            "radiometric_calibration_valid_pixels": int(
                arrays["calibration_valid"].sum()
            ),
            "terrain_gradient_valid_pixels": int(arrays["terrain_valid"].sum()),
            "layover_pixels": int(arrays["layover"].sum()),
            "radar_shadow_pixels": int(arrays["radar_shadow"].sum()),
            "unknown_is_invalid_not_zero": True,
        }
    )
    return {
        "status": "failed_automatic_pixel_qa_unresolved_required_observation_masks",
        "technical_pixel_qa_complete": True,
        "required_masks_resolved": False,
        "automatic_pixel_qa_passed": False,
        "human_review_complete": False,
        "source_valid_pixels": int(
            (arrays["calibration_valid"] & arrays["terrain_valid"]).sum()
        ),
        "total_pixels": int(arrays["calibration_valid"].size),
        "mask_stack": stack,
        "mask_semantics": {
            "missing_data": "invalid radiometric calibration or missing public-DTM gradient coverage",
            "scene_edge": "invalid radiometric-calibration coverage",
            "detection_exclusion": (
                "conservative invalid mask for unresolved forest, water, prior-deposit, "
                "component-attribution, and minimum-detectable-feature evidence"
            ),
            "survey_coverage": "no complete-search coverage asserted before human review",
            "cloud": "not applicable to SAR imagery",
            "cloud_shadow": "not applicable to SAR imagery",
            "shadow": "not applicable; SAR terrain shadow is stored separately",
            "forest": "no forest pixels asserted without independent land-cover evidence",
            "water": "no water pixels asserted without independent water evidence",
            "layover": "resolved from hashed SAFE incidence geometry and public DTM gradients",
            "radar_shadow": "resolved separately from hashed SAFE incidence geometry and public DTM gradients",
            "prior_deposit": "no prior-deposit pixels asserted before blind pre-event review",
            "usable": "no pixels until all required observation masks are resolved",
        },
        "diagnostics": diagnostics,
        "quantitative_observation_eligible": False,
    }


def _mask_counts(masks: dict[str, np.ndarray]) -> dict[str, int]:
    return {name: int(masks[name].sum()) for name in MASK_BANDS}


def _mask_payload_sha256(masks: dict[str, np.ndarray]) -> str:
    stacked = np.stack([masks[name].astype(np.uint8) for name in MASK_BANDS])
    header = {"bands": list(MASK_BANDS), "shape": list(stacked.shape), "dtype": "uint8"}
    return _sha256_bytes(_canonical_json(header) + stacked.tobytes(order="C"))


def _write_mask_stack(
    path: Path, masks: dict[str, np.ndarray], metadata: dict[str, Any]
) -> dict[str, Any]:
    try:
        import rasterio
    except ImportError as exc:
        raise RuntimeError("Pixel QA requires bake-time rasterio.") from exc
    stacked = np.stack([masks[name].astype(np.uint8) for name in MASK_BANDS])
    if not path.exists():
        profile = metadata["profile"].copy()
        profile.update(
            driver="GTiff",
            count=len(MASK_BANDS),
            dtype="uint8",
            nodata=None,
            compress="DEFLATE",
            predictor=2,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with rasterio.open(temporary, "w", **profile) as target:
                target.write(stacked)
                for index, name in enumerate(MASK_BANDS, start=1):
                    target.set_band_description(index, name)
            _write_immutable(path, temporary.read_bytes())
        finally:
            temporary.unlink(missing_ok=True)
    with rasterio.open(path) as source:
        stored = source.read()
        descriptions = tuple(source.descriptions)
    if not np.array_equal(stacked, stored) or descriptions != MASK_BANDS:
        raise ValueError(f"Stored pixel-QA stack differs from computed masks at {path}.")
    return {
        "cache_path": _stable_path(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "mask_payload_sha256": _mask_payload_sha256(masks),
        "bands": list(MASK_BANDS),
        "counts": _mask_counts(masks),
    }


def _sensor_source_valid(
    sensor: str, sensor_result: dict[str, Any]
) -> tuple[np.ndarray, dict[str, Any], dict[str, Any]]:
    pre, post = sensor_result["scenes"]
    if pre["position"] != "pre" or post["position"] != "post":
        raise ValueError("Acquisition scenes must preserve pre/post order.")
    if sensor == "sentinel_2_l2a":
        required = ("visual", "nir", "swir16", "scl", "cloud", "snow")
    else:
        required = tuple(
            sorted(
                asset["asset_name"]
                for asset in pre["assets"]
                if asset["asset_name"] in {"hh", "hv", "vh", "vv"}
            )
        )
    source_valid: np.ndarray | None = None
    reference_metadata: dict[str, Any] | None = None
    arrays_by_position: dict[str, dict[str, np.ndarray]] = {"pre": {}, "post": {}}
    for scene in (pre, post):
        for name in required:
            data, valid, metadata = _read_asset(scene, name)
            band_valid = valid.all(axis=0)
            if name in {"hh", "hv", "vh", "vv"}:
                band_valid &= (data != 0).all(axis=0)
            source_valid = band_valid if source_valid is None else source_valid & band_valid
            arrays_by_position[scene["position"]][name] = data
            if reference_metadata is None:
                reference_metadata = metadata
            elif (
                metadata["width"] != reference_metadata["width"]
                or metadata["height"] != reference_metadata["height"]
                or metadata["transform"] != reference_metadata["transform"]
                or metadata["crs"] != reference_metadata["crs"]
            ):
                raise ValueError("All acquired assets must share the frozen chip grid.")
    assert source_valid is not None and reference_metadata is not None
    return source_valid, reference_metadata, arrays_by_position


def _finite_percentiles(values: np.ndarray) -> dict[str, float | None]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"p05": None, "median": None, "p95": None}
    p05, median, p95 = np.percentile(finite, [5, 50, 95])
    return {"p05": float(p05), "median": float(median), "p95": float(p95)}


def _qa_sensor(
    candidate_id: str,
    sensor: str,
    sensor_result: dict[str, Any],
    cache_root: Path,
) -> dict[str, Any]:
    if sensor_result["status"] != "acquired_requires_pixel_qa":
        return {
            "status": "not_reached_no_acquired_pair",
            "mask_stack": None,
            "technical_pixel_qa_complete": False,
            "required_masks_resolved": False,
            "automatic_pixel_qa_passed": False,
            "human_review_complete": False,
        }
    source_valid, metadata, arrays = _sensor_source_valid(sensor, sensor_result)
    diagnostics: dict[str, Any]
    if sensor == "sentinel_2_l2a":
        metadata_by_position = {
            scene["position"]: official_s2_metadata(scene)
            for scene in sensor_result["scenes"]
        }
        masks, diagnostics = compute_s2_masks(
            arrays["pre"]["scl"][0], arrays["post"]["scl"][0], source_valid
        )
        cloud_values = np.concatenate(
            [arrays["pre"]["cloud"][0][source_valid], arrays["post"]["cloud"][0][source_valid]]
        )
        snow_values = np.concatenate(
            [arrays["pre"]["snow"][0][source_valid], arrays["post"]["snow"][0][source_valid]]
        )
        diagnostics.update(
            {
                "cloud_probability_diagnostic": _finite_percentiles(cloud_values),
                "snow_probability_diagnostic": _finite_percentiles(snow_values),
                "probability_layers_are_diagnostic_not_thresholded": True,
                "official_product_metadata": metadata_by_position,
            }
        )
        mask_semantics = {
            "missing_data": (
                "invalid source/read-mask pixels or official SCL class 0/1; SCL class 7 "
                "is separately counted as classification ambiguity"
            ),
            "scene_edge": "invalid source/read-mask pixels or official SCL class 0",
            "detection_exclusion": (
                "forest-hidden, unclassified, prior-deposit, and component-detection "
                "ambiguity retained as conservative invalid"
            ),
            "survey_coverage": "no complete-search coverage asserted before human review",
            "cloud": "resolved from Sentinel-2 SCL classes 8, 9, and 10 in either scene",
            "cloud_shadow": "resolved from official Sentinel-2 SCL class 3 in either scene",
            "shadow": "resolved from official Sentinel-2 SCL class 2 in either scene",
            "forest": (
                "SCL class 4 is definite vegetation; forest-hidden ambiguity is retained "
                "in detection_exclusion rather than falsely labelled forest"
            ),
            "water": "resolved from Sentinel-2 SCL class 6 in either scene",
            "layover": "not applicable to optical imagery",
            "radar_shadow": "not applicable to optical imagery",
            "prior_deposit": "no prior-deposit pixels asserted before blind human comparison",
            "usable": "valid only where no resolved or unresolved-conservative mask applies",
        }
    else:
        masks = compute_s1_masks(source_valid)
        diagnostics = {"raw_dn_change_by_polarization": {}}
        for name in arrays["pre"]:
            pre = arrays["pre"][name][0].astype(np.float64)
            post = arrays["post"][name][0].astype(np.float64)
            change = 10.0 * np.log10(np.maximum(post, 1.0)) - 10.0 * np.log10(
                np.maximum(pre, 1.0)
            )
            diagnostics["raw_dn_change_by_polarization"][name] = _finite_percentiles(
                change[source_valid]
            )
        diagnostics["raw_dn_change_interpretation"] = (
            "Diagnostic only: GCP-warped GRD digital numbers without completed radiometric "
            "terrain correction, calibration/noise processing, or local incidence correction."
        )
        mask_semantics = {
            "missing_data": "invalid source/read-mask or zero-DN pixels in any required asset",
            "scene_edge": "invalid source/read-mask or zero-DN pixels in any required asset",
            "detection_exclusion": "all valid pixels excluded until terrain and observation masks exist",
            "survey_coverage": "no complete-search coverage asserted before human review",
            "cloud": "not applicable to SAR imagery",
            "cloud_shadow": "not applicable to SAR imagery",
            "shadow": "not applicable; radar shadow requires processed SAR geometry",
            "forest": "no forest pixels asserted without independent land-cover evidence",
            "water": "no water pixels asserted without independent water evidence",
            "layover": "no layover pixels asserted before processed SAR geometry",
            "radar_shadow": "no radar-shadow pixels asserted before processed SAR geometry",
            "prior_deposit": "no prior-deposit pixels asserted before blind human comparison",
            "usable": "valid only where no resolved or unresolved-conservative mask applies",
        }
    stack = _write_mask_stack(
        cache_root / candidate_id / f"{sensor}-masks.tif", masks, metadata
    )
    automatic_passed = stack["counts"]["usable"] > 0 and all(
        "unresolved_conservative_invalid" not in text for text in mask_semantics.values()
    )
    return {
        "status": (
            "automatic_pixel_qa_passed_requires_human_review"
            if automatic_passed
            else "failed_automatic_pixel_qa_unresolved_required_masks"
        ),
        "automatic_pixel_qa_passed": automatic_passed,
        "technical_pixel_qa_complete": True,
        "required_masks_resolved": automatic_passed,
        "human_review_complete": False,
        "source_valid_pixels": int(source_valid.sum()),
        "total_pixels": int(source_valid.size),
        "mask_stack": stack,
        "mask_semantics": mask_semantics,
        "diagnostics": diagnostics,
        "quantitative_observation_eligible": False,
    }


def build_pixel_qa(
    acquisition_path: Path, cache_root: Path, s1_processing_path: Path
) -> dict[str, Any]:
    acquisition_bytes = acquisition_path.read_bytes()
    acquisition = json.loads(acquisition_bytes)
    s1_processing_bytes = s1_processing_path.read_bytes()
    s1_processing = json.loads(s1_processing_bytes)
    if acquisition.get("schema") != "avycore-public-event-imagery-acquisition-v2":
        raise ValueError("Unexpected imagery acquisition schema.")
    if s1_processing.get("schema") != "avycore-public-event-sentinel1-processing-v1":
        raise ValueError("Unexpected Sentinel-1 processing schema.")
    processing_by_id = {
        candidate["candidate_id"]: candidate
        for candidate in s1_processing["candidates"]
    }
    candidates = []
    for candidate in acquisition["candidates"]:
        candidate_id = candidate["candidate_id"]
        result = {
            "candidate_id": candidate_id,
            "sentinel_1_grd": _qa_processed_s1(
                candidate_id,
                processing_by_id[candidate_id],
                cache_root,
            ),
            "sentinel_2_l2a": _qa_sensor(
                candidate_id,
                "sentinel_2_l2a",
                candidate["sentinel_2_l2a"],
                cache_root,
            ),
            "prediction_or_model_overlay_accessed": False,
            "regobs_target_geometry_accessed": False,
            "human_review_complete": False,
            "validation_contract_eligible": False,
        }
        result["normalized_candidate_sha256"] = _sha256_bytes(_canonical_json(result))
        candidates.append(result)
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "qa_id": QA_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "source_acquisition_sha256": _sha256_bytes(acquisition_bytes),
        "source_sentinel1_processing_sha256": _sha256_bytes(s1_processing_bytes),
        "stage": "reproducible_conservative_pixel_qa_before_target_access",
        "predictions_generated": False,
        "model_code_imported": False,
        "holdout_partition_assigned": False,
        "holdout_targets_accessed": False,
        "regobs_attachments_accessed": False,
        "regobs_start_stop_target_coordinates_accessed": False,
        "prototype_disclaimer": (
            "Experimental research prototype only. It does not replace Avalanche Canada "
            "guidance or field assessment. Model scores are relative indices, not probabilities."
        ),
        "claim_boundary": (
            "Automated pixel masks are QA evidence only. They are not avalanche annotations or "
            "reviewed ground truth. Unresolved required masks invalidate pixels rather than "
            "turning missing knowledge into zero."
        ),
        "mask_band_order": list(MASK_BANDS),
        "cache_reference": _stable_path(cache_root),
        "counts": {
            "candidates": len(candidates),
            "sentinel_1_pairs_masked": sum(
                item["sentinel_1_grd"]["mask_stack"] is not None for item in candidates
            ),
            "sentinel_2_pairs_masked": sum(
                item["sentinel_2_l2a"]["mask_stack"] is not None for item in candidates
            ),
            "sentinel_1_automatic_pass": sum(
                item["sentinel_1_grd"]["automatic_pixel_qa_passed"] for item in candidates
            ),
            "sentinel_2_automatic_pass": sum(
                item["sentinel_2_l2a"]["automatic_pixel_qa_passed"] for item in candidates
            ),
            "human_review_complete": 0,
            "quantitative_observation_eligible": 0,
            "predictions_generated": 0,
            "holdouts_assigned": 0,
        },
        "candidates": candidates,
    }
    artifact["normalized_artifact_sha256"] = _sha256_bytes(_canonical_json(artifact))
    return artifact


def _assert_outside_protected(path: Path, label: str) -> None:
    resolved = path.resolve()
    for protected in (
        REPOSITORY_ROOT / "runtime",
        REPOSITORY_ROOT / "DATA",
        REPOSITORY_ROOT.parent / "DATA",
    ):
        try:
            resolved.relative_to(protected.resolve())
        except ValueError:
            continue
        raise ValueError(f"{label} may not be under protected path {protected}.")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--acquisition",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-imagery-acquisition-v2.json",
    )
    parser.add_argument(
        "--sentinel1-processing",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-sentinel1-processing-v1.json",
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=REPOSITORY_ROOT
        / ".validation-cache"
        / "public-event-pixel-qa-v3",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT
        / "validation-data"
        / "candidates"
        / "public-event-pixel-qa-v3.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _arguments()
    cache_root = args.cache_root.resolve()
    output = args.output.resolve()
    _assert_outside_protected(cache_root, "Pixel-QA cache")
    _assert_outside_protected(output, "Pixel-QA manifest")
    artifact = build_pixel_qa(
        args.acquisition.resolve(),
        cache_root,
        args.sentinel1_processing.resolve(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(_pretty_json(artifact))
    print(
        f"Wrote {artifact['counts']['candidates']} candidates to {output}; "
        f"S1 masked={artifact['counts']['sentinel_1_pairs_masked']}, "
        f"S2 masked={artifact['counts']['sentinel_2_pairs_masked']}, "
        f"automatic passes=0, human reviews=0."
    )


if __name__ == "__main__":
    main()
