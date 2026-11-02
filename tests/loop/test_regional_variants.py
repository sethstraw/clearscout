"""One opening advertised in several regions stays one row to review."""

from __future__ import annotations

from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.pending import retire_pending
from tailorcv.application.discovery.source_reading import SourceListing
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import scan_workspace


def test_one_opening_advertised_in_four_regions_is_reviewed_once(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    places = ["Northtown", "Southport", "Eastport", "Westhaven"]
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex",
                "Data Platform Lead",
                f"https://jobs.globex.test/{index}",
                place,
                "REQ-1",
            )
            for index, place in enumerate(places)
        ],
        utc_now().date(),
        reason="Board scan.",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert [item.location for item in queue] == ["Northtown"]
    assert queue[0].also_in == ["Eastport", "Southport", "Westhaven"]
    assert service.queries.get_pending_discovery(workspace_id, queue[0].id) == queue[0]


def test_regional_variants_cannot_consume_the_pending_queue_read_bound(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex",
                "Data Platform Lead",
                f"https://jobs.globex.test/variant-{position}",
                f"Region {position}",
                "REQ-SHARED",
            )
            for position in range(501)
        ],
        utc_now().date(),
        reason="Board scan.",
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Initech",
                "Data Platform Lead",
                "https://jobs.initech.test/primary",
                "Northtown",
                "REQ-OTHER",
            )
        ],
        utc_now().date(),
        reason="Board scan.",
    )

    queue = service.list_pending_discoveries(workspace_id, limit=250)

    assert {item.employer for item in queue} == {"Globex", "Initech"}


def test_distinct_requisitions_with_one_title_remain_distinct_headcounts(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/1", "Northtown", "REQ-1"
            ),
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/2", "Southport", "REQ-2"
            ),
        ],
        utc_now().date(),
        reason="Board scan.",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert sorted(item.location for item in queue) == ["Northtown", "Southport"]
    assert all(item.also_in == [] for item in queue)


def test_similar_listings_without_a_shared_requisition_remain_separate(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/1", "Northtown"
            ),
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/2", "Southport"
            ),
        ],
        utc_now().date(),
        reason="Board scan.",
    )

    queue = service.list_pending_discoveries(workspace_id)

    assert sorted(item.location for item in queue) == ["Northtown", "Southport"]


def test_answering_a_discovery_answers_its_regional_variants(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    service.stage_listings(
        workspace_id,
        [
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/1", "Northtown", "REQ-1"
            ),
            SourceListing(
                "Globex", "Data Platform Lead", "https://jobs.globex.test/2", "Southport", "REQ-1"
            ),
        ],
        utc_now().date(),
        reason="Board scan.",
    )
    primary = service.list_pending_discoveries(workspace_id)[0]

    with durable_database.session() as session:
        answered = session.get(PendingDiscovery, primary.id)
        assert answered is not None
        retire_pending(session, answered)

    assert service.list_pending_discoveries(workspace_id) == []
    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all()
    assert all(item.archived_at is not None for item in staged)


def test_a_regional_variant_is_found_across_separate_staging_calls(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    today = utc_now().date()
    for position, location in enumerate(("Remote - US", "Remote or Winnipeg"), start=1):
        service.stage_listings(
            workspace_id,
            [
                SourceListing(
                    "Globex Care",
                    "Principal Product Manager",
                    f"https://jobs.globex.test/{position}",
                    location,
                    "REQ-1",
                )
            ],
            today,
            reason="Provider-hosted web search returned this public posting URL.",
        )

    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery)
            .where(PendingDiscovery.workspace_id == workspace_id)
            .order_by(PendingDiscovery.discovery_url)
        ).all()
        variants = [item.regional_variant_of for item in staged]

    assert len(staged) == 2
    assert variants[1] == staged[0].id, "the second market was not tied to the first"


def test_two_requisitions_in_the_same_office_remain_two_rows(
    durable_database: Database,
) -> None:
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    today = utc_now().date()
    for requisition in ("REQ-4471", "REQ-8802"):
        service.stage_listings(
            workspace_id,
            [
                SourceListing(
                    "Globex",
                    "Associate Director - Platform Strategy",
                    f"https://jobs.globex.test/{requisition}",
                    "Massachusetts - Boston",
                    requisition,
                )
            ],
            today,
            reason="Provider-hosted web search returned this public posting URL.",
        )

    queue = service.list_pending_discoveries(workspace_id)

    assert len(queue) == 2
    assert all(item.title == "Associate Director - Platform Strategy" for item in queue)
    assert all(item.location == "Massachusetts - Boston" for item in queue)
    assert all(item.also_in == [] for item in queue)


def test_a_pair_already_staged_is_tied_together_when_the_board_is_read_again(
    durable_database: Database,
) -> None:
    """A pair staged apart is tied together as one opening when the board is read again."""
    workspace_id = scan_workspace(durable_database)
    service = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    today = utc_now().date()
    listings = [
        SourceListing(
            "Northwind Care",
            "Principal Product Manager",
            f"https://jobs.northwind.test/{position}",
            location,
            "REQ-1",
        )
        for position, location in enumerate(("Remote - US", "Remote or Riverbend"), start=1)
    ]
    service.stage_listings(workspace_id, listings, today, reason="First read.")
    with durable_database.session() as session:
        for row in session.scalars(
            select(PendingDiscovery).where(PendingDiscovery.workspace_id == workspace_id)
        ).all():
            row.regional_variant_of = None
        session.commit()

    service.stage_listings(workspace_id, listings, today, reason="Second read.")

    with durable_database.session() as session:
        staged = session.scalars(
            select(PendingDiscovery)
            .where(PendingDiscovery.workspace_id == workspace_id)
            .order_by(PendingDiscovery.discovery_url)
        ).all()

    assert [item.regional_variant_of for item in staged][1] == staged[0].id
