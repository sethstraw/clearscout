from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import IntegrityError

from clearscout.application import job_search
from clearscout.application.documents import DocumentService
from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.workspaces.records import (
    ApplicationRecordsService,
    NoteRecordsService,
    SavedViewRecordsService,
    TagRecordsService,
    WorkspaceRecords,
)
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Artifact, AuditEvent
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import studio_workspace


def test_an_audit_event_records_the_origin_it_was_written_under(
    durable_database: Database, tmp_path: Path
) -> None:
    """The provenance columns carry the origin, not a default beside a details blob.

    An audit event that took the `clearscout` default for an explicit user action
    would leave the trail unable to tell a person's edit from the program's own.
    """
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    records = WorkspaceRecords.for_database(durable_database)

    records.notes.create_note(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        title="Working thought",
        body="A note somebody typed.",
    )

    with durable_database.session() as session:
        origins = session.scalars(
            select(AuditEvent.origin).where(AuditEvent.workspace_id == workspace_id)
        ).all()
    assert "explicit_user_action" in set(origins)


def test_user_workspace_records_remain_outside_evidence(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    records = WorkspaceRecords.for_database(durable_database)
    note = records.notes.create_note(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        title="Working thought",
        body="This is my private working note.",
        pinned=True,
    )
    assert note.evidence_eligible is False
    assert records.notes.list_notes(workspace_id, search="private")[0].id == note.id
    tag = records.tags.create_tag(workspace_id=workspace_id, name="Priority", color="#4f46e5")
    assignment = records.tags.assign_tag(
        tag_id=str(tag.id),
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
    )
    assert assignment.entity_id == job_id
    view = records.saved_views.save_view(
        workspace_id=workspace_id,
        view_id=None,
        resource="jobs",
        name="Priority jobs",
        configuration={"filters": {"tag": "Priority"}, "sort": []},
    )
    assert view.configuration.filters == {"tag": "Priority"}


def test_archived_job_hides_active_child_records_and_unarchive_restores_them(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    records = WorkspaceRecords.for_database(durable_database)
    note = records.notes.create_note(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        title="Synthetic note",
        body="Never evidence.",
    )
    tag = records.tags.create_tag(workspace_id=workspace_id, name="Synthetic", color="#123456")
    records.tags.assign_tag(
        tag_id=str(tag.id),
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
    )
    document = DocumentService(
        durable_database, runtime_storage(durable_database, artifact_root)
    ).upload(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        logical_type="user.attachment",
        filename="notes.txt",
        mime_type="text/plain",
        content=b"synthetic only",
    )

    job_search.archive_job(durable_database, job_id, workspace_id)
    assert all(item.id != note.id for item in records.notes.list_notes(workspace_id))
    assert records.tags.list_tags(workspace_id)[0].assignments == []
    visible_artifacts = DocumentService(
        durable_database, runtime_storage(durable_database, artifact_root)
    ).list_artifacts(workspace_id)
    assert all(item.id != document.id for item in visible_artifacts)

    job_search.unarchive_job(durable_database, job_id, workspace_id)
    assert any(item.id == note.id for item in records.notes.list_notes(workspace_id))
    assert records.tags.list_tags(workspace_id)[0].assignments
    assert any(
        item.id == document.id
        for item in DocumentService(
            durable_database, runtime_storage(durable_database, artifact_root)
        ).list_artifacts(workspace_id)
    )


def test_tag_assignment_projection_uses_three_bounded_queries(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    records = WorkspaceRecords.for_database(durable_database)
    for index in range(12):
        tag = records.tags.create_tag(
            workspace_id=workspace_id,
            name=f"Synthetic {index}",
            color="#123456",
        )
        records.tags.assign_tag(
            tag_id=str(tag.id),
            workspace_id=workspace_id,
            entity_type="job_posting",
            entity_id=job_id,
        )
    statements: list[str] = []

    def count_statement(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(durable_database.engine, "before_cursor_execute", count_statement)
    try:
        tags = records.tags.list_tags(workspace_id)
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", count_statement)
    assert len(tags) == 12
    assert all(len(tag.assignments) == 1 for tag in tags)
    # A ceiling, not an exact count: twelve tags must not cost twelve queries,
    # and reading them in fewer than three is an improvement rather than a break.
    assert len(statements) <= 3


def test_workspace_record_crud_paths_and_validation(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    records = WorkspaceRecords.for_database(durable_database)

    note = records.notes.create_note(
        workspace_id=workspace_id,
        entity_type="workspace",
        entity_id=None,
        title="  Working note  ",
        body="Mutable Markdown",
    )
    updated_note = records.notes.update_note(
        str(note.id),
        workspace_id,
        title="Updated note",
        body="Still not evidence",
        pinned=True,
    )
    assert updated_note.pinned is True
    assert records.notes.list_notes(workspace_id, entity_type="workspace")[0].id == note.id
    records.notes.archive_note(str(note.id), workspace_id)
    assert records.notes.list_notes(workspace_id) == []
    assert records.notes.list_notes(workspace_id, include_archived=True)[0].id == note.id
    with pytest.raises(ValidationError, match="requires entity_type"):
        records.notes.list_notes(workspace_id, entity_id=job_id)

    tag = records.tags.create_tag(workspace_id=workspace_id, name="Target", color="#112233")
    updated_tag = records.tags.update_tag(
        str(tag.id), workspace_id, name="Priority", color="#abcdef"
    )
    assert updated_tag.color == "#abcdef"
    with pytest.raises(ConflictError, match="already exists"):
        records.tags.create_tag(workspace_id=workspace_id, name="Priority", color="#abcdef")
    other_tag = records.tags.create_tag(workspace_id=workspace_id, name="Other", color="#111111")
    with pytest.raises(ConflictError, match="already exists"):
        records.tags.update_tag(str(other_tag.id), workspace_id, name="Priority", color="#222222")
    records.tags.archive_tag(str(other_tag.id), workspace_id)
    with pytest.raises(ValidationError, match="Tag name"):
        records.tags.create_tag(workspace_id=workspace_id, name=" ", color="#123456")
    with pytest.raises(ValidationError, match="six-digit"):
        records.tags.create_tag(workspace_id=workspace_id, name="Bad color", color="red")
    assignment = records.tags.assign_tag(
        tag_id=str(tag.id),
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
    )
    assert (
        records.tags.assign_tag(
            tag_id=str(tag.id),
            workspace_id=workspace_id,
            entity_type="application",
            entity_id=application_id,
        ).id
        == assignment.id
    )
    assert records.tags.list_tags(workspace_id)[0].assignments[0].id == assignment.id
    records.tags.unassign_tag(
        tag_id=str(tag.id),
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
    )
    with pytest.raises(NotFoundError, match="assignment"):
        records.tags.unassign_tag(
            tag_id=str(tag.id),
            workspace_id=workspace_id,
            entity_type="application",
            entity_id=application_id,
        )
    records.tags.archive_tag(str(tag.id), workspace_id)
    assert records.tags.list_tags(workspace_id) == []
    with pytest.raises(ValidationError, match="only to jobs"):
        records.tags.assign_tag(
            tag_id=str(tag.id),
            workspace_id=workspace_id,
            entity_type="artifact",
            entity_id="missing",
        )

    view = records.saved_views.save_view(
        workspace_id=workspace_id,
        view_id=None,
        resource="jobs",
        name="My jobs",
        configuration={"filters": {}, "sort": []},
    )
    changed_view = records.saved_views.save_view(
        workspace_id=workspace_id,
        view_id=str(view.id),
        resource="jobs",
        name="My active jobs",
        configuration={"filters": [], "sort": {"field": "title"}},
    )
    assert changed_view.name == "My active jobs"
    assert records.saved_views.list_saved_views(workspace_id, resource="jobs")[0].id == view.id
    records.saved_views.archive_saved_view(str(view.id), workspace_id)
    assert records.saved_views.list_saved_views(workspace_id) == []
    with pytest.raises(ValidationError, match="Unsupported"):
        records.saved_views.list_saved_views(workspace_id, resource="applications")
    with pytest.raises(ValidationError, match="Unsupported"):
        records.saved_views.save_view(
            workspace_id=workspace_id,
            view_id=None,
            resource="applications",
            name="No",
            configuration={"filters": {}, "sort": []},
        )
    with pytest.raises(ValidationError, match="name"):
        records.saved_views.save_view(
            workspace_id=workspace_id,
            view_id=None,
            resource="jobs",
            name=" ",
            configuration={"filters": {}, "sort": []},
        )
    invalid_configurations: list[dict[str, Any]] = [
        {"filters": {}},
        {"filters": "bad", "sort": []},
        {"filters": {}, "sort": "bad"},
        {"filters": {}, "sort": [], "extra": []},
    ]
    for configuration in invalid_configurations:
        with pytest.raises(ValidationError, match="Saved-view"):
            records.saved_views.save_view(
                workspace_id=workspace_id,
                view_id=None,
                resource="jobs",
                name="Invalid",
                configuration=configuration,
            )

    with pytest.raises(NotFoundError, match="Workspace"):
        records.notes.list_notes("missing-workspace")
    with pytest.raises(NotFoundError, match="Note"):
        records.notes.update_note(
            str(note.id),
            workspace_id,
            title="Archived",
            body="Archived",
            pinned=False,
        )
    with pytest.raises(NotFoundError, match="Tag"):
        records.tags.update_tag(str(tag.id), workspace_id, name="Archived", color="#123456")
    with pytest.raises(NotFoundError, match="Saved view"):
        records.saved_views.save_view(
            workspace_id=workspace_id,
            view_id=str(view.id),
            resource="jobs",
            name="Archived",
            configuration={"filters": {}, "sort": []},
        )
    with pytest.raises(ValidationError, match="cannot be attached"):
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="artifact",
            entity_id="missing",
            title="Invalid",
            body="Invalid",
        )
    with pytest.raises(ValidationError, match="cannot name another"):
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="workspace",
            entity_id="another",
            title="Invalid",
            body="Invalid",
        )
    with pytest.raises(ValidationError, match="requires entity_id"):
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="job_posting",
            entity_id=None,
            title="Invalid",
            body="Invalid",
        )
    with pytest.raises(ValidationError, match="Note title"):
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="workspace",
            entity_id=None,
            title=" ",
            body="Invalid",
        )
    with pytest.raises(ValidationError, match="body cannot be empty"):
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="workspace",
            entity_id=None,
            title="Invalid",
            body=" ",
        )


def test_each_record_service_can_be_used_without_the_facade(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    note = NoteRecordsService(durable_database).create_note(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        title="Standalone",
        body="Written through the note service alone.",
    )
    tag = TagRecordsService(durable_database).create_tag(
        workspace_id=workspace_id, name="Standalone", color="#0f172a"
    )
    view = SavedViewRecordsService(durable_database).save_view(
        workspace_id=workspace_id,
        view_id=None,
        resource="jobs",
        name="Standalone",
        configuration={"filters": {}, "sort": []},
    )
    options = ApplicationRecordsService(durable_database).list_applications(workspace_id)
    assert [str(item.id) for item in options] == [application_id]

    # The bundle is what the HTTP layer takes, so a member wired to the wrong
    # service would answer plausibly and about the wrong records.
    bundle = WorkspaceRecords.for_database(durable_database)
    assert [item.id for item in bundle.notes.list_notes(workspace_id)] == [note.id]
    assert [item.id for item in bundle.tags.list_tags(workspace_id)] == [tag.id]
    assert [item.id for item in bundle.saved_views.list_saved_views(workspace_id)] == [view.id]
    assert [str(item.id) for item in bundle.applications.list_applications(workspace_id)] == [
        application_id
    ]


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("logical_type", "unknown.artifact", "ck_artifact_logical_type"),
        ("author_type", "imported", "ck_artifact_author_type"),
        ("integrity_status", "unchecked", "ck_artifact_integrity_status"),
    ],
)
def test_artifact_vocabularies_are_enforced_by_the_database(
    durable_database: Database,
    tmp_path: Path,
    column: str,
    value: str,
    constraint: str,
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    documents = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    artifact = documents.upload(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        logical_type="user.attachment",
        filename="current.txt",
        mime_type="text/plain",
        content=b"current artifact",
    )

    def write_unknown_vocabulary() -> None:
        with durable_database.session() as session:
            session.execute(
                update(Artifact).where(Artifact.id == artifact.id).values({column: value})
            )
            session.flush()

    with pytest.raises(IntegrityError, match=constraint):
        write_unknown_vocabulary()
