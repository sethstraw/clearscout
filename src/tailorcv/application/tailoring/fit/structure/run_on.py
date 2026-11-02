"""Run-on units cut at the labels and closing statements run into them."""

from __future__ import annotations

import bisect
import re

from tailorcv.application.tailoring.fit.lexicon import (
    BARE_RANGE,
    BOILERPLATE_LINES,
    BOILERPLATE_SUBJECTS,
    BULLET,
    CLAUSE_HEADINGS,
    CURRENCY_MENTION,
    OPTIONAL_HEADING,
    PAY_CONTEXT,
    PAY_PERIODS,
    QUOTED_PAY,
    TRAILING_PAY,
    country_hint,
)
from tailorcv.application.tailoring.fit.structure.boilerplate import (
    is_boilerplate,
    names_a_qualification,
)
from tailorcv.application.tailoring.fit.structure.headings import (
    ASKING_KINDS,
    NAME_WORD,
    NAME_WORDS_AFTER,
    SECTION_FRAGMENTS,
    kind_of,
    section_word,
)
from tailorcv.application.tailoring.fit.structure.reading import SectionKind
from tailorcv.shared.layout import GLYPHS
from tailorcv.shared.text import sentence_breaks, sentence_pieces

# A label run into a unit mid-way: "... Preferred Qualifications: <items>" or "... <dot>
# Qualifications <dot> <item> <dot> <item>" (a middle-dot glyph), with up to three words
# before its heading word, the first capitalized. "the following qualifications:" is none.
_LABEL_END = re.compile(rf"\s?:(?=\s|$)|\s(?P<glyph>[{GLYPHS}])(?=\s)")


_LABEL_WORD = re.compile(r"\S+")


# A heading phrase run in with no colon: "... close What we are looking for 1-3 years".
_PHRASE_LABELS = tuple(
    fragment
    for fragments, kind in SECTION_FRAGMENTS
    if kind in (SectionKind.REQUIREMENT, SectionKind.PREFERRED)
    for fragment in fragments
    if len(fragment.split()) >= 3 and fragment not in CLAUSE_HEADINGS
)


_BOILERPLATE_MARKERS = (*BOILERPLATE_LINES, *BOILERPLATE_SUBJECTS)


def _run_on_pieces(unit: str) -> list[str]:
    """Cut a run-on unit at each label run into it and before a closing legal statement.

    A label becomes a heading line of its own, "Label:", over the text that follows it.
    """
    found = [*_colon_labels(unit), *_phrase_labels(unit), *_closing_statement(unit)]
    cuts = sorted(cut for cut in found if not _parts_pay(unit[: cut[0]], unit[cut[0] :]))
    if not cuts:
        return [unit]
    pieces = [unit[: cuts[0][0]]]
    for index, (start, end, rest, glyph) in enumerate(cuts):
        if start < end:
            pieces.append(unit[start:end].strip() + ":")
        text = unit[rest : cuts[index + 1][0] if index + 1 < len(cuts) else len(unit)]
        # Items a glyph label runs in are separated by the same glyph; a run quoting pay stays
        # whole, so its figure keeps the words around it.
        items = text.split(glyph) if glyph and not _quotes_pay(text) else [text]
        pieces += [f"- {item}" for item in items] if len(items) > 1 else items
    return [piece.strip() for piece in pieces if piece.strip() not in ("", "-")]


def _quotes_pay(text: str) -> bool:
    """Return whether ``text`` quotes a pay figure, with a currency or beside a pay word."""
    return bool(
        QUOTED_PAY.search(text)
        or TRAILING_PAY.search(text)
        or (BARE_RANGE.search(text) and PAY_CONTEXT.search(text))
    )


def _parts_pay(head: str, tail: str) -> bool:
    """Return whether cutting ``head`` from ``tail`` parts a pay figure from what it needs.

    The figure's sentence ends ``head``; ``tail`` holding its missing currency, pay period
    or, for a bare dollar sign, its country keeps the two together.
    """
    sentence = sentence_pieces(head.rstrip())[-1]
    if not _quotes_pay(sentence):
        return False
    named = QUOTED_PAY.search(sentence) or TRAILING_PAY.search(sentence)
    currency = named is None and (
        CURRENCY_MENTION.search(tail) is not None or any(sign in tail for sign in "$\u00a3\u20ac")
    )
    country = named is not None and "$" in named.group() and not country_hint(head)
    period = not any(pattern.search(sentence) for _, pattern in PAY_PERIODS)
    return bool(
        currency
        or (country and country_hint(tail))
        or (period and any(pattern.search(tail) for _, pattern in PAY_PERIODS))
    )


def _colon_labels(unit: str) -> list[tuple[int, int, int, str]]:
    """Return each label run in mid-unit, the longest that names a section winning.

    A colon label opening the unit's text is its own heading already, and one right after a
    capitalized word ("Our Key Strengths:") is part of a longer name.
    """
    opens = found.start("text") if (found := BULLET.match(unit)) else 0
    labels = []
    words = list(_LABEL_WORD.finditer(unit, opens))
    starts = [word.start() for word in words]
    for end in _LABEL_END.finditer(unit):
        glyph = end["glyph"] or ""
        # A glyph label heads a list, so two items at least: "Toronto <dot> Qualifications <dot>
        # Hybrid", with middle dots, is a location line.
        if glyph and glyph not in unit[end.end() :]:
            continue
        if (label := _label_ending_at(end, words, starts, glyph)) is not None:
            labels.append(label)
    return labels


def _label_ending_at(
    end: re.Match[str], words: list[re.Match[str]], starts: list[int], glyph: str
) -> tuple[int, int, int, str] | None:
    """Return the longest section label ending at ``end``, when it may open a section there."""
    count = bisect.bisect_left(starts, end.start())
    for index in range(max(0, count - NAME_WORDS_AFTER - 1), count):
        if not _labels_a_section(end.string[starts[index] : end.start()].strip()):
            continue
        before = words[index - 1].group() if index else ""
        if (index or glyph) and (
            not before or before[-1] in f".:;!?){GLYPHS}" or not before[:1].isupper()
        ):
            return words[index].start(), end.start(), end.end(), glyph
        return None
    return None


def _labels_a_section(label: str) -> bool:
    """Return whether a label run into a unit names a requirement or preferred section.

    A key ending in "Preference" ("Bonus Eligible Location Preference:") asks for a place or
    shift, never a section of qualifications.
    """
    folded = label.casefold()
    return (
        label[:1].isupper()
        and not folded.endswith(("preference", "preferences"))
        and not any(character.isdigit() or character in f".,;:(){GLYPHS}" for character in label)
        and (
            (OPTIONAL_HEADING.fullmatch(label) is not None and len(label.split()) > 1)
            or (
                (section := section_word(folded)) is not None
                and len(NAME_WORD.findall(folded[: section[0].start()])) <= NAME_WORDS_AFTER
            )
        )
        and kind_of(label) in ASKING_KINDS
    )


def _phrase_labels(unit: str) -> list[tuple[int, int, int, str]]:
    folded = unit.casefold()
    found = []
    for fragment in _PHRASE_LABELS:
        for match in re.finditer(rf"(?:\b[a-z]+\s+)?\b{re.escape(fragment)}\b", folded):
            start, end = match.span()
            following = unit[end:].lstrip()
            if (
                start
                and unit[start].isupper()
                and following[:1].isupper() | following[:1].isdigit()
            ):
                found.append((start, end, end, ""))
    return found


def _closing_statement(unit: str) -> list[tuple[int, int, int, str]]:
    """Return where a closing legal statement a run-on unit reaches opens, if it does."""
    folded = unit.casefold()
    marks = [at for marker in _BOILERPLATE_MARKERS if (at := folded.find(marker)) > 0]
    if not marks:
        return []
    ends = [end for _, end in sentence_breaks(unit)]
    opener = max((end for end in ends if end <= min(marks)), default=0)
    head, tail = unit[:opener], unit[opener:]
    if not opener or not names_a_qualification(head) or not is_boilerplate(tail):
        return []
    return [(opener, opener, opener, "")]


def unrun(
    units: list[str], *marks: set[int]
) -> tuple[list[str], set[int], set[int], set[int], dict[int, int]]:
    """Cut each run-on unit into pieces, keeping each mark on its unit's first piece.

    A bullet is never cut. The last value maps where each cut unit's pieces start to
    where they end, since what a label heads ends with its unit.
    """
    compact: list[str] = []
    moved: list[set[int]] = [set() for _ in marks]
    scopes: dict[int, int] = {}
    for position, unit in enumerate(units):
        for old, new in zip(marks, moved, strict=True):
            if position in old:
                new.add(len(compact))
        pieces = [unit] if BULLET.match(unit) else _run_on_pieces(unit)
        if len(pieces) > 1:
            scopes[len(compact)] = len(compact) + len(pieces)
        compact += pieces
    paragraphs, labels, titles = moved
    return compact, paragraphs, labels, titles, scopes
