"""Search parameter and guided campaign routes: request validation and response shape."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clearscout.application import prompts
from clearscout.application.discovery import guided_campaign_report
from clearscout.application.discovery.guided_campaign import (
    DEFAULT_MAX_HOSTED_SEARCHES,
    DEFAULT_MAX_MODEL_CALLS,
    BranchKind,
    CampaignAttemptPlan,
    CampaignBounds,
    CampaignBranch,
    CampaignIdentities,
    CampaignManifest,
)
from clearscout.application.failures import NotFoundError
from clearscout.application.workspaces.prompt_library import PromptLibraryService
from clearscout.bootstrap.api import create_app
from clearscout.domain.campaigns import AttemptState, CampaignReason, CampaignStatus
from clearscout.infrastructure.persistence.database import Database


def test_search_parameter_options_publish_every_compensation_basis(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        response = client.get("/api/v1/job-search-parameters/options")

    assert response.status_code == 200
    assert response.json()["bases"] == ["annual", "monthly", "daily", "hourly"]


def test_search_parameter_options_publish_the_vocabulary_caps(
    durable_database_url: str, tmp_path: Path
) -> None:
    """The form discloses which entries are used, so it needs the same numbers the code applies."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        response = client.get("/api/v1/job-search-parameters/options")

    assert response.json()["caps"] == {
        "titles": 8,
        "archetypes": 6,
        "capabilities": 5,
        "domains": 5,
        "cluster_terms": 10,
    }


def _fake_campaign_manifest() -> CampaignManifest:
    identities = CampaignIdentities(
        search_parameter_set_id="param-set-1",
        search_parameter_sha256="a" * 64,
        master_cv_version_id="cv-1",
        master_cv_sha256="b" * 64,
        accepted_vocabulary_sha256="c" * 64,
        admission_scope_sha256="e" * 64,
        provider="openai",
        model="gpt-5.6-terra",
    )
    branch = CampaignBranch(
        key="d" * 64,
        kind=BranchKind.TITLE_TOPIC,
        priority_family="leadership",
        protected=True,
        markets=("Canada",),
        market_arrangements=(("Canada", ("remote",)),),
        lane=None,
        seniority="director",
        title_aliases=("Clinical Data Product Manager",),
        focuses=(),
        floor_query="Clinical Data Product Manager Canada",
        anchors=("Clinical Data Product Manager",),
        label="Clinical Data Product Manager",
    )
    attempt = CampaignAttemptPlan(position=1, branch_keys=(branch.key,), search_cap=5)
    return CampaignManifest(
        identities=identities,
        compiler_version="gap-campaign-v1",
        branches=(branch,),
        attempts=(attempt,),
        bounds=CampaignBounds(),
    )


def test_guided_campaign_preview_reports_the_compiled_manifest_without_writing_history(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _fake_campaign_manifest()
    preview = guided_campaign_report.CampaignPreview(
        manifest=manifest,
        cycle=1,
        prior_outcomes={},
        provider="openai",
        model="gpt-5.6-terra",
        supersedes_campaign_id=None,
        last_searched_at={manifest.branches[0].key: datetime(2026, 9, 1, 12, tzinfo=UTC)},
        planned_search_fee_cents=20,
        estimated_cost_cents=116,
        estimate_basis="provider_model",
    )
    monkeypatch.setattr(guided_campaign_report, "build_campaign_preview", lambda *a, **k: preview)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-preview", "name": "Campaign preview"}
        ).json()["id"]
        response = client.get(
            "/api/v1/guided-campaign/preview", params={"workspace_id": workspace_id}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["manifest_sha256"] == manifest.sha256
    assert body["cycle"] == 1
    assert body["supersedes_campaign_id"] is None
    assert body["branches"] == [
        {
            "position": 1,
            "key": manifest.branches[0].key,
            "label": "Clinical Data Product Manager",
            "kind": "title_topic",
            "priority_family": "leadership",
            "protected": True,
            "markets": ["Canada"],
            "floor_query": "Clinical Data Product Manager Canada",
            "attempt_position": 1,
            "prior_outcome": None,
            "last_searched_at": "2026-09-01T12:00:00Z",
        }
    ]
    assert body["attempts"] == [{"position": 1, "branch_positions": [1], "search_cap": 5}]
    assert body["planned_search_fee_cents"] == 20
    assert body["estimated_cost_cents"] == 116
    assert body["estimate_basis"] == "provider_model"


def test_guided_campaign_preview_accepts_a_budget_above_the_module_defaults(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route states an estimate rather than capping the operator at the defaults."""
    manifest = _fake_campaign_manifest()
    preview = guided_campaign_report.CampaignPreview(
        manifest=manifest,
        cycle=1,
        prior_outcomes={},
        provider="openai",
        model="gpt-5.6-terra",
        supersedes_campaign_id=None,
        last_searched_at={},
        planned_search_fee_cents=None,
        estimated_cost_cents=None,
        estimate_basis=None,
    )
    requested: dict[str, int] = {}

    def build(*args: Any, **kwargs: Any) -> guided_campaign_report.CampaignPreview:
        requested.update(
            max_model_calls=kwargs["max_model_calls"],
            max_hosted_searches=kwargs["max_hosted_searches"],
        )
        return preview

    monkeypatch.setattr(guided_campaign_report, "build_campaign_preview", build)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-budget", "name": "Campaign budget"}
        ).json()["id"]
        response = client.get(
            "/api/v1/guided-campaign/preview",
            params={
                "workspace_id": workspace_id,
                "max_model_calls": DEFAULT_MAX_MODEL_CALLS * 2,
                "max_hosted_searches": DEFAULT_MAX_HOSTED_SEARCHES * 2,
            },
        )

    assert response.status_code == 200
    assert requested == {
        "max_model_calls": DEFAULT_MAX_MODEL_CALLS * 2,
        "max_hosted_searches": DEFAULT_MAX_HOSTED_SEARCHES * 2,
    }


def test_guided_campaign_preview_uses_the_workspace_prompt_override(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preview and execution fingerprint the same effective prompt text."""
    manifest = _fake_campaign_manifest()
    preview = guided_campaign_report.CampaignPreview(
        manifest=manifest,
        cycle=1,
        prior_outcomes={},
        provider="openai",
        model="gpt-5.6-terra",
        supersedes_campaign_id=None,
        last_searched_at={},
        planned_search_fee_cents=None,
        estimated_cost_cents=None,
        estimate_basis=None,
    )
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-override", "name": "Campaign override"}
        ).json()["id"]
        PromptLibraryService(Database(durable_database_url)).save(
            workspace_id,
            "guided_campaign_system",
            prompts.load("guided_campaign_system") + "\nCampaign override.\n",
        )

        def build(*args: Any, **kwargs: Any) -> guided_campaign_report.CampaignPreview:
            assert "Campaign override." in prompts.load("guided_campaign_system")
            return preview

        monkeypatch.setattr(guided_campaign_report, "build_campaign_preview", build)
        response = client.get(
            "/api/v1/guided-campaign/preview", params={"workspace_id": workspace_id}
        )

    assert response.status_code == 200


def test_latest_guided_campaign_report_is_null_before_any_campaign(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guided_campaign_report, "latest_campaign_report", lambda *a, **k: None)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-report", "name": "Campaign report"}
        ).json()["id"]
        response = client.get(
            "/api/v1/guided-campaigns/latest", params={"workspace_id": workspace_id}
        )

    assert response.status_code == 200
    assert response.json() is None


def test_guided_campaign_report_by_id_returns_the_named_campaigns_report(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = guided_campaign_report.CampaignReport(
        campaign_id="campaign-7",
        manifest_sha256="f" * 64,
        cycle=2,
        status=CampaignStatus.COMPLETED,
        max_model_calls=4,
        max_hosted_searches=20,
        model_calls_used=1,
        hosted_searches_used=2,
        cost_cents=None,
        known_posting_count=0,
        registered_board_count=0,
        findings=(),
        boards_admitted=(
            guided_campaign_report.CampaignBoardAdmission(
                url="https://job-boards.greenhouse.io/examplehealth",
                source_id="source-1",
                employer="Examplehealth Inc",
                branch_labels=("staff-data-engineer",),
                admitted_postings=(
                    guided_campaign_report.CampaignAdmittedPosting(
                        pending_discovery_id="pending-1",
                        url="https://job-boards.greenhouse.io/examplehealth/jobs/2001",
                        employer="Examplehealth Inc",
                        title="Staff Data Engineer",
                        location="Remote",
                    ),
                ),
            ),
        ),
        not_staged=(),
        branches=(),
        attempts=(),
        warnings=(),
        stranded_processing_attempts=0,
    )
    calls: list[tuple[object, ...]] = []

    def fake_campaign_report(session: object, workspace_id: str, campaign_id: str) -> Any:
        calls.append((workspace_id, campaign_id))
        return report

    monkeypatch.setattr(guided_campaign_report, "campaign_report", fake_campaign_report)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-by-id", "name": "Campaign by id"}
        ).json()["id"]
        response = client.get(
            "/api/v1/guided-campaigns/campaign-7", params={"workspace_id": workspace_id}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["campaign_id"] == "campaign-7"
    admitted = body["boards_admitted"][0]["admitted_postings"]
    assert admitted == [
        {
            "pending_discovery_id": "pending-1",
            "url": "https://job-boards.greenhouse.io/examplehealth/jobs/2001",
            "employer": "Examplehealth Inc",
            "title": "Staff Data Engineer",
            "location": "Remote",
        }
    ]
    assert calls == [(workspace_id, "campaign-7")]


def test_guided_campaign_report_by_id_is_not_found_for_an_unknown_campaign(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_campaign_report(session: object, workspace_id: str, campaign_id: str) -> Any:
        raise NotFoundError(f"Campaign {campaign_id!r} does not exist.")

    monkeypatch.setattr(guided_campaign_report, "campaign_report", fake_campaign_report)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-missing", "name": "Campaign missing"}
        ).json()["id"]
        response = client.get(
            "/api/v1/guided-campaigns/missing", params={"workspace_id": workspace_id}
        )

    assert response.status_code == 404


def test_abandoning_an_uncertain_attempt_returns_the_updated_report(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = guided_campaign_report.CampaignReport(
        campaign_id="campaign-1",
        manifest_sha256="e" * 64,
        cycle=1,
        status=CampaignStatus.INCOMPLETE,
        max_model_calls=8,
        max_hosted_searches=40,
        model_calls_used=1,
        hosted_searches_used=3,
        cost_cents=120,
        known_posting_count=0,
        registered_board_count=0,
        findings=(),
        boards_admitted=(),
        not_staged=(),
        branches=(),
        attempts=(
            guided_campaign_report.CampaignAttemptRow(
                position=1,
                state=AttemptState.ABANDONED,
                hosted_search_count=3,
                page_action_count=0,
                finish="stop",
                captured_not_selected=0,
                cost_cents=120,
                error_category=None,
            ),
        ),
        warnings=(CampaignReason.ABANDONED_ATTEMPT,),
        stranded_processing_attempts=1,
    )
    calls: list[tuple[object, ...]] = []

    def fake_abandon(session: object, workspace_id: str, campaign_id: str, position: int):
        calls.append((workspace_id, campaign_id, position))
        return report

    monkeypatch.setattr(guided_campaign_report, "abandon_uncertain_attempt", fake_abandon)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "campaign-abandon", "name": "Campaign abandon"}
        ).json()["id"]
        response = client.post(
            "/api/v1/guided-campaigns/campaign-1/attempts/1/abandon",
            json={"workspace_id": workspace_id},
        )

    assert response.status_code == 200
    assert response.json()["attempts"][0]["state"] == "abandoned"
    assert response.json()["warnings"] == ["abandoned_attempt"]
    assert response.json()["stranded_processing_attempts"] == 1
    assert calls == [(workspace_id, "campaign-1", 1)]


def test_saving_search_parameters_queues_one_free_rescreen_per_new_version(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "rescreen-save", "name": "Rescreen"}
        ).json()["id"]
        body = {
            "workspace_id": workspace_id,
            "parameters": {"locations": [{"key": "home", "country": "Freedonia"}]},
        }
        assert client.put("/api/v1/job-search-parameters", json=body).status_code == 200
        assert client.put("/api/v1/job-search-parameters", json=body).json()["unchanged"]
        tasks = client.get("/api/v1/tasks", params={"workspace_id": workspace_id}).json()

    assert [task["task_type"] for task in tasks] == ["discovery.rescreen_queue"]
