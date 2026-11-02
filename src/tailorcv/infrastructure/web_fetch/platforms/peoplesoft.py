"""PeopleSoft Candidate Gateway searches: classic (a portal around a content frame) and Fluid."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

from tailorcv.infrastructure.web_fetch.board_mappings import PEOPLESOFT_MAPPING
from tailorcv.infrastructure.web_fetch.board_mappings.peoplesoft import (
    PEOPLESOFT_NEXT_SET,
    gateway_page,
    peoplesoft_openings,
    peoplesoft_posting,
    peoplesoft_total,
)
from tailorcv.infrastructure.web_fetch.board_platform import (
    HOST_START,
    BoardPlatform,
    board_query,
    host_match,
    match_groups,
)
from tailorcv.infrastructure.web_fetch.board_reads import (
    PublisherSearchPage,
    publisher_search_detail,
    publisher_search_read,
)

if TYPE_CHECKING:
    from tailorcv.application.discovery.source_reading import SourceContinuationTarget, SourceRead
    from tailorcv.infrastructure.web_fetch.board_platform import BoardTransport

# A posting page shares its search's component address, so it reads inside the same scope.
PEOPLESOFT_BOARD = re.compile(
    HOST_START
    + r"(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/(?P<servlet>psp|psc)/(?P<site>\w+)/"
    + r"(?P<portal>\w+)/(?P<node>\w+)/c/"
    + r"(?P<component>HRS_HRAM\.HRS_APP_SCHJOB|HRS_HRAM_FL\.HRS_CG_SEARCH_FL)\.GBL(?=\?)",
    re.IGNORECASE,
)
_REASON = "peoplesoft_search_page"
# A guest grid shows 25 (classic) or 50 (Fluid) rows a set, so this bounds a replayed walk.
_MAX_SETS = 200


def _site_id(match: re.Match[str]) -> str:
    return dict(parse_qsl(board_query(match))).get("SiteId", "")


def _adapter_groups(match: re.Match[str]) -> dict[str, str]:
    """Return a board match's groups plus the SiteId its query names."""
    return {**match_groups(match), "site_id": _site_id(match)}


def _component_url(groups: Mapping[str, str], query: dict[str, str]) -> str:
    path = "/".join((groups["servlet"], groups["site"], groups["portal"], groups["node"], "c"))
    return f"https://{groups['host']}/{path}/{groups['component']}.GBL?" + urlencode(query)


def _fluid(groups: Mapping[str, str]) -> bool:
    return groups["component"].upper().startswith("HRS_HRAM_FL.")


def _search_query(groups: Mapping[str, str]) -> dict[str, str]:
    return {
        "Page": "HRS_APP_SCHJOB_FL" if _fluid(groups) else "HRS_APP_SCHJOB",
        "Action": "U",
        "FOCUS": "Applicant",
        "SiteId": groups["site_id"],
    }


def _posting_query(groups: Mapping[str, str], job: str) -> dict[str, str]:
    return {
        "Page": "HRS_APP_JBPST_FL" if _fluid(groups) else "HRS_APP_JBPST",
        "Action": "U",
        "FOCUS": "Applicant",
        "SiteId": groups["site_id"],
        "JobOpeningId": job,
        "PostingSeq": "1",
    }


def _framed(fetcher: BoardTransport, url: str, cookies: httpx.Cookies) -> tuple[str, str]:
    """Return the final address and markup of a page, read through a portal's content frame."""
    final, _, markup = fetcher.session_text(url, cookies).partition("\n")
    frame = gateway_page(markup).frame
    if not frame:
        return final, markup
    framed = urljoin(final, frame)
    if host_match(PEOPLESOFT_BOARD, framed) is None:
        raise ValueError("PeopleSoft portal framed a page outside a Candidate Gateway")
    final, _, markup = fetcher.session_text(framed, cookies).partition("\n")
    return final, markup


class _GridSession:
    """One guest session's walk through the job grid, where each set follows the one before."""

    def __init__(self, fetcher: BoardTransport, groups: Mapping[str, str]) -> None:
        self.fetcher, self.groups = fetcher, groups
        self.cookies = httpx.Cookies()
        self.page, self.markup, self.action = 0, "", ""
        self.seen: set[str] = set()

    def _take(self, final: str, markup: str) -> list[dict[str, Any]]:
        """Keep one grid set and return the openings it adds to the walk."""
        form = gateway_page(markup)
        action = urljoin(final, form.action)
        if not form.action or urlsplit(action)[:3] != urlsplit(final)[:3]:
            raise ValueError("PeopleSoft Candidate Gateway did not answer its job search form")
        openings = peoplesoft_openings(markup)
        if not openings and peoplesoft_total(markup) is None:
            raise ValueError("PeopleSoft Candidate Gateway did not render its job grid")
        self.markup, self.action, self.page = markup, action, self.page + 1
        added = [opening for opening in openings if opening["id"] not in self.seen]
        self.seen.update(opening["id"] for opening in added)
        return added

    def _open(self) -> list[dict[str, Any]]:
        self.cookies, self.page, self.seen = httpx.Cookies(), 0, set()
        search = _component_url(self.groups, _search_query(self.groups))
        return self._take(*_framed(self.fetcher, search, self.cookies))

    def _next_set(self) -> list[dict[str, Any]]:
        fields = {**gateway_page(self.markup).fields, "ICAction": PEOPLESOFT_NEXT_SET}
        answer = self.fetcher.session_text(self.action, self.cookies, form=fields)
        final, _, markup = answer.partition("\n")
        return self._take(final, markup)

    def request(self, page: int) -> PublisherSearchPage:
        """Return one grid set's new openings, replaying the walk when a read resumes."""
        if not 1 <= page <= _MAX_SETS:
            raise ValueError("PeopleSoft grid set is outside the bounded walk")
        if page == 1 or self.page != page - 1:
            added = self._open()
            while self.page < page:
                added = self._next_set()
        else:
            added = self._next_set()
        total = peoplesoft_total(self.markup)
        more = (
            bool(added)
            and f"'{PEOPLESOFT_NEXT_SET}'" in self.markup
            and (total is None or len(self.seen) < total)
        )
        rows = []
        for opening in added:
            url = _component_url(self.groups, _posting_query(self.groups, opening["id"]))
            framed = self._frame(opening["id"])
            rows.append({**opening, "url": url, **({"frame": framed} if framed != url else {})})
        return PublisherSearchPage(rows, page + 1 if more else None, total)

    def _frame(self, job: str) -> str:
        """Return a posting's address on the content frame that answered the search form."""
        return f"{self.action}?{urlencode(_posting_query(self.groups, job))}"

    def detail(self, row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Read one posting in this session, straight from its content frame.

        A resumed read opens the guest session first, since the gateway serves a
        posting only to the session that searched.
        """
        try:
            if not self.page:
                self._open()
            address = str(row.get("frame") or self._frame(str(row["id"])))
            _final, markup = _framed(self.fetcher, address, self.cookies)
        except ValueError:
            return None
        return peoplesoft_posting(markup)


def _search(
    fetcher: BoardTransport,
    employer: str,
    groups: Mapping[str, str],
    source_url: str,
    target: SourceContinuationTarget | None = None,
) -> SourceRead:
    if not re.fullmatch(r"\w+", groups.get("site_id", "")):
        raise ValueError("PeopleSoft Candidate Gateway address did not name its SiteId")
    session = _GridSession(fetcher, groups)
    return publisher_search_read(
        fetcher,
        employer,
        source_url=source_url,
        reason=_REASON,
        first_page=1,
        request=session.request,
        mapping=PEOPLESOFT_MAPPING,
        detail=session.detail,
        target=target,
    )


def _read(fetcher: BoardTransport, employer: str, groups: dict[str, str], url: str) -> SourceRead:
    # A career page that links the gateway is read under the gateway's own search address.
    if host_match(PEOPLESOFT_BOARD, url) is not None:
        return _search(fetcher, employer, groups, url)
    return _search(fetcher, employer, groups, _component_url(groups, _search_query(groups)))


def _source_groups(target: SourceContinuationTarget) -> dict[str, str]:
    match = host_match(PEOPLESOFT_BOARD, target.source_url)
    if match is None:
        raise ValueError("PeopleSoft continuation has no Candidate Gateway source")
    return _adapter_groups(match)


def _continue(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    return _search(fetcher, employer, _source_groups(target), target.source_url, target)


def _continue_detail(
    fetcher: BoardTransport, employer: str, target: SourceContinuationTarget
) -> SourceRead:
    session = _GridSession(fetcher, _source_groups(target))
    return publisher_search_detail(
        fetcher, employer, target, mapping=PEOPLESOFT_MAPPING, detail=session.detail
    )


def _identity(match: re.Match[str]) -> str:
    groups = match.groupdict()
    parts = (groups["host"], groups["site"], groups["portal"], groups["node"])
    return "/".join((*parts, groups["component"], _site_id(match))).casefold()


PLATFORM = BoardPlatform(
    name="peoplesoft",
    label="PeopleSoft Candidate Gateway",
    board_patterns=(PEOPLESOFT_BOARD,),
    adapter=_read,
    identity_rules=((PEOPLESOFT_BOARD, _identity),),
    continuations={_REASON: _continue, "peoplesoft_search_detail": _continue_detail},
    adapter_groups=_adapter_groups,
)
