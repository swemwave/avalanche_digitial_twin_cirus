from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validation"
    / "process_public_event_sentinel1.py"
)
SPEC = importlib.util.spec_from_file_location("process_public_event_sentinel1", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_bilinear_safe_lut_does_not_extrapolate() -> None:
    lines = np.array([0.0, 10.0])
    pixels = np.array([0.0, 20.0])
    values = np.array([[0.0, 20.0], [10.0, 30.0]])
    query_lines = np.array([[5.0, -1.0, 10.0]])
    query_pixels = np.array([[10.0, 10.0, 20.0]])
    result = MODULE.bilinear_lut(
        lines, pixels, values, query_lines, query_pixels
    )
    assert result[0, 0] == pytest.approx(15.0)
    assert np.isnan(result[0, 1])
    assert result[0, 2] == pytest.approx(30.0)


def test_irregular_safe_lut_interpolates_rows_without_extrapolation() -> None:
    vectors = [
        (0.0, np.array([0.0, 10.0]), np.array([0.0, 10.0])),
        (10.0, np.array([2.0, 12.0]), np.array([12.0, 22.0])),
    ]
    query_lines = np.array([[5.0, 5.0, 5.0]])
    query_pixels = np.array([[5.0, 1.0, 11.0]])
    result = MODULE.irregular_bilinear_lut(
        vectors, query_lines, query_pixels
    )
    assert result[0, 0] == pytest.approx(10.0)
    assert np.isnan(result[0, 1])  # outside the upper row's supported samples
    assert np.isnan(result[0, 2])  # outside the lower row's supported samples


def test_radiometric_calibration_subtracts_noise_in_power_domain() -> None:
    dn = np.array([[10, 2, 0]], dtype=np.uint16)
    sigma_lut = np.array([[2.0, 2.0, 2.0]])
    noise_lut = np.array([[4.0, 9.0, 0.0]])
    sigma0, valid = MODULE.calibrate_sigma0(dn, sigma_lut, noise_lut)
    assert sigma0[0, 0] == pytest.approx(24.0)
    assert sigma0[0, 1] == pytest.approx(0.0)
    assert np.isnan(sigma0[0, 2])
    assert valid.tolist() == [[True, True, False]]


def test_noise_azimuth_requires_exactly_one_swath_assignment() -> None:
    vectors = [
        {
            "swath": "IW1",
            "first_line": 0,
            "last_line": 10,
            "first_pixel": 0,
            "last_pixel": 5,
            "lines": np.array([0.0, 10.0]),
            "lut": np.array([1.0, 2.0]),
        },
        {
            "swath": "IW2",
            "first_line": 0,
            "last_line": 10,
            "first_pixel": 5,
            "last_pixel": 10,
            "lines": np.array([0.0, 10.0]),
            "lut": np.array([3.0, 4.0]),
        },
    ]
    lines = np.array([[5.0, 5.0, 5.0]])
    pixels = np.array([[2.0, 5.0, 8.0]])
    factor = MODULE.noise_azimuth_factor(vectors, lines, pixels)
    assert factor[0, 0] == pytest.approx(1.5)
    assert np.isnan(factor[0, 1])  # ambiguous inclusive swath boundary
    assert factor[0, 2] == pytest.approx(3.5)


def test_flat_terrain_preserves_sigma0_and_has_no_visibility_masks() -> None:
    sigma0 = np.ones((7, 7), dtype=np.float64)
    dem = np.full((7, 7), 1000.0)
    incidence = np.full((7, 7), 35.0)
    result = MODULE.terrain_normalize(sigma0, dem, incidence, 90.0, 10.0)
    interior = np.s_[1:-1, 1:-1]
    assert np.allclose(result["terrain_normalized_sigma0"][interior], 1.0)
    assert not result["layover"].any()
    assert not result["radar_shadow"].any()
    assert result["terrain_gradient_valid"].sum() == 25


def test_missing_dem_never_becomes_zero_or_usable() -> None:
    sigma0 = np.ones((7, 7), dtype=np.float64)
    dem = np.full((7, 7), 1000.0)
    dem[3, 3] = np.nan
    incidence = np.full((7, 7), 35.0)
    result = MODULE.terrain_normalize(sigma0, dem, incidence, 90.0, 10.0)
    assert not result["terrain_gradient_valid"][3, 3]
    assert np.isnan(result["terrain_normalized_sigma0"][3, 3])
    assert not result["terrain_usable"][3, 3]
