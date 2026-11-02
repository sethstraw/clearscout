"""The critic's structured verdict and the doubts it raises."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from tailorcv.domain import ClaimBoundary

from .rubric import SCORED_AXES


class DoubtedClaim(BaseModel):
    """One statement the critic does not find supported, and the boundary it crosses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str
    boundary: ClaimBoundary
    # Merged from the sampled draws and kept out of the schema the provider is sent.
    corroboration: Annotated[int, SkipJsonSchema()] = 0

    def __str__(self) -> str:
        """Render the claim beside its boundary, for a report line or a redraft finding."""
        return f"{self.claim} [{self.boundary}]"


class CriticVerdict(BaseModel):
    """The critic's independent factual and hiring-quality judgments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unsupported_claims: list[DoubtedClaim] = Field(default_factory=list)
    style_flags: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    guidance: str = ""
    alignment: int = Field(ge=1, le=5)
    preservation: int = Field(ge=1, le=5)
    specificity: int = Field(ge=1, le=5)
    readability: int = Field(ge=1, le=5)
    ats_clarity: int = Field(ge=1, le=5)
    human_voice: int = Field(ge=1, le=5)

    def scores(self) -> dict[str, int]:
        """Return every axis this verdict scored, by field name."""
        return {axis: int(getattr(self, axis)) for axis in SCORED_AXES}

    def quality_scores(self) -> dict[str, int]:
        """Return the non-preservation axes used to refine writing quality."""
        return {
            "alignment": self.alignment,
            "specificity": self.specificity,
            "readability": self.readability,
            "ats clarity": self.ats_clarity,
            "human voice": self.human_voice,
        }

    @property
    def quality_average(self) -> float:
        """Return the transparent mean of the five writing-quality axes."""
        scores = self.quality_scores().values()
        return sum(scores) / len(scores)
