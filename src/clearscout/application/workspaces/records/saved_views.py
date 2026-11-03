"""Saved table configurations for workspace records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.workspaces.records.access import require_workspace
from clearscout.application.workspaces.records.models import SavedViewResponse
from clearscout.application.workspaces.records.shared import audit_user_action
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import SavedView

_VIEW_RESOURCES = frozenset({"jobs"})


class SavedViewRecordsService:
    """Persist the filters-and-sort configurations a table can be restored to."""

    def __init__(self, database: Database) -> None:
        """Bind these records to one durable database."""
        self.database = database

    def list_saved_views(
        self, workspace_id: str, *, resource: str | None = None
    ) -> list[SavedViewResponse]:
        """List active per-table configurations."""
        if resource is not None and resource not in _VIEW_RESOURCES:
            raise ValidationError(f"Unsupported saved-view resource {resource!r}.")
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            query = select(SavedView).where(
                SavedView.workspace_id == workspace_id, SavedView.archived_at.is_(None)
            )
            if resource is not None:
                query = query.where(SavedView.resource == resource)
            views = session.scalars(query.order_by(SavedView.resource, SavedView.name)).all()
            return [_saved_view_record(view) for view in views]

    def save_view(
        self,
        *,
        workspace_id: str,
        view_id: str | None,
        resource: str,
        name: str,
        configuration: dict[str, Any],
        correlation_id: str | None = None,
    ) -> SavedViewResponse:
        """Create or update one explicit filters-and-sort configuration."""
        if resource not in _VIEW_RESOURCES:
            raise ValidationError(f"Unsupported saved-view resource {resource!r}.")
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 240:
            raise ValidationError("Saved-view name must contain 1 to 240 characters.")
        _configuration(configuration)
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            if view_id is None:
                view = SavedView(
                    workspace_id=workspace_id,
                    resource=resource,
                    name=clean_name,
                    configuration=configuration,
                    origin="user_saved_view",
                    correlation_id=correlation_id,
                )
                session.add(view)
                action = "saved_view.created"
            else:
                view = self._saved_view(session, view_id, workspace_id)
                view.resource = resource
                view.name = clean_name
                view.configuration = configuration
                action = "saved_view.updated"
            session.flush()
            audit_user_action(session, workspace_id, action, "saved_view", view.id, correlation_id)
            return _saved_view_record(view)

    def archive_saved_view(
        self, view_id: str, workspace_id: str, *, correlation_id: str | None = None
    ) -> SavedViewResponse:
        """Soft archive one saved view."""
        with self.database.session() as session:
            view = self._saved_view(session, view_id, workspace_id)
            view.archived_at = utc_now()
            audit_user_action(
                session, workspace_id, "saved_view.archived", "saved_view", view.id, correlation_id
            )
            return _saved_view_record(view)

    @staticmethod
    def _saved_view(session: Session, view_id: str, workspace_id: str) -> SavedView:
        view = session.get(SavedView, view_id)
        if view is None or view.workspace_id != workspace_id or view.archived_at is not None:
            raise NotFoundError(f"Saved view {view_id!r} does not exist in this workspace.")
        return view


def _saved_view_record(view: SavedView) -> SavedViewResponse:
    """Return one persisted table configuration."""
    return SavedViewResponse.model_validate(
        {
            "id": view.id,
            "workspace_id": view.workspace_id,
            "resource": view.resource,
            "name": view.name,
            "configuration": dict(view.configuration or {}),
            "created_at": view.created_at,
            "archived_at": view.archived_at,
        }
    )


def _configuration(configuration: dict[str, Any]) -> None:
    if set(configuration) != {"filters", "sort"}:
        raise ValidationError("Saved-view configuration must contain exactly filters and sort.")
    if not isinstance(configuration["filters"], (dict, list)):
        raise ValidationError("Saved-view filters must be an object or list.")
    if not isinstance(configuration["sort"], (dict, list)):
        raise ValidationError("Saved-view sort must be an object or list.")
