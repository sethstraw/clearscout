"""Commands that run long-lived local services."""

from __future__ import annotations

import signal
from contextlib import closing
from pathlib import Path
from types import FrameType
from typing import Annotated

import typer

from clearscout.delivery.cli.options import DatabaseUrlOption, WorkspacesRootOption
from clearscout.delivery.cli.support import _reporting_errors, open_database, terminal


def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="Loopback interface only: 127.0.0.1, ::1, or localhost."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    database_url: DatabaseUrlOption = None,
    data_root: Annotated[
        Path | None,
        typer.Option("--data-root", help="Application data root for this server."),
    ] = None,
    artifact_root: Annotated[
        Path | None,
        typer.Option("--artifact-root", help="Override only the writable artifact root."),
    ] = None,
    workspaces_root: WorkspacesRootOption = None,
) -> None:
    """Serve the versioned local API without running schema migrations."""
    import uvicorn

    from clearscout.bootstrap.api import create_app
    from clearscout.bootstrap.local_runtime import STOP_WAIT_SECONDS
    from clearscout.bootstrap.logging import configure_application_logging
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("ClearScout serves only on a loopback interface.")
    with _reporting_errors():
        storage = RuntimeStorage.resolve(
            database_url,
            data_root=data_root,
            artifact_root=artifact_root,
            workspaces_root=workspaces_root,
        )
    configure_application_logging()
    uvicorn.run(
        create_app(
            storage.database_url,
            storage.artifact_root,
            data_root=storage.data_root,
            workspaces_root=workspaces_root,
        ),
        host=host,
        port=port,
        log_level="info",
        log_config=None,
        timeout_graceful_shutdown=int(STOP_WAIT_SECONDS / 2),
    )


def worker(
    once: Annotated[
        bool,
        typer.Option("--once", help="Process at most one queued task and exit."),
    ] = False,
    worker_id: Annotated[
        str,
        typer.Option("--worker-id", help="Stable lease owner shown in task history."),
    ] = "local-worker-1",
    poll_seconds: Annotated[
        float,
        typer.Option("--poll-seconds", min=0.1, max=60, help="Idle polling interval."),
    ] = 1.0,
    database_url: DatabaseUrlOption = None,
    artifact_root: Annotated[
        Path | None,
        typer.Option("--artifact-root", help="Private local artifact-store root."),
    ] = None,
    data_root: Annotated[
        Path | None,
        typer.Option("--data-root", help="Application data root for this worker."),
    ] = None,
    continuation_slots: Annotated[
        int | None,
        typer.Option(
            "--continuation-slots",
            help=(
                "Board continuations for different hosts read at once. "
                "Defaults to CLEARSCOUT_CONTINUATION_SLOTS."
            ),
        ),
    ] = None,
    workspaces_root: WorkspacesRootOption = None,
) -> None:
    """Run the recoverable database-backed local task worker."""
    from clearscout.application.failures import UnavailableError
    from clearscout.bootstrap.logging import configure_application_logging
    from clearscout.bootstrap.worker import Worker, configured_continuation_slots, default_handlers
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
    from clearscout.infrastructure.persistence.migration import status
    from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT
    from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient

    # Convert migration failures into the actionable CLI error.
    with _reporting_errors():
        storage = RuntimeStorage.resolve(
            database_url,
            data_root=data_root,
            artifact_root=artifact_root,
            workspaces_root=workspaces_root,
        )
        url = storage.database_url
        if not status(url).current:
            raise UnavailableError("Run 'clearscout db upgrade' before starting a worker.")
        slots = configured_continuation_slots(continuation_slots)
    configure_application_logging()
    with (
        open_database(url) as database,
        closing(PoliteHttpClient(user_agent=USER_AGENT)) as http_client,
    ):
        service = Worker(
            database,
            worker_id=worker_id,
            handlers=default_handlers(database, artifact_root=storage, http_client=http_client),
            continuation_slots=slots,
        )

        def request_stop(_signum: int, _frame: FrameType | None) -> None:
            service.request_stop()

        previous_sigterm = signal.signal(signal.SIGTERM, request_stop) if not once else None
        try:
            # A second worker on one database is refused, and that refusal is a
            # message rather than a traceback.
            with _reporting_errors():
                if once:
                    worked = service.run_once()
                    terminal.plain(
                        "Processed one task." if worked else "No queued task was available."
                    )
                else:
                    terminal.plain(f"Worker {worker_id!r} is ready. Press Ctrl-C to stop.")
                    service.run_forever(poll_seconds=poll_seconds)
        except KeyboardInterrupt:
            terminal.plain("Worker stopped; any live lease can be recovered after it expires.")
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)


def start(
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8765,
    database_url: DatabaseUrlOption = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
    artifact_root: Annotated[Path | None, typer.Option("--artifact-root")] = None,
    workspaces_root: WorkspacesRootOption = None,
) -> None:
    """Start the local API and its single durable worker together."""
    from clearscout.bootstrap.local_runtime import start_local_application

    with _reporting_errors():
        state, staged = start_local_application(
            database_url,
            data_root=data_root,
            artifact_root=artifact_root,
            workspaces_root=workspaces_root,
            port=port,
        )
    if staged["unresolved"]:
        terminal.warning(
            f"{staged['unresolved']} staged artifact(s) could not be recovered "
            "and are marked unresolved."
        )
    if staged["outside_root"]:
        terminal.warning(
            f"{staged['outside_root']} staged artifact(s) are outside this artifact root "
            "and were left staged; start with the root that holds them to recover them."
        )
    if staged["retrying"]:
        terminal.warning(
            f"{staged['retrying']} staged artifact(s) could not be read or written "
            "and were left staged; the next start retries them."
        )
    terminal.success(f"ClearScout is ready at http://127.0.0.1:{state.port}.")


def stop(
    database_url: DatabaseUrlOption = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
    artifact_root: Annotated[Path | None, typer.Option("--artifact-root")] = None,
) -> None:
    """Stop the API and worker previously started by ``clearscout start``."""
    from clearscout.bootstrap.local_runtime import stop_local_application
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage

    with _reporting_errors():
        # A stack started against another data root is invisible here, so the
        # nothing-found message names the root that was searched.
        storage = RuntimeStorage.resolve(
            database_url, data_root=data_root, artifact_root=artifact_root
        )
        stopped = stop_local_application(
            storage.database_url, data_root=storage.data_root, artifact_root=storage.artifact_root
        )
    terminal.plain(
        "ClearScout stopped."
        if stopped
        else f"No ClearScout started by this command was found under {storage.data_root}."
    )


def status(
    database_url: DatabaseUrlOption = None,
    data_root: Annotated[Path | None, typer.Option("--data-root")] = None,
    artifact_root: Annotated[Path | None, typer.Option("--artifact-root")] = None,
) -> None:
    """Show whether the API and worker started by ``clearscout start`` are alive."""
    from clearscout.bootstrap.local_runtime import local_application_status
    from clearscout.infrastructure.artifacts.runtime import RuntimeStorage

    with _reporting_errors():
        storage = RuntimeStorage.resolve(
            database_url, data_root=data_root, artifact_root=artifact_root
        )
        state = local_application_status(
            storage.database_url, data_root=storage.data_root, artifact_root=storage.artifact_root
        )
    if state is None:
        terminal.plain(
            f"No ClearScout started by this command was found under {storage.data_root}."
        )
        raise typer.Exit(1)
    terminal.plain(f"ClearScout is running at http://127.0.0.1:{state.port}")
