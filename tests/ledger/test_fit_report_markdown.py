"""Stored fit-report Markdown preserves the distinction between gaps and incomplete runs."""

from __future__ import annotations

from decimal import Decimal

from clearscout.application.contracts import EvaluationCommit
from clearscout.application.job_search.fit_reports import render_fit_report
from tests.support.scenarios import evaluation_commit


def test_markdown_reports_unknown_cost_without_claiming_it_was_free() -> None:
    from clearscout.application.contracts import ReportCostInput

    proposal = evaluation_commit("workspace")
    proposal = proposal.model_copy(
        update={
            "fit_report": proposal.fit_report.model_copy(
                update={
                    "costs": [ReportCostInput(stage="match", model="unpriced", amount=None)],
                }
            ),
        }
    )
    markdown = render_fit_report(
        proposal,
        evidence_ids={"posting-location": "evidence-1", "candidate-platform": "evidence-2"},
        weights={name: Decimal("0.2") for name in proposal.scores.named()},
        weight_set_name="Default",
        weight_set_version=1,
    )
    cost_section = markdown.split("## Cost", 1)[1]
    assert "Unknown" in cost_section
    assert "0 USD" not in cost_section
    assert "None USD" not in cost_section


def test_markdown_lists_unestablished_requirements_as_needs_review_not_gaps() -> None:
    proposal = evaluation_commit("workspace")
    requirement = proposal.fit_report.requirements[0].model_copy(
        update={"evidence_keys": [], "established": False}
    )
    proposal = proposal.model_copy(
        update={
            "fit_report": proposal.fit_report.model_copy(update={"requirements": [requirement]})
        }
    )

    markdown = render_fit_report(
        proposal,
        evidence_ids={"posting-location": "evidence-1"},
        weights={name: Decimal("0.2") for name in proposal.scores.named()},
        weight_set_name="Default",
        weight_set_version=1,
    )
    gaps = markdown.split("## Needs review", maxsplit=1)[0].split("## Gaps", maxsplit=1)[1]

    assert "Lead a data platform" not in gaps
    assert "## Needs review" in markdown
    assert "Lead a data platform" in markdown.split("## Needs review", maxsplit=1)[1]


def test_markdown_lists_an_unestablished_hard_gate_for_the_person_to_decide() -> None:
    proposal = evaluation_commit("workspace")
    gate = proposal.fit_report.hard_gates[0].model_copy(
        update={"text": "Doctor of Pharmacy required", "established": False}
    )
    proposal = proposal.model_copy(
        update={"fit_report": proposal.fit_report.model_copy(update={"hard_gates": [gate]})}
    )

    markdown = render_fit_report(
        proposal,
        evidence_ids={"posting-location": "evidence-1", "candidate-platform": "evidence-2"},
        weights={name: Decimal("0.2") for name in proposal.scores.named()},
        weight_set_name="Default",
        weight_set_version=1,
    )
    review = markdown.split("## Needs review", maxsplit=1)[1]

    assert "Doctor of Pharmacy required" in review
    assert "Decide whether you meet it." in review


def _render(proposal: EvaluationCommit) -> str:
    return render_fit_report(
        proposal,
        evidence_ids={"posting-location": "evidence-1", "candidate-platform": "evidence-2"},
        weights={name: Decimal("0.2") for name in proposal.scores.named()},
        weight_set_name="Default",
        weight_set_version=1,
    )


def test_answered_by_lists_record_evidence_apart_from_the_requirement_posting_span() -> None:
    proposal = evaluation_commit("workspace")
    answered = proposal.fit_report.requirements[0].model_copy(
        update={"evidence_keys": ["candidate-platform", "posting-location"]}
    )
    proposal = proposal.model_copy(
        update={
            "fit_report": proposal.fit_report.model_copy(
                update={"requirements": [answered, proposal.fit_report.requirements[1]]}
            )
        }
    )

    markdown = _render(proposal)
    table = markdown.split("## Requirement map", 1)[1].split("##", 1)[0]
    gates = markdown.split("## Hard gates", 1)[1].split("##", 1)[0]

    assert "| Answered by | Posting text |" in table
    assert "| Lead a data platform | needs_review | evidence-2 | evidence-1 |" in table
    assert "| Canada remote | needs_review | not stated | evidence-1 |" in gates


def test_reading_table_names_posting_and_record_sources_as_quoted_text() -> None:
    proposal = evaluation_commit("workspace")
    reading = proposal.fit_report.reading[0].model_copy(
        update={"question": "Years the record covers", "evidence_keys": ["candidate-platform"]}
    )
    proposal = proposal.model_copy(
        update={
            "fit_report": proposal.fit_report.model_copy(
                update={"reading": [*proposal.fit_report.reading, reading]}
            )
        }
    )

    section = _render(proposal).split("## Read from the posting and record by rule", 1)[1]

    assert "| Question | Answer | State | Source |" in section
    assert "evidence-1 (posting): Canada remote" in section
    assert "evidence-2 (record): data platform" in section
