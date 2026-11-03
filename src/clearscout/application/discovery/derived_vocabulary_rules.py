"""Rule on the names, kinds, and clusters one guided search proposed.

The model writes titles, employer archetypes, and capability or domain clusters.
Every one of them is sanitized and judged here, in Python, before anything acts
on it, and the bounds this decides against are the same ones the request schema
and the instructions state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from clearscout.application.prompts import flatten
from clearscout.domain.identity import normalize_text
from clearscout.domain.search_parameters import (
    MAX_CLUSTER_TERMS,
    ClusterKind,
    EmployerArchetype,
    JobSearchParameters,
    Lane,
    TargetTitle,
)
from clearscout.domain.search_parameters.roles import title_excluded
from clearscout.domain.taxonomy import normalize

MAX_TITLE_LENGTH = 120

MAX_ARCHETYPE_LENGTH = 120

MAX_CLUSTER_LENGTH = 120

_ARCHETYPE_DESCRIPTORS = frozenset(
    {
        "and",
        "companies",
        "company",
        "data",
        "platform",
        "platforms",
        "product",
        "products",
        "reference",
    }
)


def same_employer_archetype(left: str, right: str) -> bool:
    """Return whether two employer-kind names express the same narrow core."""
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if left_normalized == right_normalized:
        return True
    left_core = frozenset(left_normalized.split()) - _ARCHETYPE_DESCRIPTORS
    right_core = frozenset(right_normalized.split()) - _ARCHETYPE_DESCRIPTORS
    return (
        len(left_core) >= 2
        and len(right_core) >= 2
        and (left_core <= right_core or right_core <= left_core)
    )


class ProposedTitle(BaseModel):
    """One name the model says this work also goes by, and where that came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)
    lane: Lane | None = None


class ProposedArchetype(BaseModel):
    """One kind of organisation the model says buys this work, and where it read that."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    because: str
    evidence_ids: list[str] = Field(default_factory=list)


class ProposedCluster(BaseModel):
    """One capability or domain the model reads out of the record, and its words."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    terms: list[str] = Field(default_factory=list)
    because: str
    evidence_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SearchTitle:
    """One synthesized name after the program has decided what to do with it."""

    title: str
    reason: str
    evidence_ids: tuple[str, ...]
    used: bool
    refused_because: str = ""
    lane: Lane | None = None

    def describe(self) -> dict[str, Any]:
        """Return the title as the task result reports it."""
        return {
            "title": self.title,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "used": self.used,
            "refused_because": self.refused_because,
            "lane": None if self.lane is None else self.lane.value,
        }


@dataclass(frozen=True)
class SearchArchetype:
    """One proposed kind of organization after the program has ruled on its shape."""

    name: str
    because: str
    evidence_ids: tuple[str, ...]
    used: bool
    refused_because: str = ""

    def describe(self) -> dict[str, Any]:
        """Return the archetype as the task result reports it."""
        return {
            "name": self.name,
            "because": self.because,
            "evidence_ids": list(self.evidence_ids),
            "used": self.used,
            "refused_because": self.refused_because,
        }


@dataclass(frozen=True)
class SearchCluster:
    """One proposed capability or domain after the program has ruled on its shape."""

    kind: ClusterKind
    name: str
    terms: tuple[str, ...]
    because: str
    evidence_ids: tuple[str, ...]
    used: bool
    refused_because: str = ""

    def describe(self) -> dict[str, Any]:
        """Return the cluster as the task result reports it."""
        return {
            "kind": self.kind.value,
            "name": self.name,
            "terms": list(self.terms),
            "because": self.because,
            "evidence_ids": list(self.evidence_ids),
            "used": self.used,
            "refused_because": self.refused_because,
        }


def searchable_titles(
    proposed: list[ProposedTitle],
    parameters: JobSearchParameters,
    known_ids: set[str],
) -> list[SearchTitle]:
    """Decide, in Python, which synthesized names this search may use.

    Excluded titles are matched by :func:`roles.title_excluded`, the rule
    screening applies, so a synthesized title is refused exactly when the
    postings it finds would be. Under ``strict`` nothing synthesized joins the
    gate, but it is still reported, marked unused.
    """
    stated = {normalize(title) for title in parameters.seniority.stated_titles}
    refused = [title for title in parameters.seniority.excluded_titles if title.strip()]
    strict = parameters.seniority.title_matching == "strict"
    decided: list[SearchTitle] = []
    seen: set[str] = set()
    for item in proposed:
        decided.append(_search_title(item, known_ids, stated, refused, strict, seen))
    return decided


def searched_titles(
    parameters: JobSearchParameters,
    accepted: Sequence[TargetTitle],
    proposed: Sequence[SearchTitle],
) -> list[TargetTitle]:
    """Interleave stated, accepted, and usable names without weakening strict mode.

    Interleaving keeps a long stated list from crowding accepted and proposed
    titles out of a bounded plan. Duplicates keep the earliest source, and
    strict matching admits only person-stated titles.
    """
    sources: list[Sequence[TargetTitle]] = [parameters.seniority.tracked_titles]
    if parameters.seniority.title_matching != "strict":
        sources.extend(
            (
                accepted,
                tuple(
                    TargetTitle(title=item.title, lane=item.lane) for item in proposed if item.used
                ),
            )
        )
    tracked: dict[str, TargetTitle] = {}
    for position in range(max((len(source) for source in sources), default=0)):
        for source in sources:
            if position < len(source):
                item = source[position]
                tracked.setdefault(item.title, item)
    return list(tracked.values())


def _search_title(
    item: ProposedTitle,
    known_ids: set[str],
    stated: set[str],
    refused: list[str],
    strict: bool,
    seen: set[str],
) -> SearchTitle:
    """Sanitize and classify one proposed search title."""
    title = flatten(item.title)[:MAX_TITLE_LENGTH]
    cited = tuple(
        entry_id for entry_id in dict.fromkeys(item.evidence_ids) if entry_id in known_ids
    )
    normalized = normalize(title)
    because = _title_refusal(title, normalized, stated, seen, refused, strict, cited)
    seen.add(normalized)
    return SearchTitle(
        title=title,
        reason=flatten(item.reason)[:300],
        evidence_ids=cited,
        used=not because,
        refused_because=because,
        lane=item.lane,
    )


def searchable_archetypes(
    proposed: list[ProposedArchetype],
    parameters: JobSearchParameters,
    known_ids: set[str],
) -> list[SearchArchetype]:
    """Decide, in Python, which proposed kinds of organization reach the inbox.

    The rules are the title rules minus the two that are about titles: an
    archetype does not widen the title gate, so ``strict`` does not silence it,
    and there is no list of refused archetypes to check against.
    """
    stated = {normalize_text(item.name) for item in parameters.employers.stated_archetypes}
    decided: list[SearchArchetype] = []
    seen: set[str] = set()
    for item in proposed:
        decided.append(_search_archetype(item, known_ids, stated, seen))
    return decided


def searchable_clusters(
    proposed: list[ProposedCluster],
    parameters: JobSearchParameters,
    kind: ClusterKind,
    known_ids: set[str],
) -> list[SearchCluster]:
    """Decide, in Python, which proposed clusters of one kind reach the inbox."""
    stated = {normalize_text(item.name) for item in parameters.focus.stated_clusters(kind)}
    decided: list[SearchCluster] = []
    seen: set[str] = set()
    for item in proposed:
        decided.append(_search_cluster(item, kind, known_ids, stated, seen))
    return decided


def _search_cluster(
    item: ProposedCluster,
    kind: ClusterKind,
    known_ids: set[str],
    stated: set[str],
    seen: set[str],
) -> SearchCluster:
    """Sanitize and classify one proposed cluster."""
    name = flatten(item.name)[:MAX_CLUSTER_LENGTH]
    terms = tuple(
        dict.fromkeys(flatten(term)[:MAX_CLUSTER_LENGTH] for term in item.terms if flatten(term))
    )[:MAX_CLUSTER_TERMS]
    cited = tuple(
        entry_id for entry_id in dict.fromkeys(item.evidence_ids) if entry_id in known_ids
    )
    normalized = normalize_text(name)
    because = _archetype_refusal(name, normalized, stated, seen, cited)
    seen.add(normalized)
    return SearchCluster(
        kind=kind,
        name=name,
        terms=terms,
        because=flatten(item.because)[:300],
        evidence_ids=cited,
        used=not because,
        refused_because=because,
    )


def searched_archetypes(
    parameters: JobSearchParameters, accepted: Sequence[EmployerArchetype]
) -> list[EmployerArchetype]:
    """Return every kind of organization this run states, the person's own first.

    A kind the person wrote outranks the same kind a model proposed, so their
    reason for it is the one the search is told.
    """
    kept: dict[str, EmployerArchetype] = {}
    for item in (*parameters.employers.stated_archetypes, *accepted):
        kept.setdefault(normalize_text(item.name), item)
    return list(kept.values())


def _search_archetype(
    item: ProposedArchetype,
    known_ids: set[str],
    stated: set[str],
    seen: set[str],
) -> SearchArchetype:
    """Sanitize and classify one proposed employer archetype."""
    name = flatten(item.name)[:MAX_ARCHETYPE_LENGTH]
    cited = tuple(
        entry_id for entry_id in dict.fromkeys(item.evidence_ids) if entry_id in known_ids
    )
    normalized = normalize_text(name)
    because = _archetype_refusal(name, normalized, stated, seen, cited)
    seen.add(normalized)
    return SearchArchetype(
        name=name,
        because=flatten(item.because)[:300],
        evidence_ids=cited,
        used=not because,
        refused_because=because,
    )


def _archetype_refusal(
    name: str,
    normalized: str,
    stated: set[str],
    seen: set[str],
    cited: tuple[str, ...],
) -> str:
    """Return why a proposed archetype must not be offered, if it must not."""
    if not name:
        return "empty"
    if normalized in stated:
        return "already_stated"
    if normalized in seen:
        return "duplicate"
    if not cited:
        return "missing_evidence"
    return ""


def _title_refusal(
    title: str,
    normalized: str,
    stated: set[str],
    seen: set[str],
    refused: list[str],
    strict: bool,
    cited: tuple[str, ...],
) -> str:
    """Return why a proposed title must not be searched, if it must not."""
    if not title:
        return "empty"
    if normalized in stated:
        return "already_stated"
    if normalized in seen:
        return "duplicate"
    if title_excluded(title, refused):
        return "excluded_title"
    if strict:
        return "strict_title_matching"
    if not cited:
        return "missing_evidence"
    return ""
