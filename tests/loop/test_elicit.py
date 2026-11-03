"""Discovery, which is the only path in this program that can create a claim.

Mostly refusals, because a feature whose whole job is to add to the record has
to be tested for what it will not add. Nothing here touches a model, because
elicitation has no provider, prompt, or client.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from clearscout.application.master_cv.cv import CvInventory, EntryKind, parse_cv, spans
from clearscout.application.tailoring import elicit
from clearscout.application.tailoring.elicit import Line, Ownership, Placement, Point, Reply, Source
from clearscout.application.tailoring.elicit.vocabulary import LADDER, MAX_PROBES, RANK, REQUIRED
from clearscout.shared.errors import DiscoveryError
from tests.support.doubles import ScriptedPrompter

NOW = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)


@pytest.fixture
def cv_copy(master_cv: Path, tmp_path: Path) -> Path:
    """Return a writable copy of the synthetic master CV."""
    path = tmp_path / "CV_Master.md"
    shutil.copy(master_cv, path)
    return path


@pytest.fixture
def record(cv_copy: Path) -> CvInventory:
    """Return the parsed copy, which is what discovery adds to."""
    return parse_cv(cv_copy)


def answered(*replies: str, subject: str = "Kubernetes", gaps: tuple[str, ...] = ()) -> Line:
    """Return a line of questioning driven by the given answers, then closed."""
    line = Line(subject=subject, gap_ids=gaps)
    for text in replies:
        question = line.next_question()
        if question is None:
            break
        line = line.with_reply(Reply(text=text))
    return line.finished()


def material(record: CvInventory, line: Line, text: str, **kwargs: object) -> elicit.Pending:
    """Propose material from a line, placed under the first role."""
    return elicit.propose(
        line,
        text=text,
        scope=str(kwargs.pop("scope", "the ingestion team")),
        placement=Placement(role_key=record.roles[0].key),
        inventory=record,
        ambiguities=["how long it ran for"],
    )


def confirmed(record: CvInventory, line: Line, text: str) -> elicit.Confirmed:
    """Return fully confirmed material, which is the only kind that is written."""
    return elicit.confirm(material(record, line, text), list(Point), at=NOW)


def test_the_ladder_is_ordered_and_has_no_repeated_rung() -> None:
    """The order is the contract, so it is asserted rather than assumed."""
    rungs = [level for level, _ in LADDER]
    assert rungs == list(Ownership)
    assert [RANK[rung] for rung in rungs] == list(range(len(rungs)))
    assert len({level for level, _ in LADDER}) == len(LADDER)


def test_the_reading_says_which_rung_and_which_phrase_it_read() -> None:
    reading = elicit.reads_as("I sat in on the design reviews and nothing else.")
    assert reading.value is Ownership.EXPOSURE
    assert reading.evidence == ("sat in on",)


@pytest.mark.parametrize(
    ("answer", "rung", "phrase"),
    [
        ("I worked with the warehouse every week for reporting.", Ownership.USE, "worked with"),
        ("We shipped the matcher and it went live in March.", Ownership.DEPLOYMENT, "went live"),
    ],
)
def test_the_reading_names_the_lowest_and_highest_doing_rungs(
    answer: str, rung: Ownership, phrase: str
) -> None:
    reading = elicit.reads_as(answer)
    assert reading.value is rung
    assert phrase in reading.evidence


def test_a_use_answer_cannot_become_an_implementation_claim(record: CvInventory) -> None:
    line = answered("I worked with the ingestion jobs when writing reports.")
    with pytest.raises(DiscoveryError) as caught:
        material(record, line, "Built the ingestion jobs.")
    assert "claims implementation" in str(caught.value)


def test_the_reading_abstains_when_nothing_says_what_was_done() -> None:
    """A reading that answered "yes" with a level would be the whole problem."""
    reading = elicit.reads_as("Yes, at Northwind.")
    assert reading.value is None
    assert reading.render() == "not stated"
    assert "only that something happened" in reading.because()


def test_an_exposure_answer_cannot_become_an_ownership_claim(record: CvInventory) -> None:
    """The rule the whole feature exists to enforce."""
    line = answered("I sat in on the design reviews for it, nothing more.")
    with pytest.raises(DiscoveryError) as caught:
        material(record, line, "Owned the design reviews.")
    message = str(caught.value)
    assert "claims ownership" in message
    assert "describe exposure" in message
    # Both phrases are named, so the argument is about two sentences rather than
    # about a verdict.
    assert "The wording says owned" in message
    assert "the answers say sat in on" in message


def test_a_claim_at_or_below_what_was_said_is_allowed(record: CvInventory) -> None:
    line = answered("I built the retry layer for the ingestion jobs myself.")
    proposed = material(record, line, "Built the retry layer for the ingestion jobs.")
    assert proposed.level is Ownership.IMPLEMENTATION
    assert proposed.level_evidence == ("built",)


def test_a_claim_naming_no_rung_takes_the_one_the_answers_evidence(record: CvInventory) -> None:
    line = answered("I supported the ingestion jobs when they broke overnight.")
    proposed = material(record, line, "Ingestion jobs, overnight, when they broke.")
    assert proposed.level is Ownership.SUPPORT


def test_a_claim_of_a_level_no_answer_describes_is_refused(record: CvInventory) -> None:
    line = answered("Yes, at Northwind Analytics, across roughly one project cycle.")
    assert line.answered
    with pytest.raises(DiscoveryError, match="no answer says what the person did"):
        material(record, line, "Deployed the Northwind Analytics project cycle.")


def test_a_number_no_answer_stated_cannot_appear_in_a_claim(record: CvInventory) -> None:
    line = answered("I built the retry layer for the ingestion jobs myself.")
    with pytest.raises(DiscoveryError) as caught:
        material(record, line, "Built the retry layer, cutting failures by 40 percent.")
    assert "states 40, which no answer did" in str(caught.value)
    assert "does not exist" in str(caught.value)


def test_a_number_an_answer_did_state_survives(record: CvInventory) -> None:
    line = answered("I built the retry layer and it cut failures by 40 percent that quarter.")
    proposed = material(record, line, "Built the retry layer, which cut failures by 40 percent.")
    assert proposed.metrics == ("40",)


def test_material_with_no_metric_says_so_rather_than_carrying_a_blank(
    record: CvInventory,
) -> None:
    line = answered("I built the retry layer for the ingestion jobs myself.")
    proposed = material(record, line, "Built the retry layer for the ingestion jobs.")
    assert proposed.metrics == ()
    assert "none stated, and none may be added" in "\n".join(proposed.describe())


@pytest.mark.parametrize(
    "source", [Source.DRAFT, Source.MODEL, Source.POSTING], ids=lambda item: item.name
)
def test_an_answer_from_a_forbidden_source_is_refused_by_name(source: Source) -> None:
    """Generated resume text, a model's suggestion, and the job description."""
    line = Line(subject="Kubernetes")
    question = line.next_question()
    assert question is not None
    with pytest.raises(DiscoveryError) as caught:
        line.with_reply(Reply(text="Owned it.", source=source))
    assert source.value in str(caught.value)


@pytest.mark.parametrize(
    "source", [Source.DRAFT, Source.MODEL, Source.POSTING], ids=lambda item: item.name
)
def test_material_built_on_a_forbidden_source_is_refused_even_if_it_got_that_far(
    record: CvInventory, source: Source
) -> None:
    """The second half of the barrier, reached without going through the first.

    A line assembled directly, rather than by answering questions, still cannot
    produce material from anything but a person's own words.
    """
    line = Line(
        subject="Kubernetes",
        answered=True,
        replies=(Reply(text="I built the ingestion jobs.", source=source),),
    )
    with pytest.raises(DiscoveryError) as caught:
        material(record, line, "Built the ingestion jobs.")
    assert source.value in str(caught.value)


def test_wording_the_person_never_used_is_refused_whatever_it_is_labelled(
    record: CvInventory,
) -> None:
    """The barrier that does not depend on anybody labeling anything honestly.

    This is the same sentence a posting would use, arriving as a person's own
    reply. It fails on the words it uses, which is the point: those are the
    words of whoever wrote the posting.
    """
    line = answered("I helped out with the ingestion jobs when they broke.")
    with pytest.raises(DiscoveryError) as caught:
        material(record, line, "Supported enterprise-grade Kubernetes orchestration at scale.")
    message = str(caught.value)
    assert "no answer used" in message
    assert "kubernetes" in message
    assert "orchestration" in message


def test_short_and_ordinary_words_are_not_treated_as_unsourced() -> None:
    replies = (Reply(text="I built the retry layer for ingestion."),)
    assert elicit.unsourced_words("Built the retry layer for the ingestion jobs.", replies) == (
        "jobs",
    )


def test_the_first_question_about_a_subject_is_the_broadest_one() -> None:
    question = Line(subject="Kubernetes").next_question()
    assert question is not None
    assert question.broad
    assert question.angle is elicit.Angle.DIRECT
    assert "Kubernetes" in question.text


def test_questions_come_one_at_a_time_and_narrow_after_an_answer() -> None:
    line = Line(subject="Kubernetes")
    first = line.next_question()
    assert first is not None
    line = line.with_reply(Reply(text="I built the ingestion jobs on it."))
    second = line.next_question()
    assert second is not None
    assert not second.broad
    assert second.angle is elicit.Angle.LEADERSHIP
    # One at a time is structural: there is never a second question to answer.
    assert len(line.asked) == 1


def test_narrowing_skips_an_angle_the_answer_already_covered() -> None:
    line = Line(subject="Kubernetes")
    first = line.next_question()
    assert first is not None
    line = line.with_reply(Reply(text="I led two engineers building the ingestion jobs."))
    second = line.next_question()
    assert second is not None
    assert second.angle is elicit.Angle.SCALE


def test_an_unsuccessful_probe_moves_to_the_next_broad_angle() -> None:
    line = Line(subject="Kubernetes")
    first = line.next_question()
    assert first is not None
    line = line.with_reply(Reply(text="Hmm."))
    second = line.next_question()
    assert second is not None
    assert second.angle is elicit.Angle.SUPPORTING
    assert line.probes == 1


def test_a_second_unsuccessful_probe_moves_to_adjacent_work() -> None:
    line = Line(subject="Kubernetes")
    for _ in range(2):
        question = line.next_question()
        assert question is not None
        line = line.with_reply(Reply(text="Not sure."))
    third = line.next_question()
    assert third is not None
    assert third.angle is elicit.Angle.ADJACENT
    assert line.probes == 2


def test_questioning_stops_after_three_probes_that_find_nothing() -> None:
    line = Line(subject="Kubernetes")
    for _ in range(6):
        question = line.next_question()
        if question is None:
            break
        line = line.with_reply(Reply(text="Not sure."))
    assert line.probes == MAX_PROBES
    assert len(line.asked) == MAX_PROBES
    assert "found nothing to write down" in line.closed
    assert not line.answered


def test_a_no_ends_the_line_and_nothing_follows_it() -> None:
    """Accepted as a complete answer, with no follow-up that reads as disbelief."""
    line = Line(subject="Rust")
    question = line.next_question()
    assert question is not None
    line = line.with_reply(Reply(text="No."))
    assert line.next_question() is None
    assert len(line.asked) == 1
    assert "complete answer" in line.closed
    assert not line.answered


@pytest.mark.parametrize("word", ["No", "no.", "Never", "None", "I have not", ""])
def test_the_ways_people_say_no_are_all_read_as_no(word: str) -> None:
    line = Line(subject="Rust")
    question = line.next_question()
    assert question is not None
    assert line.with_reply(Reply(text=word)).closed


def test_a_no_to_a_narrowing_question_does_not_retract_the_answer_given() -> None:
    """Leading anyone and how large it was are two questions, not one asked twice."""
    line = Line(subject="Kubernetes")
    first = line.next_question()
    assert first is not None
    line = line.with_reply(Reply(text="I built the ingestion jobs on it."))
    second = line.next_question()
    assert second is not None
    line = line.with_reply(Reply(text="No."))
    assert line.answered
    third = line.next_question()
    assert third is not None
    assert third.angle is elicit.Angle.SCALE


def test_only_confirmed_material_that_was_written_closes_a_gap(record: CvInventory) -> None:
    """Being asked about a gap does not close it, and neither does answering."""
    refused = answered("No.", subject="Rust", gaps=("acme:r02",))
    recorded = answered(
        "I built the retry layer for the ingestion jobs myself.",
        subject="Kafka",
        gaps=("acme:r03",),
    )
    lines = (refused, recorded)
    assert elicit.unclosed(lines) == ("acme:r02", "acme:r03")
    written = [confirmed(record, recorded, "Built the retry layer for the ingestion jobs.")]
    assert elicit.unclosed(lines, written) == ("acme:r02",)


def test_material_cannot_be_proposed_from_a_line_that_found_nothing(record: CvInventory) -> None:
    line = answered("No.", gaps=("acme:r02",))
    with pytest.raises(DiscoveryError, match="still a gap"):
        material(record, line, "Built the ingestion jobs.")


def test_a_claim_with_no_wording_is_refused(record: CvInventory) -> None:
    line = answered("I built the retry layer for the ingestion jobs myself.")
    with pytest.raises(DiscoveryError, match="needs wording"):
        material(record, line, "   ")


@pytest.mark.parametrize("marker", ["- ", "* "])
def test_wording_that_starts_with_a_list_marker_is_refused(
    record: CvInventory, marker: str
) -> None:
    """The master CV parser rejects such an entry, so it is refused before confirmation."""
    line = answered("I built the retry layer for the ingestion jobs myself.")
    with pytest.raises(DiscoveryError, match="list marker"):
        material(record, line, f"{marker}Built the retry layer for the ingestion jobs.")


def test_material_stays_pending_until_every_point_is_confirmed(record: CvInventory) -> None:
    proposed = material(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    for missing in Point:
        with pytest.raises(DiscoveryError) as caught:
            elicit.confirm(proposed, [point for point in Point if point is not missing], at=NOW)
        assert missing.value in str(caught.value)


def test_confirming_everything_records_when_and_what(record: CvInventory) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    assert set(item.points) == REQUIRED
    assert item.at.startswith("2026-06-01")


def test_the_confirmation_prompt_shows_every_field_and_stops_at_a_refusal(
    record: CvInventory,
) -> None:
    proposed = material(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    prompter = ScriptedPrompter(decline=["ownership level"])
    assert elicit.confirm_with(prompter, proposed, at=NOW) is None
    assert "Not confirmed: the ownership level" in prompter.transcript
    assert "user-confirmed discovery" in prompter.transcript

    accepted = elicit.confirm_with(ScriptedPrompter(confirm=True), proposed, at=NOW)
    assert accepted is not None
    assert set(accepted.points) == REQUIRED


def test_a_placement_naming_no_role_or_section_is_refused(record: CvInventory) -> None:
    line = answered("I built the retry layer for the ingestion jobs myself.")
    with pytest.raises(DiscoveryError, match="no role 'nowhere'"):
        elicit.propose(
            line,
            text="Built the retry layer.",
            scope="the team",
            placement=Placement(role_key="nowhere"),
            inventory=record,
        )
    with pytest.raises(DiscoveryError, match="no section 'Nowhere'"):
        elicit.propose(
            line,
            text="Built the retry layer.",
            scope="the team",
            placement=Placement(section="Nowhere"),
            inventory=record,
        )


def test_a_section_nothing_may_cite_is_refused(record: CvInventory, cv_copy: Path) -> None:
    """A fact put somewhere no stage may cite would never be used."""
    cv_copy.write_text(
        cv_copy.read_text(encoding="utf-8") + "\n## Interests\n\n- Cycling\n", encoding="utf-8"
    )
    reparsed = parse_cv(cv_copy)
    line = answered("I built the retry layer for the ingestion jobs myself.")
    with pytest.raises(DiscoveryError, match="never be used"):
        elicit.propose(
            line,
            text="Built the retry layer.",
            scope="the team",
            placement=Placement(section="Interests"),
            inventory=reparsed,
        )


def test_the_spans_of_a_document_cover_its_roles_and_sections(cv_copy: Path) -> None:
    sections, roles = spans(cv_copy.read_text(encoding="utf-8"))
    assert set(roles) == {role.key for role in parse_cv(cv_copy).roles}
    assert "Professional Experience" in sections
    for span in (*sections.values(), *roles.values()):
        assert span.start < span.end


def test_the_change_is_visible_before_it_is_written(record: CvInventory, cv_copy: Path) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    source = cv_copy.read_text(encoding="utf-8")
    shown = elicit.preview(source, elicit.plan(source, record, [item]))
    assert "+ - Built the retry layer for the ingestion jobs." in shown
    assert record.roles[0].organization in shown
    assert cv_copy.read_text(encoding="utf-8") == source


def test_a_confirmed_fact_lands_under_its_role_with_a_content_derived_identifier(
    record: CvInventory, cv_copy: Path
) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    written = elicit.write(cv_copy, [item], inventory=record, at=NOW)
    after = parse_cv(cv_copy)

    assert len(written.entry_ids) == 1
    entry = after.by_id(written.entry_ids[0])
    # A normal identifier, derived from the content exactly as every other
    # entry's is, rather than a marked one.
    assert entry.id.startswith("b-")
    assert entry.kind is EntryKind.BULLET
    assert entry.role_key == record.roles[0].key
    assert entry.text == "Built the retry layer for the ingestion jobs."
    assert set(record.ids()) <= set(after.ids())


def test_the_provenance_of_a_discovered_fact_is_recorded_outside_the_cv(
    record: CvInventory, cv_copy: Path
) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    written = elicit.write(cv_copy, [item], inventory=record, at=NOW)
    log = written.journal.read_text(encoding="utf-8")

    assert written.journal.name == elicit.JOURNAL
    assert written.entry_ids[0] in log
    assert "confirmed discovery, not from the original" in log
    assert "Ownership: implementation, read from built" in log
    assert "I built the retry layer for the ingestion jobs myself." in log
    # The CV itself carries no marker: the entry is ordinary evidence once it is
    # there, and the log is the only record of where it came from.
    assert "discovery" not in cv_copy.read_text(encoding="utf-8").casefold()


def test_the_log_is_appended_to_rather_than_replaced(record: CvInventory, cv_copy: Path) -> None:
    first = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    elicit.write(cv_copy, [first], inventory=record, at=NOW)
    reparsed = parse_cv(cv_copy)
    second = confirmed(
        reparsed,
        answered("I configured the alerting for the overnight loads."),
        "Configured the alerting for the overnight loads.",
    )
    written = elicit.write(cv_copy, [second], inventory=reparsed, at=NOW)
    log = written.journal.read_text(encoding="utf-8")
    assert log.count("### `") == 2


def test_writing_is_refused_when_nothing_was_confirmed(record: CvInventory, cv_copy: Path) -> None:
    before = cv_copy.read_text(encoding="utf-8")
    with pytest.raises(DiscoveryError, match="not touched"):
        elicit.write(cv_copy, [], inventory=record, at=NOW)
    assert cv_copy.read_text(encoding="utf-8") == before


def test_a_write_that_would_not_parse_is_refused(
    record: CvInventory, cv_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The master CV is checked as a document before it is replaced."""
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    monkeypatch.setattr(elicit, "rewritten", lambda source, edits: "not a master CV at all\n")
    before = cv_copy.read_text(encoding="utf-8")
    with pytest.raises(DiscoveryError, match="cannot read"):
        elicit.write(cv_copy, [item], inventory=record, at=NOW)
    assert cv_copy.read_text(encoding="utf-8") == before


def test_a_write_that_would_lose_an_existing_entry_is_refused(
    record: CvInventory, cv_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lossless rule, forced by making the planned edit destructive."""
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )

    def destructive(source: str, edits: object) -> str:
        lines = source.splitlines()
        return "\n".join(line for line in lines if "lineage service" not in line) + "\n"

    monkeypatch.setattr(elicit, "rewritten", destructive)
    before = cv_copy.read_text(encoding="utf-8")
    with pytest.raises(DiscoveryError, match="already in your master CV"):
        elicit.write(cv_copy, [item], inventory=record, at=NOW)
    assert cv_copy.read_text(encoding="utf-8") == before


def test_a_write_that_would_add_something_else_is_refused(
    record: CvInventory, cv_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )

    original = elicit.rewritten

    def extra(source: str, edits: object) -> str:
        lines = original(source, edits).splitlines()  # type: ignore[arg-type]
        _, roles = spans("\n".join(lines))
        index = roles[record.roles[1].key].end
        lines[index:index] = ["", "- Something nobody confirmed"]
        return "\n".join(lines) + "\n"

    monkeypatch.setattr(elicit, "rewritten", extra)
    with pytest.raises(DiscoveryError, match="were confirmed"):
        elicit.write(cv_copy, [item], inventory=record, at=NOW)


def test_a_write_landing_in_the_wrong_place_is_refused(
    record: CvInventory, cv_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )

    def elsewhere(source: str, edits: object) -> str:
        lines = source.splitlines()
        sections, _ = spans(source)
        index = sections["Core Skills"].end - 1
        lines.insert(index, "- Built the retry layer for the ingestion jobs.")
        return "\n".join(lines) + "\n"

    monkeypatch.setattr(elicit, "rewritten", elsewhere)
    with pytest.raises(DiscoveryError, match="was confirmed as bullet"):
        elicit.write(cv_copy, [item], inventory=record, at=NOW)


def test_a_master_cv_that_moved_since_discovery_started_is_refused(
    record: CvInventory, cv_copy: Path
) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    without = "\n".join(
        line
        for line in cv_copy.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"### {record.roles[0].organization}")
    )
    with pytest.raises(DiscoveryError, match="not a heading"):
        elicit.plan(without, record, [item])


def test_an_unreadable_master_cv_is_refused(record: CvInventory, tmp_path: Path) -> None:
    item = confirmed(
        record,
        answered("I built the retry layer for the ingestion jobs myself."),
        "Built the retry layer for the ingestion jobs.",
    )
    with pytest.raises(DiscoveryError, match="Cannot read the master CV"):
        elicit.write(tmp_path / "gone.md", [item], inventory=record, at=NOW)


def transcript(*answers: str, confirm: bool = True) -> ScriptedPrompter:
    """Return a prompter scripted with one session's answers."""
    return ScriptedPrompter(answers=list(answers), confirm=confirm)


def test_a_typed_answer_is_taken_as_the_persons_own() -> None:
    (line,) = elicit.converse(
        transcript("I built the retry layer for the ingestion jobs myself.", "No.", "No."),
        elicit.lines_for([("Kubernetes", (), ())]),
    )
    assert line.answered
    assert [reply.source for reply in line.replies] == [Source.PERSON] * 3


def test_a_whole_session_asks_confirms_shows_and_writes(record: CvInventory, cv_copy: Path) -> None:
    prompter = transcript(
        "I built the retry layer for the ingestion jobs myself.",
        "No.",
        "It ran every night.",
        "Built the retry layer for the ingestion jobs.",
        "the ingestion team, over one quarter",
        record.roles[0].key,
        "how long it stayed in place",
    )
    outcome = elicit.discover(
        prompter,
        lines=elicit.lines_for([("Kubernetes", ("acme:r02",), ("acme",))]),
        inventory=record,
        path=cv_copy,
        at=NOW,
    )
    assert outcome.written is not None
    assert outcome.unclosed == ()
    assert len(outcome.confirmed) == 1
    # The change was shown, in place, before the question that wrote it.
    shown = prompter.transcript.index("+ - Built the retry layer")
    asked = prompter.transcript.index("Write 1 confirmed fact(s)")
    assert shown < asked
    assert "Built the retry layer for the ingestion jobs." in cv_copy.read_text(encoding="utf-8")


def test_declining_the_final_question_writes_nothing(record: CvInventory, cv_copy: Path) -> None:
    before = cv_copy.read_text(encoding="utf-8")
    prompter = ScriptedPrompter(
        answers=[
            "I built the retry layer for the ingestion jobs myself.",
            "No.",
            "It ran every night.",
            "Built the retry layer for the ingestion jobs.",
            "the ingestion team",
            record.roles[0].key,
            "",
        ],
        decline=["Write 1 confirmed"],
    )
    outcome = elicit.discover(
        prompter,
        lines=elicit.lines_for([("Kubernetes", (), ())]),
        inventory=record,
        path=cv_copy,
        at=NOW,
    )
    assert outcome.written is None
    assert outcome.confirmed
    assert cv_copy.read_text(encoding="utf-8") == before
    assert "Nothing was written." in prompter.transcript


def test_a_session_that_finds_nothing_hands_the_gaps_back(
    record: CvInventory, cv_copy: Path
) -> None:
    before = cv_copy.read_text(encoding="utf-8")
    outcome = elicit.discover(
        transcript("No."),
        lines=elicit.lines_for([("Rust", ("acme:r07",), ("acme",))]),
        inventory=record,
        path=cv_copy,
        at=NOW,
    )
    assert outcome.confirmed == ()
    assert outcome.written is None
    assert outcome.unclosed == ("acme:r07",)
    assert cv_copy.read_text(encoding="utf-8") == before


def test_a_refused_wording_is_reported_and_the_session_continues(
    record: CvInventory, cv_copy: Path
) -> None:
    prompter = transcript(
        "I sat in on the design reviews for it.",
        "No.",
        "No.",
        "Owned the design reviews.",
        "the platform team",
        record.roles[0].key,
        "",
    )
    outcome = elicit.discover(
        prompter,
        lines=elicit.lines_for([("Kubernetes", ("acme:r02",), ("acme",))]),
        inventory=record,
        path=cv_copy,
        at=NOW,
    )
    assert outcome.confirmed == ()
    assert outcome.written is None
    assert len(outcome.refusals) == 1
    assert "claims ownership" in outcome.refusals[0]


def test_a_subject_given_by_hand_needs_no_gap_identifiers() -> None:
    lines = elicit.lines_for([("Terraform", (), ())])
    assert lines[0].subject == "Terraform"
    assert lines[0].gap_ids == ()
    assert lines[0].key == "terraform"


def test_a_placement_is_offered_from_the_document_rather_than_guessed(
    record: CvInventory, cv_copy: Path
) -> None:
    prompter = transcript(
        "I built the retry layer for the ingestion jobs myself.",
        "No.",
        "No.",
        "Built the retry layer for the ingestion jobs.",
        "the ingestion team",
        "Core Skills",
        "",
    )
    elicit.discover(
        prompter,
        lines=elicit.lines_for([("Kubernetes", (), ())]),
        inventory=record,
        path=cv_copy,
        at=NOW,
    )
    for role in record.roles:
        assert role.key in prompter.transcript
    for section in record.sections:
        assert section in prompter.transcript
