from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

APP_VERSION = "1.0.0"

# Version of the scientific model configuration. Bump this whenever a scoring
# formula, weight schema, or threshold changes in a way that alters results, so
# stored analyses remain reproducible against the model that produced them.
MODEL_VERSION = "1.0.0"

ANALYSIS_CRS = "EPSG:26911"

#: Vocabulary used for every provenance-carrying value in the system. Enforced
#: by ``app.core.provenance``; see docs/production-upgrade-plan.md.
PROVENANCE_VALUES = (
    "observed",
    "downloaded",
    "derived",
    "interpolated",
    "modelled",
    "user_supplied",
    "unavailable",
)


class ConfigurationError(RuntimeError):
    """Raised at startup when configuration cannot produce a usable layout."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(*names: str, default: str | None = None) -> str | None:
    """First non-empty value among ``names``. Earlier names win."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _resolve(project_root: Path, value: str) -> Path:
    """Resolve a configured path, allowing values relative to the project root.

    Written so ``.\\data\\derived`` (Windows), ``./data/derived`` (POSIX), and
    ``/data/derived`` (container volume mount) all behave sensibly.
    """
    candidate = Path(value.strip().strip('"'))
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero, got {parsed}")
    return parsed


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Resolved application layout and tunables.

    Only the four roots are required, so tests and short-lived scripts can build
    a Settings against a temp directory without restating every tunable.
    """

    project_root: Path
    backend_root: Path
    runtime_root: Path
    data_root: Path
    derived_root: Path | None = None
    simulation_root: Path | None = None
    database_url: str | None = None
    redis_url: str | None = None
    app_env: str = "development"
    terrain_resolution_m: float = 5.0
    environmental_resolution_m: float = 10.0
    fallback_resolution_m: float = 30.0
    jobs_enabled: bool = True
    scheduler_enabled: bool = False
    max_job_workers: int = 2
    app_version: str = APP_VERSION
    model_version: str = MODEL_VERSION
    analysis_crs: str = ANALYSIS_CRS
    cors_origins: tuple[str, ...] = field(
        default=("http://localhost:3000", "http://127.0.0.1:3000")
    )

    def __post_init__(self) -> None:
        if self.derived_root is None:
            object.__setattr__(self, "derived_root", self.runtime_root / "derived")
        if self.simulation_root is None:
            object.__setattr__(self, "simulation_root", self.runtime_root / "simulations")
        if self.database_url is None:
            object.__setattr__(
                self,
                "database_url",
                f"sqlite:///{(self.runtime_root / 'avalanche.db').as_posix()}",
            )

    @property
    def config_root(self) -> Path:
        return self.backend_root / "config"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @classmethod
    def from_environment(cls) -> "Settings":
        project = _project_root()
        _load_env_file(project / ".env")

        # AVALANCHE_* is the documented interface. MOUNT_HOSMER_* is the
        # pre-1.0 interface and is still honoured so existing local .env files
        # and the compiled launcher keep working.
        data_root_value = _env(
            "AVALANCHE_DATA_ROOT",
            "MOUNT_HOSMER_DATA_ROOT",
            default=str(project.parent / "DATA" / "mount_hosmer_data"),
        )
        runtime_root_value = _env(
            "AVALANCHE_RUNTIME_ROOT",
            "MOUNT_HOSMER_RUNTIME_ROOT",
            default=str(project / "runtime"),
        )
        assert data_root_value and runtime_root_value

        data_root = _resolve(project, data_root_value)
        runtime_root = _resolve(project, runtime_root_value)
        derived_root = _resolve(
            project, _env("AVALANCHE_DERIVED_DATA_ROOT", default=str(runtime_root / "derived")) or ""
        )
        simulation_root = _resolve(
            project,
            _env("AVALANCHE_SIMULATION_ROOT", default=str(runtime_root / "simulations")) or "",
        )

        database_url = _env(
            "DATABASE_URL",
            default=f"sqlite:///{(runtime_root / 'avalanche.db').as_posix()}",
        )
        assert database_url

        origins = _env("AVALANCHE_CORS_ORIGINS")
        cors_origins = (
            tuple(origin.strip() for origin in origins.split(",") if origin.strip())
            if origins
            else ("http://localhost:3000", "http://127.0.0.1:3000")
        )

        return cls(
            project_root=project,
            backend_root=project / "backend",
            runtime_root=runtime_root,
            data_root=data_root,
            derived_root=derived_root,
            simulation_root=simulation_root,
            database_url=database_url,
            redis_url=_env("REDIS_URL"),
            app_env=_env("APP_ENV", default="development") or "development",
            terrain_resolution_m=_float_env("AVALANCHE_TERRAIN_RESOLUTION_M", 5.0),
            environmental_resolution_m=_float_env("AVALANCHE_ENVIRONMENTAL_RESOLUTION_M", 10.0),
            fallback_resolution_m=_float_env("AVALANCHE_FALLBACK_RESOLUTION_M", 30.0),
            jobs_enabled=_bool_env("AVALANCHE_JOBS_ENABLED", True),
            scheduler_enabled=_bool_env("AVALANCHE_SCHEDULER_ENABLED", False),
            max_job_workers=int(os.environ.get("AVALANCHE_MAX_JOB_WORKERS", "2") or "2"),
            cors_origins=cors_origins,
        )

    def validate(self, *, require_data_root: bool = True) -> list[str]:
        """Check the resolved layout. Returns non-fatal warnings.

        Raises :class:`ConfigurationError` with an actionable message when the
        layout cannot work at all, so a misconfigured deployment fails loudly at
        startup rather than silently producing empty results.
        """
        warnings: list[str] = []

        if require_data_root and not self.data_root.exists():
            raise ConfigurationError(
                f"Source data root does not exist: {self.data_root}\n"
                f"  Set AVALANCHE_DATA_ROOT to the folder that contains "
                f"'static/', 'dynamic/', 'events/' and 'metadata/'.\n"
                f"  In Docker, mount it read-only, e.g. "
                f"-v /host/mount_hosmer_data:/data/mount_hosmer_data:ro "
                f"-e AVALANCHE_DATA_ROOT=/data/mount_hosmer_data"
            )

        if self.data_root.exists():
            for required in ("metadata", "static"):
                if not (self.data_root / required).is_dir():
                    warnings.append(
                        f"Source data root is missing the '{required}/' folder: {self.data_root}"
                    )

        for label, path in (
            ("runtime", self.runtime_root),
            ("derived", self.derived_root),
            ("simulation", self.simulation_root),
        ):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigurationError(
                    f"Cannot create the {label} directory {path}: {exc}\n"
                    f"  In Docker this usually means the volume is read-only or unmounted."
                ) from exc

        if self.runtime_root == self.data_root or self.runtime_root.is_relative_to(self.data_root):
            raise ConfigurationError(
                f"The runtime root ({self.runtime_root}) must not live inside the source data "
                f"root ({self.data_root}). Source data is strictly read-only."
            )

        if self.terrain_resolution_m > self.environmental_resolution_m:
            warnings.append(
                f"Terrain grid ({self.terrain_resolution_m} m) is coarser than the environmental "
                f"grid ({self.environmental_resolution_m} m); terrain detail is being discarded."
            )

        if self.is_production and self.database_url.startswith("sqlite"):
            warnings.append(
                "APP_ENV=production with a SQLite DATABASE_URL. SQLite is supported but "
                "single-writer; set DATABASE_URL to PostgreSQL for concurrent deployments."
            )

        return warnings


_cached: Settings | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    global _cached
    if refresh or _cached is None:
        _cached = Settings.from_environment()
    return _cached
