"""A workspace is claimed once and then reads its settings from the database, not a config file."""

from __future__ import annotations

from pathlib import Path

import pytest

from tailorcv.application.config import DEFAULT_PROVIDER, ProviderName, Settings
from tailorcv.application.failures import ConflictError
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.workspaces import (
    GuardrailService,
    SearchParametersService,
    WorkspaceService,
    WorkspaceSettingsService,
)
from tailorcv.domain.search_parameters import JobSearchParameters, Location
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Workspace
from tests.support.artifacts import runtime_storage


def test_workspace_settings_are_seeded_then_owned_by_the_database(
    durable_database: Database, tmp_path: Path
) -> None:
    seed = Settings(
        provider=ProviderName.ANTHROPIC,
        rules=Guardrails(forbidden=["fabricated metric"]),
        job_search=JobSearchParameters(locations=[Location(key="home", country="Atlantis")]),
    )
    root = tmp_path / "workspace"
    workspace = WorkspaceService(
        durable_database, runtime_storage(durable_database), settings=seed
    ).create(slug="authority", name="Authority", root_path=str(root))
    (root / "tailorcv.toml").write_text('[rules]\nforbidden = ["wrong source"]\n', encoding="utf-8")

    service = WorkspaceSettingsService(durable_database)
    effective = service.current(str(workspace.id))

    assert effective.provider is ProviderName.ANTHROPIC
    assert effective.rules.forbidden == ["fabricated metric"]
    assert effective.job_search.locations[0].key == "home"

    GuardrailService(durable_database).save(
        str(workspace.id),
        Guardrails(required=["exact authorization"]),
        expected_version=1,
    )
    SearchParametersService(durable_database).save(
        str(workspace.id),
        JobSearchParameters(locations=[Location(key="remote", country="Elsewhere")]),
        expected_version=1,
    )

    changed = service.current(str(workspace.id))
    assert changed.rules.required == ["exact authorization"]
    assert changed.job_search.locations[0].key == "remote"


def test_a_workspace_runs_the_effort_this_build_ships_not_the_one_it_was_made_with(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="stale-seed", name="Stale seed", root_path=str(tmp_path / "workspace")
    )
    with durable_database.session() as session:
        stored = session.get(Workspace, str(workspace.id))
        assert stored is not None
        assert "models" not in stored.settings_seed
        stored.settings_seed = {
            **stored.settings_seed,
            "models": {
                "openai": {
                    "extraction": {"model": "gpt-5.6-terra", "effort": "low"},
                    "drafting": {"model": "gpt-5.6-terra", "effort": "high"},
                }
            },
        }
        session.commit()

    effective = WorkspaceSettingsService(durable_database).current(str(workspace.id))

    shipped = Settings().tiers_for(ProviderName.OPENAI).drafting
    assert effective.tiers_for(ProviderName.OPENAI).drafting.effort is shipped.effort
    assert effective.choice_for_stage("draft").effort is shipped.effort


def test_empty_defaults_do_not_fabricate_parameter_history(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="empty-seed", name="Empty seed"
    )

    assert SearchParametersService(durable_database).describe(str(workspace.id)).version == 0
    assert GuardrailService(durable_database).describe(str(workspace.id)).version == 0


def test_learning_loop_policy_composes_into_the_settings_snapshot(
    durable_database: Database,
) -> None:
    """Every learning-loop workstream reads its knobs off the composed settings."""
    from tailorcv.application.workspaces import LearningLoopPolicyService
    from tailorcv.domain.policy import LearningLoopPolicy

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="learn-compose", name="Compose"
    )

    settings = WorkspaceSettingsService(durable_database).current(str(workspace.id))
    assert settings.learning == LearningLoopPolicy()

    LearningLoopPolicyService(durable_database).save(
        str(workspace.id), LearningLoopPolicy(), expected_version=0
    )

    settings = WorkspaceSettingsService(durable_database).current(str(workspace.id))
    assert settings.learning == LearningLoopPolicy()


def test_workspace_creation_can_choose_its_provider(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(
        durable_database,
        runtime_storage(durable_database),
        settings=Settings(provider=ProviderName.ANTHROPIC),
    ).create(
        slug="chosen-provider",
        name="Chosen provider",
        provider=ProviderName.OPENAI,
    )

    effective = WorkspaceSettingsService(durable_database).current(str(workspace.id))
    assert effective.provider is ProviderName.OPENAI


def test_a_workspace_storing_no_provider_is_listed_as_the_shipped_default(
    durable_database: Database,
) -> None:
    service = WorkspaceService(durable_database, runtime_storage(durable_database))
    workspace = service.create(slug="no-provider", name="No provider")
    with durable_database.session() as session:
        stored = session.get(Workspace, str(workspace.id))
        assert stored is not None
        stored.settings_seed = {}

    listed = next(item for item in service.list() if item.slug == "no-provider")
    assert listed.provider is DEFAULT_PROVIDER


def test_a_workspace_slug_is_normalized_and_claimed_by_one_workspace(
    durable_database: Database,
) -> None:
    workspaces = WorkspaceService(durable_database, runtime_storage(durable_database))
    workspace = workspaces.create(slug="  Career Search  ", name="Career Search")

    assert workspace.slug == "career-search"
    assert workspaces.create(slug="career search", name="Career Search").id == workspace.id
    with pytest.raises(ConflictError, match="different workspace"):
        workspaces.create(slug="career-search", name="Other")
