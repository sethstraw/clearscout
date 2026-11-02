"""Consistent, owner-private SQLite backups."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from tailorcv.application.failures import ValidationError
from tailorcv.infrastructure.artifacts.files import private_directory, sync_directory
from tailorcv.infrastructure.persistence.database import Database, sqlite_path, sqlite_uri

NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupReport:
    """Verified backup result."""

    source: Path
    destination: Path
    byte_size: int


def backup_database(database: Database, destination: Path) -> BackupReport:
    """Create a consistent SQLite snapshot without copying a live WAL file."""
    source_path = sqlite_path(database.url)
    if source_path is None:
        raise ValidationError("Database backup currently supports only a local SQLite database.")
    if not source_path.is_file():
        raise ValidationError(f"No SQLite database exists at {source_path}.")
    target = destination.expanduser().resolve()
    if target.exists():
        raise ValidationError(f"Backup destination already exists: {target}")
    private_directory(target.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            closing(sqlite3.connect(sqlite_uri(source_path, "ro"), uri=True)) as source,
            closing(sqlite3.connect(temporary)) as backup,
        ):
            source.backup(backup)
            integrity = str(backup.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = backup.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or foreign_keys:
                raise ValidationError(
                    f"Backup verification failed: integrity={integrity!r}, "
                    f"foreign_keys={foreign_keys!r}"
                )
        temporary.chmod(0o600)
        _publish(temporary, target)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise ValidationError(f"Backup destination already exists: {target}") from None
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        temporary.unlink()
    except OSError as error:
        # The snapshot has landed, so a stray temporary file is logged rather than failing it.
        logger.warning(
            "database_backup_temporary_left",
            extra={"path": str(temporary), "error": str(error)},
        )
    sync_directory(target.parent)
    return BackupReport(source_path, target, target.stat().st_size)


def _publish(temporary: Path, target: Path) -> None:
    """Give the verified snapshot its final name, refusing a name that already exists."""
    try:
        # A hard link fails on an existing name, so a file that appeared after the check survives.
        os.link(temporary, target)
        return
    except OSError as error:
        if error.errno not in NO_HARD_LINKS:
            raise
    # FAT, exFAT, and some network shares have no hard links; an exclusive create has the same rule.
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as copy, temporary.open("rb") as source:
            shutil.copyfileobj(source, copy)
            copy.flush()
            os.fsync(copy.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
