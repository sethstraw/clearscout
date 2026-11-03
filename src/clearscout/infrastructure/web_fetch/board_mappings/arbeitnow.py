"""Arbeitnow's posting record, as its bounded newest-openings aggregator feed states it."""

from __future__ import annotations

from clearscout.infrastructure.web_fetch.board_mappings.mapper import BoardDeclaration

ARBEITNOW_MAPPING = BoardDeclaration(
    platform="arbeitnow",
    title=("title",),
    url=("url",),
    requisition_id=("slug",),
    employer=("company_name",),
    places=("location",),
)
