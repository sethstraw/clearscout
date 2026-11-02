"""Deterministic posting identity, and the text primitives it is folded with."""

from __future__ import annotations

import functools
import hashlib
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from cleanco.clean import get_unique_terms


def normalize_text(value: str) -> str:
    """Fold identity text without treating punctuation as meaningful."""
    lowered = unquote(value).lower().strip()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


def _legal_forms() -> frozenset[tuple[str, ...]]:
    """Return every legal form cleanco lists, plus the app's own, as normalized word runs."""
    listed = {tuple(normalize_text(term).split()) for term in get_unique_terms()}
    # cleanco lacks PBC and the bare Pty and BV; Group and Holdings name no other employer here.
    listed |= {("pbc",), ("pty",), ("bv",), ("group",), ("holdings",)}
    # These also end ordinary names or name a place: "Globex UK" is not Globex.
    listed -= {
        (word,)
        for word in (
            "at",
            "cio",
            "ee",
            "ent",
            "ev",
            "ks",
            "ky",
            "mb",
            "nl",
            "o",
            "ok",
            "private",
            "sd",
            "sf",
            "uk",
            "unlimited",
        )
    }
    return frozenset(form for form in listed if form)


# Trailing legal forms name no different employer: "Northwind Inc." is Northwind.
_LEGAL_FORMS = _legal_forms()
_LONGEST_LEGAL_FORM = max(len(form) for form in _LEGAL_FORMS)


def _trailing_legal_form(words: list[str]) -> int:
    """Return how many trailing words make up a legal form, longest first, or 0."""
    for size in range(min(_LONGEST_LEGAL_FORM, len(words)), 0, -1):
        if tuple(words[-size:]) in _LEGAL_FORMS:
            return size
    return 0


def employer_name_words(employer: str) -> list[str]:
    """Return an employer name's folded words without its trailing legal forms.

    The first word always stays, so a name made only of legal forms keeps one.
    """
    words = normalize_text(employer).split()
    while (size := _trailing_legal_form(words)) and size < len(words):
        del words[-size:]
    return words


# A uuid after normalize_text: its five hex groups, the dashes folded to spaces.
_BOARD_UUID = re.compile(r"[0-9a-f]{8} [0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{4} [0-9a-f]{12}")


def is_board_uuid(requisition: str) -> bool:
    """Return whether a requisition is a board-issued uuid, which names one opening anywhere."""
    return _BOARD_UUID.fullmatch(normalize_text(requisition)) is not None


def requisition_key(employer: str, requisition: str) -> str:
    """Return the stored identity of a requisition the named employer states."""
    folded = normalize_text(requisition)
    # A board-issued uuid cannot name two openings, so no employer spelling scopes it.
    if is_board_uuid(folded):
        return folded
    return f"{normalize_text(employer)}|{folded}"


# Screening one queue page scans every country spelling and place name against
# every row, and re's own cache holds 512 patterns before it drops all of them.
@functools.lru_cache(maxsize=4096)
def _whole_word_pattern(needle: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\W\d_]){re.escape(needle)}(?![^\W\d_])", flags)


def mention_spans(haystack: str, needle: str) -> tuple[tuple[int, int], ...]:
    """Return every whole-word span of casefolded ``needle`` in casefolded ``haystack``.

    The boundary is "not a letter", so "Remote (US)" and "US2" match, "discuss" does not.
    """
    # The pattern is the escaped needle between two boundaries, so haystack
    # without the literal needle cannot match it.
    if not needle or needle not in haystack:
        return ()
    return tuple(found.span() for found in _whole_word_pattern(needle).finditer(haystack))


def written_mention_spans(text: str, needle: str) -> tuple[tuple[int, int], ...]:
    """Return every whole-word span of ``needle`` in ``text`` as written, ignoring case.

    Casefolding can change a text's length, so these offsets index ``text`` itself.
    """
    if not needle:
        return ()
    return tuple(
        found.span() for found in _whole_word_pattern(needle, re.IGNORECASE).finditer(text)
    )


def mentions(haystack: str, needle: str) -> bool:
    """Return whether ``needle`` appears in ``haystack`` as whole words."""
    return bool(mention_spans(haystack, needle))


# normalize_text folds every ampersand to a space, so one never reaches this set.
PHRASE_CONNECTORS = frozenset({"and", "at", "for", "in", "of", "the", "to", "with"})


def _phrase_words(phrase: str) -> tuple[list[str], set[int]]:
    """Return a phrase's content words and the positions it joins to the word before with "and"."""
    wanted: list[str] = []
    joined: set[int] = set()
    for word in phrase.split():
        if word == "and":
            joined.add(len(wanted))
        elif word not in PHRASE_CONNECTORS:
            wanted.append(word)
    return wanted or phrase.split(), joined


def mentions_in_order(haystack: str, needle: str) -> bool:
    """Return whether ``needle``'s words run through ``haystack`` in order, as whole words.

    Both sides are already normalized. Only a connector may sit between two of
    them, so "Director of Clinical Informatics" names "Director Clinical
    Informatics" while "data science engineering manager" does not name
    "data manager". A connector inside ``needle`` is optional in the same way,
    and an "and" may sit only where ``needle`` has one, because it joins two things.
    """
    wanted, joined = _phrase_words(needle)
    if not wanted:
        return False
    found = 0
    for word in haystack.split():
        if word == wanted[found]:
            found += 1
            if found == len(wanted):
                return True
        elif found and (word not in PHRASE_CONNECTORS or (word == "and" and found not in joined)):
            found = 1 if word == wanted[0] else 0
    return False


# A Candidate Gateway serves its search and every posting at one component path,
# so only these query keys tell its pages apart.
_PEOPLESOFT_GATEWAY = re.compile(r"/ps[cp]/\w+/\w+/\w+/c/hrs_hram(?:_fl)?\.\w+\.gbl", re.IGNORECASE)


def normalize_url(value: str) -> str:
    """Keep only URL material that identifies a posting."""
    if not value.strip():
        return ""
    raw = value.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    # A scheme written as "ftp://" must not fold onto the https posting's identity key.
    scheme = parts.scheme if parts.scheme not in ("", "http", "https") else "https"
    host = parts.netloc.lower()
    host = host.removeprefix("www.")
    if host.endswith(".linkedin.com"):
        host = "linkedin.com"
    elif host.endswith(".indeed.com"):
        host = "indeed.com"
    elif host == "app.dover.com":
        # Dover serves postings at .com while its API states the .io address of each.
        host = "app.dover.io"
    path = re.sub(r"/+", "/", unquote(parts.path)).rstrip("/")
    identity_keys = {
        "gh_jid",
        "id",
        "jid",
        "job",
        "jobid",
        "jk",
        "opportunityid",
        "req",
        "requisition_id",
        "requisitionid",
    }
    if host == "workforcenow.adp.com":
        identity_keys.add("cid")
    elif _PEOPLESOFT_GATEWAY.fullmatch(path):
        identity_keys |= {"jobopeningid", "page", "postingseq", "siteid"}
    identity_query = sorted(
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() in identity_keys
    )
    return urlunsplit((scheme, host, path, urlencode(identity_query), ""))


def url_sha256(value: str) -> str:
    """Return a privacy-safe stable digest of one normalized URL."""
    return hashlib.sha256(normalize_url(value).encode("utf-8")).hexdigest()


_TRACKING_QUERY_KEYS = frozenset(
    {"_ga", "_gl", "dclid", "fbclid", "gclid", "gbraid", "mc_cid", "mc_eid", "msclkid", "wbraid"}
)


def normalize_fetch_url(value: str) -> str:
    """Normalize a fetch identity while preserving pagination and routing queries.

    A frontier row keys on this, so each page of a paginated board stays its own target.
    """
    parsed = urlsplit(value.strip())
    if not is_absolute_http_url(value.strip()):
        return ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
        ),
        doseq=True,
    )
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, query, ""))


def exact_url_key(value: str) -> str:
    """Return an absolute HTTP URL keyed with its whole query, or "" for any other value.

    Unlike ``normalize_url`` it keeps every query key, so a cited address matches only itself.
    """
    if not is_absolute_http_url(value):
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def is_absolute_http_url(value: str) -> bool:
    """Return whether ``value`` is an http or https URL that names a host."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)


def url_host(value: str) -> str:
    """Return a URL's lowercase host without its port, or "" when it names none."""
    try:
        return urlsplit(value).hostname or ""
    except ValueError:
        return ""


def url_path_segments(value: str) -> tuple[str, ...]:
    """Return a URL's nonempty path segments, percent-decoded."""
    try:
        path = urlsplit(value).path
    except ValueError:
        return ()
    return tuple(unquote(segment) for segment in path.split("/") if segment)


def url_query_keys(value: str) -> frozenset[str]:
    """Return a URL's query parameter names, casefolded."""
    try:
        query = urlsplit(value).query
    except ValueError:
        return frozenset()
    return frozenset(key.casefold() for key, _ in parse_qsl(query))
