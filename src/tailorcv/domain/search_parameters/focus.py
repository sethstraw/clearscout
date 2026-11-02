"""What the work is, what it is about, and what it is not."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field, field_validator

from tailorcv.shared.models import StrictModel

# A cluster wide enough to match anything ranks nothing, so its terms are bounded.
MAX_CLUSTER_TERMS = 10

# More clusters of one kind than this describe a market rather than a person.
# Ranking reads the first MAX_CLUSTERS stated; every stated cluster still seeds
# a search mode.
MAX_CLUSTERS = 5


class ClusterKind(StrEnum):
    """Whether a cluster names the work itself or what the work is about."""

    CAPABILITY = "capability"
    DOMAIN = "domain"


CLUSTER_KINDS: tuple[str, ...] = tuple(item.value for item in ClusterKind)


class Cluster(StrictModel):
    """A group of words the search treats as one capability or one domain.

    A name alone cannot rank a posting, because nothing in an opening says
    "data platform engineering". The terms are what a title is actually read
    against, and the name is what the person and the search call the group.
    """

    name: str = Field(max_length=120)
    terms: list[str] = Field(default_factory=list, max_length=MAX_CLUSTER_TERMS)
    because: str = Field(default="", max_length=300)

    @property
    def stated(self) -> bool:
        """Return whether this cluster names anything."""
        return bool(self.name.strip())

    @property
    def matched_on(self) -> tuple[str, ...]:
        """Return the words a posting is read against; a cluster with no terms matches its name."""
        stated = [item.strip() for item in (self.name, *self.terms) if item.strip()]
        return tuple(dict.fromkeys(stated))


class Focus(StrictModel):
    """What the work is, what it is about, and what it is not.

    A capability is the craft and a domain is the subject matter. Two openings
    can share a title and answer neither, which is what a title filter alone
    cannot see.
    """

    capabilities: list[Cluster] = Field(default_factory=list)
    domains: list[Cluster] = Field(default_factory=list)
    # Work this person does not want, which ranks a posting down rather than
    # refusing it: an exclusion that hides an opening is `excluded_titles`.
    boundaries: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "domains", mode="before")
    @classmethod
    def bare_clusters_state_no_terms(cls, value: object) -> object:
        """Accept a plain name, which is a cluster nobody gave terms to."""
        if isinstance(value, list):
            return [{"name": item} if isinstance(item, str) else item for item in value]
        return value

    def stated_clusters(self, kind: ClusterKind) -> tuple[Cluster, ...]:
        """Return one kind of cluster, without the blank entries."""
        listed = self.capabilities if kind is ClusterKind.CAPABILITY else self.domains
        return tuple(item for item in listed if item.stated)

    def ranked_clusters(self, kind: ClusterKind) -> tuple[Cluster, ...]:
        """Return the clusters a posting is ranked against: the first ``MAX_CLUSTERS`` stated."""
        return self.stated_clusters(kind)[:MAX_CLUSTERS]

    @property
    def stated_boundaries(self) -> tuple[str, ...]:
        """Return the boundaries worth ranking against, without the blanks."""
        return tuple(item.strip() for item in self.boundaries if item.strip())

    def plus(self, accepted: Sequence[tuple[ClusterKind, Cluster]]) -> Focus:
        """Return this focus widened by the clusters a person accepted.

        A cluster the person wrote outranks the same name a search proposed, so
        their terms are the ones a posting is read against.
        """
        widened = {kind: list(self.stated_clusters(kind)) for kind in ClusterKind}
        for kind, cluster in accepted:
            named = {item.name.strip().casefold() for item in widened[kind]}
            if cluster.name.strip().casefold() not in named:
                widened[kind].append(cluster)
        return Focus(
            capabilities=widened[ClusterKind.CAPABILITY],
            domains=widened[ClusterKind.DOMAIN],
            boundaries=list(self.boundaries),
        )
