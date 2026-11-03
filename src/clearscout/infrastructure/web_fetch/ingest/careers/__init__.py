"""Extract publisher links and classify career pages from structural evidence."""

from clearscout.infrastructure.web_fetch.ingest.careers.classification import (
    LinkRelation,
    PageEvidence,
    PublisherLink,
    inspect_publisher_page,
)

__all__ = ["LinkRelation", "PageEvidence", "PublisherLink", "inspect_publisher_page"]
