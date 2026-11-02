"""The workspace-record services, and the bundle that hands out all four."""

from __future__ import annotations

from dataclasses import dataclass

from tailorcv.application.workspaces.records.applications import ApplicationRecordsService
from tailorcv.application.workspaces.records.notes import NoteRecordsService
from tailorcv.application.workspaces.records.saved_views import SavedViewRecordsService
from tailorcv.application.workspaces.records.tags import TagRecordsService
from tailorcv.infrastructure.persistence.database import Database


@dataclass(frozen=True)
class WorkspaceRecords:
    """The four record services, held together so a caller can take just one.

    Notes, tags, saved views and application options share a database and
    nothing else. They are bundled because one HTTP router touches several, not
    because they are one thing.
    """

    applications: ApplicationRecordsService
    notes: NoteRecordsService
    saved_views: SavedViewRecordsService
    tags: TagRecordsService

    @classmethod
    def for_database(cls, database: Database) -> WorkspaceRecords:
        """Build every record service against one durable database."""
        return cls(
            applications=ApplicationRecordsService(database),
            notes=NoteRecordsService(database),
            saved_views=SavedViewRecordsService(database),
            tags=TagRecordsService(database),
        )
