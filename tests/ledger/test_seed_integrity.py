"""The canonical scenario has to be a database the application would have written."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from clearscout.application.tasks import TaskQueueService
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migration import status
from tests.support.scenarios import populated_workspace

SEEDED_TABLES = (
    "workspaces",
    "employers",
    "job_postings",
    "posting_observations",
    "job_evaluations",
    "evaluation_scores",
    "match_evidence",
    "artifacts",
    "background_tasks",
    "task_events",
    "notes",
    "tags",
    "saved_views",
)


@pytest.fixture
def seeded(durable_database: Database, durable_database_url: str, tmp_path: Path) -> dict[str, str]:
    """The canonical workspace, written through the shipped API by a present worker."""
    from clearscout.bootstrap.api import create_app

    TaskQueueService(durable_database).renew_worker_presence(
        worker_id="seed-worker", ttl_seconds=3600
    )
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        return populated_workspace(client, "canonical", "Northwind Analytics")


def test_the_template_database_is_at_the_migration_head(durable_database_url: str) -> None:
    """A clone of the template is a migrated database, not a copy of an old one."""
    current = status(durable_database_url)
    assert current.current, (
        f"the cloned template is at {current.current_revision}, head is {current.head_revision}"
    )


def test_the_canonical_seed_reaches_every_table_it_claims_to(
    seeded: dict[str, str], durable_database_url: str
) -> None:
    database = Database(durable_database_url)
    try:
        with database.session() as session:
            empty = [
                table
                for table in SEEDED_TABLES
                if session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0  # noqa: S608
            ]
    finally:
        database.dispose()
    assert not empty, f"the canonical seed writes nothing to {empty}"


def test_the_canonical_seed_satisfies_every_foreign_key(
    seeded: dict[str, str], durable_database_url: str
) -> None:
    database = Database(durable_database_url)
    try:
        with database.session() as session:
            violations = session.execute(text("PRAGMA foreign_key_check")).fetchall()
    finally:
        database.dispose()
    assert not violations, f"the canonical seed leaves dangling references: {violations}"


def test_every_seeded_row_belongs_to_the_workspace_that_owns_it(
    seeded: dict[str, str], durable_database_url: str
) -> None:
    """Ownership is what every scoped query depends on being true in the data."""
    owned = ("employers", "job_postings", "artifacts", "background_tasks", "notes", "tags")
    database = Database(durable_database_url)
    try:
        with database.session() as session:
            for table in owned:
                stray = session.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id != :owner"),  # noqa: S608
                    {"owner": seeded["workspace"]},
                ).scalar_one()
                assert stray == 0, f"{table} holds {stray} row(s) owned by nobody in this scenario"
    finally:
        database.dispose()


def test_recorded_history_refuses_to_be_rewritten(
    seeded: dict[str, str], durable_database_url: str
) -> None:
    """Every test that reads a superseded evaluation relies on the old one surviving."""
    database = Database(durable_database_url)
    try:
        with (
            database.session() as session,
            pytest.raises(IntegrityError, match="job_evaluations is immutable"),
        ):
            session.execute(text("UPDATE job_evaluations SET rationale = 'edited'"))
        with (
            database.session() as session,
            pytest.raises(IntegrityError, match="task_events is immutable"),
        ):
            session.execute(text("DELETE FROM task_events"))
    finally:
        database.dispose()
