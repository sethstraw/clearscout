"""Appending application events: replay, correction, reversal, and refusal."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from tailorcv.application import job_search
from tailorcv.application.contracts import ApplicationEventInput
from tailorcv.application.failures import ConflictError, ValidationError
from tailorcv.application.job_search.models import JobListQuery
from tailorcv.domain import ApplicationEventType, ApplicationStatus
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import Application
from tests.support.scenarios import NOW, workspace_with_committed_evaluation


def _applied() -> ApplicationEventInput:
    """The submission event the replay and correction cases build on."""
    return ApplicationEventInput(
        status=ApplicationStatus.APPLIED,
        occurred_at=NOW + timedelta(hours=1),
        notes="Submitted after user confirmation.",
    )


def _correction(corrects_event_id: str) -> ApplicationEventInput:
    """A package_ready correction of the named event."""
    return ApplicationEventInput(
        status=ApplicationStatus.PACKAGE_READY,
        event_type=ApplicationEventType.CORRECTION,
        occurred_at=NOW + timedelta(hours=2),
        notes="Authoritative tracker correction.",
        corrects_event_id=corrects_event_id,
    )


def test_a_reused_idempotency_key_carrying_a_different_event_conflicts(
    durable_database: Database, tmp_path: Path
) -> None:
    job = workspace_with_committed_evaluation(durable_database, tmp_path, "reused-key").detail
    job_search.append_application_event(
        durable_database, job.job_posting_id, _applied(), idempotency_key="submit-1"
    )

    with pytest.raises(ConflictError, match="different event"):
        job_search.append_application_event(
            durable_database,
            job.job_posting_id,
            _applied().model_copy(update={"notes": "Different"}),
            idempotency_key="submit-1",
        )


def test_a_correction_moves_the_status_and_joins_the_history(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "correct")
    workspace_id, job = scenario.workspace_id, scenario.detail
    written = job_search.append_application_event(
        durable_database, job.job_posting_id, _applied(), idempotency_key="submit-1"
    )
    job_search.append_application_event(
        durable_database, job.job_posting_id, _correction(written.id), idempotency_key="correct-1"
    )

    detail = job_search.job_detail(durable_database, job.job_posting_id, workspace_id)

    assert detail.application_status == "package_ready"
    # History begins at the first real move. There is no seeded not_applied
    # event, because nothing happened when the posting was evaluated.
    assert [event.status for event in detail.application_events] == [
        "applied",
        "package_ready",
    ]


def test_the_ledger_filters_jobs_by_their_corrected_application_status(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "status-filter")
    workspace_id, job = scenario.workspace_id, scenario.detail
    written = job_search.append_application_event(
        durable_database, job.job_posting_id, _applied(), idempotency_key="submit-1"
    )
    job_search.append_application_event(
        durable_database, job.job_posting_id, _correction(written.id), idempotency_key="correct-1"
    )

    filtered, total = job_search.list_jobs(
        durable_database,
        JobListQuery(
            workspace_id=workspace_id,
            application_statuses=("package_ready",),
        ),
    )

    assert total == 1 and filtered[0].job_posting_id == job.job_posting_id


def test_a_correction_must_identify_the_event_it_corrects() -> None:
    with pytest.raises(PydanticValidationError, match="must identify"):
        ApplicationEventInput(
            status=ApplicationStatus.NOT_APPLIED,
            event_type=ApplicationEventType.CORRECTION,
            occurred_at=NOW,
        )


def test_an_appended_event_requires_an_idempotency_key(
    durable_database: Database, tmp_path: Path
) -> None:
    job = workspace_with_committed_evaluation(durable_database, tmp_path, "blank-key").detail

    with pytest.raises(ValidationError, match="idempotency key"):
        job_search.append_application_event(
            durable_database, job.job_posting_id, _applied(), idempotency_key=" "
        )


def test_a_correction_reaches_only_this_applications_own_history(
    durable_database: Database, tmp_path: Path
) -> None:
    job = workspace_with_committed_evaluation(
        durable_database, tmp_path, "foreign-correction"
    ).detail
    written = job_search.append_application_event(
        durable_database, job.job_posting_id, _applied(), idempotency_key="submit-1"
    )

    with pytest.raises(ValidationError, match="only this application's history"):
        job_search.append_application_event(
            durable_database,
            job.job_posting_id,
            _correction(written.id).model_copy(update={"corrects_event_id": "missing"}),
            idempotency_key="bad-reference",
        )


def test_a_committed_evaluations_rationale_cannot_be_rewritten(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "immutable")
    workspace_id, job = scenario.workspace_id, scenario.detail

    with (
        pytest.raises(IntegrityError, match="immutable"),
        durable_database.session() as session,
    ):
        session.execute(
            text("UPDATE job_evaluations SET rationale = 'rewritten' WHERE id = :id"),
            {"id": job.evaluation_id},
        )

    assert (
        job_search.job_detail(durable_database, job.job_posting_id, workspace_id).rationale
        == "Strong product and data-platform evidence."
    )


@pytest.mark.parametrize(
    "progressed_status",
    [
        ApplicationStatus.APPLIED,
        ApplicationStatus.INTERVIEWING,
        ApplicationStatus.OFFER,
        ApplicationStatus.REJECTED,
        ApplicationStatus.WITHDRAWN,
        ApplicationStatus.DECLINED,
        ApplicationStatus.CLOSED_BEFORE_APPLICATION,
    ],
)
def test_plain_package_ready_is_rejected_after_every_progressed_status(
    durable_database: Database,
    tmp_path: Path,
    progressed_status: ApplicationStatus,
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "regress")
    workspace_id, job = scenario.workspace_id, scenario.detail
    job_id = job.job_posting_id

    job_search.append_application_event(
        durable_database,
        job_id,
        ApplicationEventInput(
            status=progressed_status,
            occurred_at=NOW + timedelta(hours=1),
            notes="The application progressed.",
        ),
        idempotency_key="progressed-1",
    )
    with pytest.raises(ConflictError, match="cannot be recorded after"):
        job_search.append_application_event(
            durable_database,
            job_id,
            ApplicationEventInput(
                status=ApplicationStatus.PACKAGE_READY,
                occurred_at=NOW + timedelta(hours=1, seconds=4),
                notes="Package assembled after progression.",
            ),
            idempotency_key="package-after-progression",
        )

    detail = job_search.job_detail(durable_database, job_id, workspace_id)
    assert detail.application_status == progressed_status.value
    assert [event.status for event in detail.application_events] == [progressed_status.value]

    filtered, total = job_search.list_jobs(
        durable_database,
        JobListQuery(
            workspace_id=workspace_id,
            application_statuses=(progressed_status.value,),
        ),
    )
    assert total == 1 and filtered[0].job_posting_id == job_id


def test_package_ready_replay_correction_and_reversal_remain_allowed_after_progression(
    durable_database: Database, tmp_path: Path
) -> None:
    scenario = workspace_with_committed_evaluation(durable_database, tmp_path, "amend")
    workspace_id, job = scenario.workspace_id, scenario.detail
    ready = ApplicationEventInput(
        status=ApplicationStatus.PACKAGE_READY,
        occurred_at=NOW,
        notes="Ready before submission.",
    )
    first = job_search.append_application_event(
        durable_database, job.job_posting_id, ready, idempotency_key="ready-first"
    )
    applied = job_search.append_application_event(
        durable_database,
        job.job_posting_id,
        ApplicationEventInput(
            status=ApplicationStatus.APPLIED,
            occurred_at=NOW + timedelta(hours=1),
        ),
        idempotency_key="applied-second",
    )

    replay = job_search.append_application_event(
        durable_database, job.job_posting_id, ready, idempotency_key="ready-first"
    )
    assert replay.id == first.id

    correction = job_search.append_application_event(
        durable_database,
        job.job_posting_id,
        ApplicationEventInput(
            status=ApplicationStatus.PACKAGE_READY,
            event_type=ApplicationEventType.CORRECTION,
            occurred_at=NOW + timedelta(hours=2),
            corrects_event_id=applied.id,
        ),
        idempotency_key="ready-correction",
    )
    reversal = job_search.append_application_event(
        durable_database,
        job.job_posting_id,
        ApplicationEventInput(
            status=ApplicationStatus.PACKAGE_READY,
            event_type=ApplicationEventType.REVERSAL,
            occurred_at=NOW + timedelta(hours=3),
            reverses_event_id=correction.id,
        ),
        idempotency_key="ready-reversal",
    )

    assert correction.status == ApplicationStatus.PACKAGE_READY
    assert reversal.status == ApplicationStatus.PACKAGE_READY
    listed, total = job_search.list_jobs(durable_database, JobListQuery(workspace_id=workspace_id))
    assert total == 1
    assert listed[0].application_event_count == 4


def test_an_application_begins_at_the_first_real_action(
    durable_database: Database, tmp_path: Path
) -> None:
    """Evaluating a posting does not create an application.

    The funnel counts what is being pursued, so an application row appears only
    at the first real move, not for every job that was read.
    """
    scenario = workspace_with_committed_evaluation(
        durable_database, tmp_path, "funnel", correlation_id="corr-1"
    )
    workspace_id, job_id = scenario.workspace_id, scenario.job_posting_id

    with durable_database.session() as session:
        assert session.scalars(select(Application)).all() == []

    # Not applied is still the reported status, because that is the truth about
    # a job nobody has acted on, not a placeholder standing in for a row.
    assert (
        job_search.job_detail(durable_database, job_id, workspace_id).application_status
        == "not_applied"
    )

    job_search.append_application_event(
        durable_database,
        job_id,
        ApplicationEventInput(
            status=ApplicationStatus.PACKAGE_READY,
            occurred_at=NOW,
            notes="Package assembled.",
        ),
        idempotency_key="first-move",
    )

    with durable_database.session() as session:
        applications = session.scalars(select(Application)).all()
    assert len(applications) == 1
    assert (
        job_search.job_detail(durable_database, job_id, workspace_id).application_status
        == "package_ready"
    )
