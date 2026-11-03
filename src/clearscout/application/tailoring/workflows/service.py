"""Persistence and safe-input support for durable workflows."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

from clearscout.application.documents import DocumentService
from clearscout.application.tailoring.workflows.approvals import (
    _approval_manifest as _approval_manifest_impl,
)
from clearscout.application.tailoring.workflows.batch_commands import (
    _persist_batch as _persist_batch_impl,
)
from clearscout.application.tailoring.workflows.context import _inputs as _inputs_impl
from clearscout.application.tailoring.workflows.context import _WorkflowInputs
from clearscout.application.tailoring.workflows.research import (
    _company_research as _company_research_impl,
)
from clearscout.application.tailoring.workflows.reuse import (
    _tailoring_preflight as _tailoring_preflight_impl,
)
from clearscout.application.tailoring.workflows.tailoring_commands import (
    _persist_tailoring as _persist_tailoring_impl,
)
from clearscout.application.workspaces import WorkspaceSettingsService
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.web_fetch.company_research import (
    CompanyResearchFetcher,
    ResearchSource,
)

from .models import BatchOutput as WorkflowBatchOutput
from .models import TailorOutput as WorkflowTailorOutput
from .models import WorkflowRunner as WorkflowRunnerProtocol
from .runner import PipelineWorkflowRunner as DefaultWorkflowRunner


class _ResearchFetcher(Protocol):
    def fetch(
        self, careers_url: str, *, posting_urls: Collection[str] = ()
    ) -> tuple[ResearchSource, ...]: ...


class WorkflowService:
    """Persistence and safe-input helpers shared by durable workflow handlers."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        runner: WorkflowRunnerProtocol | None = None,
        research_fetcher: _ResearchFetcher | None = None,
    ) -> None:
        """Configure private artifacts and the one existing-pipeline adapter."""
        self.database = database
        self.storage = storage
        self.root = storage.artifact_root
        self.documents = DocumentService(database, storage)
        self.settings = WorkspaceSettingsService(database)
        self.runner = runner or DefaultWorkflowRunner()
        self.research_fetcher = research_fetcher or CompanyResearchFetcher()

    def inputs(self, workspace_id: str, job_posting_id: str) -> _WorkflowInputs:
        """Load exact posting/CV bytes after workspace and readable-root checks."""
        return _inputs_impl(self, workspace_id, job_posting_id)

    def company_research(self, inputs: _WorkflowInputs) -> tuple[ResearchSource, ...]:
        """Combine the stored posting with bounded employer-owned web evidence."""
        return _company_research_impl(self, inputs)

    def tailoring_preflight(
        self,
        inputs: _WorkflowInputs,
        *,
        cover_letter: bool,
        approval: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Find an exact reusable package without research or provider calls."""
        return _tailoring_preflight_impl(self, inputs, cover_letter=cover_letter, approval=approval)

    def persist_tailoring(
        self,
        *,
        workspace_id: str,
        job_id: str,
        master_version_id: str,
        task_id: str,
        output: WorkflowTailorOutput,
        started_at: datetime,
        prompt_version_ids: tuple[str, ...] = (),
        research: tuple[ResearchSource, ...] = (),
    ) -> dict[str, Any]:
        """Persist one complete immutable run and append every output as a document version."""
        return _persist_tailoring_impl(
            self,
            workspace_id=workspace_id,
            job_id=job_id,
            master_version_id=master_version_id,
            task_id=task_id,
            output=output,
            started_at=started_at,
            prompt_version_ids=prompt_version_ids,
            research=research,
        )

    def persist_batch(
        self,
        *,
        workspace_id: str,
        job_ids: tuple[str, ...],
        task_id: str,
        output: WorkflowBatchOutput,
        priorities: tuple[int, ...],
        notes: tuple[str, ...],
        prompt_version_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Persist per-job reports and their immutable comparison membership."""
        return _persist_batch_impl(
            self,
            workspace_id=workspace_id,
            job_ids=job_ids,
            task_id=task_id,
            output=output,
            priorities=priorities,
            notes=notes,
            prompt_version_ids=prompt_version_ids,
        )

    def approval_manifest(self, workspace_id: str, tailoring_run_id: str) -> dict[str, Any]:
        """Rebuild the original review JSON with explicit stored decisions applied."""
        return _approval_manifest_impl(self, workspace_id, tailoring_run_id)
