"""Durable pressume rendering for indexed document artifacts."""

from __future__ import annotations

import mimetypes
import tempfile
from pathlib import Path
from typing import Any

from clearscout.application.documents.artifacts import DocumentService, _artifact_record
from clearscout.application.failures import ValidationError
from clearscout.application.tasks import TaskContext
from clearscout.domain import ArtifactLogicalType
from clearscout.infrastructure.artifacts import rendering
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database

# A package's file names, so a rendered PDF downloads as Resume.pdf, not document.pdf.
SOURCE_NAMES: dict[str, str] = {
    "resume.markdown": "Resume.md",
    "cover_letter.markdown": "Cover_Letter.md",
}


class DocumentRenderHandler:
    """Durable pressume adapter that indexes every produced output as a new artifact."""

    def __init__(self, database: Database, storage: RuntimeStorage) -> None:
        """Configure safe document reads and the artifact destination."""
        self.documents = DocumentService(database, storage)
        self.database = database

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Render one verified Markdown artifact and index the produced files."""
        artifact_id = str(payload["artifact_id"])
        workspace_id = str(context.queue.get(context.task_id).workspace_id)
        source = self.documents.read(artifact_id, workspace_id)
        if source.artifact.mime_type != "text/markdown":
            raise ValidationError("pressume rendering requires a Markdown artifact.")
        if (
            source.artifact.related_entity_type != "job_posting"
            or source.artifact.related_entity_id is None
        ):
            raise ValidationError(
                "pressume rendering requires a Markdown artifact attached to a job posting."
            )
        context.progress("preparing", {"artifact_id": artifact_id})
        context.raise_if_cancelled()
        logical_type = source.artifact.logical_type
        with tempfile.TemporaryDirectory(prefix="clearscout-render-") as temporary:
            document = Path(temporary) / SOURCE_NAMES.get(logical_type, "document.md")
            document.write_bytes(source.content)
            outcome = rendering.render(document, letter=logical_type == "cover_letter.markdown")
            if not outcome.ok:
                raise ValidationError(outcome.message)
            context.raise_if_cancelled()
            rendered: list[dict[str, Any]] = []
            with self.database.session() as session:
                for path in outcome.produced:
                    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    kind: ArtifactLogicalType = (
                        "document.rendered.pdf"
                        if path.suffix.lower() == ".pdf"
                        else "document.rendered.text"
                    )
                    artifact = self.documents.store_generated(
                        session,
                        workspace_id=workspace_id,
                        job_posting_id=source.artifact.related_entity_id,
                        tailoring_run_id=source.artifact.tailoring_run_id,
                        logical_type=kind,
                        mime_type=mime,
                        content=path.read_bytes(),
                        author_type="system",
                        provenance={
                            "source": "pressume",
                            "derived_from_artifact_id": source.artifact.id,
                            "derived_from_sha256": source.artifact.sha256,
                            "relative_path": path.name,
                        },
                    )
                    rendered.append(_artifact_record(artifact).model_dump(mode="json"))
        result = {
            "artifact_id": artifact_id,
            "message": "\n".join((outcome.message, *outcome.warnings)),
            "artifacts": rendered,
        }
        context.progress("rendered", result)
        return result
