"""Commands for screening, tailoring, and inspecting job postings."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from tailorcv.application.config import ProviderName, load_settings
from tailorcv.delivery.cli.options import DatabaseUrlOption, DataRootOption
from tailorcv.delivery.cli.rendering import (
    _print_batch,
    _print_discovery,
    _print_run,
    _print_screen,
)
from tailorcv.delivery.cli.support import CHECKS_FAILED, _reporting_errors, terminal
from tailorcv.shared.errors import ConfigError

if TYPE_CHECKING:
    from tailorcv.application.tailoring.batch import Request


def run(
    source: Annotated[
        str, typer.Argument(help="Posting URL, or a path to a PDF, screenshot, or text file.")
    ],
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    cv: Annotated[
        Path | None, typer.Option("--cv", help="Master CV to tailor. Discovered when omitted.")
    ] = None,
    pages: Annotated[
        int | None,
        typer.Option("--pages", min=1, max=4, help="Estimated page budget for content selection."),
    ] = None,
    provider: Annotated[
        ProviderName | None, typer.Option("--provider", help="Model provider for this run.")
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Directory to write the run package into.")
    ] = None,
    trim: Annotated[
        int,
        typer.Option("--trim", min=0, help="Drop this many items, in drop order, if it runs long."),
    ] = 0,
    cover_letter: Annotated[
        bool, typer.Option("--cover-letter", help="Also draft a cover letter.")
    ] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore cached model responses.")
    ] = False,
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            help="API key for this run only. Visible in shell history; prefer the env var.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Ask the model again, in a new run beside the existing one."),
    ] = False,
    render: Annotated[
        bool,
        typer.Option("--render", help="Also render PDF and text, if pressume is on your PATH."),
    ] = False,
    approve: Annotated[
        Path | None,
        typer.Option("--approve", help="Apply a reviewed changes.json: accept or reject lines."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every check and the token ledger.")
    ] = False,
) -> None:
    """Tailor the master CV to one job description and write a reviewable package."""
    from tailorcv.application.tailoring.approval import load as load_approval
    from tailorcv.application.tailoring.pipeline import RunCommand
    from tailorcv.application.tailoring.pipeline import run as run_pipeline
    from tailorcv.domain.records import PostingReadIntent

    with _reporting_errors():
        settings = load_settings(config, overrides={"pages": pages, "provider": provider})
        result = run_pipeline(
            RunCommand(
                source=source,
                settings=settings,
                master_cv=cv,
                api_key=api_key,
                prompter=terminal,
                want_cover_letter=cover_letter,
                output_dir=out,
                cache_enabled=False if no_cache else None,
                trim=trim,
                force=force,
                verbose=verbose,
                want_render=render,
                approval=load_approval(approve) if approve is not None else None,
                intent=PostingReadIntent.SINGLE_POSTING,
            )
        )

    _print_run(result, verbose)
    if not result.verification.passed:
        raise typer.Exit(CHECKS_FAILED)


def screen(
    source: Annotated[
        str, typer.Argument(help="Posting URL, or a path to a PDF, screenshot, or text file.")
    ],
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    cv: Annotated[Path | None, typer.Option("--cv", help="Master CV to screen against.")] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Also write a screening report here.")
    ] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key for this run.")] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every model call as it happens.")
    ] = False,
) -> None:
    """Is this posting worth applying to? Two model calls, no resume written."""
    from tailorcv.application.tailoring.pipeline import screen as screen_posting
    from tailorcv.domain.records import PostingReadIntent

    with _reporting_errors():
        settings = load_settings(config)
        result = screen_posting(
            source,
            settings,
            master_cv=cv,
            api_key=api_key,
            prompter=terminal,
            out=out,
            verbose=verbose,
            intent=PostingReadIntent.SINGLE_POSTING,
        )

    _print_screen(result)


def batch(
    sources: Annotated[
        list[str] | None,
        typer.Argument(help="Two to five related postings: URLs or paths."),
    ] = None,
    plan: Annotated[
        Path | None,
        typer.Option("--plan", help="A batch TOML file: sources with priorities and notes."),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    cv: Annotated[Path | None, typer.Option("--cv", help="Master CV to screen against.")] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the batch report, the record, and each screening here."),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore cached model responses.")
    ] = False,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key for this run.")] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show every model call as it happens.")
    ] = False,
) -> None:
    """Screen a few related postings side by side. Nothing is drafted."""
    from tailorcv.application.tailoring import batch as batch_module
    from tailorcv.domain.records import PostingReadIntent

    with _reporting_errors():
        settings = load_settings(config)
        requests = _batch_requests(sources or [], plan)
        result, inventory = batch_module.screen_batch(
            requests,
            settings,
            master_cv=cv,
            api_key=api_key,
            prompter=terminal,
            cache_enabled=False if no_cache else None,
            out=out,
            verbose=verbose,
            intent=PostingReadIntent.SINGLE_POSTING,
        )
        written = batch_module.write(out, result, inventory) if out is not None else None

    _print_batch(result, written)


def _batch_requests(sources: list[str], plan: Path | None) -> list[Request]:
    """Return the postings a batch was asked for, from arguments or a plan file."""
    from tailorcv.application.tailoring import batch as batch_module

    if plan is not None and sources:
        raise ConfigError(
            "Give postings as arguments or in a --plan file, not both.\n"
            "  A plan file carries the priority and note for each one."
        )
    if plan is not None:
        return list(batch_module.requests_from(plan))
    return [batch_module.Request(source=source) for source in sources]


def discover(
    queue: Annotated[
        Path | None,
        typer.Option("--queue", help="A batch.json to take the shared discovery queue from."),
    ] = None,
    about: Annotated[
        list[str] | None,
        typer.Option("--about", help="Ask about this subject. Repeatable."),
    ] = None,
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    cv: Annotated[
        Path | None, typer.Option("--cv", help="Master CV to add confirmed facts to.")
    ] = None,
) -> None:
    """Ask about experience your master CV does not carry. No model is called."""
    from tailorcv.application.master_cv.cv import parse_cv
    from tailorcv.application.tailoring import batch as batch_module
    from tailorcv.application.tailoring import elicit

    with _reporting_errors():
        if not terminal.interactive():
            raise ConfigError(
                "Discovery is a conversation, and there is nobody at this terminal.\n"
                "  Run it without piping input or output."
            )
        settings = load_settings(config)
        path = settings.master_cv(cv)
        inventory = parse_cv(path)
        lines = (
            *(batch_module.lines_from(queue) if queue is not None else ()),
            *elicit.lines_for([(subject, (), ()) for subject in about or []]),
        )
        if not lines:
            raise ConfigError(
                "Nothing to ask about.\n"
                "  Pass --queue with the batch.json a batch wrote, or --about with a subject."
            )
        terminal.plain("")
        terminal.plain(
            "Nothing here reaches a model, and nothing is written until you have seen the\n"
            "exact change. Answer in your own words, and answer 'no' where the answer is no."
        )
        outcome = elicit.discover(
            terminal, lines=lines, inventory=inventory, path=path, at=datetime.now(UTC)
        )

    _print_discovery(outcome)


def doctor(
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    database_url: DatabaseUrlOption = None,
    data_root: DataRootOption = None,
    workspace_id: Annotated[
        str | None,
        typer.Option("--workspace-id", help="Diagnose one durable app workspace."),
    ] = None,
) -> None:
    """Check the configuration, the master CV, and the credentials, and say what is wrong."""
    from tailorcv.application.diagnostics import diagnose

    diagnosis = diagnose(
        config,
        database_url=database_url,
        data_root=data_root,
        workspace_id=workspace_id,
    )
    for finding in diagnosis.findings:
        terminal.finding(finding)
    if not diagnosis.healthy:
        raise typer.Exit(1)


def report(
    run_dir: Annotated[Path, typer.Argument(help="A directory a previous run wrote.")],
) -> None:
    """Print the tailoring report from a past run."""
    path = run_dir / "Tailoring_Report.md"
    if not path.is_file():
        terminal.error(f"No tailoring report at {path}.")
        raise typer.Exit(2)
    typer.echo(path.read_text(encoding="utf-8"))
