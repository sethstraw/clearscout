"""The one plain-text layout every posting body reaches, however it was built.

Markup, a PDF text layer, a text file, and a vision transcript each arrive with
their own line ends and list glyphs. ``plain_layout`` writes them in one form:
LF line ends, single spaces, a list item opening "- " or "N. ", items of one
list on adjacent lines, and never more than one blank line.
"""

from __future__ import annotations

import re

# An item line of the layout: "- " for an unordered item, "N. " for an ordered one.
ITEM_MARKER = re.compile(r"(?:- |\d+\. )")

# Glyphs boards, word processors, and PDF symbol fonts set at the start of an item,
# the last six from the private-use range a Symbol or Wingdings bullet extracts to.
GLYPHS = "".join(
    chr(code)
    for code in (
        *(0x2022, 0x2023, 0x2043, 0x2219, 0x00B7, 0x25CF, 0x25CB, 0x25AA, 0x25AB, 0x25E6),
        *(0x25A0, 0x25A1, 0x25C6, 0x25C7, 0x25BA, 0x25B8, 0x25B6, 0x27A2, 0x27A4, 0x2794),
        *(0x2192, 0x2713, 0x2714, 0x2705, 0x2756, 0x2605, 0x1F539, 0x1F538, 0x1F449),
        *(0xF0B7, 0xF0A7, 0xF0A8, 0xF0D8, 0xF0FC, 0xF076),
    )
)
_DASHES = "".join(chr(code) for code in (0x2012, 0x2013, 0x2014, 0x2015, 0x2212))
_LINE_END = re.compile("\r\n?|[\x0b\x0c\x85\u2028]")
_PARAGRAPH_END = "\u2029"
_SPACES = re.compile(r"[^\S\n]+")
_EDGES = " \u200b\u200c\u200d\u2060\ufeff"

_GLYPH = re.compile(f"^[{GLYPHS}]\ufe0f?\\s*(?=\\S)")
_GLYPH_ONLY = re.compile(f"^[{GLYPHS}]\ufe0f?$")
_MARKDOWN_ITEM = re.compile(f"^(?:[*+]|[{_DASHES}])\\s+(?=\\S)")
_NUMBERED = re.compile(r"^\(?(\d{1,2})\)\s*(?=\S)")
# Markers that are also prose, so a line counts as an item only beside another like it.
_PAIRED_MARKERS = (
    re.compile(r"^-(?=[A-Za-z0-9(])"),
    re.compile(r"^\*(?=[A-Za-z0-9(])(?!.*\*$)"),
    re.compile(r"^o\s+(?=\S)"),
    re.compile(r"^\(?[a-z][.)]\s+(?=\S)"),
)
# A colon inside brackets, as in "Stack (primary: Python):", does not end the lead.
_INLINE_LEAD = re.compile(r"^(?P<lead>(?:[^:()]|\([^()]*\)){1,80}?):\s*(?P<rest>\S.*)$")
_INLINE_GLYPH = re.compile(f"(?:^|\\s+)[{GLYPHS}]\ufe0f?\\s+")
_HEADING_WORDS = 10
# A run of separators with no glyph before the first item must split into phrases.
_PHRASE_WORDS = 3
_EMPHASIS = re.compile(
    r"^(?P<hash>#{1,6} )?(?P<mark>\*\*|__|\*)"
    r"(?P<text>[^*_\s](?:[^*]*[^*_\s])?)(?P=mark)(?P<colon>:?)$"
)
# LinkedIn's recruiter tag encodes the place or the recruiter, never the opening.
RECRUITER_TAG = re.compile(r"#LI-[A-Za-z0-9]+", re.IGNORECASE)
# Glued to the next block with no space, a LinkedIn tag reads as part of its first word.
_GLUED_TAG = re.compile(r"[^\S\n]*(?P<tag>(?i:#li-?(?:remote|hybrid|onsite)))(?=[A-Z])")
# A sentence end glued to the next sentence; the word before it is checked separately.
_GLUED_SENTENCE = re.compile(r"(?<!\S)(?P<word>\S+?)(?P<end>[.!?])(?=(?P<next>[A-Z][a-z]+))")
_DOMAIN_ENDINGS = frozenset(("com", "org", "net", "io", "co", "ai", "dev", "edu", "gov", "us"))
# Items of one list sit on adjacent lines; a list restarting at "1." is a list of its own.
_BLANK_BETWEEN_ITEMS = re.compile(r"(?m)^((?:- |\d+\. ).*)\n\n+(?=- |(?!1\. )\d+\. )")
_TERMINAL = (".", "!", "?", ",", ";")
_BARE_HEADING_WORDS = 8
# A line ending in one of these runs on into the next line rather than ending there.
RUNS_ON = frozenset(
    (
        *("a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the"),
        *("to", "with", "that", "this", "our", "your", "their", "is", "are", "be", "will", "who"),
    )
)


def plain_layout(text: str) -> str:
    """Return posting text in the canonical layout; the layout of its own output is unchanged."""
    text = _LINE_END.sub("\n", text).replace(_PARAGRAPH_END, "\n\n")
    text = _GLUED_TAG.sub(_own_line, text)
    # Matches overlap, so "SQL.Python.Tableau" spaces one end per pass.
    while (spaced := _GLUED_SENTENCE.sub(_spaced_sentence, text)) != text:
        text = spaced
    # Emphasis comes off first, so the markers and edges it wrapped are read in this pass.
    lines = [_unemphasized(_SPACES.sub(" ", line).strip(_EDGES)) for line in text.split("\n")]
    lines = [
        piece for line in (line.strip(_EDGES) for line in lines) for piece in _inline_items(line)
    ]
    lines = _headings_set_apart(_marked(lines))
    text = _BLANK_BETWEEN_ITEMS.sub(r"\1\n", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _own_line(found: re.Match[str]) -> str:
    starts_line = found.start() == 0 or found.string[found.start() - 1] == "\n"
    return f"{'' if starts_line else chr(10)}{found['tag']}\n"


def _spaced_sentence(found: re.Match[str]) -> str:
    """Put a space after a glued sentence end unless the word is an address or abbreviation."""
    word, end = found["word"], found["end"]
    # "U.S.", "e.g.", a URL, an email, and "example.Com" all carry the mark inside a token.
    # A lead's colon ends the token, since the lead may later stand on a line of its own.
    domain = found["next"].casefold() in _DOMAIN_ENDINGS
    inside_token = domain or any(mark in word.rsplit(":", 1)[-1] for mark in "./@")
    return f"{word}{end}" if inside_token else f"{word}{end} "


def unbalanced_brackets(line: str) -> bool:
    """Return whether a line is half of a bracketed phrase wrapped across two lines."""
    return line.count("(") != line.count(")")


def _headings_set_apart(lines: list[str]) -> list[str]:
    """Put a blank line between a list's last item and a heading line that follows it."""
    spaced: list[str] = []
    for index, line in enumerate(lines):
        previous = spaced[-1] if spaced else ""
        following = lines[index + 1] if index + 1 < len(lines) else ""
        after_list = (
            bool(ITEM_MARKER.match(previous))
            and not previous.endswith(("-", ","))
            and not unbalanced_brackets(previous)
        )
        run_on = previous.rsplit(maxsplit=1)[-1].casefold() in RUNS_ON if previous else False
        if after_list and not run_on and _heading_line(line, following):
            spaced.append("")
        spaced.append(line)
    return spaced


def _heading_line(line: str, following: str) -> bool:
    """Return whether a line reads as a heading by its shape alone."""
    if (
        not line
        or ITEM_MARKER.match(line)
        or not (line[:1].isupper() or line[:1] == "#")
        or unbalanced_brackets(line)
    ):
        return False
    words = len(line.split())
    if line.startswith("#") or (line.endswith(":") and words <= _HEADING_WORDS):
        return True
    return (
        words <= _BARE_HEADING_WORDS
        and not line.endswith(_TERMINAL)
        and bool(ITEM_MARKER.match(following))
    )


def _inline_items(line: str) -> list[str]:
    """Split a line carrying a run of glyph-separated items into one line per item."""
    if ITEM_MARKER.match(line):
        return [line]
    lead = _INLINE_LEAD.match(line)
    if lead and len(lead["lead"].split()) <= _HEADING_WORDS:
        head, rest = f"{lead['lead']}:", lead["rest"]
    elif _GLYPH.match(line):
        head, rest = "", line
    else:
        return [line]
    first = _INLINE_GLYPH.search(rest)
    if first is None:
        return [line]
    # Only the glyph that opens the run separates it, so a middle dot inside an item stays.
    glyph = first.group().strip()[0]
    pieces = [
        piece.strip(_EDGES) for piece in re.split(rf"(?:^|\s+){re.escape(glyph)}\ufe0f?\s+", rest)
    ]
    items = [piece for piece in pieces if piece]
    if len(items) < 2:
        return [line]
    if pieces[0] and any(len(item.split()) < _PHRASE_WORDS for item in items):
        return [line]
    return ([head] if head else []) + [f"- {item}" for item in items]


def _marked(lines: list[str]) -> list[str]:
    """Write every recognised item marker as the layout's own."""
    lines = _joined_glyph_lines(lines)
    paired = [_paired_marker(line) for line in lines]
    filled = [index for index, line in enumerate(lines) if line]
    marked = list(lines)
    for position, index in enumerate(filled):
        line = lines[index]
        if ITEM_MARKER.match(line):
            continue
        if found := _GLYPH.match(line) or _MARKDOWN_ITEM.match(line):
            marked[index] = "- " + line[found.end() :]
        elif found := _NUMBERED.match(line):
            marked[index] = f"{int(found[1])}. " + line[found.end() :]
        elif (own := paired[index]) is not None:
            kinds = [
                neighbour[0]
                for other in (position - 1, position + 1)
                if 0 <= other < len(filled) and (neighbour := paired[filled[other]])
            ]
            if own[0] in kinds:
                marked[index] = "- " + line[own[1] :]
    return marked


def _joined_glyph_lines(lines: list[str]) -> list[str]:
    """Join a glyph a PDF set on a line of its own to the text on the next line."""
    joined: list[str] = []
    waiting = ""
    for line in lines:
        if _GLYPH_ONLY.match(line):
            waiting = line
        elif waiting and line:
            joined.append(f"{waiting} {line}")
            waiting = ""
        elif not waiting:
            joined.append(line)
    return joined + ([waiting] if waiting else [])


def _paired_marker(line: str) -> tuple[int, int] | None:
    """Return which prose-like marker opens a line, and where its text starts."""
    for kind, marker in enumerate(_PAIRED_MARKERS):
        if found := marker.match(line):
            return kind, found.end()
    return None


def _unemphasized(line: str) -> str:
    found = _EMPHASIS.match(line)
    if found is None:
        return line
    return f"{found['hash'] or ''}{found['text']}{found['colon']}"
