"""The extraction stage, including the one defense against a hostile posting."""

from __future__ import annotations

from itertools import product
from typing import Any

import pytest
from pydantic import ValidationError

from clearscout.application.config import Tier
from clearscout.application.tailoring.stages.jobspec import (
    MAX_KEYWORD_LENGTH,
    Importance,
    JobSpec,
    JobSpecExtraction,
    RawCoreOccupation,
    RawHardGate,
    RawRequirement,
    extract,
    requirement_vocabulary,
    restore_leading_qualifier,
    sanitize_keywords,
    spec_from_extraction,
)
from clearscout.domain import SpanStatus
from clearscout.domain.requirements import GATE_KINDS
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.errors import IngestError
from tests.support.schema import literals


def spec_with(title: str, company: str | None = None) -> JobSpec:
    return JobSpec(
        title=title,
        company=company,
        seniority=None,
        location=None,
        requirements=(),
        keywords=(),
        hard_gates=(),
        origin="test",
    )


def test_control_characters_are_neutralised_in_keywords() -> None:
    """A control character becomes a space, since deleting it would rejoin the split token."""
    assert sanitize_keywords(["Kaf\x00ka", "sche\x1bma"]) == ("Kaf ka", "sche ma")
    assert sanitize_keywords(["for\x00bidden"]) == ("for bidden",)


def test_a_paragraph_shaped_keyword_is_dropped() -> None:
    """A keyword is a phrase. Anything longer arrived trying to be an instruction."""
    assert sanitize_keywords(["x" * (MAX_KEYWORD_LENGTH + 1)]) == ()
    assert sanitize_keywords(["x" * MAX_KEYWORD_LENGTH]) == ("x" * MAX_KEYWORD_LENGTH,)


def test_whitespace_is_collapsed_and_duplicates_removed() -> None:
    assert sanitize_keywords(["  Kafka  ", "Kafka", "schema   registry"]) == (
        "Kafka",
        "schema registry",
    )


def test_empty_and_whitespace_only_keywords_disappear() -> None:
    assert sanitize_keywords(["", "   ", "\t\n"]) == ()


def test_a_slug_cannot_escape_the_output_directory() -> None:
    """The run directory name comes from model output."""
    assert "/" not in spec_with("../../etc", "..").slug()
    assert ".." not in spec_with("../../etc", "..").slug()


def test_a_slug_of_pure_punctuation_still_names_something() -> None:
    assert spec_with("!!!", "???").slug() == "posting"


def test_a_very_long_title_is_bounded() -> None:
    assert len(spec_with("x" * 500, "y" * 500).slug()) <= 80


def test_the_extraction_schemas_reject_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field none of the extraction contracts declared is refused."""
    with pytest.raises(ValidationError):
        JobSpecExtraction.model_validate({"title": "Staff Engineer", "extra_field": "x"})
    with pytest.raises(ValidationError):
        RawRequirement.model_validate(
            {"text": "Kafka", "importance": "must_have", "extra_field": "x"}
        )
    with pytest.raises(ValidationError):
        RawHardGate.model_validate(
            {"kind": "compensation", "text": "min salary", "extra_field": "x"}
        )
    with pytest.raises(ValidationError):
        RawCoreOccupation.model_validate({"text": "Staff Engineer", "extra_field": "x"})


def test_the_extraction_schema_offers_exactly_the_gate_kinds() -> None:
    assert literals(JobSpecExtraction, "kind") == list(GATE_KINDS)


def test_a_gate_kind_outside_the_closed_set_is_refused() -> None:
    with pytest.raises(ValidationError):
        RawHardGate.model_validate(
            {"kind": "academic_training_programs", "text": "No practical training"}
        )


def test_requirement_identifiers_are_assigned_in_order(client_factory: Any) -> None:
    """Identifiers are assigned in Python, not requested from the model."""
    client = client_factory(
        {
            "JobSpecExtraction": lambda schema: {
                "title": "Staff Engineer",
                "company": "Globex",
                "requirements": [
                    {"text": "Kafka", "importance": "must_have"},
                    {"text": "Terraform", "importance": "nice_to_have"},
                    {"text": "Ship things", "importance": "responsibility"},
                ],
                "keywords": ["Kafka"],
                "hard_gates": [],
            }
        }
    )
    spec = extract(client, JDText(text="a posting", origin="test", method="test"))
    assert [requirement.id for requirement in spec.requirements] == ["r01", "r02", "r03"]
    assert spec.requirements[0].importance is Importance.MUST_HAVE
    assert spec.requirements[0].weight > spec.requirements[2].weight


def test_the_offered_vocabulary_covers_every_requirement_it_is_asked_for() -> None:
    """No requirement can fall outside the identifiers the schema offers."""
    for count in (1, 16, 17, 38, 64, 65):
        offered = requirement_vocabulary(count)
        assert len(offered) >= count
        assert offered[count - 1] == f"r{count:02d}"


def test_extracts_quote_spans_and_groups_requirements_and_gates(client_factory: Any) -> None:
    client = client_factory(
        {
            "JobSpecExtraction": lambda schema: {
                "title": "Staff Engineer",
                "requirements": [
                    {
                        "text": "Hands-on work in Kafka",
                        "importance": "must_have",
                        "quote_or_span": "must have hands-on work in Kafka",
                        "parent_group": "requirements::primary",
                    },
                    {"text": "Terraform deployments", "importance": "nice_to_have"},
                ],
                "keywords": ["Kafka", "Terraform"],
                "hard_gates": [
                    {
                        "kind": "compensation",
                        "text": "Minimum annual salary 120k",
                        "blocking": True,
                        "quote_or_span": "minimum annual salary is $120k",
                        "parent_group": "compensation",
                    },
                    {
                        "kind": "location",
                        "text": "US only",
                        "blocking": False,
                    },
                ],
            }
        }
    )

    spec = extract(client, JDText(text="a posting", origin="test", method="test"))

    assert spec.requirements[0].quote_or_span == "must have hands-on work in Kafka"
    assert spec.requirements[0].parent_group == "requirements::primary"
    assert spec.requirements[1].quote_or_span == "Terraform deployments"
    assert spec.requirements[1].parent_group == "requirements"
    assert spec.hard_gates[0].quote_or_span == "minimum annual salary is $120k"
    assert spec.hard_gates[0].parent_group == "compensation"
    assert spec.hard_gates[1].quote_or_span == "US only"
    assert spec.hard_gates[1].parent_group == "hard_gates"


def test_extraction_runs_on_the_cheap_tier(client_factory: Any) -> None:
    """Reading a posting is mechanical and should not be billed as judgment."""
    client = client_factory(
        {
            "JobSpecExtraction": lambda schema: {
                "title": "Staff Engineer",
                "requirements": [{"text": "Kafka", "importance": "must_have"}],
                "keywords": [],
                "hard_gates": [],
            }
        }
    )
    extract(client, JDText(text="a posting", origin="test", method="test"))
    assert client.ledger.entries[0].model == client.settings.choice_for(Tier.EXTRACTION).model


def test_a_posting_with_no_requirements_is_refused(client_factory: Any) -> None:
    """A posting with no requirements is not a posting.

    A board index page extracts as a title and zero requirements, and screening
    then reports no deal-breakers and full evidence coverage. Every one of those
    statements is true of an empty set and none is about a job, so an empty
    extraction has to fail loudly.
    """
    client = client_factory(
        {
            "JobSpecExtraction": lambda schema: {
                "title": "Current openings at Initech",
                "requirements": [],
                "keywords": [],
                "hard_gates": [],
            }
        }
    )
    with pytest.raises(IngestError) as caught:
        extract(client, JDText(text="a posting", origin="test", method="test"))
    message = str(caught.value)
    assert "Current openings at Initech" in message
    assert "screenshot" in message


POSTING = (
    "Staff Engineer at Globex.\n"
    "You must have hands-on work in Kafka.\n"
    "The minimum annual salary is $120k.\n"
    "This role is US only.\n"
)


def _extraction(client_factory: Any, payload: dict[str, Any]) -> JobSpec:
    """Extract against POSTING, so every span check runs against real source text."""
    client = client_factory({"JobSpecExtraction": lambda schema: {"title": "x", **payload}})
    return extract(client, JDText(text=POSTING, origin="test", method="test"))


def test_a_gate_quoting_the_posting_is_verified(client_factory: Any) -> None:
    """Reflowed and recased wording is the same wording, and it verifies."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [{"text": "Kafka", "importance": "must_have"}],
            "hard_gates": [
                {
                    "kind": "compensation",
                    "text": "Minimum annual salary 120k",
                    "blocking": True,
                    "quote_or_span": "the MINIMUM annual   salary is $120k",
                }
            ],
        },
    )

    gate = spec.hard_gates[0]
    assert gate.source_span.status is SpanStatus.VERIFIED
    assert POSTING[gate.source_span.start : gate.source_span.end] == (
        "The minimum annual salary is $120k"
    )
    assert gate.verified_blocking


def test_the_core_occupation_is_placed_against_the_posting(client_factory: Any) -> None:
    """The occupation label verifies only when its quote is in the posting."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [{"text": "Kafka", "importance": "must_have"}],
            "core_occupation": {"text": "software engineering", "quote_or_span": "Staff Engineer"},
        },
    )

    assert spec.core_occupation is not None
    assert spec.core_occupation.text == "software engineering"
    assert spec.core_occupation.verified


BOUNDARIES = (
    "people management of sales teams as the primary role",
    "conventional software-platform, infrastructure, or DevOps engineering as the core occupation",
    "routine support without a named domain as the primary role",
)
ON_OCCUPATION = "Staff Engineer at Globex"
OCCUPATION = {"text": "platform engineering", "quote_or_span": "Staff Engineer"}


def _bounded(
    client_factory: Any,
    match: dict[str, Any] | None,
    occupation: dict[str, Any] | None = OCCUPATION,
) -> tuple[JobSpec, Any]:
    """Extract against POSTING with BOUNDARIES stated, returning the spec and the client."""
    payload: dict[str, Any] = {
        "title": "x",
        "requirements": [{"text": "Kafka", "importance": "must_have"}],
        "core_occupation": occupation,
    }
    if match is not None:
        payload["boundary_match"] = match
    client = client_factory({"JobSpecExtraction": lambda schema: payload})
    jd = JDText(text=POSTING, origin="test", method="test")
    return extract(client, jd, boundaries=BOUNDARIES), client


def _verdict_reasons(spec: JobSpec) -> tuple[str, tuple[str, ...]]:
    """Return the recommendation for a spec whose one requirement is answered strongly."""
    from clearscout.application.tailoring.batch.analysis import recommend
    from clearscout.application.tailoring.stages.match import (
        Classification,
        EvidenceMap,
        Match,
        Strength,
    )

    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r01",
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
                entry_ids=("e1",),
            ),
        )
    )
    found = recommend(spec, evidence, boundaries=BOUNDARIES)
    return found.verdict.value, found.reasons


def test_the_extraction_request_numbers_the_stated_boundaries(client_factory: Any) -> None:
    """The model sees each boundary by the position it may answer with."""
    _, client = _bounded(client_factory, None)

    task = client.provider.calls[0][1].task.text
    assert f"2. {BOUNDARIES[1]}" in task
    assert "<stated_boundaries>" in task


def test_a_grounded_boundary_answer_deprioritizes_naming_that_boundary(
    client_factory: Any,
) -> None:
    """The reason carries the search's own boundary text, then the posting text citing it."""
    spec, _ = _bounded(
        client_factory, {"quote_or_span": "staff  engineer AT globex", "boundary": 2}
    )

    verdict, reasons = _verdict_reasons(spec)
    assert verdict == "skip"
    assert reasons == (
        "the posting's core occupation is work your search rules out: "
        f'{BOUNDARIES[1]} (the posting: "{ON_OCCUPATION}")',
    )


@pytest.mark.parametrize(
    ("match", "occupation"),
    [
        pytest.param(
            {"quote_or_span": ON_OCCUPATION, "boundary": 4}, OCCUPATION, id="out-of-range"
        ),
        pytest.param({"quote_or_span": ON_OCCUPATION, "boundary": 0}, OCCUPATION, id="zero"),
        pytest.param(
            {"quote_or_span": "runs the Kubernetes fleet", "boundary": 2}, OCCUPATION, id="unquoted"
        ),
        pytest.param(
            {"quote_or_span": "hands-on work in Kafka", "boundary": 2},
            OCCUPATION,
            id="quote-off-the-occupation",
        ),
        pytest.param(
            {"quote_or_span": ON_OCCUPATION, "boundary": 2}, None, id="no-core-occupation"
        ),
        pytest.param(
            {"quote_or_span": ON_OCCUPATION, "boundary": 2},
            {"text": "platform engineering", "quote_or_span": "Principal Architect"},
            id="unverified-core-occupation",
        ),
        pytest.param(None, OCCUPATION, id="none"),
    ],
)
def test_an_unverified_boundary_answer_leaves_the_posting_to_todays_rule(
    client_factory: Any, match: dict[str, Any] | None, occupation: dict[str, Any] | None
) -> None:
    """Prose boundaries do not match the short label, so the strong fit stands."""
    spec, _ = _bounded(client_factory, match, occupation)

    assert spec.excluded_boundary is None
    assert _verdict_reasons(spec)[0] == "pursue"


def test_a_posting_with_no_core_occupation_leaves_it_unset(client_factory: Any) -> None:
    """An absent label is None, so the evaluate cap has nothing to act on."""
    spec = _extraction(
        client_factory,
        {"requirements": [{"text": "Kafka", "importance": "must_have"}]},
    )

    assert spec.core_occupation is None


def test_a_gate_quoting_wording_the_posting_lacks_is_kept_for_review(
    client_factory: Any,
) -> None:
    """The gate is not dropped and not believed: it is handed to a person."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [{"text": "Kafka", "importance": "must_have"}],
            "hard_gates": [
                {
                    "kind": "clearance",
                    "text": "Active security clearance required",
                    "blocking": True,
                    "quote_or_span": "an active security clearance is required",
                }
            ],
        },
    )

    gate = spec.hard_gates[0]
    assert gate.quote_or_span == "an active security clearance is required"
    assert gate.source_span.status is SpanStatus.NEEDS_REVIEW
    assert not gate.verified
    assert not gate.verified_blocking


def test_a_gate_with_no_quote_is_verified_only_if_its_text_is_the_posting(
    client_factory: Any,
) -> None:
    """Falling back to `text` is a quote claim like any other and is checked."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [{"text": "Kafka", "importance": "must_have"}],
            "hard_gates": [
                {"kind": "location", "text": "This role is US only", "blocking": True},
                {"kind": "degree", "text": "A doctorate is required", "blocking": True},
            ],
        },
    )

    assert spec.hard_gates[0].verified
    assert not spec.hard_gates[1].verified


def test_a_requirement_carries_where_the_posting_supports_it(client_factory: Any) -> None:
    """Requirements are checked against the same source as gates."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [
                {
                    "text": "Hands-on work in Kafka",
                    "importance": "must_have",
                    "quote_or_span": "must have hands-on work in Kafka",
                },
                {
                    "text": "Terraform deployments",
                    "importance": "nice_to_have",
                    "quote_or_span": "must have hands-on work in Terraform",
                },
            ],
            "hard_gates": [],
        },
    )

    assert spec.requirements[0].verified
    assert not spec.requirements[1].verified


def test_a_keyword_the_posting_does_not_use_is_dropped(client_factory: Any) -> None:
    """Coverage is scored over posting terms, so an invented one is not a term."""
    spec = _extraction(
        client_factory,
        {
            "requirements": [{"text": "Kafka", "importance": "must_have"}],
            "keywords": ["KAFKA", "Staff  Engineer", "Kubernetes"],
            "hard_gates": [],
        },
    )

    assert spec.keywords == ("KAFKA", "Staff Engineer")


def test_a_stored_extraction_rebuilds_the_spec_the_call_produced(client_factory: Any) -> None:
    """A recorded answer rebuilds the same requirement ids, so a later reader can name them."""
    answer = {
        "title": "Staff Engineer",
        "requirements": [
            {"text": "Ability to lift up to 25 pounds", "importance": "nice_to_have"},
            {"text": "Hands-on work in Kafka", "importance": "must_have"},
            {"text": "Terraform deployments", "importance": "nice_to_have"},
        ],
        "keywords": ["Kafka"],
        "hard_gates": [{"kind": "location", "text": "US only", "blocking": False}],
    }
    jd = JDText(text=POSTING, origin="test", method="test")
    called = extract(client_factory({"JobSpecExtraction": lambda schema: answer}), jd)

    rebuilt = spec_from_extraction(JobSpecExtraction.model_validate(answer), jd)

    assert rebuilt == called
    assert [(item.id, item.text) for item in rebuilt.requirements] == [
        ("r01", "Hands-on work in Kafka"),
        ("r02", "Terraform deployments"),
    ]


DEGREE_WORDS = (
    "deep",
    "strong",
    "solid",
    "proven",
    "demonstrated",
    "extensive",
    "significant",
    "excellent",
    "advanced",
    "expert",
    "thorough",
    "in-depth",
    "hands-on",
)
LINE_OPENERS = {"bullet": "Staff Engineer at Globex.\n- ", "sentence": "At Globex you bring "}


def _capitalized(word: str) -> str:
    return word[0].upper() + word[1:]


def _requirement_from(body: str, text: str, quote: str) -> tuple[str, str, str]:
    """Return the text, quote and located body slice of the one requirement ``body`` yields."""
    spec = spec_from_extraction(
        JobSpecExtraction(
            title="Staff Engineer",
            requirements=[
                RawRequirement(text=text, quote_or_span=quote, importance=Importance.MUST_HAVE)
            ],
        ),
        JDText(text=body, origin="test", method="test"),
    )
    item = spec.requirements[0]
    placed = body[item.source_span.start : item.source_span.end] if item.verified else ""
    return item.text, item.quote_or_span, placed


@pytest.mark.parametrize(
    ("word", "body_capitalized", "opener", "text_capitalized"),
    list(product(DEGREE_WORDS, (True, False), LINE_OPENERS, (True, False))),
)
def test_a_degree_word_the_body_puts_before_the_quote_is_restored(
    word: str, body_capitalized: bool, opener: str, text_capitalized: bool
) -> None:
    """The extraction dropped the word, so the program puts back what the body says."""
    body_word = _capitalized(word) if body_capitalized else word
    body = f"{LINE_OPENERS[opener]}{body_word} familiarity with Kafka pipelines.\nRemote in US.\n"
    text = "Familiarity with Kafka pipelines" if text_capitalized else "familiarity with Kafka"

    restored = _requirement_from(body, text, "familiarity with Kafka pipelines")

    lead = _capitalized(word) if text_capitalized else word
    tail = "with Kafka pipelines" if text_capitalized else "with Kafka"
    assert restored == (
        f"{lead} familiarity {tail}",
        f"{body_word} familiarity with Kafka pipelines",
        f"{body_word} familiarity with Kafka pipelines",
    )


@pytest.mark.parametrize(
    ("word", "body_capitalized", "opener", "text_capitalized"),
    list(product(DEGREE_WORDS, (True, False), LINE_OPENERS, (True, False))),
)
def test_a_degree_word_the_quote_opens_with_is_restored_to_the_text(
    word: str, body_capitalized: bool, opener: str, text_capitalized: bool
) -> None:
    """The quote kept the body's word and only the text dropped it."""
    body_word = _capitalized(word) if body_capitalized else word
    quote = f"{body_word} familiarity with Kafka pipelines"
    body = f"{LINE_OPENERS[opener]}{quote}.\nRemote in US.\n"
    text = "Familiarity with Kafka pipelines" if text_capitalized else "familiarity with Kafka"

    restored = _requirement_from(body, text, quote)

    lead = _capitalized(word) if text_capitalized else word
    tail = "with Kafka pipelines" if text_capitalized else "with Kafka"
    assert restored == (f"{lead} familiarity {tail}", quote, quote)


def test_a_split_requirement_keeps_the_degree_word_of_the_item_it_opens() -> None:
    """Splitting "Strong analytical and prioritization skills" grades only the first item."""
    body = "What you bring:\n- Strong analytical and prioritization skills\n"
    quote = "Strong analytical and prioritization skills"

    assert restore_leading_qualifier("Analytical skills", quote, body) == (
        "Strong analytical skills",
        quote,
    )
    assert restore_leading_qualifier("Prioritization skills", quote, body) == (
        "Prioritization skills",
        quote,
    )


NOT_RESTORED = {
    "previous_sentence": ("The pipeline team is {word}. Familiarity with Kafka pipelines.", None),
    "previous_line": ("Our tooling is {word}\nFamiliarity with Kafka pipelines.", None),
    "previous_list_item": ("You bring {word}, familiarity with Kafka pipelines.", None),
    "unplaced": ("- {word} familiarity with Kafka pipelines.", "familiarity with Flink pipelines"),
    "text_carries_word": ("- {word} familiarity with Kafka pipelines.", None),
    "text_and_quote_carry_word": (
        "- {word} familiarity with Kafka pipelines.",
        "{word} familiarity with Kafka pipelines",
    ),
    "quote_grades_another_clause": (
        "- {word} domain knowledge, with familiarity with Kafka pipelines.",
        "{word} domain knowledge, with familiarity with Kafka pipelines",
    ),
}


@pytest.mark.parametrize(("word", "case"), list(product(DEGREE_WORDS, NOT_RESTORED)))
def test_a_degree_word_is_left_out_when_the_body_does_not_attach_it(word: str, case: str) -> None:
    """Another clause's word, an unplaced quote, or a text already qualified stays as given."""
    template, other_quote = NOT_RESTORED[case]
    quote = (other_quote or "familiarity with Kafka pipelines").format(word=word)
    text = (
        f"{_capitalized(word)} familiarity with Kafka pipelines"
        if case.startswith("text_")
        else "Familiarity with Kafka pipelines"
    )

    restored_text, restored_quote, _ = _requirement_from(template.format(word=word), text, quote)

    assert (restored_text, restored_quote) == (text, quote)


def test_a_stored_answer_replays_the_restoration_on_its_own() -> None:
    """A stored requirement's text, quote and body give the pair a fresh extraction would."""
    body = "What you bring:\n- Proven delivery of Kafka pipelines at scale\n"

    assert restore_leading_qualifier(
        "Delivery of Kafka pipelines", "delivery of Kafka pipelines", body
    ) == ("Proven delivery of Kafka pipelines", "Proven delivery of Kafka pipelines")
