"""The commands that start services and inspect workspace search state.

Only what a person would notice: the guards that refuse, and what each command
says it did. Nothing here starts a server or a polling loop.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import BinaryIO, cast

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Scope
from typer.testing import CliRunner
from uvicorn.lifespan.on import LifespanOn

from clearscout.application.documents.evidence_artifacts import EvidenceArtifactService
from clearscout.application.tasks import TaskQueueService
from clearscout.application.workspaces import SearchParametersService, WorkspaceService
from clearscout.bootstrap import local_runtime
from clearscout.bootstrap.cli import app
from clearscout.bootstrap.local_runtime import LocalApplicationState
from clearscout.bootstrap.worker import Worker
from clearscout.domain import new_id, utc_now
from clearscout.domain.search_parameters import JobSearchParameters, Location
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Artifact, PendingDiscovery, PostingRefusal
from clearscout.shared.errors import ConfigError
from tests.support.artifacts import runtime_storage, strand_promotion
from tests.support.http import recorded_web_clients


@pytest.mark.parametrize(
    ("returncode", "observed_start", "expected"),
    [(0, "original", True), (0, "different", False), (1, "", False), (2, "", None), (0, "", None)],
)
def test_source_reader_recovery_distinguishes_live_reused_and_unknown_processes(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    observed_start: str,
    expected: bool | None,
) -> None:
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=observed_start
        ),
    )
    assert local_runtime.reader_process_alive(41, "original") is expected


def test_source_reader_recovery_keeps_ownership_when_process_inspection_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("process inspection unavailable")

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.subprocess.run", denied)
    assert local_runtime.reader_process_alive(41, "original") is None


runner = CliRunner()


@contextmanager
def _restored_application_logging() -> Iterator[logging.Logger]:
    """Undo what the worker command does to the process-global `clearscout` logger.

    An in-process CLI run reaches `configure_application_logging`, which owns the
    namespace's level, propagation, and handlers for the rest of the process.
    """
    application = logging.getLogger("clearscout")
    original_handlers = list(application.handlers)
    original_level = application.level
    original_propagate = application.propagate
    try:
        yield application
    finally:
        application.handlers[:] = original_handlers
        application.setLevel(original_level)
        application.propagate = original_propagate


@pytest.fixture
def isolated_application_logging() -> Iterator[None]:
    with _restored_application_logging():
        yield


def test_serving_on_a_public_interface_is_refused(tmp_path: Path) -> None:
    """The server is local. A wrong --host would publish a private ledger."""
    result = runner.invoke(
        app,
        # S104: binding to every interface is the thing being refused here.
        ["serve", "--host", "0.0.0.0", "--database-url", f"sqlite+pysqlite:///{tmp_path / 'x'}"],  # noqa: S104
    )
    assert result.exit_code != 0
    assert "loopback" in result.output


@pytest.mark.usefixtures("isolated_application_logging")
def test_serving_stops_an_open_task_stream_without_cancelling_the_queued_task(
    durable_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    workspace = WorkspaceService(durable_database, storage).create(
        slug="stream", name="Stream", root_path=str(tmp_path / "workspace")
    )
    workspace_id = str(workspace.id)
    queue = TaskQueueService(durable_database)
    task = queue.enqueue(
        workspace_id=workspace_id,
        task_type="artifact.verify",
        payload={"artifact_id": "one"},
        idempotency_key="verify-one",
    )
    task_id = str(task.id)
    original_task = queue.get(task_id, workspace_id)
    original_events = queue.events_after(task_id, 0, workspace_id=workspace_id, limit=100)
    captured: list[uvicorn.Config] = []

    def capture_server(
        application: FastAPI,
        *,
        host: str,
        port: int,
        log_level: str,
        log_config: None,
        timeout_graceful_shutdown: int | None = None,
    ) -> None:
        captured.append(
            uvicorn.Config(
                application,
                host=host,
                port=port,
                log_level=log_level,
                log_config=log_config,
                timeout_graceful_shutdown=timeout_graceful_shutdown,
            )
        )

    monkeypatch.setattr(uvicorn, "run", capture_server)
    result = runner.invoke(app, ["serve", "--database-url", durable_database.url])
    assert result.exit_code == 0, result.output
    assert len(captured) == 1

    async def exercise_shutdown() -> None:
        config = captured[0]
        server = uvicorn.Server(config)
        lifecycle = LifespanOn(config)
        server.lifespan = lifecycle
        stream_waiting = asyncio.Event()
        release_stream = asyncio.Event()
        disconnected = asyncio.Event()
        messages: list[Message] = []

        class Listener:
            closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                assert self.closed

        listener = Listener()
        server.servers = [cast(asyncio.Server, listener)]

        async def controlled_sleep(seconds: float) -> None:
            if seconds == 0.5:
                stream_waiting.set()
                await release_stream.wait()
            else:
                assert seconds == 0.1

        async def receive() -> Message:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/v1/tasks/{task_id}/events",
            "raw_path": f"/api/v1/tasks/{task_id}/events".encode(),
            "query_string": f"workspace_id={workspace_id}".encode(),
            "root_path": "",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", config.port),
        }
        monkeypatch.setattr("uvicorn.server.asyncio.sleep", controlled_sleep)
        await lifecycle.startup()
        assert not lifecycle.startup_failed

        async def stream_request() -> None:
            await cast(ASGIApp, config.loaded_app)(scope, receive, send)

        stream_task = asyncio.create_task(stream_request())
        server.server_state.tasks.add(stream_task)
        stream_task.add_done_callback(server.server_state.tasks.discard)
        ready = asyncio.create_task(stream_waiting.wait())

        def expire_graceful_wait(
            pending: Coroutine[object, object, None], *, timeout: float | None
        ) -> Coroutine[object, object, None]:
            pending.close()
            assert listener.closed
            assert not stream_task.done()
            assert timeout is not None, "An open task stream has no API shutdown deadline."
            assert 0 < timeout < local_runtime.STOP_WAIT_SECONDS

            async def expired() -> None:
                raise TimeoutError

            return expired()

        try:
            finished, _ = await asyncio.wait(
                {stream_task, ready}, return_when=asyncio.FIRST_COMPLETED
            )
            if stream_task in finished:
                await stream_task
                pytest.fail("The queued task stream ended before shutdown.")
            assert any(
                message["type"] == "http.response.start" and message["status"] == 200
                for message in messages
            )
            assert any(b"event: task.queued" in message.get("body", b"") for message in messages)
            monkeypatch.setattr("uvicorn.server.asyncio.wait_for", expire_graceful_wait)

            await server.shutdown()

            with pytest.raises(asyncio.CancelledError):
                await stream_task
            assert listener.closed
            assert stream_task.cancelled()
            assert lifecycle.shutdown_event.is_set()
        finally:
            stream_task.cancel()
            ready.cancel()
            await asyncio.gather(stream_task, ready, return_exceptions=True)
            if not lifecycle.shutdown_event.is_set():
                await lifecycle.shutdown()

    asyncio.run(exercise_shutdown())

    assert queue.get(task_id, workspace_id) == original_task
    assert queue.get(task_id, workspace_id).status == "queued"
    assert queue.events_after(task_id, 0, workspace_id=workspace_id, limit=100) == original_events


def test_a_worker_refuses_a_database_that_was_never_migrated(tmp_path: Path) -> None:
    """Starting against an empty file would fail later and less clearly."""
    result = runner.invoke(
        app,
        ["worker", "--once", "--database-url", f"sqlite+pysqlite:///{tmp_path / 'absent.sqlite3'}"],
    )
    assert result.exit_code != 0
    assert "db upgrade" in result.output


def test_start_refuses_a_database_that_was_never_migrated(tmp_path: Path) -> None:
    """The combined launcher retains the explicit-migration boundary."""
    result = runner.invoke(app, ["start", "--data-root", str(tmp_path / "absent")])

    assert result.exit_code != 0
    assert "db upgrade" in result.output


def test_runtime_children_detach_and_write_private_logs(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful start must survive the terminal that launched it."""
    storage = runtime_storage(durable_database, tmp_path / "artifacts")
    storage.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 42

    def popen(arguments: list[str], **options: object) -> Process:
        calls.append((arguments, options))
        return Process()

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.subprocess.Popen", popen)

    process = local_runtime._start_child("serve", storage, 8899)

    assert process.pid == 42
    assert calls[0][0][3] == "serve"
    options = calls[0][1]
    assert options["stdin"] is subprocess.DEVNULL
    assert options["stderr"] is subprocess.STDOUT
    assert options["start_new_session"] is True
    log_file = cast(BinaryIO, options["stdout"])
    assert log_file.name == str(storage.data_root / ".clearscout-runtime" / "clearscout-serve.log")
    assert log_file.closed is True


@pytest.mark.parametrize("explicit", [True, False])
def test_runtime_children_carry_only_an_explicit_workspaces_root(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explicit: bool,
) -> None:
    workspaces = tmp_path / "elsewhere"
    storage = RuntimeStorage.resolve(
        durable_database.url, workspaces_root=workspaces if explicit else None
    )
    storage.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    calls: list[list[str]] = []

    class Process:
        pid = 42

    def popen(arguments: list[str], **options: object) -> Process:
        calls.append(arguments)
        return Process()

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.subprocess.Popen", popen)

    local_runtime._start_child("worker", storage, 8899)

    if explicit:
        flag = calls[0].index("--workspaces-root")
        assert calls[0][flag + 1] == str(workspaces.resolve())
    else:
        assert "--workspaces-root" not in calls[0]


def test_runtime_child_hands_its_workspaces_root_to_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    received: list[Path | None] = []

    def serve(*arguments: object, workspaces_root: Path | None = None) -> None:
        received.append(workspaces_root)

    monkeypatch.setattr("clearscout.delivery.cli.runtime.serve", serve)
    monkeypatch.setattr(
        "sys.argv",
        [
            "local_runtime",
            "serve",
            "--database-url",
            "sqlite+pysqlite:///unused.sqlite3",
            "--data-root",
            str(tmp_path),
            "--artifact-root",
            str(tmp_path / "artifacts"),
            "--workspaces-root",
            str(tmp_path / "elsewhere"),
            "--port",
            "8899",
        ],
    )

    local_runtime.main()

    assert received == [tmp_path / "elsewhere"]


def test_runtime_status_preserves_owned_processes_without_a_healthy_server(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale child processes must not make an unreachable app look ready."""
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state_file.write_text('{"server_pid": 41, "worker_pid": 42, "port": 8899}')
    monkeypatch.setattr(local_runtime, "_is_owned", lambda *args: True)
    monkeypatch.setattr(local_runtime, "_is_healthy", lambda port: False)

    with pytest.raises(ConfigError, match="health check failed"):
        local_runtime.local_application_status(durable_database.url)
    assert state_file.exists(), "a transient health failure discarded the process ownership record"


def test_runtime_status_removes_only_definitively_stale_state(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two absent recorded children are stale ownership, not an uncertain runtime."""
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state_file.write_text('{"server_pid": 41, "worker_pid": 42, "port": 8899}')
    monkeypatch.setattr(local_runtime, "_is_owned", lambda *args: False)
    monkeypatch.setattr(
        local_runtime,
        "_is_healthy",
        lambda port: pytest.fail("stale processes do not need an HTTP probe"),
    )

    assert local_runtime.local_application_status(durable_database.url) is None
    assert not state_file.exists()


@pytest.mark.parametrize("surviving_role", ["serve", "worker"])
def test_partial_runtime_is_preserved_and_cannot_spawn_replacements(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    surviving_role: str,
) -> None:
    """One survivor remains stoppable instead of being orphaned by a replacement pair."""
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state_file.write_text('{"server_pid": 41, "worker_pid": 42, "port": 8899}')
    started: list[str] = []

    def owned(_pid: int, role: str) -> bool:
        return role == surviving_role

    monkeypatch.setattr(local_runtime, "_is_owned", owned)
    monkeypatch.setattr(
        local_runtime,
        "_start_child",
        lambda role, _storage, _port: started.append(role),
    )

    with pytest.raises(ConfigError, match="partially running"):
        local_runtime.local_application_status(durable_database.url)
    with pytest.raises(ConfigError, match="partially running"):
        local_runtime.start_local_application(durable_database.url)

    assert state_file.exists()
    assert started == []


def test_start_promotes_a_stranded_artifact_before_any_child_runs(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A row a stopped process left staged is verified before a server can read it."""
    root = tmp_path / "artifacts"
    storage = runtime_storage(durable_database, root)
    workspace = WorkspaceService(durable_database, storage).create(slug="stranded", name="Stranded")
    with durable_database.session() as session:
        artifact_id = (
            EvidenceArtifactService(storage)
            .store(
                session,
                workspace_id=str(workspace.id),
                logical_type="posting.body.text",
                content=b"Operations Lead Ship.",
                relative_path="stranded/body.txt",
                mime_type="text/plain",
                related_entity_type="test",
                related_entity_id="synthetic",
                provenance={"source": "test"},
                audit={"created_by": "test", "origin": "fault_injection"},
            )
            .id
        )
    strand_promotion(durable_database, root, artifact_id)
    observed: list[tuple[str, str | None]] = []

    class Process:
        pid = 42

    def start_child(role: str, _storage: object, _port: int) -> Process:
        with durable_database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            observed.append((role, artifact.integrity_status if artifact else None))
        return Process()

    monkeypatch.setattr(local_runtime, "_is_owned", lambda *args: False)
    monkeypatch.setattr(local_runtime, "_start_child", start_child)

    with pytest.raises(ConfigError, match="stopped during startup"):
        local_runtime.start_local_application(durable_database.url, artifact_root=root)

    assert observed[0] == ("serve", "verified")


def test_runtime_process_observation_failure_preserves_owned_state(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked process probe must not orphan children that start can no longer stop."""
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state_file.write_text('{"server_pid": 41, "worker_pid": 42, "port": 8899}')

    def unavailable(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise PermissionError("process inspection unavailable")

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.subprocess.run", unavailable)

    with pytest.raises(
        ConfigError,
        match="could not inspect its managed processes",
    ):
        local_runtime.local_application_status(durable_database.url)
    assert state_file.exists(), "an unavailable ownership check orphaned the managed children"

    with pytest.raises(ConfigError, match="could not inspect its managed processes"):
        local_runtime.stop_local_application(durable_database.url)
    assert state_file.exists(), "stop discarded ownership state without inspecting its children"

    with pytest.raises(ConfigError, match="could not inspect its managed processes"):
        local_runtime.start_local_application(durable_database.url)
    assert state_file.exists(), "start discarded ownership state without inspecting its children"


def test_stop_keeps_ownership_until_both_children_exit(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state = LocalApplicationState(41, 42, 8899)
    local_runtime._write_state(state_file, state)
    alive = {"serve": True, "worker": True}
    stopping: set[str] = set()
    observed: list[str] = []

    def signal_child(pid: int, _signal: int) -> None:
        stopping.add("serve" if pid == state.server_pid else "worker")

    def pause(_seconds: float) -> None:
        assert state_file.exists()
        if alive["worker"]:
            assert local_runtime.local_application_status(durable_database.url) == state
            assert stopping == {"worker"}
            observed.append("worker")
            alive["worker"] = False
        else:
            assert stopping == {"serve", "worker"}
            observed.append("serve")
            alive["serve"] = False

    monkeypatch.setattr(local_runtime, "_is_owned", lambda _pid, role: alive[role])
    monkeypatch.setattr(local_runtime, "_is_healthy", lambda _port: "serve" not in stopping)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.os.kill", signal_child)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.sleep", pause)

    assert local_runtime.stop_local_application(durable_database.url)
    assert observed == ["worker", "serve"]
    assert not state_file.exists()


def test_stop_timeout_keeps_process_ownership_for_a_later_retry(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state = LocalApplicationState(41, 42, 8899)
    local_runtime._write_state(state_file, state)
    original_state = state_file.read_text()
    alive = {"serve": True, "worker": True}
    signaled: list[int] = []
    started: list[str] = []
    ticks = iter([0.0, local_runtime.STOP_WAIT_SECONDS] * 2)

    def signal_child(pid: int, _signal: int) -> None:
        signaled.append(pid)
        if pid == state.server_pid:
            alive["serve"] = False

    monkeypatch.setattr(local_runtime, "_is_owned", lambda _pid, role: alive[role])
    monkeypatch.setattr(local_runtime, "_is_healthy", lambda _port: alive["serve"])
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.os.kill", signal_child)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        local_runtime, "_start_child", lambda role, _storage, _port: started.append(role)
    )

    with pytest.raises(ConfigError, match="still stopping"):
        local_runtime.stop_local_application(durable_database.url)

    assert state_file.read_text() == original_state
    assert signaled == [state.worker_pid, state.worker_pid]
    assert local_runtime.local_application_status(durable_database.url) == state
    with pytest.raises(ConfigError, match="already running"):
        local_runtime.start_local_application(durable_database.url)
    assert started == []

    alive["worker"] = False
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: 0.0)

    assert local_runtime.stop_local_application(durable_database.url)
    assert signaled == [state.worker_pid, state.worker_pid, state.server_pid]
    assert not state_file.exists()
    assert local_runtime.local_application_status(durable_database.url) is None


def test_stop_kills_a_worker_still_running_a_task_after_the_wait(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state = LocalApplicationState(41, 42, 8899)
    local_runtime._write_state(state_file, state)
    alive = {"serve": True, "worker": True}
    signaled: list[tuple[str, int]] = []
    ticks = iter([0.0, local_runtime.STOP_WAIT_SECONDS, 0.0, 0.0])

    def signal_child(pid: int, signum: int) -> None:
        role = "serve" if pid == state.server_pid else "worker"
        signaled.append((role, signum))
        # The busy worker ignores SIGTERM; only SIGKILL ends it.
        if role == "serve" or signum == signal.SIGKILL:
            alive[role] = False

    monkeypatch.setattr(local_runtime, "_is_owned", lambda _pid, role: alive[role])
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.os.kill", signal_child)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.sleep", lambda _seconds: None)

    assert local_runtime.stop_local_application(durable_database.url)
    assert signaled == [
        ("worker", signal.SIGTERM),
        ("worker", signal.SIGKILL),
        ("serve", signal.SIGTERM),
    ]
    assert not state_file.exists()


def test_stop_never_kills_a_worker_running_a_paid_task(
    durable_database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    workspace = WorkspaceService(durable_database, storage).create(
        slug="paid", name="Paid", root_path=str(tmp_path / "workspace")
    )
    queue = TaskQueueService(durable_database)
    queue.enqueue(
        workspace_id=str(workspace.id),
        task_type="posting.refresh",
        payload={},
        idempotency_key="paid-refresh",
    )
    assert queue.claim_next(worker_id=local_runtime.LOCAL_WORKER_ID, lease_seconds=60)
    state_file = local_runtime._state_file(storage)
    state = LocalApplicationState(41, 42, 8899)
    local_runtime._write_state(state_file, state)
    signaled: list[tuple[int, int]] = []
    ticks = iter([0.0, local_runtime.STOP_WAIT_SECONDS])

    monkeypatch.setattr(local_runtime, "_is_owned", lambda _pid, _role: True)
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.os.kill",
        lambda pid, signum: signaled.append((pid, signum)),
    )
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.sleep", lambda _seconds: None)

    with pytest.raises(ConfigError, match="still finishing posting.refresh"):
        local_runtime.stop_local_application(durable_database.url)

    assert signaled == [(state.worker_pid, signal.SIGTERM)]
    assert state_file.exists()


def test_stop_api_timeout_names_the_api_and_preserves_ownership(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    local_runtime._write_state(state_file, LocalApplicationState(41, 42, 8899))
    ticks = iter([0.0, 0.0, local_runtime.STOP_WAIT_SECONDS])
    monkeypatch.setattr(local_runtime, "_is_owned", lambda _pid, role: role == "serve")
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.os.kill", lambda _pid, _signal: None)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.sleep", lambda _seconds: None)

    with pytest.raises(ConfigError, match="local API is still stopping") as error:
        local_runtime.stop_local_application(durable_database.url)

    assert "worker" not in str(error.value)
    assert state_file.exists()


@pytest.mark.parametrize("failure", ["worker_signal", "worker_probe", "api_signal", "api_probe"])
def test_stop_failure_preserves_ownership_and_the_api(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    state = LocalApplicationState(41, 42, 8899)
    local_runtime._write_state(state_file, state)
    original_state = state_file.read_text()
    alive = {"serve": True, "worker": True}
    worker_signaled = False

    def owned(_pid: int, role: str) -> bool:
        if worker_signaled and failure == ("worker_probe" if role == "worker" else "api_probe"):
            raise ConfigError("process inspection unavailable")
        return alive[role]

    def signal_child(pid: int, _signal: int) -> None:
        nonlocal worker_signaled
        if failure == ("worker_signal" if pid == state.worker_pid else "api_signal"):
            raise PermissionError("process signal unavailable")
        if pid == state.worker_pid:
            worker_signaled = True
            alive["worker"] = False
        else:
            alive["serve"] = False

    monkeypatch.setattr(local_runtime, "_is_owned", owned)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.os.kill", signal_child)
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: 0.0)

    error = PermissionError if failure.endswith("signal") else ConfigError
    with pytest.raises(error, match="unavailable"):
        local_runtime.stop_local_application(durable_database.url)

    assert state_file.read_text() == original_state
    assert alive["serve"]


@pytest.mark.parametrize(
    ("returncode", "command"),
    [(1, ""), (0, "some_other_application serve worker")],
)
def test_stop_removes_stale_state_without_signaling_unowned_processes(
    durable_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    command: str,
) -> None:
    storage = runtime_storage(durable_database)
    state_file = local_runtime._state_file(storage)
    local_runtime._write_state(state_file, LocalApplicationState(41, 42, 8899))
    signaled: list[int] = []
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=command
        ),
    )
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.os.kill",
        lambda pid, _signal: signaled.append(pid),
    )
    monkeypatch.setattr("clearscout.bootstrap.local_runtime.time.monotonic", lambda: 0.0)

    assert local_runtime.stop_local_application(durable_database.url)
    assert signaled == []
    assert not state_file.exists()


def test_runtime_process_probe_refuses_an_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed process tool is unknown ownership, not proof that a PID is stale."""
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=2, stdout=""),
    )

    with pytest.raises(ConfigError, match="could not inspect its managed processes"):
        local_runtime._is_owned(41, "serve")


def test_start_stop_and_status_delegate_to_the_owned_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public commands report only the bootstrap runtime's process state."""
    state = LocalApplicationState(server_pid=11, worker_pid=12, port=8899)
    started: list[tuple[object, object, object, object]] = []

    def start(*args: object, **kwargs: object) -> tuple[LocalApplicationState, dict[str, int]]:
        started.append(
            (args, kwargs.get("data_root"), kwargs.get("artifact_root"), kwargs.get("port"))
        )
        return state, {"finalized": 0, "unresolved": 0, "outside_root": 0, "retrying": 0}

    monkeypatch.setattr("clearscout.bootstrap.local_runtime.start_local_application", start)
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.local_application_status", lambda *args, **kwargs: state
    )
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.stop_local_application", lambda *args, **kwargs: True
    )

    data_root = tmp_path / "data"
    started_result = runner.invoke(app, ["start", "--data-root", str(data_root), "--port", "8899"])
    status_result = runner.invoke(app, ["status", "--data-root", str(data_root)])
    stopped_result = runner.invoke(app, ["stop", "--data-root", str(data_root)])

    assert started_result.exit_code == 0, started_result.output
    assert "http://127.0.0.1:8899" in started_result.output
    assert started == [((None,), data_root, None, 8899)]
    assert status_result.exit_code == 0, status_result.output
    assert "http://127.0.0.1:8899" in status_result.output
    assert stopped_result.exit_code == 0, stopped_result.output
    assert "ClearScout stopped." in stopped_result.output


def test_start_names_the_staged_artifacts_it_left_unsettled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rows reconcile could not recover, could not reach, or left to another root are reported."""
    state = LocalApplicationState(server_pid=11, worker_pid=12, port=8899)
    monkeypatch.setattr(
        "clearscout.bootstrap.local_runtime.start_local_application",
        lambda *args, **kwargs: (
            state,
            {"finalized": 0, "unresolved": 2, "outside_root": 1, "retrying": 3},
        ),
    )

    result = runner.invoke(app, ["start", "--data-root", str(tmp_path / "data")])

    assert result.exit_code == 0, result.output
    assert "2 staged artifact(s) could not be recovered" in result.output
    assert "1 staged artifact(s) are outside this artifact root" in result.output
    assert "3 staged artifact(s) could not be read or written" in result.output


def test_stop_names_the_data_root_it_searched_when_it_finds_nothing(tmp_path: Path) -> None:
    """A stack started against another data root is invisible here, so the root is the answer."""
    searched = tmp_path / "elsewhere"

    result = runner.invoke(app, ["stop", "--data-root", str(searched)])

    assert result.exit_code == 0, result.output
    assert str(searched) in result.output


def test_status_names_the_data_root_it_searched_when_it_finds_nothing(tmp_path: Path) -> None:
    """Status misleads the same way stop did unless it says where it looked."""
    searched = tmp_path / "elsewhere"

    result = runner.invoke(app, ["status", "--data-root", str(searched)])

    assert result.exit_code == 1, result.output
    assert str(searched) in result.output


@pytest.mark.usefixtures("isolated_application_logging")
def test_a_worker_run_once_reports_whether_it_found_anything(
    durable_database_url: str, tmp_path: Path
) -> None:
    idle = runner.invoke(app, ["worker", "--once", "--database-url", durable_database_url])
    assert idle.exit_code == 0, idle.output
    assert "No queued task" in idle.output

    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="worker", name="Worker", root_path=str(tmp_path / "workspace")
        )
        TaskQueueService(database).enqueue(
            workspace_id=str(workspace.id),
            task_type="artifact.verify",
            payload={"artifact_id": "missing"},
            idempotency_key="verify-missing",
        )
    finally:
        database.dispose()

    worked = runner.invoke(app, ["worker", "--once", "--database-url", durable_database_url])
    assert worked.exit_code == 0, worked.output
    assert "Processed one task." in worked.output


@pytest.mark.usefixtures("isolated_application_logging")
def test_a_second_worker_is_told_who_is_already_serving(durable_database_url: str) -> None:
    """The refusal is a message and an exit code, not a traceback from the queue."""
    database = Database(durable_database_url)
    try:
        TaskQueueService(database).renew_worker_presence(
            worker_id="worker-elsewhere", ttl_seconds=3600
        )
    finally:
        database.dispose()

    result = runner.invoke(app, ["worker", "--once", "--database-url", durable_database_url])

    assert result.exit_code != 0
    assert "worker-elsewhere" in result.output
    assert "one worker per database" in result.output


@pytest.mark.usefixtures("isolated_application_logging")
def test_stopping_a_polling_worker_says_the_lease_survives(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C is how the worker is stopped, so it is an outcome rather than a crash."""

    def interrupted(self: object, *, poll_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("clearscout.bootstrap.worker.Worker.run_forever", interrupted)

    result = runner.invoke(app, ["worker", "--database-url", durable_database_url])

    assert result.exit_code == 0, result.output
    assert "lease can be recovered" in result.output


@pytest.mark.usefixtures("isolated_application_logging")
def test_sigterm_requests_a_worker_stop_without_interrupting_its_task(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = signal.getsignal(signal.SIGTERM)
    observed: list[bool] = []

    def stop(self: Worker, *, poll_seconds: float) -> None:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        installed(signal.SIGTERM, None)
        observed.append(self._stop_requested.is_set())

    monkeypatch.setattr(Worker, "run_forever", stop)

    result = runner.invoke(app, ["worker", "--database-url", durable_database_url])

    assert result.exit_code == 0, result.output
    assert observed == [True]
    assert signal.getsignal(signal.SIGTERM) == previous


@pytest.mark.usefixtures("isolated_application_logging")
@pytest.mark.parametrize(
    ("environment", "arguments", "expected"),
    [("", [], 4), ("6", [], 6), ("6", ["--continuation-slots", "2"], 2)],
    ids=["default", "environment", "flag-over-environment"],
)
def test_the_worker_reads_its_continuation_slots_from_the_flag_or_environment(
    durable_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    arguments: list[str],
    expected: int,
) -> None:
    started: list[int] = []

    def interrupted(self: Worker, *, poll_seconds: float) -> None:
        started.append(self.continuation_slots)
        raise KeyboardInterrupt

    monkeypatch.setattr("clearscout.bootstrap.worker.Worker.run_forever", interrupted)
    monkeypatch.setenv("CLEARSCOUT_CONTINUATION_SLOTS", environment)

    result = runner.invoke(app, ["worker", "--database-url", durable_database_url, *arguments])

    assert result.exit_code == 0, result.output
    assert started == [expected]


@pytest.mark.parametrize(
    ("environment", "arguments", "named"),
    [
        ("0", [], "CLEARSCOUT_CONTINUATION_SLOTS"),
        ("four", [], "CLEARSCOUT_CONTINUATION_SLOTS"),
        ("", ["--continuation-slots", "9"], "--continuation-slots"),
    ],
    ids=["zero", "not-a-number", "flag-past-the-cap"],
)
def test_the_worker_refuses_an_unusable_continuation_slot_setting(
    durable_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    arguments: list[str],
    named: str,
) -> None:
    monkeypatch.setenv("CLEARSCOUT_CONTINUATION_SLOTS", environment)

    result = runner.invoke(
        app, ["worker", "--once", "--database-url", durable_database_url, *arguments]
    )

    assert result.exit_code != 0
    assert f"{named} must be a whole number from 1 to 8" in result.output


def test_an_in_process_cli_run_leaves_the_application_logger_as_it_found_it(
    durable_database_url: str,
) -> None:
    """A leaked handler or level silences a later test's log assertions."""
    with _restored_application_logging() as application:
        result = runner.invoke(app, ["worker", "--once", "--database-url", durable_database_url])

        assert result.exit_code == 0, result.output
        assert application.handlers, "the worker command no longer configures application logging"

    assert application.level == logging.NOTSET
    assert application.propagate is True
    assert application.handlers == []


def test_search_show_reads_workspace_parameters_pending_and_refused_postings(
    durable_database_url: str,
) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="search", name="Search"
        )
        workspace_id = str(workspace.id)
        parameters = SearchParametersService(database).save(
            workspace_id,
            JobSearchParameters(locations=[Location(key="home", country="Atlantis")]),
        )
        with database.session() as session:
            session.add(
                PendingDiscovery(
                    id=new_id(),
                    workspace_id=workspace_id,
                    fingerprint=new_id(),
                    employer="Example",
                    title="Platform Engineer",
                    location="Remote",
                    requisition_id="REQ-1",
                    discovery_url="https://example.test/jobs/1",
                    canonical_url="https://example.test/jobs/1",
                    reason="The board matched the workspace search.",
                    first_seen_on=date(2026, 8, 13),
                )
            )
            session.add(
                PostingRefusal(
                    id=new_id(),
                    workspace_id=workspace_id,
                    employer="Example",
                    title="Restricted role",
                    condition="work_authorization",
                    reason="The workspace does not state this authorization.",
                    refused_at=utc_now(),
                    search_parameter_set_id=parameters.id,
                )
            )
    finally:
        database.dispose()

    result = runner.invoke(
        app,
        [
            "search",
            "show",
            "--workspace-id",
            workspace_id,
            "--database-url",
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Atlantis" in result.output
    assert "Pending discoveries (1)" in result.output
    assert "Platform Engineer at Example" in result.output
    assert "Current search parameters: version 1" in result.output
    assert "Refusal history (1)" in result.output
    assert "Restricted role at Example" in result.output
    assert "Search settings v1" in result.output


def test_search_show_says_when_the_workspace_has_no_search_parameters(
    durable_database_url: str,
) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="empty-search", name="Empty search"
        )
        workspace_id = str(workspace.id)
    finally:
        database.dispose()

    result = runner.invoke(
        app,
        [
            "search",
            "show",
            "--workspace-id",
            workspace_id,
            "--database-url",
            durable_database_url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No job-search parameters are saved in this workspace." in result.output


def test_search_show_closes_every_web_client_it_builds(
    durable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(durable_database_url)
    try:
        workspace = WorkspaceService(database, runtime_storage(database)).create(
            slug="closed-clients", name="Closed clients"
        )
        workspace_id = str(workspace.id)
    finally:
        database.dispose()
    built, closed = recorded_web_clients(monkeypatch)

    result = runner.invoke(
        app,
        ["search", "show", "--workspace-id", workspace_id, "--database-url", durable_database_url],
    )

    assert result.exit_code == 0, result.output
    assert built
    assert {id(client) for client in built} <= {id(client) for client in closed}
