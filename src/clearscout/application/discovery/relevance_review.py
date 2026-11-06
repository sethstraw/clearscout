"""Automatic relevance review of readable, selected pending discoveries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from itertools import product
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model

from clearscout.application.config import Settings, Stage
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
)
from clearscout.application.discovery.paid_task import billed_run
from clearscout.application.discovery.posting_fetch import PostingFetcher
from clearscout.application.discovery.refusal_patterns import (
    RefusedPosting,
    file_refusal,
    refresh_proposals,
)
from clearscout.application.discovery.relevance_review_preparation import (
    PreparedRelevanceReview,
    RelevanceReviewPreparationItem,
    RelevanceReviewPreparationService,
)
from clearscout.application.discovery.relevance_review_records import (
    RelevanceReviewContract,
    RelevanceReviewPostingInput,
    RelevanceReviewRecorder,
    ValidatedRelevanceReview,
)
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewItemFailure,
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.relevance_review_spans import (
    RelevanceReviewAliases,
    RelevanceReviewPostingSpan,
    posting_spans,
)
from clearscout.application.job_search.models import (
    RelevanceReviewAnswerOutcome,
    RelevanceReviewBatchResponse,
    RelevanceReviewCitation,
    RelevanceReviewCoreWorkAlignment,
    RelevanceReviewQualificationAlignment,
    RelevanceReviewRecommendation,
    RelevanceReviewVerdict,
)
from clearscout.application.master_cv.cv import CvInventory
from clearscout.application.prompts import flatten, render
from clearscout.application.tailoring.stages.context import citable_entries, record_block
from clearscout.application.tasks import RelevanceReviewPayload, TaskContext
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import PendingDiscovery
from clearscout.infrastructure.providers.base import Provider
from clearscout.infrastructure.providers.plan import RequestPlan, Segment, digest_of, schema_digest

# The failure type is part of this module's surface: the review outcome carries
# it, and the evaluations and tests that read that outcome import it from here.
__all__ = [
    "ALLOWED_VERDICTS",
    "REVIEW_BATCH_ITEMS",
    "RelevanceReviewHandler",
    "RelevanceReviewItemFailure",
    "RelevanceReviewOutcome",
    "RelevanceReviewService",
    "current_review_contract",
    "response_schema",
    "review_batches",
    "review_plan",
]

REVIEW_BATCH_ITEMS = 20
_MAX_BATCH_CHARACTERS = 120_000
# Listing excerpts run to about 1,100 characters, so a longer body is a fuller
# posting: never too short to judge, and a verdict citing nothing past this never read it.
_LEADING_EXCERPT_CHARACTERS = 1_200

_ANY_WORK = get_args(RelevanceReviewCoreWorkAlignment)
_ANY_QUALIFICATION = get_args(RelevanceReviewQualificationAlignment)
_OPEN_QUALIFICATION = ("partially_established", "unresolved")
_UNSETTLED = ("passes", "unresolved")

# The prompt's class definitions, one row per case it names: decisive basis, then
# the core work, core qualification, hard condition, and recommendation each allows.
_CLASS_DEFINITIONS: tuple[
    tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...
] = (
    ("hard_conflict", _ANY_WORK, _ANY_QUALIFICATION, ("conflicts",), ("unlikely",)),
    (
        "insufficient_posting",
        ("unresolved",),
        _ANY_QUALIFICATION,
        ("unresolved",),
        ("insufficient_evidence",),
    ),
    (
        "insufficient_posting",
        ("direct", "transferable"),
        ("established", *_OPEN_QUALIFICATION),
        _UNSETTLED,
        ("investigate",),
    ),
    ("core_gap", ("absent",), _ANY_QUALIFICATION, _UNSETTLED, ("unlikely",)),
    ("core_gap", ("transferable",), ("absent",), _UNSETTLED, ("unlikely",)),
    ("core_gap", ("direct",), ("absent",), _UNSETTLED, ("investigate",)),
    ("logistics_unknown", ("direct",), ("established",), ("passes",), ("review",)),
    ("logistics_unknown", ("direct",), ("established",), ("unresolved",), ("investigate",)),
    ("core_match", ("direct",), ("established",), ("passes",), ("review",)),
    ("transferable_match", ("direct",), _OPEN_QUALIFICATION, _UNSETTLED, ("investigate",)),
    (
        "transferable_match",
        ("transferable",),
        ("established", *_OPEN_QUALIFICATION),
        _UNSETTLED,
        ("investigate",),
    ),
)

# Every judged answer the class definitions allow, as (core work, core
# qualification, hard condition, decisive basis, recommendation).
ALLOWED_VERDICTS: frozenset[tuple[str, str, str, str, str]] = frozenset(
    (work, qualification, hard, basis, recommendation)
    for basis, works, qualifications, hards, recommendations in _CLASS_DEFINITIONS
    for work, qualification, hard, recommendation in product(
        works, qualifications, hards, recommendations
    )
)


def current_review_contract() -> RelevanceReviewContract:
    """Return the digests of the relevance instructions and schema in force now.

    The instructions are rendered on every call, because a bound prompt override
    changes them per workspace.
    """
    return RelevanceReviewContract(
        instructions_digest=digest_of(render("relevance_review_system")),
        schema_digest=_shape_digest(),
    )


@lru_cache(maxsize=1)
def _shape_digest() -> str:
    """Digest the schema's shape with placeholder ids; the wire schema closes over each CV."""
    shape = response_schema(("entry",), conditions=("condition",))
    return schema_digest(shape.model_json_schema())


def _judged_failure(
    pending_id: str,
    item: RelevanceReviewRecommendation,
    body: str,
    citations: list[RelevanceReviewCitation],
) -> RelevanceReviewItemFailure | None:
    """Reject a judged answer that is ungrounded, impossible, or read only an excerpt."""
    candidate_alignment = (
        item.core_work_alignment in {"direct", "transferable"}
        or item.core_qualification_alignment in {"established", "partially_established"}
        or item.decisive_basis in {"core_match", "transferable_match"}
    )
    if candidate_alignment and not item.master_cv_evidence_ids:
        return RelevanceReviewItemFailure(
            pending_id,
            "invalid_cv_evidence",
            "A positive direct or transferable recommendation must cite master-CV evidence.",
        )
    combination = (
        item.core_work_alignment,
        item.core_qualification_alignment,
        item.hard_condition,
        item.decisive_basis,
        item.recommendation,
    )
    if combination not in ALLOWED_VERDICTS:
        return RelevanceReviewItemFailure(
            pending_id,
            "inconsistent_verdict",
            "The classes and recommendation form no combination the class definitions allow: "
            + ", ".join(combination)
            + ".",
        )
    if (item.decisive_basis == "hard_conflict") != (item.matched_exclusion is not None):
        return RelevanceReviewItemFailure(
            pending_id,
            "inconsistent_verdict",
            "A hard conflict must name the listed exclusion condition it matched, "
            "and no other basis may name one.",
        )
    # A stated exclusion decides the posting wherever it appears in the text.
    if (
        item.decisive_basis != "hard_conflict"
        and len(body) > _LEADING_EXCERPT_CHARACTERS
        and all(citation.char_end <= _LEADING_EXCERPT_CHARACTERS for citation in citations)
    ):
        return RelevanceReviewItemFailure(
            pending_id,
            "leading_excerpt_only",
            "The result cited only the leading excerpt of a longer posting.",
        )
    return None


def _preparation_report(
    outcome_counts: Mapping[str, int],
    preparation: Sequence[RelevanceReviewPreparationItem],
) -> dict[str, Any]:
    """Return how preparation accounted for the candidates, by outcome and by row."""
    return {
        "preparation_outcomes": dict(outcome_counts),
        "preparation": [asdict(item) for item in preparation],
    }


@dataclass(frozen=True)
class RelevanceReviewOutcome:
    """What one relevance-review pass reviewed, recorded, and could not finish."""

    candidates: int = 0
    readable: int = 0
    reviewed: int = 0
    batches: int = 0
    model_calls_made: int = 0
    recommendations: int = 0
    validation_failures: tuple[RelevanceReviewItemFailure, ...] = ()
    unsent_postings_answered: tuple[str, ...] = ()
    preparation_outcomes: Mapping[str, int] = field(default_factory=dict)
    preparation: tuple[RelevanceReviewPreparationItem, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the task-result shape, which is read by name in the SPA."""
        # The task list names an empty list by its length, so the key appears only when it has ids.
        unsent = (
            {"unsent_postings_answered": list(self.unsent_postings_answered)}
            if self.unsent_postings_answered
            else {}
        )
        return {
            "candidates": self.candidates,
            "readable": self.readable,
            "reviewed": self.reviewed,
            "batches": self.batches,
            "model_calls_made": self.model_calls_made,
            "recommendations": self.recommendations,
            "validation_failures": [asdict(item) for item in self.validation_failures],
            **unsent,
            **_preparation_report(self.preparation_outcomes, self.preparation),
        }


def response_schema(entry_ids: tuple[str, ...], *, conditions: tuple[str, ...]) -> type[BaseModel]:
    """Close model citations over the CV evidence and exclusions every batch of a search shares.

    Posting and span names are open strings; validation refuses one the batch never issued, and
    the task outcome lists each unissued posting name an answer used.
    """
    if entry_ids:
        evidence: Any = list[Literal[entry_ids]]  # type: ignore[valid-type]
        evidence_limit = 10
    else:
        evidence = list[str]
        evidence_limit = 0
    exclusion: Any = Literal[conditions] | None if conditions else None
    hard_condition: Any = Literal["passes", "conflicts", "unresolved"]
    decisive_basis: Any = Literal[
        "core_match",
        "transferable_match",
        "core_gap",
        "hard_conflict",
        "logistics_unknown",
        "insufficient_posting",
    ]
    item = create_model(
        "ConstrainedRelevanceReviewRecommendation",
        __config__=ConfigDict(extra="forbid"),
        pending_discovery_id=(str, ...),
        summary=(str, Field(min_length=1)),
        outcome=(RelevanceReviewAnswerOutcome, "judged"),
        core_work_alignment=(RelevanceReviewCoreWorkAlignment, ...),
        core_qualification_alignment=(RelevanceReviewQualificationAlignment, ...),
        hard_condition=(hard_condition, ...),
        matched_exclusion=(exclusion, None),
        decisive_basis=(decisive_basis, ...),
        posting_evidence_span_ids=(
            list[str],
            Field(min_length=1, max_length=5),
        ),
        master_cv_evidence_ids=(evidence, Field(default_factory=list, max_length=evidence_limit)),
        unresolved_questions=(list[str], Field(default_factory=list, max_length=8)),
        recommendation=(RelevanceReviewVerdict, ...),
    )
    return create_model(
        "ConstrainedRelevanceReview",
        __config__=ConfigDict(extra="forbid"),
        recommendations=(
            list[item],  # type: ignore[valid-type]
            Field(min_length=1, max_length=REVIEW_BATCH_ITEMS),
        ),
    )


def review_batches[T](items: Sequence[T], characters_of: Callable[[T], int]) -> list[list[T]]:
    """Split ``items``, in order, into the bounded batches one review task sends."""
    batches: list[list[T]] = []
    current: list[T] = []
    characters = 0
    for item in items:
        item_characters = characters_of(item)
        if current and (
            len(current) == REVIEW_BATCH_ITEMS
            or characters + item_characters > _MAX_BATCH_CHARACTERS
        ):
            batches.append(current)
            current = []
            characters = 0
        current.append(item)
        characters += item_characters
    if current:
        batches.append(current)
    return batches


def review_plan(
    settings: Settings,
    inventory: CvInventory,
    search_context: str,
    posting_block: str,
    *,
    person_notes: str = "",
) -> RequestPlan:
    """Compose the request one relevance-review batch sends.

    Module level so an offline evaluation renders the shipped wiring rather than
    a copy of it.
    """
    return RequestPlan(
        stage=Stage.RELEVANCE_REVIEW,
        instructions=Segment(
            render("relevance_review_system"),
            "relevance_review_system",
        ),
        evidence=Segment(
            render(
                "relevance_review_evidence",
                search_parameters=search_context,
                master_cv_evidence=record_block(inventory),
            ),
            "relevance_review_evidence",
        ),
        task=Segment(
            render("relevance_review_user", postings=posting_block, person_notes=person_notes),
            "relevance_review_user",
        ),
        max_tokens=settings.policy_for(Stage.RELEVANCE_REVIEW).max_tokens,
    )


class RelevanceReviewService:
    """Review every readable selected candidate in bounded provider batches."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
        provider: Provider | None = None,
    ) -> None:
        """Bind preparation, persistence, and the provider used by relevance review."""
        self.database = database
        self.recorder = RelevanceReviewRecorder(database)
        self.rejections = RelevanceReviewRejectionService(database)
        self.preparation = RelevanceReviewPreparationService(
            database, storage=storage, fetcher=fetcher
        )
        self.provider = provider

    def review_candidates(
        self,
        workspace_id: str,
        pending_discovery_ids: list[str],
        context: TaskContext,
        *,
        require_retained: bool = False,
    ) -> RelevanceReviewOutcome:
        """Prepare, review, validate, and persist readable candidates."""
        if not pending_discovery_ids:
            return RelevanceReviewOutcome()
        contract = current_review_contract()
        prepared, preparation = self.preparation.prepare(
            workspace_id,
            pending_discovery_ids,
            contract=contract,
            require_retained=require_retained,
        )
        outcome_counts: dict[str, int] = {}
        for item in preparation:
            outcome_counts[item.outcome] = outcome_counts.get(item.outcome, 0) + 1
            if item.refused_gate:
                file_refusal(
                    self.database,
                    RefusedPosting(
                        workspace_id=workspace_id,
                        employer=item.employer,
                        title=item.title,
                        pending_discovery_id=item.pending_discovery_id,
                        search_parameter_set_id=prepared.search_parameter_set_id,
                    ),
                    condition=item.refused_gate,
                    reason=item.detail,
                    task_id=context.task_id,
                    origin="relevance_review.refusal",
                    refresh=False,
                )
        if outcome_counts.get("gate_refuse"):
            refresh_proposals(self.database, workspace_id)
        reviewable = list(prepared.pending)
        failures: list[RelevanceReviewItemFailure] = []
        unsent: list[str] = []
        recommendations = 0
        model_calls = 0
        batches = review_batches(reviewable, lambda item: len(prepared.inputs[item.id].body))
        schema = self._review_schema(prepared)
        completed = 0
        # Every payload repeats the preparation report, because a task keeps only its
        # last progress, and the rules applied here already dismissed rows for free.
        report = _preparation_report(outcome_counts, preparation)
        context.progress(
            "relevance_reviewing",
            {
                "phase": "reviewing_relevance",
                "completed": 0,
                "total": len(reviewable),
                "item_kind": "posting",
                **report,
            },
        )
        for batch_position, pending in enumerate(batches, start=1):
            context.raise_if_cancelled()
            context.progress(
                "relevance_reviewing",
                {
                    "phase": "reviewing_relevance",
                    "completed": completed,
                    "total": len(reviewable),
                    "item_kind": "review_batch",
                    "item_label": f"Review batch {batch_position} of {len(batches)}",
                    **report,
                },
            )
            inputs = {item.id: prepared.inputs[item.id] for item in pending}
            spans = self.spans(pending, inputs)
            aliases = RelevanceReviewAliases.issue(spans)
            response, calls = self._complete(
                workspace_id,
                prepared.settings,
                prepared.inventory,
                prepared.search_context,
                self.posting_block(pending, spans, aliases),
                self.person_notes(pending, prepared.notes, aliases),
                context,
                schema=schema,
            )
            valid, invalid = self.validate_response(
                response,
                inputs,
                aliases,
                {entry.id for entry in citable_entries(prepared.inventory)},
            )
            unsent.extend(aliases.unissued_postings(response))
            self.recorder.append(
                workspace_id=workspace_id,
                task_id=context.task_id,
                master_cv_version_id=prepared.master_cv_version_id,
                search_parameter_set_id=prepared.search_parameter_set_id,
                search_context=prepared.search_context,
                contract=contract,
                pending=pending,
                inputs=inputs,
                recommendations=valid,
                notes=prepared.notes,
            )
            self.rejections.record(
                workspace_id=workspace_id,
                task_id=context.task_id,
                rejected=invalid,
                accepted_ids=[item.pending_discovery_id for item in valid],
            )
            recommendations += len(valid)
            failures.extend(invalid)
            model_calls += calls
            completed += len(pending)
            context.progress(
                "relevance_reviewing",
                {
                    "phase": "reviewing_relevance",
                    "completed": completed,
                    "total": len(reviewable),
                    "item_kind": "posting",
                    **report,
                },
            )
        return RelevanceReviewOutcome(
            candidates=len(pending_discovery_ids),
            readable=len(prepared.pending),
            reviewed=completed,
            batches=len(batches),
            model_calls_made=model_calls,
            recommendations=recommendations,
            validation_failures=tuple(failures),
            unsent_postings_answered=tuple(unsent),
            preparation_outcomes=outcome_counts,
            preparation=tuple(preparation),
        )

    def content_candidate_ids(self, workspace_id: str, ids: list[str]) -> list[str]:
        """Return candidates whose deterministic rules permit a publisher read."""
        return self.preparation.content_candidate_ids(workspace_id, ids)

    def _review_schema(self, prepared: PreparedRelevanceReview) -> type[BaseModel]:
        """Keep the output schema stable before the provider's evidence breakpoint."""
        return self.batch_schema(
            prepared.inventory,
            conditions=tuple(
                condition
                for condition, _ in prepared.parameters.excluded_conditions
                if condition.strip()
            ),
        )

    @staticmethod
    def batch_schema(inventory: CvInventory, *, conditions: tuple[str, ...]) -> type[BaseModel]:
        """Close every batch's response over the CV ids it may cite and conditions it may name."""
        return response_schema(
            tuple(entry.id for entry in citable_entries(inventory)), conditions=conditions
        )

    def _complete(
        self,
        workspace_id: str,
        settings: Settings,
        inventory: CvInventory,
        search_context: str,
        posting_block: str,
        person_notes: str,
        context: TaskContext,
        *,
        schema: type[BaseModel],
    ) -> tuple[RelevanceReviewBatchResponse, int]:
        with billed_run(
            self.database,
            workspace_id=workspace_id,
            task_id=context.task_id,
            settings=settings,
            origin="discovery.relevance_review",
            provider=self.provider,
        ) as run:
            plan = review_plan(
                settings,
                inventory,
                search_context,
                posting_block,
                person_notes=person_notes,
            )
            raw = run.build_client().complete(plan, schema)
        response = RelevanceReviewBatchResponse.model_validate(raw.model_dump(mode="json"))
        return response, len(run.ledger.entries)

    @staticmethod
    def posting_block(
        pending: list[PendingDiscovery],
        spans: dict[str, tuple[RelevanceReviewPostingSpan, ...]],
        aliases: RelevanceReviewAliases,
    ) -> str:
        """Render the batch's postings and their citable spans under their batch names."""
        return "\n\n".join(
            f"DISCOVERY {aliases.posting_alias(item.id)}\n"
            f"Employer: {item.employer}\nTitle: {item.title}\n"
            f"Location: {item.location or 'not stated'}\nPosting spans:\n"
            + "\n".join(
                f"[SPAN {aliases.span_alias(span.id)}] {span.text}" for span in spans[item.id]
            )
            for item in pending
        )

    @staticmethod
    def person_notes(
        pending: list[PendingDiscovery],
        notes: Mapping[str, str],
        aliases: RelevanceReviewAliases,
    ) -> str:
        """Render the person's notes on kept postings in the batch, or nothing when none has one."""
        lines = [
            f"DISCOVERY {aliases.posting_alias(item.id)}\n"
            f"Person's note (kept this posting): {flatten(notes[item.id])}"
            for item in pending
            if item.id in notes
        ]
        if not lines:
            return ""
        return "The person wrote these notes on postings they kept:\n\n" + "\n\n".join(lines)

    @staticmethod
    def spans(
        pending: list[PendingDiscovery], inputs: dict[str, RelevanceReviewPostingInput]
    ) -> dict[str, tuple[RelevanceReviewPostingSpan, ...]]:
        """Return the citable spans of each pending posting in the batch."""
        return {item.id: posting_spans(item.id, inputs[item.id].body) for item in pending}

    @classmethod
    def validate_response(
        cls,
        response: RelevanceReviewBatchResponse,
        inputs: dict[str, RelevanceReviewPostingInput],
        aliases: RelevanceReviewAliases,
        allowed_evidence_ids: set[str],
    ) -> tuple[list[ValidatedRelevanceReview], list[RelevanceReviewItemFailure]]:
        """Split a batch answer, in its batch names, into accepted reviews and failures."""
        translated = aliases.translate(response)
        grouped = {
            pending_id: [
                item
                for item in translated.recommendations
                if item.pending_discovery_id == pending_id
            ]
            for pending_id in aliases.pending_ids
        }
        valid: list[ValidatedRelevanceReview] = []
        failures: list[RelevanceReviewItemFailure] = []
        for pending_id, items in grouped.items():
            result = cls._validate_one(pending_id, items, inputs[pending_id], allowed_evidence_ids)
            if isinstance(result, RelevanceReviewItemFailure):
                failures.append(result)
            else:
                valid.append(result)
        return valid, failures

    @staticmethod
    def _validate_one(
        pending_id: str,
        items: list[RelevanceReviewRecommendation],
        posting_input: RelevanceReviewPostingInput,
        allowed_evidence_ids: set[str],
    ) -> ValidatedRelevanceReview | RelevanceReviewItemFailure:
        if not items:
            return RelevanceReviewItemFailure(
                pending_id, "missing_result", "The provider returned no result for this posting."
            )
        if len(items) > 1:
            return RelevanceReviewItemFailure(
                pending_id,
                "duplicate_result",
                "The provider returned more than one result for this posting.",
            )
        item = items[0]
        span_by_id = {span.id: span for span in posting_spans(pending_id, posting_input.body)}
        unknown = set(item.posting_evidence_span_ids) - span_by_id.keys()
        if unknown:
            invented = all(span_id.startswith(f"{pending_id}:") for span_id in unknown)
            return RelevanceReviewItemFailure(
                pending_id,
                "wrong_posting_span",
                "The result cited a span this posting was never sent."
                if invented
                else "The result cited a span owned by another posting.",
            )
        if not set(item.master_cv_evidence_ids) <= allowed_evidence_ids:
            return RelevanceReviewItemFailure(
                pending_id,
                "invalid_cv_evidence",
                "The result cited evidence outside the selected master CV.",
            )
        citations = [
            RelevanceReviewCitation(
                span_id=span.id,
                text=span.text,
                char_start=span.char_start,
                char_end=span.char_end,
            )
            for span_id in item.posting_evidence_span_ids
            for span in [span_by_id[span_id]]
        ]
        if item.outcome == "insufficient_posting_text":
            if len(posting_input.body) > _LEADING_EXCERPT_CHARACTERS:
                return RelevanceReviewItemFailure(
                    pending_id,
                    "inconsistent_verdict",
                    "A posting longer than a listing excerpt was answered as too short to judge.",
                )
            return ValidatedRelevanceReview(
                pending_discovery_id=pending_id,
                outcome=item.outcome,
                recommendation=None,
                summary=item.summary,
                core_work_alignment=None,
                core_qualification_alignment=None,
                hard_condition=None,
                decisive_basis=None,
                posting_citations=citations,
                master_cv_evidence_ids=item.master_cv_evidence_ids,
                unresolved_questions=item.unresolved_questions,
            )
        if failure := _judged_failure(pending_id, item, posting_input.body, citations):
            return failure
        return ValidatedRelevanceReview(
            pending_discovery_id=pending_id,
            outcome=item.outcome,
            recommendation=item.recommendation,
            summary=item.summary,
            core_work_alignment=item.core_work_alignment,
            core_qualification_alignment=item.core_qualification_alignment,
            hard_condition=item.hard_condition,
            decisive_basis=item.decisive_basis,
            posting_citations=citations,
            master_cv_evidence_ids=item.master_cv_evidence_ids,
            unresolved_questions=item.unresolved_questions,
            matched_exclusion=item.matched_exclusion,
        )


class RelevanceReviewHandler:
    """Worker adapter for one relevance-review run over selected pending discoveries."""

    def __init__(
        self,
        database: Database,
        *,
        storage: RuntimeStorage,
        fetcher: PostingFetcher,
        content_resolution: PostingContentResolutionService | None = None,
        provider: Provider | None = None,
    ) -> None:
        """Configure the review service with its network, storage, and model seams."""
        self.service = RelevanceReviewService(
            database, storage=storage, fetcher=fetcher, provider=provider
        )
        self.content_resolution = content_resolution

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Resolve selected content, then review it for relevance."""
        value = RelevanceReviewPayload.model_validate(payload)
        task = context.queue.get(context.task_id)
        return self._review(str(task.workspace_id), value, context)

    def _review(
        self, workspace_id: str, payload: RelevanceReviewPayload, context: TaskContext
    ) -> dict[str, Any]:
        """Review the selected candidates for one approved durable task."""
        context.raise_if_cancelled()
        content_resolution = self._resolve_selected_content(
            workspace_id, payload.pending_discovery_ids, context
        )
        outcome = self.service.review_candidates(
            workspace_id,
            payload.pending_discovery_ids,
            context,
            require_retained=True,
        )
        return {
            "relevance_review": outcome.as_json(),
            "content_resolution": content_resolution,
            "model_calls_made": outcome.model_calls_made,
        }

    def _resolve_selected_content(
        self, workspace_id: str, pending_ids: list[str], context: TaskContext
    ) -> dict[str, Any]:
        """Resolve exact deterministic survivors before any paid relevance review."""
        candidate_ids = self.service.content_candidate_ids(workspace_id, pending_ids)
        if self.content_resolution is None:
            return {
                "selected": len(candidate_ids),
                "attempted": 0,
                "retained": 0,
                "outcomes": {},
                "evidence_ids": [],
                "withdrawn_ids": [],
            }
        return self.content_resolution.resolve_automatic(
            workspace_id,
            candidate_ids,
            context,
            origin="discovery.relevance_review",
        )
