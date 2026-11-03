"""A requirement the run could not settle is not a finding about the CV.

Every surface that reports a gap has to tell the two apart, or the product
states something false about the candidate whenever a model result goes missing.
"""

from __future__ import annotations

from clearscout.application.tailoring.batch.analysis import Verdict, recommend
from clearscout.application.tailoring.stages.jobspec import Importance, JobSpec, Requirement
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.domain import EvidenceStatus, GapClass
from tests.support.reports import rendered_report, report_section


def spec_for(*requirements: tuple[str, str, Importance]) -> JobSpec:
    return JobSpec(
        title="Staff Data Platform Engineer",
        company="Globex",
        seniority="staff",
        location="Remote",
        requirements=tuple(
            Requirement(id=identifier, text=text, importance=importance)
            for identifier, text, importance in requirements
        ),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def unestablished(requirement_id: str) -> Match:
    return Match(
        requirement_id=requirement_id,
        classification=Classification.GAP,
        strength=Strength.NONE,
        note="Model result was missing; treated as a gap.",
        evidence_status=EvidenceStatus.UNRESOLVED,
        gap_class=GapClass.INCOMPLETE_EVIDENCE,
    )


def unanswered(requirement_id: str) -> Match:
    return Match(
        requirement_id=requirement_id,
        classification=Classification.GAP,
        strength=Strength.NONE,
        note="No supported evidence was returned.",
        evidence_status=EvidenceStatus.UNRESOLVED,
        gap_class=GapClass.UNSUPPORTED_CORE,
    )


def test_the_report_keeps_an_unestablished_requirement_out_of_the_gaps_table() -> None:
    spec = spec_for(("r01", "Kafka", Importance.MUST_HAVE), ("r02", "Rust", Importance.MUST_HAVE))
    evidence = EvidenceMap(matches=(unestablished("r01"), unanswered("r02")))

    report = rendered_report(spec=spec, evidence=evidence)
    gaps = report_section(report, "Gaps")
    review = report_section(report, "Needs review")

    assert "Rust" in gaps
    assert "Kafka" not in gaps
    assert "Kafka" in review
    assert "Rust" not in review
    assert "not gaps and not answered" in review


def test_the_report_writes_no_review_section_when_every_row_came_back() -> None:
    spec = spec_for(("r01", "Rust", Importance.MUST_HAVE))
    evidence = EvidenceMap(matches=(unanswered("r01"),))

    assert report_section(rendered_report(spec=spec, evidence=evidence), "Needs review") == ""


def test_the_report_names_an_identifier_the_posting_never_had() -> None:
    """A discarded row reads as itself rather than as a requirement nobody answered."""
    spec = spec_for(("r01", "Rust", Importance.MUST_HAVE))
    evidence = EvidenceMap(matches=(unanswered("r01"),), unrecognized=("r14",))

    written = report_section(rendered_report(spec=spec, evidence=evidence), "Discarded answers")

    assert "r14" in written
    assert "this posting does not have" in written
    recognized = EvidenceMap(matches=(unanswered("r01"),))
    assert (
        report_section(rendered_report(spec=spec, evidence=recognized), "Discarded answers") == ""
    )


def test_a_recommendation_separates_what_the_record_lacks_from_what_the_run_missed() -> None:
    spec = spec_for(("r01", "Kafka", Importance.MUST_HAVE))
    evidence = EvidenceMap(matches=(unestablished("r01"),))

    verdict = recommend(spec, evidence)

    assert verdict.verdict is Verdict.CAUTIOUS
    assert any("did not establish it either way" in reason for reason in verdict.reasons)
    assert not any("the record does not answer it" in reason for reason in verdict.reasons)


def test_a_recommendation_still_says_the_record_lacks_a_stated_essential() -> None:
    spec = spec_for(("r01", "Rust", Importance.MUST_HAVE))
    evidence = EvidenceMap(matches=(unanswered("r01"),))

    verdict = recommend(spec, evidence)

    assert verdict.verdict is Verdict.CAUTIOUS
    assert any("the record does not answer it" in reason for reason in verdict.reasons)
