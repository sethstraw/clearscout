"""Results and schemas shared by the pipeline entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from clearscout.application.config import Settings
from clearscout.application.master_cv.cv import CvInventory, EntryKind
from clearscout.application.tailoring import fit
from clearscout.application.tailoring.layout import wrapped_lines
from clearscout.application.tailoring.runs import MaterializedRun
from clearscout.application.tailoring.stages import draft as draft_stage
from clearscout.application.tailoring.stages import match as match_stage
from clearscout.application.tailoring.stages import verify as verify_stage
from clearscout.application.tailoring.stages.jobspec import JobSpec, Requirement
from clearscout.application.tailoring.stages.select import Plan, TopUpStop
from clearscout.domain import ArtifactLogicalType
from clearscout.infrastructure.web_fetch.ingest import JDText
from clearscout.shared.costs import Ledger


class VisionTranscript(BaseModel):
    """Verbatim document text read out of one or more images."""

    model_config = ConfigDict(extra="forbid")

    text: str


@dataclass(frozen=True)
class StoredMasterCv:
    """The workspace's record of the master CV a run read from a temporary copy."""

    version_id: str
    file_name: str

    def describe(self) -> str:
        """Return how a report names this master CV."""
        return ", ".join(part for part in (f"version {self.version_id}", self.file_name) if part)


@dataclass
class ScreenResult:
    """What screening one posting found, without drafting anything."""

    spec: JobSpec
    evidence: match_stage.EvidenceMap
    inventory: CvInventory
    ledger: Ledger
    jd: JDText
    reading: fit.Reading
    report: Path | None = None


class RoleSelection(BaseModel):
    """One role's first share of the selection budget and what the plan gave it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role_key: str
    earlier: bool = False
    allowance: int = 0
    context_ids: tuple[str, ...] = ()
    bullet_ids: tuple[str, ...] = ()
    bullet_lines: int = 0


class CitedEntry(BaseModel):
    """One master-CV entry the match cited, what it scored, and whether it was selected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: EntryKind
    role_key: str | None = None
    lines: int
    score: float
    requirement_ids: tuple[str, ...] = ()
    selected: bool


class PageSelection(BaseModel):
    """What selection chose for the page, and how the drafted document was fitted to it.

    ``lines`` are master-text lines, the unit selection spends; ``drafted_lines``
    estimates the written document block by block.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pages: int
    page_lines: float
    fixed_lines: dict[str, float]
    selection_lines: int
    spent_lines: int
    # spent_lines + unspent_lines - overshoot_lines == selection_lines.
    unspent_lines: int
    overshoot_lines: int = 0
    max_bullets_per_role: int
    # The settings the drop order's floors and the recent-role split read.
    min_bullets_per_role: int
    min_publications: int
    full_bullet_years: int
    withheld_by_cap: tuple[str, ...] = ()
    passed_cap: int
    top_up_stop: TopUpStop | None = None
    roles: tuple[RoleSelection, ...] = ()
    summary_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    education_ids: tuple[str, ...] = ()
    publication_ids: tuple[str, ...] = ()
    must_consider_publications: tuple[str, ...] = ()
    unoffered_publications: tuple[str, ...] = ()
    stale_must_consider: tuple[str, ...] = ()
    entries: tuple[CitedEntry, ...] = ()
    drop_order: tuple[str, ...] = ()
    # The dropped entries the page no longer shows, in drop order.
    excluded: tuple[str, ...] = ()
    trimmed_to_fit: tuple[str, ...] = ()
    trimmed_on_request: tuple[str, ...] = ()
    drafted_lines: dict[str, float]
    overflow_lines: float = 0.0
    requirements: tuple[Requirement, ...] = ()
    matches: tuple[match_stage.Match, ...] = ()

    @classmethod
    def of(
        cls,
        *,
        plan: Plan,
        inventory: CvInventory,
        spec: JobSpec,
        evidence: match_stage.EvidenceMap,
        fitted: draft_stage.Fitted,
        settings: Settings,
    ) -> PageSelection:
        """Return the record of one run's selection and fit."""
        offered = set(plan.all_ids())
        lines = {
            entry_id: wrapped_lines(inventory.by_id(entry_id).full_text) for entry_id in plan.scores
        }
        return cls(
            pages=settings.pages,
            page_lines=plan.page_lines,
            fixed_lines=plan.fixed_lines,
            selection_lines=plan.selection_lines,
            spent_lines=plan.spent_lines,
            unspent_lines=plan.unspent_lines,
            overshoot_lines=plan.overshoot_lines,
            max_bullets_per_role=settings.max_bullets_per_role,
            min_bullets_per_role=settings.min_bullets_per_role,
            min_publications=settings.min_publications,
            full_bullet_years=settings.full_bullet_years,
            withheld_by_cap=plan.withheld_by_cap,
            passed_cap=plan.passed_cap,
            top_up_stop=plan.top_up_stop,
            roles=tuple(
                RoleSelection(
                    role_key=role_key,
                    earlier=role_key in plan.earlier_roles,
                    allowance=plan.allowances.get(role_key, 0),
                    context_ids=plan.role_context.get(role_key, ()),
                    bullet_ids=plan.role_bullets.get(role_key, ()),
                    bullet_lines=sum(
                        lines.get(entry_id, 0) for entry_id in plan.role_bullets.get(role_key, ())
                    ),
                )
                for role_key in (*plan.recent_roles, *plan.earlier_roles)
            ),
            summary_ids=plan.summary_ids,
            skill_ids=plan.skill_ids,
            education_ids=plan.education_ids,
            publication_ids=plan.publication_ids,
            must_consider_publications=settings.must_consider_publications,
            unoffered_publications=plan.unoffered_publications,
            stale_must_consider=plan.stale_must_consider,
            entries=tuple(
                CitedEntry(
                    id=entry_id,
                    kind=inventory.by_id(entry_id).kind,
                    role_key=inventory.by_id(entry_id).role_key,
                    lines=lines[entry_id],
                    score=score,
                    requirement_ids=plan.requirements_for.get(entry_id, ()),
                    selected=entry_id in offered,
                )
                for entry_id, score in sorted(plan.scores.items(), key=lambda item: -item[1])
            ),
            drop_order=plan.drop_order,
            excluded=tuple(entry_id for entry_id in plan.drop_order if entry_id in fitted.off_page),
            trimmed_to_fit=fitted.trimmed,
            trimmed_on_request=fitted.requested,
            drafted_lines=fitted.lines,
            overflow_lines=fitted.overflow,
            requirements=spec.requirements,
            matches=evidence.matches,
        )


@dataclass(frozen=True)
class SerializedRunArtifacts:
    """Exact serialized package plus the JSON facts its consumers query."""

    document: bytes
    report: bytes
    manifest_json: bytes
    manifest: dict[str, Any]
    cover_letter: bytes | None
    review_json: bytes | None
    review_markdown: bytes | None
    review: dict[str, Any]

    def artifact_items(
        self,
    ) -> tuple[tuple[ArtifactLogicalType, str, bytes], ...]:
        """Return every durable artifact with its closed type and media type."""
        items: list[tuple[ArtifactLogicalType, str, bytes]] = [
            ("resume.markdown", "text/markdown", self.document),
            ("tailoring.report.markdown", "text/markdown", self.report),
            ("tailoring.manifest.json", "application/json", self.manifest_json),
        ]
        if self.cover_letter is not None:
            items.append(("cover_letter.markdown", "text/markdown", self.cover_letter))
        if self.review_json is not None:
            items.append(("tailoring.review.json", "application/json", self.review_json))
        if self.review_markdown is not None:
            items.append(("tailoring.review.markdown", "text/markdown", self.review_markdown))
        return tuple(items)


@dataclass
class RunResult:
    """Everything one run produced, with optional CLI materialization."""

    verification: verify_stage.Report
    spec: JobSpec
    ledger: Ledger
    attempts: int
    trimmed: int
    serialized: SerializedRunArtifacts
    materialized: MaterializedRun | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reused: bool = False
    reading: fit.Reading | None = None
