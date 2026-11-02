"""Request plans for tests that care about one part of a request at a time."""

from __future__ import annotations

from tailorcv.application.config import Stage
from tailorcv.infrastructure.providers.plan import Image, RequestPlan, SearchPolicy, Segment


def plan_for(
    system: str = "s",
    user: str = "u",
    *,
    stage: Stage = Stage.JOBSPEC,
    evidence: str = "",
    images: tuple[Image, ...] = (),
    search: SearchPolicy | None = None,
    max_tokens: int = 8192,
) -> RequestPlan:
    """Return a plan carrying only the parts one test names.

    An empty ``evidence`` means no evidence segment at all, which is what a
    stage that sends no candidate record builds.
    """
    return RequestPlan(
        stage=stage,
        instructions=Segment(system),
        task=Segment(user),
        evidence=Segment(evidence) if evidence else None,
        images=images,
        search=search,
        max_tokens=max_tokens,
    )


__all__ = ["plan_for"]
