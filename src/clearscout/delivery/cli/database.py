"""Durable-storage administration commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from clearscout.delivery.cli.options import DatabaseUrlOption, DataRootOption
from clearscout.delivery.cli.support import _reporting_errors, open_database, terminal
from clearscout.shared.errors import ConfigError

db_app = typer.Typer(help="Inspect or explicitly migrate durable storage.", no_args_is_help=True)


def database_status(
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Show whether durable storage exists and matches this ClearScout build."""
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.persistence.migration import status

    with _reporting_errors():
        result = status(RuntimeStorage.resolve(database_url, data_root=data_root).database_url)
    terminal.plain(f"Database exists: {'yes' if result.database_exists else 'no'}")
    terminal.plain(f"Current revision: {result.current_revision or 'none'}")
    terminal.plain(f"Expected revision: {result.head_revision}")
    terminal.plain(f"Schema current: {'yes' if result.current else 'no'}")
    if result.missing_tables:
        terminal.plain(f"Missing tables: {', '.join(result.missing_tables)}")
    if result.missing_columns:
        terminal.plain(f"Missing columns: {', '.join(result.missing_columns)}")
    if result.changed_constraints:
        terminal.plain(f"Changed constraints: {', '.join(result.changed_constraints)}")


def database_upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision or 'head'.")] = "head",
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Run an explicitly requested forward schema migration."""
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.persistence.migration import upgrade

    with _reporting_errors():
        url = RuntimeStorage.resolve(database_url, data_root=data_root).database_url
        result = upgrade(url, revision)
        if result.missing_tables or result.missing_columns or result.changed_constraints:
            missing = ", ".join(
                (*result.missing_tables, *result.missing_columns, *result.changed_constraints)
            )
            raise ConfigError(
                f"The database is stamped at {result.current_revision or 'no revision'} but is "
                f"missing expected schema objects: {missing}. Back it up before rebuilding this "
                "uninitialized data root."
            )
    terminal.success(f"Database schema is at {result.current_revision or 'base'}.")


def database_import(
    source: Annotated[
        Path, typer.Argument(help="A ClearScout SQLite database at this build's schema revision.")
    ],
    database_url: DatabaseUrlOption = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Validate and print the exact plan without writing."),
    ] = False,
    source_data_root: Annotated[
        Path | None,
        typer.Option(help="Old data root used to recognize relocatable local paths."),
    ] = None,
    data_root: Annotated[
        Path | None,
        typer.Option("--data-root", help="New coherent database, workspace, and artifact root."),
    ] = None,
    source_artifact_root: Annotated[
        Path | None,
        typer.Option(help="Old artifact root when it was outside the old data root."),
    ] = None,
    artifact_root: Annotated[
        Path | None,
        typer.Option("--artifact-root", help="New artifact root when it is outside data root."),
    ] = None,
    allow_read_root: Annotated[
        list[Path] | None,
        typer.Option(
            "--allow-read-root",
            help="Deliberately preserve absolute read-only references under this root.",
        ),
    ] = None,
) -> None:
    """Copy an earlier ClearScout database into this one, which must be empty.

    The source is opened read-only and is never modified, so the file you point
    at stays a backup. Database-only import reports local file references but is
    not portable. Pass explicit old and new roots to verify, copy, and remap
    immutable master-CV and artifact bytes.
    """
    from clearscout.application.portability import import_database
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.persistence.migration import status

    with _reporting_errors():
        storage = RuntimeStorage.resolve(
            database_url, data_root=data_root, artifact_root=artifact_root
        )
        schema = status(storage.database_url)
        if not schema.current:
            raise ConfigError(
                f"The target database is at {schema.current_revision or 'no revision'} and this "
                f"build expects {schema.head_revision}. Run 'clearscout db upgrade' first."
            )
        with open_database(storage.database_url) as database:
            report = import_database(
                source,
                database,
                dry_run=dry_run,
                source_data_root=source_data_root,
                target_data_root=storage.data_root if source_data_root is not None else None,
                source_artifact_root=source_artifact_root,
                target_artifact_root=(
                    storage.artifact_root
                    if source_artifact_root is not None or source_data_root is not None
                    else None
                ),
                read_only_import_roots=tuple(allow_read_root or ()),
            )
    for line in report.lines():
        terminal.plain(line)
    terminal.success("Dry run complete; nothing was written." if dry_run else "Import complete.")


def database_backup(
    destination: Annotated[Path, typer.Argument(help="New SQLite backup file to create.")],
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Create and verify a consistent SQLite backup without overwriting files."""
    from clearscout.application.portability import backup_database
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage

    with (
        open_database(
            RuntimeStorage.resolve(database_url, data_root=data_root).database_url
        ) as database,
        _reporting_errors(),
    ):
        report = backup_database(database, destination)
    terminal.success(f"Backup verified at {report.destination} ({report.byte_size} bytes).")


def database_archive_cache(
    cache_directory: Annotated[
        Path | None,
        typer.Option("--cache-directory", help="Response cache folder to read answers from."),
    ] = None,
    origin: Annotated[
        str, typer.Option("--origin", help="Label written on every rescued row.")
    ] = "cli.db.archive-cache",
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Copy the response cache's surviving answers into durable storage.

    The cache overwrites an answer as soon as its request is made again, and the
    copy carries no call to attribute it to. Re-running adds only what the
    archive does not already hold.
    """
    from clearscout.application.workspaces.spend import archive_response_cache
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.providers.cache import default_cache_directory

    directory = cache_directory or default_cache_directory()
    with _reporting_errors():
        url = RuntimeStorage.resolve(database_url, data_root=data_root).database_url
        with open_database(url) as database, database.session() as session:
            archived = archive_response_cache(session, cache_directory=directory, origin=origin)
    terminal.success(f"Archived {archived} cached answers from {directory}.")


def database_rebuild_missed_requirements(
    workspace_id: Annotated[
        str, typer.Option("--workspace-id", help="Workspace whose rows to rebuild.")
    ],
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Rebuild the missed-requirement texts the ranker reads from stored evaluations.

    Run it after upgrading a database evaluated before those rows were kept.
    Re-running replaces the rows rather than adding to them.
    """
    from clearscout.application.discovery.missed_requirement_reader import (
        rebuild_for_workspace,
    )
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage

    with _reporting_errors():
        url = RuntimeStorage.resolve(database_url, data_root=data_root).database_url
        with open_database(url) as database, database.session() as session:
            rows = rebuild_for_workspace(session, workspace_id)
    terminal.success(f"Rebuilt {rows} missed-requirement rows for workspace {workspace_id}.")


def database_downgrade(
    revision: Annotated[str, typer.Argument(help="Target revision or 'base'.")] = "-1",
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm the explicit destructive schema downgrade."),
    ] = False,
) -> None:
    """Downgrade durable storage only after explicit confirmation."""
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.persistence.migration import downgrade

    if not yes:
        raise typer.BadParameter("A schema downgrade requires --yes.")
    with _reporting_errors():
        result = downgrade(
            RuntimeStorage.resolve(database_url, data_root=data_root).database_url, revision
        )
    terminal.success(f"Database schema is at {result.current_revision or 'base'}.")


db_app.command("status")(database_status)
db_app.command("upgrade")(database_upgrade)
db_app.command("import")(database_import)
db_app.command("backup")(database_backup)
db_app.command("archive-cache")(database_archive_cache)
db_app.command("rebuild-missed-requirements")(database_rebuild_missed_requirements)
db_app.command("downgrade")(database_downgrade)
