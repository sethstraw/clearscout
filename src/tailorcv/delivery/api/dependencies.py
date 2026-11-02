"""Application state and request-scoped service dependencies."""

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request

from tailorcv.application.config import Settings, load_settings
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.discovery.content_resolution import PostingContentResolutionService
from tailorcv.application.discovery.proposals import (
    MasterCvProposalService,
    RefusalPatternProposalService,
    SearchArchetypeProposalService,
    SearchClusterProposalService,
    SearchTitleProposalService,
)
from tailorcv.application.discovery.scan_queries import SourceScanQueries
from tailorcv.application.documents import DocumentService
from tailorcv.application.documents.approvals import ApprovalService
from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.application.failures import UnavailableError, ValidationError
from tailorcv.application.job_search.score_policies import ScorePolicyService
from tailorcv.application.master_cv import MasterCvService
from tailorcv.application.tailoring.workflows import WorkflowService
from tailorcv.application.tailoring.workflows.evaluations import EvaluationWorkflowService
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import (
    GuardrailService,
    LearningLoopMetricsService,
    LearningLoopPolicyService,
    SearchParametersService,
    WorkspaceService,
)
from tailorcv.application.workspaces.prompt_library import PromptLibraryService
from tailorcv.application.workspaces.records import WorkspaceRecords
from tailorcv.application.workspaces.scan_schedule import ScanScheduleService
from tailorcv.application.workspaces.spend import SpendService
from tailorcv.application.workspaces.spend_reconciliation import SpendReconciliationService
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.migration import MigrationStatus
from tailorcv.infrastructure.persistence.migration import status as migration_status
from tailorcv.infrastructure.providers.billing import OpenAIBillingClient, OrganizationBilling
from tailorcv.infrastructure.providers.credentials import KeyStore, find_admin_key
from tailorcv.infrastructure.web_fetch.company_research import CompanyResearchFetcher
from tailorcv.infrastructure.web_fetch.ingest.url import USER_AGENT
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient
from tailorcv.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher
from tailorcv.infrastructure.web_fetch.posting_resolution import (
    DeterministicPostingContentResolver,
)
from tailorcv.shared.costs.prices import PriceCatalog, published_catalog
from tailorcv.shared.errors import ConfigError


class ApiState:
    """Application-scoped resources shared by request dependencies."""

    def __init__(
        self,
        storage: RuntimeStorage,
        key_store: KeyStore,
        settings: Settings | None,
    ) -> None:
        """Capture resources that share the application's lifetime."""
        self.storage = storage
        self.configured_schema = _migration(storage.database_url)
        self.opened_database: Database | None = None
        self.database_lock = threading.Lock()
        self.key_store = key_store
        self.settings = settings
        self.frontend_directory: Path | None = None
        # Every web reader shares one client, and with it each host's robots and pacing.
        self.http_client = PoliteHttpClient(user_agent=USER_AGENT)
        self.posting_fetcher = PolitePostingFetcher(client=self.http_client)


def api_state(request: Request) -> ApiState:
    """Return the resources owned by the current FastAPI application."""
    state = getattr(request.app.state, "tailorcv", None)
    if not isinstance(state, ApiState):
        raise RuntimeError("TailorCV API state was not configured.")
    return state


def correlation_id(request: Request) -> str:
    """Return the correlation identifier assigned by boundary middleware."""
    return str(request.state.correlation_id)


WorkspaceId = Annotated[str, Query(min_length=1)]
CorrelationId = Annotated[str, Depends(correlation_id)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, pattern=r".*\S.*")]


def schema_state(state: ApiState) -> MigrationStatus | None:
    """Re-read a stale migration state so an upgrade needs no server restart."""
    schema = state.configured_schema
    if schema is None or not schema.current:
        schema = _migration(state.storage.database_url)
        state.configured_schema = schema
    return schema


def database_for(state: ApiState) -> Database:
    """Return the lazily opened database for an initialized schema."""
    schema = schema_state(state)
    if schema is None or not schema.current:
        raise UnavailableError(
            "Database schema is unavailable or outdated; run 'tailorcv db upgrade' explicitly."
        )
    with state.database_lock:
        if state.opened_database is None:
            state.opened_database = Database(state.storage.database_url)
        return state.opened_database


def provide_database(state: Annotated[ApiState, Depends(api_state)]) -> Database:
    """Open the configured database after confirming its migration state."""
    return database_for(state)


def provide_artifacts(
    state: Annotated[ApiState, Depends(api_state)],
) -> EvidenceArtifactService:
    """Open the evidence artifact store rooted in the configured runtime storage."""
    return EvidenceArtifactService(state.storage)


def provide_workspaces(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> WorkspaceService:
    """Build the workspace application service for one request."""
    return WorkspaceService(database, state.storage, settings=provide_settings(state))


def provide_master_cvs(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> MasterCvService:
    """Build the master-CV application service for one request."""
    return MasterCvService(database, state.storage)


def provide_source_scans(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> SourceScanService:
    """Build the source-scan application service for one request."""
    return SourceScanService(
        database,
        storage=state.storage,
        posting_fetcher=state.posting_fetcher,
        http_client=state.http_client,
    )


def provide_scan_schedule(
    database: Annotated[Database, Depends(provide_database)],
) -> ScanScheduleService:
    """Build the workspace's free local-calendar scan owner."""
    return ScanScheduleService(database)


def provide_posting_resolution(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> PostingContentResolutionService:
    """Build deterministic pending-posting content recovery for one request."""
    return PostingContentResolutionService(
        database,
        DeterministicPostingContentResolver(client=state.http_client),
        storage=state.storage,
    )


def provide_search_title_proposals(
    database: Annotated[Database, Depends(provide_database)],
) -> SearchTitleProposalService:
    """Build the search-title proposal inbox for one request."""
    return SearchTitleProposalService(database)


def provide_search_archetype_proposals(
    database: Annotated[Database, Depends(provide_database)],
) -> SearchArchetypeProposalService:
    """Build the employer-archetype proposal inbox for one request."""
    return SearchArchetypeProposalService(database)


def provide_refusal_pattern_proposals(
    database: Annotated[Database, Depends(provide_database)],
) -> RefusalPatternProposalService:
    """Build the refusal-pattern proposal inbox for one request."""
    return RefusalPatternProposalService(database)


def provide_master_cv_proposals(
    database: Annotated[Database, Depends(provide_database)],
) -> MasterCvProposalService:
    """Build the master-CV proposal inbox for one request."""
    return MasterCvProposalService(database)


def provide_search_cluster_proposals(
    database: Annotated[Database, Depends(provide_database)],
) -> SearchClusterProposalService:
    """Build the capability and domain proposal inbox for one request."""
    return SearchClusterProposalService(database)


def provide_score_policies(
    database: Annotated[Database, Depends(provide_database)],
) -> ScorePolicyService:
    """Build the scoring-policy application service for one request."""
    return ScorePolicyService(database)


def provide_spend(
    database: Annotated[Database, Depends(provide_database)],
) -> SpendService:
    """Build the spend-reporting application service for one request."""
    return SpendService(database)


def provide_openai_billing() -> OrganizationBilling | None:
    """Build an OpenAI billing reader given an admin key; ``None`` reads as unavailable."""
    admin_key = find_admin_key()
    return OpenAIBillingClient(admin_key) if admin_key else None


def provide_price_catalog() -> PriceCatalog:
    """Build the published price-data source a refresh request reads from."""
    return published_catalog()


def provide_spend_reconciliation(
    database: Annotated[Database, Depends(provide_database)],
    billing: Annotated["OrganizationBilling | None", Depends(provide_openai_billing)],
) -> SpendReconciliationService:
    """Build the OpenAI spend-reconciliation service for one request."""
    return SpendReconciliationService(database, billing)


def provide_tasks(
    database: Annotated[Database, Depends(provide_database)],
) -> TaskQueueService:
    """Build the task-queue application service for one request."""
    return TaskQueueService(database)


def provide_documents(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> DocumentService:
    """Build the document application service for one request."""
    return DocumentService(database, state.storage)


def provide_approvals(
    database: Annotated[Database, Depends(provide_database)],
) -> ApprovalService:
    """Build the human-approval application service for one request."""
    return ApprovalService(database)


def provide_evaluation_workflows(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> EvaluationWorkflowService:
    """Build evaluation preparation over retained workspace content."""
    return EvaluationWorkflowService(
        database, state.storage, fetcher=state.posting_fetcher, http_client=state.http_client
    )


def provide_workflows(
    database: Annotated[Database, Depends(provide_database)],
    state: Annotated[ApiState, Depends(api_state)],
) -> WorkflowService:
    """Build the workflow application service for one request."""
    return WorkflowService(
        database, state.storage, research_fetcher=CompanyResearchFetcher(client=state.http_client)
    )


def provide_records(
    database: Annotated[Database, Depends(provide_database)],
) -> WorkspaceRecords:
    """Build the four workspace-record services for one request."""
    return WorkspaceRecords.for_database(database)


def provide_search_parameters(
    database: Annotated[Database, Depends(provide_database)],
) -> SearchParametersService:
    """Build the search-parameter application service for one request."""
    return SearchParametersService(database)


def provide_guardrails(
    database: Annotated[Database, Depends(provide_database)],
) -> GuardrailService:
    """Build the workspace guardrail service for one request."""
    return GuardrailService(database)


def provide_learning_loop_metrics(
    database: Annotated[Database, Depends(provide_database)],
) -> LearningLoopMetricsService:
    """Build the learning-loop metrics reader for one request."""
    return LearningLoopMetricsService(database, SourceScanQueries(database))


def provide_learning_loop_policy(
    database: Annotated[Database, Depends(provide_database)],
) -> LearningLoopPolicyService:
    """Build the workspace learning-loop policy service for one request."""
    return LearningLoopPolicyService(database)


def provide_prompt_library(
    database: Annotated[Database, Depends(provide_database)],
) -> PromptLibraryService:
    """Build the prompt-library application service for one request."""
    return PromptLibraryService(database)


def provide_settings(state: Annotated[ApiState, Depends(api_state)]) -> Settings:
    """Return the settings in force, or refuse while the file is unreadable."""
    if state.settings is not None:
        return state.settings
    try:
        return load_settings()
    except ConfigError as error:
        raise UnavailableError(str(error)) from error


# Route signatures use these aliases as the typed HTTP dependency vocabulary.
ApiStateDep = Annotated[ApiState, Depends(api_state)]
ApprovalServiceDep = Annotated[ApprovalService, Depends(provide_approvals)]
ArtifactServiceDep = Annotated[EvidenceArtifactService, Depends(provide_artifacts)]
DatabaseDep = Annotated[Database, Depends(provide_database)]
DocumentServiceDep = Annotated[DocumentService, Depends(provide_documents)]
GuardrailServiceDep = Annotated[GuardrailService, Depends(provide_guardrails)]
LearningLoopMetricsServiceDep = Annotated[
    LearningLoopMetricsService, Depends(provide_learning_loop_metrics)
]
LearningLoopPolicyServiceDep = Annotated[
    LearningLoopPolicyService, Depends(provide_learning_loop_policy)
]
MasterCvServiceDep = Annotated[MasterCvService, Depends(provide_master_cvs)]
PromptLibraryServiceDep = Annotated[PromptLibraryService, Depends(provide_prompt_library)]
PriceCatalogDep = Annotated[PriceCatalog, Depends(provide_price_catalog)]
PostingResolutionServiceDep = Annotated[
    PostingContentResolutionService, Depends(provide_posting_resolution)
]
RecordsServiceDep = Annotated[WorkspaceRecords, Depends(provide_records)]
RefusalPatternProposalServiceDep = Annotated[
    RefusalPatternProposalService, Depends(provide_refusal_pattern_proposals)
]
SearchParametersServiceDep = Annotated[SearchParametersService, Depends(provide_search_parameters)]
SettingsDep = Annotated[Settings, Depends(provide_settings)]
ScorePolicyServiceDep = Annotated[ScorePolicyService, Depends(provide_score_policies)]
SearchArchetypeProposalServiceDep = Annotated[
    SearchArchetypeProposalService, Depends(provide_search_archetype_proposals)
]
MasterCvProposalServiceDep = Annotated[
    MasterCvProposalService, Depends(provide_master_cv_proposals)
]
SearchClusterProposalServiceDep = Annotated[
    SearchClusterProposalService, Depends(provide_search_cluster_proposals)
]
SearchTitleProposalServiceDep = Annotated[
    SearchTitleProposalService, Depends(provide_search_title_proposals)
]
SourceScanServiceDep = Annotated[SourceScanService, Depends(provide_source_scans)]
ScanScheduleServiceDep = Annotated[ScanScheduleService, Depends(provide_scan_schedule)]
SpendServiceDep = Annotated[SpendService, Depends(provide_spend)]
SpendReconciliationServiceDep = Annotated[
    SpendReconciliationService, Depends(provide_spend_reconciliation)
]
TaskQueueServiceDep = Annotated[TaskQueueService, Depends(provide_tasks)]
WorkflowServiceDep = Annotated[WorkflowService, Depends(provide_workflows)]
EvaluationWorkflowServiceDep = Annotated[
    EvaluationWorkflowService, Depends(provide_evaluation_workflows)
]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(provide_workspaces)]


async def bounded_body(request: Request, limit: int) -> bytes:
    """Read a request body without buffering beyond its declared service limit."""
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ValidationError(f"Uploads are limited to {limit} bytes.")
        except ValueError as error:
            raise ValidationError("Content-Length must be an integer.") from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ValidationError(f"Uploads are limited to {limit} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Close web connections and dispose the opened database when the application stops."""
    yield
    state = getattr(app.state, "tailorcv", None)
    if not isinstance(state, ApiState):
        return
    state.http_client.close()
    if state.opened_database is not None:
        state.opened_database.dispose()


def _migration(database_url: str) -> MigrationStatus | None:
    try:
        return migration_status(database_url)
    except Exception:  # noqa: BLE001 - translated without exposing the connection string
        return None
