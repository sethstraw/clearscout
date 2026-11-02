"""CLI commands that enqueue durable workflow requests."""

from __future__ import annotations

from typing import Annotated

import typer

from tailorcv.delivery.cli.options import DatabaseUrlOption, DataRootOption
from tailorcv.delivery.cli.support import _reporting_errors, open_database, terminal

tasks_app = typer.Typer(help="Queue durable workflows for the local worker.", no_args_is_help=True)


@tasks_app.command()
def tailor(
    workspace_id: Annotated[
        str, typer.Option("--workspace-id", help="Workspace that owns the job.")
    ],
    job_posting_id: Annotated[
        str, typer.Option("--job-posting-id", help="Stored posting to tailor for.")
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="Stable key for this request.")
    ],
    cover_letter: Annotated[
        bool, typer.Option("--cover-letter/--no-cover-letter", help="Include a cover-letter draft.")
    ] = True,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="Request a new package instead of reuse.")
    ] = False,
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Queue the paid tailoring workflow that the API and worker share."""
    from tailorcv.application.tasks import TaskQueueService, validate_task_payload
    from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage

    with _reporting_errors():
        payload = validate_task_payload(
            "workflow.tailor",
            {
                "job_posting_id": job_posting_id,
                "cover_letter": cover_letter,
                "regenerate": regenerate,
            },
        )
        storage = RuntimeStorage.resolve(database_url, data_root=data_root)
        with open_database(storage.database_url) as database:
            task = TaskQueueService(database).enqueue(
                workspace_id=workspace_id,
                task_type="workflow.tailor",
                payload=payload,
                idempotency_key=idempotency_key,
            )
    terminal.success(f"Queued tailoring task {task.id}.")
