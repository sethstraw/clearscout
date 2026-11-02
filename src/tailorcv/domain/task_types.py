"""Closed vocabulary for durable background work."""

from typing import Literal, get_args

TaskType = Literal[
    "artifact.verify",
    "posting.refresh",
    "posting.refresh_batch",
    "posting.refresh_with_reextraction",
    "posting.refresh_with_rematch",
    "posting.recover_batch",
    "discovery.evaluate",
    "discovery.evaluate_batch",
    "discovery.refresh_batch",
    "discovery.rescreen_queue",
    "discovery.relevance_review",
    "workflow.tailor",
    "workflow.approve",
    "workflow.batch",
    "workflow.discovery",
    "document.render",
    "source.scan",
    "source.continue",
    "search.guided_campaign",
    "search.guided_campaign_retry",
    "search.vocabulary",
]

TASK_TYPES: tuple[str, ...] = get_args(TaskType)

# Storage accepts one name the queue has no handler for. This order is the
# migration's CHECK text.
STORED_TASK_TYPES: tuple[str, ...] = (
    *TASK_TYPES,
    "posting.resolve_batch",
)
