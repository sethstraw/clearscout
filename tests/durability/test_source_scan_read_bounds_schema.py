"""The migrated source_scans table carries the read bounds its mapper declares."""

from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import Table, inspect, text
from sqlalchemy.exc import IntegrityError

from tailorcv.domain import utc_now
from tailorcv.domain.records import SourceReadOutcome
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import SourceScan
from tests.support.scenarios import employer_board, scan_workspace


def test_source_scan_read_bounds_match_between_mapper_and_migration(
    durable_database: Database,
) -> None:
    table = cast(Table, SourceScan.__table__)
    migrated = {
        column["name"]: column
        for column in inspect(durable_database.engine).get_columns("source_scans")
    }

    assert tuple(migrated) == tuple(table.columns.keys())
    interrupted = migrated["detail_interrupted_count"]
    assert (interrupted["nullable"], str(interrupted["type"])) == (True, "INTEGER")
    assert interrupted["default"] is None
    truncated, exhausted = migrated["discovery_truncated"], migrated["exhausted_outcome"]
    assert (truncated["nullable"], str(truncated["type"])) == (False, "BOOLEAN")
    assert truncated["default"] == "0"
    assert (exhausted["nullable"], str(exhausted["type"])) == (True, "VARCHAR(40)")
    assert "ck_source_scan_exhausted_outcome" in {
        constraint["name"]
        for constraint in inspect(durable_database.engine).get_check_constraints("source_scans")
    }


@pytest.mark.parametrize(
    ("attempted", "interrupted", "valid"),
    [(3, None, True), (4, None, True), (3, 0, True), (4, 1, True), (3, 1, False), (2, -1, False)],
)
def test_source_scan_detail_counts_retain_interruptions_without_inventing_history(
    durable_database: Database, attempted: int, interrupted: int | None, valid: bool
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = employer_board(durable_database, workspace_id, "Initech")
    now = utc_now()
    scan = SourceScan(
        source_id=source_id,
        scanned_on=now.date(),
        query_label="title-filter",
        status="incomplete",
        detail_attempted_count=attempted,
        detail_read_count=2,
        detail_failed_count=1,
        detail_interrupted_count=interrupted,
        scan_started_at=now,
        scan_completed_at=now,
        duration_ms=0,
    )
    if not valid:
        with durable_database.session() as session:
            session.add(scan)
            with pytest.raises(IntegrityError, match="ck_source_scan_detail"):
                session.flush()
            session.rollback()
        return
    with durable_database.session() as session:
        session.add(scan)
        session.flush()
        scan_id = scan.id
    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        assert stored.detail_interrupted_count == interrupted
        assert stored.detail_attempted_count == attempted


def test_a_scan_stored_without_a_read_outcome_loads_as_not_recorded(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = employer_board(durable_database, workspace_id, "Initech")
    now = utc_now()
    with durable_database.session() as session:
        scan = SourceScan(
            source_id=source_id,
            scanned_on=now.date(),
            query_label="title-filter",
            status="completed_empty",
            scan_started_at=now,
            scan_completed_at=now,
            duration_ms=0,
        )
        session.add(scan)
        session.flush()
        scan_id = scan.id

    with durable_database.session() as session:
        stored = session.get(SourceScan, scan_id)
        assert stored is not None
        assert SourceReadOutcome(stored.read_outcome) is SourceReadOutcome.NOT_RECORDED


def test_source_scans_refuse_an_exhausted_outcome_outside_the_read_vocabulary(
    durable_database: Database,
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_source_scan_exhausted_outcome"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO source_scans (id, source_id, scanned_on, query_label, status, "
                "read_outcome, raw_count, read_count, skip_reasons, result_count, new_count, "
                "detail_attempted_count, detail_read_count, detail_failed_count, "
                "detail_capped_count, notes, exhausted_outcome, scan_started_at, "
                "scan_completed_at, duration_ms, created_at, created_by, origin) VALUES "
                "('s', 'missing', '2026-01-01', 'q', 'incomplete', 'crawl_continuation', "
                "0, 0, '{}', 0, 0, 0, 0, 0, 0, '', 'finished', '2026-01-01T00:00:00Z', "
                "'2026-01-01T00:00:00Z', 0, '2026-01-01T00:00:00Z', 'test', 'test')"
            )
        )
