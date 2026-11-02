"""Relevance reviews append as history, and the reader serves the latest as current."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from tailorcv.application.discovery.pending import set_pending_markers
from tailorcv.application.discovery.relevance_review import current_review_contract
from tailorcv.application.discovery.relevance_review_queries import RelevanceReviewQueries
from tailorcv.application.discovery.relevance_review_records import (
    RelevanceReviewPostingInput,
    RelevanceReviewRecorder,
    ValidatedRelevanceReview,
)
from tailorcv.application.job_search.models import (
    RelevanceReviewAnswerOutcome,
    RelevanceReviewCitation,
    RelevanceReviewStaleInput,
)
from tailorcv.application.master_cv.service import MasterCvService
from tailorcv.application.master_cv.versioning import current_version_id
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces.parameters import (
    SearchParametersService,
    parameter_snapshot_in,
)
from tailorcv.domain.search_parameters import Arrangement, JobSearchParameters
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import PendingDiscovery, RelevanceReview
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import workspace_with_retained_postings

_BODY = "Data Engineer\n\nGlobex builds streaming pipelines for commerce ledgers."


@dataclass(frozen=True)
class _Reviewed:
    workspace_id: str
    pending_id: str
    task_id: str
    master_cv_version_id: str
    search_parameter_set_id: str


def _reviewable(database: Database, tmp_path: Path) -> _Reviewed:
    workspace_id, (pending_id,) = workspace_with_retained_postings(
        database, tmp_path, [("Data Engineer", _BODY)], slug="history"
    )
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [pending_id]},
        idempotency_key="relevance-review-history",
    )
    with database.session() as session:
        version_id = current_version_id(session, workspace_id)
        parameter_set_id = parameter_snapshot_in(session, workspace_id).parameter_set_id
    assert version_id is not None and parameter_set_id is not None
    return _Reviewed(workspace_id, pending_id, task.id, version_id, parameter_set_id)


def _append(
    database: Database,
    reviewed: _Reviewed,
    summary: str,
    outcome: RelevanceReviewAnswerOutcome = "judged",
    matched_exclusion: str | None = None,
    note: str | None = None,
) -> None:
    judged = outcome == "judged"
    with database.session() as session:
        pending = session.get_one(PendingDiscovery, reviewed.pending_id)
        session.expunge(pending)
    RelevanceReviewRecorder(database).append(
        workspace_id=reviewed.workspace_id,
        task_id=reviewed.task_id,
        master_cv_version_id=reviewed.master_cv_version_id,
        search_parameter_set_id=reviewed.search_parameter_set_id,
        search_context="Synthetic search context.",
        contract=current_review_contract(),
        pending=[pending],
        inputs={
            reviewed.pending_id: RelevanceReviewPostingInput(
                body=_BODY,
                fetched_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
        },
        recommendations=[
            ValidatedRelevanceReview(
                pending_discovery_id=reviewed.pending_id,
                outcome=outcome,
                recommendation="unlikely" if judged else None,
                summary=summary,
                core_work_alignment="absent" if judged else None,
                core_qualification_alignment="absent" if judged else None,
                hard_condition=("conflicts" if matched_exclusion else "passes") if judged else None,
                decisive_basis=("hard_conflict" if matched_exclusion else "core_gap")
                if judged
                else None,
                posting_citations=[
                    RelevanceReviewCitation(
                        span_id=f"{reviewed.pending_id}:span-0001",
                        text="Data Engineer",
                        char_start=0,
                        char_end=13,
                    )
                ]
                if judged
                else [],
                master_cv_evidence_ids=[],
                unresolved_questions=[],
                matched_exclusion=matched_exclusion,
            )
        ],
        notes={reviewed.pending_id: note} if note else {},
    )


def _current_summary_and_staleness(
    database: Database, reviewed: _Reviewed
) -> tuple[str, list[RelevanceReviewStaleInput]]:
    with database.session() as session:
        current = RelevanceReviewQueries(database).current_for_pending_ids(
            session, reviewed.workspace_id, [reviewed.pending_id]
        )[reviewed.pending_id]
    return current.summary, current.out_of_date_because


def _save_newer_cv_and_parameters(database: Database, tmp_path: Path, reviewed: _Reviewed) -> None:
    master = MasterCvService(database, runtime_storage(database, tmp_path))
    content = master.read_version(reviewed.workspace_id).content
    master.save_edit(reviewed.workspace_id, content + "\n- An edited line of evidence.\n")
    SearchParametersService(database).save(
        reviewed.workspace_id,
        JobSearchParameters(arrangement=Arrangement(maximum_travel_percent=10)),
    )


def _set_earlier_prompt(database: Database) -> None:
    with database.session() as session:
        latest = session.scalars(
            select(RelevanceReview).order_by(RelevanceReview.reviewed_at.desc()).limit(1)
        ).one()
        session.execute(
            update(RelevanceReview)
            .where(RelevanceReview.id == latest.id)
            .values(instructions_digest="0" * 32)
        )


def test_a_re_review_keeps_the_earlier_review_and_the_reader_returns_the_new_one(
    durable_database: Database, tmp_path: Path
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)

    _append(durable_database, reviewed, "The first review.")
    _append(durable_database, reviewed, "The second review.")

    with durable_database.session() as session:
        stored = session.scalar(select(func.count()).select_from(RelevanceReview))
    assert stored == 2
    assert _current_summary_and_staleness(durable_database, reviewed) == (
        "The second review.",
        [],
    )


def test_the_reader_serves_the_exclusion_condition_a_hard_conflict_named(
    durable_database: Database, tmp_path: Path
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)

    _append(
        durable_database,
        reviewed,
        "The posting requires an active security clearance.",
        matched_exclusion="Active security clearance required",
    )

    with durable_database.session() as session:
        current = RelevanceReviewQueries(durable_database).current_for_pending_ids(
            session, reviewed.workspace_id, [reviewed.pending_id]
        )[reviewed.pending_id]
    assert (current.decisive_basis, current.matched_exclusion) == (
        "hard_conflict",
        "Active security clearance required",
    )


def test_a_pipeline_review_records_the_worker_and_not_a_person_as_its_actor(
    durable_database: Database, tmp_path: Path
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)

    _append(durable_database, reviewed, "The model judged this posting.")

    with durable_database.session() as session:
        actors = set(session.scalars(select(RelevanceReview.created_by)))
    assert actors == {"worker"}


def test_a_review_under_an_earlier_prompt_or_schema_is_out_of_date(
    durable_database: Database, tmp_path: Path
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)

    for digests in (
        {"instructions_digest": "0" * 32},
        {"schema_digest": "0" * 32},
        {"instructions_digest": None, "schema_digest": None},
    ):
        _append(durable_database, reviewed, "Reviewed under an earlier prompt.")
        with durable_database.session() as session:
            latest = session.scalars(
                select(RelevanceReview).order_by(RelevanceReview.reviewed_at.desc()).limit(1)
            ).one()
            session.execute(
                update(RelevanceReview).where(RelevanceReview.id == latest.id).values(**digests)
            )

        assert _current_summary_and_staleness(durable_database, reviewed) == (
            "Reviewed under an earlier prompt.",
            ["review_instructions"],
        )


@pytest.mark.parametrize(
    ("outcome", "earlier_prompt", "expected"),
    [
        ("judged", False, ["master_cv", "search_parameters"]),
        ("judged", True, ["master_cv", "search_parameters", "review_instructions"]),
        ("insufficient_posting_text", False, []),
        ("insufficient_posting_text", True, ["review_instructions"]),
    ],
)
def test_a_newer_cv_or_parameter_set_dates_only_a_judged_review(
    durable_database: Database,
    tmp_path: Path,
    outcome: RelevanceReviewAnswerOutcome,
    earlier_prompt: bool,
    expected: list[RelevanceReviewStaleInput],
) -> None:
    """Preparation re-sends a too-short answer only for a new body or prompt."""
    reviewed = _reviewable(durable_database, tmp_path)
    _append(durable_database, reviewed, "Reviewed before the edits.", outcome)
    if earlier_prompt:
        _set_earlier_prompt(durable_database)

    _save_newer_cv_and_parameters(durable_database, tmp_path, reviewed)

    assert _current_summary_and_staleness(durable_database, reviewed) == (
        "Reviewed before the edits.",
        expected,
    )


_SENT_NOTE = "The ledger work is the part I want."


@pytest.mark.parametrize(
    ("sent", "current", "expected"),
    [
        (None, None, []),
        (None, _SENT_NOTE, ["keep_note"]),
        (_SENT_NOTE, _SENT_NOTE, []),
        (_SENT_NOTE, "Only if the team is remote.", ["keep_note"]),
        (_SENT_NOTE, None, ["keep_note"]),
    ],
)
def test_a_judged_review_is_current_only_while_the_family_note_is_the_one_it_was_sent(
    durable_database: Database,
    tmp_path: Path,
    sent: str | None,
    current: str | None,
    expected: list[RelevanceReviewStaleInput],
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)

    def keep(note: str | None) -> None:
        with durable_database.session() as session:
            set_pending_markers(
                session,
                reviewed.workspace_id,
                reviewed.pending_id,
                favorite=False,
                kept=note is not None,
                keep_note=note,
            )

    keep(sent)
    _append(durable_database, reviewed, "Reviewed with the note it was sent.", note=sent)

    keep(current)

    assert _current_summary_and_staleness(durable_database, reviewed) == (
        "Reviewed with the note it was sent.",
        expected,
    )


def test_a_too_short_answer_ignores_the_keep_note(
    durable_database: Database, tmp_path: Path
) -> None:
    reviewed = _reviewable(durable_database, tmp_path)
    _append(durable_database, reviewed, "Too short to judge.", "insufficient_posting_text")

    with durable_database.session() as session:
        set_pending_markers(
            session,
            reviewed.workspace_id,
            reviewed.pending_id,
            favorite=False,
            kept=True,
            keep_note=_SENT_NOTE,
        )

    assert _current_summary_and_staleness(durable_database, reviewed) == ("Too short to judge.", [])
