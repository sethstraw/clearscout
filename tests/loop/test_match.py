"""Evidence matching invariants the response schema cannot express alone."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring.batch.analysis import Verdict, recommend
from clearscout.application.tailoring.stages.context import record_block
from clearscout.application.tailoring.stages.jobspec import (
    Dimension,
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
    _normalized_matches,
    answered_gates,
    decided_by_parameters,
    response_schema,
)
from clearscout.application.tailoring.stages.spans import Span
from clearscout.domain import EvidenceStatus, GapClass, SpanStatus
from clearscout.domain.requirements import GATE_KINDS, GateKind
from tests.support.resumes import DETAIL, DETAILED_BULLET, detailed_record
from tests.support.spans import found


def _match(
    requirement_id: str,
    *,
    entry_ids: tuple[str, ...] = ("b-1",),
    classification: Classification = Classification.DIRECT,
    strength: Strength = Strength.STRONG,
    note: str = "supported",
) -> Match:
    return Match(
        requirement_id=requirement_id,
        entry_ids=entry_ids,
        classification=classification,
        strength=strength,
        note=note,
    )


def _spec(*requirements: Requirement, gates: tuple[HardGate, ...] = ()) -> JobSpec:
    return JobSpec(
        title="Product Manager",
        company="Globex",
        seniority=None,
        location=None,
        requirements=requirements,
        keywords=(),
        hard_gates=gates,
        origin="test",
    )


def _posting(*requirement_ids: str) -> JobSpec:
    """A posting whose requirements are plain must-have hard skills."""
    return _spec(
        *(
            Requirement(id=item, text=f"Requirement {item}", importance=Importance.MUST_HAVE)
            for item in requirement_ids
        )
    )


def _unanswered(requirement_id: str = "r01") -> EvidenceMap:
    """The model's answer for a requirement it found no evidence for."""
    return EvidenceMap(
        matches=(
            _match(
                requirement_id,
                entry_ids=(),
                classification=Classification.GAP,
                strength=Strength.NONE,
                note="nothing in the record answers it",
            ),
        )
    )


def test_a_run_that_established_nothing_is_told_apart_from_a_real_gap() -> None:
    """A missing row and an unanswered requirement share a classification, not a cause.

    Both stay a gap, so no verdict moves, but each carries its own gap class.
    """
    response = EvidenceMap(
        matches=(
            _match(
                "r02",
                entry_ids=(),
                classification=Classification.GAP,
                strength=Strength.NONE,
            ),
        )
    )

    result = _normalized_matches(response, _posting("r01", "r02"))

    missing = result.for_requirement("r01")
    unanswered = result.for_requirement("r02")
    assert missing is not None and unanswered is not None
    assert missing.gap_class is GapClass.INCOMPLETE_EVIDENCE
    assert unanswered.gap_class is GapClass.UNSUPPORTED_CORE
    assert result.needs_review() == (missing,)
    assert missing.classification is unanswered.classification is Classification.GAP


def test_a_missing_doctorate_records_a_permanent_formal_barrier() -> None:
    """A gap behind a formal award is not the same kind of gap as a missing tool."""
    spec = _spec(
        Requirement(
            id="r01",
            text="PhD in statistics or a related quantitative field.",
            importance=Importance.MUST_HAVE,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.PERMANENT_FORMAL


def test_an_optional_doctorate_is_not_recorded_as_a_permanent_barrier() -> None:
    """A credential the posting only prefers is not the wall a required one is."""
    spec = _spec(
        Requirement(
            id="r01",
            text="PhD in statistics preferred.",
            importance=Importance.NICE_TO_HAVE,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.BOUNDED_LEARNABLE


def test_a_credential_the_posting_says_it_does_not_need_is_no_barrier() -> None:
    """A posting ruling a credential out states the opposite of one demanding it."""
    spec = _spec(
        Requirement(
            id="r01",
            text="No security clearance required for this role.",
            importance=Importance.MUST_HAVE,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.UNSUPPORTED_CORE


def test_a_gap_class_reads_the_posting_quote_rather_than_the_extracted_wording() -> None:
    """The extraction model's paraphrase can drop the alternative the posting offered."""
    spec = _spec(
        Requirement(
            id="r01",
            text="PhD in statistics.",
            importance=Importance.MUST_HAVE,
            quote_or_span="PhD in statistics or equivalent experience",
            source_span=found(),
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.UNSUPPORTED_CORE


def test_a_work_authorization_requirement_records_an_employment_barrier() -> None:
    """Nothing in a career record answers eligibility, so it is not a hole in the record."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Must hold existing work authorization in this country.",
            importance=Importance.MUST_HAVE,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.GEOGRAPHY_EMPLOYMENT


def test_a_condition_the_posting_labels_itself_is_classed_by_that_label() -> None:
    """The gate's own kind settles a requirement whose prose never names the condition."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Willing to spend one week each quarter with the team abroad.",
            importance=Importance.MUST_HAVE,
        ),
        gates=(
            HardGate(
                kind="travel",
                text="Willing to spend one week each quarter with the team abroad",
                blocking=True,
                source_span=found(),
            ),
        ),
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.GEOGRAPHY_EMPLOYMENT


def test_an_optional_named_skill_records_a_bounded_learnable_gap() -> None:
    """The posting itself says this one is optional and sets no threshold on it."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Familiarity with Terraform.",
            importance=Importance.NICE_TO_HAVE,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.BOUNDED_LEARNABLE


def test_an_unanswered_behavioral_requirement_records_unclear_positioning() -> None:
    """A disposition the record never shows is a telling problem, not an absent credential."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Builds trust with partner teams under pressure.",
            importance=Importance.MUST_HAVE,
            dimension=Dimension.BEHAVIORAL,
        )
    )

    gap = _normalized_matches(_unanswered(), spec).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.UNCLEAR_POSITIONING


@pytest.mark.parametrize(
    "requirement",
    [
        Requirement(
            id="r01",
            text="Deep experience running distributed systems in production.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r01",
            text="8+ years in payments, preferred.",
            importance=Importance.NICE_TO_HAVE,
        ),
        Requirement(
            id="r01",
            text="PhD or equivalent practical experience.",
            importance=Importance.MUST_HAVE,
        ),
    ],
    ids=["unnamed core skill", "optional but time bound", "credential with an alternative"],
)
def test_a_gap_the_posting_does_not_classify_keeps_the_general_class(
    requirement: Requirement,
) -> None:
    """A guessed permanent barrier would talk someone out of a job they could get."""
    gap = _normalized_matches(_unanswered(), _spec(requirement)).for_requirement("r01")

    assert gap is not None
    assert gap.gap_class is GapClass.UNSUPPORTED_CORE


def test_a_stated_fact_is_self_attested_and_may_not_support_a_draft() -> None:
    response = EvidenceMap(matches=(_match("r01", classification=Classification.ASSERTED),))

    answered = _normalized_matches(response, _posting("r01")).for_requirement("r01")

    assert answered is not None
    assert answered.evidence_status is EvidenceStatus.SELF_ATTESTED
    assert not answered.may_support_a_claim


def test_evidence_cited_from_the_master_cv_may_support_a_draft() -> None:
    response = EvidenceMap(matches=(_match("r01"),))

    answered = _normalized_matches(response, _posting("r01")).for_requirement("r01")

    assert answered is not None
    assert answered.evidence_status is EvidenceStatus.VERIFIED
    assert answered.may_support_a_claim


def test_a_missing_row_needs_review_rather_than_reporting_a_gap() -> None:
    """A row the model never returned says nothing about the record.

    Reporting it as a gap would tell the user their CV does not answer a
    requirement the run never asked about.
    """
    response = EvidenceMap(matches=(_match("r02"),))
    result = _normalized_matches(response, _posting("r01", "r02", "r03"))

    assert tuple(item.requirement_id for item in result.matches) == ("r01", "r02", "r03")
    assert result.matches[1].classification is Classification.DIRECT
    assert result.gaps() == ()
    assert tuple(item.requirement_id for item in result.needs_review()) == ("r01", "r03")


def test_a_row_naming_a_requirement_this_posting_lacks_is_discarded_and_recorded() -> None:
    """The offered vocabulary is wider than the posting, so a row can name one it lacks."""
    response = EvidenceMap(matches=(_match("r01"), _match("r14")))

    result = _normalized_matches(response, _posting("r01", "r02"))

    assert tuple(item.requirement_id for item in result.matches) == ("r01", "r02")
    assert result.unrecognized == ("r14",)
    unanswered = result.for_requirement("r02")
    assert unanswered is not None
    assert unanswered.gap_class is GapClass.INCOMPLETE_EVIDENCE


def test_a_named_tool_the_requirement_never_states_is_dropped_and_recorded() -> None:
    """Only a tool the requirement's own words name can hold its evidence to account."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Write production SQL against a PostgreSQL warehouse",
            importance=Importance.MUST_HAVE,
        )
    )
    listed = Match(
        requirement_id="r01",
        entry_ids=("b-1",),
        classification=Classification.DIRECT,
        strength=Strength.STRONG,
        named_tools=("SQL", "PostgreSQL", "Snowflake", "Postgres"),
    )

    result = _normalized_matches(EvidenceMap(matches=(listed,)), spec)

    answered = result.for_requirement("r01")
    assert answered is not None
    assert answered.named_tools == ("SQL", "PostgreSQL")
    assert result.dropped_tools == (("r01", "Snowflake"), ("r01", "Postgres"))


def test_the_generated_schema_asks_for_the_tools_a_requirement_names() -> None:
    schema = response_schema(("b-1",), ("r01",))
    answer = {
        "matches": [
            {
                "requirement_id": "r01",
                "entry_ids": ["b-1"],
                "classification": "DIRECT",
                "strength": "strong",
                "named_tools": ["SQL"],
            }
        ]
    }

    assert schema.model_validate(answer).model_dump()["matches"][0]["named_tools"] == ["SQL"]


def test_the_generated_schema_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field this schema never declared is refused, not dropped."""
    schema = response_schema(("b-1",), ("r01",))
    with pytest.raises(ValidationError):
        schema.model_validate({"matches": [], "extra_field": "x"})


def test_duplicate_rows_become_a_gap_instead_of_hiding_ambiguity() -> None:
    response = EvidenceMap(matches=(_match("r01"), _match("r01", entry_ids=("b-2",))))
    result = _normalized_matches(response, _posting("r01"))

    assert result.matches[0].classification is Classification.GAP
    assert result.matches[0].strength is Strength.NONE
    assert result.matches[0].entry_ids == ()
    assert "duplicated" in result.matches[0].note


@pytest.mark.parametrize(
    ("entry_ids", "strength"),
    [(("b-1",), Strength.STRONG), (("b-1",), Strength.NONE), ((), Strength.WEAK)],
)
def test_a_gap_row_citing_evidence_needs_review_and_smuggles_nothing_into_selection(
    entry_ids: tuple[str, ...], strength: Strength
) -> None:
    """A gap that cites evidence or strength contradicts itself, so it settles nothing."""
    response = EvidenceMap(
        matches=(
            _match("r01", entry_ids=("b-1",)),
            _match(
                "r02", entry_ids=entry_ids, classification=Classification.GAP, strength=strength
            ),
        )
    )
    spec = _posting("r01", "r02")
    result = _normalized_matches(response, spec)

    contradictory = result.matches[1]
    assert contradictory.entry_ids == ()
    assert contradictory.strength is Strength.NONE
    assert contradictory.gap_class is GapClass.INCOMPLETE_EVIDENCE
    assert result.gaps() == ()
    assert result.needs_review() == (contradictory,)
    # Half the posting answered would skip on a real gap; an unsettled row commits to neither.
    assert recommend(spec, result).verdict is Verdict.CAUTIOUS


def test_supported_classifications_need_actual_evidence() -> None:
    response = EvidenceMap(matches=(_match("r01", entry_ids=()),))
    result = _normalized_matches(response, _posting("r01"))

    assert result.matches[0].classification is Classification.GAP
    assert result.matches[0].strength is Strength.NONE


def test_citations_are_unique_and_bounded_best_first() -> None:
    response = EvidenceMap(
        matches=(_match("r01", entry_ids=("b-1", "b-1", "b-2", "b-3", "b-4", "b-5")),)
    )
    result = _normalized_matches(response, _posting("r01"))

    assert result.matches[0].entry_ids == ("b-1", "b-2", "b-3", "b-4")


def test_a_status_requirement_is_asserted_rather_than_reported_as_a_gap() -> None:
    """A posting requiring work authorization is not a hole in the CV.

    The record holds citable accomplishments. Work authorization, citizenship,
    and location are none of those, so reporting them as a GAP would tell the
    applicant their own CV does not support their own status.
    """
    response = EvidenceMap(
        matches=(
            _match(
                "r01",
                entry_ids=(),
                classification=Classification.ASSERTED,
                strength=Strength.NONE,
                note="Answered by the stated work authorization.",
            ),
        )
    )
    result = _normalized_matches(response, _posting("r01"))

    assert result.matches[0].classification is Classification.ASSERTED
    assert result.gaps() == ()
    assert len(result.asserted()) == 1


def test_an_asserted_row_can_never_smuggle_uncited_evidence_into_selection() -> None:
    """ASSERTED is exempt from the citation rule, so it must be stripped instead.

    Without this the class would be a hole straight through the invariant the
    whole design rests on: a row that claims evidence without citing any.
    """
    response = EvidenceMap(
        matches=(
            _match(
                "r01",
                entry_ids=("b-1",),
                classification=Classification.ASSERTED,
                strength=Strength.STRONG,
            ),
        )
    )
    only = _normalized_matches(response, _posting("r01")).matches[0]

    assert only.entry_ids == ()
    assert only.strength is Strength.NONE
    assert only.score == 0.0, "an asserted answer must carry no selection weight"


def test_asserted_rows_are_not_counted_as_answered_directly() -> None:
    """The direct count is what the record proves; a statement is not that."""
    response = EvidenceMap(
        matches=(
            _match("r01"),
            _match(
                "r02", entry_ids=(), classification=Classification.ASSERTED, strength=Strength.NONE
            ),
        )
    )
    result = _normalized_matches(response, _posting("r01", "r02"))
    direct = [m for m in result.matches if m.classification is Classification.DIRECT]
    assert len(direct) == 1


def _record() -> CvInventory:
    """A record with one owned accomplishment and one saying the applicant helped others."""
    return CvInventory(
        source=Path("master_cv.md"),
        content_hash="cv-hash",
        name="Ada Lorne",
        entries=(
            Entry(
                id="b-1",
                kind=EntryKind.BULLET,
                section="Experience",
                text="Designed the billing ledger service and cut month-end close to 2 days.",
                role_key="initech",
            ),
            Entry(
                id="b-2",
                kind=EntryKind.BULLET,
                section="Experience",
                text="Helped two new engineers ship their first releases by pairing on review.",
                role_key="initech",
                order=1,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("dimension", "classification", "share"),
    [
        pytest.param(Dimension.HARD, Classification.TAILORING_ONLY, 0.5, id="hard-helped"),
        pytest.param(Dimension.BEHAVIORAL, Classification.TAILORING_ONLY, 0.0, id="trait-helped"),
        pytest.param(Dimension.BEHAVIORAL, Classification.TRANSFERABLE, 0.5, id="trait-transfer"),
    ],
)
def test_helping_others_is_indirect_evidence_only_for_a_hard_requirement(
    dimension: Dimension, classification: Classification, share: float
) -> None:
    """A trait such as unblocking colleagues is narrated by an entry saying the applicant helped."""
    requirements = (
        Requirement(id="r01", text="Design ledger services", importance=Importance.MUST_HAVE),
        Requirement(
            id="r02",
            text="Mentors engineers and unblocks teammates",
            importance=Importance.MUST_HAVE,
            dimension=dimension,
        ),
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match(
                "r02", entry_ids=("b-2",), classification=classification, strength=Strength.MODERATE
            ),
        )
    )
    assert evidence.indirect_share(_record(), requirements) == share


def test_the_matcher_is_shown_the_rules_it_must_classify_against() -> None:
    """The match prompt carries the configured distinctions.

    With no rule in front of it, a matcher reads "prototype dbt" work as
    answering a requirement for production dbt.
    """
    from clearscout.application.prompts import render
    from clearscout.application.tailoring.guardrails import Guardrails

    rules = Guardrails(distinctions=["dbt, Airflow, and Dagster are non-production prototypes."])
    rendered = render("match_system", distinctions=rules.prompt_block())
    assert "non-production prototypes" in rendered
    assert "prototype work does" in rendered
    assert "is a `GAP`" in rendered


def gate_spec() -> JobSpec:
    """A posting whose absolute gate is restated as a requirement, as postings do."""
    return JobSpec(
        title="Product Manager",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(
            Requirement(
                id="r01",
                text="3+ years of product management experience for a software company.",
                importance=Importance.MUST_HAVE,
            ),
            Requirement(id="r02", text="Proficiency in SQL.", importance=Importance.MUST_HAVE),
        ),
        keywords=(),
        hard_gates=(
            HardGate(
                kind="minimum_years",
                text="3+ years of product management experience for a software company",
                blocking=True,
                source_span=found(),
            ),
        ),
        origin="test",
    )


def test_gate_matching_includes_the_exact_product_threshold() -> None:
    from clearscout.application.tailoring.stages.match import answered_gates

    spec = JobSpec(
        title="Product Manager",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(
            Requirement(
                id="r01",
                text="alpha beta gamma zeta eta",
                importance=Importance.MUST_HAVE,
            ),
        ),
        keywords=(),
        hard_gates=(
            HardGate(
                kind="minimum_years",
                text="alpha beta gamma delta epsilon",
                blocking=True,
                source_span=found(),
            ),
        ),
        origin="test",
    )
    answer = answered_gates(spec, EvidenceMap(matches=(_match("r01"),)))[0]

    assert answer.classification is Classification.DIRECT


def test_gate_matching_abstains_when_equal_best_verdicts_conflict() -> None:
    from clearscout.application.tailoring.stages.match import answered_gates

    spec = JobSpec(
        title="Product Manager",
        company="Globex",
        seniority=None,
        location=None,
        requirements=(
            Requirement(id="r01", text="alpha beta gamma one", importance=Importance.MUST_HAVE),
            Requirement(id="r02", text="alpha beta gamma two", importance=Importance.MUST_HAVE),
        ),
        keywords=(),
        hard_gates=(
            HardGate(
                kind="minimum_years",
                text="alpha beta gamma",
                blocking=True,
                source_span=found(),
            ),
        ),
        origin="test",
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match(
                "r02",
                entry_ids=(),
                classification=Classification.GAP,
                strength=Strength.NONE,
            ),
        )
    )
    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is None
    assert answer.partial
    assert answer.summary == "answered in part"
    assert answer.needs_a_human


def test_a_gate_answered_only_transferably_is_reported_as_such() -> None:
    """An absolute gate with only a transferable answer is reported, not hidden.

    It is not a GAP, so the gaps table skips it; the gate answer reads the
    evidence and hands it to a person.
    """
    from clearscout.application.tailoring.stages.match import answered_gates

    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=("b-0001",),
                classification=Classification.TRANSFERABLE,
                strength=Strength.MODERATE,
            ),
        )
    )
    answers = answered_gates(gate_spec(), evidence)
    assert len(answers) == 1
    assert answers[0].classification is Classification.TRANSFERABLE
    assert answers[0].needs_a_human
    assert answers[0].summary == "transferable, moderate"


def test_a_gate_answered_directly_needs_nobody() -> None:
    """Direct evidence for an absolute condition is the case that should stay quiet."""
    from clearscout.application.tailoring.stages.match import answered_gates

    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=("b-0001",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    answer = answered_gates(gate_spec(), evidence)[0]
    assert answer.directly_met
    assert not answer.needs_a_human
    assert answer.summary == "direct evidence"


def test_a_gate_whose_quote_is_not_in_the_posting_is_not_a_verified_gate() -> None:
    """Direct evidence answers the gate, but nothing showed the posting states it.

    Answering a condition the posting was never shown to impose is not the same
    as clearing it, so the gate stays out of verified scoring and goes to a
    person instead.
    """
    from clearscout.application.tailoring.stages.match import answered_gates

    spec = gate_spec()
    unplaced = spec.hard_gates[0].model_copy(update={"source_span": Span()})
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=("b-0001",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    answer = answered_gates(spec.model_copy(update={"hard_gates": (unplaced,)}), evidence)[0]

    assert answer.directly_met
    assert answer.blocking
    assert not answer.verified_blocking
    assert answer.needs_a_human
    assert answer.span_status is SpanStatus.NEEDS_REVIEW


def test_a_gate_no_requirement_restates_is_reported_as_unchecked() -> None:
    """Nothing matched it, so nothing judged it, and saying so is the honest answer."""
    from clearscout.application.tailoring.stages.match import answered_gates

    answer = answered_gates(gate_spec(), EvidenceMap())[0]
    assert answer.classification is None
    assert answer.summary == "not checked"
    assert answer.needs_a_human


def test_a_gate_is_not_matched_to_an_unrelated_requirement() -> None:
    """Overlap has to mean something, or every gate matches the first requirement."""
    from clearscout.application.tailoring.stages.match import answered_gates

    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r02",
                entry_ids=("b-0002",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )
    # Only SQL is answered, and SQL is not the experience gate.
    assert answered_gates(gate_spec(), evidence)[0].classification is None


SETTLED_BY_PARAMETERS = {
    "location",
    "onsite_cadence",
    "travel",
    "work_authorization",
    "sponsorship",
    "time_zone",
    "employment_type",
    "compensation",
}


@pytest.mark.parametrize("kind", GATE_KINDS)
def test_only_circumstance_kinds_are_settled_by_the_search_parameters(kind: GateKind) -> None:
    assert decided_by_parameters(kind) is (kind in SETTLED_BY_PARAMETERS)


def test_a_practical_training_clause_filed_as_sponsorship_is_answered_by_parameters() -> None:
    """The clause names no parameter word, so only its kind can route it."""
    spec = _spec(
        gates=(
            HardGate(
                kind="sponsorship",
                text="Candidates on F-1 CPT or STEM OPT practical training are not considered",
                blocking=True,
                source_span=found(),
            ),
        )
    )

    answer = answered_gates(spec, EvidenceMap())[0]

    assert answer.answered_by_parameters


@pytest.mark.parametrize("kind", sorted(SETTLED_BY_PARAMETERS))
def test_a_gate_the_search_parameters_settle_is_reported_as_answered_by_them(
    kind: GateKind,
) -> None:
    """The fit reader answers these against the search parameters, so none is left unchecked."""
    gate = HardGate(
        kind=kind, text="Remote within the United States", blocking=True, source_span=found()
    )

    assert (
        answered_gates(_spec(gates=(gate,)), EvidenceMap())[0].summary == "your search parameters"
    )


PERSONAL_SCHEDULE_OR_LANGUAGE = (
    "The ability to work occasional weekdays until 5:00 pm, weekends, and statutory "
    "holidays on a rotation basis",
    "Must be available to work evenings and weekends",
    "Able to work rotating shifts, including nights",
    "Willing to work overnight shifts",
    "Availability of 10-40 hours per week",
    "Commit to at least 20 hours/week",
    "Must be able to work a Monday to Friday schedule",
    "Native Language: Native-level proficiency (C2+) in your primary language is mandatory",
    "Bilingual in English and Spanish",
    "Fluent in written and spoken French",
    "Native English speaker",
    "German at CEFR C1 level or above",
)

RECORD_CONDITIONS = (
    "Five years of experience with a programming language such as Python",
    "Hands-on natural language processing experience",
    "Expert in a query language such as SQL",
    "Experience with shift-left testing practices",
    "Built high availability systems serving 24/7 traffic",
    "Must have 5+ years of clinical informatics experience",
    "Python as your primary language",
    "Experience building multilingual NLP models",
    "Experience reporting on call center metrics",
    "Maintained overnight ETL batch jobs",
    "Must participate in an on-call rotation",
    "On-call rotation experience for production services",
    "Resolved incidents within 2-4 hours SLA",
    "Dashboards refreshed at 6 am for clinical leads",
    "Championed an early shift-left testing culture",
)

HOURS_OR_LANGUAGE_REASON = (
    "a condition about working hours or language that a career record cannot answer"
)


def _restated_condition(text: str) -> JobSpec:
    """A posting stating one absolute condition of kind other and restating it as a must-have."""
    return _spec(
        Requirement(id="r01", text=text, importance=Importance.MUST_HAVE),
        gates=(HardGate(kind="other", text=text, blocking=True, source_span=found()),),
    )


@pytest.mark.parametrize("text", PERSONAL_SCHEDULE_OR_LANGUAGE)
def test_a_schedule_or_language_condition_the_record_cannot_answer_is_never_a_skip(
    text: str,
) -> None:
    """The career record never answers the person's hours or language, so a gap is no failure."""
    spec = _restated_condition(text)

    answer = answered_gates(spec, _unanswered())[0]
    recommendation = recommend(spec, _unanswered())

    assert answer.classification is None
    assert recommendation.verdict is not Verdict.SKIP
    assert any(HOURS_OR_LANGUAGE_REASON in reason for reason in recommendation.reasons)
    assert not any("paired nothing to it" in reason for reason in recommendation.reasons)


@pytest.mark.parametrize("text", RECORD_CONDITIONS)
def test_a_record_condition_the_record_fails_is_still_a_skip(text: str) -> None:
    """Words like language, shift and availability alone do not make a condition personal."""
    assert recommend(_restated_condition(text), _unanswered()).verdict is Verdict.SKIP


@pytest.mark.parametrize("text", PERSONAL_SCHEDULE_OR_LANGUAGE)
def test_a_schedule_or_language_gate_says_no_record_answers_it(text: str) -> None:
    answer = answered_gates(_restated_condition(text), _unanswered())[0]

    assert not answer.answered_by_record
    assert answer.summary == "you, since a career record cannot answer hours or language"


@pytest.mark.parametrize("text", RECORD_CONDITIONS)
def test_a_record_condition_gate_is_answered_by_the_record(text: str) -> None:
    answer = answered_gates(_restated_condition(text), _unanswered())[0]

    assert answer.answered_by_record
    assert answer.summary == "no evidence"


def test_hours_and_language_conditions_leave_a_fully_answered_posting_a_pursue() -> None:
    """Neither condition holds back a posting whose every requirement has direct evidence."""
    gates = tuple(
        HardGate(kind="other", text=text, blocking=True, source_span=found())
        for text in ("Must be available to work evenings and weekends", "Native English speaker")
    )
    spec = _spec(
        Requirement(id="r01", text="Five years of SQL reporting.", importance=Importance.MUST_HAVE),
        gates=gates,
    )

    recommendation = recommend(spec, EvidenceMap(matches=(_match("r01"),)))

    assert recommendation.verdict is Verdict.PURSUE


def test_a_gate_restated_by_an_unestablished_match_stays_unestablished() -> None:
    """An incomplete model answer is not evidence the record fails the gate."""
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.GAP,
                strength=Strength.NONE,
                evidence_status=EvidenceStatus.UNRESOLVED,
                gap_class=GapClass.INCOMPLETE_EVIDENCE,
            ),
        )
    )

    answer = answered_gates(gate_spec(), evidence)[0]

    assert answer.classification is None
    assert not answer.partial
    assert answer.summary == "not checked"


def test_a_compound_gate_takes_the_verdict_of_its_same_words_restatement() -> None:
    """A sub-requirement sharing every word of its own does not tie the restatement."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Bachelor's degree in computing and 5+ years of software delivery.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r02",
            text="5+ years of software delivery.",
            importance=Importance.MUST_HAVE,
        ),
        gates=(
            HardGate(
                kind="degree",
                text="Bachelor's degree in computing and 5+ years of software delivery",
                blocking=True,
                source_span=found(),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.MODERATE),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is Classification.DIRECT
    assert not answer.needs_a_human


COMPOUND_GATE = "Bachelor's degree and 5+ years of experience"


def _compound_gate_spec(*requirements: tuple[str, str]) -> JobSpec:
    """A posting stating the compound degree gate absolutely, beside these requirements."""
    return _spec(
        *(
            Requirement(id=rid, text=text, importance=Importance.MUST_HAVE)
            for rid, text in requirements
        ),
        gates=(HardGate(kind="degree", text=COMPOUND_GATE, blocking=True, source_span=found()),),
    )


def _unestablished(requirement_id: str) -> Match:
    """The row a missing or contradictory model answer normalizes to."""
    return Match(
        requirement_id=requirement_id,
        classification=Classification.GAP,
        strength=Strength.NONE,
        evidence_status=EvidenceStatus.UNRESOLVED,
        gap_class=GapClass.INCOMPLETE_EVIDENCE,
    )


def _gapped(requirement_id: str) -> Match:
    return _match(
        requirement_id, entry_ids=(), classification=Classification.GAP, strength=Strength.NONE
    )


def test_an_unestablished_restatement_is_not_answered_by_one_of_its_parts() -> None:
    """The answered years clause shows only part of the gate the run left open."""
    spec = _compound_gate_spec(("r01", "5+ years of experience."), ("r02", f"{COMPOUND_GATE}."))
    evidence = EvidenceMap(matches=(_match("r01"), _unestablished("r02")))

    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is None
    assert answer.partial
    assert answer.needs_a_human


def test_an_unestablished_part_beside_an_answered_part_answers_the_gate_in_part() -> None:
    spec = _compound_gate_spec(("r01", "5+ years of experience."), ("r02", "Bachelor's degree."))
    evidence = EvidenceMap(matches=(_match("r01"), _unestablished("r02")))

    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is None
    assert answer.partial


def test_a_lone_unestablished_part_leaves_the_gate_unpaired() -> None:
    spec = _compound_gate_spec(("r01", "5+ years of experience."))
    evidence = EvidenceMap(matches=(_unestablished("r01"),))

    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is None
    assert not answer.partial


def test_a_same_words_restatement_decides_the_gate_over_a_gapped_part() -> None:
    """A preferred higher degree the record lacks does not reopen the stated one."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Bachelor's degree in a technical field.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r02",
            text="Master's degree in a technical field.",
            importance=Importance.NICE_TO_HAVE,
        ),
        gates=(
            HardGate(
                kind="degree",
                text="Bachelor's degree in a technical field",
                blocking=True,
                source_span=found(),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01", classification=Classification.TRANSFERABLE, strength=Strength.MODERATE),
            _gapped("r02"),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength, answer.partial) == (
        Classification.TRANSFERABLE,
        Strength.MODERATE,
        False,
    )


def test_a_gapped_restatement_beside_an_answered_part_stays_a_gap() -> None:
    spec = _compound_gate_spec(("r01", "5+ years of experience."), ("r02", f"{COMPOUND_GATE}."))
    evidence = EvidenceMap(matches=(_match("r01"), _gapped("r02")))

    answer = answered_gates(spec, evidence)[0]

    assert answer.classification is Classification.GAP
    assert not answer.partial


def test_a_gate_answered_by_several_parts_is_met_only_as_well_as_its_weakest() -> None:
    """The closer-worded clause is the easier one here, and it must not speak for both."""
    spec = _spec(
        Requirement(
            id="r01",
            text="Five or more years of industry experience in widget design roles.",
            importance=Importance.MUST_HAVE,
        ),
        Requirement(
            id="r02", text="Three years in sprocket tooling.", importance=Importance.MUST_HAVE
        ),
        gates=(
            HardGate(
                kind="minimum_years",
                text=(
                    "5+ years of industry experience in widget design, "
                    "with a minimum of 3 years in sprocket tooling"
                ),
                blocking=True,
                source_span=found(),
            ),
        ),
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01", classification=Classification.TRANSFERABLE, strength=Strength.WEAK),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.MODERATE),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength) == (
        Classification.TRANSFERABLE,
        Strength.WEAK,
    )


def _parted_gate(
    gate: str, *texts: str, preferred: tuple[str, ...] = (), blocking: bool = True
) -> JobSpec:
    """A posting stating one degree gate, with requirements r01, r02, ... in order.

    The ``preferred`` texts follow the rest as nice-to-have requirements.
    """
    importances = [Importance.MUST_HAVE] * len(texts) + [Importance.NICE_TO_HAVE] * len(preferred)
    return _spec(
        *(
            Requirement(id=f"r{index:02}", text=text, importance=importance)
            for index, (text, importance) in enumerate(
                zip((*texts, *preferred), importances, strict=True), 1
            )
        ),
        gates=(HardGate(kind="degree", text=gate, blocking=blocking, source_span=found()),),
    )


@pytest.mark.parametrize(
    ("gate", "contained", "loose", "preferred"),
    [
        (
            "8+ years of experience designing, building, and operating production software systems",
            (
                "8+ years of experience designing production software systems.",
                "8+ years of experience building production software systems.",
                "8+ years of experience operating production software systems.",
            ),
            ("Experience designing and developing distributed systems.",),
            (),
        ),
        (
            "5+ years of product management experience, preferably with a focus on "
            "enterprise software",
            ("5+ years of product management experience.",),
            (),
            ("Product management experience focused on enterprise software.",),
        ),
    ],
    ids=["loose-words-all-covered", "loose-part-preferred"],
)
def test_parts_wholly_inside_the_gate_decide_it_over_a_loosely_worded_part(
    gate: str, contained: tuple[str, ...], loose: tuple[str, ...], preferred: tuple[str, ...]
) -> None:
    spec = _parted_gate(gate, *contained, *loose, preferred=preferred)
    loose_id = f"r{len(contained) + 1:02}"
    evidence = EvidenceMap(
        matches=(
            *(_match(f"r{index:02}") for index in range(1, len(contained) + 1)),
            _match(
                loose_id, classification=Classification.TRANSFERABLE, strength=Strength.MODERATE
            ),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength, answer.partial) == (
        Classification.DIRECT,
        Strength.STRONG,
        False,
    )


DEGREE_OPTIONS = (
    "Doctorate degree and 2 years of widget engineering experience",
    "Master's degree and 6 years of widget engineering experience",
    "Bachelor's degree and 8 years of widget engineering experience",
    "Associate degree and 10 years of widget engineering experience",
    "High school diploma and 12 years of widget engineering experience",
)


@pytest.mark.parametrize(
    ("classification", "strength"),
    [(Classification.TRANSFERABLE, Strength.WEAK), (Classification.GAP, Strength.NONE)],
)
def test_a_gate_listing_alternatives_is_met_as_well_as_its_best_met_option(
    classification: Classification, strength: Strength
) -> None:
    """One listed option joins the next with a bare list break rather than an "or"."""
    gate = (
        f"{DEGREE_OPTIONS[0]} or\n- {DEGREE_OPTIONS[1]} or\n- {DEGREE_OPTIONS[2]}\n- "
        f"{DEGREE_OPTIONS[3]} or\n- {DEGREE_OPTIONS[4]}"
    )
    spec = _parted_gate(gate, *(f"{option}." for option in DEGREE_OPTIONS))
    evidence = EvidenceMap(
        matches=(
            *(_match(f"r0{index}") for index in range(1, 5)),
            _match(
                "r05",
                entry_ids=() if classification is Classification.GAP else ("b-1",),
                classification=classification,
                strength=strength,
            ),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength, answer.partial) == (
        Classification.DIRECT,
        Strength.STRONG,
        False,
    )


def test_a_gate_whose_parts_are_joined_by_and_is_met_only_as_well_as_its_weakest() -> None:
    spec = _parted_gate(COMPOUND_GATE, "Bachelor's degree.", "5+ years of experience.")
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.MODERATE),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength) == (
        Classification.TRANSFERABLE,
        Strength.MODERATE,
    )


def test_an_or_inside_a_parenthetical_does_not_make_the_parts_options() -> None:
    """The equivalent-experience aside qualifies the degree; the years stay required."""
    spec = _parted_gate(
        "Bachelor's degree in Computer Science (or equivalent experience), "
        "5+ years of backend development",
        "Bachelor's degree in Computer Science.",
        "5+ years of backend development.",
    )
    evidence = EvidenceMap(matches=(_match("r01"), _gapped("r02")))

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.partial) == (None, True)


def test_a_bulleted_gate_with_no_or_is_met_only_as_well_as_its_weakest_bullet() -> None:
    spec = _parted_gate(
        "- Bachelor's degree in widget engineering\n- 3+ years of sprocket tooling",
        "Bachelor's degree in widget engineering.",
        "3+ years of sprocket tooling.",
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.WEAK),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength) == (
        Classification.TRANSFERABLE,
        Strength.WEAK,
    )


def test_a_loose_part_naming_what_no_contained_part_covers_still_grades_the_gate() -> None:
    """Professional experience is the years clause in other words, and it is the weak one."""
    spec = _parted_gate(COMPOUND_GATE, "Bachelor's degree.", "5+ years of professional experience.")
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.WEAK),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength) == (
        Classification.TRANSFERABLE,
        Strength.WEAK,
    )


def test_a_loosely_worded_preference_is_no_part_of_an_absolute_gate() -> None:
    """A stated preference the record lacks does not answer the required clause in part."""
    spec = _parted_gate(
        COMPOUND_GATE,
        "Bachelor's degree.",
        "5+ years of experience.",
        preferred=("5+ years of experience in tooling.",),
    )
    evidence = EvidenceMap(matches=(_match("r01"), _match("r02"), _gapped("r03")))

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.partial) == (Classification.DIRECT, False)


PM_YEARS_GATE = (
    "3+ years of product management experience, including enterprise customer experience"
)


@pytest.mark.parametrize(
    "required",
    [(), ("Enterprise customer experience.",)],
    ids=["preferred-part-alone", "beside-a-required-part"],
)
def test_a_preference_typed_part_wholly_inside_the_gate_still_answers_it(
    required: tuple[str, ...],
) -> None:
    """The extractor's importance typing is unreliable; the gate's own words are not."""
    spec = _parted_gate(
        PM_YEARS_GATE,
        *required,
        preferred=("3+ years of product management experience",),
        blocking=False,
    )
    evidence = EvidenceMap(
        matches=tuple(_match(f"r{index:02}") for index in range(1, len(required) + 2))
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.summary) == (Classification.DIRECT, "direct evidence")


def test_a_preference_typed_part_wholly_inside_the_gate_can_be_its_weakest() -> None:
    spec = _parted_gate(
        "4+ years of product management experience in software, with direct ownership of roadmap",
        "Direct ownership of roadmap",
        preferred=("4+ years of product management experience in software",),
        blocking=False,
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01"),
            _match("r02", classification=Classification.TRANSFERABLE, strength=Strength.MODERATE),
        )
    )

    answer = answered_gates(spec, evidence)[0]

    assert (answer.classification, answer.strength) == (
        Classification.TRANSFERABLE,
        Strength.MODERATE,
    )


def test_the_candidate_record_shows_a_detail_under_its_parent() -> None:
    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]

    assert f"[{parent.id}] {DETAILED_BULLET}\n  - {DETAIL}\n" in record_block(record)


def test_a_tool_named_only_in_a_cited_detail_is_named() -> None:
    record = detailed_record()
    parent = record.of_kind(EntryKind.BULLET)[0]
    spec = _spec(Requirement(id="r01", text="Mapping with ToolX", importance=Importance.MUST_HAVE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                entry_ids=(parent.id,),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                named_tools=("ToolX",),
            ),
        )
    )

    assert evidence.unnamed_tools(spec, record) == ()


@pytest.mark.parametrize("importance", list(Importance))
def test_a_named_tool_no_cited_entry_names_is_found_at_every_importance(
    importance: Importance,
) -> None:
    requirement = Requirement(
        id="r01", text="Work with ToolA, ToolB, and ToolC for observability", importance=importance
    )
    evidence = EvidenceMap(
        matches=(_match("r01").model_copy(update={"named_tools": ("ToolA", "ToolB", "ToolC")}),)
    )

    assert evidence.unnamed_tools(_spec(requirement), _record()) == (
        (requirement, ("ToolA", "ToolB", "ToolC")),
    )


def test_a_detail_saying_the_applicant_helped_limits_its_parent_answer() -> None:
    record = _record()
    owned = record.entries[0].model_copy(
        update={"details": ("Helped the finance team reconcile the first close.",)}
    )
    record = record.model_copy(update={"entries": (owned, *record.entries[1:])})
    requirements = (
        Requirement(id="r01", text="Design ledger services", importance=Importance.MUST_HAVE),
    )
    evidence = EvidenceMap(
        matches=(
            _match("r01", classification=Classification.TAILORING_ONLY, strength=Strength.MODERATE),
        )
    )

    assert evidence.indirect_share(record, requirements) == 1.0


READINESS = (
    "Collaborate on release readiness, including positioning, enablement materials, "
    "packaging, and pricing inputs"
)
WIDGET = "Hands-on fluency using large language models such as Widget"


def _noted(requirement: Requirement, note: str) -> EvidenceMap:
    """A moderate transferable answer to one requirement carrying the model's note."""
    return EvidenceMap(
        matches=(
            _match(
                requirement.id,
                classification=Classification.TRANSFERABLE,
                strength=Strength.MODERATE,
                note=note,
            ),
        )
    )


@pytest.mark.parametrize(
    ("text", "note", "clause"),
    [
        pytest.param(
            READINESS,
            "Launch planning fits the ask; pricing inputs are not established.",
            "pricing inputs are not established",
            id="named-example",
        ),
        pytest.param(
            "Use AI tooling for discovery, including analyzing existing code, screenshots "
            "and screen flows",
            "Research work transfers. AI-assisted discovery from existing interfaces is not "
            "established.",
            "AI-assisted discovery from existing interfaces is not established",
            id="paraphrased-examples",
        ),
        pytest.param(
            READINESS,
            "Launch work is direct, but broader positioning and packaging work is not established.",
            "broader positioning and packaging work is not established",
            id="broader-examples",
        ),
        pytest.param(
            "Own the roadmap for our reporting suite, such as dashboards and alerts",
            "Roadmap ownership is shown. Globex-specific ownership is not established.",
            "Globex-specific ownership is not established",
            id="specific-to-an-unasked-name",
        ),
        pytest.param(
            "Own partner integrations for Globex customers",
            "Integration work transfers; Globex-specific ownership is not established.",
            "Globex-specific ownership is not established",
            id="specific-to-the-employer",
        ),
        pytest.param(
            "Own the Zenith product roadmap and its quarterly planning",
            "Roadmap ownership transfers; Zenith-specific ownership is not established.",
            "Zenith-specific ownership is not established",
            id="specific-to-the-employer-product",
        ),
        pytest.param(
            "Build data pipelines (e.g. ingestion, backfills) for partner teams",
            "Pipeline work is direct; there is no evidence of backfills.",
            "there is no evidence of backfills",
            id="parenthesized-example",
        ),
        pytest.param(
            "Knowledge of standards such as ABC 12 and XYZ Part 3.",
            "Protocol work transfers; knowledge of ABC 12 or XYZ Part 3 is not stated.",
            "knowledge of ABC 12 or XYZ Part 3 is not stated",
            id="example-not-stated",
        ),
        pytest.param(
            READINESS,
            "Launch work transfers; the full listed input range is not established.",
            "the full listed input range is not established",
            id="whole-list-paraphrase",
        ),
        pytest.param(
            f"{WIDGET} for product work, including reading legacy code, interpreting screen "
            "captures, and drafting specs.",
            "Model use transfers; interpreting screen captures is not shown.",
            "interpreting screen captures is not shown",
            id="including-list-after-a-short-example",
        ),
    ],
)
def test_a_note_calling_a_listed_example_missing_is_flagged(
    text: str, note: str, clause: str
) -> None:
    requirement = Requirement(id="r01", text=text, importance=Importance.MUST_HAVE)

    assert _noted(requirement, note).example_notes(_spec(requirement)) == ((requirement, clause),)


@pytest.mark.parametrize(
    ("text", "note"),
    [
        pytest.param(
            "Drive growth for a SaaS platform, including onboarding and retention",
            "Growth work transfers; SaaS context is not established.",
            id="element-of-the-ask",
        ),
        pytest.param(
            "5+ years of production Python services, such as APIs or workers",
            "The entries show scripts; production use is not established.",
            id="stated-qualifier",
        ),
        pytest.param(
            "Own pricing strategy for enterprise plans",
            "Packaging work transfers; pricing inputs are not established.",
            id="no-example-list",
        ),
        pytest.param(
            "Build SaaS integrations for partner teams",
            "Integration work transfers; SaaS-specific delivery is not established.",
            id="specific-to-a-domain-the-ask-states",
        ),
        pytest.param(
            "Lead the AI platform roadmap for partner teams",
            "Roadmap work transfers; AI-specific delivery is not established.",
            id="specific-to-an-acronym-before-a-product-noun",
        ),
        pytest.param(
            "Support clinicians and customers across care teams",
            "Support work transfers; clinician-specific and customer-specific workflows are "
            "not established.",
            id="specific-to-a-plural-the-ask-states",
        ),
        pytest.param(
            "Collaborate on release readiness across positioning, packaging, and pricing inputs",
            "Launch work transfers; the full listed input range is not established.",
            id="whole-list-paraphrase-without-an-example-list",
        ),
        pytest.param(
            f"{WIDGET} to read legacy code, interpret screen captures, and draft specs.",
            "Model use transfers; interpreting screen captures is not shown.",
            id="purpose-after-a-short-example",
        ),
        pytest.param(
            READINESS,
            "Pricing inputs and packaging are shown; release ownership is not established.",
            id="example-named-as-present",
        ),
    ],
)
def test_a_note_calling_the_ask_itself_missing_is_not_flagged(text: str, note: str) -> None:
    requirement = Requirement(id="r01", text=text, importance=Importance.MUST_HAVE)

    assert _noted(requirement, note).example_notes(_spec(requirement)) == ()


@pytest.mark.parametrize(
    ("note", "flagged"),
    [
        pytest.param("Globex-specific scope is not established.", True, id="employer-name"),
        pytest.param(
            "precision-health-specific scope is not established.", False, id="generic-word"
        ),
    ],
)
def test_only_a_title_case_word_of_the_employer_name_flags_a_specific_note(
    note: str, flagged: bool
) -> None:
    requirement = Requirement(
        id="r01",
        text="Build Globex storage components for precision health data",
        importance=Importance.MUST_HAVE,
    )
    spec = _spec(requirement).model_copy(update={"company": "Globex Health"})

    assert bool(_noted(requirement, f"Storage work transfers; {note}").example_notes(spec)) is (
        flagged
    )


PIPELINES = "Build data pipelines for partner teams, such as ingestion, backfills, and replication"
LISTED_ITEMS = (
    ("ingestion",),
    ("backfills",),
    ("replication",),
    ("ingestion", "backfills"),
    ("backfills", "replication"),
    ("ingestion", "backfills", "replication"),
)
DENIALS = (
    "{items} work is not",
    "{items} {be} not",
    "authority over {items} is not",
    "{items} {be} not documented",
    "{items} {be} documented as familiarity rather than production depth",
    "{items} {be} listed as familiarity only",
    "{items} {be} not claimed",
    "{items} {be} not explicit",
    "{items} {be} not evidenced",
    "pipeline work transfers, though not the listed {either} work",
    "pipeline work transfers but not {items} across teams",
    "schema design work is documented, not {items}",
)
GRANTS = (
    "{items} {be} documented",
    "production {items} work is shown",
    "{items} {be} not a gap",
    "not only {items} but schema design work is shown",
    "{items} {be} documented as production depth",
    "{items} {be} evidenced directly",
    "{items} {be} documented, not claimed",
    "though not the focus of the role, {items} {be} documented",
    "not a gap: {items} {be} documented",
    "while not explicitly stated, {items} work is clearly documented",
    "while not named as such, {items} {be} shown",
    "not limited to schema design, {items} {be} shown too",
)


def _listed(items: tuple[str, ...], join: str) -> str:
    """Join listed items the way a note writes them."""
    if len(items) < 3:
        return f" {join} ".join(items)
    return f"{', '.join(items[:-1])}, {join} {items[-1]}"


def _example_notes(shapes: tuple[str, ...]) -> list[object]:
    """Every shape at every listed-item position, with and without a trailing clause."""
    return [
        pytest.param(
            f"Pipeline work is direct; {note}{tail}.",
            id="-".join(f"{note}{tail}".replace(",", "").split()),
        )
        for shape in shapes
        for items in LISTED_ITEMS
        for note in [
            shape.format(
                items=_listed(items, "and"),
                either=_listed(items, "or"),
                be="is" if len(items) == 1 else "are",
            )
        ]
        for tail in ("", ", so the answer stays partial")
    ]


@pytest.mark.parametrize(
    "note",
    [
        *_example_notes(DENIALS),
        pytest.param(
            "Pipeline work transfers without the requested breadth of data movement work.",
            id="breadth-paraphrase",
        ),
    ],
)
def test_a_note_denying_a_listed_example_is_flagged(note: str) -> None:
    requirement = Requirement(id="r01", text=PIPELINES, importance=Importance.MUST_HAVE)

    flagged = _noted(requirement, note).example_notes(_spec(requirement))

    assert [held for held, _ in flagged] == [requirement]


@pytest.mark.parametrize(
    "note",
    [
        *_example_notes(GRANTS),
        pytest.param(
            "Pipeline work covers the requested breadth of data movement work.",
            id="breadth-granted",
        ),
        pytest.param(
            "Though not the focus of the role, ingestion and backfills are documented.",
            id="concession-opening-the-note",
        ),
        pytest.param("Not a gap: ingestion and backfills are documented.", id="not-a-gap-heading"),
        pytest.param(
            "Not limited to ingestion, backfills and replication are shown too.",
            id="not-limited-to-a-listed-item",
        ),
    ],
)
def test_a_note_granting_a_listed_example_is_not_flagged(note: str) -> None:
    requirement = Requirement(id="r01", text=PIPELINES, importance=Importance.MUST_HAVE)

    assert _noted(requirement, note).example_notes(_spec(requirement)) == ()


def test_familiarity_with_an_example_meets_an_ask_for_familiarity() -> None:
    requirement = Requirement(
        id="r01",
        text="Familiarity with data pipelines, such as ingestion, backfills, and replication",
        importance=Importance.MUST_HAVE,
    )
    note = "Pipeline work is direct; ingestion and backfills are listed as familiarity only."

    assert _noted(requirement, note).example_notes(_spec(requirement)) == ()


@pytest.mark.parametrize(
    ("classification", "flagged"),
    [
        pytest.param(Classification.GAP, False, id="gap"),
        pytest.param(Classification.TRANSFERABLE, True, id="transferable"),
    ],
)
def test_a_gap_may_name_an_example_as_familiarity_only(
    classification: Classification, flagged: bool
) -> None:
    requirement = Requirement(
        id="r01",
        text="Production experience with streaming systems, such as Kafka or Kinesis",
        importance=Importance.MUST_HAVE,
    )
    note = "Batch pipelines are shown; Kafka appears only as familiarity."
    evidence = EvidenceMap(
        matches=(
            _match(
                requirement.id,
                classification=classification,
                strength=Strength.WEAK,
                note=note,
            ),
        )
    )

    assert bool(evidence.example_notes(_spec(requirement))) is flagged
