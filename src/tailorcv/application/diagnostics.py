"""Report configuration, master CV, guardrails, credentials, models, and storage health.

No check prints a key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tailorcv import __version__
from tailorcv.application.config import (
    CONFIG_FILENAME,
    ProviderName,
    Settings,
    Stage,
    Tier,
    config_locations,
    find_config,
)
from tailorcv.application.master_cv.cv import parse_cv
from tailorcv.infrastructure.providers.credentials import find
from tailorcv.shared.costs.prices import priced_model
from tailorcv.shared.costs.prices import table as price_table
from tailorcv.shared.errors import TailorcvError
from tailorcv.shared.results import Finding, Outcome, worst

if TYPE_CHECKING:
    from tailorcv.infrastructure.persistence.database import Database


@dataclass(frozen=True)
class Diagnosis:
    """The full result of a diagnostic run."""

    findings: tuple[Finding, ...]

    @property
    def outcome(self) -> Outcome:
        """Return the most severe finding's outcome."""
        return worst(self.findings)

    @property
    def healthy(self) -> bool:
        """Return whether nothing failed."""
        return not self.outcome.failed


def diagnose(
    config_path: Path | None = None,
    *,
    database_url: str | None = None,
    data_root: Path | None = None,
    workspace_id: str | None = None,
) -> Diagnosis:
    """Run every diagnostic and return the findings in reporting order.

    No later check runs once configuration fails to load, since each depends on it.
    """
    findings = [Finding("tailorcv", Outcome.PASS, __version__), _config_finding(config_path)]
    try:
        settings = _settings(config_path)
    except TailorcvError as error:
        findings.append(Finding("configuration", Outcome.FAIL, str(error).splitlines()[0]))
        return Diagnosis(tuple(findings))

    if workspace_id:
        findings += [
            _credentials(settings),
            _models(settings),
            _model_availability(settings),
            *_storage_findings(database_url, data_root),
            *_workspace_findings(settings, database_url, data_root, workspace_id),
        ]
    else:
        findings += [
            _master_cv(settings),
            _rules(settings),
            _credentials(settings),
            _models(settings),
            _model_availability(settings),
            _output_dir(settings),
            *_storage_findings(database_url, data_root),
        ]
    return Diagnosis(tuple(findings))


def _workspace_findings(
    settings: Settings,
    database_url: str | None,
    data_root: Path | None,
    workspace_id: str,
) -> tuple[Finding, ...]:
    """Report whether one durable workspace can begin a job search."""
    from tailorcv.application.master_cv import MasterCvService
    from tailorcv.application.workspaces import SearchParametersService, WorkspaceService
    from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
    from tailorcv.infrastructure.persistence.database import Database

    database: Database | None = None
    try:
        storage = RuntimeStorage.resolve(database_url, data_root=data_root)
        database = Database(storage.database_url)
        workspaces = WorkspaceService(database, storage).list()
        workspace = next(
            (item for item in workspaces if item.id == workspace_id),
            None,
        )
        if workspace is None:
            return (Finding("workspace", Outcome.FAIL, f"{workspace_id} does not exist"),)
        versions = MasterCvService(database, storage).versions(workspace_id)
        current = next((item for item in versions if item.is_current), None)
        master_cv = (
            Finding(
                "workspace master CV",
                Outcome.PASS,
                f"{len(versions)} version(s); current version is {current.byte_size or 0} bytes",
            )
            if current is not None
            else Finding("workspace master CV", Outcome.FAIL, "no current version")
        )
        parameters = SearchParametersService(database).describe(workspace_id)
        search = (
            Finding(
                "workspace search parameters",
                Outcome.PASS,
                f"configured at version {parameters.version}",
            )
            if parameters.configured
            else Finding(
                "workspace search parameters",
                Outcome.FAIL,
                "not configured",
            )
        )
        return (
            Finding("workspace", Outcome.PASS, f"{workspace.name} ({workspace.slug})"),
            master_cv,
            search,
            _served_model_prices(database, settings, workspace_id),
        )
    except TailorcvError as error:
        return (Finding("workspace", Outcome.FAIL, str(error).splitlines()[0]),)
    finally:
        if database is not None:
            database.dispose()


def _served_model_prices(database: Database, settings: Settings, workspace_id: str) -> Finding:
    """Report whether every model that has answered here can be priced."""
    from tailorcv.application.workspaces.spend import recorded_served_models

    with database.session() as session:
        served = recorded_served_models(session, workspace_id)
    known = price_table(settings.prices)
    unpriced = sorted(name for name in served if priced_model(known, name) is None)
    if unpriced:
        return Finding(
            "workspace model prices",
            Outcome.WARN,
            f"no price configured for {', '.join(unpriced)}, "
            "so runs will report tokens but not dollars",
        )
    if not served:
        return Finding("workspace model prices", Outcome.PASS, "no model calls recorded yet")
    return Finding("workspace model prices", Outcome.PASS, f"{', '.join(sorted(served))} priced")


def _storage_findings(database_url: str | None, data_root: Path | None) -> tuple[Finding, ...]:
    """Report the workspace this diagnostic targets and its schema state."""
    from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
    from tailorcv.infrastructure.persistence.migration import status

    try:
        storage = RuntimeStorage.resolve(database_url, data_root=data_root)
    except TailorcvError as error:
        return (Finding("data root", Outcome.FAIL, str(error).splitlines()[0]),)
    data_root_finding = Finding("data root", Outcome.PASS, str(storage.data_root))
    try:
        schema = status(storage.database_url)
    except Exception as error:  # noqa: BLE001 - a diagnosis must never crash
        return (
            data_root_finding,
            Finding("database schema", Outcome.WARN, f"could not read: {error}"),
        )
    if not schema.database_exists:
        return (
            data_root_finding,
            Finding(
                "database schema",
                Outcome.WARN,
                "no database yet; run 'tailorcv db upgrade' to create one",
            ),
        )
    if not schema.current:
        parts = [f"revision {schema.current_revision or 'none'}, expected {schema.head_revision}"]
        if schema.missing_tables:
            parts.append(f"missing tables: {', '.join(schema.missing_tables)}")
        if schema.missing_columns:
            parts.append(f"missing columns: {', '.join(schema.missing_columns)}")
        if schema.changed_constraints:
            parts.append(f"changed constraints: {', '.join(schema.changed_constraints)}")
        return (
            data_root_finding,
            Finding("database schema", Outcome.FAIL, "; ".join(parts)),
        )
    return (
        data_root_finding,
        Finding("database schema", Outcome.PASS, f"current at {schema.head_revision}"),
    )


def _settings(config_path: Path | None) -> Settings:
    """Load settings, letting configuration errors propagate to the caller."""
    from tailorcv.application.config import load_settings

    return load_settings(config_path)


def _config_finding(config_path: Path | None) -> Finding:
    """Report which configuration file is in effect, if any.

    No configuration is a supported way to run, so this passes when nothing is
    found and simply says where it looked.
    """
    found = find_config(config_path)
    if found is None:
        looked = ", ".join(str(path) for path in config_locations())
        return Finding(
            "configuration", Outcome.PASS, f"none found, using defaults; looked in {looked}"
        )
    return Finding("configuration", Outcome.PASS, str(found))


def _master_cv(settings: Settings) -> Finding:
    """Find and parse the master CV, reporting where it came from."""
    from tailorcv.application.master_cv.discovery import describe

    try:
        path = settings.master_cv()
        inventory = parse_cv(path)
    except TailorcvError as error:
        return Finding("master CV", Outcome.FAIL, str(error).splitlines()[0])
    origin = "configured" if settings.cv else "discovered"
    return Finding("master CV", Outcome.PASS, f"{path} ({origin}; {describe(inventory)})")


def _rules(settings: Settings) -> Finding:
    """Report which content rules are in force."""
    rules = settings.rules
    if not rules.configured:
        return Finding(
            "rules",
            Outcome.WARN,
            "none configured; no protected phrases, forbidden phrases, or distinctions apply. "
            "Run 'tailorcv init' to set them up",
        )
    return Finding(
        "rules",
        Outcome.PASS,
        f"{len(rules.personal_distinctions)} distinctions, {len(rules.protected)} protected, "
        f"{len(rules.required)} required, {len(rules.forbidden)} forbidden",
    )


def _credentials(settings: Settings) -> Finding:
    """Report where a key would come from, without ever printing one."""
    credential = find(settings.provider)
    if credential is None:
        return Finding(
            "credentials",
            Outcome.WARN,
            f"no {settings.provider.value} key found; the next run will offer to set one up",
        )
    return Finding("credentials", Outcome.PASS, credential.describe())


def _models(settings: Settings) -> Finding:
    """Report the model and effort each tier will use, and whether it is priced."""
    choices = {tier: settings.choice_for(tier) for tier in Tier}
    described = ", ".join(
        f"{tier.value} {choice.model}"
        + (f" at {choice.effort.value} effort" if choice.effort else "")
        for tier, choice in choices.items()
    )
    known = price_table(settings.prices)
    unpriced = sorted({c.model for c in choices.values() if priced_model(known, c.model) is None})
    if unpriced:
        return Finding(
            "models",
            Outcome.WARN,
            f"{described}; no price configured for {', '.join(unpriced)}, "
            "so runs will report tokens but not dollars",
        )
    return Finding("models", Outcome.PASS, described)


def _model_availability(settings: Settings) -> Finding:
    """Ask the provider in force whether every model it would be sent still exists.

    Listing models costs nothing and is asked of that provider alone, so no key
    is needed for a provider no stage is configured against. Without a key this
    only warns, since the missing key is reported by its own finding.
    """
    credential = find(settings.provider)
    if credential is None:
        return Finding("model availability", Outcome.WARN, "not checked: no API key configured")
    tiers = {settings.choice_for(tier).model for tier in Tier}
    staged = {model for stage in Stage if (model := settings.policy_for(stage).model)} - tiers
    try:
        from tailorcv.infrastructure.providers.base import provider_for

        available = provider_for(settings.provider, credential.key).available_models()
    except Exception as error:  # noqa: BLE001 - a diagnosis must never be the thing that crashes
        return Finding("model availability", Outcome.WARN, f"could not ask the provider: {error}")
    if available is None:
        return Finding("model availability", Outcome.WARN, "the provider listed no models")
    missing = sorted(tiers - available)
    if missing:
        return Finding(
            "model availability",
            Outcome.FAIL,
            f"{settings.provider.value} does not offer {', '.join(missing)}.\n"
            + _suggest(settings, available),
        )
    unlisted = sorted(staged - available)
    if unlisted:
        return Finding(
            "model availability",
            Outcome.WARN,
            f"{settings.provider.value} no longer lists {', '.join(unlisted)}, set for one "
            "stage each, so those stages will fail while every other stage runs.\n"
            + _suggest(settings, available),
        )
    return Finding(
        "model availability", Outcome.PASS, f"{', '.join(sorted(tiers | staged))} available"
    )


def _suggest(settings: Settings, available: frozenset[str]) -> str:
    """Return a TOML block the user can paste, built from what the account has."""
    family = "claude" if settings.provider is ProviderName.ANTHROPIC else "gpt"
    options = sorted(name for name in available if name.startswith(family))[:8]
    if not options:
        return "  Your account lists no models under that family."
    listed = "\n".join(f"    {name}" for name in options)
    return (
        f"  Models your account can use:\n{listed}\n"
        f"  Put one in {CONFIG_FILENAME}:\n"
        f"    [models.{settings.provider.value}.extraction]\n"
        f'    model = "{options[0]}"\n'
        '    effort = "low"\n'
        f"    [models.{settings.provider.value}.drafting]\n"
        f'    model = "{options[0]}"\n'
        '    effort = "high"'
    )


def _output_dir(settings: Settings) -> Finding:
    """Confirm the output directory can actually be written to."""
    # Do not create directories from a read-only diagnostic.
    existing = settings.output_dir
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not os.access(existing, os.W_OK):
        return Finding(
            "output directory", Outcome.FAIL, f"{settings.output_dir}: {existing} is not writable"
        )
    if settings.output_dir.exists():
        return Finding("output directory", Outcome.PASS, str(settings.output_dir))
    return Finding(
        "output directory",
        Outcome.PASS,
        f"{settings.output_dir} (will be created on the first run)",
    )
