"""Each board platform pages, bounds, and recognises its own read without a network."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest

from clearscout.application.discovery.source_reading import (
    MAX_SOURCE_READ_ATTEMPTS,
    PostingCheck,
    SourceContinuationTarget,
    SourceDetailCheckpoint,
    SourceDetailCoverage,
    SourceRead,
    SourceReadOptions,
)
from clearscout.domain import SourceReadOutcome
from clearscout.domain.artifacts import PostingSource
from clearscout.infrastructure.web_fetch import board_reads
from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher, identify_board
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.platforms import icims, oracle, phenom, teamtailor, workday
from clearscout.infrastructure.web_fetch.policy import HostHeldError
from tests.loop.test_board_mappings_declared_search import _SearchSite
from tests.loop.test_fxrecruiter_reader import _FeedSite
from tests.loop.test_taleo_reader import _TaleoSite
from tests.support.doubles import ScriptedPage, published_listings
from tests.support.scenarios import source_listing


@pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
def test_workday_listings_are_read_and_paged_without_a_network(locale: str) -> None:
    """Parse the CXS payload shape, and stop when the board says it is done."""
    requests: list[tuple[str, int]] = []

    class ScriptedWorkday(BoardApiFetcher):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["offset"])
            requests.append((url, offset))
            if offset == 0:
                return {
                    "total": 21,
                    "jobPostings": [
                        {
                            "title": f"Role {index}",
                            "externalPath": f"/job/Remote/Role-{index}_R-{index}",
                            "locationsText": "Remote, Canada",
                            "bulletFields": [f"R-{index}"],
                        }
                        for index in range(20)
                    ],
                }
            if offset == 21:
                return {"total": 0, "jobPostings": []}
            return {
                "total": 0,
                "jobPostings": [
                    {
                        "title": "Role 20",
                        "externalPath": "/job/Remote/Role-20",
                        "locationsText": "Remote, Canada",
                        "bulletFields": [],
                    }
                ],
            }

    root = f"https://example.wd1.myworkdayjobs.com/{locale}Careers"
    read = ScriptedWorkday().listings("Example", root)
    assert read is not None
    assert read.complete is True
    listings = read.listings
    assert len(listings) == 21

    first = listings[0]
    assert first.employer == "Example"
    assert first.title == "Role 0"
    assert first.url == f"{root}/job/Remote/Role-0_R-0"
    assert first.location == "Remote, Canada"
    assert first.requisition_id == "R-0"
    # Do not invent a requisition number when the posting path omits it.
    assert listings[20].requisition_id == ""

    assert [offset for _, offset in requests] == [0, 20, 40]
    assert read.listing_pages == 3
    assert requests[0][0] == ("https://example.wd1.myworkdayjobs.com/wday/cxs/example/Careers/jobs")


def test_a_workday_read_ending_on_an_empty_page_records_one_listing_page() -> None:
    offsets: list[int] = []

    class ScriptedWorkday(BoardApiFetcher):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["offset"])
            offsets.append(offset)
            if offset:
                return {"total": 0, "jobPostings": []}
            return {
                "total": 20,
                "jobPostings": [
                    {
                        "title": f"Role {index}",
                        "externalPath": f"/job/Remote/Role-{index}_R-{index}",
                        "locationsText": "Remote, Canada",
                        "bulletFields": [f"R-{index}"],
                    }
                    for index in range(20)
                ],
            }

    read = ScriptedWorkday().listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert read is not None
    assert offsets == [0, 20]
    assert read.complete is True
    assert len(read.listings) == 20
    assert read.listing_pages == 1


@pytest.mark.parametrize(
    ("root", "endpoint", "shared_host"),
    [
        (
            "https://acme.wd1.myworkdayjobs.com/en-US/Acme_External",
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Acme_External/jobs",
            False,
        ),
        (
            "https://wd1.myworkdaysite.com/en-US/recruiting/acme/Acme_External",
            "https://wd1.myworkdaysite.com/wday/cxs/acme/Acme_External/jobs",
            True,
        ),
    ],
)
def test_an_employer_page_reads_its_declared_workday_scope(
    root: str, endpoint: str, shared_host: bool
) -> None:
    source = "https://corporate.example/careers"
    requests: list[tuple[str, int]] = []

    class DeclaredWorkday(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["offset"])
            requests.append((url, offset))
            return {
                "total": 2,
                "jobPostings": [
                    {
                        "title": f"Data Manager {index}",
                        "externalPath": f"/job/Remote/Data-Manager_R-{index}",
                        "locationsText": "Remote, Canada",
                        "bulletFields": [f"R-{index}"],
                    }
                    for index in range(2)
                ],
            }

    fetcher = DeclaredWorkday(
        f'<a href="{root}">Openings</a>'
        '<script>window.workday = {tenant: "acme", siteId: "Acme_External"};</script>'
    )

    read = fetcher.listings("Example", source)

    assert read.complete is True
    assert read.raw_count == 2
    assert read.outcome is SourceReadOutcome.ATS_COMPLETE
    assert requests == [(endpoint, 0), (endpoint, 20)]
    assert fetcher.fetched == ([source, root] if shared_host else [source])
    assert [(item.url, item.requisition_id) for item in read.listings] == [
        (f"{root}/job/Remote/Data-Manager_R-{index}", f"R-{index}") for index in range(2)
    ]
    for index, item in enumerate(read.listings):
        assert item.employer == "Example"
        assert item.title == f"Data Manager {index}"
        assert item.location == "Remote, Canada"
        assert item.facts.employer.provenance == "source.employer_hint"
        assert item.facts.locations.provenance == "workday.location"


_PARENT_ROOT = "https://globex.wd3.myworkdayjobs.com/en-US/globex_careers"
_PARENT_JOBS = "https://globex.wd3.myworkdayjobs.com/wday/cxs/globex/globex_careers/jobs"


class _BannerParent(ScriptedPage):
    """A parent Workday board whose CXS search narrows to one banner's rows."""

    def __init__(self, page: str) -> None:
        super().__init__(page)
        self.bodies: list[dict[str, Any]] = []
        self.rows = {"A": 25, "B": 3}

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        assert url == _PARENT_JOBS
        self.bodies.append(body)
        banners = body["appliedFacets"].get("Banner") or sorted(self.rows)
        postings = [
            {
                "title": f"Banner {banner} Role {index}",
                "externalPath": f"/job/Remote/Banner-{banner}-Role_R-{banner}{index}",
                "locationsText": "Remote, Canada",
                "bulletFields": [f"R-{banner}{index}"],
            }
            for banner in banners
            for index in range(self.rows[banner])
        ]
        offset, limit = int(body["offset"]), int(body["limit"])
        return {"total": len(postings), "jobPostings": postings[offset : offset + limit]}


_BANNER_PAGE = (
    f'<a href="{_PARENT_ROOT}?source=company_career_site&amp;utm_source=globex.example'
    '&amp;utm_medium=referral&amp;Banner=A">Current openings</a>'
)


def test_an_employer_page_linking_a_banner_scoped_parent_board_reads_only_that_banner() -> None:
    fetcher = _BannerParent(_BANNER_PAGE)

    read = fetcher.listings("Northwind", "https://child.example/careers")

    assert read.complete is True
    assert [item.title for item in read.listings] == [f"Banner A Role {i}" for i in range(25)]
    assert [body["appliedFacets"] for body in fetcher.bodies] == [{"Banner": ["A"]}] * 3


def test_a_banner_scoped_workday_cursor_keeps_its_banner_when_continued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    fetcher = _BannerParent(_BANNER_PAGE)
    first = fetcher.listings("Northwind", "https://child.example/careers")

    continued = fetcher.continue_listings(
        "Northwind",
        first.deferred_targets,
        root_url="https://child.example/careers",
        handoff_scopes=(),
    )

    titles = [item.title for item in (*first.listings, *continued.listings)]
    assert titles == [f"Banner A Role {i}" for i in range(25)]
    assert [body["appliedFacets"] for body in fetcher.bodies] == [{"Banner": ["A"]}] * 3
    assert continued.deferred_targets == ()


@pytest.mark.parametrize(
    "page",
    [
        f'<div data-jobs="{{&quot;board&quot;:&quot;{_PARENT_ROOT}?Banner=A&quot;}}"></div>',
        f"<p>See {_PARENT_ROOT}?Banner=A).</p>",
        f'<script>var board = "{_PARENT_ROOT}?Banner=A\\u0026x=1";</script>',
    ],
)
def test_a_banner_link_inside_escaped_markup_or_prose_keeps_only_its_own_value(page: str) -> None:
    fetcher = _BannerParent(page)

    read = fetcher.listings("Northwind", "https://child.example/careers")

    assert len(read.listings) == 25
    assert [body["appliedFacets"] for body in fetcher.bodies] == [{"Banner": ["A"]}] * 3


def test_repeated_banner_values_survive_the_workday_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    fetcher = _BannerParent(f'<a href="{_PARENT_ROOT}?Banner=A&amp;Banner=B">Jobs</a>')
    first = fetcher.listings("Northwind", "https://child.example/careers")

    continued = fetcher.continue_listings(
        "Northwind",
        first.deferred_targets,
        root_url="https://child.example/careers",
        handoff_scopes=(),
    )

    assert len(first.listings) + len(continued.listings) == 28
    assert [body["appliedFacets"] for body in fetcher.bodies] == [{"Banner": ["A", "B"]}] * 3


def test_a_source_saved_at_a_faceted_workday_address_reads_only_that_banner() -> None:
    fetcher = _BannerParent("")

    read = fetcher.listings("Northwind", f"{_PARENT_ROOT}?UTM_Source=x&Banner=B")

    assert [item.title for item in read.listings] == [f"Banner B Role {i}" for i in range(3)]
    assert fetcher.bodies[0]["appliedFacets"] == {"Banner": ["B"]}


def test_a_search_term_in_a_linked_workday_board_becomes_the_search_text() -> None:
    fetcher = _BannerParent(f'<a href="{_PARENT_ROOT}?q=pharmacist&amp;Banner=B">Jobs</a>')

    fetcher.listings("Northwind", "https://child.example/careers")

    assert fetcher.bodies[0] == {
        "appliedFacets": {"Banner": ["B"]},
        "limit": 20,
        "offset": 0,
        "searchText": "pharmacist",
    }


def test_an_unscoped_workday_root_posts_an_empty_search_and_a_bare_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    fetcher = _BannerParent("")

    read = fetcher.listings("Globex", _PARENT_ROOT)

    assert fetcher.bodies == [{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}]
    assert [target.url for target in read.deferred_targets] == [
        f"{_PARENT_JOBS}?limit=20&offset=20"
    ]


@pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
def test_workday_shared_host_reads_the_declared_tenant_and_preserves_its_site(locale: str) -> None:
    root = f"https://wd1.myworkdaysite.com/{locale}recruiting/example/External_Careers"
    endpoint = "https://wd1.myworkdaysite.com/wday/cxs/example/External_Careers/jobs"

    class SharedWorkday(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            assert url == endpoint
            offset = int(body["offset"])
            return {
                "total": 1,
                "jobPostings": []
                if offset
                else [
                    {
                        "title": "Data Manager",
                        "externalPath": "/job/Remote/Data-Manager_R-1",
                        "bulletFields": ["R-1"],
                    }
                ],
            }

    fetcher = SharedWorkday(
        '<script>window.workday = {tenant: "example", siteId: "External_Careers"};</script>'
    )

    read = fetcher.listings("Example", root)

    assert read.complete is True
    assert [(item.url, item.requisition_id) for item in read.listings] == [
        (f"{root}/job/Remote/Data-Manager_R-1", "R-1")
    ]
    assert fetcher.fetched == [root]


@pytest.mark.parametrize(
    ("tenant", "site"), [("other", "External_Careers"), ("example", "Other_Careers")]
)
def test_a_workday_shared_host_refuses_a_root_declaring_another_scope(
    tenant: str, site: str
) -> None:
    fetcher = ScriptedPage(
        f'<script>window.workday = {{tenant: "{tenant}", siteId: "{site}"}};</script>'
    )

    with pytest.raises(ValueError, match="Workday"):
        fetcher.listings(
            "Example", "https://wd1.myworkdaysite.com/en-US/recruiting/example/External_Careers"
        )


@pytest.mark.parametrize(
    "root",
    [
        "https://wd1.myworkdaysite.com/en-US/External_Careers",
        "https://wd1.myworkdaysite.com/recruiting/example",
        "https://wd1.myworkdaysite.com/recruiting//External_Careers",
        "https://wd1.myworkdaysite.com/recruiting/example/External_Careers%2Fother",
        "https://evilmyworkdaysite.com/recruiting/example/External_Careers",
    ],
)
def test_malformed_workday_shared_host_paths_name_no_board(root: str) -> None:
    assert identify_board(root) is None


def test_workday_short_pages_and_changed_totals_keep_incomplete_coverage() -> None:
    class MovingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            payload["total"] = 42 if not body["offset"] else 43
            if not body["offset"]:
                payload["jobPostings"] = payload["jobPostings"][:2]
            return payload

    fetcher = MovingWorkday(42, stated_total=42)
    read = fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert not read.complete
    assert read.discovery_truncated
    assert fetcher.offsets == [0, 20, 40, 60]
    assert len(read.listings) == 24
    assert read.deferred_targets == ()
    assert read.skip_reasons == {"incomplete_workday_listing": 1}


@pytest.mark.parametrize("continued", [False, True])
def test_a_repeated_workday_page_keeps_unique_rows_and_an_unfinished_cursor(
    continued: bool,
) -> None:
    class RepeatingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            payload["jobPostings"] = [
                {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                for index in range(20)
            ]
            return payload

    fetcher = RepeatingWorkday(100, stated_total=100)

    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert read.complete is False
    assert len(read.listings) == 20
    assert fetcher.offsets == [0, 20]
    assert read.deferred_targets == ()
    assert read.discovery_truncated
    assert read.skip_reasons == {"repeated_workday_page": 1}
    assert read.listing_coverage is None


def test_a_repeated_workday_page_after_resumption_cannot_claim_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)

    class RepeatingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if int(body["offset"]) == 20:
                payload["jobPostings"] = [
                    {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                    for index in reversed(range(20))
                ]
            return payload

    fetcher = RepeatingWorkday(40, stated_total=40)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Example", root)

    resumed = fetcher.continue_listings(
        "Example", first.deferred_targets, root_url=root, handoff_scopes=()
    )

    assert resumed.listings == []
    assert resumed.skip_reasons == {"repeated_workday_page": 1}
    assert resumed.deferred_targets == ()
    assert resumed.discovery_truncated
    assert not resumed.complete
    assert fetcher.offsets == [0, 20]


@pytest.mark.parametrize("locale", ["", "en-US/", "ja-JP/"])
@pytest.mark.parametrize("resumed_total", [44, 45])
def test_workday_overlap_and_changed_totals_survive_continuation_checkpoints(
    monkeypatch: pytest.MonkeyPatch, resumed_total: int, locale: str
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 1)

    class MovingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["offset"])
            self.offsets.append(offset)
            ids = {0: range(20), 20: range(19, 39), 40: range(39, 44), 60: range(0)}[offset]
            return {
                "total": 44 if offset == 0 else resumed_total,
                "jobPostings": [
                    {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                    for index in ids
                ],
            }

    fetcher = MovingWorkday(44, stated_total=44)
    root = f"https://example.wd1.myworkdayjobs.com/{locale}Careers"
    first = fetcher.listings("Example", root)
    second = fetcher.continue_listings(
        "Example", first.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert [listing.title for listing in second.listings] == [
        f"Role {index}" for index in range(20, 39)
    ]
    assert second.deferred_targets[0].source_url == root
    assert second.discovery_truncated
    checkpoint = second.deferred_targets[0].detail_checkpoint
    assert checkpoint is not None
    assert len(checkpoint.listing_ids) == 39
    assert checkpoint.listing_unstable
    assert checkpoint.listing_total == resumed_total
    assert "__reader_pages" not in second.deferred_targets[0].url
    third = fetcher.continue_listings(
        "Example", second.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert [listing.url for listing in third.listings] == [
        f"{root}/job/Remote/Role_R-{index}" for index in range(39, 44)
    ]
    target = third.deferred_targets[0]
    assert target.source_url == root
    assert "offset=60" in target.url
    assert target.automatic_eligible
    next_checkpoint = target.detail_checkpoint
    assert next_checkpoint is not None
    assert len(next_checkpoint.listing_ids) == 44
    assert next_checkpoint.listing_unstable
    assert next_checkpoint.listing_total == resumed_total
    assert third.discovery_truncated
    assert not third.complete
    assert third.skip_reasons == {}
    assert third.listing_coverage is None
    fourth = fetcher.continue_listings(
        "Example", third.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert fourth.listings == []
    assert fourth.deferred_targets == ()
    assert fourth.skip_reasons == {"incomplete_workday_listing": 1}
    assert fourth.listing_coverage is None
    # A verifying pass over 44 rows needs more pages than the one-page budget allows.
    assert fetcher.offsets == [0, 20, 40, 60]


@pytest.mark.parametrize("totals", [(44, 44, 44, 44), (44, 45, 45, 45)])
def test_workday_overlap_cannot_certify_a_complete_initial_read(totals: tuple[int, ...]) -> None:
    class MovingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            offset = int(body["offset"])
            self.offsets.append(offset)
            ids = {0: range(20), 20: range(19, 39), 40: range(39, 44), 60: range(0)}[offset]
            return {
                "total": totals[self.offsets.index(offset)],
                "jobPostings": [
                    {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                    for index in ids
                ],
            }

    fetcher = MovingWorkday(44, stated_total=44)
    read = fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    assert len(read.listings) == 44
    assert not read.complete
    assert read.discovery_truncated
    assert read.skip_reasons == {"incomplete_workday_listing": 1, "duplicate_workday_identity": 1}
    assert read.deferred_targets == ()
    # Equal counts start one verifying pass, which meets the overlap at offset 20 again.
    verifying = [0, 20] if totals[-1] == 44 else []
    assert fetcher.offsets == [0, 20, 40, 60, *verifying]
    assert read.listing_coverage is None


def test_workday_changed_positive_total_without_overlap_is_retained_across_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 1)

    class ChangingWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            payload["total"] = 41 if not body["offset"] else 40
            return payload

    fetcher = ChangingWorkday(40, stated_total=41)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Example", root)
    second = fetcher.continue_listings(
        "Example", first.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert len(second.listings) == 20
    assert second.discovery_truncated
    checkpoint = second.deferred_targets[0].detail_checkpoint
    assert checkpoint is not None and checkpoint.listing_unstable
    assert checkpoint.listing_total == 40
    assert len(checkpoint.listing_ids) == 40
    assert second.listing_coverage is None


def test_workday_full_slice_keeps_identity_history_out_of_the_cursor_url() -> None:
    fetcher = _ListedWorkday(2001, stated_total=2001)
    read = fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    target = read.deferred_targets[0]
    assert len(target.url) < 4096
    assert "__reader_pages" not in target.url
    assert target.detail_checkpoint is not None
    assert len(target.detail_checkpoint.listing_ids) == 2000
    assert len(target.detail_checkpoint.page_fingerprints) == 100
    assert target.detail_checkpoint.platform == "search:workday"


def test_a_workday_shared_host_resume_keeps_its_root_and_retries_the_failed_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    root = "https://wd1.myworkdaysite.com/ja-JP/recruiting/example/External_Careers"
    endpoint = "https://wd1.myworkdaysite.com/wday/cxs/example/External_Careers/jobs"

    class ResumingWorkday(_ListedWorkday):
        def __init__(self) -> None:
            super().__init__(42, stated_total=42)
            self.fail_next = True

        def get_text(self, url: str, *, max_bytes: int = 0) -> str:
            assert url == root
            return (
                '<script>window.workday = {tenant: "example", siteId: "External_Careers"};</script>'
            )

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            assert url == endpoint
            if int(body["offset"]) == 40 and self.fail_next:
                self.fail_next = False
                raise _refusal(url, 503)
            payload = super().post_json(url, body)
            if body["offset"]:
                payload["total"] = 0
            return payload

    fetcher = ResumingWorkday()
    first = fetcher.listings("Example", root)
    assert first.deferred_targets[0].source_url == root
    resumed = fetcher.continue_listings(
        "Example", first.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert len(resumed.listings) == 20
    assert "offset=40" in resumed.deferred_targets[0].url
    final = fetcher.continue_listings(
        "Example", resumed.deferred_targets, root_url=root, handoff_scopes=()
    )

    assert [item.url for item in final.listings] == [
        f"{root}/job/Remote/Role_R-{index}" for index in (40, 41)
    ]
    assert final.deferred_targets == ()
    assert len({item.url for item in first.listings + resumed.listings + final.listings}) == 42


@pytest.mark.parametrize(
    ("root", "scope"),
    [
        (
            "https://wd1.myworkdaysite.com/recruiting/example/External_Careers",
            "other/External_Careers",
        ),
        (
            "https://wd1.myworkdaysite.com/recruiting/example/External_Careers",
            "example/Other_Careers",
        ),
        ("https://acme.wd1.myworkdayjobs.com/en-US/Careers", "example/Careers"),
        ("https://example.wd1.myworkdayjobs.com/en-US/Other_Careers", "example/Careers"),
    ],
)
def test_a_workday_cursor_cannot_leave_its_source_scope(root: str, scope: str) -> None:
    host = (
        "wd1.myworkdaysite.com" if "myworkdaysite.com" in root else "example.wd1.myworkdayjobs.com"
    )
    target = SourceContinuationTarget(
        url=f"https://{host}/wday/cxs/{scope}/jobs?limit=20&offset=20",
        label="Workday page at offset 20",
        label_source="none",
        structural_context="other",
        relations=("same-origin", "publisher-content"),
        source_url=root,
        deferred_reason="workday_page_pending",
    )
    fetcher = _ListedWorkday(40, stated_total=40)

    read = fetcher.continue_listings("Example", (target,), root_url=root, handoff_scopes=())

    assert read.listings == []
    assert fetcher.offsets == []
    assert read.skip_reasons == {"invalid_workday_cursor": 1}


_RENAMED_HOST = "https://acme-co.wd1.myworkdayjobs.com"
_HOST_LABEL_JOBS = f"{_RENAMED_HOST}/wday/cxs/acme-co/Careers/jobs"
_STATED_JOBS = f"{_RENAMED_HOST}/wday/cxs/acme_co/Careers/jobs"


def _refusal(url: str, status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    return httpx.HTTPStatusError(
        "refused", request=request, response=httpx.Response(status, request=request)
    )


class _RenamedTenant(ScriptedPage):
    """A Workday site whose CXS answers only under the tenant its career page states."""

    def __init__(self, *, total: int, failing_offset: int | None = None) -> None:
        super().__init__(
            f"{_RENAMED_HOST}/Careers\n<script>window.workday = {{\n"
            '  tenant: "acme_co",\n  siteId: "Careers"\n};</script>'
        )
        self.total = total
        self.failing_offset = failing_offset
        self.posted: list[str] = []

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.posted.append(url)
        offset = int(body["offset"])
        if url != _STATED_JOBS:
            raise _refusal(url, 422)
        if offset == self.failing_offset:
            raise _refusal(url, 500)
        count = min(int(body["limit"]), self.total - offset)
        return {
            "total": self.total,
            "jobPostings": [
                {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                for index in range(offset, offset + count)
            ],
        }


def _workday_cursor(endpoint: str, offset: int) -> SourceContinuationTarget:
    parsed = urlsplit(endpoint)
    site = parsed.path.split("/")[-2]
    return SourceContinuationTarget(
        url=f"{endpoint}?limit=20&offset={offset}",
        label=f"Workday page at offset {offset}",
        label_source="none",
        source_url=f"https://{parsed.netloc}/{site}",
        structural_context="other",
        relations=("same-origin", "publisher-content"),
        deferred_reason="workday_page_pending",
    )


def test_workday_reads_the_tenant_its_career_page_states_when_the_host_label_is_refused() -> None:
    fetcher = _RenamedTenant(total=1)

    read = fetcher.listings("Acme", f"{_RENAMED_HOST}/Careers")

    assert fetcher.posted == [_HOST_LABEL_JOBS, _STATED_JOBS, _STATED_JOBS]
    assert fetcher.fetched == [f"{_RENAMED_HOST}/Careers"]
    assert read.complete is True
    assert [(item.title, item.url) for item in read.listings] == [
        ("Role 0", f"{_RENAMED_HOST}/Careers/job/Remote/Role_R-0")
    ]


def test_a_failed_later_workday_page_retains_rows_without_rereading_its_career_root() -> None:
    fetcher = _RenamedTenant(total=40, failing_offset=20)

    read = fetcher.listings("Acme", f"{_RENAMED_HOST}/Careers")

    assert [item.title for item in read.listings] == [f"Role {index}" for index in range(20)]
    assert read.complete is False
    assert "offset=20" in read.deferred_targets[0].url
    assert fetcher.fetched == [f"{_RENAMED_HOST}/Careers"]


def test_a_workday_continuation_on_the_host_label_recovers_under_the_stated_tenant() -> None:
    fetcher = _RenamedTenant(total=40)

    continued = fetcher.continue_listings(
        "Acme",
        (_workday_cursor(_HOST_LABEL_JOBS, 0),),
        root_url=f"{_RENAMED_HOST}/Careers",
        handoff_scopes=(),
    )

    assert [listing.title for listing in continued.listings] == [f"Role {i}" for i in range(40)]
    assert fetcher.posted == [_HOST_LABEL_JOBS, _STATED_JOBS, _STATED_JOBS, _STATED_JOBS]
    assert continued.deferred_targets == ()


def test_a_continued_workday_page_keeps_the_stated_tenant_at_the_next_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 2)
    fetcher = _RenamedTenant(total=100)

    continued = fetcher.continue_listings(
        "Acme",
        (_workday_cursor(_STATED_JOBS, 20),),
        root_url=f"{_RENAMED_HOST}/Careers",
        handoff_scopes=(),
    )

    assert [listing.title for listing in continued.listings] == [f"Role {i}" for i in range(20, 60)]
    assert fetcher.posted == [_STATED_JOBS, _STATED_JOBS]
    assert _workday_page_urls(continued) == [f"{_STATED_JOBS}?limit=20&offset=60"]
    assert fetcher.fetched == []


_EXAMPLE_JOBS = "https://example.wd1.myworkdayjobs.com/wday/cxs/example/Careers/jobs"


class _ListedWorkday(BoardApiFetcher):
    """A Workday listing with publisher paging and a controlled operation clock."""

    def __init__(
        self,
        size: int,
        *,
        stated_total: int,
        now: list[float] | None = None,
        seconds_per_page: float = 0.0,
        reset_after_last_page: bool = False,
        late_zero: bool = False,
    ) -> None:
        super().__init__()
        self.size = size
        self.stated_total = stated_total
        self.now = now
        self.seconds_per_page = seconds_per_page
        self.reset_after_last_page = reset_after_last_page
        self.late_zero = late_zero
        self.offsets: list[int] = []

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        offset, limit = int(body["offset"]), int(body["limit"])
        self.offsets.append(offset)
        if self.now is not None:
            self.now[0] += self.seconds_per_page
        page_offset = 0 if self.reset_after_last_page and offset >= self.size else offset
        count = max(0, min(limit, self.size - page_offset))
        return {
            "total": 0 if self.late_zero and offset else self.stated_total,
            "jobPostings": [
                {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                for index in range(page_offset, page_offset + count)
            ],
        }

    def continue_from(self, *offsets: int) -> SourceRead:
        """Continue one batch of retained listing cursors."""
        return self.continue_listings(
            "Example",
            tuple(_workday_cursor(_EXAMPLE_JOBS, offset) for offset in offsets),
            root_url="https://example.wd1.myworkdayjobs.com/Careers",
            handoff_scopes=(),
        )


@pytest.mark.parametrize("size", [21, 40, 333])
@pytest.mark.parametrize("late_zero", [False, True])
@pytest.mark.parametrize("continued", [False, True])
def test_workday_stable_identity_window_finishes_before_the_publisher_reset(
    size: int, late_zero: bool, continued: bool
) -> None:
    fetcher = _ListedWorkday(
        size, stated_total=size, reset_after_last_page=True, late_zero=late_zero
    )
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert len(read.listings) == size
    assert len({listing.url for listing in read.listings}) == size
    assert not read.discovery_truncated
    assert read.deferred_targets == ()
    assert read.complete is not continued
    assert fetcher.offsets == list(range(0, size + 20, 20))


def test_workday_identity_window_survives_several_listing_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 1)
    fetcher = _ListedWorkday(61, stated_total=61, reset_after_last_page=True, late_zero=True)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.listings("Example", root)
    listings = list(read.listings)
    for count in (20, 40, 60, 61):
        assert len(listings) == count
        checkpoint = read.deferred_targets[0].detail_checkpoint
        assert checkpoint is not None
        assert len(checkpoint.listing_ids) == count
        assert len(checkpoint.page_fingerprints) == -(-count // 20)
        assert checkpoint.listing_total == 61
        assert not checkpoint.listing_unstable
        read = fetcher.continue_listings(
            "Example", read.deferred_targets, root_url=root, handoff_scopes=()
        )
        listings.extend(read.listings)

    assert len({listing.url for listing in listings}) == 61
    assert not read.discovery_truncated
    assert read.deferred_targets == ()
    assert read.listing_coverage is not None
    assert fetcher.offsets == [0, 20, 40, 60, 80]


@pytest.mark.parametrize("corporate_provenance", [False, True])
@pytest.mark.parametrize(
    "root",
    [
        "https://example.wd1.myworkdayjobs.com/en-US/Careers",
        "https://wd1.myworkdaysite.com/en-US/recruiting/example/Careers",
    ],
)
def test_workday_final_listing_slice_certifies_exact_membership_without_readable_bodies(
    monkeypatch: pytest.MonkeyPatch, root: str, corporate_provenance: bool
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 1)

    class ScopedWorkday(_ListedWorkday):
        def get_text(self, url: str, *, max_bytes: int = board_reads.MAX_API_RESPONSE_BYTES) -> str:
            assert url == root
            return '<script>window.workday = {tenant: "example", siteId: "Careers"};</script>'

    fetcher = ScopedWorkday(41, stated_total=41, late_zero=True, reset_after_last_page=True)
    read = fetcher.listings("Example", root)
    for offset in (20, 40, 60):
        assert read.listing_coverage is None
        assert not read.complete
        targets = tuple(
            replace(target, source_url="https://corporate.example/careers")
            if corporate_provenance
            else target
            for target in read.deferred_targets
        )
        read = fetcher.continue_listings("Example", targets, root_url=root, handoff_scopes=())
        assert fetcher.offsets[-1] == offset

    assert read.listings == []
    assert read.listing_coverage == SourceDetailCoverage(
        platform="workday",
        board_url=root,
        posting_urls=tuple(sorted(f"{root}/job/Remote/Role_R-{index}" for index in range(41))),
    )
    assert not read.complete
    assert read.detail_coverage is None
    assert read.deferred_targets == ()
    assert fetcher.offsets == [0, 20, 40, 60]


def test_workday_listing_certificate_survives_a_batch_with_an_owed_detail() -> None:
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    url = f"{root}/job/Remote/Role_R-1"

    class OwedWorkday(_ListedWorkday):
        def workday_detail(self, url: str) -> PostingCheck:
            return PostingCheck(
                status="unreadable", platform="workday", http_status=404, note="HTTP 404"
            )

    owed = _workday_detail_target(root, certified=False)
    owed = replace(
        owed,
        url=url,
        detail_checkpoint=SourceDetailCheckpoint(
            platform="workday", board_url=root, pending_ids=(url,), listing_ids=(url,)
        ),
    )
    fetcher = OwedWorkday(40, stated_total=40, reset_after_last_page=True)
    read = fetcher.continue_listings(
        "Example", (_workday_cursor(_EXAMPLE_JOBS, 0), owed), root_url=root, handoff_scopes=()
    )

    assert read.listing_coverage == SourceDetailCoverage(
        platform="workday",
        board_url=root,
        posting_urls=tuple(sorted(f"{root}/job/Remote/Role_R-{index}" for index in range(40))),
    )
    assert not read.complete
    assert read.detail_coverage is None
    assert read.detail.failed == 1
    assert len(read.deferred_targets) == 1
    assert read.deferred_targets[0].url == url
    assert read.deferred_targets[0].detail_checkpoint is not None
    assert read.deferred_targets[0].detail_checkpoint.closed_ids == ()


@pytest.mark.parametrize(
    "contradiction", ["different_scope", "different_members", "foreign_row", "unstable_read"]
)
def test_workday_batch_does_not_merge_conflicting_listing_certificates(
    monkeypatch: pytest.MonkeyPatch,
    contradiction: str,
) -> None:
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    url = f"{root}/job/Remote/Role_R-1"
    coverage = SourceDetailCoverage(platform="workday", board_url=root, posting_urls=(url,))
    first = SourceRead(
        listings=[],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        listing_coverage=coverage,
    )
    second = SourceRead(
        listings=[], complete=False, raw_count=0, outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES
    )
    if contradiction == "different_scope":
        second = replace(
            second,
            listing_coverage=replace(
                coverage, board_url="https://globex.wd1.myworkdayjobs.com/Careers"
            ),
        )
    elif contradiction == "different_members":
        second = replace(second, listing_coverage=replace(coverage, posting_urls=()))
    elif contradiction == "foreign_row":
        second = replace(
            second,
            raw_count=1,
            listings=[
                source_listing(
                    "Role", url="https://globex.wd1.myworkdayjobs.com/Careers/job/Remote/Role_R-1"
                )
            ],
        )
    else:
        second = replace(second, discovery_truncated=True)

    targets = (_workday_cursor(_EXAMPLE_JOBS, 0), _workday_cursor(_EXAMPLE_JOBS, 20))
    scripted = {targets[0].url: first, targets[1].url: second}
    monkeypatch.setattr(
        BoardApiFetcher,
        "_continuation_readers",
        lambda self: {"workday_page_pending": lambda employer, target: scripted[target.url]},
    )
    read = _ListedWorkday(0, stated_total=0).continue_listings(
        "Example", targets, root_url=root, handoff_scopes=()
    )

    assert read.listing_coverage is None
    assert not read.complete
    assert read.detail_coverage is None


@pytest.mark.parametrize("enumerated", [False, True])
def test_workday_listing_certificate_requires_protected_ids_in_its_membership(
    monkeypatch: pytest.MonkeyPatch,
    enumerated: bool,
) -> None:
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    url = f"{root}/job/Remote/Role_R-1"
    coverage = SourceDetailCoverage(platform="workday", board_url=root, posting_urls=(url,))
    read = SourceRead(
        listings=[],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        listing_coverage=coverage,
        protected_posting_urls=(url if enumerated else f"{root}/job/Remote/Role_R-2",),
    )

    monkeypatch.setattr(
        BoardApiFetcher,
        "_continuation_readers",
        lambda self: {"workday_page_pending": lambda employer, target: read},
    )
    merged = _ListedWorkday(0, stated_total=0).continue_listings(
        "Example", (_workday_cursor(_EXAMPLE_JOBS, 0),), root_url=root, handoff_scopes=()
    )

    assert merged.listing_coverage == (coverage if enumerated else None)
    assert not merged.complete
    assert merged.detail_coverage is None


def test_workday_continuation_cannot_certify_foreign_retained_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    fetcher = _ListedWorkday(21, stated_total=21)
    partial = fetcher.listings("Example", root)
    cursor = partial.deferred_targets[0]
    assert cursor.detail_checkpoint is not None
    checkpoint = cursor.detail_checkpoint
    foreign = "https://globex.wd1.myworkdayjobs.com/Careers/job/Remote/Role_R-0"
    checkpoint = replace(checkpoint, listing_ids=(foreign, *checkpoint.listing_ids[1:]))

    read = fetcher.continue_listings(
        "Example",
        (replace(cursor, detail_checkpoint=checkpoint),),
        root_url=root,
        handoff_scopes=(),
    )

    assert read.listing_coverage is None
    assert not read.complete
    assert read.detail_coverage is None
    assert read.discovery_truncated
    assert read.deferred_targets == ()
    assert fetcher.offsets == [0, 20, 40]


def test_workday_empty_window_without_a_positive_total_has_no_listing_certificate() -> None:
    read = _ListedWorkday(0, stated_total=0).continue_from(0)

    assert read.listing_coverage is None
    assert not read.complete
    assert read.detail_coverage is None


@pytest.mark.parametrize("continued", [False, True])
def test_workday_short_intermediate_page_keeps_the_next_aligned_page_and_missing_ids(
    continued: bool,
) -> None:
    class ShortWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if not body["offset"]:
                payload["jobPostings"] = payload["jobPostings"][:2]
            return payload

    fetcher = ShortWorkday(42, stated_total=42, reset_after_last_page=True)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert [listing.title for listing in read.listings] == [
        f"Role {index}" for index in (0, 1, *range(20, 42), *range(2, 20))
    ]
    assert not read.complete
    assert read.discovery_truncated
    assert read.skip_reasons == {"repeated_workday_page": 1, "duplicate_workday_identity": 2}
    assert read.deferred_targets == ()
    # The verifying pass meets offset 0's short page again and stops at the repeat.
    assert fetcher.offsets == [0, 20, 40, 60, 80, 0, 20, 40, 60]
    assert read.listing_coverage is None


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize("invalid", ["missing_path", "duplicate_identity"])
def test_workday_invalid_row_identity_cannot_certify_the_advertised_total(
    continued: bool, invalid: str
) -> None:
    class InvalidIdentity(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if not body["offset"]:
                payload["jobPostings"][-1] = (
                    {"title": "Missing identity"}
                    if invalid == "missing_path"
                    else payload["jobPostings"][0]
                )
            return payload

    fetcher = InvalidIdentity(20, stated_total=20)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert len(read.listings) == 19
    assert not read.complete
    assert read.discovery_truncated
    assert read.skip_reasons == {
        "incomplete_workday_listing": 1,
        (
            "missing_required_posting_identity"
            if invalid == "missing_path"
            else "duplicate_workday_identity"
        ): 1,
    }
    assert read.deferred_targets == ()
    assert fetcher.offsets == [0, 20]
    assert read.listing_coverage is None


@pytest.mark.parametrize("continued", [False, True])
def test_workday_repeated_row_on_the_last_page_counts_apart_from_the_repeated_page(
    continued: bool,
) -> None:
    class RepeatedLastRow(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            for posting in payload["jobPostings"]:
                if posting["externalPath"].endswith("R-206"):
                    posting.update(title="Role 199", externalPath="/job/Remote/Role_R-199")
            return payload

    fetcher = RepeatedLastRow(207, stated_total=207, reset_after_last_page=True)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert not read.complete
    assert len(read.listings) == 206
    assert read.skip_reasons == {"repeated_workday_page": 1, "duplicate_workday_identity": 1}
    if not continued:
        assert read.raw_count == 207


@pytest.mark.parametrize("continued", [False, True])
def test_workday_empty_page_before_the_advertised_total_keeps_incomplete_coverage(
    continued: bool,
) -> None:
    fetcher = _ListedWorkday(20, stated_total=40)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert len(read.listings) == 20
    assert not read.complete
    assert read.discovery_truncated
    assert read.skip_reasons == {"incomplete_workday_listing": 1}
    assert read.deferred_targets == ()
    assert fetcher.offsets == [0, 20]
    assert read.listing_coverage is None


@pytest.mark.parametrize("unstable", [False, True])
def test_a_misaligned_workday_cursor_stays_held_without_a_request(
    monkeypatch: pytest.MonkeyPatch, unstable: bool
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 1)
    fetcher = _ListedWorkday(21, stated_total=21, reset_after_last_page=True)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Example", root)
    target = first.deferred_targets[0]
    checkpoint = target.detail_checkpoint
    assert checkpoint is not None
    target = replace(
        target,
        url=_page_url(21),
        detail_checkpoint=replace(
            checkpoint,
            listing_ids=(*checkpoint.listing_ids, f"{root}/job/Remote/Role_R-20"),
            listing_unstable=unstable,
        ),
    )

    resumed = fetcher.continue_listings("Example", (target,), root_url=root, handoff_scopes=())

    assert resumed.listings == []
    assert not resumed.complete
    assert resumed.discovery_truncated
    assert resumed.skip_reasons == {"incomplete_workday_listing": 1}
    assert len(resumed.deferred_targets) == 1
    retained = resumed.deferred_targets[0]
    assert retained.url == target.url
    assert retained.detail_checkpoint == target.detail_checkpoint
    assert not retained.automatic_eligible
    assert fetcher.offsets == [0]


class _ChurnedWorkday(_ListedWorkday):
    """A listing whose second page repeats rows already read, under a new page identity."""

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        payload = super().post_json(url, body)
        if int(body["offset"]) == 20:
            payload["jobPostings"] = [
                {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                for index in range(10)
            ]
        return payload


@pytest.mark.parametrize("continued", [False, True])
def test_a_churned_workday_page_keeps_paging_without_a_certificate(continued: bool) -> None:
    fetcher = _ChurnedWorkday(60, stated_total=60)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Acme", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert fetcher.offsets == [0, 20, 40, 60]
    assert [listing.title for listing in read.listings] == [
        f"Role {index}" for index in (*range(20), *range(40, 60))
    ]
    assert not read.complete
    assert read.discovery_truncated
    assert read.listing_coverage is None
    assert read.skip_reasons == {"incomplete_workday_listing": 1, "duplicate_workday_identity": 10}
    assert read.deferred_targets == ()


@pytest.mark.parametrize("continued", [False, True])
def test_seen_workday_rows_past_the_stated_total_end_the_listing(continued: bool) -> None:
    class SeenPastTotal(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            offset = int(body["offset"])
            if offset >= 40:
                payload["jobPostings"] = [
                    {"title": f"Role {index}", "externalPath": f"/job/Remote/Role_R-{index}"}
                    for index in range(offset % 40, offset % 40 + 10)
                ]
            return payload

    fetcher = SeenPastTotal(40, stated_total=40)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Acme", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    # The verifying pass meets the reread rows at offset 40 and stops there.
    assert fetcher.offsets == [0, 20, 40, 0, 20, 40]
    assert len(read.listings) == 40
    assert not read.complete
    assert read.discovery_truncated
    assert read.listing_coverage is None
    assert read.deferred_targets == ()
    assert read.skip_reasons == {"incomplete_workday_listing": 1, "duplicate_workday_identity": 10}


@pytest.mark.parametrize("continued", [False, True])
def test_a_capped_workday_total_reads_its_tail_to_an_empty_page_without_a_certificate(
    continued: bool,
) -> None:
    fetcher = _ListedWorkday(60, stated_total=40)
    read = (
        fetcher.continue_from(0)
        if continued
        else fetcher.listings("Acme", "https://example.wd1.myworkdayjobs.com/Careers")
    )

    assert fetcher.offsets == [0, 20, 40, 60]
    assert len({listing.url for listing in read.listings}) == 60
    assert not read.complete
    assert read.discovery_truncated
    assert read.listing_coverage is None
    assert read.deferred_targets == ()


def test_a_workday_cursor_past_a_capped_total_reads_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 2)
    fetcher = _ListedWorkday(60, stated_total=40)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Acme", root)
    target = first.deferred_targets[0]
    assert "offset=40" in target.url
    assert target.automatic_eligible

    resumed = fetcher.continue_listings("Acme", (target,), root_url=root, handoff_scopes=())

    assert [listing.title for listing in resumed.listings] == [
        f"Role {index}" for index in range(40, 60)
    ]
    assert fetcher.offsets == [0, 20, 40, 60]
    assert resumed.discovery_truncated
    assert resumed.listing_coverage is None
    assert not resumed.complete


@pytest.mark.parametrize("continued", [False, True])
def test_a_stable_exact_workday_total_certifies_only_after_an_empty_page(
    monkeypatch: pytest.MonkeyPatch, continued: bool
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 2)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 2)
    fetcher = _ListedWorkday(40, stated_total=40)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.continue_from(0) if continued else fetcher.listings("Acme", root)

    assert fetcher.offsets == [0, 20]
    assert not read.complete
    assert read.listing_coverage is None
    target = read.deferred_targets[0]
    assert "offset=40" in target.url
    assert target.automatic_eligible

    final = fetcher.continue_listings("Acme", (target,), root_url=root, handoff_scopes=())

    assert fetcher.offsets == [0, 20, 40]
    assert final.deferred_targets == ()
    assert final.listing_coverage == SourceDetailCoverage(
        platform="workday",
        board_url=root,
        posting_urls=tuple(sorted(f"{root}/job/Remote/Role_R-{index}" for index in range(40))),
    )


class _SettlingWorkday(_ListedWorkday):
    """A 40-row listing stating 41 on its first page, then 40.

    Requests after the third form the verifying pass, which may swap a posting or move the total.
    """

    def __init__(
        self,
        *,
        swapped: bool = False,
        moving: bool = False,
        repeated: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(40, stated_total=40, **kwargs)
        self.swapped = swapped
        self.moving = moving
        self.repeated = repeated

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        payload = super().post_json(url, body)
        request, offset = len(self.offsets), int(body["offset"])
        if request == 1:
            payload["total"] = 41
        if request > 3 and offset == 0 and self.swapped:
            payload["jobPostings"][0]["externalPath"] = "/job/Remote/Role_R-99"
        if request > 3 and offset == 20 and self.moving:
            payload["total"] = 41
        if request > 3 and offset == 20 and self.repeated:
            payload["jobPostings"] = super().post_json(url, {**body, "offset": 0})["jobPostings"]
            self.offsets.pop()
        return payload


@pytest.mark.parametrize("continued", [False, True])
def test_a_workday_total_that_settles_is_certified_by_one_verifying_pass(
    continued: bool,
) -> None:
    fetcher = _SettlingWorkday()
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.continue_from(0) if continued else fetcher.listings("Acme", root)

    assert fetcher.offsets == [0, 20, 40, 0, 20, 40]
    assert len(read.listings) == 40
    assert not read.discovery_truncated
    assert read.skip_reasons == {}
    assert read.deferred_targets == ()
    if continued:
        assert read.listing_coverage is not None
        assert len(read.listing_coverage.posting_urls) == 40
    else:
        assert read.complete


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize("shape", ["late_zero", "reset_after_last_page"])
def test_a_settled_total_stated_on_page_one_or_a_repeated_last_page_still_certifies(
    continued: bool, shape: str
) -> None:
    fetcher = _SettlingWorkday(**{shape: True})
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.continue_from(0) if continued else fetcher.listings("Acme", root)

    assert fetcher.offsets[3:] == [0, 20, 40]
    assert len(read.listings) == 40
    assert not read.discovery_truncated
    assert read.skip_reasons == {}
    assert read.deferred_targets == ()
    if continued:
        assert read.listing_coverage is not None
        assert len(read.listing_coverage.posting_urls) == 40
    else:
        assert read.complete


def test_a_two_slice_board_stating_its_total_on_page_one_certifies_after_settling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 2)
    fetcher = _SettlingWorkday(late_zero=True)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Acme", root)
    final = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=root, handoff_scopes=()
    )

    assert fetcher.offsets == [0, 20, 40, 0, 20, 40]
    assert final.skip_reasons == {}
    assert final.listing_coverage is not None
    assert len(final.listing_coverage.posting_urls) == 40


def test_an_empty_board_whose_total_drops_to_zero_stays_held_without_a_verifying_pass() -> None:
    class EmptiedWorkday(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            payload["total"] = 5 if len(self.offsets) == 1 else 0
            return payload

    fetcher = EmptiedWorkday(0, stated_total=5)
    read = fetcher.listings("Acme", "https://example.wd1.myworkdayjobs.com/Careers")

    assert fetcher.offsets == [0]
    assert read.listings == []
    assert not read.complete
    assert read.skip_reasons == {"incomplete_workday_listing": 1}


def test_a_verifying_pass_beyond_the_page_budget_keeps_the_hold_without_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 2)
    fetcher = _SettlingWorkday()
    read = fetcher.listings("Acme", "https://example.wd1.myworkdayjobs.com/Careers")

    assert fetcher.offsets == [0, 20, 40]
    assert not read.complete
    assert read.skip_reasons == {"incomplete_workday_listing": 1}
    assert read.listing_coverage is None


@pytest.mark.parametrize("continued", [False, True])
@pytest.mark.parametrize(
    ("change", "offsets"),
    [
        ("swapped", [0, 20, 40, 0]),
        ("moving", [0, 20, 40, 0, 20]),
        ("repeated", [0, 20, 40, 0, 20]),
    ],
)
def test_a_verifying_pass_that_disagrees_keeps_the_hold(
    continued: bool, change: str, offsets: list[int]
) -> None:
    fetcher = _SettlingWorkday(**{change: True})
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.continue_from(0) if continued else fetcher.listings("Acme", root)

    assert fetcher.offsets == offsets
    assert len(read.listings) == 40
    assert not read.complete
    assert read.discovery_truncated
    assert read.listing_coverage is None
    assert read.skip_reasons == {"incomplete_workday_listing": 1}
    assert read.deferred_targets == ()


@pytest.mark.parametrize("continued", [False, True])
def test_a_deadline_cutting_the_verifying_pass_keeps_the_hold_without_a_reset_cursor(
    monkeypatch: pytest.MonkeyPatch, continued: bool
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    fetcher = _SettlingWorkday(now=now, seconds_per_page=8.0)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    read = fetcher.continue_from(0) if continued else fetcher.listings("Acme", root)

    # Requests start at 100, 108, 116 and 124 s; the 30 s deadline stops the fifth.
    assert fetcher.offsets == [0, 20, 40, 0]
    assert not read.complete
    assert read.discovery_truncated
    assert read.listing_coverage is None
    assert read.skip_reasons == {"incomplete_workday_listing": 1}
    assert read.deferred_targets == ()


def test_distinct_workday_rows_without_end_are_bounded_per_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 3)
    monkeypatch.setattr(workday, "WORKDAY_CONTINUATION_PAGES", 2)
    fetcher = _ListedWorkday(10**6, stated_total=40)
    root = "https://example.wd1.myworkdayjobs.com/Careers"
    first = fetcher.listings("Acme", root)
    resumed = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=root, handoff_scopes=()
    )

    assert fetcher.offsets == [0, 20, 40, 60, 80]
    for read, offset in ((first, 60), (resumed, 100)):
        assert f"offset={offset}" in read.deferred_targets[0].url
        assert read.deferred_targets[0].automatic_eligible
        assert read.discovery_truncated
        assert read.listing_coverage is None


def test_workday_batch_neighbours_read_by_the_same_window_are_not_requested_again() -> None:
    fetcher = _ListedWorkday(40, stated_total=40, reset_after_last_page=True)

    read = fetcher.continue_from(0, 20)

    assert len({listing.url for listing in read.listings}) == 40
    assert not read.discovery_truncated
    assert read.deferred_targets == ()
    assert fetcher.offsets == [0, 20, 40]
    assert not read.complete
    assert read.detail_coverage is None
    assert read.listing_coverage == SourceDetailCoverage(
        platform="workday",
        board_url="https://example.wd1.myworkdayjobs.com/Careers",
        posting_urls=tuple(sorted(listing.url for listing in read.listings)),
    )


class _WorkdayDetailReader(BoardApiFetcher):
    """Read one scripted Workday body through its registered continuation."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: PostingCheck | Exception) -> None:
        super().__init__()
        self.answer = answer
        self.detail_urls: list[str] = []
        monkeypatch.setattr(
            self.publishers,
            "continue_listings",
            lambda *_args, **_kwargs: pytest.fail("Workday detail entered the generic page reader"),
        )

    def workday_detail(self, url: str) -> PostingCheck:
        self.detail_urls.append(url)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def read_targets(
        self, *targets: SourceContinuationTarget, options: SourceReadOptions | None = None
    ) -> SourceRead:
        return self.continue_listings(
            "Example",
            targets,
            root_url=targets[0].source_url,
            handoff_scopes=(),
            options=options or SourceReadOptions(),
        )


def _workday_detail_target(
    root: str = "https://example.wd1.myworkdayjobs.com/en-US/Careers",
    *,
    attempts: int = 1,
    certified: bool = True,
    unstable: bool = False,
) -> SourceContinuationTarget:
    url = f"{root}/job/Remote/Data-Manager_R-1"
    return SourceContinuationTarget(
        url=url,
        label="Data Manager",
        label_source="visible",
        source_url=root,
        structural_context="list",
        relations=("same-origin", "publisher-content"),
        deferred_reason="workday_detail_pending",
        detail_checkpoint=SourceDetailCheckpoint(
            platform="workday",
            board_url=root,
            failed_attempts=((url, attempts),) if attempts else (),
            pending_ids=(url,),
            listing_ids=(url,),
            listing_total=1 if certified else None,
            listing_unstable=unstable,
        ),
    )


def _workday_readable_detail() -> PostingCheck:
    return PostingCheck(
        status="readable",
        note="Workday published this posting.",
        platform="workday",
        title="Data Manager",
        location="Remote, Canada",
        facts=published_listing_facts("workday", locations="Remote, Canada"),
        text="Manage the published data products.",
        source=PostingSource(
            publisher="workday",
            record='{"jobPostingInfo":{"title":"Data Manager"}}',
            description_markup="<p>Manage the published data products.</p>",
            description_paths=("jobPostingInfo.jobDescription",),
        ),
    )


@pytest.mark.parametrize(
    "root",
    [
        "https://example.wd1.myworkdayjobs.com/en-US/Careers",
        "https://wd1.myworkdaysite.com/ja-JP/recruiting/example/External",
    ],
)
def test_workday_detail_continuation_reads_the_exact_owed_identity_and_retains_its_body(
    monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    target = _workday_detail_target(root)
    checked = _workday_readable_detail()
    fetcher = _WorkdayDetailReader(monkeypatch, checked)

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert len(read.listings) == 1
    listing = read.listings[0]
    assert listing.url == target.url
    assert listing.title == checked.title
    assert listing.requisition_id == "R-1"
    assert listing.published_description == checked.text
    assert listing.source == checked.source
    assert listing.facts == checked.facts
    assert read.detail.attempted == read.detail.read == 1
    assert read.detail.failed == 0
    assert read.deferred_targets == ()
    assert read.complete
    assert read.detail_coverage is not None
    assert read.detail_coverage.board_url == root
    assert read.detail_coverage.posting_urls == (target.url,)


@pytest.mark.parametrize("certified", [False, True])
@pytest.mark.parametrize("unstable", [False, True])
def test_workday_detail_success_cannot_certify_missing_or_unstable_enumeration(
    monkeypatch: pytest.MonkeyPatch, certified: bool, unstable: bool
) -> None:
    target = _workday_detail_target(certified=certified, unstable=unstable)
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert len(read.listings) == 1
    assert read.deferred_targets == ()
    assert read.complete is (certified and not unstable)
    assert (read.detail_coverage is not None) is (certified and not unstable)


@pytest.mark.parametrize("paced", [False, True])
def test_a_held_workday_detail_resumes_by_itself_only_when_paced(
    monkeypatch: pytest.MonkeyPatch, paced: bool
) -> None:
    target = _workday_detail_target(attempts=MAX_SOURCE_READ_ATTEMPTS - 1)
    fetcher = _WorkdayDetailReader(
        monkeypatch, HostHeldError(target.url, ready_at=100, paced=paced)
    )

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert read.attempted_target_urls == ()
    assert read.detail.attempted == read.detail.failed == 0
    assert read.deferred_targets == (replace(target, automatic_eligible=paced),)


@pytest.mark.parametrize("status", [401, 403, 404, 503])
def test_a_workday_detail_refusal_keeps_the_exact_identity_and_attempt_history(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    target = _workday_detail_target()
    fetcher = _WorkdayDetailReader(
        monkeypatch,
        PostingCheck(
            status="unreadable", platform="workday", note=f"HTTP {status}", http_status=status
        ),
    )

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert read.listings == []
    assert not read.complete
    assert read.detail.failed == read.detail.attempted == 1
    assert read.detail_coverage is None
    retained = read.deferred_targets[0]
    assert retained.url == target.url
    assert retained.source_url == target.source_url
    assert retained.deferred_reason == "workday_detail_pending"
    assert retained.automatic_eligible
    assert retained.detail_checkpoint is not None
    assert retained.detail_checkpoint.pending_ids == (target.url,)
    assert retained.detail_checkpoint.closed_ids == ()
    assert retained.detail_checkpoint.failed_attempts == ((target.url, 2),)


@pytest.mark.parametrize("manual", [False, True])
def test_workday_detail_retry_preserves_the_existing_attempt_limit_and_manual_override(
    monkeypatch: pytest.MonkeyPatch, manual: bool
) -> None:
    target = replace(_workday_detail_target(attempts=3), manual_detail_retry=manual)
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == ([target.url] if manual else [])
    if manual:
        assert len(read.listings) == 1
        assert read.deferred_targets == ()
    else:
        assert read.listings == []
        assert not read.complete
        retained = read.deferred_targets[0]
        assert not retained.automatic_eligible
        assert retained.detail_checkpoint == target.detail_checkpoint
        assert read.attempted_target_urls == ()


@pytest.mark.parametrize("missing", ["body", "source", "title"])
def test_workday_detail_metadata_without_a_verified_body_stays_pending(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    checked = _workday_readable_detail()
    missing_fields: dict[str, dict[str, Any]] = {
        "body": {"text": ""},
        "source": {"source": None},
        "title": {"title": ""},
    }
    checked = replace(checked, **missing_fields[missing])
    target = _workday_detail_target()
    fetcher = _WorkdayDetailReader(monkeypatch, checked)

    read = fetcher.read_targets(target)

    assert not read.complete
    assert read.listings == []
    assert read.detail.failed == 1
    assert read.deferred_targets[0].detail_checkpoint is not None
    assert read.deferred_targets[0].detail_checkpoint.pending_ids == (target.url,)


@pytest.mark.parametrize(
    "other_root",
    [
        "https://globex.wd1.myworkdayjobs.com/en-US/Careers",
        "https://example.wd1.myworkdayjobs.com/en-US/Other",
        "https://example.wd1.myworkdayjobs.com/ja-JP/Careers",
        "https://wd1.myworkdaysite.com/en-US/recruiting/globex/External",
    ],
)
def test_workday_detail_continuation_refuses_a_different_declared_scope(
    monkeypatch: pytest.MonkeyPatch, other_root: str
) -> None:
    target = replace(_workday_detail_target(), source_url=other_root)
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == []
    assert read.listings == []
    assert not read.complete
    assert read.skip_reasons == {"invalid_workday_detail_cursor": 1}


def test_protected_workday_detail_continuation_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _workday_detail_target()
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(
        target, options=SourceReadOptions(protected_posting_urls=frozenset((target.url,)))
    )

    assert fetcher.detail_urls == []
    assert read.listings == []
    assert read.protected_posting_urls == (target.url,)
    assert read.skip_reasons == {"protected_posting": 1}


def test_a_workday_detail_timeout_retains_the_owed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _workday_detail_target(attempts=0)
    fetcher = _WorkdayDetailReader(monkeypatch, httpx.ReadTimeout("Detail timed out"))

    read = fetcher.read_targets(target)

    assert read.listings == []
    assert not read.complete
    assert read.detail.failed == 1
    assert read.deferred_targets[0].detail_checkpoint is not None
    assert read.deferred_targets[0].detail_checkpoint.pending_ids == (target.url,)


@pytest.mark.parametrize("entries", [1, 3])
def test_workday_detail_recovery_preserves_unresolved_listing_locations(
    monkeypatch: pytest.MonkeyPatch, entries: int
) -> None:
    target = _workday_detail_target()
    checkpoint = target.detail_checkpoint
    assert checkpoint is not None
    original = published_listing_facts("workday", locations="3 Locations")
    target = replace(
        target,
        detail_checkpoint=replace(checkpoint, listing_facts=((target.url, original),)),
    )
    checked = replace(_workday_readable_detail(), location_entries=entries)
    fetcher = _WorkdayDetailReader(monkeypatch, checked)

    read = fetcher.read_targets(target)

    assert len(read.listings) == 1
    places = read.listings[0].facts.locations
    assert places.values == checked.facts.locations.values
    assert places.complete is (entries == 3)
    assert ("3 Locations" in places.unresolved_values) is (entries < 3)
    assert read.listings[0].published_description == checked.text


def test_a_workday_detail_interrupted_at_the_deadline_does_not_spend_a_failed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )

    class InterruptedDetail(_WorkdayDetailReader):
        def workday_detail(self, url: str) -> PostingCheck:
            self.detail_urls.append(url)
            now[0] += 31
            raise httpx.ReadTimeout("Deadline reached")

    target = _workday_detail_target(attempts=0)
    fetcher = InterruptedDetail(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert read.listings == []
    assert not read.complete
    assert read.detail.attempted == read.detail.interrupted == 1
    assert read.detail.failed == 0
    retained = read.deferred_targets[0]
    assert retained.detail_checkpoint == target.detail_checkpoint
    assert retained.automatic_eligible


@pytest.mark.parametrize("deadline_elapsed", [False, True])
def test_workday_detail_transport_timeout_preserves_the_operation_deadline(
    monkeypatch: pytest.MonkeyPatch, deadline_elapsed: bool
) -> None:
    from tests.support.http import policy_client

    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    requests: list[str] = []

    def timeout(url: str) -> httpx.Response:
        requests.append(url)
        now[0] += 31 if deadline_elapsed else 1
        raise httpx.ReadTimeout("Synthetic detail timeout", request=httpx.Request("GET", url))

    target = _workday_detail_target(attempts=1)
    fetcher = BoardApiFetcher(client=policy_client(timeout))
    read = fetcher.continue_listings(
        "Example", (target,), root_url=target.source_url, handoff_scopes=()
    )

    assert requests == [
        "https://example.wd1.myworkdayjobs.com/wday/cxs/example/Careers/job/Remote/Data-Manager_R-1"
    ]
    assert read.listings == []
    assert not read.complete
    assert read.detail.attempted == 1
    assert read.detail.interrupted == int(deadline_elapsed)
    assert read.detail.failed == int(not deadline_elapsed)
    retained = read.deferred_targets[0]
    assert retained.automatic_eligible
    assert retained.detail_checkpoint is not None
    assert retained.detail_checkpoint.failed_attempts == (
        (target.url, 1 if deadline_elapsed else 2),
    )
    assert dict(read.detail.failure_reasons) == ({} if deadline_elapsed else {"timeout": 1})


@pytest.mark.parametrize("invalid", ["platform", "board", "identity", "other_membership"])
def test_workday_detail_continuation_refuses_a_mismatched_checkpoint(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    target = _workday_detail_target()
    checkpoint = target.detail_checkpoint
    assert checkpoint is not None
    other_root = "https://globex.wd1.myworkdayjobs.com/en-US/Careers"
    other_url = f"{other_root}/job/Remote/Data-Manager_R-1"
    checkpoint_changes: dict[str, dict[str, Any]] = {
        "platform": {"platform": "Example"},
        "board": {"board_url": other_root},
        "identity": {"pending_ids": (other_url,), "failed_attempts": ()},
        "other_membership": {"listing_ids": (target.url, other_url), "listing_total": 2},
    }
    target = replace(target, detail_checkpoint=replace(checkpoint, **checkpoint_changes[invalid]))
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == []
    assert read.listings == []
    assert not read.complete
    assert read.skip_reasons == {"invalid_workday_detail_cursor": 1}


@pytest.mark.parametrize("readable", [False, True])
@pytest.mark.parametrize(
    "board_root",
    [
        "https://example.wd1.myworkdayjobs.com/en-US/Careers",
        "https://wd1.myworkdaysite.com/ja-JP/recruiting/example/External",
    ],
)
def test_a_corporate_source_recovers_its_exact_declared_workday_detail(
    monkeypatch: pytest.MonkeyPatch, readable: bool, board_root: str
) -> None:
    provenance = "https://corporate.example/careers"
    target = replace(_workday_detail_target(board_root), source_url=provenance)
    checked = (
        _workday_readable_detail()
        if readable
        else PostingCheck(
            status="unreadable", note="Temporarily unavailable", platform="workday", http_status=503
        )
    )
    fetcher = _WorkdayDetailReader(monkeypatch, checked)

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    if readable:
        assert len(read.listings) == 1
        assert read.listings[0].url == target.url
        assert read.complete
        assert read.detail_coverage is not None
        assert read.detail_coverage.board_url == board_root
    else:
        assert not read.complete
        assert read.deferred_targets[0].source_url == provenance
        assert read.deferred_targets[0].detail_checkpoint is not None
        assert read.deferred_targets[0].detail_checkpoint.board_url == board_root


def test_partial_workday_membership_recovers_an_owed_body_without_full_window_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _workday_detail_target(certified=False)
    checkpoint = target.detail_checkpoint
    assert checkpoint is not None
    earlier = f"{target.source_url}/job/Remote/Data-Manager_R-2"
    observed = f"{target.source_url}/job/Remote/Data-Manager_R-3"
    target = replace(
        target,
        detail_checkpoint=replace(
            checkpoint,
            completed=((earlier, earlier),),
            listing_ids=(target.url, earlier, observed),
        ),
    )
    fetcher = _WorkdayDetailReader(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(target)

    assert fetcher.detail_urls == [target.url]
    assert len(read.listings) == 1
    assert read.listings[0].published_description
    assert read.deferred_targets == ()
    assert not read.complete
    assert read.detail_coverage is None


@pytest.mark.parametrize("all_readable", [False, True])
def test_workday_detail_batch_certifies_scope_only_after_all_owed_targets_are_read(
    monkeypatch: pytest.MonkeyPatch, all_readable: bool
) -> None:
    first = _workday_detail_target()
    checkpoint = first.detail_checkpoint
    assert checkpoint is not None
    second_url = f"{first.source_url}/job/Remote/Data-Manager_R-2"
    ids = (first.url, second_url)
    first = replace(first, detail_checkpoint=replace(checkpoint, listing_ids=ids, listing_total=2))
    second = replace(
        first,
        url=second_url,
        detail_checkpoint=replace(
            checkpoint,
            pending_ids=(second_url,),
            failed_attempts=((second_url, 1),),
            listing_ids=ids,
            listing_total=2,
        ),
    )

    class OwedDetails(_WorkdayDetailReader):
        def workday_detail(self, url: str) -> PostingCheck:
            self.detail_urls.append(url)
            if url == second_url and not all_readable:
                return PostingCheck(
                    status="unreadable", platform="workday", note="HTTP 503", http_status=503
                )
            return _workday_readable_detail()

    fetcher = OwedDetails(monkeypatch, _workday_readable_detail())

    read = fetcher.read_targets(first, second)

    assert fetcher.detail_urls == list(ids)
    assert read.complete is all_readable
    assert read.detail.read == (2 if all_readable else 1)
    if all_readable:
        assert read.deferred_targets == ()
        assert read.detail_coverage is not None
        assert read.detail_coverage.posting_urls == ids
    else:
        assert read.detail_coverage is None
        assert len(read.deferred_targets) == 1
        assert read.deferred_targets[0].url == second_url


def _page_url(offset: int) -> str:
    return f"{_EXAMPLE_JOBS}?limit=20&offset={offset}"


def _workday_page_urls(read: SourceRead) -> list[str]:
    urls: list[str] = []
    for target in read.deferred_targets:
        parsed = urlsplit(target.url)
        query = parse_qs(parsed.query)
        query.pop("__reader_pages", None)
        urls.append(urlunsplit(parsed._replace(query=urlencode(query, doseq=True))))
    return urls


def test_a_workday_continuation_reads_pages_until_the_deadline_and_carries_the_next_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    fetcher = _ListedWorkday(900, stated_total=900, now=now, seconds_per_page=8.0)

    continued = fetcher.continue_from(200)

    # Pages start at 100, 108, 116 and 124 s; the 30 s deadline passes before a fifth.
    assert fetcher.offsets == [200, 220, 240, 260]
    assert [listing.title for listing in continued.listings] == [
        f"Role {index}" for index in range(200, 280)
    ]
    assert _workday_page_urls(continued) == [_page_url(280)]
    assert continued.attempted_target_urls == tuple(
        _page_url(offset) for offset in (200, 220, 240, 260)
    )
    assert continued.skip_reasons == {}


def test_batch_neighbours_already_read_are_not_cut_off_by_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )
    fetcher = _ListedWorkday(460, stated_total=0, now=now, seconds_per_page=12.0)

    continued = fetcher.continue_from(400, 420, 440)

    # The clock passes the deadline before the empty page can be requested.
    assert fetcher.offsets == [400, 420, 440]
    assert len(continued.listings) == 60
    assert "offset=460" in continued.deferred_targets[0].url
    assert continued.skip_reasons == {}


def test_a_short_workday_page_continues_until_an_explicit_empty_page() -> None:
    fetcher = _ListedWorkday(50, stated_total=0)

    continued = fetcher.continue_from(0)

    assert fetcher.offsets == [0, 20, 40, 60]
    assert len(continued.listings) == 50
    assert continued.deferred_targets == ()
    assert continued.outcome is SourceReadOutcome.PUBLISHER_LINKED_PAGES


def test_a_failed_later_workday_page_keeps_earlier_pages_and_resumes_at_its_offset() -> None:
    class FailingLaterPage(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            if int(body["offset"]) == 40:
                self.offsets.append(40)
                raise _refusal(url, 500)
            return super().post_json(url, body)

    fetcher = FailingLaterPage(900, stated_total=900)

    continued = fetcher.continue_from(0)

    assert fetcher.offsets == [0, 20, 40]
    assert len(continued.listings) == 40
    assert _workday_page_urls(continued) == [_page_url(40)]
    assert continued.attempted_target_urls == (_page_url(0), _page_url(20))
    assert continued.skip_reasons == {}


@pytest.mark.parametrize(
    "answer",
    [
        ["not", "a", "listing"],
        {"total": "many", "jobPostings": [{"title": "Role 40", "externalPath": "/job/R-40"}]},
    ],
    ids=["not_an_object", "non_integer_total"],
)
def test_an_unreadable_later_workday_page_keeps_earlier_pages_and_resumes_at_its_offset(
    answer: Any,
) -> None:
    class UnreadableLaterPage(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            if int(body["offset"]) == 40:
                self.offsets.append(40)
                return answer
            return super().post_json(url, body)

    fetcher = UnreadableLaterPage(900, stated_total=900)

    continued = fetcher.continue_from(0)

    assert fetcher.offsets == [0, 20, 40]
    assert len(continued.listings) == 40
    assert _workday_page_urls(continued) == [_page_url(40)]
    assert continued.attempted_target_urls == (_page_url(0), _page_url(20))


@pytest.mark.parametrize(
    "answer",
    [
        ["not", "a", "listing"],
        {"total": "many", "jobPostings": []},
        {"total": 0},
        {"total": 0, "jobPostings": None},
    ],
    ids=["not_an_object", "non_integer_total", "missing_rows", "null_rows"],
)
def test_an_unreadable_first_workday_page_is_kept_for_retry(answer: Any) -> None:
    class UnreadableFirstPage(_ListedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            self.offsets.append(int(body["offset"]))
            return answer

    fetcher = UnreadableFirstPage(900, stated_total=900)

    continued = fetcher.continue_from(0)

    assert fetcher.offsets == [0]
    assert continued.listings == []
    assert [target.url for target in continued.deferred_targets] == [_page_url(0)]
    assert continued.skip_reasons == {"fetch_failed": 1}


def test_workday_paging_uses_one_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    budgets: list[float] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )

    class TimedWorkday(BoardApiFetcher):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            budgets.append(self._remaining_seconds(url))
            now[0] += 16.0
            offset = int(body["offset"])
            return {
                "total": 200,
                "jobPostings": [
                    {
                        "title": f"Role {offset + index}",
                        "externalPath": f"/job/Role-{offset + index}",
                        "locationsText": "Remote, Canada",
                    }
                    for index in range(20)
                ],
            }

    read = TimedWorkday().listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert budgets == pytest.approx([30.0, 14.0])
    assert len(read.listings) == 40
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    # The deferred page cursor is continuation work, not a truncation.
    assert read.skip_reasons == {}
    assert read.exhausted_outcome is SourceReadOutcome.PUBLISHER_INDEX


def test_workday_timeout_at_the_deadline_retains_prior_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )

    class TimingOutWorkday(BoardApiFetcher):
        calls = 0

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            TimingOutWorkday.calls += 1
            if TimingOutWorkday.calls == 1:
                offset = int(body["offset"])
                return {
                    "total": 200,
                    "jobPostings": [
                        {
                            "title": f"Role {offset + index}",
                            "externalPath": f"/job/Role-{offset + index}",
                            "locationsText": "Remote, Canada",
                        }
                        for index in range(20)
                    ],
                }
            # Advance past the deadline before the transport surfaces the timeout,
            # so the in-flight branch converts it into a bounded partial read.
            now[0] += 60.0
            raise httpx.ReadTimeout(
                "board stalled",
                request=httpx.Request("POST", url),
            )

    read = TimingOutWorkday().listings("Example", "https://example.wd1.myworkdayjobs.com/Careers")

    assert TimingOutWorkday.calls == 2
    assert len(read.listings) == 20
    assert read.listings[0].title == "Role 0"
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    # The deferred page cursor is continuation work, not a truncation.
    assert read.skip_reasons == {}
    assert read.exhausted_outcome is SourceReadOutcome.PUBLISHER_INDEX


def test_oracle_listings_are_read_and_paged_without_a_network() -> None:
    """Parse the candidate-experience payload, and stop when the site says it is done."""
    requests: list[str] = []

    class ScriptedOracle(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requests.append(url)
            offset = int(url.split("offset=")[1])
            titles = range(offset, min(offset + 25, 26))
            return {
                "items": [
                    {
                        "TotalJobsCount": 26,
                        "requisitionList": [
                            {
                                "Id": 2020000 + index,
                                "Title": f"Role {index}",
                                "PrimaryLocation": "Springfield, IL, United States",
                            }
                            for index in titles
                        ],
                    }
                ]
            }

    read = ScriptedOracle().listings(
        "Example",
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4",
    )
    assert read is not None
    # The site said twenty-six and gave twenty-six, so this read is the whole board.
    assert read.complete is True
    assert len(read.listings) == 26

    first = read.listings[0]
    assert first.employer == "Example"
    assert first.facts.employer.provenance == "source.employer_hint"
    assert first.title == "Role 0"
    assert first.url == (
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4/job/2020000"
    )
    assert first.location == "Springfield, IL, United States"
    assert first.requisition_id == "2020000"

    assert [request.split("offset=")[1] for request in requests] == ["0", "25"]


def test_oracle_page_ceiling_carries_the_next_page_into_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 1)

    class ScriptedOracle(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            offset = int(url.split("offset=")[1])
            return {
                "items": [
                    {
                        "TotalJobsCount": 50,
                        "requisitionList": [
                            {"Id": index, "Title": f"Role {index}"}
                            for index in range(offset, offset + 25)
                        ],
                    }
                ]
            }

    fetcher = ScriptedOracle()
    read = fetcher.listings(
        "Example",
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4",
    )

    assert len(read.deferred_targets) == 1
    assert "offset=25" in read.deferred_targets[0].url

    continued = fetcher.continue_listings(
        "Example",
        read.deferred_targets,
        root_url="https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4",
        handoff_scopes=(),
    )

    assert [listing.title for listing in continued.listings] == [
        f"Role {index}" for index in range(25, 50)
    ]
    assert continued.deferred_targets == ()


def test_oracle_paging_uses_one_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [50.0]
    budgets: list[float] = []
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: now[0],
    )

    class TimedOracle(BoardApiFetcher):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            budgets.append(self._remaining_seconds(url))
            now[0] += 16.0
            offset = int(url.split("offset=")[1])
            return {
                "items": [
                    {
                        "TotalJobsCount": 200,
                        "requisitionList": [
                            {
                                "Id": f"REQ-{offset + index}",
                                "Title": f"Role {offset + index}",
                                "PrimaryLocation": "Remote, Canada",
                            }
                            for index in range(25)
                        ],
                    }
                ]
            }

    read = TimedOracle().listings(
        "Example",
        "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4",
    )

    assert budgets == pytest.approx([30.0, 14.0])
    assert len(read.listings) == 50
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    # The deferred page cursor is continuation work, not a truncation.
    assert read.skip_reasons == {}
    assert read.exhausted_outcome is SourceReadOutcome.PUBLISHER_INDEX


_ORACLE_SITE = "https://acme.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_4"


class _OracleBoard(BoardApiFetcher):
    """Serve scripted Oracle pages by offset, each stating the board's total."""

    def __init__(
        self,
        pages: dict[int, list[int]],
        total: int,
        *,
        tick: float = 0.0,
        failures: Mapping[int, Exception] | None = None,
    ) -> None:
        self.pages = pages
        self.total = total
        self.tick = tick
        self.failures = failures or {}
        self.now = [0.0]
        self.offsets: list[int] = []
        self.finders: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        offset = int(url.split("offset=")[1])
        self.offsets.append(offset)
        self.finders.append(parse_qs(urlsplit(url).query)["finder"][0])
        self.now[0] += self.tick
        if offset in self.failures:
            raise self.failures[offset]
        rows = [{"Id": job, "Title": f"Role {job}"} for job in self.pages.get(offset, [])]
        # Like the live API, a page past the end states a total of 0.
        total = self.total if rows else 0
        return {"items": [{"TotalJobsCount": total, "requisitionList": rows}]}


def _oracle_titles(read: Any) -> list[str]:
    return [listing.title for listing in read.listings]


def test_oracle_reads_past_a_short_page_in_the_middle_of_the_board() -> None:
    board = _OracleBoard({0: list(range(25)), 25: list(range(25, 33)), 50: list(range(50, 70))}, 70)

    read = board.listings("Example", _ORACLE_SITE)

    assert board.offsets == [0, 25, 50]
    assert _oracle_titles(read) == [f"Role {job}" for job in [*range(33), *range(50, 70)]]
    assert read.complete is True


def test_oracle_short_last_page_ends_complete_at_the_stated_total() -> None:
    board = _OracleBoard({0: list(range(25)), 25: list(range(25, 30))}, 30)

    read = board.listings("Example", _ORACLE_SITE)

    assert board.offsets == [0, 25]
    assert len(read.listings) == 30
    assert read.complete is True


def test_oracle_empty_page_stating_no_total_ends_the_read_short_of_the_earlier_total() -> None:
    board = _OracleBoard(
        {offset: list(range(offset, offset + 25)) for offset in range(0, 100, 25)}, 120
    )

    read = board.listings("Example", _ORACLE_SITE)

    assert board.offsets == [0, 25, 50, 75, 100]
    assert len(read.listings) == 100
    assert read.complete is False
    assert read.deferred_targets == ()
    assert read.note == oracle._SHORT_NOTE
    assert read.discovery_truncated is True
    assert read.skip_reasons == {"incomplete_oracle_listing": 1}


def test_oracle_continuation_ends_short_of_the_total_its_cursor_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 2)
    board = _OracleBoard({0: list(range(25)), 25: list(range(25, 50))}, 120)
    first = board.listings("Example", _ORACLE_SITE)

    continued = board.continue_listings(
        "Example", first.deferred_targets, root_url=_ORACLE_SITE, handoff_scopes=()
    )

    assert board.offsets == [0, 25, 50]
    assert continued.listings == []
    assert continued.discovery_truncated is True
    assert continued.skip_reasons == {"incomplete_oracle_listing": 1}


def test_a_failed_later_oracle_page_keeps_earlier_rows_and_resumes_at_its_offset() -> None:
    board = _OracleBoard(
        {0: list(range(25)), 25: list(range(25, 50))},
        50,
        failures={25: httpx.ConnectError("refused")},
    )

    read = board.listings("Example", _ORACLE_SITE)

    assert len(read.listings) == 25
    assert read.complete is False
    assert [target.url.split("offset=")[1] for target in read.deferred_targets] == ["25"]
    assert read.deferred_targets[0].automatic_eligible is True


def test_an_unpaced_hold_on_a_later_oracle_page_keeps_a_manual_cursor_and_says_so() -> None:
    board = _OracleBoard(
        {0: list(range(25)), 25: list(range(25, 50))},
        50,
        failures={25: HostHeldError("https://acme.fa.us6.oraclecloud.com/", ready_at=100)},
    )

    read = board.listings("Example", _ORACLE_SITE)

    assert len(read.listings) == 25
    assert read.skip_reasons == {"host_held": 1}
    assert "requested a wait" in read.note
    assert [target.automatic_eligible for target in read.deferred_targets] == [False]


def test_an_unpaced_hold_on_a_later_continued_oracle_page_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 1)
    board = _OracleBoard(
        {offset: list(range(offset, offset + 25)) for offset in range(0, 100, 25)}, 100
    )
    first = board.listings("Example", _ORACLE_SITE)
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 3)
    board.failures = {50: HostHeldError("https://acme.fa.us6.oraclecloud.com/", ready_at=100)}

    continued = board.continue_listings(
        "Example", first.deferred_targets, root_url=_ORACLE_SITE, handoff_scopes=()
    )

    assert len(continued.listings) == 25
    assert continued.skip_reasons == {"host_held": 1}
    assert [target.automatic_eligible for target in continued.deferred_targets] == [False]


def test_an_oracle_cursor_naming_no_page_size_is_refused_without_a_request() -> None:
    board = _OracleBoard({0: list(range(25))}, 25)
    cursor = oracle._cursor(
        "https://acme.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
        "&finder=findReqs;siteNumber=CX_4,limit=0,offset=25",
        _ORACLE_SITE,
        oracle._Pages(next_offset=25),
    )

    continued = board.continue_listings(
        "Example", (cursor,), root_url=_ORACLE_SITE, handoff_scopes=()
    )

    assert board.offsets == []
    assert continued.skip_reasons == {"invalid_oracle_cursor": 1}


def test_oracle_read_cut_by_the_deadline_carries_a_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board = _OracleBoard(
        {offset: list(range(offset, offset + 9)) for offset in range(0, 500, 25)}, 500, tick=16.0
    )
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: board.now[0],
    )

    read = board.listings("Example", _ORACLE_SITE)

    assert board.offsets == [0, 25]
    assert read.complete is False
    assert [target.url.split("offset=")[1] for target in read.deferred_targets] == ["50"]


def test_oracle_continuation_reads_several_pages_until_the_end_or_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {offset: list(range(offset, offset + 10)) for offset in range(0, 200, 25)}
    board = _OracleBoard(pages, 200, tick=7.0)
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: board.now[0],
    )
    first = board.listings("Example", _ORACLE_SITE)
    assert board.offsets == [0, 25, 50, 75, 100]

    continued = board.continue_listings(
        "Example", first.deferred_targets, root_url=_ORACLE_SITE, handoff_scopes=()
    )

    assert board.offsets[5:] == [125, 150, 175]
    assert _oracle_titles(continued) == [
        f"Role {job}" for start in (125, 150, 175) for job in range(start, start + 10)
    ]
    assert continued.deferred_targets == ()


def test_oracle_continuation_carries_its_cursor_when_the_deadline_cuts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {offset: list(range(offset, offset + 25)) for offset in range(0, 1000, 25)}
    board = _OracleBoard(pages, 1000, tick=11.0)
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: board.now[0],
    )
    first = board.listings("Example", _ORACLE_SITE)
    assert board.offsets == [0, 25, 50]

    continued = board.continue_listings(
        "Example", first.deferred_targets, root_url=_ORACLE_SITE, handoff_scopes=()
    )

    assert board.offsets[3:] == [75, 100, 125]
    assert len(continued.listings) == 75
    assert [target.url.split("offset=")[1] for target in continued.deferred_targets] == ["150"]


def test_an_oracle_site_shared_by_employers_reads_the_facets_its_career_page_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One employer's career page narrows a shared site by organization, through continuation."""
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 1)
    board = _OracleBoard({0: list(range(25)), 25: list(range(25, 40))}, 40)
    career_page = (
        f"{_ORACLE_SITE}/jobs?mode=location&selectedOrganizationsFacet=300000003438796"
        "&selectedLocationsFacet=300000000123%3B300000000456&selectedPostingDatesFacet=7"
    )

    first = board.listings("Example", career_page)
    continued = board.continue_listings(
        "Example", first.deferred_targets, root_url=_ORACLE_SITE, handoff_scopes=()
    )

    facets = (
        "selectedOrganizationsFacet=300000003438796,"
        "selectedLocationsFacet=300000000123;300000000456,"
    )
    assert board.finders == [
        f"findReqs;siteNumber=CX_4,{facets}limit=25,offset=0",
        f"findReqs;siteNumber=CX_4,{facets}limit=25,offset=25",
    ]
    assert _oracle_titles(continued) == [f"Role {job}" for job in range(25, 40)]
    assert continued.deferred_targets == ()


def test_oracle_lists_a_requisition_repeated_across_pages_once() -> None:
    board = _OracleBoard({0: list(range(25)), 25: [23, 24, *range(25, 40)]}, 40)

    read = board.listings("Example", _ORACLE_SITE)

    assert _oracle_titles(read) == [f"Role {job}" for job in range(40)]
    assert read.complete is True


class _OracleFacetBoard(BoardApiFetcher):
    """Serve an Oracle board behind an offset window, sliced by a nested posting-date facet.

    ``partitions`` maps each date value to its pages by offset; ``hidden`` adds
    postings a value counts but never serves; ``glitches`` serves a page with a
    wrong total that many times before answering it truly.
    """

    def __init__(
        self,
        partitions: dict[str, dict[int, list[int]]],
        *,
        window: int,
        nested: tuple[str, int] = ("7", 4),
        hidden: Mapping[str, int] | None = None,
        glitches: dict[tuple[str, int], int] | None = None,
        facets: bool = True,
    ) -> None:
        self.partitions = partitions
        self.window = window
        self.nested = nested
        self.hidden = hidden or {}
        self.glitches = glitches or {}
        self.facets = facets
        self.requests: list[tuple[str, int]] = []

    @property
    def everything(self) -> list[int]:
        return [
            job for pages in self.partitions.values() for at in sorted(pages) for job in pages[at]
        ]

    def count(self, value: str) -> int:
        served = {job for rows in self.partitions[value].values() for job in rows}
        return len(served) + self.hidden.get(value, 0)

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        finder = parse_qs(urlsplit(url).query)["finder"][0]
        fields = dict(part.split("=") for part in finder.split(";")[1].split(","))
        posted = fields.get("selectedPostingDatesFacet", "")
        limit, offset = int(fields["limit"]), int(fields["offset"])
        self.requests.append((posted, offset))
        if posted:
            jobs = self.partitions[posted].get(offset, [])
            total = self.count(posted)
        else:
            jobs = self.everything[offset : offset + limit]
            total = sum(self.count(value) for value in self.partitions)
        if self.glitches.get((posted, offset), 0) > 0:
            self.glitches[(posted, offset)] -= 1
            total += 3
        if offset + limit > self.window:
            jobs, total = [], 0
        values = [self.nested, *((value, self.count(value)) for value in self.partitions)]
        answer: dict[str, Any] = {
            "TotalJobsCount": total if jobs else 0,
            "requisitionList": [{"Id": str(job), "Title": f"Role {job}"} for job in jobs],
        }
        if self.facets:
            answer["postingDatesFacet"] = [
                {"Id": int(value), "Name": f"Range {value}", "TotalCount": count}
                for value, count in values
            ]
        return {"items": [answer]}


def _facet_board(**kwargs: Any) -> _OracleFacetBoard:
    """A 125-requisition board past a 100-row window: 75 recent postings and 50 older."""
    recent = {at: list(range(at, at + 25)) for at in (0, 25, 50)}
    older = {at: list(range(75 + at, 100 + at)) for at in (0, 25)}
    return _OracleFacetBoard({"30": recent, "31": older}, window=100, **kwargs)


def _requisitions(read: Any) -> list[int]:
    return sorted(int(listing.requisition_id) for listing in read.listings)


def test_an_oracle_board_past_the_offset_window_reads_every_posting_date_slice_and_certifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board()

    read = board.listings("Example", _ORACLE_SITE)

    assert _requisitions(read) == list(range(125))
    assert read.complete is True
    assert board.requests[1:] == [("30", 0), ("30", 25), ("30", 50), ("31", 0), ("31", 25)]


def test_an_oracle_partition_reads_to_its_count_past_a_short_page_and_a_page_of_repeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board()
    board.partitions["30"] = {0: list(range(4)), 25: [0, 1, 2], 50: list(range(4, 29))}
    board.hidden = {"30": 30}

    read = board.listings("Example", _ORACLE_SITE)

    assert [offset for posted, offset in board.requests if posted == "30"] == [0, 25, 50]
    assert _requisitions(read) == [*range(29), *range(75, 125)]
    assert read.complete is False


def test_an_oracle_page_stating_another_total_is_refetched_once_and_used_when_it_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board(glitches={("30", 25): 1})

    read = board.listings("Example", _ORACLE_SITE)

    assert board.requests.count(("30", 25)) == 2
    assert _requisitions(read) == list(range(125))
    assert read.complete is True


def test_an_oracle_page_whose_total_stays_different_reads_every_row_uncertified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board(glitches={("30", 25): 2})

    read = board.listings("Example", _ORACLE_SITE)

    assert board.requests.count(("30", 25)) == 2
    assert _requisitions(read) == list(range(125))
    assert read.complete is False
    assert read.discovery_truncated is True
    assert read.skip_reasons == {"incomplete_oracle_listing": 1}


def test_an_oracle_read_already_unstable_uses_a_mismatched_page_without_refetching_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board(glitches={("30", 25): 2, ("30", 50): 1})

    read = board.listings("Example", _ORACLE_SITE)

    assert board.requests.count(("30", 50)) == 1
    assert _requisitions(read) == list(range(125))
    assert read.complete is False


def test_an_oracle_cursor_whose_plan_disagrees_with_its_request_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 3)
    board = _facet_board()
    target = board.listings("Example", _ORACLE_SITE).deferred_targets[0]
    other_slice = target.url.replace("selectedPostingDatesFacet=30", "selectedPostingDatesFacet=31")
    cursors = (
        replace(target, url=other_slice),
        replace(target, detail_checkpoint=None),
        replace(target, url=target.url.split("&__reader_partitions")[0]),
    )

    for cursor in cursors:
        read = board.continue_listings(
            "Example", (cursor,), root_url=_ORACLE_SITE, handoff_scopes=()
        )
        assert "invalid_oracle_cursor" in read.skip_reasons
        assert not read.listings


def test_an_oracle_board_counting_postings_it_never_serves_reads_every_served_row_uncertified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board(hidden={"31": 4})
    board.partitions["31"] = {0: list(range(75, 96)), 25: list(range(96, 121)), 50: [121, 122]}

    read = board.listings("Example", _ORACLE_SITE)

    assert [offset for posted, offset in board.requests if posted == "31"] == [0, 25, 50]
    assert _requisitions(read) == list(range(123))
    assert read.complete is False


def test_an_oracle_board_past_the_window_whose_facet_does_not_sum_reads_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    board = _facet_board(facets=False)

    read = board.listings("Example", _ORACLE_SITE)

    assert [posted for posted, _offset in board.requests] == [""] * 5
    assert len(read.listings) == 100
    assert read.complete is False
    assert read.note == oracle._SHORT_NOTE


def test_an_oracle_board_inside_the_window_pages_without_a_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 200)
    board = _facet_board()
    board.window = 200

    read = board.listings("Example", _ORACLE_SITE)

    assert board.requests == [("", 0), ("", 25), ("", 50), ("", 75), ("", 100)]
    assert len(read.listings) == 125
    assert read.complete is True


def test_a_partitioned_oracle_read_resumes_its_plan_across_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oracle, "ORACLE_WINDOW", 100)
    monkeypatch.setattr(oracle, "ORACLE_MAX_PAGES", 3)
    board = _facet_board()
    read = board.listings("Example", _ORACLE_SITE)
    assert board.requests == [("", 0), ("30", 0), ("30", 25)]
    listings, targets = list(read.listings), read.deferred_targets

    for _run in range(10):
        if not targets:
            break
        continued = board.continue_listings(
            "Example", targets, root_url=_ORACLE_SITE, handoff_scopes=()
        )
        listings.extend(continued.listings)
        targets = continued.deferred_targets

    assert sorted(int(listing.requisition_id) for listing in listings) == list(range(125))
    assert board.requests[3:] == [("30", 50), ("31", 0), ("31", 25)]
    assert continued.skip_reasons == {}
    assert continued.discovery_truncated is False


def test_smartrecruiters_listings_are_read_without_a_network() -> None:
    """Parse the SmartRecruiters payload shape, including the URL fallback."""
    requested: list[str] = []

    class ScriptedSmartRecruiters(BoardApiFetcher):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return self.payload

    payload = {
        "content": [
            {
                "name": "Data Platform Lead",
                "ref": "https://jobs.smartrecruiters.com/AcmeCorp/744000",
                "location": {"city": "Toronto", "country": "ca"},
                "refNumber": "REQ-1",
                "id": "744000",
            },
            {"name": "Staff Engineer", "id": "744001", "location": {"city": "Remote"}},
            {"id": "744002"},
        ]
    }
    listings = published_listings(
        ScriptedSmartRecruiters(payload), "Acme", "https://careers.smartrecruiters.com/AcmeCorp"
    )
    assert listings is not None
    assert [item.title for item in listings] == ["Data Platform Lead", "Staff Engineer"]
    assert listings[0].url == "https://jobs.smartrecruiters.com/AcmeCorp/744000"
    assert listings[0].location == "Toronto, ca"
    assert listings[0].requisition_id == "REQ-1"
    # Build the posting URL from the organization and board id when no ref exists.
    assert listings[1].url == "https://jobs.smartrecruiters.com/AcmeCorp/744001"
    assert listings[1].location == "Remote"
    assert listings[1].requisition_id == "744001"

    # Treat a payload without listings as an empty board.
    assert (
        published_listings(
            ScriptedSmartRecruiters({"totalFound": 0}),
            "Acme",
            "https://careers.smartrecruiters.com/AcmeCorp",
        )
        == []
    )


def test_workable_listings_are_read_without_a_network() -> None:
    """Parse the Workable widget payload shape, including the apply-link fallback."""
    requested: list[str] = []

    class ScriptedWorkable(BoardApiFetcher):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return self.payload

    payload = {
        "jobs": [
            {
                "title": "Backend Engineer",
                "url": "https://apply.workable.com/globex/j/ABC123/",
                "shortcode": "ABC123",
                "telecommuting": True,
                "country": "United States",
                "city": "",
                "state": "",
                "locations": [
                    {
                        "country": "United States",
                        "countryCode": "US",
                        "city": "",
                        "region": None,
                        "hidden": False,
                    }
                ],
                "description": "<p>Build backend services for clinical teams.</p>",
            },
            {
                "title": "Data Analyst",
                "application_url": "https://apply.workable.com/globex/j/DEF456/apply/",
                "telecommuting": False,
                "country": "United States",
                "city": "Northtown",
                "state": "NT",
                "locations": [
                    {
                        "country": "United States",
                        "countryCode": "US",
                        "city": "Northtown",
                        "region": "NT",
                        "hidden": False,
                    },
                    {
                        "country": "United States",
                        "countryCode": "US",
                        "city": "Eastport",
                        "region": "EP",
                        "hidden": True,
                    },
                ],
                "description": "<p>Report on hospital data.</p><ul><li>Own dashboards</li></ul>",
            },
            {"title": "Unlinked Role"},
        ]
    }
    listings = published_listings(
        ScriptedWorkable(payload), "Globex", "https://apply.workable.com/globex"
    )
    assert listings is not None
    assert requested == ["https://apply.workable.com/api/v1/widget/accounts/globex?details=true"]
    assert [item.title for item in listings] == ["Backend Engineer", "Data Analyst"]
    assert listings[0].url == "https://apply.workable.com/globex/j/ABC123/"
    assert listings[0].requisition_id == "ABC123"
    # Use the apply link without inventing a shortcode.
    assert listings[1].url == "https://apply.workable.com/globex/j/DEF456/apply/"
    assert listings[1].requisition_id == ""
    assert [item.published_description for item in listings] == [
        "Build backend services for clinical teams.",
        "Report on hospital data.\n\n- Own dashboards",
    ]
    assert [item.facts.locations.values for item in listings] == [
        ("United States",),
        ("Northtown, NT, United States",),
    ]

    assert (
        published_listings(
            ScriptedWorkable({"name": "Globex"}),
            "Globex",
            "https://apply.workable.com/globex",
        )
        == []
    )


def test_recruitee_listings_are_read_without_a_network() -> None:
    """Parse the Recruitee offers payload shape, including the apply-link fallback."""
    requested: list[str] = []

    class ScriptedRecruitee(BoardApiFetcher):
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return self.payload

    payload = {
        "offers": [
            {
                "title": "Site Reliability Engineer",
                "careers_url": "https://initech.recruitee.com/o/site-reliability-engineer",
                "location": "Amsterdam",
                "id": 42,
            },
            {
                "title": "Product Designer",
                "careers_apply_url": "https://initech.recruitee.com/o/product-designer/c/new",
                "location": "Remote",
            },
            {"title": "Unlinked Role"},
        ]
    }
    listings = published_listings(
        ScriptedRecruitee(payload), "Initech", "https://initech.recruitee.com/"
    )
    assert listings is not None
    assert requested == ["https://initech.recruitee.com/api/offers/"]
    assert [item.title for item in listings] == ["Site Reliability Engineer", "Product Designer"]
    assert listings[0].url == "https://initech.recruitee.com/o/site-reliability-engineer"
    assert listings[0].location == "Amsterdam"
    assert listings[0].requisition_id == "42"
    assert listings[1].url == "https://initech.recruitee.com/o/product-designer/c/new"
    assert listings[1].requisition_id == ""

    assert (
        published_listings(
            ScriptedRecruitee({"jobs": []}), "Initech", "https://initech.recruitee.com/"
        )
        == []
    )


def test_dayforce_feed_retains_bodies_and_collapses_regional_variants() -> None:
    requested: list[str] = []

    class ScriptedDayforce(BoardApiFetcher):
        def __init__(self, payload: list[dict[str, Any]]) -> None:
            self.payload = payload

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requested.append(url)
            return self.payload

    url = "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL/jobs/1458"
    payload = [
        {
            "Title": "Clinical Informatics Lead",
            "Description": "Own the clinical data model and terminology governance.",
            "CompanyName": "Acme Health",
            "JobDetailsUrl": url,
            "City": "London",
            "State": "ON",
            "Country": "CAN",
            "ReferenceNumber": 1458,
            "ParentRequisitionCode": 421,
        },
        {
            "Title": "Clinical Informatics Lead",
            "Description": "Own the clinical data model and terminology governance.",
            "CompanyName": "Acme Health",
            "JobDetailsUrl": url,
            "City": "Remote",
            "State": "NY",
            "Country": "US",
            "ReferenceNumber": 1458,
            "ParentRequisitionCode": 421,
        },
    ]
    read = ScriptedDayforce(payload).listings(
        "Acme Health",
        "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL",
    )

    assert requested == [
        "https://www.dayforcehcm.com/api/acme/v1/jobfeeds?internalJobBoardCode=ACMECANDIDATEPORTAL"
    ]
    assert read.complete is True
    assert read.raw_count == 2
    assert read.skip_reasons == {"regional_variant_collapsed": 1}
    [listing] = read.listings
    assert listing.title == "Clinical Informatics Lead"
    assert listing.url == url
    assert listing.location == "London, ON, Canada"
    assert listing.requisition_id == "421"
    assert listing.published_description == (
        "Own the clinical data model and terminology governance."
    )


def test_dayforce_invalid_payload_cannot_claim_a_complete_empty_board() -> None:
    class ScriptedDayforce(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, _url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return {"error": "response shape changed"}

    with pytest.raises(ValueError, match="did not return a list"):
        ScriptedDayforce().listings(
            "Acme", "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL"
        )


def test_dayforce_malformed_rows_remain_visible_as_an_incomplete_read() -> None:
    class ScriptedDayforce(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_json(self, _url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return [
                {
                    "Title": "Clinical Informatics Lead",
                    "JobDetailsUrl": (
                        "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL/jobs/1458"
                    ),
                    "ReferenceNumber": 1458,
                },
                {"Title": "Missing URL"},
                "not an object",
            ]

    read = ScriptedDayforce().listings(
        "Acme", "https://jobs.dayforcehcm.com/en-US/acme/ACMECANDIDATEPORTAL"
    )

    assert read.complete is False
    assert read.raw_count == 3
    assert len(read.listings) == 1
    assert read.skip_reasons == {"missing_required_posting_identity": 2}
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def _teamtailor_feed(url: str, host: str) -> str:
    """Return a full first page at offset 0 and a one-item last page after it."""
    count = teamtailor.TEAMTAILOR_PAGE if "offset=0" in url else 1
    items = "".join(
        "<item>"
        f"<title>Clinical Product Manager {index}</title>"
        f"<description>&lt;p&gt;Own clinical workflows {index}.&lt;/p&gt;</description>"
        f"<link>https://{host}/jobs/{7000 + index}-role</link>"
        f"<guid>job-{index}</guid>"
        "<tt:locations><tt:location><tt:name>Paris</tt:name></tt:location></tt:locations>"
        "</item>"
        for index in range(count)
    )
    return (
        '<?xml version="1.0"?><rss xmlns:tt="https://teamtailor.com/locations">'
        f"<channel>{items}</channel></rss>"
    )


@pytest.mark.parametrize("host", ["alpha.teamtailor.com", "alpha.na.teamtailor.com"])
def test_teamtailor_pages_until_the_complete_full_body_feed(host: str) -> None:
    requested: list[str] = []

    class ScriptedTeamtailor(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_feed(self, url: str) -> str:
            requested.append(url)
            return _teamtailor_feed(url, host)

    read = ScriptedTeamtailor().listings("Alpha", f"https://{host}/jobs")

    assert requested == [
        f"https://{host}/jobs.rss?per_page=100&offset=0",
        f"https://{host}/jobs.rss?per_page=100&offset=100",
    ]
    assert read.complete is True
    assert read.raw_count == teamtailor.TEAMTAILOR_PAGE + 1
    assert read.listings[-1].published_description == "Own clinical workflows 0."


def test_a_teamtailor_read_ending_on_an_empty_page_records_one_listing_page() -> None:
    host = "alpha.teamtailor.com"

    class ScriptedTeamtailor(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_feed(self, url: str) -> str:
            if "offset=0" in url:
                return _teamtailor_feed(url, host)
            return (
                '<?xml version="1.0"?><rss xmlns:tt="https://teamtailor.com/locations">'
                "<channel></channel></rss>"
            )

    read = ScriptedTeamtailor().listings("Alpha", f"https://{host}/jobs")

    assert read.complete is True
    assert read.raw_count == teamtailor.TEAMTAILOR_PAGE
    assert read.listing_pages == 1


_TEAMTAILOR_FOOTER = '<a href="https://www.teamtailor.com/en?utm_source=career_site">Teamtailor</a>'


class _ScriptedTeamtailorPage(ScriptedPage):
    def __init__(self, page: str, feed_host: str) -> None:
        super().__init__(page)
        self.feed_host = feed_host
        self.feeds: list[str] = []

    def get_feed(self, url: str) -> str:
        self.feeds.append(url)
        return _teamtailor_feed(url, self.feed_host)


def test_a_teamtailor_site_on_an_employers_own_domain_reads_the_feed_on_that_domain() -> None:
    page = (
        "https://careers.example-health.test/jobs\n"
        f"<html><body><p>Openings</p><footer>{_TEAMTAILOR_FOOTER}</footer></body></html>"
    )
    fetcher = _ScriptedTeamtailorPage(page, "careers.example-health.test")

    read = fetcher.listings("Example Health", "https://careers.example-health.test/jobs")

    assert fetcher.feeds == [
        "https://careers.example-health.test/jobs.rss?per_page=100&offset=0",
        "https://careers.example-health.test/jobs.rss?per_page=100&offset=100",
    ]
    assert read.complete is True
    assert read.raw_count == teamtailor.TEAMTAILOR_PAGE + 1
    assert read.listings[0].url == "https://careers.example-health.test/jobs/7000-role"


def test_an_employer_page_linking_a_teamtailor_tenant_reads_the_tenant_feed() -> None:
    page = (
        "https://www.example-health.test/careers\n"
        f"<html><body>{_TEAMTAILOR_FOOTER}"
        '<a href="https://examplehealth.teamtailor.com/jobs">Open roles</a></body></html>'
    )
    fetcher = _ScriptedTeamtailorPage(page, "examplehealth.teamtailor.com")

    read = fetcher.listings("Example Health", "https://www.example-health.test/careers")

    assert fetcher.feeds[0] == "https://examplehealth.teamtailor.com/jobs.rss?per_page=100&offset=0"
    assert read.complete is True


@pytest.mark.parametrize(
    "board",
    [
        pytest.param("https://examplehealth.teamtailor.com/jobs", id="teamtailor"),
        pytest.param("https://examplehealth.jobs.personio.com/", id="personio"),
    ],
)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "<!DOCTYPE html><html><head><title>Careers</title></head><body><br></body>",
            id="html",
        ),
        pytest.param("<html><body><p>Careers</p></body></html>", id="well-formed-markup"),
    ],
)
def test_a_feed_board_answering_with_a_web_page_is_a_failed_read(board: str, body: str) -> None:
    class HtmlInsteadOfFeed(BoardApiFetcher):
        def __init__(self) -> None:
            pass

        def get_feed(self, url: str) -> str:
            return body

    with pytest.raises(ValueError, match=r"did not return an XML feed"):
        HtmlInsteadOfFeed().listings("Example Health", board)


def test_a_generic_rss_feed_on_a_teamtailor_site_host_is_a_failed_read() -> None:
    page = (
        "https://careers.example-health.test/jobs\n"
        f"<html><body><footer>{_TEAMTAILOR_FOOTER}</footer></body></html>"
    )

    class NewsFeedOnHost(ScriptedPage):
        def get_feed(self, url: str) -> str:
            return (
                '<?xml version="1.0"?><rss><channel><item><title>Company news</title>'
                "<link>https://careers.example-health.test/news/1</link>"
                "<guid>news-1</guid></item></channel></rss>"
            )

    with pytest.raises(ValueError, match=r"did not return an XML feed"):
        NewsFeedOnHost(page).listings("Example Health", "https://careers.example-health.test/jobs")


@pytest.mark.parametrize(
    ("link", "tenant"),
    [
        pytest.param('<a href="https://www.teamtailor.com">', None, id="site-bare-host"),
        pytest.param('<a href="https://www.teamtailor.com?utm_source=x">', None, id="site-query"),
        pytest.param('<a href="https://acme.teamtailor.com">', "acme", id="tenant-bare-host"),
        pytest.param(
            '<a href="https://acme.teamtailor.com?utm_source=x">', "acme", id="tenant-query"
        ),
    ],
)
def test_a_teamtailor_link_without_a_trailing_slash_names_the_platform(
    link: str, tenant: str | None
) -> None:
    found = identify_board(link)

    assert found is not None
    assert found[0] == "teamtailor"
    assert found[1].groupdict().get("tenant") == tenant


@pytest.mark.parametrize("service", ["app", "api", "assets", "support"])
def test_a_teamtailor_service_host_beside_the_footer_names_no_tenant(service: str) -> None:
    page = f'{_TEAMTAILOR_FOOTER}<a href="https://{service}.teamtailor.com/login">Sign in</a>'

    found = identify_board(page)

    assert found is not None
    assert found[0] == "teamtailor"
    assert found[1].groupdict().get("tenant") is None


def test_teamtailors_own_careers_tenant_is_a_tenant() -> None:
    found = identify_board('<a href="https://career.teamtailor.com/jobs">Work at Teamtailor</a>')

    assert found is not None
    assert found[1].group("tenant") == "career"


def test_hydrated_search_continuation_preserves_the_publishers_body_read_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _TaleoSite()
    site.fail_page = 2
    first = site.fetcher.listings("Acme", site.root)
    assert all(listing.published_description for listing in first.listings)
    prior_urls = tuple(listing.url for listing in first.listings)
    assert len(prior_urls) == 2
    site.fail_page = None
    readers = site.fetcher._continuation_readers()
    original = readers["taleo_search_page"]
    emitted: list[SourceRead] = []

    def read_page(employer: str, target: SourceContinuationTarget) -> SourceRead:
        read = original(employer, target)
        urls = tuple(dict.fromkeys((*prior_urls, *(listing.url for listing in read.listings))))
        assert all(listing.published_description for listing in read.listings)
        declared = replace(read, raw_count=max(read.raw_count, len(urls)), detail_read_urls=urls)
        emitted.append(declared)
        return declared

    monkeypatch.setattr(
        site.fetcher, "_continuation_readers", lambda: {**readers, "taleo_search_page": read_page}
    )
    result = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    assert len(emitted) == 1
    assert {listing.requisition_id for listing in result.listings} == {"3"}
    assert result.raw_count == 3
    assert result.detail_read_urls == emitted[0].detail_read_urls
    assert result.detail_coverage is None
    assert result.listing_coverage is None


@pytest.mark.parametrize("generic_outcome", ["success", "failure", "hold", "protected"])
def test_mixed_search_batches_preserve_owned_bodies_and_generic_results(
    monkeypatch: pytest.MonkeyPatch,
    generic_outcome: str,
) -> None:
    site = _TaleoSite()
    site.failed = {1}
    first = site.fetcher.listings("Acme", site.root)
    [typed] = first.deferred_targets
    site.failed.clear()
    generic_url = site.detail_url(9)
    generic = replace(
        typed,
        url=generic_url,
        deferred_reason="",
        detail_checkpoint=None,
    )
    generic_read = SourceRead(
        listings=[
            replace(
                source_listing("Clinical Analyst 9", generic_url, "Acme"),
                requisition_id="9",
                published_description="Generic capture without typed body authority.",
            )
        ]
        if generic_outcome == "success"
        else [],
        complete=False,
        raw_count=1,
        outcome=SourceReadOutcome.CRAWL_CONTINUATION
        if generic_outcome in {"failure", "hold"}
        else SourceReadOutcome.PUBLISHER_LINKED_PAGES,
        attempted_target_urls=(generic_url,) if generic_outcome != "hold" else (),
        deferred_targets=(replace(generic, automatic_eligible=False),)
        if generic_outcome in {"failure", "hold"}
        else (),
        skip_reasons={"generic_" + generic_outcome: 1},
        detail_read_urls=(generic_url,) if generic_outcome == "success" else None,
    )
    monkeypatch.setattr(
        site.fetcher.publishers,
        "continue_listings",
        lambda *_args, **_kwargs: generic_read,
    )
    options = (
        SourceReadOptions(protected_posting_urls=frozenset({generic_url}))
        if generic_outcome == "protected"
        else SourceReadOptions()
    )
    result = site.fetcher.continue_listings(
        "Acme",
        (generic, typed),
        root_url=site.root,
        handoff_scopes=(),
        options=options,
    )
    assert result.publisher_listing_receipts == ()
    assert result.detail_read_urls is not None
    assert site.detail_url(1) in result.detail_read_urls
    assert generic_url not in result.detail_read_urls
    assert any(
        listing.url == typed.url and listing.published_description for listing in result.listings
    )
    assert not result.complete and result.detail_coverage is None
    if generic_outcome == "protected":
        assert result.protected_posting_urls == (generic_url,)
        assert result.skip_reasons["protected_posting"] == 1
    else:
        assert result.skip_reasons["generic_" + generic_outcome] == 1
        if generic_outcome in {"failure", "hold"}:
            assert any(target.url == generic_url for target in result.deferred_targets)
        else:
            assert any(listing.url == generic_url for listing in result.listings)


@pytest.mark.parametrize("boundary", ["deadline", "exhausted"])
def test_an_unattempted_publisher_detail_retains_its_history_and_explicit_retry_allowance(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    site = _TaleoSite()
    site.pages = [[1], []]
    site.totals = [1, 1]
    site.failed = {1}
    first = site.fetcher.listings("Acme", site.root)
    [target] = first.deferred_targets
    assert target.detail_checkpoint is not None
    allowed = boundary == "deadline"
    target = replace(
        target,
        automatic_eligible=True,
        manual_detail_retry=allowed,
        detail_checkpoint=replace(
            target.detail_checkpoint, failed_attempts=(("1", MAX_SOURCE_READ_ATTEMPTS),)
        ),
    )
    site.failed.clear()
    checks = iter([False, True] if allowed else [False, False])
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: next(checks, True))

    interrupted = site.fetcher.continue_listings(
        "Acme", (target,), root_url=site.root, handoff_scopes=()
    )

    assert interrupted.detail.attempted == 0
    assert interrupted.attempted_target_urls == ()
    [retained] = interrupted.deferred_targets
    assert retained.detail_checkpoint is not None
    assert retained.detail_checkpoint.failed_attempts == (("1", MAX_SOURCE_READ_ATTEMPTS),)
    assert retained.automatic_eligible is allowed
    assert site.requests.count(target.url) == 1
    if not allowed:
        return
    monkeypatch.setattr(site.fetcher, "deadline_elapsed", lambda: False)

    resumed = site.fetcher.continue_listings(
        "Acme",
        (replace(retained, manual_detail_retry=True),),
        root_url=site.root,
        handoff_scopes=(),
    )

    assert resumed.detail.attempted == resumed.detail.read == 1
    assert resumed.attempted_target_urls == (target.url,)
    assert len(resumed.listings) == 1 and resumed.listings[0].published_description
    assert not resumed.deferred_targets
    assert site.requests.count(target.url) == 2


@pytest.mark.parametrize("boundary", ["pacing", "transport", "redirect", "retry"])
def test_publisher_detail_deadline_counts_only_a_dispatched_request(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    site = _FeedSite()
    url = site.url(1)
    redirected = site.url(2)
    original = site.answer
    dispatched: list[str] = []

    def answer(address: str, **_kwargs: object) -> httpx.Response:
        if address.endswith("/robots.txt"):
            return httpx.Response(200, request=httpx.Request("GET", address))
        if address == url:
            dispatched.append(address)
            if boundary == "transport":
                site.now += 4.0
                raise httpx.ReadTimeout(
                    "Synthetic physical request timeout", request=httpx.Request("GET", address)
                )
            if boundary == "redirect":
                return httpx.Response(
                    302, headers={"Location": redirected}, request=httpx.Request("GET", address)
                )
            if boundary == "retry":
                return httpx.Response(
                    429, headers={"Retry-After": "0"}, request=httpx.Request("GET", address)
                )
        return original(address)

    site.fetcher.client.getter = answer
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.transport.time.monotonic",
        lambda: site.now,
    )
    site.fetcher.client.get(site.root)
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS",
        1.0 if boundary == "pacing" else 3.0,
    )
    target = SourceContinuationTarget(
        url=url,
        source_url=site.root,
        label="Data Analyst 1",
        label_source="visible",
        structural_context="main",
        relations=("publisher-content",),
        deferred_reason="fxrecruiter_search_detail",
        automatic_eligible=True,
        manual_detail_retry=True,
        detail_checkpoint=SourceDetailCheckpoint(
            platform="fxrecruiter",
            board_url=url,
            pending_ids=("1",),
            listing_ids=("1",),
            listing_urls=(("1", url),),
            failed_attempts=(("1", 2),),
        ),
    )

    read = site.fetcher.continue_listings("Acme", (target,), root_url=site.root, handoff_scopes=())

    attempted = int(boundary != "pacing")
    assert len(dispatched) == attempted
    assert read.detail.attempted == attempted
    assert read.detail.interrupted == attempted and read.detail.failed == 0
    assert read.attempted_target_urls == ((url,) if attempted else ())
    assert read.skip_reasons == {"time_limit_reached": 1}
    [retained] = read.deferred_targets
    assert retained.detail_checkpoint is not None
    assert retained.detail_checkpoint.failed_attempts == (("1", 2),)
    assert retained.automatic_eligible
    if attempted:
        return
    site.fetcher.client.getter = lambda address, **_kwargs: original(address)
    monkeypatch.setattr(
        "clearscout.infrastructure.web_fetch.board_adapters.read_scope.MAX_PAGE_READ_SECONDS", 30.0
    )

    resumed = site.fetcher.continue_listings(
        "Acme",
        (replace(retained, manual_detail_retry=True),),
        root_url=site.root,
        handoff_scopes=(),
    )

    assert resumed.detail.attempted == resumed.detail.read == 1
    assert resumed.attempted_target_urls == (url,)
    assert len(resumed.listings) == 1 and resumed.listings[0].published_description
    assert site.requested.count(url) == 1


def test_hydrated_search_page_cursor_retains_exact_listing_and_completed_body_maps() -> None:
    site = _TaleoSite()
    site.fail_page = 2
    first = site.fetcher.listings("Acme", site.root)
    [cursor] = first.deferred_targets
    assert cursor.detail_checkpoint is not None
    expected = {str(number): site.detail_url(number) for number in (1, 2)}
    assert dict(cursor.detail_checkpoint.listing_urls) == expected
    assert dict(cursor.detail_checkpoint.completed) == expected
    assert first.detail_read_urls == tuple(expected.values())
    assert cursor.detail_checkpoint.protected == ()
    [initial_receipt] = first.publisher_listing_receipts
    assert dict(initial_receipt.listing_urls) == expected
    assert dict(initial_receipt.completed) == expected
    site.fail_page = None
    last = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    [final_receipt] = last.publisher_listing_receipts
    assert dict(final_receipt.listing_urls) == {
        str(number): site.detail_url(number) for number in (1, 2, 3)
    }
    assert dict(final_receipt.completed) == dict(final_receipt.listing_urls)
    assert last.raw_count == 3
    assert set(last.detail_read_urls or ()) == {site.detail_url(number) for number in (1, 2, 3)}
    assert last.detail.read == 1
    assert all(listing.published_description for listing in last.listings)


@pytest.mark.parametrize("part", ["tenant", "site", "locale"])
def test_search_merge_rejects_enumeration_receipts_from_another_owner(
    monkeypatch: pytest.MonkeyPatch,
    part: str,
) -> None:
    site = _TaleoSite()
    site.fail_page = 2
    first = site.fetcher.listings("Acme", site.root)
    [target] = first.deferred_targets
    site.fail_page = None
    readers = site.fetcher._continuation_readers()
    read = readers["taleo_search_page"]("Acme", target)
    [receipt] = read.publisher_listing_receipts
    root = {
        "tenant": site.root.replace("acme.taleo.net", "other.taleo.net"),
        "site": site.root.replace("/external/", "/other/"),
        "locale": site.root.replace("lang=en", "lang=fr"),
    }[part]
    forged = replace(read, publisher_listing_receipts=(replace(receipt, board_url=root),))
    monkeypatch.setattr(
        site.fetcher,
        "_continuation_readers",
        lambda: {**readers, "taleo_search_page": lambda _employer, _target: forged},
    )
    result = site.fetcher.continue_listings(
        "Acme", (target,), root_url=site.root, handoff_scopes=()
    )
    assert result.publisher_listing_receipts == ()
    assert result.detail_read_urls == read.detail_read_urls
    assert not result.complete and result.listing_coverage is None


@pytest.mark.parametrize(
    "case",
    [
        "unknown_platform",
        "malformed_root",
        "tenant",
        "site",
        "locale",
        "unmapped_body",
        "metadata",
        "protected",
    ],
)
def test_hydrated_search_merge_rejects_body_receipts_outside_declared_scope(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    site = _TaleoSite()
    site.fail_page = 2
    first = site.fetcher.listings("Acme", site.root)
    assert first.detail_read_urls is not None
    [original_target] = first.deferred_targets
    assert original_target.detail_checkpoint is not None
    site.fail_page = None
    readers = site.fetcher._continuation_readers()
    original = readers["taleo_search_page"]
    read = original("Acme", original_target)
    target = original_target
    if case == "unknown_platform":
        target = replace(
            target,
            deferred_reason="unknown_search_page",
            detail_checkpoint=replace(
                original_target.detail_checkpoint, platform="search:unknown_search_page"
            ),
        )
    elif case in {"malformed_root", "tenant", "site", "locale"}:
        root = {
            "malformed_root": site.origin + "/unrecognized",
            "tenant": site.root.replace("acme.taleo.net", "other.taleo.net"),
            "site": site.root.replace("/external/", "/other/"),
            "locale": site.root.replace("lang=en", "lang=fr"),
        }[case]
        target = replace(
            target,
            source_url=root,
            detail_checkpoint=replace(original_target.detail_checkpoint, board_url=root),
        )
    elif case == "unmapped_body":
        read = replace(read, detail_read_urls=(*first.detail_read_urls, site.detail_url(99)))
    elif case == "metadata":
        read = replace(
            read, listings=[replace(listing, published_description="") for listing in read.listings]
        )
    else:
        read = replace(read, protected_posting_urls=(site.detail_url(3),))
    monkeypatch.setattr(
        site.fetcher,
        "_continuation_readers",
        lambda: {**readers, target.deferred_reason: lambda _employer, _target: read},
    )
    result = site.fetcher.continue_listings(
        "Acme", (target,), root_url=site.root, handoff_scopes=()
    )
    assert result.detail_read_urls is None
    assert result.detail_coverage is None
    assert result.listing_coverage is None


def test_hydrated_search_changed_identity_url_keeps_old_success_and_owes_the_new_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _SearchSite("tbk")
    site.pages = [[1, 2], [2, 3], []]
    site.totals = [3, 3, 3]
    site.fail_page = 1
    first = site.fetcher.listings("Acme", site.root)
    old_url = site.detail_url(2)
    assert next(
        listing for listing in first.listings if listing.url == old_url
    ).published_description
    original_row = site.row
    new_url = old_url + "&revision=2"

    def row(number: int) -> dict[str, Any]:
        record = original_row(number)
        if number == 2:
            record["careerLink"] = {"href": new_url}
        return record

    monkeypatch.setattr(site, "row", row)
    site.failed_details = {2}
    site.fail_page = None
    read = site.fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=site.root, handoff_scopes=()
    )
    assert new_url in site.requested
    assert read.detail_read_urls is not None
    assert old_url not in read.detail_read_urls and new_url not in read.detail_read_urls
    assert {listing.url for listing in first.listings} == {site.detail_url(1), old_url}
    assert next(
        listing for listing in first.listings if listing.url == old_url
    ).published_description
    target = next(target for target in read.deferred_targets if target.url == new_url)
    assert not target.automatic_eligible
    assert target.detail_checkpoint is not None
    assert target.detail_checkpoint.pending_ids == ("2",)
    assert dict(target.detail_checkpoint.listing_urls)["2"] == new_url
    assert all(identity != "2" for identity, _url in target.detail_checkpoint.completed)
    assert not read.complete and read.detail_coverage is None


@pytest.mark.parametrize("kind", ["fxrecruiter", "darwinbox"])
@pytest.mark.parametrize("case", ["valid", "tenant", "company", "unknown_path", "invalid_id"])
def test_root_scoped_search_details_preserve_owned_recovery_and_refuse_unowned_addresses(
    kind: str, case: str
) -> None:
    from urllib.parse import urlsplit, urlunsplit

    from tests.loop.test_board_mappings_public_search import (
        _BODY,
        _BODY_FIELDS,
        _bootstrap_cases,
        _public_search_fetcher,
    )
    from tests.loop.test_fxrecruiter_reader import _FeedSite

    fetcher: BoardApiFetcher
    requests: list[Any]
    if kind == "fxrecruiter":
        site = _FeedSite()
        site.failed = {2}
        fetcher, source, requests = site.fetcher, site.root, site.requested
        first = fetcher.listings("Acme", source)
        site.failed.clear()
    else:
        source, _, _, records = _bootstrap_cases(kind)
        records[1].pop(_BODY_FIELDS[kind])
        fetcher, requests = _public_search_fetcher(kind, rows_override=records)
        first = fetcher.listings("Acme", source)
        records[1][_BODY_FIELDS[kind]] = _BODY
    [target] = first.deferred_targets
    assert target.detail_checkpoint is not None
    checkpoint = target.detail_checkpoint
    [identity] = checkpoint.pending_ids
    address = urlsplit(target.url)
    path = address.path
    if case == "tenant":
        address = address._replace(netloc=address.netloc.replace("acme.", "other."))
    elif case == "company":
        path = (
            path.replace("/main/", "/other/")
            if kind == "darwinbox"
            else path.replace("/jobs/", "/other/")
        )
    elif case == "unknown_path":
        path = "/unowned/" + path.rsplit("/", 1)[-1]
    elif case == "invalid_id":
        path = path.rsplit("/", 1)[0] + "/!bad"
    url = urlunsplit(address._replace(path=path))
    target = replace(
        target,
        url=url,
        detail_checkpoint=replace(
            checkpoint,
            board_url=url,
            listing_urls=tuple(
                (item, url if item == identity else owned)
                for item, owned in checkpoint.listing_urls
            ),
        ),
    )
    before = len(requests)
    read = fetcher.continue_listings("Acme", (target,), root_url=source, handoff_scopes=())
    if case == "valid":
        assert [(listing.requisition_id, listing.url) for listing in read.listings] == [
            (identity, url)
        ]
        assert read.detail.read == 1
        assert all(listing.published_description for listing in read.listings)
        assert not read.deferred_targets
    else:
        assert len(requests) == before
        assert not read.listings and read.detail_read_urls is None
        assert not read.complete


@pytest.mark.parametrize("case", ["legacy", "foreign_root", "unknown_platform"])
def test_legacy_hydrated_search_identity_prefix_is_untrusted_until_a_fresh_scan(case: str) -> None:
    site = _TaleoSite()
    site.fail_page = 2
    first = site.fetcher.listings("Acme", site.root)
    [target] = first.deferred_targets
    assert target.detail_checkpoint is not None
    legacy = replace(
        target, detail_checkpoint=replace(target.detail_checkpoint, listing_urls=(), completed=())
    )
    if case != "legacy":
        legacy = replace(
            target,
            detail_checkpoint=replace(
                target.detail_checkpoint,
                board_url=site.root.replace("acme.", "other.")
                if case == "foreign_root"
                else site.root,
                platform="search:unknown_search_page"
                if case == "unknown_platform"
                else target.detail_checkpoint.platform,
            ),
        )
    before = len(site.posts), site.detail_reads
    site.fail_page = None
    read = site.fetcher.continue_listings("Acme", (legacy,), root_url=site.root, handoff_scopes=())
    assert (len(site.posts), site.detail_reads) == before
    assert not read.listings
    assert read.detail_read_urls in {None, ()}
    assert read.detail_coverage is None
    assert not read.complete
    assert read.deferred_targets and not read.deferred_targets[0].automatic_eligible
    if case == "legacy":
        assert "Scan the source again" in read.note
        assert read.skip_reasons["publisher_listing_membership_missing"] == 1


def _phenom_page(reference: str) -> str:
    return (
        "<html><head><script>"
        'phApp.ddo = {"eventName":"pageLoad","refNum":"' + reference + '","siteType":"external"};'
        "</script></head><body><p>Openings</p></body></html>"
    )


class _PhenomWindow(ScriptedPage):
    """A widget whose bounded slices expose exact publisher listing identities."""

    root = "https://careers.acme.test/"

    def __init__(self, pages: dict[int, list[int]], total: int = 4) -> None:
        super().__init__(_phenom_page("ACMEUS"))
        self.pages = pages
        self.total = total
        self.requests: list[int] = []
        self.failed_offset: int | None = None

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        assert url == self.root + "widgets?ddoKey=refineSearch"
        assert body["refNum"] == "ACMEUS"
        offset = int(body["from"])
        self.requests.append(offset)
        if offset == self.failed_offset:
            raise httpx.ReadError("Synthetic unavailable widget page.")
        return {
            "refineSearch": {
                "totalHits": self.total,
                "data": {
                    "jobs": [
                        {
                            "jobId": f"REQ-{item}",
                            "title": "Data Analyst",
                            "applyUrl": self.root + f"job/{item}/data-analyst",
                        }
                        for item in self.pages.get(offset, [])
                    ]
                },
            }
        }


class _FlatPhenomWindow(_PhenomWindow):
    """A widget that slices one flat listing by the requested offset and size."""

    def __init__(self, items: list[int]) -> None:
        super().__init__({}, total=len(items))
        self.items = items
        self.sizes: list[int] = []

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        offset, size = int(body["from"]), int(body["size"])
        self.sizes.append(size)
        self.pages = {offset: self.items[offset : offset + size]}
        return super().post_json(url, body)


def test_phenom_board_of_several_hundred_certifies_in_three_widget_requests() -> None:
    fetcher = _FlatPhenomWindow(list(range(250)))
    read = fetcher.listings("Acme", fetcher.root)
    assert fetcher.requests == [0, 100, 200]
    assert read.complete
    assert read.raw_count == 250
    assert not read.deferred_targets


def test_phenom_stored_cursor_keeps_its_own_limit_under_a_larger_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = _FlatPhenomWindow(list(range(30)))
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    [stored] = fetcher.listings("Acme", fetcher.root).deferred_targets
    monkeypatch.undo()
    assert phenom.PHENOM_PAGE == 100
    assert "limit=10" in stored.url and "offset=10" in stored.url

    read = fetcher.continue_listings("Acme", (stored,), root_url=fetcher.root, handoff_scopes=())

    assert (fetcher.requests, fetcher.sizes) == ([0, 10], [10, 10])
    [cursor] = read.deferred_targets
    assert "limit=10" in cursor.url and "offset=20" in cursor.url


def test_phenom_successive_new_known_combinations_keep_automatic_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4], 4: [1, 3], 6: [2, 4], 8: [5, 6]}, total=12)
    current = fetcher.listings("Acme", fetcher.root)
    for offset, expected_count in ((2, 4), (4, 4), (6, 4), (8, 6)):
        current = fetcher.continue_listings(
            "Acme", current.deferred_targets, root_url=fetcher.root, handoff_scopes=()
        )
        assert current.raw_count == expected_count
        [cursor] = current.deferred_targets
        assert cursor.automatic_eligible
        assert f"offset={offset + 2}" in cursor.url
        assert cursor.detail_checkpoint is not None
        assert len(cursor.detail_checkpoint.page_fingerprints) == offset // 2 + 1
        assert not current.complete
        assert current.exhausted_outcome is None
        assert current.listing_coverage is None
        assert current.detail_coverage is None
        assert current.detail_read_urls is None
    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.listing_unstable
    assert cursor.detail_checkpoint.completed == ()
    assert set(cursor.detail_checkpoint.listing_ids) == {f"REQ-{item}" for item in range(1, 7)}
    assert fetcher.requests == [0, 2, 4, 6, 8]


def test_phenom_initial_new_known_combinations_reach_the_next_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 4)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4], 4: [1, 3], 6: [2, 4], 8: [5, 6]}, total=12)
    first = fetcher.listings("Acme", fetcher.root)
    assert fetcher.requests == [0, 2, 4, 6]
    assert first.raw_count == 4
    [cursor] = first.deferred_targets
    assert cursor.automatic_eligible
    assert "offset=8" in cursor.url
    assert cursor.detail_checkpoint is not None and cursor.detail_checkpoint.listing_unstable
    assert not first.complete
    assert first.listing_coverage is None
    assert first.detail_read_urls is None
    continued = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    assert continued.raw_count == 6
    assert continued.deferred_targets[0].automatic_eligible
    assert not continued.complete
    assert continued.detail_coverage is None
    assert continued.detail_read_urls is None
    assert fetcher.requests == [0, 2, 4, 6, 8]


@pytest.mark.parametrize("total", [None, 0, 6])
@pytest.mark.parametrize("initial", [False, True])
def test_phenom_known_combinations_require_an_actual_next_page_within_a_positive_total(
    monkeypatch: pytest.MonkeyPatch, total: int | None, initial: bool
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 3 if initial else 1)

    class BoundedWindow(_PhenomWindow):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if total is None:
                del payload["refineSearch"]["totalHits"]
            return payload

    fetcher = BoundedWindow({0: [1, 2], 2: [3, 4], 4: [1, 3], 6: [5, 6]}, total=total or 0)
    current = fetcher.listings("Acme", fetcher.root)
    if not initial:
        for _ in range(2):
            assert current.deferred_targets[0].automatic_eligible
            current = fetcher.continue_listings(
                "Acme", current.deferred_targets, root_url=fetcher.root, handoff_scopes=()
            )
    assert fetcher.requests == [0, 2, 4]
    assert current.raw_count == 4
    [cursor] = current.deferred_targets
    assert not cursor.automatic_eligible
    assert "offset=6" in cursor.url
    assert not current.complete
    assert current.exhausted_outcome is None
    assert current.listing_coverage is None
    assert current.detail_read_urls is None


@pytest.mark.parametrize(
    "blocked", ["reordered-repeat", "duplicate", "url-conflict", "manual", "unseeded"]
)
def test_phenom_known_page_guards_keep_the_actual_next_cursor_manual(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 2)

    class GuardedWindow(_PhenomWindow):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if body["from"] == 4 and blocked == "url-conflict":
                payload["refineSearch"]["data"]["jobs"][0]["applyUrl"] += "?different=1"
            return payload

    page = [2, 1] if blocked == "reordered-repeat" else [1, 1] if blocked == "duplicate" else [1, 3]
    fetcher = GuardedWindow({0: [1, 2], 2: [3, 4], 4: page, 6: [5, 6]}, total=8)
    first = fetcher.listings("Acme", fetcher.root)
    [target] = first.deferred_targets
    if blocked == "manual":
        target = replace(target, automatic_eligible=False)
    elif blocked == "unseeded":
        target = replace(target, detail_checkpoint=None)
    result = fetcher.continue_listings("Acme", (target,), root_url=fetcher.root, handoff_scopes=())
    assert fetcher.requests == [0, 2, 4]
    [cursor] = result.deferred_targets
    assert not cursor.automatic_eligible
    assert "offset=6" in cursor.url
    assert not result.complete
    assert result.exhausted_outcome is None
    assert result.listing_coverage is None
    assert result.detail_read_urls is None


@pytest.mark.parametrize("offset", ["-2", "invalid"])
def test_phenom_invalid_known_page_cursor_is_rejected_without_a_widget_request(
    monkeypatch: pytest.MonkeyPatch, offset: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 2)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4], 4: [1, 3]}, total=8)
    first = fetcher.listings("Acme", fetcher.root)
    [target] = first.deferred_targets
    target = replace(target, url=target.url.replace("offset=4", f"offset={offset}"))
    result = fetcher.continue_listings("Acme", (target,), root_url=fetcher.root, handoff_scopes=())
    assert fetcher.requests == [0, 2]
    assert result.raw_count == 0
    assert not result.listings
    assert not result.complete
    assert result.skip_reasons["invalid_phenom_cursor"] == 1
    assert result.deferred_targets == ()


@pytest.mark.parametrize("blocked", ["deadline", "held", "failed"])
def test_phenom_new_known_page_progress_preserves_an_interrupted_next_read(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 2)

    class InterruptedWindow(_PhenomWindow):
        stopped = False

        @staticmethod
        def deadline_elapsed() -> bool:
            return InterruptedWindow.stopped and blocked == "deadline"

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            if self.stopped:
                if blocked == "held":
                    raise HostHeldError(url, ready_at=100)
                raise httpx.ReadError("Synthetic unavailable widget page.")
            return super().post_json(url, body)

    fetcher = InterruptedWindow({0: [1, 2], 2: [3, 4], 4: [1, 3], 6: [5, 6]}, total=8)
    first = fetcher.listings("Acme", fetcher.root)
    progress = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    [target] = progress.deferred_targets
    assert target.automatic_eligible
    assert "offset=6" in target.url
    InterruptedWindow.stopped = True
    interrupted = fetcher.continue_listings(
        "Acme", (target,), root_url=fetcher.root, handoff_scopes=()
    )
    assert fetcher.requests == [0, 2, 4]
    assert interrupted.raw_count == 0
    assert not interrupted.listings
    [cursor] = interrupted.deferred_targets
    assert cursor.url == target.url
    assert cursor.detail_checkpoint == target.detail_checkpoint
    assert cursor.automatic_eligible == target.automatic_eligible
    assert (
        interrupted.skip_reasons[
            {"deadline": "time_limit_reached", "held": "host_held", "failed": "fetch_failed"}[
                blocked
            ]
        ]
        == 1
    )
    assert not interrupted.complete
    assert interrupted.detail_read_urls is None


@pytest.mark.parametrize(("initial_total", "later_total"), [(6, 6), (6, 8), (8, 6)])
def test_phenom_partial_overlap_advances_declared_pages_without_certifying_coverage(
    monkeypatch: pytest.MonkeyPatch, initial_total: int, later_total: int
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    fetcher = _PhenomWindow({0: [1, 2], 2: [2, 3], 4: [4, 5], 6: [6, 7]}, total=initial_total)
    first = fetcher.listings("Acme", fetcher.root)
    fetcher.total = later_total
    overlap = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    assert overlap.raw_count == 3
    assert [listing.requisition_id for listing in overlap.listings] == ["REQ-2", "REQ-3"]
    assert overlap.deferred_targets[0].automatic_eligible
    assert "offset=4" in overlap.deferred_targets[0].url
    checkpoint = overlap.deferred_targets[0].detail_checkpoint
    assert checkpoint is not None and checkpoint.listing_unstable
    current = overlap
    for _ in range(3):
        if not current.deferred_targets[0].automatic_eligible:
            break
        current = fetcher.continue_listings(
            "Acme", current.deferred_targets, root_url=fetcher.root, handoff_scopes=()
        )
        assert not current.complete
        assert current.exhausted_outcome is None
        assert current.listing_coverage is None
        assert current.detail_coverage is None
        assert current.detail_read_urls is None
    else:
        raise AssertionError(
            "The declared bounded widget pages did not reach their manual boundary."
        )
    assert current.raw_count == (5 if later_total == 6 else 7)
    checkpoint = current.deferred_targets[0].detail_checkpoint
    assert checkpoint is not None
    assert set(checkpoint.listing_ids) == {
        f"REQ-{number}" for number in range(1, int(current.raw_count) + 1)
    }
    assert checkpoint.completed == ()
    assert checkpoint.listing_unstable
    assert current.skip_reasons["phenom_listing_unstable"] == 1
    assert fetcher.requests == ([0, 2, 4] if later_total == 6 else [0, 2, 4, 6])


def test_phenom_initial_whole_repeat_holds_the_actual_next_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 3)
    fetcher = _PhenomWindow({0: [1, 2], 2: [1, 2], 4: [3, 4]}, total=8)
    result = fetcher.listings("Acme", fetcher.root)
    assert fetcher.requests == [0, 2]
    assert result.raw_count == 2
    assert {listing.requisition_id for listing in result.listings} == {"REQ-1", "REQ-2"}
    assert not result.complete
    assert result.exhausted_outcome is None
    [cursor] = result.deferred_targets
    assert not cursor.automatic_eligible
    assert "offset=4" in cursor.url
    assert cursor.detail_checkpoint is not None and cursor.detail_checkpoint.listing_unstable


@pytest.mark.parametrize("repeated_identity", [False, True])
def test_phenom_identity_url_collision_remains_manual_despite_new_records(
    monkeypatch: pytest.MonkeyPatch,
    repeated_identity: bool,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)

    class ConflictingWindow(_PhenomWindow):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if body["from"] == 2:
                payload["refineSearch"]["data"]["jobs"][0]["applyUrl"] += "?different=1"
            return payload

    fetcher = ConflictingWindow({0: [1, 2], 2: [3, 3] if repeated_identity else [2, 3]}, total=6)
    first = fetcher.listings("Acme", fetcher.root)
    read = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    assert fetcher.requests == [0, 2]
    assert read.raw_count == 3
    assert not read.complete
    assert read.exhausted_outcome is None
    assert read.deferred_targets and not read.deferred_targets[0].automatic_eligible
    assert read.skip_reasons["phenom_listing_unstable"] == 1


def test_phenom_initial_overlap_keeps_the_next_slice_automatic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 2)
    fetcher = _PhenomWindow({0: [1, 2], 2: [2, 3], 4: [4, 5]}, total=6)
    first = fetcher.listings("Acme", fetcher.root)
    assert fetcher.requests == [0, 2]
    assert first.raw_count == 3
    assert not first.complete
    assert first.exhausted_outcome is None
    [cursor] = first.deferred_targets
    assert cursor.automatic_eligible
    assert "offset=4" in cursor.url
    assert cursor.detail_checkpoint is not None and cursor.detail_checkpoint.listing_unstable


@pytest.mark.parametrize("legacy", [False, True])
def test_phenom_manual_or_unseeded_cursor_stays_manual_after_new_identities(
    monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4], 4: [5, 6]}, total=6)
    first = fetcher.listings("Acme", fetcher.root)
    [target] = first.deferred_targets
    target = (
        replace(target, detail_checkpoint=None)
        if legacy
        else replace(target, automatic_eligible=False)
    )
    result = fetcher.continue_listings("Acme", (target,), root_url=fetcher.root, handoff_scopes=())
    assert fetcher.requests == [0, 2]
    assert [listing.requisition_id for listing in result.listings] == ["REQ-3", "REQ-4"]
    assert not result.complete
    assert result.exhausted_outcome is None
    assert result.deferred_targets and not result.deferred_targets[0].automatic_eligible
    assert "offset=4" in result.deferred_targets[0].url
    assert result.detail_coverage is None


@pytest.mark.parametrize(
    ("later", "total"), [([2, 3], 4), ([1, 2], 4), ([3], 4), ([], 4), ([3, 4], 5)]
)
def test_phenom_inconsistent_windows_retain_recovery_without_certifying_membership(
    monkeypatch: pytest.MonkeyPatch, later: list[int], total: int
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    fetcher = _PhenomWindow({0: [1, 2], 2: later}, total=4)
    first = fetcher.listings("Acme", fetcher.root)
    fetcher.total = total
    result = fetcher.continue_listings(
        "Acme", first.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    assert fetcher.requests == [0, 2]
    assert result.raw_count == len({1, 2, *later})
    assert not result.complete
    assert result.exhausted_outcome is None
    assert result.deferred_targets
    if len(later) < 2 or not set(later).difference({1, 2}) or 2 + len(later) >= total:
        assert not result.deferred_targets[0].automatic_eligible
    else:
        assert result.deferred_targets[0].automatic_eligible
    assert result.detail_read_urls is None
    assert result.detail_coverage is None
    assert result.listing_coverage is None


@pytest.mark.parametrize(
    "scope", ["root", "path", "reference", "scheme", "endpoint", "body", "legacy"]
)
def test_phenom_checkpoint_scope_cannot_certify_foreign_or_unseeded_membership(
    monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    first = fetcher.listings("Acme", fetcher.root)
    [target] = first.deferred_targets
    checkpoint = target.detail_checkpoint
    assert checkpoint is not None
    if scope == "root":
        target = replace(
            target, detail_checkpoint=replace(checkpoint, board_url="https://other.acme.test/")
        )
    elif scope == "path":
        target = replace(
            target, detail_checkpoint=replace(checkpoint, board_url=fetcher.root + "other")
        )
    elif scope == "scheme":
        target = replace(target, url=target.url.replace("https:", "http:"))
    elif scope == "endpoint":
        target = replace(target, url=target.url.replace("/widgets", "/other"))
    elif scope == "body":
        target = replace(
            target, detail_checkpoint=replace(checkpoint, completed=checkpoint.listing_urls)
        )
    elif scope == "reference":
        target = replace(
            target, detail_checkpoint=replace(checkpoint, platform="search:phenom_page:OTHER")
        )
    else:
        target = replace(target, detail_checkpoint=None)
    read = fetcher.continue_listings("Acme", (target,), root_url=fetcher.root, handoff_scopes=())
    assert not read.complete
    assert read.exhausted_outcome is None
    assert read.detail_coverage is None
    if scope == "legacy":
        assert fetcher.requests == [0, 2]
        assert read.raw_count == 2
        assert read.deferred_targets
        assert not read.deferred_targets[0].automatic_eligible
    else:
        assert fetcher.requests == [0]
        assert not read.listings
        assert read.skip_reasons["phenom_scope_mismatch"] == 1


def test_phenom_initial_later_page_failure_retains_listings_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 2)
    fetcher = _PhenomWindow({0: [1, 2], 2: [3, 4]}, total=4)
    fetcher.failed_offset = 2
    result = fetcher.listings("Acme", fetcher.root)
    assert fetcher.requests == [0, 2]
    assert {listing.requisition_id for listing in result.listings} == {"REQ-1", "REQ-2"}
    assert result.raw_count == 2
    assert not result.complete
    assert result.deferred_targets
    assert "offset=2" in result.deferred_targets[0].url
    assert result.deferred_targets[0].detail_checkpoint is not None
    fetcher.failed_offset = None
    resumed = fetcher.continue_listings(
        "Acme", result.deferred_targets, root_url=fetcher.root, handoff_scopes=()
    )
    assert resumed.raw_count == 4
    assert fetcher.requests == [0, 2, 2]


@pytest.mark.parametrize("terminal_ids", [[3, 4], [3, 4, 5]])
def test_phenom_continuations_retain_distinct_listing_scope_without_body_evidence(
    monkeypatch: pytest.MonkeyPatch, terminal_ids: list[int]
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    pages = {0: [1, 2], 2: terminal_ids[:2]}
    if len(terminal_ids) == 3:
        pages[4] = terminal_ids[2:]
    fetcher = _PhenomWindow(pages, total=2 + len(terminal_ids))
    first = fetcher.listings("Acme", fetcher.root)
    [cursor] = first.deferred_targets

    assert cursor.detail_checkpoint is not None
    assert cursor.detail_checkpoint.platform == "search:phenom_page:ACMEUS"
    assert cursor.detail_checkpoint.completed == ()
    assert dict(cursor.detail_checkpoint.listing_urls) == {
        f"REQ-{item}": fetcher.root + f"job/{item}/data-analyst" for item in (1, 2)
    }
    current = first
    for _ in range(3):
        if not current.deferred_targets:
            break
        current = fetcher.continue_listings(
            "Acme", current.deferred_targets, root_url=fetcher.root, handoff_scopes=()
        )
    else:
        raise AssertionError("The coherent Phenom pages did not drain.")

    assert current.raw_count == 2 + len(terminal_ids)
    assert current.detail.read == 0
    assert current.detail_read_urls is None
    assert current.detail_coverage is None
    assert current.listing_coverage is None
    assert all(not listing.published_description for listing in current.listings)
    assert fetcher.requests == ([0, 2] if len(terminal_ids) == 2 else [0, 2, 4])


@pytest.mark.parametrize("page_path", ["global/en/home", "ca/fr/search-results"])
def test_phenom_uses_the_publisher_job_route_under_its_locale(page_path: str) -> None:
    page_url = f"https://careers.acme.test/{page_path}"
    page = _phenom_page("ACMEUS").replace(
        "</head>",
        '<script>phApp.urlMap = {"job":"job/:jobSeqNo/:title","home":"home",'
        '"search-results":"search-results"};</script>'
        f'<link rel="canonical" href="{page_url}"></head>',
    )

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {
                "refineSearch": {
                    "totalHits": 2,
                    "data": {
                        "jobs": [
                            {"jobId": "REQ-1", "jobSeqNo": "ACME/1", "title": "Data & Care"},
                            {
                                "jobId": "REQ-2",
                                "jobSeqNo": "ACME2",
                                "title": "Explicit Role",
                                "applyUrl": "https://apply.acme.test/role/2",
                            },
                        ]
                    },
                }
            }

    read = ScriptedPhenom(page).listings("Acme", page_url)

    assert read.complete is True
    assert [listing.url for listing in read.listings] == [
        f"https://careers.acme.test/{page_path.rsplit('/', 1)[0]}/job/ACME%2F1/Data%20%26%20Care",
        "https://apply.acme.test/role/2",
    ]
    assert [listing.requisition_id for listing in read.listings] == ["REQ-1", "REQ-2"]


@pytest.mark.parametrize("source_suffix", ["", "/"])
@pytest.mark.parametrize(
    ("canonical", "accepted"),
    [
        ("https://careers.acme.test", True),
        ("https://careers.acme.test/", True),
        ("https://elsewhere.test", False),
        ("http://careers.acme.test", False),
        ("https://careers.acme.test:8443", False),
        ("https://careers.acme.test/unknown", False),
    ],
)
def test_phenom_host_root_routes_keep_exact_identity_across_pages(
    source_suffix: str, canonical: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 2)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    root = "https://careers.acme.test" + source_suffix
    page = _phenom_page("ACMEUS").replace(
        "</head>",
        '<script>phApp.urlMap = {"job":"job/:jobSeqNo/:title","home":"home"};</script>'
        f'<link rel="canonical" href="{canonical}"></head>',
    )
    requested: list[int] = []
    records = [
        {"jobId": "P-1", "jobSeqNo": "ACME/1", "title": "Data & Care"},
        {"jobId": "P-2", "title": "Explicit Role", "applyUrl": "https://apply.acme.test/2"},
        {"jobId": "P-3", "jobSeqNo": "ACME3", "title": "Care Analyst"},
    ]

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            assert url == "https://careers.acme.test/widgets?ddoKey=refineSearch"
            offset = int(body["from"])
            requested.append(offset)
            return {
                "refineSearch": {"totalHits": 3, "data": {"jobs": records[offset : offset + 2]}}
            }

    fetcher = ScriptedPhenom(page)
    read = fetcher.listings("Acme", root)
    assert not read.complete
    if not accepted:
        assert [listing.url for listing in read.listings] == ["https://apply.acme.test/2"]
        assert read.skip_reasons["missing_required_posting_identity"] == 1
        return
    assert [listing.url for listing in read.listings] == [
        "https://careers.acme.test/job/ACME%2F1/Data%20%26%20Care",
        "https://apply.acme.test/2",
    ]
    continued = fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url=root, handoff_scopes=()
    )
    assert [listing.url for listing in continued.listings] == [
        "https://careers.acme.test/job/ACME3/Care%20Analyst"
    ]
    assert [listing.requisition_id for listing in (*read.listings, *continued.listings)] == [
        "P-1",
        "P-2",
        "P-3",
    ]
    assert continued.deferred_targets == ()
    assert not continued.skip_reasons
    assert requested == [0, 2]


@pytest.mark.parametrize("usable_route", [True, False], ids=["publisher-route", "unreadable"])
def test_phenom_continuation_keeps_the_route_and_mapping_failures(
    monkeypatch: pytest.MonkeyPatch, usable_route: bool
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)
    page_url = "https://careers.acme.test/global/en/home"
    page = _phenom_page("ACMEUS").replace(
        "</head>",
        (
            '<script>phApp.urlMap = {"job":"job/:jobSeqNo/:title","home":"home"};</script>'
            if usable_route
            else ""
        )
        + f'<link rel="canonical" href="{page_url}"></head>',
    )

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            start = int(body["from"])
            return {
                "refineSearch": {
                    "totalHits": 23,
                    "data": {
                        "jobs": [
                            {
                                "jobId": f"REQ-{index}",
                                "jobSeqNo": f"ACME{index}",
                                "title": f"Role {index}",
                            }
                            for index in range(start, min(start + 10, 23))
                        ]
                    },
                }
            }

    fetcher = ScriptedPhenom(page)
    read = fetcher.listings("Acme", page_url)
    assert len(read.deferred_targets) == 1
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    fetcher.page = ""
    continued = fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url=page_url, handoff_scopes=()
    )
    assert continued.raw_count == (20 if usable_route else 10)
    if not usable_route:
        assert "offset=0" in read.deferred_targets[0].url
        assert read.exhausted_outcome is None
        assert continued.listings == []
        assert continued.skip_reasons == {"missing_required_posting_identity": 10}
        assert continued.deferred_targets == ()
        assert continued.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
        assert "posting link" in continued.note
        return
    assert len(continued.deferred_targets) == 1
    assert continued.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    terminal = fetcher.continue_listings(
        "Acme", continued.deferred_targets, root_url=page_url, handoff_scopes=()
    )
    assert terminal.raw_count == 23
    assert terminal.deferred_targets == ()
    assert [listing.url for listing in terminal.listings] == [
        f"https://careers.acme.test/global/en/job/ACME{index}/Role%20{index}"
        for index in range(20, 23)
    ]
    assert all(listing.employer == "Acme" for listing in terminal.listings)
    assert terminal.skip_reasons == {}
    assert terminal.outcome is SourceReadOutcome.PUBLISHER_LINKED_PAGES


@pytest.mark.parametrize(
    ("assignment", "canonical"),
    [
        ("", "https://careers.acme.test/global/en/home"),
        ('phApp.urlMap = {"job":', "https://careers.acme.test/global/en/home"),
        ('phApp.urlMap = {"job":[]};', "https://careers.acme.test/global/en/home"),
        (
            'phApp.urlMap = {"job":"https://elsewhere.test/:jobId"};',
            "https://careers.acme.test/global/en/home",
        ),
        (
            'phApp.urlMap = {"job":"//elsewhere.test/:jobId"};',
            "https://careers.acme.test/global/en/home",
        ),
        (
            'phApp.urlMap = {"job":"../job/:jobId/:title"};',
            "https://careers.acme.test/global/en/home",
        ),
        ('phApp.urlMap = {"job":"job/:title"};', "https://careers.acme.test/global/en/home"),
        (
            'phApp.urlMap = {"job":"job/:missing/:title"};',
            "https://careers.acme.test/global/en/home",
        ),
        ('phApp.urlMap = {"job":"job/:jobId/:title"};', "https://elsewhere.test/global/en/home"),
        ('phApp.urlMap = {"job":"job/:jobId/:title"};', "https://[invalid/global/en/home"),
    ],
)
def test_phenom_does_not_invent_an_identity_from_an_unusable_route(
    assignment: str, canonical: str
) -> None:
    assignment = assignment.replace('{"job":', '{"home":"home","job":')
    page = _phenom_page("ACMEUS").replace(
        "</head>", f'<script>{assignment}</script><link rel="canonical" href="{canonical}"></head>'
    )

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {
                "refineSearch": {
                    "totalHits": 1,
                    "data": {"jobs": [{"jobId": "REQ-1", "title": "Data Analyst"}]},
                }
            }

    read = ScriptedPhenom(page).listings("Acme", "https://careers.acme.test/global/en/home")

    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert read.raw_count == 1
    assert read.listings == []
    assert read.skip_reasons == {"missing_required_posting_identity": 1}


@pytest.mark.parametrize("canonical", ["", "https://careers.acme.test/global/en"])
def test_phenom_keeps_an_ambiguous_locale_base_unreadable(canonical: str) -> None:
    page = _phenom_page("ACMEUS").replace(
        "</head>",
        '<script>phApp.urlMap = {"job":"job/:jobId/:title","home":"home"};</script>'
        + (f'<link rel="canonical" href="{canonical}">' if canonical else "")
        + "</head>",
    )

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {
                "refineSearch": {
                    "totalHits": 1,
                    "data": {"jobs": [{"jobId": "REQ-1", "title": "Data Analyst"}]},
                }
            }

    read = ScriptedPhenom(page).listings("Acme", "https://careers.acme.test/global/en")

    assert read.listings == []
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert read.raw_count == 1


@pytest.mark.parametrize("blocked", ["held", "failed", "deadline"])
def test_phenom_continuation_counts_only_observed_publisher_records(
    monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_MAX_PAGES", 1)

    class ScriptedPhenom(ScriptedPage):
        stopped = False

        @staticmethod
        def deadline_elapsed() -> bool:
            return ScriptedPhenom.stopped and blocked == "deadline"

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            if self.stopped:
                if blocked == "held":
                    raise HostHeldError(url, ready_at=100)
                raise httpx.ReadError("Scripted unread page")
            return {
                "refineSearch": {
                    "totalHits": 20,
                    "data": {
                        "jobs": [
                            {"jobId": f"REQ-{index}", "title": f"Role {index}"}
                            for index in range(10)
                        ]
                    },
                }
            }

    fetcher = ScriptedPhenom(_phenom_page("ACMEUS"))
    read = fetcher.listings("Acme", "https://careers.acme.test/")
    ScriptedPhenom.stopped = True
    continued = fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url="https://careers.acme.test/", handoff_scopes=()
    )

    assert continued.raw_count == 0
    assert continued.listings == []
    assert continued.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    assert len(continued.deferred_targets) == 1
    assert continued.skip_reasons == {
        {"held": "host_held", "failed": "fetch_failed", "deadline": "time_limit_reached"}[
            blocked
        ]: 1
    }


def _icims_page() -> str:
    return (
        "<html><head>"
        '<link rel="stylesheet" href="https://cdn.jibecdn.com/prod/acme/latest/main.css">'
        "</head><body><p>Openings</p></body></html>"
    )


def test_a_career_site_on_an_employers_own_domain_is_named_by_its_markup() -> None:
    """Phenom and iCIMS both run on the employer's domain, so the page is the evidence."""
    assert identify_board("https://careers.acme.test/") is None

    phenom = identify_board(_phenom_page("ACMEUS"))
    assert phenom is not None
    assert phenom[0] == "phenom"
    assert phenom[1].group("ref") == "ACMEUS"

    icims = identify_board(_icims_page())
    assert icims is not None
    assert icims[0] == "icims"

    # A page merely saying either word is not a career site on either platform.
    assert identify_board("<p>We use Phenom and iCIMS to hire. Reference number: 12.</p>") is None


def test_phenom_listings_are_read_and_paged_without_a_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parse the Phenom widget payload, and read it at the host the page came from."""
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    requests: list[tuple[str, int]] = []

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            start = int(body["from"])
            requests.append((url, start))
            jobs = [
                {
                    "jobId": f"REQ-{index}",
                    "jobSeqNo": f"ACME{index}EXTERNAL",
                    "title": f"Role {index}",
                    "cityStateCountry": "Springfield, IL, United States",
                    "applyUrl": f"https://careers.acme.test/job/{index}/apply",
                }
                for index in range(start, min(start + 10, 12))
            ]
            if start == 0:
                jobs.append({"title": "Unlinked Role", "jobId": "REQ-X"})
            return {"refineSearch": {"totalHits": 12, "data": {"jobs": jobs}}}

    read = ScriptedPhenom(_phenom_page("ACMEUS")).listings("Acme", "https://careers.acme.test/")

    assert read is not None
    assert read.complete is False
    assert read.skip_reasons == {"missing_required_posting_identity": 1}
    listings = read.listings
    assert [item.title for item in listings] == [f"Role {index}" for index in range(12)]

    first = listings[0]
    assert first.employer == "Acme"
    assert first.url == "https://careers.acme.test/job/0/apply"
    assert first.location == "Springfield, IL, United States"
    assert first.requisition_id == "REQ-0"

    assert requests == [
        ("https://careers.acme.test/widgets?ddoKey=refineSearch", 0),
        ("https://careers.acme.test/widgets?ddoKey=refineSearch", 10),
    ]


@pytest.mark.parametrize("stated_total", [0, 12], ids=["terminal-empty-page", "stated-total"])
def test_a_nonempty_phenom_board_without_usable_identities_is_an_incomplete_read(
    monkeypatch: pytest.MonkeyPatch, stated_total: int
) -> None:
    """Keep unreadable records distinct from a board with no openings."""
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    requests: list[int] = []

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            start = int(body["from"])
            requests.append(start)
            jobs = [
                {"title": f"Unlinked Role {index}", "jobId": f"REQ-{index}"}
                for index in range(start, min(start + 10, 12))
            ]
            return {"refineSearch": {"totalHits": stated_total, "data": {"jobs": jobs}}}

    read = ScriptedPhenom(_phenom_page("ACMEUS")).listings("Acme", "https://careers.acme.test/")

    assert read is not None
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.NO_SUPPORTED_REPRESENTATION
    assert read.listings == []
    assert read.raw_count == 12
    assert read.skip_reasons == {"missing_required_posting_identity": 12}
    assert read.deferred_targets == ()
    assert "12" in read.note
    assert "title and posting link" in read.note
    assert "Try adding an individual posting link" in read.note
    assert requests == ([0, 10] if stated_total else [0, 10, 20])


def test_a_phenom_board_with_no_openings_is_a_complete_empty_read() -> None:
    """An empty job list remains evidence that the board has no openings."""

    class ScriptedPhenom(ScriptedPage):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return {"refineSearch": {"totalHits": 0, "data": {"jobs": []}}}

    read = ScriptedPhenom(_phenom_page("ACMEUS")).listings("Acme", "https://careers.acme.test/")

    assert read is not None
    assert read.complete is True
    assert read.outcome is SourceReadOutcome.ATS_COMPLETE
    assert read.listings == []
    assert read.raw_count == 0
    assert read.listing_pages == 0
    assert read.skip_reasons == {}
    assert read.deferred_targets == ()
    assert read.note == ""


def test_icims_listings_are_read_at_the_address_the_career_page_resolved_to() -> None:
    """Parse the iCIMS career-site payload, and follow the host the page redirected to."""
    requests: list[str] = []

    class ScriptedIcims(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requests.append(url)
            page = int(url.split("page=")[1].split("&", maxsplit=1)[0])
            first = (page - 1) * 20
            jobs = [
                {
                    "data": {
                        "title": f"Role {index}",
                        "full_location": "Springfield, IL",
                        "req_id": f"REQ-{index}",
                        "apply_url": f"https://jobs.acme.test/jobs/{index}/apply",
                    }
                }
                for index in range(first, min(first + 20, 22))
            ]
            if page == 1:
                jobs.append({"data": {"title": "Unlinked Role", "req_id": "REQ-X"}})
            return {"totalCount": 22, "jobs": jobs}

    # `get_text` prefixes the address the page was finally served from.
    page = f"https://jobs.acme.test/careers\n{_icims_page()}"
    read = ScriptedIcims(page).listings("Acme", "https://www.acme.test/careers")

    assert read is not None
    assert read.complete is True
    assert [item.title for item in read.listings] == [f"Role {index}" for index in range(22)]

    first = read.listings[0]
    assert first.employer == "Acme"
    assert first.url == "https://jobs.acme.test/jobs/0/job?in_iframe=1"
    assert first.location == "Springfield, IL"
    assert first.requisition_id == "REQ-0"

    assert requests == [
        "https://jobs.acme.test/api/jobs?page=1&limit=20",
        "https://jobs.acme.test/api/jobs?page=2&limit=20",
    ]
    assert read.listing_pages == 2


@pytest.mark.parametrize("payload", [{"items": []}, {}, {"jobs": None}, ["job"]])
def test_an_icims_api_answer_of_another_shape_is_refused_rather_than_read_as_empty(
    payload: Any,
) -> None:
    class ScriptedIcims(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return payload

    page = f"https://jobs.acme.test/careers\n{_icims_page()}"

    with pytest.raises(ValueError, match="iCIMS"):
        ScriptedIcims(page).listings("Acme", "https://www.acme.test/careers")


def test_an_icims_api_stating_no_jobs_is_a_complete_empty_board() -> None:
    class ScriptedIcims(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return {"jobs": [], "totalCount": 0}

    page = f"https://jobs.acme.test/careers\n{_icims_page()}"
    read = ScriptedIcims(page).listings("Acme", "https://www.acme.test/careers")

    assert read is not None
    assert read.complete is True
    assert read.listings == []


def test_icims_page_ceiling_carries_the_next_page_into_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(icims, "ICIMS_MAX_PAGES", 1)

    class ScriptedIcims(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            page = int(url.split("page=")[1].split("&", maxsplit=1)[0])
            first = (page - 1) * 20
            return {
                "totalCount": 40,
                "jobs": [
                    {
                        "data": {
                            "title": f"Role {index}",
                            "apply_url": f"https://jobs.acme.test/jobs/{index}/apply",
                        }
                    }
                    for index in range(first, first + 20)
                ],
            }

    fetcher = ScriptedIcims(f"https://jobs.acme.test/careers\n{_icims_page()}")
    read = fetcher.listings("Acme", "https://www.acme.test/careers")

    assert read is not None
    assert read.complete is False
    assert len(read.deferred_targets) == 1
    assert "page=2" in read.deferred_targets[0].url

    continued = fetcher.continue_listings(
        "Acme", read.deferred_targets, root_url="https://www.acme.test/careers", handoff_scopes=()
    )

    assert [listing.title for listing in continued.listings] == [
        f"Role {index}" for index in range(20, 40)
    ]
    assert continued.deferred_targets == ()


@pytest.mark.parametrize("platform", ["icims", "phenom"])
@pytest.mark.parametrize(
    ("completed_pages", "missing_identity"),
    [(0, False), (2, False), (2, True)],
    ids=["first-page", "third-page", "third-page-with-mapping-skips"],
)
def test_a_publisher_wait_keeps_read_board_pages_and_the_exact_manual_cursor(
    monkeypatch: pytest.MonkeyPatch, platform: str, completed_pages: int, missing_identity: bool
) -> None:
    monkeypatch.setattr(phenom, "PHENOM_PAGE", 10)
    page_size = 20 if platform == "icims" else 10
    held_offset = completed_pages * page_size
    offsets: list[int] = []

    class HeldPage(ScriptedPage):
        held = True

        def answer(self, url: str, offset: int) -> Any:
            offsets.append(offset)
            if self.held and offset == held_offset:
                raise HostHeldError(url, ready_at=100)
            jobs = [
                {
                    "jobId": f"REQ-{index}",
                    "title": f"Role {index}",
                    "applyUrl": f"https://careers.acme.test/job/{index}/apply",
                }
                for index in range(offset, offset + page_size)
            ]
            if self.held and missing_identity:
                jobs[-1]["title"] = ""
            if platform == "phenom":
                return {"refineSearch": {"totalHits": 30, "data": {"jobs": jobs}}}
            return {
                "totalCount": 60,
                "jobs": [
                    {"data": {"title": job["title"], "apply_url": job["applyUrl"]}} for job in jobs
                ],
            }

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return self.answer(url, (page - 1) * page_size)

        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            return self.answer(url, int(body["from"]))

    root_url = "https://careers.acme.test/"
    markup = _phenom_page("ACMEUS")
    if platform == "icims":
        root_url = "https://www.acme.test/careers"
        markup = f"https://jobs.acme.test/careers\n{_icims_page()}"
    fetcher = HeldPage(markup)

    read = fetcher.listings("Acme", root_url)

    assert read.raw_count == held_offset
    assert [listing.title for listing in read.listings] == [
        f"Role {index}"
        for index in range(held_offset)
        if not missing_identity or (index + 1) % page_size
    ]
    assert read.complete is False
    assert read.outcome is SourceReadOutcome.CRAWL_CONTINUATION
    expected_skips = {"host_held": 1}
    if missing_identity:
        expected_skips["missing_required_posting_identity"] = completed_pages
    assert read.skip_reasons == expected_skips
    assert read.detail.attempted == read.detail.capped == 0
    mixed_phenom = platform == "phenom" and missing_identity
    assert len(read.deferred_targets) == (2 if mixed_phenom else 1)
    cursor = read.deferred_targets[0]
    query = parse_qs(urlsplit(cursor.url).query)
    assert query["page" if platform == "icims" else "offset"] == [
        str(completed_pages + 1 if platform == "icims" else held_offset)
    ]
    assert cursor.automatic_eligible is False
    assert read.exhausted_outcome is (
        None if platform == "phenom" else SourceReadOutcome.PUBLISHER_INDEX
    )
    if mixed_phenom:
        retry = read.deferred_targets[1]
        retry_query = parse_qs(urlsplit(retry.url).query)
        assert retry_query["offset"] == ["0"]
        assert retry_query["identity_records"] == [str(held_offset)]
        assert retry.automatic_eligible is False
        assert "could not read a title and posting link" in read.note
        assert "Scan again is required to verify complete board coverage" in read.note
    assert "publisher requested a wait" in read.note
    assert "Read deferred publisher links in source history" in read.note
    assert "scan the source again" in read.note
    assert "deadline" not in read.note

    fetcher.held = False
    recovered = []
    targets: tuple[SourceContinuationTarget, ...] = (cursor,)
    for _ in range(3 - completed_pages):
        continued = fetcher.continue_listings("Acme", targets, root_url=root_url, handoff_scopes=())
        recovered.extend(continued.listings)
        targets = continued.deferred_targets

    if mixed_phenom:
        assert targets and not targets[0].automatic_eligible
    else:
        assert targets == ()
    assert [listing.title for listing in recovered] == [
        f"Role {index}" for index in range(held_offset, 3 * page_size)
    ]
    assert offsets == [
        *range(0, held_offset, page_size),
        held_offset,
        *range(held_offset, 3 * page_size, page_size),
    ]
    if mixed_phenom:
        repaired = []
        targets = (read.deferred_targets[1],)
        for _ in range(completed_pages):
            continued = fetcher.continue_listings(
                "Acme", targets, root_url=root_url, handoff_scopes=()
            )
            repaired.extend(continued.listings)
            targets = continued.deferred_targets
        assert [listing.title for listing in repaired] == [
            f"Role {index}" for index in range(held_offset)
        ]
        assert read.complete is False
        assert read.exhausted_outcome is None


def test_crawl_delay_pacing_cut_mid_board_leaves_an_automatic_cursor() -> None:
    offsets: list[int] = []

    class PacedPage(ScriptedPage):
        paced = True

        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            offset = (int(parse_qs(urlsplit(url).query)["page"][0]) - 1) * 20
            offsets.append(offset)
            if self.paced and offset == 40:
                raise HostHeldError(url, ready_at=100, paced=True)
            jobs = [
                {
                    "data": {
                        "title": f"Role {index}",
                        "apply_url": f"https://careers.acme.test/job/{index}/apply",
                    }
                }
                for index in range(offset, offset + 20)
            ]
            return {"totalCount": 60, "jobs": jobs}

    root_url = "https://www.acme.test/careers"
    fetcher = PacedPage(f"https://jobs.acme.test/careers\n{_icims_page()}")

    read = fetcher.listings("Acme", root_url)

    assert read.raw_count == 40
    assert "host_held" not in read.skip_reasons
    assert "publisher requested a wait" not in read.note
    [cursor] = read.deferred_targets
    assert parse_qs(urlsplit(cursor.url).query)["page"] == ["3"]
    assert cursor.automatic_eligible is True

    fetcher.paced = False
    continued = fetcher.continue_listings("Acme", (cursor,), root_url=root_url, handoff_scopes=())
    assert [listing.title for listing in continued.listings] == [
        f"Role {index}" for index in range(40, 60)
    ]
    assert offsets == [0, 20, 40, 40]


def test_icims_stages_the_job_page_rather_than_the_login_gated_apply_url() -> None:
    """A site whose apply_url is a login page still stages a readable job page."""

    class ScriptedIcims(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return {
                "totalCount": 3,
                "jobs": [
                    {
                        "data": {
                            "title": "Gated Role",
                            "req_id": "REQ-1",
                            "apply_url": "https://careers-acme.icims.com/jobs/4321/login",
                        }
                    },
                    {
                        "data": {
                            "title": "Public Role",
                            "req_id": "REQ-2",
                            "apply_url": (
                                "https://careers-acme.icims.com/jobs/8765/public-role/job"
                            ),
                        }
                    },
                    {
                        "data": {
                            "title": "Unnumbered Role",
                            "req_id": "REQ-3",
                            "apply_url": "https://careers-acme.icims.com/login",
                        }
                    },
                ],
            }

    page = f"https://careers-acme.icims.com/jobs\n{_icims_page()}"
    read = ScriptedIcims(page).listings("Acme", "https://join.acme.test/jobs")

    assert read is not None
    staged = {item.title: item.url for item in read.listings}
    assert staged == {
        "Gated Role": "https://careers-acme.icims.com/jobs/4321/job?in_iframe=1",
        "Public Role": "https://careers-acme.icims.com/jobs/8765/job?in_iframe=1",
    }
    assert read.skip_reasons == {"missing_required_posting_identity": 1}


@pytest.mark.parametrize(
    ("saved_root", "receipt_tenant", "in_scope"),
    [
        ("https://careers-acme.icims.com/jobs", "careers-acme", True),
        ("https://careers-acme.icims.com/jobs/search", "careers-acme", True),
        ("https://careers-acme.icims.com", "careers-acme", True),
        ("https://careers-acme.icims.com/jobs", "careers-initech", False),
    ],
)
def test_a_classic_icims_listed_receipt_is_scoped_to_the_tenant_root_it_was_saved_at(
    saved_root: str, receipt_tenant: str, in_scope: bool
) -> None:
    """The sitemap read names /jobs/search, one source with that tenant's saved root."""
    board = f"https://{receipt_tenant}.icims.com/jobs/search"
    posting = f"https://{receipt_tenant}.icims.com/jobs/4321/job?in_iframe=1"
    target = replace(
        board_reads.page_continuation_target(
            board, source_url=saved_root, label="Listed page", reason="icims_sitemap_pending"
        ),
        detail_checkpoint=SourceDetailCheckpoint(
            platform="listed:icims",
            board_url=board,
            listing_ids=("4321",),
            listing_urls=(("4321", posting),),
            completed=(("4321", posting),),
        ),
    )

    scope = board_reads.publisher_search_scope(target)

    assert scope == (("icims", saved_root) if in_scope else None)


def test_an_aggregator_read_keeps_each_postings_employer_and_never_claims_completeness() -> None:
    """A source naming no employer keeps the one each posting states, and withdraws nothing."""
    requests: list[str] = []

    class ScriptedHimalayas(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requests.append(url)
            offset = int(url.split("offset=")[1].split("&", maxsplit=1)[0])
            jobs: list[dict[str, Any]] = [
                {
                    "title": f"Role {index}",
                    "companyName": f"Employer {index}",
                    "applicationLink": f"https://boards.example.test/{index}",
                    "locationRestrictions": ["United Kingdom", "Ireland"],
                    "guid": f"GUID-{index}",
                }
                for index in range(offset, min(offset + 20, 30))
            ]
            if offset == 0:
                jobs.append({"title": "Unlinked Role", "companyName": "Employer X"})
            return {"totalCount": 30, "jobs": jobs}

    read = ScriptedHimalayas().listings("", "https://himalayas.app/jobs")

    assert read is not None
    # An aggregator read is bounded, so absence from it never withdraws an opening.
    assert read.complete is False
    assert [item.title for item in read.listings] == [f"Role {index}" for index in range(30)]

    first = read.listings[0]
    assert first.employer == "Employer 0"
    assert first.url == "https://boards.example.test/0"
    assert first.location == "United Kingdom, Ireland"
    assert first.requisition_id == "GUID-0"

    assert requests == [
        "https://himalayas.app/jobs/api?offset=0&limit=20",
        "https://himalayas.app/jobs/api?offset=20&limit=20",
    ]


def test_an_aggregator_that_states_no_further_page_is_not_asked_for_one() -> None:
    """Paging follows what the API says is left, not the ceiling that bounds it."""
    requests: list[str] = []

    class ScriptedArbeitnow(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            requests.append(url)
            jobs: list[dict[str, Any]] = [
                {
                    "slug": f"role-{index}",
                    "company_name": f"Employer {index}",
                    "title": f"Role {index}",
                    "url": f"https://www.arbeitnow.com/view/role-{index}",
                    "location": "Berlin",
                }
                for index in range(50)
            ]
            jobs.append(
                {"slug": "unlinked", "company_name": "Employer X", "title": "Unlinked Role"}
            )
            return {"data": jobs, "links": {"next": None}}

    read = ScriptedArbeitnow().listings("", "https://www.arbeitnow.com/")

    assert read is not None
    assert read.complete is False
    assert [item.title for item in read.listings] == [f"Role {index}" for index in range(50)]

    first = read.listings[0]
    assert first.employer == "Employer 0"
    assert first.url == "https://www.arbeitnow.com/view/role-0"
    assert first.location == "Berlin"
    assert first.requisition_id == "role-0"

    assert requests == ["https://www.arbeitnow.com/api/job-board-api?page=1"]


def test_an_aggregator_read_is_bounded_by_listings_rather_than_by_pages() -> None:
    """The bound is a count of openings, so a larger page than assumed cannot widen it."""

    class ScriptedArbeitnow(ScriptedPage):
        def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
            return {
                "data": [
                    {
                        "slug": f"role-{index}",
                        "company_name": f"Employer {index}",
                        "title": f"Role {index}",
                        "url": f"https://www.arbeitnow.com/view/role-{index}",
                        "location": "Berlin",
                    }
                    for index in range(137)
                ],
                "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
            }

    read = ScriptedArbeitnow().listings("", "https://www.arbeitnow.com/")

    assert read is not None
    assert read.complete is False
    assert len(read.listings) == board_reads.AGGREGATOR_LISTINGS


def test_bounded_board_names_cap_omissions_separately_from_parser_skips() -> None:
    listings = [
        source_listing("Data Platform Lead", f"https://aggregator.example.test/jobs/{index}")
        for index in range(board_reads.AGGREGATOR_LISTINGS + 5)
    ]

    read = board_reads.bounded(listings, raw_count=len(listings))

    assert len(read.listings) == board_reads.AGGREGATOR_LISTINGS
    assert read.raw_count == board_reads.AGGREGATOR_LISTINGS + 5
    assert read.skip_reasons == {"bounded_result_cap": 5}


_CAPPED_ROOT = "https://initech.wd1.myworkdayjobs.com/Careers"
_CAPPED_FACETS = ("jobFamilyGroup", "timeType", "Location_Country")


class _CappedWorkday(BoardApiFetcher):
    """A CXS search stating at most ``cap``, on page 0 only, and answering page 0 past it."""

    def __init__(self, families: Mapping[str, int], *, cap: int = 2000) -> None:
        super().__init__()
        self.cap = cap
        self.postings = [
            (family, ("FT", "PT")[index % 2], ("US", "CA")[index % 2], f"{family}-{index}")
            for family, count in families.items()
            for index in range(count)
        ]
        self.unserved: dict[str, int] = {}
        self.untagged: set[str] = set()
        # Offsets past which an unfiltered ("") or family-only search answers empty pages.
        self.reachable: dict[str, int] = {}
        # A family whose search also answers the board's first posting.
        self.overlapping = ""
        self.bodies: list[dict[str, Any]] = []

    def _matching(self, applied: Mapping[str, list[str]], skip: str = "") -> list[tuple[str, ...]]:
        return [
            row
            for row in self.postings
            if all(
                row[_CAPPED_FACETS.index(name)] in values
                for name, values in applied.items()
                if name != skip
            )
        ]

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.bodies.append(body)
        applied = body["appliedFacets"]
        rows = [
            row
            for row in self._matching(applied)
            if int(row[3].rpartition("-")[2]) >= self.unserved.get(row[0], 0)
        ]
        stated = min(len(rows), self.cap)
        if self.overlapping and applied == {"jobFamilyGroup": [self.overlapping]}:
            rows = [self.postings[0], *rows]
        requested, limit = int(body["offset"]), int(body["limit"])
        offset = 0 if requested >= self.cap else requested
        scope = ",".join(applied.get("jobFamilyGroup", []))
        if set(applied) <= {"jobFamilyGroup"} and requested >= self.reachable.get(scope, 10**9):
            rows = rows[:offset]
        facets = []
        for position, name in enumerate(_CAPPED_FACETS):
            counts: dict[str, int] = {}
            for row in self._matching(applied, skip=name):
                if not (position == 0 and row[0] in self.untagged):
                    counts[row[position]] = counts.get(row[position], 0) + 1
            facets.append(
                {
                    "facetParameter": name,
                    "values": [
                        {"id": value, "descriptor": value.title(), "count": count}
                        for value, count in sorted(counts.items())
                    ],
                }
            )
        first = requested == 0
        return {
            "total": stated if first else 0,
            "facets": facets if first else [],
            "jobPostings": [
                {"title": f"Role {row[3]}", "externalPath": f"/job/Remote/Role_{row[3]}-1"}
                for row in rows[offset : offset + limit]
            ],
        }


def _drained(fetcher: BoardApiFetcher, url: str, *, steps: int = 30) -> list[SourceRead]:
    reads = [fetcher.listings("Initech", url)]
    targets = list(reads[0].deferred_targets)
    while targets and len(reads) < steps:
        reads.append(
            fetcher.continue_listings("Initech", (targets.pop(0),), root_url=url, handoff_scopes=())
        )
        targets.extend(reads[-1].deferred_targets)
    return reads


def _identities(reads: list[SourceRead]) -> set[str]:
    return {item.url for read in reads for item in read.listings}


def test_a_capped_workday_board_reads_every_family_and_certifies_the_facet_sum() -> None:
    fetcher = _CappedWorkday({"fam-a": 900, "fam-b": 800, "fam-c": 600})

    reads = _drained(fetcher, _CAPPED_ROOT)

    assert len(_identities(reads)) == 2300
    assert len(reads) > 1
    assert reads[-1].deferred_targets == ()
    assert reads[-1].listing_coverage is not None
    assert len(reads[-1].listing_coverage.posting_urls) == 2300
    assert {body["appliedFacets"].get("jobFamilyGroup", [""])[0] for body in fetcher.bodies} == {
        "",
        "fam-a",
        "fam-b",
        "fam-c",
    }


def test_a_family_over_the_cap_is_split_again_by_a_second_facet() -> None:
    fetcher = _CappedWorkday({"fam-a": 2500, "fam-b": 100})

    reads = _drained(fetcher, _CAPPED_ROOT, steps=40)

    assert len(_identities(reads)) == 2600
    assert reads[-1].listing_coverage is not None
    assert len(reads[-1].listing_coverage.posting_urls) == 2600
    assert {"jobFamilyGroup": ["fam-a"], "timeType": ["FT"]} in [
        body["appliedFacets"] for body in fetcher.bodies
    ]


def test_a_capped_board_whose_facet_sum_is_not_reached_stays_uncertified() -> None:
    fetcher = _CappedWorkday({"fam-a": 900, "fam-b": 800, "fam-c": 600})
    fetcher.unserved = {"fam-b": 5}

    reads = _drained(fetcher, _CAPPED_ROOT)

    assert len(_identities(reads)) == 2295
    assert reads[-1].deferred_targets == ()
    assert reads[-1].listing_coverage is None
    assert reads[-1].discovery_truncated is True
    assert not any(read.complete for read in reads)


def test_an_uncapped_faceted_workday_board_pages_without_splitting() -> None:
    fetcher = _CappedWorkday({"fam-a": 25, "fam-b": 10})

    read = fetcher.listings("Initech", _CAPPED_ROOT)

    assert read.complete is True
    assert len(read.listings) == 35
    assert [body["appliedFacets"] for body in fetcher.bodies] == [{}, {}, {}]


def test_a_certified_read_counts_a_first_pass_posting_with_no_identity() -> None:
    class UnidentifiedOnce(_CappedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if len(self.bodies) == 1:
                payload["jobPostings"].append({"title": "Role without a path"})
            return payload

    fetcher = UnidentifiedOnce({"fam-a": 25, "fam-b": 10})

    read = fetcher.listings("Initech", _CAPPED_ROOT)

    assert read.complete is True
    assert len(read.listings) == 35
    assert read.skip_reasons == {"missing_required_posting_identity": 1}


def test_a_saved_family_scope_keeps_its_family_while_a_capped_read_splits() -> None:
    fetcher = _CappedWorkday({"fam-a": 2300, "fam-b": 50})

    reads = _drained(fetcher, f"{_CAPPED_ROOT}?jobFamilyGroup=fam-a", steps=40)

    assert len(_identities(reads)) == 2300
    assert reads[-1].listing_coverage is not None
    assert {tuple(body["appliedFacets"]["jobFamilyGroup"]) for body in fetcher.bodies} == {
        ("fam-a",)
    }
    assert {"jobFamilyGroup": ["fam-a"], "timeType": ["PT"]} in [
        body["appliedFacets"] for body in fetcher.bodies
    ]


def test_a_short_family_facet_is_passed_over_for_one_summing_to_the_size() -> None:
    fetcher = _CappedWorkday({"fam-a": 1500, "fam-b": 800})
    fetcher.untagged = {"fam-b"}

    reads = _drained(fetcher, _CAPPED_ROOT)

    assert len(_identities(reads)) == 2300
    assert reads[-1].listing_coverage is not None
    assert not any("jobFamilyGroup" in body["appliedFacets"] for body in fetcher.bodies)


class _DriftingWorkday(_CappedWorkday):
    """Post a new fam-c row, and optionally withdraw a read fam-a row, as fam-c is first read."""

    def __init__(self, families: Mapping[str, int], *, withdraw: bool) -> None:
        super().__init__(families)
        self.withdraw = withdraw
        self.drifted = False

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        if not self.drifted and body["appliedFacets"].get("jobFamilyGroup") == ["fam-c"]:
            self.drifted = True
            if self.withdraw:
                self.postings.remove(("fam-a", "FT", "US", "fam-a-0"))
            first = next(i for i, row in enumerate(self.postings) if row[0] == "fam-c")
            self.postings.insert(first, ("fam-c", "FT", "US", "fam-c-9999"))
        return super().post_json(url, body)


@pytest.mark.parametrize("withdraw", [False, True])
def test_a_family_that_changes_after_the_split_cannot_certify(withdraw: bool) -> None:
    fetcher = _DriftingWorkday({"fam-a": 900, "fam-b": 800, "fam-c": 600}, withdraw=withdraw)

    reads = _drained(fetcher, _CAPPED_ROOT)

    assert fetcher.drifted
    assert any(url.endswith("Role_fam-c-9999-1") for url in _identities(reads))
    assert reads[-1].deferred_targets == ()
    assert reads[-1].listing_coverage is None


def test_a_root_listing_that_runs_dry_before_its_total_is_read_by_family() -> None:
    fetcher = _CappedWorkday({"fam-a": 40, "fam-b": 20})
    fetcher.reachable = {"": 40}

    read = fetcher.listings("Initech", _CAPPED_ROOT)

    assert read.complete is True
    assert len(read.listings) == 60
    assert {"jobFamilyGroup": ["fam-b"]} in [body["appliedFacets"] for body in fetcher.bodies]


def test_a_certified_split_read_counts_no_root_re_read_as_skipped() -> None:
    fetcher = _CappedWorkday({"fam-a": 40, "fam-b": 20})
    fetcher.reachable = {"": 40}

    read = fetcher.listings("Initech", _CAPPED_ROOT)

    assert read.complete is True
    assert read.skip_reasons == {}


def test_a_split_re_read_of_root_rows_is_not_counted_as_duplicate_rows() -> None:
    class DriftingFamily(_CappedWorkday):
        def post_json(self, url: str, body: dict[str, Any]) -> Any:
            payload = super().post_json(url, body)
            if "fam-b" in body["appliedFacets"].get("jobFamilyGroup", []):
                payload["jobPostings"] = [
                    row for row in payload["jobPostings"] if "fam-b-0" not in row["externalPath"]
                ]
            return payload

    fetcher = DriftingFamily({"fam-a": 40, "fam-b": 20})
    fetcher.reachable = {"": 40}

    read = fetcher.listings("Initech", _CAPPED_ROOT)

    assert read.complete is False
    assert read.skip_reasons == {"incomplete_workday_listing": 1}


def test_a_family_that_runs_dry_in_a_continuation_is_split_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workday, "WORKDAY_MAX_PAGES", 10)
    fetcher = _CappedWorkday({"fam-a": 900, "fam-b": 800, "fam-c": 600})
    fetcher.reachable = {"fam-a": 600}

    reads = _drained(fetcher, _CAPPED_ROOT, steps=40)

    assert "offset=180" in reads[0].deferred_targets[0].url
    assert len(_identities(reads)) == 2300
    assert reads[-1].listing_coverage is not None
    assert {"jobFamilyGroup": ["fam-a"], "timeType": ["FT"]} in [
        body["appliedFacets"] for body in fetcher.bodies
    ]


def test_a_posting_listed_under_two_families_keeps_the_split_read_uncertified() -> None:
    fetcher = _CappedWorkday({"fam-a": 900, "fam-b": 800, "fam-c": 600})
    fetcher.overlapping = "fam-b"

    reads = _drained(fetcher, _CAPPED_ROOT)

    assert len(_identities(reads)) == 2300
    assert reads[-1].deferred_targets == ()
    assert reads[-1].listing_coverage is None
