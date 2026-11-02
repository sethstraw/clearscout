"""A source is registered, corrected, revalidated, and archived through its catalog."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.source_lifecycle import NAME_CORRECTED_ACTION, SourceCatalog
from tailorcv.application.discovery.source_reading import SourceRead
from tailorcv.application.failures import (
    CareerPageRefusedError,
    ConflictError,
    ValidationError,
)
from tailorcv.application.job_search.models import SourceTerms
from tailorcv.domain import SourceKind, SourceReadOutcome, new_id, utc_now
from tailorcv.domain.identity import normalize_text
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    Employer,
    JobPosting,
    PendingDiscovery,
    PostingIdentifier,
    Source,
    SourceScan,
)
from tailorcv.infrastructure.web_fetch.policy import RobotsDeniedError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedBoards, ScriptedPage, UnexpectedPostingFetcher
from tests.support.scenarios import (
    board_source,
    seed_pending_discovery,
)
from tests.support.scenarios import (
    scan_workspace as _workspace,
)
from tests.support.scenarios import (
    source_listing as _listing,
)
from tests.support.scenarios import (
    state_target_titles as _state_titles,
)


def test_a_board_added_by_hand_is_swept_like_a_discovered_one(
    durable_database: Database,
) -> None:
    """A source added by hand is scanned the same as one a search registered."""
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_listing("Data Platform Lead", "https://acme.test/1")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    added = service.add_source(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme", origin="test"
    )
    assert added.platform == "greenhouse"
    assert added.enabled is True
    assert added.last_scanned_on is None

    result = service.scan(workspace_id)
    assert [item["title"] for item in result["discoveries"]] == ["Data Platform Lead"]

    [listed] = service.list_sources(workspace_id).items
    assert (listed.last_status, listed.last_scanned_on) == ("completed", utc_now().date())


def test_a_board_nobody_can_read_is_still_kept_and_says_why(
    durable_database: Database,
) -> None:
    """An employer worth tracking is registered before an adapter exists for it."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    added = service.add_source(
        workspace_id, "Initech", "https://www.initech.test/careers/", origin="test"
    )
    assert added.platform is None

    service.scan(workspace_id)

    [listed] = service.list_sources(workspace_id).items
    assert listed.last_status == "incomplete"
    assert listed.last_notes != ""


def test_a_board_that_has_been_read_and_never_staged_a_row_says_so(
    durable_database: Database,
) -> None:
    """The sweep list separates a board that stages rows from one that never has."""
    workspace_id = _workspace(durable_database)
    _state_titles(durable_database, workspace_id, "data")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards(
            {
                "Acme": [_listing("Data Platform Lead", "https://acme.test/1")],
                "Initech": [],
            }
        ),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.add_source(workspace_id, "Acme", "https://boards.greenhouse.io/acme", origin="test")
    service.add_source(
        workspace_id, "Initech", "https://boards.greenhouse.io/initech", origin="test"
    )

    service.scan(workspace_id)

    listed = {item.name: item for item in service.list_sources(workspace_id).items}
    assert (listed["Acme"].scan_count, listed["Acme"].staged_count) == (1, 1)
    assert (listed["Initech"].scan_count, listed["Initech"].staged_count) == (1, 0)


def _recorded_scan(
    database: Database, source_id: str, on: date, raw_count: int, status: str = ""
) -> None:
    started = datetime.combine(on, datetime.min.time(), tzinfo=UTC)
    with database.session() as session:
        session.add(
            SourceScan(
                id=new_id(),
                source_id=source_id,
                scanned_on=on,
                query_label="title-filter",
                status=status or ("completed" if raw_count else "completed_empty"),
                read_outcome=SourceReadOutcome.PUBLISHER_INDEX,
                raw_count=raw_count,
                read_count=raw_count,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                scan_started_at=started,
                scan_completed_at=started,
                duration_ms=0,
            )
        )


def test_a_source_reports_when_its_board_first_was_read_and_last_listed_openings(
    durable_database: Database,
) -> None:
    """A board that never listed an opening is told apart from one the screen emptied."""
    workspace_id = _workspace(durable_database)
    unread = board_source(durable_database, workspace_id, "Acme", "https://jobs.example/acme")
    silent = board_source(durable_database, workspace_id, "Initech", "https://jobs.example/i")
    lapsed = board_source(durable_database, workspace_id, "Globex", "https://jobs.example/g")
    _recorded_scan(durable_database, silent, date(2026, 3, 2), 0)
    _recorded_scan(durable_database, silent, date(2026, 3, 9), 0)
    _recorded_scan(durable_database, lapsed, date(2026, 3, 1), 4)
    _recorded_scan(durable_database, lapsed, date(2026, 3, 5), 2)
    _recorded_scan(durable_database, lapsed, date(2026, 3, 12), 0)

    listed = {
        item.id: item for item in SourceCatalog(durable_database).list_sources(workspace_id).items
    }

    assert (listed[unread].first_read_on, listed[unread].last_listed_on) == (None, None)
    assert (listed[silent].first_read_on, listed[silent].last_listed_on) == (
        date(2026, 3, 2),
        None,
    )
    assert (listed[lapsed].first_read_on, listed[lapsed].last_listed_on) == (
        date(2026, 3, 1),
        date(2026, 3, 5),
    )


def test_only_completed_reads_count_toward_a_board_s_empty_history(
    durable_database: Database,
) -> None:
    """A failed, partial, or omitted scan is not evidence the board lists nothing."""
    workspace_id = _workspace(durable_database)
    broken = board_source(durable_database, workspace_id, "Acme", "https://jobs.example/acme")
    mixed = board_source(durable_database, workspace_id, "Initech", "https://jobs.example/i")
    partial = board_source(durable_database, workspace_id, "Globex", "https://jobs.example/g")
    stalled = board_source(durable_database, workspace_id, "Initrode", "https://jobs.example/s")
    skipped = board_source(durable_database, workspace_id, "Umbrella", "https://jobs.example/u")
    _recorded_scan(durable_database, skipped, date(2026, 3, 2), 0, "omitted")
    _recorded_scan(durable_database, broken, date(2026, 3, 2), 0, "failed")
    _recorded_scan(durable_database, broken, date(2026, 3, 9), 0, "failed")
    _recorded_scan(durable_database, mixed, date(2026, 3, 1), 0, "failed")
    _recorded_scan(durable_database, mixed, date(2026, 3, 4), 0)
    _recorded_scan(durable_database, mixed, date(2026, 3, 8), 0, "failed")
    _recorded_scan(durable_database, mixed, date(2026, 3, 11), 0)
    _recorded_scan(durable_database, partial, date(2026, 3, 3), 3, "incomplete")
    _recorded_scan(durable_database, stalled, date(2026, 3, 3), 0, "incomplete")
    _recorded_scan(durable_database, stalled, date(2026, 3, 6), 0, "omitted")

    listed = {
        item.id: item for item in SourceCatalog(durable_database).list_sources(workspace_id).items
    }

    def counts(source_id: str) -> tuple[object, ...]:
        item = listed[source_id]
        return (
            item.scan_count,
            item.read_scan_count,
            item.incomplete_scan_count,
            item.failed_scan_count,
            item.first_read_on,
            item.last_listed_on,
        )

    assert counts(broken) == (2, 0, 0, 2, None, None)
    assert counts(mixed) == (4, 2, 0, 2, date(2026, 3, 4), None)
    assert counts(partial) == (1, 0, 1, 0, None, date(2026, 3, 3))
    assert counts(stalled) == (2, 0, 1, 0, None, None)
    assert counts(skipped) == (1, 0, 0, 0, None, None)


def test_a_board_whose_reads_published_policy_refuses_reports_that_refusal(
    durable_database: Database,
) -> None:
    """A board the sweep cannot read under published policy names that outcome."""
    workspace_id = _workspace(durable_database)
    refused = SourceRead(
        listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.POLICY_REFUSED
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Initech": refused}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.add_source(
        workspace_id, "Initech", "https://boards.greenhouse.io/initech", origin="test"
    )

    service.scan(workspace_id)

    [listed] = service.list_sources(workspace_id).items
    assert listed.last_read_outcome is SourceReadOutcome.POLICY_REFUSED
    assert listed.staged_count == 0


def test_turning_a_board_off_stops_it_being_swept(durable_database: Database) -> None:
    """Enabled is the only thing that decides whether a board is read."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_listing("Data Platform Lead", "https://acme.test/1")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme", origin="test"
    )

    turned_off = service.update_source(workspace_id, added.id, enabled=False)
    assert turned_off.enabled is False

    assert service.scan(workspace_id)["sources_considered"] == 0

    service.update_source(workspace_id, added.id, enabled=True)
    assert service.scan(workspace_id)["sources_considered"] == 1


def test_correcting_a_boards_address_changes_the_platform_it_is_read_by(
    durable_database: Database,
) -> None:
    """A board registered at a careers page was readable at its own address all along."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Globex", "https://www.globex.test/careers/", origin="test"
    )
    assert added.platform is None

    corrected = service.update_source(
        workspace_id, added.id, url="https://job-boards.greenhouse.io/globex"
    )
    assert corrected.platform == "greenhouse"
    assert corrected.url == "https://job-boards.greenhouse.io/globex"

    [listed] = service.list_sources(workspace_id).items
    assert listed.url == "https://job-boards.greenhouse.io/globex"


def test_moving_a_resolved_board_to_another_platform_scans_the_new_board(
    durable_database: Database,
) -> None:
    """An edited address is resolved again, so the sweep never reads the old board."""
    workspace_id = _workspace(durable_database)
    board = (
        "https://recruiting.paylocity.com/recruiting/jobs/All/d887949e-3f67-43b9-9f6b-3371b3f9450e"
    )
    fetcher = ScriptedBoards({"Globex": []})
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=fetcher,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Globex", "https://boards.greenhouse.io/globex", origin="test"
    )
    assert added.platform == "greenhouse"

    moved = service.update_source(workspace_id, added.id, url=board)
    service.scan(workspace_id)

    assert moved.platform == "paylocity"
    [listed] = service.list_sources(workspace_id).items
    assert listed.platform == "paylocity"
    assert fetcher.requested_urls == [board]


def test_a_source_edit_to_a_refused_address_writes_nothing(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address whose page cannot be read is refused, as it is when a source is added."""
    workspace_id = _workspace(durable_database)
    page = ScriptedPage()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Globex", "https://boards.greenhouse.io/globex", origin="test"
    )

    def refuse(url: str, **_kwargs: object) -> str:
        raise RobotsDeniedError(url)

    monkeypatch.setattr(page, "get_text", refuse)

    with pytest.raises(CareerPageRefusedError):
        service.update_source(workspace_id, added.id, url="https://www.globex.test/careers/")

    [listed] = service.list_sources(workspace_id).items
    assert (listed.url, listed.platform) == (added.url, "greenhouse")


def test_an_address_edited_while_another_edit_reads_its_page_is_refused(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit that resolved against a stale address loses, so its platform never lands."""
    workspace_id = _workspace(durable_database)
    page = ScriptedPage('<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Jobs</a>')
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Globex", "https://boards.greenhouse.io/globex", origin="test"
    )
    read_page = page.get_text

    def edited_meanwhile(url: str, **kwargs: object) -> str:
        service.update_source(workspace_id, added.id, url="https://jobs.lever.co/globex")
        return read_page(url)

    monkeypatch.setattr(page, "get_text", edited_meanwhile)

    with pytest.raises(ConflictError):
        service.update_source(workspace_id, added.id, url="https://www.globex.test/careers/")

    [listed] = service.list_sources(workspace_id).items
    assert (listed.url, listed.platform) == ("https://jobs.lever.co/globex", "lever")


def test_a_name_added_elsewhere_while_an_add_reads_its_page_is_refused(
    durable_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The add that resolved before another registered the name loses, leaving that source."""
    workspace_id = _workspace(durable_database)
    page = ScriptedPage()
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    read_page = page.get_text

    def added_meanwhile(url: str, **kwargs: object) -> str:
        monkeypatch.setattr(page, "get_text", read_page)
        service.add_source(
            workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
        )
        return read_page(url)

    monkeypatch.setattr(page, "get_text", added_meanwhile)

    with pytest.raises(ConflictError):
        service.add_source(
            workspace_id, "Globex", "https://www.globex.test/careers/", origin="test"
        )

    [listed] = service.list_sources(workspace_id).items
    assert (listed.url, listed.platform) == (
        "https://job-boards.greenhouse.io/globex",
        "greenhouse",
    )


def test_an_edit_that_keeps_the_address_reads_no_page(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    page = ScriptedPage(
        '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Acme", "https://careers.example.com/jobs", origin="test"
    )
    page.fetched.clear()

    kept = service.update_source(
        workspace_id, added.id, url="https://careers.example.com/jobs", priority=3
    )

    assert (kept.platform, kept.priority) == ("workday", 3)
    assert page.fetched == []


def test_revalidating_a_source_resolves_a_board_only_linked_from_its_page(
    durable_database: Database,
) -> None:
    """A board that was not on its page when saved resolves once revalidated."""
    workspace_id = _workspace(durable_database)
    page = ScriptedPage("no board yet")
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Acme", "https://careers.example.com/jobs", origin="test"
    )
    assert added.platform is None

    page.page = '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
    resolved = service.resolve_platform(workspace_id, added.id)

    assert resolved.platform == "workday"
    [listed] = service.list_sources(workspace_id).items
    assert listed.platform == "workday"


def test_revalidating_an_unresolvable_address_returns_no_platform_without_raising(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage("no board here"),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Initech", "https://careers.example.com/initech", origin="test"
    )

    resolved = service.resolve_platform(workspace_id, added.id)

    assert resolved.platform is None


def test_previewing_an_address_resolves_a_board_without_saving_anything(
    durable_database: Database,
) -> None:
    """Preview answers before a source exists, so nothing is written to try it."""
    workspace_id = _workspace(durable_database)
    page = ScriptedPage(
        '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
    )
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=page,
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    preview = service.preview_url(workspace_id, "https://careers.example.com/jobs")

    assert preview.platform == "workday"
    assert service.list_sources(workspace_id).items == []


@pytest.mark.parametrize(
    ("address", "note"),
    [
        (
            "https://acme.applytojob.com/apply",
            "Recognized JazzHR board. Scan results show actual coverage and any publisher limits.",
        ),
        (
            "https://boards.greenhouse.io/acme",
            "Recognized Greenhouse board. "
            "Scan results show actual coverage and any publisher limits.",
        ),
    ],
)
def test_a_preview_names_the_platform_as_people_write_it(
    durable_database: Database, address: str, note: str
) -> None:
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    assert service.preview_url(workspace_id, address).note == note


def test_unrecognized_source_preview_explains_partial_reads_and_individual_intake(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    preview = service.preview_url(workspace_id, "https://example.test/careers")

    assert preview.platform is None
    assert "complete board coverage is not established" in preview.note
    assert "individual posting links or paste posting text" in preview.note
    assert service.list_sources(workspace_id).items == []


@pytest.mark.parametrize(
    "host", ["recruiting.ultipro.com", "recruiting2.ultipro.ca", "acme.rec.pro.ukg.net"]
)
def test_a_ukg_preview_explains_individual_posting_coverage(
    durable_database: Database, host: str
) -> None:
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    preview = service.preview_url(
        workspace_id,
        f"https://{host}/GLOB1000/JobBoard/11111111-1111-1111-1111-111111111111",
    )

    assert preview.platform is None
    assert preview.note == (
        "UKG individual posting pages are supported, but a full board listing is not available. "
        "Add individual posting links to read these openings."
    )
    assert service.list_sources(workspace_id).items == []


def test_a_sources_stated_condition_travels_with_it(durable_database: Database) -> None:
    """Permission to read a source is conditional, so the condition is stored beside it."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    added = service.add_source(
        workspace_id,
        "Openings Digest",
        "https://digest.test/jobs",
        source_type=SourceKind.AGGREGATOR,
        origin="test",
        terms=SourceTerms(
            url="https://digest.test/terms",
            condition="Credit the digest and do not republish its listings.",
        ),
    )
    assert added.terms.condition == "Credit the digest and do not republish its listings."

    [listed] = service.list_sources(workspace_id).items
    assert listed.terms.url == "https://digest.test/terms"

    revised = service.update_source(
        workspace_id,
        added.id,
        terms=SourceTerms(url="https://digest.test/terms", condition="Link back to each posting."),
    )
    assert revised.terms.condition == "Link back to each posting."

    unrelated = service.update_source(workspace_id, added.id, priority=4)
    assert unrelated.terms.condition == "Link back to each posting."


def test_an_archived_board_leaves_the_list_and_is_not_swept(
    durable_database: Database,
) -> None:
    """Archiving keeps the scan history a source has already written."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedBoards({"Acme": [_listing("Data Platform Lead", "https://acme.test/1")]}),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme", origin="test"
    )
    service.scan(workspace_id)

    service.archive_source(workspace_id, added.id)

    assert service.list_sources(workspace_id).items == []
    assert service.scan(workspace_id)["sources_considered"] == 0
    assert len(service.list_scan_history(workspace_id).items) == 1


def test_adding_a_source_by_the_retired_embed_url_persists_the_canonical_root(
    durable_database: Database,
) -> None:
    """The old Greenhouse embed shape saves as the modern tenant URL, ready to scan."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    added = service.add_source(
        workspace_id,
        "Acme",
        "https://boards.greenhouse.io/embed/job_board/js?for=acme",
        origin="test",
    )
    assert added.url == "https://job-boards.greenhouse.io/acme"
    assert added.platform == "greenhouse"


def test_updating_a_source_url_persists_the_canonical_root(
    durable_database: Database,
) -> None:
    """A user pasting a single-posting permalink is corrected to the board root."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id,
        "Beta",
        "https://job-boards.greenhouse.io/beta",
        origin="test",
    )
    assert added.url == "https://job-boards.greenhouse.io/beta"

    corrected = service.update_source(
        workspace_id,
        added.id,
        url="https://job-boards.greenhouse.io/beta/jobs/6009876004",
    )
    assert corrected.url == "https://job-boards.greenhouse.io/beta"
    assert corrected.platform == "greenhouse"


def test_updating_a_source_without_a_url_leaves_the_stored_url_untouched(
    durable_database: Database,
) -> None:
    """A PATCH that changes only enabled or priority never rewrites the stored URL.

    The canonicalizer runs symmetrically at add and update on the URL field, and
    only on the URL field. A caller that PATCHes another field must not see the
    stored address silently reduced under them.
    """
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    stored_url = "https://job-boards.greenhouse.io/beta/jobs/6009876004"
    added = service.add_source(
        workspace_id,
        "Beta",
        "https://job-boards.greenhouse.io/beta",
        origin="test",
    )
    with durable_database.session() as session:
        row = session.get(Source, added.id)
        assert row is not None
        row.url = stored_url
        session.commit()

    turned_off = service.update_source(workspace_id, added.id, enabled=False)
    assert turned_off.url == stored_url

    with durable_database.session() as session:
        row = session.get(Source, added.id)
        assert row is not None
        assert row.url == stored_url


def _catalog(durable_database: Database) -> SourceScanService:
    """Build a source catalog whose board reads never leave the test."""
    return SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def _rename_events(durable_database: Database) -> list[AuditEvent]:
    with durable_database.session() as session:
        return list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.action == NAME_CORRECTED_ACTION)
            ).all()
        )


def test_renaming_a_source_renames_the_employer_it_names_and_records_both_names(
    durable_database: Database,
) -> None:
    """A machine-registered slug is corrected on the source and its employer together."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "acme-corp", "https://job-boards.greenhouse.io/acme", origin="test"
    )

    renamed = service.update_source(workspace_id, added.id, name="Acme Corporation")

    assert renamed.name == "Acme Corporation"
    assert renamed.employer == "Acme Corporation"
    with durable_database.session() as session:
        employer = session.scalar(select(Employer).where(Employer.workspace_id == workspace_id))
        assert employer is not None
        assert employer.name == "Acme Corporation"
        assert employer.normalized_name == normalize_text("Acme Corporation")
    (event,) = _rename_events(durable_database)
    assert event.entity_id == added.id
    assert event.details == {"old": "acme-corp", "new": "Acme Corporation", "restated_pending": 0}


def _pending_employers(durable_database: Database, *ids: str) -> list[str]:
    with durable_database.session() as session:
        return [session.get_one(PendingDiscovery, item).employer for item in ids]


def test_renaming_a_source_restates_every_staged_row_under_the_old_employer_name(
    durable_database: Database,
) -> None:
    """Staged rows carry the name as text, so a correction must reach them too."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Northwindhealth", "https://jobs.lever.co/northwindhealth", origin="test"
    )
    other = service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )
    seen = datetime(2026, 8, 1, tzinfo=UTC)
    restated = [
        seed_pending_discovery(
            durable_database, workspace_id, employer=employer, created_at=seen, source_id=source_id
        )
        for employer, source_id in (
            ("Northwindhealth", added.id),
            ("northwindhealth", other.id),
            ("Northwindhealth", None),
        )
    ]
    untouched = seed_pending_discovery(
        durable_database, workspace_id, employer="Globex", created_at=seen, source_id=added.id
    )

    service.update_source(workspace_id, added.id, name="Northwind Health")

    assert _pending_employers(durable_database, *restated) == ["Northwind Health"] * 3
    assert _pending_employers(durable_database, untouched) == ["Globex"]
    (event,) = _rename_events(durable_database)
    assert event.details == {
        "old": "Northwindhealth",
        "new": "Northwind Health",
        "restated_pending": 3,
    }


def test_a_refused_rename_leaves_every_staged_row_alone(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Northwindhealth", "https://jobs.lever.co/northwindhealth", origin="test"
    )
    service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )
    staged = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwindhealth",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        source_id=added.id,
    )

    with pytest.raises(ConflictError):
        service.update_source(workspace_id, added.id, name="Globex")

    assert _pending_employers(durable_database, staged) == ["Northwindhealth"]


def test_renaming_an_aggregator_source_restates_no_staged_row(durable_database: Database) -> None:
    """An aggregator names no employer, so a row sharing its name is some employer's own."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id,
        "Example Works",
        "https://jobs.example.com/listings",
        source_type=SourceKind.AGGREGATOR,
        origin="test",
    )
    staged = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Example Works",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        source_id=added.id,
    )

    service.update_source(workspace_id, added.id, name="Example Works Listings")

    assert _pending_employers(durable_database, staged) == ["Example Works"]


def test_a_blank_or_unchanged_source_name_leaves_the_source_and_the_history_alone(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "acme-corp", "https://job-boards.greenhouse.io/acme", origin="test"
    )

    assert service.update_source(workspace_id, added.id, name="   ").name == "acme-corp"
    assert service.update_source(workspace_id, added.id, name="acme-corp").name == "acme-corp"

    assert _rename_events(durable_database) == []


def test_renaming_a_source_onto_a_name_another_source_holds_is_refused(
    durable_database: Database,
) -> None:
    """Two employers must stay two, so a collision is refused rather than merged."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "acme-corp", "https://job-boards.greenhouse.io/acme", origin="test"
    )
    service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )

    with pytest.raises(ConflictError) as refusal:
        service.update_source(workspace_id, added.id, name="Globex")

    assert str(refusal.value) == "Another source is already named Globex."
    named = {item.id: item.name for item in service.list_sources(workspace_id).items}
    assert named[added.id] == "acme-corp"
    assert _rename_events(durable_database) == []


def test_renaming_a_source_onto_a_retired_source_name_says_the_holder_is_archived(
    durable_database: Database,
) -> None:
    """A retired source still holds its name, so the refusal says where the name went."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "acme-corp", "https://job-boards.greenhouse.io/acme", origin="test"
    )
    retired = service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )
    service.archive_source(workspace_id, retired.id)

    with pytest.raises(ConflictError) as refusal:
        service.update_source(workspace_id, added.id, name="Globex")

    assert str(refusal.value) == "An archived source is already named Globex."


def test_remembering_a_guided_board_returns_the_source_holding_that_board_identity(
    durable_database: Database,
) -> None:
    """The same tenant spelled differently resolves to the source the catalog already holds."""
    workspace_id = _workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        fetcher=ScriptedPage(),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    added = service.add_source(
        workspace_id,
        "Acme",
        "https://job-boards.greenhouse.io/acme",
        origin="test",
    )

    with durable_database.session() as session:
        remembered = service.remember_guided_board_in_session(
            session,
            workspace_id,
            "Acme",
            "https://job-boards.greenhouse.io/Acme",
            enabled=False,
        )

    assert remembered == added.id
    assert [source.id for source in service.list_sources(workspace_id).items] == [added.id]


def _careers_page(durable_database: Database, workspace_id: str, name: str) -> str | None:
    with durable_database.session() as session:
        employer = session.scalar(
            select(Employer).where(
                Employer.workspace_id == workspace_id,
                Employer.normalized_name == normalize_text(name),
            )
        )
        assert employer is not None
        return employer.careers_url


def test_remembering_a_board_by_a_posting_keeps_the_posting_out_of_the_careers_page(
    durable_database: Database,
) -> None:
    """Both remember paths store the board root as the source and leave the careers page empty."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)

    remembered = service.remember_board(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme/jobs/100", origin="test"
    )
    with durable_database.session() as session:
        guided = service.remember_guided_board_in_session(
            session,
            workspace_id,
            "Globex",
            "https://jobs.lever.co/globex/8c2d6c1e-9f1a-4b0e-a1d2-3c4b5a6d7e8f",
            enabled=False,
        )

    sources = {item.id: item.url for item in service.list_sources(workspace_id).items}
    assert sources == {
        remembered: "https://job-boards.greenhouse.io/acme",
        guided: "https://jobs.lever.co/globex",
    }
    assert _careers_page(durable_database, workspace_id, "Acme") is None
    assert _careers_page(durable_database, workspace_id, "Globex") is None


def _stored_platforms(durable_database: Database, *ids: str) -> dict[str, str | None]:
    with durable_database.session() as session:
        return {
            source.id: source.resolved_platform
            for source in session.scalars(select(Source).where(Source.id.in_(ids)))
        }


def test_both_remember_paths_store_the_platform_their_board_address_names(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)

    remembered = service.remember_board(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme/jobs/100", origin="test"
    )
    with durable_database.session() as session:
        guided = service.remember_guided_board_in_session(
            session, workspace_id, "Globex", "https://jobs.lever.co/globex", enabled=False
        )
    assert guided is not None

    assert _stored_platforms(durable_database, remembered, guided) == {
        remembered: "greenhouse",
        guided: "lever",
    }


def test_a_hand_add_by_an_archived_guided_siblings_name_restores_that_sibling(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    service.remember_board(
        workspace_id, "Initrode", "https://boards.greenhouse.io/initrode", origin="test"
    )
    with durable_database.session() as session:
        guided = service.remember_guided_board_in_session(
            session, workspace_id, "Initrode", "https://jobs.lever.co/initrode", enabled=True
        )
    assert guided is not None
    service.archive_source(workspace_id, guided)

    added = service.add_source(
        workspace_id, "Initrode (guided 1)", "https://jobs.lever.co/initrode", origin="test"
    )

    assert (added.id, added.name, added.employer, added.platform) == (
        guided,
        "Initrode (guided 1)",
        "Initrode",
        "lever",
    )
    with durable_database.session() as session:
        employers = session.scalars(
            select(Employer.name).where(Employer.workspace_id == workspace_id)
        ).all()
    assert employers == ["Initrode"]


def test_a_restored_sibling_does_not_block_re_adding_its_archived_primary(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    primary = service.remember_board(
        workspace_id, "Initrode", "https://boards.greenhouse.io/initrode", origin="test"
    )
    with durable_database.session() as session:
        guided = service.remember_guided_board_in_session(
            session, workspace_id, "Initrode", "https://jobs.lever.co/initrode", enabled=True
        )
    assert guided is not None
    service.archive_source(workspace_id, primary)
    service.archive_source(workspace_id, guided)
    service.add_source(
        workspace_id, "Initrode (guided 1)", "https://jobs.lever.co/initrode", origin="test"
    )

    added = service.add_source(
        workspace_id, "Initrode", "https://boards.greenhouse.io/initrode", origin="test"
    )

    assert (added.id, added.name, added.platform) == (primary, "Initrode", "greenhouse")


def test_a_remembered_employer_site_stores_no_platform(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)

    remembered = service.remember_board(
        workspace_id, "Acme", "https://www.acme.test/careers/", origin="test"
    )

    assert _stored_platforms(durable_database, remembered) == {remembered: None}


def test_remembering_a_board_onto_a_saved_employer_site_stores_the_boards_platform(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    remembered = service.remember_board(
        workspace_id, "Acme", "https://www.acme.test/careers/", origin="test"
    )

    service.remember_board(workspace_id, "Acme", "https://jobs.ashbyhq.com/acme", origin="test")

    assert _stored_platforms(durable_database, remembered) == {remembered: "ashby"}


def test_remembering_an_employer_page_onto_an_unresolved_board_clears_its_platform(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    remembered = service.remember_board(
        workspace_id, "Acme", "https://job-boards.greenhouse.io/acme", origin="test"
    )

    service.remember_board(
        workspace_id, "Acme", "https://www.acme.test/careers/job?gh_jid=4567", origin="test"
    )

    assert _stored_platforms(durable_database, remembered) == {remembered: None}


def test_remembering_a_board_again_keeps_the_employers_careers_page(
    durable_database: Database,
) -> None:
    """A board address promoted later never replaces the employer's own site."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    service.add_source(workspace_id, "Acme", "https://www.acme.test/careers/", origin="test")

    service.remember_board(
        workspace_id, "Acme", "https://boards.greenhouse.io/acme/jobs/100", origin="test"
    )

    assert _careers_page(durable_database, workspace_id, "Acme") == (
        "https://www.acme.test/careers/"
    )


def test_correcting_a_source_address_leaves_the_careers_page_alone(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Globex", "https://www.globex.test/careers/", origin="test"
    )

    corrected = service.update_source(
        workspace_id, added.id, url="https://job-boards.greenhouse.io/globex"
    )

    assert corrected.careers_url == "https://www.globex.test/careers/"
    assert _careers_page(durable_database, workspace_id, "Globex") == (
        "https://www.globex.test/careers/"
    )


def test_a_source_edit_sets_the_company_careers_page_and_refuses_an_ats_address(
    durable_database: Database,
) -> None:
    """The careers page is the employer's own site; a board or posting address is refused."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )
    assert added.careers_url is None

    edited = service.update_source(
        workspace_id, added.id, careers_url=" https://www.globex.test/about/careers "
    )
    assert edited.careers_url == "https://www.globex.test/about/careers"
    assert edited.url == "https://job-boards.greenhouse.io/globex"

    with pytest.raises(ValidationError, match="job board or applicant tracking system"):
        service.update_source(
            workspace_id, added.id, careers_url="https://job-boards.greenhouse.io/globex/jobs/7"
        )
    assert _careers_page(durable_database, workspace_id, "Globex") == (
        "https://www.globex.test/about/careers"
    )


@pytest.mark.parametrize(
    "address",
    ["www.globex.test/about", "globex", "http://localhost/careers", "https://[globex"],
)
def test_a_source_edit_refuses_a_careers_page_research_could_not_read(
    durable_database: Database, address: str
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )

    with pytest.raises(ValidationError, match=r"^Enter the full address, starting with https://"):
        service.update_source(workspace_id, added.id, careers_url=address)

    assert _careers_page(durable_database, workspace_id, "Globex") is None


def test_an_empty_careers_page_clears_the_stored_one(durable_database: Database) -> None:
    """A wrong careers page makes research describe the wrong company, so it can be removed."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Globex", "https://www.globex.test/careers/", origin="test"
    )
    assert added.careers_url == "https://www.globex.test/careers/"

    cleared = service.update_source(workspace_id, added.id, careers_url="  ")

    assert cleared.careers_url is None
    assert _careers_page(durable_database, workspace_id, "Globex") is None


def test_an_aggregator_has_no_careers_page_to_set(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id,
        "Openings Digest",
        "https://digest.test/jobs",
        source_type=SourceKind.AGGREGATOR,
        origin="test",
    )

    with pytest.raises(ValidationError, match="many employers"):
        service.update_source(workspace_id, added.id, careers_url="https://digest.test/")


_BOARD_UUID = "8c2d6c1e 9f1a 4b0e a1d2 3c4b5a6d7e8f"


def _requisition_keys(durable_database: Database, workspace_id: str, name: str) -> set[str]:
    """Record one ledger posting for the named employer and return its requisition keys."""
    with durable_database.session() as session:
        employer = session.scalar(
            select(Employer).where(
                Employer.workspace_id == workspace_id,
                Employer.normalized_name == normalize_text(name),
            )
        )
        assert employer is not None
        job = JobPosting(
            workspace_id=workspace_id,
            employer_id=employer.id,
            title="Data Platform Lead",
            normalized_title="data platform lead",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        for value in (f"{normalize_text(name)}|req 7", _BOARD_UUID):
            session.add(
                PostingIdentifier(
                    workspace_id=workspace_id,
                    job_posting_id=job.id,
                    identifier_type="requisition",
                    identifier_value=value,
                )
            )
        session.flush()
        return set(
            session.scalars(
                select(PostingIdentifier.identifier_value).where(
                    PostingIdentifier.job_posting_id == job.id
                )
            )
        )


def _stored_requisition_keys(durable_database: Database, workspace_id: str) -> set[str]:
    with durable_database.session() as session:
        return set(
            session.scalars(
                select(PostingIdentifier.identifier_value).where(
                    PostingIdentifier.workspace_id == workspace_id,
                    PostingIdentifier.identifier_type == "requisition",
                )
            )
        )


def test_renaming_a_source_restates_the_requisition_keys_its_employer_holds(
    durable_database: Database,
) -> None:
    """A requisition key embeds the employer name, so a rename must reach it too."""
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Northwindhealth", "https://jobs.lever.co/northwindhealth", origin="test"
    )
    assert _requisition_keys(durable_database, workspace_id, "Northwindhealth") == {
        "northwindhealth|req 7",
        _BOARD_UUID,
    }

    service.update_source(workspace_id, added.id, name="Northwind Health")

    assert _stored_requisition_keys(durable_database, workspace_id) == {
        "northwind health|req 7",
        _BOARD_UUID,
    }


def test_a_rename_onto_a_requisition_key_another_posting_holds_names_that_posting(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Northwindhealth", "https://jobs.lever.co/northwindhealth", origin="test"
    )
    _requisition_keys(durable_database, workspace_id, "Northwindhealth")
    service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )
    with durable_database.session() as session:
        globex = session.scalar(select(Employer).where(Employer.normalized_name == "globex"))
        assert globex is not None
        stale = JobPosting(
            workspace_id=workspace_id,
            employer_id=globex.id,
            title="Clinical Data Analyst",
            normalized_title="clinical data analyst",
            first_seen_at=utc_now(),
        )
        session.add(stale)
        session.flush()
        session.add(
            PostingIdentifier(
                workspace_id=workspace_id,
                job_posting_id=stale.id,
                identifier_type="requisition",
                identifier_value="northwind health|req 7",
            )
        )

    with pytest.raises(ConflictError) as refusal:
        service.update_source(workspace_id, added.id, name="Northwind Health")

    assert str(refusal.value) == (
        "Clinical Data Analyst at Globex already holds requisition req 7 under the name "
        "Northwind Health."
    )
    assert "northwindhealth|req 7" in _stored_requisition_keys(durable_database, workspace_id)


def test_remembering_a_board_keeps_a_hosted_posting_page_out_of_the_careers_page(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)

    service.remember_board(
        workspace_id, "Acme", "https://www.acme.test/careers/job?gh_jid=4567", origin="test"
    )

    assert _careers_page(durable_database, workspace_id, "Acme") is None


def test_a_rename_is_refused_while_a_task_targets_the_employers_staged_rows(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "Northwindhealth", "https://jobs.lever.co/northwindhealth", origin="test"
    )
    staged = seed_pending_discovery(
        durable_database,
        workspace_id,
        employer="Northwindhealth",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        source_id=added.id,
    )
    with durable_database.session() as session:
        session.add(
            BackgroundTask(
                workspace_id=workspace_id,
                task_type="discovery.evaluate",
                status="running",
                payload={"pending_discovery_id": staged, "job_posting_id": None},
                payload_sha256="0" * 64,
                idempotency_key="evaluate-staged",
            )
        )

    with pytest.raises(ConflictError, match="in flight"):
        service.update_source(workspace_id, added.id, name="Northwind Health")

    assert _pending_employers(durable_database, staged) == ["Northwindhealth"]
    named = {item.id: item.name for item in service.list_sources(workspace_id).items}
    assert named[added.id] == "Northwindhealth"


def test_a_rename_onto_a_name_another_employer_holds_names_that_employer(
    durable_database: Database,
) -> None:
    workspace_id = _workspace(durable_database)
    service = _catalog(durable_database)
    added = service.add_source(
        workspace_id, "acme-corp", "https://job-boards.greenhouse.io/acme", origin="test"
    )
    service.add_source(
        workspace_id, "Globex", "https://job-boards.greenhouse.io/globex", origin="test"
    )

    with pytest.raises(ConflictError) as refusal:
        service.update_source(workspace_id, added.id, name="globex")

    assert str(refusal.value) == "Another employer, Globex, already holds the name globex."
