"""Structural protocols that keep AvyCore independent of application classes."""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np


class Grid(Protocol):
    resolution_m: float
    shape: tuple[int, int]


class Terrain(Protocol):
    grid: Grid
    reproject: Any

    def layer(self, name: str) -> np.ma.MaskedArray: ...


class Parameters(Protocol):
    def require(self, dotted: str) -> Any: ...
