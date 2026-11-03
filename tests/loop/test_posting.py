"""What makes one posting the same posting, and two postings different ones."""

from __future__ import annotations

import pytest

from clearscout.application.discovery.posting_identity import (
    Origin,
    content_hash,
    identify,
    keeps_stored_layout,
    place_blind_hash,
    posting_board_uuid,
    posting_identity_keys,
    posting_requisition_key,
    posting_url_key,
    same_words,
)
from clearscout.infrastructure.web_fetch.ingest import JDText

GREENHOUSE_URL = "https://job-boards.greenhouse.io/globex/jobs/6001234004"
LEVER_URL = "https://jobs.lever.co/initech/7d811a2b-31b8-453f-94db-ff456ffd0376"
ASHBY_URL = "https://jobs.ashbyhq.com/globex/7a4c1e52-9d3b-4e8f-a6b2-1c0d5e7f3a98"


def jd(origin: str, text: str = "A posting. " * 60) -> JDText:
    """Build posting text as ingest would hand it over."""
    return JDText(text=text, origin=origin, method="test")


def test_every_board_the_reader_supports_yields_a_requisition_identity() -> None:
    """A board the reader supports and posting identity does not falls back to a hash.

    A hash quietly loses the property that a re-run after the employer edits a
    word is the same posting.
    """
    from clearscout.infrastructure.web_fetch.ingest.url import platform_readers as url

    readable = {"greenhouse", "lever", "workday", "ashby"}
    supported = {name for name in readable if getattr(url, name.upper(), None) is not None}
    assert supported == readable, "the reader knows a board this test does not"

    for target, expected in (
        (GREENHOUSE_URL, "gh-"),
        (LEVER_URL, "lv-"),
        (ASHBY_URL, "ab-"),
    ):
        posting = identify(jd(target))
        assert posting.origin is Origin.REQUISITION, target
        assert posting.identifier.startswith(expected), posting.identifier


def test_a_greenhouse_requisition_is_the_identity() -> None:
    """The employer already assigned one, and it is what an applicant sees."""
    posting = identify(jd(GREENHOUSE_URL))
    assert posting.origin is Origin.REQUISITION
    assert posting.identifier == "gh-6001234004"


def test_a_lever_requisition_is_recognised_too() -> None:
    posting = identify(jd(LEVER_URL))
    assert posting.origin is Origin.REQUISITION
    assert posting.identifier.startswith("lv-")


def test_two_requisitions_at_one_company_do_not_collide() -> None:
    """Same company, same title, two openings, two identities."""
    first = identify(jd("https://job-boards.greenhouse.io/initech/jobs/8501234002"))
    second = identify(jd("https://job-boards.greenhouse.io/initech/jobs/8556870002"))
    assert first.identifier != second.identifier


WORKDAY_URL = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R12345"


def _workday_spellings(url: str) -> dict[str, str]:
    host, path = url.split("://", 1)[1].split("/", 1)
    return {
        "apply": f"{url}/apply",
        "apply-slash": f"{url}/apply/",
        "application": f"{url}/application",
        "apply-manually": f"{url}/apply/applyManually",
        "tracking-query": f"{url}?utm_source=feed&source=board",
        "fragment": f"{url}#top",
        "host-upper": f"https://{host.upper()}/{path}",
        "default-port": f"https://{host}:443/{path}",
        "no-location": url.replace("/job/Remote/", "/job/"),
    }


@pytest.mark.parametrize(
    ("form", "url"),
    list(_workday_spellings(WORKDAY_URL).items()),
    ids=_workday_spellings(WORKDAY_URL),
)
def test_a_workday_posting_keeps_its_requisition_identity_however_it_is_addressed(
    form: str, url: str
) -> None:
    posting = identify(jd(url))

    assert posting.origin is Origin.REQUISITION
    assert posting.identifier == identify(jd(WORKDAY_URL)).identifier


@pytest.mark.parametrize("action", ["apply", "apply/applyManually", "apply/autofillWithResume"])
def test_two_employers_workday_apply_addresses_keep_apart(action: str) -> None:
    acme = identify(jd(f"{WORKDAY_URL}/{action}"))
    globex = identify(
        jd(f"https://globex.wd1.myworkdayjobs.com/Careers/job/Berlin/Nurse_R999/{action}")
    )

    assert acme.identifier != globex.identifier


@pytest.mark.parametrize(
    "url",
    [
        f"https://click.example.net/r?u={WORKDAY_URL}",
        f"https://aggregator.example.net/out/{WORKDAY_URL.split('://', 1)[1]}",
        f"https://click.example.net/r?u={GREENHOUSE_URL}",
        f"https://aggregator.example.net/out/{LEVER_URL.split('://', 1)[1]}",
        WORKDAY_URL.replace("myworkdayjobs", "evilmyworkdayjobs"),
        GREENHOUSE_URL.replace("https://", "https://evil.example\\@"),
        GREENHOUSE_URL.replace("job-boards.greenhouse.io", "job-boards.greenhouse.io@example.com"),
    ],
    ids=[
        "workday-query",
        "workday-path",
        "greenhouse-query",
        "lever-path",
        "workday-lookalike",
        "greenhouse-after-backslash",
        "greenhouse-as-userinfo",
    ],
)
def test_a_board_address_the_host_does_not_serve_states_no_requisition(url: str) -> None:
    assert identify(jd(url)).origin is Origin.CONTENT


def test_a_posting_with_no_requisition_falls_back_to_its_words() -> None:
    """A PDF or a screenshot has no identifier, so the content is all there is."""
    posting = identify(jd("posting.pdf"))
    assert posting.origin is Origin.CONTENT


def test_the_content_hash_ignores_layout_but_not_words() -> None:
    """Two fetches of one page differ in whitespace without the posting changing."""
    assert content_hash("Senior  Data\n\nEngineer") == content_hash("senior data engineer")
    assert content_hash("Senior Data Engineer") != content_hash("Staff Data Engineer")


def test_a_relaid_body_says_the_same_words() -> None:
    assert same_words("Senior  Data\n\nEngineer, $120k.", "senior data engineer $120k")
    # A decomposed accent and its composed letter are one letter laid out two ways.
    assert same_words("Caf\u00e9 lead", "Cafe\u0301 lead")


def test_a_read_keeps_the_stored_layout_only_when_it_adds_no_lines_or_markers() -> None:
    flat = "Operations Lead Overview Build reliable systems. Requirements Five years."
    lined = "Operations Lead\n\nOverview\nBuild reliable systems.\n\nRequirements\n- Five years."
    assert not keeps_stored_layout(flat, lined)
    assert keeps_stored_layout(lined, flat)
    assert keeps_stored_layout(lined, lined.replace("\n\n", "\n\n\n").replace(" ", "  "))
    assert not keeps_stored_layout(lined, lined.replace("Five", "Six"))
    assert not keeps_stored_layout(lined.replace("- ", ""), lined)


def test_two_readers_that_disagree_on_layout_settle_on_the_listed_one() -> None:
    listed = "Operations Lead\n\n- Ship code\n- Test code"
    lined = "Operations Lead\nShip\ncode\nTest\ncode"
    assert keeps_stored_layout(listed, lined)
    assert not keeps_stored_layout(lined, listed)


def test_a_currency_change_is_not_the_same_words() -> None:
    assert not same_words("Salary $120,000", "Salary \u20ac120,000")
    assert content_hash("Salary $120,000") != content_hash("Salary \u20ac120,000")


def test_an_accented_letter_change_is_not_the_same_words() -> None:
    assert not same_words("Ingeniero de dise\u00f1o", "Ingeniero de diseno")
    assert content_hash("\u30c7\u30fc\u30bf") != content_hash("\u30c6\u30b9\u30c8")


def test_the_place_blind_hash_ignores_a_recruiter_tag_but_not_a_word() -> None:
    """One opening posted per city carries a different LinkedIn tag on each copy."""
    tagged = place_blind_hash("Data Engineer #LI-Remote", ())
    assert tagged == place_blind_hash("Data Engineer #LI-RemoteUS", ())
    assert tagged != place_blind_hash("Staff Engineer #LI-Remote", ())


def test_a_content_identity_survives_a_re_fetch_of_the_same_document() -> None:
    same = "Requirements:\n- Go\n- Postgres\n"
    assert identify(jd("a.pdf", same)).identifier == identify(jd("b.pdf", same)).identifier


def test_a_posting_url_key_unquotes_the_path_once() -> None:
    """A path whose literal text is '%20' is a different posting from one holding a space."""
    encoded = posting_url_key("https://initech.test/jobs/a%2520b")

    assert encoded != posting_url_key("https://initech.test/jobs/a%20b")


_DOVER_SCANNED = "https://app.dover.io/apply/Globex%20Health/9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0"


@pytest.mark.parametrize(
    "pasted",
    [
        "https://app.dover.com/apply/Globex%20Health/9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0",
        "https://APP.DOVER.COM/apply/Globex Health/9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0/",
    ],
    ids=["dover-com", "dover-com-unencoded"],
)
def test_a_dover_posting_keys_the_same_at_either_dover_host(pasted: str) -> None:
    assert posting_identity_keys(pasted) == posting_identity_keys(_DOVER_SCANNED)


_ICIMS = "https://careers-examplehealth.icims.com/jobs"


@pytest.mark.parametrize(
    ("listed", "read"),
    [
        (f"{_ICIMS}/20181/job", f"{_ICIMS}/20181/job?in_iframe=1"),
        (f"{_ICIMS}/20437/product-manager/job", f"{_ICIMS}/20437/job?in_iframe=1"),
        (
            f"{_ICIMS}/18632/technical-product-owner-%28ai%e2%80%91native-product-lead%29/job",
            f"{_ICIMS}/18632/job?in_iframe=1",
        ),
    ],
    ids=["unslugged", "slugged", "encoded-slug"],
)
def test_a_classic_icims_posting_shares_a_key_across_its_address_forms(
    listed: str, read: str
) -> None:
    assert posting_requisition_key(listed) is not None
    assert posting_requisition_key(listed) == posting_requisition_key(read)


def test_a_classic_icims_requisition_is_scoped_to_its_tenant() -> None:
    other = "https://careers-globex.icims.com/jobs/20181/job"

    assert posting_requisition_key(other) != posting_requisition_key(f"{_ICIMS}/20181/job")


def test_a_labelled_icims_posting_shares_a_key_across_its_address_forms() -> None:
    listed = "https://uscareers-examplehealth.icims.com/jobs/20437/product-manager/job"
    read = "https://uscareers-examplehealth.icims.com/jobs/20437/job?in_iframe=1"

    assert posting_requisition_key(listed) is not None
    assert posting_requisition_key(listed) == posting_requisition_key(read)
    assert posting_requisition_key(listed) != posting_requisition_key(f"{_ICIMS}/20437/job")


def test_an_eu_greenhouse_posting_shares_a_key_with_its_us_mirror() -> None:
    eu = posting_requisition_key("https://job-boards.eu.greenhouse.io/acme/jobs/4012345")

    assert eu is not None
    assert eu == posting_requisition_key("https://job-boards.greenhouse.io/acme/jobs/4012345")


_CAREERS_PATH = "careers/principal-data-engineer-500001/job/"
_CAREERS_EN = f"https://careers.example.com/en/{_CAREERS_PATH}"


@pytest.mark.parametrize("locale", ["jp", "cn", "kr", "en-us", "en_GB", "pt-BR", "pt_BR"])
def test_a_careers_site_posting_shares_a_key_across_leading_locales(locale: str) -> None:
    variant = f"https://careers.example.com/{locale}/{_CAREERS_PATH}"

    assert posting_requisition_key(variant) is not None
    assert posting_requisition_key(variant) == posting_requisition_key(_CAREERS_EN)


@pytest.mark.parametrize(
    "other",
    [
        "https://careers.example.com/en/careers/staff-data-engineer-500002/job/",
        "https://careers.example.org/en/careers/principal-data-engineer-500001/job/",
        "https://careers.example.com/en/",
    ],
    ids=["another path", "another host", "bare locale"],
)
def test_a_locale_fold_keeps_other_postings_apart(other: str) -> None:
    assert posting_identity_keys(other).isdisjoint(posting_identity_keys(_CAREERS_EN))


@pytest.mark.parametrize("segment", ["careers", "job", "usa"])
def test_a_non_locale_first_segment_does_not_fold(segment: str) -> None:
    prefixed = f"https://careers.example.com/{segment}/postings/principal-data-engineer-500001"
    bare = "https://careers.example.com/postings/principal-data-engineer-500001"

    assert posting_identity_keys(prefixed).isdisjoint(posting_identity_keys(bare))


def test_two_board_tenants_with_two_letter_names_stay_apart() -> None:
    first = "https://jobs.smartrecruiters.com/ab/744000012345-data-engineer"
    second = "https://jobs.smartrecruiters.com/cd/744000012345-data-engineer"

    assert posting_identity_keys(first).isdisjoint(posting_identity_keys(second))


def test_a_locale_fold_leaves_the_url_key_unchanged() -> None:
    assert posting_url_key(_CAREERS_EN) in posting_identity_keys(_CAREERS_EN)
    assert posting_url_key(_CAREERS_EN) != posting_url_key(_CAREERS_EN.replace("/en/", "/jp/"))


def test_a_host_ending_in_a_dover_host_keys_apart_from_dover() -> None:
    lookalike = _DOVER_SCANNED.replace("app.dover.io", "app.dover.com.evil.example")

    assert posting_identity_keys(lookalike).isdisjoint(posting_identity_keys(_DOVER_SCANNED))


_BOARD_GUID = "11111111-2222-4333-8444-555555555555"
_JOB_GUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_UKG_BOARD = f"https://recruiting2.example-ukg.com/ACME1001/JobBoard/{_BOARD_GUID}"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{_UKG_BOARD}/OpportunityDetail?opportunityId={_JOB_GUID}", _JOB_GUID),
        (f"{_UKG_BOARD}/OpportunityDetail?opportunityId=4102", None),
        (
            f"https://workforcenow.example-adp.com/recruitment.html?cid={_BOARD_GUID}"
            "&ccId=19000101_000001&jobId=1000000000001_1",
            None,
        ),
        (f"https://careers.acme.example/job/{_JOB_GUID}/operations-lead/", _JOB_GUID),
        (f"https://jobs.lever.co/acme/{_JOB_GUID}", _JOB_GUID),
        (f"https://careers.acme.example/team/{_BOARD_GUID}/job/{_JOB_GUID}", _JOB_GUID),
        (f"https://careers.acme.example/job/{_JOB_GUID}?utm_id=spring", _JOB_GUID),
        ("https://careers.acme.example/job/4102/operations-lead/", None),
    ],
    ids=[
        "ukg-query",
        "query-non-uuid",
        "adp-tenant-uuid",
        "wrapper",
        "lever",
        "last-path",
        "tracking",
        "short-id",
    ],
)
def test_the_board_uuid_is_the_one_naming_the_posting(url: str, expected: str | None) -> None:
    assert posting_board_uuid(url) == expected
