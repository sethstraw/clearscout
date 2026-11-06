"""Assembly is where fabrication is designed out, so it is tested closely."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from clearscout.application.master_cv.cv import Entry, EntryKind
from clearscout.application.tailoring.stages.context import chronology_block, citable_entries
from clearscout.application.tailoring.stages.draft import (
    CoverLetter,
    Document,
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
    SourceType,
    cover_letter_schema,
    draft_schema,
    evidence_block,
    skill_items,
    skill_placements,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.stages.select import build_plan
from tests.support.resumes import (
    DETAIL,
    DETAILED_BULLET,
    bullet_ids,
    cited,
    detailed_record,
    drafted_to_length,
    long_record,
    must_haves,
    sentence,
)


def spec() -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(Requirement(id="r01", text="Kafka", importance=Importance.MUST_HAVE),),
        keywords=("Kafka",),
        hard_gates=(),
        origin="test",
    )


def plan_for(inventory, settings):
    # These tests are about assembling a document, not about deciding what goes
    # in it, so they ask for publications explicitly. Selection only includes
    # them when the posting gives a reason, and none of these postings does.
    settings.min_publications = max(settings.min_publications, 3)
    kafka = next(e.id for e in inventory.entries if e.text.startswith("Rebuilt the ingestion"))
    registry = next(e.id for e in inventory.entries if e.text.startswith("Designed the schema"))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka, registry),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    return build_plan(inventory, spec(), evidence, settings), kafka, registry


def drafted(kafka: str, registry: str) -> ResumeDraft:
    return ResumeDraft(
        summary=DraftedLine(text="Senior platform engineer.", source_ids=(kafka,)),
        skills=(DraftedSkill(label="Platforms", text="Kafka, Terraform", source_ids=(kafka,)),),
        roles=(
            DraftedRole(
                role_key="northwind-analytics",
                context=DraftedLine(text="Led platform architecture.", source_ids=(kafka,)),
                bullets=(
                    DraftedLine(text="Rebuilt ingestion on Kafka.", source_ids=(kafka,)),
                    DraftedLine(text="Designed the schema registry.", source_ids=(registry,)),
                ),
            ),
        ),
        earlier_career=DraftedLine(
            text="Earlier analytics engineering roles.", source_ids=(kafka,)
        ),
    )


def test_the_draft_schema_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field this schema never declared is refused, not dropped."""
    schema = draft_schema(skills=False)
    with pytest.raises(ValidationError):
        schema.model_validate({"summary": {"text": "x"}, "extra_field": "y"})


@pytest.mark.parametrize("field", ["greeting", "closing", "extra_field"])
def test_the_cover_letter_schema_rejects_a_key_it_does_not_ask_the_model_for(field) -> None:
    schema = cover_letter_schema()
    with pytest.raises(ValidationError):
        schema.model_validate({"paragraphs": [], field: "Dear team,"})


def test_resume_draft_rejects_an_undeclared_key() -> None:
    with pytest.raises(ValidationError):
        ResumeDraft.model_validate({"summary": {"text": "x"}, "extra_field": "y"})


def test_cover_letter_rejects_an_undeclared_key() -> None:
    with pytest.raises(ValidationError):
        CoverLetter.model_validate(
            {"greeting": "Dear team,", "closing": "Sincerely,", "extra_field": "y"}
        )


def test_lines_preserve_skill_source_types() -> None:
    draft = ResumeDraft(
        summary=DraftedLine(
            text="Senior platform engineer.",
            source_ids=("b-1",),
            source_types=(SourceType.CANDIDATE,),
        ),
        skills=(
            DraftedSkill(
                label="Platforms",
                text="Kafka",
                source_ids=("b-1",),
                source_types=(SourceType.CANDIDATE,),
            ),
        ),
    )

    lines = draft.lines()
    skill_line = next(line for line in lines if line.text.startswith("Platforms:"))
    assert skill_line.source_types == (SourceType.CANDIDATE,)


def test_role_metadata_is_copied_verbatim_from_the_master_cv(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(drafted(kafka, registry), inventory, plan, settings, guardrails).markdown()
    assert "### Northwind Analytics" in markdown
    assert "**Staff Systems Engineer - Platform**" in markdown
    assert "(title of record: Staff Systems Engineer - Platform)" in markdown
    assert "March 2021 to Present | Springfield, Illinois (Remote)" in markdown


def test_education_and_publications_never_pass_through_the_model(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(drafted(kafka, registry), inventory, plan, settings, guardrails).markdown()
    assert "**Bachelor of Science, Computer Science**, University of Illinois" in markdown
    assert "Journal of Applied Data Engineering" in markdown


def test_a_nested_line_prints_verbatim_beneath_its_degree_and_publication(settings, guardrails):
    """A degree's detail takes a hard line break, a publication's stays a nested list item."""
    from tests.support.resumes import DEGREE, PAPER, PAPER_DATE, THESIS, nested_copies_record

    record = nested_copies_record()
    settings.min_publications = 1
    plan = build_plan(record, spec(), EvidenceMap(), settings)
    draft = ResumeDraft(summary=DraftedLine(text="Data engineer.", source_ids=()))
    document = Document(draft, record, plan, settings, guardrails)

    assert document.education() == f"## {settings.sections.education}\n\n{DEGREE}  \n{THESIS}"
    assert document.publications() == (
        f"## {settings.sections.publications}\n\n- {PAPER}\n  - {PAPER_DATE}"
    )


def test_the_work_authorization_line_comes_from_guardrails(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(drafted(kafka, registry), inventory, plan, settings, guardrails).markdown()
    assert guardrails.work_authorization is not None
    assert guardrails.work_authorization in markdown


def test_excluding_a_source_removes_its_bullet(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(
        drafted(kafka, registry), inventory, plan, settings, guardrails, frozenset({registry})
    ).markdown()
    assert "Rebuilt ingestion on Kafka." in markdown
    assert "Designed the schema registry." not in markdown


def test_excluding_a_publication_removes_it(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    dropped = plan.publication_ids[-1]
    text = inventory.by_id(dropped).text
    markdown = Document(
        drafted(kafka, registry), inventory, plan, settings, guardrails, frozenset({dropped})
    ).markdown()
    assert text not in markdown


def test_section_names_are_configurable(inventory, settings, guardrails):
    settings.sections.publications = "Selected Publication"
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(drafted(kafka, registry), inventory, plan, settings, guardrails).markdown()
    assert "## Selected Publication" in markdown


def test_the_document_never_carries_an_annotation(inventory, settings, guardrails):
    plan, kafka, registry = plan_for(inventory, settings)
    markdown = Document(drafted(kafka, registry), inventory, plan, settings, guardrails).markdown()
    for marker in ("<!--", "Generated", "clearscout", "Revision", "Draft"):
        assert marker not in markdown


def test_the_evidence_block_carries_identifiers_and_requirements(inventory, settings):
    plan, kafka, _ = plan_for(inventory, settings)
    block = evidence_block(inventory, plan, spec())
    assert f"[{kafka}]" in block
    assert "answers r01; must-have r01" in block
    assert "role_key: northwind-analytics" in block


@pytest.mark.parametrize("layout", [True, False], ids=["resume", "letter"])
def test_the_evidence_block_shows_a_detail_under_its_parent(settings, layout: bool) -> None:
    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]
    evidence = EvidenceMap(matches=(cited("r01", [parent.id]),))
    plan = build_plan(record, must_haves(1), evidence, settings)

    block = evidence_block(record, plan, must_haves(1), layout=layout)

    assert f"[{parent.id}] {DETAILED_BULLET}  (answers r01; must-have r01)\n  - {DETAIL}" in block


def test_the_evidence_block_leaves_the_indented_line_rule_to_the_prompt(settings) -> None:
    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", [parent.id]),)), settings
    )

    assert "An indented line belongs" not in evidence_block(record, plan, must_haves(1))


def test_drafting_states_a_selected_bullet_once_and_still_sees_the_whole_career(
    inventory, settings
):
    """The chronology carries every role; only the task carries bullet text."""
    plan, kafka, _ = plan_for(inventory, settings)
    selected = next(entry for entry in citable_entries(inventory) if entry.id == kafka)
    task = evidence_block(inventory, plan, spec())
    sent = chronology_block(inventory)

    assert selected.text in task
    assert selected.text not in sent
    assert f"[{kafka}]" not in sent
    for role in inventory.roles:
        assert role.organization in sent


def _cv_stating_authorization(tmp_path: Path) -> Path:
    """A master CV whose header carries a work authorization sentence of its own."""
    source = tmp_path / "CV.md"
    source.write_text(
        "# ANA FERREIRA\n\n"
        "Lisbon, Portugal | ana.ferreira@example.com | +351 21 000 0000\n\n"
        "Work Authorization: citizen of Portugal and permanent resident of Ireland, "
        "eligible to work in both without sponsorship.\n\n"
        "ORCID: orcid.org/0000-0002-1825-0097\n\n"
        "Languages: English (native or bilingual proficiency)\n\n"
        "## SUMMARY\n\nBackend engineer.\n\n"
        "## PROFESSIONAL EXPERIENCE\n\n### Kettle Logistics\n\n"
        "**Backend Engineer**\nMarch 2022 to Present | Lisbon\n\n"
        "- Cut p99 latency on the routing endpoint with a materialized view\n",
        encoding="utf-8",
    )
    return source


def _header_of(record, settings, guardrails) -> list[str]:
    """Return the assembled header, which is everything above the first section."""
    plan = build_plan(record, spec(), EvidenceMap(), settings)
    draft = ResumeDraft(summary=DraftedLine(text="Backend engineer.", source_ids=()))
    document = Document(draft, record, plan, settings, guardrails).markdown()
    return document.split("\n## ")[0].splitlines()


def test_a_settings_authorization_line_replaces_the_masters_rather_than_joining_it(
    tmp_path, settings, guardrails
) -> None:
    """A work authorization in settings replaces the master's own, so it appears once.

    The settings version wins because it was written for derivatives, and every
    line that is not about authorization survives.
    """
    from clearscout.application.master_cv.cv import parse_cv

    record = parse_cv(_cv_stating_authorization(tmp_path))
    guardrails.work_authorization = (
        "Portuguese citizen and Irish permanent resident | No sponsorship required"
    )

    header = _header_of(record, settings, guardrails)

    assert guardrails.work_authorization in header
    assert not any(line.startswith("Work Authorization:") for line in header)
    for kept in (
        "Lisbon, Portugal | ana.ferreira@example.com | +351 21 000 0000",
        "ORCID: orcid.org/0000-0002-1825-0097",
        "Languages: English (native or bilingual proficiency)",
    ):
        assert kept in header


def test_the_masters_own_statement_survives_when_settings_supply_none(
    tmp_path, settings, guardrails
) -> None:
    """Dropping it unconditionally would lose the fact rather than deduplicate it."""
    from clearscout.application.master_cv.cv import parse_cv

    record = parse_cv(_cv_stating_authorization(tmp_path))
    guardrails.work_authorization = None

    header = _header_of(record, settings, guardrails)

    assert any(line.startswith("Work Authorization:") for line in header)


def test_a_derivative_omits_the_contact_lines_the_user_named(tmp_path, settings, guardrails):
    """The master CV is the superset. A resume header is expensive space."""
    from clearscout.application.master_cv.cv import parse_cv

    source = tmp_path / "CV.md"
    source.write_text(
        "# ANA FERREIRA\n\n"
        "Lisbon, Portugal | ana.ferreira@example.com\n\n"
        "ORCID: orcid.org/0000-0002-1825-0097\n\n"
        "Languages: Portuguese, English\n\n"
        "## SUMMARY\n\nBackend engineer.\n\n"
        "## PROFESSIONAL EXPERIENCE\n\n### Kettle Logistics\n\n"
        "**Backend Engineer**\nMarch 2022 to Present | Lisbon\n\n"
        "- Cut p99 latency on the routing endpoint with a materialized view\n",
        encoding="utf-8",
    )
    record = parse_cv(source)
    assert any(line.startswith("ORCID") for line in record.contact_lines), "fixture is wrong"

    plan = build_plan(record, spec(), EvidenceMap(), settings)
    draft = ResumeDraft(summary=DraftedLine(text="Backend engineer.", source_ids=()))

    full = Document(draft, record, plan, settings, guardrails).markdown()
    assert any(line.startswith("ORCID") for line in full.splitlines())

    settings.header_drop = ("ORCID", "Languages")
    trimmed = Document(draft, record, plan, settings, guardrails).markdown()

    assert len(trimmed.splitlines()) < len(full.splitlines())
    for prefix in ("ORCID", "Languages:"):
        assert not any(line.startswith(prefix) for line in trimmed.splitlines())
    # The things a reader needs in order to reply are never dropped by this.
    assert "ana.ferreira@example.com" in trimmed


def test_the_length_budget_counts_the_header_it_will_actually_write(inventory, settings):
    """A header of nine lines budgeted as five over-fills the page before selection runs."""
    # header_drop matches the start of a contact line, and this record states all
    # of its contact details on one pipe-separated line.
    settings.header_drop = ()
    wide = build_plan(inventory, spec(), EvidenceMap(), settings)
    settings.header_drop = (inventory.contact_lines[0].split(" | ")[0],)
    narrow = build_plan(inventory, spec(), EvidenceMap(), settings)

    assert narrow.unspent_lines > wide.unspent_lines


def test_a_long_record_is_drafted_to_within_two_lines_of_the_page_budget(
    tmp_path, settings, guardrails
):
    """Selection buys more than the page holds, and the fit pass trims the draft back to it.

    Drafts run shorter than their sources, so selecting only what the page
    holds at master length would print a short page.
    """
    from clearscout.application.tailoring.layout import Budget, markdown_lines
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    evidence = EvidenceMap(
        matches=tuple(cited(f"r0{number}", bullets[number - 1 :: 4]) for number in range(1, 5))
    )
    plan = build_plan(record, must_haves(1, 2, 3, 4), evidence, settings)
    fitted = fit(drafted_to_length(record, plan, 0.8), record, plan, settings, guardrails)
    budget = Budget(settings.pages).total_lines

    assert fitted.trimmed, "selection bought no more than the page holds"
    assert budget - 2 <= fitted.total_lines <= budget
    assert fitted.total_lines == pytest.approx(markdown_lines(fitted.markdown))
    printed = {line[2:] for line in fitted.markdown.splitlines() if line.startswith("- ")}
    planned = {
        drafted.text
        for role in drafted_to_length(record, plan, 0.8).roles
        for drafted in role.bullets
        if not set(drafted.source_ids) <= fitted.excluded
    }
    assert printed == planned, "the page carries a bullet no cited entry backs"


PAPERS = (
    "Example J. Streaming lineage in practice. *Journal of Examples*. 2021;2(1):3-9.",
    "Example J. A note on batch loads. *Journal of Examples*. 2019;1(2):10-12.",
    "Example J. Schema governance at scale. *Journal of Examples*. 2023;4(4):20-31.",
)


def _paper_ids(record) -> tuple[str, ...]:
    return tuple(entry.id for entry in record.of_kind(EntryKind.PUBLICATION))


def test_fit_trims_an_unlisted_weaker_paper_before_a_must_consider_one(
    tmp_path, settings, guardrails
):
    """An uncited listed paper ranks with the strongest offered paper, not at zero."""
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path, PAPERS)
    strong, weak, listed = _paper_ids(record)
    bullets = bullet_ids(record)
    evidence = EvidenceMap(
        matches=(cited("r01", (*bullets, strong)), cited("r01", (weak,), Strength.WEAK))
    )
    marked = settings.model_copy(
        update={"must_consider_publications": (listed,), "max_bullets_per_role": 40}
    )
    plan = build_plan(record, must_haves(1), evidence, marked)
    fitted = fit(drafted_to_length(record, plan, 0.8), record, plan, marked, guardrails)

    assert listed in plan.publication_ids
    assert plan.drop_order.index(weak) < plan.drop_order.index(listed)
    assert fitted.trimmed
    if listed in fitted.trimmed:
        assert weak in fitted.trimmed[: fitted.trimmed.index(listed)]


def test_a_must_consider_paper_the_budget_forces_out_is_named_in_the_notes(
    tmp_path, settings, guardrails
):
    from clearscout.application.tailoring.pipeline.rewrite import publication_notes
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path, PAPERS[2:])
    (listed,) = _paper_ids(record)
    bullets = bullet_ids(record)
    # No role passes the cap, so the drop order opens on the weakest item rather than depth.
    marked = settings.model_copy(
        update={"must_consider_publications": (listed,), "max_bullets_per_role": 40}
    )
    plan = build_plan(record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), marked)
    fitted = fit(drafted_to_length(record, plan, 1.0), record, plan, marked, guardrails)

    assert listed in fitted.trimmed
    notes = publication_notes(marked, plan, record, fitted)
    assert f"Publication trimmed for length: {PAPERS[2]}" in notes


def test_fit_keeps_one_of_two_weak_answers_to_a_must_have(tmp_path, settings, guardrails):
    """Both answers rank last, so a drop order read as a plain prefix removes both."""
    from clearscout.application.tailoring.stages.draft import fit
    from clearscout.application.tailoring.stages.verify import check

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    weak = (bullets[20], bullets[21])
    evidence = EvidenceMap(
        matches=(
            cited("r01", weak, Strength.WEAK),
            cited("r02", [entry_id for entry_id in bullets if entry_id not in weak]),
        )
    )
    plan = build_plan(record, must_haves(1, 2), evidence, settings)
    assert set(weak) <= set(plan.drop_order[:12]), "the weak answers must rank among the first"
    fitted = fit(drafted_to_length(record, plan, 0.8), record, plan, settings, guardrails)
    report = check(
        markdown=fitted.markdown,
        draft=fitted.printed,
        plan=plan,
        inventory=record,
        spec=must_haves(1, 2),
        guardrails=guardrails,
        excluded=fitted.excluded,
    )
    coverage = next(item for item in report.findings if item.check == "must-have coverage")

    assert fitted.trimmed
    assert len(set(weak) - fitted.excluded) == 1
    assert coverage.outcome.value == "pass", coverage.detail


def test_fit_never_drops_an_entry_the_summary_cites(tmp_path, settings, guardrails):
    """The summary would still make the claim the dropped bullet was its evidence for."""
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    first = plan.drop_order[0]
    written = drafted_to_length(record, plan, 0.8)
    draft = written.model_copy(
        update={"summary": written.summary.model_copy(update={"source_ids": (first,)})}
    )
    fitted = fit(draft, record, plan, settings, guardrails)

    assert fitted.trimmed
    assert first not in fitted.excluded


def test_fit_trims_roles_past_the_cap_level_by_level(tmp_path, settings, guardrails):
    """Three rich roles, all cited, all past the cap: the fit keeps their depths within one.

    Trimming the weakest over-cap bullets first would empty the oldest role back to
    the cap, because recency is part of every bullet's score.
    """
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    fitted = fit(drafted_to_length(record, plan, 0.8), record, plan, settings, guardrails)
    past = [len(role.bullets) - settings.max_bullets_per_role for role in fitted.printed.roles]

    assert fitted.trimmed
    assert all(
        len(ids) > settings.max_bullets_per_role + 1 for ids in plan.role_bullets.values()
    ), "the fixture no longer puts every role past the cap"
    assert min(past) > 0
    assert max(past) - min(past) <= 1, past


def test_a_draft_too_long_to_fit_says_how_far_over_it_runs(tmp_path, settings, guardrails):
    from clearscout.application.tailoring.layout import Budget
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    written = drafted_to_length(record, plan, 0.8)
    summary = written.summary.model_copy(update={"text": sentence(110 * 150, 0)})
    fitted = fit(
        written.model_copy(update={"summary": summary}), record, plan, settings, guardrails
    )

    assert fitted.remaining == ()
    assert fitted.overflow == pytest.approx(fitted.total_lines - Budget(settings.pages).total_lines)
    assert fitted.overflow > 0


def _merged(written: ResumeDraft, first: str, partner: str) -> ResumeDraft:
    """Return ``written`` with ``first``'s bullet citing ``partner`` too, and ``partner``'s gone."""
    roles = []
    for role in written.roles:
        kept = [bullet for bullet in role.bullets if bullet.source_ids != (partner,)]
        roles.append(
            role.model_copy(
                update={
                    "bullets": tuple(
                        bullet.model_copy(update={"source_ids": (first, partner)})
                        if bullet.source_ids == (first,)
                        else bullet
                        for bullet in kept
                    )
                }
            )
        )
    return written.model_copy(update={"roles": tuple(roles)})


def test_an_entry_whose_merged_line_still_prints_is_on_the_page(tmp_path, settings, guardrails):
    """A bullet citing two entries prints while either survives; losing one removes nothing."""
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    first, last = plan.drop_order[0], plan.drop_order[-1]
    written = _merged(drafted_to_length(record, plan, 0.8), first, last)
    fitted = fit(written, record, plan, settings, guardrails)

    assert first in fitted.excluded
    assert first not in fitted.off_page
    assert first not in fitted.trimmed
    assert first in fitted.printed_plan.all_ids()
    assert fitted.remaining[-2:] == (first, last), "a further --trim takes it off with its partner"


def test_a_planned_bullet_the_drafter_left_out_is_never_counted_as_trimmed(
    tmp_path, settings, guardrails
):
    """It was never on the page, so --trim N must take N entries that were."""
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    written = drafted_to_length(record, plan, 0.8)
    left_out = fit(written, record, plan, settings, guardrails).remaining[0]
    written = written.model_copy(
        update={
            "roles": tuple(
                role.model_copy(
                    update={
                        "bullets": tuple(b for b in role.bullets if b.source_ids != (left_out,))
                    }
                )
                for role in written.roles
            )
        }
    )
    untrimmed = fit(written, record, plan, settings, guardrails)
    one = fit(written, record, plan, settings, guardrails, trim=1)

    assert left_out not in (*untrimmed.trimmed, *untrimmed.remaining)
    assert left_out not in one.requested
    assert one.markdown != untrimmed.markdown


def test_a_merged_line_leaves_the_page_with_both_its_entries(tmp_path, settings, guardrails):
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    plan = build_plan(
        record, must_haves(1), EvidenceMap(matches=(cited("r01", bullets),)), settings
    )
    first, second = plan.drop_order[:2]
    written = _merged(drafted_to_length(record, plan, 0.8), first, second)
    fitted = fit(written, record, plan, settings, guardrails)

    assert fitted.trimmed[:2] == (first, second)
    assert {first, second} <= fitted.off_page
    assert not {first, second} & set(fitted.printed_plan.all_ids())


def test_fit_counts_only_what_the_page_prints_as_answering_a_must_have(
    tmp_path, settings, guardrails
):
    """A drafted role the page never prints answers nothing, however many lines cite it."""
    from clearscout.application.tailoring.stages.draft import fit

    record = long_record(tmp_path)
    bullets = bullet_ids(record)
    weak = (bullets[20], bullets[21])
    evidence = EvidenceMap(
        matches=(
            cited("r01", weak, Strength.WEAK),
            cited("r02", [entry_id for entry_id in bullets if entry_id not in weak]),
        )
    )
    plan = build_plan(record, must_haves(1, 2), evidence, settings)
    written = drafted_to_length(record, plan, 0.8)
    unprinted = DraftedRole(
        role_key="not-a-recent-role",
        bullets=(DraftedLine(text="Unprinted.", source_ids=(*weak, plan.drop_order[-1])),),
    )
    fitted = fit(
        written.model_copy(update={"roles": (*written.roles, unprinted)}),
        record,
        plan,
        settings,
        guardrails,
    )

    assert fitted.trimmed
    assert len(set(weak) - fitted.excluded) == 1


def test_the_evidence_block_tells_each_role_how_many_bullets_to_write(inventory, settings):
    """A ceiling with no target would let the model decide the document's length.

    "At most one bullet per selected evidence item" is satisfied by writing
    fewer. Length is Python's to decide, by fitting the finished draft to the
    page; only the prose belongs to the model.
    """
    plan, _kafka, _registry = plan_for(inventory, settings)
    block = evidence_block(inventory, plan, spec())

    assert any(plan.role_context.get(key) and ids for key, ids in plan.role_bullets.items())
    for role_key, ids in plan.role_bullets.items():
        if not ids:
            continue
        section = block.split(f"### role_key: {role_key}")[1].split("\n#")[0]
        assert f"Write {len(ids)} bullet(s) here" in section, role_key
        below = section.split("bullet(s) here")[1].splitlines()[1:]
        # A context entry listed under the count would ask for one bullet more than it says.
        assert [line[1 : line.index("]")] for line in below] == list(ids), role_key


# A headingless skill first, then a labeled paragraph, then bullets under a bold line.
SKILLS = "- Python\n\n**Platforms:** Kafka, Terraform\n\n**Data**\n\n- lineage\n- quality rules"


def _drafted_skills(tmp_path, settings, guardrails, client_factory, skills, *lines):
    """Draft skills lines, each given as its text, the entry-text prefixes it cites, and extras."""
    from clearscout.application.master_cv.cv import parse_cv
    from clearscout.application.tailoring.stages.draft import draft

    record = parse_cv(
        tmp_path / "CV.md",
        f"# ANA FERREIRA\n\n## SKILLS\n\n{skills}\n\n"
        "## EXPERIENCE\n\n### Kettle Logistics\n\n**Backend Engineer**\n"
        "March 2022 to Present | Lisbon\n\n- Cut p99 latency on the routing endpoint\n",
    )
    plan = build_plan(record, spec(), EvidenceMap(), settings)
    answer = {
        "summary": {"text": "Backend engineer."},
        "skills": [
            {
                **extra,
                "text": text,
                "source_ids": [
                    e.id for prefix in cited for e in record.entries if e.text.startswith(prefix)
                ],
            }
            for text, cited, extra in lines
        ],
    }
    client = client_factory({"ResumeDraft": lambda _: answer})
    drafted_skills = draft(client, record, spec(), plan, guardrails).skills
    return [(skill.label, skill.text) for skill in drafted_skills]


@pytest.mark.parametrize(
    ("text", "cited", "heading"),
    [("Kafka", ("Kafka",), "Platforms"), ("lineage", ("lineage",), "Data")],
)
def test_a_skills_line_takes_the_cv_heading_of_the_entries_it_cites(
    tmp_path, settings, guardrails, client_factory, text, cited, heading
) -> None:
    lines = _drafted_skills(
        tmp_path, settings, guardrails, client_factory, SKILLS, (text, cited, {})
    )

    assert lines == [(heading, text)]


def test_headingless_skills_share_one_additional_skills_line(
    tmp_path, settings, guardrails, client_factory
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        "- Python\n- Rust\n\n**Platforms:** Kafka",
        ("Python", ("Python",), {}),
        ("Rust", ("Rust",), {}),
    )

    assert lines == [("Additional skills", "Python, Rust")]


def test_lines_under_one_heading_merge_in_first_seen_order(
    tmp_path, settings, guardrails, client_factory
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        SKILLS,
        ("quality rules", ("quality",), {}),
        ("Kafka", ("Kafka",), {}),
        ("lineage", ("lineage",), {}),
    )

    assert lines == [("Data", "quality rules, lineage"), ("Platforms", "Kafka")]


@pytest.mark.parametrize(
    ("text", "cited", "expected"),
    [
        ("Kafka, lineage", ("Kafka", "lineage"), [("Platforms", "Kafka"), ("Data", "lineage")]),
        (
            "Python, Terraform",
            ("Kafka", "Python"),
            [("Additional skills", "Python"), ("Platforms", "Terraform")],
        ),
    ],
)
def test_a_line_mixing_headings_is_split_by_the_entry_stating_each_item(
    tmp_path, settings, guardrails, client_factory, text, cited, expected
) -> None:
    lines = _drafted_skills(
        tmp_path, settings, guardrails, client_factory, SKILLS, (text, cited, {})
    )

    assert lines == expected


def test_a_mixed_line_no_entry_states_stays_whole_under_its_first_citation(
    tmp_path, settings, guardrails, client_factory
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        SKILLS,
        ("Streaming, data lineage", ("Kafka", "lineage"), {}),
    )

    assert lines == [("Platforms", "Streaming, data lineage")]


SHARED_SKILLS = (
    "**Web stack:** JavaScript, React, Django, gRPC and HTTP services\n\n"
    "**Programming:** Java, R, Python, SQL\n\n"
    "**Data stores:** PostgreSQL, Redis, Message brokers (Kafka, RabbitMQ), SQL\n\n"
    "**Infrastructure:** Terraform"
)


@pytest.mark.parametrize(
    ("text", "cited", "expected"),
    [
        ("R, React", ("JavaScript", "Java, R"), [("Programming", "R"), ("Web stack", "React")]),
        (
            "Java, JavaScript",
            ("JavaScript", "Java, R"),
            [("Programming", "Java"), ("Web stack", "JavaScript")],
        ),
        (
            "PostgreSQL, SQL",
            ("Java, R", "PostgreSQL"),
            [("Data stores", "PostgreSQL"), ("Programming", "SQL")],
        ),
        ("SQL", ("PostgreSQL", "Java, R"), [("Data stores", "SQL")]),
        (
            "Python, Django, Nomad, Terraform",
            ("JavaScript", "Java, R", "Terraform"),
            [
                ("Programming", "Python"),
                ("Web stack", "Django, Nomad"),
                ("Infrastructure", "Terraform"),
            ],
        ),
        ("PostgreSQL, and Redis.", ("PostgreSQL",), [("Data stores", "PostgreSQL, Redis")]),
        (
            "Message brokers (Kafka, RabbitMQ), React",
            ("PostgreSQL", "JavaScript"),
            [("Data stores", "Message brokers (Kafka, RabbitMQ)"), ("Web stack", "React")],
        ),
        ("gRPC services, React", ("JavaScript",), [("Web stack", "gRPC services, React")]),
    ],
)
def test_each_skill_item_is_filed_by_the_first_cited_entry_listing_it_whole(
    tmp_path, settings, guardrails, client_factory, text, cited, expected
) -> None:
    """An item matches a whole entry item, and one it cannot place stays with the first citation."""
    lines = _drafted_skills(
        tmp_path, settings, guardrails, client_factory, SHARED_SKILLS, (text, cited, {})
    )

    assert lines == expected


@pytest.mark.parametrize(
    "text", ["Grafana, Loki", "Familiar with: Grafana, Loki", "Tooling: Grafana, Loki"]
)
def test_an_entrys_own_sub_label_is_written_once_before_its_items(
    tmp_path, settings, guardrails, client_factory, text
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        "**Tooling:** Familiar with: Grafana, Loki\n\n**Platforms:** Kafka",
        (text, ("Familiar",), {}),
    )

    assert lines == [("Tooling", "Familiar with: Grafana, Loki")]


def test_an_item_listed_in_a_detail_is_written_under_the_details_own_sub_label(
    tmp_path, settings, guardrails, client_factory
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        "**Tooling**\n\n- Languages: Python, SQL, Go\n  - Familiar with: Rust, Zig\n\n"
        "**Platforms:** Kafka",
        ("Zig", ("Languages",), {}),
    )

    assert lines == [("Tooling", "Familiar with: Zig")]


@pytest.mark.parametrize(
    ("text", "items"),
    [
        ("familiar with Qux, Quux, and Corge", ["familiar with Qux, Quux, and Corge"]),
        (
            "Python, working knowledge of Qux, Quux, or Corge, Go",
            ["Python", "working knowledge of Qux, Quux, or Corge", "Go"],
        ),
        ("exposure to Qux, Quux; Corge, and Grault", ["exposure to Qux, Quux", "Corge", "Grault"]),
        ("familiar with Qux, Quux and Corge", ["familiar with Qux, Quux and Corge"]),
        ("familiar with Qux, Quux, Corge", ["familiar with Qux, Quux, Corge"]),
        (
            "familiar with Qux, Quux, as well as Corge",
            ["familiar with Qux, Quux, as well as Corge"],
        ),
        (
            "familiar with Qux and Corge, Python, Go",
            ["familiar with Qux and Corge", "Python", "Go"],
        ),
        (
            "testing of Qux, Quux corpora for Corge, and Grault",
            ["testing of Qux", "Quux corpora for Corge", "Grault"],
        ),
        ("Python, SQL, and Go", ["Python", "SQL", "Go"]),
        ("Alpha, Beta (gamma, delta)", ["Alpha", "Beta (gamma, delta)"]),
        (
            "familiar with ontologies, taxonomies, and thesauri",
            ["familiar with ontologies, taxonomies, and thesauri"],
        ),
        (
            "prototype work in dbt, Airflow, and Dagster",
            ["prototype work in dbt, Airflow, and Dagster"],
        ),
    ],
)
def test_a_series_led_by_a_lead_in_phrase_is_one_skill_item(text: str, items: list[str]) -> None:
    """A lead-in limits its series up to the final "and" or "or" item, within one semicolon."""
    assert skill_items(text) == items


def _home(item: str, listed: Entry) -> Entry | None:
    """Return the entry a drafted skills item is placed under, or None when none lists it."""
    [(_, listing)] = skill_placements(item, [listed])
    return listing[0] if listing else None


@pytest.mark.parametrize(
    ("text", "items"),
    [
        ("Ruby on Rails, Go, Rust, and ETL design", ["Ruby on Rails", "Go", "Rust", "ETL design"]),
        ("Bill of Materials, MRP, and ERP", ["Bill of Materials", "MRP", "ERP"]),
    ],
)
def test_a_capitalized_name_holding_a_preposition_is_never_a_lead_in(
    text: str, items: list[str]
) -> None:
    listed = Entry(id="skl-1", kind=EntryKind.SKILL, section="Skills", text=text)

    assert skill_items(text) == items
    assert _home(items[1], listed) is listed


@pytest.mark.parametrize(
    ("text", "items"),
    [
        (
            "applied modeling in clinical data warehouses, reference vocabularies and code sets",
            [
                "applied modeling in clinical data warehouses",
                "reference vocabularies and code sets",
            ],
        ),
        (
            "semantic tagging for AI-ready data, production NLP and entity linking",
            ["semantic tagging for AI-ready data", "production NLP and entity linking"],
        ),
        (
            "semantic tagging for AI-ready data, workflow prototyping",
            ["semantic tagging for AI-ready data", "workflow prototyping"],
        ),
    ],
)
def test_a_lowercase_multiword_phrase_is_no_name_a_lead_in_limits(
    text: str, items: list[str]
) -> None:
    listed = Entry(id="skl-1", kind=EntryKind.SKILL, section="Skills", text=text)

    assert skill_items(text) == items
    assert _home(items[1], listed) is listed


def test_a_lead_in_over_lowercase_multiword_concepts_splits_per_comma() -> None:
    assert skill_items("familiar with survival analysis, causal inference") == [
        "familiar with survival analysis",
        "causal inference",
    ]


def test_a_lead_in_qualifies_every_bare_name_through_an_unpunctuated_final_join() -> None:
    text = "threat modelling for Qux, Quux, Corge, Grault and Garply"

    assert skill_items(text) == [text]


@pytest.mark.parametrize(
    ("text", "items"),
    [
        (
            "familiar with Qux, Quux, and Corge for analytics",
            ["familiar with Qux, Quux", "Corge for analytics"],
        ),
        (
            "familiar with Qux, Quux, data modeling in dbt",
            ["familiar with Qux, Quux", "data modeling in dbt"],
        ),
    ],
)
def test_a_lead_in_keeps_its_bare_names_and_a_phrase_after_them_stands_alone(
    text: str, items: list[str]
) -> None:
    listed = Entry(id="skl-1", kind=EntryKind.SKILL, section="Skills", text=text)

    assert skill_items(text) == items
    assert _home("Quux", listed) is None
    assert _home(items[1], listed) is listed


@pytest.mark.parametrize(
    ("drafted", "placed"),
    [("Quux and Corge", False), ("Corge", False), ("familiar with Quux and Corge", True)],
)
def test_a_part_of_a_led_series_finds_its_home_only_under_its_lead_in(
    drafted: str, placed: bool
) -> None:
    listed = Entry(
        id="skl-1", kind=EntryKind.SKILL, section="Skills", text="familiar with Qux, Quux and Corge"
    )

    assert (_home(drafted, listed) is listed) is placed


def test_an_item_listed_in_a_skill_entrys_detail_finds_its_home() -> None:
    listed = Entry(
        id="skl-1",
        kind=EntryKind.SKILL,
        section="Skills",
        text="Python, SQL",
        details=("Qux, Quux",),
    )

    assert _home("Quux", listed) is listed


def test_a_capitalized_lead_in_splits_its_series_per_comma() -> None:
    assert skill_items("Familiar with Qux, Quux, and Corge") == [
        "Familiar with Qux",
        "Quux",
        "Corge",
    ]


def test_the_provider_schema_sweeps_cover_both_skills_shapes() -> None:
    from tests.support.schema import declares, production_schemas

    drafts = [schema for name, schema in production_schemas().items() if name.startswith("draft")]

    assert [declares(schema, "skills") for schema in drafts] == [True, True]
    assert sorted(declares(schema, "label") for schema in drafts) == [False, True]


def test_a_cv_without_skill_headings_keeps_the_models_label(
    tmp_path, settings, guardrails, client_factory
) -> None:
    lines = _drafted_skills(
        tmp_path,
        settings,
        guardrails,
        client_factory,
        "- Python\n- PostgreSQL",
        ("Python", ("Python",), {"label": "Languages"}),
    )

    assert lines == [("Languages", "Python")]


def test_a_skills_line_under_cv_headings_names_no_heading() -> None:
    schema = draft_schema(skills=True, labelled=False)

    with pytest.raises(ValidationError):
        schema.model_validate(
            {"summary": {"text": "x"}, "skills": [{"label": "Platforms", "text": "Kafka"}]}
        )


def test_the_letter_greeting_and_sign_off_are_written_by_the_program(
    inventory, settings, guardrails, client_factory
) -> None:
    from clearscout.application.tailoring.stages.draft import cover_letter

    plan, kafka, _ = plan_for(inventory, settings)
    paragraph = {"text": "I rebuilt ingestion on Kafka.", "source_ids": [kafka]}
    client = client_factory({"CoverLetter": lambda _: {"paragraphs": [paragraph]}})

    named = cover_letter(client, inventory, spec(), plan, guardrails, employer="Example Logistics")
    unnamed = cover_letter(client, inventory, spec(), plan, guardrails)

    assert named.greeting == "Dear Example Logistics hiring team,"
    # The spec's company is model text whenever no stored name was passed.
    assert unnamed.greeting == "Dear hiring team,"
    assert named.closing == f"Sincerely,\n\n{inventory.name}"
    assert [line.text for line in named.paragraphs] == ["I rebuilt ingestion on Kafka."]


def test_the_letter_file_opens_on_a_letterhead_copied_from_the_master_cv(
    inventory, settings, guardrails, client_factory
) -> None:
    """The letter policy wants a name heading and one contact line, and the model writes neither."""
    from clearscout.application.tailoring.stages.draft import cover_letter

    plan, kafka, _ = plan_for(inventory, settings)
    record = inventory.model_copy(
        update={
            "contact_lines": (
                "Work Authorization: Authorized to work in the United States",
                "ORCID: orcid.org/0000-0002-1825-0097",
                "Lisbon, Portugal | ana.ferreira@example.com",
                "Languages: Portuguese, English",
            )
        }
    )
    settings.header_drop = ("ORCID",)
    paragraph = {"text": "I rebuilt ingestion on Kafka.", "source_ids": [kafka]}
    client = client_factory({"CoverLetter": lambda _: {"paragraphs": [paragraph]}})

    letter = cover_letter(client, record, spec(), plan, guardrails, employer="Globex")

    assert letter.markdown() == (
        f"# {record.name}\n\nLisbon, Portugal | ana.ferreira@example.com\n\n{letter.rendered()}"
    )
    # The critic and the checks read only what the model wrote around the program's frame.
    assert letter.rendered() == (
        "Dear Globex hiring team,\n\nI rebuilt ingestion on Kafka.\n\n"
        f"Sincerely,\n\n{record.name}\n"
    )
    assert letter.cited_body() == f"Paragraph 1, source_ids: {kafka}\nI rebuilt ingestion on Kafka."


def test_the_program_types_each_letter_citation_by_where_its_id_came_from(
    inventory, settings, guardrails, client_factory
) -> None:
    from clearscout.application.tailoring.stages.draft import cover_letter
    from clearscout.application.tailoring.stages.verify import check_letter
    from clearscout.infrastructure.web_fetch.company_research import ResearchSource
    from clearscout.shared.results import Outcome

    plan, kafka, _ = plan_for(inventory, settings)
    research = (
        ResearchSource(
            id="company.about",
            kind="about",
            url="https://company.example/about",
            text="Globex moves freight for regional grocers.",
            method="scripted",
        ),
    )
    paragraphs = [
        {"text": "I rebuilt ingestion on Kafka.", "source_ids": [kafka]},
        {"text": "Globex moves freight for regional grocers.", "source_ids": ["company.about"]},
    ]
    client = client_factory({"CoverLetter": lambda _: {"paragraphs": paragraphs}})

    letter = cover_letter(client, inventory, spec(), plan, guardrails, research=research)
    report = check_letter(
        letter=letter,
        plan=plan,
        inventory=inventory,
        guardrails=guardrails,
        research=research,
    )

    assert [line.source_types for line in letter.paragraphs] == [
        (SourceType.CANDIDATE,),
        (SourceType.COMPANY,),
    ]
    provenance = next(item for item in report.findings if item.check == "letter provenance")
    assert provenance.outcome is Outcome.PASS


def test_the_letter_request_shows_every_drafted_entry_without_resume_layout_instructions(
    inventory, settings, guardrails, client_factory
) -> None:
    """The letter reads the resume's evidence, but not the resume's bullet counts."""
    from clearscout.application.tailoring.stages.draft import cover_letter
    from tests.support.payloads import script

    plan, _, _ = plan_for(inventory, settings)
    *recent, oldest = plan.recent_roles
    plan = plan.model_copy(update={"recent_roles": tuple(recent), "earlier_roles": (oldest,)})
    client = client_factory(script({}))

    cover_letter(client, inventory, spec(), plan, guardrails)

    task = client.provider.calls[0][1].task.text
    assert [entry_id for entry_id in plan.drafted_ids() if f"[{entry_id}]" not in task] == []
    assert "answers r01" in task
    assert "bullet(s) here" not in task
    assert "Summarise these" not in task
    assert "do not write bullets" not in task


@pytest.mark.parametrize("schema", [cover_letter_schema(), draft_schema(skills=False)])
def test_no_writing_schema_asks_the_model_for_source_types(schema) -> None:
    line = {"text": "x", "source_ids": ["b-1"], "source_types": ["candidate"]}
    answer = {"paragraphs": [line]} if "Letter" in schema.__name__ else {"summary": line}
    with pytest.raises(ValidationError):
        schema.model_validate(answer)


@pytest.mark.parametrize("earlier", [False, True])
def test_the_drafter_is_asked_for_an_earlier_career_line_only_when_an_earlier_role_prints(
    inventory, settings, guardrails, client_factory, earlier
) -> None:
    from clearscout.application.tailoring.stages.draft import draft
    from tests.support.payloads import script
    from tests.support.schema import declares

    plan, _, _ = plan_for(inventory, settings)
    assert not plan.earlier_roles, "the fixture must select every role as recent"
    if earlier:
        *recent, oldest = plan.recent_roles
        plan = plan.model_copy(update={"recent_roles": tuple(recent), "earlier_roles": (oldest,)})
    asked: list[bool] = []
    answers = script({})
    resume = answers["ResumeDraft"]

    def capture(schema, request):
        asked.append(declares(schema, "earlier_career"))
        return resume(schema, request)

    answers["ResumeDraft"] = capture
    drafted = draft(client_factory(answers), inventory, spec(), plan, guardrails)

    assert asked == [earlier]
    assert (drafted.earlier_career is not None) is earlier


def test_production_draft_request_carries_requirement_meaning(
    inventory, settings, guardrails, client_factory
):
    from clearscout.application.tailoring.stages.draft import draft
    from tests.support.payloads import script

    target = spec().model_copy(
        update={
            "requirements": (
                Requirement(
                    id="r01",
                    text="Own incident response for distributed event ingestion",
                    importance=Importance.MUST_HAVE,
                ),
            )
        }
    )
    plan, _, _ = plan_for(inventory, settings)
    client = client_factory(script({}))
    draft(client, inventory, target, plan, guardrails)
    request = client.provider.calls[0][1]
    assert target.requirements[0].text in request.task.text
    assert "must_have" in request.task.text
    assert "answers r01" in request.task.text
