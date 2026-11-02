"""Guided setup: confirm what was discovered, ask only what cannot be."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tailorcv.application.config import ProviderName, load_settings
from tailorcv.bootstrap.setup import render_config, run_application_setup, skills_named
from tailorcv.shared.errors import ConfigError
from tests.support.doubles import ScriptedPrompter
from tests.support.http import recorded_web_clients


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)
    monkeypatch.setattr("keyring.set_password", lambda service, name, key: None)
    monkeypatch.setattr(
        "tailorcv.infrastructure.providers.credentials.check_key", lambda provider, key: None
    )


def project(tmp_path: Path, master_cv: Path) -> Path:
    (tmp_path / "cv").mkdir()
    (tmp_path / "cv" / "CV_Master.md").write_text(
        master_cv.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def application_setup(
    tmp_path: Path,
    master_cv: Path | None,
    prompter: ScriptedPrompter,
    *,
    destination: Path | None = None,
    force: bool = False,
):
    data = tmp_path / "TailorCV"
    return run_application_setup(
        prompter,
        source=master_cv,
        workspace_name="My Search",
        slug="my-search",
        root=data / "workspaces" / "my-search",
        destination=destination,
        database_url=f"sqlite+pysqlite:///{data / 'tailorcv.sqlite3'}",
        force=force,
    )


def test_setup_writes_one_file_that_loads(tmp_path: Path, master_cv: Path) -> None:
    result = application_setup(tmp_path, master_cv, ScriptedPrompter(interactive=False))

    assert result.config_path.is_file()
    settings = load_settings(result.config_path)
    assert settings.master_cv().name == "CV_Master.md"
    assert settings.pages == 2


def test_setup_confirms_the_discovered_cv_rather_than_asking_for_a_path(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, master_cv)
    monkeypatch.chdir(root)
    prompter = ScriptedPrompter(secrets=["sk-test-key"])
    application_setup(tmp_path, None, prompter)
    assert "Use" in prompter.transcript
    assert "CV_Master.md" in prompter.transcript


def test_setup_reports_what_the_cv_parsed_into(tmp_path: Path, master_cv: Path) -> None:
    prompter = ScriptedPrompter(interactive=False)
    application_setup(tmp_path, master_cv, prompter)
    assert "entries across" in prompter.transcript


def test_setup_does_not_ask_for_a_key_when_one_exists(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-already-set")
    prompter = ScriptedPrompter()
    result = application_setup(tmp_path, master_cv, prompter)
    assert result.had_credential
    assert "Paste your" not in prompter.transcript


def test_a_standard_cv_does_not_require_a_provider_key(tmp_path: Path, master_cv: Path) -> None:
    prompter = ScriptedPrompter()
    result = application_setup(tmp_path, master_cv, prompter)
    assert result.import_method == "adopted"
    assert not result.had_credential
    assert "Paste your" not in prompter.transcript


def test_the_written_file_is_private(tmp_path: Path, master_cv: Path) -> None:
    """It carries contact details and a professional history."""
    result = application_setup(tmp_path, master_cv, ScriptedPrompter(interactive=False))
    assert stat.S_IMODE(result.config_path.stat().st_mode) == 0o600


def test_an_existing_file_is_not_overwritten_without_consent(
    tmp_path: Path, master_cv: Path
) -> None:
    destination = tmp_path / "existing.toml"
    destination.write_text("# mine\n", encoding="utf-8")
    prompter = ScriptedPrompter(secrets=["sk-test-key"], decline=["already exists"])
    with pytest.raises(ConfigError, match="Kept the existing"):
        application_setup(tmp_path, master_cv, prompter, destination=destination)
    assert destination.read_text(encoding="utf-8") == "# mine\n"


def test_the_email_from_the_cv_is_prefilled(inventory, tmp_path: Path) -> None:
    body = render_config(tmp_path / "cv.md", inventory, ProviderName.ANTHROPIC, tmp_path)
    assert '"jane.doe@example.com"' in body


def test_the_technologies_the_cv_names_are_listed_as_a_checklist(inventory, tmp_path: Path) -> None:
    """Mechanical extraction, so the checklist cannot contain an invention."""
    body = render_config(tmp_path / "cv.md", inventory, ProviderName.ANTHROPIC, tmp_path)
    assert "Kafka" in body
    assert "Terraform" in body
    from tailorcv.application.master_cv.cv import EntryKind

    source = " ".join(entry.text for entry in inventory.of_kind(EntryKind.SKILL))
    for skill in skills_named(inventory):
        assert skill in source


def test_setup_never_writes_distinctions_it_invented(inventory, tmp_path: Path) -> None:
    """A wrong rule would silently govern every future run."""
    body = render_config(tmp_path / "cv.md", inventory, ProviderName.ANTHROPIC, tmp_path)
    written = body.split("distinctions = [", 1)[1]
    assert written.count('"') == 2
    assert "No fabricated roles" in written


def test_setup_refuses_to_guess_a_cv_without_a_terminal(tmp_path: Path) -> None:
    """A prompt nobody can answer should say so, not print "Aborted!"."""
    with pytest.raises(ConfigError, match="no terminal attached"):
        application_setup(tmp_path, None, ScriptedPrompter(interactive=False))


def test_setup_asks_for_the_cv_path_visibly(tmp_path: Path, master_cv: Path) -> None:
    """A file path is not a credential and must not be typed blind."""
    prompter = ScriptedPrompter(secrets=["sk-test-key"], answers=[str(master_cv)])
    application_setup(tmp_path, None, prompter)
    assert "Path to your CV" in prompter.transcript
    assert prompter.secrets == ["sk-test-key"]


def test_setup_confirms_before_billing_a_key_it_found(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambient key is usually an employer's, and that is their money."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-someone-elses")
    prompter = ScriptedPrompter()
    application_setup(tmp_path, master_cv, prompter)
    assert "Every run will be billed to it" in prompter.transcript


def test_a_starter_file_has_exact_wording_but_no_personal_distinctions(
    inventory, tmp_path: Path
) -> None:
    """Generated exact wording is active without pretending the user wrote distinctions."""
    from tailorcv.application.tailoring.guardrails import Guardrails

    body = render_config(tmp_path / "cv.md", inventory, ProviderName.ANTHROPIC, tmp_path)
    (tmp_path / "tailorcv.toml").write_text(body, encoding="utf-8")
    settings = load_settings(tmp_path / "tailorcv.toml")
    assert isinstance(settings.rules, Guardrails)
    assert settings.rules.configured
    assert not settings.rules.personal_distinctions


def test_application_setup_initializes_a_scriptable_documents_workspace(
    tmp_path: Path, master_cv: Path
) -> None:
    root = tmp_path / "TailorCV" / "workspaces" / "my-search"
    result = application_setup(tmp_path, master_cv, ScriptedPrompter(interactive=False))

    assert result.workspace_root == root
    assert result.import_method == "adopted"
    assert result.master_cv == root / "master-cv" / "CV_Master.md"
    assert result.config_path == root / "tailorcv.toml"
    assert load_settings(result.config_path).master_cv() == result.master_cv
    assert (tmp_path / "TailorCV" / "tailorcv.sqlite3").is_file()
    assert (root / "imports").is_dir()
    assert (root / "work").is_dir()
    assert (root / "backups").is_dir()


def test_application_setup_closes_every_web_client_it_builds(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built, closed = recorded_web_clients(monkeypatch)

    application_setup(tmp_path, master_cv, ScriptedPrompter(interactive=False))

    assert built
    assert {id(client) for client in built} <= {id(client) for client in closed}
