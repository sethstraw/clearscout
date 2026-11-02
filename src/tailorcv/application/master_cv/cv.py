"""Parse the master CV into an inventory of individually addressable evidence.

Every claim the tool may make is an entry produced here, each with a stable
identifier, so no downstream stage has a vocabulary for evidence that does not
exist. Identifiers derive from content, so an unedited bullet keeps its
identifier across CV revisions while an edited one gets a new one.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tailorcv.shared.errors import CvParseError

H1 = re.compile(r"^#\s+(?P<text>\S.*?)\s*$")
H2 = re.compile(r"^##\s+(?P<text>\S.*?)\s*$")
H3 = re.compile(r"^###\s+(?P<text>\S.*?)\s*$")
BULLET = re.compile(r"^(?P<indent>\s*)[-*+\u2022]\s+(?P<text>\S.*?)\s*$")
LIST_MARKER = re.compile(r"^\s*(?:[-*+\u2022]|\d{1,3}[.)])\s+")
BOLD_ONLY = re.compile(r"^\*\*(?P<text>[^*]+?)\*\*\s*$")
LABELED = re.compile(r"^\*\*(?P<label>[^*:]+):\*\*\s*(?P<text>\S.*?)\s*$")
TITLE_OF_RECORD = re.compile(r"^\(title of record:\s*(?P<text>.+?)\)\s*$", re.IGNORECASE)
THEMATIC_BREAK = re.compile(r"^-{3,}\s*$")
YEAR = re.compile(r"\b(19|20)\d{2}\b")
# "2019-21": a two-digit end year is the next one with those digits.
SHORT_END_YEAR = re.compile(r"\b(?P<start>(?:19|20)\d{2})\s*[-\u2013]\s*(?P<end>\d{2})(?![/.]?\d)")
EMPHASIS = re.compile(r"[*_`]+")
PRESENT = re.compile(r"\bpresent\b", re.IGNORECASE)

# Match section headings by casefolded substring rather than a fixed allowlist.
_SECTION_KINDS: tuple[tuple[str, EntryKind], ...]


class EntryKind(StrEnum):
    """What sort of evidence an entry is.

    Kind drives three things: the identifier prefix, which section of the
    derivative an entry may be used in, and how the selection stage weighs it.
    """

    SUMMARY = "summary"
    SKILL = "skill"
    ROLE_CONTEXT = "role_context"
    BULLET = "bullet"
    EDUCATION = "education"
    CREDENTIAL = "credential"
    APPOINTMENT = "appointment"
    PUBLICATION = "publication"
    PRESENTATION = "presentation"
    WRITING = "writing"
    MEMBERSHIP = "membership"
    AWARD = "award"
    OTHER = "other"


_SECTION_KINDS = (
    ("summary", EntryKind.SUMMARY),
    ("objective", EntryKind.SUMMARY),
    ("skill", EntryKind.SKILL),
    ("experience", EntryKind.BULLET),
    ("education", EntryKind.EDUCATION),
    ("coursework", EntryKind.EDUCATION),
    ("licens", EntryKind.CREDENTIAL),
    ("licence", EntryKind.CREDENTIAL),
    ("certification", EntryKind.CREDENTIAL),
    ("training", EntryKind.CREDENTIAL),
    ("appointment", EntryKind.APPOINTMENT),
    ("fellowship", EntryKind.APPOINTMENT),
    ("publication", EntryKind.PUBLICATION),
    ("patent", EntryKind.PUBLICATION),
    ("presentation", EntryKind.PRESENTATION),
    ("poster", EntryKind.PRESENTATION),
    ("writing", EntryKind.WRITING),
    ("organization", EntryKind.MEMBERSHIP),
    ("organisation", EntryKind.MEMBERSHIP),
    ("membership", EntryKind.MEMBERSHIP),
    ("affiliation", EntryKind.MEMBERSHIP),
    ("service", EntryKind.MEMBERSHIP),
    ("volunteer", EntryKind.MEMBERSHIP),
    ("honor", EntryKind.AWARD),
    ("honour", EntryKind.AWARD),
    ("award", EntryKind.AWARD),
    ("grant", EntryKind.AWARD),
    ("funding", EntryKind.AWARD),
)

# Each fragment has to start a word, or "grant" would claim a migrant-health
# heading and "funding" a refunding one.
_SECTION_PATTERNS = tuple(
    (re.compile(rf"\b{re.escape(fragment)}"), kind) for fragment, kind in _SECTION_KINDS
)

_ID_PREFIXES = {
    EntryKind.SUMMARY: "sum",
    EntryKind.SKILL: "skl",
    EntryKind.ROLE_CONTEXT: "ctx",
    EntryKind.BULLET: "b",
    EntryKind.EDUCATION: "edu",
    EntryKind.CREDENTIAL: "cred",
    EntryKind.APPOINTMENT: "apt",
    EntryKind.PUBLICATION: "pub",
    EntryKind.PRESENTATION: "pres",
    EntryKind.WRITING: "wri",
    EntryKind.MEMBERSHIP: "org",
    EntryKind.AWARD: "awd",
    EntryKind.OTHER: "oth",
}


class Role(BaseModel):
    """One position under an experience section.

    ``key`` is built from the organization and title rather than from position
    in the file, so reordering the CV does not renumber everything.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    organization: str
    title: str | None = None
    title_of_record: str | None = None
    dates: str | None = None
    location: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    current: bool = False
    order: int = 0

    def years_since_end(self, today: date | None = None) -> int:
        """Return how many years ago this role ended, 0 while current, for selection's recency."""
        if self.current or self.end_year is None:
            return 0
        return max(0, (today or date.today()).year - self.end_year)


class Entry(BaseModel):
    """One citable piece of evidence from the master CV.

    ``details`` holds indented sub-bullets, which on publications carry the
    structured citation fields. They travel with the parent because they are
    never independently useful.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: EntryKind
    section: str
    text: str
    role_key: str | None = None
    group: str | None = None
    label: str | None = None
    details: tuple[str, ...] = ()
    order: int = 0

    @property
    def scope(self) -> str:
        """Return the role or section this entry sits under."""
        return self.role_key or self.section

    @property
    def full_text(self) -> str:
        """Return the entry with its details, as a model should see it."""
        if not self.details:
            return self.text
        return "\n".join([self.text, *(f"  - {detail}" for detail in self.details)])


class CvInventory(BaseModel):
    """The parsed master CV: front matter, roles, and every citable entry."""

    model_config = ConfigDict(frozen=True)

    source: Path
    content_hash: str
    name: str
    contact_lines: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    roles: tuple[Role, ...] = ()
    entries: tuple[Entry, ...] = ()

    def by_id(self, entry_id: str) -> Entry:
        """Return the entry with ``entry_id``, raising if it does not exist."""
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        raise KeyError(entry_id)

    def entry(self, entry_id: str) -> Entry | None:
        """Return the entry with ``entry_id``, or None for an id the record lacks."""
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def ids(self) -> tuple[str, ...]:
        """Return every valid identifier, which is the model's entire vocabulary."""
        return tuple(entry.id for entry in self.entries)

    def of_kind(self, *kinds: EntryKind) -> tuple[Entry, ...]:
        """Return entries of the given kinds, in document order."""
        wanted = set(kinds)
        return tuple(entry for entry in self.entries if entry.kind in wanted)

    def role(self, key: str) -> Role | None:
        """Return the role with ``key``, or ``None`` when there is no such role."""
        return next((role for role in self.roles if role.key == key), None)

    def entries_for(self, role_key: str) -> tuple[Entry, ...]:
        """Return every entry belonging to one role, in document order."""
        return tuple(entry for entry in self.entries if entry.role_key == role_key)


def normalize(text: str) -> str:
    """Reduce text to its identity form, ignoring emphasis, spacing, case, and edge punctuation."""
    stripped = EMPHASIS.sub("", text)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed.strip(" .,;:-").casefold()


def content_hash(text: str) -> str:
    """Return a short stable digest of the CV text."""
    return hashlib.blake2s(text.encode("utf-8"), digest_size=8).hexdigest()


def _entry_id(kind: EntryKind, text: str, scope: str, taken: set[str]) -> str:
    """Derive a content-addressed identifier, disambiguating exact repeats.

    The scope is the role or section the entry sits under, so the same sentence
    appearing under two employers gets two identifiers. A genuine duplicate
    inside one scope takes a numeric suffix in document order, which stays
    stable as long as the duplication does.
    """
    digest = hashlib.blake2s(
        f"{normalize(scope)}|{normalize(text)}".encode(), digest_size=4
    ).hexdigest()
    base = f"{_ID_PREFIXES[kind]}-{digest}"
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    return f"{base}-{suffix}"


def kind_for_section(section: str) -> EntryKind:
    """Map a section heading to the kind of evidence it contains."""
    folded = section.casefold()
    for pattern, kind in _SECTION_PATTERNS:
        if pattern.search(folded):
            return kind
    return EntryKind.OTHER


def _role_key(organization: str, title: str | None) -> str:
    """Build a readable, stable handle for a role."""
    parts = [organization] if title is None else [organization, title]
    slug = re.sub(r"[^a-z0-9]+", "-", " ".join(parts).casefold()).strip("-")
    return slug[:64]


def _unique_role_key(organization: str, taken: set[str]) -> str:
    """Return the key one role heading gets, given the keys already assigned.

    Shared by the parser and by :func:`spans` so that a document read twice is
    read the same way.
    """
    key = _role_key(organization, None)
    if key not in taken:
        return key
    suffix = 2
    while f"{key}-{suffix}" in taken:
        suffix += 1
    return f"{key}-{suffix}"


class Span(BaseModel):
    """Where one section or role sits in the source, as a half-open line range.

    The parser throws line numbers away. Editing the master CV needs to know
    which lines belong to the role a new bullet is added under, so this walks
    the same headings and records the ranges instead.
    """

    model_config = ConfigDict(frozen=True)

    heading: str
    start: int
    end: int


def spans(text: str) -> tuple[dict[str, Span], dict[str, Span]]:
    """Return the line ranges of every section and every role, in that order.

    A section runs from its ``##`` heading to the next ``##`` or the end of the
    document, and a role from its ``###`` heading to the next heading of either
    level. Both ranges start at the heading line, so an insertion point derived
    from one is always inside the block it belongs to.
    """
    lines = text.splitlines()
    opened: list[tuple[int, str, str, int]] = []
    role_keys: set[str] = set()
    for index, raw in enumerate(lines):
        line = raw.rstrip()
        if match := H2.match(line):
            heading = match.group("text")
            opened.append((2, heading, heading, index))
            continue
        if match := H3.match(line):
            heading = match.group("text")
            key = _unique_role_key(heading, role_keys)
            role_keys.add(key)
            opened.append((3, key, heading, index))

    sections: dict[str, Span] = {}
    roles: dict[str, Span] = {}
    for position, (level, key, heading, start) in enumerate(opened):
        # A heading at either level closes the current role.
        end = next(
            (
                later_start
                for later_level, _, _, later_start in opened[position + 1 :]
                if level == 3 or later_level == 2
            ),
            len(lines),
        )
        target = sections if level == 2 else roles
        target[key] = Span(heading=heading, start=start, end=end)
    return sections, roles


# A role date field contains a date span, not arbitrary prose.
MAX_DATE_WORDS = 12
SENTENCE_END = (".", "!", "?", ";", ":")


def _looks_like_header(block: list[str]) -> bool:
    """Return whether a block is role metadata; a lone bold line is a sub-heading, not a title."""
    if any(_is_date_line(line) or TITLE_OF_RECORD.match(line) for line in block):
        return True
    return len(block) > 1 and BOLD_ONLY.match(block[0]) is not None


def _is_date_line(line: str) -> bool:
    """Return whether a role header line is the date field.

    A pipe is conclusive: the contract puts the date in its own pipe-delimited
    field. Without one the line is judged alone, because plenty of people write
    the dates with no location beside them, and requiring the pipe would leave
    the raw date string in the evidence pool for a model to rewrite as prose.
    """
    if not YEAR.search(line):
        return False
    before_pipe, pipe, _ = line.partition("|")
    if pipe:
        return bool(YEAR.search(before_pipe))
    stripped = before_pipe.strip()
    return len(stripped.split()) <= MAX_DATE_WORDS and not stripped.endswith(SENTENCE_END)


def _header_absorbed(role: Role, block: list[str]) -> tuple[Role, int]:
    """Return the role carrying the block's header fields, and the lines it spent.

    Absorption stops at the first line that is neither a title, a title of
    record, nor the date field, so the rest of the block stays role prose.
    """
    title = role.title
    title_of_record = role.title_of_record
    dates = role.dates
    location = role.location
    consumed = 0
    for line in block:
        if match := TITLE_OF_RECORD.match(line):
            title_of_record = match.group("text")
        elif (match := BOLD_ONLY.match(line)) and title is None:
            title = match.group("text")
        elif dates is None and _is_date_line(line):
            left, _, right = line.partition("|")
            dates = left.strip()
            location = right.strip() or None
        else:
            break
        consumed += 1
    start_year, end_year, current = _parse_dates(dates or "")
    updated = role.model_copy(
        update={
            "title": title,
            "title_of_record": title_of_record,
            "dates": dates,
            "location": location,
            "start_year": start_year,
            "end_year": end_year,
            "current": current,
        }
    )
    return updated, consumed


def _parse_dates(line: str) -> tuple[int | None, int | None, bool]:
    """Extract (start year, end year, currently held) from a role's date field."""
    years = [int(match.group(0)) for match in YEAR.finditer(line)]
    for match in SHORT_END_YEAR.finditer(line):
        start = int(match.group("start"))
        short = int(match.group("end"))
        end = start - start % 100 + short
        end = end if end >= start else end + 100
        # Two digits that can be a month are one, as in 2019-05 or 1998-02.
        if not 1 <= short <= 12 and end <= date.today().year:
            years.append(end)
    current = bool(PRESENT.search(line))
    if not years:
        return None, None, current
    return min(years), (None if current else max(years)), current


def parse_cv(path: Path, text: str | None = None) -> CvInventory:
    """Parse a master CV in conventional resume Markdown into a citable inventory.

    Passing ``text`` explicitly keeps the parser testable without touching the
    filesystem; in normal use the file at ``path`` is read.
    """
    try:
        source = text if text is not None else path.read_text(encoding="utf-8")
    except OSError as error:
        raise CvParseError(f"Cannot read master CV {path}: {error}") from error
    return _Parser(path, source).run()


class _Parser:
    """A single-pass, line-oriented reader for conventional resume Markdown.

    A class rather than a pile of functions because the parse is stateful: the
    meaning of a line depends on the section, role, and sub-block it sits in.
    """

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.lines = source.splitlines()
        self.name: str | None = None
        self.contact: list[str] = []
        self.sections: list[str] = []
        self.roles: list[Role] = []
        self.entries: list[Entry] = []
        self.taken: set[str] = set()
        self.section: str | None = None
        self.role: Role | None = None
        self.group: str | None = None
        self.order = 0

    def run(self) -> CvInventory:
        """Parse the document and return the finished inventory."""
        blocks = self._blocks()
        for block in blocks:
            self._consume(block)
        if self.name is None:
            raise CvParseError(
                f"{self.path} has no H1 name line. The document must start with '# Candidate Name'."
            )
        if not self.entries:
            raise CvParseError(f"{self.path} produced no citable entries; is it the right file?")
        return CvInventory(
            source=self.path,
            content_hash=content_hash(self.source),
            name=self.name,
            contact_lines=tuple(self.contact),
            sections=tuple(self.sections),
            roles=tuple(self.roles),
            entries=tuple(self.entries),
        )

    def _blocks(self) -> list[list[str]]:
        """Split the document into blank-line-separated blocks, dropping rules."""
        blocks: list[list[str]] = []
        current: list[str] = []
        for raw in self.lines:
            line = raw.rstrip()
            if not line.strip() or THEMATIC_BREAK.match(line):
                if current:
                    blocks.append(current)
                    current = []
                continue
            if H1.match(line) or H2.match(line) or H3.match(line):
                if current:
                    blocks.append(current)
                blocks.append([line])
                current = []
                continue
            current.append(line)
        if current:
            blocks.append(current)
        return blocks

    def _consume(self, block: list[str]) -> None:
        """Route one block to the handler its shape and position select."""
        first = block[0]
        if match := H1.match(first):
            self.name = match.group("text")
            return
        if match := H2.match(first):
            self.section = match.group("text")
            self.role = None
            self.group = None
            self.sections.append(self.section)
            return
        if match := H3.match(first):
            self._start_role(match.group("text"))
            return
        if self.section is None:
            self.contact.extend(block)
            return
        if self.role is not None and self._absorb_role_header(block):
            return
        if BULLET.match(first):
            self._add_bullets(block)
            return
        if len(block) == 1 and (match := BOLD_ONLY.match(first)):
            self.group = match.group("text")
            return
        self._add_paragraph(block)

    def _start_role(self, organization: str) -> None:
        """Open a new role; its header fields arrive in the following blocks."""
        key = _unique_role_key(organization, {role.key for role in self.roles})
        self.role = Role(key=key, organization=organization, order=len(self.roles))
        self.roles.append(self.role)
        self.group = None

    def _absorb_role_header(self, block: list[str]) -> bool:
        """Consume a role's title, title of record, and date line if this is it.

        Returns ``True`` when the whole block was header metadata. A block is
        header metadata only while the role has collected no prose, which is
        what stops a later bold line from being mistaken for a title.
        """
        if self.role is None:  # pragma: no cover - guarded by the caller
            return False
        if any(entry.role_key == self.role.key for entry in self.entries):
            return False
        if not _looks_like_header(block):
            return False
        updated, consumed = _header_absorbed(self.role, block)
        if consumed == 0:
            return False
        self.roles[self.roles.index(self.role)] = updated
        self.role = updated
        if consumed < len(block):
            self._add_paragraph(block[consumed:])
        return True

    def _add_bullets(self, block: list[str]) -> None:
        """Add each top-level bullet as an entry; indented bullets become details."""
        pending: list[str] = []
        details: list[str] = []
        for line in block:
            match = BULLET.match(line)
            if match is None:
                if pending:
                    pending[-1] = f"{pending[-1]} {line.strip()}"
                continue
            if match.group("indent"):
                details.append(match.group("text"))
                continue
            if pending:
                self._emit(pending[-1], details)
                details = []
            pending.append(match.group("text"))
        if pending:
            self._emit(pending[-1], details)

    def _add_paragraph(self, block: list[str]) -> None:
        """Add a paragraph as one entry, keeping a bold label when it has one."""
        joined = " ".join(line.strip() for line in block)
        label = None
        if match := LABELED.match(joined):
            label, joined = match.group("label"), match.group("text")
        self._emit(joined, [], label=label, prose=True)

    def _emit(
        self,
        text: str,
        details: list[str],
        label: str | None = None,
        prose: bool = False,
    ) -> None:
        """Create one entry with a content-addressed identifier.

        ``prose`` separates the two shapes an experience section produces: a
        bullet competes for space on its own merit, and a paragraph is the scope
        a role needs to be legible, which selection treats differently.
        """
        if self.section is None:  # pragma: no cover - guarded by the caller
            return
        kind = kind_for_section(self.section)
        if prose and kind is EntryKind.BULLET:
            kind = EntryKind.ROLE_CONTEXT
        scope = self.role.key if self.role is not None else self.section
        entry_id = _entry_id(kind, text, scope, self.taken)
        self.taken.add(entry_id)
        self.entries.append(
            Entry(
                id=entry_id,
                kind=kind,
                section=self.section,
                text=text,
                role_key=self.role.key if self.role is not None else None,
                group=self.group,
                label=label,
                details=tuple(details),
                order=self.order,
            )
        )
        self.order += 1
