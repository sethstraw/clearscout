"""Persistence, API, and prompt-inclusion behavior for the tailoring hint."""

from __future__ import annotations

import pytest

from clearscout.application.config import Settings
from clearscout.application.failures import NotFoundError
from clearscout.application.job_search.commands import set_posting_hint
from clearscout.application.prompts import render
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain import utc_now
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Employer, JobPosting
from tests.support.artifacts import runtime_storage


def _seed_posting(database: Database) -> tuple[str, str]:
    workspace = WorkspaceService(database, runtime_storage(database)).create(
        slug="hint-check", name="Hint Check"
    )
    with database.session() as session:
        employer = Employer(workspace_id=str(workspace.id), name="Globex", normalized_name="globex")
        session.add(employer)
        session.flush()
        job = JobPosting(
            workspace_id=str(workspace.id),
            employer_id=employer.id,
            title="Backend Engineer",
            normalized_title="backend engineer",
            first_seen_at=utc_now(),
        )
        session.add(job)
        session.flush()
        return str(workspace.id), str(job.id)


def test_set_posting_hint_writes_and_clears(durable_database: Database) -> None:
    """A round trip through set-hint stores and clears the free-text hint."""
    workspace_id, job_id = _seed_posting(durable_database)

    initial = set_posting_hint(durable_database, job_id, workspace_id, "Refer HM: Alice.")
    assert initial.hint == "Refer HM: Alice."
    assert initial.changed is True

    same = set_posting_hint(durable_database, job_id, workspace_id, "Refer HM: Alice.")
    assert same.hint == "Refer HM: Alice."
    assert same.changed is False

    cleared = set_posting_hint(durable_database, job_id, workspace_id, "")
    assert cleared.hint is None
    assert cleared.changed is True


def test_set_posting_hint_normalises_blank_to_none(durable_database: Database) -> None:
    """A whitespace-only body reads as clear-the-hint, not "hint is spaces"."""
    workspace_id, job_id = _seed_posting(durable_database)

    result = set_posting_hint(durable_database, job_id, workspace_id, "   ")

    assert result.hint is None


def test_set_posting_hint_refuses_unknown_job(durable_database: Database) -> None:
    """A job the workspace does not own raises rather than writing a stray row."""
    workspace_id, _ = _seed_posting(durable_database)

    with pytest.raises(NotFoundError):
        set_posting_hint(durable_database, "not-a-job", workspace_id, "anything")


def test_draft_prompt_hint_block_reflects_the_value() -> None:
    """The rendered draft prompt carries the hint verbatim when supplied."""
    body = render(
        "draft_user",
        role="Backend Engineer",
        company="Globex",
        keywords="routing",
        evidence="(evidence)",
        feedback="(no prior)",
        posting_hint="Refer HM: Alice.",
        active_disputes="",
    )

    assert "Refer HM: Alice." in body
    assert "user-supplied hint" in body


def test_cover_letter_prompt_hint_block_reflects_the_value() -> None:
    """The cover-letter template carries the hint verbatim when supplied."""
    body = render(
        "cover_letter_user",
        role="Backend Engineer",
        company="Globex",
        evidence="(evidence)",
        research="(research)",
        feedback="(no prior)",
        previous_letter="(no prior)",
        posting_hint="Team is small; opening should be personal.",
        active_disputes="",
    )

    assert "Team is small; opening should be personal." in body


def test_critic_prompt_hint_block_reflects_the_value() -> None:
    """The critic template carries the hint verbatim when supplied."""
    body = render(
        "critic_user",
        role="Backend Engineer",
        requirements="(requirements)",
        asserted="(asserted)",
        document="resume",
        draft="(resume)",
        citations="(citations)",
        offered="(offered)",
        research="",
        written_parts="the bullets",
        posting_hint="Do not report absent items I do not care about.",
        active_disputes="",
    )

    assert "Do not report absent items I do not care about." in body


def test_posting_settings_defaults_enable_hint_true() -> None:
    """The ship-dark switch defaults to hint enabled."""
    settings = Settings()

    assert settings.postings.enable_hint is True
