"""The shipped aggregators arrive switched off, with the condition they are read under."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.starter_sources import StarterSourceInstaller
from clearscout.bootstrap.api import create_app
from clearscout.domain import SourceKind
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import scan_workspace


def test_every_shipped_source_is_an_aggregator_switched_off_stating_its_condition(
    durable_database: Database,
) -> None:
    """Nothing shipped names an employer, and nothing is read before it is agreed to."""
    workspace_id = scan_workspace(durable_database, slug="starter")
    sources = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    installed = StarterSourceInstaller(sources).install(workspace_id, origin="test")

    assert installed
    for source in installed:
        assert source.source_type is SourceKind.AGGREGATOR
        assert source.employer is None
        assert source.enabled is False
        assert source.terms.condition.strip()
        assert source.terms.url.startswith("https://")
    assert sources.scan(workspace_id)["sources_considered"] == 0


def test_installing_again_leaves_a_shipped_source_the_user_switched_on_alone(
    durable_database: Database,
) -> None:
    """A second install is not allowed to undo the decision to read a source."""
    workspace_id = scan_workspace(durable_database, slug="starter")
    sources = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    installer = StarterSourceInstaller(sources)
    [first, *_] = installer.install(workspace_id, origin="test")
    sources.update_source(workspace_id, first.id, enabled=True)

    assert installer.install(workspace_id, origin="test") == []

    still_on = next(
        item for item in sources.list_sources(workspace_id).items if item.id == first.id
    )
    assert still_on.enabled is True


def test_a_workspace_created_over_http_lists_the_shipped_aggregators(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The browser finds the starter list waiting in the sources panel."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "shipped", "name": "Shipped"}
        ).json()["id"]

        listed = client.get(f"/api/v1/workspaces/{workspace_id}/sources").json()

    assert listed["items"]
    assert {item["source_type"] for item in listed["items"]} == {"aggregator"}
    assert not any(item["enabled"] for item in listed["items"])
