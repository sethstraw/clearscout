"""Provider credential commands."""

from __future__ import annotations

from typing import Annotated

import typer

from tailorcv.application.config import ProviderName
from tailorcv.delivery.cli.support import _reporting_errors, terminal

auth_app = typer.Typer(help="Manage provider API keys.", no_args_is_help=True)


def auth_set(
    provider: Annotated[ProviderName, typer.Argument(help="Which provider's key to store.")],
) -> None:
    """Create, check, and store an API key, walking through it if needed."""
    from tailorcv.infrastructure.providers.credentials import onboard

    with _reporting_errors():
        credential = onboard(provider, prompter=terminal)
    typer.secho(f"Ready: {credential.describe()}.", fg=typer.colors.GREEN)


def auth_forget(
    provider: Annotated[ProviderName, typer.Argument(help="Which provider's key to remove.")],
) -> None:
    """Remove a stored API key from the system keyring."""
    from tailorcv.infrastructure.providers.credentials import KeyringStore

    KeyringStore().forget(provider)
    typer.secho(f"Removed any stored {provider.value} key.", fg=typer.colors.GREEN)


auth_app.command("set")(auth_set)
auth_app.command("forget")(auth_forget)
