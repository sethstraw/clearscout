"""Decide what one fetch means beside the observation already on record.

Nothing here reads or writes durable state: it takes the previous observation
and the fetch result and returns the body to store, its digest, the facts the
text states, and the field-by-field changes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_identity import keeps_stored_layout
from clearscout.application.discovery.posting_text import (
    PostingFacts,
    posting_facts,
    posting_fragments,
    publisher_closing_date,
)
from clearscout.application.failures import ValidationError
from clearscout.domain.staged_facts import PublisherClosingDate
from clearscout.infrastructure.web_fetch.ingest import url as url_ingest
from clearscout.infrastructure.web_fetch.listing_facts import stated_place


@dataclass(frozen=True)
class PreviousObservation:
    """The observation on record, with the body read back from its artifact."""

    job_id: str
    workspace_id: str
    # None for a job with no observation yet; its first refresh writes one.
    observation_id: str | None
    artifact_id: str | None
    body: str
    content_sha256: str | None
    posting_status: str
    location: str
    residence_rule: str
    employment_type: str
    payroll_arrangement: str
    fetched_at: datetime | None
    extraction_method: str
    source_url: str
    title: str
    publisher_closing_date: dict[str, Any] | None = None


@dataclass(frozen=True)
class RefreshDecision:
    """Normalized refresh content and deterministic posting facts."""

    fetched: FetchedPosting
    body: str
    body_bytes: bytes
    content_sha256: str
    facts: PostingFacts
    content_unchanged: bool
    changes: dict[str, Any]


def structured_changes(
    previous: PreviousObservation,
    new_body: str,
    fetched: FetchedPosting,
    new_sha: str,
    facts: PostingFacts,
) -> dict[str, Any]:
    """Compare the stated facts and body fragments field by field."""
    before_fragments = posting_fragments(previous.body)
    after_fragments = posting_fragments(new_body)
    changes: dict[str, Any] = {
        "status": {
            "changed": previous.posting_status != fetched.posting_status,
            "before": previous.posting_status,
            "after": fetched.posting_status,
        },
        "body": {
            "changed": previous.content_sha256 != new_sha,
            "before_sha256": previous.content_sha256,
            "after_sha256": new_sha,
        },
    }
    before_date = PublisherClosingDate.from_json(previous.publisher_closing_date)
    after_date = fetched.publisher_closing_date or PublisherClosingDate()
    before_statements = [replace(item, artifact_id=None) for item in before_date.statements]
    after_statements = [replace(item, artifact_id=None) for item in after_date.statements]
    changes["publisher_closing_date"] = {
        "changed": before_statements != after_statements,
        "before": previous.publisher_closing_date,
        "after": after_date.as_json(),
        "method": "publisher_closing_statements",
    }
    title_after = facts.title or previous.title
    changes["title"] = {
        "changed": previous.title != title_after,
        "before": previous.title,
        "after": title_after,
        "method": "deterministic_posting_facts",
    }
    for field in (
        "location",
        "residence_rule",
        "employment_type",
        "payroll_arrangement",
    ):
        before = getattr(previous, field)
        after = getattr(facts, field)
        changes[field] = {
            "changed": before != after,
            "before": before,
            "after": after,
            "method": "deterministic_posting_facts",
        }
    for field in ("compensation", "requirements"):
        before = before_fragments[field]
        after = after_fragments[field]
        changes[field] = {
            "changed": before != after,
            "before": before,
            "after": after,
            "method": "deterministic_text_fragments",
        }
    return changes


def decide_refresh(
    previous: PreviousObservation,
    fetched: FetchedPosting,
    title_hint: str,
    location_hint: str,
) -> RefreshDecision:
    """Normalize one fetch into the body, digest, facts, and changes to persist."""
    if (
        fetched.posting_status == "active"
        and fetched.publisher_markup_present is False
        and url_ingest.publisher_markup_was_present(previous.extraction_method)
    ):
        fetched = replace(
            fetched,
            text=None,
            posting_status="closed",
            method="publisher_markup_disappeared",
        )
    if fetched.posting_status == "closed":
        body = previous.body
        facts = PostingFacts()
        date = PublisherClosingDate.from_json(previous.publisher_closing_date)
    elif fetched.text is None or not fetched.text.strip():
        raise ValidationError("Posting refresh returned no usable body text.")
    else:
        # A read that says the same words in no richer a layout keeps the stored body, so
        # its digest and artifact stay those every earlier read was judged against.
        relaid = keeps_stored_layout(previous.body, fetched.text)
        body = previous.body if previous.artifact_id and relaid else fetched.text
        check = fetched.publisher_check
        publisher_place = (
            stated_place(check.location) if check is not None and check.status == "readable" else ""
        )
        # The caller's program-copied hint wins, then the publisher's own page or API
        # title, whose newer read renames the posting; the stored title keeps the
        # labeled-field and heading fallbacks to filling a blank one.
        facts = posting_facts(
            body,
            title_hint=title_hint or fetched.title or previous.title,
            location_hint=stated_place(location_hint) or publisher_place,
        )
        date = fetched.publisher_closing_date or publisher_closing_date(
            body,
            source_url=fetched.final_url or previous.source_url,
            observed_at=fetched.fetched_at,
        )
    fetched = replace(fetched, publisher_closing_date=date)
    body_bytes = body.encode("utf-8")
    content_sha256 = hashlib.sha256(body_bytes).hexdigest()
    content_unchanged = bool(previous.artifact_id and previous.content_sha256 == content_sha256)
    if content_unchanged and fetched.posting_status != "closed":
        facts = replace(
            facts,
            location=facts.location or previous.location,
            residence_rule=facts.residence_rule or previous.residence_rule,
            employment_type=facts.employment_type or previous.employment_type,
            payroll_arrangement=(facts.payroll_arrangement or previous.payroll_arrangement),
        )
    return RefreshDecision(
        fetched=fetched,
        body=body,
        body_bytes=body_bytes,
        content_sha256=content_sha256,
        facts=facts,
        content_unchanged=content_unchanged,
        changes=structured_changes(previous, body, fetched, content_sha256, facts),
    )
