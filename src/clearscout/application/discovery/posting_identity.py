"""What makes one posting the same posting on a different day, or at another host.

Greenhouse, Lever, Ashby, and Workday all carry a requisition identifier in the
URL, and that identifier is what "the same posting" means. Where there is none,
because the posting arrived as a PDF or a screenshot, the fallback is a hash of
the description with its incidental text normalized away, which says only "the
same words" and is weaker on purpose.

The cross-source duplicate rules never decide anything: they group discoveries
a person is then asked to confirm, and no paid guard reads them. The registered
board lookup is stricter, because it refuses paid recovery; it never merges.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import cached_property
from itertools import combinations
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from clearscout.domain.countries import (
    DEFAULT_NAMES,
    DEFAULT_TABLE,
    SUBDIVISION_CODES,
    subdivision_name,
)
from clearscout.domain.identity import is_board_uuid, mention_spans, normalize_text, normalize_url
from clearscout.domain.places import PlaceReader
from clearscout.domain.records import (
    DuplicateKeeperBasis,
    DuplicateSuggestionSignal,
    SourceKind,
)
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.boards import board_identity, without_apply_suffix
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.infrastructure.web_fetch.platforms.ashby import ASHBY
from clearscout.infrastructure.web_fetch.platforms.greenhouse import GREENHOUSE, GREENHOUSE_EMBED
from clearscout.infrastructure.web_fetch.platforms.icims import ICIMS_CLASSIC_JOB
from clearscout.infrastructure.web_fetch.platforms.lever import LEVER
from clearscout.infrastructure.web_fetch.platforms.workday import WORKDAY, workday_posting_path
from clearscout.shared.layout import ITEM_MARKER, RECRUITER_TAG

# Whitespace, casing, and punctuation drift between two fetches of one page
# without the posting having changed; letters, digits, and currency signs do not.
NOISE = re.compile(r"[^\w\s]|_")
SPACES = re.compile(r"\s+")

# Short enough to read in a directory name and type into a bug report.
DIGEST_LENGTH = 12


class Origin(StrEnum):
    """Where a posting's identity came from, which is how much to trust it."""

    REQUISITION = "requisition"
    CONTENT = "content"


@dataclass(frozen=True)
class Posting:
    """One job opening, identified as stably as its source allows."""

    identifier: str
    origin: Origin


def identify(jd: JDText) -> Posting:
    """Return the stable identity of the posting this text came from."""
    requisition = _requisition(jd.origin)
    if requisition is not None:
        return Posting(identifier=requisition, origin=Origin.REQUISITION)
    return Posting(identifier=content_hash(jd.text), origin=Origin.CONTENT)


def posting_url_key(url: str) -> str:
    """Return the identity a posting URL and its apply-path variant share.

    Apply-stripping stays outside ``normalize_url`` so stored fingerprints keep
    their meaning. An unparseable URL digests its raw text rather than raising.
    """
    try:
        folded = without_apply_suffix(normalize_url(url))
    except ValueError:
        folded = url
    return hashlib.sha256(folded.encode("utf-8")).hexdigest()


def posting_requisition_key(url: str) -> str | None:
    """Return the tenant-scoped ATS requisition this URL names, or None.

    Off a recognised board, a URL with a leading locale segment keys on its host and
    remaining path. A locale or location path variant of one requisition shares the
    key, and two tenants' postings do not.
    """
    tenant = board_identity(url)
    if tenant is not None:
        requisition = _requisition(url)
        return None if requisition is None else f"{requisition}@{tenant[0]}:{tenant[1]}"
    unlocalized = _without_locale_segment(url)
    if unlocalized is None:
        return None
    return f"path:{posting_url_key(unlocalized)}"


def posting_board_uuid(url: str) -> str | None:
    """Return the board-issued uuid naming this posting, lowercased, or None.

    A uuid-valued posting id in the query wins. Any id there that is not a uuid names the
    posting, so a uuid beside it or in the path is the board's or tenant's own, as on UKG
    and ADP. Otherwise the last uuid in the path counts.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    ids = [value for key, value in parse_qsl(parts.query) if _POSTING_ID_PARAM.match(key)]
    if ids:
        uuids = [value.lower() for value in ids if is_board_uuid(value)]
        return uuids[0] if uuids and len(uuids) == len(ids) else None
    return next(
        (segment.lower() for segment in reversed(parts.path.split("/")) if is_board_uuid(segment)),
        None,
    )


# `opportunityId`, `jobId`, `gh_jid`, `id`, `req`; a tracking `utm_id` names no posting.
_POSTING_ID_PARAM = re.compile(r"(?!utm_)(?:\w*id|job|req|requisition)$", re.IGNORECASE)


def posting_host(url: str) -> str:
    """Return the normalized host a posting URL is served from."""
    return urlsplit(normalize_url(url)).netloc


def posting_identity_keys(url: str) -> frozenset[str]:
    """Return every key under which a known posting at this URL is recognised."""
    requisition = posting_requisition_key(url)
    return frozenset({posting_url_key(url)} | ({requisition} if requisition else set()))


# A leading `en`, `jp`, `en-us`, or `pt_BR` segment; `usa` or `careers` is a real path.
_LOCALE_SEGMENT = re.compile(r"[a-z]{2}(?:[-_][a-z]{2})?", re.IGNORECASE)


def _locale_split(url: str) -> tuple[str, str] | None:
    """Return a URL's leading locale segment and the URL without it, or None when it has none."""
    try:
        parts = urlsplit(without_apply_suffix(normalize_url(url)))
    except ValueError:
        return None
    first, _, rest = parts.path.lstrip("/").partition("/")
    if not _LOCALE_SEGMENT.fullmatch(first) or not rest.strip("/"):
        return None
    return first.lower().replace("_", "-"), parts._replace(path=f"/{rest}").geturl()


def _without_locale_segment(url: str) -> str | None:
    """Return the URL with a leading locale path segment removed, or None when it has none."""
    split = _locale_split(url)
    return None if split is None else split[1]


def posting_locale(url: str) -> str | None:
    """Return a posting URL's leading locale segment, lowercased, or None when it has none."""
    split = _locale_split(url)
    return None if split is None else split[0]


def _noise(found: re.Match[str]) -> str:
    return found[0] if unicodedata.category(found[0]) == "Sc" else " "


def _words(text: str) -> str:
    """Return the text's words in any script, with its layout and punctuation flattened."""
    # NFKC composes an accent onto its letter, so a decomposed accent is not dropped as noise.
    folded = unicodedata.normalize("NFKC", text).casefold()
    return SPACES.sub(" ", NOISE.sub(_noise, folded)).strip()


def content_hash(text: str) -> str:
    """Return a digest of the posting's words, ignoring how they were laid out."""
    return hashlib.blake2s(_words(text).encode("utf-8"), digest_size=8).hexdigest()[:DIGEST_LENGTH]


def same_words(a: str, b: str) -> bool:
    """Return whether two texts say the same words, however each is laid out."""
    return _words(a) == _words(b)


def _layout(text: str) -> tuple[int, int]:
    """Rank a layout by its list items, then its lines."""
    # `-` is noise to `same_words`, so only this count sees the layout's item markers.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return sum(1 for line in lines if ITEM_MARKER.match(line)), len(lines)


def keeps_stored_layout(stored: str, fresh: str) -> bool:
    """Return whether a fresh read says the stored words in no richer a layout."""
    # One ranking, not a count each way, so two readers that disagree settle on one body.
    return same_words(stored, fresh) and _layout(fresh) <= _layout(stored)


def place_blind_hash(text: str, places: Iterable[str]) -> str:
    """Return a digest of the posting's words without the places and recruiter tags it carries.

    One opening advertised once per city differs only by those, so its copies
    share this digest where they share no body digest. A stored digest is only
    as current as the read that wrote it.
    """
    folded = text.casefold()
    wanted = {stripped for place in places if (stripped := place.casefold().strip())}
    spans = sorted(
        [
            *(span for place in wanted for span in mention_spans(folded, place)),
            *(found.span() for found in RECRUITER_TAG.finditer(folded)),
        ]
    )
    kept: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            kept.append(folded[cursor:start])
        cursor = max(cursor, end)
    kept.append(folded[cursor:])
    return content_hash("".join(kept))


# The markets whose subdivisions a board spells out as readily as it abbreviates them.
_MARKETS = ("United States", "Canada")


def _place_words(spelling: str) -> tuple[str, ...]:
    """Return one spelling as the normalized words a place is folded through."""
    return tuple(normalize_text(spelling).split())


def _place_synonyms() -> dict[tuple[str, ...], str]:
    """Return each foldable place spelling keyed to the one form its equivalents share.

    A name two places claim, such as Georgia, is absent from the country table
    and stays as written.
    """
    folded: dict[tuple[str, ...], str] = {}
    for country in _MARKETS:
        subdivisions = {
            name: code.casefold()
            for code in SUBDIVISION_CODES
            if (name := subdivision_name(country, code))
        }
        folded.update(
            (_place_words(name), code)
            for name, code in subdivisions.items()
            if DEFAULT_TABLE.named_in(name) == country
        )
        folded.update(
            (_place_words(alias), normalize_text(country))
            for alias in DEFAULT_NAMES[country]
            if alias not in subdivisions
        )
    return folded


_PLACE_SYNONYMS = _place_synonyms()
_LONGEST_SYNONYM = max(len(words) for words in _PLACE_SYNONYMS)


def _folded_place(value: str) -> str:
    """Return one place folded so that two wordings of it compare as one.

    Only spellings of a single place fold, so two markets never meet. A bare
    two-letter token stands as written, because "CA" names California and Canada both.
    """
    words = [word for word in normalize_text(value).split() if word != "the"]
    folded: list[str] = []
    index = 0
    while index < len(words):
        for size in range(min(_LONGEST_SYNONYM, len(words) - index), 0, -1):
            synonym = _PLACE_SYNONYMS.get(tuple(words[index : index + size]))
            if synonym is not None:
                folded.append(synonym)
                index += size
                break
        else:
            folded.append(words[index])
            index += 1
    return " ".join(folded)


@dataclass(frozen=True)
class DuplicateCandidate:
    """One discovery a cross-source duplicate signal may group."""

    pending_id: str
    employer: str
    title: str
    location: str
    stated_places: tuple[str, ...]
    urls: tuple[str, ...]
    requisition_id: str
    body_digest: str
    publisher_vouched: bool
    retained_body: bool
    relevance_reviewed: bool
    already_evaluated: bool
    dismissed: bool
    source_type: str
    first_seen_on: date

    # Each pair test reads these, so a bucket of n rows would otherwise fold the
    # same strings n times.
    @cached_property
    def normalized_title(self) -> str:
        """Return the title folded for comparison."""
        return normalize_text(self.title)

    @cached_property
    def normalized_employer(self) -> str:
        """Return the employer folded for comparison."""
        return normalize_text(self.employer)

    @cached_property
    def places(self) -> frozenset[str]:
        """Return every place this discovery states, folded, and empty when it states none."""
        stated = {_folded_place(place) for place in (*self.stated_places, self.location)}
        return frozenset(place for place in stated if place)

    @cached_property
    def hosts(self) -> frozenset[str]:
        """Return the hosts this discovery was advertised at."""
        return frozenset(host for url in self.urls if (host := posting_host(url)))


@dataclass(frozen=True)
class DuplicateSuggestion:
    """Another discovery one signal links to this one, and the one to keep."""

    other_id: str
    signal: DuplicateSuggestionSignal
    keeper_id: str
    keeper_basis: DuplicateKeeperBasis


_Bucketing = Callable[[list[DuplicateCandidate]], dict[tuple[str, ...], list[DuplicateCandidate]]]
_Linked = Callable[[DuplicateCandidate, DuplicateCandidate], bool]


def _digest_buckets(
    candidates: list[DuplicateCandidate],
) -> dict[tuple[str, ...], list[DuplicateCandidate]]:
    buckets: dict[tuple[str, ...], list[DuplicateCandidate]] = {}
    for row in candidates:
        if row.body_digest:
            buckets.setdefault((row.body_digest,), []).append(row)
    return buckets


def _title_employer_buckets(
    candidates: list[DuplicateCandidate],
) -> dict[tuple[str, ...], list[DuplicateCandidate]]:
    buckets: dict[tuple[str, ...], list[DuplicateCandidate]] = {}
    for row in candidates:
        if row.normalized_title and row.normalized_employer:
            buckets.setdefault((row.normalized_title, row.normalized_employer), []).append(row)
    return buckets


def _title_place_buckets(
    candidates: list[DuplicateCandidate],
) -> dict[tuple[str, ...], list[DuplicateCandidate]]:
    """Bucket a discovery once per place it states, so a shared place is one lookup."""
    buckets: dict[tuple[str, ...], list[DuplicateCandidate]] = {}
    for row in candidates:
        if not row.normalized_title:
            continue
        for place in row.places:
            buckets.setdefault((row.normalized_title, place), []).append(row)
    return buckets


def _cross_host(left: DuplicateCandidate, right: DuplicateCandidate) -> bool:
    """Return whether two discoveries were advertised at wholly different hosts."""
    return bool(left.hosts) and bool(right.hosts) and left.hosts.isdisjoint(right.hosts)


def _separately_listed(left: DuplicateCandidate, right: DuplicateCandidate) -> bool:
    """Two discoveries at different hosts, or at one host where only one states a requisition."""
    if _cross_host(left, right):
        return True
    return bool(normalize_text(left.requisition_id)) != bool(normalize_text(right.requisition_id))


def _linked_by_title_and_employer(left: DuplicateCandidate, right: DuplicateCandidate) -> bool:
    if not _separately_listed(left, right):
        return False
    if left.places and right.places and left.places.isdisjoint(right.places):
        return False
    first, second = normalize_text(left.requisition_id), normalize_text(right.requisition_id)
    return not first or not second or first == second


def _linked_by_title_and_place(left: DuplicateCandidate, right: DuplicateCandidate) -> bool:
    # Two vouched employers are settled by the employer signal, and two guesses settle nothing.
    return _cross_host(left, right) and left.publisher_vouched != right.publisher_vouched


_SIGNAL_RULES: tuple[tuple[DuplicateSuggestionSignal, _Bucketing, _Linked], ...] = (
    (DuplicateSuggestionSignal.RETAINED_BODY_DIGEST, _digest_buckets, _cross_host),
    (
        DuplicateSuggestionSignal.TITLE_AND_EMPLOYER,
        _title_employer_buckets,
        _linked_by_title_and_employer,
    ),
    (
        DuplicateSuggestionSignal.TITLE_AND_PLACE_HINT,
        _title_place_buckets,
        _linked_by_title_and_place,
    ),
)

# An employer board speaks for its own opening, a search staged a row under no
# source at all, and an aggregator speaks for nobody.
_SOURCE_ORDER = {SourceKind.EMPLOYER_BOARD.value: 0, "": 1, SourceKind.AGGREGATOR.value: 2}

_KEEPER_RULES: tuple[tuple[DuplicateKeeperBasis, Callable[[DuplicateCandidate], int]], ...] = (
    # A dismissed row is worth linking to, but the queue no longer holds it.
    (DuplicateKeeperBasis.IN_QUEUE, lambda row: 1 if row.dismissed else 0),
    (DuplicateKeeperBasis.ALREADY_EVALUATED, lambda row: 0 if row.already_evaluated else 1),
    (
        DuplicateKeeperBasis.EMPLOYER_BOARD_SOURCE,
        lambda row: _SOURCE_ORDER.get(row.source_type, 2),
    ),
    (DuplicateKeeperBasis.PUBLISHER_VOUCHED, lambda row: 0 if row.publisher_vouched else 1),
    (DuplicateKeeperBasis.RELEVANCE_REVIEWED, lambda row: 0 if row.relevance_reviewed else 1),
    (DuplicateKeeperBasis.RETAINED_BODY, lambda row: 0 if row.retained_body else 1),
    (DuplicateKeeperBasis.FIRST_SEEN, lambda row: row.first_seen_on.toordinal()),
)


def _linked_pairs(
    rows: list[DuplicateCandidate],
) -> list[tuple[DuplicateSuggestionSignal, DuplicateCandidate, DuplicateCandidate]]:
    """Return every linked pair once, under the strongest signal that links it."""
    covered: set[frozenset[str]] = set()
    pairs: list[tuple[DuplicateSuggestionSignal, DuplicateCandidate, DuplicateCandidate]] = []
    for signal, bucketing, linked in _SIGNAL_RULES:
        for bucket in bucketing(rows).values():
            for left, right in combinations(bucket, 2):
                pair = frozenset({left.pending_id, right.pending_id})
                if pair in covered or not linked(left, right):
                    continue
                covered.add(pair)
                pairs.append((signal, left, right))
    return pairs


def _standing(row: DuplicateCandidate) -> tuple[tuple[int, ...], str]:
    """Return a row's place in the keeper order, the identifier breaking a full tie."""
    return tuple(rank(row) for _rule, rank in _KEEPER_RULES), row.pending_id


def _keeper(
    row: DuplicateCandidate, linked: list[DuplicateCandidate]
) -> tuple[str, DuplicateKeeperBasis]:
    """Return the discovery this row should keep, and the rule that separates the two."""
    ordered = sorted([row, *linked], key=_standing)
    keeper = ordered[0]
    # The keeper's own reason is what sets it above the next best copy.
    compared = ordered[1] if row is keeper else row
    # The order is lexicographic, so the first rule the two differ on favors the keeper.
    for rule, rank in _KEEPER_RULES:
        if rank(keeper) != rank(compared):
            return keeper.pending_id, rule
    return keeper.pending_id, DuplicateKeeperBasis.IDENTIFIER_ORDER


def duplicate_suggestions(
    candidates: Iterable[DuplicateCandidate],
) -> dict[str, list[DuplicateSuggestion]]:
    """Return each discovery's likely duplicates, strongest signal first and each pair once."""
    rows = list(candidates)
    pairs = _linked_pairs(rows)
    by_id = {row.pending_id: row for row in rows}
    linked_to: dict[str, list[DuplicateCandidate]] = {}
    for _signal, left, right in pairs:
        linked_to.setdefault(left.pending_id, []).append(right)
        linked_to.setdefault(right.pending_id, []).append(left)
    # No signal is transitive, so a row may only be told to keep itself or one of
    # the rows a signal linked to it directly.
    keepers = {
        pending_id: _keeper(by_id[pending_id], others) for pending_id, others in linked_to.items()
    }
    found: dict[str, list[DuplicateSuggestion]] = {}
    for signal, left, right in pairs:
        for side, other in ((left, right), (right, left)):
            keeper_id, basis = keepers[side.pending_id]
            found.setdefault(side.pending_id, []).append(
                DuplicateSuggestion(
                    other_id=other.pending_id,
                    signal=signal,
                    keeper_id=keeper_id,
                    keeper_basis=basis,
                )
            )
    return found


_PLACES = PlaceReader(DEFAULT_TABLE)
_TRAILING_PARENTHETICAL = re.compile(r"\s*\(([^()]*)\)\s*$")
_ARRANGEMENT = re.compile(r"\b(?:remote|hybrid|on[- ]?site|in[- ]office)\b", re.IGNORECASE)
_SEPARATORS = re.compile(r"^[\s,/|;:-]+|[\s,/|;:-]+$")


def _board_title(title: str) -> tuple[str, str]:
    """Return a title folded for a board match, and the place a peeled parenthetical names.

    A trailing parenthetical is peeled only when it is an arrangement, a place the
    country table names outright, or both; a city index guess such as "Enterprise"
    stays part of the title.
    """
    found = _TRAILING_PARENTHETICAL.search(title)
    if found is None:
        return normalize_text(title), ""
    arranged = _ARRANGEMENT.search(found.group(1)) is not None
    place = _SEPARATORS.sub("", _ARRANGEMENT.sub(" ", found.group(1)))
    named = bool(normalize_text(place)) and bool(_PLACES.read(place).explicit)
    if not (named or (arranged and not normalize_text(place))):
        return normalize_text(title), ""
    return normalize_text(title[: found.start()]), place


@dataclass(frozen=True)
class BoardHolder:
    """The record stored from an employer's registered board that holds a row's role."""

    entity_type: Literal["job_posting", "pending_discovery"]
    entity_id: str
    url: str


@dataclass(frozen=True)
class BoardRecord:
    """One tracked posting or pending row an enabled employer board supplied."""

    holder: BoardHolder
    source_id: str
    title: str
    places: tuple[str, ...]
    requisitions: frozenset[str]
    closed: bool


def _folded_places(title: str, places: Iterable[str]) -> frozenset[str]:
    """Return the places a record states, with arrangement words, which name no place, dropped."""
    peeled = _board_title(title)[1]
    return frozenset(
        folded
        for place in (*places, peeled)
        if (folded := _folded_place(_ARRANGEMENT.sub(" ", place)))
    )


def _conflicts(places: frozenset[str], requisitions: frozenset[str], record: BoardRecord) -> bool:
    """Return whether a record leaves the row's places unconfirmed or names another requisition."""
    theirs = _folded_places(record.title, record.places)
    # A record stating no place cannot corroborate a row that states one.
    if places and places.isdisjoint(theirs):
        return True
    return bool(
        requisitions and record.requisitions and requisitions.isdisjoint(record.requisitions)
    )


def board_holder(
    title: str,
    places: Iterable[str],
    requisitions: frozenset[str],
    records: Iterable[BoardRecord],
) -> BoardHolder | None:
    """Return the one open board record holding this role, or None when the match is unsafe."""
    wanted = _board_title(title)[0]
    if not wanted:
        return None
    hits = [record for record in records if _board_title(record.title)[0] == wanted]
    if len({record.source_id for record in hits}) != 1:
        return None
    mine = _folded_places(title, places)
    survivors = [record for record in hits if not _conflicts(mine, requisitions, record)]
    if len({normalize_url(record.holder.url) for record in survivors}) != 1:
        return None
    if any(record.closed for record in survivors):
        return None
    # A tracked posting at the same address outranks a pending row beside it.
    return min(survivors, key=lambda record: record.holder.entity_type != "job_posting").holder


def _requisition(url: str) -> str | None:
    """Return the applicant tracking system's own identifier for this posting.

    Prefixed with the board so that two systems cannot produce the same string.
    """
    for board, pattern in (
        ("gh", GREENHOUSE),
        ("gh", GREENHOUSE_EMBED),
        ("lv", LEVER),
        ("ab", ASHBY),
        ("wd", WORKDAY),
        ("ic", ICIMS_CLASSIC_JOB),
    ):
        match = host_match(pattern, url)
        if match is None:
            continue
        job = match.groupdict().get("job")
        if job:
            return f"{board}-{job[:DIGEST_LENGTH]}"
        posting = workday_posting_path(match).rpartition("/")[2]
        if posting:
            return f"{board}-{content_hash(posting)}"
    return None
