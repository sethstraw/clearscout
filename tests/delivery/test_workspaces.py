from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from clearscout.application.config import Effort, Settings, Stage
from clearscout.application.discovery.relevance_review import REVIEW_BATCH_ITEMS
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import BackgroundTask, PendingDiscovery
from tests.support.scenarios import (
    dismiss_pending_discovery,
    scan_workspace,
    seed_pending_discovery,
)

MASTER_CV = """\
# Jamie Q. Example

Springfield, Illinois | jamie.example@example.com | 555-0100

Authorized to work in Canada.

---

## Professional Experience

### Northwind Analytics

**Staff Systems Engineer**
March 2021 to Present | Springfield, Illinois (Remote)

- Rebuilt the ingestion tier on Kafka, cutting latency across every feed.
- Delivered the schema registry adopted by four downstream teams.
"""


def test_pending_closing_dates_can_be_set_reloaded_and_reset_without_paid_work(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Acme", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    route = f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/closing-date"
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.put(route, json={"closing_date_override": "2026-02-01"})
        assert response.status_code == 200
        assert response.json()["closing_date_override"] == "2026-02-01"
        reloaded = client.get(f"/api/v1/workspaces/{workspace_id}/pending-discoveries")
        assert reloaded.status_code == 200
        assert reloaded.json()[0]["closing_date_override"] == "2026-02-01"
        reset = client.put(route, json={"closing_date_override": None})
        assert reset.status_code == 200
        assert reset.json()["closing_date_override"] is None
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        assert row.closing_date_override is None
        assert row.closing_date_override_updated_at is not None
        assert row.withdrawn_at is None and row.archived_at is None
        assert session.scalar(select(func.count()).select_from(BackgroundTask)) == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"closing_date_override": "2026-02-30"},
        {"closing_date_override": "20260201"},
        {"closing_date_override": "2026-2-1"},
        {"closing_date_override": "2026-02-01T00:00:00Z"},
        {"closing_date_override": 1769904000},
        {"closing_date_override": True},
        {"closing_date_override": "soon"},
    ],
)
def test_pending_closing_date_edits_require_an_explicit_date_or_reset(
    durable_database: Database, durable_database_url: str, tmp_path: Path, body: dict[str, object]
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Acme", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.put(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/closing-date",
            json=body,
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:clearscout:problem:request-validation"
    assert response.json()["detail"]
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        assert row.closing_date_override is None
        assert row.closing_date_override_updated_at is None


@pytest.mark.parametrize("state", ["archived", "dismissed", "foreign", "archived_head"])
def test_pending_closing_date_edits_do_not_restore_or_cross_workspace_boundaries(
    durable_database: Database, durable_database_url: str, tmp_path: Path, state: str
) -> None:
    workspace_id = scan_workspace(durable_database)
    pending_id = seed_pending_discovery(
        durable_database, workspace_id, employer="Acme", created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    if state == "dismissed":
        dismiss_pending_discovery(durable_database, workspace_id, pending_id)
    elif state == "foreign":
        workspace_id = scan_workspace(durable_database, slug="other")
    else:
        with durable_database.session() as session:
            head = session.get_one(PendingDiscovery, pending_id)
            head.archived_at = datetime(2026, 1, 2, tzinfo=UTC)
        if state == "archived_head":
            copy_id = seed_pending_discovery(
                durable_database,
                workspace_id,
                employer="Northwind Fabrication",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            with durable_database.session() as session:
                session.get_one(PendingDiscovery, copy_id).regional_variant_of = pending_id
            pending_id = copy_id
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        response = client.put(
            f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/closing-date",
            json={"closing_date_override": "2026-02-01"},
        )
    assert response.status_code == 404
    assert response.json()["type"] == "urn:clearscout:problem:not-found"
    assert response.json()["detail"]
    with durable_database.session() as session:
        row = session.get_one(PendingDiscovery, pending_id)
        assert row.closing_date_override is None
        assert row.closing_date_override_updated_at is None


def test_guided_campaign_configuration_returns_the_effective_workspace_stage_choice(
    durable_database_url: str, tmp_path: Path
) -> None:
    settings = Settings()
    stages = {
        **settings.stages,
        Stage.GUIDED_CAMPAIGN: settings.stages[Stage.GUIDED_CAMPAIGN].model_copy(
            update={"effort": Effort.HIGH}
        ),
    }
    settings = settings.model_copy(update={"stages": stages})

    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            settings=settings,
        ),
        base_url="http://localhost",
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-choices", "name": "Campaign choices"}
        ).json()
        response = client.get(f"/api/v1/workspaces/{workspace['id']}/guided-campaign-configuration")

    assert response.status_code == 200
    assert response.json() == {
        "provider": "openai",
        "model_choice": {
            "model": "gpt-6.1-sol",
            "effort": "high",
            "service_tier": "flex",
        },
    }


def test_relevance_review_configuration_returns_the_effective_workspace_stage_choice(
    durable_database_url: str, tmp_path: Path
) -> None:
    settings = Settings()
    stages = {
        **settings.stages,
        Stage.RELEVANCE_REVIEW: settings.stages[Stage.RELEVANCE_REVIEW].model_copy(
            update={"effort": Effort.LOW}
        ),
    }
    settings = settings.model_copy(update={"stages": stages})

    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            settings=settings,
        ),
        base_url="http://localhost",
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "review-choices", "name": "Review choices"}
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{workspace['id']}/relevance-review-configuration"
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "openai",
        "model_choice": {
            "model": "gpt-6.1-sol",
            "effort": "low",
            "service_tier": "flex",
        },
        "batch_rows": REVIEW_BATCH_ITEMS,
    }


def test_search_vocabulary_configuration_returns_the_effective_workspace_stage_choice(
    durable_database_url: str, tmp_path: Path
) -> None:
    settings = Settings()
    stages = {
        **settings.stages,
        Stage.SEARCH_VOCABULARY: settings.stages[Stage.SEARCH_VOCABULARY].model_copy(
            update={"effort": Effort.MEDIUM}
        ),
    }
    settings = settings.model_copy(update={"stages": stages})

    with TestClient(
        create_app(
            durable_database_url,
            tmp_path / "artifacts",
            settings=settings,
        ),
        base_url="http://localhost",
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "vocabulary-choices", "name": "Vocabulary choices"}
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{workspace['id']}/search-vocabulary-configuration"
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "openai",
        "model_choice": {
            "model": "gpt-6.1-sol",
            "effort": "medium",
            "service_tier": "flex",
        },
    }


def test_guardrail_preview_endpoint_returns_matches_without_writing_a_new_version(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "preview", "name": "Preview"}
        ).json()
        workspace_id = workspace["id"]
        imported = client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv/import?filename=master.md",
            content=MASTER_CV.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
        )
        assert imported.status_code == 201

        before = client.get(f"/api/v1/workspaces/{workspace_id}/guardrails").json()

        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/guardrails/preview",
            json={
                "rules": {
                    "protected": ["Northwind Analytics"],
                    "required": ["Certified Kubernetes Administrator"],
                    "forbidden": ["Kafka"],
                    "work_authorization": "Authorized to work in the United States.",
                    "distinctions": [],
                    "first_person": False,
                    "ascii_only": True,
                },
                "expected_version": 0,
            },
        )

        after = client.get(f"/api/v1/workspaces/{workspace_id}/guardrails").json()
        history = client.get(f"/api/v1/workspaces/{workspace_id}/guardrails/history").json()

    assert response.status_code == 200
    body = response.json()
    assert body["forbidden"] == [
        {
            "phrase": "Kafka",
            "offsets": [MASTER_CV.index("Kafka")],
            "matched": True,
            "redraftable": True,
        }
    ]
    assert body["required"][0]["matched"] is False
    assert body["required"][0]["redraftable"] is False
    assert body["protected"][0]["matched"] is True

    precedence = body["work_authorization_precedence"]
    assert precedence["cv_line"] == "Authorized to work in Canada."
    assert precedence["drafting_line"] == "Authorized to work in the United States."
    assert precedence["effective"] == "Authorized to work in the United States."
    assert precedence["drops_cv_line"] is True

    # A preview reads the current master CV and rules; it writes no guardrail version.
    assert after == before
    assert history == []
