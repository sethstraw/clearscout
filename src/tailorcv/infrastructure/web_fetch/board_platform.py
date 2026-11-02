"""What one job board platform declares about itself, and the host primitives its shapes use.

Each platform module under ``platforms`` builds one ``BoardPlatform``; the
registry there is the only list of platforms, and every per-platform list the
fetchers consult is derived from it.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from httpx import Cookies

    from tailorcv.application.discovery.source_reading import (
        PostingCheck,
        SourceContinuationTarget,
        SourceListing,
        SourceRead,
    )
    from tailorcv.infrastructure.web_fetch.board_mappings import BoardDeclaration

# The order is the wire vocabulary's, so a new kind is appended.
BoardKind = Literal[
    "greenhouse",
    "lever",
    "workday",
    "ashby",
    "oracle",
    "smartrecruiters",
    "workable",
    "recruitee",
    "phenom",
    "icims",
    "himalayas",
    "arbeitnow",
    "dayforce",
    "bamboohr",
    "gem",
    "pinpoint",
    "paylocity",
    "teamtailor",
    "personio",
    "comeet",
    "rippling",
    "dover",
    "adp",
    "breezy",
    "jazzhr",
    "jobvite",
    "hibob",
    "zoho",
    "avature",
    "successfactors",
    "tbk",
    "alignerr",
    "cornerstone",
    "jobsyn",
    "darwinbox",
    "taleo",
    "fxrecruiter",
    "ycombinator",
    "eightfold",
    "jpaginate",
    "trinethire",
    "clearcompany",
    "getro",
    "taleobusiness",
    "peoplesoft",
    "shopify",
    "deel",
]

# Bounds for a host literal: no host character before it and no further label after
# it, so `acme.recruitee.com.evil.example` and `evilgreenhouse.io` name no board.
# A host may follow an escaped slash, but never begins with the escape's own hex digits.
HOST_START = r"(?:(?<=%2[Ff])|(?<![\w%-]))"
HOST_END = r"(?![\w@-]|\.[\w-])"


def hosted_org(slug: str) -> str:
    """Return an org group of ``slug`` characters that skips the host's own asset paths."""
    # A reserved word ends where the slug does, so the guard holds in a URL and in markup.
    return rf"(?P<org>(?!_|(?:static|assets|api)(?!{slug})){slug}+)"


_ADDRESS = re.compile(
    r"(?P<head>(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)?(?P<host>[^/?#]*)(?P<path>[^?#]*)(?P<tail>.*)",
    re.DOTALL,
)
_DEFAULT_PORT = re.compile(r":(?:80|443)$")


def host_match(pattern: re.Pattern[str], url: str) -> re.Match[str] | None:
    """Return the pattern's match in a URL when it begins in the host, not a path or query.

    The match runs over the address with its host lowercased and its userinfo,
    default port, trailing dot and doubled path slashes dropped, which
    ``match.string`` holds.
    """
    parts = _ADDRESS.match(url)
    # A browser reads a backslash as a path slash, so the host would end before it.
    if parts is None or "\\" in parts.group("host"):
        return None
    head = parts.group("head") or ""
    host = parts.group("host").rpartition("@")[2].lower()
    host = _DEFAULT_PORT.sub("", host).removesuffix(".")
    path = re.sub(r"/{2,}", "/", parts.group("path"))
    match = pattern.search(f"{head}{host}{path}{parts.group('tail')}")
    return match if match is not None and match.start() < len(head) + len(host) else None


class BoardTransport(Protocol):
    """The requests, deadline, and paging guards a platform reader is given."""

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        """Return the decoded JSON body of a GET, adding any ``headers`` its API requires."""
        ...

    def workday_detail(self, url: str) -> PostingCheck:
        """Read one just-listed Workday detail without treating a transient 404 as closure."""
        ...

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        """Return the decoded JSON body of a POST carrying body."""
        ...

    def post_json_with_headers(
        self, url: str, body: dict[str, Any], *, headers: Mapping[str, str]
    ) -> Any:
        """Return a JSON POST response with explicit publisher protocol headers."""
        ...

    def register_host_minimum_interval(self, url: str, seconds: float) -> None:
        """Raise the request interval floor for the URL's logical host."""
        ...

    def get_text(self, url: str, *, max_bytes: int = ...) -> str:
        """Return the final URL, a newline, then up to ``max_bytes`` of the page."""
        ...

    def get_feed(self, url: str) -> str:
        """Return a complete XML or RSS body."""
        ...

    def session_text(
        self, url: str, cookies: Cookies, *, form: Mapping[str, str] | None = None
    ) -> str:
        """Return the final URL, a newline, then a GET or ``form`` POST page read in ``cookies``."""
        ...

    def deadline_elapsed(self) -> bool:
        """Return whether the current board read has used up its time."""
        ...

    def paginated_request(
        self, request: Callable[[], Any], listings: list[SourceListing], raw_count: int
    ) -> Any:
        """Run one page request, turning a spent deadline into a partial read."""
        ...

    def page_or_stop(
        self,
        url: str,
        request: Callable[[str], Any],
        listings: list[SourceListing],
        raw_count: int,
        *,
        first_page: bool,
        cursor: SourceContinuationTarget,
    ) -> Any:
        """Return one page's body, or the incomplete read that resumes at ``cursor``."""
        ...

    def detailed_read(
        self,
        employer: str,
        rows: list[Any],
        *,
        mapping: BoardDeclaration,
        detail: Callable[[str], Mapping[str, Any] | None],
        groups: dict[str, str] | None = None,
        record_key: str = "detail",
        withdrawn: Callable[[Mapping[str, Any]], bool] = ...,
        row_id: Callable[[Any], str] | None = None,
        missing_fails: bool = False,
        robots_skips: bool = False,
        resume: tuple[str, str] | None = None,
        resume_target: SourceContinuationTarget | None = None,
        enumeration_complete: bool = True,
        relist_required: bool = False,
        records_listed: bool = False,
        skip_recent: bool = False,
    ) -> SourceRead:
        """Join each list row to its detail record, the only answer that states a body."""
        ...


class BoardAdapter(Protocol):
    """Read one recognized board through its platform-specific API."""

    # ``url`` is the address the platform was recognized at, which for a
    # fingerprinted career site is the host its API is read from.
    def __call__(
        self, fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str, /
    ) -> SourceRead:
        """Return one board's openings, and whether that was the whole board."""
        ...


class BoardContinuation(Protocol):
    """Resume one structured board page a bounded read left in the durable frontier."""

    def __call__(
        self, fetcher: BoardTransport, employer: str, target: SourceContinuationTarget, /
    ) -> SourceRead:
        """Return that page's openings and the cursor for the page after it."""
        ...


# A pattern and the board root its match names; the formatter gets the matched address.
RootRule = tuple[re.Pattern[str], Callable[[str, re.Match[str]], str]]
# A pattern and the locale- and case-free tenant key its match names.
IdentityRule = tuple[re.Pattern[str], Callable[[re.Match[str]], str]]


_BOARD_QUERY = re.compile(r"""/?\?([^"'\s<>#\\]*)""")
# An entity-escaped link only shows where its markup resumes once unescaped.
_QUERY_TAIL = re.compile(r"""["'<>\\\s].*""", re.DOTALL)


def board_query(match: re.Match[str], start: int | None = None) -> str:
    """Return the unescaped query after a board match (or after ``start``), or ""."""
    query = _BOARD_QUERY.match(match.string, match.end() if start is None else start)
    if query is None:
        return ""
    return _QUERY_TAIL.sub("", html.unescape(query.group(1))).rstrip(".,;)")


def match_groups(match: re.Match[str]) -> dict[str, str]:
    """Return a board match's named groups, empty where a group did not take part."""
    return match.groupdict(default="")


def query_groups(match: re.Match[str]) -> dict[str, str]:
    """Return a board match's groups plus the query its address carries."""
    return {**match_groups(match), "board_query": board_query(match)}


@dataclass(frozen=True)
class BoardPlatform:
    """One board platform: the shapes that name it, how its roots fold, and its reader.

    Every tuple is in match order. ``single_tenant`` says the board address
    itself names one employer; ``employer_slug`` says its tenant is that
    employer's own slug; ``posting_api`` says it publishes structured postings
    worth trying before rendered HTML. ``continuations`` maps each deferred
    page reason this platform emits to the reader that resumes it.
    """

    name: BoardKind
    board_patterns: tuple[re.Pattern[str], ...]
    adapter: BoardAdapter
    job_patterns: tuple[re.Pattern[str], ...] = ()
    source_roots: tuple[RootRule, ...] = ()
    identity_rules: tuple[IdentityRule, ...] = ()
    single_tenant: bool = True
    employer_slug: bool = False
    posting_api: bool = False
    continuations: Mapping[str, BoardContinuation] = field(default_factory=dict)
    # The groups the adapter reads from a board match; read scope never sees them.
    adapter_groups: Callable[[re.Match[str]], dict[str, str]] = match_groups
    # How a person sees the platform named; empty means the capitalized key.
    label: str = ""

    @property
    def display_name(self) -> str:
        """Return the name a user-visible sentence gives this platform."""
        return self.label or self.name.capitalize()
