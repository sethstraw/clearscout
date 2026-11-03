"""Editing a prompt, and the properties that editing must not break.

Everything asserted here is decidable without a model: whether an override is
the packaged file byte for byte, whether the suite fingerprint moved, whether a
revert appended or overwrote, and which version a stored run was produced under.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.prompts import (
    PROMPT_NAMES,
    canonical,
    digest,
    load,
    overriding,
    packaged,
    suite_fingerprint,
)
from clearscout.application.tailoring.workflows import TailorOutput
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.application.workspaces.prompt_library import PromptLibraryService, overrides_in
from clearscout.bootstrap.worker import Worker, default_handlers
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import (
    PromptTemplate,
    PromptVersion,
    RunPromptVersion,
    TailoringRun,
)
from tests.support.artifacts import runtime_storage
from tests.support.runners import ScriptedPipelineRunner

KEY = "draft_system"


@pytest.fixture
def workspace(durable_database: Database) -> str:
    """Return one workspace to hang prompt overrides on."""
    return str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="prompts", name="Prompts")
        .id
    )


@pytest.fixture
def library(durable_database: Database) -> PromptLibraryService:
    """Return the service under test."""
    return PromptLibraryService(durable_database)


def test_a_prompt_nobody_edited_has_no_row(
    library: PromptLibraryService, durable_database: Database, workspace: str
) -> None:
    listed = library.list_prompts(workspace)
    assert [item.key for item in listed] == list(PROMPT_NAMES)
    assert {item.origin for item in listed} == {"packaged"}
    assert all(item.version == 0 for item in listed)
    with durable_database.session() as session:
        assert session.scalars(select(PromptTemplate)).all() == []


def test_saving_the_packaged_text_unchanged_creates_no_override(
    library: PromptLibraryService, durable_database: Database, workspace: str
) -> None:
    """Opening a prompt and pressing save is not an edit.

    An override created by accident is one a later release has to collide with,
    and the person who created it would have no idea why they were being asked.
    """
    saved = library.save(workspace, KEY, packaged(KEY))
    assert saved.origin == "packaged"
    assert saved.version == 0
    with durable_database.session() as session:
        assert session.scalars(select(PromptVersion)).all() == []


def test_an_override_replaces_the_whole_file(library: PromptLibraryService, workspace: str) -> None:
    edited = packaged(KEY) + "\nOne more rule.\n"
    saved = library.save(workspace, KEY, edited)
    assert saved.origin == "override"
    assert saved.version == 1
    assert saved.content == canonical(edited)
    assert saved.packaged_content == packaged(KEY)
    assert library.read(workspace, KEY).content == canonical(edited)


def test_invisible_differences_do_not_move_the_suite_fingerprint(
    library: PromptLibraryService, durable_database: Database, workspace: str
) -> None:
    """Text differing only in line endings and trailing space is the same prompt.

    Without the normalization this is a different digest, a different
    `suite_fingerprint`, a reuse check that misses, and a paid run that produces
    exactly the package already on disk.
    """
    before = suite_fingerprint()
    sloppy = packaged(KEY).replace("\n", "  \r\n") + "\n\n\n"
    saved = library.save(workspace, KEY, sloppy)
    assert saved.origin == "packaged"
    with durable_database.session() as session:
        assert overrides_in(session, workspace) == {}
        with overriding({key: text for key, (_, text) in overrides_in(session, workspace).items()}):
            assert suite_fingerprint() == before


def test_the_fingerprint_moves_under_an_edited_prompt(
    library: PromptLibraryService, workspace: str
) -> None:
    before = suite_fingerprint()
    library.save(workspace, KEY, packaged(KEY) + "\nOne more rule.\n")
    with overriding({key: text for key, (_, text) in library.overrides(workspace).items()}):
        assert load(KEY).endswith("One more rule.\n")
        assert suite_fingerprint() != before
    assert suite_fingerprint() == before


def test_a_prompt_missing_a_placeholder_its_caller_fills_is_refused(
    library: PromptLibraryService, workspace: str
) -> None:
    with pytest.raises(ValidationError, match="Missing: distinctions"):
        library.save(workspace, KEY, packaged(KEY).replace("{{distinctions}}", "some rules"))


def test_a_prompt_asking_for_a_value_nobody_supplies_is_refused(
    library: PromptLibraryService, workspace: str
) -> None:
    with pytest.raises(ValidationError, match="Not supplied: salary"):
        library.save(workspace, KEY, packaged(KEY) + "\nPay attention to {{salary}}.\n")


@pytest.mark.parametrize(
    "construct",
    ["{% if evidence %}yes{% endif %}", "{{ distinctions }}", "{{Distinctions}}"],
)
def test_the_template_dialect_stays_one_substitution_and_nothing_else(
    library: PromptLibraryService, workspace: str, construct: str
) -> None:
    """A conditional or a filter reaches the model as literal text, so the save gate refuses it."""
    with pytest.raises(ValidationError):
        library.save(workspace, KEY, packaged(KEY) + f"\n{construct}\n")


def test_an_empty_prompt_is_refused(library: PromptLibraryService, workspace: str) -> None:
    with pytest.raises(ValidationError, match="cannot be empty"):
        library.save(workspace, KEY, "   \n\n")


def test_a_prompt_outside_the_suite_has_nowhere_to_be_stored(
    library: PromptLibraryService, workspace: str
) -> None:
    with pytest.raises(NotFoundError, match="not part of this prompt suite"):
        library.save(workspace, "draft_systemm", "text")


def test_the_variables_an_editor_has_to_enforce_are_published(
    library: PromptLibraryService, workspace: str
) -> None:
    published = library.read(workspace, "draft_user").variables
    assert {item.name: item.handling for item in published} == {
        "role": "inline",
        "company": "inline",
        "keywords": "block",
        "evidence": "block",
        "feedback": "block",
        "posting_hint": "block",
        "active_disputes": "block",
    }


def test_reverting_appends_a_version_rather_than_moving_a_pointer(
    library: PromptLibraryService, workspace: str
) -> None:
    """Version four's text comes back as version three, and nothing is rewritten."""
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    library.save(workspace, KEY, packaged(KEY) + "\nSecond edit.\n")
    reverted = library.revert(workspace, KEY, 1)
    assert reverted.version == 3
    assert reverted.content.endswith("First edit.\n")
    history = library.versions(workspace, KEY)
    assert [item.version for item in history] == [3, 2, 1]
    # The revert is recoverable from the digests rather than from a label.
    assert history[0].content_sha256 == history[2].content_sha256
    assert history[0].restores_version == 1
    assert history[1].restores_version is None


def test_a_saved_version_cannot_be_updated_or_deleted(
    library: PromptLibraryService, durable_database: Database, workspace: str
) -> None:
    """A run points at a version, so the database refuses what convention alone would not."""
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(text("UPDATE prompt_versions SET content = 'rewritten'"))
    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(text("DELETE FROM prompt_versions"))
    assert library.read(workspace, KEY).content.endswith("First edit.\n")


def test_resetting_returns_the_packaged_text_and_keeps_the_history(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    reset = library.reset(workspace, KEY)
    assert reset.origin == "packaged"
    assert reset.version == 0
    assert reset.content == packaged(KEY)
    assert library.overrides(workspace) == {}
    assert [item.version for item in library.versions(workspace, KEY)] == [1]


def test_editing_after_a_reset_continues_one_history(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    library.reset(workspace, KEY)
    saved = library.save(workspace, KEY, packaged(KEY) + "\nLater edit.\n")
    assert saved.version == 2
    assert [item.version for item in library.versions(workspace, KEY)] == [2, 1]


def test_two_tabs_editing_one_prompt_produce_a_refusal(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    with pytest.raises(ConflictError, match="is at version 1, not 0"):
        library.save(workspace, KEY, packaged(KEY) + "\nOther tab.\n", expected_version=0)


def test_the_report_names_the_origin_and_the_version(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    listed = {item.key: item for item in library.list_prompts(workspace)}
    assert listed[KEY].origin == "override"
    assert listed[KEY].version == 1
    assert listed[KEY].packaged_sha256 == digest(canonical(packaged(KEY)))
    assert listed[KEY].content_sha256 != listed[KEY].packaged_sha256
    assert listed["match_system"].origin == "packaged"
    assert listed["match_system"].content_sha256 == listed["match_system"].packaged_sha256


def test_any_two_versions_diff_including_the_packaged_file(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    library.save(workspace, KEY, packaged(KEY) + "\nSecond edit.\n")
    against_packaged = library.diff(workspace, KEY, from_version=0, to_version=1)
    assert "+First edit." in against_packaged.diff
    assert "packaged" in against_packaged.diff
    non_adjacent = library.diff(workspace, KEY, from_version=1, to_version=2)
    assert "-First edit." in non_adjacent.diff
    assert "+Second edit." in non_adjacent.diff


def test_a_version_that_was_never_saved_is_refused(
    library: PromptLibraryService, workspace: str
) -> None:
    with pytest.raises(NotFoundError, match="has no version 4"):
        library.diff(workspace, KEY, from_version=0, to_version=4)


def _shift_the_packaged_default(monkeypatch: pytest.MonkeyPatch, key: str, suffix: str) -> None:
    """Make the shipped file read differently, as a release would."""

    def shifted(name: str) -> str:
        return packaged(name) + suffix if name == key else packaged(name)

    monkeypatch.setattr("clearscout.application.workspaces.prompt_library.packaged", shifted)


def test_an_unchanged_default_leaves_an_override_alone(
    library: PromptLibraryService, workspace: str
) -> None:
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    state = library.read(workspace, KEY)
    assert state.upgrade == "current"
    assert state.origin == "override"


def test_a_copy_that_never_diverged_adopts_the_new_default(
    library: PromptLibraryService,
    durable_database: Database,
    workspace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody's writing is at stake, so the new default is taken and kept quiet."""
    with durable_database.session() as session:
        template = PromptTemplate(workspace_id=workspace, key=KEY)
        session.add(template)
        session.flush()
        text = canonical(packaged(KEY))
        session.add(
            PromptVersion(
                prompt_template_id=template.id,
                version_number=1,
                content=text,
                content_digest=digest(text),
                base_digest=digest(text),
            )
        )
    _shift_the_packaged_default(monkeypatch, KEY, "\nA rule the release added.\n")
    state = library.read(workspace, KEY)
    assert state.upgrade == "adopted"
    assert state.origin == "packaged"
    assert state.content.endswith("A rule the release added.\n")
    assert library.overrides(workspace) == {}


def test_two_changes_collide_and_the_persons_text_is_kept(
    library: PromptLibraryService, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is overwritten. The new default is reported beside the edit."""
    library.save(workspace, KEY, packaged(KEY) + "\nFirst edit.\n")
    _shift_the_packaged_default(monkeypatch, KEY, "\nA rule the release added.\n")
    state = library.read(workspace, KEY)
    assert state.upgrade == "collision"
    assert state.origin == "override"
    assert state.content.endswith("First edit.\n")
    assert state.packaged_content.endswith("A rule the release added.\n")
    assert state.base_sha256 != state.packaged_sha256
    # Taking the new default is the reset that was already there, and it leaves
    # the person's version behind rather than deleting it. Nothing is parked
    # afterwards either: a collision somebody settled is not still a collision.
    after = library.reset(workspace, KEY)
    assert after.content.endswith("A rule the release added.\n")
    assert after.upgrade == "current"
    assert after.origin == "packaged"
    assert [item.version for item in library.versions(workspace, KEY)] == [1]


def test_a_run_is_given_the_edited_prompt_and_records_which_version(
    durable_database: Database, tmp_path: Path
) -> None:
    """The run reads the override, and what it read is a foreign key afterwards.

    Both halves matter separately. A run given the packaged text while a row
    claims otherwise would be a lie in the ledger, and a run given the override
    with nothing recorded would be a package nobody can explain later.
    """
    from tests.support.scenarios import studio_workspace

    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    library = PromptLibraryService(durable_database)
    library.save(workspace_id, KEY, packaged(KEY) + "\nFirst edit.\n")
    library.save(workspace_id, KEY, packaged(KEY) + "\nSecond edit.\n")

    seen: dict[str, str] = {}

    class _Watching(ScriptedPipelineRunner):
        def tailor(self, *args: object, **kwargs: object) -> TailorOutput:
            seen[KEY] = load(KEY)
            seen["fingerprint"] = suite_fingerprint()
            return super().tailor(*args, **kwargs)

    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={
            "job_posting_id": job_id,
            "cover_letter": False,
            "regenerate": False,
        },
        idempotency_key="tailor-prompts",
    )
    worker = Worker(
        durable_database,
        worker_id="prompts",
        handlers=default_handlers(
            durable_database, artifact_root=artifact_root, workflow_runner=_Watching()
        ),
    )
    assert worker.run_once()
    assert queue.get(str(task.id)).status == "completed"

    assert seen[KEY].endswith("Second edit.\n")
    assert seen["fingerprint"] != suite_fingerprint()

    version_id = library.overrides(workspace_id)[KEY][0]
    with durable_database.session() as session:
        run = session.scalar(select(TailoringRun).where(TailoringRun.task_id == task.id))
        assert run is not None
        linked = session.scalars(
            select(PromptVersion)
            .join(RunPromptVersion, RunPromptVersion.prompt_version_id == PromptVersion.id)
            .where(RunPromptVersion.tailoring_run_id == run.id)
        ).all()
        assert [item.id for item in linked] == [version_id]
        assert [item.version_number for item in linked] == [2]


def test_the_http_surface_edits_diffs_reverts_and_resets_one_prompt(
    durable_database_url: str, tmp_path: Path
) -> None:
    """One pass over the surface a browser editor is built against."""
    from fastapi.testclient import TestClient

    from clearscout.bootstrap.api import create_app

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace = client.post("/api/v1/workspaces", json={"slug": "edit", "name": "Edit"})
        assert workspace.status_code == 201
        workspace_id = workspace.json()["id"]
        parameters = {"workspace_id": workspace_id}

        listed = client.get("/api/v1/prompts", params=parameters)
        assert listed.status_code == 200
        assert len(listed.json()) == len(PROMPT_NAMES)
        assert {item["origin"] for item in listed.json()} == {"packaged"}

        detail = client.get(f"/api/v1/prompts/{KEY}", params=parameters)
        assert detail.status_code == 200
        assert detail.json()["content"] == packaged(KEY)
        assert {item["name"] for item in detail.json()["variables"]} == {"distinctions"}

        refused = client.put(
            f"/api/v1/prompts/{KEY}",
            json={"workspace_id": workspace_id, "content": "No placeholders here.\n"},
        )
        assert refused.status_code == 422
        assert "distinctions" in refused.json()["detail"]

        saved = client.put(
            f"/api/v1/prompts/{KEY}",
            json={
                "workspace_id": workspace_id,
                "content": packaged(KEY) + "\nFirst edit.\n",
                "expected_version": 0,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["origin"] == "override"
        assert saved.json()["version"] == 1

        stale = client.put(
            f"/api/v1/prompts/{KEY}",
            json={
                "workspace_id": workspace_id,
                "content": packaged(KEY) + "\nOther tab.\n",
                "expected_version": 0,
            },
        )
        assert stale.status_code == 409

        diff = client.get(
            f"/api/v1/prompts/{KEY}/diff",
            params={**parameters, "from_version": 0, "to_version": 1},
        )
        assert diff.status_code == 200
        assert "+First edit." in diff.json()["diff"]

        client.put(
            f"/api/v1/prompts/{KEY}",
            json={"workspace_id": workspace_id, "content": packaged(KEY) + "\nSecond edit.\n"},
        )
        reverted = client.post(
            f"/api/v1/prompts/{KEY}/revert", json={"workspace_id": workspace_id, "version": 1}
        )
        assert reverted.status_code == 200
        assert reverted.json()["version"] == 3
        assert reverted.json()["content"].endswith("First edit.\n")

        history = client.get(f"/api/v1/prompts/{KEY}/versions", params=parameters)
        assert [item["version"] for item in history.json()] == [3, 2, 1]
        assert history.json()[0]["restores_version"] == 1

        reset = client.post(f"/api/v1/prompts/{KEY}/reset", json={"workspace_id": workspace_id})
        assert reset.status_code == 200
        assert reset.json()["origin"] == "packaged"
        assert reset.json()["content"] == packaged(KEY)
        assert len(client.get(f"/api/v1/prompts/{KEY}/versions", params=parameters).json()) == 3

        missing = client.get("/api/v1/prompts/draft_systemm", params=parameters)
        assert missing.status_code == 404


def test_a_run_under_the_packaged_suite_records_nothing(
    durable_database: Database, tmp_path: Path
) -> None:
    """No override, no row. There is no version to point at and none is invented."""
    from tests.support.scenarios import studio_workspace

    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id, "cover_letter": False},
        idempotency_key="tailor-packaged",
    )
    worker = Worker(
        durable_database,
        worker_id="prompts",
        handlers=default_handlers(
            durable_database, artifact_root=artifact_root, workflow_runner=ScriptedPipelineRunner()
        ),
    )
    assert worker.run_once()
    with durable_database.session() as session:
        assert session.scalars(select(RunPromptVersion)).all() == []
