"""Reversible pending-discovery disposition workflow."""

from collections import Counter
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.dispositions import (
    BODY_REFUSAL_CATEGORIES,
    DiscoveryDispositionService,
    dismissed_pending_ids,
)
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.discovery.source_reading import SourceListing
from tailorcv.application.failures import ConflictError, ValidationError
from tailorcv.application.job_search.models import (
    DispositionApply,
    DispositionSelection,
    PendingDuplicateSuggestionResponse,
)
from tailorcv.application.workspaces import SearchParametersService, WorkspaceService
from tailorcv.domain import (
    EXPLICIT_DISMISSAL_REASONS,
    ExplicitDismissalCategory,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingStatus,
    new_id,
    utc_now,
)
from tailorcv.domain.identity import normalize_text, normalize_url
from tailorcv.domain.search_parameters import (
    CountryNames,
    Employers,
    JobSearchParameters,
    Location,
    Seniority,
)
from tailorcv.domain.staged_facts import ListingIdentityTrust, PublishedFact, StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    Artifact,
    AuditEvent,
    Employer,
    JobEvaluation,
    JobPosting,
    PendingDiscovery,
    PendingDiscoveryDispositionEvent,
    PostingIdentifier,
    PostingUrl,
    Source,
)
from tailorcv.infrastructure.web_fetch.listing_facts import published_listing_facts
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, UnexpectedPostingFetcher
from tests.support.scenarios import (
    HOLDOUT_ORIGINS,
    board_source,
    dismiss_pending_discovery,
    employer_board,
    pending_posting_content,
    record_relevance_review,
    scan_workspace,
    seed_pending_discovery,
    titles,
)


def _pending(
    database: Database, workspace_id: str, title: str, *, variant_of: str | None = None
) -> str:
    pending_id = new_id()
    with database.session() as session:
        session.add(
            PendingDiscovery(
                id=pending_id,
                workspace_id=workspace_id,
                fingerprint=pending_id,
                employer="Northwind Fabrication",
                title=title,
                location="Remote",
                staged_facts=StagedListingFacts().as_json(),
                discovery_url=f"https://jobs.example.test/{pending_id}",
                canonical_url=f"https://jobs.example.test/{pending_id}",
                reason="Synthetic scan",
                first_seen_on=date(2026, 1, 1),
                regional_variant_of=variant_of,
            )
        )
    return pending_id


def _pending_at(database: Database, workspace_id: str, title: str, location: str) -> str:
    pending_id = new_id()
    facts = StagedListingFacts(
        locations=PublishedFact(values=(location,), provenance="board", complete=False)
    )
    with database.session() as session:
        session.add(
            PendingDiscovery(
                id=pending_id,
                workspace_id=workspace_id,
                fingerprint=pending_id,
                employer="Northwind Fabrication",
                title=title,
                location=location,
                staged_facts=facts.as_json(),
                discovery_url=f"https://jobs.example.test/{pending_id}",
                canonical_url=f"https://jobs.example.test/{pending_id}",
                reason="Synthetic scan",
                first_seen_on=date(2026, 1, 1),
            )
        )
    return pending_id


def test_explicit_dismissal_categories_are_closed_and_supply_stable_reasons() -> None:
    request = {
        "action": "dismiss",
        "selection_mode": "explicit",
        "pending_discovery_ids": ["pending-1"],
    }

    with pytest.raises(PydanticValidationError):
        DispositionSelection.model_validate(
            {**request, "category": "human_review", "reason": "Arbitrary prose"}
        )
    typed = DispositionSelection.model_validate({**request, "category": "out_of_scope_title"})
    assert typed.reason == "The title is outside the current search scope."
    invalid_page = DispositionSelection.model_validate({**request, "category": "invalid_page"})
    assert invalid_page.reason == "The page is not a job posting."
    unavailable = DispositionSelection.model_validate(
        {
            **request,
            "category": "current_evidence_unavailable",
        }
    )
    assert unavailable.reason == (
        "The posting was not evaluated: I chose to dismiss it because trustworthy current "
        "posting evidence remained unavailable."
    )
    with pytest.raises(PydanticValidationError, match="only for explicit human dismissals"):
        DispositionSelection.model_validate(
            {
                "action": "dismiss",
                "selection_mode": "rule",
                "category": "current_evidence_unavailable",
                "reason": "A machine must not emit the human stop reason.",
                "pending_discovery_ids": ["pending-1"],
            }
        )
    with pytest.raises(PydanticValidationError, match="requires a reason"):
        DispositionSelection.model_validate({**request, "category": "other", "reason": "  "})
    with pytest.raises(PydanticValidationError, match="requires a tracked job reference"):
        DispositionSelection.model_validate(
            {**request, "category": "duplicate_posting", "reason": "I found another copy."}
        )
    with pytest.raises(PydanticValidationError, match="requires a pending discovery reference"):
        DispositionSelection.model_validate(
            {**request, "category": "duplicate_pending", "reason": "I found another copy."}
        )


_EXPLICIT_DISMISSAL = {
    "action": "dismiss",
    "selection_mode": "explicit",
    "pending_discovery_ids": ["pending-1"],
}


@pytest.mark.parametrize("category", list(EXPLICIT_DISMISSAL_REASONS))
def test_an_explicit_category_keeps_the_persons_reason_verbatim(
    category: ExplicitDismissalCategory,
) -> None:
    reason = "  I reviewed this posting and chose to stop here.\t "
    references = {
        "duplicate_of_job_id": "job-1" if category == "duplicate_posting" else None,
        "duplicate_of_pending_id": "pending-2" if category == "duplicate_pending" else None,
    }
    selection = DispositionSelection.model_validate(
        {**_EXPLICIT_DISMISSAL, "category": category, "reason": reason, **references}
    )

    assert selection.category == category
    assert selection.reason == reason
    assert selection.duplicate_of_job_id == references["duplicate_of_job_id"]
    assert selection.duplicate_of_pending_id == references["duplicate_of_pending_id"]


@pytest.mark.parametrize("category", list(EXPLICIT_DISMISSAL_REASONS))
@pytest.mark.parametrize(
    "reason_document", [{}, {"reason": ""}, {"reason": " "}, {"reason": "\t\n"}]
)
def test_an_explicit_category_supplies_its_default_when_the_reason_is_blank(
    category: ExplicitDismissalCategory, reason_document: dict[str, str]
) -> None:
    selection = DispositionSelection.model_validate(
        {
            **_EXPLICIT_DISMISSAL,
            "category": category,
            **reason_document,
            "duplicate_of_job_id": "job-1" if category == "duplicate_posting" else None,
            "duplicate_of_pending_id": "pending-2" if category == "duplicate_pending" else None,
        }
    )

    assert selection.reason == EXPLICIT_DISMISSAL_REASONS[category]


@pytest.mark.parametrize(
    "reference",
    [
        {"duplicate_of_job_id": "job-1"},
        {"duplicate_of_pending_id": "pending-2"},
        {"duplicate_of_job_id": "job-1", "duplicate_of_pending_id": "pending-2"},
    ],
)
def test_free_prose_dismissal_refuses_a_duplicate_reference(reference: dict[str, str]) -> None:
    with pytest.raises(PydanticValidationError, match="require a typed duplicate category"):
        DispositionSelection.model_validate(
            {
                **_EXPLICIT_DISMISSAL,
                "category": "other",
                "reason": "The team withdrew the opening before the queue was reviewed.",
                **reference,
            }
        )


@pytest.mark.parametrize(
    "document",
    [
        {"category": "invalid_page", "duplicate_of_job_id": "job-1"},
        {"category": "closed_at_source", "duplicate_of_job_id": "job-1"},
        {
            "category": "duplicate_pending",
            "duplicate_of_job_id": "job-1",
            "duplicate_of_pending_id": "pending-2",
        },
    ],
)
def test_a_tracked_job_reference_is_refused_outside_a_duplicate_posting(
    document: dict[str, str],
) -> None:
    with pytest.raises(PydanticValidationError, match="only for duplicate postings"):
        DispositionSelection.model_validate(
            {**_EXPLICIT_DISMISSAL, "reason": "I found another copy.", **document}
        )


@pytest.mark.parametrize(
    "document",
    [
        {"category": "invalid_page", "duplicate_of_pending_id": "pending-2"},
        {"category": "out_of_scope_location", "duplicate_of_pending_id": "pending-2"},
        {
            "category": "duplicate_posting",
            "duplicate_of_job_id": "job-1",
            "duplicate_of_pending_id": "pending-2",
        },
    ],
)
def test_a_pending_discovery_reference_is_refused_outside_a_duplicate_pending(
    document: dict[str, str],
) -> None:
    with pytest.raises(PydanticValidationError, match="only for pending duplicates"):
        DispositionSelection.model_validate(
            {**_EXPLICIT_DISMISSAL, "reason": "I found another copy.", **document}
        )


def test_both_duplicate_references_on_a_plain_category_name_the_tracked_job_first() -> None:
    # Both references are wrong here, and the tracked-job refusal is the one reported.
    with pytest.raises(PydanticValidationError, match="only for duplicate postings"):
        DispositionSelection.model_validate(
            {
                **_EXPLICIT_DISMISSAL,
                "category": "invalid_page",
                "duplicate_of_job_id": "job-1",
                "duplicate_of_pending_id": "pending-2",
            }
        )


def test_pending_duplicate_reference_is_in_workspace_not_self_and_persisted(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="pending-duplicate", name="Pending duplicate")
        .id
    )
    other_workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="other-pending", name="Other pending")
        .id
    )
    source_id = _pending(durable_database, workspace_id, "Platform Steward")
    variant_id = _pending(
        durable_database, workspace_id, "Platform Steward, Remote", variant_of=source_id
    )
    retained_id = _pending(durable_database, workspace_id, "Platform Steward, Canada")
    foreign_id = _pending(durable_database, other_workspace_id, "Platform Steward")
    service = DiscoveryDispositionService(durable_database)

    retained_dismissal = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "invalid_page",
            "pending_discovery_ids": [retained_id],
        }
    )
    retained_preview = service.preview(workspace_id, retained_dismissal)
    service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**retained_dismissal.model_dump(), "snapshot_sha256": retained_preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="test",
        correlation_id="dismiss-retained",
    )
    candidates = {item.id: item for item in service.duplicate_candidates(workspace_id)}
    assert candidates[source_id].dismissed is False
    assert candidates[retained_id].dismissed is True
    assert foreign_id not in candidates

    request_document = {
        "action": "dismiss",
        "selection_mode": "explicit",
        "category": "duplicate_pending",
        "pending_discovery_ids": [source_id],
        "reason": "  I retained the fuller posting instead.  ",
    }
    for reference_id in (source_id, variant_id):
        with pytest.raises(ValidationError, match="cannot duplicate itself"):
            service.preview(
                workspace_id,
                DispositionSelection.model_validate(
                    {**request_document, "duplicate_of_pending_id": reference_id}
                ),
            )
    for reference_id in (foreign_id, "missing-pending"):
        with pytest.raises(ValidationError, match="not a discovery in this workspace"):
            service.preview(
                workspace_id,
                DispositionSelection.model_validate(
                    {**request_document, "duplicate_of_pending_id": reference_id}
                ),
            )

    selection = DispositionSelection.model_validate(
        {**request_document, "duplicate_of_pending_id": retained_id}
    )
    preview = service.preview(workspace_id, selection)
    events = service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="test",
        correlation_id="duplicate-pending",
    )

    assert {event.pending_discovery_id for event in events} == {source_id, variant_id}
    with durable_database.session() as session:
        for event in events:
            assert event.duplicate_of_pending_id == retained_id
            assert event.duplicate_of_job_id is None
            assert event.category == "duplicate_pending"
            assert event.reason == request_document["reason"]
            stored = session.get(PendingDiscoveryDispositionEvent, event.id)
            assert stored is not None
            assert stored.duplicate_of_pending_id == retained_id
            assert stored.reason == request_document["reason"]


def test_an_evaluation_refusal_dismisses_only_the_row_it_judged(
    durable_database: Database,
) -> None:
    """A human dismissal reaches a whole variant family, and its preview names them.

    An automatic refusal renders no preview and judged one posting, so a sibling
    market keeps its place in the queue and gets its own gate.
    """
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="refusal-variant", name="Refusal variant")
        .id
    )
    SearchParametersService(durable_database).save(workspace_id, JobSearchParameters())
    primary = _pending(durable_database, workspace_id, "Regional Fabrication Lead")
    variant = _pending(
        durable_database, workspace_id, "Regional Fabrication Lead", variant_of=primary
    )
    service = DiscoveryDispositionService(durable_database)

    with durable_database.session() as session:
        service.dismiss_refusal_in_session(
            session,
            workspace_id,
            primary,
            category="office attendance",
            reason="The posting requires four office days a week.",
            search_parameter_set_id=None,
            actor="worker",
            origin="evaluation.refusal",
            correlation_id="task-1",
        )

    dismissed = {row.pending_discovery_id for row in service.dismissed(workspace_id)}
    assert dismissed == {primary}
    assert variant not in dismissed


def _body_screen_workspace(database: Database, slug: str) -> tuple[str, str]:
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug=slug, name=slug).id
    )
    SearchParametersService(database).save(workspace_id, JobSearchParameters())
    return workspace_id, _pending(database, workspace_id, "Platform Product Manager")


def _body_screen_dismiss(
    database: Database, workspace_id: str, pending_id: str, category: str
) -> list[str]:
    with database.session() as session:
        events = DiscoveryDispositionService(database).dismiss_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            category=category,
            reason="A verified publisher body conflicts with the search boundary.",
            search_parameter_set_id=None,
            actor="local_user",
            origin="source.scan.body_screen",
            correlation_id="scan-1",
        )
    return [event.id for event in events]


def _found_posting(database: Database, origin: str) -> tuple[str, str]:
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="person-added", name="Person added")
        .id
    )
    SearchParametersService(database).save(workspace_id, JobSearchParameters())
    staged = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging.stage_found_posting(
        workspace_id,
        "https://job-boards.greenhouse.io/globex-health/jobs/1005",
        date(2026, 9, 29),
        origin=origin,
    )
    assert staged is not None
    return workspace_id, staged.pending_discovery_id


def _is_dismissed(database: Database, workspace_id: str, pending_id: str) -> bool:
    dismissed = DiscoveryDispositionService(database).dismissed(workspace_id)
    return pending_id in {row.pending_discovery_id for row in dismissed}


@pytest.mark.parametrize(
    ("origin", "dismissed"),
    [("api", False), ("tailorcv", False), ("search.guided_campaign", True)],
)
def test_an_automatic_refusal_leaves_a_posting_the_person_added_in_the_queue(
    durable_database: Database, origin: str, dismissed: bool
) -> None:
    workspace_id, pending_id = _found_posting(durable_database, origin)

    events = _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    assert bool(events) is dismissed
    assert _is_dismissed(durable_database, workspace_id, pending_id) is dismissed


def test_a_posting_the_person_added_stays_theirs_once_a_tracked_board_lists_it(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _found_posting(durable_database, "api")
    url = "https://job-boards.greenhouse.io/globex-health/jobs/1005"
    source_id = board_source(
        durable_database,
        workspace_id,
        "Globex Health",
        "https://job-boards.greenhouse.io/globex-health",
    )
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging.stage_listings(
        workspace_id,
        [SourceListing("Globex Health", "Data Lead", url, location="Remote")],
        date(2026, 9, 30),
        reason="Board scan matched the title filter.",
        source_id=source_id,
        refresh_only=True,
    )

    events = _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    assert events == []
    assert not _is_dismissed(durable_database, workspace_id, pending_id)


def test_a_posting_the_person_added_stays_out_of_a_dismissed_opening_a_board_lists_it_under(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _found_posting(durable_database, "api")
    board = "https://job-boards.greenhouse.io/globex-health"
    source_id = board_source(durable_database, workspace_id, "Globex Health", board)
    staging = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).staging
    head = staging.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex Health",
                "Data Lead",
                f"{board}/jobs/1001",
                location="Boston",
                requisition_id="R-100",
            )
        ],
        date(2026, 9, 1),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )
    head_id = head.row_ids_by_url[f"{board}/jobs/1001"]
    assert _body_screen_dismiss(durable_database, workspace_id, head_id, "location_outside")

    staging.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex Health",
                "Data Lead",
                f"{board}/jobs/1005",
                location="Remote",
                requisition_id="R-100",
            )
        ],
        date(2026, 9, 30),
        reason="Board scan matched the title filter.",
        source_id=source_id,
    )

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.regional_variant_of is None


def test_a_posting_the_person_added_leaves_the_queue_once_its_body_has_no_requirements(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _found_posting(durable_database, "api")

    events = _no_requirements_dismiss(durable_database, workspace_id, pending_id)

    assert events
    assert _is_dismissed(durable_database, workspace_id, pending_id)


def test_a_restore_without_provenance_is_refused_by_the_schema(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "unlabelled-restore")
    [dismissal_id] = _body_screen_dismiss(
        durable_database, workspace_id, pending_id, "location_outside"
    )

    with pytest.raises(IntegrityError), durable_database.session() as session:
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="restore",
                restore_provenance=None,
                category="",
                reason="Unlabelled restore.",
                selection_mode="explicit",
                snapshot_sha256="restore",
                supersedes_event_id=dismissal_id,
                created_by="local_user",
                origin="test",
            )
        )


def _person_restore(database: Database, workspace_id: str, pending_id: str, category: str) -> None:
    service = DiscoveryDispositionService(database)
    restore = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": category,
            "reason": "Aster Systems hires from my country; reviewing it myself.",
            "pending_discovery_ids": [pending_id],
        }
    )
    preview = service.preview(workspace_id, restore)
    service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**restore.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id="restore",
    )


def test_a_later_body_screen_does_not_override_a_persons_restore(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "person-restore")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")
    _person_restore(durable_database, workspace_id, pending_id, "location_outside")

    appended = _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert appended == []
    assert [event.action for event in history] == ["dismiss", "restore"]
    with durable_database.session() as session:
        restored = session.scalars(
            select(PendingDiscoveryDispositionEvent).where(
                PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id,
                PendingDiscoveryDispositionEvent.action == "restore",
            )
        ).one()
        assert restored.restore_provenance == "person"
        assert pending_id not in dismissed_pending_ids(session, workspace_id)


@pytest.mark.parametrize(
    ("restored", "screened"),
    [
        ("arrangement", "arrangement_refused"),
        ("arrangement_refused", "arrangement"),
        ("office attendance", "arrangement_office_limit"),
        ("travel", "travel_over_limit"),
        ("credential", "condition_excluded"),
        ("residence requirement", "location_residence_required"),
        ("location_residence_required", "residence requirement"),
    ],
)
def test_a_persons_restore_on_one_surface_holds_against_the_other(
    durable_database: Database, restored: str, screened: str
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "cross-restore")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, restored)
    _person_restore(durable_database, workspace_id, pending_id, restored)

    with durable_database.session() as session:
        assert DiscoveryDispositionService(durable_database).person_restored_in_session(
            session, workspace_id, pending_id, screened
        )
    assert _body_screen_dismiss(durable_database, workspace_id, pending_id, screened) == []


def test_an_automatic_body_restore_is_not_a_persons_override(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "automatic-restore")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")
    with durable_database.session() as session:
        restored = DiscoveryDispositionService(durable_database).restore_body_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            cleared=BODY_REFUSAL_CATEGORIES,
            actor="local_user",
            origin="source.scan.body_screen",
            correlation_id="scan-2",
        )
    assert len(restored) == 1

    appended = _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert len(appended) == 1
    assert [event.action for event in history] == ["dismiss", "restore", "dismiss"]
    with durable_database.session() as session:
        restore_event = session.scalars(
            select(PendingDiscoveryDispositionEvent).where(
                PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id,
                PendingDiscoveryDispositionEvent.action == "restore",
            )
        ).one()
        assert restore_event.restore_provenance == "audit"
        assert pending_id in dismissed_pending_ids(session, workspace_id)


def test_an_automatic_body_restore_of_arrangement_refused_is_not_a_persons_override(
    durable_database: Database,
) -> None:
    """A structured hybrid/on-site dismissal restores and re-dismisses like a location one."""
    workspace_id, pending_id = _body_screen_workspace(durable_database, "arrangement-restore")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "arrangement_refused")
    with durable_database.session() as session:
        restored = DiscoveryDispositionService(durable_database).restore_body_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            cleared=BODY_REFUSAL_CATEGORIES,
            actor="local_user",
            origin="source.scan.body_screen",
            correlation_id="scan-2",
        )
    assert len(restored) == 1

    category = "arrangement_refused"
    appended = _body_screen_dismiss(durable_database, workspace_id, pending_id, category)

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert len(appended) == 1
    assert [event.action for event in history] == ["dismiss", "restore", "dismiss"]
    with durable_database.session() as session:
        assert pending_id in dismissed_pending_ids(session, workspace_id)


def test_a_persons_restore_of_arrangement_refused_survives_a_later_body_screen(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "arr-person-restore")
    category = "arrangement_refused"
    _body_screen_dismiss(durable_database, workspace_id, pending_id, category)
    _person_restore(durable_database, workspace_id, pending_id, category)

    appended = _body_screen_dismiss(durable_database, workspace_id, pending_id, category)

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert appended == []
    assert [event.action for event in history] == ["dismiss", "restore"]
    with durable_database.session() as session:
        assert pending_id not in dismissed_pending_ids(session, workspace_id)


def _no_requirements_dismiss(database: Database, workspace_id: str, pending_id: str) -> list[str]:
    with database.session() as session:
        events = DiscoveryDispositionService(database).dismiss_no_requirements_in_session(
            session,
            workspace_id,
            pending_id,
            content_sha256="a" * 64,
            actor="worker",
            origin="evaluation.body_screen",
            correlation_id="evaluate-1",
        )
    return [event.id for event in events]


def test_a_new_body_does_not_restore_a_withdrawn_no_requirements_dismissal(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "withdrawn-no-reqs")
    _no_requirements_dismiss(durable_database, workspace_id, pending_id)
    _withdraw(durable_database, pending_id)

    with durable_database.session() as session:
        restored = DiscoveryDispositionService(durable_database).restore_no_requirements_in_session(
            session,
            workspace_id,
            pending_id,
            content_sha256="b" * 64,
            actor="worker",
            origin="evaluation.body_screen",
            correlation_id="evaluate-2",
        )

    assert restored == []
    with durable_database.session() as session:
        assert pending_id in dismissed_pending_ids(session, workspace_id)


def test_a_body_screen_that_does_not_clear_the_dismissed_category_leaves_it_dismissed(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "uncleared-refusal")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    with durable_database.session() as session:
        restored = DiscoveryDispositionService(durable_database).restore_body_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            cleared=BODY_REFUSAL_CATEGORIES - {"location_outside"},
            actor="worker",
            origin="source.continue.body_screen",
            correlation_id="continue-2",
        )

    assert restored == []
    with durable_database.session() as session:
        assert pending_id in dismissed_pending_ids(session, workspace_id)


def test_a_clean_body_screen_does_not_restore_a_withdrawn_refusal(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "withdrawn-refusal")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")
    _withdraw(durable_database, pending_id)

    with durable_database.session() as session:
        restored = DiscoveryDispositionService(durable_database).restore_body_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            cleared=BODY_REFUSAL_CATEGORIES,
            actor="local_user",
            origin="source.scan.body_screen",
            correlation_id="scan-2",
        )

    assert restored == []
    with durable_database.session() as session:
        assert pending_id in dismissed_pending_ids(session, workspace_id)


def test_a_persons_restore_blocks_a_later_no_requirements_dismissal(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "no-requirements")
    _no_requirements_dismiss(durable_database, workspace_id, pending_id)
    _person_restore(durable_database, workspace_id, pending_id, "no_requirements")

    appended = _no_requirements_dismiss(durable_database, workspace_id, pending_id)

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert appended == []
    assert [(event.action, event.category) for event in history] == [
        ("dismiss", "no_requirements"),
        ("restore", "no_requirements"),
    ]
    with durable_database.session() as session:
        assert pending_id not in dismissed_pending_ids(session, workspace_id)


def test_a_persons_later_dismissal_of_the_category_withdraws_their_restore(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="withdrawn-restore", name="Withdrawn restore")
        .id
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(
            locations=[Location(key="home", country="Canada")],
            countries=[CountryNames(country="Canada"), CountryNames(country="United States")],
        ),
    )
    foreign = _pending_at(
        durable_database, workspace_id, "Platform Product Manager", "Bangalore, India"
    )
    service = DiscoveryDispositionService(durable_database)
    _body_screen_dismiss(durable_database, workspace_id, foreign, "location_outside")
    _person_restore(durable_database, workspace_id, foreign, "location_outside")
    with durable_database.session() as session:
        assert service.person_restored_in_session(
            session, workspace_id, foreign, "location_outside"
        )
    dismiss = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "rule",
            "category": "location_outside",
            "reason": "Bangalore is outside my search after all.",
            "pending_discovery_ids": [foreign],
        }
    )
    preview = service.preview(workspace_id, dismiss)
    service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**dismiss.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id="dismiss-again",
    )

    with durable_database.session() as session:
        assert not service.person_restored_in_session(
            session, workspace_id, foreign, "location_outside"
        )


def test_an_automatic_body_restore_refuses_an_origin_outside_the_body_screen(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "restore-origin")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")

    with (
        durable_database.session() as session,
        pytest.raises(ValidationError, match="body-screen origin"),
    ):
        DiscoveryDispositionService(durable_database).restore_body_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            cleared=BODY_REFUSAL_CATEGORIES,
            actor="local_user",
            origin="api",
            correlation_id="restore-origin",
        )

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert [event.action for event in history] == ["dismiss"]


def test_a_persons_restore_does_not_block_a_different_refusal(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "other-category")
    _body_screen_dismiss(durable_database, workspace_id, pending_id, "location_outside")
    _person_restore(durable_database, workspace_id, pending_id, "location_outside")

    appended = _body_screen_dismiss(
        durable_database, workspace_id, pending_id, "location_residence_required"
    )

    history = DiscoveryDispositionService(durable_database).history(workspace_id, pending_id)
    assert len(appended) == 1
    assert [(event.action, event.category) for event in history] == [
        ("dismiss", "location_outside"),
        ("restore", "location_outside"),
        ("dismiss", "location_residence_required"),
    ]
    with durable_database.session() as session:
        assert pending_id in dismissed_pending_ids(session, workspace_id)


_PROGRAM_ORIGINS = (*HOLDOUT_ORIGINS, "source.scan.body_screen", "evaluation.refusal")


@pytest.mark.parametrize("origin", _PROGRAM_ORIGINS)
def test_a_dismissal_the_program_made_is_never_reported_as_the_persons(
    durable_database: Database, origin: str
) -> None:
    workspace_id, pending_id = _body_screen_workspace(
        durable_database, origin.replace(".", "-").replace("_", "-")
    )
    service = DiscoveryDispositionService(durable_database)
    with durable_database.session() as session:
        service.dismiss_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            category="location_outside",
            reason="Verified board facts meet a structured search hold-out.",
            search_parameter_set_id=None,
            actor="worker",
            origin=origin,
            correlation_id=None,
        )

    with durable_database.session() as session:
        assert service.person_dismissal_in_session(session, workspace_id, pending_id) is None


@pytest.mark.parametrize("origin", _PROGRAM_ORIGINS)
def test_protected_source_reads_include_person_dismissals_but_not_program_dismissals(
    durable_database: Database, origin: str
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "read-protection")
    other_id = _pending(durable_database, workspace_id, "Data Engineer")
    service = DiscoveryDispositionService(durable_database)
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    with durable_database.session() as session:
        service.dismiss_refusal_in_session(
            session,
            workspace_id,
            other_id,
            category="location_outside",
            reason="Published location falls outside the search.",
            search_parameter_set_id=None,
            actor="worker",
            origin=origin,
            correlation_id=None,
        )
    with durable_database.session() as session:
        assert service.person_dismissed_ids_in_session(session, workspace_id) == frozenset(
            {pending_id}
        )


def test_a_dismissal_the_person_made_is_still_reported_as_theirs(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "person-dismissal")
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    service = DiscoveryDispositionService(durable_database)
    with durable_database.session() as session:
        dismissal = service.person_dismissal_in_session(session, workspace_id, pending_id)

    assert dismissal is not None
    assert dismissal.category == "human_review"


@pytest.mark.parametrize("origin", HOLDOUT_ORIGINS)
def test_a_structured_holdout_is_reported_as_the_programs_own_holdout(
    durable_database: Database, origin: str
) -> None:
    workspace_id, pending_id = _body_screen_workspace(
        durable_database, "held-" + origin.replace(".", "-").replace("_", "-")
    )
    service = DiscoveryDispositionService(durable_database)
    with durable_database.session() as session:
        service.dismiss_refusal_in_session(
            session,
            workspace_id,
            pending_id,
            category="location_outside",
            reason="Verified board facts meet a structured search hold-out.",
            search_parameter_set_id=None,
            actor="worker",
            origin=origin,
            correlation_id=None,
        )

    with durable_database.session() as session:
        held_out = service.structured_holdout_in_session(session, workspace_id, pending_id)

    assert held_out is not None
    assert held_out.origin == origin


def test_a_persons_dismissal_is_not_reported_as_a_structured_holdout(
    durable_database: Database,
) -> None:
    workspace_id, pending_id = _body_screen_workspace(durable_database, "person-not-holdout")
    dismiss_pending_discovery(durable_database, workspace_id, pending_id)

    service = DiscoveryDispositionService(durable_database)
    with durable_database.session() as session:
        assert service.structured_holdout_in_session(session, workspace_id, pending_id) is None


def test_rule_disposition_resolves_variants_and_rejects_a_stale_preview(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="dismiss", name="Dismiss")
        .id
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(excluded_titles=["Retail"])),
    )
    primary = _pending(durable_database, workspace_id, "Retail Display Coordinator")
    variant = _pending(
        durable_database, workspace_id, "Retail Display Coordinator", variant_of=primary
    )
    service = DiscoveryDispositionService(durable_database)
    request = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "rule",
            "category": "title_excluded",
            "reason": "Outside the accepted title policy",
            "pending_discovery_ids": [primary],
        }
    )

    preview = service.preview(workspace_id, request)

    assert {(target.id, target.kind) for target in preview.targets} == {
        (primary, "primary"),
        (variant, "regional_variant"),
    }
    assert {target.primary_id for target in preview.targets} == {primary}
    assert all(target.employer == "Northwind Fabrication" for target in preview.targets)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(excluded_titles=["Seasonal"])),
    )
    with pytest.raises(ConflictError, match="stale"):
        service.apply(
            workspace_id,
            DispositionApply.model_validate(
                {**request.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
            ),
            actor="local_user",
            origin="test",
            correlation_id="test-disposition",
        )


def test_rule_dismissal_reaches_a_row_on_an_excluded_employers_board(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(employers=Employers(excluded_employers=["Globex"])),
    )
    pending_id = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Initech",
        created_at=utc_now(),
        source_id=employer_board(durable_database, workspace_id, "Globex"),
    )
    request = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "rule",
            "category": "employer_excluded",
            "reason": "The board belongs to an excluded employer",
            "pending_discovery_ids": [pending_id],
        }
    )

    preview = DiscoveryDispositionService(durable_database).preview(workspace_id, request)

    assert [(target.id, target.employer) for target in preview.targets] == [(pending_id, "Initech")]


@pytest.mark.parametrize("category", ["out_of_scope_title", "current_evidence_unavailable"])
def test_explicit_dismissal_and_restore_append_a_history(
    durable_database: Database, category: str
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="restore", name="Restore")
        .id
    )
    pending_id = _pending(durable_database, workspace_id, "Systems Steward")
    service = DiscoveryDispositionService(durable_database)
    reason = "  I reviewed the available posting and chose to stop here.\t "
    dismiss = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": category,
            "reason": reason,
            "pending_discovery_ids": [pending_id],
        }
    )
    preview = service.preview(workspace_id, dismiss)
    assert preview.category == category
    assert preview.reason == reason
    confirmed_dismissal = DispositionApply.model_validate(
        {**dismiss.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
    )
    [event] = service.apply(
        workspace_id,
        confirmed_dismissal,
        actor="local_user",
        origin="test",
        correlation_id="dismiss",
    )
    assert event.reason == reason
    with durable_database.session() as session:
        stored = session.get(PendingDiscoveryDispositionEvent, event.id)
        assert stored is not None
        assert stored.reason == reason
        assert stored.category == category
        assert stored.pending_discovery_id == pending_id
        assert (stored.created_by, stored.origin, stored.correlation_id) == (
            "local_user",
            "test",
            "dismiss",
        )
    assert (
        SourceScanService(
            durable_database,
            storage=runtime_storage(durable_database),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).list_pending_discoveries(workspace_id)
        == []
    )
    dismissed = service.dismissed(workspace_id)
    assert [item.pending_discovery_id for item in dismissed] == [pending_id]
    assert dismissed[0].employer == "Northwind Fabrication"
    assert dismissed[0].title == "Systems Steward"
    assert dismissed[0].canonical_url.endswith(pending_id)
    assert dismissed[0].selection_mode == "explicit"
    with pytest.raises(ConflictError, match="stale"):
        service.apply(
            workspace_id,
            confirmed_dismissal,
            actor="local_user",
            origin="test",
            correlation_id="repeat",
        )
    restore = DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": "human_review",
            "reason": "Returned to review",
            "pending_discovery_ids": [pending_id],
        }
    )
    restore_preview = service.preview(workspace_id, restore)
    service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**restore.model_dump(), "snapshot_sha256": restore_preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="test",
        correlation_id="restore",
    )

    history = service.history(workspace_id, pending_id)
    assert [event.action for event in history] == ["dismiss", "restore"]
    assert history[0].category == category
    assert history[0].reason == reason
    assert history[0].selection_mode == "explicit"
    assert history[1].category == "human_review"
    assert history[1].supersedes_event_id == history[0].id


def test_changing_an_explicit_reason_requires_a_new_preview(durable_database: Database) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="changed-reason", name="Changed reason")
        .id
    )
    pending_id = _pending(durable_database, workspace_id, "Systems Steward")
    service = DiscoveryDispositionService(durable_database)
    selection = DispositionSelection.model_validate(
        {
            **_EXPLICIT_DISMISSAL,
            "category": "out_of_scope_title",
            "reason": "I read the role and decided to dismiss it.",
            "pending_discovery_ids": [pending_id],
        }
    )
    preview = service.preview(workspace_id, selection)

    with pytest.raises(ConflictError, match="stale"):
        service.apply(
            workspace_id,
            DispositionApply.model_validate(
                {
                    **selection.model_dump(),
                    "reason": "I changed my explanation after reading the preview.",
                    "snapshot_sha256": preview.snapshot_sha256,
                }
            ),
            actor="local_user",
            origin="test",
            correlation_id="changed-reason",
        )

    assert service.history(workspace_id, pending_id) == []


def _decide(
    service: DiscoveryDispositionService,
    workspace_id: str,
    action: str,
    pending_ids: list[str],
) -> list[str]:
    """Preview and apply one explicit decision, returning the rows it reached."""
    selection = DispositionSelection.model_validate(
        {
            "action": action,
            "selection_mode": "explicit",
            "category": "closed_at_source" if action == "dismiss" else "human_review",
            "reason": "Checked the posting by hand.",
            "pending_discovery_ids": pending_ids,
        }
    )
    preview = service.preview(workspace_id, selection)
    applied = service.apply(
        workspace_id,
        DispositionApply.model_validate(
            {**selection.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
        ),
        actor="local_user",
        origin="api",
        correlation_id=action,
    )
    return sorted(event.pending_discovery_id for event in applied)


def _queued_ids(database: Database, workspace_id: str) -> list[str]:
    return [
        item.id
        for item in SourceScanService(
            database,
            storage=runtime_storage(database),
            posting_fetcher=UnexpectedPostingFetcher(),
        ).list_pending_discoveries(workspace_id)
    ]


def _withdraw(database: Database, pending_id: str) -> None:
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        row.withdrawn_at = utc_now()


def _restore_selection(pending_ids: list[str]) -> DispositionSelection:
    return DispositionSelection.model_validate(
        {
            "action": "restore",
            "selection_mode": "explicit",
            "category": "human_review",
            "reason": "Checked the posting by hand.",
            "pending_discovery_ids": pending_ids,
        }
    )


def test_a_restore_preview_names_withdrawn_rows_as_not_restorable(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="preview-withdrawn")
    live_id = _pending(durable_database, workspace_id, "Systems Steward")
    gone_id = _pending(durable_database, workspace_id, "Quality Steward")
    service = DiscoveryDispositionService(durable_database)
    _decide(service, workspace_id, "dismiss", [live_id, gone_id])
    _withdraw(durable_database, gone_id)

    preview = service.preview(workspace_id, _restore_selection([live_id, gone_id]))

    assert [target.id for target in preview.targets] == [live_id]
    assert preview.skipped_withdrawn_ids == [gone_id]


def test_a_restore_of_a_mixed_selection_leaves_the_withdrawn_row_dismissed(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="apply-withdrawn")
    live_id = _pending(durable_database, workspace_id, "Systems Steward")
    gone_id = _pending(durable_database, workspace_id, "Quality Steward")
    service = DiscoveryDispositionService(durable_database)
    _decide(service, workspace_id, "dismiss", [live_id, gone_id])
    _withdraw(durable_database, gone_id)

    assert _decide(service, workspace_id, "restore", [live_id, gone_id]) == [live_id]

    assert [event.action for event in service.history(workspace_id, gone_id)] == ["dismiss"]
    assert _queued_ids(durable_database, workspace_id) == [live_id]
    with durable_database.session() as session:
        assert dismissed_pending_ids(session, workspace_id) == {gone_id}


def test_a_dismissal_preview_says_which_targets_were_withdrawn(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="dismiss-withdrawn")
    live_id = _pending(durable_database, workspace_id, "Systems Steward")
    gone_id = _pending(durable_database, workspace_id, "Quality Steward")
    _withdraw(durable_database, gone_id)
    selection = DispositionSelection.model_validate(
        {
            "action": "dismiss",
            "selection_mode": "explicit",
            "category": "closed_at_source",
            "reason": "Checked the posting by hand.",
            "pending_discovery_ids": [live_id, gone_id],
        }
    )

    preview = DiscoveryDispositionService(durable_database).preview(workspace_id, selection)

    withdrawn = {target.id: target.withdrawn_at is not None for target in preview.targets}
    assert withdrawn == {live_id: False, gone_id: True}


def test_the_restorable_dismissals_leave_out_a_withdrawn_row(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database, slug="dismissed-withdrawn")
    live_id = _pending(durable_database, workspace_id, "Systems Steward")
    gone_id = _pending(durable_database, workspace_id, "Quality Steward")
    service = DiscoveryDispositionService(durable_database)
    _decide(service, workspace_id, "dismiss", [live_id, gone_id])
    _withdraw(durable_database, gone_id)

    assert [row.pending_discovery_id for row in service.dismissed(workspace_id)] == [live_id]


def test_a_restore_of_only_withdrawn_rows_is_refused(durable_database: Database) -> None:
    workspace_id = scan_workspace(durable_database, slug="only-withdrawn")
    gone_id = _pending(durable_database, workspace_id, "Quality Steward")
    service = DiscoveryDispositionService(durable_database)
    _decide(service, workspace_id, "dismiss", [gone_id])
    selection = _restore_selection([gone_id])
    earlier = service.preview(workspace_id, selection)
    _withdraw(durable_database, gone_id)

    with pytest.raises(ConflictError, match="withdrawn"):
        service.preview(workspace_id, selection)
    with pytest.raises(ConflictError, match="withdrawn"):
        service.apply(
            workspace_id,
            DispositionApply.model_validate(
                {**selection.model_dump(), "snapshot_sha256": earlier.snapshot_sha256}
            ),
            actor="local_user",
            origin="api",
            correlation_id="only-withdrawn",
        )

    assert [event.action for event in service.history(workspace_id, gone_id)] == ["dismiss"]


def test_restoring_a_withdrawn_head_dismissed_with_its_copy_queues_only_the_copy(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="restore-withdrawn")
    head_id = _pending(durable_database, workspace_id, "Systems Steward")
    copy_id = _pending(durable_database, workspace_id, "Systems Steward", variant_of=head_id)
    _withdraw(durable_database, head_id)
    service = DiscoveryDispositionService(durable_database)
    assert _decide(service, workspace_id, "dismiss", [head_id]) == sorted([head_id, copy_id])

    assert _decide(service, workspace_id, "restore", [head_id]) == [copy_id]

    assert [event.action for event in service.history(workspace_id, head_id)] == ["dismiss"]
    assert _queued_ids(durable_database, workspace_id) == [copy_id]
    with durable_database.session() as session:
        copy = session.get(PendingDiscovery, copy_id)
        assert copy is not None
        assert copy.regional_variant_of is None
        released = session.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_id == copy_id,
                AuditEvent.action == "pending_discovery.regional_variant_released",
            )
        ).one()
        assert released.details["released_from_head"] == head_id


def test_restoring_only_a_copy_brings_its_dismissed_head_back_with_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="restore-copy")
    head_id = _pending(durable_database, workspace_id, "Systems Steward")
    copy_id = _pending(durable_database, workspace_id, "Systems Steward", variant_of=head_id)
    service = DiscoveryDispositionService(durable_database)
    assert _decide(service, workspace_id, "dismiss", [head_id]) == sorted([head_id, copy_id])

    assert _decide(service, workspace_id, "restore", [copy_id]) == sorted([head_id, copy_id])

    assert _queued_ids(durable_database, workspace_id) == [head_id]


def test_restoring_a_dismissed_head_leaves_a_copy_with_no_dismissal_of_its_own_alone(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database, slug="restore-family")
    head_id = _pending(durable_database, workspace_id, "Systems Steward")
    service = DiscoveryDispositionService(durable_database)
    _decide(service, workspace_id, "dismiss", [head_id])
    copy_id = _pending(durable_database, workspace_id, "Systems Steward", variant_of=head_id)

    assert _decide(service, workspace_id, "restore", [head_id]) == [head_id]

    assert service.history(workspace_id, copy_id) == []
    assert _queued_ids(durable_database, workspace_id) == [head_id]


def test_travel_dismissal_is_recorded_under_its_own_category(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="travel", name="Travel")
        .id
    )
    service = DiscoveryDispositionService(durable_database)
    travel_id = _pending(durable_database, workspace_id, "Field Systems Steward")
    other_id = _pending(durable_database, workspace_id, "Systems Steward")
    for pending_id, payload in (
        (travel_id, {"category": "out_of_scope_travel"}),
        (other_id, {"category": "other", "reason": "Dismissed for an unrelated reason"}),
    ):
        selection = DispositionSelection.model_validate(
            {
                "action": "dismiss",
                "selection_mode": "explicit",
                "pending_discovery_ids": [pending_id],
                **payload,
            }
        )
        preview = service.preview(workspace_id, selection)
        service.apply(
            workspace_id,
            DispositionApply.model_validate(
                {**selection.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
            ),
            actor="local_user",
            origin="test",
            correlation_id=pending_id,
        )

    dismissed = service.dismissed(workspace_id)

    assert Counter(item.category for item in dismissed) == {"out_of_scope_travel": 1, "other": 1}
    travel = next(item for item in dismissed if item.pending_discovery_id == travel_id)
    assert travel.reason == "The travel requirement is outside the current search scope."


def test_latest_disposition_query_does_not_confuse_a_long_supersession_chain(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="long-chain", name="Long chain")
        .id
    )
    pending_id = _pending(durable_database, workspace_id, "Systems Steward")
    previous: str | None = None
    with durable_database.session() as session:
        for index in range(51):
            event_id = new_id()
            session.add(
                PendingDiscoveryDispositionEvent(
                    id=event_id,
                    workspace_id=workspace_id,
                    pending_discovery_id=pending_id,
                    action="dismiss" if index % 2 == 0 else "restore",
                    restore_provenance=None if index % 2 == 0 else "person",
                    category="human_review",
                    reason="Synthetic reversible decision",
                    selection_mode="explicit",
                    snapshot_sha256=f"{index:064x}",
                    screening={},
                    supersedes_event_id=previous,
                    created_by="local_user",
                    origin="test",
                )
            )
            previous = event_id

    dismissed = DiscoveryDispositionService(durable_database).dismissed(workspace_id)

    assert [event.pending_discovery_id for event in dismissed] == [pending_id]


def _employer_board(database: Database, workspace_id: str, name: str) -> str:
    source_id = new_id()
    with database.session() as session:
        session.add(
            Source(
                id=source_id,
                workspace_id=workspace_id,
                name=name,
                source_type="employer_board",
                url=f"https://{name}.example.test/careers",
                priority=1,
            )
        )
    return source_id


def _advertised(
    database: Database,
    workspace_id: str,
    *,
    employer: str,
    title: str,
    host: str,
    location: str = "Remote",
    requisition_id: str = "",
    body_digest: str = "",
    identity_trust: ListingIdentityTrust = ListingIdentityTrust.PUBLISHER,
    source_id: str | None = None,
    first_seen_on: date = date(2026, 1, 1),
    variant_of: str | None = None,
) -> str:
    """Stage one discovery advertised at a named host, with a retained body when asked."""
    pending_id = new_id()
    url = f"https://{host}/opening/{pending_id}"
    facts = StagedListingFacts(
        identity_trust=identity_trust,
        locations=PublishedFact(values=(location,), provenance="board", complete=False),
    )
    with database.session() as session:
        row = PendingDiscovery(
            id=pending_id,
            workspace_id=workspace_id,
            fingerprint=pending_id,
            employer=employer,
            title=title,
            location=location,
            staged_facts=facts.as_json(),
            requisition_id=requisition_id,
            discovery_url=url,
            canonical_url=url,
            reason="Synthetic scan",
            first_seen_on=first_seen_on,
            source_id=source_id,
            regional_variant_of=variant_of,
        )
        session.add(row)
        session.flush()
        if not body_digest:
            return pending_id
        artifact = Artifact(
            workspace_id=workspace_id,
            logical_type="posting.body.text",
            mime_type="text/plain",
            storage_uri=f"memory://{pending_id}",
            byte_size=9,
            sha256=body_digest,
        )
        session.add(artifact)
        session.flush()
        content = pending_posting_content(
            workspace_id,
            pending_id,
            url,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            artifact_id=artifact.id,
            extraction_method=PostingContentExtractionMethod.STATIC_HTML,
            content_sha256=body_digest,
        )
        session.add(content)
        session.flush()
        row.current_content_id = content.id
    return pending_id


def _already_evaluated(database: Database, workspace_id: str, pending_id: str) -> None:
    """Tie one discovery's URL to a job posting that already carries an evaluation."""
    with database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        employer = Employer(
            workspace_id=workspace_id,
            name=row.employer,
            normalized_name=normalize_text(row.employer),
        )
        session.add(employer)
        session.flush()
        posting = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title=row.title,
            normalized_title=normalize_text(row.title),
            first_seen_at=utc_now(),
        )
        session.add(posting)
        session.flush()
        session.add(
            PostingIdentifier(
                workspace_id=workspace_id,
                job_posting_id=posting.id,
                identifier_type="url",
                identifier_value=normalize_url(row.canonical_url),
            )
        )
        session.add(
            JobEvaluation(
                job_posting_id=posting.id,
                version_number=1,
                decision="apply_now",
                rationale="",
                scope_summary="",
                qualifications_summary="",
                payload_sha256="0" * 64,
                evaluated_at=utc_now(),
            )
        )


def _suggestions(
    database: Database, workspace_id: str
) -> dict[str, list[PendingDuplicateSuggestionResponse]]:
    """Return each queued row's duplicate suggestions as the queue states them."""
    page = SourceScanQueries(database).pending_discovery_page(workspace_id)
    return {
        item.id: list(item.duplicate_suggestions)
        for item in (*page.ready.items, *page.attention.items)
    }


def _suggested(database: Database, workspace_id: str) -> dict[str, list[tuple[str, str]]]:
    """Return each queued row's suggested duplicate ids and signals."""
    return {
        pending_id: [
            (suggestion.pending_discovery_id, str(suggestion.signal)) for suggestion in suggestions
        ]
        for pending_id, suggestions in _suggestions(database, workspace_id).items()
    }


def _keepers(database: Database, workspace_id: str) -> dict[str, list[tuple[str, str]]]:
    """Return the row each suggestion says to keep, and the rule that chose it."""
    return {
        pending_id: [
            (suggestion.keeper_pending_discovery_id, str(suggestion.keeper_basis))
            for suggestion in suggestions
        ]
        for pending_id, suggestions in _suggestions(database, workspace_id).items()
    }


def _queue_workspace(database: Database, slug: str) -> str:
    service = WorkspaceService(database, runtime_storage(database))
    return str(service.create(slug=slug, name=slug).id)


def test_one_retained_body_seen_at_two_hosts_suggests_a_cross_source_duplicate(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "body-digest")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Clinical Data Steward",
        host="jobs.globex.example.test",
        body_digest="b" * 64,
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Globex Health",
        title="Data Steward, Clinical",
        host="listings.aggregator.example.test",
        body_digest="b" * 64,
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == [(second, "retained_body_digest")]
    assert suggested[second] == [(first, "retained_body_digest")]


def test_one_retained_body_seen_twice_at_one_host_suggests_nothing(
    durable_database: Database,
) -> None:
    """Two openings on one board legitimately share a body; only a second host is evidence."""
    workspace_id = _queue_workspace(durable_database, "one-host")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Clinical Data Steward",
        host="jobs.globex.example.test",
        body_digest="c" * 64,
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Clinical Data Steward II",
        host="jobs.globex.example.test",
        body_digest="c" * 64,
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == []
    assert suggested[second] == []


def test_one_title_and_employer_at_two_hosts_suggests_a_duplicate(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "title-employer")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="platform  steward",
        host="listings.aggregator.example.test",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == [(second, "title_and_employer")]
    assert suggested[second] == [(first, "title_and_employer")]


def test_two_differing_requisitions_suppress_the_title_and_employer_suggestion(
    durable_database: Database,
) -> None:
    """The employer's own identifiers say these are two openings, not one advertised twice."""
    workspace_id = _queue_workspace(durable_database, "requisitions")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        requisition_id="REQ-4001",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        requisition_id="REQ-4002",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == []
    assert suggested[second] == []


def test_disjoint_places_suppress_the_title_and_employer_suggestion(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "places")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        location="Boston",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        location="Lisbon",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == []
    assert suggested[second] == []


@pytest.mark.parametrize(
    ("slug", "first_place", "second_place"),
    [
        ("country-article", "Remote, US", "Remote, The United States"),
        ("country-spelling", "Remote, USA", "Remote, United States"),
        ("state-spelling", "Cambridge, Massachusetts, United States", "Cambridge, MA, US"),
    ],
)
def test_two_wordings_of_one_place_keep_the_title_and_employer_suggestion(
    durable_database: Database, slug: str, first_place: str, second_place: str
) -> None:
    """Aggregators word one place differently, which is not evidence of two openings."""
    workspace_id = _queue_workspace(durable_database, slug)
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        location=first_place,
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        location=second_place,
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == [(second, "title_and_employer")]
    assert suggested[second] == [(first, "title_and_employer")]


@pytest.mark.parametrize(
    ("slug", "canadian_place", "united_states_place"),
    [
        ("markets-remote", "Remote, Canada", "Remote, United States"),
        ("markets-city", "Vancouver, British Columbia, Canada", "Vancouver, WA, United States"),
        ("markets-shared-city", "Cambridge, Ontario, Canada", "Cambridge, MA, USA"),
    ],
)
def test_two_markets_never_read_as_one_place(
    durable_database: Database, slug: str, canadian_place: str, united_states_place: str
) -> None:
    """Merging two markets is worse than missing a duplicate, so no place fold may reach across."""
    workspace_id = _queue_workspace(durable_database, slug)
    canadian = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        location=canadian_place,
    )
    united_states = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        location=united_states_place,
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[canadian] == []
    assert suggested[united_states] == []


def test_one_stated_requisition_at_one_host_suggests_a_title_and_employer_duplicate(
    durable_database: Database,
) -> None:
    """Only the publisher's identifier separates two openings on one host, and one row lacks it."""
    workspace_id = _queue_workspace(durable_database, "one-requisition")
    scanned = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        requisition_id="REQ-4001",
    )
    hand_added = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[scanned] == [(hand_added, "title_and_employer")]
    assert suggested[hand_added] == [(scanned, "title_and_employer")]


def test_two_stated_requisitions_at_one_host_suggest_nothing(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "two-requisitions")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        requisition_id="REQ-4001",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        requisition_id="REQ-4002",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == []
    assert suggested[second] == []


def test_no_stated_requisition_at_one_host_suggests_nothing(
    durable_database: Database,
) -> None:
    """With no identifier on either side the program cannot tell two markets from one opening."""
    workspace_id = _queue_workspace(durable_database, "no-requisition")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == []
    assert suggested[second] == []


def test_a_shared_title_and_place_beside_an_unverified_hint_carries_its_own_signal(
    durable_database: Database,
) -> None:
    """An aggregator's employer is unreliable, so this weakest signal is labeled apart."""
    workspace_id = _queue_workspace(durable_database, "hint")
    first = _advertised(
        durable_database,
        workspace_id,
        employer="Northwind Fabrication",
        title="Platform Steward",
        host="jobs.northwind.example.test",
        location="Boston",
    )
    second = _advertised(
        durable_database,
        workspace_id,
        employer="listings.aggregator.example.test",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        location="Boston",
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[first] == [(second, "title_and_place_hint")]
    assert suggested[second] == [(first, "title_and_place_hint")]


def test_two_guessed_employers_sharing_a_title_and_place_suggest_nothing(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "two-hints")
    for employer, host in (
        ("listings.aggregator.example.test", "listings.aggregator.example.test"),
        ("feed.aggregator.example.test", "feed.aggregator.example.test"),
    ):
        _advertised(
            durable_database,
            workspace_id,
            employer=employer,
            title="Platform Steward",
            host=host,
            location="Boston",
            identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
        )

    assert not any(_suggested(durable_database, workspace_id).values())


def test_a_regional_variant_is_never_suggested_as_a_duplicate(
    durable_database: Database,
) -> None:
    """A variant family already states that one opening is advertised in several markets."""
    workspace_id = _queue_workspace(durable_database, "variant")
    primary = _advertised(
        durable_database,
        workspace_id,
        employer="Aster Systems",
        title="Research Steward",
        host="jobs.aster.example.test",
    )
    _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        variant_of=primary,
    )
    listed = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
    )

    suggested = _suggested(durable_database, workspace_id)

    assert suggested[listed] == []


def test_the_keeper_is_the_employer_board_row_and_then_the_publisher_vouched_one(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "keeper")
    source_id = _employer_board(durable_database, workspace_id, "initech-careers")
    on_board = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        source_id=source_id,
    )
    unsourced = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
    )
    vouched = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Research Steward",
        host="jobs.globex.example.test",
    )
    hinted = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Research Steward",
        host="listings.aggregator.example.test",
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
    )
    keepers = _keepers(durable_database, workspace_id)

    assert keepers[unsourced] == [(on_board, "employer_board_source")]
    assert keepers[hinted] == [(vouched, "publisher_vouched")]


def test_copies_no_rule_separates_say_so_instead_of_naming_a_rule(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "keeper-tie")
    copies = sorted(
        _advertised(
            durable_database,
            workspace_id,
            employer="Initech",
            title="Platform Steward",
            host=host,
        )
        for host in ("jobs.initech.example.test", "listings.aggregator.example.test")
    )

    keepers = _keepers(durable_database, workspace_id)

    assert keepers[copies[0]] == [(copies[0], "identifier_order")]
    assert keepers[copies[1]] == [(copies[0], "identifier_order")]


def test_the_reason_is_the_rule_that_separates_the_keeper_from_that_row(
    durable_database: Database,
) -> None:
    workspace_id = _queue_workspace(durable_database, "keeper-reason")
    earliest = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        first_seen_on=date(2026, 1, 1),
    )
    later = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        first_seen_on=date(2026, 2, 1),
    )
    hinted = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="feed.aggregator.example.test",
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
        first_seen_on=date(2026, 1, 1),
    )

    keepers = _keepers(durable_database, workspace_id)

    assert set(keepers[hinted]) == {(earliest, "publisher_vouched")}
    assert set(keepers[later]) == {(earliest, "first_seen")}
    assert set(keepers[earliest]) == {(earliest, "first_seen")}


def test_a_row_is_only_ever_told_to_keep_a_row_a_signal_linked_to_it(
    durable_database: Database,
) -> None:
    """No signal is transitive, so a same-host pair the rules refused shares no keeper."""
    workspace_id = _queue_workspace(durable_database, "keeper-scope")
    source_id = _employer_board(durable_database, workspace_id, "initech-careers")
    on_board = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Platform Steward",
        host="jobs.initech.example.test",
        body_digest="1" * 64,
        source_id=source_id,
    )
    same_host = _advertised(
        durable_database,
        workspace_id,
        employer="Initech",
        title="Research Steward",
        host="jobs.initech.example.test",
        body_digest="1" * 64,
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
    )
    listed = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Data Steward",
        host="listings.aggregator.example.test",
        body_digest="1" * 64,
    )

    suggested = _suggested(durable_database, workspace_id)
    keepers = _keepers(durable_database, workspace_id)

    assert suggested[on_board] == [(listed, "retained_body_digest")]
    assert suggested[same_host] == [(listed, "retained_body_digest")]
    assert keepers[same_host] == [(listed, "publisher_vouched")]


def test_a_copy_already_evaluated_is_the_keeper_for_every_copy_linked_to_it(
    durable_database: Database,
) -> None:
    """Later copies of an opening must learn that a paid evaluation already answered it."""
    workspace_id = _queue_workspace(durable_database, "already-evaluated")
    paid = _advertised(
        durable_database,
        workspace_id,
        employer="Globex",
        title="Clinical Data Steward",
        host="jobs.globex.example.test",
        body_digest="2" * 64,
    )
    _already_evaluated(durable_database, workspace_id, paid)
    second = _advertised(
        durable_database,
        workspace_id,
        employer="Globex Health",
        title="Data Steward, Clinical",
        host="listings.aggregator.example.test",
        body_digest="2" * 64,
    )
    third = _advertised(
        durable_database,
        workspace_id,
        employer="Globex Health",
        title="Data Steward, Clinical",
        host="boards.example.test",
        body_digest="2" * 64,
    )

    suggested = _suggested(durable_database, workspace_id)
    keepers = _keepers(durable_database, workspace_id)

    assert sorted(other for other, _signal in suggested[second]) == sorted([paid, third])
    assert keepers[second] == [(paid, "already_evaluated")] * 2
    assert keepers[third] == [(paid, "already_evaluated")] * 2


def test_a_dismissed_duplicate_is_named_as_dismissed_and_is_never_the_keeper(
    durable_database: Database,
) -> None:
    """A dismissed row is still worth linking to, but the queue no longer holds it."""
    workspace_id = _queue_workspace(durable_database, "dismissed-duplicate")
    queued = _advertised(
        durable_database,
        workspace_id,
        employer="Initrode",
        title="Clinical Data Steward",
        host="listings.aggregator.example.test",
        body_digest="3" * 64,
        identity_trust=ListingIdentityTrust.UNVERIFIED_HINT,
    )
    gone = _advertised(
        durable_database,
        workspace_id,
        employer="Initrode",
        title="Clinical Data Steward",
        host="jobs.initrode.example.test",
        body_digest="3" * 64,
    )
    dismiss_pending_discovery(durable_database, workspace_id, gone)

    suggestions = _suggestions(durable_database, workspace_id)

    assert [
        (
            item.pending_discovery_id,
            item.dismissed,
            item.keeper_pending_discovery_id,
            str(item.keeper_basis),
        )
        for item in suggestions[queued]
    ] == [(gone, True, queued, "in_queue")]


def test_a_reviewed_row_is_kept_over_one_that_only_retained_a_body(
    durable_database: Database,
) -> None:
    """A paid relevance review is the work a duplicate must not throw away."""
    workspace_id = _queue_workspace(durable_database, "reviewed-keeper")
    reviewed = _advertised(
        durable_database,
        workspace_id,
        employer="Aster Systems",
        title="Platform Steward",
        host="jobs.aster.example.test",
    )
    record_relevance_review(durable_database, workspace_id, reviewed, body_sha256="4" * 64)
    with_body = _advertised(
        durable_database,
        workspace_id,
        employer="Aster Systems",
        title="Platform Steward",
        host="listings.aggregator.example.test",
        body_digest="4" * 64,
    )

    suggested = _suggested(durable_database, workspace_id)
    keepers = _keepers(durable_database, workspace_id)

    assert suggested[with_body] == [(reviewed, "title_and_employer")]
    assert keepers[with_body] == [(reviewed, "relevance_reviewed")]


def _us_search(database: Database, workspace_id: str) -> None:
    SearchParametersService(database).save(
        workspace_id,
        JobSearchParameters(
            seniority=Seniority(target_titles=titles("Data Platform Lead")),
            locations=[Location(key="us", country="United States")],
        ),
    )


def _greenhouse_listing(url: str, location: str) -> SourceListing:
    return SourceListing(
        employer="Acme",
        title="Data Platform Lead",
        url=url,
        location=location,
        facts=published_listing_facts("greenhouse", locations=location),
    )


def _outcomes_for(result: dict[str, Any], url: str) -> list[dict[str, Any]]:
    return [outcome for outcome in result["funnel"]["outcomes"] if outcome["url"] == url]


def test_a_complete_exhaustive_board_location_holds_out_before_any_body_read(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/1"
    boards = ScriptedBoards({"Acme": [_greenhouse_listing(url, "London, United Kingdom")]})

    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=boards,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    result = service.scan(workspace_id)

    assert service.list_pending_discoveries(workspace_id) == []
    [outcome] = _outcomes_for(result, url)
    assert outcome["reason"] == "hard_constraint_excluded"
    assert {finding["code"] for finding in outcome["screening_findings"]} == {"location_outside"}


def test_a_held_out_listing_already_pending_is_refreshed_and_keeps_its_history(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/known"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_greenhouse_listing(url, "London, United Kingdom")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [_greenhouse_listing(url, "Remote, United States")],
        date(2026, 8, 25),
        reason="Seeded.",
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending_id = session.scalars(select(PendingDiscovery.id)).one()
        dismissal = PendingDiscoveryDispositionEvent(
            id=new_id(),
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            action="dismiss",
            category="human_review",
            reason="Checking the team first.",
            selection_mode="explicit",
            snapshot_sha256="dismiss",
            created_by="local_user",
            origin="test",
        )
        session.add(dismissal)
        session.flush()
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="restore",
                restore_provenance="person",
                category="",
                reason="Reviewing this one myself.",
                selection_mode="explicit",
                snapshot_sha256="restore",
                supersedes_event_id=dismissal.id,
                created_by="local_user",
                origin="test",
            )
        )

    result = service.scan(workspace_id)

    with durable_database.session() as session:
        row = session.scalars(select(PendingDiscovery)).one()
        history = list(
            session.scalars(
                select(PendingDiscoveryDispositionEvent.action)
                .where(PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id)
                .order_by(PendingDiscoveryDispositionEvent.created_at)
            )
        )
    assert row.location == "London, United Kingdom"
    assert history == ["dismiss", "restore"]
    assert [outcome["reason"] for outcome in _outcomes_for(result, url)] == ["already_known"]
    with durable_database.session() as session:
        restored = session.scalars(
            select(PendingDiscoveryDispositionEvent)
            .where(PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id)
            .order_by(PendingDiscoveryDispositionEvent.created_at)
        ).all()[-1]
    assert restored.restore_provenance == "person"


def test_audit_restored_holdout_is_dismissed_again_when_the_board_confirms_it(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/audit-restored"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_greenhouse_listing(url, "London, United Kingdom")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [_greenhouse_listing(url, "Remote, United States")],
        date(2026, 8, 25),
        reason="Seeded.",
        source_id=source_id,
    )
    with durable_database.session() as session:
        pending_id = session.scalars(select(PendingDiscovery.id)).one()
        dismissal = PendingDiscoveryDispositionEvent(
            id=new_id(),
            workspace_id=workspace_id,
            pending_discovery_id=pending_id,
            action="dismiss",
            category="location_outside",
            reason="Earlier automatic dismissal.",
            selection_mode="automatic",
            snapshot_sha256="dismiss",
            created_by="worker",
            origin="source.scan.structured_holdout",
        )
        session.add(dismissal)
        session.flush()
        session.add(
            PendingDiscoveryDispositionEvent(
                id=new_id(),
                workspace_id=workspace_id,
                pending_discovery_id=pending_id,
                action="restore",
                restore_provenance="audit",
                category="location_outside",
                reason="A prior audit restored this row.",
                selection_mode="explicit",
                snapshot_sha256="restore",
                supersedes_event_id=dismissal.id,
                created_by="local_user",
                origin="api",
            )
        )

    result = service.scan(workspace_id)

    with durable_database.session() as session:
        events = session.scalars(
            select(PendingDiscoveryDispositionEvent)
            .where(PendingDiscoveryDispositionEvent.pending_discovery_id == pending_id)
            .order_by(PendingDiscoveryDispositionEvent.created_at)
        ).all()
    assert [(event.action, event.restore_provenance) for event in events] == [
        ("dismiss", None),
        ("restore", "audit"),
        ("dismiss", None),
    ]
    assert events[-1].category == "location_outside"
    assert [outcome["reason"] for outcome in _outcomes_for(result, url)] == [
        "hard_constraint_excluded"
    ]


def test_a_visible_known_holdout_without_a_restore_is_dismissed(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/no-restore"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_greenhouse_listing(url, "London, United Kingdom")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [_greenhouse_listing(url, "Remote, United States")],
        date(2026, 8, 25),
        reason="Seeded.",
        source_id=source_id,
    )

    result = service.scan(workspace_id)

    with durable_database.session() as session:
        [event] = session.scalars(select(PendingDiscoveryDispositionEvent)).all()
    assert (event.action, event.restore_provenance, event.category) == (
        "dismiss",
        None,
        "location_outside",
    )
    assert [outcome["reason"] for outcome in _outcomes_for(result, url)] == [
        "hard_constraint_excluded"
    ]


def test_a_standing_holdout_reads_as_held_out_on_every_later_scan(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    source_id = board_source(
        durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme"
    )
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/repeat-holdout"
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_greenhouse_listing(url, "London, United Kingdom")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [_greenhouse_listing(url, "Remote, United States")],
        date(2026, 8, 25),
        reason="Seeded.",
        source_id=source_id,
    )

    service.scan(workspace_id)
    second = service.scan(workspace_id)

    with durable_database.session() as session:
        actions = session.scalars(select(PendingDiscoveryDispositionEvent.action)).all()
    assert actions == ["dismiss"]
    assert [outcome["reason"] for outcome in _outcomes_for(second, url)] == [
        "hard_constraint_excluded"
    ]


def test_a_held_out_listing_for_a_tracked_job_still_refreshes_that_job(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = scan_workspace(durable_database)
    board_source(durable_database, workspace_id, "Acme", "https://boards.greenhouse.io/acme")
    _us_search(durable_database, workspace_id)
    url = "https://boards.greenhouse.io/acme/jobs/tracked"
    with durable_database.session() as session:
        employer = session.scalars(select(Employer)).one()
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Platform Lead",
            normalized_title="data platform lead",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        session.add(
            PostingUrl(
                job_posting_id=job.id,
                url_type="canonical",
                url=url,
                normalized_url=normalize_url(url),
                is_primary=True,
            )
        )
        job_id = job.id
    listing = _greenhouse_listing(url, "London, United Kingdom")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [listing]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    refreshed: list[tuple[str, SourceListing]] = []
    monkeypatch.setattr(
        service.staging,
        "refresh_tracked",
        lambda _workspace_id, items, _context=None, *, board_url="", require_source_read=None: (
            refreshed.extend(items)
        ),
    )

    result = service.scan(workspace_id)

    assert [job for job, _listing in refreshed] == [job_id]
    with durable_database.session() as session:
        assert session.scalars(select(PendingDiscovery)).all() == []
    assert [outcome["reason"] for outcome in _outcomes_for(result, url)] == ["already_known"]
