"""Where an evaluation's evidence points, and in what order it is collected."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.fit.structure import RecordLine
from clearscout.application.tailoring.stages.jobspec import Importance, Requirement
from clearscout.application.tailoring.stages.match import (
    Classification,
    EvidenceMap,
    Match,
    Strength,
)
from clearscout.application.tailoring.workflows.evaluation_evidence import (
    FALLBACK_CHARACTERS,
    LEADING_COMPONENTS,
    EvidenceAnchors,
    posting_body_anchor,
    reading_evidence,
    requirement_evidence,
)
from clearscout.domain.records import EvidenceClassification, EvidenceDirection, EvidenceStrength

POSTING = "We need someone who has run migrations. Kubernetes is nice to have."
CANDIDATE = "Ran three database migrations without downtime."

DISAGREEING_ARRANGEMENT = (
    "Staff Engineer\n\nWorking arrangement\nThis role is remote. #LI-Remote #LI-Onsite\n\n"
    "Requirements\nYou have run migrations.\n"
)
PLAIN_ARRANGEMENT = (
    "Staff Engineer\n\nWorking arrangement\nThis role is fully remote.\n\n"
    "Requirements\nYou have run migrations.\n"
)


def _inventory(*entries: Entry) -> CvInventory:
    return CvInventory(source=Path("cv.md"), content_hash="x" * 8, name="A", entries=entries)


def _entry(identifier: str, text: str) -> Entry:
    return Entry(id=identifier, kind=EntryKind.BULLET, section="Experience", text=text)


def _requirement(identifier: str) -> Requirement:
    return Requirement(id=identifier, text="has run migrations", importance=Importance.MUST_HAVE)


def test_the_first_anchor_carries_the_whole_evaluation_components_and_no_later_one_does() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)

    first = anchors.leading()
    anchors.posting("one", "migrations", components=first)
    second = anchors.leading()
    anchors.posting("two", "Kubernetes", components=second)

    assert first == LEADING_COMPONENTS
    # The overall scores hang off the first supporting evidence, so a second
    # claim to them would double-count.
    assert second == []
    assert [item.components for item in anchors.items] == [LEADING_COMPONENTS, []]


def test_a_gap_listed_first_leaves_the_whole_evaluation_components_to_supporting_evidence() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    inventory = _inventory(_entry("E1", CANDIDATE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r2",
                entry_ids=("E1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    requirement_evidence(anchors, [_requirement("r1"), _requirement("r2")], evidence, inventory)

    unanswered, answered, answered_posting = anchors.items
    assert unanswered.direction is EvidenceDirection.LIMITS
    assert unanswered.components == []
    assert answered.components == LEADING_COMPONENTS
    assert answered_posting.components == []


def test_an_evaluation_with_only_limiting_anchors_falls_back_to_the_posting_body() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    inventory = _inventory(_entry("E1", CANDIDATE))

    requirement_evidence(anchors, [_requirement("r1")], EvidenceMap(matches=()), inventory)
    posting_body_anchor(anchors)

    unanswered, fallback = anchors.items
    assert unanswered.components == []
    assert fallback.key == "posting-body"
    assert fallback.components == LEADING_COMPONENTS


def test_an_answered_requirement_anchors_to_the_candidate_entries_that_answer_it() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    inventory = _inventory(_entry("E1", CANDIDATE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r1",
                entry_ids=("E1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    keys = requirement_evidence(anchors, [_requirement("r1")], evidence, inventory)

    assert keys == {"r1": ["requirement-1-candidate-1", "requirement-1-posting"]}
    anchor, posting = anchors.items
    assert (posting.requirement_id, posting.text) == ("r1", "has run migrations")
    assert posting.direction is EvidenceDirection.SUPPORTS
    assert posting.components == []
    assert anchor.master_cv_entry_id == "E1"
    assert anchor.char_start == 0
    assert anchor.char_end == len(CANDIDATE)
    assert anchor.locator == ""


def test_an_answered_requirement_the_posting_never_states_cites_only_its_entries() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    requirement = Requirement(
        id="r1", text="Hands-on experience with Linear.", importance=Importance.MUST_HAVE
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r1",
                entry_ids=("E1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    keys = requirement_evidence(
        anchors, [requirement], evidence, _inventory(_entry("E1", CANDIDATE))
    )

    assert keys == {"r1": ["requirement-1-candidate-1"]}
    assert [item.key for item in anchors.items] == ["requirement-1-candidate-1"]


def test_an_entry_whose_text_has_moved_is_named_rather_than_offset_into() -> None:
    anchors = EvidenceAnchors(POSTING, "a master CV that no longer says it")
    inventory = _inventory(_entry("E1", CANDIDATE))
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r1",
                entry_ids=("E1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    requirement_evidence(anchors, [_requirement("r1")], evidence, inventory)

    # A stale offset would point somebody at the wrong sentence, so the entry is
    # named instead and the offsets are withheld.
    anchor = anchors.items[0]
    assert anchor.char_start is None
    assert anchor.char_end is None
    assert anchor.locator == "master-cv-entry:E1"


def test_an_unanswered_requirement_anchors_to_the_posting_and_reads_as_limiting() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)

    keys = requirement_evidence(anchors, [_requirement("r1")], EvidenceMap(), _inventory())

    assert keys == {"r1": ["requirement-1-posting"]}
    anchor = anchors.items[0]
    assert anchor.classification == EvidenceClassification.GAP
    assert anchor.strength == EvidenceStrength.NONE
    assert anchor.direction == EvidenceDirection.LIMITS


def test_an_asserted_requirement_also_reads_as_limiting_rather_than_supporting() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r1",
                classification=Classification.ASSERTED,
                strength=Strength.NONE,
            ),
        )
    )

    requirement_evidence(anchors, [_requirement("r1")], evidence, _inventory())

    assert anchors.items[0].direction == EvidenceDirection.LIMITS


def test_a_reading_whose_located_quotes_disagree_anchors_them_as_contradicting() -> None:
    anchors = EvidenceAnchors(DISAGREEING_ARRANGEMENT, CANDIDATE)
    reading = fit.read(DISAGREEING_ARRANGEMENT, _inventory())

    keys = reading_evidence(anchors, reading)

    assert reading.location.conflict
    anchored = {item.key: item.direction for item in anchors.items}
    assert keys["location"]
    assert all(anchored[key] is EvidenceDirection.CONTRADICTS for key in keys["location"])


def test_a_question_the_posting_never_answers_anchors_nothing_that_contradicts() -> None:
    anchors = EvidenceAnchors(PLAIN_ARRANGEMENT, CANDIDATE)
    reading = fit.read(PLAIN_ARRANGEMENT, _inventory())

    keys = reading_evidence(anchors, reading)

    # An absence is not a contradiction: a question the posting did not answer
    # quotes nothing, and a quote it did answer with supports the finding.
    assert keys["pay_fit"] == []
    assert not reading.location.conflict
    assert all(item.direction is EvidenceDirection.SUPPORTS for item in anchors.items)


def test_text_the_posting_does_not_contain_falls_back_to_its_opening() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)

    anchors.posting("invented", "a sentence the posting never made")

    anchor = anchors.items[0]
    assert anchor.char_start == 0
    assert anchor.text == POSTING[:FALLBACK_CHARACTERS]
    assert anchor.char_end == len(anchor.text)


def test_a_sanitized_reader_line_anchors_to_its_raw_words_in_the_posting() -> None:
    raw = (
        "Staff Engineer\n\nRequirements \N{EN DASH} 5+ years\N{NO-BREAK SPACE}leading "
        "\N{LEFT DOUBLE QUOTATION MARK}data\N{RIGHT DOUBLE QUOTATION MARK} teams."
    )
    anchors = EvidenceAnchors(raw, CANDIDATE)

    anchors.posting("reading-years-1", 'Requirements - 5+ years leading "data" teams.')

    anchor = anchors.items[0]
    assert anchor.char_start == raw.index("Requirements")
    assert anchor.char_end == len(raw)
    assert anchor.text == raw[anchor.char_start :]


def test_a_sanitized_reader_line_anchors_past_entities_spelled_in_the_raw_posting() -> None:
    raw = (
        "Platform &amp; Data Engineer\n\nRequirements: 5+ years leading R&amp;D teams"
        "&nbsp;at &#39;scale&#39;."
    )
    anchors = EvidenceAnchors(raw, CANDIDATE)

    anchors.posting("disagreement-1-1", "Requirements: 5+ years leading R&D teams at 'scale'.")

    anchor = anchors.items[0]
    assert anchor.char_start == raw.index("Requirements")
    assert anchor.char_end == len(raw)
    assert anchor.text == raw[anchor.char_start :]


def test_a_shortened_reader_quote_anchors_to_the_words_it_kept() -> None:
    raw = "Staff Engineer\n\nYou will own the migration plan, the rollout, and the rollback."
    anchors = EvidenceAnchors(raw, CANDIDATE)

    anchors.posting("reading-must_have-1", "...own the migration plan, the rollout...")

    anchor = anchors.items[0]
    assert anchor.text == "own the migration plan, the rollout"
    assert anchor.char_start == raw.index("own the")


def test_an_unanswered_requirement_quotes_its_placed_posting_words() -> None:
    raw = "About us\n\nWhat you bring\nHands-on Linear experience is a must.\n"
    anchors = EvidenceAnchors(raw, CANDIDATE)
    requirement = Requirement(
        id="r1",
        text="Hands-on experience with Linear.",
        importance=Importance.MUST_HAVE,
        quote_or_span="Hands-on Linear experience is a must.",
    )

    keys = requirement_evidence(anchors, [requirement], EvidenceMap(), _inventory())

    assert keys == {"r1": ["requirement-1-posting"]}
    assert anchors.items[0].text == "Hands-on Linear experience is a must."


def test_an_unanswered_requirement_the_posting_never_states_quotes_nothing() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    requirement = Requirement(
        id="r1", text="Hands-on experience with Linear.", importance=Importance.MUST_HAVE
    )

    keys = requirement_evidence(anchors, [requirement], EvidenceMap(), _inventory())

    # Quoting the posting's opening would show a reader unrelated text as the source.
    assert keys == {"r1": []}
    assert anchors.items == []


def test_a_reader_quote_the_posting_never_states_anchors_nothing() -> None:
    anchors = EvidenceAnchors(PLAIN_ARRANGEMENT, CANDIDATE)
    reading = fit.read(PLAIN_ARRANGEMENT, _inventory())
    invented = replace(reading.location, evidence=("title says 'senior'",))
    reading = replace(reading, location=invented)

    keys = reading_evidence(anchors, reading)

    assert keys["location"] == []
    assert not [item for item in anchors.items if item.key.startswith("reading-location")]


def _record_reading(posting: str) -> fit.Reading:
    reading = fit.read(posting, _inventory())
    years = fit.Answer(
        "Years the record covers",
        6,
        evidence=("earliest dated role starts 2015, latest ends 2021",),
        lines=(
            RecordLine("northwind", "dates", "2018 - 2021"),
            RecordLine("riverbend-cafe", "dates", "2015 - 2018"),
        ),
    )
    families = fit.Answer(
        "What the record reads as",
        "Product management",
        evidence=("Product management",),
        lines=(RecordLine("northwind", "title", "Product Manager"),),
    )
    return replace(reading, record_years=years, record_families=families)


def test_record_readings_cite_the_master_cv_role_lines_they_counted() -> None:
    candidate = "### Northwind\n**Product Manager**\n2018 - 2021\n### Riverbend Cafe\n2015 - 2018\n"
    anchors = EvidenceAnchors(PLAIN_ARRANGEMENT, candidate)

    keys = reading_evidence(anchors, _record_reading(PLAIN_ARRANGEMENT))

    anchored = {item.key: item for item in anchors.items}
    cited = [anchored[key] for key in keys["record_years"] + keys["record_families"]]
    assert [(item.locator, item.text) for item in cited] == [
        ("cv:role-northwind-dates", "2018 - 2021"),
        ("cv:role-riverbend-cafe-dates", "2015 - 2018"),
        ("cv:role-northwind-title", "Product Manager"),
    ]
    for item in cited:
        assert item.source_kind == "candidate"
        assert item.master_cv_entry_id is None
        assert item.requirement_id is None
        assert item.char_start is not None and item.char_end is not None
        assert candidate[item.char_start : item.char_end] == item.text


def test_a_role_line_the_cv_text_lacks_stores_no_row() -> None:
    anchors = EvidenceAnchors(PLAIN_ARRANGEMENT, "### Northwind\n**Product Manager**\n")

    keys = reading_evidence(anchors, _record_reading(PLAIN_ARRANGEMENT))

    assert keys["record_years"] == []
    assert [item.locator for item in anchors.items if item.key.startswith("reading-record")] == [
        "cv:role-northwind-title"
    ]


def test_a_record_family_the_posting_also_names_stores_no_posting_row() -> None:
    posting = "Product management lead\n\nWe value product management experience.\n"
    anchors = EvidenceAnchors(posting, "**Product Manager**\n")

    keys = reading_evidence(anchors, _record_reading(posting))

    record_rows = [
        item
        for item in anchors.items
        if item.key.startswith(("reading-record_families", "reading-record_years"))
    ]
    assert keys["record_families"]
    assert all(item.source_kind == "candidate" for item in record_rows)


def test_a_missed_quote_leaves_the_whole_evaluation_components_to_the_posting_body() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)

    missed = anchors.quote("absent", "a sentence the posting never made", leading=True)
    posting_body_anchor(anchors)

    assert missed is None
    assert [item.key for item in anchors.items] == ["posting-body"]
    assert anchors.items[0].components == LEADING_COMPONENTS


def test_a_supporting_requirement_the_posting_never_states_leaves_leading_to_the_next() -> None:
    anchors = EvidenceAnchors(POSTING, CANDIDATE)
    unstated = Requirement(
        id="r1", text="Hands-on experience with Linear.", importance=Importance.MUST_HAVE
    )
    evidence = EvidenceMap(
        matches=(
            Match(
                requirement_id="r1", classification=Classification.DIRECT, strength=Strength.WEAK
            ),
            Match(
                requirement_id="r2",
                entry_ids=("E1",),
                classification=Classification.DIRECT,
                strength=Strength.STRONG,
            ),
        )
    )

    keys = requirement_evidence(
        anchors, [unstated, _requirement("r2")], evidence, _inventory(_entry("E1", CANDIDATE))
    )
    posting_body_anchor(anchors)

    assert keys == {"r1": [], "r2": ["requirement-2-candidate-1", "requirement-2-posting"]}
    answered, answered_posting, body = anchors.items
    assert answered.components == LEADING_COMPONENTS
    assert answered_posting.components == []
    assert (body.key, body.components) == ("posting-body", [])


def test_the_posting_body_carries_the_whole_evaluation_scores_only_when_unclaimed() -> None:
    empty = EvidenceAnchors(POSTING, CANDIDATE)
    posting_body_anchor(empty)

    # Nothing was extracted, so a score with no anchor at all would be unreadable.
    assert [item.key for item in empty.items] == ["posting-body"]
    assert empty.items[0].components == LEADING_COMPONENTS

    already = EvidenceAnchors(POSTING, CANDIDATE)
    already.posting("one", "migrations", components=already.leading())
    posting_body_anchor(already)

    assert [(item.key, item.components) for item in already.items] == [
        ("one", LEADING_COMPONENTS),
        ("posting-body", []),
    ]
