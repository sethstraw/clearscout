"""What a posting URL folds to, and therefore which two postings are one job.

Fold too hard and two requisitions merge into one record; fold too little and
one posting is filed twice, once from the board and once from a pasted link.
The same trade-off decides whether a phrase counts as named in a longer string.
"""

from __future__ import annotations

import pytest

from clearscout.domain.identity import (
    employer_name_words,
    exact_url_key,
    is_absolute_http_url,
    mentions_in_order,
    normalize_fetch_url,
    normalize_url,
    requisition_key,
    url_host,
    url_path_segments,
    url_query_keys,
    url_sha256,
)


@pytest.mark.parametrize(
    ("haystack", "needle"),
    [
        # The phrase itself, and the phrase inside a longer string.
        ("director clinical informatics", "director clinical informatics"),
        ("director of clinical informatics jobs", "director clinical informatics"),
        # Several connectors in one gap, and a connector in each of two gaps.
        ("head of the records office", "head records"),
        ("head of records and the compliance desk", "head of records and compliance"),
        # A connector the phrase itself carries is as optional as one it lacks.
        ("head data platform jobs", "head of data"),
        ("head of data platform jobs", "head of data"),
        ("data governance stewardship roles", "data governance and stewardship"),
    ],
)
def test_a_phrase_is_named_when_its_words_run_in_order_through_connectors(
    haystack: str, needle: str
) -> None:
    assert mentions_in_order(haystack, needle)


@pytest.mark.parametrize(
    ("haystack", "needle"),
    [
        # A reordering is a different phrase.
        ("clinical informatics director openings", "director clinical informatics"),
        # A plural is a different word, not a boundary case.
        ("directors clinical informatics", "director clinical informatics"),
        # Content words between the phrase's words make it a different phrase,
        # so a broad query cannot claim credit for a narrow one.
        ("data science engineering manager", "data manager"),
        ("regulated clinical records roles", "regulated records"),
        # "and" joins two things, so it names the phrase only where the phrase has one.
        ("director of product and engineering", "product engineering"),
        # Nothing to look for, and nothing to look in.
        ("director clinical informatics", ""),
        ("", "director"),
    ],
)
def test_a_reordered_or_interrupted_phrase_is_not_named(haystack: str, needle: str) -> None:
    assert not mentions_in_order(haystack, needle)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Nothing to identify. An empty answer, not "https:///".
        ("", ""),
        ("   ", ""),
        # A pasted link often arrives without a scheme.
        (
            "boards.greenhouse.io/globex/jobs/6001234004",
            "https://boards.greenhouse.io/globex/jobs/6001234004",
        ),
        # http and https are the same posting.
        ("http://careers.initech.test/jobs/17", "https://careers.initech.test/jobs/17"),
        # An explicit non-http(s) scheme is preserved, not silently rewritten to https.
        ("ftp://x.example/a", "ftp://x.example/a"),
        # "www." is a spelling of the host, not a different host.
        ("https://www.initech.test/jobs/17", "https://initech.test/jobs/17"),
        # LinkedIn and Indeed serve the same posting from per-country hosts.
        (
            "https://uk.linkedin.com/jobs/view/3912345678",
            "https://linkedin.com/jobs/view/3912345678",
        ),
        (
            "https://www.linkedin.com/jobs/view/3912345678",
            "https://linkedin.com/jobs/view/3912345678",
        ),
        ("https://ca.indeed.com/viewjob?jk=9f1c2d", "https://indeed.com/viewjob?jk=9f1c2d"),
        ("https://www.indeed.com/viewjob?jk=9f1c2d", "https://indeed.com/viewjob?jk=9f1c2d"),
        ("https://app.dover.com/apply/Globex/9e8d", "https://app.dover.io/apply/Globex/9e8d"),
        # A host that merely contains a board's name is not that board.
        ("https://linkedin.com.phish.test/jobs/1", "https://linkedin.com.phish.test/jobs/1"),
        ("https://app.dover.com.phish.test/apply/1", "https://app.dover.com.phish.test/apply/1"),
        # Path noise: repeated separators, a trailing slash, percent-encoding.
        ("https://initech.test//jobs//17/", "https://initech.test/jobs/17"),
        ("https://initech.test/jobs/Staff%20Engineer", "https://initech.test/jobs/Staff Engineer"),
        # Only the query keys that identify a posting survive.
        (
            "https://initech.test/careers?utm_source=newsletter&gh_jid=42&ref=x",
            "https://initech.test/careers?gh_jid=42",
        ),
        ("https://initech.test/careers?utm_source=newsletter", "https://initech.test/careers"),
    ],
)
def test_a_posting_url_folds_to_what_identifies_the_posting(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_a_fetch_key_keeps_the_page_a_posting_identity_drops() -> None:
    """Two pages of one board are two fetch targets and no posting at all."""
    paged = "https://Careers.Initech.test/jobs?page=2&utm_source=newsletter"

    assert normalize_fetch_url(paged) == "https://careers.initech.test/jobs?page=2"
    assert normalize_url(paged) == "https://careers.initech.test/jobs"


def test_url_digest_is_stable_across_equivalent_url_spellings() -> None:
    """Logs can correlate one URL without exposing it."""
    left = "https://www.example.test/jobs/one/?utm_source=test&id=42"
    right = "example.test/jobs/one?id=42&utm_campaign=ignored"

    assert url_sha256(left) == url_sha256(right)
    assert len(url_sha256(left)) == 64


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The same Greenhouse posting: pasted from the address bar, and as the
        # board API states it.
        (
            "http://WWW.Boards.Greenhouse.io//globex//jobs/6001234004/?utm_source=x",
            "boards.greenhouse.io/globex/jobs/6001234004",
        ),
        # The same Indeed posting seen from two countries, with tracking
        # parameters attached by one of them.
        (
            "https://www.indeed.com/viewjob?jk=9f1c2d&tk=1h2i3j&from=serp",
            "https://ca.indeed.com/viewjob?jk=9f1c2d",
        ),
        # Query order is not identity.
        (
            "https://initech.test/careers?gh_jid=42&id=7",
            "https://initech.test/careers?id=7&gh_jid=42",
        ),
        # A UKG opportunity with and without the board's extra postingId.
        (
            "https://recruiting2.ultipro.com/GLO1001/JobBoard/0a1b/OpportunityDetail"
            "?opportunityId=7c1d",
            "https://recruiting2.ultipro.com/GLO1001/JobBoard/0a1b/OpportunityDetail"
            "?opportunityId=7c1d&postingId=88",
        ),
    ],
)
def test_two_spellings_of_one_posting_normalise_to_one_value(left: str, right: str) -> None:
    assert normalize_url(left) == normalize_url(right)


def test_adp_posting_identity_keeps_the_client_tenant() -> None:
    root = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
    first = root + "?cid=11111111-1111-1111-1111-111111111111&jobId=1001_1"
    other = root + "?cid=22222222-2222-2222-2222-222222222222&jobId=1001_1"
    assert normalize_url(first) != normalize_url(other)
    assert normalize_url(first + "&utm_source=board") == normalize_url(first)


_GATEWAY = "https://careers.acme.example/psc/hrprd/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"


def _gateway_posting(job: str) -> str:
    query = f"Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=2&JobOpeningId={job}"
    return f"{_GATEWAY}?{query}&PostingSeq=1"


def test_a_peoplesoft_gateway_keeps_the_query_that_names_its_page_and_posting() -> None:
    search = f"{_GATEWAY}?Page=HRS_APP_SCHJOB_FL&Action=U&FOCUS=Applicant&SiteId=2"

    assert url_sha256(_gateway_posting("113098")) != url_sha256(_gateway_posting("112959"))
    assert url_sha256(search) != url_sha256(_gateway_posting("113098"))
    assert normalize_url(_gateway_posting("113098") + "&PortalContentProvider=HRMS&") == (
        f"{_GATEWAY}?JobOpeningId=113098&Page=HRS_APP_JBPST_FL&PostingSeq=1&SiteId=2"
    )


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        (
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=11111111-1111-1111-1111-111111111111&ccId=19000101_000001&jobId=1001_1",
            "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
            "?cid=11111111-1111-1111-1111-111111111111&jobId=1001_1",
        ),
        (
            "https://careers.acme.example/careers/search?Page=HRS_APP_JBPST&SiteId=2&jobId=5",
            "https://careers.acme.example/careers/search?jobId=5",
        ),
        (f"{_GATEWAY}/extra?Page=HRS_APP_JBPST_FL&SiteId=2", f"{_GATEWAY}/extra"),
    ],
)
def test_a_url_outside_a_peoplesoft_gateway_drops_the_gateway_query(
    url: str, normalized: str
) -> None:
    assert normalize_url(url) == normalized


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Two requisitions on one board.
        (
            "https://boards.greenhouse.io/globex/jobs/6001234004",
            "https://boards.greenhouse.io/globex/jobs/6005324005",
        ),
        # Two employers on one board.
        (
            "https://boards.greenhouse.io/globex/jobs/6001234004",
            "https://boards.greenhouse.io/initech/jobs/6001234004",
        ),
        # One career page, two postings distinguished only by an identity key.
        (
            "https://initech.test/careers?gh_jid=42",
            "https://initech.test/careers?gh_jid=43",
        ),
        # Two UKG opportunities on one board share every path segment.
        (
            "https://recruiting2.ultipro.com/GLO1001/JobBoard/0a1b/OpportunityDetail"
            "?opportunityId=7c1d",
            "https://recruiting2.ultipro.com/GLO1001/JobBoard/0a1b/OpportunityDetail"
            "?opportunityId=8d2e",
        ),
        # Stripping "www." must not merge a subdomain into its parent.
        ("https://jobs.initech.test/17", "https://initech.test/17"),
    ],
)
def test_two_postings_do_not_normalise_to_one_value(left: str, right: str) -> None:
    assert normalize_url(left) != normalize_url(right)


def test_a_board_uuid_keys_the_same_under_any_employer_spelling() -> None:
    uuid = "7d3e9a1c-4b2f-4e8a-9c6d-2f1e0a9b8c7d"

    assert requisition_key("Acme Health", uuid) == requisition_key("ACME", uuid.upper())


def test_an_employer_requisition_keys_under_its_employer() -> None:
    assert requisition_key("Acme Health", "REQ-100") != requisition_key("Acme", "REQ-100")


# Spelled, abbreviated, dotted, and compound legal forms, plus Group and Holdings.
_LEGAL_FORMS = (
    "Inc",
    "Incorporated",
    "LLC",
    "L.L.C.",
    "Ltd",
    "Limited",
    "LLP",
    "L.L.P.",
    "PBC",
    "Corp",
    "Corporation",
    "Co",
    "Company",
    "PLC",
    "GmbH",
    "AG",
    "Oy",
    "Oyj",
    "AB",
    "ASA",
    "SA",
    "S.A.",
    "BV",
    "B.V.",
    "N.V.",
    "S.p.A.",
    "S.r.l.",
    "S.A.S.",
    "Pty Ltd",
    "Sdn Bhd",
    "GmbH & Co. KG",
    "Group",
    "Holdings",
    "Holdings Inc",
    "Group Ltd",
)


def _legal_form_spellings(form: str) -> dict[str, str]:
    return {
        "space": f"Northwind Health {form}",
        "comma": f"Northwind Health, {form}",
        "trailing-period": f"Northwind Health {form.rstrip('.')}.",
        "upper": f"Northwind Health {form}".upper(),
        "lower": f"Northwind Health {form}".lower(),
    }


@pytest.mark.parametrize(
    ("form", "spelling", "name"),
    [
        (form, spelling, name)
        for form in _LEGAL_FORMS
        for spelling, name in _legal_form_spellings(form).items()
    ],
)
def test_a_trailing_legal_form_names_no_other_employer(form: str, spelling: str, name: str) -> None:
    assert employer_name_words(name) == ["northwind", "health"]


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("Globex UK", ["globex", "uk"]),
        ("Globex UK Ltd", ["globex", "uk"]),
        ("Northwind Private", ["northwind", "private"]),
        ("Acme Unlimited", ["acme", "unlimited"]),
        ("Northwind Spa", ["northwind", "spa"]),
        ("Globex Labs", ["globex", "labs"]),
        ("Northwind Incubator", ["northwind", "incubator"]),
        ("Globex Cosmetics", ["globex", "cosmetics"]),
        ("Northwind Company Store", ["northwind", "company", "store"]),
        ("Acme Group Health", ["acme", "group", "health"]),
        *(
            (f"Acme {word}", ["acme", word.lower()])
            for word in ("SF", "NL", "SD", "KS", "KY", "MB", "EE", "EV", "Ent", "CIO")
        ),
    ],
)
def test_a_name_word_that_is_no_trailing_legal_form_stays(name: str, words: list[str]) -> None:
    assert employer_name_words(name) == words


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("SAS", ["sas"]),
        ("SES", ["ses"]),
        ("UAB", ["uab"]),
        ("BT Group", ["bt"]),
        ("KT Corp", ["kt"]),
    ],
)
def test_a_name_that_is_itself_a_legal_form_keeps_its_first_word(
    name: str, words: list[str]
) -> None:
    assert employer_name_words(name) == words


@pytest.mark.parametrize(
    "name",
    sorted(
        {
            spelled
            for first in _LEGAL_FORMS
            for second in ("", " Inc", " Group", ", Ltd.")
            for spelled in (f"{first}{second}", f"{first}{second}".upper())
        }
    ),
)
def test_a_name_made_only_of_legal_forms_never_folds_to_nothing(name: str) -> None:
    assert employer_name_words(name)


@pytest.mark.parametrize(
    ("value", "absolute"),
    [
        ("https://jobs.example.com/role/7", True),
        ("HTTP://Jobs.Example.com", True),
        ("ftp://jobs.example.com/role", False),
        ("jobs.example.com/role", False),
        ("https://", False),
        ("https://[::1", False),
    ],
)
def test_an_absolute_http_url_names_a_scheme_and_a_host(value: str, absolute: bool) -> None:
    assert is_absolute_http_url(value) is absolute


def test_an_exact_url_key_keeps_the_whole_query_and_folds_only_case_and_slash() -> None:
    assert (
        exact_url_key("HTTPS://Jobs.Example.com:8443/Role/7/?utm_source=x&b=2")
        == "https://jobs.example.com:8443/Role/7?utm_source=x&b=2"
    )
    assert exact_url_key("jobs.example.com/role") == ""


def test_url_parts_read_the_host_decoded_path_and_query_names() -> None:
    url = "https://Careers.Example.com:443/en-us/Remote%20Roles/?JobId=7&page=2"

    assert url_host(url) == "careers.example.com"
    assert url_path_segments(url) == ("en-us", "Remote Roles")
    assert url_query_keys(url) == frozenset({"jobid", "page"})
    assert (url_host("not a url"), url_path_segments("https://[::1"), url_query_keys("")) == (
        "",
        (),
        frozenset(),
    )
