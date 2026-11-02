"""Database configuration with private SQLite defaults and explicit sessions."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import quote

from platformdirs import user_documents_path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from tailorcv.domain.identity import normalize_text
from tailorcv.infrastructure.artifacts.files import private_directory
from tailorcv.shared.errors import ConfigError

DATABASE_ENVIRONMENT_VARIABLE = "TAILORCV_DATABASE_URL"
AFTER_COMMIT_CALLBACKS = "tailorcv.after_commit"
AFTER_ROLLBACK_CALLBACKS = "tailorcv.after_rollback"
SQLITE_TIMEOUT_SECONDS = 30


def register_session_finalizer(
    session: Session,
    *,
    after_commit: Any,
    after_rollback: Any,
) -> None:
    """Register filesystem work on the correct side of a database outcome."""
    session.info.setdefault(AFTER_COMMIT_CALLBACKS, []).append(after_commit)
    session.info.setdefault(AFTER_ROLLBACK_CALLBACKS, []).append(after_rollback)


def default_data_directory() -> Path:
    """Return the Documents directory for durable data, where backup tools can see it."""
    return Path(user_documents_path()) / "TailorCV"


def default_database_path() -> Path:
    """Return the zero-configuration SQLite database path."""
    return default_data_directory() / "tailorcv.sqlite3"


def default_database_url() -> str:
    """Resolve the configured URL without creating or migrating anything."""
    configured = os.environ.get(DATABASE_ENVIRONMENT_VARIABLE, "").strip()
    if configured:
        return configured
    return f"sqlite+pysqlite:///{default_database_path()}"


def sqlite_path(database_url: str) -> Path | None:
    """Return the local path for a SQLite URL, or ``None`` for another backend."""
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite") or not parsed.database:
        return None
    if parsed.database == ":memory:":
        return None
    return Path(parsed.database).expanduser().resolve()


def sqlite_uri(path: PurePath, mode: str) -> str:
    """Return a SQLite URI that opens a local file in ``mode``, such as ``ro`` or ``rw``."""
    location = quote(path.as_posix(), safe="/:")
    # An empty authority keeps a UNC path's leading slashes, where SQLite refuses a host.
    return f"file://{'' if location.startswith('/') else '/'}{location}?mode={mode}"


def prepare_database_location(database_url: str) -> Path | None:
    """Create only the private parent directory required by an explicit migration."""
    path = sqlite_path(database_url)
    if path is not None:
        private_directory(path.parent)
    return path


def protect_database_file(database_url: str) -> None:
    """Tighten a newly created SQLite database to owner-only permissions."""
    path = sqlite_path(database_url)
    if path is not None and path.is_file():
        path.chmod(0o600)


class Database:
    """SQLAlchemy engine and transaction boundary shared by CLI and API."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        require_existing: bool = True,
    ) -> None:
        """Open an engine without creating tables or running migrations.

        With ``require_existing`` false the engine may start before the SQLite
        file exists, but a connection never creates it.
        """
        self.url = database_url or default_database_url()
        path = sqlite_path(self.url)
        if require_existing and path is not None and not path.is_file():
            raise ConfigError(
                f"TailorCV's database does not exist at {path}.\n"
                "  Run 'tailorcv db upgrade' explicitly before starting the application."
            )
        options: dict[str, Any] = {"pool_pre_ping": True}
        if self.url.startswith("sqlite"):
            options["connect_args"] = {"timeout": SQLITE_TIMEOUT_SECONDS}
        if not require_existing and path is not None:
            # SQLite's rw mode refuses a missing file, where a plain path would create one.
            uri = sqlite_uri(path, "rw")
            options["creator"] = lambda: sqlite3.connect(
                uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS, check_same_thread=False
            )
        self.engine = create_engine(self.url, **options)
        if self.url.startswith("sqlite"):
            event.listen(self.engine, "connect", _configure_sqlite)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit one application use case atomically, rolling back on failure."""
        with self._sessions() as session:
            committed = False
            try:
                yield session
                session.commit()
                committed = True
            # Roll back staged files on cancellation as well as ordinary errors.
            except BaseException:
                if not committed:
                    session.rollback()
                    for callback in reversed(session.info.get(AFTER_ROLLBACK_CALLBACKS, [])):
                        callback()
                raise
            for callback in session.info.get(AFTER_COMMIT_CALLBACKS, []):
                callback()

    @contextmanager
    def exclusive_session(self) -> Iterator[Session]:
        """Commit one use case whose write depends on what it just read.

        SQLite takes the write lock at BEGIN IMMEDIATE rather than at the first
        write, so no second writer can commit between the read and the write
        this session bases on it. Waiting for that lock is bounded by the
        connection's busy timeout.
        """
        with self.engine.connect() as connection:
            if self.url.startswith("sqlite"):
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            with self._sessions(bind=connection) as session:
                try:
                    yield session
                    session.commit()
                except BaseException:
                    session.rollback()
                    connection.rollback()
                    for callback in reversed(session.info.get(AFTER_ROLLBACK_CALLBACKS, [])):
                        callback()
                    raise
                finalizers = session.info.get(AFTER_COMMIT_CALLBACKS, [])
            connection.commit()
            for callback in finalizers:
                callback()

    def check(self) -> None:
        """Prove the configured database accepts a trivial query."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")

    def dispose(self) -> None:
        """Release pooled database connections."""
        self.engine.dispose()


def _configure_sqlite(dbapi_connection: Any, connection_record: Any) -> None:
    """Enable integrity, concurrency, and bounded waiting on every SQLite connection."""
    del connection_record
    dbapi_connection.create_function("tailorcv_normalize", 1, normalize_text, deterministic=True)
    # SQLite's own lower() folds only A-Z; icontains and ilike compile to lower().
    dbapi_connection.create_function("lower", 1, _unicode_lower, deterministic=True)
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def _unicode_lower(value: object) -> object:
    return value.lower() if isinstance(value, str) else value
