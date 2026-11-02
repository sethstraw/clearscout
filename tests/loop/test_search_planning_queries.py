"""Characterize the durable reads that prepare a planned search."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.search_planning_queries import SearchPlanningQueries
from tailorcv.application.failures import ValidationError
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Source
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher


def test_registered_source_identity_includes_saved_and_resolved_roots(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="known-roots", name="Known roots"
    )
    saved = "https://careers.example.test/openings"
    resolved = "https://boards.greenhouse.io/example/jobs/123"
    SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    ).remember_board(workspace.id, "Example", saved, origin="test")
    with durable_database.session() as session:
        source = session.scalar(select(Source).where(Source.url == saved))
        assert source is not None
        source.resolved_url = resolved

    known = SearchPlanningQueries(
        durable_database, runtime_storage(durable_database)
    ).known_identities(workspace.id)

    assert known.source_roots == frozenset({saved, "greenhouse:example"})


def test_planning_without_saved_search_parameters_says_what_to_save(
    durable_database: Database, master_cv: Path
) -> None:
    storage = runtime_storage(durable_database)
    workspace = WorkspaceService(durable_database, storage).create(
        slug="no-parameters", name="No parameters"
    )
    MasterCvService(durable_database, storage).import_source(
        workspace_id=workspace.id, filename="CV_Master.md", content=master_cv.read_bytes()
    )
    queries = SearchPlanningQueries(durable_database, storage)

    with pytest.raises(
        ValidationError,
        match=(
            r"^Search planning needs saved search parameters for this workspace\. "
            r"Save them, then plan the search again\.$"
        ),
    ):
        queries.planning_inputs(workspace.id)
