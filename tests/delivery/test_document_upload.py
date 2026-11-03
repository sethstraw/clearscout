"""Application uploads distinguish submitted bytes from related files."""

from pathlib import Path

from fastapi.testclient import TestClient

from clearscout.bootstrap.api import create_app
from tests.support.scenarios import evaluation_commit


def test_uploaded_application_file_keeps_its_submitted_role(
    durable_database_url: str, tmp_path: Path
) -> None:
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        workspace_id = client.post(
            "/api/v1/workspaces", json={"slug": "submitted-file", "name": "Submitted file"}
        ).json()["id"]
        job_id = client.post(
            "/api/v1/evaluations", json=evaluation_commit(workspace_id).model_dump(mode="json")
        ).json()["job_posting_id"]
        event = client.post(
            f"/api/v1/jobs/{job_id}/application-events",
            params={"workspace_id": workspace_id},
            headers={"Idempotency-Key": "submitted-upload-1"},
            json={"status": "applied", "occurred_at": "2026-01-03T12:00:00Z"},
        )
        application_id = event.json()["application_id"]
        content = b"Synthetic submitted resume bytes"
        uploaded = client.post(
            "/api/v1/artifacts/upload",
            params={
                "workspace_id": workspace_id,
                "entity_type": "application",
                "entity_id": application_id,
                "logical_type": "user.attachment",
                "filename": "submitted-resume.txt",
                "submission_role": "resume",
            },
            headers={"Content-Type": "text/plain"},
            content=content,
        )
        assert uploaded.status_code == 201, uploaded.text
        artifact = uploaded.json()
        assert artifact["provenance"]["submission_role"] == "resume"
        assert artifact["related_entity_id"] == application_id
        read = client.get(
            f"/api/v1/artifacts/{artifact['id']}/content",
            params={"workspace_id": workspace_id},
        )
        assert read.status_code == 200
        assert read.content == content

        edited = client.post(
            f"/api/v1/artifacts/{artifact['id']}/versions",
            json={
                "workspace_id": workspace_id,
                "content": "Different synthetic resume text",
                "author_type": "human",
            },
        )
        assert edited.status_code == 201, edited.text
        assert "submission_role" not in edited.json()["provenance"]
        reverted = client.post(
            f"/api/v1/artifacts/{edited.json()['id']}/revert",
            json={"workspace_id": workspace_id, "target_artifact_id": artifact["id"]},
        )
        assert reverted.status_code == 201, reverted.text
        assert "submission_role" not in reverted.json()["provenance"]

        invalid = client.post(
            "/api/v1/artifacts/upload",
            params={
                "workspace_id": workspace_id,
                "entity_type": "job_posting",
                "entity_id": job_id,
                "logical_type": "user.attachment",
                "filename": "wrong-owner.txt",
                "submission_role": "resume",
            },
            headers={"Content-Type": "text/plain"},
            content=content,
        )
        assert invalid.status_code == 422
