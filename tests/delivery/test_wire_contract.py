"""What may cross the wire, asserted against the generated contract.

The pipeline generates the OpenAPI document and the browser types from Python
and CI diffs all three, so the contract cannot silently disagree with the
server. What that does not stop is the contract itself describing less than the
wire carries. These check the document rather than the FastAPI objects behind
it, because the document is what the client is generated from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from clearscout.bootstrap.api import create_app
from clearscout.domain import PURSUED_STATUSES
from tests.support.suite import REPO_ROOT

CONTRACT = REPO_ROOT / "web" / "openapi.json"

JSON_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

# Operations that answer with something other than a JSON body. Each is a
# deliberate transport decision, not a missing response model.
UNTYPED_RESPONSES = {
    ("get", "/api/v1/artifacts/{artifact_id}/content"): "stored bytes, not JSON",
    ("get", "/api/v1/jobs/{job_posting_id}/fit-report/export"): "a file download",
    ("delete", "/api/v1/tags/{tag_id}/assignments"): "204, so there is no body",
    (
        "delete",
        "/api/v1/workspaces/{workspace_id}/sources/{source_id}",
    ): "204, so there is no body",
    (
        "delete",
        "/api/v1/workspaces/{workspace_id}/requirement-rulings/{requirement_key}",
    ): "204, so there is no body",
    ("get", "/api/v1/tasks/{task_id}/events"): "a server-sent event stream",
    (
        "get",
        "/api/v1/workspaces/{workspace_id}/source-scan-outcomes/export",
    ): "newline-delimited SourceScanOutcomeResponse records",
}

# Fields whose contents genuinely vary per row and are not a vocabulary. Adding
# to this set is a decision about what the client cannot be generated against,
# so it is made here rather than by typing a field `Any` and moving on.
OPEN_FIELDS = {
    ("ArtifactResponse", "provenance"): "shape differs per artifact kind",
    ("FitReportInput", "critic_scores"): "scores keyed by whatever the critic returned",
    ("SavedViewConfiguration", "filters"): "one saved filter document, read and written whole",
    ("SavedViewConfiguration", "sort"): "one saved sort document, read and written whole",
    ("TaskCreate", "payload"): "validated against the registered task's own payload model",
    ("TaskResponse", "progress"): "each task reports its own progress shape",
    ("ValidationError", "ctx"): "framework-owned",
}


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONTRACT.read_text(encoding="utf-8")))


def _is_open(schema: dict[str, Any]) -> bool:
    if not schema:
        return True
    return schema.get("type") == "object" and "properties" not in schema and "$ref" not in schema


def _open_properties(schema: dict[str, Any]) -> bool:
    for combinator in ("anyOf", "allOf", "oneOf"):
        if combinator in schema:
            return any(_is_open(option) for option in schema[combinator])
    return _is_open(schema)


def test_every_operation_declares_what_it_answers_with() -> None:
    undeclared = set()
    for path, operations in _contract()["paths"].items():
        for method, operation in operations.items():
            if method not in JSON_METHODS:
                continue
            success = {
                code: response
                for code, response in operation.get("responses", {}).items()
                if code.startswith("2")
            }
            content = next(
                (
                    response.get("content")
                    for response in success.values()
                    if response.get("content")
                ),
                None,
            )
            if content is None or "schema" not in content.get("application/json", {}):
                undeclared.add((method, path))

    assert undeclared == set(UNTYPED_RESPONSES), (
        "An operation answers with something the contract does not describe. "
        "Give it a response model, or record here why it cannot have one."
    )


def test_no_field_reaches_the_wire_as_an_undescribed_object() -> None:
    schemas = _contract()["components"]["schemas"]
    open_fields = {
        (name, field)
        for name, schema in schemas.items()
        for field, value in schema.get("properties", {}).items()
        if _open_properties(value)
    }

    assert open_fields == set(OPEN_FIELDS), (
        "A field crosses the wire as an open object, so the generated client "
        "types it as unknown. Type it, or record here why its shape varies."
    )


def test_every_requirement_the_wire_carries_states_its_durable_identity() -> None:
    """A requirement a workspace can rule on must arrive with the key to rule by.

    The ruling endpoints are addressed by ``requirement_key``, so a nullable one
    here would let the contract describe a row the SPA cannot act on.
    """
    schemas = _contract()["components"]["schemas"]

    for name in ("ReportRequirementResponse", "RequirementGapSourceResponse"):
        schema = schemas[name]
        assert "requirement_key" in schema["required"], f"{name}.requirement_key is optional"
        assert schema["properties"]["requirement_key"] == {
            "type": "string",
            "title": "Requirement Key",
        }, f"{name}.requirement_key is not a plain non-null string"


def test_the_browser_agrees_with_the_server_about_what_is_still_being_pursued() -> None:
    """One product rule, two readers, and nothing else keeping them together.

    The browser cannot derive this subset of values from the generated types, so
    the server list and the browser's declaration are checked against each other.
    """
    declared = (REPO_ROOT / "web/src/components/statuses.ts").read_text(encoding="utf-8")
    # Split on the assignment rather than the first bracket: the type annotation
    # is `readonly ApplicationStatus[]`, whose bracket comes first.
    block = declared.split("PURSUED_STATUSES", maxsplit=1)[1]
    block = block.split("= [", maxsplit=1)[1].split("]", maxsplit=1)[0]

    assert tuple(re.findall(r'"([^"]+)"', block)) == PURSUED_STATUSES


def test_the_recorded_exemptions_all_still_exist() -> None:
    contract = _contract()
    schemas = contract["components"]["schemas"]

    # An exemption for something that is gone reads as coverage and is not.
    for method, path in UNTYPED_RESPONSES:
        assert method in contract["paths"].get(path, {}), f"{method} {path} no longer exists"
    for name, field in OPEN_FIELDS:
        assert field in schemas.get(name, {}).get("properties", {}), f"{name}.{field} is gone"


def test_workspace_routes_keep_their_public_paths_after_router_composition(
    durable_database_url: str, tmp_path: Path
) -> None:
    """Focused workspace routers retain the complete published workspace surface."""
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        paths = client.get("/api/v1/openapi.json").json()["paths"]

    expected = {
        "/api/v1/workspaces": {"get", "post"},
        "/api/v1/workspaces/{workspace_id}/summary": {"get"},
        "/api/v1/workspaces/{workspace_id}/guardrails": {"get", "put"},
        "/api/v1/workspaces/{workspace_id}/guardrails/history": {"get"},
        "/api/v1/workspaces/{workspace_id}/guardrails/preview": {"post"},
        "/api/v1/workspaces/{workspace_id}/learning-loop-metrics": {"get"},
        "/api/v1/workspaces/{workspace_id}/master-cv": {"get", "post"},
        "/api/v1/workspaces/{workspace_id}/master-cv/import": {"post"},
        "/api/v1/workspaces/{workspace_id}/master-cv/versions": {"get"},
        "/api/v1/workspaces/{workspace_id}/master-cv/diff": {"get"},
        "/api/v1/workspaces/{workspace_id}/master-cv/impact": {"post"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries": {"get", "post"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/page": {"get"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/changes": {"get"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/check": {"post"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/reset": {"post"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}": {"patch"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/content": {"post"},
        "/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending_id}/confirm-identity": {
            "post"
        },
        "/api/v1/workspaces/{workspace_id}/search-title-proposals": {"get"},
        "/api/v1/workspaces/{workspace_id}/search-title-proposals/{proposal_id}/accept": {"post"},
        "/api/v1/workspaces/{workspace_id}/search-title-proposals/{proposal_id}/reject": {"post"},
        "/api/v1/workspaces/{workspace_id}/search-archetype-proposals": {"get"},
        "/api/v1/workspaces/{workspace_id}/search-archetype-proposals/{proposal_id}/accept": {
            "post"
        },
        "/api/v1/workspaces/{workspace_id}/search-archetype-proposals/{proposal_id}/reject": {
            "post"
        },
        "/api/v1/workspaces/{workspace_id}/search-cluster-proposals": {"get"},
        "/api/v1/workspaces/{workspace_id}/search-cluster-proposals/{proposal_id}/accept": {"post"},
        "/api/v1/workspaces/{workspace_id}/search-cluster-proposals/{proposal_id}/reject": {"post"},
        "/api/v1/workspaces/{workspace_id}/posting-refusals": {"get"},
        "/api/v1/workspaces/{workspace_id}/sources": {"get", "post"},
        "/api/v1/workspaces/{workspace_id}/sources/preview": {"post"},
        "/api/v1/workspaces/{workspace_id}/sources/{source_id}": {"patch", "delete"},
        "/api/v1/workspaces/{workspace_id}/sources/{source_id}/revalidate": {"post"},
        "/api/v1/workspaces/{workspace_id}/source-scans": {"get"},
        "/api/v1/workspaces/{workspace_id}/source-scans/{source_scan_id}/continue": {"post"},
        "/api/v1/workspaces/{workspace_id}/score-weight-sets": {"get", "post"},
        "/api/v1/workspaces/{workspace_id}/spend-history": {"get"},
        "/api/v1/workspaces/{workspace_id}/spend": {"get"},
    }

    assert {
        path: {
            method for method in operations if method in {"get", "post", "put", "patch", "delete"}
        }
        for path, operations in paths.items()
        if path in expected
    } == expected
