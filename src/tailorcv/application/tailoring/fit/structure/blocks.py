"""A posting split into headed blocks."""

from __future__ import annotations

from tailorcv.application.tailoring.fit.lexicon import BARE_HEADING_WORDS, BULLET, SENTENCE_END
from tailorcv.application.tailoring.fit.structure.headings import (
    ASKING_KINDS,
    heading_of,
    kind_of,
    names_a_section,
    presented_heading,
)
from tailorcv.application.tailoring.fit.structure.reading import Block, SectionKind
from tailorcv.application.tailoring.fit.structure.run_on import unrun
from tailorcv.application.tailoring.fit.structure.units import (
    UnitReader,
    closes_section,
    reads_as_one_item,
)
from tailorcv.shared.text import sentence_breaks


def _run_in(line: str) -> tuple[str, tuple[str, ...]] | None:
    """Split "Requirements: A; B." into its section name and the items it runs in."""
    lead, colon, rest = line.partition(":")
    if not colon or not rest.strip() or BULLET.match(line) or not names_a_section(lead.strip()):
        return None
    if kind_of(lead) in (SectionKind.BOILERPLATE, SectionKind.OTHER):
        return None
    items = tuple(item.strip() for item in rest.split(";") if item.strip())
    return lead.strip(), items


def _opening(line: str, following: str, kind: SectionKind) -> tuple[str, list[str]] | None:
    """Return the heading a unit opens a block under and that block's first lines.

    Closing matter under a requirement or preferred heading opens an unnamed block,
    so it is judged as prose rather than inheriting the heading's stance.
    """
    if run_in := _run_in(line):
        name, items = run_in
        if BULLET.match(following):
            # A run-in item over a list is one of its items; a lead-in to the list is not.
            items = tuple(
                f"- {item}"
                if reads_as_one_item(item, last=False) and not item.endswith(":")
                else item
                for item in items
            )
        return name, list(items)
    if (found := heading_of(line, following)) is not None:
        return found, []
    if kind in ASKING_KINDS and closes_section(line):
        return "", [line]
    return None


def _kind_under(found: str, section: SectionKind | None) -> SectionKind:
    """Return a heading's kind, or the open section's when a sub-heading names no kind."""
    kind = kind_of(found)
    if section is not None and found and kind is SectionKind.OTHER:
        return section
    return kind


def _continues_run_in(line: str, new_paragraph: bool) -> bool:
    """Return whether a unit continues a run-in heading's items.

    A marked item does; an unmarked line does only in the heading's own paragraph.
    """
    if BULLET.match(line):
        return True
    return not new_paragraph and not closes_section(line) and reads_as_one_item(line, last=True)


def posting_blocks(posting: str) -> tuple[Block, ...]:
    """Split a posting into headed blocks, keeping the text before the first one."""
    compact, paragraphs, labels, titles, scopes = unrun(*UnitReader(posting).run())
    blocks: list[Block] = []
    heading, kind = "", SectionKind.OTHER
    current: list[str] = []
    # A requirement or preferred heading with no lines of its own groups the sub-headings under
    # it, until a sub-section whose own heading names another kind than the group's has ended.
    group: SectionKind | None = None
    # A run-in heading states its items on its own line, and only items continue it.
    run_in = False
    # The block a cut unit's pieces opened within. Where the pieces end it resumes only over a
    # headed block, never over an unheaded one.
    enclosing: dict[int, tuple[str, SectionKind, SectionKind | None, bool]] = {}
    for position, line in enumerate(compact):
        if position in scopes:
            enclosing[scopes[position]] = (heading, kind, group, run_in)
        opening = _unit_opening(
            compact,
            position,
            kind,
            labelled=position in labels,
            run_in=run_in,
            new_paragraph=position in paragraphs,
        )
        resumed = enclosing.get(position)
        if opening is None and resumed and heading and resumed[:2] != (heading, kind):
            blocks.append(Block(heading=heading, kind=kind, lines=tuple(current)))
            (heading, kind, group, run_in), current = resumed, [line]
            continue
        if opening is None:
            current.append(line)
            continue
        if current or heading:
            blocks.append(Block(heading=heading, kind=kind, lines=tuple(current)))
        group = _group_after(group, heading, kind, current)
        previous = current
        found, current = opening
        section = group
        if position in titles:
            section = _title_continues(compact, position, previous, kind, paragraphs | labels)
        heading, kind = presented_heading(found), _kind_under(found, section)
        run_in = bool(found and current)
    if current or heading:
        blocks.append(Block(heading=heading, kind=kind, lines=tuple(current)))
    return tuple(blocks)


def _unit_opening(
    compact: list[str],
    position: int,
    kind: SectionKind,
    *,
    labelled: bool,
    run_in: bool,
    new_paragraph: bool,
) -> tuple[str, list[str]] | None:
    """Return the heading and first lines the unit at ``position`` opens, or nothing."""
    line = compact[position]
    following = compact[position + 1] if position + 1 < len(compact) else ""
    opening = (line, []) if labelled else _opening(line, following, kind)
    # A unit a run-in heading does not continue opens an unnamed block.
    if opening is None and run_in and not _continues_run_in(line, new_paragraph):
        return "", [line]
    return opening


def _group_after(
    group: SectionKind | None, heading: str, kind: SectionKind, lines: list[str]
) -> SectionKind | None:
    """Return the group still open once the block under ``heading`` closes."""
    named = kind_of(heading)
    if group is None or kind not in ASKING_KINDS or named not in (SectionKind.OTHER, group):
        return kind if kind in ASKING_KINDS and heading and not lines else None
    return group


def _title_continues(
    compact: list[str], position: int, previous: list[str], kind: SectionKind, breaks: set[int]
) -> SectionKind | None:
    """Return the open section's kind when the title at ``position`` continues it."""
    # Only a title under the heading or right after one of the section's items continues
    # it; a list's items are its bullets, and an unmarked section's items are all its lines.
    item = (
        not previous
        or bool(BULLET.match(previous[-1]))
        or not any(BULLET.match(unit) for unit in previous)
    )
    # A title over prose opens a section of its own; over one-sentence items it is a
    # sub-heading of the section still open.
    return kind if item and not _heads_prose(compact, position, breaks) else None


def _heads_prose(compact: list[str], title: int, breaks: set[int]) -> bool:
    """Return whether the units under a title read as prose rather than one-sentence items.

    Prose holds a unit of more than one sentence or a lead-in ending in a colon, and a
    "Label: text" line is an item however many sentences it holds. The title's reach
    ends at the next paragraph, label, or heading.
    """
    for position in range(title + 1, len(compact)):
        unit = compact[position]
        following = compact[position + 1] if position + 1 < len(compact) else ""
        if position > title + 1 and (position in breaks or heading_of(unit, following) is not None):
            return False
        lead, colon, rest = unit.partition(":")
        labelled = (
            colon
            and rest.strip()
            and lead[:1].isupper()
            and len(lead.split()) <= BARE_HEADING_WORDS
            and not any(end in lead for end in SENTENCE_END)
        )
        if not labelled and (sentence_breaks(unit) or unit.endswith(":")):
            return True
    return False
