"""Normalize the character-level noise a job board introduces into posting text.

Scraped postings carry smart quotes, non-breaking and zero-width spaces, HTML
entities, and several dash glyphs, all of which defeat rules spelled in plain
ASCII. Folding them once, before any extractor runs, keeps every downstream rule
ASCII and makes this the single place to teach the reader a new board's quirk.
It also owns where a sentence ends, which every reader of posting prose splits on.
"""

from __future__ import annotations

import html
import re
from os.path import commonprefix

from clearscout.shared.layout import RECRUITER_TAG

# Each code point is rewritten to the ASCII on the right. Grouped by what a board
# substitutes: quotes, spaces, invisibles, and dashes.
_FOLDS: dict[int, str] = {
    # Single quotes and the prime, which stand in for the apostrophe.
    0x2018: "'",
    0x2019: "'",
    0x201A: "'",
    0x201B: "'",
    0x2032: "'",
    # Double quotes and the double prime.
    0x201C: '"',
    0x201D: '"',
    0x201E: '"',
    0x201F: '"',
    0x2033: '"',
    # Spaces that are not U+0020: no-break, narrow, thin, en/em, and friends.
    0x00A0: " ",
    0x2007: " ",
    0x2009: " ",
    0x200A: " ",
    0x2002: " ",
    0x2003: " ",
    0x2005: " ",
    0x202F: " ",
    0x3000: " ",
    # Invisibles that split a word for a literal match: zero-width and soft hyphen.
    0x00AD: "",
    0x200B: "",
    0x200C: "",
    0x200D: "",
    0x2060: "",
    0xFEFF: "",
    # Dash glyphs that stand in for the hyphen, including the range en/em dash.
    0x2010: "-",
    0x2011: "-",
    0x2012: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0x2212: "-",
    # Wave dash, wavy dash, tilde operator and swung dash, which mark a range.
    0x301C: "~",
    0x3030: "~",
    0x223C: "~",
    0x2053: "~",
    # Fullwidth currency signs outside the fullwidth ASCII block.
    0xFFE0: "\N{CENT SIGN}",
    0xFFE1: "\N{POUND SIGN}",
    0xFFE5: "\N{YEN SIGN}",
    0xFFE6: "\N{WON SIGN}",
    # Fullwidth ASCII sits at a fixed offset from the character it doubles.
    **{code: chr(code - 0xFEE0) for code in range(0xFF01, 0xFF5F)},
}
_TABLE = str.maketrans(_FOLDS)
_ESCAPED_AMPERSANDS = re.compile(r"&(?:amp;|#0*38;|#x0*26;)+", re.IGNORECASE)


def sanitize(text: str) -> str:
    """Return posting text with board character-noise folded to ASCII equivalents.

    Idempotent and structure-preserving: line breaks and bullets are untouched, so
    only characters change. Safe on already-clean text, which passes through.
    """
    if not text:
        return text
    folded = text.translate(_TABLE)
    # A doubly escaped entity, or a fullwidth ampersand, decodes only on a further pass;
    # collapsing each run of escaped ampersands first keeps a long run to one pass.
    while (
        decoded := html.unescape(_ESCAPED_AMPERSANDS.sub("&", folded)).translate(_TABLE)
    ) != folded:
        folded = decoded
    return folded


# One raw entity as ``sanitize`` decodes it: a run of escaped ampersands and the entity body
# that follows it, so "&amp;amp;#39;" is one unit.
_ENTITY = re.compile(
    r"&(?:(?:amp;|#0*38;|#x0*26;)&?)*(?:#[0-9]+;?|#x[0-9a-f]+;?|[^\t\n\f <&#;]{1,32};?)?",
    re.IGNORECASE,
)


def sanitize_with_offsets(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return ``sanitize(text)`` and the raw ``(start, end)`` span of each of its characters.

    Every character decoded from one entity maps to that entity's whole raw span.
    """
    # Entities are found after folding, as ``sanitize`` decodes them: a fullwidth digit or
    # an invisible inside one folds first. Each fold yields at most one character.
    origins = [index for index, char in enumerate(text) for _ in char.translate(_TABLE)]
    units: list[tuple[int, int]] = []
    position = 0
    for entity in _ENTITY.finditer(text.translate(_TABLE)):
        start, end = origins[entity.start()], origins[entity.end() - 1] + 1
        units.extend((index, index + 1) for index in range(position, start))
        units.append((start, end))
        position = end
    units.extend((index, index + 1) for index in range(position, len(text)))
    folded: list[str] = []
    spans: list[tuple[int, int]] = []
    for start, end in units:
        raw = text[start:end]
        piece = sanitize(raw)
        folded.append(piece)
        # A tail that decoding left unchanged, the "D" of "&amp;D", keeps its own offsets.
        kept = len(commonprefix([piece[::-1], raw[::-1]]))
        if piece != raw:
            # "&#59;" decodes to its own last character, which still spells the whole entity.
            kept = min(kept, max(len(piece) - 1, 0))
        spans.extend((start, end - kept) for _ in range(len(piece) - kept))
        spans.extend((index, index + 1) for index in range(end - kept, end))
    return "".join(folded), spans


# A sentence ends at ".", "!" or "?", bracketed or not, and whitespace, unless a lowercase
# word goes on ("etc. are") or the word before closes a dotted run or is a short word:
# "Ph.D. in", "U.S. Citizen", "Sr. Manager", "incl. Level 2", "No. 5".
_SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][)\]\"']))\s+(?!\W*[a-z])")
_ABBREVIATION = re.compile(
    r"(?i)(?:(?<=\.)[a-z]|\b(?:sr|jr|dr|mr|mrs|ms|st|mt|ft|inc|ltd|llc|co|corp|dept|vs|approx"
    r"|incl|esp|min|max|yrs|req|pref))\.$"
)
_NUMBER_SIGN = re.compile(r"(?i)\bno\.$")
# A capital dotted run or a company word still ends a sentence before a word that opens a
# clause: "in the U.S. This role", "Acme Inc. The team"; "U.S. Army" stays whole.
_ENDS_BEFORE_AN_OPENER = re.compile(r"(?:(?<=\.)[A-Z]|\b(?i:inc|ltd|llc|co|corp))\.$")
_CLAUSE_OPENER = re.compile(
    r"\W*(?:This|The|These|Those|That|There|We|You|Our|Your|They|Their|It|Its|Must|Should"
    r"|Candidates|Applicants|Everyone|All|Each)\b"
)


# A recruiter tag ("#LI-REMOTE") is a sentence of its own when no lowercase word goes on.
_AROUND_A_TAG = re.compile(
    rf"(?<!\s)(\s*)(?:{RECRUITER_TAG.pattern})\b(?!\s+\W*(?-i:[a-z]))(\s*)", re.IGNORECASE
)


def _ends_a_sentence(text: str, start: int, end: int) -> bool:
    """Return whether the whitespace at ``start:end`` after a stop ends a sentence."""
    if _NUMBER_SIGN.search(text, 0, start):
        return not text[end : end + 1].isdigit()
    if not _ABBREVIATION.search(text, 0, start):
        return True
    return bool(_ENDS_BEFORE_AN_OPENER.search(text, 0, start) and _CLAUSE_OPENER.match(text, end))


def sentence_breaks(text: str) -> list[tuple[int, int]]:
    """Return the span of the whitespace at each sentence end in ``text``."""
    ends = {
        found.span()
        for found in _SENTENCE_BREAK.finditer(text)
        if _ends_a_sentence(text, *found.span())
    }
    for tag in _AROUND_A_TAG.finditer(text):
        ends |= {tag.span(side) for side in (1, 2) if tag.start(side) < tag.end(side)}
    return sorted(ends)


def split_at(text: str, breaks: list[tuple[int, int]]) -> list[str]:
    """Return the pieces of ``text`` between ``breaks``, which are sorted spans."""
    starts = [0, *(end for _, end in breaks)]
    ends = [*(start for start, _ in breaks), len(text)]
    return [text[start:end] for start, end in zip(starts, ends, strict=True)]


def sentence_pieces(text: str) -> list[str]:
    """Return ``text`` cut at each sentence end, keeping every character but the breaks."""
    return split_at(text, sentence_breaks(text))


def sentences(text: str) -> list[str]:
    """Return the sentences of ``text``, stripped, without empty ones."""
    return [piece.strip() for piece in sentence_pieces(text) if piece.strip()]
