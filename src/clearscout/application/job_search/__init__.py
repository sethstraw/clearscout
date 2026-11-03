"""Durable job and application use cases shared by every delivery adapter.

Each operation takes the database, and the artifact store when it reads or
writes one, so a caller names what it needs rather than the object that happens
to hold both.
"""

from clearscout.application.job_search.commands import (
    append_application_event,
    archive_job,
    archive_jobs,
    save_manual_job_review,
    set_closing_date,
    set_posting_hint,
    track_pending_discovery,
    unarchive_job,
)
from clearscout.application.job_search.evaluation_currentness import (
    acknowledge_evaluation_currentness,
)
from clearscout.application.job_search.evaluations import commit_evaluation
from clearscout.application.job_search.evidence_queries import evidence_source, fit_report
from clearscout.application.job_search.posting_queries import posting_diff, posting_observation
from clearscout.application.job_search.queries import (
    job_employer_facets,
    list_jobs,
    workspace_summary,
)
from clearscout.application.job_search.read_models import job_detail
from clearscout.application.job_search.requirement_gaps import requirement_gaps

__all__ = [
    "acknowledge_evaluation_currentness",
    "append_application_event",
    "archive_job",
    "archive_jobs",
    "commit_evaluation",
    "evidence_source",
    "fit_report",
    "job_detail",
    "job_employer_facets",
    "list_jobs",
    "posting_diff",
    "posting_observation",
    "requirement_gaps",
    "save_manual_job_review",
    "set_closing_date",
    "set_posting_hint",
    "track_pending_discovery",
    "unarchive_job",
    "workspace_summary",
]
