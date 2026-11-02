"""The workspace check each scan read makes first."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tailorcv.application.failures import NotFoundError
from tailorcv.infrastructure.persistence.models import Workspace


def require_workspace_in(session: Session, workspace_id: str) -> None:
    """Raise NotFoundError unless the workspace exists in this session's database."""
    if session.get(Workspace, workspace_id) is None:
        raise NotFoundError(f"Unknown workspace {workspace_id!r}.")
