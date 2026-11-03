"""PeopleSoft Candidate Gateway searches read every grid set in one guest cookie session."""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from clearscout.application.discovery.posting_identity import posting_identity_keys
from clearscout.application.discovery.source_reading import SourceReadOptions
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher, identify_board
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.boards import board_identity
from clearscout.infrastructure.web_fetch.platforms.peoplesoft import (
    _MAX_SETS,
    PEOPLESOFT_BOARD,
    _adapter_groups,
    _GridSession,
)
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from tests.support.http import public_resolver

_FLUID = "https://careers.acme.example/psc/hrprd/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
_FLUID_SEARCH = f"{_FLUID}?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant&SiteId=2"
_PORTAL = "https://careers.acme.example/psp/hrcs/EMPLOYEE/HRMS/c/HRS_HRAM.HRS_APP_SCHJOB.GBL"
_PORTAL_SEARCH = f"{_PORTAL}?Page=HRS_APP_SCHJOB&Action=U&FOCUS=Applicant&SiteId=1001"
_FRAME = "https://hr.acme.example/psc/hrprd/EMPLOYEE/HRMS/c/HRS_HRAM.HRS_APP_SCHJOB.GBL"
_COOKIE = "PORTAL-PSJSESSIONID=guest42"


def _posting_query(page: str, site: int, job: str) -> str:
    return f"?Page={page}&Action=U&FOCUS=Applicant&SiteId={site}&JobOpeningId={job}&PostingSeq=1"


def _form(action: str, state: int, grid: str, more: str) -> str:
    return (
        f"<form id='HRS' name='win0' method='post' action=\"{action}\">"
        f"<input type='hidden' name='ICStateNum' id='ICStateNum' value='{state}' />"
        "<input type='hidden' name='ICAction' id='ICAction' value='None' />"
        "<input type='hidden' name='ICSID' id='ICSID' value='s1d=' />"
        f"{grid}{more}</form>"
    )


def _fluid_grid(rows: list[tuple[str, str, str]], total: int) -> str:
    cells = "".join(
        f"<li class='ps_grid-row'><span class='ps_box-value' id='SCH_JOB_TITLE${n}'>{title}</span>"
        f"<span class='ps_box-value' id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID${n}'>{job}</span>"
        f"<span class='ps_box-value' id='LOCATION${n}'>{place}</span>"
        f"<span class='ps_box-value' id='SCH_OPENED${n}'>10/21/2026</span></li>"
        for n, (job, title, place) in enumerate(rows)
    )
    more = (
        "<div class='ps_box-more' "
        "onclick=\"submitAction_win0(document.win0,'HRS_AGNT_RSLT_I$hdown$0');\">more</div>"
    )
    count = f"<div class='ps-htmlarea'><b>{total}</b> jobs found.</div>"
    return _form(_FLUID, len(rows), count + f"<ul>{cells}</ul>", more)


def _classic_grid(rows: list[tuple[str, str, str]], total: int, *, last: bool) -> str:
    cells = "".join(
        f"<tr><td><a class='PSHYPERLINK' id='POSTINGLINK${n}' href=\"javascript:void(0);\">"
        f"{title} - {job}</a><span id='JOBATTRIBUTES${n}'><b>Department:</b> Research"
        f"   |   <b>Location:</b> {place}   |   <b>Posted Date:</b> 2026/10/20</span></td></tr>"
        for n, (job, title, place) in enumerate(rows)
    )
    more = (
        "<img id='HRS_AGNT_RSLT_I$hdown$img$0' class='PTNEXTROW_D' alt='Show next set' />"
        if last
        else "<a id='HRS_AGNT_RSLT_I$hdown$0' name='HRS_AGNT_RSLT_I$hdown$0' "
        "href=\"javascript:submitAction_win0(document.win0,'HRS_AGNT_RSLT_I$hdown$0');\">"
        "<img id='HRS_AGNT_RSLT_I$hdown$img$0' class='PTNEXTROW' /></a>"
    )
    count = f"<span class='PSGRIDCOUNTER'>{total} matches found</span>"
    return _form(_FRAME, 1, count + f"<table>{cells}</table>", more)


def _posting(title: str, place: str, body: str, *, fluid: bool) -> str:
    heading = (
        "<h2><span class='ps-text' id='HRS_SCH_WRK_DESCR100$0lbl'>About Acme Health</span></h2>"
        if fluid
        else "<span class='PABOLDTEXT' id='HRS_SCH_PSTDSC_DESCR$0'>About Acme Health</span>"
    )
    return (
        "<html><body>"
        f"<span class='ps_box-value' id='HRS_SCH_WRK2_POSTING_TITLE'>{title}</span>"
        f"<span class='ps_box-value' id='HRS_SCH_WRK_HRS_DESCRLONG'>{place}</span>"
        "<span class='ps_box-value' id='HRS_SCH_WRK_HRS_FULL_PART_TIME'>Full-Time</span>"
        f"{heading}<div id=HRS_SCH_PSTDSC_DESCRLONG$0 class=PSLONGEDITBOX><p>{body}</p></div>"
        "</body></html>"
    )


class _Gateway:
    """Answer one gateway's pages, sending a request without the session cookie to sign in."""

    def __init__(
        self, pages: Mapping[str, str], sets: list[str], *, keeps_session: bool = True
    ) -> None:
        self.pages, self.sets, self.keeps_session = pages, sets, keeps_session
        self.requests: list[tuple[str, str]] = []
        self.forms: list[dict[str, str]] = []

    @staticmethod
    def _unsessioned(method: str, url: str) -> httpx.Response:
        """Set the session cookie and test it on a redirect, then give up on the sign-in page.

        A posting page starts no session, so only a session that searched can read one.
        """
        request = httpx.Request(method, url)
        if "cmd=login" in url:
            return httpx.Response(200, request=request, text="<p>Cookies are required.</p>")
        signin = url.split("?", maxsplit=1)[0] + "?cmd=login&errorPg=ckreq"
        if "Page=HRS_APP_JBPST" in url:
            return httpx.Response(302, request=request, headers={"Location": signin})
        location = signin if url.endswith("&") else url + "&"
        return httpx.Response(
            302,
            request=request,
            headers={"Location": location, "Set-Cookie": f"{_COOKIE}; domain=.acme.example"},
        )

    def _answer(
        self, method: str, url: str, headers: Mapping[str, str], text: str | None
    ) -> httpx.Response:
        self.requests.append((method, url))
        if not self.keeps_session or headers.get("Cookie") != _COOKIE:
            return self._unsessioned(method, url)
        request = httpx.Request(method, url)
        return httpx.Response(404 if text is None else 200, request=request, text=text or "")

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        return self._answer("GET", url, kwargs["headers"], self.pages.get(url.removesuffix("&")))

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.forms.append(dict(kwargs["data"]))
        return self._answer("POST", url, kwargs["headers"], self.sets[len(self.forms) - 1])

    def fetcher(self) -> BoardApiFetcher:
        return BoardApiFetcher(
            client=PoliteHttpClient(
                user_agent="clearscout/test",
                sleeper=lambda _seconds: None,
                jitter=lambda _low, _high: 0.0,
                getter=self.get,
                poster=self.post,
                resolver=public_resolver,
            )
        )


def test_a_fluid_search_reads_the_cumulative_next_set_in_its_guest_session() -> None:
    first = [("113098", "Imaging Technologist", "Civic Campus")]
    every = [*first, ("112959", "Pharmacy Technician", "General Campus")]
    pages = {
        _FLUID_SEARCH: _fluid_grid(first, 2),
        **{
            _FLUID + _posting_query("HRS_APP_JBPST_FL", 2, job): _posting(
                title, place, f"Acme Health hires a {title}.", fluid=True
            )
            for job, title, place in every
        },
    }
    gateway = _Gateway(pages, sets=[_fluid_grid(every, 2)])

    read = gateway.fetcher().listings("Acme Health", _FLUID_SEARCH)

    assert [(item.requisition_id, item.title, item.location) for item in read.listings] == [
        ("113098", "Imaging Technologist", "Civic Campus"),
        ("112959", "Pharmacy Technician", "General Campus"),
    ]
    assert read.listings[1].url == _FLUID + _posting_query("HRS_APP_JBPST_FL", 2, "112959")
    assert "About Acme Health" in read.listings[1].published_description
    assert "hires a Pharmacy Technician." in read.listings[1].published_description
    assert gateway.forms == [
        {"ICStateNum": "1", "ICAction": "HRS_AGNT_RSLT_I$hdown$0", "ICSID": "s1d="}
    ]
    assert read.complete is True


def test_a_classic_portal_reads_its_content_frame_on_the_gateway_host() -> None:
    sets = (
        [("79142", "Research Coordinator", "Central Campus")],
        [("79312", "Lab Technician", "Downtown Campus")],
    )
    frame_search = f"{_FRAME}?Page=HRS_APP_SCHJOB&Action=U&FOCUS=Applicant&SiteId=1001"
    portal = (
        '<iframe id="ptifrmtgtframe" name="TargetContent" '
        f'src="{html.escape(frame_search)}&amp;PortalContentProvider=HRMS"></iframe>'
    )
    pages = {
        _PORTAL_SEARCH: portal,
        frame_search + "&PortalContentProvider=HRMS": _classic_grid(sets[0], 2, last=False),
        **{
            _FRAME + _posting_query("HRS_APP_JBPST", 1001, job): _posting(
                title, place, f"Acme University hires a {title}.", fluid=False
            )
            for job, title, place in (*sets[0], *sets[1])
        },
    }
    gateway = _Gateway(pages, sets=[_classic_grid(sets[1], 2, last=True)])

    read = gateway.fetcher().listings("Acme University", _PORTAL_SEARCH)

    assert [(item.requisition_id, item.title, item.location) for item in read.listings] == [
        ("79142", "Research Coordinator", "Central Campus"),
        ("79312", "Lab Technician", "Downtown Campus"),
    ]
    assert read.listings[0].url == _PORTAL + _posting_query("HRS_APP_JBPST", 1001, "79142")
    assert ("POST", _FRAME) in gateway.requests
    assert ("GET", _FRAME + _posting_query("HRS_APP_JBPST", 1001, "79312")) in gateway.requests
    assert read.complete is True


def test_a_gateway_that_never_keeps_the_session_refuses_the_board() -> None:
    gateway = _Gateway({_FLUID_SEARCH: _fluid_grid([], 0)}, sets=[], keeps_session=False)

    with pytest.raises(ValueError, match="did not answer its job search form"):
        gateway.fetcher().listings("Acme Health", _FLUID_SEARCH)


def test_each_site_id_names_its_own_board_and_a_linking_page_names_the_gateway() -> None:
    page = f'<a href="{html.escape(_FLUID_SEARCH)}">Current openings</a>'
    other_site = _FLUID_SEARCH.replace("SiteId=2", "SiteId=5")

    identified = identify_board(page)

    assert identified is not None and identified[0] == "peoplesoft"
    assert board_identity(_FLUID_SEARCH) == (
        "peoplesoft",
        "careers.acme.example/hrprd/employee/hrms/hrs_hram_fl.hrs_cg_search_fl/2",
    )
    assert board_identity(other_site) != board_identity(_FLUID_SEARCH)
    assert board_identity(_FLUID + _posting_query("HRS_APP_JBPST_FL", 2, "113098")) == (
        board_identity(_FLUID_SEARCH)
    )


def _classic_pages(*postings: tuple[str, str, str]) -> dict[str, str]:
    """Answer the classic portal, its framed one-set grid, and each named posting's frame page."""
    frame_search = f"{_FRAME}?Page=HRS_APP_SCHJOB&Action=U&FOCUS=Applicant&SiteId=1001"
    portal = (
        '<iframe id="ptifrmtgtframe" name="TargetContent" '
        f'src="{html.escape(frame_search)}&amp;PortalContentProvider=HRMS"></iframe>'
    )
    rows = [("79142", "Research Coordinator", "Central Campus")]
    rows += [("79312", "Lab Technician", "Downtown Campus")]
    return {
        _PORTAL_SEARCH: portal,
        frame_search + "&PortalContentProvider=HRMS": _classic_grid(rows, 2, last=True),
        **{
            _FRAME + _posting_query("HRS_APP_JBPST", 1001, job): _posting(
                title, place, f"Acme University hires a {title}.", fluid=False
            )
            for job, title, place in postings
        },
    }


def test_a_resumed_posting_read_opens_the_guest_session_before_the_posting() -> None:
    coordinator = ("79142", "Research Coordinator", "Central Campus")
    technician = ("79312", "Lab Technician", "Downtown Campus")
    first = _Gateway(_classic_pages(coordinator), sets=[])
    read = first.fetcher().listings("Acme University", _PORTAL_SEARCH)
    (owed,) = [
        target
        for target in read.deferred_targets
        if target.deferred_reason == "peoplesoft_search_detail"
    ]
    resumed = _Gateway(_classic_pages(coordinator, technician), sets=[])

    continued = resumed.fetcher().continue_listings(
        "Acme University", (owed,), root_url=_PORTAL_SEARCH, handoff_scopes=()
    )

    posting = _FRAME + _posting_query("HRS_APP_JBPST", 1001, "79312")
    assert owed.url == _PORTAL + _posting_query("HRS_APP_JBPST", 1001, "79312")
    assert [(item.requisition_id, item.title) for item in continued.listings] == [
        ("79312", "Lab Technician")
    ]
    assert "hires a Lab Technician." in continued.listings[0].published_description
    sessioned = [url for _method, url in resumed.requests if not url.endswith("&")]
    assert sessioned[0].startswith(_PORTAL_SEARCH)
    assert sessioned[-1] == posting


def test_a_resumed_grid_set_replays_the_walk_from_the_first_set() -> None:
    sets = [
        [("113098", "Imaging Technologist", "Civic Campus")],
        [("112959", "Pharmacy Technician", "General Campus")],
        [("112801", "Unit Clerk", "Riverside Campus")],
    ]
    walked = [sets[0], sets[0] + sets[1], sets[0] + sets[1] + sets[2]]
    gateway = _Gateway(
        {_FLUID_SEARCH: _fluid_grid(walked[0], 3)},
        sets=[_fluid_grid(walked[1], 3), _fluid_grid(walked[2], 3)],
    )
    match = host_match(PEOPLESOFT_BOARD, _FLUID_SEARCH)
    assert match is not None
    session = _GridSession(gateway.fetcher(), _adapter_groups(match))

    page = session.request(3)

    assert [row["id"] for row in page.rows] == ["112801"]
    assert page.next_page is None
    assert [method for method, url in gateway.requests if not url.endswith("&")] == [
        "GET",
        "POST",
        "POST",
    ]
    assert [form["ICAction"] for form in gateway.forms] == ["HRS_AGNT_RSLT_I$hdown$0"] * 2


def test_a_grid_set_past_the_bounded_walk_makes_no_request() -> None:
    gateway = _Gateway({_FLUID_SEARCH: _fluid_grid([], 0)}, sets=[])
    match = host_match(PEOPLESOFT_BOARD, _FLUID_SEARCH)
    assert match is not None
    session = _GridSession(gateway.fetcher(), _adapter_groups(match))

    with pytest.raises(ValueError, match="outside the bounded walk"):
        session.request(_MAX_SETS + 1)

    assert gateway.requests == []


def test_a_portal_framing_a_page_outside_a_candidate_gateway_refuses_the_board() -> None:
    elsewhere = "https://cdn.acme.example/landing/index.html"
    portal = f'<iframe id="ptifrmtgtframe" name="TargetContent" src="{elsewhere}"></iframe>'
    gateway = _Gateway({_PORTAL_SEARCH: portal}, sets=[])

    with pytest.raises(ValueError, match="outside a Candidate Gateway"):
        gateway.fetcher().listings("Acme University", _PORTAL_SEARCH)

    assert all(url != elsewhere for _method, url in gateway.requests)


def test_a_next_set_postback_redirected_to_a_protected_posting_is_not_followed() -> None:
    protected = "https://careers.acme.example/jobs/112959"
    first = [("113098", "Imaging Technologist", "Civic Campus")]

    class _Redirecting(_Gateway):
        def post(self, url: str, **kwargs: Any) -> httpx.Response:
            self.forms.append(dict(kwargs["data"]))
            self.requests.append(("POST", url))
            request = httpx.Request("POST", url)
            return httpx.Response(302, request=request, headers={"Location": protected})

    gateway = _Redirecting(
        {
            _FLUID_SEARCH: _fluid_grid(first, 2),
            protected: _posting("Pharmacy Technician", "General Campus", "Body.", fluid=True),
        },
        sets=[],
    )

    gateway.fetcher().listings(
        "Acme Health",
        _FLUID_SEARCH,
        options=SourceReadOptions(protected_posting_urls=frozenset({protected})),
    )

    assert ("POST", _FLUID) in gateway.requests
    assert all(url.removesuffix("&") != protected for _method, url in gateway.requests)


def test_protecting_one_gateway_posting_leaves_its_search_and_other_postings_readable() -> None:
    every = [
        ("113098", "Imaging Technologist", "Civic Campus"),
        ("112959", "Pharmacy Technician", "General Campus"),
    ]
    postings = {job: _FLUID + _posting_query("HRS_APP_JBPST_FL", 2, job) for job, _, _ in every}
    pages = {
        _FLUID_SEARCH: _fluid_grid(every, 2),
        **{
            postings[job]: _posting(title, place, f"Acme Health hires a {title}.", fluid=True)
            for job, title, place in every
        },
    }
    gateway = _Gateway(pages, sets=[])

    read = gateway.fetcher().listings(
        "Acme Health",
        _FLUID_SEARCH,
        options=SourceReadOptions(protected_posting_urls=frozenset({postings["112959"]})),
    )

    keys = {job: posting_identity_keys(url) for job, url in postings.items()}
    assert not keys["113098"] & keys["112959"]
    assert not (keys["113098"] | keys["112959"]) & posting_identity_keys(_FLUID_SEARCH)
    assert [(item.requisition_id, item.url) for item in read.listings] == [
        ("113098", postings["113098"])
    ]
    assert "hires a Imaging Technologist." in read.listings[0].published_description
    assert read.protected_posting_urls == (postings["112959"],)
    assert ("GET", postings["113098"]) in gateway.requests
    assert all(url.removesuffix("&") != postings["112959"] for _method, url in gateway.requests)


def test_a_form_postback_without_an_injected_poster_sends_its_form_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[httpx.Request] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

        def build_request(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
            return httpx.Request(method, url, headers=kwargs["headers"], data=kwargs["data"])

        def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
            sent.append(request)
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)
            return httpx.Response(200, request=request, text="<p>Next set</p>")

    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.httpx.Client", Client)
    fetcher = BoardApiFetcher(
        client=PoliteHttpClient(
            user_agent="clearscout/test", sleeper=lambda _seconds: None, resolver=public_resolver
        )
    )
    cookies = httpx.Cookies()
    cookies.set("PORTAL-PSJSESSIONID", "guest42", domain="careers.acme.example")

    answer = fetcher.session_text(_FLUID, cookies, form={"ICAction": "HRS_AGNT_RSLT_I$hdown$0"})

    posted = sent[-1]
    assert answer.endswith("<p>Next set</p>")
    assert posted.method == "POST"
    assert posted.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert posted.content == b"ICAction=HRS_AGNT_RSLT_I%24hdown%240"
    assert posted.headers["Cookie"] == "PORTAL-PSJSESSIONID=guest42"
