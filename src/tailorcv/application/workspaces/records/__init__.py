"""User-authored workspace records that are intentionally outside evidence."""

from tailorcv.application.workspaces.records.applications import ApplicationRecordsService
from tailorcv.application.workspaces.records.notes import NoteRecordsService
from tailorcv.application.workspaces.records.saved_views import SavedViewRecordsService
from tailorcv.application.workspaces.records.service import WorkspaceRecords
from tailorcv.application.workspaces.records.tags import TagRecordsService

__all__ = [
    "ApplicationRecordsService",
    "NoteRecordsService",
    "SavedViewRecordsService",
    "TagRecordsService",
    "WorkspaceRecords",
]
