"""SQLite backups are consistent, private, and never overwrite a snapshot."""

from __future__ import annotations

import errno
import importlib
import importlib.util
import os
import sqlite3
from pathlib import Path

import pytest

from clearscout.application import job_search
from clearscout.application.failures import ValidationError
from clearscout.application.portability import backup_database
from clearscout.application.workspaces import WorkspaceService
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migration import upgrade
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import evaluation_commit


def test_portability_package_owns_the_database_transfer_surface() -> None:
    portability = importlib.import_module("clearscout.application.portability")

    assert tuple(portability.__all__) == (
        "BackupReport",
        "FileRelocation",
        "ImportReport",
        "backup_database",
        "import_database",
    )
    assert importlib.util.find_spec("clearscout.application.database_backup") is None
    assert importlib.util.find_spec("clearscout.application.database_import") is None


def test_backup_is_a_verified_restorable_snapshot(tmp_path: Path) -> None:
    source_path = tmp_path / "source.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    database = Database(source_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="backup", name="Backup"
        )
        job_search.commit_evaluation(
            database,
            artifact_service(database, tmp_path / "artifacts"),
            evaluation_commit(workspace.id),
        )
        destination = tmp_path / "snapshots" / "clearscout.sqlite3"
        report = backup_database(database, destination)
    finally:
        database.dispose()

    assert report.destination == destination
    assert report.byte_size == destination.stat().st_size
    assert destination.stat().st_mode & 0o777 == 0o600
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert restored.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 1
        assert restored.execute("SELECT count(*) FROM job_evaluations").fetchone()[0] == 1
    finally:
        restored.close()


def test_backup_refuses_to_replace_an_existing_snapshot(tmp_path: Path) -> None:
    source_path = tmp_path / "source.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    database = Database(source_url)
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep me")
    try:
        with pytest.raises(ValidationError, match="already exists"):
            backup_database(database, destination)
    finally:
        database.dispose()
    assert destination.read_bytes() == b"keep me"


def test_backup_refuses_a_destination_created_after_the_existence_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "source.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    database = Database(source_url)
    destination = tmp_path / "raced.sqlite3"
    original_chmod = Path.chmod

    def chmod_then_race(path: Path, mode: int, **kwargs: object) -> None:
        original_chmod(path, mode)
        if path.name.startswith(".raced.sqlite3."):
            destination.write_bytes(b"keep me")

    monkeypatch.setattr(Path, "chmod", chmod_then_race)
    try:
        with pytest.raises(ValidationError, match="already exists"):
            backup_database(database, destination)
    finally:
        database.dispose()
    assert destination.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".raced.sqlite3.*"))


def _refuse_hard_links(source: object, target: object) -> None:
    raise PermissionError(errno.EPERM, "Operation not permitted")


def test_backup_lands_on_a_filesystem_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = f"sqlite+pysqlite:///{tmp_path / 'source.sqlite3'}"
    upgrade(source_url)
    database = Database(source_url)
    destination = tmp_path / "usb" / "clearscout.sqlite3"
    monkeypatch.setattr(os, "link", _refuse_hard_links)
    try:
        report = backup_database(database, destination)
    finally:
        database.dispose()

    assert report.byte_size == destination.stat().st_size
    assert destination.stat().st_mode & 0o777 == 0o600
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()
    assert not list(destination.parent.glob(".clearscout.sqlite3.*"))


def test_backup_that_landed_succeeds_when_its_temporary_file_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = f"sqlite+pysqlite:///{tmp_path / 'source.sqlite3'}"
    upgrade(source_url)
    database = Database(source_url)
    destination = tmp_path / "snapshots" / "clearscout.sqlite3"
    original_unlink = Path.unlink

    def refuse_temporary_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(".clearscout.sqlite3."):
            raise PermissionError(errno.EACCES, "Permission denied")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_temporary_cleanup)
    try:
        report = backup_database(database, destination)
    finally:
        database.dispose()

    assert report.destination == destination
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        restored.close()


def test_backup_without_hard_links_still_refuses_a_raced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = f"sqlite+pysqlite:///{tmp_path / 'source.sqlite3'}"
    upgrade(source_url)
    database = Database(source_url)
    destination = tmp_path / "raced.sqlite3"

    def link_after_race(source: object, target: object) -> None:
        destination.write_bytes(b"keep me")
        _refuse_hard_links(source, target)

    monkeypatch.setattr(os, "link", link_after_race)
    try:
        with pytest.raises(ValidationError, match="already exists"):
            backup_database(database, destination)
    finally:
        database.dispose()
    assert destination.read_bytes() == b"keep me"
    assert not list(tmp_path.glob(".raced.sqlite3.*"))


def test_backup_requires_an_existing_local_sqlite_file(tmp_path: Path) -> None:
    memory = Database("sqlite+pysqlite:///:memory:", require_existing=False)
    missing = Database(f"sqlite+pysqlite:///{tmp_path / 'missing.sqlite3'}", require_existing=False)
    try:
        with pytest.raises(ValidationError, match="local SQLite"):
            backup_database(memory, tmp_path / "memory-backup.sqlite3")
        with pytest.raises(ValidationError, match="No SQLite database exists"):
            backup_database(missing, tmp_path / "missing-backup.sqlite3")
    finally:
        memory.dispose()
        missing.dispose()
