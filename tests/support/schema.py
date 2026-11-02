"""Read a closed vocabulary back out of a generated schema.

If a field ever stopped being closed over the real identifiers there would be no
enumeration here to find. Either the pydantic model or the JSON Schema dict is
accepted, because the scripted provider sees the class while a fake HTTP server
sees only what went over the wire.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def literals(schema: type[BaseModel] | dict[str, Any], field: str) -> list[str]:
    """Return the allowed values of a Literal-constrained field."""
    document = schema if isinstance(schema, dict) else schema.model_json_schema()
    definitions = document.get("$defs", {})
    found: list[str] = []

    def resolve(node: Any) -> Any:
        # Repeated enums are hoisted into $defs before a request is sent, so on
        # the wire the vocabulary is a reference rather than an inline list.
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            name = str(node["$ref"]).rsplit("/", 1)[-1]
            if name in seen:
                return {}
            seen.add(name)
            node = definitions.get(name, {})
        return node

    def enums(node: Any) -> None:
        node = resolve(node)
        if isinstance(node, dict):
            if "enum" in node:
                found.extend(str(value) for value in node["enum"])
            for value in node.values():
                enums(value)
        elif isinstance(node, list):
            for item in node:
                enums(item)

    def walk(node: Any) -> None:
        node = resolve(node)
        if isinstance(node, dict):
            for name, value in node.items():
                if name == field:
                    enums(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    return list(dict.fromkeys(found))


def declares(schema: type[BaseModel] | dict[str, Any], field: str) -> bool:
    """Return whether any object in the schema has a property of this name."""
    document = schema if isinstance(schema, dict) else schema.model_json_schema()

    def walk(node: Any) -> bool:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict) and field in properties:
                return True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    return walk(document)


def production_schemas() -> dict[str, type[BaseModel]]:
    """Return every response schema a production stage sends to a provider.

    One entry per client.complete call site. A stage added without an entry here
    is a stage no vendor-subset check covers, so a search for that call across
    src/ is the list this has to match.
    """
    from tailorcv.application.discovery.assisted_content_resolution import (
        response_schema as recovery_schema,
    )
    from tailorcv.application.discovery.relevance_review import (
        response_schema as relevance_review_schema,
    )
    from tailorcv.application.discovery.search_vocabulary import response_schema as search_schema
    from tailorcv.application.master_cv.sources import SourceDocument, SourceLine
    from tailorcv.application.tailoring.pipeline.models import VisionTranscript
    from tailorcv.application.tailoring.stages.draft import cover_letter_schema, draft_schema
    from tailorcv.application.tailoring.stages.jobspec import JobSpecExtraction
    from tailorcv.application.tailoring.stages.match import response_schema as match_schema
    from tailorcv.application.tailoring.stages.structure import response_schema as structure_schema
    from tailorcv.application.tailoring.stages.verify import CriticVerdict

    entries = ("entry-1", "entry-2")
    source = SourceDocument(
        origin="cv.md", method="text", lines=(SourceLine(number=1, text="A line."),)
    )
    return {
        "jobspec": JobSpecExtraction,
        "critic": CriticVerdict,
        "vision": VisionTranscript,
        "match": match_schema(entries, ("requirement-1",)),
        "draft_headed_skills": draft_schema(entries, ("role-1",), entries, labelled=False),
        "draft_unheaded_skills": draft_schema(entries, ("role-1",), entries, labelled=True),
        "cover_letter": cover_letter_schema(entries),
        "structure": structure_schema(source),
        "search_vocabulary": search_schema(entries),
        "posting_recovery": recovery_schema("pending-1"),
        "relevance_review": relevance_review_schema(entries, conditions=("A listed condition",)),
        "relevance_review_unconditioned": relevance_review_schema(entries, conditions=()),
    }
