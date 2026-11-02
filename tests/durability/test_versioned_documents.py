"""The versioned-document base class holds one contract every service reuses.

Each subclass carries a distinct row, document, and response projection, but
optimistic concurrency, unchanged-noop, and the entity_label branch of the
ConflictError are all inherited. A regression in the base that only one
subclass exercises would slip past a per-service test, so this asserts the
shared behavior across the three services that use it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from tailorcv.application.failures import ConflictError
from tailorcv.application.tailoring.guardrails import Guardrails
from tailorcv.application.workspaces import (
    GuardrailService,
    SearchParametersService,
    WorkspaceService,
)
from tailorcv.application.workspaces.versioned import VersionedDocumentService
from tailorcv.domain.search_parameters import JobSearchParameters
from tailorcv.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage


@dataclass(frozen=True)
class Case:
    label: str
    entity_label: str
    service_factory: Callable[[Database], VersionedDocumentService[Any, Any, Any, Any]]
    first: Any
    second: Any


CASES = [
    Case(
        label="guardrails",
        entity_label="guardrails",
        service_factory=GuardrailService,
        first=Guardrails(forbidden=["one"]),
        second=Guardrails(forbidden=["two"]),
    ),
    Case(
        label="search_parameters",
        entity_label="parameters",
        service_factory=SearchParametersService,
        first=JobSearchParameters.model_validate({"seniority": {"target_titles": ["one"]}}),
        second=JobSearchParameters.model_validate({"seniority": {"target_titles": ["two"]}}),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.label)
def test_versioned_document_service_shared_contract(case: Case, durable_database: Database) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug=case.label, name=case.label
    )
    service = case.service_factory(durable_database)

    saved = service.save(workspace.id, case.first, expected_version=0)
    assert saved.version == 1
    assert saved.unchanged is False

    unchanged = service.save(workspace.id, case.first, expected_version=1)
    assert unchanged.unchanged is True
    assert unchanged.version == 1

    with pytest.raises(ConflictError) as exc:
        service.save(workspace.id, case.second, expected_version=0)
    assert case.entity_label in str(exc.value)
    assert "version 1, not 0" in str(exc.value)


def test_versioned_document_service_digest_is_stable_across_calls(
    durable_database: Database,
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="digest", name="digest"
    )
    service = GuardrailService(durable_database)
    rules = Guardrails(forbidden=["one", "two"])
    first = service.save(workspace.id, rules)
    reordered = Guardrails.model_validate(rules.model_dump())
    second = service.save(workspace.id, reordered)
    assert second.unchanged is True
    assert second.id == first.id
