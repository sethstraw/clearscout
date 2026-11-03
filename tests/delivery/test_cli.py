"""The command line: exit codes, error presentation, and no business logic."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from clearscout import __version__
from clearscout.bootstrap.cli import app
from clearscout.infrastructure.artifacts import rendering
from tests.support.doubles import ScriptedProvider
from tests.support.imported_cv import MESSY_CV, tidy_extraction
from tests.support.payloads import script
from tests.support.suite import SUITE_KEY_VARIABLE, SUITE_PROVIDER

# Naming a command is not running it, so building the tree must not drag in the
# database, the providers, or the discovery stack.
DEFERRED_UNTIL_A_COMMAND_RUNS = ("sqlalchemy", "anthropic", "openai", "trafilatura")


def test_naming_a_command_does_not_import_what_running_it_would_need() -> None:
    """`clearscout --help` builds the command tree, and that is all it should cost."""
    probe = (
        "import sys; import clearscout.bootstrap.cli; "
        f"print(','.join(name for name in {DEFERRED_UNTIL_A_COMMAND_RUNS!r} "
        "if name in sys.modules))"
    )
    loaded = subprocess.run(  # noqa: S603 - fixed arguments, current interpreter
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert loaded == ""


runner = CliRunner()

POSTING = (
    "Staff Data Platform Engineer at Globex. "
    "You will own streaming ingestion, schema governance, and data lineage. "
    "Required: Kafka, Terraform, PostgreSQL, and eight years of experience. "
    "Remote within the United States. No visa sponsorship is available. "
) * 4


@pytest.fixture(autouse=True)
def offline_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """No command in this suite may construct a real SDK client."""
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.base.provider_for",
        lambda name, key, timeout=None: ScriptedProvider({}),
    )


def config_at(tmp_path: Path, master_cv: Path) -> Path:
    # `cache = false`: the response cache otherwise defaults to the user's own
    # directory, and the next test is answered from it.
    path = tmp_path / "clearscout.toml"
    path.write_text(
        f'cv = "{master_cv}"\noutput_dir = "{tmp_path / "runs"}"\ncache = false\n',
        encoding="utf-8",
    )
    return path


def test_version_prints_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_shows_help() -> None:
    assert "Usage" in runner.invoke(app, []).output


def test_a_configuration_error_prints_a_message_not_a_traceback(tmp_path: Path) -> None:
    result = runner.invoke(app, ["run", "posting.txt", "--config", str(tmp_path / "absent.toml")])
    assert result.exit_code == 2
    assert "Configuration file not found" in result.output
    assert "Traceback" not in result.output


def test_report_prints_a_past_run(tmp_path: Path) -> None:
    (tmp_path / "Tailoring_Report.md").write_text("# Report\n\nBody.\n", encoding="utf-8")
    result = runner.invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 0
    assert "# Report" in result.stdout


def test_report_on_a_directory_without_one_says_so(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 2
    assert "No tailoring report" in result.output


def test_doctor_reports_a_healthy_setup(tmp_path: Path, master_cv: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(config_at(tmp_path, master_cv))])
    assert "master CV" in result.output
    assert "PASS" in result.output


def test_doctor_does_not_create_anything(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command advertised as read-only must not leave things behind."""
    monkeypatch.chdir(tmp_path)
    config = config_at(tmp_path, master_cv)
    before = sorted(path.name for path in tmp_path.iterdir())
    runner.invoke(app, ["doctor", "--config", str(config)])
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_doctor_exits_non_zero_when_something_is_broken(tmp_path: Path) -> None:
    broken = tmp_path / "broken.md"
    broken.write_text("no heading\n", encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--config", str(config_at(tmp_path, broken))])
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_doctor_accepts_data_root_and_reports_that_workspace(
    tmp_path: Path, master_cv: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--data-root",
            str(workspace),
        ],
    )
    assert "unknown option" not in result.output.lower()
    assert "no such option" not in result.output.lower()
    assert str(workspace) in result.output


def test_doctor_never_prints_a_key(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "sk-do-not-print-me")
    result = runner.invoke(app, ["doctor", "--config", str(config_at(tmp_path, master_cv))])
    assert "sk-do-not-print-me" not in result.output


def test_auth_set_walks_through_and_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        "keyring.set_password", lambda service, name, key: stored.update({name: key})
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.credentials.check_key", lambda provider, key: None
    )
    result = runner.invoke(app, ["auth", "set", "anthropic"], input="n\nsk-test-key\n")
    assert result.exit_code == 0
    assert stored == {"anthropic": "sk-test-key"}
    assert "subscription" in result.output


def test_auth_set_reports_a_key_the_provider_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    from clearscout.shared.errors import AuthError

    def reject(provider: Any, key: str) -> None:
        raise AuthError("that key was not accepted: HTTP 401")

    monkeypatch.setattr("clearscout.infrastructure.providers.credentials.check_key", reject)
    monkeypatch.setattr("keyring.set_password", lambda service, name, key: None)
    result = runner.invoke(app, ["auth", "set", "anthropic"], input="n\nbad\nbad\nbad\n")
    assert result.exit_code == 2
    assert "HTTP 401" in result.output


def test_auth_forget_removes_a_stored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    removed: list[str] = []
    monkeypatch.setattr("keyring.delete_password", lambda service, name: removed.append(name))
    result = runner.invoke(app, ["auth", "forget", "openai"])
    assert result.exit_code == 0
    assert removed == ["openai"]


def test_a_run_with_failing_checks_exits_non_zero(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.application.tailoring.pipeline import (
        MaterializedRun,
        RunResult,
        SerializedRunArtifacts,
    )
    from clearscout.application.tailoring.stages.jobspec import JobSpec
    from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report
    from clearscout.shared.costs import Ledger
    from clearscout.shared.results import Finding, Outcome

    def fake_run(*args: Any, **kwargs: Any) -> RunResult:
        return RunResult(
            verification=Report(
                findings=(Finding("sources cited", Outcome.FAIL, "a line cites nothing"),),
                coverage=KeywordCoverage(present=(), missing=("Kafka",)),
            ),
            spec=JobSpec(
                title="T",
                company=None,
                seniority=None,
                location=None,
                requirements=(),
                keywords=(),
                hard_gates=(),
                origin="test",
            ),
            ledger=Ledger(),
            attempts=3,
            trimmed=0,
            serialized=SerializedRunArtifacts(
                document=b"",
                report=b"",
                manifest_json=b"{}\n",
                manifest={},
                cover_letter=None,
                review_json=None,
                review_markdown=None,
                review={},
            ),
            materialized=MaterializedRun(
                directory=tmp_path,
                document=tmp_path / "Resume.md",
                report=tmp_path / "Tailoring_Report.md",
                cover_letter=None,
                review=None,
            ),
            notes=["Checks still failing."],
        )

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.run", fake_run)
    result = runner.invoke(
        app, ["run", "posting.txt", "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 4
    assert "FAIL sources cited" in result.output
    assert "Checks still failing." in result.output


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Answer every stage from the shared script, and never build an SDK client."""
    sources: dict[str, Any] = {}
    provider = ScriptedProvider(script(sources))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    sources["provider"] = provider
    return sources


def posting_at(tmp_path: Path) -> str:
    path = tmp_path / "posting.txt"
    path.write_text(POSTING, encoding="utf-8")
    return str(path)


def rules_config(tmp_path: Path, master_cv: Path, body: str) -> Path:
    """Write a settings file carrying a `[rules]` block."""
    path = tmp_path / "clearscout.toml"
    path.write_text(
        f'cv = "{master_cv}"\noutput_dir = "{tmp_path / "runs"}"\ncache = false\n'
        f"\n[rules]\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_template_writes_a_skeleton_that_the_rest_of_the_tool_can_read(tmp_path: Path) -> None:
    """A format described in prose is not a format somebody can start from."""
    from clearscout.application.master_cv.cv import parse_cv

    destination = tmp_path / "CV_Master.md"
    result = runner.invoke(app, ["template", str(destination)])
    assert result.exit_code == 0
    assert f"Wrote {destination}" in result.output
    assert parse_cv(destination).entries
    assert "clearscout import" in result.output, "the alternative route is not offered"


def test_template_refuses_to_overwrite_a_master_cv(tmp_path: Path) -> None:
    """Silently replacing a career record with an empty skeleton is unrecoverable."""
    destination = tmp_path / "CV_Master.md"
    destination.write_text("# Mine\n", encoding="utf-8")
    result = runner.invoke(app, ["template", str(destination)])
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert destination.read_text(encoding="utf-8") == "# Mine\n"


def test_init_without_a_terminal_says_what_to_do_instead(tmp_path: Path) -> None:
    """It asks questions, and a piped run must not block on a hidden prompt."""
    result = runner.invoke(app, ["init", "--config", str(tmp_path / "clearscout.toml")])
    assert result.exit_code == 2
    assert "no terminal attached" in result.output
    assert "Traceback" not in result.output


def test_init_can_create_a_complete_workspace_noninteractively(
    tmp_path: Path, master_cv: Path
) -> None:
    root = tmp_path / "ClearScout" / "workspaces" / "scripted"
    database = tmp_path / "ClearScout" / "clearscout.sqlite3"
    result = runner.invoke(
        app,
        [
            "init",
            "--cv",
            str(master_cv),
            "--name",
            "Scripted Search",
            "--slug",
            "scripted",
            "--root",
            str(root),
            "--provider",
            "openai",
            "--database-url",
            f"sqlite+pysqlite:///{database}",
        ],
    )
    assert result.exit_code == 0
    assert f"Initialized {root}" in result.output
    assert "CV import: adopted" in result.output
    assert (root / "master-cv" / "CV_Master.md").is_file()
    assert (root / "clearscout.toml").is_file()
    assert database.is_file()


def setup_result(path: Path, master_cv: Path) -> Any:
    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.bootstrap.setup import ApplicationSetupResult

    return ApplicationSetupResult(
        config_path=path,
        master_cv=master_cv,
        inventory=parse_cv(master_cv),
        provider=SUITE_PROVIDER,
        had_credential=True,
        workspace_id="workspace-1",
        workspace_root=path.parent,
        database_url=f"sqlite+pysqlite:///{path.parent / 'clearscout.sqlite3'}",
        import_method="adopted",
    )


def test_init_reports_what_it_wrote_and_the_one_thing_left_to_do(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinctions are the setting that makes the output trustworthy."""
    path = tmp_path / "clearscout.toml"
    monkeypatch.setattr(
        "clearscout.bootstrap.setup.run_application_setup",
        lambda *a, **k: setup_result(path, master_cv),
    )
    result = runner.invoke(app, ["init", "--config", str(path)])
    assert result.exit_code == 0
    assert f"Initialized {tmp_path}" in result.output
    assert str(path) in result.output
    assert "distinctions" in result.output
    assert str(master_cv) in result.output


@pytest.fixture
def importable(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A provider that returns one faithful structuring of the messy source CV."""
    extracted = tidy_extraction()
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(
            {"ImportedCv": lambda schema: extracted.model_dump(mode="json")}
        ),
    )
    return extracted


def messy_cv_at(tmp_path: Path) -> Path:
    path = tmp_path / "old.txt"
    path.write_text(MESSY_CV, encoding="utf-8")
    return path


def test_import_writes_both_files_and_says_to_read_them(
    tmp_path: Path, master_cv: Path, importable: Any
) -> None:
    source = messy_cv_at(tmp_path)
    destination = tmp_path / "CV_Master.md"
    result = runner.invoke(
        app,
        [
            "import",
            str(source),
            "--out",
            str(destination),
            "--config",
            str(config_at(tmp_path, master_cv)),
        ],
    )
    assert result.exit_code == 0
    assert destination.is_file()
    assert (tmp_path / "Import_Report.md").is_file()
    assert "Import_Report.md" in result.output
    assert "It is your career, and a machine just retyped it." in result.output


def test_import_prints_what_the_run_cost_by_stage(
    tmp_path: Path, master_cv: Path, importable: Any
) -> None:
    config = config_at(tmp_path, master_cv)
    with config.open("a", encoding="utf-8") as handle:
        handle.write('\n[prices."gpt-6.1-sol"]\ninput = 100000.0\noutput = 100000.0\n')
    result = runner.invoke(
        app,
        [
            "import",
            str(messy_cv_at(tmp_path)),
            "--out",
            str(tmp_path / "CV_Master.md"),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0
    assert "Cost: $1.5000 (1 model call)" in result.output
    assert any(line.strip().startswith("structure ") for line in result.output.splitlines())


def test_import_names_the_lines_it_left_out(
    tmp_path: Path, master_cv: Path, importable: Any
) -> None:
    """A deliberate drop is reported rather than hidden; the footer is one."""
    result = runner.invoke(
        app,
        [
            "import",
            str(messy_cv_at(tmp_path)),
            "--out",
            str(tmp_path / "CV_Master.md"),
            "--config",
            str(config_at(tmp_path, master_cv)),
        ],
    )
    assert result.exit_code == 0
    assert "left out" in result.output


def test_import_whose_checks_fail_still_writes_and_exits_four(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files are the point; the exit code is how a script tells them apart."""
    extracted = tidy_extraction()
    invented = extracted.model_dump(mode="json")
    invented["summary"][0]["text"] = "A globally recognised authority on data platforms."
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider({"ImportedCv": lambda schema: invented}),
    )

    destination = tmp_path / "CV_Master.md"
    result = runner.invoke(
        app,
        [
            "import",
            str(messy_cv_at(tmp_path)),
            "--out",
            str(destination),
            "--config",
            str(config_at(tmp_path, master_cv)),
        ],
    )
    assert result.exit_code == 4
    assert destination.is_file()
    assert "did not pass" in result.output
    assert "FAIL" in result.output


def test_import_refuses_to_replace_a_master_cv_without_force(
    tmp_path: Path, master_cv: Path, importable: Any
) -> None:
    destination = tmp_path / "CV_Master.md"
    destination.write_text("# Mine\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "import",
            str(messy_cv_at(tmp_path)),
            "--out",
            str(destination),
            "--config",
            str(config_at(tmp_path, master_cv)),
        ],
    )
    assert result.exit_code == 2
    assert "--force" in result.output
    assert destination.read_text(encoding="utf-8") == "# Mine\n"


def test_screen_leads_with_the_gates_and_costs_no_draft(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    """The cheapest output and the only one that decides whether to spend a day."""
    result = runner.invoke(
        app,
        [
            "screen",
            posting_at(tmp_path),
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    output = result.output
    assert output.index("Deal-breakers") < output.index("requirements,")
    assert "sponsorship" in output
    assert "answered by" in output
    assert "clearscout run" in output
    # Two calls, and neither of them writes anything: no draft, no critic.
    names = [name for name, _ in scripted["provider"].calls]
    assert len(names) == 2
    assert not [name for name in names if "Draft" in name or "Critic" in name]

    # The rule-based reading reaches the terminal, says what it is, and prints
    # the questions it declined to answer rather than only the ones it could.
    assert "not by a model" in output
    assert "Not a score" in output
    assert "Role family" in output
    assert "not stated" in output


def test_screen_reads_the_posting_with_single_posting_intent(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person typing one posting URL earns the robots single-read exemption."""
    from clearscout.domain.records import PostingReadIntent
    from clearscout.infrastructure.web_fetch.ingest import JDText

    seen: list[PostingReadIntent] = []

    def scripted_ingest(argument: str, vision: Any = None, *, intent: PostingReadIntent) -> JDText:
        del vision
        seen.append(intent)
        return JDText(text=POSTING, origin=argument, method="plain text")

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.service.ingest", scripted_ingest)

    result = runner.invoke(
        app,
        [
            "screen",
            posting_at(tmp_path),
            "--config",
            str(config_at(tmp_path, master_cv)),
        ],
    )

    assert result.exit_code == 0
    assert seen == [PostingReadIntent.SINGLE_POSTING]


def test_batch_reads_each_named_posting_with_single_posting_intent(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: postings a person names on the command line are single-posting reads."""
    from clearscout.domain.records import PostingReadIntent
    from clearscout.infrastructure.web_fetch.ingest import JDText

    seen: list[PostingReadIntent] = []

    def scripted_ingest(argument: str, vision: Any = None, *, intent: PostingReadIntent) -> JDText:
        del vision
        seen.append(intent)
        return JDText(text=POSTING, origin=argument, method="plain text")

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.service.ingest", scripted_ingest)

    result = runner.invoke(
        app,
        ["batch", *two_postings(tmp_path), "--config", str(config_at(tmp_path, master_cv))],
    )

    assert result.exit_code == 0, result.output
    assert seen == [PostingReadIntent.SINGLE_POSTING] * 2


def test_screen_lists_the_requirements_the_cv_does_not_support(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap is the thing a person acts on, so it is named rather than counted."""
    sources: dict[str, Any] = {}
    built = script(sources)
    original = built["EvidenceMap"]

    def gapped(schema: Any) -> dict[str, Any]:
        payload = original(schema)
        for index, match in enumerate(payload["matches"]):
            match["classification"] = "GAP" if index else "ASSERTED"
            match["entry_ids"] = []
            match["strength"] = "none"
        return payload

    built["EvidenceMap"] = gapped
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(built),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    result = runner.invoke(
        app, ["screen", posting_at(tmp_path), "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 0
    assert "your CV does not support" in result.output
    assert "answered by what you state rather than by evidence" in result.output
    assert "answered by your search parameters" in result.output
    assert "absolute condition(s) not answered by direct evidence" not in result.output


@pytest.mark.parametrize(
    "condition", ["Must be available to work evenings and weekends", "Fluent in spoken French"]
)
def test_screen_labels_an_hours_or_language_gate_as_one_no_record_answers(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """Neither the terminal nor the screening report counts it as an unmet absolute condition."""
    built = script({})
    original = built["JobSpecExtraction"]

    def personal_gate(schema: Any) -> dict[str, Any]:
        payload = original(schema)
        payload["hard_gates"] = [{"kind": "other", "text": condition, "blocking": True}]
        return payload

    built["JobSpecExtraction"] = personal_gate
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(built),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    posting = tmp_path / "posting.txt"
    posting.write_text(f"{POSTING}{condition}.", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "screen",
            str(posting),
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--out",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 0
    label = "you, since a career record cannot answer hours or language"
    assert f"answered by {label}" in result.output
    assert "absolute condition(s) not answered by direct evidence" not in result.output
    (report,) = (tmp_path / "out").rglob("Screening_Report.md")
    text = report.read_text(encoding="utf-8")
    assert label in text
    assert "absolute condition(s) are not answered by direct evidence" not in text


def test_a_page_that_yielded_no_requirements_is_refused_rather_than_screened(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No requirements makes every claim vacuously true, which reads as a clean bill.

    A login wall, a board index, or a page that builds itself with JavaScript is
    what actually arrives, so the answer names those and says what to do instead.
    """
    built = script({})
    built["JobSpecExtraction"] = lambda schema: {
        "title": "Sign in to continue",
        "company": None,
        "seniority": None,
        "location": None,
        "requirements": [],
        "keywords": [],
        "hard_gates": [],
    }
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(built),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    result = runner.invoke(
        app, ["screen", posting_at(tmp_path), "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 3
    assert "No requirements could be read" in result.output
    assert "screenshot it" in result.output


def test_screen_says_a_posting_states_no_deal_breakers_only_when_it_checked_some(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the line is earned by a failed read rather than by the posting."""
    built = script({})
    original = built["JobSpecExtraction"]

    def ungated(schema: Any) -> dict[str, Any]:
        payload = original(schema)
        payload["hard_gates"] = []
        return payload

    built["JobSpecExtraction"] = ungated
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: ScriptedProvider(built),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    result = runner.invoke(
        app, ["screen", posting_at(tmp_path), "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 0
    assert "The posting states no deal-breakers." in result.output
    assert "have supporting evidence" in result.output
    # The two readings of this posting differ on which requirements are
    # essential, and the screen says so rather than picking one of them.
    assert "One of the two is wrong" in result.output


def test_a_clean_run_exits_zero_and_names_every_file_it_wrote(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            posting_at(tmp_path),
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--cover-letter",
            "--verbose",
            "--no-cache",
        ],
    )
    assert result.exit_code == 0
    # The scripted run clears every check first time, so no critique is asked for.
    assert "passed, " in result.output and " warned; " in result.output
    assert "the critic did not read the resume" in result.output
    assert "All " not in result.output
    assert "the cover letter" in result.output
    assert "read this first" in result.output
    # --verbose earns its name: every check, and what each stage cost.
    assert "PASS sources cited" in result.output
    assert "draft" in result.output and "Cost:" in result.output


def test_a_render_that_warned_prints_the_warning_beside_what_it_wrote(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def rendered(document: Path, *, letter: bool = False) -> rendering.Rendered:
        del letter
        return rendering.Rendered(
            attempted=True,
            ok=True,
            message="Rendered the PDF",
            produced=(document.with_suffix(".pdf"),),
            warnings=("pressume warned: readability: the final page is lightly filled",),
        )

    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.rendering.render", rendered
    )
    result = runner.invoke(
        app,
        [
            "run",
            posting_at(tmp_path),
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--render",
            "--no-cache",
        ],
    )
    assert result.exit_code == 0
    assert "Rendered the PDF" in result.output
    assert "readability: the final page is lightly filled" in result.output


def test_re_running_says_it_returned_the_earlier_run_rather_than_counting_nothing(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    """A returned run carries no findings, so "All 0 checks passed" would be a lie."""
    config = str(config_at(tmp_path, master_cv))
    source = posting_at(tmp_path)
    assert runner.invoke(app, ["run", source, "--config", config]).exit_code == 0

    result = runner.invoke(app, ["run", source, "--config", config])
    assert result.exit_code == 0
    assert "Returned the run already on disk" in result.output
    assert "nothing was billed" in result.output


def test_a_run_that_cannot_clear_its_checks_warns_before_it_reports_the_cost(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    """The failure is spent, written, and labeled: the warning is the deliverable."""
    config = rules_config(tmp_path, master_cv, 'forbidden = ["Delivered platform work"]')
    result = runner.invoke(app, ["run", posting_at(tmp_path), "--config", str(config)])
    assert result.exit_code == 4
    assert "Not sendable" in result.output
    assert "FAIL forbidden strings" in result.output
    assert "Do not send this as it stands" in result.output


def test_the_exit_codes_are_the_ones_the_user_guide_promises(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script batching postings reads these, and the table is a promise."""
    from clearscout.shared.errors import (
        AuthError,
        BudgetError,
        ConfigError,
        CvParseError,
        IngestError,
        ProviderError,
        QuotaError,
    )

    expected = {
        BudgetError: 1,
        ConfigError: 2,
        AuthError: 2,
        QuotaError: 2,
        IngestError: 3,
        CvParseError: 3,
        ProviderError: 3,
    }
    config = str(config_at(tmp_path, master_cv))
    for error, code in expected.items():

        def failing(*args: Any, _error: type[Exception] = error, **kwargs: Any) -> None:
            raise _error("the reason, stated plainly")

        monkeypatch.setattr("clearscout.application.tailoring.pipeline.run", failing)
        result = runner.invoke(app, ["run", "posting.txt", "--config", config])
        assert result.exit_code == code, f"{error.__name__} should exit {code}"
        assert "the reason, stated plainly" in result.output
        assert "Traceback" not in result.output


def two_postings(tmp_path: Path) -> list[str]:
    """Write two related postings and return their paths."""
    paths = []
    for name, extra in (("one", "Fusion Ledger is the billing system. "), ("two", "")):
        path = tmp_path / f"{name}.txt"
        path.write_text(extra + POSTING, encoding="utf-8")
        paths.append(str(path))
    return paths


def test_batch_screens_several_postings_and_writes_a_report(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    result = runner.invoke(
        app,
        [
            "batch",
            *two_postings(tmp_path),
            "--config",
            str(config_at(tmp_path, master_cv)),
            "--out",
            str(tmp_path / "batch"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2 posting(s), screened separately. Nothing drafted." in result.output
    assert "None of the above is a score." in result.output
    assert (tmp_path / "batch" / "Batch_Report.md").is_file()
    assert (tmp_path / "batch" / "batch.json").is_file()
    assert "clearscout discover --queue" in result.output


def test_batch_refuses_arguments_and_a_plan_file_at_once(tmp_path: Path, master_cv: Path) -> None:
    plan = tmp_path / "batch.toml"
    plan.write_text('[[posting]]\nsource = "a.txt"\n', encoding="utf-8")
    result = runner.invoke(
        app,
        ["batch", "a.txt", "--plan", str(plan), "--config", str(config_at(tmp_path, master_cv))],
    )
    assert result.exit_code == 2
    assert "not both" in result.output


def test_batch_takes_its_postings_priorities_and_notes_from_a_plan_file(
    tmp_path: Path, master_cv: Path, scripted: dict[str, Any]
) -> None:
    first, second = two_postings(tmp_path)
    plan = tmp_path / "batch.toml"
    plan.write_text(
        f'[[posting]]\nsource = "{first}"\n\n'
        f'[[posting]]\nsource = "{second}"\npriority = 4\nnote = "closes on Friday"\n',
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["batch", "--plan", str(plan), "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 0, result.output
    assert "2 posting(s)" in result.output


def test_discover_refuses_when_nobody_is_at_the_terminal(tmp_path: Path, master_cv: Path) -> None:
    """A conversation with a pipe is not a conversation."""
    result = runner.invoke(
        app, ["discover", "--about", "Kubernetes", "--config", str(config_at(tmp_path, master_cv))]
    )
    assert result.exit_code == 2
    assert "nobody at this terminal" in result.output


def test_discover_refuses_when_there_is_nothing_to_ask_about(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.delivery.cli.support import terminal

    monkeypatch.setattr(terminal, "interactive", lambda: True)
    result = runner.invoke(app, ["discover", "--config", str(config_at(tmp_path, master_cv))])
    assert result.exit_code == 2
    assert "Nothing to ask about" in result.output


def test_discover_asks_confirms_and_writes_to_the_master_cv(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole command, with a scripted person and no model anywhere."""
    import shutil

    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.delivery.cli.support import terminal

    copied = tmp_path / "CV_Master.md"
    shutil.copy(master_cv, copied)
    monkeypatch.setattr(terminal, "interactive", lambda: True)
    answers = [
        "I built the retry layer for the ingestion jobs myself.",
        "No.",
        "It ran every night.",
        "Built the retry layer for the ingestion jobs.",
        "the ingestion team, over one quarter",
        "",
        "how long it stayed in place",
        *["y"] * 8,
        "y",
    ]
    result = runner.invoke(
        app,
        ["discover", "--about", "Kubernetes", "--config", str(config_at(tmp_path, copied))],
        input="\n".join(answers) + "\n",
    )
    assert result.exit_code == 0, result.output
    assert "Added 1 confirmed fact(s)" in result.output
    assert "Discovery_Log.md" in result.output
    entry = next(
        item
        for item in parse_cv(copied).entries
        if item.text == "Built the retry layer for the ingestion jobs."
    )
    assert entry.id in result.output


def test_discover_reports_gaps_it_could_not_close(
    tmp_path: Path, master_cv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import shutil

    from clearscout.delivery.cli.support import terminal

    copied = tmp_path / "CV_Master.md"
    shutil.copy(master_cv, copied)
    record = tmp_path / "batch.json"
    record.write_text(
        json.dumps(
            {
                "schema": 1,
                "queue": [{"subject": "Rust", "gap_ids": ["one:r02"], "postings": ["one"]}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(terminal, "interactive", lambda: True)
    result = runner.invoke(
        app,
        ["discover", "--queue", str(record), "--config", str(config_at(tmp_path, copied))],
        input="No.\n",
    )
    assert result.exit_code == 0, result.output
    assert "1 gap(s) are still gaps: one:r02" in result.output
    assert copied.read_text(encoding="utf-8") == master_cv.read_text(encoding="utf-8")


def _sqlite_operational_error(message: str, code: int, name: str) -> OperationalError:
    """Return the SQLAlchemy error the driver raises for ``code``, built without a real lock."""
    driver = sqlite3.OperationalError(message)
    driver.sqlite_errorcode = code
    driver.sqlite_errorname = name
    return OperationalError("INSERT INTO task (id) VALUES (?)", ("t-1",), driver)


def _start_raising(monkeypatch: pytest.MonkeyPatch, error: OperationalError) -> Any:
    """Run `clearscout start` with its startup work raising ``error``."""

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.start_local_application", fail)
    return runner.invoke(app, ["start"])


def test_start_on_a_database_busy_with_another_process_prints_one_line_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy = _sqlite_operational_error("database is locked", sqlite3.SQLITE_BUSY, "SQLITE_BUSY")

    result = _start_raising(monkeypatch, busy)

    assert result.exit_code == 1
    printed = result.stderr.strip()
    assert "database file is busy with another process" in printed
    assert "\n" not in printed
    assert "Traceback" not in result.output


def test_start_leaves_any_other_database_operational_error_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _sqlite_operational_error("no such table: task", sqlite3.SQLITE_ERROR, "SQLITE_ERROR")

    result = _start_raising(monkeypatch, other)

    assert result.exception is other
    assert "busy with another process" not in result.output
