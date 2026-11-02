"""Versioned documents, safe file access, and evidence-aware human edits."""

from tailorcv.application.documents.artifacts import (
    EDITABLE_TYPES,
    ENTITY_MODELS,
    GENERATED_TYPES,
    JOB_SCOPED_ENTITIES,
    MAX_UPLOAD_BYTES,
    ArtifactContent,
    DocumentService,
    serves_inline,
    unified,
)
from tailorcv.application.documents.render import DocumentRenderHandler

__all__ = [
    "EDITABLE_TYPES",
    "ENTITY_MODELS",
    "GENERATED_TYPES",
    "JOB_SCOPED_ENTITIES",
    "MAX_UPLOAD_BYTES",
    "ArtifactContent",
    "DocumentRenderHandler",
    "DocumentService",
    "serves_inline",
    "unified",
]
