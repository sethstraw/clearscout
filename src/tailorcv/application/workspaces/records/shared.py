"""The audit record every workspace-record write shares."""

from __future__ import annotations

from sqlalchemy.orm import Session

from tailorcv.application.workspaces.records.access import add_audit_event


def audit_user_action(
    session: Session,
    workspace_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    correlation_id: str | None,
) -> None:
    """Stage the standard audit record for an explicit workspace-record write."""
    add_audit_event(
        session,
        workspace_id=workspace_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details={"source": "explicit_user_action"},
        origin="explicit_user_action",
        correlation_id=correlation_id,
    )
