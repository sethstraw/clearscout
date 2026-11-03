"""Assemble the employer evidence a cover letter is allowed to cite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from clearscout.application.tailoring.workflows._support import _read_artifact
from clearscout.application.tailoring.workflows.context import _WorkflowInputs
from clearscout.domain import RESEARCH_ARTIFACT_TYPES, ResearchKind
from clearscout.infrastructure.persistence.models import Artifact
from clearscout.infrastructure.web_fetch.company_research import (
    MAX_SOURCE_CHARACTERS,
    CareersPageRefusedError,
    ResearchSource,
    careers_source_refused,
    research_text_refused,
)

if TYPE_CHECKING:
    from clearscout.application.tailoring.workflows.service import WorkflowService


def _company_research(
    service: WorkflowService, inputs: _WorkflowInputs
) -> tuple[ResearchSource, ...]:
    """Combine the stored posting with bounded employer-owned web evidence."""
    posting_source = _posting_research(inputs)
    try:
        fetched = service.research_fetcher.fetch(
            inputs.careers_url, posting_urls=inputs.posting_urls
        )
    except CareersPageRefusedError:
        # A refused page's stored copy, and the about page it linked, are refused with it.
        return (posting_source,)
    present = {item.kind for item in fetched}
    reused = _stored_research(service, inputs, excluded=present)
    return (posting_source, *fetched, *reused)


def _stored_company_research(
    service: WorkflowService, inputs: _WorkflowInputs
) -> tuple[ResearchSource, ...]:
    """Return only research already stored for deterministic reuse checks."""
    return (_posting_research(inputs), *_stored_research(service, inputs, excluded=set()))


def _posting_research(inputs: _WorkflowInputs) -> ResearchSource:
    return ResearchSource(
        id="company.posting",
        kind="posting",
        url=inputs.posting_url,
        text=inputs.posting_content.decode("utf-8", errors="replace")[:MAX_SOURCE_CHARACTERS],
        method="stored_posting_artifact",
    )


def _stored_research(
    service: WorkflowService, inputs: _WorkflowInputs, *, excluded: set[ResearchKind]
) -> tuple[ResearchSource, ...]:
    """Reuse the newest stored employer pages when a fresh read is unavailable."""
    sources: list[ResearchSource] = []
    with service.database.session() as session:
        for kind in ("careers", "about"):
            if kind in excluded:
                continue
            artifact = session.scalar(
                select(Artifact)
                .where(
                    Artifact.workspace_id == inputs.workspace.id,
                    Artifact.related_entity_type == "job_posting",
                    Artifact.related_entity_id == inputs.job.id,
                    Artifact.logical_type == RESEARCH_ARTIFACT_TYPES[kind],
                    Artifact.archived_at.is_(None),
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .limit(1)
            )
            if artifact is None:
                continue
            source = ResearchSource(
                id=str(artifact.provenance.get("source_id") or f"company.{kind}"),
                kind=kind,
                url=str(artifact.provenance.get("source_url") or ""),
                text=_read_artifact(service, artifact, inputs.workspace).decode(
                    "utf-8", errors="replace"
                ),
                method="stored_research_artifact",
            )
            refused = (
                careers_source_refused(source, inputs.posting_urls)
                if kind == "careers"
                else research_text_refused(source.text)
            )
            if refused:
                continue
            sources.append(source)
    return tuple(sources)
