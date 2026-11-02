"""Install the shipped aggregator catalog into one workspace."""

from __future__ import annotations

from tailorcv.application.discovery.aggregator_catalog import (
    STARTER_AGGREGATORS,
    StarterAggregator,
)
from tailorcv.application.discovery.board_scan import SourceScanService
from tailorcv.application.job_search.models import SourceResponse, SourceTerms
from tailorcv.domain import SourceKind


class StarterSourceInstaller:
    """Register the shipped aggregators as sources of one workspace."""

    def __init__(
        self,
        sources: SourceScanService,
        *,
        catalog: tuple[StarterAggregator, ...] = STARTER_AGGREGATORS,
    ) -> None:
        """Write through the source registry that already records terms."""
        self.sources = sources
        self.catalog = catalog

    def install(
        self,
        workspace_id: str,
        *,
        origin: str,
        correlation_id: str | None = None,
    ) -> list[SourceResponse]:
        """Register the catalog entries this workspace does not already name.

        Each one arrives switched off. A source the workspace already names is
        left as it stands, switch included.
        """
        known = {item.name for item in self.sources.list_sources(workspace_id).items}
        return [
            self.sources.add_source(
                workspace_id,
                entry.name,
                entry.url,
                source_type=SourceKind.AGGREGATOR,
                terms=SourceTerms(url=entry.terms_url, condition=entry.condition),
                enabled=False,
                origin=origin,
                correlation_id=correlation_id,
            )
            for entry in self.catalog
            if entry.name not in known
        ]
