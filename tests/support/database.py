"""One migrated SQLite template per test session, cloned for every test.

Copying a file already at head is far cheaper than migrating for each test.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from clearscout.infrastructure.persistence.migration import upgrade


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


__all__ = ["build_template", "clone"]
