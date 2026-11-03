"""Sitemap locations a board publishes and the child sitemaps left to read."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from clearscout.application.discovery.source_reading import SourceContinuationTarget
from clearscout.infrastructure.web_fetch.board_reads.continuations import (
    answered_gone,
    page_continuation_target,
)
from clearscout.infrastructure.web_fetch.board_reads.payloads import required_feed_root
from clearscout.infrastructure.web_fetch.board_reads.results import BoardDeadlineReachedError
from clearscout.infrastructure.web_fetch.ingest.sitemap import parse_sitemap, xml_entity_references
from clearscout.infrastructure.web_fetch.policy import FetchPolicyError

if TYPE_CHECKING:
    from clearscout.infrastructure.web_fetch.board_platform import BoardTransport


# SuccessFactors sites still serve the Google namespace that predates sitemaps.org.
_SITEMAP_NAMESPACES = (
    "http://www.sitemaps.org/schemas/sitemap/0.9",
    "http://www.google.com/schemas/sitemap/0.9",
)


# One read takes this many of an index's child sitemaps; the rest become continuation targets.
SITEMAP_CHILDREN = 20


def sitemap_locations(
    fetcher: BoardTransport,
    url: str,
    platform: str,
    *,
    children: Callable[[list[str]], list[str]] = list,
) -> tuple[list[str], tuple[str, ...], int]:
    """Return a sitemap's pages, the child sitemaps left unread, and how many children are gone.

    The pages are those the sitemap or its first child sitemaps list, and
    ``children`` picks which of an index's child sitemaps the board reads. A body
    that is neither a urlset nor a sitemap index refuses the board. A child that
    fails to fetch or parse, or one the deadline leaves unfetched, is left unread;
    one answering 404 or 410 is gone and dropped.
    """
    is_index, locations = _sitemap(fetcher, url, platform)
    if not is_index:
        return locations, (), 0
    pages: list[str] = []
    selected = children(locations)
    first = selected[:SITEMAP_CHILDREN]
    unread = selected[SITEMAP_CHILDREN:]
    gone = 0
    for position, child in enumerate(first):
        try:
            nested, child_locations = _sitemap(fetcher, child, platform)
        except BoardDeadlineReachedError:
            unread = [*first[position:], *unread]
            break
        except (FetchPolicyError, httpx.HTTPError) as error:
            if answered_gone(error):
                gone += 1
            else:
                unread.append(child)
            continue
        except ValueError:
            # Some publishers answer a child sitemap with an empty 200 body now and then.
            unread.append(child)
            continue
        (unread if nested else pages).extend(child_locations)
    return pages, tuple(dict.fromkeys(unread)), gone


def _sitemap(fetcher: BoardTransport, url: str, platform: str) -> tuple[bool, list[str]]:
    """Return whether one fetched sitemap is an index, and the locations it lists."""
    text = fetcher.get_feed(url)
    is_index, locations = parse_sitemap(text)
    namespace = next((uri for uri in _SITEMAP_NAMESPACES if uri in text), None)
    if namespace is None:
        # Some SuccessFactors sites serve an RSS feed of postings at the sitemap address.
        required_feed_root(xml_entity_references(text), "rss", platform, url)
        return False, locations
    name = "sitemapindex" if is_index else "urlset"
    required_feed_root(text, f"{{{namespace}}}{name}", platform, url)
    return is_index, locations


def sitemap_child_targets(
    sitemaps: tuple[str, ...], *, source_url: str, reason: str = "sitemap_child_pending"
) -> tuple[SourceContinuationTarget, ...]:
    """Build one continuation target per child sitemap a read left unread."""
    return tuple(
        page_continuation_target(
            sitemap,
            source_url=source_url,
            label="Unread child sitemap",
            reason=reason,
        )
        for sitemap in sitemaps
    )
