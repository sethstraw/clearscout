"""Board identity, and single-employer detail URL derivation."""

import itertools

import pytest

from clearscout.infrastructure.web_fetch import board_adapters, boards
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.boards import (
    board_identity,
    job_board_address,
    single_employer_detail_url,
    single_tenant_board,
    without_apply_suffix,
)
from clearscout.infrastructure.web_fetch.platforms import adp, paylocity, workday


def test_board_identity_matches_a_stored_root_and_a_detail_url_for_workday() -> None:
    stored = "https://examplehealth.wd5.myworkdayjobs.com/en-US/External"
    detail = "https://examplehealth.wd5.myworkdayjobs.com/External/job/JR12345"

    assert board_identity(stored) == board_identity(detail)
    assert board_identity(detail) == ("workday", "examplehealth.wd5.myworkdayjobs.com/external")


def test_board_identity_matches_a_stored_root_and_a_detail_url_for_oracle() -> None:
    stored = (
        "https://examplehealth.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/"
        "en/sites/CX_1/requisitions"
    )
    detail = (
        "https://examplehealth.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/"
        "en-GB/sites/CX_1/job/12345"
    )

    assert board_identity(stored) == board_identity(detail)


def test_board_identity_ignores_org_case_for_ashby_and_smartrecruiters() -> None:
    assert board_identity("https://jobs.ashbyhq.com/ExampleHealth") == board_identity(
        "https://jobs.ashbyhq.com/examplehealth/11111111-1111-4111-8111-111111111111"
    )
    assert board_identity("https://careers.smartrecruiters.com/ExampleHealth") == board_identity(
        "https://careers.smartrecruiters.com/examplehealth/744000012345678-staff"
    )


def test_board_identity_matches_a_smartrecruiters_jobs_detail_url() -> None:
    root = "https://careers.smartrecruiters.com/ExampleHealth"
    detail = "https://jobs.smartrecruiters.com/examplehealth/744000012345678-staff-engineer"

    assert board_identity(root) == board_identity(detail)


def test_board_identity_matches_a_greenhouse_embed_application_url() -> None:
    root = "https://boards.greenhouse.io/ExampleHealth"
    detail = "https://boards.greenhouse.io/embed/job_app?for=examplehealth"

    assert board_identity(root) == board_identity(detail)


def test_board_identity_ignores_unrelated_smartrecruiters_and_greenhouse_urls() -> None:
    assert board_identity("https://jobs.smartrecruiters.com/examplehealth/openings") is None
    assert board_identity("https://boards.greenhouse.io/embed/job_app?source=campaign") is None
    assert (
        board_identity("https://notjobs.smartrecruiters.com/examplehealth/744000012345678") is None
    )
    assert board_identity("https://notboards.greenhouse.io/embed/job_app?for=examplehealth") is None


def test_board_identity_matches_a_stored_root_and_a_detail_url_for_workable() -> None:
    # WORKABLE_BOARD's org segment is lowercase-only, so unlike the other
    # org-slug platforms a stored root can never carry a mixed-case org.
    assert board_identity("https://apply.workable.com/examplehealth") == board_identity(
        "https://apply.workable.com/examplehealth/j/1A2B3C4D5E"
    )


def test_board_identity_matches_a_stored_root_and_a_detail_url_for_recruitee() -> None:
    assert board_identity("https://acme.recruitee.com") == ("recruitee", "acme")
    assert board_identity("https://acme.recruitee.com/o/staff-engineer") == ("recruitee", "acme")


def test_board_identity_is_none_for_an_unrecognised_url() -> None:
    assert board_identity("https://careers.examplehealth.com/openings") is None


def test_single_employer_detail_url_is_none_for_a_multi_employer_tenant() -> None:
    tenant = min(boards.MULTI_EMPLOYER_ATS_TENANTS)
    detail = f"https://jobs.lever.co/{tenant}/33333333-3333-4333-8333-333333333333"

    assert single_employer_detail_url(detail) is None


def test_single_employer_detail_url_is_none_for_a_refused_url() -> None:
    assert single_employer_detail_url("http://169.254.169.254/latest/meta-data/") is None


def test_single_employer_detail_url_is_none_for_a_url_matching_no_ats_pattern() -> None:
    assert single_employer_detail_url("https://careers.examplehealth.com/openings/staff") is None


def test_single_employer_detail_url_is_none_for_a_workable_short_link() -> None:
    assert single_employer_detail_url("https://apply.workable.com/j/4A1B2C3D4E") is None


@pytest.mark.parametrize(
    "detail",
    ["https://acme.recruitee.com/o/staff-engineer", "https://acme.recruitee.com/l/en/o/lead"],
)
def test_single_employer_detail_url_recognises_a_recruitee_job_detail(detail: str) -> None:
    assert single_employer_detail_url(detail) == detail
    assert single_employer_detail_url(f"{detail}/c/new") == detail


@pytest.mark.parametrize(
    "detail",
    ["https://acme.app.loxo.co/job/MTAwMTQtd2lkZ2V0cw==", "https://acme.app.loxo.co/job/YWJjZGVm"],
    ids=["padded", "unpadded"],
)
def test_a_loxo_job_detail_is_a_job_board_address_that_names_no_single_employer(
    detail: str,
) -> None:
    assert job_board_address(detail)
    assert single_employer_detail_url(detail) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.app.loxo.co/jobs",
        "https://acme.app.loxo.co/job/",
        "https://acme.app.loxo.co/",
        "https://acme.app.loxo.co/login",
    ],
)
def test_a_loxo_page_naming_no_job_is_not_a_job_board_address(url: str) -> None:
    assert not job_board_address(url)


def test_an_ats_career_site_no_adapter_reads_is_still_a_job_board_address() -> None:
    assert job_board_address("https://careers-examplehealth.icims.com/jobs/search?ss=1")


def test_a_greenhouse_posting_hosted_on_the_employers_site_is_a_job_board_address() -> None:
    """One posting's page, not the site that describes the company."""
    assert job_board_address("https://www.acme.test/careers/job?gh_jid=4567")
    assert not job_board_address("https://www.acme.test/careers/")


def test_without_apply_suffix_trims_c_new_only_from_a_recruitee_posting() -> None:
    other = "https://careers.examplehealth.com/o/staff-engineer/c/new"

    assert without_apply_suffix(other) == other


def test_single_employer_detail_url_trims_a_trailing_application_suffix() -> None:
    detail = "https://jobs.ashbyhq.com/examplehealth/11111111-1111-4111-8111-111111111111"

    assert single_employer_detail_url(f"{detail}/application") == detail


def test_without_apply_suffix_only_trims_the_trailing_segment() -> None:
    assert (
        without_apply_suffix("https://jobs.lever.co/examplehealth/apply-now")
        == "https://jobs.lever.co/examplehealth/apply-now"
    )


def test_without_apply_suffix_trims_the_ashby_application_suffix() -> None:
    detail = "https://jobs.ashbyhq.com/examplehealth/11111111-1111-4111-8111-111111111111"

    assert without_apply_suffix(f"{detail}/application") == detail


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://job-boards.greenhouse.io/examplehealth/jobs/4001", True, id="greenhouse"
        ),
        pytest.param("https://examplehealth.teamtailor.com/jobs/4001", True, id="teamtailor"),
        pytest.param("https://himalayas.app/jobs/staff-engineer", False, id="himalayas"),
        pytest.param("https://www.arbeitnow.com/view/staff-engineer", False, id="arbeitnow"),
        pytest.param("https://careers.examplehealth.com/openings/staff", False, id="own-domain"),
        pytest.param(
            "https://himalayas.app/jobs/staff?next=https://jobs.lever.co/examplehealth",
            False,
            id="board-address-in-a-query",
        ),
        pytest.param(
            "https://aggregator.example/out/boards.greenhouse.io/examplehealth/jobs/555",
            False,
            id="board-address-in-a-path",
        ),
    ],
)
def test_single_tenant_board_names_only_a_one_employer_address(url: str, expected: bool) -> None:
    assert single_tenant_board(url) is expected


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("https://jobs.lever.co/{tenant}", id="org-slug"),
        pytest.param("https://{tenant}.teamtailor.com/jobs/4001", id="subdomain"),
    ],
)
def test_single_tenant_board_refuses_a_multi_employer_tenant(address: str) -> None:
    tenant = min(boards.MULTI_EMPLOYER_ATS_TENANTS)

    assert single_tenant_board(address.format(tenant=tenant)) is False


class TestCanonicaliseSourceUrl:
    """Reduce a stored source address to the board root the scanner enumerates."""

    def test_the_retired_greenhouse_embed_becomes_the_modern_tenant(self) -> None:
        canonical = boards.canonicalise_source_url(
            "https://boards.greenhouse.io/embed/job_board/js?for=acme"
        )
        assert canonical == "https://job-boards.greenhouse.io/acme"

    def test_a_greenhouse_tenant_carrying_a_tracking_query_loses_it(self) -> None:
        canonical = boards.canonicalise_source_url(
            "https://job-boards.greenhouse.io/globex?gh_src=abc123def4us"
        )
        assert canonical == "https://job-boards.greenhouse.io/globex"

    def test_the_legacy_host_carrying_a_tracking_query_becomes_the_modern_host(self) -> None:
        canonical = boards.canonicalise_source_url(
            "https://boards.greenhouse.io/initech?gh_src=my.greenhouse.search"
        )
        assert canonical == "https://job-boards.greenhouse.io/initech"

    def test_a_greenhouse_single_posting_permalink_becomes_the_board_root(self) -> None:
        canonical = boards.canonicalise_source_url(
            "https://job-boards.greenhouse.io/beta/jobs/6009876004"
        )
        assert canonical == "https://job-boards.greenhouse.io/beta"

    @pytest.mark.parametrize(
        "address",
        [
            "https://job-boards.eu.greenhouse.io/acme",
            "https://job-boards.eu.greenhouse.io/acme/jobs/4012345",
            "https://boards.eu.greenhouse.io/acme?gh_src=abc123",
            "https://boards.eu.greenhouse.io/embed/job_board?for=acme",
            "job-boards.eu.greenhouse.io/acme/jobs/4012345",
            "https://JOB-BOARDS.EU.GREENHOUSE.IO/acme",
        ],
    )
    def test_an_eu_greenhouse_address_keeps_its_eu_board_root(self, address: str) -> None:
        assert boards.canonicalise_source_url(address) == "https://job-boards.eu.greenhouse.io/acme"

    def test_an_eu_greenhouse_board_is_the_same_board_as_its_token(self) -> None:
        eu = board_identity("https://job-boards.eu.greenhouse.io/Acme/jobs/4012345")
        assert eu == board_identity("https://job-boards.greenhouse.io/acme")

    def test_a_bracketed_host_still_roots_its_greenhouse_board(self) -> None:
        canonical = boards.canonicalise_source_url("https://[boards.greenhouse.io/acme/jobs/1")
        assert canonical == "https://job-boards.greenhouse.io/acme"

    @pytest.mark.parametrize(
        ("address", "root"),
        [
            (
                "https://uscareers-acme.icims.com/jobs/20181/clinical-analyst/job",
                "https://uscareers-acme.icims.com/jobs/search",
            ),
            (
                "https://employment-examplehealth.icims.com/jobs/search?ss=1",
                "https://employment-examplehealth.icims.com/jobs/search",
            ),
            (
                "https://careersacmecan-acme.icims.com/jobs/7/job",
                "https://careersacmecan-acme.icims.com/jobs/search",
            ),
            (
                "https://careers-acme-ita.icims.com/jobs/7/job",
                "https://careers-acme-ita.icims.com/jobs/search",
            ),
        ],
    )
    def test_a_labelled_icims_host_roots_its_own_classic_board(
        self, address: str, root: str
    ) -> None:
        assert boards.canonicalise_source_url(address) == root

    def test_icims_label_hosts_of_one_employer_are_distinct_boards(self) -> None:
        us = board_identity("https://uscareers-acme.icims.com/jobs/search")
        assert us is not None
        assert us != board_identity("https://careers-acme.icims.com/jobs/search")

    @pytest.mark.parametrize(
        "address",
        [
            "https://www.icims.com/jobs/search",
            "https://acme.icims.com/jobs/search",
            "https://icims.com.evil.test/jobs/search",
            "https://careers-acme.icims.com.evil.test/jobs/search",
        ],
    )
    def test_an_icims_lookalike_names_no_classic_board(self, address: str) -> None:
        assert board_identity(address) is None

    def test_a_lever_single_posting_permalink_becomes_the_board_root(self) -> None:
        canonical = boards.canonicalise_source_url(
            "https://jobs.lever.co/example/9f1b3d64-2e5a-4c7b-8d9e-0a6c4b2e1f35"
        )
        assert canonical == "https://jobs.lever.co/example"

    @pytest.mark.parametrize(
        ("posting", "board"),
        [
            (
                "https://jobs.dayforcehcm.com/en-US/acme/GLOBEXCANDIDATEPORTAL/jobs/1458",
                "https://jobs.dayforcehcm.com/en-US/acme/GLOBEXCANDIDATEPORTAL",
            ),
            (
                "https://gamma.bamboohr.com/careers/43",
                "https://gamma.bamboohr.com/careers",
            ),
            (
                "https://healthtech.pinpointhq.com/en/postings/abc-123",
                "https://healthtech.pinpointhq.com",
            ),
            (
                "https://alpha.teamtailor.com/jobs/7100001-product-manager",
                "https://alpha.teamtailor.com",
            ),
            (
                "https://alpha.na.teamtailor.com/jobs/7100001-product-manager",
                "https://alpha.na.teamtailor.com",
            ),
            (
                "https://synthetic.jobs.personio.com/job/2100001?language=en",
                "https://synthetic.jobs.personio.com",
            ),
            (
                "https://ats.rippling.com/globex/jobs/3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f",
                "https://ats.rippling.com/globex/jobs",
            ),
            (
                "https://acme.breezy.hr/p/7a86407-data-analyst",
                "https://acme.breezy.hr",
            ),
            (
                "https://acme.applytojob.com/apply/Xq7Lm2Pz9K/Clinical-Data-Analyst",
                "https://acme.applytojob.com/apply",
            ),
            (
                "https://jobs.jobvite.com/acme/job/oAb12cDe",
                "https://jobs.jobvite.com/acme/jobs",
            ),
            (
                "https://acme.zohorecruit.com/jobs/Careers/589893000022148081",
                "https://acme.zohorecruit.com/jobs/Careers",
            ),
            (
                "https://acme.avature.net/careers/JobDetail/Product-Manager/5703",
                "https://acme.avature.net/careers",
            ),
        ],
    )
    def test_supported_single_posting_permalink_becomes_its_board_root(
        self, posting: str, board: str
    ) -> None:
        assert boards.canonicalise_source_url(posting) == board

    def test_a_canonical_greenhouse_board_passes_through_unchanged(self) -> None:
        canonical = boards.canonicalise_source_url("https://job-boards.greenhouse.io/northwind")
        assert canonical == "https://job-boards.greenhouse.io/northwind"

    def test_a_canonical_lever_board_passes_through_unchanged(self) -> None:
        canonical = boards.canonicalise_source_url("https://jobs.lever.co/initrode")
        assert canonical == "https://jobs.lever.co/initrode"

    def test_a_careers_page_front_passes_through_unchanged(self) -> None:
        canonical = boards.canonicalise_source_url("https://careers.example.com/openings")
        assert canonical == "https://careers.example.com/openings"

    def test_a_teamtailor_posting_on_an_employers_own_domain_keeps_its_own_host(self) -> None:
        posting = "https://careers.example-health.test/jobs/7100001-product-manager"
        assert boards.canonicalise_source_url(posting) == posting

    def test_leading_and_trailing_whitespace_is_stripped(self) -> None:
        canonical = boards.canonicalise_source_url("  https://job-boards.greenhouse.io/northwind  ")
        assert canonical == "https://job-boards.greenhouse.io/northwind"

    def test_an_empty_url_passes_through_so_validation_still_refuses_it(self) -> None:
        assert boards.canonicalise_source_url("") == ""
        assert boards.canonicalise_source_url("   ") == ""

    def test_an_ashby_detail_url_canonicalises_to_its_recognised_board_root(self) -> None:
        detail = "https://jobs.ashbyhq.com/examplehealth/11111111-1111-4111-8111-111111111111"

        root = boards.canonicalise_source_url(detail)

        assert root == "https://jobs.ashbyhq.com/examplehealth"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "ashby"

    @pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
    def test_a_workday_detail_url_canonicalises_to_its_recognised_board_root(
        self, locale: str
    ) -> None:
        detail = f"https://examplehealth.wd5.myworkdayjobs.com/{locale}External/job/JR12345"

        root = boards.canonicalise_source_url(detail)

        assert root == f"https://examplehealth.wd5.myworkdayjobs.com/{locale}External"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "workday"
        unlocalized = "https://examplehealth.wd5.myworkdayjobs.com/External"
        identity = boards.board_identity(unlocalized)
        assert identity is not None
        assert boards.board_identity(root) == boards.board_identity(detail) == identity

    def test_an_oracle_recruiting_cloud_detail_url_canonicalises_to_its_board_root(self) -> None:
        detail = (
            "https://examplehealth.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/"
            "en/sites/CX_1/job/12345"
        )

        root = boards.canonicalise_source_url(detail)

        assert root == (
            "https://examplehealth.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"
        )
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "oracle"

    def test_a_smartrecruiters_detail_url_canonicalises_to_its_board_root(self) -> None:
        detail = "https://careers.smartrecruiters.com/examplehealth/744000012345678-staff-engineer"

        root = boards.canonicalise_source_url(detail)

        assert root == "https://careers.smartrecruiters.com/examplehealth"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "smartrecruiters"

    def test_a_workable_detail_url_canonicalises_to_its_board_root(self) -> None:
        detail = "https://apply.workable.com/examplehealth/j/1A2B3C4D5E/"

        root = boards.canonicalise_source_url(detail)

        assert root == "https://apply.workable.com/examplehealth"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "workable"

    @pytest.mark.parametrize(
        "detail",
        [
            "https://acme.recruitee.com/o/staff-engineer",
            "https://acme.recruitee.com/o/lead/c/new",
            "https://acme.recruitee.com/l/en/o/staff-engineer",
            "https://acme.recruitee.com/l/pt-br/o/lead/c/new",
        ],
    )
    def test_a_recruitee_detail_url_canonicalises_to_its_board_root(self, detail: str) -> None:
        root = boards.canonicalise_source_url(detail)

        assert root == "https://acme.recruitee.com"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "recruitee"

    def test_a_workable_short_job_link_is_left_unchanged(self) -> None:
        short_link = "https://apply.workable.com/j/1A2B3C4D5E"

        assert boards.canonicalise_source_url(short_link) == short_link
        assert boards.board_identity(short_link) is None

    def test_a_workable_api_v3_accounts_address_is_left_unchanged(self) -> None:
        api = "https://apply.workable.com/api/v3/accounts/acme/jobs"

        assert boards.canonicalise_source_url(api) == api
        assert boards.board_identity(api) is None

    def test_a_workable_widget_api_address_is_left_unchanged(self) -> None:
        widget = "https://apply.workable.com/api/v1/widget/accounts/acme"

        assert boards.canonicalise_source_url(widget) == widget

    def test_a_smartrecruiters_api_address_is_left_unchanged(self) -> None:
        api = "https://api.smartrecruiters.com/v1/companies/acme/postings"

        assert boards.canonicalise_source_url(api) == api

    def test_an_icims_detail_url_is_left_unchanged(self) -> None:
        detail = "https://examplehealth.icims.com/jobs/12345/staff-engineer/job"

        # Only a <label>-<tenant> host names a classic board; a Jibe site is found from its markup.
        assert boards.canonicalise_source_url(detail) == detail

    def test_an_adp_requisition_url_canonicalises_to_its_career_center(self) -> None:
        detail = (
            f"{_ADP_PAGE}?jobId=9200000000001_1&cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d&lang=en_CA"
        )

        root = boards.canonicalise_source_url(detail)

        assert root == f"{_ADP_PAGE}?cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d&lang=en_CA"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "adp"

    @pytest.mark.parametrize(
        "address",
        [
            "https://acme.applytojob.com",
            "https://acme.applytojob.com/",
            "https://Acme.applytojob.com/apply",
            "https://acme.applytojob.com/apply/",
        ],
    )
    def test_a_jazzhr_tenant_root_and_its_apply_page_are_one_board(self, address: str) -> None:
        root = boards.canonicalise_source_url(address)

        assert root == "https://acme.applytojob.com/apply"
        identified = board_adapters.identify_board(root)
        assert identified is not None and identified[0] == "jazzhr"


_ADP_PAGE = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"


def test_adp_career_centers_are_told_apart_by_their_client_id() -> None:
    first = f"{_ADP_PAGE}?cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d&lang=en_US"
    second = f"{_ADP_PAGE}?cid=7f6e5d4c-3b2a-4190-8f7e-6d5c4b3a2f1e&lang=en_US"
    posting = f"{_ADP_PAGE}?cid=0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D&jobId=9200000000001_1"

    assert board_identity(first) == ("adp", "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
    assert board_identity(second) != board_identity(first)
    assert board_identity(posting) == board_identity(first)
    assert single_employer_detail_url(posting) == posting
    assert single_employer_detail_url(first) is None


@pytest.mark.parametrize("career_center", ["19000101_000001_2", "19000101_000001_3"])
@pytest.mark.parametrize("center_first", [False, True])
@pytest.mark.parametrize("uppercase", [False, True])
@pytest.mark.parametrize("separator", ["&", "&amp;"])
def test_adp_career_centers_of_one_client_have_distinct_board_identities(
    career_center: str, center_first: bool, uppercase: bool, separator: str
) -> None:
    cid = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    parts = [f"cid={cid}", f"ccId={career_center}", "lang=en_CA"]
    if center_first:
        parts.reverse()
    query = separator.join(parts)
    if uppercase:
        query = query.upper()
    board = f"{_ADP_PAGE}?{query}"
    posting = f"{board}{separator}jobId=9200000000001_1"
    other = f"{_ADP_PAGE}?cid={cid}&ccId=19000101_000001_4"

    assert board_identity(board) != board_identity(other)
    assert board_identity(board) != board_identity(f"{_ADP_PAGE}?cid={cid}")
    assert board_identity(posting) == board_identity(board)


@pytest.mark.parametrize("order", list(itertools.permutations(("cid", "ccId", "lang"))))
@pytest.mark.parametrize("uppercase_keys", [False, True])
@pytest.mark.parametrize(
    "separator",
    ["&", "&amp;", "&AMP;", "&#38;", "&#038;", "&#00038;", "&#x26;", "&#x026;", "&#X00026;"],
)
def test_an_adp_posting_root_preserves_its_career_center(
    order: tuple[str, str, str], uppercase_keys: bool, separator: str
) -> None:
    cid = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    values = {"cid": cid, "ccId": "19000101_000001_2", "lang": "en_CA"}
    parts = [f"{key.upper() if uppercase_keys else key}={values[key]}" for key in order]
    posting = f"{_ADP_PAGE}?{separator.join(parts)}{separator}jobId=9200000000001_1"

    root = boards.canonicalise_source_url(posting)

    assert root == f"{_ADP_PAGE}?cid={cid}&ccId=19000101_000001_2&lang=en_CA"
    assert board_identity(root) == board_identity(posting)


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("#ccId=19000101_000001_2&lang=en_CA", ""),
        ("#&#038;ccId=19000101_000001_2&#038;lang=en_CA", ""),
        ("&#038;ccId=19000101_000001_2#lang=en_CA", "&ccId=19000101_000001_2"),
        ("&source=campaign;ccId=19000101_000001_2&lang=en_CA", "&lang=en_CA"),
    ],
)
def test_an_adp_posting_root_ignores_parameters_outside_its_query(
    suffix: str, expected: str
) -> None:
    cid = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    posting = f"{_ADP_PAGE}?cid={cid}&jobId=9200000000001_1{suffix}"

    assert boards.canonicalise_source_url(posting) == f"{_ADP_PAGE}?cid={cid}{expected}"


@pytest.mark.parametrize("separator", ["&", "&amp;", "&#038;", "&#x26;"])
def test_an_adp_fragment_names_no_board_tenant_or_requisition(separator: str) -> None:
    cid = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    fragment_tenant = f"{_ADP_PAGE}?lang=en_CA#cid={cid}{separator}jobId=9200000000001_1"
    fragment_job = f"{_ADP_PAGE}?cid={cid}#jobId=9200000000001_1{separator}ccId=19000101_000001_2"

    assert board_identity(fragment_tenant) is None
    assert single_employer_detail_url(fragment_job) is None
    assert boards.canonicalise_source_url(fragment_job) == fragment_job


def test_an_adp_external_job_number_is_not_read_as_a_requisition_id() -> None:
    short = f"{_ADP_PAGE}?cid=0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d&jobId=500851&lang=en_CA"

    assert adp.ADP_JOB.search(short) is None


# One detail URL per board the detail patterns recognize.
_DETAILS = {
    "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/4012345",
    "greenhouse-legacy": "https://boards.greenhouse.io/acme/jobs/4012345",
    "greenhouse-eu": "https://job-boards.eu.greenhouse.io/acme/jobs/4012345",
    "greenhouse-eu-legacy": "https://boards.eu.greenhouse.io/acme/jobs/4012345",
    "lever": "https://jobs.lever.co/acme/0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0",
    "ashby": "https://jobs.ashbyhq.com/acme/0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0",
    "workday": "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R12345",
    "oracle": "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/12345",
    "smartrecruiters": "https://jobs.smartrecruiters.com/acme/744000012345678-staff-engineer",
    "workable": "https://apply.workable.com/acme/j/4A1B2C3D4E",
    "recruitee": "https://acme.recruitee.com/o/staff-engineer",
    "teamtailor": "https://acme.teamtailor.com/jobs/7100001-staff-engineer",
    "pinpoint": "https://acme.pinpointhq.com/en/postings/abc-123",
    "personio": "https://acme.jobs.personio.com/job/2100001",
    "bamboohr": "https://acme.bamboohr.com/careers/43",
    "dayforce": "https://jobs.dayforcehcm.com/en-US/acme/CANDIDATEPORTAL/jobs/1458",
    "comeet": "https://www.comeet.co/jobs/acme/E5.007/staff-engineer/87.405",
    "paylocity": "https://recruiting.paylocity.com/Recruiting/Jobs/Details/7001234",
    "rippling": "https://ats.rippling.com/acme/jobs/3f2a9c1e-5b7d-4e8a-9c0f-1a2b3c4d5e6f",
    "dover": "https://app.dover.io/apply/Acme/9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0",
    "dover-com": "https://app.dover.com/apply/Acme/9e8d7c6b-5a49-4382-a716-f5e4d3c2b1a0",
    "breezy": "https://acme.breezy.hr/p/7a86407-data-analyst",
    "jazzhr": "https://acme.applytojob.com/apply/Xq7Lm2Pz9K/Clinical-Data-Analyst",
    "jobvite": "https://jobs.jobvite.com/acme/job/oAb12cDe",
    "zoho": "https://acme.zohorecruit.com/jobs/Careers/589893000022148081",
    "avature": "https://acme.avature.net/careers/JobDetail/Product-Manager/5703",
}


def _host_and_path(url: str) -> tuple[str, str]:
    host, path = url.split("://", 1)[1].split("/", 1)
    return host, path


def _wrapped(url: str) -> dict[str, str]:
    host, path = _host_and_path(url)
    return {
        "redirect-query": f"https://click.example.net/r?u={url}",
        "path-embedded": f"https://aggregator.example.net/out/{host}/{path}",
    }


@pytest.mark.parametrize(
    ("board", "form", "url"),
    [
        (board, form, wrapped)
        for board, detail in _DETAILS.items()
        for form, wrapped in _wrapped(detail).items()
    ],
)
def test_a_board_address_inside_a_redirect_wrapper_names_no_board(
    board: str, form: str, url: str
) -> None:
    assert single_employer_detail_url(url) is None
    assert board_identity(url) is None
    assert board_adapters.identify_board_url(url) is None
    assert not job_board_address(url)
    assert boards.canonicalise_source_url(url) == url


@pytest.mark.parametrize(
    "root",
    [
        "https://jobs.lever.co/acme",
        "https://job-boards.greenhouse.io/acme",
        "https://acme.wd5.myworkdayjobs.com/External",
        "https://acme.recruitee.com",
    ],
)
def test_a_board_root_inside_a_redirect_is_no_board_to_its_url_reader(root: str) -> None:
    wrapped = f"https://click.example.net/r?u={root}"

    assert board_adapters.identify_board_url(root) is not None
    assert board_adapters.identify_board_url(wrapped) is None
    # Page markup may name a board anywhere in its text.
    assert board_adapters.identify_board(f'<a href="{root}">Jobs</a>') is not None


@pytest.mark.parametrize(
    ("url", "board_host"),
    [
        ("https://:@boards.greenhouse.io/acme/jobs/123", True),
        ("https://boards.greenhouse.io./acme/jobs/123", True),
        ("https://boards.greenhouse.io.:443/acme/jobs/123", True),
        ("https://boards.greenhouse.io@example.com/acme/jobs/123", False),
        ("https://evil.example\\@boards.greenhouse.io/acme/jobs/123", False),
        ("https://boards.greenhouse.io\\@evil.example/acme/jobs/123", False),
    ],
    ids=[
        "userinfo",
        "trailing-dot",
        "trailing-dot-port",
        "board-as-userinfo",
        "backslash-before-board",
        "backslash-after-board",
    ],
)
def test_the_board_host_is_the_host_a_browser_would_reach(url: str, board_host: bool) -> None:
    assert (board_identity(url) is not None) is board_host
    assert job_board_address(url) is board_host


def _spellings(url: str) -> dict[str, str]:
    host, path = _host_and_path(url)
    return {
        "host-upper": f"https://{host.upper()}/{path}",
        "default-port": f"https://{host}:443/{path}",
        "doubled-slash": f"https://{host}//{path}",
    }


@pytest.mark.parametrize(
    ("board", "form", "url"),
    [
        (board, form, spelled)
        for board, detail in _DETAILS.items()
        for form, spelled in _spellings(detail).items()
    ],
)
def test_a_detail_url_spelled_another_way_is_still_that_detail(
    board: str, form: str, url: str
) -> None:
    detail = _DETAILS[board]

    assert single_employer_detail_url(url) == detail
    assert single_employer_detail_url(f"{url}/apply") == detail
    assert board_identity(url) == board_identity(detail)


_BOARD_HOSTS = {
    **_DETAILS,
    "recruitee-root": "https://acme.recruitee.com/",
    "pinpoint-root": "https://acme.pinpointhq.com/",
    "greenhouse-embed": "https://boards.greenhouse.io/embed/job_board?for=acme",
    "greenhouse-eu-root": "https://job-boards.eu.greenhouse.io/acme",
    "greenhouse-eu-embed": "https://boards.eu.greenhouse.io/embed/job_board?for=acme",
    "arbeitnow": "https://www.arbeitnow.com/view/staff-engineer",
    "himalayas": "https://himalayas.app/jobs/staff-engineer",
    "dover-board": "https://app.dover.io/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
    "dover-board-com": "https://app.dover.com/dover/careers/7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f",
}


def _lookalikes(url: str) -> dict[str, str]:
    host, path = _host_and_path(url)
    labels = host.split(".")
    labels[-2] = f"evil{labels[-2]}"
    return {
        "suffixed-host": f"https://{host}.evil.example/{path}",
        "prefixed-domain": f"https://{'.'.join(labels)}/{path}",
    }


@pytest.mark.parametrize("url", list(_BOARD_HOSTS.values()), ids=list(_BOARD_HOSTS))
def test_a_genuine_board_host_is_a_job_board_address(url: str) -> None:
    assert job_board_address(url)


@pytest.mark.parametrize(
    ("base", "form", "url"),
    [
        (base, form, lookalike)
        for base, genuine in _BOARD_HOSTS.items()
        for form, lookalike in _lookalikes(genuine).items()
    ],
)
def test_a_lookalike_host_names_no_board(base: str, form: str, url: str) -> None:
    assert not job_board_address(url)
    assert board_identity(url) is None
    assert not single_tenant_board(url)
    assert single_employer_detail_url(url) is None


@pytest.mark.parametrize(
    "root",
    [
        "https://acme.wd5.myworkdayjobs.com/External",
        "https://acme.wd5.myworkdayjobs.com/en-US/External",
        "https://acme.wd5.myworkdayjobs.com/en-us/External",
        "https://acme.wd5.myworkdayjobs.com/EN-US/External",
        "https://acme.wd5.myworkdayjobs.com/fr-ca/External",
        "https://acme.wd5.myworkdayjobs.com/en-us/External/job/Remote/Staff-Engineer_R12345",
    ],
)
def test_a_workday_locale_in_any_case_keys_to_the_site_it_prefixes(root: str) -> None:
    assert board_identity(root) == ("workday", "acme.wd5.myworkdayjobs.com/external")


def test_two_workday_sites_of_one_tenant_keep_apart_under_a_lowercase_locale() -> None:
    external = board_identity("https://acme.wd5.myworkdayjobs.com/en-us/External")
    campus = board_identity("https://acme.wd5.myworkdayjobs.com/en-us/Campus")

    assert external != campus


EU_LEVER_DETAIL = "https://jobs.eu.lever.co/acme/0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"


@pytest.mark.parametrize(
    "detail",
    [EU_LEVER_DETAIL, f"{EU_LEVER_DETAIL}/apply", EU_LEVER_DETAIL.replace("jobs.eu", "JOBS.EU")],
    ids=["detail", "apply", "host-upper"],
)
def test_an_eu_lever_posting_keeps_its_region_in_the_board_root(detail: str) -> None:
    assert boards.canonicalise_source_url(detail) == "https://jobs.eu.lever.co/acme"


def test_an_eu_lever_board_keys_apart_from_the_us_board_of_the_same_slug() -> None:
    eu_root = board_identity("https://jobs.eu.lever.co/acme")

    assert eu_root is not None
    assert eu_root == board_identity(EU_LEVER_DETAIL)
    assert eu_root != board_identity("https://jobs.lever.co/acme")
    assert board_identity("https://jobs.lever.co/acme") == ("lever", "acme")


def test_an_eu_lever_root_is_a_lever_board_to_every_url_reader() -> None:
    root = boards.canonicalise_source_url(EU_LEVER_DETAIL)
    identified = board_adapters.identify_board_url(root)

    assert identified is not None and identified[0] == "lever"
    assert job_board_address(root)
    assert single_tenant_board(root)


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("", "Remote/Staff-Engineer_R12345"),
        ("/apply", "Remote/Staff-Engineer_R12345"),
        ("/apply/", "Remote/Staff-Engineer_R12345"),
        ("/application", "Remote/Staff-Engineer_R12345"),
        ("/apply/applyManually", "Remote/Staff-Engineer_R12345"),
        ("/apply/autofillWithResume?source=feed", "Remote/Staff-Engineer_R12345"),
        ("#top", "Remote/Staff-Engineer_R12345"),
    ],
)
def test_a_workday_posting_path_ends_at_its_numbered_segment(suffix: str, expected: str) -> None:
    url = f"https://acme.wd5.myworkdayjobs.com/en-US/External/job/Remote/Staff-Engineer_R12345{suffix}"
    match = host_match(workday.WORKDAY, url)

    assert match is not None
    assert workday.workday_posting_path(match) == expected


_PAYLOCITY_TENANT = "0b7f2c1e-5a4d-4e8b-9c3f-7d6e5a4b3c2d"
_PAYLOCITY_PATHS = ("Recruiting/Jobs", "recruiting/jobs", "RECRUITING/JOBS")
_PAYLOCITY_TAILS = {
    "bare": "",
    "trailing-slash": "/",
    "slug": "/Examplehealth-Inc/Senior-Product-Manager",
    "query": "?source=indeed",
    "fragment": "#apply",
    "slug-query-fragment": "/Examplehealth-Inc/Senior-Product-Manager?source=x#top",
}


@pytest.mark.parametrize(
    "url",
    [
        f"https://recruiting.paylocity.com/{path}/{action}/7001234{tail}"
        for path, action, tail in itertools.product(
            _PAYLOCITY_PATHS, ("Details", "details", "Apply", "apply"), _PAYLOCITY_TAILS.values()
        )
    ],
)
def test_every_spelling_of_a_paylocity_posting_is_a_single_employer_detail(url: str) -> None:
    assert host_match(paylocity.PAYLOCITY_JOB, url) is not None
    assert single_employer_detail_url(url) is not None
    assert job_board_address(url)


_PAYLOCITY_BOARDS = [
    f"https://recruiting.paylocity.com/{path}/{tail}"
    for path, tail in itertools.product(
        ("Recruiting/Jobs/All", "recruiting/jobs/all", "RECRUITING/JOBS/ALL"),
        (
            _PAYLOCITY_TENANT,
            f"{_PAYLOCITY_TENANT}/",
            f"{_PAYLOCITY_TENANT}/Examplehealth-Inc",
            f"{_PAYLOCITY_TENANT}?view=list",
            _PAYLOCITY_TENANT.upper(),
        ),
    )
]
_PAYLOCITY_FEEDS = [
    f"https://recruiting.paylocity.com/recruiting/v2/api/feed/jobs/{_PAYLOCITY_TENANT}",
    f"https://recruiting.paylocity.com/Recruiting/v2/api/feed/jobs/{_PAYLOCITY_TENANT}",
]


@pytest.mark.parametrize("url", [*_PAYLOCITY_BOARDS, *_PAYLOCITY_FEEDS])
def test_a_paylocity_board_or_feed_address_names_its_tenant_and_no_posting(url: str) -> None:
    found = board_adapters.identify_board_url(url)

    assert found is not None
    platform, match = found
    assert (platform, match.group("tenant").casefold()) == ("paylocity", _PAYLOCITY_TENANT)
    assert host_match(paylocity.PAYLOCITY_JOB, url) is None
    assert single_employer_detail_url(url) is None


def test_a_paylocity_board_linked_from_a_careers_page_names_its_tenant() -> None:
    markup = f'<a href="https://recruiting.paylocity.com/Recruiting/Jobs/All/{_PAYLOCITY_TENANT}">'

    found = board_adapters.identify_board(markup)

    assert found is not None
    assert (found[0], found[1].group("tenant")) == ("paylocity", _PAYLOCITY_TENANT)


@pytest.mark.parametrize(
    "wrapper", ['<a href="{url}">Partner role</a>', '<script>const role = "{url}";</script>']
)
def test_a_linked_partner_posting_does_not_name_a_whole_board(wrapper: str) -> None:
    url = "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/4127"
    assert board_adapters.identify_board(wrapper.format(url=url)) is None


def test_a_board_link_after_a_partner_posting_remains_recognized() -> None:
    root = "https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2"
    detail = (
        "https://globex.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/4127"
    )
    found = board_adapters.identify_board(
        f'<a href="{detail}">Partner role</a><a href="{root}">Our openings</a>'
    )
    assert found is not None
    assert found[0] == "oracle"
    assert "CX_2" in found[1].group(0)


def test_a_teamtailor_site_on_its_own_domain_is_found_past_the_identification_read() -> None:
    head = (
        '<link rel="alternate" type="application/rss+xml" href="https://careers.acme.test/jobs.rss">'
        '<script src="https://assets-aws.teamtailor-cdn.com/assets/careersite-e4fb0512.js"></script>'
    )
    filler = "<li>Opening</li>" * (board_adapters.DETECT_BYTES // 16 + 1)
    footer = '<a href="https://www.teamtailor.com/?utm_campaign=poweredby">Teamtailor</a>'
    page = head + filler + footer

    class LongCareerSite(board_adapters.BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = board_adapters.DETECT_BYTES) -> str:
            return url + "\n" + page[:max_bytes]

    resolution = LongCareerSite().resolve("https://careers.acme.test/jobs")
    assert resolution.platform == "teamtailor"


def test_partner_posting_links_do_not_replace_publisher_declared_openings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = (
        '<a href="https://globex.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/'
        'en/sites/CX_1/job/4127">Partner role</a>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Data Platform Lead",'
        '"hiringOrganization":{"name":"Acme"},"url":"https://careers.acme.test/jobs/lead"}</script>'
    )

    class DeclaredPage(board_adapters.BoardApiFetcher):
        def get_text(self, url: str, *, max_bytes: int = board_adapters.DETECT_BYTES) -> str:
            return url + "\n" + page

    fetcher = DeclaredPage()
    monkeypatch.setattr(fetcher, "_dispatch", lambda *args: pytest.fail("Unexpected board read"))
    result = fetcher.listings("Acme", "https://careers.acme.test/careers")
    assert [(row.title, row.employer, row.url) for row in result.listings] == [
        ("Data Platform Lead", "Acme", "https://careers.acme.test/jobs/lead")
    ]
    assert result.complete is False
