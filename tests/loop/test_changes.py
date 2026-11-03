"""The review artifact: what it says about a draft, and what its digest covers."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.tailoring import changes
from clearscout.application.tailoring.changes import Kind
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.runs import RunIdentity
from clearscout.application.tailoring.stages.draft import (
    DraftedLine,
    DraftedRole,
    DraftedSkill,
    ResumeDraft,
)
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.stages.select import build_plan
from clearscout.shared.results import Finding, Outcome

IDENTITY = RunIdentity(
    posting_id="req-4821",
    cv_hash="cvhash00",
    prompt_suite="prompts0",
    settings_hash="settings",
    version="9.9.9",
)


def entry_id(inventory: CvInventory, fragment: str) -> str:
    return next(entry.id for entry in inventory.entries if entry.text.startswith(fragment))


def make_spec() -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(
            Requirement(id="r01", text="Kafka ingestion", importance=Importance.MUST_HAVE),
        ),
        keywords=("Kafka",),
        hard_gates=(),
        origin="test",
    )


def make_evidence(kafka: str) -> EvidenceMap:
    return EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(kafka,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )


def review_for(
    inventory: CvInventory,
    settings,
    guardrails: Guardrails,
    draft: ResumeDraft,
    *,
    identity: RunIdentity = IDENTITY,
    excluded: frozenset[str] = frozenset(),
) -> changes.Review:
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    spec = make_spec()
    evidence = make_evidence(kafka)
    plan = build_plan(inventory, spec, evidence, settings)
    return changes.review(
        identity=identity,
        draft=draft,
        plan=plan,
        inventory=inventory,
        spec=spec,
        evidence=evidence,
        settings=settings,
        guardrails=guardrails,
        excluded=excluded,
    )


def one_line_draft(inventory: CvInventory, text: str) -> ResumeDraft:
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    role_key = next(role.key for role in inventory.roles)
    return ResumeDraft(
        summary=DraftedLine(text=text, source_ids=(kafka,)),
        skills=(DraftedSkill(label="Platforms", text="Kafka, Python", source_ids=(kafka,)),),
        roles=(
            DraftedRole(
                role_key=role_key,
                bullets=(DraftedLine(text="Rebuilt ingestion on Kafka.", source_ids=(kafka,)),),
            ),
        ),
    )


def find(review: changes.Review, slot: str) -> changes.Proposal:
    return next(proposal for proposal in review.proposals if proposal.slot == slot)


def test_every_drafted_line_carries_evidence_excerpts_and_a_rationale(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds streaming platforms for regulated work.")
    review = review_for(inventory, settings, guardrails, draft)
    summary = find(review, "summary")

    assert summary.kind is Kind.REFRAMED
    assert summary.evidence_ids == (entry_id(inventory, "Rebuilt the ingestion tier"),)
    assert "Rebuilt the ingestion tier" in summary.evidence[0].text
    assert summary.rationale.startswith("Rewrites ")
    assert "streaming" in summary.rationale


def test_a_rewrite_that_adds_no_word_and_a_line_citing_nothing_both_say_so(
    inventory, settings, guardrails
) -> None:
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    source = inventory.by_id(kafka).text
    draft = ResumeDraft(
        summary=DraftedLine(text=" ".join(source.split()[:6]), source_ids=(kafka,)),
        skills=(DraftedSkill(label="Platforms", text="Kafka", source_ids=()),),
        roles=(
            DraftedRole(
                role_key="a-role-the-plan-does-not-carry",
                bullets=(DraftedLine(text=source, source_ids=(kafka,)),),
            ),
        ),
    )
    review = review_for(inventory, settings, guardrails, draft)

    assert "Uses no word its cited evidence does not." in find(review, "summary").rationale
    assert "Cites no evidence" in find(review, "skills[0]").rationale
    assert find(review, "skills[0]").kind is Kind.NEW
    # A drafted role the plan does not carry is still reviewed, at the end.
    assert (
        find(review, "experience/a-role-the-plan-does-not-carry/bullet[0]").kind is Kind.PRESERVED
    )


def test_a_figure_in_rewritten_text_is_named_for_a_reviewer_to_check(
    inventory, settings, guardrails
) -> None:
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    figure = next(part for part in inventory.by_id(kafka).text.split() if part.isdigit())
    draft = ResumeDraft(
        summary=DraftedLine(text=f"Rebuilt ingestion across {figure} feeds.", source_ids=(kafka,)),
    )
    review = review_for(inventory, settings, guardrails, draft)

    assert (
        f"Carries {figure}, each present in the cited evidence."
        in find(review, "summary").rationale
    )


def test_a_line_answering_a_requirement_names_it(inventory, settings, guardrails) -> None:
    draft = one_line_draft(inventory, "Rebuilt ingestion.")
    review = review_for(inventory, settings, guardrails, draft)
    summary = find(review, "summary")

    assert summary.requirement_ids == ("r01",)
    assert "r01" in summary.rationale
    assert "must_have" in summary.rationale
    assert "direct" in summary.rationale


def test_a_truncated_requirement_quote_ends_on_a_whole_word_with_an_ellipsis(
    inventory, settings, guardrails
) -> None:
    """The rationale is the approval surface, so it is never cut mid-word."""
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    long_text = (
        "Establish product principles for how external data contributes to the "
        "platform's longitudinal record and how data is shared across the ecosystem, "
        "including which sources are ingested, how identity and provenance are "
        "resolved, and how downstream consumers are told about quality and meaning"
    )
    spec = make_spec().model_copy(
        update={
            "requirements": (
                Requirement(id="r01", text=long_text, importance=Importance.MUST_HAVE),
            )
        }
    )
    evidence = make_evidence(kafka)
    plan = build_plan(inventory, spec, evidence, settings)
    review = changes.review(
        identity=IDENTITY,
        draft=one_line_draft(inventory, "Rebuilt ingestion."),
        plan=plan,
        inventory=inventory,
        spec=spec,
        evidence=evidence,
        settings=settings,
        guardrails=guardrails,
        excluded=frozenset(),
    )
    rationale = find(review, "summary").rationale

    quote = rationale.split(": ", 1)[1].split("...", 1)[0]
    assert f"{quote}..." in rationale
    assert quote.split()[-1] in long_text.split()


def test_verbatim_text_is_preserved_and_a_join_of_two_entries_is_new(
    inventory, settings, guardrails
) -> None:
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    lineage = entry_id(inventory, "Built the lineage service")
    role_key = next(role.key for role in inventory.roles)
    copied = inventory.by_id(kafka).text
    draft = ResumeDraft(
        summary=DraftedLine(text=copied, source_ids=(kafka,)),
        roles=(
            DraftedRole(
                role_key=role_key,
                bullets=(
                    DraftedLine(
                        text="Ingestion and lineage together.", source_ids=(kafka, lineage)
                    ),
                ),
            ),
        ),
    )
    review = review_for(inventory, settings, guardrails, draft)

    assert find(review, "summary").kind is Kind.PRESERVED
    assert find(review, f"experience/{role_key}/bullet[0]").kind is Kind.NEW


def test_identity_history_and_copied_entries_are_marked_immutable(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    review = review_for(inventory, settings, guardrails, draft)
    immutable = {proposal.slot for proposal in review.proposals if proposal.immutable}

    assert "header/name" in immutable
    assert any(slot.startswith("education[") for slot in immutable)
    assert not any(slot.startswith("experience/") for slot in immutable)


def test_selected_evidence_no_line_used_is_reported_as_omitted(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    review = review_for(inventory, settings, guardrails, draft)
    unused = [proposal for proposal in review.proposals if proposal.slot.startswith("unused/")]

    assert unused
    assert all(proposal.kind is Kind.OMITTED for proposal in unused)
    assert all("used by no line" in proposal.rationale for proposal in unused)


def test_a_trimmed_publication_is_omitted_rather_than_silently_absent(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    with_papers = settings.model_copy(update={"min_publications": 1})
    full = review_for(inventory, with_papers, guardrails, draft)
    publication = find(full, "publications[0]")
    dropped = review_for(
        inventory,
        with_papers,
        guardrails,
        draft,
        excluded=frozenset({publication.evidence_ids[0]}),
    )

    assert find(dropped, "publications[0]").kind is Kind.OMITTED
    assert "page budget" in find(dropped, "publications[0]").rationale


def test_the_digest_moves_when_anything_the_proposal_rests_on_moves(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    review = review_for(inventory, settings, guardrails, draft)
    original = find(review, "summary")

    other_cv = review_for(
        inventory,
        settings,
        guardrails,
        draft,
        identity=replace(IDENTITY, cv_hash="different"),
    )
    other_posting = review_for(
        inventory,
        settings,
        guardrails,
        draft,
        identity=replace(IDENTITY, posting_id="req-0000"),
    )
    other_prompts = review_for(
        inventory,
        settings,
        guardrails,
        draft,
        identity=replace(IDENTITY, prompt_suite="different"),
    )
    other_settings = review_for(
        inventory,
        settings,
        guardrails,
        draft,
        identity=replace(IDENTITY, settings_hash="different"),
    )
    other_text = review_for(
        inventory, settings, guardrails, one_line_draft(inventory, "Builds other platforms.")
    )

    moved = {
        find(other_cv, "summary").digest,
        find(other_posting, "summary").digest,
        find(other_prompts, "summary").digest,
        find(other_settings, "summary").digest,
        find(other_text, "summary").digest,
    }
    assert original.digest not in moved
    assert len(moved) == 5


def test_the_digest_ignores_the_approval_a_run_was_asked_for(
    inventory, settings, guardrails
) -> None:
    """A proposal is what drafting produced, so an approval cannot move its digest.

    If it did, the approval could never be checked against the proposals it was
    written about: applying it would change the very digests it carries.
    """
    draft = one_line_draft(inventory, "Builds platforms.")
    plain = review_for(inventory, settings, guardrails, draft)
    approved = review_for(
        inventory,
        settings,
        guardrails,
        draft,
        identity=replace(IDENTITY, approval="abc123"),
    )

    assert plain.digest == approved.digest


def test_the_set_digest_notices_a_proposal_added_or_removed() -> None:
    run = {"cv_hash": "a"}
    assert changes.set_digest(run, ("x", "y")) != changes.set_digest(run, ("x",))
    assert changes.set_digest(run, ("x", "y")) != changes.set_digest(run, ("y", "x"))


def test_rebuild_removes_rejected_lines_and_keeps_the_rest(inventory, settings, guardrails) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    role_key = next(role.key for role in inventory.roles)
    rebuilt = changes.rebuild(
        draft,
        {"skills[0]": None, f"experience/{role_key}/bullet[0]": None},
    )

    assert rebuilt.skills == ()
    assert rebuilt.roles[0].bullets == ()
    assert rebuilt.summary.text == "Builds platforms."


@pytest.mark.parametrize(
    ("replacement", "items"),
    [
        ("Platforms: Kafka", "Kafka"),
        (" platforms : Kafka", "Kafka"),
        ("Ratios 3:1 and 5:2 reporting", "Ratios 3:1 and 5:2 reporting"),
    ],
)
def test_a_skills_replacement_drops_only_the_lines_own_heading(
    inventory, settings, guardrails, replacement, items
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    rebuilt = changes.rebuild(
        draft, {"skills[0]": DraftedLine(text=replacement, source_ids=(kafka,))}
    )

    assert rebuilt.skills[0].label == "Platforms"
    assert rebuilt.skills[0].text == items


def test_an_emptied_summary_removes_the_section_rather_than_leaving_a_heading(
    inventory, settings, guardrails
) -> None:
    from clearscout.application.tailoring.stages.draft import Document

    draft = one_line_draft(inventory, "Builds platforms.")
    kafka = entry_id(inventory, "Rebuilt the ingestion tier")
    spec = make_spec()
    plan = build_plan(inventory, spec, make_evidence(kafka), settings)
    rebuilt = changes.rebuild(draft, {"summary": None})
    markdown = Document(rebuilt, inventory, plan, settings, guardrails).markdown()

    assert f"## {settings.sections.summary}" not in markdown
    assert rebuilt.summary.text == ""
    assert rebuilt.summary not in rebuilt.lines()


def test_a_role_the_draft_writes_nothing_for_is_reported_as_emptied(
    inventory, settings, guardrails
) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    role_key = next(role.key for role in inventory.roles)
    spec = make_spec()
    plan = build_plan(inventory, spec, make_evidence(entry_id(inventory, "Rebuilt")), settings)
    rebuilt = changes.rebuild(draft, {f"experience/{role_key}/bullet[0]": None})

    assert role_key in changes.emptied_roles(rebuilt, plan)


def test_both_artifacts_render_the_review(inventory, settings, guardrails) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    review = review_for(inventory, settings, guardrails, draft)
    rendered = changes.render(review)

    assert review.digest in rendered.markdown_text
    assert "Builds platforms." in rendered.markdown_text

    payload = json.loads(rendered.json_text)
    assert payload["schema"] == changes.SCHEMA
    assert payload["run"]["posting_id"] == "req-4821"
    assert all(proposal["decision"] == "accept" for proposal in payload["proposals"])


def test_recorded_decisions_reach_both_artifacts(inventory, settings, guardrails) -> None:
    draft = one_line_draft(inventory, "Builds platforms.")
    review = review_for(inventory, settings, guardrails, draft)
    target = find(review, "summary")
    rendered = changes.render(review, {target.id: "reject"})
    payload = json.loads(rendered.json_text)
    decided = next(item for item in payload["proposals"] if item["id"] == target.id)

    assert decided["decision"] == "reject"
    assert "reject" in rendered.markdown_text


@pytest.mark.parametrize(
    ("check", "outcome", "detail"),
    [
        ("factual review", Outcome.WARN, "possibly unrelated citation: {text}"),
        ("repeated phrasing", Outcome.WARN, "reused phrasing in {text!r}"),
    ],
)
def test_a_line_a_check_warned_about_defaults_to_review(
    inventory, settings, guardrails, check, outcome, detail
) -> None:
    draft = one_line_draft(inventory, "Builds streaming platforms for regulated work.")
    review = review_for(inventory, settings, guardrails, draft)
    flagged = find(review, "summary")
    findings = (
        Finding(check, outcome, detail.format(text=flagged.text[:80])),
        Finding("factual review", Outcome.WARN, "Human factual review is required."),
    )

    rendered = changes.render(review, findings=findings)

    stored = json.loads(rendered.json_text)
    decisions = {item["id"]: item["decision"] for item in stored["proposals"]}
    assert decisions[flagged.id] == changes.REVIEW
    assert {decisions[key] for key in decisions if key != flagged.id} == {"accept"}
    assert f"## {flagged.id}  summary\n\n- Kind: reframed, review" in rendered.markdown_text


def test_a_line_only_a_critic_advisory_quotes_stays_accepted(
    inventory, settings, guardrails
) -> None:
    """An advisory is a note for the reader, so it does not hold a line back for review."""
    draft = one_line_draft(inventory, "Builds streaming platforms for regulated work.")
    review = review_for(inventory, settings, guardrails, draft)
    quoted = find(review, "summary")
    advisory = f'"{quoted.text[:80]}" opens with a stacked adjective.'
    findings = (
        Finding("critic", Outcome.WARN, "alignment 4/5", redraftable=False, advisories=(advisory,)),
    )

    rendered = changes.render(review, findings=findings)

    stored = json.loads(rendered.json_text)
    assert {item["decision"] for item in stored["proposals"]} == {"accept"}
