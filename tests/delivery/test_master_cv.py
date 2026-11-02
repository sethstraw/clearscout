"""Master CV routes: upload, versioning, impact check, and in-place edit over HTTP."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tailorcv.bootstrap.api import create_app


def test_workspace_onboarding_uploads_and_versions_a_master_cv(
    durable_database_url: str, tmp_path: Path, master_cv: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces",
            json={"slug": "onboarding", "name": "Onboarding", "provider": "openai"},
        )
        assert workspace.status_code == 201
        assert workspace.json()["root_path"] == str(tmp_path / "workspaces" / "onboarding")
        assert workspace.json()["provider"] == "openai"
        imported = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/master-cv/import",
            params={"filename": "My CV.md"},
            content=master_cv.read_bytes(),
            headers={"Content-Type": "text/markdown"},
        )
        assert imported.status_code == 201
        assert imported.json()["method"] == "adopted"
        assert imported.json()["entry_count"] > 0
        assert Path(imported.json()["canonical_path"]).is_file()
        oversized = client.post(
            f"/api/v1/workspaces/{workspace.json()['id']}/master-cv/import",
            params={"filename": "too-large.pdf"},
            content=b"x",
            headers={"Content-Length": str(25 * 1024 * 1024 + 1)},
        )
        assert oversized.status_code == 422
        assert "limited" in oversized.json()["detail"]
        assert (
            "/api/v1/workspaces/{workspace_id}/master-cv/import"
            in client.get("/api/v1/openapi.json").json()["paths"]
        )


def test_master_cv_is_checked_for_impact_and_then_edited_in_place(
    durable_database_url: str, tmp_path: Path, master_cv: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces",
            json={"slug": "editing", "name": "Editing"},
        ).json()["id"]
        client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv/import",
            params={"filename": "My CV.md"},
            content=master_cv.read_bytes(),
            headers={"Content-Type": "text/markdown"},
        )
        edited = master_cv.read_bytes().decode("utf-8") + "\n- An edited line of evidence.\n"

        impact = client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv/impact",
            json={"content": edited},
        )
        assert impact.status_code == 200
        assert impact.json()["identical"] is False
        assert impact.json()["citations"] == []

        saved = client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv",
            json={"content": edited},
        )
        assert saved.status_code == 201
        assert saved.json()["created"] is True

        versions = client.get(f"/api/v1/workspaces/{workspace_id}/master-cv/versions")
        assert len(versions.json()) == 2
        current = client.get(f"/api/v1/workspaces/{workspace_id}/master-cv")
        assert current.json()["content"] == edited
        # Saving the same text again appends nothing and says so.
        again = client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv",
            json={"content": edited},
        )
        assert again.json()["created"] is False
        assert len(client.get(f"/api/v1/workspaces/{workspace_id}/master-cv/versions").json()) == 2

        refused = client.post(
            f"/api/v1/workspaces/{workspace_id}/master-cv",
            json={"content": "# JANE Q. EXAMPLE\n\n## Summary\n\nBuilds data platforms.\n"},
        )
        assert refused.status_code == 422
        assert "no dated role" in refused.text
