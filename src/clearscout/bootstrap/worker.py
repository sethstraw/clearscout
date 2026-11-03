"""Single-local-worker execution over the durable task queue."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime
from functools import cached_property, partial
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy.exc import OperationalError

from clearscout.application.failures import ConflictError, NotFoundError, ValidationError
from clearscout.application.tasks import ClaimedTask, TaskHandler, TaskQueueService, TaskResponse

# The redundant alias is PEP 484 explicit re-export, which strict mypy requires.
from clearscout.application.tasks import TaskContext as TaskContext  # noqa: PLC0414
from clearscout.application.tasks.leases import (
    CONTINUATION_TASK_TYPE,
    DEFAULT_TASK_LEASE_SECONDS,
    LaneClaim,
)
from clearscout.application.workspaces.scan_schedule import ScanScheduleService
from clearscout.domain import TaskStatus, utc_now
from clearscout.infrastructure.artifacts.runtime import RuntimeStorage
from clearscout.infrastructure.artifacts.store import LocalArtifactStore, file_uri_path
from clearscout.infrastructure.build import current_build
from clearscout.infrastructure.persistence.database import Database
from clearscout.infrastructure.persistence.models import Artifact, Workspace

if TYPE_CHECKING:
    from clearscout.application.discovery.posting_fetch import PostingFetcher
    from clearscout.application.discovery.source_reading import SourceReader
    from clearscout.application.tailoring.workflows import WorkflowRunner
    from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient

logger = logging.getLogger(__name__)

# A continuation chunk still running after 30 minutes is failed and its thread left behind.
HANDLER_TIME_LIMITS: Mapping[str, float] = MappingProxyType({CONTINUATION_TASK_TYPE: 30 * 60})
# Tasks a killed worker leaves for lease recovery. Paid handlers file their spend only on exit.
KILL_SAFE_TASK_TYPES = frozenset({"source.scan", CONTINUATION_TASK_TYPE, "artifact.verify"})
# Board continuations for different hosts read at once, up to this many.
CONTINUATION_SLOTS_ENVIRONMENT_VARIABLE = "CLEARSCOUT_CONTINUATION_SLOTS"
DEFAULT_CONTINUATION_SLOTS = 4
MIN_CONTINUATION_SLOTS = 1
MAX_CONTINUATION_SLOTS = 8


def _slot_count_allowed(count: int) -> bool:
    return MIN_CONTINUATION_SLOTS <= count <= MAX_CONTINUATION_SLOTS


def configured_continuation_slots(stated: int | None = None) -> int:
    """Return the stated slot count, else CLEARSCOUT_CONTINUATION_SLOTS's, else the default."""
    named = "--continuation-slots"
    if stated is None:
        named = CONTINUATION_SLOTS_ENVIRONMENT_VARIABLE
        text = os.environ.get(CONTINUATION_SLOTS_ENVIRONMENT_VARIABLE, "").strip()
        if not text:
            return DEFAULT_CONTINUATION_SLOTS
        stated = int(text) if text.isdecimal() else MIN_CONTINUATION_SLOTS - 1
    if not _slot_count_allowed(stated):
        raise ValidationError(
            f"{named} must be a whole number "
            f"from {MIN_CONTINUATION_SLOTS} to {MAX_CONTINUATION_SLOTS}."
        )
    return stated


class _WorkerTaskContext:
    """Cooperative heartbeat, progress, and cancellation boundary for handlers."""

    def __init__(
        self,
        queue: TaskQueueService,
        task_id: str,
        worker_id: str,
        lease_keeper: _LeaseKeeper,
    ) -> None:
        """Bind progress operations to one worker-owned task lease."""
        self.queue = queue
        self.task_id = task_id
        self.worker_id = worker_id
        self.lease_keeper = lease_keeper

    @property
    def attempt_count(self) -> int:
        return self.lease_keeper.expected_attempt

    @property
    def process_id(self) -> int:
        return os.getpid()

    @cached_property
    def process_started_at(self) -> str | None:
        from clearscout.bootstrap.local_runtime import local_process_started_at

        return local_process_started_at(self.process_id)

    def progress(self, stage: str, payload: dict[str, Any]) -> None:
        """Persist progress and extend the task lease."""
        self.lease_keeper.check_deadline()
        failure = self.lease_keeper.failure
        if isinstance(failure, (HandlerTimeLimitError, LeaseRenewalError)):
            raise failure
        if failure is not None:
            raise LeaseRenewalError from failure
        self.queue.renew_worker_presence(worker_id=self.worker_id)
        self.queue.heartbeat(
            self.task_id,
            worker_id=self.worker_id,
            expected_attempt=self.lease_keeper.expected_attempt,
            progress=payload,
            stage=stage,
        )
        logger.info(
            "task_progress",
            extra={
                "task_id": self.task_id,
                "worker_id": self.worker_id,
                "stage": stage,
                "phase": payload.get("phase"),
                "completed": payload.get("completed"),
                "current": payload.get("current"),
                "total": payload.get("total"),
            },
        )

    def cancellation_requested(self) -> bool:
        """Return whether the user requested cooperative cancellation."""
        task = self.queue.get(self.task_id)
        return task.cancellation_requested_at is not None

    def raise_if_cancelled(self) -> None:
        """Stop safely at a handler-defined cancellation point."""
        self.lease_keeper.check_deadline()
        failure = self.lease_keeper.failure
        if isinstance(failure, (HandlerTimeLimitError, LeaseRenewalError)):
            raise failure
        if failure is not None:
            raise LeaseRenewalError from failure
        if self.cancellation_requested():
            raise TaskCancelledError


class TaskCancelledError(Exception):
    """Internal cooperative signal translated to a canceled durable task."""


class LeaseRenewalError(Exception):
    """The worker can no longer prove that it owns the running task."""


class HandlerTimeLimitError(Exception):
    """The handler ran past the worker's wall-clock cap, so its lease is no longer renewed."""


class HandlerAbandonedError(HandlerTimeLimitError):
    """The handler did not stop within its grace period, so its thread was left running."""

    def __init__(self, *, repeated: bool) -> None:
        """Record whether an earlier abandoned handler thread is still alive."""
        super().__init__()
        self.repeated = repeated


@runtime_checkable
class AbandonedTaskRecovery(Protocol):
    """A handler that can settle a task's work after the worker abandoned its thread."""

    def recover_abandoned(self, task_id: str) -> None:
        """Record the abandoned task's claimed work unless its own thread already did."""
        ...


class _LeaseKeeper:
    """Renew a running task lease while its handler is blocked in external I/O."""

    def __init__(
        self,
        queue: TaskQueueService,
        task_id: str,
        worker_id: str,
        interval_seconds: float,
        time_limit_seconds: float = math.inf,
        clock: Callable[[], float] = time.monotonic,
        *,
        expected_attempt: int | None = None,
    ) -> None:
        self.queue = queue
        self.task_id = task_id
        self.worker_id = worker_id
        self.expected_attempt = (
            queue.get(task_id).attempt_count if expected_attempt is None else expected_attempt
        )
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.deadline = clock() + time_limit_seconds
        self._stop = Event()
        self.failure: Exception | None = None
        self._thread = Thread(
            target=self._run,
            name=f"clearscout-lease-{task_id}",
            daemon=True,
        )

    def start(self) -> None:
        """Begin renewing after the first interval elapses."""
        self._thread.start()

    def stop(self) -> None:
        """Stop renewal before the task is completed or failed."""
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 5)

    def check_deadline(self) -> None:
        """Record the time-limit failure once the handler's deadline has passed."""
        if self.failure is None and self._remaining() <= 0:
            self.failure = HandlerTimeLimitError()

    def _run(self) -> None:
        while not self._stop.wait(min(self.interval_seconds, self._remaining())):
            self.check_deadline()
            if isinstance(self.failure, HandlerTimeLimitError):
                return
            try:
                self.queue.renew_worker_presence(worker_id=self.worker_id)
                self.queue.heartbeat(
                    self.task_id,
                    worker_id=self.worker_id,
                    expected_attempt=self.expected_attempt,
                    lease_seconds=DEFAULT_TASK_LEASE_SECONDS,
                )
            except OperationalError as exc:
                # A short SQLite writer conflict must not fail paid or external work.
                # The next interval retries well before the normal lease expires.
                logger.warning(
                    "task_lease_renewal_failed",
                    extra={
                        "task_id": self.task_id,
                        "worker_id": self.worker_id,
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - contain daemon-thread failures
                self.failure = exc
                logger.error(
                    "task_lease_renewal_stopped",
                    extra={
                        "task_id": self.task_id,
                        "worker_id": self.worker_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return

    def _remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())


class Worker:
    """Recoverable worker whose handler registry is explicit and testable."""

    def __init__(
        self,
        database: Database,
        *,
        worker_id: str,
        handlers: dict[str, TaskHandler],
        lease_heartbeat_seconds: float = 20.0,
        handler_time_limits: Mapping[str, float] = HANDLER_TIME_LIMITS,
        continuation_slots: int = DEFAULT_CONTINUATION_SLOTS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure one worker identity and the only task types it can execute.

        `clock` measures handler time limits, and `sleep` paces idle polls and write retries.
        """
        if not _slot_count_allowed(continuation_slots):
            raise ValueError(
                f"Continuation slots must be between {MIN_CONTINUATION_SLOTS} "
                f"and {MAX_CONTINUATION_SLOTS}."
            )
        if lease_heartbeat_seconds <= 0 or lease_heartbeat_seconds >= DEFAULT_TASK_LEASE_SECONDS:
            raise ValueError(
                "The lease heartbeat interval must be positive and shorter than the task lease."
            )
        if any(limit <= 0 for limit in handler_time_limits.values()):
            raise ValueError("A handler time limit must be positive.")
        if handler_time_limits and lease_heartbeat_seconds >= DEFAULT_TASK_LEASE_SECONDS / 2:
            raise ValueError(
                "With a handler time limit, the heartbeat must be under half the task lease."
            )
        self.database = database
        self.worker_id = worker_id
        self.handlers = handlers
        self.lease_heartbeat_seconds = lease_heartbeat_seconds
        self.handler_time_limits = handler_time_limits
        from clearscout.bootstrap.local_runtime import reader_process_alive

        self.queue = TaskQueueService(database, reader_alive=reader_process_alive)
        self.scan_schedule = ScanScheduleService(database, queue=self.queue, clock=wall_clock)
        self.continuation_slots = continuation_slots
        self.clock = clock
        self.sleep = sleep
        # Claiming and marking a host busy happen under one guard, so two slots never share a host.
        self._slots_guard = Lock()
        self._slots: set[Thread] = set()
        self._busy_hosts: set[str] = set()
        self._slot_failure: BaseException | None = None
        self._stopping = False
        self._stop_requested = Event()
        self._abandoned_thread: Thread | None = None
        # Fix the build identity at worker start; modules imported later may be newer.
        current_build()

    def run_once(self) -> bool:
        """Claim and execute at most one task inline, returning whether work was found."""
        task = self.queue.claim_next(
            worker_id=self.worker_id,
            lease_seconds=DEFAULT_TASK_LEASE_SECONDS,
        )
        if task is None:
            return False
        self._execute(task)
        return True

    def request_stop(self) -> None:
        """Stop new claims after the current inline task and active slots settle."""
        self._stop_requested.set()

    def dispatch(self) -> bool:
        """Claim one task, starting a board continuation on a free slot and running others inline.

        Returns whether work was found. A continuation whose board host is already being
        read, or that finds every slot busy, stays queued.
        """
        self._raise_slot_failure()
        with self._slots_guard:
            try:
                claimed = self.queue.claim_lane(
                    worker_id=self.worker_id,
                    lease_seconds=DEFAULT_TASK_LEASE_SECONDS,
                    scope="any" if len(self._slots) < self.continuation_slots else "other",
                    busy_hosts=self._busy_hosts,
                )
            except OperationalError as exc:
                # The work stays queued, and the next poll claims it again.
                logger.warning(
                    "task_claim_failed",
                    extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                )
                return False
            if claimed is None:
                return False
            if claimed.task.task_type == CONTINUATION_TASK_TYPE:
                self._start_slot(claimed)
                return True
        self._execute(claimed.task)
        return True

    def wait_idle(self) -> None:
        """Block until every continuation slot has drained, re-raising a slot's fatal error."""
        while True:
            with self._slots_guard:
                running = list(self._slots)
            if not running:
                break
            for slot in running:
                slot.join()
        self._raise_slot_failure()

    def _start_slot(self, claimed: LaneClaim) -> None:
        """Run a claimed continuation on its own slot thread; the caller holds the slot guard."""
        slot = Thread(
            target=self._run_slot,
            args=(claimed,),
            name=f"clearscout-slot-{claimed.task.id}",
            daemon=True,
        )
        if claimed.host is not None:
            self._busy_hosts.add(claimed.host)
        self._slots.add(slot)
        slot.start()

    def _run_slot(self, claimed: LaneClaim | None) -> None:
        """Run continuations for one host after another until none can be claimed."""
        try:
            while claimed is not None:
                self._execute(claimed.task)
                with self._slots_guard:
                    if claimed.host is not None:
                        self._busy_hosts.discard(claimed.host)
                    # Cleared first, so a raising claim cannot free a host another slot took.
                    claimed = None
                    claimed = self._claim_next_continuation()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the dispatching thread
            # Logged while the slot is still listed, so wait_idle cannot re-raise ahead of it.
            try:
                logger.error(
                    "continuation_slot_failed",
                    extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                )
            finally:
                with self._slots_guard:
                    if claimed is not None and claimed.host is not None:
                        self._busy_hosts.discard(claimed.host)
                    self._slots.discard(current_thread())
                    self._slot_failure = self._slot_failure or exc

    def _claim_next_continuation(self) -> LaneClaim | None:
        """Claim a slot's next continuation, or retire the slot; the caller holds the guard."""
        claimed = None
        if not self._stopping and not self._stop_requested.is_set() and self._slot_failure is None:
            try:
                claimed = self.queue.claim_lane(
                    worker_id=self.worker_id,
                    lease_seconds=DEFAULT_TASK_LEASE_SECONDS,
                    scope="continuation",
                    busy_hosts=self._busy_hosts,
                )
            except OperationalError as exc:
                # The work stays queued, and the worker's next dispatch claims it again.
                logger.warning(
                    "continuation_slot_claim_failed",
                    extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                )
        if claimed is None:
            self._slots.discard(current_thread())
        elif claimed.host is not None:
            self._busy_hosts.add(claimed.host)
        return claimed

    def _raise_slot_failure(self) -> None:
        """Re-raise the first error that stopped a slot, such as a second abandoned handler."""
        with self._slots_guard:
            failure = self._slot_failure
        if failure is not None:
            raise failure

    def _execute(self, task: ClaimedTask) -> None:
        """Run one claimed task to a durable terminal state."""
        task_id = task.id
        task_type = task.task_type
        payload = task.payload
        started = time.monotonic()

        def log_finished(status: str, *, error_type: str | None = None) -> None:
            logger.info(
                "task_execution",
                extra={
                    "task_id": task_id,
                    "task_type": task_type,
                    "attempt": task.attempt_count,
                    "worker_id": self.worker_id,
                    "state": "finished",
                    "status": status,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "error_type": error_type,
                },
            )

        logger.info(
            "task_execution",
            extra={
                "task_id": task_id,
                "task_type": task_type,
                "attempt": task.attempt_count,
                "worker_id": self.worker_id,
                "state": "started",
            },
        )

        def reject(error_category: str, error_message: str) -> None:
            outcome = self._finish_task(
                task_id=task_id,
                task_type=task_type,
                expected_attempt=task.attempt_count,
                status=TaskStatus.FAILED,
                progress=None,
                error_category=error_category,
                error_message=error_message,
            )
            if isinstance(outcome, tuple):
                log_finished(outcome[0], error_type=outcome[1])
            else:
                log_finished(outcome.status.value, error_type=outcome.error_category)

        if not isinstance(payload, dict):
            reject("invalid_payload", "Task payload was not a JSON object.")
            return
        handler = self.handlers.get(task_type)
        if handler is None:
            reject(
                "unsupported_task_type",
                f"No worker handler is registered for {task_type!r}.",
            )
            return

        time_limit = self.handler_time_limits.get(task_type, math.inf)
        lease_keeper = _LeaseKeeper(
            self.queue,
            task_id,
            self.worker_id,
            self.lease_heartbeat_seconds,
            time_limit,
            self.clock,
            expected_attempt=task.attempt_count,
        )
        context = _WorkerTaskContext(self.queue, task_id, self.worker_id, lease_keeper)
        lease_keeper.start()
        try:
            status, progress, error_category, error_message, abandoned = self._handler_outcome(
                handler, context, payload, time_limit
            )
        except BaseException:
            # The handler thread outlives an interrupt; its next progress or cancellation
            # point stops it. A renewal still blocked past the join timeout may overwrite this
            # failure, which progress and cancellation points still raise as LeaseRenewalError.
            lease_keeper.stop()
            lease_keeper.failure = LeaseRenewalError()
            raise
        try:
            terminal_outcome = self._finish_task(
                task_id=task_id,
                task_type=task_type,
                expected_attempt=task.attempt_count,
                status=status,
                progress=progress,
                error_category=error_category,
                error_message=error_message,
            )
        finally:
            lease_keeper.stop()
        if abandoned is not None:
            self._recover_abandoned(
                handler,
                task_id,
                task_type,
                repeated=abandoned.repeated,
                recover_work=not isinstance(terminal_outcome, tuple),
            )

        if isinstance(terminal_outcome, tuple):
            terminal_status, terminal_error = terminal_outcome
            log_finished(terminal_status, error_type=terminal_error)
            return
        log_finished(terminal_outcome.status.value, error_type=terminal_outcome.error_category)

    def _handler_outcome(
        self,
        handler: TaskHandler,
        context: _WorkerTaskContext,
        payload: dict[str, Any],
        time_limit: float,
    ) -> tuple[
        TaskStatus, dict[str, Any] | None, str | None, str | None, HandlerAbandonedError | None
    ]:
        """Run the handler and return its status, progress, error, and any abandoned thread."""
        try:
            progress = self._run_handler(handler, context, payload)
        except TaskCancelledError:
            return TaskStatus.CANCELLED, None, None, None, None
        except HandlerTimeLimitError as exc:
            return (
                TaskStatus.FAILED,
                None,
                "handler_time_limit",
                f"The task ran longer than {time_limit:g} seconds and was stopped.",
                exc if isinstance(exc, HandlerAbandonedError) else None,
            )
        except LeaseRenewalError:
            return (
                TaskStatus.FAILED,
                None,
                "lease_renewal_died",
                "The worker could no longer renew this task's lease.",
                None,
            )
        except Exception as exc:  # noqa: BLE001 - durable boundary: record it and keep working
            return TaskStatus.FAILED, None, type(exc).__name__, _safe_error(exc), None
        return TaskStatus.COMPLETED, progress, None, None, None

    def _run_handler(
        self,
        handler: TaskHandler,
        context: _WorkerTaskContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a handler on its own thread, abandoning it once it passes the time cap."""
        outcome: list[dict[str, Any] | BaseException] = []

        def run() -> None:
            try:
                context.raise_if_cancelled()
                result = handler(context, payload)
                context.raise_if_cancelled()
                outcome.append(result)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the worker thread
                outcome.append(exc)

        runner = Thread(target=run, name=f"clearscout-task-{context.task_id}", daemon=True)
        runner.start()
        # One heartbeat interval of grace lets a handler at a cancellation point stop itself.
        give_up_at = context.lease_keeper.deadline + self.lease_heartbeat_seconds
        while runner.is_alive() and (left := give_up_at - self.clock()) > 0:
            runner.join(min(left, self.lease_heartbeat_seconds))
        if not outcome:
            with self._slots_guard:
                earlier = self._abandoned_thread
                self._abandoned_thread = runner
            logger.error(
                "task_handler_abandoned",
                extra={"task_id": context.task_id, "worker_id": self.worker_id},
            )
            raise HandlerAbandonedError(repeated=earlier is not None and earlier.is_alive())
        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def _recover_abandoned(
        self,
        handler: TaskHandler,
        task_id: str,
        task_type: str,
        *,
        repeated: bool,
        recover_work: bool,
    ) -> None:
        """Let the handler settle the failed task's work, then exit if two threads are loose."""
        if recover_work and isinstance(handler, AbandonedTaskRecovery):
            try:
                handler.recover_abandoned(task_id)
            except Exception as exc:  # noqa: BLE001 - the task is already failed durably
                logger.error(
                    "task_abandoned_recovery_failed",
                    extra={
                        "task_id": task_id,
                        "task_type": task_type,
                        "error_type": type(exc).__name__,
                    },
                )
        if repeated:
            # A third live writer beside the worker and one abandoned thread is refused.
            raise SystemExit(
                f"Worker {self.worker_id!r} abandoned a second handler while an earlier "
                "one was still running; restart the worker."
            )

    def _finish_task(
        self,
        *,
        task_id: str,
        task_type: str,
        expected_attempt: int,
        status: TaskStatus,
        progress: dict[str, Any] | None,
        error_category: str | None,
        error_message: str | None,
    ) -> TaskResponse | tuple[str, str]:
        """Persist one terminal outcome without replaying completed handler work."""
        for attempt in range(3):
            try:
                return self.queue.finish(
                    task_id,
                    worker_id=self.worker_id,
                    expected_attempt=expected_attempt,
                    status=status,
                    progress=progress if status is TaskStatus.COMPLETED else None,
                    error_category=error_category,
                    error_message=error_message,
                )
            except OperationalError as exc:
                logger.warning(
                    "task_terminal_write_retry",
                    extra={
                        "task_id": task_id,
                        "task_type": task_type,
                        "worker_id": self.worker_id,
                        "attempt": attempt + 1,
                        "error_type": type(exc).__name__,
                    },
                )
                if attempt == 2:
                    return "terminal_write_unavailable", type(exc).__name__
                self.sleep(0.05 * (attempt + 1))
            except (ConflictError, NotFoundError) as exc:
                logger.error(
                    "task_finalization_ownership_lost",
                    extra={
                        "task_id": task_id,
                        "task_type": task_type,
                        "worker_id": self.worker_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return "ownership_lost", type(exc).__name__
            except Exception as exc:  # noqa: BLE001 - contain the durable boundary
                logger.error(
                    "task_finalization_failed",
                    extra={
                        "task_id": task_id,
                        "task_type": task_type,
                        "worker_id": self.worker_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return "finalization_failed", type(exc).__name__
        raise AssertionError("terminal write retry loop exited without an outcome")

    def run_forever(self, *, poll_seconds: float = 1.0) -> None:
        """Poll until interrupted, then let running slots finish before withdrawing presence.

        Each slot's chunk is bounded by its handler time limit; a second interrupt stops waiting.
        """
        if poll_seconds < 0.1 or poll_seconds > 60:
            raise ValidationError("Worker polling must be between 0.1 and 60 seconds.")
        announced = False
        try:
            while not self._stop_requested.is_set():
                try:
                    self.queue.renew_worker_presence(worker_id=self.worker_id)
                except OperationalError as exc:
                    # A locked writer is retried on the next beat instead of ending the worker.
                    logger.warning(
                        "worker_presence_renewal_failed",
                        extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                    )
                    self.sleep(poll_seconds)
                    continue
                announced = True
                if self._stop_requested.is_set():
                    break
                try:
                    self.scan_schedule.tick()
                except OperationalError as exc:
                    logger.warning(
                        "scan_schedule_claim_failed",
                        extra={"worker_id": self.worker_id, "error_type": type(exc).__name__},
                    )
                    self.sleep(poll_seconds)
                    continue
                if not self.dispatch():
                    self.sleep(poll_seconds)
        finally:
            with self._slots_guard:
                self._stopping = True
            try:
                # The interrupt that stopped polling is the error to surface, not a slot's.
                with suppress(Exception):
                    self.wait_idle()
            finally:
                if announced:
                    with suppress(Exception):
                        self.queue.withdraw_worker_presence(worker_id=self.worker_id)


class ArtifactVerifier:
    """Worker handler that validates indexed file existence and SHA-256."""

    def __init__(
        self, database: Database, artifact_root: Path | RuntimeStorage | None = None
    ) -> None:
        """Configure the private root used for new artifacts."""
        self.database = database
        self.storage = (
            artifact_root
            if isinstance(artifact_root, RuntimeStorage)
            else RuntimeStorage.resolve(database.url, artifact_root=artifact_root)
        )
        self.artifact_root = self.storage.artifact_root

    def __call__(self, context: TaskContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Verify one artifact by ID without rewriting its bytes."""
        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValidationError("artifact.verify requires a non-empty artifact_id.")
        context.progress("loading", {"artifact_id": artifact_id})
        context.raise_if_cancelled()
        with self.database.session() as session:
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.archived_at is not None:
                raise NotFoundError(f"Artifact {artifact_id!r} does not exist.")
            workspace = session.get(Workspace, artifact.workspace_id)
            if workspace is None:
                raise NotFoundError(f"Artifact workspace {artifact.workspace_id!r} does not exist.")
            path = file_uri_path(artifact.storage_uri)
            if path is None or not path.is_file():
                artifact.integrity_status = "missing"
                artifact.last_verified_at = utc_now()
                return {"artifact_id": artifact.id, "integrity_status": "missing"}
            readable_roots = self.storage.readable_roots(workspace.root_path)
            store = LocalArtifactStore(self.artifact_root, readable_roots=readable_roots)
            descriptor = store.inspect(path)
            artifact.integrity_status = (
                "verified" if descriptor.sha256 == artifact.sha256 else "changed"
            )
            artifact.last_verified_at = utc_now()
            result = {
                "artifact_id": artifact.id,
                "integrity_status": artifact.integrity_status,
                "expected_sha256": artifact.sha256,
                "observed_sha256": descriptor.sha256,
                "byte_size": descriptor.byte_size,
            }
        context.progress("verified", result)
        return result


def default_handlers(
    database: Database,
    *,
    artifact_root: Path | RuntimeStorage | None = None,
    fetcher: SourceReader | PostingFetcher | None = None,
    workflow_runner: WorkflowRunner | None = None,
    http_client: PoliteHttpClient | None = None,
) -> dict[str, TaskHandler]:
    """Build the complete production handler graph for one worker process.

    Every web reader shares one client: ``http_client`` when given, which its caller closes.
    """
    from clearscout.application.discovery.assisted_content_resolution import (
        AssistedContentResolutionHandler,
        AssistedContentResolutionService,
    )
    from clearscout.application.discovery.board_scan import (
        DiscoveryRefreshBatchHandler,
        PendingQueueRescreenHandler,
        SourceScanHandler,
    )
    from clearscout.application.discovery.content_resolution import (
        PostingContentResolutionService,
    )
    from clearscout.application.discovery.guided_campaign_runner import (
        GuidedCampaignHandler,
        GuidedCampaignRetryHandler,
    )
    from clearscout.application.discovery.posting_fetch import PostingFetcher
    from clearscout.application.discovery.posting_refresh import (
        PostingRefreshBatchHandler,
        PostingRefreshHandler,
        PostingRefreshWithReextractionHandler,
    )
    from clearscout.application.discovery.posting_rematch import PostingRefreshWithRematchHandler
    from clearscout.application.discovery.relevance_review import RelevanceReviewHandler
    from clearscout.application.discovery.search_vocabulary import SearchVocabularyHandler
    from clearscout.application.discovery.source_continuation import SourceContinuationHandler
    from clearscout.application.discovery.source_reading import (
        SourceContinuationReader,
        SourceReader,
    )
    from clearscout.application.documents import DocumentRenderHandler
    from clearscout.application.tailoring.workflows import (
        BatchHandler,
        DiscoveryEvaluateBatchHandler,
        DiscoveryHandler,
        EvaluationHandler,
        EvaluationRunner,
        EvaluationWorkflowService,
        TailoringHandler,
        WorkflowService,
    )
    from clearscout.bootstrap.local_runtime import reader_process_alive
    from clearscout.infrastructure.web_fetch.board_adapters import BoardApiFetcher
    from clearscout.infrastructure.web_fetch.company_research import CompanyResearchFetcher
    from clearscout.infrastructure.web_fetch.ingest.url import USER_AGENT, workday_posting_detail
    from clearscout.infrastructure.web_fetch.policy import PoliteHttpClient
    from clearscout.infrastructure.web_fetch.posting_fetch import PolitePostingFetcher
    from clearscout.infrastructure.web_fetch.posting_resolution import (
        DeterministicPostingContentResolver,
    )

    storage = (
        artifact_root
        if isinstance(artifact_root, RuntimeStorage)
        else RuntimeStorage.resolve(database.url, artifact_root=artifact_root)
    )
    web = http_client or PoliteHttpClient(user_agent=USER_AGENT)
    board_fetcher = fetcher if isinstance(fetcher, SourceReader) else None
    posting_fetcher = (
        fetcher if isinstance(fetcher, PostingFetcher) else PolitePostingFetcher(client=web)
    )
    runner_candidate: object = workflow_runner
    evaluation_runner = runner_candidate if isinstance(runner_candidate, EvaluationRunner) else None

    workflow_service = WorkflowService(
        database, storage, workflow_runner, research_fetcher=CompanyResearchFetcher(client=web)
    )
    effective_board_fetcher = board_fetcher or BoardApiFetcher(client=web)
    content_resolution = PostingContentResolutionService(
        database,
        DeterministicPostingContentResolver(client=web),
        storage=storage,
        automatic_lanes=4,
    )
    automatic_content_resolution = content_resolution if fetcher is None else None
    guided_campaign_handler = GuidedCampaignHandler(
        database,
        storage=storage,
        fetcher=board_fetcher,
        posting_fetcher=posting_fetcher,
        content_resolution=automatic_content_resolution,
        http_client=web,
    )
    search_vocabulary_handler = SearchVocabularyHandler(database, storage=storage)
    relevance_review_handler = RelevanceReviewHandler(
        database,
        storage=storage,
        fetcher=posting_fetcher,
        content_resolution=automatic_content_resolution,
    )
    source_scan_handler = SourceScanHandler(
        database,
        storage=storage,
        fetcher=effective_board_fetcher,
        posting_fetcher=posting_fetcher,
        content_resolution=automatic_content_resolution,
        reader_alive=reader_process_alive,
        # Wire the live checker only when the caller injected no board reader.
        workday_detail_checker=(
            partial(workday_posting_detail, client=web) if board_fetcher is None else None
        ),
        http_client=web,
    )
    continuation_reader = (
        effective_board_fetcher
        if isinstance(effective_board_fetcher, SourceContinuationReader)
        else BoardApiFetcher(client=web)
    )

    evaluation_handler = EvaluationHandler(
        EvaluationWorkflowService(
            database,
            storage,
            fetcher=posting_fetcher,
            board_fetcher=board_fetcher,
            runner=evaluation_runner,
            content_resolution=content_resolution,
            http_client=web,
        )
    )

    return {
        "artifact.verify": ArtifactVerifier(database, storage),
        "posting.refresh": PostingRefreshHandler(
            database,
            storage=storage,
            fetcher=posting_fetcher,
        ),
        "posting.refresh_batch": PostingRefreshBatchHandler(
            database,
            storage=storage,
            fetcher=posting_fetcher,
        ),
        "posting.refresh_with_reextraction": PostingRefreshWithReextractionHandler(
            database,
            storage=storage,
            fetcher=posting_fetcher,
        ),
        "posting.refresh_with_rematch": PostingRefreshWithRematchHandler(
            database,
            storage=storage,
            http_client=web,
        ),
        "posting.recover_batch": AssistedContentResolutionHandler(
            database,
            AssistedContentResolutionService(database, content_resolution),
        ),
        "discovery.evaluate": evaluation_handler,
        "discovery.evaluate_batch": DiscoveryEvaluateBatchHandler(evaluation_handler, lanes=4),
        "discovery.refresh_batch": DiscoveryRefreshBatchHandler(
            source_scan_handler.service, content_resolution
        ),
        "discovery.rescreen_queue": PendingQueueRescreenHandler(source_scan_handler.service),
        "discovery.relevance_review": relevance_review_handler,
        "workflow.tailor": TailoringHandler(workflow_service),
        "workflow.approve": TailoringHandler(workflow_service),
        "workflow.batch": BatchHandler(workflow_service),
        "workflow.discovery": DiscoveryHandler(workflow_service),
        "document.render": DocumentRenderHandler(database, storage),
        "source.scan": source_scan_handler,
        CONTINUATION_TASK_TYPE: SourceContinuationHandler(
            database,
            continuation_reader,
            source_scan_handler.service.sweep,
            lambda workspace_id, task_id, outcomes: (
                source_scan_handler.service.record_task_outcomes(
                    workspace_id,
                    task_id,
                    outcomes,
                    origin="source.continue",
                )
            ),
            source_scan_handler.service.begin_recording_pass,
            time_limit_seconds=HANDLER_TIME_LIMITS[CONTINUATION_TASK_TYPE],
            reader_alive=reader_process_alive,
        ),
        "search.guided_campaign": guided_campaign_handler,
        "search.guided_campaign_retry": GuidedCampaignRetryHandler(guided_campaign_handler.service),
        "search.vocabulary": search_vocabulary_handler,
    }


def _safe_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    return (text or "Task failed without a message.")[:2000]
