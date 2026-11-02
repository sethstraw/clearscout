"""Commands for creating and importing a local workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tailorcv.application.config import ProviderName, load_settings
from tailorcv.delivery.cli.options import WorkspacesRootOption
from tailorcv.delivery.cli.support import CHECKS_FAILED, _reporting_errors, terminal


def template(
    path: Annotated[Path, typer.Argument(help="Where to write the skeleton.")] = Path(
        "CV_Master.md"
    ),
) -> None:
    """Write a master CV skeleton in the shape tailorcv reads."""
    from tailorcv.application.master_cv.starter import write_template

    with _reporting_errors():
        written = write_template(path)

    terminal.success(f"Wrote {written}")
    terminal.plain("")
    terminal.plain("Fill it in with everything you have done, not just what fits one resume.")
    terminal.plain("This file is the superset; tailorcv selects from it for each posting.")
    terminal.plain("")
    terminal.plain("Already have a resume or CV? Import and audit it instead:")
    terminal.plain(f"    tailorcv import YourResume.docx --out {path}")
    terminal.plain("Then read Import_Report.md, review the result, and run 'tailorcv init'.")


def init(
    config: Annotated[
        Path | None, typer.Option("--config", help="Where to write the settings file.")
    ] = None,
    cv: Annotated[
        Path | None,
        typer.Option("--cv", help="CV source: Word, PDF, Markdown, text, or screenshot."),
    ] = None,
    workspace_name: Annotated[
        str, typer.Option("--name", help="Name shown for this job-search workspace.")
    ] = "My Job Search",
    slug: Annotated[
        str | None, typer.Option("--slug", help="Short workspace name used in its folder.")
    ] = None,
    root: Annotated[
        Path | None,
        typer.Option("--root", help="Explicit folder for this workspace only."),
    ] = None,
    data_root: Annotated[
        Path | None,
        typer.Option(
            "--data-root",
            help="Application data root; database, workspaces, and artifacts stay together.",
        ),
    ] = None,
    artifact_root: Annotated[
        Path | None,
        typer.Option("--artifact-root", help="Override only the writable artifact root."),
    ] = None,
    workspaces_root: WorkspacesRootOption = None,
    provider: Annotated[
        ProviderName | None,
        typer.Option("--provider", help="Model provider used when the CV needs restructuring."),
    ] = None,
    database_url: Annotated[
        str | None,
        typer.Option("--database-url", help="Override TAILORCV_DATABASE_URL for initialization."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite without asking.")] = False,
) -> None:
    """Initialize Documents storage, a workspace, and a standardized master CV."""
    from tailorcv.bootstrap.setup import run_application_setup

    with _reporting_errors():
        result = run_application_setup(
            terminal,
            source=cv,
            workspace_name=workspace_name,
            slug=slug,
            root=root,
            destination=config,
            provider=provider,
            database_url=database_url,
            data_root=data_root,
            artifact_root=artifact_root,
            workspaces_root=workspaces_root,
            force=force,
        )

    terminal.plain("")
    terminal.success(f"Initialized {result.workspace_root}")
    terminal.plain(f"  settings:  {result.config_path}")
    terminal.plain(f"  master CV: {result.master_cv}")
    terminal.plain(f"  CV import: {result.import_method}")
    terminal.plain(f"  provider:  {result.provider.value}")
    terminal.plain(f"  database:  {result.database_url}")
    terminal.plain("")
    terminal.plain("One thing left, and it is the thing that makes the output trustworthy.")
    terminal.plain("Open that file and write your 'distinctions': one sentence per technology,")
    terminal.plain("drawing the line between what you actually did and what a recruiter would")
    terminal.plain('assume. For example: "Airflow was a prototype, never production."')
    terminal.plain("Your CV's technologies are already listed there as a checklist.")
    terminal.plain("")
    terminal.plain("Then: tailorcv run <posting-url>")


def import_command(
    source: Annotated[
        Path, typer.Argument(help="Your existing CV: .docx, .pdf, .md, .txt, or a screenshot.")
    ],
    out: Annotated[Path, typer.Option("--out", help="Where to write the master CV.")] = Path(
        "CV_Master.md"
    ),
    config: Annotated[Path | None, typer.Option("--config", help="Path to tailorcv.toml.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key for this run.")] = None,
) -> None:
    """Restructure an existing CV with provenance and tracked-content checks."""
    from tailorcv.application.tailoring.pipeline import import_cv

    with _reporting_errors():
        settings = load_settings(config)
        result = import_cv(
            source, settings, destination=out, api_key=api_key, prompter=terminal, force=force
        )

    terminal.plain("")
    for finding in result.findings:
        terminal.finding(finding)
    terminal.plain("")
    if result.passed:
        terminal.success(f"Wrote {result.path}")
    else:
        terminal.error(f"Wrote {result.path}, but the checks above did not pass.")
    terminal.plain(f"  {result.report.name if result.report else ''}  what was converted and why")
    if result.dropped:
        terminal.plain("")
        terminal.warning(
            f"{len(result.dropped)} line(s) were left out. Read the import report and put "
            "back anything that mattered."
        )
    terminal.plain("")
    terminal.plain(f"  Cost: {result.ledger.total_spent()}.")
    for line in result.ledger.summary_lines():
        terminal.plain(f"  {line}")
    terminal.plain("")
    terminal.plain("Read it before using it. It is your career, and a machine just retyped it.")
    terminal.plain("Then: tailorcv doctor")
    if not result.passed:
        raise typer.Exit(CHECKS_FAILED)
