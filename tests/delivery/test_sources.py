"""Workspace source routes: request validation, status mapping, and response shape."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tailorcv.application.discovery.funnel_audit import (
    FunnelCandidateKind,
    FunnelReason,
    FunnelStage,
)
from tailorcv.application.discovery.scan_queries import ScanOutcomeFilters
from tailorcv.application.job_search.models import SourceScanOutcomeResponse
from tailorcv.application.tasks import TaskQueueService
from tailorcv.application.workspaces import WorkspaceService
from tailorcv.application.workspaces.scan_schedule import ScanScheduleService
from tailorcv.bootstrap.api import create_app
from tailorcv.delivery.api.dependencies import provide_scan_schedule
from tailorcv.domain import SourceReadOutcome, new_id, utc_now
from tailorcv.domain.identity import normalize_text, url_sha256
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.models import (
    BackgroundTask,
    Employer,
    ScreenedOutTitle,
    Source,
    SourceScan,
    SourceScanOutcome,
)
from tailorcv.infrastructure.web_fetch.board_adapters import BoardApiFetcher
from tailorcv.infrastructure.web_fetch.policy import (
    DestinationUnresolvableError,
    HostHeldError,
    InvalidFetchUrlError,
    RobotsDeniedError,
    RobotsUnavailableError,
)
from tests.support.artifacts import runtime_storage
from tests.support.doubles import MemoryStore
from tests.support.scenarios import (
    same_day_rescans_with_follow_ups,
    seed_scan_continuation,
    state_target_titles,
)

_CAREERS = "https://www.initech.test/careers"


@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
def test_scan_outcome_export_closes_records_when_the_client_disconnects(
    failed_message: str,
) -> None:
    from starlette.requests import ClientDisconnect

    from tailorcv.delivery.api.routers.workspaces.sources import export_source_scan_outcomes

    closed: list[bool] = []

    def records() -> Any:
        try:
            for _ in range(1000):
                yield SourceScanOutcomeResponse(
                    id="synthetic",
                    task_id="task",
                    source_scan_id=None,
                    source_name="Example careers",
                    candidate_kind=FunnelCandidateKind.BOARD_LISTING,
                    url="https://example.test/jobs/1",
                    employer="Acme",
                    title="Data Engineer",
                    location="Remote",
                    terminal_stage=FunnelStage.SCREENED,
                    reason=FunnelReason.INSUFFICIENT_TITLE_EVIDENCE,
                    detail="Retained for audit.",
                    screening_findings=[],
                    created_at=datetime(2026, 10, 9, tzinfo=UTC),
                )
        finally:
            closed.append(True)

    async def exercise() -> None:
        service: Any = SimpleNamespace(export_scan_outcomes=lambda *args, **kwargs: records())
        response = await export_source_scan_outcomes(
            "workspace", "task", service, ScanOutcomeFilters()
        )

        async def receive() -> Any:
            raise AssertionError("ASGI 2.4 should use send failures for disconnects")

        async def send(message: Any) -> None:
            if message["type"] == failed_message:
                raise OSError("Client disconnected")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert closed == [True]
        assert response is not None

    asyncio.run(exercise())


def test_scan_outcome_export_streams_all_filtered_rows_without_repeated_metadata(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    workspace = WorkspaceService(durable_database, runtime_storage(durable_database)).create(
        slug="outcome-export", name="Outcome export"
    )
    state_target_titles(durable_database, workspace.id, "Data Platform Lead")
    task = TaskQueueService(durable_database).enqueue(
        workspace_id=workspace.id, task_type="source.scan", payload={}, idempotency_key="export"
    )
    with durable_database.session() as session:
        session.add_all(
            SourceScanOutcome(
                id=f"00000000-0000-4000-8000-{index:012d}",
                workspace_id=workspace.id,
                task_id=task.id,
                source_name="Example careers",
                candidate_kind="board_listing",
                url=f"https://example.test/jobs/export-{index}",
                normalized_url=f"https://example.test/jobs/export-{index}",
                employer="Acme",
                title="Data Platform Lead",
                location="Remote",
                terminal_stage="screened",
                reason="insufficient_title_evidence",
                detail="Retained for audit.",
                screening_findings=[],
                created_at=datetime(2026, 10, 9, 12, tzinfo=UTC),
            )
            for index in range(601)
        )
    statements: list[str] = []

    def record_sql(
        _connection: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: bool
    ) -> None:
        statements.append(statement)

    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        event.listen(Engine, "before_cursor_execute", record_sql)
        try:
            response = client.get(
                f"/api/v1/workspaces/{workspace.id}/source-scan-outcomes/export",
                params={
                    "task_id": task.id,
                    "search": "platform",
                    "reason": "insufficient_title_evidence",
                    "sort": "created_at",
                    "direction": "asc",
                },
            )
        finally:
            event.remove(Engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    rows = [json.loads(line) for line in response.text.splitlines()]
    assert len(rows) == 601
    assert [row["id"] for row in rows] == [
        f"00000000-0000-4000-8000-{index:012d}" for index in range(601)
    ]
    assert {row["task_id"] for row in rows} == {task.id}
    ledger_reads = [sql.lower() for sql in statements if "from source_scan_outcomes" in sql.lower()]
    assert len(ledger_reads) == 1
    assert all(
        "offset" not in sql and "count(" not in sql and "distinct" not in sql
        for sql in ledger_reads
    )


def test_scan_outcome_export_preserves_parent_and_direct_continuation_scope(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> None:
    workspace_id, first, second = same_day_rescans_with_follow_ups(durable_database)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        route = f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes/export"
        exported = client.get(route, params={"task_id": first})
        rows = [json.loads(line) for line in exported.text.splitlines()]
        assert {row["title"] for row in rows} == {
            "Alpha Scan Lead",
            "Alpha Follow-up Lead",
            "Alpha Chained Lead",
        }
        child = next(row["task_id"] for row in rows if row["title"] == "Alpha Follow-up Lead")
        direct = client.get(route, params={"task_id": child})
        assert {json.loads(line)["title"] for line in direct.text.splitlines()} == {
            "Alpha Follow-up Lead"
        }
        narrowed = client.get(route, params={"task_id": second, "search": "Chained"})
        assert narrowed.status_code == 200 and narrowed.text == ""
        wrong = client.get(
            "/api/v1/workspaces/missing/source-scan-outcomes/export", params={"task_id": first}
        )
        assert wrong.status_code == 404
        missing = client.get(route, params={"task_id": "missing"})
        assert missing.status_code == 404
        invalid = client.get(route, params={"task_id": first, "sort": "unsupported"})
        assert invalid.status_code == 422


def test_scan_schedule_can_be_saved_paused_and_read_without_provider_credentials(
    durable_database: Database, durable_database_url: str
) -> None:
    app = create_app(durable_database_url, key_store=MemoryStore())
    now = datetime(2026, 10, 8, 12, tzinfo=UTC)
    app.dependency_overrides[provide_scan_schedule] = lambda: ScanScheduleService(
        durable_database, clock=lambda: now
    )
    with TestClient(app, base_url="http://localhost") as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "calendar", "name": "Calendar"}
        ).json()
        workspace_id = workspace["id"]
        path = f"/api/v1/workspaces/{workspace_id}/scan-schedule"
        initial = client.get(path)
        assert initial.status_code == 200
        assert initial.json()["state"] is None
        state_target_titles(durable_database, workspace_id, "Operations Analyst")
        configuration = {
            "expected_revision": 0,
            "timezone": "America/New_York",
            "frequency": "daily",
            "local_time": "09:00",
            "scope": "all_enabled",
        }
        saved = client.put(path, json=configuration)
        assert saved.status_code == 200
        state = saved.json()["state"]
        assert state["revision"] == 1
        assert state["next_run_at"] == "2026-10-08T13:00:00Z"
        assert state["configuration"]["timezone"] == "America/New_York"
        assert client.get(path).json() == saved.json()

        stale = client.put(path, json={**configuration, "local_time": "10:00"})
        assert stale.status_code == 409
        assert client.get(path).json() == saved.json()

        paused = client.put(path, json={**configuration, "expected_revision": 1, "paused": True})
        assert paused.status_code == 200
        assert paused.json()["state"]["next_run_at"] is None
        assert paused.json()["state"]["configuration"]["paused"] is True
        assert client.get(path).json() == paused.json()


@pytest.mark.parametrize(
    "invalid",
    [
        {"timezone": "Unknown/Zone"},
        {"local_time": "24:00"},
        {"scope": "selected", "source_ids": []},
        {"scope": "selected", "source_ids": ["duplicate", "duplicate"]},
    ],
)
def test_scan_schedule_rejects_invalid_calendar_or_scope_without_saving(
    durable_database_url: str, invalid: dict[str, Any]
) -> None:
    with TestClient(
        create_app(durable_database_url, key_store=MemoryStore()), base_url="http://localhost"
    ) as client:
        workspace = client.post(
            "/api/v1/workspaces", json={"slug": "invalid-calendar", "name": "Invalid calendar"}
        ).json()
        path = f"/api/v1/workspaces/{workspace['id']}/scan-schedule"
        response = client.put(path, json={"expected_revision": 0, "timezone": "UTC", **invalid})
        assert response.status_code == 422
        assert client.get(path).json()["state"] is None


def _status_error(status: int) -> httpx.HTTPStatusError:
    """Build the error raise_for_status gives for one career-page answer."""
    request = httpx.Request("GET", _CAREERS)
    return httpx.HTTPStatusError(
        f"Client error '{status}'",
        request=request,
        response=httpx.Response(status, request=request),
    )


_REFUSED = (422, "validation")
_UNREADABLE = (502, "career-page-unreadable")

_PAGE_FAILURES = [
    pytest.param(RobotsDeniedError(_CAREERS), _REFUSED, "robots.txt refuses", id="robots-denied"),
    pytest.param(_status_error(401), _REFUSED, "answered HTTP 401", id="status-401"),
    pytest.param(_status_error(403), _REFUSED, "answered HTTP 403", id="status-403"),
    pytest.param(
        RobotsUnavailableError("robots.txt answered 500"),
        _UNREADABLE,
        "robots.txt could not be read",
        id="robots-unavailable",
    ),
    pytest.param(
        DestinationUnresolvableError("no public address"),
        _UNREADABLE,
        "host name does not resolve",
        id="unresolvable",
    ),
    pytest.param(
        HostHeldError(_CAREERS, ready_at=0.0), _UNREADABLE, "asked for a wait", id="host-held"
    ),
    pytest.param(_status_error(404), _UNREADABLE, "answered HTTP 404", id="status-404"),
    pytest.param(_status_error(503), _UNREADABLE, "answered HTTP 503", id="status-503"),
    pytest.param(
        httpx.ReadTimeout("timed out", request=httpx.Request("GET", _CAREERS)),
        _UNREADABLE,
        "did not answer in time",
        id="timeout",
    ),
    pytest.param(
        httpx.RemoteProtocolError("peer closed", request=httpx.Request("GET", _CAREERS)),
        _UNREADABLE,
        "connection to the site failed",
        id="protocol",
    ),
]


def _raising(error: Exception) -> Callable[..., str]:
    """Return a career-page reader that fails the way the polite client does."""

    def read(self: BoardApiFetcher, url: str, **_: object) -> str:
        raise error

    return read


def _assert_page_problem(
    status: int, content_type: str, body: dict[str, Any], expected: tuple[int, str], cause: str
) -> None:
    assert (status, body["type"]) == (expected[0], f"urn:tailorcv:problem:{expected[1]}")
    assert content_type == "application/problem+json"
    assert body["detail"].startswith(f"The career page at {_CAREERS} could not be read:")
    assert cause in body["detail"]


@pytest.mark.parametrize(("error", "expected", "cause"), _PAGE_FAILURES)
def test_preview_names_why_a_career_page_could_not_be_read(
    durable_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: tuple[int, str],
    cause: str,
) -> None:
    """A policy refusal is the address's fault; a failed read is the site's. Both name the cause."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", _raising(error))
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="pv", name="Pv").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/sources/preview", json={"url": _CAREERS}
            )
    finally:
        database.dispose()

    _assert_page_problem(
        response.status_code, response.headers["content-type"], response.json(), expected, cause
    )


def test_adding_a_source_whose_career_page_is_refused_saves_nothing(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused add explains itself and leaves no half-resolved source behind."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", _raising(RobotsDeniedError(_CAREERS)))
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="ad", name="Ad").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            response = client.post(base, json={"name": "Initech", "url": _CAREERS})
            listed = client.get(base).json()["items"]
    finally:
        database.dispose()

    _assert_page_problem(
        response.status_code,
        response.headers["content-type"],
        response.json(),
        _REFUSED,
        "robots.txt refuses",
    )
    assert listed == []


def test_a_refused_re_add_leaves_an_archived_source_archived(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-adding a retired board whose page is now refused does not bring it back."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="ra", name="Ra").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            source_id = client.post(base, json={"name": "Initech", "url": _CAREERS}).json()["id"]
            assert client.delete(f"{base}/{source_id}").status_code == 204
            monkeypatch.setattr(BoardApiFetcher, "get_text", _raising(RobotsDeniedError(_CAREERS)))
            response = client.post(base, json={"name": "Initech", "url": _CAREERS})
        with database.session() as session:
            source = session.get(Source, source_id)
            assert source is not None
            archived_at = source.archived_at
    finally:
        database.dispose()

    assert response.status_code == 422
    assert archived_at is not None


def test_adding_a_source_to_an_unknown_workspace_reads_no_page(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown workspace is a 404 before any career page is fetched."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", _raising(RobotsDeniedError(_CAREERS)))
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        response = client.post(
            f"/api/v1/workspaces/{new_id()}/sources", json={"name": "Initech", "url": _CAREERS}
        )

    assert response.status_code == 404
    assert response.json()["type"] == "urn:tailorcv:problem:not-found"


def test_revalidating_a_source_whose_career_page_fails_keeps_the_saved_row(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed re-resolution names its cause and leaves the saved resolution as it was."""
    careers_page = (
        f'{_CAREERS}\n<a href="https://initech.wd5.myworkdayjobs.com/en-US/External">Open roles</a>'
    )
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: careers_page)
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="rv", name="Rv").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(base, json={"name": "Initech", "url": _CAREERS}).json()
            monkeypatch.setattr(
                BoardApiFetcher,
                "get_text",
                _raising(httpx.ReadTimeout("timed out", request=httpx.Request("GET", _CAREERS))),
            )
            response = client.post(f"{base}/{created['id']}/revalidate")
        with database.session() as session:
            source = session.get(Source, created["id"])
            assert source is not None
            saved = (source.url, source.resolved_platform, source.archived_at)
    finally:
        database.dispose()

    assert created["platform"] == "workday"
    _assert_page_problem(
        response.status_code,
        response.headers["content-type"],
        response.json(),
        _UNREADABLE,
        "did not answer in time",
    )
    assert saved == (_CAREERS, "workday", None)


def test_preview_of_an_address_the_client_may_not_fetch_is_a_validation_failure(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address no outbound read may target is the request's fault, not the site's."""
    monkeypatch.setattr(
        BoardApiFetcher, "get_text", _raising(InvalidFetchUrlError("not a public HTTP(S) URL"))
    )
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="iv", name="Iv").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/sources/preview", json={"url": _CAREERS}
            )
    finally:
        database.dispose()

    assert response.status_code == 422
    assert response.json()["type"] == "urn:tailorcv:problem:validation"
    assert "not a public web address" in response.json()["detail"]


def test_a_board_source_is_added_corrected_and_retired_over_http(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board source is written directly over HTTP, not only through a search."""
    # The HTTP dependency wiring builds its own fetcher with no injection seam,
    # so the lowest network call is patched directly rather than left to reach
    # a real address while resolving the address on add.
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="sources", name="Sources")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(
                base, json={"name": "Globex", "url": "https://www.globex.test/careers/"}
            )
            assert created.status_code == 200
            source_id = created.json()["id"]
            assert created.json()["platform"] is None
            assert created.json()["enabled"] is True
            assert created.json()["source_type"] == "employer_board"
            assert created.json()["employer"] == "Globex"

            corrected = client.patch(
                f"{base}/{source_id}",
                json={"url": "https://job-boards.greenhouse.io/globex", "enabled": False},
            )
            assert corrected.status_code == 200
            assert corrected.json()["platform"] == "greenhouse"
            assert corrected.json()["enabled"] is False
            assert corrected.json()["careers_url"] == "https://www.globex.test/careers/"

            careers = client.patch(
                f"{base}/{source_id}", json={"careers_url": "https://www.globex.test/"}
            )
            assert careers.status_code == 200
            assert careers.json()["careers_url"] == "https://www.globex.test/"
            refused = client.patch(
                f"{base}/{source_id}",
                json={"careers_url": "https://job-boards.greenhouse.io/globex"},
            )
            assert refused.status_code == 422
            assert "applicant tracking system" in refused.json()["detail"]
            cleared = client.patch(f"{base}/{source_id}", json={"careers_url": ""})
            assert cleared.status_code == 200
            assert cleared.json()["careers_url"] is None

            assert [item["url"] for item in client.get(base).json()["items"]] == [
                "https://job-boards.greenhouse.io/globex"
            ]

            assert client.delete(f"{base}/{source_id}").status_code == 204
            assert client.get(base).json()["items"] == []
    finally:
        database.dispose()


def test_a_source_is_renamed_over_http_and_a_held_name_is_refused(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename reaches the employer, and a name another source holds returns 409."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="rename", name="Rename")
        .id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            slugged = client.post(
                base, json={"name": "acme-corp", "url": "https://job-boards.greenhouse.io/acme"}
            ).json()
            client.post(
                base, json={"name": "Globex", "url": "https://job-boards.greenhouse.io/globex"}
            )

            renamed = client.patch(f"{base}/{slugged['id']}", json={"name": "Acme Corporation"})
            held = client.patch(f"{base}/{slugged['id']}", json={"name": "Globex"})

        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Acme Corporation"
        assert renamed.json()["employer"] == "Acme Corporation"
        assert held.status_code == 409
        assert held.json()["detail"] == "Another source is already named Globex."
    finally:
        database.dispose()


@pytest.mark.parametrize(
    ("source_type", "first", "second"),
    [
        pytest.param(
            "employer_board",
            "https://job-boards.greenhouse.io/acme",
            "https://jobs.lever.co/acme",
            id="board",
        ),
        pytest.param("aggregator", "https://digest.test/jobs", "https://other.test/jobs", id="agg"),
    ],
)
def test_adding_a_held_name_at_another_address_is_refused_and_the_same_address_is_kept(
    durable_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_type: str,
    first: str,
    second: str,
) -> None:
    """A hand add never rewrites a registered source; repeating it records only its terms."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    terms = {"url": "https://acme.test/terms", "condition": "Personal use only."}
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="held", name="Held").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(
                base, json={"name": "Acme", "url": first, "source_type": source_type}
            ).json()
            client.patch(f"{base}/{created['id']}", json={"enabled": False})
            clash = client.post(
                base, json={"name": "Acme", "url": second, "source_type": source_type}
            )
            repeat = client.post(
                base,
                json={"name": "Acme", "url": first, "source_type": source_type, "terms": terms},
            )
            listed = client.get(base).json()["items"]
    finally:
        database.dispose()

    assert clash.status_code == 409
    assert clash.json()["type"] == "urn:tailorcv:problem:conflict"
    assert clash.json()["detail"] == f"A source named Acme already reads {first}."
    assert repeat.status_code == 200
    assert repeat.json()["id"] == created["id"]
    assert repeat.json()["enabled"] is False
    assert repeat.json()["terms"] == terms
    assert [(item["id"], item["url"], item["enabled"]) for item in listed] == [
        (created["id"], first, False)
    ]


def test_re_adding_an_archived_name_at_another_address_is_refused_and_stays_archived(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retired board's scan history never moves onto a different address."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    first = "https://job-boards.greenhouse.io/acme"
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="ar", name="Ar").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            source_id = client.post(base, json={"name": "Acme", "url": first}).json()["id"]
            assert client.delete(f"{base}/{source_id}").status_code == 204
            clash = client.post(base, json={"name": "Acme", "url": "https://jobs.lever.co/acme"})
        with database.session() as session:
            source = session.get(Source, source_id)
            assert source is not None
            kept = (source.url, source.archived_at is not None)
    finally:
        database.dispose()

    assert clash.status_code == 409
    assert clash.json()["detail"] == f"A source named Acme, archived, already reads {first}."
    assert kept == (first, True)


def test_re_adding_one_of_two_archived_boards_restores_only_the_matched_board(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An employer's other archived board keeps its own address and stays archived."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    first = "https://job-boards.greenhouse.io/acme"
    second = "https://jobs.lever.co/acme"
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="tw", name="Tw").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            first_id = client.post(base, json={"name": "Acme", "url": first}).json()["id"]
            with database.session() as session:
                held = session.get(Source, first_id)
                assert held is not None
                sibling = Source(
                    workspace_id=workspace_id,
                    employer_id=held.employer_id,
                    name="Acme (guided 1)",
                    source_type="employer_board",
                    url=second,
                    priority=1,
                    enabled=True,
                    origin="search.guided_campaign",
                )
                session.add(sibling)
                session.flush()
                second_id = sibling.id
            assert client.delete(f"{base}/{first_id}").status_code == 204
            assert client.delete(f"{base}/{second_id}").status_code == 204
            restored = client.post(base, json={"name": "Acme", "url": second})
        with database.session() as session:
            rows = {
                source.id: (source.url, source.archived_at is None)
                for source in session.scalars(
                    select(Source).where(Source.id.in_([first_id, second_id]))
                )
            }
    finally:
        database.dispose()

    assert restored.status_code == 200
    assert restored.json()["id"] == second_id
    assert rows == {first_id: (first, False), second_id: (second, True)}


def test_re_adding_a_board_under_another_spelling_of_its_address_returns_it(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two addresses of one board tenant are the same source, not a conflict."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="sp", name="Sp").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(
                base, json={"name": "Acme", "url": "https://job-boards.greenhouse.io/acme"}
            ).json()
            repeat = client.post(
                base, json={"name": "Acme", "url": "https://boards.greenhouse.io/Acme"}
            )
    finally:
        database.dispose()

    assert repeat.status_code == 200
    assert (repeat.json()["id"], repeat.json()["url"]) == (created["id"], created["url"])


def test_an_aggregator_add_under_an_employer_board_s_name_is_refused(
    durable_database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name held by a source of another type is a conflict, not that source."""
    monkeypatch.setattr(BoardApiFetcher, "get_text", lambda self, url: "")
    first = "https://job-boards.greenhouse.io/acme"
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="ty", name="Ty").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            client.post(base, json={"name": "Acme", "url": first})
            clash = client.post(
                base, json={"name": "Acme", "url": first, "source_type": "aggregator"}
            )
            listed = client.get(base).json()["items"]
    finally:
        database.dispose()

    assert clash.status_code == 409
    assert clash.json()["detail"] == f"A source named Acme already reads {first}."
    assert [item["source_type"] for item in listed] == ["employer_board"]


def test_an_aggregator_is_registered_over_http_without_naming_an_employer(
    durable_database_url: str, tmp_path: Path
) -> None:
    """An aggregator lists many employers, so the create route may not demand one."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database)).create(slug="agg", name="Agg").id
    )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            base = f"/api/v1/workspaces/{workspace_id}/sources"
            created = client.post(
                base,
                json={
                    "name": "Openings Digest",
                    "url": "https://digest.test/jobs",
                    "source_type": "aggregator",
                },
            )

            assert created.status_code == 200
            assert created.json()["source_type"] == "aggregator"
            assert created.json()["employer"] is None
            assert [item["name"] for item in client.get(base).json()["items"]] == [
                "Openings Digest"
            ]
    finally:
        database.dispose()


@pytest.mark.parametrize("interrupted", [None, 0, 1])
def test_source_scan_history_publishes_source_settings_and_recorded_results(
    durable_database_url: str, tmp_path: Path, interrupted: int | None
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="history", name="History")
        .id
    )
    source_id = new_id()
    scan_id = new_id()
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Example",
            normalized_name=normalize_text("Example"),
            careers_url="https://boards.example.test/jobs",
        )
        session.add(employer)
        session.flush()
        source = Source(
            id=source_id,
            workspace_id=workspace_id,
            employer_id=employer.id,
            name="Example careers",
            source_type="employer_board",
            url="https://boards.example.test/jobs",
            priority=2,
            enabled=True,
        )
        session.add(source)
        scan_moment = utc_now()
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=scan_moment.date(),
                query_label="title-filter",
                status="completed",
                read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                raw_count=11,
                read_count=9,
                skip_reasons={
                    "missing_required_posting_identity": 2,
                    "incomplete_workday_listing": 1,
                },
                result_count=4,
                new_count=1,
                detail_attempted_count=3 + (interrupted or 0),
                detail_read_count=2,
                detail_failed_count=1,
                detail_interrupted_count=interrupted,
                detail_capped_count=4,
                detail_failure_reasons={},
                notes="",
                scan_started_at=scan_moment,
                scan_completed_at=scan_moment,
                duration_ms=0,
            )
        )
        seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [
                {
                    "url": "https://boards.example.test/jobs/page/2",
                    "label": "Next role",
                    "source_url": "https://boards.example.test/jobs",
                }
            ],
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.get(f"/api/v1/workspaces/{workspace_id}/source-scans")
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": scan_id,
                "task_id": None,
                "source_name": "Example careers",
                "employer": "Example",
                "source_url": "https://boards.example.test/jobs",
                "source_type": "employer_board",
                "priority": 2,
                "enabled": True,
                "scanned_on": str(utc_now().date()),
                "query_label": "title-filter",
                "kind": "title-filter",
                "status": "completed",
                "read_outcome": "crawl_continuation",
                "frontier_pending_count": 1,
                "frontier_completed_count": 0,
                "frontier_failed_count": 0,
                "automatic_pending_count": 1,
                "follow_up_state": "waiting",
                "kept_as": "latest",
                "screened_out_count": 0,
                "raw_count": 11,
                "read_count": 9,
                "skip_reasons": {
                    "missing_required_posting_identity": 2,
                    "bounded_result_cap": 0,
                    "title_excluded": 0,
                    "title_mismatch": 0,
                    "title_role_family_conflict": 0,
                    "title_role_family_review": 0,
                    "employer_excluded": 0,
                    "seniority_outside": 0,
                    "location_outside": 0,
                    "location_ineligible": 0,
                    "employment_excluded": 0,
                    "employment_outside": 0,
                    "arrangement_refused": 0,
                    "arrangement_office_limit": 0,
                    "compensation_below_minimum": 0,
                    "protected_posting": 0,
                    "incomplete_workday_listing": 1,
                    "repeated_workday_page": 0,
                    "duplicate_workday_identity": 0,
                    "incomplete_oracle_listing": 0,
                },
                "result_count": 4,
                "new_count": 1,
                "detail_attempted_count": 3 + (interrupted or 0),
                "detail_read_count": 2,
                "detail_failed_count": 1,
                "detail_interrupted_count": interrupted,
                "detail_capped_count": 4,
                "detail_failure_reasons": {
                    "timeout": 0,
                    "http_status": 0,
                    "parse": 0,
                    "other": 0,
                },
                "notes": "",
                "complete": None,
                "scan_started_at": scan_moment.isoformat().replace("+00:00", "Z"),
                "scan_completed_at": scan_moment.isoformat().replace("+00:00", "Z"),
                "duration_ms": 0,
            }
        ],
        "total": 1,
        "offset": 0,
        "limit": 50,
    }


def test_source_scan_history_filters_the_whole_history_on_the_server(
    durable_database_url: str, tmp_path: Path
) -> None:
    """A source or status beyond the first page is still found, with a matching total."""
    database = Database(durable_database_url)
    workspaces = WorkspaceService(database, runtime_storage(database))
    workspace_id = str(workspaces.create(slug="history-filters", name="History filters").id)
    other_workspace_id = str(workspaces.create(slug="other-history", name="Other history").id)
    with database.session() as session:

        def source(name: str, owner: str = workspace_id) -> str:
            record = Source(
                workspace_id=owner,
                name=name,
                source_type="employer_board",
                url=f"https://boards.example.test/{name.lower()}",
                priority=1,
                enabled=True,
            )
            session.add(record)
            session.flush()
            return record.id

        def scan(
            source_id: str,
            day: int,
            status: str,
            *,
            board: bool = True,
            owner: str = workspace_id,
            complete: bool | None = None,
        ) -> str:
            moment = datetime(2026, 1, day, tzinfo=UTC)
            task_id = None
            if board:
                task = BackgroundTask(
                    workspace_id=owner,
                    task_type="source.scan",
                    status="completed",
                    payload={},
                    payload_sha256="0" * 64,
                    idempotency_key=f"history-filter-{source_id}-{day}",
                )
                session.add(task)
                session.flush()
                task_id = task.id
            record = SourceScan(
                source_id=source_id,
                scanned_on=moment.date(),
                query_label="title-filter" if board else "off-board",
                correlation_id=task_id,
                status=status,
                read_outcome=SourceReadOutcome.ATS_COMPLETE,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                complete=complete,
                scan_started_at=moment,
                scan_completed_at=moment,
                duration_ms=0,
            )
            session.add(record)
            session.flush()
            return record.id

        busy_id = source("Busy")
        quiet_id = source("Quiet")
        scan(quiet_id, 1, "completed", complete=True)
        latest_quiet = scan(quiet_id, 2, "failed")
        busy_scans = [scan(busy_id, day, "completed") for day in range(3, 9)]
        guided_quiet = scan(quiet_id, 9, "completed", board=False)
        foreign_id = source("Foreign", other_workspace_id)
        scan(foreign_id, 1, "completed", owner=other_workspace_id)

    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            url = f"/api/v1/workspaces/{workspace_id}/source-scans"
            quiet = client.get(url, params={"source_id": quiet_id, "limit": 1}).json()
            failed = client.get(url, params={"status": "failed", "limit": 1}).json()
            latest = client.get(url, params={"latest_only": True}).json()
            unknown = client.get(url, params={"status": "running"})
            foreign = client.get(url, params={"source_id": foreign_id}).json()
    finally:
        database.dispose()

    assert [item["id"] for item in quiet["items"]] == [guided_quiet]
    assert quiet["total"] == 3
    assert [item["id"] for item in failed["items"]] == [latest_quiet]
    assert failed["total"] == 1
    assert [item["id"] for item in latest["items"]] == [busy_scans[-1], latest_quiet]
    assert (foreign["items"], foreign["total"]) == ([], 0)
    assert latest["total"] == 2
    assert unknown.status_code == 422


def test_continue_source_scan_enqueues_a_bounded_task_over_http(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="continue", name="Continue")
        .id
    )
    source_id = new_id()
    scan_id = new_id()
    empty_source_id = new_id()
    empty_scan_id = new_id()
    with database.session() as session:
        employer = Employer(
            workspace_id=workspace_id,
            name="Example",
            normalized_name=normalize_text("Example"),
            careers_url="https://boards.example.test/jobs",
        )
        session.add(employer)
        session.flush()
        session.add(
            Source(
                id=source_id,
                workspace_id=workspace_id,
                employer_id=employer.id,
                name="Example careers",
                source_type="employer_board",
                url="https://boards.example.test/jobs",
                priority=1,
                enabled=True,
            )
        )
        session.add(
            Source(
                id=empty_source_id,
                workspace_id=workspace_id,
                employer_id=employer.id,
                name="Fully read careers",
                source_type="employer_board",
                url="https://boards.example.test/other-jobs",
                priority=2,
                enabled=True,
            )
        )
        scan_moment = utc_now()
        session.add(
            SourceScan(
                id=scan_id,
                source_id=source_id,
                scanned_on=scan_moment.date(),
                query_label="title-filter",
                status="incomplete",
                read_outcome=SourceReadOutcome.CRAWL_CONTINUATION,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                scan_started_at=scan_moment,
                scan_completed_at=scan_moment,
                duration_ms=0,
            )
        )
        session.add(
            SourceScan(
                id=empty_scan_id,
                source_id=empty_source_id,
                scanned_on=scan_moment.date(),
                query_label="title-filter",
                status="completed",
                read_outcome=SourceReadOutcome.ATS_COMPLETE,
                raw_count=0,
                read_count=0,
                skip_reasons={},
                result_count=0,
                new_count=0,
                notes="",
                scan_started_at=scan_moment,
                scan_completed_at=scan_moment,
                duration_ms=0,
            )
        )
        continuation_id = seed_scan_continuation(
            session,
            workspace_id,
            scan_id,
            [
                {
                    "url": "https://boards.example.test/jobs/page/2",
                    "label": "Next role",
                    "source_url": "https://boards.example.test/jobs",
                }
            ],
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"),
            base_url="http://localhost",
        ) as client:
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/source-scans/{scan_id}/continue"
            )
            empty = client.post(
                f"/api/v1/workspaces/{workspace_id}/source-scans/{empty_scan_id}/continue"
            )
    finally:
        database.dispose()

    assert response.status_code == 202
    body = response.json()
    assert body["task_type"] == "source.continue"
    with database.session() as session:
        task = session.get(BackgroundTask, body["id"])
        assert task is not None
        assert task.payload == {
            "continuation_id": continuation_id,
            "generation": 1,
            "include_policy_deferred": True,
            "no_progress_count": 0,
        }

    assert empty.status_code == 422
    assert "no deferred publisher links" in empty.json()["detail"]


def test_source_scan_outcomes_are_scoped_filtered_and_sorted(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="scan-outcomes", name="Scan outcomes")
        .id
    )
    state_target_titles(database, workspace_id, "Data Platform Lead")
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="outcome-ledger",
    )
    with database.session() as session:
        session.add_all(
            [
                SourceScanOutcome(
                    id=new_id(),
                    workspace_id=workspace_id,
                    task_id=str(task.id),
                    source_name="Example careers",
                    candidate_kind="board_listing",
                    url="https://example.test/jobs/one",
                    normalized_url="https://example.test/jobs/one",
                    employer="Acme",
                    title="Data Product Lead",
                    location="Remote",
                    terminal_stage="rejected",
                    reason="excluded_employer",
                    detail="The listing names an ineligible place.",
                    screening_findings=[
                        {"dimension": "location", "code": "location_outside", "subject": "Berlin"}
                    ],
                ),
                SourceScanOutcome(
                    id=new_id(),
                    workspace_id=workspace_id,
                    task_id=str(task.id),
                    source_name="Example careers",
                    candidate_kind="board_listing",
                    url="https://example.test/jobs/two",
                    normalized_url="https://example.test/jobs/two",
                    employer="Beta",
                    title="Platform Product Manager",
                    location="Canada",
                    terminal_stage="staged",
                    reason="staged",
                    detail="",
                    screening_findings=[],
                ),
            ]
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes",
                params={
                    "task_id": str(task.id),
                    "search": "platform",
                    "sort": "employer",
                    "direction": "asc",
                },
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["sources"] == ["Example careers"]
    assert response.json()["terminal_stages"] == ["rejected", "staged"]
    assert response.json()["reasons"] == ["excluded_employer", "staged"]
    assert response.json()["items"] == [
        {
            "id": response.json()["items"][0]["id"],
            "task_id": str(task.id),
            "source_scan_id": None,
            "source_name": "Example careers",
            "candidate_kind": "board_listing",
            "url": "https://example.test/jobs/two",
            "employer": "Beta",
            "title": "Platform Product Manager",
            "location": "Canada",
            "terminal_stage": "staged",
            "reason": "staged",
            "detail": "",
            "screening_findings": [],
            "created_at": response.json()["items"][0]["created_at"],
        }
    ]


def test_source_scan_outcomes_of_same_day_scan_tasks_stay_with_their_task(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    try:
        workspace_id, first, second = same_day_rescans_with_follow_ups(database)
    finally:
        database.dispose()
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
    ) as client:
        pages = {
            task_id: client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes",
                params={"task_id": task_id},
            )
            for task_id in (first, second)
        }

    assert {response.status_code for response in pages.values()} == {200}
    assert {item["title"] for item in pages[first].json()["items"]} == {
        "Alpha Scan Lead",
        "Alpha Follow-up Lead",
        "Alpha Chained Lead",
    }
    assert {item["title"] for item in pages[second].json()["items"]} == {
        "Bravo Scan Lead",
        "Bravo Follow-up Lead",
    }


@pytest.mark.parametrize(
    ("search", "matched"),
    [("zürich", "zurich"), ("ZÜRICH", "zurich"), ("100%", "percent"), ("data_ops", "underscore")],
)
def test_source_scan_outcome_search_folds_accents_and_matches_wildcards_literally(
    durable_database_url: str, tmp_path: Path, search: str, matched: str
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="scan-outcome-search", name="Scan outcome search")
        .id
    )
    state_target_titles(database, workspace_id, "Data Platform Lead")
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="outcome-search",
    )
    rows = {
        "zurich": ("Data Analyst", "ZÜRICH, SWITZERLAND"),
        "percent": ("Analyst (100% remote)", "Remote"),
        "decoy-percent": ("Analyst (100 days onsite)", "Remote"),
        "underscore": ("Engineer, data_ops", "Remote"),
        "decoy-underscore": ("Engineer, data-ops", "Remote"),
    }
    with database.session() as session:
        session.add_all(
            SourceScanOutcome(
                id=new_id(),
                workspace_id=workspace_id,
                task_id=str(task.id),
                source_name="Example careers",
                candidate_kind="board_listing",
                url=f"https://example.test/jobs/{key}",
                normalized_url=f"https://example.test/jobs/{key}",
                employer="Acme",
                title=title,
                location=location,
                terminal_stage="staged",
                reason="staged",
                detail="",
                screening_findings=[],
            )
            for key, (title, location) in rows.items()
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes",
                params={"task_id": str(task.id), "search": search},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert [item["url"] for item in response.json()["items"]] == [
        f"https://example.test/jobs/{matched}"
    ]


def test_source_scan_outcome_findings_reach_the_ledger(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="scan-findings", name="Scan findings")
        .id
    )
    state_target_titles(database, workspace_id, "Data Platform Lead")
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="source.scan",
        payload={},
        idempotency_key="finding-ledger",
    )
    with database.session() as session:
        session.add(
            SourceScanOutcome(
                id=new_id(),
                workspace_id=workspace_id,
                task_id=str(task.id),
                source_name="Example careers",
                candidate_kind="board_listing",
                url="https://example.test/jobs/one",
                normalized_url="https://example.test/jobs/one",
                employer="Acme",
                title="Data Product Lead",
                location="Berlin",
                terminal_stage="rejected",
                reason="excluded_employer",
                detail="The listing names an excluded employer.",
                # The funnel stores non-match findings without a standing field.
                screening_findings=[
                    {"dimension": "employer", "code": "employer_excluded", "subject": "Acme"}
                ],
            )
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes",
                params={"task_id": str(task.id)},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert response.json()["items"][0]["screening_findings"] == [
        {"dimension": "employer", "code": "employer_excluded", "subject": "Acme"}
    ]


def test_a_campaign_retry_pass_can_be_read_back_by_its_own_task(
    durable_database_url: str, tmp_path: Path
) -> None:
    """A retry pass writes under its own task id, so its outcomes must be readable there."""
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="retry-outcomes", name="Retry outcomes")
        .id
    )
    task = TaskQueueService(database).enqueue(
        workspace_id=workspace_id,
        task_type="search.guided_campaign_retry",
        payload={},
        idempotency_key="retry-outcomes",
    )
    with database.session() as session:
        session.add(
            SourceScanOutcome(
                id=new_id(),
                workspace_id=workspace_id,
                task_id=str(task.id),
                source_name="search.guided_campaign",
                candidate_kind="direct_posting",
                url="https://example.test/jobs/two",
                normalized_url="https://example.test/jobs/two",
                employer="Globex",
                title="Warehouse Systems Lead",
                location="Toronto",
                terminal_stage="staged",
                reason="staged",
                detail="The campaign staged this posting.",
                origin="search.guided_campaign",
            )
        )
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            response = client.get(
                f"/api/v1/workspaces/{workspace_id}/source-scan-outcomes",
                params={"task_id": str(task.id)},
            )
    finally:
        database.dispose()

    assert response.status_code == 200
    assert [item["url"] for item in response.json()["items"]] == ["https://example.test/jobs/two"]


def _screened_out(workspace_id: str, slug: str, title: str) -> ScreenedOutTitle:
    url = f"https://example.test/jobs/{slug}"
    seen = datetime(2025, 3, 1, tzinfo=UTC)
    return ScreenedOutTitle(
        id=new_id(),
        workspace_id=workspace_id,
        normalized_url=url,
        url=url,
        fingerprint=url_sha256(url),
        source_name="Example careers",
        employer="Acme",
        title=title,
        reason="insufficient_title_evidence",
        title_code="title_mismatch",
        title_subject="",
        first_seen_at=seen,
        last_seen_at=seen,
    )


def test_screened_out_titles_are_listed_and_marked_reviewed(
    durable_database_url: str, tmp_path: Path
) -> None:
    database = Database(durable_database_url)
    workspace_id = str(
        WorkspaceService(database, runtime_storage(database))
        .create(slug="screened-out-titles", name="Screened out titles")
        .id
    )
    rows = [
        _screened_out(workspace_id, "one", "Sales Lead"),
        _screened_out(workspace_id, "two", "Nurse"),
    ]
    with database.session() as session:
        session.add_all(rows)
    route = f"/api/v1/workspaces/{workspace_id}/screened-out-titles"
    try:
        with TestClient(
            create_app(durable_database_url, tmp_path / "artifacts"), base_url="http://localhost"
        ) as client:
            reviewed = client.post(f"{route}/review", json={"ids": [rows[0].id]})
            pending = client.get(route, params={"needs_review": "true"})
            empty = client.post(f"{route}/review", json={"ids": []})
            foreign = client.post(f"{route}/review", json={"ids": [new_id()]})
            unknown = client.get("/api/v1/workspaces/missing/screened-out-titles")
    finally:
        database.dispose()

    assert reviewed.status_code == 200
    assert reviewed.json() == {"reviewed": 1}
    assert pending.status_code == 200
    body = pending.json()
    assert (body["total"], body["all_count"], body["needs_review_count"]) == (1, 2, 1)
    assert body["sources"] == ["Example careers"]
    [item] = body["items"]
    assert (item["title"], item["review_state"], item["reviewed_at"]) == ("Nurse", "new", None)
    assert empty.status_code == 422
    assert foreign.status_code == 422
    assert unknown.status_code == 404
