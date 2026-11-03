"""Hard-wrapped posting text joined back into the units a reader sees."""

from __future__ import annotations

from clearscout.application.tailoring.fit.lexicon import (
    BARE_HEADING_WORDS,
    BULLET,
    HEADING_WORDS,
    MARKDOWN_HEADING,
    SENTENCE_END,
    TERMINAL,
)
from clearscout.application.tailoring.fit.structure.boilerplate import is_boilerplate, states_pay
from clearscout.application.tailoring.fit.structure.headings import (
    ELLIPSIS,
    heads_by_stance,
    kind_of,
    name_tail,
    names_a_section,
    section_word,
)
from clearscout.application.tailoring.fit.structure.reading import SectionKind
from clearscout.shared.layout import RECRUITER_TAG, RUNS_ON, unbalanced_brackets
from clearscout.shared.text import sentence_breaks


class UnitReader:
    """Join hard-wrapped text back into the units a reader sees, and say which open a paragraph.

    A posting arrives wrapped at whatever width the board used, so a phrase that
    spans the break is invisible to anything matching line by line. A unit is one
    bullet or one paragraph, ended by a blank line, a bullet marker, a heading, or
    a finished sentence, which is how a board renders a list carrying no bullets.
    Lines under a heading that each read as one item stay units of their own, and
    an unmarked line that is one of a list's items is marked as one. ``run`` also
    returns the labels that head the sentence under them, and the title-case ones
    among them.
    """

    def __init__(self, posting: str) -> None:
        """Hold the posting's stripped lines and nothing read yet."""
        self.lines = [raw.strip() for raw in posting.splitlines()]
        self.units: list[str] = []
        # Line indexes under a heading that each read as one unmarked item.
        self.listed: set[int] = set()
        # Line indexes of the unmarked items a list opens with before its marked ones.
        self.leading: set[int] = set()
        self.paragraphs: set[int] = set()
        self.labels: set[int] = set()
        self.titles: set[int] = set()
        # Whether the last unit is still open, so the next line may continue it.
        self.running = False
        self.blank = self.item = self.headed = False

    def run(self) -> tuple[list[str], set[int], set[int], set[int]]:
        """Walk every line and return the units, paragraph starts, labels, and titles."""
        for index, line in enumerate(self.lines):
            if not line:
                self.running, self.blank = False, True
                continue
            self._read(index, line)
            self.blank = False
        return self.units, self.paragraphs, self.labels, self.titles

    def _ended(self) -> bool:
        return bool(self.units) and self.units[-1].endswith(SENTENCE_END)

    def _following(self, index: int) -> str:
        return self.lines[index + 1] if index + 1 < len(self.lines) else ""

    def _read(self, index: int, line: str) -> None:
        """Start a unit with ``line`` or continue the one above with it."""
        if self._ended():
            self.running = False
        marked = bool(BULLET.match(line))
        described, after = self._beside_an_item(line, marked=marked)
        # A line standing on its own is judged as though the unit above had ended.
        own = after or (index in self.listed and not described)
        running = self.running and not own
        # A wrap continues its item; a sentence under a short label item may still head a list.
        heading = _ends_a_unit_as_heading(line, running=running) or (
            (described or not running) and heads_by_stance(line, self._following(index))
        )
        titled = not heading and self._titled(index, line)
        label = (after and not heading and _introduces(line, self._following(index))) or titled
        if not (heading or own or not self.running or marked or closes_section(line)):
            self.units[-1] = f"{self.units[-1]} {line}"
            return
        standing = after and not titled and _stands_as_item(self.lines, index)
        made = index in self.leading or standing
        self._record(
            index, line, heading=heading, label=label, titled=titled, made=made, marked=marked
        )

    def _beside_an_item(self, line: str, *, marked: bool) -> tuple[bool, bool]:
        """Return whether a line right under an item describes it, and whether it follows it."""
        beside = self.item and not self.blank
        # The sentence under a label item is its description, not an item of its own.
        described = beside and _introduces(self.units[-1], line)
        after = beside and not (marked or described) and _item_after(self.units[-1], line)
        return described, after

    def _titled(self, index: int, line: str) -> bool:
        """Return whether a line is a title over what follows it."""
        # A title after a section's own lines heads what follows it, on the next line or
        # past a blank one; straight under a heading, only one standing alone does.
        following = self._following(index)
        ended = self._ended()
        prose = following
        if not prose and (ended or self.headed):
            prose = next((later for later in self.lines[index + 1 :] if later), "")
        placed = ((self.blank or ended) and not self.headed) or (
            self.headed and not following and kind_of(self.units[-1]) is not SectionKind.OTHER
        )
        return bool(self.units) and placed and _titles(line, prose)

    def _record(
        self,
        index: int,
        line: str,
        *,
        heading: bool,
        label: bool,
        titled: bool,
        made: bool,
        marked: bool,
    ) -> None:
        """Append ``line`` as a new unit and note what it opens."""
        position = len(self.units)
        if self.blank:
            self.paragraphs.add(position)
        if label:
            self.labels.add(position)
        if titled:
            self.titles.add(position)
        self.units.append(f"- {line}" if made else line)
        self.running = not (heading or label)
        self.headed = heading
        self.item = marked or made
        if heading:
            run = _listed_under(self.lines, index)
            self.listed.update(run)
            self.leading.update(_leading_items(self.lines, run))


def _item_after(previous: str, line: str) -> bool:
    """Return whether an unmarked line stands on its own after the list item above it."""
    return (
        not _runs_on(previous)
        and not unbalanced_brackets(previous)
        and reads_as_one_item(line, last=True)
    )


def _stands_as_item(lines: list[str], index: int) -> bool:
    """Return whether a line standing on its own beside a list is one of its items.

    A heading, a question, a lead-in, closing matter, a label over prose, a line over
    a heading, or a short line over a list that opens after a blank line is not.
    """
    line = lines[index]
    following = lines[index + 1] if index + 1 < len(lines) else ""
    return not (
        line.endswith(("?", ":", *ELLIPSIS))
        or closes_section(line)
        or _ends_a_unit_as_heading(line, running=False)
        or heads_by_stance(line, following)
        or _introduces(line, following)
        or _ends_a_unit_as_heading(following, running=False)
        or _heads_the_next_list(lines, index)
    )


def _heads_the_next_list(lines: list[str], index: int) -> bool:
    """Return whether a short unpunctuated line stands over a list opening after a blank line."""
    line, rest = lines[index], lines[index + 1 :]
    if not rest or rest[0] or len(line.split()) > BARE_HEADING_WORDS or line.endswith(TERMINAL):
        return False
    return bool(BULLET.match(next((later for later in rest if later), "")))


# A label names its topic in a few words, as "Education" or "Technical depth" do.
_LABEL_WORDS = 3


def _introduces(
    label: str, line: str, *, words: int = _LABEL_WORDS, ends: tuple[str, ...] = SENTENCE_END
) -> bool:
    """Return whether a short unpunctuated label names the full sentence on the line under it."""
    found = BULLET.match(label)
    text = found.group("text") if found else label
    return (
        not BULLET.match(line)
        and len(text.split()) <= words
        and not text.endswith(TERMINAL)
        and line[:1].isupper()
        and line.endswith(ends)
    )


def _titles(line: str, following: str) -> bool:
    """Return whether a title-case line names the sentence or lead-in under it, as a title does.

    A line whose section word opens a phrase, as "Requirements Gathering Workshops" does,
    is no title.
    """
    folded = line.casefold()
    section = section_word(folded)
    phrase = section is not None and name_tail(folded[section[0].end() :], section[2]) is None
    return (
        not BULLET.match(line)
        and ":" not in line
        and not _runs_on(line)
        and not phrase
        and all(
            word[:1].isupper() or not word[:1].isalpha() or word.casefold() in RUNS_ON
            for word in line.split()
        )
        and line[:1].isupper()
        and _introduces(line, following, words=BARE_HEADING_WORDS, ends=(*SENTENCE_END, ":"))
    )


def _leading_items(lines: list[str], run: range) -> range:
    """Return the unmarked lines a list under a heading opens with, when marked items continue it.

    Two or more are items of that list; one alone over the marked items is their sub-heading.
    """
    first_marked = next((index for index in run if BULLET.match(lines[index])), None)
    if first_marked is None or first_marked - run.start < 2:
        return range(0)
    lead = range(run.start, first_marked)
    if all(_stands_as_item(lines, index) for index in lead):
        return lead
    return range(0)


def _ends_a_unit_as_heading(line: str, *, running: bool) -> bool:
    """Return whether a line is a heading, which ends the unit before it and its own."""
    if MARKDOWN_HEADING.match(line):
        return True
    if BULLET.match(line):
        return False
    if line.endswith(":") and len(line.split()) <= HEADING_WORDS:
        return True
    return not running and names_a_section(line)


def _listed_under(lines: list[str], heading: int) -> range:
    """Return the lines under a heading that each read as one unmarked item.

    The lines are the rest of the heading's paragraph, or the next paragraph when
    the heading stands alone, up to any closing matter. Each must open a sentence
    and hold only one, and none but the last may end mid-phrase, or the lines are
    one wrapped paragraph.
    """
    start = heading + 1
    while start < len(lines) and not lines[start]:
        start += 1
    end = start
    while end < len(lines) and lines[end] and not closes_section(lines[end]):
        end += 1
    run = lines[start:end]
    if len(run) < 2 or not all(
        reads_as_one_item(line, last=position == len(run) - 1) for position, line in enumerate(run)
    ):
        return range(0)
    return range(start, end)


def reads_as_one_item(line: str, *, last: bool) -> bool:
    """Return whether a line of an unmarked run is one item rather than part of a wrap."""
    return (
        (line[:1].isupper() or line[:1].isdigit() or bool(BULLET.match(line)))
        and not sentence_breaks(line)
        and not unbalanced_brackets(line)
        and (last or not _runs_on(line))
    )


def _runs_on(line: str) -> bool:
    """Return whether a line ends mid-phrase, on a comma or a word the next line completes."""
    return line.endswith(",") or line.rsplit(maxsplit=1)[-1].casefold() in RUNS_ON


def closes_section(line: str) -> bool:
    """Return whether an unmarked line is closing matter, which ends the section above it.

    A quoted pay figure, legal or benefits language, a recruiter tag, or a closing
    heading run into its paragraph ends a section; a marked item never does.
    """
    if BULLET.match(line):
        return False
    return bool(
        RECRUITER_TAG.match(line)
        or is_boilerplate(line)
        or states_pay(line)
        or _glued_closing_heading(line)
    )


# A closing heading run into prose is followed by a sentence, not a two-word item tail.
_GLUED_TEXT_WORDS = 3


def _glued_closing_heading(line: str) -> bool:
    """Return whether a line opens with a closing section's name run into its first sentence."""
    words = line.split()
    return any(
        words[size][:1].isupper()
        and len(words) - size >= _GLUED_TEXT_WORDS
        and names_a_section(head := " ".join(words[:size]))
        and kind_of(head) is SectionKind.BOILERPLATE
        for size in range(1, min(BARE_HEADING_WORDS, len(words)))
    )
