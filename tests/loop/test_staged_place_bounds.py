"""A long publisher place list is stored whole place by whole place."""

from __future__ import annotations

from sqlalchemy import select

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.source_reading import SourceListing
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PendingDiscovery
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher


def test_a_long_place_list_is_cut_between_places_not_inside_one(
    durable_database: Database,
) -> None:
    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="places", name="Places")
        .id
    )
    places = [f"Springfield {index}, Illinois, United States" for index in range(40)]

    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).stage_listings(
        workspace_id,
        [
            SourceListing(
                employer="Example Health",
                title="Clinical Data Analyst",
                url="https://careers.example-health.test/jobs/42",
                location="; ".join(places),
            )
        ],
        utc_now().date(),
        reason="Board scan matched the title filter.",
    )

    with durable_database.session() as session:
        stored = session.scalars(select(PendingDiscovery.location)).one()
    kept = stored.split("; ")
    assert len(stored) <= 500
    assert kept == places[: len(kept)]
    assert len("; ".join(places[: len(kept) + 1])) > 500
