"""Preflight and copy a recognizable TailorCV SQLite database safely."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import ForeignKeyConstraint, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.ddl import sort_tables

from tailorcv.application.failures import ValidationError
from tailorcv.domain import utc_now
from tailorcv.infrastructure.artifacts.store import (
    LocalArtifactStore,
    StagedArtifact,
    file_uri_path,
    uri_scheme,
)
from tailorcv.infrastructure.persistence.database import Database, sqlite_path
from tailorcv.infrastructure.persistence.models import Base
from tailorcv.infrastructure.persistence.types import UTCDateTime

NEVER_COPIED = frozenset({"alembic_version"})
RECOGNIZABLE_TABLES = frozenset(
    {"workspaces", "job_postings", "job_evaluations", "master_cv_versions", "artifacts"}
)
SEEDED_TABLES = frozenset({"score_weight_sets", "score_weight_components"})
ORDERED_BY = {"job_evaluations": ("job_posting_id", "version_number")}
PATH_COLUMNS = {
    ("workspaces", "root_path"),
    ("master_cv_versions", "source_uri"),
    ("artifacts", "storage_uri"),
}


def _import_ordered_tables() -> tuple[Table, ...]:
    """Order parents before children while deferring nullable reverse pointers."""

    def skip_current_pointer(constraint: ForeignKeyConstraint) -> bool:
        return constraint.name in {
            "fk_pending_current_content",
            "fk_source_scan_member_continuation",
        }

    return tuple(sort_tables(Base.metadata.tables.values(), skip_fn=skip_current_pointer))


@dataclass(frozen=True)
class FileRelocation:
    """One immutable, hash-and-size verified file whose reference is remapped."""

    table: str
    record_id: str
    source: Path
    target: Path
    sha256: str
    byte_size: int


@dataclass
class ImportReport:
    """Deterministic preflight plan and, after execution, its result."""

    source: Path
    target: str
    dry_run: bool = False
    copied: dict[str, int] = field(default_factory=dict)
    skipped_tables: dict[str, int] = field(default_factory=dict)
    dropped_columns: dict[str, list[str]] = field(default_factory=dict)
    added_columns: dict[str, list[str]] = field(default_factory=dict)
    already_present: dict[str, int] = field(default_factory=dict)
    rewritten_references: list[str] = field(default_factory=list)
    preserved_references: list[str] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)
    file_relocations: list[FileRelocation] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        """Return how many rows are planned or were copied."""
        return sum(self.copied.values())

    def lines(self) -> list[str]:
        """Return a stable, reviewable report with losses and references first."""
        verb = "Would import" if self.dry_run else "Imported"
        out = [f"{verb} {self.total_rows} rows from {self.source}."]
        for table, count in sorted(self.skipped_tables.items()):
            out.append(f"  skipped {table}: {count} rows, no such table in this schema")
        for table, columns in sorted(self.dropped_columns.items()):
            out.append(f"  dropped from {table}: {', '.join(columns)}")
        for table, columns in sorted(self.added_columns.items()):
            out.append(f"  absent in source {table}: {', '.join(columns)}")
        for reference in sorted(self.unresolved_references):
            out.append(f"  unresolved reference: {reference}")
        for reference in sorted(self.preserved_references):
            out.append(f"  preserved external reference: {reference}")
        for reference in sorted(self.rewritten_references):
            out.append(f"  relocated reference: {reference}")
        for table, count in sorted(self.already_present.items()):
            out.append(f"  identical in {table}: {count} rows skipped")
        for table, count in sorted(self.copied.items()):
            if count:
                out.append(f"  {table}: {count}")
        return out


@dataclass(frozen=True)
class _PreparedTable:
    name: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _ImportPlan:
    report: ImportReport
    tables: tuple[_PreparedTable, ...]
    target_data_root: Path | None
    target_artifact_root: Path | None


def _read_only(path: Path) -> sqlite3.Connection:
    """Open the source so that preflight and copy cannot alter it."""
    uri = path.resolve().as_uri().replace("file://", "file:", 1) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _source_tables(connection: sqlite3.Connection) -> dict[str, list[str]]:
    """Return source tables and columns, quoting catalog-provided names."""
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        name: [str(row["name"]) for row in connection.execute(f'PRAGMA table_info("{name}")')]
        for name in names
        if name not in NEVER_COPIED
    }


def import_database(
    source: Path,
    database: Database,
    *,
    dry_run: bool = False,
    source_data_root: Path | None = None,
    target_data_root: Path | None = None,
    source_artifact_root: Path | None = None,
    target_artifact_root: Path | None = None,
    read_only_import_roots: tuple[Path, ...] = (),
) -> ImportReport:
    """Preflight, optionally relocate immutable bytes, and copy schema-valid rows."""
    plan = _preflight(
        source,
        database,
        dry_run=dry_run,
        source_data_root=source_data_root,
        target_data_root=target_data_root,
        source_artifact_root=source_artifact_root,
        target_artifact_root=target_artifact_root,
        read_only_import_roots=read_only_import_roots,
    )
    if dry_run:
        return plan.report

    # Both lists are filled in place so that a failure part way through still
    # discards every staged file and unlinks every file this import promoted.
    staged: list[tuple[FileRelocation, StagedArtifact]] = []
    promoted: list[Path] = []
    try:
        _stage_relocations(plan, staged)
        with database.engine.begin() as target:
            target.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
            _insert_rows(target, plan.tables)
            _finalize_relocations(target, staged, promoted)
    except BaseException:
        for _, artifact in reversed(staged):
            artifact.discard()
        for path in reversed(promoted):
            path.unlink(missing_ok=True)
        raise
    return plan.report


def _stage_relocations(
    plan: _ImportPlan, staged: list[tuple[FileRelocation, StagedArtifact]]
) -> None:
    stores: dict[Path, LocalArtifactStore] = {}
    for relocation in plan.report.file_relocations:
        root = (
            plan.target_artifact_root if relocation.table == "artifacts" else plan.target_data_root
        )
        if root is None:
            raise AssertionError("file relocation requires its target root")
        store = stores.setdefault(root, LocalArtifactStore(root))
        content = relocation.source.read_bytes()
        _assert_bytes(relocation, content)
        relative = relocation.target.relative_to(root).as_posix()
        staged.append((relocation, store.stage(relative, content)))


def _insert_rows(target: Connection, tables: tuple[_PreparedTable, ...]) -> None:
    for table in tables:
        if not table.rows:
            continue
        quoted = ", ".join(f'"{column}"' for column in table.columns)
        placeholders = ", ".join(f":{column}" for column in table.columns)
        statement = text(
            f'INSERT INTO "{table.name}" ({quoted}) VALUES ({placeholders})'  # noqa: S608
        )
        target.execute(statement, list(table.rows))
    violations = target.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValidationError(f"Imported rows fail foreign-key checks: {violations!r}")


def _finalize_relocations(
    target: Connection,
    staged: list[tuple[FileRelocation, StagedArtifact]],
    promoted: list[Path],
) -> None:
    finalized_ids: list[str] = []
    finalized_targets: set[Path] = set()
    for relocation, artifact in staged:
        if artifact.target not in finalized_targets:
            existed = artifact.target.exists()
            artifact.finalize()
            finalized_targets.add(artifact.target)
            if not existed:
                promoted.append(artifact.target)
        if relocation.table == "artifacts":
            finalized_ids.append(relocation.record_id)
    if not finalized_ids:
        return
    verified = UTCDateTime.serialize(utc_now())
    target.execute(
        text(
            "UPDATE artifacts SET integrity_status = 'verified', "
            "last_verified_at = :verified WHERE id = :id"
        ),
        [{"id": artifact_id, "verified": verified} for artifact_id in finalized_ids],
    )


def _preflight(
    source: Path,
    database: Database,
    *,
    dry_run: bool,
    source_data_root: Path | None,
    target_data_root: Path | None,
    source_artifact_root: Path | None,
    target_artifact_root: Path | None,
    read_only_import_roots: tuple[Path, ...],
) -> _ImportPlan:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise ValidationError(f"No database to import at {source}.")
    if sqlite_path(database.url) is None:
        raise ValidationError("Database import currently supports only a local SQLite target.")
    source_data, target_data, source_artifacts, target_artifacts = _resolved_roots(
        source_data_root,
        target_data_root,
        source_artifact_root,
        target_artifact_root,
    )
    allowed = tuple(root.expanduser().resolve() for root in read_only_import_roots)

    ordered_tables, target_tables = _complete_target_tables(database)
    _assert_target_empty_and_sound(database, tuple(target_tables))
    report = ImportReport(source=source, target=database.url, dry_run=dry_run)
    prepared: list[_PreparedTable] = []

    connection = _read_only(source)
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick != "ok":
            raise ValidationError(f"Source SQLite integrity check failed: {quick}")
        available = _source_tables(connection)
        missing_markers = sorted(RECOGNIZABLE_TABLES - set(available))
        if missing_markers:
            raise ValidationError(
                f"{source} is not a TailorCV database with a recognizable schema; "
                "missing tables: " + ", ".join(missing_markers)
            )
        active_source_work = connection.execute(
            "SELECT 1 FROM background_tasks WHERE task_type IN "
            "('source.scan', 'source.continue') AND status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        active_reader = (
            connection.execute(
                "SELECT 1 FROM source_scan_members WHERE active_task_id IS NOT NULL LIMIT 1"
            ).fetchone()
            if "source_scan_members" in available
            else None
        )
        if active_source_work is not None or active_reader is not None:
            raise ValidationError(
                "Stop or cancel active source scans and wait for readers to release their "
                "reservations before importing. Restart the source worker to recover "
                "reservations left by stopped readers. "
                "Collected results and retained links can be imported and resumed afterward."
            )
        _record_skipped_tables(connection, report, sorted(set(available) - set(target_tables)))

        with database.engine.connect() as target:
            for table in ordered_tables:
                if table.name not in available:
                    continue
                prepared.append(
                    _prepare_table(
                        connection,
                        target,
                        table,
                        available[table.name],
                        report,
                        source_data=source_data,
                        target_data=target_data,
                        source_artifacts=source_artifacts,
                        target_artifacts=target_artifacts,
                        allowed=allowed,
                    )
                )
    finally:
        connection.close()
    _assert_relocated_files(report)
    return _ImportPlan(report, tuple(prepared), target_data, target_artifacts)


def _resolved_roots(
    source_data_root: Path | None,
    target_data_root: Path | None,
    source_artifact_root: Path | None,
    target_artifact_root: Path | None,
) -> tuple[Path | None, Path | None, Path | None, Path | None]:
    _paired_roots("data", source_data_root, target_data_root)
    _paired_roots("artifact", source_artifact_root, target_artifact_root)
    source_data = source_data_root.expanduser().resolve() if source_data_root else None
    target_data = target_data_root.expanduser().resolve() if target_data_root else None
    source_artifacts = _artifact_root(source_artifact_root, source_data)
    target_artifacts = _artifact_root(target_artifact_root, target_data)
    _paired_roots("artifact", source_artifacts, target_artifacts)
    return source_data, target_data, source_artifacts, target_artifacts


def _artifact_root(explicit: Path | None, data_root: Path | None) -> Path | None:
    if explicit:
        return explicit.expanduser().resolve()
    return data_root / "artifacts" if data_root else None


def _complete_target_tables(database: Database) -> tuple[tuple[Table, ...], dict[str, Table]]:
    inspector = inspect(database.engine)
    ordered_tables = _import_ordered_tables()
    existing_target_tables = set(inspector.get_table_names())
    target_tables = {
        table.name: table for table in ordered_tables if table.name in existing_target_tables
    }
    missing_target = sorted(
        table.name for table in ordered_tables if table.name not in target_tables
    )
    if missing_target:
        raise ValidationError(
            "The target schema is incomplete; missing tables: " + ", ".join(missing_target)
        )
    return ordered_tables, target_tables


def _record_skipped_tables(
    connection: sqlite3.Connection, report: ImportReport, names: list[str]
) -> None:
    for name in names:
        count = int(
            connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]  # noqa: S608
        )
        if count:
            report.skipped_tables[name] = count


def _prepare_table(
    connection: sqlite3.Connection,
    target: Connection,
    table: Table,
    source_columns: list[str],
    report: ImportReport,
    *,
    source_data: Path | None,
    target_data: Path | None,
    source_artifacts: Path | None,
    target_artifacts: Path | None,
    allowed: tuple[Path, ...],
) -> _PreparedTable:
    name = table.name
    shared, keys = _shared_columns(table, source_columns, report)
    payload = _table_payload(
        connection,
        table,
        shared,
        report,
        source_data=source_data,
        target_data=target_data,
        source_artifacts=source_artifacts,
        target_artifacts=target_artifacts,
        allowed=allowed,
    )
    fresh, identical = _fresh_rows(target, name, keys, shared, payload)
    if identical:
        report.already_present[name] = identical
    report.copied[name] = len(fresh)
    return _PreparedTable(name, tuple(shared), tuple(fresh))


def _assert_relocated_files(report: ImportReport) -> None:
    for relocation in report.file_relocations:
        if not relocation.source.is_file():
            raise ValidationError(f"Relocated file is missing: {relocation.source}")
        _assert_bytes(relocation, relocation.source.read_bytes())
        if relocation.target.exists():
            _assert_bytes(relocation, relocation.target.read_bytes())


def _shared_columns(
    table: Table, source_columns: list[str], report: ImportReport
) -> tuple[list[str], list[str]]:
    """Validate the columns shared by one source and target table."""
    name = table.name
    target_columns = [column.name for column in table.columns]
    shared = [column for column in target_columns if column in source_columns]
    dropped = sorted(set(source_columns) - set(target_columns))
    added = sorted(set(target_columns) - set(source_columns))
    if dropped:
        report.dropped_columns[name] = dropped
    if added:
        report.added_columns[name] = added
        # Rows go in as raw INSERTs, so only a database-side default fills an added column.
        unsafe = [
            column.name
            for column in table.columns
            if column.name in added and not column.nullable and column.server_default is None
        ]
        if unsafe:
            raise ValidationError(
                f"Source table {name!r} lacks required target columns: " + ", ".join(unsafe)
            )
    keys = [column.name for column in table.primary_key.columns]
    if not set(keys).issubset(shared):
        raise ValidationError(
            f"Source table {name!r} lacks target primary-key columns: "
            + ", ".join(sorted(set(keys) - set(shared)))
        )
    return shared, keys


def _table_payload(
    connection: sqlite3.Connection,
    table: Table,
    shared: list[str],
    report: ImportReport,
    *,
    source_data: Path | None,
    target_data: Path | None,
    source_artifacts: Path | None,
    target_artifacts: Path | None,
    allowed: tuple[Path, ...],
) -> list[dict[str, object]]:
    """Read, normalize, and relocate the rows from one source table."""
    name = table.name
    order = ORDERED_BY.get(name)
    clause = f" ORDER BY {', '.join(order)}" if order else ""
    quoted = ", ".join(f'"{column}"' for column in shared)
    source_rows = connection.execute(f'SELECT {quoted} FROM "{name}"{clause}')  # noqa: S608
    payload = [dict(zip(shared, tuple(row), strict=True)) for row in source_rows]
    for values in payload:
        for column in table.columns:
            if (
                isinstance(column.type, UTCDateTime)
                and column.name in values
                and values[column.name] is not None
            ):
                parsed = UTCDateTime.parse(str(values[column.name]))
                values[column.name] = UTCDateTime.serialize(parsed)
        _rewrite_references(
            report,
            name,
            values,
            source_data=source_data,
            target_data=target_data,
            source_artifacts=source_artifacts,
            target_artifacts=target_artifacts,
            allowed=allowed,
        )
    return payload


def _fresh_rows(
    target: Connection,
    name: str,
    keys: list[str],
    shared: list[str],
    payload: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int]:
    """Separate new rows from identical rows and reject divergent collisions."""
    present = _existing_rows(target, name, keys, shared) if keys else {}
    fresh: list[dict[str, object]] = []
    identical = 0
    for values in payload:
        key = tuple(values[key] for key in keys)
        existing = present.get(key)
        if existing is None:
            fresh.append(values)
            continue
        differing = [column for column in shared if existing[column] != values[column]]
        if differing:
            rendered_key = ", ".join(f"{column}={values[column]!r}" for column in keys)
            raise ValidationError(
                f"Divergent primary-key collision in {name} ({rendered_key}); "
                "different shared columns: " + ", ".join(differing)
            )
        identical += 1
    return fresh, identical


def _rewrite_references(
    report: ImportReport,
    table: str,
    values: dict[str, object],
    *,
    source_data: Path | None,
    target_data: Path | None,
    source_artifacts: Path | None,
    target_artifacts: Path | None,
    allowed: tuple[Path, ...],
) -> None:
    """Rewrite only recognized path columns under explicit roots."""
    source_root = source_artifacts if table == "artifacts" else source_data
    target_root = target_artifacts if table == "artifacts" else target_data
    for name in tuple(values):
        if (table, name) not in PATH_COLUMNS or not values[name]:
            continue
        _rewrite_one_reference(
            report,
            table,
            name,
            values,
            source_root=source_root,
            target_root=target_root,
            allowed=allowed,
        )


def _rewrite_one_reference(
    report: ImportReport,
    table: str,
    name: str,
    values: dict[str, object],
    *,
    source_root: Path | None,
    target_root: Path | None,
    allowed: tuple[Path, ...],
) -> None:
    raw = str(values[name])
    path = _local_reference(raw)
    if path is None:
        report.preserved_references.append(f"{table}.{name}={raw}")
        return
    if source_root is not None and target_root is not None and _is_within(path, source_root):
        target = (target_root / path.relative_to(source_root)).resolve()
        values[name] = target.as_uri() if uri_scheme(raw) == "file" else str(target)
        report.rewritten_references.append(f"{table}.{name}: {path} -> {target}")
        if table in {"artifacts", "master_cv_versions"}:
            _record_relocation(report, table, name, values, source=path, target=target)
        if table == "artifacts":
            values["integrity_status"] = "staged"
            values["last_verified_at"] = None
        return
    if any(_is_within(path, root) for root in allowed):
        report.preserved_references.append(f"{table}.{name}={path}")
    else:
        report.unresolved_references.append(f"{table}.{name}={path}")


def _record_relocation(
    report: ImportReport,
    table: str,
    name: str,
    values: dict[str, object],
    *,
    source: Path,
    target: Path,
) -> None:
    digest_column = "sha256" if table == "artifacts" else "content_sha256"
    size_value = values.get("byte_size")
    if size_value is None:
        raise ValidationError(
            f"Cannot relocate {table}.{name} for {values['id']!r}: "
            "the source row has no byte_size to verify."
        )
    report.file_relocations.append(
        FileRelocation(
            table=table,
            record_id=str(values["id"]),
            source=source,
            target=target,
            sha256=str(values[digest_column]),
            byte_size=int(str(size_value)),
        )
    )


def _local_reference(value: str) -> Path | None:
    uri_path = file_uri_path(value)
    if uri_path is not None:
        return uri_path
    if uri_scheme(value):
        return None
    path = Path(value).expanduser()
    return path.resolve(strict=False) if path.is_absolute() else None


def _existing_rows(
    target: Connection,
    name: str,
    keys: list[str],
    columns: list[str],
) -> dict[tuple[object, ...], dict[str, object]]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    rows = target.exec_driver_sql(f'SELECT {quoted} FROM "{name}"')  # noqa: S608
    result: dict[tuple[object, ...], dict[str, object]] = {}
    for row in rows:
        values = dict(zip(columns, tuple(row), strict=True))
        result[tuple(values[key] for key in keys)] = values
    return result


def _assert_target_empty_and_sound(database: Database, table_names: tuple[str, ...]) -> None:
    """Require an integral target containing only deterministic migration seeds."""
    with database.engine.connect() as target:
        integrity = str(target.exec_driver_sql("PRAGMA integrity_check").scalar_one())
        if integrity != "ok":
            raise ValidationError(f"Target SQLite integrity check failed: {integrity}")
        foreign_keys = target.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise ValidationError(f"Target SQLite foreign-key check failed: {foreign_keys!r}")
        occupied: list[str] = []
        for name in table_names:
            if name in SEEDED_TABLES:
                continue
            count = int(target.exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar_one())  # noqa: S608
            if count:
                occupied.append(f"{name}={count}")
    if occupied:
        if any(item.startswith("workspaces=") for item in occupied):
            raise ValidationError(
                "The target database already has workspaces in it; import requires a new, "
                "otherwise empty database."
            )
        raise ValidationError(
            "The target database is not empty; found rows in " + ", ".join(occupied)
        )


def _paired_roots(label: str, source: Path | None, target: Path | None) -> None:
    if (source is None) != (target is None):
        raise ValidationError(f"Artifact relocation requires both source and target {label} roots.")


def _assert_bytes(relocation: FileRelocation, content: bytes) -> None:
    digest = hashlib.sha256(content).hexdigest()
    if digest != relocation.sha256 or len(content) != relocation.byte_size:
        raise ValidationError(
            f"Relocated file {relocation.source} does not match its stored SHA-256 and size."
        )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents
