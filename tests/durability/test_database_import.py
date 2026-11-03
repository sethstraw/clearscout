"""Importing an earlier database: what arrives, what does not, and what is refused."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import warnings
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SAWarning

from clearscout.application import job_search
from clearscout.application.failures import ValidationError
from clearscout.application.portability import import_database
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import utc_now
from clearscout.infrastructure.artifacts.store import StagedArtifact
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migration import upgrade
from clearscout.infrastructure.persistence.models import (
    CandidateProfile,
    MasterCvVersion,
    SourceScan,
    SourceScanContinuation,
    SourceScanMember,
)
from clearscout.infrastructure.persistence.models.tailoring import ModelCall
from clearscout.infrastructure.persistence.models.workspaces import Workspace
from tests.support.artifacts import artifact_service, runtime_storage
from tests.support.scenarios import board_source, evaluation_commit, state_target_titles


def _populated(tmp_path: Path, name: str) -> Path:
    """Return a database holding one workspace and two evaluation versions."""
    path = tmp_path / name
    url = f"sqlite+pysqlite:///{path}"
    upgrade(url)
    database = Database(url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="source", name="Source"
        )
        artifacts = artifact_service(database, tmp_path / f"{name}-artifacts")
        first = job_search.commit_evaluation(database, artifacts, evaluation_commit(workspace.id))
        job_search.commit_evaluation(
            database,
            artifacts,
            evaluation_commit(workspace.id).model_copy(
                update={
                    "rationale": "A supported recalculation.",
                    "supersedes_evaluation_id": first.evaluation_id,
                }
            ),
        )
    finally:
        database.dispose()
    return path


def _fresh(tmp_path: Path, name: str) -> tuple[Path, Database]:
    path = tmp_path / name
    url = f"sqlite+pysqlite:///{path}"
    upgrade(url)
    return path, Database(url)


def test_an_earlier_database_arrives_whole(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source.sqlite3")
    target_path, target = _fresh(tmp_path, "target.sqlite3")

    try:
        report = import_database(source, target)
    finally:
        target.dispose()

    assert report.copied["workspaces"] == 1
    assert report.copied["job_evaluations"] == 2
    assert report.total_rows > 0
    original = sqlite3.connect(source)
    imported = sqlite3.connect(target_path)
    try:
        for table, count in report.copied.items():
            if count:
                before = original.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608
                after = imported.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]  # noqa: S608
                assert (table, before) == (table, after)
        assert imported.execute("PRAGMA foreign_key_check").fetchall() == []
        assert imported.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        original.close()
        imported.close()


def test_import_does_not_schedule_an_earlier_workspace(tmp_path: Path) -> None:
    source = _populated(tmp_path, "unscheduled.sqlite3")
    with sqlite3.connect(source) as connection:
        connection.execute("ALTER TABLE workspaces DROP COLUMN scan_schedule")
    _, target = _fresh(tmp_path, "unscheduled-target.sqlite3")
    try:
        report = import_database(source, target)
        with target.session() as session:
            workspace = session.scalar(select(Workspace))
            assert workspace is not None
            assert workspace.scan_schedule is None
        assert report.added_columns["workspaces"] == ["scan_schedule"]
        assert report.copied["job_evaluations"] == 2
    finally:
        target.dispose()


def test_import_preserves_saved_schedule_and_occurrence_history(tmp_path: Path) -> None:
    source = _populated(tmp_path, "scheduled.sqlite3")
    schedule = {
        "configuration": {
            "frequency": "weekly",
            "local_time": "09:00",
            "timezone": "America/Winnipeg",
            "weekday": 1,
            "scope": "selected",
            "source_ids": ["retained-source"],
            "paused": True,
        },
        "revision": 3,
        "next_run_at": None,
        "last_due_at": "2026-10-06T14:00:00Z",
        "last_task_id": "retained-task",
        "last_error": "Source unavailable",
    }
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE workspaces SET scan_schedule = ?", (json.dumps(schedule),))
        workspace_id = connection.execute("SELECT id FROM workspaces").fetchone()[0]
    _, target = _fresh(tmp_path, "scheduled-target.sqlite3")
    try:
        import_database(source, target)
        with target.session() as session:
            workspace = session.get(Workspace, workspace_id)
            assert workspace is not None
            assert workspace.scan_schedule == schedule
    finally:
        target.dispose()


def test_source_batch_and_continuation_references_survive_import_without_ordering_warnings(
    tmp_path: Path,
) -> None:
    source = _populated(tmp_path, "source-batch.sqlite3")
    original = Database(f"sqlite+pysqlite:///{source}")
    try:
        with original.session() as session:
            workspace_id = session.scalar(select(Workspace.id))
            assert workspace_id is not None
        state_target_titles(original, workspace_id, "data")
        source_id = board_source(original, workspace_id, "Acme", "https://example.test/jobs")
        task = TaskQueueService(original).enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={"source_ids": [source_id]},
            idempotency_key="import-batch",
        )
        with original.session() as session:
            now = utc_now()
            scan = SourceScan(
                source_id=source_id,
                scanned_on=now.date(),
                query_label="titles",
                status="incomplete",
                scan_started_at=now,
                scan_completed_at=now,
            )
            session.add(scan)
            session.flush()
            member = session.scalar(
                select(SourceScanMember).where(SourceScanMember.root_task_id == str(task.id))
            )
            assert member is not None
            continuation = SourceScanContinuation(
                workspace_id=workspace_id,
                source_scan_id=scan.id,
                generation=1,
                handoff_scopes=[],
                batch_member_id=member.id,
            )
            session.add(continuation)
            session.flush()
            member.continuation_id = continuation.id
            member.generation = 1
            member.source_scan_id = scan.id
        TaskQueueService(original).request_cancellation(task.id)
    finally:
        original.dispose()
    target_path, target = _fresh(tmp_path, "target-batch.sqlite3")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            preview = import_database(source, target, dry_run=True)
            report = import_database(source, target)
        assert preview.total_rows == report.total_rows
        assert report.copied["source_scan_members"] == 1
        assert report.copied["source_scan_continuations"] == 1
        with sqlite3.connect(source) as before, sqlite3.connect(target_path) as after:
            for query in (
                "SELECT * FROM source_scan_members",
                "SELECT * FROM source_scan_continuations",
            ):
                assert before.execute(query).fetchall() == after.execute(query).fetchall()
            assert after.execute("PRAGMA foreign_key_check").fetchall() == []
            assert after.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        target.dispose()


@pytest.mark.parametrize("task_type", ["source.scan", "source.continue"])
@pytest.mark.parametrize("status", ["queued", "running", "cancelled"])
def test_import_refuses_active_source_work_without_copying_rows(
    tmp_path: Path, task_type: str, status: str
) -> None:
    source = _populated(tmp_path, "active-source.sqlite3")
    original = Database(f"sqlite+pysqlite:///{source}")
    try:
        with original.session() as session:
            workspace_id = session.scalar(select(Workspace.id))
            assert workspace_id is not None
        state_target_titles(original, workspace_id, "data")
        source_id = board_source(original, workspace_id, "Acme", "https://example.test/jobs")
        task = TaskQueueService(original).enqueue(
            workspace_id=workspace_id,
            task_type="source.scan",
            payload={"source_ids": [source_id]},
            idempotency_key="active-import",
        )
        with original.session() as session:
            session.execute(
                text("UPDATE background_tasks SET task_type=:kind, status=:status WHERE id=:id"),
                {"kind": task_type, "status": status, "id": str(task.id)},
            )
            if status == "cancelled":
                session.execute(
                    text(
                        "UPDATE source_scan_members SET active_task_id=:id WHERE root_task_id=:id"
                    ),
                    {"id": str(task.id)},
                )
    finally:
        original.dispose()
    target_path, target = _fresh(tmp_path, "active-target.sqlite3")
    try:
        with pytest.raises(ValidationError, match="Stop or cancel active source scans"):
            import_database(source, target)
        with sqlite3.connect(target_path) as connection:
            assert connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    finally:
        target.dispose()


def test_the_seeded_scoring_policy_is_not_duplicated(tmp_path: Path) -> None:
    """A fresh database is not empty; it carries the shipped policy already."""
    source = _populated(tmp_path, "source.sqlite3")
    target_path, target = _fresh(tmp_path, "target.sqlite3")

    fresh = sqlite3.connect(target_path)
    try:
        seeded = fresh.execute("SELECT count(*) FROM score_weight_sets").fetchone()[0]
    finally:
        fresh.close()
    assert seeded, "a migrated database should already carry at least one policy"

    try:
        report = import_database(source, target)
    finally:
        target.dispose()

    assert report.already_present["score_weight_sets"] == seeded
    imported = sqlite3.connect(target_path)
    try:
        assert imported.execute("SELECT count(*) FROM score_weight_sets").fetchone()[0] == seeded
    finally:
        imported.close()


def test_the_source_is_opened_read_only(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source.sqlite3")
    before = source.read_bytes()
    _, target = _fresh(tmp_path, "target.sqlite3")

    try:
        import_database(source, target)
    finally:
        target.dispose()

    assert source.read_bytes() == before


def test_importing_into_a_used_database_is_refused(tmp_path: Path) -> None:
    """Merging two ledgers is a different problem, and this is not it."""
    source = _populated(tmp_path, "source.sqlite3")
    _, target = _fresh(tmp_path, "target.sqlite3")

    try:
        WorkspaceService(target, runtime_storage(target)).create(
            slug="already", name="Already here"
        )
        with pytest.raises(ValidationError, match="already has workspaces"):
            import_database(source, target)
    finally:
        target.dispose()


def test_a_table_this_schema_dropped_is_reported_rather_than_lost_silently(
    tmp_path: Path,
) -> None:
    source = _populated(tmp_path, "source.sqlite3")
    retired = sqlite3.connect(source)
    try:
        retired.execute("CREATE TABLE retired_notes (id TEXT PRIMARY KEY, body TEXT)")
        retired.execute("INSERT INTO retired_notes VALUES ('a', 'kept elsewhere')")
        retired.commit()
    finally:
        retired.close()
    _, target = _fresh(tmp_path, "target.sqlite3")

    try:
        report = import_database(source, target)
    finally:
        target.dispose()

    assert report.skipped_tables["retired_notes"] == 1
    assert any("retired_notes" in line for line in report.lines())


def _source_with_model_call_lacking(tmp_path: Path, column: str) -> Path:
    source = _populated(tmp_path, "source.sqlite3")
    database = Database(f"sqlite+pysqlite:///{source}")
    try:
        with database.session() as session:
            workspace = session.scalars(select(Workspace)).one()
            session.add(
                ModelCall(
                    workspace_id=workspace.id,
                    stage="match",
                    provider="openai",
                    requested_model="gpt-5.6-terra",
                    origin="workflow",
                    validated=True,
                    cache_hit=False,
                    created_at=utc_now(),
                )
            )
    finally:
        database.dispose()
    earlier = sqlite3.connect(source)
    try:
        earlier.execute(f'ALTER TABLE model_calls DROP COLUMN "{column}"')
        earlier.commit()
    finally:
        earlier.close()
    return source


def test_a_column_added_with_a_database_default_fills_earlier_rows(tmp_path: Path) -> None:
    source = _source_with_model_call_lacking(tmp_path, "resent_on_standard")
    target_path, target = _fresh(tmp_path, "target.sqlite3")

    try:
        report = import_database(source, target)
    finally:
        target.dispose()

    assert report.added_columns["model_calls"] == ["resent_on_standard"]
    imported = sqlite3.connect(target_path)
    try:
        assert imported.execute("SELECT resent_on_standard FROM model_calls").fetchall() == [(0,)]
    finally:
        imported.close()


def test_a_required_column_with_only_a_python_default_is_refused(tmp_path: Path) -> None:
    """The import writes raw rows, so an ORM-side default never reaches them."""
    source = _source_with_model_call_lacking(tmp_path, "prompt_names")
    _, target = _fresh(tmp_path, "target.sqlite3")

    try:
        with pytest.raises(ValidationError, match="lacks required target columns: prompt_names"):
            import_database(source, target)
    finally:
        target.dispose()


def test_a_file_that_is_not_a_clearscout_database_is_refused(tmp_path: Path) -> None:
    stranger = tmp_path / "stranger.sqlite3"
    connection = sqlite3.connect(stranger)
    try:
        connection.execute("CREATE TABLE unrelated (id TEXT)")
        connection.commit()
    finally:
        connection.close()
    _, target = _fresh(tmp_path, "target.sqlite3")

    try:
        with pytest.raises(ValidationError, match="not a ClearScout database"):
            import_database(stranger, target)
        with pytest.raises(ValidationError, match="No database to import"):
            import_database(tmp_path / "absent.sqlite3", target)
    finally:
        target.dispose()


def test_imported_evaluations_keep_their_supersession_chain(tmp_path: Path) -> None:
    """The chain trigger runs on insert, so a broken import fails rather than lands."""
    source = _populated(tmp_path, "source.sqlite3")
    target_path, target = _fresh(tmp_path, "target.sqlite3")

    try:
        import_database(source, target)
        with target.session() as session:
            versions = (
                session.execute(
                    text("SELECT version_number FROM job_evaluations ORDER BY version_number")
                )
                .scalars()
                .all()
            )
    finally:
        target.dispose()

    assert versions == [1, 2]
    assert target_path.exists()


def test_dry_run_preflights_without_writing_rows_or_artifacts(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source.sqlite3")
    target_path, target = _fresh(tmp_path, "target.sqlite3")
    try:
        report = import_database(source, target, dry_run=True)
    finally:
        target.dispose()

    assert report.dry_run is True
    assert report.total_rows > 0
    assert report.unresolved_references
    target_connection = sqlite3.connect(target_path)
    try:
        assert target_connection.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    finally:
        target_connection.close()


def test_divergent_seed_collision_is_refused_with_columns(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source.sqlite3")
    _, target = _fresh(tmp_path, "target.sqlite3")
    with target.engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER prevent_score_weight_sets_update")
        connection.exec_driver_sql(
            "UPDATE score_weight_sets SET name = 'Divergent seed' "
            "WHERE id = 'score-weights-default-v1'"
        )
    try:
        with pytest.raises(
            ValidationError,
            match=r"Divergent primary-key collision in score_weight_sets .*name",
        ):
            import_database(source, target)
    finally:
        target.dispose()


def test_explicit_roots_relocate_and_verify_immutable_artifacts(tmp_path: Path) -> None:
    source_root = tmp_path / "source-data"
    source_path = source_root / "clearscout.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    source_database = Database(source_url)
    try:
        workspace = WorkspaceService(source_database, runtime_storage(source_database)).create(
            slug="portable", name="Portable"
        )
        master_content = b"# Synthetic Candidate\n\nEvidence-only test record.\n"
        master_path = source_root / "workspaces" / "portable" / "master-cv" / "v1.md"
        master_path.parent.mkdir(parents=True, exist_ok=True)
        master_path.write_bytes(master_content)
        with source_database.session() as session:
            candidate = CandidateProfile(
                workspace_id=workspace.id, display_name="Synthetic Candidate"
            )
            session.add(candidate)
            session.flush()
            session.add(
                MasterCvVersion(
                    candidate_profile_id=candidate.id,
                    content_sha256=hashlib.sha256(master_content).hexdigest(),
                    source_uri=master_path.as_uri(),
                    effective_at=utc_now(),
                    byte_size=len(master_content),
                )
            )
        job_search.commit_evaluation(
            source_database,
            artifact_service(source_database, source_root / "artifacts"),
            evaluation_commit(workspace.id),
        )
    finally:
        source_database.dispose()

    target_root = tmp_path / "target-data"
    target_path = target_root / "clearscout.sqlite3"
    target_url = f"sqlite+pysqlite:///{target_path}"
    upgrade(target_url)
    target = Database(target_url)
    try:
        dry_run = import_database(
            source_path,
            target,
            dry_run=True,
            source_data_root=source_root,
            target_data_root=target_root,
        )
        assert any(item.table == "artifacts" for item in dry_run.file_relocations)
        assert not (target_root / "artifacts").exists()
        report = import_database(
            source_path,
            target,
            source_data_root=source_root,
            target_data_root=target_root,
        )
        with target.engine.connect() as connection:
            workspace_root = connection.exec_driver_sql(
                "SELECT root_path FROM workspaces"
            ).scalar_one()
            artifacts = connection.exec_driver_sql(
                "SELECT storage_uri, sha256, byte_size, integrity_status FROM artifacts"
            ).fetchall()
            master = connection.exec_driver_sql(
                "SELECT source_uri, content_sha256, byte_size FROM master_cv_versions "
                "WHERE content_sha256 = ?",
                (hashlib.sha256(master_content).hexdigest(),),
            ).one()
    finally:
        target.dispose()

    assert report.rewritten_references
    assert str(workspace_root).startswith(str(target_root))
    assert artifacts
    for storage_uri, digest, byte_size, status in artifacts:
        path = Path(str(storage_uri).removeprefix("file://"))
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == digest
        assert len(content) == byte_size
        assert status == "verified"
    master_path = Path(str(master.source_uri).removeprefix("file://"))
    master_content = master_path.read_bytes()
    assert str(master_path).startswith(str(target_root))
    assert hashlib.sha256(master_content).hexdigest() == master.content_sha256
    assert len(master_content) == master.byte_size


def test_relocation_refuses_artifact_bytes_that_changed(tmp_path: Path) -> None:
    source_root = tmp_path / "source-data"
    source_path = source_root / "clearscout.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    source_database = Database(source_url)
    try:
        workspace = WorkspaceService(source_database, runtime_storage(source_database)).create(
            slug="changed", name="Changed"
        )
        job_search.commit_evaluation(
            source_database,
            artifact_service(source_database, source_root / "artifacts"),
            evaluation_commit(workspace.id),
        )
        with source_database.engine.connect() as connection:
            uri = str(
                connection.exec_driver_sql("SELECT storage_uri FROM artifacts LIMIT 1").scalar_one()
            )
    finally:
        source_database.dispose()
    Path(uri.removeprefix("file://")).write_bytes(b"changed after indexing")

    target_root = tmp_path / "target-data"
    target_url = f"sqlite+pysqlite:///{target_root / 'clearscout.sqlite3'}"
    upgrade(target_url)
    target = Database(target_url)
    try:
        with pytest.raises(ValidationError, match="does not match its stored SHA-256"):
            import_database(
                source_path,
                target,
                source_data_root=source_root,
                target_data_root=target_root,
            )
    finally:
        target.dispose()


def test_relocation_compensates_promoted_files_when_finalize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source-data"
    source_path = source_root / "clearscout.sqlite3"
    source_url = f"sqlite+pysqlite:///{source_path}"
    upgrade(source_url)
    source_database = Database(source_url)
    try:
        workspace = WorkspaceService(source_database, runtime_storage(source_database)).create(
            slug="fault", name="Fault"
        )
        artifacts = artifact_service(source_database, source_root / "artifacts")
        first = job_search.commit_evaluation(
            source_database, artifacts, evaluation_commit(workspace.id)
        )
        job_search.commit_evaluation(
            source_database,
            artifacts,
            evaluation_commit(workspace.id).model_copy(
                update={
                    "rationale": "A second synthetic evaluation.",
                    "supersedes_evaluation_id": first.evaluation_id,
                }
            ),
        )
    finally:
        source_database.dispose()

    target_root = tmp_path / "target-data"
    target_url = f"sqlite+pysqlite:///{target_root / 'clearscout.sqlite3'}"
    upgrade(target_url)
    target = Database(target_url)
    original_finalize = StagedArtifact.finalize
    calls = 0

    def fail_second(artifact: StagedArtifact):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic finalize failure")
        return original_finalize(artifact)

    monkeypatch.setattr(StagedArtifact, "finalize", fail_second)
    try:
        with pytest.raises(OSError, match="synthetic finalize failure"):
            import_database(
                source_path,
                target,
                source_data_root=source_root,
                target_data_root=target_root,
            )
        with target.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM workspaces").scalar_one() == 0
    finally:
        target.dispose()

    assert calls >= 2
    assert not [
        path
        for path in (target_root / "artifacts").rglob("*")
        if path.is_file() and ".staging" not in path.parts
    ]
