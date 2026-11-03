from __future__ import annotations

import pytest

from clearscout.domain.records import PageClassification
from clearscout.infrastructure.web_fetch.ingest.careers import (
    LinkRelation,
    PublisherLink,
    inspect_publisher_page,
)
from tests.support.suite import FIXTURES

SOURCE = "https://publisher.example/careers"


def test_two_ordinary_role_links_form_a_collection_and_resume_stays_unclustered() -> None:
    page = """
    <main><ul>
      <li><a href="/careers/roles/data-lead">Data Lead</a></li>
      <li><a href="/careers/roles/program-lead">Program Lead</a></li>
      <li><a href="/careers/submit-resume">Submit a resume</a></li>
    </ul></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION
    assert "repeated substantive links share a target shape" in result.evidence
    assert [link.label for link in result.candidate_links] == [
        "Data Lead",
        "Program Lead",
        "Submit a resume",
    ]
    assert result.candidate_links[0].relations == (
        "same-origin",
        "publisher-content",
        "substantive-label",
        "repeated-target-shape",
    )
    resume = result.candidate_links[2]
    assert resume.label == "Submit a resume"
    assert resume.cluster_size == 1


def test_single_publisher_content_link_remains_a_candidate_for_detail_inspection() -> None:
    result = inspect_publisher_page(
        SOURCE,
        '<main><div class="role"><a href="/opportunity/only-one">Opportunity</a></div></main>',
    )

    assert result.classification is PageClassification.AMBIGUOUS
    assert [link.fetch_url for link in result.candidate_links] == [
        "https://publisher.example/opportunity/only-one"
    ]


def test_two_buttons_to_one_apply_target_are_not_listing_evidence() -> None:
    button = '<div class="apply-card"><a href="https://apply.example/j/123">Apply now</a></div>'

    result = inspect_publisher_page(SOURCE, f"<main>{button}<p>Details</p>{button}</main>")

    assert result.classification is not PageClassification.COLLECTION
    assert "repeated substantive links share a target shape" not in result.evidence


@pytest.mark.parametrize("chrome", ["nav", "footer"])
@pytest.mark.parametrize("content_first", [False, True], ids=["chrome-first", "content-first"])
def test_career_index_content_links_take_precedence_over_duplicate_chrome_links(
    chrome: str, content_first: bool
) -> None:
    roles = (
        ("senior-graphic-designer", "Senior Graphic Designer"),
        ("partnerships-manager", "Partnerships Manager"),
    )
    chrome_links = (
        f"<{chrome}>"
        + "".join(
            f'<a href="/careers/roles/{slug}?utm_source=menu" aria-label="Explore {slug}"></a>'
            for slug, _ in roles
        )
        + f"</{chrome}>"
    )
    content = (
        "<h1>Careers at Northwind</h1><div><ul>"
        + "".join(f'<li><a href="/careers/roles/{slug}">{label}</a></li>' for slug, label in roles)
        + "</ul></div><div><dl>"
        + "".join(
            f'<dt><a href="/careers/roles/{slug}">{label}</a></dt>'
            "<dd>Read the role description and follow its application instructions.</dd>"
            for slug, label in roles
        )
        + "</dl><h2>Our values</h2><p>Northwind brings people together to build useful "
        "services for its community. Our shared responsibilities include supporting "
        "one another, making decisions carefully, and improving how our teams work.</p>"
        "<h2>Benefits</h2><p>Our teams receive learning opportunities and time to "
        "contribute to projects across the organization.</p>"
        '<form action="/search"><input name="q"><button>Search</button></form></div>'
    )
    page = (
        "<html><head><title>Careers at Northwind</title></head><body>"
        + (content + chrome_links if content_first else chrome_links + content)
        + "</body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""
    assert [link.fetch_url for link in result.candidate_links] == [
        f"https://publisher.example/careers/roles/{slug}" for slug, _ in roles
    ]
    assert [link.label for link in result.candidate_links] == [label for _, label in roles]
    assert {link.label_source for link in result.candidate_links} == {"visible"}
    assert {link.structural_context for link in result.candidate_links} == {"list"}
    assert {link.cluster_size for link in result.candidate_links} == {2}
    assert all(
        LinkRelation.PUBLISHER_CONTENT in link.relations
        and LinkRelation.REPEATED_TARGET_SHAPE in link.relations
        and LinkRelation.NAVIGATION_CHROME not in link.relations
        for link in result.candidate_links
    )


@pytest.mark.parametrize("chrome", ["nav", "footer"])
def test_duplicate_menu_role_links_do_not_make_a_role_detail_a_collection(chrome: str) -> None:
    links = "".join(
        f'<a href="/careers/roles/{slug}">{label}</a>'
        for slug, label in (
            ("senior-graphic-designer", "Senior Graphic Designer"),
            ("partnerships-manager", "Partnerships Manager"),
        )
    )
    page = (
        f"<{chrome}>{links}</{chrome}><main><h1>Clinical Data Manager</h1>"
        f"{_POSTING_SECTIONS}</main><{chrome}>{links}</{chrome}>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Clinical Data Manager"
    assert result.candidate_links == ()
    assert {link.structural_context for link in result.links} == {
        "navigation" if chrome == "nav" else "footer"
    }


def test_repeated_table_rows_are_collection_evidence_without_language_or_vendor_keys() -> None:
    rows = "".join(
        f'<tr><td><a href="/portal/vista?clave={number}&seccion=detalle">'
        f"Puesto {number}</a></td></tr>"
        for number in range(8)
    )

    result = inspect_publisher_page(SOURCE, f"<main><table>{rows}</table></main>")

    assert result.classification is PageClassification.COLLECTION
    assert len(result.candidate_links) == 8
    assert {link.cluster_size for link in result.candidate_links} == {8}
    assert {link.structural_context for link in result.candidate_links} == {"table"}


@pytest.mark.parametrize("row_class", ["job_list_row", "job-list-row", "job_row"])
def test_structured_job_rows_without_a_main_frame_are_collection_evidence(row_class: str) -> None:
    rows = "".join(
        f'<div class="{row_class}"><a href="/jobs/analyst-{number}">'
        f"Clinical Data Analyst {number}</a><p>Acme, Example City. Full time. "
        "Posted today. Analyze clinical data with the reporting team.</p></div>"
        for number in range(4)
    )

    result = inspect_publisher_page(SOURCE, f"<h1>Available jobs</h1>{rows}")

    assert result.classification is PageClassification.COLLECTION
    assert len(result.candidate_links) == 4
    assert {link.cluster_size for link in result.candidate_links} == {4}
    assert all(LinkRelation.PUBLISHER_CONTENT in link.relations for link in result.candidate_links)


@pytest.mark.parametrize("role_count", [6, 10])
@pytest.mark.parametrize(
    "container",
    [
        "<section><div>{rows}</div></section>",
        "<div><section>{rows}</section></div>",
        "<section><div><section>{rows}</section></div></section>",
    ],
    ids=["section-div", "div-section", "nested-sections"],
)
def test_roles_in_sections_form_a_collection_without_a_main_frame(
    role_count: int, container: str
) -> None:
    rows = "".join(
        f'<div><h2><a href="roles/data-analyst-{number}">'
        f"Clinical Data Analyst {number}</a></h2><p>Remote. Full time.</p></div>"
        for number in range(role_count)
    )
    page = (
        '<header><section><a href="/about">About Northwind</a></section></header>'
        "<h1>Open roles</h1>"
        + container.format(rows=rows)
        + '<div><a href="/research/updates">Research updates</a></div>'
        '<footer><section><a href="/privacy">Privacy policy</a></section></footer>'
    )

    result = inspect_publisher_page("https://publisher.example/careers/", page)

    assert result.classification is PageClassification.COLLECTION
    assert [link.fetch_url for link in result.candidate_links] == [
        f"https://publisher.example/careers/roles/data-analyst-{number}"
        for number in range(role_count)
    ]
    assert [link.label for link in result.candidate_links] == [
        f"Clinical Data Analyst {number}" for number in range(role_count)
    ]
    assert {link.cluster_size for link in result.candidate_links} == {role_count}
    assert {link.structural_context for link in result.candidate_links} == {"section"}
    assert all(
        LinkRelation.PUBLISHER_CONTENT in link.relations
        and LinkRelation.REPEATED_TARGET_SHAPE in link.relations
        for link in result.candidate_links
    )


@pytest.mark.parametrize(
    ("container", "context"),
    [
        ("<footer>{rows}</footer>", "footer"),
        ("<nav>{rows}</nav>", "navigation"),
        ('<div role="navigation">{rows}</div>', "navigation"),
        ('<section role="navigation">{rows}</section>', "navigation"),
    ],
    ids=["footer", "nav", "navigation-role", "section-navigation-role"],
)
def test_sections_inside_navigation_chrome_do_not_supply_collection_candidates(
    container: str, context: str
) -> None:
    rows = (
        '<section><div><section><a href="/roles/data-lead">Data Lead</a>'
        '<a href="/roles/program-lead">Program Lead</a></section></div></section>'
    )

    result = inspect_publisher_page(SOURCE, container.format(rows=rows))

    assert result.classification is PageClassification.NAVIGATION
    assert result.candidate_links == ()
    assert {link.structural_context for link in result.links} == {context}
    assert all(LinkRelation.NAVIGATION_CHROME in link.relations for link in result.links)


def test_unrelated_section_links_do_not_form_a_collection() -> None:
    page = (
        "<h1>About Northwind</h1><section><div>"
        '<a href="/research">Research updates</a>'
        '<a href="/community/events">Community events</a>'
        '<a href="/contact/directions/office">Office directions</a>'
        "</div></section>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.AMBIGUOUS
    assert all(link.cluster_size == 1 for link in result.links)
    assert all(LinkRelation.REPEATED_TARGET_SHAPE not in link.relations for link in result.links)


@pytest.mark.parametrize("wall", ["<h1>Access denied</h1>", '<form><input type="password"></form>'])
def test_a_short_wall_with_section_role_links_is_still_a_challenge(wall: str) -> None:
    page = (
        wall + '<section><div><a href="/roles/data-lead">Data Lead</a>'
        '<a href="/roles/program-lead">Program Lead</a></div></section>'
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.ERROR_OR_CHALLENGE


def test_a_readable_job_collection_outweighs_a_future_maintenance_notice() -> None:
    rows = "".join(
        f'<div class="job_list_row"><a href="/jobs/analyst-{number}">'
        f"Clinical Data Analyst {number}</a><p>Acme, Example City. Full time. "
        "Posted today. Analyze clinical data with the reporting team.</p></div>"
        for number in range(4)
    )
    page = "<p>The site will be unavailable during scheduled maintenance next week.</p>" + rows

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION
    assert len(result.candidate_links) == 4


@pytest.mark.parametrize("container", ["nav", "footer"])
def test_structured_job_rows_inside_chrome_are_not_candidates(container: str) -> None:
    rows = "".join(
        f'<div class="job_list_row"><a href="/jobs/analyst-{number}">'
        f"Clinical Data Analyst {number}</a></div>"
        for number in range(4)
    )

    result = inspect_publisher_page(SOURCE, f"<{container}>{rows}</{container}>")

    assert result.candidate_links == ()
    assert {link.structural_context for link in result.links} == {
        container if container != "nav" else "navigation"
    }


def test_ordinary_marketing_divs_do_not_become_collection_candidates() -> None:
    page = '<h1>Our services</h1><div class="marketing_row"><a href="/services/one">'
    page += 'Clinical analytics</a></div><div class="marketing_row">'
    page += '<a href="/services/two">Reporting support</a></div>'

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.COLLECTION
    assert result.candidate_links == ()


@pytest.mark.parametrize("wall", ["<h1>Access denied</h1>", '<form><input type="password"></form>'])
def test_a_short_wall_with_job_row_links_is_still_a_challenge(wall: str) -> None:
    page = wall + '<div class="job_list_row"><a href="/jobs/one">Analyst One</a></div>'
    page += '<div class="job_list_row"><a href="/jobs/two">Analyst Two</a></div>'

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.ERROR_OR_CHALLENGE


def test_non_english_repeated_cards_are_collection_evidence() -> None:
    page = """
    <main>
      <div class="result-card"><a href="/stellen/leitung-daten">Leitung Daten</a></div>
      <div class="result-card"><a href="/stellen/leitung-produkt">Leitung Produkt</a></div>
      <div class="result-card"><a href="/stellen/leitung-programm">Leitung Programm</a></div>
    </main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION
    assert [link.structural_context for link in result.candidate_links] == ["card"] * 3


def test_tracking_is_removed_without_discarding_functional_query_keys() -> None:
    page = """
    <main><table>
      <tr><td><a href="/board?role=41&amp;locale=fr&amp;utm_source=mail&amp;gclid=x">
        Rôle 41</a></td></tr>
      <tr><td><a href="/board?role=42&amp;locale=fr&amp;utm_source=mail&amp;gclid=y">
        Rôle 42</a></td></tr>
    </table></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert [link.fetch_url for link in result.candidate_links] == [
        "https://publisher.example/board?role=41&locale=fr",
        "https://publisher.example/board?role=42&locale=fr",
    ]
    assert result.candidate_links[0].target_shape.endswith("/board?locale&role")


@pytest.mark.parametrize(
    "tracking", ["ref=search", "utm_source=mail", "gclid=click", "_ga=session"]
)
def test_tracking_only_queries_group_disjoint_role_pages_and_keep_published_next_links(
    tracking: str,
) -> None:
    pages = []
    for page_number, slugs in enumerate(
        [("research-associate", "clinical-data-manager"), ("data-engineer", "research-analyst")],
        1,
    ):
        rows = "".join(
            f'<li><a href="/roles/{slug}?{tracking}">{slug.replace("-", " ")}</a></li>'
            for slug in slugs
        )
        page = (
            f"<main><ul>{rows}</ul>"
            f'<a rel="next" href="?page={page_number + 1}">Next page</a></main>'
        )
        result = inspect_publisher_page(f"{SOURCE}?page={page_number}", page)
        assert result.classification is PageClassification.COLLECTION
        role_links = result.candidate_links[:2]
        assert {link.cluster_size for link in role_links} == {2}
        assert role_links[0].target_shape == role_links[1].target_shape
        assert [link.fetch_url for link in role_links] == [
            f"https://publisher.example/roles/{slug}"
            + (f"?{tracking}" if tracking.startswith("ref=") else "")
            for slug in slugs
        ]
        assert result.candidate_links[-1].fetch_url == f"{SOURCE}?page={page_number + 1}"
        pages.append({link.fetch_url for link in role_links})
    assert pages[0].isdisjoint(pages[1])


@pytest.mark.parametrize("functional", ["job=41", "location=west", "tenant=acme"])
def test_tracking_does_not_merge_distinct_paths_with_functional_query_keys(functional: str) -> None:
    page = (
        "<main><ul>"
        f'<li><a href="/roles/research-associate?ref=search&amp;{functional}">'
        "Research Associate</a></li>"
        f'<li><a href="/roles/data-manager?ref=search&amp;{functional}">Data Manager</a></li>'
        "</ul></main>"
    )
    result = inspect_publisher_page(SOURCE, page)
    assert result.classification is not PageClassification.COLLECTION
    assert {link.cluster_size for link in result.candidate_links} == {1}
    assert all(functional in link.fetch_url for link in result.candidate_links)
    assert all("ref=search" in link.fetch_url for link in result.candidate_links)
    assert all(
        "ref" not in link.target_shape.split("?")[-1].split("&") for link in result.candidate_links
    )


@pytest.mark.parametrize("hidden", ["hidden", 'aria-hidden="true"', 'style="display:none"'])
def test_hidden_account_controls_do_not_block_a_readable_frameless_collection(hidden: str) -> None:
    rows = "".join(
        f'<li><a href="/roles/analyst-{number}">Clinical Data Analyst {number}</a>'
        "<p>Remote full time research reporting and regulated data review.</p></li>"
        for number in range(4)
    )
    page = (
        f"<section><ul>{rows}</ul></section>"
        f'<div {hidden}><form><h2>Access denied</h2><input type="password"></form></div>'
    )
    result = inspect_publisher_page(SOURCE, page)
    assert result.classification is PageClassification.COLLECTION
    assert "Access denied" not in result.visible_text
    assert "short page carries an error or challenge signal" not in result.evidence


@pytest.mark.parametrize(
    "wall",
    [
        '<main><h1>Sign in</h1><form><input type="password"><button>Sign in</button></form></main>',
        "<main><h1>Access denied</h1><p>Complete the captcha to continue.</p></main>",
        '<header><h1>Sign in</h1><form><input type="password">'
        "<button>Sign in</button></form></header>",
    ],
)
def test_visible_login_and_challenge_walls_remain_refused(wall: str) -> None:
    result = inspect_publisher_page(SOURCE, wall)
    assert result.classification is PageClassification.ERROR_OR_CHALLENGE
    assert result.posting_body_markup == ""


def test_an_owned_description_beside_related_jobs_survives_hidden_login_and_invalid_jsonld() -> (
    None
):
    body = (
        '<section class="job-description-bg"><h2>Job Description</h2>'
        + _POSTING_SECTIONS
        + '<a href="https://apply.example/roles/4101">Apply</a></section>'
    )
    page = (
        "<title>Clinical Data Manager - Acme</title><h1>Clinical Data Manager</h1>"
        + body
        + f"<section>{_SIMILAR_JOBS}</section>"
        + '<div aria-hidden="true"><form><input type="password"></form></div>'
        + '<script type="application/ld+json">'
        '{"@type":"JobPosting", /* invalid JSON */ "title":"Wrong declared title"}'
        "</script>"
    )
    result = inspect_publisher_page("https://publisher.example/jobs/4101/data-manager", page)
    assert result.classification is PageClassification.POSTING
    assert result.title == "Clinical Data Manager"
    assert result.posting_body_markup == body
    assert "Similar jobs" not in result.posting_body_markup
    assert "publisher declared JobPosting structured data" not in result.evidence
    assert "publisher structured data is not valid JSON" in result.evidence


@pytest.mark.parametrize("role_count", [1, 2])
def test_inline_role_headings_claim_only_one_independent_posting_body(role_count: int) -> None:
    sections = "".join(
        f"<section><h2>{role}</h2>{_POSTING_SECTIONS}"
        f'<h3>How to apply</h3><a href="mailto:careers@example.com">Apply for {role}</a></section>'
        for role in ("Research Associate", "Clinical Data Manager")[:role_count]
    )
    page = f"<title>Join the Acme team</title><h1>Join the Acme team</h1>{sections}"
    result = inspect_publisher_page(SOURCE, page)
    if role_count == 1:
        assert result.classification is PageClassification.POSTING
        assert result.title == "Research Associate"
        assert result.posting_body_markup == sections
    else:
        assert result.classification is not PageClassification.POSTING
        assert result.title == ""
        assert result.posting_body_markup == ""


@pytest.mark.parametrize("related_heading", ["h2", "h3"])
def test_linked_related_headings_do_not_duplicate_an_external_role_heading(
    related_heading: str,
) -> None:
    body = (
        '<section class="job-description-bg"><h2>Job Description</h2>'
        + _POSTING_SECTIONS
        + '<a href="https://apply.example/roles/4101">Apply</a></section>'
    )
    cards = "".join(
        f'<li><a href="/jobs/details/{number}"><{related_heading}>'
        f"Clinical Data Manager</{related_heading}></a></li>"
        for number in (4102, 4103)
    )
    page = (
        "<title>Clinical Data Manager - Acme</title><h1>Clinical Data Manager</h1>"
        + body
        + f"<section><h2>Related jobs</h2><ul>{cards}</ul></section>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Clinical Data Manager"
    assert result.posting_body_markup == body
    assert "Related jobs" not in result.posting_body_markup


@pytest.mark.parametrize("body_class", ["job-description-bg", "job-description"])
@pytest.mark.parametrize("nested", [False, True], ids=["single", "nested"])
@pytest.mark.parametrize("heading", ["Clinical Data Manager", "Widget Faculty"])
def test_one_identified_named_description_needs_no_exact_section_phrases(
    body_class: str, nested: bool, heading: str
) -> None:
    prose = _POSTING_SECTIONS.replace("Responsibilities", "Your daily work").replace(
        "Qualifications", "Experience you bring"
    )
    body = (
        f'<section class="{body_class}"><h2>Job Description</h2>'
        + prose
        + '<a href="https://apply.example/roles/4101">Apply</a></section>'
    )
    frame = f'<div class="job-description">{body}</div>' if nested else body
    page = (
        '<link rel="canonical" href="/jobs/details/4101">'
        f"<title>{heading} - Acme</title><h1>{heading}</h1>"
        + frame
        + f"<section>{_SIMILAR_JOBS}</section>"
        + '<div aria-hidden="true"><form><h2>Access denied</h2>'
        '<input type="password"></form></div>' + '<script type="application/ld+json">'
        '{"@type":"JobPosting", /* invalid JSON */ "title":"Wrong declared title"}'
        "</script>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == heading
    assert result.posting_body_markup == body
    assert "Similar jobs" not in result.posting_body_markup
    assert "Access denied" not in result.visible_text
    assert "publisher structured data is not valid JSON" in result.evidence
    assert "publisher declared JobPosting structured data" not in result.evidence


@pytest.mark.parametrize("body_class", ["job-description-bg", "job-description"])
@pytest.mark.parametrize("section", ["Benefits", "Compensation", "How to apply", "Daily work"])
def test_an_identified_named_body_needs_no_role_section_vocabulary(
    body_class: str, section: str
) -> None:
    prose = _POSTING_SECTIONS.replace("Responsibilities", section).replace(
        "Qualifications", "Experience you bring"
    )
    body = (
        f'<div class="{body_class}">'
        + prose
        + '<a href="https://apply.example/roles/4101">Apply</a></div>'
    )
    page = (
        '<link rel="canonical" href="/jobs/details/4101">'
        + "<title>Widget Faculty - Acme</title><h1>Widget Faculty</h1>"
        + body
        + f"<section>{_SIMILAR_JOBS}</section>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Widget Faculty"
    assert result.posting_body_markup == body
    assert "Similar jobs" not in result.posting_body_markup


@pytest.mark.parametrize(
    ("source", "canonical", "heading", "body_class", "apply", "substantive"),
    [
        (
            "https://publisher.example/jobs/details/4101",
            "/jobs/details/4101",
            "Careers",
            "job-description-bg",
            True,
            True,
        ),
        (
            "https://publisher.example/careers",
            "/careers",
            "Clinical Data Manager",
            "job-description-bg",
            True,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "",
            "Clinical Data Manager",
            "job-description-bg",
            True,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "/jobs/details/4102",
            "Clinical Data Manager",
            "job-description-bg",
            True,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "https://other.example/jobs/details/4101",
            "Clinical Data Manager",
            "job-description-bg",
            True,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "/jobs/details/4101",
            "Clinical Data Manager",
            "description",
            True,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "/jobs/details/4101",
            "Clinical Data Manager",
            "job-description-bg",
            False,
            True,
        ),
        (
            "https://publisher.example/jobs/details/4101",
            "/jobs/details/4101",
            "Clinical Data Manager",
            "job-description-bg",
            True,
            False,
        ),
    ],
    ids=[
        "generic-heading",
        "no-identifier",
        "no-canonical",
        "canonical-path",
        "canonical-host",
        "unnamed-body",
        "no-apply",
        "short-body",
    ],
)
def test_a_named_description_without_its_own_identity_and_body_stays_unverified(
    source: str,
    canonical: str,
    heading: str,
    body_class: str,
    apply: bool,
    substantive: bool,
) -> None:
    prose = (
        _POSTING_SECTIONS.replace("Responsibilities", "Your daily work").replace(
            "Qualifications", "Experience you bring"
        )
        if substantive
        else "<p>Details follow soon.</p>"
    )
    application = '<a href="https://apply.example/roles/4101">Apply</a>' if apply else ""
    body = f'<section class="{body_class}"><h2>Job Description</h2>{prose}{application}</section>'
    page = (
        f'<link rel="canonical" href="{canonical}">'
        f"<title>{heading} - Acme</title><h1>{heading}</h1>"
        + body
        + f"<section>{_SIMILAR_JOBS}</section>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""
    assert result.posting_body_markup == ""


@pytest.mark.parametrize("section_phrases", [False, True], ids=["prose", "sections"])
def test_independent_named_descriptions_cannot_share_one_external_role_heading(
    section_phrases: bool,
) -> None:
    prose = (
        _POSTING_SECTIONS
        if section_phrases
        else _POSTING_SECTIONS.replace("Responsibilities", "Your daily work").replace(
            "Qualifications", "Experience you bring"
        )
    )
    bodies = "".join(
        '<section class="job-description-bg"><h2>Job Description</h2>'
        + prose
        + f'<a href="https://apply.example/roles/{number}">Apply</a></section>'
        for number in (4101, 4102)
    )
    page = (
        '<link rel="canonical" href="/jobs/details/4101">'
        "<title>Clinical Data Manager - Acme</title><h1>Clinical Data Manager</h1>"
        + bodies
        + f"<section>{_SIMILAR_JOBS}</section>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""
    assert result.posting_body_markup == ""


@pytest.mark.parametrize("application", ["/roles/4102", "/roles/4101?requisition=4102"])
def test_nested_independent_descriptions_with_distinct_applications_stay_unverified(
    application: str,
) -> None:
    inner = (
        '<section class="job-description-bg"><h2>Job Description</h2>'
        + _POSTING_SECTIONS
        + f'<a href="{application}">Apply</a></section>'
    )
    outer = (
        '<section class="job-description">'
        + _POSTING_SECTIONS
        + '<a href="/roles/4101">Apply</a>'
        + inner
        + "</section>"
    )
    page = (
        '<link rel="canonical" href="/jobs/details/4101">'
        "<title>Clinical Data Manager - Acme</title><h1>Clinical Data Manager</h1>"
        + outer
        + f"<section>{_SIMILAR_JOBS}</section>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is not PageClassification.POSTING
    assert result.posting_body_markup == ""


def test_unlinked_independent_role_headings_are_not_collapsed_by_caption() -> None:
    body = (
        '<section class="job-description-bg"><h2>Job Description</h2>'
        + _POSTING_SECTIONS
        + '<a href="https://apply.example/roles/4101">Apply</a></section>'
    )
    page = (
        "<title>Clinical Data Manager - Acme</title><h1>Clinical Data Manager</h1>"
        "<h1>Clinical Data Manager</h1>" + body + f"<section>{_SIMILAR_JOBS}</section>"
    )

    result = inspect_publisher_page("https://publisher.example/jobs/details/4101", page)

    assert result.classification is not PageClassification.POSTING
    assert result.posting_body_markup == ""


def test_visible_label_is_copied_before_fallback_label() -> None:
    page = """
    <main><ul>
      <li><a href="/roles/one" aria-label="Fallback one">Visible one</a></li>
      <li><a href="/roles/two" title="Fallback two"></a></li>
    </ul></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert [(link.label, link.label_source) for link in result.candidate_links] == [
        ("Visible one", "visible"),
        ("Fallback two", "title"),
    ]


@pytest.mark.parametrize("anchor_text", ["", "Browse current roles"])
def test_an_image_only_link_uses_nonempty_alt_after_anchor_labels(anchor_text: str) -> None:
    page = '<main><a href="/jobs/search/"><span><img src="/jobs.png" '
    page += f'alt="View all jobs"></span>{anchor_text}</a></main>'

    result = inspect_publisher_page(SOURCE, page)

    assert len(result.candidate_links) == 1
    assert result.candidate_links[0].label == (anchor_text or "View all jobs")


@pytest.mark.parametrize("attribute", ['aria-label="Open vacancies"', 'title="Open vacancies"'])
def test_an_anchor_label_takes_precedence_over_nested_image_alt(attribute: str) -> None:
    page = f'<main><a href="/jobs/search/" {attribute}><img src="/jobs.png" '
    page += 'alt="View all jobs"></a></main>'

    result = inspect_publisher_page(SOURCE, page)

    assert result.candidate_links[0].label == "Open vacancies"


@pytest.mark.parametrize("alt", ["", "   "])
def test_an_empty_image_alt_does_not_supply_a_link_label(alt: str) -> None:
    result = inspect_publisher_page(
        SOURCE, f'<main><a href="/jobs/search/"><img src="/jobs.png" alt="{alt}"></a></main>'
    )

    assert result.candidate_links[0].label == ""
    assert result.candidate_links[0].label_source == "none"


def test_external_handoff_is_recorded_but_footer_link_is_not_a_candidate() -> None:
    page = """
    <main><div class="card"><a href="https://apply.example/v/1">Analyste A</a></div>
    <div class="card"><a href="https://apply.example/v/2">Analyste B</a></div></main>
    <footer><a href="https://social.example/profile">Community</a></footer>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert len(result.candidate_links) == 2
    assert all("external-handoff" in link.relations for link in result.candidate_links)
    assert result.links[-1].structural_context == "footer"
    assert "navigation-chrome" in result.links[-1].relations
    assert result.links[-1] not in result.candidate_links


def test_text_outside_chrome_leaves_out_header_navigation_and_footer() -> None:
    page = """
    <html><head><title>Careers at Northwind</title><style>p{color:red}</style></head><body>
    <header><a href="/">Home</a></header><div role="navigation"><a href="/team">Team</a></div>
    <main><h1>Join Northwind</h1><p>We build route planning tools.</p></main>
    <footer><p>Copyright Northwind</p></footer></body></html>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.text_outside_chrome == (
        "Careers at Northwind Join Northwind We build route planning tools."
    )


def test_role_page_requires_more_than_a_primary_heading() -> None:
    result = inspect_publisher_page(SOURCE, "<main><h1>Director of Data</h1></main>")

    assert result.classification is PageClassification.AMBIGUOUS
    assert result.title == ""


def test_referred_role_page_with_substantive_sections_is_a_posting() -> None:
    collection = inspect_publisher_page(
        SOURCE,
        """
        <main><ul><li><a href="/roles/one">Director One</a></li>
        <li><a href="/roles/two">Director Two</a></li></ul></main>
        """,
    )
    detail = """
    <main><article><h1>Director One</h1><h2>Mandate</h2>
    <p>This leadership mandate coordinates a national data program, establishes delivery
    priorities, works across multiple partner organizations, and remains accountable for
    measurable outcomes throughout the portfolio.</p>
    <h2>Experience</h2><p>Candidates bring extensive program leadership and data experience,
    communicate with executives, guide multidisciplinary teams, and improve services.</p>
    </article></main>
    """

    result = inspect_publisher_page(
        collection.candidate_links[0].fetch_url,
        detail,
        referring_link=collection.candidate_links[0],
    )

    assert result.classification is PageClassification.POSTING
    assert result.title == "Director One"


def test_four_navigation_destinations_do_not_become_postings() -> None:
    for destination in ("Work here", "Benefits", "Culture", "Contact"):
        page = f"""
        <header><nav><a href="/about">About</a><a href="/services">Services</a></nav></header>
        <main><h1>{destination}</h1></main>
        <footer><a href="/privacy">Privacy</a></footer>
        """
        result = inspect_publisher_page(SOURCE, page)
        assert result.classification is PageClassification.NAVIGATION
        assert result.title == ""


def test_error_heading_is_not_mistaken_for_a_posting() -> None:
    result = inspect_publisher_page(
        SOURCE,
        "<title>Access denied</title><main><h1>Request unavailable</h1><p>Reference 53</p></main>",
    )

    assert result.classification is PageClassification.ERROR_OR_CHALLENGE
    assert result.title == ""


def test_a_sign_in_form_that_asks_for_a_password_is_a_challenge_not_a_posting() -> None:
    page = """
    <html><head><title>Career Opportunities: Sign In</title></head><body>
    <h1>Career Opportunities: Sign In</h1>
    <form action="/career"><p>Already have an account?</p>
    <label>Email Address:</label><input type="text" name="username">
    <label>Password:</label><input type="password" name="password">
    <button>Sign In</button><a href="/forgot">Forgot your password?</a>
    <p>Not a registered user yet? <a href="/register">Create an account</a>
    to apply for our career opportunities.</p></form></body></html>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.ERROR_OR_CHALLENGE


def test_a_posting_with_a_sign_in_box_in_its_header_is_still_a_posting() -> None:
    page = """
    <html><head><title>Clinical Data Manager | Acme Health</title></head><body>
    <header><form action="/login"><input type="text" name="user">
    <input type="password" name="pass"><button>Sign in</button></form></header>
    <main><h1>Clinical Data Manager</h1>
    <h2>Responsibilities</h2><p>Own the clinical data management plan for each study,
    review edit checks with biostatistics, and reconcile external vendor data before lock.</p>
    <h2>Qualifications</h2><p>Five years in clinical data management, working knowledge of
    CDISC standards, and experience running user acceptance testing on EDC builds.</p>
    </main></body></html>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING


_POSTING_BODY = """
<h2>Responsibilities</h2><p>Design the ingestion services for the example platform, review
schema changes with the analytics group, and keep the nightly loads within their window.</p>
<h2>Qualifications</h2><p>Five years building data services, working knowledge of Python
and SQL, and experience running production systems with an on-call rotation.</p>
"""


@pytest.mark.parametrize(
    ("page_title", "headings", "expected"),
    [
        pytest.param(
            "Senior Example Engineer in Remote, United States",
            "<header><h1>Senior Example Engineer</h1></header><main>"
            "<h1><b><span>Senior Example Engineer</span></b></h1>"
            "<h1><span>About us:</span></h1>",
            "Senior Example Engineer",
            id="header-duplicate-and-section-h1s",
        ),
        pytest.param(
            "",
            "<main><h1><b>Senior</b> <span>Example Engineer</span></h1>",
            "Senior Example Engineer",
            id="one-h1-split-across-inline-tags",
        ),
        pytest.param(
            "  senior  EXAMPLE engineer | Example Co",
            "<main><h1>Example Co Careers</h1><h1>Senior Example Engineer</h1>",
            "Senior Example Engineer",
            id="page-title-names-the-second-h1",
        ),
        pytest.param(
            "Open roles at Example Co",
            "<main><h1>Senior Example Engineer</h1><h1>Staff Example Analyst</h1>",
            "Senior Example Engineer",
            id="no-page-title-match-takes-the-first-h1",
        ),
    ],
)
def test_a_page_with_several_h1s_is_titled_by_one_of_them(
    page_title: str, headings: str, expected: str
) -> None:
    page = (
        f"<html><head><title>{page_title}</title></head><body>"
        f"{headings}{_POSTING_BODY}</main></body></html>"
    )

    result = inspect_publisher_page("https://careers.example/jobs/4100", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == expected


def test_bundled_script_vocabulary_is_not_an_error_signal() -> None:
    page = """
    <html><head><script>window.__flags = {"captcha": true, "errorBoundary": "robot"};</script>
    <style>.error { color: red; }</style></head>
    <body><h1>Careers</h1><div id="jobs"></div></body></html>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.ERROR_OR_CHALLENGE


def test_opaque_ids_before_a_slug_share_one_target_shape() -> None:
    page = """
    <main><ul>
      <li><a href="/apply/GjbNrhsqCw/Applied-AI-Engineer">Applied AI Engineer</a></li>
      <li><a href="/apply/Rk10TWwIhg/Project-Delivery-Lead">Project Delivery Lead</a></li>
      <li><a href="/jobs/48213/data-steward">Data Steward</a></li>
      <li><a href="/about/leadership/board">Our board</a></li>
      <li><a href="/about/leadership/history">Our history</a></li>
    </ul></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    sizes = {link.label: link.cluster_size for link in result.candidate_links}
    assert sizes["Applied AI Engineer"] == 2
    assert sizes["Project Delivery Lead"] == 2
    assert sizes["Data Steward"] == 1
    assert sizes["Our board"] == 2


def test_javascript_shell_is_reported_separately_from_an_empty_collection() -> None:
    result = inspect_publisher_page(
        SOURCE,
        '<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>',
    )

    assert result.classification is PageClassification.JS_SHELL
    assert "page has scripts but no publisher-readable content" in result.evidence


@pytest.mark.parametrize(
    ("source", "current", "expected"),
    [
        (
            "https://publisher.example/jobs/search/7654321",
            "1",
            "https://publisher.example/jobs/search/7654321/page2",
        ),
        ("https://publisher.example/jobs/search/7654321/page2", "2", None),
        (
            "https://publisher.example/jobs/search/7654321?sort=date",
            "1",
            "https://publisher.example/jobs/search/7654321/page2?sort=date",
        ),
    ],
)
def test_published_search_metadata_exposes_only_the_next_remaining_page(
    source: str, current: str, expected: str | None
) -> None:
    page = '<div class="jResultsContent" data-jsid="7654321">'
    page += '<div class="job_list_row"><a href="/jobs/analyst-101">Data Analyst</a></div>'
    page += '<div class="job_list_row"><a href="/jobs/analyst-102">Clinical Analyst</a></div>'
    page += (
        f'<div id="jPaginateNumPages">2.0</div><div id="jPaginateCurrPage">{current}</div></div>'
    )
    page += '<script>loader.script("/job_list.js?v=" + app.version);</script>'

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.COLLECTION
    next_links = [link for link in result.candidate_links if link.label == "Next page"]
    assert [link.fetch_url for link in next_links] == ([] if expected is None else [expected])


@pytest.mark.parametrize(
    ("source", "search_id", "total", "current", "script", "results_class"),
    [
        (
            "https://publisher.example/jobs/search/7654321",
            "7654321",
            "many",
            "1",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321",
            "7654321",
            "2.5",
            "1",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321",
            "7654321",
            "2",
            "0",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321",
            "7654321",
            "2",
            "3",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321/page2",
            "7654321",
            "3",
            "1",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321",
            "7654322",
            "2",
            "1",
            True,
            "jResultsContent",
        ),
        (
            "https://publisher.example/jobs/search/7654321",
            "7654321",
            "2",
            "1",
            False,
            "jResultsContent",
        ),
        ("https://publisher.example/careers", "7654321", "2", "1", True, "jResultsContent"),
        ("https://publisher.example/jobs/search/7654321", "7654321", "2", "1", True, "results"),
    ],
)
def test_uncorroborated_search_metadata_does_not_guess_a_next_page(
    source: str, search_id: str, total: str, current: str, script: bool, results_class: str
) -> None:
    page = f'<div class="{results_class}" data-jsid="{search_id}">'
    page += (
        f'<div id="jPaginateNumPages">{total}</div>'
        f'<div id="jPaginateCurrPage">{current}</div></div>'
    )
    if script:
        page += '<script>loader.script("/job_list.js?v=" + app.version);</script>'

    result = inspect_publisher_page(source, page)

    assert not any(link.label == "Next page" for link in result.candidate_links)


@pytest.mark.parametrize(
    "script_source", ["http://[broken", "https://external.example/job_list.js", "/unrelated.js"]
)
def test_a_search_with_an_unusable_or_external_script_has_no_derived_next_page(
    script_source: str,
) -> None:
    page = '<div class="jResultsContent" data-jsid="7654321">'
    page += '<div id="jPaginateNumPages">2</div><div id="jPaginateCurrPage">1</div></div>'
    page += f'<script src="{script_source}"></script>'

    result = inspect_publisher_page("https://publisher.example/jobs/search/7654321", page)

    assert not any(link.label == "Next page" for link in result.candidate_links)


@pytest.mark.parametrize("duplicate", ["results", "current", "total", "outside", "chrome"])
def test_search_pagination_requires_one_results_block_with_unique_nested_metadata(
    duplicate: str,
) -> None:
    metadata = '<div id="jPaginateNumPages">2</div><div id="jPaginateCurrPage">1</div>'
    extra = '<div id="jPaginateCurrPage">1</div>' if duplicate == "current" else ""
    extra += '<div id="jPaginateNumPages">2</div>' if duplicate == "total" else ""
    page = '<div class="jResultsContent" data-jsid="7654321">'
    page += (metadata + extra if duplicate != "outside" else "") + "</div>"
    if duplicate == "outside":
        page += metadata
    if duplicate == "results":
        page += '<div class="jResultsContent" data-jsid="7654321">' + metadata + "</div>"
    page += '<script src="/job_list.js?v=synthetic"></script>'
    if duplicate == "chrome":
        page = "<nav>" + page + "</nav>"

    result = inspect_publisher_page("https://publisher.example/jobs/search/7654321", page)

    assert not any(link.label == "Next page" for link in result.candidate_links)


def test_a_derived_search_page_preserves_a_referrer_prefix_and_query_without_a_fragment() -> None:
    page = '<div class="jResultsContent" data-jsid="7654321">'
    page += '<div id="jPaginateNumPages">3</div><div id="jPaginateCurrPage">2</div></div>'
    page += '<script src="/job_list.js?v=synthetic"></script>'

    result = inspect_publisher_page(
        "https://publisher.example/ref/jobs/search/7654321/page2?sort=date#results", page
    )

    assert [link.fetch_url for link in result.candidate_links] == [
        "https://publisher.example/ref/jobs/search/7654321/page3?sort=date"
    ]


def test_resume_upload_is_an_application_channel_not_a_posting() -> None:
    result = inspect_publisher_page(
        SOURCE,
        '<main><h1>Submit your resume</h1><form><input type="file" name="resume"></form></main>',
    )

    assert result.classification is PageClassification.APPLICATION_CHANNEL
    assert result.title == ""


def test_a_long_general_article_with_headings_is_not_mistaken_for_a_posting() -> None:
    page = """
    <main><article><h1>Northwind Patient Resources</h1><h2>Overview</h2>
    <p>This program supports patients and families across the region through every
    stage of treatment, coordinating specialists, community resources, and follow-up
    care so that no one navigates a diagnosis alone.</p>
    <h2>Resources</h2><p>Printed guides, a support line, and a directory of local
    services are available to anyone affected by a diagnosis in the region.</p>
    </article></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING


@pytest.mark.parametrize("section", ["Benefits", "Compensation", "How to apply"])
@pytest.mark.parametrize("placement", ["heading", "prose"])
@pytest.mark.parametrize("uppercase", [False, True], ids=["title-case", "uppercase"])
def test_generic_careers_sections_do_not_claim_a_posting(
    section: str, placement: str, uppercase: bool
) -> None:
    label = section.upper() if uppercase else section
    section_markup = f"<h2>{label}</h2>" if placement == "heading" else f"<p>{label}</p>"
    page = (
        "<main><h1>Join The Acme Team</h1><h2>Why Join Acme</h2><p>"
        + "Our people build a welcoming community and support each other's growth. " * 12
        + "</p><h2>Who We Are Looking For</h2><p>"
        + "We welcome curious people who enjoy collaboration across our teams. " * 12
        + "</p><h2>Inclusion</h2><p>Everyone belongs in our community.</p>"
        + section_markup
        + "<p>Learn about our workplace and the support available to all team members.</p>"
        + '<h2>Current Opportunities</h2><a href="/career-posting/">View Open Positions</a>'
        + "</main>"
    )

    result = inspect_publisher_page("https://publisher.example/careers/", page)

    assert result.classification is not PageClassification.POSTING
    assert result.posting_body_markup == ""


def test_a_site_wide_apply_link_in_navigation_does_not_make_an_article_a_posting() -> None:
    page = """
    <header><nav><a href="/admissions/apply">Apply now</a></nav></header>
    <main><article><h1>Northwind Patient Resources</h1><h2>Overview</h2>
    <p>This program supports patients and families across the region through every
    stage of treatment, coordinating specialists, community resources, and follow-up
    care so that no one navigates a diagnosis alone.</p>
    <h2>Resources</h2><p>Printed guides, a support line, and a directory of local
    services are available to anyone affected by a diagnosis in the region.</p>
    </article></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING


def test_a_repeated_shape_probe_of_a_general_article_is_not_a_posting() -> None:
    page = """
    <main><article><h1>Northwind Patient Resources</h1><h2>Overview</h2>
    <p>This program supports patients and families across the region through every
    stage of treatment, coordinating specialists, community resources, and follow-up
    care so that no one navigates a diagnosis alone.</p>
    <h2>Resources</h2><p>Printed guides, a support line, and a directory of local
    services are available to anyone affected by a diagnosis in the region.</p>
    </article></main>
    """
    referring_link = PublisherLink(
        fetch_url="https://publisher.example/info/page-1040",
        label="Northwind Patient Resources",
        label_source="visible",
        source_url=SOURCE,
        structural_context="list",
        target_shape="publisher.example/info/{item}",
        cluster_size=4,
        relations=(
            LinkRelation.SAME_ORIGIN,
            LinkRelation.PUBLISHER_CONTENT,
            LinkRelation.SUBSTANTIVE_LABEL,
            LinkRelation.REPEATED_TARGET_SHAPE,
        ),
    )

    result = inspect_publisher_page(SOURCE, page, referring_link=referring_link)

    assert result.classification is not PageClassification.POSTING


@pytest.mark.parametrize(
    "heading",
    [
        "Discover Your Career",
        "Career Center",
        "Employer of Choice",
        "Investor Relations",
        "Customer Stories",
        "Newsletter",
    ],
)
def test_a_repeated_shape_probe_of_a_marketing_page_is_not_a_posting(heading: str) -> None:
    page = f"""
    <main><article><h1>{heading}</h1><h2>Overview</h2>
    <p>Northwind Cooperative publishes plain-language pages about the work it does across
    the region, how each team supports the communities it serves, and where to find more
    detail about the cooperative and the people who belong to it.</p>
    <h2>Getting in touch</h2><p>Use the directory to reach a team near you and the hours
    that it keeps throughout the week.</p>
    </article></main>
    """
    referring_link = PublisherLink(
        fetch_url="https://publisher.example/about/page-1040",
        label=heading,
        label_source="visible",
        source_url=SOURCE,
        structural_context="list",
        target_shape="publisher.example/about/{item}",
        cluster_size=4,
        relations=(
            LinkRelation.SAME_ORIGIN,
            LinkRelation.PUBLISHER_CONTENT,
            LinkRelation.SUBSTANTIVE_LABEL,
            LinkRelation.REPEATED_TARGET_SHAPE,
        ),
    )

    result = inspect_publisher_page(SOURCE, page, referring_link=referring_link)

    assert result.classification is not PageClassification.POSTING


def test_an_occupation_title_with_structure_but_no_section_words_is_a_posting() -> None:
    page = """
    <main><article><h1>Senior Software Engineer</h1><h2>Overview</h2>
    <p>This position builds critical systems that support clinical operations across
    multiple regions, collaborates with data teams, and improves platform reliability
    for thousands of daily users needing dependable access.</p>
    <h2>Team</h2><p>The successful candidate partners closely with product managers,
    mentors other engineers, and contributes to architecture decisions that shape the
    roadmap for the coming year.</p>
    </article></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING


def test_a_non_english_posting_with_an_apply_link_and_no_section_words_is_a_posting() -> None:
    page = """
    <main><article><h1>Responsable de Programme Regional</h1><h2>Apercu</h2>
    <p>Ce poste supervise plusieurs equipes regionales et coordonne les priorites
    operationnelles avec les partenaires locaux, en assurant un suivi rigoureux des
    resultats mesurables tout au long de l'annee.</p>
    <h2>Equipe</h2><p>La personne retenue travaillera avec des collegues
    multidisciplinaires et rendra compte regulierement a la direction generale.</p>
    <a href="/postuler">Envoyer votre CV</a>
    </article></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING


def test_posting_section_words_with_structure_still_corroborate_a_posting() -> None:
    page = """
    <main><article><h1>Program Director</h1><h2>Overview</h2>
    <p>This role leads network programs and reports to the vice president of
    operations across every regional site.</p>
    <h2>Qualifications</h2><p>Candidates bring five years of relevant leadership
    experience and a track record of delivery.</p>
    </article></main>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING


_POSTING_SECTIONS = (
    "<h2>Responsibilities</h2><p>"
    + "Design and maintain governed data pipelines that feed clinical reporting. " * 8
    + "</p><h2>Qualifications</h2><p>"
    + "Five years building production data systems with a regulated analytics team. " * 8
    + "</p>"
)
_SIMILAR_JOBS = (
    "<h2>Similar jobs</h2><ul>"
    + "".join(
        f'<li><a href="/jobs/{number}/{slug}">{label}</a><span>Remote</span></li>'
        for number, slug, label in (
            (4101, "data-analyst", "Data Analyst"),
            (4102, "analytics-engineer", "Analytics Engineer"),
            (4103, "platform-engineer", "Platform Engineer"),
            (4104, "data-architect", "Data Architect"),
            (4105, "reporting-analyst", "Reporting Analyst"),
            (4106, "database-administrator", "Database Administrator"),
        )
    )
    + "</ul>"
)


def test_a_posting_beside_a_similar_jobs_rail_is_a_posting() -> None:
    page = (
        "<header><nav><a href='/'>Home</a></nav></header><main>"
        f"<h1>Senior Data Engineer</h1>{_POSTING_SECTIONS}"
        f"<a href='/apply/4100'>Apply now</a>{_SIMILAR_JOBS}</main>"
    )

    result = inspect_publisher_page("https://board.example/jobs/4100/senior-data-engineer", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Data Engineer"


_VIEWJOB_URL = "https://board.example/viewjob?jk=f3e2d1c0b9a8"
_CITY_LINKS = (
    "<footer><ul>"
    + "".join(
        f'<li><a href="/q-jobs-l-{city}.html">Jobs in {city}</a></li>'
        for city in ("Springfield", "Riverton", "Lakeside", "Fairview", "Oakdale", "Hillcrest")
    )
    + "</ul></footer>"
)


def _marked_title_page(*, before_title: str = "") -> str:
    """A job view with no h1, no posting data, and its title in a marked fifth-level heading."""
    return (
        "<html><head><title>Senior Data Engineer - Springfield</title></head><body>"
        f"<div class='layout'>{before_title}<div class='view'>"
        '<h5 data-testid="vj-job-title">Senior Data Engineer</h5>'
        "<div>Northwind</div><div>Springfield</div>"
        f"<div>{_POSTING_SECTIONS.replace('h2', 'b')}</div>"
        f"</div></div>{_CITY_LINKS}</body></html>"
    )


def test_a_job_view_titled_by_a_marked_heading_beside_city_links_is_a_posting() -> None:
    result = inspect_publisher_page(_VIEWJOB_URL, _marked_title_page())

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Data Engineer"


@pytest.mark.parametrize(
    "labels",
    [
        pytest.param(
            ("Senior Data Engineer", "Data Analyst", "Analytics Engineer", "Platform Engineer"),
            id="preview-names-a-card",
        ),
        pytest.param(
            ("Data Analyst", "Analytics Engineer", "Platform Engineer", "Reporting Analyst"),
            id="preview-names-no-card",
        ),
    ],
)
def test_search_results_beside_a_marked_preview_of_one_result_stay_a_collection(
    labels: tuple[str, ...],
) -> None:
    cards = "".join(
        f'<div class="result-card"><h2><a href="/viewjob?jk=a0b0c{number}">{label}</a></h2>'
        "<span>Springfield</span></div>"
        for number, label in enumerate(labels)
    )

    result = inspect_publisher_page(
        "https://board.example/jobs?q=data+engineer&l=Springfield",
        _marked_title_page(before_title=cards),
    )

    assert result.classification is PageClassification.COLLECTION


def test_a_team_page_whose_long_introduction_names_posting_sections_stays_a_collection() -> None:
    introduction = (
        "Our engineers earn competitive compensation and strong benefits, and the "
        "requirements for each opening are listed on its own page. "
    ) * 10
    page = (
        "<main><h1>Software Engineering Careers</h1>"
        f"<p>{introduction}</p>{_SIMILAR_JOBS.replace('Similar jobs', 'Open roles')}</main>"
    )

    result = inspect_publisher_page("https://board.example/teams/software-engineering", page)

    assert result.classification is PageClassification.COLLECTION


def test_a_listing_whose_long_introduction_names_benefits_stays_a_collection() -> None:
    introduction = (
        "Northwind offers competitive compensation, generous benefits, and a team that "
        "cares about the communities it serves across the region. "
    ) * 10
    rows = "".join(
        f'<li><a href="/careers/roles/{number}">Program Role {number}</a>'
        f"<span>Springfield</span></li>"
        for number in range(12)
    )
    page = (
        f"<main><h1>Current openings</h1><h2>Life at Northwind</h2><p>{introduction}</p>"
        f"<h2>Open roles</h2><ul>{rows}</ul></main>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION


def test_search_results_under_an_occupation_heading_stay_a_collection() -> None:
    cards = "".join(
        f'<div class="result-card"><h3><a href="/jobs/{number}/data-engineer">'
        f"Data Engineer {number}</a></h3><p>Responsibilities include building pipelines; "
        f"requirements include SQL and three years of platform experience.</p></div>"
        for number in range(5000, 5020)
    )
    page = f"<main><h1>Data Engineer jobs</h1><h2>Filters</h2>{cards}</main>"

    result = inspect_publisher_page("https://board.example/search?q=data+engineer", page)

    assert result.classification is PageClassification.COLLECTION


_SITE_MENU_ITEMS = "".join(
    f'<li><a href="/{path}">{label}</a></li>'
    for path, label in (
        ("platform", "Platform"),
        ("our-team", "Our Team"),
        ("blog", "Blog"),
        ("jobs", "Jobs"),
    )
)


def _posting_beside_links(links: str, title: str) -> str:
    """A long, sectioned posting body after a block of repeated links."""
    return f"<body>{links}<main>{title}{_POSTING_SECTIONS}</main></body>"


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("<h1>Senior Analytics Consultant</h1>", id="plain-h1"),
        pytest.param(
            '<h1 data-testid="job-title">Senior Analytics Consultant</h1>', id="marked-h1"
        ),
        pytest.param(
            '<h3 data-testid="job-title">Senior Analytics Consultant</h3>', id="marked-h3"
        ),
    ],
)
@pytest.mark.parametrize(
    "path",
    ["senior-analytics-consultant-rcm/", "senior-analytics-consultant-2026-09/"],
    ids=["slug-without-digits", "slug-with-digits"],
)
@pytest.mark.parametrize(
    "menu",
    [
        pytest.param("<header><nav><ul>{}</ul></nav></header>", id="header-nav-ul"),
        pytest.param("<nav><ul>{}</ul></nav>", id="nav-ul"),
        pytest.param('<div class="navi"><ul>{}</ul></div>', id="div-ul"),
        pytest.param("<footer><ul>{}</ul></footer>", id="footer-ul"),
    ],
)
def test_a_posting_beside_a_site_menu_is_a_posting(menu: str, path: str, title: str) -> None:
    page = _posting_beside_links(menu.format(_SITE_MENU_ITEMS), title)

    result = inspect_publisher_page(f"https://consultancy.example/jobs/{path}", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Analytics Consultant"


@pytest.mark.parametrize(
    "links",
    [
        pytest.param(
            (
                ("jobs/data-analyst", "Data Analyst"),
                ("jobs/analytics-engineer", "Analytics Engineer"),
                ("jobs/director-of-data", "Director of Data"),
            ),
            id="role-labels-without-ids",
        ),
        pytest.param(
            (
                ("jobs/4101/data-analyst", "Data Analyst"),
                ("jobs/4102/analytics-engineer", "Analytics Engineer"),
                ("jobs/4103/director-of-data", "Director of Data"),
            ),
            id="role-labels-with-ids",
        ),
        pytest.param(
            (
                ("jobs/4101", "Opening in Springfield"),
                ("jobs/4102", "Opening in Riverton"),
                ("jobs/4103", "Opening in Lakeside"),
            ),
            id="plain-labels-with-ids",
        ),
    ],
)
def test_a_category_page_beside_links_to_its_postings_stays_a_collection(
    links: tuple[tuple[str, str], ...],
) -> None:
    items = "".join(f'<li><a href="/{href}">{label}</a></li>' for href, label in links)
    page = _posting_beside_links(
        f"<div><ul>{items}</ul></div>", "<h1>Senior Analytics Consultant</h1>"
    )

    result = inspect_publisher_page("https://consultancy.example/careers/analytics/", page)

    assert result.classification is PageClassification.COLLECTION


def test_a_posting_titled_by_its_only_heading_the_page_title_repeats_is_a_posting() -> None:
    page = (
        "<html><head><title>Clinical Data Manager | Northwind Careers</title></head><body>"
        f"<main><h2>Clinical Data Manager</h2>{_POSTING_SECTIONS.replace('h2', 'h3')}"
        "</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Clinical Data Manager"


@pytest.mark.parametrize("title", ["Clinical Data Manager", "Head of Strategic Accounts"])
@pytest.mark.parametrize("inline_title", [False, True], ids=["plain", "inline"])
def test_a_role_heading_at_third_level_titles_its_substantive_posting(
    title: str, inline_title: bool
) -> None:
    heading = f"<span>{title}</span>" if inline_title else title
    page = (
        f"<html><head><title>{title} | Northwind Careers</title></head><body>"
        f"<main><h3>{heading}</h3>{_POSTING_SECTIONS.replace('h2', 'h3')}"
        "</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == title
    assert "page body contains substantive text" in result.evidence


def test_a_third_level_marketing_heading_does_not_title_a_posting() -> None:
    page = (
        "<html><head><title>Our teams | Northwind Careers</title></head><body>"
        f"<main><h3>Our teams</h3>{_POSTING_SECTIONS.replace('h2', 'h3')}"
        "</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


@pytest.mark.parametrize("heading", ["h2", "h3"])
def test_an_occupation_titled_index_with_role_cards_stays_a_collection(heading: str) -> None:
    cards = "".join(
        f'<div class="card"><a href="/careers/roles/engineer-{number}">'
        f"Data Engineer {number}</a><p>Build reporting systems for the platform.</p></div>"
        for number in range(3)
    )
    page = (
        "<html><head><title>Data Engineer | Northwind Careers</title></head><body>"
        f"<main><{heading}>Data Engineer</{heading}>"
        f"{_POSTING_SECTIONS}{cards}</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""


@pytest.mark.parametrize(
    "page_title",
    ["Northwind Careers", "Clinical Data Manager | Responsibilities | Northwind Careers"],
    ids=["heading-absent-from-title", "two-headings-in-title"],
)
def test_a_page_without_a_primary_heading_or_one_titled_second_heading_is_not_a_posting(
    page_title: str,
) -> None:
    page = (
        f"<html><head><title>{page_title}</title></head><body>"
        f"<main><h2>Clinical Data Manager</h2>{_POSTING_SECTIONS}</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING


def test_a_titled_second_heading_that_names_no_role_is_not_a_posting() -> None:
    page = (
        "<html><head><title>Benefits | Example Careers</title></head><body><main>"
        "<h2>Benefits</h2>"
        "<h3>Health</h3><p>"
        + "Medical, dental, and vision coverage starts on the first day of employment. " * 3
        + "</p><h3>Time off</h3><p>"
        + "Paid leave, company holidays, and a winter closure every December. " * 3
        + "</p></main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.AMBIGUOUS


@pytest.mark.parametrize("heading", ["h2", "h3"])
def test_a_repeated_title_heading_is_not_counted_as_a_section(heading: str) -> None:
    page = (
        "<html><head><title>Senior Data Engineer | Northwind Careers</title></head><body>"
        f"<main><{heading}>Senior Data Engineer</{heading}>"
        f"<{heading}>Senior Data Engineer</{heading}>"
        "<h3>Our team</h3><p>Northwind builds reporting tools for regional clinics.</p>"
        "</main></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.AMBIGUOUS
    assert "main content has multiple sections" not in result.evidence


def _plain_posting_page(apply_wrapper: str = "div") -> str:
    duties = "".join(
        f"<li><span>Own the widget roadmap for product line {number} end to end.</span></li>"
        for number in range(7)
    )
    background = "".join(
        f"<li><span>3+ years in widget analysis for area {number}.</span></li>"
        for number in range(4)
    )
    apply = '<a href="https://apply.example/j/123">Apply for this job</a>'
    return (
        "<html><head><title>Senior Widget Analyst | Example Staffing</title></head><body>"
        "<div><div><div>Share this job</div><div>Senior Widget Analyst</div>"
        f"<{apply_wrapper}>{apply}</{apply_wrapper}></div></div>"
        "<div><p><span>We are seeking a Senior Widget Analyst to lead widget reporting."
        "</span></p><p><span>What you'll do</span></p>"
        f"<ul>{duties}</ul><p><span>About you</span></p><ul>{background}</ul>"
        f"<p><{apply_wrapper}>{apply}</{apply_wrapper}></p></div>"
        "<div><span>Powered by</span></div></body></html>"
    )


@pytest.mark.parametrize("apply_wrapper", ["div", "li"], ids=["plain", "in-content"])
def test_a_plain_posting_titled_by_a_block_its_page_title_leads_with_is_a_posting(
    apply_wrapper: str,
) -> None:
    result = inspect_publisher_page(
        "https://acme.app.loxo.co/job/MTAwMTQtd2lkZ2V0cw==", _plain_posting_page(apply_wrapper)
    )

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Widget Analyst"
    assert "content names a posting section" in result.evidence
    assert "repeated substantive links share a target shape" not in result.evidence


def test_a_block_the_page_title_leads_with_that_names_no_role_does_not_title_a_page() -> None:
    page = (
        "<html><head><title>Our Team | Example Staffing</title></head><body>"
        "<div>Our Team</div><p>"
        + "What you'll do here depends on the client, and benefits vary by placement. " * 5
        + "</p></body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


def test_a_page_whose_address_names_no_posting_takes_no_title_from_a_block() -> None:
    page = (
        "<html><head><title>Software Engineer Jobs | Example Works</title></head><body>"
        "<div>Software Engineer Jobs</div><p>"
        + "Our engineers build scheduling tools, and what you'll do depends on the team. " * 5
        + "</p></body></html>"
    )

    result = inspect_publisher_page("https://careers.example.test/search?q=software", page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


_FRAMELESS_HEADER = (
    '<header><nav><a href="/">Home</a><a href="/jobs">Search jobs</a>'
    '<a href="/saved">Saved jobs</a><a href="/talent-network">Join our talent network</a>'
    '<a href="/portal">Candidate portal</a></nav></header>'
)
_FRAMELESS_FOOTER = (
    "<footer><div>"
    + "".join(
        f'<a href="/{area}/{topic}">{area.title()} {topic}</a>'
        for area in ("company", "support", "legal")
        for topic in ("overview", "contact", "privacy", "accessibility", "locations")
    )
    + "</div></footer>"
)
_FRAMELESS_DUTIES = (
    "Plan the quarterly enablement calendar with the product and field teams.",
    "Write and maintain the training guides that new staff rely on in their first month.",
    "Review course feedback each week and turn the findings into concrete revisions.",
    "Coordinate subject experts so each release ships with accurate reference material.",
)
_FRAMELESS_BACKGROUND = (
    "Five years building learning or documentation programs for a software product.",
    "Clear written communication and comfort presenting to senior stakeholders.",
    "Experience measuring program outcomes and reporting them to leadership.",
)


def _frameless_body(*, lists: bool, headings: str) -> str:
    def section(name: str, items: tuple[str, ...]) -> str:
        heading = f"<{headings}>{name}:</{headings}>"
        if headings == "strong":
            heading = f"<p>{heading}</p>"
        if lists:
            return heading + "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
        return heading + "".join(f"<p>{item}</p>" for item in items)

    return (
        "<p>Northwind is growing its enablement group and needs someone to own how teams "
        "learn the platform, from first-week onboarding to advanced certification.</p>"
        + section("Responsibilities", _FRAMELESS_DUTIES)
        + section("Qualifications", _FRAMELESS_BACKGROUND)
    )


def _frameless_page(title: str, main_section: str) -> str:
    """A posting laid out in plain divs between real header and footer chrome."""
    return (
        f"<html><head><title>{title} | Northwind Careers</title></head><body>"
        f"{_FRAMELESS_HEADER}<div class='page'><div><a href='/jobs'>Back to search results</a>"
        f"</div><section><h1>{title}</h1><div><span>Company: Northwind</span>"
        "<span>Location: Remote, United States</span><span>Date Posted: 2026-09-12</span>"
        "<span>Employment Type: Full Time</span><span>Job ID: R-1234</span></div>"
        "<div>Share: <a href='https://social-one.example/share?u=r-1234'>Share one</a>"
        "<a href='https://social-two.example/intent?u=r-1234'>Share two</a></div>"
        "<div><a href='/apply/R-1234'>Apply now</a><a href='/saved?add=R-1234'>Save job</a></div>"
        f"<div><h2>Description</h2>{main_section}</div></section></div>"
        f"{_FRAMELESS_FOOTER}</body></html>"
    )


@pytest.mark.parametrize(
    "title",
    [
        pytest.param("(Sr.) Advisory Lead, Learning Programs", id="leadership-title"),
        pytest.param("Content Enablement Partner", id="plain-title"),
        pytest.param("Senior Clinical Data Engineer", id="occupation-title"),
    ],
)
@pytest.mark.parametrize("lists", [True, False], ids=["lists", "paragraphs"])
@pytest.mark.parametrize("headings", ["strong", "h3"], ids=["strong-headings", "h3-headings"])
def test_a_posting_with_no_main_or_article_frame_is_a_posting(
    title: str, lists: bool, headings: str
) -> None:
    page = _frameless_page(title, _frameless_body(lists=lists, headings=headings))

    result = inspect_publisher_page("https://careers.example/jobs/enablement", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == title
    assert "page body contains substantive text" in result.evidence


@pytest.mark.parametrize("heading", ["h1", "h2", "h3"])
@pytest.mark.parametrize("menu_position", ["before", "after", "both"])
def test_plain_div_menus_outside_a_frameless_posting_preserve_its_body(
    heading: str, menu_position: str
) -> None:
    menu = (
        "<div>"
        + "".join(
            f'<a href="/company/{slug}">{label}</a>'
            for slug, label in (
                ("about", "About Northwind"),
                ("contact", "Contact us"),
                ("privacy", "Privacy policy"),
            )
        )
        + "</div>"
    )
    page = (
        "<html><head><title>Clinical Data Manager | Northwind Careers</title></head><body>"
        f"{menu if menu_position in {'before', 'both'} else ''}"
        f"<div><{heading}>Clinical Data Manager</{heading}>"
        f"{_POSTING_SECTIONS.replace('h2', 'h3')}</div>"
        f"{menu if menu_position in {'after', 'both'} else ''}</body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Clinical Data Manager"
    assert "page body contains substantive text" in result.evidence
    assert "Design and maintain governed data pipelines" in result.text_outside_chrome


@pytest.mark.parametrize("heading", ["h1", "h2", "h3"])
def test_plain_div_role_results_keep_an_occupation_titled_index_from_becoming_a_posting(
    heading: str,
) -> None:
    rows = "".join(
        f'<div><a href="/jobs/R-{number}/data-engineer">Data Engineer {number}</a>'
        "<p>Responsibilities include building pipelines; qualifications include "
        "production data experience.</p></div>"
        for number in range(4100, 4105)
    )
    page = (
        "<html><head><title>Data Engineer | Northwind Careers</title></head><body>"
        f"<div><{heading}>Data Engineer</{heading}>{_POSTING_SECTIONS}{rows}</div>"
        "</body></html>"
    )

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""
    assert "page body contains substantive text" not in result.evidence


@pytest.mark.parametrize(
    "title", ["Senior Graphic Designer", "Content Enablement Partner", "Clinical Data Manager"]
)
@pytest.mark.parametrize("rail_position", ["before", "after", "both"])
@pytest.mark.parametrize("rail_tag", ["div", "section", "ul"])
@pytest.mark.parametrize("section_tag", ["h2", "strong"])
def test_a_separate_frameless_role_body_survives_links_to_itself_and_sibling_roles(
    title: str, rail_position: str, rail_tag: str, section_tag: str
) -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    links = (
        f'<a href="/careers/roles/copper-leaf?utm_source=sidebar">{title}</a>'
        '<a href="/careers/roles/silver-branch">Partnerships Manager</a>'
    )
    rail = f"<{rail_tag}>{links}</{rail_tag}>"
    sections = _POSTING_SECTIONS + "".join(
        f"<h2>{name}</h2><p>{'Work with the project team to deliver useful resources. ' * 3}</p>"
        for name in ("About the role", "Requirements", "Benefits", "Compensation")
    )
    body = f'<div role="region"><h1>{title}</h1>{sections.replace("h2", section_tag)}</div>'
    page = (
        f"<html><head><title>{title} | Northwind</title></head><body>"
        f"{rail if rail_position in {'before', 'both'} else ''}{body}"
        f"{rail if rail_position in {'after', 'both'} else ''}</body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == title
    assert "page body contains substantive text" in result.evidence
    assert "Design and maintain governed data pipelines" in result.text_outside_chrome
    assert {link.fetch_url for link in result.links} == {
        source,
        "https://publisher.example/careers/roles/silver-branch",
    }


@pytest.mark.parametrize("section_tag", ["h2", "strong"])
@pytest.mark.parametrize(
    ("responsibilities", "qualifications"),
    [
        ("Key Responsibilities", "Qualifications"),
        ("Your responsibilities:", "Essential qualifications:"),
        ("KEY RESPONSIBILITIES", "REQUIRED QUALIFICATIONS"),
    ],
)
def test_a_separate_role_body_with_qualified_section_labels_is_a_posting(
    section_tag: str, responsibilities: str, qualifications: str
) -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    sections = (
        _POSTING_SECTIONS.replace("Responsibilities", responsibilities)
        .replace("Qualifications", qualifications)
        .replace("h2", section_tag)
    )
    page = (
        "<html><head><title>Senior Graphic Designer | Northwind</title></head><body>"
        '<ul><li><a href="/careers/roles/copper-leaf">Senior Graphic Designer</a></li>'
        '<li><a href="/careers/roles/silver-branch">Partnerships Manager</a></li></ul>'
        '<div role="region"><h1>Senior Graphic Designer</h1>'
        f"<{section_tag}>Job Summary</{section_tag}><p>Support the creative team.</p>"
        f"{sections}<{section_tag}>Working at Northwind</{section_tag}>"
        "<p>Join a collaborative team.</p>"
        f"<{section_tag}>Compensation</{section_tag}><p>Discuss pay with the team.</p>"
        f"<{section_tag}>How to Apply</{section_tag}><p>Send your portfolio.</p>"
        "</div></body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Graphic Designer"


@pytest.mark.parametrize("section_tag", ["h2", "strong"])
@pytest.mark.parametrize(
    ("summary", "duties", "qualifications", "application"),
    [
        ("Job Summary", "Duties and Tasks", "Qualifications", "How to Apply"),
        ("JOB SUMMARY:", "Duties & Tasks:", "Required qualifications:", "HOW TO APPLY:"),
        ("About the role", "Responsibilities", "Requirements", "How to apply"),
    ],
)
@pytest.mark.parametrize("contract", ["12-month contract", "Contract", "6 month contract"])
@pytest.mark.parametrize("rail_position", ["before", "after"])
def test_a_role_body_survives_its_contract_qualified_self_link(
    section_tag: str,
    summary: str,
    duties: str,
    qualifications: str,
    application: str,
    contract: str,
    rail_position: str,
) -> None:
    source = "https://publisher.example/careers/roles/silver-branch"
    title = "Partnerships Manager"
    sections = (
        _POSTING_SECTIONS.replace("Responsibilities", duties)
        .replace("Qualifications", qualifications)
        .replace("h2", section_tag)
    )
    rail = (
        '<ul><li><a href="/careers/roles/copper-leaf">Senior Graphic Designer</a></li>'
        f'<li><a href="{source}">{title} ({contract})</a></li></ul>'
    )
    body = (
        f"<div><h1>{title}</h1><{section_tag}>{summary}</{section_tag}>"
        f"<p>Own the partner program and coordinate its delivery.</p>{sections}"
        f"<{section_tag}>{application}</{section_tag}>"
        "<p>Send your resume and a letter describing your relevant experience.</p></div>"
    )
    page = (
        f"<html><head><title>{title} - English | Northwind</title></head><body>"
        f"{rail if rail_position == 'before' else ''}{body}"
        f"{rail if rail_position == 'after' else ''}</body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == title


@pytest.mark.parametrize("title_tag", ["h2", "h3"])
@pytest.mark.parametrize("section_tag", ["h2", "strong"])
@pytest.mark.parametrize("about_you", ["About You", "ABOUT YOU:", "Who you are"])
@pytest.mark.parametrize("application", ["section", "link"])
@pytest.mark.parametrize(
    "label_suffix",
    [
        "",
        " Full-Time See Job Details Full-Time",
        " Contract, Remote See Job Details Contract, Remote",
    ],
)
def test_a_referred_document_agreeing_heading_can_name_a_role_outside_the_title_vocabulary(
    title_tag: str,
    section_tag: str,
    about_you: str,
    label_suffix: str,
    application: str,
) -> None:
    title = "Customer Strategy & Operations"
    source = "https://publisher.example/careers/roles/blue-stone"
    root = inspect_publisher_page(
        SOURCE,
        f'<section><a href="{source}">{title}{label_suffix}</a>'
        '<a href="/careers/roles/green-stone">Program Coordinator</a></section>',
    )
    sections = (
        _POSTING_SECTIONS.replace("Responsibilities", "What you'll do")
        .replace("Qualifications", about_you)
        .replace("h2", section_tag)
    )
    application_markup = (
        f"<{section_tag}>How to Apply</{section_tag}>"
        if application == "section"
        else '<a href="/apply/blue-stone">Apply now</a>'
    )
    page = (
        f"<html><head><title>{title} | Northwind</title></head><body>"
        f"<div><{title_tag}>{title}</{title_tag}>{sections}"
        f"{application_markup}"
        "<p>Send a resume and a cover letter describing your experience.</p>"
        "</div></body></html>"
    )

    result = inspect_publisher_page(source, page, referring_link=root.candidate_links[0])

    assert result.classification is PageClassification.POSTING
    assert result.title == title


@pytest.mark.parametrize(
    "missing",
    [
        "referral",
        "matching-label",
        "longer-role-label",
        "matching-document",
        "long-body",
        "application",
        "role-sections",
        "section-labels",
        "owned-heading",
        "single-body",
    ],
)
def test_an_unrecognized_heading_needs_role_body_and_referring_evidence(missing: str) -> None:
    title = "Customer Strategy & Operations"
    source = "https://publisher.example/careers/roles/blue-stone"
    referring_label = title
    if missing == "matching-label":
        referring_label = "Our teams"
    elif missing == "longer-role-label":
        referring_label += " Support Analyst Full-Time See Job Details Full-Time"
    root = inspect_publisher_page(
        SOURCE,
        f'<section><a href="{source}">{referring_label}</a>'
        '<a href="/careers/roles/green-stone">Program Coordinator</a></section>',
    )
    sections = _POSTING_SECTIONS.replace("Responsibilities", "What you'll do").replace(
        "Qualifications", "About You"
    )
    if missing == "role-sections":
        sections = sections.replace("What you'll do", "Our services").replace(
            "About You", "Our values"
        )
    elif missing == "section-labels":
        sections = sections.replace("h2", "p")
    elif missing == "long-body":
        sections = "<strong>What you'll do</strong><p>Lead the program.</p>" + (
            "<strong>About You</strong><p>Bring relevant experience.</p>"
        )
    application = (
        ""
        if missing == "application"
        else "<strong>How to Apply</strong><p>Send your resume and a cover letter.</p>"
    )
    document_title = title if missing != "matching-document" else "Our teams"
    heading_markup = f"<h3>{title}</h3>"
    if missing == "owned-heading":
        heading_markup = f'<div class="result-card"><a href="{source}">{heading_markup}</a></div>'
    another_body = (
        f'<section><a href="{source}">{title}</a>'
        '<a href="/careers/roles/green-stone">Program Coordinator</a></section>'
        f"<div>{sections}{application}</div>"
        if missing == "single-body"
        else ""
    )
    page = (
        f"<html><head><title>{document_title} | Northwind</title></head><body>"
        f"<div>{heading_markup}{sections}{application}</div>{another_body}</body></html>"
    )

    result = inspect_publisher_page(
        source, page, referring_link=None if missing == "referral" else root.candidate_links[0]
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


@pytest.mark.parametrize("summary_position", ["first", "last"])
@pytest.mark.parametrize("row_tag", ["li", 'div class="result-card"'])
def test_an_unrecognized_heading_cannot_take_role_sections_from_linked_cards(
    summary_position: str,
    row_tag: str,
) -> None:
    title = "Customer Strategy & Operations"
    source = "https://publisher.example/careers/roles/blue-stone"
    closing_tag = row_tag.split(maxsplit=1)[0]
    summary = (
        "<strong>What you'll do</strong><p>Coordinate the team.</p>"
        "<strong>About You</strong><p>Bring clear communication.</p>"
        "<strong>How to Apply</strong><p>Send a resume.</p>"
    )
    rows = (
        f'<{row_tag}><a href="{source}">{title}</a>'
        f"{summary if summary_position == 'first' else ''}</{closing_tag}>"
        f'<{row_tag}><a href="/careers/roles/green-stone">Program Coordinator</a>'
        f"{summary if summary_position == 'last' else ''}</{closing_tag}>"
    )
    root = inspect_publisher_page(SOURCE, f"<section>{rows}</section>")
    page = (
        f"<html><head><title>{title} | Northwind</title></head><body>"
        f"<section>{rows}</section><div><h3>{title}</h3>"
        f"<p>{'What you will do depends on the team, and our services support partners. ' * 25}</p>"
        "</div></body></html>"
    )

    result = inspect_publisher_page(source, page, referring_link=root.candidate_links[0])

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""


@pytest.mark.parametrize("page_kind", ["general", "collection", "application"])
def test_a_referred_generic_secondary_heading_does_not_claim_a_role_body(page_kind: str) -> None:
    title = {
        "general": "Our teams",
        "collection": "Careers at Northwind",
        "application": "Submit your application",
    }[page_kind]
    source = "https://publisher.example/careers/roles/blue-stone"
    root = inspect_publisher_page(
        SOURCE,
        f'<section><a href="{source}">{title}</a>'
        '<a href="/careers/roles/green-stone">Program Coordinator</a></section>',
    )
    description = (
        "Our teams support partners across the region, and each group has its own "
        "responsibilities and qualifications. Contact us to learn about the company. "
    ) * 12
    content = f"<p>{description}</p><strong>Benefits</strong><p>Read our benefits guide.</p>"
    if page_kind == "application":
        content += "<strong>Contact details</strong><form><input type='file'></form>"
    elif page_kind == "collection":
        content += (
            '<section><a href="/careers/roles/blue-stone">Program Analyst</a>'
            '<a href="/careers/roles/green-stone">Program Coordinator</a></section>'
        )
    page = (
        f"<html><head><title>{title} | Northwind</title></head><body>"
        f"<main><h3>{title}</h3>{content}</main></body></html>"
    )

    result = inspect_publisher_page(source, page, referring_link=root.candidate_links[0])

    assert (
        result.classification
        is {
            "general": PageClassification.AMBIGUOUS,
            "collection": PageClassification.COLLECTION,
            "application": PageClassification.APPLICATION_CHANNEL,
        }[page_kind]
    )
    assert result.title == ""


@pytest.mark.parametrize("title", ["Careers at Northwind", "Current openings", "About our team"])
@pytest.mark.parametrize(
    "long_introduction", [False, True], ids=["brief-summaries", "long-boilerplate"]
)
def test_a_self_linked_collection_with_employment_sections_stays_a_collection(
    title: str, long_introduction: bool
) -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    introduction = (
        "Our employment requirements and responsibilities vary by opening. "
        "Northwind offers benefits and compensation to people across its teams. "
    ) * (12 if long_introduction else 1)
    page = (
        f"<html><head><title>{title} | Northwind</title></head><body>"
        f'<ul><li><a href="/careers/roles/copper-leaf">{title}</a></li>'
        '<li><a href="/careers/roles/silver-branch">Partnerships Manager</a>'
        "<p>Responsibilities include building partnerships; qualifications include "
        "clear communication.</p></li></ul>"
        f"<div><h1>{title}</h1><h2>Benefits</h2><p>{introduction}</p>"
        "<h2>Requirements</h2><p>Read each opening for its requirements.</p></div>"
        "</body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""


@pytest.mark.parametrize("summary_position", ["first", "last"])
@pytest.mark.parametrize("row_tag", ["li", 'div class="result-card"'])
def test_a_collection_body_cannot_borrow_posting_headings_from_its_linked_summaries(
    summary_position: str, row_tag: str
) -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    introduction = (
        "Our responsibilities and qualifications depend on each opening. "
        "Benefits and compensation support people across the organization. "
    ) * 12
    summary = (
        "<h2>responsibilities</h2><p>Support the project team.</p>"
        "<h2>qualifications</h2><p>Bring clear communication.</p>"
    )
    closing_tag = row_tag.split(maxsplit=1)[0]
    rows = (
        f'<{row_tag}><a href="/careers/roles/copper-leaf">Careers at Northwind</a>'
        f"{summary if summary_position == 'first' else ''}</{closing_tag}>"
        f'<{row_tag}><a href="/careers/roles/silver-branch">Partnerships Manager</a>'
        f"{summary if summary_position == 'last' else ''}</{closing_tag}>"
    )
    page = (
        "<html><head><title>Careers at Northwind | Northwind</title></head><body>"
        f"<ul>{rows}</ul><div><h1>Careers at Northwind</h1><p>{introduction}</p></div>"
        "</body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""


@pytest.mark.parametrize("frame", ["div", "main"])
def test_two_substantial_role_bodies_interleaved_with_links_stay_a_collection(frame: str) -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    page = (
        "<html><head><title>Senior Graphic Designer | Northwind</title></head><body>"
        f"<{frame}><h1>Senior Graphic Designer</h1>"
        '<ul><li><a href="/careers/roles/copper-leaf">Senior Graphic Designer</a></li></ul>'
        f"{_POSTING_SECTIONS}"
        '<ul><li><a href="/careers/roles/silver-branch">Partnerships Manager</a></li></ul>'
        f"<h2>Partnerships Manager</h2>{_POSTING_SECTIONS}</{frame}></body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.COLLECTION
    assert result.title == ""


def test_a_separate_role_body_beside_long_site_policies_is_still_a_posting() -> None:
    source = "https://publisher.example/careers/roles/copper-leaf"
    policy = "Our site protects visitor privacy and retains access records for support. " * 20
    page = (
        "<html><head><title>Senior Graphic Designer | Northwind</title></head><body>"
        f"<div><h1>Senior Graphic Designer</h1>{_POSTING_SECTIONS}</div>"
        '<ul><li><a href="/careers/roles/copper-leaf">Senior Graphic Designer</a></li>'
        '<li><a href="/careers/roles/silver-branch">Partnerships Manager</a></li></ul>'
        f"<div><h2>Privacy policy</h2><p>{policy}</p></div></body></html>"
    )

    result = inspect_publisher_page(source, page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Graphic Designer"


@pytest.mark.parametrize(
    "href",
    [
        pytest.param("/job/{number}/clinical-data-analyst", id="numeric-id"),
        pytest.param("/job/R-{number}/clinical-data-analyst", id="hyphenated-id"),
        pytest.param(
            "/job/9e8d7c6b-5a49-4382-a716-f5e4d3c2{number}/clinical-data-analyst/", id="uuid"
        ),
        pytest.param("/job/Remote-US-{number}/Clinical-Data-Analyst_JR{number}", id="place-id"),
    ],
)
def test_search_results_with_no_main_or_article_frame_are_not_a_posting(href: str) -> None:
    results = "".join(
        f'<div class="result"><a href="{href.format(number=number)}">'
        f"Clinical Data Analyst {number}</a><span>Remote, United States</span>"
        "<p>Responsibilities include building pipelines; requirements include SQL and "
        "three years of platform experience.</p></div>"
        for number in range(5000, 5020)
    )
    page = (
        "<html><head><title>Search results | Northwind Careers</title></head><body>"
        f"{_FRAMELESS_HEADER}<div><h1>Search results</h1>{results}</div>"
        f"{_FRAMELESS_FOOTER}</body></html>"
    )

    result = inspect_publisher_page("https://careers.example/jobs?q=data", page)

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


def test_a_navigation_hub_with_no_main_or_article_frame_stays_navigation() -> None:
    tiles = "".join(
        f'<div class="tile"><a href="/life/{path}">{label}</a><p>{blurb}</p></div>'
        for path, label, blurb in (
            ("benefits", "Benefits", "Our benefits cover health, retirement, and paid leave."),
            ("culture", "Our culture", "How our teams work together across every region."),
            ("students", "Students and graduates", "Internships and early career programs."),
            ("stories", "Employee stories", "People across Northwind describe their work."),
            ("teams", "Our teams", "From engineering to clinical operations and finance."),
        )
    )
    page = (
        "<html><head><title>Life at Northwind</title></head><body>"
        f"{_FRAMELESS_HEADER}<div><h1>Life at Northwind</h1><p>Find out what it is like to "
        f"work here and where you might fit.</p>{tiles}</div>{_FRAMELESS_FOOTER}</body></html>"
    )

    result = inspect_publisher_page("https://careers.example/life", page)

    assert result.classification is PageClassification.NAVIGATION
    assert result.title == ""


_HEADER_ROLES = (
    ("senior-software-engineer", "Senior Software Engineer"),
    ("staff-product-manager", "Staff Product Manager, Platform SDK"),
    ("account-manager", "Account Manager, Enterprise"),
    ("marketing-manager", "Marketing Manager/Director"),
)
_DIV_ROLES = (
    ("data-scientist", "Data Scientist Full-time , Remote"),
    ("chief-of-staff", "Chief of Staff Full-time , Remote"),
    ("backend-engineer", "Backend Engineer Full-time , Remote"),
)


def _role_links(prefix: str, roles: tuple[tuple[str, str], ...]) -> str:
    return "".join(
        f'<div class="item"><div><a href="/{prefix}/{slug}">{label}</a></div></div>'
        for slug, label in roles
    )


@pytest.mark.parametrize(
    ("wrapper", "prefix", "roles"),
    [
        (
            "<div id='main'><div class='hero'><h1>Join us</h1></div><div class='block'>"
            "<header class='roles'><div><div>{links}</div></div></header></div></div>",
            "careers",
            _HEADER_ROLES,
        ),
        (
            "<div class='page'><div class='section'><div class='container'><h2>Open roles</h2>"
            "<div class='list-wrap'><div class='rows'>{links}</div></div></div></div></div>",
            "jobs",
            _DIV_ROLES,
        ),
    ],
)
def test_occupation_links_repeated_in_a_header_or_plain_div_form_a_collection(
    wrapper: str, prefix: str, roles: tuple[tuple[str, str], ...]
) -> None:
    page = (
        "<html><head><title>Careers</title></head><body>"
        + wrapper.format(links=_role_links(prefix, roles))
        + "</body></html>"
    )

    result = inspect_publisher_page("https://studio.example/careers", page)

    assert result.classification is PageClassification.COLLECTION
    roles_found = [link for link in result.candidate_links if link.label in dict(roles).values()]
    assert [link.label for link in roles_found] == [label for _, label in roles]
    assert {link.cluster_size for link in roles_found} == {len(roles)}
    assert all(LinkRelation.PUBLISHER_CONTENT in link.relations for link in roles_found)


def test_a_header_menu_of_site_destinations_stays_navigation() -> None:
    menu = "".join(
        f'<div><a href="/{label.casefold()}">{label}</a></div>'
        for label in ("About", "Careers", "Blog", "Product", "Pricing")
    )
    page = f"<html><body><header><div>{menu}</div></header></body></html>"

    result = inspect_publisher_page("https://studio.example/", page)

    assert result.classification is PageClassification.NAVIGATION
    assert result.candidate_links == ()


@pytest.mark.parametrize("chrome", ["nav", "footer"])
def test_role_links_in_a_nav_or_footer_stay_chrome(chrome: str) -> None:
    page = (
        f"<html><body><div><{chrome}><div>{_role_links('careers', _HEADER_ROLES)}</div>"
        f"</{chrome}></div></body></html>"
    )

    result = inspect_publisher_page("https://studio.example/careers", page)

    assert result.classification is not PageClassification.COLLECTION
    assert result.candidate_links == ()


_OTHER_OPENINGS = "<h2>Other openings</h2>" + _role_links(
    "careers", (("data-analyst", "Data Analyst"), ("platform-engineer", "Platform Engineer"))
)


@pytest.mark.parametrize(
    "rail",
    [
        "<main>{body}<header>{rail}</header></main>",
        "<main>{body}</main><div class='more'>{rail}</div>",
        "<div id='main'><div class='page'><div>{body}</div><header>{rail}</header></div></div>",
    ],
    ids=["header-in-main", "div-after-main", "framer-header"],
)
def test_a_posting_beside_a_similar_jobs_list_in_a_header_or_plain_div_is_a_posting(
    rail: str,
) -> None:
    body = f"<h1>Senior Data Engineer</h1>{_POSTING_SECTIONS}<a href='/apply'>Apply now</a>"
    page = (
        "<html><head><title>Senior Data Engineer | Studio</title></head><body>"
        + rail.format(body=body, rail=_OTHER_OPENINGS)
        + "</body></html>"
    )

    result = inspect_publisher_page("https://studio.example/careers/senior-data-engineer", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Data Engineer"


@pytest.mark.parametrize(
    "labels",
    [
        ("About", "Pricing", "Head Office", "Lead Generation"),
        ("About", "Pricing", "Software Engineering", "Hardware Engineering"),
    ],
    ids=["rung-words", "team-names"],
)
def test_a_header_menu_where_half_the_links_read_as_roles_stays_navigation(
    labels: tuple[str, ...],
) -> None:
    menu = "".join(
        f'<div><a href="/{label.casefold().replace(" ", "-")}">{label}</a></div>'
        for label in labels
    )
    page = f"<html><body><header><div>{menu}</div></header></body></html>"

    result = inspect_publisher_page("https://studio.example/", page)

    assert result.classification is PageClassification.NAVIGATION
    assert result.candidate_links == ()


def test_one_role_link_labelled_learn_more_in_a_header_is_not_a_collection() -> None:
    page = (
        "<html><body><header><div><h2>Senior Software Engineer</h2>"
        '<a href="/careers/senior-software-engineer">Learn more</a></div></header></body></html>'
    )

    result = inspect_publisher_page("https://studio.example/careers", page)

    assert result.classification is not PageClassification.COLLECTION
    assert result.candidate_links == ()


_EIGHT_ROLES = (
    ("data-scientist", "Data Scientist"),
    ("backend-engineer", "Backend Engineer"),
    ("account-manager", "Account Manager"),
    ("product-designer", "Product Designer"),
    ("registered-nurse", "Registered Nurse"),
    ("marketing-manager", "Marketing Manager"),
    ("platform-engineer", "Platform Engineer"),
    ("financial-analyst", "Financial Analyst"),
)


@pytest.mark.parametrize(
    "unlabelled",
    [
        '<a href="/jobs/search/"></a>',
        '<a href="/jobs/search/">  \n\t </a>',
        '<a href="/jobs/search/"><img src="/icons/search.svg"></a>',
        '<a href="/jobs/search/"><span><img src="/icons/search.svg" alt=""></span></a>',
    ],
    ids=["empty", "whitespace", "img-no-alt", "img-empty-alt"],
)
@pytest.mark.parametrize("role_count", [2, 8])
def test_an_unlabelled_link_of_a_promoted_shape_is_not_a_candidate(
    unlabelled: str, role_count: int
) -> None:
    rows = _role_links("jobs", _EIGHT_ROLES[:role_count])
    page = (
        "<html><head><title>Careers</title></head><body><div class='page'><h2>Open roles</h2>"
        f"<div class='rows'>{rows}<div class='item'>{unlabelled}</div></div></div></body></html>"
    )

    result = inspect_publisher_page("https://northwind.example/careers", page)

    assert result.classification is PageClassification.COLLECTION
    assert [link.label for link in result.candidate_links] == [
        label for _, label in _EIGHT_ROLES[:role_count]
    ]


def test_six_labelled_role_links_of_one_loose_shape_are_all_candidates() -> None:
    rows = _role_links("jobs", _EIGHT_ROLES[:6])
    page = (
        "<html><head><title>Careers</title></head><body><div class='page'><h2>Open roles</h2>"
        f"<div class='rows'>{rows}</div></div></body></html>"
    )

    result = inspect_publisher_page("https://northwind.example/careers", page)

    assert result.classification is PageClassification.COLLECTION
    assert len(result.candidate_links) == 6


def test_a_page_declaring_posting_data_takes_no_title_from_a_block_its_page_title_leads_with() -> (
    None
):
    declared = (
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"JobPosting","title":"Senior Widget Analyst / Reporting Projects"}</script>'
    )
    page = _plain_posting_page().replace("</head>", f"{declared}</head>")

    result = inspect_publisher_page("https://acme.app.loxo.co/job/MTAwMTQtd2lkZ2V0cw==", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == ""


def test_jsonld_is_direct_publisher_evidence_for_a_posting() -> None:
    page = """
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"JobPosting","title":"Data Lead"}
    </script>
    """

    result = inspect_publisher_page(SOURCE, page)

    assert result.classification is PageClassification.POSTING
    assert "publisher declared JobPosting structured data" in result.evidence


@pytest.mark.parametrize("host", ["recruiting.paylocity.com", "publisher.example"])
def test_a_bounded_publisher_preview_without_structured_data_is_a_posting(host: str) -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")

    result = inspect_publisher_page(f"https://{host}/Recruiting/Jobs/Details/123456", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Operations Lead"
    assert "page body contains substantive text" in result.evidence


def test_a_publisher_preview_with_combined_description_keeps_its_role() -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")
    page = page.replace(
        '<div class="job-listing-header">Requirements</div>',
        "<p>Required background</p>",
    )

    result = inspect_publisher_page(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page
    )

    assert result.classification is PageClassification.POSTING
    assert result.title == "Operations Lead"


def test_a_publisher_preview_with_description_alone_stays_unverified() -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")
    page = page.replace(
        '<div class="job-listing-header">Requirements</div>', "<p>Required background</p>"
    ).replace('<div class="job-listing-header">Salary Description</div>', "<p>Compensation</p>")

    result = inspect_publisher_page(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param("job-preview-title left", "unmarked-title", id="missing-title"),
        pytest.param("<span>Operations Lead</span>", "<span></span>", id="empty-title"),
        pytest.param(
            "<span>Operations Lead</span>",
            '<span>Operations Lead</span><span class="job-preview-title">Finance Manager</span>',
            id="multiple-titles",
        ),
        pytest.param("job-preview-details", "job-overview", id="missing-body"),
        pytest.param(
            '<div class="job-preview-details">',
            '<div class="job-preview-details"><div class="job-preview-details"></div>',
            id="multiple-bodies",
        ),
        pytest.param("job-listing-header", "unmarked-section", id="missing-sections"),
        pytest.param("job-preview-header", "other-header", id="title-outside-preview-header"),
        pytest.param('role="main"', 'role="navigation"', id="preview-in-navigation"),
        pytest.param(
            "<span>Operations Lead</span>", "<span>Our community</span>", id="non-role-title"
        ),
        pytest.param(
            '<div class="job-preview-details">',
            '<form><input type="password"><div class="job-preview-details">',
            id="login-form",
        ),
    ],
)
def test_an_incomplete_or_ambiguous_publisher_preview_is_not_a_posting(old: str, new: str) -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")

    result = inspect_publisher_page(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page.replace(old, new)
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


@pytest.mark.parametrize("suffix", ["All", "Apply/123456"])
def test_a_publisher_preview_on_a_board_or_application_address_does_not_claim_one_posting(
    suffix: str,
) -> None:
    page = (FIXTURES / "board_responses" / "paylocity_detail.html").read_text(encoding="utf-8")

    result = inspect_publisher_page(
        f"https://recruiting.paylocity.com/Recruiting/Jobs/{suffix}", page
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


def test_a_link_only_publisher_preview_does_not_claim_a_posting() -> None:
    page = """
    <div class="job-preview" role="main">
      <div class="job-preview-header"><span class="job-preview-title">Operations Lead</span></div>
      <div class="job-preview-details">
        <div class="job-listing-header">Description</div>
        <div class="job-listing-header">Requirements</div>
        <a href="/Recruiting/Jobs/Apply/123456">Apply</a>
        <a href="/Recruiting/Jobs/All">View All Jobs</a>
      </div>
    </div>
    """

    result = inspect_publisher_page(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/123456", page
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


_HEADER_POSTING_TEXT = (
    "<p>Northwind builds tools that help clinics share records safely across every region.</p>"
    "<p>Role overview</p><p>You will design and ship services that move patient data between "
    "partner systems, review code with a small team, and own reliability for the platform.</p>"
    "<p>Responsibilities</p><ul><li>Build and operate backend services in Python.</li>"
    "<li>Design data pipelines that respect consent and audit rules.</li>"
    "<li>Mentor engineers and lead technical reviews.</li></ul>"
    "<p>Requirements</p><ul><li>Six or more years building production software.</li>"
    "<li>Experience with relational databases and message queues.</li>"
    "<li>Clear written communication with clinical and product partners.</li></ul>"
)


@pytest.mark.parametrize("notice", ["", "<p>Something went wrong. Please fix the error.</p>"])
def test_a_header_holding_the_only_heading_and_most_text_frames_the_posting(notice: str) -> None:
    menu = "".join(
        f'<div><a href="/{label.casefold()}">{label}</a></div>'
        for label in ("Product", "Support", "Privacy", "Blog")
    )
    page = (
        "<html><head><title>Senior Software Engineer | Careers at Northwind</title></head><body>"
        "<div id='main'><div class='framer-a'><header class='framer-b'>"
        "<h1>Senior Software Engineer</h1>"
        f"{_HEADER_POSTING_TEXT}"
        "<a href='./senior-software-engineer#application-form'>Apply today</a></header>"
        f"<div><p>Application form</p>{notice}</div><div class='framer-c'>{menu}</div>"
        "</div></div></body></html>"
    )

    result = inspect_publisher_page("https://studio.example/careers/senior-software-engineer", page)

    assert result.classification is PageClassification.POSTING
    assert result.title == "Senior Software Engineer"
    assert "Mentor engineers" in result.text_outside_chrome


def test_a_site_header_with_a_logo_heading_beside_a_long_main_stays_chrome() -> None:
    page = (
        "<html><head><title>Senior Software Engineer | Northwind</title></head><body>"
        "<header><h1>Northwind</h1><p>Records that travel with you</p>"
        "<nav><a href='/about'>About</a><a href='/careers'>Careers</a></nav></header>"
        f"<main><h2>Senior Software Engineer</h2>{_HEADER_POSTING_TEXT}</main></body></html>"
    )

    result = inspect_publisher_page("https://studio.example/careers/senior-software-engineer", page)

    assert "Records that travel with you" not in result.text_outside_chrome
    assert "Mentor engineers" in result.text_outside_chrome


def _headingless_posting_page(title_tag: str = "div") -> str:
    menus = "".join(
        "<nav>"
        + "".join(
            f'<div><a href="/{label.casefold()}">{label}</a></div>'
            for label in ("Product", "Customers", "Company", "Careers", "Contact")
        )
        + "</nav>"
        for _ in range(4)
    )
    return (
        "<html><head><title>Careers | Northwind</title></head><body>"
        f"{menus[: len(menus) // 2]}"
        f"<div><{title_tag}>Data Scientist</{title_tag}><div>Full-time<span>, </span>Remote</div>"
        "<a href='/jobs/data-scientist/apply'>Apply now</a>"
        "<div>Role Overview</div><div>Join a small team turning clinical records into "
        "models that clinicians trust.</div>"
        "<div>What You'll Do</div><ul><li>Build and validate predictive models.</li>"
        "<li>Partner with engineers to ship features to production.</li>"
        "<li>Explain results to clinical and product partners.</li></ul>"
        "<div>What We're Looking For</div><ul><li>Three or more years of applied modeling.</li>"
        "<li>Strong Python and SQL.</li><li>Experience with healthcare data.</li></ul></div>"
        f"{menus[len(menus) // 2 :]}</body></html>"
    )


@pytest.mark.parametrize("title_tag", ["div", "h5"])
@pytest.mark.parametrize("label", ["Data Scientist", "Data Scientist Full-time , Remote"])
def test_a_headingless_page_led_by_its_referring_label_is_a_posting(
    label: str, title_tag: str
) -> None:
    source = "https://studio.example/jobs/data-scientist"
    referring_link = PublisherLink(
        source,
        label,
        "visible",
        "https://studio.example/jobs",
        "list",
        "https://studio.example/jobs/{item}?",
        3,
        (LinkRelation.SAME_ORIGIN, LinkRelation.PUBLISHER_CONTENT),
    )

    result = inspect_publisher_page(
        source, _headingless_posting_page(title_tag), referring_link=referring_link
    )

    assert result.classification is PageClassification.POSTING
    assert result.title == "Data Scientist"


@pytest.mark.parametrize("label", [None, "Chief of Staff", "Data"])
def test_a_headingless_page_its_referring_label_does_not_lead_stays_navigation(
    label: str | None,
) -> None:
    source = "https://studio.example/jobs/data-scientist"
    referring_link = (
        None
        if label is None
        else PublisherLink(
            source,
            label,
            "visible",
            "https://studio.example/jobs",
            "list",
            "https://studio.example/jobs/{item}?",
            3,
            (LinkRelation.SAME_ORIGIN, LinkRelation.PUBLISHER_CONTENT),
        )
    )

    result = inspect_publisher_page(
        source, _headingless_posting_page(), referring_link=referring_link
    )

    assert result.classification is PageClassification.NAVIGATION
    assert result.title == ""


_PROBE_MENU = "".join(
    "<nav>"
    + "".join(
        f'<div><a href="/{label.casefold()}">{label}</a></div>'
        for label in ("Product", "Customers", "Company", "Careers", "Contact")
    )
    + "</nav>"
    for _ in range(2)
)
_PROBE_INTRO = (
    "Our teams build tools that help clinics share records safely. We work in small groups, "
    "ship often, and care about the people who rely on our software every day. "
)
_PROBE_ROLES = "".join(
    f'<div class="row"><div><a href="/jobs/{slug}">{label}</a></div>'
    f'<div><a href="/jobs/{slug}/apply">Apply</a></div></div>'
    for slug, label in (
        ("data-scientist", "Data Scientist"),
        ("backend-engineer", "Backend Engineer"),
        ("product-designer", "Product Designer"),
    )
)

_PROBE_HIRING = (
    "Every role page lists its responsibilities and requirements, and we read each "
    "application against them."
)
_PROBE_APPLY_ROWS = "".join(
    f'<div class="row"><div>{label}</div><div>Remote</div>'
    f'<div><a href="/jobs/{slug}/apply">Apply</a></div></div>'
    for slug, label in (
        ("data-scientist", "Data Scientist"),
        ("backend-engineer", "Backend Engineer"),
        ("product-designer", "Product Designer"),
    )
)


def _probe_listing(lead: str) -> str:
    return (
        f"<html><head><title>Careers | Northwind</title></head><body>{_PROBE_MENU}"
        f"<div><div>{lead}</div><div>{_PROBE_INTRO * 2}</div>{_PROBE_ROLES}</div>"
        f"{_PROBE_MENU}</body></html>"
    )


def _probe_section(lead: str) -> str:
    return (
        f"<html><head><title>Careers | Northwind</title></head><body>{_PROBE_MENU}"
        f"<div><div>{lead}</div><div>{_PROBE_INTRO * 3}</div>"
        "<div>Health, dental and vision cover, paid leave, and a learning budget.</div></div>"
        f"{_PROBE_MENU}</body></html>"
    )


def _probe_hero(heading: str, body: str, after: str = "") -> str:
    return (
        f"<html><head><title>{heading} | Northwind</title></head><body><div id='main'>"
        f"<header><h1>{heading}</h1><p>{body}</p></header>{after}"
        f"<div class='links'>{_PROBE_MENU}</div></div></body></html>"
    )


# Non-posting pages keyed by the referring label that repeats their leading text.
NON_POSTING_PROBES = {
    "open-roles": ("Open roles", _probe_listing("Open roles")),
    "team": ("Engineering", _probe_listing("Engineering")),
    "location": ("Toronto", _probe_listing("Toronto")),
    "benefits": ("Benefits", _probe_section("Benefits")),
    "culture": ("Life at Northwind", _probe_section("Life at Northwind")),
    "hero-index": (
        "Join Northwind",
        _probe_hero("Join Northwind", _PROBE_INTRO * 5 + _PROBE_HIRING, _PROBE_APPLY_ROWS),
    ),
    "hero-team": ("Engineering", _probe_hero("Engineering", _PROBE_INTRO * 6, _PROBE_ROLES)),
    "blog-header": (
        "How we hire",
        _probe_hero(
            "How we hire",
            "We start with a short call, then a practical exercise, then a conversation with "
            "the team. When you apply, send a resume and a short note about your work. "
            * 6
            + _PROBE_HIRING,
        ),
    ),
    "wordy-404": (
        "Page not found",
        _probe_hero(
            "Page not found",
            "The page you were looking for has moved or no longer exists. Try the links below "
            "or head back to the home page to keep exploring. " * 4,
        ),
    ),
}


@pytest.mark.parametrize("referred", [False, True])
@pytest.mark.parametrize("probe", sorted(NON_POSTING_PROBES))
def test_listings_sections_articles_and_errors_led_by_their_label_are_not_postings(
    probe: str, referred: bool
) -> None:
    label, page = NON_POSTING_PROBES[probe]
    source = "https://studio.example/careers/" + probe
    referring_link = PublisherLink(
        source,
        label,
        "visible",
        "https://studio.example/careers",
        "list",
        "https://studio.example/careers/{item}?",
        3,
        (LinkRelation.SAME_ORIGIN, LinkRelation.PUBLISHER_CONTENT),
    )

    result = inspect_publisher_page(
        source, page, referring_link=referring_link if referred else None
    )

    assert result.classification is not PageClassification.POSTING
    assert result.title == ""


def test_a_wordy_not_found_header_is_still_an_error_page() -> None:
    _label, page = NON_POSTING_PROBES["wordy-404"]

    result = inspect_publisher_page("https://studio.example/careers/gone", page)

    assert result.classification is PageClassification.ERROR_OR_CHALLENGE


_PERKS = (
    "<div>Full-time employees receive health, dental and vision cover, paid leave, "
    "and a learning budget.</div>"
)
_PROBE_MENU_APPLY = _PROBE_MENU + '<nav><div><a href="/careers/apply">Apply</a></div></nav>'


def _probe_headingless(lead: str, middle: str, menu: str = _PROBE_MENU) -> str:
    return (
        f"<html><head><title>Careers | Northwind</title></head><body>{menu}"
        f"<div><div>{lead}</div><div>{_PROBE_INTRO * 3}</div>{middle}</div>"
        f"{_PROBE_MENU}</body></html>"
    )


def _probe_titled_hero(heading: str, body: str, title: str = "Northwind") -> str:
    return (
        f"<html><head><title>{title}</title></head><body><div id='main'>"
        f"<header><h1>{heading}</h1><p>{body}</p></header>"
        f"<div class='links'>{_PROBE_MENU}</div></div></body></html>"
    )


# Pages whose lead or header looks like a role; each keeps the reading it had before
# header framing and referred leading titles existed.
ROLE_LOOKING_PROBES = {
    "team-full-time-perks": (
        "Engineering",
        _probe_headingless("Engineering", _PERKS),
        "navigation",
    ),
    "team-apply-link": (
        "Engineering",
        _probe_headingless("Engineering", _PERKS + '<a href="/jobs?team=eng">Apply</a>'),
        "navigation",
    ),
    "team-menu-apply": (
        "Engineering",
        _probe_headingless("Engineering", "", _PROBE_MENU_APPLY),
        "navigation",
    ),
    "team-contract-prose": (
        "Engineering",
        _probe_headingless(
            "Engineering",
            "<div>We never ask engineers to sign a contract they cannot read.</div>",
        ),
        "navigation",
    ),
    "hero-team-open-roles-link": (
        "Engineering",
        _probe_hero(
            "Engineering", _PROBE_INTRO * 6, '<div><a href="/jobs">See open roles</a></div>'
        ),
        "navigation",
    ),
    "hero-team-apply-link": (
        "Engineering",
        _probe_hero("Engineering", _PROBE_INTRO * 6, '<div><a href="/jobs/apply">Apply</a></div>'),
        "navigation",
    ),
    "hero-day-in-the-life": (
        "A day in the life of a Data Scientist",
        _probe_hero("A day in the life of a Data Scientist", _PROBE_INTRO * 6),
        "navigation",
    ),
    "hero-join-as": (
        "Join us as a Software Engineer",
        _probe_hero(
            "Join us as a Software Engineer",
            _PROBE_INTRO * 6,
            '<div><a href="/jobs">Apply</a></div>',
        ),
        "navigation",
    ),
    "hero-expired-role": (
        "Senior Software Engineer",
        _probe_hero(
            "Senior Software Engineer",
            "Error: this position is no longer accepting applications. " + _PROBE_INTRO * 6,
        ),
        "error_or_challenge",
    ),
    "hero-role-not-found": (
        "Senior Software Engineer",
        _probe_titled_hero(
            "Senior Software Engineer", "This page was not found. " + _PROBE_INTRO * 4
        ),
        "error_or_challenge",
    ),
    "hero-role-could-not-be-found": (
        "Senior Software Engineer",
        _probe_titled_hero(
            "Senior Software Engineer",
            "Sorry, this page could not be found. It has moved or no longer exists. " * 6,
        ),
        "navigation",
    ),
}


@pytest.mark.parametrize("referred", [False, True])
@pytest.mark.parametrize("path", ["careers/{probe}", "jobs/{probe}-48213"])
@pytest.mark.parametrize("probe", sorted(ROLE_LOOKING_PROBES))
def test_a_title_chosen_for_looking_like_a_role_does_not_also_corroborate_a_posting(
    probe: str, path: str, referred: bool
) -> None:
    label, page, expected = ROLE_LOOKING_PROBES[probe]
    source = "https://studio.example/" + path.format(probe=probe)
    referring_link = PublisherLink(
        source,
        label,
        "visible",
        "https://studio.example/careers",
        "list",
        "https://studio.example/careers/{item}?",
        3,
        (LinkRelation.SAME_ORIGIN, LinkRelation.PUBLISHER_CONTENT),
    )

    result = inspect_publisher_page(
        source, page, referring_link=referring_link if referred else None
    )

    assert result.classification.value == expected
    assert result.title == ""
