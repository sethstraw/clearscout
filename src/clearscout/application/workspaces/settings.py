"""Resolve the settings snapshot owned by one workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from clearscout.application.config import Settings, Stage
from clearscout.application.failures import NotFoundError
from clearscout.application.workspaces.guardrails import GuardrailService
from clearscout.application.workspaces.models import (
    EffectiveModelChoiceResponse,
    GuidedCampaignConfigurationResponse,
    RelevanceReviewConfigurationResponse,
    SearchVocabularyConfigurationResponse,
)
from clearscout.application.workspaces.parameters import (
    SearchParametersService,
    parameter_snapshot_in,
)
from clearscout.application.workspaces.policy import LearningLoopPolicyService
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Workspace

# Model and effort are a product decision this build owns, not workspace state.
# The field is dropped on write and on read; a workspace follows this build.
_UNSEEDED_FIELDS = {"models"}
_UNSTORED_FIELDS = {
    "cv",
    "output_dir",
    "source",
    "job_search",
    "rules",
    "learning",
} | _UNSEEDED_FIELDS


def settings_seed(settings: Settings) -> dict[str, Any]:
    """Return the path-free settings snapshot stored with a new workspace."""
    return settings.model_dump(mode="json", exclude=_UNSTORED_FIELDS)


def settings_seed_in(session: Session, workspace_id: str) -> Settings:
    """Read the seed and search parameters in the caller's transaction; the rest stay default."""
    workspace = session.get(Workspace, workspace_id)
    if workspace is None or workspace.archived_at is not None:
        raise NotFoundError(f"Workspace does not exist: {workspace_id}")
    seed = {
        name: value
        for name, value in workspace.settings_seed.items()
        if name not in _UNSEEDED_FIELDS
    }
    search = parameter_snapshot_in(session, workspace_id)
    return Settings.model_validate(seed).model_copy(update={"job_search": search.parameters})


@dataclass(frozen=True)
class WorkspaceSettingsSnapshot:
    """Composed settings coupled to the search parameters used to build them."""

    settings: Settings
    search_parameter_set_id: str | None
    search_parameter_version: int


class WorkspaceSettingsService:
    """Compose immutable seed settings with current versioned user parameters."""

    def __init__(self, database: Database) -> None:
        """Bind settings resolution to one durable database."""
        self.database = database
        self.search = SearchParametersService(database)
        self.guardrails = GuardrailService(database)
        self.learning = LearningLoopPolicyService(database)

    def current(self, workspace_id: str) -> Settings:
        """Return the database-owned settings currently in force."""
        return self.snapshot(workspace_id).settings

    def guided_campaign_configuration(
        self, workspace_id: str
    ) -> GuidedCampaignConfigurationResponse:
        """Return the effective choice one guided-campaign run will send."""
        settings = self.current(workspace_id)
        return GuidedCampaignConfigurationResponse(
            provider=settings.provider,
            model_choice=self._effective(settings, Stage.GUIDED_CAMPAIGN),
        )

    def search_vocabulary_configuration(
        self, workspace_id: str
    ) -> SearchVocabularyConfigurationResponse:
        """Return the effective choice one search-vocabulary run will send."""
        settings = self.current(workspace_id)
        return SearchVocabularyConfigurationResponse(
            provider=settings.provider,
            model_choice=self._effective(settings, Stage.SEARCH_VOCABULARY),
        )

    def relevance_review_configuration(
        self, workspace_id: str, batch_rows: int
    ) -> RelevanceReviewConfigurationResponse:
        """Return the effective choice one relevance-review run will send."""
        settings = self.current(workspace_id)
        return RelevanceReviewConfigurationResponse(
            provider=settings.provider,
            model_choice=self._effective(settings, Stage.RELEVANCE_REVIEW),
            batch_rows=batch_rows,
        )

    @staticmethod
    def _effective(settings: Settings, stage: Stage) -> EffectiveModelChoiceResponse:
        choice = settings.choice_for_stage(stage, settings.unattended_tier())
        return EffectiveModelChoiceResponse(
            model=choice.model,
            effort=choice.effort,
            service_tier=choice.service_tier,
        )

    def snapshot(self, workspace_id: str) -> WorkspaceSettingsSnapshot:
        """Return settings with the exact search-parameter identity in force."""
        with self.database.session() as session:
            seeded = settings_seed_in(session, workspace_id)
            search = parameter_snapshot_in(session, workspace_id)
        settings = seeded.model_copy(
            update={
                "rules": self.guardrails.current(workspace_id),
                "learning": self.learning.current(workspace_id),
            },
            deep=True,
        )
        return WorkspaceSettingsSnapshot(
            settings=settings,
            search_parameter_set_id=search.parameter_set_id,
            search_parameter_version=search.version,
        )
