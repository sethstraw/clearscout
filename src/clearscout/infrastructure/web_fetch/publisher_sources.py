"""Read ordinary publisher career pages without assuming an ATS vendor."""

from __future__ import annotations

import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

import httpx

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
    SourceReadOptions,
)
from clearscout.domain import PostingContentAccessStatus, SourceReadOutcome, utc_now
from clearscout.domain.records import PageClassification
from clearscout.domain.staged_facts import ListingIdentityTrust
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import declared_listing
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.boards import JOB_URL_PATTERNS, board_identity
from clearscout.infrastructure.web_fetch.ingest.careers import (
    LinkRelation,
    PageEvidence,
    PublisherLink,
    inspect_publisher_page,
)
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from clearscout.infrastructure.web_fetch.ingest.sitemap import (
    is_document_url,
    is_non_html_media_type,
)
from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT
from clearscout.infrastructure.web_fetch.listing_facts import published_listing_facts
from clearscout.infrastructure.web_fetch.platforms.smartrecruiters import (
    publisher_collection_markup,
)
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    PoliteHttpClient,
    RobotsDeniedError,
)

_HTML_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
_DEFAULT_READ_OPTIONS = SourceReadOptions()

# Mirrors SourceScanFrontierItem.label String(300); keep the two in step.
MAX_FRONTIER_LABEL_LENGTH = 300

_LOW_PRIORITY_NAVIGATION_LABELS = frozenset(
    {
        "about",
        "about us",
        "contact",
        "contact us",
        "home",
        "privacy",
        "privacy policy",
        "terms",
        "terms of use",
    }
)

_NAVIGATION_PATH_LABELS = {
    "about": frozenset({"about"}),
    "about us": frozenset({"about", "about us"}),
    "contact": frozenset({"contact"}),
    "contact us": frozenset({"contact", "contact us"}),
    "home": frozenset({"home"}),
    "privacy": frozenset({"privacy"}),
    "privacy policy": frozenset({"privacy", "privacy policy"}),
    "terms": frozenset({"terms"}),
    "terms of use": frozenset({"terms", "terms of use"}),
}


def _low_priority_navigation_link(link: PublisherLink) -> bool:
    """Recognize exact navigation labels without removing their URLs from the frontier."""
    if urlsplit(link.fetch_url).path.rstrip("/").endswith("/other-jobs-matching/location-only"):
        return True
    label = " ".join(link.label.split()).casefold()
    if label not in _LOW_PRIORITY_NAVIGATION_LABELS:
        return False
    path_label = urlsplit(link.fetch_url).path.rstrip("/").rsplit("/", 1)[-1]
    normalized_path_label = path_label.replace("-", " ").replace("_", " ").casefold()
    return normalized_path_label in _NAVIGATION_PATH_LABELS[label]


def _publisher_content_link(link: PublisherLink) -> bool:
    """Return whether a link has publisher-owned content and a usable label."""
    relations = set(link.relations)
    return {
        LinkRelation.PUBLISHER_CONTENT,
        LinkRelation.SUBSTANTIVE_LABEL,
    } <= relations


# Names an automatic link to a registered ATS posting URL: one posting, so finite work.
REGISTERED_POSTING_LINK = "registered_posting_link"


def _registered_posting_url(url: str) -> bool:
    return any(host_match(pattern, url) is not None for pattern in JOB_URL_PATTERNS)


def _posting_detail_link(link: PublisherLink) -> bool:
    """Recognize explicit posting URLs without treating career hubs as postings."""
    if not _publisher_content_link(link):
        return False
    if _registered_posting_url(link.fetch_url):
        return True
    parsed = urlsplit(link.fetch_url)
    segments = [part.casefold() for part in parsed.path.split("/") if part]
    detail_segments = {
        "emploi",
        "emplois",
        "job",
        "jobs",
        "opening",
        "openings",
        "position",
        "positions",
        "role",
        "roles",
        "vacancies",
        "vacancy",
        "opportunity",
        "opportunities",
    }
    path_signal = any(
        (segment in detail_segments or segment.startswith(("job-", "position-")))
        and index < len(segments) - 1
        for index, segment in enumerate(segments)
    )
    query_keys = {key.casefold() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    query_signal = bool(
        query_keys
        & {
            "gh_jid",
            "job",
            "jobid",
            "job_id",
            "position",
            "positionid",
            "reqid",
            "requisitionid",
        }
    )
    role_detail_signal = "role" in query_keys and bool(
        set(segments) & {"detail", "job", "jobs", "opening", "position", "role"}
    )
    return path_signal or query_signal or role_detail_signal


def _posting_gateway_link(link: PublisherLink) -> bool:
    """Recognize a careers collection; its own next page may pass the depth bound."""
    if link.collection_pagination:
        return True
    if not _publisher_content_link(link) and not (
        link.structural_context == "other" and LinkRelation.SUBSTANTIVE_LABEL in link.relations
    ):
        return False
    label = " ".join(link.label.split()).casefold()
    return label in {
        "careers",
        "current opportunities",
        "job opportunities",
        "jobs",
        "open positions",
        "open roles",
        "openings",
        "opportunities",
        "positions",
        "search careers",
        "search jobs",
        "view all jobs",
        "view jobs",
        "view openings",
        "view opportunities",
    }


def _traversal_links(evidence: PageEvidence) -> tuple[PublisherLink, ...]:
    return evidence.candidate_links + tuple(
        link
        for link in evidence.links
        if link not in evidence.candidate_links and _posting_gateway_link(link)
    )


def _repeated_shape_link(link: PublisherLink) -> bool:
    """Recognize a language-neutral repeated target as a probe candidate."""
    return _publisher_content_link(link) and (LinkRelation.REPEATED_TARGET_SHAPE in link.relations)


# (host, kind, detail) scopes exact postings, board keys, directory prefixes, or hosts.
# "same_host_probe_directory" bounds links on the serving host.
_HandoffScope = tuple[str, str, str]
_SAME_HOST_PROBE_DIRECTORY = "same_host_probe_directory"


@dataclass(frozen=True)
class PublisherReadLimits:
    """Visible bounds for one publisher-source traversal."""

    max_pages: int = 250
    max_depth: int = 2
    max_frontier_items: int = 500
    max_html_characters: int = 2_000_000
    timeout_seconds: float = 20.0
    max_elapsed_seconds: float = 30.0
    # One fetch lane per distinct host; PoliteHttpClient's per-host lock keeps
    # same-host reads serial, so distinct hosts are the only source of overlap.
    max_concurrent_hosts: int = 4


@dataclass(frozen=True)
class _Target:
    link: PublisherLink
    depth: int
    automatic_eligible: bool = True
    deferred_reason: str = ""


@dataclass
class _Traversal:
    frontier: deque[_Target]
    posting_urls: set[str]
    visited: set[str]
    listings: dict[str, SourceListing]
    deferred: list[_Target]
    reasons: dict[str, int]
    root_host: str
    handoff_scopes: tuple[_HandoffScope, ...]
    root_directory: str
    employer_is_authoritative: bool = False
    # Counted per run only: a continuation starts with an empty deferred list, so
    # a shape can only be expanded from siblings rediscovered in the same run.
    shape_confirmations: dict[str, int] = field(default_factory=dict)
    options: SourceReadOptions = field(default_factory=SourceReadOptions)
    protected: set[str] = field(default_factory=set)


class PublisherSourceFetcher:
    """Traverse publisher-declared content links and retain every bounded outcome."""

    def __init__(
        self,
        *,
        client: PoliteHttpClient,
        limits: PublisherReadLimits | None = None,
    ) -> None:
        """Configure the shared request policy and explicit traversal bounds."""
        self.client = client
        self.limits = limits or PublisherReadLimits()

    def read(
        self,
        employer: str,
        url: str,
        page: str,
        *,
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = _DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """Read linked posting pages while preserving the unresolved frontier.

        ``employer_is_authoritative`` says a person named ``employer``, which is
        what lets it vouch for markup that names no employer of its own.
        """
        page = publisher_collection_markup(url, page)
        root = inspect_publisher_page(url, page)
        state = _start_traversal(
            _traversal_links(root), url, employer_is_authoritative=employer_is_authoritative
        )
        state.options = options
        declared = self._store_declared(employer, url, page, state)
        if not declared and root.classification is PageClassification.POSTING and root.title:
            _store_listing(
                state,
                _publisher_listing(
                    employer,
                    url,
                    root.title,
                    employer_is_authoritative=state.employer_is_authoritative,
                ),
            )
        stop_reason = self._traverse(
            employer,
            state,
            deadline_at=time.monotonic() + self.limits.max_elapsed_seconds,
        )
        self._defer_frontier(state, reason=stop_reason)
        return SourceRead(
            listings=list(state.listings.values()),
            complete=False,
            raw_count=max(len(state.posting_urls), len(state.listings)),
            skip_reasons=state.reasons,
            outcome=_read_outcome(root.classification, state.listings, state.deferred),
            note=(
                _read_note(root.classification, state.listings, state.deferred)
                + (
                    f" Skipped {len(state.protected)} protected postings without detail reads."
                    if state.protected
                    else ""
                )
            ),
            deferred_targets=_continuation_targets(state.deferred),
            exhausted_outcome=(
                SourceReadOutcome.PUBLISHER_LINKED_PAGES if state.deferred else None
            ),
            handoff_scopes=state.handoff_scopes,
            protected_posting_urls=tuple(sorted(state.protected)),
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = _DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """Read only the supplied durable frontier, using GET requests throughout.

        ``employer_is_authoritative`` says a person named ``employer``, as it
        does for a first read.
        """
        state = _start_continuation(
            targets,
            root_host=_host_of(root_url),
            root_directory=_root_directory(urlsplit(root_url).path),
            handoff_scopes=handoff_scopes,
            employer_is_authoritative=employer_is_authoritative,
        )
        state.options = options
        stop_reason = self._traverse(
            employer,
            state,
            deadline_at=time.monotonic() + self.limits.max_elapsed_seconds,
        )
        self._defer_frontier(state, reason=stop_reason)
        deferred = _continuation_targets(state.deferred)
        attempted_target_urls = tuple(
            target.url for target in targets if target.url.casefold() in state.visited
        )
        return SourceRead(
            listings=list(state.listings.values()),
            complete=False,
            raw_count=max(len(targets), len(state.listings)),
            skip_reasons=state.reasons,
            outcome=(
                SourceReadOutcome.CRAWL_CONTINUATION
                if deferred
                else SourceReadOutcome.PUBLISHER_LINKED_PAGES
            ),
            note=(
                f"{len(deferred)} publisher-declared URLs remain in the durable frontier."
                if deferred
                else f"Read {len(attempted_target_urls)} durable publisher-frontier URLs."
            )
            + (
                f" Skipped {len(state.protected)} protected postings without detail reads."
                if state.protected
                else ""
            ),
            deferred_targets=deferred,
            attempted_target_urls=attempted_target_urls,
            handoff_scopes=state.handoff_scopes,
            protected_posting_urls=tuple(sorted(state.protected)),
        )

    def _traverse(self, employer: str, state: _Traversal, *, deadline_at: float) -> str | None:
        """Read the frontier with one lane per host, keeping distinct hosts busy.

        Only the network fetch runs off the orchestrator thread, so every change
        to traversal state, and every bound, is evaluated exactly as a single
        serial reader would; distinct hosts are the sole source of overlap.
        """
        busy_hosts: set[str] = set()
        in_flight: dict[Future[tuple[str, str, str]], _Target] = {}
        stop_reason: str | None = None
        with ThreadPoolExecutor(max_workers=self.limits.max_concurrent_hosts) as pool:
            while True:
                if stop_reason is None:
                    stop_reason = self._fill_lanes(pool, state, busy_hosts, in_flight, deadline_at)
                if not in_flight:
                    break
                future = next(as_completed(in_flight))
                target = in_flight.pop(future)
                busy_hosts.discard(_host(target.link))
                self._consume(employer, target, future, state)
        return stop_reason

    def _fill_lanes(
        self,
        pool: ThreadPoolExecutor,
        state: _Traversal,
        busy_hosts: set[str],
        in_flight: dict[Future[tuple[str, str, str]], _Target],
        deadline_at: float,
    ) -> str | None:
        """Start ready fetches on idle hosts until a lane, page, or time bound stops it.

        A returned stop reason is only recorded when the frontier is non-empty at
        the end; _defer_frontier discards it when the frontier drained.
        """
        while len(in_flight) < self.limits.max_concurrent_hosts:
            if len(state.visited) >= self.limits.max_pages:
                return "page_limit_reached"
            if time.monotonic() >= deadline_at:
                return "time_limit_reached"
            target = self._ready_target(state, busy_hosts)
            if target is None:
                return None
            state.visited.add(target.link.fetch_url.casefold())
            busy_hosts.add(_host(target.link))
            in_flight[pool.submit(self._fetch, target, deadline_at, state.options)] = target
        return None

    @staticmethod
    def _ready_target(state: _Traversal, busy_hosts: set[str]) -> _Target | None:
        """Pop the first frontier target on an idle host, dropping visited duplicates."""
        held: list[_Target] = []
        found: _Target | None = None
        while state.frontier:
            target = state.frontier.popleft()
            if not state.options.allows_posting(target.link.fetch_url):
                if target.link.fetch_url not in state.protected:
                    state.protected.add(target.link.fetch_url)
                    _increment(state.reasons, "protected_posting")
                continue
            if target.link.fetch_url.casefold() in state.visited:
                continue
            if _host(target.link) in busy_hosts:
                held.append(target)
                continue
            found = target
            break
        state.frontier.extendleft(reversed(held))
        return found

    def _fetch(
        self, target: _Target, deadline_at: float, options: SourceReadOptions
    ) -> tuple[str, str, str]:
        """Fetch one page; the only traversal work that leaves the orchestrator."""
        return self._page(
            target.link.fetch_url,
            max_elapsed_seconds=max(0.001, deadline_at - time.monotonic()),
            options=options,
        )

    def _consume(
        self,
        employer: str,
        target: _Target,
        future: Future[tuple[str, str, str]],
        state: _Traversal,
    ) -> None:
        """Fold one completed fetch into the shared traversal state."""
        try:
            landed_url, detail_page, content_type = future.result()
        except HostHeldError:
            # No request was made, so the target is kept as it was and not counted as attempted.
            state.visited.discard(target.link.fetch_url.casefold())
            state.deferred.append(target)
            _increment(state.reasons, "host_held")
            return
        except (FetchPolicyError, httpx.HTTPError, ValueError) as exc:
            retryable, reason = _fetch_recovery(exc)
            state.deferred.append(_Target(target.link, target.depth, retryable, reason))
            _increment(state.reasons, type(exc).__name__)
            return
        _scope_redirect(landed_url, target, state)
        detail_page = publisher_collection_markup(landed_url, detail_page)
        if self._store_declared(employer, landed_url, detail_page, state):
            self._activate_shape_siblings(target.link, state, declared=True)
            return
        # A document carries no JobPosting markup, so it is finished rather than retried.
        if is_non_html_media_type(content_type) or is_document_url(landed_url):
            _increment(state.reasons, "non_posting_page")
            return
        evidence = inspect_publisher_page(
            landed_url,
            detail_page,
            referring_link=target.link,
        )
        self._interpret_evidence(employer, landed_url, target, evidence, state)

    @staticmethod
    def _store_declared(
        employer: str,
        landed_url: str,
        detail_page: str,
        state: _Traversal,
    ) -> bool:
        declared = job_postings(detail_page)
        read_at = utc_now()
        for posting in declared:
            _store_listing(
                state,
                declared_listing(
                    posting,
                    employer=employer,
                    employer_is_authoritative=state.employer_is_authoritative,
                    page_url=landed_url,
                    read_at=read_at,
                    page_markup=detail_page,
                    content_status=PostingContentAccessStatus.READABLE,
                    content_note="Publisher-declared JobPosting content was readable.",
                ),
            )
        return bool(declared)

    def _interpret_evidence(
        self,
        employer: str,
        landed_url: str,
        target: _Target,
        evidence: PageEvidence,
        state: _Traversal,
    ) -> None:
        if evidence.classification is PageClassification.POSTING and evidence.title:
            _store_listing(
                state,
                _publisher_listing(
                    employer,
                    landed_url,
                    evidence.title,
                    employer_is_authoritative=state.employer_is_authoritative,
                ),
            )
            self._activate_shape_siblings(target.link, state, declared=False)
            return
        if evidence.classification is PageClassification.COLLECTION:
            self._extend_frontier(target.depth, _traversal_links(evidence), state)
            return
        if (
            target.link.collection_pagination
            and not evidence.links
            and not evidence.visible_text.strip()
        ):
            _increment(state.reasons, "collection_exhausted")
            return
        if evidence.classification in {
            PageClassification.ERROR_OR_CHALLENGE,
            PageClassification.JS_SHELL,
            PageClassification.AMBIGUOUS,
        }:
            state.deferred.append(
                _Target(
                    target.link,
                    target.depth,
                    False,
                    f"classification_{evidence.classification.value}",
                )
            )
        _increment(state.reasons, evidence.classification.value)

    @staticmethod
    def _activate_shape_siblings(link: PublisherLink, state: _Traversal, *, declared: bool) -> None:
        """Expand an opaque repeated URL shape once a member proves to be a posting.

        A publisher-declared JobPosting node is the publisher's own assertion and
        expands the shape alone; a heuristic classification expands one sibling
        first and the rest only once that sibling confirms too.
        """
        if not _repeated_shape_link(link):
            return
        shape = link.target_shape
        confirmations = state.shape_confirmations.get(shape, 0) + 1
        state.shape_confirmations[shape] = confirmations
        promotable = len(state.deferred) if declared or confirmations >= 2 else 1
        retained: list[_Target] = []
        for target in state.deferred:
            promotes = (
                promotable > 0
                and target.deferred_reason == "shape_probe_pending"
                and target.link.target_shape == shape
            )
            if promotes:
                promotable -= 1
                state.frontier.append(_Target(target.link, target.depth))
            else:
                retained.append(target)
        state.deferred = retained

    def _extend_frontier(
        self,
        depth: int,
        links: tuple[PublisherLink, ...],
        state: _Traversal,
    ) -> None:
        # Same-origin is relative to the traversal root, never the page just read.
        admitted, off_host, outside_root_directory, low_priority_navigation = (
            self._admissible_extensions(links, state)
        )
        next_depth = depth + 1
        policy_deferred = [link for link in admitted if _low_priority_navigation_link(link)]
        state.deferred.extend(
            _Target(link, next_depth, False, "low_priority_navigation") for link in policy_deferred
        )
        beyond_depth = depth >= self.limits.max_depth
        traversable, classified_deferred = _classify_frontier_links(
            tuple(link for link in admitted if not _low_priority_navigation_link(link)),
            depth_allows_gateway=not beyond_depth,
            depth=next_depth,
        )
        state.deferred.extend(classified_deferred)
        for target in classified_deferred:
            _increment(state.reasons, target.deferred_reason)
        # Bound only active in-memory traversal. Overflow remains durable
        # continuation work instead of disappearing at the limit.
        budget = max(0, self.limits.max_frontier_items - len(state.frontier))
        active, overflow = traversable[:budget], traversable[budget:]
        state.frontier.extend(active)
        if overflow:
            # A registered posting keeps its reason so a later task does not cap it as unknown.
            state.deferred.extend(
                _Target(
                    target.link,
                    target.depth,
                    target.automatic_eligible,
                    target.deferred_reason
                    if target.deferred_reason == REGISTERED_POSTING_LINK
                    else "frontier_deferred",
                )
                for target in overflow
            )
            _increment(state.reasons, "frontier_deferred", len(overflow))
        if off_host:
            _increment(state.reasons, "off_host_link", off_host)
        if outside_root_directory:
            _increment(state.reasons, "outside_root_directory", outside_root_directory)
        if low_priority_navigation:
            _increment(state.reasons, "low_priority_navigation", low_priority_navigation)

    @staticmethod
    def _admissible_extensions(
        links: tuple[PublisherLink, ...], state: _Traversal
    ) -> tuple[list[PublisherLink], int, int, int]:
        """Return in-scope links and account for terminal scope exclusions."""
        in_scope, off_host, outside_root_directory = _scope_filtered_links(links, state)
        known = {
            *state.visited,
            *(target.link.fetch_url.casefold() for target in state.frontier),
            *(target.link.fetch_url.casefold() for target in state.deferred),
            *(url.casefold() for url in state.protected),
        }
        admitted: list[PublisherLink] = []
        deprioritized: list[PublisherLink] = []
        low_priority_navigation_count = sum(
            _low_priority_navigation_link(link) for link in in_scope
        )
        for link in in_scope:
            identity = link.fetch_url.casefold()
            low_priority_navigation = _low_priority_navigation_link(link)
            # Only same-host repeated-shape links count toward the raw posting total.
            if (
                LinkRelation.REPEATED_TARGET_SHAPE in link.relations
                and not low_priority_navigation
                and not _posting_gateway_link(link)
            ):
                state.posting_urls.add(identity)
            if identity in known:
                continue
            known.add(identity)
            if not state.options.allows_posting(link.fetch_url):
                state.protected.add(link.fetch_url)
                _increment(state.reasons, "protected_posting")
                continue
            if low_priority_navigation:
                deprioritized.append(link)
            else:
                admitted.append(link)
        admitted.extend(deprioritized)
        return admitted, off_host, outside_root_directory, low_priority_navigation_count

    @staticmethod
    def _defer_frontier(state: _Traversal, *, reason: str | None) -> None:
        retained: list[_Target] = []
        for target in state.deferred:
            if state.options.allows_posting(target.link.fetch_url):
                retained.append(target)
            elif target.link.fetch_url not in state.protected:
                state.protected.add(target.link.fetch_url)
                _increment(state.reasons, "protected_posting")
        state.deferred = retained
        if not state.frontier:
            return
        remaining = []
        for target in state.frontier:
            if state.options.allows_posting(target.link.fetch_url):
                remaining.append(target)
            elif target.link.fetch_url not in state.protected:
                state.protected.add(target.link.fetch_url)
                _increment(state.reasons, "protected_posting")
        state.deferred.extend(remaining)
        if remaining:
            _increment(state.reasons, reason or "page_limit_reached", len(remaining))

    def _page(
        self,
        url: str,
        *,
        max_elapsed_seconds: float,
        options: SourceReadOptions = _DEFAULT_READ_OPTIONS,
    ) -> tuple[str, str, str]:
        response = self.client.get(
            url,
            headers=_HTML_HEADERS,
            timeout=httpx.Timeout(self.limits.timeout_seconds),
            follow_redirects=True,
            max_bytes=self.limits.max_html_characters * 4 + 1,
            max_elapsed_seconds=max_elapsed_seconds,
            request_guard=options.allows_posting,
        )
        response.raise_for_status()
        if len(response.text) > self.limits.max_html_characters:
            raise ValueError("publisher_page_byte_limit_reached")
        return str(response.url), response.text, response.headers.get("Content-Type", "")


def _host(link: PublisherLink) -> str:
    """The lane key for a link: its lowercased host, so one host reads serially."""
    return (urlsplit(link.fetch_url).hostname or "").casefold()


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold()


def _directory_prefix(path: str) -> str:
    """Treat the link's own path as its directory; a bare host root scopes nothing."""
    if not path or path == "/":
        return ""
    return (path if path.endswith("/") else path + "/").casefold()


def _root_directory(path: str) -> str:
    """A root path ending in a slash bounds to itself; any other names a file in its parent."""
    return _directory_prefix(path if path.endswith("/") else path.rpartition("/")[0] + "/")


def _same_employer_host(root_host: str, host: str) -> bool:
    """Return whether two hosts are one employer's own site, apart from one leading label."""
    root_rest = root_host.partition(".")[2]
    return bool(root_rest) and "." in root_rest and root_rest == host.partition(".")[2]


def _board_key(identity: tuple[str, str]) -> str:
    return f"{identity[0]}:{identity[1]}"


_WORKABLE_HOST = "apply.workable.com"


def _handoff_scopes(
    root_url: str, root_links: tuple[PublisherLink, ...]
) -> tuple[_HandoffScope, ...]:
    """Bound each off-host link the root page declares by board identity, employer host, or host.

    A shared ATS tenant scopes to its board, an employer's other host or a tenantless
    short link to the link's directory, and any other host in full for one hop.
    """
    root_host = _host_of(root_url)
    # A resume starts from the saved address, whose host can differ from the served one.
    scopes: set[_HandoffScope] = {(root_host, "host", "")} if root_host else set()
    for link in root_links:
        parsed = urlsplit(link.fetch_url)
        host = (parsed.hostname or "").casefold()
        if not host or host == root_host:
            continue
        identity = board_identity(link.fetch_url)
        if identity is not None and identity == board_identity(root_url):
            scopes.add((host, "board", _board_key(identity)))
        elif _registered_posting_url(link.fetch_url):
            scopes.add((host, "posting", link.fetch_url.casefold()))
        elif identity is not None:
            scopes.add((host, "board", _board_key(identity)))
        elif _same_employer_host(root_host, host) or host == _WORKABLE_HOST:
            prefix = _directory_prefix(parsed.path)
            if prefix:
                scopes.add((host, "directory", prefix))
        else:
            scopes.add((host, "host", ""))
    root_directory = _root_directory(urlsplit(root_url).path)
    if root_directory:
        scopes.add((root_host, _SAME_HOST_PROBE_DIRECTORY, root_directory))
    return tuple(scopes)


def _within_traversal_scope(link: PublisherLink, state: _Traversal) -> bool:
    """Same-host links stay in scope; an off-host handoff is bounded by its own scope kind."""
    url = link.fetch_url
    if _host_of(url) == state.root_host:
        return not _outside_root_directory(link, state)
    return _matches_handoff_scope(url, state.handoff_scopes)


def _matches_handoff_scope(url: str, scopes: tuple[_HandoffScope, ...]) -> bool:
    """Return whether an off-host URL stays inside a root-declared handoff."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    identity = board_identity(url)
    for scope_host, kind, detail in scopes:
        if host != scope_host:
            continue
        if kind == "posting" and url.casefold() == detail:
            return True
        if kind == "host":
            return True
        if kind == "directory" and (path == detail.removesuffix("/") or path.startswith(detail)):
            return True
        if kind == "board" and identity is not None and _board_key(identity) == detail:
            return True
    return False


def _same_host_probe_directories(state: _Traversal) -> tuple[str, ...]:
    return tuple(
        detail
        for scope_host, kind, detail in state.handoff_scopes
        if scope_host == state.root_host and kind == _SAME_HOST_PROBE_DIRECTORY
    )


def _outside_root_directory(link: PublisherLink, state: _Traversal) -> bool:
    """Bound same-host links to the root's directories, except postings and job gateways."""
    parsed = urlsplit(link.fetch_url)
    if (parsed.hostname or "").casefold() != state.root_host:
        return False
    if _posting_detail_link(link) or _posting_gateway_link(link):
        return False
    directories = _same_host_probe_directories(state)
    path = parsed.path.casefold()
    return bool(directories) and not any(
        path == directory.removesuffix("/") or path.startswith(directory)
        for directory in directories
    )


def _admit_same_host_gateway(link: PublisherLink, state: _Traversal) -> None:
    """Widen a bounded root to a same-host job gateway's directory so its children stay readable."""
    if _host(link) != state.root_host or not _posting_gateway_link(link):
        return
    directory = _directory_prefix(urlsplit(link.fetch_url).path)
    root = state.root_directory
    # Only the root's own bound is compared, so gateways admitted earlier never refuse another;
    # a gateway at or above the root would unbound the corporate site around it.
    if (
        not directory
        or root not in _same_host_probe_directories(state)
        or root.startswith(directory)
    ):
        return
    scope = (state.root_host, _SAME_HOST_PROBE_DIRECTORY, directory)
    if scope not in state.handoff_scopes:
        state.handoff_scopes = (*state.handoff_scopes, scope)


def _scope_exclusion_reason(link: PublisherLink, state: _Traversal) -> str | None:
    """Name terminal scope exclusions so they stay out of the durable frontier."""
    if _within_traversal_scope(link, state):
        return None
    return "outside_root_directory" if _outside_root_directory(link, state) else "off_host_link"


def _scope_filtered_links(
    links: tuple[PublisherLink, ...], state: _Traversal
) -> tuple[list[PublisherLink], int, int]:
    """Keep terminally excluded links out of both the active and durable frontier."""
    in_scope: list[PublisherLink] = []
    off_host: set[str] = set()
    outside_root_directory: set[str] = set()
    # Every gateway widens scope before any link is judged, so page order cannot matter.
    for link in links:
        _admit_same_host_gateway(link, state)
    for link in links:
        match _scope_exclusion_reason(link, state):
            case None:
                in_scope.append(link)
            case "outside_root_directory":
                outside_root_directory.add(link.fetch_url.casefold())
            case _:
                off_host.add(link.fetch_url.casefold())
    return in_scope, len(off_host), len(outside_root_directory)


def _scope_redirect(landed_url: str, target: _Target, state: _Traversal) -> None:
    """Preserve a declared handoff's final host within its served directory."""
    if target.depth != 1 or LinkRelation.EXTERNAL_HANDOFF not in target.link.relations:
        return
    landed_host = _host_of(landed_url)
    if not landed_host:
        return
    if _registered_posting_url(target.link.fetch_url):
        scope = (landed_host, "posting", landed_url.casefold())
        if scope not in state.handoff_scopes:
            state.handoff_scopes = (*state.handoff_scopes, scope)
        return
    if landed_host == _host_of(target.link.fetch_url):
        return
    directory = _directory_prefix(urlsplit(landed_url).path)
    scope = (landed_host, "directory", directory) if directory else (landed_host, "host", "")
    if scope not in state.handoff_scopes:
        state.handoff_scopes = (*state.handoff_scopes, scope)


def _publisher_listing(
    employer: str, url: str, title: str, *, employer_is_authoritative: bool
) -> SourceListing:
    # The crawl reads no employer off the page, so a configured name nobody
    # answered for is a hint rather than something the publisher stated.
    trust = (
        ListingIdentityTrust.PUBLISHER
        if employer_is_authoritative or not employer
        else ListingIdentityTrust.UNVERIFIED_HINT
    )
    return SourceListing(
        employer=employer,
        title=title,
        url=url,
        facts=published_listing_facts(
            "publisher_html", identity_trust=trust, locations_complete=False
        ),
        content_status=PostingContentAccessStatus.READABLE,
        content_note="Publisher posting content was readable.",
    )


def _continuation_link(target: SourceContinuationTarget) -> PublisherLink:
    """Reconstruct the exact publisher link evidence retained by the application."""
    relations = tuple(LinkRelation(relation) for relation in target.relations)
    return PublisherLink(
        fetch_url=target.url,
        label=target.label,
        label_source=target.label_source,
        source_url=target.source_url,
        structural_context=target.structural_context,
        target_shape=target.url,
        cluster_size=2 if LinkRelation.REPEATED_TARGET_SHAPE in relations else 1,
        relations=relations,
    )


def _continuation_targets(
    links: list[_Target],
) -> tuple[SourceContinuationTarget, ...]:
    """Convert infrastructure parser evidence to the application-owned frontier type."""
    unique: dict[str, _Target] = {}
    for target in links:
        unique.setdefault(target.link.fetch_url.casefold(), target)
    return tuple(
        SourceContinuationTarget(
            url=target.link.fetch_url,
            label=_bounded_continuation_label(target.link.label),
            label_source=target.link.label_source,
            source_url=target.link.source_url,
            structural_context=target.link.structural_context,
            relations=tuple(relation.value for relation in target.link.relations),
            depth=target.depth,
            automatic_eligible=target.automatic_eligible,
            deferred_reason=target.deferred_reason,
        )
        for target in unique.values()
    )


def _bounded_continuation_label(label: str) -> str:
    """Keep publisher labels within the durable frontier contract."""
    if len(label) <= MAX_FRONTIER_LABEL_LENGTH:
        return label
    clipped = label[:MAX_FRONTIER_LABEL_LENGTH].rsplit(" ", 1)[0].rstrip()
    return clipped or label[:MAX_FRONTIER_LABEL_LENGTH]


def _classify_frontier_links(
    links: tuple[PublisherLink, ...], *, depth_allows_gateway: bool, depth: int
) -> tuple[list[_Target], list[_Target]]:
    """Admit explicit details and one representative from each opaque URL shape."""
    automatic: list[_Target] = []
    deferred: list[_Target] = []
    probed_shapes: set[str] = set()
    progressed = any(_posting_detail_link(link) for link in links)
    for link in links:
        if is_document_url(link.fetch_url):
            deferred.append(_Target(link, depth, False, "non_html_document"))
        elif _low_priority_navigation_link(link):
            deferred.append(_Target(link, depth, False, "low_priority_navigation"))
        elif _posting_detail_link(link):
            reason = REGISTERED_POSTING_LINK if _registered_posting_url(link.fetch_url) else ""
            automatic.append(_Target(link, depth, True, reason))
        elif _posting_gateway_link(link):
            target = _Target(link, depth)
            # A next page passes the depth bound only while its page still lists postings.
            if depth_allows_gateway or (link.collection_pagination and progressed):
                automatic.append(target)
            else:
                deferred.append(_Target(link, depth, False, "depth_limit_reached"))
        elif _repeated_shape_link(link) and link.target_shape not in probed_shapes:
            probed_shapes.add(link.target_shape)
            automatic.append(_Target(link, depth, True, "shape_probe"))
        elif _repeated_shape_link(link):
            deferred.append(_Target(link, depth, False, "shape_probe_pending"))
        else:
            deferred.append(_Target(link, depth, False, "posting_identity_not_established"))
    return automatic, deferred


def _deferred_reason_counts(targets: list[_Target]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for target in targets:
        _increment(reasons, target.deferred_reason)
    return reasons


def _start_traversal(
    links: tuple[PublisherLink, ...], root_url: str, *, employer_is_authoritative: bool = False
) -> _Traversal:
    state = _Traversal(
        frontier=deque(),
        posting_urls=set(),
        visited=set(),
        listings={},
        deferred=[],
        reasons={},
        root_host=_host_of(root_url),
        handoff_scopes=_handoff_scopes(root_url, links),
        root_directory=_root_directory(urlsplit(root_url).path),
        employer_is_authoritative=employer_is_authoritative,
    )
    admitted, off_host, outside_root_directory, _low_priority_navigation = (
        PublisherSourceFetcher._admissible_extensions(links, state)
    )
    # The root was already read, and visited counts toward the page budget, so it is dropped here.
    automatic, deferred = _classify_frontier_links(
        tuple(link for link in admitted if link.fetch_url.casefold() != root_url.casefold()),
        depth_allows_gateway=True,
        depth=1,
    )
    state.frontier.extend(automatic)
    state.deferred.extend(deferred)
    state.reasons = _deferred_reason_counts(deferred)
    if off_host:
        _increment(state.reasons, "off_host_link", off_host)
    if outside_root_directory:
        _increment(state.reasons, "outside_root_directory", outside_root_directory)
    return state


def _start_continuation(
    targets: tuple[SourceContinuationTarget, ...],
    *,
    root_host: str,
    root_directory: str,
    handoff_scopes: tuple[_HandoffScope, ...],
    employer_is_authoritative: bool = False,
) -> _Traversal:
    """Restore persisted traversal depth, automatic-policy evidence, and scope."""
    rebuilt = tuple(
        _Target(
            _continuation_link(target),
            target.depth,
            target.automatic_eligible,
            target.deferred_reason,
        )
        for target in targets
    )
    return _Traversal(
        frontier=deque(rebuilt),
        posting_urls={
            target.link.fetch_url.casefold()
            for target in rebuilt
            if LinkRelation.REPEATED_TARGET_SHAPE in target.link.relations
        },
        visited=set(),
        listings={},
        deferred=[],
        reasons={},
        root_host=root_host,
        handoff_scopes=handoff_scopes,
        root_directory=root_directory,
        employer_is_authoritative=employer_is_authoritative,
    )


def _store_listing(state: _Traversal, listing: SourceListing) -> None:
    identity = listing.url.casefold()
    state.listings.setdefault(identity, listing)
    state.posting_urls.add(identity)


def _fetch_recovery(exc: Exception) -> tuple[bool, str]:
    """Separate transient fetch failures from stable manual-recovery outcomes."""
    if isinstance(exc, RobotsDeniedError):
        return False, "policy_refused"
    if isinstance(exc, FetchPolicyError):
        return True, "fetch_policy_unavailable"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or status >= 500:
            return True, f"transient_http_{status}"
        return False, f"stable_http_{status}"
    if isinstance(exc, httpx.RequestError):
        return True, "transient_network_error"
    return False, "invalid_publisher_response"


def _read_outcome(
    root: PageClassification,
    listings: dict[str, SourceListing],
    deferred: list[_Target],
) -> SourceReadOutcome:
    if deferred:
        return SourceReadOutcome.CRAWL_CONTINUATION
    if listings:
        return SourceReadOutcome.PUBLISHER_LINKED_PAGES
    if root is PageClassification.JS_SHELL:
        return SourceReadOutcome.RENDER_REQUIRED
    if root is PageClassification.ERROR_OR_CHALLENGE:
        return SourceReadOutcome.ACCESS_BLOCKED
    return SourceReadOutcome.NO_SUPPORTED_REPRESENTATION


def _read_note(
    root: PageClassification,
    listings: dict[str, SourceListing],
    deferred: list[_Target],
) -> str:
    if deferred:
        automatic = sum(target.automatic_eligible for target in deferred)
        return (
            f"Saved {len(deferred)} publisher-declared URLs for recovery; "
            f"{automatic} remain eligible for automatic reading."
        )
    if listings:
        return f"Read {len(listings)} postings from publisher-linked pages."
    return f"The publisher page was classified as {root.value}."


def _increment(values: dict[str, int], key: str, amount: int = 1) -> None:
    values[key] = values.get(key, 0) + amount
