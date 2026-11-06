"""The programmatic checks, one failure mode at a time."""

from __future__ import annotations

import json
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from clearscout.application.config import CriticStatistic
from clearscout.application.master_cv.cv import CvInventory, EntryKind
from clearscout.application.prompts import data_block
from clearscout.application.tailoring.fit.lexicon import HEADING_WORDS
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.stages.context import entry_line, record_block
from clearscout.application.tailoring.stages.draft import (
    CoverLetter,
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
    SourceType,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import EvidenceMap
from clearscout.application.tailoring.stages.select import build_plan
from clearscout.application.tailoring.stages.verify import (
    LETTER_CRITIC,
    CriticVerdict,
    DoubtedClaim,
    KeywordCoverage,
    Report,
    check,
    critic_finding,
    critique,
    id_texts,
    keyword_coverage,
    line_citations,
)
from clearscout.domain import ClaimBoundary, ResearchKind
from clearscout.infrastructure.providers.base import json_schema
from clearscout.infrastructure.providers.cache import ResponseCache
from clearscout.infrastructure.providers.client import Client
from clearscout.infrastructure.providers.plan import RequestPlan
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.costs import Ledger
from clearscout.shared.results import Finding, Outcome
from tests.support.doubles import ScriptedProvider
from tests.support.reports import rendered_report, report_section


def spec_with(*keywords: str) -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(Requirement(id="r01", text="Kafka", importance=Importance.MUST_HAVE),),
        keywords=keywords,
        hard_gates=(),
        origin="test",
    )


def finding(report: Report, name: str) -> Finding:
    return next(item for item in report.findings if item.check == name)


def outcome(report: Report, name: str) -> Outcome:
    return finding(report, name).outcome


def build(inventory, settings, draft, markdown, keywords=("Kafka",), *, plan=None, spec=None):
    target = spec if spec is not None else spec_with(*keywords)
    return check(
        markdown=markdown,
        draft=draft,
        plan=plan if plan is not None else build_plan(inventory, target, EvidenceMap(), settings),
        inventory=inventory,
        spec=target,
        guardrails=_guardrails(),
    )


def _guardrails():
    from clearscout.application.tailoring.guardrails import Guardrails

    return Guardrails(
        protected=["Staff Systems Engineer - Platform"],
        required=["jane.doe@example.com"],
        forbidden=["Project Halyard"],
        first_person=False,
        ascii_only=True,
    )


EMPTY_RESUME = ResumeDraft(summary=DraftedLine(text=""))


def simple_draft(inventory, text: str, source_ids: tuple[str, ...]) -> ResumeDraft:
    return ResumeDraft(
        summary=DraftedLine(text=text, source_ids=source_ids),
        skills=(DraftedSkill(label="Platforms", text="Python", source_ids=source_ids),),
    )


def evidence_record(inventory: CvInventory, text: str) -> tuple[CvInventory, str]:
    """Return the inventory with one entry's text replaced by ``text``, and that entry's id."""
    cited = next(e for e in inventory.entries if e.text.startswith("Jane Example builds"))
    entry = cited.model_copy(update={"text": text})
    record = inventory.model_copy(
        update={"entries": tuple(entry if e.id == cited.id else e for e in inventory.entries)}
    )
    return record, cited.id


def test_a_clean_document_passes_every_check(inventory, settings) -> None:
    entry = next(e for e in inventory.entries if e.text.startswith("Jane Example builds"))
    draft = simple_draft(inventory, "Jane Example builds data platforms.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nBuilds data platforms.\n"
    report = build(inventory, settings, draft, markdown, keywords=("data platforms",))
    assert report.passed
    assert outcome(report, "sources cited") is Outcome.PASS


def test_a_missing_selected_bullet_answer_fails_with_redraft_feedback(inventory, settings) -> None:
    """Selected bullet slots must reach a cited drafted line or trigger repair."""
    from clearscout.application.master_cv.cv import EntryKind
    from clearscout.application.tailoring.stages.match import Classification, Match, Strength

    kafka = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion tier"))
    summary = inventory.of_kind(EntryKind.SUMMARY)[0]
    spec = spec_with("Kafka")
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings)
    assert "r01" in {
        requirement_id
        for entry_id in plan.all_ids()
        for requirement_id in plan.requirements_for.get(entry_id, ())
    }
    draft = simple_draft(
        inventory, "Kafka platforms, but nothing this line answers.", (summary.id,)
    )
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nKafka platforms.\n"
    report = check(
        markdown=markdown,
        draft=draft,
        plan=plan,
        inventory=inventory,
        spec=spec,
        guardrails=_guardrails(),
    )
    assert outcome(report, "must-have coverage") is Outcome.FAIL
    assert "must-have coverage" in report.redraftable_checks()
    assert kafka.id in report.feedback()
    assert report.offenders["must-have coverage"] == 1


def test_a_dropped_publication_no_longer_answers_its_must_have(inventory, settings) -> None:
    """Fit can drop a publication, and a page without it does not answer what it answered."""
    from clearscout.application.tailoring.stages.match import Classification, Match, Strength

    publication = inventory.of_kind(EntryKind.PUBLICATION)[0]
    summary = inventory.of_kind(EntryKind.SUMMARY)[0]
    spec = spec_with("Kafka")
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(publication.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    plan = build_plan(inventory, spec, evidence, settings)
    assert publication.id in plan.publication_ids
    draft = simple_draft(inventory, "Builds data platforms.", (summary.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nBuilds data platforms.\n"

    def coverage(excluded: frozenset[str]) -> Finding:
        report = check(
            markdown=markdown,
            draft=draft,
            plan=plan,
            inventory=inventory,
            spec=spec,
            guardrails=_guardrails(),
            excluded=excluded,
        )
        return finding(report, "must-have coverage")

    assert coverage(frozenset()).outcome is Outcome.PASS
    dropped = coverage(frozenset({publication.id}))
    assert dropped.outcome is Outcome.WARN
    assert dropped.detail == "r01: its answer was dropped to fit the page"


def test_an_uncited_line_fails_provenance(inventory, settings) -> None:
    draft = simple_draft(inventory, "Something asserted from nowhere.", ())
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "sources cited") is Outcome.FAIL


def test_a_resume_line_typed_as_company_evidence_fails_sources_cited(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = ResumeDraft(
        summary=DraftedLine(
            text="Candidate claim.",
            source_ids=(entry.id,),
            source_types=(SourceType.COMPANY,),
        ),
        skills=(),
        roles=(),
    )
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "sources cited") is Outcome.FAIL


def test_a_citation_outside_the_plan_fails_provenance(inventory, settings) -> None:
    draft = simple_draft(inventory, "Text.", ("b-deadbeef",))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "sources cited") is Outcome.FAIL


def test_a_figure_may_not_be_borrowed_from_a_second_citation(inventory, settings) -> None:
    """Pooling sources would let a line take a number from one and a claim from another."""
    kafka = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion"))
    quality = next(e for e in inventory.entries if e.text.startswith("Introduced data quality"))
    draft = simple_draft(inventory, "Cut latency across 1,400 columns.", (kafka.id, quality.id))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "numbers traced") is Outcome.PASS


def test_an_invented_figure_fails_the_number_check(inventory, settings) -> None:
    entry = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion tier"))
    draft = simple_draft(inventory, "Cut latency across 9,000 source feeds.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "numbers traced") is Outcome.FAIL


def test_a_figure_present_in_the_evidence_passes(inventory, settings) -> None:
    entry = next(e for e in inventory.entries if e.text.startswith("Rebuilt the ingestion tier"))
    draft = simple_draft(inventory, "Cut latency across 212 source feeds.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "numbers traced") is Outcome.PASS


def _traced(inventory, settings, text: str, evidence: str) -> Outcome:
    """Score one drafted sentence against a single cited entry carrying ``evidence``."""
    record, entry_id = evidence_record(inventory, evidence)
    markdown = f"# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\n{text}\n"
    draft = simple_draft(record, text, (entry_id,))
    return outcome(build(record, settings, draft, markdown), "numbers traced")


def test_a_span_spelled_in_words_that_no_citation_states_fails(inventory, settings) -> None:
    """A duration written in words is a claim, and digits were the only thing traced."""
    assert (
        _traced(
            inventory,
            settings,
            "Eleven years of schema governance.",
            "Ran schema governance for the platform team.",
        )
        is Outcome.FAIL
    )


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("Eleven years of schema governance.", "Ran schema governance for 11 years."),
        ("11 years of schema governance.", "Ran schema governance for eleven years."),
        ("Eleven years of schema governance.", "Ran schema governance for eleven years."),
        ("Twelve months of schema governance.", "Ran schema governance for 12 months."),
        ("A 12-year run of schema governance.", "Ran schema governance for a twelve-year run."),
        ("Two decades of schema governance.", "Ran schema governance for 20 years."),
        ("20 years of schema governance.", "Ran schema governance for two decades."),
        ("Two decades of schema governance.", "Ran schema governance for 2 decades."),
    ],
)
def test_a_spelled_span_traces_the_same_figure_written_either_way(
    inventory, settings, text: str, evidence: str
) -> None:
    assert _traced(inventory, settings, text, evidence) is Outcome.PASS


@pytest.mark.parametrize("count", ["Twenty-five", "Twenty five"])
def test_a_compound_count_is_read_whole_and_not_as_its_last_part(
    inventory, settings, count: str
) -> None:
    """Reading "twenty-five years" as five would accuse an honest line and excuse a false one."""
    text = f"{count} years of schema governance."
    assert _traced(inventory, settings, text, "Ran schema governance for 25 years.") is Outcome.PASS
    assert _traced(inventory, settings, text, "Ran schema governance for 5 years.") is Outcome.FAIL


@pytest.mark.parametrize(
    "text",
    [
        "A decade of schema governance.",
        "Several years of schema governance.",
        "Two and a half years of schema governance.",
        "One hundred and twenty days of schema governance.",
        "One hundred-twenty days of schema governance.",
        "One-hundred-twenty days of schema governance.",
        "A one-hundred-twenty-day rollout of schema governance.",
        "Two hundred days of schema governance.",
    ],
)
def test_a_span_this_check_cannot_read_exactly_stays_silent(inventory, settings, text: str) -> None:
    """A shape with no exact reading is left to the critic; a misread span is a false accusation."""
    assert (
        _traced(inventory, settings, text, "Ran schema governance for the platform team.")
        is Outcome.PASS
    )


def test_a_scale_count_written_with_hyphens_does_not_accuse_its_own_evidence(
    inventory, settings
) -> None:
    """The hyphens are a typographic choice, and the last part of the count is not the count."""
    assert (
        _traced(
            inventory,
            settings,
            "Ran a one-hundred-twenty-day rollout of schema governance.",
            "Ran a 120-day rollout of schema governance.",
        )
        is Outcome.PASS
    )


def test_a_sentence_copied_from_its_own_evidence_is_traced(inventory, settings) -> None:
    """A word suppressed in one sentence must not suppress a span in the next one."""
    copied = "Scaled to one hundred! Twenty days of downtime followed."
    assert _traced(inventory, settings, copied, copied) is Outcome.PASS


def test_a_span_spelled_in_decades_that_no_citation_states_fails(inventory, settings) -> None:
    """Two decades spells twenty years exactly, so it is as traceable as twenty is."""
    assert (
        _traced(
            inventory,
            settings,
            "Two decades of schema governance.",
            "Ran schema governance for the platform team.",
        )
        is Outcome.FAIL
    )


def _scoped(inventory, settings, text: str, evidence: str, *also: str) -> Finding:
    """Return the duration finding for one sentence citing an entry carrying ``evidence``."""
    record, entry_id = evidence_record(inventory, evidence)
    markdown = f"# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\n{text}\n"
    draft = simple_draft(record, text, (entry_id, *also))
    return finding(build(record, settings, draft, markdown), "duration scope")


@pytest.mark.parametrize(
    "text",
    [
        "8 years leading data projects.",
        "8+ years leading data projects.",
        "Eight years leading data projects.",
        "Data projects led over eight years.",
    ],
)
def test_a_duration_moved_onto_a_subject_its_evidence_gives_another_fails(
    inventory, settings, text: str
) -> None:
    """The figure traces, so only the subject it is attached to shows the borrowing."""
    evidence = "8 years in clinical pharmacy; 2 years leading data projects."
    record, entry_id = evidence_record(inventory, evidence)
    markdown = f"# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\n{text}\n"
    report = build(record, settings, simple_draft(record, text, (entry_id,)), markdown)

    assert outcome(report, "numbers traced") is Outcome.PASS
    assert outcome(report, "duration scope") is Outcome.FAIL
    assert "duration scope" in report.redraftable_checks()
    assert "clinical pharmacy" in finding(report, "duration scope").detail


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        (
            "2 years leading data projects.",
            "8 years in clinical pharmacy; 2 years leading data projects.",
        ),
        (
            "Two years leading data projects.",
            "Eight years in pharmacy, then 2 years leading data projects.",
        ),
        ("Two decades of schema governance.", "Ran schema governance for 20 years."),
        ("Eleven years of schema governance.", "Ran schema governance for eleven years."),
    ],
)
def test_a_duration_kept_on_its_evidence_subject_passes(
    inventory, settings, text: str, evidence: str
) -> None:
    assert _scoped(inventory, settings, text, evidence).outcome is Outcome.PASS


def test_a_duration_borrowed_from_a_second_citation_fails(inventory, settings) -> None:
    """The second entry names the line's subject and states no duration, so the span moved."""
    quality = next(e for e in inventory.entries if e.text.startswith("Introduced data quality"))
    scope = _scoped(
        inventory,
        settings,
        "Introduced data quality rules over 3 years.",
        "Ran the warehouse migration for 3 years.",
        quality.id,
    )
    assert scope.outcome is Outcome.FAIL


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("8 years leading data projects.", "8 months leading data projects."),
        ("Delivered the migration in 6 weeks.", "Cut the migration from 6 hours to 90 minutes."),
    ],
)
def test_a_value_its_evidence_never_states_as_this_duration_fails(
    inventory, settings, text: str, evidence: str
) -> None:
    """The bare value traces, so the number check alone would pass the changed unit."""
    record, entry_id = evidence_record(inventory, evidence)
    markdown = f"# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\n{text}\n"
    report = build(record, settings, simple_draft(record, text, (entry_id,)), markdown)

    assert outcome(report, "numbers traced") is Outcome.PASS
    assert outcome(report, "duration scope") is Outcome.FAIL


def test_a_subject_neither_side_pairs_warns_without_a_redraft(inventory, settings) -> None:
    scope = _scoped(
        inventory,
        settings,
        "Eleven years of platform leadership.",
        "Ran schema governance for 11 years.",
    )
    assert scope.outcome is Outcome.WARN
    assert not scope.redraftable
    assert "not paired" in scope.detail


def test_a_duration_no_citation_states_is_left_to_the_number_check(inventory, settings) -> None:
    scope = _scoped(
        inventory,
        settings,
        "Eleven years of schema governance.",
        "Ran schema governance for the platform team.",
    )
    assert scope.outcome is Outcome.PASS


def test_first_person_in_drafted_prose_fails(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "I built the platform.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nI built the platform.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "first person") is Outcome.FAIL


def test_a_roman_numeral_is_not_a_first_person_pronoun(inventory, settings) -> None:
    """A publication titled "Phase I" must not read as the word "I"."""
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Led Phase I and Type I analyses.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "first person") is Outcome.PASS


def test_smart_punctuation_the_model_wrote_fails_and_is_redraftable(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Built it — fast.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nBuilt it — fast.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "character policy") is Outcome.FAIL
    assert report.worth_redrafting


def test_smart_punctuation_copied_from_the_cv_is_not_redraftable(inventory, settings) -> None:
    """No rewrite can remove a character the assembler copied, so do not retry."""
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Clean prose.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Education\n\nBachelor’s degree.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "source characters") is Outcome.FAIL
    assert not report.worth_redrafting
    assert report.feedback() == ""


def test_a_forbidden_string_fails(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nWorked on Project Halyard.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "forbidden strings") is Outcome.FAIL


def test_a_forbidden_string_fails_when_spelled_in_another_case(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nProject halyard shipped.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "forbidden strings") is Outcome.FAIL


def test_a_forbidden_string_fails_when_split_across_a_line_wrap(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nRan Project\nHalyard.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "forbidden strings") is Outcome.FAIL


def test_a_forbidden_string_the_assembler_copied_is_not_redraftable(inventory, settings) -> None:
    """No rewrite can remove a phrase the assembler copied, so do not retry."""
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Clean prose.", (entry.id,))
    markdown = (
        "# JANE\n\njane.doe@example.com\n\n---\n\n## Education\n\nProject Halyard fellowship.\n"
    )
    report = build(inventory, settings, draft, markdown)
    hit = finding(report, "forbidden strings")
    assert hit.outcome is Outcome.FAIL
    assert not hit.redraftable


def test_a_forbidden_string_the_model_wrote_stays_redraftable(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Worked on Project Halyard.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nWorked on Project Halyard.\n"
    report = build(inventory, settings, draft, markdown)
    hit = finding(report, "forbidden strings")
    assert hit.outcome is Outcome.FAIL
    assert hit.redraftable


def test_a_copied_forbidden_string_hardens_a_drafted_one(inventory, settings) -> None:
    """One finding carries both halves, and the copied half decides the verdict."""
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Worked on Project Halyard.", (entry.id,))
    markdown = (
        "# JANE\n\njane.doe@example.com\n\n---\n\n"
        "## Summary\n\nWorked on Project Halyard.\n\n## Education\n\nMoonrise Grant fellow.\n"
    )
    spec = spec_with("Kafka")
    report = check(
        markdown=markdown,
        draft=draft,
        plan=build_plan(inventory, spec, EvidenceMap(), settings),
        inventory=inventory,
        spec=spec,
        guardrails=Guardrails(
            protected=[],
            required=[],
            forbidden=["Project Halyard", "Moonrise Grant"],
            first_person=False,
            ascii_only=True,
        ),
    )
    hit = finding(report, "forbidden strings")
    assert hit.outcome is Outcome.FAIL
    assert not hit.redraftable
    assert "Project Halyard" in hit.detail
    assert "Moonrise Grant" in hit.detail


def test_a_forbidden_string_in_a_cover_letter_stays_redraftable(inventory, settings) -> None:
    report = letter_with(inventory, settings, "Worked on Project Halyard.")
    hit = finding(report, "forbidden strings")
    assert hit.outcome is Outcome.FAIL
    assert hit.redraftable


def test_a_forbidden_string_in_supplied_text_stays_redraftable(inventory, settings) -> None:
    from clearscout.application.tailoring.stages.verify import check_supplied

    entry = inventory.entries[0]
    findings = check_supplied(
        lines=(DraftedLine(text="Worked on Project Halyard.", source_ids=(entry.id,)),),
        plan=build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=_guardrails(),
    )
    hit = next(item for item in findings if item.check == "forbidden strings")
    assert hit.outcome is Outcome.FAIL
    assert hit.redraftable


def test_a_missing_required_string_fails(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = "# JANE\n\nno contact here\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "required strings") is Outcome.FAIL


def test_a_protected_phrase_in_variant_case_must_be_written_verbatim(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = (
        "# JANE\n\njane.doe@example.com\n\n---\n\n"
        "## Summary\n\nstaff systems engineer  -  platform at Northwind.\n"
    )
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "protected strings") is Outcome.FAIL


def test_an_untouched_protected_title_passes(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = (
        "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nSenior engineer at Northwind.\n"
    )
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "protected strings") is Outcome.PASS


def test_a_shared_first_word_does_not_trigger_the_protected_rule(inventory, settings) -> None:
    """A common first word alone must not fire a multi-word phrase rule."""
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = (
        "# JANE\n\njane.doe@example.com\n\n---\n\n"
        "## Summary\n\nStaff meetings ran weekly at Northwind.\n"
    )
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "protected strings") is Outcome.PASS


def test_a_malformed_document_fails_the_structure_check(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    report = build(inventory, settings, draft, "no heading at all\n")
    assert outcome(report, "document structure") is Outcome.FAIL


def test_a_well_formed_document_passes_the_structure_check(inventory, settings) -> None:
    entry = inventory.entries[0]
    draft = simple_draft(inventory, "Text.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nText.\n"
    report = build(inventory, settings, draft, markdown)
    assert outcome(report, "document structure") is Outcome.PASS


def test_keyword_coverage_is_measured_not_guessed() -> None:
    coverage = keyword_coverage(
        "Built a schema registry on Kafka.", spec_with("Kafka", "schema registry", "Rust")
    )
    assert coverage.present == ("Kafka", "schema registry")
    assert coverage.missing == ("Rust",)
    assert 0.6 < coverage.ratio < 0.7


def test_a_phrase_from_the_posting_title_is_scored_in_neither_direction() -> None:
    """The title names the job, and only the master CV decides whether it is true."""
    coverage = keyword_coverage(
        "Built a schema registry on Kafka.", spec_with("Kafka", "data platform", "Rust")
    )
    assert coverage.title_terms == ("data platform",)
    assert coverage.present == ("Kafka",)
    assert coverage.missing == ("Rust",)
    assert coverage.ratio == 0.5


def test_absent_title_terms_no_longer_warn_a_truthful_resume_down(inventory, settings) -> None:
    """A resume that does not claim the posting's title is not short on keywords."""
    entry = next(e for e in inventory.entries if e.text.startswith("Jane Example builds"))
    draft = simple_draft(inventory, "Jane Example builds data platforms.", (entry.id,))
    markdown = "# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\nBuilds data platforms.\n"
    report = build(
        inventory, settings, draft, markdown, keywords=("data platforms", "staff", "engineer")
    )
    assert outcome(report, "keyword coverage") is Outcome.PASS
    detail = next(item.detail for item in report.findings if item.check == "keyword coverage")
    assert "posting title terms" in detail
    assert detail.endswith("staff, engineer")


def test_the_critic_verdict_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field the critic contract never declared is refused, not dropped."""
    with pytest.raises(ValidationError):
        CriticVerdict.model_validate(
            {
                "alignment": 5,
                "preservation": 5,
                "specificity": 5,
                "readability": 5,
                "ats_clarity": 5,
                "human_voice": 5,
                "extra_field": "x",
            }
        )


def test_very_low_alignment_triggers_the_bounded_rewrite_loop() -> None:
    verdict = CriticVerdict(
        alignment=2,
        preservation=5,
        specificity=4,
        readability=4,
        ats_clarity=4,
        human_voice=4,
    )
    assert critic_finding(verdict).outcome is Outcome.FAIL


def test_sendable_but_unexceptional_quality_is_a_warning() -> None:
    verdict = CriticVerdict(
        alignment=3,
        preservation=5,
        specificity=3,
        readability=4,
        ats_clarity=4,
        human_voice=3,
    )
    assert critic_finding(verdict).outcome is Outcome.WARN


@pytest.mark.parametrize("check", ["critic", "letter critic"])
def test_weak_prose_is_reported_without_a_rewrite_but_an_unsupported_claim_still_blocks(
    check: str,
) -> None:
    """Readability, ATS clarity, and human voice below the floor warn on either document."""
    weak = CriticVerdict(
        alignment=4,
        preservation=5,
        specificity=4,
        readability=2,
        ats_clarity=2,
        human_voice=2,
    )
    unsupported = weak.model_copy(update={"unsupported_claims": [_doubt()]})
    unfaithful = weak.model_copy(update={"preservation": 3})

    finding = critic_finding(weak, check)
    assert (finding.outcome, finding.redraftable) == (Outcome.WARN, False)
    assert "readability 2/5" in finding.detail
    assert critic_finding(unsupported, check).outcome is Outcome.FAIL
    assert critic_finding(unfaithful, check).outcome is Outcome.FAIL


def test_strong_quality_passes_and_exposes_a_mean() -> None:
    verdict = CriticVerdict(
        alignment=4,
        preservation=5,
        specificity=4,
        readability=5,
        ats_clarity=4,
        human_voice=4,
    )
    assert critic_finding(verdict).outcome is Outcome.PASS
    assert verdict.quality_average == 4.2


def _doubt(
    claim: str = "Led the platform team.",
    boundary: ClaimBoundary = ClaimBoundary.TEAM_CONTRIBUTION,
    corroboration: int = 0,
) -> DoubtedClaim:
    return DoubtedClaim(claim=claim, boundary=boundary, corroboration=corroboration)


def _verdict(**overrides: Any) -> CriticVerdict:
    scores = {
        "alignment": 4,
        "preservation": 5,
        "specificity": 4,
        "readability": 4,
        "ats_clarity": 4,
        "human_voice": 4,
    }
    return CriticVerdict(**{**scores, **overrides})


def test_the_critic_may_answer_with_these_eleven_boundaries_and_nothing_else() -> None:
    """The vocabulary is read off the document the provider is sent, and is closed.

    Naming the members here rather than deriving them means one leaving the enum
    fails this test instead of quietly narrowing what a doubt can say.
    """
    schema = json_schema(CriticVerdict)
    assert schema["$defs"]["ClaimBoundary"]["enum"] == [
        "prototype",
        "exploratory",
        "familiarity",
        "adjacent",
        "production",
        "formal_title_or_tenure",
        "team_contribution",
        "individual_ownership",
        "planned_not_shipped",
        "miscited",
        "absent_from_record",
    ]
    assert [boundary.value for boundary in ClaimBoundary] == (
        schema["$defs"]["ClaimBoundary"]["enum"]
    )
    assert schema["$defs"]["DoubtedClaim"]["required"] == ["claim", "boundary"]
    with pytest.raises(ValidationError):
        DoubtedClaim.model_validate({"claim": "Led the platform team.", "boundary": "overreach"})


@pytest.mark.parametrize("boundary", list(ClaimBoundary))
def test_a_doubted_claim_fails_the_draft_whatever_boundary_it_crosses(
    boundary: ClaimBoundary,
) -> None:
    """The boundary describes an existing failure and never excuses one."""
    verdict = _verdict(unsupported_claims=[_doubt(boundary=boundary)])
    result = critic_finding(verdict)

    assert result.outcome is Outcome.FAIL
    assert "Led the platform team." in result.detail
    assert boundary.value in result.detail


def test_an_unsupported_claim_does_not_hide_a_failing_alignment_score_from_the_redraft() -> None:
    """A redraft told only about the claim fixes it and leaves the draft misaligned."""
    verdict = _verdict(
        alignment=2, unsupported_claims=[_doubt()], guidance="Lead with the streaming work."
    )

    result = critic_finding(verdict)

    assert result.outcome is Outcome.FAIL
    assert result.detail.startswith("unsupported claims: Led the platform team.")
    assert "quality below 3/5: alignment 2/5" in result.detail
    assert result.detail.endswith("Lead with the streaming work.")


def test_a_warning_only_score_stays_out_of_the_detail_an_unsupported_claim_fails_with() -> None:
    """Weak prose alone never blocks a draft, so the redraft is not sent after it."""
    verdict = _verdict(readability=2, unsupported_claims=[_doubt()], guidance="Tighten it.")

    assert critic_finding(verdict).detail == (
        "unsupported claims: Led the platform team. [team_contribution]"
    )


@pytest.mark.parametrize("check", ["critic", "letter critic"])
def test_a_style_flag_on_a_passing_read_warns_without_asking_for_a_rewrite(check: str) -> None:
    """Scores at the floor do not hide a flag the critic raised about how the draft reads."""
    verdict = _verdict(style_flags=["Three bullets open with Spearheaded."])

    finding = critic_finding(verdict, check)

    assert (finding.outcome, finding.redraftable) == (Outcome.WARN, False)
    assert finding.advisories == ("Three bullets open with Spearheaded.",)
    assert finding.detail.startswith("alignment 4/5")
    assert finding.detail.endswith("quality mean 4.0/5; 1 style flag, listed first")


def test_a_style_flag_on_a_below_floor_warning_is_named_in_its_detail() -> None:
    verdict = _verdict(readability=2, style_flags=["Three bullets open with Spearheaded.", "x"])

    finding = critic_finding(verdict)

    assert finding.outcome is Outcome.WARN
    assert finding.advisories[:2] == ("Three bullets open with Spearheaded.", "x")
    assert "2 style flags, listed first" in finding.detail


def test_a_passing_read_without_style_flags_names_no_style_cause() -> None:
    finding = critic_finding(_verdict())

    assert finding.outcome is Outcome.PASS
    assert "style flag" not in finding.detail


@pytest.mark.parametrize("check", ["critic", "letter critic"])
def test_a_weakness_on_a_passing_read_warns_and_rides_along_with_guidance(check: str) -> None:
    verdict = _verdict(
        weaknesses=["The Acme migration has no outcome."], guidance="Name what the migration cut."
    )

    finding = critic_finding(verdict, check)

    assert (finding.outcome, finding.redraftable) == (Outcome.WARN, False)
    assert finding.detail.endswith("quality mean 4.0/5; 1 weakness listed")
    assert finding.advisories == (
        "The Acme migration has no outcome.",
        "Name what the migration cut.",
    )


def test_guidance_a_failing_detail_already_quotes_is_not_repeated_as_an_advisory() -> None:
    verdict = _verdict(
        readability=2, weaknesses=["The summary repeats the headline."], guidance="Tighten it."
    )

    finding = critic_finding(verdict)

    assert finding.detail.endswith("Tighten it.")
    assert finding.advisories == ("The summary repeats the headline.",)


def test_advisories_stay_out_of_the_redraft_feedback() -> None:
    """The redraft request is sent the failure details alone, so its bytes do not move."""
    verdict = _verdict(
        unsupported_claims=[_doubt()],
        style_flags=["Stacked adjectives in the summary."],
        weaknesses=["The Globex rollout has no scale."],
        guidance="Say how many sites the rollout reached.",
    )
    report = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))

    critiqued = report.with_critic(verdict)

    assert finding(critiqued, "critic").advisories == (
        "Stacked adjectives in the summary.",
        "The Globex rollout has no scale.",
        "Say how many sites the rollout reached.",
    )
    assert critiqued.feedback() == (
        "- critic: unsupported claims: Led the platform team. [team_contribution]"
    )


def test_a_reader_sees_advisories_with_each_id_replaced_by_its_words() -> None:
    verdict = _verdict(weaknesses=["[b-1a2b3c4d] is buried under r04."])
    report = Report(findings=(), coverage=KeywordCoverage(present=(), missing=()))
    known = {"b-1a2b3c4d": "Ran the Initech payments cutover", "r04": "Own payment systems"}

    shown = report.with_critic(verdict, LETTER_CRITIC).for_reader(known)

    assert finding(shown, LETTER_CRITIC).advisories == (
        '"Ran the Initech payments cutover" is buried under "Own payment systems".',
    )


@pytest.mark.parametrize("boundary", list(ClaimBoundary))
def test_the_review_document_names_a_doubted_claim_without_its_boundary_token(
    boundary: ClaimBoundary,
) -> None:
    """A boundary is vocabulary the model and the redraft loop share, not words to read."""
    verification = Report(
        findings=(),
        coverage=KeywordCoverage(present=(), missing=()),
        critic=_verdict(unsupported_claims=[_doubt(boundary=boundary)]),
    )

    critic = report_section(rendered_report(verification=verification), "Critic")

    assert "- Led the platform team." in critic
    assert boundary.value not in critic


def _critiqued(
    inventory: CvInventory,
    settings: Any,
    guardrails: Guardrails,
    directory: Path,
    answers: list[CriticVerdict],
    statistic: CriticStatistic = CriticStatistic.MEDIAN,
) -> tuple[CriticVerdict, tuple[CriticVerdict, ...]]:
    provider = ScriptedProvider({"CriticVerdict": lambda schema: answers.pop(0).model_dump()})
    with ResponseCache(enabled=True, directory=directory) as cache:
        client = Client(
            provider=provider,
            settings=settings.model_copy(
                update={"critic_trials": 3, "critic_score_statistic": statistic}
            ),
            cache=cache,
            ledger=Ledger(),
        )
        return critique(
            client,
            markdown="# JANE\n\njane.doe@example.com\n\n## Summary\n\nBuilds data platforms.\n",
            citations="",
            inventory=inventory,
            offered=(),
            spec=spec_with("Kafka"),
            guardrails=guardrails,
        )


def _merged(
    inventory: CvInventory,
    settings: Any,
    guardrails: Guardrails,
    directory: Path,
    answers: list[CriticVerdict],
    statistic: CriticStatistic = CriticStatistic.MEDIAN,
) -> CriticVerdict:
    verdict, _ = _critiqued(inventory, settings, guardrails, directory, answers, statistic)
    return verdict


def test_two_boundaries_drawn_once_each_break_to_the_earliest_doubting_trial(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """One claim is one doubt, and a boundary no draw named twice is settled by order.

    Neither reading is protected and neither is more common, so the group takes the
    one the earlier doubting trial gave it. A claim only a later trial raises still
    survives the merge.
    """
    spend = _doubt("Cut recurring spend.", ClaimBoundary.PRODUCTION)
    answers = [
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.TEAM_CONTRIBUTION)]),
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.INDIVIDUAL_OWNERSHIP), spend]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert verdict.unsupported_claims == [
        _doubt(boundary=ClaimBoundary.TEAM_CONTRIBUTION, corroboration=2),
        spend.model_copy(update={"corroboration": 1}),
    ]
    assert critic_finding(verdict).outcome is Outcome.FAIL


def test_three_wordings_of_one_sentence_reach_the_redraft_as_one_doubt(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The critic may reword a doubt, and three wordings of one sentence are one finding.

    A redraft told the same thing three times under three boundaries is being asked
    to fix three sentences that do not exist.
    """
    answers = [
        _verdict(unsupported_claims=[_doubt("Owned the migration to Kafka end to end.")]),
        _verdict(unsupported_claims=[_doubt("Owned the migration to Kafka, end to end.")]),
        _verdict(unsupported_claims=[_doubt("Owned the Kafka migration end to end.")]),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    detail = critic_finding(verdict).detail
    assert len(verdict.unsupported_claims) == 1
    assert detail.count("Owned the") == 1


def test_one_quoted_line_with_different_reasons_attached_is_one_doubt(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Each draw may explain its doubt in its own words after quoting the same line.

    Counted apart, one unanimous doubt reads as three raised once each, and fills
    the redraft's five places with a single sentence.
    """
    line = "rebuilt the ingestion tier with the analytics group"
    answers = [
        _verdict(
            unsupported_claims=[_doubt(f"\u201c{line}\u201d moves the group onto the rebuild.")]
        ),
        _verdict(
            unsupported_claims=[_doubt(f"\u201c{line}.\u201d The evidence puts it elsewhere.")]
        ),
        _verdict(unsupported_claims=[_doubt(f"\u201cwho {line}\u201d")]),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.corroboration for doubt in verdict.unsupported_claims] == [3]


def test_a_short_quote_inside_longer_quotes_of_the_same_line_is_one_doubt(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A draw quoting less of a line doubts the same statement as the draws quoting all of it."""
    line = "rebuilt the ingestion tier for 12 depots across the northern region"
    answers = [
        _verdict(
            unsupported_claims=[
                _doubt("\u201crebuilt the ingestion tier for 12 depots\u201d overstates it.")
            ]
        ),
        _verdict(unsupported_claims=[_doubt(f"\u201c{line}\u201d is not in the record.")]),
        _verdict(unsupported_claims=[_doubt(f"\u201c{line}\u201d")]),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.corroboration for doubt in verdict.unsupported_claims] == [3]


@pytest.mark.parametrize(
    ("short", "long"),
    [
        ("led", "Led the migration and cut spend by 40 percent."),
        ("Kafka", "Ran Kafka in production for 3 teams."),
        ("40 percent", "migrated 12 depots, cutting spend 40 percent"),
        ("data pipelines", "Owned the data pipelines for the whole org."),
    ],
)
def test_a_quote_inside_another_is_a_second_doubt_when_it_names_less_or_other_figures(
    short: str,
    long: str,
    inventory: CvInventory,
    settings,
    guardrails: Guardrails,
    tmp_path: Path,
) -> None:
    """A word or two of a longer line can be a doubt of its own, and folding it drops it."""
    doubts = [_doubt(f"\u201c{short}\u201d"), _doubt(f"\u201c{long}\u201d")]
    answers = [
        _verdict(unsupported_claims=[doubts[0]]),
        _verdict(unsupported_claims=[doubts[1]]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.claim for doubt in verdict.unsupported_claims] == [d.claim for d in doubts]


def test_a_doubt_quoting_only_punctuation_does_not_absorb_a_real_one(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A span that folds to nothing sits inside no worded span, so both doubts reach the redraft."""
    empty = _doubt("--")
    answers = [
        _verdict(unsupported_claims=[_doubt()]),
        _verdict(unsupported_claims=[empty]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.claim for doubt in verdict.unsupported_claims] == [_doubt().claim, empty.claim]


def test_two_quotes_of_different_claims_on_one_line_stay_two_doubts(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Neither quote holds the other, so the line carries two doubts to the redraft."""
    spend = _doubt("\u201ccut recurring spend by 30 percent\u201d", ClaimBoundary.PRODUCTION)
    depots = _doubt("\u201cacross 12 regional depots\u201d", ClaimBoundary.PRODUCTION)
    answers = [
        _verdict(unsupported_claims=[spend]),
        _verdict(unsupported_claims=[depots]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.claim for doubt in verdict.unsupported_claims] == [spend.claim, depots.claim]


def test_two_reasons_one_draw_gives_for_one_quoted_line_stay_two_doubts_raised_once(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A draw doubting one line twice is one draw, and the report counts draws.

    Merged, the pair would read as raised by two draws and lose one reason from
    the redraft feedback.
    """
    line = "rebuilt the ingestion tier with the analytics group"
    silent = _doubt(
        f"\u201c{line}\u201d is nowhere in the record.", ClaimBoundary.ABSENT_FROM_RECORD
    )
    miscited = _doubt(f"\u201c{line}\u201d cites the wrong role.", ClaimBoundary.MISCITED)
    answers = [_verdict(unsupported_claims=[silent, miscited]), _verdict(), _verdict()]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert verdict.unsupported_claims == [
        silent.model_copy(update={"corroboration": 1}),
        miscited.model_copy(update={"corroboration": 1}),
    ]


def test_one_draw_reading_the_record_as_silent_decides_the_merged_boundary(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Telling a redraft to qualify a sentence no record supports ships a hedged fabrication.

    The silent reading arrives after the first and still wins, because deleting one true
    sentence is the cheaper mistake.
    """
    answers = [
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.ADJACENT)]),
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.ADJACENT)]),
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.ABSENT_FROM_RECORD)]),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert verdict.unsupported_claims == [
        _doubt(boundary=ClaimBoundary.ABSENT_FROM_RECORD, corroboration=3)
    ]
    assert ClaimBoundary.ABSENT_FROM_RECORD.value in critic_finding(verdict).detail


def test_one_draw_reading_a_line_as_miscited_does_not_decide_the_merged_boundary(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A miscitation crosses no boundary in the evidence, so it does not settle the group.

    The record may carry the claim elsewhere, and telling a person the record is
    silent about work it holds invites them to delete something true.
    """
    answers = [
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.MISCITED)]),
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.ADJACENT)]),
        _verdict(unsupported_claims=[_doubt(boundary=ClaimBoundary.ADJACENT)]),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert verdict.unsupported_claims == [_doubt(boundary=ClaimBoundary.ADJACENT, corroboration=3)]
    assert ClaimBoundary.MISCITED.value not in critic_finding(verdict).detail


def test_two_claims_differing_only_in_a_figure_both_reach_the_redraft(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Two figures are two claims, and merging them would delete a doubt about a number.

    The wordings are otherwise identical, so a similarity match alone would fold the
    pair and leave one unsupported figure standing in the draft.
    """
    thirty = _doubt("Cut recurring spend by 30 percent.", ClaimBoundary.PRODUCTION)
    forty = _doubt("Cut recurring spend by 40 percent.", ClaimBoundary.PRODUCTION)
    answers = [
        _verdict(unsupported_claims=[thirty]),
        _verdict(unsupported_claims=[forty]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    detail = critic_finding(verdict).detail
    assert verdict.unsupported_claims == [
        thirty.model_copy(update={"corroboration": 1}),
        forty.model_copy(update={"corroboration": 1}),
    ]
    assert "30 percent" in detail
    assert "40 percent" in detail


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Scaled the service to millions of events.", "Scaled the service to billions of events."),
        ("Shipped the feature without incident.", "Shipped the feature with incident."),
        ("Mentored junior engineers.", "Mentored senior engineers."),
        ("Built the ingestion pipeline at Acme.", "Built the ingestion pipeline at Globex."),
    ],
)
def test_two_claims_differing_only_in_a_content_word_both_reach_the_redraft(
    first: str,
    second: str,
    inventory: CvInventory,
    settings,
    guardrails: Guardrails,
    tmp_path: Path,
) -> None:
    """A magnitude, a negation, a seniority and an employer each make a second claim.

    Every pair scores above the wording threshold, so a similarity match alone would
    fold it and leave the redraft one doubt short of what the critic raised.
    """
    doubts = [_doubt(first, ClaimBoundary.PRODUCTION), _doubt(second, ClaimBoundary.PRODUCTION)]
    answers = [
        _verdict(unsupported_claims=[doubts[0]]),
        _verdict(unsupported_claims=[doubts[1]]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    detail = critic_finding(verdict).detail
    assert verdict.unsupported_claims == [
        doubt.model_copy(update={"corroboration": 1}) for doubt in doubts
    ]
    assert first in detail
    assert second in detail


def test_every_critic_trial_is_asked_again_rather_than_served_from_the_cache(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Sampling the critic catches more only when each trial is its own provider call.

    Each trial draws its own answer, and a re-run replays all of them rather than
    billing again for the draws that could not have hit one shared entry.
    """
    answers = [_verdict(), _verdict(unsupported_claims=[_doubt()]), _verdict()]
    provider = ScriptedProvider({"CriticVerdict": lambda schema: answers.pop(0).model_dump()})
    markdown = "# JANE\n\njane.doe@example.com\n\n## Summary\n\nBuilds data platforms.\n"
    with ResponseCache(enabled=True, directory=tmp_path / "critic-cache") as cache:
        client = Client(
            provider=provider,
            settings=settings.model_copy(update={"critic_trials": 3}),
            cache=cache,
            ledger=Ledger(),
        )
        verdict, draws = critique(
            client,
            markdown=markdown,
            citations="",
            inventory=inventory,
            offered=(),
            spec=spec_with("Kafka"),
            guardrails=guardrails,
        )
        again, _ = critique(
            client,
            markdown=markdown,
            citations="",
            inventory=inventory,
            offered=(),
            spec=spec_with("Kafka"),
            guardrails=guardrails,
        )

    assert len(provider.calls) == 3
    # The plan states the repeat so the adapter caches the task for trials two and three.
    assert [call[1].draws for call in provider.calls] == [3, 3, 3]
    assert len(draws) == 3
    assert verdict.unsupported_claims == [_doubt(corroboration=1)]
    assert critic_finding(verdict).outcome is Outcome.FAIL
    assert again == verdict


def test_a_critique_under_default_settings_asks_the_critic_once(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Each draw is a paid call, so sampling more than one is a setting a person raises."""
    provider = ScriptedProvider({"CriticVerdict": lambda schema: _verdict().model_dump()})
    with ResponseCache(enabled=True, directory=tmp_path / "critic-cache") as cache:
        client = Client(
            provider=provider,
            settings=settings,
            cache=cache,
            ledger=Ledger(),
        )
        _, draws = critique(
            client,
            markdown="# JANE\n\n## Summary\n\nBuilds data platforms.\n",
            citations="",
            inventory=inventory,
            offered=(),
            spec=spec_with("Kafka"),
            guardrails=guardrails,
        )

    assert len(provider.calls) == 1
    assert len(draws) == 1


class _TogetherCritic(ScriptedProvider):
    """Answer the first critic call at once and hold each later one until all are in flight."""

    def __init__(self, answers: list[CriticVerdict], later: int) -> None:
        super().__init__({"CriticVerdict": lambda schema: answers.pop(0).model_dump()})
        # Asked one at a time, the first held draw waits alone until the barrier breaks.
        self.together = threading.Barrier(later, timeout=5)
        self.first_answered = threading.Event()
        self.early = 0
        self.asked_by_caller: list[bool] = []
        self._caller = threading.get_ident()
        self._arrivals = threading.Lock()
        self._arrived = 0

    def complete(self, plan: Any, schema: Any, choice: Any) -> Any:
        with self._arrivals:
            first = self._arrived == 0
            self._arrived += 1
            if not first and not self.first_answered.is_set():
                self.early += 1
            if threading.get_ident() == self._caller:
                self.asked_by_caller.append(first)
        if first:
            answered = super().complete(plan, schema, choice)
            self.first_answered.set()
            return answered
        self.together.wait()
        return super().complete(plan, schema, choice)


def test_later_critic_draws_are_asked_together_once_the_first_has_answered(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The first draw writes the provider's prompt cache; the others need not queue behind it.

    Asked one after another, the draws cost one answer's wall time each. Every draw
    keeps its place, so a re-run asking one more trial replays the first three where
    they stood and adds the new one last.
    """
    answers = [_verdict(), _verdict(unsupported_claims=[_doubt()]), _verdict(alignment=2)]
    added = _verdict(readability=2)
    together = _TogetherCritic(answers.copy(), later=2)
    with ResponseCache(enabled=True, directory=tmp_path / "critic-cache") as cache:

        def drawn(provider: ScriptedProvider, trials: int) -> tuple[CriticVerdict, ...]:
            client = Client(
                provider=provider,
                settings=settings.model_copy(update={"critic_trials": trials}),
                cache=cache,
                ledger=Ledger(),
            )
            _, draws = critique(
                client,
                markdown="# JANE\n\n## Summary\n\nBuilds data platforms.\n",
                citations="",
                inventory=inventory,
                offered=(),
                spec=spec_with("Kafka"),
                guardrails=guardrails,
            )
            return draws

        draws = drawn(together, 3)
        extended = drawn(ScriptedProvider({"CriticVerdict": lambda schema: added.model_dump()}), 4)

    assert together.early == 0
    assert together.asked_by_caller == [True]
    assert draws[0] == answers[0]
    assert extended == (*draws, added)


class _CancelledError(Exception):
    """The run was cancelled while the critic was being asked."""


def test_a_cancel_raised_inside_a_later_critic_draw_stops_the_critique(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A later draw runs on a worker thread, and its cancel must still end the stage."""
    answered = threading.Event()

    def first_then_cancelled(schema: Any) -> dict[str, Any]:
        answered.set()
        return _verdict().model_dump()

    def raise_if_cancelled() -> None:
        if answered.is_set():
            raise _CancelledError

    provider = ScriptedProvider({"CriticVerdict": first_then_cancelled})
    with ResponseCache(enabled=True, directory=tmp_path / "critic-cache") as cache:
        client = Client(
            provider=provider,
            settings=settings.model_copy(update={"critic_trials": 3}),
            cache=cache,
            ledger=Ledger(),
            raise_if_cancelled=raise_if_cancelled,
        )
        with pytest.raises(_CancelledError):
            critique(
                client,
                markdown="# JANE\n\n## Summary\n\nBuilds data platforms.\n",
                citations="",
                inventory=inventory,
                offered=(),
                spec=spec_with("Kafka"),
                guardrails=guardrails,
            )

    assert len(provider.calls) == 1


def _critic_task(
    inventory: CvInventory,
    settings: Any,
    guardrails: Guardrails,
    directory: Path,
    citations: str,
    document: str = "resume",
    research: tuple[ResearchSource, ...] = (),
    offered: tuple[str, ...] = (),
) -> str:
    """Return the task text the critic request carried."""
    return _critic_request(
        inventory, settings, guardrails, directory, citations, document, research, offered
    ).task.text


def _critic_request(
    inventory: CvInventory,
    settings: Any,
    guardrails: Guardrails,
    directory: Path,
    citations: str,
    document: str = "resume",
    research: tuple[ResearchSource, ...] = (),
    offered: tuple[str, ...] = (),
) -> RequestPlan:
    """Return the request the critic sent on its only draw."""
    provider = ScriptedProvider({"CriticVerdict": lambda schema: _verdict().model_dump()})
    with ResponseCache(enabled=True, directory=directory) as cache:
        client = Client(
            provider=provider,
            settings=settings.model_copy(update={"critic_trials": 1}),
            cache=cache,
            ledger=Ledger(),
        )
        critique(
            client,
            markdown="# JANE\n\n## Summary\n\nBuilds data platforms.\n",
            citations=citations,
            inventory=inventory,
            offered=offered,
            spec=spec_with("Kafka"),
            guardrails=guardrails,
            document=document,
            research=research,
        )
    return provider.calls[0][1]


def test_the_critic_sees_an_unoffered_paper_only_in_the_record_not_in_its_task(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The whole record rides along, yet alignment is judged only against the offered ids."""
    paper = next(
        entry
        for entry in inventory.of_kind(EntryKind.PUBLICATION)
        if "Schema governance at scale" in entry.text
    )
    offered = tuple(entry.id for entry in inventory.of_kind(EntryKind.BULLET))

    request = _critic_request(inventory, settings, guardrails, tmp_path, "", offered=offered)

    assert request.evidence is not None
    assert request.task is not None
    assert paper.id in request.evidence.text
    assert paper.id not in request.task.text


def test_the_critic_reads_every_drafted_line_beside_the_text_of_the_entries_it_cites(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A line that widens its entry's scope is only visible with that entry's text beside it.

    Held against the record as one block, a line resting on an entry it does not
    cite looks supported, and a line crediting the candidate with a team's work
    sits far from the entry that says whose work it was.
    """
    first, second = inventory.of_kind(EntryKind.BULLET)[:2]
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(first.id,)),
        roles=(
            DraftedRole(
                role_key="acme",
                bullets=(
                    DraftedLine(
                        text="Ran the Kafka migration.",
                        source_ids=(second.id, first.id, second.id),
                    ),
                ),
            ),
        ),
        earlier_career=DraftedLine(text="Earlier roles in logistics."),
    )

    task = _critic_task(
        inventory,
        settings,
        guardrails,
        tmp_path / "critic-cache",
        line_citations(draft.lines(), inventory),
    )

    assert (
        data_block(
            "line_citations",
            f"Line 1, source_ids: {first.id}\nBuilds data platforms.\n{entry_line(first)}\n\n"
            f"Line 2, source_ids: {second.id}, {first.id}, {second.id}\n"
            f"Ran the Kafka migration.\n{entry_line(second)}\n{entry_line(first)}\n\n"
            "Line 3, source_ids: none\nEarlier roles in logistics.",
        )
        in task
    )


def test_the_document_the_critic_judges_for_readability_carries_no_citations(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Readability and ATS clarity are judged on the real document, not an annotated one."""
    cited = inventory.of_kind(EntryKind.BULLET)[0]
    draft = ResumeDraft(summary=DraftedLine(text="Builds data platforms.", source_ids=(cited.id,)))

    task = _critic_task(
        inventory,
        settings,
        guardrails,
        tmp_path / "critic-cache",
        line_citations(draft.lines(), inventory),
    )
    document = task.split("<draft>")[1].split("</draft>")[0]

    assert "<line_citations>" in task
    assert "source_ids" not in document
    assert cited.text not in document


def test_the_critic_reads_the_record_the_match_stage_sends_byte_for_byte(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A miscitation is only visible against entries the line did not cite.

    The record is the cached prefix, so a critic that marks it per document
    writes a fresh cache entry on every call instead of reading the match stage's.
    """
    from clearscout.application.tailoring.stages.match import match

    cited, *_, uncited = inventory.of_kind(EntryKind.BULLET)
    provider = ScriptedProvider(
        {
            "EvidenceMap": lambda schema: {"matches": []},
            "CriticVerdict": lambda schema: _verdict().model_dump(),
        }
    )
    with ResponseCache(enabled=True, directory=tmp_path / "cache") as cache:
        client = Client(
            provider=provider,
            settings=settings.model_copy(update={"critic_trials": 1}),
            cache=cache,
            ledger=Ledger(),
        )
        match(client, inventory, spec_with("Kafka"), guardrails)
        critique(
            client,
            markdown="# JANE\n\n## Summary\n\nBuilds data platforms.\n",
            citations="",
            inventory=inventory,
            offered=(cited.id,),
            spec=spec_with("Kafka"),
            guardrails=guardrails,
        )
    (_, matched), (_, critic) = provider.calls

    assert matched.evidence is not None and critic.evidence is not None
    assert critic.evidence.digest == matched.evidence.digest
    assert critic.evidence.text == record_block(inventory)
    assert entry_line(uncited) in critic.evidence.text
    assert uncited.text not in critic.task.text


def test_the_resume_and_letter_critics_share_one_cached_prefix(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Each document's critic would otherwise write its own copy of the record to the cache."""
    resume = _critic_request(inventory, settings, guardrails, tmp_path / "resume", "")
    letter = _critic_request(
        inventory, settings, guardrails, tmp_path / "letter", "", document="cover letter"
    )

    assert letter.cached_prefix == resume.cached_prefix
    assert "tailored resume " in resume.task.text
    assert "tailored cover letter " in letter.task.text


def test_the_critic_is_told_which_entries_the_document_may_draw_on(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Alignment judged against unoffered entries asks the redraft for evidence it cannot cite."""
    offered = build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings).all_ids()
    unoffered = next(entry.id for entry in inventory.entries if entry.id not in offered)

    task = _critic_task(
        inventory, settings, guardrails, tmp_path / "critic-cache", "", offered=offered
    )

    listed = task.split("<offered_evidence>")[1].split("</offered_evidence>")[0]
    assert listed.split() == list(offered)
    assert unoffered not in listed
    flat = " ".join(task.split())
    assert "record entries this resume may draw on" in flat
    assert "judge and name only the entries listed in the offered_evidence block." in flat


def test_the_critic_is_told_a_span_of_years_under_a_role_is_claimed_of_that_employer(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A career-wide span cited from the summary reads as tenure once it sits under one role."""
    task = _critic_task(inventory, settings, guardrails, tmp_path / "critic-cache", "")

    flat = " ".join(task.split())
    assert (
        "A resume line written under a role is a claim about that role, so a length of"
        " service it states is claimed at that employer, and an entry giving the span of"
        " the whole career does not carry it." in flat
    )


def test_the_critic_reads_a_letter_paragraph_under_its_own_citations(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The letter cites per paragraph, and the critic judges each one against what it cites."""
    cited = inventory.of_kind(EntryKind.BULLET)[1]
    letter = CoverLetter(
        greeting="Dear Hiring Manager,",
        paragraphs=(
            DraftedLine(text="I built the ingestion pipeline.", source_ids=(cited.id,)),
            DraftedLine(text="I would welcome a conversation."),
        ),
        closing="Sincerely,",
    )

    task = _critic_task(
        inventory,
        settings,
        guardrails,
        tmp_path / "critic-cache",
        line_citations(letter.paragraphs, inventory, "Paragraph"),
        document="cover letter",
    )

    assert (
        data_block(
            "line_citations",
            f"Paragraph 1, source_ids: {cited.id}\nI built the ingestion pipeline.\n"
            f"{entry_line(cited)}\n\n"
            "Paragraph 2, source_ids: none\nI would welcome a conversation.",
        )
        in task
    )


def test_a_letter_paragraph_citing_company_research_shows_that_research_once(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Company research sits in its own block, so the index names its identifier and no more."""
    posting = ResearchSource(
        id="company.posting",
        kind="posting",
        url="https://example.test/jobs/42",
        text="Globex moves regional freight schedules onto one streaming platform.",
        method="stored_posting_artifact",
    )
    letter = CoverLetter(
        greeting="Dear Hiring Manager,",
        paragraphs=(
            DraftedLine(
                text="Globex is moving its freight schedules onto one platform.",
                source_ids=(posting.id,),
                source_types=(SourceType.COMPANY,),
            ),
        ),
        closing="Sincerely,",
    )

    task = _critic_task(
        inventory,
        settings,
        guardrails,
        tmp_path / "critic-cache",
        line_citations(letter.paragraphs, inventory, "Paragraph"),
        document="cover letter",
        research=(posting,),
    )

    assert task.count(posting.text) == 1
    assert (
        data_block(
            "line_citations",
            "Paragraph 1, source_ids: company.posting\n"
            "Globex is moving its freight schedules onto one platform.",
        )
        in task
    )


def test_a_merged_verdict_scores_and_explains_itself_from_one_trial(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A score from one draw and an explanation from another would steer a redraft blind.

    The failure a person reads quotes the preservation number and the guidance
    together, so a rewrite driven by text belonging to a different draw is
    answering a complaint the failing score never made.
    """
    answers = [
        _verdict(preservation=5, guidance="keep the platform scope"),
        _verdict(preservation=2, guidance="the summary dropped the logistics work"),
        _verdict(preservation=3, guidance="the bullets lost their figures"),
    ]
    judgments = {(answer.preservation, answer.guidance) for answer in answers}

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert (verdict.preservation, verdict.guidance) in judgments


def test_a_failing_preservation_score_carries_the_guidance_of_the_trial_that_gave_it(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The redraft is told what to fix, and only the doubting trial knows what that is.

    The failure quotes the preservation number and the guidance in one sentence,
    so guidance from a trial that scored the draft as faithful would send the
    rewrite after a complaint nobody made.
    """
    doubt = "the summary dropped the logistics work"
    answers = [
        _verdict(preservation=5, guidance="keep the platform scope"),
        _verdict(preservation=5, guidance="tighten the opening line"),
        _verdict(preservation=2, guidance=doubt),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    finding = critic_finding(verdict)
    assert finding.outcome is Outcome.FAIL
    assert finding.detail == f"content preservation 2/5: {doubt}"


def test_a_failing_alignment_score_carries_the_guidance_of_the_draw_that_scored_it_lowest(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Every draw finds the draft faithful, so only an alignment draw explains the failure."""
    lowest = "lead with the Kafka migration"
    answers = [
        _verdict(alignment=4, guidance="keep the platform scope"),
        _verdict(alignment=2, guidance="the summary is generic"),
        _verdict(alignment=1, guidance=lowest),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert critic_finding(verdict).detail == f"quality below 3/5: alignment 2/5. {lowest}"


def test_letter_redraft_feedback_marks_guidance_evidence_the_letter_may_not_cite(
    inventory: CvInventory, settings
) -> None:
    """The critic reads the whole record, so its guidance can name evidence this writer lacks."""
    plan = build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings)
    citable = plan.drafted_ids()[0]
    outside = next(entry.id for entry in inventory.entries if entry.id not in plan.all_ids())
    copied = plan.education_ids[0]
    verdict = _verdict(
        alignment=2, guidance=f"Cite {outside}, {copied} and [{citable}] for the platform."
    )

    report = letter_with(inventory, settings, "The record fits this posting.")
    feedback = report.with_critic(verdict, LETTER_CRITIC).feedback()

    assert (
        f"Cite {outside} (not citable in this document), {copied} (not citable in this document)"
        f" and [{citable}] for"
    ) in feedback


def test_a_letter_advisory_marks_evidence_the_letter_may_not_cite(
    inventory: CvInventory, settings
) -> None:
    plan = build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings)
    outside = next(entry.id for entry in inventory.entries if entry.id not in plan.all_ids())
    verdict = _verdict(weaknesses=[f"{outside} would answer the platform ask."])

    report = letter_with(inventory, settings, "The record fits this posting.")
    critiqued = report.with_critic(verdict, LETTER_CRITIC)

    assert finding(critiqued, LETTER_CRITIC).advisories == (
        f"{outside} (not citable in this document) would answer the platform ask.",
    )


def _spread() -> list[CriticVerdict]:
    """Return three draws whose every axis disagrees, the harsh one in the middle."""
    return [
        _verdict(alignment=5, preservation=5, specificity=5, readability=5, ats_clarity=5),
        _verdict(alignment=1, preservation=2, specificity=1, readability=1, ats_clarity=1),
        _verdict(alignment=4, preservation=5, specificity=4, readability=4, ats_clarity=3),
    ]


def test_one_harsh_draw_decides_preservation_but_not_the_quality_axes(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """A lone draw doubting faithfulness fails the draft; a lone harsh opinion does not.

    A quality axis takes a central draw, because the first draw is one sample of
    the k that were paid for and the lowest of them is a minority reading.
    Preservation is the gate the anti-fabrication promise rests on, so one draw
    below its floor is enough.
    """
    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", _spread())

    assert verdict.scores() == {
        "alignment": 4,
        "preservation": 2,
        "specificity": 4,
        "readability": 4,
        "ats_clarity": 3,
        "human_voice": 4,
    }
    assert critic_finding(verdict).detail.startswith("content preservation 2/5")


def test_the_minimum_statistic_grades_every_quality_axis_on_the_harshest_draw(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The strict reading stays available to a person tuning their own runs."""
    verdict = _merged(
        inventory,
        settings,
        guardrails,
        tmp_path / "critic-cache",
        _spread(),
        CriticStatistic.MINIMUM,
    )

    assert verdict.scores() == {
        "alignment": 1,
        "preservation": 2,
        "specificity": 1,
        "readability": 1,
        "ats_clarity": 1,
        "human_voice": 4,
    }
    assert critic_finding(verdict).outcome is Outcome.FAIL


def test_every_sampled_draw_comes_back_beside_the_merged_verdict(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """The draws were paid for, so the spread behind a merged score stays readable."""
    answers = _spread()

    _, draws = _critiqued(
        inventory, settings, guardrails, tmp_path / "critic-cache", answers.copy()
    )

    # The later draws send one request together, so which answers which is not scripted.
    assert draws[0] == answers[0]
    assert sorted(draw.model_dump_json() for draw in draws) == sorted(
        answer.model_dump_json() for answer in answers
    )


def test_a_merged_doubt_counts_the_draws_that_raised_it(
    inventory: CvInventory, settings, guardrails: Guardrails, tmp_path: Path
) -> None:
    """Two draws agreeing on a doubt and one draw raising it alone read differently."""
    alone = _doubt("Cut recurring spend.", ClaimBoundary.PRODUCTION)
    answers = [
        _verdict(unsupported_claims=[_doubt()]),
        _verdict(unsupported_claims=[_doubt(), alone]),
        _verdict(),
    ]

    verdict = _merged(inventory, settings, guardrails, tmp_path / "critic-cache", answers)

    assert [doubt.corroboration for doubt in verdict.unsupported_claims] == [2, 1]


def test_the_provider_is_never_asked_how_many_draws_raised_a_doubt() -> None:
    """The count is merged here, and a schema field for it would ask the model to invent one."""
    schema = json_schema(CriticVerdict)

    assert "corroboration" not in json.dumps(schema)
    assert schema["$defs"]["DoubtedClaim"]["required"] == ["claim", "boundary"]


def test_the_review_document_shows_every_draw_behind_the_merged_scores() -> None:
    """A person reading one preservation number cannot see how the draws disagreed."""
    verification = Report(
        findings=(),
        coverage=KeywordCoverage(present=(), missing=()),
        critic=_verdict(alignment=4, unsupported_claims=[_doubt(corroboration=2)]),
        critic_draws=tuple(_spread()),
    )

    critic = report_section(rendered_report(verification=verification), "Critic")

    assert "| Draw | alignment | preservation |" in critic
    assert "| 2 | 1 | 2 | 1 | 1 | 1 | 4 |" in critic
    assert "- Led the platform team. (raised by 2 of 3 draws)" in critic


def test_the_critic_is_told_which_lines_it_may_not_judge(
    inventory: CvInventory, guardrails: Guardrails
) -> None:
    """The critic is shown the settings and master CV lines it must not judge.

    The critic sees the assembled resume, which carries a work authorization line
    from settings and the name, contact lines and role headers copied from the
    master CV. None of them is in the evidence inventory, so without this the
    critic reports them as unsupported claims that no redraft can change.
    """
    from clearscout.application.tailoring.stages.context import copied_text

    rules = guardrails.model_copy(
        update={
            "work_authorization": "New Zealand citizen and Australian permanent resident",
            "protected": ["Logistics Data Analyst - Acme Health"],
        }
    )
    asserted = copied_text(inventory, rules)
    assert "New Zealand citizen and Australian permanent resident" in asserted
    assert "Logistics Data Analyst - Acme Health" in asserted
    assert inventory.name in asserted
    for contact in inventory.contact_lines:
        assert contact in asserted
    copied = [
        value
        for role in inventory.roles
        for value in (role.organization, role.title, role.title_of_record, role.dates)
        if value
    ]
    assert copied
    for value in copied:
        assert value in asserted


def test_asserted_lines_are_listed_once_each(
    inventory: CvInventory, guardrails: Guardrails
) -> None:
    """A protected phrase that is also the work authorization must not repeat."""
    from clearscout.application.tailoring.stages.context import copied_text

    line = "New Zealand citizen and Australian permanent resident"
    rules = guardrails.model_copy(update={"work_authorization": line, "protected": [line, line]})
    assert copied_text(inventory, rules).count(line) == 1


def test_a_record_with_nothing_asserted_produces_nothing(
    inventory: CvInventory, guardrails: Guardrails
) -> None:
    """An empty block must not become a stray blank line in the prompt."""
    from clearscout.application.tailoring.stages.context import copied_text

    bare = inventory.model_copy(update={"name": "", "contact_lines": (), "roles": ()})
    empty = guardrails.model_copy(update={"protected": [], "work_authorization": None})
    assert copied_text(bare, empty) == ""


def test_a_role_location_the_assembler_copies_is_asserted_rather_than_judged(
    inventory: CvInventory, guardrails: Guardrails
) -> None:
    """A place name the program copies under a role heading is not the model's claim."""
    from clearscout.application.tailoring.stages.context import copied_text

    located = inventory.model_copy(
        update={
            "roles": tuple(
                role.model_copy(update={"location": "Wellington, New Zealand"})
                for role in inventory.roles
            )
        }
    )

    assert "Wellington, New Zealand" in copied_text(located, guardrails)


def test_a_forbidden_phrase_is_shown_to_the_model_that_will_be_failed_for_it() -> None:
    """The drafter is given the rules its draft is checked against.

    `_forbidden` and `_protected` are regular expressions run over the draft, so
    a drafter without them could only discover a forbidden phrase by being
    failed for it, at the cost of a second paid attempt.
    """
    rules = Guardrails(
        distinctions=["Spark is production; PySpark is familiarity."],
        forbidden=["RAG", "synergy"],
        protected=["Logistics Data Analyst - Acme Health"],
    )
    block = rules.prompt_block()
    assert "Spark is production" in block
    assert "RAG" in block and "synergy" in block
    assert "Logistics Data Analyst - Acme Health" in block
    assert "Never write any of these" in block
    assert "verbatim whenever you mention it" in block


def test_required_phrases_are_never_asked_of_the_drafter() -> None:
    """Required phrases are checked against program-copied text, so drafting never sees them."""
    block = Guardrails(required=["jane.doe@example.com"]).prompt_block()
    assert "jane.doe@example.com" not in block


def test_rules_with_nothing_configured_still_render_something_sane() -> None:
    block = Guardrails().prompt_block()
    assert "No additional distinction rules" in block
    assert "Never write" not in block


def test_a_hyphen_is_not_a_different_word() -> None:
    """A hyphenated "technical-lead" covers the keyword "technical lead".

    The token pattern keeps hyphens so that C++ and Neo4j-backed survive, which
    would otherwise make technical-lead a single token matching neither half.
    """
    from clearscout.application.tailoring.stages.jobspec import JobSpec
    from clearscout.application.tailoring.stages.verify import keyword_coverage

    document = "Held technical-lead responsibility through October 2025."
    spec = JobSpec(
        title="x",
        company=None,
        seniority=None,
        location=None,
        requirements=(),
        keywords=("technical lead", "technical-lead"),
        hard_gates=(),
        origin="t",
    )
    coverage = keyword_coverage(document, spec)
    assert coverage.missing == ()
    assert len(coverage.present) == 2


def test_splitting_hyphens_does_not_break_the_tokens_hyphens_protect() -> None:
    """C++, F#, .NET and Neo4j are why the pattern keeps punctuation at all."""
    from clearscout.application.tailoring.stages.jobspec import JobSpec
    from clearscout.application.tailoring.stages.verify import keyword_coverage

    document = "Built in C++ and F# against Neo4j-backed services on .NET."
    spec = JobSpec(
        title="x",
        company=None,
        seniority=None,
        location=None,
        requirements=(),
        keywords=("C++", "F#", "Neo4j", ".NET", "Neo4j-backed"),
        hard_gates=(),
        origin="t",
    )
    assert keyword_coverage(document, spec).missing == ()


def test_a_genuinely_absent_phrase_is_still_reported_absent() -> None:
    """Hyphen splitting must not turn the check into one that always passes."""
    from clearscout.application.tailoring.stages.jobspec import JobSpec
    from clearscout.application.tailoring.stages.verify import keyword_coverage

    spec = JobSpec(
        title="x",
        company=None,
        seniority=None,
        location=None,
        requirements=(),
        keywords=("anomaly detection", "real-world evidence"),
        hard_gates=(),
        origin="t",
    )
    assert len(keyword_coverage("Held technical-lead responsibility.", spec).missing) == 2


def letter_with(inventory, settings, text: str):
    """Return the report for a one-paragraph letter citing real evidence."""
    from clearscout.application.tailoring.stages.draft import CoverLetter, DraftedLine
    from clearscout.application.tailoring.stages.verify import check_letter

    entry_id = inventory.entries[0].id
    return check_letter(
        letter=CoverLetter(
            greeting="Dear hiring team,",
            paragraphs=(DraftedLine(text=text, source_ids=(entry_id,)),),
            closing="Available immediately.",
        ),
        plan=build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=_guardrails(),
    )


def test_a_letter_within_the_stated_limit_passes(inventory, settings) -> None:
    """The prompt states a length, so a check counts it rather than trusting it."""
    from clearscout.application.tailoring.stages.draft import MAXIMUM_LETTER_WORDS

    report = letter_with(inventory, settings, "The record fits this posting.")
    finding = next(item for item in report.findings if item.check == "letter length")
    assert finding.outcome is Outcome.PASS
    assert str(MAXIMUM_LETTER_WORDS) in finding.detail


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("I licensed drug-information products to regional hospital networks.", False),
        ("I am a licensed pharmacist with hospital reporting work behind me.", True),
        ("I am licensed to practice pharmacy and reported for hospital networks.", True),
    ],
)
def test_licensed_is_a_credential_only_beside_a_profession_or_a_licence(
    inventory, settings, text: str, flagged: bool
) -> None:
    """Product licensing is ordinary work; a licensed profession is a credential to evidence."""
    review = finding(letter_with(inventory, settings, text), "factual review")

    assert ("review unsupported credential" in review.detail) is flagged


def test_a_letter_may_cite_declared_company_research_but_not_an_unknown_source(
    inventory, settings
) -> None:
    from clearscout.application.tailoring.stages.draft import CoverLetter, DraftedLine
    from clearscout.application.tailoring.stages.verify import check_letter

    plan = build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings)

    def outcome(source_id: str, allowed: tuple[str, ...]) -> Outcome:
        report = check_letter(
            letter=CoverLetter(
                greeting="Dear hiring team,",
                paragraphs=(
                    DraftedLine(
                        text="The employer builds public tools.",
                        source_ids=(source_id,),
                        source_types=(SourceType.COMPANY,),
                    ),
                ),
                closing="Available immediately.",
            ),
            plan=plan,
            inventory=inventory,
            guardrails=_guardrails(),
            research_source_ids=allowed,
        )
        return next(item.outcome for item in report.findings if item.check == "letter provenance")

    assert outcome("company.about", ("company.about",)) is Outcome.PASS
    assert outcome("invented-source", ("company.about",)) is Outcome.FAIL


def test_a_cover_letter_cannot_mix_candidate_and_company_claims(inventory, settings) -> None:
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter

    plan = build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings)
    entry_id = inventory.entries[0].id
    company_source = "company.about"

    report = check_letter(
        letter=CoverLetter(
            greeting="Dear hiring team,",
            paragraphs=(
                DraftedLine(
                    text="Built this platform and the employer ships open APIs.",
                    source_ids=(entry_id, company_source),
                    source_types=(SourceType.CANDIDATE, SourceType.COMPANY),
                ),
            ),
            closing="Available immediately.",
        ),
        plan=plan,
        inventory=inventory,
        guardrails=_guardrails(),
        research_source_ids=(company_source,),
    )

    provenance = next(item for item in report.findings if item.check == "letter provenance")
    assert provenance.outcome is Outcome.FAIL


def test_letter_feedback_quotes_every_uncited_paragraph_and_every_untraced_figure(
    inventory, settings
) -> None:
    """A redraft can only fix what the feedback shows it, so no offender is cut to a prefix."""
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter

    cited = inventory.entries[0].id
    uncited = (
        "The platform work described here carried the reporting load for every regional office.",
        "Later work moved the same pipelines onto a streaming tier the analysts relied on.",
    )
    figures = (
        "The streaming tier moved every nightly load onto the new cluster within 9731 hours.",
        "It also cut the failure rate of the downstream reports by 4177 percent.",
    )
    report = check_letter(
        letter=CoverLetter(
            greeting="Dear hiring team,",
            paragraphs=(
                *(DraftedLine(text=text) for text in uncited),
                DraftedLine(text=" ".join(figures), source_ids=(cited,)),
            ),
            closing="Sincerely,",
        ),
        plan=build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=_guardrails(),
    )

    provenance = next(item for item in report.findings if item.check == "letter provenance")
    numbers = next(item for item in report.findings if item.check == "letter numbers")
    assert all(repr(text) in provenance.detail for text in uncited)
    assert all(repr(text) in numbers.detail for text in figures)


def test_a_letter_over_the_stated_limit_is_sent_back(inventory, settings) -> None:
    from clearscout.application.tailoring.stages.draft import MAXIMUM_LETTER_WORDS

    long_letter = " ".join(["word"] * (MAXIMUM_LETTER_WORDS + 1))
    report = letter_with(inventory, settings, long_letter)
    finding = next(item for item in report.findings if item.check == "letter length")
    assert finding.outcome is Outcome.FAIL
    assert finding.redraftable


DOCUMENT_NOUNS = (
    "the posting",
    "the job posting",
    "this posting",
    "the job description",
    "the role description",
    "the listing",
    "the job listing",
    "the company research",
)
SOURCE_NOUNS = (*DOCUMENT_NOUNS, "the research")
SOURCE_SUBJECT_VERBS = (
    "emphasizes",
    "also emphasizes",
    "notes",
    "highlights",
    "asks for",
    "describes",
    "mentions",
    "points to",
    "focuses on",
)
SOURCE_FRAMES = ("As {} notes,", "As {} says,", "According to {},", "Per {},")
ROLE_DEMANDS = tuple(
    f"{head} {verb} Kafka ingestion and schema compatibility."
    for head in ("This role", "The role", "This position", "The opening")
    for verb in ("calls for", "asks for", "requires", "seeks", "is looking for")
)
SUPPLY_TALK = (
    "No company research was provided, so this paragraph keeps to the opening.",
    "No research was supplied beyond the opening itself.",
    "Company research was not supplied for Adatum.",
    "Little information was provided about the team.",
    "The information provided says Adatum serves regional carriers.",
    "Beyond the details supplied, Adatum serves regional carriers.",
    "Beyond what was supplied, Adatum serves regional carriers.",
    "No context was given about the team.",
    "No sources were provided for Adatum.",
    "Limited materials were supplied for this letter.",
    "Scant research has been provided on the team.",
    "Details of the team weren't provided.",
    "The research supplied notes that Adatum serves regional carriers.",
)
SOURCE_TALK = [
    *(
        f"{noun[0].upper()}{noun[1:]} {verb} schema compatibility for every consumer."
        for noun in DOCUMENT_NOUNS
        for verb in SOURCE_SUBJECT_VERBS
    ),
    *(
        f"{frame.format(noun)} Adatum runs ingestion for 40 carriers."
        for frame in SOURCE_FRAMES
        for noun in SOURCE_NOUNS
    ),
    "Adatum, as its posting notes, runs ingestion for 40 carriers.",
    "Adatum's posting stresses schema compatibility.",
    "Adatum's Data Engineer posting places schema work ahead of dashboards.",
    "The Senior Data Engineer listing stresses schema compatibility.",
    "The posting also identifies clearance as a must.",
    "The team named in the job description runs ingestion for 40 carriers.",
    *ROLE_DEMANDS,
    *SUPPLY_TALK,
]


def _source_talk(inventory, settings, text: str) -> Finding:
    return finding(letter_with(inventory, settings, text), "letter source talk")


@pytest.mark.parametrize("sentence", SOURCE_TALK)
def test_a_letter_sentence_about_the_posting_or_research_is_sent_back(
    inventory, settings, sentence: str
) -> None:
    """Production always supplies the posting, so naming a source is talk about the document."""
    talk = _source_talk(inventory, settings, f"Adatum serves regional carriers. {sentence}")

    assert talk.outcome is Outcome.FAIL
    assert talk.redraftable
    assert repr(sentence) in talk.detail


@pytest.mark.parametrize(
    "sentence",
    [
        "Adatum sells freight visibility software to regional carriers.",
        "The role owns the nightly load and the schema registry behind it.",
        "The opening sits on a small team that owns ingestion end to end.",
        "My research on hospital billing codes fed the claims model.",
        "Research teams at the hospital read the lineage reports.",
        "The record fits this posting.",
        "I provided monthly extracts to hospital auditors.",
        "The listing service I built answered 40 requests a second.",
        "I wrote the posting rules that routed each ledger entry.",
        "Data was provided to auditors within a day of each request.",
        "Its teams describe each schema change before release.",
        "Its research centers on antibody discovery.",
        "Northbridge's research focuses on rare kidney disease.",
        "The research arm runs trials in 12 countries.",
        "Contoso makes lab results and dosing information available to clinicians at the bedside.",
        "I made formulary information available to every pharmacist through one search.",
        "I kept dosing details available during the migration.",
        "The information provided by sensors drove the alerts.",
        "Materials provided by suppliers were reconciled nightly.",
        "Sources available to the team grew from two to twelve.",
        "I wrote the data sources provided to analysts each morning.",
        "Details were provided to auditors within a day of each request.",
        "No information was given to nurses before the alert went live.",
        "The real estate listing shows live availability to renters.",
        "The quarterly listing review places each vendor in a tier.",
        "My team identifies drift in each nightly load.",
    ],
)
def test_a_letter_sentence_about_the_employer_or_the_work_is_not_source_talk(
    inventory, settings, sentence: str
) -> None:
    assert _source_talk(inventory, settings, sentence).outcome is Outcome.PASS


def _letter_report(
    inventory,
    settings,
    *,
    greeting: str,
    closing: str,
    forbidden=(),
    body: str = "The record fits this posting.",
):
    """Report for a letter whose greeting, body, and sign-off are under test."""
    from clearscout.application.tailoring.stages.draft import CoverLetter, DraftedLine
    from clearscout.application.tailoring.stages.verify import check_letter

    return check_letter(
        letter=CoverLetter(
            greeting=greeting,
            paragraphs=(DraftedLine(text=body, source_ids=(inventory.entries[0].id,)),),
            closing=closing,
        ),
        plan=build_plan(inventory, spec_with("Kafka"), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=Guardrails(forbidden=list(forbidden)) if forbidden else _guardrails(),
    )


def test_guardrails_read_the_letter_body_and_not_the_lines_the_program_writes(
    inventory, settings
) -> None:
    """The greeting and sign-off come from stored names, which no redraft can change."""
    written = _letter_report(
        inventory,
        settings,
        greeting="Dear Synergy Labs hiring team,",
        closing="Sincerely,\n\nSynergy Example",
        forbidden=("Synergy",),
    )
    drafted = _letter_report(
        inventory,
        settings,
        greeting="Dear hiring team,",
        closing="Sincerely,",
        forbidden=("fits",),
    )
    assert outcome(written, "forbidden strings") is Outcome.PASS
    assert outcome(drafted, "forbidden strings") is Outcome.FAIL


def test_a_letter_fails_a_forbidden_phrase_spelled_in_another_case(inventory, settings) -> None:
    report = _letter_report(
        inventory,
        settings,
        greeting="Dear hiring team,",
        closing="Sincerely,",
        forbidden=("project halyard",),
        body="Project Halyard shaped the delivery work.",
    )
    assert outcome(report, "forbidden strings") is Outcome.FAIL


def test_a_forbidden_phrase_does_not_fire_inside_a_longer_word(inventory, settings) -> None:
    report = _letter_report(
        inventory,
        settings,
        greeting="Dear hiring team,",
        closing="Sincerely,",
        forbidden=("RAG",),
        body="Owned the storage tier for every feed.",
    )
    assert outcome(report, "forbidden strings") is Outcome.PASS


def test_source_answerability_survives_an_omitted_plan_entry(inventory, settings):
    from clearscout.application.tailoring.stages.select import Plan

    plan = Plan(requirements_for={inventory.entries[0].id: ("r01",)})
    report = build(inventory, settings, EMPTY_RESUME, "# J\n", keywords=(), plan=plan)
    coverage = finding(report, "must-have coverage")
    assert coverage.outcome is Outcome.WARN
    assert "not selected" in coverage.detail


def test_empty_and_signature_only_letters_fail_as_incomplete(inventory, settings):
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter

    plan = build_plan(inventory, spec_with(), EvidenceMap(), settings)
    for paragraphs in (
        (),
        (DraftedLine(text="   "),),
        (DraftedLine(text="Sincerely, Jane Example"),),
    ):
        report = check_letter(
            letter=CoverLetter(
                greeting="Dear Hiring Team,", closing="Sincerely,", paragraphs=paragraphs
            ),
            plan=plan,
            inventory=inventory,
            guardrails=Guardrails(),
        )
        assert outcome(report, "letter completeness") is Outcome.FAIL


def test_company_numbers_are_checked_against_the_cited_company_text(inventory, settings):
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource

    source = ResearchSource(
        id="company.about",
        kind="about",
        url="https://company.example/about",
        text="Our team serves 800 hospitals.",
        method="scripted",
    )
    plan = build_plan(inventory, spec_with(), EvidenceMap(), settings)
    for number, expected in ((800, Outcome.PASS), (900, Outcome.FAIL)):
        report = check_letter(
            letter=CoverLetter(
                greeting="Dear Hiring Team,",
                closing="Sincerely,",
                paragraphs=(
                    DraftedLine(
                        text=f"Your team serves {number} hospitals.",
                        source_ids=(source.id,),
                        source_types=(SourceType.COMPANY,),
                    ),
                ),
            ),
            plan=plan,
            inventory=inventory,
            guardrails=Guardrails(),
            research=(source,),
        )
        assert outcome(report, "letter numbers") is expected


def test_a_valid_company_id_does_not_establish_support_for_an_unrelated_claim(inventory, settings):
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource

    source = ResearchSource(
        id="company.about",
        kind="about",
        url="https://company.example/about",
        text="Our team serves hospitals.",
        method="scripted",
    )
    plan = build_plan(inventory, spec_with(), EvidenceMap(), settings)
    report = check_letter(
        letter=CoverLetter(
            greeting="Dear Hiring Team,",
            closing="Sincerely,",
            paragraphs=(
                DraftedLine(
                    text="Your organization manufactures aerospace engines.",
                    source_ids=(source.id,),
                    source_types=(SourceType.COMPANY,),
                ),
            ),
        ),
        plan=plan,
        inventory=inventory,
        guardrails=Guardrails(),
        research=(source,),
    )
    assert outcome(report, "factual review") is Outcome.WARN
    assert "unrelated" in next(
        item.detail for item in report.findings if item.check == "factual review"
    )


def test_company_evidence_cannot_supply_a_candidate_achievement(inventory, settings):
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource

    source = ResearchSource(
        id="company.about",
        kind="about",
        url="https://company.example/about",
        text="Our team serves 800 hospitals.",
        method="scripted",
    )
    plan = build_plan(inventory, spec_with(), EvidenceMap(), settings)
    claims = (
        "I led delivery of the regional reporting platform to 800 hospitals across the network.",
        "That rollout was my own design, from the first pilot site to the last regional office.",
    )
    report = check_letter(
        letter=CoverLetter(
            greeting="Dear Hiring Team,",
            closing="Sincerely,",
            paragraphs=(
                DraftedLine(
                    text=f"{claims[0]} Your team serves 800 hospitals. {claims[1]}",
                    source_ids=(source.id,),
                    source_types=(SourceType.COMPANY,),
                ),
            ),
        ),
        plan=plan,
        inventory=inventory,
        guardrails=Guardrails(),
        research=(source,),
    )
    ownership = next(item for item in report.findings if item.check == "claim ownership")
    assert ownership.outcome is Outcome.FAIL
    assert all(repr(claim) in ownership.detail for claim in claims)
    assert "Your team serves" not in ownership.detail


def _company_source(kind: ResearchKind, text: str) -> ResearchSource:
    return ResearchSource(
        id=f"company.{kind}",
        kind=kind,
        url=f"https://globex.example/{kind}",
        text=text,
        method="scripted",
    )


def _company_paragraph_report(
    inventory,
    settings,
    text: str,
    research: tuple[ResearchSource, ...] = (
        _company_source("about", "Globex routes freight for 800 US hospitals across the Midwest."),
    ),
    cited: tuple[str, ...] = ("company.about",),
) -> Report:
    from clearscout.application.tailoring.stages.verify import check_letter

    return check_letter(
        letter=CoverLetter(
            greeting="Dear Hiring Team,",
            closing="Sincerely,",
            paragraphs=(
                DraftedLine(
                    text=text,
                    source_ids=cited,
                    source_types=tuple(SourceType.COMPANY for _ in cited),
                ),
            ),
        ),
        plan=build_plan(inventory, spec_with(), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=Guardrails(),
        research=research,
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Freight routing for hospitals is the problem that draws me to this team.",
        "My interest is the freight routing Globex runs for hospitals.",
        "Globex routes freight for hospitals, a problem worth taking on myself.",
        "Globex routes freight for hospitals, and we share that focus.",
        "Globex routes freight for hospitals, which gives us common ground.",
        "Globex routes freight for hospitals, much as our last team did.",
        "Globex routes freight for hospitals, a goal much like ours.",
    ],
)
def test_any_first_person_word_in_a_company_paragraph_is_a_candidate_claim(
    inventory, settings, sentence: str
) -> None:
    report = _company_paragraph_report(inventory, settings, sentence)

    ownership = finding(report, "claim ownership")
    assert ownership.outcome is Outcome.FAIL
    assert repr(sentence) in ownership.detail


def test_a_country_abbreviation_in_a_company_paragraph_is_not_first_person(
    inventory, settings
) -> None:
    report = _company_paragraph_report(
        inventory, settings, "Globex routes freight for 800 US hospitals across the Midwest."
    )

    assert outcome(report, "claim ownership") is Outcome.PASS


COMPANY_ABOUT = _company_source(
    "about",
    "Last year the company rebuilt its freight routing network for rural hospitals. "
    "The company helps rural hospitals schedule freight pickups across three regional networks. "
    "It sells inbound freight routing, loading dock scheduling, and carrier invoice billing "
    "to clinics. Its products are Route Planner, Dock Scheduler, and Carrier Ledger for "
    "regional clinics and rural hospitals.",
)
COMPANY_POSTING = _company_source(
    "posting", "You will join a team that rebuilt the claims intake pipeline for regional payers."
)


@pytest.mark.parametrize(
    ("sentence", "cited", "expected"),
    [
        (
            "The company rebuilt its freight routing network for rural hospitals and clinics.",
            ("company.about",),
            Outcome.WARN,
        ),
        (
            "Globex helps rural hospitals schedule freight pickups across three regional hubs.",
            ("company.about",),
            Outcome.WARN,
        ),
        (
            "The company renewed how freight reaches rural hospitals and clinics.",
            ("company.about",),
            Outcome.PASS,
        ),
        (
            "Globex offers inbound freight routing, loading dock scheduling, and carrier "
            "invoice billing for regional hospitals.",
            ("company.about",),
            Outcome.WARN,
        ),
        (
            "Globex builds Route Planner, Dock Scheduler, and Carrier Ledger for regional "
            "clinics and rural hospitals.",
            ("company.about",),
            Outcome.PASS,
        ),
        (
            "The team rebuilt the claims intake pipeline for regional payers last spring.",
            ("company.posting",),
            Outcome.WARN,
        ),
        (
            "The team rebuilt the claims intake pipeline for regional payers last spring.",
            ("company.about",),
            Outcome.PASS,
        ),
    ],
)
def test_a_company_sentence_copying_a_source_its_paragraph_cites_is_an_echo(
    inventory, settings, sentence: str, cited: tuple[str, ...], expected: Outcome
) -> None:
    report = _company_paragraph_report(
        inventory, settings, sentence, research=(COMPANY_ABOUT, COMPANY_POSTING), cited=cited
    )

    assert outcome(report, "letter echo") is expected


def test_context_only_misses_warn_but_mixed_slots_fail(inventory, settings):
    from clearscout.application.tailoring.stages.select import Plan

    cases: tuple[tuple[dict[str, tuple[str, ...]], Outcome], ...] = (
        ({}, Outcome.WARN),
        ({"role": ("bullet",)}, Outcome.FAIL),
    )
    for bullets, expected in cases:
        plan = Plan(
            skill_ids=("skill",),
            role_bullets=bullets,
            requirements_for={"skill": ("r01",), "bullet": ("r01",)},
        )
        missing = build(inventory, settings, EMPTY_RESUME, "# J\n", keywords=(), plan=plan)
        assert outcome(missing, "must-have coverage") is expected
        answered = build(
            inventory,
            settings,
            ResumeDraft(summary=DraftedLine(text="Supported wording", source_ids=("skill",))),
            "# J\n",
            keywords=(),
            plan=plan,
        )
        assert outcome(answered, "must-have coverage") is Outcome.PASS


@pytest.mark.parametrize("section", ["education_ids", "publication_ids"])
def test_a_must_have_answered_by_a_copied_section_needs_no_drafted_line(
    inventory, settings, section: str
) -> None:
    """The program copies education and publications onto the page, so no line can cite them."""
    from clearscout.application.tailoring.stages.select import Plan

    plan = Plan.model_validate({section: ("edu",), "requirements_for": {"edu": ("r01",)}})
    report = build(inventory, settings, EMPTY_RESUME, "# J\n", keywords=(), plan=plan)

    assert outcome(report, "must-have coverage") is Outcome.PASS


def test_one_citation_may_answer_two_must_haves_without_duplicate_prose(inventory, settings):
    from clearscout.application.tailoring.stages.select import Plan

    plan = Plan(summary_ids=("shared",), requirements_for={"shared": ("r01", "r02")})
    target = spec_with().model_copy(
        update={
            "requirements": (
                Requirement(id="r01", text="Architecture", importance=Importance.MUST_HAVE),
                Requirement(id="r02", text="Delivery", importance=Importance.MUST_HAVE),
            )
        }
    )
    report = build(
        inventory,
        settings,
        ResumeDraft(
            summary=DraftedLine(text="Led architecture and delivery.", source_ids=("shared",))
        ),
        "# J\n",
        plan=plan,
        spec=target,
    )
    coverage = finding(report, "must-have coverage")
    assert coverage.outcome is Outcome.PASS
    assert "all 2" in coverage.detail


def test_factual_review_flags_removed_limits_and_new_credentials(inventory, settings):
    entry = inventory.entries[0].model_copy(
        update={"text": "Assisted architecture delivery without managing people."}
    )
    record = inventory.model_copy(update={"entries": (entry,)})
    for text, signal in (
        ("Managed architecture and people.", "ownership/qualification"),
        ("Certified architecture professional with a doctorate.", "credential"),
    ):
        report = build(
            record,
            settings,
            ResumeDraft(summary=DraftedLine(text=text, source_ids=(entry.id,))),
            "# J\n",
        )
        review = finding(report, "factual review")
        assert review.outcome is Outcome.WARN
        assert signal in review.detail
    paraphrase = build(
        record,
        settings,
        ResumeDraft(
            summary=DraftedLine(
                text="Assisted delivery of architecture work without people management.",
                source_ids=(entry.id,),
            )
        ),
        "# J\n",
    )
    assert "omitted" not in finding(paraphrase, "factual review").detail


def test_support_and_exclusivity_words_are_not_qualification_limits(inventory, settings):
    """Naming support or an exclusive scope states no limit on what the candidate owned."""
    entry = inventory.entries[0].model_copy(
        update={"text": "Supported the claims migration for the only regional team."}
    )
    record = inventory.model_copy(update={"entries": (entry,)})
    report = build(
        record,
        settings,
        ResumeDraft(
            summary=DraftedLine(
                text="Migrated claims for the regional team.", source_ids=(entry.id,)
            )
        ),
        "# J\n",
    )
    review = finding(report, "factual review")
    assert "omitted" not in review.detail
    assert "Human factual review is required." in review.detail


@pytest.mark.parametrize(
    "wording", ["Helped with", "Contributed to", "Worked on", "Participated in"]
)
def test_a_line_dropping_participation_wording_is_flagged_and_one_keeping_it_is_not(
    inventory, settings, wording
):
    """Taking part in the work is a limit on ownership, so a line may not drop it."""
    entry = inventory.entries[0].model_copy(
        update={"text": f"{wording} production readiness on the payments platform."}
    )
    record = inventory.model_copy(update={"entries": (entry,)})

    def review(text: str) -> str:
        draft = ResumeDraft(summary=DraftedLine(text=text, source_ids=(entry.id,)))
        return finding(build(record, settings, draft, "# J\n"), "factual review").detail

    dropped = "Improved production readiness of payment services."
    assert f"review omitted ownership/qualification limit: {dropped}" in review(dropped)
    kept = f"{wording} production readiness of payment services."
    assert "omitted" not in review(kept)


# Evidence, the drafted line, and whether the line dropped a limit on the applicant's part.
QUALIFIER_CORPUS = [
    (
        "Migrated the billing service to the new cluster without downtime.",
        "Migrated the billing service to the new cluster with zero interruption.",
        False,
    ),
    (
        "Not only cut hosting costs but also improved release reliability.",
        "Cut hosting costs and improved release reliability.",
        False,
    ),
    (
        "Reconciled ledger accounts that did not balance at month end.",
        "Reconciled unbalanced ledger accounts at month end.",
        False,
    ),
    (
        "Resolved checkout outages without escalating to the vendor.",
        "Resolved checkout outages in-house.",
        False,
    ),
    (
        "Was not only responsible for the rollout but also trained support staff.",
        "Owned the rollout and trained support staff.",
        False,
    ),
    (
        "Coordinated the vendor rollout without managing the engineers.",
        "Coordinated the vendor rollout without managing engineers.",
        False,
    ),
    (
        "Assisted architecture delivery without managing people.",
        "Managed architecture and people.",
        True,
    ),
    (
        "Coordinated the vendor rollout without managing the engineers.",
        "Managed the vendor rollout engineers.",
        True,
    ),
    (
        "Advised on the data platform but was not responsible for delivery.",
        "Delivered the data platform.",
        True,
    ),
    (
        "Influenced roadmap priorities without formal authority over the teams.",
        "Set roadmap priorities for the teams.",
        True,
    ),
    ("Contributed to the payments migration.", "Migrated payments to the new processor.", True),
]


@pytest.mark.parametrize(("evidence", "text", "dropped"), QUALIFIER_CORPUS)
def test_only_a_limit_on_the_applicants_part_is_a_qualifier_a_line_may_not_drop(
    inventory, settings, evidence: str, text: str, dropped: bool
):
    """A negation about the work itself, such as "without downtime", limits nobody's part."""
    record, cited = evidence_record(inventory, evidence)
    draft = ResumeDraft(summary=DraftedLine(text=text, source_ids=(cited,)))
    detail = finding(build(record, settings, draft, "# J\n"), "factual review").detail

    assert ("ownership/qualification limit" in detail) is dropped


def test_a_qualifier_in_company_research_is_not_an_ownership_risk(inventory, settings):
    """Research describes the employer, so nothing in it states a limit on the candidate."""
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource

    source = ResearchSource(
        id="company.about",
        kind="about",
        url="https://acme.example/about",
        text="Acme is a remote-only company and runs without a central office.",
        method="scripted",
    )
    report = check_letter(
        letter=CoverLetter(
            greeting="Dear Hiring Team,",
            closing="Sincerely,",
            paragraphs=(
                DraftedLine(
                    text="Acme runs a remote company across regions.",
                    source_ids=(source.id,),
                    source_types=(SourceType.COMPANY,),
                ),
            ),
        ),
        plan=build_plan(inventory, spec_with(), EvidenceMap(), settings),
        inventory=inventory,
        guardrails=Guardrails(),
        research=(source,),
    )
    detail = next(item.detail for item in report.findings if item.check == "factual review")
    assert "ownership/qualification limit" not in detail


def test_summary_phrase_reuse_is_distinct_from_factual_review(inventory, settings):
    from clearscout.application.tailoring.stages.draft import DraftedRole

    summary = "Turned customer discovery into roadmap priorities and delivery plans."
    repeated = ResumeDraft(
        summary=DraftedLine(text=summary),
        roles=(DraftedRole(role_key="role", bullets=(DraftedLine(text=summary),)),),
    )
    assert outcome(build(inventory, settings, repeated, "# J\n"), "repeated phrasing") is (
        Outcome.FAIL
    )
    changed = repeated.model_copy(
        update={
            "summary": DraftedLine(text="Built a product practice grounded in customer research.")
        }
    )
    assert outcome(build(inventory, settings, changed, "# J\n"), "repeated phrasing") is (
        Outcome.PASS
    )


def _echo_report(
    inventory,
    settings,
    sentence: str,
    evidence: str = (
        "Rebuilt the ingestion tier on a streaming platform and cut end-to-end latency for all."
    ),
    details: tuple[str, ...] = (),
) -> Report:
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter

    entry = inventory.entries[0].model_copy(update={"text": evidence, "details": details})
    record = inventory.model_copy(update={"entries": (entry, *inventory.entries[1:])})
    return check_letter(
        letter=CoverLetter(
            greeting="Dear hiring team,",
            paragraphs=(
                DraftedLine(
                    text=f"The role centers on streaming. {sentence}", source_ids=(entry.id,)
                ),
            ),
            closing="Sincerely,",
        ),
        plan=build_plan(record, spec_with("Kafka"), EvidenceMap(), settings),
        inventory=record,
        guardrails=_guardrails(),
    )


def test_a_letter_sentence_repeating_eight_words_of_its_cited_evidence_is_a_warning(
    inventory, settings
) -> None:
    echoed = _echo_report(
        inventory,
        settings,
        "I rebuilt the ingestion tier on a streaming platform and cut end-to-end latency.",
    )
    finding = next(item for item in echoed.findings if item.check == "letter echo")
    assert finding.outcome is Outcome.WARN
    assert not finding.redraftable
    assert echoed.passed
    assert (
        "'I rebuilt the ingestion tier on a streaming platform and cut end-to-end latency.'"
        in finding.detail
    )
    assert (
        "copies 'rebuilt the ingestion tier on a streaming platform and cut end-to-end latency.'"
        in finding.detail
    )

    seven = _echo_report(
        inventory,
        settings,
        "Rebuilding the ingestion tier on a streaming platform taught me the contracts.",
    )
    assert outcome(seven, "letter echo") is Outcome.PASS


def test_a_letter_sentence_repeating_a_cited_detail_is_a_warning(inventory, settings) -> None:
    report = _echo_report(
        inventory,
        settings,
        "I rebuilt the ingestion tier on a streaming platform and cut end-to-end latency.",
        evidence="Owned the streaming program for the platform team.",
        details=("Rebuilt the ingestion tier on a streaming platform and cut end-to-end latency.",),
    )

    assert outcome(report, "letter echo") is Outcome.WARN


def test_a_detail_copied_under_a_parent_with_no_final_period_is_a_warning(
    inventory, settings
) -> None:
    report = _echo_report(
        inventory,
        settings,
        "Last year I rebuilt the ingestion tier on a streaming platform.",
        evidence="Owned the streaming program for the platform team",
        details=("Rebuilt the ingestion tier on a streaming platform.",),
    )

    assert outcome(report, "letter echo") is Outcome.WARN


def test_a_cited_id_reads_as_its_entry_with_details() -> None:
    from tests.support.resumes import DETAIL, detailed_record

    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]

    assert DETAIL in id_texts(spec_with("Kafka"), record)[parent.id]


def test_a_copied_run_broken_by_a_figure_and_a_name_is_not_one_long_run(
    inventory, settings
) -> None:
    """A skipped figure or name separates its neighbours, so neither side reaches the threshold."""
    report = _echo_report(
        inventory,
        settings,
        "I shipped the order routing platform to 950 Globex Retail stores across the region.",
        evidence="Shipped the order routing platform to 800 Globex Retail stores across a region.",
    )

    assert outcome(report, "letter echo") is Outcome.PASS


def test_a_faithful_restatement_around_copied_figures_is_not_an_echo(inventory, settings) -> None:
    """The numbers check requires the figures verbatim, so the glue between them is not a copy."""
    report = _echo_report(
        inventory,
        settings,
        "I brought latency from 40 minutes to 90 seconds across 212 source feeds.",
        evidence="Reduced alert latency from 40 minutes to 90 seconds across 212 source feeds.",
    )

    assert outcome(report, "letter numbers") is Outcome.PASS
    assert outcome(report, "letter echo") is Outcome.PASS


@pytest.mark.parametrize(
    ("sentence", "evidence"),
    [
        (
            "My feed, supporting more than 400 retailers and 90 carriers, refreshed stock "
            "counts every night.",
            "Built the catalog feed supporting more than 400 retailers and 90 carriers "
            "with refreshed stock counts posted nightly.",
        ),
        (
            "I maintained SKU-level UPC crosswalks covering roughly 4,800 GTIN codes for returns.",
            "Maintained SKU-level UPC crosswalks covering roughly 4,800 GTIN codes across the "
            "returns catalog.",
        ),
        (
            "At Northwind I cut label reprints by 30 percent after moving printing onto the "
            "Globex Label Service.",
            "Moved label printing to the Globex Label Service, reducing reprints 30 percent.",
        ),
    ],
)
def test_figures_and_names_the_numbers_check_wants_copied_are_not_an_echo(
    inventory, settings, sentence: str, evidence: str
) -> None:
    report = _echo_report(inventory, settings, sentence, evidence=evidence)

    assert outcome(report, "letter echo") is Outcome.PASS


@pytest.mark.parametrize(
    ("sentence", "evidence", "expected"),
    [
        (
            "My rollout covered configuration through dispensing, medication-administration "
            "recording, and claim submission.",
            "Configuration through dispensing, medication-administration recording, and claim "
            "submission were in scope.",
            Outcome.PASS,
        ),
        (
            "My part of the program spanned validation, workflow testing, interface-engine "
            "collaboration, and go-live support.",
            "Validation, workflow testing, interface-engine collaboration, and go-live support "
            "ran for two years.",
            Outcome.PASS,
        ),
        (
            "I led validation, workflow testing, interface-engine collaboration, and go-live "
            "support.",
            "Led validation, workflow testing, interface-engine collaboration, and go-live "
            "support for two years.",
            Outcome.PASS,
        ),
        (
            "I rebuilt the carrier onboarding flow for parcel, freight, and returns so that new "
            "carriers went live in a week.",
            "I rebuilt the carrier onboarding flow for parcel, freight, and returns so that new "
            "carriers went live in a week.",
            Outcome.WARN,
        ),
        (
            "I rebuilt the returns intake flow, cut grading delays, and trained the receiving "
            "team on it.",
            "I rebuilt the returns intake flow, cut grading delays, and trained the receiving "
            "team on it.",
            Outcome.WARN,
        ),
        (
            "I led the migration of payroll, benefits, and time tracking for the regional "
            "hospital network.",
            "Led the migration of payroll, benefits, and time tracking for the regional hospital "
            "network.",
            Outcome.WARN,
        ),
        (
            "I improved the triage queue so nurses, pharmacists, and physicians saw fewer "
            "duplicate alerts.",
            "Improved the triage queue so nurses, pharmacists, and physicians saw fewer duplicate "
            "alerts.",
            Outcome.WARN,
        ),
        (
            "I managed the rollout, trained staff, and cut onboarding time, audit findings, and "
            "rework.",
            "Managed the rollout, trained staff, and cut onboarding time, audit findings, and "
            "rework.",
            Outcome.WARN,
        ),
        (
            "I delivered the organization's first integrated hosting and public interface.",
            "Delivered the organization's first integrated hosting and public interface that year.",
            Outcome.WARN,
        ),
        (
            "I analyzed outpatient infusion claims and corrected coding errors to improve "
            "charge capture.",
            "Analyzed outpatient infusion claims and corrected coding errors to improve charge "
            "capture each quarter.",
            Outcome.WARN,
        ),
    ],
)
def test_a_copied_run_is_an_echo_only_where_it_carries_a_verb_outside_a_list(
    inventory, settings, sentence: str, evidence: str, expected: Outcome
) -> None:
    report = _echo_report(inventory, settings, sentence, evidence=evidence)

    assert outcome(report, "letter echo") is expected


VERBLESS_EVIDENCE = (
    "Clinical reporting for the regional hospital network and its outpatient clinics "
    "statewide, over six years."
)


def test_a_long_copied_run_without_a_claim_verb_is_an_echo(inventory, settings) -> None:
    report = _echo_report(
        inventory,
        settings,
        "My background is clinical reporting for the regional hospital network and its "
        "outpatient clinics statewide.",
        evidence=VERBLESS_EVIDENCE,
    )

    echo = finding(report, "letter echo")
    assert echo.outcome is Outcome.WARN
    assert (
        "copies 'clinical reporting for the regional hospital network and its outpatient "
        "clinics statewide.'" in echo.detail
    )


def test_a_short_shared_phrase_without_a_claim_verb_is_not_an_echo(inventory, settings) -> None:
    report = _echo_report(
        inventory,
        settings,
        "My background is reporting for the regional hospital network and its clinics.",
        evidence=VERBLESS_EVIDENCE,
    )

    assert outcome(report, "letter echo") is Outcome.PASS


ECHO_RUN_LENGTHS = (8, 9, 10, 11)
ACTION_OBJECT = "the staged rows behind every nightly warehouse load before each release"
ACTION_FORMS = [
    (subject, verb)
    for subject, verbs in (
        ("I", ("rebuilt", "reduced", "queried", "traced", "kept", "mapped")),
        ("I", ("rebuild", "reduce", "query", "trace", "keep", "map")),
        ("I have", ("queried", "traced")),
    )
    for verb in verbs
]


@pytest.mark.parametrize("length", ECHO_RUN_LENGTHS)
@pytest.mark.parametrize(("subject", "verb"), ACTION_FORMS)
def test_a_copied_run_stating_an_action_is_an_echo_whatever_its_verb(
    inventory, settings, subject: str, verb: str, length: int
) -> None:
    run = " ".join((verb, *ACTION_OBJECT.split()[: length - 1]))
    report = _echo_report(
        inventory,
        settings,
        f"{subject} {run} ahead of launch.",
        evidence=f"{run.capitalize()} with the platform team.",
    )

    assert outcome(report, "letter echo") is Outcome.WARN


@pytest.mark.parametrize("length", [8, 9, 10])
@pytest.mark.parametrize("subject", ["I have", "I've", "I had"])
def test_a_copied_claim_after_a_perfect_auxiliary_is_an_echo(
    inventory, settings, subject: str, length: int
) -> None:
    """The run is a claim with an accurate paraphrase, not a term of art."""
    run = " ".join(("hands-on", "experience", "with", *ACTION_OBJECT.split()[: length - 3]))
    report = _echo_report(
        inventory,
        settings,
        f"{subject} {run} ahead of launch.",
        evidence=f"{run.capitalize()} with the platform team.",
    )

    assert outcome(report, "letter echo") is Outcome.WARN


NUMERAL_HEADS = (
    "Phase",
    "Level",
    "Type",
    "Class",
    "Title",
    "Grade",
    "Stage",
    "Tier",
    "Schedule",
    "Part",
)
NOUN_RUN = "clinical data review for the regional hospital network and its outpatient clinics"


@pytest.mark.parametrize("length", [8, 9, 10])
@pytest.mark.parametrize("head", NUMERAL_HEADS)
def test_a_roman_numeral_i_does_not_make_a_copied_noun_run_an_action(
    inventory, settings, head: str, length: int
) -> None:
    run = " ".join(NOUN_RUN.split()[:length])
    report = _echo_report(
        inventory,
        settings,
        f"My work supported {head} I {run} across the state.",
        evidence=f"{run.capitalize()} over six years.",
    )

    assert outcome(report, "letter echo") is Outcome.PASS


@pytest.mark.parametrize("length", [8, 9, 10])
@pytest.mark.parametrize(
    "lead", ["At Northwind", "At this stage", "In the second phase", "At that level"]
)
def test_i_after_a_name_or_lowercase_prose_is_still_the_pronoun_in_an_echo(
    inventory, settings, lead: str, length: int
) -> None:
    run = " ".join(("queried", *ACTION_OBJECT.split()[: length - 1]))
    report = _echo_report(
        inventory,
        settings,
        f"{lead} I {run} ahead of launch.",
        evidence=f"{run.capitalize()} with the platform team.",
    )

    assert outcome(report, "letter echo") is Outcome.WARN


def test_a_copied_series_of_clauses_is_an_echo_whatever_its_verbs(inventory, settings) -> None:
    run = "queried rows, traced dependencies, and mapped impact across the staging warehouse tables"
    report = _echo_report(
        inventory, settings, f"I {run} before launch.", evidence=f"{run.capitalize()} each week."
    )

    assert outcome(report, "letter echo") is Outcome.WARN


TERM_RUN = "reporting for the regional hospital network and its outpatient clinics statewide"
TERM_SERIES = (
    "payroll, benefits, and time tracking for the regional hospital network and its clinics"
)


@pytest.mark.parametrize("length", ECHO_RUN_LENGTHS)
@pytest.mark.parametrize("prefix", ["My background is", "I bring"])
@pytest.mark.parametrize(("terms", "joints"), [(TERM_RUN, 0), (TERM_SERIES, 4)])
def test_a_copied_run_of_terms_is_an_echo_only_past_a_heading(
    inventory, settings, terms: str, joints: int, prefix: str, length: int
) -> None:
    """A series' joints do not count, so only a plain run of eleven terms reads as prose."""
    run = " ".join(terms.split()[:length])
    report = _echo_report(
        inventory,
        settings,
        f"{prefix} {run} across the state.",
        evidence=f"{run.capitalize()} over six years.",
    )

    expected = Outcome.WARN if length - joints > HEADING_WORDS else Outcome.PASS
    assert outcome(report, "letter echo") is expected


WORK_SENTENCE = "I rebuilt the intake queue so reviewers saw each case once."
APOSTROPHES = ("'", "\N{RIGHT SINGLE QUOTATION MARK}")
SUMMARY_HEADS = [
    f"{head} {noun}".rstrip()
    for head in ("That", "This", "These", "Those", "Both", "All of this", "Each of these")
    for noun in ("", "work", "schema work", "last schema migration")
] + [
    "Together,",
    "Together, these projects",
    "Taken together,",
    "Taken together, these projects",
    "Together these two projects",
    "My work",
    "My experience",
    "My work building shared pipeline tests",
]
EVALUATIVE_CLAUSES = (
    "shows how I approach a schema change",
    "showed me how to test the edge cases first",
    "has shown me where a migration breaks",
    "demonstrates that I can own the next migration",
    "demonstrated how I test the edge cases first",
    "reflects the way I work",
    "reflected how I test the edge cases first",
    "equips me to own the next migration",
    "has equipped me to own the next migration",
    "prepared me for the next migration",
    "prepares me for work of this kind",
    "taught me to test the edge cases first",
    "gave me a habit of testing the edge cases first",
    "has given me a way to make those decisions verifiable",
    "gives me a way to make those decisions verifiable",
    "lets me own the next migration",
    "is why I test the edge cases first",
    "was how I learned to test the edge cases first",
    "means I test the edge cases first",
)
OFFERED_QUALITIES = (
    "is the care I would apply to product decisions and their delivery",
    "is the contribution I would make: careful schema work backed by reusable tools",
    "was the discipline I will bring to this team",
    "is the habit I can offer here",
    *(f"{a}s the care I{a}ll bring to this work" for a in APOSTROPHES),
    *(f"{a}s the care I{a}d apply to this work" for a in APOSTROPHES),
    *(f"is the discipline I{a}d also bring here" for a in APOSTROPHES),
)
IT_SHAPED_ME = (
    "It taught me to check the source before the schema",
    "It has taught me to test the edge cases first",
    "It teaches me where a migration breaks",
    "It prepared me for the next migration",
    "It prepares me for work of this kind",
    "It equipped me to own the next migration",
    "It had equipped me to own the next migration",
    "It equips me to own the next migration",
)
OFFERING_SUBJECTS = (
    *(
        f"{modal}{adverb}"
        for modal in (
            "I would",
            "I will",
            "I can",
            "I could",
            *(f"I{a}ll" for a in APOSTROPHES),
            *(f"I{a}d" for a in APOSTROPHES),
        )
        for adverb in ("", " also")
    ),
    *(f"I am {state} to" for state in ("ready", "eager", "keen")),
    *(f"I{a}m ready to" for a in APOSTROPHES),
    "I want to",
    "I hope to",
)
FIRST_PERSON_OFFERS = [
    f"{intro}{subject} {verb} that combination of hands-on delivery and release discipline "
    "to this work"
    for intro in ("", "In this role, ", "Here, ")
    for subject in OFFERING_SUBJECTS
    for verb in ("bring", "apply", "contribute", "offer")
]


def _closing(inventory, settings, body: str) -> Finding:
    report = _letter_report(
        inventory, settings, greeting="Dear hiring team,", closing="Sincerely,", body=body
    )
    return finding(report, "letter closing")


def _unflagged(inventory, settings, closings: list[str]) -> list[str]:
    """Return each closing sentence the letter closing check lets pass."""
    return [
        closing
        for closing in closings
        if _closing(inventory, settings, f"{WORK_SENTENCE} {closing}.").outcome is Outcome.PASS
    ]


@pytest.mark.parametrize("clause", EVALUATIVE_CLAUSES)
def test_a_closing_saying_what_the_work_made_of_the_candidate_is_a_warning(
    inventory, settings, clause: str
) -> None:
    closings = [f"{head} {clause}" for head in SUMMARY_HEADS]
    assert _unflagged(inventory, settings, closings) == []


@pytest.mark.parametrize("apostrophe", APOSTROPHES)
@pytest.mark.parametrize("word", ["why", "how"])
def test_a_contracted_summary_closing_is_a_warning(
    inventory, settings, apostrophe: str, word: str
) -> None:
    closing = f"That{apostrophe}s {word} I test the edge cases first"
    assert _unflagged(inventory, settings, [closing]) == []


@pytest.mark.parametrize("clause", OFFERED_QUALITIES)
def test_a_closing_naming_a_quality_the_candidate_would_bring_is_a_warning(
    inventory, settings, clause: str
) -> None:
    heads = ["That", "That combination", "This discipline", "All of this", "My experience"]
    joined = [
        f"{head}{clause}" if clause[0] in APOSTROPHES else f"{head} {clause}" for head in heads
    ]
    assert _unflagged(inventory, settings, joined) == []


def test_a_closing_offering_the_candidate_to_the_role_is_a_warning(inventory, settings) -> None:
    assert _unflagged(inventory, settings, FIRST_PERSON_OFFERS) == []


def test_a_closing_saying_what_it_shaped_in_the_candidate_is_a_warning(inventory, settings) -> None:
    assert _unflagged(inventory, settings, list(IT_SHAPED_ME)) == []


@pytest.mark.parametrize(
    "closing",
    [
        "That experience equips me to own the next migration.",
        "That work shows how I approach a schema change: I test the edge cases first.",
        "My work building shared pipeline tests gives me a way to make those decisions verifiable.",
        "It taught me to check the source before the schema.",
        "Taken together, these projects show how I work.",
        "In this role, I would bring the same care to releases.",
        "I am ready to apply that care to your data products.",
        "I want to bring the same care to releases.",
        "I am eager to contribute careful schema work to this team.",
    ],
)
def test_the_summary_closing_warning_quotes_the_last_sentence(
    inventory, settings, closing: str
) -> None:
    report = _letter_report(
        inventory,
        settings,
        greeting="Dear hiring team,",
        closing="Sincerely,",
        body=f"{WORK_SENTENCE} {closing}",
    )

    closed = finding(report, "letter closing")
    assert closed.outcome is Outcome.WARN
    assert not closed.redraftable
    assert repr(closing) in closed.detail
    assert report.passed


@pytest.mark.parametrize(
    "closing",
    [
        "That work shows how I approach releases, e.g. schema changes",
        "That work shows how I approach U.S. Insurance data",
        "That work shows how I partner with a Sr. Manager on data",
        "That work shows how I approach vendors like Acme Inc. and their feeds",
    ],
)
def test_a_summary_closing_is_read_whole_across_an_abbreviation(
    inventory, settings, closing: str
) -> None:
    assert _unflagged(inventory, settings, [closing]) == []


@pytest.mark.parametrize(
    "body",
    [
        f"{WORK_SENTENCE} That pipeline now serves 40 clinics.",
        f"{WORK_SENTENCE} This cut review time from three days to one.",
        f"{WORK_SENTENCE} That work showed a 30% drop in denials.",
        WORK_SENTENCE,
        "At Globex, I established shared test contracts for pipeline components.",
        "I also led the engineering for a partner integration that added two mappings.",
        "My production interface work covered mapping, validation, and go-live support.",
        "When a source changed shape, I implemented a normalization that protected 55,000 records.",
        f"That experience equips me to own the next migration. {WORK_SENTENCE}",
        f"I would bring that combination of hands-on delivery to this work. {WORK_SENTENCE}",
        "That change let my team close tickets in a day.",
        "This gave my reviewers a single queue.",
        "That audit showed my team where the duplicates came from.",
        "These dashboards showed my manager a 30% drop in late claims.",
        "This let me retire the nightly job.",
        "That meant I rebuilt the loader twice.",
        "That was the migration I'd scoped in March.",
        "That was the migration I\N{RIGHT SINGLE QUOTATION MARK}d scoped in March.",
        "This is how my team ships releases today.",
        "It showed me 1,200 duplicate records in the first week.",
        "It's how I found the 1,200 duplicate records.",
        "It now lets me rerun a failed load in minutes.",
        "It gave me on-call ownership of three services.",
        "That was the migration I'd scoped to bring the claims feed onto one schema.",
    ],
)
def test_a_closing_that_states_the_work_is_not_a_summary_closing(
    inventory, settings, body: str
) -> None:
    assert _closing(inventory, settings, body).outcome is Outcome.PASS


def test_an_opening_ownership_verb_the_evidence_never_uses_is_a_warning(
    inventory, settings
) -> None:
    # Both openings are in the ownership vocabulary; only "owned" is a word the
    # cited entry uses, so the pair separates the two branches on one fixture.
    entry = inventory.entries[0].model_copy(
        update={"text": "Owned the order migration with the platform team."}
    )
    record = inventory.model_copy(update={"entries": (entry,)})

    def report(text: str) -> Finding:
        draft = ResumeDraft(summary=DraftedLine(text=text, source_ids=(entry.id,)))
        return finding(build(record, settings, draft, "# J\n"), "ownership verbs")

    promoted = report("Led the order migration.")
    kept = report("Owned the order migration.")

    assert promoted.outcome is Outcome.WARN
    assert "'led'" in promoted.detail
    assert kept.outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("Maintained the order pipeline for the regional clinics.", Outcome.WARN),
        ("Built the order pipeline for the regional clinics.", Outcome.PASS),
    ],
)
def test_a_letter_claiming_a_stronger_verb_than_its_entry_is_a_warning(
    inventory, settings, evidence: str, expected: Outcome
) -> None:
    """A letter names its verb after "I", so the ownership check reads past the opening word."""
    entry = inventory.entries[0].model_copy(update={"text": evidence})
    record = inventory.model_copy(update={"entries": (entry, *inventory.entries[1:])})
    text = "Over two years I built the order pipeline for the regional clinics."

    verbs = finding(letter_with(record, settings, text), "ownership verbs")

    assert verbs.outcome is expected
    if expected is Outcome.WARN:
        assert "'built'" in verbs.detail


def test_an_ownership_verb_the_evidence_states_passes(inventory, settings) -> None:
    entry = inventory.entries[0].model_copy(update={"text": "Led the order migration."})
    record = inventory.model_copy(update={"entries": (entry, *inventory.entries[1:])})
    draft = simple_draft(record, "Led the order migration.", (entry.id,))
    report = build(record, settings, draft, "# J\n")
    assert outcome(report, "ownership verbs") is Outcome.PASS


def test_a_skills_line_kept_whole_across_headings_is_named_in_a_warning(
    inventory, settings
) -> None:
    platforms, governance = (entry.id for entry in inventory.entries if entry.kind.value == "skill")
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(platforms,)),
        skills=(
            DraftedSkill(
                label="Platforms and Engineering",
                text="Streaming, data contracts",
                source_ids=(platforms, governance),
            ),
        ),
    )
    report = build(inventory, settings, draft, "# J\n")
    finding = next(item for item in report.findings if item.check == "skills grouping")
    assert finding.outcome is Outcome.WARN
    assert "Streaming, data contracts" in finding.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Python, gRPC services", Outcome.WARN),
        ("Go, Kafka", Outcome.PASS),
        ("Platforms and Engineering: Go, Kafka", Outcome.PASS),
    ],
)
def test_an_item_no_cited_entry_lists_is_named_under_one_heading_too(
    inventory, settings, text: str, expected: Outcome
) -> None:
    platforms = next(entry.id for entry in inventory.entries if entry.text.startswith("Python"))
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(platforms,)),
        skills=(
            DraftedSkill(label="Platforms and Engineering", text=text, source_ids=(platforms,)),
        ),
    )
    finding = next(
        item
        for item in build(inventory, settings, draft, "# J\n").findings
        if item.check == "skills grouping"
    )
    assert finding.outcome is expected
    if expected is Outcome.WARN:
        assert "gRPC services" in finding.detail


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Quux", Outcome.WARN),
        ("familiar with Quux", Outcome.PASS),
        ("familiar with Qux and Corge", Outcome.PASS),
        ("familiar with Qux, Quux, and Corge", Outcome.PASS),
    ],
)
def test_an_item_of_a_led_series_is_placed_only_with_its_lead_in(
    inventory, settings, text: str, expected: Outcome
) -> None:
    listed = next(entry for entry in inventory.entries if entry.text.startswith("Python"))
    entry = listed.model_copy(update={"text": "Python, familiar with Qux, Quux, and Corge"})
    record = inventory.model_copy(
        update={"entries": tuple(entry if e.id == listed.id else e for e in inventory.entries)}
    )
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(entry.id,)),
        skills=(DraftedSkill(label="Platforms", text=text, source_ids=(entry.id,)),),
    )

    assert outcome(build(record, settings, draft, "# J\n"), "skills grouping") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("familiar with Quux, Go, Rust", Outcome.PASS),
        ("Familiar with Quux", Outcome.PASS),
        ("familiar with Quux, Go, Zig", Outcome.WARN),
    ],
)
def test_a_drafted_led_item_is_placed_by_its_longest_listed_prefix_and_each_later_piece(
    inventory, settings, text: str, expected: Outcome
) -> None:
    skills = [entry for entry in inventory.entries if entry.kind is EntryKind.SKILL]
    led = skills[0].model_copy(update={"text": "familiar with Qux, Quux and Corge"})
    names = skills[1].model_copy(update={"text": "Go, Rust"})
    swapped = {led.id: led, names.id: names}
    record = inventory.model_copy(
        update={"entries": tuple(swapped.get(e.id, e) for e in inventory.entries)}
    )
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(led.id,)),
        skills=(DraftedSkill(label="Platforms", text=text, source_ids=(led.id, names.id)),),
    )

    assert outcome(build(record, settings, draft, "# J\n"), "skills grouping") is expected


def test_a_bare_part_of_a_led_series_is_named_when_the_cv_skills_carry_no_headings(
    inventory, settings
) -> None:
    skills = [entry for entry in inventory.entries if entry.kind is EntryKind.SKILL]
    listed = skills[0].model_copy(
        update={"text": "familiar with Qux, Quux, and Corge", "label": None, "group": None}
    )
    headless = {
        entry.id: entry.model_copy(update={"label": None, "group": None}) for entry in skills
    }
    headless[listed.id] = listed
    record = inventory.model_copy(
        update={"entries": tuple(headless.get(e.id, e) for e in inventory.entries)}
    )
    draft = ResumeDraft(
        summary=DraftedLine(text="Builds data platforms.", source_ids=(listed.id,)),
        skills=(DraftedSkill(label="Tools", text="Quux", source_ids=(listed.id,)),),
    )

    assert outcome(build(record, settings, draft, "# J\n"), "skills grouping") is Outcome.WARN


CORPUS = tomllib.loads(
    (Path(__file__).parents[1] / "fixtures" / "check_corpus" / "corpus.toml").read_text()
)["item"]


def _corpus_report(item: dict[str, Any], inventory: CvInventory, settings) -> Report:
    """Run the resume or letter check set over one corpus item citing its own evidence."""
    from clearscout.application.tailoring.stages.draft import CoverLetter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource

    original = next(e for e in inventory.entries if e.text.startswith("Jane Example builds"))
    record, cited_id = evidence_record(inventory, item.get("evidence", original.text))
    spec = spec_with().model_copy(update={"requirements": ()})
    plan = build_plan(record, spec, EvidenceMap(), settings)
    line = DraftedLine(text=item["text"], source_ids=(cited_id,))
    if item["kind"] == "resume":
        return check(
            markdown=f"# JANE\n\njane.doe@example.com\n\n---\n\n## Summary\n\n{item['text']}\n",
            draft=ResumeDraft(summary=line),
            plan=plan,
            inventory=record,
            spec=spec,
            guardrails=_guardrails(),
        )
    research: tuple[ResearchSource, ...] = ()
    if "company" in item:
        research = (
            ResearchSource(
                id="company.about",
                kind="about",
                url="https://company.example/about",
                text=item["company"],
                method="scripted",
            ),
        )
        line = DraftedLine(
            text=item["text"], source_ids=("company.about",), source_types=(SourceType.COMPANY,)
        )
    return check_letter(
        letter=CoverLetter(greeting="Dear hiring team,", paragraphs=(line,), closing="Sincerely,"),
        plan=plan,
        inventory=record,
        guardrails=_guardrails(),
        research=research,
    )


def test_each_check_scores_the_output_corpus_as_labelled(inventory, settings) -> None:
    """Text shaped like real drafts, which can trip checks that unit cases pass."""
    mismatches = [
        f"{item['name']}: {name} {actual}, labelled {expected}"
        for item in CORPUS
        for name, expected in item["expect"].items()
        if (actual := outcome(_corpus_report(item, inventory, settings), name)) != expected
    ]
    assert not mismatches, "\n".join(mismatches)


def test_an_honest_corpus_item_passes_every_check_together(inventory, settings) -> None:
    """Checks that each pass alone can still contradict one another on the same sentence."""
    failing = [
        f"{item['name']}: {', '.join(finding.check for finding in report.failures)}"
        for item in CORPUS
        if item["honest"] and not (report := _corpus_report(item, inventory, settings)).passed
    ]
    assert not failing, "\n".join(failing)
