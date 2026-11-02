from __future__ import annotations

import ast
import importlib
import json
import re
import stat
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import cast, get_args

import pytest
from sqlalchemy import CheckConstraint, Table, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from tailorcv.infrastructure.persistence.database import (
    Database,
    register_session_finalizer,
    sqlite_path,
    sqlite_uri,
)
from tailorcv.infrastructure.persistence.migration import downgrade, status, upgrade
from tailorcv.infrastructure.persistence.types import UTCDateTime
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import (
    NOW,
    pending_posting_content,
    workspace_with_committed_evaluation,
)


@pytest.mark.parametrize(
    "closing_date",
    [
        None,
        {"statements": []},
        {
            "statements": [
                {
                    "wording": "Closing date: 2026-10-19",
                    "basis": "body_label",
                    "normalized_value": "2026-10-19",
                    "precision": "date",
                }
            ],
            "source_url": "https://example.test/jobs/lead",
            "observed_at": NOW.isoformat(),
        },
    ],
)
def test_publisher_dates_round_trip_without_inventing_missing_evidence(
    durable_database: Database, tmp_path: Path, closing_date: dict[str, object] | None
) -> None:
    from datetime import date

    from tailorcv.infrastructure.persistence.models import (
        PendingDiscovery,
        PendingPostingContent,
        PostingObservation,
    )

    committed = workspace_with_committed_evaluation(durable_database, tmp_path, "closing-facts")
    with durable_database.session() as session:
        pending = PendingDiscovery(
            workspace_id=committed.workspace_id,
            fingerprint="c" * 64,
            employer="Acme",
            title="Operations Lead",
            reason="Publisher-listed role",
            first_seen_on=date(2026, 10, 8),
        )
        session.add(pending)
        session.flush()
        content = pending_posting_content(
            committed.workspace_id,
            pending.id,
            "https://example.test/jobs/lead",
            publisher_closing_date=closing_date,
        )
        observation = PostingObservation(
            job_posting_id=committed.detail.job_posting_id,
            observed_at=NOW,
            posting_status="active",
            publisher_closing_date=closing_date,
        )
        session.add_all((content, observation))
        session.flush()
        content_id, observation_id = content.id, observation.id
    with durable_database.session() as session:
        stored_content = session.get(PendingPostingContent, content_id)
        stored_observation = session.get(PostingObservation, observation_id)
        assert stored_content is not None and stored_observation is not None
        assert stored_content.publisher_closing_date == closing_date
        assert stored_observation.publisher_closing_date == closing_date
    for table in ("pending_posting_contents", "posting_observations"):
        column = next(
            item
            for item in inspect(durable_database.engine).get_columns(table)
            if item["name"] == "publisher_closing_date"
        )
        assert column["nullable"] and column["default"] is None


@pytest.mark.parametrize("value", [None, "2026-10-19"])
def test_personal_closing_dates_and_explicit_resets_survive_reload(
    durable_database: Database, tmp_path: Path, value: str | None
) -> None:
    from datetime import date

    from tailorcv.infrastructure.persistence.models import JobPosting, PendingDiscovery

    committed = workspace_with_committed_evaluation(durable_database, tmp_path, "personal-date")
    closing_date = date.fromisoformat(value) if value else None
    with durable_database.session() as session:
        job = session.get_one(JobPosting, committed.detail.job_posting_id)
        job.closing_date_override = closing_date
        job.closing_date_override_updated_at = NOW
        pending = PendingDiscovery(
            workspace_id=committed.workspace_id,
            fingerprint="d" * 64,
            employer="Acme",
            title="Operations Lead",
            reason="Publisher-listed role",
            first_seen_on=NOW.date(),
            closing_date_override=closing_date,
            closing_date_override_updated_at=NOW,
        )
        session.add(pending)
        session.flush()
        pending_id = pending.id
    with durable_database.session() as session:
        for row in (
            session.get_one(JobPosting, committed.detail.job_posting_id),
            session.get_one(PendingDiscovery, pending_id),
        ):
            assert row.closing_date_override == closing_date
            assert row.closing_date_override_updated_at == NOW
    for table in ("pending_discoveries", "job_postings"):
        columns = {
            item["name"]: item for item in inspect(durable_database.engine).get_columns(table)
        }
        for name in ("closing_date_override", "closing_date_override_updated_at"):
            assert columns[name]["nullable"] and columns[name]["default"] is None


def test_explicit_migration_cycle_is_private_and_configures_sqlite(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "tailorcv.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path}"

    before = status(database_url)
    assert not before.database_exists
    assert not database_path.exists()

    current = upgrade(database_url)
    assert current.current
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert sqlite_path(database_url) == database_path.resolve()

    database = Database(database_url)
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 30_000
    finally:
        database.dispose()

    base = downgrade(database_url)
    assert base.database_exists
    assert base.current_revision is None
    database = Database(database_url)
    try:
        with database.engine.connect() as connection:
            tables = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).scalars()
            )
    finally:
        database.dispose()
    assert "evaluation_currentness_reviews" not in tables


def test_scan_outcome_url_lookup_searches_the_scan_index(durable_database: Database) -> None:
    from tailorcv.infrastructure.persistence.models import SourceScan, SourceScanOutcome
    from tests.support.scenarios import same_day_rescans_with_follow_ups

    workspace_id, first_task_id, second_task_id = same_day_rescans_with_follow_ups(durable_database)
    expected_urls = {
        f"https://jobs.example/globex/{title}"
        for title in (
            "alpha-scan-lead",
            "alpha-follow-up-lead",
            "alpha-chained-lead",
            "bravo-scan-lead",
            "bravo-follow-up-lead",
        )
    }
    with durable_database.session() as session:
        scan_id = session.scalars(
            select(SourceScan.id).where(SourceScan.correlation_id == second_task_id)
        ).one()
        session.add_all(
            SourceScanOutcome(
                workspace_id=workspace_id,
                task_id=first_task_id,
                source_name="Globex",
                candidate_kind="board_listing",
                url=f"https://jobs.example/globex/unlinked-{number}",
                normalized_url=f"https://jobs.example/globex/unlinked-{number}",
                terminal_stage="rejected",
                reason="insufficient_title_evidence",
            )
            for number in range(128)
        )
    statement = select(SourceScanOutcome.url).where(SourceScanOutcome.source_scan_id == scan_id)
    sql = str(statement.compile(durable_database.engine, compile_kwargs={"literal_binds": True}))
    with durable_database.engine.connect() as connection:
        assert set(connection.scalars(statement)) == expected_urls
        plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}").all()
    details = [row[3] for row in plan]
    assert any(
        "SEARCH source_scan_outcomes USING INDEX ix_source_scan_outcome_source_scan" in detail
        and "(source_scan_id=?)" in detail
        for detail in details
    ), details
    assert not any("SCAN source_scan_outcomes" in detail for detail in details), details


def test_detail_rows_of_one_board_store_its_listing_membership_once(
    durable_database: Database,
) -> None:
    from tailorcv.application.discovery.source_reading import SourceDetailCheckpoint
    from tailorcv.infrastructure.persistence.models import (
        SourceListingMembership,
        SourceScanFrontierItem,
    )
    from tests.support.scenarios import (
        board_source,
        completed_earlier_scan,
        scan_workspace,
        seed_frontier_rows,
    )

    workspace_id = scan_workspace(durable_database, slug="membership")
    board = "https://jobs.example/globex"
    source_id = board_source(durable_database, workspace_id, "Globex", board)
    scan_id = completed_earlier_scan(durable_database, workspace_id, source_id, ())
    listing = tuple((f"req-{n}", f"{board}/req-{n}") for n in range(300))

    def checkpoint(identity: str) -> dict[str, object]:
        return SourceDetailCheckpoint(
            platform="workday",
            board_url=board,
            pending_ids=(identity,),
            failed_attempts=((identity, 1),),
            listing_ids=tuple(item for item, _url in listing),
            listing_urls=listing,
            listing_total=len(listing),
        ).as_json()

    detail_rows = listing[:40]
    rows: list[dict[str, object]] = [
        {
            "url": url,
            "deferred_reason": "workday_detail_pending",
            "detail_checkpoint": checkpoint(identity),
        }
        for identity, url in detail_rows
    ]
    with durable_database.session() as session:
        continuation_id = _continuation_of(session, scan_id)
        seed_frontier_rows(session, continuation_id, [*rows, {"url": f"{board}/plain"}])
    with durable_database.session() as session:
        stored = {
            row.url: row.detail_checkpoint
            for row in session.scalars(
                select(SourceScanFrontierItem).where(
                    SourceScanFrontierItem.continuation_id == continuation_id
                )
            )
        }
        with_checkpoint = session.scalars(
            select(SourceScanFrontierItem.url).where(
                SourceScanFrontierItem.continuation_id == continuation_id,
                SourceScanFrontierItem.detail_checkpoint.is_not(None),
            )
        ).all()
    assert stored == {url: checkpoint(identity) for identity, url in detail_rows} | {
        f"{board}/plain": None
    }
    assert set(with_checkpoint) == {url for _identity, url in detail_rows}
    with durable_database.engine.connect() as connection:
        row_bytes = connection.execute(
            text(
                "SELECT sum(length(detail_checkpoint)) FROM source_scan_frontier_items "
                "WHERE continuation_id = :id"
            ),
            {"id": continuation_id},
        ).scalar_one()
        memberships = connection.execute(select(SourceListingMembership.sha256)).scalars().all()
    one_checkpoint = len(json.dumps(checkpoint(detail_rows[0][0])))
    assert row_bytes < one_checkpoint
    assert len(memberships) == 1


def test_a_listing_membership_no_frontier_row_names_is_released(
    durable_database: Database,
) -> None:
    from sqlalchemy import delete

    from tailorcv.application.discovery.source_reading import SourceDetailCheckpoint
    from tailorcv.infrastructure.persistence.models import (
        SourceListingMembership,
        SourceScanFrontierItem,
    )
    from tests.support.scenarios import (
        board_source,
        completed_earlier_scan,
        scan_workspace,
        seed_frontier_rows,
    )

    workspace_id = scan_workspace(durable_database, slug="released")
    board = "https://jobs.example/initech"
    source_id = board_source(durable_database, workspace_id, "Initech", board)
    scan_id = completed_earlier_scan(durable_database, workspace_id, source_id, ())
    later_scan_id = completed_earlier_scan(durable_database, workspace_id, source_id, (), day=0)

    def checkpoint(size: int) -> dict[str, object]:
        listing = tuple((f"req-{n}", f"{board}/req-{n}") for n in range(size))
        return SourceDetailCheckpoint(
            platform="workday",
            board_url=board,
            pending_ids=("req-0",),
            listing_ids=tuple(item for item, _url in listing),
            listing_urls=listing,
        ).as_json()

    with durable_database.session() as session:
        continuation_id = _continuation_of(session, scan_id)
        seed_frontier_rows(
            session,
            continuation_id,
            [{"url": f"{board}/req-0", "detail_checkpoint": checkpoint(3)}],
        )
    with durable_database.session() as session:
        row = session.scalars(
            select(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id
            )
        ).one()
        row.detail_checkpoint = checkpoint(4)
    with durable_database.session() as session:
        assert len(session.scalars(select(SourceListingMembership)).all()) == 1
        session.execute(
            delete(SourceScanFrontierItem).where(
                SourceScanFrontierItem.continuation_id == continuation_id
            )
        )
        seed_frontier_rows(
            session, _continuation_of(session, later_scan_id), [{"url": f"{board}/fresh"}]
        )
    with durable_database.session() as session:
        stored = session.scalars(
            select(SourceScanFrontierItem).where(SourceScanFrontierItem.url == f"{board}/fresh")
        ).one()
        assert stored.detail_checkpoint is None
        assert session.scalars(select(SourceListingMembership)).all() == []


def test_a_checkpoint_reads_whole_while_another_writer_replaces_its_listing(
    durable_database: Database,
) -> None:
    from sqlalchemy import event

    from tailorcv.application.discovery.source_reading import SourceDetailCheckpoint
    from tailorcv.infrastructure.persistence.models import SourceScanFrontierItem
    from tests.support.scenarios import (
        board_source,
        completed_earlier_scan,
        scan_workspace,
        seed_frontier_rows,
    )

    workspace_id = scan_workspace(durable_database, slug="torn")
    board = "https://jobs.example/umbrella"
    source_id = board_source(durable_database, workspace_id, "Umbrella", board)
    scan_id = completed_earlier_scan(durable_database, workspace_id, source_id, ())

    def checkpoint(size: int) -> dict[str, object]:
        listing = tuple((f"req-{n}", f"{board}/req-{n}") for n in range(size))
        return SourceDetailCheckpoint(
            platform="workday",
            board_url=board,
            pending_ids=("req-0",),
            listing_ids=tuple(item for item, _url in listing),
            listing_urls=listing,
        ).as_json()

    with durable_database.session() as session:
        continuation_id = _continuation_of(session, scan_id)
        seed_frontier_rows(
            session,
            continuation_id,
            [{"url": f"{board}/req-0", "detail_checkpoint": checkpoint(3)}],
        )
    writer = Database(durable_database.url)
    replaced: list[bool] = []

    def replace_between_reads(_conn: object, _cursor: object, sql: str, *_args: object) -> None:
        if "FROM source_listing_memberships" in sql and not replaced:
            replaced.append(True)
            with writer.session() as other:
                row = other.scalars(select(SourceScanFrontierItem)).one()
                row.detail_checkpoint = checkpoint(4)

    event.listen(durable_database.engine, "before_cursor_execute", replace_between_reads)
    try:
        with durable_database.session() as session:
            row = session.scalars(select(SourceScanFrontierItem)).one()
            read = row.detail_checkpoint
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", replace_between_reads)
        writer.dispose()
    assert read in (checkpoint(3), checkpoint(4))


def _continuation_of(session: Session, scan_id: str) -> str:
    from tailorcv.infrastructure.persistence.models import SourceScanContinuation

    return session.scalars(
        select(SourceScanContinuation.id).where(SourceScanContinuation.source_scan_id == scan_id)
    ).one()


@pytest.mark.parametrize(
    ("model_name", "index_name", "columns"),
    [
        ("SourceScanOutcome", "ix_source_scan_outcome_source_scan", ("source_scan_id",)),
        ("BackgroundTask", "ix_task_type_started", ("task_type", "started_at")),
        (
            "SourceScanFrontierItem",
            "ix_source_frontier_listing_membership",
            ("listing_membership_sha256",),
        ),
    ],
)
def test_lookup_indexes_match_the_migrated_schema(
    durable_database: Database, model_name: str, index_name: str, columns: tuple[str, ...]
) -> None:
    from tailorcv.infrastructure.persistence import models

    table = cast(Table, getattr(models, model_name).__table__)
    mapped = {
        index.name: (tuple(column.name for column in index.columns), index.unique)
        for index in table.indexes
    }
    migrated = {
        index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
        for index in inspect(durable_database.engine).get_indexes(table.name)
    }
    assert mapped == migrated
    assert migrated[index_name] == (columns, False)


def test_the_claim_searches_recent_continuations_through_the_start_index(
    durable_database: Database,
) -> None:
    from sqlalchemy import event

    from tailorcv.application.tasks import TaskQueueService
    from tailorcv.application.workspaces import WorkspaceService

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="claims", name="Claims"
    )
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=str(workspace.id),
        task_type="artifact.verify",
        payload={},
        idempotency_key="claim-plan",
    )
    statements: list[tuple[str, object]] = []

    def capture(_conn: object, _cursor: object, sql: str, params: object, *_args: object) -> None:
        if "FROM background_tasks" in sql and "ORDER BY" in sql:
            statements.append((sql, params))

    event.listen(durable_database.engine, "before_cursor_execute", capture)
    try:
        assert queue.claim_next(worker_id="worker-a", lease_seconds=10) is not None
    finally:
        event.remove(durable_database.engine, "before_cursor_execute", capture)
    sql, params = statements[0]
    with durable_database.engine.connect() as connection:
        plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", params).all()  # type: ignore[arg-type]
    details = [row[3] for row in plan]
    assert any("INDEX ix_task_type_started (task_type=? AND started_at>?)" in d for d in details), (
        "\n".join(details)
    )


def test_database_requires_an_explicit_upgrade(tmp_path: Path) -> None:
    from tailorcv.shared.errors import ConfigError

    database_url = f"sqlite+pysqlite:///{tmp_path / 'missing.sqlite3'}"
    with pytest.raises(ConfigError, match="db upgrade"):
        Database(database_url)


def test_an_engine_opened_early_never_creates_the_database(tmp_path: Path) -> None:
    database_path = tmp_path / "a dir #1" / "tailorcv.sqlite3"
    database_path.parent.mkdir()
    database_url = f"sqlite+pysqlite:///{database_path}"
    database = Database(database_url, require_existing=False)
    try:
        with pytest.raises(OperationalError):
            database.check()
        assert not database_path.exists()

        upgrade(database_url)
        with database.session() as session:
            session.execute(text("CREATE TABLE early (id INTEGER)"))
            session.execute(text("INSERT INTO early VALUES (1)"))
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
    finally:
        database.dispose()
    reopened = Database(database_url)
    try:
        with reopened.engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT id FROM early").scalar_one() == 1
    finally:
        reopened.dispose()


@pytest.mark.parametrize(
    ("path", "uri"),
    [
        (PurePosixPath("/data/a b#1/t.db"), "file:///data/a%20b%231/t.db?mode=ro"),
        (PureWindowsPath(r"C:\Data\t.db"), "file:///C:/Data/t.db?mode=ro"),
        (PureWindowsPath(r"\\server\share\t.db"), "file:////server/share/t.db?mode=ro"),
    ],
)
def test_sqlite_uri_keeps_every_path_in_the_path_component(path: PurePath, uri: str) -> None:
    assert sqlite_uri(path, "ro") == uri


def test_a_write_locked_session_settles_staged_files_like_any_other(
    durable_database: Database,
) -> None:
    """Both session factories put filesystem work on the same side of the outcome.

    A finalizer registered on the exclusive session must run, or a caller that
    staged a file leaves it behind whatever the database did.
    """
    settled: list[str] = []

    with durable_database.exclusive_session() as session:
        register_session_finalizer(
            session,
            after_commit=lambda: settled.append("committed"),
            after_rollback=lambda: settled.append("rolled back"),
        )

    assert settled == ["committed"]

    def register_then_fail() -> None:
        with durable_database.exclusive_session() as session:
            register_session_finalizer(
                session,
                after_commit=lambda: settled.append("committed again"),
                after_rollback=lambda: settled.append("rolled back"),
            )
            raise RuntimeError("the use case failed")

    with pytest.raises(RuntimeError):
        register_then_fail()

    assert settled == ["committed", "rolled back"]


def test_immutable_trigger_refuses_task_event_updates(durable_database: Database) -> None:
    from tailorcv.application.tasks import TaskQueueService
    from tailorcv.application.workspaces import WorkspaceService

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="test", name="Test"
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace.id,
        task_type="artifact.verify",
        payload={"artifact_id": "example"},
        idempotency_key="one",
    )

    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE task_events SET stage = 'changed' WHERE task_id = :task_id"),
            {"task_id": task.id},
        )

    with durable_database.session() as session:
        stage = session.execute(
            text("SELECT stage FROM task_events WHERE task_id = :task_id"),
            {"task_id": task.id},
        ).scalar_one()
    assert stage == "queued"


def test_no_stored_schema_object_calls_lower(durable_database: Database) -> None:
    """An unregistered connection folds only A-Z in lower(), so no schema object may call it."""
    with durable_database.session() as session:
        objects = session.execute(
            text("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        ).all()

    assert objects
    calls = re.compile(r"\blower\s*\(", re.IGNORECASE)
    assert [(kind, name) for kind, name, sql in objects if calls.search(sql)] == []


def test_every_model_module_reaches_the_one_metadata_graph() -> None:
    """A table nobody imports is a table Alembic and create_all cannot see.

    The models are split across modules, so the failure this guards is adding a
    module and forgetting to import it in the package: the class would map, but
    its table would be missing from the graph the migration is checked against.
    """
    package = importlib.import_module("tailorcv.infrastructure.persistence.models")
    root = Path(next(iter(package.__path__)))

    # Read rather than import: importing the modules here would perform the very
    # registration this asserts the package already performed.
    declared: dict[str, str] = {}
    for path in sorted(root.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if (
                    isinstance(statement, ast.Assign)
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id == "__tablename__"
                    and isinstance(statement.value, ast.Constant)
                ):
                    declared[node.name] = str(statement.value.value)

    assert declared, "no mapped classes were found beside the package"
    assert (
        sorted(table for table in declared.values() if table not in package.Base.metadata.tables)
        == []
    )
    assert sorted(name for name in declared if not hasattr(package, name)) == []
    assert sorted(name for name in declared if name not in package.__all__) == []


def test_pending_content_evidence_is_append_only_typed_and_owner_scoped(
    durable_database: Database,
) -> None:
    from datetime import date

    from tailorcv.application.workspaces import WorkspaceService
    from tailorcv.domain import (
        PostingContentAccessStatus,
        PostingIdentityStatus,
    )
    from tailorcv.infrastructure.persistence.models import (
        PendingDiscovery,
    )

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="pending-content", name="Pending content"
    )
    with durable_database.session() as session:
        first = PendingDiscovery(
            workspace_id=workspace.id,
            fingerprint="a" * 64,
            employer="Acme",
            title="Operations Lead",
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        second = PendingDiscovery(
            workspace_id=workspace.id,
            fingerprint="b" * 64,
            employer="Acme",
            title="Program Lead",
            reason="Publisher-listed role",
            first_seen_on=date(2026, 8, 28),
        )
        session.add_all((first, second))
        session.flush()
        evidence = pending_posting_content(
            workspace.id,
            first.id,
            "https://example.test/jobs/operations-lead",
            content_status=PostingContentAccessStatus.ACCESS_BLOCKED,
            identity_status=PostingIdentityStatus.PUBLISHER_LISTED,
            http_status=403,
            publisher_markup_present=None,
            identity_evidence={"publisher_link": True},
        )
        session.add(evidence)
        session.flush()
        first.current_content_id = evidence.id
        second_id = second.id
        evidence_id = evidence.id

    def claim_content_of_another_discovery() -> None:
        with durable_database.session() as session:
            other = session.get(PendingDiscovery, second_id)
            assert other is not None
            other.current_content_id = evidence_id

    with pytest.raises(IntegrityError):
        claim_content_of_another_discovery()

    with (
        pytest.raises(IntegrityError),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE pending_posting_contents SET http_status = 401 WHERE id = :content_id"),
            {"content_id": evidence_id},
        )

    with (
        pytest.raises(IntegrityError),
        durable_database.session() as session,
    ):
        session.add(
            pending_posting_content(
                workspace.id,
                second_id,
                "https://example.test/jobs/program-lead",
                content_status=PostingContentAccessStatus.NOT_YET_READ,
                identity_status="assumed",
                identity_evidence={},
            )
        )


def test_upgrading_a_database_holding_model_calls_keeps_them_beside_the_envelope_tables(
    durable_database_url: str, tmp_path: Path
) -> None:
    """Running the migrations over a live workspace must leave its recorded calls alone."""
    from tailorcv.application.workspaces import WorkspaceService
    from tailorcv.infrastructure.persistence.models import ModelCall

    database = Database(durable_database_url)
    try:
        workspace_id = str(
            WorkspaceService(database, runtime_storage(database))
            .create(slug="live", name="Live", root_path=str(tmp_path))
            .id
        )
        with database.session() as session:
            session.add(
                ModelCall(
                    workspace_id=workspace_id,
                    stage="draft",
                    provider="synthetic",
                    requested_model="m",
                )
            )
    finally:
        database.dispose()

    assert upgrade(durable_database_url).current

    database = Database(durable_database_url)
    try:
        schema = inspect(database.engine)
        columns = {column["name"] for column in schema.get_columns("model_calls")}
        tables = set(schema.get_table_names())
        with database.session() as session:
            existing = session.scalars(select(ModelCall)).all()
    finally:
        database.dispose()

    assert {"cache_key", "cached_at", "response_digest"} <= columns
    assert [(call.stage, call.cache_key, call.response_digest) for call in existing] == [
        ("draft", None, None)
    ]
    assert {"model_call_envelopes", "response_cache_archive"} <= tables


def test_a_stored_envelope_can_be_pruned_while_its_model_call_stays(
    durable_database: Database, tmp_path: Path
) -> None:
    """Envelopes are the bulk of a workspace, and the call row is the history."""
    from tailorcv.application.workspaces import WorkspaceService
    from tailorcv.infrastructure.persistence.models import ModelCall, ModelCallEnvelope

    workspace_id = str(
        WorkspaceService(durable_database, runtime_storage(durable_database))
        .create(slug="pruning", name="Pruning", root_path=str(tmp_path))
        .id
    )
    with durable_database.session() as session:
        call = ModelCall(
            workspace_id=workspace_id,
            stage="draft",
            provider="synthetic",
            requested_model="m",
        )
        session.add(call)
        session.flush()
        session.add(
            ModelCallEnvelope(
                model_call_id=call.id,
                request_envelope={"messages": []},
                response_envelope={"content": []},
            )
        )
        call_id = call.id

    with durable_database.session() as session:
        session.execute(
            text("DELETE FROM model_call_envelopes WHERE model_call_id = :call_id"),
            {"call_id": call_id},
        )

    with durable_database.session() as session:
        assert session.scalars(select(ModelCallEnvelope)).all() == []
        assert [row.id for row in session.scalars(select(ModelCall)).all()] == [call_id]

    with (
        pytest.raises(IntegrityError, match="model_calls is immutable"),
        durable_database.session() as session,
    ):
        session.execute(text("DELETE FROM model_calls WHERE id = :call_id"), {"call_id": call_id})


def test_unreleased_schema_omits_superseded_lineage_and_score_fields(
    durable_database: Database,
) -> None:
    from sqlalchemy import inspect

    schema = inspect(durable_database.engine)
    retired = {
        "background_tasks": "provider_response_id",
        "job_evaluations": "prompt_version_id",
        "evaluation_score_evidence": "contribution",
        "posting_observations": "identity_trust",
        "evaluation_factors": "gap_class",
    }

    for table, field in retired.items():
        assert field not in {column["name"] for column in schema.get_columns(table)}


def test_migrated_check_vocabularies_match_the_mappers_closed_lists(
    durable_database: Database,
) -> None:
    """A CHECK the mapper widened alone is a value the migrated database still refuses."""
    from tailorcv.application.job_search.models import RelevanceReviewRejectionCode
    from tailorcv.domain import ARTIFACT_LOGICAL_TYPES, ROBOTS_READ_BASES, STORED_TASK_TYPES
    from tailorcv.infrastructure.persistence.models import (
        Artifact,
        BackgroundTask,
        GuidedCampaignCapturedUrl,
        PendingPostingContent,
        PostingObservation,
        RelevanceReviewRejection,
    )

    expected = {
        (Artifact, "ck_artifact_logical_type"): ARTIFACT_LOGICAL_TYPES,
        (BackgroundTask, "ck_task_type"): STORED_TASK_TYPES,
        (PostingObservation, "ck_observation_robots_basis"): ROBOTS_READ_BASES,
        (PendingPostingContent, "ck_pending_content_robots_basis"): ROBOTS_READ_BASES,
        (GuidedCampaignCapturedUrl, "ck_guided_captured_url_robots_basis"): ROBOTS_READ_BASES,
        (RelevanceReviewRejection, "ck_relevance_review_rejection_vocab"): get_args(
            RelevanceReviewRejectionCode
        ),
    }
    database_schema = inspect(durable_database.engine)

    for (model, name), vocabulary in expected.items():
        table = cast(Table, model.__table__)
        migrated = {
            constraint["name"]: str(constraint["sqltext"])
            for constraint in database_schema.get_check_constraints(table.name)
        }
        mapped = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert re.findall(r"'([^']*)'", migrated[name]) == list(vocabulary)
        assert re.findall(r"'([^']*)'", mapped[name]) == list(vocabulary)


def test_relevance_reviews_keep_history_with_nullable_contract_digests(
    durable_database: Database,
) -> None:
    """Rows written before the digests existed hold NULL and read as out of date."""
    schema = inspect(durable_database.engine)
    columns = {column["name"]: column for column in schema.get_columns("relevance_reviews")}
    unique = {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("relevance_reviews")
    }
    checks = {
        constraint["name"] for constraint in schema.get_check_constraints("relevance_reviews")
    }

    assert columns["instructions_digest"]["nullable"] is True
    assert columns["schema_digest"]["nullable"] is True
    assert ("pending_discovery_id",) not in unique
    assert "ix_relevance_review_latest" in {
        index["name"] for index in schema.get_indexes("relevance_reviews")
    }
    assert "ck_relevance_review_classes_together" in checks
    for name in (
        "recommendation",
        "core_work_alignment",
        "core_qualification_alignment",
        "hard_condition",
        "decisive_basis",
    ):
        assert columns[name]["nullable"] is True


def test_a_relevance_review_holds_all_five_classes_or_none(durable_database: Database) -> None:
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from tailorcv.infrastructure.persistence.models.discovery import RelevanceReview

    moment = datetime(2026, 1, 1, tzinfo=UTC)
    with durable_database.engine.connect() as connection:
        # Foreign keys are off so the row needs no workspace; the check still applies.
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        session = Session(bind=connection)
        session.add(
            RelevanceReview(
                workspace_id="ws",
                pending_discovery_id="pd",
                task_id="task",
                master_cv_version_id="cv",
                search_parameter_set_id="params",
                recommendation="review",
                summary="Half classified.",
                core_work_alignment="direct",
                posting_citations=[],
                master_cv_evidence_ids=[],
                unresolved_questions=[],
                fetched_at=moment,
                source_url_sha256="a" * 64,
                body_sha256="b" * 64,
                search_context_sha256="c" * 64,
                reviewed_at=moment,
            )
        )
        with pytest.raises(IntegrityError, match="ck_relevance_review_classes_together"):
            session.flush()


def test_a_read_exempt_as_one_postings_own_read_stores_that_basis(
    durable_database: Database,
) -> None:
    """The exemption a known-posting read carries is the only non-allowed basis a row may hold."""
    from datetime import date

    from tailorcv.application.workspaces import WorkspaceService
    from tailorcv.domain import PostingIdentityStatus
    from tailorcv.domain.records import RobotsReadBasis
    from tailorcv.infrastructure.persistence.models import PendingDiscovery, PendingPostingContent

    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="robots-basis", name="Robots basis"
    )
    with durable_database.session() as session:
        pending = PendingDiscovery(
            workspace_id=workspace.id,
            fingerprint="e" * 64,
            employer="Acme",
            title="Operations Lead",
            reason="Publisher-listed role",
            first_seen_on=date(2026, 9, 18),
        )
        session.add(pending)
        session.flush()
        content = pending_posting_content(
            workspace.id,
            pending.id,
            "https://example.test/jobs/operations-lead",
            identity_status=PostingIdentityStatus.PUBLISHER_LISTED,
            robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
            identity_evidence={"publisher_link": True},
        )
        session.add(content)
        session.flush()
        pending_id = pending.id

    with durable_database.session() as session:
        stored = session.scalars(select(PendingPostingContent)).all()
        assert [row.robots_basis for row in stored] == [RobotsReadBasis.SINGLE_POSTING_READ]

    with (
        pytest.raises(IntegrityError),
        durable_database.session() as session,
    ):
        session.add(
            pending_posting_content(
                workspace.id,
                pending_id,
                "https://example.test/jobs/operations-lead",
                identity_status=PostingIdentityStatus.PUBLISHER_LISTED,
                robots_basis="documented_api_read",
                identity_evidence={"publisher_link": True},
            )
        )


def test_derived_proposal_mappers_match_the_migration_schema(
    durable_database: Database,
) -> None:
    from sqlalchemy import ColumnDefault, Table, inspect

    from tailorcv.infrastructure.persistence.models import (
        DerivedCluster,
        DerivedEmployerArchetype,
        DerivedSearchTitle,
    )

    lifecycle = ("position", "state", "decided_at", "rejection_reason", "carried_identity")
    identity_and_audit = ("id", "created_at", "created_by", "origin", "correlation_id")
    expected_columns = {
        DerivedSearchTitle: (
            "workspace_id",
            "master_cv_version_id",
            "title",
            "normalized_title",
            "reason",
            "evidence_ids",
            "lane",
            *lifecycle,
            *identity_and_audit,
        ),
        DerivedEmployerArchetype: (
            "workspace_id",
            "master_cv_version_id",
            "name",
            "normalized_name",
            "because",
            "evidence_ids",
            *lifecycle,
            *identity_and_audit,
        ),
        DerivedCluster: (
            "workspace_id",
            "master_cv_version_id",
            "kind",
            "name",
            "normalized_name",
            "terms",
            "because",
            "evidence_ids",
            *lifecycle,
            *identity_and_audit,
        ),
    }
    expected_constraints = {
        DerivedSearchTitle: {
            "ck_search_title_lane",
            "ck_search_title_rejection_reason",
            "ck_search_title_state",
            "uq_derived_title",
        },
        DerivedEmployerArchetype: {
            "ck_search_archetype_rejection_reason",
            "ck_search_archetype_state",
            "uq_derived_archetype",
        },
        DerivedCluster: {
            "ck_cluster_kind",
            "ck_cluster_rejection_reason",
            "ck_cluster_state",
            "uq_derived_cluster",
        },
    }
    database_schema = inspect(durable_database.engine)

    for model, column_names in expected_columns.items():
        table = cast(Table, model.__table__)
        migrated_columns = database_schema.get_columns(table.name)
        assert tuple(table.columns.keys()) == column_names
        assert tuple(column["name"] for column in migrated_columns) == column_names
        assert tuple(column["nullable"] for column in migrated_columns) == tuple(
            column.nullable for column in table.columns
        )
        assert tuple(str(column["type"]) for column in migrated_columns) == tuple(
            str(column.type) for column in table.columns
        )
        assert {constraint.name for constraint in table.constraints if constraint.name} == (
            expected_constraints[model]
        )
        assert {
            constraint["name"] for constraint in database_schema.get_check_constraints(table.name)
        } | {
            constraint["name"] for constraint in database_schema.get_unique_constraints(table.name)
        } == expected_constraints[model]
        assert list(table.indexes) == []
        assert database_schema.get_indexes(table.name) == []

        defaults: list[object] = []
        for name in ("evidence_ids", *lifecycle):
            default = table.c[name].default
            argument = cast(ColumnDefault, default).arg if default is not None else None
            defaults.append(getattr(argument, "__name__", argument))
        assert defaults == ["list", 0, "proposed", None, None, None]


def test_database_rejects_unanchored_evidence(durable_database: Database, tmp_path: Path) -> None:
    detail = workspace_with_committed_evaluation(durable_database, tmp_path, "anchor").detail
    with (
        pytest.raises(IntegrityError, match="ck_match_evidence_anchor"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO match_evidence "
                "(evaluation_id, evidence_text, locator, source_kind, strength, "
                "classification, id, created_at, created_by, origin) VALUES "
                "(:evaluation_id, 'paraphrase', '', 'posting', 'strong', "
                "'DIRECT', 'bad-evidence', :created_at, 'test', 'test')"
            ),
            {
                "evaluation_id": detail.evaluation_id,
                "created_at": UTCDateTime.serialize(NOW),
            },
        )


def test_database_rejects_evidence_naming_a_requirement_without_its_identity(
    durable_database: Database, tmp_path: Path
) -> None:
    detail = workspace_with_committed_evaluation(durable_database, tmp_path, "identity").detail
    with (
        pytest.raises(IntegrityError, match="ck_match_evidence_requirement_identity"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO match_evidence "
                "(evaluation_id, artifact_id, requirement_id, requirement_key, evidence_text, "
                "char_start, char_end, locator, source_kind, strength, classification, id, "
                "created_at, created_by, origin) "
                "SELECT :evaluation_id, artifact_id, 'r01', NULL, evidence_text, char_start, "
                "char_end, locator, source_kind, strength, classification, 'half-identified', "
                ":created_at, 'test', 'test' FROM match_evidence LIMIT 1"
            ),
            {
                "evaluation_id": detail.evaluation_id,
                "created_at": UTCDateTime.serialize(NOW),
            },
        )


def test_database_rejects_noncanonical_posting_and_score_vocabularies(
    durable_database: Database, tmp_path: Path
) -> None:
    detail = workspace_with_committed_evaluation(durable_database, tmp_path, "vocabulary").detail
    created_at = UTCDateTime.serialize(NOW)
    with (
        pytest.raises(IntegrityError, match="ck_posting_status"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO posting_observations "
                "(job_posting_id, observed_at, posting_status, location, residence_rule, "
                "employment_type, payroll_arrangement, id, created_at, created_by, origin) "
                "VALUES (:job_id, :created_at, 'unverified', '', '', '', '', "
                "'bad-observation', :created_at, 'test', 'test')"
            ),
            {"job_id": detail.job_posting_id, "created_at": created_at},
        )
    with (
        pytest.raises(IntegrityError, match="ck_evaluation_score_component"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO evaluation_scores "
                "(evaluation_id, component, score, finding_state, id, created_at, "
                "created_by, origin) VALUES (:evaluation_id, 'overall', 88, 'supported', "
                "'bad-score', :created_at, 'test', 'test')"
            ),
            {"evaluation_id": detail.evaluation_id, "created_at": created_at},
        )
    with (
        pytest.raises(IntegrityError, match="ck_posting_requirement_importance"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO posting_requirements "
                "(job_posting_id, job_evaluation_id, requirement_id, requirement_key, text, "
                "importance, position, id, created_at, created_by, origin) VALUES "
                "(:job_id, :evaluation_id, 'r99', 'invalid-importance-key', 'Synthetic text', "
                "'optional', 99, 'bad-requirement', :created_at, 'test', 'test')"
            ),
            {
                "job_id": detail.job_posting_id,
                "evaluation_id": detail.evaluation_id,
                "created_at": created_at,
            },
        )
    with (
        pytest.raises(IntegrityError, match="ck_posting_requirement_gate_kind"),
        durable_database.session() as session,
    ):
        session.execute(
            text(
                "INSERT INTO posting_requirements "
                "(job_posting_id, job_evaluation_id, requirement_id, requirement_key, text, "
                "importance, position, is_hard_gate, gate_kind, id, created_at, created_by, "
                "origin) VALUES "
                "(:job_id, :evaluation_id, 'r98', 'invalid-gate-kind-key', 'Synthetic gate', "
                "'must_have', 98, 1, 'residency', 'bad-gate', :created_at, 'test', 'test')"
            ),
            {
                "job_id": detail.job_posting_id,
                "evaluation_id": detail.evaluation_id,
                "created_at": created_at,
            },
        )


def test_database_enforces_compensation_currency_and_basis(
    durable_database: Database, tmp_path: Path
) -> None:
    detail = workspace_with_committed_evaluation(durable_database, tmp_path, "pay-check").detail
    insert = text(
        "INSERT INTO compensation_components "
        "(job_posting_id, component_type, currency, basis, geography, description, "
        "id, created_at, created_by, origin) VALUES "
        "(:job_posting_id, 'base', :currency, :basis, '', '', :id, :created_at, 'test', 'test')"
    )
    invalid = (
        ("bad-currency", "usd", "annual", "ck_compensation_currency"),
        ("bad-basis", "USD", "weekly", "ck_compensation_basis"),
    )
    for row_id, currency, basis, constraint in invalid:
        with (
            pytest.raises(IntegrityError, match=constraint),
            durable_database.session() as session,
        ):
            session.execute(
                insert,
                {
                    "job_posting_id": detail.job_posting_id,
                    "currency": currency,
                    "basis": basis,
                    "id": row_id,
                    "created_at": UTCDateTime.serialize(NOW),
                },
            )
