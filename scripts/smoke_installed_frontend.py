"""Exercise installed assets, managed startup, and the free first-use loop."""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
from fastapi.testclient import TestClient

import tailorcv
from tailorcv.application.config import CONFIG_FILENAME, find_config
from tailorcv.application.job_search.models import ClosingDateEditInput, ManualJobReviewInput
from tailorcv.application.workspaces.models import ScanScheduleInput
from tailorcv.bootstrap.api import create_app
from tailorcv.bootstrap.worker import Worker, default_handlers
from tailorcv.delivery.api.models.search import SearchParametersBody
from tailorcv.delivery.api.models.tasks import TaskCreate
from tailorcv.delivery.api.models.workspaces import SourceCreate, WorkspaceCreate
from tailorcv.delivery.cli.support import open_database
from tailorcv.domain import SourceReadOutcome, TaskStatus
from tailorcv.domain.search_parameters import (
    Acceptance,
    Arrangement,
    JobSearchParameters,
    Location,
    Seniority,
    TargetTitle,
)
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage
from tailorcv.infrastructure.persistence.database import sqlite_path
from tailorcv.infrastructure.web_fetch.policy import PoliteHttpClient

ASSET_REFERENCE = re.compile(r"(?:src|href)=[\"'](/assets/[^\"']+)[\"']")
POSTING_URL = "https://job-boards.greenhouse.io/acme/jobs/101"
BOARD_API = "https://boards-api.greenhouse.io/v1/boards/"
PUBLISHER_DATE = date(2099, 12, 31)
PERSONAL_DATE = date(2099, 12, 15)


def _cli(root: Path, *arguments: str, expected: int = 0) -> str:
    """Run the installed CLI against the isolated data root."""
    result = subprocess.run(  # noqa: S603 - installed executable and fixed caller arguments
        [str(Path(sys.prefix) / "bin" / "tailorcv"), *arguments, "--data-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, (arguments, result.stdout, result.stderr)
    return result.stdout


def _json(client: httpx.Client | TestClient, method: str, path: str, **kwargs: Any) -> Any:
    """Require a successful typed API operation before reading its response."""
    response = client.request(method, path, **kwargs)
    assert response.is_success, (method, path, response.status_code, response.text)
    return response.json()


def _assets(client: httpx.Client) -> int:
    """Request the installed index and every local asset it references."""
    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assets = set(ASSET_REFERENCE.findall(index.text))
    assert assets, "Installed HTML has no bundled asset references"
    for path in assets:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content, path
    return len(assets)


def _onboard(
    client: httpx.Client | TestClient,
) -> tuple[str, list[str], dict[str, Any], dict[str, Any]]:
    """Create an empty workspace and save manual search preferences over HTTP."""
    schema = _json(client, "GET", "/api/v1/openapi.json")
    alias = client.get("/openapi.json")
    assert alias.status_code == 200 and "application/json" in alias.headers["content-type"]
    assert alias.json() == schema
    assert {
        "/api/v1/workspaces",
        "/api/v1/job-search-parameters",
        "/api/v1/tasks",
        "/api/v1/jobs",
    } <= schema["paths"].keys()
    providers = _json(client, "GET", "/api/v1/providers")
    assert providers and all(not provider["key_present"] for provider in providers)
    body = WorkspaceCreate(slug="installed-first-use", name="Installed first use")
    workspace = _json(client, "POST", "/api/v1/workspaces", json=body.model_dump(mode="json"))
    workspace_id = workspace["id"]
    assert _json(client, "GET", f"/api/v1/workspaces/{workspace_id}/master-cv/versions") == []
    current = _json(
        client, "GET", "/api/v1/job-search-parameters", params={"workspace_id": workspace_id}
    )
    parameters = JobSearchParameters(
        seniority=Seniority(target_titles=[TargetTitle(title="Data Engineer")]),
        locations=[Location(key="canada", country="Canada")],
        arrangement=Arrangement(remote=Acceptance.PREFERRED),
    )
    saved = _json(
        client,
        "PUT",
        "/api/v1/job-search-parameters",
        json=SearchParametersBody(
            workspace_id=workspace_id,
            parameters=parameters,
            expected_version=current["version"],
        ).model_dump(mode="json"),
    )
    assert saved["version"] > current["version"]
    assert saved["parameters"] == parameters.model_dump(mode="json")
    source_ids = []
    for name, board in (("Acme", "acme"), ("Northwind Analytics", "northwind")):
        source = _json(
            client,
            "POST",
            f"/api/v1/workspaces/{workspace_id}/sources",
            json=SourceCreate(
                name=name, url=f"https://job-boards.greenhouse.io/{board}"
            ).model_dump(mode="json"),
        )
        source_ids.append(source["id"])
    schedule_path = f"/api/v1/workspaces/{workspace_id}/scan-schedule"
    assert _json(client, "GET", schedule_path)["state"] is None
    schedule_body = ScanScheduleInput(
        expected_revision=0,
        frequency="weekly",
        local_time="17:45",
        timezone="UTC",
        weekday=4,
        scope="selected",
        source_ids=[source_ids[0]],
        paused=True,
    )
    schedule = _json(client, "PUT", schedule_path, json=schedule_body.model_dump(mode="json"))
    assert schedule["state"]["revision"] == 1, schedule
    assert schedule["state"]["configuration"] == schedule_body.model_dump(
        mode="json", exclude={"expected_revision"}
    )
    assert schedule["state"]["next_run_at"] is None, schedule
    assert schedule["state"]["last_task_id"] is None, schedule
    assert schedule["unavailable_source_ids"] == [], schedule
    assert _json(client, "GET", schedule_path) == schedule
    return workspace_id, source_ids, saved, schedule


def _synthetic_get(url: str, **_kwargs: object) -> httpx.Response:
    """Serve a matching board and an unreadable board without opening a network socket."""
    request = httpx.Request("GET", url)
    if url in {
        "https://boards-api.greenhouse.io/robots.txt",
        "https://job-boards.greenhouse.io/robots.txt",
    }:
        return httpx.Response(200, request=request, text="User-agent: *\nAllow: /\n")
    if url == f"{BOARD_API}acme/jobs?content=true":
        return httpx.Response(
            200,
            request=request,
            json={
                "jobs": [
                    {
                        "id": 101,
                        "title": "Data Engineer",
                        "absolute_url": POSTING_URL,
                        "company_name": "Acme",
                        "location": {"name": "Canada"},
                        "content": (
                            "<p>Data Engineer, remote in Canada. Build data pipelines, "
                            "maintain reliable integrations, and document data quality "
                            "checks. Work with engineers to review changes and support "
                            "production datasets. #LI-remote</p>"
                            f"<p>Applications close: {PUBLISHER_DATE.isoformat()}</p>"
                        ),
                    }
                ]
            },
        )
    if url == f"{BOARD_API}northwind/jobs?content=true":
        return httpx.Response(404, request=request, text="Synthetic board unavailable")
    raise AssertionError(f"Unexpected external GET: {url}")


def _reject_post(url: str, **_kwargs: object) -> httpx.Response:
    """Refuse every external write and provider request."""
    raise AssertionError(f"Unexpected external POST: {url}")


def _synthetic_http() -> PoliteHttpClient:
    """Build the policy client with synthetic network effects and a controlled clock."""
    synthetic_seconds = 0.0

    def advance(seconds: float) -> None:
        nonlocal synthetic_seconds
        synthetic_seconds += seconds

    return PoliteHttpClient(
        user_agent="tailorcv/installed-smoke",
        getter=_synthetic_get,
        poster=_reject_post,
        resolver=lambda _host, port: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
        sleeper=advance,
        clock=lambda: synthetic_seconds,
        jitter=lambda _low, _high: 0.0,
    )


def _scan(root: Path, workspace_id: str, source_ids: list[str]) -> dict[str, Any]:
    """Consume the API's persisted scan using the installed worker and injected transport."""
    storage = RuntimeStorage.resolve(data_root=root)
    with (
        open_database(storage.database_url) as database,
        closing(_synthetic_http()) as web,
        TestClient(create_app(data_root=root), base_url="http://localhost") as client,
    ):
        worker = Worker(
            database,
            worker_id="installed-synthetic-worker",
            handlers=default_handlers(database, artifact_root=storage, http_client=web),
        )
        worker.queue.renew_worker_presence(worker_id=worker.worker_id)
        try:
            task = _json(
                client,
                "POST",
                "/api/v1/tasks",
                json=TaskCreate(
                    workspace_id=workspace_id,
                    task_type="source.scan",
                    payload={"source_ids": source_ids},
                ).model_dump(mode="json"),
                headers={"Idempotency-Key": "installed-first-scan"},
            )
            for _ in range(10):
                if not worker.run_once():
                    break
            else:
                raise AssertionError("Synthetic scan created unbounded follow-up work")
            detail = _json(
                client,
                "GET",
                f"/api/v1/tasks/{task['id']}",
                params={"workspace_id": workspace_id},
            )
            assert detail["status"] == TaskStatus.COMPLETED.value, detail
            page = _json(
                client, "GET", f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page"
            )
            assert page["queue_total"] == 1, page
            pending = (page["ready"]["items"] + page["attention"]["items"])[0]
            assert pending["title"] == "Data Engineer", pending
            publisher = pending["publisher_closing_date"]
            assert publisher is not None, pending
            assert publisher["normalized_value"] == PUBLISHER_DATE.isoformat(), publisher
            assert publisher["precision"] == "date" and publisher["timezone"] is None, publisher
            assert publisher["source_url"] == POSTING_URL, publisher
            assert publisher["artifact_id"] and publisher["observed_at"], publisher
            assert not publisher["needs_clarification"], publisher
            assert len(publisher["statements"]) == 1, publisher
            statement = publisher["statements"][0]
            assert statement["wording"] == f"Applications close: {PUBLISHER_DATE.isoformat()}"
            assert statement["basis"] == "body_label", statement
            assert statement["artifact_id"] == publisher["artifact_id"], publisher
            assert statement["precision"] == "date" and statement["timezone"] is None, statement
            assert statement["normalized_value"] == PUBLISHER_DATE.isoformat(), statement
            assert pending["closing_date_override"] is None, pending
            closing_path = (
                f"/api/v1/workspaces/{workspace_id}"
                f"/pending-discoveries/{pending['id']}/closing-date"
            )
            for value in (PERSONAL_DATE, None, PERSONAL_DATE):
                edited = _json(
                    client,
                    "PUT",
                    closing_path,
                    json=ClosingDateEditInput(closing_date_override=value).model_dump(mode="json"),
                )
                expected = value.isoformat() if value is not None else None
                assert edited["closing_date_override"] == expected, edited
                assert edited["publisher_closing_date"] == publisher, edited
                reloaded = _json(
                    client, "GET", f"/api/v1/workspaces/{workspace_id}/pending-discoveries/page"
                )
                items = reloaded["ready"]["items"] + reloaded["attention"]["items"]
                assert len(items) == 1, reloaded
                assert items[0]["closing_date_override"] == expected, reloaded
                assert items[0]["publisher_closing_date"] == publisher, reloaded
            invalid = client.put(
                closing_path,
                json={"closing_date_override": f"{PERSONAL_DATE.isoformat()}T00:00:00Z"},
            )
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["type"] == "urn:tailorcv:problem:request-validation"
            tracked = _json(
                client,
                "POST",
                f"/api/v1/workspaces/{workspace_id}/pending-discoveries/{pending['id']}/track",
            )
            assert tracked["evaluation_id"] is None, tracked
            assert tracked["closing_date_override"] == PERSONAL_DATE.isoformat(), tracked
            assert tracked["publisher_closing_date"] == publisher, tracked
            reviewed = _json(
                client,
                "PUT",
                f"/api/v1/jobs/{tracked['job_posting_id']}/manual-review",
                params={"workspace_id": workspace_id},
                json=ManualJobReviewInput(
                    priority="high",
                    note="Check the application instructions.",
                    recorded_deadline="Friday at 5 pm",
                ).model_dump(mode="json"),
            )
            assert reviewed["recorded_deadline"] == "Friday at 5 pm", reviewed
            assert reviewed["closing_date_override"] == PERSONAL_DATE.isoformat(), reviewed
            assert reviewed["publisher_closing_date"] == publisher, reviewed
            return dict(reviewed)
        finally:
            worker.queue.withdraw_worker_presence(worker_id=worker.worker_id)


def _persisted(
    client: httpx.Client | TestClient,
    workspace_id: str,
    source_ids: list[str],
    saved: dict[str, Any],
    schedule: dict[str, Any],
    tracked: dict[str, Any],
) -> None:
    """Read saved first-use state through the restarted installed server."""
    parameters = _json(
        client, "GET", "/api/v1/job-search-parameters", params={"workspace_id": workspace_id}
    )
    assert parameters["version"] == saved["version"]
    assert parameters["parameters"] == saved["parameters"]
    assert _json(client, "GET", f"/api/v1/workspaces/{workspace_id}/scan-schedule") == schedule
    sources = _json(client, "GET", f"/api/v1/workspaces/{workspace_id}/sources")
    assert set(source_ids) <= {source["id"] for source in sources["items"]}
    scans = _json(client, "GET", f"/api/v1/workspaces/{workspace_id}/source-scans")
    assert len(scans["items"]) == 2, scans
    by_source = {scan["source_name"]: scan for scan in scans["items"]}
    assert by_source["Acme"]["new_count"] == 1, scans
    assert (
        by_source["Northwind Analytics"]["read_outcome"] == SourceReadOutcome.FETCH_FAILED.value
    ), scans
    assert by_source["Northwind Analytics"]["notes"], scans
    ledger = _json(client, "GET", "/api/v1/jobs", params={"workspace_id": workspace_id})
    assert ledger["total"] == 1, ledger
    job = ledger["items"][0]
    assert job["job_posting_id"] == tracked["job_posting_id"] and job["title"] == "Data Engineer", (
        job
    )
    assert job["overall_score"] is None, job
    detail = _json(
        client,
        "GET",
        f"/api/v1/jobs/{tracked['job_posting_id']}",
        params={"workspace_id": workspace_id},
    )
    for record in (job, detail):
        assert record["closing_date_override"] == PERSONAL_DATE.isoformat(), record
        assert record["publisher_closing_date"] == tracked["publisher_closing_date"], record
        assert record["recorded_deadline"] == "Friday at 5 pm", record
        assert record["manual_note"] == tracked["manual_note"], record
    assert _json(client, "GET", f"/api/v1/workspaces/{workspace_id}/master-cv/versions") == []
    assert all(not item["key_present"] for item in _json(client, "GET", "/api/v1/providers"))


def _first_use(root: Path) -> int:
    """Initialize storage, start both managed processes, and verify persistence after restart."""
    package = Path(tailorcv.__file__).resolve()
    checkout = Path(__file__).resolve().parents[1]
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert not package.is_relative_to(checkout), "Smoke imported TailorCV from the checkout"
    assert find_config() == Path.cwd() / CONFIG_FILENAME
    _cli(root, "db", "upgrade")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    try:
        _cli(root, "start", "--port", str(port))
        assert "is running" in _cli(root, "status")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            asset_count = _assets(client)
            workspace_id, source_ids, saved, schedule = _onboard(client)
        _no_paid_work(root)
        # The synthetic transport exists only in this process.
        _cli(root, "stop")
        _cli(root, "status", expected=1)
        tracked = _scan(root, workspace_id, source_ids)
        _no_paid_work(root)
        _cli(root, "start", "--port", str(port))
        assert "is running" in _cli(root, "status")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            _persisted(client, workspace_id, source_ids, saved, schedule, tracked)
            assert _assets(client) == asset_count
        _no_paid_work(root)
    finally:
        _cli(root, "stop")
    _cli(root, "status", expected=1)
    print(f"Installed frontend served {asset_count} referenced assets")
    print("Installed CLI started and restarted the server and managed worker")
    print("Installed injected worker scanned synthetic boards; free tracking survived restart")
    print(
        "Publisher date provenance, personal date, deadline note, "
        "and paused schedule survived restart"
    )
    return 0


def _no_paid_work(root: Path) -> None:
    """Require the fresh ledger to contain only free scan work and no provider records."""
    path = sqlite_path(RuntimeStorage.resolve(data_root=root).database_url)
    assert path is not None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
        counts = database.execute(
            "SELECT (SELECT COUNT(*) FROM model_calls), "
            "(SELECT COUNT(*) FROM cost_records), (SELECT COUNT(*) FROM job_evaluations)"
        ).fetchone()
        assert counts == (0, 0, 0), counts
        task_types = {
            row[0] for row in database.execute("SELECT DISTINCT task_type FROM background_tasks")
        }
        assert task_types <= {"source.scan", "discovery.rescreen_queue"}, task_types


def main() -> int:
    """Run first use with isolated storage, configuration, and unavailable credentials."""
    if len(sys.argv) == 3 and sys.argv[1] == "--isolated-root":
        return _first_use(Path(sys.argv[2]))
    with TemporaryDirectory(prefix="tailorcv-installed-") as temporary:
        root = Path(temporary)
        (root / CONFIG_FILENAME).write_text("", encoding="ascii")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT"}
        }
        environment.update(
            XDG_CONFIG_HOME=str(root / "config"),
            PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        )
        return subprocess.run(  # noqa: S603 - current interpreter and this script
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--isolated-root",
                str(root / "data"),
            ],
            cwd=root,
            env=environment,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
