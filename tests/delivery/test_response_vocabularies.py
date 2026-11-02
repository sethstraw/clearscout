"""Wire checks for response vocabularies owned by the application."""

from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from tailorcv.application.tasks import task_types
from tailorcv.bootstrap.api import create_app
from tailorcv.domain import ARTIFACT_LOGICAL_TYPES
from tailorcv.infrastructure.persistence.database import Database
from tests.support.scenarios import studio_workspace


def _resolved_schema(schema: dict[str, Any], field_schema: dict[str, Any]) -> dict[str, Any]:
    reference = field_schema.get("$ref")
    if reference is None:
        return field_schema
    return cast(
        dict[str, Any],
        schema["components"]["schemas"][reference.rsplit("/", maxsplit=1)[-1]],
    )


def test_logical_type_response_fields_are_closed_vocabularies(tmp_path: Path) -> None:
    schema = create_app(data_root=tmp_path).openapi()

    for model_name in (
        "ArtifactRelabel",
        "ArtifactResponse",
        "EvidenceSourceResponse",
        "PostingObservationBodyResponse",
    ):
        field_schema = schema["components"]["schemas"][model_name]["properties"]["logical_type"]
        logical_type_schema = _resolved_schema(schema, field_schema)
        assert logical_type_schema["type"] == "string"
        # The whole vocabulary, not a member of it: a field carrying a subset
        # would still let a stored artifact arrive as a value the caller cannot
        # name.
        assert tuple(logical_type_schema["enum"]) == ARTIFACT_LOGICAL_TYPES


def test_saved_view_and_task_response_vocabularies_are_closed(tmp_path: Path) -> None:
    schema = create_app(data_root=tmp_path).openapi()
    models = schema["components"]["schemas"]

    for model_name in ("SavedViewBody", "SavedViewResponse"):
        resource = _resolved_schema(schema, models[model_name]["properties"]["resource"])
        assert resource.get("enum", [resource.get("const")]) == ["jobs"]
    task_type = _resolved_schema(schema, models["TaskResponse"]["properties"]["task_type"])
    assert tuple(task_type["enum"]) == task_types()


def test_an_artifact_cannot_be_stored_under_a_label_the_vocabulary_does_not_name(
    durable_database: Database, tmp_path: Path
) -> None:
    artifact_root = tmp_path / "artifacts"
    workspace_id, job_id, _, _ = studio_workspace(
        durable_database, tmp_path / "workspace", artifact_root
    )
    with TestClient(
        create_app(durable_database.url, artifact_root), base_url="http://localhost"
    ) as client:
        refused = client.post(
            "/api/v1/artifacts/upload",
            params={
                "workspace_id": workspace_id,
                "entity_type": "job_posting",
                "entity_id": job_id,
                "logical_type": "notes.plain",
                "filename": "notes.txt",
            },
            headers={"Content-Type": "text/plain"},
            content=b"a label somebody invented",
        )
        assert refused.status_code == 422
