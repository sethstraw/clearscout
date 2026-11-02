"""The terminal, behind one small object that implements the onboarding prompter."""

from __future__ import annotations

import os
import sys
import webbrowser

import typer

from tailorcv.shared.results import Finding, Outcome

COLOURS = {
    Outcome.PASS: typer.colors.GREEN,
    Outcome.WARN: typer.colors.YELLOW,
    Outcome.FAIL: typer.colors.RED,
}


def _colour_enabled() -> bool:
    """Return whether to emit color, honoring the NO_COLOR convention."""
    return not os.environ.get("NO_COLOR")


class Terminal:
    """The real implementation of the prompting and reporting interfaces."""

    def interactive(self) -> bool:
        """Return whether a person can answer; stdin alone cannot tell, so both streams count."""
        return sys.stdin.isatty() and sys.stdout.isatty()

    def notice(self, message: str) -> None:
        """Show an informational message."""
        typer.echo(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        """Ask a yes or no question."""
        return typer.confirm(question, default=default)

    def ask(self, question: str, default: str = "") -> str:
        """Ask for a value the user can see as they type it, unlike :meth:`secret`."""
        return str(typer.prompt(question, default=default, show_default=bool(default)))

    def secret(self, question: str) -> str:
        """Ask for a value without echoing it. For credentials only."""
        return str(typer.prompt(question, hide_input=True))

    def step(self, index: int, total: int, label: str) -> None:
        """Report progress to stderr so it survives being piped."""
        typer.secho(f"[{index}/{total}] {label}", fg=self._colour(typer.colors.CYAN), err=True)

    def open_url(self, url: str) -> None:
        """Open a URL in the user's browser, saying so if that is impossible."""
        if not webbrowser.open(url):  # pragma: no cover - depends on the desktop
            typer.echo(f"Could not open a browser. Visit {url} yourself.")

    def finding(self, finding: Finding) -> None:
        """Print one finding in the color its outcome deserves."""
        typer.secho(finding.line(), fg=self._colour(COLOURS[finding.outcome]))

    def error(self, message: str) -> None:
        """Print an error to stderr."""
        typer.secho(message, fg=self._colour(typer.colors.RED), err=True)

    def warning(self, message: str) -> None:
        """Print a caveat the user should read but that is not a failure."""
        typer.secho(message, fg=self._colour(typer.colors.YELLOW))

    def success(self, message: str) -> None:
        """Print the headline outcome of a command that worked."""
        typer.secho(message, fg=self._colour(typer.colors.GREEN))

    def plain(self, message: str = "") -> None:
        """Print neutral information, which is most of what a run says."""
        typer.echo(message)

    def _colour(self, colour: str) -> str | None:
        """Return the color, or nothing when the user asked for no color."""
        return colour if _colour_enabled() else None
