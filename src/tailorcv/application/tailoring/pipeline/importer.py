"""Assemble a master CV and check generated and missing content.

Both guarantees are checked at the level of words. Authentic: every output field
repeats an unbroken run of the words of the lines it cited, in their order.
Lossless: every number and capitalized word reaches the converted document, in
the fields citing its line, or sits in a dropped line that is only page
furniture or a heading.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tailorcv.application.config import SectionNames
from tailorcv.application.master_cv.cv import (
    LIST_MARKER,
    CvInventory,
    EntryKind,
    kind_for_section,
    parse_cv,
)
from tailorcv.application.master_cv.sources import SourceDocument, SourceLine
from tailorcv.application.tailoring.stages.structure import (
    ImportedCv,
    ImportedRole,
    ImportedSection,
    Sourced,
)
from tailorcv.infrastructure.artifacts.files import write_private
from tailorcv.shared.costs import Ledger
from tailorcv.shared.errors import CvParseError
from tailorcv.shared.results import Finding, Outcome, table_cell

# Normalize hyphenated and spaced date ranges to the same tokens.
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'&./+]*")

IMPORTED = Path("the converted CV")

PAGE_NUMBER = re.compile(r"^(?:page\s+)?\d+(?:\s*(?:of|/)\s*\d+)?$", re.IGNORECASE)
# Page stamps, matched against a casefolded line.
PAGE_STAMP = re.compile(
    r"^(?:confidential|\(?continued(?: on next page)?\)?"
    r"|generated (?:by|with|using)(?: [a-z]+){1,3})$"
)
DOCUMENT_TITLE = frozenset({"curriculum", "vitae", "cv", "resume"})
# Words a section heading carries besides the one naming its section.
HEADING_MODIFIERS = frozenset(
    {
        *("about", "additional", "and", "core", "history", "key", "me", "of", "other"),
        *("personal", "professional", "relevant", "selected", "technical", "top", "work"),
        *("available", "on", "request", "upon"),
    }
)
# Lines the prompt asks to drop that no section kind names.
FRONT_MATTER_HEADINGS = ("contact", "employment", "language", "profile", "reference")
HEADING_MAX_WORDS = 4


@dataclass
class Assembled:
    """The converted document, its drops, and its expected bullet count."""

    markdown: str
    dropped: list[tuple[SourceLine, str]] = field(default_factory=list)
    bullets: int = 0


@dataclass
class ImportResult:
    """The converted CV, and the evidence that it is faithful."""

    markdown: str
    source: SourceDocument
    extracted: ImportedCv
    dropped: tuple[tuple[SourceLine, str], ...]
    findings: tuple[Finding, ...] = ()
    path: Path | None = None
    report: Path | None = None
    ledger: Ledger = field(default_factory=Ledger)

    @property
    def passed(self) -> bool:
        """Return whether every guarantee held."""
        return not [finding for finding in self.findings if finding.outcome is Outcome.FAIL]


# Apply the same normalization to both comparison sides.
TYPOGRAPHIC = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "…": "...",
        "ʼ": "'",
    }
)


def plain(text: str) -> str:
    """Return text folded towards ASCII, leaving a character with no ASCII form as written."""
    decomposed = unicodedata.normalize("NFD", text.translate(TYPOGRAPHIC))
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _token_sequence(text: str) -> list[str]:
    """Return the comparable words of a piece of text, in the order it writes them."""
    return [match.group(0).casefold().strip(".,;:-") for match in TOKEN.finditer(plain(text))]


def tokens(text: str) -> set[str]:
    """Return the comparable words of a piece of text."""
    return set(_token_sequence(text))


def content_tokens(text: str) -> set[str]:
    """Return only the words whose loss would matter.

    Numbers and capitalized words: employers, technologies, metrics, dates.
    """
    found = set()
    for match in TOKEN.finditer(plain(text)):
        word = match.group(0).strip(".,;:-")
        if word and (word[0].isupper() or any(character.isdigit() for character in word)):
            found.add(word.casefold())
    return found


def written(piece: Sourced) -> str:
    """Return one extracted field as it goes into the master CV, ASCII-folded here."""
    return plain(piece.text).strip()


def _prose_section(name: str, pieces: Sequence[Sourced]) -> list[str]:
    """Return a heading followed by one blank-separated line per piece."""
    if not pieces:
        return []
    lines = ["", f"## {name}"]
    for piece in pieces:
        lines += ["", written(piece)]
    return lines


def _skills_section(name: str, pieces: Sequence[Sourced]) -> list[str]:
    """Return the skills heading and its lines, labelling any line not already bold."""
    if not pieces:
        return []
    lines = ["", f"## {name}"]
    for piece in pieces:
        text = written(piece)
        lines += ["", text if text.startswith("**") else f"**Skills:** {text}"]
    return lines


def _extra_sections(sections: Sequence[ImportedSection]) -> list[str]:
    """Return the sections no named field holds, each under its source heading.

    The heading is written from the extracted text rather than from the
    configured section names, because these are the CV's own headings.
    """
    lines: list[str] = []
    for section in sections:
        lines += ["", f"## {written(section.heading)}"]
        for item in section.items:
            lines += ["", written(item)]
    return lines


def _role_header(role: ImportedRole) -> list[str]:
    """Return the title, title of record, and dated location lines a role carries."""
    header = []
    if role.title:
        header.append(f"**{written(role.title)}**  ")
    if role.title_of_record:
        header.append(f"(title of record: {written(role.title_of_record)})  ")
    if role.dates:
        location = f" | {written(role.location)}" if role.location else ""
        header.append(f"{written(role.dates)}{location}")
    return header


def _experience_section(name: str, roles: Sequence[ImportedRole]) -> list[str]:
    """Return the experience heading and one block per role, in the extracted order."""
    if not roles:
        return []
    lines = ["", f"## {name}"]
    for role in roles:
        lines += ["", f"### {written(role.organisation)}", ""]
        lines += _role_header(role)
        for piece in role.context:
            lines += ["", written(piece)]
        if role.bullets:
            lines.append("")
            lines += [f"- {written(piece)}" for piece in role.bullets]
    return lines


def assemble(extracted: ImportedCv, source: SourceDocument, sections: SectionNames) -> Assembled:
    """Build the Markdown from the extracted structure."""
    body = [f"# {written(extracted.name)}"]
    for line in extracted.contact:
        body += ["", written(line)]
    body += _prose_section(sections.summary, extracted.summary)
    body += _skills_section(sections.skills, extracted.skills)
    body += _experience_section(sections.experience, extracted.roles)
    body += _prose_section(sections.education, extracted.education)
    body += _prose_section(sections.publications, extracted.publications)
    body += _extra_sections(extracted.sections)

    dropped = [
        (original, item.reason)
        for item in extracted.dropped
        if (original := source.by_number(item.line)) is not None
    ]
    bullets = sum(len(role.bullets) for role in extracted.roles)
    markdown = "\n".join(body).strip() + "\n"
    return Assembled(markdown=markdown, dropped=dropped, bullets=bullets)


def verify(source: SourceDocument, extracted: ImportedCv, built: Assembled) -> tuple[Finding, ...]:
    """Check both guarantees against the words, not against intentions."""
    return (
        _authentic(source, extracted),
        _preserved(source, extracted),
        _aligned(extracted),
        _lossless(source, extracted, built),
        _characters(source, built),
        _compliant(built.markdown, built.bullets),
    )


def _preserved(source: SourceDocument, extracted: ImportedCv) -> Finding:
    """Require professional prose to retain its cited words in source order."""
    pieces = [*extracted.summary]
    for role in extracted.roles:
        pieces.extend((*role.context, *role.bullets))
    for piece in pieces:
        cited = [source.by_number(number) for number in piece.lines]
        original = " ".join(line.text for line in cited if line is not None)
        before = [word.casefold().strip(".,;:-") for word in TOKEN.findall(plain(original))]
        after = [word.casefold().strip(".,;:-") for word in TOKEN.findall(plain(piece.text))]
        if before != after or tuple(sorted(set(piece.lines))) != piece.lines:
            return Finding(
                "preserved",
                Outcome.FAIL,
                f"Professional text changed at source lines {piece.lines}; "
                "retain the complete source wording before importing.",
                redraftable=False,
            )
    return Finding("preserved", Outcome.PASS, "professional prose retains cited words in order")


def _aligned(extracted: ImportedCv) -> Finding:
    """Refuse reused achievements and citations crossing another role's anchor."""
    used: set[int] = set()
    anchors = sorted(
        {min(role.organisation.lines) for role in extracted.roles if role.organisation.lines}
    )
    for role in extracted.roles:
        start = min(role.organisation.lines, default=0)
        stop = next((anchor for anchor in anchors if anchor > start), float("inf"))
        for piece in (*role.context, *role.bullets):
            if any(line in used or not start < line < stop for line in piece.lines):
                return Finding(
                    "source alignment",
                    Outcome.FAIL,
                    "An achievement reuses source lines or crosses its role boundary; "
                    "review the source layout.",
                    redraftable=False,
                )
            used.update(piece.lines)
    return Finding(
        "source alignment",
        Outcome.PASS,
        "achievements retain distinct source lines within their roles",
    )


def _characters(source: SourceDocument, built: Assembled) -> Finding:
    """Say what the fold changed, and name anything it could not reach.

    A warning rather than a failure in both directions: folding is the intended
    outcome, and a character with no ASCII form is the CV's problem, where
    refusing to write the file would leave nothing to edit.
    """
    original = "\n".join(line.text for line in source.lines)
    folded = sorted({character for character in original if plain(character) != character})
    remaining = sorted({character for character in built.markdown if ord(character) > 127})
    if remaining:
        return Finding(
            "characters",
            Outcome.WARN,
            f"{_named(remaining)} has no ASCII form and was kept as it is. "
            "Every tailoring run will report it until you change it in the CV.",
        )
    if folded:
        return Finding(
            "characters",
            Outcome.PASS,
            f"folded to ASCII: {_named(folded)}",
        )
    return Finding("characters", Outcome.PASS, "nothing needed folding")


def _named(characters: list[str]) -> str:
    """Name each character, because a glyph alone is not searchable."""
    return ", ".join(
        f"{character!r} ({unicodedata.name(character, 'unnamed')})" for character in characters[:6]
    )


def _authentic(source: SourceDocument, extracted: ImportedCv) -> Finding:
    """Every field must repeat an unbroken run of the words of the lines it cited.

    A subset would accept a reordered publication citation and a title with a
    word taken out of the middle, whose words are all present and all cited.
    """
    invented, rearranged, uncited = _citation_faults(source, extracted)
    if invented:
        word, where = invented[0]
        return Finding(
            "authentic",
            Outcome.FAIL,
            f"{len(invented)} word(s) are in no cited source line, first {word!r} in {where!r}",
            redraftable=False,
        )
    if uncited:
        return Finding(
            "authentic",
            Outcome.FAIL,
            f"{uncited} field(s) cite no source line at all",
            redraftable=False,
        )
    if rearranged:
        return Finding(
            "authentic",
            Outcome.FAIL,
            f"{len(rearranged)} field(s) reorder or drop words of their cited line(s), "
            f"first {rearranged[0]!r}",
            redraftable=False,
        )
    return Finding("authentic", Outcome.PASS, "every output field copies a run of its cited lines")


def _cited_lines(source: SourceDocument, piece: Sourced) -> list[SourceLine]:
    """Return the source lines a field cites in document order, dropping unknown numbers."""
    # The order a field lists its citations in is the model's; only the words within
    # each line are the document's, and those are what the run is checked against.
    found = [line for number in piece.lines if (line := source.by_number(number)) is not None]
    return sorted(found, key=lambda line: line.number)


def _citation_faults(
    source: SourceDocument, extracted: ImportedCv
) -> tuple[list[tuple[str, str]], list[str], int]:
    """Return the invented words, the fields that reorder their lines, and the uncited count."""
    invented: list[tuple[str, str]] = []
    rearranged: list[str] = []
    uncited = 0
    for piece in extracted.sourced():
        if not piece.lines:
            uncited += 1
            continue
        cited = _cited_lines(source, piece)
        available: set[str] = set()
        for line in cited:
            available |= tokens(line.text)
        missing = [(word, piece.text[:60]) for word in tokens(piece.text) - available]
        invented += missing
        original = " ".join(line.text for line in cited)
        if not missing and not _runs_through(piece.text, original):
            rearranged.append(piece.text[:60])
    return invented, rearranged, uncited


def _runs_through(text: str, original: str) -> bool:
    """Return whether a field's words sit unbroken and in order inside its cited lines."""
    wanted = _token_sequence(text)
    if not wanted:
        return True
    found = _token_sequence(original)
    width = len(wanted)
    return any(found[at : at + width] == wanted for at in range(len(found) - width + 1))


def _lossless(source: SourceDocument, extracted: ImportedCv, built: Assembled) -> Finding:
    """Every tracked content token must survive where it was cited, or be dropped harmlessly.

    A cited line's words must appear in the fields citing it, so a shortened
    title fails even when its missing word appears elsewhere in the CV.
    """
    refused = _content_drops(source, extracted, built)
    if refused:
        shown = "; ".join(repr(line.text[:60]) for line in refused[:3])
        return Finding(
            "lossless",
            Outcome.FAIL,
            f"{len(refused)} dropped line(s) carry content that has no field and is not "
            f"page furniture or a heading: {shown}",
            redraftable=False,
        )
    missing = _missing_words(source, extracted, built)
    if missing:
        shown = ", ".join(sorted(missing)[:8])
        return Finding(
            "lossless",
            Outcome.FAIL,
            f"{len(missing)} word(s) from your CV reach neither the fields taken from their "
            f"lines in the converted document nor the dropped list: {shown}",
            redraftable=False,
        )
    if built.dropped:
        return Finding(
            "lossless",
            Outcome.WARN,
            f"{len(built.dropped)} line(s) were dropped on purpose; the import report lists them",
        )
    return Finding("lossless", Outcome.PASS, "every tracked content word of your CV survived")


@dataclass(frozen=True)
class _Page:
    """What decides whether a dropped line is page furniture."""

    repeats: Counter[str]
    page_numbers: frozenset[int]
    last: int
    name: str
    placed: frozenset[int]

    def at_boundary(self, number: int) -> bool:
        """Return whether a line opens or closes the document or a numbered page."""
        return number in {1, self.last} or bool({number - 1, number + 1} & self.page_numbers)

    def outside_placed(self, number: int) -> bool:
        """Return whether a line sits above or below every line placed in a field."""
        return not self.placed or not min(self.placed) < number < max(self.placed)


def _content_drops(
    source: SourceDocument, extracted: ImportedCv, built: Assembled
) -> list[SourceLine]:
    """Return the dropped lines that carry content rather than page furniture."""
    page = _Page(
        repeats=Counter(_folded(line.text) for line in source.lines),
        page_numbers=frozenset(
            line.number for line in source.lines if PAGE_NUMBER.match(_bare(line.text))
        ),
        last=max((line.number for line in source.lines), default=0),
        name=_folded(extracted.name.text),
        placed=frozenset(number for piece in extracted.sourced() for number in piece.lines),
    )
    return [
        line
        for line, _ in built.dropped
        if content_tokens(line.text) and not _furniture(line, page)
    ]


def _missing_words(source: SourceDocument, extracted: ImportedCv, built: Assembled) -> set[str]:
    """Return content words of kept lines that do not reach the assembled document.

    A word that reaches a field but not the document is lost too, so a field
    extracted correctly and then left out of the assembly is caught here.
    """
    dropped_numbers = {line.number for line, _ in built.dropped}
    citing: dict[int, set[str]] = {}
    for piece in extracted.sourced():
        for number in piece.lines:
            citing.setdefault(number, set()).update(tokens(piece.text))
    document = tokens(built.markdown)
    missing: set[str] = set()
    for line in source.lines:
        if line.number in dropped_numbers:
            continue
        words = content_tokens(LIST_MARKER.sub("", line.text))
        survived = citing[line.number] & document if line.number in citing else document
        missing |= words - survived
    return missing


def _folded(text: str) -> str:
    """Return a line reduced to what decides whether two lines repeat each other."""
    return " ".join(plain(text).casefold().split())


def _bare(text: str) -> str:
    """Return a line without the punctuation that frames page furniture."""
    return plain(text).strip(" -|.")


def _furniture(line: SourceLine, page: _Page) -> bool:
    """Return whether a dropped line is page furniture or a heading.

    A heading that names no known section counts only when the line after it is
    placed in a field, so a dropped item cannot pass as a heading.
    """
    bare = _bare(line.text)
    folded = _folded(bare)
    words = re.findall(r"[a-z]+", folded)
    digits = any(character.isdigit() for character in bare)
    short = 0 < len(words) <= HEADING_MAX_WORDS and not digits and not LIST_MARKER.match(line.text)
    if _stamp_or_title(bare, folded, words, digits, page.name):
        return True
    repeated = page.repeats[_folded(line.text)] > 1
    if page.name and page.name in folded:
        return repeated or page.outside_placed(line.number)
    if repeated and (short or page.at_boundary(line.number)):
        return True
    if not short:
        return False
    return _known_heading(words) or line.number + 1 in page.placed


def _stamp_or_title(bare: str, folded: str, words: list[str], digits: bool, name: str) -> bool:
    """Return whether a line is a page number, a page stamp, or the document's title."""
    if PAGE_NUMBER.match(bare) or PAGE_STAMP.match(folded):
        return True
    title = set(words) - set(re.findall(r"[a-z]+", name))
    return not digits and bool(title) and title <= DOCUMENT_TITLE


def _known_heading(words: list[str]) -> bool:
    """Return whether a short line names only known sections and heading modifiers."""
    named = [word for word in words if word not in HEADING_MODIFIERS]
    return bool(named) and all(
        kind_for_section(word) is not EntryKind.OTHER or word.startswith(FRONT_MATTER_HEADINGS)
        for word in named
    )


def _compliant(markdown: str, expected_bullets: int = 0) -> Finding:
    """The result has to parse, and parse into what was actually extracted.

    A role header run into its bullets parses perfectly well, as one paragraph,
    silently turning several achievements into a blob. Counting what comes back
    out is what catches that.
    """
    try:
        inventory = parse_cv(IMPORTED, markdown)
    except CvParseError as error:
        return Finding("compliant", Outcome.FAIL, str(error).splitlines()[0], redraftable=False)

    bullets = len(inventory.of_kind(EntryKind.BULLET))
    if bullets < expected_bullets:
        return Finding(
            "compliant",
            Outcome.FAIL,
            f"{expected_bullets} bullets were extracted but only {bullets} survived the parse; "
            "they have been run together",
            redraftable=False,
        )
    return Finding("compliant", Outcome.PASS, _describe(inventory))


def _describe(inventory: CvInventory) -> str:
    """Return what the parse produced."""
    return (
        f"parses as a master CV: {len(inventory.entries)} entries, "
        f"{len(inventory.roles)} roles, {len(inventory.of_kind(EntryKind.BULLET))} bullets"
    )


def write_report(directory: Path, result: ImportResult) -> Path:
    """Write the record of what was converted and what was left behind."""
    lines = [
        f"# Import report: {result.source.origin}",
        "",
        f"- Read as: {result.source.method}",
        f"- Source lines: {len(result.source.lines)}",
        f"- Roles found: {len(result.extracted.roles)}",
        f"- Dropped: {len(result.dropped)}",
        "",
        "## Checks",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {finding.check} | {finding.outcome.value} | {finding.detail} |"
        for finding in result.findings
    ]
    lines += [
        "",
        "## Dropped lines",
        "",
        "Read these. Anything here is in your original and is not in the result.",
        "",
    ]
    if result.dropped:
        lines += ["| Line | Text | Why |", "| --- | --- | --- |"]
        lines += [
            f"| {line.number} | {line.text[:120]} | {reason} |" for line, reason in result.dropped
        ]
    else:
        lines.append("Nothing was dropped.")

    lines += [
        "",
        "## Where each line came from",
        "",
        "Every field of the result, and the line numbers of your original it was",
        "taken from. Spot-check a few before you trust the rest.",
        "",
        "| Result | From lines |",
        "| --- | --- |",
    ]
    lines += [
        f"| {_cell(piece.text)} | {', '.join(str(number) for number in piece.lines) or '-'} |"
        for piece in result.extracted.sourced()
    ]
    return write_private(directory / "Import_Report.md", "\n".join(lines).rstrip() + "\n")


def _cell(text: str) -> str:
    """Flatten one import-report cell, which has less room than a run report's."""
    return table_cell(text, 90)


__all__ = ["Assembled", "ImportResult", "assemble", "verify", "write_report"]
