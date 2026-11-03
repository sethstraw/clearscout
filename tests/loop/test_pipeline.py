"""The whole pipeline, against a scripted provider. Markdown in, Markdown out.

The failures worth catching: a model citing evidence that does not exist, a
fabricated figure reaching the page, and a poisoned master CV leaking bait.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clearscout.application.master_cv.cv import EntryKind
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.layout import markdown_lines
from clearscout.application.tailoring.pipeline import (
    MaterializedRun,
    RunCommand,
    RunResult,
    run,
)
from clearscout.application.tailoring.pipeline.models import PageSelection
from clearscout.application.tailoring.stages.jobspec import StoredPostingName
from clearscout.application.tailoring.stages.select import TopUpStop
from clearscout.application.tailoring.stages.verify import SCORED_AXES
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.results import Finding, Outcome
from tests.support.doubles import ScriptedPrompter, ScriptedProvider
from tests.support.payloads import script
from tests.support.schema import literals
from tests.support.suite import (
    FIXTURES,
    POSTING,
    SUITE_KEY_VARIABLE,
    settings_for,
)


def posting(tmp_path: Path) -> str:
    path = tmp_path / "posting.txt"
    path.write_text(POSTING, encoding="utf-8")
    return str(path)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point the pipeline at a scripted provider and a key that is never sent."""
    sources: dict[str, Any] = {}
    provider = ScriptedProvider(script(sources))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    sources["provider"] = provider
    return sources


def go(master_cv: Path, tmp_path: Path, **kwargs: Any) -> Any:
    pages = kwargs.pop("pages", 2)
    update = kwargs.pop("settings_update", None)
    kwargs.setdefault("force", True)
    settings = settings_for(master_cv, tmp_path, pages)
    if update:
        settings = settings.model_copy(update=update)
    return run(
        RunCommand(source=posting(tmp_path), settings=settings, cache_enabled=False, **kwargs)
    )


def written_run(result: RunResult) -> MaterializedRun:
    assert result.materialized is not None
    return result.materialized


def leaning_on_transferable(answers: dict[str, Any]) -> None:
    """Weaken the scripted match, which is what asks the critic for a second opinion.

    The scripted posting is otherwise answered by direct evidence at full keyword
    coverage on the first attempt, the one shape no risk signal fires on.
    """
    direct = answers["EvidenceMap"]

    def leaning(schema: Any) -> dict[str, Any]:
        payload: dict[str, Any] = direct(schema)
        for match in payload["matches"]:
            match["classification"] = "TRANSFERABLE"
        return payload

    answers["EvidenceMap"] = leaning


def test_a_full_run_produces_a_clean_markdown_package(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(master_cv, tmp_path)
    written = written_run(result)
    assert written.document.is_file()
    assert written.document.suffix == ".md"
    assert written.report.is_file()
    assert result.verification.passed
    assert result.serialized.document == written.document.read_bytes()
    assert result.serialized.report == written.report.read_bytes()
    assert result.serialized.manifest_json == (written.directory / "run.json").read_bytes()
    assert result.serialized.manifest == json.loads(result.serialized.manifest_json)
    assert written.review is not None
    assert result.serialized.review_json == written.review.read_bytes()
    assert result.serialized.review == json.loads(result.serialized.review_json)
    assert result.serialized.review_markdown == written.review.with_name("changes.md").read_bytes()

    outcomes = {finding.check: finding.outcome for finding in result.verification.findings}
    assert outcomes["sources cited"] is Outcome.PASS
    assert outcomes["numbers traced"] is Outcome.PASS
    assert outcomes["document structure"] is Outcome.PASS


def test_a_non_materialized_run_returns_serialized_artifacts_without_an_output_tree(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    settings = settings_for(master_cv, tmp_path)

    result = run(
        RunCommand(
            source=posting(tmp_path),
            settings=settings,
            cache_enabled=False,
            force=True,
            materialize=False,
        )
    )

    assert result.materialized is None
    assert result.serialized.document
    assert result.serialized.report
    assert result.serialized.manifest_json
    assert result.serialized.review_json
    assert result.serialized.review_markdown
    assert not settings.output_dir.exists()


def test_rendering_a_non_materialized_run_is_refused_before_provider_work(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    from clearscout.shared.errors import ConfigError

    settings = settings_for(master_cv, tmp_path)

    with pytest.raises(ConfigError, match="Rendering requires a materialized tailoring run"):
        run(
            RunCommand(
                source=posting(tmp_path),
                settings=settings,
                cache_enabled=False,
                force=True,
                materialize=False,
                want_render=True,
            )
        )

    assert scripted["provider"].calls == []
    assert not settings.output_dir.exists()


def test_the_run_writes_markdown_and_nothing_else_to_render(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """No binary artifact should ever appear: this tool is not a renderer."""
    result = go(master_cv, tmp_path)
    produced = {path.suffix for path in written_run(result).directory.iterdir()}
    assert produced == {".md", ".json"}


def test_the_report_leads_with_gates_and_carries_provenance(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(master_cv, tmp_path)
    text = written_run(result).report.read_text(encoding="utf-8")
    assert text.index("## Hard gates") < text.index("## Requirement map")
    assert "No visa sponsorship" in text
    assert "## Provenance" in text
    assert scripted["draft_ids"][0] in text

    # The answer column keeps absolute conditions visible in reports.
    gates = text[text.index("## Hard gates") : text.index("## Gaps")]
    assert "Answered by" in gates, "the gates table does not report how gates were answered"
    assert any(
        phrase in gates
        for phrase in (
            "direct evidence",
            "transferable",
            "no evidence",
            "stated by you",
            "not checked",
            "your search parameters",
        )
    ), "the gates table reports no verdict for any gate"


def test_the_report_says_length_was_estimated_and_lists_what_to_cut(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(master_cv, tmp_path)
    text = written_run(result).report.read_text(encoding="utf-8")
    assert "## Length" in text
    assert "does not render" in text
    assert "--trim" in text


def test_the_run_says_plainly_that_the_page_count_is_unverified(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(master_cv, tmp_path)
    assert any("not measured" in note for note in result.notes)


def test_the_run_notes_a_must_have_tool_no_cited_evidence_names(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources: dict[str, Any] = {}
    scripted_run = script(sources)
    extract, answer = scripted_run["JobSpecExtraction"], scripted_run["EvidenceMap"]

    def asks_for_a_tool_the_record_never_names(schema: Any) -> dict[str, Any]:
        spec = extract(schema)
        spec["requirements"][0] = {"text": "Snowflake", "importance": "must_have"}
        return spec

    def lists_the_tool(schema: Any) -> dict[str, Any]:
        mapped = answer(schema)
        mapped["matches"][0]["named_tools"] = ["Snowflake"]
        return mapped

    scripted_run["JobSpecExtraction"] = asks_for_a_tool_the_record_never_names
    scripted_run["EvidenceMap"] = lists_the_tool
    provider = ScriptedProvider(scripted_run)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    result = go(master_cv, tmp_path)

    assert 'No evidence cited for "Snowflake" names Snowflake.' in result.notes


def test_a_draft_that_runs_out_of_cited_evidence_says_so_rather_than_filling(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The fixture record cannot fill two pages, and the run says why and what to try."""
    result = go(master_cv, tmp_path, pages=2)
    short = [note for note in result.notes if "short of 2 page(s)" in note]

    assert len(short) == 1, result.notes
    assert "Every cited bullet from the recent roles (the last" in short[0]
    assert "re-run with --pages 1" in short[0]
    assert "clearscout.toml" not in short[0]
    report = written_run(result).report.read_text(encoding="utf-8")
    length = report[report.index("## Length") :]
    assert "The resume is estimated at" in length
    assert "Every cited bullet from the recent roles" in length


def test_the_stored_selection_explains_the_page_on_its_own(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """run.json alone says what was chosen, why each entry earned its place, and how it fit."""
    result = go(master_cv, tmp_path)
    written = written_run(result)
    stored = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    selection = PageSelection.model_validate(stored["selection"])
    entries = {entry.id: entry for entry in selection.entries}
    cited_by = {
        (entry_id, match.requirement_id)
        for match in selection.matches
        for entry_id in match.entry_ids
    }
    asked = {requirement.id for requirement in selection.requirements}
    printed = [
        entry_id
        for role in selection.roles
        for entry_id in role.bullet_ids
        if entry_id not in selection.excluded
    ]

    assert printed, "the stored selection names no bullet on the page"
    for entry_id in printed:
        entry = entries[entry_id]
        assert entry.selected
        assert entry.requirement_ids, f"{entry_id} reached the page without a cited requirement"
        assert all((entry_id, req) in cited_by for req in entry.requirement_ids)
        assert set(entry.requirement_ids) <= asked
    assert (
        selection.spent_lines + selection.unspent_lines - selection.overshoot_lines
        == selection.selection_lines
    )
    assert selection.spent_lines == sum(role.bullet_lines for role in selection.roles)
    assert selection.top_up_stop is TopUpStop.EVIDENCE
    assert all(
        entry.selected
        for entry in selection.entries
        if entry.kind is EntryKind.BULLET and entry.requirement_ids
    ), "top-up stopped for want of evidence while a cited bullet sat unselected"
    document = written.document.read_text(encoding="utf-8")
    assert sum(selection.drafted_lines.values()) == pytest.approx(markdown_lines(document))
    assert sum(selection.drafted_lines.values()) <= selection.page_lines
    assert selection.summary_ids and selection.skill_ids and selection.education_ids
    settings = settings_for(master_cv, tmp_path, 2)
    assert (
        selection.min_bullets_per_role,
        selection.min_publications,
        selection.full_bullet_years,
    ) == (settings.min_bullets_per_role, settings.min_publications, settings.full_bullet_years)


def test_what_the_resume_trims_is_neither_the_letters_evidence_nor_the_critics(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The letter cites, and the critic judges against, the page as it prints."""
    answers = scripted["provider"].script
    leaning_on_transferable(answers)
    citable: dict[str, list[str]] = {}
    for writer in ("ResumeDraft", "CoverLetter"):

        def capture(schema: Any, writer: str = writer, answer: Any = answers[writer]) -> Any:
            citable[writer] = literals(schema, "source_ids")
            return answer(schema)

        answers[writer] = capture

    result = go(master_cv, tmp_path, want_cover_letter=True, trim=2)

    stored = json.loads((written_run(result).directory / "run.json").read_text(encoding="utf-8"))
    selection = PageSelection.model_validate(stored["selection"])
    off_page = set(selection.excluded)
    copied = {*selection.education_ids, *selection.publication_ids}
    critic = next(
        plan.task.text
        for name, plan in scripted["provider"].calls
        if "CriticVerdict" in name and "tailored resume " in plan.task.text
    )
    offered = critic.split("<offered_evidence>")[1].split("</offered_evidence>")[0].split()
    printed = [entry_id for entry_id in citable["ResumeDraft"] if entry_id not in off_page]
    letter = [entry_id for entry_id in citable["CoverLetter"] if entry_id in citable["ResumeDraft"]]

    assert off_page, "--trim 2 took nothing off the page"
    assert offered == printed
    # Education and publications print verbatim; the letter drafter never sees them.
    assert letter == [entry_id for entry_id in printed if entry_id not in copied]


def test_trimming_removes_items_in_drop_order(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Trimming has to remove something, and publications go before bullets."""
    full = go(master_cv, tmp_path)
    assert written_run(full).document.read_text(encoding="utf-8")
    assert len(full.spec.requirements) == 4

    plan_drops = go(master_cv, tmp_path, trim=2)
    assert len(written_run(plan_drops).document.read_text(encoding="utf-8")) < len(
        written_run(full).document.read_text(encoding="utf-8")
    )


def test_the_deliverable_carries_no_annotation(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    markdown = written_run(go(master_cv, tmp_path)).document.read_text(encoding="utf-8")
    for marker in ("clearscout", "<!--", "Tailoring report", "Run date"):
        assert marker not in markdown


def test_a_cover_letter_appears_only_when_asked(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    assert written_run(go(master_cv, tmp_path)).cover_letter is None
    letter = written_run(go(master_cv, tmp_path, want_cover_letter=True)).cover_letter
    assert letter is not None
    assert letter.is_file()


def test_a_poisoned_master_cv_never_leaks_its_bait(
    tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The drafter can only cite what exists, so bait must be cited to appear."""
    result = go(FIXTURES / "poisoned_cv.md", tmp_path, pages=1)
    markdown = written_run(result).document.read_text(encoding="utf-8")
    assert "9,999,999" not in markdown
    assert "Chief Data Officer" not in markdown
    outcomes = {finding.check: finding.outcome for finding in result.verification.findings}
    assert outcomes["numbers traced"] is Outcome.PASS


def test_a_run_without_a_key_and_without_a_terminal_fails_early(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SUITE_KEY_VARIABLE, raising=False)
    monkeypatch.setattr("keyring.get_password", lambda service, name: None)
    from clearscout.shared.errors import AuthError

    with pytest.raises(AuthError, match="clearscout auth set"):
        run(
            RunCommand(
                source=posting(tmp_path),
                settings=settings_for(master_cv, tmp_path),
                prompter=ScriptedPrompter(interactive=False),
                cache_enabled=False,
            )
        )


def test_a_fabricated_figure_is_caught_before_the_critic_runs(
    master_cv: Path, tmp_path: Path
) -> None:
    """A drafter that invents a number must not reach the critic at all."""
    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.application.tailoring.guardrails import Guardrails
    from clearscout.application.tailoring.stages.draft import DraftedLine, ResumeDraft
    from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
    from clearscout.application.tailoring.stages.match import EvidenceMap
    from clearscout.application.tailoring.stages.select import build_plan
    from clearscout.application.tailoring.stages.verify import check

    inventory = parse_cv(master_cv)
    settings = settings_for(master_cv, tmp_path)
    spec = JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(Requirement(id="r01", text="Kafka", importance=Importance.MUST_HAVE),),
        keywords=("Kafka",),
        hard_gates=(),
        origin="test",
    )
    plan = build_plan(inventory, spec, EvidenceMap(), settings)
    source = next(e.id for e in inventory.entries if e.text.startswith("Rebuilt the ingestion"))
    report = check(
        markdown="# X\n\nx@example.com\n\n---\n\n## Summary\n\nText.\n",
        draft=ResumeDraft(
            summary=DraftedLine(text="Cut latency across 8,400 feeds.", source_ids=(source,))
        ),
        plan=plan,
        inventory=inventory,
        spec=spec,
        guardrails=Guardrails(),
    )
    assert not report.passed
    assert "numbers traced" in report.feedback()


def test_a_posting_that_matches_nothing_is_refused_rather_than_half_written(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty experience section passes every check, which is the danger."""
    from clearscout.shared.errors import BudgetError

    sources: dict[str, Any] = {}
    script_with_no_matches = script(sources)
    script_with_no_matches["EvidenceMap"] = lambda schema: {"matches": []}
    provider = ScriptedProvider(script_with_no_matches)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    with pytest.raises(BudgetError, match="experience section would be empty"):
        run(
            RunCommand(
                source=posting(tmp_path),
                settings=settings_for(master_cv, tmp_path),
                cache_enabled=False,
            )
        )


def test_re_running_an_unchanged_posting_returns_the_existing_run(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Nothing that decides the output changed, so there is nothing to pay for.

    The existing run is handed back with any hand edits in it intact, rather
    than refused or overwritten.
    """
    first = go(master_cv, tmp_path, force=False)
    first_written = written_run(first)
    (first_written.directory / "my-edit.txt").write_text("mine", encoding="utf-8")

    second = go(master_cv, tmp_path, force=False)
    assert written_run(second).directory == first_written.directory
    assert second.attempts == 0
    assert (first_written.directory / "my-edit.txt").read_text(encoding="utf-8") == "mine"
    assert any("nothing was billed" in note for note in second.notes)


def test_force_drafts_again_beside_the_first_rather_than_over_it(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """--force means "do it again", never "destroy what is there"."""
    first = go(master_cv, tmp_path)
    first_written = written_run(first)
    (first_written.directory / "my-edit.txt").write_text("mine", encoding="utf-8")

    second = go(master_cv, tmp_path, force=True)
    assert written_run(second).document.is_file()
    assert (first_written.directory / "my-edit.txt").exists()


def test_every_run_for_one_posting_lives_under_that_posting(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A second run is run history, not a second application."""
    result = go(master_cv, tmp_path)
    directory = written_run(result).directory
    assert directory.parent.name == "runs"
    posting_dir = directory.parent.parent
    assert "--" in posting_dir.name, "the posting identifier belongs in the name"


def test_an_interrupted_run_leaves_nothing_behind(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written package is worse than no package, because it looks finished."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupted")

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.service.render_report", explode)
    with pytest.raises(RuntimeError):
        go(master_cv, tmp_path)

    leftovers = [path for path in tmp_path.rglob("*") if path.is_file() and path.suffix == ".md"]
    assert not [path for path in leftovers if path.name.startswith("Globex")]


def test_a_stale_cover_letter_cannot_survive_into_a_later_run(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A rerun leaves no file an earlier run wrote and this one did not.

    A Cover_Letter.md from an earlier run inside a package built without
    --cover-letter would be indistinguishable from one that run produced.
    """
    withletter = go(master_cv, tmp_path, want_cover_letter=True)
    withletter_written = written_run(withletter)
    assert withletter_written.cover_letter is not None

    without = go(master_cv, tmp_path, want_cover_letter=False, force=True)
    without_written = written_run(without)
    assert without_written.directory != withletter_written.directory
    assert not (without_written.directory / "Cover_Letter.md").exists()


def test_screening_answers_without_drafting_anything(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The cheapest and most decision-relevant output, available on its own."""
    from clearscout.application.tailoring.pipeline import screen

    result = screen(
        posting(tmp_path), settings_for(master_cv, tmp_path), cache_enabled=False, out=tmp_path
    )
    assert result.spec.hard_gates
    assert result.report is not None
    assert "Screening report" in result.report.read_text(encoding="utf-8")
    # Only the jobspec and match stages call the provider.
    assert [entry.stage for entry in result.ledger.entries] == ["jobspec", "match"]


def test_screening_costs_a_fraction_of_a_run(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    from clearscout.application.tailoring.pipeline import screen

    screened = screen(posting(tmp_path), settings_for(master_cv, tmp_path), cache_enabled=False)
    ran = go(master_cv, tmp_path)
    assert len(screened.ledger.entries) < len(ran.ledger.entries)


def test_a_discounted_screen_builds_its_client_with_the_longer_wait(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raised flex wait has to reach the client, not only the settings that state it."""
    from clearscout.application.config import FLEX_TIMEOUT_FLOOR
    from clearscout.application.tailoring.pipeline import screen
    from clearscout.shared.costs import ServiceTier

    sources: dict[str, Any] = {}
    provider = ScriptedProvider(script(sources))
    waits: list[float | None] = []

    def building(name: Any, key: Any, timeout: float | None = None) -> Any:
        waits.append(timeout)
        return provider

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.service.provider_for", building)
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    settings = settings_for(master_cv, tmp_path)

    screen(posting(tmp_path), settings, cache_enabled=False)
    screen(posting(tmp_path), settings, cache_enabled=False, service_tier=ServiceTier.FLEX)

    assert waits == [settings.request_timeout, FLEX_TIMEOUT_FLOOR]


class Watching:
    """A prompter that records every line, so narration can be asserted on."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def interactive(self) -> bool:
        return False

    def notice(self, message: str) -> None:
        self.lines.append(message)

    def plain(self, message: str) -> None:
        self.lines.append(message)

    def confirm(self, question: str, default: bool = True) -> bool:
        return default

    def ask(self, question: str, default: str = "") -> str:
        return default

    def secret(self, question: str) -> str:
        return ""

    def step(self, index: int, total: int, label: str) -> None:
        self.lines.append(f"[{index}/{total}] {label}")


def test_verbose_narrates_every_model_call_as_it_happens(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A drafting call at high effort takes minutes, and silence reads as a hang.

    One progress line covers up to three drafting attempts and three critic
    calls, so --verbose has to narrate each of them.
    """
    watching = Watching()
    leaning_on_transferable(scripted["provider"].script)
    go(master_cv, tmp_path, prompter=watching, verbose=True)
    written = "\n".join(watching.lines)

    for stage in ("jobspec", "match", "draft", "critic"):
        assert f"-> {stage}" in written, f"{stage} never announced itself"
        assert f"<- {stage}" in written, f"{stage} never reported back"

    # Progress includes model, effort, and token information.
    assert "effort=" in written
    assert "cacheable), up to" in written
    # Completion includes duration, token usage, and cost.
    assert "s  in " in written and "out " in written

    assert "drafting, attempt 1 of 3" in written

    assert "finish=complete" in written
    assert "reasoning" in written
    assert "resp_scripted" in written


def test_a_clean_first_draft_is_not_sent_to_the_critic(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The critic is paid for, so a draft with no risk signal does not buy one."""
    result = go(master_cv, tmp_path)

    assert result.verification.passed
    assert result.verification.critic is None
    assert not any("CriticVerdict" in name for name, _ in scripted["provider"].calls)
    skipped = [item for item in result.verification.findings if item.check == "critic"]
    assert [item.outcome for item in skipped] == [Outcome.WARN]
    assert "no critique was asked for" in skipped[0].detail


def test_a_draft_resting_on_transferable_evidence_is_sent_to_the_critic(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Weaker than direct evidence is the risk the critic is there to read."""
    leaning_on_transferable(scripted["provider"].script)
    result = go(master_cv, tmp_path)

    assert any("CriticVerdict" in name for name, _ in scripted["provider"].calls)
    assert result.verification.critic is not None


@pytest.mark.parametrize(
    ("context", "criticised"),
    [
        ("Helped with reporting pipelines for a manufacturing client base.", True),
        ("Built reporting pipelines for a manufacturing client base.", False),
    ],
)
def test_tailoring_only_evidence_worded_as_taking_part_is_sent_to_the_critic(
    master_cv: Path,
    tmp_path: Path,
    scripted: dict[str, Any],
    context: str,
    criticised: bool,
) -> None:
    """Participation wording is the claim a draft most readily upgrades to ownership."""
    from clearscout.application.master_cv.cv import parse_cv

    record = tmp_path / "master_cv.md"
    record.write_text(
        master_cv.read_text(encoding="utf-8").replace(
            "Built reporting pipelines for a manufacturing client base.", context
        ),
        encoding="utf-8",
    )
    cited = next(entry.id for entry in parse_cv(record).entries if entry.text == context)
    direct = scripted["provider"].script["EvidenceMap"]

    def tailoring_only(schema: Any) -> dict[str, Any]:
        payload: dict[str, Any] = direct(schema)
        for match in payload["matches"]:
            match["classification"] = "TAILORING_ONLY"
            match["entry_ids"] = [*match["entry_ids"], cited]
        return payload

    scripted["provider"].script["EvidenceMap"] = tailoring_only
    result = go(record, tmp_path)

    assert result.verification.passed
    assert any("CriticVerdict" in name for name, _ in scripted["provider"].calls) is criticised
    assert (result.verification.critic is not None) is criticised


def test_the_manifest_records_every_critic_draw_the_run_paid_for(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A merged score is not re-tunable against history unless the draws behind it are kept."""
    leaning_on_transferable(scripted["provider"].script)
    result = go(master_cv, tmp_path, settings_update={"critic_trials": 3})

    manifest = json.loads((written_run(result).directory / "run.json").read_text("utf-8"))
    drawn = manifest["run"]["critic_draws"]["resume"]
    assert len(drawn) == 3
    assert all(set(draw) == set(SCORED_AXES) for draw in drawn)


def test_each_critic_is_told_the_entries_its_writer_could_cite(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Alignment judged against entries the writer never had asks the redraft for the impossible."""
    answers = scripted["provider"].script
    leaning_on_transferable(answers)
    citable: dict[str, list[str]] = {}
    for writer in ("ResumeDraft", "CoverLetter"):

        def capture(schema: Any, writer: str = writer, answer: Any = answers[writer]) -> Any:
            citable[writer] = literals(schema, "source_ids")
            return answer(schema)

        answers[writer] = capture

    go(master_cv, tmp_path, want_cover_letter=True)

    critics = [plan for name, plan in scripted["provider"].calls if "CriticVerdict" in name]
    tasks = {
        document: next(
            plan.task.text for plan in critics if f"tailored {document} " in plan.task.text
        )
        for document in ("resume", "cover letter")
    }
    offered = {
        document: task.split("<offered_evidence>")[1].split("</offered_evidence>")[0].split()
        for document, task in tasks.items()
    }
    research = tasks["cover letter"].split("<company_research>")[1].split("</company_research>")[0]
    letter_request = next(
        plan.task.text for name, plan in scripted["provider"].calls if "CoverLetter" in name
    )
    assert offered["resume"] == citable["ResumeDraft"]
    # The letter may also cite company research, which the critic reads in its own block.
    extra = [source for source in citable["CoverLetter"] if f"Source ID: {source}" in research]
    assert extra
    assert offered["cover letter"] == [s for s in citable["CoverLetter"] if s not in extra]
    assert all(f"[{entry_id}]" in letter_request for entry_id in offered["cover letter"])


def test_the_recorded_critic_finding_names_evidence_and_requirements_by_their_words(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The tailoring review shows the recorded findings; the redraft keeps the ids."""
    from clearscout.application.master_cv.cv import parse_cv

    provider = scripted["provider"]
    leaning_on_transferable(provider.script)
    entry = parse_cv(master_cv).entries[0]
    approving = provider.script["CriticVerdict"]

    def critic(schema: Any) -> dict[str, Any]:
        verdict: dict[str, Any] = approving(schema)
        verdict["readability"] = 2
        verdict["guidance"] = f"Lead with [{entry.id}] to answer r01."
        verdict["weaknesses"] = [f"[{entry.id}] sits below weaker lines."]
        return verdict

    provider.script["CriticVerdict"] = critic
    result = go(master_cv, tmp_path)

    requirement = result.spec.requirements[0]
    assert requirement.id == "r01"
    recorded = next(
        item for item in result.serialized.manifest["run"]["findings"] if item["check"] == "critic"
    )
    opening = " ".join(entry.text.split()[:8])
    assert f'Lead with "{opening} ..." to answer "{requirement.text}".' in recorded["detail"]
    assert entry.id not in recorded["detail"]
    assert recorded["advisories"] == [f'"{opening} ..." sits below weaker lines.']
    kept = next(item for item in result.verification.findings if item.check == "critic")
    assert f"Lead with [{entry.id}] to answer r01." in kept.detail


def test_quiet_runs_stay_quiet(master_cv: Path, tmp_path: Path, scripted: dict[str, Any]) -> None:
    """Narration is opt-in. Without --verbose the stage lines are the whole story."""
    watching = Watching()
    go(master_cv, tmp_path, prompter=watching)
    written = "\n".join(watching.lines)
    assert "-> jobspec" not in written
    assert "[1/4]" in written


def test_force_asks_the_model_again_rather_than_replaying_the_cache(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--force with the cache on must not make a new directory full of old bytes.

    ``--force`` refreshes drafting and critique while extraction and matching
    remain cached.
    """
    sources: dict[str, Any] = {}
    answers = script(sources)
    leaning_on_transferable(answers)
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    # Keep the cache in the test's own directory rather than the machine's.
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.cache.user_cache_dir",
        lambda _name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")

    settings = settings_for(master_cv, tmp_path)
    run(RunCommand(source=posting(tmp_path), settings=settings, cache_enabled=True, force=True))
    first = [name for name, _ in provider.calls]
    assert first, "the first run made no calls at all"

    provider.calls.clear()
    run(RunCommand(source=posting(tmp_path), settings=settings, cache_enabled=True, force=True))
    again = [name for name, _ in provider.calls]

    # Runtime-built schemas include the decorated class name.
    assert any("ResumeDraft" in name for name in again), "--force did not ask for a new draft"
    assert "CriticVerdict" in again, "--force did not ask the critic again"
    assert "JobSpecExtraction" not in again, "extraction should still come from the cache"
    assert "EvidenceMap" not in again, "matching should still come from the cache"


def test_default_settings_reuse_persisted_responses_before_redrawing(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary cache setting must survive a second process-scoped run."""
    sources: dict[str, Any] = {}
    answers = script(sources)
    leaning_on_transferable(answers)
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    # Keep the cache in the test's own directory rather than the machine's.
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.cache.user_cache_dir",
        lambda _name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    settings = settings_for(master_cv, tmp_path)

    run(RunCommand(source=posting(tmp_path), settings=settings, force=True))
    provider.calls.clear()
    run(RunCommand(source=posting(tmp_path), settings=settings, force=True))

    stages = [schema_name for schema_name, _ in provider.calls]
    assert stages
    assert not any("JobSpecExtraction" in name for name in stages)
    assert not any("EvidenceMap" in name for name in stages)
    assert any("ResumeDraft" in name for name in stages)
    assert any("CriticVerdict" in name for name in stages)


STORED = StoredPostingName(title="Platform Product Lead", employer="Example Logistics")


def reading_a_placeholder_name(answers: dict[str, Any]) -> None:
    """Make the scripted extraction answer the way a model misreads a posting's heading."""
    extracted = answers["JobSpecExtraction"]

    def placeholder(schema: Any) -> dict[str, Any]:
        payload: dict[str, Any] = extracted(schema)
        return {**payload, "title": "The Role", "company": "Globex Placeholder"}

    answers["JobSpecExtraction"] = placeholder


def sent_after_extraction(provider: ScriptedProvider) -> dict[str, str]:
    """Return every later stage's request text, keyed by response schema."""
    return {
        name: f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}"
        for name, plan in provider.calls
        if "JobSpecExtraction" not in name
    }


def test_a_stored_title_and_employer_replace_what_the_model_read(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    provider = scripted["provider"]
    reading_a_placeholder_name(provider.script)

    result = go(master_cv, tmp_path, want_cover_letter=True, stored_name=STORED)
    written = written_run(result)

    assert written.document.name == "Example_Logistics_Platform_Product_Lead.md"
    report = written.report.read_text(encoding="utf-8")
    assert report.startswith("# Tailoring report: Platform Product Lead\n")
    assert "- Employer: Example Logistics" in report
    sent = sent_after_extraction(provider)
    for stage in ("EvidenceMap", "ResumeDraft", "CoverLetter"):
        named = [text for key, text in sent.items() if stage in key]
        assert named and all("Platform Product Lead" in text for text in named)
    assert any("Example Logistics" in text for text in sent.values())
    outputs = [report, written.document.read_text(encoding="utf-8"), *sent.values()]
    assert written.cover_letter is not None
    outputs.append(written.cover_letter.read_text(encoding="utf-8"))
    assert not any("The Role" in text or "Globex Placeholder" in text for text in outputs)
    assert "\n\nDear Example Logistics hiring team,\n\n" in outputs[-1]


def test_a_letter_with_no_stored_name_greets_the_hiring_team_and_signs_on_its_own_line(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The command-line path has no stored name, and the model's reading of one never greets."""
    reading_a_placeholder_name(scripted["provider"].script)

    letter = written_run(go(master_cv, tmp_path, want_cover_letter=True)).cover_letter

    assert letter is not None
    text = letter.read_text(encoding="utf-8")
    assert "\n\nDear hiring team,\n\n" in text
    assert "Globex Placeholder" not in text
    assert "\n\nSincerely,\n\n" in text


def test_the_saved_letter_opens_on_the_letterhead_the_critic_never_reads(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The renderer's letter policy needs a name heading; the critic judges only the letter."""
    from clearscout.application.master_cv.cv import parse_cv

    heading = f"# {parse_cv(master_cv).name}"
    result = go(master_cv, tmp_path, want_cover_letter=True)

    assert result.serialized.cover_letter is not None
    saved = result.serialized.cover_letter.decode("utf-8")
    assert saved.startswith(f"{heading}\n\n")
    assert "\n\nDear hiring team,\n\n" in saved
    critic = next(
        f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}"
        for name, plan in scripted["provider"].calls
        if "CriticVerdict" in name and "tailored cover letter " in plan.task.text
    )
    assert "Dear hiring team," in critic
    assert heading not in critic


ADDRESS_ONLY = StoredPostingName(
    title="Platform Product Lead", employer="Northwindhealth", employer_from_address_only=True
)


def test_an_employer_known_only_from_the_board_address_is_named_nowhere_in_the_package(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A title-cased tenant slug never greets, names a file, or reaches a drafting prompt."""
    assert ADDRESS_ONLY.employer_from_address_only
    provider = scripted["provider"]
    reading_a_placeholder_name(provider.script)

    written = written_run(go(master_cv, tmp_path, want_cover_letter=True, stored_name=ADDRESS_ONLY))

    assert written.document.name == "Platform_Product_Lead.md"
    assert written.cover_letter is not None
    letter = written.cover_letter.read_text(encoding="utf-8")
    assert "\n\nDear hiring team,\n\n" in letter
    sent = [
        f"{plan.instructions.text}\n{plan.task.text}\n{plan.cached_prefix}"
        for _, plan in provider.calls
    ]
    assert sent and not any("Northwindhealth" in text for text in sent)
    assert any("the employer" in text for text in sent_after_extraction(provider).values())
    report = written.report.read_text(encoding="utf-8")
    assert (
        '- Employer: The employer name "Northwindhealth" comes only from the posting\'s '
        "address and the posting text never spells it, so this package names no employer."
    ) in report
    for text in (letter, written.document.read_text(encoding="utf-8")):
        assert "Northwindhealth" not in text
        assert "Globex Placeholder" not in text


def test_a_cached_extraction_still_carries_the_stored_name(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = script({})
    reading_a_placeholder_name(answers)
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.cache.user_cache_dir",
        lambda _name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    settings = settings_for(master_cv, tmp_path)
    first = run(RunCommand(source=posting(tmp_path), settings=settings, cache_enabled=True))
    assert written_run(first).document.name == "Globex_Placeholder_The_Role.md"
    provider.calls.clear()

    again = run(
        RunCommand(
            source=posting(tmp_path),
            settings=settings,
            cache_enabled=True,
            force=True,
            stored_name=STORED,
        )
    )

    assert not any("JobSpecExtraction" in name for name, _ in provider.calls)
    written = written_run(again)
    assert written.document.name == "Example_Logistics_Platform_Product_Lead.md"
    report = written.report.read_text(encoding="utf-8")
    assert report.startswith("# Tailoring report: Platform Product Lead\n")
    assert "The Role" not in report


def test_a_returned_run_says_what_it_passed_rather_than_counting_nothing(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A count of nothing is cheerful, true, and useless.

    The reuse path builds a result with no findings, so counting them would
    announce zero checks. The manifest knows what the run did, and the summary
    reads it from there.
    """
    import json

    first = go(master_cv, tmp_path, force=False)
    assert first.verification.findings, "the first run should have checked something"

    second = go(master_cv, tmp_path, force=False)
    assert second.reused, "an unchanged re-run should be returned, not drafted"
    assert not second.verification.findings

    manifest = json.loads((written_run(second).directory / "run.json").read_text(encoding="utf-8"))
    assert manifest["run"]["checks"] == len(first.verification.findings)
    assert any(
        f"passed all {len(first.verification.findings)} checks" in note for note in second.notes
    )


def test_the_since_section_reads_coverage_from_the_manifest_not_from_prose(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """`run.json` is the machine-readable record; a rendered report is not an interface.

    Recovering the comparison from the report's Markdown would make rewording a
    sentence enough to remove the number the "did that help" question needs.
    """
    import json

    first = go(master_cv, tmp_path, force=False)
    manifest_path = written_run(first).directory / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest["run"]["coverage"], float)

    # Change only the manifest so the rendered prose remains unchanged.
    manifest["run"]["coverage"] = manifest["run"]["coverage"] + 40
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = go(master_cv, tmp_path, force=True)
    report = written_run(second).report.read_text(encoding="utf-8")
    assert "Keyword coverage" in report
    assert f"{manifest['run']['coverage']:.0f}% to" in report


def coverage_comparison(master_cv: Path, workspace: Path, coverage: float | None) -> str:
    """Record ``coverage`` on a first run's manifest and return the next run's comparison line.

    The comparison is a sentence of its own; the report's keyword section is a
    heading line, so it never answers for one.
    """
    workspace.mkdir()
    first = go(master_cv, workspace, force=False)
    manifest_path = written_run(first).directory / "run.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if coverage is None:
        del manifest["run"]["coverage"]
    else:
        manifest["run"]["coverage"] = coverage
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = written_run(go(master_cv, workspace, force=True)).report.read_text(encoding="utf-8")
    return next((line for line in report.splitlines() if line.startswith("Keyword coverage")), "")


def test_a_run_written_before_coverage_was_recorded_reports_nothing_rather_than_zero(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Absent is not the same answer as none of the posting's phrases appeared."""
    assert coverage_comparison(master_cv, tmp_path / "unrecorded", None) == ""
    assert coverage_comparison(master_cv, tmp_path / "recorded-zero", 0).startswith(
        "Keyword coverage 0% to"
    )


def test_the_manifest_records_the_model_the_provider_actually_served(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A substitution leaves a trace on every run, verbose or not.

    An alias that starts routing elsewhere changes the price and the output, so
    the served model is recorded in the manifest.
    """
    import json

    result = go(master_cv, tmp_path)
    served = json.loads((written_run(result).directory / "run.json").read_text(encoding="utf-8"))[
        "run"
    ]["served_models"]
    assert served, "no run recorded which model answered it"
    assert set(served) <= {entry.stage for entry in result.ledger.entries}
    assert all(isinstance(name, str) and name for name in served.values())


def test_the_manifest_records_the_effort_each_stage_resolved_to(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A run whose effort is only in per-call rows hides a settings mismatch.

    The manifest lets a reader compare a run's own effort to its configuration
    without querying the ledger table for every stage.
    """
    import json

    result = go(master_cv, tmp_path)
    efforts = json.loads((written_run(result).directory / "run.json").read_text(encoding="utf-8"))[
        "run"
    ]["resolved_efforts"]
    assert efforts, "no run recorded which effort each stage used"
    assert set(efforts) <= {entry.stage for entry in result.ledger.entries}
    assert all(isinstance(level, str) and level for level in efforts.values())
    for entry in result.ledger.entries:
        if entry.effort:
            assert efforts[entry.stage] == entry.effort


def test_a_truncated_manifest_is_an_error_rather_than_a_traceback(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """An empty manifest reuses with a note, and an unreadable one raises ConfigError."""
    from clearscout.application.tailoring.pipeline.reuse import _load_manifest, _reuse
    from clearscout.shared.errors import ConfigError

    first = go(master_cv, tmp_path, force=False)
    directory = written_run(first).directory
    manifest = directory / "run.json"

    assert first.reading is not None
    manifest.write_text("{}", encoding="utf-8")
    assert _load_manifest(directory) == (b"{}", {})
    reused = _reuse(directory, first.spec, first.ledger, 0, first.reading)
    assert any("an earlier run" in note for note in reused.notes)

    manifest.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ConfigError, match="could not be read"):
        _reuse(directory, first.spec, first.ledger, 0, first.reading)


def test_the_run_date_is_one_utc_value_rather_than_three_local_reads(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recency scoring, the report's date, and the manifest must agree.

    `date.today()` is local time and `datetime.now(UTC)` is not, so on either
    side of midnight they disagree. Pinning the pipeline's clock to a date no
    machine is on shows whether the report and the manifest read it or their own.
    """
    import json
    from datetime import UTC, datetime

    pinned = datetime(1999, 12, 31, 23, 30, tzinfo=UTC)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]  # noqa: ARG003
            return pinned

    monkeypatch.setattr("clearscout.application.tailoring.pipeline.service.datetime", Frozen)
    result = go(master_cv, tmp_path)

    written = written_run(result)
    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    report = written.report.read_text(encoding="utf-8")
    assert manifest["posting"]["retrieved"] == "1999-12-31"
    assert manifest["run"]["started"].startswith("1999-12-31")
    assert "- Run date: 1999-12-31" in report


def test_every_artifact_of_a_run_is_owner_only(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A cover letter carries the same CV-derived content the resume does."""
    result = go(master_cv, tmp_path, want_cover_letter=True)
    written = written_run(result)
    assert written.cover_letter is not None
    for path in sorted(written.directory.iterdir()):
        if path.is_file():
            assert path.stat().st_mode & 0o077 == 0, f"{path.name} is readable by others"


def letters(*texts: str) -> Any:
    """Return a letter builder that answers with each text in turn, then repeats.

    A blank line in a text starts another paragraph.
    """
    remaining = list(texts)

    def build(schema: Any) -> dict[str, Any]:
        first = literals(schema, "source_ids")[0]
        text = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return {
            "paragraphs": [
                {"text": paragraph, "source_ids": [first]} for paragraph in text.split("\n\n")
            ]
        }

    return build


def letter_calls(scripted: dict[str, Any]) -> int:
    """Return how many times the letter was drafted."""
    return sum(1 for name, _ in scripted["provider"].calls if "CoverLetter" in name)


def forbidding(*values: str) -> dict[str, Any]:
    """Return a settings update whose only rule is a forbidden string."""
    return {"rules": Guardrails(forbidden=list(values))}


def test_a_letter_that_fails_a_redraftable_check_is_written_again(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """And the second draft is what lands, not the one that failed."""
    scripted["provider"].script["CoverLetter"] = letters(
        "Project Halyard shaped the platform.", "The record fits this posting."
    )
    result = go(
        master_cv,
        tmp_path,
        want_cover_letter=True,
        settings_update=forbidding("Project Halyard"),
    )
    cover_letter = written_run(result).cover_letter
    assert cover_letter is not None
    body = cover_letter.read_text(encoding="utf-8")
    assert "Project Halyard" not in body
    assert "The record fits this posting." in body
    assert letter_calls(scripted) == 2
    assert result.verification.passed


def test_a_cover_letter_receives_bounded_company_research_as_citable_data(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    result = go(
        master_cv,
        tmp_path,
        want_cover_letter=True,
        research=(
            ResearchSource(
                id="company.about",
                kind="about",
                url="https://example.test/about",
                text="The employer builds public-service tools.",
                method="scripted",
            ),
        ),
    )

    assert written_run(result).cover_letter is not None
    _, plan = next(item for item in scripted["provider"].calls if "CoverLetter" in item[0])
    assert "Source ID: company.about" in plan.task.text
    assert "<company_research>" in plan.task.text


def test_a_letter_is_not_redrafted_over_a_finding_no_rewrite_could_clear(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """Sending one back buys the same failure again at full price.

    Driven through the shared loop rather than `check_letter`, because every
    check a letter answers today is redraftable.
    """
    from clearscout.application.tailoring.pipeline.progress import _Progress
    from clearscout.application.tailoring.pipeline.rewrite import _until_clean
    from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report

    attempts = 0

    def produce(feedback: str, attempt: int) -> tuple[str, Report]:
        nonlocal attempts
        attempts += 1
        return "letter", Report(
            findings=(Finding("source characters", Outcome.FAIL, "in the CV", redraftable=False),),
            coverage=KeywordCoverage(present=(), missing=()),
        )

    written, report, used = _until_clean(
        steps=_Progress(None, total=1), label="writing the letter", attempts=3, produce=produce
    )
    assert (written, len(used), attempts) == ("letter", 1, 1)
    assert not report.passed


def test_a_check_that_fails_again_stops_the_loop_though_its_wording_moved() -> None:
    """Rewording a failure is not progress on it, and the budget bought three of them."""
    from clearscout.application.tailoring.pipeline.progress import _Progress
    from clearscout.application.tailoring.pipeline.rewrite import _until_clean
    from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report

    attempts = 0

    def produce(feedback: str, attempt: int) -> tuple[str, Report]:
        nonlocal attempts
        attempts += 1
        return "resume", Report(
            findings=(Finding("evidence support", Outcome.FAIL, f"bullet {attempts}"),),
            coverage=KeywordCoverage(present=(), missing=()),
        )

    written, report, used = _until_clean(
        steps=_Progress(None, total=1), label="drafting", attempts=3, produce=produce
    )

    assert (written, len(used), attempts) == ("resume", 2, 2)
    assert not report.passed


def test_the_same_letter_complaint_twice_stops_before_the_budget(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A rewrite that changed nothing the complaint was about will not on a third try."""
    scripted["provider"].script["CoverLetter"] = letters(
        "Project Halyard shaped the platform.",
        "Project Halyard shaped the platform, again.",
        "The record fits this posting.",
    )
    result = go(
        master_cv,
        tmp_path,
        want_cover_letter=True,
        settings_update=forbidding("Project Halyard"),
    )
    assert letter_calls(scripted) == 2, "the third attempt would repeat the second"
    assert not result.verification.passed


# The master CV's summary, the first citable entry, carries no figure.
INVENTED = "I cut the nightly load by 97 percent."


def kept_letter(result: RunResult) -> tuple[str, int]:
    """Return the written letter's text and the attempt the manifest says was kept."""
    written = written_run(result)
    assert written.cover_letter is not None
    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    return written.cover_letter.read_text(encoding="utf-8"), manifest["run"]["kept"]["cover_letter"]


def test_a_letter_that_regresses_keeps_the_attempt_with_fewer_failures(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """A third draft failing two checks does not replace a second failing only one."""
    scripted["provider"].script["CoverLetter"] = letters(
        f"{INVENTED}\n\nProject Halyard shaped the platform.",
        "Thank you.",
        f"{INVENTED}\n\nProject Halyard shaped the platform, again.",
    )
    result = go(
        master_cv, tmp_path, want_cover_letter=True, settings_update=forbidding("Project Halyard")
    )

    text, kept = kept_letter(result)
    assert letter_calls(scripted) == 3
    assert kept == 2
    assert "Thank you." in text
    assert "Project Halyard" not in text
    failed = {item.check for item in result.verification.failures}
    assert failed == {"letter completeness"}
    assert any("Do not send this as it stands" in warning for warning in result.warnings)


def test_a_letter_repeating_its_failure_keeps_the_earliest_attempt(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    scripted["provider"].script["CoverLetter"] = letters(
        "Project Halyard shaped the platform.",
        "Project Halyard shaped the platform, again.",
    )
    result = go(
        master_cv, tmp_path, want_cover_letter=True, settings_update=forbidding("Project Halyard")
    )

    text, kept = kept_letter(result)
    assert kept == 1
    assert "again" not in text


def test_a_clean_second_letter_is_kept_and_ends_the_loop(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    scripted["provider"].script["CoverLetter"] = letters(
        "Project Halyard shaped the platform.",
        "The record fits this posting.",
        "Project Halyard shaped the platform, again.",
    )
    result = go(
        master_cv, tmp_path, want_cover_letter=True, settings_update=forbidding("Project Halyard")
    )

    text, kept = kept_letter(result)
    assert letter_calls(scripted) == 2
    assert kept == 2
    assert "The record fits this posting." in text
    assert result.verification.passed


@pytest.mark.parametrize(("counts", "kept", "drafted"), [((3, 1, 0), 3, 3), ((2, 2, 0), 1, 2)])
def test_a_repeated_check_continues_only_while_its_offenders_shrink(
    counts: tuple[int, ...], kept: int, drafted: int
) -> None:
    from clearscout.application.tailoring.pipeline.progress import _Progress
    from clearscout.application.tailoring.pipeline.rewrite import _until_clean
    from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report

    def produce(feedback: str, attempt: int) -> tuple[int, Report]:
        count = counts[attempt - 1]
        outcome = Outcome.FAIL if count else Outcome.PASS
        return attempt, Report(
            findings=(Finding("must-have coverage", outcome, f"{count} requirement(s)"),),
            coverage=KeywordCoverage(present=(), missing=()),
            offenders={"must-have coverage": count} if count else {},
        )

    written, report, used = _until_clean(
        steps=_Progress(None, total=1), label="drafting", attempts=3, produce=produce
    )

    assert (written, len(used)) == (kept, drafted)
    assert report.passed is (kept == 3)


def test_a_run_clearing_one_check_only_to_fail_another_stops_at_the_budget() -> None:
    """The attempt budget is the ceiling when every attempt fails something new."""
    from clearscout.application.tailoring.pipeline.progress import _Progress
    from clearscout.application.tailoring.pipeline.rewrite import _until_clean
    from clearscout.application.tailoring.stages.verify import KeywordCoverage, Report

    attempts = 0

    def produce(feedback: str, attempt: int) -> tuple[str, Report]:
        nonlocal attempts
        attempts += 1
        return "resume", Report(
            findings=(Finding(f"check {attempts}", Outcome.FAIL, "unanswered"),),
            coverage=KeywordCoverage(present=(), missing=()),
        )

    written, report, used = _until_clean(
        steps=_Progress(None, total=1), label="drafting", attempts=3, produce=produce
    )

    assert (written, len(used), attempts) == ("resume", 3, 3)
    assert not report.passed


def test_a_letter_that_never_passes_is_still_written_and_the_run_says_so(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    """The package is labeled, not withheld. Deleting it would leave nothing to read."""
    import json

    scripted["provider"].script["CoverLetter"] = letters("Project Halyard shaped the platform.")
    result = go(
        master_cv,
        tmp_path,
        want_cover_letter=True,
        settings_update=forbidding("Project Halyard"),
    )
    written = written_run(result)
    assert written.cover_letter is not None
    assert written.cover_letter.is_file()
    assert written.document.is_file()

    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    assert manifest["run"]["outcome"] == "checks failed"
    assert any("Do not send this as it stands" in warning for warning in result.warnings)


def test_missing_bullet_feedback_repairs_the_next_draft(master_cv, tmp_path, scripted):
    provider = scripted["provider"]
    compliant = provider.script["ResumeDraft"]
    attempts = 0

    def omit_then_repair(schema):
        nonlocal attempts
        attempts += 1
        payload = compliant(schema)
        if attempts == 1:
            first = literals(schema, "source_ids")[0]
            payload["summary"]["source_ids"] = [first]
            payload["roles"] = []
        return payload

    provider.script["ResumeDraft"] = omit_then_repair
    result = go(master_cv, tmp_path)
    requests = [request for name, request in provider.calls if "ResumeDraft" in name]
    assert len(requests) == 2
    assert "selected bullet answer not drafted" in requests[1].task.text
    assert result.verification.passed
    assert result.ledger.total_dollars > 0


def letter_requests(scripted: dict[str, Any]) -> list[Any]:
    """Return every letter request sent so far, the one being answered last."""
    return [plan for name, plan in scripted["provider"].calls if "CoverLetter" in name]


def test_a_letter_retry_edits_the_previous_attempt_rather_than_replacing_it(
    master_cv, tmp_path, scripted
):
    """The fake edits whatever the retry request carries, so kept sentences prove it was sent."""
    kept = "I rebuilt ingestion for the data platform."
    first = [f"{kept} Project Halyard shaped the platform.", "The record fits this posting."]

    def edit(schema: Any) -> dict[str, Any]:
        cited = literals(schema, "source_ids")[0]
        requests = letter_requests(scripted)
        if len(requests) == 1:
            return {"paragraphs": [{"text": text, "source_ids": [cited]} for text in first]}
        block = requests[-1].task.text.split("<previous_letter>")[1].split("</previous_letter>")[0]
        paragraphs = [chunk.split("\n", 1) for chunk in block.strip().split("\n\n")]
        return {
            "paragraphs": [
                {
                    "text": body.replace("Project Halyard shaped the platform.", "It held up."),
                    "source_ids": header.split("source_ids: ", 1)[1].split(", "),
                }
                for header, body in paragraphs
            ]
        }

    scripted["provider"].script["CoverLetter"] = edit
    result = go(
        master_cv, tmp_path, want_cover_letter=True, settings_update=forbidding("Project Halyard")
    )

    requests = letter_requests(scripted)
    assert len(requests) == 2
    assert "Paragraph 1" not in requests[0].task.text
    assert f"{kept} Project Halyard shaped the platform." in requests[1].task.text
    letter, _ = kept_letter(result)
    assert f"{kept} It held up." in letter
    assert "The record fits this posting." in letter
    assert result.verification.passed


def unsupported_by_critic(scripted: dict[str, Any], document: str, claim: str) -> None:
    """Make the critic report ``claim`` as unsupported whenever it reads ``document``."""
    provider = scripted["provider"]
    approving = provider.script["CriticVerdict"]

    def critic(schema: Any) -> dict[str, Any]:
        verdict: dict[str, Any] = approving(schema)
        asked = next(plan for name, plan in reversed(provider.calls) if "CriticVerdict" in name)
        if f"tailored {document}" in asked.task.text:
            verdict["unsupported_claims"] = [{"claim": claim, "boundary": "adjacent"}]
        return verdict

    provider.script["CriticVerdict"] = critic


def test_an_unsupported_claim_the_letter_critic_finds_blocks_the_letter(
    master_cv, tmp_path, scripted
):
    """A retried letter meets the resume's critic gate, and the critic's finding blocks it."""
    provider = scripted["provider"]
    unsupported_by_critic(scripted, "cover letter", "The record fits this posting.")
    provider.script["CoverLetter"] = letters(
        f"Project Halyard shaped the platform.\n\n{INVENTED}", "The record fits this posting."
    )
    result = go(
        master_cv, tmp_path, want_cover_letter=True, settings_update=forbidding("Project Halyard")
    )

    letter_critics = [
        item for item in result.verification.findings if item.check == "letter critic"
    ]
    assert [item.outcome for item in letter_critics] == [Outcome.FAIL]
    assert "The record fits this posting." in letter_critics[0].detail
    assert not result.verification.passed
    written = written_run(result)
    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    recorded = manifest["run"]["attempts"]["cover_letter"]
    assert [item["check"] for item in recorded[-1]["findings"]].count("letter critic") == 1
    report = written.report.read_text(encoding="utf-8")
    critic = report[report.index("## Critic") :]
    assert "### Cover letter" in critic
    assert "- The record fits this posting." in critic
    assert "The critic did not run." not in critic


def test_a_clean_first_letter_is_critiqued_though_the_clean_resume_is_not(
    master_cv, tmp_path, scripted
):
    """The resume keeps its risk gate; every letter that passes its checks is read."""
    result = go(master_cv, tmp_path, want_cover_letter=True)

    asked = [plan for name, plan in scripted["provider"].calls if "CriticVerdict" in name]
    assert asked and all("tailored cover letter" in plan.task.text for plan in asked)
    assert letter_calls(scripted) == 1
    letter_critic = [item for item in result.verification.findings if item.check == "letter critic"]
    assert [item.outcome for item in letter_critic] == [Outcome.PASS]


def test_a_letter_the_critic_judged_unsupported_outranks_an_earlier_one_only_too_long(
    master_cv, tmp_path, scripted
):
    """The earlier letter was never critiqued, so keeping it would hide the critic's FAIL."""
    unsupported_by_critic(scripted, "cover letter", "The record fits this posting.")
    scripted["provider"].script["CoverLetter"] = letters(
        " ".join(["The record fits this posting."] * 72), "The record fits this posting."
    )
    result = go(master_cv, tmp_path, want_cover_letter=True)

    text, kept = kept_letter(result)
    assert kept == 2
    assert text.count("The record fits this posting.") == 1
    assert {item.check for item in result.verification.failures} == {"letter critic"}
    assert any("Do not send this as it stands" in warning for warning in result.warnings)


def test_a_resume_the_critic_judged_unsupported_outranks_an_earlier_uncritiqued_one(
    master_cv, tmp_path, scripted
):
    provider = scripted["provider"]
    compliant = provider.script["ResumeDraft"]
    drafted = 0

    def forbidden_then_compliant(schema: Any) -> dict[str, Any]:
        nonlocal drafted
        drafted += 1
        payload: dict[str, Any] = compliant(schema)
        if drafted == 1:
            payload["summary"]["text"] += " Project Halyard shaped the platform."
        return payload

    provider.script["ResumeDraft"] = forbidden_then_compliant
    unsupported_by_critic(scripted, "resume", "Led the platform team.")
    result = go(master_cv, tmp_path, settings_update=forbidding("Project Halyard"))

    written = written_run(result)
    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    assert manifest["run"]["kept"] == {"resume": 2}
    assert "Project Halyard" not in written.document.read_text(encoding="utf-8")
    assert {item.check for item in result.verification.failures} == {"critic"}
    assert not result.verification.passed


def test_a_letter_echo_is_reported_but_never_sends_the_letter_back(master_cv, tmp_path, scripted):
    echo = (
        "Across eleven years the work moved from analytics engineering to platform "
        "architecture, which suits this role."
    )
    scripted["provider"].script["CoverLetter"] = letters(echo, "The record fits this posting.")
    result = go(master_cv, tmp_path, want_cover_letter=True)

    written = written_run(result)
    manifest = json.loads((written.directory / "run.json").read_text(encoding="utf-8"))
    assert letter_calls(scripted) == 1
    assert result.verification.passed
    assert not any("Do not send" in warning for warning in result.warnings)
    echoed = [item for item in manifest["run"]["findings"] if item["check"] == "letter echo"]
    assert [item["outcome"] for item in echoed] == ["warn"]
    assert "copies 'Across eleven years the work moved from analytics" in echoed[0]["detail"]


@pytest.mark.parametrize("attempt_limit", [1, 3])
def test_ignored_must_have_feedback_retains_a_failed_billed_package(
    master_cv, tmp_path, scripted, attempt_limit
):
    provider = scripted["provider"]
    compliant = provider.script["ResumeDraft"]

    def ignore_feedback(schema):
        payload = compliant(schema)
        payload["summary"]["source_ids"] = [literals(schema, "source_ids")[0]]
        payload["roles"] = []
        return payload

    provider.script["ResumeDraft"] = ignore_feedback
    result = go(master_cv, tmp_path, settings_update={"max_draft_attempts": attempt_limit})
    assert not result.verification.passed
    assert "must-have coverage" in result.verification.redraftable_checks()
    assert len([name for name, _ in provider.calls if "ResumeDraft" in name]) == min(
        2, attempt_limit
    )
    assert result.ledger.total_dollars > 0
    assert written_run(result).document.is_file()


@pytest.mark.parametrize("body", ["", "   ", "Sincerely, Jane Example"])
def test_an_empty_letter_stays_failed_when_rewrites_are_exhausted(
    master_cv, tmp_path, scripted, body
):
    scripted["provider"].script["CoverLetter"] = letters(body)
    result = go(master_cv, tmp_path, want_cover_letter=True)
    assert letter_calls(scripted) == 2
    assert not result.verification.passed
    assert any(item.check == "letter completeness" for item in result.verification.failures)
    assert result.ledger.total_dollars > 0
    assert written_run(result).cover_letter is not None


@pytest.mark.parametrize(
    ("family", "fragments"),
    [
        (
            "Transit Reliability Lead",
            ("Built the telemetry replay", "Authored the only railway", "Supervised two workshop"),
        ),
        (
            "Instrumentation Program Lead",
            ("Designed a calibration protocol", "Coordinated replacement", "Mentored engineers"),
        ),
        (
            "Public Infrastructure Delivery Lead",
            (
                "Wrote the acceptance plan",
                "Negotiated a phased supplier",
                "Authored the only railway",
            ),
        ),
    ],
)
def test_a_long_hybrid_career_reaches_a_reviewable_package_across_job_families(
    family, fragments, tmp_path, monkeypatch
):
    from clearscout.application.master_cv import parse_canonical_source
    from clearscout.application.master_cv.cv import EntryKind

    fixture = Path(__file__).parents[2] / "evals" / "cvs" / "hybrid_career.md"
    inventory = parse_canonical_source(fixture, fixture.read_bytes())
    assert inventory is not None
    assert len(inventory.roles) == 5
    selected = [
        next(entry for entry in inventory.entries if entry.text.startswith(fragment))
        for fragment in fragments
    ]
    summary = inventory.of_kind(EntryKind.SUMMARY)[0]
    answers = script({})
    answers["JobSpecExtraction"] = lambda _: {
        "title": family,
        "company": "Example Civic Systems",
        "requirements": [{"text": entry.text, "importance": "must_have"} for entry in selected],
        "keywords": [],
        "hard_gates": [],
    }
    answers["EvidenceMap"] = lambda _: {
        "matches": [
            {
                "requirement_id": f"r{index:02}",
                "entry_ids": [entry.id],
                "classification": "DIRECT",
                "strength": "strong",
            }
            for index, entry in enumerate(selected, 1)
        ]
    }
    answers["ResumeDraft"] = lambda _: {
        "summary": {"text": summary.text, "source_ids": [summary.id]},
        "roles": [
            {
                "role_key": role.key,
                "bullets": [
                    {"text": entry.text, "source_ids": [entry.id]}
                    for entry in selected
                    if entry.role_key == role.key
                ],
            }
            for role in inventory.roles
            if any(entry.role_key == role.key for entry in selected)
        ],
    }
    answers["CoverLetter"] = lambda _: {
        "paragraphs": [
            {"text": " ".join(entry.text.split()[:6]), "source_ids": [entry.id]}
            for entry in selected[:2]
        ]
    }
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    result = go(
        fixture,
        tmp_path,
        want_cover_letter=True,
        settings_update={"rules": Guardrails(), "full_bullet_years": 5},
    )
    assert result.verification.passed
    materialized = written_run(result)
    assert materialized.cover_letter is not None
    document = materialized.document.read_text()
    for entry in selected:
        assert entry.text in document
    assert materialized.review is not None
    review = materialized.review.read_text()
    assert all(entry.id in review for entry in selected)
    assert any(
        item.check == "factual review" and item.outcome is Outcome.WARN
        for item in result.verification.findings
    )
    assert any("not measured" in note for note in result.notes)


def test_the_vision_transcript_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field this schema never declared is refused, not dropped."""
    from pydantic import ValidationError

    from clearscout.application.tailoring.pipeline.models import VisionTranscript

    with pytest.raises(ValidationError):
        VisionTranscript.model_validate({"text": "posting text", "extra_field": "x"})


def test_retained_posting_export_names_publisher_and_effective_settings(
    master_cv, tmp_path, scripted
):
    result = go(master_cv, tmp_path, publisher_url="https://jobs.example/roles/platform")
    materialized = written_run(result)
    report = materialized.report.read_text()
    manifest = json.loads((materialized.directory / "run.json").read_text())
    assert manifest["posting"]["url"] == "https://jobs.example/roles/platform"
    assert "https://jobs.example/roles/platform" in report
    assert manifest["inputs"]["settings_hash"] in report
    assert manifest["inputs"]["code_revision"] in report
    assert manifest["inputs"]["code_revision"] != manifest["inputs"]["clearscout"]


def test_each_letter_paragraph_keeps_its_citations_in_the_review_and_the_report(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    from clearscout.application.master_cv.cv import parse_cv

    answered: list[dict[str, Any]] = []
    drafted = scripted["provider"].script["CoverLetter"]

    def recording(schema: Any) -> dict[str, Any]:
        payload: dict[str, Any] = drafted(schema)
        answered.append(payload)
        return payload

    scripted["provider"].script["CoverLetter"] = recording
    result = go(master_cv, tmp_path, want_cover_letter=True)
    written = written_run(result)

    cited = [list(paragraph["source_ids"]) for paragraph in answered[-1]["paragraphs"]]
    assert cited and all(cited)
    review = json.loads(written.review.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    assert [item["source_ids"] for item in review["letter"]["paragraphs"]] == cited
    assert [item["paragraph"] for item in review["letter"]["paragraphs"]] == list(
        range(1, len(cited) + 1)
    )
    report = written.report.read_text(encoding="utf-8")
    section = report[report.index("## Letter provenance") :]
    source = parse_cv(master_cv).by_id(cited[0][0]).text
    assert f"| 1 | {cited[0][0]} |" in section
    assert source[:40] in section


def failing_the_first_draft(provider: ScriptedProvider) -> None:
    """Answer the first resume draft without its roles, then comply."""
    compliant = provider.script["ResumeDraft"]
    drafts = 0

    def omit_then_repair(schema: Any) -> dict[str, Any]:
        nonlocal drafts
        drafts += 1
        payload: dict[str, Any] = compliant(schema)
        if drafts == 1:
            payload["summary"]["source_ids"] = [literals(schema, "source_ids")[0]]
            payload["roles"] = []
        return payload

    provider.script["ResumeDraft"] = omit_then_repair


def test_the_manifest_records_every_attempt_and_the_feedback_that_followed_it(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any]
) -> None:
    from clearscout.infrastructure.providers.plan import digest_of

    provider = scripted["provider"]
    failing_the_first_draft(provider)
    result = go(master_cv, tmp_path)

    manifest = json.loads((written_run(result).directory / "run.json").read_text("utf-8"))
    first, second = manifest["run"]["attempts"]["resume"]
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert manifest["run"]["kept"] == {"resume": 2}
    failed = [item for item in first["findings"] if item["outcome"] == "fail"]
    assert "must-have coverage" in {item["check"] for item in failed}
    feedback = "\n".join(f"- {item['check']}: {item['detail']}" for item in failed)
    redraft = next(plan for name, plan in reversed(provider.calls) if "ResumeDraft" in name)
    assert feedback in redraft.task.text
    assert first["feedback_digest"] == digest_of(feedback)
    assert feedback not in json.dumps(manifest)
    assert second["feedback_digest"] == ""
    assert all(item["outcome"] != "fail" for item in second["findings"])
    assert "The draft was rewritten 1 time(s) in response to checks." in manifest["run"]["notes"]


def test_the_manifest_names_the_build_and_time_behind_each_cached_answer(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    provider = ScriptedProvider(script({}))
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.providers.cache.user_cache_dir",
        lambda _name: str(tmp_path / "cache"),
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    settings = settings_for(master_cv, tmp_path)
    first = run(RunCommand(source=posting(tmp_path), settings=settings, force=True))
    assert first.serialized.manifest["run"]["cache_hits"] == []
    database = sqlite3.connect(tmp_path / "cache" / "responses" / "responses.sqlite3")
    with database:
        database.execute(
            "UPDATE responses SET revision = 'rev-synthetic-dirty', "
            "package_version = '0.0.1', updated_at = '2026-01-02 03:04:05'"
        )
    database.close()

    again = run(RunCommand(source=posting(tmp_path), settings=settings, force=True))

    hits = {item["stage"]: item for item in again.serialized.manifest["run"]["cache_hits"]}
    assert {"jobspec", "match"} <= set(hits)
    assert "draft" not in hits
    for hit in (hits["jobspec"], hits["match"]):
        assert hit["code_revision"] == "rev-synthetic-dirty"
        assert hit["package_version"] == "0.0.1"
        assert hit["stored_at"] == "2026-01-02 03:04:05"
        assert len(hit["cache_key"]) == 32


@pytest.mark.parametrize(
    ("file_name", "named"),
    [
        ("jane_doe_master.md", "version cvv-synthetic-0001, jane_doe_master.md ("),
        ("", "version cvv-synthetic-0001 ("),
    ],
)
def test_the_report_names_a_stored_master_cv_by_version_rather_than_by_path(
    master_cv: Path, tmp_path: Path, scripted: dict[str, Any], file_name: str, named: str
) -> None:
    from clearscout.application.tailoring.pipeline import StoredMasterCv

    stored = StoredMasterCv(version_id="cvv-synthetic-0001", file_name=file_name)
    result = go(master_cv, tmp_path, stored_cv=stored)

    report = written_run(result).report.read_text(encoding="utf-8")
    header = next(line for line in report.splitlines() if line.startswith("- Master CV:"))
    assert header.startswith(f"- Master CV: {named}")
    assert str(master_cv) not in report


def test_the_page_record_costs_a_bullet_with_its_details_as_selection_does(
    settings, guardrails
) -> None:
    from clearscout.application.tailoring.stages.draft import (
        DraftedLine,
        DraftedRole,
        ResumeDraft,
        fit,
    )
    from clearscout.application.tailoring.stages.match import EvidenceMap
    from clearscout.application.tailoring.stages.select import build_plan
    from tests.support.resumes import cited, detailed_record, must_haves

    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]
    evidence = EvidenceMap(matches=(cited("r01", [parent.id]),))
    plan = build_plan(record, must_haves(1), evidence, settings)
    draft = ResumeDraft(
        summary=DraftedLine(text="Data engineer.", source_ids=plan.summary_ids),
        roles=(
            DraftedRole(
                role_key="example-org",
                bullets=(DraftedLine(text="Led the mapping.", source_ids=(parent.id,)),),
            ),
        ),
    )

    selection = PageSelection.of(
        plan=plan,
        inventory=record,
        spec=must_haves(1),
        evidence=evidence,
        fitted=fit(draft, record, plan, settings, guardrails),
        settings=settings,
    )

    assert selection.roles[0].bullet_lines == plan.spent_lines == 2
