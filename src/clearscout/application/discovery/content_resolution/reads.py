"""Posting reads through the resolver in bounded parallel lanes, each one logged."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextvars import copy_context

from clearscout.application.discovery.content_resolution.results import ResolvedPostingContent
from clearscout.application.discovery.content_resolution.targets import (
    PostingContentResolver,
    PrefetchedContent,
    StoredTarget,
)
from clearscout.application.tasks import TaskContext
from clearscout.domain import new_id
from clearscout.domain.identity import url_host, url_sha256
from clearscout.domain.records import PostingReadIntent

logger = logging.getLogger(__name__)


class PostingReadLanes:
    """Read postings through the resolver in bounded parallel lanes, logging each read."""

    def __init__(self, resolver: PostingContentResolver, automatic_lanes: int) -> None:
        """Bind the reads to the effect port and the number of lanes they may keep busy."""
        self.resolver = resolver
        self.automatic_lanes = automatic_lanes

    def resolve_in_lanes(
        self,
        workspace_id: str,
        targets: list[StoredTarget],
        context: TaskContext | None,
        *,
        on_started: Callable[[int, StoredTarget], None],
        on_resolved: Callable[[int, StoredTarget, ResolvedPostingContent], None],
        prefetched: Mapping[str, PrefetchedContent] | None = None,
    ) -> None:
        """Keep bounded fetch lanes busy while retaining completed sibling results.

        After a resolver failure, already-running siblings may finish and persist,
        but no additional target starts. This keeps failures visible without
        abandoning evidence that was already safely fetched.
        """
        task_id = getattr(context, "task_id", None)
        with ThreadPoolExecutor(
            max_workers=min(self.automatic_lanes, max(1, len(targets)))
        ) as pool:
            next_target = iter(enumerate(targets))
            futures: dict[Future[ResolvedPostingContent], tuple[int, StoredTarget]] = {}
            failures: dict[int, Exception] = {}

            def submit_next() -> bool:
                try:
                    position, target = next(next_target)
                except StopIteration:
                    return False
                if context is not None:
                    context.raise_if_cancelled()
                on_started(position, target)
                futures[
                    pool.submit(
                        copy_context().run,
                        self._resolve_target,
                        target,
                        workspace_id=workspace_id,
                        task_id=task_id,
                        prefetched=(prefetched or {}).get(target.url),
                    )
                ] = (position, target)
                return True

            while len(futures) < self.automatic_lanes and submit_next():
                pass
            while futures:
                future = next(as_completed(futures))
                position, target = futures.pop(future)
                if context is not None:
                    context.raise_if_cancelled()
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - retain successful sibling reads
                    failures[position] = exc
                    continue
                on_resolved(position, target, result)
                if not failures:
                    submit_next()
            if failures:
                raise failures[min(failures)]

    def _resolve_target(
        self,
        target: StoredTarget,
        *,
        workspace_id: str,
        task_id: str | None,
        prefetched: PrefetchedContent | None = None,
    ) -> ResolvedPostingContent:
        """Resolve one posting, from a matching prefetched read when there is one."""
        if prefetched is not None and prefetched.serves(target):
            if prefetched.failure is not None:
                raise prefetched.failure
            if prefetched.result is not None:
                return prefetched.result
        return self.read_posting(
            target.url,
            employer=target.expected_employer,
            title=target.expected_title,
            identity={
                "workspace_id": workspace_id,
                "url_sha256": url_sha256(target.url),
                "pending_discovery_id": target.id,
            },
            task_id=task_id,
            board_url=target.board_url,
            intent=(
                PostingReadIntent.SINGLE_POSTING
                if target.unread_person_add
                else PostingReadIntent.CRAWL
            ),
        )

    def read_posting(
        self,
        url: str,
        *,
        employer: str,
        title: str,
        identity: dict[str, str],
        task_id: str | None,
        board_url: str = "",
        intent: PostingReadIntent = PostingReadIntent.CRAWL,
    ) -> ResolvedPostingContent:
        """Read one posting while emitting privacy-safe wall-clock evidence."""
        operation_id = new_id()
        host = url_host(url) or "unknown"
        started = time.monotonic()
        logger.info(
            "posting_content_read",
            extra={
                **identity,
                "task_id": task_id,
                "operation_id": operation_id,
                "state": "started",
                "host": host,
            },
        )
        try:
            result = self.resolver.resolve(
                url,
                employer=employer,
                title=title,
                intent=intent,
                board_url=board_url,
            )
        except Exception as exc:
            logger.warning(
                "posting_content_read",
                extra={
                    **identity,
                    "task_id": task_id,
                    "operation_id": operation_id,
                    "state": "finished",
                    "host": host,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "outcome": "failed",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        logger.info(
            "posting_content_read",
            extra={
                **identity,
                "task_id": task_id,
                "operation_id": operation_id,
                "state": "finished",
                "host": host,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "outcome": result.content_status.value,
            },
        )
        return result
