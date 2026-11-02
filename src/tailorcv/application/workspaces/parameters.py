"""Read and version one workspace's job-search parameters.

Writes append, so an evaluation recorded last month can still be read against
the parameters in force last month. The settings file is the other write
surface, and nothing copies between the two automatically.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.failures import ValidationError
from tailorcv.application.workspaces.parameter_models import (
    SearchParametersSavedResponse,
    SearchParametersStateResponse,
    SearchParameterVersionResponse,
)
from tailorcv.application.workspaces.versioned import VersionedDocumentService
from tailorcv.domain.search_parameters import (
    EMPLOYMENT_TYPES,
    ClusterKind,
    CompensationBand,
    JobSearchParameters,
    Location,
)
from tailorcv.infrastructure.persistence.models import JobSearchParameterSet


def digest(parameters: JobSearchParameters) -> str:
    """Return a stable content hash for one set of parameters, from sorted compact JSON."""
    payload = json.dumps(_document_of(parameters), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _band_summary(band: CompensationBand) -> str:
    numbers = ", ".join(
        f"{label} {value}"
        for label, value in (
            ("minimum", band.minimum),
            ("target", band.target),
            ("stretch", band.stretch),
        )
        if value is not None
    )
    return f"    {band.currency} {band.basis}: {numbers}"


def _one_location_summary(parameters: JobSearchParameters, location: Location) -> list[str]:
    basis = f" ({location.basis})" if location.basis else ""
    lines = [f"  {location.key}: {location.render()} | {location.eligibility}{basis}"]
    if location.aliases:
        lines.append(f"    also written as: {', '.join(location.aliases)}")
    if location.arrangement is not None:
        here = parameters.arrangement_for(location.key)
        stated = ", ".join(
            f"{name} {value}" for name, value in here.named().items() if value is not None
        )
        lines.append(f"    arrangement here: {stated or 'inherited'}")
    lines.extend(_band_summary(band) for band in parameters.bands_for(location.key))
    return lines


def _location_summary(parameters: JobSearchParameters) -> list[str]:
    return [
        line
        for location in parameters.locations
        for line in _one_location_summary(parameters, location)
    ]


def _arrangement_summary(parameters: JobSearchParameters) -> list[str]:
    lines = [
        f"  {name}: {acceptance}"
        for name, acceptance in parameters.arrangement.named().items()
        if acceptance is not None
    ]
    if parameters.arrangement.maximum_office_days is not None:
        lines.append(f"  office days a week at most: {parameters.arrangement.maximum_office_days}")
    if parameters.arrangement.onsite_requires_paid_travel is not None:
        answer = "yes" if parameters.arrangement.onsite_requires_paid_travel else "no"
        lines.append(f"  office attendance needs funded travel: {answer}")
    return lines


def _named_parameter_values(
    parameters: JobSearchParameters,
) -> tuple[tuple[str, Sequence[str]], ...]:
    return (
        (
            "target titles",
            [item.title for item in parameters.seniority.target_titles if item.title.strip()],
        ),
        ("titles to skip", parameters.seniority.excluded_titles),
        ("engagement types", parameters.employment.types),
        ("engagement types refused", parameters.employment.excluded_types),
        ("payroll arrangements", parameters.employment.payroll_arrangements),
        ("industries", parameters.employers.industries),
        ("industries to skip", parameters.employers.excluded_industries),
        ("employers to skip", parameters.employers.excluded_employers),
        ("employer sizes", parameters.employers.sizes),
        ("employer types", [item.name for item in parameters.employers.stated_archetypes]),
        (
            "work capabilities",
            [item.name for item in parameters.focus.stated_clusters(ClusterKind.CAPABILITY)],
        ),
        (
            "subject domains",
            [item.name for item in parameters.focus.stated_clusters(ClusterKind.DOMAIN)],
        ),
        ("work boundaries", parameters.focus.stated_boundaries),
    )


def parameter_summary(
    parameters: JobSearchParameters,
    *,
    source: Path | str | None = None,
    empty_hint: str = "Add a [job_search] table to your settings file to express any.",
) -> tuple[str, ...]:
    """Return the human-readable summary for a search-state adapter."""
    if not parameters.configured:
        return (
            "No job-search parameters are set.",
            empty_hint,
        )
    lines = [f"Read from: {source or 'defaults'}"]
    lines.extend(_location_summary(parameters))
    if parameters.working_time_zone is not None:
        lines.append(f"  usual working time zone: {parameters.working_time_zone.value}")
    if parameters.working_day_shift_hours:
        lines.append(
            f"  hours you can shift your working day: {parameters.working_day_shift_hours}"
        )
    lines.extend(_arrangement_summary(parameters))
    if parameters.seniority.stated:
        low = parameters.seniority.minimum or "any"
        high = parameters.seniority.maximum or "any"
        lines.append(f"  level: {low} to {high}")
    for label, values in _named_parameter_values(parameters):
        if values:
            lines.append(f"  {label}: {', '.join(values)}")
    if parameters.opportunity_thesis.strip():
        lines.append(f"  opportunity thesis: {parameters.opportunity_thesis.strip()}")
    if parameters.score_weight_set:
        lines.append(f"  scoring policy: {parameters.score_weight_set}")
    return tuple(lines)


def _document_of(parameters: JobSearchParameters) -> dict[str, Any]:
    """Return the JSON-safe document stored for one set of parameters."""
    return parameters.model_dump(mode="json")


@dataclass(frozen=True)
class SearchParametersSnapshot:
    """One parameter document coupled to its durable version identity."""

    parameters: JobSearchParameters
    parameter_set_id: str | None
    version: int


def parameter_snapshot_in(session: Session, workspace_id: str) -> SearchParametersSnapshot:
    """Return the exact parameter document and row identity in one transaction."""
    record = session.scalars(
        select(JobSearchParameterSet)
        .where(JobSearchParameterSet.workspace_id == workspace_id)
        .order_by(JobSearchParameterSet.version.desc())
        .limit(1)
    ).first()
    if record is None:
        return SearchParametersSnapshot(JobSearchParameters(), None, 0)
    return SearchParametersSnapshot(
        JobSearchParameters.model_validate(record.document),
        record.id,
        record.version,
    )


def parameters_in(session: Session, workspace_id: str) -> JobSearchParameters:
    """Return the parameters in force inside a caller's transaction, opening no second session."""
    # A second session would issue a nested BEGIN IMMEDIATE, which deadlocks on SQLite.
    return parameter_snapshot_in(session, workspace_id).parameters


class SearchParametersService(
    VersionedDocumentService[
        JobSearchParameterSet,
        JobSearchParameters,
        SearchParametersSavedResponse,
        SearchParametersStateResponse,
    ]
):
    """The current parameters for a workspace, and their append-only history."""

    row_type = JobSearchParameterSet
    entity_label = "parameters"

    def save(
        self,
        workspace_id: str,
        document: JobSearchParameters,
        *,
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
        expected_version: int | None = None,
    ) -> SearchParametersSavedResponse:
        """Append a new version, refusing an employment type nobody has saved before.

        A value already on record is never refused, so a free-form row keeps
        loading. Only a new value has to be one of ``EMPLOYMENT_TYPES``.
        """
        with self.database.session() as session:
            return self.save_in_session(
                session,
                workspace_id,
                document,
                actor=actor,
                origin=origin,
                correlation_id=correlation_id,
                expected_version=expected_version,
            )

    def save_in_session(
        self,
        session: Session,
        workspace_id: str,
        document: JobSearchParameters,
        *,
        actor: str = "local_user",
        origin: str = "tailorcv",
        correlation_id: str | None = None,
        expected_version: int | None = None,
    ) -> SearchParametersSavedResponse:
        """Validate and append parameters inside the caller's transaction."""
        self._workspace(session, workspace_id)
        previous = parameters_in(session, workspace_id)
        known = set(EMPLOYMENT_TYPES) | set(previous.employment.types)
        unknown = sorted({value for value in document.employment.types if value not in known})
        if unknown:
            raise ValidationError(
                "Employment types must be one of: "
                f"{', '.join(EMPLOYMENT_TYPES)}; got new value(s): {', '.join(unknown)}"
            )
        return super().save_in_session(
            session,
            workspace_id,
            document,
            actor=actor,
            origin=origin,
            correlation_id=correlation_id,
            expected_version=expected_version,
        )

    def history(self, workspace_id: str) -> list[SearchParameterVersionResponse]:
        """Return every version, newest first."""
        with self.database.session() as session:
            self._workspace(session, workspace_id)
            records = session.scalars(
                select(JobSearchParameterSet)
                .where(JobSearchParameterSet.workspace_id == workspace_id)
                .order_by(JobSearchParameterSet.version.desc())
            ).all()
            return [
                SearchParameterVersionResponse.model_validate(
                    {
                        "id": record.id,
                        "version": record.version,
                        "content_sha256": record.content_sha256,
                        "effective_at": record.effective_at.isoformat(),
                        "supersedes_id": record.supersedes_id,
                        "created_by": record.created_by,
                        "origin": record.origin,
                        "parameters": record.document,
                    }
                )
                for record in records
            ]

    def _document_of(self, document: JobSearchParameters) -> dict[str, Any]:
        return _document_of(document)

    def _parse(self, record: JobSearchParameterSet | None) -> JobSearchParameters:
        if record is None:
            return JobSearchParameters()
        return JobSearchParameters.model_validate(record.document)

    def _build_row(
        self,
        *,
        workspace_id: str,
        version: int,
        content_sha256: str,
        effective_at: datetime,
        supersedes_id: str | None,
        document: dict[str, Any],
        actor: str,
        origin: str,
        correlation_id: str | None,
    ) -> JobSearchParameterSet:
        return JobSearchParameterSet(
            workspace_id=workspace_id,
            version=version,
            content_sha256=content_sha256,
            effective_at=effective_at,
            supersedes_id=supersedes_id,
            document=document,
            created_by=actor,
            origin=origin,
            correlation_id=correlation_id,
        )

    def _render_saved(
        self, record: JobSearchParameterSet, *, unchanged: bool
    ) -> SearchParametersSavedResponse:
        return SearchParametersSavedResponse.model_validate(
            {
                "id": record.id,
                "workspace_id": record.workspace_id,
                "version": record.version,
                "content_sha256": record.content_sha256,
                "effective_at": record.effective_at.isoformat(),
                "supersedes_id": record.supersedes_id,
                "unchanged": unchanged,
                "parameters": record.document,
            }
        )

    def _render_state(
        self, workspace_id: str, record: JobSearchParameterSet | None
    ) -> SearchParametersStateResponse:
        parameters = self._parse(record)
        return SearchParametersStateResponse.model_validate(
            {
                "workspace_id": workspace_id,
                "id": record.id if record else None,
                "version": record.version if record else 0,
                "content_sha256": record.content_sha256 if record else "",
                "effective_at": record.effective_at.isoformat() if record else None,
                "configured": parameters.configured,
                "parameters": _document_of(parameters),
            }
        )
