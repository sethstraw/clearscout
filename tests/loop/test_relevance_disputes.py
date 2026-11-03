"""A workspace dispute sets a relevance verdict aside without rewriting the review."""

from pathlib import Path

import pytest
from sqlalchemy import delete, select

from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.relevance_review_queries import RelevanceReviewQueries
from clearscout.application.failures import ConflictError, NotFoundError
from clearscout.application.job_search.models import RelevanceReviewResponse
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import AuditEvent, RelevanceReview
from tests.support.scenarios import workspace_with_relevance_review


def _current(
    database: Database, workspace_id: str, pending_id: str
) -> RelevanceReviewResponse | None:
    with database.session() as session:
        return RelevanceReviewQueries(database).current_for_pending_ids(
            session, workspace_id, [pending_id]
        )[pending_id]


def test_a_dispute_is_stored_beside_the_review_and_reaches_the_queue(
    durable_database: Database, tmp_path: Path
) -> None:
    """Both values stay readable: the model's verdict, and that a person disputed it."""
    workspace_id, pending_id, review_id = workspace_with_relevance_review(
        durable_database, tmp_path, slug="dispute-stored"
    )

    RelevanceDisputeService(durable_database).dispute(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        relevance_review_id=review_id,
        note="The posting names the ledger work my CV records.",
        audit=audit_columns("local_user", "test", None),
    )

    current = _current(durable_database, workspace_id, pending_id)
    assert current is not None
    assert current.recommendation == "unlikely"
    assert current.disputed_at is not None
    assert current.dispute_note == "The posting names the ledger work my CV records."
    with durable_database.session() as session:
        stored = session.scalars(select(RelevanceReview)).all()
    assert [(row.id, row.recommendation) for row in stored] == [(review_id, "unlikely")]


def test_withdrawing_a_dispute_returns_the_model_verdict_to_the_queue(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, review_id = workspace_with_relevance_review(
        durable_database, tmp_path, slug="dispute-cleared"
    )
    service = RelevanceDisputeService(durable_database)
    service.dispute(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        relevance_review_id=review_id,
        note=None,
        audit=audit_columns("local_user", "test", None),
    )

    cleared = service.clear(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        audit=audit_columns("local_user", "test", None),
    )

    assert cleared is True
    current = _current(durable_database, workspace_id, pending_id)
    assert current is not None and current.disputed_at is None


def test_a_dispute_of_a_verdict_no_longer_in_force_is_refused(
    durable_database: Database, tmp_path: Path
) -> None:
    """A dispute lands on the verdict the person read, never on one they did not."""
    workspace_id, pending_id, _review_id = workspace_with_relevance_review(
        durable_database, tmp_path, slug="dispute-stale"
    )

    with pytest.raises(ConflictError):
        RelevanceDisputeService(durable_database).dispute(
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            relevance_review_id="review-that-is-not-in-force",
            note=None,
            audit=audit_columns("local_user", "test", None),
        )

    current = _current(durable_database, workspace_id, pending_id)
    assert current is not None and current.disputed_at is None


def test_disputing_a_discovery_with_no_review_is_refused(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, pending_id, review_id = workspace_with_relevance_review(
        durable_database, tmp_path, slug="dispute-unreviewed"
    )
    with durable_database.session() as session:
        session.execute(delete(RelevanceReview).where(RelevanceReview.id == review_id))

    with pytest.raises(NotFoundError):
        RelevanceDisputeService(durable_database).dispute(
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            relevance_review_id=review_id,
            note=None,
            audit=audit_columns("local_user", "test", None),
        )


def test_every_dispute_change_writes_an_audit_event(
    durable_database: Database, tmp_path: Path
) -> None:
    """A dispute that lands with no audit trail cannot be recovered later."""
    workspace_id, pending_id, review_id = workspace_with_relevance_review(
        durable_database, tmp_path, slug="dispute-audit"
    )
    service = RelevanceDisputeService(durable_database)

    service.dispute(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        relevance_review_id=review_id,
        note="one",
        audit=audit_columns("local_user", "test", None),
    )
    service.clear(
        workspace_id=workspace_id,
        pending_discovery_id=pending_id,
        audit=audit_columns("local_user", "test", None),
    )

    with durable_database.session() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.entity_type == "relevance_review_dispute",
            )
            .order_by(AuditEvent.created_at)
        ).all()
    assert [event.action for event in events] == [
        "relevance_dispute.set",
        "relevance_dispute.cleared",
    ]
