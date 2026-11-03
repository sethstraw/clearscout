"""Query and prompt-block helpers for active dispute rulings."""

from __future__ import annotations

import uuid

from clearscout.application.job_search.postings.rulings import (
    RequirementRulingService,
    cited_in_body,
    dispute_block,
)
from clearscout.application.workspaces import WorkspaceService
from clearscout.domain.rulings import Ruling
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage


def _workspace(database: Database) -> str:
    row = WorkspaceService(database, runtime_storage(database)).create(
        slug=f"dispute-{uuid.uuid4().hex[:8]}", name="Dispute Check"
    )
    return str(row.id)


def test_active_disputes_are_partitioned_by_kind(durable_database: Database) -> None:
    workspace_id = _workspace(durable_database)
    service = RequirementRulingService(durable_database)
    audit = {"created_by": "test", "origin": "test"}
    service.set(
        workspace_id=workspace_id,
        requirement_key="r-1",
        ruling=Ruling.DISPUTED_FIT,
        note="Five years overstates the ask",
        audit=audit,
    )
    service.set(
        workspace_id=workspace_id,
        requirement_key="r-2",
        ruling=Ruling.MET,
        note=None,
        audit=audit,
    )
    service.set(
        workspace_id=workspace_id,
        requirement_key="r-3",
        ruling=Ruling.NOT_APPLICABLE,
        note=None,
        audit=audit,
    )
    service.set(
        workspace_id=workspace_id,
        requirement_key="r-4",
        ruling=Ruling.DISPUTED_EXTRACTION,
        note="The posting did not state this",
        audit=audit,
    )

    fit_disputes = service.active_fit_disputes(workspace_id)
    extraction_disputes = service.active_extraction_disputes(workspace_id)

    assert [item.requirement_key for item in fit_disputes] == ["r-1"]
    assert fit_disputes[0].note == "Five years overstates the ask"
    assert fit_disputes[0].ruling is Ruling.DISPUTED_FIT
    assert [item.requirement_key for item in extraction_disputes] == ["r-4"]
    assert extraction_disputes[0].ruling is Ruling.DISPUTED_EXTRACTION


def test_active_disputes_return_empty_when_no_disputes_exist(durable_database: Database) -> None:
    """A workspace with no disputes returns an empty list, not None."""
    workspace_id = _workspace(durable_database)
    service = RequirementRulingService(durable_database)
    assert service.active_fit_disputes(workspace_id) == []
    assert service.active_extraction_disputes(workspace_id) == []


def test_dispute_block_renders_key_text_and_note() -> None:
    """The block quotes the identifier, the text, and the note when present."""

    class _Ruling:
        requirement_key: str
        requirement_text: str
        note: str | None

        def __init__(self, key: str, text: str, note: str | None) -> None:
            self.requirement_key = key
            self.requirement_text = text
            self.note = note

    rendered = dispute_block(
        [
            _Ruling("r-1", "Five years of Python", "prefer competence, not tenure"),
            _Ruling("r-2", "PhD required", None),
        ]
    )

    assert "- r-1: Five years of Python" in rendered
    assert "note: prefer competence, not tenure" in rendered
    assert "- r-2: PhD required" in rendered


def test_dispute_block_is_empty_for_no_rulings() -> None:
    """No disputes produce an empty block."""
    assert dispute_block([]) == ""


def test_dispute_block_uses_placeholder_for_missing_text() -> None:
    """A ruling whose requirement text is absent still renders with a placeholder."""

    class _Ruling:
        requirement_key: str
        requirement_text: str
        note: str | None

        def __init__(self) -> None:
            self.requirement_key = "r-orphan"
            self.requirement_text = ""
            self.note = None

    rendered = dispute_block([_Ruling()])
    assert "r-orphan" in rendered
    assert "requirement text unavailable" in rendered


def test_cited_in_body_normalises_whitespace_and_case() -> None:
    """The safety check tolerates whitespace and casing but not fabrication."""
    body = "We require FIVE  years\nof Python engineering experience."
    assert cited_in_body("five years of python", body) is True
    assert cited_in_body("we require   five YEARS OF python", body) is True
    assert cited_in_body("ten years of Python", body) is False
    assert cited_in_body("", body) is False
    assert cited_in_body("anything", "") is False
