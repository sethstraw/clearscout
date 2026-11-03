"""Read one posting URL through its publisher's API or page, under the shared fetch policy."""

from clearscout.infrastructure.web_fetch.ingest.url.checks import check_posting
from clearscout.infrastructure.web_fetch.ingest.url.feeds import shared_posting_feeds
from clearscout.infrastructure.web_fetch.ingest.url.page_text import (
    from_html,
)
from clearscout.infrastructure.web_fetch.ingest.url.platform_readers import posting_readers
from clearscout.infrastructure.web_fetch.ingest.url.reading import from_url, workday_posting_detail
from clearscout.infrastructure.web_fetch.ingest.url.results import (
    PublisherPostingUnverifiedError,
    PublisherReadRefusedError,
    UrlPostingText,
    publisher_markup_was_present,
)
from clearscout.infrastructure.web_fetch.ingest.url.transport import (
    JSON_HEADERS,
    MAX_POSTING_READ_SECONDS,
    PAGE_HEADERS,
    RETRY_PAUSE_SECONDS,
    TIMEOUT,
    USER_AGENT,
    guarded_posting_reads,
    posting_read_allowed,
)

__all__ = [
    "JSON_HEADERS",
    "MAX_POSTING_READ_SECONDS",
    "PAGE_HEADERS",
    "RETRY_PAUSE_SECONDS",
    "TIMEOUT",
    "USER_AGENT",
    "PublisherPostingUnverifiedError",
    "PublisherReadRefusedError",
    "UrlPostingText",
    "check_posting",
    "from_html",
    "from_url",
    "guarded_posting_reads",
    "posting_read_allowed",
    "posting_readers",
    "publisher_markup_was_present",
    "shared_posting_feeds",
    "workday_posting_detail",
]
