"""Relevance review closes model citations and rejects bad answers locally.

It also accounts on the task for the free rules it applied before any paid call.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, get_args

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import CheckConstraint, Table, select

from clearscout.application.discovery.pending import set_pending_markers
from clearscout.application.discovery.relevance_review import (
    ALLOWED_VERDICTS,
    RelevanceReviewItemFailure,
    RelevanceReviewOutcome,
    RelevanceReviewService,
    response_schema,
)
from clearscout.application.discovery.relevance_review_queries import RelevanceReviewQueries
from clearscout.application.discovery.relevance_review_records import (
    RelevanceReviewPostingInput,
    ValidatedRelevanceReview,
)
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.scan_queries import SourceScanQueries
from clearscout.application.job_search.models import (
    RelevanceReviewBatchResponse,
    RelevanceReviewRecommendation,
)
from clearscout.application.tasks import TaskQueueService
from clearscout.bootstrap.worker import _LeaseKeeper, _WorkerTaskContext
from clearscout.domain.search_parameters import Arrangement, JobSearchParameters
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models.discovery import RelevanceReview
from clearscout.infrastructure.providers.plan import schema_digest
from clearscout.shared.costs import ServiceTier
from clearscout.shared.errors import QuotaError
from tests.support.artifacts import runtime_storage
from tests.support.doubles import ScriptedProvider, UnexpectedPostingFetcher
from tests.support.scenarios import workspace_with_retained_postings

_ENTRY_IDS = ("cv-1", "cv-2")
_BODY = "Freedonia Widgets seeks a staff engineer.\n\nThe team ships payment tooling."
_ALLOWED_EVIDENCE = {"cv-1", "cv-2"}
_CONDITIONS = ("Active security clearance required", "Relocation to another country")
_POSTING = RelevanceReviewPostingInput(
    body=_BODY,
    fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
)
_CONSTRAINED_FIELDS = {
    "recommendation": "ck_relevance_review_recommendation",
    "core_work_alignment": "ck_relevance_review_core_work",
    "core_qualification_alignment": "ck_relevance_review_core_qualification",
    "hard_condition": "ck_relevance_review_hard_condition",
    "decisive_basis": "ck_relevance_review_decisive_basis",
}


def _recommendation_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pending_discovery_id": "pd-1",
        "recommendation": "review",
        "summary": "Staff platform work with payment ownership.",
        "outcome": "judged",
        "core_work_alignment": "direct",
        "core_qualification_alignment": "established",
        "hard_condition": "passes",
        "decisive_basis": "core_match",
        "matched_exclusion": None,
        "posting_evidence_span_ids": ["pd-1:span-0001"],
        "master_cv_evidence_ids": ["cv-1"],
        "unresolved_questions": [],
    }
    payload.update(overrides)
    return payload


def _answer(**overrides: Any) -> dict[str, Any]:
    return {"recommendations": [_recommendation_payload(**overrides)]}


def _recommendation(**overrides: Any) -> RelevanceReviewRecommendation:
    return RelevanceReviewRecommendation.model_validate(_recommendation_payload(**overrides))


def _batch_schema() -> type[BaseModel]:
    return response_schema(_ENTRY_IDS, conditions=_CONDITIONS)


def _item_model(schema: type[BaseModel]) -> type[BaseModel]:
    (item,) = get_args(schema.model_fields["recommendations"].annotation)
    return cast(type[BaseModel], item)


def _database_values(field: str) -> set[str]:
    (constraint,) = [
        item
        for item in cast(Table, RelevanceReview.__table__).constraints
        if isinstance(item, CheckConstraint) and item.name == _CONSTRAINED_FIELDS[field]
    ]
    return set(re.findall(r"'([^']+)'", str(constraint.sqltext)))


def test_the_generated_schema_accepts_an_answer_citing_supplied_identifiers() -> None:
    parsed = _batch_schema().model_validate(_answer())

    assert parsed.model_dump(mode="json") == _answer()


def test_an_answer_about_a_discovery_the_batch_never_sent_leaves_its_posting_unanswered() -> None:
    response = RelevanceReviewBatchResponse.model_validate(
        _batch_schema().model_validate(_answer(pending_discovery_id="pd-other")).model_dump()
    )

    valid, invalid = RelevanceReviewService.validate_response(
        response, {"pd-1": _POSTING}, {"pd-1"}, _ALLOWED_EVIDENCE
    )

    assert valid == []
    assert [(item.pending_discovery_id, item.failure_code) for item in invalid] == [
        ("pd-1", "missing_result")
    ]


def test_the_generated_schema_rejects_a_cv_evidence_id_that_was_not_supplied() -> None:
    with pytest.raises(ValidationError):
        _batch_schema().model_validate(_answer(master_cv_evidence_ids=["cv-999"]))


def test_the_generated_schema_rejects_an_answer_with_an_undeclared_key() -> None:
    """An answer carrying a field this task type never declared is refused, not dropped."""
    schema = _batch_schema()
    with pytest.raises(ValidationError):
        schema.model_validate({**_answer(), "postings": [{"url": "https://evil.example/x"}]})


def test_the_generated_schema_rejects_any_cv_citation_when_no_entries_are_supplied() -> None:
    schema = response_schema((), conditions=_CONDITIONS)

    empty = schema.model_validate(_answer(master_cv_evidence_ids=[]))

    assert empty.model_dump(mode="json") == _answer(master_cv_evidence_ids=[])
    with pytest.raises(ValidationError):
        schema.model_validate(_answer(master_cv_evidence_ids=["cv-1"]))


def test_the_generated_schema_names_only_a_listed_exclusion_condition() -> None:
    conflict = _answer(
        recommendation="unlikely",
        hard_condition="conflicts",
        decisive_basis="hard_conflict",
        matched_exclusion=_CONDITIONS[0],
    )

    parsed = _batch_schema().model_validate(conflict)

    assert parsed.model_dump(mode="json") == conflict
    with pytest.raises(ValidationError):
        _batch_schema().model_validate(
            {
                **conflict,
                "recommendations": [
                    {**conflict["recommendations"][0], "matched_exclusion": "Must reside in the US"}
                ],
            }
        )


def test_the_generated_schema_names_no_condition_when_none_are_listed() -> None:
    schema = response_schema(_ENTRY_IDS, conditions=())

    assert schema.model_validate(_answer()).model_dump(mode="json") == _answer()
    with pytest.raises(ValidationError):
        schema.model_validate(_answer(matched_exclusion=_CONDITIONS[0]))


@pytest.mark.parametrize("field", sorted(_CONSTRAINED_FIELDS))
def test_the_generated_schema_permits_exactly_the_values_the_database_accepts(field: str) -> None:
    permitted = get_args(_item_model(_batch_schema()).model_fields[field].annotation)

    assert set(permitted) == _database_values(field)


@pytest.mark.parametrize("field", sorted(_CONSTRAINED_FIELDS))
def test_the_generated_schema_rejects_a_value_outside_a_constrained_field(field: str) -> None:
    with pytest.raises(ValidationError):
        _batch_schema().model_validate(_answer(**{field: "somewhat_promising"}))


def test_validation_reports_missing_result_when_the_provider_skipped_a_posting() -> None:
    result = RelevanceReviewService._validate_one("pd-1", [], _POSTING, _ALLOWED_EVIDENCE)

    assert result == RelevanceReviewItemFailure(
        "pd-1", "missing_result", "The provider returned no result for this posting."
    )


def test_validation_reports_duplicate_result_for_two_answers_about_one_posting() -> None:
    items = [_recommendation(), _recommendation(recommendation="unlikely")]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "duplicate_result"


def test_validation_reports_wrong_posting_span_for_a_span_owned_by_another_posting() -> None:
    items = [_recommendation(posting_evidence_span_ids=["pd-2:span-0001"])]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "wrong_posting_span"


def test_validation_reports_an_invented_span_on_the_posting_as_one_it_was_never_sent() -> None:
    items = [_recommendation(posting_evidence_span_ids=["pd-1:span-0099"])]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert result == RelevanceReviewItemFailure(
        "pd-1", "wrong_posting_span", "The result cited a span this posting was never sent."
    )


def test_validation_reports_invalid_cv_evidence_for_an_entry_outside_the_master_cv() -> None:
    items = [_recommendation(master_cv_evidence_ids=["cv-999"])]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "invalid_cv_evidence"


def test_validation_reports_invalid_cv_evidence_when_a_positive_verdict_cites_nothing() -> None:
    items = [_recommendation(master_cv_evidence_ids=[])]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert result == RelevanceReviewItemFailure(
        "pd-1",
        "invalid_cv_evidence",
        "A positive direct or transferable recommendation must cite master-CV evidence.",
    )


def test_an_invalid_answer_is_not_a_recommendation_for_persistence() -> None:
    response = RelevanceReviewBatchResponse(
        recommendations=[_recommendation(master_cv_evidence_ids=[])]
    )

    valid, invalid = RelevanceReviewService.validate_response(
        response,
        {"pd-1": _POSTING},
        {"pd-1"},
        _ALLOWED_EVIDENCE,
    )

    assert valid == []
    assert [(item.pending_discovery_id, item.failure_code) for item in invalid] == [
        ("pd-1", "invalid_cv_evidence")
    ]


def test_validation_accepts_a_negative_verdict_that_cites_no_master_cv_evidence() -> None:
    items = [
        _recommendation(
            recommendation="unlikely",
            core_work_alignment="absent",
            core_qualification_alignment="absent",
            hard_condition="passes",
            decisive_basis="core_gap",
            master_cv_evidence_ids=[],
        )
    ]

    result = RelevanceReviewService._validate_one("pd-1", items, _POSTING, _ALLOWED_EVIDENCE)

    assert isinstance(result, ValidatedRelevanceReview)
    assert result.master_cv_evidence_ids == []


_CLASS_FIELDS = (
    "core_work_alignment",
    "core_qualification_alignment",
    "hard_condition",
    "decisive_basis",
    "recommendation",
)


@pytest.mark.parametrize(
    "combination",
    [
        ("direct", "established", "conflicts", "hard_conflict", "review"),
        ("direct", "established", "passes", "hard_conflict", "unlikely"),
        ("absent", "absent", "conflicts", "core_gap", "unlikely"),
        ("absent", "absent", "passes", "core_gap", "review"),
        ("direct", "absent", "passes", "core_match", "review"),
        ("direct", "established", "passes", "core_match", "unlikely"),
        ("direct", "established", "unresolved", "core_match", "review"),
        ("transferable", "established", "passes", "transferable_match", "review"),
        ("absent", "established", "passes", "transferable_match", "investigate"),
        ("absent", "absent", "passes", "logistics_unknown", "investigate"),
        ("transferable", "established", "passes", "logistics_unknown", "review"),
        ("transferable", "established", "passes", "logistics_unknown", "investigate"),
        ("transferable", "partially_established", "unresolved", "logistics_unknown", "investigate"),
        ("direct", "partially_established", "passes", "logistics_unknown", "investigate"),
        ("direct", "unresolved", "unresolved", "logistics_unknown", "investigate"),
        ("unresolved", "unresolved", "passes", "insufficient_posting", "insufficient_evidence"),
        ("direct", "established", "passes", "insufficient_posting", "review"),
        ("direct", "absent", "passes", "core_gap", "unlikely"),
        ("direct", "established", "conflicts", "logistics_unknown", "investigate"),
        ("direct", "established", "passes", "logistics_unknown", "unlikely"),
        ("direct", "established", "passes", "logistics_unknown", "investigate"),
        ("direct", "established", "unresolved", "logistics_unknown", "review"),
    ],
)
def test_validation_rejects_a_class_combination_the_definitions_forbid(
    combination: tuple[str, str, str, str, str],
) -> None:
    overrides = dict(zip(_CLASS_FIELDS, combination, strict=True))

    result = RelevanceReviewService._validate_one(
        "pd-1", [_recommendation(**overrides)], _POSTING, _ALLOWED_EVIDENCE
    )

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "inconsistent_verdict"


@pytest.mark.parametrize("combination", sorted(ALLOWED_VERDICTS))
def test_validation_accepts_every_class_combination_the_definitions_allow(
    combination: tuple[str, str, str, str, str],
) -> None:
    overrides: dict[str, Any] = dict(zip(_CLASS_FIELDS, combination, strict=True))
    if overrides["decisive_basis"] == "hard_conflict":
        overrides["matched_exclusion"] = _CONDITIONS[0]

    result = RelevanceReviewService._validate_one(
        "pd-1", [_recommendation(**overrides)], _POSTING, _ALLOWED_EVIDENCE
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert result.recommendation == overrides["recommendation"]


@pytest.mark.parametrize(
    ("decisive_basis", "matched_exclusion"),
    [("hard_conflict", None), ("core_gap", _CONDITIONS[0]), ("logistics_unknown", _CONDITIONS[0])],
)
def test_validation_ties_a_named_exclusion_to_a_hard_conflict(
    decisive_basis: str, matched_exclusion: str | None
) -> None:
    """A hard conflict names the listed condition it rests on, and no other basis names one."""
    classes = {
        "hard_conflict": ("direct", "established", "conflicts", "unlikely"),
        "core_gap": ("absent", "absent", "passes", "unlikely"),
        "logistics_unknown": ("direct", "established", "passes", "review"),
    }[decisive_basis]
    work, qualification, hard, recommendation = classes

    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                core_work_alignment=work,
                core_qualification_alignment=qualification,
                hard_condition=hard,
                decisive_basis=decisive_basis,
                recommendation=recommendation,
                matched_exclusion=matched_exclusion,
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "inconsistent_verdict"


def test_an_accepted_hard_conflict_carries_the_condition_it_named() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                hard_condition="conflicts",
                decisive_basis="hard_conflict",
                recommendation="unlikely",
                matched_exclusion=_CONDITIONS[1],
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert result.matched_exclusion == _CONDITIONS[1]


@pytest.mark.parametrize(
    ("hard_condition", "recommendation"), [("passes", "review"), ("unresolved", "investigate")]
)
def test_aligned_work_with_open_logistics_is_investigated_only_while_a_condition_is_open(
    hard_condition: str, recommendation: str
) -> None:
    """Logistics the posting leaves open are investigated only when they engage a condition."""
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                hard_condition=hard_condition,
                decisive_basis="logistics_unknown",
                recommendation=recommendation,
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert (result.recommendation, result.hard_condition) == (recommendation, hard_condition)


def test_the_class_definitions_allow_exactly_fifty_seven_combinations() -> None:
    assert len(ALLOWED_VERDICTS) == 57


@pytest.mark.parametrize(
    ("core_work_alignment", "core_qualification_alignment"),
    [("transferable", "established"), ("direct", "partially_established")],
)
def test_an_unsettled_hard_condition_keeps_the_work_based_basis(
    core_work_alignment: str, core_qualification_alignment: str
) -> None:
    """An engaged but unsettled condition is an open question, not a posting that says nothing."""
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                core_work_alignment=core_work_alignment,
                core_qualification_alignment=core_qualification_alignment,
                hard_condition="unresolved",
                decisive_basis="transferable_match",
                recommendation="investigate",
                unresolved_questions=["Does the listed attendance schedule apply to this role?"],
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert (result.decisive_basis, result.recommendation) == ("transferable_match", "investigate")


def test_review_is_unavailable_while_a_hard_condition_is_unsettled() -> None:
    """Review means nothing material is open, and an engaged condition is material."""
    reviews = {
        (work, qualification, basis)
        for work, qualification, hard, basis, recommendation in ALLOWED_VERDICTS
        if recommendation == "review" and hard == "unresolved"
    }

    assert reviews == set()


def test_open_logistics_is_a_basis_only_for_direct_work_with_established_qualifications() -> None:
    """A core-work or qualification gap decides the basis, so logistics cannot carry it."""
    logistics = {
        (work, qualification)
        for work, qualification, _, basis, _ in ALLOWED_VERDICTS
        if basis == "logistics_unknown"
    }

    assert logistics == {("direct", "established")}


def test_direct_work_missing_a_stated_qualification_is_investigated_not_unlikely() -> None:
    """Unlikely needs a concrete contradiction or unrelated work, not one absent qualification."""
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                core_work_alignment="direct",
                core_qualification_alignment="absent",
                decisive_basis="core_gap",
                recommendation="investigate",
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert result.recommendation == "investigate"


def test_an_insufficient_posting_text_answer_is_accepted_without_a_verdict() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [_recommendation(outcome="insufficient_posting_text", master_cv_evidence_ids=[])],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert result.outcome == "insufficient_posting_text"
    assert [getattr(result, name) for name in _CLASS_FIELDS] == [None] * len(_CLASS_FIELDS)


_LONG_BODY = "\n\n".join(
    f"Section {index}: Freedonia Widgets runs payment tooling for regional merchants, "
    "and this part of the posting describes the platform team, its on-call duties, "
    "the services it owns, and the review practices it follows for every change."
    for index in range(1, 11)
)
_LONG_POSTING = RelevanceReviewPostingInput(
    body=_LONG_BODY,
    fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
)


def test_an_insufficient_posting_text_answer_about_a_long_body_is_rejected() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                outcome="insufficient_posting_text",
                posting_evidence_span_ids=["pd-1:span-0009"],
                master_cv_evidence_ids=[],
            )
        ],
        _LONG_POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "inconsistent_verdict"


def test_validation_rejects_a_verdict_citing_only_the_leading_excerpt_of_a_long_body() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [_recommendation(posting_evidence_span_ids=["pd-1:span-0001", "pd-1:span-0002"])],
        _LONG_POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, RelevanceReviewItemFailure)
    assert result.failure_code == "leading_excerpt_only"


def test_validation_accepts_a_verdict_citing_a_span_past_the_leading_excerpt() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [_recommendation(posting_evidence_span_ids=["pd-1:span-0001", "pd-1:span-0009"])],
        _LONG_POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)
    assert [citation.span_id for citation in result.posting_citations] == [
        "pd-1:span-0001",
        "pd-1:span-0009",
    ]


def test_validation_resolves_a_cited_span_to_its_exact_source_text() -> None:
    result = RelevanceReviewService._validate_one(
        "pd-1", [_recommendation()], _POSTING, _ALLOWED_EVIDENCE
    )

    assert isinstance(result, ValidatedRelevanceReview)
    (citation,) = result.posting_citations
    assert citation.span_id == "pd-1:span-0001"
    assert citation.text == "Freedonia Widgets seeks a staff engineer."
    assert _BODY[citation.char_start : citation.char_end] == citation.text


def test_transferable_match_accepts_direct_work_with_a_partial_qualification_gap() -> None:
    """A non-defining qualification gap may decide an otherwise direct match."""
    result = RelevanceReviewService._validate_one(
        "pd-1",
        [
            _recommendation(
                recommendation="investigate",
                core_qualification_alignment="partially_established",
                decisive_basis="transferable_match",
            )
        ],
        _POSTING,
        _ALLOWED_EVIDENCE,
    )

    assert isinstance(result, ValidatedRelevanceReview)


_INTRO = (
    "Globex builds streaming pipelines that keep commerce ledgers accurate across "
    "regions. The team owns ingestion, schema evolution, and the quality checks that "
    "keep both trustworthy for downstream finance reporting."
)
_REQUIREMENTS = (
    "Five years of experience with distributed systems and a strong background in SQL "
    "and Python are required, along with comfort operating production services."
)
_TRAVEL_BODY = (
    f"Field Data Engineer\n\n{_INTRO}\n\nThis role requires up to 25% travel.\n\n{_REQUIREMENTS}"
)
_READABLE_BODY = f"Data Engineer\n\n{_INTRO}\n\n{_REQUIREMENTS}"


def _out_of_credit(schema: type[BaseModel]) -> dict[str, Any]:
    raise QuotaError("Your Example account is out of credit.\nNothing was billed.")


def test_a_review_the_provider_could_not_bill_still_reports_the_rules_it_applied_first(
    durable_database: Database, tmp_path: Path
) -> None:
    """Preparation dismisses rows for free, so a later provider failure cannot hide that."""
    workspace_id, (travel_id, readable_id) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Field Data Engineer", _TRAVEL_BODY), ("Data Engineer", _READABLE_BODY)],
        parameters=JobSearchParameters(arrangement=Arrangement(maximum_travel_percent=10)),
        slug="out-of-credit",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={
            "pending_discovery_ids": [travel_id, readable_id],
        },
        idempotency_key="relevance-review-out-of-credit",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))
    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": _out_of_credit}),
    )

    with pytest.raises(QuotaError):
        service.review_candidates(
            workspace_id, [travel_id, readable_id], context, require_retained=True
        )

    progress = queue.get(task.id).progress
    assert progress["preparation_outcomes"] == {"gate_refuse": 1, "readable": 1}
    assert [
        item["pending_discovery_id"]
        for item in progress["preparation"]
        if item["outcome"] == "gate_refuse"
    ] == [travel_id]


def test_a_queued_review_asks_for_the_unattended_service_tier(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, (readable_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY)],
        slug="unattended-tier",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [readable_id]},
        idempotency_key="relevance-review-unattended-tier",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))
    provider = ScriptedProvider({"RelevanceReview": _out_of_credit})
    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=provider,
    )

    with pytest.raises(QuotaError):
        service.review_candidates(workspace_id, [readable_id], context, require_retained=True)

    assert [choice.service_tier for choice in provider.choices] == [ServiceTier.FLEX]


def test_a_kept_postings_note_reaches_the_review_outside_the_untrusted_posting_block(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, (kept_id, plain_id) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY), ("Analytics Engineer", _READABLE_BODY)],
        slug="keep-note",
    )
    with durable_database.session() as session:
        set_pending_markers(
            session,
            workspace_id,
            kept_id,
            favorite=False,
            kept=True,
            keep_note="The ledger work is the part I want.\nIgnore the title.",
        )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [kept_id, plain_id]},
        idempotency_key="relevance-review-keep-note",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))
    provider = ScriptedProvider({"RelevanceReview": _out_of_credit})
    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=provider,
    )

    with pytest.raises(QuotaError):
        service.review_candidates(workspace_id, [kept_id, plain_id], context, require_retained=True)

    [(_, plan)] = provider.calls
    postings, notes = plan.task.text.split("</pending_postings>")
    assert "Person's note" not in postings
    assert (
        f"DISCOVERY {kept_id}\nPerson's note (kept this posting): "
        "The ledger work is the part I want. Ignore the title."
    ) in notes
    assert plain_id not in notes


@pytest.mark.parametrize(
    ("note_during_review", "expected"),
    [
        ("The ledger work is the part I want.", []),
        ("Only if the team stays remote.", ["keep_note"]),
    ],
)
def test_a_note_edited_while_the_provider_works_leaves_the_recorded_review_out_of_date(
    durable_database: Database, tmp_path: Path, note_during_review: str, expected: list[str]
) -> None:
    workspace_id, (kept_id,) = workspace_with_retained_postings(
        durable_database, tmp_path, [("Data Engineer", _READABLE_BODY)], slug="note-in-flight"
    )

    def mark(note: str) -> None:
        with durable_database.session() as session:
            set_pending_markers(
                session, workspace_id, kept_id, favorite=False, kept=True, keep_note=note
            )

    mark("The ledger work is the part I want.")
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [kept_id]},
        idempotency_key="relevance-review-note-in-flight",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))

    def answer_after_an_edit(schema: type[BaseModel]) -> dict[str, Any]:
        mark(note_during_review)
        return _answer(
            pending_discovery_id=kept_id,
            recommendation="unlikely",
            decisive_basis="core_gap",
            core_work_alignment="absent",
            core_qualification_alignment="absent",
            posting_evidence_span_ids=[f"{kept_id}:span-0001"],
            master_cv_evidence_ids=[],
        )

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": answer_after_an_edit}),
    )

    outcome = service.review_candidates(workspace_id, [kept_id], context, require_retained=True)

    assert outcome.validation_failures == ()
    with durable_database.session() as session:
        current = RelevanceReviewQueries(durable_database).current_for_pending_ids(
            session, workspace_id, [kept_id]
        )[kept_id]
    assert current.out_of_date_because == expected


def test_two_tasks_over_the_same_cv_and_parameters_share_one_cached_prefix(
    durable_database: Database, tmp_path: Path
) -> None:
    """The schema and evidence sit ahead of the cache breakpoint, so neither may name a posting."""
    stub = "Data Engineer\n\nApply today."
    workspace_id, pending_ids = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", stub), ("Analytics Engineer", stub)],
        parameters=JobSearchParameters(
            excluded_conditions=[(_CONDITIONS[0], "holds no clearance")]
        ),
        slug="shared-prefix",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    schemas: list[str] = []
    reviewing: list[str] = []

    def answer(schema: type[BaseModel]) -> dict[str, Any]:
        schemas.append(schema_digest(schema.model_json_schema()))
        return _answer(
            pending_discovery_id=reviewing[-1],
            outcome="insufficient_posting_text",
            recommendation="insufficient_evidence",
            core_work_alignment="unresolved",
            core_qualification_alignment="unresolved",
            hard_condition="unresolved",
            decisive_basis="insufficient_posting",
            posting_evidence_span_ids=[f"{reviewing[-1]}:span-0001"],
            master_cv_evidence_ids=[],
        )

    provider = ScriptedProvider({"RelevanceReview": answer})
    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=provider,
    )
    for pending_id in pending_ids:
        reviewing.append(pending_id)
        task = queue.enqueue(
            workspace_id=workspace_id,
            task_type="discovery.relevance_review",
            payload={"pending_discovery_ids": [pending_id]},
            idempotency_key=f"relevance-review-shared-prefix-{pending_id}",
        )
        queue.claim_next(worker_id="w1")
        context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))
        service.review_candidates(workspace_id, [pending_id], context, require_retained=True)

    first, second = (plan for _, plan in provider.calls)
    assert first.task.text != second.task.text
    assert first.cached_prefix == second.cached_prefix
    assert len(schemas) == 2
    assert schemas[0] == schemas[1]


def test_a_changed_cv_inventory_or_listed_condition_changes_the_schema() -> None:
    """CV entry ids and listed conditions stay closed, so either one moves the schema digest."""

    def digest(entries: tuple[str, ...], conditions: tuple[str, ...]) -> str:
        return schema_digest(response_schema(entries, conditions=conditions).model_json_schema())

    shipped = digest(_ENTRY_IDS, _CONDITIONS)

    assert digest(_ENTRY_IDS, _CONDITIONS) == shipped
    assert digest(("cv-1", "cv-3"), _CONDITIONS) != shipped
    assert digest(_ENTRY_IDS, _CONDITIONS[:1]) != shipped


def test_an_insufficient_posting_text_answer_records_no_verdict_and_no_class(
    durable_database: Database, tmp_path: Path
) -> None:
    workspace_id, (stub_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", "Data Engineer\n\nApply today.")],
        slug="stub",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [stub_id]},
        idempotency_key="relevance-review-stub",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))

    def stub_answer(schema: type[BaseModel]) -> dict[str, Any]:
        return {
            "recommendations": [
                _recommendation_payload(
                    pending_discovery_id=stub_id,
                    outcome="insufficient_posting_text",
                    recommendation="insufficient_evidence",
                    core_work_alignment="unresolved",
                    core_qualification_alignment="unresolved",
                    hard_condition="unresolved",
                    decisive_basis="insufficient_posting",
                    posting_evidence_span_ids=[f"{stub_id}:span-0001"],
                    master_cv_evidence_ids=[],
                )
            ]
        }

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": stub_answer}),
    )

    outcome = service.review_candidates(workspace_id, [stub_id], context, require_retained=True)

    assert outcome.validation_failures == ()
    with durable_database.session() as session:
        (row,) = session.scalars(select(RelevanceReview)).all()
        current = RelevanceReviewQueries(durable_database).current_for_pending_ids(
            session, workspace_id, [stub_id]
        )[stub_id]
    assert [getattr(row, name) for name in _CLASS_FIELDS] == [None] * len(_CLASS_FIELDS)
    assert current.outcome == "insufficient_posting_text"
    assert current.recommendation is None
    assert current.decisive_basis is None


def test_a_hard_conflict_may_name_a_condition_the_search_parameters_list(
    durable_database: Database, tmp_path: Path
) -> None:
    """The batch schema closes over the saved conditions, so the named one is stored."""
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY)],
        parameters=JobSearchParameters(
            excluded_conditions=[(_CONDITIONS[0], "holds no clearance")]
        ),
        slug="named-exclusion",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="relevance-review-named-exclusion",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))

    def conflict_answer(schema: type[BaseModel]) -> dict[str, Any]:
        return _answer(
            pending_discovery_id=pending_id,
            recommendation="unlikely",
            hard_condition="conflicts",
            decisive_basis="hard_conflict",
            matched_exclusion=_CONDITIONS[0],
            posting_evidence_span_ids=[f"{pending_id}:span-0001"],
            master_cv_evidence_ids=[],
            core_work_alignment="absent",
            core_qualification_alignment="absent",
        )

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": conflict_answer}),
    )

    outcome = service.review_candidates(workspace_id, [pending_id], context, require_retained=True)

    assert outcome.validation_failures == ()
    with durable_database.session() as session:
        current = RelevanceReviewQueries(durable_database).current_for_pending_ids(
            session, workspace_id, [pending_id]
        )[pending_id]
    assert current.matched_exclusion == _CONDITIONS[0]


def test_a_rejected_answer_records_no_review_and_marks_the_row_it_paid_for(
    durable_database: Database, tmp_path: Path
) -> None:
    """A billed answer local validation refuses leaves a mark, not a silent gap."""
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY)],
        slug="rejected-answer",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="relevance-review-rejected-answer",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))

    def impossible_answer(schema: type[BaseModel]) -> dict[str, Any]:
        return {
            "recommendations": [
                _recommendation_payload(
                    pending_discovery_id=pending_id,
                    recommendation="review",
                    core_work_alignment="absent",
                    core_qualification_alignment="absent",
                    hard_condition="passes",
                    decisive_basis="core_gap",
                    posting_evidence_span_ids=[f"{pending_id}:span-0001"],
                    master_cv_evidence_ids=[],
                )
            ]
        }

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": impossible_answer}),
    )

    outcome = service.review_candidates(workspace_id, [pending_id], context, require_retained=True)

    assert [item.failure_code for item in outcome.validation_failures] == ["inconsistent_verdict"]
    with durable_database.session() as session:
        assert session.scalars(select(RelevanceReview)).all() == []
    row = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)
    assert row.relevance_review is None
    assert row.relevance_review_rejection is not None
    assert row.relevance_review_rejection.failure_code == "inconsistent_verdict"
    assert row.relevance_review_rejection.task_id == task.id


def test_an_answer_for_a_posting_the_batch_never_sent_is_recorded_on_the_task_outcome(
    durable_database: Database, tmp_path: Path
) -> None:
    """The open posting-id schema lets a model name an unsent id; the run says so."""
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY)],
        slug="unsent-posting-id",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="relevance-review-unsent-posting-id",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))

    def answer_for_another_posting(schema: type[BaseModel]) -> dict[str, Any]:
        return {
            "recommendations": [
                _recommendation_payload(
                    pending_discovery_id="pd-never-sent",
                    recommendation="unlikely",
                    core_work_alignment="absent",
                    core_qualification_alignment="absent",
                    decisive_basis="core_gap",
                    posting_evidence_span_ids=["pd-never-sent:span-0001"],
                    master_cv_evidence_ids=[],
                )
            ]
        }

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": answer_for_another_posting}),
    )

    outcome = service.review_candidates(workspace_id, [pending_id], context, require_retained=True)

    result = outcome.as_json()
    assert result["unsent_postings_answered"] == ["pd-never-sent"]
    assert [
        (item["pending_discovery_id"], item["failure_code"])
        for item in result["validation_failures"]
    ] == [(pending_id, "missing_result")]


def test_a_run_whose_answers_named_only_sent_postings_reports_no_unsent_postings() -> None:
    assert "unsent_postings_answered" not in RelevanceReviewOutcome(reviewed=3).as_json()


def test_an_accepted_answer_clears_the_mark_the_previous_paid_review_left(
    durable_database: Database, tmp_path: Path
) -> None:
    """The shipped pipeline, not only the service, retires a mark once a verdict lands."""
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        durable_database,
        tmp_path,
        [("Data Engineer", _READABLE_BODY)],
        slug="accepted-after-rejection",
    )
    queue = TaskQueueService(durable_database)
    queue.renew_worker_presence(worker_id="w1", ttl_seconds=3600)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="relevance-review-accepted-after-rejection",
    )
    queue.claim_next(worker_id="w1")
    context = _WorkerTaskContext(queue, task.id, "w1", _LeaseKeeper(queue, task.id, "w1", 20.0))
    RelevanceReviewRejectionService(durable_database).record(
        workspace_id=workspace_id,
        task_id=task.id,
        rejected=[RelevanceReviewItemFailure(pending_id, "missing_result", "Nothing came back.")],
    )

    def judged_answer(schema: type[BaseModel]) -> dict[str, Any]:
        return {
            "recommendations": [
                _recommendation_payload(
                    pending_discovery_id=pending_id,
                    posting_evidence_span_ids=[f"{pending_id}:span-0001"],
                    master_cv_evidence_ids=[],
                    core_work_alignment="absent",
                    core_qualification_alignment="absent",
                    hard_condition="passes",
                    decisive_basis="core_gap",
                    recommendation="unlikely",
                )
            ]
        }

    service = RelevanceReviewService(
        durable_database,
        storage=runtime_storage(durable_database, tmp_path),
        fetcher=UnexpectedPostingFetcher(),
        provider=ScriptedProvider({"RelevanceReview": judged_answer}),
    )

    outcome = service.review_candidates(workspace_id, [pending_id], context, require_retained=True)

    assert outcome.validation_failures == ()
    with durable_database.session() as session:
        assert len(session.scalars(select(RelevanceReview)).all()) == 1
    row = SourceScanQueries(durable_database).get_pending_discovery(workspace_id, pending_id)
    assert row.relevance_review_rejection is None


def test_validation_fails_an_expected_posting_the_answer_never_mentioned() -> None:
    response = RelevanceReviewBatchResponse(recommendations=[_recommendation()])

    valid, invalid = RelevanceReviewService.validate_response(
        response,
        {"pd-1": _POSTING, "pd-2": _POSTING},
        {"pd-1", "pd-2"},
        _ALLOWED_EVIDENCE,
    )

    assert [item.pending_discovery_id for item in valid] == ["pd-1"]
    assert [(item.pending_discovery_id, item.failure_code) for item in invalid] == [
        ("pd-2", "missing_result")
    ]
