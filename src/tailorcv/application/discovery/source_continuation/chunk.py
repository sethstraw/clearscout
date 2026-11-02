"""One claimed frontier chunk, its liveness, and how long it went without progress."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from sqlalchemy.orm import Session

from tailorcv.application.discovery.source_continuation.limits import MAX_NO_PROGRESS_TASKS
from tailorcv.application.discovery.source_reading import (
    SourceContinuationTarget,
    SourceDetailCoverage,
    SourceDetailSummary,
    SourceRead,
)
from tailorcv.infrastructure.persistence.models import SourceScanFrontierItem


@dataclass
class Chunk:
    """One claimed frontier chunk and whether its outcome has been written."""

    continuation_id: str
    workspace_id: str
    generation: int
    items: list[SourceScanFrontierItem]
    include_policy_deferred: bool
    no_progress_count: int
    # The handler lock under which recovery sets `abandoned`.
    lock: RLock = field(default_factory=RLock, repr=False, compare=False)
    source_live: Callable[[], bool] = field(default=lambda: True, repr=False, compare=False)
    write_guard: Callable[[Session], None] | None = field(default=None, repr=False, compare=False)
    recorded: bool = False
    abandoned: bool = False
    member_id: str | None = None
    attempt_count: int = 0
    detail: SourceDetailSummary | None = None
    protected_posting_urls: tuple[str, ...] = ()

    def is_live(self) -> bool:
        """Whether the worker has not yet abandoned and re-armed this chunk."""
        with self.lock:
            return not self.abandoned and self.source_live()

    def require_live(self) -> None:
        """Refuse a further write once the worker has abandoned and re-armed this chunk."""
        if not self.is_live():
            raise ChunkAbandonedError


class ChunkAbandonedError(Exception):
    """The worker recovered this chunk at its time limit, so its own thread writes nothing."""


@dataclass(frozen=True)
class ChunkRecord:
    """What one chunk's read produced, as the handler records it against the frontier."""

    deferred_targets: tuple[SourceContinuationTarget, ...]
    attempted_target_urls: tuple[str, ...] | None
    posting_urls: frozenset[str]
    correlation_id: str
    failures: dict[str, str]
    count_no_progress: bool = True
    detail: SourceDetailSummary | None = None
    detail_coverage: SourceDetailCoverage | None = None
    listing_coverage: SourceDetailCoverage | None = None
    recorded_read: SourceRead | None = None
    discovery_truncated: bool = False
    read_skip_reasons: dict[str, int] | None = None
    listing_pages: int = 0


def stalled_count(
    session: Session, chunk: Chunk, attempted_ids: set[str], count_no_progress: bool
) -> int:
    """Return the no-progress streak, retiring unattempted rows from automatic reads at the cap."""
    if not count_no_progress:
        return chunk.no_progress_count
    stalled = chunk.no_progress_count + 1 if not attempted_ids else 0
    if chunk.include_policy_deferred or stalled < MAX_NO_PROGRESS_TASKS:
        return stalled
    for row in chunk.items:
        if row.id in attempted_ids:
            continue
        stored = session.get(SourceScanFrontierItem, row.id)
        if stored is not None:
            stored.automatic_eligible = False
            stored.deferred_reason = "automatic_no_progress"
    session.flush()
    return 0
