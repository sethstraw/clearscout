"""Typed facade over master-CV intake, commands, artifacts, and queries."""

from __future__ import annotations

from collections.abc import Callable

from tailorcv.application.config import Settings
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.master_cv.artifacts import MasterCvArtifacts
from tailorcv.application.master_cv.commands import MasterCvVersionCommands
from tailorcv.application.master_cv.intake import MasterCvIntake
from tailorcv.application.master_cv.queries import MasterCvQueries
from tailorcv.application.workspaces import (
    MasterCvContentResponse,
    MasterCvEditResponse,
    MasterCvImpactResponse,
    MasterCvImportResponse,
    MasterCvVersionResponse,
    WorkspaceSettingsService,
)
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.providers.credentials import Prompter


class MasterCvService:
    """Expose master-CV intake, version commands, artifacts, and queries through one port."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        *,
        paid_work_ready: Callable[[], bool] | None = None,
    ) -> None:
        """Compose collaborators around one database and runtime storage root."""
        self.database = database
        self.storage = storage
        self.artifacts = EvidenceArtifactService(storage)
        self.settings = WorkspaceSettingsService(database)
        self.artifact_coordination = MasterCvArtifacts(self.artifacts)
        self.queries = MasterCvQueries(database, storage, self.artifact_coordination)
        self.intake = MasterCvIntake(
            database, storage, self.settings, paid_work_ready=paid_work_ready
        )
        self.commands = MasterCvVersionCommands(database, self.intake, self.queries)

    def versions(self, workspace_id: str) -> list[MasterCvVersionResponse]:
        """List immutable master-CV versions."""
        return self.queries.versions(workspace_id)

    def read_version(self, workspace_id: str, version_id: str = "") -> MasterCvContentResponse:
        """Read one version, defaulting to the current version."""
        return self.queries.read_version(workspace_id, version_id)

    def diff_versions(self, workspace_id: str, from_version: str, to_version: str) -> str:
        """Compare two stored versions."""
        return self.queries.diff_versions(workspace_id, from_version, to_version)

    def impact(self, workspace_id: str, content: str) -> MasterCvImpactResponse:
        """Report citation impact without writing."""
        return self.queries.impact(workspace_id, content)

    def save_edit(
        self,
        workspace_id: str,
        content: str,
        *,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> MasterCvEditResponse:
        """Append validated edited Markdown as an immutable version."""
        return self.commands.save_edit(
            workspace_id,
            content,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def import_source(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        settings: Settings | None = None,
        api_key: str | None = None,
        prompter: Prompter | None = None,
        replace: bool = False,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> MasterCvImportResponse:
        """Import source bytes through the single version-registration command."""
        return self.commands.import_source(
            workspace_id=workspace_id,
            filename=filename,
            content=content,
            mime_type=mime_type,
            settings=settings,
            api_key=api_key,
            prompter=prompter,
            replace=replace,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
        )
