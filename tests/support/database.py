"""One migrated SQLite template per test session, cloned for every test.

Copying a file already at head is far cheaper than migrating for each test.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from clearscout.infrastructure.persistence.migration import upgrade

_FILLER = {"INTEGER": 0, "BOOLEAN": 0, "JSON": "{}", "DATE": "2026-01-01"}


def build_template(destination: Path) -> Path:
    """Migrate one database to head and leave it in a state that copies cleanly.

    WAL is turned off because a clone is a copy of one file, and WAL keeps part
    of the content in a sidecar.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    upgrade(f"sqlite+pysqlite:///{destination}")
    connection = sqlite3.connect(destination)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        connection.close()
    return destination


def clone(template: Path, destination: Path) -> str:
    """Copy the migrated template into place and return its database URL."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, destination)
    destination.chmod(0o600)
    return f"sqlite+pysqlite:///{destination}"


def insert_row(connection: sqlite3.Connection, table: str, **values: Any) -> None:
    """Insert a raw row, filling required columns the case does not name."""
    for _, name, kind, notnull, default, _ in connection.execute(f"PRAGMA table_info({table})"):
        if notnull and default is None and name not in values:
            filler = "2026-01-01 00:00:00" if "DATETIME" in kind else ""
            values[name] = _FILLER.get(kind.split("(")[0], filler)
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    statement = f"INSERT INTO {table} ({columns}) VALUES ({marks})"  # noqa: S608
    connection.execute(statement, list(values.values()))


__all__ = ["build_template", "clone", "insert_row"]
