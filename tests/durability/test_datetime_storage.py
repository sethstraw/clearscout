"""Durable instants use explicit canonical UTC text on SQLite."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import StatementError

from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.migration import upgrade
from tailorcv.infrastructure.persistence.models import Workspace
from tailorcv.infrastructure.persistence.types import UTCDateTime


def test_aware_datetime_is_canonical_utc_text_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "utc.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    offset = timezone(timedelta(hours=5, minutes=30))
    supplied = datetime(2026, 8, 6, 18, 7, 8, 901234, tzinfo=offset)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        upgrade(url)
        database = Database(url)
        try:
            with database.session() as session:
                session.add(
                    Workspace(
                        id="workspace-aware-utc",
                        slug="aware-utc",
                        name="Aware UTC",
                        created_at=supplied,
                    )
                )
            with database.engine.connect() as connection:
                raw = connection.exec_driver_sql(
                    "SELECT created_at FROM workspaces WHERE id = 'workspace-aware-utc'"
                ).scalar_one()
            with database.session() as session:
                restored = session.get(Workspace, "workspace-aware-utc")
                assert restored is not None
                value = restored.created_at
        finally:
            database.dispose()

    assert raw == "2026-08-06T12:37:08.901234Z"
    assert value == supplied.astimezone(UTC)
    assert value.tzinfo is UTC
    assert not [item for item in seen if issubclass(item.category, DeprecationWarning)]


def test_naive_datetime_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "naive.sqlite3"
    url = f"sqlite+pysqlite:///{path}"
    upgrade(url)
    database = Database(url)
    try:
        with (
            pytest.raises(StatementError, match="timezone-aware"),
            database.session() as session,
        ):
            session.add(
                Workspace(
                    id="workspace-naive",
                    slug="naive",
                    name="Naive",
                    created_at=datetime(2026, 8, 6),
                )
            )
    finally:
        database.dispose()


def test_datetime_type_reads_only_canonical_text_and_nulls() -> None:
    data_type = UTCDateTime()
    dialect = sqlite.dialect()

    assert data_type.process_bind_param(None, dialect) is None
    assert data_type.process_result_value(None, dialect) is None
    assert data_type.process_result_value("2026-08-06T12:30:00.000000Z", dialect) == datetime(
        2026, 8, 6, 12, 30, tzinfo=UTC
    )
    for invalid in (
        "2026-08-06 12:30:00.000000",
        "2026-08-06T12:30:00Z",
        "2026-08-06T15:30:00.000000+03:00",
    ):
        with pytest.raises(ValueError, match="canonical UTC"):
            UTCDateTime.parse(invalid)
