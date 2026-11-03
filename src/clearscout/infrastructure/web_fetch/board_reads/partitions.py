"""Facet partitions that split a capped listing into readable parts."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

PARTITION_DEPTH = 2


PARTITIONS_KEY = "__reader_partitions"


NESTED_FACET_VALUES = 8


@dataclass(frozen=True)
class FacetValue:
    """One value of a publisher's search facet and the count the publisher states for it."""

    id: str
    count: int


type FacetCounts = Mapping[str, tuple[FacetValue, ...]]


@dataclass(frozen=True)
class ListingPartition:
    """One slice of a capped listing: the facet values it applies and the count they state."""

    facets: tuple[tuple[str, str], ...]
    count: int


@dataclass(frozen=True)
class PartitionPlan:
    """The partitions still to read, the first being read now.

    ``overlaps`` marks a plan made after rows were folded in, so a re-read repeats them;
    ``resplit`` asks the current partition's first page to split it again.
    """

    queue: tuple[ListingPartition, ...]
    overlaps: bool = False
    resplit: bool = False

    def as_query(self) -> str:
        """Encode the plan as one continuation query value."""
        return json.dumps(
            {
                "overlaps": self.overlaps,
                "queue": [
                    [[list(pair) for pair in part.facets], part.count] for part in self.queue
                ],
                "resplit": self.resplit,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_query(cls, value: str) -> PartitionPlan | None:
        """Decode a plan written by ``as_query``, or None for any other shape."""
        try:
            data = json.loads(value)
            queue = tuple(
                ListingPartition(tuple((str(k), str(v)) for k, v in facets), int(count))
                for facets, count in data["queue"]
            )
            overlaps, resplit = data["overlaps"], data["resplit"]
        except (ValueError, TypeError, KeyError):
            return None
        valid = all(0 < len(part.facets) <= PARTITION_DEPTH for part in queue)
        flags = isinstance(overlaps, bool) and isinstance(resplit, bool)
        return cls(queue, overlaps, resplit) if queue and valid and flags else None


def partitioned_size(facets: FacetCounts, stated: int, *, applied: Iterable[str] = ()) -> int:
    """Return a listing's true size: a sum above ``stated`` two unapplied facets agree on."""
    skip = set(applied)
    sums = [
        sum(value.count for value in values)
        for name, values in facets.items()
        if name not in skip and values
    ]
    agreed = [total for total in sums if total > stated and sums.count(total) > 1]
    return max(agreed, default=stated)


def _summing_values(
    values: tuple[FacetValue, ...], size: int, *, nested: bool
) -> tuple[FacetValue, ...] | None:
    """Return the values summing to ``size``: all of them, or the one smallest subset that does.

    A subset is tried only for a ``nested`` facet, whose ranges contain one another.
    """
    widths = range(1, len(values)) if nested and len(values) <= NESTED_FACET_VALUES else ()
    for width in (len(values), *widths):
        found = [
            chosen
            for chosen in combinations(values, width)
            if sum(value.count for value in chosen) == size
        ]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def split_partition(
    partition: ListingPartition,
    facets: FacetCounts,
    *,
    size: int,
    nested: bool = False,
) -> tuple[ListingPartition, ...] | None:
    """Split by the first unapplied facet whose values sum to ``size``, one part per value."""
    if len(partition.facets) >= PARTITION_DEPTH:
        return None
    applied = {name for name, _ in partition.facets}
    for name, values in facets.items():
        chosen = None if name in applied else _summing_values(values, size, nested=nested)
        if chosen is None:
            continue
        parts = tuple(
            ListingPartition((*partition.facets, (name, value.id)), value.count)
            for value in sorted(chosen, key=lambda value: value.id)
            if value.count > 0
        )
        if parts:
            return parts
    return None
