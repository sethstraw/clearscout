"""Query rewrites and page fingerprints carried on a paged search cursor."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCheckpoint,
)


def with_query(url: str, **values: int | str) -> str:
    """Return one stable pagination address used only by durable continuation."""
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key not in values]
    query.extend((key, str(value)) for key, value in values.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(sorted(query)), ""))


def page_fingerprint(ids: Iterable[str]) -> str:
    """Return an order-independent digest of one publisher page's stable IDs."""
    import json
    from hashlib import sha256

    return sha256(json.dumps(sorted(set(ids)), separators=(",", ":")).encode()).hexdigest()


def retained_page_fingerprints(target: SourceContinuationTarget | None) -> tuple[str, ...]:
    """Read page digests retained in an internal continuation address."""
    from urllib.parse import parse_qs

    if target is None:
        return ()
    if target.detail_checkpoint is not None:
        return target.detail_checkpoint.page_fingerprints
    values = parse_qs(urlsplit(target.url).query).get("__reader_pages", [""])[0].split(",")
    return tuple(value for value in values if re.fullmatch(r"[0-9a-f]{64}", value))


def with_page_fingerprints(
    target: SourceContinuationTarget, fingerprints: Iterable[str]
) -> SourceContinuationTarget:
    """Retain page digests without changing the publisher source scope."""
    from urllib.parse import parse_qsl

    parsed = urlsplit(target.url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "__reader_pages"]
    checkpoint = target.detail_checkpoint or SourceDetailCheckpoint(
        platform="search:" + target.deferred_reason, board_url=target.source_url
    )
    return replace(
        target,
        url=urlunsplit(parsed._replace(query=urlencode(query))),
        detail_checkpoint=replace(checkpoint, page_fingerprints=tuple(dict.fromkeys(fingerprints))),
    )
