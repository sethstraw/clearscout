"""Durable identity for a posting's requirements across its re-evaluations.

A requirement is a statement one evaluation makes about one posting. Its
positional id changes with each re-evaluation, and a citation stored under the
positional id cannot resolve after a re-run reorders or drops it. The identity
rule in ``tailorcv.domain.statements`` is what threads a requirement across
re-evaluations of the same posting, and this module is its posting-side caller.

The projection's ``Stated.id`` is a content address of the requirement text and
a gate's kind, numbered when one evaluation repeats it, so "same content" is the
exact match the identity rule uses, and "reworded" falls through to the
similarity match. The positional ``requirement_id`` a run assigns is never a
stable identifier.

Shared identity across different postings is not stored: two postings saying
"five years of Python" assert nothing about each other, and a durable shared
id would fabricate sameness into the record.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tailorcv.application.contracts import ReportRequirementInput
from tailorcv.application.master_cv.cv import content_hash
from tailorcv.domain import new_id
from tailorcv.domain.statements import Known, Stated, carried_identities
from tailorcv.infrastructure.persistence.models import JobEvaluation, PostingRequirement

# Scope for carried_identities; matches happen only within one posting.
_SCOPE = "posting"


def _address(text: str, gate_kind: str | None) -> str:
    # One line can state two gates of different kinds, so a gate's kind is part of its address.
    return content_hash(text if gate_kind is None else f"{gate_kind}\n{text}")


def stated_requirement(requirement: ReportRequirementInput) -> Stated:
    """Project one requirement as the statement the identity rule reads."""
    return Stated(
        id=_address(requirement.text, requirement.gate_kind), scope=_SCOPE, text=requirement.text
    )


def known_requirement(row: PostingRequirement) -> Known:
    """Project a stored requirement row as the statement the identity rule reads."""
    return Known(
        id=_address(row.text, row.gate_kind),
        scope=_SCOPE,
        text=row.text,
        key=row.requirement_key,
    )


def _numbered[S: Stated](statements: list[S]) -> list[S]:
    """Number each repeat of one address in reading order, so repeats pair with repeats."""
    seen: Counter[str] = Counter()
    numbered = []
    for statement in statements:
        seen[statement.id] += 1
        count = seen[statement.id]
        numbered.append(
            statement if count == 1 else replace(statement, id=f"{statement.id}#{count}")
        )
    return numbered


class RequirementIdentityService:
    """Carry requirement identities across re-evaluations of one posting."""

    @staticmethod
    def identify(
        session: Session,
        *,
        previous_evaluation_id: str | None,
        current: Sequence[ReportRequirementInput],
    ) -> dict[str, str]:
        """Return the durable key each current requirement inherits or is assigned.

        Keyed by the current run's positional ``requirement_id``. Every current
        requirement is present in the result; one whose identity does not carry
        from the prior evaluation is given a fresh key rather than reusing one.
        Duplicates within one current evaluation each get their own key.
        """
        if not current:
            return {}
        previous_known: list[Known] = []
        if previous_evaluation_id is not None:
            previous_known = _numbered(
                [
                    known_requirement(row)
                    for row in session.scalars(
                        select(PostingRequirement)
                        .where(PostingRequirement.job_evaluation_id == previous_evaluation_id)
                        .order_by(PostingRequirement.position)
                    )
                ]
            )
        stated = _numbered([stated_requirement(req) for req in current])
        carried = carried_identities(previous_known, stated)
        keys: dict[str, str] = {}
        consumed: set[str] = set()
        for req, statement in zip(current, stated, strict=True):
            inherited = carried.get(statement.id)
            if inherited is not None and inherited not in consumed:
                keys[req.requirement_id] = inherited
                consumed.add(inherited)
            else:
                keys[req.requirement_id] = new_id()
        return keys

    @staticmethod
    def persist(
        session: Session,
        *,
        evaluation: JobEvaluation,
        current: Sequence[ReportRequirementInput],
        keys: dict[str, str],
        audit: dict[str, Any],
    ) -> None:
        """Write one durable requirement row per current requirement."""
        session.add_all(_rows(evaluation=evaluation, current=current, keys=keys, audit=audit))
        session.flush()


def _rows(
    *,
    evaluation: JobEvaluation,
    current: Sequence[ReportRequirementInput],
    keys: dict[str, str],
    audit: dict[str, Any],
) -> Iterable[PostingRequirement]:
    for position, requirement in enumerate(current):
        yield PostingRequirement(
            job_posting_id=evaluation.job_posting_id,
            job_evaluation_id=evaluation.id,
            requirement_id=requirement.requirement_id,
            requirement_key=keys[requirement.requirement_id],
            text=requirement.text,
            importance=requirement.importance,
            position=position,
            is_hard_gate=requirement.is_hard_gate,
            gate_kind=requirement.gate_kind,
            gate_quote=requirement.gate_quote,
            gate_blocking=requirement.gate_blocking,
            **audit,
        )
