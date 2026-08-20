from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "build_public_event_pixel_qa.py"
)
SPEC = importlib.util.spec_from_file_location("build_public_event_pixel_qa", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_s2_masks_keep_unresolved_forest_and_prior_deposit_invalid() -> None:
    pre = np.array([[11, 8, 3], [6, 4, 11]], dtype=np.uint8)
    post = np.array([[11, 11, 11], [11, 11, 11]], dtype=np.uint8)
    valid = np.ones((2, 3), dtype=bool)
    valid[1, 2] = False

    masks, diagnostics = MODULE.compute_s2_masks(pre, post, valid)

    assert masks["cloud"][0, 1]
    assert masks["cloud_shadow"][0, 2]
    assert masks["water"][1, 0]
    assert masks["missing_data"][1, 2]
    assert masks["forest"][1, 1]
    assert not masks["prior_deposit"].any()
    assert masks["detection_exclusion"][0, 0]
    assert not masks["survey_coverage"].any()
    assert not masks["usable"].any()
    assert diagnostics["pre_scl_vegetation_pixels"] == 1
    assert diagnostics["unknown_is_invalid_not_zero"] is True


def test_s2_official_no_data_defective_and_cast_shadow_are_masked() -> None:
    pre = np.array([[0, 1, 2, 5]], dtype=np.uint8)
    post = np.full((1, 4), 11, dtype=np.uint8)
    valid = np.ones((1, 4), dtype=bool)
    masks, diagnostics = MODULE.compute_s2_masks(pre, post, valid)
    assert masks["scene_edge"][0, 0]
    assert masks["missing_data"][0, 0]
    assert masks["missing_data"][0, 1]
    assert masks["shadow"][0, 2]
    assert not masks["forest"][0, 3]  # official clear non-vegetated observation
    assert diagnostics["scl_no_data_pixels"] == 1
    assert diagnostics["scl_saturated_or_defective_pixels"] == 1


def test_s1_masks_do_not_convert_unresolved_terrain_to_safe_zero() -> None:
    valid = np.array([[True, False], [True, True]])
    masks = MODULE.compute_s1_masks(valid)
    assert masks["detection_exclusion"][0, 0]
    assert not masks["layover"].any()
    assert not masks["radar_shadow"].any()
    assert not masks["forest"].any()
    assert not masks["water"].any()
    assert not masks["prior_deposit"].any()
    assert masks["missing_data"][0, 1]
    assert not masks["usable"].any()
    assert not masks["cloud"].any()
    assert not masks["cloud_shadow"].any()


def test_processed_s1_uses_resolved_visibility_but_retains_unknown_masks() -> None:
    calibration = np.array([[True, True], [False, True]])
    terrain = np.array([[True, False], [False, True]])
    layover = np.array([[False, False], [False, True]])
    shadow = np.zeros((2, 2), dtype=bool)
    masks = MODULE.compute_processed_s1_masks(
        calibration, terrain, layover, shadow
    )
    assert not masks["layover"][0, 0]
    assert not masks["layover"][0, 1]  # DTM gap is missing, never false layover
    assert masks["layover"][1, 1]
    assert not masks["radar_shadow"].any()
    assert masks["detection_exclusion"][0, 0]
    assert masks["missing_data"][1, 0]
    assert not masks["forest"].any()
    assert not masks["water"].any()
    assert not masks["prior_deposit"].any()
    assert not masks["usable"].any()


def test_mask_payload_identity_binds_band_meaning_and_pixels() -> None:
    masks = {
        name: np.zeros((2, 2), dtype=bool) for name in MODULE.MASK_BANDS
    }
    first = MODULE._mask_payload_sha256(masks)
    masks["missing_data"][0, 0] = True
    second = MODULE._mask_payload_sha256(masks)
    assert first != second
