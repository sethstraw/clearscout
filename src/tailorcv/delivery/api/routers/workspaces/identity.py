"""Workspace identity and command-center routes."""

from __future__ import annotations

from fastapi import APIRouter

from tailorcv.application import job_search
from tailorcv.application.discovery.relevance_review import REVIEW_BATCH_ITEMS
from tailorcv.application.discovery.starter_sources import StarterSourceInstaller
from tailorcv.application.job_search.models import WorkspaceSummaryResponse
from tailorcv.application.workspaces import (
    GuidedCampaignConfigurationResponse,
    RelevanceReviewConfigurationResponse,
    SearchVocabularyConfigurationResponse,
    WorkspaceResponse,
    WorkspaceSettingsService,
)
from tailorcv.delivery.api.constants import API_PREFIX
from tailorcv.delivery.api.dependencies import (
    CorrelationId,
    DatabaseDep,
    SourceScanServiceDep,
    WorkspaceServiceDep,
)
from tailorcv.delivery.api.models.workspaces import WorkspaceCreate

router = APIRouter()


@router.get(f"{API_PREFIX}/workspaces", response_model=list[WorkspaceResponse], tags=["workspaces"])
def list_workspaces(service: WorkspaceServiceDep) -> list[WorkspaceResponse]:
    """List active workspaces."""
    return service.list()


@router.post(
    f"{API_PREFIX}/workspaces",
    response_model=WorkspaceResponse,
    status_code=201,
    tags=["workspaces"],
)
def create_workspace(
    correlation_id: CorrelationId,
    body: WorkspaceCreate,
    service: WorkspaceServiceDep,
    sources: SourceScanServiceDep,
) -> WorkspaceResponse:
    """Create a workspace, its seeded parameter state, and the shipped aggregators."""
    workspace = service.create(
        slug=body.slug,
        name=body.name,
        root_path=body.root_path,
        provider=body.provider,
        correlation_id=correlation_id,
    )
    StarterSourceInstaller(sources).install(
        str(workspace.id), origin="api", correlation_id=correlation_id
    )
    return workspace


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/summary",
    response_model=WorkspaceSummaryResponse,
    tags=["workspaces"],
    summary="Read the complete command-center summary",
)
def workspace_summary(
    workspace_id: str,
    database: DatabaseDep,
) -> WorkspaceSummaryResponse:
    """Read the command-center summary for one workspace."""
    return job_search.workspace_summary(database, workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/guided-campaign-configuration",
    response_model=GuidedCampaignConfigurationResponse,
    tags=["workspaces"],
    summary="Read the effective guided-campaign provider choices",
)
def guided_campaign_configuration(
    workspace_id: str,
    database: DatabaseDep,
) -> GuidedCampaignConfigurationResponse:
    """Read the provider choices that a guided-campaign task will execute."""
    return WorkspaceSettingsService(database).guided_campaign_configuration(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/search-vocabulary-configuration",
    response_model=SearchVocabularyConfigurationResponse,
    tags=["workspaces"],
    summary="Read the effective search-vocabulary provider choices",
)
def search_vocabulary_configuration(
    workspace_id: str,
    database: DatabaseDep,
) -> SearchVocabularyConfigurationResponse:
    """Read the provider choices that a search-vocabulary task will execute."""
    return WorkspaceSettingsService(database).search_vocabulary_configuration(workspace_id)


@router.get(
    f"{API_PREFIX}/workspaces/{{workspace_id}}/relevance-review-configuration",
    response_model=RelevanceReviewConfigurationResponse,
    tags=["workspaces"],
    summary="Read the effective relevance-review provider choices",
)
def relevance_review_configuration(
    workspace_id: str,
    database: DatabaseDep,
) -> RelevanceReviewConfigurationResponse:
    """Read the provider choices that a relevance-review task will execute."""
    return WorkspaceSettingsService(database).relevance_review_configuration(
        workspace_id, REVIEW_BATCH_ITEMS
    )
