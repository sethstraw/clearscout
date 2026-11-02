"""Which weights an overall score was computed under, and who may change them.

Weights are versioned and never edited, so a number in the ledger can always be
explained by the policy in force when it was written.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from tailorcv.application.contracts import SCORE_COMPONENTS, EvaluationCommit, score_weight_digest
from tailorcv.application.failures import ConflictError, NotFoundError, ValidationError
from tailorcv.application.job_search.score_models import ScoreWeightSetResponse
from tailorcv.application.workspaces import parameters_in
from tailorcv.domain import utc_now
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    ScoreWeightComponent,
    ScoreWeightSet,
    Workspace,
)


def _resolve_weight_set(
    session: Session, workspace_id: str, explicit_id: str | None = None
) -> ScoreWeightSet:
    """Return the policy that governs this workspace right now.

    Resolution order is the explicit request, then the policy this
    workspace's parameters name, then the workspace's own newest policy,
    then the shipped default. The middle two are what let authoring a policy
    make it a workspace's own without passing an identifier on every commit.
    """
    weight_set = None
    if explicit_id:
        weight_set = session.get(ScoreWeightSet, explicit_id)
    else:
        newest = _weight_set_query(workspace_id)
        named = parameters_in(session, workspace_id).score_weight_set.strip()
        if named:
            weight_set = session.scalar(newest.where(ScoreWeightSet.name == named).limit(1))
            if weight_set is None:
                raise ValidationError(
                    f"These search parameters name the scoring policy {named!r}, "
                    "which does not exist in this workspace."
                )
        if weight_set is None:
            weight_set = session.scalar(
                newest.where(ScoreWeightSet.workspace_id == workspace_id).limit(1)
            )
        if weight_set is None:
            weight_set = session.scalar(
                newest.where(ScoreWeightSet.workspace_id.is_(None)).limit(1)
            )
    if weight_set is None or weight_set.workspace_id not in (None, workspace_id):
        raise ValidationError("The requested score weight set is unavailable in this workspace.")
    return weight_set


def _validate_weights(weights: dict[str, Decimal]) -> None:
    """Hold the arithmetic boundary every weight set has to satisfy.

    Shared by the commit path and by policy authoring, so a set that could
    not have scored an evaluation cannot be written in the first place.
    """
    expected = set(SCORE_COMPONENTS)
    if set(weights) != expected or sum(weights.values(), Decimal(0)) != Decimal(1):
        listed = ", ".join(sorted(expected))
        raise ValidationError(
            f"A score weight set carries exactly these components and they sum to 1: {listed}."
        )


def _weights_of(session: Session, weight_set_id: str) -> dict[str, Decimal]:
    """Read one policy's component weights."""
    rows = session.scalars(
        select(ScoreWeightComponent)
        .where(ScoreWeightComponent.weight_set_id == weight_set_id)
        .order_by(ScoreWeightComponent.component)
    ).all()
    return {row.component: Decimal(row.weight) for row in rows}


def score_policy(
    session: Session, proposal: EvaluationCommit
) -> tuple[ScoreWeightSet, dict[str, Decimal]]:
    """Resolve one stored policy and validate its complete arithmetic boundary."""
    weight_set = _resolve_weight_set(session, proposal.workspace_id, proposal.score_weight_set_id)
    weights = _weights_of(session, weight_set.id)
    _validate_weights(weights)
    return weight_set, weights


def _weight_set_query(workspace_id: str) -> Select[Any]:
    """Return the newest-policy-first query every lookup narrows further."""
    return (
        select(ScoreWeightSet)
        .where(
            or_(
                ScoreWeightSet.workspace_id.is_(None),
                ScoreWeightSet.workspace_id == workspace_id,
            )
        )
        .order_by(
            ScoreWeightSet.effective_at.desc(),
            ScoreWeightSet.version.desc(),
            ScoreWeightSet.id,
        )
    )


class ScorePolicyService:
    """List and append the scoring policies of one workspace."""

    def __init__(self, database: Database) -> None:
        """Bind the service to one database."""
        self.database = database

    def list_score_weight_sets(self, workspace_id: str) -> list[ScoreWeightSetResponse]:
        """Return every policy this workspace can score under, newest first.

        Includes the shipped defaults, which are visible to everyone and owned
        by nobody. The one currently in force is marked, because which weights
        apply is otherwise a question the resolution order answers silently.
        """
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")
            sets = session.scalars(_weight_set_query(workspace_id)).all()
            try:
                active_id = _resolve_weight_set(session, workspace_id).id
            except ValidationError:
                active_id = ""
            return [
                ScoreWeightSetResponse.model_validate(
                    {
                        "id": item.id,
                        "name": item.name,
                        "version": item.version,
                        "effective_at": item.effective_at,
                        "is_shipped_default": item.workspace_id is None,
                        "is_active": item.id == active_id,
                        "weights": [
                            {"component": component, "weight": float(weight)}
                            for component, weight in sorted(_weights_of(session, item.id).items())
                        ],
                    }
                )
                for item in sets
            ]

    def create_score_weight_set(
        self, workspace_id: str, *, name: str, weights: dict[str, Decimal]
    ) -> ScoreWeightSetResponse:
        """Append a workspace-owned policy. Never edits or replaces one.

        An evaluation points at the policy that produced it, so editing a policy
        in place would rewrite what a recorded decision meant. The database
        agrees: these tables are immutable, and an UPDATE or DELETE is refused
        by trigger rather than by convention.
        """
        label = name.strip()
        if not label:
            raise ValidationError("A scoring policy needs a name.")
        _validate_weights(weights)

        # The digest is unique across the database, so identical weights are one
        # policy rather than two that agree.
        digest = score_weight_digest({component: str(weights[component]) for component in weights})

        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None or workspace.archived_at is not None:
                raise NotFoundError(f"Workspace {workspace_id!r} does not exist.")

            existing = session.scalar(
                select(ScoreWeightSet).where(ScoreWeightSet.content_sha256 == digest)
            )
            if existing is not None:
                raise ConflictError(
                    f"These weights already exist as {existing.name!r} v{existing.version}. "
                    "Select that policy instead of creating a duplicate of it."
                )

            highest = session.scalar(
                select(func.max(ScoreWeightSet.version)).where(
                    ScoreWeightSet.workspace_id == workspace_id,
                    ScoreWeightSet.name == label,
                )
            )
            weight_set = ScoreWeightSet(
                workspace_id=workspace_id,
                name=label,
                version=int(highest or 0) + 1,
                content_sha256=digest,
                effective_at=utc_now(),
                origin="score_policy_author",
            )
            session.add(weight_set)
            session.flush()
            for component, weight in sorted(weights.items()):
                session.add(
                    ScoreWeightComponent(
                        weight_set_id=weight_set.id,
                        component=component,
                        weight=weight,
                        origin="score_policy_author",
                    )
                )
            session.commit()
            return ScoreWeightSetResponse.model_validate(
                {
                    "id": weight_set.id,
                    "name": weight_set.name,
                    "version": weight_set.version,
                    "effective_at": weight_set.effective_at,
                    "is_shipped_default": False,
                    "is_active": True,
                    "weights": [
                        {"component": component, "weight": float(weight)}
                        for component, weight in sorted(weights.items())
                    ],
                }
            )
