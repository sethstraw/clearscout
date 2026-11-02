"""Compose one relevance-review case into the call the shipped stage makes.

The service touches a database only to select candidates and record results.
Span building, prompt composition, and validation are pure, so a case reaches
them by handing over detached rows instead of a workspace. Nothing here reaches
a provider or a workspace; a caller that wants a real CV passes its inventory.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from evals.support.inventory import synthetic_inventory
from tailorcv.application.discovery.relevance_review import (
    RelevanceReviewItemFailure,
    RelevanceReviewService,
    review_batches,
)
from tailorcv.application.discovery.relevance_review_records import (
    RelevanceReviewPostingInput,
    ValidatedRelevanceReview,
)
from tailorcv.application.discovery.relevance_review_spans import RelevanceReviewPostingSpan
from tailorcv.application.job_search.models import RelevanceReviewBatchResponse
from tailorcv.application.master_cv.cv import CvInventory
from tailorcv.application.tailoring.stages.context import citable_entries
from tailorcv.infrastructure.persistence.models import PendingDiscovery

# A fetch time and source method the rows carry but no reviewed path reads, held
# fixed so two runs of one case are identical.
FETCHED_AT = datetime(2026, 6, 1, tzinfo=UTC)

UNKNOWN_RECOMMENDATION = "UNKNOWN"
# The line the search summary renders from the person's excluded conditions.
EXCLUSIONS_HEADER = "Hard exclusion conditions the model must honor: "
_EXCLUSION = re.compile(r"(.+?) \(reason: [^)]*\)(?:, |$)")
AXES = (
    "core_work_alignment",
    "core_qualification_alignment",
    "hard_condition",
    "decisive_basis",
)


class RelevanceSampleError(ValueError):
    """A relevance-review sample is malformed, or names no posting."""


def stated_conditions(search_context: str) -> tuple[str, ...]:
    """Return the excluded conditions a rendered search summary lists, without reasons."""
    for line in search_context.splitlines():
        if line.startswith(EXCLUSIONS_HEADER):
            listed = line.removeprefix(EXCLUSIONS_HEADER)
            return tuple(found.group(1) for found in _EXCLUSION.finditer(listed))
    return ()


@dataclass(frozen=True)
class RelevanceCase:
    """Everything deterministic one relevance-review batch runs on."""

    sample_id: str
    pending: list[PendingDiscovery]
    inputs: dict[str, RelevanceReviewPostingInput]
    inventory: CvInventory
    entry_ids: tuple[str, ...]
    spans: dict[str, tuple[RelevanceReviewPostingSpan, ...]]
    schema: type[BaseModel]
    search_context: str
    posting_block: str
    notes: dict[str, str]

    @property
    def pending_ids(self) -> set[str]:
        """Return the discovery identifiers this batch expects one answer each for."""
        return {item.id for item in self.pending}

    @property
    def person_notes(self) -> str:
        """Return the kept-posting notes block production sends with this batch."""
        return RelevanceReviewService.person_notes(self.pending, self.notes)

    def validate(
        self, response: RelevanceReviewBatchResponse
    ) -> tuple[list[ValidatedRelevanceReview], list[RelevanceReviewItemFailure]]:
        """Run the shipped validation over this batch's answer."""
        return RelevanceReviewService.validate_response(
            response, self.inputs, self.pending_ids, set(self.entry_ids)
        )


def relevance_batch(
    sample_id: str,
    search_context: str,
    postings: Sequence[Mapping[str, Any]],
    inventory: CvInventory,
) -> RelevanceCase:
    """Build the spans, schema, and posting block one review batch is sent."""
    if not postings:
        raise RelevanceSampleError(f"{sample_id}: a relevance-review batch needs a posting")
    pending = [
        PendingDiscovery(
            id=str(posting["id"]),
            employer=str(posting["employer"]),
            title=str(posting["title"]),
            location=str(posting.get("location") or ""),
        )
        for posting in postings
    ]
    inputs = {
        str(posting["id"]): RelevanceReviewPostingInput(
            body=str(posting["body"]), fetched_at=FETCHED_AT
        )
        for posting in postings
    }
    entry_ids = tuple(entry.id for entry in citable_entries(inventory))
    spans = RelevanceReviewService.spans(pending, inputs)
    for item in pending:
        if not spans[item.id]:
            raise RelevanceSampleError(f"{sample_id}: posting {item.id} yields no citation spans")
    return RelevanceCase(
        sample_id=sample_id,
        pending=pending,
        inputs=inputs,
        inventory=inventory,
        entry_ids=entry_ids,
        spans=spans,
        schema=RelevanceReviewService.batch_schema(
            inventory, conditions=stated_conditions(search_context)
        ),
        search_context=search_context,
        posting_block=RelevanceReviewService.posting_block(pending, spans),
        notes={
            str(posting["id"]): str(posting["note"])
            for posting in postings
            if str(posting.get("note") or "").strip()
        },
    )


def _sample_parts(sample: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return one sample's id, search context, and posting, refusing a malformed sample."""
    sample_id = str(sample.get("id") or "relevance sample")
    payload = sample.get("input")
    if not isinstance(payload, dict):
        raise RelevanceSampleError(f"{sample_id}: a relevance-review sample needs an input object")
    posting = payload.get("posting")
    if not isinstance(posting, dict):
        raise RelevanceSampleError(f"{sample_id}: input.posting must be an object")
    search_context = payload.get("search_context")
    if not isinstance(search_context, str) or not search_context.strip():
        raise RelevanceSampleError(f"{sample_id}: input.search_context must be stated")
    return sample_id, search_context, posting


def relevance_case(
    sample: Mapping[str, Any], inventory: CvInventory | None = None
) -> RelevanceCase:
    """Build the single-posting batch one sample is reviewed as, on the synthetic CV by default."""
    sample_id, search_context, posting = _sample_parts(sample)
    return relevance_batch(
        sample_id,
        search_context,
        [posting],
        inventory if inventory is not None else synthetic_inventory(),
    )


def sample_batches(samples: Sequence[Mapping[str, Any]]) -> list[list[list[int]]]:
    """Group sample positions, in order, as production would: tasks, then batches.

    A production task reviews under one search context, so each context is its own
    task, split by ``review_batches`` on the posting body length.
    """
    tasks: dict[str, list[int]] = {}
    bodies: list[int] = []
    for position, sample in enumerate(samples):
        _, search_context, posting = _sample_parts(sample)
        tasks.setdefault(search_context, []).append(position)
        bodies.append(len(str(posting.get("body", ""))))
    return [review_batches(positions, bodies.__getitem__) for positions in tasks.values()]


def relevance_group(
    label: str, samples: Sequence[Mapping[str, Any]], inventory: CvInventory
) -> RelevanceCase:
    """Build the one batch several samples sharing a search context are reviewed as."""
    parts = [_sample_parts(sample) for sample in samples]
    contexts = {search_context for _, search_context, _ in parts}
    if len(contexts) != 1:
        raise RelevanceSampleError(f"{label}: one batch reviews under one search context")
    postings = [posting for _, _, posting in parts]
    ids = [str(posting.get("id")) for posting in postings]
    if len(set(ids)) != len(ids):
        raise RelevanceSampleError(f"{label}: two samples in one batch name the same posting")
    return relevance_batch(label, contexts.pop(), postings, inventory)


def run_schema(
    samples: Iterable[Mapping[str, Any]], inventory: CvInventory | None = None
) -> type[BaseModel]:
    """Close one response schema over the CV and every condition a run states, as production."""
    cases = [relevance_case(sample, inventory) for sample in samples]
    if not cases:
        raise RelevanceSampleError("a relevance-review run needs a sample")
    conditions = tuple(
        dict.fromkeys(
            condition for case in cases for condition in stated_conditions(case.search_context)
        )
    )
    return RelevanceReviewService.batch_schema(cases[0].inventory, conditions=conditions)


def _reviewed_result(item: ValidatedRelevanceReview) -> dict[str, Any]:
    return {
        "completion": item.recommendation or UNKNOWN_RECOMMENDATION,
        "outcome": item.outcome,
        "axes": {name: getattr(item, name) for name in AXES},
        "posting_evidence_span_ids": [citation.span_id for citation in item.posting_citations],
        "master_cv_evidence_ids": list(item.master_cv_evidence_ids),
        "summary": item.summary,
        "unresolved_questions": list(item.unresolved_questions),
        "validation_failure": None,
    }


def _rejected_answer(
    response: RelevanceReviewBatchResponse | None, pending_id: str
) -> dict[str, Any] | None:
    """Return what the model answered about ``pending_id``, so a rejection can be diagnosed."""
    if response is None:
        return None
    item = next(
        (item for item in response.recommendations if item.pending_discovery_id == pending_id),
        None,
    )
    if item is None:
        return None
    return {
        "recommendation": item.recommendation,
        "outcome": item.outcome,
        "axes": {name: getattr(item, name, None) for name in AXES},
        "matched_exclusion": item.matched_exclusion,
        "summary": item.summary,
    }


def relevance_result(
    valid: Sequence[ValidatedRelevanceReview],
    failures: Sequence[RelevanceReviewItemFailure],
    response: RelevanceReviewBatchResponse | None = None,
) -> dict[str, Any]:
    """Return the solver output one reviewed posting produces."""
    if valid:
        return _reviewed_result(valid[0])
    failure = failures[0] if failures else None
    rejected = _rejected_answer(response, failure.pending_discovery_id) if failure else None
    return {
        "completion": UNKNOWN_RECOMMENDATION,
        "outcome": None,
        "axes": dict.fromkeys(AXES),
        "posting_evidence_span_ids": [],
        "master_cv_evidence_ids": [],
        "summary": "",
        "unresolved_questions": [],
        # A live rejection costs money, so the row carries the reason and the
        # answer that was rejected rather than a bare code.
        "validation_failure": {
            "reason": failure.failure_code if failure else "missing_result",
            "detail": failure.detail if failure else "",
            "rejected_answer": rejected,
        },
    }
