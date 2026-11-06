"""Deterministic posting spans used by discovery relevance-review citations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from clearscout.application.job_search.models import (
    RelevanceReviewBatchResponse,
    RelevanceReviewRecommendation,
)

# No posting or span id the application issues contains a space, so a marked id never matches one.
_UNISSUED = "unissued "


@dataclass(frozen=True)
class RelevanceReviewPostingSpan:
    """One application-owned citation identity and its exact source text."""

    id: str
    text: str
    char_start: int
    char_end: int


def posting_spans(pending_discovery_id: str, body: str) -> tuple[RelevanceReviewPostingSpan, ...]:
    """Split literal source paragraphs into stable, bounded citation spans."""
    pieces = [
        piece
        for match in re.finditer(
            r"\S(?:.*?\S)?(?=[^\S\r\n]*(?:\r?\n[^\S\r\n]*\r?\n|\s*\Z))",
            body,
            flags=re.DOTALL,
        )
        for piece in _bounded_pieces(body, match.start(), match.end())
    ]
    return tuple(
        RelevanceReviewPostingSpan(
            id=f"{pending_discovery_id}:span-{index:04d}",
            text=body[start:end],
            char_start=start,
            char_end=end,
        )
        for index, (start, end) in enumerate(pieces, start=1)
    )


@dataclass(frozen=True)
class RelevanceReviewAliases:
    """The short posting and span names one batch shows the model, and the ids they stand for."""

    posting_aliases: Mapping[str, str] = field(default_factory=dict)
    span_aliases: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def issue(
        cls, spans: Mapping[str, Sequence[RelevanceReviewPostingSpan]]
    ) -> RelevanceReviewAliases:
        """Name each posting ``pN`` in batch order and each of its spans ``pN:K``."""
        postings = {pending_id: f"p{n}" for n, pending_id in enumerate(spans, start=1)}
        return cls(
            posting_aliases=postings,
            span_aliases={
                span.id: f"{postings[pending_id]}:{k}"
                for pending_id, posting in spans.items()
                for k, span in enumerate(posting, start=1)
            },
        )

    @property
    def pending_ids(self) -> set[str]:
        """Return the real posting ids this batch issued a name for."""
        return set(self.posting_aliases)

    def posting_alias(self, pending_id: str) -> str:
        """Return the name a posting is shown under, or its id when the batch never sent it."""
        return self.posting_aliases.get(pending_id, pending_id)

    def span_alias(self, span_id: str) -> str:
        """Return the name a span is shown under, or its id when the batch never sent it."""
        return self.span_aliases.get(span_id, span_id)

    def unissued_postings(self, response: RelevanceReviewBatchResponse) -> list[str]:
        """Return each posting name the answer used that this batch never issued, once."""
        issued = set(self.posting_aliases.values())
        return list(
            dict.fromkeys(
                item.pending_discovery_id
                for item in response.recommendations
                if item.pending_discovery_id not in issued
            )
        )

    def translate(self, response: RelevanceReviewBatchResponse) -> RelevanceReviewBatchResponse:
        """Return the answer with issued names replaced by real ids, and every other name marked.

        A marked name matches no real id, so a raw id or an unissued name fails validation.
        """
        postings = {alias: real for real, alias in self.posting_aliases.items()}
        spans = {alias: real for real, alias in self.span_aliases.items()}

        def span(name: str) -> str:
            if name in spans:
                return spans[name]
            owner, separator, rest = name.rpartition(":")
            if separator and owner in postings:
                return f"{postings[owner]}:{_UNISSUED}{rest}"
            return _UNISSUED + name

        return response.model_copy(
            update={
                "recommendations": [
                    RelevanceReviewRecommendation.model_validate(
                        {
                            **item.model_dump(),
                            "pending_discovery_id": postings.get(
                                item.pending_discovery_id, _UNISSUED + item.pending_discovery_id
                            ),
                            "posting_evidence_span_ids": [
                                span(name) for name in item.posting_evidence_span_ids
                            ],
                        }
                    )
                    for item in response.recommendations
                ]
            }
        )


def _bounded_pieces(
    body: str, start: int, end: int, *, ceiling: int = 800
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    while end - start > ceiling:
        split = body.rfind(" ", start, start + ceiling + 1)
        if split <= start:
            split = start + ceiling
        pieces.append((start, split))
        start = split
        while start < end and body[start].isspace():
            start += 1
    if start < end:
        pieces.append((start, end))
    return pieces
