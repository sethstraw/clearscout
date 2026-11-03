"""Fill a throwaway data root with invented job-search data for documentation.

Every screenshot of a real workspace carries a real employer, a real salary, and
a real career record, none of which belong in a public repository. This writes a
complete, plausible workspace out of material that describes nobody:

    uv run python scripts/seed_demo_workspace.py --data-root /tmp/clearscout-demo
    uv run clearscout serve --data-root /tmp/clearscout-demo --port 8799

It refuses to run against a data root that already holds a database unless
``--force`` is passed. That refusal is the point of the script's safety, not a
convenience: the default data root is somebody's live job search.

Nothing here calls a model and nothing here costs money. The candidate is the
synthetic four-year backend engineer the agnosticism suite already uses, whose
Markdown already meets the canonical contract, so the importer adopts it rather
than paying a provider to standardize it. The employers, the postings, the
scores, and the evidence are invented for this file.

Evidence and workflow timestamps are fixed. The discovery review date and audit
timestamps come from the seed run so the resulting workspace reads as current.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from clearscout.application import job_search
from clearscout.application.contracts import (
    ApplicationEventInput,
    CandidateRecordInput,
    CompensationInput,
    EligibilityInput,
    EvaluationCommit,
    EvaluationFactorInput,
    EvaluationScores,
    FitReportInput,
    PostingBodyInput,
    ReportCheckInput,
    ReportReadingInput,
    ReportRequirementInput,
    ScoreComponentInput,
    ScoreEvidenceInput,
)
from clearscout.application.discovery.board_scan import SourceScanService
from clearscout.application.discovery.content_resolution import (
    PostingContentResolutionService,
    ResolvedPostingContent,
)
from clearscout.application.discovery.derived_vocabulary_rules import (
    SearchArchetype,
    SearchCluster,
    SearchTitle,
)
from clearscout.application.discovery.dispositions import DiscoveryDispositionService
from clearscout.application.discovery.guided_campaign_admission import AdmissionResult
from clearscout.application.discovery.guided_campaign_ledger import GuidedCampaignLedger
from clearscout.application.discovery.guided_campaign_report import compile_current_campaign
from clearscout.application.discovery.posting_fetch import FetchedPosting
from clearscout.application.discovery.posting_refresh import PostingRefreshService
from clearscout.application.discovery.proposals import (
    SearchArchetypeProposalService,
    SearchClusterProposalService,
    SearchTitleProposalService,
)
from clearscout.application.discovery.refusal_patterns import RefusedPosting, file_refusal
from clearscout.application.discovery.relevance_disputes import RelevanceDisputeService
from clearscout.application.discovery.relevance_review import current_review_contract
from clearscout.application.discovery.relevance_review_records import (
    RelevanceReviewPostingInput,
    RelevanceReviewRecorder,
    ValidatedRelevanceReview,
    current_review_in,
)
from clearscout.application.discovery.relevance_review_rejections import (
    RelevanceReviewItemFailure,
    RelevanceReviewRejectionService,
)
from clearscout.application.discovery.search_proposal_inbox import SearchProposalInbox
from clearscout.application.discovery.source_continuation import SourceContinuationHandler
from clearscout.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceListing,
    SourceRead,
)
from clearscout.application.documents import DocumentService
from clearscout.application.documents.approvals import ApprovalService
from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.job_search.models import (
    DispositionApply,
    DispositionSelection,
    RelevanceReviewAnswerOutcome,
    RelevanceReviewCitation,
    RelevanceReviewVerdict,
    SourceTerms,
)
from clearscout.application.job_search.postings import RequirementRulingService
from clearscout.application.master_cv import MasterCvService
from clearscout.application.master_cv.cv import parse_cv
from clearscout.application.prompts import packaged
from clearscout.application.tailoring.guardrails import Guardrails
from clearscout.application.tailoring.pipeline import SerializedRunArtifacts
from clearscout.application.tailoring.workflows import BatchOutput, ScreenOutput, TailorOutput
from clearscout.application.tailoring.workflows.evaluations import EvaluationWorkflowService
from clearscout.application.tailoring.workflows.service import WorkflowService
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import (
    GuardrailService,
    LearningLoopPolicyService,
    SearchParametersService,
    WorkspaceService,
)
from clearscout.application.workspaces.prompt_library import PromptLibraryService
from clearscout.application.workspaces.records import WorkspaceRecords
from clearscout.application.workspaces.records.access import audit_columns
from clearscout.application.workspaces.spend import record_calls
from clearscout.domain import (
    ARTIFACT_LOGICAL_TYPES,
    ApplicationEventType,
    ApplicationStatus,
    EvaluationDecision,
    EvidenceClassification,
    EvidenceDirection,
    EvidenceSourceKind,
    EvidenceStrength,
    PostingStatus,
    ScoreComponent,
    SourceKind,
    SourceReadOutcome,
    TailoringOutcome,
    TaskStatus,
)
from clearscout.domain.artifacts import PostingSource
from clearscout.domain.campaigns import (
    CampaignStatus,
    CapturedUrlClassification,
    CapturedUrlOutcome,
    StagedBasis,
    UrlActionKind,
)
from clearscout.domain.policy import LearningLoopPolicy
from clearscout.domain.records import (
    PostingContentAccessStatus,
    PostingContentExtractionMethod,
    PostingIdentityStatus,
    PostingReadIntent,
)
from clearscout.domain.rulings import Ruling
from clearscout.domain.search_parameters import (
    Acceptance,
    Arrangement,
    ClusterKind,
    CompensationBand,
    Employers,
    Employment,
    JobSearchParameters,
    Lane,
    Location,
    Seniority,
    TargetTitle,
    WorkEligibility,
)
from clearscout.domain.staged_facts import PublishedFact, StagedListingFacts
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.migration import status, upgrade
from clearscout.infrastructure.persistence.models import (
    Application,
    ApplicationEvent,
    JobEvaluation,
    JobPosting,
    JobSearchParameterSet,
    PendingDiscovery,
    PostingRequirement,
    ReviewProposal,
    SourceScanContinuation,
)
from clearscout.infrastructure.providers.base import (
    BackgroundPoll,
    CallRecord,
    Search,
    SearchSource,
)
from clearscout.infrastructure.web_fetch.board_adapters import (
    BoardResolution,
)
from clearscout.infrastructure.web_fetch.company_research import ResearchSource
from clearscout.shared.costs import CallManifest, Ledger, ModelPrice, Usage

WORKSPACE_SLUG = "demo-search"
WORKSPACE_NAME = "Demo search"
SEEDED_AT = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
# Keep demo evidence aligned with the agnostic test fixture.
SHORT_CAREER = (
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "short_career.md"
).read_text(encoding="utf-8")
# Earlier imports of the same record, so the CV page has versions to compare.
CV_DRAFTS: tuple[str, ...] = (
    SHORT_CAREER.replace(
        "Backend engineer, four years, mostly Go and Postgres.",
        "Backend engineer working mostly in Go and Postgres.",
    ).replace("- Added integration tests around the tariff engine, which had none\n", ""),
    SHORT_CAREER.replace(
        "**Languages:** Go, Python, SQL",
        "**Languages:** Go, Python",
    ),
)


@dataclass(frozen=True)
class DemoJob:
    """One invented posting and the invented evaluation committed against it."""

    slug: str
    employer: str
    title: str
    location: str
    place_line: str
    arrangement: str
    workspace_location_key: str
    duties: tuple[str, ...]
    asks: tuple[str, ...]
    cv_quote: str
    decision: EvaluationDecision
    posting_status: PostingStatus
    fit: int
    eligibility: int
    evidence: int
    opportunity: int
    compensation: int | None
    pay: tuple[Decimal, Decimal] | None
    currency: str
    rationale: str
    scope: str
    pro: str
    con: str
    gap: str
    risk: str
    events: tuple[tuple[ApplicationStatus, str], ...] = ()

    @property
    def pay_line(self) -> str:
        """Return the advertised pay exactly as the posting body states it."""
        if self.pay is None:
            return "Pay: this advertisement does not publish a range."
        low, high = self.pay
        return f"Pay: {low:,} to {high:,} {self.currency} per year."

    @property
    def body(self) -> str:
        """Return the invented posting body every posting quote is taken from."""
        duties = "\n".join(f"- {item}" for item in self.duties)
        asks = "\n".join(f"- {item}" for item in self.asks)
        return (
            f"{self.employer} is hiring a {self.title}.\n\n"
            f"{self.place_line}\n"
            f"{self.pay_line}\n\n"
            f"What the role covers:\n{duties}\n\n"
            f"What we ask for:\n{asks}\n"
        )


DEMO_JOBS: tuple[DemoJob, ...] = (
    DemoJob(
        slug="kestrel-routing",
        employer="Kestrel Freight Systems",
        title="Senior Backend Engineer, Routing",
        location="Berlin, Germany (remote)",
        place_line="Where: Berlin, Germany. Remote within Germany, one office day a month.",
        arrangement="remote",
        workspace_location_key="berlin",
        duties=(
            "Own the routing service and the Postgres schema underneath it",
            "Bring tail latency on the dispatch endpoint back under control",
            "Share an on-call rotation with four other engineers",
        ),
        asks=(
            "Several years writing production Go or a close equivalent",
            "Postgres beyond simple queries, including query plans",
            "Evidence of making an existing service faster without rewriting it",
        ),
        cv_quote=(
            "Cut p99 latency on the routing endpoint from 1.8s to 240ms with a materialized view"
        ),
        decision=EvaluationDecision.APPLY_NOW,
        posting_status=PostingStatus.ACTIVE,
        fit=88,
        eligibility=100,
        evidence=82,
        opportunity=74,
        compensation=80,
        pay=(Decimal(82000), Decimal(96000)),
        currency="EUR",
        rationale="The routing and latency work on the record answers the published core ask.",
        scope="Own routing, its schema, and its latency budget.",
        pro="The latency work is the same problem this posting describes.",
        con="On-call is shared across a small rotation.",
        gap="C: No named experience with freight tariffs at this scale.",
        risk="A small rotation concentrates on-call load.",
        events=(
            (ApplicationStatus.PACKAGE_READY, "Draft reviewed and evidence checked."),
            (ApplicationStatus.APPLIED, "Submitted through the careers page."),
            (ApplicationStatus.INTERVIEWING, "Two conversations booked."),
            (ApplicationStatus.OFFER, "Written offer received, deciding."),
        ),
    ),
    DemoJob(
        slug="nordlicht-payments",
        employer="Nordlicht Data GmbH",
        title="Platform Engineer, Payments",
        location="Berlin, Germany (hybrid)",
        place_line="Where: Berlin, Germany. Hybrid, two days in the office each week.",
        arrangement="hybrid",
        workspace_location_key="berlin",
        duties=(
            "Keep the payments webhook exactly-once under retry",
            "Reduce the manual reconciliation the finance team still does by hand",
            "Write the runbook nobody has written yet",
        ),
        asks=(
            "Experience with idempotency and retry in a payments path",
            "Comfort owning an incident from alert to write-up",
        ),
        cv_quote=(
            "Wrote the retry and idempotency layer for the payments webhook "
            "after a duplicate-charge incident"
        ),
        decision=EvaluationDecision.PREMIUM_TARGET,
        posting_status=PostingStatus.ACTIVE,
        fit=92,
        eligibility=100,
        evidence=86,
        opportunity=88,
        compensation=90,
        pay=(Decimal(95000), Decimal(110000)),
        currency="EUR",
        rationale="The duplicate-charge work is the exact failure this team is hiring against.",
        scope="Own the payments webhook, its retries, and its reconciliation.",
        pro="Direct, dated evidence of the retry and idempotency work asked for.",
        con="Two office days a week is above the stated preference of one.",
        gap="B: No named experience with a card scheme integration.",
        risk="Hybrid expectation may harden after joining.",
        events=((ApplicationStatus.PACKAGE_READY, "Package assembled, waiting on a referral."),),
    ),
    DemoJob(
        slug="halcyon-grid",
        employer="Halcyon Grid",
        title="Backend Engineer, Scheduling",
        location="Amsterdam, Netherlands (remote)",
        place_line="Where: Amsterdam, Netherlands. Remote across the EU.",
        arrangement="remote",
        workspace_location_key="amsterdam",
        duties=(
            "Build the scheduler that replaces a spreadsheet",
            "Add the test coverage the current service has never had",
        ),
        asks=(
            "Backend experience in any typed language",
            "Willingness to test a service that arrives untested",
        ),
        cv_quote="Added integration tests around the tariff engine, which had none",
        decision=EvaluationDecision.INVESTIGATE,
        posting_status=PostingStatus.ACTIVE,
        fit=70,
        eligibility=78,
        evidence=66,
        opportunity=64,
        compensation=58,
        pay=(Decimal(68000), Decimal(78000)),
        currency="EUR",
        rationale="Plausible on evidence, but the advertisement says almost nothing about scope.",
        scope="Replace a spreadsheet with a scheduling service.",
        pro="Testing an untested service is on the record.",
        con="The posting does not say who owns the service today.",
        gap="C: No named scheduling or constraint-solver work.",
        risk="Scope may be larger than the advertisement suggests.",
        events=(
            (ApplicationStatus.APPLIED, "Applied to keep the option open."),
            (ApplicationStatus.REJECTED, "Rejected at screen, no reason given."),
        ),
    ),
    DemoJob(
        slug="peppermill-go",
        employer="Peppermill Analytics",
        title="Senior Go Engineer",
        location="Amsterdam, Netherlands (hybrid)",
        place_line="Where: Amsterdam, Netherlands. Hybrid, three days in the office each week.",
        arrangement="hybrid",
        workspace_location_key="amsterdam",
        duties=(
            "Move the ingest pipeline off a nightly batch",
            "Keep the query layer answering while it moves",
        ),
        asks=("Go in production", "Some exposure to streaming ingest"),
        cv_quote="Go, Python, SQL",
        decision=EvaluationDecision.MONITOR,
        posting_status=PostingStatus.ACTIVE,
        fit=58,
        eligibility=72,
        evidence=60,
        opportunity=52,
        compensation=55,
        pay=(Decimal(72000), Decimal(84000)),
        currency="EUR",
        rationale="Language match only. Three office days is further than the stated preference.",
        scope="Move a nightly batch to streaming ingest.",
        pro="The language stack matches the record exactly.",
        con="Three office days a week in another country.",
        gap="D: No named streaming ingest experience.",
        risk="Relocation is implied but never stated.",
    ),
    DemoJob(
        slug="vantage-mobility",
        employer="Vantage Mobility Labs",
        title="Backend Engineer",
        location="Berlin, Germany (on site)",
        place_line="Where: Berlin, Germany. On site, five days a week.",
        arrangement="on site",
        workspace_location_key="berlin",
        duties=(
            "Keep an internal booking tool running",
            "Take whatever else the week brings",
        ),
        asks=("Any backend experience", "Comfort with an unclear brief"),
        cv_quote="Maintained an internal invoicing tool in Django",
        decision=EvaluationDecision.DEPRIORITIZE,
        posting_status=PostingStatus.CLOSED,
        fit=42,
        eligibility=64,
        evidence=44,
        opportunity=34,
        compensation=None,
        pay=None,
        currency="EUR",
        rationale="Vague brief, no published pay, and the posting has since closed.",
        scope="Keep an internal booking tool running.",
        pro="Internal-tool maintenance is on the record.",
        con="The brief does not describe a role.",
        gap="E: No named ownership of a product surface.",
        risk="Five days on site with no stated pay.",
    ),
    DemoJob(
        slug="arboreal-staff",
        employer="Arboreal Systems Inc",
        title="Staff Backend Engineer",
        location="Denver, United States (remote)",
        place_line="Where: Denver, United States. Remote, United States only, no visa support.",
        arrangement="remote",
        workspace_location_key="united_states",
        duties=(
            "Lead the migration off a monolith",
            "Mentor three engineers through it",
        ),
        asks=("Ten years of backend experience", "Existing right to work in the United States"),
        cv_quote="Backend engineer, four years, mostly Go and Postgres.",
        decision=EvaluationDecision.INELIGIBLE,
        posting_status=PostingStatus.ACTIVE,
        fit=48,
        eligibility=0,
        evidence=52,
        opportunity=40,
        compensation=86,
        pay=(Decimal(190000), Decimal(225000)),
        currency="USD",
        rationale="The posting states United States only with no visa support, which ends it.",
        scope="Lead a monolith migration and mentor three engineers.",
        pro="The compensation band is well above the search floor.",
        con="Residence rule excludes this candidate outright.",
        gap="A: Ten years asked for against four on the record.",
        risk="No route to eligibility is offered.",
    ),
    DemoJob(
        slug="quicksilver-billing",
        employer="Quicksilver Ledger",
        title="Backend Engineer, Billing",
        location="Berlin, Germany (remote)",
        place_line="Where: Berlin, Germany. Remote within the EU, quarterly team weeks.",
        arrangement="remote",
        workspace_location_key="berlin",
        duties=(
            "Own invoicing and the retry path around it",
            "Replace the manual credit-note process",
        ),
        asks=(
            "Experience with billing or invoicing systems",
            "Care about correctness over throughput",
        ),
        cv_quote="Maintained an internal invoicing tool in Django",
        decision=EvaluationDecision.APPLY_NOW,
        posting_status=PostingStatus.ACTIVE,
        fit=79,
        eligibility=100,
        evidence=74,
        opportunity=70,
        compensation=76,
        pay=(Decimal(78000), Decimal(90000)),
        currency="EUR",
        rationale="Invoicing evidence is direct, and the residence rule is one this search allows.",
        scope="Own invoicing, retries, and credit notes.",
        pro="Invoicing work is named on the record.",
        con="The invoicing evidence is from the earlier, more junior role.",
        gap="C: No named experience with tax or VAT rules.",
        risk="Quarterly team weeks are travel the search has not priced.",
        events=(
            (ApplicationStatus.PACKAGE_READY, "Draft ready, evidence identifiers all resolve."),
            (ApplicationStatus.APPLIED, "Submitted with a short cover note."),
        ),
    ),
    DemoJob(
        slug="tessellate-integrations",
        employer="Tessellate Logistics",
        title="Integration Engineer",
        location="Amsterdam, Netherlands (remote)",
        place_line="Where: Amsterdam, Netherlands. Remote, occasional travel to the depot.",
        arrangement="remote",
        workspace_location_key="amsterdam",
        duties=(
            "Connect carrier APIs that all disagree with each other",
            "Keep the retry behaviour predictable across them",
        ),
        asks=("Integration work against third-party APIs", "Patience with undocumented behaviour"),
        cv_quote="Freight routing platform used by regional carriers.",
        decision=EvaluationDecision.INVESTIGATE,
        posting_status=PostingStatus.ACTIVE,
        fit=68,
        eligibility=76,
        evidence=64,
        opportunity=60,
        compensation=62,
        pay=(Decimal(70000), Decimal(80000)),
        currency="EUR",
        rationale="Carrier context matches, but the posting never says how large the surface is.",
        scope="Integrate carrier APIs and keep retries predictable.",
        pro="The record is already a carrier-facing routing platform.",
        con="The number of integrations is never stated.",
        gap="C: No named EDI experience.",
        risk="Depot travel is unquantified.",
    ),
)

DEMO_TAGS: tuple[tuple[str, str], ...] = (
    ("remote-first", "#2f6f4f"),
    ("payments", "#4a4f9c"),
    ("needs-referral", "#8a5a2b"),
)

TAG_ASSIGNMENTS: tuple[tuple[str, str], ...] = (
    ("remote-first", "kestrel-routing"),
    ("remote-first", "quicksilver-billing"),
    ("payments", "nordlicht-payments"),
    ("payments", "quicksilver-billing"),
    ("needs-referral", "nordlicht-payments"),
)

DEMO_NOTES: tuple[tuple[str, str, str], ...] = (
    (
        "kestrel-routing",
        "Kestrel interview prep",
        "They asked for the materialized view story end to end.\n\n"
        "Have the before and after numbers ready, and be honest that the index "
        "was the second attempt.\n",
    ),
    (
        "",
        "Weekly search review",
        "Two applications out, one at offer.\n\n"
        "Stop opening hybrid roles in another country; three office days is not "
        "a commute, it is a move.\n",
    ),
)


def demo_parameters() -> JobSearchParameters:
    """Return the invented search parameters: two places and one pay band."""
    return JobSearchParameters(
        locations=[
            Location(
                key="berlin",
                country="Germany",
                metro="Berlin",
                eligibility=WorkEligibility.AUTHORIZED,
                basis="Lives and already works here.",
                aliases=["Berlin", "Germany", "DE"],
            ),
            Location(
                key="amsterdam",
                country="Netherlands",
                metro="Amsterdam",
                eligibility=WorkEligibility.SPONSORSHIP_REQUIRED,
                basis="Would need the employer to arrange a permit.",
                aliases=["Amsterdam", "Netherlands", "NL"],
            ),
        ],
        arrangement=Arrangement(
            remote=Acceptance.PREFERRED,
            hybrid=Acceptance.ACCEPTABLE,
            on_site=Acceptance.REFUSED,
            maximum_office_days=1,
        ),
        compensation=[
            CompensationBand(
                location="berlin",
                currency="EUR",
                basis="annual",
                minimum=Decimal(72000),
                target=Decimal(88000),
                stretch=Decimal(105000),
            )
        ],
        seniority=Seniority(
            minimum="mid",
            maximum="senior",
            target_titles=[TargetTitle(title="Backend Engineer")],
            excluded_titles=["Account Executive"],
        ),
        employment=Employment(types=["full_time"], payroll_arrangements=["German payroll"]),
        employers=Employers(industries=["logistics", "payments"]),
    )


def _digest(value: str) -> str:
    """Return the hex digest the campaign ledger stores for one invented string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _span(haystack: str, needle: str) -> tuple[int, int]:
    """Return the offsets of one verbatim quote, refusing a quote that is not there."""
    start = haystack.index(needle)
    return start, start + len(needle)


def _proposal(job: DemoJob, workspace_id: str, cv_text: str, cv_entry_id: str) -> EvaluationCommit:
    """Build one complete evaluation whose every score resolves to a real quote."""
    body = job.body
    cv_start, cv_end = _span(cv_text, job.cv_quote)
    place_start, place_end = _span(body, job.place_line)
    evidence = [
        ScoreEvidenceInput(
            key="cv-record",
            source_kind=EvidenceSourceKind.CANDIDATE,
            text=job.cv_quote,
            char_start=cv_start,
            char_end=cv_end,
            components=[ScoreComponent.FIT, ScoreComponent.EVIDENCE],
            classification=EvidenceClassification.DIRECT,
            strength=EvidenceStrength.STRONG,
            requirement_id="REQ-1",
            master_cv_entry_id=cv_entry_id,
        ),
        ScoreEvidenceInput(
            key="posting-place",
            source_kind=EvidenceSourceKind.POSTING,
            text=job.place_line,
            char_start=place_start,
            char_end=place_end,
            components=[ScoreComponent.ELIGIBILITY, ScoreComponent.OPPORTUNITY],
            classification=EvidenceClassification.DIRECT,
            strength=EvidenceStrength.MODERATE,
            requirement_id="REQ-2",
        ),
    ]
    reading = [
        ReportReadingInput(
            question="Where the work happens",
            answer=job.arrangement,
            evidence_keys=["posting-place"],
        )
    ]
    if job.compensation is None:
        reading.append(
            ReportReadingInput(
                question="What the role pays",
                answer=None,
                note="The advertisement publishes no range.",
            )
        )
    else:
        pay_start, pay_end = _span(body, job.pay_line)
        evidence.append(
            ScoreEvidenceInput(
                key="posting-pay",
                source_kind=EvidenceSourceKind.POSTING,
                text=job.pay_line,
                char_start=pay_start,
                char_end=pay_end,
                components=[ScoreComponent.COMPENSATION],
                classification=EvidenceClassification.DIRECT,
                strength=EvidenceStrength.MODERATE,
            )
        )
        reading.append(
            ReportReadingInput(
                question="What the role pays",
                answer=job.pay_line,
                evidence_keys=["posting-pay"],
            )
        )
    return EvaluationCommit(
        workspace_id=workspace_id,
        employer=job.employer,
        title=job.title,
        location=job.location,
        requisition_id=job.slug.upper(),
        canonical_url=f"https://careers.example.test/{job.slug}",
        discovery_url=f"https://jobs.example.test/{job.slug}",
        first_seen_at=SEEDED_AT,
        observed_at=SEEDED_AT,
        posting_status=job.posting_status,
        employment_type="full-time",
        payroll_arrangement="local payroll",
        decision=job.decision,
        rationale=job.rationale,
        scope_summary=job.scope,
        qualifications_summary="Evidence is quoted from the record and from the advertisement.",
        evaluated_at=SEEDED_AT,
        source_timestamp=SEEDED_AT.isoformat(),
        scores=EvaluationScores(
            fit=ScoreComponentInput(value=job.fit),
            compensation=(
                ScoreComponentInput(value=job.compensation)
                if job.compensation is not None
                else ScoreComponentInput(
                    reason="The advertisement publishes no range and states no pay elsewhere."
                )
            ),
            eligibility=ScoreComponentInput(value=job.eligibility),
            evidence=ScoreComponentInput(value=job.evidence),
            opportunity=ScoreComponentInput(value=job.opportunity),
        ),
        posting_body=PostingBodyInput(
            logical_type="posting.body.text",
            content=body,
            extraction_method="direct_text",
            fetched_at=SEEDED_AT,
            http_status=200,
        ),
        candidate_record=CandidateRecordInput(
            content=cv_text,
            source_uri="demo://seeded-master-cv",
            effective_at=SEEDED_AT,
        ),
        score_evidence=evidence,
        fit_report=FitReportInput(
            prompt_fingerprint="demo-seeder-v1",
            hard_gates=[
                ReportRequirementInput(
                    requirement_id="REQ-2",
                    importance="must_have",
                    text=job.place_line,
                    evidence_keys=["posting-place"],
                )
            ],
            reading=reading,
            requirements=[
                ReportRequirementInput(
                    requirement_id="REQ-1",
                    importance="must_have",
                    text=job.asks[0],
                    evidence_keys=["cv-record"],
                ),
                # Settled against the record and answered by nothing, which is what
                # the missed-requirement reader materializes for the ranker.
                ReportRequirementInput(
                    requirement_id="REQ-3",
                    importance="nice_to_have",
                    text=job.asks[-1],
                    unmet=True,
                ),
            ],
            coverage_present=1,
            coverage_total=len(job.asks),
            checks=[
                ReportCheckInput(check="every cited identifier exists", result="pass"),
                ReportCheckInput(check="no invented employer or date", result="pass"),
            ],
            length_budget="No derivative document was generated during evaluation.",
        ),
        pros=[EvaluationFactorInput(text=job.pro, evidence_keys=["cv-record"])],
        cons=[
            EvaluationFactorInput(
                text=job.con,
                evidence_keys=["posting-place"],
                direction=EvidenceDirection.LIMITS,
            )
        ],
        gaps=[
            EvaluationFactorInput(
                text=job.gap,
                evidence_keys=["cv-record"],
                direction=EvidenceDirection.LIMITS,
            )
        ],
        risks=[
            EvaluationFactorInput(
                text=job.risk,
                evidence_keys=["posting-place"],
                direction=EvidenceDirection.LIMITS,
            )
        ],
        compensation=(
            []
            if job.pay is None
            else [
                CompensationInput(
                    component_type="base",
                    amount_min=job.pay[0],
                    amount_max=job.pay[1],
                    currency=job.currency,
                    basis="annual",
                    geography=job.location,
                    description="Advertised annual base salary.",
                )
            ]
        ),
        eligibility=EligibilityInput(
            workspace_location_key=job.workspace_location_key,
            region_rule=job.location,
            arrangement=job.arrangement,
            details=job.place_line,
        ),
    )


@dataclass(frozen=True)
class DemoListing:
    """One invented board listing and the publisher facts staged beside it."""

    slug: str
    title: str
    employer: str
    location: str
    arrangement: str
    pay: str = ""
    requisition_id: str = ""


KESTREL_BOARD_URL = "https://example.test/kestrel/careers"
FEED_SOURCE_URL = "https://example-feed.test/jobs-feed"
SUNDIAL_BOARD_URL = "https://example-sundial.test/careers"

_KESTREL = "Kestrel Freight Systems"

KESTREL_LISTINGS: tuple[DemoListing, ...] = (
    DemoListing(
        "developer-platform",
        "Backend Engineer, Developer Platform",
        _KESTREL,
        "Berlin, Germany",
        "remote",
        "EUR 130000-150000 annual",
        "KES-204",
    ),
    DemoListing("account-executive", "Account Executive", _KESTREL, "Berlin, Germany", "remote"),
    DemoListing(
        "routing-reliability",
        "Backend Engineer, Routing Reliability",
        _KESTREL,
        "Berlin, Germany",
        "remote",
        "EUR 84000-98000 annual",
    ),
    DemoListing(
        "billing-services",
        "Backend Engineer, Billing Services",
        _KESTREL,
        "Berlin, Germany",
        "hybrid, 1 day per week",
        "EUR 80000-94000 annual",
    ),
    DemoListing(
        "dispatch-api",
        "Senior Backend Engineer, Dispatch API",
        _KESTREL,
        "Berlin, Germany",
        "remote",
        "EUR 90000-104000 annual",
    ),
    DemoListing(
        "warehouse-tools",
        "Backend Engineer, Warehouse Tools",
        _KESTREL,
        "Lisbon, Portugal",
        "remote",
    ),
    DemoListing(
        "field-operations",
        "Backend Engineer, Field Operations",
        _KESTREL,
        "Berlin, Germany",
        "on site, 5 days per week",
    ),
    DemoListing(
        "platform-director",
        "Director of Platform Engineering",
        _KESTREL,
        "Berlin, Germany",
        "remote",
    ),
    DemoListing(
        "data-ingest",
        "Backend Engineer, Data Ingest",
        _KESTREL,
        "Amsterdam, Netherlands",
        "remote",
    ),
    DemoListing(
        "customer-success", "Customer Success Manager", _KESTREL, "Berlin, Germany", "remote"
    ),
    DemoListing(
        "carrier-integrations",
        "Backend Engineer, Carrier Integrations",
        _KESTREL,
        "Amsterdam, Netherlands",
        "hybrid, 1 day per week",
    ),
    DemoListing(
        "tariff-engine",
        "Senior Backend Engineer, Tariff Engine",
        _KESTREL,
        "Berlin, Germany",
        "remote",
        "EUR 88000-101000 annual",
    ),
)

FEED_LISTINGS: tuple[DemoListing, ...] = (
    DemoListing(
        "lumen-dispatch",
        "Backend Engineer, Dispatch",
        "Lumen Transit Cooperative",
        "Berlin, Germany",
        "remote",
        "EUR 78000-92000 annual",
    ),
    DemoListing(
        "lumen-billing",
        "Backend Engineer, Billing",
        "Lumen Transit Cooperative",
        "Berlin, Germany",
        "remote",
    ),
    DemoListing(
        "tessellate-integration",
        "Integration Engineer",
        "Tessellate Logistics",
        "Amsterdam, Netherlands",
        "remote",
    ),
    # The same opening as the Kestrel board's billing role, on another host: the
    # queue's duplicate suggestion has a pair to propose.
    DemoListing(
        "kestrel-billing-mirror",
        "Backend Engineer, Billing Services",
        _KESTREL,
        "Berlin, Germany",
        "remote",
    ),
    DemoListing(
        "northwind-scheduling",
        "Backend Engineer, Scheduling",
        "Northwind Grid Services",
        "Amsterdam, Netherlands",
        "remote",
    ),
    DemoListing(
        "northwind-sales",
        "Account Executive",
        "Northwind Grid Services",
        "Berlin, Germany",
        "remote",
    ),
    DemoListing(
        "meridian-observability",
        "Backend Engineer, Observability",
        "Meridian Yard Systems",
        "Berlin, Germany",
        "hybrid, 1 day per week",
    ),
    DemoListing(
        "meridian-depot",
        "Backend Engineer, Depot Tools",
        "Meridian Yard Systems",
        "Berlin, Germany",
        "on site, 5 days per week",
    ),
    DemoListing(
        "cobalt-payments",
        "Backend Engineer, Payments",
        "Cobalt Rail Analytics",
        "Warsaw, Poland",
        "remote",
    ),
    DemoListing(
        "cobalt-principal",
        "Principal Backend Engineer",
        "Cobalt Rail Analytics",
        "Berlin, Germany",
        "remote",
    ),
)

_SUNDIAL = "Sundial Transit Group"

SUNDIAL_LISTINGS: tuple[DemoListing, ...] = (
    DemoListing(
        "sundial-routing", "Backend Engineer, Routing", _SUNDIAL, "Berlin, Germany", "remote"
    ),
    DemoListing(
        "sundial-ledger", "Backend Engineer, Ledger", _SUNDIAL, "Berlin, Germany", "remote"
    ),
)

SUNDIAL_CONTINUED_LISTINGS: tuple[DemoListing, ...] = (
    DemoListing(
        "sundial-scheduling", "Backend Engineer, Scheduling", _SUNDIAL, "Berlin, Germany", "remote"
    ),
    DemoListing(
        "sundial-reporting",
        "Backend Engineer, Reporting",
        _SUNDIAL,
        "Amsterdam, Netherlands",
        "remote",
    ),
)

SUNDIAL_READABLE_PAGE = "https://example-sundial.test/careers/page-2"
SUNDIAL_BROKEN_PAGE = "https://example-sundial.test/careers/archive"
SUNDIAL_HELD_PAGE = "https://example-sundial.test/careers/manual-review"


def _demo_facts(item: DemoListing) -> StagedListingFacts:
    """Return the publisher facts an invented board states for one listing."""
    return StagedListingFacts(
        locations=PublishedFact(
            values=(item.location,), provenance="demo_board.location", complete=True
        ),
        arrangements=PublishedFact(
            values=(item.arrangement,), provenance="demo_board.workplace_type", complete=True
        ),
        employment_types=PublishedFact(
            values=("permanent", "full-time"), provenance="demo_board.commitment", complete=True
        ),
        compensation=PublishedFact(
            values=(item.pay,) if item.pay else (),
            provenance="demo_board.compensation",
            complete=bool(item.pay),
        ),
    )


def _demo_source_listing(item: DemoListing, root: str) -> SourceListing:
    """Return one invented board listing addressed under the given board root."""
    return SourceListing(
        employer=item.employer,
        title=item.title,
        url=f"{root}/{item.slug}",
        location=item.location,
        requisition_id=item.requisition_id,
        published_description=(
            f"{item.employer} is advertising a {item.title} in {item.location}. "
            f"This listing is invented for the documentation workspace."
        ),
        facts=_demo_facts(item),
    )


def _demo_continuation_target(url: str, label: str, *, automatic: bool) -> SourceContinuationTarget:
    """Return one invented deferred publisher link for the Sundial board."""
    return SourceContinuationTarget(
        url=url,
        label=label,
        label_source="visible",
        source_url=SUNDIAL_BOARD_URL,
        structural_context="navigation",
        relations=("same-origin", "substantive-label"),
        automatic_eligible=automatic,
        deferred_reason="" if automatic else "policy_review",
    )


class DemoBoardUnreadableError(RuntimeError):
    """The invented board refuses one deferred link, so its frontier row fails."""


class DemoBoards:
    """Return fixed public-board results without making a network request."""

    def resolve(self, url: str) -> BoardResolution:
        """Resolve invented demo addresses without touching the network."""
        return BoardResolution(platform=None, match=None, url=url, page="")

    def listings(
        self,
        employer: str,
        url: str,
        *,
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Return the invented listing set for one enabled demo board."""
        del employer, employer_is_authoritative, options
        if url == KESTREL_BOARD_URL:
            return SourceRead(
                listings=[
                    _demo_source_listing(item, "https://example.test/kestrel/jobs")
                    for item in KESTREL_LISTINGS
                ],
                complete=True,
                raw_count=len(KESTREL_LISTINGS),
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )
        if url == FEED_SOURCE_URL:
            return SourceRead(
                listings=[
                    _demo_source_listing(item, "https://example-feed.test/postings")
                    for item in FEED_LISTINGS
                ],
                complete=True,
                raw_count=len(FEED_LISTINGS),
                outcome=SourceReadOutcome.ATS_COMPLETE,
            )
        if url == SUNDIAL_BOARD_URL:
            return SourceRead(
                listings=[
                    _demo_source_listing(item, "https://example-sundial.test/jobs")
                    for item in SUNDIAL_LISTINGS
                ],
                complete=False,
                raw_count=len(SUNDIAL_LISTINGS),
                outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                deferred_targets=(
                    _demo_continuation_target(SUNDIAL_READABLE_PAGE, "Page 2", automatic=True),
                    _demo_continuation_target(SUNDIAL_BROKEN_PAGE, "Archive", automatic=True),
                    _demo_continuation_target(SUNDIAL_HELD_PAGE, "Held page", automatic=False),
                ),
            )
        return SourceRead(
            listings=[],
            complete=False,
            raw_count=0,
            outcome=SourceReadOutcome.FETCH_FAILED,
            note="The synthetic board has no configured response for this address.",
        )

    def continue_listings(
        self,
        employer: str,
        targets: tuple[SourceContinuationTarget, ...],
        *,
        root_url: str,
        handoff_scopes: tuple[tuple[str, str, str], ...],
        employer_is_authoritative: bool = False,
        options: object = None,
    ) -> SourceRead:
        """Read the invented deferred links, refusing the one that never answers."""
        del employer, root_url, handoff_scopes, employer_is_authoritative, options
        if any(target.url == SUNDIAL_BROKEN_PAGE for target in targets):
            raise DemoBoardUnreadableError("The invented archive page answers with HTTP 503.")
        held = tuple(target for target in targets if target.url == SUNDIAL_HELD_PAGE)
        listings = (
            [
                _demo_source_listing(item, "https://example-sundial.test/jobs")
                for item in SUNDIAL_CONTINUED_LISTINGS
            ]
            if any(target.url == SUNDIAL_READABLE_PAGE for target in targets)
            else []
        )
        return SourceRead(
            listings=listings,
            complete=False,
            raw_count=len(listings),
            outcome=(
                SourceReadOutcome.CRAWL_CONTINUATION
                if held
                else SourceReadOutcome.PUBLISHER_LINKED_PAGES
            ),
            deferred_targets=held,
            attempted_target_urls=tuple(target.url for target in targets),
        )


class DemoPostingFetcher:
    """Return one changed posting body without making a network request."""

    def fetch(
        self, url: str, *, intent: PostingReadIntent = PostingReadIntent.CRAWL, board_url: str = ""
    ) -> FetchedPosting:
        """Return a later invented reading of the Kestrel posting."""
        del url, intent, board_url
        return FetchedPosting(
            text=DEMO_JOBS[0].body
            + "\nThe team now also owns the deployment playbook for this service.\n",
            posting_status="active",
            method="demo_seeder",
            fetched_at=SEEDED_AT + timedelta(days=7),
            http_status=200,
            publisher_markup_present=True,
        )


class DemoContentResolver:
    """Return invented posting bodies without making a network request."""

    def resolve(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
        board_url: str = "",
    ) -> ResolvedPostingContent:
        """Retain an invented body, or report the one page that needs remediation."""
        del intent, board_url
        fetched_at = SEEDED_AT + timedelta(days=11)
        if url.endswith("/tariff-engine"):
            return ResolvedPostingContent(
                requested_url=url,
                final_url=url,
                content_status=PostingContentAccessStatus.SESSION_REQUIRED,
                identity_status=PostingIdentityStatus.NOT_YET_VERIFIED,
                posting_status=None,
                extraction_method=None,
                fetched_at=fetched_at,
                http_status=403,
                note="The invented publisher asks for a signed-in session before the body.",
            )
        body = (
            f"{employer or 'The employer'} is hiring a {title or 'backend engineer'}.\n\n"
            "The team owns a production service, its PostgreSQL schema, and the "
            "latency budget around it. Remote work within Germany is available, and "
            "the advertisement does not publish a compensation range.\n"
        )
        return ResolvedPostingContent(
            requested_url=url,
            final_url=url,
            content_status=PostingContentAccessStatus.READABLE,
            identity_status=PostingIdentityStatus.VERIFIED,
            posting_status=PostingStatus.ACTIVE,
            extraction_method=PostingContentExtractionMethod.STATIC_STRUCTURED,
            fetched_at=fetched_at,
            content=body.encode("utf-8"),
            http_status=200,
            publisher_markup_present=True,
            identity_evidence={
                "method": "demo_seeder",
                "publisher_declaration": True,
                "publisher_title": title,
                "publisher_employer": employer,
            },
            note="Invented publisher content retained for the documentation workspace.",
        )


DEMO_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _demo_pdf() -> bytes:
    """Build a small valid PDF containing only invented documentation text."""
    stream = b"BT /F1 12 Tf 72 720 Td (Synthetic ClearScout document) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode())
        content.extend(body)
        content.extend(b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)


# The ledger prices a call by the model that served it, so every served name the
# seeder uses is a key here. Invented dollars per million tokens and per thousand
# hosted searches, small enough that a whole demo run costs a few cents.
DEMO_PRICES: dict[str, ModelPrice] = {
    "demo-model": ModelPrice(input=0.25, output=2.0, search_per_thousand_calls=10.0),
    "demo-model-2026-05": ModelPrice(input=0.25, output=2.0, search_per_thousand_calls=10.0),
    "demo-model-large": ModelPrice(input=1.25, output=10.0, search_per_thousand_calls=10.0),
    "demo-model-large-2026-05": ModelPrice(input=1.25, output=10.0, search_per_thousand_calls=10.0),
}


def _ledger(stage: str, *, prompts: tuple[str, ...], searches: int = 0) -> Ledger:
    """Return one realistically priced but entirely invented model-call ledger."""
    ledger = Ledger(prices=DEMO_PRICES)
    ledger.record(
        CallManifest(
            stage=stage,
            model="demo-model",
            served="demo-model-2026-05",
            provider="openai",
            response_id=f"demo-{stage}",
            usage=Usage(input_tokens=1840, output_tokens=430, reasoning_tokens=120),
            finish="complete",
            effort="low",
            verbosity="medium",
            service_tier="standard",
            revision="demo-seed",
            prompts=prompts,
            instructions_digest="1" * 64,
            evidence_digest="2" * 64,
            task_digest="3" * 64,
            schema_digest="4" * 64,
            validated=True,
            milliseconds=2380,
            search_request_count=searches,
        )
    )
    return ledger


class DemoTaskContext:
    """The task boundary a seeded handler run needs, over one claimed demo task."""

    def __init__(self, queue: TaskQueueService, task_id: str) -> None:
        """Bind progress writes to one claimed task and the demo worker identity."""
        self.queue = queue
        self.task_id = task_id
        self.worker_id = "demo-seeder"

    def progress(self, stage: str, payload: dict[str, object]) -> None:
        """Persist visible progress so the seeded task carries its event history."""
        self.queue.heartbeat(self.task_id, worker_id=self.worker_id, progress=payload, stage=stage)

    def cancellation_requested(self) -> bool:
        """Report that nothing cancels a seeded task."""
        return False

    def raise_if_cancelled(self) -> None:
        """Pass every cancellation point, because nothing cancels a seeded task."""
        return


# (stage, prompt name, model, input tokens, output tokens)
DEMO_SPEND: tuple[tuple[str, str, str, int, int], ...] = (
    ("match", "match_system", "demo-model", 1520, 280),
    ("match", "match_system", "demo-model", 1480, 260),
    ("jobspec", "jobspec_system", "demo-model", 2210, 540),
    ("draft", "draft_system", "demo-model-large", 3120, 980),
    ("critic", "critic_system", "demo-model", 2640, 610),
    ("cover", "cover_system", "demo-model-large", 1880, 720),
    ("relevance_review", "relevance_review_system", "demo-model", 4180, 890),
    ("relevance_review", "relevance_review_system", "demo-model", 4180, 890),
    ("vocabulary", "vocabulary_system", "demo-model", 2040, 470),
)


def _seed_spend(database: Database, workspace_id: str, job_ids: list[str]) -> None:
    """Record invented model calls across stages and models so the spend page has a shape.

    Every row carries the seed run's own timestamp: ``model_calls`` is immutable
    in the schema, so a call cannot be backdated after it is written.
    """
    queue = TaskQueueService(database)
    task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="discovery.evaluate_batch",
        payload={"job_posting_ids": job_ids},
        idempotency_key="demo-evaluate-batch",
    )
    calls = 0
    with database.session() as session:
        for position, (stage, prompt, model, input_tokens, output_tokens) in enumerate(DEMO_SPEND):
            cached = position % 4 == 3
            ledger = Ledger(prices=DEMO_PRICES)
            ledger.record(
                CallManifest(
                    stage=stage,
                    model=model,
                    served=f"{model}-2026-05",
                    provider="openai",
                    response_id=f"demo-spend-{position}",
                    usage=Usage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=0 if cached else input_tokens // 3,
                    ),
                    finish="complete",
                    effort="low",
                    verbosity="medium",
                    service_tier="standard",
                    revision="demo-seed",
                    prompts=(prompt,),
                    # True means a stored answer was replayed and no provider call happened.
                    cached=cached,
                    validated=True,
                    milliseconds=1900 + 120 * position,
                )
            )
            calls += len(
                record_calls(
                    session,
                    workspace_id=workspace_id,
                    task_id=task_id,
                    ledger=ledger,
                    provider="openai",
                    origin="demo_seeder",
                )
            )
    _finish_task(queue, task_id, progress={"calls": calls})


def _start_task(
    queue: TaskQueueService,
    *,
    workspace_id: str,
    task_type: str,
    payload: dict[str, object],
    idempotency_key: str,
) -> str:
    """Create and claim a representative task through the durable queue."""
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type=task_type,
        payload=payload,
        idempotency_key=idempotency_key,
        origin="demo_seeder",
    )
    claimed = queue.claim_next(worker_id="demo-seeder")
    if claimed is None or str(claimed.id) != str(task.id):
        raise RuntimeError(f"Demo task {task.id} could not be claimed in order.")
    queue.heartbeat(str(task.id), worker_id="demo-seeder", progress={}, stage="persisting")
    return str(task.id)


def _finish_task(queue: TaskQueueService, task_id: str, *, progress: dict[str, object]) -> None:
    """Finish a claimed demo task after its invented output is durable."""
    queue.finish(
        task_id,
        worker_id="demo-seeder",
        status=TaskStatus.COMPLETED,
        progress=progress,
    )


def _seed_workspace_configuration(database: Database, workspace_id: str) -> str:
    """Populate editable workspace policy, prompt, and table-view state."""
    GuardrailService(database).save(
        workspace_id,
        Guardrails(forbidden=["rockstar"]),
        origin="demo_seeder",
    )
    GuardrailService(database).save(
        workspace_id,
        Guardrails(
            protected=["PostgreSQL"],
            required=["Berlin, Germany"],
            forbidden=["world-class", "rockstar"],
            work_authorization="Authorized to work in Germany.",
            distinctions=[
                "Describe measured backend work without implying ownership of "
                "company-wide strategy."
            ],
        ),
        origin="demo_seeder",
    )
    LearningLoopPolicyService(database).save(
        workspace_id,
        LearningLoopPolicy(),
        origin="demo_seeder",
    )
    prompts = PromptLibraryService(database)
    prompts.save(
        workspace_id,
        "jobspec_system",
        packaged("jobspec_system") + "\nRead explicit requirements conservatively.\n",
        origin="demo_seeder",
    )
    prompts.save(
        workspace_id,
        "jobspec_system",
        packaged("jobspec_system")
        + "\nTreat an advertised requirement as required only when the posting says so.\n",
        origin="demo_seeder",
    )
    prompt_version_id = prompts.overrides(workspace_id)["jobspec_system"][0]
    WorkspaceRecords.for_database(database).saved_views.save_view(
        workspace_id=workspace_id,
        view_id=None,
        resource="jobs",
        name="Strong Berlin fits",
        configuration={
            "filters": {"decisions": ["apply_now"], "locations": ["Berlin"]},
            "sort": [{"field": "fit_score", "direction": "desc"}],
        },
    )
    return prompt_version_id


def _seed_missed_requirement_texts(
    database: Database, workspace_id: str, master_version_id: str
) -> None:
    """Materialize one missed-requirement row per evaluation payload with a gap."""
    from clearscout.application.discovery.missed_requirement_reader import refresh_for_evaluation
    from clearscout.infrastructure.persistence.models import JobPosting

    with database.session() as session:
        for row in session.scalars(
            select(JobEvaluation)
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(
                JobPosting.workspace_id == workspace_id,
                JobEvaluation.master_cv_version_id == master_version_id,
            )
        ).all():
            refresh_for_evaluation(
                session,
                workspace_id=workspace_id,
                master_cv_version_id=master_version_id,
                evaluation_id=row.id,
                payload=row.fit_report_payload,
                origin="demo_seeder",
            )
        session.commit()


def _seed_continuation(database: Database, scans: SourceScanService, workspace_id: str) -> None:
    """Drain the deferred publisher links through the continuation task chain.

    The invented archive page refuses every attempt, so its frontier row spends
    its automatic attempts and fails; the held page stays pending for review.
    """
    with database.session() as session:
        continuation = session.scalar(
            select(SourceScanContinuation).where(
                SourceScanContinuation.workspace_id == workspace_id
            )
        )
        source_scan_id = continuation.source_scan_id if continuation is not None else None
    if source_scan_id is None:
        raise RuntimeError("The demo scan did not retain a deferred publisher frontier.")
    scans.request_continuation(workspace_id, source_scan_id)
    handler = SourceContinuationHandler(
        database,
        DemoBoards(),
        scans.sweep,
        lambda ws_id, task_id, outcomes: scans.record_task_outcomes(
            ws_id, task_id, outcomes, origin="source.continue"
        ),
    )
    queue = TaskQueueService(database)
    while (claimed := queue.claim_next(worker_id="demo-seeder")) is not None:
        if claimed.task_type != "source.continue":
            raise RuntimeError(f"Unexpected demo task {claimed.task_type} in the scan chain.")
        task_id = str(claimed.id)
        result = handler(DemoTaskContext(queue, task_id), dict(claimed.payload))
        queue.finish(
            task_id,
            worker_id="demo-seeder",
            status=TaskStatus.COMPLETED,
            progress=result,
        )


def _seed_search_state(
    database: Database,
    storage: RuntimeStorage,
    scans: SourceScanService,
    workspace_id: str,
    master_version_id: str,
    evidence_id: str,
    job_ids: dict[str, str],
) -> None:
    """Populate proposal inboxes, one ruling, board history, and discovery review."""
    inbox = SearchProposalInbox()
    with database.session() as session:
        inbox.remember_titles_in_session(
            session,
            workspace_id,
            master_version_id,
            [
                SearchTitle(
                    title="Platform Backend Engineer",
                    reason=(
                        "The record combines backend delivery, PostgreSQL, and service reliability."
                    ),
                    evidence_ids=(evidence_id,),
                    used=True,
                    lane=Lane.SPECIALIST,
                ),
                SearchTitle(
                    title="Backend Reliability Engineer",
                    reason="The record includes measured latency and incident-response work.",
                    evidence_ids=(evidence_id,),
                    used=True,
                    lane=Lane.SPECIALIST,
                ),
            ],
            correlation_id=None,
        )
        inbox.remember_archetypes_in_session(
            session,
            workspace_id,
            master_version_id,
            [
                SearchArchetype(
                    name="Operational software teams",
                    because="They need measured improvements to existing production services.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
                SearchArchetype(
                    name="Infrastructure product teams",
                    because="They maintain durable services and internal platform capabilities.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
            ],
            correlation_id=None,
        )
        inbox.remember_clusters_in_session(
            session,
            workspace_id,
            master_version_id,
            [
                SearchCluster(
                    kind=ClusterKind.CAPABILITY,
                    name="Backend performance",
                    terms=("latency", "PostgreSQL", "profiling"),
                    because="The CV records a measured routing-endpoint improvement.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
                SearchCluster(
                    kind=ClusterKind.CAPABILITY,
                    name="Database operations",
                    terms=("query plans", "schema design", "PostgreSQL"),
                    because="The record names production PostgreSQL work.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
                SearchCluster(
                    kind=ClusterKind.DOMAIN,
                    name="Operational systems",
                    terms=("routing", "reliability", "incident response"),
                    because="The record is strongest where software supports live operations.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
                SearchCluster(
                    kind=ClusterKind.DOMAIN,
                    name="Logistics platforms",
                    terms=("freight", "dispatch", "carriers"),
                    because="The record names a freight-routing platform.",
                    evidence_ids=(evidence_id,),
                    used=True,
                ),
            ],
            correlation_id=None,
        )
    title_proposals = SearchTitleProposalService(database)
    title_proposals.accept(
        workspace_id,
        next(
            item.id
            for item in title_proposals.list(workspace_id)
            if item.title.startswith("Platform")
        ),
    )
    archetype_proposals = SearchArchetypeProposalService(database)
    archetype_proposals.reject(
        workspace_id,
        next(
            item.id
            for item in archetype_proposals.list(workspace_id)
            if item.name.startswith("Infrastructure")
        ),
    )
    cluster_proposals = SearchClusterProposalService(database)
    cluster_proposals.accept(
        workspace_id,
        next(
            item.id
            for item in cluster_proposals.list(workspace_id)
            if item.name == "Backend performance"
        ),
    )

    with database.session() as session:
        requirement_key = session.scalar(select(PostingRequirement.requirement_key).limit(1))
    if requirement_key is None:
        raise RuntimeError("The demo evaluations did not produce a requirement to rule on.")
    RequirementRulingService(database).set(
        workspace_id=workspace_id,
        requirement_key=requirement_key,
        ruling=Ruling.DISPUTED_FIT,
        note="The wording is broader than the evidence-backed requirement in the posting.",
        audit=audit_columns("local_user", "demo_seeder", None),
    )

    posting_fetcher = DemoPostingFetcher()
    scans.add_source(
        workspace_id,
        "Kestrel Freight Systems",
        KESTREL_BOARD_URL,
        source_type=SourceKind.EMPLOYER_BOARD,
        terms=SourceTerms(
            url="https://example.test/kestrel/terms",
            condition="Public job-board API reads are permitted for personal job search.",
        ),
        origin="demo_seeder",
    )
    scans.add_source(
        workspace_id,
        "Sample public jobs feed",
        FEED_SOURCE_URL,
        source_type=SourceKind.AGGREGATOR,
        terms=SourceTerms(
            url="https://example-feed.test/jobs-feed/terms",
            condition="Link to the original posting when retaining a result.",
        ),
        enabled=True,
        origin="demo_seeder",
    )
    scans.add_source(
        workspace_id,
        "Sundial Transit Group",
        SUNDIAL_BOARD_URL,
        source_type=SourceKind.EMPLOYER_BOARD,
        terms=SourceTerms(
            url="https://example-sundial.test/terms",
            condition="Read the published index and the pages it links to.",
        ),
        origin="demo_seeder",
    )
    queue = TaskQueueService(database)
    scan_task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="demo-source-scan",
    )
    scan_result = scans.scan(workspace_id, DemoTaskContext(queue, scan_task_id))
    _finish_task(queue, scan_task_id, progress={"funnel": scan_result["funnel"]})
    _seed_continuation(database, scans, workspace_id)
    scans.stage_found_posting(
        workspace_id,
        "https://example.test/halcyon/jobs/api-reliability",
        date.today(),
        title="Backend Engineer, API Reliability",
        employer="Halcyon Grid",
        location="Remote in Germany",
    )
    PostingRefreshService(
        database,
        storage=storage,
        fetcher=posting_fetcher,
    ).refresh(workspace_id, job_ids["kestrel-routing"])

    evaluations = EvaluationWorkflowService(database, storage, fetcher=posting_fetcher)
    closed = evaluations.inputs(
        workspace_id,
        pending_discovery_id=None,
        job_posting_id=job_ids["vantage-mobility"],
    )
    gate = evaluations.refused_by(closed)
    if gate is None:
        raise RuntimeError("The closed demo posting did not produce a deterministic refusal.")
    evaluations.record_refusal(closed, gate, task_id=None)


CAMPAIGN_QUERIES: tuple[str, ...] = (
    "backend engineer remote Berlin Germany",
    "platform backend engineer Amsterdam Netherlands",
    "backend engineer logistics dispatch remote",
)


@dataclass(frozen=True)
class DemoCapturedUrl:
    """One URL an invented campaign attempt captured, and what admission decided."""

    url: str
    classification: CapturedUrlClassification
    outcome: CapturedUrlOutcome
    detail: str
    action_kind: UrlActionKind = UrlActionKind.SEARCH_SOURCE
    model_selected: bool = False
    model_why: str = ""
    model_board: bool = False
    employer: str = ""
    title: str = ""

    @property
    def staged(self) -> bool:
        """Return whether admission staged this URL as a pending discovery."""
        return self.outcome is CapturedUrlOutcome.STAGED


CAMPAIGN_CAPTURED_URLS: tuple[DemoCapturedUrl, ...] = (
    DemoCapturedUrl(
        url="https://example-lumen.test/jobs/platform-backend",
        classification=CapturedUrlClassification.ATS_DETAIL,
        outcome=CapturedUrlOutcome.STAGED,
        detail="",
        model_selected=True,
        model_why="The advertisement names backend service ownership and PostgreSQL.",
        employer="Lumen Transit Cooperative",
        title="Backend Engineer, Platform",
    ),
    DemoCapturedUrl(
        url="https://example-northwind.test/careers/backend-scheduling",
        classification=CapturedUrlClassification.JOB_PAGE_CANDIDATE,
        outcome=CapturedUrlOutcome.STAGED,
        detail="",
        action_kind=UrlActionKind.OPEN_PAGE,
        employer="Northwind Grid Services",
        title="Backend Engineer, Scheduling Services",
    ),
    DemoCapturedUrl(
        url="https://example-meridian.test/jobs/12408",
        classification=CapturedUrlClassification.ATS_DETAIL,
        outcome=CapturedUrlOutcome.UNREADABLE,
        detail="The invented page answered with HTTP 403 and published no posting text.",
    ),
    DemoCapturedUrl(
        url="https://example.test/kestrel/jobs/developer-platform",
        classification=CapturedUrlClassification.ATS_DETAIL,
        outcome=CapturedUrlOutcome.KNOWN_POSTING,
        detail="This posting is already in the queue from the recurring board scan.",
    ),
    DemoCapturedUrl(
        url="https://example-cobalt.test/about/engineering",
        classification=CapturedUrlClassification.OTHER,
        outcome=CapturedUrlOutcome.UNRESOLVED,
        detail="The model named this page but no search or page read reached it.",
        action_kind=UrlActionKind.MODEL_WRITTEN,
        model_selected=True,
        model_why="The page was expected to link an openings index.",
    ),
    DemoCapturedUrl(
        url=KESTREL_BOARD_URL,
        classification=CapturedUrlClassification.BOARD,
        outcome=CapturedUrlOutcome.REGISTERED_BOARD,
        detail="This board is already registered for recurring scans.",
        action_kind=UrlActionKind.OPEN_PAGE,
        model_board=True,
    ),
)


def _campaign_poll(queries: tuple[str, ...], urls: tuple[DemoCapturedUrl, ...]) -> BackgroundPoll:
    """Return the invented provider poll one campaign attempt captured."""
    return BackgroundPoll(
        status="complete",
        value=None,
        record=CallRecord(
            usage=Usage(input_tokens=2400, output_tokens=620, reasoning_tokens=180),
            model="demo-model",
            response_id="demo-campaign-attempt",
            service_tier="flex",
            searches=tuple(
                Search(
                    queries=(query,),
                    sources=tuple(
                        SearchSource(url=item.url, title=item.title or "Invented result")
                        for item in urls
                        if item.action_kind is UrlActionKind.SEARCH_SOURCE
                    ),
                )
                for query in queries
            ),
            search_count=len(queries),
            page_action_count=sum(
                1 for item in urls if item.action_kind is UrlActionKind.OPEN_PAGE
            ),
        ),
        raw={"schema_version": 1, "note": "Invented capture for the documentation workspace."},
    )


def _captured_url_row(item: DemoCapturedUrl, query: str) -> dict[str, object]:
    """Return one captured-URL row in the shape the campaign ledger stores."""
    return {
        "url_sha256": _digest(item.url),
        "url": item.url,
        "classification": item.classification.value,
        "model_selected": item.model_selected,
        "model_why": item.model_why,
        "model_board": item.model_board,
        "observations": ({"action_kind": item.action_kind.value, "query": query},),
    }


def _seed_guided_campaign(
    database: Database,
    scans: SourceScanService,
    workspace_id: str,
) -> dict[str, int]:
    """Run one finished guided campaign through its own ledger, with no provider call.

    The provider poll and the admission results are invented; every state
    transition, cost record, and branch outcome is written by the ledger itself.
    """
    with database.session() as session:
        manifest = compile_current_campaign(
            session, workspace_id, max_model_calls=3, max_hosted_searches=18
        ).manifest
    queue = TaskQueueService(database)
    task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="search.guided_campaign",
        payload={
            "campaign_manifest_sha256": manifest.sha256,
            "campaign_cycle": 1,
            "max_model_calls": 3,
            "max_hosted_searches": 18,
        },
        idempotency_key="demo-guided-campaign",
    )
    ledger = GuidedCampaignLedger(database)
    campaign_id = ledger.create_or_claim(
        workspace_id,
        manifest,
        1,
        task_id,
        {"source": "demo_seeder", "note": "Invented approval snapshot."},
    )
    source_id = scans.list_sources(workspace_id).items[0].id
    staged_ids: dict[str, str] = {}
    for item in CAMPAIGN_CAPTURED_URLS:
        if not item.staged:
            continue
        response = scans.stage_found_posting(
            workspace_id,
            item.url,
            date.today(),
            title=item.title,
            employer=item.employer,
            location="Berlin, Germany",
            origin="search.guided_campaign",
            correlation_id=task_id,
        )
        if response is None:
            raise RuntimeError(f"The demo campaign could not stage {item.url}.")
        staged_ids[item.url] = response.pending_discovery.id
    for position, plan in enumerate(manifest.attempts, start=1):
        queries = CAMPAIGN_QUERIES[: max(1, len(plan.branch_keys))]
        urls = CAMPAIGN_CAPTURED_URLS if position == 1 else ()
        request_digest = _digest(f"{campaign_id}:{plan.position}")
        ledger.mark_dispatching(campaign_id, plan.position, task_id, request_digest)
        ledger.mark_dispatched(
            campaign_id,
            plan.position,
            task_id,
            f"demo-response-{plan.position}",
            request_digest,
            request_envelope=None,
        )
        ledger.capture(
            campaign_id,
            plan.position,
            task_id,
            workspace_id=workspace_id,
            poll=_campaign_poll(queries, urls),
            ledger=_ledger(
                "guided_campaign",
                prompts=("guided_campaign_system",),
                searches=len(queries),
            ),
            provider="openai",
            # Only the first branch of each attempt is credited with a query, so the
            # report shows searched branches beside unvisited ones.
            branch_queries={
                key: (queries[0],) if index == 0 else ()
                for index, key in enumerate(plan.branch_keys)
            },
            has_unattributable_search=False,
            captured_urls=tuple(_captured_url_row(item, queries[0]) for item in urls),
        )
        with database.session() as session:
            ledger.record_admissions_in_session(
                session,
                campaign_id,
                plan.position,
                task_id,
                tuple(
                    AdmissionResult(
                        url_sha256=_digest(item.url),
                        url=item.url,
                        classification=item.classification,
                        model_selected=item.model_selected,
                        model_why=item.model_why,
                        outcome=item.outcome,
                        staged_basis=(
                            StagedBasis.MODEL_SELECTED
                            if item.model_selected
                            else StagedBasis.TITLE_EVIDENCE
                        )
                        if item.staged
                        else None,
                        pending_discovery_ids=((staged_ids[item.url],) if item.staged else ()),
                        source_id=(
                            source_id
                            if item.outcome is CapturedUrlOutcome.REGISTERED_BOARD
                            else None
                        ),
                        detail=item.detail,
                    )
                    for item in urls
                ),
            )
            ledger.mark_processed_in_session(session, campaign_id, plan.position, task_id)
    ledger.finish(campaign_id, task_id, status=CampaignStatus.COMPLETED, reasons=[])
    _finish_task(
        queue,
        task_id,
        progress={"campaign_id": campaign_id, "findings": len(staged_ids)},
    )
    return {
        "guided_campaign_attempts": len(manifest.attempts),
        "guided_campaign_branches": len(manifest.branches),
        "guided_campaign_captured_urls": len(CAMPAIGN_CAPTURED_URLS),
    }


def _seed_artifact_catalog(
    database: Database,
    storage: RuntimeStorage,
    workspace_id: str,
    master_version_id: str,
    job_id: str,
    tailoring_run_id: str,
) -> None:
    """Store synthetic examples of every artifact kind not produced above."""
    documents = DocumentService(database, storage)
    with database.session() as session:
        application_id = session.scalar(
            select(Application.id).where(Application.job_posting_id == job_id).limit(1)
        )
    if application_id is None:
        raise RuntimeError("The primary demo job did not produce an application record.")

    recruiter_email = documents.upload(
        workspace_id=workspace_id,
        entity_type="job_posting",
        entity_id=job_id,
        logical_type="user.attachment",
        filename="interview-preparation.txt",
        mime_type="text/plain",
        content=b"Synthetic interview preparation notes for the routing role.\n",
    )
    documents.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="recruiter.email",
        filename="recruiter-message.txt",
        mime_type="text/plain",
        content=b"Please choose a time for a synthetic screening conversation.\n",
    )
    documents.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="take_home",
        filename="take-home-brief.md",
        mime_type="text/markdown",
        content=b"# Synthetic exercise\n\nExplain how you would investigate routing latency.\n",
    )

    with database.session() as session:
        first_event_id = session.scalar(
            select(ApplicationEvent.id)
            .where(ApplicationEvent.application_id == application_id)
            .order_by(ApplicationEvent.occurred_at, ApplicationEvent.id)
            .limit(1)
        )
    if first_event_id is None:
        raise RuntimeError("The primary demo application did not produce an event history.")
    corrected = job_search.append_application_event(
        database,
        job_id,
        ApplicationEventInput(
            status=ApplicationStatus.PACKAGE_READY,
            event_type=ApplicationEventType.CORRECTION,
            occurred_at=SEEDED_AT + timedelta(days=14),
            notes="Corrected the package-ready record, then retained the original history.",
            artifact_uri=f"artifact:{recruiter_email.id}",
            corrects_event_id=first_event_id,
        ),
        workspace_id=workspace_id,
        idempotency_key="demo-application-correction",
        origin="demo_seeder",
    )
    job_search.append_application_event(
        database,
        job_id,
        ApplicationEventInput(
            status=ApplicationStatus.OFFER,
            event_type=ApplicationEventType.REVERSAL,
            occurred_at=SEEDED_AT + timedelta(days=15),
            notes="Reversed the synthetic correction while keeping the audit trail.",
            artifact_uri=f"artifact:{recruiter_email.id}",
            reverses_event_id=corrected.id,
        ),
        workspace_id=workspace_id,
        idempotency_key="demo-application-reversal",
        origin="demo_seeder",
    )

    evidence = EvidenceArtifactService(storage)
    audit = {"created_by": "demo_seeder", "origin": "demo_seeder", "correlation_id": None}
    description = "<h1>Backend Engineer</h1><p>Synthetic posting retained as HTML.</p>"
    with database.session() as session:
        evidence.store_posting_source(
            session,
            workspace_id=workspace_id,
            directory=f"{workspace_id}/demo",
            source=PostingSource(
                publisher="page",
                record=json.dumps({"@type": "JobPosting", "description": description}),
                description_markup=description,
                description_paths=("description",),
                page_html=f"<!doctype html><html><body>{description}</body></html>",
            ),
            related_entity_type="job_posting",
            related_entity_id=job_id,
            provenance={"source": "demo_seeder", "format": "invented_html"},
            audit=audit,
        )
        evidence.store_posting_source(
            session,
            workspace_id=workspace_id,
            directory=f"{workspace_id}/demo/feed",
            source=PostingSource(
                publisher="teamtailor",
                item_xml=(
                    "<item><title>Backend Engineer</title>"
                    "<link>https://initech.teamtailor.com/jobs/1</link>"
                    "<description>Synthetic posting retained as a feed item.</description></item>"
                ),
            ),
            related_entity_type="job_posting",
            related_entity_id=job_id,
            provenance={"source": "demo_seeder", "format": "invented_feed_item"},
            audit=audit,
        )
        evidence.store(
            session,
            workspace_id=workspace_id,
            logical_type="posting.source.pdf",
            content=_demo_pdf(),
            relative_path=f"{workspace_id}/demo/posting.pdf",
            mime_type="application/pdf",
            related_entity_type="job_posting",
            related_entity_id=job_id,
            provenance={"source": "demo_seeder", "format": "invented_pdf"},
            audit=audit,
        )
        evidence.store(
            session,
            workspace_id=workspace_id,
            logical_type="posting.source.image",
            content=DEMO_PNG,
            relative_path=f"{workspace_id}/demo/posting.png",
            mime_type="image/png",
            related_entity_type="job_posting",
            related_entity_id=job_id,
            provenance={"source": "demo_seeder", "format": "invented_png"},
            audit=audit,
        )
        evidence.store(
            session,
            workspace_id=workspace_id,
            logical_type="master_cv.snapshot.text",
            content=SHORT_CAREER.encode("utf-8"),
            relative_path=f"{workspace_id}/demo/master-cv.txt",
            mime_type="text/plain",
            related_entity_type="master_cv_version",
            related_entity_id=master_version_id,
            provenance={"source": "demo_seeder", "format": "plain_text_snapshot"},
            audit=audit,
        )
        documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=tailoring_run_id,
            logical_type="resume.markdown",
            mime_type="text/markdown",
            content=(
                b"# Alex Morgan\n\n## Summary\n\n"
                b"Improved a production routing endpoint from 1.8s to 240ms.\n\n"
                b"## Skills\n\nGo, Python, PostgreSQL\n"
            ),
            author_type="human",
            provenance={"source": "demo_seeder", "reason": "synthetic_review_edit"},
        )
        documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=tailoring_run_id,
            logical_type="document.rendered.pdf",
            mime_type="application/pdf",
            content=_demo_pdf(),
            author_type="system",
            provenance={"source": "demo_renderer", "format": "pdf"},
        )
        documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=tailoring_run_id,
            logical_type="document.rendered.text",
            mime_type="text/plain",
            content=b"Alex Morgan\nBackend engineer\nSynthetic rendered text.\n",
            author_type="system",
            provenance={"source": "demo_renderer", "format": "plain_text"},
        )
        documents.store_generated(
            session,
            workspace_id=workspace_id,
            job_posting_id=job_id,
            tailoring_run_id=None,
            logical_type="discovery.session.json",
            mime_type="application/json",
            content=(
                b'{"schema_version":1,"subject":"routing latency",'
                b'"closed":false,"next_question":"What changed?"}\n'
            ),
            author_type="human",
            provenance={"source": "demo_seeder", "state": "synthetic_discovery"},
        )


def _seed_task_variants(queue: TaskQueueService, workspace_id: str, job_id: str) -> None:
    """Add failed and canceled histories without running an external effect."""
    failed_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "00000000-0000-0000-0000-000000000000"},
        idempotency_key="demo-missing-artifact",
    )
    queue.finish(
        failed_id,
        worker_id="demo-seeder",
        status=TaskStatus.FAILED,
        progress={"message": "The synthetic artifact identifier was not found."},
        error_category="not_found",
        error_message="Synthetic missing artifact used to demonstrate a failed task.",
    )
    # Claimed, heartbeat recorded, never finished: the task list shows live state.
    _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id, "cover_letter": False, "regenerate": True},
        idempotency_key="demo-running-tailor",
    )
    cancelled = queue.enqueue(
        workspace_id=workspace_id,
        task_type="posting.refresh",
        payload={"job_posting_id": job_id},
        idempotency_key="demo-cancelled-refresh",
        origin="demo_seeder",
    )
    queue.request_cancellation(
        str(cancelled.id),
        workspace_id=workspace_id,
        origin="demo_seeder",
    )
    queue.enqueue(
        workspace_id=workspace_id,
        task_type="document.render",
        payload={"job_posting_id": job_id},
        idempotency_key="demo-queued-render",
        origin="demo_seeder",
    )


@dataclass(frozen=True)
class DemoReview:
    """One invented relevance verdict, keyed by the pending title it belongs to."""

    title: str
    body: str
    outcome: RelevanceReviewAnswerOutcome
    recommendation: RelevanceReviewVerdict | None
    summary: str
    core_work_alignment: str | None
    core_qualification_alignment: str | None
    hard_condition: str | None
    decisive_basis: str | None
    questions: tuple[str, ...] = ()
    cites_record: bool = True


# The campaign-staged row whose answer the same paid batch could not record.
DEMO_REJECTED_REVIEW_TITLE = "Backend Engineer, Platform"

DEMO_REVIEWS: tuple[DemoReview, ...] = (
    DemoReview(
        title="Backend Engineer, API Reliability",
        body=(
            "Build reliable backend APIs and improve production latency. "
            "The advertisement does not state its compensation range."
        ),
        outcome="judged",
        recommendation="review",
        summary="The backend reliability scope matches the record; pay is unresolved.",
        core_work_alignment="direct",
        core_qualification_alignment="established",
        hard_condition="passes",
        decisive_basis="core_match",
        questions=("What compensation range is offered?",),
    ),
    DemoReview(
        title="Backend Engineer, Developer Platform",
        body=(
            "Maintain a typed backend service and its PostgreSQL schema. "
            "Remote work is available within Germany. The team also expects "
            "experience running a streaming ingest pipeline, which the "
            "advertisement describes only in passing."
        ),
        outcome="judged",
        recommendation="investigate",
        summary="Platform ownership is on the record; the streaming ask is only partly answered.",
        core_work_alignment="direct",
        core_qualification_alignment="partially_established",
        hard_condition="passes",
        decisive_basis="transferable_match",
        questions=("How much of the role is streaming ingest?",),
    ),
    DemoReview(
        title="Backend Engineer, Data Ingest",
        body=(
            "Own a data warehouse modelling practice and the analytics roadmap "
            "behind it. The advertisement asks for dimensional modelling and "
            "reporting ownership rather than service engineering."
        ),
        outcome="judged",
        recommendation="unlikely",
        summary="The advertised core work is warehouse modelling, which the record does not hold.",
        core_work_alignment="absent",
        core_qualification_alignment="absent",
        hard_condition="passes",
        decisive_basis="core_gap",
        cites_record=False,
    ),
    DemoReview(
        title="Backend Engineer, Carrier Integrations",
        body="Backend Engineer, Carrier Integrations. Apply through the form.",
        outcome="insufficient_posting_text",
        recommendation=None,
        summary="The retained text is a listing stub and says nothing to judge.",
        core_work_alignment=None,
        core_qualification_alignment=None,
        hard_condition=None,
        decisive_basis=None,
        cites_record=False,
    ),
    DemoReview(
        title="Backend Engineer, Routing",
        body=(
            "Own the routing service and its PostgreSQL schema. The place line "
            "reads 'Berlin or Amsterdam, to be agreed', and the advertisement "
            "says payroll may run through either an employer of record or a "
            "local entity, without saying which."
        ),
        outcome="judged",
        recommendation="investigate",
        summary="The work matches the record; the place and the payroll span are both unsettled.",
        core_work_alignment="direct",
        core_qualification_alignment="established",
        hard_condition="unresolved",
        decisive_basis="logistics_unknown",
        questions=(
            "Which of the two places is the role based in?",
            "Which payroll arrangement applies?",
        ),
    ),
)


def _seed_relevance_review(
    database: Database,
    workspace_id: str,
    master_version_id: str,
    evidence_id: str,
) -> dict[str, int]:
    """Record the range of current relevance verdicts and reversible disposition events."""
    wanted = [item.title for item in DEMO_REVIEWS]
    with database.session() as session:
        rows = {
            row.title: row
            for row in session.scalars(
                select(PendingDiscovery)
                .where(
                    PendingDiscovery.workspace_id == workspace_id,
                    PendingDiscovery.title.in_([*wanted, DEMO_REJECTED_REVIEW_TITLE]),
                )
                .order_by(PendingDiscovery.title, PendingDiscovery.id)
            ).all()
        }
        rejected_row = rows.get(DEMO_REJECTED_REVIEW_TITLE)
        if rejected_row is None:
            raise RuntimeError("The demo review found no pending row to mark as rejected.")
        rejected_id = rejected_row.id
        parameter_set_id = session.scalar(
            select(JobSearchParameterSet.id)
            .where(JobSearchParameterSet.workspace_id == workspace_id)
            .order_by(JobSearchParameterSet.version.desc())
            .limit(1)
        )
        pending = [rows[title] for title in wanted if title in rows]
        for row in pending:
            session.expunge(row)
    if len(pending) != len(DEMO_REVIEWS):
        missing = sorted(set(wanted) - set(rows))
        raise RuntimeError(f"The demo discovery review found no pending row for {missing}.")
    if parameter_set_id is None:
        raise RuntimeError("The demo discovery review requires current search parameters.")

    by_title = {item.title: item for item in DEMO_REVIEWS}
    queue = TaskQueueService(database)
    task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="discovery.relevance_review",
        payload={"pending_discovery_ids": [*(item.id for item in pending), rejected_id]},
        idempotency_key="demo-relevance-review",
    )
    inputs = {
        row.id: RelevanceReviewPostingInput(
            body=by_title[row.title].body,
            fetched_at=SEEDED_AT + timedelta(days=10),
        )
        for row in pending
    }
    RelevanceReviewRecorder(database).append(
        workspace_id=workspace_id,
        task_id=task_id,
        master_cv_version_id=master_version_id,
        search_parameter_set_id=parameter_set_id,
        search_context="Synthetic demo search context.",
        contract=current_review_contract(),
        pending=pending,
        inputs=inputs,
        recommendations=[
            ValidatedRelevanceReview(
                pending_discovery_id=row.id,
                outcome=by_title[row.title].outcome,
                recommendation=by_title[row.title].recommendation,
                summary=by_title[row.title].summary,
                core_work_alignment=by_title[row.title].core_work_alignment,
                core_qualification_alignment=by_title[row.title].core_qualification_alignment,
                hard_condition=by_title[row.title].hard_condition,
                decisive_basis=by_title[row.title].decisive_basis,
                posting_citations=[
                    RelevanceReviewCitation(
                        span_id=f"{row.id}:span-0001",
                        text=inputs[row.id].body,
                        char_start=0,
                        char_end=len(inputs[row.id].body),
                    )
                ],
                master_cv_evidence_ids=[evidence_id] if by_title[row.title].cites_record else [],
                unresolved_questions=list(by_title[row.title].questions),
            )
            for row in pending
        ],
        notes={},
    )
    # One row of the same paid batch came back unusable, so it carries a mark and no verdict.
    RelevanceReviewRejectionService(database).record(
        workspace_id=workspace_id,
        task_id=task_id,
        rejected=[
            RelevanceReviewItemFailure(
                rejected_id,
                "invalid_cv_evidence",
                "The result cited evidence outside the selected master CV.",
            )
        ],
    )
    _finish_task(
        queue,
        task_id,
        progress={"reviewed": len(pending), "message": "Synthetic discovery reviews recorded."},
    )

    # The weakest verdict carries the dispute, because setting a wrong "unlikely"
    # aside is what the affordance exists for.
    disputed = rows["Backend Engineer, Data Ingest"]
    with database.session() as session:
        disputed_review = current_review_in(session, workspace_id, disputed.id)
    if disputed_review is None:
        raise RuntimeError("The demo dispute found no relevance review to set aside.")
    RelevanceDisputeService(database).dispute(
        workspace_id=workspace_id,
        pending_discovery_id=disputed.id,
        relevance_review_id=disputed_review.id,
        note="The advertisement names the ingest work the record already holds.",
        audit=audit_columns("local_user", "demo_seeder", None),
    )

    dispositions = DiscoveryDispositionService(database)

    def apply(selection: DispositionSelection, correlation_id: str) -> None:
        preview = dispositions.preview(workspace_id, selection)
        dispositions.apply(
            workspace_id,
            DispositionApply.model_validate(
                {**selection.model_dump(), "snapshot_sha256": preview.snapshot_sha256}
            ),
            actor="local_user",
            origin="demo_seeder",
            correlation_id=correlation_id,
        )

    apply(
        DispositionSelection.model_validate(
            {
                "action": "dismiss",
                "selection_mode": "explicit",
                "category": "out_of_scope_compensation",
                "pending_discovery_ids": [pending[0].id],
            }
        ),
        "demo-discovery-dismiss-current",
    )
    corrected_id = pending[1].id
    apply(
        DispositionSelection.model_validate(
            {
                "action": "dismiss",
                "selection_mode": "explicit",
                "category": "other",
                "reason": "Initially set aside during the synthetic review.",
                "pending_discovery_ids": [corrected_id],
            }
        ),
        "demo-discovery-dismiss-corrected",
    )
    apply(
        DispositionSelection.model_validate(
            {
                "action": "restore",
                "selection_mode": "explicit",
                "category": "human_restore",
                "reason": "Restored after a second review found enough evidence to investigate.",
                "pending_discovery_ids": [corrected_id],
            }
        ),
        "demo-discovery-restore",
    )
    return {
        "relevance_reviews": len(pending),
        "relevance_review_disputes": 1,
        "relevance_review_rejections": 1,
    }


def _seed_pending_content(
    database: Database,
    storage: RuntimeStorage,
    scans: SourceScanService,
    workspace_id: str,
) -> dict[str, int]:
    """Retain invented posting bodies for the rows a review or evaluation would read."""
    active = sorted(scans.list_pending_discoveries(workspace_id), key=lambda item: item.title)
    # The tariff-engine row is the one the resolver cannot read, so it is always
    # selected: remediation and evaluate-from-pending both need an unreadable row.
    needs_remediation = [item for item in active if item.discovery_url.endswith("/tariff-engine")]
    selected = list(dict.fromkeys(item.id for item in [*active[:7], *needs_remediation]))
    if len(selected) < 8:
        raise RuntimeError("The demo scan left too few active pending discoveries to resolve.")
    resolution = PostingContentResolutionService(database, DemoContentResolver(), storage=storage)
    report = resolution.resolve_automatic(workspace_id, selected, origin="demo_seeder")
    return {"pending_posting_contents": int(report["attempted"])}


REFUSAL_CONDITION = "payroll_arrangement"
REFUSAL_REASON = (
    "The advertisement states payroll through an employer of record, which this "
    "search does not accept."
)


def _seed_refusals(
    database: Database, scans: SourceScanService, workspace_id: str
) -> dict[str, int]:
    """File repeated rule refusals so both the refusal ledger and its proposals fill.

    Three refusals share one condition and reason, which is what the default
    learning-loop threshold aggregates into a hard-gate proposal.
    """
    with database.session() as session:
        parameter_set_id = session.scalar(
            select(JobSearchParameterSet.id)
            .where(JobSearchParameterSet.workspace_id == workspace_id)
            .order_by(JobSearchParameterSet.version.desc())
            .limit(1)
        )
    active = sorted(scans.list_pending_discoveries(workspace_id), key=lambda item: item.title)
    refused = active[-3:]
    if len(refused) != 3:
        raise RuntimeError("The demo refusal ledger needs three active pending rows.")
    for item in refused:
        file_refusal(
            database,
            RefusedPosting(
                workspace_id=workspace_id,
                employer=item.employer,
                title=item.title,
                pending_discovery_id=item.id,
                search_parameter_set_id=parameter_set_id,
            ),
            condition=REFUSAL_CONDITION,
            reason=REFUSAL_REASON,
            task_id=None,
            origin="demo_seeder.refusal",
        )
    return {"posting_refusals": len(refused) + 1}


def _seed_workflow_history(
    database: Database,
    storage: RuntimeStorage,
    workspace_id: str,
    master_version_id: str,
    evidence_id: str,
    job_ids: dict[str, str],
    prompt_version_id: str,
) -> dict[str, int]:
    """Populate completed tasks, document review, spend, and a batch comparison."""
    queue = TaskQueueService(database)
    workflow = WorkflowService(database, storage)
    primary_job_id = job_ids["kestrel-routing"]
    tailor_task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={
            "job_posting_id": primary_job_id,
            "cover_letter": True,
            "regenerate": False,
        },
        idempotency_key="demo-tailor",
    )
    proposal_text = "Improved a production routing endpoint from 1.8s to 240ms."
    database_text = "Used PostgreSQL query plans to guide a measured performance change."
    ownership_text = "Owned company-wide routing strategy."
    review = {
        "proposals": [
            {
                "id": "summary-performance",
                "slot": "summary",
                "text": proposal_text,
                "evidence_ids": [evidence_id],
                "digest": "a" * 64,
                "kind": "rewrite",
                "rationale": "Lead with the closest measured result.",
            },
            {
                "id": "skills-database",
                "slot": "skills",
                "text": database_text,
                "evidence_ids": [evidence_id],
                "digest": "b" * 64,
                "kind": "rewrite",
                "rationale": "Connect the named database skill to the measured result.",
            },
            {
                "id": "summary-ownership",
                "slot": "summary",
                "text": ownership_text,
                "evidence_ids": [evidence_id],
                "digest": "c" * 64,
                "kind": "rewrite",
                "rationale": "Demonstrate a proposal that overstates the evidence.",
            },
        ]
    }
    tailored = workflow.persist_tailoring(
        workspace_id=workspace_id,
        job_id=primary_job_id,
        master_version_id=master_version_id,
        task_id=tailor_task_id,
        output=TailorOutput(
            artifacts=SerializedRunArtifacts(
                document=(
                    f"# Alex Morgan\n\n## Summary\n\n{proposal_text}\n\n"
                    f"{database_text}\n\n{ownership_text}\n"
                ).encode(),
                report=b"# Tailoring report\n\nEvidence checks passed.\n",
                manifest_json=b'{"schema_version": 1, "status": "passed"}\n',
                manifest={
                    "inputs": {
                        "prompt_suite": "demo-suite-v1",
                        "cv_hash": "b" * 64,
                        "settings_hash": "c" * 64,
                    }
                },
                cover_letter=(
                    b"# Cover letter\n\nI am applying for the routing role at "
                    b"Kestrel Freight Systems.\n"
                ),
                review_json=(json.dumps(review, sort_keys=True) + "\n").encode(),
                review_markdown=(b"# Review\n\nOne evidence-linked change is ready for review.\n"),
                review=review,
            ),
            outcome=TailoringOutcome.PASSED,
            ledger=_ledger("draft", prompts=("draft_system", "draft_user")),
        ),
        started_at=SEEDED_AT + timedelta(days=2),
        prompt_version_ids=(prompt_version_id,),
        research=(
            ResearchSource(
                id="company.posting",
                kind="posting",
                url="https://example.test/kestrel/jobs/routing",
                text="The invented posting describes a routing service and its latency budget.",
                method="demo_seeder",
            ),
            ResearchSource(
                id="company.careers",
                kind="careers",
                url="https://example.test/kestrel/careers",
                text="The invented careers page describes a small product engineering team.",
                method="demo_seeder",
            ),
            ResearchSource(
                id="company.about",
                kind="about",
                url="https://example.test/kestrel/about",
                text="The invented company page says Kestrel builds regional routing software.",
                method="demo_seeder",
            ),
        ),
    )
    _finish_task(
        queue,
        tailor_task_id,
        progress={"message": "Resume and cover letter are ready for review."},
    )
    with database.session() as session:
        proposals = session.scalars(
            select(ReviewProposal).where(
                ReviewProposal.tailoring_run_id == tailored["tailoring_run_id"]
            )
        ).all()
    by_key = {proposal.proposal_key: proposal for proposal in proposals}
    if set(by_key) != {"summary-performance", "skills-database", "summary-ownership"}:
        raise RuntimeError("The demo tailoring run did not produce its review proposals.")
    proposal = by_key["summary-performance"]
    ApprovalService(database).decide(
        proposal.id,
        workspace_id,
        proposal_digest=proposal.digest,
        decision="accept",
        replacement_text=None,
        replacement_source_ids=[],
        correlation_id=tailor_task_id,
    )
    proposal = by_key["skills-database"]
    ApprovalService(database).decide(
        proposal.id,
        workspace_id,
        proposal_digest=proposal.digest,
        decision="replace",
        replacement_text="Used PostgreSQL query plans to improve routing latency.",
        replacement_source_ids=[evidence_id],
        correlation_id=tailor_task_id,
    )
    proposal = by_key["summary-ownership"]
    ApprovalService(database).decide(
        proposal.id,
        workspace_id,
        proposal_digest=proposal.digest,
        decision="reject",
        replacement_text=None,
        replacement_source_ids=[],
        correlation_id=tailor_task_id,
    )

    batch_job_ids = (job_ids["kestrel-routing"], job_ids["nordlicht-payments"])
    batch_task_id = _start_task(
        queue,
        workspace_id=workspace_id,
        task_type="workflow.batch",
        payload={
            "job_posting_ids": list(batch_job_ids),
            "priorities": [1, 2],
            "notes": ["Closest evidence match", "Strong platform alternative"],
        },
        idempotency_key="demo-batch",
    )
    workflow.persist_batch(
        workspace_id=workspace_id,
        job_ids=batch_job_ids,
        task_id=batch_task_id,
        output=BatchOutput(
            report=b"# Batch comparison\n\nKestrel is the stronger evidence match.\n",
            record=b'{"schema_version": 1, "recommended_position": 1}\n',
            screenings=(
                ScreenOutput(
                    b"# Kestrel screening\n\nPursue.\n",
                    "pursue",
                    _ledger("match", prompts=("match_system", "match_user")),
                ),
                ScreenOutput(b"# Nordlicht screening\n\nConsider.\n", "consider", Ledger()),
            ),
        ),
        priorities=(1, 2),
        notes=("Closest evidence match", "Strong platform alternative"),
        prompt_version_ids=(prompt_version_id,),
    )
    _finish_task(queue, batch_task_id, progress={"job_posting_ids": list(batch_job_ids)})
    _seed_artifact_catalog(
        database,
        storage,
        workspace_id,
        master_version_id,
        primary_job_id,
        str(tailored["tailoring_run_id"]),
    )
    _seed_task_variants(queue, workspace_id, primary_job_id)
    return {
        "tailoring_runs": 1,
        "batch_runs": 1,
        "review_decisions": 3,
        "artifact_types": len(ARTIFACT_LOGICAL_TYPES),
    }


def seed(data_root: Path, *, force: bool) -> dict[str, int]:
    """Write the whole invented workspace and return what was written."""
    # Use the requested root so environment settings cannot redirect demo data.
    root = data_root.expanduser().resolve()
    database_url = f"sqlite+pysqlite:///{root / 'clearscout.sqlite3'}"
    if status(database_url).database_exists and not force:
        raise SystemExit(
            f"{root} already holds a ClearScout database.\n"
            "  Point --data-root at an empty throwaway directory, or pass --force "
            "if you are certain this is not your real workspace."
        )
    upgrade(database_url)
    storage = RuntimeStorage.resolve(database_url, data_root=root)
    database = Database(database_url)

    workspace = WorkspaceService(database, storage).create(
        slug=WORKSPACE_SLUG, name=WORKSPACE_NAME, origin="demo_seeder"
    )
    workspace_id = workspace.id
    parameters = SearchParametersService(database)
    parameters.save(workspace_id, JobSearchParameters(), origin="demo_seeder")
    parameters.save(workspace_id, demo_parameters(), origin="demo_seeder")
    master_cv = MasterCvService(database, storage)
    for draft in CV_DRAFTS:
        master_cv.import_source(
            workspace_id=workspace_id,
            filename="CV_Master.md",
            content=draft.encode("utf-8"),
            replace=True,
            origin="demo_seeder",
        )
    imported_cv = master_cv.import_source(
        workspace_id=workspace_id,
        filename="CV_Master.md",
        content=SHORT_CAREER.encode("utf-8"),
        replace=True,
        origin="demo_seeder",
    )
    entries = parse_cv(Path("CV_Master.md"), text=SHORT_CAREER).entries
    evidence_id = entries[0].id
    prompt_version_id = _seed_workspace_configuration(database, workspace_id)

    artifacts = EvidenceArtifactService(storage)
    job_ids: dict[str, str] = {}
    events = 0
    for job in DEMO_JOBS:
        entry_id = next(item.id for item in entries if job.cv_quote in item.text)
        committed = job_search.commit_evaluation(
            database,
            artifacts,
            _proposal(job, workspace_id, SHORT_CAREER, entry_id),
            origin="demo_seeder",
        )
        job_ids[job.slug] = str(committed.job_posting_id)
        for index, (application_status, note) in enumerate(job.events):
            job_search.append_application_event(
                database,
                job_ids[job.slug],
                ApplicationEventInput(
                    status=application_status,
                    occurred_at=SEEDED_AT + timedelta(days=3 * index + 1),
                    notes=note,
                ),
                workspace_id=workspace_id,
                idempotency_key=f"{job.slug}-{index}",
                origin="demo_seeder",
            )
            events += 1

    with database.session() as session:
        first_evaluation_id = session.scalar(
            select(JobEvaluation.id)
            .where(JobEvaluation.job_posting_id == job_ids["kestrel-routing"])
            .order_by(JobEvaluation.version_number.desc())
            .limit(1)
        )
    if first_evaluation_id is None:
        raise RuntimeError("The primary demo job did not produce its first evaluation.")
    revised = _proposal(
        DEMO_JOBS[0],
        workspace_id,
        SHORT_CAREER,
        next(item.id for item in entries if DEMO_JOBS[0].cv_quote in item.text),
    ).model_copy(
        update={
            "supersedes_evaluation_id": first_evaluation_id,
            "evaluated_at": SEEDED_AT + timedelta(days=8),
            "rationale": (
                DEMO_JOBS[0].rationale
                + " A later review confirmed the same evidence-backed priority."
            ),
        }
    )
    job_search.commit_evaluation(database, artifacts, revised, origin="demo_seeder")

    records = WorkspaceRecords.for_database(database)
    known_tags = {item.name for item in records.tags.list_tags(workspace_id)}
    for name, color in DEMO_TAGS:
        if name not in known_tags:
            records.tags.create_tag(workspace_id=workspace_id, name=name, color=color)
    tag_ids = {item.name: str(item.id) for item in records.tags.list_tags(workspace_id)}
    for name, job_slug in TAG_ASSIGNMENTS:
        records.tags.assign_tag(
            tag_id=tag_ids[name],
            workspace_id=workspace_id,
            entity_type="job_posting",
            entity_id=job_ids[job_slug],
        )

    known_notes = {item.title for item in records.notes.list_notes(workspace_id)}
    for job_slug, title, body in DEMO_NOTES:
        if title in known_notes:
            continue
        records.notes.create_note(
            workspace_id=workspace_id,
            entity_type="job_posting" if job_slug else "workspace",
            entity_id=job_ids[job_slug] if job_slug else None,
            title=title,
            body=body,
            pinned=not job_slug,
        )

    scans = SourceScanService(
        database,
        storage=storage,
        posting_fetcher=DemoPostingFetcher(),
        fetcher=DemoBoards(),
    )
    _seed_search_state(
        database,
        storage,
        scans,
        workspace_id,
        imported_cv.master_cv_version_id,
        evidence_id,
        job_ids,
    )
    campaign_counts = _seed_guided_campaign(database, scans, workspace_id)
    content_counts = _seed_pending_content(database, storage, scans, workspace_id)
    discovery_review_counts = _seed_relevance_review(
        database,
        workspace_id,
        imported_cv.master_cv_version_id,
        evidence_id,
    )
    refusal_counts = _seed_refusals(database, scans, workspace_id)
    _seed_missed_requirement_texts(database, workspace_id, imported_cv.master_cv_version_id)
    _seed_spend(database, workspace_id, list(job_ids.values()))
    # Last, because it leaves a queued and a running task no later claim may take.
    workflow_counts = _seed_workflow_history(
        database,
        storage,
        workspace_id,
        imported_cv.master_cv_version_id,
        evidence_id,
        job_ids,
        prompt_version_id,
    )
    with database.session() as session:
        evaluation_count = session.scalar(
            select(func.count(JobEvaluation.id))
            .join(JobPosting, JobPosting.id == JobEvaluation.job_posting_id)
            .where(JobPosting.workspace_id == workspace_id)
        )

    database.dispose()
    return {
        "jobs": len(job_ids),
        "evaluations": int(evaluation_count or 0),
        "application_events": events + 2,
        "tags": len(DEMO_TAGS),
        "tag_assignments": len(TAG_ASSIGNMENTS),
        "notes": len(DEMO_NOTES),
        "master_cv_versions": len(CV_DRAFTS) + 1,
        "sources": 3,
        "source_scans": 3,
        "source_scan_outcomes": 29,
        "source_scan_continuations": 1,
        "source_scan_frontier_items": 3,
        "pending_discoveries": 22,
        "pending_discovery_disposition_events": 6,
        "missed_requirement_texts": 9,
        "search_proposals": 8,
        "requirement_rulings": 1,
        "posting_observation_diffs": 1,
        "saved_views": 1,
        "guardrail_versions": 2,
        "learning_loop_policy_versions": 1,
        "prompt_versions": 2,
        "refusal_pattern_proposals": 1,
        "model_calls": 13,
        "tasks": 14,
        **campaign_counts,
        **content_counts,
        **discovery_review_counts,
        **refusal_counts,
        **workflow_counts,
    }


def main() -> None:
    """Seed one throwaway data root named explicitly on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        required=True,
        help="Throwaway application data root to fill. Never your real one.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Seed even though this data root already holds a database.",
    )
    arguments = parser.parse_args()
    counts = seed(arguments.data_root, force=arguments.force)
    print(f"Seeded {arguments.data_root} with invented data:")
    for name, value in counts.items():
        print(f"  {name}: {value}")


if __name__ == "__main__":
    main()
