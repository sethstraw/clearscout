"""Job-search parameter commands."""

from __future__ import annotations

from contextlib import closing
from typing import Annotated

import typer

from tailorcv.delivery.cli.options import DatabaseUrlOption, DataRootOption
from tailorcv.delivery.cli.support import _reporting_errors, open_database, terminal

search_app = typer.Typer(
    help="Inspect the job-search state recorded for a workspace.",
    no_args_is_help=True,
)


def search_show(
    workspace_id: Annotated[
        str,
        typer.Option("--workspace-id", help="Workspace id to inspect."),
    ],
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
) -> None:
    """Print one workspace's current search parameters and discovery state."""
    from tailorcv.application.discovery.board_scan import SourceScanService
    from tailorcv.application.workspaces import SearchParametersService, parameter_summary
    from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
    from tailorcv.infrastructure.web_fetch.ingest.url import USER_AGENT
    from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
    from tailorcv.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher

    with _reporting_errors():
        storage = RuntimeStorage.resolve(database_url, data_root=data_root)
        with (
            open_database(storage.database_url) as database,
            closing(PoliteHttpClient(user_agent=USER_AGENT)) as web,
        ):
            parameters = SearchParametersService(database).describe(workspace_id)
            discoveries = SourceScanService(
                database,
                storage=storage,
                posting_fetcher=PolitePostingFetcher(client=web),
                http_client=web,
            )
            pending = discoveries.list_pending_discoveries(workspace_id)
            refusals = discoveries.list_refusals(workspace_id)

    terminal.plain(f"Workspace: {workspace_id}")
    terminal.plain(f"Current search parameters: version {parameters.version}")
    for line in parameter_summary(
        parameters.parameters,
        source=f"workspace {workspace_id}, version {parameters.version}",
        empty_hint="No job-search parameters are saved in this workspace.",
    ):
        terminal.plain(line)
    terminal.plain("")
    terminal.plain(f"Pending discoveries ({len(pending)})")
    if pending:
        for discovery in pending:
            location = f" | {discovery.location}" if discovery.location else ""
            terminal.plain(f"  {discovery.title} at {discovery.employer}{location}")
            terminal.plain(f"    {discovery.reason}")
    else:
        terminal.plain("  None.")
    terminal.plain("")
    terminal.plain(f"Refusal history ({len(refusals)})")
    if refusals:
        for refusal in refusals:
            terminal.plain(f"  {refusal.title} at {refusal.employer} | {refusal.condition}")
            version = (
                f"Search settings v{refusal.search_parameter_version}"
                if refusal.search_parameter_version is not None
                else "No saved search settings version"
            )
            terminal.plain(f"    {version}")
            terminal.plain(f"    {refusal.reason}")
    else:
        terminal.plain("  None.")


search_app.command("show")(search_show)
