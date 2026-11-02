import type { PendingDiscoveryFacets, PendingDiscoveryPage } from "../../api/client";
import { MultiSelectFilter } from "../../components/MultiSelectFilter";
import { Toggletip } from "../../components/Toggletip";
import {
  discoveryRecommendationLabel,
  relevanceLabel,
  type RelevanceKey,
} from "./relevanceReviewModel";
import {
  ARRANGEMENT_CATEGORY_VALUES,
  CONTENT_READINESS_LABELS,
  CONTENT_READINESS_ORDER,
  CONSTRAINT_CONFIDENCE_LABELS,
  REVIEW_STALENESS_LABELS,
  REVIEW_STALENESS_ORDER,
  STAGING_PROVENANCE_LABELS,
  STAGING_PROVENANCE_ORDER,
  arrangementLabel,
  type ArrangementFilter,
  type Category,
  type ContentReadinessFilter,
  type ConstraintFilter,
  type Marker,
  type PendingQueueFilters,
  type QueuePageSize,
  type QueueSortMode,
  type ReviewStalenessFilter,
  type RelevanceReviewFilter,
  type StagingProvenanceFilter,
} from "./pendingDiscoveryFilterModel";

const MARKER_CHIPS: readonly { marker: Marker; label: string; legend: string }[] = [
  { marker: "favorite", label: "Favorites", legend: "Favorite: your best matches." },
  { marker: "kept", label: "Kept", legend: "Keep: rows the program misjudged." },
];

type FacetEntry<Value> = { readonly value: Value; readonly count: number };

function countOf<Value>(entries: readonly FacetEntry<Value>[] | undefined, value: Value): number {
  return entries?.find((entry) => entry.value === value)?.count ?? 0;
}

function totalOf<Value>(entries: readonly FacetEntry<Value>[] | undefined): number {
  return (entries ?? []).reduce((sum, entry) => sum + entry.count, 0);
}

export function PendingDiscoveryFilters({
  countPending,
  employers,
  facets,
  filters,
  laneStatus,
  matching,
  onChange,
  onPageSizeChange,
  onSearchTextChange,
  onSortChange,
  onStrictUncertaintyChange,
  pageSize,
  queueTotal,
  searchText,
  sortMode,
  strictUncertainty,
}: {
  countPending: boolean;
  employers: PendingDiscoveryPage["employers"];
  facets: PendingDiscoveryFacets | undefined;
  filters: PendingQueueFilters;
  laneStatus: string | null;
  matching: number;
  onChange: (next: PendingQueueFilters) => void;
  onPageSizeChange: (value: QueuePageSize) => void;
  onSearchTextChange: (value: string) => void;
  onSortChange: (value: QueueSortMode) => void;
  onStrictUncertaintyChange: (value: boolean) => void;
  pageSize: QueuePageSize;
  queueTotal: number;
  searchText: string;
  sortMode: QueueSortMode;
  strictUncertainty: boolean;
}) {
  // Each dimension is counted under every other filter, so its own choice hides none of its options.
  const verdictCount = (value: RelevanceKey): string => {
    const entry = facets?.relevance.find((item) => item.value === value);
    const stale = entry?.out_of_date ?? 0;
    return stale > 0 ? `${entry?.count ?? 0}, ${stale} out of date` : `${entry?.count ?? 0}`;
  };
  const readinessTotal = (facets?.readiness ?? []).reduce((sum, entry) => sum + entry.holding, 0);
  const mainRoute = strictUncertainty ? "pursue_only" : "not_excluded";
  const groupsPending = countPending || !facets;
  const constraintLabels: Record<ConstraintFilter, string> = {
    ...CONSTRAINT_CONFIDENCE_LABELS,
    all: "All constraint routes",
    not_excluded: "Not excluded",
    pursue_only: "Main queue: pursue",
    needs_clarification: "Unconfirmed matches",
    excluded_only: "Screened out",
  };
  const activeSecondaryFilters = [
    filters.constraint !== mainRoute && `Constraint route: ${constraintLabels[filters.constraint]}`,
    filters.category !== "all" &&
      `Show category: ${discoveryRecommendationLabel(filters.category)}`,
    filters.reviewStatus !== "all" &&
      `Review status: ${REVIEW_STALENESS_LABELS[filters.reviewStatus]}`,
    filters.arrangement !== "all" && `Work arrangement: ${arrangementLabel(filters.arrangement)}`,
    filters.provenance !== "all" &&
      `Where it came from: ${STAGING_PROVENANCE_LABELS[filters.provenance]}`,
    filters.employers.length > 0 && `Employer: ${filters.employers.join(", ")}`,
  ].filter(Boolean);
  return (
    <>
      {strictUncertainty && filters.constraint === "all" ? (
        <p className="field-note" role="status">
          Showing all constraint routes to reveal the posting you added. Choose Main queue to return
          to strict view.
        </p>
      ) : null}
      <div
        aria-label="Uncertainty groups"
        className="button-row discovery-toolbar__groups"
        role="group"
      >
        <label>
          <input
            checked={strictUncertainty}
            onChange={(event) => onStrictUncertaintyChange(event.target.checked)}
            type="checkbox"
          />{" "}
          Separate uncertain postings
        </label>
        <Toggletip label="About uncertain postings">
          {strictUncertainty
            ? "Unconfirmed matches holds postings with missing or unconfirmed facts, such as location or pay."
            : "Postings with missing or unconfirmed facts, such as location or pay, stay in Main queue. Turn this on to move them to Unconfirmed matches."}
        </Toggletip>
        <button
          aria-busy={groupsPending}
          aria-disabled={groupsPending}
          aria-pressed={filters.constraint === mainRoute}
          className="button button--quiet"
          onClick={() => {
            if (!groupsPending) onChange({ ...filters, constraint: mainRoute });
          }}
          type="button"
        >
          Main queue ({facets ? countOf(facets.constraint, mainRoute) : "loading"})
        </button>
        {strictUncertainty ? (
          <button
            aria-busy={groupsPending}
            aria-disabled={groupsPending}
            aria-pressed={filters.constraint === "needs_clarification"}
            className="button button--quiet"
            onClick={() => {
              if (!groupsPending) onChange({ ...filters, constraint: "needs_clarification" });
            }}
            type="button"
          >
            Unconfirmed matches (
            {facets ? countOf(facets.constraint, "needs_clarification") : "loading"})
          </button>
        ) : null}
        <button
          aria-busy={groupsPending}
          aria-disabled={groupsPending}
          aria-pressed={filters.constraint === "excluded_only"}
          className="button button--quiet"
          onClick={() => {
            if (!groupsPending) onChange({ ...filters, constraint: "excluded_only" });
          }}
          type="button"
        >
          Screened out ({facets ? countOf(facets.constraint, "excluded_only") : "loading"})
        </button>
      </div>
      <div className="discovery-toolbar__filters">
        <div className="discovery-toolbar__field">
          <label htmlFor="discovery-search">Search</label>
          <input
            id="discovery-search"
            maxLength={200}
            onChange={(event) => onSearchTextChange(event.target.value)}
            placeholder="Employer, role, or place"
            type="search"
            value={searchText}
          />
        </div>
        <div className="discovery-toolbar__field">
          <label htmlFor="discovery-content-readiness">Content readiness</label>
          <select
            id="discovery-content-readiness"
            onChange={(event) =>
              onChange({ ...filters, readiness: event.target.value as ContentReadinessFilter })
            }
            value={filters.readiness}
          >
            <option value="all">All content states ({readinessTotal})</option>
            {CONTENT_READINESS_ORDER.map((value) => (
              <option key={value} value={value}>
                {CONTENT_READINESS_LABELS[value]} ({countOf(facets?.readiness, value)})
              </option>
            ))}
          </select>
        </div>
        <div className="discovery-toolbar__field">
          <label htmlFor="discovery-relevance-review-filter">Relevance verdict</label>
          <select
            id="discovery-relevance-review-filter"
            onChange={(event) =>
              onChange({ ...filters, relevance: event.target.value as RelevanceReviewFilter })
            }
            value={filters.relevance}
          >
            <option value="all">All relevance states ({totalOf(facets?.relevance)})</option>
            <option value="not_reviewed">
              Not yet reviewed ({countOf(facets?.relevance, "not_reviewed")})
            </option>
            {(
              [
                "review",
                "investigate",
                "disputed",
                "review_rejected",
                "unlikely",
                "insufficient_evidence",
                "insufficient_posting_text",
              ] as const
            ).map((value) => (
              <option key={value} value={value}>
                {relevanceLabel(value)} ({verdictCount(value)})
              </option>
            ))}
          </select>
        </div>
      </div>
      <details className="discovery-toolbar__more">
        <summary>
          {activeSecondaryFilters.length
            ? `More filters: ${activeSecondaryFilters.join("; ")}`
            : "More filters"}
        </summary>
        <div className="discovery-toolbar__secondary">
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-constraint-filter">Constraint route</label>
            <select
              id="discovery-constraint-filter"
              onChange={(event) =>
                onChange({ ...filters, constraint: event.target.value as ConstraintFilter })
              }
              value={filters.constraint}
            >
              {!strictUncertainty ? (
                <>
                  <option value="not_excluded">
                    Not excluded ({countOf(facets?.constraint, "not_excluded")})
                  </option>
                  <option value="all">
                    All confidence routes ({facets?.constraint_total ?? 0})
                  </option>
                </>
              ) : (
                <>
                  <option value="pursue_only">
                    Main queue: pursue ({countOf(facets?.constraint, "pursue_only")})
                  </option>
                  {filters.constraint === "all" ? (
                    <option value="all">
                      All constraint routes ({facets?.constraint_total ?? 0})
                    </option>
                  ) : null}
                </>
              )}
              <option value="needs_clarification">
                Unconfirmed matches ({countOf(facets?.constraint, "needs_clarification")})
              </option>
              {(strictUncertainty
                ? (["excluded_only", "excluded"] as const)
                : ([
                    "pursue",
                    "investigate",
                    "needs_verification",
                    "uncertain_comp",
                    "excluded_only",
                    "excluded",
                  ] as const)
              ).map((value) => (
                <option key={value} value={value}>
                  {constraintLabels[value]} ({countOf(facets?.constraint, value)})
                </option>
              ))}
            </select>
          </div>
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-category">Show category</label>
            <select
              id="discovery-category"
              onChange={(event) =>
                onChange({ ...filters, category: event.target.value as Category })
              }
              value={filters.category}
            >
              <option value="all">All ({totalOf(facets?.category)})</option>
              {(["review", "investigate", "unlikely"] as const).map((value) => (
                <option key={value} value={value}>
                  {discoveryRecommendationLabel(value)} ({countOf(facets?.category, value)})
                </option>
              ))}
            </select>
          </div>
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-review-staleness">Review status</label>
            <select
              id="discovery-review-staleness"
              onChange={(event) =>
                onChange({ ...filters, reviewStatus: event.target.value as ReviewStalenessFilter })
              }
              value={filters.reviewStatus}
            >
              <option value="all">All review states ({facets?.review_status_total ?? 0})</option>
              {REVIEW_STALENESS_ORDER.map((value) => (
                <option key={value} value={value}>
                  {REVIEW_STALENESS_LABELS[value]} ({countOf(facets?.review_status, value)})
                </option>
              ))}
            </select>
          </div>
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-arrangement-filter">Work arrangement</label>
            <select
              id="discovery-arrangement-filter"
              onChange={(event) =>
                onChange({ ...filters, arrangement: event.target.value as ArrangementFilter })
              }
              value={filters.arrangement}
            >
              <option value="all">All arrangements ({facets?.arrangement_total ?? 0})</option>
              {ARRANGEMENT_CATEGORY_VALUES.map((value) => (
                <option key={value} value={value}>
                  {arrangementLabel(value)} ({countOf(facets?.arrangement, value)})
                </option>
              ))}
            </select>
          </div>
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-staging-provenance">Where it came from</label>
            <select
              id="discovery-staging-provenance"
              onChange={(event) =>
                onChange({ ...filters, provenance: event.target.value as StagingProvenanceFilter })
              }
              value={filters.provenance}
            >
              <option value="all">All origins ({totalOf(facets?.provenance)})</option>
              {STAGING_PROVENANCE_ORDER.map((value) => (
                <option key={value} value={value}>
                  {STAGING_PROVENANCE_LABELS[value]} ({countOf(facets?.provenance, value)})
                </option>
              ))}
            </select>
          </div>
          <MultiSelectFilter
            allLabel="All employers"
            label="Employer"
            onChange={(next) => onChange({ ...filters, employers: next })}
            options={employers.map((employer) => ({
              value: employer.name,
              label: employer.name,
              count: employer.count,
            }))}
            selected={filters.employers}
          />
        </div>
      </details>
      <div aria-label="Your markers" className="chip-row discovery-toolbar__markers" role="group">
        {MARKER_CHIPS.map(({ marker, label, legend }) => (
          <button
            aria-pressed={filters.markers[marker]}
            className="chip"
            key={marker}
            onClick={() =>
              onChange({
                ...filters,
                markers: { ...filters.markers, [marker]: !filters.markers[marker] },
              })
            }
            title={legend}
            type="button"
          >
            {label} ({facets?.markers[marker] ?? 0})
          </button>
        ))}
      </div>
      <div className="discovery-toolbar__view-row">
        <span className="discovery-toolbar__count" role="status">
          {queueTotal > 0 ? (
            <span>
              {countPending
                ? "Updating the count for your filters"
                : matching < queueTotal
                  ? `${matching} of ${queueTotal} in the queue match your filters; ${queueTotal - matching} hidden`
                  : `${matching} of ${queueTotal} in the queue match`}
            </span>
          ) : null}
          {queueTotal > 0 && !countPending && laneStatus ? `. ${laneStatus}` : null}
        </span>
        <div className="discovery-toolbar__view">
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-sort">Sort by</label>
            <select
              id="discovery-sort"
              onChange={(event) => onSortChange(event.target.value as QueueSortMode)}
              value={sortMode}
            >
              <option value="relevance">Relevance</option>
              <option value="rank">Discovery rank</option>
            </select>
          </div>
          <div className="discovery-toolbar__field">
            <label htmlFor="discovery-page-size">Rows per page</label>
            <select
              id="discovery-page-size"
              onChange={(event) => onPageSizeChange(event.target.value as QueuePageSize)}
              value={pageSize}
            >
              <option value="50">50</option>
              <option value="100">100</option>
              <option value="all">All</option>
            </select>
          </div>
        </div>
      </div>
    </>
  );
}
