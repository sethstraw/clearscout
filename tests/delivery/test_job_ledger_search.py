"""Searching the tracked-jobs ledger and narrowing it by employer over HTTP."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clearscout.bootstrap.api import create_app
from clearscout.domain import EvaluationDecision
from tests.support.scenarios import evaluation_commit


@pytest.fixture
def client(durable_database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as test_client:
        yield test_client


def _workspace(client: TestClient, slug: str) -> str:
    response = client.post("/api/v1/workspaces", json={"slug": slug, "name": slug.title()})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _track(client: TestClient, workspace_id: str, key: str, **changes: object) -> str:
    # The builder's rationale names data work, which would match every "data" search.
    changes.setdefault("rationale", "Synthetic rationale.")
    commit = evaluation_commit(
        workspace_id,
        requisition_id=f"REQ-{key}",
        canonical_url=f"https://example.test/jobs/{key}",
        discovery_url=f"https://example.test/jobs/{key}",
        **changes,
    )
    response = client.post("/api/v1/evaluations", json=commit.model_dump(mode="json"))
    assert response.status_code in {200, 201}, response.text
    return str(response.json()["job_posting_id"])


def _ledger(client: TestClient, workspace_id: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/jobs", params={"workspace_id": workspace_id, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _titles(page: dict[str, Any]) -> set[str]:
    return {item["title"] for item in page["items"]}


def test_search_finds_a_posting_by_its_stated_location(client: TestClient) -> None:
    workspace_id = _workspace(client, "location-search")
    _track(client, workspace_id, "lisbon", title="Data Analyst", location="Lisbon, Portugal")
    _track(client, workspace_id, "oslo", title="Data Engineer", location="Oslo, Norway")

    page = _ledger(client, workspace_id, search="LISBON")

    assert _titles(page) == {"Data Analyst"}
    assert page["total"] == 1


@pytest.mark.parametrize(
    ("stored", "search"),
    [("Zürich, Switzerland", "ZÜRICH"), ("ZÜRICH, SWITZERLAND", "zürich")],
)
def test_search_matches_an_accented_location_in_any_case(
    client: TestClient, stored: str, search: str
) -> None:
    workspace_id = _workspace(client, "accented-search")
    _track(client, workspace_id, "zurich", title="Data Analyst", location=stored)
    _track(client, workspace_id, "oslo", title="Data Engineer", location="Oslo, Norway")

    assert _titles(_ledger(client, workspace_id, search=search)) == {"Data Analyst"}


@pytest.mark.parametrize(
    ("search", "matched", "decoy"),
    [
        ("100%", "Analyst (100% remote)", "Analyst (100 days onsite)"),
        ("data_ops", "Engineer, data_ops", "Engineer, data-ops"),
    ],
)
def test_search_matches_like_wildcards_literally(
    client: TestClient, search: str, matched: str, decoy: str
) -> None:
    workspace_id = _workspace(client, "literal-search")
    _track(client, workspace_id, "matched", title=matched)
    _track(client, workspace_id, "decoy", title=decoy)

    assert _titles(_ledger(client, workspace_id, search=search)) == {matched}


def test_employer_selection_narrows_to_every_chosen_employer(client: TestClient) -> None:
    workspace_id = _workspace(client, "employer-filter")
    _track(client, workspace_id, "n1", employer="Northwind Health", title="Data Analyst")
    _track(client, workspace_id, "a1", employer="Acme Analytics", title="Data Scientist")
    _track(client, workspace_id, "g1", employer="Globex", title="Sales Director")

    page = _ledger(client, workspace_id, employer=["Northwind Health", "Globex"])

    assert _titles(page) == {"Data Analyst", "Sales Director"}
    assert page["total"] == 2


def test_employer_facet_counts_ignore_the_employer_selection_but_follow_other_filters(
    client: TestClient,
) -> None:
    workspace_id = _workspace(client, "employer-facet")
    _track(client, workspace_id, "n1", employer="Northwind Health", title="Data Analyst")
    _track(
        client,
        workspace_id,
        "n2",
        employer="Northwind Health",
        title="Data Engineer",
        decision=EvaluationDecision.MONITOR,
    )
    _track(client, workspace_id, "a1", employer="Acme Analytics", title="Data Scientist")
    _track(client, workspace_id, "g1", employer="Globex", title="Data Steward")
    _track(client, workspace_id, "g2", employer="Globex", title="Sales Director")
    _track(
        client,
        workspace_id,
        "g3",
        employer="Globex",
        title="Data Architect",
        posting_status="closed",
        decision=EvaluationDecision.CLOSED_EVIDENCE,
    )

    searched = _ledger(client, workspace_id, search="data", employer=["Acme Analytics"])
    assert _titles(searched) == {"Data Scientist"}
    assert searched["employers"] == [
        {"name": "Globex", "count": 2},
        {"name": "Northwind Health", "count": 2},
        {"name": "Acme Analytics", "count": 1},
    ]

    decided = _ledger(client, workspace_id, decision="apply_now", employer=["Globex"])
    assert _titles(decided) == {"Data Steward", "Sales Director"}
    assert decided["employers"] == [
        {"name": "Globex", "count": 2},
        {"name": "Acme Analytics", "count": 1},
        {"name": "Northwind Health", "count": 1},
    ]

    active = _ledger(client, workspace_id, search="data", posting_status="active")
    assert active["employers"] == [
        {"name": "Northwind Health", "count": 2},
        {"name": "Acme Analytics", "count": 1},
        {"name": "Globex", "count": 1},
    ]


def test_note_search_matches_like_wildcards_literally(client: TestClient) -> None:
    workspace_id = _workspace(client, "note-search")
    for title in ("Offer at 100% remote", "Offer at 100 days onsite"):
        response = client.post(
            "/api/v1/notes",
            json={
                "workspace_id": workspace_id,
                "entity_type": "workspace",
                "title": title,
                "body": "Synthetic note.",
            },
        )
        assert response.status_code == 201, response.text

    found = client.get("/api/v1/notes", params={"workspace_id": workspace_id, "search": "100%"})

    assert [note["title"] for note in found.json()] == ["Offer at 100% remote"]


@pytest.mark.parametrize(
    ("stored", "search"), [("Café interview", "CAFÉ"), ("CAFÉ INTERVIEW", "café")]
)
def test_note_search_matches_an_accented_title_in_any_case(
    client: TestClient, stored: str, search: str
) -> None:
    workspace_id = _workspace(client, "accented-note-search")
    for title in (stored, "Office interview"):
        response = client.post(
            "/api/v1/notes",
            json={
                "workspace_id": workspace_id,
                "entity_type": "workspace",
                "title": title,
                "body": "Synthetic note.",
            },
        )
        assert response.status_code == 201, response.text

    found = client.get("/api/v1/notes", params={"workspace_id": workspace_id, "search": search})

    assert [note["title"] for note in found.json()] == [stored]
