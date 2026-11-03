"""The learning-loop policy a workspace runs on, versioned like its other policies.

Each field sets how the loop learns from its own history: how much a missed
requirement discounts the next ranking, how many repeated refusals it takes to
propose a hard gate, and how many concurring rulings it takes to propose a
master-CV entry.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LearningLoopPolicy(BaseModel):
    """The knobs the learning loop reads, versioned per workspace."""

    model_config = ConfigDict(extra="forbid")

    enable_dispute_reextraction: bool = False
    enable_dispute_rematch: bool = False
    missed_requirement_penalty: int = Field(default=-5, ge=-30, le=0)
    propose_after_count: int = Field(default=3, ge=1)
    propose_window_days: int = Field(default=30, ge=1)
    propose_after_ruling_count: int = Field(default=3, ge=1)
    propose_missing_after_count: int = Field(default=3, ge=1)
