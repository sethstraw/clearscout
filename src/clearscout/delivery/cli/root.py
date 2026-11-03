"""Register the root ClearScout commands in their established order."""

from __future__ import annotations

from typing import Annotated

import typer

from clearscout import __version__
from clearscout.delivery.cli.runtime import serve, start, status, stop, worker
from clearscout.delivery.cli.support import app
from clearscout.delivery.cli.tailoring import batch, discover, doctor, report, run, screen
from clearscout.delivery.cli.workspace import import_command, init, template


def main(
    version: Annotated[
        bool, typer.Option("--version", help="Show the version and exit.", is_eager=True)
    ] = False,
) -> None:
    """Handle options that apply to every command."""
    if version:
        typer.echo(f"clearscout {__version__}")
        raise typer.Exit


def register_root_commands() -> None:
    """Attach root commands after the three sub-apps have been registered."""
    app.callback(invoke_without_command=True)(main)
    app.command()(run)
    app.command()(template)
    app.command()(init)
    app.command("import")(import_command)
    app.command()(screen)
    app.command()(batch)
    app.command()(discover)
    app.command()(doctor)
    app.command()(report)
    app.command()(start)
    app.command()(stop)
    app.command()(status)
    app.command()(serve)
    app.command()(worker)
