"""Resetting the pending queue drops machine decisions and keeps human and guarded rows."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.pending import set_pending_markers
from tailorcv.application.discovery.scan_staging import FOUND_POSTING_REASON
from tailorcv.application.failures import ConflictError, NotFoundError
from tailorcv.application.job_search.models import PendingMarkersRequest
from tailorcv.application.tasks import TaskQueueService
from tailorcv.domain import new_id, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source as _source,
)
from tests.support.scenarios import (
    scan_workspace as _workspace,
)
from tests.support.scenarios import (
    seed_pending_discovery as _seed_pending,
)
from tests.support.scenarios import (
    source_listing as _listing,
)
from tests.support.scenarios import (
    state_target_titles as _state_titles,
)


def _seed_pending_disposition(
    database: Database,
    workspace_id: str,
    pending_id: str,
    *,
    origin: str,
    selection_mode: str,
    category: str = "other",
) -> None:
    with database.session() as session:
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="dismiss",
                category=category,
                reason="Synthetic dismissal for reset behavior.",
                selection_mode=selection_mode,
                snapshot_sha256="0" * 64,
                screening={},
                created_by="local_user",
                origin=origin,
            )
        )


def test_resetting_the_pending_queue_forgets_machine_decisions_but_keeps_human_choices(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "Data Platform Lead")
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    machine_dismissed = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    _seed_pending_disposition(
        durable_database,
        workspace_id,
        machine_dismissed,
        origin="board_scan",
        selection_mode="rule",
    )
    source_less = _seed_pending(
        durable_database,
        workspace_id,
        employer="Initech",
        created_at=utc_now(),
        reason=FOUND_POSTING_REASON,
        withdrawn_at=utc_now(),
    )
    human_dismissed = _seed_pending(
        durable_database,
        workspace_id,
        employer="Acme",
        created_at=utc_now(),
        source_id=source_id,
    )
    _seed_pending_disposition(
        durable_database,
        workspace_id,
        human_dismissed,
        origin="api",
        selection_mode="explicit",
        category="current_evidence_unavailable",
    )
    guarded = _seed_pending(
        durable_database,
        workspace_id,
        employer="Initrode",
        created_at=utc_now(),
        source_id=source_id,
    )
    from tailorcv.application.tasks.payloads import DiscoveryEvaluateBatchPayload

    TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.evaluate_batch",
        payload=DiscoveryEvaluateBatchPayload(pending_discovery_ids=[guarded]).model_dump(),
        idempotency_key=f"reset-guard-{guarded}",
    )

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    response = service.reset_pending_discoveries(workspace_id)

    assert response.archived_count == 1
    assert response.kept_count == 2
    assert response.rescreened_count == 1
    with pytest.raises(NotFoundError):
        service.queries.get_pending_discovery(workspace_id, machine_dismissed)
    with durable_database.session() as session:
        source_less_row = session.get(PendingDiscovery, source_less)
        human_row = session.get(PendingDiscovery, human_dismissed)
        guarded_row = session.get(PendingDiscovery, guarded)
        reset_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.entity_id == machine_dismissed,
                AuditEvent.action == "pending_discovery.queue_reset",
            )
        )
        assert source_less_row is not None
        assert source_less_row.archived_at is None
        assert source_less_row.withdrawn_at is None
        assert human_row is not None
        assert human_row.archived_at is None
        assert guarded_row is not None
        assert guarded_row.archived_at is None
        assert reset_event is not None


@pytest.mark.parametrize("marker", ["favorite", "keep"])
def test_a_marker_is_visible_audited_and_preserved_by_reset(
    durable_database: Database, marker: str
) -> None:
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "Data Platform Lead")
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    marked = service.set_pending_markers(
        workspace_id,
        pending_id,
        PendingMarkersRequest(favorite=marker == "favorite", kept=marker == "keep"),
        correlation_id="mark-1",
    )

    assert (marked.favorite, marked.kept) == (marker == "favorite", marker == "keep")
    reset = service.reset_pending_discoveries(workspace_id)
    assert reset.archived_count == 0
    assert reset.kept_count == 1
    assert [item.id for item in service.list_pending_discoveries(workspace_id)] == [pending_id]
    with durable_database.session() as session:
        [event] = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == pending_id,
                AuditEvent.action == "pending_discovery.markers_changed",
            )
        ).all()
        assert (event.origin, event.correlation_id) == ("api", "mark-1")


def test_an_unmarked_row_is_archived_by_reset_once_its_marker_is_cleared(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex"
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.set_pending_markers(
        workspace_id, pending_id, PendingMarkersRequest(favorite=False, kept=True)
    )
    service.set_pending_markers(
        workspace_id, pending_id, PendingMarkersRequest(favorite=False, kept=False)
    )

    assert service.reset_pending_discoveries(workspace_id).archived_count == 1


def test_a_reset_archive_cannot_be_marked_afterward(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    assert service.reset_pending_discoveries(workspace_id).archived_count == 1
    with pytest.raises(NotFoundError, match="active queue"):
        service.set_pending_markers(
            workspace_id, pending_id, PendingMarkersRequest(favorite=False, kept=True)
        )


def test_a_dismissed_row_cannot_be_marked_but_a_closed_one_can(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    withdrawn_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
        withdrawn_at=utc_now(),
    )
    dismissed_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Initech",
        created_at=utc_now(),
        source_id=source_id,
    )
    _seed_pending_disposition(
        durable_database,
        workspace_id,
        dismissed_id,
        origin="api",
        selection_mode="explicit",
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    closed = service.set_pending_markers(
        workspace_id, withdrawn_id, PendingMarkersRequest(favorite=True, kept=False)
    )
    assert closed.favorite is True
    assert closed.withdrawn_at is not None
    with pytest.raises(ConflictError, match="dismissed"):
        service.set_pending_markers(
            workspace_id, dismissed_id, PendingMarkersRequest(favorite=False, kept=True)
        )


def test_a_board_listing_requeues_a_row_archived_by_reset(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    listing = _listing(
        "Data Platform Lead",
        "https://boards.greenhouse.io/globex/jobs/1",
        "Globex",
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    assert service.stage_listings(
        workspace_id,
        [listing],
        utc_now().date(),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    ).inserted == [listing]
    assert service.reset_pending_discoveries(workspace_id).archived_count == 1

    restaged = service.stage_listings(
        workspace_id,
        [listing],
        utc_now().date(),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )

    assert (restaged.inserted, restaged.requeued) == ([], [listing])
    with durable_database.session() as session:
        pending = session.scalar(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        )
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "pending_discovery.requeued",
            )
        )
        assert pending is not None
        assert pending.archived_at is None
        assert event is not None


def test_reset_keeps_a_pending_row_guarded_by_a_running_campaign_retry(
    durable_database: Database,
) -> None:
    """A guided campaign retry stages pending rows, so a reset must not run beneath it."""
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="search.guided_campaign_retry",
        payload={},
        idempotency_key="campaign-retry-reset",
    )
    with durable_database.session() as session:
        stored = session.get(BackgroundTask, task.id)
        assert stored is not None
        stored.status = "running"

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    response = service.reset_pending_discoveries(workspace_id)

    assert response.archived_count == 0
    assert response.kept_count == 1
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.archived_at is None


@pytest.mark.parametrize("status", ["queued", "running", "needs_review", "committing"])
def test_reset_keeps_a_pending_row_guarded_by_paid_recovery(
    durable_database: Database,
    status: str,
) -> None:
    from tailorcv.application.tasks.models import PostingRecoveryApprovalManifest
    from tailorcv.application.tasks.payloads import PostingRecoverBatchPayload

    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database,
        workspace_id,
        "Globex",
        "https://boards.greenhouse.io/globex",
    )
    pending_id = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    issued_at = utc_now()
    payload = PostingRecoverBatchPayload(
        approval=PostingRecoveryApprovalManifest(
            pending_discovery_ids=(pending_id,),
            snapshot_sha256="a" * 64,
            provider="synthetic",
            model="synthetic-model",
            system_prompt_sha256="b" * 64,
            user_prompt_sha256="c" * 64,
            schema_sha256="d" * 64,
            settings_sha256="e" * 64,
            price_sha256="f" * 64,
            estimate_cents=100,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
        ),
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="posting.recover_batch",
        payload=payload.model_dump(mode="json"),
        idempotency_key=f"recover-{status}",
    )
    with durable_database.session() as session:
        stored = session.get(BackgroundTask, task.id)
        assert stored is not None
        stored.status = status

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    response = service.reset_pending_discoveries(workspace_id)

    assert response.archived_count == 0
    assert response.kept_count == 1
    with durable_database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        assert pending.archived_at is None


def test_a_row_the_reset_leaves_live_is_released_from_the_head_it_archives(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex"
    )
    head = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        source_id=source_id,
    )
    copy = _seed_pending(
        durable_database,
        workspace_id,
        employer="Globex",
        created_at=utc_now(),
        reason=FOUND_POSTING_REASON,
        source_id=None,
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, copy)
        assert row is not None
        row.regional_variant_of = head
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    service.reset_pending_discoveries(workspace_id)

    assert [item.id for item in service.list_pending_discoveries(workspace_id)] == [copy]
    with durable_database.session() as session:
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == copy,
                AuditEvent.action == "pending_discovery.regional_variant_released",
            )
        ).one()
        assert released.details["released_from_head"] == head


def test_a_keep_on_a_copy_holds_its_whole_family_through_reset(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    source_id = _source(
        durable_database, workspace_id, "Globex", "https://boards.greenhouse.io/globex"
    )
    head, copy = (
        _seed_pending(
            durable_database,
            workspace_id,
            employer="Globex",
            created_at=utc_now(),
            source_id=source_id,
        )
        for _ in range(2)
    )
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, copy)
        assert row is not None
        row.regional_variant_of = head
        session.flush()
        set_pending_markers(session, workspace_id, copy, favorite=False, kept=True, keep_note=None)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    reset = service.reset_pending_discoveries(workspace_id)

    assert (reset.archived_count, reset.kept_count) == (0, 2)
    [listed] = service.list_pending_discoveries(workspace_id)
    assert (listed.id, listed.kept) == (head, True)
