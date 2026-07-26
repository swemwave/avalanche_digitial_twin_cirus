"""Small numeric helpers shared by the bake and the runtime.

numpy only -- no rasterio, no pyproj, no yaml -- so the runtime may import this
freely (see the import rules in CLAUDE.md §6).
"""

from __future__ import annotations

import numpy as np


def piecewise(values: np.ndarray, breakpoints: list[float], scores: list[float]) -> np.ndarray:
    """Score ``values`` by explicit physical breakpoints. **Never** a percentile stretch.

    This distinction is the reason the function exists rather than being inlined.
    Interpolating against fixed physical breakpoints means a score of 85 denotes the
    same slope angle in every raster, on every run, under every set of conditions. A
    percentile stretch would instead rank each raster against *itself*, so the same
    ground would score differently depending on what it was sitting next to -- and a
    uniformly gentle mountain would still produce a field of 100s.

    ``breakpoints`` must be ascending; values outside the range clamp to the end
    scores, which is ``np.interp``'s behaviour and the intended one here.
    """
    return np.interp(values, breakpoints, scores).astype("float32")
