"""User-authored workspace records that are intentionally outside evidence."""

from clearscout.application.workspaces.records.applications import ApplicationRecordsService
from clearscout.application.workspaces.records.notes import NoteRecordsService
from clearscout.application.workspaces.records.saved_views import SavedViewRecordsService
from clearscout.application.workspaces.records.service import WorkspaceRecords
from clearscout.application.workspaces.records.tags import TagRecordsService

__all__ = [
    "ApplicationRecordsService",
    "NoteRecordsService",
    "SavedViewRecordsService",
    "TagRecordsService",
    "WorkspaceRecords",
]
