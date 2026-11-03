"""The tailoring report a person reads names evidence and requirements by their words."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind, parse_cv
from clearscout.application.tailoring.pipeline import RunCommand, run
from clearscout.application.tailoring.stages.draft import DraftedLine, DraftedRole, ResumeDraft
from clearscout.application.tailoring.stages.jobspec import (
    HardGate,
    Importance,
    JobSpec,
    Requirement,
)
from clearscout.application.tailoring.stages.match import EvidenceMap
from clearscout.application.tailoring.stages.select import Plan, build_plan
from clearscout.application.tailoring.stages.verify import (
    CRITIC_CHECK,
    LETTER_CRITIC,
    CriticVerdict,
    KeywordCoverage,
    Report,
)
from tests.support.doubles import ScriptedProvider
from tests.support.payloads import script
from tests.support.reports import rendered_report, report_section
from tests.support.resumes import (
    bullet_ids,
    cited,
    drafted_to_length,
    long_record,
    must_haves,
    sentence,
)
from tests.support.spans import found
from tests.support.suite import POSTING, SUITE_KEY_VARIABLE, settings_for

BULLET = "Cut nightly batch latency from four hours to forty minutes."
CONTEXT = "Platform group serving twelve analytics teams."
REQUIREMENT = "Operate streaming ingestion at production scale"
# The bullet runs past the pointer length, so the report quotes its opening words.
POINTER = "Cut nightly batch latency from four hours to ..."
EXAMPLE_ASK = "Operate streaming ingestion, such as Kafka or Kinesis"


def _inventory() -> CvInventory:
    return CvInventory(
        source=Path("master_cv.md"),
        content_hash="cv-hash",
        name="Ada Lorne",
        entries=(
            Entry(id="b-1a2b3c4d", kind=EntryKind.BULLET, section="Experience", text=BULLET),
            Entry(
                id="ctx-9f8e7d6c", kind=EntryKind.ROLE_CONTEXT, section="Experience", text=CONTEXT
            ),
        ),
    )


def _spec() -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(Requirement(id="r04", text=REQUIREMENT, importance=Importance.MUST_HAVE),),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


@pytest.mark.parametrize("check", [CRITIC_CHECK, LETTER_CRITIC])
def test_critic_prose_quotes_the_entry_and_requirement_an_id_names(check: str) -> None:
    """The redraft needs the ids; the person reading the report needs the words."""
    verdict = CriticVerdict(
        alignment=2,
        preservation=5,
        specificity=3,
        readability=4,
        ats_clarity=4,
        human_voice=4,
        weaknesses=["[b-1a2b3c4d] is underused against r04."],
        style_flags=["ctx-9f8e7d6c repeats the heading."],
        guidance="Lead with [b-1a2b3c4d] and keep ctx-9f8e7d6c; b-00000000 is not in this run.",
    )
    verification = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))

    report = rendered_report(
        verification=verification.with_critic(verdict, check), spec=_spec(), inventory=_inventory()
    )
    critic = report_section(report, "Critic")
    checks = report_section(report, "Checks")
    readiness = report_section(report, "Application readiness")

    assert f'- "{POINTER}" is underused against "{REQUIREMENT}".' in critic
    assert f'Lead with "{POINTER}" and keep "{CONTEXT}"; b-00000000 is not in this run.' in critic
    assert f'Lead with "{POINTER}"' in checks
    assert f'Lead with "{POINTER}" and keep "{CONTEXT}"' in readiness
    assert f'- "{CONTEXT}" repeats the heading.' in critic
    for known in ("b-1a2b3c4d", "ctx-9f8e7d6c", "r04"):
        for section in (critic, checks, readiness):
            assert known not in section


def test_a_passing_critic_read_lists_its_advisories_under_it_in_the_readiness_block() -> None:
    """A read that clears every floor still says what the critic would change."""
    verdict = CriticVerdict(
        alignment=4,
        preservation=5,
        specificity=4,
        readability=4,
        ats_clarity=4,
        human_voice=4,
        weaknesses=["[b-1a2b3c4d] is buried under r04."],
    )
    verification = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))

    report = rendered_report(
        verification=verification.with_critic(verdict), spec=_spec(), inventory=_inventory()
    )
    readiness = report_section(report, "Application readiness")

    assert "- PASS critic: alignment 4/5" in readiness
    assert f'\n  - "{POINTER}" is buried under "{REQUIREMENT}".' in readiness


def test_an_uncritiqued_resume_says_why_the_critic_left_it_unread() -> None:
    reason = "A reviewer supplied replacement text, and the critic did not read it."
    verification = Report(
        findings=(), coverage=KeywordCoverage(present=(), missing=())
    ).without_critic(reason)

    report = rendered_report(verification=verification, spec=_spec(), inventory=_inventory())
    critic = report_section(report, "Critic")

    assert reason in critic
    assert "The critic did not run." not in critic


def test_an_unread_resume_keeps_the_letter_critic_scores_out_of_its_section() -> None:
    """A resume that failed its checks is merged with a letter the critic did read."""
    letter = Report(findings=(), coverage=KeywordCoverage(present=(), missing=())).with_critic(
        CriticVerdict(
            alignment=4, preservation=5, specificity=4, readability=4, ats_clarity=4, human_voice=4
        ),
        LETTER_CRITIC,
    )
    merged = Report(findings=letter.findings, coverage=KeywordCoverage(present=(), missing=()))

    report = rendered_report(verification=merged, spec=_spec(), inventory=_inventory())
    critic = report_section(report, "Critic")

    assert "The critic did not read the resume." in critic
    assert "alignment 4/5" not in critic


def _fitted_long_record(tmp_path: Path, settings: Settings) -> tuple[CvInventory, Plan, str]:
    """Return a record too long for its pages, its plan, and the report of a fitted draft."""
    record = long_record(tmp_path)
    spec = must_haves(1)
    plan = build_plan(
        record, spec, EvidenceMap(matches=(cited("r01", bullet_ids(record)),)), settings
    )
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    return record, plan, report_section(report, "Length")


def test_the_length_section_lists_each_trimmed_entry_by_its_words(
    tmp_path: Path, settings: Settings
) -> None:
    record, plan, length = _fitted_long_record(tmp_path, settings)
    first = plan.drop_order[0]

    assert "trimmed in drop order after drafting to fit the page" in length
    assert f"| 1 | {first} | {record.by_id(first).text[:60]}" in length
    assert re.search(r"with page 2 about \d+% full", length), length


def test_provenance_lists_only_the_lines_the_page_prints(
    tmp_path: Path, settings: Settings
) -> None:
    """A trimmed bullet is off the page, so it is not a line to defend in an interview."""
    record = long_record(tmp_path)
    spec = must_haves(1)
    plan = build_plan(
        record, spec, EvidenceMap(matches=(cited("r01", bullet_ids(record)),)), settings
    )
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    provenance = report_section(report, "Provenance")
    kept = plan.drop_order[-1]

    assert f"| {plan.drop_order[0]} |" not in provenance
    assert f"| {kept} |" in provenance


def test_the_length_section_names_a_role_printed_past_the_cap(
    tmp_path: Path, settings: Settings
) -> None:
    """Every bullet of every role is cited, so each fills to the cap and the page has room left."""
    record, plan, length = _fitted_long_record(tmp_path, settings)
    past = [
        role
        for role in record.roles
        if len(plan.role_bullets.get(role.key, ())) > settings.max_bullets_per_role
    ]

    assert past, "the fixture no longer passes the cap"
    assert f"{past[0].organization} ({past[0].title}, {past[0].dates}) prints " in length
    assert f"past max_bullets_per_role = {settings.max_bullets_per_role}" in length
    assert "clearscout.toml" not in length
    assert "below the cap" not in length, "no role sits below the cap, so none can explain it"


def test_roles_past_the_cap_say_once_why_a_role_below_it_did_not_take_the_room(
    tmp_path: Path, settings: Settings
) -> None:
    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    spec = must_haves(1)
    evidence = EvidenceMap(matches=(cited("r01", bullets[:60] + bullets[60:63]),))
    plan = build_plan(record, spec, evidence, settings)
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    length = report_section(report, "Length")

    assert length.count(f"past max_bullets_per_role = {settings.max_bullets_per_role}") == 2
    assert length.count("Every role below the cap had already placed all its cited bullets.") == 1


def _one_employer_for(tmp_path: Path, heading: str, employer: str) -> CvInventory:
    """Return the long record with the role under ``heading`` moved to ``employer``."""
    source = long_record(tmp_path).source
    source.write_text(
        source.read_text(encoding="utf-8").replace(heading, employer), encoding="utf-8"
    )
    return parse_cv(source)


def test_two_roles_past_the_cap_at_one_employer_are_told_apart(
    tmp_path: Path, settings: Settings
) -> None:
    record = _one_employer_for(tmp_path, "### Example Org 2", "### Example Org 1")
    bullets = bullet_ids(record)
    spec = must_haves(1)
    evidence = EvidenceMap(matches=(cited("r01", bullets[:60] + bullets[60:63]),))
    plan = build_plan(record, spec, evidence, settings)
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    length = report_section(report, "Length")
    past = record.roles[:2]

    assert [role.organization for role in past] == ["Example Org 1", "Example Org 1"]
    for role in past:
        assert f"{role.organization} ({role.title}, {role.dates}) prints " in length


def test_a_resume_left_over_its_pages_says_by_how_much(tmp_path: Path, settings: Settings) -> None:
    """A summary no drop can shorten leaves the page long after every allowed drop."""
    record = long_record(tmp_path)
    spec = must_haves(1)
    plan = build_plan(
        record, spec, EvidenceMap(matches=(cited("r01", bullet_ids(record)),)), settings
    )
    written = drafted_to_length(record, plan, 0.8)
    summary = written.summary.model_copy(update={"text": sentence(110 * 150, 0)})
    draft = written.model_copy(update={"summary": summary})
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    length = report_section(report, "Length")

    assert re.search(r"estimated about \d+ line\(s\) past 2 page\(s\)", length), length
    assert "Nothing further can be dropped" in length
    assert "--trim" not in length


def test_a_short_page_names_the_recent_roles_it_prints_without_bullets(
    inventory: CvInventory, settings: Settings
) -> None:
    """Only the master CV can give those roles bullets, so the note points there."""
    kafka = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion"))
    spec = must_haves(1)
    plan = build_plan(inventory, spec, EvidenceMap(matches=(cited("r01", [kafka.id]),)), settings)
    bare = [
        role.organization
        for role in inventory.roles
        if role.key in plan.recent_roles and not plan.role_bullets.get(role.key)
    ]
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=plan.summary_ids[:1]),
        roles=(
            DraftedRole(
                role_key=str(kafka.role_key),
                bullets=(DraftedLine(text="Rebuilt ingestion on Kafka.", source_ids=(kafka.id,)),),
            ),
        ),
    )
    report = rendered_report(
        spec=spec, plan=plan, draft=draft, inventory=inventory, settings=settings
    )
    length = report_section(report, "Length")

    assert len(bare) > 1, "the fixture must leave several recent roles bare"
    assert all(organization in length for organization in bare)
    assert " print without bullets" in length
    assert "adding it gives selection more to choose from" in length


def test_an_earlier_career_line_with_no_earlier_role_to_print_under_is_reported(
    inventory: CvInventory, settings: Settings
) -> None:
    """Every fixture role is recent, so the page has nowhere to put an earlier-career line."""
    kafka = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion"))
    spec = must_haves(1)
    plan = build_plan(inventory, spec, EvidenceMap(matches=(cited("r01", [kafka.id]),)), settings)
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=plan.summary_ids[:1]),
        roles=(
            DraftedRole(
                role_key=str(kafka.role_key),
                bullets=(DraftedLine(text="Rebuilt ingestion on Kafka.", source_ids=(kafka.id,)),),
            ),
        ),
        earlier_career=DraftedLine(
            text="Began in analytics engineering for retail clients.", source_ids=(kafka.id,)
        ),
    )
    report = rendered_report(
        spec=spec, plan=plan, draft=draft, inventory=inventory, settings=settings
    )
    length = report_section(report, "Length")

    assert not plan.earlier_roles
    assert "earlier-career line" in length
    assert "Began in analytics engineering for retail clients." in length


def test_a_full_page_names_the_recent_role_it_prints_without_bullets(
    tmp_path: Path, settings: Settings
) -> None:
    """The page ran out, not the evidence, and still only the master CV can give that role any."""
    record = long_record(tmp_path)
    bare = record.roles[2]
    one_page = settings.model_copy(update={"pages": 1})
    spec = must_haves(1)
    elsewhere = [
        entry.id for entry in record.of_kind(EntryKind.BULLET) if entry.role_key != bare.key
    ]
    plan = build_plan(record, spec, EvidenceMap(matches=(cited("r01", elsewhere),)), one_page)
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=one_page)
    length = report_section(report, "Length")

    assert "short of" not in length
    assert (
        f"{bare.organization} ({bare.title}, {bare.dates}) prints without bullets. "
        "If it holds relevant work"
    ) in length


def test_two_bare_roles_at_one_employer_are_told_apart(tmp_path: Path, settings: Settings) -> None:
    record = _one_employer_for(tmp_path, "### Example Org 3", "### Example Org 2")
    first = record.roles[0]
    spec = must_haves(1)
    cited_ids = [
        entry.id for entry in record.of_kind(EntryKind.BULLET) if entry.role_key == first.key
    ]
    plan = build_plan(record, spec, EvidenceMap(matches=(cited("r01", cited_ids),)), settings)
    draft = drafted_to_length(record, plan, 0.8)
    report = rendered_report(spec=spec, plan=plan, draft=draft, inventory=record, settings=settings)
    length = report_section(report, "Length")
    bare = [role for role in record.roles if role.key in plan.recent_roles][1:]

    assert [role.organization for role in bare] == ["Example Org 2", "Example Org 2"]
    assert "; ".join(f"{role.organization} ({role.title}, {role.dates})" for role in bare) in length


@pytest.mark.parametrize(
    ("cap", "cited_per_role", "share", "held_back"),
    [
        # Selection left room for a held bullet, but the fit trimmed drafted lines off the page.
        (12, (30, 6, 3), 0.8, False),
        # Every bullet cited fills the page, so nothing the cap kept off would fit.
        (8, (30, 30, 30), 0.8, False),
        # Drafted at the share selection assumes, the fit trims nothing and a held bullet fits.
        (12, (30, 8, 2), 0.65, True),
        # The fit trims nothing, but the page has under a line left for the held bullet.
        (13, (1, 30, 8), 0.65, False),
    ],
)
def test_the_cap_is_named_only_for_bullets_the_page_would_hold(
    tmp_path: Path,
    settings: Settings,
    cap: int,
    cited_per_role: tuple[int, ...],
    share: float,
    held_back: bool,
) -> None:
    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    one_page = settings.model_copy(update={"pages": 1, "max_bullets_per_role": cap})
    spec = must_haves(1)
    ids = [
        entry for role, count in enumerate(cited_per_role) for entry in bullets[30 * role :][:count]
    ]
    plan = build_plan(record, spec, EvidenceMap(matches=(cited("r01", ids),)), one_page)
    report = rendered_report(
        spec=spec,
        plan=plan,
        draft=drafted_to_length(record, plan, share),
        inventory=record,
        settings=one_page,
    )

    assert ("raise it in clearscout.toml" in report_section(report, "Length")) is held_back


SQL_REQUIREMENT = "Proficiency in SQL and dbt"


@pytest.mark.parametrize(
    ("importance", "cited_text", "advised"),
    [
        pytest.param(Importance.MUST_HAVE, BULLET, True, id="must-have"),
        pytest.param(
            Importance.MUST_HAVE, "Wrote the SQL and dbt models behind billing.", False, id="named"
        ),
        pytest.param(Importance.NICE_TO_HAVE, BULLET, True, id="nice-to-have"),
    ],
)
def test_a_named_tool_no_cited_entry_names_is_advised_in_the_report_and_run_notes(
    importance: Importance, cited_text: str, advised: bool
) -> None:
    from clearscout.application.tailoring.pipeline.report import named_tool_notes
    from clearscout.application.tailoring.stages.match import Classification, Match, Strength

    inventory = CvInventory(
        source=Path("master_cv.md"),
        content_hash="cv-hash",
        name="Ada Lorne",
        entries=(Entry(id="b-1", kind=EntryKind.BULLET, section="Experience", text=cited_text),),
    )
    spec = _spec().model_copy(
        update={
            "requirements": (Requirement(id="r01", text=SQL_REQUIREMENT, importance=importance),)
        }
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=("b-1",),
                classification=Classification.TRANSFERABLE,
                strength=Strength.MODERATE,
                named_tools=("SQL", "dbt"),
            ),
        )
    )

    notes = named_tool_notes(spec, evidence, inventory)
    section = report_section(
        rendered_report(spec=spec, evidence=evidence, inventory=inventory), "Named tools"
    )

    expected = f'No evidence cited for "{SQL_REQUIREMENT}" names SQL, dbt.'
    assert (expected in section) is advised
    assert any(expected in note for note in notes) is advised
    assert bool(section) is advised


def test_the_gates_headline_counts_only_conditions_the_record_must_answer() -> None:
    """The search parameters settle a location gate, so it is not left for the person."""
    gates = (
        HardGate(kind="location", text="US or Canada - Remote", blocking=True, source_span=found()),
        HardGate(
            kind="minimum_years",
            text="5+ years of data engineering experience",
            blocking=True,
            source_span=found(),
        ),
    )
    spec = _spec().model_copy(update={"hard_gates": gates})

    section = report_section(rendered_report(spec=spec, inventory=_inventory()), "Hard gates")

    assert "**1 absolute condition(s) are not answered by direct evidence.**" in section
    assert "| US or Canada - Remote | yes | verified | your search parameters |" in section
    assert "| 5+ years of data engineering experience | yes | verified | not checked |" in section


def test_a_dropped_named_tool_is_recorded_in_the_report() -> None:
    evidence = EvidenceMap(dropped_tools=(("r04", "Snowflake"),))

    section = report_section(rendered_report(spec=_spec(), evidence=evidence), "Named tools")

    assert f'Snowflake, listed for "{REQUIREMENT}"' in section


@pytest.mark.parametrize(
    ("note", "advised"),
    [
        pytest.param("Streaming work transfers; Kafka is not established.", True, id="example"),
        pytest.param("Batch work transfers; streaming is not established.", False, id="ask"),
    ],
)
def test_a_note_calling_a_listed_example_missing_is_advised_in_the_report(
    note: str, advised: bool
) -> None:
    from clearscout.application.tailoring.stages.match import Classification, Match, Strength

    text = EXAMPLE_ASK
    spec = _spec().model_copy(
        update={
            "requirements": (Requirement(id="r04", text=text, importance=Importance.MUST_HAVE),)
        }
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r04",
                entry_ids=("b-1a2b3c4d",),
                classification=Classification.TRANSFERABLE,
                strength=Strength.MODERATE,
                note=note,
            ),
        )
    )

    section = report_section(rendered_report(spec=spec, evidence=evidence), "Example-list notes")

    expected = f'The note for "{text}" says "Kafka is not established"'
    assert (expected in section) is advised
    assert bool(section) is advised


def test_a_run_whose_note_calls_a_listed_example_missing_records_it_in_the_run_notes(
    master_cv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = script({})
    extract, answer = answers["JobSpecExtraction"], answers["EvidenceMap"]

    def asks_by_example(schema: object) -> dict[str, object]:
        spec: dict[str, Any] = extract(schema)
        spec["requirements"][0] = {"text": EXAMPLE_ASK, "importance": "must_have"}
        return spec

    def holds_an_example_against_the_record(schema: object) -> dict[str, object]:
        mapped: dict[str, Any] = answer(schema)
        mapped["matches"][0]["note"] = "Streaming work transfers; Kafka is not established."
        return mapped

    answers["JobSpecExtraction"] = asks_by_example
    answers["EvidenceMap"] = holds_an_example_against_the_record
    provider = ScriptedProvider(answers)
    monkeypatch.setattr(
        "clearscout.application.tailoring.pipeline.service.provider_for",
        lambda name, key, timeout=None: provider,
    )
    monkeypatch.setenv(SUITE_KEY_VARIABLE, "test-key")
    source = tmp_path / "posting.txt"
    source.write_text(POSTING, encoding="utf-8")

    result = run(
        RunCommand(
            source=str(source),
            settings=settings_for(master_cv, tmp_path, 2),
            cache_enabled=False,
            force=True,
            materialize=False,
        )
    )

    expected = f'The note for "{EXAMPLE_ASK}" says "Kafka is not established"'
    assert any(expected in note for note in result.serialized.manifest["run"]["notes"])
