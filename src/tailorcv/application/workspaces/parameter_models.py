"""Typed read models for workspace job-search parameter versions."""

from datetime import datetime

from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.shared.models import StrictModel


class SearchParametersStateResponse(StrictModel):
    """Current immutable parameter version, or the unconfigured zero value."""

    workspace_id: str
    id: str | None
    version: int
    content_sha256: str
    effective_at: datetime | None
    configured: bool
    parameters: JobSearchParameters


class SearchParametersSavedResponse(StrictModel):
    """One append-only job-search parameter version after a write."""

    id: str
    workspace_id: str
    version: int
    content_sha256: str
    effective_at: datetime
    supersedes_id: str | None
    unchanged: bool
    parameters: JobSearchParameters


class SearchParameterVersionResponse(StrictModel):
    """One saved job-search parameter version."""

    id: str
    version: int
    content_sha256: str
    effective_at: datetime
    supersedes_id: str | None
    created_by: str
    origin: str
    parameters: JobSearchParameters
