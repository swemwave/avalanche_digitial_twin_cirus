"""Loader for the versioned model configuration.

The configuration is hashed into every analysis. Two results produced by
different configurations are not comparable, and the stored hash is what lets the
API prove which one produced a given number.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.settings import Settings

CONFIG_FILENAME = "avalanche_model.yaml"

DISCLAIMER = (
    "Physics-informed estimate from an advanced terrain- and conditions-based avalanche "
    "digital twin. This is NOT an operational avalanche forecast and is NOT a calibrated "
    "avalanche probability. It has not been validated against observed Mount Hosmer "
    "avalanches, because no historical avalanche record exists for this mountain. It must "
    "never replace Avalanche Canada forecasts or field assessment."
)


class ModelConfigError(RuntimeError):
    pass


class ModelConfig:
    """Read-only view over avalanche_model.yaml with dotted-path access."""

    def __init__(self, data: dict[str, Any], path: Path, sha256: str) -> None:
        self._data = data
        self.path = path
        self.sha256 = sha256

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise ModelConfigError(
                f"Required model parameter {dotted!r} is missing from {self.path.name}. "
                f"Refusing to substitute a default for a scientific parameter."
            )
        return value

    def normalized_weights(self, dotted: str) -> dict[str, float]:
        """Weights rescaled to sum to 1, so only their ratios matter."""
        raw = self.get(dotted, {}) or {}
        values = {key: float(value) for key, value in raw.items()}
        total = sum(values.values())
        if total <= 0:
            raise ModelConfigError(f"Weights at {dotted!r} sum to {total}; they must be positive.")
        return {key: value / total for key, value in values.items()}

_MISSING = object()


def config_path(settings: Settings) -> Path:
    return settings.config_root / CONFIG_FILENAME


@lru_cache(maxsize=4)
def _load(path_str: str, mtime: float) -> ModelConfig:
    # yaml is a bake-time dependency only: the Stage 3 runtime builds ModelConfig
    # from an embedded dict (see app.assess), never from a YAML file. Importing it
    # lazily keeps pyyaml out of the runtime dependency set.
    import yaml

    path = Path(path_str)
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ModelConfigError(f"{path.name} did not parse into a mapping.")
    return ModelConfig(data, path, hashlib.sha256(raw).hexdigest())


def load_model_config(settings: Settings) -> ModelConfig:
    path = config_path(settings)
    if not path.exists():
        raise ModelConfigError(
            f"Model configuration not found at {path}. The application will not fall back to "
            f"hard-coded scientific parameters -- fix the deployment instead."
        )
    return _load(str(path), path.stat().st_mtime)
