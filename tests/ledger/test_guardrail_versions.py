"""Workspace guardrails are validated, versioned, and immutable."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from clearscout.application.failures import ConflictError, NotFoundError
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.workspaces import GuardrailService, WorkspaceService
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import GuardrailVersion
from tests.support.artifacts import runtime_storage


def test_guardrail_versions_append_reuse_and_refuse_stale_edits(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="rules", name="Rules"
    )
    service = GuardrailService(durable_database)
    rules = Guardrails(
        forbidden=["enterprise MDM implementation"],
        distinctions=["Platform familiarity is not production experience."],
    )

    assert service.current(workspace.id) == Guardrails()
    first = service.save(workspace.id, rules, expected_version=0)
    unchanged = service.save(workspace.id, rules, expected_version=1)

    assert first.version == 1
    assert first.unchanged is False
    assert unchanged.id == first.id
    assert unchanged.unchanged is True
    assert service.current(workspace.id) == rules
    assert service.describe(workspace.id).rules == rules
    assert [item.version for item in service.history(workspace.id)] == [1]

    with pytest.raises(ConflictError, match="version 1, not 0"):
        service.save(
            workspace.id,
            rules.model_copy(update={"forbidden": ["another phrase"]}),
            expected_version=0,
        )

    def rewrite_saved_version() -> None:
        with durable_database.session() as session:
            record = session.get(GuardrailVersion, first.id)
            assert record is not None
            record.document = {}

    with pytest.raises(IntegrityError, match="guardrail_versions is immutable"):
        rewrite_saved_version()


def test_guardrail_empty_list_replaces_previous_phrases(
    durable_database: Database, tmp_path: Path
) -> None:
    """Clearing every phrase list saves an empty list rather than inheriting the prior one."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="clear", name="Clear"
    )
    service = GuardrailService(durable_database)

    first = service.save(
        workspace.id,
        Guardrails(
            forbidden=["world-class synergy"],
            protected=["Guarded phrase"],
            required=["Must appear"],
            distinctions=["Familiarity is not production experience."],
        ),
        expected_version=0,
    )
    assert first.version == 1

    cleared = Guardrails(work_authorization="US citizen")
    second = service.save(workspace.id, cleared, expected_version=1)

    assert second.version == 2
    assert second.rules.forbidden == []
    assert second.rules.protected == []
    assert second.rules.required == []
    assert second.rules.distinctions == []

    current = service.current(workspace.id)
    assert current.forbidden == []
    assert current.protected == []
    assert current.required == []
    assert current.distinctions == []
    assert current.work_authorization == "US citizen"

    described = service.describe(workspace.id)
    assert described.rules.forbidden == []


def test_guardrails_refuse_a_missing_workspace(durable_database: Database) -> None:
    with pytest.raises(NotFoundError, match="Workspace does not exist"):
        GuardrailService(durable_database).describe("missing")


def test_workspace_guardrails_are_versioned_over_http(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        created = client.post("/api/v1/workspaces", json={"slug": "api-rules", "name": "API rules"})
        assert created.status_code == 201, created.json()
        workspace_id = created.json()["id"]

        initial = client.get(f"/api/v1/workspaces/{workspace_id}/guardrails")
        assert initial.status_code == 200
        assert initial.json()["version"] == 0
        assert initial.json()["configured"] is False
        assert initial.json()["rules"]["forbidden"] == []

        saved = client.put(
            f"/api/v1/workspaces/{workspace_id}/guardrails",
            json={
                "rules": {
                    "forbidden": ["fabricated metric"],
                    "distinctions": ["Familiarity does not establish production use."],
                },
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["version"] == 1
        assert saved.json()["configured"] is True
        assert saved.json()["rules"]["forbidden"] == ["fabricated metric"]

        stale = client.put(
            f"/api/v1/workspaces/{workspace_id}/guardrails",
            json={"rules": {"forbidden": ["different"]}, "expected_version": 0},
        )
        assert stale.status_code == 409

        history = client.get(f"/api/v1/workspaces/{workspace_id}/guardrails/history")
        assert history.status_code == 200
        assert [item["version"] for item in history.json()] == [1]
        assert history.json()[0]["rules"]["forbidden"] == ["fabricated metric"]
