"""Recurring refusals aggregate into proposals, and each proposal takes one ruling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.proposals import RefusalPatternProposalService
from clearscout.application.discovery.refusal_patterns import (
    RefusedPosting,
    file_refusal,
    refresh_for_workspace,
)
from clearscout.application.failures import ConflictError
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.parameters import SearchParametersService
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.domain import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    SearchProposalState,
    new_id,
    utc_now,
)
from clearscout.domain.search_parameters import (
    Arrangement,
    JobSearchParameters,
    Location,
    Seniority,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    PendingDiscovery,
    PostingRefusal,
    RefusalPatternProposal,
)
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import dismiss_pending_discovery, seed_pending_discovery, titles


def _make_workspace(database: Database) -> str:
    """Return one fresh workspace id."""
    return str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="refusal-check", name="Refusal Check")
        .id
    )


def _record_refusals(
    database: Database,
    workspace_id: str,
    *,
    condition: str,
    reason: str,
    count: int,
    search_parameter_set_id: str | None = None,
) -> None:
    """Insert ``count`` refusals with the same condition and reason."""
    now = utc_now()
    with database.session() as session:
        for offset in range(count):
            session.add(
                PostingRefusal(
                    workspace_id=workspace_id,
                    condition=condition,
                    reason=reason,
                    refused_at=now - timedelta(days=offset),
                    search_parameter_set_id=search_parameter_set_id,
                    origin="test",
                )
            )


def test_aggregation_writes_proposal_once_over_threshold(durable_database: Database) -> None:
    """Three refusals in-window yield exactly one PROPOSED row."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization required",
        count=3,
    )
    with durable_database.session() as session:
        written = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        session.commit()
    assert len(written) == 1
    with durable_database.session() as session:
        rows = session.scalars(
            select(RefusalPatternProposal).where(
                RefusalPatternProposal.workspace_id == workspace_id
            )
        ).all()
        assert len(rows) == 1
        proposal = rows[0]
        assert proposal.state == SearchProposalState.PROPOSED.value
        assert proposal.condition == "eligibility"
        assert proposal.refusal_count == 3


def test_closed_posting_refusals_never_form_a_proposal(durable_database: Database) -> None:
    """A person cannot change a publisher's closure, so it is not offered as a rule."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="posting status",
        reason="the last reading of this posting says closed",
        count=5,
    )
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization required",
        count=3,
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        conditions = session.scalars(
            select(RefusalPatternProposal.condition).where(
                RefusalPatternProposal.workspace_id == workspace_id
            )
        ).all()
    assert conditions == ["eligibility"]


def test_aggregation_groups_reasons_that_differ_only_in_case_and_spacing(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    spellings = (
        "Compensation below floor",
        "compensation  below   floor",
        " COMPENSATION below floor ",
    )
    for reason in spellings:
        _record_refusals(
            durable_database, workspace_id, condition="compensation", reason=reason, count=1
        )
    with durable_database.session() as session:
        (proposal_id,) = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        proposal = session.get(RefusalPatternProposal, proposal_id)
        assert proposal is not None
        assert proposal.refusal_count == 3
        assert proposal.normalized_reason == "compensation below floor"
        assert sorted(proposal.reason_examples) == sorted(spellings)


def test_aggregation_uses_only_the_current_search_parameter_version(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    parameters = SearchParametersService(durable_database)
    first = parameters.save(workspace_id, JobSearchParameters())
    _record_refusals(
        durable_database,
        workspace_id,
        condition="arrangement",
        reason="Office cadence exceeds the saved limit.",
        count=3,
        search_parameter_set_id=first.id,
    )
    second = parameters.save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(minimum="senior")),
    )
    _record_refusals(
        durable_database,
        workspace_id,
        condition="arrangement",
        reason="Office cadence exceeds the saved limit.",
        count=2,
        search_parameter_set_id=second.id,
    )

    with durable_database.session() as session:
        assert refresh_for_workspace(session, workspace_id, threshold=3, window_days=30) == []

    _record_refusals(
        durable_database,
        workspace_id,
        condition="arrangement",
        reason="Office cadence exceeds the saved limit.",
        count=1,
        search_parameter_set_id=second.id,
    )
    with durable_database.session() as session:
        (proposal_id,) = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        proposal = session.get(RefusalPatternProposal, proposal_id)
        assert proposal is not None
        assert proposal.refusal_count == 3


def test_aggregation_is_idempotent_across_reruns(durable_database: Database) -> None:
    """A second aggregation over the same rows does not double-write."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database, workspace_id, condition="eligibility", reason="Same reason", count=4
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        session.commit()
    with durable_database.session() as session:
        second = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        session.commit()
    assert second == []
    with durable_database.session() as session:
        rows = session.scalars(
            select(RefusalPatternProposal).where(
                RefusalPatternProposal.workspace_id == workspace_id
            )
        ).all()
        assert len(rows) == 1


def test_aggregation_ignores_refusals_outside_window(durable_database: Database) -> None:
    """A group whose latest refusal predates the horizon does not propose."""
    workspace_id = _make_workspace(durable_database)
    horizon_past = datetime.now(UTC) - timedelta(days=60)
    with durable_database.session() as session:
        for offset in range(3):
            session.add(
                PostingRefusal(
                    workspace_id=workspace_id,
                    condition="eligibility",
                    reason="Same reason",
                    refused_at=horizon_past - timedelta(days=offset),
                    origin="test",
                )
            )
        session.commit()
    with durable_database.session() as session:
        written = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    assert written == []


def test_accept_records_the_ruling_without_writing_a_search_parameter(
    durable_database: Database,
) -> None:
    """Settling a pattern rules on it; it states no new search parameter."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization",
        count=3,
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    parameters = SearchParametersService(durable_database)
    history_before = parameters.history(workspace_id)
    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)

    accepted = inbox.accept(workspace_id, proposal.id)

    assert accepted.state == SearchProposalState.ACCEPTED
    (visible,) = inbox.list(workspace_id)
    assert visible == accepted
    assert parameters.history(workspace_id) == history_before
    assert parameters.current(workspace_id).excluded_conditions == []


def test_an_accepted_pattern_is_not_proposed_again(durable_database: Database) -> None:
    """A settled pattern stays settled however often the same refusals recur."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization",
        count=3,
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)
    inbox.accept(workspace_id, proposal.id)

    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization",
        count=3,
    )
    with durable_database.session() as session:
        assert refresh_for_workspace(session, workspace_id, threshold=3, window_days=30) == []


def test_repeat_accept_is_idempotent(
    durable_database: Database,
) -> None:
    """Accepting an accepted pattern does not move its decision time."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization",
        count=3,
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)
    first = inbox.accept(workspace_id, proposal.id)

    repeated = inbox.accept(workspace_id, proposal.id)

    assert repeated.state == SearchProposalState.ACCEPTED
    assert repeated.decided_at == first.decided_at


def test_the_live_tautological_arrangement_pattern_can_still_be_settled(
    durable_database: Database,
) -> None:
    """A group whose repeated reason is only the verdict is still a group to rule on."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="arrangement",
        reason="refused",
        count=3,
    )
    with durable_database.session() as session:
        (proposal_id,) = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)

    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)
    assert proposal.condition == "arrangement"
    assert proposal.normalized_reason == "refused"
    assert proposal.reason_examples == ["refused"]
    assert proposal.refusal_count == 3

    settled = inbox.accept(workspace_id, proposal_id)

    assert settled.state == SearchProposalState.ACCEPTED
    assert SearchParametersService(durable_database).current(workspace_id).excluded_conditions == []


def test_repeat_reject_is_idempotent_and_cannot_be_accepted_directly(
    durable_database: Database,
) -> None:
    """A rejected pattern stays terminal until aggregation proposes it again."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database, workspace_id, condition="eligibility", reason="Same reason", count=3
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)
    first = inbox.reject(workspace_id, proposal.id)

    repeated = inbox.reject(workspace_id, proposal.id)

    assert repeated.state == SearchProposalState.REJECTED
    assert repeated.decided_at == first.decided_at
    with pytest.raises(ConflictError, match="must recur and be proposed again"):
        inbox.accept(workspace_id, proposal.id)
    assert SearchParametersService(durable_database).current(workspace_id).excluded_conditions == []


def test_an_accepted_pattern_cannot_be_rejected(
    durable_database: Database,
) -> None:
    """A settled pattern is not reopened by the other button."""
    workspace_id = _make_workspace(durable_database)
    _record_refusals(
        durable_database,
        workspace_id,
        condition="eligibility",
        reason="US work authorization",
        count=3,
    )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    inbox = RefusalPatternProposalService(durable_database)
    (proposal,) = inbox.list(workspace_id)
    inbox.accept(workspace_id, proposal.id)

    with pytest.raises(ConflictError, match="already accepted"):
        inbox.reject(workspace_id, proposal.id)

    (accepted,) = inbox.list(workspace_id)
    assert accepted.state == SearchProposalState.ACCEPTED


def test_reasons_that_differ_only_in_quoted_evidence_form_one_pattern(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    for reason in (
        "the posting is hybrid, which you refuse",
        'the posting is hybrid, which you refuse: "#li-hybrid"',
        'the posting is hybrid, which you refuse: "#li-hybrid note: watch for job scams."',
    ):
        _record_refusals(
            durable_database, workspace_id, condition="arrangement", reason=reason, count=1
        )

    with durable_database.session() as session:
        (proposal_id,) = refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
        proposal = session.get(RefusalPatternProposal, proposal_id)
        assert proposal is not None
        assert proposal.normalized_reason == "the posting is hybrid, which you refuse"


def _refused_rows(
    database: Database,
    workspace_id: str,
    reason: str,
    employers: tuple[str, ...],
    *,
    search_parameter_set_id: str | None = None,
) -> list[str]:
    """File one refusal per employer against its own pending row."""
    pending_ids = [
        seed_pending_discovery(
            database, workspace_id, employer=employer, created_at=utc_now(), title="Data Lead"
        )
        for employer in employers
    ]
    with database.session() as session:
        for pending_id, employer in zip(pending_ids, employers, strict=True):
            session.add(
                PostingRefusal(
                    workspace_id=workspace_id,
                    pending_discovery_id=pending_id,
                    employer=employer,
                    title="Data Lead",
                    condition="travel",
                    reason=reason,
                    refused_at=utc_now(),
                    search_parameter_set_id=search_parameter_set_id,
                    origin="test",
                )
            )
    return pending_ids


def test_a_pattern_whose_refused_rows_all_returned_to_the_queue_is_not_proposed(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    _refused_rows(durable_database, workspace_id, "20 per cent against your 15", ("A", "B", "C"))
    standing = _refused_rows(
        durable_database, workspace_id, "40 per cent against your 25", ("D", "E", "F")
    )
    for pending_id in standing:
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)

    listed = RefusalPatternProposalService(durable_database).list(workspace_id)

    assert [item.normalized_reason for item in listed] == ["40 per cent against your 25"]


def test_a_pattern_lists_the_postings_it_refused(durable_database: Database) -> None:
    workspace_id = _make_workspace(durable_database)
    pending_ids = _refused_rows(
        durable_database, workspace_id, "40 per cent against your 25", ("Acme", "Globex", "Initech")
    )
    for pending_id in pending_ids:
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)

    [proposal] = RefusalPatternProposalService(durable_database).list(workspace_id)

    assert sorted((item.employer, item.title) for item in proposal.refused_postings) == [
        ("Acme", "Data Lead"),
        ("Globex", "Data Lead"),
        ("Initech", "Data Lead"),
    ]


def _travel_search(limit: int) -> JobSearchParameters:
    return JobSearchParameters(
        seniority=Seniority(target_titles=titles("Data Platform Lead")),
        locations=[Location(key="us", country="United States")],
        arrangement=Arrangement(maximum_travel_percent=limit),
    )


def test_a_refusal_made_under_replaced_parameters_no_longer_holds_its_proposal_open(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    parameters = SearchParametersService(durable_database)
    first = parameters.save(workspace_id, _travel_search(15))
    pending_ids = _refused_rows(
        durable_database,
        workspace_id,
        "20 per cent against your 15",
        ("Initech", "Globex", "Acme"),
        search_parameter_set_id=first.id,
    )
    for pending_id in pending_ids:
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    assert len(RefusalPatternProposalService(durable_database).list(workspace_id)) == 1

    parameters.save(workspace_id, _travel_search(25))

    assert RefusalPatternProposalService(durable_database).list(workspace_id) == []


def _travel_row(database: Database, workspace_id: str, employer: str, percent: int) -> str:
    """Stage one row whose retained body states its travel share."""
    url = f"https://boards.example.com/{employer.casefold()}/jobs/travel"
    facts = published_listing_facts("greenhouse", locations="New York, NY, United States")
    with database.session() as session:
        row = PendingDiscovery(
            workspace_id=workspace_id,
            fingerprint=new_id().replace("-", "")[:32] * 2,
            employer=employer,
            title="Data Platform Lead",
            location="New York, NY, United States",
            staged_facts=facts.as_json(),
            discovery_url=url,
            canonical_url=url,
            reason="Staged for a refusal test.",
            first_seen_on=utc_now().date(),
        )
        session.add(row)
        session.flush()
        pending_id = str(row.id)
    PostingContentResolutionService(
        database, DeterministicPostingContentResolver(), storage=runtime_storage(database)
    ).store_prefetched_result(
        workspace_id,
        pending_id,
        ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            fetched_at=utc_now(),
            content=(
                f"Data Platform Lead\n\nThis role requires travel up to {percent}% of the time.\n"
            ).encode(),
            http_status=200,
            publisher_markup_present=True,
            identity_evidence={"publisher_declaration": True},
            note="Verified publisher body.",
        ),
        None,
        origin="test",
    )
    return pending_id


def test_a_rescreen_that_reconfirms_a_refusal_keeps_its_proposal_open(
    durable_database: Database,
) -> None:
    workspace_id = _make_workspace(durable_database)
    parameters = SearchParametersService(durable_database)
    first = parameters.save(workspace_id, _travel_search(15))
    for employer in ("Initech", "Globex", "Acme"):
        pending_id = _travel_row(durable_database, workspace_id, employer, 20)
        file_refusal(
            durable_database,
            RefusedPosting(
                workspace_id,
                employer,
                "Data Platform Lead",
                pending_discovery_id=pending_id,
                search_parameter_set_id=first.id,
            ),
            condition="travel",
            reason="20 per cent against your 15",
            task_id=None,
            origin="evaluation.refusal",
            refresh=False,
        )
    with durable_database.session() as session:
        refresh_for_workspace(session, workspace_id, threshold=3, window_days=30)
    parameters.save(workspace_id, _travel_search(18))
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.rescreen_queue",
        payload={},
        idempotency_key=new_id(),
    )
    worker = Worker(
        durable_database,
        worker_id="rescreen-worker",
        handlers=default_handlers(
            durable_database, artifact_root=runtime_storage(durable_database)
        ),
    )

    assert worker.run_once()

    assert TaskQueueService(durable_database).get(str(task.id)).progress["restored_ids"] == []
    [proposal] = RefusalPatternProposalService(durable_database).list(workspace_id)
    assert proposal.normalized_reason == "20 per cent against your 15"
