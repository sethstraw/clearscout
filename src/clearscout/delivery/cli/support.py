"""Shared command-line application state and resource lifecycles."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer

from clearscout.delivery.cli.console import Terminal
from clearscout.shared.errors import ClearscoutError

if TYPE_CHECKING:
    from clearscout.infrastructure.persistence.database import Database

# This exit code means output was produced but failed validation.
CHECKS_FAILED = 4

app = typer.Typer(
    name="clearscout",
    help="Build a master CV, screen jobs, and create evidence-grounded resume Markdown.",
    add_completion=False,
    no_args_is_help=True,
)
terminal = Terminal()


def _database_busy(error: Exception) -> bool:
    """Return whether SQLAlchemy raised SQLite's busy error: another process holds the lock."""
    # Imported here because naming a command must not load SQLAlchemy.
    from sqlalchemy.exc import OperationalError

    if not isinstance(error, OperationalError):
        return False
    code = getattr(error.orig, "sqlite_errorcode", None)
    # Masking to the primary code folds SQLite's extended busy codes into SQLITE_BUSY.
    return code is not None and code & 0xFF == sqlite3.SQLITE_BUSY


@contextmanager
def _reporting_errors() -> Iterator[None]:
    """Turn a clearscout error or a busy database into a message and an exit code, not a traceback.

    A context manager, so only the work is guarded and printing a successful
    result is not.
    """
    try:
        yield
    except ClearscoutError as error:
        terminal.error(str(error))
        raise typer.Exit(error.exit_code) from error
    except Exception as error:
        if not _database_busy(error):
            raise
        terminal.error(
            "The ClearScout database file is busy with another process. "
            "Let that process finish or stop it, then try again."
        )
        raise typer.Exit(1) from error


@contextmanager
def open_database(database_url: str) -> Iterator[Database]:
    """Open one database for a command and always release its engine."""
    from clearscout.infrastructure.persistence.database import Database

    database = Database(database_url)
    try:
        yield database
    finally:
        database.dispose()
