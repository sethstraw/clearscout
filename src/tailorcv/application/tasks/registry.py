"""Single explicit registry for production background task types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tailorcv.application.failures import ValidationError
from tailorcv.application.tasks import payloads
from tailorcv.application.tasks.models import TaskTypeResponse
from tailorcv.domain import TaskType

UNPAID = "No provider charge."


@dataclass(frozen=True)
class TaskSpec:
    """One enumerable production task, its payload, and what running it costs."""

    name: TaskType
    payload_model: type[payloads.TaskPayload]
    paid: bool
    cost: str


# Whether a task can reach a billing provider is part of its shared contract.
TASK_SPECS = (
    TaskSpec("artifact.verify", payloads.ArtifactVerifyPayload, False, UNPAID),
    TaskSpec("posting.refresh", payloads.PostingRefreshPayload, False, UNPAID),
    TaskSpec("posting.refresh_batch", payloads.PostingRefreshBatchPayload, False, UNPAID),
    TaskSpec(
        "posting.refresh_with_reextraction",
        payloads.PostingRefreshPayload,
        True,
        "One extraction-tier model-token charge per invocation to re-check every "
        "disputed extraction requirement. No search fee.",
    ),
    TaskSpec(
        "posting.refresh_with_rematch",
        payloads.PostingRefreshPayload,
        True,
        "One match-stage model-token charge per invocation to re-check every disputed fit "
        "requirement. No search fee.",
    ),
    TaskSpec(
        "posting.recover_batch",
        payloads.PostingRecoverBatchPayload,
        True,
        "One model-token charge and up to two hosted-search charges per selected posting.",
    ),
    TaskSpec(
        "discovery.evaluate",
        payloads.EvaluationWorkflowPayload,
        True,
        "Each evaluation may incur provider model-token charges.",
    ),
    TaskSpec(
        "discovery.evaluate_batch",
        payloads.DiscoveryEvaluateBatchPayload,
        True,
        "Evaluates each selected discovery or tracked posting. Provider model-token "
        "charges apply; cost estimates do not stop the task.",
    ),
    TaskSpec(
        "discovery.refresh_batch",
        payloads.DiscoveryRefreshBatchPayload,
        False,
        UNPAID,
    ),
    TaskSpec("discovery.rescreen_queue", payloads.DiscoveryRescreenQueuePayload, False, UNPAID),
    TaskSpec(
        "discovery.relevance_review",
        payloads.RelevanceReviewPayload,
        True,
        "Provider model-token charges may apply per reviewed batch.",
    ),
    TaskSpec(
        "workflow.tailor",
        payloads.TailoringPayload,
        True,
        "A matching completed package is reused at no additional provider cost unless "
        "regeneration is requested. Otherwise it may call the configured provider and "
        "incur model-token charges.",
    ),
    TaskSpec(
        "workflow.approve",
        payloads.ApprovalWorkflowPayload,
        True,
        "A matching completed approved package is reused at no additional provider cost. "
        "Otherwise it may call the configured provider and incur model-token charges.",
    ),
    TaskSpec(
        "workflow.batch",
        payloads.BatchWorkflowPayload,
        True,
        "Provider model-token charges may scale with the number of postings compared.",
    ),
    TaskSpec("workflow.discovery", payloads.DiscoveryWorkflowPayload, False, UNPAID),
    TaskSpec("document.render", payloads.DocumentRenderPayload, False, UNPAID),
    TaskSpec("source.scan", payloads.SourceScanPayload, False, UNPAID),
    TaskSpec("source.continue", payloads.SourceContinuationPayload, False, UNPAID),
    TaskSpec(
        "search.guided_campaign",
        payloads.GuidedCampaignPayload,
        True,
        "Provider charges may include model tokens, search fees, and a web-content-retrieval "
        "charge. Costs are estimates and do not stop an approved task.",
    ),
    TaskSpec("search.guided_campaign_retry", payloads.GuidedCampaignRetryPayload, False, UNPAID),
    TaskSpec(
        "search.vocabulary",
        payloads.SearchVocabularyPayload,
        True,
        "One provider model-token charge per invocation. No search fee.",
    ),
)
# Keyed by plain string so an unrecognized name is a lookup miss rather than a
# type error at the boundary that has to reject it.
TASK_REGISTRY: dict[str, TaskSpec] = {spec.name: spec for spec in TASK_SPECS}


def task_types() -> tuple[TaskType, ...]:
    """Return stable API and worker enumeration order."""
    return tuple(spec.name for spec in TASK_SPECS)


def task_type_catalog() -> tuple[TaskTypeResponse, ...]:
    """Return every registered task type with the charge running it may incur."""
    return tuple(
        TaskTypeResponse(task_type=spec.name, paid=spec.paid, cost=spec.cost) for spec in TASK_SPECS
    )


def task_is_paid(task_type: str) -> bool:
    """Return whether a registered task type can reach a billing provider."""
    spec = TASK_REGISTRY.get(task_type)
    return spec is not None and spec.paid


def validate_task_payload(task_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a production task payload through its one spec."""
    spec = TASK_REGISTRY.get(task_type)
    if spec is None:
        raise ValidationError(f"Unsupported task type {task_type!r}.")
    try:
        validated = spec.payload_model.model_validate(payload)
        return validated.model_dump(mode="json")
    except ValueError as exc:
        raise ValidationError(f"Invalid {task_type} payload: {exc}") from exc
