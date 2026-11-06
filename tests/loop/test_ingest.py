"""Input detection and the reader for each shape, without touching the network."""

from __future__ import annotations

import contextlib
import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from clearscout.application.discovery.posting_text import closure_signal
from clearscout.domain.records import PostingReadIntent, RobotsReadBasis
from clearscout.domain.staged_facts import FreshnessState
from clearscout.infrastructure.web_fetch.ingest import strip_tags
from clearscout.infrastructure.web_fetch.ingest.inputs import ingest, looks_like_url
from clearscout.infrastructure.web_fetch.ingest.url import (
    PublisherPostingUnverifiedError,
    check_posting,
    from_html,
    from_url,
    shared_posting_feeds,
    workday_posting_detail,
)
from clearscout.infrastructure.web_fetch.ingest.url.page_text import _extract
from clearscout.infrastructure.web_fetch.ingest.url.platform_readers import _greenhouse, _workable
from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
from clearscout.shared.errors import IngestError
from clearscout.shared.layout import plain_layout
from tests.support.application_forms import (
    US_STATE_OPTIONS,
    ashby_form,
    lever_apply_page,
    lever_cards,
)
from tests.support.http import (
    fake_httpx_client,
    policy_client,
    public_resolver,
    recorded_web_clients,
)
from tests.support.suite import FIXTURES

LONG = "A detailed job posting. " * 40


def json_response(url: str, payload: object, status_code: int = 200) -> httpx.Response:
    """Build a response with the request metadata the production client expects."""
    return httpx.Response(status_code, request=httpx.Request("GET", url), json=payload)


def text_response(url: str, text: str, status_code: int = 200) -> httpx.Response:
    """Build a text response with an explicit final URL."""
    return httpx.Response(status_code, request=httpx.Request("GET", url), text=text)


def test_url_detection() -> None:
    assert looks_like_url("https://example.com/jobs/1")
    assert not looks_like_url("posting.pdf")
    assert not looks_like_url("./posting.png")


def test_a_text_file_is_read_directly(tmp_path: Path) -> None:
    path = tmp_path / "posting.txt"
    path.write_text(LONG, encoding="utf-8")
    assert ingest(str(path)).method == "plain text"


def test_a_short_extraction_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "posting.txt"
    path.write_text("Senior Engineer", encoding="utf-8")
    with pytest.raises(IngestError, match="too little to tailor"):
        ingest(str(path))


def test_a_bounded_publisher_preview_keeps_every_section_without_structured_data() -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")

    result = from_html("https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page)

    assert result.title == "Operations Lead"
    assert result.employer == ""
    assert result.publisher_markup_present is False
    assert result.method == "page extraction"
    assert "Description" in result.text
    assert "Requirements" in result.text
    assert "Salary Description" in result.text
    assert "Compensation is discussed during the interview process." in result.text
    assert "Maintain the operating procedures used for project handoffs." in result.text
    assert "Clear written communication and practical problem solving." in result.text
    assert "View All Jobs" not in result.text
    assert "Apply" not in result.text
    assert "Privacy policy" not in result.text
    assert result.source is not None
    assert result.source.page_html == page
    assert 'class="job-preview-details"' in result.source.description_markup
    assert result.source.description_paths == ("div.job-preview-details",)


def test_a_publisher_preview_with_combined_description_keeps_requirements_and_compensation() -> (
    None
):
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")
    page = page.replace(
        '<div class="job-listing-header">Requirements</div>', "<p>Required background</p>"
    )

    result = from_html("https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page)

    assert result.title == "Operations Lead"
    assert "Salary Description" in result.text
    assert "Compensation is discussed during the interview process." in result.text
    assert "Maintain the operating procedures used for project handoffs." in result.text
    assert "Clear written communication and practical problem solving." in result.text
    assert "View All Jobs" not in result.text
    assert "Apply" not in result.text
    assert result.source is not None
    assert result.source.page_html == page
    assert "Required background" in result.source.description_markup


def test_a_publisher_preview_keeps_usable_structured_data_as_its_preferred_body() -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")
    declared = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Operations Director",
            "description": f"<p>{LONG}</p>",
            "hiringOrganization": {"name": "Globex"},
        }
    )
    page = page.replace("</head>", f'<script type="application/ld+json">{declared}</script></head>')

    result = from_html("https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page)

    assert result.method == "publisher markup"
    assert result.title == "Operations Director"
    assert result.employer == "Globex"
    assert result.publisher_markup_present is True
    assert "A detailed job posting." in result.text
    assert "Compensation is discussed" not in result.text
    assert result.source is not None
    assert result.source.page_html == page
    assert result.source.description_markup == f"<p>{LONG}</p>"


def test_an_already_fetched_short_closure_preserves_the_publisher_notice() -> None:
    page = "<html><body><main><p>This job is no longer available.</p></main></body></html>"

    with pytest.raises(IngestError, match="This job is no longer available") as error:
        from_html("https://example.test/jobs/closed", page)

    assert closure_signal(str(error.value))


def test_an_unknown_extension_lists_what_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "posting.docx"
    path.write_bytes(b"nope")
    with pytest.raises(IngestError, match=r"\.pdf"):
        ingest(str(path))


def test_a_nonexistent_argument_says_so(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="neither a http"):
        ingest(str(tmp_path / "absent.pdf"))


def test_linkedin_fetch_is_refused_by_its_published_policy() -> None:
    requested: list[str] = []

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text="User-agent: *\nDisallow: /\n",
            )
        raise AssertionError(f"Policy allowed an unexpected request to {url}")

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(IngestError, match="robots.txt disallows fetching"):
        from_url("https://www.linkedin.com/jobs/view/123456", client=client)

    assert requested == ["https://www.linkedin.com/robots.txt"]


def test_greenhouse_is_read_from_its_public_api() -> None:
    payload = {
        "title": "Staff Data Engineer",
        "location": {"name": "Remote"},
        "content": "<p>" + LONG + "</p>",
    }
    client = policy_client(lambda url: json_response(url, payload))
    result = from_url("https://boards.greenhouse.io/globex/jobs/4567", client=client)
    assert result.method == "Greenhouse API"
    assert "Staff Data Engineer" in result.text
    assert "<p>" not in result.text


def _greenhouse_with_a_disallowed_api(requested: list[str]) -> PoliteHttpClient:
    """Serve a Greenhouse board whose API host disallows every automated read."""

    def get(url: str, **kwargs: object) -> httpx.Response:
        requested.append(url)
        if url.endswith("/robots.txt"):
            disallowed = "boards-api.greenhouse.io" in url
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text="User-agent: *\nDisallow: /\n" if disallowed else "User-agent: *\nAllow: /\n",
            )
        if "boards-api.greenhouse.io" in url:
            return json_response(
                url,
                {
                    "title": "Staff Data Engineer",
                    "location": {"name": "Remote"},
                    "content": f"<p>{LONG}</p>",
                },
            )
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            text=(
                "<html><body><main><h1>Staff Data Engineer</h1><p>" + LONG + "</p></main></body>"
                "</html>"
            ),
        )

    return PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )


def test_a_known_posting_read_reaches_a_publisher_api_its_robots_disallows() -> None:
    """Reading a posting the person already holds is their own read, not a crawl."""
    requested: list[str] = []

    result = from_url(
        "https://boards.greenhouse.io/globex/jobs/4567",
        client=_greenhouse_with_a_disallowed_api(requested),
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert result.method == "Greenhouse API"
    assert "https://boards-api.greenhouse.io/v1/boards/globex/jobs/4567?questions=true" in requested


def test_a_crawl_leaves_a_robots_disallowed_publisher_api_unread() -> None:
    """A crawl carries no exemption, so the same API host answers nothing."""
    requested: list[str] = []

    result = from_url(
        "https://boards.greenhouse.io/globex/jobs/4567",
        client=_greenhouse_with_a_disallowed_api(requested),
    )

    assert result.method != "Greenhouse API"
    assert not any(
        "boards-api.greenhouse.io" in url and not url.endswith("/robots.txt") for url in requested
    )


def test_greenhouse_preflight_preserves_publisher_metadata_without_a_provider() -> None:
    payload = {
        "title": "Staff Data Engineer",
        "company_name": "Globex Health",
        "location": {"name": "2 Locations"},
        "offices": [{"name": "Remote, Canada"}, {"name": "Remote, USA"}],
        "content": "<p>Published description.</p>",
    }

    checked = check_posting(
        "https://boards.greenhouse.io/globex/jobs/4567",
        client=policy_client(lambda url: json_response(url, payload)),
    )

    assert checked.status == "readable"
    assert checked.title == "Staff Data Engineer"
    assert checked.employer == "Globex Health"
    assert checked.location == "Remote, Canada"
    assert checked.facts.locations.values == ("Remote, Canada", "Remote, USA")
    assert checked.facts.locations.complete is True


def test_a_recorded_board_payload_is_read_through_the_real_http_client() -> None:
    """A raw, forward-compatible board response reaches the production parser."""
    fixture = Path(__file__).parents[1] / "fixtures" / "board_responses" / "greenhouse_job.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    result = from_url(
        "https://boards.greenhouse.io/globex/jobs/9001",
        client=policy_client(lambda url: json_response(url, payload)),
    )

    assert result.method == "Greenhouse API"
    assert "Knowledge & Information" in result.text
    assert "Kafka and PostgreSQL" in result.text
    assert "unknown_future_field" not in result.text


def test_a_recorded_board_page_is_extracted_through_the_real_http_client() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "board_responses" / "greenhouse_page.html"
    body = fixture.read_text(encoding="utf-8")
    result = from_url(
        "https://example.test/careers/platform-engineer",
        client=policy_client(lambda url: text_response(url, body)),
    )

    assert result.method == "publisher markup"
    assert "Cookie settings" not in result.text
    assert "Kafka" in result.text


def test_greenhouse_hosted_careers_page_is_read_through_its_embedded_board_token() -> None:
    """A careers page carrying a Greenhouse widget resolves through the JSON API.

    The employer's own careers URL passes the posting id in ``gh_jid`` and names
    the board token inside the page; extracting the rendered HTML would return
    site chrome instead of the posting.
    """
    embed_page = (
        "<html><body>"
        '<div data-board-token="example-org"></div>'
        "<p>Careers site chrome and no posting body here.</p>"
        "</body></html>"
    )
    api_payload = {
        "title": "Staff Data Engineer",
        "location": {"name": "Remote"},
        "content": "<p>" + LONG + "</p>",
    }

    def handler(url: str) -> httpx.Response:
        if "boards-api.greenhouse.io" in url:
            return json_response(url, api_payload)
        return text_response(url, embed_page)

    result = from_url(
        "https://careers.example.test/roles/open?gh_jid=1234567890",
        client=policy_client(handler),
    )
    assert result.method == "Greenhouse API"
    assert "Staff Data Engineer" in result.text
    assert "site chrome" not in result.text


def test_greenhouse_hosted_careers_page_records_an_http_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hosted Greenhouse page refusal remains visible to the URL reader."""
    caplog.set_level("DEBUG", logger="clearscout")
    answer = _greenhouse(
        "https://careers.example.test/roles/open?gh_jid=1234567890",
        client=policy_client(lambda url: text_response(url, "", status_code=404)),
    )

    assert answer.text is None
    assert answer.note == "hosted careers page returned HTTP 404"
    assert "Greenhouse hosted careers page returned HTTP 404" in caplog.text


def test_greenhouse_hosted_careers_page_records_a_robots_refusal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A robots refusal remains a typed Greenhouse result for the fallback path."""
    caplog.set_level("DEBUG", logger="clearscout")

    def unexpected_request(url: str) -> httpx.Response:
        raise AssertionError(f"unexpected request: {url}")

    answer = _greenhouse(
        "https://careers.example.test/roles/open?gh_jid=1234567890",
        client=policy_client(
            unexpected_request,
            robots="User-agent: *\nDisallow: /\n",
        ),
    )

    assert answer.text is None
    assert "robots.txt disallows fetching" in answer.note
    assert "Greenhouse hosted careers page could not be read" in caplog.text


def test_greenhouse_hosted_careers_page_records_a_missing_board_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A page without a board token remains a typed Greenhouse refusal."""
    caplog.set_level("DEBUG", logger="clearscout")
    answer = _greenhouse(
        "https://careers.example.test/roles/open?gh_jid=1234567890",
        client=policy_client(lambda url: text_response(url, "<html><body>Careers</body></html>")),
    )

    assert answer.text is None
    assert answer.note == "hosted careers page did not identify a board"
    assert "Greenhouse hosted careers page has no board token" in caplog.text


def test_greenhouse_hosted_page_without_a_board_token_cannot_become_generic_posting_text() -> None:
    """An identified hosted board must not fall through to generic careers copy."""
    generic_careers_page = "<html><body><p>" + LONG + "</p></body></html>"

    with pytest.raises(PublisherPostingUnverifiedError, match="could not verify"):
        from_url(
            "https://careers.example.test/roles/open?gh_jid=1234567890",
            client=policy_client(lambda url: text_response(url, generic_careers_page)),
        )


_HOSTED_JOB = "4012345"
_HOSTED_SHELL = (
    "<html><body><div id='root'></div><script src='/assets/app.js'></script></body></html>"
)


_HOSTED_URL = f"https://jobboard.example/job-board/globex/job/{_HOSTED_JOB}?gh_jid={_HOSTED_JOB}"
_GLOBEX_JOB = f"/boards/globex/jobs/{_HOSTED_JOB}"


def _hosted_board(
    answers: dict[str, object], requested: list[str], *, page: str = _HOSTED_SHELL
) -> PoliteHttpClient:
    """Serve a hosted careers page, and each Greenhouse API answer at its address fragment."""

    def handler(target: str) -> httpx.Response:
        requested.append(target)
        if "boards-api.greenhouse.io" not in target:
            return text_response(target, page)
        for fragment, answer in answers.items():
            if fragment in target:
                return json_response(target, answer)
        return json_response(target, {"status": 404, "error": "Job not found"}, status_code=404)

    return policy_client(handler)


def _hosted_job(**stated: object) -> dict[str, object]:
    """State the single-job answer Greenhouse gives for the hosted posting."""
    return {
        "id": int(_HOSTED_JOB),
        "title": "Staff Data Engineer",
        "location": {"name": "Remote"},
        "content": f"<p>{LONG}</p>",
        **stated,
    }


def test_a_hosted_greenhouse_shell_is_read_through_the_board_its_address_names() -> None:
    """A client-rendered page names no board token, but its own path can."""
    requested: list[str] = []
    result = from_url(_HOSTED_URL, client=_hosted_board({_GLOBEX_JOB: _hosted_job()}, requested))
    assert result.method == "Greenhouse API"
    assert "Staff Data Engineer" in result.text


def test_a_guessed_board_that_does_not_hold_the_job_is_refused() -> None:
    """Greenhouse job ids live on one board, so a wrong token cannot answer for this job."""
    requested: list[str] = []
    answer = _greenhouse(
        f"https://globex.example/job/{_HOSTED_JOB}?gh_jid={_HOSTED_JOB}",
        client=_hosted_board({}, requested),
    )
    assert answer.text is None
    assert answer.note == "hosted careers page did not identify a board"
    assert f"https://boards-api.greenhouse.io/v1{_GLOBEX_JOB}?questions=true" in requested


def test_a_guessed_board_answering_another_job_is_refused() -> None:
    requested: list[str] = []
    answer = _greenhouse(
        _HOSTED_URL, client=_hosted_board({_GLOBEX_JOB: _hosted_job(id=4099999)}, requested)
    )
    assert answer.text is None
    assert answer.note == "hosted careers page did not identify a board"


def test_a_board_token_in_the_page_markup_is_used_before_any_guess() -> None:
    requested: list[str] = []
    page = '<html><body><div data-board-token="acme-health"></div></body></html>'
    answers: dict[str, object] = {f"/boards/acme-health/jobs/{_HOSTED_JOB}": _hosted_job()}
    result = from_url(_HOSTED_URL, client=_hosted_board(answers, requested, page=page))
    assert result.method == "Greenhouse API"
    assert not any("/boards/globex/" in target for target in requested)


def test_a_guessed_board_never_reads_a_hosted_posting_as_closed() -> None:
    """A guessed board omitting the job proves nothing, so its listing is never consulted."""
    requested: list[str] = []
    listing = {"jobs": [{"id": 111, "title": "Registered Nurse"}]}
    checked = check_posting(
        _HOSTED_URL, client=_hosted_board({"/boards/globex/jobs?content": listing}, requested)
    )
    assert checked.status == "unreadable"
    assert not closure_signal(checked.note)


_LISTED_URL = f"https://www.initech.example/careers/job/{_HOSTED_JOB}?gh_jid={_HOSTED_JOB}"
_LISTED_JOB = f"/boards/initech-health/jobs/{_HOSTED_JOB}"


def test_a_hosted_posting_is_read_through_the_greenhouse_board_that_listed_it() -> None:
    """The listing board's token holds the job when neither the page nor the address names it."""
    requested: list[str] = []
    result = from_url(
        _LISTED_URL,
        client=_hosted_board({_LISTED_JOB: _hosted_job()}, requested),
        board_url="https://boards.greenhouse.io/initech-health",
    )
    assert result.method == "Greenhouse API"
    assert "Staff Data Engineer" in result.text


def test_the_page_markups_board_token_wins_over_the_board_that_listed_the_posting() -> None:
    requested: list[str] = []
    page = '<html><body><div data-board-token="acme-health"></div></body></html>'
    answers: dict[str, object] = {
        f"/boards/acme-health/jobs/{_HOSTED_JOB}": _hosted_job(),
        _LISTED_JOB: _hosted_job(),
    }
    result = from_url(
        _LISTED_URL,
        client=_hosted_board(answers, requested, page=page),
        board_url="https://boards.greenhouse.io/initech-health",
    )
    assert result.method == "Greenhouse API"
    assert any(f"/boards/acme-health/jobs/{_HOSTED_JOB}" in target for target in requested)
    assert not any("/boards/initech-health/" in target for target in requested)


@pytest.mark.parametrize(
    "board_url",
    ["", "https://jobs.lever.co/initech-health"],
    ids=["no listing board", "a board on another platform"],
)
def test_a_hosted_posting_whose_listing_board_names_no_greenhouse_token_stays_unverified(
    board_url: str,
) -> None:
    requested: list[str] = []
    with pytest.raises(PublisherPostingUnverifiedError):
        from_url(
            _LISTED_URL,
            client=_hosted_board({_LISTED_JOB: _hosted_job()}, requested),
            board_url=board_url,
        )
    assert not any(_LISTED_JOB in target for target in requested)


def test_lever_is_read_from_its_public_api() -> None:
    payload = {
        "text": "Staff Data Engineer",
        "categories": {"location": "Remote"},
        "descriptionPlain": LONG,
        "lists": [
            {
                "text": "Requirements",
                "content": (
                    "<li>Kafka</li>"
                    "<li>Must be based in a region in the Eastern Standard Time zone.</li>"
                    "<li>Travel up to 25%.</li>"
                ),
            }
        ],
        "additionalPlain": "Acme supports accessible hiring.",
    }
    client = policy_client(lambda url: json_response(url, payload))
    result = from_url(
        "https://jobs.lever.co/globex/"
        + "0" * 8
        + "-"
        + "0" * 4
        + "-"
        + "0" * 4
        + "-"
        + "0" * 4
        + "-"
        + "0" * 12,
        client=client,
    )
    assert result.method == "Lever API"
    assert "Kafka" in result.text
    assert "Must be based in a region in the Eastern Standard Time zone." in result.text
    assert "Travel up to 25%." in result.text
    assert "Acme supports accessible hiring." in result.text


def test_lever_preflight_keeps_arrangement_and_does_not_invent_employer() -> None:
    job_id = "00000000-0000-0000-0000-000000000000"
    payload = {
        "text": "Staff Data Engineer",
        "categories": {
            "location": "2 Locations",
            "allLocations": ["Remote, Canada", "Remote, USA"],
            "commitment": "Full-time",
        },
        "workplaceType": "remote",
        "descriptionPlain": LONG,
    }

    checked = check_posting(
        f"https://jobs.lever.co/globex/{job_id}",
        client=policy_client(lambda url: json_response(url, payload)),
    )

    assert checked.status == "readable"
    assert checked.title == "Staff Data Engineer"
    assert checked.employer == ""
    assert checked.location == "Remote, Canada"
    assert checked.facts.locations.values == ("Remote, Canada", "Remote, USA")
    assert checked.facts.arrangements.values == ("remote",)
    assert checked.facts.employment_types.values == ("Full-time",)


def test_lever_preflight_does_not_hard_gate_conflicting_remote_location() -> None:
    job_id = "00000000-0000-0000-0000-000000000000"
    payload = {
        "text": "Data Architect",
        "categories": {"location": "Remote (Canada)"},
        "workplaceType": "hybrid",
        "descriptionPlain": LONG,
    }

    checked = check_posting(
        f"https://jobs.lever.co/globex/{job_id}",
        client=policy_client(lambda url: json_response(url, payload)),
    )

    assert checked.status == "readable"
    assert checked.facts.arrangements.values == ()
    assert checked.facts.arrangements.unresolved_values[0] == "hybrid"
    assert checked.facts.arrangements.complete is False


def test_ashby_is_read_from_its_public_board_api() -> None:
    job_id = "7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"
    payload = {
        "jobs": [
            {
                "id": job_id,
                "title": "Principal Technical Product Manager, Data Platform",
                "location": "Canada",
                "employmentType": "FullTime",
                "descriptionPlain": LONG,
            }
        ]
    }
    seen: list[str] = []

    def api(target: str) -> httpx.Response:
        seen.append(target)
        return json_response(target, payload)

    result = from_url(f"https://jobs.ashbyhq.com/globex/{job_id}", client=policy_client(api))
    assert result.method == "Ashby API"
    assert "Technical Product Manager" in result.text
    assert seen == ["https://api.ashbyhq.com/posting-api/job-board/globex", _ASHBY_FORM_API]


def test_ashby_reports_a_requisition_missing_from_the_board() -> None:
    job_id = "7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"

    def api(target: str) -> httpx.Response:
        if "posting-api" in target:
            return json_response(target, {"jobs": [{"id": "another-job"}]})
        if target.endswith(f"/globex/{job_id}"):
            return httpx.Response(
                302,
                request=httpx.Request("GET", target),
                headers={"Location": "https://jobs.ashbyhq.com/globex"},
            )
        return text_response(target, f"<p>{LONG}</p>")

    client = policy_client(api)
    with pytest.raises(IngestError) as caught:
        from_url(f"https://jobs.ashbyhq.com/globex/{job_id}", client=client)
    # The authoritative board verdict is raised as a closure the fetcher records,
    # not buried in a declined note behind a generic redirect message.
    assert "so it has closed" in str(caught.value)
    assert closure_signal(str(caught.value))


def test_ashby_full_body_comes_from_description_html() -> None:
    job_id = "7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"
    payload = {
        "jobs": [
            {
                "id": job_id,
                "title": "Principal Technical Product Manager, Data Platform",
                "location": "Canada",
                "employmentType": "FullTime",
                "descriptionPlain": "",
                "descriptionHtml": f"<p>{LONG}</p>",
            }
        ]
    }
    result = from_url(
        f"https://jobs.ashbyhq.com/globex/{job_id}",
        client=policy_client(lambda url: json_response(url, payload)),
    )
    assert result.method == "Ashby API"
    assert LONG.strip() in result.text
    assert "<p>" not in result.text


def test_ashby_preflight_preserves_publisher_metadata_without_a_provider() -> None:
    job_id = "7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"
    payload = {
        "jobs": [
            {
                "id": job_id,
                "title": "Principal Technical Product Manager, Data Platform",
                "location": "Canada",
                "workplaceType": "Remote",
                "employmentType": "FullTime",
                "descriptionHtml": f"<p>{LONG}</p>",
            }
        ]
    }

    checked = check_posting(
        f"https://jobs.ashbyhq.com/globex/{job_id}",
        client=policy_client(lambda url: json_response(url, payload)),
    )

    assert checked.status == "readable"
    assert checked.title == "Principal Technical Product Manager, Data Platform"
    assert checked.location == "Canada"
    assert checked.facts.locations.values == ("Canada",)
    assert checked.facts.arrangements.values == ("Remote",)


_FORM_JOB = "7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"
_ASHBY_FORM_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_STATE_CARDS = lever_cards("Which state do you live in?", US_STATE_OPTIONS)
_STATE_FORM = ashby_form("Which state do you live in?", US_STATE_OPTIONS)


def _lever_api(requested: list[str], *, form_status: int = 200) -> Callable[[str], httpx.Response]:
    payload = {"text": "Staff Data Engineer", "descriptionPlain": LONG}

    def api(target: str) -> httpx.Response:
        requested.append(target)
        if target.endswith("/apply"):
            return text_response(target, lever_apply_page(_STATE_CARDS), status_code=form_status)
        return json_response(target, payload)

    return api


@pytest.mark.parametrize("region", ["", "eu."])
def test_a_lever_body_read_keeps_the_application_form_beside_the_posting(region: str) -> None:
    requested: list[str] = []
    url = f"https://jobs.{region}lever.co/globex/{_FORM_JOB}"

    result = from_url(url, client=policy_client(_lever_api(requested)))

    assert f"{url}/apply" in requested
    assert result.source is not None
    assert json.loads(result.source.application_form) == _STATE_CARDS
    assert "cards" not in json.loads(result.source.record)


def test_a_lever_form_that_does_not_answer_leaves_the_posting_read() -> None:
    requested: list[str] = []

    result = from_url(
        f"https://jobs.lever.co/globex/{_FORM_JOB}",
        client=policy_client(_lever_api(requested, form_status=404)),
    )

    assert "Staff Data Engineer" in result.text
    assert result.source is not None
    assert result.source.application_form == ""


def _ashby_board(target: str) -> httpx.Response:
    job = {"id": _FORM_JOB, "title": "Data Product Manager", "descriptionPlain": LONG}
    return json_response(target, {"jobs": [job]})


def _ashby_form_answer(posted: list[tuple[str, Any]]) -> Callable[[str, Any], httpx.Response]:
    def poster(target: str, body: Any) -> httpx.Response:
        posted.append((target, body))
        return json_response(target, {"data": {"jobPosting": {"applicationForm": _STATE_FORM}}})

    return poster


def test_an_ashby_body_read_asks_the_job_page_query_for_its_form() -> None:
    posted: list[tuple[str, Any]] = []
    client = policy_client(_ashby_board, poster=_ashby_form_answer(posted))

    result = from_url(f"https://jobs.ashbyhq.com/Globex%20Health/{_FORM_JOB}", client=client)

    [(target, body)] = posted
    assert target == _ASHBY_FORM_API
    assert body["variables"] == {
        "organizationHostedJobsPageName": "Globex Health",
        "jobPostingId": _FORM_JOB,
    }
    assert result.source is not None
    assert json.loads(result.source.application_form) == _STATE_FORM


@pytest.mark.parametrize(
    ("intent", "read"),
    [(PostingReadIntent.CRAWL, False), (PostingReadIntent.SINGLE_POSTING, True)],
)
def test_an_ashby_form_its_robots_disallow_is_read_only_for_a_single_posting(
    intent: PostingReadIntent, read: bool
) -> None:
    posted: list[tuple[str, Any]] = []
    client = policy_client(
        _ashby_board, poster=_ashby_form_answer(posted), robots="User-agent: *\nDisallow: /api/\n"
    )

    result = from_url(f"https://jobs.ashbyhq.com/globex/{_FORM_JOB}", client=client, intent=intent)

    assert "Data Product Manager" in result.text
    assert result.source is not None
    assert bool(result.source.application_form) is read
    assert bool(posted) is read


@pytest.mark.parametrize(
    "url",
    [f"https://jobs.lever.co/globex/{_FORM_JOB}", f"https://jobs.ashbyhq.com/globex/{_FORM_JOB}"],
)
def test_a_liveness_check_reads_no_application_form(url: str) -> None:
    requested: list[str] = []
    posted: list[tuple[str, Any]] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return _ashby_board(target) if "ashby" in target else _lever_api([])(target)

    checked = check_posting(url, client=policy_client(api, poster=_ashby_form_answer(posted)))

    assert checked.status == "readable"
    assert not any(target.endswith("/apply") for target in requested)
    assert posted == []


def test_workday_is_read_from_the_endpoint_the_page_itself_calls() -> None:
    """A Workday page renders client-side, so its HTML is an empty shell."""
    asked: list[str] = []

    def api(url: str) -> httpx.Response:
        asked.append(url)
        return json_response(
            url,
            {
                "jobPostingInfo": {
                    "title": "Staff Data Platform Engineer",
                    "location": "Remote, United States",
                    "timeType": "Full time",
                    "jobDescription": f"<p>{LONG}</p>",
                }
            },
        )

    client = policy_client(api)
    result = from_url(
        "https://globex.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R-1234",
        client=client,
    )
    assert asked == [
        "https://globex.wd5.myworkdayjobs.com/wday/cxs/globex/External/job/"
        "Remote/Staff-Engineer_R-1234"
    ], "the tenant, site, and job path were not recovered from the public URL"
    assert "Staff Data Platform Engineer" in result.text
    assert "Remote, United States" in result.text
    assert "<p>" not in result.text, "the description reached the model as markup"


def test_workday_posting_keeps_conflicting_linkedin_source_unresolved() -> None:
    def api(url: str) -> httpx.Response:
        return json_response(
            url,
            {
                "jobPostingInfo": {
                    "title": "Data Product Manager",
                    "jobDescription": "".join(
                        ("#LI-Onsite\nThis role is fully remote within Canada.\n", LONG)
                    ),
                }
            },
        )

    result = check_posting(
        "https://globex.wd5.myworkdayjobs.com/en-US/External/job/Remote/Data-Product-Manager_R-123",
        client=policy_client(api),
    )

    arrangement = result.facts.arrangements
    assert arrangement.values == ()
    assert arrangement.complete is False
    assert "#LI-Onsite" in arrangement.unresolved_values[0]


def test_workday_apply_url_omits_the_application_suffix_from_the_cxs_request() -> None:
    asked: list[str] = []

    def api(url: str) -> httpx.Response:
        asked.append(url)
        return json_response(
            url,
            {
                "jobPostingInfo": {
                    "title": "Platform Planner",
                    "location": "Remote, Canada",
                    "timeType": "Full time",
                    "jobDescription": f"<p>{LONG}</p>",
                }
            },
        )

    result = from_url(
        "https://globex.wd5.myworkdayjobs.com/External/job/Remote/Platform-Planner_R-1234/apply",
        client=policy_client(api),
    )

    assert asked == [
        "https://globex.wd5.myworkdayjobs.com/wday/cxs/globex/External/job/"
        "Remote/Platform-Planner_R-1234"
    ]
    assert "Platform Planner" in result.text


def test_a_workday_record_with_no_posting_in_it_says_so_rather_than_nothing() -> None:
    """The endpoint answers for a closed requisition too, with nothing in it.

    Reported by name, because a refused API and a genuinely closed posting need
    different responses from the person reading the failure.
    """

    def api(url: str) -> httpx.Response:
        if "/wday/cxs/" in url:
            return json_response(url, {"errors": []})
        return text_response(
            "https://globex.wd5.myworkdayjobs.com/en-US/External",
            f"<html><body><p>{LONG}</p></body></html>",
        )

    client = policy_client(api)
    with pytest.raises(IngestError, match="no posting in it") as raised:
        from_url(
            "https://globex.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R-1234",
            client=client,
        )
    assert "Workday API declined" in str(raised.value)


def test_oracle_is_read_from_the_endpoint_the_page_itself_calls() -> None:
    """An Oracle career site renders client-side; its HTML holds three characters."""
    asked: list[str] = []

    def api(url: str) -> httpx.Response:
        asked.append(url)
        return json_response(
            url,
            {
                "items": [
                    {
                        "Title": "Data Platform Lead",
                        "PrimaryLocation": "Springfield, IL, United States",
                        "WorkplaceType": "Hybrid",
                        "ExternalDescriptionStr": f"<p>{LONG}</p>",
                        "ExternalQualificationsStr": "",
                        "ExternalResponsibilitiesStr": "",
                    }
                ]
            },
        )

    client = policy_client(api)
    result = from_url(
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001",
        client=client,
    )
    assert asked == [
        "https://acme.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/"
        'recruitingCEJobRequisitionDetails?expand=all&finder=ById;Id="2020001",siteNumber="CX_4"'
    ], "the host, site, and requisition were not recovered from the public URL"
    assert "Data Platform Lead" in result.text
    assert "Springfield, IL, United States" in result.text
    assert "<p>" not in result.text, "the description reached the model as markup"


def test_an_oracle_detail_states_its_secondary_locations_after_the_primary() -> None:
    job = {
        "Id": "2020001",
        "Title": "Data Platform Lead",
        "PrimaryLocation": "Springfield, IL, United States",
        "ExternalDescriptionStr": f"<p>{LONG}</p>",
        "secondaryLocations": [
            {"Name": "Riverton, ON, Canada", "CountryCode": "CA"},
            {"Name": "Springfield, IL, United States", "CountryCode": "US"},
        ],
    }

    checked = check_posting(
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001",
        client=policy_client(lambda url: json_response(url, {"items": [job]})),
    )

    assert checked.location == "Springfield, IL, United States"
    assert checked.facts.locations.values == (
        "Springfield, IL, United States",
        "Riverton, ON, Canada",
    )


@pytest.mark.parametrize(
    ("host", "site"),
    [
        ("acme.fa.us6.oraclecloud.com", "CX_4"),
        ("globex.fa.us6.oraclecloud.com", "CX_4"),
        ("acme.fa.us6.oraclecloud.com", "CX_5"),
    ],
)
@pytest.mark.parametrize(
    ("company_fields", "company"),
    [
        ({}, None),
        ({"LegalEmployer": None}, None),
        ({"LegalEmployer": "  "}, None),
        ({"LegalEmployer": 1234}, None),
        ({"LegalEmployer": {"Name": "Globex Health Inc."}}, None),
        ({"LegalEmployer": "Globex Health Inc."}, "Globex Health Inc."),
    ],
    ids=["missing", "null", "blank", "numeric", "nested", "published"],
)
def test_oracle_board_uses_registration_when_a_direct_detail_has_no_company(
    host: str, site: str, company_fields: dict[str, Any], company: str | None
) -> None:
    from tests.support.doubles import ScriptedApi

    job = {
        "Id": "2020001",
        "Title": "Data Platform Lead",
        "PrimaryLocation": "Springfield, IL, United States",
        "ExternalDescriptionStr": f"<p>{LONG}</p>",
        "BusinessUnit": 1234,
        "Organization": 5678,
        **company_fields,
    }
    board_url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"
    configured_employer = "Acme"
    board = ScriptedApi({"items": [{"requisitionList": [job], "TotalJobsCount": 1}]})
    listings = board.listings(
        configured_employer, board_url, employer_is_authoritative=True
    ).listings
    checked = check_posting(
        f"{board_url}/job/2020001",
        client=policy_client(lambda url: json_response(url, {"items": [job]})),
    )
    assert len(listings) == 1
    assert checked.status == "readable"
    assert listings[0].employer == (company or configured_employer)
    assert checked.employer == (company or "")
    assert listings[0].facts.employer.provenance == (
        "oracle.employer" if company else "source.registered_employer"
    )
    assert checked.facts.employer.values == ((company,) if company else ())
    assert listings[0].title == checked.title == "Data Platform Lead"
    assert listings[0].location == checked.location == "Springfield, IL, United States"


def test_an_oracle_requisition_absent_from_the_finder_reads_as_closed() -> None:
    """A withdrawn requisition reads as closed from the finder alone.

    The ById finder answers HTTP 200 with empty items while the human page stays a
    client-side shell that never redirects, so the empty finder answer is the only
    signal and it must read as closed.
    """
    posting = (
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020001"
    )

    def api(url: str) -> httpx.Response:
        if "/hcmRestApi/" in url:
            return json_response(url, {"items": []})
        return text_response(posting, "<html><body></body></html>")

    client = policy_client(api)
    with pytest.raises(IngestError) as raised:
        from_url(posting, client=client)
    assert closure_signal(str(raised.value))
    assert "so it has closed" in str(raised.value)


def test_a_failing_api_falls_through_to_extraction() -> None:
    def api(url: str) -> httpx.Response:
        if "boards-api" in url:
            return text_response(url, "forbidden", status_code=403)
        return text_response(url, f"<html><body><p>{LONG}</p></body></html>")

    result = from_url("https://boards.greenhouse.io/globex/jobs/4567", client=policy_client(api))
    assert result.method == "page extraction"


def test_a_posting_that_redirects_to_the_board_index_is_refused() -> None:
    """A board index returned for one posting is refused, not extracted.

    The index is real HTML full of real job titles, so extraction would succeed
    against a document that is not the job.
    """

    def api(url: str) -> httpx.Response:
        if "boards-api" in url:
            return text_response(url, "forbidden", status_code=403)
        if url.endswith("/jobs/8556870002"):
            return httpx.Response(
                302,
                request=httpx.Request("GET", url),
                headers={"Location": "https://job-boards.greenhouse.io/globex"},
            )
        return text_response(
            url,
            f"<html><body><h1>Current openings at Globex</h1><p>{LONG}</p></body></html>",
        )

    client = policy_client(api)
    with pytest.raises(IngestError) as caught:
        from_url("https://job-boards.greenhouse.io/globex/jobs/8556870002", client=client)
    message = str(caught.value)
    assert "job board rather than a posting" in message
    # The board API is why the page was fetched at all, so its refusal is
    # reported too: without it a refused API and a closed posting read alike.
    assert "Greenhouse API declined: single posting returned HTTP 403" in message
    assert "board listing returned HTTP 403" in message


def test_a_redirect_into_a_disallowed_index_still_reads_as_a_closed_posting() -> None:
    """A robots refusal on the index must not hide why the posting went away.

    Boards answer a closed posting by redirecting to the index, and that index is
    often the path their robots.txt disallows. Reporting the refusal alone loses
    the one signal that says the job closed.
    """

    def get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                text="User-agent: *\nAllow: /globex/jobs/\nDisallow: /globex\n",
            )
        if "boards-api" in url:
            return text_response(url, "forbidden", status_code=403)
        return httpx.Response(
            302,
            request=httpx.Request("GET", url),
            headers={"Location": "https://job-boards.greenhouse.io/globex"},
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=get,
        resolver=public_resolver,
    )

    with pytest.raises(IngestError) as caught:
        from_url("https://job-boards.greenhouse.io/globex/jobs/8556870002", client=client)

    message = str(caught.value)
    assert "job board rather than a posting" in message
    assert "posting may have closed" in message


def test_a_board_with_no_api_reports_nothing_about_apis() -> None:
    """A plain careers page never consulted an API, so it has no reason to give."""
    result = from_url(
        "https://example.com/careers/staff-engineer",
        client=policy_client(
            lambda url: text_response(url, f"<html><body><p>{LONG}</p></body></html>")
        ),
    )
    assert result.method == "page extraction"


def test_a_retryable_status_is_retried_once_and_then_accepted() -> None:
    """A transient refusal such as 429 does not survive one retry."""
    calls: list[str] = []

    def flaky(target: str, **kwargs: object) -> httpx.Response:
        if target.endswith("/robots.txt"):
            return httpx.Response(404, request=httpx.Request("GET", target))
        calls.append(target)
        status = 429 if len(calls) == 1 else 200
        return httpx.Response(
            status,
            request=httpx.Request("GET", target),
            json={"title": "Staff Data Engineer", "content": "<p>" + LONG},
        )

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=flaky,
        resolver=public_resolver,
    )
    result = from_url("https://boards.greenhouse.io/globex/jobs/4567", client=client)
    assert len(calls) == 2
    assert result.method == "Greenhouse API"


def test_the_pause_between_a_refusal_and_its_retry_is_injectable() -> None:
    """Sleeping is not this function's subject, so a caller can say what waiting is."""
    from clearscout.infrastructure.web_fetch.ingest.url.transport import (
        RETRY_PAUSE_SECONDS,
        http_get,
    )

    def refused(target: str, **kwargs: object) -> httpx.Response:
        status = 404 if target.endswith("/robots.txt") else 429
        return httpx.Response(status, request=httpx.Request("GET", target))

    waited: list[float] = []
    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=refused,
        resolver=public_resolver,
    )
    http_get("https://example.test/jobs/1", {}, wait=waited.append, client=client)
    assert waited == [RETRY_PAUSE_SECONDS]


def test_a_persistent_refusal_is_not_retried_forever() -> None:
    """One retry, not a loop. A tool that hammers a board is a scraper."""
    calls: list[str] = []

    def refused(target: str, **kwargs: object) -> httpx.Response:
        if target.endswith("/robots.txt"):
            return httpx.Response(404, request=httpx.Request("GET", target))
        calls.append(target)
        return httpx.Response(429, request=httpx.Request("GET", target))

    client = PoliteHttpClient(
        user_agent="clearscout/test",
        sleeper=lambda seconds: None,
        getter=refused,
        resolver=public_resolver,
    )
    with pytest.raises(IngestError, match="returned HTTP 429"):
        from_url("https://boards.greenhouse.io/globex/jobs/4567", client=client)
    assert len(calls) == len(set(calls)) * 2, "each endpoint should be tried exactly twice"


def test_clearscout_says_who_it_is() -> None:
    """Every request names clearscout as its user agent rather than posing as a browser."""
    from clearscout.infrastructure.web_fetch.ingest.url import JSON_HEADERS, PAGE_HEADERS

    for headers in (PAGE_HEADERS, JSON_HEADERS):
        assert headers["User-Agent"].startswith("clearscout/")
        assert "Mozilla" not in headers["User-Agent"]
    assert PAGE_HEADERS["Accept"].startswith("text/html")
    assert JSON_HEADERS["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("read", "url"),
    [
        (check_posting, "https://careers.example.test/jobs/1"),
        (workday_posting_detail, "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Lead_R-1"),
        (from_url, "https://careers.example.test/jobs/1"),
        (ingest, "https://careers.example.test/jobs/1"),
    ],
)
def test_a_read_without_a_client_closes_the_one_it_builds(
    monkeypatch: pytest.MonkeyPatch, read: Callable[[str], object], url: str
) -> None:
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy.httpx.Client", fake_httpx_client()
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.policy._resolve_addresses", public_resolver
    )
    monkeypatch.setattr("clearscout.infrastructure.web_fetch.policy.time.sleep", lambda _s: None)
    built, closed = recorded_web_clients(monkeypatch)

    with contextlib.suppress(IngestError):
        read(url)

    assert built
    assert {id(client) for client in built} <= {id(client) for client in closed}


def greenhouse_job(job_id: str) -> dict[str, object]:
    """Return one Greenhouse job record shaped the way the board API returns it."""
    return {
        "id": int(job_id),
        "title": "Senior Data Engineer, Knowledge & Information",
        "location": {"name": "United States"},
        "content": "<p>" + LONG + "</p>",
    }


def test_a_posting_the_single_endpoint_denies_is_read_from_the_board_listing() -> None:
    """The single-posting endpoint can 404 while the board listing holds the posting.

    Both endpoints are the same public API. Trusting the 404 would fall through
    to the rendered page, which redirects to the board index.
    """
    seen: list[str] = []

    def api(target: str) -> httpx.Response:
        seen.append(target)
        if target.partition("?")[0].endswith("/jobs/8556870002"):
            return text_response(target, "missing", status_code=404)
        return json_response(
            target, {"jobs": [greenhouse_job("8556870002"), greenhouse_job("1111111111")]}
        )

    result = from_url(
        "https://job-boards.greenhouse.io/initech/jobs/8556870002", client=policy_client(api)
    )
    assert result.method == "Greenhouse API"
    assert "Knowledge & Information" in result.text
    assert seen[1].endswith("/jobs?content=true")


def test_a_posting_absent_from_its_own_board_is_reported_as_closed() -> None:
    """Absent from a board that lists other jobs is the one honest "it has closed"."""

    def api(target: str) -> httpx.Response:
        if "/jobs/" in target and "boards-api" in target:
            return text_response(target, "missing", status_code=404)
        if "boards-api" in target:
            return json_response(target, {"jobs": [greenhouse_job("1111111111")]})
        if target.endswith("/jobs/8556870002"):
            return httpx.Response(
                302,
                request=httpx.Request("GET", target),
                headers={"Location": "https://job-boards.greenhouse.io/initech?error=true"},
            )
        return text_response(target, f"<html><body><p>{LONG}</p></body></html>")

    with pytest.raises(IngestError) as caught:
        from_url(
            "https://job-boards.greenhouse.io/initech/jobs/8556870002", client=policy_client(api)
        )
    message = str(caught.value)
    assert "holds 1 open jobs and none of them is 8556870002, so it has closed" in message


def test_the_listing_is_not_fetched_when_the_single_posting_answers() -> None:
    """The cheap request stays the common path; the listing is a fallback only."""
    seen: list[str] = []

    def api(target: str) -> httpx.Response:
        seen.append(target)
        return json_response(target, greenhouse_job("4567"))

    from_url("https://boards.greenhouse.io/globex/jobs/4567", client=policy_client(api))
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/globex/jobs/4567?questions=true"]


def test_an_ordinary_redirect_within_the_same_posting_is_allowed() -> None:
    """Canonical-host and scheme redirects are normal and must not be blocked."""

    def api(url: str) -> httpx.Response:
        if "boards-api" in url:
            return text_response(url, "forbidden", status_code=403)
        return text_response(url, f"<html><body><p>{LONG}</p></body></html>")

    result = from_url(
        "http://job-boards.greenhouse.io/initech/jobs/8556870002", client=policy_client(api)
    )
    assert result.method == "page extraction"


def test_tag_stripping_drops_scripts_and_unescapes_entities() -> None:
    text = strip_tags("<script>bad()</script><p>Kafka &amp; Terraform</p>")
    assert "bad()" not in text
    assert "Kafka & Terraform" in text


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<ul><li><span>Alpha</span></li><li><span>Beta</span></li></ul>", "- Alpha\n- Beta"),
        ("<p>Alpha<b>x</b><br>Beta</p>", "Alpha x\nBeta"),
        ("<table><tr><td>A</td></tr><tr><td>B</td></tr></table>", "A\nB"),
    ],
)
def test_tag_stripping_keeps_a_break_an_inline_element_sits_inside(
    markup: str, expected: str
) -> None:
    """Real markup nests a span inside the item or cell that states the break."""
    assert strip_tags(markup) == expected


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<p>Intro.</p><ul><li>Alpha</li><li>Beta</li></ul>", "Intro.\n\n- Alpha\n- Beta"),
        (
            "<ol><li>First</li><li>Second</li></ol><ol><li>Again</li></ol>",
            "1. First\n2. Second\n\n1. Again",
        ),
        ("<ul><li><p>Alpha</p></li><li><p>Beta</p></li></ul>", "- Alpha\n- Beta"),
        (
            "<p><strong>Qualifications</strong></p><ul><li>SQL</li></ul>",
            "Qualifications\n\n- SQL",
        ),
        ("<h3>Benefits</h3><p>Dental.</p><p>Vision.</p>", "Benefits\n\nDental.\n\nVision."),
        (
            "<ul><li>Parent<ul><li>Child</li></ul></li><li>Next</li></ul>",
            "- Parent\n- Child\n- Next",
        ),
        (
            "<ol><li>Ordered<ul><li>Inner</li></ul></li><li>Back</li></ol>",
            "1. Ordered\n- Inner\n2. Back",
        ),
        ("<ul><li>Unclosed<li>Items</ul><p>After.</p>", "- Unclosed\n- Items\n\nAfter."),
    ],
)
def test_tag_stripping_writes_lists_and_headings_in_one_plain_shape(
    markup: str, expected: str
) -> None:
    """Every board's markup reduces to marked items, heading lines, and blank-line paragraphs."""
    assert strip_tags(markup) == expected


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (
            "<table><tr><th>Skills</th></tr><tr><td>SQL</td></tr><tr><td>Kafka</td></tr></table>",
            "Skills\n\n- SQL\n- Kafka",
        ),
        ("<dl><dt>Skills</dt><dd>SQL</dd><dd>Kafka</dd></dl>", "Skills\n\n- SQL\n- Kafka"),
        ("<p><b>Skills</b></p><p>SQL</p><p>Kafka</p>", "Skills\n\n- SQL\n- Kafka"),
        ("<div><b>Skills</b></div><div>SQL</div><div>Kafka</div>", "Skills\n\n- SQL\n- Kafka"),
        (f"<p>{chr(0x2022)} SQL</p><p>{chr(0x2022)} Kafka</p>", "- SQL\n- Kafka"),
        ("<![CDATA[<p>Alpha</p><p>Beta</p>]]>", "Alpha\n\nBeta"),
        ("&lt;p&gt;Alpha&lt;/p&gt;&lt;p&gt;Beta&lt;/p&gt;", "Alpha\n\nBeta"),
    ],
)
def test_tag_stripping_marks_the_items_markup_states_without_a_list(
    markup: str, expected: str
) -> None:
    """Rows, terms, and paragraphs standing under a heading element read as its items."""
    assert strip_tags(markup) == expected


@pytest.mark.parametrize(
    "markup",
    [
        "<p><b>About us</b></p><p>We plan trains.</p><p>We run depots.</p>",
        "<p><b>About us</b></p><p>We plan trains. We run depots</p><p>Join us</p>",
        "<table><tr><td>Location</td><td>Remote</td></tr><tr><td>Team</td><td>Data</td></tr></table>",
        "<p><b>Skills</b></p><p>SQL</p>",
    ],
)
def test_tag_stripping_leaves_paragraphs_under_a_heading_as_prose(markup: str) -> None:
    """A heading over sentences, a table of pairs, or a single line is not a list."""
    assert "- " not in strip_tags(markup)


# Each text-only entry point hands the reader the same layout a board's markup gets.
GLYPH_TEXT = f"{LONG}\r\n\r\nRequirements\r\n{chr(0xF0B7)} Five years\r\n{chr(0xB7)}Fluency in SQL"
GLYPH_LAYOUT = f"{LONG.strip()}\n\nRequirements\n- Five years\n- Fluency in SQL"


def test_a_text_file_reaches_the_canonical_layout(tmp_path: Path) -> None:
    path = tmp_path / "posting.txt"
    path.write_bytes(GLYPH_TEXT.encode("utf-8"))
    assert ingest(str(path)).text == GLYPH_LAYOUT


def test_a_pdf_text_layer_reaches_the_canonical_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clearscout.infrastructure.web_fetch.ingest import pdf

    monkeypatch.setattr(pdf, "extract_text", lambda path: GLYPH_TEXT)
    assert pdf.from_pdf(tmp_path / "posting.pdf").text == GLYPH_LAYOUT


def test_a_vision_transcript_reaches_the_canonical_layout(tmp_path: Path) -> None:
    from clearscout.infrastructure.web_fetch.ingest.image import from_image

    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n")
    assert from_image(path, vision=lambda images, media: GLYPH_TEXT).text == GLYPH_LAYOUT


_INTRO = "Initrode builds the scheduling platform regional clinics plan their week against. "
_DUTY = "Own the weekly clinic schedule export and its reconciliation with site rosters"
_PAGE_URL = "https://example.test/jobs/data-engineer"


def _chrome(main: str, head: str = "") -> str:
    return (
        f"<html><head><title>Data Engineer</title>{head}</head><body><nav>Home Jobs</nav>"
        f"<main>{main}</main><footer>Privacy</footer></body></html>"
    )


@pytest.mark.parametrize(
    "visible",
    ["<div>Loading</div>", f"<article><h1>Data Engineer</h1><p>{_INTRO}</p></article>"],
)
def test_a_posting_only_embedded_article_markup_carries_is_left_to_the_rendered_read(
    visible: str,
) -> None:
    body = (
        f"<h2>About the role</h2><p>{_INTRO}</p><h2>Requirements</h2><ul>"
        + "".join(f"<li>{_DUTY} {n}.</li>" for n in range(6))
        + "</ul>"
    )
    node = {"@context": "https://schema.org", "@type": "WebPage", "articleBody": body}
    script = f'<script type="application/ld+json">{json.dumps(node)}</script>'

    with pytest.raises(IngestError, match="outside its readable blocks"):
        from_html(_PAGE_URL, _chrome(visible, script))


def test_a_short_article_the_fallback_glues_still_reads_one_block_per_line() -> None:
    html = _chrome(
        f"<article><h2>About the role</h2><p>{_INTRO}</p><h2>Requirements</h2>"
        "<ul><li>Fluency in SQL</li><li>Kafka</li></ul></article>"
    )

    assert _extract(html).splitlines() == [
        "About the role",
        _INTRO.strip(),
        "Requirements",
        "- Fluency in SQL",
        "- Kafka",
    ]


def test_a_page_with_headings_and_lists_keeps_each_block_on_its_own_line() -> None:
    items = "".join(f"<li>{_DUTY} {n}.</li>" for n in range(4))
    html = _chrome(
        f"<article><h1>Data Engineer</h1><p>{_INTRO * 2}</p><h3>Requirements</h3>"
        f"<ul>{items}</ul><p>{_INTRO}</p></article>"
    )

    lines = from_html(_PAGE_URL, html).text.splitlines()

    assert lines[:3] == ["Data Engineer", (_INTRO * 2).strip(), "Requirements"]
    assert f"- {_DUTY} 0." in lines
    assert not {"Home Jobs", "Privacy"} & set(lines)


def test_a_one_paragraph_page_reads_as_that_paragraph() -> None:
    paragraph = (_INTRO * 6).strip()
    html = _chrome(f"<article><p>{paragraph}</p></article>")

    assert from_html(_PAGE_URL, html).text == paragraph


def test_an_extracted_article_reaches_the_canonical_layout() -> None:
    """The article extractor ends a list flush against the next heading; the layout parts them."""
    items = "".join(f"<li>{_DUTY} {n}</li>" for n in range(3))
    html = _chrome(
        f"<article><h1>Data Engineer</h1><p>{_INTRO * 2}</p><h3>Requirements</h3>"
        f"<ul>{items}</ul><h3>Nice to have</h3><ul><li>Experience with Kafka</li></ul>"
        "</article>"
    )

    text = from_html(_PAGE_URL, html).text

    assert f"- {_DUTY} 2\n\nNice to have\n- Experience with Kafka" in text
    assert plain_layout(text) == text


_FRAMELESS_URL = "https://careers.example.test/postings/4417"
_GUIDELINES = "Write annotation guidelines for clinical tasks."
_DATASETS = "Design fine-tuning datasets."
_ROLE = (
    "You will curate the evaluation datasets our clinical language models are graded against, "
    "and you will work with researchers, pharmacists and nurses to decide what a correct answer is."
)
_OFFER = (
    "Initrode offers a remote-friendly schedule across North American time zones, a yearly "
    "learning budget, and paid time to present the team's findings at informatics meetings."
)


def _frameless_posting(lists: str, above: str = "") -> str:
    """Wrap a posting in plain divs and sections, with no article or main element."""
    return (
        "<html><head><title>Clinical Data Curator</title></head><body>"
        "<header><nav><ul><li><a href='/'>Home</a></li><li><a href='/jobs'>Open roles</a></li>"
        "</ul></nav></header>"
        f'<div class="padded-v-xlarge">{above}<div class="row"><div class="columns"><section>'
        f'<div class="padded-v-medium"><p>About the role:</p><p>{_INTRO}{_ROLE}</p>{lists}'
        f"<p>{_OFFER}</p></div></section></div></div></div>"
        "<div class='related-jobs'><h3>Similar jobs</h3><ul><li><a href='/j/1'>Data Engineer</a>"
        "</li><li><a href='/j/2'>Research Scientist</a></li></ul></div>"
        "<footer><ul><li><a href='/privacy'>Privacy policy</a></li><li>Copyright Initrode</li>"
        "</ul></footer></body></html>"
    )


def _read_frameless(lists: str, above: str = "") -> list[str]:
    page = _frameless_posting(lists, above)
    client = policy_client(lambda target: text_response(target, page))
    return [line for line in from_url(_FRAMELESS_URL, client=client).text.splitlines() if line]


def test_a_frameless_posting_keeps_every_list_item_under_its_heading() -> None:
    lines = _read_frameless(
        "<p>Responsibilities: </p>"
        f"<ul><li><span>{_GUIDELINES}</span></li><li><span>{_DATASETS}</span></li></ul>"
        "<p>Qualifications:<br>The skills you bring:</p>"
        "<ul><li><span>Graduate degree in life sciences.</span></li>"
        "<li><span>Three years with production datasets.</span></li></ul>"
    )

    start = lines.index("Responsibilities:")
    assert lines[start : start + 6] == [
        "Responsibilities:",
        f"- {_GUIDELINES}",
        f"- {_DATASETS}",
        "Qualifications:",
        "The skills you bring:",
        "- Graduate degree in life sciences.",
    ]
    assert "- Three years with production datasets." in lines


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        (
            f"<ul><li>{_GUIDELINES}</li><li>{_DATASETS}</li></ul>",
            [f"- {_GUIDELINES}", f"- {_DATASETS}"],
        ),
        (
            f"<ul><li><p>{_GUIDELINES}</p></li><li><p>{_DATASETS}</p></li></ul>",
            [f"- {_GUIDELINES}", f"- {_DATASETS}"],
        ),
        (
            "<ul><li><strong>Write</strong> annotation guidelines for clinical tasks.</li>"
            "<li><strong>Design</strong> fine-tuning datasets.</li></ul>",
            [f"- {_GUIDELINES}", f"- {_DATASETS}"],
        ),
        (
            f"<ul><li>{_GUIDELINES}<ul><li>Cover medication reconciliation.</li></ul></li>"
            f"<li>{_DATASETS}</li></ul>",
            [f"- {_GUIDELINES}", "- Cover medication reconciliation.", f"- {_DATASETS}"],
        ),
        (
            f"<ol><li>{_GUIDELINES}</li><li>{_DATASETS}</li></ol>",
            [f"- {_GUIDELINES}", f"- {_DATASETS}"],
        ),
    ],
    ids=["bare", "paragraph", "strong", "nested", "ordered"],
)
def test_a_frameless_posting_keeps_each_list_shape_in_order(
    items: str, expected: list[str]
) -> None:
    lines = _read_frameless(f"<p>Responsibilities:</p>{items}")

    start = lines.index("Responsibilities:") + 1
    assert lines[start : start + len(expected)] == expected


def test_a_frameless_posting_leaves_out_its_menu_footer_and_related_job_lists() -> None:
    text = "\n".join(_read_frameless(f"<p>Responsibilities:</p><ul><li>{_GUIDELINES}</li></ul>"))

    assert f"- {_GUIDELINES}" in text
    for chrome in ("Home", "Open roles", "Similar jobs", "Research Scientist", "Privacy policy"):
        assert chrome not in text


_SECTIONS = {
    "Responsibilities:": "Reconcile the clinic roster export for site",
    "Qualifications:": "Show a year of scheduling data work at site",
    "Benefits:": "Receive a learning stipend renewed at site",
}


def test_a_frameless_posting_keeps_its_lists_past_a_line_only_the_recall_read_sees() -> None:
    lists = "".join(
        f"<p>{heading}</p><ul>"
        + "".join(f"<li><span>{item} {n}.</span></li>" for n in range(3))
        + "</ul>"
        for heading, item in _SECTIONS.items()
    )

    lines = _read_frameless(lists, above="<div>Share:</div>")

    for heading, item in _SECTIONS.items():
        start = lines.index(heading) + 1
        assert lines[start : start + 3] == [f"- {item} {n}." for n in range(3)]
        assert not lines[start + 3].startswith("- ")
    assert "Share:" not in lines


def test_a_frameless_posting_leaves_out_a_list_that_sits_above_its_text() -> None:
    filters = "<div><ul><li>Toronto</li><li>Vancouver</li><li>Remote</li></ul></div>"

    lines = _read_frameless(f"<p>Responsibilities:</p><ul><li>{_GUIDELINES}</li></ul>", filters)

    assert f"- {_GUIDELINES}" in lines
    assert not {"- Toronto", "- Vancouver", "- Remote"} & set(lines)


def test_a_recall_read_that_reorders_the_posting_leaves_the_default_read_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trafilatura

    extract = trafilatura.extract

    def reordered_recall(body: str, **options: Any) -> str | None:
        if not options.pop("favor_recall", False):
            return extract(body, **options)
        first, second, *rest = (extract(body, **options) or "").splitlines()
        return "\n".join([second, first, "- Reordered recall item.", *rest])

    monkeypatch.setattr(trafilatura, "extract", reordered_recall)

    lines = _read_frameless(f"<p>Responsibilities:</p><ul><li>{_GUIDELINES}</li></ul>")

    assert lines.index("About the role:") < lines.index("Responsibilities:")
    assert "- Reordered recall item." not in lines
    assert f"- {_GUIDELINES}" not in lines


def test_icims_iframe_gets_a_title_prepended_from_its_url_slug() -> None:
    """ICIMS strips the title header from its iframe body, so recover it from the path."""
    url = (
        "https://careers-example.icims.com/jobs/12345/"
        "senior-platform-services-architect/job?in_iframe=1"
    )
    body = f"<html><body><div>{LONG}</div></body></html>"
    result = from_url(url, client=policy_client(lambda target: text_response(target, body)))
    assert result.method == "page extraction"
    assert result.text.startswith("Title: senior platform services architect")


_ICIMS_SITE = "https://careers-acme.icims.com/jobs/77"
_ICIMS_DUTY = "Analyze medication management data for pharmacy partners."


def _icims_site(requested: list[str]) -> PoliteHttpClient:
    chrome = "<p>Explore careers, benefits, locations and our story at Acme. </p>" * 20

    def handler(target: str) -> httpx.Response:
        requested.append(target)
        if target == f"{_ICIMS_SITE}/job?in_iframe=1":
            return text_response(target, f"<html><body><p>{_ICIMS_DUTY} {LONG}</p></body></html>")
        return text_response(target, f"<html><body><nav>Home Jobs</nav>{chrome}</body></html>")

    return policy_client(handler)


def test_an_icims_posting_is_read_from_its_iframe_body_under_its_own_address() -> None:
    url = f"{_ICIMS_SITE}/data-analyst/job"

    result = from_url(url, client=_icims_site([]))

    assert _ICIMS_DUTY in result.text
    assert result.origin == url


def test_an_icims_address_that_is_not_a_posting_is_read_as_written() -> None:
    requested: list[str] = []

    from_url(f"{_ICIMS_SITE}/data-analyst/login", client=_icims_site(requested))

    assert requested == [f"{_ICIMS_SITE}/data-analyst/login"]


def test_a_closed_icims_posting_is_reported_under_the_address_given() -> None:
    url = f"{_ICIMS_SITE}/data-analyst/job"
    client = policy_client(lambda target: text_response(target, "Not found", status_code=404))

    with pytest.raises(IngestError) as caught:
        from_url(url, client=client)

    message = str(caught.value)
    assert url in message
    assert "in_iframe" not in message
    assert closure_signal(message)


def test_a_robots_refusal_of_an_icims_body_names_the_refused_page_and_the_posting() -> None:
    url = f"{_ICIMS_SITE}/data-analyst/job"
    client = policy_client(
        lambda target: text_response(target, f"<html><body><p>{LONG}</p></body></html>"),
        robots="User-agent: *\nDisallow: /jobs/77/job\n",
    )

    with pytest.raises(IngestError) as caught:
        from_url(url, client=client)

    message = str(caught.value)
    assert f"robots.txt disallows fetching {_ICIMS_SITE}/job?in_iframe=1" in message
    assert url in message


@pytest.mark.parametrize(
    ("shell", "declared_url"),
    [
        ("https://careers-acme.icims.com/jobs/40417/clinical-data-analyst/job", None),
        (
            "https://careers-acme.icims.com/jobs/40417/clinical-data-analyst/job",
            "https://careers-acme.icims.com/jobs/40417/job?in_iframe=1",
        ),
        (
            "https://careers-acme.icims.com/jobs/40417/clinical-data-analyst/job",
            "https://careers-acme.icims.com/jobs/40417/job",
        ),
        (
            "https://careers-acme.icims.com/jobs/40417/clinical-data-analyst/job",
            "https://careers-acme.icims.com/jobs/40417/clinical-data-analyst/job",
        ),
        ("https://Careers-Acme.iCIMS.com/jobs/40417/clinical-data-analyst/job", None),
    ],
)
def test_an_icims_posting_check_reads_the_markup_its_iframe_body_declares(
    shell: str, declared_url: str | None
) -> None:
    markup = {
        "@type": "JobPosting",
        "title": "Clinical Data Analyst",
        "hiringOrganization": {"name": "Acme Health"},
        "description": f"<p>{_ICIMS_DUTY} {LONG}</p>",
        **({"url": declared_url} if declared_url else {}),
    }

    def handler(target: str) -> httpx.Response:
        if target.casefold() == "https://careers-acme.icims.com/jobs/40417/job?in_iframe=1":
            return text_response(
                target,
                f'<html><script type="application/ld+json">{json.dumps(markup)}</script></html>',
            )
        return text_response(target, f"<html><body><nav>Home Jobs</nav><p>{LONG}</p></body></html>")

    checked = check_posting(shell, client=policy_client(handler))

    assert checked.status == "readable"
    assert (checked.title, checked.employer) == ("Clinical Data Analyst", "Acme Health")


def test_workday_falling_through_to_the_page_still_gets_a_slug_title() -> None:
    """When the CXS API is unavailable, the slug in the Workday URL rescues the title."""
    url = (
        "https://acme.wd5.myworkdayjobs.com/en-US/External/job/"
        "Remote/Platform-Reliability-Engineer_R-9876"
    )

    def handler(target: str) -> httpx.Response:
        if "/wday/cxs/" in target:
            return text_response(target, "forbidden", status_code=403)
        return text_response(
            target,
            f"<html><body><main><h1>Remote</h1><h2>Responsibilities</h2><p>{LONG}</p></main>"
            "</body></html>",
        )

    result = from_url(url, client=policy_client(handler))
    assert result.text.startswith("Title: Platform Reliability Engineer")


_LINKEDIN_URL = "https://ca.linkedin.com/jobs/view/data-engineer-at-initrode-4100000001"
_OTHER_TITLES = (
    "Data Analyst",
    "Clinical Data Coordinator",
    "Reporting Specialist",
    "Scheduling Systems Analyst",
    "Health Informatics Lead",
    "Business Intelligence Developer",
    "Operations Research Analyst",
    "Integration Engineer",
    "Analytics Consultant",
    "Platform Support Specialist",
)


def _aside_cards(titles: tuple[str, ...]) -> str:
    # The guest page states each card title twice, so plain text runs them together.
    return "".join(
        f'<li><div class="base-card base-aside-card"><a href="https://example.test/{n}">'
        f'<span class="sr-only">{title}</span></a><div class="base-aside-card__info">'
        f'<h3 class="base-aside-card__title">{title}</h3>'
        f'<h4 class="base-aside-card__subtitle">Globex Clinics</h4>'
        f'<div class="base-aside-card__metadata"><span>Calderfield, Bramblewick</span>'
        f"<span>{n + 1} weeks ago</span></div></div></div></li>"
        for n, title in enumerate(titles)
    )


def _linkedin_guest_page(sidebar_titles: tuple[str, ...]) -> str:
    duties = "".join(f"<li>{_DUTY} {n}.</li>" for n in range(6))
    description = (
        f'<section class="core-section-container my-3 description">'
        f'<div class="core-section-container__content break-words">'
        f'<div class="description__text description__text--rich"><div class="show-more-less-html">'
        f'<div class="show-more-less-html__markup"><p>{_INTRO * 2}</p><p>Responsibilities</p>'
        f"<ul>{duties}</ul><p>{_INTRO}</p></div></div></div>"
        f'<ul class="description__job-criteria-list"><li class="description__job-criteria-item">'
        f'<h3 class="description__job-criteria-subheader">Seniority level</h3>'
        f'<span class="description__job-criteria-text">Mid-Senior level</span></li></ul>'
        f"</div></section>"
    )
    similar_jobs = (
        '<section class="core-section-container my-3 similar-jobs">'
        '<h2 class="core-section-container__title section-title">Similar jobs</h2>'
        f'<div class="core-section-container__content"><ul>{_aside_cards(sidebar_titles)}</ul>'
        "</div></section>"
    )

    def aside(kind: str, heading: str) -> str:
        return (
            f'<section class="aside-section-container mb-4 {kind}">'
            f'<h2 class="aside-section-container__title section-title">{heading}</h2>'
            f'<div class="aside-section-container__content"><ul>{_aside_cards(sidebar_titles)}'
            "</ul></div></section>"
        )

    return (
        "<html><head><title>Data Engineer - Initrode</title></head><body>"
        '<main class="main"><section class="core-rail"><div class="details">'
        '<section class="top-card-layout"><h1 class="top-card-layout__title">Data Engineer</h1>'
        '<h4 class="top-card-layout__second-subline">Initrode Calderfield, Bramblewick</h4>'
        f'</section><div class="decorated-job-posting__details">{description}</div>'
        '<section class="job-alert-redirect-section">'
        '<div class="job-alert-redirect-section__container">'
        "<p>Get notified about new Data Engineer jobs in Calderfield, Bramblewick.</p></div>"
        f'</section>{similar_jobs}</div></section><section class="right-rail">'
        + aside("people-also-viewed", "People also viewed")
        + aside("similar-searches", "Similar Searches")
        + aside("pulse-articles", "Explore collaborative articles")
        + "</section></main></body></html>"
    )


def test_a_linkedin_guest_page_body_ends_at_the_posting_before_its_related_listings() -> None:
    text = from_html(_LINKEDIN_URL, _linkedin_guest_page(_OTHER_TITLES)).text

    assert (_INTRO * 2).strip() in text
    assert f"- {_DUTY} 5." in text
    assert "Mid-Senior level" in text
    for heading in (
        "Get notified",
        "Similar jobs",
        "People also viewed",
        "Similar Searches",
        "Explore collaborative articles",
    ):
        assert heading not in text
    assert not any(title in text for title in _OTHER_TITLES)


def test_a_linkedin_guest_page_reads_the_same_whatever_listings_it_shows_beside_the_posting() -> (
    None
):
    first = from_html(_LINKEDIN_URL, _linkedin_guest_page(_OTHER_TITLES)).text
    reshuffled = from_html(_LINKEDIN_URL, _linkedin_guest_page(_OTHER_TITLES[::-1][:7])).text

    assert first == reshuffled


def test_a_smartrecruiters_posting_is_read_from_its_detail_endpoint_with_its_body() -> None:
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(
            target,
            {
                "id": "744000012345",
                "name": "Clinical Systems Analyst",
                "company": {"name": "Globex Health"},
                "location": {"city": "Northtown", "country": "us", "remote": True},
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": f"<p>{LONG}</p>"},
                        "qualifications": {"text": "<p>Five years of health IT.</p>"},
                    }
                },
            },
        )

    result = from_url(
        "https://jobs.smartrecruiters.com/Globex/744000012345-clinical-systems-analyst",
        client=policy_client(api),
    )

    assert requested == [
        "https://api.smartrecruiters.com/v1/companies/Globex/postings/744000012345"
    ]
    assert result.method == "SmartRecruiters API"
    assert result.employer == "Globex Health"
    assert result.text.startswith("Clinical Systems Analyst\n\nNorthtown, us\n\n")
    assert result.text.endswith("Five years of health IT.")
    assert result.publisher_check is not None
    assert result.publisher_check.facts.arrangements.values == ("remote",)


_DAYFORCE_POSTING = "https://jobs.dayforcehcm.com/en-US/globex/CANDIDATEPORTAL/jobs/12345"


def _dayforce_feed_row(job: str, **stated: object) -> dict[str, object]:
    """Build one Dayforce feed row, which states a posting once per region it serves."""
    return {
        "Title": "Clinical Data Manager",
        "Description": f"<p>{LONG}</p>",
        "CompanyName": "Acme Health",
        "JobDetailsUrl": f"https://jobs.dayforcehcm.com/en-US/globex/CANDIDATEPORTAL/jobs/{job}",
        "City": "Springfield",
        "State": "IL",
        "Country": "US",
        "ReferenceNumber": job,
        **stated,
    }


def test_a_dayforce_posting_is_read_from_the_site_feed_with_its_body() -> None:
    """A Dayforce page declares nothing, so the feed states the body as well as the identity."""
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(
            target,
            [_dayforce_feed_row("12345"), _dayforce_feed_row("67890", Title="Staff Data Engineer")],
        )

    result = from_url(_DAYFORCE_POSTING, client=policy_client(api))

    assert requested == [
        "https://www.dayforcehcm.com/api/globex/v1/jobfeeds?internalJobBoardCode=CANDIDATEPORTAL"
    ]
    assert result.method == "Dayforce API"
    assert (result.title, result.employer) == ("Clinical Data Manager", "Acme Health")
    assert result.text.startswith("A detailed job posting.")
    assert "<p>" not in result.text
    assert result.publisher_check is not None
    assert result.publisher_check.location == "Springfield, IL, United States"


def test_a_dayforce_posting_the_feed_no_longer_lists_reads_as_closed() -> None:
    def api(target: str) -> httpx.Response:
        return json_response(target, [_dayforce_feed_row("67890")])

    with pytest.raises(IngestError) as caught:
        from_url(_DAYFORCE_POSTING, client=policy_client(api))
    assert closure_signal(str(caught.value))


_BAMBOOHR_POSTING = "https://globex.bamboohr.com/careers/77"


def _bamboohr_detail(**stated: object) -> dict[str, object]:
    """Build BambooHR's single-opening detail answer, the one endpoint that states a body."""
    return {
        "result": {
            "formFields": {},
            "jobOpening": {
                "jobOpeningName": "Clinical Systems Analyst",
                "jobOpeningStatus": "Open",
                "jobOpeningShareUrl": _BAMBOOHR_POSTING,
                "location": {"city": "Northtown", "state": "NT", "addressCountry": "Canada"},
                "description": f"<p>{LONG}</p><p>Five years of health IT.</p>",
                **stated,
            },
        }
    }


def test_a_bamboohr_posting_is_read_from_its_detail_endpoint_with_its_body() -> None:
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(target, _bamboohr_detail())

    result = from_url(_BAMBOOHR_POSTING, client=policy_client(api))

    assert requested == ["https://globex.bamboohr.com/careers/77/detail"]
    assert result.method == "BambooHR API"
    assert result.title == "Clinical Systems Analyst"
    assert result.text.endswith("Five years of health IT.")
    assert "<p>" not in result.text
    assert result.publisher_check is not None
    assert result.publisher_check.location == "Northtown, NT, Canada"


@pytest.mark.parametrize(
    "answer",
    [
        lambda target: json_response(target, _bamboohr_detail(jobOpeningStatus="Closed")),
        lambda target: httpx.Response(404, request=httpx.Request("GET", target)),
    ],
    ids=["closed-status", "gone"],
)
def test_a_bamboohr_posting_its_detail_no_longer_holds_open_reads_as_closed(
    answer: Callable[[str], httpx.Response],
) -> None:
    with pytest.raises(IngestError) as caught:
        from_url(_BAMBOOHR_POSTING, client=policy_client(answer))
    assert closure_signal(str(caught.value))


_GEM_POSTING = "https://jobs.gem.com/globex/am9icG9zdDrChY8Z"


def _gem_post(**stated: object) -> dict[str, object]:
    """Build Gem's single job post, which states the same record its board list does."""
    return {
        "id": "am9icG9zdDrChY8Z",
        "title": "Clinical Systems Analyst",
        "absolute_url": _GEM_POSTING,
        "content": f"<div><p>{LONG}</p><p>Five years of health IT.</p></div>",
        "location": {"name": "Northtown, NT"},
        "location_type": "hybrid",
        **stated,
    }


def test_a_gem_posting_is_read_from_its_job_post_endpoint_with_its_body() -> None:
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(target, _gem_post())

    result = from_url(_GEM_POSTING, client=policy_client(api))

    assert requested == ["https://api.gem.com/job_board/v0/globex/job_posts/am9icG9zdDrChY8Z"]
    assert result.method == "Gem API"
    assert result.title == "Clinical Systems Analyst"
    assert result.text.endswith("Five years of health IT.")
    assert result.publisher_check is not None
    assert result.publisher_check.location == "Northtown, NT"


def test_a_gem_posting_its_endpoint_no_longer_holds_reads_as_closed() -> None:
    def gone(target: str) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", target))

    with pytest.raises(IngestError) as caught:
        from_url(_GEM_POSTING, client=policy_client(gone))
    assert closure_signal(str(caught.value))


_RIPPLING_JOB = "3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f"
_RIPPLING_POSTING = f"https://ats.rippling.com/globex/jobs/{_RIPPLING_JOB}"


def _rippling_detail(**stated: object) -> dict[str, object]:
    """Build Rippling's single-job answer, which states the body as company and role sections."""
    return {
        "uuid": _RIPPLING_JOB,
        "name": "Clinical Systems Analyst",
        "companyName": "Globex",
        "url": _RIPPLING_POSTING,
        "description": {
            "company": "<p>Globex builds clinical tools.</p>",
            "role": f"<p>{LONG}</p><p>Five years of health IT.</p>",
        },
        "workLocations": ["Northtown, NT"],
        "employmentType": {"label": "SALARIED_FT", "id": "Salaried, full-time"},
        **stated,
    }


_DOVER_JOB = "9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0"
_DOVER_POSTING = f"https://app.dover.io/apply/Globex/{_DOVER_JOB}"


def _dover_detail(**stated: object) -> dict[str, object]:
    """Build Dover's application-portal job, the one answer that states a body."""
    return {
        "id": _DOVER_JOB,
        "client_id": "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
        "client_name": "Globex",
        "title": "Clinical Systems Analyst",
        "location": "Northtown, NT",
        "user_provided_description": f"<p>{LONG}</p><p>Five years of health IT.</p>",
        **stated,
    }


_ADP_CID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
_ADP_JOB = "9200000000001_1"
_ADP_POSTING = (
    "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    f"?cid={_ADP_CID}&jobId={_ADP_JOB}&lang=en_CA"
)


def _adp_requisition(**stated: object) -> dict[str, object]:
    """Build an ADP career center's single-requisition answer, the one that states a body."""
    return {
        "itemID": _ADP_JOB,
        "requisitionTitle": "Clinical Systems Analyst",
        "clientRequisitionID": "1001",
        "requisitionLocations": [
            {
                "address": {"cityName": "Northtown", "countryCode": "CA"},
                "nameCode": {"shortName": " Northtown, NT"},
            }
        ],
        "requisitionDescription": f"<p>{LONG}</p><p>Five years of health IT.</p>",
        **stated,
    }


@pytest.mark.parametrize(
    ("posting", "endpoint", "detail", "method", "employer"),
    [
        pytest.param(
            _ADP_POSTING,
            "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
            f"job-requisitions/{_ADP_JOB}?cid={_ADP_CID}&lang=en_CA&locale=en_CA",
            _adp_requisition,
            "ADP Workforce Now API",
            "",
            id="adp",
        ),
        pytest.param(
            _RIPPLING_POSTING,
            f"https://ats.rippling.com/api/v2/board/globex/jobs/{_RIPPLING_JOB}",
            _rippling_detail,
            "Rippling API",
            "Globex",
            id="rippling",
        ),
        pytest.param(
            _DOVER_POSTING,
            f"https://app.dover.io/api/v1/inbound/application-portal-job/{_DOVER_JOB}",
            _dover_detail,
            "Dover API",
            "Globex",
            id="dover",
        ),
        pytest.param(
            _DOVER_POSTING.replace("app.dover.io", "app.dover.com"),
            f"https://app.dover.io/api/v1/inbound/application-portal-job/{_DOVER_JOB}",
            _dover_detail,
            "Dover API",
            "Globex",
            id="dover-com",
        ),
    ],
)
def test_a_detail_endpoint_posting_is_read_with_its_body(
    posting: str,
    endpoint: str,
    detail: Callable[[], dict[str, object]],
    method: str,
    employer: str,
) -> None:
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(target, detail())

    result = from_url(posting, client=policy_client(api))

    assert requested == [endpoint]
    assert result.method == method
    assert result.title == "Clinical Systems Analyst"
    assert result.employer == employer
    assert result.text.endswith("Five years of health IT.")
    assert "<p>" not in result.text
    assert result.publisher_check is not None
    assert result.publisher_check.location == "Northtown, NT"


@pytest.mark.parametrize(
    ("company_paragraph", "employer"),
    [
        pytest.param("<p>Company: Initech</p>", "Initech", id="plain-label"),
        pytest.param(
            "<p><strong>Company:</strong> <i>Initech</i></p>", "Initech", id="inline-tags"
        ),
        pytest.param("<p>Company: Initech &amp; Globex</p>", "Initech & Globex", id="entities"),
        pytest.param("<p> cOmPaNy : Initech </p>", "Initech", id="case-and-spacing"),
        pytest.param("<p>Company:<br>Initech\nServices</p>", "Initech Services", id="wrapped-name"),
        pytest.param("<p>Company:</p>", "", id="empty-label"),
        pytest.param("<p>Our company is Initech.</p>", "", id="company-prose"),
        pytest.param("<p>Introduction.</p><p>Company: Initech</p>", "", id="later-label"),
        pytest.param("", "", id="missing-label"),
    ],
)
def test_an_adp_individual_read_names_only_a_leading_company_value(
    company_paragraph: str, employer: str
) -> None:
    published = f"{company_paragraph}<p>{LONG}</p><p>Five years of health IT.</p>"
    result = from_url(
        _ADP_POSTING,
        client=policy_client(
            lambda target: json_response(target, _adp_requisition(requisitionDescription=published))
        ),
    )

    assert result.employer == employer
    assert result.publisher_check is not None
    assert result.publisher_check.employer == employer
    assert result.text == strip_tags(published)
    assert result.publisher_check.text == result.text


@pytest.mark.parametrize("career_center", ["19000101_000001_2", "19000101_000001_3"])
@pytest.mark.parametrize("center_first", [False, True])
def test_an_adp_individual_read_keeps_its_career_center(
    career_center: str, center_first: bool
) -> None:
    page = _ADP_POSTING.partition("?")[0]
    parts = [f"cid={_ADP_CID}", f"ccId={career_center}", f"jobId={_ADP_JOB}", "lang=en_CA"]
    if center_first:
        parts.reverse()
    posting = f"{page}?{'&'.join(parts)}"
    endpoint = (
        "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
        f"job-requisitions/{_ADP_JOB}?cid={_ADP_CID}&ccId={career_center}&lang=en_CA&locale=en_CA"
    )
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return (
            json_response(target, _adp_requisition())
            if target == endpoint
            else json_response(target, {})
        )

    result = from_url(posting, client=policy_client(api))

    assert requested == [endpoint]
    assert result.method == "ADP Workforce Now API"
    assert result.title == "Clinical Systems Analyst"
    assert result.text.endswith("Five years of health IT.")


@pytest.mark.parametrize("posting", [_RIPPLING_POSTING, _DOVER_POSTING, _ADP_POSTING])
def test_a_detail_endpoint_posting_its_endpoint_no_longer_holds_reads_as_closed(
    posting: str,
) -> None:
    def gone(target: str) -> httpx.Response:
        return httpx.Response(404, request=httpx.Request("GET", target))

    with pytest.raises(IngestError) as caught:
        from_url(posting, client=policy_client(gone))
    assert closure_signal(str(caught.value))
    # The endpoint's closure, not the rendered page's own 404.
    assert str(caught.value) == "no longer lists this posting as open, so it has closed."


_TELUS_POSTING = "https://www.telusinternational.ai/cmp/public/jobs/available/4321"


def _telus_post(**stated: object) -> dict[str, object]:
    """Build TELUS AI Community's public post, whose qualifications are a second body field."""
    return {
        "id": 4321,
        "title": "Clinical Data Annotation Specialist",
        "description": f"<p>{LONG}</p>",
        "qualification_path": "<ul><li>Five years of clinical informatics.</li></ul>",
        "status": "active",
        "is_global": True,
        "region": None,
        "country": None,
        "compensation": {
            "amount": "20",
            "currency": "USD",
            "unit": "per_hour",
            "amount_max_range": "50",
        },
        **stated,
    }


def test_a_telus_posting_is_read_from_its_public_post_endpoint_with_its_qualifications() -> None:
    requested: list[str] = []

    def api(target: str) -> httpx.Response:
        requested.append(target)
        return json_response(target, _telus_post())

    result = from_url(_TELUS_POSTING, client=policy_client(api))

    assert requested == [
        "https://api.telusinternational.ai/apapi/v1/jobs-entry/job-posts/4321/public"
    ]
    assert result.method == "TELUS AI Community API"
    assert result.title == "Clinical Data Annotation Specialist"
    assert result.employer == "TELUS Digital"
    assert result.text.endswith("Five years of clinical informatics.")
    assert result.publisher_check is not None
    assert result.publisher_check.facts.compensation.values == ("20-50 USD per hour",)


@pytest.mark.parametrize(
    "answer",
    [
        lambda target: json_response(target, _telus_post(status="inactive")),
        lambda target: httpx.Response(404, request=httpx.Request("GET", target)),
    ],
    ids=["inactive-status", "gone"],
)
def test_a_telus_posting_its_endpoint_no_longer_holds_active_reads_as_closed(
    answer: Callable[[str], httpx.Response],
) -> None:
    with pytest.raises(IngestError) as caught:
        from_url(_TELUS_POSTING, client=policy_client(answer))
    assert closure_signal(str(caught.value))


_UKG_PAGE = (
    "https://recruiting2.ultipro.com/GLO1001GLOBX/JobBoard/"
    "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d/OpportunityDetail"
    "?opportunityId=7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
)
_UKG_ROBOTS = "User-agent: *\nAllow: */JobBoard/\nDisallow: */JobBoardView\n"


def _ukg_page(**stated: object) -> str:
    """Build a UKG posting page, whose script hands the opportunity record to its viewer."""
    opportunity = {
        "Id": "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
        "Title": "Clinical Informatics Pharmacist",
        "RequisitionNumber": "CLINI001234",
        "Description": f"<p>{LONG}</p><ul><li>Five years of health IT.</li></ul>",
        "OpportunityIsClosed": False,
        "Locations": [
            {
                "LocalizedName": "Northtown Office",
                "DisplayAddress": True,
                "Address": {
                    "City": "Northtown",
                    "State": {"Name": "North State", "Code": "NT"},
                    "Country": {"Name": "United States", "Code": "USA"},
                },
            },
            {"LocalizedName": "Remote - Canada", "DisplayAddress": False, "Address": None},
        ],
        "PayRangeVisible": False,
        "PayRange": {"PayRangeMinimum": "95000.00", "PayRangeMaximum": "120000.00"},
        "PayRangeCurrencyCode": "USD",
        **stated,
    }
    return (
        "<html><head><title>Opportunity</title></head><body><div id='app'></div><script>"
        "$(function () { var opportunity = new US.Opportunity.CandidateOpportunityDetail("
        f"{json.dumps(opportunity)}); }});</script></body></html>"
    )


def test_a_ukg_posting_is_read_from_the_opportunity_its_page_embeds() -> None:
    requested: list[str] = []

    def page(target: str) -> httpx.Response:
        requested.append(target)
        return text_response(target, _ukg_page())

    result = from_url(f"{_UKG_PAGE}&postingId=88", client=policy_client(page, robots=_UKG_ROBOTS))

    assert requested == [_UKG_PAGE]
    assert result.method == "UKG Pro Recruiting page"
    assert result.title == "Clinical Informatics Pharmacist"
    assert result.text.endswith("Five years of health IT.")
    assert result.publisher_check is not None
    assert result.publisher_check.location == "Northtown, NT, United States"
    assert result.publisher_check.facts.locations.values == (
        "Northtown, NT, United States",
        "Remote - Canada",
    )
    # The page fills its pay range even when it hides it from applicants.
    assert result.publisher_check.facts.compensation.values == ()


def test_a_ukg_posting_on_a_tenant_pro_host_is_read_the_same_way() -> None:
    address = _UKG_PAGE.replace("recruiting2.ultipro.com", "acme.rec.pro.ukg.net")
    requested: list[str] = []

    def page(target: str) -> httpx.Response:
        requested.append(target)
        return text_response(target, _ukg_page())

    result = from_url(address, client=policy_client(page, robots=_UKG_ROBOTS))

    assert requested == [address]
    assert result.method == "UKG Pro Recruiting page"
    assert result.title == "Clinical Informatics Pharmacist"


def test_a_ukg_posting_states_its_pay_only_when_the_page_shows_it() -> None:
    def page(target: str) -> httpx.Response:
        return text_response(target, _ukg_page(PayRangeVisible=True))

    result = from_url(_UKG_PAGE, client=policy_client(page, robots=_UKG_ROBOTS))

    assert result.publisher_check is not None
    assert result.publisher_check.facts.compensation.values == ("95000.00-120000.00 USD",)


@pytest.mark.parametrize(
    "answer",
    [
        lambda target: text_response(target, _ukg_page(OpportunityIsClosed=True)),
        lambda target: text_response(target, "Not found", status_code=404),
    ],
    ids=["closed-opportunity", "gone"],
)
def test_a_ukg_posting_its_page_states_closed_reads_as_closed(
    answer: Callable[[str], httpx.Response],
) -> None:
    with pytest.raises(IngestError) as caught:
        from_url(_UKG_PAGE, client=policy_client(answer, robots=_UKG_ROBOTS))
    assert closure_signal(str(caught.value))


_SMARTRECRUITERS_POSTING = (
    "https://jobs.smartrecruiters.com/Globex/744000012345-clinical-systems-analyst"
)
_DISALLOW_EVERYTHING = "User-agent: *\nDisallow: /\n"


def _vendor_api(target: str) -> httpx.Response:
    """Answer the vendor endpoint that states the posting behind each known URL."""
    if "dayforcehcm.com" in target:
        return json_response(target, [_dayforce_feed_row("12345")])
    if "bamboohr.com" in target:
        return json_response(target, _bamboohr_detail())
    if "api.gem.com" in target:
        return json_response(target, _gem_post())
    return json_response(
        target,
        {
            "id": "744000012345",
            "name": "Clinical Systems Analyst",
            "company": {"name": "Globex Health"},
            "location": {"city": "Northtown", "country": "us"},
            "jobAd": {"sections": {"jobDescription": {"text": f"<p>{LONG}</p>"}}},
        },
    )


@pytest.mark.parametrize(
    ("requested_url", "employer"),
    [
        (_DAYFORCE_POSTING, "Acme Health"),
        (_SMARTRECRUITERS_POSTING, "Globex Health"),
        (_BAMBOOHR_POSTING, ""),
        (_GEM_POSTING, ""),
    ],
)
def test_a_known_posting_check_reads_a_vendor_api_its_robots_disallows(
    requested_url: str, employer: str
) -> None:
    """The refresh and the preflight check a posting the person holds, so robots does not bind."""
    checked = check_posting(
        requested_url,
        client=policy_client(_vendor_api, robots=_DISALLOW_EVERYTHING),
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert checked.status == "readable"
    assert checked.employer == employer
    assert checked.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ


@pytest.mark.parametrize(
    "requested_url", [_DAYFORCE_POSTING, _SMARTRECRUITERS_POSTING, _BAMBOOHR_POSTING, _GEM_POSTING]
)
def test_a_crawling_check_of_the_same_posting_is_refused_by_that_policy(requested_url: str) -> None:
    client = policy_client(_vendor_api, robots=_DISALLOW_EVERYTHING)

    checked = check_posting(requested_url, client=client)

    assert checked.status == "unreadable"


_DAYFORCE_REGIONAL_FEED = (
    "https://regional.dayforcehcm.com/api/globex/v1/jobfeeds?internalJobBoardCode=CANDIDATEPORTAL"
)


def _regionally_redirected_feed(target: str) -> httpx.Response:
    """Answer the published feed URL with the 302 to a regional host that serves it."""
    if target.startswith("https://www.dayforcehcm.com/"):
        return httpx.Response(
            302,
            request=httpx.Request("GET", target),
            headers={"Location": _DAYFORCE_REGIONAL_FEED},
        )
    return json_response(target, [_dayforce_feed_row("12345")])


def test_a_known_posting_check_follows_the_feeds_redirect_to_a_regional_host() -> None:
    checked = check_posting(
        _DAYFORCE_POSTING,
        client=policy_client(_regionally_redirected_feed, robots=_DISALLOW_EVERYTHING),
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert checked.status == "readable"
    assert checked.employer == "Acme Health"
    assert checked.robots_basis == RobotsReadBasis.SINGLE_POSTING_READ


def test_a_crawling_check_of_the_redirected_feed_is_refused() -> None:
    checked = check_posting(
        _DAYFORCE_POSTING,
        client=policy_client(_regionally_redirected_feed, robots=_DISALLOW_EVERYTHING),
    )

    assert checked.status == "unreadable"


def test_a_feed_larger_than_the_read_ceiling_reads_as_unreadable_not_closed() -> None:
    def api(target: str) -> httpx.Response:
        oversized = _dayforce_feed_row("12345", Description="<p>" + "a" * 9_000_000 + "</p>")
        return json_response(target, [oversized])

    checked = check_posting(
        _DAYFORCE_POSTING,
        client=policy_client(api, robots=_DISALLOW_EVERYTHING),
        intent=PostingReadIntent.SINGLE_POSTING,
    )

    assert checked.status == "unreadable"
    assert "exceeded the size limit" in checked.note


def test_a_publisher_page_declaring_a_posting_states_its_structured_facts() -> None:
    markup = {
        "@type": "JobPosting",
        "title": "Clinical Systems Analyst",
        "hiringOrganization": {"name": "Globex Health"},
        "description": f"<p>{LONG}</p>",
        "validThrough": "2020-01-31",
        "jobLocation": {
            "address": {
                "addressLocality": "Northtown",
                "addressRegion": "NT",
                "addressCountry": "US",
            }
        },
    }
    page = f'<html><script type="application/ld+json">{json.dumps(markup)}</script></html>'

    result = from_url(
        "https://careers.globex.example/openings/clinical-systems-analyst",
        client=policy_client(lambda target: text_response(target, page)),
    )

    checked = result.publisher_check
    assert checked is not None
    assert (checked.platform, checked.status) == ("schema_org", "readable")
    assert (checked.title, checked.employer) == ("Clinical Systems Analyst", "Globex Health")
    assert checked.facts.locations.values == ("Northtown, NT, United States",)
    assert checked.facts.locations.complete is True
    assert checked.facts.freshness is FreshnessState.LIKELY_STALE


_NUMBERED_POSTING = "https://recruiting.globex.example/Recruiting/Jobs/Details/4471"


def _numbered_posting_page(
    og_url: str, *, head: str = "", banner: str = "", valid_through: str = ""
) -> str:
    """Return a page whose lone block states neither url nor identifier, beside its og:url."""
    if og_url:
        head = f'<meta property="og:url" content="{og_url}" />{head}'

    markup = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Clinical Systems Analyst",
        "datePosted": "2026-10-20",
        "description": f"<p>{LONG}</p>",
        "hiringOrganization": {"name": "Globex Health"},
        "jobLocation": {"address": {"addressLocality": "Northtown", "addressCountry": "US"}},
        "baseSalary": {"currency": "USD", "value": {"minValue": 90000, "maxValue": 110000}},
    }
    if valid_through:
        markup["validThrough"] = valid_through
    return (
        f"<html><head>{head}</head><body>{banner}"
        f'<script type="application/ld+json">{json.dumps(markup)}</script>'
        f'<a href="/Recruiting/Jobs/Apply/4471">Apply</a></body></html>'
    )


def test_a_lone_block_on_a_page_stating_the_requested_address_checks_readable() -> None:
    page = _numbered_posting_page(_NUMBERED_POSTING)

    checked = check_posting(
        _NUMBERED_POSTING, client=policy_client(lambda target: text_response(target, page))
    )

    assert (checked.status, checked.title) == ("readable", "Clinical Systems Analyst")
    assert checked.facts.posted_date is not None
    assert checked.facts.posted_date.latest.wording == "2026-10-20"


@pytest.mark.parametrize(
    "og_url",
    [
        "https://recruiting.globex.example/Recruiting/Jobs/Details/5582",
        "https://recruiting.globex.example/Recruiting/Jobs/All",
    ],
    ids=["another-posting", "the-index"],
)
def test_a_lone_block_on_a_page_stating_another_address_stays_unsupported(og_url: str) -> None:
    page = _numbered_posting_page(og_url)

    checked = check_posting(
        _NUMBERED_POSTING, client=policy_client(lambda target: text_response(target, page))
    )

    assert checked.status == "unsupported"


@pytest.mark.parametrize(
    ("og_url", "head"),
    [
        ("", f'<link rel="canonical" href="{_NUMBERED_POSTING}" />'),
        ("/Recruiting/Jobs/Details/4471", ""),
    ],
    ids=["canonical-only", "relative-og-url"],
)
def test_either_self_declared_address_vouches_for_the_requested_posting(
    og_url: str, head: str
) -> None:
    page = _numbered_posting_page(og_url, head=head)

    checked = check_posting(
        _NUMBERED_POSTING, client=policy_client(lambda target: text_response(target, page))
    )

    assert checked.status == "readable"


def test_an_og_url_and_canonical_that_disagree_vouch_for_neither() -> None:
    other = "https://recruiting.globex.example/Recruiting/Jobs/Details/5582"
    page = _numbered_posting_page(
        _NUMBERED_POSTING, head=f'<link rel="canonical" href="{other}" />'
    )

    checked = check_posting(
        _NUMBERED_POSTING, client=policy_client(lambda target: text_response(target, page))
    )

    assert checked.status == "unsupported"


_CLOSURE_BANNER = "<h1>This job is no longer available</h1><p>This position has been filled.</p>"
_SITE_NAV = (
    "<header><nav><ul>"
    + "".join(
        f'<li><a href="/p{i}">Section link number {i} of the site</a></li>' for i in range(14)
    )
    + "</ul></nav></header>"
)
_SITE_FOOTER = (
    "<footer><p>Globex Health is an equal opportunity employer. All qualified applicants "
    "will receive consideration for employment without regard to protected status. "
    "Copyright 2026 Globex Health. Privacy policy. Terms of use. Accessibility.</p></footer>"
)
_LARGER_FOOTER = _SITE_FOOTER.replace(
    "</p>", " " + "Offices in Northtown, Southtown and Easttown. " * 4 + "</p>"
)


@pytest.mark.parametrize("valid_through", ["2025-03-01", "2099-03-01", ""])
@pytest.mark.parametrize(
    "banner",
    [
        _CLOSURE_BANNER,
        f"<main>{_CLOSURE_BANNER}</main>{_SITE_FOOTER}",
        f"<main>{_CLOSURE_BANNER}</main>{_LARGER_FOOTER}",
        f"{_SITE_NAV}<main>{_CLOSURE_BANNER}</main>",
        f"{_SITE_NAV}<main>{_CLOSURE_BANNER}</main>{_SITE_FOOTER}",
    ],
    ids=["banner", "footer", "larger-footer", "nav", "nav-and-footer"],
)
def test_a_closed_page_echoing_the_requested_address_stays_unsupported(
    banner: str, valid_through: str
) -> None:
    page = _numbered_posting_page(_NUMBERED_POSTING, banner=banner, valid_through=valid_through)

    checked = check_posting(
        _NUMBERED_POSTING, client=policy_client(lambda target: text_response(target, page))
    )

    assert checked.status == "unsupported"


def test_a_page_stating_the_requested_address_after_a_redirect_elsewhere_stays_unsupported() -> (
    None
):
    landed = "https://recruiting.globex.example/Recruiting/Jobs/Details/5582"
    page = _numbered_posting_page(_NUMBERED_POSTING)

    def answer(target: str) -> httpx.Response:
        if target == _NUMBERED_POSTING:
            return httpx.Response(
                302, request=httpx.Request("GET", target), headers={"Location": landed}
            )
        return text_response(target, page)

    checked = check_posting(_NUMBERED_POSTING, client=policy_client(answer))

    assert checked.status == "unsupported"


def test_a_url_without_a_recognised_slug_leaves_the_body_alone() -> None:
    """A generic careers page has no derivable slug, so no title line is invented."""
    body = f"<html><body><h1>Real Title Here</h1><p>{LONG}</p></body></html>"
    result = from_url(
        "https://example.com/careers/staff-engineer",
        client=policy_client(lambda target: text_response(target, body)),
    )
    assert not result.text.startswith("Title:")


def blank_pdf(path: Path, pages: int = 1) -> Path:
    """Write a valid PDF that simply has no text on it, which is what a scan is."""
    import pypdfium2

    document = pypdfium2.PdfDocument.new()
    for _ in range(pages):
        document.new_page(612, 792)
    document.save(str(path))
    document.close()
    return path


def test_a_pdf_with_no_text_layer_says_to_screenshot_it(tmp_path: Path) -> None:
    """A scan is a real case, and the honest answer is cheaper than rasterizing."""
    from clearscout.infrastructure.web_fetch.ingest.pdf import from_pdf

    with pytest.raises(IngestError, match="screenshot"):
        from_pdf(blank_pdf(tmp_path / "scan.pdf"))


def test_an_unreadable_pdf_offers_the_same_remedy(tmp_path: Path) -> None:
    from clearscout.infrastructure.web_fetch.ingest.pdf import from_pdf

    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nnot really a pdf")
    with pytest.raises(IngestError, match="screenshot"):
        from_pdf(path)


def test_a_pdf_reader_failure_on_one_page_does_not_lose_the_others(tmp_path: Path) -> None:
    """Nine good pages should survive one malformed one."""
    from clearscout.infrastructure.web_fetch.ingest.pdf import extract_text

    assert extract_text(blank_pdf(tmp_path / "blank.pdf", pages=3)) == ""


def test_a_pdf_reports_the_lines_set_wholly_in_a_bold_font() -> None:
    """A line mixing a bold title with a lighter date is not a heading."""
    from clearscout.infrastructure.web_fetch.ingest.pdf import extract_bold_lines

    path = FIXTURES / "cv_formats" / "academic_pdf" / "cv.pdf"
    bold = [line.strip() for line in extract_bold_lines(path)]

    assert "SUMMARY" in bold
    assert "Acme Instruments" in bold
    assert not [line for line in bold if line.startswith(("PhD,", "Applications Scientist"))]


def test_a_base_font_named_bold_is_emphasis_even_with_no_weight_declared(tmp_path: Path) -> None:
    """One of the base fourteen fonts has no descriptor, so pdfium reports weight zero."""
    from clearscout.infrastructure.web_fetch.ingest.pdf import extract_bold_lines
    from tests.support.pdf_documents import text_pdf

    placed = [(60, 700, "PROFESSIONAL EXPERIENCE")]
    bold = text_pdf(tmp_path / "bold.pdf", placed, "Helvetica-Bold")
    plain = text_pdf(tmp_path / "plain.pdf", placed)

    assert [line.strip() for line in extract_bold_lines(bold)] == ["PROFESSIONAL EXPERIENCE"]
    assert extract_bold_lines(plain) == []


POSTING_FORMATS = FIXTURES / "posting_formats"
PAGE_SHAPES = (
    "declared_paragraphs_html",
    "declared_one_line_html",
    "declared_bare_list_html",
    "declared_markup_html",
    "whole_page_listing_html",
)
PDF_SHAPES = (
    "details_table_pdf",
    "sidebar_panel_pdf",
    "single_column_pdf",
    "sidebar_and_details_pdf",
)
# Which PDF readings hold a shape in its written order, by the corpus term for it.
WHOLE_UNDER = {
    "stream": ["stream"],
    "columns": ["columns"],
    "either": ["stream", "columns"],
    "neither": [],
}


def posting_format(shape: str) -> tuple[Path, dict[str, Any]]:
    """Return one corpus posting and the text each reading of it holds."""
    expected = tomllib.loads(
        (POSTING_FORMATS / shape / "expected.toml").read_text(encoding="utf-8")
    )
    return POSTING_FORMATS / shape / expected["source"], expected


def pdf_readings(path: Path) -> dict[str, list[str]]:
    """Return the non-blank lines each reading of a posting PDF holds, in order.

    The stream reading goes through the shipped posting reader, so the corpus
    fails if the opt-in column cut is ever made the default underneath it.
    """
    from clearscout.infrastructure.web_fetch.ingest.pdf import extract_text, from_pdf

    texts = {"stream": from_pdf(path).text, "columns": extract_text(path, columns=True)}
    return {
        name: [line.strip() for line in text.splitlines() if line.strip()]
        for name, text in texts.items()
    }


@pytest.mark.parametrize("shape", PAGE_SHAPES)
def test_each_posting_page_shape_reaches_the_reader_as_the_corpus_states(shape: str) -> None:
    path, expected = posting_format(shape)
    read = from_html(expected["url"], path.read_text(encoding="utf-8"))
    assert read.method == expected["method"]
    assert read.text.splitlines() == expected["lines"]


@pytest.mark.parametrize("shape", PDF_SHAPES)
def test_each_posting_pdf_shape_is_read_in_the_order_the_corpus_states(shape: str) -> None:
    path, expected = posting_format(shape)
    readings = pdf_readings(path)
    for name, lines in readings.items():
        assert lines == expected.get(name, expected["lines"])
    whole = [name for name, lines in readings.items() if lines == expected["lines"]]
    assert whole == WHOLE_UNDER[expected["faithful"]]


def test_an_image_reader_refuses_a_file_that_is_not_an_image(tmp_path: Path) -> None:
    from clearscout.infrastructure.web_fetch.ingest.image import media_type_of

    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(IngestError, match="does not look like an image"):
        media_type_of(path)


def test_an_image_without_vision_says_so(tmp_path: Path) -> None:
    from clearscout.infrastructure.web_fetch.ingest.image import from_image

    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n")
    with pytest.raises(IngestError, match="vision-capable"):
        from_image(path, vision=None)


def test_an_image_goes_through_the_injected_vision_reader(tmp_path: Path) -> None:
    from clearscout.infrastructure.web_fetch.ingest.image import from_image

    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n")
    result = from_image(path, vision=lambda images, media: LONG)
    assert result.method == "vision (screenshot)"
    assert json.dumps(result.text)[:2] == '"A'


def test_an_api_that_answers_with_nothing_falls_through_to_the_page() -> None:
    """Some boards return a valid record with an empty description."""

    def api(url: str) -> httpx.Response:
        if "boards-api" in url:
            return json_response(
                url, {"title": "Staff Data Engineer", "location": {"name": "Remote"}, "content": ""}
            )
        return text_response(url, f"<html><body><p>{LONG}</p></body></html>")

    result = from_url(
        "https://job-boards.greenhouse.io/initech/jobs/8556870002", client=policy_client(api)
    )
    assert result.method == "page extraction"
    assert len(result.text) > 400


def test_greenhouse_content_is_unescaped_twice_when_the_board_double_escapes() -> None:
    """Greenhouse serves HTML-escaped HTML; both layers have to come off."""
    escaped = "<p>" + LONG + " Kafka &amp;amp; Terraform</p>"
    result = from_url(
        "https://job-boards.greenhouse.io/initech/jobs/8556870002",
        client=policy_client(
            lambda url: json_response(
                url,
                {
                    "title": "Staff Data Engineer",
                    "location": {"name": "Remote"},
                    "content": escaped,
                },
            )
        ),
    )
    assert result.method == "Greenhouse API"
    assert "<p>" not in result.text
    assert "Kafka & Terraform" in result.text


def test_a_page_that_declares_its_own_posting_is_read_from_that_rather_than_scraped() -> None:
    declared = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Platform Engineer",
            "description": f"<p>{LONG}</p>",
            "validThrough": "2026-09-01T00:00:00Z",
        }
    )
    body = (
        f'<html><head><script type="application/ld+json">{declared}</script></head>'
        "<body><p>Cookie banner. Apply now. Share this role.</p></body></html>"
    )
    result = from_url(
        "https://northwind.example/careers/1",
        client=policy_client(
            lambda url: (
                text_response(url, body, status_code=403)
                if "api" in url
                else text_response(url, body)
            )
        ),
    )

    # The publisher stated the posting, so nothing had to be recovered from the
    # chrome around it.
    assert result.method == "publisher markup"
    assert "Cookie banner" not in result.text
    assert "<p>" not in result.text
    assert result.text.startswith("A detailed job posting.")
    assert result.publisher_markup_present is True
    assert result.valid_through == "2026-09-01T00:00:00Z"


def test_markup_that_declares_no_usable_body_still_falls_through_to_extraction() -> None:
    declared = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Platform Engineer",
            "validThrough": "2026-09-01",
        }
    )
    body = (
        f'<html><head><script type="application/ld+json">{declared}</script></head>'
        f"<body><p>{LONG}</p></body></html>"
    )
    # Plenty of pages declare the title and dates and leave the body to the page.
    result = from_url(
        "https://northwind.example/careers/2",
        client=policy_client(lambda url: text_response(url, body)),
    )
    assert result.method == "page extraction with publisher markup"
    assert result.publisher_markup_present is True
    assert result.valid_through == "2026-09-01"


_CANONICAL_POSTING = (
    "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health-4398765210"
)
_OTHER_ROLES = "".join(
    f"<p>{role} at a regional health system, with hybrid schedules and full benefits.</p>"
    for role in ("Staff Data Engineer", "Registered Nurse", "Pharmacy Technician", "Analyst")
)


def _posting_block(**stated: str) -> str:
    """State one JobPosting block with a usable body and the identity fields given."""
    declared = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Clinical Data Manager",
        "hiringOrganization": {"name": "Acme Health"},
        "description": f"<p>{LONG}</p>",
        **stated,
    }
    return f'<script type="application/ld+json">{json.dumps(declared)}</script>'


def _page_naming_its_canonical(canonical: str, *, declared: str = "") -> str:
    """Build a posting address's page: a canonical link beside a list of other roles."""
    return (
        f'<html><head><link rel="canonical" href="{canonical}">'
        f'<meta property="og:url" content="{canonical}">{declared}</head>'
        f"<body><main><h1>Jobs you may like</h1>{_OTHER_ROLES * 2}</main></body></html>"
    )


def _site(pages: dict[str, str], requested: list[str]) -> PoliteHttpClient:
    """Serve each page at its own address, recording every posting page read."""

    def handler(target: str) -> httpx.Response:
        requested.append(target)
        if target in pages:
            return text_response(target, pages[target])
        return text_response(target, "", status_code=404)

    return policy_client(handler)


@pytest.mark.parametrize(
    "requested_url",
    [
        "https://www.jobs.example/jobs/view/4398765210/",
        "https://www.jobs.example/jobs/view/4398765210",
        "https://www.jobs.example/jobs/view/another-role-4398765210",
    ],
)
def test_a_bare_posting_address_reads_the_posting_its_canonical_page_declares(
    requested_url: str,
) -> None:
    requested: list[str] = []
    pages = {
        requested_url: _page_naming_its_canonical(_CANONICAL_POSTING),
        _CANONICAL_POSTING: _page_naming_its_canonical(
            _CANONICAL_POSTING, declared=_posting_block(url=_CANONICAL_POSTING)
        ),
    }

    result = from_url(requested_url, client=_site(pages, requested))

    assert (result.method, result.title) == ("publisher markup", "Clinical Data Manager")
    assert result.origin == _CANONICAL_POSTING
    assert requested == [requested_url, _CANONICAL_POSTING]


@pytest.mark.parametrize(
    "declared_canonical",
    [
        pytest.param(
            "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health-4466005677",
            id="another-identifier",
        ),
        pytest.param(
            "https://elsewhere.example/jobs/view/clinical-data-manager-at-acme-health-4398765210",
            id="another-host",
        ),
        pytest.param(
            "https://www.jobs.example/jobs/view/clinical-data-manager-at-acme-health",
            id="no-identifier",
        ),
        pytest.param("https://www.jobs.example/jobs/view/4398765210/", id="the-page-itself"),
    ],
)
def test_a_canonical_page_that_is_not_this_posting_is_never_read(declared_canonical: str) -> None:
    bare = "https://www.jobs.example/jobs/view/4398765210/"
    requested: list[str] = []
    pages = {
        declared_canonical: _page_naming_its_canonical(
            declared_canonical, declared=_posting_block(url=declared_canonical)
        ),
        bare: _page_naming_its_canonical(declared_canonical),
    }

    result = from_url(bare, client=_site(pages, requested))

    assert result.method == "page extraction"
    assert result.origin == bare
    assert requested == [bare]


def test_a_page_declaring_its_own_posting_does_not_read_its_canonical_page() -> None:
    bare = "https://www.jobs.example/jobs/view/4398765210/"
    requested: list[str] = []
    pages = {
        bare: _page_naming_its_canonical(
            _CANONICAL_POSTING, declared=_posting_block(identifier="4398765210")
        ),
    }

    result = from_url(bare, client=_site(pages, requested))

    assert result.method == "publisher markup"
    assert result.origin == bare
    assert requested == [bare]


def test_a_workable_short_link_reads_its_board_feed_by_shortcode() -> None:
    url = "https://apply.workable.com/j/AB12CD34EF"
    board_url = "https://apply.workable.com/acme/"
    feed_url = "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"
    requested: list[str] = []
    role = "Configure clinical systems for hospital teams. " * 15

    def answer(target: str) -> httpx.Response:
        requested.append(target)
        assert target == feed_url
        return json_response(
            target,
            {
                "jobs": [
                    {"shortcode": "OTHER", "title": "Other Role", "description": LONG},
                    {
                        "shortcode": "AB12CD34EF",
                        "title": "Clinical Systems Analyst",
                        "url": url,
                        "description": role,
                    },
                ]
            },
        )

    result = from_url(
        url,
        board_url=board_url,
        intent=PostingReadIntent.SINGLE_POSTING,
        client=policy_client(answer),
    )

    assert result.method == "Workable API"
    assert result.title == "Clinical Systems Analyst"
    assert role.strip() in result.text
    assert "Other Role" not in result.text
    assert requested == [feed_url]


@pytest.mark.parametrize(
    "jobs", [[], ["invalid"], [{"title": "No shortcode"}], [{"shortcode": None}]]
)
def test_a_workable_short_link_does_not_close_on_an_unusable_feed(jobs: list[object]) -> None:
    answer = _workable(
        "https://apply.workable.com/j/AB12CD34EF",
        board_url="https://apply.workable.com/acme",
        client=policy_client(lambda target: json_response(target, {"jobs": jobs})),
    )

    assert answer.publisher_check is not None
    assert answer.publisher_check.status == "unreadable"


def test_a_workable_short_link_closes_only_when_a_populated_feed_omits_it() -> None:
    answer = _workable(
        "https://apply.workable.com/j/AB12CD34EF",
        board_url="https://apply.workable.com/acme",
        client=policy_client(
            lambda target: json_response(
                target, {"jobs": [{"shortcode": "OTHER", "title": "Other Role"}]}
            )
        ),
    )

    assert answer.publisher_check is not None
    assert answer.publisher_check.status == "closed"


def test_workable_short_links_share_one_feed_read_per_intent_and_pass() -> None:
    feed_url = "https://apply.workable.com/api/v1/widget/accounts/acme?details=true"
    requested: list[str] = []
    jobs = [
        {"shortcode": shortcode, "title": f"Role {shortcode}", "description": LONG}
        for shortcode in ("AB12CD34EF", "EF56GH78IJ")
    ]

    def response(target: str) -> httpx.Response:
        requested.append(target)
        assert target == feed_url
        return json_response(target, {"jobs": jobs})

    client = policy_client(response)
    board_url = "https://apply.workable.com/acme"
    first = "https://apply.workable.com/j/AB12CD34EF"
    second = "https://apply.workable.com/j/EF56GH78IJ"
    with shared_posting_feeds():
        assert from_url(first, client=client, board_url=board_url).method == "Workable API"
        assert from_url(second, client=client, board_url=board_url).method == "Workable API"
        assert (
            from_url(
                first,
                client=client,
                board_url=board_url,
                intent=PostingReadIntent.SINGLE_POSTING,
            ).method
            == "Workable API"
        )
    assert from_url(first, client=client, board_url=board_url).method == "Workable API"

    assert requested == [feed_url, feed_url, feed_url]
