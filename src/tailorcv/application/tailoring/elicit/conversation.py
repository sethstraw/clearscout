"""Person-only questioning and confirmation for CV discovery."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.stages.verify import WORD, bare_word
from tailorcv.domain.taxonomy import normalize, says
from tailorcv.infrastructure.providers.credentials import Prompter
from tailorcv.shared.errors import DiscoveryError

from .gates import Pending, Placement, propose
from .journal import Written, _read, write
from .plan import plan, preview
from .vocabulary import (
    BROAD,
    FUNCTION_WORDS,
    LADDER,
    MAX_PROBES,
    NARROW,
    QUESTIONS,
    REQUIRED,
    SUBSTANTIVE_WORDS,
    Angle,
    Ownership,
    Point,
    Source,
    _is_refusal,
    figures,
    reads_as,
)


@dataclass(frozen=True)
class Question:
    """One question, asked on its own.

    Handed out one at a time by :meth:`Line.next_question`, which reads the
    replies so far, because a list generated up front could not narrow.
    """

    id: str
    subject: str
    angle: Angle
    text: str

    @property
    def broad(self) -> bool:
        """Return whether this is one of the opening questions about a subject."""
        return self.angle in BROAD


@dataclass(frozen=True)
class Reply:
    """One answer, and where it came from.

    ``source`` is the first of the two barriers around fabrication. It defaults
    to the person because a caller that says nothing is a terminal reading what
    somebody typed; anything else has to say so, and is then refused.
    """

    text: str
    source: Source = Source.PERSON

    @property
    def refused(self) -> bool:
        """Return whether this answer is a complete "no"."""
        return _is_refusal(self.text)

    @property
    def substantive(self) -> bool:
        """Return whether this answer said something a claim could rest on."""
        if self.refused:
            return False
        words = [word for word in WORD.findall(self.text) if bare_word(word) not in FUNCTION_WORDS]
        return len(words) >= SUBSTANTIVE_WORDS or bool(reads_as(self.text).stated)


@dataclass(frozen=True)
class Line:
    """One line of questioning about one subject, and everything it produced.

    Immutable: :meth:`with_reply` returns the next state, so the conversation is
    a fold over replies and drives without a terminal.
    """

    subject: str
    # Track requirements so unanswered subjects return their gaps.
    gap_ids: tuple[str, ...] = ()
    postings: tuple[str, ...] = ()
    asked: tuple[Question, ...] = ()
    replies: tuple[Reply, ...] = ()
    answered: bool = False
    closed: str = ""

    @property
    def key(self) -> str:
        """Return a short stable handle for this subject, used in question ids."""
        return "-".join(normalize(self.subject).split())[:40] or "subject"

    @property
    def probes(self) -> int:
        """Return how many questions found nothing."""
        return sum(1 for reply in self.replies if not reply.substantive and not reply.refused)

    @property
    def open(self) -> bool:
        """Return whether this line still has something to ask."""
        return not self.closed

    def said(self) -> str:
        """Return everything the person said in this line, as one body of text."""
        return "\n".join(reply.text for reply in self.replies)

    def next_question(self) -> Question | None:
        """Return the one question to ask next, or nothing when the line is done.

        Broad first, then narrow, and the narrowing set depends on what the
        answers already covered: somebody who has already said they led two
        engineers is not asked whether they led anyone.
        """
        if self.closed:
            return None
        used = {question.angle for question in self.asked}
        remaining = [angle for angle in self._order() if angle not in used]
        if not remaining:
            return None
        angle = remaining[0]
        return Question(
            id=f"{self.key}-{len(self.asked) + 1:02d}",
            subject=self.subject,
            angle=angle,
            text=QUESTIONS[angle].format(subject=self.subject),
        )

    def with_reply(self, reply: Reply) -> Line:
        """Return this line after one answer, refusing an answer nobody gave.

        A "no" closes the line here rather than counting as a failed probe.
        """
        if reply.source is not Source.PERSON:
            raise DiscoveryError(
                f"Discovery was handed an answer from {reply.source.value}, which cannot "
                "become evidence.\n"
                "  Only what the person types is an answer here. Generated resume text, a "
                "model's\n  suggestion, and the job description are the three ways a claim "
                "nobody made would\n  reach the master CV, so they are refused by name."
            )
        asked = self.asked
        question = self.next_question()
        if question is not None:
            asked = (*asked, question)
        updated = replace(self, asked=asked, replies=(*self.replies, reply))
        if reply.refused:
            # A negative answer closes the current question without changing evidence state.
            if question is None or question.broad:
                return replace(updated, closed='you answered "no", which is a complete answer')
            return updated
        if reply.substantive:
            return replace(updated, answered=True)
        if updated.probes >= MAX_PROBES:
            return replace(
                updated,
                closed=f"{MAX_PROBES} questions about this found nothing to write down",
            )
        return updated

    def finished(self) -> Line:
        """Return this line closed, whatever state it reached.

        Called when the questions run out rather than when an answer ends it, so
        that a line which answered everything still records why it stopped.
        """
        if self.closed:
            return self
        if self.answered:
            return replace(self, closed="every angle for this subject was asked")
        return replace(self, closed="nothing was found for this subject")

    def _order(self) -> tuple[Angle, ...]:
        """Return the angles still worth asking, in the order to ask them."""
        if not self.answered:
            return BROAD
        said = self.said()
        return tuple(angle for angle in NARROW if not _covered(angle, said))


def _covered(angle: Angle, said: str) -> bool:
    """Return whether the answers already address one narrowing angle."""
    if angle is Angle.LEADERSHIP:
        return bool(says(said, dict(LADDER)[Ownership.LEADERSHIP]))
    return bool(figures(said)) or bool(says(said, dict(LADDER)[Ownership.DEPLOYMENT]))


def lines_for(subjects: Sequence[tuple[str, tuple[str, ...], tuple[str, ...]]]) -> tuple[Line, ...]:
    """Return one line of questioning per subject, in the order given.

    The subjects come from somewhere that already knows what is missing: the
    gaps in an evidence map, or a batch's shared discovery queue.
    """
    return tuple(
        Line(subject=subject, gap_ids=gap_ids, postings=postings)
        for subject, gap_ids, postings in subjects
    )


def converse(prompter: Prompter, lines: Sequence[Line]) -> tuple[Line, ...]:
    """Ask every line's questions, one at a time, and return what they found.

    The only function in the questioning half that needs a terminal; everything
    it decides is decided by :class:`Line`.
    """
    finished: list[Line] = []
    for line in lines:
        current = line
        prompter.plain("")
        prompter.plain(f"About: {current.subject}")
        while (question := current.next_question()) is not None:
            answer = prompter.ask(question.text)
            current = current.with_reply(Reply(text=answer))
        current = current.finished()
        prompter.plain(f"  {current.closed}.")
        finished.append(current)
    return tuple(finished)


def unclosed(lines: Sequence[Line], written: Sequence[Confirmed] = ()) -> tuple[str, ...]:
    """Return the requirement identifiers discovery did not close.

    A gap is closed by confirmed material that was written and by nothing else,
    so the default closes nothing.
    """
    closed = {gap_id for item in written for gap_id in item.material.gap_ids}
    return tuple(
        dict.fromkeys(gap_id for line in lines for gap_id in line.gap_ids if gap_id not in closed)
    )


@dataclass(frozen=True)
class Confirmed:
    """Pending material a person confirmed, point by point, at a moment in time."""

    material: Pending
    points: tuple[Point, ...]
    at: str


def confirm(material: Pending, points: Iterable[Point], *, at: datetime) -> Confirmed:
    """Return confirmed material, or refuse and name what was not confirmed."""
    given = set(points)
    missing = sorted(point.value for point in REQUIRED - given)
    if missing:
        raise DiscoveryError(
            f"{len(missing)} thing(s) about that material were not confirmed: "
            f"{'; '.join(missing)}.\n"
            "  Discovered material stays pending until every one of them is confirmed, because\n"
            "  the master CV is what every other part of this tool treats as true."
        )
    return Confirmed(
        material=material,
        points=tuple(sorted(given, key=lambda point: point.value)),
        at=at.isoformat(timespec="seconds"),
    )


def confirm_with(prompter: Prompter, material: Pending, *, at: datetime) -> Confirmed | None:
    """Ask the eight confirmations one at a time, or return nothing.

    Declining any one of them ends it. There is no partial confirmation.
    """
    prompter.plain("")
    prompter.plain("Discovered, and not recorded anywhere yet:")
    for row in material.describe():
        prompter.plain(row)
    prompter.plain("")
    given: list[Point] = []
    for point in Point:
        if not prompter.confirm(f"Confirm {point.value}?", default=False):
            prompter.plain(f"  Not confirmed: {point.value}. Nothing was recorded.")
            return None
        given.append(point)
    return confirm(material, given, at=at)


def gather(
    prompter: Prompter, lines: Sequence[Line], inventory: CvInventory
) -> tuple[tuple[Pending, ...], tuple[str, ...]]:
    """Turn every answered line into pending material, and collect the refusals.

    A refusal is reported and the line is skipped rather than ending the session,
    so somebody told their wording claims more than their answers do can carry on
    to the next subject.
    """
    pending: list[Pending] = []
    refusals: list[str] = []
    for line in lines:
        if not line.answered:
            continue
        prompter.plain("")
        prompter.plain(f"About: {line.subject}")
        wording = prompter.ask("In one sentence, in your own words, what is the fact?")
        scope = prompter.ask("What was the scope: whose work, over what, for how long?")
        placement = _asked_placement(prompter, inventory)
        ambiguous = prompter.ask("What about this is still unclear or approximate?", default="")
        try:
            pending.append(
                propose(
                    line,
                    text=wording,
                    scope=scope,
                    placement=placement,
                    inventory=inventory,
                    ambiguities=[ambiguous] if ambiguous.strip() else [],
                )
            )
        except DiscoveryError as error:
            prompter.plain("")
            prompter.plain(str(error))
            refusals.append(f"{line.subject}: {str(error).splitlines()[0]}")
    return tuple(pending), tuple(refusals)


def _asked_placement(prompter: Prompter, inventory: CvInventory) -> Placement:
    """Ask where in the master CV this belongs, offering what is actually there."""
    roles = [role.key for role in inventory.roles]
    choices = ", ".join([*roles, *inventory.sections])
    answer = prompter.ask(
        f"Which role or section does this belong under? ({choices})",
        default=roles[0] if roles else "",
    ).strip()
    return Placement(role_key=answer) if answer in roles else Placement(section=answer)


@dataclass(frozen=True)
class Outcome:
    """Everything one discovery session produced, written or not."""

    lines: tuple[Line, ...]
    confirmed: tuple[Confirmed, ...]
    unclosed: tuple[str, ...]
    refusals: tuple[str, ...]
    written: Written | None = None


def discover(
    prompter: Prompter,
    *,
    lines: Sequence[Line],
    inventory: CvInventory,
    path: Path,
    at: datetime,
) -> Outcome:
    """Run one whole discovery session: ask, propose, confirm, show, and write.

    The order is the guarantee: nothing is proposed until a line has an answer,
    nothing is confirmed until it is printed field by field, and nothing is
    written until the exact change has been shown in place and agreed to.
    """
    asked = converse(prompter, lines)
    pending, refusals = gather(prompter, asked, inventory)
    confirmed = [
        item
        for material in pending
        if (item := confirm_with(prompter, material, at=at)) is not None
    ]
    outcome = Outcome(
        lines=asked,
        confirmed=tuple(confirmed),
        unclosed=unclosed(asked),
        refusals=refusals,
    )
    if not confirmed:
        return outcome
    source = _read(path)
    prompter.plain("")
    prompter.plain(f"This is the change to {path}, and it has not been made yet:")
    prompter.plain("")
    prompter.plain(preview(source, plan(source, inventory, confirmed)))
    prompter.plain("")
    if not prompter.confirm(f"Write {len(confirmed)} confirmed fact(s) to {path.name}?", False):
        prompter.plain("Nothing was written.")
        return outcome
    return replace(
        outcome,
        written=write(path, confirmed, inventory=inventory, at=at),
        unclosed=unclosed(asked, confirmed),
    )
