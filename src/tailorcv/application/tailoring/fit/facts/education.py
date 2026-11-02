"""Read the years of experience and the degree a posting requires."""

from __future__ import annotations

import re
from bisect import bisect_right

from tailorcv.application.tailoring.fit.lexicon import (
    DEADLINE_BEFORE,
    DEGREES,
    EQUIVALENT,
    EXAMPLE_CUE,
    MENTION_PREFERENCE_MARKERS,
    MUST_MARKERS,
    SPELLED_FIGURE,
    WORK_NEXT,
    YEARS,
    clause_breaks,
    preference_cues,
    years_figure,
)
from tailorcv.application.tailoring.fit.structure import (
    ASIDE,
    Answer,
    Block,
    RequirementLine,
    Stance,
    blanked,
    content_lines,
    first_saying,
    preference_asides,
    quote_line,
    unwaived,
    waived_spans,
)
from tailorcv.domain.taxonomy import says
from tailorcv.shared.text import sentence_pieces

_DEGREE_START = "|".join(
    re.escape(phrase).replace("'", r"['\u2019]?") for _, phrases in DEGREES for phrase in phrases
)
_FIGURE_START = (
    r"(?:at\s+least\s+|a\s+minimum\s+of\s+|minimum\s+(?:of\s+)?)?"
    rf"(?:\d|{SPELLED_FIGURE})"
)
# A capital OR after one of these words, in parentheses, or before a comma or a hyphenated
# word names something: "in the OR", "an OR nurse", "(OR)", "OR-Tools".
_NOUN_BEFORE = ("the", "a", "an", "in", "into", "with", "of", "for", "to", "at", "from", "on", "as")
_CAPITAL_OR = (
    "".join(rf"(?<!\b(?i:{word})\s)" for word in _NOUN_BEFORE) + r"(?<![\w/(])OR(?![\w/,)]|-\w)"
)
# What opens another path to one requirement: an "or" after a semicolon, before a dash, a
# figure or a degree, or a capital OR naming nothing; an "and" before "9+ years for a Lead".
# "CS, Engineering, or a related field" stays one path.
_ALTERNATIVE_PATH = re.compile(
    rf"{_CAPITAL_OR}|;\s*(?i:or)\b,?|\s(?i:or)\s*-\s"
    rf"|(?<=[^\d\s])\s+(?i:or)\s+(?=(?i:{_FIGURE_START}))"
    rf"|\s(?i:or)\s+(?=(?i:(?:an?\s+)?(?:{_DEGREE_START})))"
    rf"|(?<=[^\d\s])\s+(?i:and)\s+(?=(?i:{_FIGURE_START})[^,;.]{{0,30}}?\bfor\s+(?:an?|the)\s)"
)
# What attaches a years clause to the degree before it: "Master's degree with 5 years".
_ATTACHED_YEARS = re.compile(r"\b(?:with|and|plus)\b|,", re.IGNORECASE)
# Adjacent lines one "or" joins, at the end of one or the start of the next; "(OR)"
# and "OR-Tools" are names.
_TRAILING_OR = re.compile(r"(?<![\w/(])or[^\w)]*$", re.IGNORECASE)
_LEADING_OR = re.compile(r"^\W*or\b(?![-/]\w)", re.IGNORECASE)
_DIPLOMA = re.compile(r"\bhigh school\b|\bdiploma\b|\bGED\b", re.IGNORECASE)
# The last DEGREES entry names a degree of no level, which never counts as the lowest path.
_LEVELLESS = len(DEGREES) - 1
_DEGREE_PATTERNS = tuple(
    (
        index,
        re.compile(
            r"(?<![a-z0-9+])(?:"
            + "|".join("[^a-z0-9+]+".join(re.findall(r"[a-z0-9+]+", phrase)) for phrase in phrases)
            + r")(?![a-z0-9+])",
            re.IGNORECASE,
        ),
    )
    for index, (_, phrases) in enumerate(DEGREES)
)
_LEVEL_INDEX = {name: index for index, (name, _) in enumerate(DEGREES)}
# "Master's level ... degree" names the level without the word "degree" beside it.
_LEVEL_PHRASING = tuple(
    (
        _LEVEL_INDEX[f"{stem}'s degree"],
        re.compile(rf"\b{stem}(?:['\u2019]?s)?[- ]level\b", re.IGNORECASE),
    )
    for stem in ("bachelor", "master")
)
# Degrees written as abbreviations in a list right after the word "degree".
_LISTED_DEGREES = {
    **dict.fromkeys(("phd", "pharmd", "md", "dnp", "edd", "dvm", "psyd"), "doctorate"),
    **dict.fromkeys(("ms", "ma", "msc", "mba", "mph", "msn", "mpa"), "master's degree"),
    **dict.fromkeys(("bs", "ba", "bsc", "bsn"), "bachelor's degree"),
}
_DEGREE_LIST = re.compile(r"\bdegree\s*\((?P<items>[^()]*)\)", re.IGNORECASE)
_LIST_TOKEN = re.compile(r"[A-Za-z][A-Za-z.]*")
# A heading run into a flat line, which holds until the next one.
_INLINE_HEADING = re.compile(
    r"(?P<preferred>\b(?:preferred|desired)\s+(?:qualifications|skills|requirements|experience)\b"
    r"|\bnice[- ]to[- ]haves?\s*:|\bbonus\s+points\b)"
    r"|(?P<required>\b(?:minimum|basic|required)\s+(?:qualifications|requirements|skills)\b"
    r"|\brequirements\b|\brequired\s*:)",
    re.IGNORECASE,
)
_MUST = "|".join(re.escape(marker) for marker in MUST_MARKERS)
# A must marker, or "Minimum", directly introducing the degree after it.
_REQUIRING = re.compile(
    rf"(?<!not\s)\b(?:{_MUST}|minimum)\b\s*[:\-]?\s*(?:an?\s+)?$", re.IGNORECASE
)
_MUST_AFTER = re.compile(rf"(?<!not\s)\b(?:{_MUST})\b", re.IGNORECASE)
_EXAMPLE = re.compile(rf"(?:{EXAMPLE_CUE.pattern})[\s.,:]*(?:an?\s+)?$", re.IGNORECASE)
_LIST_GAP = re.compile(r"[\s,/]*(?:(?:or|and)\s+)?", re.IGNORECASE)


def read_years_required(
    requirements: tuple[RequirementLine, ...], blocks: tuple[Block, ...]
) -> Answer[int]:
    """Return the highest experience floor the posting states as required.

    The highest, because a posting asking for eight years overall and three in
    one technology is gated on eight. Every line a number came from is quoted.
    """
    # A bare licence item alone leaves the posting's years to be read from its prose.
    stated = any(line.stance is Stance.MUST_HAVE and not line.bare for line in requirements)
    source = requirements if stated else ()
    texts = tuple(line.whole for line in source) or content_lines(blocks)
    lines, joined, owners = _sentences(
        texts, tuple(line.or_next for line in source) or (False,) * len(texts)
    )
    stances = (
        tuple(_sentence_stance(source, owners, lines, index) for index in range(len(lines)))
        if stated
        else ()
    )
    # Ladders form over every requirement, so a preferred rung parts two must-have ones.
    found = [
        floor
        for ladder in _ladders(lines, joined, stances)
        if not stated or stances[ladder[0]] is Stance.MUST_HAVE
        if (floor := _ladder_floor(tuple(lines[index] for index in ladder), required=stated))
        is not None
    ]
    if not found:
        which = "no must-have line" if stated else "no line"
        return Answer(
            "Years of experience required",
            note=f"{which} states a number of years of experience",
        )
    return Answer(
        "Years of experience required",
        max(number for number, _ in found),
        evidence=tuple(dict.fromkeys(line for _, line in found)),
    )


def _sentence_stance(
    source: tuple[RequirementLine, ...], owners: tuple[int, ...], lines: tuple[str, ...], index: int
) -> Stance:
    """Return one sentence's stance: its line's, unless it alone wishes on a longer line.

    "Experience with Tableau. 3+ years in healthcare is a plus." requires no three years.
    """
    owner = owners[index]
    alone = owners.count(owner) == 1
    sentence = lines[index]
    if alone or not preference_cues(sentence) or says(unwaived(sentence), MUST_MARKERS):
        return source[owner].stance
    return Stance.PREFERRED


def _sentences(
    lines: tuple[str, ...], joins: tuple[bool, ...]
) -> tuple[tuple[str, ...], frozenset[int], tuple[int, ...]]:
    """Split lines into sentences, say which a bare "or" line follows, and which line each is from.

    Alternatives reach no further than their sentence, which a flattened body needs.
    """
    pieces: list[str] = []
    joined: set[int] = set()
    owners: list[int] = []
    for owner, (line, join) in enumerate(zip(lines, joins, strict=True)):
        split = sentence_pieces(line)
        pieces += split
        owners += [owner] * len(split)
        if join:
            joined.add(len(pieces) - 1)
    return tuple(pieces), frozenset(joined), tuple(owners)


def _ladders(
    lines: tuple[str, ...], joined: frozenset[int], stances: tuple[Stance, ...] = ()
) -> list[list[int]]:
    """Group adjacent lines an "or" joins, or that are rungs of one ladder, into one set.

    ``joined`` holds the lines a bare "or" line follows; a change of stance ends a ladder.
    """
    ladders: list[list[int]] = []
    for index, line in enumerate(lines):
        previous = lines[index - 1] if index else ""
        if (
            index
            and (not stances or stances[index] is stances[index - 1])
            and (
                index - 1 in joined
                or _TRAILING_OR.search(previous)
                or _LEADING_OR.match(line)
                or _next_rung(previous, line)
            )
        ):
            ladders[-1].append(index)
        else:
            ladders.append([index])
    return ladders


def _next_rung(previous: str, line: str) -> bool:
    """Return whether two lines each pair a different education level with a years figure."""
    levels = (_rung_level(previous), _rung_level(line))
    return None not in levels and levels[0] != levels[1]


def _rung_level(line: str) -> str | None:
    if YEARS.search(line) is None:
        return None
    named = next((level for level, phrases in DEGREES if says(line, phrases)), None)
    return named or ("diploma" if _DIPLOMA.search(line) else None)


def _ladder_floor(rungs: tuple[str, ...], *, required: bool) -> tuple[int, str] | None:
    """Return the lowest floor one ladder's paths state, and its quote.

    A path naming an education level and no years needs none, so its ladder sets no floor.
    On a required ladder that never says "experience", a figure counts when work follows it.
    """
    counted = any("experience" in rung.casefold() for rung in rungs)
    paths = [
        path for rung in rungs for path in _rung_paths(rung, counted=counted, required=required)
    ]
    floors = [(years_figure(figure), rung, figure) for figure, rung in paths if figure is not None]
    if not floors or len(floors) < len(paths):
        return None
    number, rung, figure = min(floors, key=lambda floor: floor[0])
    return number, quote_line(rung, figure.start())


def _path_spans(rung: str) -> list[tuple[int, int]]:
    """Return the span of each alternative path a rung offers, cut where an "or" opens one."""
    cuts = [found.start() for found in _ALTERNATIVE_PATH.finditer(rung)]
    return list(zip([0, *cuts], [*cuts, len(rung)], strict=True))


def _rung_paths(
    rung: str, *, counted: bool, required: bool
) -> list[tuple[re.Match[str] | None, str]]:
    """Return each path's highest figure, or None for a path naming a level and no figure."""
    spans = _path_spans(rung)
    cuts = [start for start, _ in spans[1:]]
    matches = _rung_figures(rung, counted=counted, required=required)
    # A figure belongs to the path it starts in, so "three or four years" stays one figure.
    owners = [bisect_right(cuts, found.start()) for found in matches]
    highest = [
        max(
            (found for found, owner in zip(matches, owners, strict=True) if owner == path),
            key=years_figure,
            default=None,
        )
        for path in range(len(spans))
    ]
    paths: list[tuple[re.Match[str] | None, str]] = []
    for path, (start, end) in enumerate(spans):
        if highest[path] is not None:
            paths.append((highest[path], rung))
        elif _names_level(rung[start:end]):
            later = highest[path + 1] if path + 1 < len(spans) else None
            shared = later is not None and _shares_years(
                rung, spans[path], spans[path + 1][0], later
            )
            paths.append((later if shared else None, rung))
    return paths


def _rung_figures(rung: str, *, counted: bool, required: bool) -> list[re.Match[str]]:
    """Return the years figures a rung asks for, leaving out wished and aside figures."""
    # "3 years (5+ years is preferred) required" wishes for the five and requires the three.
    asides = preference_asides(rung) if required else []
    return [
        found
        for found in YEARS.finditer(rung)
        if not (required and _wished(rung, found.start()))
        if not any(start <= found.start() < end for start, end in asides)
        if counted
        or (
            required
            and WORK_NEXT.match(rung, found.end())
            and not DEADLINE_BEFORE.search(rung, 0, found.start())
        )
    ]


def _wished(rung: str, start: int) -> bool:
    """Return whether the figure at ``start`` sits in a clause that only wishes for it.

    "At least 1 year of experience; 3+ years is preferred" requires one year.
    """
    ends = [end for _, end in clause_breaks(rung)]
    if not ends:
        return False
    clause = bisect_right(ends, start)
    opens = ends[clause - 1] if clause else 0
    closes = ends[clause] if clause < len(ends) else len(rung)
    return bool(preference_cues(rung[opens:closes]))


def _shares_years(rung: str, span: tuple[int, int], start: int, figure: re.Match[str]) -> bool:
    """Return whether a degree path's years clause, after its degree, also binds the path before.

    "Bachelor's degree or Master's degree with 5 years" shares the five; "Master's degree,
    or Bachelor's degree and 2 years" does not, since the higher degree stands in for years.
    """
    before, after = _level_rank(rung[span[0] : span[1]]), _level_rank(rung[start : figure.start()])
    return (
        before is not None
        and after is not None
        and before >= after
        and _ATTACHED_YEARS.search(rung, start, figure.start()) is not None
    )


def _level_rank(text: str) -> int | None:
    """Return how low the highest level a text names ranks: larger is lower, a diploma lowest.

    "Master's degree in physics" also mentions a levelless "degree in", which names no level.
    """
    named = [index for _, index, _, _ in _degree_mentions(text) if index != _LEVELLESS]
    if named:
        return min(named)
    return len(DEGREES) if _DIPLOMA.search(text) else None


def _names_level(text: str) -> bool:
    return any(says(text, phrases) for _, phrases in DEGREES) or bool(_DIPLOMA.search(text))


def read_education(
    requirements: tuple[RequirementLine, ...], blocks: tuple[Block, ...]
) -> Answer[str]:
    """Return the degree the posting asks for and how firmly, or that it asks for none.

    Required, preferred, and waivable by experience are separated rather than
    reported together as "degree". Alternative paths answer their lowest degree.
    """
    question = "Education required"
    joined = frozenset(index for index, line in enumerate(requirements) if line.or_next)
    wholes = tuple(line.whole for line in requirements)
    for ladder in _ladders(wholes, joined, tuple(line.stance for line in requirements)):
        named = [
            (degree, requirements[index])
            for index in ladder
            if (degree := _line_degree(requirements[index])) is not None
        ]
        if not named:
            continue
        (index, start, stance), line = max(named, key=lambda item: _lowness(item[0][0]))
        rungs = tuple(requirements[index].whole for index in ladder)
        wording = (
            "or equivalent experience"
            if says(line.whole, EQUIVALENT) or _needs_no_degree(rungs)
            else f"stated as {stance.value}"
        )
        return Answer(
            question, f"{DEGREES[index][0]}, {wording}", evidence=(quote_line(line.whole, start),)
        )
    for level, phrases in DEGREES:
        if found := first_saying(content_lines(blocks), phrases):
            return Answer(
                question,
                f"{level}, named outside any requirement section",
                evidence=(quote_line(found[1]),),
            )
    return Answer(
        question, note="no degree is named outside the posting's legal and benefits language"
    )


def _needs_no_degree(rungs: tuple[str, ...]) -> bool:
    """Return whether a ladder offers a path a diploma or an equivalent combination meets."""
    paths = [rung[start:end] for rung in rungs for start, end in _path_spans(rung)]
    return any(
        says(path, EQUIVALENT) or (_DIPLOMA.search(path) is not None and not _degree_mentions(path))
        for path in paths
    )


def _lowness(index: int) -> int:
    """Rank a DEGREES index so a larger value is a lower degree; a levelless degree ranks last."""
    return -1 if index == _LEVELLESS else index


def _line_degree(line: RequirementLine) -> tuple[int, int, Stance] | None:
    """Return the lowest degree path a line offers: its DEGREES index, position, and stance.

    A line never answers a must-have degree from a mention a preference qualifies.
    """
    cuts = [found.start() for found in _ALTERNATIVE_PATH.finditer(line.whole)]
    paths: dict[tuple[bool, int], list[tuple[int, int, bool]]] = {}
    for start, index, preferred, listed in _degree_mentions(line.whole):
        # Each degree a list offers is a path of its own.
        path = (True, start) if listed else (False, bisect_right(cuts, start))
        paths.setdefault(path, []).append((index, start, preferred))
    read = [_path_degree(mentions) for mentions in paths.values()]
    required = [found for found, preferred in read if not preferred]
    offered = [found for found, preferred in read if preferred]
    if required:
        index, start = max(required, key=lambda found: _lowness(found[0]))
        return index, start, _required_stance(line)
    if offered:
        index, start = max(offered, key=lambda found: _lowness(found[0]))
        return index, start, Stance.PREFERRED
    return None


def _path_degree(mentions: list[tuple[int, int, bool]]) -> tuple[tuple[int, int], bool]:
    """Return the highest degree one path names, and whether only a preference names it."""
    required = [(index, start) for index, start, preferred in mentions if not preferred]
    chosen = required or [(index, start) for index, start, _ in mentions]
    return min(chosen), not required


def _degree_mentions(text: str) -> list[tuple[int, int, bool, bool]]:
    """Return each degree a line names: position, index, preference, and listed alternative.

    A degree given as an example ("e.g., PhD") is left out. A parenthetical carrying a
    preference cue is its own clause, so the degree outside it is read without it.
    """
    asides = [
        found.span()
        for found in ASIDE.finditer(text)
        if preference_cues(found.group(), MENTION_PREFERENCE_MARKERS)
    ]
    lists = [
        found for found in _DEGREE_LIST.finditer(text) if not EXAMPLE_CUE.search(found["items"])
    ]
    outside = blanked(text, asides)
    waived = waived_spans(text)
    spans = sorted(
        (found.start(), found.end(), index)
        for index, pattern in (*_DEGREE_PATTERNS, *_LEVEL_PHRASING)
        for found in pattern.finditer(text)
        if not any(listed.start("items") <= found.start() < listed.end() for listed in lists)
    )
    clauses = [0, *(end for _, end in clause_breaks(outside)), len(text)]
    headings = [
        (found.start(), found.lastgroup == "preferred") for found in _INLINE_HEADING.finditer(text)
    ]
    found: list[tuple[int, int, bool, bool]] = []
    previous_end, previous_example = 0, False
    for start, end, index in spans:
        clause = bisect_right(clauses, start)
        opens, closes = clauses[clause - 1], clauses[clause]
        gap = outside[max(opens, previous_end) : start] if previous_end <= start else ""
        example = _is_example(gap, after_example=previous_example)
        if not example:
            following = min([closes, *(later for later, _, _ in spans if later >= end)])
            under = _under_preferred(headings, start)
            # A waiver of something else in the clause says nothing about this degree.
            others = blanked(outside, [(a, b) for a, b in waived if not a <= start < b])
            preferred = any(a <= start < b for a, b in asides) or _preferred(
                gap, outside[end:following], others[opens:closes], under=under
            )
            found.append((start, index, preferred, False))
        previous_end, previous_example = max(previous_end, end), example
    return found + _listed_mentions(outside, lists, clauses)


def _listed_mentions(
    text: str, lists: list[re.Match[str]], clauses: list[int]
) -> list[tuple[int, int, bool, bool]]:
    """Return the degrees a "degree (PharmD, PhD, or MS)" list offers as alternatives."""
    found: list[tuple[int, int, bool, bool]] = []
    for listed in lists:
        clause = bisect_right(clauses, listed.start())
        opens, closes = clauses[clause - 1], clauses[clause]
        before, after = text[opens : listed.start()], text[listed.end() : closes]
        preferred = _preferred(before, after, text[opens:closes], under=False)
        found += [
            (listed.start("items") + token.start(), _LEVEL_INDEX[level], preferred, True)
            for token in _LIST_TOKEN.finditer(listed["items"])
            if (level := _LISTED_DEGREES.get(token.group().replace(".", "").casefold()))
        ]
    return found


def _is_example(gap: str, *, after_example: bool) -> bool:
    """Return whether the text before a degree mention introduces it as an example."""
    return _EXAMPLE.search(gap) is not None or (
        after_example and _LIST_GAP.fullmatch(gap) is not None
    )


def _preferred(before: str, after: str, clause: str, *, under: bool) -> bool:
    """Return whether a preference, not a requirement, qualifies one degree mention."""
    if _REQUIRING.search(before) or _MUST_AFTER.search(after):
        return False
    return under or bool(preference_cues(clause, MENTION_PREFERENCE_MARKERS))


def _under_preferred(headings: list[tuple[int, bool]], start: int) -> bool:
    """Return whether the last inline heading before ``start`` names preferred items."""
    opened = [preferred for position, preferred in headings if position < start]
    return bool(opened) and opened[-1]


def _required_stance(line: RequirementLine) -> Stance:
    # A preference cue elsewhere on the line does not soften a degree no cue qualifies.
    if line.stance is Stance.MUST_HAVE or preference_cues(line.whole):
        return Stance.MUST_HAVE
    return line.stance
