"""Learning-loop policy is versioned, editable, and refuses a stale edit."""

from __future__ import annotations

import pytest

from tailorcv.application.failures import ConflictError, NotFoundError
from tailorcv.application.workspaces import (
    LearningLoopPolicyService,
    WorkspaceService,
    WorkspaceSettingsService,
)
from tailorcv.domain.policy import LearningLoopPolicy
from tailorcv.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage


def test_learning_loop_policy_starts_empty_and_appends_on_save(
    durable_database: Database,
) -> None:
    """A workspace with no saved policy reads back the empty document at version zero."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="learn", name="Learn"
    )
    service = LearningLoopPolicyService(durable_database)

    described = service.describe(workspace.id)
    assert described.version == 0
    assert described.policy == LearningLoopPolicy()
    assert service.current(workspace.id) == LearningLoopPolicy()

    saved = service.save(workspace.id, LearningLoopPolicy(), expected_version=0)
    assert saved.version == 1
    assert saved.unchanged is False
    unchanged = service.save(workspace.id, LearningLoopPolicy(), expected_version=1)
    assert unchanged.version == 1
    assert unchanged.unchanged is True


def test_learning_loop_policy_refuses_a_stale_expected_version(
    durable_database: Database,
) -> None:
    """Two tabs cannot silently produce a last-writer result."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="stale", name="Stale"
    )
    service = LearningLoopPolicyService(durable_database)
    service.save(workspace.id, LearningLoopPolicy(), expected_version=0)

    with pytest.raises(ConflictError, match="version 1, not 0"):
        service.save(workspace.id, LearningLoopPolicy(), expected_version=0)


def test_learning_loop_policy_refuses_a_missing_workspace(
    durable_database: Database,
) -> None:
    with pytest.raises(NotFoundError, match="Workspace does not exist"):
        LearningLoopPolicyService(durable_database).describe("missing")


def test_saved_policy_reaches_the_settings_a_run_reads(
    durable_database: Database,
) -> None:
    """The composed settings snapshot reads the current policy version."""
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="reaches", name="Reaches"
    )
    LearningLoopPolicyService(durable_database).save(
        workspace.id, LearningLoopPolicy(), expected_version=0
    )

    settings = WorkspaceSettingsService(durable_database).current(workspace.id)

    assert settings.learning == LearningLoopPolicy()
