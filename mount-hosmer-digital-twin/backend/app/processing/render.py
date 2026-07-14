"""Browser-facing renderings of analysis rasters.

A 2400x2400 float32 terrain layer is 23 MB. Twenty-five of them is more than half
a gigabyte, and none of it is something a browser can display. So every raster
gets a PNG preview, downsampled to a bounded size, with NoData rendered as
transparent rather than as a colour -- because a hole in the data must look like a
hole, not like a low value.
"""

from __future__ import annotations

import colorsys
from pathlib import Path

import numpy as np
from PIL import Image

#: Longest edge of a generated preview, in pixels. 1600 keeps the 12 km AOI
#: legible at full-screen zoom while staying well under a megabyte on disk.
MAX_PREVIEW_PX = 1600

RAMPS: dict[str, list[str]] = {
    "elevation": ["#2d4739", "#5d7a52", "#a08d5e", "#cfc0a3", "#f2efe6"],
    "hillshade": ["#000000", "#ffffff"],
    "slope": ["#2f8a4c", "#7fbf5a", "#d6c64b", "#dd8f35", "#c23b35", "#7b2320"],
    "diverging": ["#2166ac", "#82b7d8", "#f2f2f2", "#e6997a", "#b2182b"],
    "hazard": ["#2f8a4c", "#d6c64b", "#dd8f35", "#c23b35", "#7a1f1c"],
    "water": ["#0b2545", "#1f6f8b", "#4fb0c6", "#a7e8f2"],
    "snow": ["#1b3a4b", "#4a90a4", "#a8d5e2", "#ffffff"],
    "thermal": ["#2c1a4a", "#2e5c9a", "#4bb1a5", "#f2d16b", "#d9552b"],
    "canopy": ["#f5f0e1", "#9dbf6e", "#3f7a3f", "#12401f"],
    "confidence": ["#7a1f1c", "#c23b35", "#d6c64b", "#7fbf5a", "#2f8a4c"],
}


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def downsample(array: np.ma.MaskedArray, max_px: int = MAX_PREVIEW_PX) -> np.ma.MaskedArray:
    """Decimate by an integer stride so preview pixels stay aligned with data pixels."""
    height, width = array.shape
    stride = max(1, int(np.ceil(max(height, width) / max_px)))
    if stride == 1:
        return array
    return array[::stride, ::stride]


def stretch(
    array: np.ma.MaskedArray,
    low: float | None = None,
    high: float | None = None,
    *,
    symmetric: bool = False,
) -> np.ndarray:
    """Map values to 0-1 for display.

    Percentile limits are used only when explicit ones are not supplied. This is
    a *display* stretch and must never be reused for scoring: a 2-98 percentile
    rescale makes the highest value in any raster look extreme, whether or not it
    is. Scoring paths use explicit physical breakpoints instead.
    """
    values = array.compressed() if np.ma.is_masked(array) else np.asarray(array).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return np.zeros(array.shape, dtype="float32")

    if symmetric:
        limit = float(np.percentile(np.abs(values), 98)) or 1.0
        low, high = -limit, limit
    else:
        low = float(np.percentile(values, 2)) if low is None else low
        high = float(np.percentile(values, 98)) if high is None else high
    if high <= low:
        high = low + 1.0

    filled = np.asarray(np.ma.asarray(array).filled(low), dtype="float32")
    return np.clip((filled - low) / (high - low), 0.0, 1.0)


def colorize(
    array: np.ma.MaskedArray,
    ramp: str | list[str] = "elevation",
    *,
    low: float | None = None,
    high: float | None = None,
    symmetric: bool = False,
    alpha: int = 220,
) -> np.ndarray:
    colors = RAMPS.get(ramp, RAMPS["elevation"]) if isinstance(ramp, str) else ramp
    scaled = stretch(array, low, high, symmetric=symmetric)
    stops = np.linspace(0.0, 1.0, len(colors))
    channels = np.array([_rgb(color) for color in colors], dtype="float32")

    rgba = np.zeros((*scaled.shape, 4), dtype=np.uint8)
    for channel in range(3):
        rgba[..., channel] = np.interp(scaled, stops, channels[:, channel]).astype(np.uint8)
    rgba[..., 3] = alpha
    rgba[np.ma.getmaskarray(array)] = (0, 0, 0, 0)
    return rgba


def colorize_aspect(aspect: np.ma.MaskedArray) -> np.ndarray:
    """Aspect as a hue wheel. Flat ground (-1) renders as neutral grey, not as north."""
    data = np.asarray(aspect.filled(-1.0), dtype="float32")
    flat = data < 0
    hue = (np.where(flat, 0.0, data) % 360.0) / 360.0
    rgb = np.array(
        [colorsys.hsv_to_rgb(float(value), 0.62, 0.95) for value in hue.ravel()], dtype="float32"
    ).reshape((*data.shape, 3))

    rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
    rgba[..., :3] = (rgb * 255).astype(np.uint8)
    rgba[..., 3] = 210
    rgba[flat] = (150, 150, 150, 120)
    rgba[np.ma.getmaskarray(aspect)] = (0, 0, 0, 0)
    return rgba


def colorize_classes(
    array: np.ma.MaskedArray, colors: dict[int, str], alpha: int = 215
) -> np.ndarray:
    """Render a class raster with one flat colour per class. Never interpolated."""
    data = np.asarray(array.filled(-9999)).astype(int)
    rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
    for value, color in colors.items():
        rgba[data == value] = (*_rgb(color), alpha)
    rgba[np.ma.getmaskarray(array)] = (0, 0, 0, 0)
    return rgba


def colorize_mask(mask: np.ndarray, color: str, alpha: int = 220) -> np.ndarray:
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    active = np.asarray(np.ma.asarray(mask).filled(0)) > 0
    rgba[active] = (*_rgb(color), alpha)
    return rgba


def save_png(path: Path, rgba: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(path, optimize=True)
    return path


def render_preview(
    path: Path,
    array: np.ma.MaskedArray,
    ramp: str | list[str] = "elevation",
    *,
    low: float | None = None,
    high: float | None = None,
    symmetric: bool = False,
    alpha: int = 220,
) -> Path:
    return save_png(
        path,
        colorize(downsample(array), ramp, low=low, high=high, symmetric=symmetric, alpha=alpha),
    )
