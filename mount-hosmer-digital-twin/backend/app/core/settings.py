from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_VERSION = "1.1.0"


class ConfigurationError(RuntimeError):
    """Raised at startup when configuration cannot produce a usable layout."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Application paths and bake tunables, populated by pydantic-settings."""

    model_config = SettingsConfigDict(
        env_file=_project_root() / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    project_root: Path = Field(default_factory=_project_root)
    backend_root: Path = Field(default_factory=lambda: _project_root() / "backend")
    runtime_root: Path = Field(
        default_factory=lambda: _project_root() / "runtime",
        validation_alias=AliasChoices(
            "AVALANCHE_RUNTIME_ROOT", "MOUNT_HOSMER_RUNTIME_ROOT", "runtime_root"
        ),
    )
    data_root: Path = Field(
        default_factory=lambda: _project_root().parent / "DATA" / "mount_hosmer_data",
        validation_alias=AliasChoices(
            "AVALANCHE_DATA_ROOT", "MOUNT_HOSMER_DATA_ROOT", "data_root"
        ),
    )
    mountain_pack_path: Path = Field(
        default_factory=lambda: _project_root() / "backend" / "config" / "mount_hosmer.pack.json",
        validation_alias=AliasChoices("AVALANCHE_MOUNTAIN_PACK", "mountain_pack_path"),
    )
    terrain_resolution_m: float = Field(
        default=5.0, gt=0, validation_alias="AVALANCHE_TERRAIN_RESOLUTION_M"
    )
    app_version: str = APP_VERSION
    cors_origins: tuple[str, ...] = Field(
        default=("http://localhost:3000", "http://127.0.0.1:3000"),
        validation_alias="AVALANCHE_CORS_ORIGINS",
    )

    @field_validator("runtime_root", "data_root", "mountain_pack_path", mode="after")
    @classmethod
    def _resolved_path(cls, value: Path) -> Path:
        return (value if value.is_absolute() else _project_root() / value).resolve()

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @property
    def config_root(self) -> Path:
        return self.backend_root / "config"

    @classmethod
    def from_environment(cls) -> "Settings":
        try:
            return cls()
        except ValidationError as exc:
            raise ConfigurationError(str(exc)) from exc

    def validate(self, *, require_data_root: bool = True) -> list[str]:
        """Validate writable/read-only roots and return non-fatal warnings."""
        warnings: list[str] = []
        if require_data_root and not self.data_root.exists():
            raise ConfigurationError(
                f"Source data root does not exist: {self.data_root}\n"
                "  Set AVALANCHE_DATA_ROOT to the folder containing the bake inputs."
            )
        if require_data_root and not self.mountain_pack_path.is_file():
            raise ConfigurationError(
                f"Mountain pack does not exist: {self.mountain_pack_path}\n"
                "  Set AVALANCHE_MOUNTAIN_PACK to a validated pack JSON file."
            )
        if self.data_root.exists():
            for required in ("metadata", "static"):
                if not (self.data_root / required).is_dir():
                    warnings.append(
                        f"Source data root is missing the '{required}/' folder: {self.data_root}"
                    )
        try:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigurationError(
                f"Cannot create the runtime directory {self.runtime_root}: {exc}"
            ) from exc
        if self.runtime_root == self.data_root or self.runtime_root.is_relative_to(self.data_root):
            raise ConfigurationError(
                f"The runtime root ({self.runtime_root}) must not live inside the source data "
                f"root ({self.data_root}). Source data is strictly read-only."
            )
        return warnings


_cached: Settings | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    global _cached
    if refresh or _cached is None:
        _cached = Settings.from_environment()
    return _cached
