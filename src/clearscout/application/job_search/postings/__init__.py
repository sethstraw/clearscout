"""Posting-scoped application services.

Contains the durable record of a posting's requirements as a run stated them,
and the identity rule that carries one requirement across re-evaluations of the
same posting.
"""

from __future__ import annotations

from clearscout.application.job_search.postings.requirements import RequirementIdentityService
from clearscout.application.job_search.postings.rulings import RequirementRulingService

__all__ = ["RequirementIdentityService", "RequirementRulingService"]
