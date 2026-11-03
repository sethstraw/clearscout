"""Shared option annotations for commands that address durable storage."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

DatabaseUrlOption = Annotated[
    str | None,
    typer.Option("--database-url", help="Override CLEARSCOUT_DATABASE_URL for this command."),
]
DataRootOption = Annotated[
    Path | None,
    typer.Option("--data-root", help="Application data root containing the database."),
]
WorkspacesRootOption = Annotated[
    Path | None,
    typer.Option("--workspaces-root", help="Folder new workspaces are created under."),
]
