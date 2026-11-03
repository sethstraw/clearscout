from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from clearscout.application.documents import DocumentService
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import WorkspaceService
from clearscout.bootstrap.api import create_app
from clearscout.infrastructure.persistence.database import Database
from tests.support.artifacts import runtime_storage
from tests.support.scenarios import seed_application_fit_report, studio_workspace


def test_artifact_api_never_serves_arbitrary_or_inline_html(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    other = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="other", name="Other"
    )
    with TestClient(
        create_app(durable_database.url, artifact_root), base_url="http://localhost"
    ) as client:
        uploaded = client.post(
            "/api/v1/artifacts/upload",
            params={
                "workspace_id": workspace_id,
                "entity_type": "job_posting",
                "entity_id": job_id,
                "logical_type": "recruiter.email",
                "filename": "message.html",
            },
            headers={"Content-Type": "text/html"},
            content=b"<script>alert(1)</script>",
        )
        assert uploaded.status_code == 201
        artifact = uploaded.json()
        assert artifact["mime_type"] == "application/octet-stream"
        opened = client.get(
            f"/api/v1/artifacts/{artifact['id']}/content",
            params={"workspace_id": workspace_id, "disposition": "inline"},
        )
        assert opened.status_code == 200
        assert opened.headers["content-type"] == "application/octet-stream"
        assert opened.headers["content-disposition"].startswith("attachment;")
        assert opened.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in opened.headers["content-security-policy"]
        # The label reaches Content-Disposition, so the closed vocabulary refuses
        # a header-injection payload on the way in.
        injected = client.patch(
            f"/api/v1/artifacts/{artifact['id']}",
            json={
                "workspace_id": workspace_id,
                "logical_type": 'message"\r\nx-injected: true',
            },
        )
        assert injected.status_code == 422
        relabeled = client.patch(
            f"/api/v1/artifacts/{artifact['id']}",
            json={"workspace_id": workspace_id, "logical_type": "take_home"},
        )
        assert relabeled.status_code == 200
        sanitized = client.get(
            f"/api/v1/artifacts/{artifact['id']}/content",
            params={"workspace_id": workspace_id},
        )
        assert "x-injected" not in sanitized.headers
        assert "\r" not in sanitized.headers["content-disposition"]
        assert "\n" not in sanitized.headers["content-disposition"]
        assert "take_home" in sanitized.headers["content-disposition"]
        crossed = client.get(
            f"/api/v1/artifacts/{artifact['id']}/content",
            params={"workspace_id": other.id},
        )
        assert crossed.status_code == 404

        text_upload = client.post(
            "/api/v1/artifacts/upload",
            params={
                "workspace_id": workspace_id,
                "entity_type": "job_posting",
                "entity_id": job_id,
                "logical_type": "resume.markdown",
                "filename": "resume.md",
            },
            headers={"Content-Type": "text/markdown"},
            content=b"# Candidate\nFirst version.\n",
        )
        assert text_upload.status_code == 201
        first = text_upload.json()
        assert (
            client.get(
                "/api/v1/artifacts",
                params={
                    "workspace_id": workspace_id,
                    "entity_type": "job_posting",
                    "entity_id": job_id,
                },
            ).status_code
            == 200
        )
        assert (
            client.get(
                f"/api/v1/artifacts/{first['id']}", params={"workspace_id": workspace_id}
            ).json()["sha256"]
            == first["sha256"]
        )
        assert (
            client.post(
                f"/api/v1/artifacts/{first['id']}/citation-check",
                json={"workspace_id": workspace_id, "content": "# Candidate\nFirst version.\n"},
            ).json()["requires_override"]
            is False
        )
        saved = client.post(
            f"/api/v1/artifacts/{first['id']}/versions",
            json={
                "workspace_id": workspace_id,
                "content": "# Candidate\nSecond version.\n",
                "author_type": "human",
                "override_reason": "",
            },
        )
        assert saved.status_code == 201
        second = saved.json()
        versions = client.get(
            f"/api/v1/artifacts/{second['id']}/versions",
            params={"workspace_id": workspace_id},
        )
        assert len(versions.json()) == 2
        diff = client.get(
            f"/api/v1/artifacts/{first['id']}/diff/{second['id']}",
            params={"workspace_id": workspace_id},
        )
        assert "Second version" in diff.json()["diff"]
        reverted = client.post(
            f"/api/v1/artifacts/{second['id']}/revert",
            json={"workspace_id": workspace_id, "target_artifact_id": first["id"]},
        )
        assert reverted.status_code == 201
        assert reverted.json()["version_number"] == 3
        assert (
            client.delete(
                f"/api/v1/artifacts/{reverted.json()['id']}",
                params={"workspace_id": workspace_id},
            ).status_code
            == 200
        )


def test_artifact_api_serves_svg_as_attachment_and_raster_images_inline(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    with TestClient(
        create_app(durable_database.url, artifact_root), base_url="http://localhost"
    ) as client:
        served = {}
        for filename, mime_type, content in (
            ("diagram.svg", "image/svg+xml", b"<svg><script>alert(1)</script></svg>"),
            ("photo.png", "image/png", b"\x89PNG\r\n\x1a\n"),
        ):
            uploaded = client.post(
                "/api/v1/artifacts/upload",
                params={
                    "workspace_id": workspace_id,
                    "entity_type": "job_posting",
                    "entity_id": job_id,
                    "logical_type": "take_home",
                    "filename": filename,
                },
                headers={"Content-Type": mime_type},
                content=content,
            )
            assert uploaded.status_code == 201
            opened = client.get(
                f"/api/v1/artifacts/{uploaded.json()['id']}/content",
                params={"workspace_id": workspace_id, "disposition": "inline"},
            )
            served[mime_type] = (uploaded.json()["inline_viewable"], opened.headers)

    svg_viewable, svg_headers = served["image/svg+xml"]
    assert svg_viewable is False
    assert svg_headers["content-type"] == "application/octet-stream"
    assert svg_headers["content-disposition"].startswith("attachment;")
    png_viewable, png_headers = served["image/png"]
    assert png_viewable is True
    assert png_headers["content-type"] == "image/png"
    assert png_headers["content-disposition"].startswith("inline;")


def test_workspace_record_api_crud_contracts(durable_database: Database, tmp_path: Path) -> None:
    workspace_id, job_id, application_id, _ = studio_workspace(
        durable_database, tmp_path / "workspace", tmp_path / "artifacts"
    )
    with TestClient(
        create_app(durable_database.url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        note_body = {
            "workspace_id": workspace_id,
            "entity_type": "job_posting",
            "entity_id": job_id,
            "title": "API note",
            "body": "Not evidence",
            "pinned": False,
        }
        note = client.post("/api/v1/notes", json=note_body)
        assert note.status_code == 201
        note_id = note.json()["id"]
        assert (
            client.get(
                "/api/v1/notes",
                params={"workspace_id": workspace_id, "search": "evidence"},
            ).json()[0]["evidence_eligible"]
            is False
        )
        note_body.update({"title": "Updated", "pinned": True})
        note_body.pop("entity_type")
        note_body.pop("entity_id")
        assert client.put(f"/api/v1/notes/{note_id}", json=note_body).status_code == 200
        assert (
            client.delete(
                f"/api/v1/notes/{note_id}", params={"workspace_id": workspace_id}
            ).status_code
            == 200
        )

        tag_body = {
            "workspace_id": workspace_id,
            "name": "API tag",
            "color": "#123456",
        }
        tag = client.post("/api/v1/tags", json=tag_body)
        assert tag.status_code == 201
        tag_id = tag.json()["id"]
        assert client.post("/api/v1/tags", json=tag_body).status_code == 409
        assert (
            client.post(
                "/api/v1/tags",
                json={**tag_body, "name": "Bad color", "color": "red"},
            ).status_code
            == 422
        )
        tag_body.update({"name": "Updated tag", "color": "#654321"})
        assert client.put(f"/api/v1/tags/{tag_id}", json=tag_body).status_code == 200
        assigned = client.post(
            f"/api/v1/tags/{tag_id}/assignments",
            json={
                "workspace_id": workspace_id,
                "entity_type": "application",
                "entity_id": application_id,
            },
        )
        assert assigned.status_code == 201
        assert client.get("/api/v1/tags", params={"workspace_id": workspace_id}).json()[0][
            "assignments"
        ]
        assert (
            client.delete(
                f"/api/v1/tags/{tag_id}/assignments",
                params={
                    "workspace_id": workspace_id,
                    "entity_type": "application",
                    "entity_id": application_id,
                },
            ).status_code
            == 204
        )
        assert (
            client.delete(
                f"/api/v1/tags/{tag_id}", params={"workspace_id": workspace_id}
            ).status_code
            == 200
        )

        view_body = {
            "workspace_id": workspace_id,
            "resource": "jobs",
            "name": "API view",
            "configuration": {"filters": {}, "sort": []},
        }
        view = client.post("/api/v1/saved-views", json=view_body)
        assert view.status_code == 201
        view_id = view.json()["id"]
        invalid_view = {
            **view_body,
            "name": "Invalid view",
            "configuration": {"filters": "not structured", "sort": []},
        }
        assert client.post("/api/v1/saved-views", json=invalid_view).status_code == 422
        unsupported_view = {**view_body, "resource": "applications"}
        assert client.post("/api/v1/saved-views", json=unsupported_view).status_code == 422
        view_body["name"] = "Updated view"
        assert client.put(f"/api/v1/saved-views/{view_id}", json=view_body).status_code == 200
        assert (
            client.get(
                "/api/v1/saved-views",
                params={"workspace_id": workspace_id, "resource": "jobs"},
            ).json()[0]["id"]
            == view_id
        )
        assert (
            client.delete(
                f"/api/v1/saved-views/{view_id}",
                params={"workspace_id": workspace_id},
            ).status_code
            == 200
        )


def test_application_files_resolve_documents_filed_against_its_job_runs(
    durable_database: Database, tmp_path: Path
) -> None:
    """A document names the run that produced it, never the application."""
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, application_id, master_version_id = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    service = DocumentService(durable_database, runtime_storage(durable_database, artifact_root))
    uploaded = service.upload(
        workspace_id=workspace_id,
        entity_type="application",
        entity_id=application_id,
        logical_type="recruiter.email",
        filename="message.txt",
        mime_type="text/plain",
        content=b"Please schedule a call.\n",
    )
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace_id,
        task_type="workflow.tailor",
        payload={"job_posting_id": job_id},
        idempotency_key="artifact-scope-run",
    )
    fit_report_id = seed_application_fit_report(
        durable_database,
        service,
        workspace_id=workspace_id,
        job_id=job_id,
        master_version_id=master_version_id,
        task_id=task.id,
    )

    direct = service.list_artifacts(
        workspace_id, entity_type="application", entity_id=application_id
    )
    assert [item.id for item in direct] == [uploaded.id]

    resolved = service.list_artifacts(
        workspace_id, entity_type="application", entity_id=application_id, include_related=True
    )
    ids = {item.id for item in resolved}
    assert uploaded.id in ids
    assert fit_report_id in ids
