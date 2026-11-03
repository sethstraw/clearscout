"""Diagnostics: useful when things are broken, and silent about secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from clearscout.application.diagnostics import Diagnosis, diagnose
from clearscout.application.workspaces import SearchParametersService
from clearscout.domain.search_parameters import JobSearchParameters, Seniority
from clearscout.infrastructure.persistence.database import Database
from clearscout.shared.results import Outcome
from tests.support.doubles import ScriptedProvider
from tests.support.scenarios import studio_workspace
from tests.support.suite import SUITE_KEY_VARIABLE


def outcome(diagnosis: Diagnosis, name: str) -> Outcome:
    return next(finding.outcome for finding in diagnosis.findings if finding.check == name)


def detail(diagnosis: Diagnosis, name: str) -> str:
    return next(finding.detail for finding in diagnosis.findings if finding.check == name)


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)


@pytest.fixture
def offline_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the model listing locally so no diagnosis reaches a network."""
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.base.provider_for",
        lambda name, key, timeout=None: ScriptedProvider({}),
    )


def write_config(
    tmp_path: Path, master_cv: Path, extra: str = "", output_dir: Path | None = None
) -> Path:
    path = tmp_path / "clearscout.toml"
    destination = output_dir or (tmp_path / "runs")
    path.write_text(f'cv = "{master_cv}"\noutput_dir = "{destination}"\n{extra}', encoding="utf-8")
    return path


def test_data_root_targets_the_named_workspace(tmp_path: Path, master_cv: Path) -> None:
    """`doctor --data-root <path>` reads that workspace, not the default."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    diagnosis = diagnose(write_config(tmp_path, master_cv), data_root=workspace)
    assert outcome(diagnosis, "data root") is Outcome.PASS
    assert str(workspace) in detail(diagnosis, "data root")
    assert outcome(diagnosis, "database schema") is Outcome.WARN
    assert "no database yet" in detail(diagnosis, "database schema")


def test_doctor_checks_the_durable_workspace_used_by_the_app(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, _, _, _ = studio_workspace(
        durable_database,
        tmp_path / "workspace",
        tmp_path / "artifacts",
    )
    SearchParametersService(durable_database).save(
        workspace_id,
        JobSearchParameters(seniority=Seniority(minimum="senior", maximum="director")),
    )

    diagnosis = diagnose(database_url=durable_database.url, workspace_id=workspace_id)

    assert outcome(diagnosis, "workspace") is Outcome.PASS
    assert outcome(diagnosis, "workspace master CV") is Outcome.PASS
    assert outcome(diagnosis, "workspace search parameters") is Outcome.PASS
    assert "version 1" in detail(diagnosis, "workspace search parameters")


def test_doctor_refuses_an_unknown_durable_workspace(durable_database: Database) -> None:
    diagnosis = diagnose(database_url=durable_database.url, workspace_id="missing")

    assert outcome(diagnosis, "workspace") is Outcome.FAIL
    assert not diagnosis.healthy


def test_a_healthy_setup_reports_healthy(tmp_path: Path, master_cv: Path) -> None:
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "master CV") is Outcome.PASS
    assert outcome(diagnosis, "models") is Outcome.PASS


def test_the_master_cv_check_reports_what_it_parsed(tmp_path: Path, master_cv: Path) -> None:
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert "entries" in detail(diagnosis, "master CV")
    assert "roles" in detail(diagnosis, "master CV")


def test_a_broken_master_cv_fails_with_the_reason(tmp_path: Path) -> None:
    broken = tmp_path / "broken.md"
    broken.write_text("no heading at all\n", encoding="utf-8")
    diagnosis = diagnose(write_config(tmp_path, broken))
    assert not diagnosis.healthy
    assert outcome(diagnosis, "master CV") is Outcome.FAIL


def test_a_broken_configuration_stops_before_cascading(tmp_path: Path) -> None:
    path = tmp_path / "clearscout.toml"
    path.write_text('master_cv = "absent.md"\n', encoding="utf-8")
    diagnosis = diagnose(path)
    assert not diagnosis.healthy
    assert [finding.check for finding in diagnosis.findings][-1] == "configuration"


def test_a_missing_key_is_a_warning_not_a_failure(tmp_path: Path, master_cv: Path) -> None:
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "credentials") is Outcome.WARN
    assert diagnosis.healthy


def test_a_configured_key_is_described_but_never_printed(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, offline_provider: None
) -> None:
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "sk-do-not-print-me")
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    rendered = "\n".join(finding.line() for finding in diagnosis.findings)
    assert "sk-do-not-print-me" not in rendered
    assert "environment variable" in rendered


def test_missing_rules_warn_that_none_apply(tmp_path: Path, master_cv: Path) -> None:
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "rules") is Outcome.WARN
    assert "clearscout init" in detail(diagnosis, "rules")


def test_rules_in_force_are_counted(tmp_path: Path, master_cv: Path) -> None:
    config = write_config(tmp_path, master_cv, extra='[rules]\ndistinctions = ["One rule."]\n')
    diagnosis = diagnose(config)
    assert outcome(diagnosis, "rules") is Outcome.PASS
    assert "1 distinctions" in detail(diagnosis, "rules")


def test_an_unpriced_model_warns_that_dollars_will_be_missing(
    tmp_path: Path, master_cv: Path
) -> None:
    """Anthropic on purpose: a `[models.anthropic]` table governs nothing else."""
    extra = 'provider = "anthropic"\n'
    extra += '[models.anthropic.extraction]\nmodel = "claude-mystery-1"\n\n'
    extra += '[models.anthropic.drafting]\nmodel = "claude-mystery-1"\n'
    diagnosis = diagnose(write_config(tmp_path, master_cv, extra=extra))
    assert outcome(diagnosis, "models") is Outcome.WARN
    assert "tokens but not dollars" in detail(diagnosis, "models")


def test_a_dated_model_snapshot_is_priced_through_the_model_it_names(
    tmp_path: Path, master_cv: Path
) -> None:
    """Anthropic on purpose: the dated name is a snapshot of a priced claude model."""
    extra = 'provider = "anthropic"\n'
    extra += '[models.anthropic.extraction]\nmodel = "claude-sonnet-5-20260514"\n\n'
    extra += '[models.anthropic.drafting]\nmodel = "claude-sonnet-5-20260514"\n'
    diagnosis = diagnose(write_config(tmp_path, master_cv, extra=extra))
    assert outcome(diagnosis, "models") is Outcome.PASS


def test_doctor_names_a_served_model_no_price_covers(
    durable_database: Database, tmp_path: Path
) -> None:
    """A run whose served model is unpriced reports tokens and no dollars."""
    from clearscout.application.workspaces.spend import record_calls
    from clearscout.shared.costs import CallManifest, Ledger, Usage

    workspace_id, _, _, _ = studio_workspace(
        durable_database,
        tmp_path / "workspace",
        tmp_path / "artifacts",
    )
    ledger = Ledger()
    ledger.record(
        CallManifest("draft", "claude-sonnet-5", Usage(input_tokens=10), served="mystery-served-1")
    )
    with durable_database.session() as session:
        record_calls(
            session,
            workspace_id=workspace_id,
            task_id=None,
            ledger=ledger,
            provider="anthropic",
            origin="workflow.tailor",
        )

    diagnosis = diagnose(database_url=durable_database.url, workspace_id=workspace_id)

    assert outcome(diagnosis, "workspace model prices") is Outcome.WARN
    assert "mystery-served-1" in detail(diagnosis, "workspace model prices")


def test_an_unwritable_output_directory_fails(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clearscout.application.diagnostics.os.access", lambda path, mode: False)
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "output directory") is Outcome.FAIL


def test_an_absent_output_directory_is_reported_not_created(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnosing must not have side effects on the user's project."""
    monkeypatch.chdir(tmp_path)
    diagnosis = diagnose(write_config(tmp_path, master_cv, output_dir=tmp_path / "not-yet"))
    assert outcome(diagnosis, "output directory") is Outcome.PASS
    assert "will be created" in detail(diagnosis, "output directory")
    assert not (tmp_path / "not-yet").exists()


def test_no_configuration_file_is_not_a_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running with no configuration is supported, so it must not read as broken."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "clearscout.application.config.user_config_dir", lambda name: str(tmp_path / "none")
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    diagnosis = diagnose(None)
    assert outcome(diagnosis, "configuration") is Outcome.PASS
    assert "using defaults" in detail(diagnosis, "configuration")


def test_a_wrong_model_identifier_is_caught_before_a_paid_run(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, offline_provider: None
) -> None:
    """Anthropic on purpose: the key, the table, and the identifiers agree."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    extra = 'provider = "anthropic"\n'
    extra += '[models.anthropic.extraction]\nmodel = "claude-imaginary-9"\n\n'
    extra += '[models.anthropic.drafting]\nmodel = "claude-sonnet-5"\n'
    diagnosis = diagnose(write_config(tmp_path, master_cv, extra=extra))
    assert outcome(diagnosis, "model availability") is Outcome.FAIL
    assert "claude-imaginary-9" in detail(diagnosis, "model availability")


def test_configured_models_that_exist_pass(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, offline_provider: None
) -> None:
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "sk-test")
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "model availability") is Outcome.PASS


def test_availability_is_not_checked_without_a_key(tmp_path: Path, master_cv: Path) -> None:
    diagnosis = diagnose(write_config(tmp_path, master_cv))
    assert outcome(diagnosis, "model availability") is Outcome.WARN
    assert "no API key" in detail(diagnosis, "model availability")


def test_a_stage_model_the_provider_dropped_warns_rather_than_failing(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, offline_provider: None
) -> None:
    """One stage names its own model; every other stage still runs, so this warns.

    Anthropic on purpose: the scripted listing offers that provider's tier model.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    extra = 'provider = "anthropic"\n'
    extra += '[stages.match]\ntier = "extraction"\nmax_tokens = 8000\n'
    extra += 'model = "claude-retired-7"\n'
    diagnosis = diagnose(write_config(tmp_path, master_cv, extra=extra))
    assert outcome(diagnosis, "model availability") is Outcome.WARN
    assert "no longer lists claude-retired-7" in detail(diagnosis, "model availability")
    assert diagnosis.healthy


def test_a_wrong_model_id_is_told_what_to_write_instead(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, offline_provider: None
) -> None:
    """Model names retire on a schedule; config files do not.

    Anthropic on purpose: the suggestion is filtered to the provider's own
    family, so the claude identifiers here are the assertion, not scenery.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    extra = 'provider = "anthropic"\n'
    extra += '[models.anthropic.extraction]\nmodel = "claude-imaginary-9"\n\n'
    extra += '[models.anthropic.drafting]\nmodel = "claude-imaginary-9"\n'
    diagnosis = diagnose(write_config(tmp_path, master_cv, extra=extra))
    detail_text = detail(diagnosis, "model availability")
    assert "claude-sonnet-5" in detail_text
    assert "[models.anthropic.extraction]" in detail_text
