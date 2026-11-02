"""Own the API and worker processes for one local TailorCV application."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import urlopen

from sqlalchemy import select

from tailorcv.application.documents.evidence_artifacts import EvidenceArtifactService
from tailorcv.domain import TaskStatus
from tailorcv.infrastructure.artifacts.runtime import RuntimeStorage, WorkspaceRootPolicy
from tailorcv.infrastructure.persistence.database import Database
from tailorcv.infrastructure.persistence.migration import status
from tailorcv.infrastructure.persistence.models import BackgroundTask
from tailorcv.shared.errors import ConfigError

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
RUNTIME_DIRECTORY = ".tailorcv-runtime"
STATE_FILENAME = ".tailorcv-runtime.json"
LOG_FILENAME_TEMPLATE = "tailorcv-{role}.log"
STOP_WAIT_SECONDS = 60.0
LOCAL_WORKER_ID = "local-worker-1"


@dataclass(frozen=True)
class LocalApplicationState:
    """Recorded child processes owned by one resolved local storage boundary."""

    server_pid: int
    worker_pid: int
    port: int


def start_local_application(
    database_url: str | None = None,
    *,
    data_root: Path | None = None,
    artifact_root: Path | None = None,
    workspaces_root: Path | None = None,
    port: int = DEFAULT_PORT,
) -> tuple[LocalApplicationState, dict[str, int]]:
    """Start one API and one worker after an explicit schema and ownership check.

    Also returns the staged-artifact counts the pre-start reconcile left for the caller.
    """
    storage = RuntimeStorage.resolve(
        database_url,
        data_root=data_root,
        artifact_root=artifact_root,
        workspaces_root=workspaces_root,
    )
    _require_current_schema(storage)
    state_file = _state_file(storage)
    current = _read_state(state_file)
    if current is not None:
        server_owned, worker_owned = _owned_processes(current)
        if server_owned and worker_owned:
            raise ConfigError(f"TailorCV is already running at http://{HOST}:{current.port}.")
        if server_owned or worker_owned:
            raise ConfigError(
                "TailorCV is partially running; run 'tailorcv stop' before starting it again."
            )
    state_file.unlink(missing_ok=True)
    reconciled = _reconcile_staged_artifacts(storage)
    server = _start_child("serve", storage, port)
    worker = _start_child("worker", storage, port)
    state = LocalApplicationState(server.pid, worker.pid, port)
    _write_state(state_file, state)
    try:
        _wait_until_ready(state)
    except Exception:
        _stop_if_owned(state.server_pid, "serve")
        _stop_if_owned(state.worker_pid, "worker")
        state_file.unlink(missing_ok=True)
        raise
    return state, reconciled


def stop_local_application(
    database_url: str | None = None,
    *,
    data_root: Path | None = None,
    artifact_root: Path | None = None,
) -> bool:
    """Stop only processes that this TailorCV runtime instance created."""
    storage = RuntimeStorage.resolve(database_url, data_root=data_root, artifact_root=artifact_root)
    state_file = _state_file(storage)
    state = _read_state(state_file)
    if state is None:
        return False
    _, worker_owned = _owned_processes(state)
    if worker_owned:
        os.kill(state.worker_pid, signal.SIGTERM)
    try:
        _wait_until_stopped(state, worker_only=True)
    except ConfigError:
        _kill_worker_if_tasks_recover(state, storage)
    _stop_if_owned(state.server_pid, "serve")
    _wait_until_stopped(state)
    state_file.unlink(missing_ok=True)
    return True


def local_application_status(
    database_url: str | None = None,
    *,
    data_root: Path | None = None,
    artifact_root: Path | None = None,
) -> LocalApplicationState | None:
    """Return the owned running application state, if both processes are alive."""
    storage = RuntimeStorage.resolve(database_url, data_root=data_root, artifact_root=artifact_root)
    state_file = _state_file(storage)
    state = _read_state(state_file)
    if state is None:
        return None
    server_owned, worker_owned = _owned_processes(state)
    if not server_owned and not worker_owned:
        state_file.unlink(missing_ok=True)
        return None
    if not server_owned or not worker_owned:
        raise ConfigError("TailorCV is partially running; run 'tailorcv stop' to clean it up.")
    if _is_healthy(state.port):
        return state
    raise ConfigError(
        "TailorCV processes are running, but the local API health check failed; retry or stop it."
    )


def _require_current_schema(storage: RuntimeStorage) -> None:
    schema = status(storage.database_url)
    if not schema.current:
        raise ConfigError("Run 'tailorcv db upgrade' before starting the local application.")


def _kill_worker_if_tasks_recover(state: LocalApplicationState, storage: RuntimeStorage) -> None:
    """Kill a worker that outlasted the stop wait, unless a running task would lose its work.

    A killed task keeps its lease, and the next worker reclaims it once that expires.
    """
    from tailorcv.bootstrap.worker import KILL_SAFE_TASK_TYPES

    unsafe = sorted(set(_running_task_types(storage)) - KILL_SAFE_TASK_TYPES)
    if unsafe:
        raise ConfigError(
            f"TailorCV worker is still finishing {', '.join(unsafe)}. "
            "Retry 'tailorcv stop' after the task settles."
        )
    _stop_if_owned(state.worker_pid, "worker", getattr(signal, "SIGKILL", signal.SIGTERM))
    _wait_until_stopped(state, worker_only=True)


def _running_task_types(storage: RuntimeStorage) -> list[str]:
    database = Database(storage.database_url)
    try:
        with database.session() as session:
            return list(
                session.scalars(
                    select(BackgroundTask.task_type).where(
                        BackgroundTask.status == TaskStatus.RUNNING,
                        BackgroundTask.leased_by == LOCAL_WORKER_ID,
                    )
                )
            )
    finally:
        database.dispose()


def _reconcile_staged_artifacts(storage: RuntimeStorage) -> dict[str, int]:
    """Promote bytes a stopped process staged but never promoted, before any child runs."""
    database = Database(storage.database_url)
    try:
        return EvidenceArtifactService(storage).reconcile(database)
    finally:
        database.dispose()


def _state_file(storage: RuntimeStorage) -> Path:
    return _runtime_directory(storage) / STATE_FILENAME


def _runtime_directory(storage: RuntimeStorage) -> Path:
    directory = storage.data_root / RUNTIME_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _start_child(role: str, storage: RuntimeStorage, port: int) -> subprocess.Popen[bytes]:
    arguments = [
        sys.executable,
        "-m",
        "tailorcv.bootstrap.local_runtime",
        role,
        "--database-url",
        storage.database_url,
        "--data-root",
        str(storage.data_root),
        "--artifact-root",
        str(storage.artifact_root),
        "--port",
        str(port),
    ]
    if storage.workspace_policy is WorkspaceRootPolicy.EXPLICIT:
        arguments += ["--workspaces-root", str(storage.workspaces_root)]
    log_path = _runtime_directory(storage) / LOG_FILENAME_TEMPLATE.format(role=role)
    with log_path.open("ab") as log_file:
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            return subprocess.Popen(  # noqa: S603 - fixed module and validated local arguments
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        return subprocess.Popen(  # noqa: S603 - fixed module and validated local arguments
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _wait_until_ready(state: LocalApplicationState) -> None:
    url = f"http://{HOST}:{state.port}/api/v1/health"
    for _ in range(80):
        if not _is_owned(state.server_pid, "serve") or not _is_owned(state.worker_pid, "worker"):
            raise ConfigError(
                "The server or worker stopped during startup; review the TailorCV logs "
                "under the data root's .tailorcv-runtime directory."
            )
        try:
            with urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise ConfigError("The local server did not become ready within 20 seconds.")


def _is_healthy(port: int) -> bool:
    try:
        with urlopen(f"http://{HOST}:{port}/api/v1/health", timeout=1):
            return True
    except OSError:
        return False


def _write_state(path: Path, state: LocalApplicationState) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(state)), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _read_state(path: Path) -> LocalApplicationState | None:
    try:
        return LocalApplicationState(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError):
        return None


def _process_start(pid: int) -> tuple[bool | None, str | None]:
    try:
        process = subprocess.run(  # noqa: S603 - fixed ps arguments and local pid
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            check=False,
            text=True,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except OSError:
        return None, None
    if process.returncode == 1:
        return False, None
    if process.returncode != 0 or not process.stdout.strip():
        return None, None
    return True, process.stdout.strip()


def local_process_started_at(pid: int) -> str | None:
    """Read an opaque process-start identity for local source-read ownership."""
    return _process_start(pid)[1]


def reader_process_alive(pid: int, started_at: str) -> bool | None:
    """Distinguish a surviving reader from an exited process or reused PID."""
    alive, current_start = _process_start(pid)
    if alive is not True:
        return alive
    return current_start == started_at


def _is_owned(pid: int, role: str) -> bool:
    try:
        process = subprocess.run(  # noqa: S603 - fixed ps arguments and local pid
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as error:
        raise ConfigError(
            "TailorCV could not inspect its managed processes; retry the command."
        ) from error
    if process.returncode not in (0, 1):
        raise ConfigError("TailorCV could not inspect its managed processes; retry the command.")
    if process.returncode == 1:
        return False
    return "tailorcv.bootstrap.local_runtime" in process.stdout and f" {role} " in process.stdout


def _owned_processes(state: LocalApplicationState) -> tuple[bool, bool]:
    """Inspect both recorded children before a caller changes ownership state."""
    return _is_owned(state.server_pid, "serve"), _is_owned(state.worker_pid, "worker")


def _wait_until_stopped(state: LocalApplicationState, *, worker_only: bool = False) -> None:
    """Keep ownership visible while children drain or a bounded wait fails."""
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while True:
        server_owned, worker_owned = _owned_processes(state)
        if not worker_owned and (worker_only or not server_owned):
            return
        if time.monotonic() >= deadline:
            if worker_owned:
                raise ConfigError("TailorCV worker is still stopping. Retry 'tailorcv stop'.")
            raise ConfigError(
                "TailorCV local API is still stopping. Retry 'tailorcv stop' after the API exits."
            )
        time.sleep(0.1)


def _stop_if_owned(pid: int, role: str, signum: int = signal.SIGTERM) -> None:
    if _is_owned(pid, role):
        os.kill(pid, signum)


def main() -> None:
    """Run one child process; the public CLI owns creation and stopping."""
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("serve", "worker"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--workspaces-root", type=Path)
    parser.add_argument("--port", required=True, type=int)
    arguments = parser.parse_args()
    if arguments.role == "serve":
        from tailorcv.delivery.cli.runtime import serve

        serve(
            HOST,
            arguments.port,
            arguments.database_url,
            arguments.data_root,
            arguments.artifact_root,
            workspaces_root=arguments.workspaces_root,
        )
        return
    from tailorcv.delivery.cli.runtime import worker

    worker(
        False,
        LOCAL_WORKER_ID,
        1.0,
        arguments.database_url,
        arguments.artifact_root,
        arguments.data_root,
        workspaces_root=arguments.workspaces_root,
    )


if __name__ == "__main__":
    main()
