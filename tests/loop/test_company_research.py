"""Company research stays bounded, same-host, and behind the polite client."""

from __future__ import annotations

import httpx
import pytest

from clearscout.infrastructure.web_fetch.company_research import (
    CareersPageRefusedError,
    CompanyResearchFetcher,
    ResearchSource,
    evidence_block,
    fingerprint,
)
from tests.support.http import COMPANY_CAREERS_PAGE

SITE_NAV = "".join(
    f'<a href="/{slug}">{slug.title()}</a>'
    for slug in ("products", "solutions", "customers", "company", "careers", "contact")
)
COMPANY_PROSE = (
    "Example Works builds scheduling and routing software for regional transit agencies. "
    "Our teams own a service from design through operation, publish an engineering handbook, "
    "and meet in person twice a year. Most roles are remote within the country. "
)
PERKS = (
    "Full health coverage, a retirement match, sixteen weeks of paid parental leave, "
    "and a learning budget."
)
ABOUT_LINK = '<p><a href="/about-us">About us</a></p>'
ABOUT_PAGE = (
    f"<html><body><main><h1>About Example Works</h1><p>{COMPANY_PROSE}</p></main></body></html>"
)


class ScriptedClient:
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, bool]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.requests.append((url, bool(kwargs["follow_redirects"])))
        if url not in self.responses:
            return httpx.Response(404, request=httpx.Request("GET", url))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            headers={"content-type": "text/html; charset=utf-8"},
            text=self.responses[url],
        )


def test_research_fingerprint_ignores_how_identical_evidence_was_retrieved() -> None:
    fetched = ResearchSource(
        id="company.about",
        kind="about",
        url="https://example.test/about",
        text="The same employer-owned evidence.",
        method="direct_http",
    )
    stored = ResearchSource(
        id=fetched.id,
        kind=fetched.kind,
        url=fetched.url,
        text=fetched.text,
        method="stored_research_artifact",
    )

    assert fingerprint((fetched,)) == fingerprint((stored,))


def test_research_reads_one_same_host_about_page_without_following_redirects() -> None:
    careers = "https://example.test/careers"
    about = "https://example.test/about-us"
    client = ScriptedClient(
        {
            careers: COMPANY_CAREERS_PAGE.replace(
                "</main>", '<a href="https://elsewhere.test/about">About a partner</a></main>'
            ),
            about: ABOUT_PAGE,
        }
    )

    sources = CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    assert [(item.id, item.url) for item in sources] == [
        ("company.careers", careers),
        ("company.about", about),
    ]
    assert client.requests == [(careers, False), (about, False)]
    assert "Source ID: company.careers" in evidence_block(sources)
    assert len(fingerprint(sources)) == 64
    assert fingerprint(sources) != fingerprint(sources[:1])


@pytest.mark.parametrize(
    ("about_page", "kept"),
    [
        pytest.param("<main>We make tools for public services.</main>", False, id="thin"),
        pytest.param(
            ABOUT_PAGE.replace("</p>", " Meet {{ team.lead_name }}.</p>"),
            False,
            id="unrendered-placeholder",
        ),
        pytest.param(ABOUT_PAGE, True, id="clean"),
    ],
)
def test_research_keeps_an_about_page_only_when_its_text_describes_the_company(
    about_page: str, kept: bool
) -> None:
    careers = "https://example.test/careers"
    about = "https://example.test/about-us"
    client = ScriptedClient({careers: COMPANY_CAREERS_PAGE, about: about_page})

    sources = CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    assert [item.url for item in sources] == [careers, *([about] if kept else [])]


def test_research_refuses_local_credential_bearing_or_malformed_urls_without_a_request() -> None:
    client = ScriptedClient({})
    fetcher = CompanyResearchFetcher(client=client)  # type: ignore[arg-type]

    assert fetcher.fetch("http://localhost/careers") == ()
    assert fetcher.fetch("https://user:secret@example.test/careers") == ()
    assert fetcher.fetch("http://127.0.0.1/careers") == ()
    assert fetcher.fetch("https://[example.test/careers") == ()
    assert client.requests == []


def test_a_careers_page_that_could_not_be_read_yields_nothing_without_a_refusal() -> None:
    client = ScriptedClient({})

    sources = CompanyResearchFetcher(client=client).fetch("https://example.test/careers")  # type: ignore[arg-type]

    assert sources == ()
    assert client.requests == [("https://example.test/careers", False)]


FOOTER_LINKS = "".join(
    f'<a href="/{slug}">{label}</a>'
    for slug, label in (
        ("privacy", "Privacy policy and data protection notice"),
        ("terms", "Terms of use for this careers website"),
        ("cookies", "Cookie preferences and consent settings"),
        ("accessibility", "Accessibility statement for applicants"),
        ("fraud", "Recruitment fraud warning and reporting"),
        ("press", "Press room and media enquiries"),
        ("investors", "Investor relations and annual reports"),
        ("locations", "Office locations around the world"),
    )
)


def careers_landing(sections: str, *, header: str, footer: str) -> str:
    """Build an employer's careers landing page: prose, a section, and one open-roles button."""
    return (
        "<html><head><title>Careers | Example Works</title></head><body>"
        f"<header>{header}</header><main><h1>Careers at Example Works</h1>"
        f"<p>{COMPANY_PROSE}</p>{sections}"
        '<a class="button" href="/careers/search">See open roles</a></main>'
        f"<footer>{footer}</footer></body></html>"
    )


@pytest.mark.parametrize(
    ("careers", "page", "requested"),
    [
        pytest.param(
            "https://example.test/careers",
            "<html><head><title>Careers | Example Works</title></head>"
            f"<body><h1>Careers | Example Works</h1>{ABOUT_LINK}</body></html>",
            ["https://example.test/careers"],
            id="only-the-page-title",
        ),
        pytest.param(
            "https://example.test/careers",
            '<html><head><script src="/static/app.js"></script></head><body>'
            "<noscript>You need to enable JavaScript to run this app.</noscript>"
            '<div id="root"></div><script>window.__BOOT__={"route":"careers"}</script>'
            f"{ABOUT_LINK}</body></html>",
            ["https://example.test/careers"],
            id="client-side-script-wall",
        ),
        pytest.param(
            "https://example.test/careers",
            "<html><body><header><nav>"
            '<a href="/">Home</a><a href="/teams">Our teams</a><a href="/life">Life here</a>'
            '<a href="/benefits">Perks</a><a href="/about-us">About us</a></nav></header>'
            "<div><h2>{{ job.job_title }}</h2><p>{{ job.location }} - {{ job.department }}</p>"
            "</div><footer>" + FOOTER_LINKS + "</footer></body></html>",
            ["https://example.test/careers"],
            id="navigation-chrome-with-template-placeholders",
        ),
        pytest.param(
            "https://example.test/careers",
            careers_landing(
                "<h2>Open roles</h2><p>{{ job.title }} in {{ job.location }}</p>" + ABOUT_LINK,
                header=f"<nav>{SITE_NAV}</nav>",
                footer=FOOTER_LINKS,
            ),
            ["https://example.test/careers"],
            id="company-prose-around-an-unrendered-placeholder",
        ),
        pytest.param(
            "https://example.test/careers",
            careers_landing(
                ABOUT_LINK,
                header=f"<nav>{SITE_NAV}</nav>",
                footer=FOOTER_LINKS,
            ).replace(
                "</head>",
                '<script type="application/ld+json">{"@context": "https://schema.org", '
                '"@type": "JobPosting", "title": "Route planning engineer", '
                '"hiringOrganization": {"@type": "Organization", "name": "Example Works"}, '
                '"description": "Plan routes for regional transit agencies."}</script></head>',
            ),
            ["https://example.test/careers"],
            id="page-declaring-a-job-posting",
        ),
        pytest.param(
            "https://boards.greenhouse.io/exampleworks/jobs/4455667",
            COMPANY_CAREERS_PAGE,
            [],
            id="another-posting-on-the-same-ats",
        ),
        pytest.param(
            "https://jobs.lever.co/exampleworks",
            COMPANY_CAREERS_PAGE,
            [],
            id="a-board-root-on-an-ats",
        ),
        pytest.param(
            "https://exampleworks.wd5.myworkdayjobs.com/en-US/External?q=analyst",
            COMPANY_CAREERS_PAGE,
            [],
            id="a-job-search-results-page-on-an-ats",
        ),
    ],
)
def test_research_refuses_a_careers_page_that_cannot_describe_the_company(
    careers: str, page: str, requested: list[str]
) -> None:
    client = ScriptedClient({careers: page, "https://example.test/about-us": ABOUT_PAGE})

    with pytest.raises(CareersPageRefusedError):
        CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    assert [url for url, _ in client.requests] == requested


@pytest.mark.parametrize(
    "page",
    [
        pytest.param(
            careers_landing(
                f"<h2>Benefits</h2><p>{PERKS}</p>",
                header=f"<nav>{SITE_NAV}</nav>",
                footer=FOOTER_LINKS,
            ),
            id="benefits-section-and-an-open-roles-button",
        ),
        pytest.param(
            careers_landing(
                f"<h2>Perks</h2><p>{PERKS}</p>",
                header=f'<div class="bar">{SITE_NAV}</div>',
                footer="".join(
                    f'<div class="col"><a href="/{slug}">{slug.title()}</a></div>'
                    for slug in (
                        "privacy",
                        "terms",
                        "cookies",
                        "accessibility",
                        "press",
                        "investors",
                        "blog",
                        "events",
                        "partners",
                        "security",
                    )
                ),
            ),
            id="perks-section-inside-link-heavy-chrome",
        ),
        pytest.param(
            careers_landing(
                "<div>.spinner{animation:spin 1s}}"
                "@keyframes spin{to{transform:rotate(1turn)}}</div>",
                header=f"<nav>{SITE_NAV}</nav>",
                footer=FOOTER_LINKS,
            ),
            id="leaked-inline-css",
        ),
    ],
)
def test_research_keeps_a_careers_landing_page_that_describes_the_company(page: str) -> None:
    careers = "https://example.test/careers"
    client = ScriptedClient({careers: page})

    sources = CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    assert [(item.id, item.url) for item in sources] == [("company.careers", careers)]
    assert "regional transit agencies" in sources[0].text


SITE_MENU = (
    "<nav><ul>"
    + "".join(
        f'<li><a href="/menu/{number}">Menu item {number:03d}</a><p>Menu item {number:03d} '
        "helps planning teams compare routing options, publish timetables, and answer "
        "rider questions from one shared workspace.</p></li>"
        for number in range(180)
    )
    + "</ul></nav>"
)
SITE_FOOTER = (
    "<footer><ul>"
    + "".join(
        f'<li><a href="/policy/{number}">Footer item {number:03d}</a><p>Footer item '
        f"{number:03d} sets out a policy for riders and staff in enough detail to read as prose."
        "</p></li>"
        for number in range(120)
    )
    + "</ul></footer>"
)
HISTORY = " ".join(
    f"In {year}, Example Works shipped release {year - 1990} of its planner to "
    f"{year - 1985} regional agencies and opened a support desk in city number {year - 1980}."
    for year in range(1991, 2031)
)


def test_research_stores_each_page_s_own_prose_without_the_site_menu_or_footer() -> None:
    careers = "https://example.test/careers"
    about = "https://example.test/about-us"
    client = ScriptedClient(
        {
            careers: f"<html><body>{SITE_MENU}<main><h1>Careers at Example Works</h1>"
            f"<p>{COMPANY_PROSE}</p><p>{PERKS}</p>{ABOUT_LINK}</main></body></html>",
            about: f"<html><body>{SITE_MENU}<main><h1>About Example Works</h1>"
            f"<p>{COMPANY_PROSE}</p><p>{HISTORY}</p></main>{SITE_FOOTER}</body></html>",
        }
    )

    sources = CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    texts = {item.id: item.text for item in sources}
    assert "regional transit agencies" in texts["company.careers"]
    assert "sixteen weeks of paid parental leave" in texts["company.careers"]
    assert "city number 50." in texts["company.about"]
    assert all("Menu item" not in text and "Footer item" not in text for text in texts.values())


MISSION = (
    "<h1>About Example Works</h1><p>Example Works exists to make every regional bus arrive "
    "when its riders expect it, and it builds the scheduling and routing software that "
    "twelve transit agencies use to plan their service each week.</p>"
)
VALUES = f"<p>We value plain answers and steady releases.</p><p>{PERKS}</p>"


@pytest.mark.parametrize(
    "about_body",
    [
        pytest.param(
            f'{SITE_MENU}<header class="hero">{MISSION}</header><main>{VALUES}</main>',
            id="hero-header",
        ),
        pytest.param(
            f"{SITE_MENU}<main><article><header>{MISSION}</header>{VALUES}</article></main>",
            id="article-header",
        ),
    ],
)
def test_research_keeps_an_about_page_mission_written_in_a_header(about_body: str) -> None:
    careers = "https://example.test/careers"
    about = "https://example.test/about-us"
    client = ScriptedClient(
        {
            careers: f"<html><body><main><h1>Careers at Example Works</h1>"
            f"<p>{COMPANY_PROSE}</p>{ABOUT_LINK}</main></body></html>",
            about: f"<html><body>{about_body}</body></html>",
        }
    )

    sources = CompanyResearchFetcher(client=client).fetch(careers)  # type: ignore[arg-type]

    about_text = {item.id: item.text for item in sources}["company.about"]
    assert "exists to make every regional bus arrive" in about_text
    assert "Menu item" not in about_text


def test_research_accepts_a_genuine_company_careers_page() -> None:
    careers = "https://example.test/careers"
    client = ScriptedClient({careers: COMPANY_CAREERS_PAGE.replace("/about-us", "/team")})

    sources = CompanyResearchFetcher(client=client).fetch(  # type: ignore[arg-type]
        careers, posting_urls=("https://example.test/careers/roles/route-planner",)
    )

    assert [(item.id, item.url) for item in sources] == [("company.careers", careers)]
    assert "regional transit agencies" in sources[0].text
