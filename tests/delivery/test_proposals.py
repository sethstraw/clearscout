"""Discovery review and proposal routes: request validation and response shape."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tailorcv.application.discovery.scan_queries import (
    MAX_COLLATERAL_PHRASES,
    PENDING_PAGE_MAX_LIMIT,
)
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.bootstrap.api import create_app
from tailorcv.domain import SearchProposalState, new_id, utc_now
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.staged_facts import StagedListingFacts
from tailorcv.domain.statements import derived_proposal_identity
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    DerivedSearchTitle,
    MasterCvVersion,
    PendingDiscovery,
)
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import workspace_with_relevance_review


def test_pending_page_route_accepts_a_whole_lane_limit(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The page read is bounded well above one screen so "all" can mean the whole lane."""
    database = Database(durable_database_url)
    try:
        workspace_id = str(
            WorkspaceService(database, runtime_storage(database, tmp_path / "artifacts"))
            .create(slug="page-limit", name="Page limit")
            .id
        )
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page",
                params={"limit": PENDING_PAGE_MAX_LIMIT},
            )
            refused = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page",
                params={"limit": PENDING_PAGE_MAX_LIMIT + 1},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json()["ready"]["limit"] == PENDING_PAGE_MAX_LIMIT
    assert refused.status_code == 422


def test_pending_page_route_binds_repeated_employers_and_refuses_unknown_filters(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="queue-filters", name="Queue filters")
        .id
    )
    with database.session() as session:
        for index, employer in enumerate(("Globex", "Initech", "Hooli")):
            session.add(
                PendingDiscovery(
                    workspace_id=workspace_id,
                    fingerprint=f"{index + 200:064x}",
                    employer=employer,
                    title="Data Platform Lead",
                    discovery_url=f"https://jobs.example.test/{employer.lower()}/{index}",
                    reason="Publisher-listed role.",
                    first_seen_on=date(2026, 9, 1),
                )
            )
    endpoint = f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page"
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            narrowed = client.get(endpoint, params=[("employer", "Globex"), ("employer", "Hooli")])
            unknown = client.get(endpoint, params={"relevance": "bogus"})
            too_long = client.get(endpoint, params={"search": "x" * 201})
            clarification = client.get(endpoint, params={"constraint": "needs_clarification"})
            strict = client.get(endpoint, params={"constraint": "pursue_only"})
            readable_clarification = client.get(
                endpoint, params={"constraint": "needs_clarification", "readiness": "ready"}
            )
    finally:
        database.dispose()

    assert narrowed.status_code == 200
    body = narrowed.json()
    listed = body["ready"]["items"] + body["attention"]["items"]
    assert {item["employer"] for item in listed} == {"Globex", "Hooli"}
    assert body["queue_total"] == 3
    assert {entry["name"] for entry in body["employers"]} == {"Globex", "Initech", "Hooli"}
    assert unknown.status_code == 422
    assert too_long.status_code == 422
    assert (
        clarification.status_code == strict.status_code == readable_clarification.status_code == 200
    )
    assert clarification.json()["attention"]["total"] == 3
    assert strict.json()["ready"]["total"] + strict.json()["attention"]["total"] == 0
    assert readable_clarification.json()["attention"]["total"] == 0
    assert {item["value"]: item["count"] for item in clarification.json()["facets"]["constraint"]}[
        "needs_clarification"
    ] == 3


def test_closed_history_is_read_past_an_active_queue_larger_than_its_window(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="closed-history", name="Closed history")
        .id
    )
    with database.session() as session:
        for index in range(4):
            session.add(
                PendingDiscovery(
                    workspace_id=workspace_id,
                    fingerprint=f"{index:064x}",
                    employer="Globex",
                    title=f"Active Role {index}",
                    discovery_url=f"https://jobs.globex.example.test/{index}",
                    reason="Publisher-listed role.",
                    first_seen_on=date(2026, 9, 1),
                )
            )
        for index, closed_on in enumerate((date(2026, 9, 5), date(2026, 9, 9))):
            session.add(
                PendingDiscovery(
                    workspace_id=workspace_id,
                    fingerprint=f"{index + 100:064x}",
                    employer="Initrode",
                    title=f"Closed Role {index}",
                    discovery_url=f"https://jobs.initrode.example.test/{index}",
                    reason="Publisher-listed role.",
                    first_seen_on=date(2026, 9, 1),
                    withdrawn_at=datetime.combine(closed_on, datetime.min.time(), UTC),
                )
            )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            endpoint = f"/api/v1/workspaces/{workspace_id}/pending-discoveries"
            active = client.get(endpoint, params={"limit": 3})
            closed = client.get(endpoint, params={"limit": 3, "closed_only": True})
    finally:
        database.dispose()

    assert [item["employer"] for item in active.json()] == ["Globex"] * 3
    assert closed.status_code == 200
    assert [item["title"] for item in closed.json()] == ["Closed Role 1", "Closed Role 0"]


def test_pending_discovery_route_binds_both_readiness_cohort_limits(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The HTTP contract cannot accidentally request only one readiness lane."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="pending-cohorts", name="Pending cohorts")
        .id
    )
    with database.session() as session:
        session.add(
            PendingDiscovery(
                workspace_id=workspace_id,
                fingerprint="9" * 64,
                employer="Northwind Research",
                title="Platform Steward",
                discovery_url="https://jobs.example.test/opening/cohort",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 8, 28),
            )
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries",
                params={"ready_limit": 250, "attention_limit": 250},
            )
            incomplete = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries",
                params={"ready_limit": 250},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert [item["employer"] for item in response.json()] == ["Northwind Research"]
    assert incomplete.status_code == 422
    assert incomplete.json()["detail"] == (
        "Ready and attention limits must be supplied together for cohort paging."
    )


def test_title_exclusion_collateral_route_answers_per_phrase_and_bounds_the_list(
    durable_database_url: str, tmp_path: Path
) -> None:
    """One request reports a count per phrase, and an oversized list is refused."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="skip-collateral", name="Skip collateral")
        .id
    )
    with database.session() as session:
        session.add(
            PendingDiscovery(
                workspace_id=workspace_id,
                fingerprint="7" * 64,
                employer="Globex",
                title="Delivery Lead",
                discovery_url="https://jobs.example.test/opening/collateral",
                reason="Publisher-listed role.",
                first_seen_on=date(2026, 8, 28),
            )
        )
    endpoint = f"/api/v1/workspaces/{workspace_id}/pending-discoveries/title-exclusion-collateral"
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(endpoint, params={"phrase": ["Delivery Lead", "Archivist"]})
            refused = client.get(
                endpoint,
                params={"phrase": [f"Role {index}" for index in range(MAX_COLLATERAL_PHRASES + 1)]},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "queue_count": 1,
        "phrases": [
            {"phrase": "Delivery Lead", "matched_count": 1},
            {"phrase": "Archivist", "matched_count": 0},
        ],
    }
    assert refused.status_code == 422


def test_title_proposal_api_exposes_workspace_decisions_provenance_and_capacity(
    durable_database_url: str, tmp_path: Path
) -> None:
    """A CV refresh cannot hide an effective ruling that still shapes later searches."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="proposal-inbox", name="Proposal inbox")
        .id
    )
    try:
        with database.session() as session:
            profile = CandidateProfile(workspace_id=workspace_id, display_name="Candidate")
            session.add(profile)
            session.flush()
            old_version = MasterCvVersion(
                candidate_profile_id=profile.id,
                content_sha256="a" * 64,
                source_uri="file:///old-cv.md",
                effective_at=utc_now(),
                byte_size=1,
            )
            session.add(old_version)
            session.flush()
            normalized = normalize_text("Platform Lead")
            session.add(
                DerivedSearchTitle(
                    id=new_id(),
                    workspace_id=workspace_id,
                    master_cv_version_id=old_version.id,
                    title="Platform Lead",
                    normalized_title=normalized,
                    reason="The record supports platform leadership.",
                    evidence_ids=["entry-1"],
                    lane=None,
                    position=0,
                    state=SearchProposalState.ACCEPTED.value,
                    origin="search.vocabulary",
                    correlation_id="task-1",
                    carried_identity=derived_proposal_identity(normalized),
                    decided_at=utc_now(),
                )
            )
            current_version = MasterCvVersion(
                candidate_profile_id=profile.id,
                content_sha256="b" * 64,
                source_uri="file:///current-cv.md",
                effective_at=utc_now(),
                byte_size=1,
            )
            session.add(current_version)
            session.flush()
            old_version_id = str(old_version.id)

        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(f"/api/v1/workspaces/{workspace_id}/search-title-proposals")

        assert response.status_code == 200
        body = response.json()
        assert body["capacities"] == [
            {"kind": "title", "active_count": 0, "limit": 8, "remaining": 8}
        ]
        assert len(body["items"]) == 1
        proposal = body["items"][0]
        assert proposal["title"] == "Platform Lead"
        assert proposal["state"] == "accepted"
        assert proposal["origin"] == "search.vocabulary"
        assert proposal["correlation_id"] == "task-1"
        assert proposal["master_cv_version_id"] == old_version_id
        assert proposal["is_current_master_cv_version"] is False
        assert proposal["evidence_ids"] == ["entry-1"]
    finally:
        database.dispose()


def test_explicit_dismissal_api_exposes_and_enforces_typed_categories(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        schema = client.get("/api/v1/openapi.json").json()
        assert schema["components"]["schemas"]["ExplicitDismissalCategory"]["enum"] == [
            "closed_at_source",
            "out_of_scope_title",
            "out_of_scope_location",
            "out_of_scope_arrangement",
            "out_of_scope_travel",
            "out_of_scope_compensation",
            "invalid_page",
            "duplicate_pending",
            "duplicate_posting",
            "employer_excluded",
            "current_evidence_unavailable",
            "other",
        ]
        endpoint = "/api/v1/workspaces/missing/pending-dispositions/preview"
        common = {
            "action": "dismiss",
            "selection_mode": "explicit",
            "pending_discovery_ids": ["pending-1"],
        }
        unsupported = client.post(
            endpoint,
            json={**common, "category": "human_choice", "reason": "Free form"},
        )
        assert unsupported.status_code == 422
        missing_other_reason = client.post(endpoint, json={**common, "category": "other"})
        assert missing_other_reason.status_code == 422
        missing_pending_reference = client.post(
            endpoint, json={**common, "category": "duplicate_pending"}
        )
        assert missing_pending_reference.status_code == 422
        explicit_schema = schema["components"]["schemas"]["ExplicitDismissDispositionSelection"]
        assert "duplicate_of_pending_id" in explicit_schema["properties"]
        response_schema = schema["components"]["schemas"]["PendingDispositionResponse"]
        assert "duplicate_of_pending_id" in response_schema["properties"]


def test_pending_duplicate_candidates_include_active_roots(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="duplicate-candidates", name="Duplicate candidates")
        .id
    )
    pending_id = new_id()
    try:
        with database.session() as session:
            session.add(
                PendingDiscovery(
                    id=pending_id,
                    workspace_id=workspace_id,
                    fingerprint=pending_id,
                    employer="Northwind Research",
                    title="Data Steward",
                    location="Remote",
                    staged_facts=StagedListingFacts().as_json(),
                    discovery_url="https://jobs.example.test/data-steward",
                    canonical_url="https://jobs.example.test/data-steward",
                    reason="Synthetic candidate",
                    first_seen_on=date(2026, 1, 1),
                )
            )
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/pending-dispositions/duplicate-candidates"
            )

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": pending_id,
                "employer": "Northwind Research",
                "title": "Data Steward",
                "location": "Remote",
                "discovery_url": "https://jobs.example.test/data-steward",
                "dismissed": False,
            }
        ]
    finally:
        database.dispose()


def test_a_pending_discovery_title_is_corrected_over_http_without_dismiss(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH preserves address, provenance, and rejects empty or over-long titles."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="pending", name="Pending")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/pending-discoveries"
            add = client.post(
                base,
                json={"url": "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"},
            )
            assert add.status_code == 200, add.text
            assert add.json()["outcome"] == "created"
            pending = add.json()["pending_discovery"]
            pending_id = pending["id"]
            assert pending["title"] == ""
            assert pending["content_status"] == "not_yet_read"
            assert pending["content_note"] == ""
            assert pending["constraint_confidence"] == "needs_verification"
            page = client.get(f"{base}/page", params={"limit": 1})
            assert page.status_code == 200, page.text
            assert page.json()["ready"] == {
                "items": [],
                "total": 0,
                "offset": 0,
                "limit": 1,
                "has_more": False,
            }
            assert page.json()["attention"]["total"] == 1
            assert page.json()["attention"]["items"][0]["id"] == pending_id
            assert page.json()["attention"]["items"][0]["relevance_review"] is None

            corrected = client.patch(
                f"{base}/{pending_id}",
                json={"title": "Principal Data Product Manager"},
            )
            assert corrected.status_code == 200, corrected.text
            assert corrected.json()["title"] == "Principal Data Product Manager"
            assert (
                corrected.json()["discovery_url"]
                == "https://jobs.lever.co/globex/0b6f3c2e-5a41-4d8e-9c7a-2f1e8d4b6a10"
            )

            missing = client.patch(
                f"{base}/00000000-0000-0000-0000-000000000000",
                json={"title": "Nope"},
            )
            assert missing.status_code == 404

            empty = client.patch(f"{base}/{pending_id}", json={"title": ""})
            assert empty.status_code == 422

            too_long = client.patch(f"{base}/{pending_id}", json={"title": "A" * 501})
            assert too_long.status_code == 422
    finally:
        database.dispose()


def test_relevance_dispute_route_records_one_row_and_shows_it_on_the_discovery(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The dispute answers with what now stands, and the queue read carries it."""
    database = Database(durable_database_url)
    try:
        workspace_id, pending_id, review_id = workspace_with_relevance_review(
            database, tmp_path, slug="dispute-route"
        )
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            endpoint = f"/api/v1/workspaces/{workspace_id}/relevance-disputes/{pending_id}"
            disputed = client.put(
                endpoint,
                json={"relevance_review_id": review_id, "note": "The stated work matches my CV."},
            )
            page = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page")
            cleared = client.delete(endpoint)
            after = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page")
    finally:
        database.dispose()

    assert disputed.status_code == 200, disputed.text
    assert disputed.json()["relevance_review_id"] == review_id
    assert disputed.json()["disposition"] == "set_aside"
    assert disputed.json()["note"] == "The stated work matches my CV."
    review = page.json()["ready"]["items"][0]["relevance_review"]
    assert review["recommendation"] == "unlikely"
    assert review["disputed_at"] is not None
    assert review["dispute_note"] == "The stated work matches my CV."
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {"pending_discovery_id": pending_id, "cleared": True}
    assert after.json()["ready"]["items"][0]["relevance_review"]["disputed_at"] is None


def test_relevance_dispute_route_refuses_a_verdict_that_is_not_in_force(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    try:
        workspace_id, pending_id, _review_id = workspace_with_relevance_review(
            database, tmp_path, slug="dispute-route-stale"
        )
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            refused = client.put(
                f"/api/v1/workspaces/{workspace_id}/relevance-disputes/{pending_id}",
                json={"relevance_review_id": "not-the-current-review"},
            )
            unnamed = client.put(
                f"/api/v1/workspaces/{workspace_id}/relevance-disputes/{pending_id}",
                json={"note": "no review named"},
            )
    finally:
        database.dispose()

    assert refused.status_code == 409, refused.text
    assert unnamed.status_code == 422
