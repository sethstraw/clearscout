"""Journeys over the shipped API, migrations, services, and worker.

Nothing here is faked except a paid provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tailorcv.application.tasks import TaskQueueService
from tailorcv.bootstrap.api import create_app
from tailorcv.bootstrap.worker import Worker
from tailorcv.domain import TaskStatus, utc_now
from tailorcv.infrastructure.persistence.database import Database
from tests.support.scenarios import evaluation_commit, populated_workspace

SCOPED_COLLECTIONS = (
    "/api/v1/jobs",
    "/api/v1/notes",
    "/api/v1/tags",
    "/api/v1/tasks",
    "/api/v1/artifacts",
    "/api/v1/saved-views",
)


def _present_worker(database: Database) -> None:
    """Advertise the scenario's worker for longer than any journey here takes."""
    TaskQueueService(database).renew_worker_presence(worker_id="worker-a", ttl_seconds=3600)


@pytest.fixture
def api(
    durable_database: Database, durable_database_url: str, tmp_path: Path
) -> Iterator[TestClient]:
    """The shipped application over a migrated database, with a worker present."""
    _present_worker(durable_database)
    with TestClient(
        create_app(durable_database_url, tmp_path / "artifacts"),
        base_url="http://localhost",
    ) as client:
        yield client


def test_records_do_not_leak_between_two_populated_workspaces(api: TestClient) -> None:
    """Reading with an unknown id passes whether or not anything is scoped."""
    first = populated_workspace(api, "north", "Northwind Analytics")
    second = populated_workspace(api, "south", "Southgate Health")

    for path in SCOPED_COLLECTIONS:
        response = api.get(path, params={"workspace_id": first["workspace"]})
        assert response.status_code == 200, (path, response.text)
        payload = response.json()
        items = payload["items"] if isinstance(payload, dict) else payload
        assert items, f"{path} returned nothing, so it cannot show isolation"
        rendered = response.text
        assert "Southgate" not in rendered, f"{path} leaked the other workspace"
        assert second["job"] not in rendered, f"{path} leaked the other workspace's job"

    for path in (
        f"/api/v1/jobs/{second['job']}",
        f"/api/v1/jobs/{second['job']}/fit-report",
        f"/api/v1/tasks/{second['task']}",
    ):
        crossed = api.get(path, params={"workspace_id": first["workspace"]})
        assert crossed.status_code == 404, (path, crossed.status_code)

    # The same route with its own workspace still answers.
    assert (
        api.get(
            f"/api/v1/jobs/{second['job']}", params={"workspace_id": second["workspace"]}
        ).status_code
        == 200
    )


def test_queueing_survives_a_clock_jump_between_worker_setup_and_the_request(
    api: TestClient,
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that suspends mid-journey must not turn a present worker into a 503."""
    workspace = api.post("/api/v1/workspaces", json={"slug": "clock", "name": "Clock"})
    assert workspace.status_code == 201, workspace.text
    clock = [utc_now()]
    monkeypatch.setattr("tailorcv.application.tasks.leases.utc_now", lambda: clock[0])
    _present_worker(durable_database)
    clock[0] += timedelta(seconds=121)

    queued = api.post(
        "/api/v1/tasks",
        json={
            "workspace_id": workspace.json()["id"],
            "task_type": "artifact.verify",
            "payload": {"artifact_id": "missing"},
        },
        headers={"Idempotency-Key": "clock-jump"},
    )

    assert queued.status_code == 202, queued.text


def test_the_same_application_event_twice_leaves_one_logical_outcome(api: TestClient) -> None:
    """A retry after a dropped response must not add to a history nothing can delete."""
    workspace = populated_workspace(api, "ledger", "Northwind Analytics")
    body = {
        "status": "package_ready",
        "occurred_at": "2026-01-03T12:00:00Z",
        "notes": "Approved locally; nothing submitted.",
    }
    post = lambda key: api.post(  # noqa: E731
        f"/api/v1/jobs/{workspace['job']}/application-events",
        params={"workspace_id": workspace["workspace"]},
        json=body,
        headers={"Idempotency-Key": key},
    )

    first = post("ready-1")
    assert first.status_code == 201
    assert post("ready-1").json()["id"] == first.json()["id"]

    detail = api.get(
        f"/api/v1/jobs/{workspace['job']}", params={"workspace_id": workspace["workspace"]}
    ).json()
    assert [event["status"] for event in detail["application_events"]] == ["package_ready"]

    # A different intent is a different event.
    assert post("ready-2").json()["id"] != first.json()["id"]
    detail = api.get(
        f"/api/v1/jobs/{workspace['job']}", params={"workspace_id": workspace["workspace"]}
    ).json()
    assert len(detail["application_events"]) == 2


def test_a_failing_task_reports_why_and_a_retry_runs_it_again_without_duplicating_it(
    api: TestClient, durable_database_url: str
) -> None:
    """A worker failure has to reach the person who asked for the work."""
    workspace = populated_workspace(api, "worker", "Northwind Analytics")
    task_id = workspace["task"]

    database = Database(durable_database_url)
    try:
        attempts: list[str] = []

        def failing(context: object, payload: dict[str, object]) -> dict[str, object]:
            attempts.append(str(payload["artifact_id"]))
            raise RuntimeError("the artifact index disagreed with the stored bytes")

        worker = Worker(database, worker_id="worker-a", handlers={"artifact.verify": failing})
        assert worker.run_once() is True
        assert worker.run_once() is False, "one queued task is one unit of work"
    finally:
        database.dispose()

    failed = api.get(f"/api/v1/tasks/{task_id}", params={"workspace_id": workspace["workspace"]})
    assert failed.json()["status"] == TaskStatus.FAILED.value
    assert failed.json()["error_category"] == "RuntimeError"
    assert "artifact index" in failed.json()["error_message"]

    retried = api.post(
        f"/api/v1/tasks/{task_id}/retry", params={"workspace_id": workspace["workspace"]}
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == TaskStatus.QUEUED.value
    # A second attempt beside the first, not an edit of it.
    assert retried.json()["id"] != task_id
    assert (
        api.get(f"/api/v1/tasks/{task_id}", params={"workspace_id": workspace["workspace"]}).json()[
            "status"
        ]
        == TaskStatus.FAILED.value
    )

    # A double-clicked retry button queues one attempt.
    again = api.post(
        f"/api/v1/tasks/{task_id}/retry", params={"workspace_id": workspace["workspace"]}
    )
    assert again.json()["id"] == retried.json()["id"]

    listed = api.get("/api/v1/tasks", params={"workspace_id": workspace["workspace"]}).json()
    assert sorted(item["id"] for item in listed) == sorted({task_id, retried.json()["id"]})

    database = Database(durable_database_url)
    try:
        worker = Worker(
            database,
            worker_id="worker-a",
            handlers={"artifact.verify": lambda context, payload: {"verified": True}},
        )
        assert worker.run_once() is True
    finally:
        database.dispose()
    assert (
        api.get(
            f"/api/v1/tasks/{retried.json()['id']}",
            params={"workspace_id": workspace["workspace"]},
        ).json()["status"]
        == TaskStatus.COMPLETED.value
    )
    assert attempts == [f"{workspace['slug']}-artifact"]

    events = api.get(
        f"/api/v1/tasks/{task_id}/events", params={"workspace_id": workspace["workspace"]}
    )
    assert "event: task.failed" in events.text


def test_the_attachment_picker_offers_this_workspaces_live_applications(
    api: TestClient,
) -> None:
    """A note or a file is attached to an application chosen from this list."""
    first = populated_workspace(api, "picker", "Northwind Analytics")
    second = populated_workspace(api, "other", "Southgate Health")

    # An application row exists once something has happened to the posting.
    for workspace in (first, second):
        assert (
            api.post(
                f"/api/v1/jobs/{workspace['job']}/application-events",
                params={"workspace_id": workspace["workspace"]},
                json={"status": "applied", "occurred_at": "2026-01-03T12:00:00Z"},
                headers={"Idempotency-Key": f"{workspace['slug']}-applied"},
            ).status_code
            == 201
        )

    options = api.get("/api/v1/applications/options", params={"workspace_id": first["workspace"]})
    assert options.status_code == 200
    assert [item["job_posting_id"] for item in options.json()] == [first["job"]]

    # An archived job is not a thing to attach a new file to.
    assert (
        api.post(
            f"/api/v1/jobs/{first['job']}/archive",
            params={"workspace_id": first["workspace"]},
            json={"reason": "closed before application"},
        ).status_code
        == 200
    )
    assert (
        api.get("/api/v1/applications/options", params={"workspace_id": first["workspace"]}).json()
        == []
    )


def test_a_posting_written_in_html_is_read_back_without_its_markup(api: TestClient) -> None:
    """A posting body is written by a stranger, and a report shows words.

    The stripping rule has its own tests. This is the one that proves the read
    path applies it rather than handing the stored bytes straight back.
    """
    workspace_id = api.post("/api/v1/workspaces", json={"slug": "html", "name": "Html"}).json()[
        "id"
    ]
    html = "<p>Lead the platform.</p><script>alert('stolen')</script><p>Canada remote</p>"
    start = html.index("Canada remote")
    commit = evaluation_commit(workspace_id)
    posting_body = commit.posting_body.model_copy(
        update={"logical_type": "posting.body.html", "mime_type": "text/html", "content": html}
    )
    # Evidence is verified verbatim against its span, so the cited text has to
    # sit where the HTML actually puts it.
    evidence = [
        item.model_copy(update={"char_start": start, "char_end": start + len("Canada remote")})
        if item.key == "posting-location"
        else item
        for item in commit.score_evidence
    ]
    created = api.post(
        "/api/v1/evaluations",
        json=commit.model_copy(
            update={"posting_body": posting_body, "score_evidence": evidence}
        ).model_dump(mode="json"),
    )
    assert created.status_code == 201, created.text

    detail = api.get(
        f"/api/v1/jobs/{created.json()['job_posting_id']}",
        params={"workspace_id": workspace_id},
    ).json()
    observed = api.get(
        f"/api/v1/posting-observations/{detail['observations'][0]['id']}",
        params={"workspace_id": workspace_id},
    ).json()

    # The bytes are kept exactly as fetched, because they are the evidence.
    assert observed["body"] == html
    assert observed["rendered_text"] == "Lead the platform.\nCanada remote"
    assert "alert" not in observed["rendered_text"]
