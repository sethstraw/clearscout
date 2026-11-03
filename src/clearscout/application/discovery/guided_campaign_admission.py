"""Classify, read, and admit the URLs one campaign attempt captured.

Publisher reads run with no open transaction. ``admit_read_urls`` then writes
every staged posting and admitted board inside the caller's one transaction.
Staged facts come only from the publisher read.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date

import httpx
from sqlalchemy.orm import Session

from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.board_slugs import board_identity_key, employer_name
from clearscout.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelOutcome,
    FunnelReason,
    FunnelScreeningFinding,
    FunnelStage,
    bulk_staging_refusal_outcome,
    listing_outcome,
)
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_identity import posting_identity_keys, posting_url_key
from clearscout.application.discovery.scan_queries import refusal_text
from clearscout.application.discovery.scan_sweep import SOURCE_STAGE_LIMIT
from clearscout.application.discovery.screening import (
    BulkStagingRefusal,
    DiscoveryScreening,
    ScreeningStanding,
    body_states_saved_focus,
    bulk_staging_refusal,
    screen_discovery,
    structured_holdout,
)
from clearscout.application.discovery.search_planning_queries import known_identities_in
from clearscout.application.discovery.source_reading import PostingCheck, SourceListing
from clearscout.application.failures import ValidationError
from clearscout.application.tailoring import fit
from clearscout.domain.campaigns import (
    CapturedUrlClassification,
    CapturedUrlOutcome,
    StagedBasis,
    UrlActionKind,
)
from clearscout.domain.identity import (
    employer_name_words,
    url_host,
    url_path_segments,
    url_query_keys,
)
from clearscout.domain.records import PageClassification, RobotsReadBasis
from clearscout.domain.search_parameters import JobSearchParameters
from clearscout.domain.staged_facts import ListingIdentityTrust, StagedListingFacts
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import spells_the_page_host
from clearscout.infrastructure.web_fetch.boards import (
    board_identity,
    canonicalise_source_url,
    single_employer_detail_url,
    single_tenant_board,
    without_apply_suffix,
)
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    RobotsDeniedError,
    fetch_refusal,
)
from clearscout.shared.errors import IngestError

CAPTURE_ACTION_KINDS = frozenset(
    {UrlActionKind.SEARCH_SOURCE, UrlActionKind.OPEN_PAGE, UrlActionKind.FIND_IN_PAGE}
)
_PAGE_ACTION_KINDS = frozenset({UrlActionKind.OPEN_PAGE, UrlActionKind.FIND_IN_PAGE})
NOT_CAPTURED_DETAIL = "not in captured search output"
BOARD_LISTINGS_NOT_STAGED_DETAIL = "none of the board's postings staged"
NO_POSTING_TITLE_DETAIL = "page names no single posting title"
HOST_NAMED_AS_EMPLOYER_DETAIL = "page names its own host as the employer"
LINKEDIN_SEARCH_RESULTS_DETAIL = "LinkedIn jobs search-results collection, not one posting"
DUPLICATE_LEAD_DETAIL = "another branch of this run already reached the same board lead"
_JOB_PATH_SEGMENTS = frozenset(
    {
        "career",
        "careers",
        "job",
        "jobs",
        "opening",
        "openings",
        "position",
        "positions",
        "role",
        "roles",
        "vacancy",
        "vacancies",
    }
)
_JOB_QUERY_KEYS = frozenset({"job", "job_id", "jobid", "gh_jid", "opening_id", "requisition_id"})
# LinkedIn names a title search "<title>-jobs" and a title-and-place search
# "<title>-jobs-<place>"; one posting lives under /jobs/view/ instead.
_LINKEDIN_SEARCH_SLUG = re.compile(r".+-jobs(?:-.+)?")


def model_pick_refusal(url: str) -> str:
    """Return why a model-written URL pick may not be fetched, or an empty string."""
    return fetch_refusal(url)


@dataclass(frozen=True)
class ObservedUrl:
    """One URL an attempt captured, with the actions that surfaced it."""

    url: str
    observations: tuple[tuple[str, str], ...]
    model_selected: bool
    model_why: str
    model_board: bool = False


@dataclass(frozen=True)
class BoardListingCandidate:
    """One board-root listing that screening cleared to stage in the write phase."""

    url: str
    title: str
    employer: str
    location: str
    facts: StagedListingFacts


@dataclass(frozen=True)
class ReadUrl:
    """One captured URL's decision before any write, with the publisher read it rests on."""

    item: ObservedUrl
    classification: CapturedUrlClassification
    outcome: CapturedUrlOutcome
    staged_basis: StagedBasis | None = None
    detail: str = ""
    posting: FetchedPosting | None = None
    board_root: str = ""
    board_employer: str = ""
    staged_url: str = ""
    board_listings: tuple[BoardListingCandidate, ...] = ()
    # The funnel rows for the board-root listings this read decided about and did
    # not stage; empty on a root an earlier read in the same pass already screened.
    board_listing_outcomes: tuple[FunnelOutcome, ...] = ()
    robots_basis: RobotsReadBasis | None = None
    employer: str = ""
    title: str = ""
    funnel_reason: FunnelReason | None = None
    screening_findings: tuple[FunnelScreeningFinding, ...] = ()
    # Set when the read phase short-circuited a board root an earlier read in the
    # same pass already reached, so the admission phase can still record the drop.
    duplicate_lead_root: str = ""


@dataclass(frozen=True)
class AdmissionResult:
    """What processing decided for one captured URL."""

    url_sha256: str
    url: str
    classification: CapturedUrlClassification
    model_selected: bool
    model_why: str
    outcome: CapturedUrlOutcome
    staged_basis: StagedBasis | None
    pending_discovery_ids: tuple[str, ...]
    source_id: str | None
    detail: str
    robots_basis: RobotsReadBasis | None = None


def _linkedin_job_search_collection(url: str) -> bool:
    """Return whether a LinkedIn URL names its jobs search-results collection."""
    hostname = url_host(url)
    segments = tuple(segment.casefold() for segment in url_path_segments(url))
    return (
        (hostname == "linkedin.com" or hostname.endswith(".linkedin.com"))
        and len(segments) == 2
        and segments[0] == "jobs"
        and _LINKEDIN_SEARCH_SLUG.fullmatch(segments[1]) is not None
    )


def classify_url(
    url: str,
    *,
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
) -> tuple[CapturedUrlClassification, str]:
    """Return (classification, policy refusal) for one captured URL, before any fetch."""
    refusal = fetch_refusal(url)
    if refusal:
        return CapturedUrlClassification.OTHER, refusal
    if not posting_identity_keys(url).isdisjoint(known_posting_fingerprints):
        return (
            CapturedUrlClassification.ATS_DETAIL
            if single_employer_detail_url(url) is not None
            else CapturedUrlClassification.OTHER
        ), ""
    if board_identity_key(url) in known_source_roots:
        return CapturedUrlClassification.BOARD, ""
    if single_employer_detail_url(url) is not None:
        return CapturedUrlClassification.ATS_DETAIL, ""
    segments = tuple(segment.casefold() for segment in url_path_segments(url))
    query_keys = url_query_keys(url)
    if any(segment in _JOB_PATH_SEGMENTS for segment in segments[:-1]) or (
        _JOB_QUERY_KEYS & query_keys
    ):
        return CapturedUrlClassification.JOB_PAGE_CANDIDATE, ""
    return CapturedUrlClassification.OTHER, ""


def _screening_findings(screening: DiscoveryScreening) -> tuple[FunnelScreeningFinding, ...]:
    """Return a screening's non-matching findings as the funnel's retained detail."""
    return tuple(
        FunnelScreeningFinding(
            dimension=finding.dimension, code=finding.code, subject=finding.subject
        )
        for finding in screening.findings
        if finding.standing != ScreeningStanding.MATCH
    )


def _held_out_detail(
    facts: StagedListingFacts,
    screening: DiscoveryScreening,
    parameters: JobSearchParameters,
    body_text: str,
    *,
    publisher_read: bool,
) -> tuple[str, DiscoveryScreening] | None:
    """Return why a hard constraint holds this page out and the screening that records it.

    None when nothing does. The credential gate reads only a fetched publisher
    page, never a board's collapsed summary.
    """
    holdout = structured_holdout(facts, "", screening, parameters, body_text)
    if holdout is not None:
        return refusal_text(holdout.finding, parameters), holdout.screening
    if not publisher_read or not body_text.strip():
        return None
    credential = fit.excluded_condition(fit.read(body_text, parameters=parameters), parameters)
    if credential.standing is not fit.Standing.INELIGIBLE:
        return None
    return credential.reason, screening


_StagingDecision = tuple[
    CapturedUrlOutcome, StagedBasis | None, str, FunnelReason, tuple[FunnelScreeningFinding, ...]
]


def _staging_decision(
    *,
    parameters: JobSearchParameters,
    facts: StagedListingFacts,
    url: str,
    title: str,
    employer: str,
    body_text: str,
    searched_titles: list[str],
    model_selected: bool,
    has_search_source_observation: bool,
    publisher_read: bool,
) -> _StagingDecision:
    """Return (outcome, basis, detail, funnel reason, screening findings).

    ``employer`` is the name the read page declares, not a stored one, and a
    focus term or model pick replaces only title evidence.
    """
    if not title.strip():
        return (
            CapturedUrlOutcome.UNRESOLVED,
            None,
            NO_POSTING_TITLE_DETAIL,
            FunnelReason.INCOMPLETE_POSTING,
            (),
        )
    # A page outside a single-tenant board that declares its own host as the hiring
    # organization names no employer, so nothing downstream can resolve one.
    if spells_the_page_host(employer, url) and not single_tenant_board(url):
        return (
            CapturedUrlOutcome.SCREENED_OUT,
            None,
            HOST_NAMED_AS_EMPLOYER_DETAIL,
            FunnelReason.INCOMPLETE_POSTING,
            (),
        )
    screening = screen_discovery(employer, title, "", parameters, searched_titles, facts)
    findings = _screening_findings(screening)
    held_out = _held_out_detail(
        facts, screening, parameters, body_text, publisher_read=publisher_read
    )
    if held_out is not None:
        detail, recorded = held_out
        return (
            CapturedUrlOutcome.HELD_OUT,
            None,
            detail,
            FunnelReason.HARD_CONSTRAINT_EXCLUDED,
            _screening_findings(recorded),
        )
    refusal = bulk_staging_refusal(screening, include_bounded_review=True)
    if refusal is not None and refusal is not BulkStagingRefusal.INSUFFICIENT_TITLE_EVIDENCE:
        reason, detail = bulk_staging_refusal_outcome(refusal)
        return CapturedUrlOutcome.SCREENED_OUT, None, detail, reason, findings
    refused = next(
        (item for item in screening.findings if item.standing is ScreeningStanding.REFUSE), None
    )
    if refused is not None:
        return (
            CapturedUrlOutcome.SCREENED_OUT,
            None,
            refusal_text(refused, parameters),
            FunnelReason.HARD_CONSTRAINT_EXCLUDED,
            findings,
        )
    if refusal is None:
        return (
            CapturedUrlOutcome.STAGED,
            StagedBasis.TITLE_EVIDENCE,
            "",
            FunnelReason.STAGED,
            findings,
        )
    if body_states_saved_focus(parameters, body_text) is not None:
        return (
            CapturedUrlOutcome.STAGED,
            StagedBasis.BODY_FOCUS_TERM,
            "",
            FunnelReason.STAGED,
            findings,
        )
    if model_selected and has_search_source_observation:
        return (
            CapturedUrlOutcome.STAGED,
            StagedBasis.MODEL_SELECTED,
            "",
            FunnelReason.STAGED,
            findings,
        )
    reason, detail = bulk_staging_refusal_outcome(refusal)
    return CapturedUrlOutcome.SCREENED_OUT, None, detail, reason, findings


def read_captured_urls(
    scans: SourceScanService,
    *,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    captured: tuple[ObservedUrl, ...],
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
    should_stop: Callable[[], bool] | None = None,
) -> tuple[ReadUrl, ...]:
    """Classify and publisher-read every captured URL in the fixed order, writing nothing.

    A board identity is read at most once across the whole pass.
    """
    board_reads: dict[str, str] = {}
    board_listing_reads: dict[str, tuple[str, str, tuple[BoardListingCandidate, ...]]] = {}
    return tuple(
        _read_one(
            scans,
            item=item,
            parameters=parameters,
            searched_titles=searched_titles,
            known_source_roots=known_source_roots,
            known_posting_fingerprints=known_posting_fingerprints,
            board_reads=board_reads,
            board_listing_reads=board_listing_reads,
            should_stop=should_stop,
        )
        for item in captured
    )


def _screen_board_listings(
    listings: list[SourceListing],
    root: str,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    known_posting_fingerprints: frozenset[str],
) -> tuple[list[BoardListingCandidate], tuple[FunnelOutcome, ...]]:
    """Screen a board root's raw listings into the ones that would stage, plus funnel rows.

    Every listing this decides about and does not stage earns one row, so a
    listing the board root offered cannot leave the run uncounted.
    """
    board_name, tenant_only = employer_name(
        "",
        root,
        "\n".join(listing.published_description or listing.summary for listing in listings),
    )
    candidates: list[BoardListingCandidate] = []
    outcomes: list[FunnelOutcome] = []
    for listing in listings:
        employer = listing.employer or board_name
        named = replace(listing, employer=employer)
        source = _funnel_source(listing.url)
        if not posting_identity_keys(listing.url).isdisjoint(known_posting_fingerprints):
            outcomes.append(
                listing_outcome(
                    named, source=source, terminal=None, reason=FunnelReason.ALREADY_KNOWN
                )
            )
            continue
        outcome, _staged_basis, detail, reason, findings = _staging_decision(
            parameters=parameters,
            facts=listing.facts,
            url=listing.url,
            title=listing.title,
            employer=employer,
            body_text=listing.summary,
            searched_titles=searched_titles,
            model_selected=False,
            has_search_source_observation=False,
            publisher_read=False,
        )
        if outcome != CapturedUrlOutcome.STAGED:
            outcomes.append(
                listing_outcome(
                    named,
                    source=source,
                    terminal=None,
                    reason=reason,
                    detail=detail,
                    screening_findings=findings,
                )
            )
            continue
        if len(candidates) >= SOURCE_STAGE_LIMIT:
            outcomes.append(
                listing_outcome(
                    named,
                    source=source,
                    terminal=FunnelStage.REJECTED,
                    reason=FunnelReason.STAGE_LIMIT_REACHED,
                )
            )
            continue
        candidates.append(
            BoardListingCandidate(
                url=listing.url,
                title=listing.title,
                employer=employer,
                location=listing.location,
                # Screening judged the listing's own facts; only the stored row
                # carries the flag that its name came from the tenant alone.
                facts=(
                    replace(listing.facts, identity_trust=ListingIdentityTrust.UNVERIFIED_HINT)
                    if not listing.employer and tenant_only
                    else listing.facts
                ),
            )
        )
    return candidates, tuple(outcomes)


def _board_root_listing_candidates(
    scans: SourceScanService,
    url: str,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
    board_listing_reads: dict[str, tuple[str, str, tuple[BoardListingCandidate, ...]]],
    should_stop: Callable[[], bool] | None,
) -> tuple[str, str, tuple[BoardListingCandidate, ...], tuple[FunnelOutcome, ...]]:
    """Read a model-named board root once per pass and screen its listings into candidates.

    The funnel rows come back only from the read that screened them, so a root a
    second branch reaches contributes no duplicate listing rows.
    """
    root = canonicalise_source_url(url)
    identity = board_identity_key(root)
    if identity in known_source_roots:
        return "", "", (), ()
    if identity in board_listing_reads:
        return (*board_listing_reads[identity], ())
    if should_stop is not None and should_stop():
        return "", "", (), ()
    result: tuple[str, str, tuple[BoardListingCandidate, ...]]
    listing_outcomes: tuple[FunnelOutcome, ...] = ()
    try:
        read = scans.fetcher.listings("", root)
    except (httpx.HTTPError, FetchPolicyError, ValueError):
        result = ("", "", ())
    else:
        candidates, listing_outcomes = _screen_board_listings(
            read.listings, root, parameters, searched_titles, known_posting_fingerprints
        )
        # A board naming several employers is a network, not one employer's board.
        stated = [listing.employer for listing in read.listings if listing.employer] or [
            candidate.employer for candidate in candidates if candidate.employer
        ]
        named = {tuple(employer_name_words(name)) for name in stated}
        board_employer = stated[0] if len(named) == 1 else ""
        result = (root, board_employer, tuple(candidates)) if candidates else ("", "", ())
    board_listing_reads[identity] = result
    return (*result, listing_outcomes)


def _search_sourced_board_route(
    scans: SourceScanService,
    item: ObservedUrl,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
    board_listing_reads: dict[str, tuple[str, str, tuple[BoardListingCandidate, ...]]],
    should_stop: Callable[[], bool] | None,
) -> ReadUrl | None:
    """Route a search-captured board root the model named in ``boards[]`` to one listing read.

    None when the item is not such a root, so the ordinary classification path applies.
    """
    if not item.model_board:
        return None
    if not any(kind == UrlActionKind.SEARCH_SOURCE for kind, _ in item.observations):
        return None
    root = canonicalise_source_url(item.url)
    identity = board_identity_key(root)
    if board_identity(root) is None or identity in known_source_roots:
        return None
    # The cached read holds an empty root when the fetch failed or every listing screened
    # out, and a root nothing proposed is no lead for a second branch to duplicate.
    cached_read = board_listing_reads.get(identity)
    duplicate_lead_root = root if cached_read is not None and cached_read[0] else ""
    board_root, board_employer, candidates, listing_outcomes = _board_root_listing_candidates(
        scans,
        item.url,
        parameters,
        searched_titles,
        known_source_roots,
        known_posting_fingerprints,
        board_listing_reads,
        should_stop,
    )
    if board_root:
        return ReadUrl(
            item,
            CapturedUrlClassification.BOARD,
            CapturedUrlOutcome.BOARD_ADMITTED,
            board_root=board_root,
            board_employer=board_employer,
            board_listings=candidates,
            board_listing_outcomes=listing_outcomes,
            duplicate_lead_root=duplicate_lead_root,
        )
    return ReadUrl(
        item,
        CapturedUrlClassification.BOARD,
        CapturedUrlOutcome.UNRESOLVED,
        detail=BOARD_LISTINGS_NOT_STAGED_DETAIL,
        board_listing_outcomes=listing_outcomes,
        duplicate_lead_root=duplicate_lead_root,
    )


def _pre_fetch_outcome(
    scans: SourceScanService,
    item: ObservedUrl,
    classification: CapturedUrlClassification,
    refusal: str,
    url: str,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
    board_listing_reads: dict[str, tuple[str, str, tuple[BoardListingCandidate, ...]]],
    should_stop: Callable[[], bool] | None,
) -> ReadUrl | None:
    """Return a decision that needs no fetch, or None to proceed to the publisher read."""
    if refusal:
        return ReadUrl(item, classification, CapturedUrlOutcome.POLICY_REFUSED, detail=refusal)
    if not posting_identity_keys(url).isdisjoint(known_posting_fingerprints):
        return ReadUrl(item, classification, CapturedUrlOutcome.KNOWN_POSTING)
    if _linkedin_job_search_collection(url):
        return ReadUrl(
            item,
            classification,
            CapturedUrlOutcome.UNRESOLVED,
            detail=LINKEDIN_SEARCH_RESULTS_DETAIL,
        )
    if classification == CapturedUrlClassification.BOARD:
        return ReadUrl(item, classification, CapturedUrlOutcome.REGISTERED_BOARD)
    routed = _search_sourced_board_route(
        scans,
        item,
        parameters,
        searched_titles,
        known_source_roots,
        known_posting_fingerprints,
        board_listing_reads,
        should_stop,
    )
    if routed is not None:
        return routed
    if (
        classification == CapturedUrlClassification.OTHER
        and not item.model_selected
        and not any(kind in _PAGE_ACTION_KINDS for kind, _ in item.observations)
    ):
        return ReadUrl(item, classification, CapturedUrlOutcome.UNRESOLVED)
    return None


def _final_url_outcome(
    item: ObservedUrl,
    classification: CapturedUrlClassification,
    url: str,
    final_url: str,
    known_posting_fingerprints: frozenset[str],
) -> ReadUrl | None:
    """Return a decision the redirect target changes, or None to keep reading it."""
    if final_url == url:
        return None
    final_refusal = fetch_refusal(final_url)
    if final_refusal:
        return ReadUrl(
            item, classification, CapturedUrlOutcome.POLICY_REFUSED, detail=final_refusal
        )
    if not posting_identity_keys(final_url).isdisjoint(known_posting_fingerprints):
        return ReadUrl(item, classification, CapturedUrlOutcome.KNOWN_POSTING)
    # LinkedIn answers an expired posting by redirecting to its search results.
    if _linkedin_job_search_collection(final_url):
        return ReadUrl(
            item,
            classification,
            CapturedUrlOutcome.UNRESOLVED,
            detail=LINKEDIN_SEARCH_RESULTS_DETAIL,
        )
    return None


def _non_posting_page(
    item: ObservedUrl, classification: CapturedUrlClassification, fetched: FetchedPosting
) -> ReadUrl | None:
    """Return the unresolved outcome for an HTML page read that is not one posting.

    Publisher JobPosting markup is never overruled by classification: a JSON-LD
    posting whose visible text reads as an error page must not be lost.
    """
    if not fetched.page_classification or fetched.page_classification == PageClassification.POSTING:
        return None
    if fetched.publisher_markup_present is True:
        return None
    return ReadUrl(
        item,
        classification,
        CapturedUrlOutcome.UNRESOLVED,
        detail=f"page is not a single posting (classified as {fetched.page_classification})",
        robots_basis=fetched.robots_basis,
        employer=fetched.employer or "",
        title=fetched.title or "",
        funnel_reason=FunnelReason.INCOMPLETE_POSTING,
    )


def _fetch_posting(
    scans: SourceScanService,
    item: ObservedUrl,
    classification: CapturedUrlClassification,
) -> FetchedPosting | ReadUrl:
    """Fetch one captured posting, which the person opened themselves before it was captured."""
    try:
        fetched = scans.postings.fetcher.fetch(item.url)
    except IngestError:
        outcome, detail = CapturedUrlOutcome.UNREADABLE, "posting could not be read safely"
    except ValidationError as error:
        if isinstance(error.__cause__, RobotsDeniedError):
            outcome, detail = (
                CapturedUrlOutcome.POLICY_REFUSED,
                "posting source policy refused the read",
            )
        else:
            outcome, detail = CapturedUrlOutcome.UNREADABLE, "posting could not be read safely"
    else:
        return fetched
    return ReadUrl(item, classification, outcome, detail=detail)


def _read_one(
    scans: SourceScanService,
    *,
    item: ObservedUrl,
    parameters: JobSearchParameters,
    searched_titles: list[str],
    known_source_roots: frozenset[str],
    known_posting_fingerprints: frozenset[str],
    board_reads: dict[str, str] | None = None,
    board_listing_reads: dict[str, tuple[str, str, tuple[BoardListingCandidate, ...]]]
    | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ReadUrl:
    """Decide one captured URL; only a posting that stages new may carry a board to admit."""
    board_reads = {} if board_reads is None else board_reads
    board_listing_reads = {} if board_listing_reads is None else board_listing_reads
    url = item.url
    if not any(kind in CAPTURE_ACTION_KINDS for kind, _ in item.observations):
        return ReadUrl(
            item,
            CapturedUrlClassification.OTHER,
            CapturedUrlOutcome.UNRESOLVED,
            detail=NOT_CAPTURED_DETAIL,
        )
    classification, refusal = classify_url(
        url,
        known_source_roots=known_source_roots,
        known_posting_fingerprints=known_posting_fingerprints,
    )
    early = _pre_fetch_outcome(
        scans,
        item,
        classification,
        refusal,
        url,
        parameters,
        searched_titles,
        known_source_roots,
        known_posting_fingerprints,
        board_listing_reads,
        should_stop,
    )
    if early is not None:
        return early
    fetched = _fetch_posting(scans, item, classification)
    if isinstance(fetched, ReadUrl):
        return fetched
    if fetched.text is None or fetched.posting_status != "active":
        return ReadUrl(
            item,
            classification,
            CapturedUrlOutcome.UNREADABLE,
            detail=fetched.verification_note or fetched.posting_status,
            robots_basis=fetched.robots_basis,
            employer=fetched.employer or "",
            title=fetched.title or "",
            funnel_reason=FunnelReason.FETCH_FAILED,
        )
    final_url = fetched.final_url or url
    redirected = _final_url_outcome(
        item, classification, url, final_url, known_posting_fingerprints
    )
    if redirected is not None:
        return redirected
    not_a_posting = _non_posting_page(item, classification, fetched)
    if not_a_posting is not None:
        return not_a_posting
    outcome, staged_basis, detail, funnel_reason, screening_findings = _staging_decision(
        parameters=parameters,
        facts=(
            fetched.publisher_check.facts
            if fetched.publisher_check is not None
            else StagedListingFacts()
        ),
        url=final_url,
        title=fetched.title or "",
        employer=fetched.employer or "",
        body_text=fetched.text,
        searched_titles=searched_titles,
        model_selected=item.model_selected,
        has_search_source_observation=any(
            kind == UrlActionKind.SEARCH_SOURCE for kind, _ in item.observations
        ),
        publisher_read=True,
    )
    if outcome != CapturedUrlOutcome.STAGED:
        return ReadUrl(
            item,
            classification,
            outcome,
            staged_basis,
            detail,
            robots_basis=fetched.robots_basis,
            employer=fetched.employer or "",
            title=fetched.title or "",
            funnel_reason=funnel_reason,
            screening_findings=screening_findings,
        )
    staged_url = single_employer_detail_url(final_url) or without_apply_suffix(final_url)
    check = fetched.publisher_check
    readable = check is not None and check.status == "readable"
    # The staged row takes the publisher's readable name first, so its board takes it too.
    stated = (check.employer if check is not None and readable else "") or fetched.employer or ""
    employer, tenant_only = employer_name(stated, staged_url, fetched.text)
    if check is not None and readable and employer and not stated and not tenant_only:
        # The publisher's own text printed this spelling, so it vouches as a stated name would.
        fetched = replace(fetched, publisher_check=replace(check, employer=employer))
    board_root, duplicate_lead_root = (
        _readable_board(scans, staged_url, employer, known_source_roots, board_reads, should_stop)
        if classification == CapturedUrlClassification.ATS_DETAIL
        else ("", "")
    )
    return ReadUrl(
        item,
        classification,
        outcome,
        staged_basis,
        detail,
        fetched,
        board_root,
        employer if board_root else "",
        staged_url,
        robots_basis=fetched.robots_basis,
        employer=employer,
        title=fetched.title or "",
        funnel_reason=funnel_reason,
        screening_findings=screening_findings,
        duplicate_lead_root=duplicate_lead_root,
    )


def _readable_board(
    scans: SourceScanService,
    url: str,
    employer: str,
    known_source_roots: frozenset[str],
    board_reads: dict[str, str],
    should_stop: Callable[[], bool] | None,
) -> tuple[str, str]:
    """Return (root, duplicate root) for an unregistered board, read once per pass."""
    root = canonicalise_source_url(url)
    identity = board_identity_key(root)
    if not employer or identity in known_source_roots:
        return "", ""
    if identity in board_reads:
        cached_root = board_reads[identity]
        return cached_root, root if cached_root else ""
    if should_stop is not None and should_stop():
        return "", ""
    try:
        read = scans.fetcher.listings("", root)
    except (httpx.HTTPError, FetchPolicyError, ValueError):
        read_root = ""
    else:
        read_root = root if read.listings else ""
    board_reads[identity] = read_root
    return read_root, ""


def _admit_staged_posting(
    session: Session,
    scans: SourceScanService,
    *,
    workspace_id: str,
    today: date,
    correlation_id: str | None,
    read: ReadUrl,
    posting: FetchedPosting,
    live_roots: set[str],
) -> tuple[CapturedUrlOutcome, str, str | None, str | None]:
    """Stage a posting the read phase selected, admitting its named board when new."""
    outcome = read.outcome
    pending_id: str | None = None
    source_id: str | None = None
    staged = scans.staging.stage_found_posting(
        workspace_id,
        read.staged_url or read.item.url,
        today,
        title=posting.title or "",
        employer=read.employer,
        origin="search.guided_campaign",
        correlation_id=correlation_id,
        publisher_check=posting.publisher_check,
        derive_url_hints=False,
        existing_session=session,
    )
    if staged is None:
        outcome = CapturedUrlOutcome.KNOWN_POSTING
    else:
        pending_id = staged.pending_discovery_id
    if pending_id is not None and read.board_root:
        identity = board_identity_key(read.board_root)
        if identity not in live_roots:
            source_id = scans.remember_guided_board_in_session(
                session,
                workspace_id,
                read.board_employer,
                read.board_root,
                enabled=False,
                correlation_id=correlation_id,
            )
            live_roots.add(identity)
    return outcome, read.detail, pending_id, source_id


def _admit_board_listings(
    session: Session,
    scans: SourceScanService,
    *,
    workspace_id: str,
    today: date,
    correlation_id: str | None,
    read: ReadUrl,
    live_roots: set[str],
    funnel_outcomes: list[FunnelOutcome],
) -> tuple[CapturedUrlOutcome, str, tuple[str, ...], str | None, str]:
    """Admit a model-named board's screened listings, once per unregistered root.

    The last element names the root when this run already admitted it.
    """
    identity = board_identity_key(read.board_root)
    if identity in live_roots:
        return CapturedUrlOutcome.REGISTERED_BOARD, read.detail, (), None, read.board_root
    pending_ids: list[str] = []
    for candidate in read.board_listings:
        staged = scans.staging.stage_found_posting(
            workspace_id,
            candidate.url,
            today,
            title=candidate.title,
            employer=candidate.employer,
            location=candidate.location,
            origin="search.guided_campaign",
            correlation_id=correlation_id,
            # The board read stated this identity, so the candidate's facts carry its trust.
            publisher_check=PostingCheck(
                status="readable",
                note="",
                title=candidate.title,
                employer=candidate.employer,
                location=candidate.location,
                facts=candidate.facts,
            ),
            derive_url_hints=False,
            from_board_read=True,
            existing_session=session,
        )
        newly_staged = staged is not None and staged.pending_discovery_id not in pending_ids
        if newly_staged and staged is not None:
            pending_ids.append(staged.pending_discovery_id)
        funnel_outcomes.append(
            listing_outcome(
                candidate,
                source=_funnel_source(candidate.url),
                terminal=FunnelStage.STAGED if newly_staged else None,
                reason=FunnelReason.STAGED if newly_staged else FunnelReason.ALREADY_KNOWN,
            )
        )
    if not pending_ids:
        return CapturedUrlOutcome.UNRESOLVED, BOARD_LISTINGS_NOT_STAGED_DETAIL, (), None, ""
    live_roots.add(identity)
    if not read.board_employer:
        return read.outcome, read.detail, tuple(pending_ids), None, ""
    source_id = scans.remember_guided_board_in_session(
        session,
        workspace_id,
        read.board_employer,
        read.board_root,
        enabled=False,
        correlation_id=correlation_id,
    )
    return read.outcome, read.detail, tuple(pending_ids), source_id, ""


def _funnel_source(url: str) -> str:
    """Return the posting's host as the funnel row's source label."""
    return url_host(url)


def _captured_url_funnel_outcome(
    read: ReadUrl, outcome: CapturedUrlOutcome, detail: str
) -> FunnelOutcome | None:
    """Return the one funnel row a directly-captured URL earns, only when its page was read.

    ``robots_basis`` is non-null exactly when the program fetched and read the page, so
    it is the grain: one row per URL actually decided about, not per URL captured.
    """
    if read.robots_basis is None:
        return None
    reason = (
        FunnelReason.ALREADY_KNOWN
        if outcome == CapturedUrlOutcome.KNOWN_POSTING
        else read.funnel_reason
    )
    if reason is None:
        return None
    terminal = FunnelStage.STAGED if outcome == CapturedUrlOutcome.STAGED else FunnelStage.REJECTED
    return FunnelOutcome(
        kind=FunnelCandidateKind.DIRECT_POSTING,
        employer=read.employer,
        title=read.title,
        url=read.item.url,
        source=_funnel_source(read.item.url),
        stages=(
            FunnelStage.PROPOSED,
            FunnelStage.FETCHED,
            FunnelStage.READ,
            FunnelStage.SCREENED,
            terminal,
        ),
        reason=reason,
        detail=detail,
        screening_findings=read.screening_findings,
    )


def _duplicate_lead_outcomes(read: ReadUrl, admitted_root: str) -> tuple[FunnelOutcome, ...]:
    """Return the one funnel row a board root this run reached twice earns, else nothing."""
    root = read.duplicate_lead_root or admitted_root
    if not root:
        return ()
    return (
        FunnelOutcome(
            kind=FunnelCandidateKind.BOARD_LEAD,
            employer=read.board_employer,
            url=root,
            source=_funnel_source(root),
            stages=(FunnelStage.PROPOSED, FunnelStage.REJECTED),
            reason=FunnelReason.DUPLICATE_LEAD,
            detail=DUPLICATE_LEAD_DETAIL,
        ),
    )


def admit_read_urls(
    session: Session,
    scans: SourceScanService,
    *,
    workspace_id: str,
    today: date,
    correlation_id: str | None,
    reads: tuple[ReadUrl, ...],
) -> tuple[tuple[AdmissionResult, ...], tuple[FunnelOutcome, ...]]:
    """Stage postings and admit boards for already-read URLs inside one transaction.

    Every directly-captured URL the program actually read is also recorded as a
    funnel row, as is every board-root listing it decided about and every board root
    a second branch of this run converged on. Those rows come back with the results
    as offered, not as kept, so a row the ledger drops shows as a coverage gap.
    """
    live_roots = set(known_identities_in(session, workspace_id).source_roots)
    results: list[AdmissionResult] = []
    funnel_outcomes: list[FunnelOutcome] = []
    for read in reads:
        outcome = read.outcome
        detail = read.detail
        pending_ids: tuple[str, ...] = ()
        source_id: str | None = None
        admitted_root = ""
        if outcome == CapturedUrlOutcome.STAGED and read.posting is not None:
            outcome, detail, pending_id, source_id = _admit_staged_posting(
                session,
                scans,
                workspace_id=workspace_id,
                today=today,
                correlation_id=correlation_id,
                read=read,
                posting=read.posting,
                live_roots=live_roots,
            )
            pending_ids = (pending_id,) if pending_id is not None else ()
        elif outcome == CapturedUrlOutcome.BOARD_ADMITTED and read.board_root:
            outcome, detail, pending_ids, source_id, admitted_root = _admit_board_listings(
                session,
                scans,
                workspace_id=workspace_id,
                today=today,
                correlation_id=correlation_id,
                read=read,
                live_roots=live_roots,
                funnel_outcomes=funnel_outcomes,
            )
        results.append(
            AdmissionResult(
                url_sha256=posting_url_key(read.item.url),
                url=read.item.url,
                classification=read.classification,
                model_selected=read.item.model_selected,
                model_why=read.item.model_why,
                outcome=outcome,
                staged_basis=read.staged_basis if outcome == CapturedUrlOutcome.STAGED else None,
                pending_discovery_ids=pending_ids,
                source_id=source_id,
                detail=detail,
                robots_basis=read.robots_basis,
            )
        )
        funnel_outcome = _captured_url_funnel_outcome(read, outcome, detail)
        if funnel_outcome is not None:
            funnel_outcomes.append(funnel_outcome)
        funnel_outcomes.extend(read.board_listing_outcomes)
        funnel_outcomes.extend(_duplicate_lead_outcomes(read, admitted_root))
    if funnel_outcomes and correlation_id is not None:
        scans.record_task_outcomes_in_session(
            session,
            workspace_id,
            correlation_id,
            funnel_outcomes,
            origin="search.guided_campaign",
            default_source="search.guided_campaign",
        )
    return tuple(results), tuple(funnel_outcomes)
