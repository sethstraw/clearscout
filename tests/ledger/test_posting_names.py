"""The single audited write that renames a tracked posting."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from tailorcv.application.job_search.posting_names import rename_posting
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.domain.identity import normalize_text
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    Employer,
    JobPosting,
    Workspace,
)

_AUDIT = audit_columns("operations", "operations.title_backfill", None)


def _job(database: Database, title: str) -> str:
    """Seed one workspace, employer, and tracked posting. Returns the job id."""
    with database.session() as session:
        workspace = Workspace(slug="posting-names", name="Posting names")
        session.add(workspace)
        session.flush()
        employer = Employer(
            workspace_id=workspace.id,
            name="Globex",
            normalized_name=normalize_text("Globex"),
        )
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=workspace.id,
            employer_id=employer.id,
            title=title,
            normalized_title=normalize_text(title),
            first_seen_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
        session.add(job)
        session.flush()
        return job.id


def _events(database: Database) -> list[AuditEvent]:
    with database.session() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == "posting.title_corrected")
            ).all()
        )


def test_a_rename_records_the_name_the_ledger_carried_before(
    durable_database: Database,
) -> None:
    job_id = _job(durable_database, "Product Manager")

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert rename_posting(
            session, job, "Senior Product Manager", audit=_AUDIT, reason="the publisher says so"
        )

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert job.title == "Senior Product Manager"
        assert job.normalized_title == normalize_text("Senior Product Manager")
    (event,) = _events(durable_database)
    assert event.entity_id == job_id
    assert event.details == {
        "old": "Product Manager",
        "new": "Senior Product Manager",
        "reason": "the publisher says so",
    }


def test_a_blank_or_unchanged_title_leaves_the_posting_and_the_history_alone(
    durable_database: Database,
) -> None:
    job_id = _job(durable_database, "Product Manager")

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert not rename_posting(session, job, "   ", audit=_AUDIT, reason="blank")
        assert not rename_posting(session, job, "Product Manager", audit=_AUDIT, reason="same")

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None and job.title == "Product Manager"
    assert _events(durable_database) == []


def test_a_title_wider_than_the_column_is_truncated_on_both_columns(
    durable_database: Database,
) -> None:
    job_id = _job(durable_database, "Product Manager")

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert rename_posting(
            session, job, "Lead " + "e" * 600, audit=_AUDIT, reason="an over-long page title"
        )

    with durable_database.session() as session:
        job = session.get(JobPosting, job_id)
        assert job is not None
        assert len(job.title) == 500
        assert len(job.normalized_title) == 500
