"""State one person's search parameters as instructions a model can act on.

The parameter and rejection helpers spend nothing and render only what the
person answered or ruled on. The accepted-proposals helper reads the inbox so
the model receives the workspace's own decisions as evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from tailorcv.application.discovery.derived_vocabulary_rules import (
    same_employer_archetype,
    searched_archetypes,
)
from tailorcv.application.discovery.proposals import CarriedRejection, CarriedRejections
from tailorcv.application.discovery.search_proposal_inbox import effective_proposal_rows
from tailorcv.application.prompts import flatten
from tailorcv.domain import SearchProposalState
from tailorcv.domain.identity import normalize_text
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    Cluster,
    ClusterKind,
    CompensationBand,
    EmployerArchetype,
    JobSearchParameters,
    Location,
)
from tailorcv.infrastructure.persistence.models import (
    DerivedCluster,
    DerivedEmployerArchetype,
    DerivedSearchTitle,
)


def _named(cluster: Cluster) -> str:
    """Render one cluster as its name and the words it stands for."""
    terms = ", ".join(item for item in cluster.terms if item.strip())
    return f"{cluster.name} ({terms})" if terms else cluster.name


def _summary_line(
    lines: list[str], label: str, values: Sequence[str], separator: str = ", "
) -> None:
    """Append one preference line when it contains a stated value."""
    wanted = [flatten(value) for value in values if flatten(value)]
    if wanted:
        lines.append(f"{label}: {separator.join(wanted)}")


def _describe_search_place(
    location: Location, arrangement: Arrangement, compensation: Sequence[str]
) -> str:
    """Render one place with the constraints that apply there."""
    details = [f"eligibility {location.eligibility}"]
    if location.basis:
        details.append(f"basis {location.basis}")
    preferences = ", ".join(
        f"{name} {value}"
        for name, value in arrangement.named().items()
        if value is not None and value is not Acceptance.REFUSED
    )
    if preferences:
        details.append(f"arrangement {preferences}")
    refusals = ", ".join(
        name for name, value in arrangement.named().items() if value is Acceptance.REFUSED
    )
    if refusals:
        details.append(f"refused {refusals}")
    if arrangement.maximum_office_days is not None:
        details.append(f"max office days a week {arrangement.maximum_office_days}")
    if arrangement.onsite_requires_paid_travel:
        details.append("office attendance only with employer-funded travel")
    if arrangement.maximum_travel_percent is not None:
        details.append(f"max travel {arrangement.maximum_travel_percent}%")
    if arrangement.relocation is not None:
        details.append(f"relocation {arrangement.relocation}")
    if compensation:
        details.append(f"compensation {', '.join(compensation)}")
    return f"{location.render()} ({'; '.join(details)})"


def _format_compensation_band(band: CompensationBand) -> str:
    """Render the stated amounts of one compensation band."""
    values = ", ".join(
        part
        for part in (
            f"minimum {band.minimum}" if band.minimum is not None else "",
            f"target {band.target}" if band.target is not None else "",
            f"stretch {band.stretch}" if band.stretch is not None else "",
        )
        if part
    )
    return f"{values} {band.currency} {band.basis}"


def _summarize_unscoped_arrangement(lines: list[str], parameters: JobSearchParameters) -> None:
    if parameters.locations:
        return
    accepted = parameters.arrangement.named()
    _summary_line(
        lines,
        "Working arrangements accepted",
        [name for name, value in accepted.items() if value and value is not Acceptance.REFUSED],
    )
    _summary_line(
        lines,
        "Working arrangements refused",
        [name for name, value in accepted.items() if value is Acceptance.REFUSED],
    )
    if parameters.arrangement.maximum_office_days is not None:
        lines.append(f"Maximum office days a week: {parameters.arrangement.maximum_office_days}")
    if parameters.arrangement.maximum_travel_percent is not None:
        lines.append(f"Maximum travel: {parameters.arrangement.maximum_travel_percent}%")
    if parameters.arrangement.onsite_requires_paid_travel:
        lines.append("Office attendance only with employer-funded travel")
    if parameters.arrangement.relocation is not None:
        lines.append(f"Relocation: {'yes' if parameters.arrangement.relocation else 'no'}")


def _summarize_focus_and_employers(
    lines: list[str],
    parameters: JobSearchParameters,
    allowed: Sequence[EmployerArchetype],
) -> None:
    _summary_line(
        lines,
        "Kinds of work this person does",
        [_named(item) for item in parameters.focus.stated_clusters(ClusterKind.CAPABILITY)],
    )
    _summary_line(
        lines,
        "Subjects this work is about",
        [_named(item) for item in parameters.focus.stated_clusters(ClusterKind.DOMAIN)],
    )
    boundaries = [flatten(item) for item in parameters.focus.stated_boundaries if flatten(item)]
    if boundaries:
        lines.append("Work this person does not want:")
        lines.extend(f"- {item}" for item in boundaries)
    _summary_line(lines, "Industries of interest", parameters.employers.industries)
    _summary_line(lines, "Industries to leave out", parameters.employers.excluded_industries)
    _summary_line(lines, "Employer sizes worth considering", parameters.employers.sizes)
    _summary_line(
        lines,
        "Kinds of organisation worth looking at",
        [
            f"{item.name} ({item.because})" if item.because else item.name
            for item in searched_archetypes(parameters, allowed)
        ],
    )
    _summary_line(lines, "Organisations to leave out", parameters.employers.excluded_employers)


def _summarize_seniority(lines: list[str], parameters: JobSearchParameters) -> None:
    if not (parameters.seniority.minimum or parameters.seniority.maximum):
        return
    band = " to ".join(
        part for part in (parameters.seniority.minimum, parameters.seniority.maximum) if part
    )
    lines.append(f"Seniority band: {band}")


def search_summary(
    parameters: JobSearchParameters, allowed: Sequence[EmployerArchetype] = ()
) -> str:
    """State the search as instructions, leaving out every unanswered question.

    An empty field is no opinion rather than a refusal, so an absent line means
    the model is told nothing about that question instead of being told a
    default somebody never chose. ``allowed`` carries the archetypes an earlier
    run proposed and the person accepted, which are stated as their own.
    """
    lines: list[str] = []

    _summary_line(
        lines,
        "Opportunity thesis (person-stated preference, not evidence)",
        [parameters.opportunity_thesis],
    )
    _summary_line(
        lines,
        "Titles worth looking for",
        [
            f"{item.title} ({item.lane.value} track)" if item.lane else item.title
            for item in parameters.seniority.target_titles
            if item.title.strip()
        ],
        "; ",
    )
    _summary_line(lines, "Titles to skip", parameters.seniority.excluded_titles, "; ")
    _summary_line(
        lines,
        "Hard exclusion conditions the model must honor",
        [
            f"{condition} (reason: {reason})"
            for condition, reason in parameters.excluded_conditions
            if condition.strip()
        ],
    )
    if parameters.working_time_zone is not None:
        lines.append(f"Usual working time zone: {parameters.working_time_zone.value}")
    # Stated only when non-zero, so an unshifted search keeps its context digest.
    if parameters.working_day_shift_hours:
        lines.append(
            f"Hours this person can shift their working day: {parameters.working_day_shift_hours}"
        )
    _summary_line(
        lines,
        "Places this person may work",
        [
            _describe_search_place(
                location,
                parameters.arrangement_for(location.key),
                [_format_compensation_band(band) for band in parameters.bands_for(location.key)],
            )
            for location in parameters.locations
        ],
    )
    _summarize_unscoped_arrangement(lines, parameters)
    _summary_line(lines, "Engagement types", parameters.employment.types)
    _summary_line(lines, "Engagement types refused", parameters.employment.excluded_types)
    _summary_line(
        lines, "Payroll arrangements accepted", parameters.employment.payroll_arrangements
    )
    _summarize_focus_and_employers(lines, parameters, allowed)
    _summarize_seniority(lines, parameters)
    return "\n".join(lines) or "This person has stated no search preferences."


def _accepted_names(
    session: Session,
    row_type: Any,
    workspace_id: str,
    master_cv_version_id: str,
    column: str,
    equivalent: Callable[[str, str], bool] | None = None,
) -> tuple[str, ...]:
    """Return distinct accepted names, carrying decisions across CV versions."""
    rows = effective_proposal_rows(session, row_type, workspace_id, master_cv_version_id)
    kept: list[str] = []
    for value in (
        str(getattr(row, column))
        for row in rows
        if row.state == SearchProposalState.ACCEPTED.value and getattr(row, column)
    ):
        if any(
            equivalent(value, previous)
            if equivalent is not None
            else normalize_text(value) == normalize_text(previous)
            for previous in kept
        ):
            continue
        kept.append(value)
    return tuple(kept)


def accepted_proposals_block(session: Session, workspace_id: str, master_cv_version_id: str) -> str:
    """Render what the workspace has already accepted, as evidence the model reads.

    The three inbox axes each surface as one line, so a re-proposal of an
    accepted name is not a citation the model can make against nothing. Empty
    when no axis holds an acceptance, so a caller may include it unconditionally.
    """
    titles = _accepted_names(
        session, DerivedSearchTitle, workspace_id, master_cv_version_id, "title"
    )
    archetypes = _accepted_names(
        session,
        DerivedEmployerArchetype,
        workspace_id,
        master_cv_version_id,
        "name",
        same_employer_archetype,
    )
    clusters = _accepted_names(session, DerivedCluster, workspace_id, master_cv_version_id, "name")
    if not (titles or archetypes or clusters):
        return ""
    return "\n".join(
        [
            "Already accepted (do not re-propose):",
            f"- Titles: {'; '.join(titles)}",
            f"- Archetypes: {'; '.join(archetypes)}",
            f"- Clusters: {'; '.join(clusters)}",
        ]
    )


def _render_carried(entry: CarriedRejection) -> str:
    """Render one rejection as its name, followed by the reason when the user stated one."""
    if entry.reason is None:
        return entry.name
    return f"{entry.name} (rejected: {entry.reason.value})"


def carried_rejections_block(carried: CarriedRejections) -> str:
    """Render the rejection preamble; empty when nothing has been rejected."""
    if not carried.has_any():
        return ""
    lines: list[str] = []
    if carried.titles:
        lines.append(
            "Titles already ruled out (do not propose): "
            + "; ".join(_render_carried(item) for item in carried.titles)
        )
    if carried.archetypes:
        lines.append(
            "Kinds of organisation already ruled out (do not propose): "
            + "; ".join(_render_carried(item) for item in carried.archetypes)
        )
    if carried.clusters:
        lines.append(
            "Capabilities and domains already ruled out (do not propose): "
            + "; ".join(_render_carried(item) for item in carried.clusters)
        )
    return "\n".join(lines)
