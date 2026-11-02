"""Compose focused workspace routes without changing their public URLs."""

from fastapi import APIRouter

from tailorcv.delivery.api.routers.workspaces import (
    guardrails,
    identity,
    learning_policy,
    master_cv,
    metrics,
    proposals,
    rulings,
    scoring,
    sources,
    spend,
)

router = APIRouter()
for child in (
    identity.router,
    guardrails.router,
    learning_policy.router,
    master_cv.router,
    metrics.router,
    proposals.router,
    rulings.router,
    spend.router,
    sources.router,
    scoring.router,
):
    router.include_router(child)
