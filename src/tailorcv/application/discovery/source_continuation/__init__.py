"""Persist and execute bounded continuation of publisher source traversal."""

from tailorcv.application.discovery.source_continuation.commands import (
    FrontierPersistResult,
    SourceContinuationCommands,
)
from tailorcv.application.discovery.source_continuation.coverage import MANUAL_RECOVERY_NOTE
from tailorcv.application.discovery.source_continuation.family import (
    LATEST_SCAN_FIRST,
    continuation_task_ids,
    fresh_root_task,
    is_current_in_session,
)
from tailorcv.application.discovery.source_continuation.handler import (
    CONTINUATION_CHUNK_SIZE,
    SourceContinuationHandler,
)
from tailorcv.application.discovery.source_continuation.limits import (
    DISCOVERED_DURING_FOLLOW_UP,
    MAX_AUTOMATIC_ATTEMPTS,
    MAX_NO_PROGRESS_TASKS,
    POSTING_DETAIL_COMPLETED,
)
from tailorcv.application.discovery.source_continuation.publisher import (
    drained_publisher_listing_coverage,
)
from tailorcv.application.discovery.source_continuation.rescreen import (
    RESCREEN_COMPLETED_AFTER,
    recent_posting_reads,
    recent_root_posting_reads,
)
from tailorcv.application.discovery.source_continuation.targets import (
    ListedPostingCredit,
    completed_frontier_urls,
)

__all__ = [
    "CONTINUATION_CHUNK_SIZE",
    "DISCOVERED_DURING_FOLLOW_UP",
    "LATEST_SCAN_FIRST",
    "MANUAL_RECOVERY_NOTE",
    "MAX_AUTOMATIC_ATTEMPTS",
    "MAX_NO_PROGRESS_TASKS",
    "POSTING_DETAIL_COMPLETED",
    "RESCREEN_COMPLETED_AFTER",
    "FrontierPersistResult",
    "ListedPostingCredit",
    "SourceContinuationCommands",
    "SourceContinuationHandler",
    "completed_frontier_urls",
    "continuation_task_ids",
    "drained_publisher_listing_coverage",
    "fresh_root_task",
    "is_current_in_session",
    "recent_posting_reads",
    "recent_root_posting_reads",
]
