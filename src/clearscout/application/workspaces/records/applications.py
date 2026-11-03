"""Application records exposed as workspace attachment choices."""

from __future__ import annotations

from sqlalchemy import select

from clearscout.application.workspaces.records.access import require_workspace
from clearscout.application.workspaces.records.models import ApplicationOptionResponse
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Application, JobPosting


class ApplicationRecordsService:
    """Read applications as the choices an attachment form offers."""

    def __init__(self, database: Database) -> None:
        """Bind these records to one durable database."""
        self.database = database

    def list_applications(self, workspace_id: str) -> list[ApplicationOptionResponse]:
        """List active applications as attachment choices."""
        with self.database.session() as session:
            require_workspace(session, workspace_id)
            rows = session.execute(
                select(Application, JobPosting)
                .join(JobPosting, JobPosting.id == Application.job_posting_id)
                .where(
                    Application.workspace_id == workspace_id,
                    Application.archived_at.is_(None),
                    JobPosting.archived_at.is_(None),
                )
                .order_by(JobPosting.title)
            ).all()
            return [
                ApplicationOptionResponse(
                    id=application.id,
                    job_posting_id=job.id,
                    title=job.title,
                )
                for application, job in rows
            ]
