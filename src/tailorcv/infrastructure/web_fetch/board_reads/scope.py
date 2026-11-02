"""The publisher scope a continuation target belongs to."""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
)
from tailorcv.domain.identity import normalize_fetch_url
from tailorcv.infrastructure.web_fetch.board_platform import host_match

NATIVE_LISTED_PLATFORMS = frozenset({"jobvite", "avature", "successfactors", "jazzhr", "icims"})


def listed_posting_reason(platform: str) -> str:
    """Return the reason a listed-page platform defers one posting page under."""
    return f"{platform}_posting_pending"


def listed_sitemap_reason(platform: str) -> str:
    """Return the reason a listed-page platform defers one unread child sitemap under."""
    return f"{platform}_sitemap_pending"


def _parsed_member(url: str, source_url: str, platform: str) -> list[dict[str, str]] | None:
    """Return the row a domain-served platform's parser reads ``url`` as under ``source_url``.

    None means the platform's URL shapes, not a parser, decide membership.
    """
    from tailorcv.infrastructure.web_fetch.board_mappings import (
        avature_openings,
        avature_portal,
        successfactors_brand,
        successfactors_openings,
    )

    if platform == "successfactors":
        host = urlsplit(source_url).netloc
        return successfactors_openings([url], host, successfactors_brand(source_url))
    if platform == "avature":
        portal = avature_portal(source_url)
        return avature_openings([url], *portal) if portal is not None else []
    return None


@lru_cache(maxsize=8192)
def _publisher_member_scope(url: str, source_url: str, platform: str) -> bool:
    from urllib.parse import parse_qs

    from tailorcv.infrastructure.web_fetch.boards import board_read_scope, canonicalise_source_url

    address, source = urlsplit(url), urlsplit(source_url)
    if (address.scheme, address.netloc) != (source.scheme, source.netloc):
        return False
    parsed = _parsed_member(url, source_url, platform)
    if parsed is not None:
        return bool(parsed)
    declared = board_read_scope(source_url, platform)
    member = board_read_scope(canonicalise_source_url(url), platform)
    if declared is None:
        return platform not in {"jobvite", "avature", "jazzhr"}
    if member is None:
        if platform == "fxrecruiter":
            return (
                source.path.rstrip("/") == "/jobs"
                and re.fullmatch(r"/jobs/details/.+/[0-9]+/?", address.path) is not None
            )
        if platform == "ycombinator":
            prefix = source.path.rstrip("/") + "/"
            return re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+/?", address.path) is not None
        if platform == "darwinbox":
            prefix = source.path.rsplit("/", 1)[0] + "/jobDetails/"
            return re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+/?", address.path) is not None
        return False
    expected, actual = dict(declared), dict(member)
    if {key: value for key, value in expected.items() if key != "query"} != {
        key: value for key, value in actual.items() if key != "query"
    }:
        return False
    query = parse_qs(expected.get("query", ""))
    detail_query = parse_qs(actual.get("query", ""))
    return all(detail_query.get(key, value) == value for key, value in query.items())


def _registered_publisher_source(
    platform: str, reason: str, url: str, *, declared: str = ""
) -> bool:
    """Whether ``url`` is a source ``platform`` reads, or one a read's checkpoint ``declared``."""
    from tailorcv.infrastructure.web_fetch.boards import board_read_scope
    from tailorcv.infrastructure.web_fetch.platforms import PLATFORMS

    registered = next(
        (entry for entry in PLATFORMS if entry.name == platform and reason in entry.continuations),
        None,
    )
    if registered is None:
        return False
    # No URL shape names an Avature portal on the employer's own domain; its read's checkpoint does.
    if platform == "avature" and declared == platform and board_read_scope(url, platform) is None:
        from tailorcv.infrastructure.web_fetch.board_mappings import avature_portal

        return avature_portal(url) is not None
    scoped = any(
        {"host", "tenant", "site", "company"} & pattern.groupindex.keys()
        for pattern in registered.board_patterns
    )
    return not scoped or board_read_scope(url, platform) is not None


def _native_listing_page_matches(
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    platform: str,
) -> bool:
    """Require the exact root or an owned native sitemap page."""
    from tailorcv.infrastructure.web_fetch.boards import canonicalise_source_url

    page, root = urlsplit(target.url), urlsplit(target.source_url)
    named = normalize_fetch_url(target.url)
    # A classic iCIMS sitemap read names the tenant's canonical root, not the saved form.
    roots = {normalize_fetch_url(target.source_url)}
    if platform == "icims":
        roots.add(normalize_fetch_url(canonicalise_source_url(target.source_url)))
    if (
        normalize_fetch_url(checkpoint.board_url) != named
        or checkpoint.pending_ids
        or (page.scheme, page.netloc) != (root.scheme, root.netloc)
        or (
            named not in roots
            and (
                target.deferred_reason != listed_sitemap_reason(platform)
                or not page.path.endswith(".xml")
            )
        )
    ):
        return False
    if platform != "avature":
        return True
    from tailorcv.infrastructure.web_fetch.board_mappings import avature_portal

    # A portal read from one of its pages still owns the sitemaps under the portal itself.
    portal = avature_portal(target.source_url)
    return normalize_fetch_url(target.url) == normalize_fetch_url(target.source_url) or (
        portal is not None and page.path.startswith(f"/{portal[1]}/")
    )


def _native_listing_target_matches(
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    platform: str,
) -> bool:
    """Validate the native cursor and the original identity it selects."""
    if platform == "jazzhr" and target.deferred_reason == "jazzhr_detail_rows_pending":
        from tailorcv.infrastructure.web_fetch.boards import board_read_scope

        root = board_read_scope(target.source_url, platform)
        return (
            root is not None
            and root == board_read_scope(target.url, platform)
            and normalize_fetch_url(checkpoint.board_url) == normalize_fetch_url(target.source_url)
            and (not checkpoint.platform.startswith("listed:") or not checkpoint.pending_ids)
        )
    if checkpoint.platform.startswith("listed:"):
        return _native_listing_page_matches(target, checkpoint, platform)
    membership = dict(checkpoint.listing_urls)
    resolved = (
        set(checkpoint.pending_ids)
        | set(dict(checkpoint.completed))
        | set(dict(checkpoint.protected))
    )
    return (
        target.deferred_reason == listed_posting_reason(platform)
        and len(checkpoint.listing_ids) == 1
        and normalize_fetch_url(checkpoint.board_url) == normalize_fetch_url(target.url)
        and membership.get(checkpoint.listing_ids[0]) == target.url
        and set(checkpoint.listing_ids) <= resolved
    )


def _native_listing_member_matches(
    identity: str,
    url: str,
    source_url: str,
    platform: str,
) -> bool:
    """Match one original native identity through the platform's posting parser."""
    from tailorcv.infrastructure.web_fetch.platforms import PLATFORMS

    parsed = _parsed_member(url, source_url, platform)
    if parsed is not None:
        valid_identity = bool(parsed) and parsed[0]["id"] == identity
    else:
        registered = next(entry for entry in PLATFORMS if entry.name == platform)
        valid_identity = any(
            (match := host_match(pattern, url)) is not None and match.group("job") == identity
            for pattern in registered.job_patterns
        )
    return valid_identity and _publisher_member_scope(url, source_url, platform)


def _native_listed_scope(
    target: SourceContinuationTarget,
    checkpoint: SourceDetailCheckpoint,
    posting_urls: tuple[str, ...],
) -> tuple[str, str] | None:
    """Validate original native row identities within one declared source root."""
    platform = checkpoint.platform.removeprefix("listed:")
    membership = dict(checkpoint.listing_urls)
    from tailorcv.infrastructure.web_fetch.platforms import PLATFORMS

    registered = next(entry for entry in PLATFORMS if entry.name == platform)
    reason = target.deferred_reason
    if (
        reason not in registered.continuations
        or not _registered_publisher_source(platform, reason, target.source_url, declared=platform)
        or reason
        not in {
            listed_posting_reason(platform),
            listed_sitemap_reason(platform),
            "jazzhr_detail_rows_pending" if platform == "jazzhr" else "",
        }
    ):
        return None
    if not _native_listing_target_matches(target, checkpoint, platform):
        return None
    if any(
        not _native_listing_member_matches(identity, url, target.source_url, platform)
        for identity, url in membership.items()
    ):
        return None
    if any(not _publisher_member_scope(url, target.source_url, platform) for url in posting_urls):
        return None
    return platform, normalize_fetch_url(target.source_url)


def publisher_search_scope(
    target: SourceContinuationTarget, *, posting_urls: tuple[str, ...] = ()
) -> tuple[str, str] | None:
    """Validate declared publisher membership and the exact work one cursor names."""
    checkpoint = target.detail_checkpoint
    if checkpoint is None:
        return None
    membership = dict(checkpoint.listing_urls)
    if set(membership) != set(checkpoint.listing_ids) or any(
        membership.get(item) != url for item, url in (*checkpoint.completed, *checkpoint.protected)
    ):
        return None
    if checkpoint.platform.removeprefix("listed:") in NATIVE_LISTED_PLATFORMS:
        return _native_listed_scope(target, checkpoint, posting_urls)
    if target.deferred_reason.endswith("_search_page"):
        platform = target.deferred_reason.removesuffix("_search_page")
        if (
            checkpoint.platform != "search:" + target.deferred_reason
            or normalize_fetch_url(checkpoint.board_url) != normalize_fetch_url(target.source_url)
            or checkpoint.pending_ids
        ):
            return None
    elif target.deferred_reason.endswith("_search_detail"):
        platform = target.deferred_reason.removesuffix("_search_detail")
        if (
            checkpoint.platform != platform
            or len(checkpoint.pending_ids) != 1
            or normalize_fetch_url(checkpoint.board_url) != normalize_fetch_url(target.url)
            or membership.get(checkpoint.pending_ids[0]) != target.url
        ):
            return None
    else:
        return None
    if not _registered_publisher_source(platform, target.deferred_reason, target.source_url):
        return None
    if platform != "alignerr" and any(
        not _publisher_member_scope(url, target.source_url, platform)
        for url in (*membership.values(), *posting_urls)
    ):
        return None
    return platform, normalize_fetch_url(target.source_url)


def scope_owned_by_root(scope_url: str, root_url: str) -> bool:
    """Whether a publisher scope belongs to the source read at ``root_url``."""
    scope, root = urlsplit(normalize_fetch_url(scope_url)), urlsplit(normalize_fetch_url(root_url))
    if scope == root:
        return True
    # A site-wide search scopes to the host root while the source was saved at a sub-path.
    return scope.path in {"", "/"} and (scope.scheme, scope.netloc) == (root.scheme, root.netloc)
