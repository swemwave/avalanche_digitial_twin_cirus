"""Engine, session, and schema migration.

Migrations are applied by a small in-house runner rather than Alembic. The reason
is the delivery mode: the primary way this application is used is a double-clicked
`.exe` on a Windows laptop with no database server and no shell. That user cannot
run `alembic upgrade head`. Schema setup therefore has to happen automatically at
startup, be idempotent, and work on a fresh SQLite file with no configuration.

The migration list below is ordered and versioned, applied inside a transaction,
and recorded in a `schema_migrations` table, so it behaves like a migration tool
where it matters. Alembic remains a straightforward swap for a server deployment
that wants it; the models are plain SQLAlchemy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.storage.models import Base

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class Migration:
    version: int
    name: str
    statements: list[str]


#: Applied in order. Version 1 is created by ``Base.metadata.create_all``; later
#: versions add incremental DDL here. Never edit an applied migration -- add a new one.
MIGRATIONS: list[Migration] = []


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @property
    def is_sqlite(self) -> bool:
        return self.engine.dialect.name == "sqlite"

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def migrate(self) -> list[int]:
        """Bring the schema up to date. Safe to run on every startup."""
        applied: list[int] = []
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "version INTEGER PRIMARY KEY, "
                    "name VARCHAR(128) NOT NULL, "
                    "applied_utc VARCHAR(40) NOT NULL)"
                )
            )
            rows = connection.execute(text("SELECT version FROM schema_migrations")).fetchall()
            existing = {int(row[0]) for row in rows}

        # Baseline. create_all is idempotent and is what stands up a fresh file.
        self.create_all()

        with self.engine.begin() as connection:
            if 1 not in existing:
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations (version, name, applied_utc) "
                        "VALUES (1, 'baseline', datetime('now'))"
                        if self.is_sqlite
                        else "INSERT INTO schema_migrations (version, name, applied_utc) "
                        "VALUES (1, 'baseline', CAST(NOW() AS VARCHAR))"
                    )
                )
                applied.append(1)

            for migration in sorted(MIGRATIONS, key=lambda item: item.version):
                if migration.version in existing or migration.version <= 1:
                    continue
                logger.info("Applying migration %s: %s", migration.version, migration.name)
                for statement in migration.statements:
                    connection.execute(text(statement))
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations (version, name, applied_utc) "
                        "VALUES (:version, :name, :applied)"
                    ),
                    {
                        "version": migration.version,
                        "name": migration.name,
                        "applied": "now",
                    },
                )
                applied.append(migration.version)

        return applied

    def session(self) -> Session:
        return self.session_factory()

    def dispose(self) -> None:
        self.engine.dispose()


def _build_engine(settings: Settings) -> Engine:
    url = str(settings.database_url)

    if url.startswith("sqlite"):
        # SQLite path: make sure the parent directory exists before the driver
        # tries to open the file, or it fails with a bare "unable to open".
        prefix = "sqlite:///"
        if url.startswith(prefix):
            db_path = Path(url[len(prefix):])
            if str(db_path) not in {":memory:", ""}:
                db_path.parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            url,
            future=True,
            # The job runner writes from worker threads while requests read from
            # the event loop, and SQLite objects are otherwise thread-bound.
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _configure_sqlite(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            # WAL lets readers proceed while a job writes. Without it, a long
            # simulation write blocks every API read for its duration.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    return create_engine(url, future=True, pool_pre_ping=True, pool_size=5, max_overflow=10)


_database: Database | None = None


def get_database(settings: Settings, *, refresh: bool = False) -> Database:
    global _database
    if _database is None or refresh:
        if _database is not None:
            _database.dispose()
        _database = Database(_build_engine(settings))
        _database.migrate()
    return _database


@contextmanager
def session_scope(settings: Settings) -> Iterator[Session]:
    """A transactional session. Commits on success, rolls back on any exception."""
    database = get_database(settings)
    session = database.session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
