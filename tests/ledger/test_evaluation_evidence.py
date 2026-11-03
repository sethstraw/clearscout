"""Where an evaluation's evidence points, and in what order it is collected."""

from __future__ import annotations

from pathlib import Path

from clearscout.application.master_cv.cv import CvInventory, Entry, EntryKind
from clearscout.application.tailoring import fit
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

    unanswered, answered = anchors.items
    assert unanswered.direction is EvidenceDirection.LIMITS
    assert unanswered.components == []
    assert answered.components == LEADING_COMPONENTS


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

    assert keys == {"r1": ["requirement-1-candidate-1"]}
    anchor = anchors.items[0]
    assert anchor.master_cv_entry_id == "E1"
    assert anchor.char_start == 0
    assert anchor.char_end == len(CANDIDATE)
    assert anchor.locator == ""


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


def test_the_posting_body_is_the_evidence_of_last_resort_and_only_that() -> None:
    empty = EvidenceAnchors(POSTING, CANDIDATE)
    posting_body_anchor(empty)

    # Nothing was extracted, so a score with no anchor at all would be unreadable.
    assert [item.key for item in empty.items] == ["posting-body"]
    assert empty.items[0].components == LEADING_COMPONENTS

    already = EvidenceAnchors(POSTING, CANDIDATE)
    already.posting("one", "migrations", components=already.leading())
    posting_body_anchor(already)

    assert [item.key for item in already.items] == ["one"]
