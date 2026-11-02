"""Create and list durable job-search workspaces."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from tailorcv.application.config import DEFAULT_PROVIDER, ProviderName, Settings
from tailorcv.application.failures import ConflictError, ValidationError
from tailorcv.application.workspaces import guardrails, parameters
from tailorcv.application.workspaces.layout import layout_for, stage_layout
from tailorcv.application.workspaces.models import WorkspaceResponse
from tailorcv.application.workspaces.records.access import audit_columns
from tailorcv.application.workspaces.settings import settings_seed
from tailorcv.domain import new_id, utc_now
from tailorcv.domain.identity import normalize_text
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database, register_session_finalizer
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    GuardrailVersion,
    JobSearchParameterSet,
    Workspace,
)


def _workspace(workspace: Workspace) -> dict[str, Any]:
    return {
        "id": workspace.id,
        "slug": workspace.slug,
        "name": workspace.name,
        "root_path": workspace.root_path,
        "provider": workspace.settings_seed.get("provider", DEFAULT_PROVIDER.value),
        "created_at": workspace.created_at,
    }


class WorkspaceService:
    """Create and list user-owned job-search workspaces."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        *,
        settings: Settings | None = None,
    ) -> None:
        """Use the supplied explicit transaction boundary."""
        self.database = database
        self.storage = storage
        self.settings = settings or Settings()

    def create(
        self,
        *,
        slug: str,
        name: str,
        root_path: str | None = None,
        provider: ProviderName | None = None,
        actor: str = "local_user",
        origin: str = "api",
        correlation_id: str | None = None,
    ) -> WorkspaceResponse:
        """Create one workspace, returning the existing match idempotently."""
        normalized_slug = normalize_text(slug).replace(" ", "-")
        if not normalized_slug or not name.strip():
            raise ValidationError("Workspace slug and name are required.")
        layout = layout_for(
            normalized_slug,
            root=root_path,
            storage=self.storage,
        )
        resolved_root = str(layout.root)
        seed_settings = (
            self.settings.model_copy(update={"provider": provider}) if provider else self.settings
        )
        with self.database.session() as session:
            existing = session.scalar(select(Workspace).where(Workspace.slug == normalized_slug))
            if existing is not None:
                if existing.name != name.strip() or existing.root_path != resolved_root:
                    raise ConflictError(
                        f"Workspace slug {normalized_slug!r} already names a different workspace."
                    )
                if (
                    provider is not None
                    and existing.settings_seed.get("provider") != provider.value
                ):
                    raise ConflictError(
                        f"Workspace slug {normalized_slug!r} already uses a different provider."
                    )
                if not layout.root.exists():
                    staged = stage_layout(layout, existing.id)
                    staged.finalize()
                return WorkspaceResponse.model_validate(_workspace(existing))
            workspace = Workspace(
                id=new_id(),
                slug=normalized_slug,
                name=name.strip(),
                root_path=resolved_root,
                settings_seed=settings_seed(seed_settings),
                **audit_columns(actor, origin, correlation_id),
            )
            staged = stage_layout(layout, workspace.id)
            register_session_finalizer(
                session,
                after_commit=staged.finalize,
                after_rollback=staged.discard,
            )
            session.add(workspace)
            session.flush()
            if self.settings.job_search.configured:
                session.add(
                    JobSearchParameterSet(
                        workspace_id=workspace.id,
                        version=1,
                        content_sha256=parameters.digest(self.settings.job_search),
                        effective_at=utc_now(),
                        document=self.settings.job_search.model_dump(mode="json"),
                        **audit_columns(actor, f"{origin}.seed", correlation_id),
                    )
                )
            if self.settings.rules.configured:
                session.add(
                    GuardrailVersion(
                        workspace_id=workspace.id,
                        version=1,
                        content_sha256=guardrails.digest(self.settings.rules),
                        effective_at=utc_now(),
                        document=guardrails.document_of(self.settings.rules),
                        **audit_columns(actor, f"{origin}.seed", correlation_id),
                    )
                )
            session.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    action="workspace.created",
                    entity_type="workspace",
                    entity_id=workspace.id,
                    details={"slug": workspace.slug},
                    **audit_columns(actor, origin, correlation_id),
                )
            )
            return WorkspaceResponse.model_validate(_workspace(workspace))

    def list(self) -> list[WorkspaceResponse]:
        """List active workspaces in stable name order."""
        with self.database.session() as session:
            rows = session.scalars(
                select(Workspace)
                .where(Workspace.archived_at.is_(None))
                .order_by(Workspace.name, Workspace.id)
            ).all()
            return [WorkspaceResponse.model_validate(_workspace(item)) for item in rows]
