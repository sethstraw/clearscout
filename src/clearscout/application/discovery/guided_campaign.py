"""Compile a guided campaign: coverage branches, attempt groups, and manifest.

One branch per title topic or focus bundle covers every actionable market.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from clearscout.application.discovery.board_slugs import TENANT_ATS_HOSTS
from clearscout.application.discovery.derived_vocabulary_rules import searched_archetypes
from clearscout.application.discovery.titles import ladder_words, role_words
from clearscout.domain.search_parameters import (
    Acceptance,
    ClusterKind,
    EmployerArchetype,
    JobSearchParameters,
    Lane,
    Location,
    TargetTitle,
)

COMPILER_VERSION = "gap-campaign-v1"
# Two branches per attempt with three searches of headroom keep a 40-branch plan
# inside 20 calls and 100 searches.
BRANCHES_PER_ATTEMPT = 2
SEARCH_HEADROOM_PER_ATTEMPT = 3
SEARCHES_PER_ATTEMPT = 5
DEFAULT_MAX_MODEL_CALLS = 20
DEFAULT_MAX_HOSTED_SEARCHES = 100
CAMPAIGN_MAX_OUTPUT_TOKENS = 16000

UNLANED = "unlaned"
MAX_TITLE_ALIASES_PER_TOPIC = 4
MAX_FOCUSES_PER_BUNDLE = 3
MAX_FLOOR_QUERY_LENGTH = 300
_ATS_SITE_CLAUSE = "(" + " OR ".join(f"site:{host}" for host in TENANT_ATS_HOSTS) + ")"


class BranchKind(StrEnum):
    """Whether a coverage identity names a title family or a focus bundle."""

    TITLE_TOPIC = "title_topic"
    FOCUS_BUNDLE = "focus_bundle"


class SearchMode(StrEnum):
    """The kind of accepted vocabulary carried by one focus atom."""

    MANDATE_CAPABILITY = "mandate_capability"
    DOMAIN_INTERSECTION = "domain_intersection"
    EMPLOYER_ARCHETYPE = "employer_archetype"


# The priority family a focus bundle reports under, keyed by its shared mode.
_BUNDLE_FAMILY = {
    SearchMode.MANDATE_CAPABILITY: "capability",
    SearchMode.DOMAIN_INTERSECTION: "domain",
    SearchMode.EMPLOYER_ARCHETYPE: "archetype",
}


@dataclass(frozen=True)
class _SearchFocus:
    """One accepted search atom assigned to a focus bundle."""

    mode: SearchMode
    value: str


@dataclass(frozen=True)
class _SearchTarget:
    """One non-refused market and compatible working arrangement."""

    place: str
    arrangement: str
    acceptance: Acceptance | None


@dataclass(frozen=True)
class _TitleTopic:
    """Up to four semantically related aliases in one lane."""

    aliases: tuple[str, ...]
    lane: Lane | None
    rung: tuple[str, ...]


def _digest(value: object) -> str:
    """Return the stable digest of one JSON-compatible manifest value."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _focus_atoms(
    parameters: JobSearchParameters, archetypes: Sequence[EmployerArchetype]
) -> tuple[_SearchFocus, ...]:
    """Return every person-stated or accepted focus once in stable order."""
    atoms: list[_SearchFocus] = []
    for mode, kind in (
        (SearchMode.MANDATE_CAPABILITY, ClusterKind.CAPABILITY),
        (SearchMode.DOMAIN_INTERSECTION, ClusterKind.DOMAIN),
    ):
        atoms.extend(
            _SearchFocus(mode, cluster.name.strip())
            for cluster in parameters.focus.stated_clusters(kind)
        )
    atoms.extend(
        _SearchFocus(SearchMode.EMPLOYER_ARCHETYPE, archetype.name.strip())
        for archetype in searched_archetypes(parameters, archetypes)
        if archetype.name.strip()
    )
    first: dict[tuple[SearchMode, str], _SearchFocus] = {}
    for atom in atoms:
        first.setdefault((atom.mode, atom.value), atom)
    return tuple(first.values())


def _contains_location(broad: Location, narrow: Location) -> bool:
    """Return whether one declared place geographically contains another."""
    if broad.key == narrow.key or broad.country.casefold() != narrow.country.casefold():
        return False
    if broad.region and broad.region.casefold() != narrow.region.casefold():
        return False
    if broad.metro and broad.metro.casefold() != narrow.metro.casefold():
        return False
    return (not broad.region and bool(narrow.region)) or (not broad.metro and bool(narrow.metro))


def _is_redundant_target(
    parameters: JobSearchParameters,
    location: Location,
    arrangement: str,
    acceptance: Acceptance | None,
) -> bool:
    """Drop a narrow target already represented by an equivalent broader rule."""
    for broad in parameters.locations:
        if not broad.actionable or not _contains_location(broad, location):
            continue
        if broad.eligibility != location.eligibility:
            continue
        broad_bands = tuple(
            band.model_copy(update={"location": ""}) for band in parameters.bands_for(broad.key)
        )
        narrow_bands = tuple(
            band.model_copy(update={"location": ""}) for band in parameters.bands_for(location.key)
        )
        if broad_bands != narrow_bands:
            continue
        broad_acceptance = parameters.arrangement_for(broad.key).acceptance_of(arrangement)
        if broad_acceptance == acceptance:
            return True
    return False


def _targets_for_location(
    parameters: JobSearchParameters, location: Location
) -> tuple[_SearchTarget, ...]:
    """Return normalized usable targets for one actionable place."""
    arrangement = parameters.arrangement_for(location.key)
    named: tuple[tuple[str, Acceptance | None], ...] = tuple(
        (name, acceptance)
        for name, acceptance in arrangement.named().items()
        if acceptance is not None and acceptance is not Acceptance.REFUSED
    )
    if not arrangement.stated:
        named = (("", None),)
    targets: list[_SearchTarget] = []
    for name, acceptance in named:
        if _is_redundant_target(parameters, location, name, acceptance):
            continue
        targets.append(_SearchTarget(location.render(), name, acceptance))
    return tuple(targets)


def _search_targets(parameters: JobSearchParameters) -> tuple[_SearchTarget, ...]:
    """Return distinct markets at the broadest authorized useful scope."""
    targets = [
        target
        for location in parameters.locations
        if location.actionable
        for target in _targets_for_location(parameters, location)
    ]
    return tuple(dict.fromkeys(targets))


def _same_topic(left: _TitleTopic, candidate: TargetTitle) -> bool:
    """Return whether an alias can safely share one title topic."""
    title = candidate.title.strip()
    if left.lane != candidate.lane:
        return False
    # Explicit rung variants may share a topic with each other, but a generic
    # title needs its own topic so it can search the entire stated level band.
    if bool(left.rung) != bool(ladder_words(title)):
        return False
    return any(len(role_words(title) & role_words(alias)) >= 2 for alias in left.aliases)


def _title_topics(titles: Sequence[TargetTitle]) -> tuple[_TitleTopic, ...]:
    """Pack coherent aliases by lane while retaining every explicit rung."""
    distinct: dict[str, TargetTitle] = {}
    for item in titles:
        title = item.title.strip()
        if title:
            distinct.setdefault(title.casefold(), TargetTitle(title=title, lane=item.lane))
    topics: list[_TitleTopic] = []
    for item in distinct.values():
        for index, topic in enumerate(topics):
            if len(topic.aliases) < MAX_TITLE_ALIASES_PER_TOPIC and _same_topic(topic, item):
                topics[index] = _TitleTopic(
                    (*topic.aliases, item.title),
                    topic.lane,
                    tuple(sorted(set(topic.rung) | ladder_words(item.title))),
                )
                break
        else:
            topics.append(
                _TitleTopic((item.title,), item.lane, tuple(sorted(ladder_words(item.title))))
            )
    return tuple(topics)


def _focus_bundles(atoms: Sequence[_SearchFocus]) -> tuple[tuple[_SearchFocus, ...], ...]:
    """Bundle a bounded number of same-mode atoms without pairing them to a title."""
    by_mode = {mode: tuple(atom for atom in atoms if atom.mode is mode) for mode in SearchMode}
    return tuple(
        values[start : start + MAX_FOCUSES_PER_BUNDLE]
        for values in by_mode.values()
        for start in range(0, len(values), MAX_FOCUSES_PER_BUNDLE)
    )


def _seniority_scope(parameters: JobSearchParameters) -> str:
    """Render the full stated band carried by an unlevelled or focus-only branch."""
    minimum = parameters.seniority.minimum
    maximum = parameters.seniority.maximum
    if minimum and maximum and minimum != maximum:
        return f"{minimum} through {maximum}"
    if minimum:
        return f"{minimum} and above" if not maximum else minimum
    if maximum:
        return f"up to {maximum}"
    return ""


def _normalize_anchor(anchor: str) -> str:
    """Strip quotes so the anchor survives inside a floor query the model runs verbatim."""
    return anchor.replace('"', "").replace("'", "").strip()


def _render_floor_terms(anchors: Sequence[str], places: Sequence[str], remote: bool) -> str:
    """Render a floor query's length-bounded terms, each anchor quoted as a posting carries it."""
    anchor_clause = " OR ".join(f'"{_normalize_anchor(anchor)}"' for anchor in anchors)
    place_terms = [*places, *(("remote",) if remote else ())]
    place_clause = " OR ".join(place_terms)
    if place_clause:
        return f"({anchor_clause}) ({place_clause})"
    return f"({anchor_clause})"


def _trimmed_to_term_boundary(anchor: str, budget: int) -> str:
    """Cut an anchor at a word boundary rather than mid-word, within ``budget`` characters."""
    if len(anchor) <= budget:
        return anchor
    cut = anchor[:budget].rsplit(" ", 1)[0]
    return cut or anchor[:budget]


def _floor_query(anchors: Sequence[str], markets: Sequence[str], remote: bool) -> str:
    """Return one chunk's floor query: the site clause, then terms within the length bound."""
    return f"{_ATS_SITE_CLAUSE} {_floor_terms_within_bound(anchors, markets, remote)}"


def _floor_terms_within_bound(anchors: Sequence[str], markets: Sequence[str], remote: bool) -> str:
    """Return a chunk's floor terms, trimming anchors only once no market is left."""
    kept_anchors = list(anchors) or [""]
    kept_places = list(markets)
    while True:
        query = _render_floor_terms(kept_anchors, kept_places, remote)
        if len(query) <= MAX_FLOOR_QUERY_LENGTH:
            return query
        if kept_places:
            kept_places.pop()
        elif len(kept_anchors) > 1:
            kept_anchors.pop()
        else:
            overflow = len(query) - MAX_FLOOR_QUERY_LENGTH
            trimmed = _trimmed_to_term_boundary(
                kept_anchors[0], max(1, len(kept_anchors[0]) - overflow)
            )
            if trimmed == kept_anchors[0]:
                return query[:MAX_FLOOR_QUERY_LENGTH]
            kept_anchors[0] = trimmed


def _floor_query_chunks(
    anchors: Sequence[str],
    markets: Sequence[str],
    remote_markets: frozenset[str],
) -> tuple[tuple[tuple[str, ...], str], ...]:
    """Split markets into chunks that each fit one floor query, one branch per chunk.

    A market that does not fit alongside the ones already packed starts the
    next chunk rather than being dropped, so no market goes unsearched.
    """
    if not markets:
        return (((), _floor_query(anchors, (), False)),)
    chunks: list[tuple[tuple[str, ...], str]] = []
    remaining = list(markets)
    while remaining:
        kept: list[str] = []
        for market in list(remaining):
            candidate = [*kept, market]
            remote = bool(candidate) and all(place in remote_markets for place in candidate)
            if (
                kept
                and len(_render_floor_terms(anchors, candidate, remote)) > MAX_FLOOR_QUERY_LENGTH
            ):
                break
            kept = candidate
        for market in kept:
            remaining.remove(market)
        remote = bool(kept) and all(place in remote_markets for place in kept)
        chunks.append((tuple(kept), _floor_query(anchors, kept, remote)))
    return tuple(chunks)


@dataclass(frozen=True)
class CampaignBranch:
    """One coverage identity a campaign attempt may run a floor query for."""

    key: str
    kind: BranchKind
    priority_family: str
    protected: bool
    markets: tuple[str, ...]
    market_arrangements: tuple[tuple[str, tuple[str, ...]], ...]
    lane: Lane | None
    seniority: str
    title_aliases: tuple[str, ...]
    focuses: tuple[tuple[str, str], ...]
    floor_query: str
    anchors: tuple[str, ...]
    label: str

    def describe(self) -> dict[str, object]:
        """Return the stable wire representation of this branch."""
        return {
            "key": self.key,
            "kind": self.kind.value,
            "priority_family": self.priority_family,
            "protected": self.protected,
            "markets": list(self.markets),
            "market_arrangements": [
                [market, list(arrangements)] for market, arrangements in self.market_arrangements
            ],
            "lane": self.lane.value if self.lane is not None else "",
            "seniority": self.seniority,
            "title_aliases": list(self.title_aliases),
            "focuses": [{"mode": mode, "value": value} for mode, value in self.focuses],
            "floor_query": self.floor_query,
            "anchors": list(self.anchors),
            "label": self.label,
        }


def _build_branch(
    *,
    kind: BranchKind,
    priority_family: str,
    markets: tuple[str, ...],
    market_arrangements: tuple[tuple[str, tuple[str, ...]], ...],
    lane: Lane | None,
    seniority: str,
    title_aliases: tuple[str, ...],
    focuses: tuple[tuple[str, str], ...],
    floor_query: str,
    anchors: tuple[str, ...],
    label: str,
) -> CampaignBranch:
    """Build one branch, keyed by the content a recompile reproduces identically."""
    branch = CampaignBranch(
        key="",
        kind=kind,
        priority_family=priority_family,
        protected=False,
        markets=markets,
        market_arrangements=market_arrangements,
        lane=lane,
        seniority=seniority,
        title_aliases=title_aliases,
        focuses=focuses,
        floor_query=floor_query,
        anchors=anchors,
        label=label,
    )
    identity = {k: v for k, v in branch.describe().items() if k not in ("key", "protected")}
    return replace(branch, key=_digest(identity))


def _round_robin_by_family(branches: Sequence[CampaignBranch]) -> tuple[CampaignBranch, ...]:
    """Interleave branches so no single priority family monopolizes the front."""
    families: list[str] = []
    grouped: dict[str, list[CampaignBranch]] = {}
    for branch in branches:
        grouped.setdefault(branch.priority_family, []).append(branch)
        if branch.priority_family not in families:
            families.append(branch.priority_family)
    ordered: list[CampaignBranch] = []
    for position in range(max((len(items) for items in grouped.values()), default=0)):
        for family in families:
            items = grouped[family]
            if position < len(items):
                ordered.append(items[position])
    return tuple(ordered)


@dataclass(frozen=True)
class CampaignAttemptPlan:
    """One paid model call and the branches it is responsible for."""

    position: int
    branch_keys: tuple[str, ...]
    search_cap: int

    def describe(self) -> dict[str, object]:
        """Return the stable wire representation of this attempt group."""
        return {
            "position": self.position,
            "branch_keys": list(self.branch_keys),
            "search_cap": self.search_cap,
        }


@dataclass(frozen=True)
class CampaignBounds:
    """The planned bounds the compiler sizes one campaign's attempts to."""

    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS
    max_hosted_searches: int = DEFAULT_MAX_HOSTED_SEARCHES
    max_output_tokens: int = CAMPAIGN_MAX_OUTPUT_TOKENS
    searches_per_attempt: int = SEARCHES_PER_ATTEMPT

    def document(self) -> dict[str, int]:
        """Return the stable wire representation of these bounds."""
        return {
            "max_model_calls": self.max_model_calls,
            "max_hosted_searches": self.max_hosted_searches,
            "max_output_tokens": self.max_output_tokens,
            "searches_per_attempt": self.searches_per_attempt,
        }


@dataclass(frozen=True)
class CampaignIdentities:
    """Everything that identifies what this campaign was compiled and asked against."""

    search_parameter_set_id: str
    search_parameter_sha256: str
    master_cv_version_id: str
    master_cv_sha256: str
    accepted_vocabulary_sha256: str
    admission_scope_sha256: str
    prompt_digests: dict[str, str] = field(default_factory=dict)
    response_schema_version: str = ""
    provider: str = ""
    model: str = ""
    effort: str = ""
    verbosity: str = ""
    service_tier: str = ""

    def document(self) -> dict[str, object]:
        """Return the stable wire representation of these identities."""
        return {
            "search_parameter_set_id": self.search_parameter_set_id,
            "search_parameter_sha256": self.search_parameter_sha256,
            "master_cv_version_id": self.master_cv_version_id,
            "master_cv_sha256": self.master_cv_sha256,
            "accepted_vocabulary_sha256": self.accepted_vocabulary_sha256,
            "admission_scope_sha256": self.admission_scope_sha256,
            "prompt_digests": dict(sorted(self.prompt_digests.items())),
            "response_schema_version": self.response_schema_version,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "verbosity": self.verbosity,
            "service_tier": self.service_tier,
        }


@dataclass(frozen=True)
class CampaignManifest:
    """The complete, content-addressed campaign a worker refuses to recompile away from."""

    identities: CampaignIdentities
    compiler_version: str
    branches: tuple[CampaignBranch, ...]
    attempts: tuple[CampaignAttemptPlan, ...]
    bounds: CampaignBounds

    def document(self) -> dict[str, object]:
        """Return the canonical JSON document this manifest's digest is taken over."""
        return {
            "identities": self.identities.document(),
            "compiler_version": self.compiler_version,
            "branches": [branch.describe() for branch in self.branches],
            "attempts": [attempt.describe() for attempt in self.attempts],
            "bounds": self.bounds.document(),
        }

    @classmethod
    def from_document(cls, document: dict[str, object]) -> CampaignManifest:
        """Restore the immutable manifest stored with a campaign."""
        identities = document["identities"]
        branches = document["branches"]
        attempts = document["attempts"]
        bounds = document["bounds"]
        if (
            not isinstance(identities, dict)
            or not isinstance(bounds, dict)
            or not isinstance(branches, list)
            or not isinstance(attempts, list)
            or not all(isinstance(value, dict) for value in [*branches, *attempts])
        ):
            raise ValueError("Stored campaign manifest is malformed.")
        return cls(
            identities=CampaignIdentities(
                search_parameter_set_id=str(identities["search_parameter_set_id"]),
                search_parameter_sha256=str(identities["search_parameter_sha256"]),
                master_cv_version_id=str(identities["master_cv_version_id"]),
                master_cv_sha256=str(identities["master_cv_sha256"]),
                accepted_vocabulary_sha256=str(identities["accepted_vocabulary_sha256"]),
                admission_scope_sha256=str(identities["admission_scope_sha256"]),
                prompt_digests={
                    str(key): str(value)
                    for key, value in dict(identities["prompt_digests"]).items()
                },
                response_schema_version=str(identities["response_schema_version"]),
                provider=str(identities["provider"]),
                model=str(identities["model"]),
                effort=str(identities["effort"]),
                verbosity=str(identities["verbosity"]),
                service_tier=str(identities["service_tier"]),
            ),
            compiler_version=str(document["compiler_version"]),
            branches=tuple(
                CampaignBranch(
                    key=str(branch["key"]),
                    kind=BranchKind(str(branch["kind"])),
                    priority_family=str(branch["priority_family"]),
                    protected=bool(branch["protected"]),
                    markets=tuple(str(value) for value in branch["markets"]),
                    market_arrangements=tuple(
                        (str(market), tuple(str(value) for value in arrangements))
                        for market, arrangements in branch["market_arrangements"]
                    ),
                    lane=Lane(str(branch["lane"])) if branch["lane"] else None,
                    seniority=str(branch["seniority"]),
                    title_aliases=tuple(str(value) for value in branch["title_aliases"]),
                    focuses=tuple(
                        (str(focus["mode"]), str(focus["value"])) for focus in branch["focuses"]
                    ),
                    floor_query=str(branch["floor_query"]),
                    anchors=tuple(str(value) for value in branch["anchors"]),
                    label=str(branch["label"]),
                )
                for branch in branches
            ),
            attempts=tuple(
                CampaignAttemptPlan(
                    position=int(attempt["position"]),
                    branch_keys=tuple(str(value) for value in attempt["branch_keys"]),
                    search_cap=int(attempt["search_cap"]),
                )
                for attempt in attempts
            ),
            bounds=CampaignBounds(
                max_model_calls=int(bounds["max_model_calls"]),
                max_hosted_searches=int(bounds["max_hosted_searches"]),
                max_output_tokens=int(bounds["max_output_tokens"]),
                searches_per_attempt=int(bounds["searches_per_attempt"]),
            ),
        )

    @property
    def sha256(self) -> str:
        """Return this manifest's content digest."""
        return _digest(self.document())


def _market_arrangements_by_place(
    targets: Sequence[_SearchTarget], markets: Sequence[str]
) -> dict[str, tuple[str, ...]]:
    """Return each market's accepted arrangements, kept per market and never unioned.

    Never unioning across markets keeps a branch line from stating an
    arrangement for a market that actually refuses it.
    """
    return {
        market: tuple(
            dict.fromkeys(
                target.arrangement
                for target in targets
                if target.place == market and target.arrangement
            )
        )
        for market in markets
    }


def _topic_branches(
    topics: Sequence[_TitleTopic],
    markets: Sequence[str],
    market_arrangements: dict[str, tuple[str, ...]],
    remote_markets: frozenset[str],
    seniority_scope: str,
) -> list[CampaignBranch]:
    """Build one branch per market chunk for each title topic."""
    branches: list[CampaignBranch] = []
    for topic in topics:
        seniority = "" if topic.rung else seniority_scope
        priority_family = topic.lane.value if topic.lane is not None else UNLANED
        anchors = tuple(_normalize_anchor(alias) for alias in topic.aliases)
        for chunk_markets, floor_query in _floor_query_chunks(
            topic.aliases, markets, remote_markets
        ):
            branches.append(
                _build_branch(
                    kind=BranchKind.TITLE_TOPIC,
                    priority_family=priority_family,
                    markets=chunk_markets,
                    market_arrangements=tuple(
                        (market, market_arrangements[market]) for market in chunk_markets
                    ),
                    lane=topic.lane,
                    seniority=seniority,
                    title_aliases=topic.aliases,
                    focuses=(),
                    floor_query=floor_query,
                    anchors=anchors,
                    label=topic.aliases[0],
                )
            )
    return branches


def _bundle_branches(
    bundles: Sequence[tuple[_SearchFocus, ...]],
    markets: Sequence[str],
    market_arrangements: dict[str, tuple[str, ...]],
    seniority_scope: str,
) -> list[CampaignBranch]:
    """Build one branch per focus bundle over every market, with no floor query.

    The model searches each bundle with its own queries, built from its focuses.
    """
    return [
        _build_branch(
            kind=BranchKind.FOCUS_BUNDLE,
            priority_family=_BUNDLE_FAMILY[bundle[0].mode],
            markets=tuple(markets),
            market_arrangements=tuple((market, market_arrangements[market]) for market in markets),
            lane=None,
            seniority=seniority_scope,
            title_aliases=(),
            focuses=tuple((item.mode.value, item.value) for item in bundle),
            floor_query="",
            anchors=(),
            label=" | ".join(item.value for item in bundle),
        )
        for bundle in bundles
    ]


def _ordered_protected_branches(
    raw: Sequence[CampaignBranch], searched_branch_keys: frozenset[str]
) -> tuple[CampaignBranch, ...]:
    """Order unsearched branches first, round-robin, then mark each family's lead protected.

    A family carrying no title topic is left unprotected: a focus bundle has no
    floor query, and its focuses are not a title a posting must carry.
    """
    unsearched = [branch for branch in raw if branch.key not in searched_branch_keys]
    searched = [branch for branch in raw if branch.key in searched_branch_keys]
    ordered = (*_round_robin_by_family(unsearched), *_round_robin_by_family(searched))
    titled_families = {
        branch.priority_family for branch in raw if branch.kind is BranchKind.TITLE_TOPIC
    }
    seen_families: set[str] = set()
    final: list[CampaignBranch] = []
    for branch in ordered:
        protected = (
            branch.priority_family in titled_families
            and branch.priority_family not in seen_families
        )
        seen_families.add(branch.priority_family)
        final.append(replace(branch, protected=protected))
    return tuple(final)


def _plan_attempts(
    branches: Sequence[CampaignBranch], bounds: CampaignBounds
) -> tuple[CampaignAttemptPlan, ...]:
    """Group ordered branches into paid attempts, each capped at its branch count plus headroom.

    Attempts continue until every branch is assigned or a bound stops them.
    """
    attempts: list[CampaignAttemptPlan] = []
    branch_position = 0
    remaining_searches = bounds.max_hosted_searches
    for position in range(1, bounds.max_model_calls + 1):
        if branch_position >= len(branches) or remaining_searches <= 0:
            break
        group_size = min(BRANCHES_PER_ATTEMPT, len(branches) - branch_position)
        search_cap = min(
            group_size + SEARCH_HEADROOM_PER_ATTEMPT,
            bounds.searches_per_attempt,
            remaining_searches,
        )
        group = branches[branch_position : branch_position + min(group_size, search_cap)]
        attempts.append(
            CampaignAttemptPlan(
                position=position,
                branch_keys=tuple(branch.key for branch in group),
                search_cap=search_cap,
            )
        )
        branch_position += len(group)
        remaining_searches -= search_cap
    return tuple(attempts)


def compile_campaign(
    parameters: JobSearchParameters,
    titles: Sequence[TargetTitle],
    archetypes: Sequence[EmployerArchetype],
    *,
    identities: CampaignIdentities,
    bounds: CampaignBounds,
    searched_branch_keys: frozenset[str] = frozenset(),
) -> CampaignManifest:
    """Compile one campaign manifest from the current search scope.

    Unsearched branches come first, round-robin across priority families; branches
    past the last attempt stay listed without an attempt.
    """
    topics = _title_topics(titles)
    targets = _search_targets(parameters)
    atoms = _focus_atoms(parameters, archetypes)
    bundles = _focus_bundles(atoms)
    seniority_scope = _seniority_scope(parameters)
    markets = tuple(dict.fromkeys(target.place for target in targets if target.place))
    market_arrangements = _market_arrangements_by_place(targets, markets)
    remote_markets = frozenset(
        market for market, accepted in market_arrangements.items() if "remote" in accepted
    )

    raw = [
        *_topic_branches(topics, markets, market_arrangements, remote_markets, seniority_scope),
        *_bundle_branches(bundles, markets, market_arrangements, seniority_scope),
    ]
    final = _ordered_protected_branches(raw, searched_branch_keys)
    attempts = _plan_attempts(final, bounds)

    return CampaignManifest(
        identities=identities,
        compiler_version=COMPILER_VERSION,
        branches=final,
        attempts=attempts,
        bounds=bounds,
    )
