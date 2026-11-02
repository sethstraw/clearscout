"""Read master-CV versions, comparisons, and citation impact without writing."""

from __future__ import annotations

from collections.abc import Sequence
from difflib import unified_diff
from typing import Any

from sqlalchemy import select

from tailorcv.application.config import Stage
from tailorcv.application.failures import NotFoundError
from tailorcv.application.master_cv.anchoring import Anchor, Drift, reanchor_many
from tailorcv.application.master_cv.artifacts import MasterCvArtifacts
from tailorcv.application.master_cv.cv import CvInventory, Entry, parse_cv
from tailorcv.application.master_cv.intake import canonical_problem
from tailorcv.application.workspaces import (
    MasterCvContentResponse,
    MasterCvImpactResponse,
    MasterCvRereadEstimateResponse,
    MasterCvVersionResponse,
    layout_for,
)
from tailorcv.application.workspaces.spend import recorded_call_cost
from tailorcv.domain.statements import Known, Stated, carried_identities
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    CandidateProfile,
    Employer,
    JobEvaluation,
    JobPosting,
    MasterCvEntry,
    MasterCvVersion,
    MatchEvidence,
    ModelCall,
)
from tailorcv.shared.errors import CvParseError

IMPACT_CITATION_LIMIT = 50
REREAD_ORIGIN = "master_cv.import"


def _citation_order(citation: dict[str, Any]) -> tuple[int, str, str, str]:
    """Sort impact citations by materiality, role, and source location."""
    drift = citation["drift"]
    tier = 0 if drift["material"] else 1 if drift["changed"] else 2
    return tier, str(citation["employer"]), str(citation["title"]), str(citation["quote"])


def _stated_entries(entries: Sequence[Entry]) -> list[Stated]:
    """Return parsed entries as statements for an impact identity check."""
    return [Stated(id=entry.id, scope=entry.scope, text=entry.text) for entry in entries]


def _known_entry(row: MasterCvEntry) -> Known:
    """Project a stored entry for an impact identity check."""
    return Known(
        id=row.entry_id, key=row.entry_key, scope=row.role_key or row.section, text=row.text
    )


class MasterCvQueries:
    """Own master-CV reads and dry-run citation impact reports."""

    def __init__(
        self,
        database: Database,
        storage: RuntimeStorage,
        artifacts: MasterCvArtifacts,
    ) -> None:
        """Configure version reads and verified snapshot access."""
        self.database = database
        self.storage = storage
        self.artifacts = artifacts

    def versions(self, workspace_id: str) -> list[MasterCvVersionResponse]:
        """List every stored master-CV version, newest first."""
        with self.database.session() as session:
            rows = self._versions(session, workspace_id)
            return [self._describe(item, index == 0) for index, item in enumerate(rows)]

    def read_version(self, workspace_id: str, version_id: str = "") -> MasterCvContentResponse:
        """Return one stored version with its text; an empty id selects the current version."""
        with self.database.session() as session:
            rows = self._versions(session, workspace_id)
            if not rows:
                raise NotFoundError("No master CV has been imported into this workspace.")
            chosen = (
                rows[0]
                if not version_id
                else next((item for item in rows if item.id == version_id), None)
            )
            if chosen is None:
                raise NotFoundError(f"Master CV version {version_id!r} does not exist.")
            described = self._describe(chosen, chosen.id == rows[0].id)
            return MasterCvContentResponse(
                **described.model_dump(),
                content=self.artifacts.content(session, workspace_id, chosen.id),
            )

    def diff_versions(self, workspace_id: str, from_version: str, to_version: str) -> str:
        """Return a unified diff between two stored versions."""
        left = self.read_version(workspace_id, from_version)
        right = self.read_version(workspace_id, to_version)
        return "".join(
            unified_diff(
                left.content.splitlines(keepends=True),
                right.content.splitlines(keepends=True),
                fromfile=f"version {left.content_sha256[:12]}",
                tofile=f"version {right.content_sha256[:12]}",
            )
        )

    def _carried_keys(self, version_id: str, proposed: CvInventory | None) -> set[str]:
        """Return the identities the proposed document still carries.

        An empty set where the proposal does not parse, so a citation reports no
        entry answer rather than a wrong one.
        """
        if proposed is None:
            return set()
        with self.database.session() as session:
            stored = [
                _known_entry(row)
                for row in session.scalars(
                    select(MasterCvEntry).where(MasterCvEntry.master_cv_version_id == version_id)
                )
            ]
        return set(carried_identities(stored, _stated_entries(proposed.entries)).values())

    def _parsed(self, workspace_id: str, content: str) -> CvInventory | None:
        """Parse a proposed document, or return None where it does not parse."""
        layout = layout_for(workspace_id, storage=self.storage)
        try:
            return parse_cv(layout.master_cv, text=content)
        except CvParseError:
            return None

    def reread_estimate(self, workspace_id: str) -> MasterCvRereadEstimateResponse:
        """Average the recorded cost of this workspace's past model re-reads of a CV."""
        with self.database.session() as session:
            rereads, _calls_unpriced, _total = recorded_call_cost(
                session,
                ModelCall.workspace_id == workspace_id,
                ModelCall.origin == REREAD_ORIGIN,
                ModelCall.stage == Stage.STRUCTURE.value,
            )
            _count, _unpriced, total = recorded_call_cost(
                session,
                ModelCall.workspace_id == workspace_id,
                ModelCall.origin == REREAD_ORIGIN,
            )
        average = float(total / rereads) if rereads and total is not None else None
        return MasterCvRereadEstimateResponse(past_rereads=rereads, average_usd=average)

    def impact(self, workspace_id: str, content: str) -> MasterCvImpactResponse:
        """Report which committed candidate citations an edit would contradict."""
        current = self.read_version(workspace_id)
        old_text = current.content
        with self.database.session() as session:
            rows = session.execute(
                select(
                    MatchEvidence.evaluation_id,
                    MatchEvidence.evidence_text,
                    MatchEvidence.char_start,
                    MatchEvidence.char_end,
                    MatchEvidence.classification,
                    MatchEvidence.master_cv_entry_key,
                    JobEvaluation.master_cv_version_id,
                    JobPosting.id,
                    JobPosting.title,
                    Employer.name,
                )
                .join(JobEvaluation, JobEvaluation.id == MatchEvidence.evaluation_id)
                .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
                .join(Employer, Employer.id == JobPosting.employer_id)
                .where(
                    JobPosting.workspace_id == workspace_id,
                    MatchEvidence.source_kind == "candidate",
                )
            ).all()
        proposed = self._parsed(workspace_id, content)
        carried = self._carried_keys(current.id, proposed)
        drifts = self._edit_drifts(workspace_id, old_text, content, rows)
        citations: list[dict[str, Any]] = []
        for row, drift in zip(rows, drifts, strict=True):
            (
                evaluation_id,
                quote,
                start,
                end,
                classification,
                key,
                _version_id,
                job_id,
                title,
                employer,
            ) = row
            citations.append(
                {
                    "evaluation_id": evaluation_id,
                    "job_posting_id": job_id,
                    "employer": employer,
                    "title": title,
                    "quote": quote,
                    "char_start": start,
                    "char_end": end,
                    "classification": classification,
                    "entry_carried": key in carried if key else None,
                    "drift": {
                        "found": drift.found,
                        "text": drift.text,
                        "changed": drift.changed,
                        "material": drift.material,
                        "reason": drift.reason,
                    },
                }
            )
        citations.sort(key=_citation_order)
        material = [item for item in citations if item["drift"]["material"]]
        gone = [item for item in citations if not item["drift"]["found"]]
        changed = [
            item for item in citations if item["drift"]["changed"] and not item["drift"]["material"]
        ]
        return MasterCvImpactResponse.model_validate(
            {
                "workspace_id": workspace_id,
                "master_cv_version_id": current.id,
                "content_sha256": current.content_sha256,
                "identical": content == old_text,
                "citation_count": len(citations),
                "material_count": len(material),
                "changed_count": len(changed),
                "unchanged_count": len(citations) - len(changed) - len(material) - len(gone),
                "not_found_count": len(gone),
                "orphaned_count": sum(item["entry_carried"] is False for item in citations),
                "returned_count": min(len(citations), IMPACT_CITATION_LIMIT),
                "omitted_count": max(len(citations) - IMPACT_CITATION_LIMIT, 0),
                "citations": citations[:IMPACT_CITATION_LIMIT],
                "reread_reason": canonical_problem(proposed) if proposed is not None else None,
            }
        )

    def _edit_drifts(
        self,
        workspace_id: str,
        current_text: str,
        proposed_text: str,
        rows: Sequence[Any],
    ) -> list[Drift]:
        grouped: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            grouped.setdefault(row[6], []).append(index)
        drifts: list[Drift | None] = [None] * len(rows)
        for version_id, indices in grouped.items():
            source_text = self.read_version(workspace_id, version_id).content
            anchors = [
                Anchor(quote=rows[index][1], char_start=rows[index][2], char_end=rows[index][3])
                for index in indices
            ]
            before = reanchor_many(source_text, current_text, anchors)
            after = reanchor_many(source_text, proposed_text, anchors)
            for index, anchor, existing, proposed in zip(
                indices, anchors, before, after, strict=True
            ):
                drifts[index] = (
                    Drift(
                        found=True,
                        text=proposed.text or anchor.quote,
                        changed=False,
                        material=False,
                        reason="The proposed edit leaves this citation as it was.",
                    )
                    if existing == proposed
                    else proposed
                )
        return [drift for drift in drifts if drift is not None]

    @staticmethod
    def _versions(session: Any, workspace_id: str) -> list[MasterCvVersion]:
        profile = session.scalar(
            select(CandidateProfile).where(CandidateProfile.workspace_id == workspace_id)
        )
        if profile is None:
            return []
        return list(
            session.scalars(
                select(MasterCvVersion)
                .where(MasterCvVersion.candidate_profile_id == profile.id)
                .order_by(MasterCvVersion.effective_at.desc(), MasterCvVersion.id.desc())
            )
        )

    @staticmethod
    def _describe(version: MasterCvVersion, is_current: bool) -> MasterCvVersionResponse:
        return MasterCvVersionResponse(
            id=version.id,
            content_sha256=version.content_sha256,
            effective_at=version.effective_at,
            byte_size=version.byte_size,
            source_uri=version.source_uri,
            is_current=is_current,
        )
