"""Translation of screening findings into the stored evaluation commit."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from clearscout.application import job_search
from clearscout.application.contracts import EvaluationCommit
from clearscout.application.discovery.posting_text import publisher_closing_date
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.job_search.fit_reports import render_fit_report
from clearscout.application.job_search.models import ClosingDateEditInput
from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.fit.structure import Answer
from clearscout.application.tailoring.stages.jobspec import (
    HardGate,
    Importance,
    JobSpec,
    Requirement,
)
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.stages.spans import locate
from clearscout.application.tailoring.workflows.evaluation_commit import (
    EvaluationCommitInputs,
    build_evaluation_commit,
    build_refusal_commit,
)
from clearscout.application.tailoring.workflows.models import EvaluationOutput
from clearscout.domain.records import EvaluationDecision
from clearscout.domain.search_parameters import Acceptance, Arrangement, JobSearchParameters
from clearscout.domain.staged_facts import PublisherClosingDate
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import JobPosting, PendingDiscovery
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.costs import CallManifest, Ledger, Usage
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import evaluation_commit, scan_workspace, seed_pending_discovery

POSTING = "# Platform Director\n\n## Requirements\n- Lead a governed data platform"
CANDIDATE = "Led a governed data platform for Globex."


def _output(reasons: tuple[str, ...]) -> EvaluationOutput:
    inventory = CvInventory(source=Path("master.md"), content_hash="abcd1234", name="Candidate")
    ledger = Ledger()
    ledger.record(CallManifest("jobspec", "model-a", Usage(input_tokens=20, output_tokens=8)))
    return EvaluationOutput(
        spec=JobSpec(
            title="Platform Director",
            company="Globex",
            seniority="director",
            location="Canada",
            requirements=(
                Requirement(
                    id="R1",
                    text="Lead a governed data platform",
                    importance=Importance.MUST_HAVE,
                ),
            ),
            keywords=("data platform",),
            hard_gates=(),
            origin="posting.txt",
        ),
        evidence=EvidenceMap(),
        inventory=inventory,
        ledger=ledger,
        jd=JDText(text=POSTING, origin="posting.txt", method="direct_text"),
        reading=fit.read(POSTING, inventory, today=date(2026, 8, 11)),
        outcome="watch",
        reasons=reasons,
        prompt_fingerprint="prompt-suite",
    )


def _inputs() -> EvaluationCommitInputs:
    moment = datetime(2026, 8, 11, 9, tzinfo=UTC)
    return EvaluationCommitInputs(
        workspace_id="workspace-1",
        supersedes_evaluation_id=None,
        employer="Globex",
        title="Platform Director",
        location="Canada",
        requisition_id="REQ-1",
        canonical_url="https://example.invalid/jobs/1",
        discovery_url="https://example.invalid/jobs/1",
        first_seen_at=moment,
        observed_at=moment,
        posting_status="active",
        residence_rule="",
        employment_type="permanent",
        payroll_arrangement="employee",
        source_timestamp=None,
        extraction_method="direct_text",
        fetched_at=moment,
        http_status=200,
        robots_basis=None,
        master_content=CANDIDATE.encode("utf-8"),
        master_source_uri="file:///master.md",
        master_effective_at=moment,
    )


@pytest.mark.parametrize(
    ("valid_through", "label"),
    [
        ("2026-08-10", ""),
        ("2026-08-28T15:00:00-07:00", ""),
        ("2026-08-27", "Closing date: 2026-09-15"),
        ("", ""),
    ],
)
def test_commit_translation_preserves_closing_metadata_without_changing_the_evaluation(
    valid_through: str, label: str
) -> None:
    inputs = _inputs()
    closing_date = publisher_closing_date(
        label,
        valid_through=valid_through,
        source_url=inputs.canonical_url,
        observed_at=inputs.observed_at,
    ).with_artifacts(body_artifact_id="body-evidence", publisher_artifact_id="publisher-evidence")
    output = _output(("Keep the source statement.",))
    baseline = build_evaluation_commit(inputs, output)

    proposal = build_evaluation_commit(replace(inputs, publisher_closing_date=closing_date), output)

    assert proposal.posting_body.publisher_closing_date is not None
    assert (
        PublisherClosingDate.from_json(
            proposal.posting_body.publisher_closing_date.model_dump(mode="json")
        )
        == closing_date
    )
    assert proposal.model_dump(exclude={"posting_body"}) == baseline.model_dump(
        exclude={"posting_body"}
    )
    assert proposal.posting_body.content == baseline.posting_body.content


def test_the_stored_eligibility_detail_reads_the_arrangement_from_the_arrangement_answer() -> None:
    """The Arrangement label carries the arrangement answer, not the job title."""
    inventory = CvInventory(source=Path("master.md"), content_hash="abcd1234", name="Candidate")
    posting = "# Staff Engineer, Toronto\n\n## Requirements\n\n- Hybrid two days a week."
    reading = fit.read(
        posting,
        inventory,
        parameters=JobSearchParameters(arrangement=Arrangement(remote=Acceptance.PREFERRED)),
        today=date(2026, 8, 11),
    )

    commit = build_evaluation_commit(_inputs(), replace(_output(("watch",)), reading=reading))

    stated = commit.eligibility.details.split("Arrangement:", 1)[1]
    assert reading.arrangement_fit.because() in stated
    assert reading.location.because() not in stated


def test_the_rationale_ends_each_reason_before_the_next_one_starts() -> None:
    output = _output(
        (
            "Premium-looking but gated",
            "Unmatched requirements are unsupported by the selected CV evidence.",
            "Is the arrangement stated anywhere?",
            "The posting and the stored eligibility conclusion differ",
        )
    )

    commit = build_evaluation_commit(_inputs(), output)

    assert commit.rationale == (
        "Premium-looking but gated. "
        "Unmatched requirements are unsupported by the selected CV evidence. "
        "Is the arrangement stated anywhere? "
        "The posting and the stored eligibility conclusion differ"
    )


def test_the_rationale_names_the_outcome_when_the_run_gave_no_reason() -> None:
    commit = build_evaluation_commit(_inputs(), replace(_output(()), reasons=("", "   ")))

    assert commit.rationale == "Evaluation outcome: watch."


def test_the_staged_identity_is_kept_over_the_extractor_reading() -> None:
    """The model read "Platform Director" in "Canada"; the ledger keeps the staged text."""
    staged = replace(
        _inputs(),
        title="VP, Platform Director",
        employer="Globex Health",
        location="Toronto, Ontario",
    )

    commit = build_evaluation_commit(staged, _output(("watch",)))

    assert (commit.title, commit.employer, commit.location) == (
        "VP, Platform Director",
        "Globex Health",
        "Toronto, Ontario",
    )


def _placed(stated_rule: str) -> tuple[EvaluationCommitInputs, EvaluationOutput]:
    """A pursued posting the model placed in a declared market, beside a stated rule."""
    output = _output(("pursue",))
    place_fit = Answer(
        question="Where you could work this",
        value="canada_remote or us_remote: authorized",
        evidence=("Canada",),
    )
    return (
        replace(_inputs(), residence_rule=stated_rule),
        replace(output, reading=replace(output.reading, place_fit=place_fit), outcome="pursue"),
    )


def test_the_model_place_fit_leaves_the_stated_residence_rule_untouched() -> None:
    stated = build_evaluation_commit(*_placed("Applicants must reside in Canada."))
    unstated = build_evaluation_commit(*_placed(""))

    assert stated.residence_rule == "Applicants must reside in Canada."
    assert unstated.residence_rule == ""
    assert stated.eligibility.workspace_location_key == "canada_remote or us_remote"


def test_the_place_fit_judgment_grants_apply_now_when_the_posting_states_no_rule() -> None:
    commit = build_evaluation_commit(*_placed(""))

    assert commit.decision == EvaluationDecision.APPLY_NOW


def _premium(location: Answer[str]) -> tuple[EvaluationCommitInputs, EvaluationOutput]:
    """A pursued, placed posting paying at the band's target, with one arrangement reading."""
    inputs, output = _placed("")
    reading = replace(output.reading, pay_target_met=True, location=location)
    return inputs, replace(output, reading=reading)


def test_an_arrangement_the_readers_leave_in_conflict_caps_a_premium_target() -> None:
    conflicted: Answer[str] = Answer(
        "Where the work happens",
        evidence=("#LI-Hybrid", "Remote - Canada; Remote - United States"),
        note="the LinkedIn arrangement tag conflicts with a remote place the posting lists",
        conflict=True,
    )

    commit = build_evaluation_commit(*_premium(conflicted))

    assert commit.decision == EvaluationDecision.APPLY_NOW
    assert commit.rationale.startswith(
        "the posting's working arrangement does not add up, so this is apply now rather than "
        "premium target: the LinkedIn arrangement tag conflicts with a remote place the "
        "posting lists. pursue"
    )


def test_a_resolved_arrangement_still_reaches_premium_target() -> None:
    resolved = Answer("Where the work happens", "remote", evidence=("Remote - Canada",))

    commit = build_evaluation_commit(*_premium(resolved))

    assert commit.decision == EvaluationDecision.PREMIUM_TARGET
    assert commit.rationale == "pursue"


def test_a_pay_floor_above_the_stretch_commits_apply_now_at_the_target_score() -> None:
    inputs, output = _premium(Answer("Where the work happens", "remote"))
    reading = replace(
        output.reading,
        pay_fit=Answer("Pay against your band", "at or above your CAD annual floor"),
        pay_standing=90,
        pay_above_stretch="CAD annual",
    )

    commit = build_evaluation_commit(inputs, replace(output, reading=reading))

    assert commit.decision == EvaluationDecision.APPLY_NOW
    assert commit.rationale.startswith(
        "the posting's pay floor is above your CAD annual stretch figure, which can mean a "
        "role pitched at another level, so this is apply now rather than premium target. pursue"
    )
    assert commit.scores.compensation.value == 90


FULL_FIT = "the record answers 100 per cent of what the posting weights"


def _pursued(place_fit: Answer[str]) -> tuple[EvaluationCommitInputs, EvaluationOutput]:
    """A posting the record answers in full, pursued, with one place-fit reading."""
    output = _output((FULL_FIT,))
    answered = EvidenceMap(
        matches=(
            Match(
                requirement_id="R1",
                entry_ids=("b-1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    entry = Entry(id="b-1", kind=EntryKind.BULLET, section="Experience", text=CANDIDATE)
    inventory = output.inventory.model_copy(update={"entries": (entry,)})
    reading = replace(output.reading, place_fit=place_fit)
    return _inputs(), replace(
        output, evidence=answered, inventory=inventory, reading=reading, outcome="pursue"
    )


def test_a_full_fit_outside_the_declared_places_names_the_place_ahead_of_the_fit() -> None:
    inputs, output = _pursued(
        Answer(
            "Where you could work this",
            ": outside your declared places",
            note="the posting names none of your places (canada_remote, us_remote)",
        )
    )

    commit = build_evaluation_commit(inputs, output)

    assert commit.scores.fit.value == 100
    assert commit.decision == EvaluationDecision.INVESTIGATE
    assert commit.rationale == (
        "the place reading puts the posting in none of your markets, so this is investigate "
        "rather than apply now: the posting names none of your places (canada_remote, "
        f"us_remote). {FULL_FIT}"
    )


_RESIDENTS_ONLY = "Open to current Springfield residents ONLY."
_INITECH_POSTING = (
    "# Data Engineer at Initech\n\nRemote within the US.\n"
    f"{_RESIDENTS_ONLY}\n\n## Requirements\n- Lead a governed data platform"
)


def test_a_residence_gate_the_rules_did_not_read_holds_a_full_fit_at_investigate() -> None:
    inputs, output = _pursued(Answer("Where you could work this", "us_remote: authorized"))
    gate = HardGate(
        kind="location",
        text=_RESIDENTS_ONLY,
        blocking=True,
        source_span=locate(_RESIDENTS_ONLY, _INITECH_POSTING),
    )
    reading = replace(
        fit.read(_INITECH_POSTING, output.inventory, today=date(2026, 8, 11)),
        place_fit=output.reading.place_fit,
    )
    assert reading.residence_requirement.value is None
    initech = replace(
        output,
        spec=output.spec.model_copy(update={"company": "Initech", "hard_gates": (gate,)}),
        jd=JDText(text=_INITECH_POSTING, origin="posting.txt", method="direct_text"),
        reading=reading,
    )

    commit = build_evaluation_commit(replace(inputs, employer="Initech"), initech)

    assert commit.scores.fit.value is not None and commit.scores.fit.value >= 60
    assert commit.decision == EvaluationDecision.INVESTIGATE
    assert commit.rationale.startswith(
        "the posting states a residence rule the place reading did not read, so this is "
        f"investigate rather than apply now: {_RESIDENTS_ONLY}"
    )


def test_a_place_reading_naming_several_places_says_it_did_not_settle_the_market() -> None:
    inputs, output = _pursued(
        Answer(
            "Where you could work this",
            note="the posting names more than one of your places: canada_remote, us_onsite",
            conflict=True,
        )
    )

    commit = build_evaluation_commit(inputs, output)

    assert commit.decision == EvaluationDecision.INVESTIGATE
    assert commit.rationale == (
        "the place reading did not settle which of your markets the posting is in, so this "
        "is investigate rather than apply now: the posting names more than one of your "
        f"places: canada_remote, us_onsite. {FULL_FIT}"
    )


PLACED = Answer(
    "Where you could work this", "canada_remote: authorized", evidence=("Remote, Canada",)
)
BARRED = Answer(
    "Where you could work this",
    "us_onsite: ineligible",
    evidence=("Applicants must reside in the United States",),
)


@pytest.mark.parametrize(
    ("place_fit", "status", "credential_gate", "decision", "reason"),
    [
        (
            PLACED,
            "closed",
            "",
            EvaluationDecision.CLOSED_EVIDENCE,
            "the last reading of this posting says it is closed, so this is closed evidence",
        ),
        (
            BARRED,
            "active",
            "",
            EvaluationDecision.INELIGIBLE,
            "the posting's place rules you out, so this is ineligible: "
            "Applicants must reside in the United States",
        ),
        (
            PLACED,
            "active",
            "Active locksmith license required at hire",
            EvaluationDecision.INELIGIBLE,
            "the posting states as absolute a credential you declared you lack, so this is "
            "ineligible: Active locksmith license required at hire",
        ),
    ],
    ids=["closed", "ineligible-residence", "credential-conflict"],
)
def test_what_overrides_a_pursue_leads_the_rationale(
    place_fit: Answer[str],
    status: str,
    credential_gate: str,
    decision: EvaluationDecision,
    reason: str,
) -> None:
    inputs, output = _pursued(place_fit)

    commit = build_evaluation_commit(
        replace(inputs, posting_status=status), output, credential_gate=credential_gate
    )

    assert commit.decision == decision
    assert commit.rationale == f"{reason}. {FULL_FIT}"


YEARS_POSTING = f"{POSTING}\n- 5+ years of experience leading data teams"


def _markdown(commit: EvaluationCommit) -> str:
    return render_fit_report(
        commit,
        evidence_ids={item.key: item.key for item in commit.score_evidence},
        weights={name: Decimal("0.2") for name in commit.scores.named()},
        weight_set_name="Default",
        weight_set_version=1,
    )


def _years_gated(gate: str) -> EvaluationOutput:
    """A posting stating five years, beside a model that gated on ``gate`` and read no level."""
    output = _output(("watch",))
    spec = output.spec.model_copy(
        update={
            "seniority": None,
            "hard_gates": (HardGate(kind="minimum_years", text=gate),),
        }
    )
    reading = fit.read(YEARS_POSTING, output.inventory, today=date(2026, 8, 11))
    jd = JDText(text=YEARS_POSTING, origin="posting.txt", method="direct_text")
    return replace(output, spec=spec, reading=reading, jd=jd)


def test_a_years_floor_the_model_and_the_rules_read_differently_is_a_reported_row() -> None:
    output = _years_gated("7+ years of experience leading data teams")
    assert output.reading.years.value == 5

    commit = build_evaluation_commit(_inputs(), output)

    [row] = commit.fit_report.disagreements
    assert (row.subject, row.rules_say) == ("Years of experience required", "5")
    assert row.model_says.startswith("7, from ")
    assert row.evidence_keys == ["reading-years-1"]
    cited = {item.key: item.text for item in commit.score_evidence}
    assert cited["reading-years-1"] == "5+ years of experience leading data teams"
    section = _markdown(commit).split("## Model and rules disagreements", 1)[1]
    assert "| Years of experience required | 7, from " in section
    assert "No disagreement was recorded." not in section


def test_a_years_floor_both_readers_agree_on_is_no_row_beside_a_level_they_do_not() -> None:
    output = _years_gated("5+ years of experience leading data teams")
    output = replace(output, spec=output.spec.model_copy(update={"seniority": "senior"}))

    commit = build_evaluation_commit(_inputs(), output)

    assert [row.subject for row in commit.fit_report.disagreements] == ["Seniority"]


LONG_LINE = (
    "Lead a governed data platform across ingestion, modelling, quality monitoring, lineage "
    "capture, access review, retention policy and cost reporting for every product team"
)


def test_an_importance_row_cites_the_whole_posting_line_at_its_span() -> None:
    posting = f"# Platform Director\n\n## Requirements\n- {LONG_LINE}"
    output = _output(("watch",))
    requirement = Requirement(id="R1", text=LONG_LINE, importance=Importance.NICE_TO_HAVE)
    output = replace(
        output,
        spec=output.spec.model_copy(update={"requirements": (requirement,)}),
        reading=fit.read(posting, output.inventory, today=date(2026, 8, 11)),
        jd=JDText(text=posting, origin="posting.txt", method="direct_text"),
    )

    commit = build_evaluation_commit(_inputs(), output)

    [row] = commit.fit_report.disagreements
    assert row.subject == "Importance of R1"
    [key] = row.evidence_keys
    [cited] = [item for item in commit.score_evidence if item.key == key]
    assert (cited.text, cited.char_start) == (LONG_LINE, posting.index(LONG_LINE))


def test_a_seniority_row_whose_reading_quotes_no_posting_text_cites_the_posting_body() -> None:
    agreed = _output(("watch",))
    disputed = replace(agreed, spec=agreed.spec.model_copy(update={"seniority": "senior"}))

    commit = build_evaluation_commit(_inputs(), disputed)

    # The reading's "title says ..." is its own summary, not posting text.
    [row] = commit.fit_report.disagreements
    [seniority] = [item for item in commit.fit_report.reading if item.question == "Seniority"]
    assert row.subject == "Seniority"
    assert seniority.evidence_keys == []
    assert row.evidence_keys == ["posting-body"]
    assert commit.score_evidence == build_evaluation_commit(_inputs(), agreed).score_evidence


def test_a_row_resting_on_no_quote_cites_the_posting_body_after_an_answered_requirement() -> None:
    inputs, output = _pursued(Answer("Where you could work this", note="no place is stated"))
    disputed = replace(output, spec=output.spec.model_copy(update={"seniority": "senior"}))

    commit = build_evaluation_commit(inputs, disputed)

    # The answered requirement's CV entry is not what a seniority row was read from.
    [row] = commit.fit_report.disagreements
    [cited] = [item for item in commit.score_evidence if item.key in row.evidence_keys]
    assert cited.key == "posting-body"
    assert cited.components == []


def test_no_posting_body_row_is_stored_when_nothing_cites_it() -> None:
    inputs, output = _pursued(Answer("Where you could work this", note="no place is stated"))
    gap = Requirement(id="R2", text="Run a regional sales team", importance=Importance.NICE_TO_HAVE)
    with_gap = replace(
        output,
        spec=output.spec.model_copy(update={"requirements": (*output.spec.requirements, gap)}),
    )

    commit = build_evaluation_commit(inputs, with_gap)

    # The CV answers one requirement and the other is a stated gap, so no row
    # rests on the posting body and its opening would show as stray evidence.
    assert commit.fit_report.disagreements == []
    assert "posting-body" not in [item.key for item in commit.score_evidence]


def test_a_row_resting_on_no_quote_cites_the_posting_body() -> None:
    posting = (
        "# Platform Director\n\n## Requirements\n"
        "- Lead a governed data platform under the direction of the CTO"
    )
    output = _output(("watch",))
    output = replace(
        output,
        reading=fit.read(posting, output.inventory, today=date(2026, 8, 11)),
        jd=JDText(text=posting, origin="posting.txt", method="direct_text"),
    )
    assert output.reading.seniority.conflict

    commit = build_evaluation_commit(_inputs(), output)

    [row] = commit.fit_report.disagreements
    assert (row.subject, row.rules_say) == ("Seniority", "not stated")
    assert row.evidence_keys == ["posting-body"]
    unasked = replace(output, spec=output.spec.model_copy(update={"seniority": None}))
    assert commit.score_evidence == build_evaluation_commit(_inputs(), unasked).score_evidence


def test_an_unstated_degree_row_cites_its_hard_gate_anchor_rather_than_a_copy() -> None:
    output = _output(("watch",))
    gates = (
        HardGate(kind="minimum_years", text="3 years of experience"),
        HardGate(kind="degree", text="Bachelor's degree required"),
    )
    output = replace(output, spec=output.spec.model_copy(update={"hard_gates": gates}))
    assert not output.reading.education.stated

    commit = build_evaluation_commit(_inputs(), output)

    [row] = [row for row in commit.fit_report.disagreements if row.subject == "Education required"]
    assert row.evidence_keys == ["hard-gate-2"]
    assert not [item for item in commit.score_evidence if item.key.startswith("disagreement-")]


def test_a_hard_gate_quote_cites_no_requirement_so_it_states_no_coverage() -> None:
    output = _output(("watch",))
    gates = (
        HardGate(kind="minimum_years", text="3 years of experience"),
        HardGate(kind="degree", text="Bachelor's degree required"),
    )
    output = replace(output, spec=output.spec.model_copy(update={"hard_gates": gates}))

    commit = build_evaluation_commit(_inputs(), output)

    quotes = [item for item in commit.score_evidence if item.key.startswith("hard-gate-")]
    assert [item.key for item in quotes] == ["hard-gate-1", "hard-gate-2"]
    assert [item.requirement_id for item in quotes] == [None, None]
    assert [gate.evidence_keys for gate in commit.fit_report.hard_gates] == [
        ["hard-gate-1"],
        ["hard-gate-2"],
    ]


def test_a_rule_refusal_says_the_readers_were_not_compared() -> None:
    commit = build_refusal_commit(
        _inputs(),
        posting_text=POSTING,
        condition="closed",
        reason="the posting says it is closed",
        eligibility=None,
        code_revision=None,
    )

    section = _markdown(commit).split("## Model and rules disagreements", 1)[1]
    assert section.startswith("\n\nNot compared: a rule refused this posting")
    assert "No disagreement was recorded." not in section


def _gaps_section(commit: EvaluationCommit) -> str:
    return _markdown(commit).split("## Gaps", 1)[1].split("\n## ", 1)[0]


def test_an_unmet_requirement_quoted_from_the_posting_is_still_a_gap() -> None:
    commit = build_evaluation_commit(_inputs(), _output(("watch",)))

    [requirement] = commit.fit_report.requirements
    assert requirement.evidence_keys == ["requirement-1-posting"]
    assert requirement.unmet
    assert "| Lead a governed data platform |" in _gaps_section(commit)


def test_the_gaps_table_lists_an_unmet_requirement_once() -> None:
    output = _output(("watch",))
    unstated = Requirement(
        id="R1", text="Hands-on experience with Linear", importance=Importance.MUST_HAVE
    )
    output = replace(output, spec=output.spec.model_copy(update={"requirements": (unstated,)}))

    commit = build_evaluation_commit(_inputs(), output)

    assert _gaps_section(commit).count("Hands-on experience with Linear") == 1


def test_a_partial_match_is_a_con_but_not_a_gap() -> None:
    output = _output(("pursue",))
    partial = EvidenceMap(
        matches=(
            Match(
                requirement_id="R1",
                classification=Classification.TRANSFERABLE,
                strength=Strength.WEAK,
            ),
        )
    )

    commit = build_evaluation_commit(_inputs(), replace(output, evidence=partial))

    assert [factor.text for factor in commit.cons] == [
        "Partial match: Lead a governed data platform"
    ]
    assert commit.gaps == []


@pytest.mark.parametrize("personal_date", [date(2026, 11, 2), None])
@pytest.mark.parametrize("target_state", ["new", "unedited", "edited", "reset"])
def test_evaluation_tracking_transfers_a_pending_date_unless_the_target_was_edited(
    durable_database: Database, personal_date: date | None, target_state: str
) -> None:
    database = durable_database
    workspace_id = scan_workspace(database, slug="paid-date")
    artifacts = EvidenceArtifactService(runtime_storage(database))
    proposal = evaluation_commit(
        workspace_id,
        employer="Example",
        requisition_id="REQ-1",
        canonical_url="https://boards.greenhouse.io/example/jobs/1",
        discovery_url="https://boards.greenhouse.io/example/jobs/1",
    )
    initial = None
    target_date = date(2026, 12, 1) if target_state == "edited" else None
    if target_state != "new":
        initial = job_search.commit_evaluation(database, artifacts, proposal)
        if target_state in {"edited", "reset"}:
            job_search.set_closing_date(
                database,
                workspace_id,
                initial.job_posting_id,
                ClosingDateEditInput(closing_date_override=target_date),
            )
    moment = datetime(2026, 10, 8, 12, tzinfo=UTC)
    pending_id = seed_pending_discovery(
        database, workspace_id, employer="Example", created_at=moment
    )
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        assert pending is not None
        pending.closing_date_override = personal_date
        pending.closing_date_override_updated_at = moment
    detail = job_search.commit_evaluation(
        database, artifacts, proposal, pending_discovery_id=pending_id
    )
    expected = target_date if target_state in {"edited", "reset"} else personal_date
    assert detail.closing_date_override == expected
    assert detail.evaluation_count == 1
    if initial is not None:
        assert detail.evaluation_id == initial.evaluation_id
    with database.session() as session:
        pending = session.get(PendingDiscovery, pending_id)
        job = session.get(JobPosting, detail.job_posting_id)
        assert pending is not None and pending.archived_at is not None
        assert job is not None
        assert pending.closing_date_override == personal_date
        assert pending.closing_date_override_updated_at == moment
        if target_state in {"new", "unedited"}:
            assert job.closing_date_override_updated_at == moment


def test_a_later_evaluation_refresh_preserves_the_personal_date(durable_database: Database) -> None:
    database = durable_database
    workspace_id = scan_workspace(database, slug="date-refresh")
    artifacts = EvidenceArtifactService(runtime_storage(database))
    proposal = evaluation_commit(workspace_id)
    initial = job_search.commit_evaluation(database, artifacts, proposal)
    job_search.set_closing_date(
        database,
        workspace_id,
        initial.job_posting_id,
        ClosingDateEditInput(closing_date_override=date(2026, 11, 2)),
    )
    changed = proposal.model_copy(
        update={
            "supersedes_evaluation_id": initial.evaluation_id,
            "rationale": "Updated reading of the same synthetic posting.",
        }
    )
    refreshed = job_search.commit_evaluation(
        database, artifacts, changed, job_posting_id=initial.job_posting_id
    )
    assert refreshed.closing_date_override == date(2026, 11, 2)
    assert refreshed.evaluation_count == 2
    assert refreshed.evaluation_id != initial.evaluation_id
