"""Shared Pydantic model policy."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Reject fields that the declared contract does not own."""

    model_config = ConfigDict(extra="forbid")
