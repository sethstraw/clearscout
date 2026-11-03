"""The board fetcher: a recognized board's adapter, else declared markup and sitemaps."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from functools import partial
from urllib.parse import urlsplit

import httpx

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
    SourceReadOptions,
)
from clearscout.domain import SourceReadOutcome, utc_now
from clearscout.domain.identity import normalize_fetch_url
from clearscout.domain.records import PageClassification
from clearscout.infrastructure.web_fetch.board_adapters.continuation_merge import (
    finished_target_urls,
    merged_continuation,
)
from clearscout.infrastructure.web_fetch.board_adapters.detail_reads import BoardDetailReader
from clearscout.infrastructure.web_fetch.board_adapters.identify import (
    BOARD_ADAPTER_REGISTRY,
    BoardResolution,
    identify_board,
    identify_board_url,
)
from clearscout.infrastructure.web_fetch.board_adapters.read_scope import (
    BOARD_DEADLINE_AT,
    DEFAULT_READ_OPTIONS,
    SOURCE_READ_OPTIONS,
    board_read_deadline,
)
from clearscout.infrastructure.web_fetch.board_mappings import icims_job_page
from clearscout.infrastructure.web_fetch.board_mappings.schema_org import declared_listing
from clearscout.infrastructure.web_fetch.board_platform import host_match
from clearscout.infrastructure.web_fetch.board_reads import (
    HAND_NAMED_EMPLOYER,
    BoardDeadlineReachedError,
    answered_gone,
    continued_page,
    gone_sitemap,
    held_continuation,
    owed_gone_postings,
    page_continuation_target,
    page_markup,
    read_result,
    retry_continuation,
    sitemap_child_targets,
)
from clearscout.infrastructure.web_fetch.ingest.careers import inspect_publisher_page
from clearscout.infrastructure.web_fetch.ingest.job_posting_markup import job_postings
from clearscout.infrastructure.web_fetch.ingest.sitemap import (
    SitemapLimits,
    discover_job_urls,
    expand_sitemaps,
)
from clearscout.infrastructure.web_fetch.platforms import PLATFORMS
from clearscout.infrastructure.web_fetch.platforms.icims import ICIMS_JOB
from clearscout.infrastructure.web_fetch.policy import (
    FetchPolicyError,
    HostHeldError,
    RobotsDeniedError,
)

# The sitemap tier reads at most this many discovered posting pages, the
# expensive part, so one large sitemap cannot fan out into hundreds of fetches
# under the board deadline.
SITEMAP_POSTING_FETCHES = 40


class BoardApiFetcher(BoardDetailReader):
    """Production fetcher for the JSON board APIs."""

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """Fetch one source's openings, from its board API when it has one.

        A recognized board is read through its API, falling back to the fetched
        page when robots refuses the API. Otherwise declared markup and visible
        collections are read before sitemap discovery.
        ``employer_is_authoritative`` keeps ``employer`` ahead of the name each
        record states.
        """
        token = BOARD_DEADLINE_AT.set(board_read_deadline())
        named = HAND_NAMED_EMPLOYER.set(employer_is_authoritative)
        selected = SOURCE_READ_OPTIONS.set(options)
        try:
            if not options.allows_posting(url):
                return SourceRead(
                    listings=[],
                    complete=False,
                    raw_count=1,
                    outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                    skip_reasons={"protected_posting": 1},
                    note="Skipped one protected posting without a detail read.",
                    attempted_target_urls=(),
                    protected_posting_urls=(url,),
                )
            resolution = self.resolve(url)
            if resolution.platform is not None and resolution.match is not None:
                try:
                    return self._dispatch(
                        employer, resolution.platform, resolution.match, resolution.url
                    )
                except (RobotsDeniedError, httpx.HTTPStatusError) as exc:
                    return self._publisher_api_fallback(
                        employer,
                        resolution,
                        exc,
                        employer_is_authoritative=employer_is_authoritative,
                        options=options,
                    )
            if resolution.page is None:
                raise ValueError(
                    "a source without a recognized board must include fetched page content"
                )
            declared = self._declared(employer, resolution.url, resolution.page)
            if declared is not None:
                return declared
            markup = page_markup(resolution.page)
            if (
                inspect_publisher_page(resolution.url, markup).classification
                is PageClassification.COLLECTION
            ):
                return self.publishers.read(
                    employer,
                    resolution.url,
                    markup,
                    employer_is_authoritative=employer_is_authoritative,
                    options=options,
                )
            from_sitemap = self._from_sitemap(employer, resolution.url, resolution.page)
            if from_sitemap is not None:
                return from_sitemap
            return self.publishers.read(
                employer,
                resolution.url,
                page_markup(resolution.page),
                employer_is_authoritative=employer_is_authoritative,
                options=options,
            )
        except BoardDeadlineReachedError as exc:
            return read_result(
                exc.listings,
                raw_count=exc.raw_count,
                complete=False,
                incomplete_reason="time_limit_reached",
            )
        finally:
            BOARD_DEADLINE_AT.reset(token)
            HAND_NAMED_EMPLOYER.reset(named)
            SOURCE_READ_OPTIONS.reset(selected)

    def _publisher_api_fallback(
        self,
        employer: str,
        resolution: BoardResolution,
        exc: RobotsDeniedError | httpx.HTTPStatusError,
        *,
        employer_is_authoritative: bool,
        options: SourceReadOptions,
    ) -> SourceRead:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        # Some classic iCIMS hosts refuse the sitemap to untrusted networks yet serve the page.
        classic_icims = resolution.platform == "icims" and bool(
            resolution.match and resolution.match.groupdict().get("tenant")
        )
        access_refused = status in {401, 403} and (
            resolution.platform == "workday" or classic_icims
        )
        if status is not None and not access_refused:
            raise exc
        if resolution.page is None and not (
            access_refused or resolution.platform in {"smartrecruiters", "workday"}
        ):
            raise exc
        page = resolution.page or self.get_text(resolution.url)
        publisher_url = page.partition("\n")[0] if page.startswith("http") else resolution.url
        # A classic iCIMS root redirected to another host is read as a Jibe board only when that
        # page loads the Jibe bundle, whose API lives on the redirected host; a refusal there
        # falls through to the publisher read.
        if (
            classic_icims
            and resolution.page is None
            and urlsplit(publisher_url).hostname != urlsplit(resolution.url).hostname
        ):
            found = identify_board(page, page_url=publisher_url)
            if found is not None and found[0] == "icims" and not found[1].groupdict():
                with suppress(RobotsDeniedError, httpx.HTTPStatusError, ValueError):
                    return self._dispatch(employer, found[0], found[1], publisher_url)
        fallback = self.publishers.read(
            employer,
            publisher_url,
            page_markup(page),
            employer_is_authoritative=employer_is_authoritative,
            options=options,
        )
        representation = (
            "already-readable" if resolution.page is not None else "independently permitted"
        )
        refusal = (
            "board_api_policy_refused"
            if isinstance(exc, RobotsDeniedError)
            else "board_api_access_refused"
        )
        return replace(
            fallback,
            skip_reasons={**fallback.skip_reasons, refusal: 1},
            note=(
                f"The board API refused automated access; the {representation} "
                f"publisher page was used instead. {fallback.note}"
            ),
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        employer_is_authoritative: bool = False,
        options: SourceReadOptions = DEFAULT_READ_OPTIONS,
    ) -> SourceRead:
        """Read an exact publisher or structured-board continuation frontier.

        ``employer_is_authoritative`` keeps ``employer`` ahead of the name each
        record states, as it does for a first read.
        """
        token = BOARD_DEADLINE_AT.set(board_read_deadline())
        named = HAND_NAMED_EMPLOYER.set(employer_is_authoritative)
        selected = SOURCE_READ_OPTIONS.set(options)
        try:
            readers = self._continuation_readers()
            reads: list[SourceRead] = []
            publisher_reads: list[tuple[SourceContinuationTarget, SourceRead]] = []
            protected = tuple(
                target.url for target in targets if not options.allows_posting(target.url)
            )
            if protected:
                reads.append(
                    SourceRead(
                        listings=[],
                        complete=False,
                        raw_count=len(protected),
                        outcome=SourceReadOutcome.PUBLISHER_LINKED_PAGES,
                        skip_reasons={"protected_posting": len(protected)},
                        note=f"Skipped {len(protected)} protected postings without detail reads.",
                        attempted_target_urls=(),
                        protected_posting_urls=protected,
                    )
                )
            allowed = tuple(target for target in targets if options.allows_posting(target.url))
            generic = tuple(target for target in allowed if target.deferred_reason not in readers)
            if generic:
                reads.append(
                    self.publishers.continue_listings(
                        employer,
                        generic,
                        root_url=root_url,
                        handoff_scopes=handoff_scopes,
                        employer_is_authoritative=employer_is_authoritative,
                        options=options,
                    )
                )
            structured = tuple(target for target in allowed if target.deferred_reason in readers)
            unread: tuple[SourceContinuationTarget, ...] = ()
            # A reader that pages on from its cursor may already have read a later target.
            finished: set[str] = set()
            for position, target in enumerate(structured):
                if normalize_fetch_url(target.url) in finished:
                    continue
                if self.deadline_elapsed():
                    unread = tuple(
                        pending
                        for pending in structured[position:]
                        if normalize_fetch_url(pending.url) not in finished
                    )
                    break
                read = _protect_search_metadata(readers[target.deferred_reason](employer, target))
                publisher_reads.append((target, read))
                finished |= finished_target_urls(read)
            publisher_reads = owed_gone_postings(publisher_reads)
            reads.extend(read for _target, read in publisher_reads)
            return merged_continuation(
                targets, reads, unread, publisher_reads=publisher_reads, root_url=root_url
            )
        finally:
            BOARD_DEADLINE_AT.reset(token)
            HAND_NAMED_EMPLOYER.reset(named)
            SOURCE_READ_OPTIONS.reset(selected)

    def _continuation_readers(
        self,
    ) -> dict[str, Callable[[str, SourceContinuationTarget], SourceRead]]:
        """Map each board-owned continuation reason to the reader that resumes it."""
        readers: dict[str, Callable[[str, SourceContinuationTarget], SourceRead]] = {
            reason: partial(reader, self)
            for platform in PLATFORMS
            for reason, reader in platform.continuations.items()
        }
        readers["sitemap_child_pending"] = self._continue_sitemap_child
        return readers

    def resolve(self, url: str) -> BoardResolution:
        """Identify the board named by the URL or its page."""
        found = identify_board_url(url)
        if found is not None:
            platform, match = found
            return BoardResolution(platform=platform, match=match, url=url, page=None)
        page = self._page(url)
        page_url = _page_url(url, page)
        found = identify_board(page, page_url=page_url)
        if found is not None:
            platform, match = found
            return BoardResolution(platform=platform, match=match, url=page_url, page=page)
        return BoardResolution(platform=None, match=None, url=page_url, page=page)

    def _page(self, url: str) -> str:
        """Read a career page once, for both the board hunt and its own markup."""
        return self.get_text(url)

    def _declared(self, employer: str, url: str, page: str) -> SourceRead | None:
        """Take the openings a page states about itself when it names no board.

        The fields are the publisher's own, so they arrive with the same
        provenance as a board API answer rather than as something a model read
        off the page. A page with no usable declaration falls through to the
        generic publisher reader.
        """
        declared = job_postings(page)
        if not declared:
            return None
        # A page describes the jobs it happens to embed, which is not a claim to
        # be the whole board, so absence here never means an opening was pulled.
        read_at = utc_now()
        return SourceRead(
            listings=[
                declared_listing(
                    posting,
                    employer=employer,
                    employer_is_authoritative=HAND_NAMED_EMPLOYER.get(),
                    page_url=url,
                    read_at=read_at,
                    page_markup=page_markup(page),
                )
                for posting in declared
            ],
            complete=False,
            raw_count=len(declared),
            outcome=SourceReadOutcome.PUBLISHER_INDEX,
            note="Read publisher-declared JobPosting data.",
        )

    def _from_sitemap(self, employer: str, url: str, page: str) -> SourceRead | None:
        """Read JobPosting markup from posting pages a site's sitemaps declare.

        Tried after the front page's declared postings and before the generic
        crawl. Returns None when discovery or the discovered pages yield no
        postings, so the read falls through to the crawl. The postings carry the
        publisher's own fields, the same provenance as a board API answer.
        """
        # An empty career-page read means the origin served nothing, so there is
        # no live site to probe for a sitemap.
        if self.deadline_elapsed() or not page_markup(page).strip():
            return None
        discovery = discover_job_urls(
            self._sitemap_fetch,
            url,
            limits=SitemapLimits(max_elapsed_seconds=self._remaining_seconds(url)),
        )
        candidates = _sitemap_posting_pages(discovery.candidate_urls)
        protected = [
            candidate
            for candidate in candidates
            if not SOURCE_READ_OPTIONS.get().allows_posting(candidate)
        ]
        eligible = [
            candidate
            for candidate in candidates
            if SOURCE_READ_OPTIONS.get().allows_posting(candidate)
        ]
        listings: list[SourceListing] = []
        seen: set[str] = set()
        processed = 0
        unread: list[str] = []
        fetched: list[str] = []
        read_at = utc_now()
        for candidate in eligible[:SITEMAP_POSTING_FETCHES]:
            if self.deadline_elapsed():
                break
            processed += 1
            markup = self._sitemap_fetch(candidate)
            if markup is None:
                unread.append(candidate)
                continue
            fetched.append(candidate)
            for posting in job_postings(markup):
                listing = declared_listing(
                    posting,
                    employer=employer,
                    employer_is_authoritative=HAND_NAMED_EMPLOYER.get(),
                    page_url=candidate,
                    read_at=read_at,
                    page_markup=markup,
                )
                identity = listing.url.casefold()
                if identity in seen:
                    continue
                seen.add(identity)
                listings.append(listing)
        deferred = (
            *(
                page_continuation_target(
                    candidate,
                    source_url=url,
                    label="Sitemap-discovered posting",
                    reason="sitemap_posting_pending",
                )
                for candidate in (*unread, *eligible[processed:])
            ),
            *sitemap_child_targets(discovery.pending_sitemaps, source_url=url),
        )
        if not listings and not deferred and not protected:
            return None
        skip_reasons = (
            {"sitemap_non_posting_url": discovery.non_posting_locs}
            if discovery.non_posting_locs
            else {}
        )
        if protected:
            skip_reasons["protected_posting"] = len(protected)
        return SourceRead(
            listings=listings,
            complete=False,
            raw_count=max(len(candidates), len(listings)),
            outcome=(
                SourceReadOutcome.CRAWL_CONTINUATION
                if deferred
                else SourceReadOutcome.PUBLISHER_INDEX
            ),
            skip_reasons=skip_reasons,
            note=_sitemap_read_note(discovery.truncated)
            + (
                f" Skipped {len(protected)} protected postings without detail reads."
                if protected
                else ""
            ),
            deferred_targets=deferred,
            # A page fetched here yields no frontier row, so this is the only
            # record that the traversal covered it.
            attempted_target_urls=tuple(fetched),
            protected_posting_urls=tuple(protected),
            discovery_truncated=discovery.truncated,
            exhausted_outcome=SourceReadOutcome.PUBLISHER_INDEX if deferred else None,
        )

    def _sitemap_fetch(self, url: str) -> str | None:
        """Fetch one sitemap, robots, or posting page through the page seam.

        Routing through ``get_text`` keeps every sitemap-tier read on the one
        overridable request seam, so a test double intercepts it. The final-URL
        prefix ``get_text`` adds is stripped so XML parses.
        """
        try:
            return page_markup(self.get_text(url))
        except (FetchPolicyError, httpx.HTTPError):
            return None

    def _continue_sitemap_child(
        self, _employer: str, target: SourceContinuationTarget
    ) -> SourceRead:
        """Read one sitemap a bound left unread into posting and child-sitemap targets."""
        failed: list[str] = []
        held: list[str] = []
        gone: list[str] = []

        def fetch(url: str) -> str | None:
            try:
                return page_markup(self.get_text(url))
            except HostHeldError:
                held.append(url)
            except (FetchPolicyError, httpx.HTTPError) as error:
                (gone if answered_gone(error) else failed).append(url)
            return None

        discovery = expand_sitemaps(
            fetch,
            (target.url,),
            limits=SitemapLimits(max_elapsed_seconds=self._remaining_seconds(target.url)),
        )
        if target.url in held:
            return held_continuation(target)
        if target.url in gone:
            return gone_sitemap(target)
        if target.url in failed:
            return retry_continuation(target, "sitemap_child_pending")
        unread = tuple(dict.fromkeys((*failed, *held, *discovery.pending_sitemaps)))
        deferred = (
            *(
                page_continuation_target(
                    candidate,
                    source_url=target.source_url,
                    label="Sitemap-discovered posting",
                    reason="sitemap_posting_pending",
                )
                for candidate in _sitemap_posting_pages(discovery.candidate_urls)
            ),
            *sitemap_child_targets(unread, source_url=target.source_url),
        )
        reasons = {
            "sitemap_non_posting_url": discovery.non_posting_locs,
            "sitemap_child_gone": len(set(gone)),
        }
        return replace(
            continued_page(target, [], deferred),
            skip_reasons={reason: count for reason, count in reasons.items() if count},
        )

    def _dispatch(self, employer: str, kind: str, match: re.Match[str], url: str) -> SourceRead:
        platform = BOARD_ADAPTER_REGISTRY.get(kind)
        if platform is None:
            raise ValueError(f"identified board platform has no adapter: {kind}")
        read = platform.adapter(self, employer, platform.adapter_groups(match), url)
        read = _protect_search_metadata(read)
        options = SOURCE_READ_OPTIONS.get()
        return replace(
            read,
            deferred_targets=tuple(
                target for target in read.deferred_targets if options.allows_posting(target.url)
            ),
        )


def _page_url(requested: str, page: str) -> str:
    """Return the address a career page was finally served from."""
    served = page.partition("\n")[0].strip()
    return served if served.startswith(("http://", "https://")) else requested


def _sitemap_posting_pages(urls: tuple[str, ...]) -> tuple[str, ...]:
    """Return the page that states each sitemap-listed posting's JobPosting markup, once each."""
    return tuple(
        dict.fromkeys(icims_job_page(url) if host_match(ICIMS_JOB, url) else url for url in urls)
    )


def _sitemap_read_note(truncated: bool) -> str:
    """State whether sitemap discovery itself stopped at a bound, not just the read budget."""
    note = "Read JobPosting data from sitemap-discovered pages."
    if truncated:
        note += " Sitemap discovery stopped at a bound before the site was fully read."
    return note


def _protect_search_metadata(read: SourceRead) -> SourceRead:
    """Retain protected list identities without scheduling a posting read."""
    if not any(
        item.source and item.source.publisher == "alignerr" and not item.published_description
        for item in read.listings
    ):
        return read
    protected = tuple(
        item.url for item in read.listings if not SOURCE_READ_OPTIONS.get().allows_posting(item.url)
    )
    return replace(
        read,
        protected_posting_urls=tuple(dict.fromkeys((*read.protected_posting_urls, *protected))),
        exhausted_outcome=read.exhausted_outcome
        if any(item.url not in protected for item in read.deferred_targets)
        else None,
        deferred_targets=tuple(item for item in read.deferred_targets if item.url not in protected),
    )
