"""Guided first-time setup: ask only what cannot be worked out.

The one thing setup cannot infer is the wording someone refuses to have written
wrong; every other question confirms something already discovered. It proposes
no rules itself and lists the technologies the CV names so the person can write them.
"""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from clearscout.application.config import (
    DEFAULT_PROVIDER,
    ProviderName,
    Settings,
)
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.starter_sources import StarterSourceInstaller
from clearscout.application.master_cv import (
    SUPPORTED_SUFFIXES,
    MasterCvService,
    parse_canonical_source,
)
from clearscout.application.master_cv.cv import CvInventory, EntryKind, parse_cv
from clearscout.application.master_cv.discovery import candidates, describe
from clearscout.application.workspaces import WorkspaceService, layout_for
from clearscout.delivery.cli.support import open_database
from clearscout.domain.identity import normalize_text
from clearscout.infrastructure.artifacts.files import write_private
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.migration import upgrade
from clearscout.infrastructure.providers.credentials import Prompter, find, onboard
from clearscout.shared.errors import ConfigError

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Split on commas outside brackets so parenthetical commas remain intact.
SKILL_SPLIT = re.compile(r",\s*(?![^(]*\))")
MAX_LISTED_SKILLS = 60


@dataclass(frozen=True)
class ApplicationSetupResult:
    """Documents-first application and workspace initialized by one command."""

    config_path: Path
    master_cv: Path
    inventory: CvInventory
    provider: ProviderName
    had_credential: bool
    workspace_id: str
    workspace_root: Path
    database_url: str
    import_method: str


def run_application_setup(
    prompter: Prompter,
    *,
    source: Path | None = None,
    workspace_name: str = "My Job Search",
    slug: str | None = None,
    root: Path | None = None,
    destination: Path | None = None,
    provider: ProviderName | None = None,
    database_url: str | None = None,
    data_root: Path | None = None,
    artifact_root: Path | None = None,
    workspaces_root: Path | None = None,
    force: bool = False,
) -> ApplicationSetupResult:
    """Initialize storage, one workspace, settings, and a versioned master CV."""
    interactive = prompter.interactive()
    if source is None and not interactive:
        raise ConfigError(
            "'clearscout init' needs --cv because there is no terminal attached.\n"
            "  Give it a .docx, .pdf, .md, .txt, or screenshot to initialize from."
        )
    selected = _choose_cv_source(prompter, Path.cwd(), source)
    source_content = selected.read_bytes()
    normalized_slug = normalize_text(slug or workspace_name).replace(" ", "-")
    if not normalized_slug:
        raise ConfigError("Workspace name or slug must contain letters or numbers.")
    storage = RuntimeStorage.resolve(
        database_url,
        data_root=data_root,
        artifact_root=artifact_root,
        workspaces_root=workspaces_root,
    )
    url = storage.database_url
    resolved_layout = layout_for(normalized_slug, root=root, storage=storage)
    config_path = (destination or resolved_layout.config).expanduser().resolve()
    if (
        config_path.exists()
        and not force
        and (
            not interactive
            or not prompter.confirm(f"{config_path} already exists. Overwrite it?", default=False)
        )
    ):
        raise ConfigError(
            f"Kept the existing {config_path}. Pass --force to replace it explicitly."
        )

    selected_provider = provider or (
        _choose_provider(prompter) if interactive else DEFAULT_PROVIDER
    )
    had_credential = find(selected_provider) is not None
    needs_provider = parse_canonical_source(selected, source_content) is None
    if interactive and needs_provider and not had_credential:
        onboard(selected_provider, prompter=prompter)

    upgrade(url)
    with open_database(url) as database:
        workspace = WorkspaceService(
            database, storage, settings=Settings(provider=selected_provider)
        ).create(
            slug=normalized_slug,
            name=workspace_name,
            root_path=str(resolved_layout.root),
            origin="cli.init",
        )
        from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT
        from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
        from clearscout.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher

        with closing(PoliteHttpClient(user_agent=USER_AGENT)) as web:
            StarterSourceInstaller(
                SourceScanService(
                    database,
                    storage=storage,
                    posting_fetcher=PolitePostingFetcher(client=web),
                    http_client=web,
                )
            ).install(str(workspace.id), origin="cli.init")
        imported = MasterCvService(database, storage).import_source(
            workspace_id=str(workspace.id),
            filename=selected.name,
            content=source_content,
            settings=Settings(provider=selected_provider),
            prompter=prompter,
            replace=force,
            origin="cli.init",
        )
    master_cv = Path(imported.canonical_path)
    inventory = parse_cv(master_cv)
    prompter.notice(f"Read {master_cv}: {describe(inventory)}.\n")
    write_private(
        config_path,
        render_config(master_cv, inventory, selected_provider, config_path.parent),
    )
    return ApplicationSetupResult(
        config_path=config_path,
        master_cv=master_cv,
        inventory=inventory,
        provider=selected_provider,
        had_credential=had_credential,
        workspace_id=str(workspace.id),
        workspace_root=resolved_layout.root,
        database_url=url,
        import_method=imported.method,
    )


def _choose_cv_source(prompter: Prompter, root: Path, explicit: Path | None) -> Path:
    """Choose any supported CV source, including one not yet standardized."""
    if explicit is not None:
        path = explicit.expanduser().resolve()
        _validate_cv_source(path)
        return path
    found = candidates(root)
    if len(found) == 1 and prompter.confirm(f"Use {found[0]} as your master CV?", default=True):
        return found[0]
    if len(found) > 1:
        prompter.notice("Several standardized CVs were found here:")
        for path in found:
            prompter.notice(f"  {path}")
    for _ in range(3):
        answer = prompter.ask("Path to your CV (.docx, .pdf, .md, .txt, or screenshot)").strip()
        answer = answer.strip("'\"").replace("\\ ", " ")
        if not answer:
            break
        path = Path(answer).expanduser().resolve()
        try:
            _validate_cv_source(path)
        except ConfigError as error:
            prompter.notice(f"{error}\n")
            continue
        return path
    raise ConfigError("No usable CV source was selected, so nothing was initialized.")


def _validate_cv_source(path: Path) -> None:
    """Refuse missing or unsupported CV sources before any durable write."""
    if not path.is_file():
        raise ConfigError(f"CV source does not exist: {path}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ConfigError(f"CV source must use one of: {supported}.")


def _choose_provider(prompter: Prompter) -> ProviderName:
    """Ask which provider to bill, defaulting to the one already set up."""
    for provider in ProviderName:
        credential = find(provider)
        if credential is None:
            continue
        # Prefer credentials exported by the user's shell environment.
        if prompter.confirm(
            f"Found a {credential.describe()}. Use it? Every run will be billed to it.",
            default=True,
        ):
            return provider
    openai = prompter.confirm(
        "Use OpenAI? Answer no for Anthropic, which is implemented and unproven.",
        default=True,
    )
    return ProviderName.OPENAI if openai else ProviderName.ANTHROPIC


def render_config(
    master_cv: Path, inventory: CvInventory, provider: ProviderName, base: Path
) -> str:
    """Return the configuration file, commented for the person who will edit it."""
    relative = _relative(master_cv, base)
    email = _first_email(inventory)
    lines = [
        "# clearscout settings. Every value is optional; delete anything you do not",
        "# want to override. Run 'clearscout doctor' to see what is actually in effect.",
        "",
        f'cv = "{relative}"',
        "",
        "# Estimated page budget for content selection. The renderer determines",
        "# the actual page count.",
        "pages = 2",
        "",
        f'provider = "{provider.value}"',
        "",
        "# The part no tool can guess. These rules govern every run.",
        "[rules]",
        "",
        "# Phrases that must appear verbatim whenever they are mentioned.",
        "# Use this for an official title that must never be abbreviated.",
        "protected = []",
        "",
        "# Phrases the resume must carry, checked against copied text such as your contact line.",
        f"required = [{f'{email!r}' if email else ''}]".replace("'", '"'),
        "",
        "# Phrases that must never appear: employers under NDA, project code names.",
        "# Matched in any case, as whole words.",
        "forbidden = []",
        "",
        "# Printed under your contact details on every derivative. Delete if unwanted.",
        'work_authorization = ""',
        "",
        "# The rules that keep a resume defensible in the interview it earns you.",
        "# Each one draws a line between what you actually did and what a keyword",
        "# would imply. Write them as complete sentences: they are handed to the",
        "# model verbatim, not parsed.",
        "#",
        "# For example:",
        '#   "Kubernetes is operational usage of an existing cluster, not administration."',
        '#   "Terraform is production; Pulumi is familiarity only."',
        '#   "The warehouse work was collaborative: contributor, never sole owner."',
        "#",
        "# Your CV names the technologies below. Any of them where the honest",
        "# answer is more complicated than the word suggests needs a rule here.",
    ]
    lines += [f"#   {chunk}" for chunk in _skill_lines(inventory)]
    lines += [
        "distinctions = [",
        '  "No fabricated roles, dates, metrics, employers, degrees, or capabilities.",',
        "]",
        "",
    ]
    return "\n".join(lines)


def _relative(path: Path, base: Path) -> str:
    """Return the CV path relative to the config file when that is shorter."""
    try:
        return str(path.relative_to(base.resolve()))
    except ValueError:
        return str(path)


def _first_email(inventory: CvInventory) -> str | None:
    """Return the email address from the contact block, if there is one."""
    for line in inventory.contact_lines:
        if match := EMAIL.search(line):
            return match.group(0)
    return None


def skills_named(inventory: CvInventory) -> list[str]:
    """Return the individual technologies the CV's skills section names.

    Split from the skills lines with no inference, so it holds only what the CV says.
    """
    found: list[str] = []
    for entry in inventory.of_kind(EntryKind.SKILL):
        for piece in SKILL_SPLIT.split(entry.text):
            cleaned = piece.strip().strip(".;").replace("**", "")
            if 2 <= len(cleaned) <= 40 and cleaned not in found:
                found.append(cleaned)
    return found[:MAX_LISTED_SKILLS]


def _skill_lines(inventory: CvInventory, width: int = 70) -> list[str]:
    """Wrap the skills checklist into comment-width lines."""
    skills = skills_named(inventory)
    if not skills:
        return ["(none found in the skills section)"]
    lines: list[str] = []
    current = ""
    for skill in skills:
        candidate = f"{current}, {skill}" if current else skill
        if len(candidate) > width:
            lines.append(current)
            current = skill
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines
