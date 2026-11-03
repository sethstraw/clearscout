"""Read a guided campaign's preview and report for the operator."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clearscout.application.config import BACKGROUND_WEB_SEARCH_PROVIDERS, Settings, Stage
from clearscout.application.discovery.derived_vocabulary_rules import searched_titles
from clearscout.application.discovery.guided_campaign import (
    CampaignBounds,
    CampaignIdentities,
    CampaignManifest,
    compile_campaign,
)
from clearscout.application.discovery.guided_campaign_admission import CAPTURE_ACTION_KINDS
from clearscout.application.discovery.guided_campaign_ledger import (
    GuidedCampaignLedger,
    stranded_attempt_count,
    supersedable_campaign_in_session,
)
from clearscout.application.discovery.guided_campaign_request import RESPONSE_SCHEMA_VERSION
from clearscout.application.discovery.proposals import (
    CarriedRejections,
    accepted_archetypes_in,
    accepted_clusters,
    accepted_titles,
    carried_rejections_in,
)
from clearscout.application.discovery.search_parameter_summary import accepted_proposals_block
from clearscout.application.discovery.search_planning_queries import known_identities_in
from clearscout.application.failures import NotFoundError, ValidationError
from clearscout.application.master_cv.versioning import current_version_id
from clearscout.application.prompts import data_block, flatten
from clearscout.application.workspaces.settings import settings_seed_in
from clearscout.application.workspaces.spend import per_unit_costs
from clearscout.domain.campaigns import (
    AttemptErrorCategory,
    AttemptState,
    BranchOutcome,
    CampaignReason,
    CampaignStatus,
    CapturedUrlOutcome,
    StagedBasis,
)
from clearscout.domain.records import ProposalRejectionReason
from clearscout.domain.search_parameters import EmployerArchetype, JobSearchParameters, TargetTitle
from clearscout.infrastructure.persistence.models import (
    CostRecord,
    GuidedCampaign,
    GuidedCampaignAdmittedDiscovery,
    GuidedCampaignAttempt,
    GuidedCampaignBranch,
    GuidedCampaignBranchOutcome,
    GuidedCampaignCapturedUrl,
    GuidedCampaignState,
    GuidedCampaignUrlObservation,
    JobSearchParameterSet,
    MasterCvVersion,
    PendingDiscovery,
    Source,
)
from clearscout.infrastructure.web_fetch.boards import canonicalise_source_url
from clearscout.shared.costs import cents
from clearscout.shared.costs.prices import priced_model
from clearscout.shared.costs.prices import table as price_table

# Whether an estimate was measured over the provider and model this campaign
# would call, or over every campaign this workspace has paid for.
EstimateBasis = Literal["provider_model", "workspace"]


@dataclass(frozen=True, slots=True)
class CampaignPreview:
    """What a confirmation would run, with each branch's prior outcome."""

    manifest: CampaignManifest
    cycle: int
    prior_outcomes: dict[str, BranchOutcome]
    last_searched_at: dict[str, datetime]
    provider: str
    model: str
    supersedes_campaign_id: str | None
    planned_search_fee_cents: int | None
    estimated_cost_cents: int | None
    estimate_basis: EstimateBasis | None


@dataclass(frozen=True, slots=True)
class CampaignFinding:
    """A newly staged posting, described by publisher facts."""

    captured_url_id: str
    url: str
    staged_basis: StagedBasis
    pending_discovery_id: str
    employer: str
    title: str
    location: str | None
    branch_labels: tuple[str, ...]
    model_why: str | None


@dataclass(frozen=True, slots=True)
class CampaignAdmittedPosting:
    """One posting a board admission staged from its listings."""

    pending_discovery_id: str
    url: str
    employer: str
    title: str
    location: str | None


@dataclass(frozen=True, slots=True)
class CampaignBoardAdmission:
    """A board the campaign admitted; a registered one stays disabled until someone enables it."""

    url: str
    source_id: str | None
    employer: str
    branch_labels: tuple[str, ...]
    admitted_postings: tuple[CampaignAdmittedPosting, ...]


@dataclass(frozen=True, slots=True)
class CampaignNotStagedUrl:
    """A captured URL a refusal or a gap kept out of staging, kept visible and recoverable."""

    url: str
    outcome: CapturedUrlOutcome
    detail: str | None


@dataclass(frozen=True, slots=True)
class CampaignBranchRow:
    """Coverage for one branch."""

    position: int
    label: str
    priority_family: str
    protected: bool
    attempt_position: int | None
    outcome: BranchOutcome
    matched_queries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CampaignAttemptRow:
    """Execution and spend for one grouped provider call."""

    position: int
    state: AttemptState
    hosted_search_count: int
    page_action_count: int
    finish: str | None
    # Null until this attempt is processed; a live count before then would be misleading.
    captured_not_selected: int | None
    cost_cents: int | None
    error_category: AttemptErrorCategory | None


@dataclass(frozen=True, slots=True)
class CampaignReport:
    """Everything one campaign found, spent, and left unsettled."""

    campaign_id: str
    manifest_sha256: str
    cycle: int
    status: CampaignStatus
    max_model_calls: int
    max_hosted_searches: int
    model_calls_used: int
    hosted_searches_used: int
    # None when a call was recorded without a price, so a partial sum never reads as the total.
    cost_cents: int | None
    known_posting_count: int
    registered_board_count: int
    findings: tuple[CampaignFinding, ...]
    boards_admitted: tuple[CampaignBoardAdmission, ...]
    not_staged: tuple[CampaignNotStagedUrl, ...]
    branches: tuple[CampaignBranchRow, ...]
    attempts: tuple[CampaignAttemptRow, ...]
    warnings: tuple[CampaignReason, ...]
    # Counted across the workspace: a superseded campaign is no longer the latest report.
    stranded_processing_attempts: int


def _master_cv_version_in(session: Session, workspace_id: str) -> MasterCvVersion:
    """Read the current master-CV version, inside the caller's transaction."""
    version_id = current_version_id(session, workspace_id)
    version = session.get(MasterCvVersion, version_id) if version_id is not None else None
    if version is None:
        raise NotFoundError("No master CV has been imported into this workspace.")
    return version


def _identities_in(
    session: Session,
    workspace_id: str,
    settings: Settings,
    master_cv: MasterCvVersion,
    *,
    accepted_vocabulary_sha256: str,
    admission_scope_sha256: str,
) -> CampaignIdentities:
    """Build the identity block a manifest's digest is taken over."""
    from clearscout.application.prompts import digest as prompt_digest
    from clearscout.application.prompts import load, without_markers

    parameter_set = session.scalar(
        select(JobSearchParameterSet)
        .where(JobSearchParameterSet.workspace_id == workspace_id)
        .order_by(JobSearchParameterSet.version.desc())
        .limit(1)
    )
    choice = settings.choice_for_stage(Stage.GUIDED_CAMPAIGN, settings.unattended_tier())
    return CampaignIdentities(
        search_parameter_set_id=parameter_set.id if parameter_set is not None else "",
        search_parameter_sha256=parameter_set.content_sha256 if parameter_set is not None else "",
        master_cv_version_id=master_cv.id,
        master_cv_sha256=master_cv.content_sha256,
        accepted_vocabulary_sha256=accepted_vocabulary_sha256,
        admission_scope_sha256=admission_scope_sha256,
        prompt_digests={
            name: prompt_digest(without_markers(load(name)))
            for name in ("guided_campaign_system", "guided_campaign_user")
        },
        response_schema_version=RESPONSE_SCHEMA_VERSION,
        provider=settings.provider.value,
        model=choice.model,
        effort=choice.effort.value if choice.effort is not None else "",
        verbosity=choice.verbosity.value if choice.verbosity is not None else "",
        service_tier=choice.service_tier.value,
    )


@dataclass(frozen=True, slots=True)
class CampaignCompilation:
    """The manifest plus every widened input it was compiled from."""

    manifest: CampaignManifest
    settings: Settings
    parameters: JobSearchParameters
    titles: tuple[TargetTitle, ...]
    archetypes: tuple[EmployerArchetype, ...]
    master_cv: MasterCvVersion
    ruled_out_block: str
    known_source_roots: frozenset[str]
    known_posting_fingerprints: frozenset[str]


def _digest(value: object) -> str:
    """Return a stable digest for immutable campaign identity material."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_SOFT_REJECTIONS = frozenset({ProposalRejectionReason.DUPLICATE, ProposalRejectionReason.DEFER})


def _ruled_out_block(carried: CarriedRejections, cap: int) -> str:
    """Render up to ``cap`` hard-rejected titles and kinds of organisation, as a data block.

    Soft rejections are dropped before the cap, so they never displace a hard one.
    A rejected cluster is left out: its name is an internal label, not search vocabulary.
    """
    lines = []
    for label, entries in (
        ("Titles", carried.titles),
        ("Kinds of organisation", carried.archetypes),
    ):
        names = [
            flatten(entry.name)
            for entry in entries
            if entry.reason not in _SOFT_REJECTIONS and flatten(entry.name)
        ][:cap]
        if names:
            lines.append(f"{label}: {'; '.join(names)}")
    return data_block("ruled_out_search_vocabulary", "\n".join(lines)) if lines else ""


def compile_current_campaign(
    session: Session, workspace_id: str, *, max_model_calls: int, max_hosted_searches: int
) -> CampaignCompilation:
    """Compile the campaign the current search scope and chosen bounds would approve."""
    settings = settings_seed_in(session, workspace_id)
    if settings.provider not in BACKGROUND_WEB_SEARCH_PROVIDERS:
        raise ValidationError(
            f"A guided web campaign needs background web search, which "
            f"{settings.provider.value.title()} does not offer. Switch this workspace to "
            "OpenAI to run one."
        )
    master_cv = _master_cv_version_in(session, workspace_id)
    accepted_block = accepted_proposals_block(session, workspace_id, master_cv.id)
    ruled_out = _ruled_out_block(
        carried_rejections_in(session, workspace_id, cap=sys.maxsize),
        settings.discovery.max_carried_rejections,
    )
    known = known_identities_in(session, workspace_id)
    identities = _identities_in(
        session,
        workspace_id,
        settings,
        master_cv,
        accepted_vocabulary_sha256=_digest({"accepted": accepted_block, "ruled_out": ruled_out}),
        admission_scope_sha256=_digest(
            {
                "source_roots": sorted(known.source_roots),
                "posting_fingerprints": sorted(known.posting_fingerprints),
            }
        ),
    )
    widened_focus = settings.job_search.focus.plus(accepted_clusters(session, workspace_id))
    parameters = settings.job_search.model_copy(update={"focus": widened_focus})
    titles = searched_titles(parameters, accepted_titles(session, workspace_id), ())
    archetypes = accepted_archetypes_in(session, workspace_id, master_cv.id)
    bounds = CampaignBounds(
        max_model_calls=max_model_calls, max_hosted_searches=max_hosted_searches
    )
    manifest = compile_campaign(
        parameters,
        list(titles),
        list(archetypes),
        identities=identities,
        bounds=bounds,
        searched_branch_keys=frozenset(_last_searched_at(session, workspace_id)),
    )
    return CampaignCompilation(
        manifest=manifest,
        settings=settings,
        parameters=parameters,
        titles=tuple(titles),
        archetypes=archetypes,
        master_cv=master_cv,
        ruled_out_block=ruled_out,
        known_source_roots=known.source_roots,
        known_posting_fingerprints=known.posting_fingerprints,
    )


def _prior_outcomes(
    session: Session, workspace_id: str, branch_keys: tuple[str, ...]
) -> dict[str, BranchOutcome]:
    """Return each branch's most recent outcome from any earlier campaign in this workspace."""
    if not branch_keys:
        return {}
    rows = session.execute(
        select(GuidedCampaignBranch.branch_key, GuidedCampaignBranchOutcome.outcome)
        .join(
            GuidedCampaignBranchOutcome,
            GuidedCampaignBranchOutcome.branch_id == GuidedCampaignBranch.id,
        )
        .join(GuidedCampaign, GuidedCampaign.id == GuidedCampaignBranch.campaign_id)
        .where(
            GuidedCampaign.workspace_id == workspace_id,
            GuidedCampaignBranch.branch_key.in_(branch_keys),
        )
        .order_by(GuidedCampaignBranchOutcome.created_at)
    ).all()
    return {key: BranchOutcome(outcome) for key, outcome in rows}


def _last_searched_at(session: Session, workspace_id: str) -> dict[str, datetime]:
    """Return when each branch last had captured search evidence in an earlier campaign."""
    rows = session.execute(
        select(GuidedCampaignBranch.branch_key, func.max(GuidedCampaignBranchOutcome.created_at))
        .join(
            GuidedCampaignBranchOutcome,
            GuidedCampaignBranchOutcome.branch_id == GuidedCampaignBranch.id,
        )
        .join(
            GuidedCampaign,
            GuidedCampaign.id == GuidedCampaignBranch.campaign_id,
        )
        .where(
            GuidedCampaign.workspace_id == workspace_id,
            GuidedCampaignBranchOutcome.outcome == BranchOutcome.SEARCHED.value,
        )
        .group_by(GuidedCampaignBranch.branch_key)
    )
    return dict(rows.all())


def _planned_search_fee_cents(
    settings: Settings, model: str, max_hosted_searches: int
) -> int | None:
    """Return the hosted-search fee for the planned search count, or None when unpriced."""
    known = price_table(settings.prices)
    key = priced_model(known, model)
    price = known.get(key) if key is not None else None
    if price is None or price.search_per_thousand_calls is None:
        return None
    return cents(Decimal(str(price.search_per_thousand_calls)) * max_hosted_searches / 1000)


def _estimated_cost_cents(
    session: Session,
    workspace_id: str,
    *,
    attempt_count: int,
    provider: str,
    model: str,
) -> tuple[int | None, EstimateBasis | None]:
    """Estimate from the median cost per attempt of completed campaigns, and say over which.

    Campaigns on the provider and model this one would call price it best; every
    completed campaign is the fallback when that narrower sample is empty.
    """
    matched = per_unit_costs(session, workspace_id, provider=provider, model=model)
    per_attempt = matched["search.guided_campaign"].median
    basis: EstimateBasis = "provider_model"
    if per_attempt is None:
        per_attempt = per_unit_costs(session, workspace_id)["search.guided_campaign"].median
        basis = "workspace"
    if per_attempt is None:
        return None, None
    return cents(per_attempt * attempt_count), basis


def build_campaign_preview(
    session: Session,
    workspace_id: str,
    *,
    max_model_calls: int,
    max_hosted_searches: int,
) -> CampaignPreview:
    """Compile the campaign the current inputs and bounds would approve."""
    compilation = compile_current_campaign(
        session,
        workspace_id,
        max_model_calls=max_model_calls,
        max_hosted_searches=max_hosted_searches,
    )
    manifest = compilation.manifest
    supersedable = supersedable_campaign_in_session(session, workspace_id)
    if supersedable is not None:
        cycle = supersedable.cycle + 1
        supersedes_campaign_id: str | None = supersedable.id
    else:
        # The same ordering the report uses, so both agree on which campaign is latest.
        latest = session.scalar(
            select(GuidedCampaign)
            .where(GuidedCampaign.workspace_id == workspace_id)
            .order_by(GuidedCampaign.created_at.desc())
            .limit(1)
        )
        cycle = 1 if latest is None else latest.cycle
        supersedes_campaign_id = None
        if latest is not None:
            state = session.scalar(
                select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == latest.id)
            )
            if state is not None and state.status in (
                CampaignStatus.COMPLETED,
                CampaignStatus.INCOMPLETE,
            ):
                cycle = latest.cycle + 1
    estimated_cost_cents, estimate_basis = _estimated_cost_cents(
        session,
        workspace_id,
        attempt_count=len(manifest.attempts),
        provider=manifest.identities.provider,
        model=manifest.identities.model,
    )
    return CampaignPreview(
        manifest=manifest,
        cycle=cycle,
        prior_outcomes=_prior_outcomes(
            session, workspace_id, tuple(branch.key for branch in manifest.branches)
        ),
        last_searched_at=_last_searched_at(session, workspace_id),
        provider=manifest.identities.provider,
        model=manifest.identities.model,
        supersedes_campaign_id=supersedes_campaign_id,
        planned_search_fee_cents=_planned_search_fee_cents(
            compilation.settings, manifest.identities.model, manifest.bounds.max_hosted_searches
        ),
        estimated_cost_cents=estimated_cost_cents,
        estimate_basis=estimate_basis,
    )


def _cost_cents(session: Session, model_call_id: str | None) -> int | None:
    """Return one attempt's recorded cost in integer cents, or None when unpriced."""
    if model_call_id is None:
        return None
    amount = session.scalar(
        select(CostRecord.amount).where(CostRecord.model_call_id == model_call_id)
    )
    if amount is None:
        return None
    return cents(Decimal(amount))


def _credited_labels(
    branch_rows: Sequence[GuidedCampaignBranch],
    outcomes_by_branch: dict[str, GuidedCampaignBranchOutcome],
    attempt_position_by_id: dict[str, int],
) -> dict[tuple[int, str], tuple[str, ...]]:
    """Map each (attempt position, captured query) to the labels of the branches it credited.

    Read from the outcomes the runner stored, so a label names only a branch the runner credited.
    """
    credited: dict[tuple[int, str], list[str]] = {}
    for row in branch_rows:
        outcome = outcomes_by_branch.get(row.id)
        position = (
            attempt_position_by_id.get(outcome.attempt_id)
            if outcome is not None and outcome.attempt_id is not None
            else None
        )
        if outcome is None or position is None:
            continue
        for query in dict.fromkeys(outcome.matched_queries):
            credited.setdefault((position, query), []).append(row.document.get("label", ""))
    return {pair: tuple(labels) for pair, labels in credited.items()}


def _index_observations(
    observations: Sequence[GuidedCampaignUrlObservation],
    attempt_position_by_id: dict[str, int],
    captured_by_id: dict[str, GuidedCampaignCapturedUrl],
    selected_by_attempt: dict[str, frozenset[str]],
) -> tuple[dict[str, list[tuple[int, str]]], dict[str, set[str]]]:
    """Group each captured URL's queries by attempt.

    Flags ids an attempt saw but did not pick.
    """
    observation_queries: dict[str, list[tuple[int, str]]] = {}
    captured_not_selected: dict[str, set[str]] = {}
    for observation in observations:
        attempt_position = attempt_position_by_id.get(observation.attempt_id)
        if attempt_position is not None:
            observation_queries.setdefault(observation.captured_url_id, []).append(
                (attempt_position, observation.query)
            )
        captured = captured_by_id[observation.captured_url_id]
        if (
            observation.action_kind in CAPTURE_ACTION_KINDS
            and captured.classification in ("ats_detail", "job_page_candidate")
            and captured.url_sha256 not in selected_by_attempt.get(observation.attempt_id, ())
        ):
            captured_not_selected.setdefault(observation.attempt_id, set()).add(captured.id)
    return observation_queries, captured_not_selected


def _captured_url_labels(
    observation_queries: dict[str, list[tuple[int, str]]],
    credited: dict[tuple[int, str], tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Return each captured URL's deduplicated branch labels from the queries that surfaced it."""
    return {
        captured_id: tuple(
            dict.fromkeys(label for pair in pairs for label in credited.get(pair, ()))
        )
        for captured_id, pairs in observation_queries.items()
    }


def _admitted_postings_in(
    session: Session, captured_url_ids: Sequence[str]
) -> dict[str, tuple[CampaignAdmittedPosting, ...]]:
    """Return each board admission's staged postings, keyed by its captured URL id."""
    if not captured_url_ids:
        return {}
    rows = session.execute(
        select(GuidedCampaignAdmittedDiscovery.captured_url_id, PendingDiscovery)
        .join(
            PendingDiscovery,
            PendingDiscovery.id == GuidedCampaignAdmittedDiscovery.pending_discovery_id,
        )
        .where(GuidedCampaignAdmittedDiscovery.captured_url_id.in_(captured_url_ids))
        .order_by(PendingDiscovery.employer, PendingDiscovery.title, PendingDiscovery.discovery_url)
    ).all()
    grouped: dict[str, list[CampaignAdmittedPosting]] = {}
    for captured_url_id, pending in rows:
        grouped.setdefault(captured_url_id, []).append(
            CampaignAdmittedPosting(
                pending_discovery_id=pending.id,
                url=pending.discovery_url,
                employer=pending.employer,
                title=pending.title,
                location=pending.location or None,
            )
        )
    return {captured_url_id: tuple(postings) for captured_url_id, postings in grouped.items()}


def _board_admission(row: GuidedCampaignCapturedUrl) -> bool:
    """Return whether a captured URL admitted a board, registered as a source or not."""
    return row.source_id is not None or row.outcome == CapturedUrlOutcome.BOARD_ADMITTED


def _classify_captured_rows(
    session: Session,
    captured_rows: Sequence[GuidedCampaignCapturedUrl],
    source_names: dict[str, str],
    labels_by_captured_id: dict[str, tuple[str, ...]],
    admitted_postings_by_captured_id: dict[str, tuple[CampaignAdmittedPosting, ...]],
) -> tuple[
    list[CampaignFinding], list[CampaignBoardAdmission], list[CampaignNotStagedUrl], int, int
]:
    """Sort captured URLs into findings, admitted boards, unstaged rows, and known-item counts."""
    findings: list[CampaignFinding] = []
    boards_admitted: list[CampaignBoardAdmission] = []
    not_staged: list[CampaignNotStagedUrl] = []
    known_posting_count = 0
    registered_board_count = 0
    for row in captured_rows:
        if _board_admission(row):
            boards_admitted.append(
                CampaignBoardAdmission(
                    url=canonicalise_source_url(row.url),
                    source_id=row.source_id,
                    employer=source_names.get(row.source_id or "", ""),
                    branch_labels=labels_by_captured_id.get(row.id, ()),
                    admitted_postings=admitted_postings_by_captured_id.get(row.id, ()),
                )
            )
        if row.outcome == CapturedUrlOutcome.KNOWN_POSTING:
            known_posting_count += 1
            continue
        if row.outcome == CapturedUrlOutcome.REGISTERED_BOARD:
            registered_board_count += 1
            continue
        if row.outcome == CapturedUrlOutcome.STAGED and row.pending_discovery_id is not None:
            pending = session.get(PendingDiscovery, row.pending_discovery_id)
            if pending is not None:
                findings.append(
                    CampaignFinding(
                        captured_url_id=row.id,
                        url=row.url,
                        staged_basis=StagedBasis(row.staged_basis)
                        if row.staged_basis
                        else StagedBasis.TITLE_EVIDENCE,
                        pending_discovery_id=pending.id,
                        employer=pending.employer,
                        title=pending.title,
                        location=pending.location or None,
                        branch_labels=labels_by_captured_id.get(row.id, ()),
                        model_why=row.model_why or None,
                    )
                )
            continue
        if row.outcome in (
            CapturedUrlOutcome.UNRESOLVED,
            CapturedUrlOutcome.UNREADABLE,
            CapturedUrlOutcome.POLICY_REFUSED,
            CapturedUrlOutcome.SCREENED_OUT,
            CapturedUrlOutcome.HELD_OUT,
        ):
            not_staged.append(
                CampaignNotStagedUrl(
                    url=row.url, outcome=CapturedUrlOutcome(row.outcome), detail=row.detail or None
                )
            )
    return findings, boards_admitted, not_staged, known_posting_count, registered_board_count


def _campaign_warnings(state: GuidedCampaignState) -> list[CampaignReason]:
    """Return the final campaign's stored reasons, or an uncertain-attempt warning while paused.

    A final campaign's reasons are read from the state the ledger wrote at
    finish time, rather than recomputed here, so the two never drift apart.
    """
    if state.status == CampaignStatus.PAUSED_UNCERTAIN:
        return [CampaignReason.UNCERTAIN_ATTEMPT]
    if state.status in (CampaignStatus.COMPLETED, CampaignStatus.INCOMPLETE):
        return [CampaignReason(value) for value in state.reasons]
    return []


def _branch_report_rows(
    branch_rows: Sequence[GuidedCampaignBranch],
    outcomes_by_branch: dict[str, GuidedCampaignBranchOutcome],
) -> tuple[CampaignBranchRow, ...]:
    """Project each branch's stored coverage into its report row."""
    return tuple(
        CampaignBranchRow(
            position=row.position,
            label=row.document.get("label", ""),
            priority_family=row.priority_family,
            protected=row.protected,
            attempt_position=row.attempt_position,
            outcome=BranchOutcome(outcomes_by_branch[row.id].outcome)
            if row.id in outcomes_by_branch
            else BranchOutcome.UNVISITED,
            matched_queries=tuple(outcomes_by_branch[row.id].matched_queries)
            if row.id in outcomes_by_branch
            else (),
        )
        for row in branch_rows
    )


def _attempt_report_rows(
    session: Session,
    attempt_rows: Sequence[GuidedCampaignAttempt],
    captured_not_selected: dict[str, set[str]],
) -> tuple[CampaignAttemptRow, ...]:
    """Project each attempt's stored execution and spend into its report row."""
    return tuple(
        CampaignAttemptRow(
            position=row.position,
            state=AttemptState(row.state),
            hosted_search_count=row.hosted_search_count,
            page_action_count=row.page_action_count,
            finish=row.finish or None,
            captured_not_selected=(
                len(captured_not_selected.get(row.id, set()))
                if row.state == AttemptState.PROCESSED
                else None
            ),
            cost_cents=_cost_cents(session, row.model_call_id),
            error_category=AttemptErrorCategory(row.error_category) if row.error_category else None,
        )
        for row in attempt_rows
    )


def _source_names_in(
    session: Session, captured_rows: Sequence[GuidedCampaignCapturedUrl]
) -> dict[str, str]:
    """Return admitted board source names keyed by source id."""
    return {
        row.id: row.name
        for row in session.scalars(
            select(Source).where(
                Source.id.in_([row.source_id for row in captured_rows if row.source_id is not None])
            )
        )
    }


def _total_cost_cents(
    session: Session, attempt_rows: Sequence[GuidedCampaignAttempt]
) -> int | None:
    """Sum every called attempt's recorded cost, or None when any call went unpriced."""
    costs = [
        _cost_cents(session, row.model_call_id)
        for row in attempt_rows
        if row.model_call_id is not None
    ]
    if any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)


def _campaign_report(session: Session, campaign: GuidedCampaign) -> CampaignReport:
    """Project one campaign's stored rows into its operator-facing report."""
    state = session.scalar(
        select(GuidedCampaignState).where(GuidedCampaignState.campaign_id == campaign.id)
    )
    if state is None:
        raise NotFoundError(f"Campaign {campaign.id!r} has no execution state.")
    attempt_rows = session.scalars(
        select(GuidedCampaignAttempt)
        .where(GuidedCampaignAttempt.campaign_id == campaign.id)
        .order_by(GuidedCampaignAttempt.position)
    ).all()
    branch_rows = session.scalars(
        select(GuidedCampaignBranch)
        .where(GuidedCampaignBranch.campaign_id == campaign.id)
        .order_by(GuidedCampaignBranch.position)
    ).all()
    outcomes_by_branch = {
        row.branch_id: row
        for row in session.scalars(
            select(GuidedCampaignBranchOutcome).where(
                GuidedCampaignBranchOutcome.branch_id.in_([b.id for b in branch_rows])
            )
        )
    }
    captured_rows = session.scalars(
        select(GuidedCampaignCapturedUrl).where(
            GuidedCampaignCapturedUrl.campaign_id == campaign.id
        )
    ).all()
    observations = session.scalars(
        select(GuidedCampaignUrlObservation)
        .join(GuidedCampaignCapturedUrl)
        .where(GuidedCampaignCapturedUrl.campaign_id == campaign.id)
    ).all()
    captured_by_id = {row.id: row for row in captured_rows}
    selected_by_attempt = {
        row.id: frozenset((row.capture or {}).get("selected_url_sha256s", []))
        for row in attempt_rows
    }
    attempt_position_by_id = {row.id: row.position for row in attempt_rows}

    observation_queries, captured_not_selected = _index_observations(
        observations, attempt_position_by_id, captured_by_id, selected_by_attempt
    )
    labels_by_captured_id = _captured_url_labels(
        observation_queries,
        _credited_labels(branch_rows, outcomes_by_branch, attempt_position_by_id),
    )

    source_names = _source_names_in(session, captured_rows)
    admitted_postings_by_captured_id = _admitted_postings_in(
        session, [row.id for row in captured_rows if _board_admission(row)]
    )
    findings, boards_admitted, not_staged, known_posting_count, registered_board_count = (
        _classify_captured_rows(
            session,
            captured_rows,
            source_names,
            labels_by_captured_id,
            admitted_postings_by_captured_id,
        )
    )

    hosted_searches_used = sum(row.hosted_search_count for row in attempt_rows)
    cost_cents = _total_cost_cents(session, attempt_rows)

    return CampaignReport(
        campaign_id=campaign.id,
        manifest_sha256=campaign.manifest_sha256,
        cycle=campaign.cycle,
        status=CampaignStatus(state.status),
        max_model_calls=campaign.max_model_calls,
        max_hosted_searches=campaign.max_hosted_searches,
        model_calls_used=sum(1 for row in attempt_rows if row.state != AttemptState.PLANNED),
        hosted_searches_used=hosted_searches_used,
        cost_cents=cost_cents,
        known_posting_count=known_posting_count,
        registered_board_count=registered_board_count,
        findings=tuple(findings),
        boards_admitted=tuple(boards_admitted),
        not_staged=tuple(not_staged),
        branches=_branch_report_rows(branch_rows, outcomes_by_branch),
        attempts=_attempt_report_rows(session, attempt_rows, captured_not_selected),
        warnings=tuple(_campaign_warnings(state)),
        stranded_processing_attempts=stranded_attempt_count(session, campaign.workspace_id),
    )


def latest_campaign_report(session: Session, workspace_id: str) -> CampaignReport | None:
    """Return the newest campaign's report, or None before any campaign."""
    campaign = session.scalar(
        select(GuidedCampaign)
        .where(GuidedCampaign.workspace_id == workspace_id)
        .order_by(GuidedCampaign.created_at.desc())
        .limit(1)
    )
    if campaign is None:
        return None
    return _campaign_report(session, campaign)


def campaign_report(session: Session, workspace_id: str, campaign_id: str) -> CampaignReport:
    """Return one campaign's report by id, scoped to its workspace."""
    campaign = session.get(GuidedCampaign, campaign_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        raise NotFoundError(f"Campaign {campaign_id!r} does not exist.")
    return _campaign_report(session, campaign)


def abandon_uncertain_attempt(
    session: Session, workspace_id: str, campaign_id: str, position: int
) -> CampaignReport:
    """Abandon an uncertain attempt, closing the campaign incomplete; it never resumes."""
    campaign = session.get(GuidedCampaign, campaign_id)
    if campaign is None or campaign.workspace_id != workspace_id:
        raise NotFoundError(f"Campaign {campaign_id!r} does not exist.")
    GuidedCampaignLedger.abandon_uncertain_in_session(session, campaign_id, position)
    return _campaign_report(session, campaign)
