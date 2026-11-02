"""Campaign admission decisions made without a session: classification, staging, board routing."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from itertools import product
from pathlib import Path
from socket import gaierror
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from tailorcv.application.discovery import guided_campaign_admission
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.board_slugs import board_identity_key
from tailorcv.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelReason,
    FunnelStage,
)
from tailorcv.application.discovery.guided_campaign import (
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignIdentities,
    CampaignManifest,
)
from tailorcv.application.discovery.guided_campaign_admission import (
    BOARD_LISTINGS_NOT_STAGED_DETAIL,
    AdmissionResult,
    BoardListingCandidate,
    ObservedUrl,
    ReadUrl,
    _read_one,
    _staging_decision,
    admit_read_urls,
    classify_url,
    read_captured_urls,
)
from tailorcv.application.discovery.guided_campaign_ledger import GuidedCampaignLedger
from tailorcv.application.discovery.pending import (
    MARKERS_CHANGED_ACTION,
    reset_pending_queue,
    set_pending_markers,
)
from tailorcv.application.discovery.posting_fetch import FetchedPosting
from tailorcv.application.discovery.posting_identity import posting_identity_keys, posting_url_key
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.discovery.scan_staging import (
    CAMPAIGN_BOARD_LISTING_REASON,
    CAMPAIGN_FOUND_POSTING_REASON,
)
from tailorcv.application.discovery.scan_sweep import SOURCE_STAGE_LIMIT
from tailorcv.application.discovery.source_reading import PostingCheck, SourceListing
from tailorcv.application.failures import ValidationError
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.application.workspaces.parameters import SearchParametersService
from tailorcv.domain import new_id, utc_now
from tailorcv.domain.campaigns import (
    CapturedUrlClassification,
    CapturedUrlOutcome,
    StagedBasis,
    UrlActionKind,
)
from tailorcv.domain.records import (
    PageClassification,
    PostingContentAccessStatus,
    PostingReadIntent,
    RobotsReadBasis,
)
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    CountryNames,
    Employers,
    Focus,
    JobSearchParameters,
    Location,
    Seniority,
)
from tailorcv.domain.staged_facts import ListingIdentityTrust, PublishedFact, StagedListingFacts
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    AuditEvent,
    BackgroundTask,
    Employer,
    PendingDiscovery,
    Source,
    SourceScanOutcome,
)
from tailorcv.infrastructure.providers.base import BackgroundPoll, CallRecord
from tailorcv.infrastructure.web_fetch.policy import (
    UNRESOLVED_DESTINATION_NOTE,
    PoliteHttpClient,
    RobotsDeniedError,
)
from tailorcv.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher
from tailorcv.shared.costs import CallManifest, Usage
from tailorcv.shared.costs import Ledger as CostLedger
from tailorcv.shared.errors import IngestError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import UnexpectedPostingFetcher
from tests.support.scenarios import scan_workspace
from tests.support.scenarios import titles as stated_titles

_POSTING_URL = "https://jobs.example.test/roles/1"


def test_a_posting_matching_a_searched_title_is_staged_not_held_out() -> None:
    """A readable posting whose title matches a searched title stages on title evidence."""
    outcome, staged_basis, _detail, _reason, _findings = _staging_decision(
        parameters=JobSearchParameters(),
        facts=StagedListingFacts(),
        url=_POSTING_URL,
        title="Staff Data Engineer",
        employer="Example Co",
        body_text="We are hiring a Staff Data Engineer.",
        searched_titles=["Staff Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.STAGED, StagedBasis.TITLE_EVIDENCE)


def test_open_page_only_plus_model_selected_alone_does_not_stage() -> None:
    """A model pick with only an open-page observation is screened out, not staged."""
    outcome, staged_basis, _detail, _reason, _findings = _staging_decision(
        parameters=JobSearchParameters(),
        facts=StagedListingFacts(),
        url=_POSTING_URL,
        title="Unrelated Barista Role",
        employer="Example Co",
        body_text="Generic body with no saved focus terms.",
        searched_titles=["Staff Data Engineer"],
        model_selected=True,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.SCREENED_OUT, None)


def test_a_body_focus_term_posting_outside_saved_places_is_screened_out_with_location_reason() -> (
    None
):
    """A seniority/location refusal still screens out even a body-focus-term match."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
        seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
        focus=Focus(capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]),
    )
    outcome, staged_basis, detail, _reason, _findings = _staging_decision(
        parameters=parameters,
        facts=StagedListingFacts(
            locations=PublishedFact(
                values=("Southport, Ruritania",), provenance="fictional.location", complete=True
            )
        ),
        url=_POSTING_URL,
        title="Staff Data Engineer",
        employer="Example Co",
        body_text="We need someone deep in distributed systems.",
        searched_titles=["Staff Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.SCREENED_OUT, None)
    assert "does not fit the saved places" in detail


_SENIORITY_BAND_TARGETS = (
    "Senior Data Product Manager",
    "Associate Director, Data Governance",
    "Technical Product Manager, Import Compliance",
    "Principal Technical Product Manager, Data Platform",
    "Senior Logistics Data Product Manager",
    "Associate Director, Logistics Data Strategy",
)


def test_a_model_selected_posting_keeps_title_inferred_seniority_open_for_review() -> None:
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(
            target_titles=stated_titles(*_SENIORITY_BAND_TARGETS), minimum="senior"
        ),
    )
    outcome, staged_basis, detail, _reason, _findings = _staging_decision(
        parameters=parameters,
        facts=StagedListingFacts(
            locations=PublishedFact(
                values=("Freedonia",), provenance="fictional.location", complete=True
            )
        ),
        url=_POSTING_URL,
        title="Associate Product Manager - Fictional Suite",
        employer="Example Cooperative",
        body_text="",
        searched_titles=list(_SENIORITY_BAND_TARGETS),
        model_selected=True,
        has_search_source_observation=True,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.STAGED, StagedBasis.TITLE_EVIDENCE)
    assert detail == ""


def test_a_body_focus_term_posting_in_band_and_place_is_staged_as_body_focus_term() -> None:
    """A body focus term overrides only a missing title match, so it can still stage."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia")],
        seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
        focus=Focus(capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]),
    )
    outcome, staged_basis, _detail, _reason, _findings = _staging_decision(
        parameters=parameters,
        facts=StagedListingFacts(
            locations=PublishedFact(
                values=("Freedonia",), provenance="fictional.location", complete=True
            )
        ),
        url=_POSTING_URL,
        title="Totally Unrelated Title",
        employer="Example Co",
        body_text="We need someone deep in distributed systems.",
        searched_titles=["Staff Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.STAGED, StagedBasis.BODY_FOCUS_TERM)


@pytest.mark.parametrize(
    ("model_selected", "has_search_source_observation"),
    [(False, False), (True, True)],
)
def test_a_blank_title_is_never_staged_even_with_a_body_focus_term_or_model_pick(
    model_selected: bool, has_search_source_observation: bool
) -> None:
    """Classification is an extra gate, not a replacement for a real posting title."""
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
        focus=Focus(capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]),
    )

    outcome, staged_basis, detail, _reason, _findings = _staging_decision(
        parameters=parameters,
        facts=StagedListingFacts(),
        url=_POSTING_URL,
        title="   ",
        employer="Example Co",
        body_text="We need someone deep in distributed systems.",
        searched_titles=["Staff Data Engineer"],
        model_selected=model_selected,
        has_search_source_observation=has_search_source_observation,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.UNRESOLVED, None)
    assert detail == guided_campaign_admission.NO_POSTING_TITLE_DETAIL


class _CollectionPageFetcher:
    """Answers every fetch with a readable page classified as a collection, not a posting."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="Browse 40 openings in distributed systems across our teams.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            page_classification=PageClassification.COLLECTION,
        )


@pytest.mark.parametrize("model_selected", [False, True])
def test_a_page_that_names_no_posting_title_is_never_staged(model_selected: bool) -> None:
    """A collection page never stages, whatever else it mentions."""
    item = ObservedUrl(
        url="https://jobs.example.test/careers",
        observations=((UrlActionKind.OPEN_PAGE, ""),),
        model_selected=model_selected,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_CollectionPageFetcher()),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
            focus=Focus(
                capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]
            ),
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.staged_basis is None
    assert result.detail == "page is not a single posting (classified as collection)"


class _H1OnlyPostingFetcher:
    """A posting page with no JSON-LD markup, classified from its h1 title alone."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="Staff Data Engineer\nBuild reliable data systems for our platform.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            page_classification=PageClassification.POSTING,
        )


def test_a_page_classified_as_a_posting_from_its_h1_alone_is_staged() -> None:
    """No JSON-LD markup is required to stage: the h1-derived title is enough recall."""
    item = ObservedUrl(
        url="https://jobs.example.test/roles/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_H1OnlyPostingFetcher()),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.STAGED


class _HostNamingPostingFetcher:
    """A posting page whose declared hiring organization is the page's own hostname."""

    def __init__(self, employer: str) -> None:
        self._employer = employer

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="Staff Data Engineer\nBuild reliable data systems for our platform.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer=self._employer,
            publisher_markup_present=True,
            page_classification=PageClassification.POSTING,
            robots_basis=RobotsReadBasis.ALLOWED,
        )


_MIRROR_POSTING_URL = "https://jobmirror.example.test/jobs/44120"


def _mirror_read(declared_employer: str) -> ReadUrl:
    item = ObservedUrl(
        url=_MIRROR_POSTING_URL,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    return _read_one(
        _postings_scans(_HostNamingPostingFetcher(declared_employer)),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )


def test_a_page_declaring_its_own_host_as_the_employer_is_refused_at_intake() -> None:
    """A page naming itself as the hiring organization is refused with its reason stated."""
    result = _mirror_read("jobmirror.example.test")

    assert result.outcome == CapturedUrlOutcome.SCREENED_OUT
    assert result.staged_basis is None
    assert result.detail == guided_campaign_admission.HOST_NAMED_AS_EMPLOYER_DETAIL


def test_a_page_naming_another_employer_stages_even_on_a_host_that_hires_for_nobody() -> None:
    """The refusal reads only the name the page claims for itself."""
    result = _mirror_read("Northwind")

    assert result.outcome == CapturedUrlOutcome.STAGED


def test_a_single_tenant_board_whose_tenant_names_the_employer_still_stages() -> None:
    """The refusal spells the whole host, so an employer's own tenant label still stages."""
    outcome, staged_basis, _detail, _reason, _findings = _staging_decision(
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        facts=StagedListingFacts(),
        url="https://northwind.wd3.myworkdayjobs.com/en-US/Careers/job/Staff-Data-Engineer_R-1",
        title="Staff Data Engineer",
        employer="Northwind",
        body_text="Build reliable data systems for our platform.",
        searched_titles=["Staff Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.STAGED, StagedBasis.TITLE_EVIDENCE)


class _MisclassifiedJobPostingMarkupFetcher:
    """A JSON-LD JobPosting page whose visible text reads as unavailable."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="This posting is unavailable.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            publisher_markup_present=True,
            page_classification=PageClassification.ERROR_OR_CHALLENGE,
        )


def test_an_agency_posting_on_loxo_is_a_job_page_candidate_not_a_single_employer_detail() -> None:
    classification, refusal = classify_url(
        "https://acme.app.loxo.co/job/MTAwMTQtd2lkZ2V0cw==",
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert (classification, refusal) == (CapturedUrlClassification.JOB_PAGE_CANDIDATE, "")


def test_a_page_with_publisher_jobposting_markup_is_never_classified_out() -> None:
    """Publisher JobPosting markup overrides a misleading visible-text classification."""
    item = ObservedUrl(
        url="https://jobs.example.test/roles/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_MisclassifiedJobPostingMarkupFetcher()),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.STAGED


class _ExplosivePostingFetcher:
    """Fails the test if the admission layer ever fetches this URL."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        raise AssertionError(f"fetch must not be called for {url}")


class _FetcherOnlyScans:
    """Just enough of ``SourceScanService`` for a fetch-path guard test."""

    class postings:  # noqa: N801 - mirrors the real service's attribute shape
        fetcher = _ExplosivePostingFetcher()


def test_a_model_written_url_with_no_real_observation_is_rejected_before_any_fetch() -> None:
    """A model pick with no search or page-action observation never reaches the fetcher."""
    item = ObservedUrl(
        url="https://jobs.example.test/phantom",
        observations=(),
        model_selected=True,
        model_why="looks relevant",
    )

    result = _read_one(
        _FetcherOnlyScans(),  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == "not in captured search output"


@pytest.mark.parametrize("host", ["ca.linkedin.com", "www.linkedin.com"])
def test_linkedin_jobs_search_results_never_reach_the_posting_fetcher(host: str) -> None:
    """A LinkedIn title-search collection is not mistaken for one posting."""
    item = ObservedUrl(
        url=f"https://{host}/jobs/senior-data-engineering-jobs?f_AL=true",
        observations=((UrlActionKind.SEARCH_SOURCE, "Senior Data Engineer jobs"),),
        model_selected=True,
        model_why="looks relevant",
    )

    result = _read_one(
        _FetcherOnlyScans(),  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.classification == CapturedUrlClassification.JOB_PAGE_CANDIDATE
    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == guided_campaign_admission.LINKEDIN_SEARCH_RESULTS_DETAIL


_LINKEDIN_HOSTS = ("www.linkedin.com", "ca.linkedin.com", "uk.linkedin.com", "linkedin.com")
_COLLECTION_SLUGS = (
    "associate-director-data-jobs",
    "associate-director-data-jobs-denver",
    "associate-director-data-jobs-denver-co",
    "product-manager-jobs-toronto-on",
    "product-manager-jobs-san-francisco-ca",
)
_URL_ENDINGS = ("", "/", "?position=1&pageNum=0", "/?f_TPR=r86400")


def _captured_search_source(url: str) -> ObservedUrl:
    return ObservedUrl(
        url=url,
        observations=((UrlActionKind.SEARCH_SOURCE, "Associate Director Data jobs"),),
        model_selected=True,
        model_why="looks relevant",
    )


@pytest.mark.parametrize(
    ("host", "slug", "ending"), list(product(_LINKEDIN_HOSTS, _COLLECTION_SLUGS, _URL_ENDINGS))
)
def test_every_linkedin_search_results_slug_is_unresolved_before_any_fetch(
    host: str, slug: str, ending: str
) -> None:
    """A title search, with or without a place after ``-jobs``, is a collection, not a posting."""
    result = _read_one(
        _FetcherOnlyScans(),  # type: ignore[arg-type]
        item=_captured_search_source(f"https://{host}/jobs/{slug}{ending}"),
        parameters=JobSearchParameters(),
        searched_titles=["Associate Director Data"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == guided_campaign_admission.LINKEDIN_SEARCH_RESULTS_DETAIL


@pytest.mark.parametrize(
    "url",
    [
        *(
            f"https://{host}/jobs/view/{slug}"
            for host in _LINKEDIN_HOSTS
            for slug in (
                "associate-director-data-at-example-co-4400000001",
                "data-jobs-lead-at-example-co-4400000002",
                "4400000003",
            )
        ),
        *(
            f"https://{host}/jobs/{slug}"
            for host in ("jobs.example.test", "linkedin.example.test")
            for slug in _COLLECTION_SLUGS
        ),
    ],
)
def test_a_linkedin_posting_or_another_hosts_jobs_slug_is_read_as_a_page(url: str) -> None:
    """Only a LinkedIn search slug is refused unread; the page read decides everything else."""
    scans: Any = SimpleNamespace(postings=SimpleNamespace(fetcher=_CollectionPageFetcher()))

    result = _read_one(
        scans,
        item=_captured_search_source(url),
        parameters=JobSearchParameters(),
        searched_titles=["Associate Director Data"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.detail != guided_campaign_admission.LINKEDIN_SEARCH_RESULTS_DETAIL
    assert result.detail.startswith("page is not a single posting")


def test_a_linkedin_posting_redirected_to_a_placed_search_is_never_staged() -> None:
    """A read landing on a title-and-place collection is as unresolved as a bare title one."""
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(
            fetcher=_RedirectingPostingFetcher(
                "https://www.linkedin.com/jobs/staff-data-engineer-jobs-denver-co?trk=expired"
            )
        )
    )

    result = _read_one(
        scans,
        item=_captured_search_source(
            "https://www.linkedin.com/jobs/view/staff-data-engineer-at-example-co-4400000004"
        ),
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == guided_campaign_admission.LINKEDIN_SEARCH_RESULTS_DETAIL


def test_a_model_written_url_cannot_claim_a_known_posting_overlap() -> None:
    """An invented URL is unresolved even when its fingerprint names a known posting."""
    item = ObservedUrl(
        url="https://boards.example.test/globex/jobs/42",
        observations=(),
        model_selected=True,
        model_why="looks relevant",
    )

    result = _read_one(
        _FetcherOnlyScans(),  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset({posting_url_key(item.url)}),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == "not in captured search output"


def test_a_known_posting_url_counts_as_overlap_without_a_fetch() -> None:
    """A URL already tracked as a posting classifies from its fingerprint alone."""
    item = ObservedUrl(
        url="https://boards.example.test/globex/jobs/42",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        None,  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset({posting_url_key(item.url)}),
    )

    assert result.outcome == CapturedUrlOutcome.KNOWN_POSTING


def test_a_tracked_apply_url_is_not_readmitted_from_its_detail_url() -> None:
    """The tracked posting kept '/apply'; the campaign captured its detail page."""
    detail = "https://jobs.lever.co/globex/9f2c1d3e"
    item = ObservedUrl(
        url=detail,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        None,  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset({posting_url_key(f"{detail}/apply")}),
    )

    assert result.outcome == CapturedUrlOutcome.KNOWN_POSTING


def test_a_workday_locale_variant_of_a_known_requisition_counts_as_overlap() -> None:
    """The queue holds the location-path URL; the campaign captured the /en-US/ one."""
    listed = (
        "https://acme.wd1.myworkdayjobs.com/acme_careers/job/Canada-Remote/Product-Manager_R10001"
    )
    item = ObservedUrl(
        url="https://acme.wd1.myworkdayjobs.com/en-US/acme_careers/job/Product-Manager_R10001",
        observations=((UrlActionKind.SEARCH_SOURCE, "Product Manager jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        None,  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=posting_identity_keys(listed),
    )

    assert result.outcome == CapturedUrlOutcome.KNOWN_POSTING


def test_a_registered_board_url_counts_as_overlap_without_a_fetch() -> None:
    """A URL already tracked as a registered board classifies from its identity alone."""
    item = ObservedUrl(
        url="https://boards.greenhouse.io/example",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        None,  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset({board_identity_key(item.url)}),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.REGISTERED_BOARD


def test_a_non_job_search_source_is_retained_without_a_publisher_read() -> None:
    """A generic source is evidence, not permission to read an unrelated page."""
    item = ObservedUrl(
        url="https://news.example.test/company-announcement",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(fetcher=UnexpectedPostingFetcher()),
    )

    result = _read_one(
        scans,
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.classification == CapturedUrlClassification.OTHER
    assert result.outcome == CapturedUrlOutcome.UNRESOLVED


def _exclusion_decision(parameters: JobSearchParameters, title: str, employer: str) -> Any:
    return _staging_decision(
        parameters=parameters,
        facts=StagedListingFacts(),
        url=_POSTING_URL,
        title=title,
        employer=employer,
        body_text="We need someone deep in distributed systems.",
        searched_titles=["Staff Data Engineer"],
        model_selected=True,
        has_search_source_observation=True,
        publisher_read=False,
    )


def test_an_excluded_title_is_screened_out_when_a_body_term_would_replace_title_evidence() -> None:
    """A hard title exclusion is enforced by the program, never left to the prompt."""
    parameters = JobSearchParameters(
        seniority=Seniority(
            target_titles=stated_titles("Staff Data Engineer"), excluded_titles=["Sales"]
        ),
        focus=Focus(capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]),
    )

    outcome, staged_basis, detail, _reason, _findings = _exclusion_decision(
        parameters, "Sales Director, Distributed Systems", "Example Co"
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.SCREENED_OUT, None)
    assert "exclude titles containing Sales" in detail


def test_an_excluded_employer_is_screened_out_when_a_model_pick_would_replace_title_evidence() -> (
    None
):
    """A person-stated employer exclusion refuses the weaker staging path too."""
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
        employers=Employers(excluded_employers=["Globex"]),
        focus=Focus(capabilities=[Cluster(name="Data platforms", terms=["distributed systems"])]),
    )

    outcome, staged_basis, detail, _reason, _findings = _exclusion_decision(
        parameters, "Platform Reliability Lead", "Globex Corporation"
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.SCREENED_OUT, None)
    assert detail == "The employer is excluded by the current settings."


def test_a_published_hybrid_posting_outside_accepted_places_is_held_out_with_its_body() -> None:
    """The arrangement hold-out reads the fetched body, so a title match cannot bypass it."""
    remote_only = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    parameters = JobSearchParameters(
        locations=[Location(key="us_remote", country="United States", arrangement=remote_only)],
        countries=[CountryNames(country="United States")],
        arrangement=remote_only,
        seniority=Seniority(target_titles=stated_titles("Data Engineer")),
    )
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("New York, NY",), provenance="lever.location", complete=True
        ),
        arrangements=PublishedFact(
            values=("hybrid",), provenance="lever.arrangement", complete=True
        ),
    )

    outcome, staged_basis, detail, _reason, _findings = _staging_decision(
        parameters=parameters,
        facts=facts,
        url=_POSTING_URL,
        title="Data Engineer",
        employer="Acme",
        body_text="This role is hybrid, based in NYC.",
        searched_titles=["Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.HELD_OUT, None)
    assert detail == "The published work arrangement is refused for United States."


def test_an_office_posting_with_no_stated_arrangement_at_a_remote_only_market_is_held_out() -> None:
    """A search result naming a US city, no arrangement, and no remote offer never stages."""
    remote_only = Arrangement(
        remote=Acceptance.PREFERRED,
        hybrid=Acceptance.REFUSED,
        on_site=Acceptance.REFUSED,
        maximum_office_days=0,
    )
    parameters = JobSearchParameters(
        locations=[Location(key="us_remote", country="United States", arrangement=remote_only)],
        countries=[CountryNames(country="United States")],
        arrangement=remote_only,
        seniority=Seniority(target_titles=stated_titles("Data Engineer")),
    )
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("Denver, Colorado", "United States"),
            provenance="lever.location",
            complete=True,
        ),
    )

    outcome, _staged_basis, detail, reason, findings = _staging_decision(
        parameters=parameters,
        facts=facts,
        url=_POSTING_URL,
        title="Data Engineer",
        employer="Acme",
        body_text="Join the data team at our Denver office.",
        searched_titles=["Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=True,
    )

    assert (outcome, reason) == (CapturedUrlOutcome.HELD_OUT, FunnelReason.HARD_CONSTRAINT_EXCLUDED)
    assert detail == (
        "No remote option was published, and every named site is a place you accept only as remote."
    )
    assert [item.code for item in findings if item.dimension == "arrangement"] == [
        "arrangement_unknown",
        "arrangement_remote_only_sites",
    ]


def test_a_published_foreign_location_is_held_out_naming_the_location_rule() -> None:
    """A place outside every saved country holds the page out and says so."""
    parameters = JobSearchParameters(
        locations=[Location(key="us_remote", country="United States")],
        countries=[CountryNames(country="United States")],
        seniority=Seniority(target_titles=stated_titles("Data Engineer")),
    )
    facts = StagedListingFacts(
        locations=PublishedFact(
            values=("Berlin, Germany",), provenance="lever.location", complete=True
        ),
    )

    outcome, _staged_basis, detail, reason, _findings = _staging_decision(
        parameters=parameters,
        facts=facts,
        url=_POSTING_URL,
        title="Data Engineer",
        employer="Acme",
        body_text="Join our Berlin data team.",
        searched_titles=["Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert (outcome, reason) == (CapturedUrlOutcome.HELD_OUT, FunnelReason.HARD_CONSTRAINT_EXCLUDED)
    assert detail == "The staged location is outside your current places: Germany."


def test_an_exact_title_match_at_an_excluded_employer_is_screened_out() -> None:
    """Title evidence never outranks an employer the person excluded."""
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
        employers=Employers(excluded_employers=["Globex"]),
    )

    outcome, staged_basis, detail, _reason, _findings = _exclusion_decision(
        parameters, "Staff Data Engineer", "Globex Corporation"
    )

    assert (outcome, staged_basis) == (CapturedUrlOutcome.SCREENED_OUT, None)
    assert detail == "The employer is excluded by the current settings."


def test_a_posting_url_key_does_not_raise_for_an_unparseable_url() -> None:
    """A malformed captured URL still gets a stable 64-character digest."""
    key = posting_url_key("https://[::1")

    assert len(key) == 64
    assert key == posting_url_key("https://[::1")


def test_an_unparseable_url_is_policy_refused_without_a_fetch() -> None:
    """A malformed captured URL is refused by policy rather than crashing admission."""
    item = ObservedUrl(
        url="https://[::1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _FetcherOnlyScans(),  # type: ignore[arg-type]
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.POLICY_REFUSED
    assert result.detail == "unreadable_url"


@dataclass
class _ListingsRead:
    """Just enough of a source-scan result for the board-routing tests."""

    listings: list[SourceListing]


class _CountingBoardFetcher:
    """Answers every board root read with the same listing set, or a failure, counting the reads."""

    def __init__(self, listings: list[SourceListing], error: Exception | None = None) -> None:
        self._listings = listings
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> _ListingsRead:
        self.calls.append((employer, url))
        if self._error is not None:
            raise self._error
        return _ListingsRead(listings=self._listings)


def _board_scans(listings_fetcher: _CountingBoardFetcher, posting_fetcher: Any = None) -> Any:
    return SimpleNamespace(
        fetcher=listings_fetcher,
        postings=SimpleNamespace(fetcher=posting_fetcher or _ExplosivePostingFetcher()),
    )


def _postings_scans(posting_fetcher: Any) -> Any:
    return SimpleNamespace(postings=SimpleNamespace(fetcher=posting_fetcher))


def _unresolvable_fetcher() -> PolitePostingFetcher:
    """The real polite fetcher over a resolver that reaches no public address."""

    def resolve(host: str, port: int) -> list[tuple[object, ...]]:
        del host, port
        raise gaierror("synthetic DNS failure")

    def get(url: str, **_kwargs: object) -> httpx.Response:
        raise AssertionError(f"an unresolvable host must not be contacted: {url}")

    return PolitePostingFetcher(
        client=PoliteHttpClient(user_agent="tailorcv/test", getter=get, resolver=resolve)
    )


def test_an_unresolvable_captured_url_is_unreadable_and_the_pass_continues() -> None:
    """One host that answers no address leaves the rest of the captured URLs decided."""
    unresolvable = ObservedUrl(
        url="https://missing.example.test/jobs/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    uncaptured = ObservedUrl(
        url="https://jobs.example.test/phantom",
        observations=(),
        model_selected=True,
        model_why="looks relevant",
    )

    read = read_captured_urls(
        _postings_scans(_unresolvable_fetcher()),
        parameters=JobSearchParameters(),
        searched_titles=[],
        captured=(unresolvable, uncaptured),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert [item.outcome for item in read] == [
        CapturedUrlOutcome.UNREADABLE,
        CapturedUrlOutcome.UNRESOLVED,
    ]
    assert read[0].detail == UNRESOLVED_DESTINATION_NOTE


_STAGING_TITLE = "Staff Data Engineer"


def _matching_listing(url: str, employer: str = "") -> SourceListing:
    """A board listing whose title matches the sole searched title, so it stages."""
    return SourceListing(employer=employer, title=_STAGING_TITLE, url=url)


def _screened_out_listing(url: str, employer: str = "") -> SourceListing:
    """A board listing with no matching title evidence, so screening refuses it."""
    return SourceListing(employer=employer, title="Warehouse Associate", url=url)


def test_a_search_sourced_board_root_the_model_named_is_admitted_when_it_lists_openings() -> None:
    """A model-named board root whose listings include a staging match admits on the read pass."""
    listing = _matching_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/1", employer="Examplehealth Inc"
    )
    listings_fetcher = _CountingBoardFetcher([listing])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(listings_fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.classification == CapturedUrlClassification.BOARD
    assert result.outcome == CapturedUrlOutcome.BOARD_ADMITTED
    assert result.board_root == "https://job-boards.greenhouse.io/examplehealth"
    # The board's name comes from the listing's own employer, not the slug.
    assert result.board_employer == "Examplehealth Inc"
    assert [candidate.url for candidate in result.board_listings] == [listing.url]
    assert listings_fetcher.calls == [("", "https://job-boards.greenhouse.io/examplehealth")]


def test_a_named_board_stages_no_more_listings_than_a_board_scan_would() -> None:
    listings = [
        _matching_listing(f"https://job-boards.greenhouse.io/examplehealth/jobs/{index}")
        for index in range(SOURCE_STAGE_LIMIT + 5)
    ]
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(_CountingBoardFetcher(listings)),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert len(result.board_listings) == SOURCE_STAGE_LIMIT


def test_a_search_sourced_board_root_with_no_openings_is_unresolved() -> None:
    """A model-named board root that lists nothing stays unresolved, not admitted."""
    listings_fetcher = _CountingBoardFetcher([])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(listings_fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == BOARD_LISTINGS_NOT_STAGED_DETAIL


def test_a_named_root_whose_only_postings_screen_out_is_not_admitted() -> None:
    """Reading a board's listings is not enough; a title-only match must survive screening."""
    listing = _screened_out_listing("https://job-boards.greenhouse.io/examplehealth/jobs/2")
    listings_fetcher = _CountingBoardFetcher([listing])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(listings_fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == BOARD_LISTINGS_NOT_STAGED_DETAIL
    assert result.board_listings == ()
    assert len(listings_fetcher.calls) == 1


def test_a_model_named_board_without_a_search_source_observation_is_not_routed() -> None:
    """An open-page observation earns a fetch, but only a search result routes a board."""
    fetcher = _RecordingPostingFetcher()
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.OPEN_PAGE, ""),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _postings_scans(fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.classification == CapturedUrlClassification.OTHER
    assert result.outcome == CapturedUrlOutcome.UNREADABLE
    assert fetcher.calls == [item.url]


def _converging_board_branches() -> tuple[ObservedUrl, ObservedUrl]:
    """Two captures of one board identity, as two branches of a single run produce them."""
    return (
        ObservedUrl(
            url="https://boards.greenhouse.io/examplehealth",
            observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
            model_selected=False,
            model_why="",
            model_board=True,
        ),
        ObservedUrl(
            url="https://boards.greenhouse.io/examplehealth?utm_source=campaign",
            observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
            model_selected=False,
            model_why="",
            model_board=True,
        ),
    )


def test_a_board_identity_is_read_at_most_once_per_admission_pass() -> None:
    """Two captures of the same board identity share one listings read."""
    listing = _matching_listing("https://job-boards.greenhouse.io/examplehealth/jobs/1")
    listings_fetcher = _CountingBoardFetcher([listing])
    scans = _board_scans(listings_fetcher)

    results = read_captured_urls(
        scans,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=_converging_board_branches(),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert [read.outcome for read in results] == [
        CapturedUrlOutcome.BOARD_ADMITTED,
        CapturedUrlOutcome.BOARD_ADMITTED,
    ]
    assert len(listings_fetcher.calls) == 1


class _IntentRecordingFetcher:
    """Records the intent each fetch received and answers a readable posting."""

    def __init__(self, robots_basis: RobotsReadBasis = RobotsReadBasis.ALLOWED) -> None:
        self.calls: list[tuple[str, PostingReadIntent]] = []
        self._robots_basis = robots_basis

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        self.calls.append((url, intent))
        return FetchedPosting(
            text="We are hiring a Staff Data Engineer.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            robots_basis=self._robots_basis,
            page_classification=PageClassification.POSTING,
        )


def test_an_admitted_board_is_named_by_the_employer_its_staged_posting_states() -> None:
    """The board a staged ATS posting carries takes the posting's name, so commit finds one."""
    listing = _matching_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/1", employer="Examplehealth Inc"
    )
    listings_fetcher = _CountingBoardFetcher([listing])
    item = ObservedUrl(
        url="https://job-boards.greenhouse.io/examplehealth/jobs/1001",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _board_scans(listings_fetcher, _IntentRecordingFetcher()),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.STAGED
    assert (result.employer, result.board_employer) == ("Example Co", "Example Co")
    assert listings_fetcher.calls == [("", "https://job-boards.greenhouse.io/examplehealth")]


def test_two_recruitee_postings_from_one_tenant_share_its_one_board() -> None:
    """An apply address stages as its posting, and both postings name the tenant's board."""
    listings_fetcher = _CountingBoardFetcher(
        [_matching_listing("https://acme.recruitee.com/o/data-platform-lead")]
    )
    captured = tuple(
        ObservedUrl(
            url=url,
            observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
            model_selected=False,
            model_why="",
        )
        for url in (
            "https://acme.recruitee.com/o/staff-data-engineer",
            "https://acme.recruitee.com/o/senior-data-engineer/c/new",
        )
    )

    results = read_captured_urls(
        _board_scans(listings_fetcher, _IntentRecordingFetcher()),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=captured,
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert [read.classification for read in results] == [CapturedUrlClassification.ATS_DETAIL] * 2
    assert [read.staged_url for read in results] == [
        "https://acme.recruitee.com/o/staff-data-engineer",
        "https://acme.recruitee.com/o/senior-data-engineer",
    ]
    assert {read.board_root for read in results} == {"https://acme.recruitee.com"}
    assert board_identity_key(results[0].board_root) == "recruitee:acme"
    assert listings_fetcher.calls == [("", "https://acme.recruitee.com")]


def test_a_captured_posting_is_read_as_a_crawl_and_records_the_reads_basis() -> None:
    """A campaign capture is not an address the person typed, so robots.txt decides it."""
    fetcher = _IntentRecordingFetcher()
    item = ObservedUrl(
        url="https://jobs.example.test/roles/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    [read] = read_captured_urls(
        _postings_scans(fetcher),
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert fetcher.calls == [(item.url, PostingReadIntent.CRAWL)]
    assert read.robots_basis == RobotsReadBasis.ALLOWED


def test_should_stop_prevents_a_new_board_read_but_not_a_cached_one() -> None:
    """A cancellation callable is checked between board reads, not on a cache hit."""
    listing = _matching_listing("https://job-boards.greenhouse.io/examplehealth/jobs/1")
    listings_fetcher = _CountingBoardFetcher([listing])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(listings_fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
        should_stop=lambda: True,
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert listings_fetcher.calls == []


class _RecordingPostingFetcher:
    """Records every URL fetched and answers each as unreadable."""

    def __init__(self, *, status: str = "unsupported", note: str = "") -> None:
        self.calls: list[str] = []
        self.status = status
        self.note = note

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        self.calls.append(url)
        return FetchedPosting(
            text=None,
            posting_status=self.status,
            method="test",
            fetched_at=utc_now(),
            verification_note=self.note,
        )


@pytest.mark.parametrize(
    ("note", "detail"),
    [
        ("Workday returned HTTP 403, errorCode S22.", "Workday returned HTTP 403, errorCode S22."),
        ("", "unverified"),
    ],
)
def test_an_unverified_read_records_why_the_publisher_did_not_answer(
    note: str, detail: str
) -> None:
    item = ObservedUrl(
        url="https://jobs.example.test/roles/123",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_RecordingPostingFetcher(status="unverified", note=note)),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNREADABLE
    assert result.detail == detail


class _UnreadablePostingFetcher:
    """Records one publisher read that fails content validation."""

    def __init__(self, error: Exception) -> None:
        self.calls: list[str] = []
        self.error = error

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        self.calls.append(url)
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        IngestError("Posting text is too short."),
        ValidationError("Posting refresh requires a resolvable public HTTP(S) source URL."),
    ],
)
def test_an_unreadable_captured_url_records_an_outcome(error: Exception) -> None:
    """One unreadable capture does not abort the whole campaign attempt."""
    fetcher = _UnreadablePostingFetcher(error)
    item = ObservedUrl(
        url="https://jobs.example.test/roles/123",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert fetcher.calls == [item.url]
    assert result.outcome == CapturedUrlOutcome.UNREADABLE
    assert result.detail == "posting could not be read safely"


def test_a_robots_refused_captured_url_records_a_policy_refused_outcome() -> None:
    """A publisher robots refusal retains its policy distinction in the campaign report."""
    item = ObservedUrl(
        url="https://jobs.example.test/roles/123",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    error = ValidationError("The posting source disallows this refresh in robots.txt.")
    error.__cause__ = RobotsDeniedError(item.url)
    fetcher = _UnreadablePostingFetcher(error)

    result = _read_one(
        _postings_scans(fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert fetcher.calls == [item.url]
    assert result.outcome == CapturedUrlOutcome.POLICY_REFUSED
    assert result.detail == "posting source policy refused the read"


def test_an_other_classified_page_action_url_is_fetched() -> None:
    """A real open-page or find-in-page observation is fetched even when not job-shaped."""
    fetcher = _RecordingPostingFetcher()
    item = ObservedUrl(
        url="https://jobs.example.test/announcements/2026-update",
        observations=((UrlActionKind.OPEN_PAGE, ""),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(fetcher),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert fetcher.calls == [item.url]
    assert result.outcome == CapturedUrlOutcome.UNREADABLE


class _RedirectingPostingFetcher:
    """Answers every fetch as readable, landing at a caller-chosen final URL."""

    def __init__(self, final_url: str) -> None:
        self._final_url = final_url

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="We are hiring a Staff Data Engineer.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Staff Data Engineer",
            employer="Example Co",
            final_url=self._final_url,
        )


# A tracking link that carries none of the eventual ATS shape itself, so a
# correct outcome can only come from reading ``FetchedPosting.final_url``.
_TRACKING_CAPTURE_URL = "https://jobs.example.test/careers/job/abc123"
_LANDED_LEVER_URL = "https://jobs.lever.co/examplehealth/11111111-1111-4111-8111-111111111111"


def test_a_redirecting_capture_stages_the_landed_ats_detail_url() -> None:
    """Staging uses the redirect target's URL, never the tracking capture."""
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(fetcher=_RedirectingPostingFetcher(_LANDED_LEVER_URL)),
    )
    item = ObservedUrl(
        url=_TRACKING_CAPTURE_URL,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        scans,
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.STAGED
    assert result.staged_url == _LANDED_LEVER_URL


def test_a_redirect_to_a_policy_refused_host_is_policy_refused() -> None:
    """A redirect that lands on a refused host is refused, even though the capture was clean."""
    final_url = "http://169.254.169.254/latest/meta-data/"
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(fetcher=_RedirectingPostingFetcher(final_url))
    )
    item = ObservedUrl(
        url=_TRACKING_CAPTURE_URL,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        scans,
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.POLICY_REFUSED


def test_a_redirect_to_an_already_known_posting_is_known_posting() -> None:
    """A redirect that lands on a fingerprint already known is known_posting, not staged again."""
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(fetcher=_RedirectingPostingFetcher(_LANDED_LEVER_URL))
    )
    item = ObservedUrl(
        url=_TRACKING_CAPTURE_URL,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        scans,
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset({posting_url_key(_LANDED_LEVER_URL)}),
    )

    assert result.outcome == CapturedUrlOutcome.KNOWN_POSTING


def test_an_expired_linkedin_posting_redirected_to_search_results_is_never_staged() -> None:
    """The capture is one posting, but the read landed on a collection with a matching title."""
    final_url = "https://www.linkedin.com/jobs/staff-data-engineer-jobs?trk=expired_jd_redirect"
    scans: Any = SimpleNamespace(
        postings=SimpleNamespace(fetcher=_RedirectingPostingFetcher(final_url))
    )
    item = ObservedUrl(
        url="https://ca.linkedin.com/jobs/view/staff-data-engineer-at-example-co-4400000001",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        scans,
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == guided_campaign_admission.LINKEDIN_SEARCH_RESULTS_DETAIL


class _FakeStaging:
    """Answers ``stage_found_posting`` from a scripted queue of results."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[str] = []

    def stage_found_posting(self, workspace_id: str, url: str, today: date, **_kwargs: Any) -> Any:
        self.calls.append(url)
        return self._results.pop(0)


class _FakeAdmissionScans:
    """Just enough of ``SourceScanService`` for the ``admit_read_urls`` write phase."""

    def __init__(self, staged_results: list[Any]) -> None:
        self.staging = _FakeStaging(staged_results)
        self.remembered_roots: list[str] = []

    def remember_guided_board_in_session(
        self,
        session: Any,
        workspace_id: str,
        employer: str,
        root: str,
        *,
        enabled: bool,
        correlation_id: str | None,
    ) -> str:
        self.remembered_roots.append(root)
        return "source-1"


@dataclass(frozen=True)
class _StagedResult:
    pending_discovery_id: str | None


def _known(source_roots: frozenset[str] = frozenset()) -> Any:
    return SimpleNamespace(source_roots=source_roots, posting_fingerprints=frozenset())


def test_a_new_staged_posting_admits_its_unregistered_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A posting staged new carries its readable, unregistered board to admission."""
    monkeypatch.setattr(
        guided_campaign_admission, "known_identities_in", lambda session, workspace_id: _known()
    )
    scans = _FakeAdmissionScans(staged_results=[_StagedResult(pending_discovery_id="pending-1")])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth/jobs/1001",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    posting = FetchedPosting(
        text="We are hiring.",
        posting_status="active",
        method="test",
        fetched_at=utc_now(),
        title="Staff Data Engineer",
        employer="Example Health",
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.ATS_DETAIL,
        CapturedUrlOutcome.STAGED,
        StagedBasis.TITLE_EVIDENCE,
        "",
        posting,
        "https://job-boards.greenhouse.io/examplehealth",
        "Example Health",
        item.url,
    )

    ([result], _written) = admit_read_urls(
        object(),  # type: ignore[arg-type]
        scans,  # type: ignore[arg-type]
        workspace_id="workspace-1",
        today=date(2026, 1, 1),
        correlation_id=None,
        reads=(read,),
    )

    assert isinstance(result, AdmissionResult)
    assert result.pending_discovery_ids == ("pending-1",)
    assert result.source_id == "source-1"
    assert scans.remembered_roots == ["https://job-boards.greenhouse.io/examplehealth"]


def test_a_deduped_posting_admits_no_board(monkeypatch: pytest.MonkeyPatch) -> None:
    """A posting that dedupes to a known job never admits its board."""
    monkeypatch.setattr(
        guided_campaign_admission, "known_identities_in", lambda session, workspace_id: _known()
    )
    scans = _FakeAdmissionScans(staged_results=[None])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth/jobs/1001",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    posting = FetchedPosting(
        text="We are hiring.",
        posting_status="active",
        method="test",
        fetched_at=utc_now(),
        title="Staff Data Engineer",
        employer="Example Health",
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.ATS_DETAIL,
        CapturedUrlOutcome.STAGED,
        StagedBasis.TITLE_EVIDENCE,
        "",
        posting,
        "https://job-boards.greenhouse.io/examplehealth",
        "Example Health",
        item.url,
    )

    ([result], _written) = admit_read_urls(
        object(),  # type: ignore[arg-type]
        scans,  # type: ignore[arg-type]
        workspace_id="workspace-1",
        today=date(2026, 1, 1),
        correlation_id=None,
        reads=(read,),
    )

    assert result.outcome == CapturedUrlOutcome.KNOWN_POSTING
    assert result.pending_discovery_ids == ()
    assert result.source_id is None
    assert scans.remembered_roots == []


def test_a_board_admitted_read_that_duplicates_an_already_admitted_board_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board another read in this pass already admitted dedupes to registered."""
    monkeypatch.setattr(
        guided_campaign_admission,
        "known_identities_in",
        lambda session, workspace_id: _known(frozenset({"greenhouse:examplehealth"})),
    )
    scans = _FakeAdmissionScans(staged_results=[])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.BOARD_ADMITTED,
        board_root="https://job-boards.greenhouse.io/examplehealth",
        board_employer="Examplehealth",
    )

    ([result], _written) = admit_read_urls(
        object(),  # type: ignore[arg-type]
        scans,  # type: ignore[arg-type]
        workspace_id="workspace-1",
        today=date(2026, 1, 1),
        correlation_id=None,
        reads=(read,),
    )

    assert result.outcome == CapturedUrlOutcome.REGISTERED_BOARD
    assert result.source_id is None
    assert scans.remembered_roots == []


def _matching_candidate(url: str) -> BoardListingCandidate:
    """A board listing candidate the read phase already screened as stageable."""
    return BoardListingCandidate(
        url=url,
        title="Staff Data Engineer",
        employer="Examplehealth",
        location="",
        facts=StagedListingFacts(),
    )


def test_a_board_admitted_read_for_a_new_board_stages_its_candidates_and_writes_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new board read stages its candidates, then writes the source."""
    monkeypatch.setattr(
        guided_campaign_admission, "known_identities_in", lambda session, workspace_id: _known()
    )
    candidate = _matching_candidate("https://job-boards.greenhouse.io/examplehealth/jobs/1")
    scans = _FakeAdmissionScans(staged_results=[_StagedResult(pending_discovery_id="pending-9")])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.BOARD_ADMITTED,
        board_root="https://job-boards.greenhouse.io/examplehealth",
        board_employer="Examplehealth",
        board_listings=(candidate,),
    )

    ([result], _written) = admit_read_urls(
        object(),  # type: ignore[arg-type]
        scans,  # type: ignore[arg-type]
        workspace_id="workspace-1",
        today=date(2026, 1, 1),
        correlation_id=None,
        reads=(read,),
    )

    assert result.outcome == CapturedUrlOutcome.BOARD_ADMITTED
    assert result.pending_discovery_ids == ("pending-9",)
    assert result.source_id == "source-1"
    assert scans.staging.calls == [candidate.url]
    assert scans.remembered_roots == ["https://job-boards.greenhouse.io/examplehealth"]


def _campaign_scans(database: Database) -> SourceScanService:
    """A real scan service for the admission write phase, with no posting fetches."""
    return SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )


def test_a_listing_taken_off_a_board_tells_the_queue_the_campaign_read_a_board(
    durable_database: Database,
) -> None:
    """A row harvested from a board page must not claim the campaign proposed it."""
    workspace_id = scan_workspace(durable_database)
    scans = _campaign_scans(durable_database)
    candidate = _matching_candidate("https://job-boards.greenhouse.io/examplehealth/jobs/1")
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.BOARD_ADMITTED,
        board_root="https://job-boards.greenhouse.io/examplehealth",
        board_employer="Examplehealth",
        board_listings=(candidate,),
    )

    with durable_database.session() as session:
        ([result], _written) = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=None,
            reads=(read,),
        )

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, result.pending_discovery_ids[0])
        assert row is not None
        assert row.reason == CAMPAIGN_BOARD_LISTING_REASON
        assert "listed on it" in row.reason


def test_a_posting_the_campaign_captured_itself_keeps_the_proposed_directly_reason(
    durable_database: Database,
) -> None:
    """A campaign's own captured posting keeps the sentence that is true for that path."""
    workspace_id = scan_workspace(durable_database)
    scans = _campaign_scans(durable_database)
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth/jobs/2002",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    posting = FetchedPosting(
        text="We are hiring.",
        posting_status="active",
        method="test",
        fetched_at=utc_now(),
        title="Staff Data Engineer",
        employer="Example Health",
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.ATS_DETAIL,
        CapturedUrlOutcome.STAGED,
        StagedBasis.TITLE_EVIDENCE,
        "",
        posting,
        "https://job-boards.greenhouse.io/examplehealth",
        "Example Health",
        item.url,
    )

    with durable_database.session() as session:
        ([result], _written) = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=None,
            reads=(read,),
        )

    with durable_database.session() as session:
        row = session.get(PendingDiscovery, result.pending_discovery_ids[0])
        assert row is not None
        assert row.reason == CAMPAIGN_FOUND_POSTING_REASON


_SLUG_BOARD = "https://boards.greenhouse.io/northwindhealth"
_SLUG_POSTING = "https://job-boards.greenhouse.io/northwindhealth/jobs/1001"
_SPELLED = "Northwind Health builds clinical data tools for regional hospitals."


def _admitted_names(
    database: Database, workspace_id: str, pending_ids: tuple[str, ...]
) -> set[str]:
    """Return every name the admitted rows, their employers, and their sources carry."""
    with database.session() as session:
        rows = {session.get_one(PendingDiscovery, item).employer for item in pending_ids}
        employers = set(
            session.scalars(select(Employer.name).where(Employer.workspace_id == workspace_id))
        )
        sources = set(
            session.scalars(select(Source.name).where(Source.workspace_id == workspace_id))
        )
    return rows | employers | sources


def _admit(database: Database, reads: tuple[ReadUrl, ...]) -> tuple[str, AdmissionResult]:
    workspace_id = scan_workspace(database)
    with database.session() as session:
        ([result], _written) = admit_read_urls(
            session,
            _campaign_scans(database),
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=None,
            reads=reads,
        )
    return workspace_id, result


def test_a_slug_only_board_listing_carries_its_descriptions_spelling_everywhere(
    durable_database: Database,
) -> None:
    """The staged row, the employer, and the source take one name, from the publisher's text."""
    listing = SourceListing(employer="", title=_STAGING_TITLE, url=_SLUG_POSTING, summary=_SPELLED)
    item = ObservedUrl(
        url=_SLUG_BOARD,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    reads = read_captured_urls(
        _board_scans(_CountingBoardFetcher([listing])),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    workspace_id, result = _admit(durable_database, reads)

    assert result.outcome == CapturedUrlOutcome.BOARD_ADMITTED
    assert _admitted_names(durable_database, workspace_id, result.pending_discovery_ids) == {
        "Northwind Health"
    }
    assert _stored_trust(durable_database, result.pending_discovery_ids) == {
        ListingIdentityTrust.PUBLISHER
    }


_NETWORK_BOARD = "https://example-ventures.getro.com/jobs"


def _network_board_reads(
    *employers: str, screened_out: tuple[str, ...] = ()
) -> tuple[ReadUrl, ...]:
    listings = [
        _matching_listing(f"https://jobs.example-{index}.test/jobs/{index}", employer=employer)
        for index, employer in enumerate(employers)
    ] + [
        _screened_out_listing(f"https://jobs.example-out-{index}.test/jobs/{index}", employer=name)
        for index, name in enumerate(screened_out)
    ]
    item = ObservedUrl(
        url=_NETWORK_BOARD,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    return read_captured_urls(
        _board_scans(_CountingBoardFetcher(listings)),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )


def test_a_board_listing_several_employers_stages_them_and_registers_no_source(
    durable_database: Database,
) -> None:
    reads = _network_board_reads("Example Bio", "Example Labs")

    workspace_id, result = _admit(durable_database, reads)

    assert reads[0].board_employer == ""
    assert len(result.pending_discovery_ids) == 2
    assert result.source_id is None
    with durable_database.session() as session:
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id))
        assert list(sources) == []


def test_a_board_whose_screened_out_listings_name_other_employers_registers_no_source(
    durable_database: Database,
) -> None:
    reads = _network_board_reads("Example Bio", screened_out=("Example Labs", "Example Tx"))

    workspace_id, result = _admit(durable_database, reads)

    assert len(result.pending_discovery_ids) == 1
    assert result.source_id is None
    with durable_database.session() as session:
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id))
        assert list(sources) == []


def test_a_second_capture_of_an_unregistered_network_root_is_not_admitted_again(
    durable_database: Database,
) -> None:
    reads = _network_board_reads("Example Bio", "Example Labs")
    workspace_id = scan_workspace(durable_database)

    with durable_database.session() as session:
        (first, second), _written = admit_read_urls(
            session,
            _campaign_scans(durable_database),
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=None,
            reads=(reads[0], reads[0]),
        )

    assert len(first.pending_discovery_ids) == 2
    assert second.pending_discovery_ids == ()
    assert second.outcome == CapturedUrlOutcome.REGISTERED_BOARD


@pytest.mark.parametrize(
    ("employers", "board_employer"),
    [
        (("Example Bio", "Example Bio"), "Example Bio"),
        (("Example Bio", "EXAMPLE BIO"), "Example Bio"),
        (("Example Bio", "Example Bio, Inc."), "Example Bio"),
        (("Example Bio", "Northwind Labs"), ""),
    ],
    ids=["same", "case", "legal-form", "two-employers"],
)
def test_a_board_registers_its_source_only_when_it_names_one_employer(
    durable_database: Database, employers: tuple[str, str], board_employer: str
) -> None:
    reads = _network_board_reads(*employers)

    _workspace_id, result = _admit(durable_database, reads)

    assert result.outcome == CapturedUrlOutcome.BOARD_ADMITTED
    assert reads[0].board_employer == board_employer
    assert (result.source_id is not None) == bool(board_employer)


def _stored_trust(database: Database, pending_ids: tuple[str, ...]) -> set[ListingIdentityTrust]:
    with database.session() as session:
        return {
            StagedListingFacts.from_json(
                session.get_one(PendingDiscovery, item).staged_facts
            ).identity_trust
            for item in pending_ids
        }


def _unnamed_posting_read(text: str) -> ReadUrl:
    """Read one captured posting whose publisher states a title and no employer."""

    class _UnnamedPosting:
        def fetch(
            self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
        ) -> FetchedPosting:
            return FetchedPosting(
                text=text,
                posting_status="active",
                method="test",
                fetched_at=utc_now(),
                title=_STAGING_TITLE,
                employer="",
                robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
                page_classification=PageClassification.POSTING,
                publisher_check=PostingCheck(
                    status="readable", note="", platform="greenhouse", title=_STAGING_TITLE
                ),
            )

    item = ObservedUrl(
        url=_SLUG_POSTING,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    board = _CountingBoardFetcher(
        [_matching_listing(f"{_SLUG_BOARD}/jobs/2", employer="Northwindhealth")]
    )
    return _read_one(
        _board_scans(board, _UnnamedPosting()),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )


def test_a_staged_posting_naming_no_employer_carries_its_bodys_spelling_everywhere(
    durable_database: Database,
) -> None:
    read = _unnamed_posting_read(f"{_SPELLED} We are hiring a Staff Data Engineer.")

    workspace_id, result = _admit(durable_database, (read,))

    assert result.outcome == CapturedUrlOutcome.STAGED
    assert result.source_id is not None
    assert _admitted_names(durable_database, workspace_id, result.pending_discovery_ids) == {
        "Northwind Health"
    }
    assert _stored_trust(durable_database, result.pending_discovery_ids) == {
        ListingIdentityTrust.PUBLISHER
    }


def test_a_staged_posting_named_only_by_its_tenant_is_stored_as_an_unverified_hint(
    durable_database: Database,
) -> None:
    read = _unnamed_posting_read("We are hiring a Staff Data Engineer.")

    workspace_id, result = _admit(durable_database, (read,))

    assert _admitted_names(durable_database, workspace_id, result.pending_discovery_ids) == {
        "Northwindhealth"
    }
    assert _stored_trust(durable_database, result.pending_discovery_ids) == {
        ListingIdentityTrust.UNVERIFIED_HINT
    }


def test_a_listing_on_a_tenant_named_board_outside_the_saved_places_is_refused() -> None:
    """Flagging the slug name must not switch off the listing's own place refusal."""
    parameters = JobSearchParameters(
        locations=[Location(key="home", country="Freedonia")],
        countries=[CountryNames(country="Freedonia"), CountryNames(country="Ruritania")],
        seniority=Seniority(target_titles=stated_titles(_STAGING_TITLE)),
    )
    listing = SourceListing(
        employer="",
        title=_STAGING_TITLE,
        url=_SLUG_POSTING,
        facts=StagedListingFacts(
            locations=PublishedFact(
                values=("Southport, Ruritania",), provenance="fictional.location", complete=True
            )
        ),
    )
    item = ObservedUrl(
        url=_SLUG_BOARD,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    read = _read_one(
        _board_scans(_CountingBoardFetcher([listing])),
        item=item,
        parameters=parameters,
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert read.board_listings == ()
    assert [outcome.reason for outcome in read.board_listing_outcomes] == [
        FunnelReason.LOCATION_OUTSIDE_SAVED_PLACES
    ]


def test_a_board_listing_named_only_by_its_tenant_stays_an_unverified_hint() -> None:
    listing = _matching_listing(_SLUG_POSTING)
    item = ObservedUrl(
        url=_SLUG_BOARD,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    read = _read_one(
        _board_scans(_CountingBoardFetcher([listing])),
        item=item,
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    [candidate] = read.board_listings
    assert (candidate.employer, read.board_employer) == ("Northwindhealth", "Northwindhealth")
    assert candidate.facts.identity_trust is ListingIdentityTrust.UNVERIFIED_HINT


def test_a_board_admitted_read_whose_candidates_all_dedupe_is_not_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board is only admitted once one of its screened candidates truly stages new."""
    monkeypatch.setattr(
        guided_campaign_admission, "known_identities_in", lambda session, workspace_id: _known()
    )
    candidate = _matching_candidate("https://job-boards.greenhouse.io/examplehealth/jobs/1")
    scans = _FakeAdmissionScans(staged_results=[None])
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    read = ReadUrl(
        item,
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.BOARD_ADMITTED,
        board_root="https://job-boards.greenhouse.io/examplehealth",
        board_employer="Examplehealth",
        board_listings=(candidate,),
    )

    ([result], _written) = admit_read_urls(
        object(),  # type: ignore[arg-type]
        scans,  # type: ignore[arg-type]
        workspace_id="workspace-1",
        today=date(2026, 1, 1),
        correlation_id=None,
        reads=(read,),
    )

    assert result.outcome == CapturedUrlOutcome.UNRESOLVED
    assert result.detail == BOARD_LISTINGS_NOT_STAGED_DETAIL
    assert result.pending_discovery_ids == ()
    assert result.source_id is None
    assert scans.remembered_roots == []


@pytest.mark.parametrize(
    ("staged_ids", "expected_ids", "expected_reasons"),
    [
        pytest.param(
            (None, None),
            (),
            (FunnelReason.ALREADY_KNOWN, FunnelReason.ALREADY_KNOWN),
            id="all-known",
        ),
        pytest.param(
            ("pending-1", "pending-1"),
            ("pending-1",),
            (FunnelReason.STAGED, FunnelReason.ALREADY_KNOWN),
            id="regional-copies",
        ),
        pytest.param(
            ("pending-1", "pending-2"),
            ("pending-1", "pending-2"),
            (FunnelReason.STAGED, FunnelReason.STAGED),
            id="distinct-postings",
        ),
        pytest.param(
            (None, "pending-2"),
            ("pending-2",),
            (FunnelReason.ALREADY_KNOWN, FunnelReason.STAGED),
            id="known-and-new",
        ),
    ],
)
def test_board_candidate_admission_records_each_regional_url_once(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    staged_ids: tuple[str | None, ...],
    expected_ids: tuple[str, ...],
    expected_reasons: tuple[FunnelReason, ...],
) -> None:
    """Regional listings keep their admission outcomes in the task's durable funnel."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    root = "https://job-boards.greenhouse.io/examplehealth"
    candidates = tuple(
        replace(_matching_candidate(f"{root}/jobs/{index}"), location=location)
        for index, location in enumerate(("Toronto, Canada", "Vancouver, Canada"), start=1)
    )
    read = ReadUrl(
        ObservedUrl(
            url=root,
            observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
            model_selected=False,
            model_why="",
            model_board=True,
        ),
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.BOARD_ADMITTED,
        board_root=root,
        board_employer="Examplehealth",
        board_listings=candidates,
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    staging = _FakeStaging(
        [_StagedResult(pending_id) if pending_id is not None else None for pending_id in staged_ids]
    )
    monkeypatch.setattr(scans.staging, "stage_found_posting", staging.stage_found_posting)

    with durable_database.session() as session:
        ([result], written) = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=(read,),
        )

    assert result.pending_discovery_ids == expected_ids
    assert result.outcome == (
        CapturedUrlOutcome.BOARD_ADMITTED if expected_ids else CapturedUrlOutcome.UNRESOLVED
    )
    if not expected_ids:
        assert result.detail == BOARD_LISTINGS_NOT_STAGED_DETAIL
        assert result.source_id is None
    else:
        assert result.source_id is not None
    paths = tuple(
        (
            FunnelStage.PROPOSED,
            FunnelStage.FETCHED,
            FunnelStage.READ,
            FunnelStage.SCREENED,
            *((FunnelStage.STAGED,) if reason == FunnelReason.STAGED else ()),
        )
        for reason in expected_reasons
    )
    listing_rows = [row for row in written if row.kind == FunnelCandidateKind.BOARD_LISTING]
    assert [
        (row.url, row.source, row.employer, row.title, row.location, row.reason, row.stages)
        for row in listing_rows
    ] == [
        (
            candidate.url,
            "job-boards.greenhouse.io",
            candidate.employer,
            candidate.title,
            candidate.location,
            reason,
            path,
        )
        for candidate, reason, path in zip(candidates, expected_reasons, paths, strict=True)
    ]

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome)
            .where(
                SourceScanOutcome.task_id == task_id,
                SourceScanOutcome.candidate_kind == "board_listing",
            )
            .order_by(SourceScanOutcome.url)
        ).all()
        sources = session.scalars(select(Source).where(Source.workspace_id == workspace_id)).all()

    assert [
        (
            row.url,
            row.source_name,
            row.employer,
            row.title,
            row.location,
            row.reason,
            row.terminal_stage,
            row.origin,
        )
        for row in rows
    ] == [
        (
            candidate.url,
            "job-boards.greenhouse.io",
            candidate.employer,
            candidate.title,
            candidate.location,
            reason.value,
            path[-1].value,
            "search.guided_campaign",
        )
        for candidate, reason, path in zip(candidates, expected_reasons, paths, strict=True)
    ]
    assert len(sources) == (1 if expected_ids else 0)


def _admitted_pending_discovery(
    database: Database, tmp_path: Path, master_cv: Path
) -> tuple[str, str]:
    """Build a workspace with one pending discovery a real guided campaign admitted."""
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="campaign-admission", name="Campaign admission")
        .id
    )
    parameter_set_id = (
        SearchParametersService(database)
        .save(
            workspace_id,
            JobSearchParameters(
                seniority=Seniority(target_titles=stated_titles("Data Platform Lead"))
            ),
        )
        .id
    )
    master_cv_version_id = (
        MasterCvService(database, runtime_storage(database, tmp_path / "artifacts"))
        .import_source(
            workspace_id=workspace_id, filename="CV_Master.md", content=master_cv.read_bytes()
        )
        .master_cv_version_id
    )
    pending_id = new_id()
    url = "https://boards.greenhouse.io/globex/jobs/1"
    with database.session() as session:
        session.add(
            PendingDiscovery(
                id=pending_id,
                workspace_id=workspace_id,
                fingerprint=new_id(),
                employer="Globex",
                title="Data Platform Lead",
                location="Remote",
                requisition_id="REQ-1",
                discovery_url=url,
                canonical_url=url,
                content_status=PostingContentAccessStatus.NOT_YET_READ,
                reason="A guided campaign staged this from a captured URL.",
                first_seen_on=utc_now().date(),
                created_at=utc_now(),
            )
        )
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type="search.guided_campaign",
            status="running",
            payload={},
            payload_sha256="0" * 64,
            idempotency_key="campaign-admission",
        )
        session.add(task)
        session.flush()
        task_id = task.id

    manifest = CampaignManifest(
        identities=CampaignIdentities(
            search_parameter_set_id=parameter_set_id,
            search_parameter_sha256="a" * 64,
            master_cv_version_id=master_cv_version_id,
            master_cv_sha256="a" * 64,
            accepted_vocabulary_sha256="a" * 64,
            admission_scope_sha256="a" * 64,
        ),
        compiler_version="gap-campaign-v1",
        branches=(),
        attempts=(CampaignAttemptPlan(position=1, branch_keys=(), search_cap=1),),
        bounds=CampaignBounds(),
    )
    ledger = GuidedCampaignLedger(database)
    campaign_id = ledger.create_or_claim(workspace_id, manifest, 1, task_id, {})
    position = manifest.attempts[0].position
    ledger.mark_dispatching(campaign_id, position, task_id, "a" * 64)
    ledger.mark_dispatched(
        campaign_id, position, task_id, "resp_1", "a" * 64, request_envelope=None
    )
    cost_ledger = CostLedger()
    cost_ledger.record(CallManifest(stage="guided_campaign", model="gpt-5.6-terra", usage=Usage()))
    url_sha256 = posting_url_key(url)
    ledger.capture(
        campaign_id,
        position,
        task_id,
        workspace_id=workspace_id,
        poll=BackgroundPoll(
            status="complete",
            value=None,
            record=CallRecord(usage=Usage(input_tokens=10, output_tokens=5)),
        ),
        ledger=cost_ledger,
        provider="openai",
        branch_queries={},
        has_unattributable_search=False,
        captured_urls=(
            {
                "url_sha256": url_sha256,
                "url": url,
                "classification": CapturedUrlClassification.ATS_DETAIL,
            },
        ),
    )
    with database.session() as session:
        GuidedCampaignLedger.record_admissions_in_session(
            session,
            campaign_id,
            position,
            task_id,
            (
                AdmissionResult(
                    url_sha256=url_sha256,
                    url=url,
                    classification=CapturedUrlClassification.ATS_DETAIL,
                    model_selected=False,
                    model_why="",
                    outcome=CapturedUrlOutcome.STAGED,
                    staged_basis=StagedBasis.TITLE_EVIDENCE,
                    pending_discovery_ids=(pending_id,),
                    source_id=None,
                    detail="",
                ),
            ),
        )
        # A finished campaign task must not itself guard the row, so only the
        # admission record is left protecting it.
        task_row = session.get(BackgroundTask, task_id)
        assert task_row is not None
        task_row.status = "completed"
    return workspace_id, pending_id


def test_a_campaign_admitted_discovery_survives_a_queue_reset(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A guided campaign's admission protects its discovery through a queue reset."""
    workspace_id, pending_id = _admitted_pending_discovery(durable_database, tmp_path, master_cv)

    with durable_database.session() as session:
        result = reset_pending_queue(session, workspace_id)

    assert result.archived_count == 0
    assert result.kept_count == 1
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.archived_at is None


def test_a_campaign_admitted_discovery_tells_the_queue_a_guided_campaign_staged_it(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A row no board scan staged is still named for the campaign that admitted it."""
    workspace_id, pending_id = _admitted_pending_discovery(durable_database, tmp_path, master_cv)

    response = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)

    assert response.staging_provenance == "guided_campaign"
    with durable_database.session() as session:
        row = session.get(PendingDiscovery, pending_id)
        assert row is not None
        assert row.source_id is None


def test_a_campaign_admitted_discovery_is_not_reported_as_kept_before_a_person_marks_it(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """A machine admission alone must not read as the person's keep marker."""
    workspace_id, pending_id = _admitted_pending_discovery(durable_database, tmp_path, master_cv)

    response = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)

    assert (response.kept, response.favorite) == (False, False)


def test_keeping_a_campaign_admitted_discovery_writes_the_persons_marker(
    durable_database: Database, tmp_path: Path, master_cv: Path
) -> None:
    """An explicit keep on a campaign-admitted row is recorded, not swallowed as already kept."""
    workspace_id, pending_id = _admitted_pending_discovery(durable_database, tmp_path, master_cv)
    note = "Reviewed against the current search parameters and master CV."

    with durable_database.session() as session:
        set_pending_markers(
            session, workspace_id, pending_id, favorite=False, kept=True, keep_note=note
        )

    response = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)
    assert (response.kept, response.keep_note) == (True, note)
    with durable_database.session() as session:
        event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == MARKERS_CHANGED_ACTION,
                AuditEvent.entity_id == pending_id,
            )
        )
        assert event is not None
        assert event.details["keep_note"] == note


def _funnel_task(database: Database, workspace_id: str) -> str:
    """Create the background task a funnel row's foreign key requires."""
    with database.session() as session:
        task = BackgroundTask(
            workspace_id=workspace_id,
            task_type="search.guided_campaign",
            status="running",
            payload={},
            payload_sha256="0" * 64,
            idempotency_key=new_id(),
        )
        session.add(task)
        session.flush()
        return task.id


def test_admit_read_urls_persists_a_funnel_row_for_a_staged_captured_url(
    durable_database: Database,
) -> None:
    """A directly captured posting the program read and staged earns one funnel row."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    item = ObservedUrl(
        url="https://jobs.example.test/roles/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    reads = read_captured_urls(
        _postings_scans(_IntentRecordingFetcher()),
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
        ).all()

    assert len(rows) == 1
    assert rows[0].candidate_kind == "direct_posting"
    assert rows[0].reason == "staged"
    assert rows[0].terminal_stage == "staged"
    assert rows[0].origin == "search.guided_campaign"


def test_admit_read_urls_accounts_for_a_board_lead_two_branches_converged_on(
    durable_database: Database,
) -> None:
    """Every captured board root either queues postings or earns a funnel row saying why."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    listing = _matching_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/1", employer="Examplehealth Inc"
    )
    reads = read_captured_urls(
        _board_scans(_CountingBoardFetcher([listing])),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=_converging_board_branches(),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        results, written = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    queued = [result for result in results if result.pending_discovery_ids]
    board_leads = [row for row in written if row.kind == FunnelCandidateKind.BOARD_LEAD]
    assert len(queued) < len(results)
    assert len(queued) + len(board_leads) == len(results)
    assert [row.reason for row in board_leads] == [FunnelReason.DUPLICATE_LEAD]
    assert [
        (row.url, row.reason) for row in written if row.kind == FunnelCandidateKind.BOARD_LISTING
    ] == [(listing.url, FunnelReason.STAGED)]


def _converging_board_funnel_reasons(
    database: Database, listings_fetcher: _CountingBoardFetcher
) -> list[FunnelReason]:
    """Admit two branches of one run that reach the same board root and return their reasons."""
    workspace_id = scan_workspace(database)
    task_id = _funnel_task(database, workspace_id)
    reads = read_captured_urls(
        _board_scans(listings_fetcher),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=_converging_board_branches(),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with database.session() as session:
        _results, written = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )
    return [row.reason for row in written]


def test_a_board_root_whose_listings_all_screen_out_earns_no_duplicate_lead_row(
    durable_database: Database,
) -> None:
    """A second branch reaching a root that proposed no lead records no duplicate rejection."""
    listing = _screened_out_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/2", employer="Examplehealth Inc"
    )

    reasons = _converging_board_funnel_reasons(durable_database, _CountingBoardFetcher([listing]))

    assert reasons == [FunnelReason.INSUFFICIENT_TITLE_EVIDENCE]


def test_a_board_root_whose_read_failed_earns_no_duplicate_lead_row(
    durable_database: Database,
) -> None:
    """A root neither branch could read proposed nothing, so nothing was dropped as a duplicate."""
    listings_fetcher = _CountingBoardFetcher([], httpx.ConnectError("synthetic board read failure"))

    reasons = _converging_board_funnel_reasons(durable_database, listings_fetcher)

    assert reasons == []
    assert len(listings_fetcher.calls) == 1


def test_two_staged_postings_on_an_unreadable_board_earn_no_duplicate_lead_row(
    durable_database: Database,
) -> None:
    """The board behind two staged postings was never admitted, so neither one duplicates it."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    captured = tuple(
        ObservedUrl(
            url=f"https://job-boards.greenhouse.io/examplehealth/jobs/{index}",
            observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
            model_selected=False,
            model_why="",
        )
        for index in (1001, 1002)
    )
    listings_fetcher = _CountingBoardFetcher([], httpx.ConnectError("synthetic board read failure"))
    reads = read_captured_urls(
        _board_scans(listings_fetcher, _IntentRecordingFetcher()),
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles(_STAGING_TITLE))
        ),
        searched_titles=[_STAGING_TITLE],
        captured=captured,
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        _results, written = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    assert [row.reason for row in written] == [FunnelReason.STAGED, FunnelReason.STAGED]


class _BelowBandPostingFetcher:
    """Answers every fetch with a readable posting below a configured seniority band."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text="Junior support role.",
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title="Associate Product Manager - Fictional Suite",
            employer="Example Cooperative",
            robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
            page_classification=PageClassification.POSTING,
        )


def test_admit_read_urls_names_the_specific_screening_reason_for_a_screened_out_url(
    durable_database: Database,
) -> None:
    """A publisher-named excluded employer remains a definite refusal after a read."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    item = ObservedUrl(
        url="https://jobs.example.test/roles/2",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    reads = read_captured_urls(
        _postings_scans(_BelowBandPostingFetcher()),
        parameters=JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles(*_SENIORITY_BAND_TARGETS), minimum="senior"
            ),
            employers=Employers(excluded_employers=["Example Cooperative"]),
        ),
        searched_titles=list(_SENIORITY_BAND_TARGETS),
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    with durable_database.session() as session:
        [row] = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
        ).all()

    assert row.reason == "hard_constraint_excluded"
    assert row.terminal_stage == "rejected"


def test_admit_read_urls_writes_no_funnel_row_for_a_policy_refused_url_never_read(
    durable_database: Database,
) -> None:
    """A captured URL policy refuses before any read leaves no funnel row behind."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    item = ObservedUrl(
        url="http://127.0.0.1/jobs/1",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    reads = read_captured_urls(
        _postings_scans(UnexpectedPostingFetcher()),
        parameters=JobSearchParameters(),
        searched_titles=[],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
        ).all()

    assert rows == []


def test_a_known_posting_decided_before_its_page_was_read_records_no_funnel_row(
    durable_database: Database,
) -> None:
    """A URL answered as already known before any read earns no row, because nothing read it."""
    workspace_id = scan_workspace(durable_database)
    task_id = _funnel_task(durable_database, workspace_id)
    url = "https://jobs.example.test/roles/3"
    item = ObservedUrl(
        url=url,
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )
    reads = read_captured_urls(
        _postings_scans(UnexpectedPostingFetcher()),
        parameters=JobSearchParameters(),
        searched_titles=[],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset({posting_url_key(url)}),
    )
    scans = SourceScanService(
        durable_database,
        storage=runtime_storage(durable_database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )

    with durable_database.session() as session:
        _results, offered = admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )

    with durable_database.session() as session:
        rows = session.scalars(
            select(SourceScanOutcome).where(SourceScanOutcome.task_id == task_id)
        ).all()

    assert [read.outcome for read in reads] == [CapturedUrlOutcome.KNOWN_POSTING]
    assert offered == ()
    assert rows == []


def _board_listing_funnel_rows(
    database: Database,
    listings: list[SourceListing],
    *,
    known_posting_fingerprints: frozenset[str] = frozenset(),
) -> list[SourceScanOutcome]:
    """Run one campaign pass over a model-named board root and return its board-listing rows."""
    workspace_id = scan_workspace(database)
    task_id = _funnel_task(database, workspace_id)
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )
    reads = read_captured_urls(
        _board_scans(_CountingBoardFetcher(listings)),
        parameters=JobSearchParameters(),
        searched_titles=[_STAGING_TITLE],
        captured=(item,),
        known_source_roots=frozenset(),
        known_posting_fingerprints=known_posting_fingerprints,
    )
    scans = SourceScanService(
        database,
        storage=runtime_storage(database),
        posting_fetcher=UnexpectedPostingFetcher(),
    )
    with database.session() as session:
        admit_read_urls(
            session,
            scans,
            workspace_id=workspace_id,
            today=date(2026, 1, 1),
            correlation_id=task_id,
            reads=reads,
        )
    with database.session() as session:
        return list(
            session.scalars(
                select(SourceScanOutcome).where(
                    SourceScanOutcome.task_id == task_id,
                    SourceScanOutcome.candidate_kind == "board_listing",
                )
            )
        )


def test_a_board_root_listing_already_known_is_recorded_in_the_funnel(
    durable_database: Database,
) -> None:
    """A listing the campaign skips because the posting is known still earns a funnel row."""
    listing = _matching_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/1", employer="Examplehealth Inc"
    )

    rows = _board_listing_funnel_rows(
        durable_database,
        [listing],
        known_posting_fingerprints=frozenset({posting_url_key(listing.url)}),
    )

    assert [(row.url, row.reason, row.terminal_stage) for row in rows] == [
        (listing.url, "already_known", "screened")
    ]


def test_a_board_root_listing_screening_refused_keeps_the_reason_screening_computed(
    durable_database: Database,
) -> None:
    """A listing screened out inside a board root keeps its own reason, not a generic one."""
    listing = _screened_out_listing(
        "https://job-boards.greenhouse.io/examplehealth/jobs/2", employer="Examplehealth Inc"
    )

    rows = _board_listing_funnel_rows(durable_database, [listing])

    assert [(row.url, row.reason) for row in rows] == [
        (listing.url, FunnelReason.INSUFFICIENT_TITLE_EVIDENCE.value)
    ]
    assert rows[0].employer == "Examplehealth Inc"


def test_a_board_root_listing_past_the_stage_limit_is_recorded_in_the_funnel(
    durable_database: Database,
) -> None:
    """A listing the per-source stage limit pushes past is recorded as rejected, not lost."""
    listings = [
        _matching_listing(
            f"https://job-boards.greenhouse.io/examplehealth/jobs/{index}",
            employer="Examplehealth Inc",
        )
        for index in range(SOURCE_STAGE_LIMIT + 1)
    ]

    rows = _board_listing_funnel_rows(durable_database, listings)

    assert sorted((row.url, row.reason, row.terminal_stage) for row in rows) == sorted(
        [(listing.url, "staged", "staged") for listing in listings[:SOURCE_STAGE_LIMIT]]
        + [(listings[-1].url, "stage_limit_reached", "rejected")]
    )


@pytest.mark.parametrize(
    ("title", "employer"),
    [("   ", "Example Co"), ("Staff Data Engineer", "jobmirror.example.test")],
)
def test_a_page_missing_a_usable_identity_is_filed_as_an_incomplete_posting(
    title: str, employer: str
) -> None:
    """Neither a missing title nor a self-named employer is a search refusal; both are gaps."""
    _outcome, _staged_basis, _detail, reason, _findings = _staging_decision(
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        facts=StagedListingFacts(),
        url=_MIRROR_POSTING_URL,
        title=title,
        employer=employer,
        body_text="Build reliable data systems for our platform.",
        searched_titles=["Staff Data Engineer"],
        model_selected=False,
        has_search_source_observation=False,
        publisher_read=False,
    )

    assert reason is FunnelReason.INCOMPLETE_POSTING


def test_a_page_that_is_not_one_posting_is_filed_as_an_incomplete_posting() -> None:
    """A collection page reached the read, so the funnel must not call it a fetch failure."""
    item = ObservedUrl(
        url="https://jobs.example.test/careers",
        observations=((UrlActionKind.OPEN_PAGE, ""),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_CollectionPageFetcher()),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer"))
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.funnel_reason is FunnelReason.INCOMPLETE_POSTING


class _ClinicalPostingFetcher:
    """Answers every fetch with a readable posting whose qualifications hold ``line``."""

    def __init__(self, line: str, title: str = "Staff Data Engineer") -> None:
        self.line = line
        self.title = title

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL
    ) -> FetchedPosting:
        return FetchedPosting(
            text=(
                f"{self.title}\n\nBuild reliable data systems for our clinics.\n\n"
                f"Qualifications\n- {self.line}\n- 5+ years of data engineering experience\n"
            ),
            posting_status="active",
            method="test",
            fetched_at=utc_now(),
            title=self.title,
            employer="Example Health Cooperative",
            robots_basis=RobotsReadBasis.SINGLE_POSTING_READ,
            page_classification=PageClassification.POSTING,
        )


_LICENCE_CONDITION = "Requires an active pharmacist or clinical practice license"


@pytest.mark.parametrize(
    ("qualification", "outcome"),
    [
        ("Experience maintaining clinical content platforms", CapturedUrlOutcome.STAGED),
        ("Active pharmacist license required", CapturedUrlOutcome.HELD_OUT),
    ],
)
def test_domain_only_title_review_retains_a_readable_posting_until_a_definite_body_refusal(
    qualification: str, outcome: CapturedUrlOutcome
) -> None:
    item = ObservedUrl(
        url="https://jobs.example.test/roles/domain-advisor",
        observations=((UrlActionKind.SEARCH_SOURCE, "Clinical Content jobs"),),
        model_selected=False,
        model_why="",
    )
    result = _read_one(
        _postings_scans(_ClinicalPostingFetcher(qualification, title="Clinical Content Advisor")),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(
                target_titles=stated_titles("Service Manager"),
                strong_domain_tokens=["Clinical Content"],
            ),
            excluded_conditions=[(_LICENCE_CONDITION, "not held")],
        ),
        searched_titles=["Service Manager"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome is outcome
    if outcome is CapturedUrlOutcome.STAGED:
        assert result.staged_basis is StagedBasis.TITLE_EVIDENCE
        assert result.posting is not None
        assert result.posting.text is not None and "Qualifications" in result.posting.text
    else:
        assert result.funnel_reason is FunnelReason.HARD_CONSTRAINT_EXCLUDED


@pytest.mark.parametrize(
    ("line", "outcome", "detail"),
    [
        (
            "Active pharmacist license required",
            CapturedUrlOutcome.HELD_OUT,
            f"the posting states an excluded condition: {_LICENCE_CONDITION}",
        ),
        ("PharmD is required", CapturedUrlOutcome.STAGED, ""),
        ("PhD, MD or PharmD required", CapturedUrlOutcome.STAGED, ""),
    ],
)
def test_a_captured_page_requiring_an_excluded_licence_is_held_out(
    line: str, outcome: CapturedUrlOutcome, detail: str
) -> None:
    """The credential gate relevance review runs also holds a captured page out of Pending."""
    item = ObservedUrl(
        url="https://jobs.example.test/roles/7",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
    )

    result = _read_one(
        _postings_scans(_ClinicalPostingFetcher(line)),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles("Staff Data Engineer")),
            excluded_conditions=[(_LICENCE_CONDITION, "not held")],
        ),
        searched_titles=["Staff Data Engineer"],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert (result.outcome, result.detail) == (outcome, detail)
    if outcome is CapturedUrlOutcome.HELD_OUT:
        assert result.funnel_reason is FunnelReason.HARD_CONSTRAINT_EXCLUDED


@pytest.mark.parametrize(
    "summary",
    [
        "Qualifications - Active pharmacist license required - 5+ years of data engineering",
        "Build reliable data systems. Requirements: Active pharmacist license required. "
        "5+ years of data engineering experience.",
        "Requirements\n- Active pharmacist license required\n- 5+ years of data engineering",
    ],
)
def test_a_board_listing_summary_naming_an_excluded_licence_still_stages(summary: str) -> None:
    """A board's collapsed summary is no publisher read, so the credential gate skips it."""
    listing = SourceListing(
        employer="Example Health Cooperative",
        title=_STAGING_TITLE,
        url="https://job-boards.greenhouse.io/examplehealth/jobs/7",
        summary=summary,
    )
    item = ObservedUrl(
        url="https://boards.greenhouse.io/examplehealth",
        observations=((UrlActionKind.SEARCH_SOURCE, "Staff Data Engineer jobs"),),
        model_selected=False,
        model_why="",
        model_board=True,
    )

    result = _read_one(
        _board_scans(_CountingBoardFetcher([listing])),
        item=item,
        parameters=JobSearchParameters(
            seniority=Seniority(target_titles=stated_titles(_STAGING_TITLE)),
            excluded_conditions=[(_LICENCE_CONDITION, "not held")],
        ),
        searched_titles=[_STAGING_TITLE],
        known_source_roots=frozenset(),
        known_posting_fingerprints=frozenset(),
    )

    assert result.outcome == CapturedUrlOutcome.BOARD_ADMITTED
    assert [candidate.url for candidate in result.board_listings] == [listing.url]
    assert result.board_listing_outcomes == ()
